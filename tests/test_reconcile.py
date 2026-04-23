from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import pytest

from lobster_ledger import db, ledger
from lobster_ledger.reconcile import checkers as checkers_mod
from lobster_ledger.reconcile import worker as worker_mod
from lobster_ledger.reconcile.checkers import CheckResult, ManualChecker, RailChecker
from lobster_ledger.reconcile.worker import ReconcileConfig, run_once


class _StubChecker:
    # Canned checker used to drive the worker deterministically.
    def __init__(self, result: CheckResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def check(self, tx: dict[str, Any]) -> CheckResult:
        self.calls.append(tx)
        return self._result


def _insert_pending_tx(
    conn: sqlite3.Connection,
    rail: str = "lightning",
    external_id: str | None = "hash-1",
    created_at: int | None = None,
    amount_sats: int | None = 1000,
    amount_usdc_micro: int | None = None,
) -> int:
    ts = created_at if created_at is not None else db.now_ts()
    cur = conn.execute(
        "INSERT INTO transactions(direction, rail, status, amount_sats, "
        "amount_usdc_micro, amount_usd_fmv_cents, external_id, created_at) "
        "VALUES('out', ?, 'pending', ?, ?, 100, ?, ?)",
        (rail, amount_sats, amount_usdc_micro, external_id, ts),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def test_run_once_with_empty_db_returns_zero_counts(conn: sqlite3.Connection) -> None:
    counts = run_once(conn, ReconcileConfig())
    assert counts == {"checked": 0, "settled": 0, "failed": 0, "skipped": 0}


def test_run_once_ignores_old_transactions(conn: sqlite3.Connection) -> None:
    old_ts = db.now_ts() - 10 * 86400
    _insert_pending_tx(conn, created_at=old_ts)
    counts = run_once(conn, ReconcileConfig(max_age_hours=72))
    assert counts["checked"] == 0


def test_run_once_marks_settled_when_checker_returns_settled(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tx_id = _insert_pending_tx(conn)
    stub = _StubChecker(CheckResult(status="settled", raw_proof='{"preimage":"deadbeef"}'))

    def _stub_get_checker(rail: str) -> RailChecker | None:
        return stub

    monkeypatch.setattr(worker_mod, "get_checker", _stub_get_checker)

    counts = run_once(conn, ReconcileConfig())
    assert counts["checked"] == 1
    assert counts["settled"] == 1

    row = ledger.get_transaction(conn, tx_id)
    assert row is not None
    assert row["status"] == "settled"
    assert row["raw_proof"] == '{"preimage":"deadbeef"}'
    assert row["settled_at"] is not None


def test_run_once_marks_failed_when_checker_returns_failed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tx_id = _insert_pending_tx(conn)
    stub = _StubChecker(CheckResult(status="failed", reason="expired"))

    def _stub_get_checker(rail: str) -> RailChecker | None:
        return stub

    monkeypatch.setattr(worker_mod, "get_checker", _stub_get_checker)

    counts = run_once(conn, ReconcileConfig())
    assert counts["failed"] == 1

    row = ledger.get_transaction(conn, tx_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["memo"] is not None
    assert "expired" in row["memo"]
    assert "reconcile" in row["memo"]


def test_run_once_skips_unknown_rail(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rail CHECK constraint limits the DB to three rails, so we simulate the
    # "unknown rail" path by returning None from the dispatch for a known rail.
    tx_id = _insert_pending_tx(conn, rail="lightning")

    def _stub_get_checker(rail: str) -> RailChecker | None:
        return None

    monkeypatch.setattr(worker_mod, "get_checker", _stub_get_checker)

    counts = run_once(conn, ReconcileConfig())
    assert counts["checked"] == 1
    assert counts["skipped"] == 1
    assert counts["settled"] == 0
    assert counts["failed"] == 0

    row = ledger.get_transaction(conn, tx_id)
    assert row is not None
    assert row["status"] == "pending"


def test_run_once_honors_batch_limit(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = db.now_ts()
    for i in range(5):
        _insert_pending_tx(conn, external_id=f"hash-{i}", created_at=now - i)

    stub = _StubChecker(CheckResult(status="pending", reason="still pending"))

    def _stub_get_checker(rail: str) -> RailChecker | None:
        return stub

    monkeypatch.setattr(worker_mod, "get_checker", _stub_get_checker)

    counts = run_once(conn, ReconcileConfig(batch_limit=2))
    assert counts["checked"] == 2
    assert counts["skipped"] == 2
    assert len(stub.calls) == 2


def test_run_once_ignores_transactions_without_external_id(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_pending_tx(conn, external_id=None)
    stub = _StubChecker(CheckResult(status="settled", raw_proof="proof"))

    def _stub_get_checker(rail: str) -> RailChecker | None:
        return stub

    monkeypatch.setattr(worker_mod, "get_checker", _stub_get_checker)

    counts = run_once(conn, ReconcileConfig())
    assert counts == {"checked": 0, "settled": 0, "failed": 0, "skipped": 0}
    assert stub.calls == []


def test_manual_checker_always_pending() -> None:
    result = ManualChecker().check({"external_id": "anything", "rail": "manual"})
    assert result.status == "pending"
    assert result.reason == "manual rail skipped"


def test_get_checker_dispatches_by_rail() -> None:
    assert isinstance(checkers_mod.get_checker("manual"), ManualChecker)
    assert isinstance(checkers_mod.get_checker("lightning"), checkers_mod.NWCLightningChecker)
    assert isinstance(checkers_mod.get_checker("base"), checkers_mod.BaseUsdcChecker)
    assert checkers_mod.get_checker("mystery") is None


@pytest.mark.skip(reason="flaky in CI: signal handling across threads varies by platform")
def test_run_loop_responds_to_signal() -> None:  # pragma: no cover
    # Spawn the loop in a background thread and fire SIGINT at ourselves.
    import os
    import signal

    thread = threading.Thread(
        target=worker_mod.run_loop,
        args=(ReconcileConfig(interval_seconds=0.1),),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    os.kill(os.getpid(), signal.SIGINT)
    thread.join(timeout=3.0)
    assert not thread.is_alive()
