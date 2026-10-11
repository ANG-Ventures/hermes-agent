"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` (pins the
file path) > ``<root>/kanban/current`` > ``default``; the dispatcher injects these into workers.
Concurrency: WAL + ``BEGIN IMMEDIATE`` + compare-and-swap on ``tasks.status``/``claim_lock`` —
SQLite serializes writers so one claimer wins, losers see zero rows (no retries, no distributed
locks). Schema: tasks, task_links, task_comments, task_events, task_runs, attachments, notify subs.
"""
from __future__ import annotations
import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import os
import re
import random
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import logging
import time
import psutil
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union
from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing
from hermes_cli import kanban_worker_hosts as _kwh
from toolsets import get_toolset_names
from utils import env_var_enabled
import unicodedata  # noqa: E402
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as _REVIEW_LENSES  # noqa: E402
from hermes_cli.kanban_review_schema import HEAD_SHA_PATTERN as _HEAD_SHA_PATTERN  # noqa: E402
from hermes_cli.kanban_review_schema import validate_v2_fields as _validate_review_v2  # noqa: E402
_log = logging.getLogger(__name__)


# --- Shared micro-helpers (row access, JSON, env, git) ---
def _lossy_text(value: Any) -> Any:
    """``bytes`` -> ``str`` with U+FFFD for undecodable sequences; anything else passes through.

    Installed as every board connection's ``text_factory`` (a TEXT cell holding
    invalid UTF-8 otherwise aborts the whole ``fetchall`` with "Could not decode
    to UTF-8") and applied to BLOB-typed cells in the ``from_row`` constructors
    (task, comment, event, run), so one
    corrupt row degrades to replacement characters instead of taking the board
    listing down."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row_get(row: Any, col: str, default: Any = None) -> Any:
    """``row[col]`` tolerant of the column being absent from the SELECT / schema."""
    if row is None or col not in row.keys():
        return default
    return row[col]


def _json_or(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column; any decode failure or empty value yields ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    """Decode a JSON text column that must be an object; anything else yields ``{}``."""
    parsed = _json_or(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer env override: absent/empty/non-integer/below ``minimum`` falls back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return default
        if parsed >= minimum:
            return parsed
    return default


def _git_out(cwd: Path, *args: str, timeout: int = 30) -> Optional[str]:
    """Run ``git -C cwd args`` and return stripped stdout, or ``None`` on any failure / empty output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons. Distinguishes the two fundamentally different things a
# worker (or human) means by "blocked", so each can be routed differently
# instead of all landing in one undifferentiated ``blocked`` bucket that a cron
# unblocks → worker re-blocks → cron unblocks … forever.
#
#   * ``dependency``   — can't proceed until another task finishes. Routed to
#                        ``todo`` (NOT ``blocked``) so the existing
#                        parent-gating / ``recompute_ready`` machinery promotes
#                        it automatically once parents are done. No human, no
#                        cron, no retry storm.
#   * ``needs_input``  — needs a human decision/answer it cannot derive.
#   * ``capability``   — hit a hard wall (no access, missing creds, an action no
#                        AI agent can perform). Genuinely human-only.
#   * ``transient``    — a flaky/temporary failure that may clear on retry.
#   * ``deferred``     — waiting on wall-clock time (``until``). Parks in
#                        ``scheduled`` with a timed wake; the dispatcher's
#                        ``wake_due_scheduled`` returns it to ``ready`` at that
#                        time. Not a human question, so it never pages.
#
# ``needs_input`` and ``capability`` are "truly blocked": they go to ``blocked``
# for a human, and the unblock-loop breaker (see ``block_task`` /
# ``BLOCK_RECURRENCE_LIMIT``) escalates them to ``triage`` if a cron keeps
# unblocking them only to have the worker re-block for the same reason.
# ``None`` = legacy/un-typed block (treated as a generic human blocker).
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient", "deferred"}

# A ``dependency`` block with no open parent has nothing to wait on. It used to
# be re-kinded to ``needs_input`` silently, which paged the origin channel every
# 2 h for what was usually a time wait (closeout 10-09 lesson 5). It is refused
# instead, naming the two kinds that mean what the caller wanted.
DEPENDENCY_NO_PARENT_REFUSAL = (
    "kind 'dependency' waits on an open parent task, and this card has none. "
    "Pick one: --kind deferred --until <ISO|+6h> for a time wait (parks in "
    "scheduled, wakes to ready at that time, pages nobody), or --kind "
    "needs_input for a human ruling (stays blocked and pages the origin channel)."
)


class BlockRefused(ValueError):
    """``block_task`` refused the requested kind; the message says what to use instead."""


def parse_wake_at(value: str, *, now: Optional[int] = None) -> int:
    """Wake time -> epoch seconds: epoch digits, ``+<N>[smhd]`` relative to
    *now*, or ISO-8601 (a naive timestamp is local time). Raises ValueError."""
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    rel = re.fullmatch(r"\+(\d+)([smhd])", value)
    if rel:
        base = int(time.time()) if now is None else int(now)
        return base + int(rel.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[rel.group(2)]
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def format_deferred_until(until: Optional[int]) -> str:
    """``⏸ deferred until <local time>``: the one rendering of a deferred park."""
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(int(until))) if until else "?"
    return f"⏸ deferred until {when}"


# Run outcomes that mean "this attempt ENDED the card successfully".
# ``superseded`` is the honest close for a card whose premise was already
# satisfied elsewhere (evidence = the ``superseded_by`` pointer, no artifact).
# Every reader that asks "did this task succeed" must treat it like
# ``completed``, or a superseded card reads as never-run and gets respawned.
# ``external`` (t_768c9e91) is the terminal close for a card whose remaining
# step belongs to someone outside the fleet (an upstream maintainer merge):
# evidence = the upstream URL + the watcher that reopens it, no artifact.
SUCCESS_RUN_OUTCOMES = ("completed", "superseded", "external")
_SUCCESS_RUN_OUTCOMES_SQL = "('completed', 'superseded', 'external')"

# After a task has been blocked, unblocked, and re-blocked this many times for
# the same (truly-blocked) reason, the unblock-loop breaker stops trusting the
# unblocker (usually a cron) and routes the task to ``triage`` instead of back
# to ``blocked`` — breaking the infinite unblock↔re-block loop and forcing a
# human-in-the-loop decision. Mirrors the dispatcher's ``DEFAULT_FAILURE_LIMIT``
# spirit (default 2) but counts a different signal: manual unblock recurrences,
# not dispatcher spawn/crash/timeout failures.
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}

# Link kinds on ``task_links``. A parent->child edge means one of two very
# different things, and conflating them strands shipped work:
#
#   * ``blocks``       — a real dependency. The child cannot start until the
#                        parent is done. This is the historical (and default)
#                        meaning of every edge, so nothing silently un-gates.
#   * ``derived-from`` — provenance only. The parent DISCOVERED or SPAWNED the
#                        child (an audit, survey or investigation that filed
#                        remediation work). The child is independently
#                        dispatchable the moment it is defined and is NEVER
#                        gated on the parent.
#
# The motivating incident (2026-08-08): a DEPLOY card was created as a child of
# an "audit all 37 protocol fields" card to record where it came from. The audit
# went to triage, so the deploy could never promote — a merged, green fix sat
# undeployed because a research task had not finished. A DEPLOY must never be
# gated on a DISCOVERY; the link was right, the edge type was wrong.
LINK_KIND_BLOCKS = "blocks"
LINK_KIND_DERIVED_FROM = "derived-from"
VALID_LINK_KINDS = {LINK_KIND_BLOCKS, LINK_KIND_DERIVED_FROM}
DEFAULT_LINK_KIND = LINK_KIND_BLOCKS


def normalize_link_kind(kind: Optional[str]) -> str:
    """Normalize a link kind, defaulting NULL/blank to ``blocks``.

    Legacy rows predate the column and read back as NULL; they were created
    under the gating semantics, so NULL must mean ``blocks``. Raises
    ``ValueError`` on an unknown kind rather than silently degrading to a
    non-gating edge.
    """
    if kind is None:
        return DEFAULT_LINK_KIND
    normalized = str(kind).strip().lower()
    if not normalized:
        return DEFAULT_LINK_KIND
    # Accept the underscore spelling as an alias; the canonical stored form
    # is the hyphenated one used by the CLI flag.
    if normalized == "derived_from":
        normalized = LINK_KIND_DERIVED_FROM
    if normalized not in VALID_LINK_KINDS:
        raise ValueError(
            f"unknown link kind {kind!r}; expected one of "
            f"{', '.join(sorted(VALID_LINK_KINDS))}"
        )
    return normalized


def normalize_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """``VALID_REASONING_EFFORTS`` or ``"none"`` (thinking off), case-insensitive;
    empty/None = inherit the profile's own effort (NULL). Anything else raises —
    a typo'd level must not quietly hand the task back to the profile default."""
    from hermes_constants import VALID_REASONING_EFFORTS

    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "none" or value in VALID_REASONING_EFFORTS:
        return value
    allowed = ", ".join(("none", *VALID_REASONING_EFFORTS))
    raise ValueError(f"reasoning_effort must be one of {allowed}, got {effort!r}")


# Per-card harness brain (t_a8f335c5): the Claude lane a foreign-lane worker
# (cc-worker) dials through, normally the profile-wide ``foreign_lane.brain``.
# Stored in the full (``f``) lane grammar; the lane runner
# (skills-shared/coding/kanban-foreign-lane/scripts/harness_model.py) maps it to
# its internal id and re-validates it at spawn. Pre-v2 internal ids are
# accepted and stored in the f spelling. Bare ``clr`` / ``clx-N`` are the SLIM
# harness since 2026-10-01 (t_faf5af7b) and are refused by name.
CARD_BRAIN_ALLOWED = (
    "clrf | clxf-<N> | dtlrf | dtlxf-<N> | alrf | cliproxy:<model> | openrouter:<model>"
)
_CARD_BRAIN_ALIASES = {"cpr-cli": "clrf", "dtlr": "dtlrf", "alrf": "alrf", "clrf": "clrf", "dtlrf": "dtlrf"}
_CARD_BRAIN_MODEL_RE = re.compile(r"(cliproxy|openrouter):([A-Za-z0-9][A-Za-z0-9._/-]*)")


def normalize_card_brain(brain: Optional[str]) -> Optional[str]:
    """Validate a per-card brain against the allowlist; return its stored form.

    Empty / None / ``none`` / ``-`` means "no card brain" (NULL: the profile's
    ``foreign_lane.brain`` applies). Anything outside the allowlist raises
    ``ValueError`` naming the allowed values, so a typo never silently falls
    back to the profile's lane.
    """
    value = str(brain or "").strip()
    if value.lower() in ("", "none", "-", "null"):
        return None
    low = value.lower()
    if low in _CARD_BRAIN_ALIASES:
        return _CARD_BRAIN_ALIASES[low]
    m = re.fullmatch(r"(clxf|cpx-cli|dtlxf|dtlx)[-:](\d+)", low)
    if m:
        family = "clxf" if m.group(1) in ("clxf", "cpx-cli") else "dtlxf"
        return f"{family}-{int(m.group(2))}"
    m = _CARD_BRAIN_MODEL_RE.fullmatch(value)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if low in ("clr", "clx") or re.fullmatch(r"clx[-:]\d+", low):
        full = low.replace("clr", "clrf").replace("clx", "clxf")
        raise ValueError(
            f"brain {value!r} is the SLIM harness since 2026-10-01 (t_faf5af7b); "
            f"a coding worker needs the full harness: use {full}"
        )
    raise ValueError(f"brain must be one of {CARD_BRAIN_ALLOWED}, got {value!r}")


KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # one cap for dashboard, tools and CLI


def _is_delegated_child() -> bool:
    """Whether this code runs inside a ``delegate_task`` child context."""
    try:
        from agent.delegation_context import is_delegated_child_process_context

        return is_delegated_child_process_context()
    except Exception:
        return bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))

# Set only by :func:`add_comment` for the duration of its own write. The single
# append-only write a delegated child may perform (t_70fcc2c3): comments carry
# no status/ownership/claim state, and add_comment stamps the author with
# :data:`SUBAGENT_AUTHOR_MARKER` so the thread shows who actually wrote it.
_DELEGATED_CHILD_COMMENT_GRANT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kanban_delegated_child_comment_grant", default=False
)
SUBAGENT_AUTHOR_MARKER = " (subagent)"


def _assert_not_delegated_child_mutation(path: "str | Path | None" = None) -> None:
    """Reject Kanban mutations from ``delegate_task`` child contexts.

    The tool/CLI fast-fail guards are UX, not a trust boundary (a child can shell
    out or import this module); the invariant lives here so every ``write_txn``
    user and board-metadata mutator fails closed before touching durable state.
    *path* is the board DB / metadata root being mutated; ``None`` means the
    lineage's own board (``kanban_home()``).

    Reads are allowed: a child's :func:`connect` opens the existing DB with
    ``PRAGMA query_only=ON`` and skips schema/migration writes. The only write
    exception is :func:`add_comment` (append-only, author marked as subagent).
    """
    if _DELEGATED_CHILD_COMMENT_GRANT.get():
        return
    from agent.delegation_context import kanban_path_is_fenced

    if kanban_path_is_fenced(kanban_home() if path is None else path):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")


def _connect_delegated_child(path: Path) -> sqlite3.Connection:
    """Open an EXISTING board DB for a ``delegate_task`` child, read-only.

    No mkdir, no permission repair, no schema script, no migrations — those are
    all writes. The connection runs with ``PRAGMA query_only=ON`` so even a
    direct ``conn.execute("UPDATE ...")`` that bypasses :func:`write_txn` is
    refused by SQLite itself. :func:`add_comment` lifts it for its own insert.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise PermissionError(
            "delegate_task child contexts cannot initialize a Kanban board "
            f"(no board DB at {path})"
        )
    conn = _sqlite_connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.text_factory = _lossy_text
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA cell_size_check=ON")
        conn.execute("PRAGMA query_only=ON")
        if not _schema_is_present(conn):
            raise PermissionError(
                "delegate_task child contexts cannot initialize a Kanban board "
                f"(no schema in {path})"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Best-effort lifecycle hook. Call AFTER the write txn commits (plugins never
    run under the SQLite write lock, always see durable state); failures are
    swallowed so an observer can never break a transition."""
    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


def _fire_task_hook(event: str, task: Optional["Task"], task_id: str, run_id: Optional[int], **fields: Any) -> None:
    """Lifecycle hook for a task transition; ``assignee`` from the (possibly missing) row."""
    _fire_kanban_lifecycle_hook(
        event, task_id, board=get_current_board(),
        assignee=task.assignee if task else None, run_id=run_id, **fields,
    )


def _hook_profile_name() -> str:
    """Active profile for hook payloads; ``"default"`` when it cannot be resolved."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return get_active_profile_name()
    except Exception:
        return "default"


def _kanban_observer_consumed(event: str) -> bool:
    """Hot-path short-circuit: skip payload assembly when nothing subscribes.
    Inspection failure counts as unconsumed (dropping an observer is always safe)."""
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook(event)
    except Exception:  # pragma: no cover - defensive
        return False


def _fire_worker_spawned_hook(
    conn: sqlite3.Connection, task: "Task", workspace_path: str, pid: Optional[int], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_worker_spawned`` AFTER the PID is durably persisted; best-effort."""
    if not _kanban_observer_consumed("on_kanban_worker_spawned"):
        return
    try:
        _fire_kanban_lifecycle_hook(
            "on_kanban_worker_spawned", task.id, board=board or get_current_board(),
            assignee=task.assignee, run_id=_current_run_id(conn, task.id),
            worker_pid=int(pid) if pid else None, workspace_path=str(workspace_path),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban worker spawned hook failed: %s", exc)


def notify_task_updated(
    conn: sqlite3.Connection, task_id: str, changed_fields: Iterable[str], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_task_updated`` AFTER a non-lifecycle task mutation commits
    (also for direct-SQL surfaces like dashboard field editors).
    ``changed_fields`` carries field NAMES only, never values."""
    if not _kanban_observer_consumed("on_kanban_task_updated"):
        return
    try:
        row = conn.execute(
            "SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        _fire_kanban_lifecycle_hook(
            "on_kanban_task_updated", task_id, board=board or get_current_board(),
            assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, changed_fields=list(changed_fields),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban task updated hook failed: %s", exc)

# DispatchResult counters whose non-zero value means the tick did something.
_TICK_ACTIVITY_FIELDS = (
    "spawned", "reclaimed", "promoted", "reconciled_orphans", "reaped_terminal_workers",
    "ended_terminal_runs", "worker_leftovers_reaped", "orphans_reaped", "crashed", "stale", "timed_out", "process_capped",
    "auto_blocked", "rate_limited",
    "infra_unavailable", "cohort_deaths", "auto_assigned_default", "respawn_guarded",
    "skipped_per_profile_capped", "skipped_unassigned", "skipped_nonspawnable",
)


def _fire_dispatch_tick_hook(
    result: "DispatchResult", *, board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """``on_kanban_dispatch_tick`` — strictly AFTER ``_dispatch_tick_lock`` is
    released so a slow subscriber cannot stall a sibling dispatcher.

    Re-port of PR #56066 per the #64231 batch disposition: renamed to the taxonomy form and called by
    ``dispatch_once`` strictly AFTER ``_dispatch_tick_lock`` has been released — the original fired inside
    the lock, so a slow subscriber could extend the single-writer critical section and stall a sibling
    dispatcher's tick. Observer-only and fully best-effort: any subscriber failure is swallowed.
    """
    if not _kanban_observer_consumed("on_kanban_dispatch_tick"):
        return
    try:
        from hermes_cli.lifecycle import invoke_hook

        profile_name = _hook_profile_name()
        if board is None:
            try:
                board = get_current_board()
            except Exception:
                board = None
        outcome = "ok"
        if result.skipped_locked:
            outcome = "skipped_locked"
        elif result.workspace_refused:
            outcome = "workspace_refused"
        elif not any(getattr(result, f) for f in _TICK_ACTIVITY_FIELDS):
            outcome = "idle"
        invoke_hook(
            "on_kanban_dispatch_tick", board=board, profile_name=profile_name,
            dry_run=bool(dry_run), outcome=outcome, result=result,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban dispatch tick hook failed: %s", exc)

# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# If a worker's PID is still alive but its ``last_heartbeat_at`` is
# older than this when ``release_stale_claims`` runs, treat the worker
# as wedged and reclaim regardless of PID liveness (#29747 gap 3).
# This catches the logic-loop case where the process is technically
# running but not making observable progress.  ``_touch_activity``
# bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
# so any genuinely active worker keeps its heartbeat fresh as a side
# effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace added to a claim when a reclaim is deferred because the previous
# host-local worker is still alive after a termination attempt. Releasing the
# claim in that state would spawn a duplicate alongside the surviving worker —
# the runaway seen when a cgroup memory.high throttle parks a worker in
# uninterruptible (D) state, where a pending SIGKILL cannot be delivered until
# the throttle lifts. Holding the claim a short grace and retrying next tick
# stops the duplication; once no duplicate is spawned the pressure eases, the
# signal lands, and the following tick reclaims cleanly.
RECLAIM_DEFER_GRACE_SECONDS = 120

# A pid-less claim whose local CLAIMER (dispatching gateway) is dead may still
# have a detached worker: Popen precedes the pid stamp. With no heartbeat on
# the current run yet, hold the claim this long after it was taken before
# treating the spawn as never-happened. Measured 2026-09-24 over 12,732 live
# runs: claim-to-first-heartbeat p50 6 s, p90 21 s, p99 69 s, max 708 s.
# The claim TTL (900 s) sits above that max. See _dead_claimer_release_at.
DEAD_CLAIMER_LAUNCH_BOUND_SECONDS = DEFAULT_CLAIM_TTL_SECONDS


def _dead_claimer_launch_bound_seconds() -> int:
    """The launch bound follows the CLAIM TTL: an operator who shortens the claim window
    (``HERMES_KANBAN_CLAIM_TTL_SECONDS``, e.g. a chaos rig at 3 s) has said how long a claim may
    hold without evidence; holding a dead claimer's pid-less claim for the 900 s default anyway
    strands the card for 15 min past every tick that could have reclaimed it. Default unchanged."""
    if os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip():
        return _resolve_claim_ttl_seconds()
    return DEAD_CLAIMER_LAUNCH_BOUND_SECONDS


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Explicit ``ttl_seconds`` > ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` > default."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    return _env_int("HERMES_KANBAN_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1)

# Grace period after a task transitions to ``running`` during which
# ``detect_crashed_workers`` skips the ``_pid_alive`` check. Covers the
# fork() → /proc-visibility window where liveness can transiently report
# False for a freshly-spawned worker. The 15-minute claim TTL still
# catches genuinely-crashed workers; this only suppresses false positives
# during the launch window.
DEFAULT_CRASH_GRACE_SECONDS = 30

# Sentinel exit code a kanban worker uses to signal "I bailed because the
# provider rate-limited / exhausted quota, not because the task failed."
# The dispatcher's reap classifier maps this to a ``rate_limited`` exit kind
# so ``detect_crashed_workers`` can release the task back to ``ready``
# WITHOUT counting a failure (the circuit breaker must never trip on a
# transient throttle). 75 == BSD ``EX_TEMPFAIL`` (sysexits.h) — the
# conventional "temporary failure, retry later" code, and well clear of the
# 0/1/2 codes the worker uses for success / generic failure / usage error.
KANBAN_RATE_LIMIT_EXIT_CODE = 75

# Worker exit "provider rejected the configuration": credential revoked (401/403), model gone
# (404), TLS chain broken — a retry cannot fix it, so the dispatcher parks the card blocked on
# the FIRST occurrence instead of spending ``failure_limit`` identical spawns. 78 == BSD EX_CONFIG.
KANBAN_TERMINAL_PROVIDER_EXIT_CODE = 78

# Exit codes that mean the worker HARNESS never ran, so the task never got a
# chance to fail (card t_263e7303, incident 2026-09-21).
#
# These are the shell's own "I could not execute that" codes, emitted BEFORE
# any worker code runs:
#   127 — command not found / a wrapper exec'ing a path that does not exist.
#         The live case: ``~/.local/bin/hermes`` execs the deploy venv's CLI,
#         the deploy tree was missing for 38 minutes, and every worker spawned
#         in that window exited 127. Three argus runs on t_671fd52c and three
#         on t_59c0886e were recorded as CRASHES and their CARDS flipped to
#         blocked — a deploy-window outage attributed to innocent work.
#   126 — found but not executable / bad interpreter. Same class: a venv
#         console script whose absolute shebang points at a renamed venv dies
#         exactly this way (measured while fixing the shim).
#
# A task cannot be at fault for an exit that happened before its worker
# started, so this is classed like the quota wall: requeue, do NOT count a
# failure, do NOT flip the card to blocked. It is kept DISTINCT from
# ``rate_limited`` because the remedy is different — quota needs a timer,
# this needs the deploy/venv fixed — and the board history should not call an
# infrastructure outage a quota wall.
KANBAN_INFRA_EXIT_CODES: frozenset[int] = frozenset({126, 127})


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``kanban.rate_limit_cooldown_seconds`` is authoritative; the legacy
    ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` bridge is the fallback (0 = next tick, for
    tests). Invalid/negative values use ``DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS``."""
    try:
        from hermes_cli.config import load_config
        configured = load_config().get("kanban", {}).get("rate_limit_cooldown_seconds")
    except Exception:
        configured = None
    if configured is not None:
        try:
            parsed = int(str(configured))
        except (TypeError, ValueError):
            parsed = -1
        if parsed >= 0:
            return parsed
        return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)

# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # per comment


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """``just now`` / ``18h ago`` / ``3d ago``; "" for a missing/invalid ts. An LLM
    reads a bare absolute timestamp as current fact — the relative age is what
    prompts a worker to re-verify stale sibling work."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 60:  # includes negative = clock skew across machines; never claim "in the future"
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override", default=None,
)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

# Env vars that pin a kanban path DIRECTLY, outranking anything derived from
# ``HERMES_HOME``. The dispatcher injects several of these into every worker's
# environment on purpose (defense in depth), which means a worker that redirects
# ``HERMES_HOME`` to a tempdir — the standard, documented way to sandbox fleet
# state — is NOT sandboxed for kanban and will happily write to the live board.
# ``HERMES_KANBAN_SANDBOX=1`` neutralises all of them at once; see
# :func:`kanban_db_path`.
_KANBAN_PATH_PIN_ENV_VARS = (
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


def kanban_sandbox_enabled() -> bool:
    """True when ``HERMES_KANBAN_SANDBOX`` requests HERMES_HOME-relative paths.

    Set it to ``1``/``true``/``yes``/``on`` to make every kanban path resolve
    from ``HERMES_HOME`` and ignore the ``HERMES_KANBAN_*`` path pins listed in
    :data:`_KANBAN_PATH_PIN_ENV_VARS`. Intended for tests and any code that
    must not touch the live board — notably a dispatched worker writing tests
    against kanban internals, which inherits the dispatcher's pins.
    """
    return env_var_enabled("HERMES_KANBAN_SANDBOX")


def _kanban_path_override(name: str) -> str:
    """Return the raw ``name`` path-pin env value, or ``""`` when sandboxed.

    Single choke point for every ``HERMES_KANBAN_*`` path pin so the sandbox
    flag can't be honoured by some resolvers and silently ignored by others.
    """
    if kanban_sandbox_enabled():
        return ""
    return os.environ.get(name, "").strip()

# The ``HERMES_KANBAN_DB`` pin and ``HERMES_HOME`` as they were when this
# module was first imported. Load-bearing for telling the INCIDENT shape apart
# from a caller that legitimately pins a DB of its own.
#
# The incident is specifically: the pin arrives INHERITED (the dispatcher puts
# it in every worker's env) and the process then redirects ``HERMES_HOME`` to
# sandbox itself. The pin is unchanged from startup; ``HERMES_HOME`` is not.
#
# A test fixture or tool that sets ``HERMES_KANBAN_DB`` itself has *chosen* that
# path — measured on tests/gateway/test_kanban_notifier.py and 8 sibling files,
# which pin a per-test tmp DB while ``HERMES_HOME`` is a different tmp dir. That
# is not a divergence to refuse; a blanket refusal broke 33 such tests.
_PIN_AT_IMPORT = os.environ.get("HERMES_KANBAN_DB", "").strip()
_HERMES_HOME_AT_IMPORT = os.environ.get("HERMES_HOME", "").strip()


def _pin_divergence_is_a_hazard(target: Path) -> bool:
    """True when a diverging pin is the INCIDENT shape, not a chosen path.

    Both divergence guards gate on this one predicate, so they cannot drift on
    which situations are dangerous.

    Two shapes qualify, and between them they cover every recorded incident:

    * **Inherited pin under a redirected HERMES_HOME** — the pin is byte-identical
      to what this process started with (the dispatcher injects it into every
      worker env) and ``HERMES_HOME`` has moved since. That is 2026-08-08 and
      t_d2b884e7 exactly: a worker sandboxes itself by moving ``HERMES_HOME``
      and the inherited pin silently keeps it on the live board.
    * **The pin reaches this machine's native Hermes home** — catches the
      variant with no in-process change to detect, e.g.
      ``HERMES_HOME=$(mktemp -d) HERMES_KANBAN_DB=~/.hermes/kanban.db cmd``.
      The pin points at production; refusing is right regardless of provenance.

    Everything else is a caller that CHOSE its pin and is not being silently
    un-sandboxed — every kanban test fixture does this (measured: a blanket
    refusal broke 33 otherwise-passing tests across 9 files). Left alone.
    """
    # Shape 1: inherited pin, HERMES_HOME moved out from under it.
    if _PIN_AT_IMPORT and os.environ.get(
        "HERMES_KANBAN_DB", ""
    ).strip() == _PIN_AT_IMPORT:
        if os.environ.get("HERMES_HOME", "").strip() != _HERMES_HOME_AT_IMPORT:
            return True
    # Shape 2: the pin reaches the machine's real Hermes home.
    try:
        from hermes_constants import _get_platform_default_hermes_home
        native = _get_platform_default_hermes_home().resolve(strict=False)
    except Exception:  # pragma: no cover - defensive
        return False
    # Spelling-blind: a pin naming the live home in another case/firmlink
    # spelling still reaches production (card t_ee808d83 class sweep).
    return _pin_tree_agrees(target, native)


class KanbanPinDivergenceError(RuntimeError):
    """Raised when the ``HERMES_KANBAN_*`` pins contradict what a caller asked.

    Two shapes, one defect class: the resolver has **already computed** that
    the board the caller asked for and the board the pin points at are
    different, and the old behaviour was to log a warning and hand back the
    pinned (live) board anyway. That is fail-OPEN on a destructive path — a
    warning on stdout is perfectly accurate and perfectly skippable, and on
    2026-09-21 a probe that redirected ``HERMES_HOME`` to sandbox itself wrote
    a junk card and 17 events onto 16 production cards with the warning right
    there in its output.

    ``HERMES_KANBAN_SANDBOX=1`` remains the documented escape hatch: it
    neutralises every pin, so the divergence cannot arise and this never
    raises.
    """

# Pairs of (HERMES_HOME, override) already proven SAFE by
# ``_refuse_if_override_escapes_hermes_home``. Keyed on the RAW env strings so
# the filesystem work (two ``Path.resolve()`` calls) happens once per distinct
# environment rather than on every ``kanban_db_path()`` — which sits on the
# ``connect()`` path and is called constantly.
#
# Only AGREEING pairs are memoised, and that is load-bearing: a warn-once
# ledger would have made the second call to a diverging pin succeed silently,
# so a caller that ignored (or never saw) the first refusal could simply try
# again and reach the live board. A refusal must be permanent for as long as
# the environment says it.
_CHECKED_OVERRIDE_ESCAPES: set[tuple[str, str]] = set()


def _refuse_if_override_escapes_hermes_home(override: Path) -> None:
    """Refuse when ``HERMES_KANBAN_DB`` points outside the HERMES_HOME root.

    This is the signal that was missing on 2026-08-08: a worker redirected
    ``HERMES_HOME`` to a tempdir, believed it was sandboxed, and wrote six real
    cards to the production board because the dispatcher's ``HERMES_KANBAN_DB``
    outranks ``HERMES_HOME``. It was made loud in that incident's wake, and on
    2026-09-21 the loudness proved insufficient: the warning fired, verbatim
    and correct, and the probe wrote to the live board regardless.

    Redirecting ``HERMES_HOME`` is an unambiguous statement of intent to
    isolate. Having detected that the pin defeats it, we refuse rather than
    hand a live-board path to a caller that asked to be sandboxed.

    Scoped to the genuinely hazardous shapes — see
    :func:`_pin_divergence_is_a_hazard`. A caller that pins a DB of its own
    choosing (every kanban test fixture does) is not making an isolation claim
    it is then betrayed on, and is left alone: measured, a blanket refusal here
    broke 33 otherwise-passing tests across 9 files.
    """
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if not hermes_home:
        return
    key = (hermes_home, str(override))
    if key in _CHECKED_OVERRIDE_ESCAPES:
        return  # proven safe already.
    try:
        root = kanban_home().resolve(strict=False)
        target = override.resolve(strict=False)
    except OSError:
        return
    # Spelling-blind on the AGREE side too: the hazard predicate below is
    # spelling-blind, so a pin naming a file inside the root in another
    # case/firmlink spelling must be recognised here or it would be refused
    # as an escape (card t_ee808d83, Argus round 2 N1).
    if _pin_tree_agrees(target, root):
        _CHECKED_OVERRIDE_ESCAPES.add(key)
        return  # override lives inside the HERMES_HOME-derived root: normal.
    if not _pin_divergence_is_a_hazard(target):
        # The pin escapes the HERMES_HOME root, but it was chosen by this
        # process and does not reach the machine's live board. Nobody is being
        # silently un-sandboxed. Do NOT memoise: the hazard state is
        # env-dependent and must be re-evaluated on every call.
        return
    raise KanbanPinDivergenceError(
        f"HERMES_KANBAN_DB={target} resolves OUTSIDE the HERMES_HOME-derived "
        f"kanban root {root} — the override wins, so redirecting HERMES_HOME "
        f"did NOT sandbox kanban. Refusing to resolve a live-board path for a "
        f"process that asked to be isolated. Set HERMES_KANBAN_SANDBOX=1 (or "
        f"unset the HERMES_KANBAN_* path pins) if you meant to isolate this "
        f"process from the live board; unset HERMES_HOME (or point it at the "
        f"root that contains the pin) if you meant to use the pinned board."
    )

# ``(board slug, raw HERMES_KANBAN_DB)`` pairs already proven to AGREE by
# ``_refuse_if_pin_contradicts_board_arg``. Same resolve-once role as
# ``_CHECKED_OVERRIDE_ESCAPES``: ``kanban_db_path()`` sits on the ``connect()``
# path, so the two ``Path.resolve()`` calls must not run per call. Only
# agreeing pairs are memoised — a disagreeing pair must refuse every time, not
# just the first, or a caller could retry its way onto the live board.
_CHECKED_PIN_BOARD_CONTRADICTIONS: set[tuple[str, str]] = set()

# Depth of the innermost ``enumerating_boards()`` scope on THIS thread. Thread-
# local because the dispatcher ticks boards in parallel via ``asyncio.to_thread``
# (``_tick_once_for_board``), and a module-global counter would let one thread's
# enumeration silence another thread's genuine single-board contradiction.
_ENUMERATION_DEPTH = threading.local()


def _enumeration_depth() -> int:
    return getattr(_ENUMERATION_DEPTH, "value", 0)


@contextlib.contextmanager
def enumerating_boards():
    """Mark a dynamic extent as ENUMERATING boards rather than ADDRESSING one.

    The pin-contradiction guard exists for a caller that names ONE board and
    gets another. A caller sweeping every board on disk is a different shape:
    under a ``HERMES_KANBAN_DB`` pin every non-active slug trivially disagrees
    with the pin, so an unscoped sweep would refuse on the first board and take
    down every legitimate enumerator in the process (measured before this
    extent existed: 64 contradiction reports on a single dispatcher sweep
    across 65 live boards).

    This is a DYNAMIC EXTENT, not a per-call flag, and that distinction is the
    whole fix. The first cut passed ``warn_on_pin_contradiction=False`` at each
    ``kanban_db_path()`` call site, which cannot work: enumerators also call
    ``connect(board=slug)`` / ``connect_closing(board=slug)``, which re-resolve
    the path internally with no flag to thread through. Measured on that design,
    ``_board_task_counts`` still reported 8 contradictions over 8 boards
    *despite* its call site being flagged. Wrapping the LOOP covers every nested
    resolution, however deep, including ones added later.

    Scoped to the current thread, and re-entrant.
    """
    _ENUMERATION_DEPTH.value = _enumeration_depth() + 1
    try:
        yield
    finally:
        _ENUMERATION_DEPTH.value = max(0, _enumeration_depth() - 1)


class EnumeratedBoardSlug(str):
    """A board slug DISCOVERED by sweeping ``boards/``, not named by a caller.

    The pin-contradiction guard exists to catch a caller that *names* one board
    and silently gets another. An enumerator never names a board — it reports
    what is on disk — so its slugs must not trip the guard.

    :func:`enumerating_boards` expresses that as a DYNAMIC EXTENT, and an extent
    is the wrong shape for this data. It is bound to one thread and to one
    moment, but an enumerated slug is a VALUE that outlives both: the notifier
    sweeps boards in ``_collect()``, stores ``{"board": slug}`` in a delivery
    dict, returns it (extent exits), then addresses that slug from a worker
    thread via ``asyncio.to_thread``. Both axes were measured on this branch:

        collect-then-address, SAME thread   -> REFUSED   (extent exited in TIME)
        extent held, asyncio.to_thread      -> REFUSED   (extent lost by THREAD)

    Marking the VALUE fixes both, because the value is what travels. Note that
    a ``ContextVar`` would NOT have been enough for the second axis:
    ``gateway/kanban_watchers.py::_run_in_fresh_context`` runs the offloaded
    call in an empty ``Context()`` precisely to drop inherited ContextVars.

    Verified to survive every hop on the real path: a ``dict`` entry, an
    ``asyncio.to_thread`` argument, a list/queue, sqlite parameter binding,
    f-strings, equality and dict-key use against plain ``str``.

    This is deliberately structural. The alternative — re-entering the extent
    at each point of use — works (measured: ALLOWED) but has to be *remembered*
    at all ten board-scoped offload sites in ``gateway/kanban_watchers.py``, and
    silently re-arms the outage the moment an eleventh is added. Marking at the
    enumerator is one choke point that no call site can forget.

    A ``str`` subclass, so every existing consumer keeps working unchanged.
    Provenance deliberately does NOT survive serialization: a slug that
    round-trips through JSON comes back a plain ``str`` and re-arms the guard,
    which is the fail-CLOSED direction.
    """

    __slots__ = ()


def _is_enumerated(board: Optional[str]) -> bool:
    """True when this slug came from an enumerator rather than a caller."""
    return isinstance(board, EnumeratedBoardSlug)


def enumerated_slug(board):
    """Stamp ``board`` as enumerator-produced, preserving ``None``.

    Idempotent, and never raises on an odd value — provenance marking must not
    become its own failure mode on a path whose whole job is not to fail open.
    """
    if board is None:
        return None
    if isinstance(board, EnumeratedBoardSlug):
        return board
    try:
        return EnumeratedBoardSlug(board)
    except Exception:  # pragma: no cover - defensive
        return board


def enumerating_each(boards):
    """Iterate ``boards`` with :func:`enumerating_boards` held for each BODY.

    ``for meta in enumerating_each(boards):`` puts the whole loop body inside
    the extent, which is the shape that cannot half-apply. Wrapping only the
    path resolve inside a per-board loop does not work: the body then calls
    ``connect(board=slug)`` / ``count_notify_subs(board=slug)``, which
    re-resolve internally and land OUTSIDE the extent. Measured on a simulated
    dispatcher tick with the fingerprint scoped and the ``connect`` not: the
    sweep still emitted warnings, and a genuine single-board contradiction
    probed straight afterwards went silent. With the body inside the extent the
    same sweep emitted 0 and the probe stayed loud.

    The generator is suspended inside the context manager while the consumer
    runs the body, so ``continue``, ``break``, ``return`` and exceptions all
    unwind it correctly.

    The yielded board also carries its ENUMERATED PROVENANCE on the slug value
    itself (:class:`EnumeratedBoardSlug`), which is what covers the consumer
    that stores the slug and addresses it later — after this extent has exited,
    possibly from another thread. The extent alone could not: it is scoped to
    one thread and one moment, and the notifier's slugs outlive both.
    """
    for board in boards:
        with enumerating_boards():
            yield _mark_board_meta(board)


def _mark_board_meta(board: Any) -> Any:
    """Stamp an enumerated board's ``slug`` (and any mapping copy) in place.

    Board entries are ``dict``s from :func:`read_board_metadata`; consumers do
    ``board_meta.get("slug")`` and carry that value onward. Stamping here means
    every one of them inherits the provenance without changing a line — which
    also covers the degraded ``except: boards = [read_board_metadata(DEFAULT)]``
    fallback that never went through :func:`list_boards` at all.
    """
    try:
        if isinstance(board, dict) and board.get("slug") is not None:
            board["slug"] = enumerated_slug(board["slug"])
            return board
        if isinstance(board, str):
            return enumerated_slug(board)
    except Exception:  # pragma: no cover - defensive
        pass
    return board


def _refuse_if_pin_contradicts_board_arg(board: Optional[str], override: Path) -> None:
    """Refuse when an EXPLICIT ``board`` arg disagrees with the pinned DB.

    The ``HERMES_KANBAN_DB`` pin is checked before anything derived from the
    ``board`` argument, and the dispatcher injects it into every worker env. So
    inside a worker, ``kanban_db_path("some-other-board")`` silently returned
    the pinned path and the caller read — or wrote — the wrong board.

    That is not hypothetical: it produced two wrong readings inside one task
    (t_a1e6e877) — a repro that wrote junk cards to the live default board, and
    a review probe whose ``kanban_db_path('ban-forensics')`` answer read exactly
    like the board-alias feature being broken when in fact it was the pin.
    Both operators knew about the trap and hit it anyway. Making it loud was
    not enough either (t_d2b884e7, 2026-09-21), so the contradiction is now
    refused: a caller that names a board and gets a different one is a wrong
    answer, and a wrong answer that looks right is worse than an error.

    Silent by design in the shapes that are not contradictions: ``board`` is
    None (the pin is then the intended source of truth), the pin already
    resolves to the requested board's DB (the normal worker case), the call
    happens inside an :func:`enumerating_boards` extent (a sweep over every
    board on disk is not a claim to be addressing any one of them), and the
    divergence is not hazardous per :func:`_pin_divergence_is_a_hazard` (the
    caller chose its own pin and is not being silently put on production).
    """
    if board is None:
        return
    if _is_enumerated(board):
        # Provenance travels with the VALUE, so this holds in any thread and at
        # any later time — including the notifier's collect-in-the-loop,
        # deliver-from-a-worker-thread shape, which the dynamic extent alone
        # could not reach.
        return  # sweeping every board, not addressing the one named.
    if _enumeration_depth() > 0:
        return  # sweeping every board, not addressing the one named.
    try:
        slug = _normalize_board_slug(board)
    except ValueError:
        return  # invalid slug: the pin path short-circuits before validation.
    if slug is None:
        return
    key = (slug, str(override))
    if key in _CHECKED_PIN_BOARD_CONTRADICTIONS:
        return  # proven to agree with the pin already.
    try:
        requested = _board_db_path_ignoring_pin(slug).resolve(strict=False)
        pinned = override.resolve(strict=False)
    except (OSError, ValueError):
        return
    # Spelling-blind equality: the same DB file spelled in another case or
    # via the Data-volume firmlink is agreement, not a contradiction (card
    # t_ee808d83, Argus round 2 N1).
    if _pin_file_agrees(requested, pinned):
        _CHECKED_PIN_BOARD_CONTRADICTIONS.add(key)
        return  # pin agrees with the argument: the normal worker case.
    if not _pin_divergence_is_a_hazard(pinned):
        # Shares one predicate with the escape guard so the two cannot drift on
        # what counts as dangerous. Not memoised: the hazard state depends on
        # live env, so it must be re-evaluated every call.
        return
    raise KanbanPinDivergenceError(
        f"kanban_db_path(board={slug!r}) would return the HERMES_KANBAN_DB pin "
        f"{pinned}, NOT that board's DB {requested} — the pin outranks the "
        f"board argument, so this caller would read (and write) a different "
        f"board than it asked for. Refusing. Set HERMES_KANBAN_SANDBOX=1 (or "
        f"unset the HERMES_KANBAN_* path pins) if you meant to address the "
        f"board you named; if this call site sweeps every board, wrap its loop "
        f"in kanban_db.enumerating_boards()."
    )

# Slug validator: lowercase alphanumerics, digits, hyphens; 1–64 chars.
# Strict enough to stop traversal (`..`) and embedded path separators, loose
# enough that kebab-case names like ``atm10-server`` or ``hermes-agent``
# pass without fuss. Board names with display formatting (spaces, emoji)
# live in ``board.json``; the slug is just the directory name.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def _slug_or_default(board: Optional[str]) -> str:
    return _normalize_board_slug(board) or DEFAULT_BOARD


def _require_slug(slug: str) -> str:
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    return normed


def kanban_home() -> Path:
    """Return the shared Hermes root that anchors the kanban board.

    Resolution order:

    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments). Ignored when
       ``HERMES_KANBAN_SANDBOX`` is set — see :func:`kanban_db_path`.
    2. ``get_default_hermes_root()``, which already returns ``<root>``
       when ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns
       ``HERMES_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``HERMES_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = _kanban_path_override("HERMES_KANBAN_HOME")
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """``<root>/kanban/boards`` — parent of the *additional* named boards.
    ``default`` is deliberately not here (its DB stays at ``<root>/kanban.db``)."""
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """``<root>/kanban/current`` — one-line slug written by ``boards switch``; absent = ``default``."""
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Active slug: context override -> ``HERMES_KANBAN_BOARD`` -> ``<root>/kanban/current``
    (only while that board exists) -> ``DEFAULT_BOARD``. A malformed/stale slug
    falls through — the dispatcher must never crash on a hand-edited file."""
    def _existing(candidate: str) -> Optional[str]:
        if not candidate:
            return None
        try:
            normed = _normalize_board_slug(candidate)
        except ValueError:
            return None
        return normed if normed and board_exists(normed) else None

    for candidate in (
        (_CURRENT_BOARD_OVERRIDE.get() or "").strip(),
        os.environ.get("HERMES_KANBAN_BOARD", "").strip(),
    ):
        found = _existing(candidate)
        if found:
            return found
    try:
        f = current_board_path()
        if f.exists():
            # utf-8-sig read fix (ours): tolerate BOM-persisted current-board files.
            val = f.read_text(encoding="utf-8-sig").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board; returns the file written. Does NOT
    check the board exists — callers do (so ``boards switch <typo>`` errors)."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    path = current_board_path()
    _assert_live_board_tree_write_allowed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    _assert_live_board_tree_write_allowed(current_board_path())
    with contextlib.suppress(FileNotFoundError):
        current_board_path().unlink()
_BOARD_ALIAS_CACHE: tuple[Optional[tuple[int, int]], dict[str, str]] = (None, {})


def board_aliases_path() -> Path:
    """Return ``<root>/kanban/board-aliases.json``.

    Maps a retired board slug to its current one, so consumers pinned to
    the old name (a worker's spawn env, a script, a bookmark) resolve to
    the live board instead of silently initialising an empty phantom.
    """
    return kanban_home() / "kanban" / "board-aliases.json"


def _read_board_aliases() -> dict[str, str]:
    """Return the ``{old_slug: new_slug}`` map. Never raises.

    A malformed or missing file yields ``{}`` — board resolution must not
    break because a hand-edited JSON file lost a brace.

    Cached on (mtime, size) because :func:`board_dir` is on the hot path
    for every :func:`connect`; an edit to the file is still picked up on
    the next call without a restart.
    """
    global _BOARD_ALIAS_CACHE
    try:
        p = board_aliases_path()
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _BOARD_ALIAS_CACHE = (None, {})
        return {}
    cached_stamp, cached_val = _BOARD_ALIAS_CACHE
    if cached_stamp == stamp:
        return cached_val
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        _BOARD_ALIAS_CACHE = (stamp, {})
        return {}
    if not isinstance(raw, dict):
        _BOARD_ALIAS_CACHE = (stamp, {})
        return {}
    out: dict[str, str] = {}
    for old, new in raw.items():
        try:
            o = _normalize_board_slug(old)
            n = _normalize_board_slug(new)
        except ValueError:
            continue
        if o and n and o != n:
            out[o] = n
    _BOARD_ALIAS_CACHE = (stamp, out)
    return out


def resolve_board_alias(slug: Optional[str]) -> Optional[str]:
    """Follow the alias map for ``slug``; return the canonical slug.

    Chains are followed (``a -> b -> c``) with a bounded walk and a
    cycle guard, so a self-referential or looping map degrades to the
    last good slug rather than hanging board resolution.
    """
    if slug is None:
        return None
    aliases = _read_board_aliases()
    if not aliases:
        return slug
    seen = {slug}
    cur = slug
    for _ in range(len(aliases) + 1):
        nxt = aliases.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return cur


def board_dir(board: Optional[str] = None) -> Path:
    """Return the on-disk directory for ``board``.

    ``default`` is ``<root>/kanban/boards/default/`` **for metadata only**
    (board.json + workspaces/ + logs/). Its DB file stays at
    ``<root>/kanban.db`` for back-compat — see :func:`kanban_db_path`.

    All other boards live at ``<root>/kanban/boards/<slug>/`` with
    everything inside that directory including the ``kanban.db``.

    Retired slugs are redirected through :func:`resolve_board_alias`, so
    a consumer still pinned to an old board name lands on the live board
    rather than initialising an empty phantom beside it.
    """
    slug = _slug_or_default(board)
    slug = resolve_board_alias(slug) or slug
    return boards_root() / slug


def board_state_dir(board: Optional[str] = None) -> Path:
    """Directory for a board's dispatcher latch files (rate-limit circuit, budget pause).

    ``default`` -> ``<root>/kanban/``; every other board -> :func:`board_dir`.
    Writing ``default``'s latches into ``boards/default/`` minted that dir on a
    host whose default DB lives at ``<root>/kanban.db``. A create-mode sqlite
    connect on ``boards/default/kanban.db`` then left a 0-byte phantom board DB
    instead of failing, and enumerators failed closed on it for 24h
    (2026-09-18, 09-27, 09-29). Without the dir that connect cannot create a
    file.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    slug = resolve_board_alias(slug) or slug
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban"
    return board_dir(slug)


def board_exists(board: Optional[str] = None) -> bool:
    """Board has ``board.json`` or ``kanban.db`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))


def _dir_holds_board(d: Path) -> bool:
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver: ``env_var`` override, else legacy ``<root>/<default_parts>``
    for the ``default`` board, else ``board_dir(slug)/leaf``."""
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def kanban_db_path(board: Optional[str] = None) -> Path:
    """Return the path to the ``kanban.db`` for ``board``.

    Resolution (highest precedence first):

    1. ``HERMES_KANBAN_DB`` env var — pins the path directly. Honoured for
       back-compat and for the dispatcher→worker handoff (defense in
       depth: dispatcher injects this into worker env so workers are
       immune to any path-resolution disagreement).
    2. When ``board`` arg is None, the active board from
       :func:`get_current_board` is used.
    3. Board ``default`` → ``<root>/kanban.db`` (back-compat path).
       Other boards → ``<root>/kanban/boards/<slug>/kanban.db``.

    🔴 **The override DEFEATS ``HERMES_HOME`` sandboxing.** Because it is
    checked before anything ``HERMES_HOME``-derived, and because the
    dispatcher pins it into *every* worker env, the standard hermeticity
    move — ``HERMES_HOME=$(mktemp -d)`` — does **not** isolate kanban. A
    process that redirects only ``HERMES_HOME`` still resolves to, and
    writes to, the LIVE board. On 2026-08-08 that put six fixture cards on
    a production board and got a real worker spawned against one of them.

    To isolate kanban, set ``HERMES_KANBAN_SANDBOX=1`` — it neutralises
    every ``HERMES_KANBAN_*`` path pin so all kanban paths resolve from
    ``HERMES_HOME``::

        HERMES_KANBAN_SANDBOX=1 HERMES_HOME=$(mktemp -d) pytest ...

    Equivalently, strip the pins before importing this module::

        for _k in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
            os.environ.pop(_k, None)

    When ``HERMES_HOME`` is set and the override resolves outside the
    ``HERMES_HOME``-derived kanban root, this **raises**
    :class:`KanbanPinDivergenceError`. Redirecting ``HERMES_HOME`` is an
    unambiguous statement of intent to isolate; handing back a live-board path
    anyway is fail-open on a destructive path, and a warning was demonstrably
    skippable (2026-09-21: the warning fired and the probe wrote 17 events onto
    16 production cards regardless).

    Likewise, when an EXPLICIT ``board`` argument is passed and the override
    resolves to a *different* board's DB, this raises rather than returning a
    board the caller did not ask for.

    A caller that sweeps every board on disk is not addressing any one of them,
    and under a pin every non-active slug trivially disagrees — so wrap such a
    loop in :func:`enumerating_boards`, which suppresses the board-argument
    check for the whole dynamic extent (including the nested ``connect()``
    resolutions a per-call flag could never reach). Do NOT wrap a single-board
    lookup: that is exactly the case the guard exists to catch.
    """
    override = _kanban_path_override("HERMES_KANBAN_DB")
    if override:
        path = Path(override).expanduser()
        _refuse_if_override_escapes_hermes_home(path)
        _refuse_if_pin_contradicts_board_arg(board, path)
        return path
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    return _board_db_path_ignoring_pin(slug)


def _board_db_path_ignoring_pin(slug: str) -> Path:
    """Map an already-normalized board slug to its DB path, ignoring the pin.

    The single definition of the board→DB layout, shared by
    :func:`kanban_db_path` and the contradiction guard
    :func:`_refuse_if_pin_contradicts_board_arg` — so the guard can never drift
    from the resolution it is describing.
    """
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def workspaces_root(board: Optional[str] = None, *, stale_pin_ok: bool = False) -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Anchored per-board so workspaces don't leak between projects.
    ``kanban.workspaces_root`` is the canonical placement policy. The
    dispatcher injects its board-qualified result as
    ``HERMES_KANBAN_WORKSPACES_ROOT`` into worker env; when both are visible,
    disagreement fails closed. The environment override remains available for
    tests and deployments without configured policy, and is ignored when
    ``HERMES_KANBAN_SANDBOX`` is set — see :func:`kanban_db_path`.

    ``default`` keeps the legacy path ``<root>/kanban/workspaces/`` so
    that existing scratch workspaces from before the boards feature are
    preserved. Other boards use ``<root>/kanban/boards/<slug>/workspaces/``.

    ``stale_pin_ok`` is for NON-placement readers (gc, audits): a worker
    spawned before ``kanban.workspaces_root`` changed still carries the old
    pin, and refusing there only breaks the reader. Config wins; placement
    callers keep the default fail-closed raise.
    """
    from hermes_cli.kanban_workspace_policy import (
        WorkspaceUnavailable, configured_root, validate_mount,
    )

    override = _kanban_path_override("HERMES_KANBAN_WORKSPACES_ROOT")
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    root, require_mount = configured_root()
    if root is not None:
        if require_mount:
            validate_mount(root)
        resolved = root / slug
        if override and Path(override).expanduser() != resolved and not stale_pin_ok:
            raise WorkspaceUnavailable("workspaces_root_invalid: board pin disagrees with config")
        return resolved
    if override:
        return Path(override).expanduser()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "workspaces"
    return board_dir(slug) / "workspaces"


def workspace_root_candidates(board: Optional[str] = None) -> list[Path]:
    """Every root a scratch workspace of *board* may live under. Never raises.

    The UNION of the env pin, the configured ``kanban.workspaces_root/<board>``
    and the legacy per-board root, without the fail-closed pin/config
    disagreement check or mount validation of :func:`workspaces_root`. For
    exclusion/containment readers only (survivor durability): excluding more
    is the conservative direction there. Never use it for placement.
    """
    from hermes_cli.kanban_workspace_policy import configured_root

    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    roots: list[Path] = []
    override = _kanban_path_override("HERMES_KANBAN_WORKSPACES_ROOT")
    if override:
        roots.append(Path(override).expanduser())
    try:
        root, _require_mount = configured_root()
    except ValueError:
        root = None
    if root is not None:
        roots.append(root / slug)
    if slug == DEFAULT_BOARD:
        roots.append(kanban_home() / "kanban" / "workspaces")
    else:
        roots.append(board_dir(slug) / "workspaces")
    return list(dict.fromkeys(roots))


def attachments_root(board: Optional[str] = None) -> Path:
    """Return the directory under which task file attachments are stored.

    Mirrors :func:`worker_logs_dir` / :func:`workspaces_root`: anchored
    per-board so attachments don't leak between projects. Each task gets
    its own ``<root>/.../attachments/<task_id>/`` subdirectory.

    ``HERMES_KANBAN_ATTACHMENTS_ROOT`` pins the path directly (highest
    precedence) for tests and unusual deployments. Ignored when
    ``HERMES_KANBAN_SANDBOX`` is set — see :func:`kanban_db_path`.

    ``default`` uses ``<root>/kanban/attachments/``; other boards use
    ``<root>/kanban/boards/<slug>/attachments/``.

    Workers (which run with full file-tool access) read attached files
    by the absolute path surfaced in :func:`build_worker_context`. On the
    local terminal backend — the default for kanban — that path resolves
    directly. Remote backends (Docker/Modal) need this directory mounted;
    see the kanban docs.
    """
    override = _kanban_path_override("HERMES_KANBAN_ATTACHMENTS_ROOT")
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "attachments"
    return board_dir(slug) / "attachments"


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _board_path(None, board, ("kanban", "logs"), "logs")
_WORKER_LOG_TAIL_BYTES = 4096

# The per-task worker log is opened APPEND across runs (see ``_default_spawn``),
# so a byte window into it spans MULTIPLE runs. Anything that needs "what THIS
# run said" must segment the file first: the dispatcher stamps a boundary line
# immediately before each spawn, and the segment is everything after the LAST
# one. Without this, a tail-based fingerprint is a size-dependent slice of all
# runs concatenated — it silently compares run 1 against itself forever below
# the window size, and cannot see a repeat at all just under it.
_RUN_BOUNDARY_PREFIX = "[hermes-kanban-run-boundary "

# How far back to look for the boundary. Real worker runs on this board measure
# ~2-3 KB; 64 KiB covers an order of magnitude more. A run whose output exceeds
# it has no findable boundary, and the segment reader returns None rather than
# guessing — an unsegmentable run must never be fingerprinted.
_WORKER_LOG_SEGMENT_BYTES = 65536

# Lines whose content varies per run even when the run did the SAME thing.
# They must not contribute to a run fingerprint or every run looks distinct.
_RUN_VARYING_LINE_MARKERS = ("session_id:",)


def _stamp_worker_log_run_boundary(log_path: Path) -> None:
    """Append a unique run-boundary line to a worker log before spawning.

    Best-effort: a log that cannot be written just yields an unsegmentable
    run later (no fingerprint, full retry budget), never a failed spawn.

    The nonce makes the boundary unforgeable from inside the worker — a worker
    that echoed a predictable marker could otherwise truncate its own segment
    down to a short constant and fake a reproduced no-op.
    """
    try:
        import secrets
        with open(log_path, "ab") as fh:
            fh.write(
                f"\n{_RUN_BOUNDARY_PREFIX}{secrets.token_hex(8)}]\n".encode("utf-8")
            )
    except OSError:
        pass


def _worker_log_run_segment(
    task_id: str,
    *,
    board: Optional[str] = None,
) -> Optional[str]:
    """Return only the CURRENT run's slice of a worker's append-mode log.

    ``None`` when the run cannot be isolated (no log, no boundary in the
    window, empty segment). Callers must treat ``None`` as "no evidence",
    never as "same as last time".
    """
    try:
        path = worker_logs_dir(board=board) / f"{task_id}.log"
        with path.open("rb") as log_f:
            log_f.seek(0, os.SEEK_END)
            size = log_f.tell()
            log_f.seek(max(0, size - _WORKER_LOG_SEGMENT_BYTES))
            raw = log_f.read(_WORKER_LOG_SEGMENT_BYTES)
        text = raw.decode("utf-8", errors="replace")
        idx = text.rfind(_RUN_BOUNDARY_PREFIX)
        if idx < 0:
            return None
        newline = text.find("\n", idx)
        if newline < 0:
            return None
        segment = text[newline + 1:].strip()
        return segment or None
    except OSError:
        return None
    except Exception:
        _log.debug("failed to segment worker log for %s", task_id, exc_info=True)
        return None

# A worker that dies on a provider wall BEFORE the model loop can exit 1 with
# the cause only in its log ("Codex credential is in cooldown." — raised at
# credential resolve, where the EX_TEMPFAIL sentinel is never reached). Only the
# run's final lines count, so a stray mention earlier in a real crash does not.
_STDERR_COOLDOWN_CLASSES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("credential_cooldown", re.compile(r"\bcredentials? (?:is|are) in cooldown\b", re.IGNORECASE)),
    ("quota", re.compile(
        r"\b(?:rate[\s_-]?limit(?:ed)?|too many requests|quota (?:exceeded|exhausted)|"
        r"usage limit (?:reached|exceeded)|(?:HTTP|status|error)[\s:]*429)\b",
        re.IGNORECASE,
    )),
)
_STDERR_COOLDOWN_TAIL_LINES = 3


def _stderr_cooldown_class(segment: Optional[str]) -> Optional[str]:
    """Name the provider-wall class this run's last log lines show, or None."""
    if not segment:
        return None
    lines = [ln.strip() for ln in segment.splitlines() if ln.strip()]
    tail = "\n".join(lines[-_STDERR_COOLDOWN_TAIL_LINES:])
    for name, pattern in _STDERR_COOLDOWN_CLASSES:
        if pattern.search(tail):
            return name
    return None


def _run_output_fingerprint(segment: Optional[str]) -> str:
    """Fingerprint ONE run's output; "" when there is nothing comparable.

    Taken from the END of the run, not the start: the constant startup banner
    is identical on every run, so a head-anchored fingerprint says "identical"
    about two runs that did completely different work. The worker's last words
    are the part that actually distinguishes a reproduced no-op from two
    unrelated paperwork misses.
    """
    if not segment:
        return ""
    kept = [
        line for line in segment.splitlines()
        if not any(marker in line for marker in _RUN_VARYING_LINE_MARKERS)
    ]
    normalized = " ".join(" ".join(kept).split())
    return normalized[-400:]


def _worker_log_stderr_tail(
    task_id: str,
    *,
    board: Optional[str] = None,
) -> Optional[str]:
    """Return a bounded, redacted tail of a worker's combined output log.

    ``_default_spawn`` merges stderr into stdout, so the per-task log is the
    only durable source for startup/provider errors after the child exits.
    This helper is best-effort: crash accounting must still work when the log
    is absent, unreadable, or contains malformed bytes.
    """
    try:
        path = worker_logs_dir(board=board) / f"{task_id}.log"
        with path.open("rb") as log_f:
            log_f.seek(0, os.SEEK_END)
            size = log_f.tell()
            log_f.seek(max(0, size - _WORKER_LOG_TAIL_BYTES))
            raw = log_f.read(_WORKER_LOG_TAIL_BYTES)
        tail = raw.decode("utf-8", errors="replace").strip()
        if not tail:
            return None
        # Event payloads are durable board history. Redact unconditionally,
        # independent of the display-only security.redact_secrets toggle.
        from agent.redact import redact_sensitive_text  # type: ignore[import-not-found]
        return redact_sensitive_text(tail, force=True).strip() or None
    except (OSError, ImportError):
        return None
    except Exception:
        # Log-tail capture is observability only. A redactor/runtime failure
        # must never roll back the surrounding crash-recovery transaction.
        _log.debug("failed to capture worker log tail for %s", task_id, exc_info=True)
        return None


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return board_dir(_slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Return ``board.json`` contents (or synthesized defaults).

    Never raises — a missing / malformed ``board.json`` falls back to a
    synthesised entry so the dashboard always has something to render.
    Includes the canonical ``slug`` and ``db_path`` so the caller
    doesn't need to reconstruct them.

    The ``db_path`` resolve is REPORTING where a board's DB would live, not a
    claim to be addressing that board, so it runs inside
    :func:`enumerating_boards`. Without that it trips the pin-contradiction
    refusal and breaks the "never raises" contract above — which matters most
    exactly where it is least expected: this function IS the discovery
    fallback for every board sweep in the fleet::

        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]

    (``gateway/kanban_watchers.py`` x4, ``tui_gateway/server.py``,
    ``plugins/kanban/dashboard/plugin_api.py``). That branch runs when
    ``list_boards()`` itself failed — i.e. already degraded — and it is
    evaluated BEFORE ``enumerating_each()`` can stamp anything, so neither the
    extent nor the value-provenance mark reached it.

    Note what is deliberately NOT done here: the returned ``slug`` is left
    unstamped. Only an enumerator (:func:`list_boards`, :func:`enumerating_each`)
    marks provenance, so a caller that NAMES a board still refuses when it goes
    on to open it.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    with enumerating_boards():
        meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here)."""
    _assert_not_delegated_child_mutation()
    slug = _slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    _assert_live_board_tree_write_allowed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    # Display metadata for the board just written.
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata)."""
    normed = _require_slug(slug)
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta

# Phantom board dirs already warned about in this process (see list_boards).
_PHANTOM_BOARD_WARNED: set[str] = set()


def _board_db_is_empty(db_path: Path) -> bool:
    """Return True if ``db_path`` is a kanban DB holding zero tasks.

    Read-only and fail-CLOSED: any error (unreadable, locked, corrupt, not
    a kanban schema) returns ``False`` so the caller keeps the board
    visible. Never hide a board we could not positively prove is empty.

    ``mode=ro`` only — deliberately NOT ``immutable=1``. Every board DB is
    ``journal_mode=wal``, and ``immutable=1`` tells SQLite the file cannot
    change so it skips the ``-wal`` entirely: a board whose cards are
    committed but not yet checkpointed (the normal state while a worker or
    the dispatcher holds the connection open) reads back as zero tasks and
    the caller HIDES a board full of real cards. Measured: with a writer
    open on a 3-card board, ``immutable=1`` returns 0 and plain ``mode=ro``
    returns 3. ``mode=ro`` still refuses to create a missing file, so this
    probe cannot itself become a phantom-creator.
    """
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return bool(row) and row[0] == 0


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Enumerate all boards that exist on disk.

    Always includes ``default`` (even when the ``boards/default/``
    metadata dir doesn't exist, because its DB is at the legacy path).
    Other boards are discovered by scanning ``boards/`` for subdirectories
    that either contain a ``kanban.db`` or a ``board.json``.

    Returns a list of metadata dicts, sorted with ``default`` first and
    the rest alphabetically.

    The whole scan runs inside :func:`enumerating_boards`: it resolves a DB path
    per board, which under a ``HERMES_KANBAN_DB`` pin would otherwise trip the
    contradiction guard once for every non-active board on disk.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    with enumerating_boards():
        # Default board is always first.
        entries.append(read_board_metadata(DEFAULT_BOARD))
        seen.add(DEFAULT_BOARD)

        root = boards_root()
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                slug = child.name
                # Keep slug normalisation soft for discovery — but skip dirs
                # that don't parse as valid slugs so we don't surface junk.
                try:
                    normed = _normalize_board_slug(slug)
                except ValueError:
                    continue
                if not normed or normed in seen:
                    continue
                has_db = (child / "kanban.db").exists()
                has_meta = (child / "board.json").exists()
                if not (has_db or has_meta):
                    continue
                # A dir holding a kanban.db but NO board.json was never created
                # through create_board() — it is almost always a phantom: some
                # consumer resolved a stale/typo slug and connect() initialised an
                # empty schema beside the real board. Surfacing it as a board is a
                # silent card-loss path (cards created against the stale slug land
                # in the empty DB, invisible on the real one). Skip it only when it
                # is genuinely empty; a dir with real cards is someone's data and
                # must stay visible even if board.json was lost.
                if has_db and not has_meta and _board_db_is_empty(child / "kanban.db"):
                    # Warn once per process per dir: list_boards() runs on every
                    # watcher/dispatcher tick (~5 s), and an unchanged phantom
                    # repeating forever buried real errors in the gateway log.
                    if str(child) not in _PHANTOM_BOARD_WARNED:
                        _PHANTOM_BOARD_WARNED.add(str(child))
                        _log.warning(
                            "kanban: ignoring phantom board dir %s (kanban.db with 0 "
                            "tasks and no board.json). If this slug was renamed, add "
                            "it to %s; otherwise remove the directory.",
                            child,
                            board_aliases_path(),
                        )
                    continue
                meta = read_board_metadata(normed)
                if meta.get("archived") and not include_archived:
                    continue
                entries.append(meta)
                seen.add(normed)
    # Stamp provenance at the SOURCE. Every entry here was discovered by
    # scanning ``boards/``, never named by a caller, so no consumer of this
    # list should ever trip the pin-contradiction guard — including the ones
    # that skip ``enumerating_each`` and the ones that carry a slug out of this
    # frame into another thread or a later tick.
    return [_mark_board_meta(m) for m in entries]


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Remove or archive a board.

    ``archive=True`` (default) moves the board's directory to
    ``<root>/kanban/boards/_archived/<slug>-<timestamp>/`` so the data
    is recoverable. ``archive=False`` deletes the directory outright.

    **Both** modes are refused while any card on the board is running or
    holds an unexpired claim lock: the board directory contains those
    cards' ``workspaces/``, so either mode takes a live worker's cwd out
    from under it. Both modes also write an audit trail (``ATTEMPT`` then
    ``ARCHIVE`` / ``DELETE`` / ``FAILED``, or ``REFUSED`` for the liveness
    gate) to a log outside the affected tree.

    The ``default`` board cannot be removed — raises :class:`ValueError`.
    Returns a summary dict describing what happened (``{"slug", "action",
    "new_path"}``).
    """
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    _assert_live_board_tree_write_allowed(d)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # A board directory CONTAINS that board's workspaces/ -- retiring it
    # takes every card's scratch dir at once, the same blast radius as the
    # 2026-09-20 incident. BOTH branches do that: archive renames the tree
    # away, delete removes it. Round 3 gated only the delete branch, which
    # is the one a user has to opt into (`boards rm --delete`, dashboard
    # `?delete=true`); the DEFAULT archive branch yanked a running worker's
    # cwd with no refusal and no audit line anywhere (card t_63fb42f9,
    # review round 6, measured). The gate is hoisted here so it covers the
    # default path too.
    #
    # Checked BEFORE the cache invalidation below: it opens the board DB
    # (which would re-populate _INITIALIZED_PATHS) and it can abort, so no
    # state may be torn down ahead of it -- INCLUDING the active-board pin,
    # which used to be cleared above this gate. A refused removal that had
    # already unlinked <root>/kanban/current left get_current_board() falling
    # through to DEFAULT_BOARD, so every later `kanban add` / `list` /
    # `dispatch` silently addressed the default board with nothing saying the
    # pin had moved (FleetReview on PR #785, measured).
    live = _board_has_live_cards(normed)
    if live:
        verb = "archive" if archive else "delete"
        _audit_workspace_deletion(
            d, task_id=live[0], reason="remove_board", outcome=AUDIT_REFUSED,
            detail=f"board-has-live-cards:{','.join(live[:5])} action={verb}",
            board=normed,
        )
        raise ValueError(
            f"board {normed!r} has {len(live)} card(s) running or holding "
            f"a live claim lock ({', '.join(live[:5])}); refusing to "
            f"{verb} its directory, which contains their workspaces. "
            "Wait for the cards to finish, or stop them first."
        )

    # Remember the active pin while the board still exists. It is cleared only
    # after the archive/delete succeeds; either operation has later failure
    # boundaries (rename errors and survivor-held hard deletes) that must leave
    # the operator pointed at the still-present board.
    was_current_board = get_current_board() == normed

    # A concurrent connect(board=normed) after the rename/delete recreates
    # an empty sqlite file via mkdir(exist_ok=True); the cache entry must be
    # dropped first so the schema init pass re-runs on that fresh file.
    db_path = d / "kanban.db"
    _INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Resolver fairness state is process-local except when a live LRU entry
    # has been spilled beside its DB. Retiring the board must remove both so
    # an archived directory does not retain an orphaned swap file.
    _invalidate_pr_state_cache(_pr_state_cache_key(db_path))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        # An archive is a relocation, not an rmtree -- the bytes survive
        # under _archived/ -- but from the point of view of anything holding
        # a path into this board (a worker's cwd, an open workspace) it is
        # indistinguishable from a deletion, which is exactly the symptom
        # the 2026-09-20 incident presented as. Deliverable 2b says log
        # EVERY workspace deletion; round 6 measured `rglob` returning [] on
        # this branch. ATTEMPT goes down before the rename so a process that
        # dies mid-move still names itself, and both records are routed
        # through _durable_audit_log_path, which keeps them OUTSIDE the tree
        # being moved.
        _audit_workspace_deletion(
            d, task_id=None, reason="remove_board", outcome=AUDIT_ATTEMPT,
            detail=f"board={normed} action=archive dest={target}",
            board=normed,
        )
        try:
            d.rename(target)
        except OSError as exc:
            _audit_workspace_deletion(
                d, task_id=None, reason="remove_board", outcome=AUDIT_FAILED,
                detail=f"board={normed} action=archive {type(exc).__name__}: {exc}"[:200],
                board=normed,
            )
            raise
        _audit_workspace_deletion(
            d, task_id=None, reason="remove_board", outcome=AUDIT_ARCHIVE,
            detail=f"board={normed} action=archive dest={target}",
            board=normed,
        )
        if was_current_board:
            clear_current_board()
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    else:
        from hermes_cli.kanban_survivor import remove_workspace_dir
        # Two independent refusals now apply to a board hard-delete: the
        # liveness gate above (no card on this board may be running or
        # claim-locked) and their survivor gate inside remove_workspace_dir
        # (the board may not still hold recoverable work). The audit records
        # the ATTEMPT before the removal and the terminal outcome after, to
        # an explicitly-pinned destination OUTSIDE the board being deleted
        # -- card t_63fb42f9 / incident 2026-09-20. Writing DELETE up front
        # to the board's own logs/ meant a success destroyed its own record
        # and a survivor refusal left a DELETE line for a board that still
        # exists (review round 4, measured).
        _audit_workspace_deletion(
            d, task_id=None, reason="remove_board", outcome=AUDIT_ATTEMPT,
            detail=f"board={normed}", board=normed,
        )
        try:
            removed = bool(remove_workspace_dir(None, None, d, board=True))
        except Exception as exc:
            _audit_workspace_deletion(
                d, task_id=None, reason="remove_board", outcome=AUDIT_REFUSED,
                detail=f"board={normed} {type(exc).__name__}: {exc}"[:200],
                board=normed,
            )
            raise
        if not removed:
            _audit_workspace_deletion(
                d, task_id=None, reason="remove_board", outcome=AUDIT_FAILED,
                detail=f"board={normed} survivor-held-or-removal-failed",
                board=normed,
            )
            raise ValueError(
                f"board {normed!r} could not be deleted; it still holds "
                "recoverable work. Archive it instead."
            )
        _audit_workspace_deletion(
            d, task_id=None, reason="remove_board", outcome=AUDIT_DELETE,
            detail=f"board={normed}", board=normed,
        )
        if was_current_board:
            clear_current_board()
        return {"slug": normed, "action": "deleted", "new_path": ""}


def _board_has_live_cards(slug: str) -> list:
    """Return the ids of cards on *slug* that are running or claim-locked.

    Opens that board's CANONICAL DB path directly rather than going through
    ``connect_closing(board=slug)``: ``kanban_db_path`` gives an ambient
    ``HERMES_KANBAN_DB`` pin precedence even over an explicit board argument,
    and the dispatcher pins that variable into every worker env. Under a pin,
    the board argument was silently ignored, so removing board B inspected
    board A's tasks, concluded B was idle, and archived it out from under a
    live worker -- the primary data-loss guard answering for the wrong
    database (FleetReview on PR #785, measured).

    Fail-closed: if the board's DB cannot be read, return a sentinel so the
    caller refuses rather than deleting a board whose state is unknown.
    """
    try:
        db_path = _board_db_path_ignoring_pin(_normalize_board_slug(slug) or slug)
    except Exception:
        return ["<unreadable-board-db>"]
    if not db_path.is_file():
        # No DB on disk means no rows to be live. A board directory that
        # exists without one is empty as far as cards are concerned.
        return []
    try:
        with connect_closing(db_path=db_path) as conn:
            rows = conn.execute(
                "SELECT id, status, claim_expires FROM tasks "
                "WHERE status = 'running' OR claim_expires IS NOT NULL"
            ).fetchall()
    except Exception:
        return ["<unreadable-board-db>"]
    now = int(time.time())
    live = []
    for row in rows:
        if row["status"] == "running":
            live.append(row["id"])
            continue
        try:
            if row["claim_expires"] and int(row["claim_expires"]) > now:
                live.append(row["id"])
        except (TypeError, ValueError):
            live.append(row["id"])  # fail closed on an unparseable lock
    return live


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Column semantics: see SCHEMA_SQL.
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    skills: Optional[list] = None            # None = defaults only; [] = explicitly none
    model_override: Optional[str] = None
    provider_override: Optional[str] = None  # provider ``model_override`` belongs to
    reasoning_effort: Optional[str] = None   # VALID_REASONING_EFFORTS | "none"; NULL = profile's
    # Deliberate single-sub pin (``--pin-sub "<reason>"``, t_957ca870). Set only when
    # ``provider_override`` is one claude-bpx-N / claude-apx-N sub. ``pin_sub_fallback`` lets a
    # capped pinned sub fall back to its family pool; the default is to WAIT for the sub.
    # Per-card harness brain for a foreign-lane worker (t_a8f335c5), stored in
    # the f lane grammar (normalize_card_brain). NULL = the profile's
    # ``foreign_lane.brain``.
    brain: Optional[str] = None
    pin_sub_reason: Optional[str] = None
    pin_sub_fallback: bool = False
    # ``(model, provider)`` the card ROW pinned when this Task was claimed
    # (:func:`_snapshot_claimed_card_pin`), before a lane override or a capped-pool rung mutates
    # ``model_override`` in memory. In-memory only; ``_default_spawn`` hands it to the worker
    # (t_a30417c3). ``None`` = not a claimed Task (no snapshot taken).
    claimed_card_pin: Optional[tuple] = None
    next_eligible_at: Optional[int] = None
    # Breaker trip count; None -> ``kanban.failure_limit`` -> DEFAULT_FAILURE_LIMIT.
    max_retries: Optional[int] = None
    # ``/goal``-style loop: a judge re-checks each turn IN THE SAME SESSION until
    # done / budget exhausted (-> kanban_block); ``goal_max_turns`` None -> goals default.
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard
    # The ``"unhomed"`` sentinel (:data:`UNHOMED_SESSION`, stamped by :func:`create_task` when a
    # card is born with no session identity) is NEVER surfaced in ``session_id``: it loads as
    # ``session_id=None`` + ``unhomed=True`` so notification/wake consumers keep their
    # pre-sentinel semantics (no phantom wake keyed ``"unhomed"``).
    unhomed: bool = False
    # VALID_BLOCK_KINDS or None (legacy); kept across unblock so a same-kind re-block reads as a loop.
    block_kind: Optional[str] = None
    block_recurrences: int = 0               # unblock-loop counter, see BLOCK_RECURRENCE_LIMIT
    # Operator-only card: never spawned by the dispatcher and never assignable to a worker
    # profile without an explicit ``worker_ok``. See :func:`set_no_worker`.
    no_worker: bool = False
    completion_contract: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        g = lambda col, default=None: _lossy_text(_row_get(row, col, default))  # noqa: E731
        parsed = _json_or(g("skills"))
        session_id = g("session_id")
        unhomed = is_unhomed(session_id)
        skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
        return cls(
            **{col: _lossy_text(row[col]) for col in _TASK_REQUIRED_COLUMNS},
            **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS if col != "session_id"},
            session_id=None if unhomed else session_id,
            unhomed=unhomed,
            **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
            # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
            # reachable on a DB never opened since the rename migration landed.
            consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
            last_failure_error=g("last_failure_error", g("last_spawn_error")),
            skills=skills_value,
            goal_mode=bool(g("goal_mode")),
            block_recurrences=int(g("block_recurrences") or 0),
            pin_sub_reason=g("pin_sub_reason") or None,
            pin_sub_fallback=bool(g("pin_sub_fallback")),
            next_eligible_at=g("next_eligible_at"),
            no_worker=bool(g("no_worker")),
        )

# Columns every schema version has (KeyError if the SELECT omitted them).
_TASK_REQUIRED_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by", "created_at",
    "started_at", "completed_at", "workspace_kind", "workspace_path", "claim_lock", "claim_expires",
)

# Later-added columns read as NULL when absent from the row.
_TASK_OPTIONAL_COLUMNS = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "last_heartbeat_at", "current_run_id", "workflow_template_id",
    "current_step_key", "max_retries", "session_id", "completion_contract",
)

# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
    "brain",
)


@dataclass
class Run:
    """One attempt at a task (``task_runs`` row): opened on claim, closed on
    complete/block/crash/timeout/reclaim; carries the handoff summary."""

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(
            **{
                col: _lossy_text(row[col]) for col in (
                    "task_id", "profile", "step_key", "status", "claim_lock", "claim_expires",
                    "worker_pid", "max_runtime_seconds", "last_heartbeat_at", "outcome", "summary", "error",
                )
            },
            id=int(row["id"]),
            started_at=int(row["started_at"]),
            ended_at=_opt_int(row["ended_at"]),
            metadata=_json_or(_lossy_text(row["metadata"])),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int
    #: Dispatcher run that wrote this comment, when the writer was a worker
    #: scoped to this task. NULL for CLI / dashboard / orchestrator writes and
    #: for every pre-migration row.
    run_id: Optional[int] = None
    #: Bounded, non-reversible fingerprint of the writing session's id — what
    #: makes two concurrent SAME-PROFILE sessions distinguishable. NULL when no
    #: session id was in context, and on every pre-migration row.
    session_ref: Optional[str] = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Comment":
        # Tolerates a pre-migration row shape (``SELECT *`` on an older DB):
        # legacy rows simply carry NULL provenance.
        return cls(
            id=r["id"], task_id=r["task_id"], author=_lossy_text(r["author"]),
            body=_lossy_text(r["body"]), created_at=r["created_at"],
            run_id=_opt_int(_row_get(r, "run_id")),
            session_ref=_row_get(r, "session_ref") or None,
        )


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Attachment":
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=r["created_at"],
        )


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None
    # Session that wrote the event (``task_events.actor_session_id``). The
    # notifier uses it to skip waking the chat whose own session made the
    # transition (t_a4890a77).
    actor_session_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        run_id = _row_get(row, "run_id")
        return cls(
            id=row["id"], task_id=row["task_id"], kind=_lossy_text(row["kind"]),
            payload=_json_or(_lossy_text(row["payload"])), created_at=row["created_at"], run_id=_opt_int(run_id),
            actor_session_id=_row_get(row, "actor_session_id"),
        )

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Restart-stable fingerprint of worker_pid ("<boot/instantiation epoch>|<start time>",
    -- kanban_db_dispatch._process_fingerprint) recorded at spawn: liveness and kills require pid
    -- AND fingerprint to agree, so a PID recycled after a reboot is never read as our worker or
    -- signalled. NULL = legacy row (pre-fingerprint spawn); 'unverified' = capture failed at
    -- spawn (held while live, never signalled). Column keeps its INTEGER affinity for the
    -- start-time-only integer values older rows carry.
    worker_started_at    INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    next_eligible_at     INTEGER,
    -- Per-task reasoning effort for the worker (minimal|low|medium|high|
    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher
    -- passes --reasoning <level> so the worker runs at that depth regardless
    -- of the profile's agent.reasoning_effort. NULL = profile setting.
    reasoning_effort     TEXT,
    -- Per-card harness brain for a foreign-lane worker (t_a8f335c5), f lane
    -- grammar (clrf, clxf-N, dtlrf, ...). NULL = profile foreign_lane.brain.
    brain                TEXT,
    -- Deliberate single-sub pin (t_957ca870): the operator's --pin-sub reason
    -- when provider_override is claude-bpx-N / claude-apx-N. NULL = no pin.
    pin_sub_reason       TEXT,
    -- 1 = a capped/cooling pinned sub may fall back to its family pool
    -- (claude-bpr / claude-apr); 0 (default) = the card waits for the sub.
    pin_sub_fallback     INTEGER NOT NULL DEFAULT 0,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var, and for an id with no ``sessions`` row in this
    -- profile's state.db (kanban_create verifies before stamping). Indexed
    -- so per-session list queries stay cheap on larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    -- Operator-only flag (1 = no worker). The dispatcher never spawns a
    -- flagged card and assign/reassign refuse a worker profile unless the
    -- same call passes worker_ok (which clears the flag). Prose such as
    -- "do not dispatch a worker" in the body is not a guard; this is.
    no_worker            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    -- Edge semantics (VALID_LINK_KINDS). 'blocks' (the default) gates the
    -- child on the parent finishing; 'derived-from' is provenance only and
    -- never gates. Legacy rows are backfilled to 'blocks' by the additive
    -- migration because they were created under gating semantics; every
    -- scheduling query additionally COALESCEs NULL to 'blocks' so a
    -- hand-edited DB can't silently un-gate a real dependency.
    kind       TEXT NOT NULL DEFAULT 'blocks',
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    -- Per-run / per-session provenance. Both NULL on legacy rows and on any
    -- write with no trusted runtime context, which renders as an explicit
    -- "unknown" (see ``format_comment_author``) rather than a bare author that
    -- reads as attributed.
    run_id      INTEGER,
    session_ref TEXT,
    created_at  INTEGER NOT NULL
);

-- Retained across config rollback so old volatile paths stay fenced.
CREATE TABLE IF NOT EXISTS workspace_mount_roots (
    root       TEXT PRIMARY KEY,
    mount_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL,
    actor_profile    TEXT,
    actor_session_id TEXT
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    -- Spawn-time start fingerprint of worker_pid (see tasks.worker_started_at). Retained with
    -- worker_pid after the run ends so a worker that outlives its terminal transition can
    -- still be found and reaped; NULL = legacy row, never signalled.
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_workspace_survivors (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    bases TEXT NOT NULL DEFAULT '{}',
    held_reason TEXT,
    survivor TEXT
);

CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    -- Canonical participant on platforms that key sessions on a STABLE
    -- alternate id (feishu union_id, signal uuid, dingtalk staff_id).
    -- ``build_session_key`` keys the participant segment on
    -- ``user_id_alt or user_id``, so a row that stores only ``user_id``
    -- rebuilds a DIFFERENT key than the creator's on those platforms.
    user_id_alt   TEXT,
    chat_type     TEXT,
    -- Platform-neutral scope discriminator (Slack workspace/team id).
    -- ``build_session_key`` inserts it BEFORE chat_id on both the DM and
    -- group branches for Slack, so a row without it drops that segment.
    scope_id      TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- Board-level, time-boxed model routing. A row here re-routes every spawn
-- for its lane that does NOT carry its own per-card override, and expires on
-- the dispatcher clock (``expires_at``) so a capacity workaround cannot
-- become a standing default the way editing a profile's config.yaml does.
-- ``assignee IS NULL`` is the board-wide lane; a row naming an assignee wins
-- over it for that profile. One active row per lane (PRIMARY KEY collapses
-- re-sets to an upsert), so ``lane-model set`` is idempotent.
CREATE TABLE IF NOT EXISTS lane_model_overrides (
    assignee     TEXT PRIMARY KEY,   -- '' == board-wide lane (NULL can't be a PK)
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    reasoning_effort TEXT,
    reason       TEXT,
    firepower    TEXT,               -- justification when the model is flagship-class
    pin_sub_reason TEXT,             -- --pin-sub reason when provider is claude-bpx-N/apx-N
    created_by   TEXT,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lane_model_expires ON lane_model_overrides(expires_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
"""


def _read_dispatch_lock_holder(db_path: Path) -> dict:
    """Best-effort contention snapshot, never evidence that a lock is held.

    Read only after a failed acquire. An older dispatcher, a racing release,
    or a partial write can leave no usable stamp; report unknown in that case.
    Byte zero is reserved for the Windows byte-range lock.
    """
    try:
        with db_path.with_name(db_path.name + ".dispatch.lock").open("rb") as handle:
            handle.seek(1)
            stamp = json.loads(handle.read(4096))
        if not isinstance(stamp, dict):
            return {}
        pid, started, site = stamp["pid"], stamp["monotonic"], stamp["acquire_site"]
        if type(pid) is not int or pid <= 0 or type(started) not in (int, float):
            return {}
        age = time.monotonic() - started
        if not 0 <= age < float("inf") or not isinstance(site, str):
            return {}
        return {"pid": pid, "age_seconds": age, "acquire_site": site}
    except (OSError, ValueError, KeyError, OverflowError):
        return {}


def format_dispatch_lock_skip(holder: dict) -> str:
    """Shared human-readable board-lock skip diagnostic for CLI and logs."""
    age = holder.get("age_seconds")
    age_text = f"{age:.1f}s" if age is not None else "unknown"
    return (
        f"skipped: board dispatcher lock held by pid {holder.get('pid', 'unknown')} "
        f"for {age_text}; acquire site={holder.get('acquire_site', 'unknown')}"
    )


class KanbanNonCanonicalBoardPathError(RuntimeError):
    """Raised when :func:`connect` is handed ``<...>/kanban/boards/default/kanban.db``.

    The ``default`` board's DB is ``<root>/kanban.db``; ``boards/default/`` is
    never a board. ``connect()`` creates and initializes whatever it is given,
    so a guessed path there minted a full-schema, zero-card phantom board
    (2026-10-02 09:02:00, a cron agent's ``kb.connect(<root>/kanban/boards/
    default/kanban.db)`` one-liner; card t_1462ab0d). Earlier shapes left a
    0-byte file (2026-09-18, 09-27, 09-29). Refusing before the mkdir means the
    wrong guess costs one traceback instead of a phantom that enumerators trip on.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        super().__init__(
            f"{db_path} is not a kanban board path: the default board lives at "
            f"<root>/kanban.db (kanban_db_path('default')). Refusing to create "
            f"or open a phantom board here."
        )


def _refuse_noncanonical_board_path(path: Path) -> None:
    """Raise :class:`KanbanNonCanonicalBoardPathError` for ``kanban/boards/default/kanban.db``.

    Structural, so it holds under any root, pin or sandbox. Refused when ANY
    of three views of the path is the phantom:

    * the LOGICAL spelling (absolute + ``..``-collapsed, symlinks kept): a
      symlinked ``kanban/`` or ``boards/`` ancestor relocates storage but does
      not make ``boards/default/kanban.db`` a board (Prism f836c7bad244);
    * the RESOLVED target: a relative ``kanban.db`` from a ``boards/default``
      cwd or a symlinked parent dir lands on the same file (Prism d0894d0cbe77);
    * the resolved target of this home's ``board_dir("default")/kanban.db``,
      for a caller that hands over the already-resolved path behind a
      symlinked ancestor, whose names no longer spell ``kanban/boards``.
    """
    raw = Path(path).expanduser()
    resolved = raw.resolve(strict=False)
    for p in (Path(os.path.normpath(os.path.abspath(raw))), resolved):
        if (
            p.name == "kanban.db"
            and p.parent.name == DEFAULT_BOARD
            and p.parent.parent.name == "boards"
            and p.parent.parent.parent.name == "kanban"
        ):
            raise KanbanNonCanonicalBoardPathError(raw)
    if resolved.name != "kanban.db":
        return
    try:
        phantom = (kanban_home() / "kanban" / "boards" / DEFAULT_BOARD / "kanban.db").resolve(strict=False)
    except Exception:  # best-effort third view; the two above already ran
        return
    if resolved == phantom:
        raise KanbanNonCanonicalBoardPathError(raw)


class KanbanDbNotABoardError(RuntimeError):
    """Raised when a path opens as SQLite but holds no kanban board.

    ``sqlite3.connect()`` — including ``mode=ro`` on an **existing**
    zero-byte file — succeeds silently and reports an empty
    ``sqlite_master``. A read-only caller then sees ``0 tasks`` and
    concludes "this board is empty" when the truth is "I opened the wrong
    file". That fail-OPEN turned a mistyped path into a wrong answer that
    looks like a right one.

    Read-only probes raise this instead. It is deliberately **not** raised
    by :func:`connect`, which legitimately creates and initializes a fresh
    board. Never auto-create the schema in an unexpected empty file to
    "resolve" this — that manufactures a real-looking empty board and makes
    the confusion permanent.
    """

    def __init__(self, db_path: Path, *, hint: Optional[str] = None):
        self.db_path = db_path
        self.hint = hint
        try:
            size = db_path.stat().st_size
            size_str = f"{size} bytes"
        except OSError:
            size_str = "unreadable"
        message = (
            f"{db_path} is not a kanban board: it opened as SQLite but has no "
            f"'tasks' table ({size_str}). This is the zero-byte fail-open — a "
            f"reader would otherwise see an EMPTY board instead of an error."
        )
        if hint:
            message += f" Did you mean: {hint}?"
        super().__init__(message)


def _canonical_db_path_hint(db_path: Path) -> Optional[str]:
    """Best-effort "did you mean" for a path that is not a board DB.

    Only the two shapes that are actually canonical are suggested, and only
    when the suggested file exists and is non-empty:

    * ``<...>/boards/<slug>.db``  → ``<...>/boards/<slug>/kanban.db``
    * ``<...>/boards/<slug>/board.db`` (or any other name in a board dir)
      → ``<...>/boards/<slug>/kanban.db``

    Returns ``None`` when nothing better can be pointed at — a wrong guess
    is worse than no guess.
    """
    try:
        candidates: list[Path] = []
        parent = db_path.parent
        # boards/<slug>.db → boards/<slug>/kanban.db
        if parent.name == "boards" and db_path.name != "kanban.db":
            candidates.append(parent / db_path.stem / "kanban.db")
        # boards/<slug>/<other>.db → boards/<slug>/kanban.db
        if parent.parent.name == "boards" and db_path.name != "kanban.db":
            candidates.append(parent / "kanban.db")
        for candidate in candidates:
            if candidate != db_path and candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate)
    except OSError:
        return None
    return None


def assert_is_board_db(db_path: Path, conn: sqlite3.Connection) -> None:
    """Raise :class:`KanbanDbNotABoardError` unless ``conn`` holds a board.

    The check is the presence of the ``tasks`` table — the one table every
    kanban board has had since the schema existed, so this stays true for
    legacy boards that predate later tables. Call it on **read-only**
    opens, right after connecting and before running any query whose empty
    result would be mistaken for an empty board.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if row is None:
        raise KanbanDbNotABoardError(
            db_path, hint=_canonical_db_path_hint(db_path)
        )


class LiveBoardWriteRefused(RuntimeError):
    """Raised when a test/probe process tries to open the PRODUCTION board rw.

    See :func:`_assert_live_board_write_allowed`.
    """


def _production_kanban_roots() -> list[Path]:
    """The Hermes roots whose ``kanban.db`` is the LIVE board.

    Delegates to :func:`hermes_state._production_state_roots` rather than
    computing its own answer. That list is the fleet's single definition of
    "this is production": the platform-default root resolved WITHOUT
    ``Path.home()`` / ``hermes_constants`` (both of which tests monkeypatch),
    plus ``_STATE_DB_GUARD_EXTRA_DENY_ROOTS``, into which ``tests/conftest.py``
    injects the pre-sandbox production root so custom-``HERMES_HOME``
    deployments are covered too.

    Sharing it is the point. ``state.db`` and ``kanban.db`` are the same class
    of live store reached the same way, and the 2026-07-24 state.db incident
    and the 2026-08-08 / 2026-09-21 kanban incidents are the same bug. A second
    root definition here would be free to drift from the one the rest of the
    guard class uses, which is how the first member got fixed while this one
    kept leaking.
    """
    from hermes_state import _production_state_roots
    return list(_production_state_roots())


def _in_test_context() -> bool:
    """True when this process is a test run, by environment OR by ancestry.

    Re-exported from the leaf module ``hermes_test_context`` — the same single
    definition ``hermes_state``'s guard uses. Deliberately NOT a local
    ``PYTEST_CURRENT_TEST`` check: that misses a child spawned with a rebuilt
    environment, which loses ``PYTEST_*`` and ``HERMES_HOME`` together and is
    precisely the state in which it writes to production (#82770).
    """
    from hermes_test_context import _in_test_context as _impl
    return _impl()


def _is_production_board_db(resolved: Path, root: Path) -> bool:
    """True when *resolved* is a LIVE board DB of the production root *root*.

    Mirrors :func:`hermes_state._is_production_state_db` and covers the two
    on-disk board layouts :func:`_board_db_path_ignoring_pin` produces:

    * ``<root>/kanban.db`` — the ``default`` board (back-compat path);
    * ``<root>/kanban/boards/<slug>/kanban.db`` — every named board.

    Deliberately narrow. Anything deeper or elsewhere under the root is NOT a
    board — notably ``~/.hermes/hermes-agent/...`` worktrees and
    ``~/.hermes/kanban/workspaces/<task>/...`` scratch dirs, where hermetic
    tests and workers legitimately create throwaway DBs. A containment-only
    check (``is_relative_to(root)``) would refuse all of those.
    """
    if resolved == root / "kanban.db":
        return True
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    return (
        len(parts) == 4
        and parts[0] == "kanban"
        and parts[1] == "boards"
        and parts[3] == "kanban.db"
    )


def _assert_live_board_write_allowed(path: Path) -> None:
    """Refuse a READ-WRITE open of the LIVE board by a test/probe process.

    The structural half of the 2026-08-08 / 2026-09-21 incidents. Until now the
    only thing standing between a fixture card and the live board was
    ``tests/conftest.py``'s env scrub, which is PATH-SCOPED: it loads when
    pytest collects a file under ``tests/``, so a probe script sitting anywhere
    else keeps the dispatcher-injected ``HERMES_KANBAN_DB`` pin and writes to
    production. On 2026-09-21 that put three fixture cards on the live board and
    burned three real worker runs against them.

    A doc line and an opt-in flag cannot fix that — they require the probe's
    author to remember. This gate sits at the ``connect()`` choke point instead,
    so it covers every entry path regardless of where the ``.py`` file lives.

    This is deliberately the SAME guard ``state.db`` has carried since the
    2026-07-24 WAL incident (:func:`hermes_state._ensure_test_isolation`): same
    production-root list, same test-context predicate, same fail-before-open
    placement. Two stores, one class, one definition.

    Two independent refusals, each covering a leak shape the other misses:

    * **R1 (test context).** The process is a test run — by env *or* by process
      ancestry (:func:`hermes_test_context._in_test_context`) — and is opening a
      live board. This is the 16:05 shape: a probe under pytest from outside
      ``tests/``, inheriting the worker's pin, ``HERMES_HOME`` still the real
      profile. R2 cannot see it; nothing was redirected.
    * **R2 (redirected home).** ``HERMES_HOME`` declares a root that is NOT a
      production root — the caller sandboxed its Hermes state — yet kanban
      resolved to a live board anyway, because a ``HERMES_KANBAN_*`` pin
      outranks ``HERMES_HOME``. This is the 16:30 shape: bare
      ``python probe.py`` under a throwaway probe home with no pytest marker at
      all, so R1 cannot see it. This is the exact contradiction
      :func:`_warn_if_override_escapes_hermes_home` has only ever WARNED about.

    Neither condition can hold for a production writer. The fleet runs with
    ``HERMES_HOME`` unset, ``=~/.hermes``, or ``=~/.hermes/profiles/<name>``,
    all of which resolve ``kanban_home()`` to the production root (R2 false),
    and no fleet component is a test context (R1 false). The gate is inert in
    production and costs one ``Path.resolve()``.

    A deliberate operator pin to a board outside every production root — the
    documented ``HERMES_KANBAN_DB`` use — is untouched.
    """
    try:
        target = path.expanduser().resolve(strict=False)
    except OSError:  # pragma: no cover - resolution failure is not a leak
        return
    live_root: Optional[Path] = None
    for root in _production_kanban_roots():
        if _is_production_board_db(target, root):
            live_root = root
            break
    if live_root is None:
        return  # not a live board — nothing this guard is about.
    _refuse_live_kanban_write(target, live_root, "open the live board read-write")


def _refuse_live_kanban_write(target: Path, live_root: Path, action: str) -> None:
    """Shared R1/R2 decision for :func:`_assert_live_board_write_allowed` and
    :func:`_assert_live_board_tree_write_allowed`; raises or returns."""
    reason: Optional[str] = None
    if _in_test_context():
        reason = (
            f"this process is a TEST context and {target} is the LIVE board "
            f"(under real Hermes root {live_root})"
        )
    else:
        declared = os.environ.get("HERMES_HOME", "").strip()
        if declared:
            try:
                declared_root = kanban_home().expanduser().resolve(strict=False)
            except OSError:  # pragma: no cover - diagnostic only
                declared_root = live_root
            if declared_root not in {
                r for r in _production_kanban_roots()
            }:
                reason = (
                    f"HERMES_HOME={declared} declares the kanban root "
                    f"{declared_root}, but a HERMES_KANBAN_* path pin "
                    f"outranked it and resolved to {target} — the LIVE board "
                    f"under {live_root}"
                )
    if reason is None:
        return
    pins = ", ".join(
        f"{k}={os.environ[k]}"
        for k in (*_KANBAN_PATH_PIN_ENV_VARS, "HERMES_KANBAN_BOARD")
        if os.environ.get(k, "").strip()
    ) or "<none>"
    raise LiveBoardWriteRefused(
        f"kanban live-system guard: refusing to {action} — {reason}. Writes from here create REAL cards that the "
        f"dispatcher claims and spawns real workers against (3 fixture cards + "
        f"3 burned runs on 2026-09-21). Active path pins: {pins}. To run "
        f"against a throwaway board: HERMES_KANBAN_SANDBOX=1 "
        f"HERMES_HOME=$(mktemp -d) — the flag neutralises every "
        f"HERMES_KANBAN_* pin so kanban resolves under your temp home. "
        f"Read-only inspection of the live board is still allowed via "
        f"connect_readonly()."
    )


def _is_live_board_tree_path(resolved: Path, root: Path) -> bool:
    """True for board-set state of *root* that is not a DB: the ``current``
    pointer, ``board-aliases.json`` and anything under ``kanban/boards/``."""
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) < 2 or parts[0] != "kanban":
        return False
    return parts[1] == "boards" or (len(parts) == 2 and parts[1] in ("current", "board-aliases.json"))


def _assert_live_board_tree_write_allowed(path: Path) -> None:
    """Refuse a test/probe process creating, renaming or re-pointing LIVE boards.

    ``connect()``'s guard only sees ``kanban.db``. ``create_board()`` writes
    ``board.json`` (and mkdirs the board dir) BEFORE it reaches ``connect()``,
    so a refused create still left a board that ``list_boards()`` surfaces;
    ``set_current_board()`` / ``remove_board()`` never touch the DB at all.
    That is how ten fixture boards (alpha, curr, spawntest, slug-immutable, …)
    landed in the live ``kanban/boards/`` on 2026-09-22 (card t_216b74e0).
    Same production-root list and R1/R2 predicate as the DB guard.
    """
    try:
        target = path.expanduser().resolve(strict=False)
    except OSError:  # pragma: no cover - resolution failure is not a leak
        return
    for root in _production_kanban_roots():
        if _is_live_board_tree_path(target, root):
            _refuse_live_kanban_write(target, root, f"write live board state {target}")
            return


def connect_readonly(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open an existing kanban board read-only, failing LOUD on non-boards.

    The safe counterpart to :func:`connect` for readers (diagnostics,
    audits, dashboards, ad-hoc queries). Unlike a bare
    ``sqlite3.connect('file:...?mode=ro', uri=True)``:

    * a MISSING file raises ``FileNotFoundError`` naming the path, instead
      of sqlite's opaque ``unable to open database file``;
    * a file that opens but has no ``tasks`` table raises
      :class:`KanbanDbNotABoardError` — this is the zero-byte fail-open
      the function exists to close.

    Never creates a file, never writes schema, never migrates.
    """
    path = db_path if db_path is not None else kanban_db_path(board=board)
    if not path.exists():
        raise FileNotFoundError(
            f"no kanban DB at {path} — nothing to read. Boards live at "
            f"<root>/kanban/boards/<slug>/kanban.db (or <root>/kanban.db "
            f"for the '{DEFAULT_BOARD}' board)."
        )
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert_is_board_db(path, conn)
    except Exception:
        conn.close()
        raise
    return conn


def _discard_stub_db(path: Path) -> None:
    """Remove a just-created ``.db`` that never got its schema.

    ``sqlite3.connect()`` creates the file the moment it opens, before any
    of the WAL/pragma/schema work runs. When that work then raises, the
    caller sees the error — but a **zero-byte .db is left on disk**, and
    every later read-only reader opens it happily and reports an EMPTY
    board rather than an error. That is how 14 decoy stubs accumulated
    under one fleet's ``~/.hermes/kanban``.

    Only ever called on the failure path for a file this ``connect()``
    created, and only when the file is still empty — a non-empty file is
    somebody's data and is never touched. Best-effort by design: cleanup
    must not mask the original init error the caller is about to see.
    """
    for candidate in (path, path.with_name(path.name + "-wal"),
                      path.with_name(path.name + "-shm")):
        try:
            if candidate.is_file() and candidate.stat().st_size == 0:
                candidate.unlink()
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Status-change audit (Q15 option B, harness-parity spec 4.7)
# ---------------------------------------------------------------------------
# A raw ``UPDATE tasks`` writes no task_events row, so a same-uid process can
# move a card to 'done' and back to 'running' and leave a clean row. This
# persistent trigger mirrors every change of status / current_run_id /
# worker_pid into an append-only audit table, whoever the writer is.
#
# Attribution marker: the persistent trigger always writes source='unknown'.
# Every read-write kanban_db connection installs a per-connection TEMP trigger
# that restamps the row it just caused as 'kanban_db:<pid>'. TEMP objects live
# only in the connection that created them, so a kanban_db connection being
# open elsewhere never stamps a raw client's write (sqlite3 CLI, python
# sqlite3, a script). Forging the marker needs a writer that knows this schema
# and deliberately recreates the temp trigger or edits the audit table; the
# naive restore (arm A6-6) cannot produce it. Same uid, so this is a tripwire,
# not a wall (Q15 option C is the wall).
_STATUS_AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS task_status_audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    old_status     TEXT,
    new_status     TEXT,
    old_run_id     INTEGER,
    new_run_id     INTEGER,
    old_worker_pid INTEGER,
    new_worker_pid INTEGER,
    changed_at     INTEGER NOT NULL,
    source         TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_status_audit_task    ON task_status_audit(task_id, id);
CREATE INDEX IF NOT EXISTS idx_status_audit_changed ON task_status_audit(changed_at);
CREATE TRIGGER IF NOT EXISTS trg_tasks_status_audit
AFTER UPDATE OF status, current_run_id, worker_pid ON tasks
WHEN OLD.status IS NOT NEW.status
  OR OLD.current_run_id IS NOT NEW.current_run_id
  OR OLD.worker_pid IS NOT NEW.worker_pid
BEGIN
    INSERT INTO task_status_audit (
        task_id, old_status, new_status, old_run_id, new_run_id,
        old_worker_pid, new_worker_pid, changed_at, source
    ) VALUES (
        NEW.id, OLD.status, NEW.status, OLD.current_run_id, NEW.current_run_id,
        OLD.worker_pid, NEW.worker_pid, CAST(strftime('%s', 'now') AS INTEGER),
        'unknown'
    );
END;
"""


def _ensure_status_audit(conn: sqlite3.Connection) -> None:
    """Create the audit table + trigger. Runs after the additive column
    migrations so every column the trigger names exists on legacy DBs."""
    conn.executescript(_STATUS_AUDIT_SQL)


def _install_status_audit_marker(conn: sqlite3.Connection) -> None:
    """Per-connection TEMP trigger: stamp audit rows this connection causes."""
    pid = int(os.getpid())
    conn.execute(
        "CREATE TEMP TRIGGER IF NOT EXISTS kanban_status_audit_source "
        "AFTER INSERT ON main.task_status_audit WHEN NEW.source = 'unknown' "
        f"BEGIN UPDATE task_status_audit SET source = 'kanban_db:{pid}' "
        "WHERE id = NEW.id; END"
    )
NO_WORKER_BODY_RE = re.compile(r"do not dispatch a worker", re.IGNORECASE)
"""Body prose that marked a card operator-only before the ``no_worker``
column existed. Used once, by the migration that adds the column."""


def _backfill_no_worker_from_body(conn: sqlite3.Connection) -> list[str]:
    """Flag legacy cards whose body says "do not dispatch a worker".

    Runs only in the migration pass that adds the column, so a later operator
    ``--worker-ok`` on such a card is never undone by a re-migration. Returns
    and logs the flagged ids.
    """
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id, body FROM tasks WHERE body IS NOT NULL AND no_worker = 0"
        )
        if NO_WORKER_BODY_RE.search(row["body"] or "")
    ]
    for tid in ids:
        conn.execute("UPDATE tasks SET no_worker = 1 WHERE id = ?", (tid,))
    if ids:
        _log.warning(
            "kanban migrate: flagged %d card(s) no_worker from body text: %s",
            len(ids), ", ".join(ids),
        )
    return ids

# Set by ``_home_session_guarded`` for the duration of one guarded mutator:
# the first OUTERMOST write transaction the mutator opens re-runs the
# home-session check under its write lock, before any write.
_PENDING_HOME_CHECK: ContextVar[Optional[dict]] = ContextVar(
    "kanban_pending_home_check", default=None
)
_GUARDED_COLUMNS_SQL = (
    "SELECT status, assignee, priority, session_id FROM tasks WHERE id = ?"
)


def _recheck_pending_home_session(conn: sqlite3.Connection) -> Optional[tuple]:
    """Re-run the pending home check under the write lock. Returns the target's
    guarded columns as of txn start when a foreign override is pending (the
    baseline :func:`_record_pending_foreign_action` compares against)."""
    pending = _PENDING_HOME_CHECK.get()
    if pending is None or pending["done"]:
        return None
    pending["done"] = True
    token = _MUTATION_ACTOR.set(pending["actor"])
    try:
        pending["result"] = check_home_session(
            conn, pending["task_id"], pending["action"]
        )
    finally:
        _MUTATION_ACTOR.reset(token)
    if pending["result"] is None:
        return None
    pending["home_before"] = _read_home_session(conn, pending["task_id"])
    row = conn.execute(_GUARDED_COLUMNS_SQL, (pending["task_id"],)).fetchone()
    return tuple(row) if row is not None else ()


def _record_pending_foreign_action(conn: sqlite3.Connection, guarded_before: tuple) -> None:
    """Write a guarded mutator's takeover/override audit in the SAME
    transaction as the mutation (C5 #23, PR #951 review): a crash or a
    concurrent reader between two commits can no longer see the foreign
    mutation without its takeover event and comment.

    Only when this transaction changed the target card's guarded columns
    (status/assignee/priority/session_id): that is the foreign mutation
    itself, so the audit is owed whatever the mutator later returns. A txn
    that only wrote side rows (a ``reclaim_refused`` event, a claim-expiry
    bump) records nothing here; the wrapper then falls back to the
    post-commit record gated on ``_mutation_succeeded(result)``."""
    pending = _PENDING_HOME_CHECK.get()
    if pending is None or pending.get("recorded") or pending["result"] is None:
        return
    row = conn.execute(_GUARDED_COLUMNS_SQL, (pending["task_id"],)).fetchone()
    if row is None or tuple(row) == guarded_before:
        return
    pending["recorded"] = True
    pending["new_home"] = record_foreign_action(
        conn, pending["task_id"], pending["action"], pending["result"],
        home_before=pending["home_before"], subscribe=False,
    )


def _sync_home_index(conn: sqlite3.Connection) -> None:
    try:
        from hermes_cli import kanban_home_index

        kanban_home_index.sync_after_commit(conn)
    except Exception:  # pragma: no cover - the index is a mirror, never a gate
        pass


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------
def _new_task_id() -> str:
    """``t_`` + 4 hex bytes (collision ~1e-3 at 100k tasks; 2 bytes would hit 50%
    by 10k). Idempotency belongs to ``idempotency_key``, not id uniqueness."""
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _host_prefix() -> str:
    """``"<host>:"`` prefix shared by every claim lock issued from this host."""
    return f"{_claimer_id().split(':', 1)[0]}:"


# --- Task creation / mutation ---
def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip both; a provider without a model is rejected (a bare ``--provider``
    would re-resolve the profile's model against another backend — exactly
    the mismatch the override exists to kill)."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------
def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


def _resolve_stored_model_pair(
    model: Optional[str], provider: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a config `model.aliases` key / `provider/model` pair at WRITE time.

    Every kanban model-override write funnels through here so a card can
    never persist a raw alias (``grok``). The dispatcher passes
    ``model_override`` to the worker as ``-m <model>``; an unresolved alias
    reaches the worker's configured provider, 400s, and the fallback chain
    silently serves a different provider AND model (measured 2026-09-18).

    Resolving at write time also PINS the card: retargeting
    ``model.aliases.grok`` later cannot change what an already-created card
    runs. Best-effort — anything unresolvable is stored verbatim, preserving
    the previous literal-storage behaviour, and resolution failure never
    blocks a board write.
    """
    if not model:
        return model, provider
    try:
        from hermes_cli.model_switch import resolve_model_pair_for_storage

        return resolve_model_pair_for_storage(model, provider)
    except Exception:  # pragma: no cover - a board write must never fail here
        _log.debug("kanban model alias resolution failed for %r", model, exc_info=True)
        return model, provider

# ---------------------------------------------------------------------------
# Home-session card ownership (foreign-session mutation guard)
# ---------------------------------------------------------------------------
#
# ``tasks.session_id`` is a card's HOME session: the chat/agent session that
# created it. A session acting from a chat may freely READ and COMMENT on any
# card, but a status/ownership mutation of a card born in ANOTHER session is
# refused unless the caller passes an explicit override reason, which is then
# recorded as a comment the home session will see.
#
# The guard is keyed on an explicit caller context (:func:`mutation_actor`),
# set once by each chat-facing surface (the ``hermes kanban`` CLI entry and
# the ``kanban_*`` tool registration). When no actor is bound — the
# dispatcher, reapers, the PR gate, direct library/test callers — the guard
# is inert: it polices humans-and-agents acting from a chat, never the
# execution lane. The body of a guarded mutator runs with the actor cleared,
# so internal cascades (a completion promoting children, a reassign that
# reclaims first) are never re-checked against other cards.


# Home stamp for a card born with NO session identity (cron, launchd, a plain
# shell, a script calling the library). Never NULL: an unstamped card has no
# home, so every session felt entitled to it (58/144 open cards, 2026-09-23).
# ``unhomed`` is foreign to EVERY session -- a chat must take it over
# explicitly (``--takeover``); the execution lane (no bound actor, the
# assignee, the dispatched worker) is unaffected.
UNHOMED_SESSION = "unhomed"


def is_unhomed(session_id: Any) -> bool:
    return str(session_id or "").strip() == UNHOMED_SESSION

# Fleet OPERATOR pseudo-session (t_09fea045). A card minted by a cron/script
# that has no chat session of its own is homed here instead of ``unhomed``:
# ``unhomed`` is foreign to EVERY session, so such cards (review-merge-pass,
# main-red-watch, horizon-watch rebase cards) sat Stuck with no session able
# to unblock/complete them. Any session of an operator profile
# (:data:`OPERATOR_PROFILES`) owns an operator-homed card; every other
# session still sees it as foreign.
OPERATOR_HOME_PREFIX = "operator:"
OPERATOR_HOME_SESSION = OPERATOR_HOME_PREFIX + "apollo"


def is_operator_home(session_id: Any) -> bool:
    sid = str(session_id or "").strip()
    return sid.startswith(OPERATOR_HOME_PREFIX) and len(sid) > len(OPERATOR_HOME_PREFIX)


class UnhomedCreateError(ValueError):
    """``create`` would mint a card no session can drive (``unhomed``)."""

# Who is minting a card (t_6281f908, Ace 2026-10-03 14:42 option A, D-O1/D-O2).
# Automated minters (a dispatched worker, a cron script, the gateway's
# in-process ``/kanban``) with no resolvable home are REFUSED unless they say
# ``--unhomed`` / ``--session``; a hand-typed CLI/TUI create only WARNS.
# ``script`` = no marker and no TTY (launchd/systemd openers): refused, as
# since t_09fea045.
CREATE_ORIGIN_WORKER = "worker"
CREATE_ORIGIN_CRON = "cron"
CREATE_ORIGIN_GATEWAY = "gateway"
CREATE_ORIGIN_HAND = "hand"
CREATE_ORIGIN_SCRIPT = "script"
# Set by cron/scheduler_script.py on every script-job child (HERMES_CRON_SCRIPT
# is also set there when the gh shim is installed).
CRON_JOB_ID_ENV = "HERMES_CRON_JOB_ID"
# Set by ``kanban.run_slash`` for a ``/kanban`` typed in the CLI or TUI (never
# the gateway, which is classified first). Not ``HERMES_INTERACTIVE``: agent
# subprocesses (-q runs, workers) inherit that env flag.
HAND_TYPED_SLASH: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "kanban_hand_typed_slash", default=False
)


def classify_create_origin() -> str:
    """The minter class of THIS process: worker > cron > gateway > hand > script."""
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return CREATE_ORIGIN_WORKER
    if any((os.environ.get(v) or "").strip() for v in (CRON_JOB_ID_ENV, "HERMES_CRON_SCRIPT")):
        return CREATE_ORIGIN_CRON
    if _process_is_gateway():
        return CREATE_ORIGIN_GATEWAY
    if HAND_TYPED_SLASH.get():
        return CREATE_ORIGIN_HAND  # a typed CLI/TUI ``/kanban`` (the TUI slash worker's stdin is a pipe)
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return CREATE_ORIGIN_HAND
    except (ValueError, OSError):
        pass
    return CREATE_ORIGIN_SCRIPT


def _minted_by(origin: str) -> dict:
    """``created`` event provenance the orphan watch infers a home from."""
    out: dict = {"class": origin}
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    job = (os.environ.get(CRON_JOB_ID_ENV) or "").strip()
    script = (os.environ.get("HERMES_CRON_SCRIPT") or "").strip()
    if task:
        out["task"] = task
    if run.isdigit():
        out["run_id"] = int(run)
    if job:
        out["cron_job"] = job
    if script:
        out["cron_script"] = os.path.basename(script)
    return out


# Suite-compat escape for the require_home refusal: the test suite's hermetic
# environment sets it so the hundreds of fixture ``kanban create`` calls that
# predate the refusal keep working; the refusal's own tests unset it. Not a
# user-facing knob -- production callers pass ``--home operator`` /
# ``--session <sid>`` instead (t_09fea045).
ALLOW_UNHOMED_CREATE_ENV = "HERMES_KANBAN_ALLOW_UNHOMED_CREATE"


def _ambient_session_env(name: str) -> str:
    """Per-session gateway context (ContextVar-first), env outside it."""
    try:
        from gateway.session_context import get_session_env

        return (get_session_env(name, "") or "").strip()
    except Exception:
        return (os.environ.get(name) or "").strip()


def format_origin_line(
    session_id: Optional[str],
    *,
    created_by: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    """``origin: <platform> <chat_name> (<chat_id>) · session <id> · <date>``.

    The human-readable birth certificate prepended to a card body, so any
    session reading the card sees whose it is without a DB lookup.
    """
    date = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    if is_operator_home(session_id):
        who = f" · by {created_by}" if created_by else ""
        return (
            f"origin: operator ({str(session_id).strip()}: cron/script, any "
            f"operator-profile session may act){who} · {date}"
        )
    if not session_id or is_unhomed(session_id):
        who = f" · by {created_by}" if created_by else ""
        return (
            "origin: unhomed (no session identity: cron/script/shell)"
            f"{who} · {date}"
        )
    platform = _ambient_session_env("HERMES_SESSION_PLATFORM") or "cli"
    chat_name = _ambient_session_env("HERMES_SESSION_CHAT_NAME")
    chat_id = _ambient_session_env("HERMES_SESSION_CHAT_ID")
    where = platform
    if chat_name:
        where += f" {chat_name}"
    if chat_id:
        where += f" ({chat_id})"
    return f"origin: {where} · session {session_id} · {date}"


def body_has_origin(body: Optional[str]) -> bool:
    return any(
        line.strip().lower().startswith("origin:")
        for line in (body or "").splitlines()
    )


def stamp_origin_body(body: Optional[str], origin_line: str) -> str:
    """Prepend ``origin_line`` unless the body already carries one."""
    if body_has_origin(body):
        return body or ""
    rest = (body or "").strip("\n")
    return f"{origin_line}\n\n{rest}" if rest.strip() else origin_line


def _resolve_birth_session(
    conn: sqlite3.Connection,
    session_id: Optional[str],
    parents: Iterable[str],
    *,
    explicit: bool = False,
    creator_task_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """THE home a new card is born with, plus the ``origin:`` line it inherits.

    0. ``explicit`` (the caller NAMED the session, e.g. ``create --session``):
       that session wins over every parent.
    1. The first homed parent, then the ``creator_task_id`` (durable lineage
       without a dependency edge), then the card the creating kanban worker run
       was dispatched for: fan-out belongs to the HUMAN home of its lineage.
       A parent's CURRENT home wins over a defaulted ``session_id`` -- so after
       a ``--takeover`` re-home, children follow the new home.
    2. Inside a worker run with no homed lineage: ``unhomed`` -- never the
       run's own per-run session id, which no human session reads.
    3. Otherwise the explicit ``session_id``, else ``unhomed``.
    """
    if explicit:
        # An explicit ``--session none`` arrives as None/"" and must stay
        # unhomed; falling through would inherit the PARENT's home and stamp
        # the child with a session the caller refused (C6, #1118).
        sid = str(session_id).strip() if session_id else ""
        return (sid or UNHOMED_SESSION), None
    worker_tid = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    creator_tid = (str(creator_task_id).strip() if creator_task_id else "")
    for tid in (
        *(parents or ()),
        *((creator_tid,) if creator_tid else ()),
        *((worker_tid,) if worker_tid else ()),
    ):
        row = conn.execute(
            "SELECT session_id, body FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        home = (row["session_id"] or "").strip() if row is not None else ""
        if home and not is_unhomed(home):
            return home, _origin_line(row["body"])
    if worker_tid:
        return UNHOMED_SESSION, None
    sid = (str(session_id).strip() if session_id else "")
    return (sid or UNHOMED_SESSION), None
UNHOMED_BACKFILL_COMMENT = (
    "origin: unknown (pre-stamp or cron) \u00b7 homed-by triage -- this card "
    "had no home session; stamped '" + UNHOMED_SESSION + "' so no chat session "
    "adopts it implicitly. The home session (or the triage sweep) re-homes "
    "it with: hermes kanban update <id> --session <sid> --takeover \"<reason>\""
)


def find_homeless_open_tasks(conn: sqlite3.Connection) -> list[str]:
    """Open (non-terminal, non-archived) cards with a NULL/empty home."""
    return [
        r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE (session_id IS NULL OR "
            "TRIM(session_id) = '') AND status NOT IN ('done', 'archived') "
            "ORDER BY created_at, id"
        )
    ]


def backfill_unhomed(
    conn: sqlite3.Connection, *, dry_run: bool = False, author: str = "kanban-home-lint"
) -> list[str]:
    """Stamp every homeless OPEN card ``unhomed`` + an explanatory comment.

    Execution-lane write (no mutation actor): it restamps only rows that
    have no home at all, so there is no home session to trespass on.
    ``task_comments`` carries only a one-way ``session_ref`` fingerprint,
    so a home cannot be recovered from comments; ``unhomed`` is the
    honest value. Idempotent: a second run finds nothing.
    """
    ids = find_homeless_open_tasks(conn)
    if dry_run:
        return ids
    done: list[str] = []
    for tid in ids:
        with write_txn(conn, allow_nested=True):
            cur = conn.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ? AND "
                "(session_id IS NULL OR TRIM(session_id) = '')",
                (UNHOMED_SESSION, tid),
            )
            if cur.rowcount != 1:
                continue
            _append_event(
                conn, tid, "session_restamped",
                {"session_id": UNHOMED_SESSION, "backfill": True},
            )
            # Same transaction as the stamp: a failed comment must roll the
            # stamp back, or the next run skips the card and the audit
            # comment is lost for good (C5 backfill, PR #987 review).
            add_comment(conn, tid, author=author, body=UNHOMED_BACKFILL_COMMENT)
        done.append(tid)
    return done


class ForeignSessionMutationError(ValueError):
    """A chat-driven status/ownership mutation targeted a card whose home
    session is a different session. ``ValueError`` so every existing CLI /
    tool error path already renders it as a clean refusal."""


@dataclass(frozen=True)
class MutationActor:
    """Who is mutating, as seen by the home-session guard."""

    session_ids: tuple[str, ...]
    profile: Optional[str]
    foreign_ok: Optional[str] = None
    surface: str = "cli"  # "cli" | "tool" -- only shapes the refusal hint
    # ``--operator "<who: why>"``: an operator profile applying a relayed
    # human decision. Recorded as an ``operator_override`` event; on a
    # status/timing verb also a FOREIGN CHANGE comment (FOREIGN_CHANGE_ACTIONS).
    operator: Optional[str] = None
    # What a ``--takeover`` does to the card's home: ``"keep"`` (--keep-home),
    # ``"transfer"`` (--transfer-home), or None = the verb's default (see
    # :data:`KEEP_HOME_BY_DEFAULT_ACTIONS`).
    home: Optional[str] = None

# Profiles allowed to use the ``--operator`` override. ``default`` is Apollo.
OPERATOR_PROFILES: frozenset[str] = frozenset({"default", "apollo", "aegis"})
_MUTATION_ACTOR: ContextVar[Optional[MutationActor]] = ContextVar(
    "kanban_mutation_actor", default=None
)
_UNSTAMPED_WARNED: list[bool] = [False]

# Provenance copy of the bound actor for ``task_events``. Unlike
# ``_MUTATION_ACTOR`` the guard wrapper never clears it, so events written
# inside a guarded mutator's body still record who asked for the mutation.
_EVENT_ACTOR: ContextVar[Optional[MutationActor]] = ContextVar(
    "kanban_event_actor", default=None
)


def _process_is_gateway() -> bool:
    """True only when THIS process is the running gateway.

    ``_HERMES_GATEWAY=1`` is inherited by gateway descendants and is also set
    at import time by ``gateway.run``, which CLI-side code imports lazily
    (send_message, platform actions, compression, tui_gateway). Neither the
    marker nor the import proves ownership. Require positive evidence: a live
    ``GatewayRunner`` (``gateway.run._gateway_runner_ref``), or the gateway PID
    record naming this process (FleetReview 09c07e5eb0a9).
    """
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return False
    run_mod = sys.modules.get("gateway.run")
    if run_mod is None:
        return False
    ref = getattr(run_mod, "_gateway_runner_ref", None)
    try:
        if callable(ref) and ref() is not None:
            return True
    except Exception:
        pass
    try:
        from gateway.status import get_running_pid

        return get_running_pid(cleanup_stale=False) == os.getpid()
    except Exception:
        return False


def _event_actor() -> tuple[Optional[str], Optional[str]]:
    """``(actor_profile, actor_session_id)`` for a ``task_events`` row.

    Same resolution order as the home-session guard's callers: the explicitly
    bound actor (CLI / tool surface), then the in-process session context,
    then the environment. ``(None, None)`` when there is no identity.
    """
    actor = _EVENT_ACTOR.get()
    if actor is not None:
        return actor.profile, (actor.session_ids[0] if actor.session_ids else None)
    session_id: Optional[str] = None
    in_gateway = _process_is_gateway()
    try:
        from gateway.session_context import resolve_current_session_id

        session_id = (resolve_current_session_id() or "").strip() or None
    except Exception:
        session_id = None
    if session_id is None and not in_gateway:
        # In-process, the env belongs to another session -- never ours.
        session_id = (os.environ.get("HERMES_SESSION_ID") or "").strip() or None
    profile = None
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        profile = (os.environ.get(env) or "").strip() or None
        if profile:
            break
    if profile is None and session_id is not None:
        # A chat/gateway caller with a session but no profile env (only
        # worker spawns export it): the running profile IS the actor.
        # Identity-less callers (no session) stay NULL.
        try:
            from hermes_cli.profiles import get_active_profile_name

            profile = get_active_profile_name() or None
        except Exception:
            profile = None
    return profile, session_id


@contextlib.contextmanager
def mutation_actor(
    *,
    session_ids: Iterable[Optional[str]] = (),
    profile: Optional[str] = None,
    foreign_ok: Optional[str] = None,
    surface: str = "cli",
    operator: Optional[str] = None,
    home: Optional[str] = None,
):
    """Bind the chat-facing caller for the home-session guard."""
    if home not in (None, "keep", "transfer"):
        raise ValueError(f"home must be 'keep', 'transfer' or None, not {home!r}")
    ids = tuple(dict.fromkeys(
        str(s).strip() for s in (session_ids or ()) if s and str(s).strip()
    ))
    actor = MutationActor(
        session_ids=ids,
        profile=(str(profile).strip() or None) if profile else None,
        foreign_ok=(str(foreign_ok).strip() or None) if foreign_ok else None,
        surface=surface,
        operator=(str(operator).strip() or None) if operator else None,
        home=home,
    )
    token = _MUTATION_ACTOR.set(actor)
    ev_token = _EVENT_ACTOR.set(actor)
    try:
        yield actor
    finally:
        _EVENT_ACTOR.reset(ev_token)
        _MUTATION_ACTOR.reset(token)


def _caller_session_lineage(session_id: str) -> tuple[str, ...]:
    """Compression lineage of the caller's session (best effort).

    Context compression rotates the physical session id; without this a
    session would lose ownership of its own cards after its first
    compaction. Consulted only on the mismatch path, never on the hot path.
    """
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            lineage = db.get_compression_lineage(session_id)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
        return tuple(str(x) for x in (lineage or ()) if x)
    except Exception:
        return ()


def session_owner_profile(session_id: Optional[str]) -> Optional[str]:
    """The profile whose ``state.db`` holds ``session_id`` (``default`` for
    the root home), or ``None``.

    The profile env of a CLI call is not the caller's identity: a script
    that repoints the home at the root board (to reach ``~/.hermes`` state)
    resolves as ``default`` from inside another profile's session. The
    session row is. Read-only, short timeout, fail-open; consulted only on
    the guard's mismatch path.
    """
    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        return None
    try:
        from hermes_cli.profiles import _get_default_hermes_home

        root = _get_default_hermes_home()
    except Exception:
        return None
    candidates = [("default", root / "state.db")]
    try:
        candidates += [
            (p.name, p / "state.db")
            for p in sorted((root / "profiles").iterdir())
            if p.is_dir()
        ]
    except OSError:
        pass
    for name, path in candidates:
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0
            )
            try:
                hit = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (sid,)
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            continue
        if hit is not None:
            return name
    return None


def _actor_profiles(actor: MutationActor) -> frozenset[str]:
    """Every profile identity the actor legitimately holds.

    The owner profile of each caller session is authoritative. The bound
    (env-derived) profile counts only when no caller session resolves to an
    owner: a root-home repoint makes it read ``default`` -- an operator
    profile -- from inside another profile's session, so keeping both let
    that caller pass ``--operator`` or mutate a ``default``-assigned card
    (FleetReview #1074).
    """
    owners = {
        owner for owner in (session_owner_profile(sid) for sid in actor.session_ids)
        if owner
    }
    if owners:
        return frozenset(owners)
    return frozenset({actor.profile} if actor.profile else ())


# What sent an operator send-back back (request_changes ``operator_kind``).
OPERATOR_KIND_HUMAN = "human"
OPERATOR_KIND_MACHINE = "machine"
OPERATOR_KINDS: tuple[str, ...] = (OPERATOR_KIND_HUMAN, OPERATOR_KIND_MACHINE)


def _valid_operator_reason(reason: str) -> bool:
    who, sep, why = reason.partition(":")
    return bool(sep and who.strip() and why.strip())
HOME_LINEAGE_MAX_DEPTH = 10

# Per-process lineage cache.  Lineage rows are append-mostly (ancestors never
# change; descendants appear on rotation, which hands the caller a NEW id), so
# a short TTL bounds the only staleness (a new descendant of an OLD id).
# Keyed by (session id, state.db path).  Only successful DB-backed answers are
# cached: a fail-open ``{session_id}`` (no db, no row yet, locked) is retried.
HOME_IDS_CACHE_MAX = 256
HOME_IDS_CACHE_TTL_S = 300.0
_HOME_CACHE: "dict[tuple[str, str, int], tuple[float, frozenset[str], Optional[float]]]" = {}
_HOME_CACHE_LOCK = threading.Lock()


def clear_home_ids_cache() -> None:
    with _HOME_CACHE_LOCK:
        _HOME_CACHE.clear()


def home_ids(session_id: Optional[str], *, db_path: Any = None) -> frozenset[str]:
    """THE definition of a session's kanban "home": the id plus its
    ``parent_session_id`` ancestors and descendants that share its
    ``session_key`` (depth <= :data:`HOME_LINEAGE_MAX_DEPTH` each way).

    Gateway chats rotate their session id (resume_pending_expired,
    session_switch, most ``/new``) while keeping the ``session_key`` and
    linking the successor via ``parent_session_id``; an exact-id home would
    make a chat foreign to its own cards after every rotation. Requiring the
    same non-NULL ``session_key`` keeps another chat's chain (and keyless
    subagent/CLI children) out.

    Read-only against the gateway ``state.db`` sessions table, short
    ``busy_timeout``, and FAIL-OPEN: any error (no state.db, no such row,
    locked, old schema) returns ``{session_id}`` -- exactly the pre-lineage
    exact-id behaviour. Shared by ``list --home``, ``show``'s home label,
    the home-session guard and the kanban-home-cards plugin.  Answers are
    cached per process (:data:`HOME_IDS_CACHE_TTL_S`).
    """
    return home_lineage(session_id, db_path=db_path)[0]


def home_lineage(
    session_id: Optional[str], *, db_path: Any = None
) -> tuple[frozenset[str], Optional[float]]:
    """``(home_ids(session_id), earliest started_at in that home)``.

    The start time is ``None`` whenever it is unknown (fail-open answer, no
    ``started_at``), which callers must treat as "could be arbitrarily old".
    """
    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        return frozenset(), None
    only = (frozenset({sid}), None)
    try:
        if db_path is None:
            from hermes_state import _default_db_path

            db_path = _default_db_path()
        path = Path(db_path)
        try:
            st = path.stat()
        except OSError:
            return only
        # inode in the key: a replaced/recreated state.db is a new cache.
        key = (sid, str(path), st.st_ino)
        now = time.monotonic()
        with _HOME_CACHE_LOCK:
            hit = _HOME_CACHE.get(key)
            if hit is not None and now - hit[0] < HOME_IDS_CACHE_TTL_S:
                return hit[1], hit[2]
        if not path.is_file():
            return only
        found = _home_lineage_query(path, sid)
        if found is None:
            return only
        with _HOME_CACHE_LOCK:
            _HOME_CACHE.pop(key, None)
            _HOME_CACHE[key] = (now, found[0], found[1])
            while len(_HOME_CACHE) > HOME_IDS_CACHE_MAX:
                _HOME_CACHE.pop(next(iter(_HOME_CACHE)))
        return found
    except Exception:
        return only


def _home_lineage_query(
    path: Path, sid: str
) -> Optional[tuple[frozenset[str], Optional[float]]]:
    """Walk the lineage in ``path``; ``None`` = no keyed row (not cacheable)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        # started_at is optional: a sessions table without it (older schema,
        # the dashboard facet fixture) still yields the lineage, start unknown.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        st = "started_at" if "started_at" in cols else "NULL"
        row = conn.execute(
            f"SELECT session_key, parent_session_id, {st} FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        key = row[0]
        ids = {sid}
        starts = [row[2]]
        parent = row[1]
        for _ in range(HOME_LINEAGE_MAX_DEPTH):
            if not parent or parent in ids:
                break
            prow = conn.execute(
                f"SELECT parent_session_id, {st} FROM sessions "
                "WHERE id = ? AND session_key = ?",
                (parent, key),
            ).fetchone()
            if prow is None:
                break
            ids.add(parent)
            starts.append(prow[1])
            parent = prow[0]
        frontier = [sid]
        for _ in range(HOME_LINEAGE_MAX_DEPTH):
            if not frontier:
                break
            ph = ",".join("?" * len(frontier))
            kids = []
            for r in conn.execute(
                f"SELECT id, {st} FROM sessions WHERE parent_session_id IN ({ph}) "
                "AND session_key = ?",
                (*frontier, key),
            ):
                if r[0] and r[0] not in ids:
                    kids.append(r[0])
                    starts.append(r[1])
            ids.update(kids)
            frontier = kids
        try:
            started = min(float(x) for x in starts) if all(
                isinstance(x, (int, float)) for x in starts) else None
        except (TypeError, ValueError):
            started = None
        return frozenset(ids), started
    finally:
        conn.close()


def home_guard_mode() -> str:
    """``kanban.home_guard``: ``refuse`` (default) or ``warn``.

    2026-09-24: default flipped warn -> refuse now that "home" is the
    session lineage (:func:`home_ids`), so id rotations inside one chat no
    longer make a session foreign to its own cards. ``warn`` is the
    operator escape hatch: the foreign mutation proceeds with one stderr line.
    Any unreadable/unknown value means ``refuse``.
    """
    try:
        from hermes_cli.config import load_config

        value = (load_config() or {}).get("kanban", {}).get("home_guard", "refuse")
    except Exception:
        return "refuse"
    return "warn" if str(value).strip().lower() == "warn" else "refuse"
WORKER_FANOUT_MAX_DEPTH = 10


def _worker_owns_card(
    conn: sqlite3.Connection,
    task_id: str,
    worker_task_id: str,
    session_ids: Iterable[str] = (),
) -> bool:
    """True when ``task_id`` belongs to the fan-out of the dispatched worker
    run for ``worker_task_id``: it descends from that card through
    ``task_links`` (any kind, depth <= :data:`WORKER_FANOUT_MAX_DEPTH`), or its
    ``created`` event was written by this run's session (``actor_session_id``).
    A card that only shares the worker card's human home is NOT owned."""
    seen = {task_id}
    frontier = [task_id]
    for _ in range(WORKER_FANOUT_MAX_DEPTH):
        if not frontier:
            break
        ph = ",".join("?" * len(frontier))
        ups = [
            r[0] for r in conn.execute(
                f"SELECT parent_id FROM task_links WHERE child_id IN ({ph})",
                frontier,
            )
            if r[0]
        ]
        if worker_task_id in ups:
            return True
        frontier = [u for u in ups if u not in seen]
        seen.update(frontier)
    sids = tuple(s for s in (session_ids or ()) if s)
    if not sids:
        return False
    try:
        ph = ",".join("?" * len(sids))
        row = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'created' "
            f"AND actor_session_id IN ({ph}) LIMIT 1",
            (task_id, *sids),
        ).fetchone()
    except sqlite3.OperationalError:  # pre-provenance schema
        return False
    return row is not None

# ---------------------------------------------------------------------------
# Operator-token flag gate (harness-parity spec 4.7 layer i, AC-A6)
# ---------------------------------------------------------------------------
# ``--takeover`` / ``--operator`` let a CLI caller act on a card it does not
# own. While a run is active on the card, those flags are refused unless the
# process presents ``KANBAN_OPERATOR_TOKEN`` equal to the ``operator-token``
# file beside the board (written 0600 by a launchd job, never by the gateway).
# The dispatcher's own worker is exempt only when ``HERMES_KANBAN_OWNER_PID`` is
# THIS pid: an exact pid match, never an env value alone and never ancestry.
# This is a tripwire, not a wall: a same-uid process can read the file (AC-A6
# arm 6, Q15). Every refusal leaves a ``takeover_refused`` event.
OPERATOR_TOKEN_ENV = "KANBAN_OPERATOR_TOKEN"
OPERATOR_TOKEN_FILENAME = "operator-token"
OPERATOR_FLAG_GATED_ACTIONS: frozenset[str] = frozenset({
    "complete", "block", "unblock", "reassign", "archive", "request-review",
    # Also end a live run under ``--takeover``/``--operator`` (t_920c6b4a):
    # ``schedule_task`` and ``triage_resolve_task`` call ``_end_run``, so an
    # ungated flag there parks a live worker's card. ``request-changes`` is
    # deliberately absent: it ends only a review run the calling session
    # holds (or none, on a parked card) and refuses every other holder itself.
    "schedule", "triage-resolve",
})

# Actions where ONLY ``--operator`` is gated: on ``reclaim`` it overrides the
# dead-claimer liveness hold (t_6451e7c9), which must not be reachable with an
# arbitrary string. ``reclaim --takeover`` keeps its existing behaviour.
OPERATOR_ONLY_GATED_ACTIONS: frozenset[str] = frozenset({"reclaim"})


class OperatorTokenRequiredError(ValueError):
    """A flag override on a card with an active run lacked the operator token."""


def operator_token_path() -> Path:
    """``<root>/kanban/operator-token``: beside the boards, outside any profile."""
    return kanban_home() / "kanban" / OPERATOR_TOKEN_FILENAME


def _operator_token_state() -> str:
    """``ok`` | ``absent`` | ``no_token_file`` | ``mismatch``. Never the value."""
    import hmac

    presented = (os.environ.get(OPERATOR_TOKEN_ENV) or "").strip()
    if not presented:
        return "absent"
    try:
        expected = operator_token_path().read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "no_token_file"
    if not expected:
        return "no_token_file"
    if hmac.compare_digest(presented.encode(), expected.encode()):
        return "ok"
    return "mismatch"


def _caller_holds_grant_for(conn: sqlite3.Connection, task_id: str) -> bool:
    """True only for the dispatcher's worker on its own card, by exact pid.

    The env pair alone is caller-controlled: any process can name a live card
    and its own pid (or the ``pending`` sentinel) before invoking the CLI. So
    the pid must ALSO be the one the dispatcher stamped on the card's current
    run (``tasks.worker_pid``), a value the caller cannot supply through its
    environment (t_920c6b4a, FleetReview 80796f262c18).
    """
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip() != task_id:
        return False
    owner = (os.environ.get("HERMES_KANBAN_OWNER_PID") or "").strip()
    try:
        if int(owner) != os.getpid():
            return False
    except ValueError:
        return False
    row = conn.execute(
        "SELECT worker_pid, current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None or row["current_run_id"] is None or row["worker_pid"] is None:
        return False
    try:
        return int(row["worker_pid"]) == os.getpid()
    except (TypeError, ValueError):
        return False


def task_run_is_active(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None
) -> bool:
    """A run is active: ``current_run_id`` set, and the card is ``running`` or
    its claim has not yet expired.

    A lapsed claim on a ``running`` card is a normal state for a live worker
    (a slow tool-free LLM call outlives the TTL until the next dispatcher
    sweep extends it, see ``release_stale_claims``), so expiry alone must not
    open the gate (t_920c6b4a, FleetReview 9094c8989646). Mirrors
    :func:`_task_has_live_run`.
    """
    row = conn.execute(
        "SELECT status, current_run_id, claim_expires FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["current_run_id"] is None:
        return False
    if row["status"] == "running":
        return True
    expires = row["claim_expires"]
    return bool(expires) and int(expires) > int(now if now is not None else time.time())


def _gated_flags_for(action: str, flags: Iterable[str]) -> list[str]:
    used = sorted({f for f in (flags or ()) if f})
    if action in OPERATOR_ONLY_GATED_ACTIONS:
        return [f for f in used if f == "--operator"]
    if action not in OPERATOR_FLAG_GATED_ACTIONS:
        return []
    return used


def _operator_gate_refusal(
    conn: sqlite3.Connection, task_ids: Sequence[str], action: str, used: Sequence[str],
) -> Optional[tuple[str, dict]]:
    """``(task_id, event payload)`` for the first card the flags may not
    override, or ``None`` when every card passes.

    The payload is a sanitized description of the Kanban request (action,
    flags, target ids), never the process argv: argv carries ``--summary`` /
    ``--metadata`` values, and in-process ``/kanban`` it is the gateway's own
    launch line (t_920c6b4a, FleetReview fc8a7bdcf814).
    """
    if not used:
        return None
    token = _operator_token_state()
    if token == "ok":
        return None
    for tid in task_ids:
        if _caller_holds_grant_for(conn, tid) or not task_run_is_active(conn, tid):
            continue
        return tid, {
            "action": action,
            "flags": list(used),
            "task_ids": list(task_ids),
            "caller_pid": os.getpid(),
            "token": token,
        }
    return None


def _operator_gate_error(action: str, used: Sequence[str], tid: str, token: str):
    return OperatorTokenRequiredError(
        f"refused {action} {' '.join(used)} on {tid}: a run is active on it, "
        f"and overriding a live run needs the operator token "
        f"({OPERATOR_TOKEN_ENV}, token {token}). Recorded as a "
        f"takeover_refused event. An operator presents it inline: "
        f"{OPERATOR_TOKEN_ENV}=$(cat {operator_token_path()}) "
        f"hermes kanban {action} {tid} ..."
    )


def enforce_operator_flag_gate(
    conn: sqlite3.Connection,
    task_ids: Iterable[str],
    action: str,
    *,
    flags: Iterable[str],
) -> None:
    """Refuse ``flags`` on any of ``task_ids`` that has an active run, unless
    the caller presents the operator token or owns the card's grant by pid.

    Raises :class:`OperatorTokenRequiredError` after appending one
    ``takeover_refused`` event (caller pid, sanitized request, which flags,
    token state). This is the fast preflight; the authoritative recheck runs
    inside each write transaction under :func:`operator_flag_gate_scope`.
    """
    used = _gated_flags_for(action, flags)
    tids = list(dict.fromkeys(str(t) for t in (task_ids or ()) if t))
    refusal = _operator_gate_refusal(conn, tids, action, used)
    if refusal is None:
        return
    tid, payload = refusal
    with write_txn(conn, allow_nested=True):
        _append_event(conn, tid, "takeover_refused", payload)
    raise _operator_gate_error(action, used, tid, payload["token"])

# Set by :func:`operator_flag_gate_scope` for one gated CLI mutation: every
# OUTERMOST write transaction opened inside it re-runs the gate under its write
# lock, before any write. The preflight above runs on its own connection and
# commits nothing, so a dispatcher claim landing between the preflight and the
# handler's mutation would otherwise be closed by a tokenless override
# (t_920c6b4a, FleetReview c12b9f27d054).
_PENDING_OPERATOR_GATE: ContextVar[Optional[dict]] = ContextVar(
    "kanban_pending_operator_gate", default=None
)


@contextlib.contextmanager
def operator_flag_gate_scope(task_ids: Iterable[str], action: str, *, flags: Iterable[str]):
    """Hold the operator-flag gate for the duration of one lifecycle handler.

    A refusal inside a write transaction rolls that transaction back; the
    ``takeover_refused`` event is then written in its own transaction on the
    way out, so the refusal stays audited.
    """
    used = _gated_flags_for(action, flags)
    if not used:
        yield
        return
    pending = {
        "task_ids": list(dict.fromkeys(str(t) for t in (task_ids or ()) if t)),
        "action": action,
        "flags": used,
        "refused": [],
    }
    token = _PENDING_OPERATOR_GATE.set(pending)
    try:
        yield
    finally:
        _PENDING_OPERATOR_GATE.reset(token)
        for tid, payload in pending["refused"]:
            try:
                with connect_closing() as conn:
                    with write_txn(conn):
                        _append_event(conn, tid, "takeover_refused", payload)
            except Exception as exc:
                # The refusal itself stands (the mutation rolled back); only
                # its audit row is missing. Never swallow that silently
                # (t_920c6b4a, FleetReview 667f8318dd40).
                _log.warning(
                    "takeover_refused audit NOT recorded for %s (%s): %s",
                    tid, payload.get("action"), exc,
                )
                try:
                    print(
                        f"kanban: warning: the takeover_refused event for {tid} "
                        f"could not be recorded ({exc})",
                        file=sys.stderr,
                    )
                except Exception:
                    pass


def _recheck_pending_operator_gate(conn: sqlite3.Connection) -> None:
    """Re-run the pending gate. Runs on EVERY outermost write txn in the scope,
    including after an earlier refusal: a caller that catches the refusal and
    goes on to another write is gated again, never waved through
    (t_920c6b4a, FleetReview d292b198bb11)."""
    pending = _PENDING_OPERATOR_GATE.get()
    if pending is None:
        return
    refusal = _operator_gate_refusal(
        conn, pending["task_ids"], pending["action"], pending["flags"]
    )
    if refusal is None:
        return
    pending["refused"].append(refusal)
    tid, payload = refusal
    raise _operator_gate_error(pending["action"], pending["flags"], tid, payload["token"])


def authorize_pending_operator_gate(conn: sqlite3.Connection) -> None:
    """Run the pending gate under the write lock NOW, for callers about to take
    a side effect no transaction can roll back (signalling a worker). A no-op
    outside :func:`operator_flag_gate_scope`."""
    if _PENDING_OPERATOR_GATE.get() is None:
        return
    if getattr(conn, "in_transaction", False):
        _recheck_pending_operator_gate(conn)
        return
    # A bare IMMEDIATE txn, not ``write_txn``: that would also consume the
    # pending home-session recheck, which must stay with the real mutation.
    _execute_boundary_with_retry(conn, "BEGIN IMMEDIATE")
    try:
        _recheck_pending_operator_gate(conn)
    finally:
        try:
            conn.execute("ROLLBACK")  # nothing was written
        except sqlite3.OperationalError:
            pass


# Status/timing verbs whose ``--operator`` / ``--takeover`` on a foreign card
# must announce itself (FOREIGN CHANGE comment + ``foreign_change`` event) and
# yield to a NEWER human ruling the home session holds (t_4b826d5c: on
# 09-30 21:58 one operator session re-parked another session's cards against
# Ace's later ruling, and the home session learned of it 10 minutes later).
FOREIGN_CHANGE_ACTIONS: frozenset[str] = frozenset({
    "schedule", "unblock", "block", "triage-resolve", "reassign", "assign",
    "priority",
})
# A cited human ruling: ``msg <discord message id>`` (snowflakes grow with time).
_RULING_MSG_RE = re.compile(r"\bmsg\s+(\d{18,20})\b")
# A home-session comment that relays a human ruling.
_HOME_RULING_COMMENT_RE = re.compile(r"APOLLO\b.*\bACE\b|--operator", re.S)


def ruling_msg_id(text: Optional[str]) -> Optional[int]:
    """Newest ``msg <id>`` cited in ``text``, or ``None``."""
    ids = [int(m) for m in _RULING_MSG_RE.findall(text or "")]
    return max(ids) if ids else None


def home_ruling_msg_id(
    conn: sqlite3.Connection, task_id: str, home: str
) -> Optional[int]:
    """Newest Discord msg id the HOME session cited on this card: its
    ``APOLLO … ACE …`` / ``--operator`` comments and its own
    ``operator_override`` events. ``None`` when it cited none."""
    sids = set(home_ids(home)) | {home}
    refs = {derive_session_ref(s) for s in sids}
    best: Optional[int] = None
    for r in conn.execute(
        "SELECT body, session_ref FROM task_comments WHERE task_id = ?",
        (task_id,),
    ):
        if r["session_ref"] in refs and _HOME_RULING_COMMENT_RE.search(r["body"] or ""):
            mid = ruling_msg_id(r["body"])
            if mid is not None and (best is None or mid > best):
                best = mid
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'operator_override'",
        (task_id,),
    ):
        try:
            p = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(p, dict) or not (set(p.get("by_sessions") or ()) & sids):
            continue
        mid = ruling_msg_id(str(p.get("reason") or ""))
        if mid is not None and (best is None or mid > best):
            best = mid
    return best


def _origin_channel(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    for line in ((row["body"] if row else "") or "").splitlines():
        if line.startswith("origin:"):
            return line[len("origin:"):].split("·")[0].strip() or "the home chat"
    return "the home chat"


def _check_ruling_precedence(
    conn: sqlite3.Connection, task_id: str, action: str, home: str,
    reason: Optional[str],
) -> None:
    """Refuse a foreign status/timing override that would overturn a NEWER
    human ruling held by the card's home session. No id on the home side =
    allowed (status quo); the change is still announced."""
    if action not in FOREIGN_CHANGE_ACTIONS:
        return
    home_msg = home_ruling_msg_id(conn, task_id, home)
    if home_msg is None:
        return
    cited = ruling_msg_id(reason)
    if cited is not None and cited >= home_msg:
        return
    raise ForeignSessionMutationError(
        f"refused {action} on {task_id}: home session holds a newer human "
        f"ruling (msg {home_msg}); re-home with --takeover or ask in "
        f"{_origin_channel(conn, task_id)}. "
        + (f"You cited msg {cited}. " if cited is not None
           else "You cited no msg id. ")
        + f"Re-home: hermes kanban update {task_id} --session <yours> "
        f"--takeover \"<reason>\"."
    )


def _announce_foreign_change(
    conn: sqlite3.Connection, task_id: str, action: str, actor: MutationActor,
    home: Optional[str],
) -> None:
    """``FOREIGN CHANGE by <session> (<reason>)`` comment + ``foreign_change``
    event, so the home session sees a foreign status/timing change on its next
    overview instead of discovering it by accident."""
    if action not in FOREIGN_CHANGE_ACTIONS:
        return
    sess = ", ".join(actor.session_ids) or "no-session"
    reason = actor.operator or actor.foreign_ok or ""
    with write_txn(conn, allow_nested=True):
        _append_event(conn, task_id, "foreign_change", {
            "action": action,
            "via": "--operator" if actor.operator else "--takeover",
            "reason": reason,
            "cited_msg": (str(ruling_msg_id(reason))
                          if ruling_msg_id(reason) is not None else None),
            "by_sessions": list(actor.session_ids),
            "by_profile": actor.profile,
            "home": home,
        })
    try:
        session_ref = (derive_session_ref(actor.session_ids[0])
                       if actor.session_ids else None)
    except Exception:
        session_ref = None
    add_comment(
        conn, task_id, author=actor.profile or "user",
        body=f"FOREIGN CHANGE by {sess} ({reason}) [{action}]",
        session_ref=session_ref,
    )


def check_home_session(
    conn: sqlite3.Connection, task_id: str, action: str
) -> Optional[MutationActor]:
    """Enforce home-session ownership for one mutation of ``task_id``.

    Returns ``None`` when the mutation is allowed outright, the bound
    :class:`MutationActor` when it is allowed ONLY via the ``foreign_ok``
    override (the caller must then record the audit comment once the
    mutation succeeds), and raises :class:`ForeignSessionMutationError` when
    it is refused.
    """
    actor = _MUTATION_ACTOR.get()
    if actor is None:
        return None
    row = conn.execute(
        "SELECT session_id, assignee FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None  # unknown id: the mutator reports it in its own words
    home = (row["session_id"] or "").strip()
    if not home:
        if actor.surface == "cli" and not _UNSTAMPED_WARNED[0]:
            _UNSTAMPED_WARNED[0] = True
            print(
                f"kanban: warning: {task_id} has no home session (legacy "
                f"unstamped card) -- {action} allowed",
                file=sys.stderr,
            )
        return None
    # No chat-session identity (cron opener, wake script, a plain shell):
    # that is the execution lane, not a chat acting on a foreign card.
    if not actor.session_ids:
        return None
    if home in actor.session_ids:
        return None
    # Operator-homed (cron/script minted): any operator-profile session owns
    # it -- the whole point of the pseudo-session (t_09fea045).
    if is_operator_home(home) and (_actor_profiles(actor) & OPERATOR_PROFILES):
        return None
    # Execution lane: a dispatched worker always owns the card it was spawned
    # for. The assignee match lives below, on :func:`_actor_profiles` (session
    # owner over env profile), not on the raw env profile (FleetReview #1074).
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip() == task_id:
        return None
    # ...and the cards it fanned out: item 1 stamps a worker's children with
    # the HUMAN home, so without this the guard refuses a worker on its own
    # fan-out (link/assign/promote/archive...). Unrelated cards that merely
    # share that home stay foreign.
    worker_tid = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if worker_tid and _worker_owns_card(
        conn, task_id, worker_tid, actor.session_ids
    ):
        return None
    for sid in actor.session_ids:
        if home in home_ids(sid) or home in _caller_session_lineage(sid):
            return None
    # Assignee identity = the profile of the caller SESSION too, not only the
    # profile env (a root-home repoint makes that read ``default``).
    profiles = _actor_profiles(actor)
    if (row["assignee"] or "") in profiles:
        return None
    caller = ", ".join(actor.session_ids) or "none"
    if actor.operator:
        if not (profiles & OPERATOR_PROFILES):
            raise ForeignSessionMutationError(
                f"refused {action} on {task_id}: --operator is for operator "
                f"profiles ({', '.join(sorted(OPERATOR_PROFILES))}); caller "
                f"profile(s): {', '.join(sorted(profiles)) or 'unknown'}."
            )
        if not _valid_operator_reason(actor.operator):
            raise ForeignSessionMutationError(
                f"refused {action} on {task_id}: --operator needs "
                f"\"<who: why>\" (e.g. \"Ace via Aegis: ruled (a)\")."
            )
        _check_ruling_precedence(conn, task_id, action, home, actor.operator)
        return actor
    if actor.foreign_ok:
        _check_ruling_precedence(conn, task_id, action, home, actor.foreign_ok)
        return actor
    if home_guard_mode() == "warn":
        print(
            f"kanban: warning: {action} on {task_id} from caller session "
            f"{caller}; its home session is {home} (kanban.home_guard=warn, "
            f"allowed)",
            file=sys.stderr,
        )
        return None
    override = (
        '--takeover "<reason>"' if actor.surface == "cli"
        else 'foreign_ok="<reason>"'
    )
    operator_hint = (
        f" An operator profile ({', '.join(sorted(OPERATOR_PROFILES))}) "
        f"applying a relayed human decision uses --operator \"<who: why>\" "
        f"instead (recorded as an operator_override event; status/timing verbs also post a FOREIGN CHANGE comment)."
        if actor.surface == "cli" else ""
    )
    if is_unhomed(home):
        raise ForeignSessionMutationError(
            f"refused {action} on {task_id}: it is UNHOMED (born with no "
            f"session identity -- cron/script/shell), so no session owns it "
            f"(caller session: {caller}). Comment instead: "
            f"hermes kanban comment {task_id} \"...\", or take it over "
            f"explicitly with {override} (adopt it for good: hermes kanban "
            f"update {task_id} --session <yours> --takeover \"<reason>\"); "
            f"the takeover is recorded as an event + audit comment."
            f"{operator_hint}"
        )
    raise ForeignSessionMutationError(
        f"refused {action} on {task_id}: its home session is {home} "
        f"(caller session: {caller}). Status/ownership changes belong to the "
        f"home session or the assignee -- comment instead: "
        f"hermes kanban comment {task_id} \"...\" "
        f"(or take over with {override}, which records a takeover event + "
        f"an audit comment the home session sees).{operator_hint}"
    )


def _mutation_succeeded(result: Any) -> bool:
    if result is None:  # ``-> None`` mutators (link_tasks) signal failure by raising
        return True
    if isinstance(result, tuple):
        return bool(result and result[0])
    return bool(result)

# ``--takeover`` on these verbs ADOPTS the card: the taking session becomes its
# home (children and pings follow). Other verbs stay a one-off foreign action;
# ``edit --session <sid>`` re-homes without a status change. ``--operator``
# never re-homes (it applies a relayed ruling, it does not adopt). ``complete``
# is terminal, so it never adopts either. Sweep actors (cron sessions,
# delegate children) never re-home: see :func:`_can_adopt_home`.
REHOME_ON_TAKEOVER_ACTIONS: frozenset[str] = frozenset({
    "assign", "unblock", "promote", "reclaim", "triage-resolve",
})

# Adopting verbs whose ``--takeover`` KEEPS the home unless the caller passes
# ``--transfer-home``. A reclaim returns a running card to its queue (load
# shedding, a hung worker); it is not an ownership transfer. Measured
# 2026-09-29 19:12: 14 foreign cards reclaimed under load were re-homed into
# the operator's session and their review pings moved to its chat (t_0b3b0667).
KEEP_HOME_BY_DEFAULT_ACTIONS: frozenset[str] = frozenset({"reclaim"})


def _takeover_transfers_home(action: str, actor: "MutationActor") -> bool:
    """Whether this ``--takeover`` asks to move the card's home to the actor."""
    if action not in REHOME_ON_TAKEOVER_ACTIONS:
        return False
    if actor.home is not None:
        return actor.home == "transfer"
    return action not in KEEP_HOME_BY_DEFAULT_ACTIONS


def _home_conversation(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """The card's home CONVERSATION: the chats its pings go to right now."""
    try:
        rows = conn.execute(
            "SELECT platform, chat_id, thread_id FROM kanban_notify_subs "
            "WHERE task_id = ? ORDER BY created_at, platform, chat_id, thread_id",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"platform": r["platform"], "chat_id": r["chat_id"],
         "thread_id": r["thread_id"] or ""}
        for r in rows
    ]


def _can_adopt_home(session_id: str) -> bool:
    """True when *session_id* may become a card's home on ``--takeover``.

    A cron run (``cron_<job>_<ts>``) or a ``delegate_task`` child is a sweep,
    not a conversation: adopting would pull the card out of its home chat and
    route its pings nowhere. The takeover event still records the actor.
    """
    if session_id.startswith("cron_"):
        return False
    try:
        from agent.delegation_context import is_delegated_child_process_context

        return not is_delegated_child_process_context()
    except Exception:
        return not os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT")
_HOME_UNREAD = object()


def _read_home_session(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row["session_id"] if row is not None else None


def record_foreign_action(
    conn: sqlite3.Connection, task_id: str, action: str, actor: MutationActor,
    *, home_before: Any = _HOME_UNREAD, subscribe: bool = True,
) -> Optional[str]:
    """Append the audit comment for an overridden foreign-session mutation.

    An ``--operator`` override records an ``operator_override`` event only:
    no takeover comment. A status/timing verb (:data:`FOREIGN_CHANGE_ACTIONS`)
    additionally posts the FOREIGN CHANGE comment + ``foreign_change`` event,
    for ``--operator`` and ``--takeover`` alike, so the home session is told.

    ``home_before`` is the home read BEFORE the guarded mutation ran; the
    mutation itself may have re-stamped ``tasks.session_id`` (``update
    --session``), and the audit must name the displaced home (C7 k103).

    Returns the new home session when the takeover re-homed the card.
    ``subscribe=False`` (the in-transaction caller) leaves the post-commit
    chat subscription to the caller.
    """
    sess = ", ".join(actor.session_ids) or "no-session"
    prev_home = (
        _read_home_session(conn, task_id)
        if home_before is _HOME_UNREAD else home_before
    )
    if actor.operator:
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if last is not None and last["kind"] == "operator_override":
            try:
                prev = json.loads(last["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                prev = {}
            if (
                isinstance(prev, dict)
                and prev.get("action") == action
                and prev.get("reason") == actor.operator
            ):
                # The mutator already recorded this override itself
                # (request-changes' operator send-back); one event per call.
                return None
        with write_txn(conn, allow_nested=True):
            _append_event(
                conn,
                task_id,
                "operator_override",
                {
                    "action": action,
                    "reason": actor.operator,
                    "by_sessions": list(actor.session_ids),
                    "by_profile": actor.profile,
                    "home": prev_home,
                },
            )
        _announce_foreign_change(conn, task_id, action, actor, prev_home)
        return None
    new_home = (
        actor.session_ids[0]
        if _takeover_transfers_home(action, actor)
        and actor.session_ids
        and _can_adopt_home(actor.session_ids[0])
        else None
    )
    payload = {
        "action": action,
        "reason": actor.foreign_ok,
        "by_sessions": list(actor.session_ids),
        "by_profile": actor.profile,
        "by_chat": _ambient_session_env("HERMES_SESSION_CHAT_NAME")
        or _ambient_session_env("HERMES_SESSION_CHAT_ID")
        or None,
        "home": prev_home,
        # What a restore needs: the session and the chats the card belonged
        # to BEFORE this takeover, and whether the home moved.
        "previous_session": prev_home,
        "previous_home": _home_conversation(conn, task_id),
        "home_transfer": bool(new_home),
    }
    if new_home:
        payload["prev_session_id"] = prev_home
        payload["session_id"] = new_home
        # The chat the post-commit subscribe adds: what a restore removes.
        taker_platform = _ambient_session_env("HERMES_SESSION_PLATFORM")
        taker_chat_id = _ambient_session_env("HERMES_SESSION_CHAT_ID")
        if taker_platform and taker_chat_id:
            payload["taker_chat"] = {
                "platform": taker_platform, "chat_id": taker_chat_id,
                "thread_id": _ambient_session_env("HERMES_SESSION_THREAD_ID"),
            }
    with write_txn(conn, allow_nested=True):
        if new_home:
            conn.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?", (new_home, task_id)
            )
        _append_event(conn, task_id, "takeover", payload)
    session_ref = None
    if actor.session_ids:
        try:
            session_ref = derive_session_ref(actor.session_ids[0])
        except Exception:
            session_ref = None
    add_comment(
        conn,
        task_id,
        author=actor.profile or "user",
        body=(
            f"takeover: foreign-session action by {sess} "
            f"({actor.profile or 'unknown'}): {actor.foreign_ok} [{action}]"
            + (
                f" -- card re-homed to {new_home}" if new_home
                else f" -- home kept ({prev_home})"
                if action in REHOME_ON_TAKEOVER_ACTIONS else ""
            )
        ),
        session_ref=session_ref,
    )
    _announce_foreign_change(conn, task_id, action, actor, prev_home)
    if new_home and subscribe:
        _subscribe_new_home(conn, task_id)
    return new_home


def _subscribe_new_home(conn: sqlite3.Connection, task_id: str) -> None:
    # Pings follow the new home: subscribe the taker's chat. Best effort --
    # notification bookkeeping must never fail the mutation.
    try:
        from tools.kanban_tools import subscribe_calling_session

        # takeover=True: the wake MOVES to the taker (t_74bf5296).
        subscribe_calling_session(conn, task_id, takeover=True)
    except Exception:
        pass


def _home_session_guarded(action: str, task_param: str = "task_id"):
    """Decorate a status/ownership mutator with the home-session guard.

    ``task_param`` names the parameter holding the card being mutated
    (``child_id`` for :func:`link_tasks`, whose status a link can demote).
    The contract test enumerates writers of ``tasks.status/assignee/priority/``
    ``session_id`` plus dispatch-intent events. Each carries this decorator or
    is an execution-lane internal listed in its ``EXECUTION_LANE`` table.
    """

    def deco(fn):
        sig = inspect.signature(fn)
        if task_param not in sig.parameters:
            raise TypeError(f"{fn.__name__} has no parameter {task_param!r}")

        @functools.wraps(fn)
        def wrapper(conn, *args, **kwargs):
            if _MUTATION_ACTOR.get() is None or kwargs.get("dry_run"):
                return fn(conn, *args, **kwargs)
            task_id = sig.bind_partial(conn, *args, **kwargs).arguments.get(task_param)
            override = check_home_session(conn, str(task_id), action)
            home_before = (
                _read_home_session(conn, str(task_id))
                if override is not None else _HOME_UNREAD
            )
            # Fast refusal above; the AUTHORITATIVE check re-runs inside the
            # mutator's own write transaction (see ``write_txn``), so a restamp
            # between this read and the write cannot slip a foreign mutation
            # through (C5 TOCTOU, PR #951 review). It also re-reads the home
            # under the same lock, before any write (C7 k103 audit).
            pending = {
                "task_id": str(task_id), "action": action,
                "actor": _MUTATION_ACTOR.get(), "done": False, "result": override,
                "home_before": home_before,
            }
            token = _MUTATION_ACTOR.set(None)
            pending_token = _PENDING_HOME_CHECK.set(pending)
            try:
                result = fn(conn, *args, **kwargs)
            finally:
                _PENDING_HOME_CHECK.reset(pending_token)
                _MUTATION_ACTOR.reset(token)
            override = pending["result"]
            if pending.get("recorded"):
                if pending.get("new_home"):
                    _subscribe_new_home(conn, str(task_id))
            elif override is not None and _mutation_succeeded(result):
                record_foreign_action(
                    conn, str(task_id), action, override,
                    home_before=pending["home_before"],
                )
            return result

        wrapper.__home_session_action__ = action
        wrapper.__home_session_task_param__ = task_param
        return wrapper

    return deco


@_home_session_guarded("update --session")
def set_task_session(
    conn: sqlite3.Connection, task_id: str, session_id: Optional[str]
) -> bool:
    """(Re)stamp a card's home session. ``None`` clears it (unstamped)."""
    sid = (str(session_id).strip() or None) if session_id else None
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?", (sid, task_id)
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "session_restamped", {"session_id": sid}
        )
    return True


def _origin_line(body: Optional[str]) -> Optional[str]:
    """The card's ``origin: ...`` provenance line (its first non-empty line)."""
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line if line.lower().startswith("origin:") else None
    return None

# ---------------------------------------------------------------------------
# Needs-input pager (t_c8ca40b4). A priority-chain card that blocks on a human
# question paged nobody: 3x in 24 h on the DPX chain, ~12 h of silence. The
# gateway dispatcher pages the card's origin channel (dedup + re-page lives in
# gateway/kanban_watchers.py); this half picks the cards and holds the opt-out.
# ---------------------------------------------------------------------------

NEEDS_INPUT_PAGE_MIN_PRIORITY = 100
NEEDS_INPUT_PAGE_ALERTS_PRIORITY = 200
_NEEDS_INPUT_PAGE_OFF = "needs_input_page_off"
_NEEDS_INPUT_PAGE_ON = "needs_input_page_on"
# ``origin: discord <name> (<numeric channel id>) · session ...`` (see
# format_origin_line). The id is the LAST parenthetical of the first `` · ``
# field, so a chat name with its own parentheses still resolves. Numeric ids
# only: a channel NAME is never a delivery target.
_ORIGIN_DISCORD_CHANNEL_RE = re.compile(r"^origin:\s*discord\b.*\((\d{15,22})\)\s*$", re.I)


def origin_discord_channel(body: Optional[str]) -> Optional[str]:
    """The numeric Discord channel id in the card's ``origin:`` line, else None."""
    line = _origin_line(body)
    where = line.split(" \u00b7 ", 1)[0] if line else ""
    match = _ORIGIN_DISCORD_CHANNEL_RE.match(where) if where else None
    return match.group(1) if match else None


def needs_input_page_enabled(conn: sqlite3.Connection, task_id: str) -> bool:
    """False once ``edit --no-page`` opted the card out (latest toggle wins)."""
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, _NEEDS_INPUT_PAGE_OFF, _NEEDS_INPUT_PAGE_ON),
    ).fetchone()
    return row is None or row["kind"] == _NEEDS_INPUT_PAGE_ON


def set_needs_input_page(
    conn: sqlite3.Connection,
    task_id: str,
    enabled: bool,
    *,
    operator: Optional[str] = None,
) -> bool:
    """Opt a card out of (or back into) the needs-input pager.

    Returns False for an unknown id. A change records ``needs_input_page_off``
    / ``needs_input_page_on``; setting the current value is a silent no-op.
    """
    with write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return False
        if needs_input_page_enabled(conn, task_id) != bool(enabled):
            _append_event(
                conn, task_id,
                _NEEDS_INPUT_PAGE_ON if enabled else _NEEDS_INPUT_PAGE_OFF,
                {"operator": operator},
            )
    return True


_BLOCK_REASON_EVENTS = ("blocked", "block_loop_detected")
# An operator comment that answers the block (t_dfc938c4: t_e6b3713d re-paged
# "needs a ruling" 80 min after "APOLLO 14:45 — B4 RULED"). All of:
#  * author is an operator/Ace author (kanban_worker_policy.RULING_AUTHORS, the
#    same label trust find_ruled_parent uses), not a delegated child
#    (SUBAGENT_AUTHOR_MARKER), and the comment carries no worker run_id (a
#    dispatched run on this card stamps one; ``--author default`` cannot drop it);
#  * the first line opens with ``APOLLO`` and carries an uppercase RULED /
#    RULING(S) / ANSWERED whose clause (text since the last : ; , . ( ) or dash)
#    holds no negator, so "NEEDS (AN OPERATOR) RULING", "NO RULING yet",
#    "NOT ANSWERED" stay pending;
#  * it was written after the card's latest block event, ordered by event id
#    (``created_at`` is whole seconds; a same-second ruling must still count and
#    a same-second re-block must still page).
_RULING_ANSWER_LEAD_RE = re.compile(r"\AAPOLLO\b")
_RULING_ANSWER_WORD_RE = re.compile(r"\b(?:RULED|RULINGS?|ANSWERED)\b")
_RULING_CLAUSE_BREAK_RE = re.compile(r"[:;,.()\u2013\u2014]")
_RULING_ANSWER_NEGATORS = frozenset({
    "needs", "need", "needing", "no", "not", "awaiting", "await", "pending",
    "for", "without", "requesting", "request", "asks", "ask", "wants", "want",
})


def _is_ruling_answer(body: str) -> bool:
    line = (body or "").lstrip().split("\n", 1)[0]
    if not _RULING_ANSWER_LEAD_RE.match(line):
        return False
    for m in _RULING_ANSWER_WORD_RE.finditer(line):
        clause = _RULING_CLAUSE_BREAK_RE.split(line[:m.start()])[-1]
        if not any(w.lower() in _RULING_ANSWER_NEGATORS for w in re.findall(r"[A-Za-z]+", clause)):
            return True
    return False


def _ruled_since_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when an operator ruling comment was written after the card's latest block."""
    from .kanban_worker_policy import RULING_AUTHORS

    row = conn.execute(
        "SELECT MAX(id) AS id FROM task_events WHERE task_id = ? AND kind IN (?, ?)",
        (task_id, *_BLOCK_REASON_EVENTS),
    ).fetchone()
    if row is None or row["id"] is None:
        return False
    # add_comment logs one ``commented`` event in the comment's own txn, so the
    # newest N comments are the N written after the block. A comment written by a
    # path that logs no event only shrinks that window (fails toward paging).
    n = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'commented' AND id > ?",
        (task_id, row["id"]),
    ).fetchone()[0]
    if not n:
        return False
    for r in conn.execute(
        "SELECT author, body, run_id FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, int(n)),
    ):
        author = (r["author"] or "").strip()
        if author.endswith(SUBAGENT_AUTHOR_MARKER.strip()) or author.lower() not in RULING_AUTHORS:
            continue
        if r["run_id"] is not None:
            continue  # a dispatched worker run on this card cannot answer its own block
        if _is_ruling_answer(r["body"] or ""):
            return True
    return False


def _block_was_rekinded_no_open_parent(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the latest block was a ``dependency`` ask re-kinded to
    ``needs_input`` because no open parent existed: a time/external wait, not
    a decision, so it must not page while ``blocked`` (a ``triage`` escalation
    still pages)."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, *_BLOCK_REASON_EVENTS),
    ).fetchone()
    try:
        payload = json.loads(row["payload"]) if row and row["payload"] else {}
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("rekind_reason") == "no_open_parent"


def _latest_block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, *_BLOCK_REASON_EVENTS),
    ).fetchone()
    try:
        payload = json.loads(row["payload"]) if row and row["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    return str((payload or {}).get("reason") or "").strip() if isinstance(payload, dict) else ""


def needs_input_page_candidates(
    conn: sqlite3.Connection,
    *,
    include_dependency: bool = False,
    min_priority: int = NEEDS_INPUT_PAGE_MIN_PRIORITY,
) -> list[dict]:
    """Cards waiting on a human ruling that should page their origin channel.

    A card qualifies when it sits in ``blocked`` with kind ``needs_input`` (a
    same-kind re-block escalates to ``triage`` and still waits on a human, so
    that counts too) or,
    with ``include_dependency``, waits as ``dependency`` on a parent that is
    itself ``blocked``), was not opted out, has no operator ``APOLLO … RULED``
    comment newer than its latest block (answered, waiting on an unblock; see
    ``_ruled_since_block``), and has ``priority >= min_priority``
    OR an ``origin:`` line naming a numeric Discord channel. Each item carries
    ``channel`` (origin channel id or None) and ``alerts`` (priority >= 200).
    """
    rows = list(conn.execute(
        "SELECT id, title, body, priority, status, block_kind FROM tasks "
        "WHERE status IN ('blocked', 'triage') AND block_kind = 'needs_input' ORDER BY id"
    ))
    if include_dependency:
        rows += list(conn.execute(
            "SELECT id, title, body, priority, status, block_kind FROM tasks t "
            "WHERE status IN ('todo', 'blocked') AND block_kind = 'dependency' "
            "AND EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "            WHERE l.child_id = t.id AND COALESCE(l.kind, ?) = ? "
            "            AND p.status = 'blocked') ORDER BY id",
            (DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
        ))
    out: list[dict] = []
    for row in rows:
        priority = int(row["priority"] or 0)
        channel = origin_discord_channel(row["body"])
        if priority < int(min_priority) and channel is None:
            continue
        if not needs_input_page_enabled(conn, row["id"]):
            continue
        # A re-kinded dependency is a time/external wait only while it sits in
        # ``blocked``; once it recurs into ``triage`` it needs a human decision.
        if row["block_kind"] == "needs_input" and (
            _ruled_since_block(conn, row["id"])
            or (row["status"] == "blocked" and _block_was_rekinded_no_open_parent(conn, row["id"]))
        ):
            continue
        if row["block_kind"] == "dependency":
            parents = [
                (r["id"], _latest_block_reason(conn, r["id"]))
                for r in conn.execute(
                    "SELECT p.id AS id FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                    "WHERE l.child_id = ? AND COALESCE(l.kind, ?) = ? "
                    "AND p.status = 'blocked' ORDER BY p.id",
                    (row["id"], DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
                )
            ]
            reason = "; ".join(
                f"waiting on blocked parent {pid}: {why or '(no reason)'}" for pid, why in parents
            )
        else:
            reason = _latest_block_reason(conn, row["id"])
        out.append({
            "task_id": row["id"],
            "title": row["title"],
            "priority": priority,
            "kind": row["block_kind"],
            "reason": reason or "(no reason given)",
            "channel": channel,
            "alerts": priority >= NEEDS_INPUT_PAGE_ALERTS_PRIORITY,
        })
    return out


# ---------------------------------------------------------------------------
# Near-duplicate guard (opt-in per create surface: CLI + kanban_create tool)
# ---------------------------------------------------------------------------
# Sibling sessions re-filing the same card minutes apart burn worker slots and
# race competing PRs onto the same files. Similarity is a token-set Jaccard over
# the title plus the first ``NEAR_DUP_BODY_CHARS`` of the body (``origin:``
# lines stripped -- every card carries one). Measured on the live board
# (24h, 773 cards, 2026-09-26): 26 pairs scored >= 0.8; every pair whose title
# token sets were IDENTICAL was a real duplicate, while every sharded fan-out
# ("SHARD 2/4" vs "3/4", "agent tranche" vs "gateway tranche") differs in the
# title. So an identical title + score >= threshold REFUSES; any other pair
# over the threshold is a WARNING recorded on the new card.
NEAR_DUP_THRESHOLD = 0.8
NEAR_DUP_WINDOW_SECONDS = 24 * 3600
NEAR_DUP_BODY_CHARS = 200

# Titles shorter than this are too generic to call duplicates ("fix", "test x").
NEAR_DUP_MIN_TITLE_TOKENS = 3
_NEAR_DUP_TOKEN_RE = re.compile(r"[a-z0-9]+")


class NearDuplicateError(ValueError):
    """``create_task(duplicate_guard=True)`` refused a near-duplicate card."""

    def __init__(self, message: str, duplicates: list[dict]):
        super().__init__(message)
        self.duplicates = duplicates


def _near_dup_body(body: Optional[str]) -> str:
    kept = [
        line for line in (body or "").splitlines()
        if not line.strip().lower().startswith("origin:")
    ]
    return "\n".join(kept).strip()[:NEAR_DUP_BODY_CHARS]


def near_dup_tokens(title: Optional[str], body: Optional[str]) -> frozenset:
    text = f"{title or ''} {_near_dup_body(body)}".lower()
    return frozenset(_NEAR_DUP_TOKEN_RE.findall(text))


def _title_tokens(title: Optional[str]) -> frozenset:
    return frozenset(_NEAR_DUP_TOKEN_RE.findall((title or "").lower()))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_near_duplicates(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str],
    tenant: Optional[str] = None,
    now: Optional[int] = None,
    threshold: float = NEAR_DUP_THRESHOLD,
    window_seconds: int = NEAR_DUP_WINDOW_SECONDS,
) -> list[dict]:
    """Non-archived cards created in the window that look like ``title``/``body``.

    Returns ``[{"id", "title", "score", "same_title", "created_at", "status"}]``
    sorted by score, best first. ``same_title`` marks the refusal class.
    """
    title_toks = _title_tokens(title)
    if len(title_toks) < NEAR_DUP_MIN_TITLE_TOKENS:
        return []
    new_toks = near_dup_tokens(title, body)
    since = int(now if now is not None else time.time()) - int(window_seconds)
    rows = conn.execute(
        # Only the head of a body is compared; don't page whole bodies in.
        "SELECT id, title, substr(body, 1, 2000) AS body, created_at, status FROM tasks "
        "WHERE created_at >= ? AND status != 'archived' AND tenant IS ?",
        (since, tenant),
    ).fetchall()
    hits: list[dict] = []
    for row in rows:
        score = _jaccard(new_toks, near_dup_tokens(row["title"], row["body"]))
        if score < threshold:
            continue
        hits.append({
            "id": row["id"],
            "title": row["title"],
            "score": round(score, 3),
            "same_title": _title_tokens(row["title"]) == title_toks,
            "created_at": row["created_at"],
            "status": row["status"],
        })
    hits.sort(key=lambda h: (-h["score"], -int(h["created_at"] or 0)))
    return hits


def near_duplicate_warning(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """Payload of the ``near_duplicate_warning`` event on ``task_id``, if any."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'near_duplicate_warning' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def _normalize_skills(skills: Iterable[str]) -> list[str]:
    """Strip, drop empties, dedupe (order kept); refuse commas and toolset names.

    Shared by :func:`create_task` and :func:`set_task_skills` so ``create
    --skill`` and ``edit --skill`` accept exactly the same names.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    # Collect all toolset-name confusions up front so the user sees the
    # whole list at once. Raising on the first hit is friendly when the
    # input has one mistake, but agents that confuse skills with toolsets
    # usually pass several at once (`skills=["web", "browser", "terminal"]`)
    # and serial-correcting one per failure round-trips wastes tokens.
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


# A ``[milestone] QA`` card runs Argus on the sdlc-review procedure; the kernel
# only force-loads that skill for review-lane spawns (off), so create attaches it
# (kanban-review-lane-lint's "lack skill sdlc-review" finding, t_c9af70b6).
MILESTONE_QA_TITLE_PREFIX = "[milestone] qa"
MILESTONE_QA_SKILL = "sdlc-review"


def is_milestone_qa_title(title: Optional[str]) -> bool:
    return (title or "").strip().lower().startswith(MILESTONE_QA_TITLE_PREFIX)


def max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM task_events").fetchone()
    return int(row[0] or 0)


def created_skills_auto_added(
    conn: sqlite3.Connection, task_id: str, *, after_event_id: int = 0,
) -> list[str]:
    """Skills ``create_task`` auto-attached to this card, read from its
    created event; ``after_event_id`` ignores a created event at or below
    that id (an idempotent hit returning an older card)."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "AND id > ? ORDER BY id LIMIT 1", (task_id, after_event_id),
    ).fetchone()
    try:
        payload = json.loads(row["payload"]) if row and row["payload"] else {}
    except Exception:
        return []
    return list(payload.get("skills_auto_added") or [])


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    parents_kind: Optional[str] = None,
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    flagship_override_reason: Optional[str] = None,
    flagship_override_author: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    brain: Optional[str] = None,
    pin_sub_reason: Optional[str] = None,
    pin_sub_fallback: bool = False,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    forced_status: Optional[str] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    project_id: Optional[str] = None,
    project_source_task_id: Optional[str] = None,
    creator_task_id: Optional[str] = None,
    completion_contract: Optional[str] = None,
    session_explicit: bool = False,
    duplicate_guard: bool = False,
    force_reason: Optional[str] = None,
    require_home: bool = False,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    ``parents_kind`` sets the edge semantics for every parent link
    (:data:`VALID_LINK_KINDS`, default ``blocks``). Pass
    ``derived-from`` when the parent merely DISCOVERED this work — an
    audit/survey/investigation filing remediation — so the new task is
    independently dispatchable and never held on the parent finishing.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``hermes --skills ...``. Use this to pin a task to a
    specialist skill (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).

    ``model_override`` / ``provider_override`` pin the worker to a specific
    model (and optionally its provider) without touching the profile's
    config — passed to the worker as ``-m <model> [--provider <name>]``.
    ``provider_override`` requires ``model_override``.

    ``reasoning_effort`` pins the worker's thinking depth for this task
    (``minimal``…``ultra``, or ``none`` to disable thinking), passed as
    ``--reasoning <level>``. It is independent of ``model_override``: a task
    can run the profile's own model at a different depth.

    ``project_source_task_id`` is an internal cross-profile fallback for a
    worker-created child. When the active profile cannot resolve ``project_id``
    in its own projects.db, a matching canonical project-linked task in this
    board can supply the repo and branch convention. Its literal worktree is
    never reused; the new task still gets its own task-id-keyed path.

    ``duplicate_guard`` (set by the CLI and ``kanban_create`` tool) runs
    :func:`find_near_duplicates` inside the write transaction: an identical
    title scoring >= :data:`NEAR_DUP_THRESHOLD` raises
    :class:`NearDuplicateError` unless ``force_reason`` is given (ledgered as a
    ``near_duplicate_forced`` event); other hits are recorded as a
    ``near_duplicate_warning`` event on the new card. A same-title hit only
    warns when the matched card is ``done`` (a refile after completion) or when
    the caller passed an ``idempotency_key`` that matched no live card (the
    documented recurring-automation pattern owns its own dedup).

    ``creator_task_id``: inherit durable session/subscriptions independently of
    dependency edges; an explicit ``session_id`` still wins.
    ``completion_contract`` (``kanban_pr_acceptance.validate_contract``) is the
    PR acceptance contract recorded on the card.
    ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.
    """
    force_reason = (force_reason or "").strip() or None
    model_override = (model_override or "").strip() or None
    provider_override = (provider_override or "").strip() or None
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    brain = normalize_card_brain(brain)
    if provider_override and not model_override:
        raise ValueError("provider_override requires a model_override")
    from hermes_cli.model_policy import pin_sub_arg_error, validate_route_provider

    pin_sub_error = pin_sub_arg_error(
        model_override, provider_override, pin_sub_reason,
        pin_sub_fallback=bool(pin_sub_fallback),
    )
    if pin_sub_error:
        raise ValueError(pin_sub_error)
    pin_sub_reason = (pin_sub_reason or "").strip() or None
    validate_route_provider(model_override, provider_override, pin_sub_reason=pin_sub_reason)
    model_override, provider_override = _resolve_stored_model_pair(
        model_override, provider_override
    )
    validate_route_provider(model_override, provider_override, pin_sub_reason=pin_sub_reason)
    from hermes_cli.model_policy import validate_worker_model

    flagship_override_reason = validate_worker_model(
        model_override,
        allow_flagship_reason=flagship_override_reason,
    )
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    from hermes_cli.kanban_db_graph import inherit_creator_origin
    from hermes_cli.kanban_pr_acceptance import validate_contract

    completion_contract = validate_contract(completion_contract)
    # ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    # an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (_board_meta_for(board).get("project_id") or "").strip() or None
        except Exception:
            pass
    if workspace_kind is None:
        workspace_kind = "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")

    # Home-session stamp at birth -- the ONE choke point every create path
    # (CLI, kanban tool, swarm, dashboard, library callers) flows through, so
    # no card can land without a home (never NULL) or without an origin line.
    parents = tuple(parents or ())
    created_by = created_by or _ambient_session_env("HERMES_SESSION_PROFILE") or None
    requested_session_id, unstamped_body = session_id, body
    birth = _resolve_birth_session(
        conn, session_id, parents, explicit=session_explicit,
        creator_task_id=creator_task_id,
    )
    session_id, inherited_origin = birth
    # ``require_home`` (the ``hermes kanban create`` CLI and the
    # ``kanban_create`` tool): a caller with NO session identity and no homed
    # parent/worker lineage would mint an ``unhomed`` card that no session can
    # drive. Refuse a worker / cron / gateway / script minter (D-O1,
    # t_6281f908; t_09fea045 for scripts); a hand-typed CLI/TUI create is
    # allowed and the CLI warns (D-O2). ``--unhomed`` / ``--session none`` /
    # ``--home operator`` is an explicit choice and always allowed.
    create_origin = classify_create_origin()
    if (
        require_home
        and not session_explicit
        and is_unhomed(session_id)
        and create_origin != CREATE_ORIGIN_HAND
        and (os.environ.get(ALLOW_UNHOMED_CREATE_ENV) or "").strip() != "1"
    ):
        raise UnhomedCreateError(
            f"kanban: refused create ({create_origin}): no home session (no "
            "session identity, no homed --parent). An unhomed card is "
            "undrivable: no session can unblock/complete it. Pass --session "
            "<sid> (or --parent <homed card>), --home operator (fleet operator "
            f"pseudo-session {OPERATOR_HOME_SESSION}, owned by any "
            f"{'/'.join(sorted(OPERATOR_PROFILES))} session), or --unhomed to "
            "mint it unhomed on purpose (the orphan watch then infers a home)."
        )
    body = stamp_origin_body(
        body,
        inherited_origin or format_origin_line(session_id, created_by=created_by),
    )

    # Inherit the board's scoped project when the caller didn't name one, so a
    # project-scoped board anchors every new task to that project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    project_id, project_obj, project_repo, workspace_kind = _resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path
    )
    parents = tuple(p for p in parents if p)
    # Raise on a bad kind before any row is written, and resolve NULL to
    # ``blocks`` so the status decision below reads one value.
    parents_kind = normalize_link_kind(parents_kind)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `hermes --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: Optional[list[str]] = None
    if skills is not None:
        skills_list = _normalize_skills(skills)
    # Auto-attach sdlc-review to a ``[milestone] QA`` card (t_c9af70b6); the
    # created event records it so the addition is never silent.
    skills_auto_added: list[str] = []
    if is_milestone_qa_title(title) and MILESTONE_QA_SKILL not in (skills_list or []):
        skills_list = [*(skills_list or []), MILESTONE_QA_SKILL]
        skills_auto_added.append(MILESTONE_QA_SKILL)


    pr_owner_forced: Optional[dict] = None
    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]
        # One running card per PR (t_cb70d390): a rebase helper for a PR
        # whose owner card is running is refused at birth, owner named.
        # ``force_reason`` (CLI ``--force "<reason>"``) overrides it; the
        # override is ledgered as a ``pr_owner_forced`` event below.
        from hermes_cli import kanban_pr_owner as _kpo

        try:
            _kpo.refuse_rebase_card_at_birth(conn, idempotency_key)
        except _kpo.PrOwnerBusyError as exc:
            if not force_reason:
                raise
            pr_owner_forced = {"owner": exc.owner["task_id"], "pr": exc.owner["pr"],
                               "reason": force_reason}

    now = int(time.time())

    # Resolve workspace_path from board-level default_workdir when the
    # caller did not specify one explicitly. Board defaults represent
    # persistent project checkouts, so only persistent workspace kinds may
    # inherit them. Scratch workspaces are auto-deleted on completion and
    # must stay under the per-board scratch root created by
    # ``resolve_workspace``; inheriting ``default_workdir`` for a scratch
    # task would point cleanup at the user's source tree (#28818). The
    # containment guard in ``_cleanup_workspace`` is the safety rail, but
    # we also stop the bad state from being created in the first place.
    if (
        workspace_path is None
        and project_repo is None
        and workspace_kind in {"dir", "worktree"}
    ):
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            # ``allow_nested=True``: graph builders (kanban_swarm.create_swarm)
            # compose create_task calls under one outer commit so the
            # dispatcher can never observe a partially constructed graph.
            with write_txn(conn, allow_nested=True):
                # C5 #44 (PR #987): a parent re-homed between the birth
                # resolution above and this write lock must still be the
                # home the child is born with. Re-resolve under the lock,
                # BEFORE every check that reads the final home or body.
                rebirth = _resolve_birth_session(
                    conn, requested_session_id, parents, explicit=session_explicit,
                    creator_task_id=creator_task_id,
                )
                if rebirth != birth:
                    birth = rebirth
                    session_id, inherited_origin = rebirth
                    body = stamp_origin_body(
                        unstamped_body,
                        inherited_origin
                        or format_origin_line(session_id, created_by=created_by),
                    )
                ruled_mint = None
                # Determine task status from parent status, unless the caller
                # parks it directly in blocked for human-ops review or in
                # triage for a specifier.
                if initial_status == "blocked":
                    task_status = "blocked"
                    if parents:
                        missing = _missing_task_ids(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                elif forced_status:
                    # Fan-out brake: a policy layer (kanban_worker_policy) has
                    # decided this creation must PARK rather than queue — e.g.
                    # a dispatched worker creating a child card. Parent ids are
                    # still validated so the link rows can't dangle, but no
                    # parent-gated promotion applies: the card sits until a
                    # human moves it.
                    if forced_status not in VALID_STATUSES:
                        raise ValueError(
                            f"forced_status must be one of {sorted(VALID_STATUSES)}"
                        )
                    task_status = forced_status
                    if parents:
                        missing = _missing_task_ids(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                    if task_status == "triage":
                        # r16 K: a child minted under an already-ruled parent
                        # has nothing left to rule -- it lands in todo (the
                        # normal parent gating then promotes it) instead of a
                        # triage card Apollo hand-resolves every sweep. Only an
                        # explicit ``NEEDS RULING:`` body line keeps the park.
                        from . import kanban_worker_policy as _kwp_mint

                        ruled_mint = _kwp_mint.resolve_ruled_mint(
                            conn, parents=parents, body=body,
                        )
                        if ruled_mint is not None:
                            task_status = "todo"
                elif triage:
                    task_status = "triage"
                else:
                    task_status = "ready"
                    if parents:
                        missing = _missing_task_ids(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo — but only
                        # a 'blocks' edge gates. A 'derived-from' parent is
                        # provenance, so the task stays immediately dispatchable.
                        if parents_kind == LINK_KIND_BLOCKS:
                            rows = conn.execute(
                                "SELECT status FROM tasks WHERE id IN "
                                "(" + ",".join("?" * len(parents)) + ")",
                                parents,
                            ).fetchall()
                            if any(r["status"] != "done" for r in rows):
                                task_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _missing_task_ids(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

                # Project-linked worktree: a fresh worktree dir under the repo
                # plus a deterministic branch (project slug + task id). Together
                # these kill the random ``wt/<task-id>`` worker fallback and the
                # unanchored ``.worktrees/<id>`` under the dispatcher's cwd.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(
                            project_repo, ".worktrees", task_id
                        )
                    if not branch_name:
                        branch_name = _project_branch_name(project_obj, task_id, title)

                # Placeholder-assignee lint for worker-minted cards: a human /
                # operator lane ('apollo', 'human:x', 'default', ...) is never
                # spawnable for a worker's child. Ruled-parent children ride
                # the parent's lane; anything else falls back to
                # kanban.default_assignee or the create is refused.
                from . import kanban_worker_policy as _kwp_lint

                assignee, assignee_remap, assignee_err = (
                    _kwp_lint.resolve_worker_assignee(
                        assignee,
                        parent_lane=(
                            ruled_mint.get("parent_assignee") if ruled_mint else None
                        ),
                    )
                    if assignee and _kwp_lint.is_dispatched_worker()
                    else (assignee, None, None)
                )
                if assignee_err:
                    raise ValueError(assignee_err)
                assignee = _canonical_assignee(assignee) if assignee else assignee

                if tenant is None and parents:
                    # Parent order breaks ties in this soft namespace; an
                    # explicit tenant wins (upstream initial_task_state).
                    tenant = next((
                        r["tenant"] for r in conn.execute(
                            "SELECT id, tenant FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")", parents,
                        ).fetchall() if r["tenant"]
                    ), None)
                near_dups: list[dict] = []
                same: list[dict] = []
                if duplicate_guard:
                    near_dups = find_near_duplicates(
                        conn, title=title, body=body, tenant=tenant, now=now,
                    )
                    # An explicit idempotency_key that matched nothing is the
                    # caller's own dedup decision (recurring automation), and a
                    # done card is a refile, not a race: both only warn.
                    same = [] if idempotency_key else [
                        d for d in near_dups
                        if d["same_title"] and d.get("status") != "done"
                    ]
                    if same and not force_reason:
                        best = same[0]
                        raise NearDuplicateError(
                            f"near-duplicate: {best['id']} has the same title "
                            f"(similarity {best['score']:.2f}, created "
                            f"{max(0, now - int(best['created_at']))}s ago) -- "
                            "comment on it instead, or re-run with "
                            "--force <reason> / force_reason to file anyway",
                            same,
                        )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        reasoning_effort, pin_sub_reason, pin_sub_fallback,
                        goal_mode, goal_max_turns, session_id, completion_contract, brain
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        task_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        branch_name,
                        project_id,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                        model_override,
                        provider_override,
                        reasoning_effort,
                        pin_sub_reason,
                        1 if (pin_sub_reason and pin_sub_fallback) else 0,
                        1 if goal_mode else 0,
                        int(goal_max_turns) if goal_max_turns is not None else None,
                        session_id,
                        completion_contract,
                        brain,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links "
                        "(parent_id, child_id, kind) VALUES (?, ?, ?)",
                        (pid, task_id, parents_kind),
                    )
                # Notify-sub inheritance (ACK-edge: the originating channel
                # still hears about a child that BLOCKs, not just the final
                # fan-in) is handled by the single-owner helper below —
                # _inherit_notify_subs copies every routing/delivery column.
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "parents_kind": parents_kind if parents else None,
                        "tenant": tenant,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                        "project_id": project_id,
                        "creator_task_id": creator_task_id,
                        "skills": list(skills_list) if skills_list else None,
                        **({"skills_auto_added": skills_auto_added}
                           if skills_auto_added else {}),
                        "goal_mode": bool(goal_mode) or None,
                        "model_override": model_override,
                        "provider_override": provider_override,
                        "minted_by": _minted_by(create_origin),
                        **({"brain": brain} if brain else {}),
                        **({"pin_sub_reason": pin_sub_reason,
                            "pin_sub_fallback": bool(pin_sub_fallback)}
                           if pin_sub_reason else {}),
                    },
                )
                if pin_sub_reason:
                    from hermes_cli.model_policy import sub_pin_comment

                    add_comment(
                        conn,
                        task_id,
                        flagship_override_author or created_by or "operator",
                        sub_pin_comment(provider_override, pin_sub_reason,
                                        fallback=bool(pin_sub_fallback)),
                    )
                if near_dups:
                    forced = force_reason and bool(same)
                    _append_event(
                        conn,
                        task_id,
                        "near_duplicate_forced" if forced else "near_duplicate_warning",
                        {
                            "duplicates": [
                                {k: d[k] for k in ("id", "score", "same_title")}
                                for d in near_dups[:5]
                            ],
                            **({"reason": force_reason} if forced else {}),
                        },
                    )
                if pr_owner_forced:
                    _append_event(conn, task_id, "pr_owner_forced", pr_owner_forced)
                overlap_hits: list[dict] = []
                if duplicate_guard:
                    # Same-incident gate (t_ba30f0de): another session minted
                    # this incident in the last 30 min. Never refuses; comments
                    # both cards and holds this one in ready for 10 min.
                    from . import kanban_overlap as _kov

                    overlap_hits = _kov.find_overlaps(
                        conn, task_id=task_id, title=title, body=body,
                        session_id=session_id, tenant=tenant, now=now,
                        parents=parents,
                    )
                    if overlap_hits:
                        _kov.record_overlaps(
                            conn, task_id, overlap_hits, now=now,
                            append_event=_append_event, add_comment=add_comment,
                        )
                if flagship_override_reason:
                    from hermes_cli.model_policy import override_comment

                    add_comment(
                        conn,
                        task_id,
                        flagship_override_author or created_by or "operator",
                        override_comment(flagship_override_reason),
                    )
                if ruled_mint is not None:
                    _append_event(
                        conn,
                        task_id,
                        _kwp_mint.RULED_MINT_EVENT,
                        _kwp_mint.ruled_mint_event_payload(ruled_mint),
                    )
                if assignee_remap is not None:
                    _append_event(conn, task_id, "assignee_remapped", assignee_remap)
                if forced_status and task_status == forced_status:
                    # Audit the brake on the card itself so the park is
                    # explicable without reading config: WHY this card is not
                    # in ``ready``, and which knob restores the old behaviour.
                    from hermes_cli import kanban_worker_policy as _kwp

                    _append_event(
                        conn,
                        task_id,
                        "parked_by_policy",
                        _kwp.park_event_payload(task_status),
                    )
                if task_status == "blocked":
                    # Tag the source so dependency resolution can distinguish
                    # this creation-time hold from a later explicit worker or
                    # operator block. Parentless creation holds remain sticky;
                    # a creation hold with a real ``blocks`` edge may release
                    # automatically once every parent is terminal.
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {
                            "reason": "created with initial_status=blocked",
                            "source": "initial_status",
                        },
                    )
                if task_status == "todo":
                    gating = [p for p in parents if _task_status(conn, p) not in ("done", "archived")]
                    if gating:
                        _append_event(
                            conn,
                            task_id,
                            "dependency_wait",
                            {"reason": "parent_not_done", "parent": gating[0]},
                        )
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
                inherit_creator_origin(conn, task_id, creator_task_id, created_at=now)
            if overlap_hits:
                # After commit, fire-and-forget: a create never waits on Discord.
                _kov.notify_logs(board, task_id, overlap_hits)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")


def _board_meta_for(board: Optional[str]) -> dict:
    return read_board_metadata(board if board else get_current_board())


def _project_branch_name(project_obj: Any, task_id: str, title: Optional[str]) -> Optional[str]:
    from hermes_cli import projects_db as _pdb

    try:
        return _pdb.branch_name_for(project_obj, task_id, title=title or "")
    except Exception:
        return None


def _link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )


def _missing_task_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Subset of ``ids`` (order kept) with no ``tasks`` row."""
    ids = list(ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in ids if p not in present]


def _inherit_notify_subs(
    conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *,
    created_at: Optional[int] = None,
) -> None:
    """Copy parents' notify subscriptions to a child, cursor caught up to the
    child's current event so a late ``link_tasks`` never replays history.

    Single owner of inheritance (create_task, link_tasks, decompose). It must
    copy EVERY routing/delivery column: dropping ``chat_type`` made DM-originated
    completions wake a fresh group session instead of the originating DM.

    Copies EVERY routing/delivery column (chat_type, user_id_alt, scope_id,
    delivery_mode, delivery_metadata included) — this helper is the single
    owner of subscription inheritance for create_task, link_tasks, and triage
    decomposition. Omitting columns here silently degrades routing: a
    DM-originated child completion falls back to chat_type='group' and wakes
    a fresh group-scoped session instead of the originating DM (issue #73030).
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    # One subscriber chat per card (t_484a3c72): a parent's chat joins only
    # when the child has no live subscriber on that platform yet. Decided per
    # parent row, oldest first, so a child of two parents in two chats ends up
    # with one of them, not both.
    for prow in conn.execute(
        f"SELECT * FROM kanban_notify_subs WHERE task_id IN ({placeholders})"
        " ORDER BY created_at, rowid",
        parent_ids,
    ).fetchall():
        decision, others = _notify_sub_admission(
            conn, task_id=child_id, platform=prow["platform"],
            chat_id=prow["chat_id"], thread_id=prow["thread_id"] or "",
            notifier_profile=prow["notifier_profile"],
        )
        if decision == "keep":
            _log_sub_kept(child_id, others, prow["chat_id"])
            continue
        if decision == "replace":
            _drop_notify_subs(conn, others)
        # One waker per card: an inherited wake row becomes notify when the
        # child already has a waker (else INSERT OR IGNORE would drop the
        # whole row on the one-waker index).
        inherited_mode = prow["delivery_mode"] or "notify"
        if inherited_mode in NOTIFY_WAKE_MODES and prow["platform"] != "api_server" and card_waker(
            conn, child_id,
            exclude=(prow["platform"], prow["chat_id"], prow["thread_id"] or ""),
        ) is not None:
            inherited_mode = "notify"
        conn.execute(
            f"""
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 scope_id, chat_type, notifier_profile, delivery_mode,
                 delivery_metadata, created_at, last_event_id)
            SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,
                   scope_id, COALESCE(chat_type, 'dm'), notifier_profile,
                   ?, delivery_metadata, ?, ?
              FROM kanban_notify_subs
             WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
            """,
            (
                child_id,
                inherited_mode,
                int(created_at if created_at is not None else time.time()),
                cursor,
                prow["task_id"], prow["platform"], prow["chat_id"],
                prow["thread_id"] or "",
            ),
        )


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None

# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
    "completed-desc": "completed_at DESC NULLS LAST, id DESC",
}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    session_ids: Optional[Iterable[str]] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> list[Task]:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    for col, val in (
        ("assignee", _canonical_assignee(assignee)), ("status", status), ("tenant", tenant),
        ("session_id", session_id), ("workflow_template_id", workflow_template_id),
        ("current_step_key", current_step_key),
    ):
        if val is not None:
            query += f" AND {col} = ?"
            params.append(val)
    if session_ids is not None:
        sids = sorted({str(x) for x in session_ids if x})
        if not sids:
            query += " AND 0"
        else:
            query += f" AND session_id IN ({','.join('?' * len(sids))})"
            params.extend(sids)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}")
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


class ReviewHoldRequired(RuntimeError):
    """Reassigning a ``review`` card with an open own PR needs a changes request.

    t_947cea0e (2026-09-29): an operator posted CHANGES REQUESTED as a comment
    and ran ``assign <card> daedalus``. The card stayed in ``review``, no worker
    was ever dispatched, and the hold had no owner (t_dadfedb2).
    """


def review_hold_open_prs(conn: sqlite3.Connection, task_id: str, *, query_fn=None) -> list:
    """The card's own fleet PRs that are OPEN or unreadable while it sits in ``review``.

    Own PRs are what any run recorded (:func:`_card_recorded_pr_refs`), never prose
    mentions. Unreadable fails closed, as on the completion path; so does a host
    with no PR lookup at all. Empty for a card that is not in ``review``.
    """
    from . import kanban_open_pr as _open_pr
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "review":
        return []
    refs = _open_pr.split_fleet(_open_pr.extract_pr_refs(
        survivor_pr=_card_recorded_pr_refs(conn, task_id)))[0]
    if not refs:
        return []
    query_fn = _open_pr.memo_query(query_fn)
    if query_fn is None:
        return [f"{r.repo}#{r.number}" for r in refs]
    opened, unverified = _open_pr.unmerged_refs(refs, query_fn=query_fn, primary=refs)
    return [f"{r.repo}#{r.number}" for r in opened + unverified]


def _reassign_request_changes(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], reason: str,
    operator: Optional[str],
) -> Optional[str]:
    """Route a reassign of a ``review`` card through :func:`request_changes`.

    Same event and payload as ``request-changes --operator``: the review run is
    opened and closed as the caller, ``changes_requested`` carries
    ``"operator": "<who>: <why>"`` (hermes-home changes_hold.py keys on it), the
    card lands ``ready`` and the implementer is restored. Returns that
    implementer; raises :class:`ReviewHoldRequired` on refusal.
    """
    who = (str(operator or "").strip() or _event_actor()[0] or "operator").split(":", 1)[0].strip()
    ok, detail = request_changes(
        conn, task_id, reason=reason, claimer=who,
        operator=f"{who}: reassign to {profile or '(unassigned)'}: {reason}",
    )
    if not ok:
        raise ReviewHoldRequired(
            f"cannot reassign {task_id}: --request-changes refused: {detail}"
        )
    return detail


class NoWorkerFlagSet(RuntimeError):
    """A worker assignee was requested for a card flagged ``no_worker``."""


def is_worker_assignee(assignee: Optional[str]) -> bool:
    """True when ``assignee`` names a lane the dispatcher would spawn.

    Only "unassigned" and the explicit human sentinel (``human`` /
    ``human:<name>``) are non-worker targets. Every profile name counts as a
    worker, because the dispatcher spawns whatever profile the card names.
    """
    value = (assignee or "").strip()
    return bool(value) and not is_human_reviewer(value)


def _no_worker_refusal(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str],
) -> Optional[str]:
    """One-line refusal when ``profile`` is a worker and the card is flagged."""
    if not is_worker_assignee(profile):
        return None
    row = conn.execute(
        "SELECT no_worker FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None or not row["no_worker"]:
        return None
    return (
        f"refused: {task_id} is no-worker (dispatch: operator-only); "
        f"assigning worker {profile!r} needs --worker-ok on the same call "
        "(clears the flag)"
    )


def _set_no_worker_locked(
    conn: sqlite3.Connection,
    task_id: str,
    flag: bool,
    *,
    source: str,
    operator: Optional[str] = None,
) -> bool:
    """Write the flag inside an open txn; append an event only on a change."""
    row = conn.execute(
        "SELECT no_worker FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    if bool(row["no_worker"]) == bool(flag):
        return True
    conn.execute(
        "UPDATE tasks SET no_worker = ? WHERE id = ?", (1 if flag else 0, task_id),
    )
    payload: dict = {"source": source}
    if operator:
        payload["operator"] = operator
    _append_event(
        conn, task_id, "no_worker_set" if flag else "no_worker_cleared", payload,
    )
    return True


def set_no_worker(
    conn: sqlite3.Connection,
    task_id: str,
    flag: bool,
    *,
    operator: Optional[str] = None,
) -> bool:
    """Set or clear a card's ``no_worker`` (operator-only) flag.

    Returns False for an unknown id. A change records ``no_worker_set`` /
    ``no_worker_cleared``; setting the current value is a silent no-op.
    """
    with write_txn(conn):
        ok = _set_no_worker_locked(
            conn, task_id, flag, source="edit", operator=operator,
        )
    if ok:
        notify_task_updated(conn, task_id, ("no_worker",))
    return ok


@_home_session_guarded("assign")
def assign_task(
    conn: sqlite3.Connection,
    task_id: str,
    profile: Optional[str],
    *,
    request_changes_reason: Optional[str] = None,
    operator: Optional[str] = None,
    pr_query=None,
    worker_ok: bool = False,
) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.

    A card in ``review`` whose own PR is open (or unreadable) is refused with
    :class:`ReviewHoldRequired` unless ``request_changes_reason`` is given; with
    it the reassign goes through :func:`request_changes` (card ``ready``,
    implementer restored, ``changes_requested`` with ``operator``), then
    ``profile`` becomes the assignee if it differs from the implementer.

    A card flagged ``no_worker`` refuses a worker ``profile`` with
    :class:`NoWorkerFlagSet` — ``operator`` does not bypass it. Only
    ``worker_ok=True`` does, and it clears the flag (``no_worker_cleared``).
    """
    profile = _canonical_assignee(profile)
    refusal = _no_worker_refusal(conn, task_id, profile)
    if refusal and not worker_ok:
        raise NoWorkerFlagSet(refusal)
    clear_no_worker = bool(refusal)
    changes_reason = str(request_changes_reason or "").strip()
    if not changes_reason:
        held = review_hold_open_prs(conn, task_id, query_fn=pr_query)
        if held:
            raise ReviewHoldRequired(
                f"cannot reassign {task_id}: it is in review with open PR(s) "
                f"{', '.join(held)}. Reassigning would leave it in review with no "
                f"worker. Pass --request-changes \"<reason>\" to send it back "
                f"(changes_requested, status ready) or land/close the PR first."
            )
    else:
        implementer = _reassign_request_changes(
            conn, task_id, profile, changes_reason, operator,
        )
        if implementer == profile:
            if clear_no_worker:
                with write_txn(conn):
                    _set_no_worker_locked(
                        conn, task_id, False, source="worker_ok", operator=operator,
                    )
            notify_task_updated(conn, task_id, ("assignee", "status"))
            return True
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        if row["assignee"] != profile:
            # The failure streak is per task/profile; a new profile starts fresh.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?", (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        if clear_no_worker:
            _set_no_worker_locked(
                conn, task_id, False, source="worker_ok", operator=operator,
            )
        # ``from`` lets the respawn guard tell a real handoff (dev→closer) from
        # a no-op re-assign or an unassign, which must not lift ``active_pr``.
        _append_event(
            conn, task_id, "assigned", {"assignee": profile, "from": row["assignee"]},
        )
    # Observer fires AFTER commit so subscribers see durable state.
    notify_task_updated(conn, task_id, ("assignee",))
    return True


def set_model_override(
    conn: sqlite3.Connection,
    task_id: str,
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    audit_comment_author: Optional[str] = None,
    audit_comment_body: Optional[str] = None,
    flagship_override_reason: Optional[str] = None,
    flagship_override_author: Optional[str] = None,
    pin_sub_reason: Optional[str] = None,
    pin_sub_fallback: bool = False,
    pin_sub_author: Optional[str] = None,
) -> bool:
    """Set (or clear) the per-task model/provider override.

    ``model=None`` (or empty) clears BOTH overrides — the worker falls back
    to its profile's configured model. ``provider`` without ``model`` is
    rejected: a bare provider switch has no defined meaning for the worker
    spawn (``--provider`` alone would re-resolve the profile's model name
    against a different backend, which is exactly the mismatch class this
    feature exists to kill).

    Allowed on any non-archived task, including ``running`` ones — the
    override only takes effect on the NEXT dispatch, so setting it on a
    running task that's about to be reclaimed/retried is the primary
    rate-limit-recovery flow. ``audit_comment_author`` and
    ``audit_comment_body`` must be supplied together; the comment is committed
    atomically with the override. ``flagship_override_reason`` (main's
    ``--allow-flagship``) is translated into the ``flagship override:``
    comment the dispatcher's flagship gate looks for. Returns True on success.
    """
    audit_comment_author, audit_comment_body = _flagship_reason_to_audit(
        model, provider,
        audit_comment_author=audit_comment_author,
        audit_comment_body=audit_comment_body,
        flagship_override_reason=flagship_override_reason,
        flagship_override_author=flagship_override_author,
    )
    model, provider = _validate_model_override_args(
        model, provider,
        audit_comment_author=audit_comment_author,
        audit_comment_body=audit_comment_body,
        pin_sub_reason=pin_sub_reason,
        pin_sub_fallback=pin_sub_fallback,
    )
    with write_txn(conn):
        if not _set_model_override_locked(
            conn, task_id, model, provider,
            audit_comment_author=audit_comment_author,
            audit_comment_body=audit_comment_body,
            pin_sub_reason=pin_sub_reason,
            pin_sub_fallback=pin_sub_fallback,
            pin_sub_author=pin_sub_author,
        ):
            return False
    # Task-mutation observer (RFC #58548), fired AFTER the txn commits.
    notify_task_updated(conn, task_id, ("model_override", "provider_override"))
    return True


def _set_task_override(
    conn: sqlite3.Connection, task_id: str, sql: str, params: tuple, event_kind: str, payload: dict,
    changed_fields: tuple[str, ...], *, archived_msg: str,
) -> bool:
    """Per-task override write: refuse archived tasks, record ``event_kind``,
    then fire the task-updated observer AFTER commit (RFC #58548)."""
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"{archived_msg} on archived task {task_id}")
        conn.execute(sql, (*params, task_id))
        _append_event(conn, task_id, event_kind, payload)
    notify_task_updated(conn, task_id, changed_fields)
    return True


def _validate_model_override_args(
    model: Optional[str],
    provider: Optional[str],
    *,
    audit_comment_author: Optional[str] = None,
    audit_comment_body: Optional[str] = None,
    pin_sub_reason: Optional[str] = None,
    pin_sub_fallback: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Normalise + validate a route pair WITHOUT touching the database.

    Split out of :func:`set_model_override` so a batch can validate every
    card's arguments before it opens its single write transaction: argument
    errors must never be discovered halfway through a batch, where some
    cards have already been written.
    """
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if bool(audit_comment_author) != bool(audit_comment_body):
        raise ValueError(
            "audit_comment_author and audit_comment_body must be supplied together"
        )
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    if not model:
        provider = None
    from hermes_cli.model_policy import pin_sub_arg_error, validate_route_provider

    pin_sub_error = pin_sub_arg_error(
        model, provider, pin_sub_reason, pin_sub_fallback=bool(pin_sub_fallback),
    )
    if pin_sub_error:
        raise ValueError(pin_sub_error)
    pin_sub_reason = (pin_sub_reason or "").strip() or None
    validate_route_provider(model, provider, pin_sub_reason=pin_sub_reason)
    model, provider = _resolve_stored_model_pair(model, provider)
    validate_route_provider(model, provider, pin_sub_reason=pin_sub_reason)
    # Main's flagship ban (model_policy) is the one predicate. A flagship
    # route is only writable together with the ``flagship override:`` comment
    # the dispatcher's flagship gate accepts, so a route this layer writes can
    # never be one the dispatcher then silently refuses.
    from hermes_cli.model_policy import (
        FLAGSHIP_OVERRIDE_COMMENT_PREFIX,
        flagship_model_error,
        is_firepower_model,
    )

    if is_firepower_model(model):
        body = (audit_comment_body or "").lstrip().casefold()
        if not body.startswith(FLAGSHIP_OVERRIDE_COMMENT_PREFIX):
            raise ValueError(flagship_model_error(str(model)))
    return model, provider


def _flagship_reason_to_audit(
    model: Optional[str],
    provider: Optional[str],
    *,
    audit_comment_author: Optional[str],
    audit_comment_body: Optional[str],
    flagship_override_reason: Optional[str],
    flagship_override_author: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Map main's ``flagship_override_reason`` onto the audit-comment pair.

    Only a flagship route gets a comment (same as main: a reason on a
    standard model is ignored). An explicit audit comment wins.
    """
    reason = (flagship_override_reason or "").strip()
    if audit_comment_body or not reason:
        return audit_comment_author, audit_comment_body
    from hermes_cli.model_policy import is_firepower_model, override_comment

    resolved, _ = _resolve_stored_model_pair(
        (model or "").strip() or None, (provider or "").strip() or None,
    )
    if not is_firepower_model(resolved):
        return audit_comment_author, audit_comment_body
    return flagship_override_author or "operator", override_comment(reason)


def _set_model_override_locked(
    conn: sqlite3.Connection,
    task_id: str,
    model: Optional[str],
    provider: Optional[str],
    *,
    audit_comment_author: Optional[str] = None,
    audit_comment_body: Optional[str] = None,
    pin_sub_reason: Optional[str] = None,
    pin_sub_fallback: bool = False,
    pin_sub_author: Optional[str] = None,
) -> bool:
    """Write one route override. MUST already be inside a ``write_txn``.

    Every route write also (re)writes the pin columns: a route written
    without ``pin_sub_reason`` clears any earlier pin, so a pin can never
    outlive the route it authorized.

    The status re-read happens here, inside the caller's transaction, so it
    is the row state the write commits against — not a stale pre-check. A
    batch therefore cannot half-commit when another connection archives a
    selected card after selection: the ``RuntimeError`` raised here unwinds
    the caller's transaction and every card in it.

    ``model``/``provider`` must already have passed
    :func:`_validate_model_override_args`.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return False
    if row["status"] == "archived":
        raise RuntimeError(f"cannot set model override on archived task {task_id}")
    pin_sub_reason = (pin_sub_reason or "").strip() or None
    pin_sub_fallback = bool(pin_sub_reason and pin_sub_fallback)
    conn.execute(
        "UPDATE tasks SET model_override = ?, provider_override = ?, "
        "pin_sub_reason = ?, pin_sub_fallback = ? WHERE id = ?",
        (model, provider, pin_sub_reason, 1 if pin_sub_fallback else 0, task_id),
    )
    payload = {"model": model, "provider": provider}
    if pin_sub_reason:
        payload.update(pin_sub_reason=pin_sub_reason, pin_sub_fallback=pin_sub_fallback)
    _append_event(conn, task_id, "model_override_set", payload)
    if audit_comment_body:
        add_comment(
            conn,
            task_id,
            audit_comment_author or "",
            audit_comment_body,
        )
    if pin_sub_reason:
        from hermes_cli.model_policy import sub_pin_comment

        add_comment(
            conn, task_id, pin_sub_author or audit_comment_author or "operator",
            sub_pin_comment(provider, pin_sub_reason, fallback=pin_sub_fallback),
        )
    return True


def set_reasoning_effort(conn: sqlite3.Connection, task_id: str, effort: Optional[str]) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = normalize_reasoning_effort(effort)
    with write_txn(conn):
        if not _set_reasoning_effort_locked(conn, task_id, effort):
            return False
    # Task-mutation observer (RFC #58548), fired AFTER the txn commits.
    notify_task_updated(conn, task_id, ("reasoning_effort",))
    return True


def _set_reasoning_effort_locked(
    conn: sqlite3.Connection,
    task_id: str,
    effort: Optional[str],
) -> bool:
    """Write one effort override. MUST already be inside a ``write_txn``.

    Sibling of :func:`_set_model_override_locked`; same reason. ``effort``
    must already have passed :func:`normalize_reasoning_effort`.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return False
    if row["status"] == "archived":
        raise RuntimeError(
            f"cannot set reasoning effort on archived task {task_id}"
        )
    conn.execute(
        "UPDATE tasks SET reasoning_effort = ? WHERE id = ?",
        (effort, task_id),
    )
    _append_event(
        conn, task_id, "reasoning_effort_set", {"reasoning_effort": effort}
    )
    return True


def set_card_brain(
    conn: sqlite3.Connection,
    task_id: str,
    brain: Optional[str],
) -> bool:
    """Set (or clear) the per-card harness brain (t_a8f335c5).

    ``brain=None`` (or empty / ``none``) clears it: the worker uses its
    profile's ``foreign_lane.brain``. Independent of the model and effort
    overrides, applies on the NEXT dispatch, and records ``brain_set`` (the
    same event ledger and actor stamp as ``reasoning_effort_set``).
    """
    brain = normalize_card_brain(brain)
    with write_txn(conn):
        if not _set_card_brain_locked(conn, task_id, brain):
            return False
    notify_task_updated(conn, task_id, ("brain",))
    return True


def _set_card_brain_locked(
    conn: sqlite3.Connection,
    task_id: str,
    brain: Optional[str],
) -> bool:
    """Write one card brain. MUST already be inside a ``write_txn``; ``brain``
    must already have passed :func:`normalize_card_brain`."""
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return False
    if row["status"] == "archived":
        raise RuntimeError(f"cannot set brain on archived task {task_id}")
    conn.execute("UPDATE tasks SET brain = ? WHERE id = ?", (brain, task_id))
    _append_event(conn, task_id, "brain_set", {"brain": brain})
    return True


@dataclass
class BatchRouteWrite:
    """One card's requested route/effort change inside a batch."""

    task_id: str
    touch_model: bool = False
    model: Optional[str] = None
    provider: Optional[str] = None
    audit_comment_author: Optional[str] = None
    audit_comment_body: Optional[str] = None
    touch_effort: bool = False
    effort: Optional[str] = None
    # Per-card harness brain (``--brain`` / ``--clear-brain``, t_a8f335c5).
    touch_brain: bool = False
    brain: Optional[str] = None
    # Deliberate single-sub pin (``--pin-sub``); only with ``touch_model``.
    pin_sub_reason: Optional[str] = None
    pin_sub_fallback: bool = False
    pin_sub_author: Optional[str] = None
    # The selection predicate that chose this card, re-checked under the
    # batch's writer lock. ``None`` means "no constraint on that column".
    require_statuses: Optional[frozenset] = None
    require_assignees: Optional[frozenset] = None
    # Selector-chosen cards (``--where`` / ``--all-active``) that stopped
    # matching are SKIPPED and reported; an explicitly named card that
    # stopped matching aborts the whole batch, exactly as it would have at
    # selection time — an operator naming five cards must not get four.
    skip_if_unmatched: bool = False
    # ``set-model --live`` (t_033a3bb1): when the card is RUNNING, also append
    # a run-scoped ``route_changed`` event in the SAME transaction. The live
    # worker polls for it at its next loop iteration and switches in place
    # (no abort, conversation kept). Not running => plain next-dispatch write.
    live: bool = False

# Run-scoped trigger for a live route switch (``set-model --live``). The card
# row stays the authority on the route; the event only says "re-read it now".
ROUTE_CHANGED_EVENT = "route_changed"

# Run-scoped marker a worker writes when its runtime cannot switch in place
# (``codex_app_server``: the turn runs in a subprocess, no loop-boundary
# poll). A live write to such a run is next-dispatch only, never promised.
ROUTE_LIVE_UNSUPPORTED_EVENT = "route_live_unsupported"


def _append_live_route_changed_locked(
    conn: sqlite3.Connection, task_id: str, *, touch_model: bool, touch_effort: bool,
) -> Optional[int]:
    """Append ``route_changed`` for the card's live run; in-txn only.

    Returns the run id the event is scoped to, or None when the card has no
    live run (not ``running``), in which case the write is next-dispatch only.
    The payload snapshots the route just written, for the audit trail; the
    worker still re-reads the card row, which is the authority.
    """
    row = conn.execute(
        "SELECT status, current_run_id, model_override, provider_override, "
        "reasoning_effort FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or (row["status"] or "").lower() != "running" or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? LIMIT 1",
        (task_id, run_id, ROUTE_LIVE_UNSUPPORTED_EVENT),
    ).fetchone():
        return None
    _append_event(conn, task_id, ROUTE_CHANGED_EVENT, {
        "live": True,
        "model": row["model_override"],
        "provider": row["provider_override"],
        "reasoning_effort": row["reasoning_effort"],
        "touch_model": bool(touch_model),
        "touch_effort": bool(touch_effort),
    }, run_id=run_id)
    return run_id


def _batch_write_mismatch(conn: sqlite3.Connection, write: BatchRouteWrite) -> Optional[str]:
    """Why ``write`` no longer matches its selection, or None. In-txn only."""

    row = conn.execute(
        "SELECT status, assignee FROM tasks WHERE id = ?", (write.task_id,),
    ).fetchone()
    if row is None:
        return "no such task"
    status = (row["status"] or "").lower()
    if write.require_statuses is not None and status not in write.require_statuses:
        return f"status is now {status}"
    if (write.require_assignees is not None
            and (row["assignee"] or "") not in write.require_assignees):
        return f"assignee is now {row['assignee'] or '(none)'}"
    return None


def apply_batch_route_writes(
    conn: sqlite3.Connection,
    writes: Sequence[BatchRouteWrite],
    *,
    skipped: Optional[dict[str, str]] = None,
    live_runs: Optional[dict[str, int]] = None,
) -> list[str]:
    """Apply every route/effort write in ONE transaction, or none of them.

    ``hermes kanban set-model`` selects N cards and then mutates them. Before
    this primitive it called the individually-committing ``set_model_override``
    / ``set_reasoning_effort`` in a loop, so a card that changed state AFTER
    selection (another connection archives it; a worker claims it) failed
    midway and left the cards ahead of it committed — a silently split route
    across a batch the operator believed was one action, with no receipt
    naming which cards moved.

    Static prevalidation cannot close that: the interval between "we checked"
    and "we wrote" is exactly where the race lives. The fix is to do the
    checking and the writing inside one ``BEGIN IMMEDIATE`` — the per-card
    status re-read in ``_set_model_override_locked`` then runs against the
    same locked snapshot the UPDATE commits against, and any refusal unwinds
    the whole batch.

    The selection predicate itself (``require_statuses`` /
    ``require_assignees``) is re-evaluated under the same lock: a card that
    was ``ready`` when ``--where status=ready`` selected it and was completed
    by another connection before this lock is no longer in the batch. Such a
    selector-chosen card is left untouched and recorded in ``skipped``
    (``task_id -> reason``); an explicitly named one aborts the batch.

    Returns the ids written, in order. Raises ``RuntimeError`` (archived card,
    or an explicit card that stopped matching) or ``ValueError`` (bad
    arguments) having written NOTHING. Arguments are validated for every card
    up front so an argument error also cannot reach the transaction
    half-applied.
    """
    prepared: list[tuple[BatchRouteWrite, Optional[str], Optional[str], Optional[str]]] = []
    for write in writes:
        model, provider = (None, None)
        if write.touch_model:
            model, provider = _validate_model_override_args(
                write.model, write.provider,
                audit_comment_author=write.audit_comment_author,
                audit_comment_body=write.audit_comment_body,
                pin_sub_reason=write.pin_sub_reason,
                pin_sub_fallback=write.pin_sub_fallback,
            )
        effort = normalize_reasoning_effort(write.effort) if write.touch_effort else None
        if write.touch_brain:
            write.brain = normalize_card_brain(write.brain)
        prepared.append((write, model, provider, effort))

    # Home-session guard for every card in the batch, before the writer lock:
    # one foreign card refuses the whole batch with nothing written.
    overrides: dict[str, MutationActor] = {}
    for write in writes:
        ov = check_home_session(conn, write.task_id, "set-model")
        if ov is not None:
            overrides[write.task_id] = ov

    written: list[str] = []
    fields: dict[str, tuple[str, ...]] = {}
    skipped_now: dict[str, str] = {}
    live_now: dict[str, int] = {}
    with write_txn(conn):
        for write, model, provider, effort in prepared:
            mismatch = _batch_write_mismatch(conn, write)
            if mismatch is not None:
                if not write.skip_if_unmatched:
                    raise RuntimeError(
                        f"{write.task_id}: {mismatch}; no cards were changed"
                    )
                skipped_now[write.task_id] = mismatch
                continue
            changed: tuple[str, ...] = ()
            if write.touch_model:
                if not _set_model_override_locked(
                    conn, write.task_id, model, provider,
                    audit_comment_author=write.audit_comment_author,
                    audit_comment_body=write.audit_comment_body,
                    pin_sub_reason=write.pin_sub_reason,
                    pin_sub_fallback=write.pin_sub_fallback,
                    pin_sub_author=write.pin_sub_author or write.audit_comment_author,
                ):
                    raise RuntimeError(f"no such task: {write.task_id}")
                changed += ("model_override", "provider_override")
            if write.touch_effort:
                if not _set_reasoning_effort_locked(conn, write.task_id, effort):
                    raise RuntimeError(f"no such task: {write.task_id}")
                changed += ("reasoning_effort",)
            if write.touch_brain:
                if not _set_card_brain_locked(conn, write.task_id, write.brain):
                    raise RuntimeError(f"no such task: {write.task_id}")
                changed += ("brain",)
            if changed:
                written.append(write.task_id)
                fields[write.task_id] = changed
                if write.live:
                    run_id = _append_live_route_changed_locked(
                        conn, write.task_id,
                        touch_model=write.touch_model, touch_effort=write.touch_effort,
                    )
                    if run_id is not None:
                        live_now[write.task_id] = run_id
    if skipped is not None:
        skipped.update(skipped_now)
    if live_runs is not None:
        live_runs.update(live_now)
    # Observers fire only AFTER the whole batch commits, so a rolled-back
    # batch never announces a mutation that did not happen.
    for task_id in written:
        notify_task_updated(conn, task_id, fields[task_id])
        if task_id in overrides:
            record_foreign_action(conn, task_id, "set-model", overrides[task_id])
    return written


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
@_home_session_guarded("link", task_param="child_id")
def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    kind: Optional[str] = None,
    expected_child_run_id: Optional[int] = None,
) -> bool:
    """Link ``parent_id -> child_id``. Returns True when the link gated a
    ``ready`` child back to ``todo`` (the new parent is not yet terminal), so
    callers can surface the demotion instead of a silent status flip.

    ``kind`` defaults to ``blocks`` (the historical behaviour): the child is
    held in ``todo`` until the parent is ``done``. ``derived-from`` records
    provenance WITHOUT gating — use it when a discovery task (audit, survey,
    investigation) spawned the child, so remediation stays independently
    dispatchable. Re-linking an existing pair UPDATES its kind, so an edge
    created with the wrong semantics can be corrected in place.

    A running child cannot normally be gated retroactively, so reject the edge
    rather than record a dependency that did not constrain the active run. The
    owning worker may link its own active run for a subsequent dependency-block
    handoff by supplying its trusted ``expected_child_run_id``.
    """
    kind = normalize_link_kind(kind)
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gated = False
    with write_txn(conn):
        missing = _missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        child = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
        if child["status"] == "running" and (
            expected_child_run_id is None
            or child["current_run_id"] != expected_child_run_id
        ):
            raise ValueError(f"cannot link {parent_id} -> {child_id}: child is already running")
        # Cycle detection walks BOTH kinds: a provenance edge still describes
        # the graph the dashboard renders, and an A->B->A loop there is a
        # data-modelling error regardless of whether it can deadlock.
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id, kind) VALUES (?, ?, ?) "
            "ON CONFLICT(parent_id, child_id) DO UPDATE SET kind = excluded.kind",
            (parent_id, child_id, kind),
        )
        # Only a blocking edge demotes a ready child. A derived-from edge is
        # provenance: the child stays dispatchable. If child was ready but parent
        # is not yet terminal, demote child to todo (archived counts as terminal,
        # matching _parents_satisfied/recompute_ready).
        if kind == LINK_KIND_BLOCKS and _task_status(conn, parent_id) not in ("done", "archived"):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            gated = cur.rowcount == 1
            if gated:
                _append_event(
                    conn,
                    child_id,
                    "dependency_wait",
                    {"reason": "parent_not_done", "demoted": True, "parent": parent_id},
                )
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id, "kind": kind},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))

    if kind != LINK_KIND_BLOCKS:
        # Converting a blocking edge to provenance can release a child that
        # was parked in ``todo`` solely because of it. Matches the contract of
        # ``unlink_tasks``: don't make the operator wait for a dispatcher tick.
        recompute_ready(conn)
    return gated


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


# Removing an edge re-promotes the child: a status write on it, same as link
# (C5 #21, PR #951).
@_home_session_guarded("unlink", task_param="child_id")
def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _append_event(conn, child_id, "unlinked", {"parent": parent_id, "child": child_id})
    if removed:
        # Re-gate the child now (as complete_task/unblock_task do) instead of
        # leaving it in todo until the next tick.
        recompute_ready(conn)
    return removed


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "parent_id", "child_id", task_id)


def parent_links(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """Return ``(parent_id, kind)`` for every parent edge, ordered by id.

    The companion to :func:`parent_ids` for callers that must distinguish a
    gating ``blocks`` parent from a provenance-only ``derived-from`` one —
    without it there is no way to observe an existing edge's semantics.
    """
    rows = conn.execute(
        "SELECT parent_id, kind FROM task_links WHERE child_id = ? "
        "ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [(r["parent_id"], normalize_link_kind(r["kind"])) for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "child_id", "parent_id", task_id)


def child_links(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """Return ``(child_id, kind)`` for every child edge, ordered by id."""
    rows = conn.execute(
        "SELECT child_id, kind FROM task_links WHERE parent_id = ? "
        "ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [(r["child_id"], normalize_link_kind(r["kind"])) for r in rows]


def task_graph_contexts(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict]:
    """Bulk-load compact direct graph state for graph-aware diagnostics."""
    ordered_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    contexts = {task_id: {"parents": [], "children": []} for task_id in ordered_ids}
    if not ordered_ids:
        return contexts

    placeholders = ",".join("?" for _ in ordered_ids)
    for bucket, own, other in (("parents", "child_id", "parent_id"), ("children", "parent_id", "child_id")):
        for row in conn.execute(
            f"SELECT l.{own} AS owner_id, t.id, t.title, t.status "
            f"FROM task_links l JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} IN ({placeholders}) ORDER BY l.{own}, t.id", tuple(ordered_ids),
        ).fetchall():
            contexts[row["owner_id"]][bucket].append(
                {"id": row["id"], "title": row["title"], "status": row["status"]}
            )
    return contexts


def task_graph_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """Return compact direct parent/child state for one task."""
    return task_graph_contexts(conn, [task_id])[task_id]

# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

#: Length of a comment ``session_ref``. Long enough that two live sessions
#: colliding is not a practical concern, short enough to read at a glance in a
#: chat bubble / terminal / card drawer.
SESSION_REF_LEN = 12
_SESSION_REF_RE = re.compile(rf"^[0-9a-f]{{{SESSION_REF_LEN}}}$")

#: Rendered when a comment carries no provenance at all — every pre-migration
#: row, plus any write with no trusted runtime context. Deliberately explicit:
#: a bare author on a board with mixed attribution reads as "attributed to this
#: profile's only session", which is the exact ambiguity this closes.
LEGACY_PROVENANCE_LABEL = "provenance unknown"


def derive_session_ref(session_id: Optional[str]) -> Optional[str]:
    """Return a bounded, non-reversible fingerprint of ``session_id``.

    The raw session id is NOT stored. It can embed routing identity that is
    unpleasant to persist forever on a board row (``sms:+1555…``,
    ``discord:<user>``), and it is unbounded in length. A truncated BLAKE2b
    digest keeps rows small and fixed-width while still separating two
    concurrent sessions of the same profile — which is all the board needs.

    Returns ``None`` for a missing / blank id so callers store NULL rather than
    a fabricated value.
    """
    if not session_id or not str(session_id).strip():
        return None
    digest = hashlib.blake2b(
        str(session_id).strip().encode("utf-8"), digest_size=SESSION_REF_LEN // 2
    )
    return digest.hexdigest()


def format_comment_author(
    author: str,
    *,
    run_id: Optional[int] = None,
    session_ref: Optional[str] = None,
) -> str:
    """Render a comment's author WITH its provenance, for every read surface.

    One formatter so the CLI, the agent tool, the dashboard API and the worker
    prompt context cannot drift apart on how a comment is attributed::

        apollo (run 327, sess 3f2a9c1b0d44)
        apollo (sess 3f2a9c1b0d44)
        daedalus (run 327)
        apollo (provenance unknown)
    """
    parts: list[str] = []
    if run_id is not None:
        parts.append(f"run {int(run_id)}")
    if session_ref:
        parts.append(f"sess {session_ref}")
    if not parts:
        parts.append(LEGACY_PROVENANCE_LABEL)
    return f"{author} ({', '.join(parts)})"


def _validate_comment_provenance(
    run_id: Optional[int], session_ref: Optional[str]
) -> tuple[Optional[int], Optional[str]]:
    """Validate the provenance pair at the single write choke point.

    Rejects anything outside the expected shapes so no caller — tool arg,
    dashboard payload, or future code path — can smuggle a long, structured or
    identity-bearing value into a durable board row. ``session_ref`` must be a
    :func:`derive_session_ref` output; ``run_id`` a positive int.
    """
    if run_id is not None:
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            raise ValueError("comment run_id must be an int")
        if run_id <= 0:
            raise ValueError("comment run_id must be positive")
    if session_ref is not None:
        if not isinstance(session_ref, str) or not _SESSION_REF_RE.match(session_ref):
            raise ValueError(
                "comment session_ref must be a "
                f"{SESSION_REF_LEN}-char lowercase hex digest "
                "(use kanban_db.derive_session_ref)"
            )
    return run_id, session_ref


# Bound on an unhonoured ``--author`` label kept for forensics (see add_comment).
_CLAIMED_AUTHOR_MAX = 200


def add_comment(
    conn: sqlite3.Connection,
    task_id: str,
    author: str,
    body: str,
    *,
    run_id: Optional[int] = None,
    session_ref: Optional[str] = None,
    claimed_author: Optional[str] = None,
) -> int:
    """Append a comment, recording who wrote it and from which run/session.

    ``run_id`` / ``session_ref`` must come from trusted runtime context (see
    ``hermes_cli.kanban_identity.resolve_comment_provenance``), never from
    caller-supplied tool args or comment text — the same rule ``author`` already
    follows.

    ``claimed_author`` is an author label the caller asked for but was not
    allowed to use (``kanban comment --author`` without the operator token).
    It is recorded on the ``commented`` event and the journal only; every
    reader of ``task_comments.author`` keeps seeing the real author.
    """
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    if claimed_author is not None:
        if not isinstance(claimed_author, str) or not claimed_author.strip():
            raise ValueError("claimed_author must be a non-empty string")
        if len(claimed_author) > _CLAIMED_AUTHOR_MAX:
            raise ValueError(f"claimed_author longer than {_CLAIMED_AUTHOR_MAX} chars")
        claimed_author = claimed_author.strip()
    run_id, session_ref = _validate_comment_provenance(run_id, session_ref)
    now = int(time.time())
    if _is_delegated_child():
        # The one write a delegate_task child may make (t_70fcc2c3). Append-only
        # and attributed: the marker is added here, not by the caller, so an
        # explicit ``--author`` cannot pass a child's words off as the parent's.
        if not author.strip().endswith(SUBAGENT_AUTHOR_MARKER.strip()):
            author = author.strip() + SUBAGENT_AUTHOR_MARKER
        grant = _DELEGATED_CHILD_COMMENT_GRANT.set(True)
        conn.execute("PRAGMA query_only=OFF")
        try:
            return _add_comment_txn(
                conn, task_id, author, body, run_id, session_ref, now,
                claimed_author,
            )
        finally:
            conn.execute("PRAGMA query_only=ON")
            _DELEGATED_CHILD_COMMENT_GRANT.reset(grant)
    return _add_comment_txn(
        conn, task_id, author, body, run_id, session_ref, now, claimed_author
    )


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def _add_comment_txn(
    conn: sqlite3.Connection,
    task_id: str,
    author: str,
    body: str,
    run_id: Optional[int],
    session_ref: Optional[str],
    now: int,
    claimed_author: Optional[str] = None,
) -> int:
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with write_txn(conn, allow_nested=True):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_comments "
            "(task_id, author, body, run_id, session_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, author.strip(), body.strip(), run_id, session_ref, now),
        )
        _append_event(
            conn,
            task_id,
            "commented",
            {
                "author": author,
                "len": len(body),
                # Non-secret fingerprint only, so the event log stays
                # attributable even if a comment row is later pruned.
                **({"session_ref": session_ref} if session_ref else {}),
                **({"claimed_author": claimed_author} if claimed_author else {}),
            },
            run_id=run_id,
        )
        # The CONTENT hook (card t_357330bf). The ``commented`` event above
        # carries author + length, not the text. On 2026-09-21 the comment
        # THREADS were the only part of the wiped subs-ace board that could
        # not be reconstructed from any other source -- cards came back,
        # discussion did not. Journaling the body is the single thing that
        # would have saved them, so it is recorded explicitly rather than
        # inferred from the event.
        try:
            from hermes_cli import kanban_journal

            kanban_journal.append(
                _journal_board_slug(),
                task_id,
                "comment_body",
                {
                    "author": author.strip(),
                    "body": body.strip(),
                    "session_ref": session_ref,
                    "created_at": now,
                    "comment_id": int(cur.lastrowid or 0),
                    **({"claimed_author": claimed_author} if claimed_author else {}),
                },
                actor=author.strip(),
                run_id=run_id,
            )
        except Exception:  # pragma: no cover - never fail the comment write
            pass
        return int(cur.lastrowid or 0)


def _inline_comment_provenance(task_id: str) -> tuple[Optional[int], Optional[str]]:
    """Provenance for the audit comments written inline inside a write_txn.

    ``specify_triage_task`` / ``resolve_triage_task`` / ``decompose_triage_task``
    each INSERT their audit comment directly rather than calling
    :func:`add_comment` (they are already inside an IMMEDIATE txn, and a nested
    ``BEGIN IMMEDIATE`` would raise). They must still be attributable, or the
    same-profile ambiguity just moves to the workflow lanes. Best-effort: any
    resolution failure degrades to NULL/NULL, which renders as
    ``provenance unknown`` — never a guess.
    """
    try:
        from hermes_cli.kanban_identity import safe_comment_provenance

        return _validate_comment_provenance(*safe_comment_provenance(task_id))
    except Exception:
        _log.debug("kanban: comment provenance resolution failed", exc_info=True)
        return None, None


def _comment_from_row(r: sqlite3.Row) -> Comment:
    """Build a ``Comment`` tolerating a pre-migration row shape.

    ``list_comments`` uses ``SELECT *``, so a DB opened by an older Hermes
    (columns absent) must not raise — legacy rows simply carry NULL provenance.
    """
    keys = r.keys()
    run_id = r["run_id"] if "run_id" in keys else None
    return Comment(
        id=r["id"],
        task_id=r["task_id"],
        author=r["author"],
        body=r["body"],
        created_at=r["created_at"],
        run_id=int(run_id) if run_id is not None else None,
        session_ref=(r["session_ref"] if "session_ref" in keys else None) or None,
    )


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    return [Comment.from_row(r) for r in _task_rows(conn, "task_comments", task_id, "created_at ASC")]


def list_comments_after(
    conn: sqlite3.Connection, task_id: str, *, after_id: int = 0
) -> list[Comment]:
    """Comments with ``id > after_id`` — keyed on rowid, not ``created_at``, so a
    same-second burst is never skipped (live worker comment bridge)."""
    rows = conn.execute(
        "SELECT * FROM task_comments "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC",
        (task_id, int(after_id)),
    ).fetchall()
    return [Comment.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

# The attachment size cap is the module-level ``KANBAN_ATTACHMENT_MAX_BYTES``
# (defined near the top of this file) — one constant shared by the dashboard
# HTTP endpoint, the agent toolset, and the CLI so the limit cannot drift
# between surfaces.
class AttachmentTooLarge(ValueError):
    """Attachment over the size cap. A ``ValueError`` so generic 400 handlers
    still catch it while the tool/CLI can give a 413-style message."""


def _safe_attachment_name(raw: str) -> str:
    """Client filename -> safe basename: strip directories (both separators),
    control chars and leading dots (no dotfiles, no traversal); ValueError when
    nothing usable remains. Only ever joined under the per-task attachments dir."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """``foo.pdf`` -> ``foo.pdf``, ``foo (1).pdf``, ... first one that doesn't exist."""
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection, task_id: str, filename: str, data: bytes, *,
    content_type: Optional[str] = None, uploaded_by: Optional[str] = None,
    board: Optional[str] = None, max_bytes: Optional[int] = None,
) -> int:
    """Single attachment write path (dashboard, tools, CLI): size cap, safe
    basename, collision-free blob under :func:`task_attachments_dir`, then the
    metadata row. Raises :class:`AttachmentTooLarge` / ``ValueError``; a blob
    whose row insert fails is removed before re-raising. Returns the new id."""
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        if dest_path.read_bytes() != data:
            raise OSError("attachment write verification failed: stored bytes differ from input")
        return add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise


def add_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: Optional[str] = None, size: int = 0, uploaded_by: Optional[str] = None,
) -> int:
    """Record the metadata row (+ ``attached`` event) for a blob the caller already wrote."""
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename.strip(), stored_path, content_type, int(size), uploaded_by, now),
        )
        _append_event(
            conn, task_id, "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    return [Attachment.from_row(r) for r in _task_rows(conn, "task_attachments", task_id, "created_at ASC, id ASC")]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return None if r is None else Attachment.from_row(r)


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete the row (source of truth) and best-effort its blob; None when no row matched."""
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        has_remaining_blob_reference = conn.execute(
            "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
            (att.stored_path,),
        ).fetchone() is not None
        _append_event(conn, att.task_id, "attachment_removed", {"filename": att.filename})
    if not has_remaining_blob_reference:
        with contextlib.suppress(OSError):
            p = Path(att.stored_path)
            if p.is_file():
                p.unlink()
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    return [Event.from_row(r) for r in _task_rows(conn, "task_events", task_id, "created_at ASC, id ASC")]


def _insert_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, created_at: int,
) -> None:
    """Raw comment INSERT for callers already inside a write txn (``add_comment``
    opens its own txn and emits ``commented``). Still attributable: provenance
    resolves best-effort via :func:`_inline_comment_provenance` (NULL/NULL
    renders as ``provenance unknown``, never a guess)."""
    run_id, session_ref = _inline_comment_provenance(task_id)
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, run_id, session_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (task_id, author, body, run_id, session_ref, created_at),
    )


def _journal_board_slug() -> Optional[str]:
    """Best-effort board slug for a journal record.

    Resolution must never raise inside a write txn, and must never be the
    reason a mutation fails, so every error degrades to ``None`` (which the
    journal records under the ``default`` board).
    """
    try:
        return get_current_board()
    except Exception:
        return None


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> None:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    if kind in _RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS or (
        kind == "dependency_wait" and (payload or {}).get("kind") == "dependency"
    ) or (kind == "reclaimed" and (payload or {}).get("manual") is True):
        # Snapshot comment causality in the same transaction as operator intent.
        # Old commented events cannot be correlated with comment rows reliably.
        payload = dict(payload or {})
        payload["after_comment_id"] = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (task_id,),
        ).fetchone()[0]
    actor_profile, actor_session_id = _event_actor()
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at, "
        "actor_profile, actor_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, run_id, kind, _json_or_null(payload), int(time.time()), actor_profile, actor_session_id),
    )
    # Append-only mutation journal (card t_357330bf). This is the single choke
    # point every lifecycle mutation already flows through, so journaling here
    # covers every event kind -- including ones added later -- without touching
    # the ~119 call sites. Outside the kanban home on purpose: the 2026-09-21
    # deleter took the whole kanban directory, so a journal inside it would
    # have died with the data it protects. Best-effort by contract: a journal
    # failure must never fail the board write.
    try:
        from hermes_cli import kanban_journal

        kanban_journal.append(
            _journal_board_slug(),
            task_id,
            kind,
            payload,
            run_id=run_id,
        )
    except Exception:  # pragma: no cover - the journal is a net, never a gate
        pass


def _append_deferred_event_once(
    conn: sqlite3.Connection, task_id: str, payload: dict,
) -> None:
    """Append a ``deferred`` event unless the card's newest event is already
    the identical deferral. A steady-state backlog otherwise wrote one row per
    backlogged card per dispatcher tick (FleetReview #83); a changed payload
    (new counts, reason, provider) still records."""
    last = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if last is not None and last[0] == "deferred":
        try:
            if json.loads(last[1] or "null") == payload:
                return
        except (TypeError, ValueError):
            pass
    _append_event(conn, task_id, "deferred", payload)


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
) -> Optional[int]:
    """Close the active run (``status`` defaults to ``outcome``) and clear
    ``current_run_id``; None when no run was active (never-claimed task).

    ``worker_pid`` / ``worker_started_at`` / ``claim_lock`` stay on the closed
    row: they are the only evidence left of the OS process once the task row
    is wiped, and :func:`kanban_db_dispatch.reap_terminal_workers` needs them
    to end a worker that survived its own terminal transition."""
    now = int(time.time())
    run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return None
    metadata = _carry_run_counters(conn, run_id, metadata)
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, _json_or_null(metadata), now, run_id),
    )
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    return run_id


def _carry_run_counters(conn: sqlite3.Connection, run_id: int, metadata: Optional[dict]) -> Optional[dict]:
    """Keep worker-written run counters (``placement_reapply_failed``, KWLB
    RC-9) through the close: the closing metadata replaces the column."""
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    try:
        prior = json.loads(row["metadata"]) if row and row["metadata"] else {}
    except (TypeError, ValueError):
        return metadata
    key = _kwh.REAPPLY_FAILED_KEY
    if not isinstance(prior, dict) or key not in prior:
        return metadata
    merged = dict(metadata or {})
    try:
        stored = int(prior[key])
    except (TypeError, ValueError):
        return merged if key in merged else {**merged, key: prior[key]}
    try:
        incoming = int(merged[key]) if key in merged else None
    except (TypeError, ValueError):
        incoming = None
    # keep_max: a counter never goes down through the close (Prism 94214e112dc1).
    merged[key] = stored if incoming is None else max(stored, incoming)
    return merged


def _first_line(text: Optional[str], limit: int) -> str:
    """First non-blank-stripped line of ``text`` capped at ``limit`` chars; "" when empty."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _opt_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` when ``value`` is ``None`` (NULL column passthrough)."""
    return int(value) if value is not None else None


def _json_or_null(obj: Any) -> Optional[str]:
    """JSON text for a payload/metadata column; falsy -> NULL."""
    return json.dumps(obj, ensure_ascii=False) if obj else None


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Current ``tasks.status`` for ``task_id``, or ``None`` when no such row."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None

# Distinguishes "caller named the acting profile" (which may legitimately be
# None for an unassigned card) from "read the card's current assignee".
_UNSET: Any = object()


def _end_or_synthesize_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, status: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None, synthesize: bool,
    profile: Any = _UNSET,
) -> Optional[int]:
    """:func:`_end_run`; when no run was active and ``synthesize`` holds, record a
    zero-duration run instead so the handoff fields survive in attempt history.
    ``profile`` overrides the profile read off the task row for the synthesized
    run — transitions that reassign the task (e.g. review handoff) pass the
    acting profile captured before the rewrite."""
    run_id = _end_run(conn, task_id, outcome=outcome, status=status, summary=summary, metadata=metadata)
    if run_id is None and synthesize:
        run_id = _synthesize_ended_run(conn, task_id, outcome=outcome, summary=summary, metadata=metadata, profile=profile)
    return run_id


def _synthesize_ended_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None,
    profile: Any = _UNSET,
) -> int:
    """Zero-duration closed run for a terminal transition on a never-claimed
    task, so the handoff fields aren't silently dropped (``_end_run`` is a
    no-op then). ``started_at == ended_at`` keeps elapsed stats honest. Does
    NOT touch the tasks row.

    ``profile`` overrides the profile read off the task row: transitions that
    reassign the task (e.g. review handoff) pass the acting profile captured
    before the rewrite, so the run names the actor, not the new assignee."""
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if profile is _UNSET:
        profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key, outcome, outcome, summary, error, _json_or_null(metadata),
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------
def _latest_block_source(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Classify the task's active block as ``initial_status`` or ``explicit``.

    The exact legacy reason is recognized so cards created before the source
    tag was introduced receive the same dependency-release behavior.
    """
    row = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or row["kind"] != "blocked":
        return None
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if isinstance(payload, dict):
        if payload.get("source") == "initial_status":
            return "initial_status"
        explicit_fields = {
            "kind", "source_status", "recurrences", "classified_in_place",
            "requested_kind", "rekind_reason",
        }
        if (
            payload.get("reason") == "created with initial_status=blocked"
            and explicit_fields.isdisjoint(payload)
        ):
            # Compatibility for genuine pre-tag creation events. ``reason`` is
            # caller-controlled on block_task, so it is only a legacy marker
            # when no explicit-block provenance fields are present.
            return "initial_status"
    return "explicit"


def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when ``task_id`` has an active block event, or the newest
    ``gave_up`` says the block must wait for an operator.

    Creation-time holds are sticky while parentless, but unlike explicit
    worker/operator blocks they may be released by ``recompute_ready`` after a
    real ``blocks`` parent reaches a terminal state (:func:`_latest_block_source`).

    A breaker trip ``_record_task_failure`` stamped ``sticky`` — the clean-exit
    protocol-violation budget or a systemic same-error wave — trips on a policy
    independent of ``consecutive_failures``, so ``recompute_ready``'s counter
    check cannot see it; without this the trip is promoted back to ``ready`` in
    the same tick and the card respawns forever. A plain (unified-budget)
    ``gave_up`` carries no marker and is judged by the counter, so raising
    ``failure_limit`` or ``assign_task`` to a fresh profile still releases it; a
    task with no such event at all (direct DB edit) auto-recovers.
    """
    if _latest_block_source(conn, task_id) is not None:
        return True
    trip = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gave_up' AND id > COALESCE("
        "  (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id),
    ).fetchone()
    return bool(trip) and bool(_json_dict(trip["payload"]).get("sticky"))


def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Newest ``task_events`` row of ``kind`` (optionally scoped to one run)."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params: tuple[Any, ...] = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def find_parent_satisfied_sticky_blocks(conn: sqlite3.Connection) -> list[str]:
    """Name explicit blocks whose graph dependencies are all terminal."""
    rows = conn.execute(
        "SELECT t.id FROM tasks t "
        "WHERE t.status = 'blocked' "
        "AND EXISTS ("
        "  SELECT 1 FROM task_links l WHERE l.child_id = t.id "
        "  AND COALESCE(l.kind, ?) = ?"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "  WHERE l.child_id = t.id AND COALESCE(l.kind, ?) = ? "
        "  AND p.status NOT IN ('done', 'archived')"
        ") ORDER BY t.id",
        (DEFAULT_LINK_KIND, LINK_KIND_BLOCKS, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
    ).fetchall()
    return [
        row["id"] for row in rows
        if _latest_block_source(conn, row["id"]) == "explicit"
    ]


def find_stranded_by_triage(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Return ``(child_id, parent_id)`` pairs stranded behind a human-gated parent.

    A child is *stranded* when it sits in ``todo`` (the column
    ``recompute_ready`` holds it in while parents are open) and at least one
    blocking parent is in ``triage`` or ``blocked`` — a status that only a
    human can clear. Provenance-only parents and parents in
    ``ready``/``running``/``review`` are excluded: those do not strand the
    child.

    This is a pure read used to make a zero-spawn dispatch tick legible. The
    dispatcher is right to hold these children; the defect was that it held
    them *silently*, so a triaged parent froze an entire subtree while
    ``Spawned: 0`` looked exactly like an idle board.

    Ordered by child id then parent id so the report is stable across ticks
    (an operator diffing two ticks should see content changes, not shuffling).
    """
    rows = conn.execute(
        "SELECT l.child_id AS child, l.parent_id AS parent "
        "FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE c.status = 'todo' AND p.status IN ('triage', 'blocked') "
        "AND COALESCE(l.kind, ?) = ? "
        "ORDER BY l.child_id, l.parent_id",
        (DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
    ).fetchall()
    return [(r["child"], r["parent"]) for r in rows]


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', "
        "'unblocked', 'changes_requested', 'status', 'reclaimed', "
        # 'review_reopened' is historical (verb retired); legacy rows still count.
        "'review_reopened', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited', "
        "'infra_unavailable'"
        ") ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"


def recompute_ready(conn: sqlite3.Connection, failure_limit: int = None) -> int:
    """Promote ``todo``/``blocked`` tasks whose parents are all done/archived;
    returns the count. Opens its own IMMEDIATE txn — call OUTSIDE any write txn.

    ``blocked`` is skipped when sticky (explicit ``kanban_block``) or when
    ``consecutive_failures`` reached the limit (else the breaker could never
    trip). Limit order matches ``_record_task_failure``: ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* in two cases:

    1. The active block was an explicit worker/operator ``kanban_block``;
       those stay blocked until ``kanban_unblock`` (#28712). A creation-time
       ``initial_status=blocked`` hold is different: it stays sticky while
       parentless (the human-ops/R3 gate), but auto-releases when it has at
       least one ``blocks`` parent and every such parent is terminal.

    2. The task's ``consecutive_failures`` has reached the effective
       failure limit.  This prevents infinite retry loops when a task
       repeatedly exhausts its iteration budget: without this guard the
       counter would reset on every recovery cycle and the circuit
       breaker could never trip (#35072).

    The effective failure limit resolves in the same order as the
    circuit breaker in ``_record_task_failure`` so the two never
    disagree about when a task is permanently blocked:

      1. per-task ``max_retries`` if set
      2. caller-supplied ``failure_limit`` (the dispatcher passes the
         ``kanban.failure_limit`` config value through ``dispatch_once``)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            parents = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ? AND COALESCE(l.kind, ?) = ?",
                (task_id, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
            ).fetchall()
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                block_source = _latest_block_source(conn, task_id)
                if block_source != "initial_status" or not parents:
                    # Explicit worker/operator blocks always require an unblock.
                    # Parentless creation holds are the human-ops/R3 gate and
                    # remain sticky too. Only a creation hold backed by at
                    # least one real dependency edge may auto-release.
                    continue
            if all(p["status"] in ("done", "archived") for p in parents):
                resume_status = _resume_status_from_events(conn, task_id)
                if cur_status == "blocked":
                    # At the breaker limit, no auto-recovery (else block ->
                    # recover -> respawn -> exhaust -> block forever). The
                    # counter is preserved so it accumulates across cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = ? "
                        "WHERE id = ? AND status = 'blocked'", (resume_status, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE id = ? AND status = 'todo'",
                        (resume_status, task_id),
                    )
                _append_event(
                    conn, task_id, "promoted",
                    {"status": resume_status} if resume_status != "ready" else None,
                )
                promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------
def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether every direct parent is terminal for dependency gating.

    Only ``blocks`` edges gate (``derived-from`` is provenance-only); NULL
    kinds COALESCE to the default so a hand-edited DB can't un-gate a dep.
    """
    return conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND COALESCE(l.kind, ?) = ? "
        "AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
    ).fetchone() is None


def unsatisfied_parents(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """``(parent_id, status)`` for every direct parent :func:`_parents_satisfied`
    still counts as open (``done`` / ``archived`` release the child), in id
    order, so a refusal or a board view can name the blockers instead of the
    caller guessing. Read-only."""
    rows = conn.execute(
        "SELECT p.id, p.status FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') "
        "ORDER BY p.id", (task_id,),
    ).fetchall()
    return [(row["id"], row["status"]) for row in rows]


def _claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: Optional[dict] = None,
) -> Optional[int]:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost. Caller holds the txn."""
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    _append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def _prior_worker_still_alive(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[dict]:
    """Evidence that ANY previous spawned owner still lives on this host.

    A release outcome is not a death certificate: operator and worker calls
    can write identical outcomes, and a newer synthetic row can mask an older
    owner. Inspect EVERY prior run at both claim doors, before a second
    spawn -- ended or still open. An open run on a claimable card is a leaked
    ``current_run_id`` (claim_task's invariant recovery closes it below); its
    owner is exactly as able to be alive as an ended run's, so the ended_at
    filter must not hide it. A claimed-but-never-spawned run has no PID to
    probe here; its active claim remains protected by the reclaim/reconcile
    guards.
    """
    # An outcome cannot certify exit: operators can write the same outcomes as
    # worker tools, and a newer synthetic row can hide an older live owner.
    runs = conn.execute(
        # The run's OWN runtime cap, snapshotted at claim: the card's current
        # cap can be shortened after release while that worker still runs
        # (FleetReview #956). A run with no snapshot is probed, never skipped.
        "SELECT r.id, r.outcome, r.ended_at, r.started_at, r.max_runtime_seconds "
        "FROM task_runs r "
        "WHERE r.task_id = ? ORDER BY r.id DESC",
        (task_id,),
    ).fetchall()
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    for row in runs:
        alive = _spawned_owner_alive(conn, task_id, row, host_prefix)
        if alive is not None:
            return alive
    return None


def _spawned_owner_alive(conn, task_id, row, host_prefix):
    """Probe one prior run's spawned owner(s) using durable event evidence."""

    # _end_run clears task_runs.worker_pid, so the durable claimed/spawned
    # events are the record. Both claim doors emit ``claimed`` with the lock
    # and run_id. A spawn that lands after the release is stamped either on
    # the old run (fenced _set_worker_pid) or, from a legacy launcher, with
    # run_id NULL -- measured on t_09180e10, 5 s after the reclaim.
    # Legacy / hand-built rows may lack the claimed event; fall back to any
    # event of that run that recorded the lock (reclaimed.prev_lock, ...).
    # Explicitly bounded workers cannot still own a run this long after its
    # release, even when the PID has since been recycled.
    if (row["max_runtime_seconds"] is not None and row["ended_at"] is not None
            and time.time() > row["ended_at"] + row["max_runtime_seconds"]
            + RECLAIM_DEFER_GRACE_SECONDS):
        return None
    run_events = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND run_id = ? "
        "ORDER BY (kind = 'claimed') DESC, id ASC",
        (task_id, row["id"]),
    ).fetchall()
    if not run_events:
        return None
    # Lower edge of the run's causal window: a worker cannot exist before its
    # claim committed. Legacy rows without a ``claimed`` event fall back to
    # the run's started_at (stamped in the same claim txn).
    claimed_at = next(
        (ev["created_at"] for ev in run_events if ev["kind"] == "claimed"),
        row["started_at"],
    )
    lock = ""
    for ev in run_events:
        try:
            detail = json.loads(ev["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        lock = (detail.get("lock") or detail.get("prev_lock")
                or detail.get("claim_lock") or detail.get("stale_lock") or "")
        if lock:
            break
    if not str(lock).startswith(host_prefix):
        return None
    boundary_id = min(ev["id"] for ev in run_events)
    # An unattributed legacy spawn belongs only to this claim interval;
    # otherwise an older run could borrow a newer run's PID.
    next_claim = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE task_id = ? "
        "AND kind = 'claimed' AND id > ?", (task_id, boundary_id),
    ).fetchone()[0]
    # Probe EVERY spawn in the interval, not just the newest: a run stamped
    # twice leaves an older PID that a later dead one must not vouch for.
    # _set_worker_pid is the only worker_pid writer and always appends this
    # event in the same txn, so an open run's row pid is covered here too.
    spawns = conn.execute(
        "SELECT id, run_id, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'spawned' AND id >= ? "
        "AND (run_id = ? OR (run_id IS NULL AND (? IS NULL OR id < ?))) "
        "ORDER BY id DESC",
        (task_id, boundary_id, row["id"], next_claim, next_claim),
    ).fetchall()
    candidates = []
    for spawned in spawns:
        try:
            payload = json.loads(spawned["payload"] or "{}")
            pid = int(payload["pid"])
        except (TypeError, ValueError, KeyError):
            continue
        token = _spawn_start_token(payload)
        candidates.append((pid, spawned["run_id"] is None,
                           spawned["created_at"], token))
    for pid, late, spawned_at, token in candidates:
        if not _pid_alive(pid):
            continue
        # Liveness is not identity: the PID may now belong to an unrelated
        # process (recycled PID). The owner is the process at this PID only
        # if its start token matches the one recorded at spawn (legacy rows:
        # if it was created inside this run's causal window).
        identity = _owner_identity(pid, claimed_at, spawned_at, token)
        if identity == "recycled":
            continue
        return {"prev_pid": pid, "prev_lock": lock,
                "prev_run_id": row["id"],
                "prev_outcome": row["outcome"],
                "prev_run_open": row["ended_at"] is None,
                "late_spawn": late,
                "owner_identity": identity,
                "needs_attention": True}
    return None

# Causal-window tolerances for owner identity, in seconds. Event timestamps
# are integer seconds (floored), so allow 1 s before the claim and 2 s after
# the spawned event. The window is deliberately ONE-SIDED around the spawn:
# ``_set_worker_pid`` writes the ``spawned`` event inside write_txn AFTER
# Popen, so under DB lock contention the event can lag the worker's real
# creation by up to the busy timeout (measured -7.5 s / -19.6 s on genuine
# workers). A symmetric +/-N s window around ``spawned_at`` would call those
# genuine workers dead and let a second worker onto the card.
#
# The window compares an EPOCH create time with wall-clock event stamps, so a
# wall-clock step between spawn and probe shifts one side and not the other
# (psutil's epoch create_time moves with the clock: Linux re-reads btime,
# macOS applies ``adjust_proc_create_time``). It is kept ONLY for legacy
# ``spawned`` rows that carry no ``start_token``.
_OWNER_CREATE_LEAD_SECONDS = 1.0
_OWNER_CREATE_LAG_SECONDS = 2.0

# Start-token match tolerance, in seconds. Both readings come from the same
# kernel field of the same process (Linux: /proc starttime ticks since boot,
# 0.01 s resolution; macOS: the raw kinfo start timeval), so a genuine owner
# matches EXACTLY; the tolerance only absorbs float round-trips through JSON.
# psutil's own PID-reuse identity uses exact equality on the same value.
_OWNER_START_TOKEN_TOLERANCE_SECONDS = 0.05


def _pid_start_token(pid: int) -> Optional[float]:
    """Clock-step-immune start stamp of ``pid``, or None if unreadable.

    psutil's ``create_time(monotonic=True)`` -- the value psutil itself uses
    for PID-reuse identity on Linux/macOS/NetBSD -- is read straight from the
    kernel and never re-based on the current wall clock, so two readings of
    one process agree across any clock step, and across processes. Other
    platforms (and a missing psutil) return None, which keeps the legacy
    causal-window check.
    """
    try:
        import psutil
        if not (psutil.LINUX or psutil.MACOS or psutil.NETBSD):
            return None
        return float(psutil.Process(int(pid))._proc.create_time(monotonic=True))
    except Exception:  # optional psutil, private-API drift, or process gone
        return None


def _boot_id() -> Optional[str]:
    """This boot's identity, or None where the start token is not boot-relative.

    A Linux start token is /proc starttime ticks SINCE BOOT, so after a reboot a
    new process at a reused PID can carry the very token a pre-boot worker
    recorded. The ``spawned`` event stamps this id next to the token so a token
    from another boot is never read as identity (C7 k109).
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _spawn_start_token(payload: Any) -> Any:
    """The ``start_token`` a ``spawned`` payload recorded, if still meaningful.

    A token stamped under a different boot proves nothing about a live PID
    now, so it is dropped and the causal-window check decides instead.
    """
    if not isinstance(payload, dict):
        return None
    token = payload.get("start_token")
    recorded_boot = payload.get("boot_id")
    if token is not None and recorded_boot and recorded_boot != _boot_id():
        return None
    return token


def _pid_create_time(pid: int) -> Optional[float]:
    """Wall-clock epoch creation time of ``pid``, or None if unreadable."""
    try:
        import psutil
        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # optional psutil, or process vanished during the probe
        pass
    if os.name == "posix":
        try:
            from datetime import datetime
            from tools.environments.local import build_subprocess_env
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(int(pid))],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=1,
                env=build_subprocess_env(scrub_secrets=False, inherit_profile_home=False, extra={"LC_ALL": "C"}),
                check=False,
            )
            if proc.returncode == 0:
                return datetime.strptime(
                    proc.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def _real_pid_started_in_claim(pid, claimed_at, spawned_at,
                               start_token=None) -> Optional[bool]:
    """Whether a live ``pid`` is the process the run recorded.

    With a recorded ``start_token`` (every spawn since t_21dfa673): ``True``
    iff the PID's current start token matches it. No wall-clock value is
    involved, so a clock step or DB lock lag cannot flip the verdict.

    Legacy rows (no token): whether ``pid`` was created inside the causal
    window ``[claimed_at - 1 s, spawned_at + 2 s]``.

    ``False``: provably not the recorded worker. ``None``: the needed reading
    is unreadable, or the ``spawned`` upper bound is missing (identity
    unproven). Missing evidence never proves a PID recycled, and never proves
    it is the worker either.
    """
    if start_token is not None:
        try:
            recorded = float(start_token)
        except (TypeError, ValueError):
            recorded = None
        if recorded is not None:
            current = _pid_start_token(pid)
            if current is None:
                return None
            return abs(current - recorded) <= _OWNER_START_TOKEN_TOLERANCE_SECONDS
    created = _pid_create_time(pid)
    if created is None:
        return None
    if claimed_at is not None and created < float(claimed_at) - _OWNER_CREATE_LEAD_SECONDS:
        return False
    if spawned_at is not None and created > float(spawned_at) + _OWNER_CREATE_LAG_SECONDS:
        return False
    if spawned_at is None:
        # Only the claim lower bound is known (no run-scoped ``spawned``
        # evidence: legacy/migrated runs). Any process created after the
        # claim -- including one that reused the worker's PID -- fits, so
        # this is UNPROVEN, not proven: termination never signals it, while
        # every liveness caller still treats it as alive (FleetReview #1021).
        return None
    return True

# Seam: tests with synthetic PIDs substitute a constant verdict here.
_pid_started_in_claim = _real_pid_started_in_claim


def _owner_identity(pid, claimed_at, spawned_at, start_token=None) -> str:
    """Classify a LIVE pid against the run that recorded it.

    Returns ``"verified"``, ``"recycled"`` (provably not the recorded
    worker), or ``"unverified"`` (start evidence unreadable -- fail CLOSED,
    the caller treats it as the owner).
    """
    started = _pid_started_in_claim(pid, claimed_at, spawned_at, start_token)
    if started is None:
        return "unverified"
    return "verified" if started else "recycled"


def _worker_owner_window(
    conn: sqlite3.Connection,
    task_id: str,
    pid: Optional[int],
    run_id: Optional[int] = None,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """The owner window ``(claimed_at, spawned_at, start_token)`` for ``pid``.

    ``start_token`` is the clock-step-immune stamp the ``spawned`` event
    recorded (t_21dfa673); when present :func:`_owner_identity` matches on it
    and ignores the wall-clock bounds, exactly like the claim guard.

    Feeds :func:`_owner_identity` for the TERMINATION and LIVENESS paths, so
    they judge a live PID by the same rule the claim guard uses (t_0ae83825):
    a live process at ``tasks.worker_pid`` is the recorded worker only if it
    was created no earlier than its run's claim and no later than the
    ``spawned`` event that recorded it.

    ``run_id`` defaults to the card's ``current_run_id``. The lower bound is
    that run's ``claimed`` event, falling back to the run's ``started_at``
    and then the card's first ``started_at`` (a worker cannot predate the
    card's first start). The upper bound is the newest ``spawned`` event of
    the run that recorded this pid. A missing bound is ``None`` and is simply
    not applied, so missing evidence can never prove a PID recycled.
    """
    if not pid:
        return (None, None, None)
    if run_id is None:
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        run_id = row["current_run_id"] if row is not None else None
    claimed_at = None
    if run_id is not None:
        ev = conn.execute(
            "SELECT created_at FROM task_events WHERE task_id = ? "
            "AND run_id = ? AND kind = 'claimed' ORDER BY id ASC LIMIT 1",
            (task_id, int(run_id)),
        ).fetchone()
        if ev is not None:
            claimed_at = ev["created_at"]
        else:
            run = conn.execute(
                "SELECT started_at FROM task_runs WHERE id = ?", (int(run_id),),
            ).fetchone()
            if run is not None:
                claimed_at = run["started_at"]
    if claimed_at is None:
        row = conn.execute(
            "SELECT started_at FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is not None:
            claimed_at = row["started_at"]
    spawned_at = None
    if run_id is not None:
        spawns = conn.execute(
            "SELECT payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind = 'spawned' AND run_id = ? ORDER BY id DESC",
            (task_id, int(run_id)),
        ).fetchall()
    else:
        spawns = conn.execute(
            "SELECT payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind = 'spawned' ORDER BY id DESC",
            (task_id,),
        ).fetchall()
    start_token = None
    for ev in spawns:
        try:
            payload = json.loads(ev["payload"] or "{}")
            if int(payload["pid"]) == int(pid):
                spawned_at = ev["created_at"]
                start_token = _spawn_start_token(payload)
                break
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return (claimed_at, spawned_at, start_token)


def _recorded_worker_alive(
    conn: sqlite3.Connection,
    task_id: str,
    pid: Optional[int],
    run_id: Optional[int] = None,
) -> bool:
    """True if ``pid`` is alive AND is not provably a recycled PID.

    Liveness is not identity: a dead, unreaped worker's PID can be reused by
    any process (measured on the live board: macOS daemons and another card's
    worker). An ``unverified`` identity (create time unreadable) counts as
    alive -- fail closed, never release beside a possible owner.
    """
    if not _pid_alive(pid):
        return False
    # Upstream's spawn-time fingerprint (``worker_started_at``) is a second
    # identity witness: a live PID that no longer matches it is a stranger
    # (post-reboot recycle), never extended or signalled. The UNVERIFIED
    # marker and a legacy NULL row carry no verdict and fall through to the
    # owner window.
    row = conn.execute(
        "SELECT worker_started_at FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    started_at = row["worker_started_at"] if row is not None else None
    if (
        started_at is not None
        and started_at != UNVERIFIED_WORKER_FINGERPRINT
        and _pid_recycled(pid, started_at)
    ):
        return False
    window = _worker_owner_window(conn, task_id, pid, run_id)
    return _owner_identity(int(pid), *window) != "recycled"


class _PendingTermination(tuple):
    """A ``(worker_pid, claim_lock, worker_started_at)`` triple (upstream's
    pid-recycle witness rides in the tuple) plus the fork's owner-identity
    window.

    Deferred (post-commit) terminations are drained after the run that
    recorded the pid has been closed, so the causal window is captured while
    the evidence is at hand. Compares and unpacks exactly like the plain
    3-tuple upstream callers use.
    """

    owner_window: tuple
    started_at: Optional[int]

    def __new__(cls, pid, claim_lock, owner_window=(None, None), started_at=None):
        obj = super().__new__(cls, (pid, claim_lock, started_at))
        obj.owner_window = tuple(owner_window)
        obj.started_at = started_at
        return obj


def _termination_window(entry) -> tuple:
    """Owner window carried by a deferred termination entry, if any."""
    return getattr(entry, "owner_window", (None, None))


def _snapshot_claimed_card_pin(task: Optional[Task]) -> Optional[Task]:
    """Record the card-row pin on a freshly claimed ``task`` (t_a30417c3).

    Called on the row read inside the claim transaction, so the snapshot is
    the pin as claimed: a later ``set-model`` (no ``--live``) cannot reach
    this run through the worker's own row read, and a lane override or a
    dispatch rung applied afterwards is not a card pin.
    """
    if task is not None:
        from hermes_cli.kanban_provider_health import model_override

        task.claimed_card_pin = model_override(task)
    return task


@_home_session_guarded("claim")
def claim_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        # Single enforcement point: never ready -> running with an undone
        # parent, whichever writer set 'ready'. Demote to 'todo';
        # recompute_ready re-promotes when the parents finish.
        if not _parents_satisfied(conn, task_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'", (task_id,),
            )
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        # Last line of defence against two workers on one card (scope item 3).
        # A ``ready`` card whose previous worker process is STILL ALIVE must
        # not be claimed into a second concurrent run — that is exactly the
        # t_09180e10 shape, where run 7394 was mid-flight when run 7414
        # claimed the same card and both committed to the same file.
        alive = _prior_worker_still_alive(conn, task_id)
        if alive is not None:
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "prior_worker_still_alive", **alive},
            )
            return None
        # Close a leaked prior run so the CAS below doesn't strand it.
        _reclaim_dangling_run(
            conn, task_id, statuses=("ready",), now=now, note="invariant recovery on re-claim",
        )
        run_id = _claim_and_open_run(conn, task_id, "ready", lock, expires, now)
        if run_id is None:
            return None
        claimed = _snapshot_claimed_card_pin(get_task(conn, task_id))
    _fire_task_hook("kanban_task_claimed", claimed, task_id, run_id)
    return claimed


# Unguarded helper (EXECUTION_LANE in test_kanban_home_session): its callers
# carry the policy. claim_review_task is the unguarded claim lane (dispatcher
# and ``claim --review``); request_changes is guarded before it gets here.
def _open_review_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lock: str,
    expires: int,
    now: int,
    session_ref: Optional[str] = None,
    operator_claim: bool = False,
) -> Optional[int]:
    """CAS ``review -> running`` and open the review run; caller holds the txn.

    Shared by :func:`claim_review_task` (dispatched reviewer) and the
    reviewer send-back in :func:`request_changes`, so both leave the same
    ``claimed(source_status=review)`` audit shape. Returns the new run id,
    or None when the card is no longer an unclaimed ``review`` card.
    """
    cur = conn.execute(
        """
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = 'review'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id,
            trow["assignee"] if trow else None,
            trow["current_step_key"] if trow else None,
            lock,
            expires,
            trow["max_runtime_seconds"] if trow else None,
            now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute(
        "UPDATE tasks SET current_run_id = ? WHERE id = ?",
        (run_id, task_id),
    )
    _append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id,
         "source_status": "review",
         **({"session_ref": session_ref} if session_ref else {}),
         **({"operator_claim": True} if operator_claim else {})},
        run_id=run_id,
    )
    return run_id


def claim_review_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
    session_ref: Optional[str] = None,
    operator_claim: bool = False,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    ``session_ref`` binds the review run to the operator session that made the
    claim (``hermes kanban claim <id> --review``). It must be derived from
    trusted runtime context by the caller, never from model-supplied args; see
    :func:`review_claim_run_for_session` for the only reader.

    ``operator_claim`` marks a claim that will NEVER spawn a worker (the
    ``hermes kanban claim --review`` CLI). Its ``claim_lock`` pid is the
    short-lived CLI process, so once that pid is dead nothing can still be
    running for the run and a reclaim may release it at once (t_c3cf232e);
    see :func:`_dead_claimer_release_at`. The dispatcher never sets it.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Parent dependencies are re-checked because a previously completed parent
    may have been reopened while this task waited in review.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL", (task_id,),
            )
            if demoted.rowcount == 1:
                _append_event(
                    conn, task_id, "dependency_wait",
                    {"reason": "parent_reopened", "source_status": "review"},
                )
            return None
        # Same last line of defence as claim_task: a review retry must not
        # start a second reviewer beside a previous run that is still alive.
        alive = _prior_worker_still_alive(conn, task_id)
        if alive is not None:
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "prior_worker_still_alive",
                 "source_status": "review", **alive},
            )
            return None
        if _open_review_run(
            conn, task_id, lock=lock, expires=expires, now=now,
            session_ref=session_ref, operator_claim=operator_claim,
        ) is None:
            return None
        return _snapshot_claimed_card_pin(get_task(conn, task_id))


def review_claim_run_for_session(
    conn: sqlite3.Connection,
    task_id: str,
    session_ref: Optional[str],
) -> Optional[int]:
    """Return the active review run id iff ``session_ref`` made its claim.

    The human review lane claims from one CLI process and comments / returns
    work from later ones, so the dispatcher env never attests to that run. The
    claim is the provenance instead: only the session recorded on the current
    run's ``claimed`` event (``source_status=review``) gets the run id back.
    ``None`` for any other session, a sessionless claim, or a card that is not
    in an active review run.
    """
    if not session_ref:
        return None
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] is None:
        return None
    run_id = int(row["current_run_id"])
    event = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    try:
        payload = json.loads(event["payload"]) if event and event["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("source_status") != "review":
        return None
    return run_id if payload.get("session_ref") == session_ref else None


@_home_session_guarded("request-changes")
def release_unbound_review_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
) -> bool:
    """Return an ORPHANED human-lane review claim to ``review``.

    A review claim that recorded no ``session_ref`` can never be used by
    :func:`review_claim_run_for_session`, so the card sits in ``running``
    under a claim nobody can send back until the TTL lapses (t_0485b3ff).
    Released only when it is provably orphaned: the active run's ``claimed``
    event is a review claim (``source_status=review``) with no session bound,
    no worker pid is attached, and the claimer process is gone (or is this
    very process). Returns True when the card went back to ``review``.
    """
    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if (
        row is None or row["status"] != "running"
        or row["current_run_id"] is None or row["worker_pid"] is not None
    ):
        return False
    run_id = int(row["current_run_id"])
    event = conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    try:
        payload = json.loads(event["payload"]) if event and event["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if (
        not isinstance(payload, dict)
        or payload.get("source_status") != "review"
        or payload.get("session_ref")
    ):
        return False
    lock = row["claim_lock"] or ""
    host, _, pid_text = lock.rpartition(":")
    try:
        lock_pid = int(pid_text)
    except ValueError:
        lock_pid = None
    # Fail closed: only a lock this host can probe proves its claimer gone.
    # A remote (or malformed) lock is unprovable, so it is never released.
    if not host or host != _claimer_id().rpartition(":")[0] or lock_pid is None:
        return False  # unprovable_claimer: remote or malformed claim lock
    if lock_pid != os.getpid() and _pid_alive(lock_pid):
        return False  # a live sessionless claimer may still be reviewing
    with write_txn(conn):
        # Recheck every release condition under the write lock: a worker may
        # have attached, or the claim rebound, since the reads above.
        cur = conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = 'running' AND current_run_id = ? "
            "AND claim_lock IS ? AND worker_pid IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM task_events e "
            "  WHERE e.task_id = tasks.id AND e.run_id = ? "
            "  AND e.kind = 'claimed' AND e.id > ?)",
            (task_id, run_id, row["claim_lock"], run_id, event["id"]),
        )
        if cur.rowcount != 1:
            return False
        closed = _end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            error=f"unbound_review_claim_released: {reason}",
        )
        _append_event(
            conn, task_id, "reclaimed",
            {
                "manual": True,
                "reason": reason,
                "prev_lock": row["claim_lock"],
                "retry_status": "review",
                "unbound_review_claim": True,
            },
            run_id=closed,
        )
    return True


def _retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` — one place, so crash/timeout/reclaim can't silently turn a
    reviewer run into an implementation run."""
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _latest_event(conn, task_id, "claimed", run_id)
    payload = _json_dict(_row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"

# Run outcome -> lifecycle status a goal loop should report for a handed-off run.
_RUN_OUTCOME_TERMINAL_STATUS = {
    "completed": "done",
    # A superseded close IS a completion by this worker — it just had no work
    # to do. Without an entry here the literal 'superseded' falls through and
    # collides with goal_run_status's OWN use of that string to mean
    # "ownership lost".
    "superseded": "done",
    "external": "done",
    "review_requested": "review",
    "changes_requested": "changes_requested",
    "blocked": "blocked",
    "dependency_wait": "blocked",
}


def goal_run_status(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Lifecycle status as seen by ONE run: terminal handoffs bind to that run,
    any other ownership loss is ``superseded`` — otherwise an old goal loop
    would read the successor's live ``running`` and mutate it."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    if expected_run_id is not None:
        row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (int(expected_run_id), task_id),
        ).fetchone()
        outcome = str(row["outcome"]) if row and row["outcome"] is not None else None
        terminal_status = _RUN_OUTCOME_TERMINAL_STATUS.get(outcome)
        if terminal_status is not None:
            return terminal_status
        if outcome is not None or task.current_run_id != int(expected_run_id):
            return "superseded"
    if task.status in {"ready", "todo"}:
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if event and event["kind"] == "changes_requested":
            return "changes_requested"
    return task.status


def heartbeat_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim; True if we still own it."""
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?", (expires, task_id, lock),
        )
        if cur.rowcount != 1:
            return False
        _extend_run_claim(conn, task_id, expires)
        return True


def _extend_run_claim(conn: sqlite3.Connection, task_id: str, expires: int) -> Optional[int]:
    """Mirror a task claim extension onto its active run row; returns that run id."""
    run_id = _current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (expires, run_id))
    return run_id


def release_stale_claims(
    conn: sqlite3.Connection, *, signal_fn=None, failure_limit: Optional[int] = None,
) -> int:
    """Reclaim ``running`` tasks whose claim expired; returns the count reclaimed.

    Every reclaim that actually releases a claim is a non-success attempt and
    is booked through ``_record_task_failure`` (#111306): a claim that expired
    without a worker ever spawning otherwise loops claim -> reclaim -> claim
    with ``consecutive_failures`` stuck at 0, so the breaker never trips.
    ``reclaim_task`` (operator path) deliberately resets the counter instead.

    A host-local worker that is still alive gets its claim *extended* instead
    (a slow model can sit longer than the TTL inside one tool-free call, so no
    heartbeat) — unless ``last_heartbeat_at`` is older than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (wedged; ``_touch_activity``
    keeps any genuinely active worker fresh). Safe to call often.

    Reclaiming a live worker mid-flight produces the spawn- then-immediately-reclaim loop seen on slow
    models that spend longer than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call (#23025):
    no tool calls means no ``kanban_heartbeat``, even though the subprocess is healthy.
    Backstop (#29747 gap 3): if the worker's PID is still alive but its ``last_heartbeat_at`` is stale by
    more than ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has been making no observable
    progress and we reclaim anyway — even if ``_pid_alive`` is still true. This catches the
    wedged-in-a-logic-loop case where the process is technically running but accomplishing nothing.
    ``_touch_activity`` (run_agent.py) bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
    so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
    ``enforce_max_runtime`` and ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = _host_prefix()
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, worker_started_at, claim_expires, last_heartbeat_at, "
        "       assignee, current_run_id "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?", (now,),
    ).fetchall()
    for row in stale:
        host_local = (row["claim_lock"] or "").startswith(host_prefix)
        hb = row["last_heartbeat_at"]
        # Backstop: a heartbeat older than the max-stale threshold means no
        # observable progress — reclaim even if the PID is alive (logic loop).
        heartbeat_stale = hb is not None and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        started_at = _row_get(row, "worker_started_at")
        # Identity, not bare liveness: a recycled holder must not keep
        # extending a dead worker's claim forever (t_0ae83825).
        if (host_local and row["worker_pid"]
                and _recorded_worker_alive(conn, row["id"], row["worker_pid"])
                and not heartbeat_stale):
            _extend_live_stale_claim(conn, row, now)
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn, started_at=started_at,
            owner_window=_worker_owner_window(
                conn, row["id"], row["worker_pid"], row["current_run_id"],
            ),
            conn=conn, task_id=row["id"], run_id=row["current_run_id"],
        )
        # A live worker of ours must keep its claim (else a duplicate spawns beside it).
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            retry_status = _retry_status_for_run(conn, row["id"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ? "
                # The release verdict was made for THIS run; a new run that
                # reuses the same lock (gateway pid) is not covered by it.
                "AND current_run_id IS ? "
                # A worker that registered its own pid since the SELECT keeps its claim.
                "AND worker_pid IS ?",
                (retry_status, row["id"], row["claim_lock"], now,
                 row["current_run_id"], row["worker_pid"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _record_reclaim(
                conn, row["id"], termination,
                error=f"stale_lock={row['claim_lock']}",
                payload={
                    "stale_lock": row["claim_lock"],
                    "worker_pid": _opt_int(row["worker_pid"]),
                    "claim_expires": int(row["claim_expires"]),
                    "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
                    "now": now,
                    "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                    "retry_status": retry_status,
                },
            )
            reclaimed += 1
        # Own txn, after the reclaim commit (same shape as ``enforce_max_runtime``):
        # the run ended without a verdict, so it counts toward the breaker and a
        # trip flips the task to ``blocked`` + ``gave_up`` on top of ``reclaimed``.
        _record_task_failure(
            conn, row["id"], f"stale_lock={row['claim_lock']}",
            outcome="reclaimed", failure_limit=failure_limit,
            release_claim=False, end_run=False,
            event_payload_extra={"worker_pid": _opt_int(row["worker_pid"]), "retry_status": retry_status},
        )
        # Post-commit observer; every non-reclaim branch ``continue``d above.
        if _kanban_observer_consumed("on_kanban_worker_stale_claim"):
            _fire_kanban_lifecycle_hook(
                "on_kanban_worker_stale_claim", row["id"], board=get_current_board(),
                assignee=row["assignee"], run_id=run_id, worker_pid=_opt_int(row["worker_pid"]),
                heartbeat_stale=bool(heartbeat_stale), retry_status=retry_status,
            )
    return reclaimed


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    run_id = _end_run(
        conn, task_id, outcome="reclaimed", status="reclaimed", error=error, metadata=termination,
    )
    payload.update(termination)
    _append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id


def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    new_expires = now + _resolve_claim_ttl_seconds()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _extend_run_claim(conn, row["id"], new_expires)
        _append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": int(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )


@_home_session_guarded("reclaim")
def reclaim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    signal_fn=None,
    operator: Optional[str] = None,
) -> bool:
    """Operator-driven reclaim: release the claim and restore its source phase.

    Unlike :func:`release_stale_claims` which only acts on tasks whose
    ``claim_expires`` has passed, this function reclaims immediately
    regardless of TTL. Intended for the dashboard/CLI recovery flow
    when an operator wants to abort a running worker without waiting
    for the TTL to expire (e.g. after seeing a hallucination warning).

    If the worker was signalled but survived, or if this host holds the
    claim without a worker pid and its claimer may still be alive,
    reclamation FAILS CLOSED.
    The card retains its owner and emits a ``reclaim_refused`` event marked
    ``needs_attention``. A human must resolve the worker outside this path;
    an operator request alone does not prove the worker is gone.

    Measured incident ``t_09180e10`` (2026-09-22): the worker sweep saw
    ``worker_pid IS NULL`` 197 s into a claim, called this function, and got
    ``status='ready'`` back. The spawn was still in flight — the ``spawned``
    event (pid 26401) landed **5 seconds after** the reclaim. The dispatcher
    then claimed the re-queued card and started a second worker; both runs
    committed to the same file 90 s apart.

    A reclaim releases a *claim*; it never promotes. Rows parked in
    ``blocked`` / ``triage`` / ``scheduled`` keep that status and only
    shed their stale claim residue — promoting them here would feed them
    straight back to the dispatcher and defeat the block. Use
    :func:`unblock_task` or :func:`promote_task` to actually re-queue one.

    ``operator`` (``--operator "<who: why>"``) overrides exactly one hold:
    a host-local claim with NO stamped worker pid whose claimer pid is proven
    gone (ESRCH), still inside the dead-claimer launch bound. The typical
    shape is ``claim --review`` from a short-lived CLI process: the claimer
    exits at once and no worker is ever spawned, yet the launch bound held the
    card for a full claim TTL with no escape (t_6451e7c9). The override is
    ledgered on the ``reclaimed`` event. A live claimer, a stamped worker pid,
    or a signalled survivor is still refused.

    Returns True if a reclaim happened, False if the task isn't in a
    reclaimable state (not running, or doesn't exist) or if the worker's
    death cannot be proven.
    """
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    # ``reclaim --operator`` / ``reassign --reclaim --takeover``: authorize
    # under the write lock BEFORE the termination signal. The recheck inside
    # the later release txn cannot un-signal a worker the dispatcher claimed
    # after the preflight (t_920c6b4a, FleetReview 946120c1ae67).
    authorize_pending_operator_gate(conn)
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn, started_at=row["worker_started_at"],
        owner_window=_worker_owner_window(
            conn, task_id, row["worker_pid"], row["current_run_id"],
        ),
        conn=conn, task_id=task_id, run_id=row["current_run_id"],
    )
    # Never release a claim while our host-local worker is alive or its
    # liveness is unknown. This also covers NULL pid in the TTL and stale
    # paths that share the predicate. A request is not proof of death.
    override_ok = False
    if operator and _worker_survived_termination(termination):
        # The override relaxes liveness, so it needs the same authorization
        # as every other operator flag on a card with an active run: the
        # operator token (or the card's own dispatcher grant). A bare
        # ``--operator`` string is not authority (FleetReview on #1404).
        token = _operator_token_state()
        if token == "ok" or _caller_holds_grant_for(conn, task_id):
            override_ok = True
        else:
            termination["operator_override_refused"] = f"token_{token}"
    if (
        override_ok
        and _dead_claimer_hold_only(row["worker_pid"], termination)
        and not _host_process_mentions_task(task_id)
    ):
        termination["operator_override"] = str(operator)
        termination["operator_override_basis"] = "dead_claimer_no_worker_pid"
        termination["liveness_unprovable"] = False
        termination["terminated"] = True
    if _worker_survived_termination(termination):
        _refuse_reclaim_unproven_death(
            conn, task_id, prev_lock, termination, reason=reason,
        )
        return False
    # A reclaim RELEASES A CLAIM; it is not a promotion. Laundering a
    # ``blocked``/``triage``/``scheduled`` row into ``ready`` here hands it
    # straight to the dispatcher, which claims + spawns a worker on the next
    # tick — silently defeating ``--initial-status blocked``, a worker's
    # ``review-required`` handoff, and the circuit breaker alike. Those rows
    # keep their status and only lose the stale claim residue. Reclaiming a
    # blocked card is also invisible to ``_has_sticky_block`` (it emits
    # ``reclaimed``, never ``unblocked``), so the pre-fix path produced a
    # genuinely incoherent row: ``status='ready'`` while still sticky-blocked.
    # ``unblock_task`` / ``promote_task`` remain the only ways out of blocked.
    held_status = row["status"]
    preserve_status = held_status in ("blocked", "triage", "scheduled")
    with write_txn(conn):
        if termination.get("operator_override") and (
            _dead_claimer_release_at(conn, task_id)[1] != "launch_bound"
        ):
            # The override was granted on "no worker evidence". Worker evidence
            # (a ``spawned``/``heartbeat`` event) that landed after that check
            # voids it: re-read it under the write lock (FleetReview
            # 8a8140b0725a on #1404).
            return False
        retry_status = _retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked', "
            "'triage', 'scheduled') "
            "AND claim_lock IS ? "
            # Fence to the run the termination verdict inspected: a gateway
            # dispatcher run reusing the same lock (spawn in flight, pid not
            # stamped yet) must not be released by it (FleetReview ffdcb1bab162).
            "AND current_run_id IS ? "
            # ... and to the worker pid it inspected: a pid stamped after the
            # verdict is a live worker the verdict never saw (FleetReview
            # 8a8140b0725a on #1404).
            "AND worker_pid IS ?",
            (held_status if preserve_status else retry_status, task_id, prev_lock,
             row["current_run_id"], row["worker_pid"]),
        )
        if cur.rowcount != 1:
            return False
        payload = {
            "manual": True,
            "reason": reason,
            "prev_lock": prev_lock,
            "retry_status": retry_status,
        }
        if preserve_status:
            # Make the non-promotion explicit in board history so an operator
            # who reclaims a blocked card can see why it did NOT go ready.
            payload["status_preserved"] = held_status
        _record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload=payload,
        )
    # Operator intervention — they've looked at the task, so the
    # consecutive-failures counter is now stale. Give the next retry
    # a fresh budget. (_clear_failure_counter opens its own write_txn,
    # so it runs after the enclosing one commits.)
    _clear_failure_counter(conn, task_id)
    return True


@_home_session_guarded("reassign")
def reassign_task(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], *, reclaim_first: bool = False,
    reason: Optional[str] = None,
    receipt: Optional[dict] = None,
    request_changes_reason: Optional[str] = None,
    operator: Optional[str] = None,
    pr_query=None,
    worker_ok: bool = False,
) -> bool:
    """Reassign a task, optionally reclaiming a stuck running worker first.

    ``request_changes_reason`` / ``operator`` / ``pr_query`` pass through to
    :func:`assign_task` (the review-hold gate). A :class:`ReviewHoldRequired`
    refusal is written to ``receipt["hold_error"]`` when a receipt is given,
    else raised.

    This is the recovery path for "this profile's model is broken, try
    a different one". If ``reclaim_first`` is True, any active claim is
    released (via :func:`reclaim_task`) before the reassign happens;
    otherwise the function refuses to reassign a currently-running task
    and returns False (caller can retry with ``reclaim_first=True``).

    Returns True if the reassign landed. ``profile`` may be ``None`` to
    unassign entirely.

    ``reclaim_first`` makes this a two-step operation whose FIRST step is
    irreversible (SIGTERM to a live worker, claim released) and whose second
    can still fail. Returning a bare ``False`` there is a lie by omission:
    the caller reports "nothing happened / still running" while the worker is
    already dead. Pass ``receipt`` — a dict this function fills with
    ``reclaimed`` (bool) and, on failure, ``reclaim_error`` (str) — to report
    what actually happened. The reclaim leg never raises through this
    function for the same reason the set-model batch catches by position:
    an irreversible effect that already fired must not take its own receipt
    down with it.

    The ``no_worker`` refusal (:class:`NoWorkerFlagSet`) is checked BEFORE
    the reclaim, so a refused reassign never signals a live worker.
    """
    if not worker_ok:
        refusal = _no_worker_refusal(conn, task_id, _canonical_assignee(profile))
        if refusal:
            raise NoWorkerFlagSet(refusal)
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        try:
            reclaimed = bool(reclaim_task(conn, task_id, reason=reason or "reassign"))
            if receipt is not None:
                receipt["reclaimed"] = reclaimed
        except BaseException as exc:  # noqa: BLE001 - see docstring
            if receipt is None:
                raise
            receipt["reclaimed"] = False
            receipt["reclaim_error"] = (
                f"{exc.__class__.__name__}: {exc or 'no detail'}; "
                "worker may already have been signalled or claim released — "
                f"inspect task {task_id}"
            )
            return False
    # assign_task handles its own txn + the still-running guard. After a
    # committed reclaim, *any* assign failure must preserve that receipt;
    # even its post-commit observer can raise after assignment landed.
    try:
        return assign_task(
            conn, task_id, profile, request_changes_reason=request_changes_reason,
            operator=operator, pr_query=pr_query, worker_ok=worker_ok,
        )
    except ReviewHoldRequired as exc:
        if receipt is None:
            raise
        receipt["hold_error"] = str(exc)
        return False
    except BaseException as exc:  # noqa: BLE001 - post-reclaim receipt boundary
        if receipt is not None and receipt.get("reclaimed"):
            if isinstance(exc, RuntimeError) and str(exc).startswith(
                f"cannot reassign {task_id}: currently running (claimed)."
            ):
                # A new claim raced the assign; do not repeat assign_task's
                # advice to reclaim it (that now belongs to another worker).
                return False
            receipt["assign_error"] = f"{exc.__class__.__name__}: {exc or 'no detail'}"
            return False
        if isinstance(exc, RuntimeError):
            # Existing still-running refusal without a reclaim.
            return False
        raise


def _verify_created_cards(
    conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom). Verified = the row
    exists AND ``created_by`` is the completing task's assignee or id, OR the
    card is linked as its child (created elsewhere, attached by the worker).
    Never mutates."""
    ordered = list(dict.fromkeys(str(x).strip() for x in (claimed_ids or []) if str(x).strip()))
    if not ordered:
        return [], []

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,)).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})", tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        trusted = created_by is not None and (
            (completing_assignee and created_by == completing_assignee)
            or created_by == completing_task_id
            or cid in linked_children
        )
        (verified if trusted else phantom).append(cid)
    return verified, phantom

# Task-id pattern used both by ``kanban_create`` (``t_<12 hex>``) and
# ``_new_task_id`` below. Kept permissive on length for forward compat:
# accept 8+ hex chars after the ``t_`` prefix.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """``t_<hex>`` references in ``text`` that don't resolve to a task (deduped; advisory)."""
    if not text:
        return []
    return _missing_task_ids(conn, dict.fromkeys(_TASK_ID_PROSE_RE.findall(text)))


class HallucinatedCardsError(ValueError):
    """``complete_task`` refused: ``created_cards`` has ids that don't exist or
    weren't created by this worker (``.phantom``). A ``ValueError`` so tool
    error handlers treat it as recoverable."""

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class EmptyCompletionError(ValueError):
    """``complete_task`` refused: no substantive ``result``, ``summary``, or
    stored result. A ``ValueError`` so tool error handlers treat it as
    recoverable. Review approvals are exempt (the human is the record)."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} has no result or summary evidence"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


class LiveClaimError(ValueError):
    """``complete_task`` refused: the task is ``running`` under a live claim and
    the caller neither owns its run (``expected_run_id``) nor passed ``force``.
    Completing anyway would close the worker's run row underneath a process
    that is still executing. A ``ValueError`` so tool error handlers treat it
    as recoverable."""

    def __init__(self, task_id: str):
        super().__init__(
            f"{task_id} is running under a live worker claim; pass expected_run_id "
            "(worker ownership) or force=True (explicit operator override) instead "
            "of closing the live run"
        )


def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim still protects a run: the worker process
    it spawned exists (PID + start-time fingerprint). A claim whose worker is gone,
    or a library/CLI claim that never spawned one, has no run to protect. TTL expiry
    is deliberately not consulted: ``reclaim_stale_tasks`` extends, not reclaims, the
    claim of a live worker, so the process is the liveness authority here too."""
    return bool(
        trow["status"] == "running"
        and trow["claim_lock"] is not None
        and trow["worker_pid"]
        and _worker_alive(trow["worker_pid"], trow["worker_started_at"])
    )

# Evidence pointer for a superseded close, capped so a worker cannot paste a
# whole transcript into the durable field the board renders. An overlong value
# is REFUSED, never sliced: a cut URL can still look valid while naming a
# different resource, and a cut watcher id no longer names the watcher.
_SUPERSEDED_POINTER_MAX = 500


def _overlong(value: str) -> str:
    """The refusal reason for an evidence field over the cap, else ``""``."""
    if len(value) > _SUPERSEDED_POINTER_MAX:
        return f"is {len(value)} chars, over the {_SUPERSEDED_POINTER_MAX}-char cap"
    return ""


class EmptySupersedeError(ValueError):
    """Raised by ``complete_task`` when ``superseded_by`` is blank.

    The pointer is the ONLY evidence a superseded close carries, so an empty
    one would record that the card's work vanished without recording what
    replaced it. ``ValueError`` so existing tool-error handlers treat it as a
    recoverable user error the worker can retry.
    """

    def __init__(self, task_id: str, why: str = ""):
        self.task_id = task_id
        if why:
            # Overlong pointer (refused rather than truncated).
            super().__init__(
                f"completion blocked: {task_id} superseded_by {why}; pass a short pointer "
                f"(card id, PR url or sha), not a transcript. {task_id} is unchanged"
            )
            return
        super().__init__(
            f"completion blocked: {task_id} was completed as superseded with an empty "
            f"superseded_by; name the card, PR or sha that satisfied the premise "
            f"(an unnamed supersede is a silent delete of the work)"
        )


class ExternalCloseError(ValueError):
    """Raised by ``complete_task`` when an ``external`` close lacks its evidence.

    An external close (t_768c9e91) is terminal only because a watcher reopens
    the card when the outside gate flips; without the upstream URL and the
    watcher id it is an unwatched drop of the work. Nothing is mutated.
    """

    def __init__(self, task_id: str, why: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} external close {why}; pass --external "
            f"<http(s) url of the upstream PR/issue> and --watcher <id of the watcher that "
            f"reopens the card when it merges or closes>. {task_id} is unchanged"
        )


class EmptyDraftOverrideError(ValueError):
    """Raised by ``complete_task`` when ``draft_ok`` is given but blank.

    ``draft_ok`` lifts the DRAFT-PR completion refusal for ONE card
    (t_f38605be); the reason is the audit record, so an empty one is refused.
    """

    def __init__(self, task_id: str, why: str = ""):
        self.task_id = task_id
        if why:
            super().__init__(
                f"completion blocked: {task_id} draft_ok {why}; give a one-line reason. "
                f"{task_id} is still in-flight (no state change)"
            )
            return
        super().__init__(
            f"completion blocked: {task_id} passed an empty draft_ok; give the reason "
            f"the named DRAFT PR is intentionally left open, e.g. 'CI vehicle for "
            f"upstream PR o/r#N'. {task_id} is still in-flight (no state change)"
        )


def _merged_survivor_prs(metadata: Optional[dict], survivor_pr) -> Optional[list]:
    """``[("o/r#N @ <merge sha>", "<pr head sha>", "<merge sha>"), ...]`` when
    EVERY fleet PR the handoff owns (``--survivor-pr`` + metadata
    pr_url/pr_urls/pr) is REST ``merged=true``; None when there is none, any is
    not merged, or a lookup cannot tell. The full head and merge shas ("" when
    unknown) tie the PR to a checkout's trunk and content.
    """
    from hermes_cli import kanban_open_pr as _open_pr

    own = _open_pr.split_fleet(
        _open_pr.extract_pr_refs(metadata=metadata, survivor_pr=survivor_pr))[0]
    query = _open_pr.memo_query() if own else None
    if query is None:
        return None
    merged = []
    for ref in own:
        try:
            state = query(ref.repo, ref.number)
        except Exception as exc:
            _log.warning("branch-base: %s#%s lookup failed: %s", ref.repo, ref.number, exc)
            return None
        if not isinstance(state, dict) or str(state.get("state") or "").upper() != "MERGED":
            return None
        merge_sha = str(state.get("merge_commit_sha") or "")
        merged.append((f"{ref.repo}#{ref.number} @ {merge_sha[:12] or '?'}",
                       str(state.get("head_sha") or ""), merge_sha))
    return merged


def _enforce_branch_base(
    conn: sqlite3.Connection, task: "Task", metadata: Optional[dict],
    survivor_pr=None,
) -> None:
    """Branch-base guard on a worker handoff (t_18e781d0).

    Raises :class:`kanban_branch_base.StaleBaseError` -- before any mutation
    other than one audit event -- when a checkout in the worker's workspace
    is cut from a stale/foreign base. Fail-open on anything it cannot measure.

    A handoff whose own PRs are all already merged (t_22c91696) passes: the
    stale post-merge workspace branch is measured against a trunk that holds
    the squash of that very PR, so the "foreign" commit and the conflict are
    the merge itself. Recorded as ``base_guard_survivor_merged``. An OPEN PR
    keeps the guard -- that is the branch that will not land. The merged PR
    must be TIED to each failing checkout (FleetReview #1394, #1434): its head
    contains the checkout's HEAD, its merge commit is on the checkout's own
    trunk, and the checkout's change is present in that merge commit
    (:func:`kanban_branch_base.pr_landed_checkout`), else naming an unrelated,
    fork-merged or reverted PR would excuse foreign commits and conflicts.
    """
    from hermes_cli import kanban_branch_base as _bb

    try:
        checked = _bb.enforce_handoff(
            task.id,
            workspace_path=task.workspace_path,
            workspace_kind=task.workspace_kind,
            scope=sorted(_extract_explicit_dispatch_file_paths(task.body)),
            created_at=task.created_at,
            metadata=metadata,
        )
    except _bb.StaleBaseError as err:
        failures = {r.repo: r.failures for r in err.reports}
        merged = _merged_survivor_prs(metadata, survivor_pr)
        untied: list = []
        if merged:
            prs = [(head, merge_sha) for _, head, merge_sha in merged]
            untied = [r.repo for r in err.reports if not _bb.pr_landed_checkout(r, prs)]
            if not untied:
                with write_txn(conn):
                    _append_event(conn, task.id, "base_guard_survivor_merged", {
                        "survivor_merged": [m[0] for m in merged],
                        "failures": failures,
                    })
                return
        payload = {"failures": failures}
        if untied:
            payload["survivor_merged_untied"] = untied
        with write_txn(conn):
            _append_event(conn, task.id, "completion_blocked_stale_base", payload)
        raise
    except Exception as exc:  # the guard must never break a handoff by crashing
        _log.warning("branch-base guard skipped for %s: %s", task.id, exc)
        return
    if checked and checked.get("override"):
        with write_txn(conn):
            _append_event(conn, task.id, "base_guard_overridden", checked)


def _routed_own_prs(conn: sqlite3.Connection, task_id: str) -> list:
    """The own PRs of EVERY open-PR-routed run (``own_prs``; legacy: ``auto_routed_open_prs``), deduped.

    A later prose-only route records ``own_prs=[]``; that must not erase a PR an earlier route owned.
    """
    out: list = []
    for run in list_runs(conn, task_id):
        md = run.metadata if isinstance(run.metadata, dict) else {}
        if "auto_routed_open_prs" in md:
            for pr in (md.get("own_prs") if "own_prs" in md else md.get("auto_routed_open_prs")) or []:
                if pr not in out:
                    out.append(pr)
    return out


def _enforce_routed_pr_survivor(
    conn: sqlite3.Connection, task_id: str, *, survivor_pr, survivor_ref,
    abandon_routed_pr: Optional[str], query_fn,
) -> Optional[dict]:
    """Refuse ``done`` on a survivor claim that skips the card's still-OPEN routed PR (t_829fce95).

    t_cee802d5 was routed to review on its own open PR, then closed with an
    unbound ``--survivor-pr`` naming an unrelated merged PR. Gated only when a
    survivor claim is given; a claim naming the routed PR passes. A non-empty
    ``abandon_routed_pr`` lets it through: the returned ``routed_pr_abandoned``
    payload is recorded by the caller in the SAME transaction as ``done``, so a
    completion that later fails (e.g. survivor verification) leaves no audit.
    Raises :class:`kanban_open_pr.RoutedPrOpenError` before any mutation.
    """
    from hermes_cli import kanban_open_pr as _open_pr
    if not (survivor_pr or survivor_ref):
        return None
    routed = _routed_own_prs(conn, task_id)
    if not routed:
        return None
    opened, unverified = _open_pr.routed_prs_still_open(
        routed, survivor_pr=survivor_pr, query_fn=query_fn)
    if not (opened or unverified):
        return None
    reason = str(abandon_routed_pr).strip() if abandon_routed_pr is not None else None
    if reason and not _overlong(reason):
        return {"prs": opened + unverified, "reason": reason}
    blank = abandon_routed_pr is not None
    with write_txn(conn):
        _append_event(conn, task_id, "completion_blocked_routed_pr_open",
                      {"prs": opened, "unverified": unverified,
                       **({"reason": "empty_or_overlong_abandon_reason"} if blank else {})})
    raise _open_pr.RoutedPrOpenError(task_id, opened, unverified, blank_reason=blank)


@_home_session_guarded("complete")
def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
    fire_lifecycle_hook: bool = True,
    force: bool = False,
    survivor_ref: Optional[Union[str, Sequence[str]]] = None,
    survivor_pr: Optional[Union[str, Sequence[str]]] = None,
    survivor_unbound: Union[bool, str, Sequence[Union[bool, str]], None] = None,
    survivor_none: bool = False,
    survivor_reason: Optional[str] = None,
    superseded_by: Optional[str] = None,
    draft_ok: Optional[str] = None,
    external: Optional[str] = None,
    watcher: Optional[str] = None,
    abandon_routed_pr: Optional[str] = None,
) -> bool:
    """Transition ``running|ready|blocked|review -> done`` and record ``result``.

    ``abandon_routed_pr`` (t_829fce95) is the audited override for
    :func:`_enforce_routed_pr_survivor`: a survivor claim naming a different PR
    while the card's routed own PR is still OPEN is refused without it.

    ``external`` + ``watcher`` (t_768c9e91) close a card whose remaining step
    is an outside party's (an upstream maintainer merge): the closing run's
    outcome is ``external``, the URL and the watcher id are the evidence (run
    metadata ``external`` and the ``completed`` event), and no summary, receipt
    or survivor PR is required. The watcher, not the card, owns the wait: it
    reopens the card when the upstream closes unmerged. Both are required and
    the URL must be http(s) (:class:`ExternalCloseError`, audited, no mutation).

    ``draft_ok`` is the audited per-card override for the DRAFT-PR refusal
    (t_f38605be): a non-empty reason lets a handoff name an intentionally-open
    draft PR. The draft is dropped from the open-PR route, and a
    ``completion_draft_override`` event records the PRs and the reason. A blank
    reason is refused (:class:`EmptyDraftOverrideError`); without it the
    default :class:`DraftPrError` refusal is unchanged.

    ``superseded_by`` closes a card whose premise is ALREADY SATISFIED on
    current main — the sibling card, PR or sha that did the work. The closing
    run's outcome is ``superseded`` rather than ``completed``, and the pointer
    IS the evidence, so no ``summary``/``result`` is required. An empty /
    whitespace-only pointer is refused (:class:`EmptySupersedeError`) — a
    superseded card with no pointer is a silent delete of work. Survivor
    preservation is deliberately NOT skipped: ``preserve`` already returns
    ``None`` when the run produced nothing, so a genuine no-op close needs no
    survivor, while a worker that DID change files still cannot use this
    disposition to escape the survivor gate. Without this verb a worker that
    finds its card already done has no honest option: completion demands work
    it did not do and blocking demands a blocker that does not exist, so it
    exits rc=0 and the dispatcher books a protocol violation and retries.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence. ``review`` is accepted so a human
    (or reviewer) can approve a task parked in the review lane by
    :func:`request_review` — even when it has no active run
    (``current_run_id IS NULL``), the handoff fields are preserved via
    :func:`_synthesize_ended_run`.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.

    A ``running`` task under a live claim is only completed with proof of
    ownership (``expected_run_id``) or ``force=True`` (explicit operator
    override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. Completions from non-review statuses need
    evidence: a stripped ``result`` or ``summary``, or a stripped result already
    stored on the card; empty evidence raises :class:`EmptyCompletionError`
    after an auditable event (approving a card out of ``review`` is exempt).
    PR acceptance (``kanban_pr_acceptance_store``) is prepared before the txn
    and recorded inside it.
    """
    now = int(time.time())
    # Fail before validating cards or staging artifacts; re-check inside the
    # final write transaction below to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return False

    # A superseded close carries its evidence in the pointer, so it is the one
    # thing that must not be blank. Gate it before any filesystem work, and
    # emit the audit event the same way the card gates do.
    if superseded_by is not None:
        superseded_by = str(superseded_by).strip()
        too_long = _overlong(superseded_by)
        if not superseded_by or too_long:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_empty_supersede",
                    {"reason": "overlong_superseded_pointer" if too_long
                     else "empty_superseded_pointer"},
                )
            raise EmptySupersedeError(task_id, too_long)
    run_outcome = "superseded" if superseded_by else "completed"
    if external is not None or watcher is not None:
        external = str(external or "").strip()
        watcher = str(watcher or "").strip()
        why = ("has no --external url" if not external
               else f"url {_overlong(external)}" if _overlong(external)
               else "url is not http(s)" if not re.match(r"https?://\S+$", external)
               else "has no --watcher" if not watcher
               else f"watcher {_overlong(watcher)}" if _overlong(watcher)
               else "cannot also be superseded" if superseded_by else "")
        if why:
            with write_txn(conn):
                _append_event(conn, task_id, "completion_blocked_external", {"reason": why})
            raise ExternalCloseError(task_id, why)
        run_outcome = "external"
    if draft_ok is not None:
        draft_ok = str(draft_ok).strip()
        too_long = _overlong(draft_ok)
        if not draft_ok or too_long:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_empty_draft_override",
                    {"reason": "overlong_draft_ok" if too_long else "empty_draft_ok"},
                )
            raise EmptyDraftOverrideError(task_id, too_long)

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    # Reject stale workers before doing filesystem work or recording a hold.
    candidate = get_task(conn, task_id)
    # An external close also takes a ``triage`` card: the external-wait card
    # parked there by a block loop is exactly what the verb exists to end.
    closable = ('running', 'ready', 'blocked', 'review') + (('triage',) if external else ())
    if candidate is None or candidate.status not in closable:
        return False
    if expected_run_id is not None and candidate.current_run_id != expected_run_id:
        return False
    # A reviewer approval (the active run was claimed from ``review``) that
    # names the reviewed PR head writes the APPROVE review_coverage record
    # card-sourced land requests read as the review of record (t_7fee0f83).
    # Validated here, before any mutation; an implementer run never writes one.
    approve_head_sha: Optional[str] = None
    if (
        isinstance(metadata, dict)
        and metadata.get("head_sha") is not None
        and candidate.status == "running"
        and candidate.current_run_id is not None
        and _retry_status_for_run(conn, task_id, candidate.current_run_id) == "review"
    ):
        approve_head_sha = str(metadata["head_sha"]).strip()
        if not _REVIEW_HEAD_SHA_RE.fullmatch(approve_head_sha):
            raise ValueError(
                "head_sha must be the reviewed PR head (7-40 hex characters)"
            )
    # Branch-base guard: an implementer handoff whose branch is cut from a
    # stale/foreign base is refused here, in the worker's run, not at the
    # merge pass (t_18e781d0). Reviewer approvals are not implementer work.
    if candidate.status == 'running' and not approve_head_sha:
        _enforce_branch_base(conn, candidate, metadata, survivor_pr=survivor_pr)
    # A completion whose evidence names a still-OPEN PR is a review handoff,
    # not ``done``: done releases dependants, and an unmerged PR has no owner
    # once the card is terminal (t_1bd02e0b, 2026-09-25). A card already in
    # ``review`` is a reviewer/human approval and is left alone; so is a
    # claimed reviewer run approving with ``head_sha`` -- the PR it approved
    # is OPEN by definition and the land queue merges it from that record.
    # Negative-handoff gate (t_4209baaa): a handoff that SAYS it did not land
    # is Needs-Apollo, not done. Only an implementer's live run is gated: a
    # review/claimed-review approval is not a handoff, and a ``blocked`` or
    # ``ready`` card cannot enter review (request_review accepts
    # running/ready only; operator closes of blocked cards must still work).
    # A human/reviewer who claimed the parked card (review -> running) and now
    # approves it is not an implementer handoff either (FleetReview #1447).
    review_claimed = (
        candidate.status == "running"
        and candidate.current_run_id is not None
        and _retry_status_for_run(conn, task_id, candidate.current_run_id) == "review"
    )
    # Receipt gate (t_e21aa11c): a dispatcher-owned worker handoff
    # (``expected_run_id`` set) that closes ``done`` with prose only -- no PR,
    # survivor, attachment or structured metadata -- is refused before any
    # mutation. Operator closes are not gated. Knob ``kanban.receipt_gate``.
    if (
        expected_run_id is not None
        and candidate.status == 'running' and not review_claimed
        and not approve_head_sha and not superseded_by and not external
        and configured_receipt_gate()
    ):
        from hermes_cli import kanban_receipt as _receipt
        if _receipt.missing(
            summary=summary, result=result, metadata=metadata,
            survivor_pr=survivor_pr, survivor_ref=survivor_ref,
            survivor_none=survivor_none,
            attachments=_receipt.attachment_count(conn, task_id),
        ):
            with write_txn(conn):
                _append_event(
                    conn, task_id, _receipt.EVENT,
                    {"reason": _receipt.REASON_CODE,
                     "metadata_keys": sorted((metadata or {}).keys())
                     if isinstance(metadata, dict) else []},
                )
            raise _receipt.ReceiptRequiredError(task_id)
    if (
        expected_run_id is not None
        and candidate.status == 'running' and not review_claimed
        and not approve_head_sha and not superseded_by and not external
    ):
        _enforce_handback_head(
            conn, task_id, summary=summary, result=result, metadata=metadata,
            survivor_pr=survivor_pr,
        )
    negative_trigger: Optional[str] = None
    if (
        candidate.status == 'running' and not review_claimed
        and not approve_head_sha and not superseded_by and not external
        and configured_negative_handoff_review()
    ):
        from hermes_cli import kanban_negative_handoff as _neg
        negative_trigger = _neg.match(_neg.handoff_texts(summary, result), metadata)
    from hermes_cli import kanban_open_pr as _open_pr
    _pr_query = None
    if candidate.status != 'review' and not approve_head_sha:
        # Foreign-owner PRs (upstream / third-party repos) are mentions, not a
        # gate: the fleet cannot merge them (t_06dccfe3). Record, never route.
        foreign = _open_pr.foreign_pr_refs(
            result, summary, metadata=metadata, survivor_pr=survivor_pr,
        )
        if foreign:
            metadata = dict(metadata or {}, mentioned_foreign_prs=[
                f"{r.repo}#{r.number}" for r in foreign
            ])
        _pr_query = _open_pr.memo_query()
        still_open = _open_pr.open_pr_refs(
            result, summary, metadata=metadata, survivor_pr=survivor_pr,
            query_fn=_pr_query,
        )
        if still_open:
            # Handoff freshness gate (t_14b81673): refuse a DRAFT (raises
            # DraftPrError, nothing mutated), update a stale head, arm a green
            # non-milestone PR through fleet-merge.sh.
            from hermes_cli import kanban_pr_freshness as _fresh
            try:
                # Arm only the card's OWN handoff PR(s), never a PR the prose
                # merely mentions (FleetReview #1234 finding).
                freshness = _fresh.check(
                    still_open, task_id=task_id,
                    allow_arm=not is_milestone_card(conn, task_id),
                    armable={
                        f"{r.repo}#{r.number}" for r in _open_pr.extract_pr_refs(
                            metadata=metadata, survivor_pr=survivor_pr,
                        )
                    },
                    draft_ok=draft_ok is not None,
                )
            except _fresh.DraftPrError as draft_err:
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "completion_blocked_draft_pr",
                        {"prs": draft_err.prs},
                    )
                raise
            overridden = freshness.get("draft_override") or []
            if overridden:
                # Audited, never silent (t_f38605be): the intentionally-open
                # draft is a mention, not a route to review that would wait
                # on a merge nobody intends.
                override = {"prs": list(overridden), "reason": draft_ok}
                with write_txn(conn):
                    _append_event(conn, task_id, "completion_draft_override", override)
                metadata = dict(metadata or {}, draft_override=override)
                still_open = [
                    r for r in still_open if f"{r.repo}#{r.number}" not in overridden
                ]
        if still_open:
            note = _open_pr.route_note(still_open)
            routed_meta = dict(metadata or {}, auto_routed_open_prs=[
                f"{r.repo}#{r.number}" for r in still_open
            ])
            # The card's OWN PRs (metadata + --survivor-pr), persisted so a later approval/archive is
            # gated on them; prose mentions in auto_routed_open_prs are not (FleetReview #1352).
            own = _open_pr.split_fleet(_open_pr.extract_pr_refs(
                metadata=metadata, survivor_pr=survivor_pr))[0]
            # Always written (even []): its presence marks the run as post-#1352 (no legacy fallback).
            routed_meta["own_prs"] = [f"{r.repo}#{r.number}" for r in own]
            if freshness.get("prs"):
                routed_meta["handoff_freshness"] = freshness
            from hermes_cli import kanban_negative_handoff as _neg
            open_pr_reviewer = None
            if negative_trigger:
                # The handoff also says it did not land: escalate to Apollo
                # with the marker instead of the ordinary reviewer.
                note = "\n".join([_neg.route_note(negative_trigger), note])
                routed_meta["negative_handoff"] = negative_trigger
                open_pr_reviewer = _neg.REVIEWER
            routed_summary = _neg.routed_summary(note, summary, result)
            routed_meta, staged = _stage_routed_scratch_artifacts(
                conn, task_id, routed_meta, summary=summary, result=result,
            )
            ok, route_reason = request_review(
                conn, task_id, summary=routed_summary, metadata=routed_meta,
                reviewer=open_pr_reviewer,
                expected_run_id=expected_run_id, force=True, with_reason=True,
            )
            _settle_routed_scratch_artifacts(conn, task_id, staged, ok)
            if not ok:
                # complete_task returns a bare bool, so callers can only say
                # "unknown id or already terminal". Leave the real refusal on
                # the card where an operator can read it (t_503df7c5).
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "completion_route_refused",
                        {"open_prs": routed_meta["auto_routed_open_prs"],
                         "reason": route_reason},
                    )
            if ok:
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "completion_routed_to_review",
                        {"open_prs": routed_meta["auto_routed_open_prs"], "note": note},
                    )
                    if negative_trigger:
                        _append_event(
                            conn, task_id, "completion_routed_negative_handoff",
                            {"trigger": negative_trigger, "reason": None,
                             "open_prs": routed_meta["auto_routed_open_prs"]},
                        )
                # One durable line on the card (t_36d0114e): why it is not done,
                # and the PR refs the review-card closer resolves on merged=true.
                add_comment(conn, task_id, "kanban", _open_pr.route_comment(still_open))
            return bool(ok)
    # Negative handoff with no open PR (t_d0aee724, t_b6eb2944): route to
    # human:apollo. Default-off: kanban.negative_handoff_review.
    if negative_trigger:
        from hermes_cli import kanban_negative_handoff as _neg
        trigger = negative_trigger
        note = _neg.route_note(trigger)
        routed_meta, staged = _stage_routed_scratch_artifacts(
            conn, task_id, dict(metadata or {}, negative_handoff=trigger),
            summary=summary, result=result,
        )
        ok, route_reason = request_review(
            conn, task_id,
            summary=_neg.routed_summary(note, summary, result),
            metadata=routed_meta,
            reviewer=_neg.REVIEWER, expected_run_id=expected_run_id,
            force=True, with_reason=True,
        )
        _settle_routed_scratch_artifacts(conn, task_id, staged, ok)
        with write_txn(conn):
            _append_event(
                conn, task_id,
                "completion_routed_negative_handoff" if ok
                else "completion_route_refused",
                {"trigger": trigger, "reason": route_reason},
            )
        if ok:
            add_comment(conn, task_id, "kanban", note)
        return bool(ok)
    # Closed-unmerged done gate (t_a1550189): the card's own PR was closed
    # without merge (e.g. auto-closed when its stacked base was deleted), so
    # the work is not on default. Refuse done unless the handoff carries a
    # SUPERSEDED-BY / RE-CARRIED-AS token naming merged work for that PR; an
    # unreadable lookup refuses too. Every completion is gated, including a
    # reviewer approving a card parked in review. Raises before any mutation.
    try:
        _open_pr.enforce_not_closed_unmerged(
            task_id, result, summary, metadata=metadata,
            survivor_pr=survivor_pr, superseded_by=superseded_by,
            query_fn=_pr_query or _open_pr.memo_query(),
            recorded=_card_recorded_pr_refs(conn, task_id),
        )
    except _open_pr.ClosedUnmergedPrError as closed_err:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_closed_unmerged_pr",
                {"prs": closed_err.closed, "unverified": closed_err.unverified},
            )
        raise
    routed_abandon = _enforce_routed_pr_survivor(
        conn, task_id, survivor_pr=survivor_pr, survivor_ref=survivor_ref,
        abandon_routed_pr=abandon_routed_pr, query_fn=_pr_query or _open_pr.memo_query(),
    )
    from hermes_cli.kanban_survivor import preserve
    survivor = preserve(
        conn, task_id, metadata,
        survivor_ref=survivor_ref, survivor_pr=survivor_pr,
        survivor_unbound=survivor_unbound,
        survivor_none=survivor_none, survivor_reason=survivor_reason,
        evidence=[t for t in (summary, result) if t],
    )
    if survivor:
        metadata = dict(metadata or {}, survivor=survivor)
        if survivor['kind'] == 'patch':
            survivor_note = (
                f"survivor=patch {survivor['path']} {survivor['sha256']} {survivor['bytes']} "
                f"{survivor.get('notice') or 'NOT PUSHED'}"
            )
            if survivor.get("claims"):
                survivor_note += " claims=" + " ".join(
                    f"{ref.get('pr') or ref['remote']}@{ref['sha']}" for ref in survivor["claims"]
                )
        elif survivor['kind'] == 'bundle':
            survivor_note = f"survivor=bundle {survivor['sidecar']} NOT PUSHED"
        elif survivor['kind'] == 'landed':
            survivor_note = "survivor=landed " + " ".join(
                f"{entry['repository']}@{entry['sha']} ({entry['matched_by']})"
                for entry in survivor["landed"]
            )
        elif survivor['kind'] == 'none':
            survivor_note = f"survivor=none follow-up={survivor['follow_up_card']}"
        elif survivor['kind'] == 'artifact':
            survivor_note = "survivor=artifact " + " ".join(
                f"{a['path']} {a['sha256']}" for a in survivor["artifacts"])
        else:
            survivor_note = "survivor=ref " + " ".join(
                f"{ref.get('repository_path') or ref['remote']}/{ref['branch']}@{ref['sha']}"
                + (" (live tree)" if ref.get("matched_by") == "canonical" else "")
                for ref in survivor["refs"]
            )
        result = '\n'.join(filter(None, [result, survivor_note]))
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    if candidate.status == "review" or review_claimed:
        metadata = _carry_routed_artifacts(conn, task_id, metadata)
    if superseded_by:
        metadata = dict(metadata or {}, superseded_by=superseded_by)
        if not (summary or "").strip() and not (result or "").strip():
            # The pointer is the evidence; give the board a readable line too.
            summary = f"Premise already satisfied; superseded by {superseded_by}."
    if external:
        metadata = dict(metadata or {}, external={"url": external, "watcher": watcher})
        if not (summary or "").strip() and not (result or "").strip():
            summary = f"External: {external} (watcher {watcher} reopens on upstream close)."
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return False
    with write_txn(conn):
        # Parent completion is a hard invariant even for direct human review
        # approval. A parent may have been reopened after this task entered
        # ``review`` or ``running``.
        if not _parents_satisfied(conn, task_id):
            return False
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return False
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        if expected_run_id is None and not force and trow and _claim_is_live(trow):
            raise LiveClaimError(task_id)
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND (status IN ('running', 'ready', 'blocked', 'review')
                        OR (? AND status = 'triage'))
                """,
                (result, now, task_id, bool(external)),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        if routed_abandon:
            _append_event(conn, task_id, "routed_pr_abandoned", routed_abandon)
        if approve_head_sha:
            add_comment(
                conn, task_id, candidate.assignee or "reviewer",
                "review_coverage: " + json.dumps(
                    {"verdict": "approve", "head_sha": approve_head_sha}
                ),
                run_id=int(candidate.current_run_id),
            )
        if isinstance(metadata, dict):
            _stage_completion_artifacts(conn, task_id, metadata, now)
        run_id = _end_run(
            conn, task_id,
            outcome=run_outcome, status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (
            summary or metadata or result or prior_status == "review"
        ):
            synth_summary = summary if summary is not None else result
            synth_metadata = metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = "Review approved without additional evidence."
                synth_metadata = {
                    "source_status": "review",
                    "approval": "manual",
                }
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome=run_outcome,
                summary=synth_summary,
                metadata=synth_metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        event_summary = summary if summary is not None else result
        if prior_status == "review" and not event_summary:
            event_summary = "Review approved without additional evidence."
        _ev_lines = (event_summary or "").strip().splitlines()
        ev_summary = _ev_lines[0][:400] if _ev_lines else ""
        completed_payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if survivor:
            completed_payload["survivor"] = survivor
        if superseded_by:
            # Read by the gateway notifier to say "premise superseded by X"
            # instead of the generic done ping.
            completed_payload["superseded_by"] = superseded_by
        if external:
            completed_payload["external"] = {"url": external, "watcher": watcher}
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_kanban_lifecycle_hook(
            "kanban_task_completed",
            task_id,
            board=get_current_board(),
            assignee=_done_task.assignee if _done_task else None,
            run_id=run_id,
            summary=(summary if summary is not None else result),
        )
    return True
_REVIEW_APPROVED_NOTE = "Review approved without additional evidence."


def _gate_created_cards(
    conn: sqlite3.Connection, task_id: str, created_cards: Optional[Iterable[str]], preview_text: Optional[str],
) -> list[str]:
    """Verify ``created_cards`` BEFORE the main write txn; returns the verified
    ids. A phantom id is recorded in its own tiny txn (auditable) then raised
    as :class:`HallucinatedCardsError` without touching task state."""
    if not created_cards:
        return []
    verified_cards, phantom_cards = _verify_created_cards(conn, task_id, created_cards)
    if phantom_cards:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_hallucination",
                {
                    "phantom_cards": phantom_cards,
                    "verified_cards": verified_cards,
                    "summary_preview": _first_line(preview_text, 200) or None,
                },
            )
        raise HallucinatedCardsError(phantom_cards, task_id)
    return verified_cards


def _substantive_text(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip())


def _gate_empty_completion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
) -> None:
    """Refuse a completion that would leave the card with no evidence.

    Review approvals are exempt: a human vouches for the card and
    ``_REVIEW_APPROVED_NOTE`` is the documented record.
    """
    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] == "review":
        return
    stored = row["result"]
    if _substantive_text(result) or _substantive_text(summary) or _substantive_text(stored):
        return
    with write_txn(conn):
        _append_event(
            conn, task_id, "completion_blocked_empty_result",
            {
                "result_preview": _first_line(result, 200) or None,
                "summary_preview": _first_line(summary, 200) or None,
            },
        )
    raise EmptyCompletionError(task_id)


def _stage_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict, now: int, *,
    uploaded_by: str = "kanban_complete",
) -> list[Path]:
    """Copy scratch artifacts to the attachments dir and record each as an
    attachment row; returns the copies so the caller can discard them if its
    transaction rolls back."""
    _persist_scratch_completion_artifacts(conn, task_id, metadata)
    staged = [Path(stored_path) for stored_path in metadata.pop("_staged_artifacts", [])]
    for path in staged:
        _insert_completion_attachment(
            conn, task_id, filename=path.name, stored_path=str(path),
            size=path.stat().st_size, created_at=now, uploaded_by=uploaded_by,
        )
    return staged


def _cleaned_artifact_paths(metadata: Any) -> list[str]:
    """Non-blank string paths declared in ``metadata["artifacts"]``."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()]


def _completed_event_payload(
    result: Optional[str], event_summary: Optional[str], verified_cards: list[str], metadata: Any,
) -> dict:
    """``completed`` event payload: first summary line (400 chars) so gateway
    notifiers / dashboard WS render without a second round-trip; verified
    cards; and ``metadata["artifacts"]`` promoted so the notifier can upload
    them as native attachments without fetching the run row."""
    # Mirror CLI's _show_voice_status: include STT/TTS provider availability so the user can tell at a
    # glance *why* voice mode isn't working ("STT provider: MISSING ..." is the common case). ``record_key``
    # mirrors the configured ``voice.record_key`` so the TUI can both bind it (frontend
    # ``isVoiceToggleKey``) and display it in /voice status — previously the TUI hardcoded Ctrl+B and
    # ignored the config (#18994).
    payload: dict = {
        "result_len": len(result) if result else 0,
        "summary": _first_line(event_summary, 400) or None,
    }
    if verified_cards:
        payload["verified_cards"] = verified_cards
    if isinstance(metadata, dict):
        cleaned = _cleaned_artifact_paths(metadata)
        if cleaned:
            payload["artifacts"] = cleaned
    return payload


def _flag_phantom_prose_refs(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
    summary: Optional[str], result: Optional[str], verified_cards: list[str],
) -> None:
    """Advisory post-commit scan of summary+result for unresolvable ``t_<hex>``
    references; emits ``suspected_hallucinated_references`` in its own txn so
    the completion is already durable. Never blocks."""
    scan_text = " ".join(filter(None, [summary, result]))
    if not scan_text:
        return
    phantom_refs = [p for p in _scan_prose_for_phantom_ids(conn, scan_text) if p not in set(verified_cards)]
    if phantom_refs:
        with write_txn(conn):
            _append_event(
                conn, task_id, "suspected_hallucinated_references",
                {"phantom_refs": phantom_refs, "source": "completion_summary"}, run_id=run_id,
            )


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------
def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers named deliverables only by absolute path in prose; add
    those that exist under the scratch workspace to ``metadata["artifacts"]``
    before cleanup can erase them."""
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    copied_paths: list[str] = []
    changed = False

    def _discard_copies() -> None:
        _discard_staged_copies(used_destinations, attachment_dir)

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        # Spelling-blind: an artifact inside the workspace but spelled in
        # another case/firmlink form must still be copied out before the
        # workspace is removed (card t_ee808d83 class sweep).
        if not _same_tree(resolved_src, workspace_root):
            persisted.append(artifact)
            continue

        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise ArtifactPreservationError(problem)

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_capped(resolved_src, dest, artifact)
        except Exception as exc:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        copied_paths.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        # Only copies made HERE need an attachment row: an already-stored
        # path in the list (a routed copy carried onto an approval) has one.
        metadata["_staged_artifacts"] = copied_paths


def _discard_staged_copies(copies: Iterable[Path], attachment_dir: Path) -> None:
    """Remove staged attachment copies whose DB rows never committed; a leaked
    copy would make the retry stage ``name_1.ext`` next to an orphan."""
    for copied in copies:
        with contextlib.suppress(OSError):
            Path(copied).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        attachment_dir.rmdir()


def _copy_capped(src: Path, dest: Path, artifact: str) -> None:
    """Chunked copy that aborts if the file grows past the attachment cap mid-copy."""
    with src.open("rb") as source_file, dest.open("xb") as destination_file:
        copied = 0
        while chunk := source_file.read(1024 * 1024):
            copied += len(chunk)
            if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                raise ArtifactPreservationError(
                    f"declared scratch artifact grew beyond the size limit: {artifact}"
                )
            destination_file.write(chunk)


def _stage_routed_scratch_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: dict,
    *,
    summary: Optional[str],
    result: Optional[str],
) -> tuple[dict, list[tuple[str, int]]]:
    """Copy declared scratch artifacts out before a completion is routed to review.

    A review route returns before the ``done`` path's artifact persistence. A
    later approval that does not repeat the implementer's metadata has no
    artifact list, and its cleanup deletes the scratch workspace with the
    files in it (FleetReview #1447, t_daa1f3bf). The routed run records the
    copies under ``routed_artifacts`` so :func:`_carry_routed_artifacts` can
    put them on the approving ``completed`` event;
    :func:`_settle_routed_scratch_artifacts` registers them once the route
    lands. Sizes are measured here, while the copy is known to exist.
    """
    # Same order as the done path: promote prose-named scratch files first.
    staged_meta = dict(_merge_completion_prose_artifacts(
        conn, task_id, dict(metadata), summary=summary, result=result,
    ) or {})
    _persist_scratch_completion_artifacts(conn, task_id, staged_meta)
    staged = [
        (str(p), Path(p).stat().st_size)
        for p in staged_meta.pop("_staged_artifacts", [])
    ]
    artifacts = staged_meta.get("artifacts")
    if isinstance(artifacts, (list, tuple)) and artifacts:
        staged_meta["routed_artifacts"] = [str(a) for a in artifacts]
    return staged_meta, staged


def _settle_routed_scratch_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    staged: list[tuple[str, int]],
    ok: bool,
) -> None:
    """Register staged copies when the review route landed; drop them if refused.

    The route is already committed when this runs, so a copy that vanished in
    between is recorded as an event rather than raised (which would report a
    failed completion for a card that is in fact in review).
    """
    if not staged:
        return
    if not ok:
        for stored_path, _size in staged:
            try:
                Path(stored_path).unlink(missing_ok=True)
            except OSError:
                pass
        return
    now = int(time.time())
    with write_txn(conn):
        for stored_path, size in staged:
            path = Path(stored_path)
            if not path.is_file():
                _append_event(
                    conn, task_id, "routed_artifact_missing",
                    {"stored_path": stored_path},
                )
                continue
            _insert_completion_attachment(
                conn, task_id, filename=path.name, stored_path=str(path),
                size=size, created_at=now,
            )


def _carry_routed_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
) -> Optional[dict]:
    """The approving completion carries the routed run's copies.

    The gateway uploads files from the ``completed`` event only, so a bare
    approval of a routed card would otherwise deliver nothing. An approval
    that declares artifacts of its own gets the routed copies FIRST, then its
    own (deduped) -- replacing one list with the other dropped the
    implementer's files (FleetReview #1447 @aa9e59a3).
    """
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? "
        "AND outcome = 'review_requested' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        routed = json.loads(row["metadata"]) if row and row["metadata"] else {}
    except (TypeError, ValueError):
        return metadata
    carried = routed.get("routed_artifacts") if isinstance(routed, dict) else None
    if not isinstance(carried, list) or not carried:
        return metadata
    own = metadata.get("artifacts") if isinstance(metadata, dict) else None
    if isinstance(own, str):
        own = [own]
    merged: list[str] = []
    for item in [*carried, *(own if isinstance(own, (list, tuple)) else [])]:
        if str(item) not in merged:
            merged.append(str(item))
    return dict(metadata or {}, artifacts=merged)


def _insert_completion_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str, size: int,
    created_at: int, uploaded_by: str = "kanban_complete",
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, filename, stored_path, size, uploaded_by, created_at),
    )
    _append_event(conn, task_id, "attached", {"filename": filename, "size": size, "by": uploaded_by})


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    stem, suffix = Path(safe_name).stem or "artifact", Path(safe_name).suffix
    candidate = directory / safe_name
    idx = 1
    while candidate in used or candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result."""
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        assignments = []
        params = []
        for field, value in (("title", title), ("body", body), ("priority", priority)):
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if result is not None:
            assignments.append("result = ?")
            params.append(result)
            changed_fields.append("result")
        if not assignments:
            return False
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
            (*params, task_id),
        )
        if priority is not None:
            _append_event(conn, task_id, "reprioritized", {"priority": priority})
        if result is None:
            non_priority_fields = [field for field in changed_fields if field != "priority"]
            if non_priority_fields:
                _append_event(conn, task_id, "edited", {"fields": non_priority_fields})
        else:
            handoff_summary = summary if summary is not None else result
            changed_fields.append("summary")
            if metadata is not None:
                changed_fields.append("metadata")
            run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
            if run is None:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
                )
            else:
                run_id = int(run["id"])
                conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
                if metadata is not None:
                    conn.execute(
                        "UPDATE task_runs SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), run_id),
                    )
            _append_event(
                conn, task_id, "edited",
                {
                    "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                    "result_len": len(result) if result else 0,
                    "summary": _first_line(handoff_summary, 400) or None,
                },
                run_id=run_id,
            )
    notify_task_updated(conn, task_id, changed_fields, board=board)
    return True

#: A scratch workspace directory is named for the card that owns it
#: (:func:`_gen_task_id` -> ``t_`` + 8 hex chars). Used to recover the owner
#: of a directory whose row never stored an explicit ``workspace_path``.
_TASK_DIR_NAME_RE = re.compile(r"^t_[0-9a-f]{4,}$")


def _conn_is_board(conn: sqlite3.Connection, board: str) -> bool:
    """True when *conn* is already open on *board*'s database file.

    Asks the connection which file it is attached to (``PRAGMA
    database_list``) rather than trusting what the caller believes: under an
    ambient ``HERMES_KANBAN_DB`` pin a connection opened "for" one board can
    be attached to another's file. Any uncertainty answers False, which costs
    one redundant connection -- never a wrong reuse.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:
        return False
    actual = ""
    for row in rows:
        # (seq, name, file) -- `main` is the connection's primary database.
        if (row[1] if not isinstance(row, sqlite3.Row) else row["name"]) == "main":
            actual = (row[2] if not isinstance(row, sqlite3.Row) else row["file"]) or ""
            break
    if not actual:
        return False
    try:
        want = _board_db_path_ignoring_pin(_normalize_board_slug(board) or board)
        return _same_path(Path(actual).resolve(strict=False), want.resolve(strict=False))
    except Exception:
        return False

#: Per-GC-run cache of the machine-wide process-cwd snapshot. ``None`` means
#: "no scope active: scan per call"; inside :func:`process_cwd_snapshot_scope`
#: it holds a one-slot list, empty until the first probe fills it.
_CWD_SNAPSHOT_SCOPE: ContextVar[Optional[list]] = ContextVar(
    "kanban_cwd_snapshot_scope", default=None,
)
_CWD_SCAN_BUDGET_SECONDS = 30


@contextlib.contextmanager
def _bounded_cwd_probe():
    """Bound lsof, cwd identity snapshot, and a candidate stat; fail closed.

    The deadline starts with a probe, never with the GC removal loop: a slow
    rmtree must not spend the following candidate's probe budget. All lsof
    cwd ancestors are resolved in the first probe and cached for this run.
    A stat on a dead mount can block independently of lsof. SIGALRM interrupts
    it on POSIX; threads/platforms without timers refuse reclamation.
    """
    deadline = time.monotonic() + _CWD_SCAN_BUDGET_SECONDS
    alarm_signal = getattr(signal, "SIGALRM", None)
    if (alarm_signal is None or not hasattr(signal, "setitimer") or
            threading.current_thread() is not threading.main_thread()):
        raise TimeoutError("cwd probe cannot be bounded in this context")
    previous = signal.getitimer(signal.ITIMER_REAL)
    if previous[0] > 0:
        raise TimeoutError("cwd probe cannot replace an existing alarm")
    old_handler = signal.getsignal(alarm_signal)

    def expired(_signum, _frame):
        raise TimeoutError("cwd probe budget exhausted")

    signal.signal(alarm_signal, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(deadline - time.monotonic(), 0.000001))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(alarm_signal, old_handler)


def _path_identity(path: Path, memo: Optional[dict] = None) -> Optional[tuple[int, int]]:
    """Filesystem identity of an EXISTING path, without opening a descriptor.

    The kernel resolves case, Unicode and firmlink aliases. Missing paths have
    no identity; never borrow a parent mount's identity for a missing child.
    In particular, opening and closing a DB file would cancel SQLite locks.
    """
    key = str(path)
    if memo is not None and key in memo:
        return memo[key]
    try:
        stat = os.stat(os.path.realpath(os.path.expanduser(key)))
        result = (stat.st_dev, stat.st_ino)
    except (OSError, ValueError):
        result = None
    if memo is not None:
        memo[key] = result
    return result


def _existing_ancestor(path: Path) -> Path:
    """For a missing path, find the nearest ancestor with a filesystem ID."""
    current = path
    while _path_identity(current) is None and current.parent != current:
        current = current.parent
    return current


def _same_tree(child: Path, parent: Path, memo: Optional[dict] = None) -> bool:
    """Walk the existing child's ancestors; compare filesystem identities."""
    target = _path_identity(parent, memo)
    if target is None or _path_identity(child, memo) is None:
        return False
    current = Path(os.path.realpath(os.path.expanduser(str(child))))
    while True:
        if _path_identity(current, memo) == target:
            return True
        if current == current.parent:
            return False
        current = current.parent


def _same_path(a: Path, b: Path, memo: Optional[dict] = None) -> bool:
    """True only for two existing filesystem objects with identical identity."""
    first, second = _path_identity(a, memo), _path_identity(b, memo)
    return first is not None and first == second


def _pin_missing_parts(path: Path) -> tuple[Optional[tuple[int, int]], tuple[str, ...]]:
    """Pin-only identity: nearest existing ancestor ID and exact missing tail."""
    ancestor = _existing_ancestor(path)
    return _path_identity(ancestor), path.parts[len(ancestor.parts):]


def _pin_tree_agrees(child: Path, parent: Path) -> bool:
    """Pin-only containment: filesystem identity plus exact uncreated tail.

    Never use this for scratch admission or ownership. Missing paths have no
    identity there; only the pin resolver needs to agree before mkdir.
    """
    child_base, child_tail = _pin_missing_parts(child)
    parent_base, parent_tail = _pin_missing_parts(parent)
    if child_base is None or parent_base is None:
        return False
    if not parent_tail:
        return _same_tree(_existing_ancestor(child), parent)
    return child_base == parent_base and child_tail[:len(parent_tail)] == parent_tail


def _pin_file_agrees(a: Path, b: Path) -> bool:
    """Compare an uncreated DB pin by existing ancestor and exact missing tail.

    This is a pin agreement check, never scratch admission or ownership: an
    uncreated DB file has no filesystem identity until the first connection.
    """
    identity_a, tail_a = _pin_missing_parts(a)
    identity_b, tail_b = _pin_missing_parts(b)
    return identity_a is not None and identity_a == identity_b and tail_a == tail_b


def _spelling_fold(path: Path) -> str:
    """Lexical spelling fold: NFC, casefold, and the macOS Data firmlink.

    Only ever used to ADD a match that forces stricter validation (or a
    refusal); never as proof that two paths are the same object.
    """
    import unicodedata

    name = unicodedata.normalize("NFC", str(path)).casefold()
    prefix = "/system/volumes/data"
    if name.startswith(prefix + "/"):
        name = name[len(prefix):]
    return name.rstrip("/") or "/"


def _unknown_owner_may_claim(candidate: Path, stored: Path) -> bool:
    """A missing stored path may only add a refusal, never admit deletion."""
    fold = _spelling_fold
    a, b = fold(candidate), fold(stored)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _scan_process_cwds() -> Optional[frozenset]:
    """Return every process cwd on the machine as lsof names it, or None.

    One ``lsof -d cwd -Fn`` lists only the cwd descriptor of each process, so
    its cost scales with the process count, not with the size of any tree.
    The previous per-candidate ``lsof +D <workspace>`` walked the whole
    workspace and took 42-55 s on 62k-205k-entry workspaces, over its 30 s
    timeout (card t_ee808d83). A machine-wide scan always contains at least
    this process's own cwd, so a non-zero exit or an empty listing is a
    failed scan, never "nothing found"; callers must fail closed on None.

    Names are kept exactly as lsof prints them. The first candidate's probe
    resolves every cwd ancestor under a single 30s deadline and caches the
    result; later candidates get fresh bounded identity checks.
    """
    try:
        result = subprocess.run(
            ["lsof", "-d", "cwd", "-Fn"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    raw = {line[1:] for line in result.stdout.splitlines()
           if line.startswith("n") and len(line) > 1}
    if not raw:
        return None
    return frozenset(Path(name) for name in raw)


@contextlib.contextmanager
def process_cwd_snapshot_scope():
    """Reuse ONE process-cwd scan for every liveness probe in this block.

    ``kanban gc`` wraps its removal loop in this so a run with N candidates
    costs one ``lsof`` instead of N. A failed scan is cached too, so every
    candidate in the run fails closed consistently. The snapshot is only as
    fresh as the run: a process that enters a candidate after the scan is not
    seen, which is the same window the per-path probe had between its lsof
    and the rmtree.
    """
    token = _CWD_SNAPSHOT_SCOPE.set([])
    try:
        yield
    finally:
        _CWD_SNAPSHOT_SCOPE.reset(token)


def _process_cwds() -> Optional[tuple[frozenset, tuple]]:
    """Cache cwd ancestor identities as well as lsof output under one deadline."""
    slot = _CWD_SNAPSHOT_SCOPE.get()
    if slot is not None and slot:
        return slot[0]
    try:
        with _bounded_cwd_probe():
            cwds = _scan_process_cwds()
            snapshot = None
            if cwds is not None:
                known, unknown = set(), []
                for cwd in cwds:
                    if _path_identity(cwd) is None:
                        unknown.append(cwd)
                        continue
                    current = Path(os.path.realpath(os.path.expanduser(str(cwd))))
                    while True:
                        identity = _path_identity(current)
                        if identity is None:
                            unknown.append(cwd)
                            break
                        known.add(identity)
                        if current == current.parent:
                            break
                        current = current.parent
                snapshot = (frozenset(known), tuple(unknown))
    except (OSError, RuntimeError, ValueError):
        snapshot = None
    if slot is not None:
        slot.append(snapshot)
    return snapshot


def _process_cwd_within(path: Path) -> bool:
    """Fail closed when a process has its cwd in *path* (including children).

    A live dir:home card normally does not own every managed scratch workspace,
    but its worker may have entered one. Check actual process cwd before
    exempting that broad enclosing path from ownership. A failed or timed-out
    cwd scan answers True (preserve the path).
    """
    snapshot = _process_cwds()
    if snapshot is None:
        return True
    try:
        # Candidate identity is separately bounded; it does not consume or
        # consult the cached cwd scan's deadline after a slow deletion.
        with _bounded_cwd_probe():
            identity = _path_identity(path)
            if identity is None:
                return True
            known, unknown = snapshot
            if identity in known:
                return True
            for cwd in unknown:
                if _unknown_owner_may_claim(path, cwd):
                    return True
            return False
    except (OSError, RuntimeError, ValueError):
        return True


def _live_owners_of_path(
    path: Path,
    *,
    conn: Optional[sqlite3.Connection] = None,
    live_only: bool = True,
) -> list:
    """Return the ids of cards that OWN *path* (live ones, by default).

    Liveness is a property of the DIRECTORY BEING DELETED, not of the
    ``task_id`` the caller happened to pass. :func:`_task_has_live_run`
    answers only "is the card the caller named live?", so a row whose
    ``workspace_path`` points at *another* card's live workspace deletes it
    while the audit names the wrong card (card t_63fb42f9, review round 5:
    ``hermes kanban gc`` removed a ``running`` card's dir and its unretained
    work, logging a clean ATTEMPT/DELETE pair against the archived caller).

    Ownership is resolved two ways, because both exist in the schema:

    * an explicit ``workspace_path`` row pointing at this directory, and
    * the ``<workspaces_root>/t_<hex>`` naming convention, for rows that
      never stored a path.

    All ownership rows are inspected before checking liveness. Unknown or
    ambiguous ownership and unreadable/unresolvable state fail closed. Path
    overlap counts as ownership: deleting an ancestor or a nested checkout
    can destroy a live card's work just as deleting its exact root can.

    ``live_only=False`` returns every owner regardless of run state; the
    executing lanes use it to name the OWNING card in the audit line, so a
    post-incident log points at the directory's owner rather than at whoever
    asked for the deletion.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        return ["<unresolvable-path>"] if live_only else []
    try:
        is_managed, board = _managed_scratch_path_info(resolved)
    except Exception:
        return ["<unresolvable-owner>"] if live_only else []

    managed_root: Optional[Path] = None
    if is_managed:
        for parent in (resolved, *resolved.parents):
            if not _is_managed_scratch_path(parent):
                managed_root = parent
                break
    # Stored rows and the candidate come from different sources and may spell
    # the same directory differently (case, NFC/NFD, firmlink); compare them
    # through _same_tree, sharing kernel lookups across rows.
    spelling_memo: dict = {}

    found: set = set()
    all_owners: set = set()
    cwd_in_path: Optional[bool] = None
    with contextlib.ExitStack() as stack:
        conns = []
        if conn is not None:
            conns.append(conn)
        try:
            if is_managed and board:
                # The directory's own board, which may not be the caller's.
                # Reuse the caller's connection when it is ALREADY that board:
                # opening a second connection to the same database is pure
                # cost on the completion path, which calls this while holding
                # the completing task's connection (FleetReview on PR #785 --
                # measured one redundant connection per call).
                if not (conn is not None and _conn_is_board(conn, board)):
                    conns.append(stack.enter_context(connect_closing(board=board)))
            elif conn is None:
                conns.append(stack.enter_context(connect_closing()))
        except Exception:
            return ["<unreadable-board-db>"] if live_only else []
        sql = "SELECT id, workspace_path FROM tasks"
        for c in conns:
            try:
                rows = c.execute(sql).fetchall()
            except Exception:
                return ["<unreadable-task-table>"] if live_only else []
            ids = set()
            for row in rows:
                # Convention ownership is a filesystem question, not a
                # comparison of the candidate's spelling with a task id.
                if (is_managed and managed_root is not None
                        and _TASK_DIR_NAME_RE.fullmatch(row["id"])
                        and _same_tree(resolved, managed_root / row["id"], spelling_memo)):
                    ids.add(row["id"])
                if not row["workspace_path"]:
                    continue
                try:
                    stored = Path(
                        str(row["workspace_path"])
                    ).expanduser().resolve(strict=False)
                except Exception:
                    return ["<unresolvable-owner-path>"] if live_only else []
                if _path_identity(stored, spelling_memo) is None:
                    if (live_only and _task_has_live_run(c, row["id"])
                            and _unknown_owner_may_claim(resolved, stored)):
                        # A row may name the candidate despite an unstatable
                        # path. Unknown identity can only prevent removal.
                        return ["<unknown-owner-path>"]
                    continue
                stored_in = _same_tree(stored, resolved, spelling_memo)
                path_in = _same_tree(resolved, stored, spelling_memo)
                if not (stored_in or path_in):
                    continue
                if (is_managed and path_in and not stored_in
                        and managed_root is not None
                        and _same_tree(managed_root, stored, spelling_memo)):
                    # A broad dir:home row (at or above the workspaces root)
                    # owns a scratch workspace only when it is live AND a
                    # process actually has its cwd there. Unrelated
                    # home-rooted cards must not pin all workspaces.
                    if not (live_only and _task_has_live_run(c, row["id"])):
                        continue
                    if cwd_in_path is None:
                        cwd_in_path = _process_cwd_within(resolved)
                    if not cwd_in_path:
                        continue
                ids.add(row["id"])
            all_owners.update(ids)
            for tid in ids:
                if not live_only or _task_has_live_run(c, tid):
                    found.add(tid)
    if live_only and not found:
        if not all_owners:
            return ["<unknown-owner>"]
        if len(all_owners) != 1:
            return ["<ambiguous-owner>", *sorted(all_owners)]
    return sorted(found)

#: Basename of the append-only workspace-deletion audit log. Named here
#: rather than inlined because the log reapers have to recognise it: it
#: shares a directory with the disposable per-task worker logs, and a
#: routine log GC must never be able to delete it.
WORKSPACE_DELETION_LOG_NAME = "workspace-deletions.log"


def workspace_deletion_log_path(board: Optional[str] = None) -> Path:
    """Return the append-only audit log for workspace deletions.

    Lives beside the worker logs (``<root>/kanban/logs/`` for the default
    board, ``<root>/kanban/boards/<slug>/logs/`` otherwise) so a board's
    deletion history is scoped the same way everything else about that
    board is.
    """
    return worker_logs_dir(board=board) / WORKSPACE_DELETION_LOG_NAME


def is_deletion_audit_path(path: Path) -> bool:
    """True when *path* is the deletion audit log, or a rotation of it.

    The audit lives in the same directory as the per-task worker logs, so
    every consumer that reaps or rotates files in that directory would
    otherwise treat it as disposable (card t_63fb42f9, review round 7:
    ``hermes kanban gc`` reported ``1 log file(s) removed`` and that file
    was the audit — the lane that performs deletions destroying the record
    of them). The invariant this predicate exists to enforce is: **an
    append-only deletion audit is never removed by a routine GC or
    rotation.**

    Matching is on the basename so it holds regardless of which board's
    ``logs/`` directory, or which of ``_durable_audit_log_path``'s outward
    fallbacks, the line actually landed in — those fallbacks are suffixed
    forms (``kanban-workspace-deletions.log``,
    ``hermes-workspace-deletions.log``) and are covered too. Rotated
    generations (``workspace-deletions.log.1``) count as well, since
    rotation is exactly how a long audit would be split.
    """
    name = path.name
    if name.endswith(WORKSPACE_DELETION_LOG_NAME):
        return True
    # A rotated generation: "<name>.<n>".
    stem, _, suffix = name.rpartition(".")
    return stem.endswith(WORKSPACE_DELETION_LOG_NAME) and suffix.isdigit()

#: Audit outcomes. ``ATTEMPT`` is written *before* an irreversible removal
#: so a process that dies mid-rmtree still names itself; ``DELETE`` is only
#: ever written after the removal actually returned success. ``REFUSED`` is a
#: gate saying no, ``FAILED`` is the executor saying no.
AUDIT_ATTEMPT = "ATTEMPT"
AUDIT_DELETE = "DELETE"
AUDIT_REFUSED = "REFUSED"
AUDIT_FAILED = "FAILED"

#: ``ARCHIVE`` is the relocation counterpart of ``DELETE``: the tree is gone
#: from its old path (a live process holding it is just as broken) but the
#: bytes survive at the recorded destination. Distinct from ``DELETE`` so an
#: operator reading the log knows whether recovery is possible.
AUDIT_ARCHIVE = "ARCHIVE"


def _durable_audit_log_path(target: Path, board: Optional[str]) -> Path:
    """Pick an audit log that will still exist after *target* is removed.

    A board hard-delete removes ``<root>/kanban/boards/<slug>/`` -- which
    CONTAINS that board's own ``logs/``. Routing the audit there means a
    successful deletion destroys its own record (card t_63fb42f9, review
    round 4: ``action='deleted'``, remaining audit files ``[]``). This is
    not a board-only hazard: any deletion whose target encloses the log
    directory has it, so the containment check lives here, in front of
    every call site, rather than at the one that happened to be reported.

    Candidates are tried outermost-last; the first one not inside *target*
    wins.
    """
    candidates: list[Path] = []
    try:
        candidates.append(workspace_deletion_log_path(board=board))
    except Exception:
        pass
    try:
        # The default board's log lives at <root>/kanban/logs/, which is a
        # sibling of boards/ and of workspaces/ -- outside every per-board
        # and per-card deletion target.
        candidates.append(workspace_deletion_log_path(board=DEFAULT_BOARD))
    except Exception:
        pass
    try:
        # Last resort for the incident's own shape: the whole kanban home
        # as the target. Nothing under it is durable, so step outside.
        candidates.append(kanban_home() / "kanban-workspace-deletions.log")
    except Exception:
        pass
    try:
        import tempfile

        # And if even the home is the target, leave the tree entirely. An
        # audit line outside the blast radius beats no audit line at all --
        # the whole point of deliverable 2b.
        candidates.append(
            Path(tempfile.gettempdir()) / "hermes-workspace-deletions.log"
        )
    except Exception:
        pass
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        try:
            # The audit FILE may not exist yet; its existing parent still
            # determines whether writing it would be inside the target.
            current = _existing_ancestor(resolved)
            if (_same_tree(current, target)
                    or (_path_identity(target) is None
                        and _unknown_owner_may_claim(resolved, target))):
                continue  # would be destroyed by the deletion it records
        except (ValueError, OSError):
            pass
        return candidate
    return candidates[-1] if candidates else Path("workspace-deletions.log")


def _audit_workspace_deletion(
    path: Path,
    *,
    task_id: Optional[str],
    reason: str,
    allowed: Optional[bool] = None,
    detail: str = "",
    outcome: Optional[str] = None,
    board: Optional[str] = None,
) -> None:
    """Append one line to the workspace-deletion audit log. Best effort.

    The 2026-09-20 incident was invisible after the fact: an entire
    default-board scratch root vanished under a running worker and no log
    named a deleter. Every attempted removal -- permitted or refused --
    now leaves a record naming pid, ppid, task, path and outcome.

    ``outcome`` is one of :data:`AUDIT_ATTEMPT` / :data:`AUDIT_DELETE` /
    :data:`AUDIT_REFUSED` / :data:`AUDIT_FAILED`. ``allowed`` is the older
    boolean spelling and maps to DELETE/REFUSED. A caller that is about to
    perform an irreversible removal writes ATTEMPT first and DELETE only
    once the executor has returned success -- writing DELETE up front makes
    the log lie whenever the executor refuses (review round 4 measured
    exactly that on a survivor-held board: a DELETE line for a board that
    still exists).

    ``board`` pins the log's board explicitly. Without it the destination is
    inferred, and for a target that is not a scratch descendant (a board
    root) the inference falls through to the ambient active board.
    """
    try:
        if outcome is None:
            outcome = AUDIT_DELETE if allowed else AUDIT_REFUSED
        if board is None:
            is_managed, matched_board = _managed_scratch_path_info(path)
            if is_managed:
                board = matched_board
        try:
            target = Path(path).resolve(strict=False)
        except OSError:
            target = Path(path)
        log_path = _durable_audit_log_path(target, board)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        verdict = outcome
        try:
            argv = " ".join(sys.argv)[:300]
        except Exception:
            argv = "?"
        line = (
            f"{stamp}\t{verdict}\ttask={task_id or '-'}\tpid={os.getpid()}"
            f"\tppid={os.getppid()}\treason={reason}\tpath={path}"
            f"\tdetail={detail}\targv={argv}\n"
        )
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass  # auditing must never block or break cleanup


def _task_has_live_run(
    conn: Optional[sqlite3.Connection], task_id: Optional[str]
) -> bool:
    """Return True when *task_id* looks like it is being actively worked.

    "Live" means the card is in a non-terminal working state (``running``)
    **or** it still holds an unexpired dispatch claim lock. Either is
    sufficient: a worker mid-run owns its workspace, and deleting it out
    from under the process destroys unretained work (incident 2026-09-20,
    run 1880 -- the worker recreated its dir plus a locked git worktree and
    both vanished again within seconds).

    Errors resolve to ``True`` (fail-closed): if we cannot prove the card
    is idle, we do not delete its workspace.
    """
    if not task_id:
        return False
    if conn is None:
        try:
            with connect_closing() as own:
                return _task_has_live_run(own, task_id)
        except Exception:
            return True
    try:
        row = conn.execute(
            "SELECT status, claim_expires FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except Exception:
        return True  # fail closed
    if row is None:
        return False
    if row["status"] == "running":
        return True
    claim_expires = row["claim_expires"]
    try:
        if claim_expires and int(claim_expires) > int(time.time()):
            return True
    except (TypeError, ValueError):
        return True
    return False


def safe_remove_workspace_dir(
    path: Path,
    *,
    task_id: Optional[str] = None,
    reason: str,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """THE choke point for removing a kanban-managed scratch workspace.

    Every workspace deletion in Hermes goes through here. Three gates, in
    order, each of which independently refuses:

    1. **Containment.** :func:`_is_managed_scratch_path` requires a
       *strict* descendant of a ``workspaces/`` root. The root itself, the
       kanban home, board roots and ``logs/`` are all refused -- deleting
       the root wipes every card's scratch dir at once, which is exactly
       what happened on 2026-09-20.
    2. **Liveness.** A card that is ``running`` or holds an unexpired
       claim lock owns its directory; refuse regardless of what the caller
       believes. Fail-closed on any DB error.
    3. **Audit.** Both outcomes are appended to the per-board deletion log
       so the next incident names its deleter.

    Returns True iff the directory was removed.
    """
    try:
        resolved = Path(path).resolve(strict=False)
    except OSError:
        _audit_workspace_deletion(
            Path(path), task_id=task_id, reason=reason, allowed=False,
            detail="unresolvable-path",
        )
        return False

    if not resolved.is_dir():
        return False
    if not _is_managed_scratch_path(resolved):
        _audit_workspace_deletion(
            resolved, task_id=task_id, reason=reason, allowed=False,
            detail="not-a-managed-scratch-descendant",
        )
        _log.warning(
            "Refusing to remove workspace %s (task %s, reason %s): not a "
            "strict descendant of a kanban-managed workspaces root",
            resolved, task_id, reason,
        )
        return False

    # Nothing to remove. Checked BEFORE the liveness and owner scans, which
    # are the expensive gates: `_live_owners_of_path` opens a second
    # connection and full-scans `tasks`, resolving every row's path. In gc's
    # steady state most archived cards' workspaces were already removed at
    # completion, so leaving this check last turned `kanban gc` into O(M)
    # extra connections plus O(M*N) path resolutions for zero removals -- and
    # appended one permanent `REFUSED` line per already-clean row to an
    # append-only log that `gc_worker_logs` is deliberately forbidden to reap,
    # burying the DELETE/ATTEMPT records the audit exists to surface
    # (FleetReview on PR #785, measured: 1 owner scan + 1 audit line for a
    # path that does not exist).
    #
    # No audit line either: the log records deletions and refusals TO DELETE.
    # A path with nothing at it was never a deletion, and a per-row entry that
    # can never be reaped is exactly the noise the finding named.
    if not resolved.is_dir():
        return False

    if task_id and _has_active_children(conn, task_id):
        _audit_workspace_deletion(
            resolved, task_id=task_id, reason=reason, allowed=False,
            detail="active-children-need-handoff",
        )
        return False
    if _task_has_live_run(conn, task_id):
        _audit_workspace_deletion(
            resolved, task_id=task_id, reason=reason, allowed=False,
            detail="task-has-live-run",
        )
        _log.warning(
            "Refusing to remove workspace %s: task %s is running or holds a "
            "live claim lock (reason %s)",
            resolved, task_id, reason,
        )
        return False

    # Liveness of the *caller* is not enough: the path is supplied separately
    # from the task_id, so a row pointing at another card's live workspace
    # sails through the check above. Ask who owns THIS directory and whether
    # THAT card is live (review round 5).
    owners = _live_owners_of_path(resolved, conn=conn)
    if owners:
        _audit_workspace_deletion(
            resolved, task_id=task_id, reason=reason, allowed=False,
            detail="owner-has-live-run owner=%s" % ",".join(owners),
        )
        _log.warning(
            "Refusing to remove workspace %s (caller task %s, reason %s): it "
            "is owned by live card(s) %s",
            resolved, task_id, reason, ",".join(owners),
        )
        return False

    # The audit must name the card that OWNS this directory, not just the
    # caller -- round 5 measured a DELETE line attributing another card's
    # workspace to the archived row that asked for it.
    owner_detail = ""
    owners_any = _live_owners_of_path(resolved, conn=conn, live_only=False)
    if owners_any and owners_any != [task_id]:
        owner_detail = "owner=%s" % ",".join(owners_any)

    _audit_workspace_deletion(
        resolved, task_id=task_id, reason=reason, outcome=AUDIT_ATTEMPT,
        detail=owner_detail,
    )
    # The removal itself goes through kanban_survivor.remove_workspace_dir
    # (#783), which captures recoverable implementation work before deleting
    # and HOLDS the workspace if it cannot. This function owns the three
    # gates in FRONT of it -- containment, liveness, audit -- so the two
    # protections compose instead of competing, and there remains exactly ONE
    # directory deleter in the package (asserted by
    # test_workspace_deletion_has_one_choke_point).
    from hermes_cli.kanban_survivor import remove_workspace_dir
    from hermes_cli.worktree_ops import release_lsp_clients

    # Release any LSP clients rooted in the tree before it goes (upstream
    # kanban_db_workspace does this ahead of every rmtree).
    release_lsp_clients(str(resolved))

    # The survivor capture needs a task connection: without one it can only
    # prove durability from an independently-pushed ref, and holds the
    # workspace otherwise. Callers of this choke point may omit conn (gc
    # does), so open one here rather than degrading into a hold -- the same
    # self-connect _task_has_live_run performs.
    try:
        if conn is not None or not task_id:
            removed = bool(remove_workspace_dir(conn, task_id, resolved))
        else:
            with connect_closing() as own:
                removed = bool(remove_workspace_dir(own, task_id, resolved))
    except Exception as exc:
        # remove_workspace_dir raises sqlite3.IntegrityError for a task_id
        # with no row (its capture writes an attachment FK). This choke point
        # is called from best-effort cleanup paths and must degrade to a
        # refusal, never propagate.
        _log.warning(
            "Survivor-backed removal of %s (task %s) failed: %s",
            resolved, task_id, exc,
        )
        removed = False
    if not removed:
        _audit_workspace_deletion(
            resolved, task_id=task_id, reason=reason, outcome=AUDIT_FAILED,
            detail=("survivor-held-workspace " + owner_detail).strip(),
        )
        return False
    _audit_workspace_deletion(
        resolved, task_id=task_id, reason=reason, outcome=AUDIT_DELETE,
        detail=owner_detail,
    )
    _log.debug(
        "Removed scratch workspace %s (task %s, reason %s)",
        resolved, task_id, reason,
    )
    return True


@_home_session_guarded("block")
def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    kind: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    until: Optional[int] = None,
) -> bool:
    """Transition ``running``/``ready`` → ``blocked`` (or route elsewhere).

    ``kind`` (one of :data:`VALID_BLOCK_KINDS`, or ``None`` for a legacy
    un-typed block) drives routing instead of every block landing in one
    undifferentiated ``blocked`` bucket:

    * ``dependency`` — the task is only waiting on another task. It does NOT
      sit in ``blocked`` (where a cron would keep "unblocking" it); it goes to
      ``todo`` so the existing parent-gating / ``recompute_ready`` machinery
      promotes it automatically once its parents finish. No human, no cron, no
      retry storm. This is Dale's "Type 2 — dependency blocked". With no open
      blocking parent it is refused with :class:`BlockRefused`
      (:data:`DEPENDENCY_NO_PARENT_REFUSAL`), never silently re-kinded.

    * ``deferred`` — waiting on wall-clock time. Requires ``until`` (epoch
      seconds). Parks in ``scheduled`` with ``next_eligible_at = until``, so
      the dispatcher's :func:`wake_due_scheduled` returns it to ``ready``
      (``todo`` while parents are open) at that time. Never ``blocked``, so
      the needs-input pager and lifecycle block lines never see it. Accepted
      from ``todo``/``ready``/``running``/``blocked`` (an existing human park
      can be converted), like :func:`schedule_task`.

    * ``needs_input`` / ``capability`` / ``None`` — "truly blocked" (Dale's
      "Type 1"). Lands in ``blocked`` for a human. BUT: each time such a task
      is re-blocked for the SAME kind after having been unblocked, the
      unblock-loop counter (``block_recurrences``) increments. When it reaches
      :data:`BLOCK_RECURRENCE_LIMIT`, the task is routed to ``triage`` instead
      of ``blocked`` — breaking the cron-unblock ↔ worker-re-block loop and
      forcing a human-in-the-loop triage decision.

    * ``transient`` — treated like a generic block for routing, but a worker
      can use it to signal "this might clear on its own"; it still participates
      in the loop breaker so a forever-flaky task eventually escalates.

    Returns True on any successful transition (to ``blocked``, ``todo``, or
    ``triage``), False when the task wasn't in a blockable state.

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.

    ``review`` is a blockable state: a card whose implementation is landed but
    whose completion is refused must be parkable, or the review dispatcher
    keeps respawning reviewers on it. Such a block records
    ``source_status='review'`` so :func:`unblock_task` restores the review
    phase instead of handing the card back to an implementer.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    if until is not None and kind != "deferred":
        raise BlockRefused("until only applies to kind 'deferred'")
    if kind == "deferred" and until is None:
        raise BlockRefused("kind 'deferred' needs until (--until <ISO|+6h>): the wake time")
    recurrences = 0
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        if kind == "dependency" and _parents_satisfied(conn, task_id):
            raise BlockRefused(DEPENDENCY_NO_PARENT_REFUSAL)
        if kind == "deferred":
            # Same transition as schedule_task + set_schedule_wake in one step, so
            # the dispatcher's wake_due_scheduled owns the wake. No ``blocked``
            # event and no ``kanban_task_blocked`` hook: a time park is not a
            # human question, and every pager keys on those.
            sql = (
                "UPDATE tasks SET status = 'scheduled', claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, block_kind = 'deferred', next_eligible_at = ? "
                "WHERE id = ? AND status IN ('todo', 'ready', 'running', 'blocked')"
            )
            params: list[Any] = [int(until), task_id]
            if expected_run_id is not None:
                sql += " AND current_run_id = ?"
                params.append(int(expected_run_id))
            if conn.execute(sql, params).rowcount != 1:
                return False
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="scheduled", status="scheduled", summary=reason,
                synthesize=bool(reason),
            )
            _append_event(conn, task_id, "scheduled", {
                "reason": reason, "kind": "deferred", "until": int(until),
                "source_status": cur_row["status"],
            }, run_id=run_id)
            return True
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready/review, so that policy could never be attached
        # later (#117363). Classify in place; never re-type or flap status. A
        # caller asserting run ownership (``expected_run_id``) cannot own a
        # parked card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return False
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                return False
            _append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            return True
        source_status = (
            _retry_status_for_run(conn, task_id)
            if cur_row["status"] == "running"
            else ("review" if cur_row["status"] == "review" else "ready")
        )
        prev_kind = cur_row["block_kind"] if "block_kind" in cur_row.keys() else None
        prev_recurrences = (
            int(cur_row["block_recurrences"])
            if "block_recurrences" in cur_row.keys()
            and cur_row["block_recurrences"] is not None
            else 0
        )

        # Genuine parent-gated waits never enter the human ``blocked`` bucket — they
        # wait in ``todo`` and let ``recompute_ready`` gate on parents. Routing
        # here (rather than ``blocked``) is what keeps a cron from ever seeing
        # a dependency-wait as something to "unblock".
        # Without an open blocking parent, todo would promote on the next tick
        # forever. Fall through to the sticky block / recurrence breaker instead.
        if kind == "dependency" and not _parents_satisfied(conn, task_id):
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'todo',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       block_kind    = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'review')
                """ + ("" if expected_run_id is None else " AND current_run_id = ?"),
                (kind, task_id) if expected_run_id is None
                else (kind, task_id, int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                )
            _append_event(
                conn, task_id, "dependency_wait",
                {
                    "reason": reason,
                    "kind": kind,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
            _blocked_task = get_task(conn, task_id)
            _fire_kanban_lifecycle_hook(
                "kanban_task_blocked",
                task_id,
                board=get_current_board(),
                assignee=_blocked_task.assignee if _blocked_task else None,
                run_id=run_id,
                reason=reason,
            )
            return True

        # Truly-blocked kinds. Increment the unblock-loop counter when this is a
        # re-block for the SAME reason after a prior unblock. block_task only
        # fires from running/ready (i.e. AFTER an unblock returned the task to
        # the work pool), so a stored block_kind that matches the incoming kind
        # means: blocked → unblocked → about-to-re-block for the same cause.
        # An un-typed (None) block compares as "same" to a prior un-typed block.
        same_cause = prev_kind == kind
        recurrences = prev_recurrences + 1 if same_cause else 1

        if recurrences >= BLOCK_RECURRENCE_LIMIT:
            # Loop detected — stop letting the unblocker spin this task. Route
            # to triage for a human-in-the-loop decision instead of blocked.
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'triage',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       block_kind    = ?,
                       block_recurrences = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'review')
                """ + ("" if expected_run_id is None else " AND current_run_id = ?"),
                (kind, recurrences, task_id) if expected_run_id is None
                else (kind, recurrences, task_id, int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                )
            _append_event(
                conn, task_id, "block_loop_detected",
                {
                    "reason": reason,
                    "kind": kind,
                    "recurrences": recurrences,
                    "limit": BLOCK_RECURRENCE_LIMIT,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
            # Escalating to triage freezes every non-terminal child linked by
            # a blocking edge: those children stay in ``todo`` until a human
            # resolves this card, and nothing else on the board says so.
            # Record what this decision is costing AT THE MOMENT it happens,
            # so the stranding is auditable from the parent's own event log
            # and not just inferable from a dispatch tick nobody read.
            stranded_children = [
                r["id"] for r in conn.execute(
                    "SELECT c.id AS id FROM task_links l "
                    "JOIN tasks c ON c.id = l.child_id "
                    "WHERE l.parent_id = ? "
                    "AND COALESCE(l.kind, ?) = ? "
                    "AND c.status NOT IN ('done', 'archived') "
                    "ORDER BY c.id",
                    (task_id, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
                )
            ]
            if stranded_children:
                _append_event(
                    conn, task_id, "triage_stranded_subtree",
                    {"stranded": stranded_children, "count": len(stranded_children)},
                    run_id=run_id,
                )
                _log.warning(
                    "kanban: %s escalated to triage and is stranding %d "
                    "downstream task(s): %s — they will not spawn until it is "
                    "resolved (hermes kanban triage-resolve %s --to "
                    "todo|done|archived --reason \"...\")",
                    task_id, len(stranded_children),
                    ", ".join(stranded_children), task_id,
                )
        else:
            if expected_run_id is None:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready', 'review')
                    """,
                    (kind, recurrences, task_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready', 'review')
                       AND current_run_id = ?
                    """,
                    (kind, recurrences, task_id, int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            # Synthesize a run when blocking a never-claimed task so the
            # reason is preserved in attempt history.
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id,
                    outcome="blocked",
                    summary=reason,
                )
            _append_event(
                conn, task_id, "blocked",
                {
                    "reason": reason,
                    "kind": kind,
                    "recurrences": recurrences,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
        _blocked_task = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_blocked",
        task_id,
        board=get_current_board(),
        assignee=_blocked_task.assignee if _blocked_task else None,
        run_id=run_id,
        reason=reason,
    )
    return True


def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for :func:`block_task`.

    ``dependency`` never enters the human ``blocked`` bucket: it waits in
    ``todo`` for ``recompute_ready``, so a cron never sees a dependency-wait
    as something to "unblock". Callers that pass ``dependency`` with no
    incomplete parent are re-kinded to ``needs_input`` before this runs
    (see :func:`block_task`). Every other kind counts unblock-loop
    recurrences: block_task only fires from running/ready (AFTER an unblock
    returned the task to the pool), so a stored ``block_kind`` equal to the
    incoming one means blocked -> unblocked -> re-block for the same cause
    (un-typed None compares equal to a prior un-typed block). At
    ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
    """
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value
HUMAN_REVIEWER_SENTINEL = "human"


def is_human_reviewer(value: Optional[str]) -> bool:
    """Return True for the explicit human-review sentinel.

    Accepts the bare sentinel ``human`` and the attributed form
    ``human:<name>`` so a board can name WHICH human owns the lane while
    still being recognizable as a deliberate (non-spawnable) terminal lane.
    """
    if not isinstance(value, str):
        return False
    head = value.strip().casefold().split(":", 1)[0].strip()
    return head == HUMAN_REVIEWER_SENTINEL


def spawnable_reviewer_profiles() -> list[str]:
    """Installed NAMED profile ids that a review card may legally be assigned to.

    Excludes the implicit ``default`` entry, which :func:`list_profile_names`
    always reports whether or not any profile directory exists — so an empty
    list here means "no fleet profiles installed", not "one is installed".
    """
    try:
        from hermes_cli.profiles import list_profile_names

        return sorted(n for n in list_profile_names() if n != "default")
    except Exception:
        return []


def _board_home_kanban_cfg() -> dict:
    """The ``kanban`` section of ``kanban_home()/config.yaml`` — the board's owner.

    Presence-sensitive read: only keys actually written in the board home's
    file count (a DEFAULT_CONFIG merge would make every key "present" and
    mask the profile fallback). ``${VAR}`` expansion and the managed-scope
    overlay are applied like the gateway's presence-sensitive bridges.
    Missing file -> ``{}``; unparseable YAML raises (callers fail closed).
    """
    from hermes_cli.config import _expand_env_vars, read_user_config_raw

    cfg = _expand_env_vars(read_user_config_raw(kanban_home() / "config.yaml"))
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        from hermes_cli import managed_scope

        cfg = managed_scope.apply_managed_overlay(cfg)
    except Exception:
        pass
    section = cfg.get("kanban")
    return section if isinstance(section, dict) else {}


def _profile_config_sets_kanban_key(key: str) -> bool:
    """True when the ACTIVE profile's config.yaml literally writes ``kanban.<key>``
    (used only to label the DEBUG source; never raises)."""
    try:
        from hermes_cli.config import get_config_path, read_user_config_raw

        section = read_user_config_raw(get_config_path()).get("kanban")
        return isinstance(section, dict) and key in section
    except Exception:
        return False
_REVIEW_SETTING_MISSING = object()


def _kanban_review_setting(key: str, default: Any) -> tuple[Any, str]:
    """Resolve a board-scoped ``kanban.<key>`` review setting -> ``(value, source)``.

    The review policy is a BOARD property: the board is shared across profiles
    by design (see :func:`kanban_home`), so a worker running under
    ``HERMES_HOME=<root>/profiles/<p>`` must see the same policy as the
    dispatcher. Precedence: the board home's ``config.yaml`` when it writes the
    key (``board_home``); else the active profile's ``load_config()`` value
    (``profile``, or ``default`` when only DEFAULT_CONFIG supplies it); else
    ``default``. The winning source is logged at DEBUG. Raises when a config
    file is unreadable — each caller keeps its own fail-closed fallback.
    """
    board = _board_home_kanban_cfg()
    if key in board:
        value, source = board[key], "board_home"
    else:
        from hermes_cli.config import get_config_path, load_config, read_user_config_raw

        # load_config() swallows a YAML/read error and serves DEFAULT_CONFIG
        # (fresh process) -- whose review_policy is ``all``. Read the profile
        # file raw first so an unreadable config RAISES as documented and each
        # caller fails closed (review_policy -> none), never to ``all`` (C6, #1065).
        read_user_config_raw(get_config_path())
        profile_cfg = (load_config() or {}).get("kanban", {}) or {}
        if key in profile_cfg:
            value = profile_cfg[key]
            source = "profile" if _profile_config_sets_kanban_key(key) else "default"
        else:
            value, source = default, "default"
    _log.debug("kanban.%s=%r (source=%s)", key, value, source)
    return value, source


def configured_review_assignee() -> Optional[str]:
    """Default reviewer from ``kanban.review_assignee`` (no implementer fallback).

    Returns ``None`` when unset/blank so the caller can refuse explicitly
    rather than silently leaving the implementer as their own reviewer.
    """
    try:
        value, _source = _kanban_review_setting("review_assignee", None)
    except Exception:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
DEFAULT_MAX_REVIEW_ROUNDS = 3
MILESTONE_MARKER = "[milestone]"
QA_REQUIRED_MARKER = "qa:required"
REVIEW_POLICIES = ("all", "milestone_only", "none")


def configured_max_review_rounds() -> int:
    """``kanban.max_review_rounds`` — reviewer↔implementer round cap (0 = off).

    A "round" is one ``changes_requested`` verdict. Once a card has collected
    this many, the NEXT ``request_review`` does not re-spawn the reviewer: the
    card is BLOCKED (``needs_input``) for the orchestrator to take over. Measured
    2026-09-24 (Mac Studio): 458 reviewed cards / 7d averaged 10.8 reviewer
    SESSIONS per card (incl. rate-limited respawns — not changes_requested
    rounds), max 123, 107 cards at 13+ — the tail that produced 498 reviewer
    sessions a day.
    Default :data:`DEFAULT_MAX_REVIEW_ROUNDS`.
    """
    try:
        value, _source = _kanban_review_setting(
            "max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS
        )
        rounds = int(value)
    except Exception:
        return DEFAULT_MAX_REVIEW_ROUNDS
    return rounds if rounds >= 0 else DEFAULT_MAX_REVIEW_ROUNDS


def configured_negative_handoff_review() -> bool:
    """``kanban.negative_handoff_review`` — route negative handoffs to review (default off)."""
    try:
        value, _source = _kanban_review_setting("negative_handoff_review", False)
    except Exception:
        return False
    return value is True or str(value).strip().casefold() in ("1", "true", "yes", "on")


def configured_receipt_gate() -> bool:
    """``kanban.receipt_gate`` — refuse a receipt-less implementer completion (default on)."""
    try:
        value, _source = _kanban_review_setting("receipt_gate", True)
    except Exception:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in ("0", "false", "no", "off")


def configured_review_policy() -> str:
    """``kanban.review_policy`` — ``all`` (default), ``milestone_only`` or ``none``.

    ``none``: no card gets a reviewer session at all (Ace 2026-09-24 13:51 — Argus out of
    kanban review; CI + the orchestrator merge pass are the gate). Every handoff completes
    with ``review_skipped=policy_none``; only the ``human`` sentinel or force=True bypasses.

    ``milestone_only``: only *milestone* cards (see :func:`is_milestone_card`)
    are routed to ``kanban.review_assignee``; every other card that asks for
    review is COMPLETED instead, with a ``review_skipped`` event — CI is the
    gate for slice work, the reviewer profile is functional QA at milestones.
    Applies even when a reviewer PROFILE is named explicitly; only the
    ``human`` sentinel or ``force=True`` bypasses it.

    A MISSING key keeps the documented default ``all``. A PRESENT-but-invalid
    value (unknown, empty) or an unreadable config fails to ``none`` — never
    to ``all`` — and logs a ``review_policy_invalid`` WARNING (spec §0.1 F0:
    a typo must not silently re-enable a reviewer on every card).

    Resolved from the BOARD HOME first (see :func:`_kanban_review_setting`).
    """
    try:
        raw, _source = _kanban_review_setting("review_policy", _REVIEW_SETTING_MISSING)
        if raw is _REVIEW_SETTING_MISSING:
            return "all"
    except Exception as exc:
        _log.warning("review_policy_invalid: config unreadable (%s); using 'none'", exc)
        return "none"
    value = str(raw if raw is not None else "").strip().lower()
    if value in REVIEW_POLICIES:
        return value
    _log.warning("review_policy_invalid: %r is not one of %s; using 'none'", raw, REVIEW_POLICIES)
    return "none"


def is_milestone_card(conn: sqlite3.Connection, task_id: str) -> bool:
    """A card is a milestone when its title/body carries ``[milestone]`` or
    ``qa:required`` (any case). Nothing else: being a PARENT in ``task_links``
    does NOT qualify — under fan-in QA every slice is the parent of its QA
    card (spec §5.2), so a parent clause routed every slice to a reviewer."""
    row = conn.execute(
        "SELECT title, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    text = f"{row['title'] or ''}\n{row['body'] or ''}".lower()
    return MILESTONE_MARKER in text or QA_REQUIRED_MARKER in text


def count_review_rounds(conn: sqlite3.Connection, task_id: str) -> int:
    """Number of ``changes_requested`` verdicts recorded for ``task_id``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested'",
        (task_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def resolve_per_profile_cap(
    spec: Union[int, Mapping[str, Any], None], assignee: Optional[str]
) -> Optional[int]:
    """Per-profile in-flight cap for ``assignee`` from ``spec``.

    ``kanban.max_in_progress_per_profile`` is either a single positive int
    (every profile gets the same cap — the original #21582 shape) or a
    mapping ``{default: N, <profile>: M, ...}`` so one hungry profile (a
    reviewer that re-runs CI locally, a browser-pool profile) can be held
    tighter than the coders without starving them. ``None`` / invalid = no cap.
    """
    if spec is None:
        return None
    if isinstance(spec, Mapping):
        key = _canonical_assignee(assignee) if assignee else None
        raw = spec.get(key) if key is not None else None
        if raw is None and key is not None:
            # Config keys are written by hand (``Argus: 4``); compare them in
            # the same canonical form as the assignee, or a mixed-case key
            # silently falls back to the default cap (C6, #1002).
            for spec_key, spec_value in spec.items():
                if (
                    isinstance(spec_key, str)
                    and spec_key != "default"
                    and _canonical_assignee(spec_key) == key
                ):
                    raw = spec_value
                    break
        if raw is None:
            raw = spec.get("default")
    else:
        raw = spec
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def review_stale_minutes() -> int:
    """Minutes an unclaimed review card may sit before it is reported stale."""
    try:
        from hermes_cli.config import load_config

        value = (load_config() or {}).get("kanban", {}).get(
            "review_stale_minutes", 30
        )
        minutes = int(value)
    except Exception:
        return 30
    return minutes if minutes > 0 else 30


def resolve_reviewer(
    reviewer: Optional[str],
    implementer: Optional[str],
    *,
    allow_same_actor: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve+validate a review assignee. Returns ``(canonical, error)``.

    The invariant this enforces, at the moment the reviewer is SET: a review
    assignee must be spawnable (a real profile) or explicitly ``human``.
    Before this gate any free-text string (the literal ``reviewer``) was
    accepted, the card was reassigned to it, and the dispatcher then skipped
    it forever as a "non-spawnable assignee" with nothing alerting.

    ``reviewer=None`` resolves to config ``kanban.review_assignee`` — never to
    the implementer, never to a placeholder.
    """
    if reviewer is None or not str(reviewer).strip():
        reviewer = configured_review_assignee()
        if reviewer is None:
            # No explicit reviewer and no configured default: leave the
            # assignee untouched (pre-gate behavior) rather than refusing the
            # transition outright — an unconfigured board would otherwise be
            # unable to request review at all. The fleet sets
            # kanban.review_assignee, so it resolves to a real profile there;
            # boards that don't are surfaced by review_awaiting_human()'s
            # stale branch instead of silently parking.
            return None, None

    raw = str(reviewer).strip()
    if is_human_reviewer(raw):
        return raw.casefold(), None

    try:
        canonical = _canonical_assignee(raw)
    except Exception:
        canonical = None
    if not canonical:
        return None, f"invalid reviewer {raw!r}"

    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        # Can't introspect profiles (partial install) — accept, preserving
        # the pre-gate behavior rather than hard-failing the transition.
        return canonical, None

    if not spawnable_reviewer_profiles():
        # No profiles installed AT ALL: this is not a real fleet board (bare
        # checkout / hermetic test home), so profile_exists() would refuse
        # every possible reviewer. Fail open, mirroring has_spawnable_ready.
        return canonical, None

    if not profile_exists(canonical):
        known = ", ".join(spawnable_reviewer_profiles()) or "(none installed)"
        return None, (
            f"reviewer {canonical!r} is not an installed profile and is not "
            f"the explicit sentinel '{HUMAN_REVIEWER_SENTINEL}' — a review "
            "card assigned to it can never be spawned and would wait "
            f"forever. Spawnable reviewer profiles: {known}"
        )

    if (
        implementer
        and not allow_same_actor
        and canonical == _canonical_assignee(implementer)
    ):
        return None, (
            f"reviewer {canonical!r} is the implementer — same-actor review "
            "is refused; pass a different reviewer or allow_same_actor=True "
            "(--allow-same-actor), which is recorded on the event"
        )

    return canonical, None


def review_awaiting_human(
    conn: sqlite3.Connection, *, stale_minutes: Optional[int] = None
) -> list[dict]:
    """Review cards that no autonomous reviewer will ever pick up (or hasn't).

    Returns one dict per card — ``{task_id, assignee, age_minutes, reason}``,
    oldest first — for every ``review`` task that is unclaimed AND either

    * ``non_spawnable``: its assignee is not an installed profile (an explicit
      ``human`` lane, or the placeholder bug this detector exists for), or
    * ``stale``: it is spawnable but has sat unclaimed longer than
      ``kanban.review_stale_minutes``.

    The dispatcher previously reported these as "terminal lane, OK" and
    nothing alerted, so a card could wait for a human indefinitely with no
    signal (incident 2026-09-21: 10 cards, one for 2h+).
    """
    if stale_minutes is None:
        stale_minutes = review_stale_minutes()
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        profile_exists = None  # type: ignore[assignment]

    now = int(time.time())
    out: list[dict] = []
    for row in conn.execute(
        "SELECT id, assignee, COALESCE(started_at, created_at) AS since "
        "FROM tasks WHERE status = 'review' AND claim_lock IS NULL"
    ):
        assignee = row["assignee"]
        since = int(row["since"] or now)
        age_minutes = max(0, (now - since) // 60)
        spawnable = bool(
            assignee
            and profile_exists is not None
            and profile_exists(assignee)
        )
        if not assignee or not spawnable:
            reason = "non_spawnable"
        elif age_minutes >= stale_minutes:
            reason = "stale"
        else:
            continue
        out.append(
            {
                "task_id": row["id"],
                "assignee": assignee,
                "age_minutes": age_minutes,
                "reason": reason,
            }
        )
    out.sort(key=lambda r: -r["age_minutes"])
    return out


def format_review_awaiting_human(entries: list[dict]) -> Optional[str]:
    """One-line operator summary for :func:`review_awaiting_human`."""
    if not entries:
        return None
    oldest = entries[0]
    ids = ", ".join(e["task_id"] for e in entries[:5])
    if len(entries) > 5:
        ids += f", +{len(entries) - 5} more"
    return (
        f"review: {len(entries)} awaiting HUMAN "
        f"(oldest {oldest['age_minutes']}m): {ids}"
    )


def arm_review_stale_alerts(conn: sqlite3.Connection, entries: list[dict]) -> list[dict]:
    """Return the subset of *entries* that have not yet been alerted, arming them.

    One-shot per review episode: an alert fires the first time a card crosses
    the threshold, and re-arms when the card is claimed (a claim closes the
    review episode, and a later ``review_requested`` starts a new one, both of
    which leave events newer than the alert marker).
    """
    fresh: list[dict] = []
    with write_txn(conn):
        for entry in entries:
            task_id = entry["task_id"]
            row = conn.execute(
                "SELECT kind FROM task_events "
                "WHERE task_id = ? "
                "  AND kind IN ('review_stale_alerted', 'review_requested', "
                "               'claimed') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is not None and row["kind"] == "review_stale_alerted":
                continue  # already alerted for this review episode
            _append_event(
                conn,
                task_id,
                "review_stale_alerted",
                {
                    "assignee": entry.get("assignee"),
                    "age_minutes": entry.get("age_minutes"),
                    "reason": entry.get("reason"),
                },
            )
            fresh.append(entry)
    return fresh


@_home_session_guarded("request-review")
def request_review(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    force: bool = False,
    allow_same_actor: bool = False,
    with_reason: bool = False,
):
    """Transition implementation work into the first-class review phase.

    Unlike :func:`block_task`, this transition never touches block recurrence
    accounting.  The current implementer and resolved reviewer are recorded on
    the event so an autonomous reviewer can route requested changes back to the
    right profile.  Supplying ``reviewer`` reassigns the task before it is
    exposed to the review dispatcher.  On re-review, omitting it reuses the
    reviewer provenance persisted by the latest ``changes_requested`` event.

    When the task is ``running`` under a live claim, a caller that supplies no
    ``expected_run_id`` must pass ``force=True`` (explicit human/CLI override)
    — otherwise the request is refused instead of silently clearing the live
    worker's ``claim_lock``/``worker_pid``. Workers prove ownership by passing
    their own run id as ``expected_run_id`` (unchanged).

    Returns ``bool`` by default. With ``with_reason=True`` returns
    ``(ok, reason)`` mirroring :func:`request_changes` — ``reason`` is a
    diagnostic string on failure, ``None`` on success.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)

    # ── Review-round cap (kanban.max_review_rounds) ────────────────────────
    # Evaluated BEFORE the transaction because block_task opens its own.
    # Round N+1 on a card that already collected N changes_requested verdicts
    # does not re-spawn the reviewer: the ping-pong has stopped converging and
    # the orchestrator takes the card over. The block is typed needs_input so
    # the notifier wakes the orchestrator and the recurrence accounting is
    # honest (this is a real "needs a human/orchestrator decision" block).
    cap = configured_max_review_rounds()
    if cap > 0 and not force:
        rounds = count_review_rounds(conn, task_id)
        if rounds >= cap:
            reason = (
                f"review round cap reached: {rounds} changes_requested round(s) "
                f"on this card (kanban.max_review_rounds={cap}). Not re-spawning "
                "the reviewer — card BLOCKED (needs_input) for orchestrator "
                "take-over. Re-request with force=True (--force) to override."
            )
            blocked = block_task(
                conn, task_id, reason=reason, kind="needs_input",
                expected_run_id=expected_run_id,
            )
            if blocked:
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "review_round_cap",
                        {"rounds": rounds, "cap": cap, "summary": (summary or "")[:400] or None},
                    )
            return _ret(False, reason)

    # ── Review policy (kanban.review_policy=milestone_only|none) ────────────────
    # Slice cards do not get a reviewer session; CI is their gate. They are
    # COMPLETED here with a review_skipped event so the orchestrator's merge
    # pass sees them as done-with-PR. The policy applies even when the worker
    # names a reviewer profile explicitly (workers were templated to pass
    # reviewer="argus" on every card — that IS the mechanism being removed).
    # Only the explicit ``human`` sentinel or force=True (operator) bypasses it.
    _policy = configured_review_policy()
    _neg_result = None
    if (
        _policy in ("milestone_only", "none")
        and not force
        and not is_human_reviewer(reviewer)
    ):
        _skip_review = _policy == "none" or not is_milestone_card(conn, task_id)
        if _skip_review:
            # A handoff that itself says its close gate is NOT met / not
            # proven / FAIL is never completed in place (t_db9ca661): it goes
            # to the human review lane, where the orchestrator decides.
            from hermes_cli.kanban_negative_result import negative_result_match

            _neg_result = negative_result_match(summary, metadata)
            if _neg_result is not None:
                reviewer = HUMAN_REVIEWER_SENTINEL
        if _skip_review and _neg_result is None:
            skip_meta = dict(metadata or {})
            skip_meta["review_skipped"] = "policy_none" if _policy == "none" else "non_milestone"
            done = complete_task(
                conn, task_id, summary=summary, metadata=skip_meta,
                expected_run_id=expected_run_id,
            )
            if done and _task_status(conn, task_id) == "review":
                return _ret(True, "open PR named in the handoff — auto-routed to review instead of done")
            if not done:
                return _ret(
                    False,
                    f"review_policy={_policy}: card needs no review session and "
                    "could not be completed in place (not running/ready, or "
                    "expected_run_id mismatch)",
                )
            with write_txn(conn):
                _append_event(
                    conn, task_id, "review_skipped",
                    {"policy": _policy, "summary": (summary or "")[:400] or None},
                )
            _why = "review_policy=none" if _policy == "none" else "non-milestone card"
            return _ret(True, f"review skipped ({_why}, kanban.review_policy={_policy}) — card completed; CI is the gate")

    # Branch-base guard for handoffs that reach a review session (milestone
    # cards); the policy path above is gated inside complete_task (t_18e781d0).
    _bb_task = get_task(conn, task_id) if not force else None
    if _bb_task is not None and _bb_task.status == "running":
        _enforce_branch_base(conn, _bb_task, metadata)
        if expected_run_id is not None:
            _enforce_handback_head(conn, task_id, summary=summary, metadata=metadata)

    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    # A completion the fork auto-routed here already promoted + staged the
    # prose-named scratch files (``_stage_routed_scratch_artifacts`` stamps
    # ``routed_artifacts``); re-scanning the same prose would copy each file a
    # second time (``name_1.ext``) and attach it twice.
    if not (isinstance(metadata, dict) and metadata.get("routed_artifacts")):
        metadata = _merge_completion_prose_artifacts(
            conn, task_id, metadata, summary=summary, result=None,
        )
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        result = _request_review_txn(
            conn, task_id, summary=summary, metadata=metadata, reviewer=reviewer,
            expected_run_id=expected_run_id, force=force, allow_same_actor=allow_same_actor,
            now=now, staged_copies=staged_copies, _ret=_ret,
            negative_result=(
                {"policy": _policy, "match": _neg_result} if _neg_result is not None else None
            ),
        )
        return result
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise


def _enforce_handback_head(conn, task_id, *, summary, metadata, result=None, survivor_pr=None):
    """Refuse a worker handoff that cites an older commit of its open PR (t_e52337cb).

    Raises :class:`kanban_handback_head.StaleHandbackHeadError` (nothing else
    mutated) after a ``completion_blocked_stale_head`` event. An unreadable PR
    is fail-open: ``head_check_unavailable`` is logged and the handoff proceeds.
    """
    from hermes_cli import kanban_handback_head as _hh

    try:
        report = _hh.check(task_id=task_id, summary=summary, result=result,
                           metadata=metadata, survivor_pr=survivor_pr)
    except _hh.StaleHandbackHeadError as err:
        with write_txn(conn):
            _append_event(conn, task_id, "completion_blocked_stale_head", {"stale": err.stale})
        raise
    if report["unavailable"]:
        with write_txn(conn):
            _append_event(conn, task_id, "head_check_unavailable", {"prs": report["unavailable"]})


def _request_review_txn(
    conn: sqlite3.Connection, task_id: str, *, summary, metadata, reviewer, expected_run_id,
    force, allow_same_actor, now, staged_copies, _ret, negative_result=None,
):
    """The transactional half of :func:`request_review` (artifact staging rides
    inside the txn; the caller discards staged copies on rollback).
    ``negative_result`` ({policy, match}) is recorded in the same txn so a
    failed audit write rolls the transition back instead of orphaning it."""
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            return _ret(False, "parent dependencies are not satisfied")
        trow = conn.execute(
            "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
            "worker_started_at FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if trow is None:
            return _ret(False, "task not found")
        # Refuse to clear a live worker's claim without proof of ownership
        # (expected_run_id) or an explicit human override (force=True);
        # the same fence as complete_task (_claim_is_live, #111764).
        if expected_run_id is None and not force and _claim_is_live(trow):
            return _ret(
                False,
                "task is running under a live claim; pass expected_run_id "
                "(worker ownership) or force=True (explicit operator "
                "override) instead of clearing the live run's claim",
            )
        # The actor is the run that did the work. ``assignee`` is the actor
        # only while a worker holds the card; on a never-claimed card it is
        # whoever the operator assigned -- possibly the reviewer itself, which
        # is what ``kanban create --assignee <reviewer>`` + ``request-review``
        # produces. request_changes() routes on this field and refuses a
        # handoff with no implementer provenance, so prefer the run's profile.
        implementer = None
        if trow["current_run_id"] is not None:
            arow = conn.execute(
                "SELECT profile FROM task_runs WHERE id = ?", (trow["current_run_id"],),
            ).fetchone()
            implementer = arow["profile"] if arow else None
        if implementer is None and trow["assignee"] != reviewer:
            # Recording the reviewer as its own implementer is worse than recording
            # nothing: request_changes() routes on this field and already refuses a
            # handoff with no implementer provenance (upstream).
            implementer = trow["assignee"]
        reviewer_from_provenance = False
        if reviewer is None:
            changes_run = conn.execute(
                "SELECT id FROM task_runs "
                "WHERE task_id = ? AND outcome = 'changes_requested' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            changes_event = None
            if changes_run is not None:
                changes_event = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND run_id = ? "
                    "AND kind = 'changes_requested' "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, int(changes_run["id"])),
                ).fetchone()
            try:
                changes_payload = (
                    json.loads(changes_event["payload"])
                    if changes_event and changes_event["payload"]
                    else {}
                )
            except (json.JSONDecodeError, TypeError):
                changes_payload = {}
            prior_reviewer = (
                changes_payload.get("reviewer")
                if isinstance(changes_payload, dict)
                else None
            )
            if changes_run is not None:
                if not isinstance(prior_reviewer, str) or not prior_reviewer.strip():
                    return _ret(
                        False,
                        "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
                reviewer = prior_reviewer
                reviewer_from_provenance = True
        # Validate/resolve at the gate: a review assignee must be spawnable
        # (a real profile) or the explicit `human` sentinel. A placeholder
        # string used to be accepted here and parked the card forever.
        reviewer, reviewer_error = resolve_reviewer(
            reviewer, implementer, allow_same_actor=allow_same_actor
        )
        if reviewer_error is not None and reviewer_from_provenance:
            # Inherited provenance the gate now refuses (e.g. a same-actor
            # review drain recorded reviewer == implementer, t_503df7c5):
            # the worker never chose it, so refusing strands a finished card
            # behind a generic "unknown id or already terminal". Fall back to
            # the configured default reviewer instead.
            reviewer, reviewer_error = resolve_reviewer(
                None, implementer, allow_same_actor=allow_same_actor
            )
        if reviewer_error is not None:
            return _ret(False, reviewer_error)
        assignee_sql = ", assignee = ?" if reviewer is not None else ""
        params: tuple[Any, ...]
        if expected_run_id is None:
            params = (reviewer, task_id) if reviewer is not None else (task_id,)
            run_guard = ""
        else:
            params = (
                (reviewer, task_id, int(expected_run_id))
                if reviewer is not None
                else (task_id, int(expected_run_id))
            )
            run_guard = " AND current_run_id = ?"
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'review',
                   claim_lock    = NULL,
                   claim_expires = NULL,
                   worker_pid    = NULL
            """ + assignee_sql + """
             WHERE id = ?
               AND status IN ('running', 'ready')
            """ + run_guard,
            params,
        )
        if cur.rowcount != 1:
            return _ret(
                False,
                "task is not in running/ready (or expected_run_id did not "
                "match the current run)",
            )
        if isinstance(metadata, dict):
            staged_copies.extend(_stage_completion_artifacts(
                conn, task_id, metadata, now, uploaded_by="kanban_request_review",
            ))
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="review_requested", status="review",
            summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
            profile=implementer,
        )
        payload: dict = {
            "summary": _first_line(summary, 400) or None,
            "implementer": implementer,
            "reviewer": reviewer,
            **(
                {"allow_same_actor": True}
                if allow_same_actor
                and reviewer
                and implementer
                and reviewer == _canonical_assignee(implementer)
                else {}
            ),
        }
        staged = _cleaned_artifact_paths(metadata)
        if staged:
            payload["artifacts"] = staged
        _append_event(conn, task_id, "review_requested", payload, run_id=run_id)
        if negative_result is not None:
            _append_event(
                conn, task_id, "review_negative_result",
                {**negative_result, "reviewer": reviewer},
            )
    return _ret(True)


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _json_dict(_row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None
_REVIEW_HEAD_SHA_RE = re.compile(_HEAD_SHA_PATTERN)

# An ``n/a: <reason>`` lens value certifies the lens does not APPLY to the
# deliverable. A reason that reports an INABILITY anywhere in it ("skipped",
# "the reviewer could not run it", "mutmut missing on host", "budget
# exhausted") is not an applicability claim: that reviewer must use
# kanban_block(kind=capability). The reason is NFKC-normalized and stripped of
# zero-width/control characters first, so invisible characters cannot split a
# phrase. Matching is deliberately conservative: an applicability reason that
# happens to use one of these words ("vendors cannot differ") is refused and
# must be rephrased ("vendors do not differ") -- a false refusal costs one
# rewrite, a false accept hides an unrun lens.
_REVIEW_NA_INABILITY = re.compile(
    r"\b(?:"
    r"skip(?:ped|ping|s)?|"
    r"(?:could|can)\s*(?:not|n['\u2019]t)|can['\u2019]t|cannot|unable|"
    r"(?:did|was|were|does|do)\s*(?:not|n['\u2019]t)\s+(?:run|ran|execute|executed|attempt|attempted|try|tried|finish|finished|complete|completed|get|reach)|"
    r"not\s+(?:run|ran|executed|attempted|tried|finished|completed|reached)|"
    r"ran\s+out|out\s+of\s+time|no\s+time\b|timed?\s*out|"
    r"fail(?:ed|s)?\s+to\b|errored|crashed|blocked\s+(?:by|on)\b|"
    r"missing|not\s+(?:available|installed|accessible)|unavailable|inaccessible|"
    r"no\s+(?:\S+\s+){0,3}?(?:tool|tools|tooling|access|budget|runner|harness)\b|lacked?\s+access|"
    r"exhausted|deferred|postponed|todo\b|tbd\b"
    r")",
    re.IGNORECASE,
)

# Items must name a finding, not a placeholder: at least this many visible
# characters, one of them alphanumeric.
_REVIEW_ITEM_MIN_CHARS = 3

# A single review round longer than a day is not a real measurement.
_REVIEW_MINUTES_MAX = 24 * 60


def _review_normalize(text: str) -> str:
    """NFKC-normalize and drop zero-width/format/control characters."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        ch if unicodedata.category(ch) not in {"Cf", "Cc"} else (" " if ch in "\t\n\r" else "")
        for ch in text
    )
    return " ".join(text.split())


def _review_coverage_payload(line: str) -> Optional[str]:
    """Return the JSON text of a ``review_coverage:`` line, or None.

    Accepts the bare form and the inline-code form the skill documents
    (one surrounding backtick pair), so a reviewer who copies the example
    verbatim is not refused.
    """
    text = line.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    if not text.startswith("review_coverage:"):
        return None
    return text.split("review_coverage:", 1)[1].strip()


def _review_lens_state(state: Any) -> Optional[str]:
    """Casefold a lens state: ``done``, ``n/a: <reason>`` or None."""
    if not isinstance(state, str):
        return None
    return _review_normalize(state).casefold()


def _review_na_reason_ok(state: Any) -> bool:
    norm = _review_lens_state(state)
    if norm is None or not norm.startswith("n/a:"):
        return False
    reason = norm[4:].strip()
    return bool(reason) and not _REVIEW_NA_INABILITY.search(reason)


def _review_item_ok(item: Any) -> bool:
    if not isinstance(item, str):
        return False
    text = _review_normalize(item)
    return len(text) >= _REVIEW_ITEM_MIN_CHARS and any(ch.isalnum() for ch in text)


def _latest_review_coverage(rows: list) -> tuple[Optional[dict], Optional[str]]:
    """Newest comment line (newest comment first) that parses to a JSON object.

    A later prose comment that merely mentions ``review_coverage:`` must not
    hide a valid earlier record. When nothing parses, report why the NEWEST
    candidate failed.
    """
    first_error: Optional[str] = None
    for row in rows:
        payloads = [p for p in map(_review_coverage_payload, row["body"].splitlines()) if p is not None]
        if not payloads:
            first_error = first_error or "missing review_coverage JSON line"
            continue
        for payload in reversed(payloads):
            try:
                coverage = json.loads(payload)
            except (ValueError, TypeError):
                first_error = first_error or "invalid review_coverage JSON"
                continue
            if isinstance(coverage, dict):
                return coverage, None
            first_error = first_error or "review_coverage must be a JSON object"
    return None, first_error or "missing review_coverage JSON line"
_REVIEW_COVERAGE_MISSING = (
    "missing review_coverage comment on this review run "
    "(use kanban_block(kind=capability) if a lens cannot run)"
)


def _validate_review_coverage(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[str]:
    """Require a current-run, parseable review record before returning work.

    A prior round's comment cannot certify the current head.  The batch id is
    recorded in the comment, not inferred from a model's unsupported claim.
    """
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND run_id = ? "
        "AND body LIKE '%review_coverage:%' ORDER BY id DESC",
        (task_id, run_id),
    ).fetchall()
    if not rows:
        return _REVIEW_COVERAGE_MISSING
    coverage, error = _latest_review_coverage(rows)
    if coverage is None:
        return error
    return _review_coverage_record_error(coverage)


def review_coverage_text_error(text: Any) -> Optional[str]:
    """Why a caller-supplied ``review_coverage`` JSON is invalid, or None.

    Pure (no DB access): request-changes runs it before any write, so a
    refused record never lands on the card where the lander would read it as
    the deciding record (t_c5bfb48b).
    """
    coverage, error = _latest_review_coverage(
        [{"body": "review_coverage: " + str(text or "").strip()}]
    )
    if coverage is None:
        return error
    return _review_coverage_record_error(coverage)


def _review_coverage_record_error(coverage: dict) -> Optional[str]:
    """Field checks for one parsed review_coverage record; None when valid."""
    lenses = coverage.get("lenses")
    if not isinstance(lenses, dict):
        return "missing lenses object"
    lenses = {
        _review_normalize(key).casefold(): value
        for key, value in lenses.items() if isinstance(key, str)
    }
    for lens in _REVIEW_LENSES:
        state = lenses.get(lens.casefold())
        if _review_lens_state(state) == "done":
            continue
        if _review_na_reason_ok(state):
            continue
        return f"missing/invalid lens {lens}: use 'done' or 'n/a: <applicability reason>'; inability to run requires kanban_block(kind=capability)"
    count = coverage.get("findings")
    if type(count) is not int or count < 1:
        return "findings must be an integer >= 1"
    items = coverage.get("items")
    if (not isinstance(items, list) or len(items) != count
            or not all(_review_item_ok(item) for item in items)):
        return (
            "items must list exactly findings findings, each at least "
            f"{_REVIEW_ITEM_MIN_CHARS} visible characters with a letter or digit"
        )
    minutes = coverage.get("review_minutes")
    if type(minutes) is not int or minutes < 0 or minutes > _REVIEW_MINUTES_MAX:
        return f"review_minutes must be an integer from 0 to {_REVIEW_MINUTES_MAX}"
    # Optional (per-card batteries are being retired; CI owns suites). When
    # given it must still be a real value, not an empty placeholder.
    battery = coverage.get("battery")
    if battery is not None and (not isinstance(battery, str) or not battery.strip()):
        return "battery, when given, must be a nonempty string (attachment name, 'seeded' or 'n/a: <reason>')"
    batch = coverage.get("batch_id")
    if not isinstance(batch, str) or not batch.strip():
        return "batch_id must identify the single delegate_task batch in this comment"
    head = coverage.get("head_sha")
    if not (isinstance(head, str) and (_REVIEW_HEAD_SHA_RE.fullmatch(head.strip())
                                        or _review_na_reason_ok(head))):
        return "head_sha must be the reviewed PR head (7-40 hex) or 'n/a: <reason>' for a card with no PR"
    # review_coverage v2 (additive): records with no v2 key pass untouched.
    return _validate_review_v2(coverage, surface="board")


def _operator_caller_profiles() -> frozenset[str]:
    """Profiles the current caller holds, for the operator send-back check.

    The bound actor (CLI / tool surface) resolves through
    :func:`_actor_profiles` like the home-session guard; an unbound caller
    (the dashboard server) falls back to its profile env, then the active
    profile.
    """
    actor = _EVENT_ACTOR.get()
    if actor is not None:
        names = set(_actor_profiles(actor))
    else:
        profile, _sid = _event_actor()
        names = {profile} if profile else set()
    if not names:
        try:
            from .profiles import get_active_profile_name

            active = get_active_profile_name()
        except Exception:
            active = None
        if active:
            names.add(active)
    return frozenset(names)


def _operator_send_back_refusal(task_id: str, reason: str) -> Optional[str]:
    """Why an ``--operator`` send-back is refused, or ``None`` if allowed."""
    profiles = _operator_caller_profiles()
    if not (profiles & OPERATOR_PROFILES):
        return (
            f"refused request-changes on {task_id}: --operator is for operator "
            f"profiles ({', '.join(sorted(OPERATOR_PROFILES))}); caller "
            f"profile(s): {', '.join(sorted(profiles)) or 'unknown'}. A "
            f"reviewer sends back with a full review_coverage record."
        )
    if not _valid_operator_reason(reason):
        return (
            f"refused request-changes on {task_id}: --operator needs "
            f"\"<who: why>\" (e.g. \"Ace via Apollo: wrong repo\")."
        )
    return None


class _SendBackRefused(Exception):
    """Roll back a send-back's own review claim when the handoff is refused."""

    def __init__(self, detail: Optional[str]) -> None:
        super().__init__(detail)
        self.detail = detail


@_home_session_guarded("request-changes")
def request_changes(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    expected_run_id: Optional[int] = None,
    claimer: Optional[str] = None,
    coverage: Optional[str] = None,
    session_ref: Optional[str] = None,
    operator: Optional[str] = None,
    operator_kind: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Finish an active review run and route the task back for rework.

    The transition is valid only for a run claimed from ``review``.  It closes
    that reviewer run, restores the implementer recorded by the latest
    ``review_requested`` event, reapplies parent gating, and emits an auditable
    ``changes_requested`` event.  The second tuple item is the implementer on
    success or a diagnostic reason on failure.

    ``claimer`` is the reviewer send-back for a card parked in ``review``
    with nobody holding it (human/orchestrator review lane, no dispatched
    reviewer). When set, and the caller is not a worker run
    (``expected_run_id`` is None), the review run is opened here as
    ``claimer`` -- the same ``claimed(source_status=review)`` event a
    dispatched reviewer leaves -- and the changes are requested in the SAME
    transaction. Any refusal after that point rolls the claim back, so a
    refused send-back leaves the card in ``review`` with no orphan run.
    A card running under a non-review claim is refused exactly as before.

    ``coverage`` is the reviewer's ``review_coverage`` JSON. When given it is
    recorded as a comment bound to the run being closed (for the send-back,
    the run opened above) in the same transaction, before the coverage gate
    reads it -- the only way a parked-review send-back can carry a
    current-run record. A parked-review send-back without it is refused
    before any claim is opened.

    ``session_ref`` (trusted runtime context, never model args) is recorded on
    the opened run's ``claimed`` event, as ``claim_review_task`` does for
    ``claim --review``.

    ``operator`` (``"<who: why>"``, the home-guard #1074 vocabulary) is the
    operator send-back: an operator profile (:data:`OPERATOR_PROFILES`)
    bouncing a card for a non-review reason ("wrong repo", "rebase first").
    It waives the coverage requirement for that call only and records an
    ``operator_override`` event on the closed run. A non-operator caller or a
    reason without ``who: why`` is refused before anything is written. The
    coverage gate is unchanged for every call without it.

    ``operator_kind`` (:data:`OPERATOR_KINDS`, default ``human``) says what
    sent an operator send-back back: ``machine`` for automation (the
    hermes-home merge pass's Prism HOLD-FR). It is recorded on the
    ``changes_requested`` event as ``operator_kind`` so a landing gate keys on
    a FIELD, not on the operator string, to tell a human CHANGES REQUESTED
    (holds a landing) from a machine send-back (never holds; t_86ca5b3d).
    Without ``operator`` it is refused.
    """
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"
    # Same normalization as the home-guard actor, so its dedupe matches.
    operator_reason = (str(operator).strip() or None) if operator else None
    kind = str(operator_kind).strip().lower() if operator_kind is not None else None
    if kind is not None and kind not in OPERATOR_KINDS:
        return False, f"operator_kind must be one of {', '.join(OPERATOR_KINDS)}"
    if kind is not None and operator_reason is None:
        return False, "operator_kind needs --operator \"<who: why>\""
    if operator_reason is not None:
        refusal = _operator_send_back_refusal(task_id, operator_reason)
        if refusal is not None:
            return False, refusal
    coverage_text = str(redact_review_value(coverage or "")).strip()
    if coverage_text:
        # Validate the whole record before any write (t_c5bfb48b), so no
        # rejected call can leave it behind.
        coverage_error = review_coverage_text_error(coverage_text)
        if coverage_error:
            return False, coverage_error
    coverage_body = f"review_coverage: {coverage_text}" if coverage_text else None
    comment_author = str(claimer or "reviewer").strip() or "reviewer"

    opened: list[int] = []
    posted: list[tuple[int, int, int]] = []  # (comment_id, run_id, created_at)

    def _in_txn() -> tuple[bool, Optional[str]]:
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id, claim_lock, claim_expires "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if claimer and expected_run_id is None and task_row["status"] == "review":
            if coverage_body is None and operator_reason is None:
                # Same message the gate gives; refused before any claim churn.
                return False, _REVIEW_COVERAGE_MISSING
            if _prior_worker_still_alive(conn, task_id) is not None:
                return False, "a prior worker run on this task is still alive"
            now = int(time.time())
            run_id = _open_review_run(
                conn, task_id, lock=str(claimer),
                expires=now + _resolve_claim_ttl_seconds(None), now=now,
                session_ref=session_ref,
            )
            if run_id is None:
                return False, "task left review before the review run opened"
            opened.append(run_id)
            current_run_id = run_id
        elif task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _json_dict(_row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"
        if claimer and expected_run_id is None and not opened:
            # C5 #70 (PR #1081): a send-back with no run id carries no proof
            # of which run it owns (``claimer`` is a reusable profile name, not
            # a run credential), so it may close only a lapsed run: claim
            # expired and no live owner. A live run is closed with its run id.
            if (
                task_row["claim_lock"]
                and int(task_row["claim_expires"] or 0) > int(time.time())
            ) or _prior_worker_still_alive(conn, task_id) is not None:
                return False, (
                    "a live review run holds this task; pass its run id "
                    "(expected_run_id) to close it"
                )

        requested_event = _latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _nonblank_str(_json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        if coverage_body is not None:
            now = int(time.time())
            comment_cur = conn.execute(
                "INSERT INTO task_comments "
                "(task_id, author, body, run_id, session_ref, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (task_id, comment_author, coverage_body, int(current_run_id), now),
            )
            _append_event(
                conn, task_id, "commented",
                {"author": comment_author, "len": len(coverage_body)},
                run_id=int(current_run_id),
            )
            posted.append((int(comment_cur.lastrowid or 0), int(current_run_id), now))
        coverage_error = (
            None if operator_reason is not None
            else _validate_review_coverage(conn, task_id, int(current_run_id))
        )
        if coverage_error:
            return False, coverage_error
        reviewer = task_row["assignee"]
        if isinstance(reviewer, str) and reviewer.strip():
            reviewer = _canonical_assignee(reviewer)
        else:
            reviewer = None

        new_status = _landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        _append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
                **({"operator": operator_reason,
                    "operator_kind": kind or OPERATOR_KIND_HUMAN} if operator_reason else {}),
            },
            run_id=run_id,
        )
        if operator_reason is not None:
            by_profile, _sid = _event_actor()
            bound = _EVENT_ACTOR.get()
            home_row = conn.execute(
                "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            _append_event(
                conn,
                task_id,
                "operator_override",
                {
                    "action": "request-changes",
                    "reason": operator_reason,
                    "coverage_waived": True,
                    "by_sessions": list(bound.session_ids) if bound else [],
                    "by_profile": by_profile,
                    "home": home_row["session_id"] if home_row is not None else None,
                },
                run_id=run_id,
            )
        return True, implementer

    try:
        with write_txn(conn):
            ok, detail = _in_txn()
            # A refusal rolls back everything this call wrote: the opened
            # claim AND a posted coverage comment the gate just rejected
            # (C7 k107) -- a refused record must not persist on the run.
            if not ok and (opened or posted):
                raise _SendBackRefused(detail)
    except _SendBackRefused as exc:
        return False, exc.detail
    # Journal the committed coverage comment (add_comment's content hook),
    # only after commit so a rolled-back send-back journals nothing.
    for comment_id, comment_run_id, created_at in posted:
        try:
            from hermes_cli import kanban_journal

            kanban_journal.append(
                _journal_board_slug(), task_id, "comment_body",
                {"author": comment_author, "body": coverage_body,
                 "session_ref": None, "created_at": created_at,
                 "comment_id": comment_id},
                actor=comment_author, run_id=comment_run_id,
            )
        except Exception:  # pragma: no cover - never fail the transition
            pass
    return ok, detail


@_home_session_guarded("requeue")
def requeue_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: str,
) -> tuple[bool, Optional[str]]:
    """Record operator intent to run a READY card now (``requeued`` event).

    The operator verb for a card that is already ``ready`` but deferred by the
    respawn guard (typically ``active_pr``: the open PR is the fix-round
    target). ``unblock``/``reopen``/``triage-resolve`` all require a non-ready
    source state, which previously forced a block->unblock round trip just to
    mint an intent event. Status is unchanged; ``requeued`` is in
    ``_RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS`` so the next dispatch tick spawns.

    Returns ``(True, None)`` on success, ``(False, reason)`` if refused.
    """
    if not (reason or "").strip():
        return False, "a reason is required to requeue a task"
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False, f"task {task_id} not found"
        if row["status"] != "ready":
            return False, (
                f"task {task_id} is {row['status']!r}; requeue only applies to "
                f"'ready' tasks (use unblock/reopen/triage-resolve/promote "
                f"for other states)"
            )
        _append_event(
            conn, task_id, "requeued", {"actor": actor, "reason": reason},
        )
    return True, None


@_home_session_guarded("reopen")
def reopen_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: str,
    to_status: str = "ready",
) -> tuple[bool, Optional[str]]:
    """Void a terminal completion and return the task to the queue.

    Recovery path for a FALSE terminal state — most importantly one written by
    a process that was never the run owner (an ordinary nested process that
    inherited a worker's ``HERMES_KANBAN_*`` env and called ``kanban_complete``
    on its parent's card, 2026-08-12). Comments cannot fix that: the card still
    reads ``done`` and carries the impostor's summary as its durable result, and
    every downstream consumer — dependent children, ``build_worker_context``,
    the board UI — trusts those fields.

    ``edit_completed_task_result`` is deliberately not enough here. It backfills
    the result of a task that is legitimately done; it cannot express "this
    completion never happened", leaves the task terminal, and so cannot let the
    real work be re-dispatched.

    The false result is CLEARED rather than overwritten, and the closing run is
    marked ``outcome='voided'`` so the audit trail keeps the fact that a bogus
    completion occurred instead of silently rewriting history. ``reason`` is
    mandatory and recorded on the ``reopened`` event: a terminal state being
    reversed is exactly the kind of mutation that must never be anonymous.

    Returns ``(True, None)`` on success, ``(False, reason)`` if refused.
    """
    if to_status not in ("ready", "todo", "review"):
        return False, f"invalid target status {to_status!r} (use 'ready', 'todo' or 'review')"
    if not (reason or "").strip():
        return False, "a reason is required to reverse a terminal state"

    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"
    if row["status"] != "done":
        return False, (
            f"task {task_id} is {row['status']!r}; reopen only applies to "
            f"'done' tasks (use unblock/promote for other states)"
        )

    # ``review`` (t_36d0114e): a card closed ``done`` while its PR is still
    # OPEN goes back to the review lane -- owned by kanban.review_assignee --
    # where the review-card closer completes it on REST merged=true.
    review_assignee = configured_review_assignee() if to_status == "review" else None
    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks "
            "   SET status = ?, result = NULL, completed_at = NULL, "
            "       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "       current_run_id = NULL, assignee = COALESCE(?, assignee) "
            " WHERE id = ? AND status = 'done'",
            (to_status, review_assignee, task_id),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} changed state concurrently; retry"
        run_id = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? AND outcome IN " + _SUCCESS_RUN_OUTCOMES_SQL + " "
            "ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        voided_run = int(run_id["id"]) if run_id else None
        if voided_run is not None:
            conn.execute(
                "UPDATE task_runs SET outcome = 'voided' WHERE id = ?",
                (voided_run,),
            )
        _append_event(
            conn,
            task_id,
            "reopened",
            {
                "actor": actor,
                "reason": reason,
                "to_status": to_status,
                "voided_run_id": voided_run,
                "voided_result": (row["result"] or "")[:500] or None,
            },
        )

        # This task is no longer ``done``, so any child promoted on that
        # premise now violates the invariant the dispatcher trusts: a ``ready``
        # task has all blocking parents done/archived (see ``promote_task``'s
        # own check). ``recompute_ready`` ONLY ever promotes, so nothing else
        # walks this back — the child stays dispatchable and a worker gets
        # spawned on a premise that was explicitly withdrawn. Demote the
        # unclaimed ones; a child already claimed or past ``ready`` is reported
        # rather than yanked out from under its running worker.
        demoted: list[str] = []
        still_live: list[str] = []
        children = conn.execute(
            "SELECT t.id, t.status, t.current_run_id, t.claim_lock FROM tasks t "
            "JOIN task_links l ON l.child_id = t.id "
            "WHERE l.parent_id = ? AND COALESCE(l.kind, ?) = ?",
            (task_id, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
        ).fetchall()
        for child in children:
            if (
                child["status"] == "ready"
                and not child["current_run_id"]
                and not child["claim_lock"]
            ):
                conn.execute(
                    "UPDATE tasks SET status = 'todo' "
                    "WHERE id = ? AND status = 'ready'",
                    (child["id"],),
                )
                _append_event(
                    conn,
                    child["id"],
                    "demoted",
                    {"reason": f"parent {task_id} reopened", "actor": actor},
                )
                demoted.append(child["id"])
            elif child["status"] in ("running", "review", "done"):
                still_live.append(child["id"])
        if demoted or still_live:
            _append_event(
                conn,
                task_id,
                "reopen_child_fanout",
                {"demoted": demoted, "not_demoted": still_live},
            )
    return True, None


@_home_session_guarded("promote")
def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Operator promotion ``todo``/``blocked`` -> ``ready`` with an audit event.
    Refused while a parent is unfinished; ``dry_run`` only validates.
    Returns ``(ok, reason)``."""
    cur_status = _task_status(conn, task_id)
    if cur_status is None:
        return False, f"task {task_id} not found"
    if cur_status in ("done", "archived"):
        # Same wording as complete: a stale replayed script must read as a no-op.
        return False, f"{explain_complete_refusal(conn, task_id)}; nothing to promote"

    if cur_status not in ("todo", "blocked"):
        hint = (
            f" (a scheduled card wakes with 'hermes kanban unblock {task_id}'"
            f" or 'hermes kanban schedule {task_id} --now|--at TS')"
            if cur_status == "scheduled" else ""
        )
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'{hint}"
        )

    # No override: claim_task demotes ready -> todo on an undone parent whichever
    # writer set 'ready', so a forced promotion would only report a success the
    # first claim silently reverts (#106195). The dependency itself is the knob.
    parents = conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? AND COALESCE(l.kind, ?) = ?",
        (task_id, DEFAULT_LINK_KIND, LINK_KIND_BLOCKS),
    ).fetchall()
    unsatisfied = [p["id"] for p in parents if p["status"] not in ("done", "archived")]
    if unsatisfied:
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')", (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(conn, task_id, "promoted_manual", {"actor": actor, "reason": reason})

    return True, None


def _reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses, now: int, note: str,
) -> None:
    """Close a leaked open run before a status flip so the invariant
    ``current_run_id IS NULL <=> run row terminal`` holds; no-op normally."""
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )


def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """``ready`` if every parent is terminal else ``todo`` — the re-gate shared by
    unblock/reopen so neither can spawn a child whose upstream is unfinished."""
    return "ready" if _parents_satisfied(conn, task_id) else "todo"


@_home_session_guarded("unblock")
def unblock_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    comment: Optional[tuple] = None,
) -> bool:
    """Transition ``blocked``/``scheduled`` to its safe resumable phase.

    ``comment`` = ``(author, body, run_id, session_ref)``: a status comment
    written in the SAME transaction as the transition, and only when it
    lands. A respawned worker can then never read the thread between the
    card becoming dispatchable and the reason appearing, and a refused or
    failed unblock leaves no comment (FleetReview aa67ba1c7513).

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    now = int(time.time())
    with write_txn(conn):
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if _task_status(conn, task_id) == "blocked"
            else "ready"
        )
        _reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL, "
            # A scheduled card's ``next_eligible_at`` is its timed-wake stamp
            # (``set_schedule_wake``); once it leaves ``scheduled`` a stale
            # value must not read as a rate-limit cooldown on the ready card.
            "next_eligible_at = CASE WHEN status = 'scheduled' "
            "THEN NULL ELSE next_eligible_at END "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        if comment is not None:
            c_author, c_body, c_run_id, c_session_ref = comment
            add_comment(
                conn, task_id, c_author, c_body,
                run_id=c_run_id, session_ref=c_session_ref,
            )
        _append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return True


# A ``transient`` block whose reason names HOST resource exhaustion (out of
# process slots, fork EAGAIN, load over the gate) clears when the host does,
# not when a human looks. t_b660edb6: t_5ea5bcd0 blocked "Studio is out of
# process slots ... Requeue once the host recovers" at 02:06 and sat 7h50m
# after the host recovered (02:10) until Apollo unblocked it by hand.
# Transient blocks for anything else (time gates, a lander, a judge error)
# are NOT host conditions and stay put: requeueing those every healthy tick
# would just walk them into the recurrence breaker.
HOST_TRANSIENT_REASON_RE = re.compile(
    r"EAGAIN|Resource temporarily unavailable|BlockingIOError|Errno 35"
    r"|process slots|out of process|maxprocperuid|fork\(?\)?:? "
    r"(?:fails|failed|returns|Resource)|host_emergency|\bload1\b|host load",
    re.IGNORECASE,
)
HOST_TRANSIENT_MIN_BLOCKED_SECONDS = 120


def requeue_host_transient_blocks(
    conn: sqlite3.Connection,
    *,
    note: str,
    now: Optional[int] = None,
    min_blocked_seconds: int = HOST_TRANSIENT_MIN_BLOCKED_SECONDS,
) -> list[str]:
    """Unblock ``transient`` cards blocked for host exhaustion; return ids.

    The CALLER decides the host has recovered (the dispatcher's load gate is
    admitting); this only selects which blocked cards that recovery clears:
    ``status='blocked'``, ``block_kind='transient'``, latest ``blocked`` event
    older than ``min_blocked_seconds`` whose reason matches
    :data:`HOST_TRANSIENT_REASON_RE`. Each goes through :func:`unblock_task`
    with a comment naming ``note`` in the same transaction, so the respawned
    worker reads why it is running again.
    """
    now = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        "SELECT t.id AS id, "
        "(SELECT e.payload FROM task_events e WHERE e.task_id = t.id "
        " AND e.kind = 'blocked' ORDER BY e.id DESC LIMIT 1) AS payload, "
        "(SELECT e.created_at FROM task_events e WHERE e.task_id = t.id "
        " AND e.kind = 'blocked' ORDER BY e.id DESC LIMIT 1) AS blocked_at "
        "FROM tasks t WHERE t.status = 'blocked' AND t.block_kind = 'transient'"
    ).fetchall()
    out: list[str] = []
    for row in rows:
        if row["blocked_at"] is None or now - int(row["blocked_at"]) < min_blocked_seconds:
            continue
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        reason = str((payload or {}).get("reason") or "") if isinstance(payload, dict) else ""
        if not HOST_TRANSIENT_REASON_RE.search(reason):
            continue
        body = (
            f"auto-requeue: host recovered ({note}). This card was blocked "
            f"kind=transient for a host condition: {reason[:300]}"
        )
        if unblock_task(conn, row["id"], comment=("kanban-dispatcher", body, None, None)):
            out.append(row["id"])
    return out


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Legacy verdict bypass retired: claim the review and request changes.

    A parked review has no reviewer-run evidence. Moving it directly to its
    implementer evades the full-review coverage gate on request_changes.
    Kept as a refusal for existing CLI callers; no state is changed, so it
    carries no home-session guard (a guard would print "reopen-review
    allowed" before the refusal). The ``review_reopened`` event it used to
    emit is historical: old rows are still read by the resume-status query.
    """
    return False


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``/``done``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated.
    Every surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor. Running descendants are closed
    ``reclaimed`` and their workers killed strictly post-commit (audit trail
    before death) — when composed, the CALLER must drain ``terminations``
    after its own commit. ``consecutive_failures`` resets (deliberate operator
    action), the opposite of :func:`reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "terminations": [(worker_pid, claim_lock, worker_started_at)]}``.
    """
    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn, allow_nested=True):
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock, t.worker_started_at
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append(_PendingTermination(
                    row["worker_pid"], row["claim_lock"],
                    _worker_owner_window(
                        conn, row["id"], row["worker_pid"],
                        row["current_run_id"],
                    ),
                    started_at=row["worker_started_at"],
                ))
                run_id = _end_run(
                    conn, row["id"], outcome="reclaimed", status="todo",
                    summary=f"ancestor {task_id} reopened",
                )
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "current_run_id = NULL, consecutive_failures = 0 WHERE id = ?", (row["id"],),
            )
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for entry in terminations:
            pid, claim_lock = entry[0], entry[1]
            _terminate_reclaimed_worker(
                pid, claim_lock, owner_window=_termination_window(entry),
                started_at=getattr(entry, "started_at", None),
            )
    return {"invalidated": invalidated, "terminations": terminations}


@_home_session_guarded("specify")
def specify_triage_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, assignee: Optional[str] = None, author: Optional[str] = None,
) -> bool:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one
    txn; False when not in triage. Lands in ``todo`` (not ``ready``) so parent
    gating still applies; the audit comment is written only when a field changed.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'", tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Not add_comment (own txn + 'commented' event); 'specified' below records it.
            _insert_comment(
                conn, task_id, author.strip(),
                "Specified — updated " + ", ".join(changed_fields) + " and promoted to todo.",
                int(time.time()),
            )
        _append_event(
            conn, task_id, "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Own IMMEDIATE txn (outside the one above): a parent-free specified task
    # flips to 'ready' now instead of idling until the next tick.
    recompute_ready(conn)
    return True

#: Statuses a human may resolve a ``triage`` card into. Deliberately does NOT
#: include ``ready`` — routing straight into the work pool would re-arm the very
#: unblock→re-block spin the escalation exists to stop. ``todo`` goes through
#: ``recompute_ready``, so parent gating still applies.
TRIAGE_RESOLVE_TARGETS = ("todo", "done", "archived")


@_home_session_guarded("triage-resolve")
def triage_resolve_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    to: str,
    reason: str,
    actor: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Resolve a ``triage`` card into ``to`` with an explicit human decision.

    ``triage`` is the terminus of the unblock-loop breaker in
    :func:`block_task`: an automated unblocker already tried and failed
    :data:`BLOCK_RECURRENCE_LIMIT` times, so a human must decide. That intent is
    correct — but before this verb existed the column had **no supported exit**.
    ``unblock_task`` no-ops (it only matches ``blocked``/``scheduled``),
    ``promote_task`` refuses ("promote only applies to 'todo' or 'blocked'"),
    and ``complete_task`` refuses (its guard is ``running``/``ready``/``blocked``).
    Operators were left writing raw SQL against a live board.

    This is that exit, and it is deliberately human-shaped:

    * ``to`` must be named explicitly (one of :data:`TRIAGE_RESOLVE_TARGETS`).
      There is no default, because "where should this go" is exactly the
      decision the escalation asked a human for.
    * ``reason`` is mandatory and non-blank — the audit trail is the point.
    * ``ready`` is NOT a legal target. Resolving to ``todo`` hands the card to
      ``recompute_ready``, which honours parent gating; jumping the queue would
      re-create the spin.

    On success the loop counter is cleared (``block_recurrences = 0``,
    ``block_kind = NULL``) so a human decision genuinely starts the task over
    rather than leaving it one re-block away from bouncing back to ``triage``.
    Any run still pointed at by ``current_run_id`` is closed as ``reclaimed``,
    and a ``triage_resolved`` event records WHO / WHY / WHERE.

    Returns ``(True, None)`` on success, ``(False, reason)`` when refused.
    """
    if to not in TRIAGE_RESOLVE_TARGETS:
        return False, (
            f"invalid target {to!r}; triage-resolve accepts "
            f"{', '.join(TRIAGE_RESOLVE_TARGETS)}"
        )
    reason = (reason or "").strip()
    if not reason:
        return False, "a reason is required (this is a human-in-the-loop decision)"

    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False, f"task {task_id} not found"
        if row["status"] in ("done", "archived"):
            return False, f"{explain_complete_refusal(conn, task_id)}; nothing to triage-resolve"
        if row["status"] != "triage":
            hint = {
                "blocked": "use 'hermes kanban unblock' instead",
                "scheduled": "use 'hermes kanban unblock' instead",
                "todo": "use 'hermes kanban promote' instead",
                "review": (
                    "a review card is parked until a reviewer verdict moves it: "
                    "'hermes kanban request-changes <id> \"<asks>\" --coverage "
                    "'<json>'' sends it back to the worker (status ready, "
                    "dispatchable); 'hermes kanban complete <id>' closes it. "
                    "assign/reassign alone leaves it in review, undispatched"
                ),
                "running": "use 'hermes kanban complete' instead",
                "ready": "use 'hermes kanban complete' instead",
            }.get(
                str(row["status"]),
                "use unblock/promote/complete instead",
            )
            return False, (
                f"task {task_id} is {row['status']!r}; triage-resolve only "
                f"applies to 'triage' ({hint})"
            )
        # A triaged card should have no live run (block_task closes it), but a
        # crash between the two writes could leave the pointer dangling. Close
        # it here so the runs invariant holds after the status flip.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="triage resolved with run still active",
        )
        completed_at_sql = ", completed_at = ?" if to == "done" else ""
        params: list[Any] = [to]
        if to == "done":
            params.append(now)
        params.append(task_id)
        cur = conn.execute(
            f"""
            UPDATE tasks
               SET status            = ?{completed_at_sql},
                   claim_lock        = NULL,
                   claim_expires     = NULL,
                   worker_pid        = NULL,
                   block_kind        = NULL,
                   block_recurrences = 0,
                   consecutive_failures = 0,
                   last_failure_error = NULL
             WHERE id = ? AND status = 'triage'
            """,
            tuple(params),
        )
        if cur.rowcount != 1:
            return False, f"task {task_id} status changed during resolve"
        if actor and actor.strip():
            # Inline INSERT: we're already inside this function's write_txn and
            # ``add_comment`` would open a nested BEGIN IMMEDIATE.
            _run_id, _sess_ref = _inline_comment_provenance(task_id)
            conn.execute(
                "INSERT INTO task_comments "
                "(task_id, author, body, run_id, session_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id, actor.strip(), f"TRIAGE-RESOLVE -> {to}: {reason}",
                    _run_id, _sess_ref, now,
                ),
            )
        _append_event(
            conn, task_id, "triage_resolved",
            {"to": to, "reason": reason, "actor": actor},
            run_id=run_id,
        )
    # Outside the txn (recompute_ready opens its own IMMEDIATE txn). Resolving
    # to ``todo`` promotes straight to ``ready`` when parents allow; resolving
    # to ``done``/``archived`` unblocks any children this card was stranding.
    recompute_ready(conn)
    return True, None


def _card_recorded_pr_refs(conn: sqlite3.Connection, task_id: str, runs=None) -> list:
    """Every PR string any of the card's runs persisted (``kanban_open_pr.recorded_pr_refs``): pr_url /
    pr_urls / pr, auto_routed_open_prs and survivor PR evidence. The card's own PRs for the closed-unmerged
    gates (FleetReview #1339), so neither done nor archive depends on which key carried the ref."""
    from hermes_cli import kanban_open_pr as _open_pr
    out: list = []
    for run in (list_runs(conn, task_id) if runs is None else runs):
        out.extend(_open_pr.recorded_pr_refs(run.metadata))
    return out


def _archive_closed_pr_gate(conn: sqlite3.Connection, task_id: str, query_fn=None) -> None:
    """Refuse to archive a card whose own PR was closed without merge (t_a1550189).

    Archiving hides a card the same way ``done`` does, so a card whose PR was auto-closed (stacked base
    deleted) must not vanish from the board with its content never on default. The card's own refs are
    every PR any of its runs recorded (:func:`_card_recorded_pr_refs`). Archive passes when the card's result,
    run summaries or comments carry a SUPERSEDED-BY / RE-CARRIED-AS token naming merged work, or a card
    comment records an explicit close decision (``CLOSED: REJECTED|ABANDONED|THROWAWAY|STALE|
    DUPLICATE-OF``, the line the close-reason contract says to copy onto the card). Unreadable refuses.
    """
    from hermes_cli import kanban_open_pr as _open_pr
    task = get_task(conn, task_id)
    if task is None or task.status == "archived":
        return
    runs = list_runs(conn, task_id)
    urls = _card_recorded_pr_refs(conn, task_id, runs=runs)
    if not urls:
        return
    texts: list = [task.result] + [run.summary for run in runs]
    comments = [c.body for c in list_comments(conn, task_id)]
    try:
        _open_pr.enforce_not_closed_unmerged(
            task_id, *texts, recorded=urls,
            query_fn=query_fn or _open_pr.memo_query(),
            verb="archive", decision_texts=comments,
        )
    except _open_pr.ClosedUnmergedPrError as closed_err:
        with write_txn(conn):
            _append_event(
                conn, task_id, "archive_blocked_closed_unmerged_pr",
                {"prs": closed_err.closed, "unverified": closed_err.unverified},
            )
        raise


@_home_session_guarded("archive")
def archive_task(conn: sqlite3.Connection, task_id: str, *, signal_fn=None) -> bool:
    """Archive ``task_id``. Raises :class:`kanban_open_pr.ClosedUnmergedPrError` (no state change) when
    the card's own PR is closed-unmerged with no recorded superseder or close decision (t_a1550189).

    A *running* task's host-local worker is terminated after commit (#76196):
    the kill is contingent on THIS caller winning the archive transition, and
    ``archived`` is terminal so no dispatcher can spawn a duplicate off the
    released claim. ``signal_fn`` is the test seam for the termination signal."""
    _archive_closed_pr_gate(conn, task_id)
    # Capture the live worker BEFORE the UPDATE nulls worker_pid (t_89dfa2c9).
    # Archive used to drop the pid, end the run and rmtree the workspace while
    # the worker process kept running: t_cfbbf9a9's worker ran 12 more minutes
    # from a deleted cwd (every guard-hook call failed closed, 12 CRITICAL
    # pages) and armed launchd jobs for a dropped card. No later reaper could
    # find it, because the only pointer to it was the pid this UPDATE clears.
    #
    # The snapshot is read INSIDE the same write txn as the UPDATE (t_b9d9bcbc,
    # Prism 82f34e87302a): read outside it, a worker stamping its pid between
    # the SELECT and the UPDATE was invisible here, so its pid was cleared,
    # it was never signalled, and its workspace was reaped. Inside the txn a
    # later stamp is fenced by _set_worker_pid's run check (the run is ended
    # below) and the spawner terminates that orphan.
    with write_txn(conn):
        prior = conn.execute(
            "SELECT worker_pid, claim_lock, current_run_id, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        live_pid = int(prior["worker_pid"]) if prior is not None and prior["worker_pid"] else None
        prior_lock = prior["claim_lock"] if prior is not None else None
        prior_run_id = prior["current_run_id"] if prior is not None else None
        prior_started = prior["worker_started_at"] if prior is not None else None
        owner_window = (
            _worker_owner_window(conn, task_id, live_pid, prior_run_id)
            if live_pid else (None, None, None)
        )
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status != 'archived'", (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # Archived mid-run (dashboard): close the run so history isn't orphaned.
        run_id = _end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # Stop the worker before its workspace is reaped below. Same identity-checked
    # host-local termination the reclaim paths use. Reap ONLY on proven death:
    # a host-local termination that reports ``terminated``. Anything else keeps
    # the workspace -- including a remote claim or a NULL claim_lock, where
    # _terminate_reclaimed_worker returns host_local=False without checking
    # anything (t_b9d9bcbc, Prism 6383a983f818). _worker_survived_termination
    # reads host_local=False as "not ours to hold" (right for reclaim's claim
    # release), which is not proof of death and must not gate a reap.
    can_reap = not live_pid
    if live_pid and live_pid != os.getpid():
        termination = _terminate_reclaimed_worker(
            live_pid, prior_lock, signal_fn=signal_fn, started_at=prior_started,
            owner_window=owner_window,
            conn=conn, task_id=task_id, run_id=prior_run_id,
        )
        can_reap = bool(termination.get("host_local") and termination.get("terminated"))
        if not can_reap:
            termination = {**termination, "workspace_kept": True}
        with write_txn(conn):
            _append_event(conn, task_id, "archive_worker_terminated",
                          termination, run_id=run_id)
    # ``archived`` parents no longer block children, same as ``done``.
    # Promote newly-unblocked dependents immediately instead of waiting
    # for a later dispatcher tick.
    recompute_ready(conn)
    # Reap only after worker death is verified (or there was no worker). An
    # in-worker archive cannot kill its own process before returning either.
    if can_reap:
        _cleanup_workspace(conn, task_id)
    return True


def _delete_task_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete every row referencing ``task_id`` (schema has no ON DELETE CASCADE)."""
    conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
    for table in ("task_comments", "task_events", "task_runs", "kanban_notify_subs"):
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete an ARCHIVED task (+ related rows); anything else must be
    archived first so data loss takes two deliberate actions."""
    with write_txn(conn):
        if _task_status(conn, task_id) != "archived":
            return False
        _delete_task_relations(conn, task_id)
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


class TaskRunningError(RuntimeError):
    """Raised by :func:`delete_task` when the target task is ``running``
    with a live ``worker_pid`` and ``force`` was not requested.

    Deliberately distinct from the ``not found -> False`` return so
    callers can tell "there is nothing to delete" apart from "there is a
    live worker attached to this row". The dashboard maps this to HTTP
    409; ``force=True`` overrides it.

    Structured attributes (``task_id``, ``worker_pid``, ``claim_lock``,
    ``run_id``) are attached for callers that want more than the message.
    """

    def __init__(
        self,
        task_id: str,
        worker_pid: Optional[int],
        claim_lock: Optional[str] = None,
        run_id: Optional[int] = None,
    ):
        self.task_id = task_id
        self.worker_pid = worker_pid
        self.claim_lock = claim_lock
        self.run_id = run_id
        super().__init__(
            f"refusing to delete task {task_id}: it is running with a live "
            f"worker (pid={worker_pid}, claim={claim_lock or 'none'}, "
            f"run={run_id if run_id is not None else 'none'}). Deleting it "
            "would amputate that worker — its board row, run history and "
            "event log vanish mid-flight and it can no longer complete or "
            "block. Archive the task first (which reclaims the run), or "
            "pass force=True if the worker is genuinely wedged."
        )


def delete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    force: bool = False,
) -> bool:
    """Hard-delete a task and cascade to all related rows.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    we explicitly delete from child tables first, then the task row.
    This keeps the operation atomic (single ``write_txn``).

    **Live-claim guard.** A ``running`` task whose ``worker_pid`` is still
    alive is refused with :class:`TaskRunningError` unless ``force=True``.
    Without this, one dashboard click (or one REPL call) hard-deletes a
    dispatched worker's row out from under it: the worker keeps burning
    tokens against an id that no longer exists, every ``kanban_*`` call
    fails with a bare "unknown task", and the cascade into ``task_runs`` /
    ``task_events`` destroys the only record the run ever happened. The
    liveness check runs *inside* the write transaction so a concurrent
    claim can't slip in between the check and the delete.

    Note the guard is deliberately narrow — it keys on a *live* pid, so a
    crashed worker's stale claim stays deletable without ``force``. Prefer
    :func:`archive_task` for anything reachable from a UI: it clears the
    claim, closes the run as ``reclaimed``, and keeps the audit trail.

    Returns ``True`` if the task existed and was deleted, ``False``
    if the task was not found. Raises :class:`TaskRunningError` when the
    live-claim guard trips.
    """
    with write_txn(conn):
        if not force:
            row = conn.execute(
                "SELECT status, worker_pid, claim_lock, current_run_id "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if (
                row is not None
                and row["status"] == "running"
                and _pid_alive(row["worker_pid"])
            ):
                raise TaskRunningError(
                    task_id,
                    row["worker_pid"],
                    claim_lock=row["claim_lock"],
                    run_id=row["current_run_id"],
                )
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        _delete_task_relations(conn, task_id)
    recompute_ready(conn)
    return True


@dataclass(frozen=True)
class _WorkspaceAdmission:
    root: Path
    mount_path: Path


def _lexical_root_anchor(path: Path, root: Path) -> Optional[Path]:
    """The lexical ancestor of ``path`` that names ``root`` in any spelling.

    Walks ``path``'s own (unresolved) ancestors, so a symlinked alias is still
    visible to the caller's escape check. An ancestor matches when it is the
    same existing filesystem object as ``root`` (kernel resolves case, Unicode
    and firmlink aliases) or, when either side is missing (lost mount), when
    its spelling folds to the same string. Both only widen the set of rows
    that get full mount validation, so the fold fails closed.
    """
    target = _spelling_fold(root)
    root_identity = _path_identity(root)
    for ancestor in (path, *path.parents):
        if _spelling_fold(ancestor) == target:
            return ancestor
        if root_identity is not None and _path_identity(ancestor) == root_identity:
            return ancestor
    return None


def _recorded_root_alias(root: Path, roots: dict) -> Optional[Path]:
    """The recorded mount-root row that ``root`` aliases, if any."""
    if root in roots:
        return root
    for recorded in roots:
        if _spelling_fold(recorded) == _spelling_fold(root) or _same_path(recorded, root):
            return recorded
    return None


def _validate_workspace_admission(
    task: Task, *, board: Optional[str] = None, conn=None, dry_run=False,
) -> Optional[_WorkspaceAdmission]:
    from hermes_cli.kanban_workspace_policy import (
        WorkspaceUnavailable, configured_root, validate_mount,
        validate_persisted, validate_target,
    )

    root, require_mount = configured_root()
    if conn is None:
        with connect_closing(board=board) as owned:
            return _validate_workspace_admission(task, board=board, conn=owned, dry_run=dry_run)
    roots = {
        Path(row["root"]): Path(row["mount_path"])
        for row in conn.execute("SELECT root, mount_path FROM workspace_mount_roots")
    }
    if root is not None and require_mount:
        # A config root spelled differently from its recorded row (case, NFD,
        # firmlink) is still that row: keep its admitted mount anchor so an
        # unmounted root cannot fall through to a still-mounted parent.
        recorded = _recorded_root_alias(root, roots)
        if recorded is not None and recorded != root:
            raise WorkspaceUnavailable(
                f"workspaces_root_invalid: configured root {root} is an alias "
                f"of recorded root {recorded}; use the recorded spelling"
            )
        expected_mount = roots.get(root)
        mount_path = validate_mount(root, expected_mount=expected_mount)
        if expected_mount is None:
            roots[root] = mount_path
            if not dry_run:
                with write_txn(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_mount_roots(root, mount_path) "
                        "VALUES (?, ?)",
                        (str(root), str(mount_path)),
                    )

    def resolved(candidate: Path) -> Path:
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceUnavailable(
                f"workspaces_root_invalid: cannot resolve: {candidate}"
            ) from exc

    if task.workspace_path:
        path = Path(task.workspace_path).expanduser()
        for protected, mount_path in sorted(
            roots.items(), key=lambda item: len(item[0].parts), reverse=True,
        ):
            path_resolved = resolved(path)
            protected_resolved = resolved(protected)
            # Spelling-blind root match (t_800d50d2): Path.resolve() keeps
            # case, NFC/NFD and the /System/Volumes/Data firmlink spelling, so
            # a string compare alone lets an aliased row skip mount policy.
            anchor = _lexical_root_anchor(path, protected)
            if (
                anchor is not None
                or path_resolved.is_relative_to(protected_resolved)
                or _same_tree(path, protected)
            ):
                validate_mount(protected, expected_mount=mount_path)
                if anchor is None:
                    # Reaches the root only through a symlink.
                    raise WorkspaceUnavailable("workspaces_root_invalid: workspace symlink escape")
                # Re-spell the task path under the recorded root; every later
                # check then runs on the canonical spelling.
                canonical = protected.joinpath(*path.parts[len(anchor.parts):])
                if resolved(anchor) != anchor.absolute() or resolved(canonical) != canonical.absolute():
                    raise WorkspaceUnavailable("workspaces_root_invalid: workspace symlink escape")
                validate_target(protected, canonical)
                validate_persisted(canonical)
                return _WorkspaceAdmission(protected, mount_path)
    elif task.workspace_kind in (None, "scratch"):
        target = workspaces_root(board=board) / task.id
        if require_mount:
            assert root is not None
            validate_target(root, target)
            return _WorkspaceAdmission(root, roots[root])
    return None


def _stranded_event_payload(reason: str) -> dict:
    from hermes_cli.kanban_workspace_policy import STRANDED_RECOVERY_COMMAND

    payload = {"reason": reason}
    if reason.startswith("stranded_by_mount_loss:"):
        payload["recovery"] = STRANDED_RECOVERY_COMMAND
    return payload

# Refusals a retired root can cause: the old volume is gone/unwritable, or it is
# mounted but the card's tree is gone. ``workspaces_root_invalid`` (symlink
# escape, traversal, alias) is a policy fault and is never healed.
_RETIRED_ROOT_HEALABLE_REASONS = (
    "workspaces_root_unmounted:",
    "workspaces_root_unwritable:",
    "stranded_by_mount_loss:",
)


def _retired_scratch_root(conn, task: Task) -> Optional[Path]:
    """The recorded mount root a scratch card's path sits under, when that
    root is no longer the configured ``kanban.workspaces_root``; else None.

    A root drops out of config when the operator retires it (e.g.
    /Volumes/ramscratch, 2026-09-26) but its ``workspace_mount_roots`` row
    keeps fencing every card persisted under it, so the card is refused
    every tick forever. Only ``scratch`` qualifies: dir/worktree paths are
    operator-owned and must never be moved.
    """
    from hermes_cli.kanban_workspace_policy import (
        WorkspaceUnavailable, configured_root,
    )

    if (task.workspace_kind or "scratch") != "scratch" or not task.workspace_path:
        return None
    try:
        current, _required = configured_root()
    except WorkspaceUnavailable:
        return None  # config itself is broken; do not guess
    path = Path(task.workspace_path).expanduser()
    recorded = sorted(
        (Path(row["root"]) for row in conn.execute(
            "SELECT root FROM workspace_mount_roots")),
        key=lambda root: len(root.parts), reverse=True,
    )
    for root in recorded:
        if _lexical_root_anchor(path, root) is None:
            continue
        # Most specific recorded root wins (same rule as admission).
        if current is not None and _recorded_root_alias(current, {root: root}) is not None:
            return None
        return root
    return None


def _reallocate_retired_scratch_workspace(conn, task: Task, retired: Path, reason: str) -> bool:
    """Drop a scratch card's path under a retired root so the next admission
    places it under the current root. Scratch content is disposable."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET workspace_path = NULL WHERE id = ? "
            "AND workspace_path = ? AND status = ? "
            "AND COALESCE(workspace_kind, 'scratch') = 'scratch'",
            (task.id, task.workspace_path, task.status),
        )
        if not cur.rowcount:
            return False
        baseline = _reset_survivor_baseline(conn, task.id)
        _append_event(conn, task.id, "workspace_reallocated", {
            "actor": "dispatcher",
            "reason": reason,
            "retired_root": str(retired),
            "previous_path": task.workspace_path,
            "survivor_baseline": baseline,
        })
    add_comment(
        conn, task.id, "dispatcher",
        f"Workspace reallocated: {task.workspace_path} sits under retired "
        f"workspaces_root {retired} ({reason}). The dispatcher creates an EMPTY "
        "scratch workspace under the current root; resume from your remote branch/PR.",
    )
    return True


def _reset_survivor_baseline(conn, task_id: str) -> str:
    """Clear the record-once survivor baseline of a discarded scratch tree.

    The old ``bases`` describe repos in the discarded tree. A recorded
    survivor pointer / hold is evidence and is kept (``bases`` reset only).
    Caller holds the write transaction.
    """
    survivor = conn.execute(
        "SELECT held_reason, survivor FROM task_workspace_survivors WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if survivor is None:
        return "none"
    if survivor[0] is None and survivor[1] is None:
        conn.execute("DELETE FROM task_workspace_survivors WHERE task_id=?", (task_id,))
        return "cleared"
    conn.execute(
        "UPDATE task_workspace_survivors SET bases='{}' WHERE task_id=?",
        (task_id,),
    )
    return "bases_reset_survivor_kept"


def _workspace_admission_refused(conn, task_id, result, *, board, dry_run):
    from hermes_cli.kanban_workspace_policy import (
        STRANDED_RECOVERY_COMMAND, WorkspaceUnavailable, auto_unstrand_enabled,
    )

    task = get_task(conn, task_id)
    if task is None:
        return True
    try:
        _validate_workspace_admission(task, board=board, conn=conn, dry_run=dry_run)
    except WorkspaceUnavailable as exc:
        reason = str(exc)
        if (
            not dry_run
            and task.status in ("ready", "review")
            and reason.startswith(_RETIRED_ROOT_HEALABLE_REASONS)
        ):
            retired = _retired_scratch_root(conn, task)
            if retired is not None and _reallocate_retired_scratch_workspace(
                conn, task, retired, reason,
            ):
                _log.warning(
                    "kanban dispatch: workspace_reallocated task=%s "
                    "(retired root %s: %s)", task_id, retired, reason,
                )
                # Path is now NULL: re-admit against the CURRENT root.
                return _workspace_admission_refused(
                    conn, task_id, result, board=board, dry_run=dry_run,
                )
        if (
            not dry_run
            and reason.startswith("stranded_by_mount_loss:")
            and task.status in ("ready", "review")
            and auto_unstrand_enabled()
        ):
            evidence = _unstrand_evidence(conn, task_id)
            if evidence is not None:
                ok, _err = reset_stranded_workspace(
                    conn, task_id, actor="dispatcher",
                    reason=f"auto_unstrand: {evidence}", board=board,
                )
                if ok:
                    # Healed. From the startup scan, the ready loop in the same
                    # tick re-reads the card and recreates <root>/<board>/<id>;
                    # from the ready loop itself, the card spawns next tick.
                    _log.warning(
                        "kanban dispatch: auto-unstranded task=%s (%s)",
                        task_id, evidence,
                    )
                    return True
        # Only a spawnable lane refusal is a dispatcher fault. Startup
        # reconciliation also scans todo/running tasks so their lost path is
        # durable and visible, but must not make unrelated ready work look stuck.
        spawnable_lane = task.status in ("ready", "review")
        if spawnable_lane and not any(
            item[0] == task_id for item in result.workspace_refused
        ):
            result.workspace_refused.append((task_id, reason))
        stranded = bool(task.workspace_path) and reason.startswith((
            "stranded_by_mount_loss:", "workspaces_root_unmounted:",
        ))
        if stranded and task_id not in result.stranded_by_mount_loss:
            result.stranded_by_mount_loss.append(task_id)
        event_kind = "stranded_by_mount_loss" if stranded else "workspace_refused"
        if reason.startswith("stranded_by_mount_loss:"):
            _log.warning(
                "kanban dispatch: %s task=%s (recover: %s)",
                reason, task_id, STRANDED_RECOVERY_COMMAND,
            )
        else:
            _log.warning("kanban dispatch: %s task=%s", reason, task_id)
        if not dry_run:
            with write_txn(conn):
                # A ``workspace_reset`` ends the previous stranding episode:
                # the recreated path is byte-identical, so a second loss must
                # not be deduped against the first one's event.
                previous = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id=? "
                    "AND kind IN (?, 'workspace_reset') ORDER BY id DESC LIMIT 1",
                    (task_id, event_kind),
                ).fetchone()
                payload = _stranded_event_payload(reason)
                if (
                    previous is None
                    or previous[0] != event_kind
                    or json.loads(previous[1]) != payload
                ):
                    _append_event(conn, task_id, event_kind, payload)
        return True
    return False

# Statuses whose card may have its dead scratch path cleared. ``running`` has a
# live claim (reclaim first); ``done``/``archived`` never dispatch again.
_WORKSPACE_RESET_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "blocked", "review"}
)


def _unstrand_evidence(conn, task_id: str) -> Optional[str]:
    """Why the lost scratch tree cannot have held unrecoverable work, or None.

    Scratch cards carry no branch column, so "the work is on a remote (rule 0)"
    is only provable from the board itself: either no worker ever spawned into
    the tree, or a recorded survivor pointer names where its work landed.
    """
    spawned = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='spawned' LIMIT 1",
        (task_id,),
    ).fetchone()
    if spawned is None:
        return "never_spawned"
    survivor = conn.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if survivor is not None and survivor[0]:
        # A reset keeps the pointer, and the row carries no workspace
        # generation. Once a worker has spawned into a RECREATED workspace the
        # pointer describes the previous tree, not this one: it proves nothing
        # about the newer, possibly unpushed work (C6, #1037). Fail closed.
        respawned = conn.execute(
            "SELECT 1 FROM task_events s WHERE s.task_id=? AND s.kind='spawned' "
            "AND s.id > (SELECT COALESCE(MAX(r.id), 0) FROM task_events r "
            "WHERE r.task_id=? AND r.kind IN ('workspace_reset', 'workspace_reallocated')) "
            "AND EXISTS (SELECT 1 FROM task_events r WHERE r.task_id=? "
            "AND r.kind IN ('workspace_reset', 'workspace_reallocated')) LIMIT 1",
            (task_id, task_id, task_id),
        ).fetchone()
        if respawned is not None:
            return None
        return "survivor_recorded"
    return None


def stranded_workspace_candidates(conn: sqlite3.Connection) -> list[str]:
    """Scratch cards in a resettable status whose persisted path is gone."""
    rows = conn.execute(
        "SELECT id, workspace_path, status FROM tasks "
        "WHERE workspace_path IS NOT NULL AND workspace_path != '' "
        "AND COALESCE(workspace_kind, 'scratch') = 'scratch' ORDER BY id"
    ).fetchall()
    return [
        row["id"] for row in rows
        if row["status"] in _WORKSPACE_RESET_STATUSES
        and not os.path.lexists(Path(row["workspace_path"]).expanduser())
    ]


@_home_session_guarded("workspace")
def reset_stranded_workspace(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    board: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Clear a stranded scratch card's dead ``workspace_path``.

    The recovery half of mount-loss admission: ``validate_persisted`` refuses a
    vanished path forever (fail-closed, correct), so without this verb a
    stranded card needs hand-written SQL. Only the exact stranded state is
    reset: scratch kind, resettable status, path absent (``lexists``), and the
    dispatcher's own admission refusing with ``stranded_by_mount_loss`` — which
    it only reaches after the root passed ``validate_mount``, i.e. the volume
    is mounted and writable again, so the old path is not coming back. With
    the root still unmounted the path may reappear on remount, so we refuse.

    The survivor baseline is cleared too: ``record_baseline`` is record-once
    and the old ``bases`` describe repos in the destroyed tree. A recorded
    survivor pointer / hold is evidence and is kept (``bases`` reset only).

    Returns ``(True, None)`` on success, ``(False, reason)`` if refused.
    """
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

    task = get_task(conn, task_id)
    if task is None:
        return False, f"task {task_id} not found"
    kind = task.workspace_kind or "scratch"
    if kind != "scratch":
        return False, (
            f"task {task_id} is {kind}-kind; reset only applies to scratch "
            f"workspaces (a {kind} path is operator-owned; restore it or edit the card)"
        )
    if not task.workspace_path:
        return False, f"task {task_id} has no persisted workspace_path; nothing to reset"
    if task.status not in _WORKSPACE_RESET_STATUSES:
        hint = " (reclaim it first)" if task.status == "running" else ""
        return False, f"task {task_id} is {task.status!r}; cannot reset its workspace{hint}"
    path = Path(task.workspace_path).expanduser()
    if os.path.lexists(path):
        return False, f"task {task_id} workspace {path} still exists; nothing is stranded"
    try:
        _validate_workspace_admission(task, board=board, conn=conn, dry_run=True)
    except WorkspaceUnavailable as exc:
        refusal = str(exc)
        if not refusal.startswith("stranded_by_mount_loss:"):
            return False, (
                f"task {task_id} is refused as {refusal}; fix the workspace root "
                "first (a remount may bring the path back)"
            )
    else:
        return False, (
            f"task {task_id} is not stranded: its path is outside every guarded "
            "root, so the dispatcher recreates it itself"
        )
    if dry_run:
        return True, None
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET workspace_path = NULL WHERE id = ? "
            "AND workspace_path = ? AND status = ?",
            (task_id, task.workspace_path, task.status),
        )
        if not cur.rowcount:
            return False, f"task {task_id} changed during reset; retry"
        baseline = _reset_survivor_baseline(conn, task_id)
        _append_event(conn, task_id, "workspace_reset", {
            "actor": actor,
            "reason": reason or "stranded_by_mount_loss",
            "previous_path": task.workspace_path,
            "survivor_baseline": baseline,
        })
    add_comment(
        conn, task_id, actor,
        f"Workspace reset: {task.workspace_path} was lost with its volume "
        "(stranded_by_mount_loss). The dispatcher recreates an EMPTY scratch "
        "workspace on the next tick; resume from your remote branch/PR.",
    )
    return True, None
_REFUSAL_EVENT_KINDS = ("workspace_refused", "stranded_by_mount_loss")
_REFUSAL_CLEARING_KINDS = ("claimed", "spawned", "workspace_reset", "workspace_reallocated")


def workspace_refusal_state(conn, task_ids) -> dict[str, dict]:
    """Open workspace-admission refusal episodes, keyed by task id.

    A card the dispatcher refuses every tick stays ``ready`` in the status
    column, which reads as "waiting for a slot". This names the refusal so
    ``kanban show``/``list`` can say so. An episode is open from the first
    refusal event after the last claim/spawn/reset/reallocation and carries
    ``{"reason", "since", "last"}``.
    """
    ids = [tid for tid in task_ids if tid]
    if not ids:
        return {}
    kinds = _REFUSAL_EVENT_KINDS + _REFUSAL_CLEARING_KINDS
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            f"SELECT task_id, kind, payload, created_at FROM task_events "
            f"WHERE task_id IN ({','.join('?' * len(chunk))}) "
            f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id",
            (*chunk, *kinds),
        ).fetchall()
        for task_id, kind, payload, created_at in rows:
            if kind in _REFUSAL_CLEARING_KINDS:
                out.pop(task_id, None)
                continue
            try:
                reason = (json.loads(payload) or {}).get("reason") or kind
            except (TypeError, ValueError):
                reason = kind
            state = out.setdefault(task_id, {"since": created_at})
            state["reason"] = str(reason)
            state["last"] = created_at
    return out

# Durable post-on-change latch for the workspace-refusal #alerts page.
_REFUSAL_PAGED_KIND = "workspace_refusal_paged"


def claim_workspace_refusal_pages(conn, refused, *, channel: str = "alerts"):
    """Claim the (card, reason) refusals that have not been paged yet.

    The watcher used to latch in memory per board and re-arm on any tick
    whose refused list was empty. A card only reaches admission after the
    cap / respawn-guard / provider gates, so a tick that skipped it for one
    of those reasons looked healthy, re-armed the latch, and the next refused
    tick paged again; every gateway restart and every failover dispatcher
    re-paged too (t_ff4197d3: 5 pages in 28 min on 2026-09-27, t_ca81dfe2).
    The latch now lives on the card: one page per (card, reason) per refusal
    episode. An episode ends on any ``_REFUSAL_CLEARING_KINDS`` event, so a
    card that is admitted and later refused again pages again, and so does a
    new reason inside one episode.

    Returns ``[(task_id, reason, event_id)]`` for the pairs claimed now. The
    caller pages them and passes the ids to
    :func:`release_workspace_refusal_pages` when delivery fails.
    """
    kinds = _REFUSAL_CLEARING_KINDS + (_REFUSAL_PAGED_KIND,)
    marks = ",".join("?" * len(kinds))
    claimed = []
    with write_txn(conn):
        for task_id, reason in refused or []:
            task_id, reason = str(task_id), str(reason)
            already = False
            for kind, payload in conn.execute(
                f"SELECT kind, payload FROM task_events WHERE task_id=? "
                f"AND kind IN ({marks}) ORDER BY id DESC",
                (task_id, *kinds),
            ):
                if kind != _REFUSAL_PAGED_KIND:
                    break  # episode boundary
                try:
                    data = json.loads(payload) or {}
                except (TypeError, ValueError):
                    data = {}
                if data.get("channel") == channel and data.get("reason") == reason:
                    already = True
                    break
            if already:
                continue
            _append_event(
                conn, task_id, _REFUSAL_PAGED_KIND,
                {"channel": channel, "reason": reason},
            )
            event_id = conn.execute(
                "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?",
                (task_id, _REFUSAL_PAGED_KIND),
            ).fetchone()[0]
            claimed.append((task_id, reason, event_id))
    return claimed


def release_workspace_refusal_pages(conn, event_ids) -> None:
    """Undo claims whose page was not delivered, so the next tick retries."""
    ids = [int(event_id) for event_id in event_ids or [] if event_id is not None]
    if not ids:
        return
    with write_txn(conn):
        conn.execute(
            f"DELETE FROM task_events WHERE kind=? "
            f"AND id IN ({','.join('?' * len(ids))})",
            (_REFUSAL_PAGED_KIND, *ids),
        )


def _release_claim_for_workspace_refusal(conn, task_id, result, reason):
    """Undo a claim when the mount changes during the claim/resolve window."""
    task = get_task(conn, task_id)
    if task is None:
        return
    if not any(item[0] == task_id for item in result.workspace_refused):
        result.workspace_refused.append((task_id, reason))
    stranded = bool(task.workspace_path) and reason.startswith((
        "stranded_by_mount_loss:", "workspaces_root_unmounted:",
    ))
    if stranded and task_id not in result.stranded_by_mount_loss:
        result.stranded_by_mount_loss.append(task_id)
    with write_txn(conn):
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        run_id = row["current_run_id"] if row else None
        retry_status = _retry_status_for_run(conn, task_id, run_id)
        conn.execute(
            "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=? AND current_run_id=?",
            (retry_status, task_id, run_id),
        )
        closed_run_id = _end_run(
            conn, task_id, outcome="workspace_refused",
            status="workspace_refused", error=reason[:500],
            metadata={"retry_status": retry_status},
        )
        _append_event(
            conn, task_id,
            "stranded_by_mount_loss" if stranded else "workspace_refused",
            _stranded_event_payload(reason), run_id=closed_run_id,
        )


@_home_session_guarded("set-model")
def set_task_model(
    conn: sqlite3.Connection, task_id: str, model: Optional[str]
) -> int:
    """Set (or clear) a task's per-task model override.

    ``model`` is taken literally APART from alias resolution: a config
    ``model.aliases`` key (or a ``provider/model`` pair) is resolved to its
    concrete target first — see :func:`_resolve_stored_model_pair` — and
    anything that does not resolve is stored verbatim. ``None`` writes SQL
    NULL (clears the override). The DB layer does NOT interpret ``""`` —
    empty-string handling is a CLI concern.

    This setter always writes BOTH columns in one statement, exactly like
    :func:`set_model_override`. Writing ``model_override`` alone would leave
    whatever ``provider_override`` the PREVIOUS model was pinned with, so
    ``edit --model claude-opus-5`` on a card pinned to ``xai-oauth`` would
    spawn that model against xAI — the exact mismatch this resolution exists
    to prevent. It is also an illegal card state: every other writer here
    rejects a ``provider_override`` without a ``model_override``.

    Returns the number of rows affected: a call against a nonexistent
    ``task_id`` returns ``0`` (never a silent success), so callers can tell
    a real write from a no-op.
    """
    from hermes_cli.model_policy import validate_route_provider, validate_worker_model

    validate_route_provider(model, None)
    resolved_model, resolved_provider = _resolve_stored_model_pair(model, None)
    validate_route_provider(resolved_model, resolved_provider)
    validate_worker_model(resolved_model)
    if not resolved_model:
        resolved_provider = None
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET model_override = ?, provider_override = ?, "
            "pin_sub_reason = NULL, pin_sub_fallback = 0 WHERE id = ?",
            (resolved_model, resolved_provider, task_id),
        )
    return int(cur.rowcount or 0)



def set_task_skills(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    add: Iterable[str] = (),
    clear: bool = False,
    operator: Optional[str] = None,
) -> Optional[list[str]]:
    """Edit a card's force-loaded skills: ``clear`` empties the list first,
    then ``add`` names are appended (validated + deduped like ``create
    --skill``). Returns the new list, or ``None`` for an unknown id.

    A change records ``skills_set`` ``{before, after, operator}``; a no-op
    edit writes nothing. Takes effect on the card's next spawn.
    """
    added = _normalize_skills(add)
    with write_txn(conn):
        row = conn.execute(
            "SELECT skills FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        before: list[str] = []
        if row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    before = [str(x) for x in parsed if x]
            except Exception:
                before = []
        after = [] if clear else list(before)
        after += [n for n in added if n not in after]
        if after != before:
            conn.execute(
                "UPDATE tasks SET skills = ? WHERE id = ?",
                (json.dumps(after) if after else None, task_id),
            )
            _append_event(
                conn, task_id, "skills_set",
                {"before": before, "after": after, "operator": operator},
            )
    if after != before:
        notify_task_updated(conn, task_id, ("skills",))
    return after

@_home_session_guarded("priority")
def set_task_priority(
    conn: sqlite3.Connection,
    task_id: str,
    priority: int,
    *,
    actor: Optional[str] = None,
) -> tuple[bool, Optional[int]]:
    """Set a card's dispatch priority (higher dispatches first).

    Returns ``(ok, old_priority)``; ``(False, None)`` for an unknown id (a
    tuple so the home-session guard reads success from ``ok``, not from an
    old priority of 0). A change records a ``priority_set`` event
    ``{old, new, actor}``; setting the current value is a silent no-op.
    """
    new = int(priority)
    with write_txn(conn):
        row = conn.execute(
            "SELECT priority FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False, None
        old = int(row["priority"] or 0)
        if old == new:
            return True, old
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?", (new, task_id)
        )
        payload: dict = {"old": old, "new": new}
        if actor:
            payload["actor"] = actor
        _append_event(conn, task_id, "priority_set", payload)
    notify_task_updated(conn, task_id, ("priority",))
    return True, old


# ---------------------------------------------------------------------------
@_home_session_guarded("schedule")
def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until ``unblock_task`` re-gates it."""
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   -- A leftover rate-limit cooldown would read as a timed wake
                   -- to wake_due_scheduled; set_schedule_wake stamps a real one.
                   next_eligible_at = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True

# Scheduled cards with no timed wake (``next_eligible_at IS NULL``) wait on a
# named event only a human/automation will act on. Past this age they are
# reported every dispatch tick instead of rotting silently (t_6915068e).
SCHEDULED_UNWOKEN_THRESHOLD_SECONDS = 24 * 3600


@_home_session_guarded("schedule")
def set_schedule_wake(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    wake_at: int,
    actor: str,
    reason: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Give a ``scheduled`` card a timed wake: the dispatcher returns it to
    ``ready`` (``todo`` while parents are open) on its first tick at or after
    ``wake_at`` via :func:`wake_due_scheduled`.

    Before this there was no timed exit from ``scheduled``: a card parked on
    a date sat there until someone remembered to ``unblock`` it. The stamp
    lives in ``next_eligible_at`` (the dispatcher's existing eligibility
    column); ``unblock_task`` clears it when the card leaves ``scheduled``.

    Returns ``(True, None)`` on success, ``(False, reason)`` if refused.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False, f"task {task_id} not found"
        if row["status"] != "scheduled":
            return False, (
                f"task {task_id} is {row['status']!r}; a wake time only "
                f"applies to 'scheduled' tasks"
            )
        cur = conn.execute(
            "UPDATE tasks SET next_eligible_at = ? "
            "WHERE id = ? AND status = 'scheduled'",
            (int(wake_at), task_id),
        )
        if cur.rowcount != 1:
            return False, f"task {task_id} changed state concurrently; retry"
        _append_event(
            conn, task_id, "schedule_wake_set",
            {"actor": actor, "reason": reason, "wake_at": int(wake_at)},
        )
    return True, None


def wake_due_scheduled(
    conn: sqlite3.Connection, *, now: Optional[int] = None,
) -> list[str]:
    """Wake every ``scheduled`` card whose timed wake has passed.

    Each card goes through :func:`unblock_task` (same parent re-gate, same
    ``unblocked`` event, same stale-run recovery as the operator verb), then
    gets a ``schedule_elapsed`` event naming the wake time so the audit trail
    says WHY it moved. Cards with ``next_eligible_at IS NULL`` are never
    touched: they wait on an event, and :func:`find_unwoken_scheduled`
    reports them instead. Returns the woken ids.
    """
    now = int(time.time()) if now is None else int(now)
    due = conn.execute(
        "SELECT id, next_eligible_at FROM tasks "
        "WHERE status = 'scheduled' AND next_eligible_at IS NOT NULL "
        "AND next_eligible_at <= ? ORDER BY next_eligible_at, id",
        (now,),
    ).fetchall()
    woken: list[str] = []
    for row in due:
        if not unblock_task(conn, row["id"]):
            continue
        with write_txn(conn):
            _append_event(
                conn, row["id"], "schedule_elapsed",
                {"wake_at": int(row["next_eligible_at"]), "woken_at": now},
            )
        woken.append(row["id"])
    return woken


def find_unwoken_scheduled(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    threshold_seconds: int = SCHEDULED_UNWOKEN_THRESHOLD_SECONDS,
) -> list[tuple[str, int]]:
    """Return ``(task_id, parked_seconds)`` for ``scheduled`` cards with NO
    timed wake that have been parked at least ``threshold_seconds``.

    Such a card waits on a named event in its body; nothing in the dispatcher
    will ever move it, so without this read it rots silently (t_dab7ed4e sat
    32h past its gate on 2026-09-26). Parked-since is the latest ``scheduled``
    event, falling back to ``created_at``. Pure read; ordered oldest first.
    """
    now = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        "SELECT t.id AS id, COALESCE(("
        "  SELECT MAX(e.created_at) FROM task_events e "
        "  WHERE e.task_id = t.id AND e.kind = 'scheduled'"
        "), t.created_at) AS parked_at "
        "FROM tasks t "
        "WHERE t.status = 'scheduled' AND t.next_eligible_at IS NULL",
    ).fetchall()
    out = [
        (r["id"], now - int(r["parked_at"] or now))
        for r in rows
        if now - int(r["parked_at"] or now) >= int(threshold_seconds)
    ]
    out.sort(key=lambda item: (-item[1], item[0]))
    return out


def format_unwoken_scheduled(entries) -> str:
    """One operator-facing line (plus per-card lines) for
    :func:`find_unwoken_scheduled` output; ``""`` when there is nothing.
    Shared by the CLI dispatch report and the gateway dispatcher warning."""
    entries = list(entries or [])
    if not entries:
        return ""
    lines = [
        f"STRANDED: {len(entries)} scheduled task(s) have no timed wake "
        f"(next_eligible_at NULL) and have been parked >= "
        f"{SCHEDULED_UNWOKEN_THRESHOLD_SECONDS // 3600}h — nothing will "
        f"wake them:"
    ]
    for tid, age in entries:
        lines.append(f"  - {tid} parked {int(age) // 3600}h")
    lines.append(
        "  wake with: hermes kanban unblock <id>  |  "
        "hermes kanban schedule <id> --now|--at TS"
    )
    return "\n".join(lines)

# A blocked card with a recent handoff can still own unmerged files. Keep it
# in the pre-dispatch collision scan for one week; older blocked cards are too
# stale to be useful collision evidence and create false-positive noise.
DISPATCH_COLLISION_RECENT_BLOCKED_SECONDS = 7 * 24 * 60 * 60
_DISPATCH_CHANGED_FILES_KEY_RE = re.compile(r"[\"']changed_files[\"']\s*:")
_DISPATCH_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_DISPATCH_PATH_TOKEN_RE = re.compile(
    r"(?<![\w@])(?:~?/|\.{1,2}/|(?:[A-Za-z0-9_.-]+/)+)"
    r"[A-Za-z0-9_.@%+=~/-]+(?::\d+(?::\d+)?)?"
)
_DISPATCH_BARE_FILE_RE = re.compile(
    r"(?<![\w./-])[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9_-]*"
    r"(?::\d+(?::\d+)?)?"
)
_DISPATCH_EXTENSIONLESS_PATH_ROOTS = {
    "app", "apps", "bin", "config", "configs", "docs", "gateway",
    "hermes_cli", "lib", "packages", "plugins", "scripts", "skills-shared",
    "src", "templates", "tests", "tools", "website",
}

# Escalating hold after CONSECUTIVE rate-limited runs. The flat cooldown above
# never escalated, so a card re-probed an exhausted pool every 5 minutes for
# hours (measured 2026-09-22 over 7d: 3,706 rate_limited runs, zero inter-retry
# gaps below 304s, 1,538 clustered at the floor; worst card retried 128x).
# Streak N holds for ladder[N-1], capped at the last rung; the configured
# cooldown stays the floor. Any non-rate_limited run resets the streak.
RATE_LIMIT_BACKOFF_LADDER: tuple[int, ...] = (300, 900, 2700, 7200)  # 5m 15m 45m 2h

# Board-wide circuit: >= trip rate_limited closes inside the window hold every
# pool-bound spawn for one further window. The pool is down for everyone, so
# per-card backoff alone still lets N cards each spend a probe.
DEFAULT_RATE_LIMIT_TRIP = 5
RATE_LIMIT_TRIP_WINDOW_SECONDS = 600  # 10 minutes
_RATE_LIMIT_CIRCUIT_MARKER = ".rate_limit_circuit.json"


def _rate_limit_hold_seconds(streak: int, *, base: int) -> int:
    """Seconds to hold a card after ``streak`` consecutive rate-limited runs."""
    if base <= 0:
        return 0  # operator disabled the cooldown entirely
    if streak <= 1:
        return base
    rung = RATE_LIMIT_BACKOFF_LADDER[min(streak, len(RATE_LIMIT_BACKOFF_LADDER)) - 1]
    return max(base, rung)


def consecutive_rate_limited_runs(conn: sqlite3.Connection, task_id: str) -> int:
    """Trailing count of CLOSED runs whose outcome is ``rate_limited``."""
    streak = 0
    for r in conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT ?",
        (task_id, len(RATE_LIMIT_BACKOFF_LADDER) + 1),
    ):
        if r["outcome"] != "rate_limited":
            break
        streak += 1
    return streak


def _resolve_rate_limit_trip() -> int:
    """``kanban.rate_limit_trip`` (0 disables the circuit)."""
    try:
        from hermes_cli.config import load_config
        value = int(load_config().get("kanban", {}).get("rate_limit_trip", DEFAULT_RATE_LIMIT_TRIP))
        return value if value >= 0 else DEFAULT_RATE_LIMIT_TRIP
    except Exception:
        return DEFAULT_RATE_LIMIT_TRIP


def rate_limit_circuits(
    conn: sqlite3.Connection, *, now: int, trip: int,
    window: int = RATE_LIMIT_TRIP_WINDOW_SECONDS,
) -> dict[str, int]:
    """``{pool_key: open_until}`` for every pool whose circuit is open now.

    Only closes whose route is pool-bound count, grouped by the pool that
    served them (``kanban_provider_health.pool_key``: relay family, or the one
    sub a pinned lane hits) -- five openai-codex 429s are not claude-pool
    evidence, and a bpr storm says nothing about apr (Argus r1, PR #953).
    A close is charged to the pool that SERVED the run: a dispatch-time
    route change (capped-pool fallback rung, lane override) is recorded as a
    run-scoped ``dispatch_provider_fallback`` / ``dispatch_lane_route`` event
    and wins, and a worker-side swap (``worker_route_substituted``, written by
    the worker after spawn) wins over both; otherwise the route is re-derived
    from the card's pin + the run's profile (Argus r2 G1: fallback-rung 429s charged to the primary).

    Derived from ``task_runs`` alone (no in-memory latch), so it survives a
    gateway restart: a pool trips at the latest close that completes ``trip``
    of its rate-limited closes within ``window`` and holds for ``window``.
    """
    if trip <= 0:
        return {}
    from types import SimpleNamespace
    from hermes_cli.kanban_provider_health import effective_provider, pool_key

    rows = conn.execute(
        "SELECT r.id, r.ended_at, r.profile, t.assignee, t.model_override, t.provider_override "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.outcome = 'rate_limited' AND r.ended_at IS NOT NULL AND r.ended_at >= ? "
        "ORDER BY r.ended_at",
        (now - 2 * window,),
    ).fetchall()
    served: dict[int, str] = {}
    served_pool: dict[int, str] = {}
    run_ids = [int(r["id"]) for r in rows]
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        for ev in conn.execute(
            "SELECT run_id, kind, payload FROM task_events WHERE run_id IN ("
            + ",".join("?" * len(chunk)) + ") AND kind IN "
            "('dispatch_lane_route', 'dispatch_provider_fallback', "
            "'worker_route_substituted', 'spawned') ORDER BY id",
            chunk,
        ):
            try:
                payload = json.loads(ev["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if ev["kind"] == "spawned":
                # The pool this spawn was charged to, recorded AT spawn time
                # (t_38be6b10): immune to a later repin of the card (C6, #953).
                if isinstance(payload.get("pool"), str) and payload["pool"]:
                    served_pool[int(ev["run_id"])] = payload["pool"]
                    served.pop(int(ev["run_id"]), None)
                continue
            field = "provider" if ev["kind"] == "dispatch_lane_route" else "to_provider"
            if isinstance(payload.get(field), str):
                served[int(ev["run_id"])] = payload[field]  # later event wins
                served_pool.pop(int(ev["run_id"]), None)
    keys: dict[tuple, Optional[str]] = {}
    by_pool: dict[str, list[int]] = {}
    for r in rows:
        if int(r["id"]) in served_pool:
            by_pool.setdefault(served_pool[int(r["id"])], []).append(int(r["ended_at"]))
            continue
        if int(r["id"]) in served:
            route = (None, "served", served[int(r["id"])])
        else:
            # Legacy run with no recorded route: re-derived from the card's
            # CURRENT pin, which a later repin can have changed.
            route = (r["profile"] or r["assignee"], r["model_override"], r["provider_override"])
        if route not in keys:
            keys[route] = pool_key(effective_provider(SimpleNamespace(
                assignee=route[0], model_override=route[1], provider_override=route[2],
            )))
        if keys[route] is not None:
            by_pool.setdefault(keys[route], []).append(int(r["ended_at"]))
    open_until: dict[str, int] = {}
    for key, ends in by_pool.items():
        tripped_at = None
        for i in range(trip - 1, len(ends)):
            if ends[i] - ends[i - trip + 1] <= window:
                tripped_at = ends[i]
        if tripped_at is not None and now < tripped_at + window:
            open_until[key] = tripped_at + window
    return open_until

# A worker that refused its route because the provider's credential is rate
# limited (``worker_route_pin_refused`` with ``rate_limited: true``, e.g.
# "Codex credential is in cooldown") is proof the credential is dead for every
# card, not just that one. Treat the provider as capped for this long after the
# latest refusal, so the capped-pool fallback does not spawn more workers into
# it (t_6445986b: a P0 card hit exit 75 three times in 25 min, each one
# stamping a longer rate_limit backoff). ``kanban.credential_cooldown_seconds``.
DEFAULT_CREDENTIAL_COOLDOWN_SECONDS = 1800  # 30 minutes


def _resolve_credential_cooldown() -> int:
    """``kanban.credential_cooldown_seconds`` (0 disables)."""
    try:
        from hermes_cli.config import load_config
        value = int(load_config().get("kanban", {}).get(
            "credential_cooldown_seconds", DEFAULT_CREDENTIAL_COOLDOWN_SECONDS))
        return value if value >= 0 else DEFAULT_CREDENTIAL_COOLDOWN_SECONDS
    except Exception:
        return DEFAULT_CREDENTIAL_COOLDOWN_SECONDS


def cooling_providers(
    conn: sqlite3.Connection, *, now: int, window: int,
) -> dict[str, int]:
    """``{provider: cooling_until}`` for providers with a recent rate-limited refusal.

    Keyed by provider name (lower case), not ``pool_key``: ``openai-codex`` is
    not pool-bound, so the rate-limit circuit can never see it. The events are
    run-scoped, so only runs still open or closed inside ``window`` are read
    (``idx_events_run``), never the whole event table.
    """
    if window <= 0:
        return {}
    from hermes_cli.kanban_worker_route import WORKER_ROUTE_PIN_REFUSED_EVENT

    run_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM task_runs WHERE ended_at IS NULL OR ended_at >= ?",
        (now - window,),
    )]
    until: dict[str, int] = {}
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        for ev in conn.execute(
            "SELECT payload, created_at FROM task_events WHERE run_id IN ("
            + ",".join("?" * len(chunk)) + ") AND kind = ? AND created_at >= ?",
            (*chunk, WORKER_ROUTE_PIN_REFUSED_EVENT, now - window),
        ):
            try:
                payload = json.loads(ev["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("rate_limited") is not True:
                continue
            # A runtime-stage refusal is one mid-turn 429 on a pinned (often
            # pooled) provider; the worker keeps retrying it. Only an auth-stage
            # refusal (the provider was unusable at startup) cools the provider
            # (FleetReview #1198). Legacy events without a stage were auth.
            if payload.get("stage", "auth") != "auth":
                continue
            provider = payload.get("provider")
            if not isinstance(provider, str) or not provider.strip():
                continue
            key = provider.strip().lower()
            until[key] = max(until.get(key, 0), int(ev["created_at"]) + window)
    return until


def _format_skipped_rungs(skipped) -> str:
    """``openai-codex:credential_cooldown, claude-apr:pool_budget``."""
    return ", ".join(f"{s.get('provider')}:{s.get('reason')}" for s in skipped or ())


def _notify_rate_limit_circuit(
    board: Optional[str], pool: str, until: int, trip: int, *, now: Optional[int] = None,
) -> None:
    """Post ONE #logs line per pool circuit EPISODE (marker file = latch).

    Workers already running when a pool trips keep dying at 429 during the
    hold, and every such close pushes ``until`` forward. That extends the same
    episode: the latch is "the recorded hold for this pool has not lapsed yet",
    never the exact ``until`` value (Argus F2: 4 notifies for one episode).
    """
    now = int(time.time()) if now is None else int(now)
    try:
        from hermes_cli import kanban_budget as _kbudget

        marker = board_state_dir(board) / _RATE_LIMIT_CIRCUIT_MARKER
        try:
            seen = json.loads(marker.read_text(encoding="utf-8-sig"))
        except Exception:
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        prior = seen.get(pool)
        same_episode = isinstance(prior, (int, float)) and now < int(prior)
        if same_episode and int(prior) >= until:
            return
        seen[pool] = max(int(prior), until) if same_episode else until

        def _latch() -> None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(seen), encoding="utf-8")

        if same_episode:
            _latch()
            return  # hold extended; already announced
        script = _kbudget._notify_script_path()
        if script is None:
            _latch()
            return
        import sys as _sys
        body = (
            f"🧯 Kanban '{board or 'default'}': rate-limit circuit OPEN for {pool} — "
            f">= {trip} workers hit 429 within {RATE_LIMIT_TRIP_WINDOW_SECONDS // 60} min. "
            f"Holding {pool} spawns until "
            f"{time.strftime('%H:%M:%S', time.localtime(until))}; other pools unaffected."
        )
        delivered = _kbudget._run_notify([
            _sys.executable, script, "--channel", "discord",
            "--target", _kbudget.RECOVERY_TARGET, "--send", body,
        ])
        # Latch the episode only once the page went out: a failed send
        # must be retried on the next tick, not silently swallowed (C7 k105).
        if delivered is not False:
            _latch()
    except Exception as exc:
        _log.warning("kanban rate-limit circuit notify failed (%s: %s)", type(exc).__name__, exc)

# Event kinds that mean "an OPERATOR deliberately asked for this task to run
# again". An unused event strictly AFTER the newest PR comment overrides
# ``active_pr`` for one spawn: the open PR is the fix-round target.
# This is the TOTAL set of operator requeue verbs -- every CLI verb that moves a
# task back toward ``ready`` by human intent must appear here, and
# ``tests/hermes_cli/test_kanban_db.py::test_operator_requeue_verbs_all_override_active_pr``
# drives each verb for real and fails if one is missing. Automatic
# transitions (dependency promotion, generic status writes, crash reclaim)
# deliberately stay OUT: they carry no intent. 2026-09-08: ``triage_resolved``
# (block-loop -> triage -> ``triage-resolve --to todo``) and ``reopened``
# (``reopen``) were missing after #653 and stranded t_e0c917fc for 6 minutes
# behind its own checkpoint draft PR (respawn_guarded:active_pr every tick).
_RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS: tuple[str, ...] = (
    "changes_requested",
    "unblocked",
    # Historical: the reopen-review verb is retired and no longer emits it,
    # but legacy rows still mark an operator requeue.
    "review_reopened",
    "triage_resolved",
    "reopened",
    # ``kanban requeue`` — the operator verb for a READY card (2026-09-23,
    # t_7d7ff489): unblock/reopen/triage-resolve all need a non-ready source
    # state, so a guarded ready card had no verb short of block->unblock.
    "requeued",
)

# Event kinds that make a stamped failure STALE for the ``blocker_auth`` and
# ``rate_limit_cooldown`` rules when they land at/after the failing run ended:
# the operator-intent verbs above, the requeue events ``recent_success``
# already honors (status / promoted / unblocked / reclaimed), and the two
# LANE-CHANGE verbs (reassign, set-model) — a quota wall belongs to the
# provider lane that hit it, not to the task. 2026-09-09, hermes-fork
# t_53316ee6: a 429 stamped that morning kept deferring the card as
# ``blocker_auth`` after a review reopen until the column was NULLed by hand;
# a set-model to a pool with free seats still sat out the full cooldown.
_RESPAWN_GUARD_FAILURE_RESET_KINDS: tuple[str, ...] = (
    *_RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS,
    "status",
    "promoted",
    "reclaimed",
    "assigned",
    "model_override_set",
)

# Run outcomes that stamp ``tasks.last_failure_error``. A newer run with any
# other outcome (completed / review_requested / changes_requested / blocked /
# reclaimed / stale ...) supersedes the stamped text: it belongs to history.
_RESPAWN_GUARD_FAILURE_OUTCOMES: frozenset[str] = frozenset(
    {"crashed", "timed_out", "spawn_failed", "gave_up", "rate_limited",
     "infra_unavailable"}
)
_RESPAWN_GUARD_PR_QUERY_LIMIT = 5
_RESPAWN_GUARD_PR_QUERY_TIMEOUT_SECONDS = 5
_RESPAWN_GUARD_PR_TERMINAL_CACHE_LIMIT = 1024
_RESPAWN_GUARD_PR_NONTERMINAL_CACHE_LIMIT = 1024
_RESPAWN_GUARD_PR_BOARD_CACHE_LIMIT = 32
_RESPAWN_GUARD_PR_BOARD_CACHE_RETENTION_SECONDS = 7 * 86400

# Bounded process-lifetime cache for PR states the respawn-guard policy treats
# as terminal. MERGED is irreversible; CLOSED is intentionally cached too even
# though GitHub permits reopening, so a reopened PR is not observed until its
# entry is evicted or the dispatcher restarts. This tradeoff prevents completed
# PR history from repeatedly consuming the bounded query budget.
_PR_TERMINAL_STATE_CACHE: dict[tuple[str, int], str] = {}

# Bounded per-board fairness state for nonterminal PRs. A completed query cycle
# clears the skip set; while a cycle is budget-starved, prior OPEN/unknown
# results are reused fail-safe so later PRs receive the next tick's query budget.
_PR_NONTERMINAL_STATE_CACHES: dict[
    str, dict[tuple[str, int], Optional[str]]
] = {}
_PR_QUERY_CYCLE_SKIPS: dict[str, set[tuple[str, int]]] = {}
_PR_BOARD_CACHE_LAST_SEEN: dict[str, float] = {}


def _pr_state_cache_key(db_path: Path) -> str:
    return str(db_path)


def _pr_state_swap_path(cache_key: str) -> Path:
    db_path = Path(cache_key)
    return db_path.with_name(f".{db_path.name}.pr-state-cache.json")


def _invalidate_pr_state_cache(cache_key: str) -> None:
    """Discard all persistent resolver state for one board."""
    _PR_NONTERMINAL_STATE_CACHES.pop(cache_key, None)
    _PR_QUERY_CYCLE_SKIPS.pop(cache_key, None)
    _PR_BOARD_CACHE_LAST_SEEN.pop(cache_key, None)
    try:
        _pr_state_swap_path(cache_key).unlink()
    except OSError:
        pass


def _store_pr_state_cache(
    cache_key: str,
    nonterminal_cache: dict[tuple[str, int], Optional[str]],
    cycle_skip: set[tuple[str, int]],
) -> bool:
    """Externalize an evicted active board's fairness state."""
    db_path = Path(cache_key)
    if not db_path.exists():
        return True
    swap_path = _pr_state_swap_path(cache_key)
    tmp_path = swap_path.with_name(
        f"{swap_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "nonterminal": [
            [repo, number, state]
            for (repo, number), state in nonterminal_cache.items()
        ],
        "cycle_skip": [[repo, number] for repo, number in cycle_skip],
    }
    try:
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp_path, swap_path)
        return True
    except OSError:
        _log.debug(
            "kanban PR-state cache spill failed for %s",
            cache_key,
            exc_info=True,
        )
        return False
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _load_pr_state_cache(
    cache_key: str,
) -> tuple[dict[tuple[str, int], Optional[str]], set[tuple[str, int]]]:
    """Restore fairness state externalized by board-cache eviction."""
    swap_path = _pr_state_swap_path(cache_key)
    if not swap_path.exists():
        return {}, set()
    try:
        if (
            time.time() - swap_path.stat().st_mtime
            > _RESPAWN_GUARD_PR_BOARD_CACHE_RETENTION_SECONDS
        ):
            return {}, set()
        payload = json.loads(swap_path.read_text(encoding="utf-8-sig"))
        nonterminal_cache = {
            (str(repo), int(number)): state
            for repo, number, state in payload.get("nonterminal", [])
            if state in {"OPEN", None}
        }
        cycle_skip = {
            (str(repo), int(number))
            for repo, number in payload.get("cycle_skip", [])
        }
        cycle_skip.intersection_update(nonterminal_cache)
        return nonterminal_cache, cycle_skip
    except (AttributeError, OSError, TypeError, ValueError):
        _log.debug(
            "kanban PR-state cache restore failed for %s",
            cache_key,
            exc_info=True,
        )
        return {}, set()
    finally:
        try:
            swap_path.unlink()
        except OSError:
            pass


def _pr_state_caches_for_board(
    cache_key: str,
) -> tuple[dict[tuple[str, int], Optional[str]], set[tuple[str, int]]]:
    """Return bounded per-board state, spilling live LRU entries to disk."""
    now = time.time()
    for stale_key, last_seen in list(_PR_BOARD_CACHE_LAST_SEEN.items()):
        if now - last_seen > _RESPAWN_GUARD_PR_BOARD_CACHE_RETENTION_SECONDS:
            _invalidate_pr_state_cache(stale_key)

    nonterminal_cache = _PR_NONTERMINAL_STATE_CACHES.pop(cache_key, None)
    cycle_skip = _PR_QUERY_CYCLE_SKIPS.pop(cache_key, None)
    if nonterminal_cache is None or cycle_skip is None:
        restored_cache, restored_skip = _load_pr_state_cache(cache_key)
        nonterminal_cache = nonterminal_cache or restored_cache
        cycle_skip = cycle_skip or restored_skip
    _PR_NONTERMINAL_STATE_CACHES[cache_key] = nonterminal_cache
    _PR_QUERY_CYCLE_SKIPS[cache_key] = cycle_skip
    _PR_BOARD_CACHE_LAST_SEEN[cache_key] = now

    for stale_key in list(_PR_NONTERMINAL_STATE_CACHES):
        if len(_PR_NONTERMINAL_STATE_CACHES) <= _RESPAWN_GUARD_PR_BOARD_CACHE_LIMIT:
            break
        if stale_key == cache_key:
            continue
        stale_cache = _PR_NONTERMINAL_STATE_CACHES[stale_key]
        stale_skip = _PR_QUERY_CYCLE_SKIPS.get(stale_key, set())
        if _store_pr_state_cache(stale_key, stale_cache, stale_skip):
            _PR_NONTERMINAL_STATE_CACHES.pop(stale_key, None)
            _PR_QUERY_CYCLE_SKIPS.pop(stale_key, None)
            _PR_BOARD_CACHE_LAST_SEEN.pop(stale_key, None)

    return nonterminal_cache, cycle_skip
_RESPAWN_GUARD_PR_PARSE_RE = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#][^\s]*)?$",
    re.IGNORECASE,
)


def _parse_github_pr_url(url: str) -> Optional[tuple[str, int]]:
    """Return ``(owner/repo, number)`` for a canonical GitHub PR URL."""
    if not isinstance(url, str):
        return None
    # Strip trailing punctuation that commonly abuts a URL in prose AND in
    # serialized payloads. The quote characters matter: workers routinely post
    # structured JSON handbacks (``"pr": "https://github.com/o/r/pull/3",``),
    # and the URL regex captures the trailing ``",`` — which made this parser
    # return None, which ``check_respawn_guard`` treats as "assume open" and
    # fail-closed to ``active_pr`` FOREVER, even after the PR merged. Observed
    # 2026-08-07: a task stayed undispatchable across repeated dispatch ticks
    # while its only referenced PR was demonstrably MERGED.
    cleaned = url.rstrip(".,;:!?)]}>\"'")
    match = _RESPAWN_GUARD_PR_PARSE_RE.fullmatch(cleaned)
    if match is None:
        return None
    return (f"{match.group(1)}/{match.group(2)}", int(match.group(3)))

# Head branch of every PR the guard has queried, keyed like the state caches.
# A PR's head ref never changes, so entries never go stale; bounded FIFO.
# Populated as a side effect of ``_query_github_pr_state`` (same ``gh`` call,
# no extra query budget). Absent entry = owner unknown = guard fail-safe.
_PR_HEAD_REF_CACHE: dict[tuple[str, int], str] = {}
_PR_HEAD_REF_CACHE_LIMIT = 1024

# Owner position only: the card id that OPENS the branch's last path segment
# (``<assignee>/<card_id>-<topic>``, ``wt/<card_id>``, ``<slug>/<card_id>``).
# An id elsewhere (``alice/fix-t_11111111``) is a topic mention, not an owner.
_CARD_ID_OWNER_RE = re.compile(r"t_[0-9a-f]{8}(?![0-9a-z])")


def _pr_belongs_to_other_card(repo: str, number: int, task_id: str) -> bool:
    """True only on POSITIVE evidence the PR is another card's work.

    Fleet worker branches are named ``<assignee>/<card_id>-<topic>``. A PR
    whose head branch has a card id other than ``task_id`` in that OWNER
    position (start of the last path segment) was opened for a different card: its URL on this card is a cross-card
    mention (a coordination note), not this card's in-flight work.
    2026-09-28, t_a8549f8b. Unknown head ref, or a branch with no card id in
    the owner position (even one mentioning an id in its topic, t_84471ec4),
    stays guarded (fail-safe).
    """
    head = _PR_HEAD_REF_CACHE.get((repo.lower(), int(number)))
    if not head:
        return False
    owner = _CARD_ID_OWNER_RE.match(head.lower().rsplit("/", 1)[-1])
    return owner is not None and owner.group(0) != task_id.lower()


def _pr_owned_by_card(repo: str, number: int, task_id: str) -> bool:
    """True only on POSITIVE evidence the PR is ``task_id``'s own work.

    The head branch carries ``task_id`` in the OWNER position (start of the
    last path segment, ``<assignee>/<card_id>-<topic>``). An unknown head ref
    or a branch with no card id there is NOT ownership: a card comment may
    mention any PR. Gates automatic land-requests (Prism #1545 cd0457757028).
    """
    head = _PR_HEAD_REF_CACHE.get((repo.lower(), int(number)))
    if not head:
        return False
    owner = _CARD_ID_OWNER_RE.match(head.lower().rsplit("/", 1)[-1])
    return owner is not None and owner.group(0) == task_id.lower()


def _query_github_pr_state(repo: str, number: int) -> Optional[str]:
    """Resolve a PR state via ``gh``; return None on any query failure.

    The health fields (``mergeStateStatus``, ``statusCheckRollup``) need
    check/status read access a PR-only token may lack. When that enriched
    query fails, retry with the state-only fields: the authoritative state
    must survive, and merge health is left unknown (the guard keeps holding).
    """
    key = (repo.lower(), int(number))
    base_fields = "state,mergedAt,headRefName"
    for fields in (f"{base_fields},mergeStateStatus,statusCheckRollup", base_fields):
        try:
            proc = subprocess.run(
                ["gh", "pr", "view", str(number), "-R", repo, "--json", fields],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                timeout=_RESPAWN_GUARD_PR_QUERY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return None
        if proc.returncode == 0:
            break
    else:
        return None
    with_health = fields != base_fields
    try:
        payload = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    head = payload.get("headRefName")
    if isinstance(head, str) and head:
        _PR_HEAD_REF_CACHE[(repo.lower(), int(number))] = head
        while len(_PR_HEAD_REF_CACHE) > _PR_HEAD_REF_CACHE_LIMIT:
            _PR_HEAD_REF_CACHE.pop(next(iter(_PR_HEAD_REF_CACHE)))
    if not with_health:
        # Never act on an older reading when this one could not see health.
        _PR_MERGE_HEALTH_CACHE.pop(key, None)
    else:
        _PR_MERGE_HEALTH_CACHE[key] = {
            "merge_state": str(payload.get("mergeStateStatus") or "").upper() or None,
            "ci_red": _pr_rollup_is_red(payload.get("statusCheckRollup")),
        }
    while len(_PR_MERGE_HEALTH_CACHE) > _PR_HEAD_REF_CACHE_LIMIT:
        _PR_MERGE_HEALTH_CACHE.pop(next(iter(_PR_MERGE_HEALTH_CACHE)))
    if payload.get("mergedAt"):
        return "MERGED"
    state = str(payload.get("state") or "").upper()
    return state if state in {"OPEN", "CLOSED", "MERGED"} else None

# Health of every PR the guard has queried, filled by the same query as the
# state (no extra budget): ``{"merge_state": mergeStateStatus, "ci_red": bool}``.
# Absent entry = health unknown = the ``active_pr`` guard keeps holding.
_PR_MERGE_HEALTH_CACHE: dict[tuple[str, int], dict] = {}

# mergeStateStatus values only the card's own worker can fix (rebase /
# update-branch / fix checks). BLOCKED is excluded: it is usually a review or
# required-approval wait; red required checks are caught by ``ci_red``.
_PR_WORKER_FIXABLE_MERGE_STATES = frozenset({"DIRTY", "BEHIND", "UNSTABLE"})
_PR_RED_CHECK_VALUES = frozenset({
    "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
    "STARTUP_FAILURE",
})

# States in which the PR can land without its worker.
_PR_MERGEABLE_STATES = frozenset({"CLEAN", "HAS_HOOKS", "BLOCKED"})


def _pr_rollup_is_red(rollup) -> bool:
    """True when any check-run conclusion / status-context state is failing."""
    if not isinstance(rollup, list):
        return False
    for item in rollup:
        if not isinstance(item, dict):
            continue
        for key in ("conclusion", "state"):
            if str(item.get(key) or "").upper() in _PR_RED_CHECK_VALUES:
                return True
    return False


def _pr_needs_its_worker(repo: str, number: int) -> Optional[str]:
    """Why an OPEN PR can only move with its card's worker, else None.

    ``DIRTY``/``BEHIND``/``UNSTABLE`` or a red check: nobody but the worker
    rebases or fixes it, so holding the workerless card for it deadlocks
    (2026-09-30, t_57274bc7 sat ready 12 h behind its own DIRTY #1871).
    """
    health = _PR_MERGE_HEALTH_CACHE.get((repo.lower(), int(number)))
    if not health:
        return None
    merge_state = health.get("merge_state")
    if merge_state in _PR_WORKER_FIXABLE_MERGE_STATES:
        return merge_state
    if health.get("ci_red"):
        return "CI red"
    return None


@dataclass
class _PrStateResolver:
    """Per-tick PR memo, optionally backed by a shared terminal-state cache."""

    max_queries: int = _RESPAWN_GUARD_PR_QUERY_LIMIT
    cache: dict[tuple[str, int], Optional[str]] = field(default_factory=dict)
    terminal_cache: dict[tuple[str, int], str] = field(default_factory=dict)
    terminal_cache_limit: Optional[int] = None
    nonterminal_cache: dict[tuple[str, int], Optional[str]] = field(default_factory=dict)
    nonterminal_cache_limit: Optional[int] = None
    cycle_skip: set[tuple[str, int]] = field(default_factory=set)
    query_count: int = 0
    budget_exhausted: bool = False
    seen_keys: set[tuple[str, int]] = field(default_factory=set, init=False)
    queried_nonterminal_keys: set[tuple[str, int]] = field(
        default_factory=set, init=False
    )

    def resolve(self, repo: str, number: int) -> Optional[str]:
        key = (repo.lower(), int(number))
        self.seen_keys.add(key)
        if key in self.terminal_cache:
            state = self.terminal_cache.pop(key)
            self.terminal_cache[key] = state
            return state
        if key in self.cache:
            return self.cache[key]
        if key in self.cycle_skip and key in self.nonterminal_cache:
            state = self.nonterminal_cache[key]
            self.cache[key] = state
            return state
        if self.query_count >= self.max_queries:
            self.budget_exhausted = True
            _log.debug(
                "kanban PR-state query budget exhausted for %s#%s "
                "(max_queries=%s)",
                key[0], key[1], self.max_queries,
            )
            return None
        self.query_count += 1
        state = _query_github_pr_state(repo, number)
        self.cache[key] = state
        if state in {"MERGED", "CLOSED"}:
            self.terminal_cache[key] = state
            if self.terminal_cache_limit is not None:
                while len(self.terminal_cache) > self.terminal_cache_limit:
                    self.terminal_cache.pop(next(iter(self.terminal_cache)))
            self.nonterminal_cache.pop(key, None)
            self.cycle_skip.discard(key)
        else:
            self.nonterminal_cache[key] = state
            self.queried_nonterminal_keys.add(key)
            if self.nonterminal_cache_limit is not None:
                while len(self.nonterminal_cache) > self.nonterminal_cache_limit:
                    stale_key = next(iter(self.nonterminal_cache))
                    self.nonterminal_cache.pop(stale_key)
                    self.cycle_skip.discard(stale_key)
        return state

    def finish_tick(self, *, scan_complete: bool) -> None:
        """Advance the fair-query cycle after the ready queue scan."""
        if self.budget_exhausted or not scan_complete or not self.seen_keys:
            self.cycle_skip.update(self.queried_nonterminal_keys)
        else:
            self.cycle_skip.clear()
_worker_processes: dict = {}
_worker_processes_lock = threading.Lock()
# pid -> (task_id, run_id, spawned_at) for every worker THIS process spawned. Read when the worker is
# seen to exit, so the leftovers of a run that ended cleanly (worker called kanban_complete, then
# exited) are reaped too: the crash path only ever looks at ``running`` cards (fork t_446b6b99).
_worker_identities: "dict[int, tuple[str, Optional[int], float]]" = {}

# Startup stranding is a boot/restart reconciliation pass, not a per-tick
# mount-probe fan-out. Ready/review candidates are still checked every tick
# immediately before claim.
_workspace_startup_scanned: set[str] = set()


def _record_worker_returncode(pid: int, code: int) -> None:
    """Remember Popen's native return code, including on Windows."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(code), now)
    # Age-based trim: drop entries older than the TTL.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    # Size cap as a final guard.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_run_exit(conn, task_id, run_id, pid):
    """Use the connected board and exact run, never ambient env or PID alone."""
    from hermes_cli.kanban_worker_exit import exit_file, read_exit_class, read_exit_status

    db_path = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    if db_path and run_id is not None:
        receipt = exit_file(Path(db_path), task_id, run_id)
        code = read_exit_status(receipt)
        if code is not None:
            # A worker that caught SIGTERM/SIGHUP/SIGINT records 128+signum
            # with exit_class "signaled" before its os._exit — it was ended
            # from outside, which must never read as a clean exit.
            if code > 128 and read_exit_class(receipt) == "signaled":
                return "signaled", code - 128
            if code == 0:
                return "clean_exit", code
            if code == KANBAN_RATE_LIMIT_EXIT_CODE:
                return "rate_limited", code
            if code in KANBAN_INFRA_EXIT_CODES:
                return "infra_unavailable", code
            return "nonzero_exit", code
    return _classify_worker_exit(pid)


def _run_exit_class(conn, task_id, run_id) -> Optional[str]:
    """Telemetry-only: which retry-preserving class the run's receipt named.

    ``quota`` / ``pool_exhausted`` / ``upstream_capacity`` (see
    ``hermes_cli.kanban_worker_exit``), or ``None`` for a legacy receipt or a
    status-only fallback. Never changes the exit KIND — that is the code.
    """
    from hermes_cli.kanban_worker_exit import exit_file, read_exit_class

    db_path = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    if db_path and run_id is not None:
        return read_exit_class(exit_file(Path(db_path), task_id, run_id))
    return None


def _run_model_turn_completed(conn, task_id, run_id) -> bool:
    """Positive receipt evidence that this run produced a model response.

    A clean process exit alone is insufficient: provider/bootstrap failures
    have historically returned rc=0 before a worker ever received a turn.
    Legacy or missing receipts therefore fail closed to ``False``.
    """
    from hermes_cli.kanban_worker_exit import exit_file, read_model_turn_completed

    db_path = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    if db_path and run_id is not None:
        return read_model_turn_completed(exit_file(Path(db_path), task_id, run_id))
    return False


def _dead_claimer_release_at(
    conn: sqlite3.Connection, task_id: str,
) -> tuple[Optional[int], str, Optional[str]]:
    """Earliest time a pid-less claim with a DEAD claimer may be released.

    A dead claimer is not proof that no worker exists: ``_default_spawn``
    calls ``Popen(start_new_session=True)`` before ``_set_worker_pid``
    commits, so a gateway killed in that window leaves a live orphan with no
    stamped pid. The row cannot tell such an orphan from a spawn that never
    happened, so release is time-bounded on the CURRENT run only:

    * no worker evidence yet (no ``heartbeat``/``spawned`` event on this run):
      release after ``DEAD_CLAIMER_LAUNCH_BOUND_SECONDS`` from the claim. An
      orphan that has not heartbeated by then is outside every observed launch
      (live ledger 2026-09-24, 12,732 runs: first heartbeat p99 69 s, max 708 s).
    * worker evidence exists: release after the newest evidence is older than
      ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` — the same staleness rule
      ``release_stale_claims`` applies to a worker whose pid IS alive. Without
      this bound an orphan that heartbeated once and then died held the card
      forever (the stuck-running shape this path exists to end).

    * operator claim (``claimed`` event carries ``operator_claim``, set only by
      ``hermes kanban claim --review``) with no ``spawned`` event (heartbeats
      alone do not count; the CLI can send them): release at
      the claim itself. Nothing ever spawns for such a run, so the pid in
      ``claim_lock`` (a CLI, or the long-lived gateway) is not a worker and its
      liveness is irrelevant; :func:`_terminate_reclaimed_worker` releases it
      without probing that pid (t_c3cf232e: without this the orphan held the
      card while every operator ``reclaim`` was refused ``liveness_unprovable``).

    Returns ``(release_at, basis, evidence_kind)``; ``release_at`` is None when
    there is no current run to anchor the bound (held).
    """
    run = conn.execute(
        "SELECT r.id, r.started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    if run is None or run["started_at"] is None:
        return None, "no_current_run", None
    spawned = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'spawned' LIMIT 1",
        (task_id, int(run["id"])),
    ).fetchone()
    if spawned is None:
        # Checked before heartbeat evidence: a credential-less
        # ``kanban heartbeat`` on an operator claim is not a worker, and must
        # not put it back behind the live-claimer hold (FleetReview
        # 19939cc85ba7). A ``spawned`` event is real worker evidence and keeps
        # the worker-safety path below.
        claimed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
            (task_id, int(run["id"])),
        ).fetchone()
        try:
            payload = (
                json.loads(claimed["payload"])
                if claimed and claimed["payload"] else {}
            )
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("operator_claim") is True:
            return int(run["started_at"]), "operator_claim_no_worker", None
    evidence = conn.execute(
        "SELECT kind, created_at FROM task_events WHERE task_id = ? "
        "AND run_id = ? AND kind IN ('heartbeat', 'spawned') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, int(run["id"])),
    ).fetchone()
    if evidence is None:
        bound = _dead_claimer_launch_bound_seconds()
        # The claim's OWN window (``claim --ttl N``, recorded as the ``claimed`` event's
        # ``expires``) is the same operator statement the env var is: holding a 1 s claim for
        # the 900 s default strands the card past every tick that could reclaim it. Dispatcher
        # claims pass no TTL, so their window IS the default and nothing changes for them.
        claimed_ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
            (task_id, int(run["id"])),
        ).fetchone()
        try:
            claim_window = int(json.loads(claimed_ev["payload"]).get("expires")) - int(run["started_at"])
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            claim_window = 0
        if 0 < claim_window < bound:
            bound = claim_window
        return (
            int(run["started_at"]) + bound,
            "launch_bound",
            None,
        )
    return (
        int(evidence["created_at"]) + DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS,
        "evidence_stale_bound",
        evidence["kind"],
    )


def _worker_session_members(sid: int) -> list[tuple[int, int]]:
    """``(pid, pgid)`` of every process still in session ``sid``.

    Workers are spawned with ``start_new_session=True``, so worker pid ==
    sid == pgid. Anything the worker started in ANOTHER process group of that
    session (pytest children, helper-launched headless Chromes, background
    shells) is not reached by signalling the worker pid and outlives it with
    ppid 1 (t_ca0233d7: 54 leaked Chromes, Studio load1 250). ``ps`` lists
    pid+pgid; ``os.getsid`` maps each PID (not each pgid, since a group whose
    leader exited still has live members). Same algorithm as hermes-home
    ``scripts/test-gate`` ``_session_groups``.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pgid="], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    members: list[tuple[int, int]] = []
    me = os.getpid()
    my_pgid = os.getpgid(0)
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, pgid = int(parts[0]), int(parts[1])
        if pid == me or pgid <= 1 or pgid == my_pgid:
            continue
        try:
            if os.getsid(pid) == sid:
                members.append((pid, pgid))
        except OSError:
            pass
    return members


def _member_birth(pid: int) -> Optional[float]:
    """Create time of ``pid``, or None when it is gone/unreadable."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def _session_owned_by_run(
    members: list[tuple[int, int]], born_after: float, born_before: float,
) -> bool:
    """True when some member of the session was created inside the recorded
    run, i.e. between its claim and its last liveness evidence.

    A worker pid can be recycled once its whole session is gone; a new
    session leader on that pid, exited with children left behind, looks
    exactly like a dead worker's leftovers. Its members were all created
    after the recorded worker died, which is after the run's last evidence,
    so they fail this check. While any member of the ORIGINAL session lives,
    the session is continuous and every group in it is the worker's.

    Known residual (deliberate, safety over completeness): on the crash path
    a child the worker started after its last heartbeat is indistinguishable
    from a recycled session's child and is left alone. Paths that verified
    the worker alive before signalling it bound on that moment instead.
    """
    for pid, _ in members:
        created = _member_birth(pid)
        # No upper slack: a recycled leader (and every child of it) is
        # created after the worker died, i.e. strictly after its last
        # evidence, and event times are floored, so ``<= born_before`` can
        # never admit one. The lower bound only needs claim-time slack.
        if created is not None and born_after - 1.0 <= created <= born_before:
            return True
    return False


def _reap_worker_session(
    sid: Optional[int],
    *,
    born_after: Optional[float],
    born_before: Optional[float],
    grace: float = 3.0,
) -> int:
    """SIGTERM every process group left in worker session ``sid``, wait up to
    ``grace`` seconds, then SIGKILL the survivors. Returns the groups signalled.

    Call only once the worker is gone. Signals nothing unless the session is
    proved to be the recorded run's (:func:`_session_owned_by_run`, with
    ``born_after`` = claim time and ``born_before`` = the last moment the
    worker was known alive); unknown bounds mean no reap. Every later scan
    must share a ``(pid, create_time)`` member with the scan before it, so a
    session that empties and has its sid reused mid-reap is never signalled.
    Groups that appear DURING the reap (a member forking into a new group on
    SIGTERM) are signalled while that continuity holds. Never targets sid <= 1, the caller's own session, or the
    caller's own process group. POSIX only; a no-op elsewhere.
    """
    import signal

    if not sid or int(sid) <= 1 or born_after is None or born_before is None:
        return 0
    if not (hasattr(os, "getsid") and hasattr(os, "killpg")):
        return 0
    sid = int(sid)
    try:
        if sid in (os.getsid(0), os.getpid()):
            return 0
    except OSError:
        return 0
    members = _worker_session_members(sid)
    if not members:
        return 0
    if not _session_owned_by_run(members, float(born_after), float(born_before)):
        _log.info(
            "kanban: session %d has %d member(s) but none from the recorded run; not reaped",
            sid, len(members),
        )
        return 0

    signalled: set[int] = set()

    def _term(groups: set[int]) -> None:
        for pgid in sorted(groups - signalled):
            try:
                os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok (POSIX-gated above)
            except OSError:
                pass
            signalled.add(pgid)

    def _identities(found: list[tuple[int, int]]) -> set[tuple[int, float]]:
        out = set()
        for pid, _ in found:
            created = _member_birth(pid)
            if created is not None:
                out.add((pid, created))
        return out

    known = _identities(members)

    def _left() -> set[int]:
        # Ownership is re-established on EVERY snapshot, never assumed: a
        # snapshot is the same session only if it still holds a member
        # (same pid AND create time) of the previous one. A session holding
        # any original member has not emptied, so its sid cannot have been
        # reused; groups a member created in answer to SIGTERM ride along.
        # No overlap -> the session emptied (or is unprovable): stop.
        nonlocal known
        current = _worker_session_members(sid)
        ids = _identities(current)
        if not current or not (ids & known):
            return set()
        known = ids
        return {pgid for _, pgid in current}

    _term({pgid for _, pgid in members})
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        left = _left()
        if not left:
            break
        _term(left)  # groups born during the reap get their SIGTERM too
        time.sleep(0.1)
    for pgid in sorted(_left()):
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok (POSIX-gated above)
        except OSError:
            pass
        signalled.add(pgid)
    _log.warning(
        "kanban: reaped %d leftover process group(s) of worker session %d",
        len(signalled), sid,
    )
    return len(signalled)


_CARD_ID_RE = re.compile(r"t_[0-9a-f]+")


def _cmdline_profile_cards(cmdline) -> set[str]:
    """Card ids named as a path component of a ``--user-data-dir`` argument.

    Chrome on Linux rewrites its argv area for its process title, and the
    kernel's ``/proc/<pid>/environ`` window sits right after argv, so every
    Chrome process reads back an environment WITHOUT the run identity
    (measured on ACE-AI: 0 of 11 chrome processes kept
    ``HERMES_KANBAN_TASK``; their ``cat`` helpers did). A browser profile
    under the card workspace (``.../workspaces/<card>/...`` or
    ``<repo>/.worktrees/<card>/...``) still names the card in its argv.
    """
    out: set[str] = set()
    args = list(cmdline or [])
    for i, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            path = arg.split("=", 1)[1]
        elif arg == "--user-data-dir" and i + 1 < len(args):
            path = args[i + 1]
        else:
            continue
        out.update(c for c in path.replace("\\", "/").split("/") if _CARD_ID_RE.fullmatch(c))
    return out


def _safe_cmdline(proc) -> list[str]:
    """``proc.cmdline()``, or [] when unreadable. psutil on macOS can raise
    ``SystemError`` (not a psutil.Error) for a process exiting mid-read."""
    try:
        return proc.cmdline() or []
    except Exception:
        return []


def _run_env_escapees_detailed(task_id: str, run_id: int) -> list[tuple[int, int, bool]]:
    """``(pid, pgid, by_env)`` of every live process whose ENVIRONMENT carries
    exactly this task+run identity, wherever it sits in the session tree
    (``by_env`` True). A process
    with no run identity in its environment also matches when its
    ``--user-data-dir`` names the card (:func:`_cmdline_profile_cards`):
    Linux Chrome erases its own environment window (``by_env`` False: the
    path proves the process, not its process group). Callers bound every
    match by the run's birth window, which tells runs of one card apart.

    A worker's children inherit its environment. One class of child
    ``setsid()``s into a NEW session on purpose (the browser-use harness
    daemon, ``browser_harness.daemon``, spawns with ``start_new_session``), so
    :func:`_worker_session_members` cannot see it and it outlives the run with
    ppid 1 (2026-09-30, Mac Studio: 26 daemons of done/archived cards, 1.2 GB
    RSS, +4/day). The run identity still travels with it in the environment.
    Same-uid only (``environ()`` raises otherwise); never self or our session.
    """
    me = os.getpid()
    try:
        my_sid = os.getsid(0) if hasattr(os, "getsid") else None
    except OSError:
        my_sid = None
    want_task, want_run = str(task_id), str(run_id)
    found: list[tuple[int, int]] = []
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        if pid == me or pid <= 1:
            continue
        try:
            env = proc.environ()
        except Exception:  # psutil on macOS: SystemError for a process exiting mid-read
            continue
        env_task = env.get("HERMES_KANBAN_TASK")
        if env_task is None:
            if want_task not in _cmdline_profile_cards(_safe_cmdline(proc)):
                continue
        elif env_task != want_task or env.get("HERMES_KANBAN_RUN_ID") != want_run:
            continue
        try:
            if my_sid is not None and os.getsid(pid) == my_sid:
                continue
            pgid = os.getpgid(pid)
        except OSError:
            continue
        if pgid <= 1 or pgid == os.getpgid(0):
            continue
        found.append((pid, pgid, env_task is not None))
    return found


def _run_env_escapees(task_id: str, run_id: int) -> list[tuple[int, int]]:
    """``(pid, pgid)`` of :func:`_run_env_escapees_detailed`."""
    return [(pid, pgid) for pid, pgid, _ in _run_env_escapees_detailed(task_id, run_id)]


#: Lower-bound slack on a member's birth time. Linux psutil derives
#: ``create_time`` from ``/proc/stat`` ``btime``, an INTEGER second, so a
#: process can read as born up to 1 s before it was (ACE-AI: btime fraction
#: 0.0855 s, child births read 0.093 s early). Same slack as
#: :func:`_session_owned_by_run`.
_BIRTH_SLACK_SECONDS = 1.0


def _reap_run_env_escapees(
    task_id: Optional[str],
    run_id: Optional[int],
    *,
    born_after: Optional[float],
    born_before: Optional[float],
    grace: float = 3.0,
) -> int:
    """Second sweep after :func:`_reap_worker_session`: SIGTERM (then SIGKILL)
    the process groups of run-identified processes that escaped the worker's
    session (see :func:`_run_env_escapees`). Same proof as the session reap:
    only members born inside ``[born_after, born_before]`` — the recorded
    run's window — are signalled; unknown bounds mean no reap. Call only once
    the worker is gone. POSIX only; a no-op elsewhere."""
    import signal

    if not task_id or run_id is None or born_after is None or born_before is None:
        return 0
    if not (hasattr(os, "getsid") and hasattr(os, "killpg")):
        return 0
    lo, hi = float(born_after) - _BIRTH_SLACK_SECONDS, float(born_before)
    matched: list[tuple[int, int, bool]] = []
    for pid, pgid, by_env in _run_env_escapees_detailed(task_id, run_id):
        born = _member_birth(pid)
        if born is None or not (lo <= born <= hi):
            continue
        matched.append((pid, pgid, by_env))
    if not matched:
        return 0
    # An env match proves the whole group (the run identity is inherited).
    # A profile-path match proves only that process, unless its group leader
    # is itself a match: an unrelated script could have launched the browser
    # into the script's own group, and that group is not the run's.
    matched_pids = {pid for pid, _, _ in matched}
    targets: dict[int, int] = {}  # pgid -> witness pid (group kill)
    for pid, pgid, by_env in matched:
        if by_env or pgid in matched_pids:
            targets[pgid] = pid
    singles: dict[int, float] = {}  # pid -> birth (identity-checked kill)
    for pid, pgid, by_env in matched:
        if pgid not in targets:
            born = _member_birth(pid)
            if born is not None:
                singles[pid] = born

    def _same(pid: int, born: float) -> bool:
        return _member_birth(pid) == born

    def _signal(sig, groups: dict[int, int], procs: dict[int, float]) -> None:
        for pgid in sorted(groups):
            try:
                os.killpg(pgid, sig)  # windows-footgun: ok (POSIX-gated above)
            except OSError:
                pass
        for pid, born in procs.items():
            if _same(pid, born):
                try:
                    os.kill(pid, sig)  # windows-footgun: ok (POSIX-gated above)
                except OSError:
                    pass

    reaped = len(targets) + len(singles)
    if not reaped:
        return 0
    _signal(signal.SIGTERM, targets, singles)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and (
        any(_pid_alive(p) for p in targets.values())
        or any(_same(p, b) for p, b in singles.items())
    ):
        time.sleep(0.1)
    _signal(
        signal.SIGKILL,  # windows-footgun: ok (POSIX-gated above)
        {g: p for g, p in targets.items() if _pid_alive(p)},
        {p: b for p, b in singles.items() if _same(p, b)},
    )
    _log.warning(
        "kanban: reaped %d run-identified process group(s) and %d profile-matched "
        "process(es) that escaped worker session of %s run %s",
        len(targets), len(singles), task_id, run_id,
    )
    return reaped


#: TERM -> KILL grace for the exit-path and orphan-sweep reaps (t_446b6b99).
WORKER_LEFTOVER_KILL_GRACE_SECONDS = 10.0
#: A card must have been terminal this long before the orphan sweep touches
#: processes in its workspace: a worker that called ``kanban_complete`` is
#: still finishing its turn for a few seconds after the card flips to done.
ORPHAN_SWEEP_MIN_TERMINAL_AGE_SECONDS = 300
_ORPHAN_SWEEP_CARD_RE = re.compile(r"t_[0-9a-f]+")


def _register_worker_identity(
    pid: int, task_id: str, run_id: Optional[int], spawned_at: float,
) -> None:
    """Remember which card/run a spawned worker pid belongs to."""
    with _worker_processes_lock:
        _worker_identities[int(pid)] = (str(task_id), run_id, float(spawned_at))


def reap_exited_worker_leftovers(
    conn: Optional[sqlite3.Connection],
    exited_pids: Iterable[int],
    *,
    grace: float = WORKER_LEFTOVER_KILL_GRACE_SECONDS,
) -> list[str]:
    """Reap what each just-exited worker left behind, whatever the card state.

    Workers run in their own session (``start_new_session=True``). A worker
    that completes its card and exits leaves every server it started (a
    ``caddy``, a preview server, a bridge) running with ppid 1; the crash
    path never sees it because the card is no longer ``running``
    (2026-10-01: four such listeners, 3 h 42 m to 2 d 7 h old). For each pid
    observed exiting this tick, reap its session's process groups and the
    run-identified processes that left the session, then record a
    ``worker_leftovers_reaped`` event on the card. Members must be born
    between the spawn and the moment ``poll()`` reaped the worker: its pid
    (= sid) is only free for reuse after that, so a recycled session leader
    is always born too late to qualify. Returns the task ids that had
    leftovers.
    """
    reaped_cards: list[str] = []
    for pid in exited_pids:
        with _worker_processes_lock:
            ident = _worker_identities.pop(int(pid), None)
        if ident is None:
            continue
        task_id, run_id, spawned_at = ident
        exit_entry = _recent_worker_exits.get(int(pid))
        now = exit_entry[1] if exit_entry else time.time()
        try:
            groups = _reap_worker_session(
                int(pid), born_after=spawned_at, born_before=now, grace=grace,
            )
            escaped = _reap_run_env_escapees(
                task_id, run_id, born_after=spawned_at, born_before=now, grace=grace,
            )
        except Exception as exc:  # never break a dispatcher tick
            _log.warning("kanban: leftover reap of worker %s failed: %s", pid, exc)
            continue
        if not (groups or escaped):
            continue
        reaped_cards.append(task_id)
        _log.warning(
            "kanban: reaped leftovers of exited worker pid=%s task=%s run=%s "
            "(session_groups=%d env_escapees=%d)",
            pid, task_id, run_id, groups, escaped,
        )
        if conn is not None:
            try:
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "worker_leftovers_reaped",
                        {"pid": int(pid), "session_groups": groups,
                         "env_escapees": escaped},
                        run_id=run_id,
                    )
            except Exception as exc:
                _log.debug("worker_leftovers_reaped event failed: %s", exc)
    return reaped_cards


def _card_terminal_since(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    """When ``task_id`` last changed if it is ``done``/``archived``, else None.

    "Last changed" is its newest event, so a card reopened and closed again
    restarts the age clock."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] not in ("done", "archived"):
        return None
    ev = conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (task_id,),
    ).fetchone()
    return float(ev[0]) if ev and ev[0] is not None else 0.0


_INIT_REAPER_NAMES = frozenset({"launchd", "init", "systemd"})


def _init_parented(proc: "psutil.Process", ppid: Optional[int]) -> bool:
    """True when ``proc`` was reparented to init: ppid 1 (launchd on macOS),
    or a Linux ``systemd --user`` child subreaper that adopts orphans in a
    user session (the CI runner's case)."""
    if ppid == 1:
        return True
    if not ppid or not sys.platform.startswith("linux"):
        return False
    try:
        return psutil.Process(ppid).name() in _INIT_REAPER_NAMES
    except (psutil.Error, OSError):
        return False


def sweep_terminal_workspace_orphans(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    root: Optional[Path] = None,
    grace: float = WORKER_LEFTOVER_KILL_GRACE_SECONDS,
    min_terminal_age: float = ORPHAN_SWEEP_MIN_TERMINAL_AGE_SECONDS,
    notify: bool = True,
) -> dict[str, list[dict]]:
    """Reap ppid-1 processes whose cwd is a TERMINAL card's scratch workspace.

    Backstop for leftovers the exit-path reap cannot see: workers spawned by a
    dispatcher that has since restarted (no retained Popen), or children that
    dropped both the session and the run identity. A candidate must be ours
    (same uid), reparented to init (:func:`_init_parented`), have its cwd under
    ``<workspaces_root>/<card>/``, and that card must be ``done``/``archived``
    for at least ``min_terminal_age`` seconds. A process whose environment
    names a DIFFERENT card that is not terminal is left alone. Each target and
    its descendants get SIGTERM, then SIGKILL after ``grace`` seconds. Records
    an ``orphans_reaped`` event per card and sends ONE #logs line per sweep,
    only when something was reaped. Returns ``{card: [{pid, name, cwd}]}``.
    """
    if not hasattr(os, "getuid"):
        return {}
    try:
        base = root if root is not None else workspaces_root(board, stale_pin_ok=True)
        base_real = os.path.realpath(str(base))
    except Exception:
        return {}
    me, uid = os.getpid(), os.getuid()  # windows-footgun: ok (hasattr-gated above)
    candidates: dict[str, list] = {}
    for proc in psutil.process_iter(["pid", "ppid", "uids"]):
        info = proc.info
        pid = info.get("pid") or 0
        if pid <= 1 or pid == me or not _init_parented(proc, info.get("ppid")):
            continue
        uids = info.get("uids")
        if uids is None or uids.real != uid:
            continue
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            continue
        if not cwd:
            continue
        try:
            rel = os.path.relpath(os.path.realpath(cwd), base_real)
        except ValueError:
            continue
        card = rel.split(os.sep, 1)[0]
        if rel.startswith(os.pardir) or not _ORPHAN_SWEEP_CARD_RE.fullmatch(card):
            continue
        candidates.setdefault(card, []).append((proc, cwd))
    if not candidates:
        return {}

    now = time.time()
    terminal: dict[str, bool] = {}

    def _is_terminal(card: str) -> bool:
        if card not in terminal:
            since = _card_terminal_since(conn, card)
            terminal[card] = since is not None and now - since >= min_terminal_age
        return terminal[card]

    plan: dict[str, list] = {}
    for card, procs in candidates.items():
        if not _is_terminal(card):
            continue
        for proc, cwd in procs:
            try:
                env_card = proc.environ().get("HERMES_KANBAN_TASK")
            except (psutil.Error, OSError):
                env_card = None
            if env_card and env_card != card and not _is_terminal(env_card):
                continue
            plan.setdefault(card, []).append((proc, cwd))
    if not plan:
        return {}

    targets: dict[int, "psutil.Process"] = {}
    reaped: dict[str, list[dict]] = {}
    for card, procs in plan.items():
        for proc, cwd in procs:
            try:
                name = proc.name()
                family = [proc, *proc.children(recursive=True)]
            except (psutil.Error, OSError):
                continue
            for p in family:
                if p.pid > 1 and p.pid != me:
                    targets.setdefault(p.pid, p)
            reaped.setdefault(card, []).append({"pid": proc.pid, "name": name, "cwd": cwd})
    for p in targets.values():
        try:
            p.terminate()  # psutil refuses a pid reused since it was listed
        except (psutil.Error, OSError):
            pass
    _, alive = psutil.wait_procs(list(targets.values()), timeout=grace)
    for p in alive:
        try:
            p.kill()
        except (psutil.Error, OSError):
            pass
    if not reaped:
        return {}
    for card, items in reaped.items():
        _log.warning("kanban: reaped %d orphan(s) in terminal card %s workspace: %s",
                     len(items), card, ", ".join(f"{i['name']}({i['pid']})" for i in items))
        try:
            with write_txn(conn):
                _append_event(conn, card, "orphans_reaped",
                              {"processes": items, "sigkill": len(alive)})
        except Exception as exc:
            _log.debug("orphans_reaped event failed: %s", exc)
    if notify:
        _notify_orphan_sweep(board, reaped)
    return reaped


def _notify_orphan_sweep(board: Optional[str], reaped: dict[str, list[dict]]) -> None:
    """ONE #logs line for a sweep that reaped something. Best-effort."""
    try:
        from hermes_cli import kanban_budget as _kbudget

        script = _kbudget._notify_script_path()
        if script is None:
            return
        n = sum(len(v) for v in reaped.values())
        detail = "; ".join(
            f"{card}: " + ", ".join(f"{i['name']}({i['pid']})" for i in items)
            for card, items in sorted(reaped.items())
        )
        body = (
            f"🧹 Kanban '{board or 'default'}': reaped {n} orphan process(es) "
            f"left in terminal cards' workspaces: {detail}"
        )
        _kbudget._run_notify([
            sys.executable, script, "--channel", "discord",
            "--target", _kbudget.RECOVERY_TARGET, "--send", body[:1900],
        ])
    except Exception as exc:  # pragma: no cover - paging must never break a tick
        _log.debug("orphan sweep notify failed: %s", exc)


def _run_last_evidence_at(
    conn: Optional[sqlite3.Connection], task_id: Optional[str],
    run_id: Optional[int] = None,
) -> Optional[float]:
    """Newest ``heartbeat``/``spawned`` event time of the run (current run
    when ``run_id`` is None): the last moment its worker is known alive."""
    if conn is None or task_id is None:
        return None
    if run_id is None:
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        run_id = row[0] if row else None
    if run_id is None:
        return None
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind IN ('heartbeat', 'spawned')",
        (task_id, int(run_id)),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _reap_terminated_worker_session(
    pid, info: dict, signal_fn, owner_window: tuple,
    verified_alive_at: Optional[float], conn, task_id,
    run_id: Optional[int] = None,
) -> None:
    """Reap the leftovers of a worker ``_terminate_reclaimed_worker`` just
    proved gone. Session members must date from the recorded run: claimed
    at/after the claim and no later than the moment the worker was verified
    alive (or, if it was already dead, its last heartbeat/spawn event).
    ``run_id`` is the TERMINATED run, never ``tasks.current_run_id``: by now
    the card may carry a newer run whose processes share the task id.
    Skipped when a test ``signal_fn`` stands in for real signals (those runs
    use fake or borrowed pids)."""
    if signal_fn is not None:
        return
    born_before = verified_alive_at
    if born_before is None and run_id is not None:
        born_before = _run_last_evidence_at(conn, task_id, run_id)
    reaped = _reap_worker_session(
        pid, born_after=owner_window[0] if owner_window else None,
        born_before=born_before,
    )
    if reaped:
        info["session_groups_reaped"] = reaped
    escaped = _reap_run_env_escapees(
        task_id, run_id,
        born_after=owner_window[0] if owner_window else None,
        born_before=born_before,
    )
    if escaped:
        info["env_escapees_reaped"] = escaped


def _dead_claimer_hold_only(worker_pid: Optional[int], termination: dict) -> bool:
    """True when the ONLY thing holding a claim is the dead-claimer bound.

    Host-local, no worker pid ever stamped, nothing signalled, the claimer
    pid parsed from ``claim_lock`` proven gone, and NO worker evidence
    (``spawned``/``heartbeat``) on the current run (``launch_bound`` basis).
    Anything else (a live claimer, a stamped pid, an identity check, a
    heartbeat from an unstamped orphan) is real liveness evidence.
    """
    return bool(
        termination.get("host_local")
        and not worker_pid
        and not termination.get("termination_attempted")
        and termination.get("claimer_pid_dead")
        and termination.get("dead_claimer_release_basis") == "launch_bound"
    )


def _host_process_mentions_task(task_id: str) -> bool:
    """True if any other process on this host carries ``task_id`` in argv.

    Dispatcher workers are launched with the task id on their command line,
    so an unstamped orphan from a claimer that died between ``Popen`` and
    ``_set_worker_pid`` is visible here. This process and its ancestors are
    skipped. An unreadable process table counts as a match (fail closed).

    A process whose command line cannot be read is INCONCLUSIVE, so it also
    counts as a match: it could be exactly the unstamped worker we are
    looking for. Two shapes are provably not our worker and are skipped: a
    zombie (no code runs) and a process whose effective uid differs from
    ours (the dispatcher launches workers as this user; setuid ``sudo`` /
    ``login`` and other users' processes are unreadable on macOS for that
    reason). If the uid cannot be read either, fail closed.
    """
    try:
        # This CLI and the shell(s) that launched it name the task too.
        me = psutil.Process()
        parents = me.parents()
        # Windows has no uids: never skip on uid there.
        _geteuid = getattr(os, "geteuid", None)
        my_euid = _geteuid() if _geteuid is not None else None
        # Skipping ancestors must not skip the worker itself: an unstamped
        # orphan that runs ``reclaim --operator`` on its own card is our
        # ancestor (or this very process). Its env grant is inherited by every
        # descendant, so check it before excluding anyone (FleetReview
        # 2362caff15e1 on #1404).
        if _caller_inside_task_worker(task_id, parents):
            return True
        own = {me.pid, *(p.pid for p in parents)}
        for proc in psutil.process_iter(["pid", "cmdline", "status", "uids"]):
            info = proc.info
            if info.get("pid") in own:
                continue
            cmdline = info.get("cmdline")
            if cmdline is None:
                if info.get("status") == psutil.STATUS_ZOMBIE:
                    continue
                uids = info.get("uids")
                if (
                    my_euid is not None
                    and uids is not None
                    and getattr(uids, "effective", my_euid) != my_euid
                ):
                    continue
                return True
            if any(task_id in str(a) for a in cmdline):
                return True
    except Exception:
        return True
    return False

# Process title a dispatcher worker gives itself (hermes_cli/main.py
# ``_set_process_title``). ``setproctitle`` overwrites argv AND the environ
# block, so the title is the only place the task id survives in the process
# table (FleetReview 1d1cb187c593 on #1404). Keep both sides in sync.
KANBAN_WORKER_PROCTITLE = "hermes kanban-worker {task_id}"


def _proc_is_titled_worker(cmdline, task_id: str) -> bool:
    """True if ``cmdline`` is exactly ``task_id``'s rewritten worker title."""
    joined = " ".join(str(a) for a in (cmdline or ()) if a).strip()
    return joined == KANBAN_WORKER_PROCTITLE.format(task_id=task_id)


def _caller_inside_task_worker(task_id: str, parents) -> bool:
    """True if this process runs inside ``task_id``'s own worker tree.

    The worker's env grant (``HERMES_KANBAN_TASK``) is inherited by every
    subprocess it launches; delegated children have it scrubbed, so each
    ancestor's env grant and process title are read too. A bare task id in
    an ancestor's argv is NOT a match: the operator's own shell
    (``sh -c '... reclaim t_x'``) names the task (FleetReview c18fd1d3b7f7
    on #1442). An ancestor whose title or env cannot be read is skipped, as
    every ancestor was before: a dispatcher worker is a same-uid Python
    process whose argv and env are readable, while CI runners and containers
    do have unreadable ancestors, and failing closed on them refused every
    override there.
    """
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip() == task_id:
        return True
    for parent in parents:
        try:
            if _proc_is_titled_worker(parent.cmdline(), task_id):
                return True
        except (psutil.Error, OSError):
            pass
        try:
            env = parent.environ()
        except (psutil.Error, OSError):
            continue
        if (env.get("HERMES_KANBAN_TASK") or "").strip() == task_id:
            return True
    return False


def _refuse_reclaim_unproven_death(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    termination: dict,
    *,
    reason: Optional[str] = None,
) -> None:
    """Record a reclaim REFUSED because the worker was not proven dead.

    Failing closed has to be visible or it is just a card that quietly stops
    moving. The ``reclaim_refused`` event is what surfaces the card as
    needs-attention: the claim, the run, and the status are left with their
    existing owner. A human can inspect and terminate the worker out of band;
    the next reclaim succeeds only after the worker pid is resolvable and
    proven gone. A NULL-pid claim remains held, not silently retried.

    ``claim_expires`` is extended the same way :func:`_defer_reclaim_for_live_worker`
    does, so the TTL sweep cannot turn around and release on the next tick what
    this call just refused to release.
    """
    now = int(time.time())
    grace = now + RECLAIM_DEFER_GRACE_SECONDS
    with write_txn(conn):
        run_id = _current_run_id(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount == 1 and run_id is not None:
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (grace, run_id),
            )
        payload = {
            "reason": (
                "liveness_unprovable"
                if termination.get("liveness_unprovable")
                else "worker_survived_termination"
            ),
            "requested_reason": reason,
            "claim_lock": claim_lock,
            "needs_attention": True,
        }
        payload.update(termination)
        _append_event(conn, task_id, "reclaim_refused", payload, run_id=run_id)


def _worker_cpu_active(pid: int) -> bool:
    """Veto only: a worker burning CPU right now is evidence against a stall.

    This probe can never *authorize* a reclaim on its own -- a worker blocked
    on an in-flight provider request reads 0% CPU, exactly like a dead
    socket. The stall decision comes from the agent's own progress timestamp
    (:func:`_run_progress_at`); this sample can only cancel it. Child
    processes are deliberately NOT a veto: workers hold persistent idle
    children (execute_code kernels, LSP servers) for their whole life, and a
    healthy long tool call already advances ``progress_at`` through the tool
    keepalive tickers.

    "Right now" is a delta: two samples of the pid's cumulative user+system
    CPU time :data:`_CPU_SAMPLE_SECONDS` apart. ``ps -o pcpu`` cannot answer
    it on Linux, where procps reports lifetime cputime / lifetime elapsed: a
    worker that burned CPU once and then stalled read busy for minutes to
    hours and vetoed its own reclaim, and a fresh idle child read busy ~1 s
    per 1 ms of startup CPU (t_46a2f8a1). A pid that no longer exists reads
    idle (nothing to veto). Unknown state (psutil missing, access denied,
    any other probe error) returns True so it never authorizes a kill.
    """
    try:
        before = _process_cpu_seconds(pid)
        time.sleep(_CPU_SAMPLE_SECONDS)
        after = _process_cpu_seconds(pid)
    except Exception as exc:
        try:
            import psutil
        except ImportError:
            return True  # Unknown process state must not authorize a kill.
        # NoSuchProcess includes ZombieProcess: exited, not busy.
        return not isinstance(exc, psutil.NoSuchProcess)
    return after > before

# Window between the two CPU-time samples in :func:`_worker_cpu_active`.
_CPU_SAMPLE_SECONDS = 0.5


def _process_cpu_seconds(pid: int) -> float:
    """Cumulative user+system CPU seconds of ``pid`` (the probe's only I/O).

    Children are never counted. Split out so tests inject a deterministic
    sampler instead of depending on a loaded host's scheduler. Raises
    ImportError / psutil errors; the caller maps them.
    """
    import psutil

    times = psutil.Process(int(pid)).cpu_times()
    return times.user + times.system


def _run_progress_at(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[int]:
    """Latest agent-reported progress time for ``run_id``, or None if unknown.

    The worker's heartbeat bridge (``heartbeat_current_worker_from_env``)
    stamps ``progress_at`` -- the agent's last API-call start/finish, stream
    chunk, tool start/finish or retry -- onto its heartbeat events. Pure
    provider-wait tickers refresh the heartbeat but NOT ``progress_at``, so a
    fresh heartbeat with an old ``progress_at`` is the run 7914 shape. A run
    that never reported ``progress_at`` (older worker, non-agent worker) is
    unknown and must never be reclaimed by the stall detector.
    """
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='heartbeat' AND payload LIKE '%progress_at%' "
        "ORDER BY id DESC LIMIT 5",
        (task_id, run_id),
    ).fetchall()
    for row in rows:
        try:
            value = json.loads(row["payload"] or "null").get("progress_at")
            if value is not None:
                return int(value)
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def detect_progress_stalls(
    conn: sqlite3.Connection, *, stall_seconds: int = 900,
    reclaim_seconds: int = 1500, board: Optional[str] = None,
) -> list[str]:
    """Detect a silent model loop even while its wrapper sends heartbeats.

    A run is stalled only when the agent's own progress timestamp (plus
    worker-log growth) has been stale for the whole window; a single
    idle process sample is never sufficient, only a veto.
    """
    now = int(time.time())
    reclaimed = []
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.claim_lock, t.current_run_id, "
        "t.last_heartbeat_at, t.assignee, r.started_at FROM tasks t "
        "JOIN task_runs r ON r.id=t.current_run_id WHERE t.status='running'"
    ).fetchall()
    for row in rows:
        if row["started_at"] is None or row["worker_pid"] is None:
            continue
        start = int(row["started_at"])
        hb = row["last_heartbeat_at"]
        if hb is None or now - int(hb) >= _STALE_HEARTBEAT_GAP_SECONDS:
            continue
        pid = int(row["worker_pid"])
        lock = row["claim_lock"]
        if not lock or not str(lock).startswith(f"{_claimer_id().split(':', 1)[0]}:"):
            continue
        rid = row["current_run_id"]
        progress_at = _run_progress_at(conn, row["id"], rid)
        if progress_at is None:
            continue  # No agent progress signal: unknown, never reclaim.
        try:
            log_time = int(worker_log_path(row["id"], board=board).stat().st_mtime)
        except OSError:
            log_time = start
        age = now - max(start, log_time, progress_at)
        if age < stall_seconds:
            continue
        if _worker_cpu_active(pid):
            continue
        evidence = {
            "progress_age_seconds": age, "heartbeat_age_seconds": now - int(hb),
            "agent_progress_age_seconds": now - progress_at,
            "log_age_seconds": now - log_time, "worker_pid": pid,
            "oldest_age_seconds": max(now - progress_at, now - log_time),
            "signals": ["no_agent_progress", "no_log_growth", "no_cpu"],
        }
        prior = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='stalled' LIMIT 1",
            (row["id"], rid),
        ).fetchone()
        if not prior:
            with write_txn(conn):
                _append_event(conn, row["id"], "stalled", evidence, run_id=rid)
        if age < reclaim_seconds:
            continue
        termination = _terminate_reclaimed_worker(
            pid, lock, owner_window=_worker_owner_window(conn, row["id"], pid, rid),
            conn=conn, task_id=row["id"], run_id=rid,
        )
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], lock, now, termination, reason="progress_stalled_worker_alive",
            )
            continue
        attempts = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND outcome='stalled'",
            (row["id"],),
        ).fetchone()[0]
        escalate = attempts >= 2
        reason = f"worker progress stalled for {age}s; {evidence['signals']}; heartbeat age {evidence['heartbeat_age_seconds']}s"
        with write_txn(conn):
            status = "blocked" if escalate else _retry_status_for_run(conn, row["id"])
            cur = conn.execute(
                "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, "
                "worker_pid=NULL, last_heartbeat_at=NULL, block_kind=? "
                "WHERE id=? AND status='running' AND current_run_id=? AND claim_lock=?",
                (status, "capability" if escalate else None, row["id"], rid, lock),
            )
            if cur.rowcount != 1:
                continue
            ended = _end_run(
                conn, row["id"], outcome="stalled", status="stalled",
                error=reason, metadata={**evidence, **termination},
            )
            _append_event(conn, row["id"], "blocked" if escalate else "stalled_reclaimed",
                          {"reason": reason, **evidence}, run_id=ended)
        reclaimed.append(row["id"])
    return reclaimed
ORPHANED_TERMINAL_TASK_OUTCOME = "orphaned_terminal_task"


def end_orphaned_terminal_runs(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
) -> list[int]:
    """End ``task_runs`` rows still open on a card that is already terminal.

    Every other reaper (``release_stale_claims``, ``detect_crashed_workers``,
    ``enforce_max_runtime``, ``reconcile_orphaned_running``) selects from
    ``tasks WHERE status = 'running'``. A run left open when its card reached
    ``done``/``archived`` by some path that did not close it (run no longer
    the card's ``current_run_id``, a transition that bypassed ``_end_run``,
    manual SQL) is therefore invisible to all of them and stays ``running``
    forever — four such rows sat on the live board for 2-3 days (card
    t_cb91bfc4).

    A row is ended when its task is ``done``/``archived`` AND either the
    worker is provably gone (no pid, or a host-local pid that is not alive)
    OR its last sign of life (heartbeat, else start) is older than the run's
    ``max_runtime_seconds`` (default ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS``).
    A host-local live pid with a fresh heartbeat is left alone: that worker may
    still be finalising after its own transition. Foreign-host pids cannot be
    probed, so only the heartbeat arm applies to them.

    Ends the row with ``status='reclaimed'``,
    ``outcome='orphaned_terminal_task'`` and records an
    ``orphaned_terminal_run_ended`` event. Returns the ended run ids.
    """
    now = int(time.time()) if now is None else int(now)
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    rows = conn.execute(
        "SELECT r.id, r.task_id, r.worker_pid, r.claim_lock, r.started_at, "
        "       r.last_heartbeat_at, r.max_runtime_seconds, r.metadata, "
        "       t.status AS task_status "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.ended_at IS NULL AND t.status IN ('done', 'archived')"
    ).fetchall()
    ended: list[int] = []
    for row in rows:
        pid = row["worker_pid"]
        host_local = str(row["claim_lock"] or "").startswith(host_prefix)
        pid_dead = (not pid) or (host_local and not _pid_alive(int(pid)))
        last_sign = row["last_heartbeat_at"] or row["started_at"]
        limit = int(
            row["max_runtime_seconds"] or DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        )
        stale = last_sign is None or now - int(last_sign) > limit
        if not (pid_dead or stale):
            continue
        run_id = int(row["id"])
        payload = {
            "reason": ORPHANED_TERMINAL_TASK_OUTCOME,
            "task_status": row["task_status"],
            "worker_pid": int(pid) if pid else None,
            "claim_lock": row["claim_lock"],
            "last_heartbeat_at": _opt_int_value(row["last_heartbeat_at"]),
            "pid_dead": bool(pid_dead),
            "heartbeat_stale": bool(stale),
            "max_runtime_seconds": limit,
            "now": now,
        }
        # Merge, never replace: the open run may already carry metadata
        # (``pool`` from _stamp_run_pool) that the ledger reads (C7 k110).
        try:
            prior_meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            prior_meta = {}
        run_meta = {**prior_meta, **payload} if isinstance(prior_meta, dict) else payload
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE task_runs SET status = 'reclaimed', outcome = ?, "
                "    error = ?, metadata = ?, ended_at = ?, "
                "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND ended_at IS NULL",
                (
                    ORPHANED_TERMINAL_TASK_OUTCOME,
                    f"run left open on a {row['task_status']} card; ended by reaper",
                    json.dumps(run_meta, ensure_ascii=False),
                    now,
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                continue
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL "
                "WHERE id = ? AND current_run_id = ?",
                (row["task_id"], run_id),
            )
            _append_event(
                conn, row["task_id"], "orphaned_terminal_run_ended", payload,
                run_id=run_id,
            )
        ended.append(run_id)
        _log.info(
            "kanban reaper: ended run %s left open on %s task %s (pid=%r)",
            run_id, row["task_status"], row["task_id"], pid,
        )
    return ended


def _opt_int_value(value) -> Optional[int]:
    return int(value) if value is not None else None

# Identical clean exits (same captured worker output) that end the retry budget
# EARLY. Two is the smallest number that distinguishes "reproduced" from
# "happened once": a worker that reaches the same dead end twice is not
# converging, so a third spawn buys nothing but a worker slot and quota. The
# common cause is a card whose premise was already satisfied before dispatch,
# which now has an honest terminal verb (``complete_task(superseded_by=...)``).
_PROTOCOL_VIOLATION_REPRODUCED_LIMIT = 2


def _violation_output_fingerprint(metadata: dict) -> str:
    """Per-run fingerprint of a violation run's own output; "" when absent.

    Reads ONLY ``run_output_fingerprint``, which ``detect_crashed_workers``
    computes from the boundary-delimited segment for that one run. It
    deliberately does NOT fall back to ``stderr_tail``: that field is a raw
    byte window into the APPEND-mode per-task log, so it spans every run of the
    task, and comparing two of them answers a different question than "did this
    worker reproduce itself". Both ways it lies — below the window size the
    window starts at byte 0 every run, so the same leading bytes compare EQUAL
    across genuinely different work; just under it the window straddles a run
    boundary differently each time, so a true repeat compares UNEQUAL.

    Absent output must never compare equal: two runs with nothing recorded say
    nothing about each other, so the early trip stays off for them. Runs
    recorded before the segment existed have no ``run_output_fingerprint`` and
    therefore never trip it either.
    """
    raw = metadata.get("run_output_fingerprint") or ""
    return " ".join(str(raw).split())[:400]


def _protocol_violation_history(
    conn: sqlite3.Connection, task_id: str,
) -> tuple[int, int]:
    """``(streak, identical_repeats)`` over the trailing violation run.

    ``identical_repeats`` counts how many of those violations were captured
    with the SAME worker output as the newest one (see
    ``_violation_output_fingerprint``). A worker that reproduces its own clean
    exit verbatim is not converging, so ``_account_crashes`` trips on the
    repeat instead of spending the whole budget re-running it.
    """
    streak = 0
    newest_output: Optional[str] = None
    identical = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome in ("rate_limited", "infra_unavailable"):
            continue
        if outcome == "crashed":
            is_violation = False
            meta: dict = {}
            raw_meta = row["metadata"]
            if raw_meta:
                try:
                    parsed = json.loads(raw_meta)
                    meta = parsed if isinstance(parsed, dict) else {}
                    is_violation = bool(meta.get("protocol_violation"))
                except (ValueError, TypeError):
                    meta, is_violation = {}, False
            if not is_violation:
                is_violation = "protocol violation" in (row["error"] or "")
            if is_violation:
                streak += 1
                output = _violation_output_fingerprint(meta)
                if streak == 1:
                    newest_output = output
                    identical = 1 if output else 0
                elif output and output == newest_output and identical == streak - 1:
                    identical += 1
                continue
        break
    return streak, identical

# Cohort-death guard (card t_0c1ebbae). On 2026-09-23 15:58 an outside reaper
# SIGTERM'd 17 heartbeating workers at once; each was accounted as its own
# crash / protocol violation, and the same-fingerprint "systemic" rule then
# blocked several cards outright. A burst of externally-ended pids that were
# all still heartbeating is one event about the HOST, never N task failures.
_COHORT_DEATH_MIN = 3
_COHORT_DEATH_HEARTBEAT_WINDOW_SECONDS = 120

# Exit kinds that mean "ended from outside": no receipt + not our child
# (``unknown``) or terminated by a signal (reaped status or signal receipt).
_COHORT_DEATH_EXIT_KINDS = frozenset({"unknown", "signaled"})


def _cohort_death_ids(dead: list, *, now: float) -> set:
    """Task ids of dead workers that died as a cohort in this tick, else empty.

    ``dead`` holds ``(row, pid, exit_kind, exit_code)``. A worker counts toward
    the cohort only if it ended externally (``_COHORT_DEATH_EXIT_KINDS``) and
    its last heartbeat is within the window — a worker that had already gone
    silent is a stall, not part of a simultaneous kill.
    """
    fresh_after = now - _COHORT_DEATH_HEARTBEAT_WINDOW_SECONDS
    members = set()
    for row, _pid, kind, _code in dead:
        if kind not in _COHORT_DEATH_EXIT_KINDS:
            continue
        hb = row["last_heartbeat_at"] if "last_heartbeat_at" in row.keys() else None
        if hb is not None and hb >= fresh_after:
            members.add(row["id"])
    return members if len(members) >= _COHORT_DEATH_MIN else set()


def _page_cohort_death(conn: sqlite3.Connection, task_ids: list) -> None:
    """Page #alerts ONCE for a cohort death. Best-effort, never raises.

    Once per cohort by construction: the members are released in the same
    tick that detects them, so the next tick cannot see them dead again.
    """
    try:
        import importlib
        import subprocess

        cli = importlib.import_module(f"{__package__}.kanban")
        script = cli._notify_script_path()
        if script is None:
            return
        db_path = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
        body = (
            f"💀 Kanban cohort death: {len(task_ids)} workers ended externally in ONE "
            f"dispatcher tick while still heartbeating ({os.path.basename(db_path or '')}).\n"
            f"Tasks: {', '.join(task_ids[:20])}{' …' if len(task_ids) > 20 else ''}\n"
            "Requeued WITHOUT counting a failure. Something outside the dispatcher killed "
            "them (reaper / OOM / signal sweep) — find the sender: "
            "`log show --predicate 'eventMessage CONTAINS \"sent by\"'` around the tick."
        )
        subprocess.run(
            [sys.executable, str(script), "--send", body, "--channel", "discord"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as exc:  # pragma: no cover - paging must never break a tick
        _log.debug("cohort-death page failed: %s", exc)

# Workspace-resolution errors that NO retry can clear: the anchor path is
# missing, is not a repo, or is a BARE repo with no checkout to hang a linked
# worktree on. The repo's shape does not change between dispatcher ticks, so
# spending the retry budget on it is pure waste.
#
# 2026-09-20: ~/dev/fleetreview-router was converted to a bare repo at 21:32;
# seven cards created 23:29-23:31 anchored there each burned 3 spawns (21
# total) before gave_up parked them in an untyped ``blocked`` with no
# actionable reason. These are capability walls — only a human re-pointing the
# card fixes them — so they block on failure #1.
#
# Every marker here must describe state that CANNOT change between ticks.
# "board has no default_workdir" deliberately does NOT qualify: that is board
# metadata an operator can set without touching the card, so a card blocked on
# it is recoverable and keeps its ordinary retry budget.
_UNUSABLE_WORKSPACE_MARKERS = (
    "is not inside a git repo and does not point at a git repo root",
    "workspace path must be absolute",
)


def _unusable_workspace_reason(exc: BaseException) -> Optional[str]:
    """Return an operator-actionable reason when *exc* is a permanent wall.

    ``None`` means "treat as an ordinary, possibly-transient spawn failure and
    let the normal retry budget apply".

    The operator instructions LEAD. ``_record_task_failure`` persists
    ``error[:500]``, and the raw exception embeds a full workspace path — a
    deep path pushed the entire actionable half past the cut, storing 500
    characters that ended mid-path and told the reader nothing.
    """
    text = str(exc)
    if not any(marker in text for marker in _UNUSABLE_WORKSPACE_MARKERS):
        return None
    return (
        "workspace is unusable and NO retry can fix it, so the card is "
        "blocked on the FIRST failure instead of burning the retry budget. "
        "Fix: re-create the card with --workspace worktree:/abs/path/to/a/"
        "NON-BARE checkout (a bare repo has no work tree to anchor on), or "
        "--workspace scratch for a read-only probe. There is no "
        f"'edit --workspace', so the card must be archived and re-created. "
        f"Underlying error: {text}"
    )


def _stamp_run_pool(conn: sqlite3.Connection, run_id: int, pool: str) -> None:
    """Record the relay pool a spawn was charged to on ``task_runs.metadata``
    (key ``pool``) so the dispatcher's in-flight count is one query and never
    re-resolves routes for running tasks (t_38be6b10). Best-effort."""
    try:
        with write_txn(conn):
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if row is None:
                return
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["pool"] = pool
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), run_id),
            )
    except Exception as exc:
        _log.warning("kanban run pool stamp failed for run %s (%s: %s)",
                     run_id, type(exc).__name__, exc)


def _pool_in_flight(conn: sqlite3.Connection) -> dict[str, int]:
    """Open runs per charged relay pool (``task_runs.metadata.pool``).

    Runs without a ``pool`` key (legacy, non-pool routes, or unparseable
    metadata) count as 0: the concurrency ceiling fails OPEN, never closed.
    """
    counts: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT metadata FROM task_runs "
            "WHERE status = 'running' AND ended_at IS NULL "
            "AND metadata IS NOT NULL",
        ).fetchall()
    except Exception as exc:
        _log.warning("kanban pool in-flight query failed (%s: %s)", type(exc).__name__, exc)
        return counts
    for row in rows:
        try:
            meta = json.loads(row["metadata"])
        except (TypeError, ValueError):
            continue
        pool = meta.get("pool") if isinstance(meta, dict) else None
        if isinstance(pool, str) and pool:
            counts[pool] = counts.get(pool, 0) + 1
    return counts


def _claim_still_held(conn: sqlite3.Connection, task: "Task") -> bool:
    """True while ``task``'s claimed run still owns the card.

    Checked immediately before spawning: workspace setup can take minutes,
    and a release in that window (reclaim, reconcile, dashboard move) means
    this launch no longer owns the card. Spawning anyway is the t_09180e10
    double-worker shape.
    """
    if task.current_run_id is None:
        return True
    return conn.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND status = 'running' "
        "AND current_run_id = ?",
        (task.id, int(task.current_run_id)),
    ).fetchone() is not None


def _abort_lost_claim_spawn(
    conn: sqlite3.Connection, task: "Task", pid: Optional[int],
) -> None:
    """Record (and, if a process started, terminate) a launch whose claim was
    released while the spawn was in flight."""
    payload: dict[str, Any] = {
        "reason": "claim_lost_before_spawn" if not pid else "claim_lost_after_spawn",
        "run_id": task.current_run_id,
    }
    if pid:
        termination = _terminate_reclaimed_worker(
            int(pid), task.claim_lock, conn=conn, task_id=task.id,
            run_id=task.current_run_id,
            owner_window=_worker_owner_window(
                conn, task.id, int(pid), task.current_run_id,
            ),
        )
        payload.update(termination)
        if not termination.get("terminated"):
            payload["needs_attention"] = True
    _append_event(conn, task.id, "spawn_aborted", payload,
                  run_id=task.current_run_id)


def _respawn_guard_eligible_at(
    next_eligible_at: Optional[int],
    failed_at: Optional[int],
    cooldown: int,
) -> Optional[int]:
    """When the failure stamped on a task stops deferring its respawn.

    ``tasks.next_eligible_at`` wins when it was stamped for THIS failure
    (at/after the failing run ended); a leftover from an earlier rate-limit
    requeue is ignored and the bound is derived as ``failed_at + cooldown``.
    """
    if next_eligible_at is not None and (
        failed_at is None or int(next_eligible_at) >= failed_at
    ):
        return int(next_eligible_at)
    if failed_at is not None:
        return failed_at + cooldown
    return None


def _respawn_guard_failure_reset_after(
    conn: sqlite3.Connection, task_id: str, failed_at: int,
) -> bool:
    """True when a requeue / lane-change event landed at/after ``failed_at``.

    Such an event makes the failure stamped on the task stale for the
    ``rate_limit_cooldown`` and ``blocker_auth`` respawn-guard rules.
    """
    kinds_sql = ",".join("?" * len(_RESPAWN_GUARD_FAILURE_RESET_KINDS))
    return conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND created_at >= ? "
        f"AND kind IN ({kinds_sql}) LIMIT 1",
        (task_id, failed_at, *_RESPAWN_GUARD_FAILURE_RESET_KINDS),
    ).fetchone() is not None


def _unused_operator_intent_after_pr(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether the latest PR-bearing comment has an unconsumed resume intent.

    Event ids establish causality even when writes share a timestamp. A spawn
    consumes the intent; automatic crash reclaim cannot reuse it.
    """
    comments = conn.execute(
        "SELECT id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY id DESC",
        (task_id, int(time.time()) - _RESPAWN_GUARD_PR_WINDOW),
    ).fetchall()
    pr_comment = next(
        (c for c in comments if _RESPAWN_GUARD_PR_URL_RE.search(c["body"] or "")), None,
    )
    if pr_comment is None:
        return False
    # The intent event snapshots the highest comment id atomically. No guess
    # about which 'commented' event corresponds to this PR is needed: inline
    # audit comments and historical trimmed bodies cannot skew the ordering.
    # Pre-upgrade intent events have no marker; an equal-second tie resumes:
    # one duplicate worker is recoverable, an indefinite guard is not.
    kinds = ",".join("?" * len(_RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS))
    return conn.execute(
        "SELECT 1 FROM task_events i WHERE i.task_id = ? "
        f"AND (i.kind IN ({kinds}) OR "
        "(i.kind = 'reclaimed' AND json_extract(i.payload, '$.manual') = 1) OR "
        "(i.kind = 'dependency_wait' AND json_extract(i.payload, '$.kind') = 'dependency' "
        "AND EXISTS (SELECT 1 FROM task_events p WHERE p.task_id = i.task_id "
        "AND p.kind = 'promoted' AND p.id > i.id))) "
        "AND (json_extract(i.payload, '$.after_comment_id') >= ? OR "
        "(json_type(i.payload, '$.after_comment_id') IS NULL AND i.created_at >= ?)) "
        "AND NOT EXISTS (SELECT 1 FROM task_events s WHERE s.task_id = i.task_id "
        "AND s.kind = 'spawned' AND s.id > i.id) LIMIT 1",
        (task_id, *_RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS,
         pr_comment["id"], pr_comment["created_at"]),
    ).fetchone() is not None

def _reassign_stale_pr_owners(
    conn: sqlite3.Connection,
    task_id: str,
    owners: list,
    detail: dict,
    *,
    dry_run: bool = False,
) -> Optional[str]:
    """Reclaim each STALE running owner of ``task_id``'s PRs (t_cb70d390).

    The PR passes to ``task_id``; the old owner goes back to its queue and is
    held by ``pr_owner_busy`` while the new card runs. Returns None when every
    owner was reclaimed (spawn may proceed), else ``pr_owner_busy``: a reclaim
    that cannot prove the old worker dead (``reclaim_task`` fails closed) never
    becomes a second writer.
    """
    from . import kanban_pr_owner as _kpo

    for owner in owners:
        reason = (
            f"{_kpo.REASSIGN_EVENT}: {owner['pr']} had no push and no new run for "
            f"{_kpo.STALE_OWNER_SECONDS // 3600} h; reassigned to {task_id}"
        )
        if dry_run:
            continue
        ok = False
        try:
            ok = reclaim_task(conn, owner["task_id"], reason=reason)
        except Exception:
            _log.exception("kanban dispatch: stale PR owner reclaim failed for %s",
                           owner["task_id"])
        if not ok:
            detail.update(pr=owner["pr"], owner=owner["task_id"],
                          hold=f"stale owner {owner['task_id']} could not be reclaimed")
            return _kpo.GUARD_REASON
        payload = {"pr": owner["pr"], "from": owner["task_id"], "to": task_id,
                   "owner_since": owner.get("since"),
                   "head_committed_at": owner.get("head_committed_at")}
        with write_txn(conn):
            _append_event(conn, owner["task_id"], _kpo.REASSIGN_EVENT, payload)
            _append_event(conn, task_id, _kpo.REASSIGN_EVENT, payload)
    return None


# ``hold`` of an active_pr decline whose OPEN own-PR is mergeable. The gateway
# watcher enqueues that PR on the land queue instead of paging (t_5a9deed5).
RESPAWN_GUARD_HOLD_MERGEABLE = "PR mergeable, closer will land it"

# A READY+assigned card continuously deferred as ``respawn_guarded:active_pr``
# for this long is STUCK, not cooling down: nothing automatic will clear it.
RESPAWN_GUARD_STUCK_SECONDS = 30 * 60

# The newest guard event must be this fresh for the streak to be "current"
# (the dispatcher still re-evaluates it); a stale streak means the dispatcher
# itself is down, which other health lanes own.
_RESPAWN_GUARD_STUCK_FRESH_SECONDS = 10 * 60


def render_operator_command(board: str, verb: str, *args: str) -> str:
    """Render a runnable ``hermes kanban`` command for an operator page.

    The ONLY place an alert/hint may build a ``hermes kanban`` command
    string. It always pins ``--board <slug>`` (the default board included),
    so the command acts on the card's board no matter which board is current
    where the operator pastes it. Arguments are shell-quoted. 2026-09-24,
    t_7d7ff489 r5: a hand-assembled unscoped verb failed (rc1 "not found")
    against a secondary-board card.
    """
    return shlex.join(["hermes", "kanban", "--board", str(board), verb, *map(str, args)])

# Event kinds that can change ``check_respawn_guard``'s ``active_pr`` answer:
# the intent/requeue kinds the guard itself reads, plus the worker's own
# dependency block and a spawn (which consumes intent). Only these restart
# the continuous-guard age in ``respawn_guard_stuck_tasks``; everything else
# (commented, heartbeat, linked, attached, ...) is data. Any trip away from
# READY returns through one of the requeue kinds, so status transitions are
# covered too.
_RESPAWN_GUARD_STUCK_RESET_KINDS: tuple[str, ...] = (
    *_RESPAWN_GUARD_FAILURE_RESET_KINDS,
    "dependency_wait",
    "spawned",
)


def respawn_guard_stuck_tasks(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    min_seconds: int = RESPAWN_GUARD_STUCK_SECONDS,
    now: Optional[int] = None,
) -> list[dict]:
    """Return unclaimed cards continuously refused by active_pr or prior worker.

    Active-PR guards page for ready cards after 30 minutes; claim rejection by
    an allegedly live previous worker pages for ready or review cards after
    15 minutes (the process may have exited or its PID may have been recycled).
    For active_pr, a card qualifies when it has been guarded with ``active_pr`` since the
    last event that could have changed the guard's answer
    (``_RESPAWN_GUARD_STUCK_RESET_KINDS``, or a guard decline for another
    reason), the first such guard row is at least ``min_seconds`` old, and the
    newest is recent. Data-only events (a progress comment, heartbeat,
    attachment) do NOT restart the age. Each entry carries ``clear_verb``,
    rendered by ``render_operator_command`` for ``board`` (defaults to the
    current board; callers holding another board's connection must pass it).

    ``respawn_guarded`` is a benign decline for the stall streak, so without
    this probe a card held by the guard is indistinguishable from a card that
    is briefly cooling down. 2026-09-23, t_7d7ff489: two cards sat silent for
    8h and 14h.
    """
    now = int(time.time()) if now is None else int(now)
    board = board or get_current_board()
    reset_marks = ", ".join("?" for _ in _RESPAWN_GUARD_STUCK_RESET_KINDS)
    out: list[dict] = []
    for row in conn.execute(
        "SELECT id, assignee, status FROM tasks WHERE status IN ('ready', 'review') "
        "AND assignee IS NOT NULL AND claim_lock IS NULL ORDER BY id"
    ).fetchall():
        task_id = row["id"]
        last_reset = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events WHERE task_id=? "
            "AND (kind IN ('claimed', 'spawned', 'requeued', 'unblocked', 'status') "
            "OR (kind='claim_rejected' AND ("
            "COALESCE(json_extract(payload, '$.reason'), '') != 'prior_worker_still_alive' "
            "OR COALESCE(json_extract(payload, '$.source_status'), 'ready') != ?)))",
            (task_id, row["status"]),
        ).fetchone()["m"]
        rejected = conn.execute(
            "SELECT MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
            "COUNT(*) AS n, (SELECT json_extract(e2.payload, '$.prev_pid') FROM task_events e2 "
            "WHERE e2.task_id=? AND e2.id>? AND e2.kind='claim_rejected' "
            "ORDER BY e2.id DESC LIMIT 1) AS pid "  # the LATEST refusal's pid (P2 #26)
            "FROM task_events WHERE task_id=? AND id>? AND kind='claim_rejected'",
            (task_id, last_reset, task_id, last_reset),
        ).fetchone()
        if (rejected["n"] and now - rejected["first_at"] > 15 * 60
                and now - rejected["last_at"] <= _RESPAWN_GUARD_STUCK_FRESH_SECONDS):
            out.append({
                "task_id": task_id, "assignee": row["assignee"],
                "reason": "prior_worker_still_alive",
                "status": row["status"],
                "guarded_since": rejected["first_at"],
                "guarded_seconds": now - rejected["first_at"],
                "guard_events": rejected["n"],
                "prev_pid": rejected["pid"],
                "clear_verb": render_operator_command(board, "show", task_id),
            })
        if row["status"] != "ready":
            continue  # active_pr is a ready-only respawn guard
        last_other = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events "
            f"WHERE task_id = ? AND (kind IN ({reset_marks}) "
            "OR (kind = 'respawn_guarded' "
            "AND COALESCE(json_extract(payload, '$.reason'), '') != 'active_pr'))",
            (task_id, *_RESPAWN_GUARD_STUCK_RESET_KINDS),
        ).fetchone()["m"]
        streak = conn.execute(
            "SELECT MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
            "COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND id > ? AND kind = 'respawn_guarded'",
            (task_id, int(last_other)),
        ).fetchone()
        if not streak or not streak["n"]:
            continue
        first_at = int(streak["first_at"])
        last_at = int(streak["last_at"])
        if now - first_at < min_seconds:
            continue
        if now - last_at > _RESPAWN_GUARD_STUCK_FRESH_SECONDS:
            continue
        newest = conn.execute(
            "SELECT json_extract(payload, '$.pr') AS pr, "
            "json_extract(payload, '$.hold') AS hold, "
            "json_extract(payload, '$.merge_state') AS merge_state, "
            "json_extract(payload, '$.pr_owned') AS pr_owned FROM task_events "
            "WHERE task_id = ? AND id > ? AND kind = 'respawn_guarded' "
            "ORDER BY id DESC LIMIT 1",
            (task_id, int(last_other)),
        ).fetchone()
        last_run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        out.append({
            "task_id": task_id,
            "assignee": row["assignee"],
            "reason": "active_pr",
            "pr": newest["pr"] if newest else None,
            # Why the newest guard decline held (``check_respawn_guard``
            # ``hold``): the gateway enqueues a land-request instead of
            # paging when it is the mergeable hold (t_5a9deed5).
            "hold": newest["hold"] if newest else None,
            "merge_state": newest["merge_state"] if newest else None,
            # True only when the guard saw the PR's head branch name this
            # card (``_pr_owned_by_card``); absent/false = page, never land.
            "pr_owned": bool(newest["pr_owned"]) if newest else False,
            "guarded_since": first_at,
            "guarded_seconds": now - first_at,
            "guard_events": int(streak["n"]),
            "last_outcome": last_run["outcome"] if last_run else None,
            "clear_verb": render_operator_command(board, "requeue", task_id, "<reason>"),
        })
    return out


def count_spawnable_demand(
    conn: sqlite3.Connection,
    *,
    default_assignee: Optional[str] = None,
    include_review: bool = False,
) -> int:
    """Upper bound on the spawns this board could use this tick.

    Ready (and, with ``include_review``, review) unclaimed tasks whose
    assignee is a real Hermes profile, plus unassigned ready tasks when
    ``default_assignee`` is set. Used by the gateway dispatcher to split one
    per-tick load-gate allowance across boards (t_f78d1938). Deliberately a
    superset of what :func:`dispatch_once` will actually spawn: an
    under-count here would give a board a zero quota.
    """
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        profile_exists = None
    statuses = ["ready"] + (["review"] if include_review else [])
    rows = conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        f"WHERE status IN ({','.join('?' * len(statuses))}) "
        "    AND claim_lock IS NULL GROUP BY assignee, status",
        tuple(statuses),
    ).fetchall()
    total = 0
    for row in rows:
        who = row["assignee"]
        if not who:
            if row["status"] == "ready" and default_assignee:
                total += int(row["n"])
            continue
        if profile_exists is None or profile_exists(who):
            total += int(row["n"])
    return total


def _normalize_dispatch_file_path(value: Any) -> Optional[str]:
    """Return a cheap, filesystem-free path representation for comparison."""
    if not isinstance(value, str):
        return None
    path = value.strip().strip("`'\"<>()[]{}").rstrip(",;")
    if not path or any(ch.isspace() for ch in path) or "://" in path:
        return None
    path = path.replace("\\", "/")
    path = re.sub(r":\d+(?::\d+)?$", "", path)
    path = re.sub(r"#L\d+(?:-L\d+)?$", "", path)
    while "//" in path:
        path = path.replace("//", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = [part for part in path.split("/") if part not in {"", ".", "~"}]
    if not parts:
        return None
    leaf = parts[-1]
    # Extensionless paths are accepted only when they have a directory shape.
    # This keeps real script paths such as scripts/claude-cpx while rejecting
    # lane vocabulary such as APX/BPX as fake file scope.
    if "." not in leaf:
        has_explicit_root = value.strip().startswith(("/", "~/", "./", "../"))
        if (
            len(parts) < 2
            or any(part.isupper() for part in parts)
            or (
                not has_explicit_root
                and parts[0] not in _DISPATCH_EXTENSIONLESS_PATH_ROOTS
            )
        ):
            return None
    return path


def _extract_explicit_dispatch_file_paths(body: Optional[str]) -> set[str]:
    """Extract only explicit path-like tokens from a new card body.

    This deliberately does not infer files from prose or symbols. Partial
    coverage is preferable to a broad heuristic that creates false confidence.
    """
    text = body or ""
    candidates: list[str] = []
    candidates.extend(match.group(1) for match in _DISPATCH_CODE_SPAN_RE.finditer(text))
    candidates.extend(match.group(0) for match in _DISPATCH_PATH_TOKEN_RE.finditer(text))
    candidates.extend(match.group(0) for match in _DISPATCH_BARE_FILE_RE.finditer(text))
    return {
        normalized
        for candidate in candidates
        if (normalized := _normalize_dispatch_file_path(candidate)) is not None
    }


def _changed_files_from_value(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        normalized
        for item in value
        if (normalized := _normalize_dispatch_file_path(item)) is not None
    }


def _extract_reported_changed_files(text: Optional[str]) -> set[str]:
    """Read structured ``changed_files`` arrays from handoff comment text."""
    if not text:
        return set()
    decoder = json.JSONDecoder()
    files: set[str] = set()
    for match in _DISPATCH_CHANGED_FILES_KEY_RE.finditer(text):
        tail = text[match.end():].lstrip()
        try:
            value, _end = decoder.raw_decode(tail)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        files.update(_changed_files_from_value(value))
    return files


def _load_dispatch_collision_scopes(
    conn: sqlite3.Connection,
) -> dict[str, set[str]]:
    """Load reported file scopes for active/recently-blocked cards in 3 reads."""
    cutoff = int(time.time()) - DISPATCH_COLLISION_RECENT_BLOCKED_SECONDS
    rows = conn.execute(
        "SELECT t.id FROM tasks t "
        "WHERE t.status IN ('running', 'review') "
        "OR (t.status = 'blocked' AND EXISTS ("
        "    SELECT 1 FROM task_events e "
        "    WHERE e.task_id = t.id AND e.kind = 'blocked' "
        "      AND e.created_at >= ?"
        "))",
        (cutoff,),
    ).fetchall()
    scopes: dict[str, set[str]] = {row["id"]: set() for row in rows}
    if not scopes:
        return scopes
    for row in conn.execute(
        "SELECT r.task_id, r.metadata FROM task_runs r "
        "JOIN tasks t ON t.id = r.task_id "
        "WHERE r.metadata IS NOT NULL AND ("
        "    t.status IN ('running', 'review') "
        "    OR (t.status = 'blocked' AND EXISTS ("
        "        SELECT 1 FROM task_events e "
        "        WHERE e.task_id = t.id AND e.kind = 'blocked' "
        "          AND e.created_at >= ?"
        "    ))"
        ")",
        (cutoff,),
    ):
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict):
            scope = scopes.get(row["task_id"])
            if scope is None:
                continue
            scope.update(
                _changed_files_from_value(metadata.get("changed_files"))
            )
    for row in conn.execute(
        "SELECT c.task_id, c.body FROM task_comments c "
        "JOIN tasks t ON t.id = c.task_id "
        "WHERE t.status IN ('running', 'review') "
        "OR (t.status = 'blocked' AND EXISTS ("
        "    SELECT 1 FROM task_events e "
        "    WHERE e.task_id = t.id AND e.kind = 'blocked' "
        "      AND e.created_at >= ?"
        "))",
        (cutoff,),
    ):
        scope = scopes.get(row["task_id"])
        if scope is not None:
            scope.update(_extract_reported_changed_files(row["body"]))
    return scopes


def _dispatch_paths_overlap(left: str, right: str) -> bool:
    """Compare normalized paths, allowing repo-relative suffix matches."""
    left_parts = tuple(
        part for part in left.split("/") if part not in {"", ".", "~"}
    )
    right_parts = tuple(
        part for part in right.split("/") if part not in {"", ".", "~"}
    )
    if not left_parts or not right_parts:
        return False
    if left_parts == right_parts:
        return True
    # A bare filename such as ``config.py`` is too weak to correlate with
    # every reported ``*/config.py`` path on the board. Exact bare-name
    # equality remains useful, but suffix matching needs directory context.
    if len(left_parts) < 2 or len(right_parts) < 2:
        return False
    short, long = (
        (left_parts, right_parts)
        if len(left_parts) <= len(right_parts)
        else (right_parts, left_parts)
    )
    return long[-len(short):] == short


def _dispatch_event_payload_seen(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict,
) -> bool:
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 20",
        (task_id, kind),
    ):
        try:
            if json.loads(row["payload"] or "null") == payload:
                return True
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return False


def _insert_dispatcher_comment(
    conn: sqlite3.Connection,
    task_id: str,
    body: str,
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_comments "
        "(task_id, author, body, run_id, session_ref, created_at) "
        "VALUES (?, 'kanban-dispatcher', ?, NULL, NULL, ?)",
        (task_id, body, now),
    )
    _append_event(
        conn,
        task_id,
        "commented",
        {"author": "kanban-dispatcher", "len": len(body)},
    )


def _record_dispatch_collision_warning(
    conn: sqlite3.Connection,
    task_id: str,
    collisions: list[tuple[str, list[str]]],
) -> None:
    payload = {
        "collisions": [
            {"task_id": other_id, "paths": paths}
            for other_id, paths in collisions
        ]
    }
    with write_txn(conn):
        if _dispatch_event_payload_seen(
            conn, task_id, "dispatch_collision_warning", payload
        ):
            return
        details = "; ".join(
            f"{other_id} on {', '.join(paths)}"
            for other_id, paths in collisions
        )
        _insert_dispatcher_comment(
            conn,
            task_id,
            "⚠️ DISPATCH COLLISION WARNING: "
            f"{task_id} overlaps {details}. Dispatch continues by design; "
            "coordinate before editing the shared file(s).",
        )
        _append_event(conn, task_id, "dispatch_collision_warning", payload)
        for other_id, paths in collisions:
            peer_payload = {"task_id": task_id, "paths": paths}
            if _dispatch_event_payload_seen(
                conn, other_id, "dispatch_collision_peer_warning", peer_payload
            ):
                continue
            _insert_dispatcher_comment(
                conn,
                other_id,
                "⚠️ DISPATCH COLLISION WARNING: newly dispatching card "
                f"{task_id} overlaps this card {other_id} on "
                f"{', '.join(paths)}. Dispatch continues by design; "
                "coordinate before editing the shared file(s).",
            )
            _append_event(
                conn,
                other_id,
                "dispatch_collision_peer_warning",
                peer_payload,
            )


def _record_dispatch_scope_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict,
) -> None:
    with write_txn(conn):
        if not _dispatch_event_payload_seen(conn, task_id, kind, payload):
            _append_event(conn, task_id, kind, payload)


def _check_dispatch_file_collisions(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    task_id: str,
    body: Optional[str],
    reported_scopes: Mapping[str, set[str]],
    dry_run: bool,
) -> None:
    """Warn on reported file overlap without preventing dispatch."""
    new_scope = _extract_explicit_dispatch_file_paths(body)
    candidates = {
        other_id: paths
        for other_id, paths in reported_scopes.items()
        if other_id != task_id
    }
    if not new_scope:
        result.collision_scope_unknown.append(task_id)
        _log.info(
            "KANBAN DISPATCH SCOPE UNKNOWN: %s has no explicit file paths; "
            "collision comparison was not possible",
            task_id,
        )
        if not dry_run:
            _record_dispatch_scope_event(
                conn,
                task_id,
                "dispatch_scope_unknown",
                {"reason": "no_explicit_file_paths"},
            )
        return

    unreported = sorted(
        other_id for other_id, paths in candidates.items() if not paths
    )
    if unreported:
        result.collision_scope_unreported.append((task_id, unreported))
        _log.info(
            "KANBAN DISPATCH COLLISION CHECK PARTIAL: %s could not be compared "
            "with cards lacking reported changed_files: %s",
            task_id,
            ", ".join(unreported),
        )
        if not dry_run:
            _record_dispatch_scope_event(
                conn,
                task_id,
                "dispatch_scope_partial",
                {"unchecked_task_ids": unreported},
            )

    collisions: list[tuple[str, list[str]]] = []
    for other_id, changed_files in candidates.items():
        overlap = sorted(
            changed_path
            for changed_path in changed_files
            if any(
                _dispatch_paths_overlap(new_path, changed_path)
                for new_path in new_scope
            )
        )
        if overlap:
            collisions.append((other_id, overlap))
    collisions.sort(key=lambda item: item[0])
    for other_id, paths in collisions:
        result.collision_warnings.append((task_id, other_id, paths))
        _log.warning(
            "KANBAN DISPATCH COLLISION WARNING: %s overlaps %s on %s; "
            "dispatch continues",
            task_id,
            other_id,
            ", ".join(paths),
        )
    if collisions and not dry_run:
        _record_dispatch_collision_warning(conn, task_id, collisions)


def count_running_by_placement(boards) -> "dict[str, tuple[int, dict[str, int]]]":
    """ONE pass per board: ``slug -> (running_total, running_remote_by_host)``.

    Both numbers come from the same connection, so they cannot disagree on
    which boards were read (KWLB PRD 5.2.4, RC-7). A board that fails to open
    or query is ABSENT (0 to both counts). Each DB file is counted once.
    """
    from hermes_cli import kanban_worker_pool as _kwp

    out: "dict[str, tuple[int, dict[str, int]]]" = {}
    seen: "set[str]" = set()
    for meta in enumerating_each(boards):
        slug = (meta.get("slug") if isinstance(meta, dict) else None) or DEFAULT_BOARD
        conn = None
        try:
            with enumerating_boards():
                path = str(kanban_db_path(slug).expanduser().resolve())
                if path in seen:
                    continue
                conn = connect(board=slug)
            # ONE read snapshot: a run that starts or ends between the two
            # queries cannot land in one count and not the other.
            conn.execute("BEGIN")
            try:
                total = _count_running_strict(conn)
                remote = _kwp.running_by_host(conn)
            finally:
                conn.execute("ROLLBACK")
        except Exception:
            # An unreadable board hides its running workers: report it as
            # UNKNOWN (total None) so the gate falls back to its floor instead
            # of over-admitting on an undercount (Prism r9 capacity undercount).
            out[slug] = (None, {})
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        seen.add(path)
        out[slug] = (int(total), dict(remote))
    return out


def merge_run_metadata(conn: sqlite3.Connection, run_id: int, values: dict,
                       *, keep_max: Iterable[str] = ()) -> None:
    """Merge ``values`` into one run's metadata JSON (other keys kept).

    A key in ``keep_max`` is a monotonic counter: inside the write txn the
    stored value becomes ``max(stored, incoming)``, so concurrent writers
    landing out of order can never lower it.
    """
    with write_txn(conn):
        row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        values = dict(values)
        for key in keep_max:
            old = meta.get(key)
            if key in values and isinstance(old, int) and not isinstance(old, bool):
                values[key] = max(old, values[key])
        meta.update(values)
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                     (json.dumps(meta, ensure_ascii=False), run_id))


def count_running_tasks_host() -> Optional[int]:
    """Total ``running`` tasks across EVERY board on this host.

    The load gate's host-wide worker count (t_ebbea874). The current board
    is counted in its own isolation domain, like every other board: an
    unreadable current board used to abort the whole count (``None``), so
    an idle small host with one corrupt board never got its empty-host
    admission floor and stopped dispatching the healthy boards too.

    Each DB file is counted once (``HERMES_KANBAN_DB`` pins every slug to one
    file). Returns ``None`` only when NO board could be read, so a caller
    that needs a KNOWN count (the empty-host floor) never guesses.
    """
    total, readable = 0, 0
    try:
        current_path: Optional[str] = str(
            kanban_db_path(board=None).expanduser().resolve()
        )
    except Exception:
        current_path = None
    try:
        conn = connect()
        try:
            total += _count_running_strict(conn)
            readable += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
    other, other_readable = _scan_running_tasks(
        {current_path} if current_path else set()
    )
    total += other
    readable += other_readable
    return total if readable else None


def _count_running_strict(conn: sqlite3.Connection) -> int:
    """``count_running_tasks`` that RAISES on a broken board (no fail-open)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    )


def _scan_running_tasks(skip_paths: "set[str]") -> "tuple[int, int]":
    """``(running_total, boards_read)`` over every non-archived board.

    Per-board failure isolation: a board that cannot be opened or queried
    contributes nothing and is not counted as read. Boards whose resolved DB
    path is in ``skip_paths``, or was already counted, are skipped.
    """
    try:
        boards = list_boards(include_archived=False)
    except Exception:
        return 0, 0
    seen = set(skip_paths)
    total, readable = 0, 0
    # Extent spans each loop body (belt and braces with the inner
    # ``enumerating_boards()`` below, which stays so a direct call to this
    # helper is covered too).
    for meta in enumerating_each(boards):
        slug = meta.get("slug") or DEFAULT_BOARD
        try:
            # Enumerating every board on disk, not addressing the named one.
            # The extent also covers the ``connect(board=slug)`` below, which
            # re-resolves the path internally.
            with enumerating_boards():
                path = kanban_db_path(board=slug).expanduser()
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                if not path.exists():
                    continue
                other = connect(board=slug)
            try:
                total += _count_running_strict(other)
                readable += 1
            finally:
                try:
                    other.close()
                except Exception:
                    pass
        except Exception:
            continue
    return total, readable


def _prefetch_pr_gates_for_tick(
    conn: sqlite3.Connection, *, dry_run: bool = False,
):
    """Perform bounded GitHub I/O before the dispatcher writer lock."""
    if dry_run:
        return None
    try:
        from hermes_cli import kanban_pr_gate

        return kanban_pr_gate.prefetch_pr_gate_states(conn)
    except Exception as exc:
        if type(exc).__name__ == "SandboxEscape":
            raise  # see _reevaluate_pr_gates_for_tick: never absorbed.
        _log.warning(
            "kanban dispatch: PR-gate prefetch failed (%s: %s); "
            "continuing this tick without gate mutation",
            type(exc).__name__, exc,
        )
        return None


def _drop_no_worker_rows(
    conn: sqlite3.Connection,
    rows: list,
    result: "DispatchResult",
    *,
    dry_run: bool,
) -> list:
    """Remove ``no_worker`` cards from a dispatch candidate list.

    Each dropped id lands in ``result.skipped_no_worker``. A real tick also
    records ``dispatch_skipped {"reason": "no_worker"}`` once per flagging
    (not once per tick); a dry run writes nothing.
    """
    kept = []
    for row in rows:
        if not row["no_worker"]:
            kept.append(row)
            continue
        result.skipped_no_worker.append(row["id"])
        if not dry_run:
            _record_no_worker_skip_once(conn, row["id"])
    return kept


def _record_no_worker_skip_once(conn: sqlite3.Connection, task_id: str) -> None:
    try:
        with write_txn(conn):
            last_set = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events "
                "WHERE task_id = ? AND kind = 'no_worker_set'",
                (task_id,),
            ).fetchone()[0]
            seen = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? "
                "AND kind = 'dispatch_skipped' AND id > ? "
                "AND payload LIKE '%\"no_worker\"%' LIMIT 1",
                (task_id, last_set),
            ).fetchone()
            if seen is None:
                _append_event(
                    conn, task_id, "dispatch_skipped", {"reason": "no_worker"},
                )
                _log.info(
                    "kanban dispatch: %s is no_worker (operator-only); not spawned",
                    task_id,
                )
    except Exception:
        _log.debug(
            "kanban dispatch: could not record no_worker skip for %s",
            task_id, exc_info=True,
        )


def _reevaluate_pr_gates_for_tick(
    conn: sqlite3.Connection,
    result: "DispatchResult",
    *,
    dry_run: bool = False,
    prefetched=None,
) -> None:
    """Resolve blocked cards whose external PR gate has already been satisfied.

    Fail-open by construction: any exception is logged and dispatch continues.
    A diagnostic that can brick the dispatcher is worse than the stale-block
    class it exists to close. ``dry_run`` skips it entirely — a dry run must
    not mutate the board.
    """
    if dry_run or prefetched is None:
        return
    try:
        from hermes_cli import kanban_pr_gate

        for outcome in kanban_pr_gate.reevaluate_pr_gates(
            conn, prefetched=prefetched,
        ):
            if outcome.action == "unblocked":
                result.gate_auto_resolved.append(outcome.task_id)
            elif outcome.action == "closed_unmerged":
                result.gate_closed_unmerged.append(outcome.task_id)
    except Exception as exc:
        # A sandbox escape is NOT an ordinary fault to absorb: it means a
        # harness with a fabricated PR oracle is pointed at a real board. The
        # fail-open policy below exists so a diagnostic cannot brick dispatch;
        # applying it here would instead reduce a loud, actionable refusal to a
        # log line the harness author never reads (2026-09-21). Re-raise.
        if type(exc).__name__ == "SandboxEscape":
            raise
        _log.warning(
            "kanban dispatch: PR-gate re-evaluation failed (%s: %s); "
            "continuing this tick",
            type(exc).__name__, exc,
        )
WORKER_CPU_PRIORITY_DEFAULT = "background"
WORKER_BACKGROUND_NICE = 19


def worker_cpu_priority_config(
    kanban_cfg: Optional[dict] = None,
) -> "tuple[str, int]":
    """Return ``(mode, nice_value)`` for dispatcher-spawned worker gateways.

    ``background`` (the default) runs each worker at ``nice 19`` so batch
    worker load can never outbid a RESIDENT gateway on the same host for CPU.
    Because niceness is inherited across ``fork``/``exec``, deprioritising the
    worker gateway deprioritises everything it later spawns — its terminal-tool
    children included, which is the seam the 2026-09-20 incident escaped
    through (a worker's 384 orphaned busy-loops took the host to load 538 on 32
    cores and starved the resident gateway's event loop into two watchdog
    hard-exits).

    ``normal`` opts out and leaves the child at the dispatcher's own priority.
    An unrecognised value falls back to ``background``: this knob guards
    interactive responsiveness, so a typo must fail SAFE (still niced), never
    silently re-arm the incident.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    raw = (kanban_cfg or {}).get("worker_cpu_priority", WORKER_CPU_PRIORITY_DEFAULT)
    mode = str(raw or "").strip().lower()
    if mode == "normal":
        return ("normal", 0)
    if mode == "idle":
        return ("idle", WORKER_BACKGROUND_NICE)
    return ("background", WORKER_BACKGROUND_NICE)

# macOS: nice(2) only reorders threads INSIDE one scheduling class. Darwin's
# real levers are the task QoS clamp and the darwin-BG flag, which also set
# the disk I/O tier. Both are inherited across fork/exec. The QoS clamp can
# only be applied at spawn time (posix_spawnattr_set_qos_clamp_np) — there is
# no setpriority() form — so it rides as an exec-form ``taskpolicy`` prefix:
# taskpolicy execs the worker in place (same pid, same argv once running).
#
# Measured on the M3 Ultra, 2026-09-24 (16 spinners x 5s, 61% host idle):
#   unclamped            15.9 cores   iotier 0 (IMPORTANT)
#   -c utility            3.8 cores   iotier 1 (STANDARD), P+E cores
#   -b (darwin BG)        0.1 cores   iotier 2, E-cores only
# "background" = utility clamp: batch work sits strictly below an
# Interactive gateway without collapsing worker throughput. "idle" = darwin
# BG for hosts where the gateway must win at any cost to worker speed.
WORKER_DARWIN_TASKPOLICY = "/usr/sbin/taskpolicy"
_WORKER_DARWIN_POLICY_ARGS = {
    "background": ("-c", "utility"),
    "idle": ("-b",),
}


def worker_darwin_qos_prefix(mode: str) -> "list[str]":
    """Return the exec-form ``taskpolicy`` argv prefix for *mode*, or ``[]``.

    Empty off macOS, for ``normal``, and when ``taskpolicy`` is missing — a
    missing wrapper must degrade to nice-only, never refuse the spawn.
    """
    args = _WORKER_DARWIN_POLICY_ARGS.get(mode)
    if not args or sys.platform != "darwin":
        return []
    if not os.access(WORKER_DARWIN_TASKPOLICY, os.X_OK):
        return []
    return [WORKER_DARWIN_TASKPOLICY, *args]


def _build_worker_priority_preexec(nice_value: int):
    """Return a ``preexec_fn`` that deprioritises the child, or ``None``.

    Runs in the forked child between ``fork`` and ``exec``, so the setting is
    part of the process image the worker gateway execs into and is inherited by
    every descendant it spawns. Windows has no ``preexec_fn`` (and no POSIX
    priority API), so it gets ``None`` — the knob is a no-op there.

    Every call inside is best-effort: a hardened container may deny
    ``sched_setscheduler`` while allowing ``setpriority``, and a preexec hook
    that raises aborts the spawn. Refusing to start the worker would be a worse
    outcome than running it at normal priority, so failures degrade silently.
    """
    if _IS_WINDOWS or nice_value <= 0 or not hasattr(os, "setpriority"):
        return None

    def _apply_background_priority() -> None:  # pragma: no cover - child side
        try:
            os.setpriority(os.PRIO_PROCESS, 0, nice_value)
        except Exception:
            pass
        # Linux only: SCHED_IDLE yields the CPU to any runnable normal-class
        # task, which is strictly stronger than nice 19 under heavy load.
        sched_idle = getattr(os, "SCHED_IDLE", None)
        setscheduler = getattr(os, "sched_setscheduler", None)
        if sched_idle is not None and setscheduler is not None:
            try:
                setscheduler(0, sched_idle, os.sched_param(0))
            except Exception:
                pass

    return _apply_background_priority


def _kanban_worker_skill_available(hermes_home: Optional[str]) -> bool:
    """True if the bundled ``kanban-worker`` skill resolves for the home the
    spawned worker will run under.

    The dispatcher injects ``--skills kanban-worker`` into every worker. When
    the worker activates a profile (``hermes -p <name>``), its ``SKILLS_DIR``
    becomes ``<profile_home>/skills`` — which on many profiles does NOT contain
    the bundled skill (it ships in the *default* root home, not every
    profile-scoped skills dir). Preloading a missing skill is fatal at CLI
    startup (``ValueError: Unknown skill(s): kanban-worker``), aborting the
    worker before the agent loop runs. Gate the flag on actual resolvability;
    the kanban lifecycle contract is still injected via ``KANBAN_GUIDANCE``, so
    omitting the flag only drops the supplementary pattern library.
    """
    from pathlib import Path as _Path
    from hermes_cli.kanban_skill_resolve import profile_skill_dirs, skill_resolves

    base = _Path(hermes_home) if hermes_home else (_Path.home() / ".hermes")
    # Same roots the worker's skill_view walks: <home>/skills AND
    # skills.external_dirs, with .archive/.hub/support dirs excluded. A bare
    # ``rglob`` of <home>/skills missed external dirs (fleets that ship
    # kanban-worker from a shared dir never got the flag) and matched archived
    # copies the worker cannot load.
    try:
        return skill_resolves("kanban-worker", profile_skill_dirs(base))
    except Exception:
        return False


def _card_skills_refused(conn, task_id, assignee, result, *, dry_run) -> bool:
    """Block a card whose ``skills`` would crash the assignee's worker.

    The worker resolves ``--skills`` against its own profile home. Its
    contract (``cli.py`` ``finalize_preloaded_skills`` / ``tui_gateway``
    ``server.py``) is all-or-nothing: when NO requested skill loads it exits 1
    at startup (``Unknown skill(s)``), the crash counter retries it and gives
    up (t_0b786d9b, three spawns); when at least one loads it logs a warning
    and runs without the rest. ``_default_spawn`` adds ``--skills
    kanban-worker`` whenever that skill resolves for the profile, so that
    counts as a loaded skill too.

    This mirrors the split exactly: a card whose worker would crash is blocked
    (kind=capability) with the missing skill and the directory it lives in,
    before any run is claimed; a card whose worker would run degraded is
    dispatched and gets ONE dispatcher comment naming what it runs without
    (the worker's own warning only reaches its log). Fails open on any error.
    """
    task = get_task(conn, task_id)
    if task is None or not task.skills or not assignee:
        return False
    try:
        from pathlib import Path as _Path
        from hermes_cli.kanban_skill_resolve import (
            degraded_reason,
            refusal_reason,
            unresolved_skills,
        )
        from hermes_cli.profiles import get_profile_dir

        home = _Path(get_profile_dir(assignee))
        if not home.is_dir():
            return False
        extra = []
        if task.workspace_path and (task.workspace_kind or "scratch") != "scratch":
            ws = _Path(task.workspace_path).expanduser()
            extra = [ws / ".hermes" / "skills", ws / ".agents" / "skills"]
        # The exact ``--skills`` list the worker gets (see ``_default_spawn``):
        # kanban-worker when it resolves for the profile, then the card's
        # skills minus kanban-worker.
        names = [s for s in dict.fromkeys(task.skills) if s and s != "kanban-worker"]
        injected = ["kanban-worker"] if _kanban_worker_skill_available(str(home)) else []
        missing = unresolved_skills(names, home, extra_dirs=extra)
        if not missing:
            return False
        # Unjudgeable identifiers (plugin-qualified, absolute) never appear in
        # ``missing`` and so count as loadable here -- failing open.
        loaded = injected + [n for n in names if n not in missing]
        try:
            root_home = _Path(get_profile_dir("default"))
        except Exception:
            root_home = None
        if loaded:
            reason = degraded_reason(assignee, home, missing, root_home, loaded)
        else:
            reason = refusal_reason(assignee, home, missing, root_home)
    except Exception:
        _log.debug("kanban dispatch: card skill check failed open for %s",
                   task_id, exc_info=True)
        return False
    if loaded:
        result.skill_degraded.append((task_id, missing))
        if dry_run:
            return False
        _log.warning("PHASE=kanban_skill_degraded task=%s assignee=%s missing=%s loaded=%s",
                     task_id, assignee, ",".join(missing), ",".join(loaded))
        already_logged = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body = ? LIMIT 1",
            (task_id, reason),
        ).fetchone()
        if not already_logged:
            with write_txn(conn):
                add_comment(conn, task_id, "dispatcher", reason)
                _append_event(conn, task_id, "skill_degraded",
                              {"assignee": assignee, "missing": missing,
                               "loaded": loaded})
        return False
    result.skill_refused.append((task_id, missing))
    if dry_run:
        return True
    _log.warning("PHASE=kanban_skill_refused task=%s assignee=%s missing=%s",
                 task_id, assignee, ",".join(missing))
    if block_task(conn, task_id, reason=reason, kind="capability"):
        with write_txn(conn):
            _append_event(conn, task_id, "skill_refused",
                          {"assignee": assignee, "missing": missing})
    return True


@dataclass
class LaneModelOverride:
    """An active board-level model route with an expiry on the dispatcher clock."""

    assignee: Optional[str]
    provider: str
    model: str
    reasoning_effort: Optional[str] = None
    reason: Optional[str] = None
    firepower: Optional[str] = None
    created_by: Optional[str] = None
    created_at: int = 0
    expires_at: int = 0
    pin_sub_reason: Optional[str] = None

    @property
    def route(self) -> str:
        return f"{self.provider}/{self.model}"

    def ttl_remaining(self, now: Optional[int] = None) -> int:
        """Seconds left before the dispatcher stops applying this row."""

        current = int(time.time()) if now is None else int(now)
        return max(0, int(self.expires_at) - current)


def _lane_model_row(row) -> LaneModelOverride:
    return LaneModelOverride(
        assignee=(row["assignee"] or None),
        provider=row["provider"],
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        reason=row["reason"],
        firepower=row["firepower"],
        created_by=row["created_by"],
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
        pin_sub_reason=(
            row["pin_sub_reason"] if "pin_sub_reason" in row.keys() else None
        ) or None,
    )


def set_lane_model_override(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    expires_at: int,
    reasoning_effort: Optional[str] = None,
    reason: Optional[str] = None,
    assignee: Optional[str] = None,
    firepower: Optional[str] = None,
    created_by: Optional[str] = None,
    now: Optional[int] = None,
    pin_sub_reason: Optional[str] = None,
) -> LaneModelOverride:
    """Install (or replace) the lane override for ``assignee``.

    ``pin_sub_reason`` (``--pin-sub``) authorizes a claude-bpx-N / apx-N lane;
    without it a single-sub lane is refused exactly as on a card.

    ``assignee=None`` sets the board-wide lane. Re-setting the same lane is an
    upsert, so an operator extending a window never stacks duplicate rows.
    """

    from hermes_cli.model_policy import pin_sub_arg_error, validate_route_provider

    pin_sub_error = pin_sub_arg_error(model, provider, pin_sub_reason)
    if pin_sub_error:
        raise ValueError(pin_sub_error)
    pin_sub_reason = (pin_sub_reason or "").strip() or None
    validate_route_provider(model, provider, pin_sub_reason=pin_sub_reason)
    created = int(time.time()) if now is None else int(now)
    key = (assignee or "").strip()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO lane_model_overrides "
            "(assignee, provider, model, reasoning_effort, reason, firepower, created_by, "
            " created_at, expires_at, pin_sub_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(assignee) DO UPDATE SET "
            "  provider=excluded.provider, model=excluded.model, "
            "  reasoning_effort=excluded.reasoning_effort, "
            "  reason=excluded.reason, firepower=excluded.firepower, "
            "  created_by=excluded.created_by, created_at=excluded.created_at, "
            "  expires_at=excluded.expires_at, pin_sub_reason=excluded.pin_sub_reason",
            (
                key, provider, model, reasoning_effort, reason, firepower, created_by,
                created, int(expires_at), pin_sub_reason,
            ),
        )
    return LaneModelOverride(
        assignee=(key or None), provider=provider, model=model,
        reasoning_effort=reasoning_effort, reason=reason, firepower=firepower, created_by=created_by,
        created_at=created, expires_at=int(expires_at), pin_sub_reason=pin_sub_reason,
    )


def get_lane_model_override(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[LaneModelOverride]:
    """Return the active override for ``assignee``, or None.

    An assignee-specific row beats the board-wide row. Expiry is exclusive —
    a row is live strictly before ``expires_at`` — so the TTL boundary belongs
    to the profile default and a 0-second window is never "briefly active".
    """

    current = int(time.time()) if now is None else int(now)
    row = conn.execute(
        "SELECT * FROM lane_model_overrides "
        "WHERE assignee IN (?, '') AND expires_at > ? "
        # '' (board-wide) sorts before any real name, so DESC puts the
        # assignee-specific row first when both lanes are active.
        "ORDER BY assignee DESC LIMIT 1",
        ((assignee or "").strip(), current),
    ).fetchone()
    return _lane_model_row(row) if row else None


def list_lane_model_overrides(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    include_expired: bool = False,
) -> list[LaneModelOverride]:
    """All lane overrides, active-only by default, board-wide lane first."""

    current = int(time.time()) if now is None else int(now)
    if include_expired:
        rows = conn.execute(
            "SELECT * FROM lane_model_overrides ORDER BY assignee ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM lane_model_overrides WHERE expires_at > ? "
            "ORDER BY assignee ASC",
            (current,),
        ).fetchall()
    return [_lane_model_row(row) for row in rows]


def clear_lane_model_override(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
) -> Optional[LaneModelOverride]:
    """Drop the lane override for ``assignee``; return what was removed."""

    key = (assignee or "").strip()
    # Read and delete under ONE writer lock (same rule as
    # clear_all_lane_model_overrides): a row read before the lock can be
    # replaced by a concurrent ``lane-model set`` whose new window this call
    # would then delete while reporting the old one.
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM lane_model_overrides WHERE assignee = ?", (key,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM lane_model_overrides WHERE assignee = ?", (key,))
    return _lane_model_row(row)


def clear_all_lane_model_overrides(
    conn: sqlite3.Connection,
) -> list[LaneModelOverride]:
    """Drop EVERY lane override in one transaction; return what was removed.

    Same atomicity rule as :func:`apply_batch_route_writes`: ``lane-model
    clear --all`` is one operator action over N rows, so looping over the
    single-row clear (which commits each delete separately) could both leave
    the board half-cleared and report rows that a concurrent writer had
    already removed. Reading and deleting under one ``write_txn`` makes the
    returned list the rows this call actually deleted.
    """

    with write_txn(conn):
        rows = [
            _lane_model_row(row)
            for row in conn.execute("SELECT * FROM lane_model_overrides").fetchall()
        ]
        if rows:
            conn.execute("DELETE FROM lane_model_overrides")
    return rows


def expire_lane_model_overrides(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
) -> list[LaneModelOverride]:
    """Delete every elapsed override and return them.

    The dispatcher calls this once per tick. Because the rows are DELETED,
    a given expiry is reported exactly once — the tick that retires the
    override logs ``lane-model expired``, later ticks stay quiet.
    """

    current = int(time.time()) if now is None else int(now)
    # Cheap unlocked probe so an idle tick never takes the writer lock.
    if conn.execute(
        "SELECT 1 FROM lane_model_overrides WHERE expires_at <= ? LIMIT 1",
        (current,),
    ).fetchone() is None:
        return []
    # The report is read under the same writer lock as the DELETE: a window
    # renewed between an unlocked read and the delete must be neither
    # deleted nor announced as expired.
    with write_txn(conn):
        rows = conn.execute(
            "SELECT * FROM lane_model_overrides WHERE expires_at <= ? "
            "ORDER BY assignee ASC",
            (current,),
        ).fetchall()
        if rows:
            conn.execute(
                "DELETE FROM lane_model_overrides WHERE expires_at <= ?", (current,),
            )
    return [_lane_model_row(row) for row in rows]


def lane_successor_label(override: Optional[LaneModelOverride]) -> str:
    """Name what routes a lane once an override row is gone.

    ``override`` is the still-active lookup for that lane (see
    :func:`get_lane_model_override`): None means the profile default, and a
    surviving row — the board-wide lane under a retired assignee lane, or a
    renewed window — is named with its route so receipts never claim
    ``profile default`` while a lane is still routing the cards.
    """

    if override is None:
        return "profile default"
    scope = f"lane {override.assignee}" if override.assignee else "board-wide lane"
    return f"{scope} {override.route}"


def apply_lane_model_override(
    task: Task,
    override: Optional[LaneModelOverride],
    *,
    now: Optional[int] = None,
) -> Optional[str]:
    """Route ``task`` through ``override`` when it has no override of its own.

    Returns the ``source`` label for the spawn's route line, or ``None`` when
    the lane override did not apply. A per-card override always wins: the card
    is the narrower, explicitly-chosen instruction, and silently overwriting it
    from a board-wide row would make ``set-model`` unreliable.
    """

    if override is None:
        return None
    if task.model_override:
        return None
    task.model_override = override.model
    task.provider_override = override.provider
    if task.reasoning_effort is None:
        task.reasoning_effort = override.reasoning_effort
    source = f"lane-override({override.ttl_remaining(now)}s remaining)"
    if override.pin_sub_reason:
        # In-memory only, like the route itself: the lane pin governs this
        # spawn's admission (wait, never a silent profile fallback).
        task.pin_sub_reason = override.pin_sub_reason
        task.pin_sub_fallback = False
        return f"pin({source})"
    return source


def _pin_route_source(task: Task, source: Optional[str]) -> Optional[str]:
    """``pin`` for a card's deliberate single-sub pin (t_957ca870), so the
    dispatcher route line reads ``route=claude-bpx-N/<model> source=pin``.
    Lane pins already carry ``pin(lane-override(...))``."""
    from hermes_cli.model_policy import is_sub_pin_route

    if (source == "card-override" and task.pin_sub_reason
            and is_sub_pin_route(task.model_override, task.provider_override)):
        return "pin"
    return source


def effective_worker_route(task: Task) -> str:
    """Return the provider/model route a dispatcher spawn will use.

    Card overrides win. Otherwise read the assignee profile's canonical model
    block directly, matching the profile activated by ``_default_spawn``.
    The helper is fail-soft because route announcement must never prevent a
    worker from spawning.
    """

    model = task.model_override
    provider = task.provider_override
    if model:
        try:
            from hermes_cli.kanban_provider_health import model_override

            model, provider = model_override(task)
        except Exception:
            pass
    elif task.assignee:
        try:
            from pathlib import Path as _Path
            from hermes_cli.profiles import _read_config_model, resolve_profile_env

            model, provider = _read_config_model(
                _Path(resolve_profile_env(task.assignee))
            )
        except Exception:
            model = provider = None
    model_label = str(model or "unknown").strip() or "unknown"
    provider_label = str(provider or "unknown").strip() or "unknown"
    qualified_prefix = f"{provider_label}/"
    if provider_label != "unknown" and model_label.startswith(qualified_prefix):
        model_label = model_label[len(qualified_prefix):]
    return f"{provider_label}/{model_label}"


def _native_worker_argv(task: Task, profile_home: Optional[str]) -> list[str]:
    """argv for a native foreign-lane worker (``foreign_lane.worker_command``).

    The runner ignores its own flags. ``-m/--provider/--reasoning`` state the
    route this dispatcher resolved (card override, else the profile's model,
    exactly as a shim's argv would), because the lane's model gate reads its
    parent's argv to catch a card re-pinned between spawn and harness start.
    """
    argv = [sys.executable, "-m", "hermes_cli.kanban_native_worker"]
    model = provider = None
    if task.model_override:
        from hermes_cli.kanban_provider_health import model_override

        model, provider = model_override(task)
    elif profile_home:
        from hermes_cli.profiles import _read_config_model

        model, provider = _read_config_model(Path(profile_home))
    if model:
        argv.extend(["-m", str(model)])
        if provider:
            argv.extend(["--provider", str(provider)])
    if task.reasoning_effort:
        argv.extend(["--reasoning", task.reasoning_effort])
    return argv
_SHIM_MODEL_FAMILY_RANK = (("haiku", 1), ("sonnet", 2), ("opus", 3))
SHIM_MODEL_CAPPED_FROM_ENV = "HERMES_KANBAN_SHIM_MODEL_CAPPED_FROM"
# Per-card harness brain as claimed (t_a8f335c5); read by the lane runner.
CARD_BRAIN_ENV = "HERMES_KANBAN_CARD_BRAIN"
_SHIM_MODEL_ID_RE = re.compile(r"claude-(haiku|sonnet|opus)-\d[0-9a-z.-]*$")


def _shim_model_rank(model: Optional[str]) -> Optional[int]:
    """Rank a Claude model id by family (haiku < sonnet < opus); None = unknown.

    Only the bare id after the last ``/`` is read, so ``provider/model`` forms
    rank the same as the bare model. Unknown families never rank, so a cap
    never fires on a model the dispatcher cannot place (fail-open to today).
    """
    name = str(model or "").rsplit("/", 1)[-1].lower()
    # Only recognized Claude model-id shapes rank (``claude-<family>-<digit>``
    # e.g. claude-opus-5, claude-sonnet-4-5, claude-haiku-4-5-20251001).
    # A custom alias (``acme/opus-v1``, ``claude-custom-opus-v1``) never ranks,
    # so it is never swapped for a Claude model on the card's provider.
    m = _SHIM_MODEL_ID_RE.match(name)
    if not m:
        return None
    return dict(_SHIM_MODEL_FAMILY_RANK)[m.group(1)]


def _read_shim_model_cap(hermes_home: Optional[str]) -> Optional[str]:
    """``foreign_lane.shim_model_cap`` from the assignee profile's config.yaml.

    Only honoured on a foreign-lane shim profile, i.e. one whose
    ``foreign_lane.harness`` is set; any other profile returns None.

    Read raw at spawn time (no restart to flip). Unset, empty, unreadable or
    malformed all return None = today's behaviour (the shim runs the card's
    model). Fail-soft: a bad config must never block a spawn.
    """
    if not hermes_home:
        return None
    try:
        # Presence-sensitive read of ANOTHER profile's file (unset = no cap),
        # so the raw owner primitive + env expansion, not load_config() (which
        # reads this process's home and merges defaults).
        from hermes_cli.config import _expand_env_vars, read_user_config_raw

        path = Path(hermes_home) / "config.yaml"
        if not path.is_file():
            return None
        cfg = _expand_env_vars(read_user_config_raw(path))
        # Not a kanban.* key: a profile's foreign_lane section. The local name
        # must not collide with a name test_kanban_config_keys binds to
        # .get("kanban") (its scan is file-wide, not scope-aware).
        foreign_lane_cfg = cfg.get("foreign_lane") if isinstance(cfg, dict) else None
        if not isinstance(foreign_lane_cfg, dict):
            return None
        # Scope: only a real foreign-lane shim profile (one that names its
        # harness) caps. Any other profile's tasks keep the card's model.
        harness = foreign_lane_cfg.get("harness")
        if not (isinstance(harness, str) and harness.strip()):
            return None
        cap = foreign_lane_cfg.get("shim_model_cap")
        cap = str(cap).strip() if isinstance(cap, str) else ""
        return cap or None
    except Exception as exc:
        _log.debug("kanban spawn: shim_model_cap unreadable at %r (%s)", hermes_home, exc)
        return None


def _apply_shim_model_cap(
    model: str, cap: Optional[str], provider: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Return ``(model_to_spawn, capped_from)``.

    ``capped_from`` is the card's model when the cap applied, else None. The
    cap applies only when both ids rank and the card's model ranks strictly
    above the cap; an equal or lower model, or an unrankable one, is untouched.
    The spawn keeps the card's provider, so a provider-qualified cap
    (``<provider>/<model>``) applies only when that provider IS the card's
    effective provider; a different one is refused (logged, not capped)
    rather than spawning a model the provider may not serve.
    """
    if not cap:
        return model, None
    cap_provider, sep, cap_model = cap.rpartition("/")
    if sep:
        if cap_provider != (provider or ""):
            _log.warning(
                "kanban spawn: shim_model_cap %r ignored: its provider is not "
                "the card's provider %r", cap, provider,
            )
            return model, None
        cap = cap_model
    have, limit = _shim_model_rank(model), _shim_model_rank(cap)
    if have is None or limit is None or have <= limit:
        return model, None
    return cap, model


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------
def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Everything a worker should read about its task: header, body,
    attachments, prior attempts, done-parent handoffs, the assignee's recent
    work, comments. Lists are tail-capped and fields char-capped
    (``_CTX_MAX_*``) so the prompt stays bounded on pathological boards."""
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    # One clock reading so every relative age in this rendering agrees.
    now = int(time.time())
    lines: list[str] = []
    _ctx_header(lines, task)
    _ctx_attachments(lines, list_attachments(conn, task_id))
    _ctx_prior_attempts(lines, conn, task_id, now)
    _ctx_parent_results(lines, conn, task_id, now)
    _ctx_role_history(lines, conn, task, now)
    _ctx_comments(lines, list_comments(conn, task_id), now)
    return "\n".join(lines).rstrip() + "\n"


def _ctx_cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate to ``limit`` chars with a visible ellipsis."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def _ctx_stamp(ts: int, now: int) -> str:
    """``YYYY-MM-DD HH:MM`` plus a relative age when one is available."""
    disp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    age = _relative_age(ts, now)
    return f"{disp}, {age}" if age else disp


def _ctx_metadata_line(metadata: Any) -> Optional[str]:
    if not metadata:
        return None
    try:
        return f"_metadata_: `{_ctx_cap(json.dumps(metadata, ensure_ascii=False, sort_keys=True))}`"
    except Exception:
        return None


def _ctx_tail(items: list, cap: int, noun: str) -> tuple[list, Optional[str]]:
    """Keep the newest ``cap`` items; describe the omitted head, if any."""
    omitted = max(0, len(items) - cap)
    if not omitted:
        return items, None
    return items[-cap:], (
        f"_({omitted} earlier {noun}{'s' if omitted != 1 else ''} "
        f"omitted; showing most recent {cap})_"
    )


def _ctx_header(lines: list[str], task: Task) -> None:
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")
    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_ctx_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")


def _ctx_attachments(lines: list[str], attachments: list[Attachment]) -> None:
    """Absolute on-disk paths so the worker's file tools read them directly
    (remote terminal backends need the attachments dir mounted)."""
    if not attachments:
        return
    lines.append("## Attachments")
    lines.append(
        "Files attached to this task. Read them with the file/terminal "
        "tools at the absolute paths below:"
    )
    for att in attachments:
        size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
        size_str = f", {size_kb} KB" if size_kb else ""
        ctype = f", {att.content_type}" if att.content_type else ""
        lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
    lines.append("")


def _ctx_prior_attempts(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Closed runs on this task (the active run is this worker), newest
    ``_CTX_MAX_PRIOR_ATTEMPTS`` in full, older ones as a one-line marker."""
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    shown, omitted_note = _ctx_tail(all_prior, _CTX_MAX_PRIOR_ATTEMPTS, "attempt")
    if not shown:
        return
    first_shown_idx = len(all_prior) - len(shown) + 1
    lines.append("## Prior attempts on this task")
    if omitted_note:
        lines.append(omitted_note)
    for offset, run in enumerate(shown):
        profile = run.profile or "(unknown)"
        outcome = run.outcome or run.status
        lines.append(
            f"### Attempt {first_shown_idx + offset} — {outcome} ({profile}, {_ctx_stamp(run.started_at, now)})"
        )
        if run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        if run.error and run.error.strip():
            lines.append(f"_error_: {_ctx_cap(run.error)}")
        meta_line = _ctx_metadata_line(run.metadata)
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_parent_results(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Done-parent handoffs: newest ``completed`` run's summary+metadata,
    falling back to ``task.result`` for pre-runs-table data. Stamped with a
    relative age so the worker re-verifies stale upstream results."""
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task_id,),
    ).fetchall()
    wrote_header = False
    for pid in (r["parent_id"] for r in parent_rows):
        pt = get_task(conn, pid)
        if not pt or pt.status != "done":
            continue
        runs = [r for r in list_runs(conn, pid) if r.outcome in SUCCESS_RUN_OUTCOMES]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        run = runs[0] if runs else None
        if not wrote_header:
            lines.append("## Parent task results")
            lines.append(
                "_Handoffs from upstream tasks, captured when each parent "
                "completed (see age below). These are point-in-time "
                "snapshots, not live state — if a result drives your "
                "current work and it's not recent, re-verify against the "
                "source before acting on it as current._"
            )
            wrote_header = True
        done_ts = run.ended_at if run is not None and run.ended_at else (pt.completed_at or None)
        age = _relative_age(done_ts, now)
        lines.append(f"### {pid}" + (f" (completed {age})" if age else ""))
        if run is not None and run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        elif pt.result:
            lines.append(_ctx_cap(pt.result))
        else:
            lines.append("(no result recorded)")
        meta_line = _ctx_metadata_line(run.metadata) if run is not None else None
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_role_history(lines: list[str], conn: sqlite3.Connection, task: Task, now: int) -> None:
    """The assignee's 5 most recent completed runs on OTHER tasks — implicit
    role continuity without wiring anything into SOUL.md / MEMORY.md."""
    if not task.assignee:
        return
    role_rows = conn.execute(
        "SELECT t.id, t.title, r.summary, r.ended_at "
        "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
        "WHERE r.profile = ? AND r.task_id != ? "
        "  AND r.outcome IN " + _SUCCESS_RUN_OUTCOMES_SQL + " "
        "ORDER BY r.ended_at DESC LIMIT 5", (task.assignee, task.id),
    ).fetchall()
    if not role_rows:
        return
    lines.append(f"## Recent work by @{task.assignee}")
    for row in role_rows:
        first = _first_line(row["summary"], 200) or "(no summary)"
        lines.append(
            f"- {row['id']} — {row['title']} ({_ctx_stamp(int(row['ended_at']), now)}): {first}"
        )
    lines.append("")


def _ctx_comments(lines: list[str], comments: list[Comment], now: int) -> None:
    """Newest ``_CTX_MAX_COMMENTS`` comments. The explicit "comment from
    worker" framing stops an operator-controlled HERMES_PROFILE like
    "hermes-system" being read as a system directive above an
    attacker-influenceable body (defense-in-depth)."""
    shown, omitted_note = _ctx_tail(comments, _CTX_MAX_COMMENTS, "comment")
    if not shown:
        return
    lines.append("## Comment thread")
    if omitted_note:
        lines.append(omitted_note)
    for c in shown:
        # Render author with explicit "comment from worker" framing so operator-controlled HERMES_PROFILE
        # values like "hermes-system" or "operator" can't be misread by the next worker as a system
        # directive above the (attacker-influenceable) comment body. Defense-in-depth — the LLM-controlled
        # author-forgery surface was already closed in #22435. See #22452.
        #
        # The run/session provenance rides inside the same code span so two
        # concurrent SAME-PROFILE sessions read as different writers. It is
        # appended AFTER the backtick strip because it comes from trusted
        # runtime context and is shape-validated at the write path — only
        # the author is operator-controlled text.
        safe_author = (c.author or "").replace("`", "")
        author_disp = format_comment_author(
            safe_author, run_id=c.run_id, session_ref=c.session_ref
        )
        lines.append(f"comment from worker `{author_disp}` at {_ctx_stamp(c.created_at, now)}:")
        lines.append(_ctx_cap(c.body, _CTX_MAX_COMMENT_BYTES))
        lines.append("")


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------
def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts and the oldest ``ready`` age (staleness signal)."""
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee = _counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    # Active lane overrides ride along with the counts so `kanban stats` —
    # the first thing anyone reads when workers behave oddly — shows that the
    # board is re-routing spawns, and for how much longer. A silent override
    # is the failure mode this feature exists to avoid.
    try:
        lane_overrides = [
            {
                "assignee": row.assignee,
                "provider": row.provider,
                "model": row.model,
                "reason": row.reason,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "ttl_remaining_seconds": row.ttl_remaining(now),
            }
            for row in list_lane_model_overrides(conn, now=now)
        ]
    except sqlite3.Error:
        # Stats must never fail closed on a board whose schema predates the
        # table and hasn't been reopened through connect()'s migration pass.
        lane_overrides = []

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "lane_model_overrides": lane_overrides,
        "now": now,
    }


def _counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _to_epoch(val) -> Optional[int]:
    """Epoch seconds from int/float/numeric string/ISO-8601; None for empty/invalid."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


# --- Retention + garbage collection ---
def _retention_seconds(older_than_seconds: int) -> int:
    """Normalise a gc retention window, rejecting negatives.

    Shared by both gc sweeps: a negative window puts the cutoff in the future,
    so "older than cutoff" would match every row / file instead of none —
    refuse before any sweep runs.
    """
    older_than_seconds = int(older_than_seconds)
    if older_than_seconds < 0:
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds!r}: "
            "a negative retention selects everything."
        )
    return older_than_seconds


def backfill_notify_sub_user_ids(
    conn: sqlite3.Connection,
    resolve_identity: "Callable[[dict], Optional[Mapping[str, Any]]]",
    *,
    dry_run: bool = False,
    evidence_unavailable: bool = False,
) -> list[dict]:
    """Backfill missing session-key identity on legacy subscription rows.

    The wake rebuilds its :class:`SessionSource` from ``user_id``,
    ``user_id_alt``, and ``scope_id``. Rows written before those fields were
    fully plumbed can therefore wake the WRONG session: alt-keyed adapters use
    ``user_id_alt or user_id`` for the participant segment, while Slack inserts
    ``scope_id`` before the chat id.

    ``resolve_identity`` receives a row dict and returns a mapping containing
    the complete identity observed in durable gateway routing, or ``None``.
    Returning ``None`` MUST leave the row untouched. A cron / CLI / home-channel
    subscription is legitimately user-less, and inventing an identity would
    route a system notification into some human's private session. The caller
    owns that evidence and refuses chats with zero or multiple candidates; this
    function owns the fill-a-hole-only write. ``evidence_unavailable``
    distinguishes an empty but readable index from a failed index read, so even
    named-but-incomplete rows remain visible in the operator report.

    Existing values are authoritative and are never overwritten. In particular,
    an alt id is added to a named row only when the routing entry's raw
    ``user_id`` agrees, preventing a different participant's alt id from being
    grafted onto the row. Rows for platforms that legitimately have no alt or
    scope stay idempotent: if the resolver supplies no missing value, the row is
    omitted from the result instead of being reported forever.

    Returns records for rows changed (or changeable under ``dry_run``) and for
    incomplete rows that could not be repaired safely. Each record includes all
    fields, ``backfilled_fields``, and ``action`` (``"backfilled"``,
    ``"skipped_no_evidence"``, ``"skipped_evidence_unavailable"``, or
    ``"skipped_raced"``).
    """
    rows = conn.execute(
        """
        SELECT n.task_id, n.platform, n.chat_id, n.chat_type, n.thread_id,
               n.user_id, n.user_id_alt, n.scope_id, n.notifier_profile,
               n.delivery_metadata, t.session_id AS creator_session_id
          FROM kanban_notify_subs AS n
          LEFT JOIN tasks AS t ON t.id = n.task_id
         WHERE n.user_id IS NULL OR n.user_id = ''
            OR n.user_id_alt IS NULL OR n.user_id_alt = ''
            OR n.scope_id IS NULL OR n.scope_id = ''
        """
    ).fetchall()

    fields = ("user_id", "user_id_alt", "scope_id")

    def _clean(value: Any) -> str:
        return str(value or "").strip()

    results: list[dict] = []
    pending: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if is_unhomed(item.get("creator_session_id")):
            # The 'unhomed' sentinel means "no creator provenance", exactly
            # like a legacy NULL. Normalize at this reader so no resolver can
            # mistake it for a raw session id and adopt on lane evidence.
            item["creator_session_id"] = None
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(
                item.get("delivery_metadata")
            )
        try:
            raw_identity = resolve_identity(item)
        except Exception:
            raw_identity = None
        if isinstance(raw_identity, Mapping):
            resolved = {
                field: _clean(raw_identity.get(field)) for field in fields
            }
        else:
            # Compatibility with the original user-id-only callback contract.
            resolved = {
                "user_id": _clean(raw_identity),
                "user_id_alt": "",
                "scope_id": "",
            }
        existing = {field: _clean(item.get(field)) for field in fields}
        row_key = {
            "task_id": item["task_id"],
            "platform": item["platform"],
            "chat_id": item["chat_id"],
            "thread_id": item.get("thread_id") or "",
        }

        if not any(resolved.values()):
            # A readable index with no matching creator evidence is still an
            # incomplete row, not "nothing to repair". Keep it visible while
            # refusing to invent identity.
            results.append({
                **row_key,
                **{field: existing[field] or None for field in fields},
                "backfilled_fields": [],
                "action": (
                    "skipped_evidence_unavailable"
                    if evidence_unavailable else "skipped_no_evidence"
                ),
            })
            continue

        conflicts = any(
            existing[field]
            and resolved[field]
            and existing[field] != resolved[field]
            for field in fields
        )
        # ``user_id_alt`` outranks ``user_id`` in build_session_key. Never graft
        # an alt id onto a named row unless the same routing entry proves its
        # lower-priority raw id is the row's existing owner.
        if (
            resolved["user_id_alt"]
            and existing["user_id"]
            and resolved["user_id"] != existing["user_id"]
        ):
            conflicts = True
        if conflicts:
            if not existing["user_id"] and not existing["user_id_alt"]:
                results.append({
                    **row_key,
                    **{field: existing[field] or None for field in fields},
                    "backfilled_fields": [],
                    "action": "skipped_no_evidence",
                })
            continue

        backfilled_fields = [
            field for field in fields if not existing[field] and resolved[field]
        ]
        if not backfilled_fields:
            continue
        final_identity = {
            field: existing[field] or resolved[field] or None for field in fields
        }
        result = {
            **row_key,
            **final_identity,
            "backfilled_fields": backfilled_fields,
            "action": "backfilled",
        }
        if dry_run:
            results.append(result)
        else:
            pending.append({
                "identity": {**row_key, **resolved},
                "result": result,
            })

    if pending:
        with write_txn(conn):
            for change in pending:
                identity = change["identity"]
                row_params = (
                    identity["task_id"], identity["platform"],
                    identity["chat_id"], identity["thread_id"],
                )
                cursor = conn.execute(
                    """
                    UPDATE kanban_notify_subs
                       SET user_id = CASE
                               WHEN user_id IS NULL OR user_id = ''
                               THEN COALESCE(NULLIF(?, ''), user_id)
                               ELSE user_id END,
                           user_id_alt = CASE
                               WHEN user_id_alt IS NULL OR user_id_alt = ''
                               THEN COALESCE(NULLIF(?, ''), user_id_alt)
                               ELSE user_id_alt END,
                           scope_id = CASE
                               WHEN scope_id IS NULL OR scope_id = ''
                               THEN COALESCE(NULLIF(?, ''), scope_id)
                               ELSE scope_id END
                     WHERE task_id = ? AND platform = ?
                       AND chat_id = ? AND thread_id = ?
                       AND (user_id IS NULL OR user_id = '' OR user_id = ?)
                       AND (user_id_alt IS NULL OR user_id_alt = ''
                            OR user_id_alt = ?)
                       AND (scope_id IS NULL OR scope_id = '' OR scope_id = ?)
                    """,
                    (
                        identity["user_id"], identity["user_id_alt"],
                        identity["scope_id"], *row_params,
                        identity["user_id"], identity["user_id_alt"],
                        identity["scope_id"],
                    ),
                )
                if cursor.rowcount:
                    results.append(change["result"])
                    continue

                # Another writer changed or deleted the row after the scan. The
                # tuple guards correctly refused a partial/interleaved repair;
                # report that refusal instead of claiming a write that did not
                # commit.
                current = conn.execute(
                    """
                    SELECT user_id, user_id_alt, scope_id
                      FROM kanban_notify_subs
                     WHERE task_id = ? AND platform = ?
                       AND chat_id = ? AND thread_id = ?
                    """,
                    row_params,
                ).fetchone()
                raced = dict(change["result"])
                raced.update({
                    field: (_clean(current[field]) or None) if current else None
                    for field in fields
                })
                raced["backfilled_fields"] = []
                raced["action"] = "skipped_raced"
                results.append(raced)
    return results

# Task statuses at which a notify subscription has served its purpose. The
# notifier consumers (gateway ``_kanban_notifier_watcher`` and the TUI poller)
# delete a subscription once it has DELIVERED the events it claimed while the
# task sits in one of these statuses — so the terminal line still arrives, and
# nothing is left behind to wake a big-context origin session on later noise.
# A controller that reopens a ``done`` card re-subscribes explicitly
# (``hermes kanban notify-subscribe``).
# Policy: t_6d6e9467 (1,238 of Apollo's 1,295 wake subs sat on done/archived).
NOTIFY_SUB_FINAL_STATUSES = frozenset({"done", "archived"})


def notify_sub_is_final(task: Any) -> bool:
    """True when ``task`` is in a status that ends notify-sub ownership."""
    return bool(task) and (getattr(task, "status", "") or "") in NOTIFY_SUB_FINAL_STATUSES


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------
def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity until task deletion.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.
    """
    cutoff = int(time.time()) - _retention_seconds(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND kind != 'decomposed' AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))", (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_status_audit(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete ``task_status_audit`` rows older than ``older_than_seconds``
    (any task status: the audit is read by a run's post-run reconciliation,
    which never looks back further than its own run). Returns rows deleted."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_status_audit WHERE changed_at < ?", (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    older_than_seconds = _retention_seconds(older_than_seconds)
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        # The workspace-deletion audit shares this directory with the
        # disposable per-task worker logs. It is append-only forensic
        # history, not a worker log, and `hermes kanban gc` is the SAME
        # command that performs workspace deletions -- reaping it here let
        # the deleting lane destroy its own record (card t_63fb42f9).
        if is_deletion_audit_path(p):
            continue
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------
def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None, board: Optional[str] = None,
) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip the partial first line unless the window has no newline
                # at all (readline() would eat everything).
                probe = f.tell()
                if not f.readline().endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------
def list_profiles_on_disk() -> list[str]:
    """Profiles with a ``config.yaml`` plus the implicit ``default``; reads paths
    directly to avoid importing ``hermes_cli.profiles`` at startup."""
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")
    if profiles_dir.is_dir():
        try:
            names.update(e.name for e in profiles_dir.iterdir() if e.is_dir() and (e / "config.yaml").is_file())
        except OSError:
            pass
    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """``{"name", "on_disk", "counts"}`` for every on-disk profile or task
    assignee, so a fresh profile appears in pickers before it has a task."""
    on_disk = set(list_profiles_on_disk())
    counts = _counts_by_assignee(conn)
    return [
        {"name": name, "on_disk": name in on_disk, "counts": counts.get(name, {})}
        for name in sorted(on_disk | set(counts))
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------
def list_runs(
    conn: sqlite3.Connection, task_id: str, *, include_active: bool = True,
    state_type: Optional[str] = None, state_name: Optional[str] = None,
) -> list[Run]:
    """Runs in start order; ``include_active=False`` = closed only; ``state_type``
    (``status``/``outcome``) + ``state_name`` filter together."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def explain_complete_refusal(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_run_id: Optional[int] = None,
) -> str:
    """Why ``complete_task`` returned False, in one clause, read after the fact.

    Replaces the generic "unknown id or terminal state": a worker retrying a
    timed-out complete needs to see "already done by <who> at <when>", not
    a guess (t_ef1ba08b).
    """
    task = get_task(conn, task_id)
    if task is None:
        return "unknown id"
    if task.status in ("done", "archived"):
        row = conn.execute(
            "SELECT profile, outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        who = (row["profile"] if row else None) or task.assignee or "unknown"
        outcome = f", outcome {row['outcome']}" if row and row["outcome"] else ""
        when = (
            time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(task.completed_at))
            if task.completed_at
            else "unknown time"
        )
        state = "already done" if task.status == "done" else "archived (was done)" if task.completed_at else "archived"
        return f"{state} by {who} at {when}{outcome}"
    if task.status not in ("running", "ready", "blocked", "review"):
        return f"status is {task.status!r}; complete needs running/ready/blocked/review"
    if expected_run_id is not None and task.current_run_id != expected_run_id:
        return (
            f"run {expected_run_id} is no longer the current run "
            f"(current: {task.current_run_id}); another run owns this card"
        )
    if not _parents_satisfied(conn, task_id):
        return "a parent task is not done"
    return f"refused while status is {task.status!r} (state changed concurrently?)"


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Newest non-empty run summary, or None. Workers hand off via ``summary`` and
    leave ``tasks.result`` NULL, so views need this or a done task looks empty."""
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, str]:
    """``{task_id: newest non-empty run summary}`` in one query (window function,
    SQLite >= 3.25); tasks without a summary are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}


def current_run_started_ats(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """``{task_id: started_at of the run ``tasks.current_run_id`` points at}``
    in one query; tasks with no active run (NULL or dangling pointer) are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS task_id, r.started_at AS started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        f"WHERE t.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["task_id"]: r["started_at"] for r in rows}


# --- Split modules (imported at the tail: they import this module as ``_kb``) ---
from hermes_cli.kanban_db_connect import (  # noqa: E402
    _INITIALIZED_PATHS,
    _execute_boundary_with_retry,
    _schema_is_present,
    _sqlite_connect,
    connect,
    connect_closing,
    init_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (  # noqa: E402
    _cleanup_workspace,
    _has_active_children,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _scratch_workspace,
)
from hermes_cli.kanban_db_dispatch import (  # noqa: E402
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    DispatchResult,
    _PROTOCOL_VIOLATION_SCAN_LIMIT,
    _RECENT_WORKER_EXITS_MAX,
    _RECENT_WORKER_EXIT_TTL_SECONDS,
    _RESPAWN_GUARD_PR_URL_RE,
    _RESPAWN_GUARD_PR_WINDOW,
    _STALE_HEARTBEAT_GAP_SECONDS,
    UNVERIFIED_WORKER_FINGERPRINT,
    _classify_worker_exit,
    _clear_failure_counter,
    _defer_reclaim_for_live_worker,
    _pid_alive,
    _pid_recycled,
    _recent_worker_exits,
    _record_task_failure,
    _terminate_reclaimed_worker,
    _worker_alive,
    _worker_survived_termination,
    _worker_terminal_timeout_env,
    # Fork facade re-exports: the dispatcher entry points lived here before
    # upstream's kanban_db_dispatch split; fork tests and callers still reach
    # them as ``kanban_db.dispatch_once`` etc.
    check_respawn_guard,
    detect_crashed_workers,
    dispatch_once,
    enforce_max_runtime,
    has_spawnable_ready,
)
from hermes_cli.kanban_db_notify import (  # noqa: E402
    NOTIFY_SUB_OWNER_LIVE_SECONDS,
    NOTIFY_WAKE_MODES,
    ONE_WAKER_INDEX,
    _ensure_single_waker_index,
    card_waker,
    _decode_notify_delivery_metadata,
    _drop_notify_subs,
    _log_sub_kept,
    _notify_sub_admission,
    card_home_chat,
    dedupe_notify_subs,
    notify_chat_is_live,
)
