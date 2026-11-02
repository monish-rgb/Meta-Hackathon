"""FlowGate external client (httpx-based).

Extracted from ``scripts/agent_demo.py`` so scripts and other external
callers share one client implementation. This package is a pure HTTP
client: it never imports backend code and it changes no gateway
semantics. The admin key (when supplied) is used only for admin-gated
calls such as run creation and receipt/intent listing; every tool call
and artifact creation carries only the run bearer token.
"""

from __future__ import annotations

from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only without httpx
    httpx = None  # type: ignore[assignment]


class FlowGateClientError(RuntimeError):
    """A gateway client call failed (or httpx is not installed)."""


# Resources served by the kv tool; everything else goes via shared_doc.
# This mirrors the mapping the agent demo has always used.
_KV_RESOURCES = {"public-outbox", "shared-board", "shared-scratch", "challenge-outbox"}


def _tool_name_for(resource_id: str) -> str:
    return "shared_kv" if resource_id in _KV_RESOURCES else "shared_doc"


class _SyncFromAsyncTransport(httpx.BaseTransport if httpx is not None else object):  # type: ignore[misc]
    """Adapt an async-only transport (httpx.ASGITransport) for the
    synchronous client, so tests can drive an ASGI app in-process."""

    def __init__(self, async_transport: Any) -> None:
        self._async_transport = async_transport

    def handle_request(self, request):  # type: ignore[override]
        import anyio

        content = request.read()

        async def _send():
            async with httpx.AsyncClient(
                transport=self._async_transport, timeout=15.0
            ) as async_client:
                resp = await async_client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=content,
                )
                return resp.status_code, resp.headers, resp.content

        status, headers, body = anyio.run(_send)
        return httpx.Response(
            status_code=status, headers=headers, content=body, request=request
        )

    def close(self) -> None:  # sync transports close best-effort only
        pass


class GatewayClient:
    """Minimal external gateway client (httpx).

    ``transport`` may be an ``httpx`` transport (for tests driving an
    ASGI app in-process); otherwise a normal network client is used.
    """

    def __init__(
        self,
        base_url: str,
        admin_key: str | None = None,
        transport: Any = None,
        timeout: float = 15.0,
    ) -> None:
        if httpx is None:  # pragma: no cover
            raise FlowGateClientError("httpx is not installed")
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        if transport is not None and not hasattr(transport, "handle_request"):
            # Async-only transport (e.g. httpx.ASGITransport in tests):
            # adapt it so the synchronous client can use it.
            transport = _SyncFromAsyncTransport(transport)
        self._client = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            # Some in-process transports (ASGI test transports) have no
            # synchronous close; closing is best-effort cleanup only.
            pass

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internal helpers -------------------------------------------------

    def _admin_headers(self) -> dict:
        return {"X-Admin-Key": self.admin_key} if self.admin_key else {}

    @staticmethod
    def _payload(resp) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    # -- runs -------------------------------------------------------------

    def create_run(
        self,
        project: str,
        clearance: int,
        tenant: str | None = None,
        parent_run_id: str | None = None,
        spawn_mode: str | None = None,
        effective_level: int | None = None,
        effective_compartments: list | None = None,
    ) -> dict:
        """Create a run (admin key only here; tool calls use the token).

        Optional fields are sent only when not ``None`` so the server's
        own defaults — and, for spawns, its server-side label rules —
        apply exactly as if the caller had omitted them. Spawn semantics
        are the server's: ``continuing`` inherits the parent's current
        effective label, ``clean`` starts at ``(0, [])``.
        """
        body: dict[str, Any] = {"project": project, "clearance": clearance}
        if tenant is not None:
            body["tenant"] = tenant
        if parent_run_id is not None:
            body["parent_run_id"] = parent_run_id
        if spawn_mode is not None:
            body["spawn_mode"] = spawn_mode
        if effective_level is not None:
            body["effective_level"] = effective_level
        if effective_compartments is not None:
            body["effective_compartments"] = list(effective_compartments)
        resp = self._client.post("/runs", json=body, headers=self._admin_headers())
        if resp.status_code != 201:
            raise FlowGateClientError(
                f"run creation failed: HTTP {resp.status_code} {resp.text}"
            )
        return resp.json()

    # -- tool calls ---------------------------------------------------------

    def tool_call(
        self,
        token: str,
        tool_name: str,
        operation: str,
        resource_id: str,
        content: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, dict]:
        body: dict[str, Any] = {"operation": operation, "resource_id": resource_id}
        if content is not None:
            body["content"] = content
        if request_id is not None:
            body["request_id"] = request_id
        resp = self._client.post(
            f"/tools/{tool_name}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.status_code, self._payload(resp)

    def gateway_tool(
        self,
        token: str,
        operation: str,
        resource_id: str,
        content: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, dict]:
        """Execute one gateway_tool proposal via the matching real tool."""
        return self.tool_call(
            token,
            _tool_name_for(resource_id),
            operation,
            resource_id,
            content,
            request_id=request_id,
        )

    # -- artifacts ----------------------------------------------------------

    def create_artifact(
        self, token: str, resource_id: str, kind: str, content: str
    ) -> tuple[int, dict]:
        """Create a versioned artifact (run token; server assigns the label).

        Returns ``(status, body)`` like :meth:`tool_call`, so callers can
        assert on denied creations (e.g. 403/409) without exceptions.
        """
        resp = self._client.post(
            "/artifacts",
            json={"resource_id": resource_id, "kind": kind, "content": content},
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.status_code, self._payload(resp)

    # -- evidence (admin) ---------------------------------------------------

    def tool_receipts(
        self, run_id: str | None = None, resource_id: str | None = None
    ) -> dict:
        """List gateway execution receipts (admin key required)."""
        params: dict[str, Any] = {}
        if run_id is not None:
            params["run_id"] = run_id
        if resource_id is not None:
            params["resource_id"] = resource_id
        resp = self._client.get(
            "/tool-receipts", params=params, headers=self._admin_headers()
        )
        if resp.status_code != 200:
            raise FlowGateClientError(
                f"tool receipts failed: HTTP {resp.status_code} {resp.text}"
            )
        payload = self._payload(resp)
        return payload

    def execution_intents(self) -> dict:
        """List durable external-execution intents (admin key required)."""
        resp = self._client.get(
            "/execution-intents", headers=self._admin_headers()
        )
        if resp.status_code != 200:
            raise FlowGateClientError(
                f"execution intents failed: HTTP {resp.status_code} {resp.text}"
            )
        return self._payload(resp)
