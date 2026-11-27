"""Hosted-first overflow through the real matrix CLI and workflow trust guards."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HOSTED = '["ubuntu-latest"]'
LOCAL = '["self-hosted","universal-ci"]'
TRUST = "(github.event_name == 'push' || github.event_name == 'merge_group' || (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository))"


def generate(tmp_path, *args):
    files = []
    for i in range(6):
        path = tmp_path / f"test_{i}.py"
        path.write_text("def test_ok(): pass\n")
        files.append(str(path))
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/run_tests_parallel.py'),
         '--generate-slices', '6', '--files', ':'.join(files), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return [s['runs_on'] for s in json.loads(proc.stdout)['slice']]


def test_trusted_push_main_last_three(tmp_path):
    assert generate(tmp_path, '--self-hosted-last-n=3', '--event', 'push',
                    '--same-repo', 'false', '--ref', 'refs/heads/main') == [HOSTED] * 3 + [LOCAL] * 3


@pytest.mark.parametrize('event,same,ref', [
    ('merge_group', 'false', 'refs/heads/gh-readonly-queue/main/pr-1'),
    ('pull_request', 'true', 'refs/pull/1/merge'),
    ('push', 'false', 'refs/heads/feature'),
    ('push', 'false', ''),
])
def test_trusted_but_unplaceable_stays_hosted(tmp_path, event, same, ref):
    # group 15 serves tests.yml@refs/heads/main only: a trusted non-main run routed
    # to universal-ci would queue on labels no runner group can place.
    assert generate(tmp_path, '--self-hosted-last-n=3', '--event', event,
                    '--same-repo', same, '--ref', ref) == [HOSTED] * 6


@pytest.mark.parametrize('raw,count', [('', 0), ('0', 0), ('-1', 0), ('garbage', 0), ('1.5', 0), ('999999999999999999999999', 6)])
def test_counts_fail_closed_and_clamp(tmp_path, raw, count):
    assert generate(tmp_path, f'--self-hosted-last-n={raw}', '--event=push',
                    '--ref=refs/heads/main') == [HOSTED] * (6-count) + [LOCAL] * count


@pytest.mark.parametrize('event,same', [('pull_request', 'false'), ('pull_request', ''), ('workflow_dispatch', 'true'), ('pull_request_target', 'true'), ('', '')])
def test_untrusted_overrides_all_routing_knobs(tmp_path, event, same):
    assert generate(tmp_path, '--self-hosted-last-n=3', '--self-hosted-slots=6',
                    '--self-hosted-labels', LOCAL, '--blacksmith-slices=6',
                    '--arm-hosted-slices=6', '--event', event, '--same-repo', same) == [HOSTED] * 6


def test_default_and_legacy_compatibility(tmp_path):
    assert generate(tmp_path) == [HOSTED] * 6
    legacy = ['--self-hosted-slots=2', '--self-hosted-labels', LOCAL, '--event=push']
    assert generate(tmp_path, *legacy) == [LOCAL] * 2 + [HOSTED] * 4
    assert generate(tmp_path, *legacy, '--self-hosted-last-n=0') == [HOSTED] * 6


@pytest.mark.parametrize('event,same', [('push', 'false'), ('pull_request', 'false')])
def test_plugin_scoped_matrix_obeys_last_n_and_trust(tmp_path, event, same):
    plugin = next(p.name for p in sorted((ROOT / 'tests/plugins').iterdir())
                  if p.is_dir() and any(p.glob('test_*.py')))
    labels = generate(tmp_path, '--test-scope', f'plugin:{plugin}',
                      '--self-hosted-last-n=1', '--self-hosted-labels', LOCAL,
                      '--event', event, '--same-repo', same, '--ref', 'refs/heads/main')
    assert labels == ([HOSTED] * (len(labels) - 1) + [LOCAL]
                      if event == 'push' else [HOSTED] * len(labels))


def test_workflow_plumbing_and_matrix_have_independent_fork_guard():
    for filename in ('tests.yml', 'ci.yaml', 'ci-placement-probe.yml'):
        jobs = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())['jobs']
        for job in jobs.values():
            expr = job.get('runs-on', '')
            if 'CI_PLUMBING_RUNNER' in expr or 'CI_RUNNER_LABELS' in expr or 'matrix.slice.runs_on' in expr:
                assert TRUST in expr, (filename, expr)
                assert 'ubuntu-latest' in expr
    text = (ROOT / '.github/workflows/tests.yml').read_text()
    assert 'CI_SELF_HOSTED_LAST_N: ${{ vars.CI_SELF_HOSTED_LAST_N }}' in text
    assert '--self-hosted-last-n="$CI_SELF_HOSTED_LAST_N"' in text
    assert 'GH_REF: ${{ github.ref }}' in text and '--ref="$GH_REF"' in text
