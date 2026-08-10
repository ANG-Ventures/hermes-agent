"""Dashboard API + bundle render of kanban comment provenance.

The dashboard is the surface where the original incident was invisible: two
``apollo`` comments from different sessions rendered identically. The REST
detail endpoint must carry ``run_id`` / ``session_ref`` / ``author_display``,
and the shipped browser bundle must actually render the display string.

Hermeticity (AC6): strips every ``HERMES_KANBAN*`` pin and asserts the resolved
DB is inside ``tmp_path`` before any write.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_provenance_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()

    resolved = kb.kanban_db_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"kanban DB escaped the sandbox: {resolved}"
    )
    assert "/.hermes/kanban/boards/" not in str(resolved), (
        f"kanban DB resolved onto a live board: {resolved}"
    )
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_detail_exposes_provenance_for_same_profile_sessions(client):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="contended card")
        ref_a = kb.derive_session_ref("apollo-session-A")
        ref_b = kb.derive_session_ref("apollo-session-B")
        kb.add_comment(conn, tid, author="apollo", body="parking this",
                       session_ref=ref_a, run_id=None)
        kb.add_comment(conn, tid, author="apollo", body="waiver granted",
                       session_ref=ref_b, run_id=12)
    finally:
        conn.close()

    d = client.get(f"/api/plugins/kanban/tasks/{tid}").json()
    comments = d["comments"]
    assert [c["author"] for c in comments] == ["apollo", "apollo"]
    assert [c["session_ref"] for c in comments] == [ref_a, ref_b]
    assert [c["run_id"] for c in comments] == [None, 12]
    displays = [c["author_display"] for c in comments]
    assert displays[0] != displays[1]
    assert ref_a in displays[0] and ref_b in displays[1]


def test_detail_marks_legacy_comment_unknown(client):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy thread")
        kb.add_comment(conn, tid, author="apollo", body="historical")
    finally:
        conn.close()

    c = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["comments"][0]
    assert c["session_ref"] is None and c["run_id"] is None
    assert "unknown" in c["author_display"]


def test_dashboard_post_comment_stamps_its_own_session(client, monkeypatch):
    """A comment authored through the dashboard gets the dashboard process's
    session fingerprint — the payload cannot supply one."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ui card")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_SESSION_ID", "dashboard-session-1")
    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/comments",
        json={"body": "from the UI", "session_ref": "ffffffffffff", "run_id": 999},
    )
    assert r.status_code == 200

    conn = kb.connect()
    try:
        c = kb.list_comments(conn, tid)[0]
    finally:
        conn.close()
    assert c.session_ref == kb.derive_session_ref("dashboard-session-1")
    assert c.run_id is None


def test_bundle_renders_author_display():
    """The shipped bundle must render the provenance string. A backend-only
    change leaves the board exactly as ambiguous as it was during the
    incident."""
    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    src = bundle.read_text(encoding="utf-8")
    assert "author_display" in src, (
        "dashboard bundle never reads author_display — comment provenance is "
        "invisible in the UI"
    )
    # The comment-thread renderer specifically (not just the child-results
    # block that reuses the same CSS class).
    assert "c.author_display || c.author" in src
