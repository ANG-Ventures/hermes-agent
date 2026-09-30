"""no_worker (operator-only) card flag: assign/reassign/dispatch/migrate (t_bfda40dd)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Hermetic: a caller session in the env would trip the home-session guard
    # on these unhomed fixture cards before the no-worker gate is reached.
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    return home


def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return parser


def _run(*argv):
    return kc.kanban_command(_parser().parse_args(["kanban", *argv]))


def _flagged(conn, **kw):
    tid = kb.create_task(conn, title="operator-only", **kw)
    assert kb.set_no_worker(conn, tid, True)
    return tid


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def test_edit_flag_roundtrip_and_show(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee=None)
    assert _run("edit", tid, "--no-worker") == 0
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).no_worker is True
        assert "no_worker_set" in _kinds(conn, tid)
    assert "dispatch:  operator-only" in kc.run_slash(f"show {tid}")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["no_worker"] is True
    assert _run("edit", tid, "--worker-ok") == 0
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).no_worker is False
        assert "no_worker_cleared" in _kinds(conn, tid)
    assert "operator-only" not in kc.run_slash(f"show {tid}")


def test_assign_worker_refused(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="human")
    assert _run("assign", tid, "daedalus") == 1
    err = capsys.readouterr().err
    assert "no-worker" in err and "--worker-ok" in err
    assert len([ln for ln in err.splitlines() if "no-worker" in ln]) == 1
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "human" and task.no_worker is True


def test_assign_refused_even_with_operator(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="daedalus")
    assert _run("assign", tid, "daedalus", "--operator", "x: y") != 0
    assert "no-worker" in capsys.readouterr().err
    assert _run("reassign", tid, "cc-worker", "--operator", "x: y") != 0
    assert "no-worker" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        with pytest.raises(kb.NoWorkerFlagSet):
            kb.assign_task(conn, tid, "cc-worker", operator="apollo: sweep")
        with pytest.raises(kb.NoWorkerFlagSet):
            kb.reassign_task(conn, tid, "cc-worker", operator="apollo: sweep",
                             reclaim_first=True, receipt={})
    assert task.assignee == "daedalus" and task.no_worker is True


def test_reassign_refusal_happens_before_reclaim(kanban_home, monkeypatch):
    calls = []
    monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: calls.append(a) or True)
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="human")
        with pytest.raises(kb.NoWorkerFlagSet):
            kb.reassign_task(conn, tid, "daedalus", reclaim_first=True, receipt={})
    assert calls == []


def test_non_worker_targets_still_allowed(kanban_home):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="daedalus")
        assert kb.assign_task(conn, tid, "human:apollo")
        assert kb.assign_task(conn, tid, None)
        assert kb.get_task(conn, tid).no_worker is True


def test_worker_ok_assigns_and_clears(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="human")
    assert _run("assign", tid, "daedalus", "--worker-ok") == 0
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.assignee == "daedalus" and task.no_worker is False
        cleared = [e for e in kb.list_events(conn, tid) if e.kind == "no_worker_cleared"]
    assert len(cleared) == 1 and cleared[0].payload["source"] == "worker_ok"


def test_reassign_worker_ok(kanban_home):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="human")
    assert _run("reassign", tid, "cc-worker", "--worker-ok") == 0
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "cc-worker" and task.no_worker is False


def _spawn(*_a, **_k):
    return 12345


def test_dispatch_skips_flagged_card(kanban_home):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="default")
        ok_tid = kb.create_task(conn, title="normal", assignee="default")
        dry = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=True)
    spawned = [s[0] for s in dry.spawned]
    assert tid not in spawned and ok_tid in spawned
    assert dry.skipped_no_worker == [tid]
    with kb.connect_closing() as conn:
        assert "dispatch_skipped" not in _kinds(conn, tid)  # dry run writes nothing
        for _ in range(3):
            res = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
            assert tid not in [s[0] for s in res.spawned]
        task = kb.get_task(conn, tid)
        kinds = _kinds(conn, tid)
        skips = [e for e in kb.list_events(conn, tid) if e.kind == "dispatch_skipped"]
    assert task.status == "ready" and task.claim_lock is None
    assert "claimed" not in kinds and "spawned" not in kinds
    assert len(skips) == 1 and skips[0].payload == {"reason": "no_worker"}


def test_dispatch_skips_flagged_unassigned_card_without_default_assignee(kanban_home):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee=None)
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, dry_run=False, default_assignee="default",
        )
        task = kb.get_task(conn, tid)
    assert tid not in res.auto_assigned_default and task.assignee is None


def test_dispatch_dry_run_cli_never_lists_flagged(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _flagged(conn, assignee="default")
    capsys.readouterr()
    _run("dispatch", "--dry-run")
    assert tid not in capsys.readouterr().out
    _run("dispatch", "--dry-run", "--json")
    out = capsys.readouterr().out
    assert tid not in out and '"skipped_no_worker_count": 1' in out


def test_migrate_flags_cards_from_body_text(kanban_home):
    with kb.connect_closing() as conn:
        hit = kb.create_task(
            conn, title="ext", assignee="apollo",
            body="Apollo-owned. Do NOT dispatch a worker; blocked on external.",
        )
        miss = kb.create_task(conn, title="plain", assignee="daedalus", body="fix it")
        conn.execute("ALTER TABLE tasks DROP COLUMN no_worker")
    kb.init_db()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, hit).no_worker is True
        assert kb.get_task(conn, miss).no_worker is False
        # A later --worker-ok is not undone by re-running migrations.
        assert kb.assign_task(conn, hit, "daedalus", worker_ok=True)
    kb.init_db()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, hit).no_worker is False
