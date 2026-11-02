"""Tool-backend outcome classification tests (CONTRACT Tier 4).

``ToolServiceClient`` must never treat a backend 5xx as proof that an
execution did not happen: 5xx is ambiguous (the write may have
committed), other 4xx refusals are definitive non-executions, and 409
is the request-id-reuse binding error.  Likewise, a ``pending``
file-docs receipt is not proof of execution, so ``receipt_for_request``
skips it; only a ``committed`` receipt reconciles a retry.

``ToolServiceClient`` calls the module-level ``httpx.post``/``httpx.get``
helpers, so these tests bridge an ``httpx.MockTransport`` through those
helpers (patching the transport, not the client logic) and run fully
offline.

Run context is backend/; a sys.path fallback keeps pytest working from
the workspace root.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.tools import (  # noqa: E402
    ToolBackendAmbiguous,
    ToolBackendError,
    ToolBackendNotExecuted,
    ToolServiceClient,
)


def _client_with_transport(monkeypatch, handler) -> ToolServiceClient:
    """A ToolServiceClient whose httpx.post/get run on a MockTransport."""
    transport = httpx.MockTransport(handler)

    def fake_post(url, json=None, headers=None, timeout=None):
        request = httpx.Request("POST", url, json=json, headers=headers)
        return transport.handle_request(request)

    def fake_get(url, params=None, headers=None, timeout=None):
        request = httpx.Request("GET", url, params=params, headers=headers)
        return transport.handle_request(request)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    return ToolServiceClient("http://tool-backend.test", "test-tool-key")


def _status_handler(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, json={"detail": "backend response"}, request=request
        )

    return handler


def _execute(client: ToolServiceClient):
    return client.execute(
        "run-1",
        "write",
        "public-outbox",
        content="Classification probe message about gardening tools.",
        request_id="req-classification",
        payload_hash="payload-hash-1",
    )


@pytest.mark.parametrize("status_code", [500, 503])
def test_execute_5xx_is_ambiguous(monkeypatch, status_code):
    client = _client_with_transport(monkeypatch, _status_handler(status_code))
    with pytest.raises(ToolBackendAmbiguous) as excinfo:
        _execute(client)
    # An ambiguous outcome must not be misread as definitive either way.
    assert not isinstance(excinfo.value, ToolBackendNotExecuted)


def test_execute_400_is_not_executed(monkeypatch):
    client = _client_with_transport(monkeypatch, _status_handler(400))
    with pytest.raises(ToolBackendNotExecuted) as excinfo:
        _execute(client)
    assert not isinstance(excinfo.value, ToolBackendAmbiguous)


def test_execute_409_is_request_id_reuse_error(monkeypatch):
    client = _client_with_transport(monkeypatch, _status_handler(409))
    with pytest.raises(ToolBackendError) as excinfo:
        _execute(client)
    # Exactly the base binding error, not an outcome classification.
    assert type(excinfo.value) is ToolBackendError
    assert "request-id-reuse" in str(excinfo.value)


def test_receipt_for_request_skips_pending_receipts(monkeypatch):
    pending = {
        "receipt_id": 11,
        "request_id": "req-classification",
        "resource_id": "public-outbox",
        "operation": "write",
        "state": "pending",
    }
    committed = dict(pending, receipt_id=12, state="committed")
    served = {"receipts": [pending]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/receipts"
        receipts = served["receipts"]
        return httpx.Response(
            200,
            json={"count": len(receipts), "receipts": receipts},
            request=request,
        )

    client = _client_with_transport(monkeypatch, handler)

    # A pending receipt alone is not proof of execution.
    assert client.receipt_for_request("req-classification") is None

    # Once a committed receipt is present it is returned (the pending
    # entry ahead of it is still skipped, not mistaken for proof).
    served["receipts"] = [pending, committed]
    receipt = client.receipt_for_request("req-classification")
    assert receipt is not None
    assert receipt["receipt_id"] == committed["receipt_id"]
    assert receipt["state"] == "committed"
