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


def _write_pool(fleet: Path, state: str = "active") -> None:
    """The pool files the dispatcher's own plan builder reads: ace-ai, 4 slots, alpha."""
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": state}}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": kwp.SSH_USER, "priority": ["ace-ai"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "required"}}}))


PROBES: list = []


def _probe(host, **kw):
    """The plan's probe (never ssh): ace-ai idle, every board hostable unless asked otherwise."""
    PROBES.append((host.name, kw))
    return kwp.HostSample(load1=1.0, ncpu=32, boards=frozenset({"default"}) if "workspaces_root" in kw else None)


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
                        lambda: {"kanban": {"dispatch_load_gate": dict(GATE), "placement": {"read_signal": False}}})
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (home / "kanban").mkdir(exist_ok=True)
    kb.operator_token_path().write_text(TOKEN)
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, TOKEN)
    # Never a real worker or a real ssh probe.
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real spawn")))
    _write_pool(kb.kanban_home() / "fleet")
    PROBES.clear()
    monkeypatch.setattr(kwp, "probe_host", _probe)
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
    _write_pool(kb.kanban_home() / "fleet", state="draining")
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
    from hermes_cli.fleet_placement import placement as pl

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

    from hermes_cli.fleet_placement import placement as pl

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
    with pytest.raises(ka.AdmitRefused, match="held: browser_join_off"):
        ka.admit(tid, "studio", "ruling", spawn_fn=_spawn)
    assert SPAWNS == [] and _status(tid) == "ready" and seen["rows"] == [tid]


def test_no_pool_plan_routes_exactly_as_a_tick_with_none(board, monkeypatch):
    """Prism d18e71bf0dca: with no plan the dispatcher's own no-plan branch
    runs (known_host_ids from the pool files), so a ``host: ace-ai`` card waits
    pool_unavailable and never admits locally; nothing is placed remotely; an
    unpinned card still admits on the Studio."""
    _hot_local(monkeypatch)
    monkeypatch.setattr(ka, "_dispatcher_plan", lambda cfg: (None, board / "fleet"))
    pinned = _card(body="host: ace-ai\n")
    SPAWNS.clear()
    for host in ("studio", "ace-ai"):        # the tick's own no-plan wait, whatever host is asked
        with pytest.raises(ka.AdmitRefused, match="held: pool_unavailable"):
            ka.admit(pinned, host, "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    assert SPAWNS == [] and _claim(pinned) == ("ready", None) and not _events(pinned)
    free = _card()
    with pytest.raises(ka.AdmitRefused, match="ace-ai is not eligible.*no pool plan: pool_unavailable"):
        ka.admit(free, "ace-ai", "ruling", spawn_fn=_spawn, measure_remote_fn=_remote())
    assert ka.admit(free, "studio", "ruling", spawn_fn=_spawn)["host"] == "studio"
    assert SPAWNS == [(free, None)]


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


def test_admit_probes_board_roots_and_refuses_a_host_without_this_board(board, monkeypatch):
    """Prism 9f96730083e6: admit's OWN path (measure_remote, inside the lock) asks
    the probe for kanban.workspaces_root, and a pool host whose probe lists no
    root for this board is refused by the dispatcher's board_refusal
    (no_board_root), never spawned into unprovisioned storage."""
    import time as _time
    from types import SimpleNamespace

    signal = SimpleNamespace(policy=SimpleNamespace(pressure_path="/x", stale_after_s=600.0),
                             placement_table=None, state_path=Path("/nonexistent/state.json"))
    real_plan = ka._dispatcher_plan

    def plan_with_signal(cfg):
        plan, fleet, *_ = real_plan(cfg)
        plan.signal = signal
        return plan, fleet

    monkeypatch.setattr(ka, "_dispatcher_plan", plan_with_signal)
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
    assert asked and all(kw.get("workspaces_root") == "/srv/ws" for kw in asked)
    assert SPAWNS == [] and _claim(tid) == ("ready", None) and not _events(tid)
    # The same probe listing this board admits.
    monkeypatch.setattr(kwp, "probe_host", lambda host, **kw: kwp.HostSample(
        load1=1.0, ncpu=32, remote_now=now,
        pressure_text=json.dumps({"at": now, "mem_available_gb": 100.0}),
        boards=frozenset({"default"})))
    assert ka.admit(tid, "ace-ai", "ruling", kanban_cfg={"workspaces_root": "/srv/ws"},
                    spawn_fn=_spawn)["host"] == "ace-ai"
    assert SPAWNS == [(tid, "ace-ai")]


def test_pool_off_switch_refuses_a_remote_admit_with_the_ticks_own_reason(board, monkeypatch):
    """Prism ddce35a5f884: ``worker_pool.enabled: false`` is the operator's pool
    off-switch, not host admission. admit's plan goes through the same
    ``pool_switch_refusal`` the tick's plan_pool applies, so a remote admit refuses
    with the tick's own reason (config_disabled) and nothing spawns; the Studio
    still admits."""
    from gateway import kanban_gate_tick as kgt
    from hermes_cli import kanban_load_gate as klg

    _hot_local(monkeypatch, load1=10.0)
    cfg = {"worker_pool": {"enabled": False}}
    tick = kgt.standalone_builder(klg.gate_from_config({"kanban": cfg}), lambda: cfg)
    assert tick.plan_pool([], {}) is None and tick.gate.pool["reason"] == "config_disabled"
    assert tick.build_pool_plan([], {}, cfg) is None and tick.gate.pool["reason"] == "config_disabled"
    tid = _card()
    SPAWNS.clear()
    with pytest.raises(ka.AdmitRefused, match="config_disabled"):
        ka.admit(tid, "ace-ai", "ruling", kanban_cfg=cfg, spawn_fn=_spawn, measure_remote_fn=_remote())
    assert SPAWNS == [] and _claim(tid) == ("ready", None) and not _events(tid)
    assert ka.admit(tid, "studio", "ruling", kanban_cfg=cfg, spawn_fn=_spawn)["host"] == "studio"
    assert SPAWNS == [(tid, None)]


def test_placement_table_under_an_unsearchable_dir_is_nan_not_zero(board, monkeypatch):
    """Prism d16d6de8bc5b: ``Path.exists()`` is False on PermissionError (py3.14),
    so a table beneath a 0o000 directory read as "no reserve". Only a missing
    file is absent; anything else is an unknown threshold that refuses, naming
    the table."""
    import math as _math
    import os as _os

    from hermes_cli.fleet_placement import placement as pl

    table = pl.fleet_root() / pl.TABLE_REL
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("{}")
    parent = table.parent
    parent.chmod(0o000)
    try:
        if _os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        threads, reserve, err = ka._host_row(None, "local", 8)
        assert _math.isnan(reserve) and err is not None and str(table) in err
        m = ka.Measure("studio", 1.0, 8, 100.0, reserve, threshold_error=err)
        refusal = ka.floor_refusal(m)
        assert refusal is not None and str(table) in refusal
    finally:
        parent.chmod(0o755)
    # A truly absent table stays "no reserve": 0 GB.
    table.unlink()
    assert ka._host_row(None, "local", 8) == (8, 0.0, None)


# -- r7 (Apollo invariant): admit == the dispatcher's own route and refusal, host admission off --

def _decision_sites() -> set:
    """Every place ``_dispatch_once_locked`` can decide NOT to spawn a ready
    card, read from its SOURCE (AST), never from a hand list: an early
    ``return`` before the ready loop, a ``continue``/``break`` of the ready loop,
    a fail-open/abort ``except`` in it, a pass that drops ready rows or writes
    ``row_route``. A site added to the dispatcher is a new key here, and the
    contract test is red until the key has a trigger."""
    import ast
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(kbd._dispatch_once_locked))).body[0]
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For)
                and ast.unparse(n.iter) == "enumerate(ready_rows)")
    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}

    def owner(n):
        p = parents.get(n)
        while p is not None and not isinstance(p, (ast.For, ast.While, ast.FunctionDef,
                                                    ast.AsyncFunctionDef, ast.Lambda)):
            p = parents.get(p)
        return p

    def guard(n):
        p = parents[n]
        while not isinstance(p, (ast.If, ast.ExceptHandler)):
            p = parents[p]
        if isinstance(p, ast.If):
            return ast.unparse(p.test)
        first = next((ast.unparse(c.func) for c in ast.walk(ast.Module(body=parents[p].body, type_ignores=[]))
                      if isinstance(c, ast.Call)), "?")
        return f"except {ast.unparse(p.type) if p.type else ''} (try: {first})"

    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and owner(n) is fn and n.lineno < loop.lineno:
            out.add("return: " + guard(n))
        elif isinstance(n, (ast.Continue, ast.Break)) and owner(n) is loop:
            out.add(f"{type(n).__name__.lower()}: " + guard(n))
        elif isinstance(n, ast.ExceptHandler) and owner(n) is loop and n.body and not any(
                isinstance(x, (ast.Continue, ast.Break)) for x in ast.walk(n)):
            out.add("fail: " + guard(n.body[0]))
        elif isinstance(n, ast.Call) and n.lineno < loop.lineno:
            f = ast.unparse(n.func)
            if f in ("row_route.update", "row_route.setdefault"):
                out.add(f"route: {f}")
            elif any(isinstance(a, ast.Name) and a.id in ("ready_rows", "row_route") for a in n.args):
                out.add("rows: " + f)
        if isinstance(n, ast.Assign) and n.lineno < loop.lineno and any(
                (isinstance(t, ast.Name) and t.id == "row_route") or
                (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == "row_route")
                for t in n.targets):
            calls = [ast.unparse(c.func) for c in ast.walk(n.value) if isinstance(c, ast.Call)]
            out.add("route: " + (calls[0] if calls else ast.unparse(n.value)))
    return out


# Sites that are not a per-card refusal admit could inherit, each with why. A
# key here that no longer exists in the source is red too (no stale excuses).
NOT_A_CARD_REFUSAL = {
    "continue: dry_run": "the --dry-run preview; admit never dry-runs",
    "return: _cap == 0": "`dispatch --max 0`, an operator argument; admit has no --max (spec item 5)",
    "continue: record_only is not None": "the tail behind an EARLIER row's wall; admit routes one row",
    "rows: sorted": "orders the rows; admit has one",
    "rows: bool": "the placement-record gate snapshot",
    "rows: _capabilities_unplanned": "dry-run preview only",
    "rows: studio_affinity_unplanned": "dry-run preview only",
    "fail: except (TypeError, ValueError) (try: inspect.signature)": "spawn_fn signature back-compat",
    "fail: except Exception (try: _kb._load_dispatch_collision_scopes)": "collision check fails OPEN by design",
    "fail: except Exception (try: _kb._check_dispatch_file_collisions)": "collision check fails OPEN by design",
}


def _browser_spawn(task, workspace, *, board=None, placement=None, browser_env=None):
    SPAWNS.append((task.id, getattr(placement, "name", None)))
    return 4242


def _site_triggers():
    """site key -> trigger(m, ctx) that puts the card(s) ``ctx["tids"]`` (or
    the board) into the state that fires that site. It returns ``(host, kind,
    expect)``: kind ``twin`` = admit refuses with exactly the reason the tick
    gives a twin card (``refusal_from`` over the tick's own result), ``admit`` =
    a check only admit has (forced host, floor), ``host`` = HOST ADMISSION: the
    tick refuses and admit spawns."""
    from hermes_cli import kanban_budget as kbudget
    from hermes_cli import kanban_continuation as kcont
    from hermes_cli import kanban_pause_hold as kph
    from hermes_cli import kanban_provider_health as kph_health
    from hermes_cli import model_policy, model_registry
    from hermes_cli import profiles
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

    def upd(sql, ctx, *args):
        with kb.connect_closing() as conn:
            for tid in ctx["tids"]:
                conn.execute(sql, (*args, tid))

    def budget(m, ctx):
        m.setattr(kbudget, "evaluate_board_budget", lambda *a, **k: True)
        return "studio", "twin", "budget_paused"

    def max_spawn(m, ctx):
        with kb.connect_closing() as conn:
            assert kb.claim_task(conn, kb.create_task(conn, title="other", assignee="alpha"))
        ctx["kanban_cfg"] = {"max_spawn": 1}
        return "studio", "twin", "spawn_capped: max_spawn=1"

    def review_reservation(m, ctx):
        with kb.connect_closing() as conn:
            rid = kb.create_task(conn, title="review", assignee="alpha")
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (rid,))
        ctx["kanban_cfg"] = {"max_spawn": 1}
        return "studio", "twin", "total:ready_budget"

    def no_worker(m, ctx):
        upd("UPDATE tasks SET no_worker=1 WHERE id=?", ctx)
        return "studio", "twin", "no_worker"

    def human_lane(m, ctx):
        upd("UPDATE tasks SET assignee='human:apollo' WHERE id=?", ctx)
        m.setattr(profiles, "profile_exists", lambda name: not str(name).startswith("human"))
        return "studio", "twin", "not a spawnable profile"

    def pause_pre(m, ctx):
        m.setattr(kph, "held_routes", lambda rows, cache: {r["id"]: ("wait", "pause_hold (test)") for r in rows})
        return "studio", "twin", "pause_hold"

    def pause_post(m, ctx):
        real = kph.gate_rows

        def gate(conn, rows, result, row_route, *a, lane, **k):
            out = real(conn, rows, result, row_route, *a, lane=lane, **k)
            if lane == "ready":
                for r in rows:
                    row_route[r["id"]] = ("wait", "pause_hold (post-route)")
            return out
        m.setattr(kph, "gate_rows", gate)
        return "studio", "twin", "pause_hold \\(post-route\\)"

    def browser(m, ctx):
        def gate(conn, rows, result, rr, dry_run, *a, **k):
            for r in rows:
                rr[r["id"]] = ("wait", "browser_join_off")
        m.setattr(kbd, "_browser_gate", gate)
        return "studio", "twin", "browser_join_off"

    def route_row(m, ctx):
        m.setattr(kbd, "_route_row", lambda conn, row, plan, *a, **k: ("wait", "capability:gpu"))
        return "studio", "twin", "capability:gpu"

    def bind(m, ctx):
        # A workspace bound to a directory on ace-ai pins the card there.
        m.setattr(kbd, "_workspace_binds", lambda conn, rows, **k: {
            r["id"]: ("workspace_of:t_x", "t_x", "ace-ai") for r in rows})
        return "studio", "admit", "studio is not eligible.*pin ace-ai"

    def no_plan_pin(m, ctx):
        upd("UPDATE tasks SET body='host: ace-ai' WHERE id=?", ctx)
        ctx["plan"] = None
        return "studio", "twin", "pool_unavailable"

    def no_plan_unknown_pin(m, ctx):
        upd("UPDATE tasks SET body='host: nosuch-box' WHERE id=?", ctx)
        ctx["plan"] = None
        return "studio", "twin", "pin_unknown_host:nosuch-box"

    def no_plan_identity(m, ctx):
        m.setattr(kbd, "_identity_only_unplanned", lambda rows: {r["id"]: "requires_unsatisfied:docker"
                                                                 for r in rows})
        ctx["plan"] = None
        return "studio", "twin", "requires_unsatisfied:docker"

    def no_plan_origin(m, ctx):
        upd("UPDATE tasks SET origin_host='ace-ai' WHERE id=?", ctx)
        m.setattr(kbd, "_root_kanban_cfg", lambda: {"placement": {"continuation": "enforce"}})
        m.setattr(kcont, "origin_route", lambda conn, row, pool, **k: ("wait", "origin_host_absent"))
        ctx["plan"] = None
        return "studio", "twin", "origin_host_absent"

    def unassigned(m, ctx):
        upd("UPDATE tasks SET assignee=NULL WHERE id=?", ctx)
        return "studio", "twin", "unassigned"

    def default_assignee_write(m, ctx):
        upd("UPDATE tasks SET assignee=NULL WHERE id=?", ctx)
        ctx["kanban_cfg"] = {"default_assignee": "alpha"}
        real = kb._append_event

        def append(conn, task_id, kind, payload=None, **kw):
            if kind == "assigned":
                raise RuntimeError("disk full")
            return real(conn, task_id, kind, payload, **kw)
        m.setattr(kb, "_append_event", append)
        return "studio", "twin", "unassigned"

    def profile(m, ctx):
        m.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: False))
        return "studio", "twin", "not a spawnable profile"

    def per_profile_cap(m, ctx):
        with kb.connect_closing() as conn:
            assert kb.claim_task(conn, kb.create_task(conn, title="other", assignee="alpha"))
        ctx["kanban_cfg"] = {"max_in_progress_per_profile": 1}
        return "studio", "twin", "per_profile_cap"

    def flagship(m, ctx):
        upd("UPDATE tasks SET model_override='big-model' WHERE id=?", ctx)
        m.setattr(model_policy, "flagship_model_match", lambda model, cfg=None: True)
        return "studio", "twin", "flagship"

    def invalid_pin(m, ctx):
        upd("UPDATE tasks SET model_override='m', provider_override='prov' WHERE id=?", ctx)
        m.setattr(model_registry, "invalid_pin_reason", lambda provider, model: "unknown model m")
        return "studio", "twin", "blocked by the dispatcher"

    def respawn_guard(m, ctx):
        m.setattr(kbd, "check_respawn_guard", lambda conn, task_id, **k: "rate_limit_cooldown")
        return "studio", "twin", "rate_limit_cooldown"

    def provider_admission(m, ctx):
        upd("UPDATE tasks SET model_override='m', provider_override='prov' WHERE id=?", ctx)
        m.setattr(model_registry, "invalid_pin_reason", lambda provider, model: None)
        m.setattr(kb, "cooling_providers", lambda conn, **k: {"prov": 9999999999})
        m.setattr(kph_health, "available_profile_fallback", lambda *a, **k: None)
        return "studio", "twin", "credential_cooldown"

    def card_skills(m, ctx):
        def refused(conn, task_id, assignee, result, *, dry_run):
            result.skill_refused.append((task_id, ["missing-skill"]))
            return True
        m.setattr(kb, "_card_skills_refused", refused)
        return "studio", "twin", "card skills refused: missing-skill"

    def workspace_admission(m, ctx):
        def refused(conn, task_id, result, *, board, dry_run):
            result.workspace_refused.append((task_id, "workspaces_root_unmounted"))
            return True
        m.setattr(kb, "_workspace_admission_refused", refused)
        return "studio", "twin", "workspace unavailable: workspaces_root_unmounted"

    def floor(m, ctx):
        _hot_local(m, load1=200.0)
        return "studio", "admit", "refused on measured danger: studio: load1 200.0"

    def floor_error(m, ctx):
        m.setattr(ka, "measure_local", lambda: (_ for _ in ()).throw(RuntimeError("probe died")))
        return "studio", "admit", "refused on measured danger: floor error: probe died"

    def reach(m, ctx):
        m.setattr(kbd, "_portable_only", lambda *a, **k: (("ace-media",), None))
        ctx["measure_remote_fn"] = _remote()
        return "ace-ai", "admit", "ace-ai is not eligible.*host_reach"

    def forced_take(m, ctx):
        m.setattr(kwp, "board_fault", lambda host, board, **k: "board root unmounted")
        ctx["measure_remote_fn"] = _remote()
        return "ace-ai", "admit", "ace-ai is not eligible.*board_unhostable"

    def forced_host(m, ctx):
        return "nope-box", "admit", "nope-box is not eligible"

    def layer0b(m, ctx):
        m.setattr(kbd, "_attached_skill_gap", lambda row, host, caps: "gpu")
        ctx["measure_remote_fn"] = _remote()
        return "ace-ai", "admit", "requires:gpu\\+layer0b:ace-ai"

    def claim(m, ctx):
        m.setattr(kb, "claim_task", lambda conn, task_id, **k: None)
        return "studio", "twin", "did not spawn"

    def claim_pause(m, ctx):
        def held(conn, task_id, **k):
            raise kph.PauseHeld("studio", "m-1", task_id)
        m.setattr(kb, "claim_task", held)
        return "studio", "twin", "pause_hold \\(studio:m-1 stage=claim\\)"

    def lane_route(m, ctx):
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
        return "studio", "twin", "spawn failed"

    def workspace_unavailable(m, ctx):
        def refuse(task, board=None, conn=None, **kw):
            if task.status == "running":                    # the post-claim check only
                raise WorkspaceUnavailable("workspaces_root_unmounted: /Volumes/x")
            return None
        m.setattr(kb, "_validate_workspace_admission", refuse)
        return "studio", "twin", "workspace unavailable"

    def workspace_error(m, ctx):
        from hermes_cli import kanban_db_workspace as kbw
        m.setattr(kbw, "resolve_workspace", lambda task, **k: (_ for _ in ()).throw(RuntimeError("no dir")))
        return "studio", "twin", "spawn failed"

    def claim_lost_before(m, ctx):
        m.setattr(kb, "_claim_still_held", lambda conn, task: False)
        m.setattr(kb, "_abort_lost_claim_spawn", lambda conn, task, pid: _release(task.id))
        return "studio", "twin", "did not spawn"

    def _join(m, ctx):
        def gate(conn, rows, result, rr, dry_run, *a, **k):
            for r in rows:
                result.browser_joins[r["id"]] = {"plans": [{"host": "ci-box"}], "source_host": "studio"}
        m.setattr(kbd, "_browser_gate", gate)
        m.setattr(kbd, "_browser_release_joined", lambda *a, **k: None)
        ctx["spawn_fn"] = _browser_spawn

    def join_refused(m, ctx):
        _join(m, ctx)
        m.setattr(kbd, "_browser_join_lease", lambda conn, claimed, join, host: (None, "BROWSER_REFUSED"))
        return "studio", "twin", "browser_refused:BROWSER_REFUSED"

    def join_claim_lost(m, ctx):
        _join(m, ctx)

        def lease(conn, claimed, join, host):
            _release(claimed.id)                             # the relay took minutes; the claim went
            return {"X": "1"}, None
        m.setattr(kbd, "_browser_join_lease", lease)
        m.setattr(kb, "_abort_lost_claim_spawn", lambda conn, task, pid: None)
        return "studio", "twin", "did not spawn"

    def launch(m, ctx):
        ctx["admitted"] = True
        ctx["spawn_fn"] = lambda task, workspace, *, board=None, placement=None: (_ for _ in ()).throw(
            RuntimeError("exec failed"))
        return "studio", "twin", "spawn failed"

    def launch_not_owned(m, ctx):
        ctx["admitted"] = True
        m.setattr(kcont, "record_placement", lambda conn, task_id, run_id, host: _release(task_id) or False)
        return "studio", "twin", "did not spawn"

    def launch_claim_lost(m, ctx):
        ctx["admitted"] = True
        m.setattr(kbd, "_set_worker_pid", lambda conn, task_id, pid, **k: _release(task_id) or False)
        m.setattr(kb, "_abort_lost_claim_spawn", lambda conn, task, pid: None)
        m.setattr(kbd, "_stop_own_spawn", lambda proc, grace=5.0: None)
        ctx["launched"] = True                               # spawned, then the claim was gone
        return "studio", "twin", "did not spawn"

    # HOST ADMISSION: the one axis admit turns off. The tick refuses, admit spawns.
    def load_gate_paused(m, ctx):
        m.setattr(kbd, "_route_first_enabled", lambda: False)
        ctx["tick_kw"] = {"spawn_paused": "load gate paused", "spawn_limit": 0}
        ctx["plan"] = None
        return "studio", "host", None

    def load_gate_zero(m, ctx):
        ctx["tick_kw"] = {"spawn_limit": 0}
        ctx["plan"] = None
        return "studio", "host", None

    def max_in_progress(m, ctx):
        with kb.connect_closing() as conn:
            assert kb.claim_task(conn, kb.create_task(conn, title="other", assignee="alpha"))
        ctx["tick_kw"] = {"max_in_progress": 1}
        return "studio", "host", None

    def memory_critical(m, ctx):
        m.setattr(kbd, "_memory_pressure_level", lambda sample=None: "critical")
        return "studio", "host", None

    def pool_capacity(m, ctx):
        # A card pinned to ace-ai, ace-ai with no free slot (band hot) while the
        # pool as a whole has room: the tick's take refuses pin_host_full and no
        # last resort applies; admit's forced take skips band/slots and places it.
        upd("UPDATE tasks SET body='host: ace-ai' WHERE id=?", ctx)
        real = ka._dispatcher_plan

        def full():
            plan, *_ = real({})
            plan.slots = {"ace-ai": 0, "ace-media": 1}
            return plan
        ctx["plan_fn"] = full
        ctx["measure_remote_fn"] = _remote()
        return "ace-ai", "host", None

    return {
        "return: _kbudget.evaluate_board_budget(board, _kb.kanban_db_path(board=board), "
        "_kb.board_state_dir(board), cache=budget_cache)": budget,
        "return: running_count >= max_spawn": max_spawn,
        "break: ready_budget is not None and spawned >= ready_budget": review_reservation,
        "continue: route_first": review_reservation,
        "rows: _kb._drop_no_worker_rows": no_worker,
        "rows: _drop_human_lane_rows": human_lane,
        "rows: _kph.held_routes": pause_pre,
        "route: row_route.update": pause_pre,
        "rows: _kph.gate_rows": pause_post,
        "rows: _browser_gate": browser,
        "route: _route_row": route_row,
        "rows: _workspace_binds": bind,
        "route: ('wait', 'pool_unavailable')": no_plan_pin,
        "route: _pr_np.pin_unknown_host": no_plan_unknown_pin,
        "rows: _identity_only_unplanned": no_plan_identity,
        "route: row_route.setdefault": no_plan_identity,
        "route: _origin_route or ('local', None)": no_plan_origin,
        "continue: wait is not None": route_row,
        "continue: _default_assignee and _default_assignee_resolved": unassigned,
        "continue: except Exception (try: _kbc.write_txn)": default_assignee_write,
        "continue: profile_exists is not None and (not profile_exists(row_assignee))": profile,
        "continue: current >= _row_cap": per_profile_cap,
        "continue: flagship_refused(row['id'], row_assignee)": flagship,
        "continue: invalid_pin_refused(row['id'])": invalid_pin,
        "continue: guard_reason is not None": respawn_guard,
        "continue: deferred": provider_admission,
        "continue: _kb._card_skills_refused(conn, row['id'], row_assignee, result, dry_run=dry_run)": card_skills,
        "continue: _kb._workspace_admission_refused(conn, row['id'], result, board=board, dry_run=dry_run)":
            workspace_admission,
        "continue: _forced is not None": forced_host,
        "continue: _floor is not None": floor,
        "fail: except Exception (try: admit_floor)": floor_error,
        "continue: _only is not None and force_host.lower() not in {h.lower() for h in _only}": reach,
        "continue: _forced_remote": forced_take,
        "continue: _gap is not None": layer0b,
        "continue: claimed is None": claim,
        "fail: except _kph.PauseHeld (try: _kb.claim_task)": claim_pause,
        "continue: except Exception (try: _kb.apply_lane_model_override)": lane_route,
        "continue: except WorkspaceUnavailable (try: _kb._validate_workspace_admission)": workspace_unavailable,
        "continue: except Exception (try: _kb._validate_workspace_admission)": workspace_error,
        "continue: not _kb._claim_still_held(conn, claimed)": claim_lost_before,
        "continue: _btok is not None": join_refused,
        "continue: _btok is None and (not _kb._claim_still_held(conn, claimed))": join_claim_lost,
        "fail: except Exception (try: result.browser_joins.get)": launch,
        "continue: outcome == _LAUNCH_NOT_OWNED": launch_not_owned,
        "continue: outcome == _LAUNCH_CLAIM_LOST": launch_claim_lost,
        "return: (spillover is None or spillover.budget <= 0) and (not route_first)": load_gate_paused,
        "break: not local_free and (not pool_free)": load_gate_zero,
        "return: total_running >= max_in_progress": max_in_progress,
        "return: pressure == 'critical'": memory_critical,
        "continue: not _last_resort_allowed(route, local_free, _cap_only, spillover)": pool_capacity,
    }


def _release(task_id):
    """Somebody else released the claim (the lost-claim shapes)."""
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?",
                     (task_id,))


def _norm(text: str, tid: str) -> str:
    return text.replace(tid, "<card>")


def test_every_dispatcher_decision_site_has_a_trigger():
    """Built from the dispatcher's source: a site with no trigger and no
    recorded reason fails here, so the next seam cannot be missed."""
    sites = _decision_sites()
    triggers = _site_triggers()
    assert not sites - set(triggers) - set(NOT_A_CARD_REFUSAL), (
        f"dispatcher decision site(s) with no admit trigger: {sorted(sites - set(triggers) - set(NOT_A_CARD_REFUSAL))}")
    stale = (set(triggers) | set(NOT_A_CARD_REFUSAL)) - sites
    assert not stale, f"trigger(s) for sites the dispatcher no longer has: {sorted(stale)}"


@pytest.mark.parametrize("site", sorted(_site_triggers()))
def test_admit_returns_the_dispatchers_own_refusal_at_every_site(board, monkeypatch, site):
    """INVARIANT (Apollo r7): admit produces exactly the route and refusal the
    dispatcher produces for this card on this host, with ONE axis disabled:
    host-capacity admission. ``twin``: admit's refusal == refusal_from(the tick's
    own result) for an identical card. ``host``: the tick refuses, admit spawns.
    ``admit``: a check only admit makes (forced host, floor) refuses."""
    with monkeypatch.context() as m:
        _hot_local(m, load1=10.0)
        a, b = _card(), _card()
        ctx: dict = {"tids": [a, b], "spawn_fn": _spawn}
        host, kind, expect = _site_triggers()[site](m, ctx)
        cfg = ctx.get("kanban_cfg", {})
        if "plan_fn" in ctx or "plan" in ctx:
            plan_fn = ctx.get("plan_fn") or (lambda: ctx["plan"])
            m.setattr(ka, "_dispatcher_plan", lambda c: (plan_fn(), board / "fleet"))
        SPAWNS.clear()
        if kind == "host":
            with kb.connect_closing() as conn:
                res = kbd.dispatch_once(conn, spawn_fn=ctx["spawn_fn"], spillover=ka._dispatcher_plan(cfg)[0],
                                        **ka._tick_knobs(cfg), **ctx.get("tick_kw", {}))
            assert not res.spawned and SPAWNS == [], (site, res.spawned)
            assert ka.admit(b, host, f"site {site}", kanban_cfg=cfg, spawn_fn=ctx["spawn_fn"],
                            measure_remote_fn=ctx.get("measure_remote_fn"))["host"] == host
            assert SPAWNS == [(b, None if host == "studio" else host)]
            return
        with pytest.raises(ka.AdmitRefused, match=expect) as got:
            ka.admit(a, host, f"site {site}", kanban_cfg=cfg, spawn_fn=ctx["spawn_fn"],
                     measure_remote_fn=ctx.get("measure_remote_fn"))
        status, lock = _claim(a)
        assert SPAWNS == ([(a, None)] if ctx.get("launched") else []), (site, SPAWNS)
        assert lock is None and status != "running", (site, status, lock)
        SPAWNS.clear()
        # An admission_override event exists only once the run reached launch
        # (the audit of the override exercised); every earlier refusal has none.
        assert bool(_events(a)) == bool(ctx.get("admitted")), site
        if kind == "twin":
            with kb.connect_closing() as conn:
                res = kbd.dispatch_once(conn, spawn_fn=ctx["spawn_fn"], spillover=ka._dispatcher_plan(cfg)[0],
                                        **ka._tick_knobs(cfg), **ctx.get("tick_kw", {}))
            assert b not in [s[0] for s in res.spawned], (site, res.spawned)
            assert _norm(str(got.value), a) == _norm(f"{b}: {ka.refusal_from(res, b)}", b), site
