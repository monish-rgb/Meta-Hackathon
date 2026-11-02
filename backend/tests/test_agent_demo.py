"""Tests for scripts/agent_demo.py (Tier 3 real-agent step).

Drives ScriptedPlanner end-to-end through httpx ASGITransport against a
temp-db gateway in demo_mode, without any network or backend imports in
the script under test (it is loaded as an external client module).
"""

import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = WORKSPACE / "scripts"
BACKEND_DIR = WORKSPACE / "backend"
for _path in (str(SCRIPTS_DIR), str(BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import httpx  # noqa: E402
import pytest  # noqa: E402

import agent_demo  # noqa: E402
from app.main import create_app  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"


def _gateway_client(tmp_path) -> agent_demo.GatewayClient:
    app = create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        demo_mode=True,
    )
    transport = httpx.ASGITransport(app=app)
    return agent_demo.GatewayClient(
        "http://testserver", admin_key="flowgate-dev-admin", transport=transport
    )


def test_scripted_planner_end_to_end(tmp_path):
    client = _gateway_client(tmp_path)
    try:
        run = client.create_run("public", 0)
        assert run["token"]

        planner = agent_demo.ScriptedPlanner()
        verdicts = planner.run(client, run["token"])

        assert [v["operation"] for v in verdicts] == ["read", "write"]
        assert verdicts[0]["resource_id"] == "public-source"
        assert verdicts[0]["decision"] == "allowed"
        write = verdicts[1]
        assert write["resource_id"] == "public-outbox"
        assert write["decision"] == "allowed", write
        assert write["status"] == 200

        # Read-back: the composed summary is actually in the outbox.
        status, body = client.gateway_tool(run["token"], "read", "public-outbox")
        assert status == 200, body
        assert body["decision"] == "allowed"
        assert body["result"]
        assert body["result"] == planner.last_summary
        assert "Community Open Day" in body["result"]
    finally:
        client.close()


def test_claude_planner_unavailable_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    planner = agent_demo.ClaudePlanner()
    client = _gateway_client(tmp_path)
    try:
        with pytest.raises(agent_demo.UnavailableError):
            planner.run(client, "unused-token")
    finally:
        client.close()


def test_claude_planner_unavailable_without_package(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    # A None entry in sys.modules makes `import anthropic` fail.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    planner = agent_demo.ClaudePlanner()
    client = _gateway_client(tmp_path)
    try:
        with pytest.raises(agent_demo.UnavailableError):
            planner.run(client, "unused-token")
    finally:
        client.close()


_LLM_ENV_VARS = [
    "FLOWGATE_AGENT_API_KEY", "LLM_API_KEY", "FLOWGATE_AGENT_BASE_URL",
    "LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY", "TOGETHER_API_KEY", "XAI_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    "FLOWGATE_AGENT_MODEL", "GEMINI_MODEL", "GOOGLE_MODEL",
    "FLOWGATE_AGENT_PROVIDER", "FLOWGATE_ADMIN_KEY",
]


def test_openai_compatible_planner_unavailable_without_key(tmp_path, monkeypatch):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    planner = agent_demo.OpenAICompatiblePlanner()
    client = _gateway_client(tmp_path)
    try:
        with pytest.raises(agent_demo.UnavailableError):
            planner.run(client, "unused-token")
    finally:
        client.close()


def test_openai_compatible_config_detects_provider_key(monkeypatch):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    config = agent_demo.detect_openai_compatible_config()
    assert config is not None
    key, base_url, _model = config
    assert key == "test-groq-key"
    assert "groq" in base_url


def test_gemini_config_uses_env_model_not_claude_default(monkeypatch):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("FLOWGATE_AGENT_MODEL", "gemini-from-env")
    config = agent_demo.detect_openai_compatible_config()
    assert config is not None
    key, base_url, model = config
    assert key == "test-gemini-key"
    assert "generativelanguage.googleapis.com" in base_url
    assert model == "gemini-from-env"
    assert "claude" not in model

    key2, base2, model2 = agent_demo.resolve_gemini_config()
    assert (key2, model2) == ("test-gemini-key", "gemini-from-env")
    assert base2 == agent_demo.GEMINI_BASE_URL

    # CLI --model beats every environment variable.
    _k, _b, model3 = agent_demo.resolve_gemini_config(model="gemini-from-cli")
    assert model3 == "gemini-from-cli"


def test_main_reports_unreachable_backend(monkeypatch, capsys):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Port 9 on loopback: nothing listens, connection refused at once.
    rc = agent_demo.main(
        ["--base-url", "http://127.0.0.1:9", "--offline"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "backend unreachable" in err
    assert "uvicorn" in err


def test_openai_compatible_planner_end_to_end(tmp_path):
    import json as _json

    seen_models = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        payload = _json.loads(request.content)
        seen_models.append(payload["model"])
        messages = payload["messages"]
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if not tool_results:
            call = {
                "id": "call-read", "type": "function",
                "function": {
                    "name": "gateway_tool",
                    "arguments": _json.dumps(
                        {"operation": "read", "resource_id": "public-source"}
                    ),
                },
            }
        elif len(tool_results) == 1:
            call = {
                "id": "call-write", "type": "function",
                "function": {
                    "name": "gateway_tool",
                    "arguments": _json.dumps(
                        {
                            "operation": "write",
                            "resource_id": "public-outbox",
                            "content": (
                                "Open day summary: community festival on "
                                "Saturday with stalls, music and garden tours."
                            ),
                        }
                    ),
                },
            }
        else:
            return httpx.Response(
                200,
                json={"choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Done."},
                }]},
            )
        return httpx.Response(
            200,
            json={"choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant", "content": None, "tool_calls": [call],
                },
            }]},
        )

    client = _gateway_client(tmp_path)
    try:
        run = client.create_run("public", 0)
        planner = agent_demo.OpenAICompatiblePlanner(
            api_key="test-key",
            base_url="http://llm.test/v1",
            model="test-model",
            transport=httpx.MockTransport(llm_handler),
        )
        verdicts = planner.run(client, run["token"])
        assert seen_models == ["test-model", "test-model", "test-model"]
        assert [v["decision"] for v in verdicts] == ["allowed", "allowed"]

        status, body = client.gateway_tool(run["token"], "read", "public-outbox")
        assert status == 200, body
        assert "community festival" in body["result"]
    finally:
        client.close()
