"""Tests for POST /demo/challenge (Tier 3 judge-challenge mode).

Fresh writers are decided by the scanner, tainted writers (who read the
roadmap through the gateway first) by the flow check. The response must
never echo the submitted content or a canary value.

Run context is backend/; a sys.path fallback keeps the test working
when pytest is invoked from the workspace root.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from seed import ALPHA_DRAFT_CONTENT, LEAK_SUMMARY, ROADMAP_CONTENT  # noqa: E402

POLICIES_PATH = BACKEND_DIR / "policies.yaml"
ADMIN_KEY = "test-admin"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}
CANARY = "CANARY-ALPHA-NOVA-7F3K9"

BENIGN = (
    "Community picnic volunteers arrange tables near the elm trees, share "
    "lemonade and sketchbooks, and welcome neighbours for songs and games."
)


def _make_client(tmp_path, db_name: str = "gateway.db") -> TestClient:
    app = create_app(
        db_path=str(tmp_path / db_name),
        policies_path=str(POLICIES_PATH),
        admin_key=ADMIN_KEY,
    )
    return TestClient(app)


def _challenge(client: TestClient, content: str, writer: str, headers=None):
    kwargs = {} if headers is None else {"headers": headers}
    return client.post(
        "/demo/challenge", json={"content": content, "writer": writer}, **kwargs
    )


def test_challenge_requires_admin(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _challenge(client, BENIGN, "fresh")
        assert resp.status_code == 401, resp.text
        assert resp.json()["policy_id"] == "admin-required"

        resp = _challenge(
            client, BENIGN, "fresh", headers={"X-Admin-Key": "wrong-key"}
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["policy_id"] == "admin-required"


def test_challenge_fresh_exact_copy_blocked_by_scanner(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _challenge(client, ROADMAP_CONTENT, "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decision"] == "blocked", body
        assert body["policy_id"] == "detect-exact-copy", body
        assert body["layer"] == "scanner", body
        assert body["writer"] == "fresh", body
        assert body["suspended"] is True, body
        assert body["run_id"] and body["event_id"], body
        # Submitted content / canary must never be echoed back.
        assert CANARY not in resp.text
        assert ROADMAP_CONTENT[:40] not in resp.text


def test_challenge_fresh_canary_blocked_and_redacted(tmp_path):
    with _make_client(tmp_path) as client:
        payload = f"Weekly picnic reminder mention {CANARY} at the door."
        resp = _challenge(client, payload, "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decision"] == "blocked", body
        assert body["policy_id"] == "detect-canary", body
        assert body["layer"] == "scanner", body
        assert body["suspended"] is True, body
        assert CANARY not in resp.text
        assert CANARY not in str(body.get("reason") or "")


def test_challenge_fresh_benign_allowed(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _challenge(client, BENIGN, "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decision"] == "allowed", body
        assert body["layer"] == "none", body
        assert body["suspended"] is False, body


def test_challenge_tainted_write_down_blocked_by_flow_without_suspension(tmp_path):
    with _make_client(tmp_path) as client:
        for payload in (ROADMAP_CONTENT, ALPHA_DRAFT_CONTENT, BENIGN):
            resp = _challenge(client, payload, "tainted", headers=ADMIN_HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["decision"] == "blocked", body
            assert body["policy_id"] == "deny-write-down", body
            assert body["layer"] == "flow", body
            assert body["writer"] == "tainted", body
            assert body["suspended"] is False, body
            assert CANARY not in resp.text


def test_challenge_validation_limits(tmp_path):
    with _make_client(tmp_path) as client:
        resp = _challenge(client, "", "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 422, resp.text

        resp = _challenge(client, "x" * 2001, "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 422, resp.text

        resp = _challenge(client, BENIGN, "someone-else", headers=ADMIN_HEADERS)
        assert resp.status_code == 422, resp.text


def test_challenge_rate_limited(tmp_path):
    with _make_client(tmp_path) as client:
        for attempt in range(20):
            resp = _challenge(
                client, f"{BENIGN} Note {attempt}.", "fresh",
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 200, (attempt, resp.text)
        resp = _challenge(client, BENIGN, "fresh", headers=ADMIN_HEADERS)
        assert resp.status_code == 429, resp.text
        assert resp.json()["policy_id"] == "challenge-rate-limited"
