"""Tier 5 clean-context spawning tests (CONTRACT Tier 5 Amendment).

Server-side spawn semantics under test:

- ``continuing`` inherits the parent's *current* effective label;
  client-supplied effective values are overridden by the server, so a
  fresh token cannot launder provenance.
- ``clean`` starts at ``(0, [])`` regardless of client-supplied values;
  a clean context is not a skeleton key for the parent's restricted
  artifacts.
- Unknown parent -> 422; ``spawn_mode`` without a parent -> 422.
- The lineage graph carries a ``spawn`` edge (parent run -> child run).
- Non-spawn creation is unchanged.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from seed import ALPHA_DRAFT_CONTENT  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"
ADMIN_KEY = "flowgate-dev-admin"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}

PUBLIC_NOTICE = "Project Alpha customer release notes are now available on request."


def _make_client(tmp_path) -> TestClient:
    app = create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        demo_mode=True,
    )
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_run(client: TestClient, body: dict, expect: int = 201):
    resp = client.post("/runs", json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == expect, resp.text
    return resp


def _run_ok(client: TestClient, body: dict) -> dict:
    run = _create_run(client, body).json()
    assert run.get("token"), run
    return run


def _tool(client, token, tool_name, operation, resource_id, content=None):
    body = {"operation": operation, "resource_id": resource_id}
    if content is not None:
        body["content"] = content
    return client.post(f"/tools/{tool_name}", headers=_auth(token), json=body)


def _runs_by_id(client: TestClient) -> dict:
    resp = client.get("/runs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    return {run["run_id"]: run for run in resp.json()}


def _tainted_parent(client: TestClient):
    """An alpha parent that has read the roadmap and created a draft."""
    parent = _run_ok(client, {"project": "alpha", "clearance": 2, "tenant": "alpha"})
    resp = _tool(client, parent["token"], "shared_doc", "read", "alpha-roadmap")
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "allowed"
    parent_now = _runs_by_id(client)[parent["run_id"]]
    assert parent_now["effective_level"] == 2, parent_now
    assert parent_now["effective_compartments"] == ["alpha"], parent_now

    resp = client.post(
        "/artifacts",
        json={
            "resource_id": "alpha-spawn-draft",
            "kind": "doc",
            "content": ALPHA_DRAFT_CONTENT,
        },
        headers=_auth(parent["token"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sensitivity"] == 2, resp.text
    assert resp.json()["compartments"] == ["alpha"], resp.text
    return parent


def test_continuing_spawn_inherits_parent_taint(tmp_path):
    with _make_client(tmp_path) as client:
        parent = _tainted_parent(client)
        parent_now = _runs_by_id(client)[parent["run_id"]]

        # Client-supplied effective values (and even a different
        # project/clearance) must be overridden by the server.
        child = _run_ok(
            client,
            {
                "project": "beta",
                "clearance": 0,
                "tenant": "beta",
                "effective_level": 0,
                "effective_compartments": [],
                "parent_run_id": parent["run_id"],
                "spawn_mode": "continuing",
            },
        )
        assert child["parent_run_id"] == parent["run_id"], child
        assert child["spawn_mode"] == "continuing", child
        # Child effective label equals the parent's current label.
        assert child["effective_level"] == parent_now["effective_level"] == 2, child
        assert child["effective_compartments"] == ["alpha"], child
        assert _runs_by_id(client)[child["run_id"]]["effective_level"] == 2

        # Provenance survives the fresh token: the continuing child
        # still cannot write down to the public outbox.
        resp = _tool(
            client, child["token"], "shared_kv", "write", "public-outbox",
            PUBLIC_NOTICE,
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["decision"] == "blocked", body
        assert body["policy_id"] == "deny-write-down", body
        # Flow denies never suspend.
        assert _runs_by_id(client)[child["run_id"]]["suspended"] is False


def test_clean_spawn_starts_clean_and_is_not_a_skeleton_key(tmp_path):
    with _make_client(tmp_path) as client:
        parent = _tainted_parent(client)

        # A clean public context spawned by the controller: it claims
        # the parent's compartments at creation, and the server must
        # strip them — clean means no context is transferred.
        clean = _run_ok(
            client,
            {
                "project": "public",
                "clearance": 0,
                "effective_level": 2,
                "effective_compartments": ["alpha"],
                "parent_run_id": parent["run_id"],
                "spawn_mode": "clean",
            },
        )
        assert clean["parent_run_id"] == parent["run_id"], clean
        assert clean["spawn_mode"] == "clean", clean
        assert clean["effective_level"] == 0, clean
        assert clean["effective_compartments"] == [], clean

        # The clean context can publish an innocent public notice.
        resp = _tool(
            client, clean["token"], "shared_kv", "write", "public-outbox",
            PUBLIC_NOTICE,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed", resp.text

        # But the spawn link alone grants nothing: reading the parent's
        # restricted artifact is blocked.
        resp = _tool(
            client, clean["token"], "shared_doc", "read", "alpha-spawn-draft"
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["decision"] == "blocked", body
        assert body["policy_id"] in ("deny-cross-project", "deny-compartment"), body


def test_unknown_parent_rejected_422(tmp_path):
    with _make_client(tmp_path) as client:
        _create_run(
            client,
            {
                "project": "alpha",
                "clearance": 2,
                "parent_run_id": "no-such-parent-run",
                "spawn_mode": "continuing",
            },
            expect=422,
        )


def test_spawn_mode_without_parent_rejected_422(tmp_path):
    with _make_client(tmp_path) as client:
        _create_run(
            client,
            {"project": "alpha", "clearance": 2, "spawn_mode": "clean"},
            expect=422,
        )


def test_graph_has_spawn_edge(tmp_path):
    with _make_client(tmp_path) as client:
        parent = _tainted_parent(client)
        child = _run_ok(
            client,
            {
                "project": "alpha",
                "clearance": 2,
                "parent_run_id": parent["run_id"],
                "spawn_mode": "continuing",
            },
        )
        resp = client.get("/graph", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        graph = resp.json()
        spawn_edges = [
            e for e in graph["edges"] if e.get("operation") == "spawn"
        ]
        assert spawn_edges, graph
        assert any(
            e["source"] == f"run:{parent['run_id']}"
            and e["target"] == f"run:{child['run_id']}"
            for e in spawn_edges
        ), spawn_edges


def test_non_spawn_creation_unchanged(tmp_path):
    with _make_client(tmp_path) as client:
        run = _run_ok(client, {"project": "alpha", "clearance": 2})
        assert run["effective_level"] == 0, run
        assert run["effective_compartments"] == [], run
        assert run.get("parent_run_id") is None, run
        assert run.get("spawn_mode") is None, run
        assert run["project"] == "alpha", run
        assert run["clearance"] == 2, run
        assert run["suspended"] is False, run
