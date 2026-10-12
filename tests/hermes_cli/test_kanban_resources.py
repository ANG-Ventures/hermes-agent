"""Resources bound to a card's lifetime (t_2df204e5): a run records what it started; once the
card is done/archived the dispatcher sweep stops it, unless the body says ``keep-alive:``."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_resources as kres


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    # The fleet registry the sweep binds hosts to (live shape: kanban-reach-matrix.json aliases).
    (home / "fleet").mkdir()
    (home / "fleet" / "kanban-reach-matrix.json").write_text(
        json.dumps({"schema": 1, "aliases": ["ace-ai", "ace-media"], "hosts": {}}))
    return home


def _running_card(conn, body: str = "") -> tuple[str, int]:
    tid = kb.create_task(conn, title="drill", body=body, assignee="daedalus")
    assert kb.claim_task(conn, tid) is not None
    return tid, int(kb.get_task(conn, tid).current_run_id)


class _Runner:
    def __init__(self, rc: int = 0):
        self.rc, self.calls = rc, []

    def __call__(self, argv):
        self.calls.append(argv)
        return self.rc, "" if self.rc == 0 else "boom"


def _quiet(_board, _line):
    pass


def test_fresh_and_legacy_runs_carry_an_empty_resources_list(kanban_home, tmp_path):
    with kbc.connect_closing() as conn:
        tid, run = _running_card(conn)
        assert json.loads(conn.execute("SELECT resources FROM task_runs WHERE id=?", (run,)).fetchone()[0]) == []
    # Legacy board: task_runs without the column gains it with the [] default (additive migration).
    db = tmp_path / "legacy.db"
    seed = sqlite3.connect(db)
    seed.executescript(kb.SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS task_runs")[0])
    create_runs, _ = kbc._REBUILD_SPECS["task_runs"]
    seed.execute(create_runs.replace(", resources TEXT NOT NULL DEFAULT '[]'", ""))
    seed.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_00000001','x','done',0)")
    seed.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES ('t_00000001','done',0)")
    seed.commit()
    assert "resources" not in {r[1] for r in seed.execute("PRAGMA table_info(task_runs)")}
    seed.close()
    conn = kbc.connect(db)
    try:
        assert conn.execute("SELECT resources FROM task_runs").fetchone()[0] == "[]"
        assert kres.release_finished(conn, runner=_Runner(), notify=_quiet) == []
    finally:
        conn.close()


def test_finished_card_stops_what_its_run_started(kanban_home):
    runner, lines = _Runner(), []
    with kbc.connect_closing() as conn:
        tid, run = _running_card(conn)
        kres.kanban_run_resource(conn, tid, "compose", "mem0-rt-s", "local")
        kres.kanban_run_resource(conn, tid, "vm", "win11-ai", "ace-ai")
        # Still running: the sweep leaves it alone.
        assert kres.release_finished(conn, runner=runner, notify=_quiet) == []
        assert kb.complete_task(conn, tid, summary="ok")
        acts = kres.release_finished(conn, runner=runner, notify=lambda b, l: lines.append(l))
        assert {a["action"] for a in acts} == {"stopped"}
        assert runner.calls[0] == ["docker", "compose", "--project-name=mem0-rt-s", "stop"]
        assert runner.calls[1][:4] == ["ssh", "-o", "BatchMode=yes", "-o"]
        assert runner.calls[1][-3:] == ["--", "ace-ai", "sudo -n virsh shutdown -- win11-ai"]
        assert len(lines) == 2 and all(tid in line for line in lines)
        assert kres.outstanding(conn, tid) == []
        assert {r["state"] for r in kres.list_resources(conn, tid)} == {"stopped"}
        # Idempotent: a second tick has nothing to do.
        assert kres.release_finished(conn, runner=runner, notify=_quiet) == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("resource_started") == 2 and kinds.count("resource_stopped") == 2


def test_keep_alive_in_the_body_keeps_it(kanban_home):
    runner = _Runner()
    with kbc.connect_closing() as conn:
        tid, _ = _running_card(conn, body="host: studio\nkeep-alive: Ace uses this VM daily\n")
        kres.kanban_run_resource(conn, tid, "container", "keepme", "local")
        kb.complete_task(conn, tid, summary="ok")
        acts = kres.release_finished(conn, runner=runner, notify=_quiet)
        assert [a["action"] for a in acts] == ["kept"] and runner.calls == []
        assert kres.list_resources(conn, tid)[0]["kept_reason"] == "Ace uses this VM daily"
        assert kres.outstanding(conn, tid) == []


def test_archive_releases_too_and_failed_stops_stay_open_for_closeout(kanban_home):
    runner = _Runner(rc=1)
    with kbc.connect_closing() as conn:
        tid, _ = _running_card(conn)
        kres.kanban_run_resource(conn, tid, "gpu-resident", "user:zerank-native.service", "ace-ai")
        kb.archive_task(conn, tid)
        for _ in range(kres.MAX_STOP_ATTEMPTS):
            kres.release_finished(conn, runner=runner, notify=_quiet)
        assert "systemctl --user stop -- zerank-native.service" in runner.calls[0][-1]
        (left,) = kres.outstanding(conn, tid)
        assert left["state"] == "stop_failed" and "rc=1" in left["last_error"]
        kres.release_finished(conn, runner=runner, notify=_quiet)
        assert len(runner.calls) == kres.MAX_STOP_ATTEMPTS  # gives up; no stop storm
        assert kres.mark(conn, tid, "gpu-resident", "user:zerank-native.service", "ace-ai", "stopped",
                         reason="stopped by hand") == 1
        assert kres.outstanding(conn, tid) == []


@pytest.mark.parametrize("rid", ["a;rm -rf /", "$(id)", "x y", ""])
def test_registration_refuses_shell_tokens_and_finished_cards(kanban_home, rid):
    with kbc.connect_closing() as conn:
        tid, _ = _running_card(conn)
        with pytest.raises(ValueError):
            kres.kanban_run_resource(conn, tid, "container", rid, "local")
        kb.complete_task(conn, tid, summary="ok")
        with pytest.raises(ValueError, match="finished card"):
            kres.kanban_run_resource(conn, tid, "container", "ok", "local")


@pytest.mark.parametrize("host", ["-v", "-oProxyCommand=id", "ace@ace-ai", "/tmp/x", "evil-box", ""])
def test_registration_refuses_option_shaped_and_unregistered_hosts(kanban_home, host):
    with kbc.connect_closing() as conn:
        tid, _ = _running_card(conn)
        with pytest.raises(ValueError, match="resource host"):
            kres.kanban_run_resource(conn, tid, "container", "ok", host)
        assert kres.kanban_run_resource(conn, tid, "container", "ok", "ACE-AI")["resource"]["host"] == "ace-ai"


@pytest.mark.parametrize("rid", ["--help", "-f"])
def test_registration_refuses_option_shaped_ids(kanban_home, rid):
    with kbc.connect_closing() as conn:
        tid, _ = _running_card(conn)
        with pytest.raises(ValueError, match="resource id"):
            kres.kanban_run_resource(conn, tid, "container", rid, "local")


@pytest.mark.parametrize("kind", sorted(kres.STOP_VERBS))
def test_an_option_shaped_id_planted_in_the_row_is_never_an_option(kind):
    """Defence past registration: whatever reaches the argv sits after ``--`` (or inside ``--opt=``)."""
    for rid in ("--help", "user:--help"):
        argv = kres._stop_argv({"kind": kind, "id": rid, "host": "local"})
        idx = argv.index("--") if "--" in argv else None
        bare = [a for a in argv if a in (rid, rid[len("user:"):])]
        if idx is None:  # compose: the id is glued to its own option
            assert bare == [] and f"--project-name={rid}" in argv
        else:
            assert bare and all(argv.index(a) > idx for a in bare)
    remote = kres._stop_argv({"kind": kind, "id": "x", "host": "-v"})
    assert remote[remote.index("--") + 1] == "-v"  # ssh never parses the host as an option


def test_sweep_refuses_a_planted_row_without_running_it(kanban_home):
    runner = _Runner()
    with kbc.connect_closing() as conn:
        tid, run = _running_card(conn)
        planted = [{"kind": "container", "id": "--help", "host": "local", "started_at": 0, "state": "started"},
                   {"kind": "vm", "id": "win11-ai", "host": "-v", "started_at": 0, "state": "started"},
                   {"kind": "vm", "id": "win11-ai", "host": "evil-box", "started_at": 0, "state": "started"}]
        conn.execute("UPDATE task_runs SET resources = ? WHERE id = ?", (kres._dump(planted), run))
        conn.commit()
        kb.complete_task(conn, tid, summary="ok")
        acts = kres.release_finished(conn, runner=runner, notify=_quiet)
        assert runner.calls == []
        assert [a["action"] for a in acts] == ["stop_failed"] * 3
        assert all(a["error"].startswith("refused: ") for a in acts)
        assert len(kres.outstanding(conn, tid)) == 3  # left open for the ang-closeout Resources step


def test_fleet_hosts_fails_closed(tmp_path):
    assert kres.fleet_hosts(tmp_path) == frozenset()
    (tmp_path / "placement-policy.json").write_text("{not json")
    (tmp_path / "kanban-pool.json").write_text(json.dumps({"hosts": {"ci-box": {}, "-v": {}, "a@b": {}}}))
    assert kres.fleet_hosts(tmp_path) == {"ci-box"}


def test_keep_alive_needs_a_reason():
    assert kres.keep_alive_reason("keep-alive:   \n") is None
    assert kres.keep_alive_reason("blah\n  Keep-Alive: serving voice\n") == "serving voice"
    assert kres.keep_alive_reason("we do not keep-alive: inline") is None


def test_dispatcher_tick_runs_the_sweep(kanban_home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd

    seen = []
    monkeypatch.setattr(kres, "release_finished", lambda conn, board=None: seen.append(board) or [])
    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert seen == []  # a dry run never stops anything
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert len(seen) == 1


def test_real_container_is_stopped_by_the_sweep(kanban_home):
    """The closing-condition fixture: a throwaway container started under a card is gone after
    the card completes and one sweep runs. Skipped where docker is not usable."""
    try:
        ok = subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        pytest.skip("docker not available")
    name = f"kres-test-{uuid.uuid4().hex[:8]}"
    if subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "alpine:3", "sleep", "300"],
                      capture_output=True, timeout=120).returncode != 0:
        pytest.skip("cannot start alpine:3")
    try:
        with kbc.connect_closing() as conn:
            tid, _ = _running_card(conn)
            kres.kanban_run_resource(conn, tid, "container", name, "local")
            kb.complete_task(conn, tid, summary="ok")
            (act,) = kres.release_finished(conn, notify=_quiet)
            assert act["action"] == "stopped"
        ps = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"], capture_output=True, text=True)
        assert ps.stdout.strip() == ""
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
