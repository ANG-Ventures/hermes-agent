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
