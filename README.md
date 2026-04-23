# Lobster Ledger

Lobster Ledger is a Python MCP server that governs the payments AI agents propose. It sits in front of Lightning and USDC-on-Base rails, applies a policy gate (blocklist, allowlist, spend caps, velocity, category budgets, approval thresholds) before any money moves, and records every outcome in an append-only SQLite ledger with per-wallet FIFO lot tracking for IRS Rev Proc 2024-28 (1099-DA) compliance. It ships stub Lightning and Base adapters so the MVP runs end to end on a laptop without network access, and plugs into Claude Code or any other MCP client over stdio.

## Install

```bash
git clone https://github.com/arctic-intelligence/lobster-ledger.git
cd lobster-ledger
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

The ledger file lives at `~/.lobster-ledger/ledger.db` by default. Override with the `LOBSTER_LEDGER_DB` environment variable.

```bash
export LOBSTER_LEDGER_DB=/path/to/ledger.db
```

## Install in Claude Code

Add the `lobster-ledger` entry from `examples/claude_code_mcp_config.json` into your Claude Code MCP settings and restart. The `lobster-ledger` entry point is installed by pip as a console script.

## Demo walkthrough

Run the server against the MCP inspector to drive the tools interactively.

```bash
mcp dev src/lobster_ledger/server.py
```

1. Call `register_wallet` with label `hot-ln`, rail `lightning`, and an identifier for your node.
2. Call `set_rule` to install a spend cap, for example kind `spend_cap` with `{"max_amount_usd_cents": 5000, "rail": "lightning"}`.
3. Call `set_rule` again with kind `require_approval_above` and `{"threshold_usd_cents": 2000}`.
4. Call `record_manual_transaction` with direction `in`, rail `lightning`, `amount_sats` 500000, and `amount_usd_fmv_cents` 32500 to seed a cost basis lot.
5. Call `propose_payment` with rail `lightning`, `amount_usd_cents` 1500, `amount_sats` 24000, and a counterparty. The policy allows it and the stub adapter settles immediately.
6. Call `propose_payment` again with `amount_usd_cents` 3000. The policy returns `require_approval` and the transaction lands in `list_pending_approvals`.
7. Call `resolve_approval` with the returned `tx_id` and `approved=True` to settle it through the stub adapter.
8. Call `export_1099_da` with the current year and inspect the returned CSV. The first line is `# 1099-DA draft v1` followed by one row per disposition event.

## Architecture

`types.py` defines the Pydantic models and literal enums. `RuleConfig` is a discriminated union keyed on `kind` so persisted rules round-trip through `TypeAdapter(RuleConfig).validate_python`.

`db.py` owns the SQLite connection and schema. Every `connect()` opens with WAL journaling, NORMAL synchronous, foreign keys on, and a 5-second busy timeout, then runs `CREATE IF NOT EXISTS` for all six tables: `wallets`, `rules`, `transactions`, `lots`, `approvals`, `disposition_events`. Timestamps are integer epoch seconds.

`policy.py` exposes `evaluate(conn, request)`. The phases run in order: blocklist, allowlist, spend cap, velocity, category budget, require-approval-above. Deny phases short-circuit; the approval phase escalates without denying. Inbound transactions skip the gate entirely.

`ledger.py` owns writes. `record_transaction` persists a row, stamps the policy JSON, and either opens a lot (inbound settled) or consumes lots FIFO (outbound settled). `mark_settled` is idempotent and drives the same lot paths for the approval-driven flow. `export_1099_da` renders disposition events as CSV.

`payments.py` defines a `PaymentAdapter` protocol and three stubs (`StubLightningAdapter`, `StubBaseAdapter`, `StubManualAdapter`). `get_adapter(rail)` dispatches. No network calls anywhere.

`server.py` wires the FastMCP instance and registers 14 tools. Each MCP-decorated tool is a thin wrapper around a private `_do_*` handler that takes a connection directly, which keeps the handlers unit-testable without an MCP client.

Transactions move through a four-state machine: `pending`, `settled`, `denied`, `failed`. `pending` covers both "waiting for settlement" and "waiting for operator approval" (the approvals row carries the approval status). Rows are never deleted or rewritten beyond the status, settled_at, raw_proof, memo, and policy_json fields. The append-only invariant is what makes reproducible audits and the 1099-DA export possible.

## Rev Proc 2024-28 compliance

IRS Rev Proc 2024-28 became effective January 1, 2025 and requires per-wallet FIFO lot tracking for digital asset dispositions. Lobster Ledger tracks lots per wallet on acquisition (inbound) and consumes them FIFO on disposition (outbound settlement), emitting a `disposition_events` row per consumption with basis, proceeds, realized gain, holding period, and a short or long term flag. The `export_1099_da(year)` tool produces a CSV that maps to the 1099-DA form data model. The first line is stamped `# 1099-DA draft v1` because the IRS final form layout may still evolve; downstream consumers can pin to that stamp and upgrade deliberately.

## Testing

```bash
pytest -v
mypy --strict src/lobster_ledger
ruff check .
ruff format --check .
```

## License

MIT. Copyright Arctic Intelligence.
