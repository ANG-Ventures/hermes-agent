"""The 272K price-tier notice is display-only, once per session (t_a56d83c1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import codex_tier_notice as notice
from agent import model_metadata as mm

pytestmark = pytest.mark.real_codex_context_policy


def _agent(session="session-A", model="gpt-6.1-sol", provider="openai-codex", enabled=True):
    sent = []
    agent = SimpleNamespace(
        session_id=session,
        model=model,
        provider=provider,
        _codex_tier_notice_enabled=enabled,
        _codex_tier_notice_shown=False,
        context_compressor=SimpleNamespace(threshold_tokens=450_000),
        _emit_status=sent.append,
    )
    return agent, sent


@pytest.fixture(autouse=True)
def isolated_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(notice, "_marker_path", lambda: tmp_path / "notice.sessions")
    notice._SEEN_SESSIONS.clear()
    yield
    notice._SEEN_SESSIONS.clear()


@pytest.fixture
def policy(monkeypatch):
    mode = ["large"]
    monkeypatch.setattr(mm, "codex_context_policy", lambda: mode[0])
    return mode


def test_crossing_emits_once_and_persists_across_agent_rebuild_and_restart(policy):
    agent, sent = _agent()
    assert notice.maybe_emit_codex_tier_notice(agent, 272_000) is None
    assert notice.maybe_emit_codex_tier_notice(agent, 272_001) == sent[0]
    assert len(sent) == 1
    assert "gpt-6.1-sol" in sent[0]
    assert "2× input" in sent[0]
    assert "450K" in sent[0]
    assert "model.codex_context_policy: advertised" in sent[0]
    assert notice.maybe_emit_codex_tier_notice(agent, 400_000) is None
    rebuilt, rebuilt_sent = _agent()
    assert notice.maybe_emit_codex_tier_notice(rebuilt, 400_000) is None
    assert rebuilt_sent == []
    notice._SEEN_SESSIONS.clear()  # process restart
    restarted, restarted_sent = _agent()
    assert notice.maybe_emit_codex_tier_notice(restarted, 400_000) is None
    assert restarted_sent == []
    distinct, distinct_sent = _agent(session="session-B")
    assert notice.maybe_emit_codex_tier_notice(distinct, 400_000) == distinct_sent[0]


@pytest.mark.parametrize(
    "model,provider,enabled,mode",
    [
        ("gpt-6.1-sol", "openai-codex", True, "advertised"),
        ("gpt-6.1-sol", "openai-codex", False, "large"),
        ("gpt-6.1-sol", "openai", True, "large"),
        ("gpt-5.5", "openai-codex", True, "large"),
    ],
)
def test_no_notice_outside_large_codex(policy, model, provider, enabled, mode):
    policy[0] = mode
    agent, sent = _agent(model=model, provider=provider, enabled=enabled)
    assert notice.maybe_emit_codex_tier_notice(agent, 500_000) is None
    assert sent == []


def test_status_only_never_mutates_wire_messages(policy):
    agent, sent = _agent()
    wire = [{"role": "user", "content": "large prompt"}]
    original = [m.copy() for m in wire]
    assert notice.maybe_emit_codex_tier_notice(agent, 272_001)
    assert wire == original
    assert len(sent) == 1


def test_unmeasured_prompt_does_not_trigger(policy):
    agent, sent = _agent()
    for value in (None, "unavailable", 0, 272_000):
        assert notice.maybe_emit_codex_tier_notice(agent, value) is None
    assert sent == []
