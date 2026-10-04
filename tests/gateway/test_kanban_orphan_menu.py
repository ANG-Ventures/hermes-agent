"""Orphan-card menu reply round trip (t_6281f908, D-O6/D-O7, D-O5 finality)."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import kanban_orphan_menu as om
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

S_A = "20261003_141500_aaaa"
S_B = "20261003_090000_bbbb"
S_CLI = "20261003_080000_cli"
CHAN_A = "1502228850338435153"
CHAN_B = "1508648744143421561"

# Byte-for-byte what hermes-home scripts/kanban-cross-session-watch.py renders
# (orphan_line + menu_option, pinned in its test_human_orphan_with_several_candidates_gets_numbered_menu).
MENU = (
    "\U0001f3f7 Kanban {a} has no home session/channel (no session) \u2014 created by user "
    "2026-10-03 15:00 PDT \u2014 reply with a number to re-home it, or "
    "`hermes kanban rehome {a} --session <sid>`\n"
    f"  1. {{a}} \u2192 <#{CHAN_A}> \u00b7 session {S_A} (parent t_00000701)\n"
    f"  2. {{a}} \u2192 <#{CHAN_B}> \u00b7 session {S_B} (parent t_00000702)\n"
    "\U0001f3f7 Kanban {b} [subs-ace] has no home session/channel (no session) \u2014 \u2026\n"
    f"  3. {{b}} [subs-ace] \u2192 <#{CHAN_B}> \u00b7 session {S_B} (origin line)"
)


def _origin(platform, chat):
    return json.dumps({"platform": platform, "chat_id": chat, "chat_type": "group"})


ROWS = {S_A: {"origin_json": _origin("discord", CHAN_A)},
        S_B: {"origin_json": _origin("discord", CHAN_B)},
        S_CLI: {"origin_json": json.dumps({"platform": "cli"})}}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "om.db"))
    kb.init_db()
    from gateway import kanban_home_route as khr

    monkeypatch.setattr(khr, "read_session_row", lambda sid: ROWS.get(sid))
    return tmp_path


def _orphan(session=None):
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="orphan", assignee="w", session_id=session,
                              session_explicit=True)


# parse ---------------------------------------------------------------------------

def test_parse_picks_the_numbered_option():
    menu = MENU.format(a="t_0000aaaa", b="t_0000bbbb")
    assert om.parse_choice("2", menu) == om.MenuChoice(2, "t_0000aaaa", "default", S_B)
    assert om.parse_choice(" 1 ", menu) == om.MenuChoice(1, "t_0000aaaa", "default", S_A)
    assert om.parse_choice("3", menu) == om.MenuChoice(3, "t_0000bbbb", "subs-ace", S_B)


@pytest.mark.parametrize("text,replied", [
    ("2", "some other bot message with 2. a list"),       # not a menu
    ("2", None),                                         # not a reply
    ("2 please", MENU.format(a="t_0000aaaa", b="t_0000bbbb")),  # not bare
    ("yes", MENU.format(a="t_0000aaaa", b="t_0000bbbb")),
    ("123", MENU.format(a="t_0000aaaa", b="t_0000bbbb")),       # 3 digits: never a menu choice
])
def test_parse_passes_everything_else_through(text, replied):
    assert om.parse_choice(text, replied) is None


def test_number_not_in_menu_is_answered_not_passed_to_agent():
    choice = om.parse_choice("9", MENU.format(a="t_0000aaaa", b="t_0000bbbb"))
    assert choice is not None and choice.card == ""
    assert om.apply_choice(choice) == "\u26a0 No option 9 in that menu."


# round trip ------------------------------------------------------------------------

def test_reply_round_trip_rehomes_and_subscribes(board):
    a = _orphan()
    choice = om.parse_choice("2", MENU.format(a=a, b="t_0000bbbb"))
    out = om.apply_choice(choice, actor="Ace")
    assert out == f"\u23f3 {a} re-homed to <#{CHAN_B}> (session {S_B}, option 2)."
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).session_id == S_B
        assert [(s["platform"], s["chat_id"]) for s in kbn.list_notify_subs(conn, a)] == [("discord", CHAN_B)]
        comments = [c.body for c in kb.list_comments(conn, a)]
    assert any("orphan menu: re-homed to session" in c and "chosen by Ace" in c for c in comments)


def test_reply_on_a_card_with_no_chat_session_is_allowed(board):
    a = _orphan(S_CLI)                                    # NO-CHANNEL orphan
    om.apply_choice(om.parse_choice("1", MENU.format(a=a, b="t_0000bbbb")))
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).session_id == S_A


@pytest.mark.parametrize("home", [S_A, "operator:apollo"])
def test_rehomes_are_final(board, home):
    """D-O5: a card that has a home already is never moved by the menu."""
    a = _orphan(home)
    out = om.apply_choice(om.parse_choice("2", MENU.format(a=a, b="t_0000bbbb")))
    assert "already has a home" in out and "final" in out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).session_id == home


def test_option_session_without_chat_is_refused(board):
    a = _orphan()
    menu = MENU.format(a=a, b="t_0000bbbb").replace(S_B, S_CLI)
    out = om.apply_choice(om.parse_choice("2", menu))
    assert "has no chat" in out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).unhomed or kb.get_task(conn, a).session_id is None


# gateway wiring --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gateway_intercepts_menu_reply_before_the_agent(board, monkeypatch):
    from gateway import run as gr

    a = _orphan()
    called = {}

    def fake_apply(choice, actor=""):
        called["choice"], called["actor"] = choice, actor
        return "ok"

    monkeypatch.setattr(om, "apply_choice", fake_apply)
    src = SimpleNamespace(platform=None, chat_id=CHAN_A, user_id="u1", user_name="Ace")
    event = SimpleNamespace(text="1", reply_to_text=MENU.format(a=a, b="t_0000bbbb"),
                            reply_to_is_own_message=True)
    reply = await gr._maybe_orphan_menu_reply(event, src)
    assert reply == "ok" and called["choice"].card == a and called["actor"] == "Ace"
    event.text = "hello"
    assert await gr._maybe_orphan_menu_reply(event, src) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("authorship", [
    {"reply_to_is_own_message": False, "reply_to_author_id": "4242"},  # a user's forged menu
    {},                                                                 # platform stamps nothing
])
async def test_forged_menu_from_a_non_bot_author_is_not_executable(board, monkeypatch, authorship):
    """t_3ad14889 (Prism P1 'Untrusted Menus'): option lines are only a menu when the
    replied-to message was posted by this gateway's own bot. A user who copies the
    option shape (naming any card/board/session) and replies to it with a number
    must pass through to the agent: nothing is rehomed, nothing is subscribed."""
    from gateway import run as gr

    a = _orphan()
    monkeypatch.setattr(om, "apply_choice", lambda *a_, **k: pytest.fail("forged menu executed"))
    src = SimpleNamespace(platform=None, chat_id=CHAN_A, user_id="4242", user_name="mallory")
    event = SimpleNamespace(text="2", reply_to_text=MENU.format(a=a, b="t_0000bbbb"), **authorship)
    assert await gr._maybe_orphan_menu_reply(event, src) is None
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, a).session_id is None
        assert kbn.list_notify_subs(conn, a) == []
