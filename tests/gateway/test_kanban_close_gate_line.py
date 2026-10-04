"""t_dcc4ed08 (Apollo scope add, 2026-10-02 17:30): a changes_requested on a LANDED card whose
review only waits on a native close gate renders ``⏳ ... landed · close on: <gate>``, not
``🛑 ... changes/BLOCK``. A real send-back keeps 🛑. An operator batch folds to one digest line.
"""
import asyncio
import json
import time

from gateway import kanban_close_gate as cg
from gateway.kanban_lifecycle_digest import LifecycleDigest, fold_batches, render
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from tests.gateway.test_kanban_changes_requested_notifier import (
    RecordingAdapter,
    _run_one_tick,
    _runner,
)

PR = "ANG-Ventures/hermes-home#2475"
HEAD = "55e99cfd9a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _card(*, reason, items=None, head=HEAD, batch="apollo-1700", own_pr=PR, coverage=True):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fs-daemon-guard", assignee="daedalus",
                             session_id="agent:main:telegram:thread:chat-1:topic-7")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", thread_id="topic-7",
                          chat_type="thread", delivery_mode="notify",
                          delivery_metadata={"session_id": "topic-7", "chat_type": "thread"})
        if own_pr:
            conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at) "
                         "VALUES (?, 'daedalus', 'done', 'review_requested', ?, ?)",
                         (tid, json.dumps({"own_prs": [own_pr]}), int(time.time()) - 600))
        if coverage:
            cov = {"verdict": "CHANGES_REQUESTED", "head_sha": head, "findings": 1,
                   "items": items or [reason], "batch_id": batch}
            kb.add_comment(conn, tid, "default", "review_coverage: " + json.dumps(cov))
        kb._append_event(conn, tid, kind="changes_requested",
                         payload={"reason": reason, "reviewer": "human:apollo", "implementer": "daedalus",
                                  "status": "ready"})
        conn.commit()
        return tid
    finally:
        conn.close()


def _tick(monkeypatch, states):
    adapter = RecordingAdapter()
    runner = _runner(adapter)
    runner._kanban_close_gate_query = (lambda repo, n: states.get(f"{repo}#{n}"))
    asyncio.run(_run_one_tick(monkeypatch, runner))
    return adapter


def test_merged_at_coverage_head_renders_landed_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _card(reason="2 quiet ticks + mdutil -s")
    a = _tick(monkeypatch, {PR: {"state": "MERGED", "head_sha": HEAD}})
    text = a.sent[0]["text"]
    assert text.startswith(f"⏳ [default] Kanban {tid} landed · close on: 2 quiet ticks + mdutil -s"), text
    assert "implementer @daedalus" in text
    assert "🛑" not in text and "BLOCK" not in text and "requested changes" not in text


def test_mutant_merged_without_marker_still_landed(tmp_path, monkeypatch):
    """Merged = landed, whatever the reviewer typed (no close-on word anywhere)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    _card(reason="p99 < 200ms one hour")
    a = _tick(monkeypatch, {PR: {"state": "MERGED", "head_sha": HEAD}})
    assert a.sent[0]["text"].startswith("⏳ "), a.sent[0]["text"]


def test_marker_without_pr_state_renders_landed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    _card(reason="#2475 MERGED. Close on: fs-daemon-guard quiet 2 native ticks", own_pr=None)
    a = _tick(monkeypatch, {})
    assert a.sent[0]["text"].split(" · close on: ")[1].startswith("fs-daemon-guard quiet 2 native ticks")


def test_real_sendback_open_pr_keeps_stop_sign(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    _card(reason="F1: missing test for the empty-proof path", head="aaaaaaa1111")
    a = _tick(monkeypatch, {PR: {"state": "OPEN"}})
    assert a.sent[0]["text"].startswith("🛑 [default] Kanban "), a.sent[0]["text"]
    assert "changes/BLOCK: F1: missing test" in a.sent[0]["text"]


def test_open_pr_with_marker_is_still_a_sendback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    _card(reason="fix F1, then close on one green tick")
    a = _tick(monkeypatch, {PR: {"state": "OPEN"}})
    assert a.sent[0]["text"].startswith("🛑 ")


def test_merged_at_other_head_without_marker_is_a_sendback(tmp_path, monkeypatch):
    """Coverage names a head the merge did not carry: a post-merge follow-up, not a landed gate."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    _card(reason="fix the 5 Prism P1s in a new PR", head="bbbbbbbbbbbb")
    a = _tick(monkeypatch, {PR: {"state": "MERGED", "head_sha": HEAD}})
    assert a.sent[0]["text"].startswith("🛑 ")


def test_gate_marker_shapes():
    for s, g in (("Close on: one converged tick", "one converged tick"),
                 ("#2433 MERGED. Close on crons.ace 0 Down for 24 h", "crons.ace 0 Down for 24 h"),
                 ("Closes on a successful native tick", "a successful native tick"),
                 ("close when the drill pages once", "the drill pages once")):
        assert cg.gate_marker(s) == g, s
    for s in ("closes on merge", "Close on merged=true", "rebase onto main", "tooltip disclosed"):
        assert cg.gate_marker(s) is None, s


# Today's 8 cards from the 17:18 PT operator batch (reasons verbatim from the board, batch apollo-1700).
TODAY = [
    ("t_128ec7c6", "native tick after resume + live reservation skip", "daedalus"),
    ("t_5685054a", "one quiet autocommit cycle", "daedalus"),
    ("t_8c473fdc", "2 quiet ticks + mdutil -s", "daedalus"),
    ("t_9058a474", "p99 < 200ms one hour", "daedalus-opus"),
    ("t_9038e9f3", "one spawned run after restart window", "daedalus-opus"),
    ("t_df8b9c98", "next inherited-red episode lands unattended", "daedalus-opus"),
    ("t_180d564a", "one 6h digest cycle", "daedalus"),
    ("t_784d5a55", "ACE-AI failed=0 + doctor rc0", "daedalus"),
]


def _today_lines():
    lines, folds = [], []
    for tid, gate, who in TODAY:
        lines.append(f"⏳ [default] Kanban {tid} landed · close on: {gate} — implementer @{who}")
        folds.append({"key": "close-gate:apollo-1700", "task_id": tid, "gate": gate, "implementer": who,
                      "board_tag": "[default] ", "batch": "apollo-1700"})
    return lines, folds


def test_operator_batch_folds_to_one_line():
    lines, folds = _today_lines()
    other = "✔ [default] @athena Kanban t_00000001 done — unrelated"
    out = fold_batches([other] + lines, [None] + folds)
    assert len(out) == 2 and out[0] == other
    assert out[1].startswith("⏳ [default] Kanban 8 cards landed (batch apollo-1700) · close on: t_128ec7c6 ")
    for tid, gate, _ in TODAY:
        assert f"{tid} {gate}" in out[1]
    assert "🛑" not in out[1]


def test_lone_fold_key_renders_unchanged_and_no_key_never_folds():
    lines, folds = _today_lines()
    assert fold_batches(lines[:1], folds[:1]) == lines[:1]
    assert fold_batches(lines, [None] * len(lines)) == lines


def test_digest_posts_one_message_with_one_batch_line():
    lines, folds = _today_lines()
    d = LifecycleDigest()
    adapter = RecordingAdapter()
    for m, f in zip(lines, folds):
        d.add(("discord", "logs"), adapter, m, 900, 0.0, fold=f)
    assert asyncio.run(d.flush(1000.0)) == 1
    text = adapter.sent[0]["text"]
    assert len([l for l in text.splitlines() if l.startswith("⏳")]) == 1
    assert "Kanban 8 cards landed (batch apollo-1700)" in text
    print("\n" + text)  # the screenshot-equivalent for the handback


def test_render_single_batch_is_the_folded_line():
    lines, folds = _today_lines()
    assert render(lines, 0, folds).startswith("⏳ [default] Kanban 8 cards landed")


def test_watcher_folds_an_operator_batch_into_one_home_line(tmp_path, monkeypatch):
    """End to end on the live route shape (home routing + home digest): 3 landed send-backs of one
    operator batch -> ONE ⏳ line in the card's home chat; a real send-back in the same tick stays 🛑."""
    import yaml
    from gateway.config import Platform
    from gateway import kanban_home_route as hr
    import gateway.kanban_watchers as kw
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"kanban": {
        "lifecycle_channel": "telegram:logs", "lifecycle_home_digest_seconds": 120}}))
    sid = "20260924_121319_4865b4f3"
    monkeypatch.setattr(hr, "read_session_row", lambda s: {
        "origin_json": json.dumps({"platform": "telegram", "chat_id": "home", "chat_type": "group"}),
        "source": "telegram"} if s == sid else None)
    conn = kb.connect()
    try:
        ids = []
        for i, gate in enumerate(("2 quiet ticks + mdutil -s", "p99 < 200ms one hour", "one 6h digest cycle")):
            pr = f"ANG-Ventures/hermes-home#{2470 + i}"
            tid = kb.create_task(conn, title=f"c{i}", assignee="daedalus", session_id=sid)
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="home", chat_type="group",
                              delivery_mode="notify")
            conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at) "
                         "VALUES (?, 'daedalus', 'done', 'review_requested', ?, 1)",
                         (tid, json.dumps({"own_prs": [pr]})))
            kb.add_comment(conn, tid, "default", "review_coverage: " + json.dumps(
                {"head_sha": HEAD, "items": [gate], "batch_id": "apollo-1700"}))
            kb._append_event(conn, tid, kind="changes_requested",
                             payload={"reason": gate, "implementer": "daedalus", "reviewer": "human:apollo"})
            ids.append(tid)
        real = kb.create_task(conn, title="real", assignee="daedalus", session_id=sid)
        kbn.add_notify_sub(conn, task_id=real, platform="telegram", chat_id="home", chat_type="group",
                          delivery_mode="notify")
        kb._append_event(conn, real, kind="changes_requested",
                         payload={"reason": "F1: fix the race", "implementer": "daedalus"})
        conn.commit()
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = _runner(adapter)
    runner.adapters = {Platform.TELEGRAM: adapter}
    states = {f"ANG-Ventures/hermes-home#{2470 + i}": {"state": "MERGED", "head_sha": HEAD} for i in range(3)}
    runner._kanban_close_gate_query = lambda repo, n: states.get(f"{repo}#{n}")
    clock = [1000.0]
    monkeypatch.setattr(kw.time, "time", lambda: clock[0])
    asyncio.run(_run_one_tick(monkeypatch, runner))
    clock[0] += 200
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    texts = "\n".join(m["text"] for m in adapter.sent)
    landed = [l for l in texts.splitlines() if l.startswith("⏳")]
    assert len(landed) == 1, texts
    assert "Kanban 3 cards landed (batch apollo-1700)" in landed[0]
    assert all(t in landed[0] for t in ids)
    assert f"🛑 [default] Kanban {real} review requested changes/BLOCK: F1: fix the race" in texts
