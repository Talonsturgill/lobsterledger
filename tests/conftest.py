from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lobster_ledger import db as _db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    # In-memory DB keeps policy and ledger unit tests fast and fully isolated.
    c = _db.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    # File-backed DB fixture for tests that need to exercise WAL mode or persistence.
    target = tmp_path / "ledger.db"
    c = _db.connect(target)
    try:
        yield c
    finally:
        c.close()
