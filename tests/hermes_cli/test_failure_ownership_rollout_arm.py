"""S5 rollout arm (t_b8c1b185): the real authority core on a scratch board, driven by hermes-home's operator CLI.

Rollback drill (spec D10): enroll + on, the core mints one failure, `failure-ownership rollback`, then nothing is lost
(every tasks / failure_* row count unchanged) and no further producer write happens (observe_failure -> DISABLED,
a legacy direct mint is refused by the compatibility floor, the existing repair still dispatches). Migration on the
same board: a card the authority owns is not re-imported as a legacy record, and a re-run imports 0.

Driven by `scripts/failure-ownership-proof.py --arm rollout --home-repo <hermes-home>` (FAILURE_HOME_REPO). Never
opens a fleet board: HERMES_HOME is a tmp dir.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_failure_ownership as fo

pytestmark = pytest.mark.platforms('posix')
LINEAGE = ('a' * 40, 'b' * 40)


def key(node):
    return json.dumps([1, 'fleet', '', 'github.com', 1, 2, '.github/workflows/ci.yml', [], [['os', 'linux']],
                       'suite', 'unit:invocation', 'pytest-node', node], ensure_ascii=False, separators=(',', ':'))


def admit(conn, source, node, producer='main-tip-red'):
    obs = fo.Observation(key=key(node), source_key=source, result='failure', sha='a' * 40, run_id=1, attempt=1,
                         job_id=1, lineage=LINEAGE, policy_generation=1)
    ref = fo.publish_evidence(conn, json.dumps(fo.asdict(obs)).encode(), verifier=lambda _: obs)
    card = fo.prepare_card(conn, title='repair %s' % source, body='bounded action; acceptance: %s' % node,
                           assignee='alice', session_id='operator')
    return fo.observe_failure(conn, ref, producer=producer, card_spec=card)


def home(*args, db):
    repo = os.environ.get('FAILURE_HOME_REPO')
    if not repo:
        pytest.skip('FAILURE_HOME_REPO unset: run through failure-ownership-proof.py --arm rollout --home-repo')
    return subprocess.run([sys.executable, str(Path(repo) / 'scripts' / 'failure-ownership'), *args, '--db', str(db)],
                          capture_output=True, text=True, timeout=120)


def counts(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND (name='tasks' OR name GLOB 'failure_*')")]
    return {n: conn.execute('SELECT count(*) FROM "%s"' % n).fetchone()[0] for n in sorted(names)}


@pytest.fixture
def board(tmp_path, monkeypatch, all_assignees_spawnable):
    root = tmp_path.resolve()
    monkeypatch.setenv('HERMES_HOME', str(root / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    conn = kb.connect(root / 'board' / 'kanban.db')
    assert Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve().is_relative_to(root)
    yield conn
    conn.close()


def test_rollback_drill_keeps_every_row_and_stops_producer_writes(board, tmp_path):
    db = board.execute('PRAGMA database_list').fetchone()[2]
    # Stands in for `failure-ownership enroll main-tip-red`: its real proof read refuses every producer today (no
    # census call site routes through observe_failure yet); the home suite proves that refusal.
    with kb.write_txn(board):
        board.execute("UPDATE failure_producers SET enrolled=1 WHERE producer='main-tip-red'")
    r = home('toggle', 'on', db=db)                                       # every producer enabled, mode=on
    line = next(l for l in r.stdout.splitlines() if l.startswith('main-tip-red'))
    assert r.returncode == 0 and ' ON ' in line, r.stdout + r.stderr       # enrolled: no ON* marker
    minted = admit(board, 'red', 'tests/test_x.py::test_A')
    assert minted.action == 'REPAIR' and minted.card_id
    before = counts(board)
    r = home('rollback', db=db)
    assert r.returncode == 0, r.stderr
    assert counts(board) == before                                         # D10: nothing lost
    assert board.execute('SELECT mode FROM failure_authority').fetchone()[0] == 'preserve'
    assert {r[0] for r in board.execute('SELECT enabled FROM failure_producers')} == {0}
    later = admit(board, 'red-later', 'tests/test_x.py::test_B')
    assert later.action == 'DISABLED' and later.card_id is None            # no further producer-side create
    with pytest.raises(sqlite3.IntegrityError, match='incompatible writer'):
        with kb.write_txn(board):
            kb._insert_task_row(board, dict(id='legacy', title='bypass', status='ready', created_at=1,
                                            created_by='main-tip-red'))
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == before['tasks']
    spawned = []
    kbd.dispatch_once(board, spawn_fn=lambda task, ws, *a, **k: spawned.append(task.id) or 4242, max_spawn=8)
    assert spawned == [minted.card_id]                                     # the known repair is still dispatchable
    state = tmp_path / 'state'
    state.mkdir()
    r = home('migrate', '--state-dir', str(state), db=db)
    assert r.returncode == 0 and 'imported=0 already-present=1 skipped=0' in r.stdout, r.stdout + r.stderr
    print('ROLLOUT_ARM %s' % json.dumps({'case': 'rollback_drill', 'rows': before, 'after': counts(board)}))
