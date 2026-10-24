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
import os
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
    # Hermetic: no ssh. A mid-run re-route test models a host that proved the run's workspace
    # empty; the custody tests below replace this with the real probe and a fake runner.
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: False)
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
    assert ev[0].pop("after_comment_id") == 0          # continuation causality snapshot (360354deb8ab)
    assert ev == [{"from": "ace-ai", "cap": "browser-session", "run_id": run1, "token": "BROWSER_RAIL_ABSENT",
                   "to": "ready"}]
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


def test_undeclared_browser_navigate_on_ci_box_returns_the_token_and_records_it(board, monkeypatch):
    # Layer 1b, D1b-undeclared (t_f813a42f): no requires: line and no skill on the card, so
    # boot, dispatch and skill_view all pass; the browser tool itself must refuse up front.
    from tools import rail_preflight as rp
    from tools.registry import registry
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ci-box\nopen example.com")
    _placed_env(monkeypatch, tid, 1, host="ci-box")
    assert pff.boot_check(tid) is None                       # the declared-need gates see nothing
    out = json.loads(registry.dispatch("browser_navigate", {"url": "https://example.com"}, task_id="t"))
    assert out["success"] is False
    assert out["error"].startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ci-box")
    assert rp.take_rail_absent() == out["error"]
    assert pff.parse_absent(out["error"])[:2] == ("BROWSER_RAIL_ABSENT", "browser-session")


def test_capability_gate_is_inert_on_a_capable_host_unplaced_or_warn(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1, host="studio")
    assert pff.capability_gate("browser-session") is None
    _placed_env(monkeypatch, tid, 1, host="ci-box", caps=json.dumps({"*": "warn"}))
    assert pff.capability_gate("browser-session") is None
    _placed_env(monkeypatch, tid, 1, host="studio-local")
    assert pff.capability_gate("browser-session") is None
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    monkeypatch.setenv(khc.CAPS_ENV, ENFORCE_BROWSER)
    assert pff.capability_gate("browser-session") is None
    _placed_env(monkeypatch, tid, 1, host="ci-box")
    monkeypatch.delenv("HERMES_KANBAN_TASK")                # not the owning worker: never gated
    assert pff.capability_gate("browser-session") is None


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


# -- Prism round 1 on #1963 ------------------------------------------------------------

@pytest.mark.parametrize("cap", ["gpu-v2", "screen", "browser-session", "x264-box", "4k-display"])
def test_token_round_trips_for_every_capability_spelling(cap):
    line = pff.absent_line(cap, "ace-ai")
    assert pff.ABSENT_RE.match(line), line
    assert pff.parse_absent(line)[1:] == (cap, "ace-ai")


def test_reroute_of_a_reviewer_run_returns_to_review(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cart", assignee="daedalus", body="x")
    run1 = _claim(tid)
    with kb.connect_closing() as conn:
        assert kb.request_review(conn, tid, summary="impl done", expected_run_id=run1)
        assert kb.claim_review_task(conn, tid) is not None
        run2 = kb.latest_run(conn, tid).id
    _placed_env(monkeypatch, tid, run2)
    assert kwe.block_on_rail_absent(RAIL) is True
    assert _task(tid).status == "review"
    assert _events(tid, pff.REROUTE_EVENT)[0]["to"] == "review"


def test_stamp_is_dropped_when_the_files_change_during_derivation(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    real = pff.hard_requirements

    def racing(cmap, body, skills):
        out = real(cmap, body, skills)
        idx = board / khc.INDEX_FILE
        idx.write_bytes(idx.read_bytes() + b"\n")          # the index changes mid-derivation
        return out
    monkeypatch.setattr(pff, "hard_requirements", racing)
    task = _task(tid)
    snap = pff._fleet_bytes(board)
    rec = pff.placement_record(task, board)
    # The stamp describes the bytes it derived from, never the files written meanwhile
    # (23e10f0f6800): its hash is of the captured pair, so the boot check sees a stale route.
    assert rec[pff.ROUTE_HASH_KEY] == pff.route_hash(task.body, task.skills or (), board, snapshot=snap)
    assert rec[pff.ROUTE_HASH_KEY] != pff.route_hash(task.body, task.skills or (), board)
    monkeypatch.setattr(pff, "hard_requirements", real)
    rec = pff.placement_record(task, board)
    assert rec[pff.ROUTE_HASH_KEY] == pff.route_hash(task.body, task.skills or (), board)


def test_boot_check_survives_json_null_skills(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
        conn.execute("UPDATE tasks SET skills = 'null' WHERE id = ?", (tid,))
        conn.commit()
    _placed_env(monkeypatch, tid, 1)
    assert pff.boot_check(tid).startswith("BROWSER_RAIL_ABSENT")


def test_background_review_fork_never_gates_or_queues(board, monkeypatch, tmp_path):
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    from tools.skill_provenance import set_current_write_origin, BACKGROUND_REVIEW, _write_origin
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    tok = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        out = json.loads(registry.dispatch("skill_view", {"name": "amazon-cart-ops-local"}, task_id="t"))
        assert "error" not in out or not str(out.get("error", "")).startswith("BROWSER_RAIL_ABSENT")
        assert rp.record_absent(pff.absent_line("screen", "ace-ai")) is None
    finally:
        _write_origin.reset(tok)
    assert rp.take_rail_absent() is None


# -- Prism round 2 on #1963 ---------------------------------------------------------------

def _revoke_on_read(monkeypatch, board, host="ace-ai", cap="browser-session", after=1):
    """Give ``host`` the capability, then revoke it (atomic rewrite) right after the
    ``after``-th host-capability read: the torn-snapshot race Prism r2 described."""
    m = board / khc.MAP_FILE
    doc = json.loads(m.read_text())
    doc["hosts"].setdefault(host, {})["capabilities"] = sorted(
        set(doc["hosts"][host].get("capabilities") or []) | {cap})
    m.write_text(json.dumps(doc))
    revoked = dict(doc)
    revoked["hosts"] = {**doc["hosts"], host: {**doc["hosts"][host],
                        "capabilities": [c for c in doc["hosts"][host]["capabilities"] if c != cap]}}
    real = pff.snapshot_map
    calls = {"n": 0}

    def load(snapshot):
        out = real(snapshot)
        calls["n"] += 1
        if calls["n"] == after:
            m.write_text(json.dumps(revoked))
        return out
    monkeypatch.setattr(pff, "snapshot_map", load)
    return calls


def test_boot_check_rereads_when_the_map_changes_under_it(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1)
    _revoke_on_read(monkeypatch, board)
    assert (pff.boot_check(tid) or "").startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")


def test_skill_gate_rereads_when_the_map_changes_under_it(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    _revoke_on_read(monkeypatch, board)
    assert (pff.skill_gate("amazon-cart-ops-local") or "").startswith("BROWSER_RAIL_ABSENT")


@pytest.mark.parametrize("spelling", ["personal:amazon-cart-ops-local", "personal/amazon-cart-ops-local",
                                      "Amazon-Cart-Ops-Local"])
def test_skill_gate_normalizes_qualified_names(board, monkeypatch, spelling):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    assert (pff.skill_gate(spelling) or "").startswith("BROWSER_RAIL_ABSENT"), spelling


def test_midrun_qualified_skill_view_is_gated_through_the_registry(board, monkeypatch, tmp_path):
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    out = json.loads(registry.dispatch("skill_view", {"name": "personal:amazon-cart-ops-local"}, task_id="t"))
    assert out["error"].startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")
    assert rp.take_rail_absent() == out["error"]


@pytest.mark.parametrize("caps", [json.dumps({"*": "off"}), json.dumps({"*": "warn"})])
def test_unplaced_worker_still_parks_on_a_rail_absent_whatever_the_caps_env(board, monkeypatch, caps):
    # #1876: the dispatcher sets KANBAN_PLACEMENT_CAPS on EVERY spawn (off when the layer is
    # off). An unplaced worker missing the rail must still record the line so the turn loop parks.
    from tools import rail_preflight as rp
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv(kwh.PLACEMENT_ENV, raising=False)
    monkeypatch.setenv(khc.CAPS_ENV, caps)
    monkeypatch.setattr(rp, "_endpoint_answers", lambda url: False)   # hermetic: no live :9222 probe
    msg = rp.browser_rail_error("http://127.0.0.1:9222")
    assert msg and msg.startswith("BROWSER_RAIL_ABSENT")
    assert rp.take_rail_absent() == msg


# -- Apollo review 2026-10-13 on #1963 @274fcd8c ----------------------------------------------

def _churn(monkeypatch, board, *, raise_too=False):
    """Every snapshot parse rewrites the map (still valid JSON): no two reads ever agree."""
    m = board / khc.MAP_FILE
    real = pff.snapshot_map
    n = {"loads": 0}

    def load(snapshot):
        out = real(snapshot)
        n["loads"] += 1
        m.write_text(m.read_text() + "\n")
        if raise_too:
            raise RuntimeError("index mid-write")
        return out
    monkeypatch.setattr(pff, "snapshot_map", load)
    monkeypatch.setattr(pff, "_SNAPSHOT_BACKOFF_S", 0.0)
    return n


def test_boot_check_refuses_when_every_snapshot_is_torn(board, monkeypatch):
    # 62b835e510f0: the card needs nothing the host lacks, so every attempt says None (admit).
    # Five torn reads must still refuse: an unproven admission is not an admission.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain coding card")
    _placed_env(monkeypatch, tid, 1)
    n = _churn(monkeypatch, board)
    line = pff.boot_check(tid)
    assert line is not None and line.startswith(pff.INDETERMINATE_TOKEN), line
    assert pff.ABSENT_RE.match(line)
    assert n["loads"] >= pff._SNAPSHOT_TRIES


@pytest.mark.parametrize("raise_too", [False, True])
def test_skill_gate_refuses_when_every_snapshot_is_torn(board, monkeypatch, raise_too):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    _churn(monkeypatch, board, raise_too=raise_too)
    # a skill this host serves fine: every attempt admits; and attempts that raise
    line = pff.skill_gate("some-coding-skill")
    assert line is not None and line.startswith(pff.INDETERMINATE_TOKEN), line


def test_torn_snapshots_refuse_nothing_when_nothing_is_enforced(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    _churn(monkeypatch, board)
    assert pff.boot_check(tid) is None
    assert pff.skill_gate("amazon-cart-ops-local") is None


def test_stable_snapshot_control_admits_and_refuses_as_before(board, monkeypatch):
    with kb.connect_closing() as conn:
        ok = kb.create_task(conn, title="t", assignee="daedalus", body="plain coding card")
        bad = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, ok, 1)
    assert pff.boot_check(ok) is None
    assert pff.skill_gate("some-coding-skill") is None
    assert pff.boot_check(bad).startswith("BROWSER_RAIL_ABSENT cap=browser-session")


def test_indeterminate_line_parks_needs_input_and_never_reroutes(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ace-ai\nx")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run)
    line = pff.indeterminate_line("ace-ai")
    assert kwe.block_on_rail_absent(line) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == []
    assert "requires:" not in (t.body or "")


@pytest.mark.parametrize("spelling", ["browser-access", "media/browser-access", "media:browser-access",
                                      "Media:Browser-Access"])
def test_every_skill_spelling_derives_the_same_requirements(board, spelling):
    # 2972d9d25619: derivation, needs lane lookup and the gates share one normalizer.
    cmap = khc.load_map(board)
    reqs, _ = khc.derive("", [spelling], cmap)
    assert sorted({r.cap for r in reqs if r.strength == khc.HARD}) == ["browser-session", "keychain"]
    assert pff.hard_requirements(cmap, "", [spelling]) == ["browser-session", "keychain"]
    assert pff._skill_key(spelling) == khc.skill_key(spelling) == "browser-access"


@pytest.mark.parametrize("spelling", ["ops:amazon-cart-ops-local", "ops/amazon-cart-ops-local",
                                      "amazon-cart-ops-local"])
def test_lane_of_reads_every_skill_spelling(tmp_path, spelling):
    # 64301a687435: a migrated lane attached as category:name must not resolve to "none".
    (tmp_path / kn.REGISTRY_FILE).write_text(json.dumps(
        {"migrated_lanes": ["amazon-cart-ops-local"], "legacy_allowlist": []}))
    reg = kn.load_registry(tmp_path)
    assert kn.lane_of([spelling], "", reg) == "amazon-cart-ops-local"
    assert isinstance(kn.resolve("browser:amazon-alex", kn.lane_of([spelling], "", reg), reg), kn.Refusal)


# -- boot seam + preload ordering (77f581ae3801, fbadb37df1b5, 98e899076450) -----------------

class _FakeCli:
    def __init__(self):
        self.calls = []

    def start_preloaded_skills(self):
        self.calls.append("preload")

    def _claim_active_session(self, *_a, **_k):
        self.calls.append("claim")
        return False          # stop the one-shot right after the gate


def _boot(monkeypatch, line, decided):
    from hermes_cli import cli_single_query as csq
    import tools.kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_boot")
    monkeypatch.setattr(kt, "register_current_worker_from_env", lambda: True)
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: "t_boot")
    monkeypatch.setattr(pff, "boot_check", lambda task, preloads=(): line)
    seen = []
    monkeypatch.setattr(kwe, "block_on_rail_absent",
                        lambda m: seen.append(m) or decided)
    cli = _FakeCli()
    with pytest.raises(SystemExit) as exc:
        csq._run_single_query_mode(cli, "do it", None, True, True)
    return exc.value.code, seen, cli.calls


def test_boot_refusal_goes_through_the_rail_seam_before_any_preload(monkeypatch):
    line = pff.absent_line("browser-session", "ace-ai")
    code, seen, calls = _boot(monkeypatch, line, True)
    assert (code, seen, calls) == (0, [line], [])


def test_boot_refusal_that_did_not_commit_is_not_a_clean_exit(monkeypatch):
    code, _seen, calls = _boot(monkeypatch, pff.absent_line("screen", "ace-ai"), False)
    assert code == 1 and calls == []


def test_boot_pass_starts_the_preload_then_proceeds(monkeypatch):
    code, seen, calls = _boot(monkeypatch, None, True)
    assert seen == [] and calls == ["preload", "claim"] and code == 1


def test_kanban_worker_preload_does_not_render_before_the_boot_gate(monkeypatch):
    # fbadb37df1b5: rendering a skill (template vars, inline shell when skills.inline_shell is on)
    # happens in the preload thread; a kanban worker must not start it before Layer 0.
    import cli as cli_mod
    rendered = []
    monkeypatch.setattr(cli_mod, "build_preloaded_skills_prompt",
                        lambda names, **kw: rendered.append(list(names)) or ("", list(names), []))

    class _Cli(_FakeCli):
        session_id = "s"
        ignore_rules = True
        _auto_load_skills_result = ("", [], [])
        _preload_skills_thread = None

        def __init__(self, **kw):
            super().__init__()

        start_preloaded_skills = cli_mod.HermesCLI.start_preloaded_skills
    monkeypatch.setattr(cli_mod, "HermesCLI", _Cli)
    args = dict(model=None, toolsets="terminal", provider=None, reasoning=None, api_key=None, base_url=None,
                max_turns=None, run_budget=None, verbose=False, compact=False, resume=None, checkpoints=False,
                pass_session_id=False, ignore_rules=True, skills="amazon-cart-ops-local")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_boot")
    c = cli_mod._build_cli_from_args(**args)
    import time
    time.sleep(0.2)
    assert c._preload_skills_thread.ident is None and rendered == []
    c.start_preloaded_skills()
    c._preload_skills_thread.join(5)
    assert rendered == [["amazon-cart-ops-local"]]
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    c2 = cli_mod._build_cli_from_args(**args)
    c2._preload_skills_thread.join(5)
    assert rendered == [["amazon-cart-ops-local"]] * 2



# -- consolidated correction 2026-10-13 (Apollo, parent-verified) on #1963 @39b28b56 ------------

def test_boot_check_covers_the_actual_preload_not_only_the_card(board, monkeypatch):
    # fbadb37df1b5 / 54ceec367123 / 62677503233c: a plain card, but the worker's queued preload
    # (auto_load or a CLI-only --skills) holds a browser skill. Boot must refuse.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain coding card")
    _placed_env(monkeypatch, tid, 1, record=pff.placement_record(_task(tid), board))
    assert pff.boot_check(tid) is None                                         # card alone: admit
    for spelling in ("amazon-cart-ops-local", "personal/amazon-cart-ops-local/"):
        line = pff.boot_check(tid, preloads=[spelling])
        assert (line or "").startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai"), spelling
    assert pff.boot_check(tid, preloads=["some-coding-skill"]) is None        # control
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    assert pff.boot_check(tid, preloads=["amazon-cart-ops-local"]) is None    # WARN stays inert


def test_cli_only_preload_is_refused_before_anything_renders(board, monkeypatch, tmp_path):
    # Real boot_check + real block_on_rail_absent through the one-shot entry: the --skills
    # preload thread never starts, the card is re-routed, no payload was rendered.
    from hermes_cli import cli_single_query as csq
    import tools.kanban_tools as kt
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain coding card")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run)
    monkeypatch.setattr(kt, "register_current_worker_from_env", lambda: True)
    cli = _FakeCli()
    cli._preload_skills_requested = ["amazon-cart-ops-local"]
    with pytest.raises(SystemExit) as exc:
        csq._run_single_query_mode(cli, "do it", None, True, True)
    assert exc.value.code == 0 and cli.calls == []
    t = _task(tid)
    assert t.status == "ready" and t.body.rstrip().endswith("requires: browser-session")


@pytest.mark.parametrize("body", ["requires: never-defined-cap", "plain\nrequires: browser-session, never-defined-cap"])
def test_unknown_hard_requirement_is_kept_and_refused_under_enforcement(board, monkeypatch, body):
    # 470af3484c95: an unresolved hard requirement is a requirement, never "needs nothing".
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body=body)
    assert "unknown:never-defined-cap" in pff.hard_requirements(khc.load_map(board), body, [])
    rec = pff.placement_record(_task(tid), board)
    assert "unknown:never-defined-cap" in rec[pff.REQUIRES_KEY]
    for record in (rec, None):                                   # fresh stamp, and a stale route
        _placed_env(monkeypatch, tid, 1, record=record)
        line = pff.boot_check(tid)
        assert line is not None and line.startswith(pff.UNRESOLVED_TOKEN), line
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    assert pff.boot_check(tid) is None                           # WARN: inert
    (board / khc.MAP_FILE).write_text("{not json")
    _placed_env(monkeypatch, tid, 1)
    assert pff.boot_check(tid) is None                           # invalid map: deliberate WARN


def test_unresolved_line_parks_needs_input_and_never_reroutes(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: never-defined-cap")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run)
    line = pff.boot_check(tid)
    assert line.startswith(pff.UNRESOLVED_TOKEN)
    assert kwe.block_on_rail_absent(line) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == [] and "requires: placement" not in t.body


@pytest.mark.parametrize("spelling", ["amazon-cart-ops-local", "personal/amazon-cart-ops-local",
                                      "personal:amazon-cart-ops-local", "personal/amazon-cart-ops-local/"])
def test_every_loader_spelling_is_gated_through_the_real_registry(board, monkeypatch, tmp_path, spelling):
    # f779b3d305d8: the loader accepts a trailing separator; the gate must key it too.
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    from tools.skills_tool import skill_view
    import model_tools  # noqa: F401
    rp._emitted.clear()
    assert khc.skill_key(spelling) == "amazon-cart-ops-local"
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    monkeypatch.delenv(khc.CAPS_ENV, raising=False)
    assert json.loads(skill_view(spelling)).get("success") is True      # positive loader control
    _placed_env(monkeypatch, tid, 1)
    out = json.loads(registry.dispatch("skill_view", {"name": spelling}, task_id="t"))
    assert out["error"].startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai"), spelling
    assert pff.attached_skill_gap([spelling], "ace-ai",
                                  khc.tick(board, {"placement": {"capabilities": "enforce"}}, ("ace-ai",))) \
        == "browser-session"


@pytest.mark.parametrize("with_task_id", [True, False])
def test_skill_gate_runs_without_the_optional_task_id(board, monkeypatch, tmp_path, with_task_id):
    # 64c6bea00955: placement ownership decides, not the dedup id.
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    kw = {"task_id": "t"} if with_task_id else {}
    out = json.loads(registry.dispatch("skill_view", {"name": "amazon-cart-ops-local"}, **kw))
    assert out["error"].startswith("BROWSER_RAIL_ABSENT")
    assert rp.take_rail_absent() == out["error"]


def test_review_fork_without_task_id_is_never_gated(board, monkeypatch, tmp_path):
    _skills_home(monkeypatch, tmp_path)
    from tools import rail_preflight as rp
    from tools.registry import registry
    from tools.skill_provenance import set_current_write_origin, BACKGROUND_REVIEW, _write_origin
    import model_tools  # noqa: F401
    rp._emitted.clear()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    tok = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        out = json.loads(registry.dispatch("skill_view", {"name": "amazon-cart-ops-local"}))
    finally:
        _write_origin.reset(tok)
    assert not str(out.get("error", "")).startswith("BROWSER_RAIL_ABSENT")
    assert rp.take_rail_absent() is None


def _guard(tid):
    from types import SimpleNamespace
    from hermes_cli import kanban_db_dispatch as kdd
    with kb.connect_closing() as conn:
        return kdd.check_respawn_guard(conn, tid, pr_state_resolver=SimpleNamespace(resolve=lambda *a, **k: "OPEN"))


def test_reroute_is_a_one_shot_continuation_for_the_active_pr_guard(board, monkeypatch):
    # 360354deb8ab: real claim -> PR comment -> re-route. The next spawn may resume the PR once.
    monkeypatch.setattr(kb, "_pr_belongs_to_other_card", lambda *a, **k: False)
    monkeypatch.setattr(kb, "_pr_needs_its_worker", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ace-ai\nplain")
    run = _claim(tid)
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "daedalus", "PR: https://github.com/ANG-Ventures/hermes-agent/pull/1963")
    assert _guard(tid) == "active_pr"                             # control: no intent yet
    _placed_env(monkeypatch, tid, run)
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    assert _task(tid).status == "ready"
    assert _guard(tid) is None                                    # continuation: may spawn
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "spawned", {"pid": 1})
    assert _guard(tid) == "active_pr"                             # consumed by that spawn


def test_a_newer_pr_comment_after_the_reroute_is_not_covered(board, monkeypatch):
    monkeypatch.setattr(kb, "_pr_belongs_to_other_card", lambda *a, **k: False)
    monkeypatch.setattr(kb, "_pr_needs_its_worker", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run)
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "daedalus", "PR: https://github.com/ANG-Ventures/hermes-agent/pull/7")
    assert _guard(tid) == "active_pr"


# -- custody (0e9694bf2944): host-local workspaces, real probe, a fake ssh -------------------------

def _hosts_runner(roots):
    """Run the probe's remote script locally, against the FAKE filesystem of the ssh target."""
    import subprocess

    def run(argv, **kw):
        target, script = argv[-2], argv[-1]
        root = roots.get(target.split("@")[-1])
        if root is None:
            return subprocess.CompletedProcess(argv, 255, "", "ssh: connect refused")
        return subprocess.run(["bash", "-c", script.replace("/ws/", f"{root}/ws/")], **kw)
    return run


_REAL_PROBE = kwh.remote_workspace_has_content      # bound at import, before any fixture stubs it


def _custody(monkeypatch, tmp_path, roots):
    monkeypatch.setattr(kwh, "remote_workspace_has_content",
                        lambda raw, runner=None: _REAL_PROBE(raw, _hosts_runner(roots)))


def _placed_ws(monkeypatch, tid, run, host, ssh_host, ws="/ws/card"):
    _placed_env(monkeypatch, tid, run, host=host,
                record={"env": {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": ssh_host,
                                "TERMINAL_SSH_USER": "kanbanw", "TERMINAL_CWD": ws}})


def test_midrun_miss_with_work_in_the_source_workspace_parks_for_recovery(board, monkeypatch, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "ws/card").mkdir(parents=True)
    (dst / "ws/card").mkdir(parents=True)
    (src / "ws/card/draft.patch").write_text("SOURCE_SENTINEL")
    (dst / "ws/card/other.txt").write_text("DEST_SENTINEL")
    _custody(monkeypatch, tmp_path, {"10.0.0.1": src, "10.0.0.2": dst})
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ace-ai\nplain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.1")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == [] and "requires: browser-session" not in t.body
    (ev,) = _events(tid, pff.CUSTODY_EVENT)
    assert (ev["host"], ev["run_id"], ev["workspace"]) == ("ace-ai", run, "/ws/card")
    with kb.connect_closing() as conn:
        reason = kb._latest_block_reason(conn, tid)
    assert "ace-ai" in reason and f"run {run}" in reason and "/ws/card" in reason
    assert (src / "ws/card/draft.patch").read_text() == "SOURCE_SENTINEL"       # source untouched
    assert (dst / "ws/card/other.txt").read_text() == "DEST_SENTINEL"


def test_proven_empty_source_reroutes_whatever_the_destination_holds(board, monkeypatch, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "ws/card").mkdir(parents=True)
    (dst / "ws/card").mkdir(parents=True)
    (dst / "ws/card/other.txt").write_text("DEST_SENTINEL")
    _custody(monkeypatch, tmp_path, {"10.0.0.1": src, "10.0.0.2": dst})
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ace-ai\nplain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.1")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    assert _task(tid).status == "ready" and _events(tid, pff.CUSTODY_EVENT) == []
    assert len(_events(tid, pff.REROUTE_EVENT)) == 1


def test_unprovable_source_workspace_fails_closed(board, monkeypatch, tmp_path):
    _custody(monkeypatch, tmp_path, {})                          # ssh refused
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.9")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    assert _task(tid).block_kind == "needs_input" and _events(tid, pff.REROUTE_EVENT) == []


def test_stale_run_with_work_never_mutates_the_card(board, monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / "ws/card").mkdir(parents=True)
    (src / "ws/card/draft.patch").write_text("x")
    _custody(monkeypatch, tmp_path, {"10.0.0.1": src})
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run + 5, "ace-ai", "10.0.0.1")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is False
    assert _task(tid).status == "running" and _events(tid, pff.CUSTODY_EVENT) == []


def test_second_miss_keeps_needs_input_without_a_custody_probe(board, monkeypatch, tmp_path):
    # first-clean-miss / second-miss-needs_input: the second miss parks on the ledger as before.
    calls = []
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: calls.append(1) or False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.1")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    run2 = _claim(tid)
    _placed_ws(monkeypatch, tid, run2, "ace-media", "10.0.0.2")
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-media")) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input" and calls == [1]
    with kb.connect_closing() as conn:
        assert "Second placement miss" in kb._latest_block_reason(conn, tid)


# -- parent review of 7e4dcce8: four boundary cases -------------------------------------------

def test_local_placement_rail_miss_parks_without_custody_probe_or_reroute(board, monkeypatch, tmp_path):
    # 03985b00389e: the synthetic local placement is unplaced for every gate AND the exit seam.
    from hermes_cli.kanban_local_worker_uid import LOCAL_HOST_NAME
    from tools import rail_preflight as rp
    calls = []
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: calls.append(1) or False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "unfinished.patch").write_text("SOURCE_WORK")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_env(monkeypatch, tid, run, host=LOCAL_HOST_NAME,
                record={"env": {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "localhost",
                                "TERMINAL_CWD": str(ws)}})
    assert kwe.block_on_rail_absent(rp.rail_absent_message("browser", LOCAL_HOST_NAME)) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == [] and calls == [] and "requires:" not in t.body
    assert (ws / "unfinished.patch").read_text() == "SOURCE_WORK"


def test_committed_custody_hold_stays_handled_when_its_event_write_fails(board, monkeypatch, tmp_path):
    # 35b39eb7244e: after block_task committed, a diagnostic event failure is not "unhandled".
    from unittest.mock import patch
    hook = []
    monkeypatch.setattr("tools.capability_gap_hooks.on_rail_absent", lambda m: hook.append(m))
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: True)
    real = kb._append_event

    def failing(conn, task_id, kind, payload=None, **kw):
        if kind == pff.CUSTODY_EVENT:
            raise OSError("diagnostic write failed after block commit")
        return real(conn, task_id, kind, payload, **kw)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.1")
    with patch.object(kb, "_append_event", side_effect=failing):
        assert kwe.block_on_rail_absent(RAIL) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input" and len(hook) == 1
    assert _events(tid, pff.CUSTODY_EVENT) == [] and _events(tid, pff.REROUTE_EVENT) == []


def test_custody_block_failure_before_commit_is_unhandled_and_fenced(board, monkeypatch):
    # The other side of 35b39eb7244e: nothing committed means not handled, card untouched.
    hook = []
    monkeypatch.setattr("tools.capability_gap_hooks.on_rail_absent", lambda m: hook.append(m))
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: True)

    def boom(*a, **kw):
        raise OSError("board write failed before commit")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "10.0.0.1")
    monkeypatch.setattr(kb, "block_task", boom)
    assert kwe.block_on_rail_absent(RAIL) is False
    assert _task(tid).status == "running" and hook == []


def _probe_raw(ws):
    return json.dumps({"host": "ace-ai", "env": {"TERMINAL_SSH_HOST": "fixture.invalid",
                                                 "TERMINAL_CWD": str(ws)}})


def _local_shell(argv, **kw):
    import subprocess
    return subprocess.run(["bash", "-c", argv[-1]], **kw)


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores mode bits")
def test_custody_probe_discriminates_absent_from_unreadable(tmp_path):
    import subprocess
    parent = tmp_path / "parent"
    ws = parent / "workspace"
    ws.mkdir(parents=True)
    assert _REAL_PROBE(_probe_raw(ws), _local_shell) is False                   # empty
    (ws / "unfinished.patch").write_text("SOURCE_WORK")
    assert _REAL_PROBE(_probe_raw(ws), _local_shell) is True                    # content
    assert _REAL_PROBE(_probe_raw(parent / "never"), _local_shell) is False     # ENOENT
    assert _REAL_PROBE(_probe_raw(parent / "a" / "b"), _local_shell) is False   # deep ENOENT
    refused = lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "refused")
    assert _REAL_PROBE(_probe_raw(parent / "never"), refused) is True           # ssh failure
    for mode in (0o000, 0o600):                                                 # unsearchable
        parent.chmod(mode)
        try:
            assert _REAL_PROBE(_probe_raw(ws), _local_shell) is True
            assert _REAL_PROBE(_probe_raw(parent / "never"), _local_shell) is True
        finally:
            parent.chmod(0o700)
    assert (ws / "unfinished.patch").read_text() == "SOURCE_WORK"


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores mode bits")
def test_unsearchable_source_parks_through_the_exit_seam(board, monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    ws = parent / "workspace"
    ws.mkdir(parents=True)
    (ws / "unfinished.patch").write_text("SOURCE_WORK")
    monkeypatch.setattr(kwh, "remote_workspace_has_content",
                        lambda raw, runner=None: _REAL_PROBE(raw, _local_shell))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    run = _claim(tid)
    _placed_ws(monkeypatch, tid, run, "ace-ai", "fixture.invalid", ws=str(ws))
    parent.chmod(0)
    try:
        assert kwe.block_on_rail_absent(RAIL) is True
    finally:
        parent.chmod(0o700)
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == [] and len(_events(tid, pff.CUSTODY_EVENT)) == 1
    assert (ws / "unfinished.patch").read_text() == "SOURCE_WORK"


@pytest.mark.parametrize("stamp", ["fresh", "stale"])
@pytest.mark.parametrize("body", ["requires: browser-session, never-defined-cap",
                                  "requires: never-defined-cap, browser-session",
                                  "requires: never-defined-cap\nrequires: browser-session"])
def test_mixed_known_missing_and_unknown_requirement_parks_only(board, monkeypatch, body, stamp):
    # 470af3484c95: an unresolved word parks before any routable known miss, whatever the order.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body=body)
    run = _claim(tid)
    record = pff.placement_record(_task(tid), board) if stamp == "fresh" else None
    _placed_env(monkeypatch, tid, run, record=record)
    line = pff.boot_check(tid)
    assert line is not None and line.startswith(pff.UNRESOLVED_TOKEN), line
    assert kwe.block_on_rail_absent(line) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert _events(tid, pff.REROUTE_EVENT) == []


def test_mixed_requirement_with_preload_unknown_parks_only(board, monkeypatch):
    # The combined card + preload set: a known miss on the card, an unknown in the preloads' needs.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1)
    assert pff.boot_check(tid).startswith("BROWSER_RAIL_ABSENT")                 # control
    monkeypatch.setattr(pff, "hard_requirements",
                        lambda cmap, body, skills, _r=pff.hard_requirements:
                        ["unknown:never-defined-cap"] if list(skills) == ["odd-skill"] else _r(cmap, body, skills))
    assert pff.boot_check(tid, preloads=["odd-skill"]).startswith(pff.UNRESOLVED_TOKEN)


# -- parent review of 3d52e02b: combined correction A1-A6 (residual-probe.py) ------------------

def test_boot_check_uses_the_captured_placement_when_the_env_record_changes(board, monkeypatch):
    # A1 0c0e383033ed: the runtime path reads the placement captured at process start.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    assert kwh.capture_boot_placement() == "ace-ai"
    control = pff.boot_check(tid)
    assert control.startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    assert pff.boot_check(tid) == control                                  # env record dropped
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local"}))
    assert pff.boot_check(tid) == control                                  # env record rewritten


def test_boot_check_rederives_when_the_stamp_names_another_placement(board, monkeypatch):
    # A1: a stamp for another host is not trusted; the current card decides.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", "ace-ai")
    lie = {"host": "studio", pff.REQUIRES_KEY: [],
           pff.ROUTE_HASH_KEY: pff.route_hash("requires: browser-session", [], board)}
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps(lie))
    monkeypatch.setenv(khc.CAPS_ENV, ENFORCE_BROWSER)
    assert pff.boot_check(tid).startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")
    # Controls: an explicit environ is a complete fixture; genuinely unplaced stays inert.
    assert pff.boot_check(tid, environ={kwh.PLACEMENT_ENV: json.dumps({"host": "studio-local"}),
                                        khc.CAPS_ENV: ENFORCE_BROWSER}) is None
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    assert pff.boot_check(tid) is None


def _typo_index(board, skill="typo-skill", cap="browser_sesion"):
    idxp = board / khc.INDEX_FILE
    idx = json.loads(idxp.read_text())
    idx["skills"][skill] = [cap]
    idx["source_hash"] = khc.index_hash(idx["skills"])
    idxp.write_text(json.dumps(idx))
    assert khc.load_map(board).index_error is None


@pytest.mark.parametrize("spelling", ["typo-skill", "personal/typo-skill", "personal:typo-skill"])
def test_preload_and_midrun_refuse_an_unresolved_skill_prerequisite_alike(board, monkeypatch, spelling):
    # A2 682bd83da2ba / 289d1155b74d: one normalization, one unresolved-first decision.
    _typo_index(board)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    boot = pff.boot_check(tid, preloads=[spelling])
    mid = pff.skill_gate(spelling)
    assert boot is not None and boot.startswith(pff.UNRESOLVED_TOKEN), boot
    assert mid == boot
    # Controls: a known missing skill refuses as before, a served skill admits, WARN is inert.
    assert pff.skill_gate("amazon-cart-ops-local").startswith("BROWSER_RAIL_ABSENT")
    assert pff.skill_gate("some-coding-skill") is None
    _placed_env(monkeypatch, tid, 1, caps=json.dumps({"*": "warn"}))
    assert pff.skill_gate(spelling) is None and pff.boot_check(tid, preloads=[spelling]) is None


def test_midrun_unresolved_parks_before_a_known_miss(board, monkeypatch):
    # A2: a skill needing both a known missing cap and an unknown word parks unresolved first.
    _typo_index(board, skill="mixed-skill", cap="browser_sesion")
    idxp = board / khc.INDEX_FILE
    idx = json.loads(idxp.read_text())
    idx["skills"]["mixed-skill"] = ["browser-session", "browser_sesion"]
    idx["source_hash"] = khc.index_hash(idx["skills"])
    idxp.write_text(json.dumps(idx))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="x")
    _placed_env(monkeypatch, tid, 1)
    assert pff.skill_gate("mixed-skill").startswith(pff.UNRESOLVED_TOKEN)
    assert pff.boot_check(tid, preloads=["mixed-skill"]).startswith(pff.UNRESOLVED_TOKEN)


def test_a_map_replaced_and_restored_during_evaluation_is_never_read(board, monkeypatch):
    # A3 23e10f0f6800: the evaluation parses the captured bytes; a granting map written and
    # restored around any file read inside it cannot reach the decision.
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="requires: browser-session")
    _placed_env(monkeypatch, tid, 1)
    mp = board / khc.MAP_FILE
    before = mp.read_bytes()
    relaxed = json.loads(before)
    relaxed["hosts"]["ace-ai"]["capabilities"].append("browser-session")
    control = pff.boot_check(tid)
    assert control.startswith("BROWSER_RAIL_ABSENT")
    real_load, real_parse = khc.load_map, khc.map_from_bytes

    def flip(real):
        def inner(*a, **kw):
            mp.write_text(json.dumps(relaxed))
            try:
                return real(*a, **kw) if real is real_parse else real(*a, **kw)
            finally:
                mp.write_bytes(before)
        return inner
    monkeypatch.setattr(khc, "load_map", flip(real_load))
    monkeypatch.setattr(khc, "map_from_bytes", flip(real_parse))
    assert pff.boot_check(tid) == control
    assert pff.skill_gate("amazon-cart-ops-local").startswith("BROWSER_RAIL_ABSENT")
    assert mp.read_bytes() == before
    # Control: a map that really grants the capability admits.
    mp.write_text(json.dumps(relaxed))
    monkeypatch.setattr(khc, "load_map", real_load)
    monkeypatch.setattr(khc, "map_from_bytes", real_parse)
    assert pff.boot_check(tid) is None


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores mode bits")
@pytest.mark.parametrize("name", ["\n", "\n\n", " ", "\t", ".hidden", "ordinary.patch", "-n", "a\nb"])
def test_custody_probe_sees_every_legal_filename(tmp_path, name):
    # A4 24a15e048a61: presence never goes through a lossy line listing.
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert _REAL_PROBE(_probe_raw(ws), _local_shell) is False                   # empty control
    (ws / name).write_text("SOURCE_WORK")
    assert _REAL_PROBE(_probe_raw(ws), _local_shell) is True
    assert (ws / name).read_text() == "SOURCE_WORK"


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores mode bits")
def test_custody_probe_fails_closed_on_every_unprovable_path(tmp_path):
    # A4: the error side stays closed: unreadable workspace, file parent, dangling link.
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "x").write_text("SOURCE_WORK")
    ws.chmod(0o300)                                                          # searchable, unlistable
    try:
        assert _REAL_PROBE(_probe_raw(ws), _local_shell) is True
    finally:
        ws.chmod(0o700)
    f = tmp_path / "file"
    f.write_text("x")
    assert _REAL_PROBE(_probe_raw(f), _local_shell) is True                     # not a directory
    assert _REAL_PROBE(_probe_raw(f / "child"), _local_shell) is True            # non-directory parent
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    assert _REAL_PROBE(_probe_raw(tmp_path / "dangling"), _local_shell) is True
    assert _REAL_PROBE(_probe_raw(tmp_path / "deep" / "absent"), _local_shell) is False   # control


def _dead_pid():
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


_DEAD = _dead_pid()


def _late_spawn_card(board, monkeypatch, tmp_path):
    # The original worker has exited (a dead pid), so a genuine successor may be claimed;
    # a live original pid is the late-spawn owner guard's case, not this one.
    from hermes_cli import kanban_db_dispatch as kdd
    monkeypatch.setattr(kb, "_pr_belongs_to_other_card", lambda *a, **k: False)
    monkeypatch.setattr(kb, "_pr_needs_its_worker", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="host: ace-ai\nplain")
    run = _claim(tid)
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "daedalus", "PR: https://github.com/ANG-Ventures/hermes-agent/pull/1963")
        assert kdd.adopt_worker_pid(conn, tid, run, _DEAD)
    assert _guard(tid) == "active_pr"
    return kdd, tid, run


@pytest.mark.parametrize("order", ["late_spawn_after_reroute", "reroute_after_late_spawn"])
def test_reroute_continuation_survives_the_original_runs_late_spawn(board, monkeypatch, tmp_path, order):
    # A5 c9e6038b0137: the original run's late-spawn audit (real _set_worker_pid, same run id,
    # claim lost) is not a successor and must not consume the continuation.
    kdd, tid, run = _late_spawn_card(board, monkeypatch, tmp_path)
    _placed_env(monkeypatch, tid, run)
    monkeypatch.setattr("tools.capability_gap_hooks.on_rail_absent", lambda m: None)
    if order == "reroute_after_late_spawn":
        # The audit lands while the run still owns the card: it is a normal spawn row then;
        # the reroute comes after it, so it cannot consume the later continuation either.
        with kb.connect_closing() as conn:
            assert kdd._set_worker_pid(conn, tid, _DEAD, run_id=run) is True
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-ai")) is True
    assert _guard(tid) is None
    if order == "late_spawn_after_reroute":
        with kb.connect_closing() as conn:
            assert kdd._set_worker_pid(conn, tid, _DEAD, run_id=run) is False
            assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawned' "
                                "AND json_extract(payload,'$.claim_lost')=1", (tid,)).fetchone()[0] == 1
    assert _guard(tid) is None                                     # still one continuation owed
    # A genuine successor consumes it once.
    run2 = _claim(tid)
    with kb.connect_closing() as conn:
        assert kdd._set_worker_pid(conn, tid, _DEAD, run_id=run2) is True
    assert _guard(tid) == "active_pr"
    # A second miss parks; it never re-routes again.
    _placed_env(monkeypatch, tid, run2, host="ace-media")
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    assert kwe.block_on_rail_absent(pff.absent_line("browser-session", "ace-media")) is True
    t = _task(tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    assert len(_events(tid, pff.REROUTE_EVENT)) == 1


def test_operator_requeue_is_still_consumed_by_any_later_spawn(board, monkeypatch, tmp_path):
    # A5 control: operator intent keeps its established "a later spawn consumes it" semantics.
    kdd, tid, run = _late_spawn_card(board, monkeypatch, tmp_path)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "requeued", {"by": "operator"}, run_id=run)
    assert _guard(tid) is None
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "spawned", {"pid": 1, "late_spawn": True, "claim_lost": True}, run_id=run)
    assert _guard(tid) == "active_pr"


def test_host_classification_does_not_need_the_posix_uid_module(monkeypatch):
    # A6 f6dcfd6d5efb: simulated unavailable ``pwd`` at the import seam (NOT native Windows E2E).
    import builtins
    import importlib
    import sys
    orig = builtins.__import__

    def no_pwd(name, *a, **kw):
        if name == "pwd":
            raise ModuleNotFoundError("No module named 'pwd' (simulated)")
        return orig(name, *a, **kw)
    monkeypatch.delitem(sys.modules, "hermes_cli.kanban_local_worker_uid", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_pwd)
    assert kwh.is_pool_host(None) is False
    assert kwh.is_pool_host(kwh.LOCAL_HOST_NAME) is False
    assert kwh.is_pool_host("ace-ai") is True
    # Without pwd (native Windows) an unset knob still spawns plain local workers; a set knob,
    # a privilege boundary the host cannot honour, fails closed (Prism f591bbc9b1f8).
    klu = importlib.import_module("hermes_cli.kanban_local_worker_uid")
    assert klu.pwd is None                                           # control: the seam is real
    assert klu.resolve_user({}) is None
    assert klu.resolve_user({"local_worker_uid": None}) is None
    with pytest.raises(klu.LocalWorkerUidError):
        klu.resolve_user({"local_worker_uid": "_kanbanw"})
