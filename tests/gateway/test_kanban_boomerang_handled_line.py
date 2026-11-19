"""t_8ab5557d: the ``boomerang handled`` line leaves the owner's chat.

1. A handback that also delivers a TURN posts no line (the turn is the user-facing message).
2. A handled-only handback writes its line to the fleet agent log, not the chat; the card's
   ``boomerang:`` comment stays.
3. A refused turn still posts its line to the chat.
Knob: ``state/boomerang-handled-line-chat.enabled`` restores the chat line.

Drives the real run entry / delivery against a temp board (fixtures from the dispatch tests). The
fleet library is a stand-in with hermes-home ``scripts/lib/agent_inbox.py``'s write_agent_log contract.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway import kanban_boomerang_wake as ah
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_boomerang_wake_dispatch import (  # noqa: F401 -- env is a fixture
    _card, _claimed_entry, _run, _verdict, _worker_close, env)

LIB = '''
import json
from pathlib import Path
def write_agent_log(root, producer, sev, message, now=None, **extra):
    d = Path(root) / "state" / "agent-logs"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "day.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"producer": producer, "sev": sev, "text": message, **extra}) + "\\n")
    return d / "day.jsonl"
'''


def _home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


@pytest.fixture
def fleet_lib(env):
    lib = _home() / "scripts" / "lib" / "agent_inbox.py"
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(LIB, encoding="utf-8")
    yield _home() / "state" / "agent-logs" / "day.jsonl"
    lib.unlink()


@pytest.fixture
def chat_knob(env):
    knob = _home() / "state" / ah.HANDLED_LINE_CHAT_FILE
    knob.parent.mkdir(parents=True, exist_ok=True)
    knob.write_text("")
    yield
    knob.unlink()


def _lines(env) -> list:
    return [s for s in env["adapter"].sent if s.startswith("boomerang handled")]


def _log_rows(path) -> list:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _breadcrumbs(tid) -> list:
    with kb.connect_closing() as conn:
        return [c.body for c in kb.list_comments(conn, tid)
                if c.author == ah.AUTHOR and c.body.startswith("boomerang: ")]


def _surface(env):
    tid = _card(env)
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    _run(env, entry, "completed", _verdict(entry, "surface", summary="needs the owner"))
    return tid


def test_surfaced_turn_posts_no_handled_line(env, fleet_lib):
    """1: the surface turn reaches the home; no ``boomerang handled`` line in the chat."""
    tid = _surface(env)
    assert len(env["adapter"].handled) == 1
    assert _lines(env) == []
    assert _breadcrumbs(tid), "the card's boomerang: comment is kept"


def test_complete_turn_posts_no_handled_line(env, fleet_lib):
    """1: a complete verdict that also wakes the chat home (t_aad55db4) posts no line beside it."""
    tid = _card(env)
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    _run(env, entry, "completed", _verdict(entry, "complete", summary="verified: PR merged"))
    assert len(env["adapter"].handled) == 1
    assert _lines(env) == []
    assert _breadcrumbs(tid)
    assert ah.BoomerangWakeState().handbacks() == {}


def _handled_only(env, tid=None):
    tid = tid or _card(env)
    _worker_close(tid)
    entry = _claimed_entry(env, tid)
    state = ah.BoomerangWakeState()
    ah.enqueue_handback(entry, {"summary": "send-back written", "action": "request_changes"}, state=state)
    asyncio.run(ah.deliver_handbacks(env["runner"], state))
    return tid, entry


def test_handled_only_line_goes_to_the_agent_log(env, fleet_lib):
    """2: no turn; the line is one agent-log row and never reaches the chat; the breadcrumb stays."""
    tid, entry = _handled_only(env)
    assert _lines(env) == [] and env["adapter"].handled == []
    rows = _log_rows(fleet_lib)
    assert [(r["producer"], r["text"]) for r in rows] == [
        ("boomerang-wake", f"boomerang handled {tid} (completed): send-back written")]
    assert rows[0]["task_id"] == tid and rows[0]["delegation_id"] == entry["delegation_id"]
    assert _breadcrumbs(tid) == [f"boomerang: completed send-back written (delegation {entry['delegation_id']})"]
    assert ah.BoomerangWakeState().handbacks() == {}


def test_handled_only_line_without_agent_log_posts_to_chat(env):
    """2, fail open: no fleet library on this host, so the line is posted rather than lost."""
    tid, _ = _handled_only(env)
    assert _lines(env) == [f"boomerang handled {tid} (completed): send-back written"]


def test_refused_turn_still_posts_its_line(env, fleet_lib):
    """3: the turn gets no admission receipt; the line goes to the chat as the warning."""
    env["adapter"].fail_turns = 1
    tid = _surface(env)
    assert env["adapter"].handled == []
    assert _lines(env) == [f"boomerang handled {tid} (completed): needs the owner"]
    asyncio.run(ah.deliver_handbacks(env["runner"], ah.BoomerangWakeState()))   # the retry delivers the turn
    assert len(env["adapter"].handled) == 1
    assert len(_lines(env)) == 1


@pytest.mark.parametrize("shape", ["surface", "handled_only"])
def test_knob_restores_the_chat_line(env, fleet_lib, chat_knob, shape):
    """4: with the knob file present both shapes post the line to the chat, as before; no log row."""
    tid = _surface(env) if shape == "surface" else _handled_only(env)[0]
    assert len(_lines(env)) == 1 and _lines(env)[0].startswith(f"boomerang handled {tid} ")
    assert _log_rows(fleet_lib) == []
