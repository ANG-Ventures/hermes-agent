"""kanban_attach fails closed when the path cannot be proven to be the file
the agent meant (FleetReview retro-backfill C6, hermes-agent#1024)."""

from __future__ import annotations

import hashlib
import json

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _stored(tid):
    from pathlib import Path
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        return [Path(a.stored_path).read_bytes() for a in kb.list_attachments(conn, tid)]
    finally:
        conn.close()


def test_trailing_space_path_is_not_rewritten_to_a_sibling(worker_env, tmp_path):
    """#1024 L1288: '/tmp/report ' must never attach '/tmp/report'."""
    from tools import kanban_tools as kt

    (tmp_path / "report").write_bytes(b"WRONG FILE")
    (tmp_path / "report ").write_bytes(b"requested")
    out = json.loads(kt._handle_attach({"task_id": worker_env, "path": str(tmp_path / "report ")}))
    assert out.get("ok") is True, out
    assert _stored(worker_env) == [b"requested"]


def test_trailing_space_path_that_does_not_exist_is_refused(worker_env, tmp_path):
    from tools import kanban_tools as kt

    (tmp_path / "report").write_bytes(b"WRONG FILE")
    out = json.loads(kt._handle_attach({"task_id": worker_env, "path": str(tmp_path / "report ")}))
    assert "error" in out and "not a regular file" in out["error"]
    assert _stored(worker_env) == []


@pytest.mark.parametrize("backend", ["docker", "ssh", "modal"])
def test_remote_backend_path_without_digest_is_refused(worker_env, tmp_path, monkeypatch, backend):
    """#1024 L1331: on a remote terminal the host file may be a different file."""
    from tools import kanban_tools as kt

    monkeypatch.setenv("TERMINAL_ENV", backend)
    src = tmp_path / "artifact.txt"
    src.write_bytes(b"host bytes")
    out = json.loads(kt._handle_attach({"task_id": worker_env, "path": str(src)}))
    assert "remote-path-unverified" in out.get("error", ""), out
    assert _stored(worker_env) == []


def test_remote_backend_path_with_matching_digest_attaches(worker_env, tmp_path, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    src = tmp_path / "artifact.txt"
    src.write_bytes(b"same bytes")
    out = json.loads(kt._handle_attach({
        "task_id": worker_env, "path": str(src),
        "expected_sha256": hashlib.sha256(b"same bytes").hexdigest(),
    }))
    assert out.get("ok") is True, out
    assert _stored(worker_env) == [b"same bytes"]


def test_local_backend_path_needs_no_digest(worker_env, tmp_path):
    from tools import kanban_tools as kt

    src = tmp_path / "artifact.txt"
    src.write_bytes(b"x")
    assert json.loads(kt._handle_attach({"task_id": worker_env, "path": str(src)})).get("ok") is True
