"""AC-12 (Traceability-vs-Placement spec v1.1 §10, Phase 3; card t_87e6afb5).

``hermes kanban placement --fleet`` reads ONE module
(``kanban_placement_view``): a fixture of 3 pool hosts x 2 boards, the band
from a ``load_gate.json`` fixture, held rows grouped by code with the host
that refused. Named mutations (each shown red once on the card):

* a hard-coded host list (the ladder is the pool's priority order, which here
  is neither alphabetical nor the live fleet's names);
* running counted from ``tasks.status`` alone (a stale ``worker_placed`` on an
  earlier run, or none at all, must not count on a host).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_placement_reasons as pr
from hermes_cli import kanban_placement_view as kpv
from hermes_cli import kanban_worker_pool as kwp

NOW = 1_791_800_000
# Priority order deliberately not alphabetical and not the live fleet's names.
LADDER = ["zeta-box", "alpha-box", "mid-box"]


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing():
        kb.create_board(slug="default", name="Default")
    kb.create_board(slug="second", name="Second")
    _write(h / "fleet" / "fleet-roles.json", {"schema": 1, "hosts": {
        "zeta-box": {"roles": {"kanban-worker": {"slots": 6}}, "state": "active"},
        "alpha-box": {"roles": {"kanban-worker": {"slots": 2}}, "state": "active"},
        "mid-box": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"}}})
    _write(h / "fleet" / "kanban-pool.json", {
        "schema": 1, "ssh_user": "kanbanw", "capacity_pct": 0.8, "priority": LADDER,
        "profiles": ["alpha"], "hosts": {n: {"enabled": True, "absence": "optional"} for n in LADDER}})
    return h


def _gate(band_zeta="ok"):
    row = lambda band, free: {"slots": 4, "running": 0, "load1": 1.0, "ncpu": 16, "band": band,  # noqa: E731
                               "hot": False, "free": free, "reachable": True}
    return {"updated_at": NOW - 5, "band": "spilling", "pool": {
        "planned": True, "planned_at": NOW - 5, "hosts": {
            "zeta-box": row(band_zeta, 3), "alpha-box": dict(row("warm", 0), pressure="cpu 0.91"),
            "mid-box": row("ok", 2)}}}


def _running(conn, title, *, host=None, stale_host=None, rule="pin"):
    """A running card; ``host`` = worker_placed on the CURRENT run,
    ``stale_host`` = worker_placed on an EARLIER run only."""
    tid = kb.create_task(conn, title=title, assignee="alpha")
    for h, current in ((stale_host, False), (host, True)):
        if h is None and current is False:
            continue
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'alpha', ?, ?)",
            (tid, "running" if current else "crashed", NOW - 100))
        run_id = cur.lastrowid
        if h is not None:
            kb._append_event(conn, tid, pr.WORKER_PLACED_EVENT, {"host": h, "reason": rule}, run_id=run_id)
        if current:
            conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (run_id, tid))
    conn.commit()
    return tid


def _held(conn, title, payload, *, then=None):
    tid = kb.create_task(conn, title=title, assignee="alpha")
    kb._append_event(conn, tid, pr.PLACEMENT_WAIT_EVENT, payload)
    if then:
        kb._append_event(conn, tid, then, {})
    conn.commit()
    return tid


@pytest.fixture
def boards(home):
    ids = {}
    with kb.connect_closing(board="default") as c:
        ids["z1"] = _running(c, "z1", host="zeta-box")
        ids["z2"] = _running(c, "z2", host="zeta-box", rule="band_spilling")
        ids["m1"] = _running(c, "m1", host="mid-box")
        ids["local"] = _running(c, "local")                          # no worker_placed at all
        ids["stale"] = _running(c, "stale", stale_host="alpha-box")  # placed on an EARLIER run only
        ids["h_pin"] = _held(c, "h_pin", {"code": "pin_host_full", "detail": "0 free",
                                          "route": "pin", "host": "zeta-box", "since": NOW - 600})
        ids["h_legacy"] = _held(c, "h_legacy", {"reason": "pin_host_hot (cpu 0.91)", "route": "pin",
                                                "host": "alpha-box", "since": NOW - 300})
        ids["h_ended"] = _held(c, "h_ended", {"code": "local_full", "route": "local"}, then="claimed")
    with kb.connect_closing(board="second") as c:
        ids["z3"] = _running(c, "z3", host="zeta-box")
        ids["a1"] = _running(c, "a1", host="alpha-box")
        ids["h_pool"] = _held(c, "h_pool", {"code": "pin_host_full", "route": "pin", "host": "mid-box",
                                            "since": NOW - 60})
        ids["h_local"] = _held(c, "h_local", {"reason": "local_full", "route": "local", "host": None,
                                              "since": NOW - 30})
    return ids


def _report(home, gate_state):
    return kpv.compute(home, boards=kpv.live_boards(), gate_state=gate_state, now=NOW)


def _running_by_host_all_boards():
    total = {}
    for slug in ("default", "second"):
        with kb.connect_closing(board=slug) as c:
            for h, n in kwp.running_by_host(c).items():
                total[h] = total.get(h, 0) + n
    return total


def test_ac12_running_per_host_equals_running_by_host_in_ladder_order(home, boards):
    rep = _report(home, _gate())
    assert list(rep["hosts"]) == LADDER  # the pool's priority, not a hard-coded list
    running = {n: h["running"] for n, h in rep["hosts"].items() if h["running"]}
    assert running == _running_by_host_all_boards() == {"zeta-box": 3, "mid-box": 1, "alpha-box": 1}
    assert [h["slots"] for h in rep["hosts"].values()] == [6, 2, 4]
    zeta = rep["hosts"]["zeta-box"]
    assert {(c["task_id"], c["board"], c["rule"]) for c in zeta["cards"]} == {
        (boards["z1"], "default", "pin"), (boards["z2"], "default", "band_spilling"),
        (boards["z3"], "second", "pin")}
    # Local = running with no worker_placed on the CURRENT run (a stale one doesn't count).
    assert {c["task_id"] for c in rep["local"]["cards"]} == {boards["local"], boards["stale"]}
    assert rep["errors"] == []


def test_ac12_band_and_pressure_come_from_the_gate_file(home, boards, tmp_path):
    gate_file = tmp_path / "load_gate.json"
    _write(gate_file, _gate())
    rep = _report(home, klg.read_state(gate_file))
    assert rep["hosts"]["zeta-box"]["band"] == "ok"
    assert rep["hosts"]["alpha-box"]["pressure"] == "cpu 0.91"
    assert rep["hosts"]["alpha-box"]["free"] == 0
    # A hand edit of a COPY changes the printed band: generated, not maintained.
    copy = tmp_path / "copy.json"
    doc = json.loads(gate_file.read_text())
    doc["pool"]["hosts"]["zeta-box"]["band"] = "hot"
    _write(copy, doc)
    edited = _report(home, klg.read_state(copy))
    assert edited["hosts"]["zeta-box"]["band"] == "hot"
    assert any(line.startswith("  zeta-box:") and "band=hot" in line for line in kpv.format_fleet(edited))
    assert any(line.startswith("  zeta-box:") and "band=ok" in line for line in kpv.format_fleet(rep))


def test_ac12_held_groups_by_code_and_names_the_refusing_host(home, boards):
    rep = _report(home, _gate())
    held = rep["held"]
    assert held["count"] == 4  # h_ended's episode closed with `claimed`
    assert held["by_code"] == {"pin_host_full": 2, "local_full": 1, "pin_host_hot": 1}
    by_id = {r["task_id"]: r for r in held["rows"]}
    assert by_id[boards["h_pin"]]["host"] == "zeta-box" and by_id[boards["h_pool"]]["host"] == "mid-box"
    assert by_id[boards["h_legacy"]]["code"] == "pin_host_hot"  # pre-2a row: code from legacy reason
    text = kpv.format_fleet(rep)
    assert "  held: 4 (pin_host_full 2, local_full 1, pin_host_hot 1)" in text
    assert any(boards["h_pool"] in ln and "pin_host_full host=mid-box" in ln for ln in text)


def test_ac12_json_is_the_dict_the_text_prints(home, boards):
    rep = _report(home, _gate())
    assert json.loads(json.dumps(rep)) == rep
    text = "\n".join(kpv.format_fleet(rep))
    for name, h in rep["hosts"].items():
        assert f"  {name}: running={h['running']} slots={h['slots']}" in text


def test_ac12_task_block(home, boards):
    with kb.connect_closing(board="default") as c:
        blk = kpv.task_block(c, boards["z2"], now=NOW)
        held = kpv.task_block(c, boards["h_pin"], now=NOW)
    assert blk["running_on"]["host"] == "zeta-box" and blk["running_on"]["rule"] == "band_spilling"
    assert held["held"]["code"] == "pin_host_full" and held["held"]["host"] == "zeta-box"
    assert "  held: pin_host_full host=zeta-box since 10m00s ago (0 free)" in kpv.format_task(held)


def test_ac12_unreadable_board_is_an_error_not_empty(home, boards, tmp_path):
    bad = tmp_path / "missing.db"
    rep = kpv.compute(home, boards=[*kpv.live_boards(), ("ghost", bad)], gate_state=_gate(), now=NOW)
    assert len(rep["errors"]) == 1 and rep["errors"][0].startswith("ghost:")


def test_registered_board_with_missing_db_is_an_error_not_dropped(home, boards):
    """Apollo r1 on 26668b7a99a1: a board list_boards() registers (board.json)
    whose kanban.db is gone must surface as an error, never as an idle board."""
    want = _report(home, _gate())
    gone = kb._board_db_path_ignoring_pin("second")
    kb.create_board("vanished")
    kb._board_db_path_ignoring_pin("vanished").unlink()
    rep = _report(home, _gate())
    assert [e.split(":", 1)[0] for e in rep["errors"]] == ["vanished"]
    assert {n: h["running"] for n, h in rep["hosts"].items()} == {n: h["running"] for n, h in want["hosts"].items()}
    assert rep["held"]["by_code"] == want["held"]["by_code"]
    assert gone.exists()


def test_live_boards_ignore_the_worker_db_pin(home, boards, monkeypatch):
    """Prism 26668b7a99a1: under the dispatcher's HERMES_KANBAN_DB pin every
    slug must still resolve to its OWN board, or counts multiply by N boards."""
    want = _report(home, _gate())
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)  # kanban-sandbox: off — the pin under test; `home` keeps HERMES_HOME on tmp_path
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb._board_db_path_ignoring_pin("default")))
    assert kb.kanban_db_path() == kb._board_db_path_ignoring_pin("default")  # the pin is live
    paths = [p for _, p in kpv.live_boards()]
    assert len(paths) == len(set(paths)) == 2
    got = _report(home, _gate())
    assert {n: h["running"] for n, h in got["hosts"].items()} == {n: h["running"] for n, h in want["hosts"].items()}
    assert got["held"]["by_code"] == want["held"]["by_code"]


def test_task_block_reads_one_snapshot(home, boards, monkeypatch):
    """Prism bac7a74e910f: the tasks row, the current run and the held episode
    come from one read transaction; a caller's own transaction is preserved."""
    seen = []
    real = kpv._running
    monkeypatch.setattr(kpv, "_running", lambda c: (seen.append(c.in_transaction), real(c))[1])
    with kb.connect_closing(board="default") as c:
        assert kpv.task_block(c, boards["z2"], now=NOW)["running_on"]["host"] == "zeta-box"
        assert seen == [True] and not c.in_transaction  # opened, then released
        c.execute("BEGIN")
        kpv.task_block(c, boards["z2"], now=NOW)
        assert seen == [True, True] and c.in_transaction  # caller's txn left open
        c.execute("ROLLBACK")


def test_ac12_no_reason_literal_outside_the_reasons_module():
    """I-S5: the reader module names no reason/event string itself; it imports them."""
    values = {v for k, v in vars(pr).items() if k.isupper() and isinstance(v, str)}
    values |= {"pin_host_full", "pin_host_hot", "pool_full", "local_full"}  # take()/route codes
    src = Path(kpv.__file__)
    tree = ast.parse(src.read_text())
    # The report's own schema key "gate" shares its spelling with
    # LOCAL_BINDING_GATE (#2256). It is exempt only in KEY position (dict-literal
    # key or ``.get("gate")``); as a value it is still a reason literal.
    report_keys = {"gate"}
    key_nodes = {id(k) for n in ast.walk(tree) if isinstance(n, ast.Dict) for k in n.keys if k is not None}
    key_nodes |= {id(n.args[0]) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "get" and n.args}
    found = sorted({(n.value, n.lineno) for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in values
                    and not (n.value in report_keys and id(n) in key_nodes)})
    assert found == [], found
    wp = ast.parse(Path(kwp.__file__).read_text())
    assert not [n for n in ast.walk(wp) if isinstance(n, ast.Constant) and n.value == pr.WORKER_PLACED_EVENT]


def _parse(*argv):
    import argparse

    from hermes_cli.kanban_parser import build_parser

    top = argparse.ArgumentParser()
    build_parser(top.add_subparsers(dest="cmd"))
    return top.parse_args(["kanban", *argv])


def test_cli_placement_fleet_and_diagnostics_alias(home, boards, tmp_path, capsys, monkeypatch):
    from hermes_cli import kanban as kcli

    gate_file = tmp_path / "g.json"
    _write(gate_file, _gate(band_zeta="warm"))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    # --gate PATH reads that file; the default path is <root>/load_gate.json.
    assert kcli._cmd_placement(_parse("placement", "--fleet", "--gate", str(gate_file), "--json")) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["hosts"]["zeta-box"]["band"] == "warm" and rep["hosts"]["zeta-box"]["running"] == 3
    _write(home / "load_gate.json", _gate(band_zeta="hot"))
    assert kcli._cmd_diagnostics(_parse("diagnostics", "--placement", "--fleet")) == 0
    assert any(ln.startswith("  zeta-box: running=3 slots=6") and "band=hot" in ln
               for ln in capsys.readouterr().out.splitlines())
    assert kcli._cmd_placement(_parse("placement", boards["h_pin"])) == 0
    assert "held: pin_host_full host=zeta-box" in capsys.readouterr().out
