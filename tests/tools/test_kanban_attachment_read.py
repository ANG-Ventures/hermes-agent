"""kanban_attachment_read: a worker reads an attachment's BYTES through the kanban
tools, so it works from a pool host (ssh terminal backend) where the board-host
``stored_path`` does not exist (t_0fc58360)."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_session_id_contextvar():
    from gateway.session_context import _SESSION_ID, _UNSET
    _SESSION_ID.set(_UNSET)
    yield
    _SESSION_ID.set(_UNSET)


@pytest.fixture
def pool_worker(monkeypatch, tmp_path):
    """A dispatcher-owned worker whose terminal runs on a pool host over ssh."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        other = kb.create_task(conn, title="sibling", assignee="someone")
        # A provenance (non-gating) parent: the handoff source.
        tid = kb.create_task(conn, title="worker", assignee="test-worker",
                             parents=[parent], parents_kind="derived-from")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        assert parent in kb.parent_ids(conn, tid)
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    if run_id is not None:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"tid": tid, "parent": parent, "other": other}


def _store(task_id: str, name: str, data: bytes, content_type=None) -> int:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.store_attachment_bytes(conn, task_id, name, data, content_type=content_type)
    finally:
        conn.close()


def _read(args: dict) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_attachment_read(args))


def test_pool_worker_round_trips_its_own_attachment(pool_worker):
    """Write with kanban_attach, read back with kanban_attachment_read: sha256 equal."""
    from tools import kanban_tools as kt
    payload = bytes(range(256)) * 40  # binary, 10 KiB
    digest = hashlib.sha256(payload).hexdigest()
    put = json.loads(kt._handle_attach({
        "filename": "blob.bin", "content_base64": base64.b64encode(payload).decode(),
        "expected_sha256": digest}))
    assert put.get("ok") is True, put
    got = _read({"attachment_id": put["attachment_id"]})
    assert got.get("ok") is True, got
    assert got["encoding"] == "base64"
    assert got["sha256"] == digest
    assert got["size"] == len(payload)
    assert hashlib.sha256(base64.b64decode(got["content_base64"])).hexdigest() == digest


def test_text_attachment_comes_back_as_text(pool_worker):
    text = "spec v1 \u2014 caf\u00e9\n" * 50
    att = _store(pool_worker["tid"], "spec.md", text.encode("utf-8"), "text/markdown")
    got = _read({"attachment_id": att})
    assert got.get("ok") is True, got
    assert got["encoding"] == "utf-8"
    assert got["text"] == text
    assert got["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert got["filename"] == "spec.md"
    forced = _read({"attachment_id": att, "encoding": "base64"})
    assert base64.b64decode(forced["content_base64"]).decode("utf-8") == text


def test_parent_attachment_is_readable(pool_worker):
    att = _store(pool_worker["parent"], "handoff.txt", b"handoff bytes\n")
    got = _read({"attachment_id": att, "task_id": pool_worker["parent"]})
    assert got.get("ok") is True, got
    assert got["text"] == "handoff bytes\n"


def test_other_card_attachment_is_refused(pool_worker):
    att = _store(pool_worker["other"], "secret.txt", b"not yours\n")
    got = _read({"attachment_id": att, "task_id": pool_worker["other"]})
    assert got.get("ok") is not True, got
    assert pool_worker["other"] in got["error"] and "not yours" not in json.dumps(got)
    # Naming your own card does not launder another card's attachment id.
    got = _read({"attachment_id": att})
    assert got.get("ok") is not True, got
    assert "not yours" not in json.dumps(got)


def test_over_cap_attachment_is_refused_with_its_size(pool_worker):
    from tools import kanban_tools as kt
    big = b"x" * (kt.KANBAN_ATTACHMENT_READ_MAX_BYTES + 1)
    att = _store(pool_worker["tid"], "big.log", big)
    got = _read({"attachment_id": att})
    assert got.get("ok") is not True, got
    assert str(len(big)) in got["error"]
    assert str(kt.KANBAN_ATTACHMENT_READ_MAX_BYTES) in got["error"]


@pytest.mark.parametrize("unit", [b"\x00", b'"\\'], ids=["nul", "quote-backslash"])
def test_escape_heavy_text_at_the_cap_fits_the_result_budget(pool_worker, unit):
    """Valid UTF-8 that JSON must escape grows up to 6x; the reply falls back to base64
    instead of overflowing the tool-result budget (Prism c-9baa2b4733c00414)."""
    from tools import kanban_tools as kt
    from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
    payload = unit * (kt.KANBAN_ATTACHMENT_READ_MAX_BYTES // len(unit))
    assert len(json.dumps(payload.decode("utf-8"))) > DEFAULT_RESULT_SIZE_CHARS
    att = _store(pool_worker["tid"], "escapes.txt", payload)
    raw = kt._handle_attachment_read({"attachment_id": att})
    assert len(raw) <= DEFAULT_RESULT_SIZE_CHARS
    got = json.loads(raw)
    assert got.get("ok") is True, got
    assert got["encoding"] == "base64" and "budget" in got["note"]
    digest = hashlib.sha256(payload).hexdigest()
    assert got["sha256"] == digest
    assert hashlib.sha256(base64.b64decode(got["content_base64"])).hexdigest() == digest


def test_large_file_reads_back_in_ranges(pool_worker):
    """A 200 KiB file is refused whole but reassembles from four ranged reads."""
    from tools import kanban_tools as kt
    payload = bytes((i * 7 + i // 251) % 256 for i in range(200 * 1024))
    digest = hashlib.sha256(payload).hexdigest()
    att = _store(pool_worker["tid"], "big.bin", payload)
    assert _read({"attachment_id": att}).get("ok") is not True
    cap = kt.KANBAN_ATTACHMENT_READ_MAX_BYTES
    parts, offset = [], 0
    for _ in range(4):
        got = _read({"attachment_id": att, "offset": offset, "length": cap})
        assert got.get("ok") is True, got
        assert got["size_bytes"] == len(payload) and got["sha256"] == digest
        chunk = base64.b64decode(got["content_base64"])
        assert len(chunk) <= cap and got["offset"] == offset
        parts.append(chunk)
        offset += len(chunk)
    assert got["eof"] is True
    assert hashlib.sha256(b"".join(parts)).hexdigest() == digest
    assert _read({"attachment_id": att, "length": cap + 1}).get("ok") is not True


def test_no_path_argument_is_accepted(pool_worker):
    got = _read({"attachment_id": 1, "path": "/etc/passwd"})
    assert got.get("ok") is not True
    assert "unknown parameter" in got["error"]


def test_row_pointing_outside_the_attachments_dir_is_refused(pool_worker, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes\n")
    conn = kbc.connect()
    try:
        att = kb.add_attachment(conn, pool_worker["tid"], filename="outside.txt",
                                stored_path=str(outside), size=14)
    finally:
        conn.close()
    got = _read({"attachment_id": att})
    assert got.get("ok") is not True, got
    assert "outside bytes" not in json.dumps(got)


def test_worker_context_points_at_the_read_tool(pool_worker):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    att = _store(pool_worker["tid"], "notes.txt", b"n\n")
    conn = kbc.connect()
    try:
        ctx = kb.build_worker_context(conn, pool_worker["tid"])
    finally:
        conn.close()
    assert "kanban_attachment_read" in ctx
    assert f"id {att}" in ctx
