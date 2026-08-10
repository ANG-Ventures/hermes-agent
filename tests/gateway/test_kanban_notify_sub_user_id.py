"""Kanban notify-subs must round-trip the creator's ``user_id`` end to end.

Regression coverage for the "phantom second session per channel" bug: a
notify-sub written without ``user_id`` makes the wake injector rebuild the
creator's scope with ``user_id=None``, and
``gateway.session.build_session_key`` then omits the participant segment. The
kanban wake therefore lands in a DIFFERENT session key from the creator's own
messages — a session no human can reach from chat, so ``/reasoning`` and
``/model`` overrides set by the user never apply to it.

Three properties are pinned here:

1. **Round-trip** — a sub written from a gateway turn carrying a bound
   ``user_id`` reproduces the creator's session key through
   ``add_notify_sub`` -> wake ``SessionSource`` -> ``build_session_key``.
2. **Self-heal** — a later write that DOES carry an identity backfills a
   legacy row that has none. Without this the historical empty rows keep
   feeding the phantom session forever, since ``add_notify_sub`` is
   ``INSERT OR IGNORE``.
3. **Negative control** — a genuinely user-less origin (cron / CLI / a
   dispatcher-spawned worker with no chat identity) must still deliver and
   must NOT have an identity fabricated for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db as kb

CHAT_ID = "1535189663533506600"
USER_ID = "117431298246705156"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatcher-spawned worker runs with these pinned; they would send
    # kanban_db_path() at the LIVE production board instead of tmp_path.
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    resolved = str(kb.kanban_db_path())
    assert str(tmp_path) in resolved, f"test escaped its sandbox: {resolved}"
    kb.init_db()
    return home


def _sub_for(conn, task_id: str) -> dict:
    subs = kb.list_notify_subs(conn, task_id)
    assert len(subs) == 1, subs
    return subs[0]


def _wake_source(sub: dict) -> SessionSource:
    """Rebuild the creator's scope exactly as the wake injector does.

    Mirrors ``GatewayKanbanWatchersMixin._kanban_notifier_watcher``'s
    push-adapter branch (gateway/kanban_watchers.py).
    """
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=sub["chat_id"],
        chat_type=str(sub.get("chat_type") or "group"),
        thread_id=sub.get("thread_id") or None,
        user_id=sub.get("user_id"),
    )


def _creator_key() -> str:
    """The session key the creator's OWN messages resolve to."""
    return build_session_key(
        SessionSource(
            platform=Platform.DISCORD,
            chat_id=CHAT_ID,
            chat_type="group",
            user_id=USER_ID,
        ),
        group_sessions_per_user=True,
    )


# ---------------------------------------------------------------------------
# 1 — round-trip
# ---------------------------------------------------------------------------


def test_notify_sub_user_id_round_trips_to_the_creators_session_key(kanban_home):
    """A sub written with the creator's identity wakes the creator's OWN
    session — one key for the channel, not two."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="round trip", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id=CHAT_ID,
            chat_type="group",
            user_id=USER_ID,
        )
        sub = _sub_for(conn, tid)
    finally:
        conn.close()

    assert sub["user_id"] == USER_ID, "user_id was dropped at the DB layer"

    wake_key = build_session_key(_wake_source(sub), group_sessions_per_user=True)
    assert wake_key == _creator_key()
    # The participant segment is what makes the two keys identical; assert it
    # explicitly so a key-format change can't make this pass vacuously.
    assert wake_key.endswith(f":{USER_ID}")


def test_blank_user_id_in_the_wake_path_splits_the_session(kanban_home):
    """MUTATION CONTROL for the assertion above.

    Blanking ``user_id`` on the way into the wake ``SessionSource`` — the
    exact shape of the original bug — must produce a DIFFERENT key. If this
    ever passes, the round-trip test above proves nothing.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="mutation", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id=CHAT_ID,
            chat_type="group",
            user_id=USER_ID,
        )
        sub = _sub_for(conn, tid)
    finally:
        conn.close()

    mutated = dict(sub)
    mutated["user_id"] = None
    mutated_key = build_session_key(
        _wake_source(mutated), group_sessions_per_user=True
    )

    assert mutated_key != _creator_key()
    assert not mutated_key.endswith(f":{USER_ID}")


# ---------------------------------------------------------------------------
# 2 — self-heal of legacy identity-less rows
# ---------------------------------------------------------------------------


def test_resubscribe_backfills_user_id_on_a_legacy_row(kanban_home):
    """``add_notify_sub`` is INSERT OR IGNORE, so a re-subscribe from a turn
    that DOES carry an identity must backfill the empty column — exactly as
    ``chat_type`` and ``notifier_profile`` already self-heal. Otherwise the
    historical identity-less rows keep waking the phantom session forever."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy row", assignee="worker")
        # Legacy write: no identity (the bug's output).
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT_ID,
        )
        assert _sub_for(conn, tid)["user_id"] in (None, "")

        # Same chat re-subscribes from a turn that HAS an identity.
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id=CHAT_ID,
            chat_type="group",
            user_id=USER_ID,
        )
        healed = _sub_for(conn, tid)
    finally:
        conn.close()

    assert healed["user_id"] == USER_ID
    wake_key = build_session_key(_wake_source(healed), group_sessions_per_user=True)
    assert wake_key == _creator_key()


def test_self_heal_never_overwrites_an_existing_identity(kanban_home):
    """Backfill fills a HOLE; it must not repoint an existing subscription at
    a different participant. Two users in one channel keep their own rows."""
    other = "999000111222333444"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no clobber", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT_ID,
            chat_type="group", user_id=USER_ID,
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT_ID,
            chat_type="group", user_id=other,
        )
        sub = _sub_for(conn, tid)
    finally:
        conn.close()

    assert sub["user_id"] == USER_ID


# ---------------------------------------------------------------------------
# 3 — negative control: a genuinely user-less origin
# ---------------------------------------------------------------------------


def test_userless_origin_still_delivers_and_is_not_fabricated(kanban_home):
    """A cron / CLI / home-channel subscription legitimately has no
    participant. It must still be listed for delivery, and no identity may be
    invented for it — a fabricated user_id would route a system notification
    into some human's private per-user session."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cron origin", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT_ID,
            chat_type="group",
        )
        sub = _sub_for(conn, tid)
    finally:
        conn.close()

    assert not sub["user_id"], "an identity was fabricated for a user-less origin"

    # Still deliverable: the wake resolves to the shared per-channel session,
    # which is the correct destination when nobody in particular owns the sub.
    wake_key = build_session_key(_wake_source(sub), group_sessions_per_user=True)
    assert wake_key.endswith(CHAT_ID)
    assert wake_key != _creator_key()
