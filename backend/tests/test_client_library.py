"""Tier 5 client-library tests for ``flowgate_client`` (repo root).

The client is driven through an httpx ASGI transport against a temp-db
gateway (the pattern from ``test_agent_demo``), plus a mock transport
that captures the exact JSON the client sends for run creation, so the
spawn-parameter forwarding is asserted independently of the server.
"""

import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
BACKEND_DIR = WORKSPACE / "backend"
for _path in (str(WORKSPACE), str(BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import httpx  # noqa: E402

from app.main import create_app  # noqa: E402
from flowgate_client import FlowGateClientError, GatewayClient  # noqa: E402
from seed import ALPHA_DRAFT_CONTENT  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"


def _gateway_client(tmp_path) -> GatewayClient:
    app = create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        demo_mode=True,
    )
    transport = httpx.ASGITransport(app=app)
    return GatewayClient(
        "http://testserver", admin_key="flowgate-dev-admin", transport=transport
    )


def test_create_run_and_gateway_tool_allowed(tmp_path):
    client = _gateway_client(tmp_path)
    try:
        run = client.create_run("public", 0)
        assert run["token"]
        assert run["project"] == "public"

        status, body = client.gateway_tool(run["token"], "read", "public-source")
        assert status == 200, body
        assert body["decision"] == "allowed", body
        assert body["result"]

        status, body = client.gateway_tool(
            run["token"], "write", "public-outbox",
            "Open day summary from the client library.",
        )
        assert status == 200, body
        assert body["decision"] == "allowed", body

        status, body = client.gateway_tool(run["token"], "read", "public-outbox")
        assert status == 200, body
        assert body["result"] == "Open day summary from the client library."
    finally:
        client.close()


def test_create_artifact_and_evidence_methods(tmp_path):
    client = _gateway_client(tmp_path)
    try:
        alpha = client.create_run("alpha", 2, tenant="alpha")
        status, body = client.gateway_tool(
            alpha["token"], "read", "alpha-roadmap"
        )
        assert status == 200, body
        assert body["decision"] == "allowed", body

        status, body = client.create_artifact(
            alpha["token"], "client-alpha-draft", "doc", ALPHA_DRAFT_CONTENT
        )
        assert status == 201, body
        assert body["sensitivity"] == 2, body
        assert body["compartments"] == ["alpha"], body

        receipts = client.tool_receipts(
            run_id=alpha["run_id"], resource_id="alpha-roadmap"
        )
        assert receipts["count"] >= 1, receipts
        assert any(
            r["operation"] == "read" for r in receipts["receipts"]
        ), receipts

        intents = client.execution_intents()
        assert "count" in intents, intents
        assert "intents" in intents, intents
    finally:
        client.close()


def test_create_run_failure_raises_client_error(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        demo_mode=True,
    )
    client = GatewayClient(
        "http://testserver",
        admin_key="not-the-admin-key",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        try:
            client.create_run("public", 0)
        except FlowGateClientError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected FlowGateClientError for a bad admin key")
    finally:
        client.close()


def test_spawn_params_forwarded():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        captured.append(payload)
        body = dict(payload)
        body.update({"run_id": "child-run", "token": "child-token"})
        return httpx.Response(201, json=body)

    client = GatewayClient(
        "http://testserver",
        admin_key="admin-x",
        transport=httpx.MockTransport(handler),
    )
    try:
        run = client.create_run(
            "alpha",
            2,
            tenant="alpha",
            parent_run_id="parent-run",
            spawn_mode="continuing",
            effective_level=0,
            effective_compartments=[],
        )
        assert run["run_id"] == "child-run"
        assert len(captured) == 1, captured
        sent = captured[0]
        assert sent["project"] == "alpha", sent
        assert sent["clearance"] == 2, sent
        assert sent["tenant"] == "alpha", sent
        assert sent["parent_run_id"] == "parent-run", sent
        assert sent["spawn_mode"] == "continuing", sent
        assert sent["effective_level"] == 0, sent
        assert sent["effective_compartments"] == [], sent

        # Optional fields omitted when not supplied (server defaults).
        captured.clear()
        client.create_run("public", 0)
        sent = captured[0]
        assert sent == {"project": "public", "clearance": 0}, sent
    finally:
        client.close()
