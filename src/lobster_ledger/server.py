from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from lobster_ledger import db, ledger, payments, policy
from lobster_ledger.types import (
    Direction,
    PaymentRequest,
    Rail,
    RuleKind,
    TxStatus,
)

# Context is generic over session, lifespan, and request types; this alias keeps
# mypy strict happy without pinning tools to a specific server session shape.
ToolContext = Context[Any, Any, Any]

mcp = FastMCP("lobster-ledger")


def _request_from_tx_row(row: sqlite3.Row | dict[str, Any]) -> PaymentRequest:
    # Rebuilding the PaymentRequest from the stored row keeps adapter calls symmetric
    # whether the path is propose->pay or approval->pay.
    return PaymentRequest(
        rail=row["rail"],
        direction=row["direction"],
        amount_usd_fmv_cents=int(row["amount_usd_fmv_cents"]),
        amount_sats=row["amount_sats"],
        amount_usdc_micro=row["amount_usdc_micro"],
        counterparty=row["counterparty"],
        category=row["category"],
        memo=row["memo"],
        agent_id=row["agent_id"],
        external_id=row["external_id"],
        wallet_id=row["wallet_id"],
    )


def _do_propose_payment(
    conn: sqlite3.Connection,
    *,
    rail: Rail,
    amount_usd_cents: int,
    amount_sats: int | None,
    amount_usdc_micro: int | None,
    counterparty: str | None,
    category: str | None,
    memo: str | None,
    agent_id: str | None,
    external_id: str | None,
    wallet_id: int | None,
) -> dict[str, Any]:
    req = PaymentRequest(
        rail=rail,
        direction="out",
        amount_usd_fmv_cents=amount_usd_cents,
        amount_sats=amount_sats,
        amount_usdc_micro=amount_usdc_micro,
        counterparty=counterparty,
        category=category,
        memo=memo,
        agent_id=agent_id,
        external_id=external_id,
        wallet_id=wallet_id,
    )
    result = policy.evaluate(conn, req)
    policy_dict = result.model_dump(mode="json")

    if result.outcome == "deny":
        tx_id = ledger.record_transaction(conn, req, status="denied", policy=result)
        return {"status": "denied", "tx_id": tx_id, "policy": policy_dict}

    if result.outcome == "require_approval":
        tx_id = ledger.record_transaction(conn, req, status="pending", policy=result)
        reason = "; ".join(result.reasons) if result.reasons else None
        approval_id = ledger.create_approval(conn, tx_id, reason=reason)
        return {
            "status": "pending_approval",
            "tx_id": tx_id,
            "approval_id": approval_id,
            "policy": policy_dict,
        }

    tx_id = ledger.record_transaction(conn, req, status="pending", policy=result)
    adapter = payments.get_adapter(rail)
    success, _external_id, raw_proof = adapter.pay(req)
    if not success:
        ledger.mark_failed(conn, tx_id, memo="adapter returned failure")
        return {"status": "failed", "tx_id": tx_id, "policy": policy_dict}
    ledger.mark_settled(conn, tx_id, raw_proof=raw_proof)
    return {"status": "settled", "tx_id": tx_id, "policy": policy_dict}


def _do_check_balance(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    wallets = ledger.list_wallets(conn, only_active=True)
    out: list[dict[str, Any]] = []
    for w in wallets:
        wallet_id = int(w["id"])
        rail = w["rail"]
        lots = conn.execute(
            "SELECT original_units, remaining_units, basis_cents FROM lots "
            "WHERE wallet_id=? AND remaining_units > 0",
            (wallet_id,),
        ).fetchall()
        sats = 0
        usdc_micro = 0
        usd_fmv_cents = 0
        for lot in lots:
            remaining = int(lot["remaining_units"])
            original = int(lot["original_units"])
            basis = int(lot["basis_cents"])
            if rail == "lightning":
                sats += remaining
            elif rail == "base":
                usdc_micro += remaining
            else:
                # Manual rail does not distinguish native denominations; skip the native tally.
                pass
            usd_fmv_cents += round(basis * remaining / original) if original > 0 else 0

        pending = conn.execute(
            "SELECT COALESCE(SUM(amount_sats), 0) AS sats_out, "
            "COALESCE(SUM(amount_usdc_micro), 0) AS usdc_out "
            "FROM transactions WHERE wallet_id=? AND direction='out' AND status='pending'",
            (wallet_id,),
        ).fetchone()
        if pending is not None:
            if rail == "lightning":
                sats -= int(pending["sats_out"] or 0)
            elif rail == "base":
                usdc_micro -= int(pending["usdc_out"] or 0)

        out.append(
            {
                "wallet_id": wallet_id,
                "label": w["label"],
                "rail": rail,
                "identifier": w["identifier"],
                "sats": sats,
                "usdc_micro": usdc_micro,
                "usd_fmv_cents": usd_fmv_cents,
            }
        )
    return out


def _do_set_rule(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: RuleKind,
    config: dict[str, Any],
) -> dict[str, Any]:
    rule_id = ledger.create_rule(conn, name, kind, config)
    return {"rule_id": rule_id}


def _do_list_rules(conn: sqlite3.Connection, *, only_enabled: bool) -> list[dict[str, Any]]:
    return ledger.list_rules(conn, only_enabled=only_enabled)


def _do_disable_rule(conn: sqlite3.Connection, *, rule_id: int) -> dict[str, Any]:
    ok = ledger.set_rule_enabled(conn, rule_id, False)
    return {"ok": ok}


def _do_enable_rule(conn: sqlite3.Connection, *, rule_id: int) -> dict[str, Any]:
    ok = ledger.set_rule_enabled(conn, rule_id, True)
    return {"ok": ok}


def _do_delete_rule(conn: sqlite3.Connection, *, rule_id: int) -> dict[str, Any]:
    ok = ledger.delete_rule(conn, rule_id)
    return {"ok": ok}


def _do_register_wallet(
    conn: sqlite3.Connection,
    *,
    label: str,
    rail: Rail,
    identifier: str | None,
) -> dict[str, Any]:
    wallet_id = ledger.register_wallet(conn, label, rail, identifier)
    return {"wallet_id": wallet_id}


def _do_list_wallets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return ledger.list_wallets(conn)


def _do_list_pending_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return ledger.list_pending_approvals(conn)


def _do_resolve_approval(
    conn: sqlite3.Connection,
    *,
    tx_id: int,
    approved: bool,
    reason: str | None,
) -> dict[str, Any]:
    approval = ledger.resolve_approval(conn, tx_id, approved, reason)
    if approval is None:
        return {"approval": None, "settlement": "not_found"}

    if not approved:
        return {"approval": approval, "settlement": "denied"}

    tx_row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if tx_row is None:
        return {"approval": approval, "settlement": "not_found"}

    req = _request_from_tx_row(tx_row)
    adapter = payments.get_adapter(req.rail)
    success, _external_id, raw_proof = adapter.pay(req)
    if not success:
        ledger.mark_failed(conn, tx_id, memo="adapter returned failure after approval")
        return {"approval": approval, "settlement": "failed"}
    ledger.mark_settled(conn, tx_id, raw_proof=raw_proof)
    return {"approval": approval, "settlement": "settled"}


def _do_query_transactions(
    conn: sqlite3.Connection,
    *,
    status: TxStatus | None,
    rail: Rail | None,
    category: str | None,
    agent_id: str | None,
    since: int | None,
    until: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    return ledger.query_transactions(
        conn,
        status=status,
        rail=rail,
        category=category,
        agent_id=agent_id,
        since=since,
        until=until,
        limit=limit,
    )


def _do_export_1099_da(conn: sqlite3.Connection, *, year: int) -> dict[str, Any]:
    csv_text = ledger.export_1099_da(conn, year)
    return {"csv": csv_text}


def _do_record_manual_transaction(
    conn: sqlite3.Connection,
    *,
    rail: Rail,
    direction: Direction,
    amount_usd_fmv_cents: int,
    amount_sats: int | None,
    amount_usdc_micro: int | None,
    counterparty: str | None,
    category: str | None,
    memo: str | None,
    agent_id: str | None,
    external_id: str | None,
    wallet_id: int | None,
) -> dict[str, Any]:
    req = PaymentRequest(
        rail=rail,
        direction=direction,
        amount_usd_fmv_cents=amount_usd_fmv_cents,
        amount_sats=amount_sats,
        amount_usdc_micro=amount_usdc_micro,
        counterparty=counterparty,
        category=category,
        memo=memo,
        agent_id=agent_id,
        external_id=external_id,
        wallet_id=wallet_id,
    )
    tx_id = ledger.record_transaction(
        conn,
        req,
        status="settled",
        settled_at=db.now_ts(),
    )
    return {"tx_id": tx_id}


@mcp.tool()
async def propose_payment(
    rail: Rail,
    amount_usd_cents: int,
    amount_sats: int | None = None,
    amount_usdc_micro: int | None = None,
    counterparty: str | None = None,
    category: str | None = None,
    memo: str | None = None,
    agent_id: str | None = None,
    external_id: str | None = None,
    wallet_id: int | None = None,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    """Propose an outbound payment. Runs the policy gate; on allow, invokes the rail
    adapter and records settlement. On require_approval, records a pending transaction
    and an approval row. On deny, records a denied transaction. The caller supplies
    USD FMV; the ledger does not fetch prices. Returns status, tx_id, and policy result.
    """
    if ctx is not None:
        await ctx.info(f"propose_payment called rail={rail} amount_usd_cents={amount_usd_cents}")
    conn = db.connect()
    try:
        result = _do_propose_payment(
            conn,
            rail=rail,
            amount_usd_cents=amount_usd_cents,
            amount_sats=amount_sats,
            amount_usdc_micro=amount_usdc_micro,
            counterparty=counterparty,
            category=category,
            memo=memo,
            agent_id=agent_id,
            external_id=external_id,
            wallet_id=wallet_id,
        )
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"propose_payment completed status={result.get('status')}")
    return result


@mcp.tool()
async def check_balance(ctx: ToolContext | None = None) -> list[dict[str, Any]]:
    """Return per-wallet balances derived from open FIFO lots, minus pending outbound
    native units. USD FMV is a proportional share of remaining lot basis.
    """
    if ctx is not None:
        await ctx.info("check_balance called")
    conn = db.connect()
    try:
        result = _do_check_balance(conn)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"check_balance completed wallets={len(result)}")
    return result


@mcp.tool()
async def set_rule(
    name: str,
    kind: RuleKind,
    config: dict[str, Any],
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    """Create a policy rule. Config is validated against the discriminated union
    keyed on kind. Rules are enabled on creation.
    """
    if ctx is not None:
        await ctx.info(f"set_rule called name={name} kind={kind}")
    conn = db.connect()
    try:
        result = _do_set_rule(conn, name=name, kind=kind, config=config)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"set_rule completed rule_id={result.get('rule_id')}")
    return result


@mcp.tool()
async def list_rules(
    only_enabled: bool = False,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    """List all policy rules. Set only_enabled=True to skip disabled rules."""
    if ctx is not None:
        await ctx.info(f"list_rules called only_enabled={only_enabled}")
    conn = db.connect()
    try:
        result = _do_list_rules(conn, only_enabled=only_enabled)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"list_rules completed count={len(result)}")
    return result


@mcp.tool()
async def disable_rule(rule_id: int, ctx: ToolContext | None = None) -> dict[str, Any]:
    """Operator only, not for autonomous use by the agent being governed.
    Disable a rule so policy evaluation skips it.
    """
    if ctx is not None:
        await ctx.info(f"disable_rule called rule_id={rule_id}")
    conn = db.connect()
    try:
        result = _do_disable_rule(conn, rule_id=rule_id)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"disable_rule completed ok={result.get('ok')}")
    return result


@mcp.tool()
async def enable_rule(rule_id: int, ctx: ToolContext | None = None) -> dict[str, Any]:
    """Enable a previously disabled rule."""
    if ctx is not None:
        await ctx.info(f"enable_rule called rule_id={rule_id}")
    conn = db.connect()
    try:
        result = _do_enable_rule(conn, rule_id=rule_id)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"enable_rule completed ok={result.get('ok')}")
    return result


@mcp.tool()
async def delete_rule(rule_id: int, ctx: ToolContext | None = None) -> dict[str, Any]:
    """Operator only, not for autonomous use by the agent being governed.
    Permanently remove a rule.
    """
    if ctx is not None:
        await ctx.info(f"delete_rule called rule_id={rule_id}")
    conn = db.connect()
    try:
        result = _do_delete_rule(conn, rule_id=rule_id)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"delete_rule completed ok={result.get('ok')}")
    return result


@mcp.tool()
async def register_wallet(
    label: str,
    rail: Rail,
    identifier: str | None = None,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    """Register a wallet on a given rail. The identifier is opaque to the ledger
    (e.g. a Lightning node pubkey or a Base address).
    """
    if ctx is not None:
        await ctx.info(f"register_wallet called label={label} rail={rail}")
    conn = db.connect()
    try:
        result = _do_register_wallet(conn, label=label, rail=rail, identifier=identifier)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"register_wallet completed wallet_id={result.get('wallet_id')}")
    return result


@mcp.tool()
async def list_wallets(ctx: ToolContext | None = None) -> list[dict[str, Any]]:
    """List active wallets."""
    if ctx is not None:
        await ctx.info("list_wallets called")
    conn = db.connect()
    try:
        result = _do_list_wallets(conn)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"list_wallets completed count={len(result)}")
    return result


@mcp.tool()
async def list_pending_approvals(ctx: ToolContext | None = None) -> list[dict[str, Any]]:
    """List all approvals that are still pending, joined with their transaction fields."""
    if ctx is not None:
        await ctx.info("list_pending_approvals called")
    conn = db.connect()
    try:
        result = _do_list_pending_approvals(conn)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"list_pending_approvals completed count={len(result)}")
    return result


@mcp.tool()
async def resolve_approval(
    tx_id: int,
    approved: bool,
    reason: str | None = None,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    """Operator only, not for autonomous use by the agent being governed.
    Approve or deny a pending approval. Approval drives adapter settlement. Denial
    flips the transaction to status denied and does not invoke the adapter.
    """
    if ctx is not None:
        await ctx.info(f"resolve_approval called tx_id={tx_id} approved={approved}")
    conn = db.connect()
    try:
        result = _do_resolve_approval(conn, tx_id=tx_id, approved=approved, reason=reason)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"resolve_approval completed settlement={result.get('settlement')}")
    return result


@mcp.tool()
async def query_transactions(
    status: TxStatus | None = None,
    rail: Rail | None = None,
    category: str | None = None,
    agent_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int = 100,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    """Query transactions with optional filters. since and until are epoch seconds."""
    if ctx is not None:
        await ctx.info(f"query_transactions called limit={limit}")
    conn = db.connect()
    try:
        result = _do_query_transactions(
            conn,
            status=status,
            rail=rail,
            category=category,
            agent_id=agent_id,
            since=since,
            until=until,
            limit=limit,
        )
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"query_transactions completed count={len(result)}")
    return result


@mcp.tool()
async def export_1099_da(year: int, ctx: ToolContext | None = None) -> dict[str, Any]:
    """Export the 1099-DA CSV for the given tax year. Header is stamped
    # 1099-DA 2025 schema v1 and maps to IRS Form 1099-DA (Rev Jan 2025).
    """
    if ctx is not None:
        await ctx.info(f"export_1099_da called year={year}")
    conn = db.connect()
    try:
        result = _do_export_1099_da(conn, year=year)
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"export_1099_da completed bytes={len(result.get('csv', ''))}")
    return result


@mcp.tool()
async def record_manual_transaction(
    rail: Rail,
    direction: Direction,
    amount_usd_fmv_cents: int,
    amount_sats: int | None = None,
    amount_usdc_micro: int | None = None,
    counterparty: str | None = None,
    category: str | None = None,
    memo: str | None = None,
    agent_id: str | None = None,
    external_id: str | None = None,
    wallet_id: int | None = None,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    """Record a manual historical transaction. Bypasses the policy gate because
    these are imports, not proposals. Settled immediately so lot accounting runs.
    """
    if ctx is not None:
        await ctx.info(f"record_manual_transaction called rail={rail} direction={direction}")
    conn = db.connect()
    try:
        result = _do_record_manual_transaction(
            conn,
            rail=rail,
            direction=direction,
            amount_usd_fmv_cents=amount_usd_fmv_cents,
            amount_sats=amount_sats,
            amount_usdc_micro=amount_usdc_micro,
            counterparty=counterparty,
            category=category,
            memo=memo,
            agent_id=agent_id,
            external_id=external_id,
            wallet_id=wallet_id,
        )
    finally:
        conn.close()
    if ctx is not None:
        await ctx.info(f"record_manual_transaction completed tx_id={result.get('tx_id')}")
    return result


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
