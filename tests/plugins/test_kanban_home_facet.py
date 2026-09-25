"""Home-session dashboard projection and gateway link contracts."""

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_home_facet_api", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    return TestClient(app), module


def test_board_projects_home_channel_without_mutating_cards(board, monkeypatch):
    client, module = board
    with kb.connect() as conn:
        known = kb.create_task(conn, title="known", created_by="user", session_id="session-a")
        unknown = kb.create_task(conn, title="unknown", created_by="user", session_id="session-b")
        blank = kb.create_task(conn, title="blank", created_by="dashboard")
    monkeypatch.setattr(module, "_session_channel_origins", lambda ids: {
        "session-a": ("discord", "123"), "session-b": ("discord", "999")
    })
    monkeypatch.setattr(module, "_channel_display_names", lambda: {("discord", "123"): "#builds"})
    data = client.get("/api/plugins/kanban/board").json()
    tasks = {t["id"]: t for col in data["columns"] for t in col["tasks"]}
    assert tasks[known]["home_channel"] == "#builds"
    assert tasks[unknown]["home_channel"] == "999"
    assert tasks[blank]["home_channel"] is None
    assert tasks[known]["session_id"] == "session-a"
    with kb.connect() as conn:
        stored = kb.get_task(conn, known)
        assert stored is not None and stored.session_id == "session-a"


def test_gateway_dashboard_link_preserves_public_base_path_and_session(monkeypatch):
    from gateway.kanban_dashboard_link import dashboard_link

    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url",
                        lambda: "https://example.test/hermes")
    assert dashboard_link("session id/with?chars") == (
        "https://example.test/hermes/kanban?session=session%20id%2Fwith%3Fchars"
    )
    assert dashboard_link(None) == "https://example.test/hermes/kanban"
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "")
    assert dashboard_link("session-a") is None


def test_board_reads_session_and_directory_in_one_projection(board):
    client, _ = board
    home = Path.home() / ".hermes"
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT, source TEXT, chat_id TEXT)")
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [
            ("s1", "discord", "123"), ("s2", "discord", "456"),
        ])
    (home / "channel_directory.json").write_text(json.dumps({
        "platforms": {"discord": [{"id": "123", "name": "#builds"}]}
    }))
    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", session_id="s1")
        second = kb.create_task(conn, title="second", session_id="s2")
    data = client.get("/api/plugins/kanban/board").json()
    tasks = {t["id"]: t for col in data["columns"] for t in col["tasks"]}
    assert tasks[first]["home_channel"] == "#builds"
    assert tasks[second]["home_channel"] == "456"


def test_board_finds_session_origin_in_named_profile_state(board):
    client, _ = board
    home = Path.home() / ".hermes"
    profile = home / "profiles" / "apollo"
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT, source TEXT, chat_id TEXT)")
        db.execute("INSERT INTO sessions VALUES ('profile-session', 'discord', '777')")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="from profile", session_id="profile-session")
    data = client.get("/api/plugins/kanban/board").json()
    tasks = {t["id"]: t for col in data["columns"] for t in col["tasks"]}
    assert tasks[task_id]["home_channel"] == "777"


def test_gateway_kanban_dashboard_reply_carries_invoking_session(monkeypatch):
    from gateway.slash_commands import GatewaySlashCommandsMixin

    async def entry_for(_key):
        return SimpleNamespace(session_id="invoking-session")

    gateway = SimpleNamespace(
        async_session_store=SimpleNamespace(entry_for=entry_for),
        _session_key_for_source=lambda _source: "chat-key",
    )
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url",
                        lambda: "https://example.test/hermes")
    event = SimpleNamespace(text="/kanban dashboard", source=SimpleNamespace())
    result = asyncio.run(GatewaySlashCommandsMixin._handle_kanban_command(
        cast(Any, gateway), cast(Any, event)))
    assert result == "https://example.test/hermes/kanban?session=invoking-session"


def _cards(client, url):
    data = client.get(url).json()
    return data, {t["id"]: t for col in data["columns"] for t in col["tasks"]}


def test_viewer_home_uses_session_lineage_not_exact_id(board):
    client, module = board
    home = module._state_db_paths()[0].parent
    with sqlite3.connect(home / "state.db") as db:
        db.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, chat_id TEXT, "
            "session_key TEXT, parent_session_id TEXT)"
        )
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", [
            ("old", "discord", "1", "chat-a", None),
            ("new", "discord", "1", "chat-a", "old"),
            ("other", "discord", "2", "chat-b", None),
        ])
    with kb.connect() as conn:
        before = kb.create_task(conn, title="before rotation", session_id="old")
        after = kb.create_task(conn, title="after rotation", session_id="new")
        foreign = kb.create_task(conn, title="other chat", session_id="other")
        bare = kb.create_task(conn, title="unstamped")
    data, cards = _cards(client, "/api/plugins/kanban/board?session=new")
    assert data["viewer_home_ids"] == ["new", "old"]
    assert cards[before]["in_viewer_home"] and cards[after]["in_viewer_home"]
    assert not cards[foreign]["in_viewer_home"]
    assert not cards[bare]["in_viewer_home"]
    _, unscoped = _cards(client, "/api/plugins/kanban/board")
    assert not any(t["in_viewer_home"] for t in unscoped.values())


def test_viewer_home_fails_open_to_exact_id(board):
    client, _ = board
    with kb.connect() as conn:
        mine = kb.create_task(conn, title="mine", session_id="unknown-session")
    data, cards = _cards(client, "/api/plugins/kanban/board?session=unknown-session")
    assert data["viewer_home_ids"] == ["unknown-session"]
    assert cards[mine]["in_viewer_home"]


def test_worker_created_requires_worker_provenance_event(board):
    client, _ = board
    with kb.connect() as conn:
        by_worker = kb.create_task(conn, title="worker card", created_by="daedalus")
        interactive = kb.create_task(conn, title="cli card", created_by="daedalus")
        kb._append_event(conn, by_worker, "parked_by_policy",
                         {"status": "triage", "worker_task_id": "t_parent"})
        kb._append_event(conn, interactive, "parked_by_policy", {"status": "triage"})
        conn.commit()
    _, cards = _cards(client, "/api/plugins/kanban/board")
    assert cards[by_worker]["worker_created"] is True
    assert cards[interactive]["worker_created"] is False
