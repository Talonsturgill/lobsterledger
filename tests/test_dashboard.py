from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lobster_ledger import db as _db
from lobster_ledger.dashboard.app import app
from lobster_ledger.dashboard.deps import get_conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # File-backed DB so each request (threadpool) can open its own connection.
    target = tmp_path / "ledger.db"
    conn = _db.connect(target)
    conn.close()
    return target


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    def _override() -> Iterator[sqlite3.Connection]:
        c = _db.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    app.dependency_overrides[get_conn] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def test_index_renders_stats_with_empty_db(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Active wallets" in body
    assert "Pending approvals" in body
    assert '<p class="big">0</p>' in body


def test_index_reflects_wallet_count(client: TestClient, db_path: Path) -> None:
    conn = _db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO wallets(label, rail, identifier, active, created_at) "
            "VALUES('w1', 'lightning', 'alice@ln', 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # The active-wallets card should now report one.
    assert "Active wallets" in body
    assert '<p class="big">1</p>' in body


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "ok" in resp.text


def test_static_css_served(client: TestClient) -> None:
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "css" in ctype


def test_nav_links_present(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    for href in ("/approvals", "/transactions", "/wallets", "/rules"):
        assert f'href="{href}"' in body


def test_index_reflects_pending_approvals_count(client: TestClient, db_path: Path) -> None:
    conn = _db.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO transactions(direction, rail, status, amount_usd_fmv_cents, created_at) "
            "VALUES('out', 'lightning', 'pending', 500, 1)"
        )
        tx_id = cur.lastrowid
        assert tx_id is not None
        conn.execute(
            "INSERT INTO approvals(tx_id, status, reason, created_at) "
            "VALUES(?, 'pending', NULL, 1)",
            (tx_id,),
        )
        conn.commit()
    finally:
        conn.close()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Verify the pending-approvals card reports one by locating the heading and the value.
    marker = '<h2>Pending approvals</h2><p class="big">1</p>'
    assert marker in body
