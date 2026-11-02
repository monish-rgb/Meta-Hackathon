"""FlowGate gateway e2e tests — Tier 1 acceptance (admin auth, effective
labels, write-down flow control, twin public/restricted scenario).

Run context is backend/ (imports read as ``from app.main import create_app``
and ``from seed import ...``). A sys.path fallback keeps the test working
when pytest is invoked from the workspace root instead.
"""

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from tool_service import create_tool_app  # noqa: E402
from seed import ALPHA_DRAFT_CONTENT, PUBLIC_DRAFT_CONTENT, ROADMAP_CONTENT  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"

ADMIN_KEY = "test-admin"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}
WRONG_ADMIN_HEADERS = {"X-Admin-Key": "not-the-admin-key"}

_CANARY_RE = re.compile(r"CANARY-[A-Z0-9]+(?:-[A-Z0-9]+)*")

# Detection denies are acceptable alternatives to the flow deny in the
# restricted-payload tests (ordering between flow check and scan is an
# implementation detail); the write must be blocked either way.
DETECTION_DENY_POLICIES = {
    "detect-exact-copy",
    "detect-canary",
    "detect-taint-overlap",
    "detect-suspicious-overlap",
}
RESTRICTED_WRITE_POLICIES = {"deny-write-down"} | DETECTION_DENY_POLICIES


def _make_client(tmp_path, db_name: str = "gateway.db") -> TestClient:
    app = create_app(
        db_path=str(tmp_path / db_name),
        policies_path=str(POLICIES_PATH),
        admin_key=ADMIN_KEY,
    )
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _policy_id(resp) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("policy_id"):
        return body["policy_id"]
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("policy_id")
    if isinstance(detail, str):
        return detail
    return None


def _create_run(client: TestClient, project: str, clearance: int,
                tenant: str | None = None) -> dict:
    body: dict = {"project": project, "clearance": clearance}
    if tenant is not None:
        body["tenant"] = tenant
    resp = client.post(
        "/runs",
        json=body,
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run.get("token"), "run creation must return the bearer token"
    return run


def _tool(client: TestClient, token: str, tool_name: str, operation: str,
          resource_id: str, content: str | None = None):
    body: dict = {"operation": operation, "resource_id": resource_id}
    if content is not None:
        body["content"] = content
    return client.post(f"/tools/{tool_name}", headers=_auth(token), json=body)


def _runs_by_id(client: TestClient) -> dict:
    resp = client.get("/runs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    return {run["run_id"]: run for run in resp.json()}


def _create_artifact(client: TestClient, token: str, resource_id: str,
                     kind: str, content: str):
    return client.post(
        "/artifacts",
        json={"resource_id": resource_id, "kind": kind, "content": content},
        headers=_auth(token),
    )


def _assert_effective(client: TestClient, run_id: str, level: int,
                      compartments: list) -> None:
    run = _runs_by_id(client)[run_id]
    assert run.get("effective_level") == level, run
    assert list(run.get("effective_compartments") or []) == compartments, run


def _assert_blocked(resp, status: int = 403) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body.get("decision") == "blocked", body
    return body


# ---------------------------------------------------------------------------
# 1. Admin gating
# ---------------------------------------------------------------------------

def test_admin_gating(tmp_path):
    with _make_client(tmp_path) as client:
        # POST /runs: no key -> 401, wrong key -> 401, correct key -> 201.
        resp = client.post("/runs", json={"project": "alpha", "clearance": 2})
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        resp = client.post(
            "/runs",
            json={"project": "alpha", "clearance": 2},
            headers=WRONG_ADMIN_HEADERS,
        )
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        run = _create_run(client, "alpha", 2)
        run_id, token = run["run_id"], run["token"]

        # A run bearer token used as the admin key is not admin -> 401.
        resp = client.post(
            "/runs",
            json={"project": "beta", "clearance": 1},
            headers={"X-Admin-Key": token},
        )
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        # Suspend / reactivate without (or with the wrong) admin key -> 401.
        for path in (f"/runs/{run_id}/suspend", f"/runs/{run_id}/reactivate"):
            resp = client.post(path)
            assert resp.status_code == 401, resp.text
            assert _policy_id(resp) == "admin-required", resp.text
            resp = client.post(path, headers=WRONG_ADMIN_HEADERS)
            assert resp.status_code == 401, resp.text
            assert _policy_id(resp) == "admin-required", resp.text
        # The rejected calls had no effect.
        assert _runs_by_id(client)[run_id]["suspended"] is False

        # A run token presented as admin key does not authorise suspend.
        resp = client.post(
            f"/runs/{run_id}/suspend", headers={"X-Admin-Key": token}
        )
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        # /demo/reset and /demo/run without the admin key -> 401.
        resp = client.post("/demo/reset")
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        resp = client.post("/demo/run")
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        # Positive controls: the admin key does suspend and reactivate.
        resp = client.post(f"/runs/{run_id}/suspend", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["suspended"] is True

        resp = client.post(f"/runs/{run_id}/reactivate", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["suspended"] is False


def test_demo_run_endpoint_returns_twin_scenario(tmp_path):
    with _make_client(tmp_path) as client:
        resp = client.post("/demo/run", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        steps = resp.json()["steps"]
        assert len(steps) == 8, steps
        assert [s["step"] for s in steps] == list(range(1, 9)), steps
        assert all(s["ok"] for s in steps), steps


# ---------------------------------------------------------------------------
# 2. Twin scenario end-to-end (mirrors /demo/run via HTTP)
# ---------------------------------------------------------------------------

def test_twin_scenario_e2e(tmp_path):
    with _make_client(tmp_path) as client:
        a_public = _create_run(client, "public", 0)
        b_public = _create_run(client, "public", 0)
        a_alpha = _create_run(client, "alpha", 2)
        b_alpha = _create_run(client, "alpha", 2)

        # Public chain ------------------------------------------------------------
        # 1: A_public reads public-source -> allowed.
        resp = _tool(client, a_public["token"], "shared_doc", "read", "public-source")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        assert resp.json().get("result"), "expected public source content"

        # 2: A_public writes the public draft text to public-draft -> allowed.
        resp = _tool(client, a_public["token"], "shared_doc", "write",
                     "public-draft", PUBLIC_DRAFT_CONTENT)
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        # 3: B_public reads public-draft -> allowed, with the draft content.
        resp = _tool(client, b_public["token"], "shared_doc", "read", "public-draft")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        draft_content = resp.json()["result"]
        assert draft_content == PUBLIC_DRAFT_CONTENT

        # 4: B_public writes what it read to public-outbox -> allowed.
        resp = _tool(client, b_public["token"], "shared_kv", "write",
                     "public-outbox", draft_content)
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        # Restricted chain ---------------------------------------------------------
        # 5: A_alpha reads alpha-roadmap -> allowed.
        resp = _tool(client, a_alpha["token"], "shared_doc", "read", "alpha-roadmap")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        assert resp.json()["result"] == ROADMAP_CONTENT

        # Reading the roadmap taints A_alpha: level 2, compartment alpha.
        _assert_effective(client, a_alpha["run_id"], 2, ["alpha"])

        # 6: A_alpha creates a NEW artifact; the server assigns the
        # inherited label (2, [alpha]) from A_alpha's effective label.
        resp = _create_artifact(client, a_alpha["token"], "alpha-plan-draft",
                                "doc", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 201, resp.text
        artifact = resp.json()
        assert artifact["sensitivity"] == 2, artifact
        assert artifact["compartments"] == ["alpha"], artifact
        assert artifact["version"] == 1, artifact
        assert artifact["created_by"] == a_alpha["run_id"], artifact

        # 7: B_alpha reads alpha-plan-draft -> allowed, with the content.
        resp = _tool(client, b_alpha["token"], "shared_doc", "read", "alpha-plan-draft")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        restricted_content = resp.json()["result"]
        assert restricted_content == ALPHA_DRAFT_CONTENT

        # Reading only alpha-draft taints B_alpha the same way; the public
        # twins were never tainted by their sensitivity-0 reads.
        _assert_effective(client, b_alpha["run_id"], 2, ["alpha"])
        _assert_effective(client, a_public["run_id"], 0, [])
        _assert_effective(client, b_public["run_id"], 0, [])

        # 8: B_alpha tries to publish that content to public-outbox ->
        # blocked with deny-write-down, and NOT suspended.
        resp = _tool(client, b_alpha["token"], "shared_kv", "write",
                     "public-outbox", restricted_content)
        body = _assert_blocked(resp)
        assert body["policy_id"] == "deny-write-down", body
        if "action" in body:
            assert body["action"] != "deny_and_suspend", body
        assert _runs_by_id(client)[b_alpha["run_id"]]["suspended"] is False

        # The public-outbox still holds the public text from step 4.
        resp = _tool(client, b_public["token"], "shared_kv", "read", "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        assert resp.json()["result"] == PUBLIC_DRAFT_CONTENT


# ---------------------------------------------------------------------------
# 3. Restricted payloads cannot reach the public outbox
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload_kind",
    ["verbatim-excerpt", "paraphrase", "canary-stripped"],
)
def test_restricted_payloads_blocked_from_public_outbox(tmp_path, payload_kind):
    if payload_kind == "verbatim-excerpt":
        payload = ROADMAP_CONTENT[:240]
    elif payload_kind == "paraphrase":
        payload = ALPHA_DRAFT_CONTENT
    else:
        payload = _CANARY_RE.sub("", ROADMAP_CONTENT)
        assert "CANARY-" not in payload

    with _make_client(tmp_path) as client:
        alpha = _create_run(client, "alpha", 2)

        resp = _tool(client, alpha["token"], "shared_doc", "read", "alpha-roadmap")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        resp = _tool(client, alpha["token"], "shared_kv", "write",
                     "public-outbox", payload)
        body = _assert_blocked(resp)
        assert body["policy_id"] in RESTRICTED_WRITE_POLICIES, body

        # Nothing reached the outbox (read back as a fresh public run).
        reader = _create_run(client, "public", 0)
        resp = _tool(client, reader["token"], "shared_kv", "read", "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        assert resp.json()["result"] == ""


# ---------------------------------------------------------------------------
# 4. Clean public behaviour (sensitivity-0 false-positive regression)
# ---------------------------------------------------------------------------

def test_clean_public_behaviour_no_false_positive(tmp_path):
    with _make_client(tmp_path) as client:
        clean = _create_run(client, "public", 0)

        # Seed a sensitivity-0 fingerprint by reading the public source.
        resp = _tool(client, clean["token"], "shared_doc", "read", "public-source")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        # Write identical public text to both public destinations: the
        # sensitivity-0 fingerprints from the reads above must not turn
        # these into exact-copy blocks.
        resp = _tool(client, clean["token"], "shared_doc", "write",
                     "public-draft", PUBLIC_DRAFT_CONTENT)
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        resp = _tool(client, clean["token"], "shared_doc", "read", "public-draft")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == PUBLIC_DRAFT_CONTENT

        resp = _tool(client, clean["token"], "shared_kv", "write",
                     "public-outbox", PUBLIC_DRAFT_CONTENT)
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"

        resp = _tool(client, clean["token"], "shared_kv", "read", "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "allowed"
        assert resp.json()["result"] == PUBLIC_DRAFT_CONTENT

        run = _runs_by_id(client)[clean["run_id"]]
        assert run["suspended"] is False
        _assert_effective(client, clean["run_id"], 0, [])


# ---------------------------------------------------------------------------
# 5. Beta control: cross-project read is still blocked
# ---------------------------------------------------------------------------

def test_beta_cross_project_read_still_blocked(tmp_path):
    with _make_client(tmp_path) as client:
        beta = _create_run(client, "beta", 1)

        resp = _tool(client, beta["token"], "shared_doc", "read", "alpha-roadmap")
        body = _assert_blocked(resp)
        assert body["policy_id"] == "deny-cross-project", body

        assert _runs_by_id(client)[beta["run_id"]]["suspended"] is False


# ---------------------------------------------------------------------------
# 6. Tier 2: admin boundary, persistence, atomicity, artifacts, receipts
# ---------------------------------------------------------------------------

def test_admin_fail_closed_without_configured_key(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "closed.db"), policies_path=str(POLICIES_PATH)
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # The well-known dev key must NOT work outside demo mode.
        resp = client.post(
            "/runs", json={"project": "alpha", "clearance": 2},
            headers={"X-Admin-Key": "flowgate-dev-admin"},
        )
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text
        assert client.get("/runs").status_code == 401


def test_admin_demo_mode_uses_dev_key(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "demo.db"),
        policies_path=str(POLICIES_PATH),
        demo_mode=True,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/runs", json={"project": "alpha", "clearance": 2},
            headers={"X-Admin-Key": "flowgate-dev-admin"},
        )
        assert resp.status_code == 201, resp.text


def test_get_endpoints_require_admin(tmp_path):
    with _make_client(tmp_path) as client:
        for path in ("/runs", "/resources", "/events", "/graph", "/tool-receipts"):
            assert client.get(path).status_code == 401, path
            resp = client.get(path, headers=ADMIN_HEADERS)
            assert resp.status_code == 200, (path, resp.text)


def test_restart_preserves_resource_content(tmp_path):
    with _make_client(tmp_path) as client:
        writer = _create_run(client, "public", 0)
        resp = _tool(client, writer["token"], "shared_kv", "write",
                     "public-outbox", "persistent hello")
        assert resp.status_code == 200, resp.text
    # A fresh app on the same DB (simulated restart) must not reseed
    # over the stored content.
    with _make_client(tmp_path) as client:
        reader = _create_run(client, "public", 0)
        resp = _tool(client, reader["token"], "shared_kv", "read", "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "persistent hello"


def test_failed_event_insert_rolls_back_write(tmp_path, monkeypatch):
    import app.store as store_module

    with TestClient(
        create_app(
            db_path=str(tmp_path / "atomic.db"),
            policies_path=str(POLICIES_PATH),
            admin_key=ADMIN_KEY,
        ),
        raise_server_exceptions=False,
    ) as client:
        writer = _create_run(client, "public", 0)
        real_insert_event = store_module.insert_event

        def boom(*args, **kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(store_module, "insert_event", boom)
        resp = _tool(client, writer["token"], "shared_kv", "write",
                     "public-outbox", "must not persist")
        assert resp.status_code == 500, resp.text
        monkeypatch.setattr(store_module, "insert_event", real_insert_event)

        reader = _create_run(client, "public", 0)
        resp = _tool(client, reader["token"], "shared_kv", "read", "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == ""


def test_artifact_versions_and_tool_receipts(tmp_path):
    with _make_client(tmp_path) as client:
        alpha = _create_run(client, "alpha", 2)
        resp = _tool(client, alpha["token"], "shared_doc", "read", "alpha-roadmap")
        assert resp.status_code == 200, resp.text

        resp = _create_artifact(client, alpha["token"], "alpha-plan-draft",
                                "doc", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 201, resp.text
        # Duplicate creation is rejected.
        resp = _create_artifact(client, alpha["token"], "alpha-plan-draft",
                                "doc", "again")
        assert resp.status_code == 409, resp.text

        # A tool write to the artifact appends version 2.
        resp = _tool(client, alpha["token"], "shared_doc", "write",
                     "alpha-plan-draft", ALPHA_DRAFT_CONTENT + " Updated.")
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == 2, resp.text
        assert resp.json().get("execution_receipt_id"), resp.text

        resp = client.get("/artifacts/alpha-plan-draft/versions",
                          headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        versions = resp.json()
        assert [v["version"] for v in versions] == [1, 2], versions
        assert versions[0]["compartments"] == ["alpha"], versions
        assert client.get("/artifacts/alpha-plan-draft/versions").status_code == 401

        # Denied write-down produces no execution receipt; allowed does.
        beta_alpha = _create_run(client, "alpha", 2)
        resp = _tool(client, beta_alpha["token"], "shared_doc", "read",
                     "alpha-plan-draft")
        assert resp.status_code == 200, resp.text
        before = client.get(
            "/tool-receipts",
            params={"run_id": beta_alpha["run_id"], "resource_id": "public-outbox"},
            headers=ADMIN_HEADERS,
        ).json()["count"]
        resp = _tool(client, beta_alpha["token"], "shared_kv", "write",
                     "public-outbox", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 403, resp.text
        after = client.get(
            "/tool-receipts",
            params={"run_id": beta_alpha["run_id"], "resource_id": "public-outbox"},
            headers=ADMIN_HEADERS,
        ).json()
        write_receipts = [
            r for r in after["receipts"] if r["operation"] == "write"
        ]
        assert len(write_receipts) == before, after


def test_restart_preserves_artifact_versions(tmp_path):
    with _make_client(tmp_path) as client:
        alpha = _create_run(client, "alpha", 2)
        assert _tool(client, alpha["token"], "shared_doc", "read",
                     "alpha-roadmap").status_code == 200
        resp = _create_artifact(client, alpha["token"], "alpha-plan-draft",
                                "doc", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 201, resp.text
        resp = _tool(client, alpha["token"], "shared_doc", "write",
                     "alpha-plan-draft", ALPHA_DRAFT_CONTENT + " Updated.")
        assert resp.status_code == 200, resp.text
    # Simulated restart: second app on the same DB must preserve the
    # artifact, its content, and its full version history.
    with _make_client(tmp_path) as client:
        reader = _create_run(client, "alpha", 2)
        resp = _tool(client, reader["token"], "shared_doc", "read",
                     "alpha-plan-draft")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == ALPHA_DRAFT_CONTENT + " Updated."
        resp = client.get("/artifacts/alpha-plan-draft/versions",
                          headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        assert [v["version"] for v in resp.json()] == [1, 2], resp.text


def test_security_invariants_canary_and_suspension(tmp_path):
    with _make_client(tmp_path) as client:
        # Auth failures are not recorded as events (no run is known).
        before = client.get("/events", headers=ADMIN_HEADERS).json()
        assert client.post("/runs", json={"project": "alpha", "clearance": 2}).status_code == 401
        assert client.get("/runs").status_code == 401
        after = client.get("/events", headers=ADMIN_HEADERS).json()
        assert len(after) == len(before)

        # A canary block must never echo the canary value into the
        # (admin-visible) event reason.
        beta = _create_run(client, "beta", 1)
        canary_text = "Innocent picnic notes CANARY-ALPHA-NOVA-7F3K9 end."
        resp = _tool(client, beta["token"], "shared_kv", "write",
                     "shared-board", canary_text)
        body = _assert_blocked(resp)
        assert body["policy_id"] == "detect-canary", body
        events = client.get("/events", headers=ADMIN_HEADERS).json()
        assert events, "expected the blocked event to be recorded"
        assert "CANARY-ALPHA-NOVA-7F3K9" not in str(events), events

        # A suspended run cannot create artifacts either.
        resp = _create_artifact(client, beta["token"], "beta-artifact",
                                "kv", "hello")
        body = _assert_blocked(resp)
        assert body["policy_id"] == "run-suspended", body


def test_tenant_isolation_and_alerts(tmp_path):
    with _make_client(tmp_path) as client:
        a_tenant = _create_run(client, "alpha", 2, tenant="alpha")
        assert a_tenant["tenant"] == "alpha", a_tenant
        assert _tool(client, a_tenant["token"], "shared_doc", "read",
                     "alpha-roadmap").status_code == 200
        resp = _create_artifact(client, a_tenant["token"], "tenant-a-plan",
                                "doc", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant"] == "alpha", resp.text

        # Same project, different tenant: still a hard boundary.
        b_tenant = _create_run(client, "alpha", 2, tenant="tenant-b")
        resp = _tool(client, b_tenant["token"], "shared_doc", "read",
                     "tenant-a-plan")
        body = _assert_blocked(resp)
        assert body["policy_id"] == "deny-cross-project", body

        # Same tenant, same project: allowed.
        a2_tenant = _create_run(client, "alpha", 2, tenant="alpha")
        resp = _tool(client, a2_tenant["token"], "shared_doc", "read",
                     "tenant-a-plan")
        assert resp.status_code == 200, resp.text

        # Alerts surface: the blocked attempts above are alerted with
        # run context, admin-gated, and only blocked decisions appear.
        assert client.get("/alerts").status_code == 401
        resp = client.get("/alerts", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["count"] >= 1, payload
        assert all(a["decision"] == "blocked" for a in payload["alerts"])
        cross = [a for a in payload["alerts"]
                 if a["policy_id"] == "deny-cross-project"]
        assert cross and cross[0]["run_tenant"] == "tenant-b", payload
        assert cross[0]["run_project"] == "alpha", payload


# ---------------------------------------------------------------------------
# 7. Gap regressions: creation scan, compartment reads, graph create edge
# ---------------------------------------------------------------------------

def test_artifact_creation_scans_restricted_content(tmp_path):
    with _make_client(tmp_path) as client:
        # A fresh run that never read the roadmap tries to stash the
        # known secret fixture as a public artifact: the seeded
        # fingerprint backstop must stop creation itself.
        fresh = _create_run(client, "public", 0)
        resp = _create_artifact(client, fresh["token"], "stolen-roadmap",
                                "doc", ROADMAP_CONTENT)
        body = _assert_blocked(resp)
        assert body["policy_id"] == "detect-exact-copy", body
        assert _runs_by_id(client)[fresh["run_id"]]["suspended"] is True
        resource_ids = {
            r["resource_id"]
            for r in client.get("/resources", headers=ADMIN_HEADERS).json()
        }
        assert "stolen-roadmap" not in resource_ids

        fresh2 = _create_run(client, "public", 0)
        resp = _create_artifact(
            client, fresh2["token"], "canary-note", "kv",
            "Innocent notes CANARY-ALPHA-NOVA-7F3K9 here.",
        )
        body = _assert_blocked(resp)
        assert body["policy_id"] == "detect-canary", body
        assert _runs_by_id(client)[fresh2["run_id"]]["suspended"] is True


def test_read_requires_all_compartments(tmp_path):
    with _make_client(tmp_path) as client:
        # Controller-initialised run entitled to both compartments.
        resp = client.post(
            "/runs",
            json={
                "project": "alpha", "clearance": 2,
                "effective_level": 2,
                "effective_compartments": ["alpha", "beta"],
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        creator = resp.json()
        resp = _create_artifact(
            client, creator["token"], "joint-plan", "doc",
            "Joint alpha beta planning note for entitled readers only.",
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["compartments"] == ["alpha", "beta"], resp.text

        # Alpha-only run: project+clearance match, but it is not
        # entitled to the beta compartment -> denied before disclosure.
        alpha_only = _create_run(client, "alpha", 2)
        resp = _tool(client, alpha_only["token"], "shared_doc", "read",
                     "joint-plan")
        body = _assert_blocked(resp)
        assert body["policy_id"] == "deny-compartment", body
        assert ALPHA_DRAFT_CONTENT not in resp.text

        # The entitled creator can read it back.
        resp = _tool(client, creator["token"], "shared_doc", "read",
                     "joint-plan")
        assert resp.status_code == 200, resp.text


def test_graph_includes_create_edge(tmp_path):
    with _make_client(tmp_path) as client:
        alpha = _create_run(client, "alpha", 2)
        assert _tool(client, alpha["token"], "shared_doc", "read",
                     "alpha-roadmap").status_code == 200
        resp = _create_artifact(client, alpha["token"], "alpha-plan-draft",
                                "doc", ALPHA_DRAFT_CONTENT)
        assert resp.status_code == 201, resp.text
        graph = client.get("/graph", headers=ADMIN_HEADERS).json()
        create_edges = [
            e for e in graph["edges"] if e["operation"] == "create"
        ]
        assert create_edges, graph
        assert create_edges[0]["source"] == f"run:{alpha['run_id']}"
        assert create_edges[0]["target"] == "res:alpha-plan-draft"


# ---------------------------------------------------------------------------
# 8. Separate protected tool backend (public-outbox integration)
# ---------------------------------------------------------------------------

class _ToolClientAdapter:
    """Adapts a tool-service TestClient to the gateway tool_client API."""

    KEY = "test-tool-key"

    def __init__(self, tmp_path):
        self.http = TestClient(
            create_tool_app(db_path=str(tmp_path / "tools.db"),
                            service_key=self.KEY)
        )

    def execute(self, run_id, operation, resource_id, content=None,
                request_id=None, payload_hash=None):
        payload = {"resource_id": resource_id, "operation": operation,
                   "content": content, "run_id": run_id}
        if request_id is not None:
            payload["request_id"] = request_id
        if payload_hash is not None:
            payload["payload_hash"] = payload_hash
        resp = self.http.post(
            "/execute",
            json=payload,
            headers={"X-Tool-Key": self.KEY},
        )
        if resp.status_code == 409:
            raise RuntimeError(
                f"request-id-reuse: tool backend 409: {resp.text}"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"tool backend {resp.status_code}: {resp.text}")
        body = resp.json()
        return body.get("result"), body.get("receipt_id")

    def receipt_for_request(self, request_id):
        resp = self.http.get(
            "/receipts", params={"request_id": request_id},
            headers={"X-Tool-Key": self.KEY},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"tool backend receipts {resp.status_code}: {resp.text}"
            )
        body = resp.json()
        receipts = body.get("receipts") if isinstance(body, dict) else body
        if not receipts:
            return None
        return receipts[0]

    def state(self, resource_id):
        resp = self.http.get(
            f"/state/{resource_id}", headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def receipts(self, run_id=None, resource_id=None):
        params = {}
        if run_id:
            params["run_id"] = run_id
        if resource_id:
            params["resource_id"] = resource_id
        resp = self.http.get(
            "/receipts", params=params, headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()


def test_tool_service_requires_key_and_gateway_mediation(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    # Agents cannot bypass the gateway: no key / wrong key -> 401.
    assert adapter.http.post(
        "/execute",
        json={"resource_id": "public-outbox", "operation": "write",
              "content": "pwned", "run_id": "agent"},
    ).status_code == 401
    assert adapter.http.post(
        "/execute",
        json={"resource_id": "public-outbox", "operation": "write",
              "content": "pwned", "run_id": "agent"},
        headers={"X-Tool-Key": "wrong"},
    ).status_code == 401
    assert adapter.state("public-outbox")["content"] == ""

    app = create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        admin_key=ADMIN_KEY,
        tool_client=adapter,
    )
    with TestClient(app) as client:
        public = _create_run(client, "public", 0)
        resp = _tool(client, public["token"], "shared_kv", "write",
                     "public-outbox", "hello external world")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("tool_receipt_id"), resp.text
        # Independent destination state lives in the tool backend.
        assert adapter.state("public-outbox")["content"] == "hello external world"
        assert adapter.receipts(run_id=public["run_id"])["count"] == 1

        # Reads also come from the tool backend.
        resp = _tool(client, public["token"], "shared_kv", "read",
                     "public-outbox")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "hello external world"

        # Denied write-down never reaches the tool backend.
        alpha = _create_run(client, "alpha", 2)
        assert _tool(client, alpha["token"], "shared_doc", "read",
                     "alpha-roadmap").status_code == 200
        resp = _tool(client, alpha["token"], "shared_kv", "write",
                     "public-outbox", ALPHA_DRAFT_CONTENT)
        body = _assert_blocked(resp)
        assert body["policy_id"] == "deny-write-down", body
        assert adapter.receipts(run_id=alpha["run_id"],
                                resource_id="public-outbox")["count"] == 0
        assert adapter.state("public-outbox")["content"] == "hello external world"
