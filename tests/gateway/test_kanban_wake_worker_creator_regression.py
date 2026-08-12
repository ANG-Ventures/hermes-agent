"""Regression: worker/CLI-created cards must not re-mint the phantom (#568).

#562 resolved an identity-less sub against the live routing index; #568 then
bound that evidence to the task creator's exact session KEY. But
``tasks.session_id`` holds a session key only for gateway-created tasks —
worker/CLI-created tasks stamp a RAW session id (``20260811_220323_2eafab``),
and unstamped tasks carry none. #568's strict key equality therefore returned
EMPTY evidence for every worker card, reverting the #562 prevention to dead
code: the 2026-08-12 phantom (session ``20260811_181317_ea569b93``, Discord
chat 1535189663533506600) was minted by exactly this path on a gateway that
already carried both fixes.

Reuses the real-notifier harness from
``test_kanban_wake_phantom_prevention`` — the wake is driven through the
actual ``_kanban_notifier_watcher`` tick and the actual ``SessionStore``.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb

from tests.gateway.test_kanban_wake_phantom_prevention import (  # noqa: F401
    CHAT,
    HUMAN,
    RecordingAdapter,
    _human_turn,
    _make_runner,
    _resolve,
    _wake_source,
    env,
)

OTHER = "999888777666555444"

# A raw session id, as the dispatcher stamps on worker-created tasks: no
# colon, so it can never equal a routing-index session KEY.
WORKER_RAW_SESSION_ID = "20260811_220323_2eafab"


def _worker_card(session_id) -> str:
    """A card created the way the dispatcher/worker/CLI path does — with a
    RAW session id (or none), never a session key — plus an identity-less
    subscription."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="worker card", assignee="worker",
            session_id=session_id,
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT,
            chat_type="group",
        )
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def test_worker_created_card_with_raw_session_id_lands_in_humans_session(
    env, monkeypatch,
):
    """THE 2026-08-12 regression. A daedalus-created card stamps a raw
    session id; #568's strict key-binding yielded empty evidence and the wake
    minted the phantom. With the lane fallback the wake must adopt the single
    human participant."""
    store = env
    human = _human_turn(store)
    _worker_card(WORKER_RAW_SESSION_ID)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN, (
        "worker-card wake carried no participant — #568's creator veto is back"
    )
    woken = _resolve(store, source)
    assert woken.session_key == human.session_key
    assert woken.session_id == human.session_id


def test_unstamped_card_sends_text_but_never_wakes(env, monkeypatch):
    """A card with NO session id at all cannot wake anything (the notifier's
    wake path requires ``task.session_id``), so no phantom is possible — the
    ping degrades to a plain text send. Pins the contract this suite's other
    tests rely on."""
    import asyncio as _asyncio

    from tests.gateway.test_kanban_wake_phantom_prevention import (
        _run_one_notifier_tick,
    )

    store = env
    _human_turn(store)
    _worker_card(None)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    _asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert not adapter.handled, "an unstamped card produced a wake event"
    assert adapter.sent, "the terminal notification text was dropped"


def test_worker_card_in_two_human_channel_still_refuses(env, monkeypatch):
    """Negative control: the lane fallback must inherit #562's refusal — two
    participants in the chat, no adoption, wake stays on the shared key."""
    store = env
    _human_turn(store, user_id=HUMAN)
    _human_turn(store, user_id=OTHER)
    _worker_card(WORKER_RAW_SESSION_ID)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None, (
        "ambiguous shared channel adopted a participant — lane hijack"
    )


def test_gateway_key_stamped_card_still_binds_strictly(env, monkeypatch):
    """#568's intent must survive: a card stamped with the creating session
    KEY binds to that entry only — a second human in the chat cannot divert
    it, and the creator's own identity wins even in an ambiguous lane."""
    store = env
    human = _human_turn(store, user_id=HUMAN)
    _human_turn(store, user_id=OTHER)  # ambiguous lane without binding
    _worker_card(human.session_key)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN, (
        "creator-key binding stopped selecting the creator in an ambiguous lane"
    )
    woken = _resolve(store, source)
    assert woken.session_key == human.session_key
