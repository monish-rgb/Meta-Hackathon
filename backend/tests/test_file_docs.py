"""Tier 5 file-docs external backend tests (CONTRACT Tier 5 Amendment).

``backend/file_docs_service.py`` is a second protected backend with the
same execution protocol as ``tool_service.py``: ``POST /execute``,
``GET /state/{id}``, ``GET /receipts``, ``X-Tool-Key`` only, and Tier 4
``request_id``/``payload_hash`` deduplication. A resource_id maps to
exactly one file ``<root>/<resource_id>.txt``; traversal is rejected.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from file_docs_service import create_file_docs_app  # noqa: E402

SERVICE_KEY = "test-file-docs-key"
KEY_HEADERS = {"X-Tool-Key": SERVICE_KEY}


def _make_client(tmp_path) -> TestClient:
    app = create_file_docs_app(root_dir=str(tmp_path), service_key=SERVICE_KEY)
    return TestClient(app)


def _execute(client, body, headers=None):
    return client.post(
        "/execute", json=body, headers=KEY_HEADERS if headers is None else headers
    )


def test_key_required(tmp_path):
    with _make_client(tmp_path) as client:
        body = {
            "resource_id": "tenant-alpha-brief",
            "operation": "read",
            "run_id": "run-1",
        }
        # No key and wrong key are both rejected, on every surface.
        assert _execute(client, body, headers={}).status_code == 401
        assert _execute(client, body, headers={"X-Tool-Key": "wrong"}).status_code == 401
        assert client.get("/state/tenant-alpha-brief").status_code == 401
        assert client.get(
            "/state/tenant-alpha-brief", headers={"X-Tool-Key": "wrong"}
        ).status_code == 401
        assert client.get("/receipts").status_code == 401
        # Nothing was executed or written by the rejected calls.
        assert not (tmp_path / "tenant-alpha-brief.txt").exists()


def test_write_read_state_roundtrip_hits_real_file(tmp_path):
    with _make_client(tmp_path) as client:
        content = "Tenant Alpha brief stored on disk for roundtrip testing."
        resp = _execute(
            client,
            {
                "resource_id": "tenant-alpha-brief",
                "operation": "write",
                "content": content,
                "run_id": "run-1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("receipt_id"), resp.text

        # The write is a real file on disk, the system of record.
        on_disk = tmp_path / "tenant-alpha-brief.txt"
        assert on_disk.exists()
        assert on_disk.read_text(encoding="utf-8") == content

        # Read back through the execution protocol.
        resp = _execute(
            client,
            {
                "resource_id": "tenant-alpha-brief",
                "operation": "read",
                "run_id": "run-1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == content, resp.text

        # And through the state endpoint.
        resp = client.get("/state/tenant-alpha-brief", headers=KEY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["resource_id"] == "tenant-alpha-brief", body
        assert body["content"] == content, body
        assert body.get("exists") is True, body


def test_traversal_rejected_400(tmp_path):
    with _make_client(tmp_path) as client:
        for bad_id in ("../secret", "..", "a/b", "Tenant-Alpha"):
            resp = _execute(
                client,
                {
                    "resource_id": bad_id,
                    "operation": "write",
                    "content": "pwned",
                    "run_id": "run-1",
                },
            )
            assert resp.status_code == 400, (bad_id, resp.text)
            resp = client.get(f"/state/{bad_id}", headers=KEY_HEADERS)
            # Path-style traversal may 404 at routing instead of reaching
            # the handler; the write path above is the binding 400 check.
            assert resp.status_code in (400, 404), (bad_id, resp.text)
        # No file escaped the docs root.
        assert not (tmp_path.parent / "secret.txt").exists()
        assert not (tmp_path / "secret.txt").exists()


def test_request_id_dedupe_one_receipt_same_id(tmp_path):
    with _make_client(tmp_path) as client:
        body = {
            "resource_id": "published-releases",
            "operation": "write",
            "content": "Release notes v1.",
            "run_id": "run-1",
            "request_id": "req-dedupe-1",
            "payload_hash": "hash-v1",
        }
        first = _execute(client, body)
        assert first.status_code == 200, first.text
        first_id = first.json()["receipt_id"]

        second = _execute(client, dict(body))
        assert second.status_code == 200, second.text
        assert second.json()["receipt_id"] == first_id, second.text

        resp = client.get(
            "/receipts", params={"request_id": "req-dedupe-1"}, headers=KEY_HEADERS
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        receipts = payload["receipts"] if isinstance(payload, dict) else payload
        assert len(receipts) == 1, payload
        assert receipts[0]["receipt_id"] == first_id, receipts


def test_request_id_mismatch_409(tmp_path):
    with _make_client(tmp_path) as client:
        body = {
            "resource_id": "published-releases",
            "operation": "write",
            "content": "Release notes v1.",
            "run_id": "run-1",
            "request_id": "req-mismatch-1",
            "payload_hash": "hash-v1",
        }
        assert _execute(client, body).status_code == 200

        mismatched = dict(body, content="Different payload.", payload_hash="hash-v2")
        resp = _execute(client, mismatched)
        assert resp.status_code == 409, resp.text

        # The mismatched retry did not overwrite the committed file.
        assert (tmp_path / "published-releases.txt").read_text(
            encoding="utf-8"
        ) == "Release notes v1."
