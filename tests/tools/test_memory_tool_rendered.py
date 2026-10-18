"""A rendered pointer file (first entry starts with ``POINTER FILE``) is never written in place.

The fleet rebuilds MEMORY.md / USER.md from templates (scripts/memory-md-render.py); an in-place
memory-tool edit drifted the live file from its render and reddened memory_md_role_consistent.
"""

import json

import pytest

from tools.memory_tool import ENTRY_DELIMITER, MemoryStore, apply_memory_pending, memory_tool

RENDERED = {
    "memory": ENTRY_DELIMITER.join([
        "POINTER FILE (Memory Store Policy). Never add facts here: this file is RENDERED from "
        "fleet/memory-templates by scripts/memory-md-render.py.",
        "Hosts/SSH -> skill ssh-machines.",
        "Kanban: workers pull one card each."]),
    "user": ENTRY_DELIMITER.join([
        "POINTER FILE, one copy fleet-wide. Facts about the user live in mem0.",
        "User: owner/operator, solo."]),
}
FILENAME = {"memory": "MEMORY.md", "user": "USER.md"}

OPS = {
    "add": dict(action="add", content="a brand new fact"),
    "replace": dict(action="replace", old_text="workers pull", content="Kanban: rewritten line"),
    "remove": dict(action="remove", old_text="workers pull"),
    "batch": dict(operations=[{"action": "add", "content": "x"},
                              {"action": "replace", "old_text": "workers pull", "content": "y"}]),
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    return tmp_path


def _store():
    s = MemoryStore(memory_char_limit=5000, user_char_limit=5000)
    s.load_from_disk()
    return s


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("op", sorted(OPS))
def test_write_never_changes_a_rendered_file(home, target, op):
    path = home / FILENAME[target]
    path.write_text(RENDERED[target], encoding="utf-8")
    kwargs = dict(OPS[op])
    if target == "user" and op in ("replace", "remove", "batch"):
        kwargs = json.loads(json.dumps(kwargs).replace("workers pull", "owner/operator"))

    result = json.loads(memory_tool(target=target, store=_store(), **kwargs))

    assert path.read_text(encoding="utf-8") == RENDERED[target]
    assert result["success"] is False and result.get("done") is True
    assert "mem0_conclude" in result["error"]
    assert "fleet/memory-templates/" in result["error"]


@pytest.mark.parametrize("target", ["memory", "user"])
def test_store_level_writes_refuse_a_rendered_file(home, target):
    """The store's own write path (used by /memory approve replay and the gateway) refuses too."""
    path = home / FILENAME[target]
    path.write_text(RENDERED[target], encoding="utf-8")
    store = _store()

    assert store.add(target, "direct add")["success"] is False
    assert apply_memory_pending({"action": "add", "target": target, "content": "staged"}, store)["success"] is False
    assert path.read_text(encoding="utf-8") == RENDERED[target]


@pytest.mark.parametrize("target", ["memory", "user"])
def test_non_rendered_file_still_writes_in_place(home, target):
    path = home / FILENAME[target]
    path.write_text(ENTRY_DELIMITER.join(["plain entry one", "plain entry two"]), encoding="utf-8")

    result = json.loads(memory_tool(action="add", target=target, content="new fact", store=_store()))

    assert result["success"] is True
    assert path.read_text(encoding="utf-8").endswith(ENTRY_DELIMITER + "new fact")


def test_pointer_text_later_in_the_file_does_not_mark_it_rendered(home):
    """Only the FIRST entry marks a file rendered; a fact that mentions the phrase is ordinary."""
    path = home / "MEMORY.md"
    path.write_text("first entry" + ENTRY_DELIMITER + "POINTER FILE mention", encoding="utf-8")

    assert json.loads(memory_tool(action="add", target="memory", content="z", store=_store()))["success"] is True


def test_missing_file_still_writes(home):
    assert json.loads(memory_tool(action="add", target="memory", content="first", store=_store()))["success"] is True
    assert (home / "MEMORY.md").read_text(encoding="utf-8") == "first"
