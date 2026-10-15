"""memory.background_review_flat_file_writes: false refuses every memory-tool op in an
unattended background review (nothing applied, nothing staged); default/true keeps the
add-applies / replace-remove-staged gate; attended and foreground paths are unchanged.

Driven through the registry dispatch with a real temp HERMES_HOME config.yaml."""

import json

import pytest

from tools.memory_tool import FLAT_FILE_REVIEW_REFUSAL, MemoryStore
from tools.skill_provenance import (
    reset_current_write_origin, reset_review_attended, set_current_write_origin, set_review_attended)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path / "memories")
    (tmp_path / "memories").mkdir()

    def configure(knob):
        body = "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n"
        if knob is not None:
            body += f"  background_review_flat_file_writes: {knob}\n"
        (tmp_path / "config.yaml").write_text(body)
        store = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
        store.load_from_disk()
        store.add("memory", "seed entry rendered from templates")
        return store
    return configure


def _call(store, attended=False, **args):
    from tools.registry import registry
    token = set_current_write_origin("background_review")
    att = set_review_attended(True) if attended else None
    try:
        return json.loads(registry.dispatch("memory", args, store=store))
    finally:
        if att is not None:
            reset_review_attended(att)
        reset_current_write_origin(token)


def _pending(tmp_path):
    d = tmp_path / "pending" / "memory"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _disk(tmp_path):
    return "".join(p.read_text() for p in (tmp_path / "memories").glob("*.md"))


_OPS = [
    pytest.param({"action": "add", "content": "a new fact"}, id="add"),
    pytest.param({"action": "add", "target": "user", "content": "a user fact"}, id="add-user"),
    pytest.param({"action": "replace", "old_text": "seed entry", "content": "rewritten"}, id="replace"),
    pytest.param({"action": "remove", "old_text": "seed entry"}, id="remove"),
    pytest.param({"operations": [{"action": "add", "content": "batched add"}]}, id="batch-add"),
    pytest.param({"operations": [{"action": "remove", "old_text": "seed entry"},
                                 {"action": "add", "content": "batched add"}]}, id="batch-remove"),
]


@pytest.mark.parametrize("knob", ["false", "off", "'false'"])
@pytest.mark.parametrize("args", _OPS)
def test_knob_false_refuses_every_unattended_op(home, tmp_path, knob, args):
    store = home(knob)
    before = _disk(tmp_path)
    out = _call(store, **args)
    assert out.get("success") is False
    assert FLAT_FILE_REVIEW_REFUSAL in out["error"]
    assert "staged" not in out
    assert _pending(tmp_path) == []
    assert _disk(tmp_path) == before
    assert store._entries_for("memory") == ["seed entry rendered from templates"]
    assert store._entries_for("user") == []


@pytest.mark.parametrize("knob", [None, "true"])
def test_default_and_true_keep_add_applies_and_delete_staged(home, tmp_path, knob):
    store = home(knob)
    added = _call(store, action="add", content="a new fact")
    assert added["success"] is True and "a new fact" in store._entries_for("memory")
    staged = _call(store, action="remove", old_text="seed entry")
    assert staged["proposal_staged"] is True
    assert len(_pending(tmp_path)) == 1
    assert "seed entry rendered from templates" in store._entries_for("memory")


def test_knob_false_attended_review_keeps_full_operation_set(home, tmp_path):
    store = home("false")
    out = _call(store, attended=True, action="replace", old_text="seed entry", content="rewritten by refine")
    assert out["success"] is True
    assert store._entries_for("memory") == ["rewritten by refine"]
    assert _pending(tmp_path) == []


def test_knob_false_foreground_unaffected(home, tmp_path):
    from tools.registry import registry
    store = home("false")
    out = json.loads(registry.dispatch("memory", {"action": "remove", "old_text": "seed entry"}, store=store))
    assert out["success"] is True
    assert store._entries_for("memory") == []
