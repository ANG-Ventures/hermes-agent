"""foreign_lane.shim_model_cap: cap the foreign-lane shim's model at spawn.

Model-switching spec 4.4 follow-up. The shim follows the card's model
(Q-M1 = B); a profile that sets ``foreign_lane.shim_model_cap`` spawns the
shim at the cap when the card's model ranks above it. Unset = today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(kb.SHIM_MODEL_CAPPED_FROM_ENV, raising=False)
    kb.init_db()
    return home


CAP = "foreign_lane:\n  harness: claude-code-tui\n  shim_model_cap: claude-sonnet-5\n"


def _profile(kanban_home, cap_block: str) -> None:
    prof = kanban_home / "profiles" / "cc-worker"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(
        "model:\n  provider: claude-bpr\n  default: claude-haiku-4-5\n" + cap_block,
        encoding="utf-8",
    )


def _spawn(monkeypatch, model: str, provider: str | None = "claude-bpr"):
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="shim cap", assignee="cc-worker")
        conn.execute(
            "UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
            (model, provider, tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)
    assert kbd._default_spawn(task, str(kbw.resolve_workspace(task))) == 4242
    argv = captured["cmd"]
    return argv[argv.index("-m") + 1], argv, captured["env"]


def test_cap_applied_when_card_model_ranks_above(kanban_home, monkeypatch):
    _profile(kanban_home, CAP)
    model, argv, env = _spawn(monkeypatch, "claude-opus-5")
    assert model == "claude-sonnet-5"
    assert argv.count("-m") == 1
    assert argv[argv.index("--provider") + 1] == "claude-bpr"  # provider kept
    assert env[kb.SHIM_MODEL_CAPPED_FROM_ENV] == "claude-opus-5"


def test_cap_applied_to_provider_partition_form(kanban_home, monkeypatch):
    _profile(kanban_home, CAP)
    model, argv, env = _spawn(monkeypatch, "claude-bpr/claude-opus-5", provider=None)
    assert model == "claude-sonnet-5"
    assert argv[argv.index("--provider") + 1] == "claude-bpr"
    assert env[kb.SHIM_MODEL_CAPPED_FROM_ENV] == "claude-opus-5"


@pytest.mark.parametrize("card_model", [
    "claude-sonnet-5", "claude-haiku-4-5", "gpt-5.5",
    "acme/opus-v1",  # non-Claude alias with a family word: never capped
    "claude-custom-opus-v1",  # claude- prefixed custom alias: never capped
])
def test_cap_not_applied_at_or_below_cap_or_unranked(kanban_home, monkeypatch, card_model):
    _profile(kanban_home, CAP)
    model, _argv, env = _spawn(monkeypatch, card_model)
    assert model == card_model
    assert kb.SHIM_MODEL_CAPPED_FROM_ENV not in env


@pytest.mark.parametrize("block", [
    "",                                           # no foreign_lane section
    "foreign_lane:\n  harness: claude-code-tui\n",  # section, no cap
    "foreign_lane:\n  shim_model_cap: ''\n",        # empty
    "foreign_lane:\n  shim_model_cap: null\n",      # null
    # cap set but not a foreign-lane shim profile (no harness): not honoured
    "foreign_lane:\n  shim_model_cap: claude-sonnet-5\n",
])
def test_cap_unset_is_todays_behaviour(kanban_home, monkeypatch, block):
    _profile(kanban_home, block)
    model, _argv, env = _spawn(monkeypatch, "claude-opus-5")
    assert model == "claude-opus-5"
    assert kb.SHIM_MODEL_CAPPED_FROM_ENV not in env


def test_inherited_capped_from_env_never_leaks(kanban_home, monkeypatch):
    """A stale marker in the dispatcher env must not ride into an uncapped spawn."""
    _profile(kanban_home, "")
    monkeypatch.setenv(kb.SHIM_MODEL_CAPPED_FROM_ENV, "claude-opus-5")
    _model, _argv, env = _spawn(monkeypatch, "claude-opus-5")
    assert kb.SHIM_MODEL_CAPPED_FROM_ENV not in env


def test_rank_orders_families():
    assert kb._shim_model_rank("claude-haiku-4-5") < kb._shim_model_rank("claude-sonnet-5")
    assert kb._shim_model_rank("claude-sonnet-5") < kb._shim_model_rank("anthropic/claude-opus-5")
    assert kb._shim_model_rank("gpt-5.5") is None
    assert kb._shim_model_rank("acme/opus-v1") is None
    assert kb._shim_model_rank("claude-custom-opus-v1") is None
    assert kb._shim_model_rank("claude-opus-v1") is None
    assert kb._shim_model_rank("claude-haiku-4-5-20251001") == 1


@pytest.mark.parametrize("cap, card_provider, expected", [
    ("claude-bpr/claude-sonnet-5", "claude-bpr", "claude-sonnet-5"),  # same provider: capped
    ("anthropic/claude-sonnet-5", "claude-bpr", "claude-opus-5"),     # other provider: refused
    ("anthropic/claude-sonnet-5", None, "claude-opus-5"),             # no card provider: refused
])
def test_provider_qualified_cap_only_on_matching_provider(
    kanban_home, monkeypatch, cap, card_provider, expected,
):
    _profile(kanban_home, f"foreign_lane:\n  harness: claude-code-tui\n  shim_model_cap: {cap}\n")
    model, _argv, env = _spawn(monkeypatch, "claude-opus-5", provider=card_provider)
    assert model == expected
    assert (kb.SHIM_MODEL_CAPPED_FROM_ENV in env) == (expected != "claude-opus-5")
