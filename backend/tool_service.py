"""FlowGate protected tool backend (separate service).

This is the independently protected execution backend for externalised
resources (by default ``public-outbox``). It owns its own SQLite store
and execution receipts, and only accepts calls carrying the tool
service key (``X-Tool-Key``) — a credential held by the gateway, never
by agent runs. Agents cannot bypass the gateway to reach it: without
the service key every execution/state/receipt call returns 401.

Run standalone:
    $env:FLOWGATE_TOOL_SERVICE_KEY = "pick-a-service-secret"
    $env:FLOWGATE_TOOL_DB = "$env:TEMP\flowgate-tools.db"
    python -m uvicorn tool_service:app --port 8001   (from backend/)
"""

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecuteRequest(BaseModel):
    resource_id: str
    operation: str  # read | write
    content: Optional[str] = None
    run_id: Optional[str] = None
    request_id: Optional[str] = None
    payload_hash: Optional[str] = None


def create_tool_app(db_path: Optional[str] = None, service_key: Optional[str] = None) -> FastAPI:
    resolved_db = str(
        db_path
        or os.environ.get("FLOWGATE_TOOL_DB")
        or (Path(__file__).resolve().parent / "tool_backend.db")
    )
    resolved_key = (
        service_key
        or os.environ.get("FLOWGATE_TOOL_SERVICE_KEY")
        or secrets.token_urlsafe(32)
    )
    conn = sqlite3.connect(resolved_db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kv(
          resource_id TEXT PRIMARY KEY,
          content TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS receipts(
          receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          run_id TEXT,
          operation TEXT,
          resource_id TEXT,
          request_id TEXT,
          payload_hash TEXT
        );
        """
    )
    # Migrate existing DBs created before Tier 4 idempotency columns.
    _receipt_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
    }
    if "request_id" not in _receipt_columns:
        conn.execute("ALTER TABLE receipts ADD COLUMN request_id TEXT")
    if "payload_hash" not in _receipt_columns:
        conn.execute("ALTER TABLE receipts ADD COLUMN payload_hash TEXT")
    conn.commit()

    application = FastAPI(title="FlowGate Tool Backend")
    application.state.db_path = resolved_db
    application.state.service_key = resolved_key
    application.state.conn = conn
    state = application.state

    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"decision": "blocked", "policy_id": "tool-key-required"},
        )

    def _is_service(x_tool_key: Optional[str]) -> bool:
        return bool(x_tool_key) and secrets.compare_digest(
            str(x_tool_key), str(state.service_key)
        )

    @application.get("/health")
    def health():
        return {"status": "ok", "service": "flowgate-tool-backend"}

    @application.post("/execute")
    def execute(body: ExecuteRequest, x_tool_key: Optional[str] = Header(None, alias="X-Tool-Key")):
        if not _is_service(x_tool_key):
            return _unauthorized()
        if body.request_id:
            existing_row = conn.execute(
                "SELECT * FROM receipts WHERE request_id = ? ORDER BY receipt_id LIMIT 1",
                (body.request_id,),
            ).fetchone()
            if existing_row is not None:
                existing = dict(existing_row)
                if (
                    existing.get("resource_id") == body.resource_id
                    and existing.get("operation") == body.operation
                    and existing.get("run_id") == body.run_id
                    and existing.get("payload_hash") == body.payload_hash
                ):
                    if body.operation == "read":
                        row = conn.execute(
                            "SELECT content FROM kv WHERE resource_id = ?",
                            (body.resource_id,),
                        ).fetchone()
                        result = row["content"] if row else ""
                    else:
                        result = None
                    return {
                        "receipt_id": existing.get("receipt_id"),
                        "result": result,
                        "deduplicated": True,
                    }
                return JSONResponse(
                    status_code=409,
                    content={"decision": "blocked", "policy_id": "request-id-reuse"},
                )
        if body.operation not in ("read", "write"):
            return JSONResponse(
                status_code=400,
                content={"decision": "blocked", "policy_id": "unknown-operation"},
            )
        if body.operation == "write":
            conn.execute(
                "INSERT INTO kv(resource_id, content) VALUES (?,?)"
                " ON CONFLICT(resource_id) DO UPDATE SET content=excluded.content",
                (body.resource_id, body.content or ""),
            )
            result = None
        else:
            row = conn.execute(
                "SELECT content FROM kv WHERE resource_id = ?", (body.resource_id,)
            ).fetchone()
            result = row["content"] if row else ""
        cur = conn.execute(
            "INSERT INTO receipts(ts, run_id, operation, resource_id, request_id, payload_hash)"
            " VALUES (?,?,?,?,?,?)",
            (
                _now(),
                body.run_id,
                body.operation,
                body.resource_id,
                body.request_id,
                body.payload_hash,
            ),
        )
        conn.commit()
        return {"receipt_id": cur.lastrowid, "result": result}

    @application.get("/state/{resource_id}")
    def get_state(resource_id: str, x_tool_key: Optional[str] = Header(None, alias="X-Tool-Key")):
        if not _is_service(x_tool_key):
            return _unauthorized()
        row = conn.execute(
            "SELECT content FROM kv WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        return {
            "resource_id": resource_id,
            "exists": row is not None,
            "content": row["content"] if row else "",
        }

    @application.get("/receipts")
    def get_receipts(
        run_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        request_id: Optional[str] = None,
        x_tool_key: Optional[str] = Header(None, alias="X-Tool-Key"),
    ):
        if not _is_service(x_tool_key):
            return _unauthorized()
        query = "SELECT * FROM receipts"
        clauses, params = [], []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if resource_id is not None:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if request_id is not None:
            clauses.append("request_id = ?")
            params.append(request_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY receipt_id"
        receipts = [dict(r) for r in conn.execute(query, params).fetchall()]
        return {"count": len(receipts), "receipts": receipts}

    return application


app = create_tool_app()
