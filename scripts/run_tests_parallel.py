#!/usr/bin/env python3
"""Per-file parallel test runner.

The minimum-viable replacement for pytest-xdist + a subprocess-isolation
plugin. Discovers test files under ``tests/`` (excluding integration/e2e
unless explicitly requested), then runs one ``python -m pytest <file>``
subprocess per file, with bounded parallelism (default: ``os.cpu_count()``).

Why per-file rather than per-test?
    Per-test spawn overhead (~250ms × 17k tests = 70min CPU minimum)
    swamped the actual work. Per-file spawn (~250ms × ~850 files = ~3.5min)
    fits in the budget while still giving every file a fresh Python
    interpreter — the only isolation boundary that actually matters
    (cross-file module-level state leakage was the original flake source;
    intra-file state is the test author's responsibility).

Why drop xdist entirely?
    xdist's persistent workers accumulate state across files, which is
    exactly the leakage we wanted to fix. xdist also adds complexity
    (loadfile vs loadscope, --max-worker-restart, internal control plane)
    that we don't need when the unit of work is "run pytest on one file".
    A subprocess.Popen pool gated by a semaphore is ~60 lines and does
    the job.

Usage:
    python scripts/run_tests_parallel.py [pytest_args...]

    Common pytest args pass through to each per-file pytest invocation
    (e.g. ``-q``, ``-v``, ``-x``, ``--tb=long``, ``-k 'pattern'``, ``--lf``)
    with no special separator — a bare ``-q`` "just works". Anything after
    a literal ``--`` is also passed through, and stacks with bare flags.

Environment:
    HERMES_TEST_WORKERS  Worker-count CEILING (default: effective_cpus*2).
                         Clamped to max(2, effective_cpus*2), where
                         effective_cpus is read from the cgroup CPU quota
                         rather than the host core count — `docker run
                         --cpus 2` on a 24-core box still reports 24 from
                         os.cpu_count().
    HERMES_TEST_WORKERS_FORCE
                         Set to 1 to take HERMES_TEST_WORKERS/-j literally
                         and skip the quota clamp.
    HERMES_TEST_PATHS    Override discovery roots (colon-sep; on Windows
                         ';' also works and drive letters are handled;
                         default: 'tests')

Exit code: 0 if every file's pytest exited 0; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Dict, List, Tuple


# Default test discovery roots.
_DEFAULT_ROOTS = ["tests"]

# Where the kernel exposes this process's CPU quota. Overridable so the unit
# tests can point at a fixture tree instead of the real (unwritable) cgroupfs.
_CGROUP_ROOT = Path("/sys/fs/cgroup")


def effective_cpu_count(cgroup_root: Path = _CGROUP_ROOT) -> Tuple[int, str]:
    """How many CPUs this process may actually use, and where we learned it.

    ``os.cpu_count()`` reports the HOST's cores, which is a lie inside a
    CFS-capped container: ``docker run --cpus 2`` on a 24-core box still
    reports 24 because ``--cpus`` sets a *quota*, not a cpuset. Sizing the
    worker pool off that number oversubscribes the quota by the ratio of the
    two (6x on the ACE-AI CI runners), and the resulting CFS throttling blows
    every wall-clock window and sqlite busy-timeout in the suite.

    Resolution order, first hit wins:
      1. cgroup v2 ``cpu.max``        ("<quota> <period>", or "max <period>")
      2. cgroup v1 ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us`` (-1 = no quota)
      3. ``os.sched_getaffinity(0)``  (respects taskset/cpuset pinning)
      4. ``os.cpu_count()``           (macOS/Windows have no affinity call)

    Quotas are rounded UP: ``--cpus 1.5`` is closer to 2 usable CPUs than 1,
    and flooring a sub-1.0 quota to zero would be nonsense. Any unreadable or
    malformed file degrades to the next source rather than raising — a broken
    cgroupfs must not take down the whole test run.
    """
    # 1. cgroup v2
    try:
        raw = (cgroup_root / "cpu.max").read_text(encoding="utf-8").split()
        if len(raw) == 2 and raw[0] != "max":
            quota, period = int(raw[0]), int(raw[1])
            if quota > 0 and period > 0:
                return max(1, -(-quota // period)), "cgroup-v2"
    except Exception:
        pass

    # 2. cgroup v1
    try:
        quota = int(
            (cgroup_root / "cpu" / "cpu.cfs_quota_us").read_text(encoding="utf-8")
        )
        period = int(
            (cgroup_root / "cpu" / "cpu.cfs_period_us").read_text(encoding="utf-8")
        )
        if quota > 0 and period > 0:
            return max(1, -(-quota // period)), "cgroup-v1"
    except Exception:
        pass

    # 3. scheduler affinity (POSIX only)
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            n = len(getaffinity(0))
            if n > 0:
                return n, "affinity"
        except Exception:
            pass

    # 4. host core count
    return max(1, os.cpu_count() or 4), "cpu_count"


def resolve_worker_count(
    requested: int | None, effective_cpus: int, force: bool = False
) -> int:
    """Clamp a requested worker count to what the CPU quota can actually run.

    ``HERMES_TEST_WORKERS`` (and ``-j``) is a CEILING, not an absolute: it can
    lower the worker count below the quota-derived default but never raise it
    above, because the value that is right for a 4-vCPU hosted runner is 6x
    oversubscribed on a 2-CPU self-hosted container. ``HERMES_TEST_WORKERS_FORCE=1``
    bypasses the clamp for deliberate oversubscription experiments.
    """
    default = max(2, effective_cpus * 2)
    if requested is None:
        return default
    if force:
        return requested
    return min(requested, default)


def format_worker_sizing_log(
    workers: int, effective_cpus: int, requested: int | None, source: str
) -> str:
    """One line naming every input to the sizing decision.

    Printed at startup so a CI log answers "why N workers?" without a repro —
    the clamp is invisible otherwise (the workflow still says 12).
    """
    req = "none" if requested is None else str(requested)
    return (
        f"workers={workers} (effective_cpus={effective_cpus}, "
        f"requested={req}, source={source})"
    )


# Directories to skip during discovery — these suites require real
# external services (a model gateway, a docker daemon with a prebuilt
# image, etc.) and are run in their own dedicated CI jobs:
#
#   tests/e2e/         — .github/workflows/tests.yml :: e2e job
#   tests/integration/ — historical; legacy --ignore flags
#   tests/docker/      — .github/workflows/docker.yml ::
#                        build-amd64 job (runs against the freshly-loaded
#                        nousresearch/hermes-agent:test image, via
#                        ``HERMES_TEST_IMAGE`` so the fixture skips
#                        rebuild). The full pytest-shard runner can't
#                        host these because the session-scoped
#                        ``built_image`` fixture would do a 3-7min
#                        ``docker build``,
#                        so the build is guaranteed to die in fixture
#                        setup. The dedicated job sidesteps both costs.
_SKIP_PARTS = {"integration", "e2e", "docker"}

# Per-file wall-clock cap. Override
# via --file-timeout or HERMES_TEST_FILE_TIMEOUT.
#
# Set to 300s (5 min) deliberately generous: the per-test subprocess
# isolation plugin spawns a fresh Python process per test, so a
# large-collection file pays N × (interpreter startup + import) of
# overhead before any test logic runs — and that overhead dilates under
# load on shared CI runners, producing false "no tests ran" timeouts on
# files that finish in ~100s on a quiet box. The Docker build matrix jobs
# take 7-10 min anyway, so this headroom costs nothing on total CI wall
# time while keeping a genuinely hung file bounded.
_DEFAULT_FILE_TIMEOUT_SECONDS = 300.0

# One-shot retry of failing test FILES. A file that exits non-zero is re-run
# once in a fresh subprocess; if the re-run passes, the file counts as passed
# but is loudly reported as FLAKY so it gets fixed rather than hidden.
# Deterministic failures fail both attempts — a real regression can never be
# laundered into green by this (it would have to flake in our favor twice in
# a row on the same runner, which is exactly the definition of a flake).
# Set to 0 to disable (env: HERMES_TEST_FILE_RETRIES).
_DEFAULT_FILE_RETRIES = 1

# Duration cache: maps relative file paths to last-observed subprocess
# wall-clock seconds. Used by ``--slice`` to distribute files across
# CI jobs by estimated total time, so no one job gets all the slow files.
_DURATIONS_FILE = "test_durations.json"

# Every plugin-scoped CI run keeps one fixed core smoke slice. These files pin
# the gateway, session, and config seams that every plugin is loaded through.
_CORE_SMOKE_TESTS = (
    "tests/gateway/test_gateway_process_exit.py",
    "tests/gateway/test_session.py",
    "tests/gateway/test_config.py",
    "tests/hermes_cli/test_gateway.py",
    "tests/hermes_cli/test_config.py",
    "tests/hermes_state/test_resolve_resume_session_id.py",
)


# A plugin-scoped run also executes every test OUTSIDE tests/plugins/<name>/
# that references the plugin by module path (``plugins.<name>``) or file path
# (``plugins/<name>/``). #905 changed plugins/blackbox/store.py, the merge
# group ran plugin:blackbox (158 tests, green), and main went red on
# tests/test_request_composition.py — a cross-tree blackbox consumer the
# scoped matrix never selected. A plugin with more consumers than this is
# effectively core, so its scope fails open to the full matrix instead of
# packing hundreds of files into one slice.
_MAX_PLUGIN_DEPENDENT_TESTS = 60


def _plugin_dependent_tests(plugin_name: str, repo_root: Path) -> List[Path]:
    """Return test files outside ``tests/plugins/<name>/`` that reference the plugin.

    Plain text match, deliberately over-inclusive: a mention in a comment or
    string selects the file too, which costs one extra file and can never
    under-test.
    """
    name = re.escape(plugin_name)
    pattern = re.compile(
        rf"(?<![\w.-])plugins[./]{name}(?![\w-])"
        rf"|\bfrom\s+plugins\s+import\s+[^\n]*\b{name}\b"
    )
    own_root = (repo_root / "tests" / "plugins" / plugin_name).resolve()
    out: List[Path] = []
    for path in _discover_files([repo_root / root for root in _DEFAULT_ROOTS]):
        if path.resolve().is_relative_to(own_root):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            out.append(path)
    return out


def _split_pathspec(value: str) -> List[str]:
    """Split a separator-joined path list (``--paths``/``--files``/
    ``HERMES_TEST_PATHS``) into individual paths.

    POSIX: ``:``-separated, as documented.

    Windows: ``;`` (``os.pathsep``) and ``:`` are both accepted as
    separators, but a ``:`` that forms a drive letter (``C:\\...`` or
    ``C:/...``) stays glued to its path — a naive ``split(":")`` turns
    ``C:\\repo\\tests`` into ``['C', '\\repo\\tests']``, where the bogus
    ``C`` becomes a phantom discovery root and the rooted remainder only
    resolves by accident of ``Path.__truediv__`` re-anchoring it onto
    ``repo_root``'s drive.
    """
    if sys.platform != "win32":
        return [p for p in value.split(":") if p.strip()]
    parts: List[str] = []
    for chunk in value.split(";"):
        raw = chunk.split(":")
        i = 0
        while i < len(raw):
            part = raw[i]
            if (
                len(part) == 1
                and part.isalpha()
                and i + 1 < len(raw)
                and raw[i + 1][:1] in ("\\", "/")
            ):
                part = f"{part}:{raw[i + 1]}"
                i += 1
            parts.append(part)
            i += 1
    return [p for p in parts if p.strip()]

# Host-OS gating (see the ``_OS_MARKS`` block in tests/conftest.py): tests
# marked for another host are collected and SKIPPED by the conftest hook —
# this runner never executes them, by construction. The summary calls that
# out explicitly so a local run isn't misread as covering macOS/Windows
# behaviour, and names the CI lane where those tests actually execute.
_OS_MARKERS = {
    "linux_only": ("linux", "the main Linux CI lane"),
    "macos_only": ("darwin", "the tests-os CI lane (macos-latest)"),
    "windows_only": ("win32", "the tests-os CI lane (windows-latest)"),
}


def _off_host_marker_files(files: List[Path]) -> dict[str, int]:
    """Count discovered files referencing each marker for an OS we are not on.

    Whole-word text match, same approach as scripts/ci/list_os_marked_tests.py:
    over-counting a prose mention is harmless here (the note is informational);
    what matters is never reporting 0 while gated tests exist.
    """
    off_host = {
        marker: re.compile(rf"\b{marker}\b")
        for marker, (host_prefix, _) in _OS_MARKERS.items()
        if not sys.platform.startswith(host_prefix)
    }
    counts = {marker: 0 for marker in off_host}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker, pattern in off_host.items():
            if pattern.search(text):
                counts[marker] += 1
    return {marker: n for marker, n in counts.items() if n}


def _approximately_count_tests(
    files: List[Path], repo_root: Path
) -> dict[Path, int]:
    """
    Make a decent estimate at individual tests per file.
    Running ``pytest --co -q`` is WAY too slow because it actually imports everything.

    Returns a mapping ``{file_path: test_count}``. Files with zero
    collected tests are omitted from the dict (not an error — e.g. the
    file only defines fixtures / conftest helpers).

    """

    results = {}

    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            contents = f.read()
        results[path] = contents.count("def test_")

    return results


def _discover_files(roots: List[Path]) -> List[Path]:
    """Return every ``test_*.py`` under the given roots (sorted).

    Roots may be directories (recursed for ``test_*.py``) or explicit
    ``.py`` files (included as-is, even if they don't match the
    ``test_*`` prefix — caller knows what they want).

    Exclude any file whose path contains a component in ``_SKIP_PARTS``,
    UNLESS the user explicitly named it as a root (in which case the
    user's intent overrides the skip filter). This makes
    ``scripts/run_tests.sh tests/docker/`` work locally the same way
    ``pytest tests/docker/`` does — the CI-level skip exists to keep
    the sharded matrix from blowing up, not to block targeted runs.
    """
    seen: set[Path] = set()
    out: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            # Explicit file: include it as-is, skip the _SKIP_PARTS filter
            # since the user named it directly.
            real = root.resolve()
            if real not in seen:
                seen.add(real)
                out.append(root)
            continue
        # If the explicit root itself sits inside a skipped dir (e.g.
        # the user said ``tests/docker``), the user has overridden the
        # skip for that subtree. Compute the set of skip-parts the user
        # opted into, and only filter files whose path crosses a
        # skip-part *outside* that opt-in.
        root_skip_overrides = {
            part for part in root.parts if part in _SKIP_PARTS
        }
        effective_skips = _SKIP_PARTS - root_skip_overrides
        for path in root.rglob("test_*.py"):
            if any(part in effective_skips for part in path.parts):
                continue
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            out.append(path)
    return sorted(out)


def _plugin_scope_from_changes(changed_paths: List[str]) -> str:
    """Return ``plugin:<name>`` only for one isolated plugin tree.

    Any empty, malformed, cross-plugin, shared-fixture, or out-of-plugin path
    fails open to ``full``. ``tests/plugins/<name>/`` is the only allowed
    companion tree for ``plugins/<name>/``.
    """
    plugin_name: str | None = None
    paths = [path.strip() for path in changed_paths if path.strip()]
    if not paths or any("\\" in path for path in paths):
        return "full"

    for path in paths:
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            return "full"
        if path.endswith("/conftest.py") or path == "conftest.py":
            return "full"
        if len(parts) >= 3 and parts[0] == "plugins":
            candidate = parts[1]
        elif len(parts) >= 4 and parts[:2] == ["tests", "plugins"]:
            candidate = parts[2]
        else:
            return "full"
        if (
            not candidate.isascii()
            or not candidate.replace("_", "").replace("-", "").isalnum()
        ):
            return "full"
        if plugin_name is None:
            plugin_name = candidate
        elif candidate != plugin_name:
            return "full"

    return f"plugin:{plugin_name}" if plugin_name else "full"


# Default runner labels when no self-hosted pool is configured. Mirrors the
# ``vars.CI_RUNNER_LABELS || '["ubuntu-latest"]'`` fallback in the workflows.
_HOSTED_RUNNER_LABELS = '["ubuntu-latest"]'


def _runs_on_for(
    index: int,
    self_hosted_slots: int | None,
    self_hosted_labels: str,
) -> str:
    """Return the ``runs-on`` label JSON for 1-based slice *index*.

    Slices ``1..self_hosted_slots`` get the self-hosted pool's labels; the
    overflow tail spills onto GitHub-hosted runners, which are unmetered on a
    public repo and let one wave of jobs start at once instead of queueing
    behind a fixed pool. ``self_hosted_slots is None`` means "no cap" — every
    slice stays self-hosted, so an unset ``CI_SELF_HOSTED_SLOTS`` repo variable
    is a no-op and the prior behaviour is exactly preserved.

    Rollback is unchanged: pointing ``CI_RUNNER_LABELS`` at ``ubuntu-latest``
    makes *self_hosted_labels* equal the hosted labels, so every slice is
    hosted regardless of the slot count.
    """
    if self_hosted_slots is None or index <= self_hosted_slots:
        return self_hosted_labels
    return _HOSTED_RUNNER_LABELS


def _resolve_self_hosted_labels(raw: str | None) -> str:
    """Normalize ``--self-hosted-labels`` into schedulable ``runs-on`` JSON.

    Empty/unset mirrors the workflows' ``vars.CI_RUNNER_LABELS || '[...]'``
    fallback. Anything that is not a JSON array of non-empty strings falls
    back to the hosted default and says so on stderr: a malformed value
    emitted into ``runs-on`` makes every matrix job unschedulable, whereas
    the hosted default always runs.
    """
    if raw is None or not raw.strip():
        return _HOSTED_RUNNER_LABELS
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(x, str) and x.strip() for x in parsed)
    ):
        print(
            f"warning: --self-hosted-labels {raw!r} is not a JSON array of "
            f"labels; using {_HOSTED_RUNNER_LABELS}",
            file=sys.stderr,
        )
        return _HOSTED_RUNNER_LABELS
    return json.dumps(parsed, separators=(",", ":"))


def _resolve_self_hosted_slots(raw: str | None) -> int | None:
    """Normalize ``--self-hosted-slots`` into a cap, or ``None`` for no cap.

    Empty/unset/non-numeric/negative all mean "no cap", i.e. every slice stays
    self-hosted exactly as before this flag existed — a missing or fat-fingered
    ``CI_SELF_HOSTED_SLOTS`` repo variable can never move jobs off the pool.
    ``0`` is meaningful and honoured: it spills the entire matrix to hosted
    runners, which is how you drain the pool without touching the workflows.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        print(
            f"warning: --self-hosted-slots {raw!r} is not an integer; "
            "leaving every slice self-hosted",
            file=sys.stderr,
        )
        return None
    if value < 0:
        print(
            f"warning: --self-hosted-slots {value} is negative; "
            "leaving every slice self-hosted",
            file=sys.stderr,
        )
        return None
    return value


def _resolve_x64_hosted_min(raw: str | None) -> int | None:
    """Normalize ``--x64-hosted-min``; ``None`` keeps the legacy ARM trial.

    Empty/unset, non-integer and negative all mean "no floor" and fall back to
    the legacy lightest-N selection, so ``CI_X64_HOSTED_MIN=off`` is the
    rollback for arm-first routing.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = -1
    if value < 0:
        print(
            f"warning: --x64-hosted-min {raw!r} is not a non-negative integer; "
            "using the legacy --arm-hosted-slices selection",
            file=sys.stderr,
        )
        return None
    return value


def _route_arm_slices(
    matrix: dict,
    raw_count: str | None,
    repo_root: Path,
    raw_x64_min: str | None = None,
) -> None:
    """Route eligible slices to ``ubuntu-24.04-arm``.

    ``raw_count`` unset/0/invalid disables ARM entirely (the kill switch).
    Without an x64 floor, the N lightest non-core slices move to ARM from
    either venue (the original trial). With ``raw_x64_min`` = M, routing is
    arm-first: every GitHub-hosted non-core slice moves to ARM except that at
    least M hosted slices stay x64 as arch canaries — the count is taken after
    the self-hosted cap, so no ``CI_SELF_HOSTED_SLOTS`` value the placement
    controller writes can remove them. Self-hosted slices are never moved.
    The heaviest hosted slices go to ARM (faster there); the lightest stay x64.
    """
    try:
        count = int(raw_count or 0)
    except ValueError:
        count = -1
    if count < 0:
        print("warning: --arm-hosted-slices must be non-negative; ARM disabled", file=sys.stderr)
        return
    if not count:
        return
    durations = _load_durations(repo_root)
    core = set(_CORE_SMOKE_TESTS)
    candidates = []
    for slice_ in matrix["slice"]:
        files = _split_pathspec(slice_["files"])
        if not files or slice_["name"] == "core smoke" or core.intersection(files):
            continue
        weight = sum(durations.get(f, 2.0) for f in files)
        candidates.append((weight, slice_["index"], slice_))
    x64_min = _resolve_x64_hosted_min(raw_x64_min)
    if x64_min is None:
        chosen = sorted(candidates, key=lambda item: item[:2])[:count]
    else:
        hosted = [s for s in matrix["slice"] if s["runs_on"] == _HOSTED_RUNNER_LABELS]
        budget = max(0, len(hosted) - x64_min)
        eligible = [c for c in candidates if c[2]["runs_on"] == _HOSTED_RUNNER_LABELS]
        chosen = sorted(eligible, key=lambda item: (-item[0], item[1]))[:budget]
    for _, _, slice_ in chosen:
        slice_["runs_on"] = '["ubuntu-24.04-arm"]'


def _scoped_plugin_matrix(
    scope: str,
    repo_root: Path,
    self_hosted_slots: int | None = None,
    self_hosted_labels: str = _HOSTED_RUNNER_LABELS,
) -> dict[str, list[dict[str, object]]] | None:
    """Build one plugin slice, one pinned core-smoke slice, and — when any
    test outside the plugin's own tree references it — one dependents slice.

    Invalid scope names, missing plugin tests, a missing pinned smoke file, or
    more than ``_MAX_PLUGIN_DEPENDENT_TESTS`` cross-tree consumers return
    ``None`` so the caller can fail open to the full matrix.
    """
    if not scope.startswith("plugin:"):
        return None
    plugin_name = scope.removeprefix("plugin:")
    if (
        not plugin_name
        or not plugin_name.isascii()
        or not plugin_name.replace("_", "").replace("-", "").isalnum()
    ):
        return None

    plugin_root = repo_root / "tests" / "plugins" / plugin_name
    plugin_files = _discover_files([plugin_root])
    smoke_files = [repo_root / path for path in _CORE_SMOKE_TESTS]
    if not plugin_files or any(not path.is_file() for path in smoke_files):
        return None
    dependent_files = _plugin_dependent_tests(plugin_name, repo_root)
    if len(dependent_files) > _MAX_PLUGIN_DEPENDENT_TESTS:
        return None

    matrix: dict[str, list[dict[str, object]]] = {
        "slice": [
            {
                "index": 1,
                "name": f"plugin {plugin_name}",
                "files": ":".join(_format_file(path, repo_root) for path in plugin_files),
                "runs_on": _runs_on_for(1, self_hosted_slots, self_hosted_labels),
            },
            {
                "index": 2,
                "name": "core smoke",
                "files": ":".join(_format_file(path, repo_root) for path in smoke_files),
                # The pinned smoke slice never spills: it is the one slice that
                # gates every plugin-scoped run, so it keeps the fastest
                # runner class unconditionally.
                "runs_on": self_hosted_labels,
            },
        ]
    }
    if dependent_files:
        matrix["slice"].append(
            {
                "index": 3,
                "name": f"plugin {plugin_name} dependents",
                "files": ":".join(
                    _format_file(path, repo_root) for path in dependent_files
                ),
                "runs_on": _runs_on_for(3, self_hosted_slots, self_hosted_labels),
            }
        )
    return matrix


def _kill_tree(proc: "subprocess.Popen", pgid: int | None = None) -> None:
    """Kill the pytest subprocess and every descendant it spawned.

    A test run can spin up uvicorn servers, async runtimes, or other
    long-running grandchildren that survive the pytest subprocess exit
    if we don't kill the whole tree. ``subprocess.Popen.kill()`` only
    targets the immediate child; grandchildren reparent to PID 1
    (Linux) / get adopted by services.exe (Windows) and leak.

    POSIX: the caller must pass ``pgid`` — the process group id captured
    immediately after Popen (via ``os.getpgid(proc.pid)``). We can't
    look it up here in the happy path because by the time we get
    called the leader process has already been reaped and its pid is
    gone from the kernel's process table, even though descendants in
    the group are still alive. SIGKILL'ing the captured pgid takes out
    everything in that group atomically.

    Windows: ``taskkill /F /T /PID`` walks the recorded ppid chain and
    terminates the whole tree, even when the root has already exited.

    Why not psutil: psutil walks the parent-child tree, but in the
    happy path the root has already been reaped so ``psutil.Process(pid)``
    can't find it; grandchildren reparented to PID 1 are also
    unreachable by tree walk at that point. The platform-native
    primitives (process groups / taskkill) handle both cases correctly
    without an extra abstraction layer.
    """
    if proc.pid is None:
        return

    if sys.platform == "win32":
        try:
            
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )  # windows-footgun: ok
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    else:
        # POSIX: kill the captured pgid. Local-import signal so the
        # SIGKILL attribute is never referenced on Windows.
        if pgid is not None:
            try:
                import signal as _signal
                os.killpg(pgid, _signal.SIGKILL)  # windows-footgun: ok
            except (ProcessLookupError, PermissionError, OSError):
                pass

    # Belt-and-suspenders: ensure subprocess.communicate() sees the exit.
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _run_one_file(
    file: Path,
    pytest_args: List[str],
    repo_root: Path,
    file_timeout: float,
    retries: int = 0,
) -> Tuple[Path, int, str, dict[str, int], float]:
    """Run ``python -m pytest <file> <pytest_args>`` in a fresh subprocess.

    Returns (file, returncode, captured_combined_output, summary_counts, subprocess_wall_seconds).

    ``retries`` > 0 enables the one-shot flake retry: a non-zero exit is
    re-run in a fresh subprocess; if the re-run passes, the file counts as
    passed but the output is prefixed with a FLAKY banner and the file/output
    are recorded in ``_FLAKY_RESULTS`` so the summary can call it out. A
    deterministic failure fails every attempt, so real regressions cannot
    be laundered green.

    ``summary_counts`` is the result of ``_parse_pytest_summary(output)`` —

    pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):
        0 = all tests passed
        1 = some tests failed
        2 = test execution interrupted
        3 = internal error
        4 = pytest CLI usage error
        5 = no tests collected

    We treat exit 5 as a pass: it just means every test in the file was
    skipped or filtered by a marker (e.g. ``-m 'not integration'`` skips
    files where every test is marked integration). That's intentional and
    not a failure mode.

    On per-file timeout (``file_timeout`` seconds) or any other exception
    during ``communicate()``, we kill the whole process group / process
    tree so grandchildren (uvicorn servers, async runtimes, etc.) do not
    orphan onto PID 1. This outer timeout exists only to
    bound a pathologically slow or hung file as a whole.
    """
    file, rc, output, summary, subproc_wall = _run_one_file_once(
        file, pytest_args, repo_root, file_timeout
    )
    attempt = 0
    while rc != 0 and attempt < retries:
        attempt += 1
        first_output = output
        file, rc, output, summary, subproc_wall2 = _run_one_file_once(
            file, pytest_args, repo_root, file_timeout
        )
        subproc_wall += subproc_wall2
        if rc == 0:
            output = (
                f"⚠ FLAKY: failed on attempt 1, passed on retry "
                f"(attempt {attempt + 1}). Fix the flake — do not ignore this.\n"
                f"--- first-attempt output ---\n{first_output}\n"
                f"--- retry output ---\n{output}"
            )
            with _flaky_lock:
                _FLAKY_RESULTS.append((file, output))
    return file, rc, output, summary, subproc_wall


# Files that failed once and passed on retry, with both attempts' output.
# Keeping the traceback is load-bearing: a self-healed flake without its
# failing assertion is only a filename, which forces another expensive full
# run to rediscover the race.
_FLAKY_RESULTS: List[Tuple[Path, str]] = []
_flaky_lock = threading.Lock()


def _run_one_file_once(
    file: Path,
    pytest_args: List[str],
    repo_root: Path,
    file_timeout: float,
) -> Tuple[Path, int, str, dict[str, int], float]:
    """Single attempt of a per-file pytest subprocess (see _run_one_file)."""
    cmd = [sys.executable, "-m", "pytest", str(file), *pytest_args]

    # Give this subprocess its own pytest temp root.
    #
    # pytest builds its tmp_path root as <temproot>/pytest-of-<user>/. At the
    # end of a session it walks that directory with cleanup_dead_symlinks().
    # The walk lists the directory. Then it asks whether the `pytest-current`
    # symlink resolves. Then it unlinks the symlink.
    #
    # Every file shared one root. A second process replaced that symlink
    # between the question and the unlink. The first process then died with
    # FileNotFoundError after all of its tests passed.
    #
    # The risk grows with the number of processes that finish together. At 8
    # workers it never occurred. At 144 workers it occurs.
    #
    # One root for each subprocess removes the shared directory that the race
    # needs. The parent deletes the root after the attempt. The same private
    # dir is also passed as an explicit --basetemp (fork behavior): that pins
    # request.config.option.basetemp per file so independent processes never
    # share pytest-of-<user> stale-directory cleanup at all. An explicit
    # caller --basetemp still wins (and then gets no runner cleanup).
    env = os.environ.copy()
    # skipping writing bytecode because we're running a bunch of parallel
    # python processes on the same code (fork parity)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    private_basetemp: Path | None = None
    if not any(
        arg == "--basetemp" or arg.startswith("--basetemp=")
        for arg in pytest_args
    ):
        private_basetemp = Path(
            tempfile.mkdtemp(prefix="hermes-pytest-tmproot-")
        )
        env["PYTEST_DEBUG_TEMPROOT"] = str(private_basetemp)
        cmd.append(f"--basetemp={private_basetemp}")

    subproc_start = time.monotonic()
    timed_out = False
    # launch the pytest process
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
            # POSIX: place the child at the head of its own process group so
            # _kill_tree can SIGKILL the group atomically.
            # Windows: this maps to CREATE_NEW_PROCESS_GROUP in CPython 3.12+;
            # _kill_tree handles the Windows path via taskkill /F /T.
            start_new_session=True,
        )
    except BaseException:
        # A spawn failure must not leak the runner-owned temp root.
        if private_basetemp is not None:
            shutil.rmtree(private_basetemp, ignore_errors=True)
        raise

    # Capture the pgid NOW, before the leader can exit and be reaped. Once
    # the leader is reaped, os.getpgid(proc.pid) raises ProcessLookupError
    # even though grandchildren in that group are still alive — defeating
    # the whole cleanup. None on Windows where the pgid concept doesn't apply.
    pgid: int | None = None
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None

    try:
        output, _ = proc.communicate(timeout=file_timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc, pgid=pgid)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = "(file timeout exceeded; output unavailable)"
        rc = 124  # de facto convention for "killed by timeout".
        output = (
            f"({file_timeout:.0f}s exceeded; "
            f"process tree SIGKILL'd)\n{output}"
        )
    except BaseException:
        # KeyboardInterrupt / runner crash — make sure no zombie
        # grandchildren outlive us.
        _kill_tree(proc, pgid=pgid)
        raise
    else:
        # Happy path: pytest exited on its own. Kill the group anyway in
        # case it left grandchildren behind; already-dead is a no-op.
        _kill_tree(proc, pgid=pgid)

        output +=  "\n"
    finally:
        # Delete the temp root for this attempt. Nothing reads it after the
        # subprocess exits. More than 3000 of them fill the disk of the
        # runner over one suite.
        if private_basetemp is not None:
            shutil.rmtree(private_basetemp, ignore_errors=True)

    summary = _parse_pytest_summary(output)
    if timed_out:
        # The per-file wall ceiling fired and we SIGKILL'd pytest. A killed
        # pytest never prints its `=== N passed ... ===` summary, so the
        # parsed counts are empty — which used to be indistinguishable from
        # "collected nothing" and got reported as a no-op. Record the real
        # event instead: how far the run got before it was killed.
        summary.update(_parse_timeout_progress(output))
        summary["timed_out"] = 1
        summary["timeout_secs"] = int(file_timeout)
    if rc == 5:
        # No tests collected — every test in the file was filtered out.
        # Treat as a pass (a correctly marker-filtered file SHOULD be a
        # no-op in the unit lane) BUT record the no-op instead of erasing
        # it: tag the summary so the aggregate verdict site can see that
        # this file collected zero tests. Without this tag, an exit-5 file
        # is coerced to rc=0, never enters `failures`, and a whole-suite
        # zero-collect run reports green ("green because it didn't run").
        # (upstream parity note: platform-gated / fully-marker-filtered files
        # collect nothing legitimately; the RUN-level guard in main() still
        # fails when NOTHING was collected across every file.)
        rc = 0
        summary["noop_exit5"] = 1
        # Distinguish a LEGITIMATE optional-dep skip (a module-level
        # ``pytest.importorskip("numpy")`` or ``pytest.skip(..., allow_module_level=True)``
        # aborts collection with exit-5 AND prints a skip line, but reports 0
        # collected — so `skipped` stays 0 and it would otherwise trip the
        # explicit-no-op RED gate) from a genuine no-op (bad selector / broken
        # import). A skip reason in the output means the file opted out on
        # purpose; tag it so Gate 2 excludes it (skip ≠ caller-intent no-op).
        _low = output.lower()
        if ("importorskip" in _low or "skipped" in _low
                or "allow_module_level" in _low or "collected 0 items / 1 skipped" in _low):
            summary["noop_skip"] = 1
        else:
            # No skip reason. Distinguish an INTENTIONALLY testless file (an empty
            # tombstone left after a feature was removed, or a __main__-driven
            # standalone script with no pytest test functions at all) from a file
            # that SHOULD have tests but collected zero (a real no-op / broken
            # selector). A file whose source defines no ``def test``/``class Test``
            # is not a pytest target — flag it ⚠, don't RED the whole suite on it.
            try:
                _src = file.read_text(encoding="utf-8", errors="replace")
                import re as _re
                _has_tests = bool(_re.search(r"^\s*(async\s+)?def\s+test|^\s*class\s+Test",
                                             _src, _re.MULTILINE))
                if not _has_tests:
                    summary["noop_testless"] = 1
            except Exception:
                pass
    subproc_wall = time.monotonic() - subproc_start
    return file, rc, output, summary, subproc_wall


def _looks_like_no_collection(output: str, summary: dict) -> bool:
    """True only with POSITIVE evidence that the file collected no tests.

    An absent counts line is NOT such evidence (a pytest killed mid-run also
    has none — that was the misdiagnosis this predicate exists to stop).
    Accepts: pytest's own zero-collect phrasing, the exit-5 no-op tags this
    runner sets, or a collection/import error.
    """
    if summary.get("noop_exit5") or summary.get("noop_skip") or summary.get("noop_testless"):
        return True
    low = output.lower()
    return (
        "no tests ran" in low
        or "collected 0 items" in low
        or "error collecting" in low
        or "errors during collection" in low
        or "importerror" in low
        or "modulenotfounderror" in low
        or "no tests collected" in low
    )


def _format_timeout_verdict(output: str, summary: dict) -> str:
    """Human verdict for a file killed at the per-file wall ceiling.

    e.g. ``TIMED OUT after 402s at ~39% (81 collected), last test reached:
    tests/x.py::test_foo``. Each clause is emitted only when the evidence for
    it is actually present in the captured output — no invented numbers.
    """
    parts = [f"TIMED OUT after {summary.get('timeout_secs', 0)}s"]
    pct = summary.get("progress_pct")
    if pct is not None:
        parts.append(f"at ~{pct}%")
    collected = summary.get("collected")
    if collected is not None:
        parts.append(f"({collected} collected)")
    verdict = " ".join(parts)
    last = _last_test_reached(output)
    if last:
        verdict += f", last test reached: {last}"
    return verdict


def _parse_timeout_progress(output: str) -> dict[str, int]:
    """Extract how far a KILLED pytest got, from its partial output.

    A pytest killed at the per-file wall ceiling never prints its counts
    line, so ``_parse_pytest_summary`` returns ``{}`` — indistinguishable
    from "collected nothing" unless we read the progress it DID print.

    Returns (only keys that were found):
      ``collected``  — N from ``collected N items`` / ``collected N item``
                       (also ``N items / M deselected`` and ``collecting N``)
      ``progress_pct`` — the last ``[ NN%]`` progress marker pytest printed.

    Both are evidence that collection SUCCEEDED and tests were running, which
    is what routes the file to the "timed out mid-run" verdict instead of the
    no-op gate.
    """
    result: dict[str, int] = {}
    m_collected = None
    for m in re.finditer(r"collected\s+(\d+)\s+items?", output):
        m_collected = m
    if m_collected is not None:
        result["collected"] = int(m_collected.group(1))
    # Last progress marker pytest emitted, e.g. "........ [ 39%]".
    last_pct = None
    for m in re.finditer(r"\[\s*(\d{1,3})%\]", output):
        last_pct = m
    if last_pct is not None:
        result["progress_pct"] = int(last_pct.group(1))
    return result


def _last_test_reached(output: str) -> str | None:
    """Best-effort name of the last test pytest STARTED before being killed.

    Only available when the run was verbose (``-v``): pytest then prints the
    ``path::test_name`` line when it STARTS a test, before its status. So the
    final such line names the test that was in flight when the kill landed —
    i.e. the hang itself. (Verified live: a deliberate 300 s sleep in
    ``test_c_hangs`` produced exactly that name.) Returns None on non-verbose
    output rather than guessing.
    """
    last: str | None = None
    for m in re.finditer(r"^(\S+::\S+)\s", output, re.MULTILINE):
        last = m.group(1)
    return last


def _parse_pytest_summary(output: str) -> dict[str, int]:
    """Extract per-file test pass/fail/skip counts from pytest output.

    pytest prints a summary line like ``12 passed, 3 skipped, 1 failed in 2.1s``
    as the last non-empty line before the short test summary.  We scrape that
    line for the individual counts so the progress display can show test-level
    granularity instead of just file-level pass/fail.

    Returns a dict with keys ``passed``, ``failed``, ``skipped``, ``errors``,
    ``xfailed``, ``xpassed`` (only keys found in the output are present).
    """
    result: dict[str, int] = {}
    # Walk backwards from the end — the summary line is always near the tail.
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Match "N passed", "N failed", "N skipped", "N errors", "N xfailed", "N xpassed"
        for m in re.finditer(r"(\d+)\s+(passed|failed|skipped|errors|xfailed|xpassed)", line):
            result[m.group(2)] = int(m.group(1))
        # Also match "N error" (singular — pytest uses this sometimes).
        for m in re.finditer(r"(\d+)\s+error\b", line):
            result.setdefault("errors", result.get("errors", 0) + int(m.group(1)))
        if result:
            # Found the counts line — done.
            break
        # Stop at the short test summary header (if any) — everything above
        # that is individual failure details, not the counts line.
        if line.startswith("FAILED") or line.startswith("SHORT TEST SUMMARY"):
            break
    return result


def _format_file(file: Path, repo_root: Path) -> str:
    """Render a test-file path for display: strip the repo-root prefix
    when possible so output reads ``tests/acp/test_auth.py`` instead of
    ``/home/runner/work/hermes-agent/hermes-agent/tests/acp/test_auth.py``.

    Falls back to the absolute path for anything outside the repo root.
    """
    try:
        return str(file.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(file)


def _print_progress(
    tests_done: int,
    approx_total_tests: int,
    file: Path,
    rc: int,
    dur: float,
    repo_root: Path,
    tests_passed: int,
    tests_failed: int,
    test_counts: dict[Path, int],
    file_summary: dict[str, int] | None = None,
    subproc_wall: float | None = None,
) -> None:
    """Single-line live progress.

    When ``file_summary`` is provided (parsed from pytest output), the
    per-file parenthetical shows individual test pass/fail counts instead
    of just the total test count.

    ``subproc_wall`` is the actual subprocess wall-clock time (excluding
    queue-wait). When available, the display shows both the subprocess
    time and the queue-inclusive elapsed time.
    """
    status = "✓" if rc == 0 else "✗"
    pct = min((tests_done / approx_total_tests * 100), 100) if approx_total_tests else 0
    # Digit width for left-side counter padding (derived from total file count).
    fw = len(str(tests_passed + tests_failed))
    # Build per-file test count string.
    if file_summary:
        parts = []
        p = file_summary.get("passed", 0)
        f = file_summary.get("failed", 0)
        s = file_summary.get("skipped", 0)
        e = file_summary.get("errors", 0)
        if p:
            parts.append(f"{p}✓")
        if f:
            parts.append(f"{f}✗")
        if s:
            parts.append(f"{s}s")
        if e:
            parts.append(f"{e}e")
        # xfailed/xpassed are rare; include if present.
        xf = file_summary.get("xfailed", 0)
        xp = file_summary.get("xpassed", 0)
        if xf:
            parts.append(f"{xf}xf")
        if xp:
            parts.append(f"{xp}xp")
        test_str = " ".join(parts) + ", " if parts else ""
    else:
        n_tests = test_counts.get(file, 0)
        test_str = f"{n_tests} tests, " if n_tests else ""
    if file_summary and file_summary.get("timed_out"):
        # A killed pytest parses to no counts, so the line would otherwise
        # read like an ordinary slow file. Name the kill.
        _c = file_summary.get("collected")
        _p = file_summary.get("progress_pct")
        test_str = "TIMED OUT"
        if _p is not None:
            test_str += f" at ~{_p}%"
        if _c is not None:
            test_str += f", {_c} collected"
        test_str += ", "
    # Show subprocess time when available; fall back to queue-inclusive dur.
    if subproc_wall is not None:
        time_str = f"{subproc_wall:.1f}s"
    else:
        time_str = f"{dur:.1f}s"
    msg = (
        f"[{pct:5.1f}% | {tests_done:>5}/~{approx_total_tests}"
        f" | ✓{tests_passed:>{fw}} | ✗{tests_failed:>{fw}}] "
        f"{status} {_format_file(file, repo_root)} ({test_str}{time_str})"
    )
    # Truncate to terminal width if available (no clobbering ANSI lines).
    try:
        cols = os.get_terminal_size().columns
        if len(msg) > cols:
            msg = msg[: cols - 1] + "…"
    except OSError:
        pass
    print(msg, flush=True)


def _print_inline_failure(
    file: Path, output: str, repo_root: Path, pytest_passthrough: List[str]
) -> None:
    """Print a compact failure summary immediately when a file fails.

    Shows the tail of the pytest output (the failure section with stack
    traces) and a ready-to-run repro command, so the developer doesn't
    have to wait for the full run to finish before seeing what broke.
    """
    rel = _format_file(file, repo_root)
    # Build a repro command the developer can copy-paste.
    passthrough_str = " ".join(pytest_passthrough) if pytest_passthrough else ""
    repro = f"python -m pytest {rel}"
    if passthrough_str:
        repro += f" {passthrough_str}"

    # Grab just the failure lines (last ~30 lines of pytest output —
    # typically the FAILED summary + short test info).
    lines = output.rstrip().splitlines()
    tail = "\n".join(lines[-30:])

    print(flush=True)
    print(f"  ╔╍ Failed: {rel} ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍", flush=True)
    for line in tail.splitlines():
        print(f"  ║ {line}", flush=True)
    print("  ║", flush=True)
    print(f"  ║  Repro: {repro}", flush=True)
    print("  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍", flush=True)
    print(flush=True)


def _load_durations(repo_root: Path) -> dict[str, float]:
    """Read the duration cache from the repo root.

    Returns a dict mapping relative file paths (e.g.
    ``tests/tools/test_code_execution.py``) to wall-clock seconds from
    the last run. Missing or corrupt file → empty dict (safe fallback).
    """
    path = repo_root / _DURATIONS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print("[ERROR] Failed to load json durations file! {e}")
        return {}


def _save_durations(
    file_times: List[Tuple[Path, float]],
    repo_root: Path,
) -> None:
    """Write the duration cache so future ``--slice`` runs can use it.

    Merges with any existing cache so entries from files not in the
    current run (e.g. from a different slice) are preserved. Keys are
    repo-relative paths so the cache is portable across checkouts
    and CI runners.
    """
    data: dict[str, float] = _load_durations(repo_root)
    for f, t in file_times:
        key = _format_file(f, repo_root)
        data[key] = round(t, 3)
    path = repo_root / _DURATIONS_FILE
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _compute_lpt_slices(
    files: List[Path],
    slice_count: int,
    durations: dict[str, float],
    repo_root: Path,
) -> List[List[Path]]:
    """Distribute files across N slices using LPT (Longest Processing Time first).

    Sorts files by estimated duration descending, then greedily assigns each
    file to the slice with the smallest accumulated time so far. This
    minimizes the makespan (max slice duration) and keeps CI jobs balanced.

    Files with no cached duration get a default estimate of 2.0s (roughly
    the P50 from profiling). This means first-time runs (no cache) still
    get reasonable distribution, and new files don't all land in one slice.

    Returns a list of N file-lists, one per slice (0-indexed).
    """
    if slice_count < 2:
        return [files]

    default_dur = 2.0
    file_durs: List[Tuple[Path, float]] = []
    for f in files:
        rel = _format_file(f, repo_root)
        dur = durations.get(rel, default_dur)
        file_durs.append((f, dur))

    # Sort longest first (LPT).
    file_durs.sort(key=lambda x: x[1], reverse=True)

    # Greedy assignment: for each file, add it to the slice with the
    # smallest current total.
    bucket_files: List[List[Path]] = [[] for _ in range(slice_count)]
    bucket_totals: List[float] = [0.0] * slice_count

    for f, dur in file_durs:
        min_idx = min(range(slice_count), key=lambda i: bucket_totals[i])
        bucket_files[min_idx].append(f)
        bucket_totals[min_idx] += dur

    return bucket_files


def _slice_files(
    files: List[Path],
    slice_index: int,
    slice_count: int,
    durations: dict[str, float],
    repo_root: Path,
) -> List[Path]:
    """Return the subset of *files* belonging to slice *slice_index*.

    Uses :func:`_compute_lpt_slices` for LPT distribution.

    ``slice_index`` is 1-indexed (1..slice_count) for ergonomics —
    ``--slice 1/4`` reads more naturally than ``--slice 0/4``.
    """
    if slice_count < 2:
        return files
    if not (1 <= slice_index <= slice_count):
        print(
            f"error: --slice index must be 1..{slice_count}, got {slice_index}",
            file=sys.stderr,
        )
        sys.exit(2)

    bucket_files = _compute_lpt_slices(files, slice_count, durations, repo_root)

    target = bucket_files[slice_index - 1]
    target_dur = sum(
        durations.get(_format_file(f, repo_root), 2.0) for f in target
    )
    total_dur = sum(
        durations.get(_format_file(f, repo_root), 2.0)
        for bucket in bucket_files
        for f in bucket
    )
    print(
        f"Slice {slice_index}/{slice_count}: {len(target)} files "
        f"(~{target_dur:.0f}s estimated of {total_dur:.0f}s total)",
        flush=True,
    )

    return target


def _make_stdio_glyph_safe() -> None:
    """Keep status glyphs from killing the runner on narrow console encodings.

    On native Windows, piped or legacy-console stdio defaults to a locale
    codec (usually cp1252) that cannot encode the ✓/✗ progress glyphs — the
    first per-file status line then dies with UnicodeEncodeError before a
    single test result is reported. Declare the runner's own output UTF-8
    (what CI and every modern terminal already are), with errors="replace"
    as the can't-crash backstop; where the encoding can't be changed, fall
    back to errors="replace" alone so glyphs degrade to "?" instead of
    killing the run. On already-UTF-8 stdio this is a no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def main() -> int:
    _make_stdio_glyph_safe()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help=(
            "Parallel worker count CEILING (default: $HERMES_TEST_WORKERS, "
            "else effective_cpus*2). Clamped to max(2, effective_cpus*2) — "
            "where effective_cpus comes from the cgroup CPU quota, not the "
            "host core count — unless HERMES_TEST_WORKERS_FORCE=1."
        ),
    )
    parser.add_argument(
        "--paths",
        default=os.environ.get("HERMES_TEST_PATHS", ":".join(_DEFAULT_ROOTS)),
        help=(
            "Colon-separated discovery roots (default: 'tests'). On "
            "Windows, ';' also separates and drive letters (C:\\...) are "
            "kept intact."
        ),
    )
    parser.add_argument(
        "--include-integration",
        action="store_true",
        help="Don't skip integration/ e2e/ during discovery",
    )
    parser.add_argument(
        "--file-timeout",
        type=float,
        default=float(
            os.environ.get("HERMES_TEST_FILE_TIMEOUT", _DEFAULT_FILE_TIMEOUT_SECONDS)
        ),
        help=(
            "Per-file wall-clock cap in seconds. On timeout, the pytest "
            "subprocess and its full process tree are SIGKILL'd. "
            f"Default: {_DEFAULT_FILE_TIMEOUT_SECONDS}s ({round(_DEFAULT_FILE_TIMEOUT_SECONDS/60)} min), env: HERMES_TEST_FILE_TIMEOUT."
        ),
    )
    parser.add_argument(
        "--file-retries",
        type=int,
        default=int(
            os.environ.get("HERMES_TEST_FILE_RETRIES", _DEFAULT_FILE_RETRIES)
        ),
        help=(
            "Re-run a failing test FILE this many times in a fresh subprocess "
            "before declaring it failed. A pass-on-retry counts as passed but "
            "is reported as FLAKY in the summary. 0 disables. "
            f"Default: {_DEFAULT_FILE_RETRIES}, env: HERMES_TEST_FILE_RETRIES."
        ),
    )
    parser.add_argument(
        "--slice",
        metavar="I/N",
        help=(
            "Run only slice I of N (e.g. --slice 1/4). "
            "Files are distributed across slices using cached durations "
            "so each slice takes roughly equal wall time. "
            "Without a duration cache, files are distributed by count. "
            "Env: HERMES_TEST_SLICE (format: I/N)."
        ),
    )
    parser.add_argument(
        "--generate-slices",
        metavar="N",
        type=int,
        help=(
            "Discover test files, distribute them across N slices using "
            "LPT on cached durations, and print a JSON matrix to stdout "
            "then exit (no tests run). The JSON has the shape "
            "'{\"slices\": [{\"index\": 1, \"files\": [\"tests/foo.py\", ...]}, ...]}' "
            "so the CI generate job can feed it directly into a matrix."
        ),
    )
    parser.add_argument(
        "--files",
        metavar="LIST",
        help=(
            "Explicit colon-separated list of test files to run (on "
            "Windows, ';' also separates and drive letters are kept "
            "intact). Bypasses discovery entirely — used by CI matrix "
            "jobs that receive their file list from the generate job."
        ),
    )
    parser.add_argument(
        "--self-hosted-slots",
        metavar="K",
        default=None,
        help=(
            "Number of matrix slices to stamp with --self-hosted-labels; "
            "slices past K spill onto GitHub-hosted runners. Empty or "
            "unset means no cap (every slice self-hosted). "
            "Env/CI source: vars.CI_SELF_HOSTED_SLOTS."
        ),
    )
    parser.add_argument(
        "--arm-hosted-slices",
        metavar="N",
        default=None,
        help="Route the N lightest non-core slices to ubuntu-24.04-arm (default 0).",
    )
    parser.add_argument(
        "--x64-hosted-min",
        metavar="M",
        default=None,
        help=(
            "Arm-first routing: with --arm-hosted-slices > 0, move every "
            "GitHub-hosted non-core slice to ubuntu-24.04-arm except M, which "
            "stay x64 as canaries. Unset/invalid keeps the lightest-N trial. "
            "Env/CI source: vars.CI_X64_HOSTED_MIN (workflow default 2)."
        ),
    )
    parser.add_argument(
        "--self-hosted-labels",
        metavar="JSON",
        default=_HOSTED_RUNNER_LABELS,
        help=(
            "JSON array of runner labels for the self-hosted slices, e.g. "
            '\'["self-hosted","hermes-ci"]\'. Empty or unset falls back to '
            f"{_HOSTED_RUNNER_LABELS} so every slice is GitHub-hosted. "
            "Env/CI source: vars.CI_RUNNER_LABELS."
        ),
    )
    parser.add_argument(
        "--changed-files-scope",
        action="store_true",
        help=(
            "Read newline-separated changed paths from stdin, print either "
            "'plugin:<name>' or fail-open 'full', then exit."
        ),
    )
    parser.add_argument(
        "--test-scope",
        default="full",
        help=(
            "CI slice scope from --changed-files-scope. A valid isolated "
            "'plugin:<name>' scope emits one plugin slice plus one core-smoke "
            "slice; anything else fails open to the full matrix."
        ),
    )
    parser.add_argument(
        "--min-tests",
        metavar="N",
        type=int,
        default=None,
        help=(
            "No-op floor: fail the whole run RED if fewer than N tests were "
            "actually executed (passed+failed). OFF by default. Opt-in per "
            "invocation for a suite whose expected count the operator knows — "
            "catches a whole directory silently vanishing from discovery."
        ),
    )
    parser.add_argument(
        "--strict-noop",
        dest="strict_noop",
        action="store_true",
        default=False,
        help=(
            "OPT-IN: hard RED on an EXPLICITLY-requested test file that executed "
            "0 tests (--files entries / positional .py files). OFF by default "
            "because a full suite run legitimately lists many files that filter to "
            "zero in a given lane (marker-filtered integration tests, platform "
            "importorskips, module skipif). Whole-suite-zero is ALWAYS a hard RED "
            "regardless of this flag (the load-bearing 'green because it didn't "
            "run' guard); skip-storm/testless ⚠ surfacing is also unconditional. "
            "Turn this on for a targeted run where every named file MUST have run."
        ),
    )
    parser.add_argument(
        "--no-strict-noop",
        dest="strict_noop",
        action="store_false",
        help="Explicit off (already the default); kept for back-compat.",
    )
    parser.add_argument(
        "paths_positional",
        nargs="*",
        metavar="PATH",
        help=(
            "Restrict discovery to these paths (directories or .py files). "
            "Mutually exclusive with --paths. Anything after a literal '--' "
            "separator is passed through to each per-file pytest invocation."
        ),
    )
    # Split argv into "our flags + positional paths" vs "pytest passthrough".
    #
    # Two ways to pass args through to the per-file pytest invocation:
    #   1. Explicit ``--`` separator: everything after it goes to pytest.
    #   2. Bare pytest flags anywhere before ``--``: any token starting with
    #      ``-`` that isn't one of OUR options is routed to pytest, so a bare
    #      ``-q`` / ``-v`` / ``-x`` / ``--tb=long`` / ``-k expr`` "just works"
    #      without the developer remembering the ``--``. This matches the
    #      docstring's promise and pytest muscle-memory.
    #
    # The subtlety bare-flag routing must handle: value-taking pytest flags
    # given in space-separated form (``-k expr``, ``-m mark``, ``-p plugin``,
    # ``-o name=val``). Naively, ``expr`` would look like a positional path and
    # clobber discovery. We peel the following token along with such flags so
    # it never reaches our positional ``paths``. ``=``-joined forms
    # (``-k=expr``, ``--tb=long``) are self-contained and need no lookahead.
    OUR_FLAGS = {
        "-j", "--jobs", "--paths", "--include-integration",
        "--file-timeout", "--file-retries", "--slice", "--generate-slices", "--files",
        "--changed-files-scope", "--test-scope",
        "--self-hosted-slots", "--self-hosted-labels", "--arm-hosted-slices",
        "--x64-hosted-min",
        "--min-tests", "--strict-noop", "--no-strict-noop",
    }
    # pytest short flags that consume the NEXT token as their value.
    PYTEST_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-c", "-r", "-W"}

    def _is_our_flag(tok: str) -> bool:
        # Match exact (``-j``, ``--paths``), ``=``-joined (``--paths=x``),
        # and attached short-value (``-j4``) forms of our own options.
        if tok in OUR_FLAGS:
            return True
        head = tok.split("=", 1)[0]
        if head in OUR_FLAGS:
            return True
        # Attached short value, e.g. ``-j4`` → ``-j``.
        if len(tok) > 2 and tok[:2] in OUR_FLAGS and not tok[1] == "-":
            return True
        return False

    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        before, explicit_passthrough = argv[:sep], argv[sep + 1 :]
    else:
        before, explicit_passthrough = argv, []

    our_args: List[str] = []
    bare_passthrough: List[str] = []
    i = 0
    while i < len(before):
        tok = before[i]
        if tok.startswith("-") and not _is_our_flag(tok):
            bare_passthrough.append(tok)
            # Pull the value token for space-separated value flags.
            if tok in PYTEST_VALUE_FLAGS and i + 1 < len(before):
                bare_passthrough.append(before[i + 1])
                i += 2
                continue
        else:
            our_args.append(tok)
        i += 1

    args = parser.parse_args(our_args)

    # ── Worker sizing: the CPU QUOTA is the ceiling, not the host core count ─
    # An explicit -j (or $HERMES_TEST_WORKERS) is a REQUEST that can only lower
    # the count. CI pins 12, which is right on a 4-vCPU hosted runner and 6x
    # oversubscribed inside a --cpus=2 self-hosted container; the resulting CFS
    # throttling was ejecting merge-queue entries on wall-clock and sqlite
    # busy-timeout flakes. HERMES_TEST_WORKERS_FORCE=1 restores the old
    # take-the-number-literally behaviour for deliberate experiments.
    _requested = args.jobs
    if _requested is None:
        _env_workers = os.environ.get("HERMES_TEST_WORKERS")
        if _env_workers:
            try:
                _requested = int(_env_workers)
            except ValueError:
                print(
                    f"warning: ignoring non-integer HERMES_TEST_WORKERS={_env_workers!r}",
                    file=sys.stderr,
                    flush=True,
                )
    _effective_cpus, _cpu_source = effective_cpu_count()
    args.jobs = resolve_worker_count(
        _requested,
        _effective_cpus,
        force=os.environ.get("HERMES_TEST_WORKERS_FORCE") == "1",
    )
    print(
        format_worker_sizing_log(
            workers=args.jobs,
            effective_cpus=_effective_cpus,
            requested=_requested,
            source=_cpu_source,
        ),
        # stderr, not stdout: `--generate-slices` stdout is captured verbatim
        # by CI (`MATRIX=$(...)` → `fromJSON`), so any extra stdout line kills
        # the generate job. stderr still shows in the job log.
        file=sys.stderr,
        flush=True,
    )

    # ── Node-id selectors → file + ``-k`` filter ────────────────────────────
    # This runner is FILE-granular: it spawns one ``pytest <file>`` per test
    # file. A pytest node id (``tests/foo.py::TestBar::test_baz``) is not an
    # existing path, so discovery silently dropped it and the run exited with
    # "No test files to run" — the selector looked accepted but nothing ran.
    # Translate instead: run the FILE and narrow with ``-k`` on the last
    # segment, which is what the caller meant.
    node_id_selectors: List[Tuple[str, str]] = []
    if args.paths_positional:
        translated: List[str] = []
        for raw in args.paths_positional:
            if "::" not in raw:
                translated.append(raw)
                continue
            file_part, _, selector = raw.partition("::")
            leaf = selector.rsplit("::", 1)[-1]
            # Strip a parametrized id (``test_x[case]``) down to the function
            # name; ``-k`` matches substrings, and brackets are -k syntax.
            leaf = leaf.split("[", 1)[0]
            node_id_selectors.append((raw, leaf))
            translated.append(file_part)
        if node_id_selectors:
            args.paths_positional = translated
            keys = [leaf for _, leaf in node_id_selectors]
            expr = " or ".join(dict.fromkeys(keys))
            for raw, leaf in node_id_selectors:
                print(
                    f"note: '{raw}' is a pytest node id; this runner is "
                    f"file-granular. Running the file with -k {leaf!r}.",
                    file=sys.stderr,
                )
            # Only inject -k when the caller didn't pass one themselves; their
            # explicit filter wins over our inferred one.
            if not any(
                t == "-k" or t.startswith("-k=") or (t.startswith("-k") and len(t) > 2)
                for t in bare_passthrough + explicit_passthrough
            ):
                bare_passthrough = bare_passthrough + ["-k", expr]

    # Bare flags run before any explicit ``--`` passthrough so ordering is
    # intuitive (``run_tests.sh tests/foo.py -q -- --tb=long`` → ``-q --tb=long``).
    pytest_passthrough = bare_passthrough + explicit_passthrough

    # Parse --slice (or HERMES_TEST_SLICE) early so we can exit on bad input
    # before doing any expensive discovery.
    slice_raw = args.slice or os.environ.get("HERMES_TEST_SLICE")
    slice_index: int | None = None
    slice_count: int = 1
    if slice_raw:
        try:
            idx_s, count_s = slice_raw.split("/", 1)
            slice_index = int(idx_s)
            slice_count = int(count_s)
        except (ValueError, AttributeError):
            print(f"error: --slice must be I/N (e.g. 1/4), got: {slice_raw!r}", file=sys.stderr)
            sys.exit(2)

    repo_root = Path(__file__).resolve().parent.parent

    if args.changed_files_scope:
        print(_plugin_scope_from_changes(sys.stdin.read().splitlines()))
        return 0

    self_hosted_labels = _resolve_self_hosted_labels(args.self_hosted_labels)
    self_hosted_slots = _resolve_self_hosted_slots(args.self_hosted_slots)

    if args.generate_slices is not None and args.test_scope != "full":
        scoped_matrix = _scoped_plugin_matrix(
            args.test_scope, repo_root, self_hosted_slots, self_hosted_labels
        )
        if scoped_matrix is not None:
            _route_arm_slices(
                scoped_matrix, args.arm_hosted_slices, repo_root, args.x64_hosted_min
            )
            print(
                f"Test scope: {args.test_scope} + core smoke"
                f" ({len(scoped_matrix['slice'])} slices)",
                file=sys.stderr,
            )
            print(json.dumps(scoped_matrix))
            return 0
        print(
            f"Test scope {args.test_scope!r} is invalid or incomplete; failing open to full",
            file=sys.stderr,
        )

    # --files: explicit file list from the CI generate job — skip discovery.
    # Track which files were *explicitly requested* (by --files or by a
    # positional .py path) vs. discovered by directory recursion. An
    # explicitly-requested file that collects 0 tests is a no-op the caller
    # did not intend (they asked for those specific tests) → hard RED under
    # --strict-noop. A default-discovery marker-filtered 0-collect file is
    # legitimate and only ⚠-surfaced.
    explicit_files: set[Path] = set()
    if args.files:
        files = [repo_root / f for f in _split_pathspec(args.files)]
        roots = []
        explicit_files = {f.resolve() for f in files}
    else:
        # Resolve discovery roots: positional path args override --paths if any
        # were supplied, otherwise --paths (which itself defaults to 'tests').
        if args.paths_positional:
            roots = [repo_root / p for p in args.paths_positional]
            # A positional that names a .py file directly is an explicit
            # request for that file's tests.
            explicit_files = {
                (repo_root / p).resolve()
                for p in args.paths_positional
                if str(p).endswith(".py")
            }
        else:
            roots = [repo_root / p for p in _split_pathspec(args.paths)]

        if args.include_integration:
            # Caller takes responsibility — typically used via explicit -k filter.
            global _SKIP_PARTS  # noqa: PLW0603 — config knob
            _SKIP_PARTS = set()

        files = _discover_files(roots)

    if not files:
        print("No test files to run", file=sys.stderr)
        return 1

    # --generate-slices: compute LPT distribution and emit JSON, then exit.
    if args.generate_slices is not None:
        durations = _load_durations(repo_root)
        slices = _compute_lpt_slices(
            files, args.generate_slices, durations, repo_root
        )
        matrix = {
            "slice": [
                {
                    "index": i + 1,
                    "name": f"slice {i + 1}/{args.generate_slices}",
                    "files": ":".join(_format_file(f, repo_root) for f in bucket),
                    # Head of the matrix stays on the self-hosted pool, the
                    # tail spills to GitHub-hosted. LPT seeds buckets with the
                    # longest files first, so the low indices carry the
                    # heaviest individual files — the ones that benefit most
                    # from the faster pool and from LAN git-mirror alternates.
                    "runs_on": _runs_on_for(
                        i + 1, self_hosted_slots, self_hosted_labels
                    ),
                }
                for i, bucket in enumerate(slices)
            ]
        }
        print(
            f"Test scope: full ({args.generate_slices} slices)",
            file=sys.stderr,
        )
        _route_arm_slices(
            matrix, args.arm_hosted_slices, repo_root, args.x64_hosted_min
        )
        # Print to stdout so the CI step can capture it with $().
        print(json.dumps(matrix))
        return 0

    # Count individual tests per file
    test_counts = _approximately_count_tests(files, repo_root)
    approx_total_tests = sum(test_counts.values())

    # Apply slicing if requested — distribute files across CI jobs by
    # estimated duration so no one job gets all the slow files.
    if slice_index is not None:
        durations = _load_durations(repo_root)
        files = _slice_files(files, slice_index, slice_count, durations, repo_root)
        # Recount after slicing.
        test_counts = {f: test_counts[f] for f in files if f in test_counts}
        approx_total_tests = sum(test_counts.values())

    if roots:
        roots_str = [str(r.relative_to(repo_root)) if r.is_relative_to(repo_root) else str(r) for r in roots]
        print(
            f"Discovered {len(files)} test files (~{approx_total_tests} tests) under "
            f"{roots_str}; running with -j {args.jobs}",
            flush=True,
        )
    else:
        print(
            f"Running {len(files)} test files (~{approx_total_tests} tests) "
            f"with -j {args.jobs}",
            flush=True,
        )

    # Capture and print on completion (out-of-order is fine — keeps the
    # terminal clean rather than interleaving N parallel pytest outputs).
    failures: List[Tuple[Path, str, Dict[str, int]]] = []
    file_times: List[Tuple[Path, float]] = []  # (file, subprocess_wall) for distribution
    # Per-file summary for EVERY completed file (not just failures), so the
    # aggregate no-op gate can see exit-5→0-coerced zero-collect files that
    # never enter `failures`. Without this the silent no-op is invisible.
    all_summaries: List[Tuple[Path, Dict[str, int]]] = []
    started = time.monotonic()
    files_done = 0
    tests_done = 0
    pass_count = 0
    fail_count = 0
    tests_passed = 0
    tests_failed = 0
    tests_skipped = 0
    # Every collected outcome, not just pass/fail: a legitimately all-skipped
    # (platform-gated) file reports "2 skipped" and must NOT trip the
    # nothing-ran guard, whereas a file that died before collection reports
    # nothing at all and must.
    tests_collected = 0
    lock = threading.Lock()

    def _on_done(file: Path, started_at: float, fut: "Future[Tuple[Path, int, str, Dict[str, int], float]]") -> None:
        nonlocal files_done, tests_done, pass_count, fail_count, tests_passed, tests_failed, tests_skipped
        nonlocal tests_collected
        n_tests = test_counts.get(file, 0)
        try:
            fpath, rc, output, summary, subproc_wall = fut.result()
        except Exception as exc:  # noqa: BLE001 — must always advance counter
            with lock:
                files_done += 1
                tests_done += n_tests
                fail_count += 1
                failures.append((file, f"runner crashed: {exc!r}", {}))
                _print_progress(
                    tests_done, approx_total_tests, file, 1,
                    time.monotonic() - started_at,
                    repo_root, tests_passed, tests_failed,
                    test_counts,
                    subproc_wall=0.0,
                )
            return
        with lock:
            files_done += 1
            tests_done += n_tests
            # Accumulate test-level counts from parsed summary.
            tests_passed += summary.get("passed", 0)
            tests_failed += summary.get("failed", 0)
            tests_skipped += summary.get("skipped", 0)
            tests_collected += sum(
                summary.get(k, 0)
                for k in ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
            )
            file_times.append((fpath, subproc_wall))
            all_summaries.append((fpath, summary))
            if rc == 0:
                pass_count += 1
            else:
                fail_count += 1
                failures.append((fpath, output, summary))
            _print_progress(
                tests_done, approx_total_tests, fpath, rc,
                time.monotonic() - started_at,
                repo_root, tests_passed, tests_failed,
                test_counts,
                file_summary=summary,
                subproc_wall=subproc_wall,
            )
            if rc != 0:
                _print_inline_failure(fpath, output, repo_root, pytest_passthrough)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures: List[Future] = []
        for file in files:
            t0 = time.monotonic()
            fut = pool.submit(
                _run_one_file, file, pytest_passthrough, repo_root,
                args.file_timeout, args.file_retries,
            )
            fut.add_done_callback(lambda f, file=file, t0=t0: _on_done(file, t0, f))
            futures.append(fut)
        # Block until everything's done. ThreadPoolExecutor.__exit__ waits
        # for all submitted work, but doing it explicitly here makes the
        # control flow obvious.
        for fut in futures:
            fut.result() if fut.exception() is None else None

    elapsed = time.monotonic() - started
    print()
    pct = min(100, (tests_done / approx_total_tests * 100)) if approx_total_tests else 0
    skipped_note = f", {tests_skipped} skipped" if tests_skipped else ""
    # A file killed at the per-file wall ceiling contributes 0 passed / 0
    # failed (a SIGKILL'd pytest prints no counts line), so a run whose ONLY
    # problem is a hang used to read "0 failed" on a red job. Name it here.
    n_timed_out = sum(1 for _f, s in all_summaries if s.get("timed_out"))
    timeout_note = (
        f", {n_timed_out} timed-out file{'s' if n_timed_out != 1 else ''}"
        if n_timed_out else ""
    )
    print(f"=== Summary: {len(files)} files, {tests_passed} tests passed, {tests_failed} failed{skipped_note}{timeout_note} ({pct:.0f}% complete) in {elapsed:.1f}s ({args.jobs} workers) ===")

    # Host-OS gating note: tests marked for another OS were skipped by the
    # conftest hook, not run. Say so explicitly — a green local run on Linux
    # proves nothing about the macos_only/windows_only tests, and the reader
    # should know where they DO run rather than misreading skips as coverage.
    off_host = _off_host_marker_files(files)
    if off_host:
        print()
        for marker, n in sorted(off_host.items()):
            _, lane = _OS_MARKERS[marker]
            print(
                f"  note: {marker} tests (in {n} file{'s' if n != 1 else ''}) were "
                f"SKIPPED on this host ({sys.platform}); they run on {lane}."
            )

    # Zero tests collected across the WHOLE run is NOT a pass. Per-file rc=5
    # is deliberately tolerated above (platform-gated files), but if NOTHING
    # ran anywhere the invocation itself was broken — a venv without pytest, a
    # -k/-m filter that matched nothing, or collection erroring everywhere.
    # The summary line above reads green at a glance ("0 failed ... 100%
    # complete"), which has been misread as a successful verification, so say
    # it plainly AND fail the exit code.
    no_tests_ran_at_all = bool(files) and tests_collected == 0
    if no_tests_ran_at_all and n_timed_out:
        # A file killed at the per-file ceiling contributes no counts, so
        # "0 collected" is an artifact of the kill, not a measurement. Say
        # that instead of claiming the invocation collected nothing.
        print()
        print(
            f"=== ✗ NO TEST COUNTS — {n_timed_out} file(s) were KILLED at the "
            "per-file timeout before printing a summary. Collection counts are "
            "UNKNOWN, not zero. See the TIMED OUT verdict below. ==="
        )
    elif no_tests_ran_at_all:
        print()
        print(
            "=== ✗ NO TESTS RAN — 0 collected across "
            f"{len(files)} file{'s' if len(files) != 1 else ''}. "
            "This is NOT a pass. ==="
        )
        print(
            "  Common causes: the selected venv has no pytest; a -k/-m filter "
            "matched nothing; or collection errored in every file."
        )
        print("  Check the per-file output above for the real error.")

    # Flaky files: failed once, passed on the automatic retry. Green, but
    # loudly reported so they get fixed instead of silently re-flaking.
    if _FLAKY_RESULTS:
        print()
        print(f"=== ⚠ {len(_FLAKY_RESULTS)} FLAKY file{'s' if len(_FLAKY_RESULTS) != 1 else ''} (failed once, passed on retry — fix these) ===")
        for f, output in _FLAKY_RESULTS:
            print(f"  {_format_file(f, repo_root)}")
            print(output.rstrip())

    # Save durations for future --slice runs. Each slice writes its own
    # partial test_durations.json; a CI merge step joins them later.
    # Locally, _save_durations merges with any existing cache so entries
    # from previous runs aren't lost.
    if file_times:
        _save_durations(file_times, repo_root)
        print(f"  Durations cached to {_DURATIONS_FILE} ({len(file_times)} files)")

    # Per-file time distribution (throwaway diagnostic — shows how
    # subprocess time is distributed so we can see if startup dominates).
    if file_times:
        times = sorted([t for _, t in file_times])
        total_subproc = sum(times)
        median_t = times[len(times) // 2]
        p50 = median_t
        p90 = times[int(len(times) * 0.90)]
        p95 = times[int(len(times) * 0.95)]
        p99 = times[min(int(len(times) * 0.99), len(times) - 1)]
        max_t = times[-1]
        # How many files finish in <1s? That's roughly "just startup".
        fast = sum(1 for t in times if t < 1.0)
        fast_2s = sum(1 for t in times if t < 2.0)
        print()
        print("=== Per-file subprocess time distribution ===")
        print(f"  Files:   {len(times)}")
        print(f"  Total subprocess CPU-wall: {total_subproc:.1f}s  (runner wall: {elapsed:.1f}s, parallelism: {args.jobs}x)")
        print(f"  P50: {p50:.2f}s  P90: {p90:.2f}s  P95: {p95:.2f}s  P99: {p99:.2f}s  Max: {max_t:.2f}s")
        print(f"  <1s: {fast} files ({fast/len(times)*100:.0f}%)  <2s: {fast_2s} files ({fast_2s/len(times)*100:.0f}%)")
        # Top 10 slowest files — likely the ones dragging the run.
        slowest = sorted(file_times, key=lambda x: x[1], reverse=True)[:10]
        print("  Top 10 slowest:")
        for f, t in slowest:
            print(f"    {t:>6.2f}s  {_format_file(f, repo_root)}")

    had_failures = False
    if failures:
        print()
        print("=== Failure output ===")
        for file, output, _summary in failures:
            print()
            print(f"--- {_format_file(file, repo_root)} ---")
            print(output.rstrip())
        print()
        # Split: files with actual test failures vs non-zero exit for other reasons
        test_fail_files = [(f, s) for f, _o, s in failures if s.get("failed", 0) > 0]
        all_passed_but_nonzero = [(f, s) for f, _o, s in failures
                                  if s.get("failed", 0) == 0 and s.get("passed", 0) > 0]
        # A file KILLED at the per-file wall ceiling prints no counts line, so
        # its parsed summary is empty — which used to fall through to
        # `no_tests_ran` and get reported as "collected 0 tests (no-op)". That
        # diagnosis is false whenever collection succeeded (the captured output
        # carries `collected N items` and/or a progress marker) and it hides the
        # real event: a hang. Classify these first and never let them reach the
        # no-op bucket.
        timed_out_files = [(f, o, s) for f, o, s in failures if s.get("timed_out")]
        _timed_out_paths = {f for f, _o, _s in timed_out_files}
        no_tests_ran = [(f, s) for f, _o, s in failures
                        if f not in _timed_out_paths
                        and s.get("failed", 0) == 0 and s.get("passed", 0) == 0
                        # Ask #2: an ABSENT summary is not evidence of no
                        # collection. Require a positive signal that nothing
                        # was collected — pytest's own "no tests ran" /
                        # "collected 0 items", the exit-5 no-op tag, or a
                        # collection/import error.
                        and _looks_like_no_collection(_o, s)]
        if timed_out_files:
            print(f"=== {len(timed_out_files)} file{'s' if len(timed_out_files) != 1 else ''} TIMED OUT (killed at the per-file wall ceiling — a hang, NOT a no-op) ===")
            for file, output, s in timed_out_files:
                print(f"  {_format_file(file, repo_root)}  {_format_timeout_verdict(output, s)}")
        if test_fail_files:
            total_tf = sum(s.get("failed", 0) for _, s in test_fail_files)
            print(f"=== {len(test_fail_files)} file{'s' if len(test_fail_files) != 1 else ''} with test failures ({total_tf} test{'s' if total_tf != 1 else ''} failed) ===")
            for file, s in test_fail_files:
                nf = s.get("failed", 0)
                print(f"  {_format_file(file, repo_root)}  ({nf} test{'s' if nf != 1 else ''} failed)")
        if all_passed_but_nonzero:
            print(f"=== {len(all_passed_but_nonzero)} file{'s' if len(all_passed_but_nonzero) != 1 else ''} where all tests passed but pytest exited non-zero (warnings-as-errors, hook failures, etc.) ===")
            for file, s in all_passed_but_nonzero:
                print(f"  {_format_file(file, repo_root)}  ({s.get('passed', 0)} passed)")
        if no_tests_ran:
            print(f"=== {len(no_tests_ran)} file{'s' if len(no_tests_ran) != 1 else ''} where no tests ran (collection/import error, timeout before collection, etc.) ===")
            for file, s in no_tests_ran:
                print(f"  {_format_file(file, repo_root)}")
        # Residual: a non-zero exit with no counts line and no positive
        # evidence of zero-collection. Previously these were swept into
        # `no_tests_ran` and mislabelled; report them honestly as unknown
        # rather than asserting a cause we cannot see.
        _classified = (
            {f for f, _s in test_fail_files}
            | {f for f, _s in all_passed_but_nonzero}
            | _timed_out_paths
            | {f for f, _s in no_tests_ran}
        )
        unclassified = [f for f, _o, _s in failures if f not in _classified]
        if unclassified:
            print(f"=== {len(unclassified)} file{'s' if len(unclassified) != 1 else ''} exited non-zero with no pytest summary (cause not determinable from output — read the captured output above) ===")
            for file in unclassified:
                print(f"  {_format_file(file, repo_root)}")
        had_failures = True

    # ── No-op guard: count EXECUTED tests, don't trust exit codes ────────────
    # This is the single aggregate-verdict site (test-gate-honesty §5). It
    # catches the silent no-op: a suite that collected 0 tests reads "green"
    # only because nothing ran. Every no-op cause (missing dep → importorskip
    # storm, marker-emptied file, fixture-nuked collection, exit-5→0 coercion)
    # funnels through "0 executed" here.
    total_executed = tests_passed + tests_failed
    noop_red = _noop_guard(
        all_summaries=all_summaries,
        total_executed=total_executed,
        explicit_files=explicit_files,
        strict_noop=args.strict_noop,
        min_tests=args.min_tests,
        repo_root=repo_root,
    )

    if had_failures or noop_red:
        return 1

    if no_tests_ran_at_all:
        return 1

    return 0


def _noop_guard(
    *,
    all_summaries: List[Tuple[Path, Dict[str, int]]],
    total_executed: int,
    explicit_files: set[Path],
    strict_noop: bool,
    min_tests: int | None,
    repo_root: Path,
) -> bool:
    """Fail LOUD when a suite collected/executed zero tests.

    Returns True if the run should be RED for a no-op reason. Prints loud
    banners for each trip. The gates, in order of severity:

      1. Whole-suite-zero (hard RED, always): total executed == 0 across the
         entire invoked set is never legitimate — the Ace-named "green because
         it didn't run" failure.
      2. Explicitly-requested 0-collect (RED under --strict-noop): a file the
         caller named specifically (--files / positional .py) that executed 0
         tests is a no-op the caller did not intend. Scoped to explicit
         requests so a default-discovery marker-filtered file stays GREEN.
      3. --min-tests N floor (opt-in): fewer than N executed → RED.

    Skip-storms (skipped>0, passed==0, failed==0) are a loud ⚠ *surfacing*,
    not a gate — a dep-missing skip-storm stays visible even where the dep is
    genuinely optional, without false-reding an optional-dep environment.
    """
    def _executed(s: Dict[str, int]) -> int:
        return s.get("passed", 0) + s.get("failed", 0)

    red = False

    # ── ⚠ skip-storm surfacing (unconditional, never a gate) ──────────────
    skip_storms = [
        (f, s) for f, s in all_summaries
        if (s.get("skipped", 0) > 0 and s.get("passed", 0) == 0 and s.get("failed", 0) == 0)
        or s.get("noop_skip")  # exit-5 from a module-level importorskip/skip
    ]
    if skip_storms:
        print()
        print(f"⚠  {len(skip_storms)} file{'s' if len(skip_storms) != 1 else ''} skipped every test and executed none "
              f"(importorskip fired? missing optional dep?) — surfaced, not failed:")
        for f, s in skip_storms:
            _n = s.get("skipped", 0)
            print(f"  ⚠  {_format_file(f, repo_root)}  ({_n} skipped, 0 run)")

    # ── ⚠ intentionally-testless files (tombstones / __main__ scripts) ────
    testless = [(f, s) for f, s in all_summaries if s.get("noop_testless")]
    if testless:
        print()
        print(f"⚠  {len(testless)} file{'s' if len(testless) != 1 else ''} in the set define no pytest tests "
              f"(empty tombstone or __main__ script) — surfaced, not failed:")
        for f, s in testless:
            print(f"  ⚠  {_format_file(f, repo_root)}  (no def test / class Test)")

    # ── Gate 1: whole-suite-zero (hard RED, always) ───────────────────────
    if total_executed == 0:
        print()
        any_timed_out = any(s.get("timed_out") for _f, s in all_summaries)
        if any_timed_out:
            # Still RED (the caller already reds on the non-zero exit), but do
            # not assert "no-op": a killed pytest executed an unknown number of
            # tests and simply never printed its counts line.
            print("=== 0 tests counted, but at least one file was KILLED at the per-file "
                  "timeout — counts are unknown, NOT zero. See the TIMED OUT verdict above. ===")
        else:
            print("=== NO TESTS EXECUTED — suite is a no-op (missing dep? bad selector? all filtered?) ===")
            print("    A run that executed 0 tests is RED, not green (test-gate-honesty §5).")
        red = True

    # ── Gate 2: explicitly-requested paths that collected 0 tests ─────────
    if explicit_files:
        # Aggregate per resolved path first: a file can appear in all_summaries
        # more than once (e.g. a skip-reported entry AND a separate exit-5→0 entry
        # under parallel/retry). It is a legit skip if ANY entry indicates a skip
        # — so fold the skip signals across all of a file's entries before gating,
        # else a duplicate no-skip entry false-REDs a file already surfaced as ⚠.
        by_path: dict = {}
        for f, s in all_summaries:
            rp = f.resolve()
            agg = by_path.setdefault(rp, {"f": f, "executed": 0, "skipped": 0,
                                          "noop_skip": False, "noop_testless": False,
                                          "noop_exit5": False, "timed_out": False})
            agg["executed"] += _executed(s)
            agg["skipped"] += s.get("skipped", 0)
            agg["noop_skip"] = agg["noop_skip"] or bool(s.get("noop_skip"))
            agg["noop_testless"] = agg["noop_testless"] or bool(s.get("noop_testless"))
            agg["noop_exit5"] = agg["noop_exit5"] or bool(s.get("noop_exit5"))
            agg["timed_out"] = agg["timed_out"] or bool(s.get("timed_out"))
        noop_explicit = [
            (agg["f"], agg) for rp, agg in by_path.items()
            # An explicit file that executed 0 tests is a caller-intent no-op —
            # EXCEPT it legitimately SKIPPED rather than silently no-op'd:
            #   (a) noop_skip: a module-level importorskip opted out (missing dep);
            #   (b) noop_testless: an empty tombstone / __main__ script with no tests;
            #   (c) skipped>0: every test skipped (module skipif / empty-parametrize /
            #       per-test pytest.skip()) — pytest ran the file, the tests opted out.
            #   (d) timed_out: killed at the per-file wall ceiling. It executed an
            #       unknown number of tests and printed no counts line; that is a
            #       HANG, reported by its own verdict above, never a no-op.
            # None is a broken request; all surface as ⚠ above, never a RED gate. A
            # file that DOES define tests, skipped none, and collected zero still REDs
            # (the real silent no-op the guard exists to catch).
            if rp in explicit_files and agg["executed"] == 0
            and not agg["noop_skip"] and not agg["noop_testless"]
            and not agg["timed_out"]
            and agg["skipped"] == 0
        ]
        if noop_explicit:
            print()
            verb = "RED" if strict_noop else "⚠ (--no-strict-noop: surfaced only)"
            print(f"=== {len(noop_explicit)} explicitly-requested file(s) collected 0 tests (no-op) — {verb} ===")
            for f, s in noop_explicit:
                tag = " [exit-5 no-op]" if s.get("noop_exit5") else ""
                print(f"    {_format_file(f, repo_root)}{tag}")
            if strict_noop:
                red = True

    # ── Gate 3: --min-tests floor (opt-in) ────────────────────────────────
    if min_tests is not None and total_executed < min_tests:
        print()
        print(f"=== EXECUTED {total_executed} tests, below --min-tests floor of {min_tests} — RED ===")
        red = True

    return red


if __name__ == "__main__":
    sys.exit(main())
