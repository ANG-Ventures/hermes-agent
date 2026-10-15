"""Hermetic continuation regressions (t_8bfdcc07)."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_host_capabilities as khc


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(kb.SCHEMA_SQL)
    yield c
    c.close()


def card(c, tid="t_11111111", origin="ace-media", body="requires: none", status="ready"):
    c.execute("INSERT INTO tasks(id,title,assignee,status,created_at,workspace_path,origin_host,body) "
              "VALUES(?,?,'alpha',?,1,'/missing',?,?)", (tid, tid, status, origin, body))
    return c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def route(c, row, state="nonempty", config=None, caps=None):
    pool = SimpleNamespace(pool_hosts=(SimpleNamespace(name="ace-media", profiles=("alpha",), absence="optional"),))
    return kc.origin_route(c, row, pool, config=config or {"placement": {"continuation": "enforce"}},
                           states={("ace-media", "/missing"): state}, caps=caps, dry_run=True, now=10000)


def test_remote_retry_has_origin_route(db):
    assert route(db, card(db)) == ("origin", "ace-media")


def test_linked_card_overrides_portability(db):
    row = card(db)
    card(db, "t_22222222", origin="ace-media")
    db.execute("INSERT INTO task_links VALUES('t_11111111','t_22222222','blocks')")
    assert route(db, row) == ("origin", "ace-media")


def test_provenance_never_creates_split_brain(db):
    row = card(db)
    card(db, "t_22222222", origin="ace-ai")
    db.execute("INSERT INTO task_links VALUES('t_11111111','t_22222222','derived-from')")
    assert route(db, row) == ("origin", "ace-media")


def test_all_links_checked_for_split_brain(db):
    row = card(db)
    for tid, host in (("t_22222222", "ace-media"), ("t_33333333", "ace-ai")):
        card(db, tid, origin=host)
        db.execute("INSERT INTO task_links VALUES(?,?,'blocks')", (row["id"], tid))
    assert "split brain" in route(db, row)[1]


def test_requires_checked_independently_of_capability_warn(db):
    ctx = khc.CapabilityTick(khc.CapabilityMap(capabilities=("browser-session",),
                             hosts={"ace-media": frozenset()}), "warn", (), ("ace-media",))
    assert "needs browser-session" in route(db, card(db, body="requires: browser-session"), caps=ctx)[1]


def test_off_ignores_origin(db):
    assert route(db, card(db), config={"placement": {"continuation": "off"}}) is None


def test_failed_probe_never_clears(db):
    assert route(db, card(db), state="unknown") == ("wait", "origin_host_absent")
    assert db.execute("SELECT origin_host FROM tasks").fetchone()[0] == "ace-media"


def test_host_key_mismatch_refuses_immediately(db):
    assert "re-imaged" in route(db, card(db), state="host_key_mismatch")[1]


def test_absence_wait_times_out(db):
    row = card(db)
    kb._append_event(db, row["id"], "placement_wait", {"host": "ace-media", "reason": "origin_host_absent", "since": 1})
    assert "absent for" in route(db, row, state="unknown")[1]


def test_positive_empty_proof_cas_notice(db):
    row = card(db)
    assert kc.clear_lost_state(db, row["id"], "ace-media")
    assert not kc.clear_lost_state(db, row["id"], "ace-media")
    assert db.execute("SELECT origin_host FROM tasks").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 1
    payload = json.loads(db.execute("SELECT payload FROM task_events WHERE kind='origin_state_lost'").fetchone()[0])
    assert payload == {"host": "ace-media", "last_run": None}


def test_placement_precedes_spawn_and_survives_crash(db):
    row = card(db, status="running", origin=None)
    db.execute("INSERT INTO task_runs(id,task_id,status,started_at) VALUES(1,?,'running',1)", (row["id"],))
    db.execute("UPDATE tasks SET current_run_id=1")
    assert kc.record_placement(db, row["id"], 1, "ace-media")
    db.execute("UPDATE tasks SET status='ready'")
    db.execute("UPDATE task_runs SET status='crashed',outcome='crashed'")
    assert db.execute("SELECT origin_host FROM tasks").fetchone()[0] == "ace-media"
    assert db.execute("SELECT host FROM task_runs").fetchone()[0] == "ace-media"
    assert not kc.record_placement(db, row["id"], 1, "ace-ai")


def test_backfill_latest_remote_event_nonterminal_only(db):
    for tid, status in (("t_11111111", "ready"), ("t_22222222", "done")):
        card(db, tid, origin=None, status=status)
        kb._append_event(db, tid, "worker_placed", {"host": "ace-ai"})
        kb._append_event(db, tid, "worker_placed", {"host": "ace-media"})
    assert kc.backfill_origin_hosts(db) == 1
    assert db.execute("SELECT origin_host FROM tasks WHERE status='ready'").fetchone()[0] == "ace-media"
    assert db.execute("SELECT origin_host FROM tasks WHERE status='done'").fetchone()[0] is None


def test_transfer_clears_parent_path_atomically(db):
    parent = card(db)
    child = card(db, "t_22222222", origin=None)
    with kb.write_txn(db):
        kc.transfer_workspace(db, parent["id"], child["id"])
    assert db.execute("SELECT workspace_path FROM tasks WHERE id=?", (parent["id"],)).fetchone()[0] is None
    assert db.execute("SELECT origin_host FROM tasks WHERE id=?", (child["id"],)).fetchone()[0] == 'ace-media'
    assert db.execute("SELECT kind FROM task_links").fetchone()[0] == 'derived-from'


def test_hint_is_durable_comment_and_event(db):
    parent = card(db)
    child = card(db, "t_22222222", origin=None)
    with kb.write_txn(db):
        kc.create_hints(db, child["id"], 'redo ' + parent["id"])
    assert 'recreate with --continues' in db.execute("SELECT body FROM task_comments").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM task_events WHERE kind='placement_hint'").fetchone()[0] == 1


def test_warn_deduplicates_observation(db):
    row = card(db)
    pool = SimpleNamespace(pool_hosts=())
    for _ in range(2):
        assert kc.origin_route(db, row, pool, config={'placement': {'continuation': 'warn'}}, states={}) is None
    assert db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_terminal_transition_clears_origin(db):
    card(db)
    kb._ensure_status_audit(db)
    db.execute("UPDATE tasks SET status='done'")
    assert db.execute("SELECT origin_host FROM tasks").fetchone()[0] is None


def test_host_lost_three_ticks_uses_termination_fence(db, monkeypatch):
    row = card(db, status='running')
    lock = kb._host_prefix() + 'fixture'
    db.execute("INSERT INTO task_runs(id,task_id,status,started_at,host) VALUES(1,?,'running',1,'ace-media')", (row['id'],))
    db.execute('UPDATE tasks SET current_run_id=1,worker_pid=12345,claim_lock=?', (lock,))
    calls = []
    monkeypatch.setattr(kb, '_worker_owner_window', lambda *a: None)
    monkeypatch.setattr(kb, '_terminate_reclaimed_worker', lambda *a, **kw: calls.append(kw) or {'terminated': True})
    assert kc.interrupt_lost_hosts(db, {}, now=100) == []
    assert kc.interrupt_lost_hosts(db, {}, now=100) == []
    assert kc.interrupt_lost_hosts(db, {}, now=160) == []
    assert kc.interrupt_lost_hosts(db, {}, now=220) == [row['id']]
    assert len(calls) == 1
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] == 'ace-media'
    assert db.execute('SELECT outcome FROM task_runs').fetchone()[0] == 'host_lost'


def test_probe_failure_and_host_key_do_not_prove_empty(db):
    row = card(db)
    host = SimpleNamespace(name='ace-media', target='kanbanw@example', ssh_port=22)
    pool = SimpleNamespace(pool_hosts=(host,))
    def runner(argv, **kw):
        assert "python3 -c" in argv[-1]
        return SimpleNamespace(returncode=255, stdout='', stderr='REMOTE HOST IDENTIFICATION HAS CHANGED')
    assert kc.probe_states(db, pool, runner=runner) == {('ace-media', '/missing'): 'host_key_mismatch'}


def test_dispatcher_origin_precedes_linked_classifier(db, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    row = card(db)
    card(db, 't_22222222', origin='ace-media')
    db.execute("INSERT INTO task_links VALUES('t_11111111','t_22222222','blocks')")
    host = kwp.PoolHost('ace-media', 'example', 'kanbanw', 2, .8, 'optional', ('alpha',), 'active', True, 1)
    pool = kwp.PoolConfig(hosts=('ace-media',), profiles=('alpha',), pool_hosts=(host,))
    plan = kwp.plan([host], {}, probe=lambda h: (1, 8), config=pool)
    plan.continuation_states = {('ace-media', '/missing'): 'nonempty'}
    monkeypatch.setattr(kbd, '_root_kanban_cfg', lambda: {'placement': {'continuation': 'enforce'}})
    assert kbd._route_row(db, row, plan) == ('origin', 'ace-media')


def test_create_continues_real_kernel(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda name: True)
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title='source', assignee='alpha', body='requires: screen')
        conn.execute('UPDATE tasks SET workspace_path=?,origin_host=? WHERE id=?',
                     (str(tmp_path / 'state'), 'ace-media', parent))
        child = kb.create_task(conn, title='successor', assignee='alpha', continues=parent)
        assert kb.get_task(conn, parent).workspace_path is None
        task = kb.get_task(conn, child)
        assert task.workspace_path == str(tmp_path / 'state')
        assert task.origin_host == 'ace-media'
        assert 'requires: screen' in task.body
        assert task.status == 'ready'


def test_full_dispatch_retry_uses_remote_origin(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(kbd, '_root_kanban_cfg', lambda: {'placement': {'continuation': 'enforce'}})
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda name: True)
    monkeypatch.setattr(kbd, '_system_memory_sample', lambda: {}, raising=False)
    host = kwp.PoolHost('ace-media', 'example', 'kanbanw', 2, .8, 'optional', ('alpha',), 'active', True, 1)
    pool = kwp.PoolConfig(hosts=('ace-media',), profiles=('alpha',), pool_hosts=(host,))
    plan = kwp.plan([host], {}, probe=lambda h: (1, 8), config=pool)
    monkeypatch.setattr(kc, 'probe_states', lambda *a: {('ace-media', str(tmp_path / 'workspace')): 'nonempty'})
    monkeypatch.setattr(kb._kwh, 'prepare_remote_workspace', lambda *a: None)
    seen = []
    def spawn(task, workspace, *, placement=None, board=None):
        seen.append(placement.name if placement else None)
        assert kb.get_task(conn, task.id).origin_host == 'ace-media'
        return None
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='origin retry', assignee='alpha', body='requires: none')
        conn.execute('UPDATE tasks SET origin_host=?,workspace_path=? WHERE id=?',
                     ('ace-media', str(tmp_path / 'workspace'), tid))
        result = kbd.dispatch_once(conn, spillover=plan, spawn_fn=spawn, max_spawn=1,
                                   reconcile_orphans=False)
        assert seen == ['ace-media'], result
        assert kb.get_task(conn, tid).origin_host == 'ace-media'
        assert conn.execute('SELECT host FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 'ace-media'


def test_migration_backfills_once_and_drop_survives_reconnect(db):
    from hermes_cli import kanban_db_connect as kbc
    row = card(db, origin=None)
    kb._append_event(db, row['id'], 'worker_placed', {'host': 'ace-media'})
    db.execute('ALTER TABLE tasks DROP COLUMN origin_host')
    kbc._migrate_add_optional_columns(db)
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] == 'ace-media'
    db.execute('UPDATE tasks SET origin_host=NULL')
    kbc._migrate_add_optional_columns(db)
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] is None


def test_remote_deleter_holds_unpreserved_file_and_removes_empty(db, tmp_path, monkeypatch):
    import shlex
    import subprocess
    from hermes_cli import kanban_survivor as ks
    card(db, status='done')
    workspace = tmp_path / 'quoted space'
    workspace.mkdir()
    sentinel = workspace / 'sentinel'
    sentinel.write_text('must survive')
    actual = subprocess.run
    def local_remote(argv, **kwargs):
        # Execute the exact remote payload, not a synthesized result.
        return actual(shlex.split(argv[-1]), **kwargs)
    monkeypatch.setattr(ks.subprocess, 'run', local_remote)
    host = SimpleNamespace(name='fixture', target='unused', ssh_port=22)
    assert not ks.remove_workspace_dir(db, 't_11111111', workspace, remote_host=host)
    assert sentinel.read_text() == 'must survive'
    sentinel.unlink()
    assert ks.remove_workspace_dir(db, 't_11111111', workspace, remote_host=host)
    assert not workspace.exists()


def test_drop_origin_is_not_resurrected_by_local_spawn(db):
    row = card(db)
    assert kc.drop_origin(db, row['id'], actor='operator', files=['sentinel'])
    db.execute("UPDATE tasks SET status='running',current_run_id=1")
    db.execute("INSERT INTO task_runs(id,task_id,status,started_at) VALUES(1,?,'running',1)", (row['id'],))
    assert kc.record_placement(db, row['id'], 1, None)
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] is None
    assert db.execute('SELECT host FROM task_runs').fetchone()[0] == 'studio'
