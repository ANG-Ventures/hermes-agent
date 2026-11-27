"""Operator regression probes; disposable boards only."""
import importlib.util
import itertools
import pytest
from pathlib import Path

_spec = importlib.util.spec_from_file_location('authority_helpers', Path(__file__).with_name('test_kanban_failure_ownership.py'))
assert _spec and _spec.loader
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)
board = h.board


def test_proof_runner_clears_inherited_pytest_options(tmp_path, monkeypatch):
    import runpy
    import sys
    script = Path(__file__).resolve().parents[2] / 'scripts/failure-ownership-proof.py'
    module = runpy.run_path(str(script))
    captured = {}
    class Child:
        pid = 123
        def __init__(self, command, **kwargs):
            captured.update(kwargs['env'])
        def wait(self, timeout=None):
            return 0
    monkeypatch.setenv('PYTEST_ADDOPTS', '--collect-only')
    monkeypatch.setattr(module['subprocess'], 'Popen', Child)
    monkeypatch.setattr(module['os'], 'killpg', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', [str(script), '--arm', 'atomic', '--scratch', str(tmp_path / 'proof')])
    assert module['main']() == 3  # zero child exit without execution report is refused
    assert 'PYTEST_ADDOPTS' not in captured


@pytest.mark.parametrize('body,accepted', [
    ('<testcase name="p"/>', True),
    ('<testcase name="p"><skipped/></testcase>', False),
    ('<testcase name="p"><failure/></testcase>', False),
    ('<testcase name="other"/>', False),
    ('', False),
    ('<testcase name="p"/><testcase name="p"/>', False),
])
def test_proof_execution_report_refuses_nonexecution(tmp_path, body, accepted):
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/failure-ownership-proof.py'))
    report = tmp_path / 'report.xml'
    report.write_text('<testsuites><testsuite>' + body + '</testsuite></testsuites>')
    assert module['executed_arm'](report, 'p', 1) is accepted


def test_trigger_replacement_has_no_committed_gap(board):
    import sqlite3
    peer = sqlite3.connect(board.execute('PRAGMA database_list').fetchone()[2], timeout=0)
    observed = []
    def trace(sql):
        if sql.lstrip().startswith('CREATE TRIGGER failure_owner_terminal'):
            observed.append(peer.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='failure_owner_terminal'").fetchone()[0])
    board.set_trace_callback(trace)
    try:
        h.fo.migrate(board)
    finally:
        board.set_trace_callback(None)
        peer.close()
    assert observed == [1], 'another connection saw the trigger absent during replacement'


def test_migration_does_not_commit_callers_transaction(board):
    board.execute('CREATE TABLE operator_sentinel(value TEXT)')
    board.commit()
    board.execute('BEGIN IMMEDIATE')
    board.execute("INSERT INTO operator_sentinel VALUES ('uncommitted')")
    h.fo.migrate(board)
    assert board.in_transaction
    board.rollback()
    assert board.execute('SELECT count(*) FROM operator_sentinel').fetchone()[0] == 0


def test_failed_migration_restores_original_trigger(board, monkeypatch):
    import sqlite3
    before = board.execute("SELECT sql FROM sqlite_master WHERE name='failure_owner_terminal'").fetchone()[0]
    monkeypatch.setattr(h.fo, 'SCHEMA', 'DROP TRIGGER failure_owner_terminal;\nSELECT missing_migration_function();\n')
    with pytest.raises(sqlite3.OperationalError):
        h.fo.migrate(board)
    after = board.execute("SELECT sql FROM sqlite_master WHERE name='failure_owner_terminal'").fetchone()[0]
    assert after == before
    assert not board.in_transaction


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_overlapping_later_passes_heal_delayed_failure(board, order):
    h.enroll(board)
    observations = (
        h.observation('p2', result='pass', run_id=2, started_at=20, completed_at=40),
        h.observation('p3', result='pass', run_id=3, started_at=30, completed_at=50),
        h.observation('red', run_id=1, started_at=0, completed_at=10),
    )
    for index in order:
        h.admit(board, observations[index])
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='active'").fetchone()[0] == 0


def test_repeated_lineage_is_not_recompared_per_observation(board, monkeypatch):
    h.enroll(board)
    for i in range(12):
        h.admit(board, h.observation('repeat-%s' % i))
    calls = []
    original = h.fo._order
    def counted(a, b, chain):
        calls.append((a['source_key'], b['source_key'], tuple(chain)))
        return original(a, b, chain)
    monkeypatch.setattr(h.fo, '_order', counted)
    h.admit(board, h.observation('repeat-final'))
    # Each semantic pair/chain should be evaluated once, not once per capture.
    assert len(calls) == len(set(calls))
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_disabled_evidence_can_be_reattested_after_policy_change(board):
    from dataclasses import replace
    obs = h.observation('disabled-before-enrollment')
    assert h.admit(board, obs).action == 'DISABLED'
    h.enroll(board)
    with h.kb.write_txn(board):
        board.execute('UPDATE failure_authority SET policy_generation=2')
    refreshed = replace(obs, policy_generation=2)
    decision = h.admit(board, refreshed)
    assert decision.action == 'REPAIR'
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0] == 1
    stored = board.execute('SELECT data FROM failure_observations').fetchone()[0]
    assert h.json.loads(stored)['policy_generation'] == 2


def test_reattest_cannot_change_disabled_source_facts(board):
    from dataclasses import replace
    obs = h.observation('immutable-source')
    assert h.admit(board, obs).action == 'DISABLED'
    h.enroll(board)
    with h.kb.write_txn(board):
        board.execute('UPDATE failure_authority SET policy_generation=2')
    with pytest.raises(ValueError, match='source replay changed payload'):
        h.admit(board, replace(obs, policy_generation=2, sha='d'*40))
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    stored = board.execute('SELECT data FROM failure_observations').fetchone()[0]
    assert h.json.loads(stored)['policy_generation'] == 1


def test_late_lineage_reconstruction_preserves_single_binding(board):
    from dataclasses import replace
    h.enroll(board)
    a, b, x, d = (letter * 40 for letter in ('a', 'b', 'c', 'd'))
    rows = (
        replace(h.observation('failure-a'), sha=a, lineage=(a,)),
        replace(h.observation('pass-b', result='pass'), sha=b, lineage=(a, b)),
        replace(h.observation('failure-x'), sha=x, lineage=(x,)),
        replace(h.observation('failure-d'), sha=d, lineage=(a, x, b, d)),
    )
    decisions = [h.admit(board, obs) for obs in rows]
    original_owner = decisions[0].card_id
    retained_owner = decisions[2].card_id
    assert original_owner != retained_owner
    bindings = board.execute("SELECT b.card_id FROM failure_bindings b JOIN failure_episodes e USING(episode_id) WHERE b.state='bound' AND e.state='active'").fetchall()
    assert [row[0] for row in bindings] == [retained_owner]
    with h.kb.write_txn(board):
        assert h.fo.recheck_for_claim(board, original_owner).action == 'healed'
        assert h.fo.recheck_for_claim(board, retained_owner).action == 'claim'
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert board.execute('SELECT count(*) FROM failure_observations').fetchone()[0] == 4
    assert board.execute("SELECT count(*) FROM failure_episodes WHERE state='active'").fetchone()[0] == 1
    assert board.execute('PRAGMA foreign_key_check').fetchall() == []


def test_new_detector_replacement_does_not_inherit_adoption_exemption(board):
    h.enroll(board)
    red = h.observation()
    original = h.admit(board, red)
    # Existing task is operator-adopted; the replacement is detector-created.
    with h.kb.write_txn(board):
        board.execute('UPDATE failure_bindings SET detector_only=0 WHERE card_id=?', (original.card_id,))
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (original.card_id,))
    card = h.fo.prepare_card(board, title='replacement', body='bounded repair', assignee=None, session_id='operator')
    replacement, = h.fo.reconcile_pending(board, producer='ci-red-owner', card_spec=card)
    binding = board.execute('SELECT detector_only FROM failure_bindings WHERE card_id=?', (replacement.card_id,)).fetchone()
    assert binding[0] == 1
