from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime
from typing import Any

from lobster_ledger import db
from lobster_ledger.types import (
    RULE_CONFIG_ADAPTER,
    PaymentRequest,
    PolicyResult,
    Rail,
    RuleKind,
    TxStatus,
)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def register_wallet(
    conn: sqlite3.Connection,
    label: str,
    rail: Rail,
    identifier: str | None = None,
) -> int:
    created_at = db.now_ts()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO wallets(label, rail, identifier, active, created_at) "
            "VALUES(?, ?, ?, 1, ?)",
            (label, rail, identifier, created_at),
        )
    wallet_id = cur.lastrowid
    assert wallet_id is not None
    return int(wallet_id)


def list_wallets(conn: sqlite3.Connection, only_active: bool = True) -> list[dict[str, Any]]:
    if only_active:
        rows = conn.execute("SELECT * FROM wallets WHERE active=1 ORDER BY id ASC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM wallets ORDER BY id ASC").fetchall()
    return _rows_to_dicts(rows)


def create_rule(
    conn: sqlite3.Connection,
    name: str,
    kind: RuleKind,
    config: dict[str, Any],
) -> int:
    # Validate via discriminated-union adapter so persisted config always matches the schema.
    validated = RULE_CONFIG_ADAPTER.validate_python({"kind": kind, **config})
    config_json = validated.model_dump_json()
    created_at = db.now_ts()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO rules(name, kind, config_json, enabled, created_at) VALUES(?, ?, ?, 1, ?)",
            (name, kind, config_json, created_at),
        )
    rule_id = cur.lastrowid
    assert rule_id is not None
    return int(rule_id)


def list_rules(conn: sqlite3.Connection, only_enabled: bool = False) -> list[dict[str, Any]]:
    if only_enabled:
        rows = conn.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY id ASC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM rules ORDER BY id ASC").fetchall()
    return _rows_to_dicts(rows)


def set_rule_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> bool:
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE rules SET enabled=? WHERE id=?",
            (1 if enabled else 0, rule_id),
        )
    return cur.rowcount > 0


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    with db.transaction(conn):
        cur = conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    return cur.rowcount > 0


def _create_lot_for_inbound(
    conn: sqlite3.Connection,
    tx_id: int,
    wallet_id: int,
    req: PaymentRequest,
    acquired_at: int,
) -> None:
    # Native units drive basis accounting; pick the rail's native unit first.
    units: int | None
    if req.rail == "lightning":
        units = req.amount_sats
    elif req.rail == "base":
        units = req.amount_usdc_micro
    else:
        # Manual rail: honour whichever native unit is present.
        units = req.amount_sats if req.amount_sats is not None else req.amount_usdc_micro
    if units is None or units <= 0:
        return
    conn.execute(
        "INSERT INTO lots(wallet_id, source_tx_id, rail, acquired_at, "
        "original_units, remaining_units, basis_cents) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            wallet_id,
            tx_id,
            req.rail,
            acquired_at,
            units,
            units,
            req.amount_usd_fmv_cents,
        ),
    )


def _consume_lots_fifo(
    conn: sqlite3.Connection,
    tx_id: int,
    wallet_id: int,
    req: PaymentRequest,
    settled_at: int,
) -> None:
    total_out: int | None = (
        req.amount_sats if req.amount_sats is not None else req.amount_usdc_micro
    )
    if total_out is None or total_out <= 0:
        return

    proceeds_total = req.amount_usd_fmv_cents
    remaining = total_out
    proceeds_allocated = 0

    lots = conn.execute(
        "SELECT * FROM lots WHERE wallet_id=? AND rail=? AND remaining_units>0 "
        "ORDER BY acquired_at ASC, id ASC",
        (wallet_id, req.rail),
    ).fetchall()

    for lot in lots:
        if remaining <= 0:
            break
        lot_remaining = int(lot["remaining_units"])
        lot_original = int(lot["original_units"])
        lot_basis = int(lot["basis_cents"])
        lot_acquired = int(lot["acquired_at"])
        take = min(lot_remaining, remaining)
        basis_share = round(lot_basis * take / lot_original)
        proceeds_share = round(proceeds_total * take / total_out)
        gain = proceeds_share - basis_share
        hold = (settled_at - lot_acquired) // 86400
        short_term = 1 if hold <= 365 else 0
        conn.execute(
            "INSERT INTO disposition_events(tx_id, lot_id, units_consumed, "
            "basis_cents, proceeds_cents, realized_gain_cents, "
            "holding_period_days, short_term, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tx_id,
                int(lot["id"]),
                take,
                basis_share,
                proceeds_share,
                gain,
                hold,
                short_term,
                settled_at,
            ),
        )
        conn.execute(
            "UPDATE lots SET remaining_units=remaining_units-? WHERE id=?",
            (take, int(lot["id"])),
        )
        proceeds_allocated += proceeds_share
        remaining -= take

    if remaining > 0:
        # Zero-basis residual: compute explicit remainder to sidestep rounding drift.
        residual_proceeds = proceeds_total - proceeds_allocated
        conn.execute(
            "INSERT INTO disposition_events(tx_id, lot_id, units_consumed, "
            "basis_cents, proceeds_cents, realized_gain_cents, "
            "holding_period_days, short_term, created_at) "
            "VALUES(?, NULL, ?, 0, ?, ?, 0, 1, ?)",
            (
                tx_id,
                remaining,
                residual_proceeds,
                residual_proceeds,
                settled_at,
            ),
        )
        warning = f" | zero-basis residual on {remaining} units"
        conn.execute(
            "UPDATE transactions SET memo = COALESCE(memo, '') || ? WHERE id=?",
            (warning, tx_id),
        )


def record_transaction(
    conn: sqlite3.Connection,
    req: PaymentRequest,
    status: TxStatus,
    policy: PolicyResult | None = None,
    wallet_id: int | None = None,
    raw_proof: str | None = None,
    settled_at: int | None = None,
) -> int:
    created_at = db.now_ts()
    effective_settled_at: int | None
    if status == "settled":
        effective_settled_at = settled_at if settled_at is not None else created_at
    else:
        effective_settled_at = None

    policy_json = policy.model_dump_json() if policy is not None else None

    effective_wallet_id = wallet_id if wallet_id is not None else req.wallet_id

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO transactions(direction, rail, status, wallet_id, "
            "amount_sats, amount_usdc_micro, amount_usd_fmv_cents, counterparty, "
            "category, memo, agent_id, external_id, raw_proof, policy_json, "
            "created_at, settled_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                req.direction,
                req.rail,
                status,
                effective_wallet_id,
                req.amount_sats,
                req.amount_usdc_micro,
                req.amount_usd_fmv_cents,
                req.counterparty,
                req.category,
                req.memo,
                req.agent_id,
                req.external_id,
                raw_proof,
                policy_json,
                created_at,
                effective_settled_at,
            ),
        )
        tx_id_raw = cur.lastrowid
        assert tx_id_raw is not None
        tx_id = int(tx_id_raw)

        if status == "settled" and effective_wallet_id is not None:
            anchor = effective_settled_at if effective_settled_at is not None else created_at
            if req.direction == "in":
                _create_lot_for_inbound(conn, tx_id, effective_wallet_id, req, anchor)
            elif req.direction == "out":
                _consume_lots_fifo(conn, tx_id, effective_wallet_id, req, anchor)

    return tx_id


def mark_settled(conn: sqlite3.Connection, tx_id: int, raw_proof: str | None = None) -> bool:
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        return False
    if row["status"] == "settled":
        # Idempotent: repeated calls after success are a no-op.
        return True

    settled_at = db.now_ts()
    with db.transaction(conn):
        if raw_proof is not None:
            conn.execute(
                "UPDATE transactions SET status='settled', settled_at=?, raw_proof=? WHERE id=?",
                (settled_at, raw_proof, tx_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET status='settled', settled_at=? WHERE id=?",
                (settled_at, tx_id),
            )

        wallet_id = row["wallet_id"]
        if wallet_id is not None:
            req = PaymentRequest(
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
                wallet_id=int(wallet_id),
            )
            if row["direction"] == "in":
                _create_lot_for_inbound(conn, tx_id, int(wallet_id), req, settled_at)
            elif row["direction"] == "out":
                _consume_lots_fifo(conn, tx_id, int(wallet_id), req, settled_at)

    return True


def mark_failed(conn: sqlite3.Connection, tx_id: int, memo: str | None = None) -> bool:
    row = conn.execute("SELECT memo FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        return False

    with db.transaction(conn):
        if memo is not None:
            existing = row["memo"]
            new_memo = f"{existing} | {memo}" if existing else memo
            conn.execute(
                "UPDATE transactions SET status='failed', memo=? WHERE id=?",
                (new_memo, tx_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET status='failed' WHERE id=?",
                (tx_id,),
            )
    return True


def get_transaction(conn: sqlite3.Connection, tx_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return _row_to_dict(row)


def query_transactions(
    conn: sqlite3.Connection,
    status: TxStatus | None = None,
    rail: Rail | None = None,
    category: str | None = None,
    agent_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if rail is not None:
        clauses.append("rail=?")
        params.append(rail)
    if category is not None:
        clauses.append("category=?")
        params.append(category)
    if agent_id is not None:
        clauses.append("agent_id=?")
        params.append(agent_id)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("created_at < ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM transactions {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return _rows_to_dicts(rows)


def create_approval(conn: sqlite3.Connection, tx_id: int, reason: str | None = None) -> int:
    created_at = db.now_ts()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO approvals(tx_id, status, reason, created_at) VALUES(?, 'pending', ?, ?)",
            (tx_id, reason, created_at),
        )
    approval_id = cur.lastrowid
    assert approval_id is not None
    return int(approval_id)


def resolve_approval(
    conn: sqlite3.Connection,
    tx_id: int,
    approved: bool,
    reason: str | None = None,
) -> dict[str, Any] | None:
    existing = conn.execute("SELECT * FROM approvals WHERE tx_id=?", (tx_id,)).fetchone()
    if existing is None:
        return None

    resolved_at = db.now_ts()
    new_status = "approved" if approved else "denied"

    with db.transaction(conn):
        if reason is not None:
            conn.execute(
                "UPDATE approvals SET status=?, reason=?, resolved_at=? WHERE tx_id=?",
                (new_status, reason, resolved_at, tx_id),
            )
        else:
            conn.execute(
                "UPDATE approvals SET status=?, resolved_at=? WHERE tx_id=?",
                (new_status, resolved_at, tx_id),
            )
        # Approve leaves tx pending so the server layer can drive settlement; deny flips tx.
        if not approved:
            conn.execute(
                "UPDATE transactions SET status='denied' WHERE id=?",
                (tx_id,),
            )

    updated = conn.execute("SELECT * FROM approvals WHERE tx_id=?", (tx_id,)).fetchone()
    return _row_to_dict(updated)


def list_pending_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.id AS approval_id, a.tx_id AS tx_id, a.status AS approval_status, "
        "a.reason AS approval_reason, a.created_at AS approval_created_at, "
        "a.resolved_at AS approval_resolved_at, "
        "t.direction, t.rail, t.status AS tx_status, t.wallet_id, "
        "t.amount_sats, t.amount_usdc_micro, t.amount_usd_fmv_cents, "
        "t.counterparty, t.category, t.memo, t.agent_id, t.external_id, "
        "t.raw_proof, t.policy_json, t.created_at AS tx_created_at, "
        "t.settled_at "
        "FROM approvals a JOIN transactions t ON a.tx_id = t.id "
        "WHERE a.status='pending' ORDER BY a.created_at ASC"
    ).fetchall()
    return _rows_to_dicts(rows)


def _cents_to_usd_str(cents: int) -> str:
    # Preserve sign explicitly for negative gains so CSV stays machine-parseable.
    sign = "-" if cents < 0 else ""
    absval = abs(cents)
    dollars, remainder = divmod(absval, 100)
    return f"{sign}{dollars}.{remainder:02d}"


_RAIL_ASSET_CODE: dict[str, str] = {"lightning": "BTC-LN", "base": "USDC-BASE"}
_RAIL_ASSET_NAME: dict[str, str] = {
    "lightning": "Bitcoin (Lightning)",
    "base": "USD Coin (Base)",
}


def export_1099_da(conn: sqlite3.Connection, year: int) -> str:
    start = int(datetime(year, 1, 1, tzinfo=UTC).timestamp())
    end = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp())

    rows = conn.execute(
        "SELECT d.tx_id AS tx_id, d.lot_id AS lot_id, d.units_consumed AS units_consumed, "
        "d.basis_cents AS basis_cents, d.proceeds_cents AS proceeds_cents, "
        "d.realized_gain_cents AS realized_gain_cents, "
        "d.holding_period_days AS holding_period_days, d.short_term AS short_term, "
        "d.created_at AS sold_at, "
        "t.rail AS rail, t.raw_proof AS raw_proof, "
        "l.acquired_at AS acquired_at, w.identifier AS wallet_identifier "
        "FROM disposition_events d "
        "JOIN transactions t ON d.tx_id = t.id "
        "LEFT JOIN lots l ON d.lot_id = l.id "
        "LEFT JOIN wallets w ON t.wallet_id = w.id "
        "WHERE d.created_at >= ? AND d.created_at < ? "
        "ORDER BY d.created_at ASC, d.id ASC",
        (start, end),
    ).fetchall()

    buf = io.StringIO()
    buf.write("# 1099-DA 2025 schema v1\n")
    buf.write("# Source form: IRS Form 1099-DA (Rev Jan 2025)\n")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "box_1a_asset_code",
            "box_1b_asset_name",
            "box_1c_units",
            "box_1d_acquired",
            "box_1e_disposed",
            "box_1f_proceeds",
            "box_1g_basis",
            "box_1h_accrued_mkt_disc",
            "box_1i_wash_sale_disallowed",
            "box_2_term",
            "box_3a_net_proceeds",
            "box_3b_qof",
            "box_4_backup_wh",
            "box_5_nondeductible",
            "box_6_treatment",
            "box_7_cash_only",
            "box_8_customer_data",
            "box_9_noncovered",
            "box_10_qof_sale",
            "box_11a_nft_count",
            "box_11b_nft_creator",
            "box_11c_nft_first_sale",
            "box_12_state",
            "box_13_txid",
            "box_14_wallet_address",
        ]
    )
    for r in rows:
        rail = r["rail"]
        acquired_at = r["acquired_at"]
        acquired_date = (
            datetime.fromtimestamp(int(acquired_at), tz=UTC).date().isoformat()
            if acquired_at is not None
            else ""
        )
        sold_date = datetime.fromtimestamp(int(r["sold_at"]), tz=UTC).date().isoformat()
        term = "short" if int(r["short_term"]) == 1 else "long"
        asset_code = _RAIL_ASSET_CODE.get(rail, "")
        asset_name = _RAIL_ASSET_NAME.get(rail, "")
        cash_only = "X" if rail in ("lightning", "base") else ""
        customer_data = "X" if rail == "manual" else ""
        noncovered = "X" if r["lot_id"] is None else ""
        wallet_identifier = r["wallet_identifier"] if r["wallet_identifier"] is not None else ""
        raw_proof = r["raw_proof"] if r["raw_proof"] is not None else ""
        writer.writerow(
            [
                asset_code,
                asset_name,
                r["units_consumed"],
                acquired_date,
                sold_date,
                _cents_to_usd_str(int(r["proceeds_cents"])),
                _cents_to_usd_str(int(r["basis_cents"])),
                "0",
                "0",
                term,
                "",
                "",
                "0",
                "",
                term,
                cash_only,
                customer_data,
                noncovered,
                "",
                "0",
                "",
                "",
                "",
                raw_proof,
                wallet_identifier,
            ]
        )
    return buf.getvalue()
