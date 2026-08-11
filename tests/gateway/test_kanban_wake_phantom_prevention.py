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
HUMAN_ALT = "uuid-1174-3129"
OTHER_HUMAN = "220000000000000001"
SLACK_TEAM = "T_WORKSPACE"


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


def _make_runner(store, adapter, platform=Platform.DISCORD):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    return runner


def _subscribed_completed_task(
    session_key: str, *, platform: str = "discord", chat_id: str = CHAT, **sub_kw,
) -> str:
    """A card whose terminal event the notifier will wake ``session_key`` for."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="worker card", assignee="worker", session_id=session_key,
        )
        kw: dict = {"chat_type": "group"}
        kw.update(sub_kw)
        kb.add_notify_sub(
            conn, task_id=tid, platform=platform, chat_id=chat_id, **kw,
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


@pytest.mark.parametrize(("platform", "participant_alt"), [
    (Platform.SIGNAL, "uuid-1174-3129"),
    (Platform.FEISHU, "union-1174-3129"),
    (Platform.DINGTALK, "staff-1174-3129"),
])
def test_identity_less_alt_keyed_wake_lands_in_the_humans_own_session(
    env, monkeypatch, platform, participant_alt,
):
    """Alt-keyed platforms select the participant from user_id_alt. Evidence
    must validate and return THAT segment, not reject the entry because its key
    does not end in the lower-priority user_id."""
    store = env
    human_source = SessionSource(
        platform=platform,
        chat_id=CHAT,
        chat_type="group",
        user_id=HUMAN,
        user_id_alt=participant_alt,
    )
    human = store.get_or_create_session(human_source)
    assert human.session_key.endswith(f":{participant_alt}")
    assert not human.session_key.endswith(f":{HUMAN}")
    _subscribed_completed_task(human.session_key, platform=platform.value)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, platform)
    source = _wake_source(monkeypatch, store, adapter, runner)

    # Live evidence retains the complete source identity rather than collapsing
    # the effective participant into user_id. Repair and wake therefore agree on
    # which raw and alternate ids belong together.
    assert source.user_id == HUMAN
    assert source.user_id_alt == participant_alt
    woken = _resolve(store, source)
    assert woken.session_key == human.session_key
    assert woken.session_id == human.session_id


@pytest.mark.parametrize(("chat_id", "chat_type"), [
    ("C1", "group"),
    ("D1", "dm"),
])
def test_identity_less_slack_wake_adopts_user_and_workspace_as_one_identity(
    env, monkeypatch, chat_id, chat_type,
):
    """Slack's scope_id is part of the key before chat/user. Live evidence must
    carry it with the participant or the wake still mints an unscoped phantom."""
    store = env
    human_source = SessionSource(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=HUMAN,
        scope_id=SLACK_TEAM,
    )
    human = store.get_or_create_session(human_source)
    assert SLACK_TEAM in human.session_key
    _subscribed_completed_task(
        human.session_key,
        platform=Platform.SLACK.value,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, Platform.SLACK)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN
    assert source.scope_id == SLACK_TEAM
    woken = _resolve(store, source)
    assert woken.session_key == human.session_key
    assert woken.session_id == human.session_id


def test_live_slack_evidence_refuses_same_participant_in_two_workspaces(
    env, monkeypatch,
):
    """Candidate uniqueness is over the complete identity, not just user_id."""
    store = env
    for scope_id in ("T_WORKSPACE_A", "T_WORKSPACE_B"):
        store.get_or_create_session(SessionSource(
            platform=Platform.SLACK,
            chat_id="C1",
            chat_type="group",
            user_id=HUMAN,
            scope_id=scope_id,
        ))
    _subscribed_completed_task(
        "agent:main:slack:group:C1",
        platform=Platform.SLACK.value,
        chat_id="C1",
    )

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, Platform.SLACK)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id is None
    assert source.scope_id is None


def test_scope_only_slack_row_narrows_live_identity_to_one_workspace(
    env, monkeypatch,
):
    store = env
    expected = None
    for user_id, scope_id in (
        (HUMAN, "T_WORKSPACE_A"),
        (OTHER_HUMAN, "T_WORKSPACE_B"),
    ):
        session = store.get_or_create_session(SessionSource(
            platform=Platform.SLACK,
            chat_id="C1",
            chat_type="group",
            user_id=user_id,
            scope_id=scope_id,
        ))
        if scope_id == "T_WORKSPACE_A":
            expected = session
    assert expected is not None
    _subscribed_completed_task(
        expected.session_key,
        platform=Platform.SLACK.value,
        chat_id="C1",
        scope_id="T_WORKSPACE_A",
    )

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, Platform.SLACK)
    source = _wake_source(monkeypatch, store, adapter, runner)

    assert source.user_id == HUMAN
    assert source.scope_id == "T_WORKSPACE_A"
    woken = _resolve(store, source)
    assert woken.session_id == expected.session_id


def test_alt_keyed_shared_thread_is_still_not_adopted(env):
    """A shared key is not evidence even when its thread-id suffix happens to
    equal the alternate participant bytes."""
    store = env
    threaded = store.get_or_create_session(
        SessionSource(
            platform=Platform.SIGNAL,
            chat_id=CHAT,
            chat_type="thread",
            # Collision control: build_session_key appends only this shared
            # thread id, not a participant segment, yet endswith(participant)
            # is still true.
            thread_id=HUMAN_ALT,
            user_id=HUMAN,
            user_id_alt=HUMAN_ALT,
        )
    )
    assert threaded.session_key.endswith(f":{HUMAN_ALT}")
    runner = _make_runner(store, RecordingAdapter(), Platform.SIGNAL)
    assert runner._live_chat_participants(
        Platform.SIGNAL, CHAT, HUMAN_ALT, "thread",
    ) == set()


def test_prospective_thread_origin_is_not_group_lane_evidence(env):
    from gateway.routing_identity import effective_routing_lane

    assert effective_routing_lane(
        platform=Platform.SIGNAL,
        chat_id=CHAT,
        chat_type="group",
        prospective_thread_id="99",
    ) == ("signal", CHAT, "thread", "99")
    store = env
    prospective = store.get_or_create_session(SessionSource(
        platform=Platform.SIGNAL,
        chat_id=CHAT,
        chat_type="group",
        prospective_thread_id="99",
        user_id=HUMAN,
        user_id_alt=HUMAN_ALT,
    ))
    assert ":thread:" in prospective.session_key

    runner = _make_runner(store, RecordingAdapter(), Platform.SIGNAL)
    assert runner._live_chat_participants(
        Platform.SIGNAL,
        CHAT,
        None,
        "group",
        prospective.session_key,
    ) == set()


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
    """A genuinely user-less subscription remains shared even when one human
    has an unrelated per-user session in the same lane."""
    store = env
    _human_turn(store, user_id=HUMAN)
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
    _subscribed_completed_task("agent:main:discord:group:" + CHAT)

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
    other = _human_turn(store, user_id=OTHER_HUMAN)
    _subscribed_completed_task(
        other.session_key, user_id=HUMAN,
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
