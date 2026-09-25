"""A compaction that runs on a fallback model must persist the PRIMARY identity.

Observed 2026-09-24 (t_d47cefd1): the primary (claude-fable-5-1) hit its limit,
the turn fell back to claude-opus-5-5, and threshold/pre-API compaction ran
mid-turn on the fallback. The rebuilt prompt said ``Model: claude-opus-5-5``
and was written to the session row. The next turn restored the primary, so
``_stored_prompt_matches_runtime`` rejected the row and rebuilt the prompt
(cold prefix cache) even though model and provider had not changed between
turns. The in-memory prompt keeps the fallback identity (the model must know
what is answering); only the stored bytes carry the primary's labels, matching
``rewrite_prompt_model_identity``'s "stored row keeps the primary's labels".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import (
    _restore_or_build_system_prompt,
    _stored_prompt_matches_runtime,
    _stored_prompt_runtime_mismatch,
)
from hermes_state import SessionDB

PRIMARY_MODEL = "test/model"
FALLBACK_MODEL = "fallback/model"
FALLBACK_PROVIDER = "fallback-provider"


def _agent(db: SessionDB, session_id: str, *, in_place: bool):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model=PRIMARY_MODEL,
            provider="openrouter",
            platform="discord",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    compressor = MagicMock()
    compressor.compress.return_value = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "tail"},
    ]
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_summary_auth_failure = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent.compression_in_place = in_place
    return agent


def _activate_fallback(agent) -> None:
    """The fields ``try_activate_fallback`` swaps that the prompt reads."""
    assert agent._primary_runtime["model"] == PRIMARY_MODEL
    assert agent._primary_runtime["provider"] == "openrouter"
    agent.model = FALLBACK_MODEL
    agent.provider = FALLBACK_PROVIDER
    agent._fallback_activated = True


def _primary_agent_like(agent):
    """A fresh next-turn agent: primary runtime restored, same cwd/platform."""
    fresh = MagicMock()
    fresh.model = agent._primary_runtime["model"]
    fresh.provider = agent._primary_runtime["provider"]
    fresh.platform = agent.platform
    return fresh


@pytest.mark.parametrize("in_place", [True, False], ids=["in_place", "rotation"])
def test_fallback_compaction_persists_primary_identity(
    tmp_path: Path, in_place: bool, monkeypatch
):
    # Pin the cwd the prompt records so only the identity lines can differ
    # (an import on the compaction path can bridge terminal.cwd into the env).
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sid = "FALLBACK_COMPACT"
        db.create_session(sid, source="discord")
        agent = _agent(db, sid, in_place=in_place)
        _activate_fallback(agent)

        agent._compress_context(
            [{"role": "user", "content": f"m{i}"} for i in range(20)],
            None,
            approx_tokens=120_000,
            trigger_reason="threshold",
        )

        # In memory: the fallback is still answering this turn.
        assert f"Model: {FALLBACK_MODEL}" in agent._cached_system_prompt
        # Stored: the primary identity the next turn will restore.
        row = db.get_session(agent.session_id)
        stored = row["system_prompt"]
        assert stored, "compaction did not persist a system prompt"
        primary_provider = agent._primary_runtime["provider"]
        assert f"Model: {PRIMARY_MODEL}" in stored
        assert f"Model: {FALLBACK_MODEL}" not in stored
        assert f"Provider: {primary_provider}" in stored
        assert FALLBACK_PROVIDER not in stored

        nxt = _primary_agent_like(agent)
        assert _stored_prompt_runtime_mismatch(nxt, stored) is None
        assert _stored_prompt_matches_runtime(nxt, stored) is True
    finally:
        db.close()


def test_no_fallback_compaction_persists_prompt_verbatim(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sid = "PRIMARY_COMPACT"
        db.create_session(sid, source="discord")
        agent = _agent(db, sid, in_place=True)
        agent._compress_context(
            [{"role": "user", "content": f"m{i}"} for i in range(20)],
            None,
            approx_tokens=120_000,
            trigger_reason="threshold",
        )
        assert db.get_session(sid)["system_prompt"] == agent._cached_system_prompt
    finally:
        db.close()


def test_stale_identity_log_names_field_and_values(caplog):
    stored = "You are Hermes Agent.\nModel: old/model\nProvider: openrouter"
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": stored}
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "sid"
    agent.model = "new/model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._build_system_prompt = MagicMock(return_value="REBUILT")

    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

    lines = [r.getMessage() for r in caplog.records if "stale runtime identity" in r.getMessage()]
    assert len(lines) == 1
    assert "Model: stored='old/model' runtime='new/model'" in lines[0]


@pytest.mark.parametrize(
    "prompt, agent_attrs, expected",
    [
        ("Model: a\nProvider: p", {"model": "b", "provider": "p"}, ("Model", "a", "b")),
        ("Model: a\nProvider: p", {"model": "a", "provider": "q"}, ("Provider", "p", "q")),
        ("Model: a\nPlatform: cli", {"model": "a", "platform": "discord"}, ("Platform", "cli", "discord")),
        ("Model: a\nProvider: p\nPlatform: cli", {"model": "a", "provider": "p", "platform": "cli"}, None),
    ],
)
def test_mismatch_reports_first_stale_field(prompt, agent_attrs, expected):
    class _A:
        model = ""
        provider = ""
        platform = ""

    a = _A()
    for k, v in agent_attrs.items():
        setattr(a, k, v)
    assert _stored_prompt_runtime_mismatch(a, prompt) == expected


def test_mismatch_reports_cwd():
    class _A:
        model = "m"
        provider = "p"
        platform = ""

    prompt = (
        "User home directory: /home/t\n"
        "Current working directory: /old\n"
        "Model: m\nProvider: p"
    )
    with patch("agent.conversation_loop.resolve_agent_cwd", return_value="/new"):
        assert _stored_prompt_runtime_mismatch(_A(), prompt) == (
            "Current working directory", "/old", "/new",
        )
