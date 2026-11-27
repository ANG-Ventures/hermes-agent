"""S4 dispatch arm (t_08cf1f58): failure authority -> scratch board -> the REAL dispatcher tick.

Spec plans/2026-11-07_failure-ownership-SPEC.md section 5: a repair card whose failure is healed before the claim
transaction linearizes gets zero runs, zero spawns and no workspace; the negative controls (no pass, unknown
evidence, a passing sibling workflow, a pass on another matrix axis, preserve mode, stale ticket) keep the repair
and dispatch it exactly once. The home leg drives hermes-home's `scripts/failure-ownership toggle` CLI against the
same scratch board to prove the operator toggle is the switch observe_failure reads (C1/C5/A5).

Driven by `scripts/failure-ownership-proof.py --arm dispatch --home-repo <hermes-home>`; FAILURE_HOME_REPO is how
the arm passes the checkout. Never opens a fleet board: HERMES_HOME is a tmp dir.
"""
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_failure_ownership as fo

pytestmark = pytest.mark.platforms('posix')

LINEAGE = ('a' * 40, 'b' * 40, 'c' * 40)
# Predeclared counts per case: (spawns, task_runs, repair cards). The arm fails on any other number.
EXPECTED = {
    'healed_before_claim': (0, 0, 1),
    'no_pass': (1, 1, 1),
    'unknown_evidence': (1, 1, 1),
    'sibling_workflow_pass': (1, 1, 1),
    'other_matrix_axis_pass': (1, 1, 1),
    'preserve_mode_pass': (1, 1, 1),
}


def key(workflow=2, axes=(('os', 'linux'),), node='tests/test_x.py::test_A'):
    return json.dumps([1, 'fleet', '', 'github.com', 1, workflow, '.github/workflows/ci.yml', [],
                       [list(a) for a in axes], 'suite', 'unit:invocation', 'pytest-node', node],
                      ensure_ascii=False, separators=(',', ':'))


def obs(source, result='failure', sha='a', run_id=1, **kw):
    return fo.Observation(key=kw.pop('k', key()), source_key=source, result=result, sha=sha * 40,
                          run_id=run_id, attempt=1, job_id=1, lineage=LINEAGE, policy_generation=1,
                          complete=result == 'pass', **kw)


def admit(conn, observation, producer='ci-red-owner'):
    ref = fo.publish_evidence(conn, json.dumps(fo.asdict(observation)).encode(), verifier=lambda _: observation)
    card = fo.prepare_card(conn, title='repair %s' % observation.source_key,
                           body='bounded action; acceptance: tests/test_x.py::test_A', assignee='alice',
                           session_id='operator')
    return fo.observe_failure(conn, ref, producer=producer, card_spec=card)


@pytest.fixture
def board(tmp_path, monkeypatch, all_assignees_spawnable):
    root = tmp_path.resolve()
    monkeypatch.setenv('HERMES_HOME', str(root / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    path = root / 'board' / 'kanban.db'
    conn = kb.connect(path)
    assert Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve().is_relative_to(root)
    yield conn
    conn.close()


def seed(conn, mode='on'):
    # Test-only policy seed: enrollment has no public API (S5 owns it). The toggle under test writes `enabled`.
    with kb.write_txn(conn):
        conn.execute('UPDATE failure_producers SET enabled=1,enrolled=1')
        conn.execute('UPDATE failure_authority SET mode=?', (mode,))


def dispatch(conn):
    spawned = []

    def spawn(task, workspace, *a, **kw):        # process-boundary recorder: what would have been Popen'd
        spawned.append((task.id, workspace))
        return 4242
    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=8)
    return spawned, res


def counts(conn, spawned):
    runs = conn.execute('SELECT count(*) FROM task_runs').fetchone()[0]
    cards = conn.execute('SELECT count(*) FROM failure_bindings WHERE card_id IS NOT NULL').fetchone()[0]
    return len(spawned), runs, cards


@pytest.mark.parametrize('case', sorted(EXPECTED))
def test_dispatch_arm(board, case):
    seed(board, mode='preserve' if case == 'preserve_mode_pass' else 'on')
    red = admit(board, obs('red'))
    assert red.action == 'REPAIR' and red.card_id
    follow = {
        'healed_before_claim': obs('green', 'pass', 'b', run_id=2),
        'no_pass': None,
        'unknown_evidence': replace(obs('unread', 'unknown', 'b', run_id=2), key=None, reason='http_502'),
        'sibling_workflow_pass': obs('green-sibling', 'pass', 'b', run_id=2, k=key(workflow=3)),
        'other_matrix_axis_pass': obs('green-mac', 'pass', 'b', run_id=2, k=key(axes=(('os', 'macos'),))),
        'preserve_mode_pass': obs('green', 'pass', 'b', run_id=2),
    }[case]
    if follow is not None:
        admit(board, follow)
    spawned, res = dispatch(board)
    got = counts(board, spawned)
    workspace = board.execute('SELECT workspace_path, status FROM tasks WHERE id=?', (red.card_id,)).fetchone()
    print('DISPATCH_ARM %s' % json.dumps({'case': case, 'spawns': got[0], 'task_runs': got[1],
                                          'repair_cards': got[2], 'workspace_path': workspace[0],
                                          'status': workspace[1], 'expected': EXPECTED[case]}))
    assert got == EXPECTED[case], (case, got, res)
    if case == 'healed_before_claim':
        assert workspace[0] is None and workspace[1] == 'ready'
        assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1    # history kept, no new card
    else:
        assert spawned[0][0] == red.card_id


def test_stale_claim_ticket_retries_without_a_run(board):
    seed(board)
    red = admit(board, obs('red'))
    with kb.write_txn(board):
        current = fo.recheck_for_claim(board, red.card_id).revision
        assert fo.recheck_for_claim(board, red.card_id, expected_revision=current - 1).action == 'retry'
    assert board.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
    spawned, _ = dispatch(board)
    assert counts(board, spawned) == (1, 1, 1)       # a fresh claim proceeds on the current revision


def _toggle(home_repo, db, *args):
    return subprocess.run([sys.executable, str(Path(home_repo) / 'scripts' / 'failure-ownership'), 'toggle', *args,
                           '--db', str(db)], capture_output=True, text=True, timeout=60)


def test_home_toggle_is_the_switch_observe_failure_reads(board):
    home_repo = os.environ.get('FAILURE_HOME_REPO')
    if not home_repo:
        pytest.skip('FAILURE_HOME_REPO unset: run through failure-ownership-proof.py --arm dispatch --home-repo')
    db = board.execute('PRAGMA database_list').fetchone()[2]
    seed(board)
    owner = admit(board, obs('red'))
    assert owner.action == 'REPAIR'
    r = _toggle(home_repo, db, 'off', '--producer', 'ci-red-owner')
    assert r.returncode == 0, r.stderr
    off = admit(board, obs('red2', k=key(node='tests/test_x.py::test_B')))
    assert off.action == 'DISABLED' and off.card_id is None
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1          # zero producer-side creates
    other = admit(board, obs('red3', k=key(node='tests/test_x.py::test_C')), producer='main-green-streak')
    assert other.action == 'REPAIR'                                                # only that producer is off
    assert board.execute("SELECT enrolled FROM failure_producers WHERE producer='ci-red-owner'").fetchone()[0] == 1
    spawned, _ = dispatch(board)
    assert owner.card_id in [s[0] for s in spawned]     # C1/A5: disabled producer never stops an existing repair
    r = _toggle(home_repo, db, 'on', '--producer', 'ci-red-owner')
    assert r.returncode == 0, r.stderr
    again = admit(board, obs('red4', k=key(node='tests/test_x.py::test_D')))
    assert again.action == 'REPAIR'                    # on applies to the next call, no restart
    r = _toggle(home_repo, db, 'status')
    assert r.returncode == 0 and 'ci-red-owner' in r.stdout, r.stderr


def test_mutant_claim_without_recheck_spawns_the_healed_card(board, monkeypatch):
    """Mutation control (spec: "omit claim revision check"): with the claim-side recheck stubbed to always claim, the
    healed_before_claim case spawns. The zero above is the recheck's doing, not an undispatchable fixture."""
    seed(board)
    red = admit(board, obs('red'))
    admit(board, obs('green', 'pass', 'b', run_id=2))
    monkeypatch.setattr(fo, 'recheck_for_claim', lambda *a, **k: fo.Decision('claim'))
    spawned, _ = dispatch(board)
    assert counts(board, spawned) == (1, 1, 1) and spawned[0][0] == red.card_id
