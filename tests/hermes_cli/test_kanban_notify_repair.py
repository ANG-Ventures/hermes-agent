"""``hermes kanban notify-repair`` — backfill complete key identity on legacy
notify subscriptions.

``add_notify_sub`` is ``INSERT OR IGNORE``, so a subscription nobody
re-subscribes to can never self-heal missing ``user_id``, ``user_id_alt``, or
``scope_id`` — and while the identity is incomplete the wake injector can
rebuild a different key, splitting one chat into two sessions (see
``tests/gateway/test_kanban_notify_sub_user_id.py`` for that half).

The rules pinned here are the ones that make the repair safe to run against a
live board:

* it backfills only when the creator identity is UNAMBIGUOUS,
* it never invents an identity for a genuinely user-less origin,
* it never re-points a row that already names a participant,
* it is idempotent, and ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

CHAT = "1535189663533506600"
USER = "117431298246705156"
OTHER_USER = "999000111222333444"
CRON_CHAT = "1523978409129021484"
CREATOR_SESSION = "agent:main:test:group:creator"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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


def _identity(user_id: str, user_id_alt: str = "", scope_id: str = "") -> tuple:
    """One routing-index candidate: raw id, alt id, and workspace scope."""
    return (user_id, user_id_alt, scope_id)


def _routing(monkeypatch, chats: dict[tuple, list]) -> None:
    """Stand in for the gateway routing index with explicit lane identities."""
    index = {
        (key if len(key) == 4 else (*key, "group", "")): {
            (
                candidate if len(candidate) == 4 else (*candidate, CREATOR_SESSION)
            ) if isinstance(candidate, tuple) else (*_identity(candidate), CREATOR_SESSION)
            for candidate in candidates
        }
        for key, candidates in chats.items()
    }
    monkeypatch.setattr(kc, "_routing_participant_index", lambda: index)


def _sub(
    conn, task_id: str, *, platform: str = "discord", chat_id: str = CHAT,
    chat_type: str = "group", user_id=None, user_id_alt=None, scope_id=None,
    creator_session_id: str | None = CREATOR_SESSION,
) -> None:
    kb.add_notify_sub(
        conn, task_id=task_id, platform=platform, chat_id=chat_id,
        chat_type=chat_type, user_id=user_id, user_id_alt=user_id_alt,
        scope_id=scope_id,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            (creator_session_id, task_id),
        )


def _run(**kwargs) -> int:
    args = argparse.Namespace(dry_run=False, json=False)
    for key, value in kwargs.items():
        setattr(args, key, value)
    return kc._cmd_notify_repair(args)


def _identity_of(task_id: str):
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id)
        assert len(subs) == 1, subs
        return {
            field: subs[0].get(field)
            for field in ("user_id", "user_id_alt", "scope_id")
        }
    finally:
        conn.close()


def _user_id_of(task_id: str):
    return _identity_of(task_id)["user_id"]


def test_backfills_the_unambiguous_creator_identity(kanban_home, monkeypatch, capsys):
    """One known participant for the chat -> the empty row is repaired."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()
    assert not _user_id_of(tid)

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0

    assert _user_id_of(tid) == USER
    assert "Backfilled 1 of 1" in capsys.readouterr().out


def test_dry_run_reports_without_writing(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="dry", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run(dry_run=True) == 0

    assert not _user_id_of(tid), "--dry-run wrote to the DB"
    assert "Would backfill 1 of 1" in capsys.readouterr().out


def test_userless_origin_is_left_alone(kanban_home, monkeypatch, capsys):
    """NEGATIVE CONTROL. A cron / CLI / home-channel subscription has no
    participant by construction. The repair must report it and move on — a
    fabricated identity would route a system notification into a human's
    private per-user session."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cron origin", assignee="worker")
        _sub(conn, tid, creator_session_id=None)
    finally:
        conn.close()

    # One unrelated human exists in the same lane. Missing creator provenance
    # must still keep this legitimate system subscription shared.
    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0

    assert not _user_id_of(tid), "an identity was fabricated for a user-less origin"
    out = capsys.readouterr().out
    assert "Left untouched (1)" in out
    assert "shared per-chat session" in out


def test_ambiguous_shared_chat_is_left_alone(kanban_home, monkeypatch):
    """Two humans in one channel: there is no single right answer, and picking
    one would hijack the other's notification lane."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="shared", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER, OTHER_USER]})
    assert _run() == 0

    assert not _user_id_of(tid)


def test_never_repoints_a_row_that_already_has_an_identity(kanban_home, monkeypatch):
    """Rows naming a participant are outside the repair's WHERE clause, so a
    different resolved identity cannot steal an existing subscription."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned", assignee="worker")
        _sub(conn, tid, user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [OTHER_USER]})
    assert _run() == 0

    assert _user_id_of(tid) == USER


def test_repair_is_idempotent(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="twice", assignee="worker")
        _sub(conn, tid)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run() == 0
    capsys.readouterr()

    assert _run() == 0
    assert _user_id_of(tid) == USER
    assert "nothing to repair" in capsys.readouterr().out


def test_json_report_separates_repaired_from_skipped(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        good = kb.create_task(conn, title="good", assignee="worker")
        _sub(conn, good)
        cron = kb.create_task(conn, title="cron", assignee="worker")
        _sub(conn, cron, chat_id=CRON_CHAT)
    finally:
        conn.close()

    _routing(monkeypatch, {("discord", CHAT): [USER]})
    assert _run(json=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["considered"] == 2
    assert report["backfilled"] == 1
    assert report["skipped_no_evidence"] == 1
    by_task = {r["task_id"]: r for r in report["rows"]}
    assert by_task[good]["user_id"] == USER
    assert by_task[good]["action"] == "backfilled"
    assert by_task[cron]["user_id"] is None
    assert by_task[cron]["action"] == "skipped_no_evidence"


def test_real_routing_index_repairs_alt_key_and_scope_on_a_named_legacy_row(
    kanban_home, monkeypatch, tmp_path,
):
    """Regression: the durable routing entry carries all three fields, and its
    key ends in user_id_alt. A legacy row already has user_id, so the old repair
    skipped it entirely and could never reconstruct the creator's key."""
    import hermes_state

    alt_id = "uuid-aaaa-bbbb"
    scope_id = "signal-account-1"
    creator_key = f"agent:main:signal:group:{CHAT}:{alt_id}"
    state_path = tmp_path / "routing-state.db"
    real_db = hermes_state.SessionDB(db_path=state_path)
    real_db.save_gateway_routing_entry(
        creator_key,
        json.dumps({
            "origin": {
                "platform": "signal",
                "chat_id": CHAT,
                "chat_type": "group",
                "user_id": USER,
                "user_id_alt": alt_id,
                "scope_id": scope_id,
            },
        }),
    )
    # Same chat, different human, in a sibling per-user thread. Its valid
    # participant suffix must not make the group lane ambiguous.
    real_db.save_gateway_routing_entry(
        f"agent:main:signal:thread:{CHAT}:42:uuid-thread-user",
        json.dumps({
            "origin": {
                "platform": "signal",
                "chat_id": CHAT,
                "chat_type": "thread",
                "thread_id": "42",
                "user_id": USER,
                "user_id_alt": "uuid-thread-user",
            },
        }),
    )
    # A shared group key can coincidentally end in the participant bytes when
    # chat and participant namespaces overlap. Full-tail validation must reject
    # it; a bare endswith(participant) check would poison this group lane.
    real_db.save_gateway_routing_entry(
        f"agent:main:signal:group:{CHAT}",
        json.dumps({
            "origin": {
                "platform": "signal",
                "chat_id": CHAT,
                "chat_type": "group",
                "user_id_alt": CHAT,
            },
        }),
    )
    prospective_key = (
        f"agent:main:signal:thread:{CHAT}:99:uuid-prospective-user"
    )
    real_db.save_gateway_routing_entry(
        prospective_key,
        json.dumps({
            "origin": {
                "platform": "signal",
                "chat_id": CHAT,
                "chat_type": "group",
                "prospective_thread_id": "99",
                "user_id": USER,
                "user_id_alt": "uuid-prospective-user",
            },
        }),
    )
    real_db.close()
    real_session_db = hermes_state.SessionDB
    monkeypatch.setattr(
        hermes_state, "SessionDB", lambda: real_session_db(db_path=state_path),
    )
    assert kc._routing_participant_index() == {
        ("signal", CHAT, "group", ""): {(USER, alt_id, scope_id, creator_key)},
        ("signal", CHAT, "thread", "42"): {(
            USER, "uuid-thread-user", "",
            f"agent:main:signal:thread:{CHAT}:42:uuid-thread-user",
        )},
        ("signal", CHAT, "thread", "99"): {(
            USER, "uuid-prospective-user", "", prospective_key,
        )},
    }

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy signal", assignee="worker")
        _sub(
            conn, tid, platform="signal", user_id=USER,
            creator_session_id=creator_key,
        )
    finally:
        conn.close()

    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": alt_id, "scope_id": scope_id,
    }


def test_repairs_slack_scope_without_repointing_the_named_user(
    kanban_home, monkeypatch,
):
    workspace = "T_WORKSPACE"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy slack", assignee="worker")
        _sub(conn, tid, platform="slack", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {("slack", CHAT): [_identity(USER, "", workspace)]})
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": workspace,
    }


def test_real_routing_index_repairs_slack_dm_scope_from_the_dm_key_shape(
    kanban_home, monkeypatch, tmp_path,
):
    """A Slack DM key omits the participant suffix, but its exact scoped DM tail
    is durable evidence for the workspace needed to reconstruct that key."""
    import hermes_state

    workspace = "T_WORKSPACE"
    dm_chat = "D1"
    creator_key = f"agent:main:slack:dm:{workspace}:{dm_chat}"
    state_path = tmp_path / "slack-routing-state.db"
    real_db = hermes_state.SessionDB(db_path=state_path)
    real_db.save_gateway_routing_entry(
        creator_key,
        json.dumps({
            "origin": {
                "platform": "slack",
                "chat_id": dm_chat,
                "chat_type": "dm",
                "user_id": USER,
                "scope_id": workspace,
            },
        }),
    )
    real_db.close()
    real_session_db = hermes_state.SessionDB
    monkeypatch.setattr(
        hermes_state, "SessionDB", lambda: real_session_db(db_path=state_path),
    )

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy slack dm", assignee="worker")
        _sub(
            conn, tid, platform="slack", chat_id=dm_chat,
            chat_type="dm", user_id=USER, creator_session_id=creator_key,
        )
    finally:
        conn.close()

    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": workspace,
    }


def test_alt_repair_refuses_ambiguous_participants(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ambiguous signal", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {
        ("signal", CHAT): [
            _identity(USER, "uuid-a"),
            _identity(USER, "uuid-b"),
        ],
    })
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }


@pytest.mark.parametrize("candidates", [
    [_identity(USER, "uuid-a"), _identity(USER, "uuid-b")],
    [
        _identity(USER, "uuid-a", "workspace-a"),
        _identity(USER, "uuid-a", "workspace-b"),
    ],
])
def test_repair_uniqueness_covers_the_complete_identity_tuple(
    kanban_home, monkeypatch, candidates,
):
    """Never collapse candidates merely because one identity field matches.

    Otherwise the repair could interleave user_id from one routing entry with
    user_id_alt or scope_id from another and reconstruct neither real key.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="tuple collision", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {("signal", CHAT): candidates})
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }


def test_existing_user_narrows_a_shared_lane_before_alt_fill(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="named shared lane", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {("signal", CHAT): [
        _identity(USER, "uuid-user"),
        _identity(OTHER_USER, "uuid-other"),
    ]})
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": "uuid-user", "scope_id": None,
    }


def test_existing_scope_narrows_a_shared_slack_lane_before_user_fill(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="scoped shared lane", assignee="worker")
        _sub(conn, tid, platform="slack", scope_id="workspace-a")
    finally:
        conn.close()

    _routing(monkeypatch, {("slack", CHAT): [
        _identity(USER, "", "workspace-a"),
        _identity(OTHER_USER, "", "workspace-b"),
    ]})
    assert _run() == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": "workspace-a",
    }


@pytest.mark.parametrize("candidate_user_id", [OTHER_USER, ""])
def test_alt_repair_never_grafts_an_alt_id_onto_a_different_named_user(
    kanban_home, monkeypatch, candidate_user_id, capsys,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned signal", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {
        ("signal", CHAT): [_identity(candidate_user_id, "uuid-other")],
    })
    assert _run(json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backfilled"] == 0
    assert report["skipped_no_evidence"] == 1
    assert report["rows"][0]["action"] == "skipped_no_evidence"
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }


def test_db_helper_rejects_alt_without_matching_raw_owner(kanban_home):
    """Defense in depth: callers other than the CLI cannot bypass the raw-owner
    proof required before adding a higher-priority alternate id."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="direct DB guard", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
        results = kb.backfill_notify_sub_user_ids(
            conn,
            lambda _row: {
                "user_id": "",
                "user_id_alt": "uuid-without-raw-owner",
                "scope_id": "",
            },
        )
    finally:
        conn.close()

    assert results == []
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }


def test_concurrent_owner_change_cannot_interleave_alt_or_scope(
    kanban_home,
):
    """The scan precedes the write transaction. If another writer names the row
    in that window, every field update must refuse the now-foreign identity."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="raced owner", assignee="worker")
        _sub(conn, tid, platform="signal")

        def resolve_after_race(_row):
            with kb.connect_closing() as racer:
                with kb.write_txn(racer):
                    racer.execute(
                        "UPDATE kanban_notify_subs SET user_id = ? WHERE task_id = ?",
                        (OTHER_USER, tid),
                    )
            return {
                "user_id": USER,
                "user_id_alt": "uuid-owned-by-user",
                "scope_id": "signal-account-user",
            }

        results = kb.backfill_notify_sub_user_ids(conn, resolve_after_race)
    finally:
        conn.close()

    assert _identity_of(tid) == {
        "user_id": OTHER_USER, "user_id_alt": None, "scope_id": None,
    }
    assert results == [{
        "task_id": tid,
        "platform": "signal",
        "chat_id": CHAT,
        "thread_id": "",
        "user_id": OTHER_USER,
        "user_id_alt": None,
        "scope_id": None,
        "backfilled_fields": [],
        "action": "skipped_raced",
    }]


def test_routing_index_failure_is_reported_as_unavailable(
    kanban_home, monkeypatch, capsys,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unavailable evidence", assignee="worker")
        _sub(conn, tid, user_id=USER)
    finally:
        conn.close()

    monkeypatch.setattr(kc, "_routing_participant_index", lambda: None)
    assert _run(json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backfilled"] == 0
    assert report["skipped_evidence_unavailable"] == 1
    assert report["rows"][0]["action"] == "skipped_evidence_unavailable"


def test_alt_and_scope_dry_run_reports_without_writing(
    kanban_home, monkeypatch, capsys,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="dry alt", assignee="worker")
        _sub(conn, tid, platform="signal", user_id=USER)
    finally:
        conn.close()

    _routing(monkeypatch, {
        ("signal", CHAT): [_identity(USER, "uuid-dry", "account-dry")],
    })
    assert _run(dry_run=True) == 0
    assert _identity_of(tid) == {
        "user_id": USER, "user_id_alt": None, "scope_id": None,
    }
    assert "Would backfill 1 of 1" in capsys.readouterr().out
