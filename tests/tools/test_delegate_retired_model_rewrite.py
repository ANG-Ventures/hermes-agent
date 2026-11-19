"""G7 (t_1e1450d1): delegate_task rewrites a RETIRED model id to its successor.

Six subagent turns went to ``gpt-6.1-sol`` after its retirement because long-lived
sessions copied ``{"model": "gpt-6.1-sol", "provider": "cpa"}`` out of their own context.
"""
import json

import pytest

from tools import delegate_tool
from hermes_cli.model_registry import retired_rewrite

RETIRED = (
    "# GENERATED from config/model-registry.json\n"
    "gpt-5.6-sol cutover=2026-09-23T07:20-07:00\n"
    "gpt-6-sol cutover=2026-10-02T11:36-07:00\n"
    "gpt-6.1-sol cutover=2026-11-18T21:52-08:00  # trailing comment\n"
)
SUCCESSORS = {"_comment": "x", "successors": {"gpt-6.1-sol": "gpt-6.2-sol", "gpt-5.6-sol": "gpt-6-sol"}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    (root / "config" / "model-retired-ids.txt").write_text(RETIRED)
    (root / "config" / "model-successors.json").write_text(json.dumps(SUCCESSORS))
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def test_retired_id_rewritten_with_note(home):
    new, note = retired_rewrite("gpt-6.1-sol")
    assert new == "gpt-6.2-sol"
    assert "gpt-6.1-sol -> gpt-6.2-sol" in note and "retired" in note and "2026-11-18T21:52-08:00" in note


def test_lane_prefix_is_kept(home):
    assert retired_rewrite("cpa/gpt-6.1-sol")[0] == "cpa/gpt-6.2-sol"


def test_live_id_untouched(home):
    assert retired_rewrite("gpt-6.2-sol") == ("gpt-6.2-sol", None)
    assert retired_rewrite("claude-opus-5-5") == ("claude-opus-5-5", None)


def test_chain_ending_on_a_retired_id_without_successor_is_not_rewritten(home):
    # gpt-5.6-sol -> gpt-6-sol, which is itself retired with no successor: never hand back a retired id.
    assert retired_rewrite("gpt-5.6-sol") == ("gpt-5.6-sol", None)


@pytest.mark.parametrize("missing", ["model-retired-ids.txt", "model-successors.json"])
def test_missing_file_fails_open(home, missing):
    (home / "config" / missing).unlink()
    assert retired_rewrite("gpt-6.1-sol") == ("gpt-6.1-sol", None)


def test_unreadable_successors_fails_open(home):
    (home / "config" / "model-successors.json").write_text("{not json")
    assert retired_rewrite("gpt-6.1-sol") == ("gpt-6.1-sol", None)


def _capture(monkeypatch, result):
    seen = {}

    def fake_impl(*args, **kwargs):
        seen.update(kwargs)
        return result

    monkeypatch.setattr(delegate_tool, "_delegate_task_impl", fake_impl)
    return seen


def test_delegate_task_rewrites_and_notes_the_result(home, monkeypatch):
    seen = _capture(monkeypatch, json.dumps({"status": "dispatched"}))
    out = json.loads(delegate_tool.delegate_task(goal="g", model="gpt-6.1-sol", provider="cpa", parent_agent=object()))
    assert seen["model"] == "gpt-6.2-sol" and seen["provider"] == "cpa"
    assert out["status"] == "dispatched"
    assert out["model_rewritten"].startswith("model rewritten: gpt-6.1-sol -> gpt-6.2-sol")


def test_delegate_task_non_json_result_gets_a_note_line(home, monkeypatch):
    _capture(monkeypatch, "plain text")
    out = delegate_tool.delegate_task(goal="g", model="gpt-6.1-sol", parent_agent=object())
    assert out.splitlines()[0] == "plain text"
    assert out.splitlines()[1].startswith("model rewritten: gpt-6.1-sol -> gpt-6.2-sol")


def test_delegate_task_live_id_untouched(home, monkeypatch):
    seen = _capture(monkeypatch, json.dumps({"status": "ok"}))
    out = json.loads(delegate_tool.delegate_task(goal="g", model="gpt-6.2-sol", parent_agent=object()))
    assert seen["model"] == "gpt-6.2-sol" and "model_rewritten" not in out


def test_delegate_task_force_model_keeps_the_retired_id(home, monkeypatch):
    seen = _capture(monkeypatch, json.dumps({"status": "ok"}))
    out = json.loads(delegate_tool.delegate_task(goal="g", model="gpt-6.1-sol", force_model="prism re-cert",
                                                 parent_agent=object()))
    assert seen["model"] == "gpt-6.1-sol" and "model_rewritten" not in out


def test_delegate_task_missing_files_fail_open(home, monkeypatch):
    (home / "config" / "model-retired-ids.txt").unlink()
    seen = _capture(monkeypatch, json.dumps({"status": "ok"}))
    out = json.loads(delegate_tool.delegate_task(goal="g", model="gpt-6.1-sol", parent_agent=object()))
    assert seen["model"] == "gpt-6.1-sol" and "model_rewritten" not in out


def test_live_agent_dispatch_forwards_force_model(monkeypatch):
    """Prism 9dbbad0141a8: the inline dispatch path (run_agent._dispatch_delegate_task) must pass force_model."""
    import run_agent

    seen = {}
    monkeypatch.setattr(delegate_tool, "delegate_task", lambda **kw: seen.update(kw) or "{}")
    run_agent.AIAgent._dispatch_delegate_task(object.__new__(run_agent.AIAgent),
                                              {"goal": "g", "model": "gpt-6.1-sol", "force_model": "prism re-cert"})
    assert seen["force_model"] == "prism re-cert"
