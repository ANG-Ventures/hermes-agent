"""Fork-feature canary lane: every registered nodeid must collect AND execute (t_f34a6e21).

``lint_manifest.lint_nodeids`` proves a nodeid collects. That is not coverage: a test that is
``skipif``'d on the CI platform, or whose ``importorskip`` misses, collects fine and guards
nothing. This lane RUNS every nodeid registered in ``docs/sync/fork-features.json`` and reads the
per-item outcome (``canary_plugin``):

* passed / xfailed (a strict xfail executed and failed as declared)   -> covered
* failed / errored                                                     -> RED (the canary fired)
* skipped                                                              -> RED, not covered
  (a file/class registration is RED only when none of its members runs; a skipped member is a WARN)
* never collected (renamed / deleted / module import broke)            -> RED, repoint or record coverage_gap

``*_ptb.py`` files need real python-telegram-bot and the rest of the suite needs it ABSENT (see
``scripts/run_tests_parallel.py`` ``_DEDICATED_LANE_SUFFIXES``), so they run in a second pass under
``--ptb-python``. Without it those nodeids are RED ("not run"), never silently dropped.

``--changed-file-list`` / ``--changed-from`` make the lane a no-op (rc 0, ``not affected``) when a
PR touches nothing the registry guards: the registry itself, ``tests/fork_canaries/``, this
package, any registered test file, or any file a registry ``paths`` glob matches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import forkdelta, gitops

PTB_SUFFIX = "_ptb.py"
LANE_INPUTS = ("docs/sync/fork-features.json", "tests/fork_canaries/", "scripts/hermes_parity/")
REMEDY_MISSING = "repoint the nodeid at its successor, or drop it and record a coverage_gap"
REMEDY_SKIPPED = "skipped on this platform = not covered: make it run here, or drop it and record a coverage_gap"


@dataclass
class LaneResult:
    executed: dict[str, str] = field(default_factory=dict)   # nodeid -> passed|xfailed
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    affected: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures


def registered_test_files(nodeids: Iterable[str]) -> set[str]:
    return {node.split("::", 1)[0] for node in nodeids}


def is_affected(manifest: Path, changed: Iterable[str]) -> tuple[bool, str]:
    """Does this change touch anything the registry guards? -> (affected, first reason)."""
    features = forkdelta.load_manifest(manifest)
    test_files = registered_test_files(n for f in features for n in f.tests)
    for path in sorted(set(changed)):
        if any(path == p.rstrip("/") or path.startswith(p) for p in LANE_INPUTS):
            return True, f"{path} is a lane input"
        if path in test_files:
            return True, f"{path} is a registered test file"
        if forkdelta.covered_path(path, features):
            return True, f"{path} matches a registry paths glob"
    return False, "no changed file is the registry, a canary, a registered test file, or a registered path"


def _run_file(python: str, repo: Path, nodeids: Sequence[str], timeout_s: int) -> dict:
    """One pytest process for one test file's nodeids (scripts/run_tests.sh isolation shape: a single
    process over every registered file cross-contaminates module state and hung on ace-media)."""
    with tempfile.TemporaryDirectory(prefix="canary-lane-") as td:
        report = Path(td) / "report.json"
        # HERMES_HOME: tests/conftest.py isolates the home. PYTEST_ADDOPTS: `-o addopts=` does not clear it, and an
        # inherited `-k`/`-m` would deselect registered members before collection is recorded.
        env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "PYTEST_ADDOPTS")}
        cmd = [python, "-m", "pytest", "-q", "-rfEs", "-o", "addopts=", "-p", "no:cacheprovider",
               "-p", "no:randomly", "-p", "scripts.hermes_parity.canary_plugin",
               "--canary-report", str(report), *nodeids]
        try:
            proc = subprocess.run(cmd, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, check=False, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"crash": f"timed out after {timeout_s}s", "rc": -1}
        if not report.exists():
            return {"crash": proc.stdout[-2000:], "rc": proc.returncode}
        data = json.loads(report.read_text(encoding="utf-8-sig"))
        data["rc"] = proc.returncode
        return data


def _run_pass(python: str, repo: Path, nodeids: Sequence[str], result: LaneResult, *, jobs: int,
              timeout_s: int) -> None:
    by_file: dict[str, list[str]] = {}
    for node in nodeids:
        by_file.setdefault(node.split("::", 1)[0], []).append(node)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {f: pool.submit(_run_file, python, repo, nodes, timeout_s) for f, nodes in by_file.items()}
    for f, fut in sorted(futures.items()):
        data = fut.result()
        nodes = by_file[f]
        if len(nodes) > 1 and "crash" not in data and any(not _expand(n, data["collected"]) for n in nodes):
            # pytest runs NOTHING when one nodeid of the batch fails to collect ("found no collectors"):
            # re-run one nodeid per process so only the rotten one is reported
            for n in nodes:
                one = _run_file(python, repo, [n], timeout_s)
                if "crash" in one:
                    result.failures.append(f"NO REPORT {n} (rc {one['rc']}): {one['crash'][-400:].strip()}")
                else:
                    _classify_checked(n, [n], one, result)
            continue
        if "crash" in data:
            result.failures.append(f"NO REPORT {f} (rc {data['rc']}): {data['crash'][-400:].strip()}")
            continue
        _classify_checked(f, nodes, data, result)


def _classify_checked(label: str, nodes: Sequence[str], data: dict, result: LaneResult) -> None:
    """classify, then hold the process to its exit status: a nonzero pytest exit that no item failure explains
    (interrupted teardown, a session hook setting exitstatus, pytest.exit()) is RED, never coverage."""
    before = len(result.failures)
    classify(nodes, data, result)
    if data.get("rc", 0) != 0 and len(result.failures) == before:
        result.failures.append(f"PYTEST EXIT {label}: rc {data['rc']} with no failing item (session failed or was "
                               "interrupted; its item outcomes are not trusted)")


def _expand(nodeid: str, collected: Sequence[str]) -> list[str]:
    """Collected items a registered nodeid stands for (a file, class, or parametrized base expands)."""
    out = []
    for item in collected:
        if item == nodeid or item.startswith(nodeid + "::") or item.startswith(nodeid + "["):
            out.append(item)
    return out


_RANK = {"passed": 0, "xfailed": 1, "skipped": 2}   # anything else (failed / error in <phase>) ranks 3


def classify(nodeids: Sequence[str], data: dict, result: LaneResult) -> None:
    collected = data.get("collected") or []
    outcome: dict[str, tuple[str, str]] = {}
    called: set[str] = set()   # items whose test body actually ran (a call-phase report exists)
    for r in data.get("reports") or []:
        node, when, oc = r["nodeid"], r["when"], r["outcome"]
        if when == "call":
            called.add(node)
        prev = outcome.get(node)
        if oc == "skipped":
            state = "xfailed" if r.get("xfail") else "skipped"
        elif oc == "failed":
            state = "failed" if when == "call" else f"error in {when}"
        else:
            state = "passed"   # includes a non-strict xpass: the body ran and held
        # worst phase wins (setup skip, call failure, teardown error all outrank a passing phase)
        if prev is None or _RANK.get(state, 3) > _RANK.get(prev[0], 3):
            outcome[node] = (state, r.get("reason", ""))
    for nodeid in nodeids:
        items = _expand(nodeid, collected)
        if not items:
            hint = "; ".join(e["nodeid"] + ": " + e["longrepr"].strip().splitlines()[-1]
                             for e in data.get("errors") or [] if nodeid.startswith(e["nodeid"]) and e["longrepr"].strip())
            result.failures.append(f"NOT COLLECTED {nodeid} ({REMEDY_MISSING})" + (f" [{hint}]" if hint else ""))
            continue
        container_skips = []
        ran = 0
        for item in items:
            state, reason = outcome.get(item, ("not run", ""))
            # a file/class registration is a container: one member skipping (a platforms("windows") test on
            # linux) is that member's OS lane's job, but a container where NOTHING runs guards nothing
            member = item.startswith(nodeid + "::")
            if state == "xfailed" and item not in called:
                # xfail(run=False) reports from setup as skipped+wasxfail: the body never ran -> a skip, not coverage
                state, reason = "skipped", reason or "the test body never ran (no call phase)"
            elif state == "passed" and item not in called:
                # Prism 6c8f20320fee: setup PASSED and no call report (pytest.exit() before the call, a session
                # hook ending the run). Not a skip (a real skip reports `skipped` from setup): the guard simply
                # never executed, so it is RED even inside a file/class registration whose siblings ran.
                result.failures.append(f"NO CALL {item}: setup passed but the test body never ran (session "
                                       "ended before the call; not coverage)")
                continue
            if state in ("passed", "xfailed"):
                result.executed[item] = state
                ran += 1
            elif state == "skipped" and member:
                container_skips.append(f"{item}: {reason.strip()}")
            elif state == "skipped":
                result.failures.append(f"SKIPPED {item}: {reason.strip()} ({REMEDY_SKIPPED})")
            elif state == "not run":
                result.failures.append(f"NOT RUN {item} (collected but no outcome; did the session crash?)")
            else:
                result.failures.append(f"RED {item}: {state}")
        if container_skips and not ran:
            result.failures.append(f"SKIPPED {nodeid}: every member skipped ({REMEDY_SKIPPED}): "
                                   + "; ".join(container_skips[:3]))
        else:
            result.warnings.extend(f"skipped member of {nodeid}: {s}" for s in container_skips)


def run_lane(repo: Path, *, python: str, ptb_python: str | None, manifest: Path | None = None,
             changed: Iterable[str] | None = None, jobs: int = 4, timeout_s: int = 600,
             group: str = "all") -> LaneResult:
    manifest = repo / (manifest or forkdelta.DEFAULT_MANIFEST)
    result = LaneResult()
    if changed is not None:
        result.affected, result.reason = is_affected(manifest, changed)
        if not result.affected:
            return result
    nodeids = forkdelta.manifest_nodeids(manifest)
    plain = [n for n in nodeids if not n.split("::", 1)[0].endswith(PTB_SUFFIX)]
    ptb = [n for n in nodeids if n.split("::", 1)[0].endswith(PTB_SUFFIX)]
    passes = {"plain": (plain, python), "ptb": (ptb, ptb_python)}
    for name in (("plain", "ptb") if group == "all" else (group,)):
        nodes, py = passes[name]
        if not nodes:
            continue
        if py is None:
            result.failures.extend(f"NOT RUN {n} (needs real python-telegram-bot: pass --ptb-python)" for n in nodes)
            continue
        _run_pass(py, repo, nodes, result, jobs=jobs, timeout_s=timeout_s)
    return result


def changed_from(repo: Path, base: str) -> list[str]:
    # --no-renames: a rename lists only its NEW path by default, so moving a registered test or a covered
    # source to an unregistered name would read as "not affected"; both sides must be checked
    base_sha = gitops.merge_base(repo, base, "HEAD")
    # -z: otherwise git C-quotes non-ASCII paths ("tests/test_caf\\303\\251.py") and no registry entry matches
    out = gitops.run_git(repo, ["diff", "-z", "--no-renames", "--name-only", f"{base_sha}..HEAD"]).stdout
    return [p for p in out.split("\0") if p]


def main(args) -> int:
    repo = Path(args.worktree).resolve() if args.worktree else gitops.repo_root(Path.cwd())
    changed = None
    if args.changed_file_list:
        changed = [ln.strip() for ln in Path(args.changed_file_list).read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    elif args.changed_from:
        changed = changed_from(repo, args.changed_from)
    absolute = lambda p: str(Path(p).absolute()) if p and os.sep in p else p   # noqa: E731 - pytest runs with cwd=repo
    ptb_python = absolute(args.ptb_python)
    if args.group == "ptb" and not ptb_python:
        ptb_python = absolute(args.python) or sys.executable   # the ptb job's own interpreter has real PTB
    result = run_lane(repo, python=absolute(args.python) or sys.executable, ptb_python=ptb_python,
                      changed=changed, jobs=args.jobs, timeout_s=args.timeout, group=args.group)
    if not result.affected:
        print(f"canary-lane: not affected ({result.reason})")
        return 0
    if result.reason:
        print(f"canary-lane: affected ({result.reason})")
    for line in result.warnings:
        print(f"WARN {line}")
    for line in result.failures:
        print(line)
    n_x = sum(1 for s in result.executed.values() if s == "xfailed")
    print(f"canary-lane: {len(result.executed)} registered items executed ({n_x} strict-xfail), "
          f"{len(result.failures)} failure(s) -> {'GREEN' if result.ok else 'RED'}")
    return 0 if result.ok else 1
