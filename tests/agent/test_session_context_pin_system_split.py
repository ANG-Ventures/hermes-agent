"""A conversation that was sent ``system`` as ``[static, suffix]`` keeps that split (class I, t_91b6a4c4).

Attributed by t_d2892ff6 (apx PR #251) from the sub-0 wirelog: hermes-agent sent ``system`` as two marked
blocks while it knew the static prefix, then on one request (session 695fbaef, pair 50->51) collapsed to ONE
marked block equal to static+suffix. Only the full prompt was persisted; a fresh agent re-derived the split from
today's builder (``reconstruct_static_prefix``), and once the stable tier had changed (deploy, SOUL/skills edit)
the derivation failed and the request fell back to one block. The fix persists the split point with the
session. Real SessionDB, real restore, real cache planner; oracle: the request's system blocks.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.conversation_loop import _persist_system_prompt, _restore_or_build_system_prompt
from agent.prompt_caching import build_prompt_cache_plan
from hermes_state import SessionDB

STATIC = "You are Hermes.\n\n" + "stable guidance " * 40
SUFFIX = "\n\n## Skills\n- a\n\nConversation started: Thursday, October 15, 2026\nModel: m\nProvider: p"


def _agent(db, session_id):
    agent = MagicMock()
    agent.session_id, agent.model, agent.provider, agent.platform = session_id, "m", "p", "discord"
    agent._session_db, agent._use_prompt_caching, agent.pass_session_id = db, True, False
    agent._persist_disabled = False
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent.tools = []
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None
    agent._static_rebuild_failed_for = None
    return agent


def _system_blocks(agent):
    messages = [{"role": "system", "content": agent._cached_system_prompt}, {"role": "user", "content": "hi"}]
    static = agent._cached_system_prompt_static
    plan = build_prompt_cache_plan(messages, [], native_anthropic=True,
                                   static_system_prefix=static if isinstance(static, str) else None)
    content = plan.messages[0]["content"]
    return [part["text"] for part in content] if isinstance(content, list) else [content]


def test_session_context_pin_keeps_two_block_system_split(tmp_path, monkeypatch):
    import agent.system_prompt as sp

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-i", source="discord")

    # Turn N: the first build knows the static tier and is sent as [static, suffix].
    first = _agent(db, "sess-i")
    first._cached_system_prompt, first._cached_system_prompt_static = STATIC + SUFFIX, STATIC
    _persist_system_prompt(first, "persist failed %s %s")
    assert _system_blocks(first) == [STATIC, SUFFIX]

    # Turn N+1 on a fresh agent after a deploy changed the stable tier the builder renders today.
    monkeypatch.setattr(sp, "build_system_prompt_parts",
                        lambda agent, system_message=None: {"stable": "You are Hermes v2.", "context": "", "volatile": ""})
    second = _agent(db, "sess-i")
    _restore_or_build_system_prompt(second, None, [{"role": "user", "content": "earlier"}])

    assert second._cached_system_prompt == STATIC + SUFFIX
    assert _system_blocks(second) == [STATIC, SUFFIX]
