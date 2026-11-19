"""A message steered into a running turn keeps its inbound record (t_b8fbfc54).

Live 2026-11-19 05:39 PST: Ace's Telegram voice note reached a mid-turn Apollo session as an
OUT-OF-BAND steer, and its user row carried no ``display_metadata`` at all, so the chat-buy gate
(hermes-home ``scripts/shopper-codex/chat_gate.py``) refused it. These tests drive the REAL busy
steer entry point, the REAL agent steer buffer and the REAL steer-row builder.
"""

import ast
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway import inbound_record
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from tests.gateway.test_inbound_record import _event, _msg, _voice_event

REPO = Path(__file__).resolve().parents[2]
ACE = "571820863"


def _agent():
    """A real AIAgent's steer surface (no __init__), as tests/agent/test_steer.py builds it."""
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._model_request_active = threading.Event()
    agent._executing_tools = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.quiet_mode = True
    return agent


def _runner():
    runner = GatewayRunner(config=GatewayConfig())
    runner._should_echo_stt_transcripts = lambda: False
    runner._pending_event_audio_paths = lambda e: list(getattr(e, "media_urls", None) or [])
    return runner


async def _steer(runner, agent, event):
    outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", agent)
    assert outcome.steered, outcome


def _steer_row(agent):
    """The user row the agent writes after its next tool batch (agent_runtime_helpers)."""
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                {"role": "tool", "content": "output", "tool_call_id": "a"}]
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert messages[-1]["role"] == "user" and messages[-1].get("display_kind") == "steer", messages[-1]
    return messages[-1]


def _gate_reads(row):
    """chat_gate._telegram's own reading of a user row's record (hermes-home 19284e822a0): the words it
    classifies, or a refusal reason."""
    rec = (row.get("display_metadata") or {}).get(inbound_record.KEY)
    if not isinstance(rec, dict):
        return "telegram-unrecorded"
    ok = (str(rec.get("platform")) == "telegram" and str(rec.get("chat_id")) == ACE
          and rec.get("forwarded") is False and rec.get("merged") is not True)
    if not ok or str(rec.get("from_id")) != ACE:
        return "approval-not-from-ace"
    if rec.get("edited") is not False:
        return "approval-edited"
    if rec.get("kind") == "voice":
        tx = rec.get("transcript")
        return tx if isinstance(tx, str) and not rec.get("text") else ""
    return str(rec.get("text") or "")


@pytest.mark.asyncio
async def test_a_steered_typed_place_it_row_carries_the_record_the_gate_accepts():
    runner, agent = _runner(), _agent()
    _, ev = _event(text="place it")
    await _steer(runner, agent, ev)
    row = _steer_row(agent)
    assert row["display_metadata"][inbound_record.KEY]["message_id"] == "4242"
    assert _gate_reads(row) == "place it"


@pytest.mark.asyncio
async def test_a_steered_voice_place_it_row_carries_the_gateway_transcript():
    runner, agent = _runner(), _agent()
    ev = _voice_event()
    with patch("tools.transcription_tools.transcribe_audio",
               lambda *a, **k: {"success": True, "transcript": "Place it.", "provider": "parakeet"}):
        await _steer(runner, agent, ev)
    row = _steer_row(agent)
    rec = row["display_metadata"][inbound_record.KEY]
    assert rec["kind"] == "voice" and rec["transcript_source"] == "parakeet"
    assert _gate_reads(row) == "Place it."


@pytest.mark.asyncio
async def test_two_messages_steered_into_one_row_are_merged_and_approve_nothing():
    runner, agent = _runner(), _agent()
    adapter, first = _event(text="place it")
    second = adapter._build_message_event(_msg("and a second one", message_id=4243), first.message_type)
    await _steer(runner, agent, first)
    await _steer(runner, agent, second)
    row = _steer_row(agent)
    assert row["display_metadata"][inbound_record.KEY]["merged"] is True
    assert _gate_reads(row) == "approval-not-from-ace"


@pytest.mark.asyncio
async def test_a_steer_that_bypassed_the_gateway_merges_the_row():
    """Kanban comment-inject / api_server call ``agent.steer`` directly: their text joins the row with no
    record of its own, so the row is not one platform message any more."""
    runner, agent = _runner(), _agent()
    _, ev = _event(text="place it")
    await _steer(runner, agent, ev)
    agent.steer("a kanban comment")
    assert _steer_row(agent)["display_metadata"][inbound_record.KEY]["merged"] is True


@pytest.mark.asyncio
async def test_a_leftover_steer_carries_the_record_into_the_next_turn():
    from gateway.run_busy import leftover_steer_display_metadata, leftover_steer_is_internal
    from gateway.run_turn import _LEFTOVER_STEER_META, _leftover_steer_meta
    runner, agent = _runner(), _agent()
    _, ev = _event(text="place it")
    await _steer(runner, agent, ev)
    leftover = agent._drain_pending_steer()   # turn_finalizer: arrived after the last tool batch
    meta = leftover_steer_display_metadata(agent, leftover)
    assert leftover_steer_is_internal(agent, leftover) is False
    result = {"pending_steer": leftover, _LEFTOVER_STEER_META: meta}
    assert _gate_reads({"display_metadata": _leftover_steer_meta(result, leftover)}) == "place it"
    assert _leftover_steer_meta(result, "an interrupt message") == {}
    # A follow-up already owns the next turn: the leftover is queued as an event that keeps the record.
    runner._queue_leftover_steer(leftover, ev.source, "key", display_metadata=meta)
    queued = runner._session_state("key").conversation.queued_events[-1]
    assert _gate_reads({"display_metadata": inbound_record.display_metadata(queued)}) == "place it"
    # ...and its platform message id, which the row's platform_message_id (restart dedup, telegram:<id>) reads.
    assert queued.message_id == str(ev.message_id)


def test_a_merged_leftover_steer_carries_no_platform_message_id():
    from gateway.run_turn import _leftover_steer_message_id
    rec = {"message_id": "4242", "from_id": "571820863"}
    assert _leftover_steer_message_id({inbound_record.KEY: rec}) == "4242"
    assert _leftover_steer_message_id({inbound_record.KEY: {**rec, "merged": True}}) is None
    assert _leftover_steer_message_id({}) is None


def test_an_internal_or_unrecorded_steer_row_has_no_record():
    agent = _agent()
    agent.steer("plain cli steer")
    assert "display_metadata" not in _steer_row(agent)


# -- the choke point: every writer of a steer-derived user row passes the source of the record -------------------

_WRITERS = {  # callee -> the keyword that carries the record; a literal None is allowed only where noted
    "steer_user_row": "agent",
    "_steer_running_agent": "event",
    "steer_with_provenance": "event",
    "_queue_leftover_steer": "display_metadata",
}


_ROOTS = ("agent", "gateway", "plugins", "tools", "tui_gateway", "hermes_cli", "acp_adapter")


def _sources():
    yield from sorted(REPO.glob("*.py"))
    for root in _ROOTS:
        yield from sorted((REPO / root).glob("**/*.py"))


def _calls(name):
    import warnings
    for path in _sources():
        rel = path.relative_to(REPO).as_posix()
        if "/node_modules/" in rel:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                callee = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if callee == name:
                    yield rel, node


def _imports_and_getattrs(name):
    import warnings
    for path in _sources():
        rel = path.relative_to(REPO).as_posix()
        if "/node_modules/" in rel:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)) and any(
                    a.name == name and a.asname not in (None, name) for a in node.names):
                yield rel, node
            elif (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr" and len(node.args) > 1
                  and isinstance(node.args[1], ast.Constant) and node.args[1].value == name):
                yield rel, node


@pytest.mark.parametrize("callee,keyword", sorted(_WRITERS.items()))
def test_every_steer_row_writer_passes_the_record_source(callee, keyword):
    """A new caller must hand the record's source through (never a literal None in production): the steer
    row is the only user row the agent writes from a platform message on its own (t_b8fbfc54)."""
    found, bad = [], []
    for rel, node in _calls(callee):
        found.append(rel)
        kw = next((k for k in node.keywords if k.arg == keyword), None)
        if kw is None or (isinstance(kw.value, ast.Constant) and kw.value.value is None):
            bad.append(f"{rel}:{node.lineno}")
    assert found, f"no production caller of {callee} found; update _WRITERS"
    # A call through an alias (``import ... as x``) or getattr would escape the name match above.
    aliased = [rel for rel, node in _imports_and_getattrs(callee)]
    assert not aliased, f"{callee} reached through an alias or getattr (the ratchet cannot see it): {aliased}"
    assert not bad, f"{callee} called without {keyword}=<record source>: {bad}"
