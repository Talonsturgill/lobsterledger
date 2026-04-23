from __future__ import annotations

import sqlite3

from lobster_ledger import db
from lobster_ledger.types import (
    RULE_CONFIG_ADAPTER,
    AllowlistConfig,
    BlocklistConfig,
    CategoryBudgetConfig,
    PaymentRequest,
    PolicyResult,
    RequireApprovalAboveConfig,
    RuleConfig,
    RuleKind,
    SpendCapConfig,
    TriggeredRule,
    VelocityConfig,
)


def _load_enabled_rules(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, RuleKind, RuleConfig]]:
    cur = conn.execute(
        "SELECT id, name, kind, config_json FROM rules WHERE enabled = 1 ORDER BY id ASC"
    )
    rows = cur.fetchall()
    out: list[tuple[int, str, RuleKind, RuleConfig]] = []
    for row in rows:
        raw = db.loads(row["config_json"])
        # Rule configs are a discriminated union keyed on "kind"; injecting the
        # row's kind keeps us honest if the stored JSON ever drifted from the column.
        if isinstance(raw, dict):
            raw["kind"] = row["kind"]
        validated = RULE_CONFIG_ADAPTER.validate_python(raw)
        out.append((int(row["id"]), str(row["name"]), row["kind"], validated))
    return out


def _counterparty_contains(counterparty: str, entry: str) -> bool:
    return entry.lower() in counterparty.lower()


def _sum_outbound(
    conn: sqlite3.Connection,
    *,
    since_ts: int,
    rail: str | None,
    category: str | None,
) -> int:
    sql = (
        "SELECT COALESCE(SUM(amount_usd_fmv_cents), 0) AS total "
        "FROM transactions "
        "WHERE direction = 'out' "
        "AND status IN ('pending','settled') "
        "AND created_at >= ?"
    )
    params: list[object] = [since_ts]
    if rail is not None:
        sql += " AND rail = ?"
        params.append(rail)
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return int(row["total"]) if row is not None else 0


def evaluate(
    conn: sqlite3.Connection,
    request: PaymentRequest,
    now: int | None = None,
) -> PolicyResult:
    if request.direction == "in":
        return PolicyResult(outcome="allow", reasons=[], triggered_rules=[])

    current_ts = now if now is not None else db.now_ts()
    rules = _load_enabled_rules(conn)

    reasons: list[str] = []
    triggered: list[TriggeredRule] = []

    # Phase 1: blocklist. Any substring match short-circuits to deny.
    for rule_id, name, kind, config in rules:
        if not isinstance(config, BlocklistConfig):
            continue
        if request.counterparty is None:
            continue
        for entry in config.entries:
            if _counterparty_contains(request.counterparty, entry):
                reason = (
                    f"blocklist rule '{name}' matched counterparty "
                    f"'{request.counterparty}' on entry '{entry}'"
                )
                return PolicyResult(
                    outcome="deny",
                    reasons=[reason],
                    triggered_rules=[
                        TriggeredRule(rule_id=rule_id, name=name, kind=kind, reason=reason)
                    ],
                )

    # Phase 2: allowlist. OR across all allowlist rules; missing counterparty denies.
    allowlist_rules: list[tuple[int, str, RuleKind, AllowlistConfig]] = [
        (rid, rname, rkind, cfg)
        for (rid, rname, rkind, cfg) in rules
        if isinstance(cfg, AllowlistConfig)
    ]
    if allowlist_rules:
        if request.counterparty is None:
            reason = "allowlist rules active but counterparty is missing"
            triggered_rules = [
                TriggeredRule(rule_id=rid, name=rname, kind=rkind, reason=reason)
                for (rid, rname, rkind, _cfg) in allowlist_rules
            ]
            return PolicyResult(
                outcome="deny",
                reasons=[reason],
                triggered_rules=triggered_rules,
            )
        matched = False
        for _rid, _rname, _rkind, cfg in allowlist_rules:
            for entry in cfg.entries:
                if _counterparty_contains(request.counterparty, entry):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            reason = f"counterparty '{request.counterparty}' did not match any allowlist entry"
            triggered_rules = [
                TriggeredRule(rule_id=rid, name=rname, kind=rkind, reason=reason)
                for (rid, rname, rkind, _cfg) in allowlist_rules
            ]
            return PolicyResult(
                outcome="deny",
                reasons=[reason],
                triggered_rules=triggered_rules,
            )

    # Phase 3: spend cap. Per-transaction ceiling.
    for rule_id, name, kind, config in rules:
        if not isinstance(config, SpendCapConfig):
            continue
        if config.rail is not None and config.rail != request.rail:
            continue
        if request.amount_usd_fmv_cents > config.max_amount_usd_cents:
            reason = (
                f"spend cap rule '{name}' exceeded: "
                f"{request.amount_usd_fmv_cents} > {config.max_amount_usd_cents}"
            )
            return PolicyResult(
                outcome="deny",
                reasons=[reason],
                triggered_rules=[
                    TriggeredRule(rule_id=rule_id, name=name, kind=kind, reason=reason)
                ],
            )

    # Phase 4: velocity. Rolling window sum plus current proposal.
    for rule_id, name, kind, config in rules:
        if not isinstance(config, VelocityConfig):
            continue
        since_ts = current_ts - config.window_seconds
        prior_sum = _sum_outbound(
            conn,
            since_ts=since_ts,
            rail=config.rail,
            category=None,
        )
        if prior_sum + request.amount_usd_fmv_cents > config.max_total_usd_cents:
            reason = (
                f"velocity rule '{name}' exceeded: "
                f"{prior_sum} + {request.amount_usd_fmv_cents} > "
                f"{config.max_total_usd_cents} in window of {config.window_seconds}s"
            )
            return PolicyResult(
                outcome="deny",
                reasons=[reason],
                triggered_rules=[
                    TriggeredRule(rule_id=rule_id, name=name, kind=kind, reason=reason)
                ],
            )

    # Phase 5: category budget. Same windowing as velocity, scoped by category.
    for rule_id, name, kind, config in rules:
        if not isinstance(config, CategoryBudgetConfig):
            continue
        if request.category != config.category:
            continue
        since_ts = current_ts - config.window_seconds
        prior_sum = _sum_outbound(
            conn,
            since_ts=since_ts,
            rail=None,
            category=config.category,
        )
        if prior_sum + request.amount_usd_fmv_cents > config.max_total_usd_cents:
            reason = (
                f"category budget rule '{name}' exceeded for category "
                f"'{config.category}': {prior_sum} + {request.amount_usd_fmv_cents} "
                f"> {config.max_total_usd_cents} in window of {config.window_seconds}s"
            )
            return PolicyResult(
                outcome="deny",
                reasons=[reason],
                triggered_rules=[
                    TriggeredRule(rule_id=rule_id, name=name, kind=kind, reason=reason)
                ],
            )

    # Phase 6: require approval above. Escalate but keep scanning; no later phase denies.
    outcome: str = "allow"
    for rule_id, name, kind, config in rules:
        if not isinstance(config, RequireApprovalAboveConfig):
            continue
        if config.rail is not None and config.rail != request.rail:
            continue
        if request.amount_usd_fmv_cents > config.threshold_usd_cents:
            reason = (
                f"require approval rule '{name}' breached: "
                f"{request.amount_usd_fmv_cents} > {config.threshold_usd_cents}"
            )
            reasons.append(reason)
            triggered.append(TriggeredRule(rule_id=rule_id, name=name, kind=kind, reason=reason))
            outcome = "require_approval"

    if outcome == "require_approval":
        return PolicyResult(
            outcome="require_approval",
            reasons=reasons,
            triggered_rules=triggered,
        )
    return PolicyResult(outcome="allow", reasons=[], triggered_rules=[])
