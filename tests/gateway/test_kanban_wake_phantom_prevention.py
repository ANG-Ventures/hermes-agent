"""Acceptance: a kanban wake must never MINT a phantom second session.

#555 made an identity-less ``kanban_notify_subs`` row *recoverable*
(``add_notify_sub`` backfills, ``hermes kanban notify-repair`` collapses
existing rows). It did not make the phantom *impossible*: the wake still
opens the user-less key on the row's FIRST wake, before any repair can run.

This suite drives the prevention half end-to-end through the REAL notifier
tick (``GatewayRunner._kanban_notifier_watcher``) against the REAL
``SessionStore`` — real ``get_or_create_session``, real ``build_session_key``,
real routing index. The wake's ``SessionSource`` is captured off the adapter
the notifier actually hands it to, so reverting the fix at the call site turns
these RED (proven by scripts/mutation_proof.sh, 3/3 mutants killed).

The human's own message creates the routing entry; the wake then has to land
in that same SESSION ID, not merely the same key shape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers import resolve_wake_participant
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB

CHAT = "1535189663533506600"
HUMAN = "117431298246705156"
OTHER_HUMAN = "220000000000000001"


class RecordingAdapter:
    """Push-capable adapter: records the wake's synthetic event."""

    def __init__(self) -> None:
        self.sent: list = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated kanban board + isolated session store, both on tmp_path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    resolved = str(kb.kanban_db_path())
    assert str(tmp_path) in resolved, f"test escaped its sandbox: {resolved}"
    kb.init_db()

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(store, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    return runner


def _subscribed_completed_task(session_key: str, **sub_kw) -> str:
    """A card whose terminal event the notifier will wake ``session_key`` for."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="worker card", assignee="worker", session_id=session_key,
        )
        kw: dict = {"chat_type": "group"}
        kw.update(sub_kw)
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT, **kw,
        )
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def _human_turn(store, user_id: str = HUMAN, **kw):
    """The human speaks in the channel — creates the real routing entry."""
    return store.get_or_create_session(
        SessionSource(
            platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
            user_id=user_id, **kw,
        )
    )


def _wake_source(monkeypatch, store, adapter, runner):
    """Run the real tick; return the SessionSource the wake was built with."""
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.handled, "notifier delivered no wake event"
    return adapter.handled[-1].source


def _resolve(store, source):
    """Route the wake's own source exactly as handle_message would."""
    return store.get_or_create_session(source)


# ---------------------------------------------------------------------------
# The acceptance criterion — proven through the real notifier
# ---------------------------------------------------------------------------

def test_identity_less_wake_lands_in_the_humans_own_session(env, monkeypatch):
    """A worker-created card (no participant on the sub row) notifying a
    channel the human has spoken in resolves to the human's OWN session — one
    session for the channel, not two."""
    store = env
    human = _human_turn(store)
    _subscribed_completed_task(human.session_key)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN, (
        "the wake was built with no participant — it will mint the phantom"
    )
    woken = _resolve(store, source)
    assert woken.session_key == human.session_key, (
        f"wake minted a second key: {woken.session_key!r} != {human.session_key!r}"
    )
    assert woken.session_id == human.session_id
    assert human.session_key.endswith(f":{HUMAN}")


def test_the_unfixed_path_would_have_minted_a_bare_key(env):
    """FIXTURE CONTROL: prove the phantom is real, i.e. that passing the row's
    NULL user_id straight through (the pre-fix behaviour) produces a DIFFERENT
    key. Without this the suite above could pass vacuously."""
    store = env
    human = _human_turn(store)

    unfixed = store.get_or_create_session(
        SessionSource(
            platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
            user_id=None,  # what the old call site passed: sub["user_id"]
        )
    )
    assert unfixed.session_key != human.session_key
    assert unfixed.session_id != human.session_id
    assert not unfixed.session_key.endswith(f":{HUMAN}")


# ---------------------------------------------------------------------------
# Negative controls — the refusals that keep this honest
# ---------------------------------------------------------------------------

def test_userless_chat_still_delivers_and_no_identity_is_fabricated(
    env, monkeypatch,
):
    """A genuinely user-less chat (cron / CLI / home-channel origin): nobody
    has ever spoken, so there is no evidence. The wake must still DELIVER, to
    the shared per-chat session, and must NOT invent a participant."""
    store = env
    _subscribed_completed_task("agent:main:discord:group:" + CHAT)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None, "an identity was fabricated for a user-less chat"
    assert adapter.sent, "delivery was dropped"
    woken = _resolve(store, source)
    assert woken.session_key.endswith(CHAT)
    assert HUMAN not in woken.session_key
    assert woken.session_id


def test_shared_channel_with_two_humans_refuses_to_pick(env, monkeypatch):
    """Two participants in one chat: adopting either would route a system
    notification into some human's private per-user session. Refuse."""
    store = env
    a = _human_turn(store, user_id=HUMAN)
    b = _human_turn(store, user_id=OTHER_HUMAN)
    assert a.session_key != b.session_key
    _subscribed_completed_task(a.session_key)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None
    woken = _resolve(store, source)
    assert woken.session_key not in {a.session_key, b.session_key}
    assert HUMAN not in woken.session_key
    assert OTHER_HUMAN not in woken.session_key


def test_a_row_that_names_a_participant_is_never_repointed(env, monkeypatch):
    """The row's own identity is authoritative. A second human speaking in the
    chat must not steal the first one's notification lane."""
    store = env
    _human_turn(store, user_id=OTHER_HUMAN)
    _subscribed_completed_task(
        "agent:main:discord:group:" + CHAT, user_id=HUMAN,
    )

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN
    woken = _resolve(store, source)
    assert woken.session_key.endswith(f":{HUMAN}")
    assert OTHER_HUMAN not in woken.session_key


def test_participants_in_another_chat_are_not_adopted(env, monkeypatch):
    """Evidence is per-chat. A human active in a DIFFERENT channel must never
    supply the identity for this one."""
    store = env
    store.get_or_create_session(
        SessionSource(
            platform=Platform.DISCORD, chat_id="9999999999", chat_type="group",
            user_id=HUMAN,
        )
    )
    _subscribed_completed_task("agent:main:discord:group:" + CHAT)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None
    assert HUMAN not in _resolve(store, source).session_key


def test_shared_thread_session_is_not_adopted_as_a_participant(
    env, monkeypatch,
):
    """A thread keyed WITHOUT a participant segment (the default
    ``thread_sessions_per_user=False``) is a shared lane. Adopting its
    speaker's id would route the wake into a per-user key nobody reads."""
    store = env
    threaded = store.get_or_create_session(
        SessionSource(
            platform=Platform.DISCORD, chat_id=CHAT, chat_type="thread",
            thread_id="42", user_id=HUMAN,
        )
    )
    assert not threaded.session_key.endswith(f":{HUMAN}"), (
        "fixture assumption broken: the thread key IS per-user here"
    )
    _subscribed_completed_task(
        threaded.session_key, chat_type="thread", thread_id="42",
    )

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None
    assert _resolve(store, source).session_key == threaded.session_key


# ---------------------------------------------------------------------------
# The security property the dispatcher's env strip exists for
# ---------------------------------------------------------------------------

def test_a_stale_participant_from_another_turn_cannot_be_inherited(
    env, monkeypatch,
):
    """The dispatcher strips every HERMES_SESSION_* var so a worker cannot
    inherit a previous gateway turn's routing (hermes_cli/kanban_db.py
    ``_default_spawn``). This fix must not smuggle that identity back in: the
    resolver reads the ROUTING INDEX for the row's own chat, never ambient
    session state. Bind a foreign identity into the session context and show
    the wake ignores it."""
    from gateway.session_context import reset_session_vars, set_session_vars

    store = env
    _subscribed_completed_task("agent:main:discord:group:" + CHAT)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter)
    set_session_vars(
        platform="discord", chat_id="a-different-chat", chat_type="group",
        user_id=OTHER_HUMAN,
    )
    try:
        source = _wake_source(monkeypatch, store, adapter, runner)
    finally:
        reset_session_vars()

    assert source.user_id is None, (
        "the wake inherited a participant from ambient session context — "
        "this re-opens exactly the leak the dispatcher's env strip prevents"
    )
    woken = _resolve(store, source)
    assert OTHER_HUMAN not in woken.session_key
    assert woken.session_key.endswith(CHAT)


# ---------------------------------------------------------------------------
# The pure resolver's own contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "row_user, live, expected",
    [
        ("u1", set(), "u1"),            # row identity always wins
        ("u1", {"u2"}, "u1"),           # ...even against live evidence
        ("u1", {"u2", "u3"}, "u1"),
        (None, {"u2"}, "u2"),           # exactly one => adopt
        ("", {"u2"}, "u2"),             # empty string is identity-less
        ("   ", {"u2"}, "u2"),
        (None, set(), None),            # no evidence => refuse
        (None, {"u2", "u3"}, None),     # ambiguous => refuse
        (None, {"", "  "}, None),       # blanks are not evidence
        (None, {"u2", ""}, "u2"),       # blanks filtered, one real left
    ],
)
def test_resolver_contract(row_user, live, expected):
    assert resolve_wake_participant(row_user, live) == expected
