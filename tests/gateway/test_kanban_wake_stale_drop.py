"""A kanban wake whose card went done/archived before the wake ran is dropped (t_07ffc6cb).

Incident 2026-10-03: four stuck/gave_up wakes for t_49ca79b8 / t_c459e6e6
(events 21:11-21:22) were consumed after both cards were archived at 21:30.
Each test archives the card BETWEEN the claim and the send (or the dequeue)
and drives the real code path for that stage.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from gateway import kanban_owner_wake as ow
from gateway import kanban_wake_freshness as fresh
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

from tests.gateway.test_kanban_owner_wake import CHAT, HUMAN, _blocked, _card, _event, _tick, env  # noqa: F401

STALE_LOG = "kanban_wake_stale_dropped"


def _archive(tid):
    with kb.connect_closing() as conn:
        assert kb.archive_task(conn, tid)


def _stale_lines(caplog):
    return [r.getMessage() for r in caplog.records if STALE_LOG in r.getMessage()]


# --- owner-wake (held in the coalescing window, then sent) -------------------


def test_owner_wake_held_event_dropped_when_card_archived_before_send(env, caplog):
    caplog.set_level(logging.INFO)
    tid = _card(env, title="smoke")
    _event(tid, "gave_up", {"error": "retries exhausted"})
    state = ow.WakeState(Path(get_hermes_home()) / "gateway" / "t.json")
    state.cursors = {"default": 0}
    t0 = 1_000_000.0
    state.last_wake[f"default|{tid}"] = t0  # an earlier wake: this one is held
    assert asyncio.run(ow.tick(env["runner"], now=t0 + 60, state=state)) == 0
    assert f"default|{tid}" in state.pending, "held in the coalescing window"

    _archive(tid)
    assert asyncio.run(ow.tick(env["runner"], now=t0 + ow.COALESCE_SECONDS + 1, state=state)) == 0
    assert env["adapter"].handled == [], "no wake for an archived card"
    assert state.pending == {}, "dropped, not held for retry"
    lines = _stale_lines(caplog)
    assert len(lines) == 1 and tid in lines[0] and "kind=gave_up" in lines[0]
    assert "status=archived" in lines[0] and "event_ts=" in lines[0] and "age_s=" in lines[0]


def test_owner_wake_still_sent_while_card_is_live(env):
    tid = _card(env)
    _event(tid, "gave_up", {"error": "retries exhausted"})
    handled = _tick(env)
    assert len(handled) == 1 and tid in handled[0].text
    cards = handled[0].metadata[fresh.META_KEY]
    assert [c["task_id"] for c in cards] == [tid] and cards[0]["kinds"] == ["gave_up"]
    assert cards[0]["text"] == handled[0].text, "dequeue re-check can match the rendered text"


# --- notifier sub wake (claimed, then sent) ----------------------------------


def _sub_card(env, mode):
    tid = _card(env)
    with kb.connect_closing() as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=CHAT,
                           chat_type="group", user_id=HUMAN, delivery_mode=mode)
    return tid


def _archive_after_claim(env, tid):
    real = kbn.claim_unseen_events_for_sub

    def claim_then_archive(conn, **kw):
        out = real(conn, **kw)
        if kw.get("task_id") == tid and out[2]:
            kb.archive_task(conn, tid)
        return out

    env["monkeypatch"].setattr(kbn, "claim_unseen_events_for_sub", claim_then_archive)


@pytest.mark.parametrize("mode", ["notify+wake", "wake"])
def test_notifier_wake_dropped_when_card_archived_between_claim_and_send(env, caplog, mode):
    caplog.set_level(logging.INFO)
    _cfgless_owner_wake_off(env)
    tid = _sub_card(env, mode)
    _event(tid, "gave_up", {"error": "retries exhausted"})
    _archive_after_claim(env, tid)
    handled = _tick(env)
    assert [e for e in handled if tid in (e.text or "")] == [], "no wake turn for an archived card"
    if mode == "notify+wake":
        assert any(tid in m["text"] for m in env["adapter"].sent), "the passive line still posts"
    lines = _stale_lines(caplog)
    assert lines and all(tid in ln and "stage=notifier" in ln for ln in lines)


def test_notifier_wake_still_sent_for_live_card(env):
    _cfgless_owner_wake_off(env)
    tid = _sub_card(env, "notify+wake")
    _blocked(tid)  # gave_up leaves the card blocked
    _event(tid, "gave_up", {"error": "retries exhausted"})
    handled = [e for e in _tick(env) if tid in (e.text or "")]
    assert len(handled) == 1
    assert handled[0].metadata[fresh.META_KEY][0]["task_id"] == tid


def test_completion_of_a_done_card_still_wakes(env):
    _cfgless_owner_wake_off(env)
    tid = _sub_card(env, "notify+wake")
    with kb.connect_closing() as conn:
        kb.complete_task(conn, tid, summary="shipped")
    handled = [e for e in _tick(env) if tid in (e.text or "")]
    assert len(handled) == 1, "the completion IS the news"


def _cfgless_owner_wake_off(env):
    # Isolate the subscription path from the owner-wake module.
    env["monkeypatch"].setattr(ow, "resolve_settings", lambda: (False, ()))


# --- dequeue (a busy session pops the wake minutes later) --------------------


def _wake_event(cards_live_and_stale):
    cards = [fresh.card_entry("default", tid, ["gave_up"], 1_000, f"[kanban wake] {tid}")
             for tid in cards_live_and_stale]
    src = SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type="group", user_id=HUMAN)
    return MessageEvent(text="\n\n".join(c["text"] for c in cards), message_type=MessageType.TEXT,
                        source=src, internal=True, metadata={fresh.META_KEY: cards})


def test_dequeue_drops_wake_for_archived_card(env, caplog):
    caplog.set_level(logging.INFO)
    tid = _card(env)
    _blocked(tid)
    ev = _wake_event([tid])
    assert fresh.filter_event(ev) is False, "live card: kept"
    _archive(tid)
    assert fresh.filter_event(ev, now=1_600) is True
    line = _stale_lines(caplog)[0]
    assert "stage=dequeue" in line and "age_s=600" in line and "status=archived" in line


def test_dequeue_narrows_multi_card_wake_to_live_cards(env):
    live, gone = _card(env), _card(env)
    ev = _wake_event([live, gone])
    _archive(gone)
    _blocked(live)  # after the archive: its ready sweep re-readies an untyped raw block
    assert fresh.filter_event(ev) is False
    assert ev.text == f"[kanban wake] {live}"
    assert [c["task_id"] for c in ev.metadata[fresh.META_KEY]] == [live]


def test_dequeue_leaves_a_merged_human_message_alone(env):
    tid = _card(env)
    ev = _wake_event([tid])
    ev.text += "\nAce: also check the relay"
    _archive(tid)
    assert fresh.filter_event(ev) is False


def test_dequeue_keeps_wake_when_board_unreadable(env, monkeypatch):
    tid = _card(env)
    ev = _wake_event([tid])

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(kb, "connect_readonly", boom)
    assert fresh.filter_event(ev) is False, "unknown status: the duplicate is the safe failure"


def test_admitted_handler_returns_before_running_a_stale_wake(env):
    tid = _card(env)
    ev = _wake_event([tid])
    _archive(tid)
    runner = GatewayRunner.__new__(GatewayRunner)
    assert asyncio.run(runner._handle_message_with_agent_admitted(ev, ev.source, "k", 0)) is None


def test_drain_skips_stale_wake_and_promotes_the_next_queued_event(env):
    tid = _card(env)
    stale = _wake_event([tid])
    _archive(tid)
    human = MessageEvent(text="hi", message_type=MessageType.TEXT, source=stale.source)

    class Adapter:
        _pending_messages = {"k": stale}

        def get_pending_message(self, key):
            return self._pending_messages.pop(key, None)

    runner = GatewayRunner.__new__(GatewayRunner)
    queue = [human]
    runner._overflow_queue = lambda key: queue
    runner._draining = False
    pending_event, pending = asyncio.run(
        runner._run_agent_drain_pending({"final_response": "x"}, Adapter(), stale.source, "k"))
    assert pending_event is human and pending == "hi"


# --- t_28b3f37c: main-train's owner notice is news about a CLOSED card by design ----------------------------------

OWNER_NOTICE = {"by": "card-mechanical-wake", "source": "main-train",
                "episode": "main-train-owner:ANG-Ventures/hermes-home#11647:mt-12202",
                "text": "[main-train] your PR turned main red"}
DETACHED = {"by": "card-mechanical-wake", "source": "detached-run", "episode": "detached:r1", "text": "job x rc=1"}


def _close(tid, status="done"):
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


@pytest.mark.parametrize("status", ["done", "archived"])
def test_owner_notice_on_a_closed_card_is_not_stale(status):
    assert fresh.is_stale(status, "mechanical_wake", OWNER_NOTICE) is False
    assert fresh.is_stale(status, "mechanical_wake", DETACHED) is True, "every other wake stays dropped"
    assert fresh.is_stale(status, "mechanical_wake") is True
    assert fresh.is_stale(status, "gave_up", OWNER_NOTICE) is True, "only the mechanical_wake kind"
    for bad in ({**OWNER_NOTICE, "source": "detached-run"}, {**OWNER_NOTICE, "episode": "detached:r1"},
                {**OWNER_NOTICE, "by": "someone"}):
        assert fresh.is_stale(status, "mechanical_wake", bad) is True, bad
    assert fresh.is_stale(fresh.GONE, "mechanical_wake", OWNER_NOTICE) is True, "a gone card stays stale"


@pytest.mark.parametrize("status", ["done", "archived"])
def test_owner_wake_delivers_an_owner_notice_on_a_closed_card(env, caplog, status):
    caplog.set_level(logging.INFO)
    tid = _card(env)
    _close(tid, status)
    _event(tid, "mechanical_wake", OWNER_NOTICE)
    handled = [e for e in _tick(env) if tid in (e.text or "")]
    assert len(handled) == 1, "one turn for the owner notice"
    assert handled[0].metadata["gateway_session_key"] == env["owner"].session_key
    assert not _stale_lines(caplog)
    assert fresh.filter_event(handled[0]) is False, "the dequeue re-check keeps it too"


def test_owner_wake_still_drops_a_detached_run_wake_on_a_done_card(env, caplog):
    caplog.set_level(logging.INFO)
    tid = _card(env)
    _close(tid)
    _event(tid, "mechanical_wake", DETACHED)
    assert [e for e in _tick(env) if tid in (e.text or "")] == []
    lines = _stale_lines(caplog)
    assert len(lines) == 1 and "kind=mechanical_wake" in lines[0] and "status=done" in lines[0]


def test_dequeue_keeps_an_owner_notice_on_a_done_card_and_drops_it_once_gone(env):
    tid = _card(env)
    card = fresh.card_entry("default", tid, ["mechanical_wake"], 1_000, f"[kanban wake] {tid}", closed_news=True)
    src = SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type="group", user_id=HUMAN)
    ev = MessageEvent(text=card["text"], message_type=MessageType.TEXT, source=src, internal=True,
                      metadata={fresh.META_KEY: [card]})
    _archive(tid)
    assert fresh.filter_event(ev) is False
    plain = _wake_event([tid])
    plain.metadata[fresh.META_KEY][0]["kinds"] = ["mechanical_wake"]
    assert fresh.filter_event(plain) is True, "a mechanical_wake that is not an owner notice is dropped"
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    assert fresh.filter_event(ev) is True, "a gone card stays stale"


# --- t_05badfca: a status notice whose status the card no longer holds ------------------------------------------------
# Replay of house-voice t_305ecaca, 2026-11-18: the owner's request-changes unblocked the scheduled card into
# review (16:24:24, the entry trigger writes a review_requested), the notifier woke the owner's busy session with
# "handed off for review" (queued 16:24:25), the owner requested changes (16:24:28) and the implementer's run
# claimed the card (16:24:42). The queued wake ran at 16:24:48 and told the owner a review was waiting.


def _set_status(tid, status):
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


def _review_wake(env):
    """The 16:24:24-25 half: the card enters review and the notifier builds its wake."""
    _cfgless_owner_wake_off(env)
    tid = _sub_card(env, "notify+wake")
    _set_status(tid, "review")  # tasks_review_handoff writes the review_requested, as on the board
    with kb.connect_closing() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
    assert "review_requested" in kinds
    wakes = [e for e in _tick(env) if tid in (e.text or "")]
    assert len(wakes) == 1 and "handed off for review" in wakes[0].text, "the card is in review: sent"
    assert wakes[0].metadata[fresh.META_KEY][0]["kinds"] == ["review_requested"]
    return tid, wakes[0]


def test_replay_t_305ecaca_review_wake_dropped_after_changes_requested(env, caplog):
    caplog.set_level(logging.INFO)
    tid, wake = _review_wake(env)
    assert fresh.filter_event(wake) is False, "still in review: kept"
    _set_status(tid, "ready")
    _event(tid, "changes_requested", {"reason": "drill 4 result", "status": "ready"})
    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, tid) is not None, "the implementer's run claims it (16:24:42)"
    assert fresh.filter_event(wake, now=wake.metadata[fresh.META_KEY][0]["event_ts"] + 23) is True
    line = _stale_lines(caplog)[-1]
    assert tid in line and "kind=review_requested" in line and "stage=dequeue" in line and "status=running" in line


def test_review_wake_kept_while_the_reviewer_holds_the_claim(env):
    tid, wake = _review_wake(env)
    with kb.connect_closing() as conn:
        assert kb.claim_review_task(conn, tid) is not None
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    assert fresh.read_status("default", tid) == "review", "a review run is still the review"
    assert fresh.filter_event(wake) is False


def test_notifier_drops_a_review_wake_when_changes_land_between_claim_and_send(env, caplog):
    caplog.set_level(logging.INFO)
    _cfgless_owner_wake_off(env)
    tid = _sub_card(env, "notify+wake")
    _set_status(tid, "review")
    real = kbn.claim_unseen_events_for_sub

    def claim_then_send_back(conn, **kw):
        out = real(conn, **kw)
        if kw.get("task_id") == tid and out[2]:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        return out

    env["monkeypatch"].setattr(kbn, "claim_unseen_events_for_sub", claim_then_send_back)
    assert [e for e in _tick(env) if tid in (e.text or "")] == []
    lines = _stale_lines(caplog)
    assert lines and "stage=notifier" in lines[-1] and "kind=review_requested" in lines[-1] and "status=ready" in lines[-1]


@pytest.mark.parametrize("status,kind,stale", [
    ("review", "review_requested", False), ("running", "review_requested", True),
    ("ready", "review_requested", True), ("scheduled", "review_requested", True),
    ("blocked", "gave_up", False), ("ready", "gave_up", True),
    ("blocked", "blocked", False), ("ready", "blocked", True),
    ("triage", "block_loop_detected", False), ("ready", "block_loop_detected", True),
    ("running", "completed", False), ("done", "completed", False), ("done", "gave_up", True),
    ("ready", "crashed", False), (None, "review_requested", False), ("", "blocked", False),
])
def test_is_stale_notice(status, kind, stale):
    assert fresh.is_stale_notice(status, kind) is stale
