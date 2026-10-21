"""Kanban dashboard plugin: board rename + delete over the REST surface.

The desktop board switcher drives both from its kebab menu and reads
``result.new_path`` back to tell the user where an archived board went, so
these assert the response shape as well as the on-disk outcome.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_lifecycle_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_rename_board_keeps_slug(client):
    client.post("/api/plugins/kanban/boards", json={"slug": "widget", "name": "Widget"})

    r = client.patch("/api/plugins/kanban/boards/widget", json={"name": "Gadget"})
    assert r.status_code == 200, r.text
    assert r.json()["board"] == {**r.json()["board"], "slug": "widget", "name": "Gadget"}

    listed = next(b for b in client.get("/api/plugins/kanban/boards").json()["boards"] if b["slug"] == "widget")
    assert listed["name"] == "Gadget"


def test_delete_board_archives(client):
    client.post("/api/plugins/kanban/boards", json={"slug": "widget", "name": "Widget"})
    client.post("/api/plugins/kanban/tasks?board=widget", json={"title": "do the thing"})

    r = client.delete("/api/plugins/kanban/boards/widget")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["action"] == "archived"
    assert "current" not in body  # S7 D5: there is no current board

    # The switcher shows this path, so it must point at real recoverable data.
    archived = Path(body["result"]["new_path"])
    assert archived.is_dir()
    assert (archived / "kanban.db").exists()
    assert not kb.board_dir("widget").exists()

    assert all(b["slug"] != "widget" for b in client.get("/api/plugins/kanban/boards").json()["boards"])


def test_delete_board_hard_leaves_nothing(client):
    client.post("/api/plugins/kanban/boards", json={"slug": "widget"})

    r = client.delete("/api/plugins/kanban/boards/widget?delete=true")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["action"] == "deleted"
    assert not kb.board_dir("widget").exists()


def test_delete_default_board_is_refused(client):
    r = client.delete("/api/plugins/kanban/boards/default")
    assert r.status_code == 400
    assert "default" in r.json()["detail"]


# --- S7 D5: no current board on the dashboard ------------------------------------------------

def test_board_query_omitted_is_400_with_the_next_command(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 400
    assert "board required" in r.json()["detail"]
    assert "--board <slug>" in r.json()["detail"]
    assert client.get("/api/plugins/kanban/board?board=default").status_code == 200


def test_kill_switch_default_serves_the_default_board(client, tmp_path):
    (tmp_path / ".hermes" / "config.yaml").write_text("kanban:\n  bare_verb: default\n", encoding="utf-8")
    assert client.get("/api/plugins/kanban/board").status_code == 200


def test_set_current_endpoint_and_field_are_gone(client):
    client.post("/api/plugins/kanban/boards", json={"slug": "widget"})
    assert client.post("/api/plugins/kanban/boards/widget/switch").status_code in (404, 405)
    body = client.get("/api/plugins/kanban/boards").json()
    assert "current" not in body
    assert all("is_current" not in b for b in body["boards"])
    assert not (tmp_path_of(client) / "kanban" / "current").exists()


def tmp_path_of(_client) -> Path:
    import os

    return Path(os.environ["HERMES_HOME"])
