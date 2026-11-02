"""FlowGate quantified mini-eval tests (Tier 3).

Pytest mirror of scripts/mini_eval.py: the same frozen 24 cases so the
reported numbers cannot silently drift. 12 restricted writes must all
be blocked (6 by the flow check, 6 by the scanner), 12 benign public
writes must all be allowed, flow blocks never suspend, and the scanner
alone (direct scan_write) must catch fewer than the gateway's 12/12.

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

from app import store  # noqa: E402
from app.detection import scan_write  # noqa: E402
from app.main import create_app  # noqa: E402
from seed import (  # noqa: E402
    ALPHA_DRAFT_CONTENT,
    LEAK_SUMMARY,
    PUBLIC_DRAFT_CONTENT,
    ROADMAP_CONTENT,
)

POLICIES_PATH = BACKEND_DIR / "policies.yaml"
ADMIN_KEY = "test-admin"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}

PADDING = (
    "Community picnic volunteers arrange tables and chairs, pour lemonade, "
    "share sketchbooks and garden stories, and welcome neighbours for songs "
    "and afternoon games on the lawn near the river. Families bring blankets, "
    "fruit, and board games for a relaxed gathering together."
)


def _excerpt() -> str:
    words = ROADMAP_CONTENT.split()
    return " ".join(words[10:25])


def _first_half() -> str:
    words = ROADMAP_CONTENT.split()
    return " ".join(words[: len(words) // 2])


def _second_half() -> str:
    words = ROADMAP_CONTENT.split()
    return " ".join(words[len(words) // 2:])


def _padded_copy() -> str:
    return f"{PADDING} {LEAK_SUMMARY} {PADDING}"


def _canary_text() -> str:
    return "Weekly picnic reminder mention CANARY-ALPHA-NOVA-7F3K9 at the door."


FLOW_CASES = [
    ("exact-copy", ROADMAP_CONTENT),
    ("leak-summary", LEAK_SUMMARY),
    ("short-excerpt", _excerpt()),
    ("padded-copy", _padded_copy()),
    ("clean-paraphrase", ALPHA_DRAFT_CONTENT),
    ("split-chunk-first-half", _first_half()),
]

SCANNER_CASES = [
    ("exact-copy", ROADMAP_CONTENT),
    ("canary-text", _canary_text()),
    ("leak-summary", LEAK_SUMMARY),
    ("short-excerpt", _excerpt()),
    ("split-chunk-second-half", _second_half()),
    ("padded-copy", _padded_copy()),
]

BENIGN_CASES = [
    ("public-draft-text", PUBLIC_DRAFT_CONTENT, "shared_doc", "public-draft"),
    (
        "library-hours",
        "Neighbourhood library reading circle meets on Tuesday evening to "
        "discuss short stories, share tea, and plan weekend visits for local "
        "families near the old fountain square.",
        "shared_kv", "public-outbox",
    ),
    (
        "garden-rota",
        "Community garden helpers plant carrots, lettuce, and courgettes in "
        "the raised beds, then water the seedlings and tidy the tool shed "
        "before sharing soup at noon.",
        "shared_doc", "public-draft",
    ),
    (
        "market-rota",
        "Saturday market stall rota lists Priya at the bakery table, Tomas "
        "at the flower stand, and June handling tickets for the morning "
        "craft fair in the village hall.",
        "shared_kv", "public-outbox",
    ),
    (
        "choir-rehearsal",
        "Local choir rehearsal moves to Thursday evening in the school hall, "
        "with warm-up songs, harmony practice, and biscuits during the "
        "interval for every singer attending.",
        "shared_doc", "public-draft",
    ),
    (
        "park-cleanup",
        "Park cleanup crew gathers on Sunday morning by the elm trees, "
        "bringing gloves, litter pickers, and flasks of tea, then finishes "
        "with sandwiches on the grass.",
        "shared_kv", "public-outbox",
    ),
    (
        "cooking-class",
        "Cooking class shares simple soup recipes, kneads bread dough, and "
        "practises knife skills together before tasting vegetable pies with "
        "fresh herbs from the window boxes.",
        "shared_doc", "public-draft",
    ),
    (
        "cycling-club",
        "Cycling club plans a gentle ride along the river path on Saturday, "
        "stopping for coffee at the boathouse and returning through the "
        "meadow before lunchtime.",
        "shared_kv", "public-outbox",
    ),
    (
        "chess-club",
        "Chess club invites beginners on Friday afternoon for friendly games, "
        "puzzle sheets, and tea in the community room above the bookshop on "
        "the high street.",
        "shared_doc", "public-draft",
    ),
    (
        "pottery-workshop",
        "Pottery workshop glazes bowls and mugs in soft blue, fires the kiln "
        "overnight, and displays finished cups at the weekend fair for "
        "neighbours to admire.",
        "shared_kv", "public-outbox",
    ),
    (
        "walking-group",
        "Walking group meets at the fountain for a slow stroll through the "
        "botanical gardens, pausing to sketch flowers and feed ducks by the "
        "pond before heading home.",
        "shared_doc", "public-draft",
    ),
    (
        "book-swap",
        "Book swap table opens beside the entrance on Monday, offering novels, "
        "picture books, and gardening guides that visitors may borrow, trade, "
        "or return the following week.",
        "shared_kv", "public-outbox",
    ),
]


def _make_app(tmp_path):
    return create_app(
        db_path=str(tmp_path / "gateway.db"),
        policies_path=str(POLICIES_PATH),
        admin_key=ADMIN_KEY,
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_run(client: TestClient, project: str, clearance: int) -> dict:
    resp = client.post(
        "/runs", json={"project": project, "clearance": clearance},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tool(client, token, tool_name, operation, resource_id, content=None):
    body: dict = {"operation": operation, "resource_id": resource_id}
    if content is not None:
        body["content"] = content
    return client.post(f"/tools/{tool_name}", headers=_auth(token), json=body)


def _runs_by_id(client: TestClient) -> dict:
    resp = client.get("/runs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    return {run["run_id"]: run for run in resp.json()}


def _scanner_only(payload: str, writer_project: str, fingerprints: list) -> dict:
    # Both restricted destinations are sensitivity 0.
    return scan_write(payload, fingerprints, writer_project, 0)


def test_mini_eval_frozen_24_cases(tmp_path):
    app = _make_app(tmp_path)
    fingerprints = store.list_fingerprints(app.state.conn)
    with TestClient(app) as client:
        flow_blocked = 0
        scanner_blocked = 0
        benign_allowed = 0
        restricted_for_scanner: list[tuple[str, str]] = []

        # 6 tainted flow cases: alpha read roadmap -> public-outbox.
        for name, payload in FLOW_CASES:
            restricted_for_scanner.append((payload, "alpha"))
            alpha = _create_run(client, "alpha", 2)
            resp = _tool(client, alpha["token"], "shared_doc", "read", "alpha-roadmap")
            assert resp.status_code == 200, (name, resp.text)
            assert resp.json()["decision"] == "allowed"

            resp = _tool(client, alpha["token"], "shared_kv", "write",
                         "public-outbox", payload)
            assert resp.status_code == 403, (name, resp.text)
            body = resp.json()
            assert body["decision"] == "blocked", (name, body)
            assert body["policy_id"] == "deny-write-down", (name, body)
            flow_blocked += 1
            # Flow blocks never suspend the tainted writer.
            assert _runs_by_id(client)[alpha["run_id"]]["suspended"] is False

        # 6 fresh scanner cases: beta read nothing -> shared-board.
        suspend_policies = {
            "detect-exact-copy", "detect-canary", "detect-taint-overlap",
        }
        for name, payload in SCANNER_CASES:
            restricted_for_scanner.append((payload, "beta"))
            beta = _create_run(client, "beta", 1)
            resp = _tool(client, beta["token"], "shared_kv", "write",
                         "shared-board", payload)
            assert resp.status_code == 403, (name, resp.text)
            body = resp.json()
            assert body["decision"] == "blocked", (name, body)
            assert body["policy_id"].startswith("detect-"), (name, body)
            scanner_blocked += 1
            suspended = _runs_by_id(client)[beta["run_id"]]["suspended"]
            if body["policy_id"] in suspend_policies:
                assert suspended is True, (name, body)
            elif body["policy_id"] == "detect-suspicious-overlap":
                assert suspended is False, (name, body)

        # 12 benign public writes: all allowed, none suspended.
        for name, text, tool_name, resource_id in BENIGN_CASES:
            public = _create_run(client, "public", 0)
            resp = _tool(client, public["token"], tool_name, "write",
                         resource_id, text)
            assert resp.status_code == 200, (name, resp.text)
            assert resp.json()["decision"] == "allowed", (name, resp.text)
            benign_allowed += 1
            assert _runs_by_id(client)[public["run_id"]]["suspended"] is False

        assert flow_blocked == 6
        assert scanner_blocked == 6
        assert flow_blocked + scanner_blocked == 12  # gateway blocks 12/12
        assert benign_allowed == len(BENIGN_CASES) == 12

        # The scanner alone is a backstop, not the guarantee: it misses
        # at least the clean paraphrase that the flow check blocks.
        scanner_only_blocked = sum(
            1
            for payload, writer_project in restricted_for_scanner
            if _scanner_only(payload, writer_project, fingerprints)["action"] != "allow"
        )
        assert scanner_only_blocked < 12
        # ... while the gateway blocked every restricted write above.
        assert flow_blocked + scanner_blocked == 12
