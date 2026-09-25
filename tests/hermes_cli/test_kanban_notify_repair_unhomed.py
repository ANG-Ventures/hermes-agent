"""notify-repair negative control on the REAL user-less birth paths.

Cards created with no chat-session env are born ``session_id = 'unhomed'``
(never NULL), and ``backfill_unhomed`` turns legacy NULL rows into
``'unhomed'`` too. ``kanban notify-repair`` must treat that sentinel exactly
like missing creator provenance: a repair is a PERMANENT identity write, so a
user-less (cron / CLI / home-channel) card must never adopt the lone human in
its chat on lane evidence alone.

The routing index here uses the real 5-tuple entry shape
``(user_id, user_id_alt, scope_id, session_key, session_id)`` so the raw-id
branch of ``_resolve`` (which reads ``item[4]``) is actually exercised; the
positive control proves the same fixture CAN produce an adoption, so the
negative arms are not vacuous.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_notify_repair import (  # noqa: F401  (fixture)
    CHAT, USER, _run, _user_id_of, kanban_home,
)

_SESSION_ENV = (
    "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_NAME",
)
RAW_CREATOR = "20260924_000000_rawcreator"
HUMAN_SESSION = "20260101_000000_humansess"


def _routing5(monkeypatch, entries) -> None:
    """Gateway routing index with REAL 5-tuple participant entries."""
    index = {("discord", CHAT, "group", ""): set(entries)}
    monkeypatch.setattr(kc, "_routing_participant_index", lambda: index)


def _card(monkeypatch, origin: str) -> tuple[str, object]:
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cron origin", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id=CHAT,
            chat_type="group", user_id=None, user_id_alt=None, scope_id=None,
        )
        if origin in ("legacy_null", "legacy_null_backfilled"):
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET session_id = NULL WHERE id = ?", (tid,)
                )
        if origin == "legacy_null_backfilled":
            kb.backfill_unhomed(conn)
        if origin == "raw_creator":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET session_id = ? WHERE id = ?",
                    (RAW_CREATOR, tid),
                )
        stored = conn.execute(
            "SELECT session_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()[0]
    finally:
        conn.close()
    return tid, stored


@pytest.mark.parametrize(
    "origin, expected_stamp",
    [
        ("born_sessionless", kb.UNHOMED_SESSION),
        ("legacy_null", None),
        ("legacy_null_backfilled", kb.UNHOMED_SESSION),
    ],
)
def test_userless_origin_never_adopts_the_lone_human(
    kanban_home, monkeypatch, capsys, origin, expected_stamp
):
    tid, stored = _card(monkeypatch, origin)
    assert stored == expected_stamp, (origin, stored)

    # One unrelated human in the lane, bound to a raw session id.
    _routing5(monkeypatch, [(USER, "", "", f"agent:main:discord:group:{CHAT}:{USER}", HUMAN_SESSION)])
    assert _run() == 0

    assert not _user_id_of(tid), (
        f"identity fabricated for a user-less origin ({origin}, stamp={stored!r})"
    )
    out = capsys.readouterr().out
    assert "Left untouched (1)" in out


def test_positive_control_raw_creator_adopts_via_5tuple_fixture(
    kanban_home, monkeypatch
):
    """The same fixture + a real raw-id creator DOES repair: proves the raw-id
    branch is reachable and the negative arms above can go red."""
    tid, stored = _card(monkeypatch, "raw_creator")
    assert stored == RAW_CREATOR
    _routing5(monkeypatch, [(USER, "", "", f"agent:main:discord:group:{CHAT}:{USER}", RAW_CREATOR)])
    assert _run() == 0
    assert _user_id_of(tid) == USER
