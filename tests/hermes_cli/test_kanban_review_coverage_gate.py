"""A review cannot be returned for rework after only the first finding/lens."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def review(tmp_path, monkeypatch):
    home = tmp_path / 'hermes'
    home.mkdir()
    for key in list(__import__('os').environ):
        if key.startswith('HERMES_KANBAN'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='coverage probe', assignee='builder')
        implementation = kb.claim_task(conn, tid)
        assert implementation
        assert kb.request_review(conn, tid, summary='ready', reviewer='argus', expected_run_id=implementation.current_run_id)
        claimed = kb.claim_review_task(conn, tid)
        assert claimed
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claimed.current_run_id))
    return tid


def coverage(lenses=None, **overrides):
    payload = {
        'lenses': lenses or {key: 'done' for key in ('contract', 'execution', 'cross-vendor', 'mutation')},
        'findings': 1, 'items': ['Missing behavior assertion at handler:42'],
        'review_minutes': 12, 'battery': 'seeded', 'batch_id': 'batch-123',
    }
    payload.update(overrides)
    return 'review_coverage: ' + json.dumps(payload)


def test_three_lenses_refused_and_four_accepted_with_persistence(review):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses={
            'contract': 'done', 'execution': 'done', 'mutation': 'done',
        }), run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix missing guard')
        assert not ok and 'cross-vendor' in detail
        assert kb.get_task(conn, review).status == 'running'
        assert not any(e.kind == 'changes_requested' for e in kb.list_events(conn, review))
        kb.add_comment(conn, review, 'argus', coverage(), run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix missing guard') == (True, 'builder')
        assert kb.get_task(conn, review).status == 'ready'
        assert any(e.kind == 'changes_requested' for e in kb.list_events(conn, review))


@pytest.mark.parametrize('override,missing', [
    ({'findings': 2}, 'items'), ({'items': []}, 'items'),
    ({'findings': 0, 'items': []}, 'findings'),
    ({'lenses': {'contract': 'done', 'execution': 'done', 'cross-vendor': 'n/a:   ', 'mutation': 'done'}}, 'cross-vendor'),
    ({'lenses': {'contract': 'done', 'execution': 'done', 'cross-vendor': 'n/a: could not reach vendor', 'mutation': 'done'}}, 'capability'),
    ({'review_minutes': -1}, 'review_minutes'), ({'battery': ''}, 'battery'),
    ({'batch_id': ''}, 'batch_id'),
    ({'lenses': {'contract': 'done', 'execution': 'done', 'cross-vendor': 'n/a:', 'mutation': 'done'}}, 'cross-vendor'),
    ({'lenses': {'contract': 'done', 'execution': 'done', 'cross-vendor': 'blocked', 'mutation': 'done'}}, 'cross-vendor'),
])
def test_invalid_coverage_rejected(review, override, missing):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(**override), run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and missing in detail
        assert kb.get_task(conn, review).status == 'running'


def test_old_round_comment_cannot_authorize_new_review(review):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(), run_id=1)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and 'review_coverage' in detail


def test_second_round_cannot_reuse_first_round_battery_sentinel(review):
    with kb.connect() as conn:
        current = kb.get_task(conn, review).current_run_id
        kb.add_comment(conn, review, 'argus', coverage(), run_id=current)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')[0]
        candidate = kb.claim_task(conn, review)
        assert candidate
        assert kb.request_review(conn, review, summary='v2', expected_run_id=candidate.current_run_id)
        second = kb.claim_review_task(conn, review)
        assert second
        kb.add_comment(conn, review, 'argus', coverage(), run_id=second.current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard again')
        assert not ok and 'battery' in detail


def test_tool_and_cli_share_gate(review, monkeypatch):
    from tools import kanban_tools as tools
    monkeypatch.setenv('HERMES_PROFILE', 'argus')
    rejected = json.loads(tools._handle_request_changes({'reason': 'BEHAVIOUR: fix guard'}))
    assert 'review_coverage' in rejected['error']
    assert 'cannot request changes' in cli.run_slash(f'request-changes {review} "BEHAVIOUR: fix guard"')
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses={
            'contract': 'done', 'execution': 'done', 'mutation': 'done',
        }), run_id=kb.get_task(conn, review).current_run_id)
    partial = json.loads(tools._handle_request_changes({'reason': 'BEHAVIOUR: fix guard'}))
    assert 'cross-vendor' in partial['error']
    assert 'cross-vendor' in cli.run_slash(f'request-changes {review} "BEHAVIOUR: fix guard"')
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(), run_id=kb.get_task(conn, review).current_run_id)
    assert 'Requested changes' in cli.run_slash(f'request-changes {review} "BEHAVIOUR: fix guard"')


def test_real_cli_process_refuses_then_persists(review):
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_KANBAN')}
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
    env.update(HERMES_KANBAN_TASK=review, HERMES_KANBAN_RUN_ID=str(run_id))
    command = [sys.executable, '-m', 'hermes_cli.main', 'kanban',
               'request-changes', review, 'BEHAVIOUR: fix guard']
    refused = subprocess.run(command, env=env, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=90)
    assert refused.returncode != 0
    assert 'review_coverage' in refused.stderr
    with kb.connect() as conn:
        assert kb.get_task(conn, review).status == 'running'
        kb.add_comment(conn, review, 'argus', coverage(lenses={
            'contract': 'done', 'execution': 'done', 'mutation': 'done',
        }), run_id=run_id)
    partial = subprocess.run(command, env=env, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=90)
    assert partial.returncode != 0 and 'cross-vendor' in partial.stderr
    with kb.connect() as conn:
        assert kb.get_task(conn, review).status == 'running'
        kb.add_comment(conn, review, 'argus', coverage(), run_id=run_id)
    accepted = subprocess.run(command, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=90)
    assert accepted.returncode == 0, accepted.stderr
    with kb.connect() as conn:
        assert kb.get_task(conn, review).status == 'ready'


def test_capability_block_and_approval_are_not_review_rework(review):
    with kb.connect() as conn:
        assert kb.block_task(conn, review, reason='cannot run mutation lens', kind='capability')
        assert kb.get_task(conn, review).status == 'blocked'
    # A separate review may be approved without the rework-only gate.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='approval control', assignee='builder')
        implementation = kb.claim_task(conn, tid)
        assert implementation
        assert kb.request_review(conn, tid, summary='ready', reviewer='argus', expected_run_id=implementation.current_run_id)
        assert kb.claim_review_task(conn, tid)
        assert kb.complete_task(conn, tid, summary='approved')
        assert kb.get_task(conn, tid).status == 'done'


def test_legacy_parked_review_reopen_cannot_bypass_full_review(review):
    with kb.connect() as conn:
        parked = kb.create_task(conn, title='parked review', assignee='builder')
        assert kb.request_review(conn, parked, summary='ready', reviewer='argus')
        assert kb.reopen_review_task(conn, parked) is False
        assert kb.get_task(conn, parked).status == 'review'


def test_human_only_board_can_claim_review_and_return_full_verdict(review):
    with kb.connect() as conn:
        parked = kb.create_task(conn, title='human review', assignee='builder')
        assert kb.request_review(conn, parked, summary='ready', reviewer='human')
    assert 'Claimed' in cli.run_slash(f'claim {parked} --review')
    with kb.connect() as conn:
        claimed = kb.get_task(conn, parked)
        assert claimed.status == 'running'
    payload = coverage().split('review_coverage: ', 1)[1]
    import shlex
    output = cli.run_slash(
        f'request-changes {parked} "BEHAVIOUR: fix guard" --coverage {shlex.quote(payload)}'
    )
    assert 'Requested changes' in output
    with kb.connect() as conn:
        assert kb.get_task(conn, parked).status == 'ready'
        assert any(c.run_id == claimed.current_run_id and 'batch-123' in c.body
                   for c in kb.list_comments(conn, parked))
