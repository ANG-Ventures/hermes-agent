"""Disposable-board acceptance for the inert authority (never the fleet board)."""
import json
import itertools
import multiprocessing
import os
import signal
import subprocess
import sys
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_failure_ownership as fo

pytestmark = pytest.mark.platforms('posix')


@pytest.fixture
def board(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv('HERMES_HOME', str(root / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    path = root / 'board' / 'kanban.db'
    assert path.resolve().is_relative_to(root)
    conn = kb.connect(path)
    yield conn
    conn.close()


def key(node='tests/test_x.py::test_A'):
    return json.dumps([1, 'fleet', '', 'github.com', 1, 2, '.github/workflows/ci.yml', [],
                       [['os', 'linux']], 'suite', 'unit:invocation', 'pytest-node', node],
                      ensure_ascii=False, separators=(',', ':'))


def test_disabled_does_not_create_or_wake(board):
    ref = fo.publish_evidence(board, b'capture', verifier=lambda data: fo.Observation(
        key=key(), source_key='source-1', result='failure', sha='a'*40,
        run_id=1, attempt=1, job_id=1, lineage=('a'*40,), policy_generation=1))
    card = fo.prepare_card(board, title='repair', body='bounded action; acceptance: test_x',
                           assignee=None, session_id='operator')
    result = fo.observe_failure(board, ref, producer='ci-red-owner', card_spec=card)
    assert result.action == 'DISABLED'
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert board.execute('SELECT count(*) FROM task_events').fetchone()[0] == 0


def enroll(conn):
    # Test-only direct policy seeding. Production enrollment has NO public API.
    with kb.write_txn(conn):
        conn.execute("UPDATE failure_producers SET enabled=1,enrolled=1")
        conn.execute("UPDATE failure_authority SET mode='on'")


def observation(source='red', result='failure', sha='a', **kw):
    return fo.Observation(key=key(), source_key=source, result=result, sha=sha*40,
                          run_id=kw.pop('run_id', 1), attempt=1, job_id=1,
                          lineage=('a'*40,'b'*40,'c'*40), policy_generation=1,
                          complete=result=='pass', **kw)


def publish(conn, obs):
    return fo.publish_evidence(conn, json.dumps(fo.asdict(obs)).encode(), verifier=lambda _:obs)


def admit(conn, obs, producer='ci-red-owner'):
    card = fo.prepare_card(conn,title='repair',body='bounded action; acceptance: test_x',
                           assignee=None,session_id='operator')
    return fo.observe_failure(conn,publish(conn,obs),producer=producer,card_spec=card)


def test_atomic_replay_distinct_and_claim(board):
    enroll(board)
    red = observation()
    first = admit(board,red)
    assert first.action=='REPAIR'
    assert admit(board,red).action=='REPLAY'
    second = admit(board,replace(red,source_key='red2'))
    assert second.card_id==first.card_id
    distinct = admit(board,replace(red,key=key('tests/test_x.py::test_a'),source_key='other'))
    assert distinct.card_id != first.card_id
    with kb.write_txn(board):
        board.execute('UPDATE failure_producers SET enabled=0,enrolled=0')
        assert fo.recheck_for_claim(board,first.card_id).action=='claim'
        assert fo.recheck_for_claim(board,first.card_id,expected_revision=-1).action=='retry'
        assert kb._claim_and_open_run(board,first.card_id,'ready','scratch:1',200,100)
    assert board.execute('SELECT count(*) FROM task_runs').fetchone()[0]==1


def test_healed_shared_claim_creates_zero_runs(board):
    enroll(board)
    red = admit(board,observation())
    heal = admit(board,observation('green','pass','b',run_id=2))
    assert heal.action=='HEALED'
    with kb.write_txn(board):
        assert fo.recheck_for_claim(board,red.card_id).action=='healed'
        assert kb._claim_and_open_run(board,red.card_id,'ready','scratch:1',200,100) is None
    assert board.execute('SELECT count(*) FROM task_runs').fetchone()[0]==0
    assert board.execute('SELECT workspace_path FROM tasks').fetchone()[0] is None


@pytest.mark.parametrize('order',list(itertools.permutations(range(3))))
def test_late_healing_boundary_and_recurrence(board,order):
    enroll(board)
    observations=[observation('A'),observation('B','pass','b',run_id=2),observation('C','failure','c',run_id=3)]
    for i in order:
        admit(board,observations[i])
    episodes=board.execute('SELECT state,healed_obs,predecessor FROM failure_episodes ORDER BY generation').fetchall()
    assert len(episodes)==2
    assert sorted(r['state'] for r in episodes)==['active','healed']
    assert board.execute("SELECT count(*) FROM failure_bindings b JOIN failure_episodes e USING(episode_id) WHERE b.state='bound' AND e.state='active'").fetchone()[0]==1
    count=board.execute('SELECT count(*) FROM tasks').fetchone()[0]
    assert admit(board,observations[0]).action=='REPLAY'
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==count


def test_invalid_receipts_missing_blob_and_stale_policy(board):
    ref=publish(board,observation())
    for forged in (True,fo.EvidenceRef('forged',ref.digest),fo.EvidenceRef(ref.token,'0'*64)):
        with pytest.raises(ValueError):
            fo.observe_failure(board,forged,producer='ci-red-owner')
    blob=fo._blob_root(board)/ref.digest
    blob.unlink()
    with pytest.raises(FileNotFoundError):
        fo.observe_failure(board,ref,producer='ci-red-owner')
    ref=publish(board,observation('again'))
    with kb.write_txn(board):
        board.execute('UPDATE failure_authority SET policy_generation=2')
    with pytest.raises(ValueError,match='stale evidence policy'):
        fo.observe_failure(board,ref,producer='ci-red-owner')
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0]==0


def test_unknown_force_push_and_skipped_preserve_failure(board):
    enroll(board)
    red=admit(board,observation())
    unknown=admit(board,replace(observation('skip'),result='unknown',complete=False,reason='skipped',retry_cursor='page:2'))
    assert unknown.action=='EVIDENCE_PENDING'
    assert board.execute('SELECT retry_state FROM failure_unresolved').fetchone()[0]
    admit(board,replace(observation('force','pass','b'),lineage=('b'*40,)))
    with kb.write_txn(board):
        assert fo.recheck_for_claim(board,red.card_id).action=='claim'
    with pytest.raises(ValueError,match='incomplete'):
        publish(board,replace(observation('badpass','pass','b'),complete=False))


def test_terminal_delete_reopen_and_adopt_cas(board):
    enroll(board)
    red=admit(board,observation())
    with kb.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError):
            board.execute('DELETE FROM tasks WHERE id=?',(red.card_id,))
        board.execute("UPDATE tasks SET status='done' WHERE id=?",(red.card_id,))
    pending=board.execute("SELECT * FROM failure_bindings WHERE state='pending'").fetchone()
    assert pending is not None
    replacement=admit(board,observation('replacement'))
    assert replacement.card_id != red.card_id
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?",(red.card_id,))
        assert fo.recheck_for_claim(board,red.card_id).action=='healed'
    assert fo.adopt_owner(board,replacement.episode_id,red.card_id,expected_revision=-1,provenance_ref=publish(board,observation('adopt'))).action=='retry'


def test_pending_validation_never_claims_owned(board):
    enroll(board)
    result=fo.observe_failure(board,publish(board,observation()),producer='ci-red-owner',card_spec=None)
    assert result.action=='EVIDENCE_PENDING' and result.card_id is None
    assert board.execute("SELECT count(*) FROM failure_bindings WHERE state='pending'").fetchone()[0]==1
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    card=fo.prepare_card(board,title='repair',body='action and acceptance',assignee=None,session_id='operator')
    results=fo.reconcile_pending(board,producer='ci-red-owner',card_spec=card)
    assert len(results)==1 and results[0].action=='REPAIR'
    assert fo.reconcile_pending(board,producer='ci-red-owner',card_spec=card)==()


@pytest.mark.parametrize('task_kind', ['repair', 'diagnostic'])
def test_detach_reconciles_without_new_observation(board, task_kind):
    enroll(board)
    obs = observation(task_kind=task_kind)
    original = admit(board, obs)
    binding = dict(board.execute("SELECT * FROM failure_bindings WHERE state='bound'").fetchone())
    detached = fo.detach_owner(board, original.episode_id,
                               expected_revision=original.revision, reason='replace owner')
    assert detached.action == 'EVIDENCE_PENDING' and detached.card_id is None
    pending = board.execute("SELECT * FROM failure_bindings WHERE state='pending'").fetchone()
    assert pending['provenance_json'] == binding['provenance_json']
    assert pending['task_kind'] == task_kind
    assert pending['detector_only'] == binding['detector_only']
    with kb.write_txn(board):
        board.execute("UPDATE failure_producers SET enabled=0 WHERE producer='ci-red-owner'")
    card = fo.prepare_card(board, title='replacement', body='action and acceptance',
                           assignee=None, session_id='operator')
    assert fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card) == ()
    with kb.write_txn(board):
        board.execute("UPDATE failure_producers SET enabled=1 WHERE producer='ci-red-owner'")
    assert admit(board, obs).action == 'REPLAY'
    replacement, = fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    assert replacement.action == task_kind.upper()
    assert replacement.episode_id == original.episode_id
    assert replacement.card_id != original.card_id
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0] == 1
    assert fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card) == ()


def test_pending_limit_is_per_producer(board, monkeypatch):
    enroll(board)
    for index in range(33):
        obs = replace(observation('backlog-' + str(index)), key=key('tests/test_backlog.py::test_' + str(index)))
        result = fo.observe_failure(board, publish(board, obs), producer='ci-red-owner')
        assert result.action == 'EVIDENCE_PENDING'
    target = replace(observation('target'), key=key('tests/test_target.py::test_target'))
    pending = fo.observe_failure(board, publish(board, target), producer='main-tip-red')
    card = fo.prepare_card(board, title='repair', body='action and acceptance',
                           assignee=None, session_id='operator')
    result, = fo.reconcile_pending(board, producer='main-tip-red', card_spec=card)
    assert result.episode_id == pending.episode_id and result.action == 'REPAIR'
    first = fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    assert len(first) == 32 and all(r.action == 'REPAIR' for r in first)
    second, = fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    assert second.action == 'REPAIR'
    assert second.episode_id not in {r.episode_id for r in first}
    assert board.execute("SELECT count(*) FROM failure_bindings WHERE state='pending'").fetchone()[0] == 0


def assert_responsibility(conn):
    for episode in conn.execute("SELECT * FROM failure_episodes WHERE state='active'"):
        bindings = conn.execute("SELECT * FROM failure_bindings WHERE episode_id=? AND state IN ('bound','pending')",
                                (episode['episode_id'],)).fetchall()
        assert len(bindings) == 1
        assert json.loads(bindings[0]['provenance_json']).get('producer') in fo.PRODUCERS


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
@pytest.mark.parametrize('task_kind', ['repair', 'diagnostic'])
def test_pending_boundary_continuity(board, order, task_kind):
    enroll(board)
    values = [observation('A', task_kind=task_kind),
              observation('B', 'pass', 'b', run_id=2),
              observation('C', 'failure', 'c', run_id=3, task_kind=task_kind)]
    for index in order:
        fo.observe_failure(board, publish(board, values[index]), producer='ci-red-owner')
        assert_responsibility(board)
    card = fo.prepare_card(board, title='repair', body='action and acceptance', assignee=None, session_id='operator')
    result, = fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    assert result.action == task_kind.upper()
    assert_responsibility(board)
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='healed'").fetchone()[0] == 1


@pytest.mark.parametrize('disabled_producer', ['ci-red-owner', 'main-tip-red'])
def test_disabled_replay_after_enrollment(board, disabled_producer):
    obs = observation()
    assert admit(board, obs, producer=disabled_producer).action == 'DISABLED'
    enroll(board)
    result = admit(board, obs)
    assert result.action == 'REPAIR'
    assert_responsibility(board)
    assert admit(board, obs).action == 'REPLAY'
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0] == 1


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_delayed_prefix_keeps_verified_boundary(board, order):
    enroll(board)
    values = [replace(observation('A'), lineage=('a'*40,)),
              observation('B', 'pass', 'b', run_id=2),
              observation('C', 'failure', 'c', run_id=3)]
    for index in order:
        admit(board, values[index])
        assert_responsibility(board)
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='healed'").fetchone()[0] == 1
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='active'").fetchone()[0] == 1


def test_repeated_adoption_terminal_has_one_replacement(board):
    enroll(board)
    obs = observation()
    result = admit(board, obs)
    for _ in range(2):
        result = fo.adopt_owner(board, result.episode_id, result.card_id,
                                expected_revision=result.revision, provenance_ref=publish(board, obs))
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='done' WHERE id=?", (result.card_id,))
        assert_responsibility(board)
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (result.card_id,))
        revision = board.execute('SELECT revision FROM failure_episodes WHERE episode_id=?', (result.episode_id,)).fetchone()[0]
        result = replace(result, revision=revision)


def test_incomparable_pass_does_not_resurrect_healed_red(board):
    enroll(board)
    admit(board, observation())
    admit(board, observation('B', 'pass', 'b', run_id=2))
    unrelated = replace(observation('unrelated', 'pass', 'x', run_id=3), lineage=('x'*40,))
    result = admit(board, unrelated)
    assert result.action == 'HEALED'
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='active'").fetchone()[0] == 0
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('task_kind', ['repair', 'diagnostic'])
def test_adoption_detach_keeps_routable_identity(board, task_kind):
    enroll(board)
    obs = observation(task_kind=task_kind)
    original = admit(board, obs)
    adopted_card = kb.create_task(board, title='human repair', session_id='operator', session_explicit=True)
    adopted = fo.adopt_owner(board, original.episode_id, adopted_card,
                             expected_revision=original.revision, provenance_ref=publish(board, obs))
    assert_responsibility(board)
    fo.detach_owner(board, adopted.episode_id, expected_revision=adopted.revision, reason='replace')
    assert_responsibility(board)
    card = fo.prepare_card(board, title='repair', body='action and acceptance', assignee=None, session_id='operator')
    result, = fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    assert result.action == task_kind.upper()
    assert_responsibility(board)


def test_publication_failure_no_receipt_or_owner(board,monkeypatch):
    def fail(*args):
        raise OSError('injected durable publication failure')
    monkeypatch.setattr(fo.os,'replace',fail)
    with pytest.raises(OSError,match='durable publication'):
        publish(board,observation())
    assert board.execute('SELECT count(*) FROM failure_receipts').fetchone()[0]==0
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0]==0
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==0


def test_legacy_writer_floor(board):
    # Old ordinary semantics remain unchanged until a producer is enrolled.
    with kb.write_txn(board):
        kb._insert_task_row(board,dict(id='old',title='old',status='ready',created_at=1,created_by='ci-red-owner'))
    enroll(board)
    with pytest.raises(sqlite3.IntegrityError,match='incompatible writer'):
        with kb.write_txn(board):
            kb._insert_task_row(board,dict(id='bypass',title='bad',status='ready',created_at=1,created_by='ci-red-owner'))
    assert admit(board,observation()).action=='REPAIR'


# Census (hermes-home scripts/lib/failure_producer_census.md): every real caller identity and legacy key namespace.
LEGACY_MINTS = (
    dict(created_by='main-tip-red'), dict(created_by='main-tip-red-rollup'), dict(created_by='landing-queue'),
    dict(created_by='ci-red-owner'), dict(created_by='main-green-streak'),
    dict(idempotency_key='main-tip-red:o/r/u/t:abcdef123456'), dict(idempotency_key='main-tip-red-rollup:standing'),
    dict(signature='main-tip-red-rollup:standing'), dict(idempotency_key='ci-red-owner:o/r@abc'),
    dict(idempotency_key='main-red:fam:2026-11-14'), dict(idempotency_key='ci-advisory-landed:o/r@abc'),
)


def test_mixed_writer_cannot_bypass_authority_while_mode_on(board):
    """S5 item 4 (t_b8c1b185): with mode != preserve, a legacy direct mint (no core protocol marker) by any census
    identity or namespace is refused even before any producer is enrolled; preserve mode keeps legacy semantics; the
    core admission path still creates."""
    for i, extra in enumerate(LEGACY_MINTS):
        with kb.write_txn(board):
            kb._insert_task_row(board, dict(id='pre%d' % i, title='old', status='ready', created_at=1, **extra))
    with kb.write_txn(board):
        board.execute("UPDATE failure_authority SET mode='on'")
    assert board.execute('SELECT count(*) FROM failure_producers WHERE enrolled=1').fetchone()[0] == 0
    for i, extra in enumerate(LEGACY_MINTS):
        with pytest.raises(sqlite3.IntegrityError, match='incompatible writer'):
            with kb.write_txn(board):
                kb._insert_task_row(board, dict(id='bypass%d' % i, title='bad', status='ready', created_at=1,
                                                **extra))
    human = kb.create_task(board, title='human project', session_id='operator', session_explicit=True)
    assert kb.get_task(board, human) is not None              # unregistered creates are outside the floor
    enroll(board)
    assert admit(board, observation()).action == 'REPAIR'     # the authority path carries the marker
    assert board.execute("SELECT count(*) FROM tasks WHERE id LIKE 'bypass%'").fetchone()[0] == 0


def _race_worker(path,root,index,barrier,queue):
    assert Path(path).resolve().is_relative_to(Path(root).resolve())
    conn=kb.connect(Path(path))
    try:
        obs=observation('race-'+str(index))
        ref=publish(conn,obs)
        card=fo.prepare_card(conn,title='repair',body='action and acceptance',assignee=None,session_id='operator')
        barrier.wait(timeout=90)
        result=fo.observe_failure(conn,ref,producer=fo.PRODUCERS[index%4],card_spec=card)
        queue.put((result.action,result.card_id))
    finally:
        conn.close()


def test_real_32_process_four_producer_race(board):
    enroll(board)
    path=board.execute('PRAGMA database_list').fetchone()[2]
    root=str(Path(path).parent)
    ctx=multiprocessing.get_context('spawn')
    barrier=ctx.Barrier(32)
    queue=ctx.Queue()
    children=[ctx.Process(target=_race_worker,args=(path,root,i,barrier,queue)) for i in range(32)]
    try:
        for child in children: child.start()
        results=[queue.get(timeout=150) for _ in children]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode==0
        assert len({r[1] for r in results})==1
        assert sum(r[0]=='REPAIR' for r in results)==1
        assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0]==32
        assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        print('32 processes / 4 producer labels: one card, 32 observations; parent pid',os.getpid())
    finally:
        for child in children:
            if child.is_alive(): child.kill()
            child.join(timeout=5)


def _crash_writer(path, root, boundary):
    assert Path(path).resolve().is_relative_to(Path(root).resolve())
    conn=kb.connect(Path(path))
    obs=observation('crash')
    ref=publish(conn,obs)
    card=fo.prepare_card(conn,title='repair',body='action and acceptance',assignee=None,session_id='operator')
    def trace(sql):
        normalized=' '.join(sql.upper().split())
        if boundary in normalized:
            os.kill(os.getpid(),signal.SIGKILL)
    conn.set_trace_callback(trace)
    fo.observe_failure(conn,ref,producer='ci-red-owner',card_spec=card)
    if boundary == 'REPLY_LOSS':
        os.kill(os.getpid(),signal.SIGKILL)  # reply loss after commit
    raise AssertionError('requested SQL crash boundary was never reached: ' + boundary)


@pytest.mark.parametrize('boundary',[
    'INSERT OR IGNORE INTO FAILURE_KEYS', 'INSERT INTO FAILURE_OBSERVATIONS',
    'INSERT INTO FAILURE_EPISODES', 'INSERT OR IGNORE INTO FAILURE_MEMBERSHIPS',
    'INSERT INTO FAILURE_BINDINGS', 'INSERT INTO TASKS', 'INSERT INTO TASK_EVENTS',
    "UPDATE FAILURE_BINDINGS SET CARD_ID", 'INSERT INTO FAILURE_TRANSITIONS',
    'UPDATE FAILURE_OBSERVATIONS SET DISPOSITION', 'COMMIT', 'REPLY_LOSS',
])
def test_sigkill_transaction_boundary_retry(board,boundary):
    enroll(board)
    path=board.execute('PRAGMA database_list').fetchone()[2]
    ctx=multiprocessing.get_context('spawn')
    child=ctx.Process(target=_crash_writer,args=(path,str(Path(path).parent),boundary))
    try:
        child.start()
        child.join(timeout=90)
        assert child.exitcode==-signal.SIGKILL
        result=admit(board,observation('crash'))
        assert result.action in ('REPAIR','REPLAY')
        assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0]==1
        assert board.execute("SELECT count(*) FROM failure_bindings WHERE state='bound'").fetchone()[0]==1
        assert board.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally:
        if child.is_alive(): child.kill()
        child.join(timeout=5)


def test_diagnostic_separate_key_and_known_red_with_partial_read(board):
    enroll(board)
    red=admit(board,observation())
    diagnostic=admit(board,replace(observation('infra'),key=key('tests/test_infra.py::runner_loss'),task_kind='diagnostic'))
    assert diagnostic.action=='DIAGNOSTIC' and diagnostic.card_id!=red.card_id
    admit(board,replace(observation('partial'),result='unknown',reason='HTTP 502'))
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='active'").fetchone()[0]==2
    assert board.execute("SELECT task_kind FROM failure_bindings WHERE card_id=?",(diagnostic.card_id,)).fetchone()[0]=='diagnostic'


def test_legacy_create_semantics_unchanged(board):
    card=kb.create_task(board,title='human project',session_id='operator',session_explicit=True)
    task = kb.get_task(board,card)
    assert task is not None and task.title=='human project'
    assert board.execute('SELECT count(*) FROM failure_bindings').fetchone()[0]==0


def test_raw_unupgraded_writer_inert_and_floor(board):
    path=board.execute('PRAGMA database_list').fetchone()[2]
    old=sqlite3.connect(path)
    try:
        old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('manual','manual','ready',1)")
        old.commit()
        enroll(board)
        with pytest.raises(sqlite3.IntegrityError,match='incompatible writer'):
            old.execute("INSERT INTO tasks(id,title,status,created_at,created_by) VALUES('old-ci','old','ready',1,'ci-red-owner')")
        old.rollback()
        assert board.execute('SELECT count(*) FROM failure_write_context').fetchone()[0]==0
    finally:
        old.close()


def test_public_claim_after_healing_no_allocation(board):
    enroll(board)
    red=admit(board,observation())
    admit(board,observation('pass','pass','b',run_id=2))
    assert red.card_id is not None
    assert kb.claim_task(board,red.card_id,claimer='scratch:1') is None
    assert board.execute('SELECT count(*) FROM task_runs').fetchone()[0]==0


def test_owner_create_refusal_is_durable_pending(board,monkeypatch):
    enroll(board)
    def refuse(conn,row):
        raise ValueError('test refusal')
    monkeypatch.setattr(kb,'_insert_task_row',refuse)
    result=admit(board,observation())
    assert result.action=='EVIDENCE_PENDING' and result.card_id is None
    assert board.execute("SELECT reason FROM failure_bindings WHERE state='pending'").fetchone()[0]
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0]==0


@pytest.mark.slow_test(200, reason='bounded 180-second SIGKILL/32-process acceptance subprocess')
@pytest.mark.parametrize('arm',['atomic','crash'])
def test_proof_entrypoint_real_execution(tmp_path,arm):
    root=tmp_path/'proof'
    script=Path(__file__).resolve().parents[2]/'scripts'/'failure-ownership-proof.py'
    proc=subprocess.run([sys.executable,str(script),'--arm',arm,'--scratch',str(root)],
                         stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=190)
    assert proc.returncode==0,proc.stdout+proc.stderr+(root/'output.log').read_text()
    summary=json.loads((root/'summary.json').read_text())
    assert summary['exit_code']==0 and summary['pid']>0
    print(json.dumps(summary))


@pytest.mark.parametrize('policy', [True, False])
def test_owner_card_boomerang_wake_follows_policy(board, monkeypatch, policy):
    """A failure-owner card is a card producer too: never a NULL wake level (t_c24b1cc7)."""
    from hermes_cli import kanban_boomerang_default as kbd
    monkeypatch.setattr(kbd, 'policy_default', lambda: policy)
    enroll(board)
    first = admit(board, observation())
    assert first.action == 'REPAIR'
    stored = board.execute('SELECT boomerang_wake FROM tasks WHERE id=?', (first.card_id,)).fetchone()[0]
    assert stored == (1 if policy else 0)
    payload = json.loads(board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (first.card_id,)).fetchone()[0])
    assert payload['boomerang_wake'] is policy
    assert payload['boomerang_wake_source'] == kbd.SOURCE_DEFAULT
