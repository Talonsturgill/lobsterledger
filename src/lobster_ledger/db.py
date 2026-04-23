from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".lobster-ledger" / "ledger.db"


# CREATE IF NOT EXISTS everywhere so connect() is safe to call on every open.
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS wallets (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  rail TEXT NOT NULL CHECK (rail IN ('lightning','base','manual')),
  identifier TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('spend_cap','velocity','allowlist','blocklist','category_budget','require_approval_above')),
  config_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_kind_enabled ON rules(kind, enabled);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  rail TEXT NOT NULL CHECK (rail IN ('lightning','base','manual')),
  status TEXT NOT NULL CHECK (status IN ('pending','settled','denied','failed')),
  wallet_id INTEGER REFERENCES wallets(id),
  amount_sats INTEGER,
  amount_usdc_micro INTEGER,
  amount_usd_fmv_cents INTEGER NOT NULL,
  counterparty TEXT,
  category TEXT,
  memo TEXT,
  agent_id TEXT,
  external_id TEXT,
  raw_proof TEXT,
  policy_json TEXT,
  created_at INTEGER NOT NULL,
  settled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tx_created_at ON transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_rail ON transactions(rail);
CREATE INDEX IF NOT EXISTS idx_tx_agent_id ON transactions(agent_id);

CREATE TABLE IF NOT EXISTS lots (
  id INTEGER PRIMARY KEY,
  wallet_id INTEGER NOT NULL REFERENCES wallets(id),
  source_tx_id INTEGER NOT NULL REFERENCES transactions(id),
  rail TEXT NOT NULL CHECK (rail IN ('lightning','base','manual')),
  acquired_at INTEGER NOT NULL,
  original_units INTEGER NOT NULL CHECK (original_units > 0),
  remaining_units INTEGER NOT NULL CHECK (remaining_units >= 0),
  basis_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_fifo ON lots(wallet_id, acquired_at);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  tx_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
  status TEXT NOT NULL CHECK (status IN ('pending','approved','denied')),
  reason TEXT,
  created_at INTEGER NOT NULL,
  resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS disposition_events (
  id INTEGER PRIMARY KEY,
  tx_id INTEGER NOT NULL REFERENCES transactions(id),
  lot_id INTEGER REFERENCES lots(id),
  units_consumed INTEGER NOT NULL,
  basis_cents INTEGER NOT NULL,
  proceeds_cents INTEGER NOT NULL,
  realized_gain_cents INTEGER NOT NULL,
  holding_period_days INTEGER NOT NULL,
  short_term INTEGER NOT NULL CHECK (short_term IN (0,1)),
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disposition_events_tx_id ON disposition_events(tx_id);
"""


def resolve_db_path() -> Path:
    override = os.environ.get("LOBSTER_LEDGER_DB")
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


def now_ts() -> int:
    return int(time.time())


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def loads(s: str) -> Any:
    return json.loads(s)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    if path is None:
        target: Path | str = resolve_db_path()
    else:
        target = path

    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target))
    else:
        # String path covers special SQLite targets like ":memory:" which
        # must not go through Path parent-dir creation.
        if target not in (":memory:", "") and not target.startswith("file:"):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
