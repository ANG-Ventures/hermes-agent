"""A review cannot be returned for rework after only the first finding/lens."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

# Every lens name below comes from the schema constant (t_164f0178 F3): a
# lens-set change fails loudly in the schema/SKILL.md parity test, not here.
TARGET = LENSES[-1]


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


def all_done():
    return {key: 'done' for key in LENSES}


def lenses_with(state, lens=TARGET):
    lenses = all_done()
    lenses[lens] = state
    return lenses


def lenses_without(lens=TARGET):
    return {key: 'done' for key in LENSES if key != lens}


def coverage(lenses=None, **overrides):
    payload = {
        'lenses': lenses or all_done(),
        'findings': 1, 'items': ['Missing behavior assertion at handler:42'],
        'review_minutes': 12, 'battery': 'seeded', 'batch_id': 'batch-123',
    }
    payload.update(overrides)
    return 'review_coverage: ' + json.dumps(payload)


def test_three_lenses_refused_and_four_accepted_with_persistence(review):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_without()), run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix missing guard')
        assert not ok and TARGET in detail
        assert kb.get_task(conn, review).status == 'running'
        assert not any(e.kind == 'changes_requested' for e in kb.list_events(conn, review))
        kb.add_comment(conn, review, 'argus', coverage(), run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix missing guard') == (True, 'builder')
        assert kb.get_task(conn, review).status == 'ready'
        assert any(e.kind == 'changes_requested' for e in kb.list_events(conn, review))


@pytest.mark.parametrize('override,missing', [
    ({'findings': 2}, 'items'), ({'items': []}, 'items'),
    ({'findings': 0, 'items': []}, 'findings'),
    ({'lenses': lenses_with('n/a:   ')}, TARGET),
    ({'lenses': lenses_with('n/a: could not reach vendor')}, 'capability'),
    ({'review_minutes': -1}, 'review_minutes'),     ({'batch_id': ''}, 'batch_id'),
    ({'lenses': lenses_with('n/a:')}, TARGET),
    ({'lenses': lenses_with('blocked')}, TARGET),
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


def test_battery_is_optional(review):
    """Per-card batteries are being retired (CI owns suites): omitting it passes."""
    payload = json.loads(coverage().split('review_coverage: ', 1)[1])
    del payload['battery']
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', 'review_coverage: ' + json.dumps(payload),
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


@pytest.mark.parametrize('battery', ['', '   ', 7, True])
def test_battery_when_given_must_be_nonempty_string(review, battery):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(battery=battery),
                       run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and 'battery' in detail


def test_gate_lens_list_is_data_driven_from_schema(review, monkeypatch):
    """The required-lens list comes from ONE schema constant, not hardcoded names."""
    monkeypatch.setattr(kb, '_REVIEW_LENSES', ('contract', 'boundary'))
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
        kb.add_comment(conn, review, 'argus', coverage(), run_id=run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and 'boundary' in detail
        kb.add_comment(conn, review, 'argus', coverage(lenses={'contract': 'done', 'boundary': 'done'}), run_id=run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


def test_schema_constant_feeds_gate_tool_and_prompt():
    from hermes_cli import kanban_review_schema as schema
    from tools import kanban_tools as tools
    from agent import prompt_builder
    assert kb._REVIEW_LENSES is schema.REQUIRED_REVIEW_LENSES
    description = tools.KANBAN_REQUEST_CHANGES_SCHEMA['description']
    assert schema.lens_list_text() in description
    assert schema.lens_list_text() in prompt_builder.KANBAN_GUIDANCE


def test_tool_and_cli_share_gate(review, monkeypatch):
    from tools import kanban_tools as tools
    monkeypatch.setenv('HERMES_PROFILE', 'argus')
    rejected = json.loads(tools._handle_request_changes({'reason': 'BEHAVIOUR: fix guard'}))
    assert 'review_coverage' in rejected['error']
    assert 'cannot request changes' in cli.run_slash(f'request-changes {review} "BEHAVIOUR: fix guard"')
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_without()), run_id=kb.get_task(conn, review).current_run_id)
    partial = json.loads(tools._handle_request_changes({'reason': 'BEHAVIOUR: fix guard'}))
    assert TARGET in partial['error']
    assert TARGET in cli.run_slash(f'request-changes {review} "BEHAVIOUR: fix guard"')
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
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_without()), run_id=run_id)
    partial = subprocess.run(command, env=env, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=90)
    assert partial.returncode != 0 and TARGET in partial.stderr
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


def test_human_only_board_can_claim_review_and_return_full_verdict(review, monkeypatch):
    # The human-lane claim binds to the claiming session (t_088fe9e3); pin one
    # so the result never depends on the runner's ambient session env.
    monkeypatch.setenv('HERMES_SESSION_ID', 'human-review-session')
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


@pytest.mark.parametrize('override,missing', [
    # type(x) is int, not isinstance: a JSON bool is not a count.
    ({'findings': True, 'items': ['one real finding']}, 'findings'),
    ({'review_minutes': True}, 'review_minutes'),
    # items must be nonempty strings, not whitespace.
    ({'items': ['   ']}, 'items'),
    # the n/a form requires the ': ' separator and a reason.
    ({'lenses': lenses_with('n/aXYZ')}, TARGET),
])
def test_parser_type_and_shape_mutants_refused(review, override, missing):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(**override), run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and missing in detail
        assert kb.get_task(conn, review).status == 'running'


@pytest.mark.parametrize('reason', [
    'n/a: no provider code touched, so vendors do not differ',
    'n/a: the failed-state code path is not changed',
    'n/a: error handling is unchanged; docs-only diff',
    'N/A: Docs-only diff',  # casefolded (F6)
])
def test_na_applicability_reason_accepted(review, reason):
    """An n/a certifies the lens does not APPLY to the deliverable."""
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_with(reason)),
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')
        assert kb.get_task(conn, review).status == 'ready'


@pytest.mark.parametrize('reason', [
    'n/a: skipped', 'n/a: Skipped for time', 'n/a: ran out of time', 'n/a: did not run',
    'n/a: could not reach vendor', 'n/a: cannot run the suite', 'n/a: unable to build',
    'n/a: failed to start', 'n/a: timed out', 'n/a: deferred to next round',
    # F2 (Apollo r3): zero-width split, and inability later in the reason.
    'n/a: \u200bcould not run', 'n/a: the reviewer could not run it',
    'n/a: no mutation tool available here', 'n/a: mutmut missing on host',
    'n/a: budget exhausted', 'n/a: c\u200bould n\u200bot run',
    'n/a: \uff43ould not run',  # fullwidth 'c' folds under NFKC
    # Deliberately conservative: an inability WORD anywhere is refused, so an
    # applicability claim must be phrased without it ("do not differ").
    'n/a: no provider code touched, so vendors cannot differ',
])
def test_na_inability_anywhere_refused_and_routed_to_capability_block(review, reason):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_with(reason)),
                       run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and TARGET in detail and 'kind=capability' in detail
        assert kb.get_task(conn, review).status == 'running'


def _skill_example_line():
    skill = Path(__file__).resolve().parents[2] / 'skills' / 'devops' / 'sdlc-review' / 'SKILL.md'
    lines = [line for line in skill.read_text(encoding='utf-8').splitlines()
             if 'review_coverage:' in line and '{' in line]
    assert len(lines) == 1, lines
    return lines[0]


def test_documented_skill_example_accepted_verbatim(review):
    """The reviewer copies the SKILL.md example (inline-code backticks and all)."""
    example = _skill_example_line()
    assert example.startswith('`review_coverage: ') and example.endswith('`')
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', 'Round 1 verdict.\n' + example,
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


def test_double_backtick_wrapper_is_not_unwrapped(review):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', '``' + coverage() + '``',
                       run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and 'JSON line' in detail


def test_later_prose_mention_does_not_hide_valid_record(review):
    """F4: the newest comment that parses wins, not the newest token mention."""
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
        kb.add_comment(conn, review, 'argus', coverage(), run_id=run_id)
        kb.add_comment(conn, review, 'argus',
                       'Note: the review_coverage: record above is final.', run_id=run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


def test_newest_valid_record_wins_over_older_valid_record(review):
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
        kb.add_comment(conn, review, 'argus', coverage(), run_id=run_id)
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses_without()), run_id=run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and TARGET in detail


def test_unparseable_newest_reports_its_error(review):
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
        kb.add_comment(conn, review, 'argus', 'review_coverage: {not json', run_id=run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and 'invalid review_coverage JSON' in detail


@pytest.mark.parametrize('override,missing', [
    ({'items': ['-']}, 'items'),
    ({'items': ['F1']}, 'items'),
    ({'items': ['...']}, 'items'),
    ({'items': ['\u200b\u200bab']}, 'items'),
    ({'review_minutes': 10 ** 9}, 'review_minutes'),
    ({'review_minutes': kb._REVIEW_MINUTES_MAX + 1}, 'review_minutes'),
])
def test_shallow_findings_refused(review, override, missing):
    """F5: items must name a finding; minutes must be a plausible round."""
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(**override), run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard')
        assert not ok and missing in detail
        assert kb.get_task(conn, review).status == 'running'


@pytest.mark.parametrize('override', [
    {'items': ['F12']}, {'review_minutes': kb._REVIEW_MINUTES_MAX}, {'review_minutes': 0},
])
def test_boundary_findings_accepted(review, override):
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(**override), run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


def test_lens_keys_and_states_are_casefolded(review):
    """F6: 'DONE' / 'N/A:' / capitalised keys are the same lens record."""
    lenses = {key.upper(): 'DONE' for key in LENSES}
    lenses[TARGET.title()] = lenses.pop(TARGET.upper())
    with kb.connect() as conn:
        kb.add_comment(conn, review, 'argus', coverage(lenses=lenses),
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason='BEHAVIOUR: fix guard') == (True, 'builder')


def test_skill_example_lens_set_matches_schema():
    """F3: ONE place fails when the lens set changes -- SKILL.md must follow the schema."""
    example = _skill_example_line()
    payload = json.loads(example.strip().strip('`').split('review_coverage:', 1)[1])
    assert tuple(payload['lenses']) == LENSES


def test_retired_reopen_review_cli_prints_only_the_refusal(review):
    """F7: no 'reopen-review allowed' line before 'cannot reopen'."""
    with kb.connect() as conn:
        parked = kb.create_task(conn, title='parked', assignee='builder')
        assert kb.request_review(conn, parked, summary='ready', reviewer='argus')
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_KANBAN')}
    env.pop('HERMES_KANBAN_TASK', None)
    proc = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'kanban', 'reopen-review', parked],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=90)
    assert proc.returncode != 0
    assert 'cannot reopen' in proc.stderr
    assert 'allowed' not in proc.stderr + proc.stdout
    with kb.connect() as conn:
        assert kb.get_task(conn, parked).status == 'review'
        assert not any(e.kind == 'review_reopened' for e in kb.list_events(conn, parked))
