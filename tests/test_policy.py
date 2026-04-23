from __future__ import annotations

import sqlite3
import time
from typing import Any

from lobster_ledger import db
from lobster_ledger.policy import evaluate
from lobster_ledger.types import PaymentRequest


def _insert_rule(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    config_dict: dict[str, Any],
    enabled: bool = True,
) -> int:
    with db.transaction(conn) as c:
        cur = c.execute(
            "INSERT INTO rules(name,kind,config_json,enabled,created_at) VALUES (?,?,?,?,?)",
            (
                name,
                kind,
                db.dumps({"kind": kind, **config_dict}),
                1 if enabled else 0,
                db.now_ts(),
            ),
        )
    rowid = cur.lastrowid
    assert rowid is not None
    return int(rowid)


def _insert_tx(
    conn: sqlite3.Connection,
    *,
    direction: str,
    rail: str,
    status: str,
    amount_usd_fmv_cents: int,
    created_at: int,
    category: str | None = None,
    counterparty: str | None = None,
) -> int:
    with db.transaction(conn) as c:
        cur = c.execute(
            "INSERT INTO transactions("
            "direction,rail,status,amount_usd_fmv_cents,category,counterparty,created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                direction,
                rail,
                status,
                amount_usd_fmv_cents,
                category,
                counterparty,
                created_at,
            ),
        )
    rowid = cur.lastrowid
    assert rowid is not None
    return int(rowid)


def test_empty_rules_allows_outbound(conn: sqlite3.Connection) -> None:
    request = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=100,
        amount_usdc_micro=1000,
    )
    result = evaluate(conn, request)
    assert result.outcome == "allow"
    assert result.reasons == []
    assert result.triggered_rules == []


def test_inbound_always_allowed_even_with_strict_cap(conn: sqlite3.Connection) -> None:
    _insert_rule(conn, "tight_cap", "spend_cap", {"max_amount_usd_cents": 1})
    request = PaymentRequest(
        rail="base",
        direction="in",
        amount_usd_fmv_cents=1_000_000,
        amount_usdc_micro=10_000_000_000,
    )
    result = evaluate(conn, request)
    assert result.outcome == "allow"
    assert result.triggered_rules == []


def test_spend_cap_under_allowed_over_denied(conn: sqlite3.Connection) -> None:
    _insert_rule(conn, "daily_cap", "spend_cap", {"max_amount_usd_cents": 1000})

    under = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=500,
        amount_usdc_micro=5_000_000,
    )
    result_under = evaluate(conn, under)
    assert result_under.outcome == "allow"

    over = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=1500,
        amount_usdc_micro=15_000_000,
    )
    result_over = evaluate(conn, over)
    assert result_over.outcome == "deny"
    assert len(result_over.triggered_rules) == 1
    assert result_over.triggered_rules[0].name == "daily_cap"
    assert "daily_cap" in result_over.reasons[0]


def test_rail_scoped_spend_cap(conn: sqlite3.Connection) -> None:
    _insert_rule(
        conn,
        "ln_cap",
        "spend_cap",
        {"max_amount_usd_cents": 500, "rail": "lightning"},
    )

    base_request = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=1000,
        amount_usdc_micro=10_000_000,
    )
    assert evaluate(conn, base_request).outcome == "allow"

    ln_request = PaymentRequest(
        rail="lightning",
        direction="out",
        amount_usd_fmv_cents=1000,
        amount_sats=100_000,
    )
    ln_result = evaluate(conn, ln_request)
    assert ln_result.outcome == "deny"
    assert ln_result.triggered_rules[0].name == "ln_cap"


def test_velocity_window_two_txs_exceed(conn: sqlite3.Connection) -> None:
    _insert_rule(
        conn,
        "hourly_velocity",
        "velocity",
        {"window_seconds": 3600, "max_total_usd_cents": 1000},
    )
    fixed_now = int(time.time())
    _insert_tx(
        conn,
        direction="out",
        rail="base",
        status="settled",
        amount_usd_fmv_cents=400,
        created_at=fixed_now - 100,
    )

    over = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=700,
        amount_usdc_micro=7_000_000,
    )
    over_result = evaluate(conn, over, now=fixed_now)
    assert over_result.outcome == "deny"
    assert over_result.triggered_rules[0].name == "hourly_velocity"

    under = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=500,
        amount_usdc_micro=5_000_000,
    )
    under_result = evaluate(conn, under, now=fixed_now)
    assert under_result.outcome == "allow"


def test_velocity_window_expired(conn: sqlite3.Connection) -> None:
    _insert_rule(
        conn,
        "hourly_velocity",
        "velocity",
        {"window_seconds": 3600, "max_total_usd_cents": 1000},
    )
    fixed_now = int(time.time())
    _insert_tx(
        conn,
        direction="out",
        rail="base",
        status="settled",
        amount_usd_fmv_cents=800,
        created_at=fixed_now - 7200,
    )

    request = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=500,
        amount_usdc_micro=5_000_000,
    )
    result = evaluate(conn, request, now=fixed_now)
    assert result.outcome == "allow"


def test_allowlist_match_and_mismatch(conn: sqlite3.Connection) -> None:
    _insert_rule(
        conn,
        "known_vendors",
        "allowlist",
        {"entries": ["alice", "bob"]},
    )

    match = PaymentRequest(
        rail="lightning",
        direction="out",
        amount_usd_fmv_cents=100,
        amount_sats=1000,
        counterparty="lnurl-alice-shop",
    )
    assert evaluate(conn, match).outcome == "allow"

    miss = PaymentRequest(
        rail="lightning",
        direction="out",
        amount_usd_fmv_cents=100,
        amount_sats=1000,
        counterparty="lnurl-eve",
    )
    miss_result = evaluate(conn, miss)
    assert miss_result.outcome == "deny"


def test_blocklist_overrides_allowlist(conn: sqlite3.Connection) -> None:
    _insert_rule(conn, "known_vendors", "allowlist", {"entries": ["alice"]})
    _insert_rule(conn, "banned", "blocklist", {"entries": ["alice"]})

    request = PaymentRequest(
        rail="lightning",
        direction="out",
        amount_usd_fmv_cents=100,
        amount_sats=1000,
        counterparty="alice",
    )
    result = evaluate(conn, request)
    assert result.outcome == "deny"
    assert len(result.triggered_rules) == 1
    assert result.triggered_rules[0].kind == "blocklist"
    assert result.triggered_rules[0].name == "banned"


def test_category_budget_scopes_by_category(conn: sqlite3.Connection) -> None:
    _insert_rule(
        conn,
        "inference_budget",
        "category_budget",
        {
            "category": "inference",
            "window_seconds": 3600,
            "max_total_usd_cents": 500,
        },
    )
    fixed_now = int(time.time())
    _insert_tx(
        conn,
        direction="out",
        rail="base",
        status="settled",
        amount_usd_fmv_cents=300,
        category="inference",
        created_at=fixed_now - 100,
    )
    _insert_tx(
        conn,
        direction="out",
        rail="base",
        status="settled",
        amount_usd_fmv_cents=400,
        category="storage",
        created_at=fixed_now - 100,
    )

    inference = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=300,
        amount_usdc_micro=3_000_000,
        category="inference",
    )
    inference_result = evaluate(conn, inference, now=fixed_now)
    assert inference_result.outcome == "deny"
    assert inference_result.triggered_rules[0].name == "inference_budget"

    storage = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=300,
        amount_usdc_micro=3_000_000,
        category="storage",
    )
    storage_result = evaluate(conn, storage, now=fixed_now)
    assert storage_result.outcome == "allow"


def test_require_approval_vs_spend_cap_at_same_threshold(
    conn: sqlite3.Connection,
) -> None:
    _insert_rule(conn, "approval_gate", "require_approval_above", {"threshold_usd_cents": 500})
    cap_id = _insert_rule(conn, "hard_cap", "spend_cap", {"max_amount_usd_cents": 500})

    over = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=600,
        amount_usdc_micro=6_000_000,
    )
    over_result = evaluate(conn, over)
    assert over_result.outcome == "deny"
    assert over_result.triggered_rules[0].name == "hard_cap"

    at_threshold = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=500,
        amount_usdc_micro=5_000_000,
    )
    at_result = evaluate(conn, at_threshold)
    assert at_result.outcome == "allow"

    # Disable the hard cap so only the approval rule is active; strict > means
    # 501 breaches a 500 threshold and the outcome becomes require_approval.
    with db.transaction(conn) as c:
        c.execute("UPDATE rules SET enabled = 0 WHERE id = ?", (cap_id,))

    just_over = PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=501,
        amount_usdc_micro=5_010_000,
    )
    just_over_result = evaluate(conn, just_over)
    assert just_over_result.outcome == "require_approval"
    assert just_over_result.triggered_rules[0].name == "approval_gate"
