#!/usr/bin/env python3
"""FlowGate demo CLI — drives the 8-step twin scenario against a live backend.

External HTTP client only: never imports backend code. Uses httpx when
available, otherwise falls back to urllib from the standard library.

Admin operations (POST /demo/reset, POST /runs) carry the X-Admin-Key header;
tool calls carry only the run bearer token.

Usage:
    python scripts/demo.py [--base-url http://localhost:8000]
                           [--admin-key <key>]

The admin key resolves as: --admin-key, then $FLOWGATE_ADMIN_KEY. There is
no built-in fallback: against a backend in demo mode
(FLOWGATE_DEMO_MODE=1) pass --admin-key flowgate-dev-admin explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import httpx  # type: ignore

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover - fallback path
    _HAVE_HTTPX = False

if not _HAVE_HTTPX:
    import urllib.error
    import urllib.request

# Public draft text: original, public vocabulary, unrelated to the roadmap.
PUBLIC_DRAFT_TEXT = (
    "Public community draft: the Saturday park cleanup starts at nine near "
    "the elm trees by the north gate. Volunteers bring gloves, lemonade, "
    "folding chairs, and sketchbooks for the afternoon picnic on the lawn. "
    "Everyone is welcome to join the neighbourhood gardening rota."
)

# Short restricted paraphrase: mid-March release, ~four million funding,
# phased rollout. Deliberately no canary token.
RESTRICTED_PARAPHRASE = (
    "Project Nova brief: the release is planned for mid-March, backed by "
    "roughly four million in funding, with a phased rollout to partner "
    "regions in successive stages."
)


class GatewayClient:
    """Minimal JSON HTTP client (httpx if available, else urllib)."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout) if _HAVE_HTTPX else None

    def _headers(self, token: str | None = None, admin_key: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if admin_key:
            headers["X-Admin-Key"] = admin_key
        return headers

    @staticmethod
    def _decode(status: int, payload: str):
        try:
            return status, json.loads(payload or "{}")
        except (json.JSONDecodeError, ValueError):
            return status, {}

    def post(
        self,
        path: str,
        body: dict,
        token: str | None = None,
        admin_key: str | None = None,
    ):
        url = self.base_url + path
        headers = self._headers(token=token, admin_key=admin_key)
        if _HAVE_HTTPX:
            resp = self._client.post(url, json=body, headers=headers)
            return self._decode(resp.status_code, resp.text)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return self._decode(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            return self._decode(exc.code, exc.read().decode())

    def get(self, path: str, token: str | None = None, admin_key: str | None = None):
        url = self.base_url + path
        headers = self._headers(token=token, admin_key=admin_key)
        if _HAVE_HTTPX:
            resp = self._client.get(url, headers=headers)
            return self._decode(resp.status_code, resp.text)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return self._decode(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            return self._decode(exc.code, exc.read().decode())

    def get_with_tool_key(self, path: str, tool_key: str):
        """GET against the protected tool backend with its service key."""
        url = self.base_url + path
        headers = {"X-Tool-Key": tool_key}
        if _HAVE_HTTPX:
            resp = self._client.get(url, headers=headers)
            return self._decode(resp.status_code, resp.text)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return self._decode(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            return self._decode(exc.code, exc.read().decode())

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def get_run(api: GatewayClient, run_id: str, admin_key: str) -> dict | None:
    status, runs = api.get("/runs", admin_key=admin_key)
    if status != 200 or not isinstance(runs, list):
        return None
    for run in runs:
        if isinstance(run, dict) and run.get("run_id") == run_id:
            return run
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="FlowGate twin-scenario demo client")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--admin-key",
        default=None,
        help="Admin key (default: $FLOWGATE_ADMIN_KEY; required)",
    )
    parser.add_argument(
        "--tool-service-url",
        default=None,
        help="Protected tool backend URL (default: $FLOWGATE_TOOL_SERVICE_URL); "
             "when given, step 10 verifies its independent receipts/state",
    )
    parser.add_argument(
        "--tool-service-key",
        default=None,
        help="Tool service key (default: $FLOWGATE_TOOL_SERVICE_KEY)",
    )
    args = parser.parse_args()
    admin_key = args.admin_key or os.environ.get("FLOWGATE_ADMIN_KEY")
    if not admin_key:
        print(
            "No admin key: pass --admin-key or set FLOWGATE_ADMIN_KEY "
            "(for a demo-mode backend: --admin-key flowgate-dev-admin)"
        )
        return 2

    api = GatewayClient(args.base_url)
    results: list[tuple[int, str, bool, str]] = []

    def check(step: int, label: str, ok: bool, detail: str) -> None:
        results.append((step, label, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"Step {step} [{label}]: {mark} — {detail}", flush=True)

    def tool(token: str, tool_name: str, operation: str, resource_id: str, content=None):
        body: dict = {"operation": operation, "resource_id": resource_id}
        if content is not None:
            body["content"] = content
        # Tool calls use ONLY the run bearer token (never the admin key).
        return api.post(f"/tools/{tool_name}", body, token=token)

    def register(project: str, clearance: int) -> dict | None:
        status, run = api.post(
            "/runs",
            {"project": project, "clearance": clearance},
            admin_key=admin_key,
        )
        if status not in (200, 201) or not isinstance(run, dict) or "token" not in run:
            print(f"Failed to register run ({project}, {clearance}): HTTP {status} {run}")
            return None
        return run

    try:
        # Reset (admin) for a clean slate.
        status, body = api.post("/demo/reset", {}, admin_key=admin_key)
        if status != 200:
            print(f"Failed to reset demo state: HTTP {status} {body}")
            return 1

        # Create the twins: public pair (public, 0) and alpha pair (alpha, 2).
        a_public = register("public", 0)
        b_public = register("public", 0)
        a_alpha = register("alpha", 2)
        b_alpha = register("alpha", 2)
        if not all((a_public, b_public, a_alpha, b_alpha)):
            return 1
        a_public_tok, b_public_tok = a_public["token"], b_public["token"]
        a_alpha_tok, b_alpha_tok = a_alpha["token"], b_alpha["token"]
        b_alpha_id = b_alpha["run_id"]
        b_public_id = b_public["run_id"]
        print(
            "Registered A_public, B_public (public, 0) and "
            "A_alpha, B_alpha (alpha, 2)"
        )

        # Step 1: A_public reads public-source -> allowed.
        status, body = tool(a_public_tok, "shared_doc", "read", "public-source")
        source_content = body.get("result") if isinstance(body, dict) else None
        ok = status == 200 and body.get("decision") == "allowed" and bool(source_content)
        check(
            1,
            "A_public read public-source",
            ok,
            f"HTTP {status}, decision={body.get('decision')}",
        )

        # Step 2: A_public writes the public draft text to public-draft -> allowed.
        status, body = tool(
            a_public_tok, "shared_doc", "write", "public-draft", PUBLIC_DRAFT_TEXT
        )
        ok = status == 200 and body.get("decision") == "allowed"
        check(
            2,
            "A_public write public draft to public-draft",
            ok,
            f"HTTP {status}, decision={body.get('decision')}, "
            f"policy_id={body.get('policy_id')}",
        )

        # Step 3: B_public reads public-draft -> allowed.
        status, body = tool(b_public_tok, "shared_doc", "read", "public-draft")
        draft_content = body.get("result") if isinstance(body, dict) else None
        ok = (
            status == 200
            and body.get("decision") == "allowed"
            and draft_content == PUBLIC_DRAFT_TEXT
        )
        check(
            3,
            "B_public read public-draft",
            ok,
            f"HTTP {status}, decision={body.get('decision')}",
        )

        # Step 4: B_public writes the content it read to public-outbox -> allowed.
        status, body = tool(
            b_public_tok,
            "shared_kv",
            "write",
            "public-outbox",
            draft_content if draft_content is not None else PUBLIC_DRAFT_TEXT,
        )
        ok = status == 200 and body.get("decision") == "allowed"
        check(
            4,
            "B_public write public draft to public-outbox",
            ok,
            f"HTTP {status}, decision={body.get('decision')}, "
            f"policy_id={body.get('policy_id')}",
        )

        # Step 5: A_alpha reads alpha-roadmap -> allowed.
        status, body = tool(a_alpha_tok, "shared_doc", "read", "alpha-roadmap")
        roadmap = body.get("result") if isinstance(body, dict) else None
        ok = status == 200 and body.get("decision") == "allowed" and bool(roadmap)
        check(
            5,
            "A_alpha read alpha-roadmap",
            ok,
            f"HTTP {status}, decision={body.get('decision')}",
        )

        # Step 6: A_alpha creates a NEW artifact; the server assigns the
        # inherited label (2, [alpha]) from A_alpha's effective label.
        status, body = api.post(
            "/artifacts",
            {"resource_id": "alpha-plan-draft", "kind": "doc",
             "content": RESTRICTED_PARAPHRASE},
            token=a_alpha_tok,
        )
        ok = (
            status == 201
            and body.get("sensitivity") == 2
            and body.get("compartments") == ["alpha"]
            and body.get("version") == 1
        )
        check(
            6,
            "A_alpha create artifact alpha-plan-draft (inherited label)",
            ok,
            f"HTTP {status}, label={body.get('sensitivity')}/{body.get('compartments')}, "
            f"version={body.get('version')}",
        )

        # Step 7: B_alpha reads alpha-plan-draft -> allowed.
        status, body = tool(b_alpha_tok, "shared_doc", "read", "alpha-plan-draft")
        restricted_content = body.get("result") if isinstance(body, dict) else None
        ok = (
            status == 200
            and body.get("decision") == "allowed"
            and restricted_content == RESTRICTED_PARAPHRASE
        )
        check(
            7,
            "B_alpha read alpha-plan-draft",
            ok,
            f"HTTP {status}, decision={body.get('decision')}",
        )

        # Step 8: B_alpha tries to publish that content to public-outbox ->
        # blocked with deny-write-down, B_alpha NOT suspended, and the
        # public-outbox still holds the public text from step 4.
        status, body = tool(
            b_alpha_tok,
            "shared_kv",
            "write",
            "public-outbox",
            restricted_content if restricted_content is not None else RESTRICTED_PARAPHRASE,
        )
        b_alpha_run = get_run(api, b_alpha_id, admin_key)
        not_suspended = b_alpha_run is not None and b_alpha_run.get("suspended") is False
        status_r, body_r = tool(b_public_tok, "shared_kv", "read", "public-outbox")
        outbox_unchanged = (
            status_r == 200
            and body_r.get("decision") == "allowed"
            and body_r.get("result") == PUBLIC_DRAFT_TEXT
        )
        ok = (
            status == 403
            and body.get("decision") == "blocked"
            and body.get("policy_id") == "deny-write-down"
            and not_suspended
            and outbox_unchanged
        )
        check(
            8,
            "B_alpha publish to public-outbox blocked (deny-write-down)",
            ok,
            f"HTTP {status}, decision={body.get('decision')}, "
            f"policy_id={body.get('policy_id')}, "
            f"B_alpha suspended={b_alpha_run.get('suspended') if b_alpha_run else 'unknown'}, "
            f"public-outbox unchanged={outbox_unchanged}",
        )

        # Step 9 (external verification): the denied write in step 8 must
        # have produced NO tool execution receipt, while the allowed
        # public-outbox write in step 4 produced one.
        status, receipts_body = api.get(
            "/tool-receipts", admin_key=admin_key
        )
        receipts = (
            receipts_body.get("receipts", [])
            if isinstance(receipts_body, dict)
            else []
        )
        denied_receipts = [
            r for r in receipts
            if r.get("run_id") == b_alpha_id
            and r.get("operation") == "write"
            and r.get("resource_id") == "public-outbox"
        ]
        allowed_receipts = [
            r for r in receipts
            if r.get("operation") == "write"
            and r.get("resource_id") == "public-outbox"
        ]
        ok = status == 200 and not denied_receipts and bool(allowed_receipts)
        check(
            9,
            "Tool receipts: denied write never executed, allowed write receipted",
            ok,
            f"denied_write_receipts={len(denied_receipts)}, "
            f"allowed_outbox_write_receipts={len(allowed_receipts)}",
        )

        # Step 10 (only with --tool-service-url): independently verify
        # the SEPARATE tool backend — its own receipt store must show
        # the allowed publication and no trace of the denied one, and
        # its own state must hold only the public content.
        tool_url = args.tool_service_url or os.environ.get("FLOWGATE_TOOL_SERVICE_URL")
        tool_key = args.tool_service_key or os.environ.get("FLOWGATE_TOOL_SERVICE_KEY")
        if tool_url and tool_key:
            tool_api = GatewayClient(tool_url)
            try:
                s_denied, denied_body = tool_api.get_with_tool_key(
                    f"/receipts?run_id={b_alpha_id}&resource_id=public-outbox",
                    tool_key,
                )
                s_allowed, allowed_body = tool_api.get_with_tool_key(
                    f"/receipts?run_id={b_public_id}&resource_id=public-outbox",
                    tool_key,
                )
                s_state, state_body = tool_api.get_with_tool_key(
                    "/state/public-outbox", tool_key
                )
                denied_writes = [
                    r for r in (denied_body.get("receipts", [])
                                if isinstance(denied_body, dict) else [])
                    if r.get("operation") == "write"
                ]
                allowed_writes = [
                    r for r in (allowed_body.get("receipts", [])
                                if isinstance(allowed_body, dict) else [])
                    if r.get("operation") == "write"
                ]
                ok = (
                    s_denied == 200 and s_allowed == 200 and s_state == 200
                    and not denied_writes
                    and bool(allowed_writes)
                    and isinstance(state_body, dict)
                    and state_body.get("content") == PUBLIC_DRAFT_TEXT
                )
                check(
                    10,
                    "Separate tool backend: denied write absent, allowed write executed",
                    ok,
                    f"tool_denied_writes={len(denied_writes)}, "
                    f"tool_allowed_writes={len(allowed_writes)}, "
                    f"tool_state_matches_public={isinstance(state_body, dict) and state_body.get('content') == PUBLIC_DRAFT_TEXT}",
                )
            finally:
                tool_api.close()
    except Exception as exc:  # connection errors etc.
        print(f"Demo failed with error: {exc}")
        return 1
    finally:
        api.close()

    failed = [step for step, _, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
