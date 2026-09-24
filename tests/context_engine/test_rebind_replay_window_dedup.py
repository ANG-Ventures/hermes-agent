"""Re-bind replay must not re-append the stored transcript (t_0877ab8b).

Live evidence (Apollo lcm.db snapshot 2026-09-24): 84% of rows ingested in a
week were exact duplicates. Every gateway re-bind of an existing session
(restart auto-resume, session switch) replays the post-compaction active
context ``[scaffold + summaries] + [fresh tail]``; the fresh tail was
re-appended because both replay proofs demand an EXACT contiguous suffix of
the stored tail, and the store routinely holds rows the replay does not carry
(orphan-recovery tool rows, background-review rows, earlier duplicate
replays). One missed replay made the next proof harder, so it compounded.

These tests drive the REAL ``compress`` (offline L3 summarizer) to build the
post-compaction context, then bind a fresh engine (a restarted process) to
the same session and replay it.
"""
from __future__ import annotations

import sqlite3

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine

SESSION = "rebind-sess"
ORPHAN = (
    "[Orphan recovery: interrupted side-effecting tool may have executed; "
    "its result was not observed.]"
)


def _cfg(tmp_path):
    return LCMConfig(
        database_path=str(tmp_path / "lcm.db"),
        leaf_chunk_tokens=120,
        condensation_fanin=2,
        fresh_tail_count=6,
        context_threshold=0.05,
        incremental_max_depth=3,
    )


def _engine(tmp_path):
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION)
    return eng


def _transcript(n_pairs=30):
    msgs = [{"role": "system", "content": "You are Apollo."}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"do task {i} " + ("w" * 30)})
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "run", "arguments": f'{{"i": {i}}}'}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + ("z" * 60)})
        msgs.append({"role": "assistant", "content": f"done {i}"})
    return msgs


def _rows(tmp_path):
    return sqlite3.connect(str(tmp_path / "lcm.db")).execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=?", (SESSION,)
    ).fetchone()[0]


def _count(tmp_path, content):
    return sqlite3.connect(str(tmp_path / "lcm.db")).execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND content=?", (SESSION, content)
    ).fetchone()[0]


def _compacted(tmp_path):
    """Run a real compaction and return (engine, post-compaction context)."""
    eng = _engine(tmp_path)
    compressed = eng.compress(list(_transcript()), current_tokens=10**9)
    assert eng._last_compression_status == "compacted", eng._last_compression_status
    assert any(eng._is_strong_compaction_scaffold(m) for m in compressed)
    return eng, compressed


def _store_only_row(eng):
    # A row the store holds but the host transcript never carries, e.g. the
    # orphan-recovery tool row written at shutdown.
    eng._store.append_batch(SESSION, [{"role": "tool", "tool_call_id": "c29", "content": ORPHAN}])


def _rebind_ingest(tmp_path, messages):
    fresh = _engine(tmp_path)  # restarted process: cursor unknown -> reconcile
    assert fresh._ingest_cursor_needs_reconcile
    fresh._ingest_messages([dict(m) for m in messages])
    return fresh


class TestRebindReplayWindowDedup:
    def test_replaying_real_compacted_context_twice_appends_nothing(self, tmp_path):
        eng, compressed = _compacted(tmp_path)
        _store_only_row(eng)
        n0 = _rows(tmp_path)
        _rebind_ingest(tmp_path, compressed)
        assert _rows(tmp_path) == n0
        _rebind_ingest(tmp_path, compressed)
        assert _rows(tmp_path) == n0

    def test_replay_over_already_duplicated_store_appends_nothing(self, tmp_path):
        # Backlog shape: the fresh tail was already re-appended once by an
        # earlier missed replay, then a store-only row landed after it.
        eng, compressed = _compacted(tmp_path)
        tail = [m for m in compressed if not eng._is_strong_compaction_scaffold(m)
                and not eng._is_replayed_context_scaffold_message(m)
                and m.get("role") != "system"]
        eng._store.append_batch(SESSION, [dict(m) for m in tail])
        _store_only_row(eng)
        n0 = _rows(tmp_path)
        _rebind_ingest(tmp_path, compressed)
        assert _rows(tmp_path) == n0

    def test_new_turn_repeating_stored_tail_is_still_appended(self, tmp_path):
        # No-loss control: a genuinely new turn whose content repeats the
        # last stored turn exactly must be appended, not skipped as replay.
        eng, compressed = _compacted(tmp_path)
        _store_only_row(eng)
        n0 = _rows(tmp_path)
        last_user = next(m for m in reversed(compressed) if m.get("role") == "user")
        last_asst = compressed[-1]
        assert last_asst.get("role") == "assistant"
        before_user = _count(tmp_path, last_user["content"])
        _rebind_ingest(tmp_path, compressed + [dict(last_user), dict(last_asst)])
        assert _rows(tmp_path) == n0 + 2
        assert _count(tmp_path, last_user["content"]) == before_user + 1

    def test_new_turn_after_clean_replay_is_appended(self, tmp_path):
        eng, compressed = _compacted(tmp_path)
        _store_only_row(eng)
        n0 = _rows(tmp_path)
        new = {"role": "user", "content": "GENUINELY_NEW_TURN"}
        _rebind_ingest(tmp_path, compressed + [new])
        assert _rows(tmp_path) == n0 + 1
        assert _count(tmp_path, "GENUINELY_NEW_TURN") == 1

    def test_delta_without_scaffold_is_never_deduplicated(self, tmp_path):
        # Dup-over-loss: an anchorless delta that repeats stored rows is kept.
        eng, compressed = _compacted(tmp_path)
        _store_only_row(eng)
        n0 = _rows(tmp_path)
        tail = [m for m in compressed if m.get("role") in ("user", "assistant", "tool")
                and not eng._is_strong_compaction_scaffold(m)
                and not eng._is_replayed_context_scaffold_message(m)]
        _rebind_ingest(tmp_path, tail[-2:])
        assert _rows(tmp_path) > n0
