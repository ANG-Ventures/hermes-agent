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
GATE = {"enabled": True, "pause_above": 64, "resume_below": 48, "max_spawn_per_tick": 16}
POOL_HOST = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                         absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
POOL = kwp.PoolConfig(hosts=("ace-ai",), profiles=("alpha",), pool_hosts=(POOL_HOST,))


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
