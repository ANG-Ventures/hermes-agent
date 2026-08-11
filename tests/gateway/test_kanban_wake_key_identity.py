"""Acceptance: the wake must reconstruct the creator's key, not an approximation.

Card 5 (#562) closed the phantom whose cause was MISSING EVIDENCE — an
identity-less row, resolved from the gateway's live routing index. This suite
covers the phantom whose cause is MISSING DATA: two inputs of
``build_session_key`` that ``kanban_notify_subs`` had no column for, so the
wake could not reproduce the creator's key no matter how good the evidence was.

* ``user_id_alt`` — ``build_session_key`` derives the participant segment from
  ``user_id_alt or user_id``. Feishu (union_id), Signal (uuid) and DingTalk
  (staff_id) populate the alt slot, so the human's session key ends in the ALT
  id while the wake built one ending in ``user_id``.
* ``scope_id`` — Slack keys the workspace before ``chat_id`` on BOTH the DM and
  the group branch, so dropping it minted a second key for every Slack wake.

Unlike card 5's window this fired on EVERY wake on those platforms, warm
routing index or not: a row that names a participant short-circuits
``resolve_wake_participant`` (correctly — it must never repoint a named lane),
and an identity-less row can never satisfy that resolver's
``key.endswith(":" + user_id)`` evidence guard when the key ends in the alt id.

Everything below drives the REAL notifier tick
(``GatewayRunner._kanban_notifier_watcher``) against a REAL ``SessionStore``,
and the write side goes through the REAL gateway identity binding
(``_set_session_vars_for_source`` → ``subscribe_calling_session``) rather than
hand-writing rows, so a revert at either end turns these RED.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore, build_session_key
from gateway.session_context import get_session_env, reset_session_vars
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB
from tools.kanban_tools import subscribe_calling_session

CHAT = "oc_group_1535189663533506600"
OPEN_ID = "ou_open_117431298246705156"
UNION_ID = "on_union_220000000000000001"
SLACK_TEAM = "T_WORKSPACE_01"


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


def _make_runner(store, adapter, platform):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _bind_turn(source: SessionSource, entry):
    """Bind a turn's identity through the REAL gateway binder."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    return runner._set_session_vars_for_source(
        source=source,
        session_key=entry.session_key,
        session_id=entry.session_id,
        message_id=None,
    )


def _create_and_subscribe_as(source: SessionSource, entry) -> tuple[str, dict]:
    """Run the REAL gateway-turn create + auto-subscribe. Returns (task, row)."""
    _bind_turn(source, entry)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn, title="worker card", assignee="worker",
                session_id=entry.session_key,
            )
            assert subscribe_calling_session(conn, tid) is True
            kb.complete_task(conn, tid, summary="done")
            return tid, kb.list_notify_subs(conn, tid)[0]
        finally:
            conn.close()
    finally:
        reset_session_vars()


def _wake_source(monkeypatch, adapter, runner):
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.handled, "notifier delivered no wake event"
    return adapter.handled[-1].source


# ---------------------------------------------------------------------------
# The acceptance criterion, per phantom class
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "platform, user_id, user_id_alt, scope_id, chat_id, chat_type",
    [
        (Platform.FEISHU, OPEN_ID, UNION_ID, None, CHAT, "group"),
        (Platform.SIGNAL, "+15551234567", "uuid-aaaa-bbbb", None, CHAT, "group"),
        (Platform.DINGTALK, "dt_user", "staff_9", None, CHAT, "group"),
        (Platform.SLACK, "U1", None, SLACK_TEAM, "C1", "group"),
        (Platform.SLACK, "U1", None, SLACK_TEAM, "D1", "dm"),
    ],
)
def test_wake_lands_in_the_creators_own_session(
    env, monkeypatch, platform, user_id, user_id_alt, scope_id, chat_id,
    chat_type,
):
    """End to end, through the real write path AND the real notifier tick: the
    wake must resolve to the SAME session the human's own messages do."""
    store = env
    source = SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type,
        user_id=user_id, user_id_alt=user_id_alt, scope_id=scope_id,
    )
    human = store.get_or_create_session(source)
    _create_and_subscribe_as(source, human)

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, platform)
    woken = store.get_or_create_session(_wake_source(monkeypatch, adapter, runner))

    assert woken.session_key == human.session_key, (
        f"wake minted a second key: {woken.session_key!r} != "
        f"{human.session_key!r}"
    )
    assert woken.session_id == human.session_id


@pytest.mark.parametrize(
    "platform, user_id, user_id_alt, scope_id, chat_id, chat_type",
    [
        (Platform.FEISHU, OPEN_ID, UNION_ID, None, CHAT, "group"),
        (Platform.SLACK, "U1", None, SLACK_TEAM, "C1", "group"),
        (Platform.SLACK, "U1", None, SLACK_TEAM, "D1", "dm"),
    ],
)
def test_the_unpersisted_identity_would_have_minted_a_second_key(
    env, platform, user_id, user_id_alt, scope_id, chat_id, chat_type,
):
    """FIXTURE CONTROL: prove each phantom is real, i.e. that dropping the field
    the row now persists produces a DIFFERENT key. Without this the suite above
    could pass vacuously on a platform whose key never used the field."""
    creator = build_session_key(SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type,
        user_id=user_id, user_id_alt=user_id_alt, scope_id=scope_id,
    ))
    # What the wake built before the columns existed: user_id only, no scope.
    unfixed = build_session_key(SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type,
        user_id=user_id,
    ))
    assert unfixed != creator


def test_a_plain_user_id_platform_is_unaffected(env, monkeypatch):
    """REGRESSION CONTROL: discord carries neither field. Its wake must keep
    landing exactly where card 5 put it."""
    store = env
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="C9", chat_type="group",
        user_id="U9",
    )
    human = store.get_or_create_session(source)
    _, row = _create_and_subscribe_as(source, human)
    assert not row.get("user_id_alt")
    assert not row.get("scope_id")

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, Platform.DISCORD)
    woken = store.get_or_create_session(_wake_source(monkeypatch, adapter, runner))
    assert woken.session_key == human.session_key
    assert woken.session_id == human.session_id


# ---------------------------------------------------------------------------
# The persistence contract — evidence, never fabrication
# ---------------------------------------------------------------------------

def test_the_row_persists_exactly_what_the_turn_carried(env):
    """The stored identity is a COPY of the creating turn's own source, not an
    inference. A turn without these fields must store NULL, not a guess."""
    store = env
    rich = SessionSource(
        platform=Platform.FEISHU, chat_id=CHAT, chat_type="group",
        user_id=OPEN_ID, user_id_alt=UNION_ID,
    )
    _, row = _create_and_subscribe_as(rich, store.get_or_create_session(rich))
    assert row["user_id"] == OPEN_ID
    assert row["user_id_alt"] == UNION_ID
    assert not row.get("scope_id")

    bare = SessionSource(
        platform=Platform.FEISHU, chat_id="other-chat", chat_type="group",
        user_id=OPEN_ID,
    )
    _, row2 = _create_and_subscribe_as(bare, store.get_or_create_session(bare))
    assert row2["user_id"] == OPEN_ID
    assert not row2.get("user_id_alt"), "an alt identity was fabricated"


def test_a_userless_origin_stores_no_identity_at_all(env):
    """The negative control card 5 ships, extended to the new columns: a
    genuinely user-less origin must not acquire one."""
    store = env
    source = SessionSource(
        platform=Platform.FEISHU, chat_id=CHAT, chat_type="group",
    )
    _, row = _create_and_subscribe_as(source, store.get_or_create_session(source))
    assert not row.get("user_id")
    assert not row.get("user_id_alt")
    assert not row.get("scope_id")


def test_backfill_never_repoints_a_named_lane(env):
    """``add_notify_sub`` self-heals a hole but must never repoint an identity.

    The alt id is gated on the row being identity-less in BOTH columns: writing
    an alt id onto a row that already names a DIFFERENT ``user_id`` would
    repoint that row's lane to another participant — the exact hijack the
    ``user_id`` guard exists to prevent. The pair is one identity."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        kb.add_notify_sub(
            conn, task_id=tid, platform="feishu", chat_id=CHAT,
            chat_type="group", user_id=OPEN_ID,
        )
        # A DIFFERENT participant subscribing in the same chat.
        kb.add_notify_sub(
            conn, task_id=tid, platform="feishu", chat_id=CHAT,
            chat_type="group", user_id="ou_someone_else",
            user_id_alt="on_someone_else",
        )
        row = kb.list_notify_subs(conn, tid)[0]
        assert row["user_id"] == OPEN_ID, "the named lane was repointed"
        assert not row.get("user_id_alt"), (
            "a foreign alt id was grafted onto another participant's row"
        )

        # The SAME participant re-subscribing DOES fill the hole.
        kb.add_notify_sub(
            conn, task_id=tid, platform="feishu", chat_id=CHAT,
            chat_type="group", user_id=OPEN_ID, user_id_alt=UNION_ID,
        )
        healed = kb.list_notify_subs(conn, tid)[0]
        assert healed["user_id"] == OPEN_ID
        assert healed["user_id_alt"] == UNION_ID
    finally:
        conn.close()


def test_legacy_rows_without_the_columns_behave_exactly_as_before(
    env, monkeypatch,
):
    """Backward compatibility: a row written before the migration carries NULL
    in both columns and must wake precisely where it used to."""
    store = env
    human = store.get_or_create_session(SessionSource(
        platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
        user_id=OPEN_ID,
    ))
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="legacy", assignee="w", session_id=human.session_key,
        )
        # Exactly the columns a pre-migration writer supplied.
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT,
            chat_type="group", user_id=OPEN_ID,
        )
        kb.complete_task(conn, tid, summary="done")
        row = kb.list_notify_subs(conn, tid)[0]
    finally:
        conn.close()
    assert row["user_id_alt"] is None
    assert row["scope_id"] is None

    adapter = RecordingAdapter()
    runner = _make_runner(store, adapter, Platform.DISCORD)
    woken = store.get_or_create_session(_wake_source(monkeypatch, adapter, runner))
    assert woken.session_key == human.session_key


def test_child_tasks_inherit_the_full_identity(env):
    """``_inherit_notify_subs`` copies a parent's subscription to a child. A
    truncated copy would re-open the phantom on every derived card."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        kb.add_notify_sub(
            conn, task_id=parent, platform="slack", chat_id="C1",
            chat_type="group", user_id="U1", user_id_alt="alt-1",
            scope_id=SLACK_TEAM,
        )
        child = kb.create_task(conn, title="c", assignee="w", parents=(parent,))
        row = kb.list_notify_subs(conn, child)[0]
        assert row["user_id"] == "U1"
        assert row["user_id_alt"] == "alt-1"
        assert row["scope_id"] == SLACK_TEAM
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The security property the dispatcher's env strip exists for (card 5's test,
# extended to the two new identity vars)
# ---------------------------------------------------------------------------

def test_the_new_identity_vars_are_stripped_from_a_dispatched_worker():
    """The dispatcher pops every key in ``session_context._VAR_MAP`` from a
    worker's env so it cannot inherit a previous gateway turn's routing. The
    two new vars carry IDENTITY, so they must be in that map — otherwise a
    worker would inherit an ambient participant and subscribe a foreign chat's
    human into its own card, which is precisely the leak the strip prevents."""
    from gateway.session_context import _VAR_MAP

    assert "HERMES_SESSION_USER_ID_ALT" in _VAR_MAP
    assert "HERMES_SESSION_SCOPE_ID" in _VAR_MAP


def test_a_cleared_context_does_not_leak_an_identity_from_os_environ(
    monkeypatch,
):
    """``clear_session_vars`` sets every identity var to "" so a post-turn read
    cannot fall through to a stale ``os.environ`` mirror. The new vars must
    participate, or a subscribe running after a turn ends could pick up another
    session's participant."""
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("HERMES_SESSION_USER_ID_ALT", "stale-alt")
    monkeypatch.setenv("HERMES_SESSION_SCOPE_ID", "stale-scope")
    tokens = set_session_vars(
        platform="feishu", chat_id=CHAT, user_id=OPEN_ID,
        user_id_alt=UNION_ID, scope_id=SLACK_TEAM,
    )
    try:
        assert get_session_env("HERMES_SESSION_USER_ID_ALT") == UNION_ID
        assert get_session_env("HERMES_SESSION_SCOPE_ID") == SLACK_TEAM
        clear_session_vars(tokens)
        assert get_session_env("HERMES_SESSION_USER_ID_ALT") == ""
        assert get_session_env("HERMES_SESSION_SCOPE_ID") == ""
    finally:
        reset_session_vars()


def test_a_stale_alt_identity_from_another_turn_cannot_be_inherited(env):
    """Sibling of card 5's env-strip test, on the write side. A turn that binds
    NO alt id must not pick one up from a previous turn's ``os.environ``
    mirror: ``reset_session_vars`` restores the _UNSET sentinel, and the
    gateway binder writes "" for a source that carries nothing."""
    store = env
    foreign = SessionSource(
        platform=Platform.FEISHU, chat_id="a-different-chat", chat_type="group",
        user_id="ou_foreign", user_id_alt="on_foreign",
    )
    _bind_turn(foreign, store.get_or_create_session(foreign))
    try:
        # A SECOND turn in another chat, carrying no alt identity at all.
        mine = SessionSource(
            platform=Platform.FEISHU, chat_id=CHAT, chat_type="group",
            user_id=OPEN_ID,
        )
        _, row = _create_and_subscribe_as(mine, store.get_or_create_session(mine))
    finally:
        reset_session_vars()

    assert row["user_id"] == OPEN_ID
    assert not row.get("user_id_alt"), (
        "the subscribe inherited an alt participant from another turn — this "
        "re-opens exactly the leak the dispatcher's env strip prevents"
    )
