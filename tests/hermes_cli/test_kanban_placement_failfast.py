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
    _placed_env(monkeypatch, tid, run2, host="studio-local")
    assert kwe.block_on_rail_absent(RAIL) is True
    assert _task(tid).status == "review"
    assert _events(tid, pff.REROUTE_EVENT)[0]["to"] == "review"


def test_stamp_is_dropped_when_the_files_change_during_derivation(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="daedalus", body="plain")
    real = pff.hard_requirements

    def racing(body, skills, fleet_dir):
        out = real(body, skills, fleet_dir)
        idx = Path(fleet_dir) / khc.INDEX_FILE
        idx.write_bytes(idx.read_bytes() + b"\n")          # the index changes mid-derivation
        return out
    monkeypatch.setattr(pff, "hard_requirements", racing)
    assert pff.placement_record(_task(tid), board) == {}
    monkeypatch.setattr(pff, "hard_requirements", real)
    task = _task(tid)
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
    real = khc.load_map
    calls = {"n": 0}

    def load(fleet_dir):
        out = real(fleet_dir)
        calls["n"] += 1
        if calls["n"] == after:
            m.write_text(json.dumps(revoked))
        return out
    monkeypatch.setattr(khc, "load_map", load)
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
    """Every load_map rewrites the map (still valid JSON): no two reads ever agree."""
    m = board / khc.MAP_FILE
    real = khc.load_map
    n = {"loads": 0}

    def load(fleet_dir):
        out = real(fleet_dir)
        n["loads"] += 1
        m.write_text(m.read_text() + "\n")
        if raise_too:
            raise RuntimeError("index mid-write")
        return out
    monkeypatch.setattr(khc, "load_map", load)
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
    assert pff.hard_requirements("", [spelling], board) == ["browser-session", "keychain"]
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
    monkeypatch.setattr(pff, "boot_check", lambda task: line)
    seen = []
    monkeypatch.setattr(kwe, "block_on_rail_absent", lambda m: seen.append(m) or decided)
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
