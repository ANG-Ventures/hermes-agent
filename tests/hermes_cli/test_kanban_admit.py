"""``hermes kanban admit <card> --host <host> --reason`` (t_32e8796a, FWA).

One ready card onto one host now, past the LoadGate and the host admission
band, never past eligibility, the measured-danger floor, the operator token
or the kill switch; every admission writes an ``admission_override`` event.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_admit as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp

TOKEN = "op-token-test"
GATE = {"enabled": True, "pause_above": 64, "resume_below": 48, "max_spawn_per_tick": 16, "ecore_gate": "off"}
LOCAL_NAME = "admit-test-box"     # this machine's hostname in every test, whatever venue runs it
POOL_HOST = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                         absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
POOL = kwp.PoolConfig(hosts=("ace-ai",), profiles=("alpha",), pool_hosts=(POOL_HOST,))


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Pin the local identity: kwp._studio_words() and the claim lock read the
    # hostname, so on a pool host (ace-ai) `local` would otherwise BE that host.
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: LOCAL_NAME)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "apollo", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"dispatch_load_gate": dict(GATE)}})
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (home / "kanban").mkdir(exist_ok=True)
    kb.operator_token_path().write_text(TOKEN)
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, TOKEN)
    # Never a real worker or a real ssh probe.
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real spawn")))
    monkeypatch.setattr(ka, "_pool_and_signal", lambda cfg: (POOL, None, home / "fleet"))
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


SPAWNS: list = []


def _spawn(task, workspace, *, board=None, placement=None):
    SPAWNS.append((task.id, getattr(placement, "name", None)))
    return 4242


def _card(body="") -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="fx", assignee="alpha", body=body)


def _status(tid):
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def _events(tid, kind=ka.EVENT):
    with kb.connect_closing() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()]


def _hot_local(monkeypatch, *, load1=66.0, mem=100.0, threads=64):
    """The Studio's LoadGate PAUSED (load1 66 > pause_above 64), memory and threads as given."""
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (load1, load1))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.99, None))
    monkeypatch.setattr(klg, "utilization_context", lambda: {"mem_available_gb": mem})
    monkeypatch.setattr(ka.os, "cpu_count", lambda: threads)


def _remote(band="red", hot=True, mem=100.0, load1=10.0, threads=32):
    def fn(host, signal):
        return ka.Measure(host.name, load1, threads, mem, 8.0, extra={"band": band, "hot": hot, "ncpu": threads})
    return fn


def test_admit_spawns_on_a_host_whose_band_is_hot_and_writes_the_event(board, monkeypatch):
    _hot_local(monkeypatch)
    tid = _card()
    SPAWNS.clear()
    res = ka.admit(tid, "mac-studio", "Ace 21:3x: do both", spawn_fn=_spawn)
    assert res["host"] == "studio" and res["pid"] == 4242
    assert SPAWNS == [(tid, None)] and _status(tid) == "running"
    [ev] = _events(tid)
    assert ev["actor"] == "apollo" and ev["host"] == "studio" and ev["reason"] == "Ace 21:3x: do both"
    assert ev["band"] == "paused" and ev["gate_allowance"] == 0      # what it overrode
    assert ev["load1"] == 66.0 and ev["load_limit"] == 96.0 and ev["mem_reserve_gb"] == 0.0


def test_admit_spawns_on_a_hot_pool_host_and_places_it_there(board, monkeypatch):
    tid = _card()
    SPAWNS.clear()
    res = ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    assert res["host"] == "ace-ai" and SPAWNS == [(tid, "ace-ai")]
    [ev] = _events(tid)
    assert ev["band"] == "red" and ev["hot"] is True
    [placed] = _events(tid, kb._kwh.PLACED_EVENT)
    assert placed["host"] == "ace-ai" and placed["reason"] == "admission_override"


def test_admit_refuses_an_ineligible_host(board, monkeypatch):
    _hot_local(monkeypatch)
    pinned = _card(body="host: ace-ai\n")
    with pytest.raises(ka.AdmitRefused, match="studio is not eligible.*eligible: ace-ai"):
        ka.admit(pinned, "studio", "ruling", spawn_fn=_spawn)
    studio_only = _card(body="host: studio\n")
    with pytest.raises(ka.AdmitRefused, match="ace-ai is not eligible.*eligible: studio"):
        ka.admit(studio_only, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    with pytest.raises(ka.AdmitRefused, match="nope is not eligible"):
        ka.admit(_card(), "nope", "ruling", spawn_fn=_spawn)
    assert _status(pinned) == _status(studio_only) == "ready" and not _events(pinned)


def test_admit_refuses_under_the_measured_floor_and_prints_number_and_threshold(board, monkeypatch):
    tid = _card()
    with pytest.raises(ka.AdmitRefused, match=r"memory available 3\.0 GB < mem_reserve_gb 8 GB"):
        ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote(mem=3.0))
    with pytest.raises(ka.AdmitRefused, match=r"load1 49\.0 > threads 32 x 1\.5 = 48"):
        ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote(load1=49.0))
    with pytest.raises(ka.AdmitRefused, match="memory available is unmeasured"):
        ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote(mem=None))
    _hot_local(monkeypatch, load1=100.0, threads=64)
    with pytest.raises(ka.AdmitRefused, match=r"load1 100\.0 > threads 64 x 1\.5 = 96"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert _status(tid) == "ready" and not _events(tid)


def test_admit_refuses_a_running_card_naming_its_host(board, monkeypatch):
    tid = _card()
    ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    with pytest.raises(ka.AdmitRefused, match="already running on ace-ai"):
        ka.admit(tid, "ace-ai", "again", spawn_fn=_spawn, measure_remote_fn=_remote())
    assert len(_events(tid)) == 1


def test_admit_refuses_a_locally_running_card_naming_the_canonical_host(board, monkeypatch):
    _hot_local(monkeypatch)
    tid = _card()
    ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).claim_lock.startswith(LOCAL_NAME + ":")
    with pytest.raises(ka.AdmitRefused, match=r"already running on studio$"):
        ka.admit(tid, "studio", "again", spawn_fn=_spawn)


def test_admit_refuses_a_second_writer_on_a_pr_another_card_runs(board, monkeypatch):
    """Override admission, never ownership (Prism 416e418c6ee1): card A running
    on a PR holds it, so admit of card B naming the same PR refuses, owner named."""
    from hermes_cli import kanban_pr_owner as kpo
    monkeypatch.setattr(kpo, "pr_head_committed_at", lambda repo, n, **kw: None)
    _hot_local(monkeypatch)
    pr = "ANG-Ventures/hermes-agent#1624"
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title=f"parity sync {pr}", assignee="alpha")
        assert kb.claim_task(conn, a)
        b = kb.create_task(conn, title=f"fix red slice on {pr}", assignee="alpha")
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match=rf"pr_owner_busy: .*#1624 is owned by running card {a}"):
        ka.admit(b, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _status(b) == "ready" and not _events(b)


def test_floor_refuses_non_finite_measurements():
    nan, inf = float("nan"), float("inf")
    assert "unmeasured" in ka.floor_refusal(ka.Measure("h", nan, 8, nan, 4.0))
    assert "unmeasured" in ka.floor_refusal(ka.Measure("h", 1.0, 8, inf, 4.0))
    assert "load1/threads unmeasured" in ka.floor_refusal(ka.Measure("h", nan, 8, 50.0, 4.0))
    assert "load1/threads unmeasured" in ka.floor_refusal(ka.Measure("h", 1.0, -4, 50.0, 4.0))
    assert "not a finite threshold" in ka.floor_refusal(ka.Measure("h", 1.0, 8, 50.0, nan))
    assert ka.floor_refusal(ka.Measure("h", 1.0, 8, 50.0, 4.0)) is None


def test_admit_refuses_when_the_workspace_admission_refuses(board, monkeypatch):
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

    _hot_local(monkeypatch)
    tid = _card()

    def refuse(task, board=None, conn=None, **kw):
        raise WorkspaceUnavailable("workspaces_root_unmounted: /Volumes/x")

    monkeypatch.setattr(kb, "_validate_workspace_admission", refuse)
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="workspace unavailable: workspaces_root_unmounted"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _status(tid) == "ready"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).claim_lock is None


def test_a_local_admit_lowers_the_next_gateway_tick_allowance_by_one(board, monkeypatch):
    """The gateway tick seeds its ramp with LOCAL runs it did not spawn (an
    admit, a one-shot dispatch), so a local admit counts against the host's
    budget on the very next tick, before its CPU shows in load1."""
    from gateway import kanban_gate_tick as kgt

    _hot_local(monkeypatch, load1=40.0)        # (64 - 40) / cost 2 = 12 admitted, under the cap
    monkeypatch.setattr(klg, "sample_user_procs", lambda: (None, None))
    cfg = {"dispatch_load_gate": dict(GATE)}

    def gateway_allowance():
        gate = klg.gate_from_config({"kanban": cfg})
        builder = kgt.standalone_builder(gate, lambda: cfg)
        with monkeypatch.context() as m:
            m.setattr(kb, "_claimer_id", lambda: "gateway-box:999")   # the gateway is another process
            return builder.build(kgt.live_boards()).local_allowance

    before = gateway_allowance()
    ka.admit(_card(), "studio", "ruling", spawn_fn=_spawn)
    after = gateway_allowance()
    assert (before, after) == (12, 11)


def test_the_gateway_tick_does_not_rebook_its_own_spawns_or_a_seeded_run_twice(board, monkeypatch):
    from gateway import kanban_gate_tick as kgt

    _hot_local(monkeypatch, load1=40.0)
    tid = _card()
    ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    gate = klg.gate_from_config({"kanban": {"dispatch_load_gate": dict(GATE)}})
    builder = kgt.standalone_builder(gate, lambda: {})
    boards = kgt.live_boards()
    assert builder.seed_out_of_tick(boards) == 0                   # claimed by this process: already booked
    monkeypatch.setattr(kb, "_claimer_id", lambda: "gateway-box:999")
    assert builder.seed_out_of_tick(boards) == 1
    assert builder.seed_out_of_tick(boards) == 0                   # once per run
    assert gate.ramp_workers() == 1


def test_admit_refuses_a_draining_pool_host(board, monkeypatch):
    import dataclasses

    draining = dataclasses.replace(POOL_HOST, state="draining")
    pool = dataclasses.replace(POOL, pool_hosts=(draining,))
    monkeypatch.setattr(ka, "_pool_and_signal", lambda cfg: (pool, None, board / "fleet"))
    tid = _card()
    with pytest.raises(ka.AdmitRefused, match="ace-ai is not eligible"):
        ka.admit(tid, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    assert _status(tid) == "ready"


def test_a_stale_remote_pressure_sample_is_unmeasured_memory(monkeypatch):
    from types import SimpleNamespace

    sample = kwp.HostSample(load1=1.0, ncpu=32, remote_now=2000.0,
                            pressure_text=json.dumps({"at": 1000.0, "mem_available_gb": 100.0}))
    monkeypatch.setattr(kwp, "probe_host", lambda host, **kw: sample)
    signal = SimpleNamespace(policy=SimpleNamespace(pressure_path="/x", stale_after_s=60.0),
                             placement_table=None, state_path=Path("/nonexistent/state.json"))
    m = ka.measure_remote(POOL_HOST, signal)
    assert m.mem_available_gb is None and "unmeasured" in ka.floor_refusal(m)


def test_an_unreadable_local_placement_table_refuses_instead_of_a_zero_reserve(board, monkeypatch):
    from hermes_cli import placement as pl

    _hot_local(monkeypatch, load1=10.0)
    table = pl.fleet_root() / pl.TABLE_REL
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("{not json")
    with pytest.raises(ka.AdmitRefused, match="not a finite threshold"):
        ka.admit(_card(), "studio", "ruling", spawn_fn=_spawn)


def test_an_invalid_remote_placement_table_refuses_instead_of_a_zero_reserve(board, monkeypatch):
    """Prism 6e8b5b41758b: kh.load_table() returns None for an INVALID table
    too; the remote floor must not read that as a 0 GB reserve."""
    from types import SimpleNamespace

    from hermes_cli import placement as pl

    table = pl.fleet_root() / pl.TABLE_REL
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("{not json")
    sample = kwp.HostSample(load1=1.0, ncpu=32, remote_now=2000.0,
                            pressure_text=json.dumps({"at": 2000.0, "mem_available_gb": 100.0}))
    monkeypatch.setattr(kwp, "probe_host", lambda host, **kw: sample)
    signal = SimpleNamespace(policy=SimpleNamespace(pressure_path="/x", stale_after_s=60.0),
                             placement_table=None, state_path=Path("/nonexistent/state.json"))
    m = ka.measure_remote(POOL_HOST, signal)
    assert m.mem_available_gb == 100.0
    refusal = ka.floor_refusal(m)
    assert refusal is not None and "not a finite threshold" in refusal and str(table) in refusal
    # An ABSENT table is still "no reserve configured": 0 GB, admits.
    table.unlink()
    assert ka.floor_refusal(ka.measure_remote(POOL_HOST, signal)) is None


def test_admit_applies_the_active_lane_model_override_card_pin_wins(board, monkeypatch):
    """Prism edcd308fa63f / cbdc23241724: an admitted card spawns on the lane
    route like a tick spawn; a card's own override beats it; an expired lane
    row is ignored."""
    import time as _time

    _hot_local(monkeypatch)
    seen = {}

    def spawn(task, workspace, *, board=None, placement=None):
        seen[task.id] = (task.model_override, task.provider_override)
        return 4242

    now = int(_time.time())
    with kb.connect_closing() as conn:
        kb.set_lane_model_override(conn, provider="lane-prov", model="lane-model",
                                   expires_at=now + 3600, reason="capacity", assignee="alpha")
    lane = _card()
    ka.admit(lane, "studio", "ruling", spawn_fn=spawn)
    assert seen[lane] == ("lane-model", "lane-prov")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, lane).model_override is None       # never persisted onto the card
        [route] = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='dispatch_lane_route'", (lane,))]
    assert route == {"provider": "lane-prov", "model": "lane-model"}

    pinned = _card()
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET model_override='card-model', provider_override='card-prov' WHERE id=?",
                     (pinned,))
    ka.admit(pinned, "studio", "ruling", spawn_fn=spawn)
    assert seen[pinned] == ("card-model", "card-prov")

    with kb.connect_closing() as conn:
        kb.set_lane_model_override(conn, provider="lane-prov", model="lane-model",
                                   expires_at=now - 1, reason="capacity", assignee="alpha", now=now - 100)
    expired = _card()
    ka.admit(expired, "studio", "ruling", spawn_fn=spawn)
    assert seen[expired] == (None, None)


def test_the_board_owner_root_config_kill_switch_refuses(board, monkeypatch):
    _hot_local(monkeypatch)
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {"admin_admit": {"enabled": False}})
    tid = _card()
    with pytest.raises(ka.AdmitRefused, match="kanban.admin_admit.enabled: false"):
        ka.admit(tid, "studio", "ruling", kanban_cfg={}, spawn_fn=_spawn)
    assert _status(tid) == "ready"


def test_admit_refuses_what_the_browser_gate_would_hold(board, monkeypatch):
    """The tick's own browser gate runs (not a dry copy): its wait refuses."""
    _hot_local(monkeypatch)
    seen = {}

    def gate(conn, rows, result, route, dry_run, assignee=None, *, park=True, join=True):
        seen.update(rows=[r["id"] for r in rows])
        route[rows[0]["id"]] = ("wait", "browser_join_off")

    monkeypatch.setattr(kbd, "_browser_gate", gate)
    tid = _card()
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="studio is not eligible.*wait: browser_join_off"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _status(tid) == "ready" and seen["rows"] == [tid]


def test_no_pool_still_runs_the_dispatchers_local_routing(board, monkeypatch):
    _hot_local(monkeypatch)
    monkeypatch.setattr(ka, "_pool_and_signal", lambda cfg: (None, None, board / "fleet"))
    monkeypatch.setattr(kbd, "_route_row", lambda conn, row, plan, *a, **k: ("wait", "capability:gpu"))
    tid = _card()
    with pytest.raises(ka.AdmitRefused, match="studio is not eligible.*wait: capability:gpu"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert _status(tid) == "ready"


def test_kill_switch_off_refuses(board, monkeypatch):
    _hot_local(monkeypatch)
    tid = _card()
    with pytest.raises(ka.AdmitRefused, match="kanban.admin_admit.enabled: false"):
        ka.admit(tid, "studio", "ruling", kanban_cfg={"admin_admit": {"enabled": False}}, spawn_fn=_spawn)
    assert _status(tid) == "ready"


def test_operator_token_required(board, monkeypatch):
    _hot_local(monkeypatch)
    tid = _card()
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV)
    with pytest.raises(ka.AdmitRefused, match="operator-only: KANBAN_OPERATOR_TOKEN absent"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "wrong")
    with pytest.raises(ka.AdmitRefused, match="mismatch"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert _status(tid) == "ready"


def test_cli_verb_is_wired_with_no_bulk_form(board, monkeypatch, capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kb_cli.build_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(["kanban", "admit", "t_x", "--host", "studio", "--reason", "ruling"])
    assert (ns.kanban_action, ns.task_id, ns.host, ns.reason) == ("admit", "t_x", "studio", "ruling")
    for bulk in (["--max", "3"], ["t_y"]):          # one card, no --max
        with pytest.raises(SystemExit):
            root.parse_args(["kanban", "admit", "t_x", *bulk, "--host", "studio", "--reason", "r"])
    capsys.readouterr()
    assert kb_cli._HANDLERS["admit"] is ka.cmd_admit
    monkeypatch.setattr(ka, "admit", lambda t, h, r: (_ for _ in ()).throw(ka.AdmitRefused(f"{t}: test refusal")))
    assert ka.cmd_admit(ns) == 1
    assert "kanban admit: REFUSED t_x: test refusal" in capsys.readouterr().err


# -- r5 (Apollo shape ruling): admit IS the tick's ready lane, minus host admission --

def _claim(tid):
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
        return t.status, t.claim_lock


def test_a_board_over_its_budget_ceiling_refuses_admit_like_dispatch(board, monkeypatch):
    """Prism c4eed7905e67: the board's usd_per_24h ceiling binds admit with the
    reason dispatch gives (budget_paused)."""
    from hermes_cli import kanban_budget as kbudget

    _hot_local(monkeypatch)
    monkeypatch.setattr(kbudget, "configured_ceiling_usd", lambda: 1.0)
    monkeypatch.setattr(kbudget, "board_spend_usd", lambda *a, **k: 5.0)
    monkeypatch.setattr(kbudget, "_page_paused", lambda *a, **k: None)
    tid = _card()
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="budget_paused"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _claim(tid) == ("ready", None) and not _events(tid)
    with kb.connect_closing() as conn:                       # the tick holds it the same way
        assert kbd.dispatch_once(conn, spawn_fn=_spawn).budget_paused is True
    assert SPAWNS == []


def test_a_raising_lane_route_write_releases_the_claim(board, monkeypatch):
    """Prism 739dc0878823: the claim and every write after it are guarded; a
    raising dispatch_lane_route write leaves the card ready with no live claim."""
    import time as _time

    _hot_local(monkeypatch)
    with kb.connect_closing() as conn:
        kb.set_lane_model_override(conn, provider="lane-prov", model="lane-model",
                                   expires_at=int(_time.time()) + 3600, reason="capacity", assignee="alpha")
    real = kb._append_event

    def append(conn, task_id, kind, payload=None, **kw):
        if kind == "dispatch_lane_route":
            raise RuntimeError("disk full")
        return real(conn, task_id, kind, payload, **kw)

    monkeypatch.setattr(kb, "_append_event", append)
    tid = _card()
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="spawn failed"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _claim(tid) == ("ready", None)
    with kb.connect_closing() as conn:
        run = conn.execute("SELECT outcome, ended_at FROM task_runs WHERE task_id=? ORDER BY id DESC",
                           (tid,)).fetchone()
    assert run[0] == "spawn_failed" and run[1] is not None


def _gate_triggers():
    """One trigger per ready-lane gate: (setup(monkeypatch, tid, ctx) -> host, expected refusal regex).
    ``ctx`` carries kanban_cfg / spawn_fn overrides for the admit call."""
    from hermes_cli import kanban_budget as kbudget
    from hermes_cli import kanban_pause_hold as kph
    from hermes_cli import kanban_provider_health as kph_health
    from hermes_cli import model_policy, model_registry
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

    def budget(m, tid, ctx):
        m.setattr(kbudget, "evaluate_board_budget", lambda *a, **k: True)
        return "studio", "budget_paused"

    def max_spawn(m, tid, ctx):
        with kb.connect_closing() as conn:
            assert kb.claim_task(conn, kb.create_task(conn, title="other", assignee="alpha"))
        ctx["kanban_cfg"] = {"max_spawn": 1}
        return "studio", "spawn_capped: max_spawn=1"

    def no_worker(m, tid, ctx):
        with kb.connect_closing() as conn:
            conn.execute("UPDATE tasks SET no_worker=1 WHERE id=?", (tid,))
        return "studio", "no_worker"

    def pause_hold(m, tid, ctx):
        m.setattr(kph, "held_routes", lambda rows, cache: {tid: ("wait", "pause_hold (test)")})
        return "studio", "pause_hold"

    def route(m, tid, ctx):
        m.setattr(kbd, "_route_row", lambda conn, row, plan, *a, **k: ("wait", "capability:gpu"))
        return "studio", "wait: capability:gpu"

    def browser(m, tid, ctx):
        def gate(conn, rows, result, rr, dry_run, *a, **k):
            for r in rows:
                rr[r["id"]] = ("wait", "browser_join_off")
        m.setattr(kbd, "_browser_gate", gate)
        return "studio", "browser_join_off"

    def assignee(m, tid, ctx):
        with kb.connect_closing() as conn:
            conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (tid,))
        return "studio", "unassigned"

    def profile(m, tid, ctx):
        m.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: False))
        return "studio", "not a spawnable profile"

    def per_profile_cap(m, tid, ctx):
        with kb.connect_closing() as conn:
            assert kb.claim_task(conn, kb.create_task(conn, title="other", assignee="alpha"))
        ctx["kanban_cfg"] = {"max_in_progress_per_profile": 1}
        return "studio", "per_profile_cap: alpha"

    def flagship(m, tid, ctx):
        with kb.connect_closing() as conn:
            conn.execute("UPDATE tasks SET model_override='big-model' WHERE id=?", (tid,))
        m.setattr(model_policy, "flagship_model_match", lambda model, cfg=None: True)
        return "studio", "flagship"

    def invalid_pin(m, tid, ctx):
        with kb.connect_closing() as conn:
            conn.execute("UPDATE tasks SET model_override='m', provider_override='prov' WHERE id=?", (tid,))
        m.setattr(model_registry, "invalid_pin_reason", lambda provider, model: "unknown model m")
        return "studio", "blocked by the dispatcher"

    def respawn_guard(m, tid, ctx):
        m.setattr(kbd, "check_respawn_guard", lambda conn, task_id, **k: "rate_limit_cooldown")
        return "studio", "rate_limit_cooldown"

    def provider_admission(m, tid, ctx):
        with kb.connect_closing() as conn:
            conn.execute("UPDATE tasks SET model_override='m', provider_override='prov' WHERE id=?", (tid,))
        m.setattr(model_registry, "invalid_pin_reason", lambda provider, model: None)
        m.setattr(kb, "cooling_providers", lambda conn, **k: {"prov": 9999999999})
        m.setattr(kph_health, "available_profile_fallback", lambda *a, **k: None)
        return "studio", "credential_cooldown"

    def card_skills(m, tid, ctx):
        def refused(conn, task_id, assignee, result, *, dry_run):
            result.skill_refused.append((task_id, ["missing-skill"]))
            return True
        m.setattr(kb, "_card_skills_refused", refused)
        return "studio", "card skills refused: missing-skill"

    def workspace_admission(m, tid, ctx):
        def refused(conn, task_id, result, *, board, dry_run):
            result.workspace_refused.append((task_id, "workspaces_root_unmounted"))
            return True
        m.setattr(kb, "_workspace_admission_refused", refused)
        return "studio", "workspace unavailable: workspaces_root_unmounted"

    def placement(m, tid, ctx):
        m.setattr(kwp, "board_fault", lambda host, board, **k: "board root unmounted")
        ctx["measure_remote_fn"] = _remote()
        return "ace-ai", "ace-ai is not eligible.*board_unhostable"

    def claim(m, tid, ctx):
        m.setattr(kb, "claim_task", lambda conn, task_id, **k: None)
        return "studio", "did not spawn"

    def lane_route(m, tid, ctx):
        import time as _time
        with kb.connect_closing() as conn:
            kb.set_lane_model_override(conn, provider="lane-prov", model="lane-model",
                                       expires_at=int(_time.time()) + 3600, reason="r", assignee="alpha")
        real = kb._append_event

        def append(conn, task_id, kind, payload=None, **kw):
            if kind == "dispatch_lane_route":
                raise RuntimeError("disk full")
            return real(conn, task_id, kind, payload, **kw)
        m.setattr(kb, "_append_event", append)
        return "studio", "spawn failed"

    def workspace(m, tid, ctx):
        def refuse(task, board=None, conn=None, **kw):
            if task.status == "running":                    # the post-claim check only
                raise WorkspaceUnavailable("workspaces_root_unmounted: /Volumes/x")
            return None
        m.setattr(kb, "_validate_workspace_admission", refuse)
        return "studio", "workspace unavailable"

    def launch(m, tid, ctx):
        ctx["spawn_fn"] = lambda task, workspace, *, board=None, placement=None: (_ for _ in ()).throw(
            RuntimeError("exec failed"))
        return "studio", "spawn failed"

    return {
        "budget_paused": budget, "max_spawn": max_spawn, "no_worker": no_worker, "pause_hold": pause_hold,
        "route": route, "browser": browser, "assignee": assignee, "profile": profile,
        "per_profile_cap": per_profile_cap, "flagship": flagship, "invalid_pin": invalid_pin,
        "respawn_guard": respawn_guard, "provider_admission": provider_admission,
        "card_skills": card_skills, "workspace_admission": workspace_admission, "placement": placement,
        "claim": claim, "lane_route": lane_route, "workspace": workspace, "launch": launch,
    }


def test_admit_refuses_on_every_dispatcher_gate_except_host_admission(board, monkeypatch):
    """The contract: walk the dispatcher's ready-lane gate list. admit refuses on
    each gate (nothing spawned, no live claim), and host admission is the ONE it
    skips. A gate added to kbd.READY_LANE_GATES has no trigger here, so this test
    is red until it is proven that admit holds on it too."""
    triggers = _gate_triggers()
    assert set(kbd.READY_LANE_GATES) == set(triggers) | {"host_admission"}, (
        "a dispatcher gate without an admit trigger: "
        f"{sorted(set(kbd.READY_LANE_GATES) - set(triggers) - {'host_admission'})}")
    for gate in kbd.READY_LANE_GATES:
        if gate == "host_admission":
            continue
        with monkeypatch.context() as m:
            _hot_local(m, load1=10.0)
            tid = _card()
            ctx: dict = {"spawn_fn": _spawn}
            host, expect = triggers[gate](m, tid, ctx)
            SPAWNS.clear()
            with pytest.raises(ka.AdmitRefused, match=expect):
                ka.admit(tid, host, f"gate {gate}", kanban_cfg=ctx.get("kanban_cfg", {}),
                         spawn_fn=ctx["spawn_fn"], measure_remote_fn=ctx.get("measure_remote_fn"))
            status, lock = _claim(tid)
            assert SPAWNS == [] and lock is None and status != "running", (gate, status, lock)
    # host admission: the LoadGate is PAUSED (load1 66 > 64) and the tick spawns
    # nothing, yet admit spawns.
    _hot_local(monkeypatch)
    held, tid = _card(), None
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_spawn, spawn_paused="load gate paused", spawn_limit=0)
    assert not res.spawned and _claim(held) == ("ready", None)
    SPAWNS.clear()
    assert ka.admit(held, "studio", "host admission override", spawn_fn=_spawn)["host"] == "studio"
    assert SPAWNS == [(held, None)]


def test_admit_probes_board_roots_and_refuses_a_host_without_this_board(board, monkeypatch):
    """Prism 9f96730083e6: admit's OWN path (measure_remote -> _admit_plan) asks
    the probe for kanban.workspaces_root, and a pool host whose probe lists no
    root for this board is refused by the dispatcher's board_refusal
    (no_board_root), never spawned into unprovisioned storage."""
    import time as _time
    from types import SimpleNamespace

    signal = SimpleNamespace(policy=SimpleNamespace(pressure_path="/x", stale_after_s=600.0),
                             placement_table=None, state_path=Path("/nonexistent/state.json"))
    monkeypatch.setattr(ka, "_pool_and_signal", lambda cfg: (POOL, signal, board / "fleet"))
    now = _time.time()
    asked = []

    def probe(host, **kw):
        asked.append(kw)
        return kwp.HostSample(load1=1.0, ncpu=32, remote_now=now,
                              pressure_text=json.dumps({"at": now, "mem_available_gb": 100.0}),
                              boards=frozenset({"other"}))

    monkeypatch.setattr(kwp, "probe_host", probe)
    tid = _card()
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="no_board_root"):
        ka.admit(tid, "ace-ai", "ruling", kanban_cfg={"workspaces_root": "/srv/ws"}, spawn_fn=_spawn)
    assert asked and asked[-1].get("workspaces_root") == "/srv/ws"
    assert SPAWNS == [] and _claim(tid) == ("ready", None) and not _events(tid)
    # The same probe listing this board admits.
    monkeypatch.setattr(kwp, "probe_host", lambda host, **kw: kwp.HostSample(
        load1=1.0, ncpu=32, remote_now=now,
        pressure_text=json.dumps({"at": now, "mem_available_gb": 100.0}),
        boards=frozenset({"default"})))
    assert ka.admit(tid, "ace-ai", "ruling", kanban_cfg={"workspaces_root": "/srv/ws"},
                    spawn_fn=_spawn)["host"] == "ace-ai"
    assert SPAWNS == [(tid, "ace-ai")]
