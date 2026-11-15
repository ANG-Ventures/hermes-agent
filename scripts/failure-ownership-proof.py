#!/usr/bin/env python3
"""Disposable S2 authority acceptance; never opens a production board."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def executed_arm(report, selected, expected):
    """A zero exit is insufficient: every pinned case must execute and pass. `selected` is one test name (its
    parametrizations count) or a tuple of the exact allowed names (the dispatch arm's whole file)."""
    try:
        cases = ET.parse(report).getroot().findall('.//testcase')
        names = [c.get('name', '') for c in cases]
        ok = (lambda n: n.split('[')[0] in selected) if isinstance(selected, tuple) else \
            (lambda n: n == selected or n.startswith(selected + '['))
        return (len(cases) == expected and len(set(names)) == expected
                and all(ok(n) for n in names)
                and all(not list(c) or all(x.tag in ('properties', 'system-out', 'system-err')
                                          for x in c) for c in cases))
    except (OSError, ET.ParseError):
        return False


# Dispatch arm (S4, t_08cf1f58): every test in the file must run and pass, none skipped. 6 parametrized cases + 3.
DISPATCH_CASES = ('test_dispatch_arm', 'test_stale_claim_ticket_retries_without_a_run',
                  'test_home_toggle_is_the_switch_observe_failure_reads',
                  'test_mutant_claim_without_recheck_spawns_the_healed_card')
DISPATCH_EXPECTED = 9
# Rollout arm (S5, t_b8c1b185): the rollback drill through hermes-home's operator CLI on the real core.
ROLLOUT_CASES = ('test_rollback_drill_keeps_every_row_and_stops_producer_writes',)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=('atomic', 'crash', 'dispatch', 'rollout'), required=True)
    parser.add_argument('--home-repo', type=Path, help='dispatch/rollout arm: the hermes-home checkout whose toggle CLI '
                        'and producer rows are proved against the scratch board')
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--producers', type=int, default=4)
    parser.add_argument('--processes', type=int, default=32)
    parser.add_argument('--all-boundaries', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('SIGKILL/process-group acceptance requires POSIX')
    root = args.scratch.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    # An explicit empty/new scratch root is required; no DB path override exists.
    if root == Path.home() or root == repo or not root.is_absolute():
        parser.error('dedicated scratch directory required')
    if root.exists() and any(root.iterdir()):
        parser.error('scratch root must be empty')
    if (args.producers, args.processes) != (4, 32):
        parser.error('this pinned acceptance requires 4 producers and 32 processes')
    home_repo = None
    if args.arm in ('dispatch', 'rollout'):
        home_repo = args.home_repo.expanduser().resolve() if args.home_repo else None
        if home_repo is None or not (home_repo / 'scripts' / 'failure-ownership').is_file():
            parser.error('--arm %s requires --home-repo <hermes-home checkout with scripts/failure-ownership>'
                         % args.arm)
    root.mkdir(parents=True, exist_ok=True)
    if args.arm == 'dispatch':
        selected = DISPATCH_CASES
        target = 'tests/hermes_cli/test_failure_ownership_dispatch_arm.py'
    elif args.arm == 'rollout':
        selected = ROLLOUT_CASES
        target = 'tests/hermes_cli/test_failure_ownership_rollout_arm.py'
    else:
        selected = ('test_real_32_process_four_producer_race' if args.arm == 'atomic'
                    else 'test_sigkill_transaction_boundary_retry')
        target = f'tests/hermes_cli/test_kanban_failure_ownership.py::{selected}'
    command = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-s', target,
               '--basetemp', str(root / 'pytest'), '--junitxml', str(root / 'executed.xml')]
    env = dict(os.environ, HERMES_HOME=str(root / 'home'), TMPDIR=str(root))
    if home_repo is not None:
        env['FAILURE_HOME_REPO'] = str(home_repo)
    env.pop('HERMES_KANBAN_DB', None)
    # A proof arm must execute its pinned test, regardless of caller pytest flags.
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    started = time.monotonic()
    with (root / 'output.log').open('wb') as output:
        child = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            rc = child.wait(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only acceptance arm
            child.wait()
            rc = 124
        finally:
            # Reap multiprocessing grandchildren even after interrupted pytest.
            try:
                os.killpg(child.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only acceptance arm
            except ProcessLookupError:
                pass
    verified = executed_arm(root / 'executed.xml', selected,
                            {'atomic': 1, 'crash': 12, 'dispatch': DISPATCH_EXPECTED, 'rollout': 1}[args.arm])
    if rc == 0 and not verified:
        rc = 3
    extra = {}
    if args.arm == 'dispatch':
        extra['cases'] = [json.loads(line.split('DISPATCH_ARM ', 1)[1]) for line in
                          (root / 'output.log').read_text(encoding='utf-8-sig', errors='replace').splitlines()
                          if 'DISPATCH_ARM ' in line]
        extra['home_repo'] = str(home_repo)
    if args.arm == 'rollout':
        extra['cases'] = [json.loads(line.split('ROLLOUT_ARM ', 1)[1].strip()) for line in
                          (root / 'output.log').read_text(encoding='utf-8-sig', errors='replace').splitlines()
                          if 'ROLLOUT_ARM {' in line]
        extra['home_repo'] = str(home_repo)
    summary = dict(arm=args.arm, pid=child.pid, exit_code=rc, executed_verified=verified, **extra,
                   elapsed_seconds=time.monotonic()-started, command=command,
                   log=str(root / 'output.log'))
    (root / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
