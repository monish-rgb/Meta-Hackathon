#!/usr/bin/env python3
"""FlowGate real-agent step (Tier 3) — one LLM-proposed, gateway-decided task.

Redaction rule (hard): the LLM step runs the PUBLIC task only — read
``public-source`` and publish a one-line summary to ``public-outbox``.
Restricted content (the alpha roadmap and anything derived from it) is
never placed in a cloud model's context. Resource names to avoid
(``alpha-roadmap`` and alpha resources generally) may be named in the
prompt solely so the planner knows not to touch them; no restricted
text is included anywhere in this script. The alpha climax remains the
deterministic scripted path in the twin-scenario demo.

External client only: this script never imports backend code. The admin
key is used solely to create the run; every tool call carries only the
run bearer token. The model proposes calls through a single
``gateway_tool``; the gateway verdict stands — a blocked call is
terminal, is reported with its policy reason, and is never retried
around the gate.

LLM provider selection (any provider, key supplied at runtime — never
hard-coded and never bundled): the operator supplies the key in the
shell via an environment variable, for example in PowerShell
``$env:OPENAI_API_KEY = "..."`` then ``python scripts/agent_demo.py``.
Recognised variables, in priority order:

- Generic OpenAI-compatible: ``FLOWGATE_AGENT_API_KEY`` (or
  ``LLM_API_KEY``) with optional ``FLOWGATE_AGENT_BASE_URL`` (or
  ``LLM_BASE_URL`` / ``OPENAI_BASE_URL``) and ``FLOWGATE_AGENT_MODEL``.
  CLI flags ``--llm-api-key`` / ``--llm-base-url`` / ``--model``
  override every environment variable, so any OpenAI-compatible
  endpoint (OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Together,
  xAI, Gemini's OpenAI-compatible endpoint, a local server, ...) can be
  used from the command prompt without editing this file.
- Provider keys auto-detected with sensible base URL / model defaults:
  ``OPENAI_API_KEY``, ``OPENROUTER_API_KEY``, ``GROQ_API_KEY``,
  ``DEEPSEEK_API_KEY``, ``MISTRAL_API_KEY``, ``TOGETHER_API_KEY``,
  ``XAI_API_KEY``, ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``.
- Anthropic: ``ANTHROPIC_API_KEY`` (optional ``anthropic`` package,
  native messages API; ``FLOWGATE_AGENT_MODEL`` overrides the model).

The model is always selectable without editing this file, highest
precedence first: ``--model`` flag > ``FLOWGATE_AGENT_MODEL`` env var >
provider-specific env (``GEMINI_MODEL``) > provider default. The
``claude-opus-5-5`` default applies ONLY to the Anthropic planner; the
Gemini path defaults to ``gemini-2.0-flash`` at Google's
OpenAI-compatible endpoint. ``--provider gemini|anthropic|
openai-compatible`` (or ``FLOWGATE_AGENT_PROVIDER``) forces a provider.

Gemini from PowerShell (backend must be running first — see below):

    cd backend; $env:FLOWGATE_DEMO_MODE = "1"
    python -m uvicorn app.main:app --port 8000   # leave running
    # second terminal, from repo root:
    $env:GEMINI_API_KEY = "..."
    $env:FLOWGATE_AGENT_MODEL = "gemini-2.5-flash"   # optional
    python scripts/agent_demo.py --admin-key flowgate-dev-admin
    # equivalent one-off: python scripts/agent_demo.py --provider gemini --model gemini-2.5-flash --admin-key flowgate-dev-admin

With no usable key/package (or with ``--offline``) the script falls
back to the deterministic scripted planner performing the identical
gateway calls, and says so.

Usage:
    python scripts/agent_demo.py [--base-url http://localhost:8000]
                                 [--admin-key <key>] [--offline]
                                 [--provider auto|gemini|anthropic|openai-compatible]
                                 [--llm-api-key <key>]
                                 [--llm-base-url <url>] [--model <id>]

Exit codes: 0 = public publication allowed; 1 = not allowed;
2 = FlowGate backend unreachable (start the backend first — that is a
connection error, not an LLM API-key error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

# The importable client package lives at the repo root, next to scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only without httpx
    httpx = None  # type: ignore[assignment]

from flowgate_client import FlowGateClientError, GatewayClient  # noqa: E402

__all__ = ["GatewayClient"]

# Anthropic-planner default ONLY (never used by Gemini/OpenAI-compatible
# planners, which carry their own provider defaults below).
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_MODEL = DEFAULT_ANTHROPIC_MODEL  # backwards-compatible alias
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

_KV_RESOURCES = {"public-outbox", "shared-board", "shared-scratch", "challenge-outbox"}
# The planner may only ever touch these public resources. Naming them is
# the allowlist; anything else the model proposes is stopped locally
# and never executed, so restricted text can never enter its context.
_PUBLIC_RESOURCES = {"public-source", "public-draft", "public-outbox"}

_SYSTEM_PROMPT = (
    "You are an agent operating through the FlowGate gateway. You may "
    "only use the gateway_tool tool. The gateway may block you: accept "
    "its reason and stop. Never attempt to work around a block, never "
    "ask for labels or credentials, and never try a different resource "
    "to evade a denial. Complete the public task, then stop."
)

_PUBLIC_TASK = (
    "Public task: use gateway_tool to read the resource public-source, "
    "then use gateway_tool to write a one-line summary of what you read "
    "to the resource public-outbox. Do not access alpha-roadmap or any "
    "alpha resource. When the public-outbox write succeeds, stop."
)


class UnavailableError(RuntimeError):
    """The configured LLM planner cannot run (no key/package/endpoint)."""


def _tool_name_for(resource_id: str) -> str:
    return "shared_kv" if resource_id in _KV_RESOURCES else "shared_doc"


def _verdict(operation: str, resource_id: str, status: int, body: dict) -> dict:
    return {
        "operation": operation,
        "resource_id": resource_id,
        "status": status,
        "decision": body.get("decision"),
        "policy_id": body.get("policy_id"),
        "reason": body.get("reason"),
        "result": body.get("result"),
    }


class Planner(Protocol):
    """A planner proposes gateway calls and returns their verdicts."""

    def run(self, client: GatewayClient, token: str) -> list[dict]:
        ...


class ScriptedPlanner:
    """Deterministic planner: read public-source, publish a summary."""

    name = "scripted"

    def __init__(self) -> None:
        self.last_summary: str | None = None

    def run(self, client: GatewayClient, token: str) -> list[dict]:
        verdicts: list[dict] = []
        status, body = client.gateway_tool(token, "read", "public-source")
        verdicts.append(_verdict("read", "public-source", status, body))
        if body.get("decision") != "allowed":
            return verdicts
        source = body.get("result") or ""
        first_sentence = source.split(".")[0].strip()
        summary = f"Open day summary: {first_sentence}." if first_sentence else (
            "Open day summary: community open day at the Riverside centre."
        )
        self.last_summary = summary
        status, body = client.gateway_tool(token, "write", "public-outbox", summary)
        verdicts.append(_verdict("write", "public-outbox", status, body))
        return verdicts


# (env var, default OpenAI-compatible base URL, default model)
_OPENAI_COMPAT_PROVIDERS: list[tuple[str, str, str]] = [
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest"),
    ("TOGETHER_API_KEY", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ("XAI_API_KEY", "https://api.x.ai/v1", "grok-2-latest"),
    ("GEMINI_API_KEY", GEMINI_BASE_URL, DEFAULT_GEMINI_MODEL),
    ("GOOGLE_API_KEY", GEMINI_BASE_URL, DEFAULT_GEMINI_MODEL),
]
_GENERIC_KEY_ENVS = ("FLOWGATE_AGENT_API_KEY", "LLM_API_KEY")
_GENERIC_BASE_ENVS = ("FLOWGATE_AGENT_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL")
# Provider-specific model env vars, consulted after FLOWGATE_AGENT_MODEL.
_PROVIDER_MODEL_ENVS = {
    "GEMINI_API_KEY": ("GEMINI_MODEL", "GOOGLE_MODEL"),
    "GOOGLE_API_KEY": ("GEMINI_MODEL", "GOOGLE_MODEL"),
}


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def detect_openai_compatible_config() -> tuple[str, str, str] | None:
    """(api_key, base_url, default_model) from the environment, or None."""
    generic_key = _first_env(_GENERIC_KEY_ENVS)
    if generic_key:
        return (
            generic_key,
            _first_env(_GENERIC_BASE_ENVS) or "https://api.openai.com/v1",
            os.environ.get("FLOWGATE_AGENT_MODEL") or "gpt-4o-mini",
        )
    for env_name, base_url, model in _OPENAI_COMPAT_PROVIDERS:
        key = os.environ.get(env_name)
        if key:
            provider_model = _first_env(_PROVIDER_MODEL_ENVS.get(env_name, ()))
            return key, base_url, (
                os.environ.get("FLOWGATE_AGENT_MODEL") or provider_model or model
            )
    return None


def resolve_gemini_config(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> tuple[str | None, str, str]:
    """(api_key, base_url, model) for Gemini's OpenAI-compatible endpoint.

    Precedence per field: explicit argument > FLOWGATE_AGENT_MODEL /
    GEMINI_MODEL env > GEMINI_API_KEY/GOOGLE_API_KEY env > default.
    """
    key = (
        api_key
        or _first_env(_GENERIC_KEY_ENVS)
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    resolved_model = (
        model
        or os.environ.get("FLOWGATE_AGENT_MODEL")
        or _first_env(("GEMINI_MODEL", "GOOGLE_MODEL"))
        or DEFAULT_GEMINI_MODEL
    )
    return key, (base_url or GEMINI_BASE_URL).rstrip("/"), resolved_model


class OpenAICompatiblePlanner:
    """LLM planner over any OpenAI-compatible chat-completions endpoint.

    Talked to with plain httpx, so no provider SDK is required: the key
    comes from ``--llm-api-key`` or the environment variables listed in
    the module docstring. Same contract as every planner here: one
    ``gateway_tool``, max 6 turns, a blocked call is terminal, and only
    the public task text is ever sent to the model.
    """

    name = "openai-compatible"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        transport: Any = None,
    ) -> None:
        detected = detect_openai_compatible_config()
        self.api_key = api_key or (detected[0] if detected else None)
        self.base_url = (
            base_url
            or _first_env(_GENERIC_BASE_ENVS)
            or (detected[1] if detected else "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or (detected[2] if detected else "gpt-4o-mini")
        self._transport = transport

    @classmethod
    def is_configured(cls) -> bool:
        return detect_openai_compatible_config() is not None

    def run(self, client: GatewayClient, token: str) -> list[dict]:
        if httpx is None:  # pragma: no cover
            raise UnavailableError("httpx is not installed")
        if not self.api_key:
            raise UnavailableError(
                "no LLM API key configured; set FLOWGATE_AGENT_API_KEY / "
                "OPENAI_API_KEY (or another supported provider key), pass "
                "--llm-api-key, or run with --offline"
            )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "gateway_tool",
                    "description": (
                        "Perform one FlowGate tool call. operation is read "
                        "or write; content is required for write. The "
                        "gateway decides whether the call executes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["read", "write"]},
                            "resource_id": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["operation", "resource_id"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            }
        ]
        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _PUBLIC_TASK},
        ]
        verdicts: list[dict] = []
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(
            transport=self._transport, timeout=60.0
        ) as llm:
            for _turn in range(6):
                try:
                    resp = llm.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": self.model,
                            "messages": messages,
                            "tools": tools,
                            "tool_choice": "auto",
                            "temperature": 0,
                        },
                    )
                except Exception as exc:
                    raise UnavailableError(
                        f"LLM endpoint unreachable at {self.base_url}: {exc}"
                    ) from exc
                if resp.status_code != 200:
                    raise UnavailableError(
                        f"LLM endpoint returned HTTP {resp.status_code} "
                        f"for model {self.model} at {self.base_url}"
                    )
                try:
                    data = resp.json()
                    choice = data["choices"][0]
                    message = choice.get("message") or {}
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise UnavailableError(
                        "LLM endpoint returned an unexpected response shape"
                    ) from exc
                if message.get("refusal") or choice.get("finish_reason") == "refusal":
                    break
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                blocked = False
                for call in tool_calls:
                    function = call.get("function") or {}
                    if function.get("name") != "gateway_tool":
                        continue
                    raw_args = function.get("arguments") or "{}"
                    try:
                        inputs = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else dict(raw_args)
                        )
                    except (ValueError, TypeError):
                        inputs = {}
                    operation = inputs.get("operation")
                    resource_id = inputs.get("resource_id")
                    content = inputs.get("content")
                    if resource_id not in _PUBLIC_RESOURCES:
                        verdicts.append(
                            {
                                "operation": operation,
                                "resource_id": resource_id,
                                "status": 0,
                                "decision": "blocked",
                                "policy_id": "planner-allowlist",
                                "reason": "Planner may only use public resources.",
                                "result": None,
                            }
                        )
                        blocked = True
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "content": json.dumps(
                                    {
                                        "decision": "blocked",
                                        "policy_id": "planner-allowlist",
                                        "reason": "Planner may only use public resources.",
                                    }
                                ),
                            }
                        )
                        continue
                    status, body = client.gateway_tool(
                        token, operation, resource_id, content
                    )
                    verdicts.append(_verdict(operation, resource_id, status, body))
                    if body.get("decision") == "blocked":
                        blocked = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": json.dumps(
                                {
                                    "decision": body.get("decision"),
                                    "policy_id": body.get("policy_id"),
                                    "reason": body.get("reason"),
                                    "result": body.get("result"),
                                }
                            ),
                        }
                    )
                if blocked:
                    # Terminal: the gateway reason stands; no retry
                    # around the gate on a later turn.
                    break
                if choice.get("finish_reason") == "stop":
                    break
        return verdicts


class ClaudePlanner:
    """Real-LLM planner: Claude proposes gateway_tool calls, gateway decides.

    Only the public task text above is ever sent to the model. Raises
    UnavailableError when the anthropic package or ANTHROPIC_API_KEY is
    missing, so callers can fall back to ScriptedPlanner.
    """

    name = "claude"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get(
            "FLOWGATE_AGENT_MODEL", DEFAULT_ANTHROPIC_MODEL
        )

    def _ensure_available(self):
        try:
            import anthropic
        except Exception as exc:  # ImportError and friends
            raise UnavailableError(
                "the anthropic package is not installed; "
                "install it or run with --offline"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise UnavailableError(
                "ANTHROPIC_API_KEY is not set; export it or run with --offline"
            )
        return anthropic

    @staticmethod
    def _block_attr(block: Any, attr: str) -> Any:
        if isinstance(block, dict):
            return block.get(attr)
        return getattr(block, attr, None)

    def run(self, client: GatewayClient, token: str) -> list[dict]:
        anthropic = self._ensure_available()
        api = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        tools = [
            {
                "name": "gateway_tool",
                "description": (
                    "Perform one FlowGate tool call. operation is read or "
                    "write; content is required for write. The gateway "
                    "decides whether the call executes."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["read", "write"]},
                        "resource_id": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["operation", "resource_id"],
                    "additionalProperties": False,
                },
            }
        ]
        messages: list[dict] = [{"role": "user", "content": _PUBLIC_TASK}]
        verdicts: list[dict] = []

        for _turn in range(6):
            response = api.messages.create(
                model=self.model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                tools=tools,
                tool_choice={"type": "auto"},
                messages=messages,
            )
            stop_reason = self._block_attr(response, "stop_reason")
            if stop_reason == "refusal":
                break
            content_blocks = self._block_attr(response, "content") or []
            tool_uses = [
                block
                for block in content_blocks
                if self._block_attr(block, "type") == "tool_use"
                and self._block_attr(block, "name") == "gateway_tool"
            ]
            if not tool_uses:
                break

            tool_results: list[dict] = []
            blocked = False
            for block in tool_uses:
                inputs = self._block_attr(block, "input") or {}
                if isinstance(inputs, str):
                    try:
                        inputs = json.loads(inputs)
                    except ValueError:
                        inputs = {}
                operation = inputs.get("operation")
                resource_id = inputs.get("resource_id")
                content = inputs.get("content")
                if resource_id not in _PUBLIC_RESOURCES:
                    # Local allowlist stop: never execute non-public
                    # proposals, so restricted text cannot be fetched
                    # into the model's context in the first place.
                    verdict = {
                        "operation": operation,
                        "resource_id": resource_id,
                        "status": 0,
                        "decision": "blocked",
                        "policy_id": "planner-allowlist",
                        "reason": "Planner may only use public resources.",
                        "result": None,
                    }
                    verdicts.append(verdict)
                    blocked = True
                    continue
                status, body = client.gateway_tool(
                    token, operation, resource_id, content
                )
                verdict = _verdict(operation, resource_id, status, body)
                verdicts.append(verdict)
                if body.get("decision") == "blocked":
                    blocked = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": self._block_attr(block, "id"),
                        "content": json.dumps(
                            {
                                "decision": body.get("decision"),
                                "policy_id": body.get("policy_id"),
                                "reason": body.get("reason"),
                                "result": body.get("result"),
                            }
                        ),
                    }
                )
            if blocked:
                # A denied call is terminal: the gateway reason stands
                # and the loop does not continue around the gate.
                break
            messages.append({"role": "assistant", "content": content_blocks})
            messages.append({"role": "user", "content": tool_results})
            if stop_reason == "end_turn":
                break
        return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FlowGate real-agent step")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--admin-key",
        default=None,
        help="Admin key (default: $FLOWGATE_ADMIN_KEY or flowgate-dev-admin)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force the deterministic scripted planner",
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help="LLM API key for any OpenAI-compatible endpoint "
             "(default: $FLOWGATE_AGENT_API_KEY / $OPENAI_API_KEY / "
             "another supported provider key)",
    )
    parser.add_argument(
        "--llm-base-url",
        default=None,
        help="OpenAI-compatible base URL, e.g. "
             "https://openrouter.ai/api/v1 (default per provider key)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model id (default: $FLOWGATE_AGENT_MODEL, then "
             "$GEMINI_MODEL for Gemini, then the provider default; "
             "the claude-opus-5-5 default applies only to --provider "
             "anthropic)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["auto", "gemini", "anthropic", "openai-compatible"],
        help="Force the LLM provider (default: $FLOWGATE_AGENT_PROVIDER "
             "or auto-detect from the API-key environment variables)",
    )
    args = parser.parse_args(argv)
    admin_key = (
        args.admin_key
        or os.environ.get("FLOWGATE_ADMIN_KEY")
        or "flowgate-dev-admin"
    )
    provider = (
        args.provider or os.environ.get("FLOWGATE_AGENT_PROVIDER") or "auto"
    ).lower()

    def _run_llm(client, token, planner, label):
        try:
            result = planner.run(client, token)
            print(f"Using {label} (model {planner.model}).")
            return result
        except UnavailableError as exc:
            print(
                f"{label} unavailable: {exc}. "
                "Falling back to the scripted planner."
            )
            return ScriptedPlanner().run(client, token)

    try:
        with GatewayClient(args.base_url, admin_key=admin_key) as client:
            run = client.create_run("public", 0)
            token = run["token"]
            if args.offline:
                print("Using scripted planner (--offline).")
                verdicts = ScriptedPlanner().run(client, token)
            elif provider == "gemini":
                g_key, g_base, g_model = resolve_gemini_config(
                    api_key=args.llm_api_key,
                    base_url=args.llm_base_url,
                    model=args.model,
                )
                verdicts = _run_llm(
                    client, token,
                    OpenAICompatiblePlanner(
                        api_key=g_key, base_url=g_base, model=g_model
                    ),
                    f"Gemini planner at {g_base}",
                )
            elif provider == "anthropic":
                verdicts = _run_llm(
                    client, token,
                    ClaudePlanner(model=args.model),
                    "Claude planner",
                )
            elif (
                provider == "openai-compatible"
                or args.llm_api_key
                or args.llm_base_url
                or OpenAICompatiblePlanner.is_configured()
            ):
                llm_planner = OpenAICompatiblePlanner(
                    api_key=args.llm_api_key,
                    base_url=args.llm_base_url,
                    model=args.model,
                )
                verdicts = _run_llm(
                    client, token, llm_planner,
                    f"OpenAI-compatible planner at {llm_planner.base_url}",
                )
            elif os.environ.get("ANTHROPIC_API_KEY"):
                verdicts = _run_llm(
                    client, token,
                    ClaudePlanner(model=args.model),
                    "Claude planner",
                )
            else:
                print(
                    "No LLM configured: set an API key in the environment "
                    "(e.g. $env:GEMINI_API_KEY, $env:OPENAI_API_KEY, "
                    "$env:FLOWGATE_AGENT_API_KEY, or $env:ANTHROPIC_API_KEY) "
                    "or pass --llm-api-key. Falling back to the scripted planner."
                )
                verdicts = ScriptedPlanner().run(client, token)
    except FlowGateClientError as exc:
        # The extracted client renames this script's old httpx-missing
        # error to FlowGateClientError; preserve the original surface
        # (and therefore the planners' UnavailableError fallback
        # behaviour) when httpx itself is unavailable.
        if httpx is None:
            raise UnavailableError(str(exc)) from exc
        raise
    except Exception as exc:
        if httpx is not None and isinstance(
            exc, (httpx.ConnectError, httpx.TimeoutException)
        ):
            print(
                f"FlowGate backend unreachable at {args.base_url}: "
                "connection refused. This is NOT an LLM API-key error — "
                "the gateway is not running. Start it first:\n"
                "  cd backend\n"
                '  $env:FLOWGATE_DEMO_MODE = "1"\n'
                "  python -m uvicorn app.main:app --port 8000\n"
                "Leave it running, then re-run this script from a "
                "second terminal.",
                file=sys.stderr,
            )
            return 2
        raise

    for verdict in verdicts:
        print(
            f"{verdict['operation']} {verdict['resource_id']}: "
            f"{verdict['decision']} (policy_id={verdict['policy_id']}, "
            f"HTTP {verdict['status']}) {verdict.get('reason') or ''}".rstrip()
        )
    published = any(
        v["operation"] == "write"
        and v["resource_id"] == "public-outbox"
        and v["decision"] == "allowed"
        for v in verdicts
    )
    print("Public publication allowed." if published else "Public publication NOT allowed.")
    return 0 if published else 1


if __name__ == "__main__":
    sys.exit(main())
