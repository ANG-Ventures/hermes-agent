"""Conversation prefix-stability guard (card t_c07124ab).

The invariant: between consecutive requests of one session the system prompt,
the tools and every already-sent message must be byte-identical (only the
previous request's LAST message may change; new messages append). A violation
is the class that collapses every prompt-cache read to the static prefix.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from plugins import blackbox
from plugins.blackbox import prefix_guard as pg
from plugins.blackbox import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store._connect().close()
    return store._db_path()


@pytest.fixture
def enabled(monkeypatch):
    cfg = {"enabled": True, "alerts_enabled": False, "record_subagents": True,
           "retention_days": 3650, "prefix_guard": True}
    monkeypatch.setattr(blackbox, "_config", lambda: cfg)
    return cfg


def _request(n_msgs: int = 4, *, cache_control_on: int | None = None) -> dict:
    messages = []
    for i in range(n_msgs):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": [{"type": "text", "text": f"m{i}"}]})
    if cache_control_on is not None:
        messages[cache_control_on]["content"][0]["cache_control"] = {"type": "ephemeral"}
    return {
        "system": [{"type": "text", "text": "SOUL", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "tools": [{"name": "terminal", "input_schema": {"type": "object"}}],
        "messages": messages,
    }


def _grow(req: dict, k: int = 2) -> dict:
    """Append k messages — the legal way a conversation changes."""
    out = copy.deepcopy(req)
    n = len(out["messages"])
    for i in range(n, n + k):
        out["messages"].append({"role": "user" if i % 2 == 0 else "assistant",
                                "content": [{"type": "text", "text": f"m{i}"}]})
    return out


def _check(seq: int, req: dict, *, session="sess-A", pid=None, model="claude-opus-4-8",
           api_mode="anthropic_messages", cache_read=None, reset=None, allowlist=None,
           ts=None, provider="claude-apr", turn=None, prompt_tokens=None,
           compare_across_turns=True):
    return store.record_prefix_check(
        session_key=session, turn_id=f"turn-{seq}" if turn is None else turn, seq=seq,
        ts=1_000.0 + seq if ts is None else ts, pid=os.getpid() if pid is None else pid,
        provider=provider, model=model, api_mode=api_mode,
        fingerprint=pg.fingerprint_request(req), cache_read=cache_read, reset=reset,
        allowlist=allowlist, prompt_tokens=prompt_tokens,
        compare_across_turns=compare_across_turns,
    )


# --- fingerprint --------------------------------------------------------------

def test_fingerprint_ignores_cache_control_placement():
    """Breakpoint markers move as the conversation grows; they are not content."""
    a = pg.fingerprint_request(_request(4, cache_control_on=1))
    b = pg.fingerprint_request(_request(4, cache_control_on=3))
    assert a == b
    assert a["system"] is not None and a["tools"] is not None and len(a["messages"]) == 4
    assert all(isinstance(h, str) and isinstance(n, int) for h, n in a["messages"])


def test_fingerprint_retains_no_text():
    fp = pg.fingerprint_request(_request(2))
    assert "SOUL" not in json.dumps(fp) and "m0" not in json.dumps(fp)


def test_fingerprint_openai_chat_and_responses_shapes():
    chat = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}],
            "tools": [{"type": "function", "function": {"name": "t"}}]}
    fp = pg.fingerprint_request(chat)
    assert fp["system"] is not None and len(fp["messages"]) == 1
    responses = {"instructions": "S", "input": [{"role": "user", "content": "u"},
                                                {"type": "compaction", "encrypted_content": "abc"}]}
    fp2 = pg.fingerprint_request(responses)
    assert fp2["system"] is not None and len(fp2["messages"]) == 2 and fp2["checkpoint"]
    assert pg.fingerprint_request({"model": "x"}) is None
    assert pg.fingerprint_request("not a dict") is None


# --- compare ------------------------------------------------------------------

def test_compare_holds_for_append_and_last_message_change():
    prev = pg.fingerprint_request(_request(4))
    assert pg.compare(prev, pg.fingerprint_request(_grow(_request(4), 2))) == []
    last_changed = _request(4)
    last_changed["messages"][-1]["content"][0]["text"] = "assistant streamed more"
    assert pg.compare(prev, pg.fingerprint_request(last_changed)) == []
    # identical request re-sent (retry) is also fine
    assert pg.compare(prev, prev) == []


def test_compare_names_first_divergent_index_and_segments():
    prev = pg.fingerprint_request(_request(6))
    cur_req = _grow(_request(6), 2)
    cur_req["messages"][0]["content"][0]["text"] = "INJECTED PARAGRAPH"
    cur_req["messages"][2]["content"][0]["text"] = "also changed"
    cur_req["system"][0]["text"] = "SOUL v2"
    cur_req["tools"].append({"name": "browser", "input_schema": {}})
    out = pg.compare(prev, pg.fingerprint_request(cur_req))
    by_seg = {v["segment"]: v for v in out}
    assert set(by_seg) == {"system", "tools", "messages"}
    assert by_seg["messages"]["kind"] == pg.KIND_MUTATION
    assert by_seg["messages"]["first_divergent_index"] == 0  # the msg[0] toggle class
    assert by_seg["messages"]["bytes_before"] != by_seg["messages"]["bytes_after"]
    assert by_seg["system"]["kind"] == pg.KIND_MUTATION and by_seg["tools"]["kind"] == pg.KIND_MUTATION


def test_compare_turn_boundary_drops_last_message_exemption():
    """Within a turn the previous request's last message may still change;
    across a turn boundary it is persisted history and must be byte-stable."""
    prev = pg.fingerprint_request(_request(4))
    tail_rewritten = _grow(_request(4), 2)
    tail_rewritten["messages"][3]["content"][0]["text"] = "persisted differently"
    cur = pg.fingerprint_request(tail_rewritten)
    assert pg.compare(prev, cur) == []
    out = pg.compare(prev, cur, turn_boundary=True)
    assert [(v["segment"], v["kind"], v["first_divergent_index"]) for v in out] == [
        ("messages", pg.KIND_MUTATION, 3)]
    # a clean append is still clean at a boundary, and a one-shorter history is a shrink
    assert pg.compare(prev, pg.fingerprint_request(_grow(_request(4), 2)), turn_boundary=True) == []
    assert [v["kind"] for v in pg.compare(prev, pg.fingerprint_request(_request(3)),
                                          turn_boundary=True)] == [pg.KIND_SHRINK]


def test_compare_reports_shrink_not_mutation():
    prev = pg.fingerprint_request(_request(6))
    out = pg.compare(prev, pg.fingerprint_request(_request(2)))
    assert [v["kind"] for v in out] == [pg.KIND_SHRINK]
    assert out[0]["first_divergent_index"] == 2


# --- store: persistence + once-per-session transition -------------------------

def test_store_persists_mutation_row_and_pages_once_per_session(db):
    r0 = _check(0, _request(4), cache_read=199_607)
    assert r0["violations"] == [] and r0["alert"] is False and r0["previous"] is None

    bad = _grow(_request(4), 2)
    bad["messages"][0]["content"][0]["text"] = "INJECTED"
    r1 = _check(1, bad, cache_read=14_947)
    assert r1["alert"] is True and r1["suppressed"] == 0
    assert r1["previous"]["messages"] == 4 and r1["previous"]["cache_read"] == 199_607
    assert [v["first_divergent_index"] for v in r1["violations"]] == [0]

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM prefix_mutations").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert (row["session_key"], row["turn_id"], row["seq"], row["prev_turn_id"], row["prev_seq"]) == (
            "sess-A", "turn-1", 1, "turn-0", 0)
        assert (row["segment"], row["kind"], row["first_divergent_index"]) == ("messages", "mutation", 0)
        assert (row["messages_before"], row["messages_after"]) == (4, 6)
        assert (row["cache_read_before"], row["cache_read_after"]) == (199_607, 14_947)
        assert row["context"] is None and row["allowlisted"] == 0 and row["alerted"] == 1
        assert row["lane_family"] == "apx/apr" and row["provider"] == "claude-apr"
        sess = conn.execute("SELECT * FROM prefix_sessions WHERE session_key='sess-A'").fetchone()
        assert sess["turn_id"] == "turn-1" and sess["alerted_at"] is not None
        assert "INJECTED" not in sess["fingerprint_json"]

    # A second mutation later in the SAME session is recorded but never pages again.
    worse = _grow(bad, 2)
    worse["messages"][3]["content"][0]["text"] = "changed again"
    r2 = _check(2, worse, ts=1_000.0 + 2 + 3600)
    assert r2["alert"] is False and len(r2["violations"]) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*), SUM(alerted) FROM prefix_mutations").fetchone() == (2, 1)


def test_store_profile_floor_holds_pages_and_reports_suppressed(db):
    def mutate_session(session, ts):
        _check(0, _request(4), session=session, ts=ts)
        bad = _grow(_request(4))
        bad["messages"][1]["content"][0]["text"] = "X"
        return _check(1, bad, session=session, ts=ts + 1)

    assert mutate_session("s1", 10_000.0)["alert"] is True
    held = mutate_session("s2", 10_000.0 + 60)          # within the 15-min floor
    assert held["alert"] is False and held["suppressed"] == 1
    held2 = mutate_session("s3", 10_000.0 + 120)
    assert held2["alert"] is False and held2["suppressed"] == 2
    later = mutate_session("s4", 10_000.0 + store.PREFIX_ALERT_MIN_SPACING_S + 5)
    assert later["alert"] is True and later["suppressed"] == 2  # tells the operator what it held
    with sqlite3.connect(db) as conn:
        # every held session still consumed its single slot: 4 rows, 2 paged
        assert conn.execute("SELECT COUNT(*), SUM(alerted) FROM prefix_mutations").fetchone() == (4, 2)
        assert conn.execute("SELECT COUNT(*) FROM prefix_sessions WHERE alerted_at IS NOT NULL").fetchone()[0] == 4


@pytest.mark.parametrize("kwargs, expected", [
    ({"reset": "compaction:threshold"}, "compaction:threshold"),
    ({"pid": 424242}, "process_restart"),
    ({"model": "claude-sonnet-5"}, "model_change"),
    ({"api_mode": "openai_chat"}, "api_mode_change"),
])
def test_store_tags_expected_rewrites_and_does_not_page(db, kwargs, expected):
    _check(0, _request(6))
    rewritten = _request(3)  # compaction: history rewritten and shorter
    rewritten["messages"][0]["content"][0]["text"] = "summary"
    r = _check(1, rewritten, **kwargs)
    assert r["alert"] is False and r["violations"]
    assert {v["context"] for v in r["violations"]} == {expected}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT DISTINCT context FROM prefix_mutations").fetchall() == [(expected,)]
        assert conn.execute("SELECT alerted_at FROM prefix_sessions").fetchone()[0] is None


def test_store_tags_native_checkpoint_rewrite(db):
    before = {"instructions": "S", "input": [{"role": "user", "content": f"u{i}"} for i in range(6)]}
    _check(0, before)
    after = {"instructions": "S", "input": [{"type": "compaction", "encrypted_content": "ck1"},
                                            {"role": "user", "content": "u5"},
                                            {"role": "assistant", "content": "a"}]}
    r = _check(1, after)
    assert r["alert"] is False and {v["context"] for v in r["violations"]} == {"compaction:native"}
    # the checkpoint persists on later requests: no further diff, no tag needed
    later = copy.deepcopy(after)
    later["input"].append({"role": "user", "content": "u6"})
    assert _check(2, later)["violations"] == []


def test_store_allowlist_requires_reason_and_live_expiry(db):
    _check(0, _request(4))
    bad = _grow(_request(4))
    bad["system"][0]["text"] = "rotated soul"
    live = [{"segment": "system", "reason": "SOUL rollout 09-25", "expires": "2099-01-01"}]
    r = _check(1, bad, allowlist=live)
    assert r["alert"] is False and r["violations"][0]["allowlisted"] == 1
    assert r["violations"][0]["allowlist_reason"] == "SOUL rollout 09-25"
    # no expiry / expired / no reason -> ignored -> pages
    now = 2_000_000_000.0  # 2033: 2020 is expired, 2099 is live
    for entry in ([{"segment": "system", "reason": "forever"}],
                  [{"segment": "system", "reason": "old", "expires": "2020-01-01"}],
                  [{"segment": "system", "expires": "2099-01-01"}]):
        assert pg.allowlist_reason(entry, segment="system", session_key="sess-A", now=now) is None
    assert pg.allowlist_reason(live, segment="messages", session_key="sess-A", now=now) is None
    assert pg.allowlist_reason(live, segment="system", session_key="sess-A", now=now) == "SOUL rollout 09-25"


def test_store_uses_turn_boundary_mode_only_across_turns(db):
    _check(0, _request(4), turn="T1", prompt_tokens=235_000)
    tail = _request(4)
    tail["messages"][3]["content"][0]["text"] = "tool result, still streaming"
    # same turn: the last message may change
    assert _check(1, tail, turn="T1", prompt_tokens=235_100)["violations"] == []
    nxt = _grow(_request(4), 2)  # turn T2 re-renders msg[3] back to the original bytes
    r = _check(2, nxt, turn="T2", prompt_tokens=90_000)
    assert [(v["kind"], v["first_divergent_index"]) for v in r["violations"]] == [
        (pg.KIND_MUTATION, 3)]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT prompt_tokens_before, prompt_tokens_after FROM prefix_mutations"
        ).fetchall() == [(235_100, 90_000)]
        assert conn.execute("SELECT prompt_tokens FROM prefix_sessions").fetchone()[0] == 90_000


def test_store_fresh_baseline_per_turn_when_not_comparing_across_turns(db):
    """Background-review forks: each fork turn replays the parent snapshot, so
    fork N+1 must not be diffed against the end of fork N's tool loop."""
    _check(0, _grow(_request(6), 4), session="S:review", turn="F1", compare_across_turns=False)
    r = _check(0, _request(6), session="S:review", turn="F2", compare_across_turns=False)
    assert r["violations"] == [] and r["previous"] is None
    bad = _grow(_request(6))
    bad["messages"][0]["content"][0]["text"] = "X"
    r2 = _check(1, bad, session="S:review", turn="F2", compare_across_turns=False)
    assert [v["first_divergent_index"] for v in r2["violations"]] == [0]  # within a fork: still guarded


def test_prompt_token_columns_migrate_onto_existing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = store._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:  # pre-change shape of the two guard tables
        conn.execute("CREATE TABLE prefix_sessions (session_key TEXT PRIMARY KEY, turn_id TEXT,"
                     " seq INT, ts REAL, pid INT, api_mode TEXT, model TEXT, cache_read INT,"
                     " fingerprint_json TEXT, updated_at REAL, alerted_at REAL)")
        conn.execute("CREATE TABLE prefix_mutations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " ts REAL, session_key TEXT)")
    store._connect().close()
    with sqlite3.connect(path) as conn:
        assert "prompt_tokens" in {r[1] for r in conn.execute("PRAGMA table_info(prefix_sessions)")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(prefix_mutations)")}
        assert {"prompt_tokens_before", "prompt_tokens_after"} <= cols


# --- plugin entry + chokepoint wiring ------------------------------------------

def test_record_api_call_writes_ledger_then_guard_and_pages_via_notify(db, enabled, monkeypatch):
    sent: list[str] = []
    threads = []
    monkeypatch.setattr(pg, "send_alert", lambda body: sent.append(body) or True)
    real_dispatch = pg.dispatch_alert
    monkeypatch.setattr(pg, "dispatch_alert", lambda body, fn=None: threads.append(real_dispatch(body, fn)))

    def call(seq, req, cache_read):
        blackbox.record_api_call(
            turn_id="turn-x", seq=seq, ts=500.0 + seq, provider="claude-apr", model="claude-opus-4-8",
            usage=SimpleNamespace(input_tokens=10, output_tokens=1, cache_read_input_tokens=cache_read,
                                  cache_creation_input_tokens=0),
            api_mode="anthropic_messages", sub_key="sub-vps-3", attribution="wire", http_status=200,
            relay_synthetic=False, route_id=None, api_kwargs=req, session_key="sess-live",
        )

    call(0, _request(4), 199_607)
    bad = _grow(_request(4))
    bad["messages"][0]["content"][0]["text"] = "system-reminder injected into msg[0]"
    call(1, bad, 14_947)
    for t in threads:
        t.join(timeout=5)
    assert len(sent) == 1
    body = sent[0]
    assert "messages[0] (mutation)" in body and "199,607 → 14,947" in body
    assert "sess-live" in body and "claude-apr/claude-opus-4-8" in body and pg.CARD_REF in body
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_api_calls").fetchone()[0] == 2
        assert conn.execute("SELECT first_divergent_index, alerted FROM prefix_mutations").fetchall() == [(0, 1)]


def test_guard_disabled_by_config_records_nothing(db, enabled):
    enabled["prefix_guard"] = False
    for seq, req in enumerate((_request(2), _request(9))):
        blackbox.record_api_call(
            turn_id="t", seq=seq, ts=1.0 + seq, provider="p", model="m", usage=None,
            api_mode="anthropic_messages", sub_key=None, attribution="wire", http_status=200,
            relay_synthetic=False, route_id=None, api_kwargs=req, session_key="s",
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_api_calls").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM prefix_sessions").fetchone()[0] == 0


def test_guard_failure_never_costs_a_ledger_row(db, enabled, monkeypatch):
    def boom(**_):
        raise sqlite3.OperationalError("guard table locked")
    monkeypatch.setattr(store, "record_prefix_check", boom)
    blackbox.record_api_call(
        turn_id="t", seq=0, ts=1.0, provider="p", model="m", usage=None,
        api_mode="anthropic_messages", sub_key=None, attribution="wire", http_status=200,
        relay_synthetic=False, route_id=None, api_kwargs=_request(2), session_key="s",
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_api_calls").fetchone()[0] == 1


def test_chokepoint_passes_session_and_consumes_compaction_marker(monkeypatch):
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    agent = SimpleNamespace(_current_turn_id="turn-1", provider="claude-apr", model="m",
                            api_mode="anthropic_messages", session_id="20260925_050000_abcdef",
                            _blackbox_prefix_reset="compaction:threshold")
    req = _request(2)
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                               pool_headers={"x-pool-served-by": "sub-vps-3"})
    cch._record_successful_api_call(agent, response, req)
    cch._record_successful_api_call(agent, response, req)
    assert [(r["session_key"], r["prefix_reset"]) for r in rows] == [
        ("20260925_050000_abcdef", "compaction:threshold"),
        ("20260925_050000_abcdef", None),  # one-shot: tags exactly the first request after compaction
    ]
    assert rows[0]["api_kwargs"] is req
    assert agent._blackbox_prefix_reset is None
    # failures carry the request too (it was sent), so the fingerprint chain stays intact
    cch._record_failed_api_call(agent, RuntimeError("529"), req)
    assert rows[-1]["http_status"] is None and rows[-1]["api_kwargs"] is req


def test_chokepoint_keys_review_fork_on_its_own_chain(monkeypatch):
    """The review fork shares session_id with the parent; its requests must not
    become the main lane's comparison baseline."""
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                               pool_headers={})
    main = SimpleNamespace(_current_turn_id="S:S:aaaa", provider="p", model="m",
                           api_mode="anthropic_messages", session_id="S")
    fork = SimpleNamespace(_current_turn_id="S:uuid:bbbb", provider="p", model="m",
                           api_mode="anthropic_messages", session_id="S",
                           _memory_write_origin="background_review")
    cch._record_successful_api_call(main, response, _request(2))
    cch._record_successful_api_call(fork, response, _request(2))
    assert [(r["session_key"], r["prefix_compare_across_turns"]) for r in rows] == [
        ("S", True), ("S:review", False)]


def test_committed_compaction_leaves_one_shot_marker():
    from agent.conversation_compression import _record_blackbox_compaction

    agent = SimpleNamespace(_blackbox_compaction={"idle_compaction_fired": False})
    _record_blackbox_compaction(agent, trigger="idle_resume", before=2000, after=300,
                                telemetry=None, cost_sink=None)
    assert agent._blackbox_prefix_reset == "compaction:idle_resume"
    bare = SimpleNamespace()  # no blackbox state at all: marker still set, nothing raised
    _record_blackbox_compaction(bare, trigger=None, before=1, after=1, telemetry=None)
    assert bare._blackbox_prefix_reset == "compaction:unattributed"


def test_render_alert_shape():
    body = pg.render_alert(
        profile="apollo", provider="claude-apr", model="claude-opus-4-8", session_key="S",
        turn_id="T" * 40, seq=3,
        violations=[{"segment": "messages", "kind": "mutation", "first_divergent_index": 0,
                     "bytes_before": 4028, "bytes_after": 4990}],
        messages_before=40, messages_after=42, cache_read_before=199607, cache_read_after=14947,
        suppressed=2,
    )
    lines = body.splitlines()
    assert lines[0].startswith("🧬")
    assert "• Changed: messages[0] (mutation) 4,028 → 4,990 bytes" in lines
    assert "• History: 40 → 42 messages" in lines
    assert "• Cache read tokens: 199,607 → 14,947" in lines
    assert "• 2 more session(s) hit this since the last page" in lines
    assert ("T" * 32) in body and ("T" * 33) not in body
