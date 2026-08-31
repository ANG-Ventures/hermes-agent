"""Tests for SessionStore.rewind_session — the gateway /undo [N] primitive.

The gateway /undo backs up N half-turns by soft-deleting rows in state.db
(active=0, kept for audit, hidden from re-prompts/search) via the shared
undo core. load_transcript returns only the active view. See issue #21910.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from gateway.config import GatewayConfig
from gateway.session import SessionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = db  # use the same DB instance the fixture seeds
    return s


def _seed(store, sid, source="telegram", turns=3):
    store._db.create_session(sid, source=source)
    for i in range(1, turns + 1):
        store._db.append_message(sid, "user", f"q{i}")
        store._db.append_message(sid, "assistant", f"a{i}")
    return sid


def test_rewind_default_one_turn(store):
    sid = _seed(store, "gw-1")
    res = store.rewind_session(sid)
    assert res["rewound_ids"]
    assert res["prefill_text"] == "q3"
    assert len(res["rewound_ids"]) == 1  # a3
    active = store.load_transcript(sid)
    assert [m["role"] for m in active] == ["user", "assistant", "user", "assistant", "user"]


def test_rewind_n_turns(store):
    sid = _seed(store, "gw-2")
    res = store.rewind_session(sid, 2)
    assert len(res["rewound_ids"]) == 2  # q3,a3
    assert res["prefill_text"] is None
    assert len(store.load_transcript(sid)) == 4  # q1,a1,q2,a2


def test_rewind_operates_on_raw_active_rows_not_projection(store):
    """The fork /undo (shared hermes_undo core, half-turn semantics) computes
    its target on the RAW active row set, not the replay projection.

    Legacy background-review rows are intentionally absent from replay, but
    remain physical active rows. The rewind must neither lose them nor corrupt
    the visible transcript: exactly one raw half-turn (the physical tail, here
    the curator-only reply) is soft-deleted, everything else stays active.

    (Upstream's inline full-user-turn CAS rewind asserted target_text == "q2"
    / rewound_count == 4 here; that mechanism was retired in favor of the
    fork's hermes_undo core — RESOLUTION-LEDGER-2026-08-29.md rows 96/97/111.)
    """
    sid = _seed(store, "gw-review-harness", turns=2)
    store._db.append_message(
        sid,
        "user",
        "Review the conversation above and update the skill library safely",
    )
    store._db.append_message(sid, "assistant", "curator-only reply")

    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
        "q2",
        "a2",
    ]
    raw_before = store._db._conn.execute(
        "SELECT id, content FROM messages "
        "WHERE session_id = ? AND active = 1 ORDER BY id",
        (sid,),
    ).fetchall()
    curator_reply_id = raw_before[-1][0]

    result = store.rewind_session(sid)

    assert result is not None
    assert "status" not in result
    # Half-turn contract: only the raw physical tail was retired, soft-deleted
    # (recoverable for audit), not hard-deleted.
    assert result["rewound_ids"] == [curator_reply_id]
    rows = store._db._conn.execute(
        "SELECT id, active FROM messages WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (row_id, 1) for row_id, _ in raw_before[:-1]
    ] + [(curator_reply_id, 0)]
    # The visible transcript is uncorrupted.
    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
        "q2",
        "a2",
    ]


_CAS_STOP_REASON = (
    "STOP-B1 (docs/sync/review/FIXPASS-LOG-B.md): fail-closed CAS for the plain "
    "/undo path requires adopting expected_active_ids in hermes_undo.undo's "
    "rewind_to_message call — hermes_undo.py is outside card-B file ownership "
    "(^tests/gateway/ + gateway/ only). Until that lands, a cross-process "
    "append racing /undo between its snapshot read and its write is silently "
    "soft-deleted along with the rewind."
)


@pytest.mark.xfail(reason=_CAS_STOP_REASON, strict=False)
def test_rewind_fails_closed_when_transcript_changes_after_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-cas", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    original_rewind = store._db.rewind_to_message

    def _append_then_rewind(*args, **kwargs):
        sibling.append_message(sid, "assistant", "concurrent tail")
        return original_rewind(*args, **kwargs)

    monkeypatch.setattr(store._db, "rewind_to_message", _append_then_rewind)

    result = store.rewind_session(sid)
    # Fail-closed contract: when the active transcript changed between the
    # snapshot read and the write, NOTHING may be mutated. (The fork's honesty
    # contract reports the conflict as a retryable sentinel, never a silent
    # success — any of None/busy/error is acceptable as long as no row moved.)
    assert result is None or result.get("status") in ("busy", "error")

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("concurrent tail", 1),
    ]
    sibling.close()


@pytest.mark.xfail(reason=_CAS_STOP_REASON, strict=False)
def test_rewind_fails_closed_when_new_turn_lands_after_id_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-snapshot-order", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    # The fork undo core snapshots via SessionDB.get_messages (upstream's
    # inline rewind used get_messages_as_conversation); inject the race at the
    # read the live code path actually performs, one-shot so the post-commit
    # prefill read doesn't re-fire it.
    original_load = store._db.get_messages
    fired = {"done": False}

    def _load_then_append(*args, **kwargs):
        snapshot = original_load(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            sibling.append_message(sid, "user", "q3-from-other-process")
            sibling.append_message(sid, "assistant", "a3-from-other-process")
        return snapshot

    monkeypatch.setattr(store._db, "get_messages", _load_then_append)

    result = store.rewind_session(sid)
    assert result is None or result.get("status") in ("busy", "error")

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("q3-from-other-process", 1),
        ("a3-from-other-process", 1),
    ]
    sibling.close()
