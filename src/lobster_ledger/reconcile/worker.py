from __future__ import annotations

import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from types import FrameType

from lobster_ledger import db, ledger
from lobster_ledger.reconcile.checkers import get_checker

log = logging.getLogger("lobster_ledger.reconcile")


@dataclass(frozen=True)
class ReconcileConfig:
    interval_seconds: float = 30.0
    batch_limit: int = 100
    max_age_hours: int = 72

    @classmethod
    def from_env(cls) -> ReconcileConfig:
        return cls(
            interval_seconds=float(os.environ.get("LL_RECONCILE_INTERVAL", "30")),
            batch_limit=int(os.environ.get("LL_RECONCILE_BATCH", "100")),
            max_age_hours=int(os.environ.get("LL_RECONCILE_MAX_AGE_HOURS", "72")),
        )


def run_once(conn: sqlite3.Connection, config: ReconcileConfig) -> dict[str, int]:
    # One reconciliation pass. Caller owns the connection lifecycle.
    now = db.now_ts()
    min_ts = now - config.max_age_hours * 3600
    rows = conn.execute(
        "SELECT * FROM transactions "
        "WHERE status='pending' AND external_id IS NOT NULL AND created_at >= ? "
        "ORDER BY created_at ASC LIMIT ?",
        (min_ts, config.batch_limit),
    ).fetchall()

    counts: dict[str, int] = {"checked": 0, "settled": 0, "failed": 0, "skipped": 0}
    for row in rows:
        counts["checked"] += 1
        checker = get_checker(str(row["rail"]))
        if checker is None:
            counts["skipped"] += 1
            continue
        result = checker.check(dict(row))
        if result.status == "settled":
            ledger.mark_settled(conn, int(row["id"]), raw_proof=result.raw_proof)
            counts["settled"] += 1
            log.info(
                "reconciled_settled",
                extra={"tx_id": int(row["id"]), "rail": str(row["rail"])},
            )
        elif result.status == "failed":
            ledger.mark_failed(
                conn,
                int(row["id"]),
                memo=f"reconcile: {result.reason}" if result.reason else "reconcile: failed",
            )
            counts["failed"] += 1
            log.info(
                "reconciled_failed",
                extra={
                    "tx_id": int(row["id"]),
                    "rail": str(row["rail"]),
                    "reason": result.reason,
                },
            )
        else:
            counts["skipped"] += 1
    return counts


def run_loop(config: ReconcileConfig | None = None) -> None:
    # Long-running poll loop. Exits cleanly on SIGTERM/SIGINT.
    cfg = config or ReconcileConfig.from_env()
    stop = {"flag": False}

    def _handler(signum: int, _frame: FrameType | None) -> None:
        stop["flag"] = True
        log.info("reconcile_shutdown_signal", extra={"signal": signum})

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    conn = db.connect()
    try:
        while not stop["flag"]:
            counts = run_once(conn, cfg)
            log.info("reconcile_cycle", extra=counts)
            slept = 0.0
            while slept < cfg.interval_seconds and not stop["flag"]:
                time.sleep(0.5)
                slept += 0.5
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_loop()
