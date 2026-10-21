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
    src = db.execute("SELECT workspace_path,origin_host FROM tasks WHERE id=?", (parent["id"],)).fetchone()
    # The source can never re-resolve to the path it gave away, nor wait on its origin.
    assert src[0] not in (None, '/missing') and src[1] is None
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
    assert kc.interrupt_lost_hosts(db, {}, hosts={'ace-ai'}, now=220) == []
    assert kc.interrupt_lost_hosts(db, {}, now=220) == [row['id']]
    assert len(calls) == 1
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] == 'ace-media'
    assert db.execute('SELECT outcome FROM task_runs').fetchone()[0] == 'host_lost'


def _pool_host(name='ace-media', slots=2):
    from hermes_cli import kanban_worker_pool as kwp
    return kwp.PoolHost(name, 'example', 'kanbanw', slots, .8, 'optional', ('alpha',), 'active', True, 1)


def test_host_key_change_rides_pool_probe_and_never_proves_empty(db):
    from hermes_cli import kanban_worker_pool as kwp
    calls = []

    def runner(argv, **kw):
        calls.append(argv[-1])
        return SimpleNamespace(returncode=255, stdout='', stderr='REMOTE HOST IDENTIFICATION HAS CHANGED')
    host = _pool_host()
    plan = kwp.plan([host], {}, probe=lambda h, **kw: kwp.probe_host(h, runner, **kw),
                    workspace_paths={'ace-media': ['/missing']})
    assert len(calls) == 1 and kwp.STATES_MARKER in calls[0] and 'loadavg' in calls[0]
    assert plan.continuation_states == {('ace-media', '/missing'): 'host_key_mismatch'}
    assert plan.slots['ace-media'] == 0
    assert 're-imaged' in route(db, card(db), state=plan.continuation_states[('ace-media', '/missing')])[1]


def test_unreachable_probe_leaves_state_unknown(db):
    from hermes_cli import kanban_worker_pool as kwp
    plan = kwp.plan([_pool_host()], {}, probe=lambda h, **kw: None,
                    workspace_paths={'ace-media': ['/missing']})
    assert plan.continuation_probed and plan.continuation_states == {}
    row = card(db)
    assert kc.origin_route(db, row, plan.config, config={'placement': {'continuation': 'enforce'}},
                           states=plan.continuation_states, dry_run=True, now=10000) == ('wait', 'origin_host_absent')


def test_full_host_is_probed_for_state_but_gains_no_slot():
    from hermes_cli import kanban_worker_pool as kwp
    seen = []

    def probe(h, workspace_paths=()):
        seen.append(tuple(workspace_paths))
        return kwp.HostSample(0.1, 32, workspace_states={p: 'nonempty' for p in workspace_paths})
    plan = kwp.plan([_pool_host(slots=1)], {'ace-media': 1}, probe=probe,
                    workspace_paths={'ace-media': ['/ws/a']})
    assert seen == [('/ws/a',)]
    assert plan.slots['ace-media'] == 0
    assert plan.continuation_states == {('ace-media', '/ws/a'): 'nonempty'}


def test_state_script_classifies_real_paths(tmp_path):
    """Run the exact shell payload the probe sends (states half) locally."""
    import subprocess
    from hermes_cli import kanban_worker_pool as kwp
    board = tmp_path / "board root"
    board.mkdir()
    full = board / "t_full 'q'"
    full.mkdir()
    (full / 'sentinel').write_text('x')
    empty = board / 't_empty'
    empty.mkdir()
    gone = board / 't_gone'
    unmounted = tmp_path / 'no-board' / 't_x'
    paths = [str(full), str(empty), str(gone), str(unmounted)]
    proc = subprocess.run(['/bin/sh', '-c', kwp._states_cmd(paths)], capture_output=True, text=True)
    marker, _, body = proc.stdout.partition('\n')
    assert marker == kwp.STATES_MARKER
    assert json.loads(body) == {str(full): 'nonempty', str(empty): 'empty',
                                str(gone): 'empty', str(unmounted): 'unknown'}


def test_probe_host_parses_states_in_one_round_trip():
    from hermes_cli import kanban_worker_pool as kwp

    def runner(argv, **kw):
        out = '0.5 0.4 0.3 1/2 3\n32\n' + kwp.STATES_MARKER + '\n' + json.dumps({'/ws/a': 'empty', '/ws/b': 'bogus'})
        return SimpleNamespace(returncode=0, stdout=out, stderr='')
    got = kwp.probe_host(_pool_host(), runner, workspace_paths=('/ws/a', '/ws/b'))
    assert (got.load1, got.ncpu) == (0.5, 32)
    assert got.workspace_states == {'/ws/a': 'empty', '/ws/b': 'unknown'}


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
        assert kb.get_task(conn, parent).workspace_path not in (None, str(tmp_path / 'state'))
        assert kb.get_task(conn, parent).origin_host is None
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
    ws = str(tmp_path / 'workspace')
    plan = kwp.plan([host], {}, probe=lambda h, workspace_paths=(): kwp.HostSample(
        1, 8, workspace_states={p: 'nonempty' for p in workspace_paths}), config=pool,
        workspace_paths={'ace-media': [ws]})
    monkeypatch.setattr(kb._kwh, 'prepare_remote_workspace', lambda *a: None)
    seen = []
    def spawn(task, workspace, *, placement=None, board=None):
        seen.append(placement.name if placement else None)
        assert kb.get_task(conn, task.id).origin_host == 'ace-media'
        return 4242
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='origin retry', assignee='alpha', body='requires: none')
        conn.execute('UPDATE tasks SET origin_host=?,workspace_path=? WHERE id=?',
                     ('ace-media', str(tmp_path / 'workspace'), tid))
        result = kbd.dispatch_once(conn, spillover=plan, spawn_fn=spawn, max_spawn=1,
                                   reconcile_orphans=False)
        assert seen == ['ace-media'], result
        assert kb.get_task(conn, tid).origin_host == 'ace-media'
        assert conn.execute('SELECT host FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 'ace-media'
        placement = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='placement'", (tid,)).fetchone()[0])
        assert (placement['route'], placement['rule'], placement['host']) == ('remote-pin', 'origin', 'ace-media')


def test_migration_backfills_once_and_drop_survives_reconnect(db):
    from hermes_cli import kanban_db_connect as kbc
    row = card(db, origin=None)
    kb._append_event(db, row['id'], 'worker_placed', {'host': 'ace-media'})
    # Rebuild a legacy table without the column. DROP COLUMN fails on
    # SQLite < 3.47 ("incomplete input") for this schema's last column.
    cols = ','.join(r[1] for r in db.execute('PRAGMA table_info(tasks)') if r[1] != 'origin_host')
    db.executescript(f'CREATE TABLE legacy AS SELECT {cols} FROM tasks; DROP TABLE tasks; '
                     'ALTER TABLE legacy RENAME TO tasks;')
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
    assert db.execute('SELECT workspace_path FROM tasks').fetchone()[0].startswith('/missing.fresh-')
    db.execute("UPDATE tasks SET status='running',current_run_id=1")
    db.execute("INSERT INTO task_runs(id,task_id,status,started_at) VALUES(1,?,'running',1)", (row['id'],))
    assert kc.record_placement(db, row['id'], 1, None)
    assert db.execute('SELECT origin_host FROM tasks').fetchone()[0] is None
    assert db.execute('SELECT host FROM task_runs').fetchone()[0] == 'studio'


def test_gateway_plan_pool_reads_origin_state_in_the_load_probe(tmp_path, monkeypatch):
    """The tick's ONE pool plan asks origin hosts for their live workspaces,
    and a ready origin card alone justifies the plan under enforce."""
    from gateway.kanban_gate_tick import GateTickBuilder
    from hermes_cli.kanban_load_gate import LoadGate
    from hermes_cli import kanban_worker_pool as kwp
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda name: True)
    fleet = home / 'kanban' / 'fleet'
    fleet.mkdir(parents=True)
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({'schema': 1, 'hosts': {
        'ace-media': {'roles': {'kanban-worker': {'slots': 4}}, 'state': 'active'}}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        'schema': 1, 'ssh_user': 'kanbanw', 'capacity_pct': 0.8, 'priority': ['ace-media'],
        'profiles': ['alpha'], 'hosts': {'ace-media': {'enabled': True, 'absence': 'optional'}}}))
    ws = str(tmp_path / 'ws')
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='origin', assignee='alpha', body='requires: none')
        conn.execute('UPDATE tasks SET origin_host=?,workspace_path=? WHERE id=?', ('ace-media', ws, tid))
    asked = []

    def probe(h, workspace_paths=()):
        asked.append((h.name, tuple(workspace_paths)))
        return kwp.HostSample(1.0, 16, workspace_states={p: 'nonempty' for p in workspace_paths})
    cfg = {'placement': {'read_signal': False, 'continuation': 'enforce'},
           'worker_pool': {'overflow': False}}
    gate = LoadGate({}, ncpu=32)
    gate.band, gate.remote_allowed = 'admitting', True
    builder = GateTickBuilder(gate, fleet_dir=lambda: fleet, kanban_cfg=lambda: cfg, ledger=lambda b: {},
                              connect=lambda board=None: kb.connect(board=board), probe=probe)
    plan = builder.plan_pool([{'slug': 'default'}], {})
    assert plan is not None, gate.pool
    assert asked == [('ace-media', (ws,))]
    assert plan.continuation_probed and plan.continuation_states == {('ace-media', ws): 'nonempty'}
    # continuation off: remote-first (#2148) still plans for the ready card, but
    # no origin workspace rides the probe.
    cfg['placement']['continuation'] = 'off'
    asked.clear()
    plan = builder.plan_pool([{'slug': 'default'}], {})
    assert plan is not None and not plan.continuation_probed and asked == [('ace-media', ())]
    # Overflow admission on and NOTHING ready: a running origin card alone
    # still gets its host probed (HOST_LOST reads this plan).
    cfg['placement']['continuation'] = 'enforce'
    cfg['worker_pool'] = {'overflow': True}
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    asked.clear()
    plan = builder.plan_pool([{'slug': 'default'}], {})
    assert plan is not None and asked == [('ace-media', (ws,))], gate.pool


def test_state_script_symlink_and_failed_interpreter(tmp_path):
    """A dangling symlink is not proof of loss; a host that answered but whose
    state script failed reads unknown (never unreachable, never empty)."""
    import subprocess
    from hermes_cli import kanban_worker_pool as kwp
    board = tmp_path / 'board'
    board.mkdir()
    link = board / 't_link'
    link.symlink_to(tmp_path / 'not-mounted')
    proc = subprocess.run(['/bin/sh', '-c', kwp._states_cmd([str(link)])], capture_output=True, text=True)
    assert json.loads(proc.stdout.partition('\n')[2]) == {str(link): 'unknown'}

    def runner(argv, **kw):
        return SimpleNamespace(returncode=0, stdout='0.5 0.4 0.3 1/2 3\n32\n' + kwp.STATES_MARKER + '\n', stderr='')
    got = kwp.probe_host(_pool_host(), runner, workspace_paths=('/ws/a',))
    assert got.workspace_states == {'/ws/a': 'unknown'}


def test_host_lost_not_counted_while_host_answers_unknown(db, monkeypatch):
    row = card(db, status='running')
    lock = kb._host_prefix() + 'fixture'
    db.execute("INSERT INTO task_runs(id,task_id,status,started_at,host) VALUES(1,?,'running',1,'ace-media')", (row['id'],))
    db.execute('UPDATE tasks SET current_run_id=1,worker_pid=12345,claim_lock=?', (lock,))
    monkeypatch.setattr(kb, '_terminate_reclaimed_worker', lambda *a, **kw: pytest.fail('must not terminate'))
    for now in (100, 160, 220, 280):
        assert kc.interrupt_lost_hosts(db, {('ace-media', '/missing'): 'unknown'}, now=now) == []


def test_remote_owner_check_refuses_ancestor_workspace(db, tmp_path, monkeypatch):
    from hermes_cli import kanban_survivor as ks
    root = tmp_path / 'root'
    card(db, 't_11111111', status='done')
    db.execute("UPDATE tasks SET workspace_path=? WHERE id='t_11111111'", (str(root / 'outer' / 'inner'),))
    card(db, 't_22222222', status='blocked')
    db.execute("UPDATE tasks SET workspace_path=? WHERE id='t_22222222'", (str(root / 'outer'),))
    monkeypatch.setattr(kb, '_is_managed_scratch_path', lambda p: True)
    monkeypatch.setattr(ks, 'remove_workspace_dir', lambda *a, **kw: pytest.fail('must not reach the deleter'))
    host = SimpleNamespace(name='fixture', target='unused', ssh_port=22)
    assert not kb.safe_remove_workspace_dir(root / 'outer' / 'inner', task_id='t_11111111', reason='t',
                                            conn=db, remote_host=host)


def test_remote_deleter_holds_stash_and_unpushed_branch(db, tmp_path, monkeypatch):
    import shlex
    import subprocess
    from pathlib import Path
    from hermes_cli import kanban_survivor as ks
    card(db, status='done')
    ws = tmp_path / 'ws'
    fixture = Path(__file__).with_name('fixtures_continuation_unpushed.sh')
    subprocess.run(['/bin/sh', str(fixture), str(tmp_path / 'origin.git'), str(ws)], check=True,
                   capture_output=True)
    actual = subprocess.run
    monkeypatch.setattr(ks.subprocess, 'run', lambda argv, **kw: actual(shlex.split(argv[-1]), **kw))
    host = SimpleNamespace(name='fixture', target='unused', ssh_port=22)
    assert not ks.remove_workspace_dir(db, 't_11111111', ws, remote_host=host)
    assert (ws / 'f').exists()


def test_browser_gate_reads_origin_route_as_remote():
    from hermes_cli import kanban_db_dispatch as kbd
    assert kbd._pin_host(('origin', 'ace-ai')) == 'ace-ai'
    assert kbd._pin_host(('portable', None)) == 'studio'


@pytest.fixture
def mounted_home(tmp_path, monkeypatch):
    """A real board under the live config shape: workspaces_root_require_mount=true."""
    import os
    from pathlib import Path
    import yaml
    for key in tuple(os.environ):
        if key.startswith('HERMES_KANBAN_'):
            monkeypatch.delenv(key)
    monkeypatch.delenv('HERMES_SESSION_ID', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'kanban': {
        'workspaces_root': str(root), 'workspaces_root_require_mount': True}}))
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    kb.init_db()
    return root


def _spawned(conn, task_id):
    from hermes_cli import kanban_db_dispatch as kbd
    calls = []
    result = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, **kw: calls.append((task.id, ws)))
    assert not result.stranded_by_mount_loss, result.stranded_by_mount_loss
    assert not result.workspace_refused, result.workspace_refused
    return dict(calls).get(task_id)


def test_drop_origin_fresh_path_spawns_under_require_mount(mounted_home):
    """Prism 999501f3822b: the fresh path must exist, or admission strands it."""
    old = mounted_home / 'default' / 't_old'
    old.mkdir(parents=True)
    (old / 'sentinel').write_text('left behind')
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='drop origin', assignee='default',
                             workspace_kind='scratch', workspace_path=str(old))
        conn.execute("UPDATE tasks SET origin_host='ace-media' WHERE id=?", (tid,))
        assert kc.drop_origin(conn, tid, actor='operator', files=['sentinel'])
        fresh = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (tid,)).fetchone()[0]
        assert fresh != str(old)
        assert _spawned(conn, tid) == fresh
    assert (old / 'sentinel').read_text() == 'left behind'


def test_continues_source_fresh_path_spawns_under_require_mount(mounted_home):
    shared = mounted_home / 'default' / 't_src'
    shared.mkdir(parents=True)
    with kb.connect_closing() as conn:
        src = kb.create_task(conn, title='source', assignee='default',
                             workspace_kind='scratch', workspace_path=str(shared))
        child = kb.create_task(conn, title='successor', assignee='default', continues=src)
        rows = dict(conn.execute('SELECT id,workspace_path FROM tasks').fetchall())
        assert rows[child] == str(shared) and rows[src] != str(shared)
        assert _spawned(conn, src) == rows[src]


# Prism bbd09df624d7: the dispatcher returns origin_route before portable(),
# so the access-bound rule (spec §2.6) must hold on the origin path too.
@pytest.mark.parametrize("body", [
    "requires: control-plane-read\nRead the board.\n",
    "sqlite3 -readonly ~/.hermes/kanban.db 'select 1'\n",
])
@pytest.mark.parametrize("with_caps", [True, False])
def test_access_bound_continuation_never_routes_to_origin(db, body, with_caps):
    """RED ON BASE (8eb418ee) for every caps arm except requires+no-map."""
    ctx = khc.CapabilityTick(khc.CapabilityMap(capabilities=("control-plane",),
                             hosts={"ace-media": frozenset()}), "enforce", (), ("ace-media",)) \
        if with_caps else None
    res = route(db, card(db, body=body), caps=ctx)
    assert res is not None and res[0] == "wait", res
    assert res != ("origin", "ace-media")


def test_access_bound_origin_reason_names_the_access(db):
    res = route(db, card(db, body="cat ~/.hermes/fleet/kanban-pool.json\n"))
    assert res[0] == "wait" and "access control-plane-read is Studio-only" in res[1]
