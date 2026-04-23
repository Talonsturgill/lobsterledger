from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from lobster_ledger import db


def get_conn() -> Iterator[sqlite3.Connection]:
    # Short-lived per-request connections rely on WAL mode to coexist with the MCP server process.
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()
