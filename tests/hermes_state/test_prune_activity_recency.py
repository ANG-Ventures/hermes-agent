import time
from contextlib import closing
import pytest
from hermes_cli.sessions_cmd import _note_pinned_skipped
from hermes_state import SessionDB


def test_prune_sessions_respects_touch_session_activity(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        now = time.time()
        old_time = now - 100 * 86400  # 100 days ago
        recent_time = now - 2 * 86400  # 2 days ago

        # Session with older messages and started_at, but recent heartbeat / activity
        db.create_session("active_by_heartbeat", source="cli")
        db.append_message("active_by_heartbeat", "user", "hello", timestamp=old_time)
        db.end_session("active_by_heartbeat", "done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (old_time, old_time, "active_by_heartbeat"),
        )
        db._conn.commit()
        db.touch_session_activity("active_by_heartbeat", ts=recent_time)

        # Truly old session
        db.create_session("truly_old", source="cli")
        db.append_message("truly_old", "user", "old message", timestamp=old_time)
        db.end_session("truly_old", "done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (old_time, old_time, "truly_old"),
        )
        db._conn.commit()

        # Check dry-run candidates
        candidates = db.list_prune_candidates(older_than_days=10)
        candidate_ids = [c["id"] for c in candidates]
        assert "active_by_heartbeat" not in candidate_ids
        assert "truly_old" in candidate_ids

        candidate_map = {c["id"]: c for c in db.list_prune_candidates(older_than_days=None)}
        assert candidate_map["active_by_heartbeat"]["last_active"] == pytest.approx(recent_time, abs=1.0)

        # Pruning sessions older than 10 days should spare active_by_heartbeat
        pruned = db.prune_sessions(older_than_days=10)
        assert pruned == 1
        assert db.get_session("truly_old") is None
        assert db.get_session("active_by_heartbeat") is not None


def test_prune_keeps_the_compressed_segments_of_a_chat_still_in_use(tmp_path, capsys):
    """A rotated conversation ages as one: its old compressed-away segments stay while a later segment
    is still in use, and go together once the whole chat is idle. The CLI/dashboard preview lists what prune deletes."""
    with closing(SessionDB(tmp_path / "state.db")) as db:
        old = time.time() - 120 * 86400

        def rotate(parent, child, at):
            db.publish_compression_child(parent_session_id=parent, child_session_id=child, source="cli",
                                         messages=[{"role": "user", "content": "[summary]", "timestamp": at}],
                                         require_compression_lease=False)
            db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (at, parent))
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (at, child))
            db._conn.commit()

        for chat in ("live", "idle"):
            db.create_session(chat, source="cli")
            db.append_message(chat, "user", f"{chat} early detail", timestamp=old)
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old, chat))
            db._conn.commit()
            rotate(chat, f"{chat}-2", old + 60)
            rotate(f"{chat}-2", f"{chat}-3", old + 120)
        db.append_message("live-3", "user", "still talking today")
        db.end_session("idle-3", "done")
        db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 'idle-3'", (old + 180,))
        db._conn.commit()

        idle = {"idle", "idle-2", "idle-3"}
        # The CLI pinned-skip note counts pinned rows, not the unpinned ancestors a pinned tip spares.
        db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'idle-3'")
        db._conn.commit()
        _note_pinned_skipped(db, {"older_than_days": 90, "lineage_tips_only": False}, "prune")
        assert "Note: 1 pinned session also match" in capsys.readouterr().out
        db._conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 'idle-3'")
        db._conn.commit()
        assert {c["id"] for c in db.list_prune_candidates(older_than_days=90, whole_lineages=True)} == idle
        assert db.prune_sessions(older_than_days=90) == len(idle)
        assert db.get_compression_lineage("live-3") == ["live", "live-2", "live-3"]
        assert not any(db.get_session(sid) for sid in idle)


def test_prune_commits_per_chunk_not_one_transaction(tmp_path, monkeypatch):
    """A prune over more than one row budget commits each chunk on its own, so no single write-lock hold spans
    the whole prune (one BEGIN IMMEDIATE held 61.5 s for 872 sessions at load 160, t_0b0c41e3)."""
    import hermes_state_maintenance as maint
    monkeypatch.setattr(maint, "_PRUNE_TXN_ROW_BUDGET", 10)
    with closing(SessionDB(tmp_path / "state.db")) as db:
        old = time.time() - 120 * 86400
        for i in range(6):  # 6 sessions x (1 row + 4 messages) = 30 rows -> 3 chunks of 2 sessions
            sid = f"old-{i}"
            db.create_session(sid, source="cli")
            for j in range(4):
                db.append_message(sid, "user", f"m{j}", timestamp=old)
            db.end_session(sid, "done")
            db._conn.execute("UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?", (old, old, sid))
            db._conn.commit()
        db.create_session("recent", source="cli")

        commits = []
        orig = db._execute_write

        def counting(fn, patience_s=None):
            def wrapped(conn):
                result = fn(conn)
                commits.append(conn.execute("SELECT COUNT(*) FROM sessions WHERE id LIKE 'old-%'").fetchone()[0])
                return result
            return orig(wrapped, patience_s=patience_s)

        monkeypatch.setattr(db, "_execute_write", counting)
        assert db.prune_sessions(older_than_days=90) == 6
        # 3 delete chunks, each committed with fewer old rows than before, then one recompute/cleanup txn.
        assert commits == [4, 2, 0, 0], commits
        assert db.get_session("recent") is not None
