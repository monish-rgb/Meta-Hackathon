"""Tier 5 file-docs external backend tests (CONTRACT Tier 5 Amendment).

``backend/file_docs_service.py`` is a second protected backend with the
same execution protocol as ``tool_service.py``: ``POST /execute``,
``GET /state/{id}``, ``GET /receipts``, ``X-Tool-Key`` only, and Tier 4
``request_id``/``payload_hash`` deduplication. A resource_id maps to
exactly one file ``<root>/<resource_id>.txt``; traversal is rejected.
"""

import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import file_docs_service  # noqa: E402
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


def _payload_hash(content: str) -> str:
    """Normalised-content hash, matching file-docs recovery semantics."""
    normalised = re.sub(r"\s+", " ", content.lower()).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _receipts_for(client, request_id: str) -> list:
    resp = client.get(
        "/receipts", params={"request_id": request_id}, headers=KEY_HEADERS
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    receipts = payload["receipts"] if isinstance(payload, dict) else payload
    return list(receipts or [])


class _CommitGate:
    """Counts commits and can fail exactly one numbered commit.

    ``sqlite3.Connection.commit`` is read-only on the C type, so the
    receipts connection (``app.state.conn``) cannot be monkeypatched
    directly.  Instead the app is created with a ``sqlite3.Connection``
    subclass installed through the ``factory`` argument: that subclass
    *is* the connection the service closes over and exposes as
    ``app.state.conn``, and its ``commit`` consults this gate.  The
    gate can count commits during a normal execute (to learn the
    cadence) and then raise ``RuntimeError`` on the Nth commit of the
    next execute, exactly once.
    """

    def __init__(self):
        self.observing = False
        self.observed_commits = 0
        self.fail_on_commit = None
        self.commits_since_arm = 0
        self.tripped = False

    def start_counting(self) -> None:
        self.observing = True
        self.observed_commits = 0

    def stop_counting(self) -> int:
        self.observing = False
        return self.observed_commits

    def arm(self, fail_on_commit: int) -> None:
        self.fail_on_commit = fail_on_commit
        self.commits_since_arm = 0
        self.tripped = False

    def disarm(self) -> None:
        self.fail_on_commit = None


def _create_gated_app(tmp_path, gate: _CommitGate):
    """Create a file-docs app whose receipts connection consults ``gate``.

    ``sqlite3.connect`` is patched only while ``create_file_docs_app``
    opens its connection and is restored immediately afterwards; the
    returned connection keeps the gated subclass for its lifetime.
    """

    class _GatedConnection(sqlite3.Connection):
        def commit(self):  # noqa: D102 - fault-injection wrapper
            if gate.observing:
                gate.observed_commits += 1
            if gate.fail_on_commit is not None:
                gate.commits_since_arm += 1
                if gate.commits_since_arm == gate.fail_on_commit:
                    gate.fail_on_commit = None
                    gate.tripped = True
                    raise RuntimeError(
                        "injected committed-marking commit failure"
                    )
            return super().commit()

    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        kwargs["factory"] = _GatedConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = _connect
    try:
        return create_file_docs_app(root_dir=str(tmp_path), service_key=SERVICE_KEY)
    finally:
        sqlite3.connect = real_connect


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


def test_committed_marking_commit_failure_recovers_on_retry(tmp_path):
    """Reviewer reproduction, service level: the committed-marking commit
    fails after ``os.replace`` has landed.

    The file changing while the call errors is exactly why a 5xx from
    this backend can never prove non-execution.  The durable ``pending``
    receipt is the recovery record: a retry under the same ``request_id``
    promotes it to ``committed`` with no second receipt.
    """
    gate = _CommitGate()
    app = _create_gated_app(tmp_path, gate)
    # The gated connection is the very object the service exposes.
    assert isinstance(app.state.conn, sqlite3.Connection)
    client = TestClient(app, raise_server_exceptions=False)
    with client:
        # Learn the commit cadence of one successful write empirically:
        # pending-receipt commit first, committed-marking commit last.
        warm_content = "Warmup release notes used to learn commit cadence."
        gate.start_counting()
        warm = _execute(
            client,
            {
                "resource_id": "published-releases",
                "operation": "write",
                "content": warm_content,
                "run_id": "run-1",
                "request_id": "req-warmup",
                "payload_hash": _payload_hash(warm_content),
            },
        )
        commits_per_write = gate.stop_counting()
        assert warm.status_code == 200, warm.text
        assert commits_per_write >= 2, commits_per_write

        # Fail exactly the commit that follows the os.replace.
        gate.arm(commits_per_write)
        content = "Recovered release notes whose marking commit failed once."
        body = {
            "resource_id": "published-releases",
            "operation": "write",
            "content": content,
            "run_id": "run-1",
            "request_id": "req-commit-fail",
            "payload_hash": _payload_hash(content),
        }
        failed = _execute(client, body)
        assert gate.tripped is True
        assert failed.status_code == 500, failed.text

        # The write DID land even though the call reported failure.
        on_disk = tmp_path / "published-releases.txt"
        assert on_disk.read_text(encoding="utf-8") == content

        # Exactly one receipt, left pending as the recovery record.
        pending = _receipts_for(client, "req-commit-fail")
        assert len(pending) == 1, pending
        assert pending[0]["state"] == "pending", pending

        # Retry with the commit restored: the pending receipt is
        # promoted (file already carries the payload), not duplicated.
        gate.disarm()
        retry = _execute(client, dict(body))
        assert retry.status_code == 200, retry.text
        assert retry.json().get("deduplicated") is True, retry.text
        assert retry.json()["receipt_id"] == pending[0]["receipt_id"], retry.text

        receipts = _receipts_for(client, "req-commit-fail")
        assert len(receipts) == 1, receipts
        assert receipts[0]["state"] == "committed", receipts
        assert on_disk.read_text(encoding="utf-8") == content


def test_crash_before_replace_recovers_on_retry(tmp_path, monkeypatch):
    """Pending receipt exists but the replace never happened: the retry
    performs the write now and converges to one committed receipt."""
    app = create_file_docs_app(root_dir=str(tmp_path), service_key=SERVICE_KEY)
    client = TestClient(app, raise_server_exceptions=False)
    with client:
        old_content = "Original release notes before the simulated crash."
        first = _execute(
            client,
            {
                "resource_id": "published-releases",
                "operation": "write",
                "content": old_content,
                "run_id": "run-1",
                "request_id": "req-before-crash",
                "payload_hash": _payload_hash(old_content),
            },
        )
        assert first.status_code == 200, first.text

        content = "Release notes that only land on the recovery retry."
        body = {
            "resource_id": "published-releases",
            "operation": "write",
            "content": content,
            "run_id": "run-1",
            "request_id": "req-crash-before-replace",
            "payload_hash": _payload_hash(content),
        }

        # file_docs_service calls os.replace through the stdlib os
        # module; failing it once simulates dying after the durable
        # pending insert but before the file is replaced.
        real_replace = os.replace
        replace_calls = {"count": 0}

        def failing_replace(src, dst):
            replace_calls["count"] += 1
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(file_docs_service.os, "replace", failing_replace)
        failed = _execute(client, body)
        assert replace_calls["count"] == 1
        assert failed.status_code == 500, failed.text
        monkeypatch.setattr(file_docs_service.os, "replace", real_replace)

        # Nothing replaced yet, but the pending receipt is durable.
        on_disk = tmp_path / "published-releases.txt"
        assert on_disk.read_text(encoding="utf-8") == old_content
        pending = _receipts_for(client, "req-crash-before-replace")
        assert len(pending) == 1, pending
        assert pending[0]["state"] == "pending", pending

        retry = _execute(client, dict(body))
        assert retry.status_code == 200, retry.text
        assert retry.json().get("deduplicated") is True, retry.text
        assert retry.json()["receipt_id"] == pending[0]["receipt_id"], retry.text

        receipts = _receipts_for(client, "req-crash-before-replace")
        assert len(receipts) == 1, receipts
        assert receipts[0]["state"] == "committed", receipts
        assert on_disk.read_text(encoding="utf-8") == content
