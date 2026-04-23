from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lobster_ledger.dashboard.deps import get_conn

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Lobster Ledger Dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row["c"])


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: ConnDep) -> Response:
    # Aggregate stats populate the landing card grid.
    stats: dict[str, int] = {
        "active_wallets": _count(conn, "SELECT COUNT(*) AS c FROM wallets WHERE active=1"),
        "pending_approvals": _count(
            conn, "SELECT COUNT(*) AS c FROM approvals WHERE status='pending'"
        ),
        "total_transactions": _count(conn, "SELECT COUNT(*) AS c FROM transactions"),
        "settled_transactions": _count(
            conn, "SELECT COUNT(*) AS c FROM transactions WHERE status='settled'"
        ),
        "denied_transactions": _count(
            conn, "SELECT COUNT(*) AS c FROM transactions WHERE status='denied'"
        ),
        "enabled_rules": _count(conn, "SELECT COUNT(*) AS c FROM rules WHERE enabled=1"),
    }
    ctx: dict[str, Any] = {"request": request, "stats": stats}
    return TEMPLATES.TemplateResponse(request, "index.html", ctx)


@app.get("/health", response_class=HTMLResponse)
def health() -> HTMLResponse:
    return HTMLResponse("<p>ok</p>")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("LL_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("LL_DASHBOARD_PORT", "8765"))
    uvicorn.run("lobster_ledger.dashboard.app:app", host=host, port=port, reload=False)
