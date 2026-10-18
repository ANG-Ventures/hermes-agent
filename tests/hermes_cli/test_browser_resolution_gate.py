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
_REAL_SPAWN = kbd._default_spawn   # captured before the autouse fixture replaces it
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
    # Hermetic: never the real host-local registry (/usr/local/etc/browsers on the Studio); tests that need one
    # point HOST_LOCAL_REGISTRY at a temp file (t_458bbb42).
    monkeypatch.setattr(kbg, "HOST_LOCAL_REGISTRY", {})
    monkeypatch.setattr(kbg, "_fallback_logged", set())
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


def _pool(name="ace-ai"):
    """The live pool's shape: a pool plan with the Studio-bound browser skills (t_8c474c2e)."""
    host = kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                        absence="required", profiles=(OTHER, SOAK_PROFILE), state="active", enabled=True,
                        priority=0)
    p = kwp.plan([host], {}, probe=lambda h: (1.0, 32))
    p.band, p.spill_reason = "paused", "load"
    p.config = kwp.PoolConfig(hosts=(name,), profiles=(OTHER, SOAK_PROFILE), pool_hosts=(host,),
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


# -- Prism round 2 on #2002 ----------------------------------------------------------------------

def test_da4f556e96bb_malformed_existing_registry_holds_browser_cards(home, ledger):
    (home / "fleet" / kn.REGISTRY_FILE).write_text("{not json")
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res, spawned = _live(conn)
        assert tid not in spawned and res.placement_waits[tid] == kbg.WAIT_GATE_ERROR
    (home / "fleet" / kn.REGISTRY_FILE).unlink()
    assert kn.load_registry(home / "fleet") == kn.LaneRegistry()     # absent = documented default (mode off)


def test_1d0c5e1cc25d_review_spawn_is_gated_too(home, ledger, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True, raising=False)
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=OTHER, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        plain = _card(conn, assignee=OTHER, body="fix a typo\n")
        conn.execute("UPDATE tasks SET status='review' WHERE id IN (?,?)", (tid, plain))
        conn.commit()
        rows = conn.execute("SELECT id, assignee, no_worker FROM tasks WHERE status='review'").fetchall()
        res = kbd.DispatchResult()
        kept = kbd._browser_hold_review_rows(conn, rows, res, dry_run=False)
        assert [r["id"] for r in kept] == [plain]
        assert res.placement_waits[tid] == "browser_refused:BROWSER_CLASS_REFUSED"
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "review"   # never parked


# -- Apollo review r3 on #2002 (Prism 2a0c67315e6c / d702aef9bd19): ownership before persistence --------

def test_2a0c67315e6c_foreign_assignee_is_never_parked(home, ledger):
    """A home whose ``kanban.dispatch_profiles`` excludes the assignee must not transition the card: after a
    live tick it stays ``ready`` and carries exactly the events a foreign card with NO browser need gets (the
    gate writes nothing: no block, no refusal, no legacy_route, no browser wait). The owned twin is parked."""
    (home / "config.yaml").write_text(f"kanban:\n  dispatch_profiles: [{OTHER}]\n")
    with kb.connect_closing() as conn:
        plain = _card(conn, assignee="beta", body="fix a typo\n")
        foreign = _card(conn, assignee="beta", skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        legacy = _card(conn, assignee="beta", skills=["ebay-login"], body="needs: browser:ebay-ace\n")
        owned = _card(conn, assignee=OTHER, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res, spawned = _live(conn)

        def evts(t):
            return [(k, json.loads(p or "{}").get("code")) for k, p in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (t,))]
        for t in (foreign, legacy):
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()[0] == "ready"
            assert t not in spawned
            assert evts(t) == evts(plain)
        assert res.placement_caps[foreign]["browser_refused"]["token"] == "BROWSER_CLASS_REFUSED"  # dry verdict only
        assert res.placement_caps[foreign]["browser_foreign"] is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (owned,)).fetchone()[0] == "blocked"


# -- P1-4b s7 join (t_2828292d): kanban.browser_join takes the ci-box ephemeral at spawn ---------------

LEASE = {"ok": True, "name": "eph-ci-box-x", "host": "ci-box", "owner_card": "t", "lease_id": 9,
         "lease_token": "L" * 64, "lease_class": "act", "lease_expires_at": 1792050000.5,
         "lease_url": "wss://cdp.browsers.ace/eph-ci-box-x"}
# the lane by ``origin:`` (no attached skill: the spawn's skill check would refuse an uninstalled one)
EPH_BODY = 'origin: browser-phase1-ephemeral\nhost: ace-ai\nneeds: [{kind: resource, name: "browser:ephemeral"}]\n'


@pytest.fixture
def join(home, monkeypatch):
    (home / "config.yaml").write_text("kanban:\n  browser_join: true\n")
    calls = []

    def relay(host, card, run, source_host):
        calls.append({"host": host, "card": card, "run": run, "source_host": source_host})
        return dict(relay.reply)
    relay.reply = LEASE
    monkeypatch.setattr(kbd, "_browser_relay_create", relay)
    return calls, relay


def _live_env(conn, plan=None):
    got = {}

    def spawn(task, workspace, *, board=None, placement=None, browser_env=None):
        got[task.id] = {"placement": getattr(placement, "name", None), "env": dict(browser_env or {})}
        return 4242
    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spawn_paused="test",
                            spillover=plan if plan is not None else _pool(), reconcile_orphans=False)
    return res, got


def test_join_spawns_the_ace_ai_ephemeral_with_the_lease_env_only(home, ledger, join):
    calls, _ = join
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        dry = _dry(conn)
        assert dry.placement_caps[tid]["browser_join"]["source_host"] == "ace-ai" and calls == []   # dry: no lease
        res, got = _live_env(conn)
        assert got[tid]["placement"] == "ace-ai"
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert calls == [{"host": "ci-box", "card": tid, "run": str(run), "source_host": "ace-ai"}]
        assert got[tid]["env"] == {"BROWSER_LEASE_URL": LEASE["lease_url"], "BROWSER_LEASE_TOKEN": "L" * 64,
                                   "BROWSER_LEASE_CLASS": "act", "BROWSER_LEASE_EXPIRES_AT": "1792050000"}
        # the token is in the env and nowhere durable
        dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM task_events")]) + \
            json.dumps([tuple(r) for r in conn.execute("SELECT * FROM task_runs")])
        assert "L" * 64 not in dump
        (ev,) = _events(conn, tid, "browser_lease_joined")
        assert ev["host"] == "ci-box" and ev["source_host"] == "ace-ai" and ev["name"] == "eph-ci-box-x"


def test_join_spawns_a_ci_box_placed_ephemeral_on_ci_box(home, ledger, join):
    """t_36b46ab2: a card placed ON ci-box joins its own ci-box ephemeral (source_host ci-box), never browser_join_off."""
    calls, _ = join
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY.replace("host: ace-ai", "host: ci-box"))
        res, got = _live_env(conn, _pool("ci-box"))
        assert res.placement_waits.get(tid) != kbg.WAIT_JOIN_OFF
        assert got[tid]["placement"] == "ci-box"
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert calls == [{"host": "ci-box", "card": tid, "run": str(run), "source_host": "ci-box"}]
        assert got[tid]["env"]["BROWSER_LEASE_URL"] == LEASE["lease_url"]
        (ev,) = _events(conn, tid, "browser_lease_joined")
        assert ev["host"] == "ci-box" and ev["source_host"] == "ci-box"


def test_join_off_holds_the_same_card(home, ledger, join):
    calls, _ = join
    (home / "config.yaml").write_text("kanban:\n  browser_join: false\n")
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        res, got = _live_env(conn)
        assert tid not in got and res.placement_waits[tid] == kbg.WAIT_JOIN_OFF and calls == []


def test_join_never_takes_an_identity_lease(home, ledger, join):
    calls, _ = join
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=SOAK_PROFILE, skills=["browser-soak-amazon"], body="needs: browser:amazon-alex\n")
        res, got = _live_env(conn)
        assert tid not in got and res.placement_waits[tid] == kbg.WAIT_JOIN_OFF and calls == []


def test_join_refusal_releases_the_claim_and_never_spawns(home, ledger, join):
    calls, relay = join
    relay.reply = {"ok": False, "refusal": "BROWSER_DISPATCH_PEER_UNKNOWN"}
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        res, got = _live_env(conn)
        assert tid not in got and len(calls) == 1
        row = conn.execute("SELECT status, consecutive_failures, claim_lock FROM tasks WHERE id=?", (tid,)).fetchone()
        assert tuple(row) == ("ready", 0, None)          # infrastructure: the breaker is never charged
        assert [e["token"] for e in _events(conn, tid, "browser_dispatch_refused")] == ["BROWSER_DISPATCH_PEER_UNKNOWN"]
        assert res.placement_waits[tid] == "browser_refused:BROWSER_DISPATCH_PEER_UNKNOWN"


def test_join_refuses_a_spawn_fn_that_cannot_carry_the_lease(home, ledger, join):
    calls, _ = join
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        res, spawned = _live(conn)            # spawn(task, workspace, *, board, placement): no browser_env
        assert tid not in spawned and calls == []
        assert res.placement_waits[tid] == "browser_refused:BROWSER_JOIN_SPAWN_UNSUPPORTED"


def test_default_spawn_forwards_only_the_lease_this_spawn_took(home, monkeypatch, tmp_path):
    """An inherited BROWSER_LEASE_* never rides into a worker; the joined lease does, by the named allowlist."""
    monkeypatch.setenv("BROWSER_LEASE_TOKEN", "inherited")
    monkeypatch.setenv("BROWSER_LEASE_URL", "wss://stale")
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            self.pid = 4242
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    task = kb.Task(id="t_join_env", title="x", body=None, assignee=OTHER, status="ready", priority=0,
                   created_by=None, created_at=0, started_at=None, completed_at=None, workspace_kind="scratch",
                   workspace_path=str(tmp_path / "ws"), claim_lock=None, claim_expires=None, tenant=None)
    _REAL_SPAWN(task, str(tmp_path / "ws"))
    lease_like = ("BROWSER_LEASE_", "BROWSER_RUN_", "BROWSER_DISPATCH_")
    assert not [k for k in captured["env"] if k.startswith(lease_like)]
    lease = {"BROWSER_LEASE_URL": "wss://cdp.browsers.ace/eph", "BROWSER_LEASE_TOKEN": "T" * 64,
             "BROWSER_DISPATCH_TOKEN": "never"}
    _REAL_SPAWN(task, str(tmp_path / "ws"), browser_env=lease)
    got = {k: v for k, v in captured["env"].items() if k.startswith(lease_like)}
    assert got == {"BROWSER_LEASE_URL": "wss://cdp.browsers.ace/eph", "BROWSER_LEASE_TOKEN": "T" * 64}


# -- Prism round 1 on #2009 ------------------------------------------------------------------------

@pytest.fixture
def released(monkeypatch):
    out = []
    monkeypatch.setattr(kbd, "_browser_release_lease", lambda host, token: out.append((host, token)) or {"ok": True})
    return out


def test_d598b2a71104_a_failed_spawn_releases_the_joined_lease(home, ledger, join, released):
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)

        def spawn(task, workspace, *, board=None, placement=None, browser_env=None):
            raise OSError("exec failed")
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spawn_paused="test",
                          spillover=_pool(), reconcile_orphans=False)
        assert released == [("ci-box", "L" * 64)]
        assert [e["why"] for e in _events(conn, tid, "browser_lease_released")] == ["spawn_failed"]
        assert "L" * 64 not in json.dumps([tuple(r) for r in conn.execute("SELECT * FROM task_events")])


def test_d598b2a71104_a_spawn_that_succeeds_keeps_its_lease(home, ledger, join, released):
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        _, got = _live_env(conn)
        assert tid in got and released == []


def test_375e680b84c1_claim_lost_during_the_relay_is_never_cleaned_up_as_ours(home, ledger, join, released,
                                                                             monkeypatch):
    calls, relay = join
    state = {}

    def slow_relay(host, card, run, source_host):
        # while the relay is out, someone else takes the card: a successor run now owns it
        with kb.connect_closing() as c2:
            c2.execute("UPDATE tasks SET current_run_id = current_run_id + 1000 WHERE id=?", (card,))
            c2.commit()
            state["run"] = c2.execute("SELECT current_run_id FROM tasks WHERE id=?", (card,)).fetchone()[0]
        return dict(slow_relay.reply)
    for reply, want_release in ((LEASE, [("ci-box", "L" * 64)]), ({"ok": False, "refusal": "BROWSER_SYSTEM_OFF"}, [])):
        released.clear()
        slow_relay.reply = reply
        monkeypatch.setattr(kbd, "_browser_relay_create", slow_relay)
        with kb.connect_closing() as conn:
            tid = _card(conn, body=EPH_BODY)
            _, got = _live_env(conn)
            row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()
            assert tid not in got
            assert tuple(row) == ("running", state["run"])          # the successor's claim is untouched
            assert _events(conn, tid, "browser_dispatch_refused") == []
            assert released == want_release


def test_606e8aff45d3_failure_transition_is_fenced_to_the_run_in_its_own_txn(home):
    """The run fence is read inside _record_task_failure's write txn: a mismatched run is a no-op (None)."""
    with kb.connect_closing() as conn:
        tid = _card(conn, body="x\n")
        claimed = kb.claim_task(conn, tid, ttl_seconds=60)
        run = claimed.current_run_id
        assert kbd._record_task_failure(conn, tid, "e", outcome="spawn_failed", release_claim=True, end_run=True,
                                        infrastructure=True, expected_run_id=run + 1) is None
        row = conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row[0] == "running" and row[1] == run and row[2] is not None
        assert kbd._record_task_failure(conn, tid, "e", outcome="spawn_failed", release_claim=True, end_run=True,
                                        infrastructure=True, expected_run_id=run) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "ready"


# -- t_458bbb42: the dispatcher reads the HOST-LOCAL registry browserd reads, not the repo seed ----------------

def _hosts_file(path: Path, mode: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"browsers": {"mode": mode, "migrated_lanes": PHASE1, "legacy_allowlist": LEGACY}}))
    return path


@pytest.mark.parametrize("present,expect", [
    (("config", "host_local", "repo"), "config"),
    (("host_local", "repo"), "host_local"),
    (("config", "repo"), "config"),
    (("repo",), "fallback"),
    ((), "fallback"),
])
def test_registry_resolution_order(tmp_path, monkeypatch, present, expect):
    files = {"config": tmp_path / "cfg" / "bh.json", "host_local": tmp_path / "etc" / "bh.json",
             "repo": tmp_path / "fleet" / kn.REGISTRY_FILE}
    for k in present:
        _hosts_file(files[k], "on")
    monkeypatch.setattr(kbg, "HOST_LOCAL_REGISTRY", {"darwin": str(files["host_local"])})
    path, source = kbg.registry_path(tmp_path / "fleet", str(files["config"]), platform="darwin")
    assert source == expect
    assert path == (files["repo"] if expect == "fallback" else files[expect])
    # unset knob and a blank knob both fall through to the host-local default
    for knob in (None, "", "  "):
        assert kbg.registry_path(tmp_path / "fleet", knob, platform="darwin")[1] == \
            ("host_local" if "host_local" in present else "fallback")
    # an OS with no default (win32) never reads a host-local path
    assert kbg.registry_path(tmp_path / "fleet", None, platform="win32")[1] == "fallback"


def test_fallback_is_logged_once_per_process_and_path(tmp_path, monkeypatch):
    monkeypatch.setattr(kbg, "HOST_LOCAL_REGISTRY", {"linux": str(tmp_path / "absent.json")})
    monkeypatch.setattr(kbg, "_fallback_logged", set())
    _hosts_file(tmp_path / "fleet" / kn.REGISTRY_FILE, "off")
    lines = []

    class Log:
        def warning(self, fmt, *a):
            lines.append(fmt % a)
    for _ in range(3):
        assert kbg.load(tmp_path / "fleet", None, platform="linux", log=Log()).mode == "off"
    assert len(lines) == 1 and lines[0].startswith(kbg.FALLBACK_EVENT + ":") and "absent.json" in lines[0]
    # the host-local file appears: no fallback line, its mode wins
    _hosts_file(tmp_path / "absent.json", "on")
    assert kbg.load(tmp_path / "fleet", None, platform="linux", log=Log()).mode == "on" and len(lines) == 1


def test_dispatcher_reads_host_local_mode_over_the_repo_seed(home, ledger, monkeypatch):
    """The repo seed says off (the committed default); the host-local copy says on -> the gate plans a lease.
    Flipping the HOST-LOCAL file off is the kill switch: the next tick is legacy_route, no restart."""
    write_hosts(home, mode="off")
    local = _hosts_file(home.parent / "etc" / "browser-hosts.json", "on")
    monkeypatch.setattr(kbg, "HOST_LOCAL_REGISTRY", {"darwin": str(local), "linux": str(local)})
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        res = _dry(conn)
        assert "browser_lease_plan" in res.placement_caps[tid], res.placement_caps[tid]
        _hosts_file(local, "off")
        res = _dry(conn)
        assert res.placement_caps[tid]["legacy_route"]["reason"] == "mode_off"


def test_root_config_browser_hosts_file_wins(home, ledger, monkeypatch):
    write_hosts(home, mode="off")
    local = _hosts_file(home.parent / "etc" / "browser-hosts.json", "off")
    monkeypatch.setattr(kbg, "HOST_LOCAL_REGISTRY", {"darwin": str(local), "linux": str(local)})
    knob = _hosts_file(home.parent / "knob" / "browser-hosts.json", "on")
    (home / "config.yaml").write_text(f"kanban:\n  browser_hosts_file: {knob}\n")
    with kb.connect_closing() as conn:
        tid = _card(conn, body=EPH_BODY)
        assert "browser_lease_plan" in _dry(conn).placement_caps[tid]
