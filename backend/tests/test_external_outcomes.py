"""Tier 4 external-execution outcome tests (regression for the two P1 bugs).

Bug A: a completed external write whose response is lost must be reported
as ``unknown`` (never ``blocked``), then reconciled on retry without a
second backend execution.

Bug B: an external write that commits on the backend but whose gateway
audit insert fails must also be ``unknown``; the local audit/version
transaction completes exactly once on a reconciling retry.

Run context is backend/; a sys.path fallback keeps pytest working from
the workspace root.
"""

import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.store as store_module  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tools import (  # noqa: E402
    ToolBackendAmbiguous,
    ToolBackendError,
    ToolBackendNotExecuted,
)
from file_docs_service import create_file_docs_app  # noqa: E402
from tool_service import create_tool_app  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"

ADMIN_KEY = "test-admin"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


# ---------------------------------------------------------------------------
# Helpers (duplicated tiny helpers; nothing imported from test_gateway_e2e)
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise AssertionError(f"expected JSON body, got: {resp.text!r}")
    assert isinstance(body, dict), body
    return body


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


def _create_run(client: TestClient, project: str, clearance: int) -> dict:
    resp = client.post(
        "/runs",
        json={"project": project, "clearance": clearance},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run.get("token"), "run creation must return the bearer token"
    return run


def _tool_write(client: TestClient, token: str, content: str,
                request_id: str | None = None, resource_id: str = "public-outbox",
                tool_name: str = "shared_kv"):
    body: dict = {
        "operation": "write",
        "resource_id": resource_id,
        "content": content,
    }
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/tools/{tool_name}", headers=_auth(token), json=body)


def _make_client(tmp_path, tool_client, db_name: str = "gateway.db") -> TestClient:
    app = create_app(
        db_path=str(tmp_path / db_name),
        policies_path=str(POLICIES_PATH),
        admin_key=ADMIN_KEY,
        tool_client=tool_client,
    )
    return TestClient(app, raise_server_exceptions=False)


def _gateway_receipt_count(client: TestClient, run_id: str,
                           resource_id: str = "public-outbox") -> int:
    resp = client.get(
        "/tool-receipts",
        params={"run_id": run_id, "resource_id": resource_id},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if isinstance(body, dict) and "count" in body:
        return int(body["count"])
    receipts = body.get("receipts") if isinstance(body, dict) else body
    return len(receipts or [])


def _outbox_version_count(client: TestClient) -> int:
    resp = client.get(
        "/artifacts/public-outbox/versions", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        versions = body.get("versions") or body.get("items") or []
        if "count" in body:
            return int(body["count"])
        return len(versions)
    raise AssertionError(f"unexpected versions body: {body!r}")


def _backend_receipts_for_request(adapter, request_id: str) -> list:
    resp = adapter.http.get(
        "/receipts", params={"request_id": request_id},
        headers={"X-Tool-Key": adapter.KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    receipts = body.get("receipts") if isinstance(body, dict) else body
    return list(receipts or [])


def _execution_intents(client: TestClient) -> list:
    resp = client.get("/execution-intents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("intents", "execution_intents", "items", "results", "data"):
            if isinstance(body.get(key), list):
                return body[key]
        if "count" in body and len(body) <= 2:
            return []
    raise AssertionError(f"unexpected execution-intents body: {body!r}")


def _intent_for(client: TestClient, request_id: str) -> dict | None:
    for intent in _execution_intents(client):
        if isinstance(intent, dict) and intent.get("request_id") == request_id:
            return intent
    return None


def _intent_state(intent: dict) -> str | None:
    for key in ("state", "status", "intent_state", "outcome"):
        if intent.get(key):
            return intent[key]
    return None


# ---------------------------------------------------------------------------
# Real tool-service adapter (same pattern as test_gateway_e2e)
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

    # Alias in case the gateway reconciles via a differently-named lookup.
    def get_receipt_for_request(self, request_id):
        return self.receipt_for_request(request_id)

    def state(self, resource_id):
        resp = self.http.get(
            f"/state/{resource_id}", headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def receipts(self, run_id=None, resource_id=None, request_id=None):
        params = {}
        if run_id:
            params["run_id"] = run_id
        if resource_id:
            params["resource_id"] = resource_id
        if request_id:
            params["request_id"] = request_id
        resp = self.http.get(
            "/receipts", params=params, headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()


# ---------------------------------------------------------------------------
# File-docs adapter (Tier 5 backend under the Tier 4 outcome rules)
# ---------------------------------------------------------------------------

class _FileDocsCommitGate:
    """Fails exactly one numbered commit on the file-docs receipts DB.

    ``sqlite3.Connection.commit`` is read-only on the C type, so the
    gate is installed via a ``Connection`` subclass (the ``factory``
    argument) when the file-docs app opens its receipts connection --
    that connection is the one the service exposes as ``app.state.conn``.
    ``sqlite3.connect`` is restored immediately after app creation, so
    the gateway's own database never sees the gated connection class.
    """

    def __init__(self):
        self.fail_on_commit = None
        self.commits_since_arm = 0
        self.tripped = False

    def arm(self, fail_on_commit: int) -> None:
        self.fail_on_commit = fail_on_commit
        self.commits_since_arm = 0
        self.tripped = False

    def disarm(self) -> None:
        self.fail_on_commit = None


def _create_file_docs_app_with_gate(root_dir, gate, service_key):
    class _GatedConnection(sqlite3.Connection):
        def commit(self):  # noqa: D102 - fault-injection wrapper
            if gate.fail_on_commit is not None:
                gate.commits_since_arm += 1
                if gate.commits_since_arm == gate.fail_on_commit:
                    gate.fail_on_commit = None
                    gate.tripped = True
                    raise RuntimeError(
                        "injected file-docs committed-marking commit failure"
                    )
            return super().commit()

    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        kwargs["factory"] = _GatedConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = _connect
    try:
        return create_file_docs_app(root_dir=str(root_dir), service_key=service_key)
    finally:
        sqlite3.connect = real_connect


class _FileDocsAdapter:
    """Adapts a file-docs TestClient to the gateway tool_client API.

    Mirrors ``ToolServiceClient``'s Tier 4 classification against the
    file-docs backend: 409 is a request-id binding error, 5xx is
    ambiguous (the two-phase write may have landed), other refusals are
    definitive non-executions.  ``receipt_for_request`` ignores
    ``pending`` receipts -- a pending receipt is not proof of
    execution, so the gateway must redispatch under the same
    ``request_id`` and let the backend promote/complete the write.
    """

    KEY = "test-file-docs-key"

    def __init__(self, tmp_path, gate: _FileDocsCommitGate):
        self.root = tmp_path / "file-docs"
        self.app = _create_file_docs_app_with_gate(self.root, gate, self.KEY)
        self.http = TestClient(self.app, raise_server_exceptions=False)

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
            raise ToolBackendError(
                f"tool backend request-id-reuse (409): {resp.text}"
            )
        if resp.status_code >= 500:
            raise ToolBackendAmbiguous(
                "tool backend outcome unknown: HTTP 5xx may have committed"
            )
        if resp.status_code != 200:
            raise ToolBackendNotExecuted(
                f"tool backend refused execution ({resp.status_code}): {resp.text}"
            )
        body = resp.json()
        return body.get("result"), body.get("receipt_id")

    def receipt_for_request(self, request_id):
        resp = self.http.get(
            "/receipts", params={"request_id": request_id},
            headers={"X-Tool-Key": self.KEY},
        )
        if resp.status_code != 200:
            raise ToolBackendAmbiguous(
                f"tool backend receipt lookup failed ({resp.status_code}): {resp.text}"
            )
        body = resp.json()
        receipts = body.get("receipts") if isinstance(body, dict) else body
        committed = [
            r for r in (receipts or [])
            if not (isinstance(r, dict) and r.get("state") == "pending")
        ]
        if not committed:
            return None
        return committed[0]

    def get_receipt_for_request(self, request_id):
        return self.receipt_for_request(request_id)

    def state(self, resource_id):
        resp = self.http.get(
            f"/state/{resource_id}", headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def receipts(self, run_id=None, resource_id=None, request_id=None):
        params = {}
        if run_id:
            params["run_id"] = run_id
        if resource_id:
            params["resource_id"] = resource_id
        if request_id:
            params["request_id"] = request_id
        resp = self.http.get(
            "/receipts", params=params, headers={"X-Tool-Key": self.KEY}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()


class _AmbiguousAfterCommitWrapper:
    """Executes for real (backend commits + receipt), then loses the response.

    Raises a plain ``RuntimeError``: the gateway must treat a plain
    RuntimeError from the tool client as an ambiguous outcome, because
    the backend may already have committed.
    """

    def __init__(self, real: _ToolClientAdapter):
        self._real = real
        self._failures_remaining = 1

    def execute(self, run_id, operation, resource_id, content=None,
                request_id=None, payload_hash=None, **kwargs):
        if request_id is None:
            request_id = kwargs.pop("request_id", None)
        if payload_hash is None:
            payload_hash = kwargs.pop("payload_hash", None)
        result, receipt_id = self._real.execute(
            run_id, operation, resource_id, content=content,
            request_id=request_id, payload_hash=payload_hash,
        )
        if operation == "write" and self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise RuntimeError(
                "simulated lost response after tool backend commit"
            )
        return result, receipt_id

    def receipt_for_request(self, request_id):
        return self._real.receipt_for_request(request_id)

    def get_receipt_for_request(self, request_id):
        return self._real.receipt_for_request(request_id)

    def state(self, resource_id):
        return self._real.state(resource_id)

    def receipts(self, run_id=None, resource_id=None, request_id=None):
        return self._real.receipts(
            run_id=run_id, resource_id=resource_id, request_id=request_id
        )


class _DefinitiveFailureWrapper:
    """Always raises ToolBackendNotExecuted (execution never happened)."""

    def __init__(self, real: _ToolClientAdapter):
        self._real = real

    def execute(self, run_id, operation, resource_id, content=None,
                request_id=None, payload_hash=None, **kwargs):
        raise ToolBackendNotExecuted(
            "simulated definitive tool backend failure before execution"
        )

    def receipt_for_request(self, request_id):
        return self._real.receipt_for_request(request_id)

    def get_receipt_for_request(self, request_id):
        return self._real.receipt_for_request(request_id)

    def state(self, resource_id):
        return self._real.state(resource_id)

    def receipts(self, run_id=None, resource_id=None, request_id=None):
        return self._real.receipts(
            run_id=run_id, resource_id=resource_id, request_id=request_id
        )


# ---------------------------------------------------------------------------
# Bug A: response lost after backend commit -> unknown, then reconciled
# ---------------------------------------------------------------------------

def test_bug_a_lost_response_after_commit_is_unknown_then_reconciled(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    wrapper = _AmbiguousAfterCommitWrapper(adapter)
    content = (
        "Bug A external outcome message about gardening tools "
        "and weekend picnic planning notes for reconciliation."
    )
    with _make_client(tmp_path, wrapper) as client:
        writer = _create_run(client, "public", 0)
        initial_versions = _outbox_version_count(client)

        first = _tool_write(client, writer["token"], content,
                            request_id="req-bug-a")
        body = _body(first)
        assert first.status_code in (500, 503), first.text
        # A committed write must never be reported as blocked.
        assert body.get("decision") == "unknown", body
        assert body.get("decision") != "blocked", body
        assert body.get("policy_id") == "execution-outcome-unknown", body
        assert body.get("request_id") == "req-bug-a", body

        # The backend really did commit, exactly once.
        assert len(_backend_receipts_for_request(adapter, "req-bug-a")) == 1
        assert adapter.receipt_for_request("req-bug-a") is not None
        assert adapter.state("public-outbox")["content"] == content

        # Retry with the same request_id + same payload: reconcile/replay,
        # without a second backend execution, receipt, or version.
        retry = _tool_write(client, writer["token"], content,
                            request_id="req-bug-a")
        retry_body = _body(retry)
        assert retry.status_code == 200, retry.text
        assert retry_body.get("decision") == "allowed", retry_body
        assert (
            retry_body.get("reconciled") is True
            or retry_body.get("replayed") is True
        ), retry_body
        assert retry_body.get("request_id") == "req-bug-a", retry_body

        assert len(_backend_receipts_for_request(adapter, "req-bug-a")) == 1
        assert _gateway_receipt_count(client, writer["run_id"]) == 1
        assert _outbox_version_count(client) == initial_versions + 1


# ---------------------------------------------------------------------------
# Bug B: gateway audit insert fails after backend commit -> unknown
# ---------------------------------------------------------------------------

def test_bug_b_audit_failure_after_commit_is_unknown_then_reconciled(
    tmp_path, monkeypatch
):
    adapter = _ToolClientAdapter(tmp_path)
    content = (
        "Bug B external outcome message about library books "
        "and evening reading club arrangements for audit checks."
    )
    with _make_client(tmp_path, adapter) as client:
        writer = _create_run(client, "public", 0)
        initial_versions = _outbox_version_count(client)

        real_insert_event = store_module.insert_event
        fault_state = {"failed": False}

        def flaky_insert_event(*args, **kwargs):
            decision = kwargs.get("decision")
            if decision is None and len(args) >= 6:
                # insert_event(conn, run_id, tool, operation, resource_id,
                #               decision, ...)
                decision = args[5]
            if decision == "allowed" and not fault_state["failed"]:
                fault_state["failed"] = True
                raise RuntimeError(
                    "injected gateway audit failure after backend commit"
                )
            return real_insert_event(*args, **kwargs)

        # Arm the fault immediately before the write call so setup
        # events (if any) are not the ones that fail.
        monkeypatch.setattr(store_module, "insert_event", flaky_insert_event)
        first = _tool_write(client, writer["token"], content,
                            request_id="req-bug-b")
        body = _body(first)
        # Restore the real audit path before any retry.
        monkeypatch.setattr(store_module, "insert_event", real_insert_event)

        assert fault_state["failed"] is True
        assert first.status_code in (500, 503), first.text
        assert body.get("decision") == "unknown", body
        assert body.get("decision") not in ("blocked", "allowed"), body
        assert body.get("policy_id") == "execution-outcome-unknown", body
        assert body.get("request_id") == "req-bug-b", body

        # Backend committed; the gateway audit transaction did not.
        assert len(_backend_receipts_for_request(adapter, "req-bug-b")) == 1
        assert adapter.state("public-outbox")["content"] == content
        assert _gateway_receipt_count(client, writer["run_id"]) == 0

        retry = _tool_write(client, writer["token"], content,
                            request_id="req-bug-b")
        retry_body = _body(retry)
        assert retry.status_code == 200, retry.text
        assert retry_body.get("decision") == "allowed", retry_body
        assert retry_body.get("reconciled") is True, retry_body
        assert retry_body.get("request_id") == "req-bug-b", retry_body

        assert len(_backend_receipts_for_request(adapter, "req-bug-b")) == 1
        assert _gateway_receipt_count(client, writer["run_id"]) == 1
        assert _outbox_version_count(client) == initial_versions + 1


# ---------------------------------------------------------------------------
# File-docs backend: committed-marking failure -> unknown, then recovered
# ---------------------------------------------------------------------------

def test_file_docs_commit_failure_is_unknown_then_recovers_exactly_once(tmp_path):
    """Gateway-level reviewer reproduction against the file-docs backend.

    The service's committed-marking commit fails on the first write:
    the file has changed, the receipt is still ``pending``, and the
    gateway must report ``unknown`` -- never ``blocked``.  Retrying
    under the same ``request_id`` promotes the pending receipt (the
    adapter's receipt lookup returns ``None`` for pending, so the
    gateway redispatches and the backend self-heals) and completes the
    gateway audit exactly once.
    """
    gate = _FileDocsCommitGate()
    adapter = _FileDocsAdapter(tmp_path, gate)
    content = (
        "File docs gateway message about gardening tools "
        "and weekend picnic planning notes for outcome checks."
    )
    with _make_client(tmp_path, adapter) as client:
        writer = _create_run(client, "public", 0)
        initial_versions = _outbox_version_count(client)

        # Commit cadence of one file-docs write, learned in
        # test_file_docs: commit 1 is the durable pending insert,
        # commit 2 marks the receipt committed after the replace.
        gate.arm(2)
        first = _tool_write(client, writer["token"], content,
                            request_id="req-file-docs-unknown")
        body = _body(first)
        assert gate.tripped is True
        assert first.status_code == 503, first.text
        # An ambiguous file-docs outcome is never reported as blocked.
        assert body.get("decision") == "unknown", body
        assert body.get("decision") != "blocked", body
        assert body.get("policy_id") == "execution-outcome-unknown", body
        assert body.get("request_id") == "req-file-docs-unknown", body

        # The backend file really changed despite the failed call, and
        # the single backend receipt is still pending.
        assert adapter.state("public-outbox")["content"] == content
        pending = _backend_receipts_for_request(
            adapter, "req-file-docs-unknown"
        )
        assert len(pending) == 1, pending
        assert pending[0].get("state") == "pending", pending

        gate.disarm()
        retry = _tool_write(client, writer["token"], content,
                            request_id="req-file-docs-unknown")
        retry_body = _body(retry)
        assert retry.status_code == 200, retry.text
        assert retry_body.get("decision") == "allowed", retry_body
        assert retry_body.get("request_id") == "req-file-docs-unknown", retry_body

        # Exactly-once everywhere: the pending receipt was promoted, not
        # duplicated; one gateway receipt; one new artifact version.
        backend_receipts = _backend_receipts_for_request(
            adapter, "req-file-docs-unknown"
        )
        assert len(backend_receipts) == 1, backend_receipts
        assert backend_receipts[0].get("state") == "committed", backend_receipts
        assert _gateway_receipt_count(client, writer["run_id"]) == 1
        assert _outbox_version_count(client) == initial_versions + 1
        assert adapter.state("public-outbox")["content"] == content


# ---------------------------------------------------------------------------
# Request-ID reuse, definitive failure, replay, admin gating
# ---------------------------------------------------------------------------

def test_request_id_reuse_with_different_content_is_409(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    first_content = (
        "Reuse test first message about mountain hiking trails "
        "and trail map printing for the weekend group."
    )
    other_content = (
        "Reuse test second message about ocean sailing lessons "
        "and harbour timetable changes for next month."
    )
    with _make_client(tmp_path, adapter) as client:
        writer = _create_run(client, "public", 0)

        first = _tool_write(client, writer["token"], first_content,
                            request_id="req-reuse")
        assert first.status_code == 200, first.text
        versions_after_first = _outbox_version_count(client)
        assert adapter.state("public-outbox")["content"] == first_content

        second = _tool_write(client, writer["token"], other_content,
                             request_id="req-reuse")
        body = _body(second)
        assert second.status_code == 409, second.text
        assert _policy_id(second) == "request-id-reuse", body

        # Nothing executed for the mismatched reuse.
        assert adapter.state("public-outbox")["content"] == first_content
        assert len(_backend_receipts_for_request(adapter, "req-reuse")) == 1
        assert _outbox_version_count(client) == versions_after_first


def test_definitive_backend_failure_is_blocked_and_intent_failed(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    wrapper = _DefinitiveFailureWrapper(adapter)
    content = (
        "Definitive failure message about office stationery orders "
        "that must never reach the external tool backend."
    )
    with _make_client(tmp_path, wrapper) as client:
        writer = _create_run(client, "public", 0)

        resp = _tool_write(client, writer["token"], content,
                           request_id="req-definitive")
        body = _body(resp)
        assert resp.status_code == 503, resp.text
        assert body.get("decision") == "blocked", body
        assert body.get("policy_id") == "tool-backend-unavailable", body

        assert _backend_receipts_for_request(adapter, "req-definitive") == []
        assert adapter.receipt_for_request("req-definitive") is None
        assert adapter.state("public-outbox")["content"] == ""

        intent = _intent_for(client, "req-definitive")
        assert intent is not None, _execution_intents(client)
        assert _intent_state(intent) == "failed", intent


def test_successful_external_write_replays_without_reexecuting(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    content = (
        "Replay test message about community garden watering "
        "schedules and seed packet labels for spring planting."
    )
    with _make_client(tmp_path, adapter) as client:
        writer = _create_run(client, "public", 0)

        first = _tool_write(client, writer["token"], content,
                            request_id="req-replay")
        first_body = _body(first)
        assert first.status_code == 200, first.text
        assert first_body.get("decision") == "allowed", first_body

        backend_after_first = _backend_receipts_for_request(adapter, "req-replay")
        assert len(backend_after_first) == 1
        versions_after_first = _outbox_version_count(client)
        gateway_after_first = _gateway_receipt_count(client, writer["run_id"])

        second = _tool_write(client, writer["token"], content,
                             request_id="req-replay")
        second_body = _body(second)
        assert second.status_code == 200, second.text
        assert second_body.get("decision") == "allowed", second_body
        assert second_body.get("replayed") is True, second_body
        assert second_body.get("request_id") == "req-replay", second_body

        assert len(_backend_receipts_for_request(adapter, "req-replay")) == 1
        assert _outbox_version_count(client) == versions_after_first
        assert _gateway_receipt_count(client, writer["run_id"]) == gateway_after_first
        assert adapter.state("public-outbox")["content"] == content


def test_execution_intents_requires_admin(tmp_path):
    adapter = _ToolClientAdapter(tmp_path)
    with _make_client(tmp_path, adapter) as client:
        resp = client.get("/execution-intents")
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        resp = client.get(
            "/execution-intents", headers={"X-Admin-Key": "not-the-admin-key"}
        )
        assert resp.status_code == 401, resp.text
        assert _policy_id(resp) == "admin-required", resp.text

        resp = client.get("/execution-intents", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
