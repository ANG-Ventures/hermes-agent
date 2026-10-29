"""Cron lanes: which ticker runs which job, and the shared facts both tickers read.

Spec: Obsidian ``AI/Hermes Agent/Cron Script Runner Out Of Gateway — Spec (2026-10-27).md`` v1.0
(S1: §5.1, §5.2, §5.2a; ``host_gateways`` / ``code_contains_s1`` from §5.4). Two lanes partition the
store: ``script`` (``no_agent is True``) and ``agent`` (everything else). ``job_lane`` is the ONE
place lane membership is decided (invariant I1); ``tests/cron/test_cron_lanes.py`` fails on a
``no_agent`` read in lane-scoped code anywhere else.

Constants have one encoding here and several readers (the gateway's delegation predicate, the
reap exemption, the runner watchdog, ``hermes cron status``).

Nothing in this module imports ``cron.jobs`` at import time: ``cron.jobs`` imports it.
"""

from __future__ import annotations

import collections
import enum
import json
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, NamedTuple, Optional, Tuple

LANES = ("all", "agent", "script")

RUNNER_CYCLE_S = 60
# Q1-dependent: the value under option A (spec §10). Option B changes this one line.
RUNNER_PROGRESS_STALE_S = 180
# D15: max(30, ceil(4 x T_max)). P0-5 (t_0d495b7a, AC-13 replay of the 09:00 busiest minute, 172
# script jobs, 10 repeats x 3 cycles, two processes): n=30, p50 0.58 s, p99 = T_max 1.78 s ->
# ceil(7.12) = 8 -> the 30 s floor. Delivery runs in pool workers after tick() returns (sync=False),
# except missed one-shot notices (scheduler_tick.py), which the replay does not exercise.
RUNNER_TICK_WATCHDOG_S = 30
# Watchdog tree-kill grace (§5.5 step 5). ``terminate_running_scripts`` has no named grace, so this
# is the spec's own value.
RUNNER_WATCHDOG_KILL_GRACE_S = 3
# hermes-agent#2396 (S1) merge commit: a gateway whose boot code_sha descends from it takes both
# lane locks, so the runner may tick (host precondition, §5.4).
S1_MERGE_SHA: Optional[str] = "410f6d65af5af9045ee1c0d24c56090cf715d4ff"

TICK_LOCK_NAMES = {"agent": ".tick.lock", "script": ".tick-script.lock"}
HEARTBEAT_NAME = "script_runner_heartbeat"
REWOUND_LANES_NAME = "registry_rewound_lanes.json"
_GIT_TIMEOUT_S = 5


def job_lane(job: Dict[str, Any]) -> str:
    """``"script"`` for a plain-script job (``no_agent`` is literally ``True``), else ``"agent"``."""
    return "script" if job.get("no_agent") is True else "agent"


def tick_lock_name(lane: str) -> str:
    """Per-home tick lock file name for one lane (``"agent"`` | ``"script"``)."""
    return TICK_LOCK_NAMES[lane]


class TickOutcome(enum.Enum):
    """How one ``tick()`` call ended. ``ADMITTED``: the due scan completed; the rest name the six
    return-0 paths before it."""

    ADMITTED = "ADMITTED"
    REFUSED_RETIREMENT = "REFUSED_RETIREMENT"
    LOCK_BUSY = "LOCK_BUSY"
    ESTOP = "ESTOP"
    DISPATCH_PAUSED = "DISPATCH_PAUSED"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    ADMISSION_HOLD = "ADMISSION_HOLD"


# --- knob ---------------------------------------------------------------------------------------

_knob_cache: Dict[str, Tuple[Tuple[int, int], bool]] = {}
_knob_lock = threading.Lock()


def knob_enabled(home=None) -> bool:
    """``cron.script_runner.enabled`` from the PROCESS home's ``config.yaml`` (never the per-tick
    profile scope), or from *home*'s when given, True only for a YAML boolean ``true``. Missing
    file, missing key, a string, a number or a parse failure all read False. Cached on the file's
    (mtime, size)."""
    try:
        from hermes_constants import get_process_hermes_home

        path = (Path(home) if home is not None else get_process_hermes_home()) / "config.yaml"
        st = path.stat()
    except (OSError, ValueError):
        return False
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _knob_lock:
        hit = _knob_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        from hermes_cli.config_effective import load_user_config_effective

        cfg = load_user_config_effective(path)
        runner = (cfg.get("cron") or {}).get("script_runner") or {}
        value = isinstance(runner, dict) and runner.get("enabled") is True
    except Exception:
        value = False
    with _knob_lock:
        _knob_cache[key] = (sig, bool(value))
    return bool(value)


# --- _jobs_lock wait probe (§5.2) ----------------------------------------------------------------

# Per-process ring of (section_label, seconds): each outermost ``_jobs_lock`` acquisition records
# how long it waited, and each ``tick()`` call records ``("tick:<lane>", duration)``. AC-13 and
# P0-5 read it through ``lock_waits()``.
_LOCK_WAITS: Deque[Tuple[str, float]] = collections.deque(maxlen=10_000)


def record_lock_wait(label: str, seconds: float) -> None:
    _LOCK_WAITS.append((str(label), float(seconds)))


def lock_waits() -> List[Tuple[str, float]]:
    return list(_LOCK_WAITS)


def reset_lock_waits() -> None:
    _LOCK_WAITS.clear()


def caller_section_label(frame) -> str:
    """Name of the function that entered a ``with _jobs_lock():`` block, skipping contextlib."""
    try:
        import contextlib as _contextlib

        skip = {_contextlib.__file__}
        while frame is not None and frame.f_code.co_filename in skip:
            frame = frame.f_back
        return frame.f_code.co_name if frame is not None else "unknown"
    except Exception:
        return "unknown"


# --- rewound-burst marker across lanes (§5.1) ---------------------------------------------------

def merge_rewound_lanes(cron_dir: Path, lane: str, ids: List[str], now: datetime) -> List[str]:
    """Record this *lane* scan's rewound *ids* in ``<cron>/registry_rewound_lanes.json`` as
    ``{lane: {"tick_at", "ids"}}`` and return the union over every lane entry stamped within
    ``RUNNER_CYCLE_S`` of *now* (this scan's included). The caller holds ``_jobs_lock``. Best
    effort: a read or write failure counts only this scan's ids."""
    from utils import atomic_write_text

    path = Path(cron_dir) / REWOUND_LANES_NAME
    ids = [str(i) for i in ids]
    try:
        stored = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    old = stored.get(lane)
    if ids or (isinstance(old, dict) and old.get("ids")):
        stored[lane] = {"tick_at": now.isoformat(), "ids": ids}
        try:
            atomic_write_text(path, json.dumps(stored, sort_keys=True) + "\n",
                              tmp_prefix=".rewound_lanes_", mode=0o600)
        except Exception:
            pass
    window = timedelta(seconds=RUNNER_CYCLE_S)
    union: List[str] = list(ids)
    seen = set(ids)
    for other in sorted(stored):
        if other == lane:
            continue
        entry = stored[other]
        try:
            fresh = abs(now - datetime.fromisoformat(entry["tick_at"])) <= window
            other_ids = [str(i) for i in entry.get("ids") or []]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if fresh:
            for i in other_ids:
                if i not in seen:
                    seen.add(i)
                    union.append(i)
    return union


# --- runner heartbeat I/O -----------------------------------------------------------------------

def _heartbeat_path(home) -> Path:
    return Path(home) / "cron" / HEARTBEAT_NAME


def read_runner_heartbeat(home) -> Optional[Dict[str, Any]]:
    """The runner heartbeat for *home*, or None when absent or unreadable."""
    try:
        data = json.loads(_heartbeat_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_runner_heartbeat(home, payload: Dict[str, Any]) -> bool:
    """Atomically write *payload* as *home*'s runner heartbeat. Never creates directories (a
    deleted profile's home must not come back, #47368). True when written."""
    from utils import atomic_write_text

    path = _heartbeat_path(home)
    if not path.parent.is_dir():
        return False
    try:
        atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n",
                          tmp_prefix=".script_runner_heartbeat_", mode=0o600)
        return True
    except Exception:
        return False


# --- liveness -----------------------------------------------------------------------------------

def runner_pid_alive(pid, pid_start) -> bool:
    """True only when *pid* exists, its start time is readable and it matches *pid_start*
    (``start_time_fingerprints_match``). Any failure reads False."""
    try:
        import gateway.status as status

        pid = int(pid)
        if not status._pid_exists(pid):
            return False
        observed = status.get_process_start_time(pid)
        if observed is None:
            return False
        return bool(status.start_time_fingerprints_match(pid_start, observed))
    except Exception:
        return False


def _now():
    from hermes_time import now as _hermes_now

    return _hermes_now()


def _age_s(stamp, now) -> float:
    return (now - datetime.fromisoformat(str(stamp))).total_seconds()


def lane_peer(pid, pid_start, home) -> bool:
    """True when the execution owner ``(pid, pid_start)`` is the script runner of *home* and that
    runner is progressing: active with ``last_ok_at`` within ``RUNNER_PROGRESS_STALE_S``, or draining
    for no longer than one script timeout plus one standalone send. Such a row is exempt from the
    cross-process wedged-claim reap (§5.2a). Any read failure returns False (today's bound)."""
    try:
        import gateway.status as status

        hb = read_runner_heartbeat(home)
        if hb is None or int(hb["pid"]) != int(pid):
            return False
        if not status.start_time_fingerprints_match(hb["pid_start"], pid_start):
            return False
        now = _now()
        state = hb.get("state")
        if state == "active":
            return _age_s(hb["last_ok_at"], now) <= RUNNER_PROGRESS_STALE_S
        if state == "draining":
            from cron.scheduler_delivery import _get_standalone_send_timeout
            from cron.scheduler_script import _get_script_timeout

            bound = float(_get_script_timeout()) + float(_get_standalone_send_timeout())
            return _age_s(hb["draining_since"], now) <= bound
        return False
    except Exception:
        return False


# --- gateway side: who owns a home's script lane (§5.3, S3) --------------------------------------

def _progress_age_s(hb: Dict[str, Any], now) -> Optional[float]:
    try:
        return _age_s(hb["last_ok_at"], now)
    except (KeyError, TypeError, ValueError):
        return None


def delegate(home) -> bool:
    """True when this gateway hands *home*'s script lane to the runner: the knob is on, the
    runner heartbeat is ``active``, ``last_ok_at`` (progress, not liveness) is within
    ``RUNNER_PROGRESS_STALE_S`` and the heartbeat pid is alive with a matching start time. Fails
    toward False (the gateway ticks ``"all"``). The knob is the HOST root's, where the runner
    reads it: a profile gateway ticking the root home while root's gateway is down must hand the
    lane to the same runner (t_0faaf89b)."""
    try:
        from hermes_constants import get_default_hermes_root

        if not knob_enabled(get_default_hermes_root()):
            return False
        return runner_progressing(home)
    except Exception:
        return False


def runner_progressing(home) -> bool:
    """The heartbeat half of ``delegate``: ``active``, ``last_ok_at`` within
    ``RUNNER_PROGRESS_STALE_S``, pid alive with a matching start time. Fails toward False."""
    try:
        hb = read_runner_heartbeat(home)
        if hb is None or hb.get("state") != "active":
            return False
        age = _progress_age_s(hb, _now())
        if age is None or age > RUNNER_PROGRESS_STALE_S:
            return False
        return runner_pid_alive(hb.get("pid"), hb.get("pid_start"))
    except Exception:
        return False


def home_label(home) -> str:
    """``<name>`` for ``<root>/profiles/<name>``, else ``"default"`` (keys of the published
    ``cron_script_lane_last_outcome`` map)."""
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


# Last script-lane outcome per home this gateway process ticked, published into its
# ``gateway_state.json`` by the housekeeping heartbeat: ``{home_label: {"outcome", "at"}}``.
_gateway_outcomes: Dict[str, Dict[str, str]] = {}
_gateway_outcomes_lock = threading.Lock()
DELEGATED = "DELEGATED"


def gateway_script_outcome(outcome, scanned_lane: Optional[str], *, delegated: bool = False) -> str:
    """What happened to the script lane on one gateway tick. ``DELEGATED``: the gateway ticked
    ``"agent"`` on purpose. ``LOCK_BUSY``: an ``"all"`` tick was narrowed to the agent lane because
    ``.tick-script.lock`` was held, or the whole tick was lock-skipped. Otherwise the reported
    ``TickOutcome`` name, or the raised exception's type name."""
    if delegated:
        return DELEGATED
    if scanned_lane == "agent":
        return TickOutcome.LOCK_BUSY.value
    if isinstance(outcome, TickOutcome):
        return outcome.value
    if isinstance(outcome, BaseException):
        return type(outcome).__name__
    return "UNKNOWN"


def record_gateway_script_outcome(home, outcome: str) -> None:
    with _gateway_outcomes_lock:
        _gateway_outcomes[home_label(home)] = {"outcome": str(outcome), "at": _now().isoformat()}


def gateway_lane_status_fields() -> Dict[str, Any]:
    """The two ``gateway_state.json`` fields each housekeeping cycle publishes:
    ``cron_script_lane_delegation = int(knob_enabled())`` (this process home's knob) and
    ``cron_script_lane_last_outcome``."""
    with _gateway_outcomes_lock:
        outcomes = {k: dict(v) for k, v in _gateway_outcomes.items()}
    return {"cron_script_lane_delegation": int(knob_enabled()),
            "cron_script_lane_last_outcome": outcomes}


class GatewayEntry(NamedTuple):
    home: Path
    pid: int
    start_time: Any
    code_sha: Optional[str]
    record_stale: bool


def host_gateways(root=None) -> List[GatewayEntry]:
    """Every live gateway on the host: ``<root>/gateway_state.json`` plus
    ``<root>/profiles/*/gateway_state.json``. Live = the pid exists and the record is not
    dead-by-identity, where dead-by-identity needs a readable start time that does NOT match the
    record AND a stale record (a clock step breaks the match but a live gateway keeps refreshing,
    so it stays live). Fails toward live: an unreadable start time or a comparator error is live.
    A record without a pid names no gateway and is skipped."""
    import gateway.status as status

    if root is None:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    root = Path(root)
    paths = [root / "gateway_state.json"] + sorted((root / "profiles").glob("*/gateway_state.json"))
    out: List[GatewayEntry] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            pid = int(record["pid"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        try:
            if not status._pid_exists(pid):
                continue
        except Exception:
            pass  # cannot prove the pid is gone: live
        stale = status.runtime_status_is_stale(record)
        try:
            observed = status.get_process_start_time(pid)
            matches = observed is None or status.start_time_fingerprints_match(
                record.get("start_time"), observed)
        except Exception:
            matches = True
        if not matches and stale:
            continue
        sha = record.get("code_sha")
        out.append(GatewayEntry(path.parent, pid, record.get("start_time"),
                                sha if isinstance(sha, str) else None, bool(stale)))
    return out


# --- code ancestry ------------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ancestry_memo: Dict[Tuple[str, str, str], bool] = {}
_UNSET = object()


def _runtime_tree() -> Path:
    return Path(__file__).resolve().parents[1]


def code_contains_s1(code_sha, *, tree=None, s1_sha: Any = _UNSET) -> bool:
    """True iff *code_sha* is 40-hex and ``git merge-base --is-ancestor S1_MERGE_SHA code_sha``
    exits 0 in the runtime tree. Missing sha, unknown commit, git failure or a 5 s timeout read
    False. Definitive answers (exit 0 or 1) are memoised per sha for the life of the process."""
    if s1_sha is _UNSET:
        s1_sha = S1_MERGE_SHA
    if not isinstance(code_sha, str) or not _SHA_RE.match(code_sha) or not s1_sha:
        return False
    tree = Path(tree) if tree is not None else _runtime_tree()
    key = (str(tree), str(s1_sha), code_sha)
    if key in _ancestry_memo:
        return _ancestry_memo[key]
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree), "merge-base", "--is-ancestor", str(s1_sha), code_sha],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode in (0, 1):
        _ancestry_memo[key] = proc.returncode == 0
        return proc.returncode == 0
    return False
