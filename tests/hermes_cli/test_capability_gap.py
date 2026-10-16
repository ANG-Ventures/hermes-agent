"""worker_capability_gap: one write-only emitter behind four hooks (Fleet Host Lanes spec v0.2 §4.3, t_b1100bf0).

Hermetic: a temp board DB, a temp fleet root holding a stand-in ``scripts/fleet_host_onboard.py`` (the real one
lives in hermes-home; only resolve_host + TOOL_REQUIREMENTS are read), no network, no notify.
"""
from __future__ import annotations

import ast
import json
import os
import socket
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_capability_gap as gap
from tools import capability_gap_hooks as hooks
from tools import rail_preflight as rp
from hermes_cli import kanban_worker_hosts as kwh

REPO = Path(__file__).resolve().parents[2]

FHO_STUB = '''
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class Col:  # the real module defines a dataclass at import: the loader must survive that
    id: str
    fix: Optional[str] = None


HOSTS = {
    "studio": {"aliases": ["mac-studio-m3u", "studio"]},
    "ci-box": {"aliases": ["ci", "ci-box"]},
    "ace-ai": {"aliases": ["ace-ai", "ace-ai-linux"]},
    "ace-media": {"aliases": ["ace-media", "ace-media-linux"]},
}
TOOL_REQUIREMENTS = {"computer_use": "K21", "browser_*": "K23", "op-cached-read": "C5", "test-gate": "K13"}
def resolve_host(raw):
    if not raw:
        return None
    s = str(raw).strip().lower().split(".", 1)[0]
    return next((h for h, spec in HOSTS.items() if s == h or s in spec["aliases"]), None)
'''


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "fleet_host_onboard.py").write_text(FHO_STUB)
    monkeypatch.setattr(gap, "fleet_root", lambda: root)
    gap._fho_cache.clear()
    gap._fho_failed.clear()
    hooks._emitted.clear()
    hooks._refused_logged.clear()
    rp._emitted.clear()
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.delenv(kwh.PLACEMENT_ENV, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    yield root
    gap._fho_cache.clear()
    hooks._emitted.clear()


def _card(monkeypatch, *, owned=True):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gap probe", assignee="daedalus")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    if owned:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return tid, run.id


def _events(tid):
    conn = kb.connect()
    try:
        rows = conn.execute("SELECT payload, run_id FROM task_events WHERE task_id=? AND kind=?",
                            (tid, gap.EVENT_KIND)).fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) | {"_run_id": r[1]} for r in rows]


def _ledger(root):
    p = root / "state" / "fleet-host-gap" / "gaps.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


SCREEN = ("SCREEN_RAIL_ABSENT: no screen session on ace-ai. This card needs Ace's logged-in screen session, "
          "which lives only on the Mac Studio.")
BROWSER = ("BROWSER_RAIL_ABSENT: no browser session on ace-ai. This card needs Ace's logged-in browser, "
           "which lives only on the Mac Studio.")


# ── Hook A: rails ────────────────────────────────────────────────────────────────────────────────
def test_hook_a_rail_absent_one_event_after_block(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    tid, run_id = _card(monkeypatch)
    assert kwe.block_on_rail_absent(BROWSER) is True
    assert kwe.block_on_rail_absent(BROWSER) is False  # card already blocked: no second park
    hooks.on_rail_absent(BROWSER)                      # a second hook call in the same run is deduped
    ev = _events(tid)
    assert len(ev) == 1
    e = ev[0]
    assert (e["gap_class"], e["requirement_id"], e["host"], e["host_raw"], e["tool"]) == (
        "rail_absent", "K23", "ace-ai", "ace-ai", "browser_*")
    assert e["_run_id"] == run_id
    assert e["profile"] == "daedalus"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_hook_a_screen_rail_maps_to_computer_use_column(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    assert kwe.block_on_rail_absent(SCREEN) is True
    (e,) = _events(tid)
    assert (e["gap_class"], e["requirement_id"], e["tool"]) == ("rail_absent", "K21", "computer_use")


def test_hook_a_no_event_when_block_did_not_happen(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    monkeypatch.setattr(kwe, "_park_needs_input", lambda reason: False)
    assert kwe.block_on_rail_absent(BROWSER) is False
    assert _events(tid) == []


# ── Hook A, both rail spellings through the one seam (t_ecfd5591 review 77f581ae3801) ───────────────
def _placed(monkeypatch, host="ace-ai"):
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": host, "env": {}}))
    # Hook A is under test, not custody: the host proved the run's workspace empty (no ssh).
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: False)


@pytest.mark.parametrize("cap,tool,req", [("browser-session", "browser_*", "K23"), ("screen", "computer_use", "K21")])
def test_hook_a_new_gate_spelling_emits_once_after_the_board_decision(fleet, monkeypatch, cap, tool, req):
    from hermes_cli import kanban_placement_failfast as pff
    from hermes_cli import kanban_worker_exit as kwe
    tid, run_id = _card(monkeypatch)
    _placed(monkeypatch)
    line = pff.absent_line(cap, "ace-ai")
    assert kwe.block_on_rail_absent(line) is True          # placed: re-route, committed
    hooks.on_rail_absent(line)                              # same run again: deduped
    (e,) = _events(tid)
    assert (e["gap_class"], e["tool"], e["requirement_id"], e["host_raw"], e["_run_id"]) == (
        "rail_absent", tool, req, "ace-ai", run_id)
    assert len([r for r in _ledger(fleet) if r["task_id"] == tid]) == 1


def test_hook_a_old_spelling_still_emits(fleet, monkeypatch):
    tid, _ = _card(monkeypatch)
    from hermes_cli import kanban_worker_exit as kwe
    assert kwe.block_on_rail_absent(BROWSER) is True
    assert [e["tool"] for e in _events(tid)] == ["browser_*"]


def test_hook_a_non_rail_absent_line_emits_nothing(fleet, monkeypatch):
    from hermes_cli import kanban_placement_failfast as pff
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    _placed(monkeypatch)
    assert kwe.block_on_rail_absent(pff.absent_line("imessage", "ace-ai")) is True
    tid2, _ = _card(monkeypatch)
    _placed(monkeypatch)
    assert kwe.block_on_rail_absent(pff.indeterminate_line("ace-ai")) is True
    assert _events(tid) == [] and _events(tid2) == []


def test_hook_a_stale_owner_and_unowned_emit_nothing(fleet, monkeypatch):
    from hermes_cli import kanban_placement_failfast as pff
    from hermes_cli import kanban_worker_exit as kwe
    tid, run_id = _card(monkeypatch)
    _placed(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 7))          # a stale run
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is False
    tid2, _ = _card(monkeypatch, owned=False)                             # no owned task
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is False
    assert _events(tid) == [] and _events(tid2) == []


def test_hook_a_ledger_failure_keeps_the_committed_decision(fleet, monkeypatch):
    from hermes_cli import kanban_placement_failfast as pff
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    _placed(monkeypatch)
    monkeypatch.setattr(gap, "_append_ledger", lambda line: (_ for _ in ()).throw(OSError("disk full")))
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    assert len(_events(tid)) == 1 and _ledger(fleet) == []
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


# ── Hook B: toolset scope (classification is the key, never requirement_id null-ness) ─────────────
def test_hook_b_out_of_scope_is_profile_toolset_with_null_requirement(fleet, monkeypatch):
    tid, _ = _card(monkeypatch)
    msg = "'computer_use' is not available in this session. Use tool_search to find tools you can call."
    assert hooks.on_scope_block("computer_use", msg, enabled_toolsets=["terminal", "file"]) is True
    (e,) = _events(tid)
    assert e["gap_class"] == "profile_toolset"
    assert e["requirement_id"] is None
    assert e["tool"] == "computer_use"


def test_hook_b_check_fn_filtered_is_check_fn_unavailable_on_dispatcher_host(fleet, monkeypatch):
    from tools.registry import registry
    import tools.registry as reg
    tid, _ = _card(monkeypatch)
    entry = registry.get_entry("computer_use")
    assert entry is not None and entry.check_fn is not None
    monkeypatch.setattr(reg, "_check_fn_cached", lambda fn: False if fn is entry.check_fn else True)
    monkeypatch.setattr(socket, "gethostname", lambda: "mac-studio-m3u")
    msg = "'computer_use' is not available in this session. Use tool_search to find tools you can call."
    assert hooks.on_scope_block("computer_use", msg, enabled_toolsets=["computer_use", "terminal"]) is True
    (e,) = _events(tid)
    assert (e["gap_class"], e["requirement_id"], e["host_raw"], e["host"]) == (
        "check_fn_unavailable", "K21", "mac-studio-m3u", "studio")


def test_profile_toolset_with_requirement_id_is_refused(fleet, monkeypatch):
    tid, run_id = _card(monkeypatch)
    conn = kb.connect()
    try:
        with pytest.raises(ValueError):
            gap.record_capability_gap(conn, tid, run_id=run_id, host_raw="ace-ai", profile="daedalus",
                                      gap_class="profile_toolset", requirement_id="K21", tool="computer_use",
                                      message="x")
        with pytest.raises(ValueError):
            gap.record_capability_gap(conn, tid, run_id=run_id, host_raw="ace-ai", profile="daedalus",
                                      gap_class="nope", requirement_id=None, tool="x", message="x")
    finally:
        conn.close()
    assert _events(tid) == [] and _ledger(fleet) == []
    # through a hook: dropped, no exception, no row
    assert hooks._emit(gap_class="profile_toolset", tool="computer_use", requirement_id="K21",
                       host_raw="ace-ai", message="x", hook="test") is False
    assert _events(tid) == []


def test_drill_shape_through_the_real_scope_branch(fleet, monkeypatch):
    """t_3aa5b020's shape: a worker whose toolsets lack computer_use calls it through tool_call."""
    from types import SimpleNamespace
    from agent.tool_executor import _unwrap_tool_search_call
    tid, _ = _card(monkeypatch)
    agent = SimpleNamespace(enabled_toolsets=["terminal", "file", "todo"], disabled_toolsets=None)
    _, _, block = _unwrap_tool_search_call(agent, "tool_call",
                                           {"name": "computer_use", "arguments": {"action": "capture"}})
    assert block == "'computer_use' is not available in this session. Use tool_search to find tools you can call."
    (e,) = _events(tid)
    assert (e["gap_class"], e["requirement_id"]) == ("profile_toolset", None)
    assert len(_ledger(fleet)) == 1
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"  # the card is NOT parked by Hook B
    finally:
        conn.close()


def test_drill_shape_through_the_bridge_dispatch(fleet, monkeypatch):
    import model_tools
    tid, _ = _card(monkeypatch)
    out = model_tools._dispatch_bridge_tool("tool_call", {"name": "computer_use", "arguments": {"action": "capture"}},
                                            ["terminal", "file", "todo"], None)
    assert "is not available in this session" in out[0]
    (e,) = _events(tid)
    assert e["gap_class"] == "profile_toolset"


# ── Hook C: terminal `command not found` ─────────────────────────────────────────────────────────
def test_hook_c_required_command_missing(fleet, monkeypatch):
    from tools.terminal_tool_result import _failure_hint
    tid, _ = _card(monkeypatch)
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "ci-box", "env": {"TERMINAL_ENV": "ssh"}}))
    hint = _failure_hint("op-cached-read x", 127, "bash: op-cached-read: command not found\n", None)
    assert "op-cached-read" in hint  # the model still gets its hint
    (e,) = _events(tid)
    assert (e["gap_class"], e["requirement_id"], e["host_raw"], e["host"], e["placed"]) == (
        "cmd_not_found", "C5", "ci-box", "ci-box", True)


def test_hook_c_unlisted_command_is_ignored(fleet, monkeypatch):
    from tools.terminal_tool_result import _failure_hint
    tid, _ = _card(monkeypatch)
    _failure_hint("foo", 127, "bash: foo: command not found\n", None)
    assert _events(tid) == []


# ── Hook D: tool_describe not_found (advisory) ───────────────────────────────────────────────────
def test_hook_d_describe_not_found_is_one_advisory_event(fleet, monkeypatch):
    from tools import tool_search as ts
    tid, _ = _card(monkeypatch)
    out = json.loads(ts.dispatch_tool_describe({"names": ["computer_use", "zz_unknown_tool"]}, current_tool_defs=[]))
    assert "computer_use" in out["not_found"]
    (e,) = _events(tid)
    assert (e["gap_class"], e["requirement_id"], e["advisory"], e["tool"]) == (
        "profile_toolset", None, True, "computer_use")


# ── ownership: no owned task -> zero on every hook ───────────────────────────────────────────────
def test_no_owned_task_emits_nothing_on_every_hook(fleet, monkeypatch):
    from tools import tool_search as ts
    from tools.terminal_tool_result import _failure_hint
    tid, _ = _card(monkeypatch, owned=False)
    assert hooks.on_rail_absent(BROWSER) is False
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False
    _failure_hint("op-cached-read", 127, "bash: op-cached-read: command not found", None)
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])
    assert _events(tid) == [] and _ledger(fleet) == []


def test_delegate_child_emits_nothing(fleet, monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    tid, _ = _card(monkeypatch)
    with non_dispatcher_owned_context():
        assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False
    assert _events(tid) == []


# ── I6: never break the board write, never stall ─────────────────────────────────────────────────
def test_unwritable_ledger_keeps_block_and_row(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    (fleet / "state").mkdir()
    (fleet / "state" / "fleet-host-gap").write_text("a FILE where the ledger dir should be")
    assert kwe.block_on_rail_absent(BROWSER) is True
    assert len(_events(tid)) == 1


def test_emitter_is_fast_and_does_no_io_beyond_db_and_ledger(fleet, monkeypatch):
    import subprocess
    tid, run_id = _card(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    gap.fleet_host_onboard()  # module load is a one-time cost, not the emitter's
    conn = kb.connect()
    try:
        t0 = time.perf_counter()
        gap.record_capability_gap(conn, tid, run_id=run_id, host_raw="ace-ai", profile="daedalus",
                                  gap_class="cmd_not_found", requirement_id="C5", tool="op-cached-read", message="m")
        assert time.perf_counter() - t0 < 0.05
    finally:
        conn.close()


# ── AC10: every event row has exactly one ledger line ────────────────────────────────────────────
def test_every_event_row_has_exactly_one_ledger_line(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    from tools.terminal_tool_result import _failure_hint
    tid, run_id = _card(monkeypatch)
    hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"])
    _failure_hint("op-cached-read", 127, "op-cached-read: command not found", None)
    kwe.block_on_rail_absent(BROWSER)
    rows = sorted((e["gap_class"], e["requirement_id"], e["tool"], e["_run_id"]) for e in _events(tid))
    lines = sorted((l["gap_class"], l["requirement_id"], l["tool"], l["run_id"]) for l in _ledger(fleet)
                   if l["task_id"] == tid)
    assert len(rows) == 3 and rows == lines


def test_unresolvable_host_is_kept_with_host_none(fleet, monkeypatch):
    tid, run_id = _card(monkeypatch)
    conn = kb.connect()
    try:
        gap.record_capability_gap(conn, tid, run_id=run_id, host_raw="zzz", profile="daedalus",
                                  gap_class="cmd_not_found", requirement_id="C5", tool="op-cached-read", message="m")
    finally:
        conn.close()
    (e,) = _events(tid)
    assert e["host"] is None and e["host_raw"] == "zzz"


# ── Prism 5090d1ec75eb: a failed emission releases its dedup slot ──────────────────────────────────
def test_transient_db_failure_does_not_burn_the_dedup_slot(fleet, monkeypatch):
    tid, _ = _card(monkeypatch)
    real_connect = kb.connect
    calls = {"n": 0}

    def flaky_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database is locked (transient)")
        return real_connect(*a, **k)

    monkeypatch.setattr(kb, "connect", flaky_connect)
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is True
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False  # now deduped
    assert len(_events(tid)) == 1 and len(_ledger(fleet)) == 1


def test_event_row_write_failure_does_not_burn_the_dedup_slot(fleet, monkeypatch):
    tid, _ = _card(monkeypatch)
    real = kb._append_event
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk I/O error")
        return real(*a, **k)

    monkeypatch.setattr(kb, "_append_event", flaky)
    from tools.terminal_tool_result import _failure_hint
    _failure_hint("op-cached-read", 127, "op-cached-read: command not found", None)
    assert _events(tid) == []
    _failure_hint("op-cached-read", 127, "op-cached-read: command not found", None)
    assert len(_events(tid)) == 1


# ── Prism 858bd830ca46: an actionable occurrence supersedes an advisory one ─────────────────────────
def test_actual_scope_block_after_advisory_describe_is_recorded(fleet, monkeypatch):
    from tools import tool_search as ts
    tid, _ = _card(monkeypatch)
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is True
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False
    ev = sorted(_events(tid), key=lambda e: e["advisory"])
    assert [e["advisory"] for e in ev] == [False, True]
    assert ev[0]["hook"] == "scope"


def test_advisory_after_actual_is_still_deduped(fleet, monkeypatch):
    from tools import tool_search as ts
    tid, _ = _card(monkeypatch)
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is True
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])
    (e,) = _events(tid)
    assert e["advisory"] is False


def test_failed_supersede_keeps_the_advisory_slot(fleet, monkeypatch):
    from tools import tool_search as ts
    tid, run_id = _card(monkeypatch)
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])
    monkeypatch.setattr(kb, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("transient")))
    assert hooks.on_scope_block("computer_use", "x", enabled_toolsets=["terminal"]) is False
    assert hooks._emitted[(tid, run_id, "computer_use")] == hooks._ADVISORY
    ts.dispatch_tool_describe({"names": ["computer_use"]}, current_tool_defs=[])  # still deduped: no write tried


# ── Prism 7e51fe22e18a: the ledger gets whole lines or nothing ──────────────────────────────────────
_REAL_WRITE = os.write


def _short_writes(monkeypatch, chunk=7):
    real = _REAL_WRITE

    def short(fd, data):
        return real(fd, bytes(data[:chunk]))

    monkeypatch.setattr(gap.os, "write", short)


def test_short_write_is_completed_not_left_as_a_fragment(fleet, monkeypatch):
    tid, run_id = _card(monkeypatch)
    _short_writes(monkeypatch)
    for i in range(3):
        gap._append_ledger({"task_id": tid, "i": i})
    monkeypatch.setattr(gap.os, "write", _REAL_WRITE)
    raw = (fleet / "state" / "fleet-host-gap" / "gaps.jsonl").read_text()
    assert raw.endswith("\n")
    assert [json.loads(l)["i"] for l in raw.splitlines()] == [0, 1, 2]


def test_failed_append_rolls_back_its_fragment(fleet, monkeypatch):
    gap._append_ledger({"i": 0})
    real = os.write
    state = {"n": 0}

    def half_then_enospc(fd, data):
        state["n"] += 1
        if state["n"] == 1:
            return real(fd, bytes(data[:5]))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gap.os, "write", half_then_enospc)
    with pytest.raises(OSError):
        gap._append_ledger({"i": 1})
    monkeypatch.setattr(gap.os, "write", real)
    gap._append_ledger({"i": 2})
    lines = (fleet / "state" / "fleet-host-gap" / "gaps.jsonl").read_text().splitlines()
    assert [json.loads(l)["i"] for l in lines] == [0, 2]


@pytest.mark.platforms("posix")
def test_concurrent_short_writers_never_interleave(fleet, monkeypatch):
    import threading
    _short_writes(monkeypatch, chunk=3)
    threads = [threading.Thread(target=lambda n=n: [gap._append_ledger({"w": n, "j": j, "pad": "x" * 40})
                                                    for j in range(10)]) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.setattr(gap.os, "write", _REAL_WRITE)
    lines = (fleet / "state" / "fleet-host-gap" / "gaps.jsonl").read_text().splitlines()
    assert sorted((d["w"], d["j"]) for d in map(json.loads, lines)) == [(n, j) for n in range(6) for j in range(10)]


# ── Prism 6f40dacff8d0: a hook failure never masks a committed park ─────────────────────────────────
def test_hook_a_failure_still_reports_the_park(fleet, monkeypatch):
    from hermes_cli import kanban_worker_exit as kwe
    tid, _ = _card(monkeypatch)
    monkeypatch.setattr(hooks, "on_rail_absent", lambda m: (_ for _ in ()).throw(RuntimeError("bad table")))
    assert kwe.block_on_rail_absent(BROWSER) is True
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


# ── Prism d4e5f47a3c6a / 09008d9e183c: a NAMED scope reason is recorded too ─────────────────────────
_NAMED = "'computer_use' needs a desktop-app session with a GUI surface."


def test_named_scope_reason_is_recorded_through_tool_executor(fleet, monkeypatch):
    from types import SimpleNamespace
    from tools import tool_search as ts
    from agent.tool_executor import _unwrap_tool_search_call
    tid, _ = _card(monkeypatch)
    monkeypatch.setattr(ts, "out_of_scope_reason", lambda name: _NAMED)
    agent = SimpleNamespace(enabled_toolsets=["terminal", "file", "todo"], disabled_toolsets=None)
    _, _, block = _unwrap_tool_search_call(agent, "tool_call",
                                           {"name": "computer_use", "arguments": {"action": "capture"}})
    assert block == _NAMED
    (e,) = _events(tid)
    assert (e["gap_class"], e["message"]) == ("profile_toolset", _NAMED)


def test_named_scope_reason_is_recorded_through_bridge_dispatch(fleet, monkeypatch):
    import model_tools
    from tools import tool_search as ts
    tid, _ = _card(monkeypatch)
    monkeypatch.setattr(ts, "out_of_scope_reason", lambda name: _NAMED)
    out = model_tools._dispatch_bridge_tool("tool_call", {"name": "computer_use", "arguments": {"action": "capture"}},
                                            ["terminal", "file", "todo"], None)
    assert _NAMED in out[0]
    (e,) = _events(tid)
    assert e["message"] == _NAMED


# ── Prism 7012c86f40c7: the terminal buffer never leaves the transcript ─────────────────────────────
def test_hook_c_persists_only_the_command_name(fleet, monkeypatch):
    from tools.terminal_tool_result import _failure_hint
    tid, _ = _card(monkeypatch)
    secret = "API_TOKEN=sk-live-DO-NOT-PERSIST-0123456789"
    out = f"{secret}\n" + "noise\n" * 200 + "bash: op-cached-read: command not found\n"
    _failure_hint("op-cached-read x", 127, out, None)
    (e,) = _events(tid)
    assert e["message"] == "op-cached-read: command not found"
    raw = (fleet / "state" / "fleet-host-gap" / "gaps.jsonl").read_text()
    assert "DO-NOT-PERSIST" not in raw and "noise" not in raw


# ── Prism 3a92b672df75: a failed onboard-module load is retried, not cached for life ────────────────
def test_failed_onboard_load_is_retried(fleet, monkeypatch):
    script = fleet / "scripts" / "fleet_host_onboard.py"
    script.write_text("raise RuntimeError('half-written file')\n")
    assert gap.requirement_for("op-cached-read") is None
    script.write_text(FHO_STUB)
    assert gap.requirement_for("op-cached-read") is None          # inside the retry window: no hammering
    monkeypatch.setattr(gap, "_FHO_RETRY_S", 0.0)
    assert gap.requirement_for("op-cached-read") == "C5"          # recovered without a process restart
    assert gap.resolve_host("mac-studio-m3u") == "studio"


# ── I3: one emitter, four hook sites ─────────────────────────────────────────────────────────────
_ALLOWED_EMITTER_CALLERS = {"tools/capability_gap_hooks.py"}
_ALLOWED_HOOK_CALLERS = {
    "on_rail_absent": {"hermes_cli/kanban_worker_exit.py"},
    "on_scope_block": {"agent/tool_executor.py", "model_tools.py"},
    "on_command_not_found": {"tools/terminal_tool_result.py"},
    "on_describe_not_found": {"tools/tool_search.py"},
}


def _production_py():
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/", "node_modules/", "website/")) or "/node_modules/" in rel:
            continue
        yield rel, p


def test_one_emitter_and_four_hook_sites():
    """AST contract (I3): nothing but the hooks module calls the emitter or names the event kind / ledger,
    and each hook is called only from its declared site."""
    emitter_calls, hook_calls, kind_literals = {}, {}, set()
    for rel, p in _production_py():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name == "record_capability_gap":
                    emitter_calls.setdefault(rel, 0)
                    emitter_calls[rel] += 1
                if name in _ALLOWED_HOOK_CALLERS:
                    hook_calls.setdefault(name, set()).add(rel)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                    node.value == gap.EVENT_KIND or node.value == "gaps.jsonl"):
                kind_literals.add(rel)
    assert set(emitter_calls) == _ALLOWED_EMITTER_CALLERS
    assert hook_calls == _ALLOWED_HOOK_CALLERS
    assert kind_literals == {"hermes_cli/kanban_db_capability_gap.py"}
