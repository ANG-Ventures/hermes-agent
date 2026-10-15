"""Browser Sandboxes P1-4 (t_42728faf): the resolution gate switched on for the three Phase 1 lanes.

Spec v1.2 s3.3 (three keys, lane_of), AC-1.13 (kill switch, dispatcher half), AC-1.15 (i)-(v) as
dispatcher dry runs, pass-3 R2 (third lane) and R4 (creator provenance), and the pass-3 lens: the dry
run and the live tick reach ONE placement function (``kanban_browser_gate.gate_rows``).

Hermetic: a temp board, a temp ``fleet/browser-hosts.json`` and a fake identity lookup (the real one
asks browserd's ``identity`` verb over its unix socket). No browserd, no Docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_browser_gate as kbg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_needs as kn
from hermes_cli import kanban_worker_pool as kwp

PHASE1 = ["browser-soak-amazon", "browser-phase1-ephemeral", "browser-phase1-read"]
LEGACY = ["amazon-cart-ops-local", "amazon-login", "browser-access", "ebay-seller-ops"]
SOAK_PROFILE = "soak-reader"
OTHER = "alpha"

AMAZON = {"name": "id-amazon-alex", "app": "amazon", "account": "alex", "host": "studio",
          "sealed_at": 1792000000.0, "policy": json.dumps({"read": [SOAK_PROFILE]})}


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    for p in (OTHER, SOAK_PROFILE):
        (h / "profiles" / p).mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    (h / "fleet").mkdir()
    write_hosts(h, mode="on")
    return h


class Ledger:
    """The identity lookup; records every call (AC-1.13: zero calls while mode is off)."""

    def __init__(self, rows=(AMAZON,)):
        self.rows = {r["name"]: r for r in rows}
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return self.rows.get(name)


@pytest.fixture
def ledger(monkeypatch):
    led = Ledger()
    monkeypatch.setattr(kbd, "_browser_lookup", lambda: led)
    return led


def write_hosts(h: Path, *, mode="on", migrated=PHASE1, legacy=LEGACY):
    (h / "fleet" / kn.REGISTRY_FILE).write_text(json.dumps(
        {"browsers": {"mode": mode, "migrated_lanes": list(migrated), "legacy_allowlist": list(legacy)}}))


def _pool():
    """The live pool's shape: a pool plan with the Studio-bound browser skills (t_8c474c2e)."""
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                        absence="required", profiles=(OTHER, SOAK_PROFILE), state="active", enabled=True,
                        priority=0)
    p = kwp.plan([host], {}, probe=lambda h: (1.0, 32))
    p.band, p.spill_reason = "paused", "load"
    p.config = kwp.PoolConfig(hosts=("ace-ai",), profiles=(OTHER, SOAK_PROFILE), pool_hosts=(host,),
                              studio_bound_skills=("amazon-cart-ops-local", "amazon-login", "browser-access",
                                                   "ebay-seller-ops", "ebay-login"))
    return p


def _card(conn, *, assignee=OTHER, skills=(), body="", minted_by=None):
    tid = kb.create_task(conn, title="fx", assignee=assignee, body=body, skills=list(skills) or None)
    if minted_by is not None:
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='created'",
                           (tid,)).fetchone()
        p = json.loads(row["payload"] or "{}")
        p["minted_by"] = minted_by
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(p), row["id"]))
        conn.commit()
    return tid


def _dry(conn, plan=None):
    return kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1, max_spawn=64, spawn_limit=4,
                             spillover=plan if plan is not None else _pool(), reconcile_orphans=False)


def _live(conn, plan=None):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append(task.id)
        return 4242
    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spawn_paused="test",
                            spillover=plan if plan is not None else _pool(), reconcile_orphans=False)
    return res, spawned


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _route(res, tid):
    (r,) = [x for x in res.routes if x["task_id"] == tid]
    return r


# -- registry: nested schema (the live file) ---------------------------------------------------

def test_registry_reads_the_nested_browsers_section(home):
    reg = kn.load_registry(home / "fleet")
    assert reg.mode == "on" and reg.migrated == tuple(PHASE1) and "amazon-cart-ops-local" in reg.legacy
    write_hosts(home, mode="off")
    assert kn.load_registry(home / "fleet").mode == "off"
    (home / "fleet" / kn.REGISTRY_FILE).write_text("{}")
    assert kn.load_registry(home / "fleet") == kn.LaneRegistry()


# -- AC-1.15 (i)-(v) as dispatcher dry runs ----------------------------------------------------

def test_ac115_i_ii_unmigrated_lanes_are_placed_exactly_as_today(home, ledger):
    with kb.connect_closing() as conn:
        ebay = _card(conn, skills=["ebay-login", "browser-access"], body="needs: browser:ebay-ace\n")
        amz = _card(conn, skills=["amazon-cart-ops-local"], body="needs: browser:amazon-alex\n")
        res = _dry(conn)
        for tid, lane, need in ((ebay, "browser-access", "browser:ebay-ace"),
                                (amz, "amazon-cart-ops-local", "browser:amazon-alex")):
            assert res.placement_affinity[tid]["host"] == kwp.STUDIO_HOST
            assert res.placement_affinity[tid]["rule"] == "studio_bound_skill"
            assert res.placement_caps[tid]["legacy_route"] == {"need": need, "lane": lane,
                                                               "reason": "lane_not_migrated"}
            assert "browser_refused" not in res.placement_caps[tid]
            assert not any(str(v).startswith("BROWSER_") for v in json.dumps(res.placement_caps[tid]).split('"'))
        assert ledger.calls == []          # an unmigrated lane never asks the ledger
        # live tick: exactly one legacy_route event each, no BROWSER_* token, still ready
        _live(conn)
        _live(conn)
        for tid, lane in ((ebay, "browser-access"), (amz, "amazon-cart-ops-local")):
            assert [e["reason"] for e in _events(conn, tid, "legacy_route")] == ["lane_not_migrated"]
            assert _events(conn, tid, "legacy_route")[0]["lane"] == lane
            assert _events(conn, tid, kbg.REFUSED_EVENT) == []
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "ready"


def test_ac115_iii_a_claimed_lane_does_not_grant(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=OTHER, skills=["browser-soak-amazon", "browser-access"],
                    body="needs: browser:amazon-alex\n")
        res = _dry(conn)
        ref = res.placement_caps[tid]["browser_refused"]
        assert ref["token"] == "BROWSER_CLASS_REFUSED" and "reason=policy" in ref["detail"]
        assert ref["exit"] == kn.NEEDS_INPUT and ref["lane"] == "browser-soak-amazon"
        assert "browser_lease_plan" not in res.placement_caps[tid]
        _live(conn)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row[0] == "blocked"
        (blk,) = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,))]
        assert blk.get("reason", "").startswith("BROWSER_CLASS_REFUSED") and blk.get("kind") == "needs_input"


def test_ac115_iv_lane_of_is_the_first_registered_skill(home):
    reg = kn.load_registry(home / "fleet")
    assert kn.lane_of(["browser-access", "browser-soak-amazon"], "", reg) == "browser-access"
    # browser-access is registered (legacy); a card whose first REGISTERED skill is the soak lane:
    assert kn.lane_of(["notes", "browser-soak-amazon"], "", reg) == "browser-soak-amazon"
    write_hosts(home, legacy=[l for l in LEGACY if l != "browser-access"])
    reg = kn.load_registry(home / "fleet")
    assert kn.lane_of(["browser-access", "browser-soak-amazon"], "", reg) == "browser-soak-amazon"


def test_ac115_iv_through_the_dispatcher(home, ledger):
    write_hosts(home, legacy=[l for l in LEGACY if l != "browser-access"])
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-access", "browser-soak-amazon"],
                    body="needs: browser:amazon-alex\n")
        res = _dry(conn)
        assert res.placement_caps[tid]["browser_lease_plan"] == [
            {"need": "browser:amazon-alex", "lane": "browser-soak-amazon", "class": "read",
             "identity": "id-amazon-alex", "host": "studio"}]
        assert _route(res, tid)["code"] == kbg.WAIT_JOIN_OFF


def test_ac115_v_worker_minted_card_is_refused_creator_provenance(home, ledger):
    """Pass-3 R4: a worker can set ``assignee`` to the policy profile; provenance refuses it."""
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"],
                    body="needs: browser:amazon-alex\n",
                    minted_by={"class": "worker", "task": "t_00000001", "run_id": 7})
        ok = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"],
                   body="needs: browser:amazon-alex\n", minted_by={"class": "gateway"})
        res = _dry(conn)
        ref = res.placement_caps[tid]["browser_refused"]
        assert ref["token"] == "BROWSER_CLASS_REFUSED" and "reason=creator_provenance" in ref["detail"]
        assert "t_00000001" in ref["detail"]
        assert res.placement_caps[ok]["browser_lease_plan"][0]["class"] == "read"


def test_provenance_is_the_kernels_created_event(home, ledger, monkeypatch):
    """The provenance read is the kernel's own ``created.minted_by`` (kanban_db._minted_by), not a label."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "41")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="w", assignee=SOAK_PROFILE, body="needs: browser:amazon-alex\n",
                             skills=["browser-soak-amazon"])
        mb = kbg.minted_by_of(conn, [tid])[tid]
        assert mb["class"] == "worker" and mb["task"] == "t_deadbeef"


def test_ephemeral_lane_plans_a_ci_box_lease_for_an_ace_ai_worker(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, skills=["browser-phase1-ephemeral"],
                    body='host: ace-ai\nneeds: [{kind: resource, name: "browser:ephemeral"}]\n')
        res = _dry(conn)
        assert res.placement_caps[tid]["browser_lease_plan"] == [
            {"need": "browser:ephemeral", "lane": "browser-phase1-ephemeral", "class": "act", "host": "ci-box"}]
        assert ledger.calls == []           # an ephemeral holds no identity


def test_join_off_never_spawns_a_lease_plan_on_a_legacy_profile(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"],
                    body="needs: browser:amazon-alex\n")
        res, spawned = _live(conn)
        assert tid not in spawned and res.placement_waits[tid] == kbg.WAIT_JOIN_OFF


def test_unsealed_and_unprovisioned(home, monkeypatch):
    led = Ledger(rows=[dict(AMAZON, sealed_at=None)])
    monkeypatch.setattr(kbd, "_browser_lookup", lambda: led)
    with kb.connect_closing() as conn:
        uns = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        unp = _card(conn, assignee=SOAK_PROFILE, skills=["browser-phase1-read"], body="needs: browser:ebay-ace\n")
        res = _dry(conn)
        assert res.placement_caps[uns]["legacy_route"]["reason"] == "unsealed"
        assert res.placement_caps[unp]["browser_refused"]["token"] == "BROWSER_IDENTITY_UNPROVISIONED"


def test_ledger_unreadable_holds_ready(home, monkeypatch):
    def lookup(name):
        raise kbg.LedgerUnreadable("socket")
    monkeypatch.setattr(kbd, "_browser_lookup", lambda: lookup)
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res, spawned = _live(conn)
        assert tid not in spawned and res.placement_waits[tid] == kbg.WAIT_LEDGER
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "ready"


# -- AC-1.13 dispatcher half ----------------------------------------------------------------

def test_ac113_mode_off_is_today_and_flip_needs_no_restart(home, ledger):
    write_hosts(home, mode="off")
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon", "amazon-cart-ops-local"],
                    body="needs: browser:amazon-alex\n")
        off = _dry(conn)
        assert off.placement_caps[tid]["legacy_route"]["reason"] == "mode_off"
        assert off.placement_affinity[tid]["rule"] == "studio_bound_skill"
        assert "browser_lease_plan" not in off.placement_caps[tid]
        assert ledger.calls == []          # no browserd call of any kind while off
        write_hosts(home, mode="on")        # same process, no restart
        on = _dry(conn)
        assert on.placement_caps[tid]["browser_lease_plan"][0]["identity"] == "id-amazon-alex"
        assert ledger.calls == ["id-amazon-alex"]
        write_hosts(home, mode="off")
        assert _dry(conn).placement_caps[tid]["legacy_route"]["reason"] == "mode_off"


# -- pass-3 lens: one code path ---------------------------------------------------------------

def test_dry_run_and_live_tick_call_the_same_placement_function(home, ledger, monkeypatch):
    seen = []
    real = kbg.gate_rows

    def spy(rows, **kw):
        seen.append(sorted(r["id"] for r in rows))
        return {r["id"]: kbg.CardVerdict(refusal=kn.refusal("BROWSER_LEDGER_UNREACHABLE", "spy"),
                                          refusal_payload={"token": "BROWSER_LEDGER_UNREACHABLE"}) for r in rows}
    with kb.connect_closing() as conn:
        tid = _card(conn, skills=["ebay-login"], body="needs: browser:ebay-ace\n")
        before = _dry(conn)
        assert before.placement_caps[tid]["legacy_route"]["reason"] == "lane_not_migrated"
        monkeypatch.setattr(kbg, "gate_rows", spy)
        after = _dry(conn)
        assert after.placement_caps[tid]["browser_refused"]["token"] == "BROWSER_LEDGER_UNREACHABLE"
        res, _ = _live(conn)
        assert seen == [[tid], [tid]]       # the dry run AND the live tick went through the patched function
        monkeypatch.setattr(kbg, "gate_rows", real)


def test_a_card_without_a_browser_need_is_untouched(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="fix a typo\n")
        res = _dry(conn)
        assert tid not in res.placement_caps or not any(
            k.startswith(("browser", "legacy")) for k in res.placement_caps[tid])


# -- Prism round 1 on #2002 ----------------------------------------------------------------------

def test_671171332677_gate_fault_holds_browser_cards_and_never_spawns_them(home, monkeypatch):
    bad = Ledger(rows=[dict(AMAZON, policy=json.dumps({"read": True}))])   # malformed policy shape
    monkeypatch.setattr(kbd, "_browser_lookup", lambda: bad)
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res = _dry(conn)
        assert res.placement_caps[tid]["browser_refused"]["token"] == "BROWSER_CLASS_REFUSED"  # true grants nobody

    def boom(*a, **k):
        raise TypeError("gate fault")
    monkeypatch.setattr(kbg, "gate_rows", boom)
    with kb.connect_closing() as conn:
        plain = _card(conn, body="fix a typo\n")
        res, spawned = _live(conn)
        assert tid not in spawned and res.placement_waits[tid] == kbg.WAIT_GATE_ERROR
        assert plain not in res.placement_waits or res.placement_waits[plain] != kbg.WAIT_GATE_ERROR


def test_2a0c67315e6c_a_studio_local_ephemeral_plans_the_studio(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, skills=["browser-phase1-ephemeral"], body="needs: browser:ephemeral\n")
        res = _dry(conn)
        assert res.placement_caps[tid]["browser_lease_plan"][0]["host"] == "studio"
    assert kbd._pin_host(("pin", "ace-ai")) == "ace-ai" and kbg.ephemeral_host("ace-ai") == "ci-box"


def test_5acdd292d27b_unassigned_card_is_judged_as_the_default_assignee(home, ledger):
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=None, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1, max_spawn=64, spawn_limit=4,
                                spillover=_pool(), reconcile_orphans=False, default_assignee=SOAK_PROFILE)
        assert "browser_refused" not in res.placement_caps[tid]
        assert res.placement_caps[tid]["browser_lease_plan"][0]["identity"] == "id-amazon-alex"
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "ready"
