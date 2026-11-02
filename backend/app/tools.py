"""FlowGate shared tools. Assumes the caller already authorised the call."""

import httpx

from .store import set_resource_content

TOOL_KIND = {"shared_doc": "doc", "shared_kv": "kv"}


class ToolBackendError(RuntimeError):
    """Base error for tool-backend execution failures."""


class ToolBackendNotExecuted(ToolBackendError):
    """Execution definitively did not happen on the tool backend."""


class ToolBackendAmbiguous(ToolBackendError):
    """Execution may have committed on the tool backend."""


class ToolServiceClient:
    """Client for the separately protected tool backend.

    The gateway holds the tool service key; agent runs never see it.
    ``execute`` returns (result, service_receipt_id). Failures raise
    ToolBackendError subclasses so callers can distinguish a definitive
    non-execution from an ambiguous outcome instead of falling back to a
    local, ungated execution path.
    """

    def __init__(self, base_url: str, service_key: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.timeout = timeout

    def execute(self, run_id, operation, resource_id, content=None, request_id=None, payload_hash=None):
        payload = {
            "resource_id": resource_id,
            "operation": operation,
            "content": content,
            "run_id": run_id,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        if payload_hash is not None:
            payload["payload_hash"] = payload_hash
        try:
            resp = httpx.post(
                f"{self.base_url}/execute",
                json=payload,
                headers={"X-Tool-Key": self.service_key},
                timeout=self.timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ToolBackendNotExecuted(
                f"tool backend unreachable before dispatch: {exc}"
            ) from exc
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ) as exc:
            raise ToolBackendAmbiguous(
                f"tool backend outcome unknown after dispatch: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolBackendAmbiguous(
                f"tool backend outcome unknown after dispatch: {exc}"
            ) from exc
        if resp.status_code == 409:
            raise ToolBackendError(
                f"tool backend request-id-reuse ({resp.status_code}): {resp.text}"
            )
        if resp.status_code >= 500:
            raise ToolBackendAmbiguous(
                "tool backend outcome unknown: HTTP 5xx may have committed"
            )
        if resp.status_code != 200:
            raise ToolBackendNotExecuted(
                f"tool backend refused execution ({resp.status_code}): {resp.text}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ToolBackendAmbiguous(
                "tool backend outcome unknown: invalid success response"
            ) from exc
        return body.get("result"), body.get("receipt_id")

    def receipt_for_request(self, request_id):
        try:
            resp = httpx.get(
                f"{self.base_url}/receipts",
                params={"request_id": request_id},
                headers={"X-Tool-Key": self.service_key},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ToolBackendAmbiguous(
                f"tool backend receipt lookup outcome unknown: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise ToolBackendAmbiguous(
                f"tool backend receipt lookup failed ({resp.status_code}): {resp.text}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ToolBackendAmbiguous(
                "tool backend receipt lookup outcome unknown: invalid response"
            ) from exc
        if isinstance(body, dict):
            receipts = body.get("receipts") or []
        elif isinstance(body, list):
            receipts = body
        else:
            receipts = []
        # A pending receipt is not proof of execution (file-docs two-phase
        # protocol); receipts without a state field pass through.
        receipts = [
            r
            for r in receipts
            if not (isinstance(r, dict) and r.get("state") == "pending")
        ]
        if not receipts:
            return None
        return receipts[0]

    def state(self, resource_id):
        try:
            resp = httpx.get(
                f"{self.base_url}/state/{resource_id}",
                headers={"X-Tool-Key": self.service_key},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"tool backend unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"tool backend refused state read ({resp.status_code}): {resp.text}"
            )
        return resp.json()


def execute_tool(conn, tool_name, operation, resource, content=None, commit=True):
    """Execute an authorised tool call.

    Read returns the resource content; write persists via the store and
    returns None. Raises ValueError on unknown tool or operation.
    ``commit=False`` lets the caller fold execution into its own
    transaction (mutation + audit event commit or roll back together).
    """
    if tool_name not in TOOL_KIND:
        raise ValueError(f"Unknown tool: {tool_name}")
    if operation not in ("read", "write"):
        raise ValueError(f"Unknown operation: {operation}")
    if operation == "read":
        return resource["content"]
    set_resource_content(conn, resource["resource_id"], content, commit=commit)
    return None
