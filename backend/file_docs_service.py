"""FlowGate protected file-docs backend (separate service).

This is a second independently protected execution backend, mirroring
``tool_service.py``'s protocol and security model. It maps each
``resource_id`` to exactly one file ``<root>/<resource_id>.txt`` and only
accepts calls carrying the tool service key (``X-Tool-Key``) — a
credential held by the gateway, never by agent runs. Without the key
every execution/state/receipt call returns 401 ``tool-key-required``.

Reads/writes are the system of record for externalised file resources.
Writes use a two-phase receipt protocol: a ``pending`` receipt is
inserted and committed before the file is touched, the document is then
written via a temporary file in the same directory followed by
``os.replace`` (so a partially written document is never visible), and
finally the receipt is updated to ``committed`` and committed again. A
failure between phases leaves the durable ``pending`` receipt as the
recovery record; a retry with the same ``request_id`` either promotes it
when the file already carries the intended payload or completes the
write, converging to exactly one ``committed`` receipt.

Receipts are stored in SQLite at ``<root>/.flowgate_receipts.db`` with
Tier 4 ``request_id``/``payload_hash`` deduplication: a repeated
``/execute`` with the same ``request_id`` and the same binding
(resource, operation, run, payload hash) returns the original
``receipt_id`` with ``deduplicated: true`` without re-executing; a
mismatched binding returns 409 ``request-id-reuse``.

Run standalone:
    $env:FLOWGATE_FILE_DOCS_KEY = "pick-a-service-secret"
    $env:FLOWGATE_FILE_DOCS_ROOT = "$env:TEMP\\flowgate-file-docs"
    python -m uvicorn file_docs_service:app --port 8002   (from backend/)
"""

import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_RESOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-_]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecuteRequest(BaseModel):
    resource_id: str
    operation: str  # read | write
    content: Optional[str] = None
    run_id: Optional[str] = None
    request_id: Optional[str] = None
    payload_hash: Optional[str] = None


def create_file_docs_app(root_dir=None, service_key: Optional[str] = None) -> FastAPI:
    resolved_root = Path(
        root_dir
        or os.environ.get("FLOWGATE_FILE_DOCS_ROOT")
        or (Path(__file__).resolve().parent / "file_docs_data")
    )
    resolved_root.mkdir(parents=True, exist_ok=True)
    resolved_key = (
        service_key
        or os.environ.get("FLOWGATE_TOOL_SERVICE_KEY")
        or os.environ.get("FLOWGATE_FILE_DOCS_KEY")
        or secrets.token_urlsafe(32)
    )
    resolved_db = resolved_root / ".flowgate_receipts.db"
    conn = sqlite3.connect(str(resolved_db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS receipts(
          receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          run_id TEXT,
          operation TEXT,
          resource_id TEXT,
          request_id TEXT,
          payload_hash TEXT,
          state TEXT NOT NULL DEFAULT 'committed'
        );
        """
    )
    # Migrate existing DBs created before the two-phase state column.
    _receipt_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
    }
    if "state" not in _receipt_columns:
        conn.execute(
            "ALTER TABLE receipts ADD COLUMN state TEXT NOT NULL DEFAULT 'committed'"
        )
    conn.commit()
    lock = threading.Lock()

    application = FastAPI(title="FlowGate File Docs Backend")
    application.state.root_dir = str(resolved_root)
    application.state.root = str(resolved_root)
    application.state.db_path = str(resolved_db)
    application.state.service_key = resolved_key
    application.state.conn = conn
    state = application.state

    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"decision": "blocked", "policy_id": "tool-key-required"},
        )

    def _invalid_resource() -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"decision": "blocked", "policy_id": "invalid-resource-id"},
        )

    def _is_service(x_tool_key: Optional[str]) -> bool:
        return bool(x_tool_key) and secrets.compare_digest(
            str(x_tool_key), str(state.service_key)
        )

    def _is_valid_resource(resource_id: str) -> bool:
        return bool(_RESOURCE_ID_RE.match(resource_id or ""))

    def _file_for(resource_id: str) -> Path:
        return resolved_root / f"{resource_id}.txt"

    def _normalised_hash(content: str) -> str:
        normalised = re.sub(r"\s+", " ", (content or "").lower()).strip()
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    def _write_file_atomically(path: Path, text: str) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=str(resolved_root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _mark_committed(receipt_id) -> None:
        try:
            conn.execute(
                "UPDATE receipts SET state='committed' WHERE receipt_id=?",
                (receipt_id,),
            )
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    @application.get("/health")
    def health():
        return {"status": "ok", "service": "flowgate-file-docs"}

    @application.post("/execute")
    def execute(body: ExecuteRequest, x_tool_key: Optional[str] = Header(None, alias="X-Tool-Key")):
        if not _is_service(x_tool_key):
            return _unauthorized()
        if not _is_valid_resource(body.resource_id):
            return _invalid_resource()
        with lock:
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
                        if existing.get("state") == "pending" and body.operation == "write":
                            receipt_id = existing.get("receipt_id")
                            path = _file_for(body.resource_id)
                            landed = False
                            if body.payload_hash is not None and path.exists():
                                current = path.read_text(encoding="utf-8")
                                landed = _normalised_hash(current) == body.payload_hash
                            if not landed:
                                resolved_root.mkdir(parents=True, exist_ok=True)
                                _write_file_atomically(path, body.content or "")
                            _mark_committed(receipt_id)
                            return {
                                "receipt_id": receipt_id,
                                "result": None,
                                "deduplicated": True,
                            }
                        if body.operation == "read":
                            path = _file_for(body.resource_id)
                            result = path.read_text(encoding="utf-8") if path.exists() else ""
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
            resolved_root.mkdir(parents=True, exist_ok=True)
            path = _file_for(body.resource_id)
            if body.operation == "write":
                text = body.content or ""
                # Phase 1: durable pending receipt before touching the file.
                # If this insert/commit fails nothing was touched; raise.
                try:
                    cur = conn.execute(
                        "INSERT INTO receipts(ts, run_id, operation, resource_id, request_id, payload_hash, state)"
                        " VALUES (?,?,?,?,?,?,'pending')",
                        (
                            _now(),
                            body.run_id,
                            body.operation,
                            body.resource_id,
                            body.request_id,
                            body.payload_hash,
                        ),
                    )
                    receipt_id = cur.lastrowid
                    conn.commit()
                except BaseException:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                # Phase 2: temp-file + fsync + os.replace. On failure the
                # committed pending receipt remains as the recovery record.
                _write_file_atomically(path, text)
                # Phase 3: mark committed. On failure the file has changed
                # and the receipt stays pending (rolled back above in the
                # helper), which is recoverable on retry — never silent.
                _mark_committed(receipt_id)
                result = None
                return {"receipt_id": receipt_id, "result": result}
            else:
                result = path.read_text(encoding="utf-8") if path.exists() else ""
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
        if not _is_valid_resource(resource_id):
            return _invalid_resource()
        path = _file_for(resource_id)
        exists = path.exists()
        return {
            "resource_id": resource_id,
            "exists": exists,
            "content": path.read_text(encoding="utf-8") if exists else "",
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


app = create_file_docs_app()
