#!/usr/bin/env python3
"""FlowGate flagship: multi-tenant assistant with clean-context spawning.

One-command orchestrator over external processes only. It starts the
protected file-docs backend and the gateway as subprocesses, then drives
the whole story over HTTP with ``flowgate_client``:

1. an Alpha assistant reads its private tenant file-doc,
2. creates a draft artifact (the server assigns the inherited label),
3. is blocked ``deny-write-down`` when it tries to publish, leaving the
   published file untouched and unreceipted,
4. a Beta assistant is blocked reading Alpha's brief,
5. a controller-spawned **clean** child publishes an innocent public
   notice (a clean context can speak, it just knows nothing),
6. a controller-spawned **continuing** child cannot — a fresh token
   does not launder provenance,
7. the execution-intent trace proves the step-3 write never committed.

No LLM is involved anywhere: drafting is deterministic in this script,
and tenant brief text is never sent to any model.

Usage (from the repo root):
    python scripts/tenant_assistant_demo.py [--keep-running]

Exit codes: 0 = every step passed; 1 = a step failed; 2 = a server
failed to start.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from flowgate_client import GatewayClient  # noqa: E402

ADMIN_KEY = "flowgate-dev-admin"
TOOL_KEY = "tenant-demo-tool-key"
BACKEND_DIR = _REPO_ROOT / "backend"
FIXTURES_DIR = BACKEND_DIR / "file_docs_fixtures"
EXTERNAL_RESOURCES = "tenant-alpha-brief,tenant-beta-brief,published-releases,public-outbox"

PUBLIC_NOTICE = "Project Alpha customer release notes are now available on request."
# Deterministic in-script drafting; the brief text itself is never used
# to compose this, and no model ever sees either text.
INTERNAL_DRAFT = (
    "Internal Alpha release draft: prepare a concise tenant rollout "
    "summary for internal review by Friday, pending leadership "
    "approval before any external circulation."
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return body.get("status") == "ok"
    except Exception:
        return False


def _wait_healthy(procs: dict, urls: dict, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for name, proc in procs.items():
            if proc.poll() is not None:
                print(f"FAIL startup: {name} exited early (code {proc.returncode})")
                return False
        if all(_health_ok(url) for url in urls.values()):
            return True
        time.sleep(0.25)
    return False


def _terminate(procs: dict) -> None:
    for proc in procs.values():
        if proc.poll() is None:
            proc.terminate()
    for proc in procs.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FlowGate multi-tenant assistant demo (clean-context spawning)"
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave both servers running and print their URLs",
    )
    args = parser.parse_args(argv)

    work_dir = Path(tempfile.mkdtemp(prefix="flowgate-tenant-demo-"))
    docs_root = work_dir / "file_docs"
    file_port = _free_port()
    gateway_port = _free_port()
    file_docs_url = f"http://127.0.0.1:{file_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"

    procs: dict[str, subprocess.Popen] = {}
    log_handles = []
    results: list[bool] = []

    def check(step: int, description: str, ok: bool, detail: str = "") -> bool:
        results.append(bool(ok))
        verdict = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"Step {step}: {verdict}: {description}{suffix}")
        return bool(ok)

    try:
        # --- Fixtures + server startup (external processes only) --------
        if not FIXTURES_DIR.is_dir():
            print(f"FAIL startup: fixtures not found at {FIXTURES_DIR}")
            return 2
        shutil.copytree(FIXTURES_DIR, docs_root)

        base_env = dict(os.environ)

        file_env = dict(base_env)
        file_env["FLOWGATE_FILE_DOCS_ROOT"] = str(docs_root)
        file_env["FLOWGATE_TOOL_SERVICE_KEY"] = TOOL_KEY

        gateway_env = dict(base_env)
        gateway_env["FLOWGATE_DEMO_MODE"] = "1"
        gateway_env["GATEWAY_DB"] = str(work_dir / "gw.db")
        gateway_env["FLOWGATE_TOOL_SERVICE_URL"] = f"http://localhost:{file_port}"
        gateway_env["FLOWGATE_TOOL_SERVICE_KEY"] = TOOL_KEY
        gateway_env["FLOWGATE_EXTERNAL_RESOURCES"] = EXTERNAL_RESOURCES

        for name, module, port, env in (
            ("file-docs", "file_docs_service:app", file_port, file_env),
            ("gateway", "app.main:app", gateway_port, gateway_env),
        ):
            log_handle = open(work_dir / f"{name}.log", "ab")  # noqa: SIM115
            log_handles.append(log_handle)
            procs[name] = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", module,
                    "--host", "127.0.0.1", "--port", str(port),
                ],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )

        urls = {"file-docs": file_docs_url, "gateway": gateway_url}
        if not _wait_healthy(procs, urls, timeout=30.0):
            print(
                "FAIL startup: servers did not become healthy within 30s; "
                f"logs in {work_dir}"
            )
            return 2
        print(f"file-docs backend: {file_docs_url}")
        print(f"gateway:           {gateway_url}")

        published_path = docs_root / "published-releases.txt"
        brief_path = docs_root / "tenant-alpha-brief.txt"

        def _published_text() -> str:
            return published_path.read_text(encoding="utf-8") if published_path.exists() else ""

        def _write_receipts(client: GatewayClient, run_id: str) -> list:
            payload = client.tool_receipts(
                run_id=run_id, resource_id="published-releases"
            )
            return [
                r for r in payload.get("receipts", [])
                if r.get("operation") == "write"
            ]

        # --- The flagship story, over HTTP only ---------------------------
        with GatewayClient(gateway_url, admin_key=ADMIN_KEY) as client:
            # 1. Alpha reads its private, file-backed tenant brief.
            alpha = client.create_run("alpha", 2, tenant="alpha")
            alpha_id, alpha_token = alpha["run_id"], alpha["token"]
            try:
                status, body = client.tool_call(
                    alpha_token, "shared_doc", "read", "tenant-alpha-brief"
                )
                brief_text = (
                    brief_path.read_text(encoding="utf-8").strip()
                    if brief_path.exists() else ""
                )
                result = body.get("result") or ""
                ok = (
                    status == 200
                    and body.get("decision") == "allowed"
                    and bool(brief_text)
                    and brief_text in result
                )
                detail = f"policy_id={body.get('policy_id')}"
            except Exception as exc:  # verdict line only, never the brief
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(1, "alpha assistant reads tenant-alpha-brief (file-backed)", ok, detail)

            # 2. Alpha creates a draft; the server assigns the label.
            try:
                status, body = client.create_artifact(
                    alpha_token, "tenant-alpha-release-draft", "doc", INTERNAL_DRAFT
                )
                ok = (
                    status == 201
                    and body.get("sensitivity") == 2
                    and list(body.get("compartments") or []) == ["alpha"]
                )
                detail = (
                    f"label=({body.get('sensitivity')}, "
                    f"{body.get('compartments')}) version={body.get('version')}"
                )
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(2, "alpha assistant creates inherited-label draft artifact", ok, detail)

            # 3. Alpha tries to publish its draft: flow blocks it.
            before_publish = _published_text()
            try:
                status, body = client.tool_call(
                    alpha_token, "shared_doc", "write",
                    "published-releases", INTERNAL_DRAFT,
                )
                receipts = _write_receipts(client, alpha_id)
                ok = (
                    status == 403
                    and body.get("decision") == "blocked"
                    and body.get("policy_id") == "deny-write-down"
                    and _published_text() == before_publish
                    and not receipts
                )
                detail = (
                    f"policy_id={body.get('policy_id')}, file unchanged, "
                    f"write receipts={len(receipts)}"
                )
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(3, "alpha publish blocked deny-write-down, file untouched, no receipt", ok, detail)

            # 4. Beta cannot read Alpha's brief across the tenant boundary.
            beta = client.create_run("beta", 2, tenant="beta")
            try:
                status, body = client.tool_call(
                    beta["token"], "shared_doc", "read", "tenant-alpha-brief"
                )
                ok = (
                    status == 403
                    and body.get("decision") == "blocked"
                    and body.get("policy_id") in ("deny-cross-project", "deny-compartment")
                )
                detail = f"policy_id={body.get('policy_id')}"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(4, "beta assistant cross-tenant read blocked", ok, detail)

            # 5. A controller-spawned CLEAN child publishes the notice.
            clean = client.create_run(
                "alpha", 2, tenant="alpha",
                parent_run_id=alpha_id, spawn_mode="clean",
            )
            try:
                status, body = client.tool_call(
                    clean["token"], "shared_doc", "write",
                    "published-releases", PUBLIC_NOTICE,
                )
                receipts = _write_receipts(client, clean["run_id"])
                ok = (
                    status == 200
                    and body.get("decision") == "allowed"
                    and PUBLIC_NOTICE in _published_text()
                    and bool(receipts)
                    and clean.get("effective_level") == 0
                    and list(clean.get("effective_compartments") or []) == []
                )
                detail = (
                    f"policy_id={body.get('policy_id')}, "
                    f"write receipts={len(receipts)}, clean label=(0, [])"
                )
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(5, "clean child publishes innocent public notice", ok, detail)

            # 6. A CONTINUING child makes the identical write: blocked.
            after_clean = _published_text()
            continuing = client.create_run(
                "alpha", 2, tenant="alpha",
                parent_run_id=alpha_id, spawn_mode="continuing",
            )
            try:
                status, body = client.tool_call(
                    continuing["token"], "shared_doc", "write",
                    "published-releases", PUBLIC_NOTICE,
                )
                ok = (
                    status == 403
                    and body.get("decision") == "blocked"
                    and body.get("policy_id") == "deny-write-down"
                    and _published_text() == after_clean
                    and continuing.get("effective_level") == 2
                    and list(continuing.get("effective_compartments") or []) == ["alpha"]
                )
                detail = (
                    f"policy_id={body.get('policy_id')}, file unchanged, "
                    "continuing label=(2, [alpha])"
                )
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(6, "continuing child cannot publish (fresh token keeps provenance)", ok, detail)

            # 7. Trace: no committed intent exists for the step-3 denial.
            try:
                payload = client.execution_intents()
                intents = payload.get("intents") or []
                committed_for_denied = [
                    i for i in intents
                    if i.get("run_id") == alpha_id
                    and i.get("resource_id") == "published-releases"
                    and i.get("state") == "committed"
                ]
                ok = not committed_for_denied
                detail = (
                    f"intents={len(intents)}, "
                    f"committed for step-3 denied write={len(committed_for_denied)}"
                )
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check(7, "trace: no committed intent for the denied step-3 write", ok, detail)

        passed = sum(1 for r in results if r)
        overall = passed == 7
        print(f"RESULT: {'PASS' if overall else 'FAIL'} ({passed}/7 steps passed)")
        if args.keep_running:
            print("Leaving servers running (--keep-running):")
            print(f"  file-docs backend: {file_docs_url}  (docs root: {docs_root})")
            print(f"  gateway:           {gateway_url}  (db: {work_dir / 'gw.db'})")
            print(f"  server logs:       {work_dir}")
            # Detach: the finally block must not tear the servers down.
            procs.clear()
        return 0 if overall else 1
    finally:
        if procs:
            _terminate(procs)
        for handle in log_handles:
            try:
                handle.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
