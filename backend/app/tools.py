"""FlowGate shared tools. Assumes the caller already authorised the call."""

import httpx

from .store import set_resource_content

TOOL_KIND = {"shared_doc": "doc", "shared_kv": "kv"}


class ToolServiceClient:
    """Client for the separately protected tool backend.

    The gateway holds the tool service key; agent runs never see it.
    ``execute`` returns (result, service_receipt_id). Failures raise
    RuntimeError so callers fail closed instead of falling back to a
    local, ungated execution path.
    """

    def __init__(self, base_url: str, service_key: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.timeout = timeout

    def execute(self, run_id, operation, resource_id, content=None):
        try:
            resp = httpx.post(
                f"{self.base_url}/execute",
                json={
                    "resource_id": resource_id,
                    "operation": operation,
                    "content": content,
                    "run_id": run_id,
                },
                headers={"X-Tool-Key": self.service_key},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"tool backend unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"tool backend refused execution ({resp.status_code}): {resp.text}"
            )
        body = resp.json()
        return body.get("result"), body.get("receipt_id")

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
