"""SQLite store for FlowGate (backend/app/store.py)."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_compartments(row) -> list:
    try:
        raw = row["effective_compartments"]
    except (KeyError, IndexError):
        return []
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        values = parsed if isinstance(parsed, list) else []
    else:
        return []
    return sorted({str(v) for v in values if v is not None and str(v) != ""})


def _run_dict(row) -> dict:
    try:
        effective_level = row["effective_level"]
    except (KeyError, IndexError):
        effective_level = 0
    try:
        effective_level = int(effective_level or 0)
    except (TypeError, ValueError):
        effective_level = 0
    try:
        tenant = row["tenant"]
    except (KeyError, IndexError):
        tenant = None
    return {
        "run_id": row["run_id"],
        "project": row["project"],
        "tenant": tenant or row["project"],
        "clearance": row["clearance"],
        "suspended": bool(row["suspended"]),
        "created_at": row["created_at"],
        "effective_level": effective_level,
        "effective_compartments": _parse_compartments(row),
    }


def _parse_stored_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        values = parsed if isinstance(parsed, list) else []
    else:
        return []
    return sorted({str(v) for v in values if v is not None and str(v) != ""})


def _resource_dict(row) -> dict:
    try:
        compartments = _parse_stored_list(row["compartments"])
    except (KeyError, IndexError):
        compartments = []
    try:
        created_by = row["created_by"]
    except (KeyError, IndexError):
        created_by = None
    try:
        version = int(row["version"] or 1)
    except (KeyError, IndexError, TypeError, ValueError):
        version = 1
    try:
        tenant = row["tenant"]
    except (KeyError, IndexError):
        tenant = None
    return {
        "resource_id": row["resource_id"],
        "kind": row["kind"],
        "project": row["project"],
        "tenant": tenant or row["project"],
        "sensitivity": row["sensitivity"],
        "content": row["content"],
        "compartments": compartments,
        "created_by": created_by,
        "version": version,
    }


def init_db(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs(
          run_id TEXT PRIMARY KEY,
          project TEXT NOT NULL,
          tenant TEXT NOT NULL DEFAULT '',
          clearance INTEGER NOT NULL,
          suspended INTEGER NOT NULL DEFAULT 0,
          token TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL,
          effective_level INTEGER NOT NULL DEFAULT 0,
          effective_compartments TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS resources(
          resource_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          project TEXT NOT NULL,
          tenant TEXT NOT NULL DEFAULT '',
          sensitivity INTEGER NOT NULL,
          content TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          compartments TEXT NOT NULL DEFAULT '[]',
          created_by TEXT,
          version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS artifact_versions(
          resource_id TEXT NOT NULL,
          version INTEGER NOT NULL,
          content TEXT NOT NULL,
          created_by TEXT,
          sensitivity INTEGER NOT NULL,
          compartments TEXT NOT NULL DEFAULT '[]',
          content_hash TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY(resource_id, version)
        );
        CREATE TABLE IF NOT EXISTS execution_receipts(
          receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          run_id TEXT,
          tool TEXT,
          operation TEXT,
          resource_id TEXT,
          version INTEGER
        );
        CREATE TABLE IF NOT EXISTS events(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          run_id TEXT,
          tool TEXT,
          operation TEXT,
          resource_id TEXT,
          decision TEXT,
          action TEXT,
          policy_id TEXT,
          reason TEXT,
          similarity REAL,
          content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS fingerprints(
          fingerprint_id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_resource_id TEXT,
          source_project TEXT,
          source_sensitivity INTEGER,
          first_read_by TEXT,
          hash TEXT,
          canaries TEXT,
          shingles TEXT,
          created_at TEXT
        );
        """
    )
    conn.commit()
    # Migrate pre-Tier-1 databases whose runs table lacks the
    # effective-label columns.
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "effective_level" not in existing:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN effective_level INTEGER NOT NULL DEFAULT 0"
        )
    if "effective_compartments" not in existing:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN effective_compartments TEXT NOT NULL DEFAULT '[]'"
        )
    if "tenant" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN tenant TEXT NOT NULL DEFAULT ''")
    # Existing runs belong to the tenant named by their project.
    conn.execute("UPDATE runs SET tenant = project WHERE tenant = ''")
    # Migrate pre-Tier-2 databases whose resources table lacks the
    # artifact columns.
    resource_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(resources)").fetchall()
    }
    if "compartments" not in resource_cols:
        conn.execute(
            "ALTER TABLE resources ADD COLUMN compartments TEXT NOT NULL DEFAULT '[]'"
        )
    if "created_by" not in resource_cols:
        conn.execute("ALTER TABLE resources ADD COLUMN created_by TEXT")
    if "version" not in resource_cols:
        conn.execute(
            "ALTER TABLE resources ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    if "tenant" not in resource_cols:
        conn.execute("ALTER TABLE resources ADD COLUMN tenant TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE resources SET tenant = project WHERE tenant = ''")
    conn.commit()
    return conn


def _normalise_compartments(compartments) -> list:
    if compartments is None:
        return []
    if isinstance(compartments, str):
        text = compartments.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = [compartments]
        compartments = parsed if isinstance(parsed, list) else [parsed]
    try:
        values = list(compartments)
    except TypeError:
        return []
    return sorted({str(v) for v in values if v is not None and str(v) != ""})


def create_run(
    conn, project, clearance, token, effective_level=0, effective_compartments=None,
    tenant=None,
) -> dict:
    run_id = uuid.uuid4().hex[:16]
    created_at = _now()
    tenant = tenant or project
    try:
        level = int(effective_level or 0)
    except (TypeError, ValueError):
        level = 0
    compartments = _normalise_compartments(effective_compartments)
    conn.execute(
        "INSERT INTO runs(run_id, project, tenant, clearance, suspended, token, created_at,"
        " effective_level, effective_compartments)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, project, tenant, clearance, 0, token, created_at, level, json.dumps(compartments)),
    )
    conn.commit()
    created = get_run(conn, run_id) or {
        "run_id": run_id,
        "project": project,
        "tenant": tenant,
        "clearance": clearance,
        "suspended": False,
        "created_at": created_at,
        "effective_level": level,
        "effective_compartments": compartments,
    }
    created["token"] = token
    return created


def update_run_effective_label(conn, run_id, classification, compartments=None, commit=True) -> Optional[dict]:
    """Join ``(classification, compartments)`` into the run effective label.

    The join is max classification / union of compartments. Also accepts a
    label dict (``{"classification": ..., "compartments": [...]}``) as
    ``classification``. Returns the updated run dict, or None if unknown.
    """
    if isinstance(classification, dict):
        label = classification
        if compartments is None:
            compartments = label.get(
                "compartments", label.get("effective_compartments", [])
            )
        classification = label.get(
            "classification",
            label.get("effective_level", label.get("sensitivity", 0)),
        )
    current = get_run(conn, run_id)
    if current is None:
        return None
    try:
        incoming_level = int(classification or 0)
    except (TypeError, ValueError):
        incoming_level = 0
    new_level = max(int(current.get("effective_level", 0) or 0), incoming_level)
    new_compartments = sorted(
        set(_normalise_compartments(current.get("effective_compartments")))
        | set(_normalise_compartments(compartments))
    )
    conn.execute(
        "UPDATE runs SET effective_level = ?, effective_compartments = ? WHERE run_id = ?",
        (new_level, json.dumps(new_compartments), run_id),
    )
    if commit:
        conn.commit()
    return get_run(conn, run_id)


def get_run_by_token(conn, token) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    return _run_dict(row)


def get_run(conn, run_id) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return _run_dict(row)


def list_runs(conn) -> list:
    rows = conn.execute("SELECT * FROM runs ORDER BY rowid").fetchall()
    return [_run_dict(r) for r in rows]


def set_suspended(conn, run_id, suspended: bool, commit=True) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE runs SET suspended = ? WHERE run_id = ?",
        (1 if suspended else 0, run_id),
    )
    if commit:
        conn.commit()
    return get_run(conn, run_id)


def upsert_resource(conn, resource_id, kind, project, sensitivity, content, tenant=None) -> dict:
    now = _now()
    tenant = tenant or project
    conn.execute(
        "INSERT INTO resources(resource_id, kind, project, tenant, sensitivity, content, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(resource_id) DO UPDATE SET"
        " kind=excluded.kind, project=excluded.project, tenant=excluded.tenant,"
        " sensitivity=excluded.sensitivity, content=excluded.content,"
        " updated_at=excluded.updated_at",
        (resource_id, kind, project, tenant, sensitivity, content, now),
    )
    conn.commit()
    return get_resource(conn, resource_id)


def get_resource(conn, resource_id) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    if row is None:
        return None
    return _resource_dict(row)


def list_resources(conn) -> list:
    rows = conn.execute("SELECT * FROM resources ORDER BY rowid").fetchall()
    return [_resource_dict(r) for r in rows]


def set_resource_content(conn, resource_id, content, commit=True) -> None:
    conn.execute(
        "UPDATE resources SET content = ?, updated_at = ? WHERE resource_id = ?",
        (content, _now(), resource_id),
    )
    if commit:
        conn.commit()


def insert_event(
    conn,
    run_id,
    tool,
    operation,
    resource_id,
    decision,
    action,
    policy_id,
    reason,
    similarity=None,
    content_hash=None,
    commit=True,
) -> int:
    cur = conn.execute(
        "INSERT INTO events(ts, run_id, tool, operation, resource_id, decision,"
        " action, policy_id, reason, similarity, content_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _now(),
            run_id,
            tool,
            operation,
            resource_id,
            decision,
            action,
            policy_id,
            reason,
            similarity,
            content_hash,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_events(conn, run_id=None, decision=None, limit=200) -> list:
    query = "SELECT * FROM events"
    clauses = []
    params = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if decision is not None:
        clauses.append("decision = ?")
        params.append(decision)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY event_id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def insert_fingerprint(
    conn,
    source_resource_id,
    source_project,
    source_sensitivity,
    first_read_by,
    fingerprint: dict,
    commit=True,
) -> int:
    shingles = fingerprint.get("shingles", [])
    if isinstance(shingles, set):
        shingles = sorted(shingles)
    else:
        shingles = list(shingles)
    cur = conn.execute(
        "INSERT INTO fingerprints(source_resource_id, source_project,"
        " source_sensitivity, first_read_by, hash, canaries, shingles, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            source_resource_id,
            source_project,
            source_sensitivity,
            first_read_by,
            fingerprint.get("hash"),
            json.dumps(list(fingerprint.get("canaries", []))),
            json.dumps(shingles),
            _now(),
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_fingerprints(conn) -> list:
    rows = conn.execute(
        "SELECT * FROM fingerprints ORDER BY fingerprint_id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        fid = d.pop("fingerprint_id")
        d["id"] = fid
        d["fingerprint_id"] = fid
        d["canaries"] = json.loads(d["canaries"]) if d["canaries"] else []
        d["shingles"] = json.loads(d["shingles"]) if d["shingles"] else []
        out.append(d)
    return out


def create_artifact(
    conn, resource_id, kind, project, sensitivity, compartments,
    content, created_by, content_hash=None, commit=True, tenant=None,
) -> dict:
    """Create a versioned artifact at version 1 (resource + version row)."""
    now = _now()
    tenant = tenant or project
    compartments = _normalise_compartments(compartments)
    conn.execute(
        "INSERT INTO resources(resource_id, kind, project, tenant, sensitivity, content,"
        " updated_at, compartments, created_by, version) VALUES (?,?,?,?,?,?,?,?,?,1)",
        (resource_id, kind, project, tenant, sensitivity, content, now,
         json.dumps(compartments), created_by),
    )
    conn.execute(
        "INSERT INTO artifact_versions(resource_id, version, content, created_by,"
        " sensitivity, compartments, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (resource_id, 1, content, created_by, sensitivity,
         json.dumps(compartments), content_hash, now),
    )
    if commit:
        conn.commit()
    return get_resource(conn, resource_id)


def append_artifact_version(
    conn, resource_id, content, created_by, content_hash=None, commit=True
) -> Optional[int]:
    """Append a version for an artifact write; None if not an artifact."""
    resource = get_resource(conn, resource_id)
    if resource is None or resource.get("created_by") is None:
        return None
    new_version = int(resource.get("version") or 1) + 1
    conn.execute(
        "UPDATE resources SET version = ? WHERE resource_id = ?",
        (new_version, resource_id),
    )
    conn.execute(
        "INSERT INTO artifact_versions(resource_id, version, content, created_by,"
        " sensitivity, compartments, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (resource_id, new_version, content, created_by,
         resource["sensitivity"], json.dumps(resource.get("compartments") or []),
         content_hash, _now()),
    )
    if commit:
        conn.commit()
    return new_version


def list_artifact_versions(conn, resource_id) -> list:
    rows = conn.execute(
        "SELECT * FROM artifact_versions WHERE resource_id = ? ORDER BY version",
        (resource_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["compartments"] = _parse_stored_list(d.get("compartments"))
        out.append(d)
    return out


def insert_execution_receipt(
    conn, run_id, tool, operation, resource_id, version=None, commit=True
) -> int:
    cur = conn.execute(
        "INSERT INTO execution_receipts(ts, run_id, tool, operation, resource_id, version)"
        " VALUES (?,?,?,?,?,?)",
        (_now(), run_id, tool, operation, resource_id, version),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_execution_receipts(conn, run_id=None, resource_id=None) -> list:
    query = "SELECT * FROM execution_receipts"
    clauses, params = [], []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if resource_id is not None:
        clauses.append("resource_id = ?")
        params.append(resource_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY receipt_id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def reset_all(conn) -> None:
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM fingerprints")
    conn.execute("DELETE FROM artifact_versions")
    conn.execute("DELETE FROM execution_receipts")
    conn.execute("DELETE FROM resources WHERE created_by IS NOT NULL")
    conn.commit()
