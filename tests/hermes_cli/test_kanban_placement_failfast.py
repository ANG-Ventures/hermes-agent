"""Fleet Work Placement Phase 2: the fail-fast loop (card t_ecfd5591).

Spec: Obsidian ``AI/Infrastructure/Fleet Work Allocation -- Host Affinity and
Continuation (spec, 2026-10-11).md`` v2 s3 + s7 Phase 2; MOMUS pass 1 findings
F1 (re-route counted by its own ledger, not ``block_recurrences``), F5 (workers
honour ``KANBAN_PLACEMENT_CAPS``) and F14 (no gate at the skill preload).
Browser spec v1.2 s3.3/s3.10 RC-N6 (one list of six refusal tokens).

Hermetic: a temp board under ``tmp_path``, the Phase 1 fixture map + index,
no network, no real spawn. Every board transition goes through the real
``claim_task`` / ``block_on_rail_absent`` path; nothing seeds a counter.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_needs as kn
from hermes_cli import kanban_placement_failfast as pff
from hermes_cli import kanban_worker_exit as kwe
from hermes_cli import kanban_worker_hosts as kwh

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement"
ENFORCE_BROWSER = json.dumps({"*": "warn", "browser-session": "enforce", "screen": "enforce"})


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.delenv(khc.CAPS_ENV, raising=False)
    fleet = home / "fleet"
    fleet.mkdir()
    for f in (khc.MAP_FILE, khc.INDEX_FILE):
        shutil.copy(FIXTURES / f, fleet / f)
    return fleet


def _placed_env(monkeypatch, task_id, run_id, host="ace-ai", caps=ENFORCE_BROWSER, record=None):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    doc = {"host": host, "env": {"TERMINAL_ENV": "ssh"}, **(record or {})}
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps(doc))
    if caps is None:
        monkeypatch.delenv(khc.CAPS_ENV, raising=False)
    else:
        monkeypatch.setenv(khc.CAPS_ENV, caps)


def _claim(tid):
    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, tid) is not None
        return kb.latest_run(conn, tid).id


def _task(tid):
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid)


def _events(tid, kind):
    with kb.connect_closing() as conn:
        return [json.loads(r[0] or "{}") for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


RAIL = ("BROWSER_RAIL_ABSENT: no browser session on ace-ai. This card needs Ace's logged-in browser, "
        "which lives only on the Mac Studio. Add the line `host: studio` to the card body (or "
        "`requires: browser-session` once the affinity spec lands) and re-run; do not retry here.")


# -- F1: the re-route sequence through the real exit path -------------------------

def test_reroute_then_needs_input_never_triage(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cart", assignee="daedalus",
                             body="host: ace-ai\nrun scripts/amz_cart.py add B0X")
    run1 = _claim(tid)
    _placed_env(monkeypatch, tid, run1)
    assert kwe.block_on_rail_absent(RAIL) is True
    t = _task(tid)
    assert t.status == "ready"
    assert t.body.rstrip().endswith("requires: browser-session")
    ev = _events(tid, pff.REROUTE_EVENT)
    assert ev == [{"from": "ace-ai", "cap": "browser-session", "run_id": run1, "token": "BROWSER_RAIL_ABSENT"}]
    with kb.connect_closing() as conn:
        assert any(c.body.startswith("re-routed: browser-session, ran on ace-ai")
                   for c in kb.list_comments(conn, tid))
        assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run1,)).fetchone()[0] == "rerouted"
    # A second forced mismatch (the map was wrong): needs_input, never triage.
    run2 = _claim(tid)
    _placed_env(monkeypatch, tid, run2, host="ace-media")
    assert kwe.block_on_rail_absent(RAIL.replace("ace-ai", "ace-media")) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert len(_events(tid, pff.REROUTE_EVENT)) == 1
    with kb.connect_closing() as conn:
        reason = kb._latest_block_reason(conn, tid)
    assert reason.startswith("BROWSER_RAIL_ABSENT") and "Second placement miss" in reason
    assert "host=ace-ai" in reason


def test_second_miss_after_an_earlier_needs_input_block_still_needs_input(board, monkeypatch):
    # The F1 trap: a prior needs_input block makes block_recurrences 1, and a second
    # same-kind block_task would land in triage. The ledger path must not.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cart", assignee="daedalus", body="host: ace-ai\nbuy it")
    run1 = _claim(tid)
    with kb.connect_closing() as conn:
        assert kb.block_task(conn, tid, reason="need a decision", kind="needs_input", expected_run_id=run1)
        assert kb.unblock_task(conn, tid)
    run2 = _claim(tid)
    _placed_env(monkeypatch, tid, run2)
    assert kwe.block_on_rail_absent(RAIL) is True
    assert _task(tid).status == "ready"          # first placement miss: re-routed
    run3 = _claim(tid)
    _placed_env(monkeypatch, tid, run3)
    assert kwe.block_on_rail_absent(RAIL) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"


def test_unplaced_worker_parks_needs_input_verbatim(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cart", assignee="daedalus", body="x")
    run = _claim(tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.delenv(kwh.PLACEMENT_ENV, raising=False)
    assert kwe.block_on_rail_absent(RAIL) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == []
    with kb.connect_closing() as conn:
        assert kb._latest_block_reason(conn, tid) == RAIL


def test_stale_run_never_mutates_the_card(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cart", assignee="daedalus", body="x")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run + 999)
    assert kwe.block_on_rail_absent(RAIL) is False
    assert _task(tid).status == "running"


def test_parse_absent_reads_both_spellings():
    assert pff.parse_absent(RAIL) == ("BROWSER_RAIL_ABSENT", "browser-session", "ace-ai")
    line = pff.absent_line("imessage", "ci-box")
    assert pff.parse_absent(line) == ("IMESSAGE_ABSENT", "imessage", "ci-box")
    assert pff.ABSENT_RE.match(line)
    assert pff.parse_absent("some other error") is None


# -- Layer 0: boot check with the route-hash re-derive (F1) ----------------------

def test_boot_check_trusts_a_fresh_route_and_rederives_a_stale_one(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain coding card")
    task = _task(tid)
    record = pff.placement_record(task, board)
    assert record[pff.REQUIRES_KEY] == []
    _placed_env(monkeypatch, tid, 1, record=record)
    assert pff.boot_check(tid) is None
    # The body is edited between route and spawn: the hash no longer matches, the
    # worker re-derives from the CURRENT body instead of trusting the env.
    with kb.connect_closing() as conn:
        kb.edit_task(conn, tid, body="plain coding card\nrequires: browser-session")
    line = pff.boot_check(tid)
    assert line is not None and line.startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")


def test_boot_check_reads_attached_skills(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x", skills=["computer-use"])
    _placed_env(monkeypatch, tid, 1)
    assert pff.boot_check(tid).startswith("SCREEN_RAIL_ABSENT cap=screen host=ace-ai")


def test_boot_check_is_inert_unplaced_or_off_or_warn(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "off"}))
    assert pff.boot_check(tid) is None
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    assert pff.boot_check(tid) is None
    _placed_env(monkeypatch, tid, 1, caps=None)
    assert pff.boot_check(tid) is None
    _placed_env(monkeypatch, tid, 1, host="studio-local")
    assert pff.boot_check(tid) is None
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    assert pff.boot_check(tid) is None


# -- Layer 0b: dispatch-time attached-skill gate (F14) ------------------------------

def test_attached_gated_skill_is_never_spawned_on_a_host_without_it(board):
    ctx = khc.tick(board, {"placement": {"capabilities": "enforce"}}, ("ace-ai", "ace-media"))
    assert pff.attached_skill_gap(["amazon-cart-ops-local"], "ace-ai", ctx) == "browser-session"
    assert pff.attached_skill_gap(["amazon-cart-ops-local"], "studio", ctx) is None
    assert pff.attached_skill_gap(["some-coding-skill"], "ace-ai", ctx) is None
    warn = khc.tick(board, {"placement": {"capabilities": "warn"}}, ("ace-ai",))
    assert pff.attached_skill_gap(["amazon-cart-ops-local"], "ace-ai", warn) is None
    assert pff.attached_skill_gap(["amazon-cart-ops-local"], "ace-ai", None) is None


# -- Layer 1: mid-run skill_view gate; never at the preload (F14) -------------------

def _skills_home(monkeypatch, tmp_path):
    sk = tmp_path / ".hermes" / "skills" / "personal" / "amazon-cart-ops-local"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: amazon-cart-ops-local\ndescription: cart\n---\nDrive the cart.\n",
                                 encoding="utf-8")


def test_midrun_skill_view_returns_the_token_and_records_it(board, monkeypatch, tmp_path):
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    out = json.loads(registry.dispatch("skill_view", {"name": "amazon-cart-ops-local"}, task_id="t"))
    assert out["error"].startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")
    assert rp.take_rail_absent() == out["error"]


def test_preload_is_never_gated(board, monkeypatch, tmp_path):
    # F14: the preload calls skill_view() directly. A gate there would make the skill
    # "missing" and cli.py would continue WITHOUT it. The preload must still load it;
    # Layer 0 is the preload's guard.
    _skills_home(monkeypatch, tmp_path)
    from agent.skill_commands import build_preloaded_skills_prompt
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    prompt, loaded, missing = build_preloaded_skills_prompt(["amazon-cart-ops-local"])
    assert loaded == ["amazon-cart-ops-local"] and missing == []
    assert "Drive the cart." in prompt


def test_skill_gate_is_inert_for_unplaced_or_warn(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    assert pff.skill_gate("amazon-cart-ops-local") is None
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    monkeypatch.setenv(khc.CAPS_ENV, ENFORCE_BROWSER)
    assert pff.skill_gate("amazon-cart-ops-local") is None


# -- F5: rail gates honour KANBAN_PLACEMENT_CAPS ------------------------------------

def test_rail_gate_off_or_warn_refuses_the_call_but_never_parks(board, monkeypatch):
    from tools import rail_preflight as rp
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    msg = rp.browser_rail_error("http://127.0.0.1:9222")
    assert msg and msg.startswith("BROWSER_RAIL_ABSENT")
    assert rp.take_rail_absent() is None
    assert _events(tid, pff.GATE_WARN_EVENT)[0]["cap"] == "browser-session"
    _placed_env(monkeypatch, tid, 1, caps=ENFORCE_BROWSER)
    rp.browser_rail_error("http://127.0.0.1:9222")
    assert rp.take_rail_absent() == msg


# -- RC-N6: the needs shape and the one token list -----------------------------------

def test_needs_record_is_the_generic_shape():
    rec = kn.needs_of(["browser-session"], "needs: browser:amazon-alex, imessage-otp\n")
    assert rec == [{"kind": "host-cap", "name": "browser-session"},
                   {"kind": "resource", "name": "browser:amazon-alex"}]
    assert kn.validate(rec) == []
    assert kn.validate([{"kind": "resource", "name": "amazon"}])
    assert kn.validate([{"kind": "host", "name": "x"}])


def test_six_refusals_each_with_an_exit_and_none_reroutes():
    assert set(kn.REFUSALS) == {
        "BROWSER_IDENTITY_UNPROVISIONED", "BROWSER_LEASE_AMBIGUOUS", "BROWSER_LEASE_WAIT",
        "BROWSER_CLASS_REFUSED", "BROWSER_EGRESS_UNVERIFIED", "BROWSER_LEDGER_UNREACHABLE"}
    for token, exit_ in kn.REFUSALS.items():
        r = kn.refusal(token, "detail")
        assert r.line().split(":", 1)[0] == token and r.rerouting is False
        assert exit_ == (kn.READY_UNTIL_TIMEOUT if token == "BROWSER_LEASE_WAIT" else kn.NEEDS_INPUT)
    with pytest.raises(ValueError):
        kn.refusal("BROWSER_RAIL_ABSENT")


def test_lane_of_and_migrated_lanes_empty_is_legacy(tmp_path):
    (tmp_path / kn.REGISTRY_FILE).write_text(json.dumps(
        {"migrated_lanes": [], "legacy_allowlist": ["amazon-cart-ops-local", "browser-soak-amazon"]}))
    reg = kn.load_registry(tmp_path)
    assert kn.lane_of(["browser-access", "amazon-cart-ops-local"], "", reg) == "amazon-cart-ops-local"
    assert kn.lane_of(["browser-access"], "origin: browser-soak-amazon · x", reg) == "browser-soak-amazon"
    assert kn.lane_of(["browser-access"], "", reg) == kn.LANE_NONE
    assert kn.resolve("browser:amazon-alex", "amazon-cart-ops-local", reg) == {
        "need": "browser:amazon-alex", "lane": "amazon-cart-ops-local", "reason": "lane_not_migrated"}
    assert kn.load_registry(tmp_path / "absent") == kn.LaneRegistry()
