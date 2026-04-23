from __future__ import annotations

import sqlite3

from lobster_ledger import ledger
from lobster_ledger.server import (
    _do_check_balance,
    _do_delete_rule,
    _do_disable_rule,
    _do_enable_rule,
    _do_export_1099_da,
    _do_list_pending_approvals,
    _do_list_rules,
    _do_list_wallets,
    _do_propose_payment,
    _do_query_transactions,
    _do_record_manual_transaction,
    _do_register_wallet,
    _do_resolve_approval,
    _do_set_rule,
    mcp,
)
from lobster_ledger.types import PaymentRequest

EXPECTED_TOOLS = {
    "propose_payment",
    "check_balance",
    "set_rule",
    "list_rules",
    "disable_rule",
    "enable_rule",
    "delete_rule",
    "register_wallet",
    "list_wallets",
    "list_pending_approvals",
    "resolve_approval",
    "query_transactions",
    "export_1099_da",
    "record_manual_transaction",
}


def test_server_registers_tools() -> None:
    tools = mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS.issubset(names)
    assert len(EXPECTED_TOOLS & names) == 14


def test_register_wallet_and_list(conn: sqlite3.Connection) -> None:
    result = _do_register_wallet(conn, label="hot-ln", rail="lightning", identifier="node-a")
    assert result["wallet_id"] > 0
    wallets = _do_list_wallets(conn)
    assert len(wallets) == 1
    assert wallets[0]["label"] == "hot-ln"


def test_set_list_disable_enable_delete_rule(conn: sqlite3.Connection) -> None:
    r1 = _do_set_rule(
        conn,
        name="cap-lightning-5usd",
        kind="spend_cap",
        config={"max_amount_usd_cents": 500, "rail": "lightning"},
    )
    assert r1["rule_id"] > 0

    all_rules = _do_list_rules(conn, only_enabled=False)
    assert len(all_rules) == 1

    disabled = _do_disable_rule(conn, rule_id=r1["rule_id"])
    assert disabled["ok"] is True
    enabled_only = _do_list_rules(conn, only_enabled=True)
    assert enabled_only == []

    re_enabled = _do_enable_rule(conn, rule_id=r1["rule_id"])
    assert re_enabled["ok"] is True
    assert len(_do_list_rules(conn, only_enabled=True)) == 1

    deleted = _do_delete_rule(conn, rule_id=r1["rule_id"])
    assert deleted["ok"] is True
    assert _do_list_rules(conn, only_enabled=False) == []


def test_propose_payment_allow_path_settles(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    # Seed inbound lot so the outbound has basis to consume.
    ledger.record_transaction(
        conn,
        req=_ln_inbound(50_000, 10_00),
        status="settled",
        wallet_id=wallet_id,
    )

    result = _do_propose_payment(
        conn,
        rail="lightning",
        amount_usd_cents=500,
        amount_sats=25_000,
        amount_usdc_micro=None,
        counterparty="bob",
        category="ops",
        memo=None,
        agent_id="agent-1",
        external_id=None,
        wallet_id=wallet_id,
    )
    assert result["status"] == "settled"
    assert result["policy"]["outcome"] == "allow"
    tx = ledger.get_transaction(conn, result["tx_id"])
    assert tx is not None
    assert tx["status"] == "settled"
    assert tx["raw_proof"] == "stub-proof"


def test_propose_payment_deny_path_records_denied(conn: sqlite3.Connection) -> None:
    ledger.create_rule(
        conn,
        name="block-sanctioned",
        kind="blocklist",
        config={"entries": ["darknet"]},
    )
    result = _do_propose_payment(
        conn,
        rail="lightning",
        amount_usd_cents=500,
        amount_sats=1000,
        amount_usdc_micro=None,
        counterparty="darknet-market",
        category=None,
        memo=None,
        agent_id=None,
        external_id=None,
        wallet_id=None,
    )
    assert result["status"] == "denied"
    assert result["policy"]["outcome"] == "deny"
    tx = ledger.get_transaction(conn, result["tx_id"])
    assert tx is not None
    assert tx["status"] == "denied"


def test_propose_payment_approval_flow_settles_on_approve(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    ledger.record_transaction(
        conn,
        req=_ln_inbound(100_000, 10_000),
        status="settled",
        wallet_id=wallet_id,
    )
    ledger.create_rule(
        conn,
        name="approval-above-1usd",
        kind="require_approval_above",
        config={"threshold_usd_cents": 100},
    )

    propose = _do_propose_payment(
        conn,
        rail="lightning",
        amount_usd_cents=500,
        amount_sats=25_000,
        amount_usdc_micro=None,
        counterparty="bob",
        category=None,
        memo=None,
        agent_id=None,
        external_id=None,
        wallet_id=wallet_id,
    )
    assert propose["status"] == "pending_approval"

    pending = _do_list_pending_approvals(conn)
    assert len(pending) == 1
    assert pending[0]["tx_id"] == propose["tx_id"]

    resolved = _do_resolve_approval(
        conn,
        tx_id=propose["tx_id"],
        approved=True,
        reason="ok",
    )
    assert resolved["settlement"] == "settled"
    tx = ledger.get_transaction(conn, propose["tx_id"])
    assert tx is not None
    assert tx["status"] == "settled"
    assert tx["raw_proof"] == "stub-proof"


def test_resolve_approval_deny_path(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    ledger.record_transaction(
        conn,
        req=_ln_inbound(100_000, 10_000),
        status="settled",
        wallet_id=wallet_id,
    )
    ledger.create_rule(
        conn,
        name="approval-above-1usd",
        kind="require_approval_above",
        config={"threshold_usd_cents": 100},
    )
    propose = _do_propose_payment(
        conn,
        rail="lightning",
        amount_usd_cents=500,
        amount_sats=25_000,
        amount_usdc_micro=None,
        counterparty="bob",
        category=None,
        memo=None,
        agent_id=None,
        external_id=None,
        wallet_id=wallet_id,
    )
    resolved = _do_resolve_approval(
        conn,
        tx_id=propose["tx_id"],
        approved=False,
        reason="not this time",
    )
    assert resolved["settlement"] == "denied"
    tx = ledger.get_transaction(conn, propose["tx_id"])
    assert tx is not None
    assert tx["status"] == "denied"


def test_check_balance_reflects_lots_and_pending(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    ledger.record_transaction(
        conn,
        req=_ln_inbound(100_000, 5_000),
        status="settled",
        wallet_id=wallet_id,
    )
    # Pending outbound reduces native sats but not USD FMV (lots not yet consumed).
    ledger.record_transaction(
        conn,
        req=_ln_outbound(10_000, 500),
        status="pending",
        wallet_id=wallet_id,
    )
    balances = _do_check_balance(conn)
    assert len(balances) == 1
    b = balances[0]
    assert b["wallet_id"] == wallet_id
    assert b["rail"] == "lightning"
    assert b["sats"] == 100_000 - 10_000
    assert b["usd_fmv_cents"] == 5_000


def test_query_transactions_filters(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    ledger.record_transaction(
        conn,
        req=_ln_inbound(50_000, 1000),
        status="settled",
        wallet_id=wallet_id,
    )
    rows = _do_query_transactions(
        conn,
        status="settled",
        rail="lightning",
        category=None,
        agent_id=None,
        since=None,
        until=None,
        limit=10,
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "settled"


def test_record_manual_transaction_settles_directly(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    result = _do_record_manual_transaction(
        conn,
        rail="lightning",
        direction="in",
        amount_usd_fmv_cents=1234,
        amount_sats=10_000,
        amount_usdc_micro=None,
        counterparty="import-source",
        category=None,
        memo="historical",
        agent_id=None,
        external_id=None,
        wallet_id=wallet_id,
    )
    tx_id = result["tx_id"]
    tx = ledger.get_transaction(conn, tx_id)
    assert tx is not None
    assert tx["status"] == "settled"
    lots = conn.execute("SELECT * FROM lots WHERE source_tx_id=?", (tx_id,)).fetchall()
    assert len(lots) == 1


def test_export_1099_da_emits_header(conn: sqlite3.Connection) -> None:
    wallet_id = ledger.register_wallet(conn, "hot-ln", "lightning", "node-a")
    ledger.record_transaction(
        conn,
        req=_ln_inbound(100_000, 5000),
        status="settled",
        wallet_id=wallet_id,
    )
    ledger.record_transaction(
        conn,
        req=_ln_outbound(40_000, 2500),
        status="settled",
        wallet_id=wallet_id,
    )
    # Pick the year from the latest disposition event to keep the test stable.
    row = conn.execute(
        "SELECT strftime('%Y', created_at, 'unixepoch') AS y FROM disposition_events LIMIT 1"
    ).fetchone()
    assert row is not None
    year = int(row["y"])
    result = _do_export_1099_da(conn, year=year)
    csv_text = result["csv"]
    assert csv_text.startswith("# 1099-DA draft v1\n")
    assert (
        "tx_id,lot_id,rail,acquired_date,sold_date,units_consumed,"
        "proceeds_usd,basis_usd,realized_gain_usd,holding_period_days,term"
    ) in csv_text


# Small helpers keep the happy-path tests readable without pulling in test_ledger internals.
def _ln_inbound(sats: int, fmv_cents: int) -> PaymentRequest:
    return PaymentRequest(
        rail="lightning",
        direction="in",
        amount_usd_fmv_cents=fmv_cents,
        amount_sats=sats,
    )


def _ln_outbound(sats: int, fmv_cents: int) -> PaymentRequest:
    return PaymentRequest(
        rail="lightning",
        direction="out",
        amount_usd_fmv_cents=fmv_cents,
        amount_sats=sats,
        counterparty="bob",
    )
