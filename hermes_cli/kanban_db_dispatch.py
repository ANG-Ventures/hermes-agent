"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import Union
from typing import TYPE_CHECKING
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes


# Priority-starvation guard (t_24d20798). The ready loop already admits
# ``priority DESC, created_at ASC``; a high-priority card that sits in ready
# while lower-priority cards spawn is therefore being HELD by a per-card gate
# (respawn guard, pool deferral, overlap hold, ...). On 2026-10-02 t_2f8b03ac
# (p62) sat ready 10:05 -> 15:40 behind ``respawn_guarded: active_pr`` (247
# events) while 110 lower-priority claims went through, and nothing said so.
# 2 h is the threshold the card set; measured p90 ready-dwell of every band
# over the prior 48 h was <= 30 min for unheld cards.
PRIORITY_STARVATION_SECONDS = 2 * 3600
_PRIORITY_STARVATION_MARKER = ".priority_starvation.json"
_PRIORITY_STARVATION_HOLD_KINDS = ("respawn_guarded", "deferred", "parked_by_policy")


def find_priority_starved(
    conn: sqlite3.Connection, *, now: Optional[int] = None,
    threshold_seconds: int = PRIORITY_STARVATION_SECONDS,
) -> list[dict]:
    """Ready cards held past ``threshold_seconds`` while lower priority ran.

    A card qualifies when it has been ``ready`` (since its latest entry into
    ready, else ``created_at``) for longer than the threshold AND at least
    one run on a strictly lower-priority card started after that entry.
    Each hit names the card, its dwell, the newest hold reason recorded
    since it went ready, and one lower-priority card admitted meanwhile.
    """
    now = int(time.time()) if now is None else int(now)
    hits: list[dict] = []
    for row in conn.execute(
        "SELECT id, priority, assignee, created_at FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL"
    ).fetchall():
        entered = conn.execute(
            "SELECT MAX(changed_at) FROM task_status_audit WHERE task_id = ? "
            "AND new_status = 'ready' AND old_status IS NOT 'ready'",
            (row["id"],),
        ).fetchone()[0]
        since = int(entered if entered is not None else row["created_at"])
        if now - since <= threshold_seconds:
            continue
        prio = int(row["priority"] or 0)
        lower = conn.execute(
            "SELECT r.task_id, t.priority FROM task_runs r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE r.started_at >= ? AND r.task_id != ? "
            "AND COALESCE(t.priority, 0) < ? ORDER BY r.id DESC LIMIT 1",
            (since, row["id"], prio),
        ).fetchone()
        if lower is None:
            continue
        hold = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND created_at >= ? "
            "AND kind IN (%s) ORDER BY id DESC LIMIT 1"
            % ",".join("?" * len(_PRIORITY_STARVATION_HOLD_KINDS)),
            (row["id"], since, *_PRIORITY_STARVATION_HOLD_KINDS),
        ).fetchone()
        reason = "none recorded"
        if hold is not None:
            try:
                reason = str((json.loads(hold[0] or "null") or {}).get("reason") or reason)
            except (TypeError, ValueError, AttributeError):
                pass
        hits.append({
            "task_id": row["id"], "priority": prio, "assignee": row["assignee"],
            "ready_since": since, "ready_seconds": now - since, "reason": reason,
            "lower_task_id": lower["task_id"], "lower_priority": int(lower["priority"] or 0),
        })
    return hits


def _notify_priority_starved(
    conn: sqlite3.Connection, board: Optional[str], hits: list[dict],
) -> None:
    """One #logs line + one ``priority_starved`` event per card ready-episode.

    Latched per ``(task_id, ready_since)`` in the board state dir, and only
    once the line went out, so a failed send retries next tick and a card
    that leaves ready and comes back starved again is reported again.
    """
    try:
        from hermes_cli import kanban_budget as _kbudget

        marker = _kb.board_state_dir(board) / _PRIORITY_STARVATION_MARKER
        try:
            seen = json.loads(marker.read_text(encoding="utf-8-sig"))
        except Exception:
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        current = {h["task_id"]: h["ready_since"] for h in hits}
        latched = {k: v for k, v in seen.items() if current.get(k) == v}
        changed = latched != seen
        script = _kbudget._notify_script_path()
        import sys as _sys
        for h in hits:
            if latched.get(h["task_id"]) == h["ready_since"]:
                continue
            body = (
                f"⏳ Kanban '{board or 'default'}': {h['task_id']} (p{h['priority']}, "
                f"{h['assignee'] or 'unassigned'}) READY {h['ready_seconds'] // 60} min "
                f"while lower priority was admitted (e.g. {h['lower_task_id']} "
                f"p{h['lower_priority']}). Held by: {h['reason']}."
            )
            _kb._log.warning("PHASE=kanban_priority_starved %s", body)
            delivered = None
            if script is not None:
                delivered = _kbudget._run_notify([
                    _sys.executable, script, "--channel", "discord",
                    "--target", _kbudget.RECOVERY_TARGET, "--send", body,
                ])
            if delivered is False:
                continue
            with _kbc.write_txn(conn):
                _kb._append_event(conn, h["task_id"], "priority_starved", {
                    k: h[k] for k in ("ready_seconds", "reason", "lower_task_id",
                                      "lower_priority")
                })
            latched[h["task_id"]] = h["ready_since"]
            changed = True
        if changed:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(latched), encoding="utf-8")
    except Exception as exc:
        _kb._log.warning("kanban priority-starvation notify failed (%s: %s)", type(exc).__name__, exc)

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours
_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/[^\s]+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """Task ids requeued by :func:`reconcile_orphaned_running` this tick —
    ``running`` cards whose claim bookkeeping was broken (no valid claim,
    dead/gone worker). See the reconciliation pass for details."""
    ended_terminal_runs: list[int] = field(default_factory=list)
    """Run ids closed by :func:`end_orphaned_terminal_runs` this tick — runs
    still open on a ``done``/``archived`` card (outcome
    ``orphaned_terminal_task``)."""
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by
    :func:`reap_terminal_workers`."""
    worker_leftovers_reaped: list[str] = field(default_factory=list)
    """Task ids whose exited worker left processes that were reaped this tick
    (:func:`reap_exited_worker_leftovers`, fork t_446b6b99)."""
    orphans_reaped: dict = field(default_factory=dict)
    """``{card: [{pid, name, cwd}]}`` reaped by
    :func:`sweep_terminal_workspace_orphans` this tick."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    spawn_routes: dict[str, str] = field(default_factory=dict)
    """Effective ``provider/model`` route for each task spawned this tick."""
    spawn_route_sources: dict[str, str] = field(default_factory=dict)
    """Where each spawned task's route came from — ``card-override``,
    ``lane-override(<n>s remaining)``, or ``profile-default``. Paired with
    ``spawn_routes`` so a tick log can say WHY a worker got the model it got
    instead of leaving an operator to guess which layer won."""
    expired_lane_models: list[tuple[str, str]] = field(default_factory=list)
    """Lane overrides retired this tick as ``(lane, route)``, where ``lane`` is
    the assignee or ``*`` for the board-wide row. Reported exactly once — the
    rows are deleted when they expire — so the tick log carries a single
    ``lane-model expired -> <successor>`` line per window."""
    expired_lane_successors: dict[str, str] = field(default_factory=dict)
    """What routes each expired lane (keyed like ``expired_lane_models``) for
    the rest of this tick: ``profile default``, or the lane override that is
    still active — e.g. the board-wide lane under an expired assignee lane, or
    a window renewed after the expiry pass. Read through the same memoized
    lookup the spawns in this tick use, so the log and the routes agree."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids skipped because they have no assignee at all.
    Operator-actionable — usually a misfiled task waiting for routing."""
    flagship_refused: list[str] = field(default_factory=list)
    """Task ids whose model override matched the configured flagship ban and
    lacked an explicit ``flagship override:`` audit comment. These tasks stay
    ready/review and are reconsidered on the next tick."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Task ids that were unassigned in the DB and had
    ``kanban.default_assignee`` applied this tick before spawning (#27145).
    Surfaces the auto-assignment to telemetry / CLI / dashboard so the
    operator can see when the dispatcher is acting on the fallback rule
    rather than on explicit per-task assignments."""
    skipped_no_worker: list[str] = field(default_factory=list)
    """Ready/review task ids skipped because the card is flagged
    ``no_worker`` (operator-only). Never claimed, never spawned."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    spillover: Optional[str] = None
    """The shared pool plan this tick saw (KWLB v0.1): per-host load1 / ncpu /
    running / free. None when no plan was passed."""
    placed: list = field(default_factory=list)
    """``(task_id, host)`` for workers placed on a pool host; the gateway
    books local spawns as ``len(spawned) - len(placed)``."""
    placement_waits: dict = field(default_factory=dict)
    """task id -> why a ready card was not spawned anywhere this tick
    (``local_full``, ``pool_full``, ``pin_*``, ``not_portable:<rule>``,
    ``studio_affinity:<rule>``)."""
    rows_routed: int = 0
    """Ready rows this tick's routing/spawn loop considered (pool plan or not).
    Read by the gateway's tick series (``gate-ticks.jsonl``), never by dispatch."""
    portable_waiting: int = 0
    """Rows routed ``portable`` still in ``placement_waits`` at the end of the
    ready section (the I-0 query's input, spec v0.3 §3.6)."""
    portable_waiting_with_room: int = 0
    """Portable rows the pool refused that then WAITED although this board's
    local allowance had room at their turn (spec v0.3 I-0 per-board key, Momus
    pass 3 R1). Any cap on the last-resort spawn makes this non-zero; quota
    stranded by the board split does not (that is ``stranded_allowance``)."""
    take_refusals: dict = field(default_factory=dict)
    """task id -> the take() refusal of a portable row spawned locally as the
    last resort this tick (dry run included), for the printer and the record."""
    placement_affinity: dict = field(default_factory=dict)
    """task id -> ``{host, rule, matched_by}`` for ready cards whose work is
    bound to one host's state (t_3e6c9b2b): they run there or wait, never spill."""
    unknown_pins: dict = field(default_factory=dict)
    """task id -> the ``host:`` word that names no host (t_cb2b888d): the card
    is held local-only and gets one latched comment per (task, word)."""
    access_contradictions: dict = field(default_factory=dict)
    """task id -> the §5 line of an access-bound card (spec §2.6) pinned to a
    pool host: parked ``needs_input`` after the loop (dry run: reported)."""
    browser_joins: dict = field(default_factory=dict)
    """task id -> ``{"plans": [...], "source_host": <worker host>}`` for a ready card whose
    ``browser:ephemeral`` lease the s7 join takes at spawn (``kanban.browser_join``, t_2828292d)."""
    placement_caps: dict = field(default_factory=dict)
    """task id -> what the capability layer (t_8c474c2e) saw on a ready card
    without changing its route: ``{"warn": <payload>}`` (warn mode),
    ``{"unknown": <word>}``, ``{"action": "needs_input"}`` (a pool pin that
    lacks a hard requirement), and ``"_map_error"`` -> the map problem."""
    routes: list = field(default_factory=list)
    """One dict per ready row this tick, in loop order (spec §4.3,
    t_7133e03d): ``{task_id, route, rule, host, outcome, code, detail}``.
    ``route`` is ``local | remote-pin | remote-policy | hold`` (None when a
    total-budget gate held the row before routing)."""
    spawn_paused: Optional[str] = None
    """Non-None when this tick RECLAIMED but deliberately spawned nothing —
    the gateway's load gate (``kanban.dispatch_load_gate``) held the host was
    over its run-queue bar. The string is the human reason (load1/ncpu). NOT
    an operator-actionable failure; spawning resumes when the host cools."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    stranded_by_mount_loss: list[str] = field(default_factory=list)
    workspace_refused: list[tuple[str, str]] = field(default_factory=list)
    """Mount or persisted-workspace failures, refused BEFORE claiming a run."""
    skill_refused: list[tuple[str, list[str]]] = field(default_factory=list)
    """``(task_id, missing_skills)`` for cards blocked BEFORE claiming a run
    because NO skill the worker would be given resolves for the assignee
    profile (the worker would exit ``Unknown skill(s)`` at startup). See
    :func:`_card_skills_refused`."""
    skill_degraded: list[tuple[str, list[str]]] = field(default_factory=list)
    """``(task_id, missing_skills)`` for cards dispatched although SOME card
    skill does not resolve: the worker loads the rest and skips these with a
    warning, so the dispatcher comments on the card instead of blocking it."""
    spawn_failed: list[str] = field(default_factory=list)
    """Task ids whose spawn attempt failed THIS tick — recorded on every
    failure (workspace resolution or worker launch), whether or not it was
    the failure that tripped the circuit breaker. ``auto_blocked`` is the
    subset that crossed ``failure_limit`` this tick; ``spawn_failed`` is the
    superset including the earlier, pre-breaker failures. Health telemetry
    consults this so a genuine early-phase spawn failure on one board is
    visible as a fault immediately (failure #1), instead of staying invisible
    until the breaker trips several ticks later — where a benign decline on a
    DIFFERENT board could otherwise mask it and reset the stuck-streak."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    process_capped: list[str] = field(default_factory=list)
    """Task ids whose live worker tree exceeded
    ``kanban.worker_max_procs_per_run`` and was terminated + blocked."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed because no progress (heartbeat) was seen
    within ``dispatch_stale_timeout_seconds``."""
    progress_stalled: list[str] = field(default_factory=list)
    """Task ids reclaimed after fresh wrapper heartbeats but no worker activity."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped by the respawn guard, as ``(task_id, reason)`` pairs.

    Reasons: ``"blocker_auth"`` (quota/auth error — also auto-blocked),
    ``"recent_success"`` (completed run within guard window),
    ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    respawn_guard_details: dict[str, dict] = field(default_factory=dict)
    """For failure-derived guard reasons (``blocker_auth`` /
    ``rate_limit_cooldown``): ``task_id`` → ``{"error", "recorded_at",
    "eligible_at"}`` so the deferral names WHICH error text is holding the
    task and WHEN it was recorded (staleness is visible, not inferred)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released back to ``ready`` WITHOUT
    counting a failure. These never trip the circuit breaker — a long quota
    window just makes the task bounce cheaply until the window clears."""
    cohort_deaths: list[str] = field(default_factory=list)
    """Task ids whose workers ended EXTERNALLY together in one tick (a cohort
    of >= ``_COHORT_DEATH_MIN`` dead pids, each with a heartbeat fresher than
    ``_COHORT_DEATH_HEARTBEAT_WINDOW_SECONDS``). One outside actor ended them
    all — released back without counting a failure (card t_0c1ebbae)."""
    infra_unavailable: list[str] = field(default_factory=list)
    """Task ids whose worker HARNESS could not be executed at all (exit
    126/127 — the ``hermes`` CLI path missing or unrunnable, e.g. during a
    deploy that took the runtime venv offline). Released back without
    counting a failure, exactly like ``rate_limited``, but tracked
    separately: the remedy is fixing the deploy, not waiting out a quota."""
    lock_holder: dict = field(default_factory=dict)
    """Best-effort pid, age_seconds and acquire_site snapshot on a skipped tick.
    Empty for legacy holders or racing/failed stamp reads; not a liveness probe."""
    skipped_locked: bool = False
    """True when this tick was skipped because another process already held
    the board's dispatch lock (issue #35240). A losing dispatcher does no
    DB writes this tick — the lock holder is making progress on the same
    board. This is the steady-state signal that a single-writer guard is
    actively preventing two dispatchers from racing on ``kanban.db``."""
    budget_paused: bool = False
    """True when this board spawned nothing because its rolling-window worker
    spend has reached ``kanban.budget.usd_per_24h``. Reclaim / promotion /
    bookkeeping still ran — only NEW spawns are withheld, and they resume
    automatically once the window rolls the spend back under the ceiling."""
    parent_satisfied_sticky: list[str] = field(default_factory=list)
    """Explicitly blocked task ids that have one or more ``blocks`` parents
    and whose parents are all terminal. The graph is satisfied, but the
    worker/operator handoff intentionally remains sticky until an explicit
    unblock. Surfaced so a zero-promotion tick names the hold instead of
    silently reporting ``promoted=0``."""
    woken_scheduled: list[str] = field(default_factory=list)
    """``scheduled`` task ids whose timed wake (``next_eligible_at``) passed
    this tick and were returned to ``ready``/``todo`` by ``wake_due_scheduled``."""
    priority_starved: list[tuple[str, int, str]] = field(default_factory=list)
    """``(task_id, ready_seconds, hold_reason)`` for ready cards past
    ``PRIORITY_STARVATION_SECONDS`` while a lower-priority card was admitted
    since they went ready; see :func:`find_priority_starved`."""
    placement_rerouted: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, host)`` for capability blocks on a misplaced run that this tick
    pinned to ``host`` and returned to the queue (:mod:`kanban_placement_block`)."""
    placement_unroutable: list[str] = field(default_factory=list)
    """Placement blocks this tick could not re-route; kanban-placement-watch pages them."""
    unwoken_scheduled: list[tuple[str, int]] = field(default_factory=list)
    """``(task_id, parked_seconds)`` for ``scheduled`` cards with NO timed wake
    parked past ``SCHEDULED_UNWOKEN_THRESHOLD_SECONDS`` — nothing will ever
    move them; see ``find_unwoken_scheduled``."""
    stranded_by_triage: list[tuple[str, str]] = field(default_factory=list)
    """``(child_id, parent_id)`` pairs where a ``todo`` card is held ONLY
    because a parent sits in ``triage``/``blocked`` — i.e. behind a card that
    needs a human and will never clear on its own.

    Operator-actionable, and the reason this bucket exists: a triaged parent
    silently freezes its whole subtree. The dispatcher was correct to hold the
    children (parent gating is the invariant), but ``Spawned: 0`` with every
    other bucket empty is byte-identical to "nothing to do" — so a deploy card
    sat idle for hours behind a triaged parent and nobody was told. Surfacing
    the pairs makes the stranding LOUD; ``hermes kanban triage-resolve`` is the
    exit."""
    collision_warnings: list[tuple[str, str, list[str]]] = field(default_factory=list)
    """Warning-only file overlaps found before spawn, as
    ``(new_task_id, existing_task_id, overlapping_paths)`` triples. The
    dispatcher still spawns the new task; this bucket makes the collision
    visible without turning legitimate shared-file work into an approval
    gate."""
    collision_scope_unknown: list[str] = field(default_factory=list)
    """Task ids whose body exposed no explicit file path, so the dispatcher
    could not compare their likely scope. This is reported rather than
    silently presenting the collision scan as complete."""
    collision_scope_unreported: list[tuple[str, list[str]]] = field(default_factory=list)
    """``(new_task_id, existing_task_ids)`` entries for in-flight or recently
    blocked cards that reported no ``changed_files``. The scan is intentionally
    partial in this case and says so rather than guessing from prose."""
    collision_check_failed: list[str] = field(default_factory=list)
    """Task ids whose warning-only collision check raised unexpectedly. The
    failure is logged loudly and dispatch continues (fail-open), preserving
    the rule that this diagnostic must never become an approval gate."""
    gate_auto_resolved: list[str] = field(default_factory=list)
    """Task ids the PR-gate re-evaluator unblocked this tick because every
    GitHub PR named in their block reason had merged (see
    :mod:`hermes_cli.kanban_pr_gate`). Before this, a card blocked on "merge
    PR #N then unblock me" stayed blocked until a human board sweep noticed —
    six cards sat up to 12 h that way on 2026-09-21. Surfaced here so the
    dispatch report and sweep tooling can COUNT the automation rather than
    infer it from card history."""
    gate_closed_unmerged: list[str] = field(default_factory=list)
    """Task ids whose gate PR is CLOSED WITHOUT MERGING. Deliberately NOT
    unblocked — the premise died rather than being satisfied, so a human has
    to re-point or retire the card. One advisory comment is posted once."""
    memory_pressure: Optional[str] = None
    """System memory pressure observed at spawn time when the memory guard
    restricted this tick (OOF-30/OOF-77): ``"critical"`` — no new workers
    were spawned this tick; ``"elevated"`` — at most one new worker was
    spawned. ``None`` when memory was fine/unknown and the guard imposed
    no restriction. Reclaim/promotion bookkeeping still ran either way;
    deferred tasks stay queued for the next tick."""
    spawn_capped: Optional[str] = None
    """Non-None when a concurrency cap or the per-tick spawn limit left this
    tick with a spawn budget of ZERO (``kanban.max_spawn`` / ``--max``,
    ``kanban.max_in_progress`` host cap, or the load-gate ``spawn_limit``).
    The string names the cap and the numbers that tripped it, so a manual
    ``hermes kanban dispatch`` that spawns nothing says WHY instead of
    printing a bare ``Spawned: 0`` (t_f78d1938)."""


def describe_suppression(results: Iterable[Optional["DispatchResult"]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)

# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}

# Windows has no ``waitpid(-1)``: a child's exit code is only recoverable
# through a live handle, so ``_default_spawn`` parks each worker's ``Popen``
# here (Windows only) and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: "dict[int, subprocess.Popen]" = {}


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Legacy POSIX wait-status entry point; retained for older callers."""
    try:
        code = os.waitstatus_to_exitcode(raw_status)
    except (ValueError, AttributeError):
        return
    _kb._record_worker_returncode(pid, code)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """Classify a recently-reaped worker by pid.

    Returns ``(kind, code)`` where ``kind`` is one of:

    * ``"clean_exit"`` — ``WIFEXITED`` with ``WEXITSTATUS == 0``. When the
      task is still ``running`` in the DB, this is a protocol violation
      (worker exited without calling ``kanban_complete`` / ``kanban_block``)
      and should be auto-blocked immediately — retrying will just loop.
    * ``"rate_limited"`` — ``WIFEXITED`` with status
      ``KANBAN_RATE_LIMIT_EXIT_CODE``. The worker bailed because the
      provider rate-limited / exhausted quota, NOT because the task failed.
      ``detect_crashed_workers`` releases the task back to ``ready`` without
      counting a failure, so a long quota window can't trip the breaker.
    * ``"infra_unavailable"`` — ``WIFEXITED`` with a status in
      ``KANBAN_INFRA_EXIT_CODES`` (126/127). The worker harness could not be
      executed at all — the CLI path was missing or unrunnable — so the task
      never started and cannot be at fault. Handled exactly like
      ``rate_limited`` (requeue, no failure counted), with its own event kind
      so the board history names the real cause.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``rate_limited`` /
    ``infra_unavailable`` / ``nonzero_exit``) or the signal number (for
    ``signaled``), or ``None`` for ``unknown``.
    """
    # Read through the kanban_db facade: ``_record_worker_returncode`` writes to
    # ``kanban_db._recent_worker_exits`` (the name tests patch), and a rebound dict
    # there must be the one classified here (test_windows_native_support).
    entry = _kb._recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    code, _ = entry
    if code < 0:
        return ("signaled", -code)
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code in _kb.KANBAN_INFRA_EXIT_CODES:
        return ("infra_unavailable", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)


def _exit_code_kind(code: int) -> "tuple[str, int]":
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)
_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Only the
    CURRENT run's segment counts: the log is append-mode across re-runs, and a
    previous run's ``rc=0`` trailer read for a live-but-misjudged worker booked
    13 still-running workers as clean-exit protocol violations on 2026-10-12
    12:11 (t_5c37ab9d). A log with no boundary (never stamped) keeps the
    whole-tail read; an empty current segment is no evidence.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=_kb._WORKER_LOG_SEGMENT_BYTES, board=board)
    except Exception:
        return None
    raw = raw or ""
    boundary = raw.rfind(_kb._RUN_BOUNDARY_PREFIX)
    if boundary >= 0:
        raw = raw[boundary:]
    matches = _EXIT_TRAILER_RE.findall(raw)
    return int(matches[-1]) if matches else None


def reap_worker_zombies() -> "list[int]":
    """Poll only our retained children, not other subsystems' subprocesses.

    Popen can report zero if an external reaper wins; the run-scoped receipt
    takes precedence over this best-effort fallback.
    """
    reaped: list[int] = []
    with _kb._worker_processes_lock:
        for pid, proc in list(_kb._worker_processes.items()):
            code = proc.poll()
            if code is not None:
                _kb._record_worker_returncode(pid, code)
                _kb._worker_processes.pop(pid, None)
                reaped.append(pid)
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Uses ``gateway.status._pid_exists`` (OpenProcess on Windows, ``os.kill(pid, 0)``
    on POSIX). **DO NOT** call ``os.kill(pid, 0)`` directly on Windows — there
    ``sig=0`` is ``CTRL_C_EVENT`` broadcast to the console group, potentially
    killing unrelated processes.

    Zombies (exited, not yet reaped) still pass the existence check, so a
    worker would look "alive" forever between exit and reap. Linux: peek at
    ``/proc/<pid>/status`` and treat ``State: Z`` as dead; macOS: ask ``ps``
    for the BSD ``stat`` field and treat ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True

# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<instantiation epoch>|<start time>"``. The start
    time alone (``/proc/<pid>/stat`` field 22 on Linux) is clock ticks since THIS boot, so a row that
    survives a reboot could match an unrelated process with the same PID and the same tick value;
    ``gateway.drain_control.current_instantiation_epoch`` (``boot_id`` + PID-1 start) changes on every
    reboot / container recreate, so the composed value never survives one. ``None`` when unreadable."""
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    start = get_process_start_time(int(pid))
    if start is None:
        return None
    return f"{current_instantiation_epoch()}|{start}"


def _fingerprints_agree(recorded: str, current: Optional[str]) -> bool:
    """Same process: the boot epochs are equal and the start times agree within
    ``START_TIME_DRIFT_TOLERANCE`` (2 s).

    Never compare the strings exactly. On macOS the start time is psutil's
    ``create_time()``, which re-bases every reading by the change in the
    whole-second ``boot_time()`` once a wall-clock step moves it across a
    second boundary. 2026-10-12 12:11:25 PT: timed stepped the Studio clock
    +0.75 s and every fingerprint the gateway read afterwards was +100 cs off
    the one it recorded at spawn. The exact compare called all 40 live workers
    recycled, and they were requeued as a "cohort death" while still running
    (t_5c37ab9d).
    """
    from gateway.status import start_time_fingerprints_match

    if current is None:
        return False
    rec_epoch, _, rec_start = recorded.rpartition("|")
    cur_epoch, _, cur_start = current.rpartition("|")
    # An empty recorded epoch is a row written where the epoch was unreadable (macOS before
    # t_7fe63baa): it carries no boot witness, so only the start time can decide.
    if rec_epoch and rec_epoch != cur_epoch:
        return False
    try:
        return start_time_fingerprints_match(rec_start, cur_start)
    except (TypeError, ValueError):
        return False


def _live_pid_names_task(pid: int, task_id: str) -> bool:
    """True when ``pid`` is running and its argv is a worker for ``task_id``
    (``... chat -q "work kanban task <id>"``). The duplicate-worker guard: a recycled pid
    cannot name the card, so this holds whatever the start fingerprint says."""
    if not _kb._pid_alive(pid):
        return False
    from gateway.status import _read_process_cmdline

    cmdline = _read_process_cmdline(int(pid)) or ""
    return re.search(rf"\bwork kanban task {re.escape(task_id)}(?![0-9A-Za-z_])", cmdline) is not None


def _liveness_evidence(
    conn: sqlite3.Connection, row, pid: int, kind: str, code: Optional[int], names_task: bool,
) -> dict[str, Any]:
    """The readings the crash sweep acted on, stamped on ``crashed``/``gave_up`` (t_fdfa6613).

    A false crash (10-12: a 1.000 s fingerprint skew) used to be diagnosable only by reading
    this module. ``decision`` is why the pid was judged gone: ``recycled`` (the pid is running
    but failed the identity checks), ``signaled``, ``nonzero`` (exited with a nonzero code),
    else ``not-alive``.
    """
    pid_exists = bool(_kb._pid_alive(pid))
    recorded = conn.execute(
        "SELECT worker_started_at FROM tasks WHERE id = ?", (row["id"],),
    ).fetchone()
    token_recorded = _kb._worker_owner_window(conn, row["id"], pid, row["current_run_id"])[2]
    if pid_exists:
        decision = "recycled"
    elif kind == "signaled":
        decision = "signaled"
    elif code not in (None, 0):
        decision = "nonzero"
    else:
        decision = "not-alive"
    return {
        "pid_exists": pid_exists,
        "fingerprint_recorded": recorded[0] if recorded is not None else None,
        "fingerprint_live": _process_fingerprint(pid) if pid_exists else None,
        "start_token_recorded": token_recorded,
        "start_token_live": _kb._pid_start_token(pid) if pid_exists else None,
        "argv_names_task": names_task,
        "decision": decision,
    }


def _process_group_evidence(pid: int) -> dict[str, Any]:
    """``{ppid, pgid, sid}`` of a live ``pid``, read without forking.

    Stamped on the ``spawned`` event so a later cohort death can say which
    process group/session the dead workers lived in. Missing keys = unreadable.
    """
    out: dict[str, Any] = {}
    for key, read in (("pgid", getattr(os, "getpgid", None)), ("sid", getattr(os, "getsid", None))):
        if read is not None:
            with contextlib.suppress(OSError):
                out[key] = read(int(pid))
    with contextlib.suppress(Exception):
        import psutil

        out["ppid"] = psutil.Process(int(pid)).ppid()
    return out


def _cohort_member_evidence(pid: int, spawn_group: Optional[int]) -> dict[str, Any]:
    """Death-time evidence for one cohort member.

    ``pid_alive``: the pid is still a running (non-zombie) process. A cohort
    whose pids are still running was not killed; the liveness/identity check
    misjudged it. ``group_members``: other live pids still in the worker's
    spawn process group (who else shared the pgrp when it died).
    """
    evidence: dict[str, Any] = {"pid_alive": bool(_kb._pid_alive(int(pid)))}
    if spawn_group is None:
        return evidence
    with contextlib.suppress(Exception):
        import psutil

        members = []
        for proc in psutil.process_iter(["pid"]):
            with contextlib.suppress(Exception):
                if proc.pid != int(pid) and os.getpgid(proc.pid) == int(spawn_group):
                    members.append(proc.pid)
        evidence["group_members"] = sorted(members)[:20]
    return evidence


def _spawn_group(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], pid: int) -> Optional[int]:
    """The pgid the run's ``spawned`` event recorded for ``pid``, if any."""
    if run_id is None:
        return None
    for ev in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'spawned' ORDER BY id DESC",
        (task_id, int(run_id)),
    ):
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            payload = json.loads(ev["payload"] or "{}")
            if int(payload.get("pid", -1)) == int(pid) and payload.get("pgid") is not None:
                return int(payload["pgid"])
    return None


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned. ``started_at`` is the fingerprint
    recorded by ``_set_worker_pid``; after a reboot (or any PID recycle) an unrelated process can own
    the number, so bare existence is never enough to extend a claim or to signal. A legacy row without
    a fingerprint keeps the existence answer: killing it is the pre-fingerprint behaviour and the row is
    rewritten with a fingerprint on its next spawn. An UNVERIFIED spawn also keeps the existence answer
    (a claim is never released beside a possibly-live worker) but ``_terminate_reclaimed_worker``
    refuses to signal it."""
    if not _kb._pid_alive(pid):
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    return not _pid_recycled(pid, started_at)


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is NOT the process fingerprinted at spawn (or the fingerprint can no
    longer be read). Signalling it would hit a stranger. ``None`` fingerprint = legacy row, never
    recycled; the UNVERIFIED marker is always foreign. An integer fingerprint (rows written before the
    boot witness was added) compares the start time only."""
    if started_at is None or not pid:
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    if isinstance(started_at, str) and "|" in started_at:
        return not _fingerprints_agree(started_at, _process_fingerprint(int(pid)))
    from gateway.status import _start_times_agree, get_process_start_time
    current = get_process_start_time(int(pid))
    if current is None:
        return True
    try:
        return not _start_times_agree(current, started_at)
    except (TypeError, ValueError):
        return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _worker_alive(pid, started_at):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    owner_window: tuple,
    signal_fn=None,
    conn: Optional[sqlite3.Connection] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    started_at=None,
    signal_allowed: bool = True,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths.

    ``signal_allowed=False`` makes this a liveness probe with reap: a dead or
    recycled recorded worker is reported ``terminated`` exactly as before, but
    a live, verified one is never signalled (``worker_alive_unsignalled``),
    so the caller's :func:`_worker_survived_termination` check fails closed
    BEFORE any signal. The pr-owner reclaimer uses it (t_0a9a20ea).

    ``run_id`` is the run ``owner_window`` was computed for. The post-death
    reap sweeps only that run's identity; it never re-reads
    ``tasks.current_run_id``, which may already name a NEWER run whose live
    processes must not be touched (Prism a8211e2aced2). ``None`` skips the
    run-scoped sweeps.

    ``owner_window`` is the recorded run's owner window
    ``(claimed_at, spawned_at, start_token)`` from :func:`_worker_owner_window`. It is a
    REQUIRED keyword so no call site can signal a PID without an identity
    check (t_0ae83825): liveness is not identity, and a dead, unreaped
    worker's PID can be reused by an unrelated process -- a macOS daemon or
    another card's live worker. A live PID is classified by
    :func:`_owner_identity` BEFORE any signal:

    * ``recycled`` -- provably not the recorded worker. Never signalled; the
      recorded worker is gone, so ``terminated=True, identity_mismatch=True``.
    * ``unverified`` -- create time unreadable. Fail CLOSED: never signalled
      and never released (``liveness_unprovable``).
    * ``verified`` -- signalled exactly as before.

    ``started_at`` is the spawn-time fingerprint upstream records on the row
    (``tasks.worker_started_at``): when the live process no longer matches it,
    the PID was recycled and nothing is signalled — the worker is gone, which
    is what the reclaim wanted (``terminated`` = True, ``pid_recycled``). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never
    signalled either, but it is reported as surviving (``signal_refused`` +
    ``liveness_unprovable``) so the reclaim holds the claim instead of
    spawning a duplicate beside it. ``None`` (legacy row / fork-only caller)
    falls through to the owner-window identity check alone.
    """
    import signal

    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not claim_lock:
        return info

    host_prefix = f"{_kb._claimer_id().split(':', 1)[0]}:"
    if not str(claim_lock).startswith(host_prefix):
        return info
    info["host_local"] = True

    if not pid or pid <= 0:
        # OUR host holds this claim but no worker pid was ever stamped. That
        # is UNKNOWN liveness unless we can prove otherwise (t_09180e10).
        info["liveness_unprovable"] = True
        if conn is None or task_id is None:
            # Fail closed on omission: without the run row we cannot rule out
            # a detached worker whose pid was never stamped, so a caller that
            # forgets conn/task_id must hold the claim, never release it.
            info["unstamped_worker_check"] = "skipped_no_run_context"
            return info
        release_at, basis, _ = _kb._dead_claimer_release_at(conn, task_id)
        if basis == "operator_claim_no_worker":
            # An operator review claim never spawns a worker, so the claimer's
            # liveness says nothing about one. The gateway's in-process
            # ``/kanban claim --review`` records the long-lived gateway pid,
            # which would otherwise hold the card for as long as the gateway
            # runs (FleetReview fd7f0d736976, t_c3cf232e).
            info["dead_claimer_release_basis"] = basis
            info["liveness_unprovable"] = False
            info["terminated"] = True
            return info
        claimer_pid = 0
        try:
            claimer_pid = int(str(claim_lock)[len(host_prefix):])
            if claimer_pid <= 0:
                return info
            # Equivalent to kill(pid, 0), without Windows' destructive
            # CTRL_C_EVENT behavior for signal 0.
            _kb.psutil.Process(claimer_pid)
            # Claimer alive: its spawn may still be in flight (launch grace).
            return info
        except _kb.psutil.NoSuchProcess:
            pass
        except (ValueError, OSError, _kb.psutil.AccessDenied):
            return info
        # The claimer is dead, but that alone does not prove no worker exists:
        # Popen(start_new_session=True) precedes _set_worker_pid, so a claimer
        # killed in between leaves a live, unstamped orphan. Release only once
        # no worker can still be alive for THIS run (see _dead_claimer_release_at).
        info["claimer_pid_dead"] = claimer_pid
        release_at, basis, evidence_kind = _kb._dead_claimer_release_at(conn, task_id)
        info["dead_claimer_release_basis"] = basis
        if evidence_kind:
            info["unstamped_worker_evidence"] = evidence_kind
        if release_at is None or int(time.time()) < release_at:
            info["dead_claimer_hold_until"] = release_at
            return info
        info["liveness_unprovable"] = False
        info["terminated"] = True
        return info

    verified_alive_at: Optional[float] = None
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        if _kb._pid_alive(pid):
            info["liveness_unprovable"] = True
            info["needs_attention"] = True
        else:
            info["terminated"] = True
        return info
    if started_at is not None and started_at != UNVERIFIED_WORKER_FINGERPRINT \
            and _kb._pid_alive(pid) and _pid_recycled(pid, started_at):
        info["terminated"] = True
        info["pid_recycled"] = True
        info["identity_mismatch"] = True
        return info
    if _kb._pid_alive(pid):
        verified_alive_at = time.time()
        identity = _kb._owner_identity(int(pid), *owner_window)
        info["owner_identity"] = identity
        if identity == "recycled":
            # The recorded worker is gone; this PID now belongs to someone
            # else. Signalling it would kill an unrelated process, and
            # holding the claim for it would defer forever.
            info["identity_mismatch"] = True
            info["terminated"] = True
            return info
        if identity == "unverified":
            info["identity_unverifiable"] = True
            info["liveness_unprovable"] = True
            info["needs_attention"] = True
            return info
        if not signal_allowed:
            # The caller may reclaim a DEAD run but never end a live one
            # (the pr-owner reclaimer, t_0a9a20ea: it SIGTERM'd a heartbeating
            # worker and only then recorded the refusal). Report the live,
            # verified worker and send nothing.
            info["signal_refused"] = True
            info["worker_alive_unsignalled"] = True
            return info
    elif not signal_allowed:
        # Already gone: nothing to signal, reap its leftovers as a kill would.
        info["terminated"] = True
        _kb._reap_terminated_worker_session(
            pid, info, signal_fn, owner_window, None, conn, task_id, run_id,
        )
        return info

    kill = signal_fn if signal_fn is not None else (
        os.kill if hasattr(os, "kill") else None
    )
    if kill is None:
        info["terminated"] = not _kb._pid_alive(pid)
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Process is already gone — that's a successful termination, not a
        # survival. Leaving terminated=False here would make the reclaim guard
        # misread a dead worker as still-alive and defer forever.
        info["terminated"] = True
        _kb._reap_terminated_worker_session(
            pid, info, signal_fn, owner_window, verified_alive_at, conn, task_id, run_id,
        )
        return info
    except PermissionError:
        # Identity was verified above (or the pid was not visibly alive), so
        # this is OUR recorded worker running under another uid: hold it and
        # surface it rather than retrying silently every tick.
        info["signal_error"] = "EPERM"
        info["needs_attention"] = True
        return info
    except OSError:
        return info

    for _ in range(10):
        if not _kb._pid_alive(pid):
            info["terminated"] = True
            _kb._reap_terminated_worker_session(
                pid, info, signal_fn, owner_window, verified_alive_at, conn, task_id, run_id,
            )
            return info
        time.sleep(0.5)

    if _kb._pid_alive(pid):
        try:
            # signal.SIGKILL doesn't exist on Windows; fall back to SIGTERM
            # (which maps to TerminateProcess via the stdlib shim).
            _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill(int(pid), _sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError):
            return info

    info["terminated"] = not _kb._pid_alive(pid)
    if info["terminated"]:
        _kb._reap_terminated_worker_session(
            pid, info, signal_fn, owner_window, verified_alive_at, conn, task_id, run_id,
        )
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    alive = _worker_alive(pid, fingerprint)
    termination = None
    if alive:
        # The fork's termination contract needs the recorded run's owner window
        # (identity before any signal, t_0ae83825); the closed run row is the
        # run that recorded this pid. Without it the call raised TypeError,
        # the per-row guard swallowed it, and no terminal worker was ever reaped.
        termination = _kb._terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint,
            owner_window=_kb._worker_owner_window(conn, row["task_id"], pid, row["id"]),
            conn=conn, task_id=row["task_id"], run_id=row["id"],
        )
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when a host-local worker has NOT been proven gone.

    A signalled-but-still-alive worker is positive liveness evidence. A
    missing worker pid is UNKNOWN liveness if the claimer may still launch or
    the current run has evidence of an unstamped worker. Unknown liveness must
    not release a claim: it could spawn a second worker beside the first.
    """
    if not termination.get("host_local") or termination.get("terminated"):
        return False
    return bool(
        termination.get("termination_attempted")
        or termination.get("liveness_unprovable")
        or termination.get("worker_alive_unsignalled")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    progress_at: Optional[float] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    ``progress_at`` is the agent's last real progress time (API call,
    stream chunk, tool call). It rides on the event payload so the stall
    detector can tell a live wrapper from a progressing model loop.

    Called by long-running workers as a liveness signal orthogonal to
    the PID check. A worker that forks a long-lived child (train loop,
    video encode, web crawl) can have its Python still alive while the
    actual work process is stuck; periodic heartbeats catch that.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        payload: dict[str, Any] = {}
        if note:
            payload["note"] = note
        if progress_at is not None:
            payload["progress_at"] = int(progress_at)
        _kb._append_event(
            conn, task_id, "heartbeat",
            payload or None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock, t.current_run_id "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL (5 s grace) through the shared helper, so the
        # owner-identity check guards this path too (t_0ae83825): a recycled
        # PID is never signalled and proves the recorded worker gone.
        termination = _kb._terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn,
            started_at=_kb._row_get(row, "worker_started_at"),
            owner_window=_kb._worker_owner_window(conn, tid, pid, row["current_run_id"]),
            conn=conn, task_id=tid, run_id=row["current_run_id"],
        )
        killed = bool(termination.get("sigkill"))
        if not termination.get("terminated"):
            # Signal delivery (including SIGKILL) is not proof of death.
            # Keep the owner and run intact until a later tick proves it gone.
            with _kbc.write_txn(conn):
                current = conn.execute(
                    "SELECT current_run_id FROM tasks WHERE id=? AND status='running' "
                    "AND worker_pid=? AND claim_lock IS ?",
                    (tid, pid, row["claim_lock"]),
                ).fetchone()
                if current and not conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='timeout_refused' "
                    "AND run_id=? LIMIT 1", (tid, current["current_run_id"]),
                ).fetchone():
                    refused = {"pid": pid, "sigkill": bool(termination.get("sigkill")),
                               "needs_attention": True}
                    for key in ("owner_identity", "identity_unverifiable", "signal_error"):
                        if key in termination:
                            refused[key] = termination[key]
                    _kb._append_event(conn, tid, "timeout_refused", refused,
                                  run_id=current["current_run_id"])
            continue

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                if termination.get("identity_mismatch"):
                    payload["identity_mismatch"] = True
                run_id = _kb._end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                _kb._append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
                )
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
            )
    return timed_out

# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       t.current_run_id, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Terminate the worker if it's still host-local.
        termination = _kb._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, conn=conn, task_id=tid,
            run_id=row["current_run_id"],
            owner_window=_kb._worker_owner_window(conn, tid, pid, row["current_run_id"]),
            started_at=_kb._row_get(row, "worker_started_at"),
        )

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND current_run_id IS ?",
                (retry_status, tid, row["claim_lock"], row["current_run_id"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    Tracked-state vs. reality divergence: a task can sit in
    ``status='running'`` with ``claim_lock IS NULL`` or ``claim_expires IS
    NULL`` (crash mid-claim, manual SQL, DB restore). None of the other
    recovery paths ever touch such a card — ``release_stale_claims``
    requires a non-NULL ``claim_expires``, ``detect_crashed_workers``
    requires a host-local claim_lock + worker_pid, and
    ``detect_stale_running`` is disabled by default — so the card shows
    Running forever (a zombie).

    This pass finds those orphans, requeues them to ``ready`` with an
    explanatory comment, closes any leaked run, and appends a
    ``reconciled`` event. If the orphan row still records a live PID on
    this host, requeueing is deferred to a later tick so we never spawn a
    duplicate beside a possibly-alive worker. If THIS host holds the claim
    but no PID is stamped, liveness is unprovable: the card keeps its owner
    and a ``reconcile_refused`` (needs_attention) event is recorded instead.

    Returns the list of reconciled task ids. Safe to call every tick.

    Idea from openai/symphony's tracker reconciliation (Apache-2.0).
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        if pid and _kb._recorded_worker_alive(conn, tid, pid):
            # The recorded worker may still be doing real work — never
            # requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        host_prefix = f"{_kb._claimer_id().split(':', 1)[0]}:"
        if not pid and str(row["claim_lock"] or "").startswith(host_prefix):
            # THIS host claimed the card but no worker pid is stamped: the
            # launch may still be in flight (workspace setup takes minutes).
            # Missing pid is not death evidence -- fail closed, keep the
            # owner, and surface the card once per run for a human.
            with _kbc.write_txn(conn):
                run_id = _kb._current_run_id(conn, tid)
                seen = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? "
                    "AND kind = 'reconcile_refused' AND run_id IS ? LIMIT 1",
                    (tid, run_id),
                ).fetchone()
                if seen is None:
                    _kb._append_event(
                        conn, tid, "reconcile_refused",
                        {"reason": "liveness_unprovable",
                         "claim_lock": row["claim_lock"],
                         "claim_expires": row["claim_expires"],
                         "needs_attention": True},
                        run_id=run_id,
                    )
            continue
        with _kbc.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()

# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Provider REJECTIONS (HTTP 4xx from the model provider) count as failures and
# block the card after this many in a row (t_d564a8dd). Separate from
# ``failure_limit`` so an operator's dispatcher setting of 1 or 5 does not
# change how many identical 404s a dead pin is allowed.
_PROVIDER_REJECTED_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks the task's closed runs newest-first — including the violation run
    ``detect_crashed_workers`` just closed — and counts how many in a row were
    clean-exit protocol violations:

    * ``rate_limited`` runs are neutral and skipped: a quota wall says nothing
      about the task, exactly as it is neutral for the unified
      ``consecutive_failures`` counter. ``infra_unavailable`` runs (the worker
      CLI could not be executed at all) are neutral for the same reason — the
      task never started, so it cannot have violated anything.
    * Any other closed run (completed, plain crash, timeout, spawn failure,
      reclaim, …) breaks the streak, so the bounded retry budget counts ONLY
      protocol violations — mixed failure kinds can neither consume nor
      extend it.

    Violation runs are recognized by the ``protocol_violation`` marker that
    ``detect_crashed_workers`` stamps into the run metadata; the violation
    error text is matched as a fallback for runs recorded before the marker
    existed.
    """
    return _kb._protocol_violation_history(conn, task_id)[0]
_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)

# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")


def _exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def _log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))


def _worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", raw)
    cut = raw.rfind(_exit_summary_marker())
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith(_log_noise_prefixes()):
            lines.append(ln)
    return " ".join(lines)[-400:]


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall.
        return "rate_limited" if self.rate_limited else "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], *, task_id: Optional[str] = None, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited requeue does not need it.
    """
    dead = _classify_dead_worker_exit(pid, claimer, task_id=task_id, board=board)
    if task_id and not dead.rate_limited:
        worker_output = _worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f" Worker's last output: {worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    The reap registry only knows children of THIS process; a per-tick dispatcher
    reads the exit trailer the worker left in its log instead, so the same death
    gets the same booking (protocol violation / rate-limit requeue / crash) as
    under the gateway-embedded dispatcher. A worker that never reached its exit
    epilogue (killed, OOM) leaves no trailer and stays a plain crash.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _worker_alive(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                continue

            pid = int(row["worker_pid"])
            dead = _classify_dead_worker(pid, row["claim_lock"], task_id=row["id"], board=board)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead.event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, dead in crash_details:
        fp = _error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider:
            # A retry cannot heal a revoked credential or a missing model, so
            # the whole ``failure_limit`` budget would be spent on identical
            # failures. ``force_trip`` blocks now, sticky: ``recompute_ready``
            # must not auto-resume it before the operator fixes the provider.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            extra = {"pid": pid, "claimer": claimer}
            if is_systemic:
                # Trips at 1, below any ``failure_limit``: hold it for an operator.
                extra["sticky"] = True
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and restores the task's source phase.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).

    When the reap registry shows the worker exited cleanly (rc=0) but
    the task was still ``running`` in the DB, treat it as a protocol
    violation (worker answered conversationally without calling
    ``kanban_complete`` / ``kanban_block``). It gets a bounded retry budget;
    only two identical, boundary-delimited worker responses can stop it early.
    Clean exits without positive receipt evidence of a model response (for
    example a pre-model provider abort) can never take that shortcut.

    When the reap registry shows the worker exited with the rate-limit
    sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``), the worker bailed on a
    provider quota wall, NOT a task failure. Such tasks are released back
    to its source phase WITHOUT counting a failure (so a long quota window can't
    trip the breaker) and stamped with a quota-blocker error so
    ``check_respawn_guard`` defers their respawn until the window clears.
    The ids are returned via the ``_last_rate_limited`` function attribute
    (the public return stays the crashed-only ``list[str]``).
    """
    crashed: list[str] = []
    rate_limited: list[str] = []
    infra_unavailable: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case, which is accounted against its
    # own bounded violation streak instead of the unified failure
    # counter (see the post-txn loop below).
    crash_details: list[tuple[str, int, str, bool, str, Optional[str]]] = []
    # (task_id, pid, claimer, protocol_violation, error_text, stderr_tail)
    dead: list[tuple] = []  # (row, pid, exit_kind, exit_code) for every dead pid
    cohort_dead: list[str] = []
    # Cohort members whose pid was still in the process table: a misjudged
    # liveness check, not a kill (t_5c37ab9d). Named in the page.
    cohort_alive: list[str] = []
    # task id -> ``_liveness_evidence``, stamped on its ``crashed`` / ``gave_up`` payloads.
    liveness: dict[str, dict] = {}
    # Worker-exit observer payloads (RFC #58548), collected inside the main
    # txn and fired only after every reclaim/accounting txn has committed.
    exited_hook_payloads: list[dict] = []
    with _kbc.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock, started_at, assignee, current_run_id, "
            "       last_heartbeat_at "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_kb._claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Skip liveness check inside the launch-window grace period
            # so a freshly-spawned worker isn't reclaimed before its PID
            # is visible on /proc.
            started_at = row["started_at"] if "started_at" in row.keys() else None
            if started_at is not None:
                grace = _kb._resolve_crash_grace_seconds()
                if time.time() - started_at < grace:
                    continue
            # Identity, not bare liveness: a recycled holder of a dead
            # worker's PID must not hide the crash forever (t_0ae83825).
            if _kb._recorded_worker_alive(
                conn, row["id"], row["worker_pid"], row["current_run_id"],
            ):
                continue

            pid = int(row["worker_pid"])
            names_task = _live_pid_names_task(pid, row["id"])
            if names_task:
                # The fingerprint disagrees, but the pid is running ``work kanban task <id>``:
                # that is this card's worker. Booking it crashed spawns a duplicate beside it.
                _kb._log.warning(
                    "kanban: worker pid %s for %s fails the start fingerprint (recorded %s, live %s) "
                    "but still names the task; not reclaiming",
                    pid, row["id"], conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?",
                                                 (row["id"],)).fetchone()[0], _process_fingerprint(pid))
                continue
            kind, code = _kb._classify_run_exit(conn, row["id"], row["current_run_id"], pid)
            if kind == "unknown":
                # Neither the run receipt nor the reap registry (children of THIS
                # process only) saw the exit: a per-tick ``hermes kanban dispatch``
                # process reads the trailer the worker wrote to its own log, so the
                # same death gets the same booking as under the embedded dispatcher
                # (upstream ``_classify_dead_worker_exit``). No trailer = plain crash.
                logged = _worker_log_exit_code(row["id"], board=board)
                if logged is not None:
                    kind, code = _exit_code_kind(logged)
            dead.append((row, pid, kind, code))
            # Read now: the reclaim UPDATE below clears ``worker_started_at``.
            liveness[row["id"]] = _liveness_evidence(conn, row, pid, kind, code, names_task=names_task)
            if not _kb._pid_alive(pid):
                # The worker died on its own; whatever it left in other
                # process groups of its session would outlive it (a live pid
                # here is a recycled one and is never used as a sid). Only a
                # session with a member from THIS run is signalled.
                _born_after = _kb._worker_owner_window(
                    conn, row["id"], pid, row["current_run_id"],
                )[0]
                _born_before = _kb._run_last_evidence_at(
                    conn, row["id"], row["current_run_id"],
                )
                _kb._reap_worker_session(
                    pid, born_after=_born_after, born_before=_born_before,
                )
                # ...and whatever setsid()'d OUT of that session but still
                # carries the run's identity (browser harness daemons).
                _kb._reap_run_env_escapees(
                    row["id"], row["current_run_id"],
                    born_after=_born_after, born_before=_born_before,
                )
        # N workers ending externally in the same tick while all were still
        # heartbeating is ONE event (something outside killed them), not N
        # independent task failures. Decided over the whole tick before any
        # row is accounted, so the systemic-fingerprint rule below can never
        # turn a cohort kill into N immediate ``gave_up`` blocks.
        cohort_ids = _kb._cohort_death_ids(dead, now=time.time())
        for row, pid, kind, code in dead:
            rate_limited_exit = False
            cohort_death = row["id"] in cohort_ids
            stderr_exit_class = None
            provider_rejected_line = None
            if kind == "nonzero_exit" and not cohort_death:
                stderr_exit_class = _kb._stderr_cooldown_class(
                    _kb._worker_log_run_segment(row["id"], board=board)
                )
                if stderr_exit_class is not None:
                    kind = "rate_limited"
            if kind in ("rate_limited", "nonzero_exit") and not cohort_death:
                # Whatever the worker said ("wall", or a plain nonzero exit), its own
                # last lines carry the provider's verdict. A 400/401/403/404 is a
                # REJECTION (dead model id, bad credential): the same answer on every
                # retry, so it is a failure that counts toward the rejection streak,
                # never a requeue-without-a-strike (t_d564a8dd: 24 such exits booked
                # ``rate_limited`` for 8 h) and never generic crash accounting (the
                # pinned-worker refusal path now exits nonzero on a 4xx on purpose,
                # ``apply_pin_refusal_to_result``). 429/503/529 stay a wall.
                from hermes_cli.kanban_worker_exit import provider_rejected_status

                provider_rejected_line = provider_rejected_status(
                    _kb._worker_log_run_segment(row["id"], board=board)
                )
                if provider_rejected_line:
                    kind = "provider_rejected"
            if cohort_death:
                protocol_violation = False
                error_text = (
                    f"pid {pid} ended externally together with "
                    f"{len(cohort_ids) - 1} other worker(s) in one dispatcher "
                    f"tick while still heartbeating — cohort death, not a task "
                    f"failure; requeued without counting a failure"
                )
                event_kind = "cohort_death"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_kind": kind,
                    "exit_code": code,
                    "cohort_size": len(cohort_ids),
                    "cohort": sorted(cohort_ids)[:50],
                    "liveness": liveness[row["id"]],
                }
                event_payload.update(_cohort_member_evidence(
                    pid, _spawn_group(conn, row["id"], row["current_run_id"], pid),
                ))
                if event_payload["pid_alive"]:
                    cohort_alive.append(row["id"])
                stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                if stderr_tail:
                    event_payload["stderr_tail"] = stderr_tail
            elif kind == "clean_exit":
                # Worker subprocess returned 0 but its task is still
                # ``running`` in the DB — it exited without calling
                # ``kanban_complete`` / ``kanban_block``. Overwhelmingly the
                # work itself succeeded and only the paperwork was skipped, so
                # a retry usually completes; the corrective sentence below is
                # surfaced to the retry worker via the prior-attempt error in
                # ``build_worker_context`` (guidance approach from #61817).
                protocol_violation = True
                error_text = (
                    "worker exited cleanly (rc=0) without calling "
                    "kanban_complete or kanban_block — protocol violation. "
                    "If the work is already done, verify and report it via "
                    "kanban_complete (superseded_by=<card|PR|sha> if a sibling "
                    "got there first); a run that ends without a terminal "
                    "kanban call counts as failed."
                )
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    # Durable marker for _protocol_violation_streak: _end_run
                    # copies this payload into the run metadata, which is how
                    # the violation-only retry budget is derived later.
                    "protocol_violation": True,
                }
                # The worker's own last words are what distinguishes a
                # REPRODUCED no-op ("nothing to implement, already on main")
                # from three unrelated paperwork misses. Positive receipt
                # evidence that a model response was produced is mandatory:
                # bootstrap/provider failures have historically returned rc=0,
                # and identical infrastructure text is not worker work. The
                # per-task log is APPEND-mode across runs, so a raw tail of it
                # is a slice of every run concatenated; fingerprint the
                # boundary-delimited segment for THIS run instead. A legacy or
                # missing receipt, or an unsegmentable run, yields no fingerprint
                # and therefore keeps the early trip off.
                stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                if stderr_tail:
                    event_payload["stderr_tail"] = stderr_tail
                model_turn_completed = _kb._run_model_turn_completed(
                    conn, row["id"], row["current_run_id"]
                )
                event_payload["model_turn_completed"] = model_turn_completed
                if model_turn_completed:
                    run_fingerprint = _kb._run_output_fingerprint(
                        _kb._worker_log_run_segment(row["id"], board=board)
                    )
                    if run_fingerprint:
                        event_payload["run_output_fingerprint"] = run_fingerprint
            elif kind == "rate_limited":
                # Worker bailed because the provider rate-limited / exhausted
                # quota (EX_TEMPFAIL sentinel). This is NOT a task failure —
                # the task is fine, the account just hit a wall. Release it
                # back to its source phase so the respawn guard defers it until the
                # quota window clears, and crucially do NOT count a failure
                # (skip ``_record_task_failure``) so a long quota window can't
                # trip the circuit breaker and permanently block the card.
                protocol_violation = False
                rate_limited_exit = True
                exit_class = (
                    _kb._run_exit_class(conn, row["id"], row["current_run_id"])
                    or stderr_exit_class
                )
                _wall = {
                    "credential_cooldown": "credential cooldown",
                    "upstream_capacity": "provider capacity overload",
                    "pool_exhausted": "sub pool capped",
                    "pinned_provider_unavailable": "pinned provider unavailable",
                }.get(exit_class or "", "quota wall")
                error_text = (
                    f"pid {pid} exited rate-limited ({_wall}) — "
                    f"requeued without counting a failure"
                )
                event_kind = "rate_limited"
                # This run is still open, so the streak it completes is the
                # closed trailing streak + 1.
                _rl_hold = _kb._rate_limit_hold_seconds(
                    _kb.consecutive_rate_limited_runs(conn, row["id"]) + 1,
                    base=_kb._resolve_rate_limit_cooldown_seconds(),
                )
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    "next_eligible_at": int(time.time()) + _rl_hold,
                }
                if exit_class:
                    event_payload["exit_class"] = exit_class
                if stderr_exit_class is not None:
                    stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                    if stderr_tail:
                        event_payload["stderr_tail"] = stderr_tail
            elif kind == "infra_unavailable":
                # The worker HARNESS could not be executed (126/127) — the CLI
                # path was missing or unrunnable, so no worker code ran and the
                # task never had a chance to fail. Blaming the card for this is
                # what happened on 2026-09-21: a 38-minute deploy-tree outage
                # flipped six innocent argus runs to crashed/blocked.
                #
                # Same remedy as the quota wall (requeue, don't count a failure,
                # defer the respawn so we don't spin against a broken deploy),
                # but its OWN event kind + error text so the board names the
                # real cause instead of reporting a quota wall that never
                # happened.
                protocol_violation = False
                rate_limited_exit = True
                error_text = (
                    f"pid {pid} exited {code} — the worker CLI could not be "
                    f"executed (infrastructure, not this task); requeued "
                    f"without counting a failure"
                )
                event_kind = "infra_unavailable"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    "exit_class": "infra_unavailable",
                    "next_eligible_at": int(time.time()) + _kb._resolve_rate_limit_cooldown_seconds(),
                }
                # The harness never reached the model, so the only evidence of
                # WHY lives in the spawn log (e.g. the shim's "real CLI not
                # found" line). Carry it: without this the operator sees an
                # exit code and no cause.
                stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                if stderr_tail:
                    event_payload["stderr_tail"] = stderr_tail
            elif kind == "provider_rejected":
                protocol_violation = False
                error_text = (
                    f"pid {pid} exited on a provider rejection — {provider_rejected_line}; "
                    f"counts as a failure (a 4xx answers the same way on every retry)"
                )
                event_kind = "provider_rejected"
                event_payload = {
                    "pid": pid, "claimer": row["claim_lock"], "exit_kind": kind,
                    "exit_code": code, "provider_status": provider_rejected_line,
                    "provider_rejected": True,
                }
                stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                if stderr_tail:
                    event_payload["stderr_tail"] = stderr_tail
            elif kind == "terminal_provider":
                # EX_CONFIG (upstream 2b94b0d40f): the provider rejected this
                # profile's credential or model — retrying cannot help, so the
                # accounting loop trips the breaker on the first exit.
                protocol_violation = False
                error_text = (
                    f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
                    "this profile's credential or model — fix the configuration, then unblock."
                )
                event_kind = "crashed"
                event_payload = {
                    "pid": pid, "claimer": row["claim_lock"], "exit_kind": kind,
                    "exit_code": code, "terminal_provider": True,
                    "liveness": liveness[row["id"]],
                }
            else:
                protocol_violation = False
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": row["claim_lock"], "liveness": liveness[row["id"]]}
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code
                stderr_tail = _kb._worker_log_stderr_tail(row["id"], board=board)
                if stderr_tail:
                    event_payload["stderr_tail"] = stderr_tail

            # Upstream #88603 / #46593: a clean exit or a crash carries the
            # worker's own last output so the board and the retry worker see
            # WHY instead of a bare label; a rate-limited / infra requeue and a
            # cohort death do not need it.
            if not rate_limited_exit and not cohort_death:
                worker_output = _worker_final_output(row["id"], board=board)
                if worker_output:
                    error_text += f" Worker's last output: {worker_output!r}"
                    event_payload["worker_output"] = worker_output

            retry_status = _kb._retry_status_for_run(conn, row["id"])
            event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                # Rate-limited requeues are a clean release, not a crash —
                # record the run outcome as ``rate_limited`` so the board
                # history doesn't show a phantom crash for a quota wall.
                # An infra-unavailable requeue is the same shape but a
                # different cause, so it gets its own outcome rather than
                # being filed under a quota wall it never hit.
                if cohort_death:
                    _run_outcome = "cohort_death"
                elif kind == "infra_unavailable":
                    _run_outcome = "infra_unavailable"
                elif kind == "provider_rejected":
                    _run_outcome = "provider_rejected"
                elif rate_limited_exit:
                    _run_outcome = "rate_limited"
                else:
                    _run_outcome = "crashed"
                run_id = _kb._end_run(
                    conn, row["id"],
                    outcome=_run_outcome, status=_run_outcome,
                    error=error_text,
                    metadata=dict(event_payload),
                )
                _kb._append_event(
                    conn, row["id"], event_kind,
                    event_payload,
                    run_id=run_id,
                )
                exited_hook_payloads.append({
                    "task_id": row["id"],
                    "assignee": row["assignee"],
                    "run_id": run_id,
                    "worker_pid": pid,
                    "exit_kind": kind,
                    "exit_code": code,
                    "outcome": _run_outcome,
                    "retry_status": retry_status,
                })
                if cohort_death:
                    # Released like a quota wall (no failure counted) but with
                    # no respawn deferral: the task was healthy, respawn it.
                    cohort_dead.append(row["id"])
                elif rate_limited_exit:
                    # Stamp the failure-error column so ``check_respawn_guard``
                    # recognizes this as a quota blocker and defers the
                    # respawn until the window clears — WITHOUT touching
                    # ``consecutive_failures`` (that's the whole point: no
                    # breaker trip on a throttle).
                    conn.execute(
                        "UPDATE tasks SET last_failure_error = ?, next_eligible_at = ? WHERE id = ?",
                        (error_text[:500], event_payload["next_eligible_at"], row["id"]),
                    )
                    # Same deferral mechanics, separate ledger: an infra
                    # outage and a quota wall must not be reported as the
                    # same thing on the board or in dispatch telemetry.
                    if kind == "infra_unavailable":
                        infra_unavailable.append(row["id"])
                    else:
                        rate_limited.append(row["id"])
                else:
                    if protocol_violation:
                        # Stamp the failure error now: a below-budget
                        # violation never reaches ``_record_task_failure``
                        # (which stamps this column for every other failure
                        # kind), yet the board UI and the retry worker's
                        # context still need the violation message + the
                        # corrective guidance it carries.
                        conn.execute(
                            "UPDATE tasks SET last_failure_error = ? "
                            "WHERE id = ?",
                            (error_text[:500], row["id"]),
                        )
                    crashed.append(row["id"])
                    crash_details.append(
                        (row["id"], pid, row["claim_lock"],
                         protocol_violation, error_text,
                         event_payload.get("stderr_tail"))
                    )
    # Outside the main txn: account each crashed task and maybe trip the
    # breaker (the retried task transitions to blocked with a ``gave_up`` event
    # on top of the event we already emitted).
    #
    # Protocol-violation crashes (clean exit, no terminal tool call) get a
    # BOUNDED retry, not an immediate trip: empirically ~96% of these tasks
    # complete on a later run (a goal-mode finalize nudge, or the model simply
    # emitting kanban_complete/kanban_block next time), so blocking on the first
    # occurrence just churned them through the respawn cycle. The retry budget
    # is a violation-only streak (``_protocol_violation_streak``): earlier
    # timeouts / nonzero exits neither consume nor extend it, and a
    # below-budget violation does not tick the unified
    # ``consecutive_failures`` counter, so the two budgets stay independent.
    # A per-task ``max_retries`` overrides the violation bound with the same
    # top precedence it has for every other failure kind. Systemic same-error
    # crashes still trip immediately.
    auto_blocked: list[str] = []
    if crash_details:
        # Fingerprint errors to detect systemic failures.
        _fp_counts: dict[str, int] = {}
        for _, _, _, _, err_text, _ in crash_details:
            fp = _error_fingerprint(err_text)
            _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
        for (
            tid,
            pid,
            claimer,
            protocol_violation,
            error_text,
            stderr_tail,
        ) in crash_details:
            if protocol_violation:
                streak, identical = _kb._protocol_violation_history(conn, tid)
                trow = conn.execute(
                    "SELECT max_retries FROM tasks WHERE id = ?", (tid,),
                ).fetchone()
                if trow is None:
                    continue  # task deleted mid-loop
                task_override = (
                    trow["max_retries"] if "max_retries" in trow.keys() else None
                )
                violation_limit = (
                    int(task_override)
                    if task_override is not None
                    else _PROTOCOL_VIOLATION_FAILURE_LIMIT
                )
                # Two byte-identical clean exits are a REPRODUCED no-op,
                # not a flake: the worker reached the same dead end twice, so
                # a third spawn buys nothing. Stop early and surface it as
                # needing input rather than burning the rest of the budget on
                # the same run. (The usual cause — a premise already satisfied
                # on main — now has an honest verb: superseded_by.)
                reproduced = identical >= _kb._PROTOCOL_VIOLATION_REPRODUCED_LIMIT
                if streak < violation_limit and not reproduced:
                    # Below budget: the task is already back at ``ready``
                    # (respawn allowed) with ``last_failure_error`` stamped.
                    # Deliberately no ``_record_task_failure`` call — a
                    # below-budget violation must not consume the unified
                    # failure budget, just as other failure kinds don't
                    # consume this one.
                    continue
                violation_extra = {
                    "pid": pid,
                    "claimer": claimer,
                    "liveness": liveness.get(tid),
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                }
                if reproduced:
                    violation_extra["identical_violations"] = identical
                    violation_extra["stopped_early"] = "reproduced_clean_exit"
                    # PREPENDED, not appended: ``_record_task_failure`` caps
                    # the stored error at 500 chars and the canned violation
                    # text plus the worker's own output already fills it, so a
                    # trailing hint would be truncated away unread.
                    error_text = (
                        f"Stopped retrying after {identical} IDENTICAL clean "
                        f"exits — the worker reproduced the same no-op, so "
                        f"this needs input, not another attempt. If the card's "
                        f"premise was already satisfied, close it with `hermes "
                        f"kanban complete {tid} --superseded-by <card|PR|sha>`. "
                        f"{error_text}"
                    )
                # Streak reached the bound: trip the breaker. ``force_trip``
                # skips the threshold resolution inside
                # ``_record_task_failure`` because the decision — including
                # the per-task ``max_retries`` override — was already made
                # against the violation streak above.
                tripped = _record_task_failure(
                    conn, tid,
                    error=error_text,
                    outcome="crashed",
                    failure_limit=violation_limit,
                    force_trip=True,
                    release_claim=False,
                    end_run=False,
                    event_payload_extra=violation_extra,
                )
                if tripped:
                    auto_blocked.append(tid)
                continue
            if "exited on a provider rejection" in error_text:
                # Three identical rejections → blocked with a typed ``capability`` block
                # (sticky; fires ``kanban_task_blocked`` → owner wake). Below that, a
                # counted failure the respawn guard spaces like any crash.
                _rej_streak = _kb._provider_rejected_streak(conn, tid)
                tripped = _record_task_failure(
                    conn, tid,
                    error=error_text,
                    outcome="provider_rejected",
                    failure_limit=_PROVIDER_REJECTED_FAILURE_LIMIT,
                    force_trip=_rej_streak >= _PROVIDER_REJECTED_FAILURE_LIMIT,
                    release_claim=False,
                    end_run=False,
                    event_payload_extra={"pid": pid, "claimer": claimer, "provider_rejected": True,
                                         "provider_rejected_streak": _rej_streak,
                                         "liveness": liveness.get(tid)},
                    block_kind="capability" if _rej_streak >= _PROVIDER_REJECTED_FAILURE_LIMIT else None,
                )
                if tripped:
                    auto_blocked.append(tid)
                continue
            if "terminal provider error" in error_text:
                tripped = _record_task_failure(
                    conn, tid,
                    error=error_text,
                    outcome="crashed",
                    force_trip=True,
                    release_claim=False,
                    end_run=False,
                    event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True,
                                         "liveness": liveness.get(tid)},
                )
                if tripped:
                    auto_blocked.append(tid)
                continue
            fp = _error_fingerprint(error_text)
            is_systemic = _fp_counts.get(fp, 0) >= 3
            failure_payload = {"pid": pid, "claimer": claimer, "liveness": liveness.get(tid)}
            if is_systemic:
                # A same-error wave is a policy trip independent of the counter:
                # ``_has_sticky_block`` holds it for an operator (84c6e50339).
                failure_payload["sticky"] = True
            if stderr_tail:
                failure_payload["stderr_tail"] = stderr_tail
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=failure_payload,
            )
            if tripped:
                auto_blocked.append(tid)
    # Stash auto-blocked ids on the function for the dispatch loop to pick up.
    # Keeps the public return type (``list[str]``) stable for direct callers
    # and tests that destructure the result; ``dispatch_once`` reads this
    # side-channel attribute to populate ``DispatchResult.auto_blocked``.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    # Same side-channel for rate-limited requeues — these did NOT count a
    # failure and are NOT crashes, so they stay out of the ``crashed`` return.
    detect_crashed_workers._last_rate_limited = rate_limited  # type: ignore[attr-defined]
    # Same side-channel for harness-unavailable requeues (126/127): no
    # failure counted, not a crash, and NOT a quota wall.
    detect_crashed_workers._last_infra_unavailable = infra_unavailable  # type: ignore[attr-defined]
    detect_crashed_workers._last_cohort_deaths = cohort_dead  # type: ignore[attr-defined]
    if cohort_dead:
        _kb._log.error(
            "kanban cohort death: %d workers ended externally in one tick "
            "(no failure counted): %s",
            len(cohort_dead), ", ".join(cohort_dead),
        )
        _kb._page_cohort_death(conn, cohort_dead, still_alive=cohort_alive)
    # Worker-lifecycle observer (RFC #58548): exit events are tick-derived
    # from this reclaim pass — fired only now, after the main reclaim txn
    # AND the breaker accounting above have committed, so subscribers always
    # observe fully durable board state.
    if exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return crashed


def _host_fault(exc: BaseException) -> Optional[dict]:
    """Run metadata for a spawn the HOST refused, or None for a card fault.

    ``RestartSafeScopeUnavailable`` (#114720) and a pool host that cannot
    create the workspace (t_9fb4d477) never charge the card's retries. The
    latter also quarantines the (host, board) pair and pages once, and skips
    the infrastructure cooldown: placement already routes around the pair,
    so the card spills on the next tick instead of waiting."""
    from tools.process_registry import RestartSafeScopeUnavailable

    if isinstance(exc, RestartSafeScopeUnavailable):
        return {}
    if isinstance(exc, _kb._kwh.RemoteWorkspaceUnavailable):
        _kb._kwh.note_board_fault(exc.host, exc.board, str(exc))
        return {"board_fault": {"host": exc.host, "board": exc.board}}
    return None


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    block_kind: Optional[str] = None,
    infrastructure: bool = False,
    run_metadata_extra: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a non-success outcome (spawn_failed / crashed / timed_out)
    and maybe trip the circuit breaker.

    Unified replacement for the old spawn-only ``_record_spawn_failure``.
    Every path that ends a task with a non-success outcome funnels
    through here so the ``consecutive_failures`` counter and the
    auto-block threshold stay consistent.

    Returns True when the task was auto-blocked (counter reached
    ``failure_limit``), False when it was just updated in place.

    Modes:

    * ``release_claim=True, end_run=True`` — spawn-failure path.
      Caller has a running task with an open run; this transitions
      it back to its source phase (or ``blocked`` when the breaker trips),
      releases the claim, and closes the run with ``outcome=<outcome>``.

    * ``release_claim=False, end_run=False`` — timeout/crash path.
      Caller has ALREADY restored the task's source phase and closed the
      run with the appropriate outcome. This just increments the
      counter; if the breaker trips, the task is re-transitioned
      into ``blocked`` and a ``gave_up`` event is emitted.

    ``event_payload_extra`` merges into the ``gave_up`` event payload
    when the breaker trips, so callers can include outcome-specific
    context (e.g. pid on crash, elapsed on timeout).

    Resolution order for the effective threshold:
      1. per-task ``max_retries`` if set (nothing else overrides)
      2. caller-supplied ``failure_limit`` (gateway passes the config
         value from ``kanban.failure_limit``; tests pass fixed values)
      3. ``DEFAULT_FAILURE_LIMIT``

    ``force_trip=True`` trips the breaker unconditionally, skipping the
    counter-vs-threshold comparison (the resolution order above is then
    only reported in the ``gave_up`` payload, not re-evaluated). Callers
    use it when they have already applied their own bounded-retry policy
    — e.g. the clean-exit protocol-violation streak in
    ``detect_crashed_workers``, which resolves the per-task
    ``max_retries`` override against the violation streak itself. The
    failure is still counted into ``consecutive_failures``.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with _kbc.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id, "
            "block_kind, block_recurrences "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        # ``expected_run_id``: the caller's claimed run must still own the card, checked inside this write
        # transaction, so a successor run that claimed it meanwhile is never reset (Prism 606e8aff45d3).
        if expected_run_id is not None and (row["status"] != "running"
                                            or row["current_run_id"] != int(expected_run_id)):
            return None
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        # ``infrastructure=True``: the host refused the spawn (no restart-safe
        # scope, #114720) — the run is closed with ``infrastructure: true`` but
        # ``consecutive_failures`` is left alone and the breaker never trips.
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over both caller-supplied and default
        # thresholds. None (the common case) falls through.
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if not infrastructure and (force_trip or failures >= effective_limit):
            # Trip the breaker.
            # ``block_kind`` types the resulting block when the caller knows
            # WHY it is unrecoverable (e.g. an unusable workspace anchor is a
            # ``capability`` wall a human must re-point). None keeps the legacy
            # untyped block.
            _bk = block_kind if block_kind in _kb.VALID_BLOCK_KINDS else None

            # A TYPED breaker block is a deliberate, human-gated stop — it must
            # behave like ``block_task``, not like an ordinary breaker trip:
            #
            #  * ``gave_up`` alone is NOT sticky (``_has_sticky_block`` reads
            #    'blocked'/'unblocked' events only), so ``recompute_ready``
            #    promotes the card straight back to ``ready`` on the next tick
            #    and it burns the retry budget anyway. We emit a real
            #    ``blocked`` event so the hold holds.
            #  * the unblock-loop breaker counts ``block_recurrences``. Without
            #    arming it, an operator who unblocks without fixing the cause
            #    gets an unbounded unblock -> re-block loop with no escalation.
            _recurrences = 0
            _typed_status = "blocked"
            if _bk:
                _prev_kind = (
                    row["block_kind"] if "block_kind" in row.keys() else None
                )
                _prev_recurrences = (
                    int(row["block_recurrences"])
                    if "block_recurrences" in row.keys()
                    and row["block_recurrences"] is not None
                    else 0
                )
                _recurrences = (
                    _prev_recurrences + 1 if _prev_kind == _bk else 1
                )
                if _recurrences >= _kb.BLOCK_RECURRENCE_LIMIT:
                    _typed_status = "triage"

            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ?, "
                    "block_kind = COALESCE(?, block_kind), "
                    "block_recurrences = CASE WHEN ? IS NULL "
                    "THEN block_recurrences ELSE ? END "
                    "WHERE id = ? AND status IN ('running', 'ready', 'review')",
                    (
                        _typed_status if _bk else "blocked",
                        failures, error[:500], _bk, _bk, _recurrences, task_id,
                    ),
                )
            else:
                # Timeout/crash path: source phase already restored with claim
                # cleared; just flip to blocked + update counter fields. A typed
                # ``block_kind`` (provider rejection streak) lands here too.
                conn.execute(
                    "UPDATE tasks SET status = ?, "
                    "consecutive_failures = ?, last_failure_error = ?, "
                    "block_kind = COALESCE(?, block_kind), "
                    "block_recurrences = CASE WHEN ? IS NULL "
                    "THEN block_recurrences ELSE ? END "
                    "WHERE id = ? AND status IN ('ready', 'review', 'running')",
                    (_typed_status if _bk else "blocked",
                     failures, error[:500], _bk, _bk, _recurrences, task_id),
                )
            run_id = None
            if end_run:
                # Only the spawn path has an open run to close.
                run_id = _kb._end_run(
                    conn, task_id,
                    outcome="gave_up", status="gave_up",
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "trigger_outcome": outcome,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                        "retry_status": retry_status,
                    },
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "error": error[:500],
                "trigger_outcome": outcome,
                "retry_status": retry_status,
            }
            if _bk:
                payload["block_kind"] = _bk
                payload["recurrences"] = _recurrences
                payload["block_status"] = _typed_status
            if force_trip:
                # The caller applied its own bounded policy, so the counter cannot
                # judge this block: ``recompute_ready`` holds it for an operator.
                payload["sticky"] = True
            if event_payload_extra:
                payload.update(event_payload_extra)
            _kb._append_event(
                conn, task_id, "gave_up", payload, run_id=run_id,
            )
            if _bk:
                # ``gave_up`` is deliberately NOT sticky — the breaker's normal
                # trips are meant to auto-recover. A typed block is a human
                # gate, so it also emits the event ``_has_sticky_block`` reads
                # (or ``block_loop_detected`` once the recurrence limit trips,
                # matching ``block_task``'s escalation).
                _kb._append_event(
                    conn, task_id,
                    "block_loop_detected" if _typed_status == "triage"
                    else "blocked",
                    {
                        "reason": error[:500],
                        "kind": _bk,
                        "recurrences": _recurrences,
                        "limit": _kb.BLOCK_RECURRENCE_LIMIT,
                        "source_status": retry_status,
                        "auto": True,
                    },
                    run_id=run_id,
                )
            blocked = True
        else:
            # Below threshold.
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: caller already restored the source phase.
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _kb._end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "retry_status": retry_status,
                        **({"infrastructure": True} if infrastructure else {}),
                        **(run_metadata_extra or {}),
                    },
                )
                _kb._append_event(
                    conn, task_id, outcome,
                    {
                        "error": error[:500],
                        "failures": failures,
                        "retry_status": retry_status,
                        **({"infrastructure": True} if infrastructure else {}),
                    },
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.
    return blocked


def _set_worker_pid(
    conn: sqlite3.Connection,
    task_id: str,
    pid: int,
    *,
    run_id: Optional[int] = None,
    pool: Optional[str] = None,
    placement: Optional[dict] = None,
) -> bool:
    """Record the spawned child's pid + emit a ``spawned`` event.

    ``placement`` (spec §4.1, t_7133e03d) is written as a ``placement``
    event in the same transaction as ``spawned``, when the run still holds
    the card.

    The event's payload carries the pid so a human reading ``hermes kanban
    tail`` can correlate log lines with OS-level traces without opening
    the drawer.

    ``run_id`` fences the write to the run that launched this process. If the
    card no longer belongs to that run (it was released while the spawn was
    in flight, and possibly re-claimed), the pid is NOT stamped onto the card
    -- that would overwrite a successor's pid -- and ``False`` is returned so
    the caller terminates the orphan. The ``spawned`` event is still recorded
    against the launching run so liveness checks can see the process.
    Measured on t_09180e10: the spawn landed 5 s after a reclaim.

    The payload also records the process's clock-step-immune start token
    (``start_token``, see :func:`_pid_start_token`), read BEFORE the write
    txn so neither DB lock lag nor a later wall-clock step can blur it. The
    claim guard matches a live PID against it to tell the genuine worker
    from a recycled PID. Omitted when unreadable (legacy window applies).
    """
    # Restart-stable fingerprint (``_process_fingerprint``): what lets every
    # later liveness/kill decision tell OUR worker from a process that
    # recycled the PID after a reboot. A failed capture is persisted as
    # ``UNVERIFIED_WORKER_FINGERPRINT``, never NULL: NULL is the legacy
    # pre-fingerprint row whose bare-PID kill authority a new spawn must not
    # inherit.
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    spawn_payload: dict[str, Any] = {"pid": int(pid), "started_at": started_at}
    start_token = _kb._pid_start_token(int(pid))
    if start_token is not None:
        spawn_payload["start_token"] = start_token
        boot_id = _kb._boot_id()
        if boot_id:
            spawn_payload["boot_id"] = boot_id
    # Process group / session / parent at spawn: a cohort death names the
    # group its members lived in and who else was still in it (t_5c37ab9d).
    spawn_payload.update(_process_group_evidence(int(pid)))
    if pool is not None:
        # The relay pool this spawn was charged to (t_38be6b10).
        spawn_payload["pool"] = pool
    with _kbc.write_txn(conn):
        if run_id is not None:
            held = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND status = 'running' "
                "AND current_run_id = ?",
                (task_id, int(run_id)),
            ).fetchone()
            if held is None:
                _kb._append_event(
                    conn, task_id, "spawned",
                    {**spawn_payload, "late_spawn": True,
                     "claim_lost": True},
                    run_id=int(run_id),
                )
                return False
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
            (int(pid), started_at, task_id),
        )
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                (int(pid), started_at, run_id),
            )
        _kb._append_event(conn, task_id, "spawned", spawn_payload, run_id=run_id)
        if placement is not None:
            from hermes_cli.kanban_placement_reasons import PLACEMENT_EVENT
            _kb._append_event(conn, task_id, PLACEMENT_EVENT, placement, run_id=run_id)
    return True


def adopt_worker_pid(conn: sqlite3.Connection, task_id: str, run_id: int, pid: int) -> bool:
    """Worker-side half of ``_set_worker_pid``, run by the worker before its first model call.

    A dispatcher killed between spawning the worker and ``_set_worker_pid`` leaves the run with no
    pid: no liveness check can see the worker, so a TTL expiry reclaims the card and spawns a second
    worker beside it. The worker fills the missing pid itself (``worker_registered``). False when
    ``run_id`` is no longer the card's live run: the card was reclaimed before this worker got here,
    and it must exit without working it."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        # Liveness checks are host-local: a pid from another host (or pid namespace) proves nothing here.
        if row["worker_pid"] is None and (row["claim_lock"] or "").startswith(_kb._host_prefix()):
            conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, task_id))
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, int(run_id)))
            _kb._append_event(conn, task_id, "worker_registered", {"pid": int(pid), "started_at": started_at},
                              run_id=int(run_id))
    return True


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def _ledger_pr_owner_stale_skips(
    conn: sqlite3.Connection, task_id: str, owners: list,
) -> None:
    """Record ``pr_owner_stale_skipped`` on each owner a heartbeat kept fresh (t_0a9a20ea).

    Once per owner run: the guard runs every tick, and the event is for the
    operator reading ``kanban tail`` after the fact, not a heartbeat mirror.
    """
    from . import kanban_pr_owner as _kpo

    for owner in owners:
        if owner.get("stale_skipped") != _kpo.STALE_SKIP_REASON:
            continue
        run_id = owner.get("run_id")
        already = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? "
            "AND run_id IS ? LIMIT 1",
            (owner["task_id"], _kpo.STALE_SKIP_EVENT, run_id),
        ).fetchone()
        if already is not None:
            continue
        with _kbc.write_txn(conn):
            _kb._append_event(
                conn, owner["task_id"], _kpo.STALE_SKIP_EVENT,
                {"reason": _kpo.STALE_SKIP_REASON, "pr": owner["pr"],
                 "waiting": task_id, "last_progress_at": owner.get("last_heartbeat_at"),
                 "head_committed_at": owner.get("head_committed_at")},
                run_id=run_id,
            )


def check_respawn_guard(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lane: str = "ready",
    pr_state_resolver: Optional[_kb._PrStateResolver] = None,
    detail: Optional[dict] = None,
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in its
    source phase and gets another chance on the next dispatcher tick.

    ``detail`` (optional dict) is filled in-place for the failure-derived
    reasons (``rate_limit_cooldown`` / ``blocker_auth``) with ``error`` (the
    stamped text), ``recorded_at`` (when the failing run ended) and
    ``eligible_at`` (when the guard stops deferring), so the dispatcher can
    name WHICH error is holding the task and HOW OLD it is.

    ``lane`` names the dispatch column the task is being spawned from
    (``"ready"`` or ``"review"``). In the review lane the
    ``recent_success`` and ``active_pr`` rules are skipped: a recent PR
    URL comment (and often a recent completed run) is the *precondition*
    of the canonical review handoff — a worker opened a PR and requested
    review — not a duplicate-work signal. Rate-limit cooldown and the
    auth-blocker check still apply in every lane.

    Checks in priority order:

    ``"infrastructure_cooldown"``
        The latest run is a ``spawn_failed`` the host refused (no restart-safe
        scope, #114720) within the rate-limit cooldown. Never counted.

    ``"rate_limit_cooldown"``
        The task's most recent run ended with the ``rate_limited`` outcome
        (a worker bailed on a provider quota wall via the EX_TEMPFAIL
        sentinel) within ``_resolve_rate_limit_cooldown_seconds()``. The
        quota almost certainly hasn't reset yet, so defer the respawn until
        the cooldown elapses — then allow a cheap probe. This is checked
        BEFORE ``blocker_auth`` because the rate-limit requeue stamps a
        quota-flavored ``last_failure_error`` that would otherwise match the
        auth-blocker regex and park the task forever (the rate-limit path
        never increments ``consecutive_failures``, so the breaker can't free
        it). Once the cooldown elapses the task falls through and respawns.

    ``"blocker_auth"``
        The task's last failure error matches a quota / authentication
        pattern. Retrying immediately is unlikely to help (rate limits
        reset on a timer; auth needs human action), so we defer to the
        next tick. The existing ``consecutive_failures`` counter still
        trips the auto-block circuit breaker after ``failure_limit``
        consecutive failures, so a persistent auth error eventually
        blocks via the normal path — but a transient 429 gets a few
        ticks of recovery first.

    ``"recent_success"``
        A completed run exists within ``_RESPAWN_GUARD_SUCCESS_WINDOW``
        seconds. Useful work already succeeded for this task; wait for an
        explicit re-queue rather than immediately re-spawning. Bypassed when an
        explicit re-queue event (status change, promote, unblock, reclaim)
        arrives AFTER that completion — that's a deliberate re-run request.

    ``"active_pr"``
        A GitHub PR URL appears in a recent task comment (within
        ``_RESPAWN_GUARD_PR_WINDOW`` seconds).  A prior worker already
        opened a PR; re-spawning risks a duplicate PR on the same task.
        A PR whose head branch names a DIFFERENT card id is skipped (see
        ``_pr_belongs_to_other_card``). An OPEN PR only the card's worker can
        move (mergeStateStatus DIRTY/BEHIND/UNSTABLE or a red check, see
        ``_pr_needs_its_worker``) holds the card ONLY while a prior worker is
        alive; a workerless card spawns so its worker can fix the PR.
        ``detail`` gets ``pr``, ``pr_state``, ``merge_state`` and ``hold``
        (``worker alive`` / ``PR mergeable, closer will land it`` /
        ``PR merge state unknown``) for the PR that is holding the card.

    Stale / dead claim locks are NOT a guard reason — they are handled
    by ``release_stale_claims`` and ``detect_crashed_workers`` which
    reset the task to ``ready`` only after verifying the lock is
    genuinely dead (no live PID on this host).
    """
    row = conn.execute(
        "SELECT last_failure_error, next_eligible_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 0. Same-incident overlap hold (t_ba30f0de). A card minted while another
    #    session's card for the same incident was <30 min old waits in ready
    #    for 10 min so the minting session can merge or withdraw it. An
    #    operator requeue verb after the hold releases it early.
    if lane == "ready":
        from . import kanban_overlap as _kov

        hold_until = _kov.overlap_hold_until(
            conn, task_id, _kb._RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS,
        )
        if hold_until is not None and now < hold_until:
            if detail is not None:
                detail["eligible_at"] = hold_until
            return "overlap_hold"

    # 0b. One running card per PR (t_cb70d390). A fresh owner holds the card;
    #     a stale one (run, heartbeat and PR head all >2 h old) is left in
    #     ``detail["pr_owner_stale"]`` for the dispatcher to reclaim.
    if lane == "ready":
        from . import kanban_pr_owner as _kpo

        owners = _kpo.spawn_owners(conn, task_id, now=now)
        fresh = [o for o in owners if not o["stale"]]
        _ledger_pr_owner_stale_skips(conn, task_id, fresh)
        if fresh:
            if detail is not None:
                detail.update(
                    pr=fresh[0]["pr"], owner=fresh[0]["task_id"],
                    pr_owner=fresh[0]["pr_owner"], hold=_kpo.describe(fresh[0]),
                )
            return _kpo.GUARD_REASON
        if owners:
            if detail is None:
                # No out-param means nobody can reclaim the stale owner:
                # hold rather than spawn a second writer beside it.
                return _kpo.GUARD_REASON
            detail["pr_owner_stale"] = owners

    # 1. Rate-limit cooldown. The most recent run ended ``rate_limited``
    #    (quota wall) — defer while inside the cooldown window, then allow a
    #    cheap probe. Must run BEFORE the blocker_auth regex check, because a
    #    rate-limit requeue stamps a quota-flavored last_failure_error that
    #    the regex would otherwise match → defer forever (no failure counter
    #    increment on this path means the breaker can never free it).
    #
    #    We look at the LATEST run only (ORDER BY ended_at DESC LIMIT 1): if a
    #    newer crash/completion superseded the rate-limit run, this guard
    #    no longer applies and the normal paths take over.
    #
    #    A requeue / lane-change event (``_RESPAWN_GUARD_FAILURE_RESET_KINDS``)
    #    at/after the rate-limited run resets the cooldown: the wall belonged
    #    to the provider lane the task was on when it hit it.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    failed_at: Optional[int] = (
        int(latest_run["ended_at"])
        if latest_run is not None and latest_run["ended_at"] is not None
        else None
    )
    # An infrastructure spawn refusal (#114720) shares the cooldown: the host
    # condition is not the card's, so it retries forever, spaced, and never
    # reaches the breaker.
    if latest_outcome == "spawn_failed":
        _meta = _kb._json_dict(latest_run["metadata"])
        if _meta.get("board_fault"):
            # A pool host could not create the workspace (t_9fb4d477): the
            # planner already skips that (host, board) pair, so the card may
            # spill on the very next tick. Its "Permission denied" text is a
            # host fact, never an auth blocker on the card.
            return None
        if rl_cooldown > 0 and _meta.get("infrastructure"):
            if failed_at is not None and (now - failed_at) < rl_cooldown:
                if detail is not None:
                    detail.update(recorded_at=failed_at, eligible_at=failed_at + rl_cooldown)
                return "infrastructure_cooldown"
    if latest_outcome in ("rate_limited", "infra_unavailable"):
        if failed_at is not None and _kb._respawn_guard_failure_reset_after(
            conn, task_id, failed_at,
        ):
            return None
        eligible_at = _kb._respawn_guard_eligible_at(
            row["next_eligible_at"], failed_at, rl_cooldown,
        )
        streak = 0
        if latest_outcome == "rate_limited" and failed_at is not None:
            # Escalate by the consecutive streak (5m/15m/45m/2h). Computed
            # here, not only at release, so rows stamped before this ladder
            # existed and any other release path are held too.
            streak = _kb.consecutive_rate_limited_runs(conn, task_id)
            laddered = failed_at + _kb._rate_limit_hold_seconds(streak, base=rl_cooldown)
            eligible_at = laddered if eligible_at is None else max(int(eligible_at), laddered)
        if eligible_at is not None and now < int(eligible_at):
            if detail is not None:
                detail.update(
                    error=row["last_failure_error"] or latest_run["error"],
                    recorded_at=failed_at,
                    eligible_at=int(eligible_at),
                )
                if streak > 1:
                    detail["backoff_streak"] = streak
            return "rate_limit_cooldown"
        # Cooldown elapsed (or disabled) — allow the respawn. Return early
        # so the blocker_auth check below doesn't catch the rate-limit text
        # we stamped on the task; this path intentionally retries forever
        # (cheaply, spaced by the cooldown) until quota returns or a real
        # crash/completion supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help. The
    #    stamped text is only evidence while it is CURRENT:
    #      * a newer run with a non-failure outcome (review_requested,
    #        completed, ...) supersedes it — the column is just history;
    #      * a requeue / lane-change event at/after the failing run means
    #        an operator (or the lifecycle) deliberately put the task back;
    #      * older than the provider cooldown (``next_eligible_at``, else
    #        ended_at + cooldown) it is no longer a blocker — allow a probe
    #        so the breaker can accumulate real failures instead of the
    #        task parking on one stale 429 forever.
    # A plain crash is different: its persisted error includes the worker's
    # last captured output, which is context rather than a diagnosis and may
    # contain benign commands such as ``claude auth status`` (#117097).
    err = _kb._lossy_text(row["last_failure_error"])
    if err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(err):
        if (
            latest_outcome is not None
            and latest_outcome not in _kb._RESPAWN_GUARD_FAILURE_OUTCOMES
        ):
            return None
        if failed_at is not None and _kb._respawn_guard_failure_reset_after(
            conn, task_id, failed_at,
        ):
            return None
        eligible_at = _kb._respawn_guard_eligible_at(
            row["next_eligible_at"], failed_at, rl_cooldown,
        )
        if eligible_at is not None and now >= int(eligible_at):
            return None
        if detail is not None:
            detail.update(
                error=err,
                recorded_at=failed_at,
                eligible_at=int(eligible_at) if eligible_at is not None else None,
            )
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR
    # URL comment are the canonical *inputs* to a review handoff (worker
    # opened a PR, then requested review), not signals of duplicate work.
    if lane == "review":
        return None

    # 3. Completed run within guard window — proof of recent success.
    #    Exception: an explicit re-queue AFTER that success (an operator
    #    dragging done→ready, a dependency re-promotion, an unblock, a
    #    reclaim) is a deliberate "run it again" — honor it instead of
    #    deferring. Without this, a manual done→ready just sits there,
    #    silently held by the guard, until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome IN " + _kb._SUCCESS_RUN_OUTCOMES_SQL + " AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. Recent GitHub PR comments. Guard while ANY referenced PR is open,
    #    unparseable, unqueryable, or beyond this tick's query budget. The
    #    duplicate-PR risk is gone only when ALL referenced PRs are closed.
    #    Exception: a fresh, unconsumed operator requeue AFTER the newest PR
    #    comment means the open PR is the fix-round target, not duplicate
    #    work. Worker dependency_wait followed by promotion grants the same
    #    one-shot continuation. Both are consumed by the next spawn.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    pr_urls: list[str] = []
    newest_pr_comment_at = 0
    assignee_row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    assignee = str(assignee_row["assignee"] or "") if assignee_row else ""
    for c in conn.execute(
        "SELECT body, created_at, author FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        body = _kb._lossy_text(c["body"]) or ""
        found = [match.group(0) for match in _RESPAWN_GUARD_PR_URL_RE.finditer(body)]
        if found:
            pr_urls.extend(found)
            # Only the assignee's own PR comment (or the kernel's completion
            # routing comment, author ``kanban``) can supersede a handoff: the
            # reviewer's send-back note and Prism's review posted right after a
            # changes_requested name the same PR but are not the worker acting
            # on it. Counting them held every merge-pass send-back behind
            # active_pr and paged #alerts (t_da538470).
            if c["author"] in {assignee, "kanban"} - {""}:
                newest_pr_comment_at = max(newest_pr_comment_at, int(c["created_at"] or 0))
    if pr_urls:
        if _kb._unused_operator_intent_after_pr(conn, task_id):
            return None
        # A handoff AFTER the assignee's newest PR comment (operator reassign
        # to a different profile, reviewer changes_requested, review reopen)
        # names the profile that must now work on THAT PR — a closer or the
        # implementer finishing it, not a duplicate implementation (#111910).
        # Strictly after: a same-second tie stays guarded (fail closed).
        handoff_events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND created_at > ? "
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened')",
            (task_id, newest_pr_comment_at),
        ).fetchall()
        if any(_is_handoff_event(e["kind"], e["payload"]) for e in handoff_events):
            return None
        resolver = pr_state_resolver or _kb._PrStateResolver()
        for url in dict.fromkeys(pr_urls):
            parsed = _kb._parse_github_pr_url(url)
            if parsed is None:
                if detail is not None:
                    detail.update(pr=url, pr_state="unparseable")
                return "active_pr"
            repo, number = parsed
            state = resolver.resolve(repo, number)
            if state in {"MERGED", "CLOSED"}:
                continue
            # Another card's PR mentioned here is not this card's work.
            if _kb._pr_belongs_to_other_card(repo, number, task_id):
                continue
            health = _kb._PR_MERGE_HEALTH_CACHE.get((repo.lower(), int(number))) or {}
            merge_state = health.get("merge_state")
            fixable = _kb._pr_needs_its_worker(repo, number) if state == "OPEN" else None
            if fixable is not None:
                # Only the card's own worker can rebase / fix this PR. Hold
                # only while that worker is alive; a workerless card spawns.
                if _kb._prior_worker_still_alive(conn, task_id) is None:
                    continue
                hold = "worker alive"
            elif merge_state in _kb._PR_MERGEABLE_STATES:
                hold = _kb.RESPAWN_GUARD_HOLD_MERGEABLE
            else:
                hold = "PR merge state unknown"
            if detail is not None:
                detail.update(
                    pr=f"https://github.com/{repo}/pull/{number}",
                    pr_state=state or "unknown",
                    hold=hold,
                    # Only a PR whose head branch names THIS card may be
                    # landed automatically (Prism #1545 cd0457757028).
                    pr_owned=_kb._pr_owned_by_card(repo, number, task_id),
                )
                if merge_state:
                    detail["merge_state"] = merge_state
                if fixable is not None:
                    detail["pr_needs"] = fixable
            return "active_pr"

    return None


def _is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee).

    When ``kanban.dispatch_profiles`` is set (#110995) the returned predicate
    additionally requires the assignee to be listed, fail-closed — so a card
    assigned to ``default`` is only claimable by homes that opted into it.
    Foreign assignees land in the existing ``skipped_nonspawnable`` bucket.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return None
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return profile_exists

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        return canon in allowlist and bool(profile_exists(name))

    return _gated


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is
    fail-closed: an empty list, ``null`` or a bare ``dispatch_profiles:`` claims
    nothing. The user layer is read without the ``DEFAULT_CONFIG`` merge (whose
    ``None`` placeholder would make the key look present in every home), and a
    config read that raises also claims nothing — a corrupt config on a shared
    board must never widen this home's claim scope silently (#113620).
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.dispatch_profiles (%s: %s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping) or "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable resolution of ``kanban.dispatch_profiles`` for this home.

    Surfaced by ``hermes kanban diagnostics`` so an operator on a shared board
    can see what a home believes it may claim (#113620): ``any`` (key absent),
    the sorted allowed names, or ``none (fail-closed: ...)``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True

# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    return _kb._scan_running_tasks({current_path} if current_path else set())[0]


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


def _release_after_merge_edges(conn: sqlite3.Connection) -> None:
    """Drop the gate of every ``after-merge`` edge whose parent PR merged, so
    this tick's recompute_ready promotes the child. Its GitHub reads run here,
    before the tick lock, like the PR-gate prefetch. Never breaks a tick."""
    try:
        from hermes_cli import kanban_after_merge

        released = kanban_after_merge.release_merged_edges(conn)
        if released:
            _kb._log.info("kanban dispatch: after-merge edges released: %s",
                          ", ".join(f"{p}->{c}" for p, c in released))
    except Exception as exc:
        if type(exc).__name__ == "SandboxEscape":
            raise  # a stubbed oracle on a live board is never absorbed
        _kb._log.warning("kanban dispatch: after-merge release failed (%s: %s); "
                         "continuing this tick", type(exc).__name__, exc)


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Union[int, Mapping[str, Any], None] = None,
    spawn_paused: Optional[str] = None,
    spawn_limit: Optional[int] = None,
    reconcile_orphans: bool = True,
    budget_cache: Optional[dict] = None,
    spillover_fn=None,
    spillover=None,
    max_new: Optional[int] = None,
    entry: str = "gateway",
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Thin wrapper around :func:`_dispatch_once_locked`. It acquires a
    non-blocking, board-scoped dispatch lock (issue #35240) so that two
    dispatchers pointed at the same ``kanban.db`` — e.g. the service-
    managed gateway and a shell-spawned orphan that escaped the service
    cgroup — can never run a reclaim/spawn/write tick concurrently and
    race on WAL frames. The losing dispatcher returns an empty
    ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes;
    the holder is already making progress on the same board.

    The lock is keyed off the board's resolved DB path, so unrelated
    boards tick in parallel. See :func:`_dispatch_tick_lock` for the
    cross-process / cross-platform mechanics.
    """
    pr_gate_prefetch = _kb._prefetch_pr_gates_for_tick(conn, dry_run=dry_run)
    if not dry_run:
        _release_after_merge_edges(conn)
    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception:
        # Path resolution should never fail, but if it somehow does we
        # must not lose the tick — fall through to an unguarded dispatch
        # rather than dropping work.
        result = _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            spawn_paused=spawn_paused,
            spawn_limit=spawn_limit,
            reconcile_orphans=reconcile_orphans,
            pr_gate_prefetch=pr_gate_prefetch,
            budget_cache=budget_cache,
            spillover_fn=spillover_fn,
            spillover=spillover,
            max_new=max_new,
            entry=entry,
        )
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            result = DispatchResult(
                skipped_locked=True, lock_holder=_kb._read_dispatch_lock_holder(db_path),
            )
            _kb._log.warning("%s (board=%s)", _kb.format_dispatch_lock_skip(result.lock_holder), db_path)
        else:
            result = _dispatch_once_locked(
                conn,
                spawn_fn=spawn_fn,
                ttl_seconds=ttl_seconds,
                dry_run=dry_run,
                max_spawn=max_spawn,
                max_in_progress=max_in_progress,
                failure_limit=failure_limit,
                stale_timeout_seconds=stale_timeout_seconds,
                board=board,
                default_assignee=default_assignee,
                max_in_progress_per_profile=max_in_progress_per_profile,
                spawn_paused=spawn_paused,
                spawn_limit=spawn_limit,
                reconcile_orphans=reconcile_orphans,
                pr_gate_prefetch=pr_gate_prefetch,
                budget_cache=budget_cache,
                spillover_fn=spillover_fn,
                spillover=spillover,
                max_new=max_new,
                entry=entry,
            )
            # Still under the dispatch lock: run the periodic PASSIVE WAL
            # checkpoint (see _maybe_checkpoint_wal; the -wal file size is
            # bounded by journal_size_limit on the writer's natural reset).
            _kbc._maybe_checkpoint_wal(conn, db_path)
    # Outside the dispatch lock: stop resources (VM/container/unit) whose card finished
    # (t_2df204e5). Only the tick that held the lock sweeps; release_finished claims each resource in a
    # write txn before stopping it, so an overlapping sweep never stops the same one twice.
    if held and not dry_run:
        _release_finished_resources(conn, board)
    # The dispatch lock has been released here. Fire the tick observer
    # strictly OUTSIDE the single-writer critical section (#56066 sweeper
    # finding / #64231 disposition): a slow subscriber must never extend
    # the lock hold and stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def _release_finished_resources(conn: sqlite3.Connection, board: Optional[str]) -> None:
    """Best-effort: a failing sweep is logged and retried next tick, never breaks dispatch."""
    try:
        from hermes_cli import kanban_resources

        kanban_resources.release_finished(conn, board=board)
    except Exception as exc:
        _kb._log.warning("kanban resource release failed (%s: %s)", type(exc).__name__, exc)


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _pause_held_at_claim(conn, result, exc) -> None:
    """A run opener refused a paused host's card (fleet pause layer 2, t_133d6efd). In a correct build layer 1 held
    it first, so reaching this means a route entry bypassed ``gate_rows``: record the wait with ``stage=claim``, the
    refusal event in its own txn (the opener's rolled back), and say so loudly."""
    from hermes_cli import kanban_pause_hold as _kph
    _kb._log.warning("kanban dispatch: pause_hold reached the claim for %s: a route entry bypassed gate_rows",
                     exc.task_id)
    result.placement_waits[exc.task_id] = exc.reason
    _kph.record_refusal(conn, exc)


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        # Per-task diagnostic so ``show``/``tail`` name the missing profile instead of leaving
        # the card in ``ready`` with zero board evidence (#122422). Unlike a respawn guard the
        # condition never expires on its own, so write it once: a repeat only when something
        # else happened on the card since (reassign, comment) — not one row per tick forever,
        # and not one row per foreign home per tick on a shared board (#101015).
        if not dry_run:
            with _kb.write_txn(conn):
                last = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
                if (last is None or last["kind"] != "skipped_nonspawnable"
                        or last["payload"] != _kb._json_or_null({"assignee": assignee})):
                    _kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})
        return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    guard_reason = check_respawn_guard(conn, task_id, lane=lane)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "respawn_guarded", {"reason": guard_reason})
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    from hermes_cli import kanban_pause_hold as _kph
    try:
        claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    except _kph.PauseHeld as exc:
        _pause_held_at_claim(conn, result, exc)
        return False
    if claimed is None:
        return False
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        from hermes_cli.kanban_continuation import record_placement
        if not record_placement(conn, claimed.id, claimed.current_run_id, None):
            return False
        pid = _call_spawn_fn(spawn_fn if spawn_fn is not None else _default_spawn, claimed, str(workspace), board)
        if pid:
            _set_worker_pid(conn, claimed.id, int(pid))
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        # The host refused the spawn (no restart-safe scope, no workspace
        # mount): nothing about the card ran, so it must not spend the card's
        # retry budget (#114720, t_9fb4d477).
        host_fault = _host_fault(exc)
        infrastructure = host_fault is not None
        if infrastructure:
            _kb._log.warning("kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s", claimed.id, exc)
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            infrastructure=infrastructure, run_metadata_extra=host_fault or None,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reaped_terminal_workers = reap_terminal_workers(conn)
    result.reclaimed = _kb.release_stale_claims(conn, failure_limit=failure_limit)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _reroute_placement_blocks(conn, result) -> None:
    """Capability blocks on misplaced runs go back to the queue pinned to a host
    that has the capability (t_a32116ec). Never breaks a tick."""
    try:
        from hermes_cli import kanban_placement_block as _kpb
        from hermes_cli import kanban_worker_pool as _kwp
        pool = _kwp.read_pool(_kb.kanban_home() / "fleet")
        result.placement_rerouted, result.placement_unroutable = _kpb.reroute_placement_blocks(conn, pool)
    except Exception:
        _kb._log.warning("kanban dispatch: placement-block re-route failed", exc_info=True)


def _native_command_profile(assignee: str, cache: dict) -> bool:
    """The assignee's profile sets ``foreign_lane.worker_command``: its lane
    runs as a LOCAL Popen, so it can never be placed (cached per tick)."""
    if assignee not in cache:
        from hermes_cli.kanban_native_worker import worker_command
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
        try:
            cache[assignee] = worker_command(
                resolve_profile_env(normalize_profile_name(assignee))) is not None
        except FileNotFoundError:
            cache[assignee] = False   # no such profile: the spawn itself fails
        except Exception:
            cache[assignee] = True    # cannot tell: never place it
    return cache[assignee]


_LINK_CHUNK = 250


def _linked_ids(conn, ids, mode: Optional[str] = None) -> dict:
    """``{id: (kind, other_id)}`` for ids among ``ids`` whose links hold them
    local: ONE query per tick (bounded by the ready set), not one per row.
    ``other_id`` is the far end of its lowest (parent_id, child_id) such
    link, whatever the order or chunking of ``ids``.

    ``mode`` is ``kanban.placement.lineage_pins`` (None = read it): ``kinds``
    skips provenance-only kinds (``derived-from``); ``legacy`` counts every
    link, as before t_ae87edff. NULL reads as ``blocks``; an unknown kind
    holds the card (fail closed)."""
    from hermes_cli import kanban_placement_reasons as _pr
    if mode is None:
        mode = _lineage_pins_mode()
    if mode == _pr.LINEAGE_PINS_NONE:
        return {}  # lineage never pins (spec Phase 2b-i); the binds decide
    ids = list(ids)
    out: dict = {}
    best: dict = {}
    # Each id is bound twice (parent + child): 250 ids = 500 variables, under
    # SQLite's historical 999 limit (SQLITE_MAX_VARIABLE_NUMBER < 3.32).
    for i in range(0, len(ids), _LINK_CHUNK):
        chunk = ids[i:i + _LINK_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT parent_id, child_id, kind FROM task_links "
            f"WHERE parent_id IN ({marks}) OR child_id IN ({marks}) "
            f"ORDER BY parent_id, child_id", chunk + chunk,
        ):
            try:
                kind = _kb.normalize_link_kind(r[2])
            except ValueError:
                kind = str(r[2])
            if mode != _pr.LINEAGE_PINS_LEGACY and kind in _pr.PROVENANCE_LINK_KINDS:
                continue
            # Keep the lowest (parent_id, child_id) edge across ALL chunks, not
            # the first one seen: chunk membership follows ready-row order, and
            # a reason that flips with it resets placement_wait ``since``
            # (Prism 9457394f1922).
            edge = (r[0], r[1])
            for me, other in ((r[0], r[1]), (r[1], r[0])):
                if me not in best or edge < best[me]:
                    best[me] = edge
                    out[me] = (kind, other)
    return out


def _workspace_binds(conn, rows, *, dry_run: bool = False) -> dict:
    """``{id: (rule, other_id, host)}`` for ready scratch rows whose workspace
    is bound to another card's (spec §2.4): ``workspace_of`` first (explicit
    beats derived), else the canonical-path ``workspace_shared`` relation.
    Never reads ``task_links``. One path index per tick. An unresolvable path
    binds local (fail closed).

    Host-aware (Apollo r1 on #2032): ``other_id`` and ``host`` come from
    :func:`kanban_db_workspace.resolve_bind`, which follows the reader chain
    to the directory's owner and its placement record. A directory that lives
    on a pool host is never judged by a Studio-local fact: its bind holds
    (spec §2.4: remote path existence is not probed; fail closed: pin). Only
    a LOCAL directory whose owner is terminal and whose path is gone drops
    the bind (``placement_bind_dropped``, once); the row then routes on."""
    from hermes_cli import kanban_placement_reasons as _pr
    from hermes_cli import kanban_db_workspace as _kbw
    out: dict = {}
    scratch = [r for r in rows if (r["workspace_kind"] or "scratch") == "scratch"]
    if not scratch:
        return out
    index = None
    for r in scratch:
        tid = r["id"]
        of = _kb._row_get(r, "workspace_of")
        if of:
            owner, host = _kbw.resolve_bind(conn, of)
            prow = conn.execute("SELECT status, workspace_path FROM tasks WHERE id = ?",
                                (owner,)).fetchone()
            gone = prow is None or (host is None and prow[0] in _kbw._TERMINAL_STATUSES and not (
                prow[1] and os.path.isdir(_kbw.canon(prow[1]))))
            if not gone:
                out[tid] = (_pr.workspace_bind_rule(_pr.WORKSPACE_OF, of), owner, host)
                continue
            if not dry_run and _kb._latest_event(conn, tid, _pr.PLACEMENT_BIND_DROPPED_EVENT) is None:
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, _pr.PLACEMENT_BIND_DROPPED_EVENT,
                                      {"of": of, "why": "path_gone"})
        if not r["workspace_path"]:
            continue
        try:
            if index is None:
                index = _kbw.live_path_index(conn)
            shared = _kbw.workspace_shared(conn, tid, r["workspace_path"], role="reader",
                                           index=index)
        except (OSError, RuntimeError, ValueError):
            out[tid] = (_pr.workspace_bind_rule(_pr.WORKSPACE_SHARED, "unresolved"), "", None)
            continue
        if shared:
            # The sharer whose chain reaches a placed host wins over an unrun
            # reader that merely sorts first (Prism aabb9c4f4a2f).
            resolved = [(sid, *_kbw.resolve_bind(conn, sid)) for sid in shared]
            sid, owner, host = next((x for x in resolved if x[2]), resolved[0])
            out[tid] = (_pr.workspace_bind_rule(_pr.WORKSPACE_SHARED, sid), owner, host)
    return out


def _lineage_pins_mode() -> str:
    """``kanban.placement.lineage_pins``, read per tick (default ``kinds``)."""
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        from hermes_cli.config import load_config_readonly
        kcfg = (load_config_readonly() or {}).get("kanban")
    except Exception:
        kcfg = None
    return _pr.lineage_pins(kcfg)


def _wait_key(payload: dict) -> tuple:
    """``(code, route, host, keyed detail)`` of a placement_wait payload: the
    dedup key (I-S6 + Momus R-B). ``detail`` is compared only where it names
    the holding edge (``_pr.keyed_detail``); a payload from before Phase 2a
    has no ``code`` and is split from its ``reason``."""
    from hermes_cli import kanban_placement_reasons as _pr
    code, detail = payload.get("code"), payload.get("detail")
    if code is None:
        code, detail = _pr.split_reason(payload.get("reason"))
    return (code, payload.get("route"), payload.get("host"), _pr.keyed_detail(code, detail))


def _wait_history(conn, tids: list, since_ts: int) -> "tuple[dict, dict]":
    """One batched read per tick for the held cards: ``({tid: [(id,
    created_at, payload)] newest first}, {tid: last episode-end event id})``.
    The history holds each card's latest placement_wait plus every one inside
    the refresh window (the flap check needs those, nothing older). A
    ``{cleared: true}`` placement_wait is an episode end, never a wait
    (Prism 5ff168657c9a): a hold after it is a new episode, not a flap.
    One SELECT per 400 cards over ``idx_events_task``; payloads are decoded
    only for the rows kept."""
    from hermes_cli import kanban_placement_reasons as _pr
    hist: dict = {}
    ends: dict = {}
    kinds = [_pr.PLACEMENT_WAIT_EVENT, *sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)]
    for i in range(0, len(tids), 400):
        chunk = tids[i:i + 400]
        for tid, eid, created, kind, payload in conn.execute(
            f"SELECT task_id, id, created_at, kind, "
            f"CASE WHEN kind = ? THEN payload END FROM task_events "
            f"WHERE task_id IN ({','.join('?' * len(chunk))}) "
            f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC",
            [_pr.PLACEMENT_WAIT_EVENT, *chunk, *kinds],
        ):
            if kind != _pr.PLACEMENT_WAIT_EVENT:
                ends.setdefault(tid, eid)
                continue
            if tid in ends or (tid in hist and int(created or 0) < since_ts):
                continue  # older than the episode end, or than the window
            try:
                pl = json.loads(payload or "{}")
            except (TypeError, ValueError):
                pl = {}
            pl = pl if isinstance(pl, dict) else {}
            if pl.get("cleared"):
                ends.setdefault(tid, eid)
                continue
            hist.setdefault(tid, []).append((eid, int(created or 0), pl))
    return hist, ends


def _record_placement_waits(conn, result, row_route: dict, dry_run: bool,
                            reached=()) -> None:
    """One ``placement_wait`` task_event per held ready card
    ``{code, detail, reason, route, host, since}`` (spec §4.2).

    Written only when the card's dedup key ``(code, route, host)`` differs
    from its latest one in the current episode, or that one is older than
    PLACEMENT_WAIT_REFRESH_SECONDS. ``detail`` (the volatile part) is never
    compared (I-S6); ``host`` is the pin host, or for a plan-wide refusal the
    host(s) the reason names, so a refusal moving between hosts is a new row
    (Momus R-B). A key already seen in this episode inside the refresh window
    (A->B->A) writes ONE ``flapping`` row and then nothing until the window
    passes. A lifecycle event after the latest wait
    (PLACEMENT_WAIT_EPISODE_END_KINDS) starts a new episode: written now,
    ``since`` = now. ``reason`` = code + detail, kept one release for readers
    of the #1880 shape.

    ``reached``: rows that had a slot this tick. One that spawned nothing
    and holds no wait was stopped by a later gate (respawn guard, provider
    admission, ...): its open wait gets ``{cleared: true}``, because the
    reason it named has ended (Prism 8db25fd42347).

    Cost (Momus R-G): one batched SELECT per tick (per 400 held cards),
    outside any transaction; the write transaction opens only when there is
    something to write. Never on a dry run. Best-effort.

    The other writer of this event kind is
    ``kanban_host_affinity.record_affinity_events``: it owns Studio-affinity
    cards (``{host, reason, matched_by}`` / ``{cleared: true}``) and this
    writer skips them, so each card has one writer."""
    from hermes_cli import kanban_placement_reasons as _pr
    bound = result.placement_affinity or {}
    waits = {t: r for t, r in (result.placement_waits or {}).items() if t not in bound}
    spawned = {s[0] for s in result.spawned}
    gated = [t for t in dict.fromkeys(reached)
             if t not in waits and t not in bound and t not in spawned]
    if dry_run or not (waits or gated):
        return
    now = int(time.time())
    window = _pr.PLACEMENT_WAIT_REFRESH_SECONDS
    try:
        hist, ends = _wait_history(conn, [*waits, *gated], now - window)
        writes: list = [(t, {"cleared": True}) for t in gated
                        if any(h[0] > ends.get(t, 0) for h in hist.get(t, ()))]
        for tid, reason in waits.items():
            code, detail = _pr.split_reason(reason)
            route, arg = row_route.get(tid, ("local", None))
            if route in ("pin", "origin"):
                host = arg
            elif code == "origin_host_absent":
                host = conn.execute("SELECT origin_host FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            else:
                host = _pr.named_hosts(code, detail)
            key = (code, route, host, _pr.keyed_detail(code, detail))
            episode = [h for h in hist.get(tid, ()) if h[0] > ends.get(tid, 0)]
            since = now
            if episode:
                last_id, last_at, last = episode[0]
                fresh = now - last_at < window
                if _wait_key(last) == key:
                    if fresh:
                        continue
                    since = int(last.get("since") or last_at or now)
                elif fresh:
                    recent = [_wait_key(p) for _i, at, p in episode if now - at < window]
                    if key in recent[1:]:
                        # A->B->A inside the window: one ``flapping`` row,
                        # then quiet until the window passes.
                        if last.get("code") == _pr.FLAPPING:
                            continue
                        seen = list(dict.fromkeys(k[0] for k in recent if k[0] != _pr.FLAPPING))
                        code, detail, host = _pr.FLAPPING, "|".join(seen), None
                        reason = f"{code} ({detail})"
            writes.append((tid, {
                "code": code, "detail": detail, "reason": reason, "route": route,
                "host": host, "since": since,
            }))
        if not writes:
            return
        with _kb.write_txn(conn):
            for tid, payload in writes:
                _kb._append_event(conn, tid, _pr.PLACEMENT_WAIT_EVENT, payload)
    except Exception:
        _kb._log.exception("kanban dispatch: could not record placement_wait events")


def _record_total_hold(conn, result, gate: str, dry_run: bool) -> None:
    """A total-budget gate (spec §3.1 step 1) spawns nothing this tick: every
    ready row still gets a ``placement_wait {code: total:<gate>}`` (deduped
    like any other), and the dry run sees the same rows in ``routes``."""
    from hermes_cli import kanban_placement_reasons as _pr
    reason = _pr.total(gate)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
            "AND COALESCE(no_worker, 0) = 0 ORDER BY priority DESC, created_at ASC"
        ).fetchall()
    except Exception:
        _kb._log.exception("kanban dispatch: could not enumerate ready rows for %s", reason)
        return
    for r in rows:
        result.placement_waits[r[0]] = reason
        result.routes.append({"task_id": r[0], "route": None, "rule": None, "host": None,
                              "outcome": "held", "code": reason, "detail": None})
    _record_placement_waits(conn, result, {r[0]: (None, None) for r in rows}, dry_run)


def _route_first_enabled() -> bool:
    """``kanban.placement.route_first`` from the ROOT config, read per tick."""
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        from hermes_cli.config import load_config_readonly
        kcfg = (load_config_readonly() or {}).get("kanban")
    except Exception:
        kcfg = None
    return _pr.route_first(kcfg)


# ``placement.route`` values (spec §4.1).
_ROUTE_LOCAL = "local"
_ROUTE_REMOTE_PIN = "remote-pin"
_ROUTE_REMOTE_POLICY = "remote-policy"
_ROUTE_HOLD = "hold"


def _spec_route(route: Optional[str], placed: bool, spawned_local: bool) -> Optional[str]:
    """The spec §4.1 route name for an internal ``_route_row`` route."""
    from hermes_cli import kanban_placement_reasons as _pr
    if route in ("pin", "origin"):
        return _ROUTE_REMOTE_PIN
    if route == "portable":
        return _ROUTE_LOCAL if spawned_local else _ROUTE_REMOTE_POLICY
    if route == "wait":
        return _ROUTE_HOLD
    if route is None and not placed and not spawned_local:
        return None
    return _ROUTE_LOCAL


def _placement_rule(route: str, arg: Optional[str], rule: Optional[str], placed: bool,
                    planned: bool) -> str:
    """``placement.rule`` (spec §4.1): the ``_route_row`` rule; ``default``
    for a portable card the Studio took locally; ``unplanned`` when no pool
    plan ran this tick."""
    from hermes_cli import kanban_placement_reasons as _pr
    if not planned:
        return _pr.RULE_UNPLANNED
    if route == "portable" and not placed:
        return _pr.RULE_DEFAULT
    if route == "origin":
        return _pr.RULE_ORIGIN
    return rule or (_pr.RULE_PIN if route == "pin" else _pr.RULE_DEFAULT)


def _placement_payload(*, run_id, route: str, arg: Optional[str], rule: Optional[str],
                       placed_host, plan, entry: str, gate: dict,
                       take_refusal: Optional[str] = None) -> dict:
    """The ``placement`` event payload, every key present (spec §4.1).

    Control-Plane Protection v0.3 §3.1 adds the fields the I-1 / AC-2 queries
    read: ``route_class`` (the dispatcher's own route: ``portable`` | ``pin`` |
    ``local`` | ``wait``), ``placement_default`` (``remote`` | ``local`` this
    tick), ``reason`` (``pool`` placed, ``last-resort`` = local after take()
    refused, ``local-first`` = local without asking the pool), ``take_refusal``
    (the take() refusal behind a last-resort spawn, else None) and
    ``pool_free_at_spawn`` (display only, never the I-1 predicate)."""
    from hermes_cli import kanban_placement_reasons as _pr
    from hermes_cli import kanban_worker_pool as _kwp_pd
    placed = placed_host is not None
    considered = []
    if plan is not None:
        for name in plan.hosts:
            d = plan.detail.get(name) or {}
            considered.append({
                "host": name, "band": d.get("band"), "free_slots": plan.slots.get(name, 0),
                "pressure": d.get("pressure"), "load1": d.get("load1"),
                "ncpu": d.get("ncpu"), "reachable": d.get("reachable"),
            })
    remote = bool(getattr(plan, "default_remote", False))
    return {
        "run_id": run_id,
        "route": _spec_route(route, placed, not placed),
        "rule": _placement_rule(route, arg, rule, placed, plan is not None),
        "host": placed_host.name if placed else _kb._claimer_id().split(":", 1)[0],
        "considered": considered,
        "gate": gate,
        "plan_at": getattr(plan, "planned_at", None) if plan is not None else None,
        "entry": entry,
        "route_class": route,
        "placement_default": _kwp_pd.PLACEMENT_DEFAULT_REMOTE if remote else _kwp_pd.PLACEMENT_DEFAULT_LOCAL,
        "reason": "pool" if placed else (_pr.LAST_RESORT if take_refusal else "local-first"),
        "take_refusal": take_refusal,
        "pool_free_at_spawn": plan.budget if plan is not None else 0,
    }


def _tick_routes(result, ready_rows, row_route: dict, row_rules: dict, planned: bool,
                 dry_run: bool) -> None:
    """``result.routes``: one dict per ready row in loop order (spec §4.3)."""
    from hermes_cli import kanban_placement_reasons as _pr
    placed = dict(result.placed or [])
    spawned = {t for t, _w, _s in result.spawned}
    local_host = _kb._claimer_id().split(":", 1)[0]
    for row in ready_rows:
        tid = row["id"]
        route, arg = row_route.get(tid, ("local", None))
        host = placed.get(tid) or (arg if route in ("pin", "origin") else None)
        # ``default`` names a portable card the Studio TOOK; a held one keeps
        # the rule it was routed by.
        rule = _placement_rule(route, arg, row_rules.get(tid), tid in placed, planned) \
            if tid in spawned else (row_rules.get(tid) or arg)
        entry = {"task_id": tid,
                 "route": _spec_route(route, tid in placed, tid in spawned and tid not in placed),
                 "rule": rule, "host": host, "outcome": None, "code": None, "detail": None}
        if tid in spawned:
            entry["outcome"] = "would_place" if dry_run else "spawned"
            entry["host"] = placed.get(tid) or local_host
            if tid in (result.take_refusals or {}):
                entry["take_refusal"] = result.take_refusals[tid]
        elif tid in (result.placement_waits or {}):
            entry["outcome"] = "held"
            entry["code"], entry["detail"] = _pr.split_reason(result.placement_waits[tid])
        else:
            entry["outcome"] = "skipped"
        if entry["route"] is None:
            entry["route"] = _ROUTE_LOCAL
        result.routes.append(entry)


def _route_row(conn, row, plan, default_assignee: Optional[str] = None,
               native_cache: Optional[dict] = None,
               linked_ids: Optional[dict] = None,
               lineage_mode: Optional[str] = None,
               affinity_out: Optional[dict] = None,
               unknown_out: Optional[dict] = None,
               caps=None, caps_out: Optional[dict] = None,
               rules_out: Optional[dict] = None,
               access_out: Optional[dict] = None,
               binds: Optional[dict] = None) -> "tuple[str, Optional[str]]":
    """One route per ready row (KWLB PRD 5.2.6/5.3): ``("pin", host)``,
    ``("portable", None)``, ``("local", rule)`` or ``("wait", reason)``.
    ``rules_out[id]`` gets the ``portable()`` rule (``pin``/``policy`` when
    portable) for the ``placement`` record (spec §4.1). ``access_out[id]``
    gets the §5 contradiction line of an access-bound card pinned to a pool
    host (parked ``needs_input`` after the loop).

    ``portable()`` is THE classifier; a remote pin on a card that fails
    ``hard_ok`` waits ``not_portable:<rule>`` and is never spawned locally.

    ``caps`` is this tick's :class:`kanban_host_capabilities.CapabilityTick`
    (None = layer off or map absent: routes as before it existed). A card
    pinned by an enforced capability lands in ``affinity_out``; a warn-mode
    observation, a remote pin that lacks a hard requirement (F3) and an
    unknown ``requires:`` word land in ``caps_out``.
    """
    from hermes_cli import kanban_worker_pool as _kwp
    from hermes_cli import kanban_continuation as _kcont
    _origin = _kcont.origin_route(
        conn, row, plan.config, config=_root_kanban_cfg(),
        states=getattr(plan, "continuation_states", {}), caps=caps,
        dry_run=getattr(plan, "continuation_dry_run", False),
    )
    if _origin is not None:
        return _origin

    try:
        skills = json.loads(row["skills"] or "[]") or []
    except (TypeError, ValueError, IndexError, KeyError):
        skills = []
    from hermes_cli import kanban_placement_reasons as _pr
    if lineage_mode is None:
        lineage_mode = _lineage_pins_mode()
    if linked_ids is None:
        linked_ids = _linked_ids(conn, [row["id"]], lineage_mode)
    link = linked_ids.get(row["id"])
    linked = link is not None
    if binds is None:
        binds = _workspace_binds(conn, [row])
    bind = binds.get(row["id"])
    # An unassigned card is routed as the assignee the loop will give it.
    assignee = row["assignee"] or default_assignee
    allowlisted = bool(assignee) and assignee in tuple(plan.config.profiles or ())
    native = allowlisted and \
        _native_command_profile(assignee, {} if native_cache is None else native_cache)
    # The filesystem scan is the expensive check: only a row every cheaper
    # rule would admit pays for it (non-scratch, linked or non-allowlisted
    # rows can never place; a non-allowlisted one reports ``profile``). A
    # bound row never scans: its directory is the bind target's.
    scan = ((row["workspace_kind"] or "scratch") == "scratch" and not linked and allowlisted
            and bind is None)
    skills = tuple(s for s in skills if isinstance(s, str))
    manifest = _manifest_affinity(skills, assignee) if allowlisted and skills else {}
    targets = _manifest_targets(skills, assignee) if allowlisted and skills else {}
    pin, ignored = _kwp.resolve_pin(row["body"], plan.config.hosts or (),
                                    getattr(plan.config, "pin_aliases", None))
    verdict = None
    if caps is not None:
        from hermes_cli import kanban_host_capabilities as _khc
        verdict = _khc.evaluate(
            row["body"], skills, caps,
            pin_host=pin if pin not in (None, _kwp.PIN_ANY, _kwp.PIN_STUDIO) else None,
            any_pin=pin == _kwp.PIN_ANY)
    ok, rule, route_class = _kwp.portable(
        workspace_kind=row["workspace_kind"], has_links=linked, workspace_bind=bind,
        workspace_has_content=(_kb._kwh.local_workspace_has_content(row["workspace_path"])
                               if scan else False),
        assignee=assignee, body=row["body"],
        skills=skills, pool=plan.config,
        native_command=native, manifest=manifest, targets=targets,
        requires=verdict.for_portable() if verdict is not None else None,
        body_signals=caps is None or caps.legacy_body,
    )
    if ignored is not None:
        _kwp.note_ignored_pin(row["id"], ignored)
    if (verdict is not None and not ok and pin in (None, _kwp.PIN_ANY)
            and _identity_only_needed(verdict, caps, any_pin=pin == _kwp.PIN_ANY)):
        # An identity-only capability (docker, spec v0.3 §3.4) may run on the
        # Studio only under an explicit `host: studio` pin, whatever ELSE kept
        # the card off the pool (native command, studio-bound skill, linked):
        # those rules choose "local", which this capability forbids unpinned.
        # Prism fbd44b39be03 / 3b4b76976536 on #1902.
        req = verdict.pin or next(r for r in verdict.enforced if r.cap in caps.cmap.identity_only)
        if affinity_out is not None:
            from hermes_cli import kanban_host_capabilities as _khc
            affinity_out[row["id"]] = _khc.affinity_payload(req, verdict, _khc.ENFORCE, host="none")
        if caps_out is not None:
            _note_capability_verdict(row["id"], verdict, caps, rule, pin, caps_out)
        return "wait", verdict.wait or f"requires_unsatisfied:{req.cap}+{rule}"
    if verdict is not None and caps_out is not None:
        _note_capability_verdict(row["id"], verdict, caps, rule, pin, caps_out)
        if not ok and verdict.pin is not None:
            # Every door that keeps the card home, named (spec v0.3 AC-11b:
            # `ssh ci sudo` reads `host_reach` from the matrix AND
            # `requires:fleet-ssh` from the map).
            caps_out.setdefault("reasons", {})[row["id"]] = list(dict.fromkeys(
                [rule, *(r.rule for r in verdict.enforced)]))
        if caps.error and (verdict.observed is not None or skills):
            # A card the bad map/index demoted (F11): it carries the event.
            caps_out.setdefault("touched", []).append(row["id"])
    if ok:
        if verdict is not None and verdict.only and route_class != "pin":
            # Capability-aware placement (t_90887924): some pool host
            # advertises what the card needs; it goes to those hosts only,
            # never to a local slot (a docker card does not drift onto the
            # Studio as portable overflow, spec v0.3 §3.4).
            if caps_out is not None:
                caps_out.setdefault("only", {})[row["id"]] = verdict.only
            if affinity_out is not None and verdict.pin is not None:
                from hermes_cli import kanban_host_capabilities as _khc
                affinity_out[row["id"]] = _khc.affinity_payload(
                    verdict.pin, verdict, _khc.ENFORCE, host="|".join(verdict.only))
        if bind is not None and bind[2]:
            # Co-located with the card whose directory it shares (spec §2.4.2).
            if rules_out is not None:
                rules_out[row["id"]] = f"{_pr.BIND_HOST}:{bind[2]}({bind[0]})"
            return "pin", bind[2]
        if rules_out is not None:
            rules_out[row["id"]] = _pr.RULE_PIN if route_class == "pin" else _pr.RULE_POLICY
        return ("pin", pin) if route_class == "pin" else ("portable", None)
    if rule == _pr.PIN_UNKNOWN_HOST:
        # A host: line naming no host: local only, named, never spilled (t_cb2b888d).
        if unknown_out is not None:
            unknown_out[row["id"]] = ignored
        if rules_out is not None:
            rules_out[row["id"]] = rule
        return "local", _pr.pin_unknown_host(ignored, _kwp.pin_suggestions(
            ignored, plan.config.hosts or (), getattr(plan.config, "pin_aliases", None)))
    if rule == _pr.LINKED and link is not None and lineage_mode != _pr.LINEAGE_PINS_LEGACY:
        rule = _pr.linked(*link)  # name the edge that holds it (t_ae87edff)
    if rules_out is not None:
        rules_out[row["id"]] = rule
    if bind is not None and bind[2]:
        # Bound to a directory on a pool host, and that host cannot take it
        # (profile, native command, access, reach, capability): it waits,
        # named. A local run would read an empty path (Prism f399797453ae).
        return "wait", _pr.not_portable(rule)
    if rule.startswith(_pr.ACCESS_BOUND + ":"):
        # Access-bound (spec §2.6): local, named, never spilled. A pool pin
        # contradicts it: park needs_input (§5), never wait on that host.
        if pin is not None and pin not in (_kwp.PIN_ANY, _kwp.PIN_STUDIO):
            if access_out is not None:
                access_out[row["id"]] = _pr.access_pin_contradiction(
                    rule.split(":", 1)[1], pin)
            return "wait", rule
        return "local", rule
    if rule == _pr.HOST_REACH:
        # A host-bound card (ssh skill / fleet alias in the body) with no
        # worker host proven to reach its target: local, never spilled.
        _kb._log.info("kanban dispatch: %s classified local (host_reach): host-bound card, "
                      "no pool host with a proven ssh route (reach matrix %s)",
                      row["id"], getattr(plan.config, "reach_status", "absent"))
    if pin is not None and pin not in (_kwp.PIN_ANY, _kwp.PIN_STUDIO):
        return "wait", _pr.not_portable(rule)
    if (verdict is not None and verdict.wait and verdict.pin is not None
            and rule == verdict.pin.rule and pin != _kwp.PIN_STUDIO):
        # An identity-only capability (the Studio serves it to `host: studio`
        # pins only) that no pool host advertises: WAIT, named, never spawn
        # locally unpinned (spec v0.3 §3.4, AC-5; t_90887924).
        if affinity_out is not None:
            from hermes_cli import kanban_host_capabilities as _khc
            affinity_out[row["id"]] = _khc.affinity_payload(verdict.pin, verdict, _khc.ENFORCE,
                                                            host="none")
        return "wait", verdict.wait
    if verdict is not None and verdict.pin is not None and rule == verdict.pin.rule:
        # An enforced capability no pool host has (t_8c474c2e): runs here
        # or waits, named, never spills.
        if affinity_out is not None:
            from hermes_cli import kanban_host_capabilities as _khc
            affinity_out[row["id"]] = _khc.affinity_payload(verdict.pin, verdict, _khc.ENFORCE)
        return "local", f"studio_affinity:{rule}"
    if rule in _STUDIO_AFFINITY_RULES:
        # Work bound to the Studio's state (t_3e6c9b2b): runs here or waits,
        # named, never spills. ``portable()`` only reaches these rules when
        # no explicit pin decided the route.
        hit = _kwp.studio_affinity(row["body"], skills, plan.config, manifest=manifest)
        if affinity_out is not None:
            affinity_out[row["id"]] = {"host": _kwp.STUDIO_HOST, "rule": rule,
                                       "matched_by": hit[1] if hit else rule}
        if rule == _kwp.STUDIO_NEEDS_RULE:
            # Name what it needs (t_aea826a9): ``studio_affinity:studio_needs(vault,skills_tree)``.
            return "local", f"studio_affinity:{rule}({','.join(sorted(_kwp.card_needs(row['body'])))})"
        return "local", f"studio_affinity:{rule}"
    # host:any that fails hard_ok may still run locally; with no local slot
    # it waits on the failed hard rule (RC-1), an unpinned card on local_full.
    return "local", (_pr.not_portable(rule) if pin == _kwp.PIN_ANY else None)


_STUDIO_AFFINITY_RULES = frozenset({"studio_bound_skill", "studio_manifest", "studio_body",
                                    "studio_needs"})


def _drop_foreign(conn, pending: dict, default_assignee: Optional[str] = None) -> None:
    """Pop every task id from ``pending`` whose effective assignee this home
    would not itself spawn (``_profile_exists_fn`` / ``dispatch_profiles``, the
    ready loop's gate). Every post-loop park goes through this first: routing
    sees the whole shared board, so a park without it blocks another home's
    card (Prism 229da8245003)."""
    if not pending:
        return
    _exists = _profile_exists_fn()
    _dflt = _resolve_default_assignee(default_assignee)
    for tid in list(pending):
        row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        eff = (row[0] if row else None) or _dflt
        if not eff or (_exists is not None and not _exists(eff)):
            pending.pop(tid)


def _park_access_contradictions(conn, result, dry_run: bool,
                                default_assignee: Optional[str] = None) -> None:
    """Park every access-bound card pinned to a pool host ``needs_input``
    with the §5 line (spec §2.6): it cannot run there, so it must not wait
    there. Fenced like the pin-lacks park: a card claimed since the snapshot
    is left alone. A dry run only reports.

    Ownership first (Prism 229da8245003): only a card whose effective assignee
    passes this home's ``_profile_exists_fn`` / ``dispatch_profiles`` predicate
    (the ready loop's spawn gate, as in ``_browser_gate``) is parked; a foreign
    card is dropped from ``access_contradictions`` and left to its owner."""
    pending = result.access_contradictions or {}
    if not pending:
        return
    _drop_foreign(conn, pending, default_assignee)
    for tid, line in pending.items():
        if dry_run:
            continue
        try:
            if _kb.block_task(conn, tid, reason=line, kind="needs_input",
                              require_unclaimed_ready=True):
                _kb.add_comment(conn, tid, "dispatcher", line)
                (result.placement_waits or {}).pop(tid, None)
            else:
                _kb._log.info("kanban dispatch: %s no longer ready+unclaimed; access park skipped", tid)
        except Exception:
            _kb._log.warning("kanban dispatch: could not park %s needs_input (%s)", tid, line,
                             exc_info=True)


def _identity_only_needed(verdict, caps, any_pin: bool = False) -> bool:
    """An enforced requirement the Studio serves to `host: studio` pins only.
    Under `host: any` only HARD requirements count (F12)."""
    from hermes_cli import kanban_host_capabilities as _khc
    return caps is not None and any(r.cap in caps.cmap.identity_only for r in verdict.enforced
                                    if not any_pin or r.strength == _khc.HARD)


def _note_capability_verdict(tid, verdict, caps, rule, pin, caps_out: dict) -> None:
    """Record what the capability layer saw on one card this tick:
    ``pin_lacks`` (a ``host:<pool-id>`` pin missing a HARD requirement, F3:
    parked needs_input after the loop), ``unknown`` (a word the map does not
    define, F4) and ``observed`` (a warn-mode requirement: event only)."""
    from hermes_cli import kanban_host_capabilities as _khc
    from hermes_cli import kanban_worker_pool as _kwp
    if (verdict.pin is not None and verdict.pin.strength == _khc.HARD and rule == verdict.pin.rule
            and pin not in (None, _kwp.PIN_ANY, _kwp.PIN_STUDIO)):
        caps_out.setdefault("pin_lacks", {})[tid] = (pin, verdict.pin)
    for req in verdict.enforced:
        # Only the word that actually holds the card: a soft one that
        # ``host: any`` overrode must not claim the card stays local (Prism 027ec54981dd).
        if req.unknown and rule == req.rule:
            caps_out.setdefault("unknown", {})[tid] = req
            break
    if verdict.observed is not None and verdict.pin is None:
        caps_out.setdefault("observed", {})[tid] = _khc.affinity_payload(
            verdict.observed, verdict, _khc.WARN)


def _root_kanban_cfg() -> dict:
    """The ``kanban`` section of the ROOT ``config.yaml`` (F5: never a
    profile's), ``{}`` when unreadable. Same presence-sensitive read as the
    board-home knobs: only what the file writes."""
    try:
        return _kb._board_home_kanban_cfg()
    except Exception:
        _kb._log.debug("kanban dispatch: root kanban config unreadable", exc_info=True)
        return {}


def _capability_tick(pool_config):
    """This tick's capability context, or None (layer off / map absent):
    read once per tick, from the root config and the fleet dir. The
    candidate hosts are every host the pool files name (disabled ones too:
    Phase 1 asks all of them, the conservative direction)."""
    try:
        from hermes_cli import kanban_host_capabilities as _khc
        hosts = tuple(getattr(pool_config, "hosts", ()) or ())
        return _khc.tick(_kb.kanban_home() / "fleet", _root_kanban_cfg(), hosts)
    except Exception:
        _kb._log.warning("kanban dispatch: capability map unreadable, layer off this tick",
                         exc_info=True)
        return None


def _browser_lookup():
    """The identity lookup the gate uses this tick: browserd's read-only ``identity`` verb over the
    local socket (the ledger is ``_browserd``-owned; the dispatcher never opens it). Tests replace it."""
    from hermes_cli import kanban_browser_gate as _kbg
    return _kbg.socket_lookup(_kbg.DEFAULT_SOCKET)


def _pin_host(route) -> str:
    """The host a browser card's worker runs on: the pool host of a ``("pin", host)`` route, else the
    Studio (every other route spawns locally on the dispatcher host), keyed as browser-hosts.json does."""
    if isinstance(route, tuple) and len(route) == 2 and route[0] in ("pin", "origin") and route[1]:
        return route[1]
    return "studio"


def _browser_join_on() -> bool:
    """``kanban.browser_join`` from the ROOT config, read per tick (default off; P1-4b t_2828292d)."""
    return _root_kanban_cfg().get("browser_join") is True


def _relay_joinable(plans, worker_host: str) -> bool:
    """The s7 join this build takes: every plan is a ``browser:ephemeral`` on a browserd host that is NOT
    the worker's own (ci-box for an ace-ai worker). Identity leases and same-host ephemerals stay held."""
    from hermes_cli import kanban_browser_gate as _kbg
    from hermes_cli import kanban_needs as _kn
    return bool(plans) and all(p.need == _kn.EPHEMERAL and p.identity is None and p.host == _kbg.EPHEMERAL_HOST
                               and p.host != worker_host for p in plans)


def _browser_relay_create(host: str, card: str, run: str, source_host: str) -> dict:
    """``scripts/browsers/dispatch_relay.py create-ephemeral`` (hermes-home, t_f48d777e): mint, create and
    revoke happen on ``host``; the reply carries only the worker's lease. Tests replace this seam."""
    script = _kb.kanban_home() / "scripts" / "browsers" / "dispatch_relay.py"
    try:
        out = subprocess.run([sys.executable, str(script), "create-ephemeral", "--host", host, "--card", card,
                              "--run", run, "--source-host", source_host],
                             capture_output=True, text=True, timeout=260, stdin=subprocess.DEVNULL)
        resp = json.loads(((out.stdout or "").strip().splitlines() or ["{}"])[-1])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"ok": False, "refusal": "BROWSER_RELAY_TRANSPORT", "detail": type(exc).__name__}
    return resp if isinstance(resp, dict) else {"ok": False, "refusal": "BROWSER_RELAY_TRANSPORT"}


def _browser_join_lease(conn, claimed, join: dict, placed_host) -> tuple:
    """Take the joined lease for a claimed card: ``(env, None)`` or ``(None, refusal_token)``.

    The worker's host must be the one the plan was cut for (``source_host`` is the lease row's tool host).
    The reply goes into ``lease_env`` (the LEASE_ENV_NAMES allowlist) and nowhere else: no event, no log
    line and no run metadata carries the token."""
    from hermes_cli import kanban_browser_lease as _kbl
    source = placed_host.name if placed_host is not None else "studio"
    if source != join["source_host"]:
        return None, "BROWSER_JOIN_HOST_MOVED"
    (plan,) = join["plans"]
    resp = _browser_relay_create(plan["host"], claimed.id, str(claimed.current_run_id or ""), source)
    if not resp.get("ok"):
        return None, str(resp.get("refusal") or "BROWSER_REFUSED")
    try:
        env = _kbl.lease_env(resp, None)
    except (KeyError, TypeError, ValueError):
        return None, "BROWSER_RELAY_REPLY_MALFORMED"
    with _kbc.write_txn(conn):
        _kb._append_event(conn, claimed.id, "browser_lease_joined",
                          {"host": plan["host"], "name": resp.get("name"), "lease_id": resp.get("lease_id"),
                           "lease_class": resp.get("lease_class"), "source_host": source,
                           "lease_expires_at": resp.get("lease_expires_at")},
                          run_id=claimed.current_run_id)
    return env, None


def _browser_gate(conn, ready_rows, result, row_route: dict, dry_run: bool,
                  default_assignee: Optional[str] = None, *, park: bool = True, join: bool = True) -> None:
    """The Browser Sandboxes resolution gate for this tick (t_42728faf; spec v1.2 s3.3, AC-1.13/1.15).

    The live tick and ``--dry-run`` both come through here and both call
    ``kanban_browser_gate.gate_rows`` (one code path, pass-3 lens). ``browsers.mode`` and
    ``migrated_lanes`` are read on every tick (no restart to flip) from the host-local browser-hosts.json
    browserd reads (``kanban_browser_gate.registry_path``; the repo file is only the ``mode: off`` seed).
    A card with no ``browser:*`` need is untouched. Per card:

    * ``legacy_route`` (mode off, lane not migrated, identity unsealed): today's placement, one event
      per (card, need, lane), never a ``BROWSER_*`` token, never a browserd call while mode is off.
    * a refusal: the card parks ``needs_input`` with the token as the first line (dry run: reported).
    * a lease plan: held ``ready`` with ``browser_join_off`` and NEVER spawned onto a legacy profile; a
      ledger wait holds it the same way. Exception (s7 join, ``kanban.browser_join: true``, t_2828292d): a
      ``browser:ephemeral`` on another host's browserd is recorded in ``result.browser_joins`` and spawned;
      the spawn loop takes the lease through ``dispatch_relay.py`` first (``_browser_join_lease``).
    FAIL CLOSED (Prism 671171332677): a gate fault holds every browser card this tick (``browser_gate_error``)
    and never lets it spawn on a legacy profile; cards with no browser need are untouched. An unassigned card is
    judged as ``kanban.default_assignee`` (the profile the ready loop will assign), never as nobody.
    """
    from hermes_cli import kanban_browser_gate as _kbg
    from hermes_cli import kanban_needs as _kn
    rows = [r for r in ready_rows if _kn.declared_resources(r["body"])]
    if not rows:
        return
    _dflt = _resolve_default_assignee(default_assignee)
    try:
        reg = _kbg.load(_kb.kanban_home() / "fleet", _root_kanban_cfg().get("browser_hosts_file"), log=_kb._log)
        join_on = join and reg.mode == _kbg.MODE_ON and _browser_join_on()
        lookup = _browser_lookup() if reg.mode == _kbg.MODE_ON else (lambda name: None)
        eff = [r if r["assignee"] or not _dflt else dict(dict(zip(r.keys(), tuple(r))), assignee=_dflt)
               for r in rows]
        verdicts = _kbg.gate_rows(eff, registry=reg, lookup=lookup,
                                  minted_by=_kbg.minted_by_of(conn, [r["id"] for r in rows]),
                                  worker_host=lambda r: _pin_host(row_route.get(r["id"])))
    except Exception:
        _kb._log.warning("kanban dispatch: browser gate failed; browser cards held this tick", exc_info=True)
        for r in rows:
            result.placement_caps.setdefault(r["id"], {})["browser_wait"] = {"reason": _kbg.WAIT_GATE_ERROR}
            row_route[r["id"]] = ("wait", _kbg.WAIT_GATE_ERROR)
        return
    # Ownership before persistence (Prism 2a0c67315e6c / d702aef9bd19): only a row this home would itself
    # spawn (an effective assignee that passes the ready loop's _profile_exists_fn / dispatch_profiles
    # predicate) may get a persisted event, a status change or a wait route. A foreign row keeps a dry
    # verdict in placement_caps only and is left to the ready loop exactly as today (it never spawns it).
    _exists = _profile_exists_fn()
    owned = {r["id"] for r in eff if r["assignee"] and (_exists is None or _exists(r["assignee"]))}
    for tid, v in verdicts.items():
        caps = result.placement_caps.setdefault(tid, {})
        if tid not in owned:
            caps.update({"browser_foreign": True, **({"legacy_route": v.legacy[-1]} if v.legacy else {}),
                         **({"browser_refused": dict(v.refusal_payload or {}, exit=v.refusal.exit)}
                            if v.refusal is not None else {})})
            continue
        for payload in v.legacy:
            caps["legacy_route"] = payload
            if dry_run:
                continue
            try:
                if conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? "
                        "AND json_extract(payload, '$.need') = ? AND json_extract(payload, '$.lane') = ? "
                        "AND json_extract(payload, '$.reason') = ? LIMIT 1",
                        (tid, _kn.LEGACY_ROUTE_EVENT, payload["need"], payload["lane"],
                         payload["reason"])).fetchone() is None:
                    with _kbc.write_txn(conn):
                        _kb._append_event(conn, tid, _kn.LEGACY_ROUTE_EVENT, payload)
            except Exception:
                _kb._log.warning("kanban dispatch: could not record legacy_route for %s", tid, exc_info=True)
        if v.refusal is not None:
            caps["browser_refused"] = dict(v.refusal_payload or {}, exit=v.refusal.exit)
            row_route[tid] = ("wait", f"browser_refused:{v.refusal.token}")
            if not dry_run and park:
                try:
                    if _kb.block_task(conn, tid, reason=v.refusal.line(), kind="needs_input",
                                      require_unclaimed_ready=True):
                        with _kbc.write_txn(conn):
                            _kb._append_event(conn, tid, _kbg.REFUSED_EVENT, v.refusal_payload)
                        caps["action"] = "needs_input"
                except Exception:
                    _kb._log.warning("kanban dispatch: could not park %s on %s", tid, v.refusal.token,
                                     exc_info=True)
            continue
        if v.wait is not None:
            caps["browser_wait"] = {"reason": v.wait.reason, "need": v.wait.need, "lane": v.wait.lane}
            row_route[tid] = ("wait", v.wait.reason)
        elif v.plans:
            caps["browser_lease_plan"] = [p.as_dict() for p in v.plans]
            wh = _pin_host(row_route.get(tid))
            if join and join_on and _relay_joinable(v.plans, wh):
                result.browser_joins[tid] = {"plans": [p.as_dict() for p in v.plans], "source_host": wh}
                caps["browser_join"] = dict(result.browser_joins[tid])
            else:
                row_route[tid] = ("wait", _kbg.WAIT_JOIN_OFF)


def _accepts_kw(fn, name: str) -> bool:
    import inspect
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _browser_release_lease(host: str, token: str) -> dict:
    """Release a joined lease by its token on ``host`` (``browserctl --lease-token-stdin release`` over ssh, the
    ctl_api remote path; the token rides stdin, never argv). Tests replace this seam."""
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                              "/usr/local/bin/browserctl --lease-token-stdin --json release"],
                             input=token + "\n", capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "refusal": "BROWSER_RELAY_TRANSPORT", "detail": type(exc).__name__}
    return {"ok": out.returncode == 0, "rc": out.returncode}


def _browser_release_joined(conn, claimed, join: dict, env: Mapping[str, str], why: str) -> None:
    """A lease the join took for a worker that never ran (spawn failed, claim lost): release it now so a
    retry does not strand one ci-box ephemeral per failed spawn. Best effort; the reaper's TTL is the floor."""
    (plan,) = join["plans"]
    try:
        r = _browser_release_lease(plan["host"], env["BROWSER_LEASE_TOKEN"])
    except Exception as exc:  # never break the tick on cleanup
        r = {"ok": False, "detail": type(exc).__name__}
    try:
        with _kbc.write_txn(conn):
            _kb._append_event(conn, claimed.id, "browser_lease_released",
                              {"host": plan["host"], "why": why, "ok": bool(r.get("ok"))},
                              run_id=claimed.current_run_id)
    except Exception:
        _kb._log.warning("kanban dispatch: could not record browser lease release for %s", claimed.id, exc_info=True)


def _browser_join_refused(conn, claimed, token: str, result, failure_limit) -> None:
    """R3(c)'s one exit shape for a lease the join could not take: the claim is released, the card goes back
    to ``ready`` (the run closes ``spawn_failed`` with ``infrastructure: true``, so the breaker is never
    charged), one ``browser_dispatch_refused {token}`` event. Never a re-route, never a spawn without it.
    Fenced to the claimed run (Prism 375e680b84c1): a successor run that claimed the card while the relay
    was out is never touched."""
    from hermes_cli import kanban_browser_lease as _kbl
    task_id = claimed.id
    _kb._log.warning("kanban dispatch: browser join refused for %s: %s", task_id, token)
    result.placement_waits[task_id] = f"browser_refused:{token}"
    fenced = _record_task_failure(conn, task_id, f"browser lease: {token}", outcome="spawn_failed",
                                  release_claim=True, end_run=True, failure_limit=failure_limit, infrastructure=True,
                                  run_metadata_extra={"browser_refused": token},
                                  expected_run_id=claimed.current_run_id)
    if fenced is None:
        _kb._abort_lost_claim_spawn(conn, claimed, None)
        return
    with _kbc.write_txn(conn):
        _kb._append_event(conn, task_id, _kbl.REFUSED_EVENT, {"token": token, "join": True})


def _browser_hold_review_rows(conn, review_rows, result, dry_run: bool) -> list:
    """Review spawns go through the SAME browser gate (Prism 1d0c5e1cc25d): a reviewer keeps the card's skills, so a
    review row whose browser need is refused, waiting, a lease plan, or a gate fault is not spawned this tick. It is
    never parked: the card's own lane decides that, not its reviewer. Rows with no browser need pass unchanged."""
    if not review_rows:
        return review_rows
    ids = [r["id"] for r in review_rows]
    marks = ",".join("?" * len(ids))
    full = conn.execute(f"SELECT id, assignee, body, skills FROM tasks WHERE id IN ({marks})", ids).fetchall()
    held: dict = {}
    _browser_gate(conn, full, result, held, dry_run, park=False, join=False)
    for tid, (_route, why) in held.items():
        result.placement_waits[tid] = why
    return [r for r in review_rows if r["id"] not in held]


def _attached_skill_gap(row, host: str, caps) -> Optional[str]:
    """Layer 0b: the enforced capability an attached skill needs that ``host``
    lacks (t_ecfd5591, F14), or None. Never raises."""
    try:
        from hermes_cli import kanban_placement_failfast as _pff
        skills = json.loads(row["skills"] or "[]") or []
        return _pff.attached_skill_gap(skills, host, caps)
    except Exception:
        _kb._log.debug("kanban dispatch: layer 0b check failed open", exc_info=True)
        return None


def _caps_into_result(result, caps, caps_out: dict) -> None:
    """Surface the capability layer's non-route observations on the result
    (the dry-run column reads them)."""
    if caps is None:
        return
    if caps.error:
        result.placement_caps["_map_error"] = caps.error
    for tid, payload in (caps_out.get("observed") or {}).items():
        result.placement_caps.setdefault(tid, {})["warn"] = payload
    for tid, req in (caps_out.get("unknown") or {}).items():
        result.placement_caps.setdefault(tid, {})["unknown"] = req.cap.split(":", 1)[-1]
    for tid, (host, req) in (caps_out.get("pin_lacks") or {}).items():
        result.placement_caps.setdefault(tid, {})["pin_lacks"] = f"host:{host} lacks {req.cap}"
    for tid, reasons in (caps_out.get("reasons") or {}).items():
        result.placement_caps.setdefault(tid, {})["reasons"] = reasons
    for tid, hosts in (caps_out.get("only") or {}).items():
        result.placement_caps.setdefault(tid, {})["only"] = list(hosts)


def _identity_only_unplanned(rows) -> dict:
    """``{task_id: "requires_unsatisfied:<cap>"}`` for ready cards with no pool
    plan this tick that need an identity-only capability and carry no
    ``host: studio`` pin. With no plan nothing is placed remotely, so the
    card waits. Empty when the layer is off, the map absent, or no capability
    is identity-only (today's map: byte-identical)."""
    try:
        from hermes_cli import kanban_host_capabilities as _khc
        from hermes_cli import kanban_worker_pool as _kwp
        cfg = _kwp.read_pool(_kb.kanban_home() / "fleet")
        caps = _capability_tick(cfg)
        if caps is None or not caps.cmap.identity_only:
            return {}
        known = tuple(cfg.hosts or ()) or _kwp.known_host_ids(_kb.kanban_home() / "fleet")
        out = {}
        for r in rows:
            pin, _ignored = _kwp.resolve_pin(r["body"], known)
            if pin == _kwp.PIN_STUDIO:
                continue
            try:
                skills = tuple(s for s in (json.loads(r["skills"] or "[]") or []) if isinstance(s, str))
            except (TypeError, ValueError):
                skills = ()
            v = _khc.evaluate(r["body"], skills, caps,
                              pin_host=pin if pin not in (None, _kwp.PIN_ANY) else None,
                              any_pin=pin == _kwp.PIN_ANY)
            need = sorted(c.cap for c in v.enforced if c.cap in caps.cmap.identity_only
                          and (pin != _kwp.PIN_ANY or c.strength == _khc.HARD))
            if need and pin in (None, _kwp.PIN_ANY):
                out[r["id"]] = f"{_khc.UNSATISFIED}:{need[0]}"
        return out
    except Exception:
        _kb._log.warning("kanban dispatch: identity-only capability check failed", exc_info=True)
        return {}


def _capabilities_unplanned(rows, result) -> None:
    """Dry-run view with no pool plan: the capability verdict of each ready
    card against the pool files, so ``--dry-run`` shows a pin before a spill."""
    try:
        from hermes_cli import kanban_host_capabilities as _khc
        from hermes_cli import kanban_worker_pool as _kwp
        cfg = _kwp.read_pool(_kb.kanban_home() / "fleet")
        caps = _capability_tick(cfg)
        if caps is None:
            return
        if not caps.legacy_body:
            # The planned path drops #1877's body list while the map enforces
            # (F13); the preview must too (Prism 6b270fae4947).
            for tid in [t for t, a in result.placement_affinity.items()
                        if a.get("rule") == "studio_body"]:
                del result.placement_affinity[tid]
        known = tuple(cfg.hosts or ()) or _kwp.known_host_ids(_kb.kanban_home() / "fleet")
        out: dict = {}
        for r in rows:
            pin, _ignored = _kwp.resolve_pin(r["body"], known,
                                              _kwp.pin_aliases(_kb.kanban_home() / "fleet"))
            try:
                skills = tuple(s for s in (json.loads(r["skills"] or "[]") or []) if isinstance(s, str))
            except (TypeError, ValueError):
                skills = ()
            v = _khc.evaluate(r["body"], skills, caps,
                              pin_host=pin if pin not in (None, _kwp.PIN_ANY, _kwp.PIN_STUDIO) else None)
            # portable() returns a pin route before the soft rule: only an
            # unpinned card is held by a soft requirement (Prism 9c7ca62a7410).
            rule = v.hard_rule or (v.soft_rule if pin is None else None)
            if v.pin is not None and rule == v.pin.rule and pin != _kwp.PIN_STUDIO:
                result.placement_affinity[r["id"]] = _khc.affinity_payload(v.pin, v, _khc.ENFORCE)
                if pin not in (None, _kwp.PIN_ANY) and v.pin.strength == _khc.HARD:
                    out.setdefault("pin_lacks", {})[r["id"]] = (pin, v.pin)
            elif v.observed is not None:
                out.setdefault("observed", {})[r["id"]] = _khc.affinity_payload(v.observed, v, _khc.WARN)
        _caps_into_result(result, caps, out)
    except Exception:
        _kb._log.debug("kanban dispatch: dry-run capability view failed", exc_info=True)


def _manifest_affinity(skills, assignee) -> dict:
    from hermes_cli.kanban_host_affinity import manifest_affinity_safe
    return manifest_affinity_safe(skills, assignee)


def _manifest_targets(skills, assignee) -> dict:
    from hermes_cli.kanban_host_affinity import manifest_targets_safe
    return manifest_targets_safe(skills, assignee)


def _route_wait(route: str, arg: Optional[str], local_free: bool, plan) -> Optional[str]:
    """Why this row cannot spawn anywhere this tick, or None to proceed.

    Decided before any guard runs so a row with no slot writes no events.
    """
    if route == "wait":
        return arg
    if route == "local":
        return None if local_free else (arg or "local_full")
    if route == "portable" and not local_free:
        if plan.pins_only:
            return "local_full"  # admitting band: the pool takes pins only
        if plan.budget <= 0:
            return plan.full_reason()
    return None


def _i0_counts(reason) -> bool:
    """Does a portable row's wait reason count toward the I-0 per-board key?
    Fail closed: every code counts except the per-row holds that are not about
    capacity (no assignee, the profile's own in-flight cap, a layer-0b
    capability gap on the chosen host). A cap added on the last-resort spawn,
    under any code, counts."""
    from hermes_cli import kanban_placement_reasons as _pr
    code = _pr.take_refusal_code(reason) or ""
    return not (code in _I0_EXEMPT or code.startswith("requires:"))


_I0_EXEMPT = frozenset({"unassigned", "per_profile_cap"})


def _last_resort_allowed(route: str, local_free: bool, cap_only, plan) -> bool:
    """Spawn a pool-refused row locally this tick? Only a ``portable`` row
    under ``kanban.placement.default: remote`` with local allowance left and no
    capability ``only`` set (a docker card never drifts onto the Studio, §3.4).
    No count, no cap (spec v0.3 I-0 / I-2): the pool having refused IS the
    last-resort condition."""
    return (route == "portable" and local_free and not cap_only
            and bool(getattr(plan, "default_remote", False)))


def _wall_reason(route: str, arg: Optional[str], plan) -> str:
    """Why a row waits once no slot is left anywhere (record-only mode)."""
    from hermes_cli import kanban_placement_reasons as _pr
    if plan is None:
        return arg if route == "wait" and arg else (arg or _pr.LOCAL_FULL)
    return (_route_wait(route, arg, False, plan)
            or ((plan.pin_host_refusal(arg) or "pin_host_full")
                if route in ("pin", "origin") else plan.full_reason()))


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unavailable profile metadata retains the historic fail-open behavior. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, or respawn-guarded — cannot
    consume the reservation, so it must not withhold capacity from an
    otherwise ready task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        if per_profile_cap is not None and running.get(assignee, 0) >= per_profile_cap:
            continue
        if check_respawn_guard(conn, row["id"], lane="review") is None:
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. When the profiles module isn't importable trust the
    operator's config: the downstream check still buckets a missing profile
    as nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Union[int, Mapping[str, Any], None] = None,
    spawn_paused: Optional[str] = None,
    spawn_limit: Optional[int] = None,
    reconcile_orphans: bool = True,
    pr_gate_prefetch=None,
    budget_cache: Optional[dict] = None,
    spillover_fn=None,
    spillover=None,
    max_new: Optional[int] = None,
    entry: str = "gateway",
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim stale running tasks (no recent heartbeat).
      3. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path, board) -> Optional[int]``. The
         return value (if any) is recorded as ``worker_pid`` so subsequent
         ticks can detect crashes before the TTL expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``max_spawn`` is a **live concurrency cap**, not a per-tick spawn budget:
    it counts tasks already in ``status='running'`` plus this tick's spawns
    against the limit. So ``max_spawn=4`` means "at most 4 workers running
    at any time across the whole board" — matching the gateway's stated
    intent ("limit concurrent kanban tasks"). With a per-tick interpretation
    a 60-second tick interval could grow concurrency by N every minute on a
    busy board and accumulate without bound.

    ``max_in_progress`` is a **host-level** concurrency cap (OOF-30): it
    counts running tasks on every active board — not just this one — plus
    this tick's spawns. Workers are OS processes sharing one machine's
    memory, so a per-board interpretation would multiply the cap by the
    number of active boards. ``max_spawn`` retains its historical per-board
    semantics.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    ``board`` pins workspace/log/db resolution for this tick to a specific
    board. When omitted, the current-board resolution chain is used.
    """
    # Reap zombie children from previously spawned workers. See
    # reap_worker_zombies() for the full rationale.
    exited_workers = reap_worker_zombies()
    result_reaped_terminal = reap_terminal_workers(conn)

    result = DispatchResult()
    # Workers that outlived their closed run are terminated (upstream
    # reap_terminal_workers) before any reclaim/spawn decision this tick.
    result.reaped_terminal_workers = result_reaped_terminal
    if not dry_run:
        # A worker that exited (cleanly or not) leaves its servers behind; reap them now, and sweep
        # init-parented strays in terminal cards' workspaces no retained handle can reach (fork t_446b6b99).
        result.worker_leftovers_reaped = _kb.reap_exited_worker_leftovers(conn, exited_workers)
        try:
            result.orphans_reaped = _kb.sweep_terminal_workspace_orphans(conn, board=board)
        except Exception as exc:  # never break a dispatcher tick
            _kb._log.warning("kanban orphan sweep failed: %s", exc)

    # ---- lane-model overrides (board-level, time-boxed routing) ----
    # One clock read for the whole tick so every card in this pass sees the
    # same TTL and a window cannot expire halfway through a batch.
    _tick_now = int(time.time())
    if not dry_run:
        # Retire elapsed windows first: the rows are deleted, so each expiry
        # is reported exactly once and the routing below can never read a
        # stale override. A dry run reports the board as-is and writes nothing.
        result.expired_lane_models = [
            (row.assignee or "*", row.route)
            for row in _kb.expire_lane_model_overrides(conn, now=_tick_now)
        ]
    _lane_override_cache: dict[str, Optional[_kb.LaneModelOverride]] = {}

    def _lane_override_for(assignee: Optional[str]) -> Optional[_kb.LaneModelOverride]:
        """Active override for ``assignee``, memoized for this tick."""
        key = (assignee or "").strip()
        if key not in _lane_override_cache:
            _lane_override_cache[key] = _kb.get_lane_model_override(
                conn, assignee=key or None, now=_tick_now,
            )
        return _lane_override_cache[key]

    for _lane_key, _ in result.expired_lane_models:
        result.expired_lane_successors[_lane_key] = _kb.lane_successor_label(
            _lane_override_for(None if _lane_key == "*" else _lane_key)
        )

    # First tick after process start: mark lost persisted paths before reapers
    # can make them spawnable. Do not repeat O(active tasks) DB transactions and
    # write probes every tick; candidates are rechecked just before claim below.
    startup_key = str(_kb.kanban_db_path(board))
    if dry_run or startup_key not in _kb._workspace_startup_scanned:
        for row in conn.execute(
            "SELECT id FROM tasks WHERE workspace_path IS NOT NULL "
            "AND status IN ('todo', 'ready', 'running', 'review')"
        ).fetchall():
            _kb._workspace_admission_refused(
                conn, row["id"], result, board=board, dry_run=dry_run,
            )
        if not dry_run:
            _kb._workspace_startup_scanned.add(startup_key)
    pr_cycle_key = _kb._pr_state_cache_key(_kb.kanban_db_path(board))
    pr_nonterminal_cache, pr_cycle_skip = _kb._pr_state_caches_for_board(pr_cycle_key)
    pr_state_resolver = _kb._PrStateResolver(
        terminal_cache=_kb._PR_TERMINAL_STATE_CACHE,
        terminal_cache_limit=_kb._RESPAWN_GUARD_PR_TERMINAL_CACHE_LIMIT,
        nonterminal_cache=pr_nonterminal_cache,
        nonterminal_cache_limit=_kb._RESPAWN_GUARD_PR_NONTERMINAL_CACHE_LIMIT,
        cycle_skip=pr_cycle_skip,
    )

    result.crashed = detect_crashed_workers(conn, board=board)
    if reconcile_orphans:
        # Orphaned-card reconciliation: requeue 'running' cards whose claim
        # bookkeeping is broken (no valid claim, dead/gone worker) that the
        # TTL/crash/stale paths can never see. See reconcile_orphaned_running.
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    # Runs left open on done/archived cards: every sweep above selects
    # running cards only, so nothing else can ever close these rows.
    result.ended_terminal_runs = _kb.end_orphaned_terminal_runs(conn)
    # Classify dead workers before TTL/staleness can erase their terminal status.
    result.reclaimed = _kb.release_stale_claims(conn)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds,
    )
    try:
        from hermes_cli.config import load_config as _load_stall_config
        stall_cfg = (_load_stall_config() or {}).get("kanban") or {}
        stall_seconds = int(stall_cfg.get("stall_minutes", 15)) * 60
        reclaim_seconds = int(stall_cfg.get("stall_reclaim_minutes", 25)) * 60
    except (TypeError, ValueError, OSError):
        stall_seconds, reclaim_seconds = 900, 1500
    if stall_seconds > 0 and reclaim_seconds > stall_seconds and not dry_run:
        result.progress_stalled = _kb.detect_progress_stalls(
            conn, stall_seconds=stall_seconds,
            reclaim_seconds=reclaim_seconds, board=board,
        )
    # detect_crashed_workers stashes protocol-violation auto-blocks on
    # itself so the public list-return stays stable. Pull them into the
    # DispatchResult here so telemetry / tests see the trip.
    _crash_auto_blocked = getattr(
        detect_crashed_workers, "_last_auto_blocked", []
    )
    if _crash_auto_blocked:
        result.auto_blocked.extend(_crash_auto_blocked)
    # Rate-limited requeues (quota wall, no failure counted) — surface for
    # telemetry / tests. These tasks went back to ``ready`` and the respawn
    # guard will defer them until the quota window clears.
    _crash_rate_limited = getattr(
        detect_crashed_workers, "_last_rate_limited", []
    )
    if _crash_rate_limited:
        result.rate_limited.extend(_crash_rate_limited)
    # Harness-unavailable requeues (exit 126/127, the CLI could not run) —
    # same "no failure counted" treatment, reported under their own name so
    # a deploy outage is never read as a quota wall.
    _crash_infra = getattr(
        detect_crashed_workers, "_last_infra_unavailable", []
    )
    if _crash_infra:
        result.infra_unavailable.extend(_crash_infra)
    _crash_cohort = getattr(detect_crashed_workers, "_last_cohort_deaths", [])
    if _crash_cohort:
        result.cohort_deaths.extend(_crash_cohort)
    result.timed_out = enforce_max_runtime(conn)
    if not dry_run:
        # A live worker whose tree outgrew the per-run cap (t_368e9873): the
        # reapers above only act once a worker is gone.
        try:
            from hermes_cli.kanban_proc_bounds import enforce_worker_process_cap
            result.process_capped = enforce_worker_process_cap(conn)
        except Exception as exc:  # never break a dispatcher tick
            _kb._log.warning("kanban process cap check failed: %s", exc)
    # PR-gate re-evaluation BEFORE recompute_ready so a card whose external
    # gate is already satisfied becomes spawnable in the SAME tick rather
    # than waiting for the next one. Bounded + cached + fail-safe: see
    # hermes_cli.kanban_pr_gate for why every uncertain path is a no-op.
    _kb._reevaluate_pr_gates_for_tick(
        conn, result, dry_run=dry_run, prefetched=pr_gate_prefetch,
    )
    # Timed wakes BEFORE recompute_ready so a woken card whose parents are
    # done is spawnable this tick. Skipped under dry_run (the SAFE probe).
    if not dry_run:
        result.woken_scheduled = _kb.wake_due_scheduled(conn)
        _reroute_placement_blocks(conn, result)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)
    # Explicit human holds whose graph dependencies are already satisfied.
    # Computed after promotion so creation-source dependency holds have left
    # ``blocked`` and only intentionally sticky worker/operator blocks remain.
    result.parent_satisfied_sticky = _kb.find_parent_satisfied_sticky_blocks(conn)
    # Children held in ``todo`` behind a parent only a human can clear.
    # Computed AFTER recompute_ready so anything promotable this tick has
    # already left ``todo`` and can't be mis-reported as stranded.
    result.stranded_by_triage = _kb.find_stranded_by_triage(conn)
    # Scheduled cards nothing will ever wake (no timed wake, parked > 24h).
    result.unwoken_scheduled = _kb.find_unwoken_scheduled(conn)

    # Fan-out brake: per-board rolling-window USD ceiling. Evaluated AFTER all
    # reclaim/promotion bookkeeping so a paused board stays accurate on the
    # dashboard, and BEFORE any spawn decision so the pause actually withholds
    # workers. Fail-open by construction (see evaluate_board_budget): a cost
    # measurement fault must never halt the host.
    #
    # Skipped entirely under ``dry_run``: the documented SAFE probe must not
    # write a pause marker or fire a real page about a tick it only observes.
    # (Same rule the review-stale detector follows.)
    #
    # kanban.placement.route_first (t_7133e03d, read per tick): every total-
    # budget gate below records a ``total:<gate>`` hold on each ready row
    # before it returns, and a paused tick with no plan still routes.
    from hermes_cli import kanban_placement_reasons as _pr
    route_first = _route_first_enabled()
    if not dry_run:
        try:
            from hermes_cli import kanban_budget as _kbudget

            # Single-board addressing, NOT enumeration — do not wrap in
            # enumerating_boards(); that would suppress exactly the pin-vs-board
            # contradiction warning this lookup should surface.
            if _kbudget.evaluate_board_budget(
                board,
                _kb.kanban_db_path(board=board),
                _kb.board_state_dir(board),
                cache=budget_cache,
            ):
                result.budget_paused = True
                if route_first:
                    _record_total_hold(conn, result, _pr.TOTAL_BUDGET_PAUSED, dry_run)
                return result
        except Exception as exc:
            _kb._log.warning(
                "kanban dispatch: budget gate failed (%s: %s); continuing tick",
                type(exc).__name__, exc,
            )

    # Count tasks already running so max_spawn enforces concurrency rather
    # than a per-tick spawn budget. See the docstring above for the full
    # rationale; the short version is that a 60-second tick interval with a
    # per-tick budget of N would grow concurrency by N every tick on a busy
    # board, since "running" tasks aren't reclaimed by completion alone —
    # they sit in status='running' until the worker calls
    # kanban_complete/kanban_block (or the dispatcher TTL-reclaims them).
    # Load gate (kanban.dispatch_load_gate): reclaim/stale/orphan bookkeeping
    # above already ran, so a paused tick keeps the board honest but adds
    # NO new workers to an overloaded host. Measured 2026-09-24: load1 80-110
    # on 32 cores while the dispatcher kept spawning 9-14 workers a tick.
    # KWLB v0.1 TWO budgets (PRD 5.2.6, RC-2). ``local_budget`` = the gate's
    # spawn_limit (None only when the gate is disabled; a paused tick is 0),
    # decremented by LOCAL spawns only. ``spawn_budget`` below is the TOTAL
    # budget (max_spawn ∩ max_in_progress ∩ memory), decremented by local AND
    # remote spawns: every worker loop runs here. The local budget is never
    # intersected into the total. ``spillover`` = the gateway tick's shared
    # pool plan (its ``spillover_fn`` test seam only when none is passed).
    if spillover is None and spillover_fn is not None:
        spillover = spillover_fn(conn)
    local_budget: Optional[int] = None if spawn_limit is None else max(0, int(spawn_limit))
    if spillover is not None:
        result.spillover = spillover.summary()
    if spawn_paused:
        result.spawn_paused = str(spawn_paused)
        local_budget = 0
        if (spillover is None or spillover.budget <= 0) and not route_first:
            return result

    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Convert any concurrency caps into a shared additional-spawns budget
    # for this tick. Both ready and review loops consume from the same
    # budget so the total number of new workers stays bounded.
    if max_spawn is not None:
        if running_count >= max_spawn:
            result.spawn_capped = (
                f"max_spawn={max_spawn} reached: {running_count} running on this board"
            )
            if route_first:
                _record_total_hold(conn, result, _pr.TOTAL_MAX_SPAWN, dry_run)
            return result
        spawn_budget = max_spawn - running_count

    # Honour kanban.max_in_progress across both ready and review queues: if
    # the board already has enough running tasks, skip this tick entirely.
    # When there is room left, intersect the remaining in-progress budget
    # with any explicit max_spawn cap above.
    #
    # max_in_progress is a HOST-level cap, not a per-board one (OOF-30):
    # workers are OS processes sharing one machine's memory, so running
    # workers on every other board count against the same budget. Without
    # this, N active boards multiply the cap by N — exactly the fan-out
    # the memory-derived default exists to prevent.
    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            result.spawn_capped = (
                f"max_in_progress={max_in_progress} reached: {total_running} "
                f"running host-wide ({running_count} on this board)"
            )
            if route_first:
                _record_total_hold(conn, result, _pr.TOTAL_MAX_IN_PROGRESS, dry_run)
            return result
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Load-gate allowance (kanban.dispatch_load_gate, see kanban_load_gate):
    # the per-tick number of NEW LOCAL workers this host can absorb. It caps
    # ``local_budget`` only, never the total (a paused tick may still place).
    if local_budget == 0 and not spawn_paused:
        result.spawn_capped = (
            "load gate: this tick's spawn allowance for this board is 0"
        )

    # Memory-pressure guard (OOF-30/OOF-77): even a well-chosen static cap
    # can't see the host's actual memory state (other tenants, bloated
    # long-lived workers, dashboard growth). Under observed pressure the
    # dispatcher stops adding load: critical -> spawn nothing this tick;
    # elevated -> at most one new worker. Reclaim/promotion above already
    # ran, so board bookkeeping stays live either way, and deferred tasks
    # simply wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        if route_first:
            _record_total_hold(conn, result, _pr.TOTAL_MEMORY_CRITICAL, dry_run)
        return result
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1

    # ``max_new`` (CLI ``--max N``, additive) caps NEW workers this call
    # across local AND remote spawns: it is a TOTAL-budget cap, never only
    # the load gate's local allowance (else remote pins bypass it).
    if max_new is not None:
        _cap = max(0, int(max_new))
        if spawn_budget is None or spawn_budget > _cap:
            spawn_budget = _cap
        if _cap == 0:
            result.spawn_capped = "--max=0: no new workers this call"
            if route_first:
                _record_total_hold(conn, result, _pr.TOTAL_MAX_NEW, dry_run)
            return result

    from hermes_cli import kanban_continuation as _kcont_lost
    # HOST_LOST (F18) counts only ticks whose pool probe asked about the
    # running workspaces; a planless CLI dispatch is no evidence either way.
    if (not dry_run and spillover is not None and spillover.continuation_probed
            and _kcont_lost.mode(_root_kanban_cfg()) == 'enforce'):
        _kcont_lost.interrupt_lost_hosts(
            conn, spillover.continuation_states,
            hosts={h.name for h in spillover.config.pool_hosts})
    ready_rows = conn.execute(
        "SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker, priority, "
        "origin_host, workspace_of "
        "FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    # Operator-only cards are never spawnable, whatever their assignee.
    ready_rows = _kb._drop_no_worker_rows(conn, ready_rows, result, dry_run=dry_run)
    # One route per row (KWLB D-11): with a pool plan, ONE stable sort puts
    # remote pins first, then non-portable cards (first claim on the local
    # budget), then portable ones; the ORDER BY holds inside each class.
    # Without a plan every row is local and the order is unchanged.
    row_route: dict = {}
    row_rules: dict = {}  # task id -> portable() rule, for the placement record
    # Ready cards whose Studio binding was evaluated this tick (a pool plan
    # ran the classifier on every one of them) and, of those, the ones the
    # spawn loop reached: the affinity event log reconciles against both.
    _affinity_evaluated: list = []
    _affinity_reached: set = set()
    _wait_reached: list = []  # rows that had a slot (placement_wait clear)
    _caps = None
    _caps_out: dict = {}
    if spillover is not None:
        if dry_run:
            # Dry run reserves on its own ledger: the shared plan is untouched.
            import dataclasses as _dc
            # Real takes already sit in the ledger choose() reads; only this
            # copy's own takes are simulated (Prism 08bbc332020f).
            spillover = _dc.replace(spillover, slots=dict(spillover.slots), taken=dict(spillover.taken),
                                    dry_run=True, _rids={}, _simulated=dict(spillover._simulated))
        _eff_default = (default_assignee or "").strip() or None
        _native: dict = {}
        _lineage = _lineage_pins_mode()  # kanban.placement.lineage_pins, per tick
        _linked = _linked_ids(conn, [r["id"] for r in ready_rows], _lineage)
        _binds = _workspace_binds(conn, ready_rows, dry_run=dry_run)  # spec §2.4, per tick
        _caps = _capability_tick(spillover.config)  # kanban.placement.capabilities, per tick
        spillover.continuation_dry_run = dry_run
        row_route = {r["id"]: _route_row(conn, r, spillover, _eff_default, _native, _linked,
                                         _lineage, result.placement_affinity,
                                         unknown_out=result.unknown_pins,
                                         caps=_caps, caps_out=_caps_out,
                                         rules_out=row_rules,
                                         access_out=result.access_contradictions,
                                         binds=_binds)
                     for r in ready_rows}
        _caps_into_result(result, _caps, _caps_out)
        _affinity_evaluated = [r["id"] for r in ready_rows]
        _rank = {"origin": 0, "pin": 0, "wait": 1, "local": 1, "portable": 2}
        ready_rows = sorted(ready_rows, key=lambda r: _rank[row_route[r["id"]][0]])
    else:
        # No plan this tick (pool disabled, refused, no hosts, proc_paused,
        # or a caller that plans no pool): a REMOTE pin still never runs
        # locally (I-11). It waits until a plan can place it.
        # A pin is only a host the pool files name: with no pool configured
        # a ``host: <word>`` line is prose and the card runs locally.
        from hermes_cli import kanban_worker_pool as _kwp
        _known = None
        _aliases = None
        for r in ready_rows:
            if _kwp.card_pin(r["body"]) is None:
                continue
            if _known is None:
                _known = _kwp.known_host_ids(_kb.kanban_home() / "fleet")
                _aliases = _kwp.pin_aliases(_kb.kanban_home() / "fleet")
            _pin, _ignored = _kwp.resolve_pin(r["body"], _known, _aliases)
            if _ignored is not None:
                _kwp.note_ignored_pin(r["id"], _ignored)
                if _pin is None:
                    # No plan: it runs locally anyway, but still says so (t_cb2b888d).
                    result.unknown_pins[r["id"]] = _ignored
            # Not ``elif``: a prose ``Host:`` line above a real pin returns
            # BOTH, and the pin must still wait (Prism 56085b64).
            if _pin is not None and _pin not in (_kwp.PIN_ANY, _kwp.PIN_STUDIO):
                row_route[r["id"]] = ("wait", "pool_unavailable")
        # Identity-only capabilities (docker) hold with or without a plan:
        # no plan is the normal admitting band, and an unpinned docker card
        # must not spawn locally there either (Prism b83b86d68a65 on #1902).
        for _rid, _why in _identity_only_unplanned(ready_rows).items():
            row_route.setdefault(_rid, ("wait", _why))
        if dry_run:
            from hermes_cli.kanban_host_affinity import studio_affinity_unplanned
            studio_affinity_unplanned(
                ready_rows, (default_assignee or "").strip() or None, result)
            _capabilities_unplanned(ready_rows, result)
        _unplanned = sum(1 for v in row_route.values() if v == ("wait", "pool_unavailable"))
        if _unplanned:
            # No plan this tick: the gateway tick and the one-shot CLI both
            # plan the pool, so this is a disabled/refused/empty pool,
            # proc_paused, or the standalone daemon (which never places).
            _kb._log.info(
                "kanban dispatch: %d remote-pinned card(s) wait pool_unavailable: no pool "
                "plan this tick (pool disabled/refused/no hosts, proc_paused, or a "
                "standalone daemon, which never places remotely)", _unplanned,
            )
    # An absent pool plan must not send a sticky continuation to Studio.
    from hermes_cli import kanban_continuation as _kcont_unplanned
    if spillover is None and _kcont_unplanned.mode(_root_kanban_cfg()) == "enforce":
        # Resolve every origin even without a pool plan: registry failures
        # refuse immediately and absent-host waits retain their timeout.
        from hermes_cli import kanban_worker_pool as _kwp_origin
        _unplanned_pool = _kwp_origin.read_pool(_kb.kanban_home() / 'fleet', kanban_cfg=_root_kanban_cfg())
        for r in ready_rows:
            if r["origin_host"]:
                _origin_route = _kcont_unplanned.origin_route(
                    conn, r, _unplanned_pool, config=_root_kanban_cfg(), states={}, dry_run=dry_run,
                    caps=_capability_tick(_unplanned_pool),
                )
                row_route[r["id"]] = _origin_route or ('local', None)
    # Fleet pause hold, layer 1 (t_133d6efd, spec §5.8): after BOTH routing branches, before any spawn.
    from hermes_cli import kanban_pause_hold as _kph
    _pause_cache: dict = {}
    _kph.gate_rows(conn, ready_rows, result, row_route, dry_run, _pause_cache, lane="ready",
                   planned=spillover is not None, entry=entry)
    # Browser Sandboxes resolution gate (t_42728faf): after every route is known, before any spawn.
    _browser_gate(conn, ready_rows, result, row_route, dry_run, default_assignee)
    # Review rows are enumerated up front (not after the ready loop) so the
    # budget split below can see whether review work exists at all. The
    # review lane is never offered to the pool: its rows are always local.
    review_rows = []
    if review_dispatch_enabled():
        review_rows = conn.execute(
            "SELECT id, assignee, no_worker, body, skills, workspace_path, workspace_kind, origin_host FROM tasks "
            "WHERE status = 'review' AND claim_lock IS NULL "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        review_rows = _kb._drop_no_worker_rows(
            conn, review_rows, result, dry_run=dry_run,
        )
        review_rows = _browser_hold_review_rows(conn, review_rows, result, dry_run)
        review_rows = _kph.gate_rows(conn, review_rows, result, row_route, dry_run, _pause_cache,
                                     lane="review", entry=entry)
    # Review-lane reservation (OOF-30 review finding): the ready loop runs
    # first and used to consume the ENTIRE shared budget, so a sustained
    # ready backlog permanently starved autonomous reviews — completed work
    # sat in 'review' forever while new work kept spawning. When spawnable
    # review work exists and the tick has any budget, hold one slot back
    # from the ready loop so the review lane always gets a spawn
    # opportunity. The reservation is per-tick and self-releasing: with no
    # spawnable review work (or no cap at all) the ready loop keeps the
    # full budget. "Spawnable" mirrors the review loop's own gate
    # (assigned + real profile) so a review column full of human-pulled
    # control-plane lanes doesn't permanently tax ready throughput.
    # Per-profile concurrency cap (#21582): when set, track how many
    # workers each assignee already has in flight, and refuse to spawn
    # when this would push that assignee past the cap. Prevents
    # fan-out workloads from melting a single profile's local model /
    # API quota / browser pool while leaving other profiles idle.
    # Tasks blocked this way go to skipped_per_profile_capped (not
    # skipped_unassigned — the operator-actionable signal is different:
    # "this profile is busy, try again later" not "this needs routing").
    # ``max_in_progress_per_profile`` is an int (one cap for all) or a mapping
    # ``{default: N, <profile>: M}`` (see resolve_per_profile_cap). ``_per_profile_cap``
    # stays as the "is any cap configured" sentinel; the per-assignee value is
    # looked up through ``_cap_for``.
    _per_profile_spec = max_in_progress_per_profile if (
        isinstance(max_in_progress_per_profile, Mapping)
        or (isinstance(max_in_progress_per_profile, int) and max_in_progress_per_profile > 0)
    ) else None
    _per_profile_cap = _per_profile_spec

    def _cap_for(assignee: Optional[str]) -> Optional[int]:
        return _kb.resolve_per_profile_cap(_per_profile_spec, assignee)

    _per_profile_running: dict[str, int] = {}
    if _per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            _per_profile_running[prow["assignee"]] = int(prow["n"])
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick (respawn guard,
    # per-profile cap): such a row cannot use the reserved slot and must not
    # starve the ready lane (``_any_spawnable_review``).
    def _any_spawnable_review_this_tick() -> bool:
        if not review_rows:
            return False
        if _per_profile_cap is None:
            return _any_spawnable_review(conn, review_rows)
        return any(
            _any_spawnable_review(
                conn, [row],
                per_profile_cap=_cap_for(row["assignee"]),
                per_profile_running=_per_profile_running,
            )
            for row in review_rows
        )

    ready_budget = spawn_budget
    # Review rows spawn locally, so the reservation holds back one LOCAL slot
    # too (``ready_local``); the review loop checks the full ``local_budget``.
    ready_local = local_budget
    local_spawned = 0
    if (
        (spawn_budget is None or spawn_budget > 0)
        and (local_budget is None or local_budget > 0)
        and (spawn_budget is not None or local_budget is not None)
        and _any_spawnable_review_this_tick()
    ):
        if spawn_budget is not None:
            ready_budget = max(spawn_budget - 1, 0)
        if local_budget is not None:
            ready_local = local_budget - 1
    # Lazily populated only when a ready card reaches the point where it would
    # actually spawn. A queue containing only unassigned, capped, guarded, or
    # control-plane cards pays zero collision-scan queries on every idle tick.
    reported_collision_scopes: Optional[dict[str, set[str]]] = None
    collision_scope_load_failed = False
    spawned = 0
    from hermes_cli.kanban_provider_health import (
        available_profile_fallback, capped_provider, configured_min_eligible,
        configured_box_health, configured_pool_health_urls, configured_probes,
        configured_pool_shed, configured_pool_spawns_per_eligible, effective_provider,
        pool_budget_eligible, pool_key, pool_route, shed_priority,
    )
    health_probes = configured_probes()
    min_eligible = configured_min_eligible()
    shed_tiers = configured_pool_shed()
    pool_urls = configured_pool_health_urls()
    box_health = configured_box_health()
    pool_spawns_per_eligible = configured_pool_spawns_per_eligible()
    # The relay pools are shared by every board, so the per-tick admission
    # budget must be too. The gateway tick calls dispatch_once once per board
    # with ONE ``budget_cache``; keep the admitted-per-pool counter (and the
    # health probe results it is measured against) in that tick-scoped dict so
    # board N+1 sees board N's spawns. Callers without cross-board state (CLI,
    # standalone daemon) pass no cache and get a fresh per-call map.
    if budget_cache is not None:
        health_cache: dict = budget_cache.setdefault(("_provider_health_cache",), {})
        admitted_this_tick: dict[str, int] = budget_cache.setdefault(
            ("_pool_admitted_this_tick",), {},
        )
    else:
        health_cache = {}
        admitted_this_tick = {}
    admitted_routes: dict[str, str | None] = {}
    # The budget is a CONCURRENCY ceiling (t_38be6b10): workers already
    # running on a pool count against it, not just this tick's admissions.
    # Measured 09-24: 80% of residual 429s hit mid-run with 28-32 workers in
    # flight on 1-9 eligible subs, because every tick re-granted eligible*N.
    # ``pool`` is stamped on the run at spawn (charge_pool), so this is one
    # query per board per tick; legacy runs without it count as 0 (fail
    # open). Snapshot taken BEFORE this board spawns anything, so the tick's
    # own spawns are counted once, via ``admitted_this_tick``. Per-board
    # snapshots share the tick cache so later boards see earlier boards'.
    in_flight_by_board: dict[str, dict[str, int]] = (
        budget_cache.setdefault(("_pool_in_flight_by_board",), {})
        if budget_cache is not None else {}
    )
    if pool_spawns_per_eligible:
        in_flight_by_board[board or _kb.DEFAULT_BOARD] = _kb._pool_in_flight(conn)

    def pool_in_flight(pool):
        return sum(m.get(pool, 0) for m in in_flight_by_board.values())

    # Review-lane POOL reservation (t_becb0042, #985 C4 follow-up): the
    # spawn-slot reservation above is not enough when the binding limit is a
    # pool's admission budget. The ready loop runs first and charges every
    # admission to its pool, so a ready backlog >= eligible*N on pool P left
    # every review card on P deferred ``pool_budget`` every tick. Mirror the
    # spawn-slot hold per pool: for each pool that has a spawnable review
    # card, the ready loop sees one fewer admission. Self-releasing: pools
    # with no spawnable review work are untouched, and the map is cleared
    # before the review loop so reviewers see the full pool budget.
    review_pool_reserve: dict[str, int] = {}
    # Reviews only ever run locally: with no local slot this tick (load gate
    # paused / allowance 0) the review loop cannot launch, so holding a pool
    # admission for it would only starve portable ready cards that could
    # place remotely on the same pool.
    if pool_spawns_per_eligible and review_rows and (local_budget is None or local_budget > 0):
        try:
            from hermes_cli.profiles import profile_exists as _rpp
        except Exception:
            _rpp = None
        for _rrow in review_rows:
            if not _rrow["assignee"]:
                continue
            if _rpp is not None and not _rpp(_rrow["assignee"]):
                continue
            _rtask = _kb.get_task(conn, _rrow["id"])
            if _rtask is None:
                continue
            _rtask.assignee = _rrow["assignee"]
            _kb.apply_lane_model_override(
                _rtask, _lane_override_for(_rrow["assignee"]), now=_tick_now)
            _rpool = pool_key(effective_provider(_rtask))
            if _rpool is not None:
                review_pool_reserve[_rpool] = 1

    def pool_budget(provider):
        pool = pool_key(provider)
        if pool is None or pool_spawns_per_eligible == 0:
            return None
        eligible = pool_budget_eligible(provider, health_probes, health_cache, pool_urls,
                                        box_health=box_health)
        if eligible is None:
            return None  # Unknown probe: fail open.
        admitted = admitted_this_tick.get(pool, 0)
        in_flight = pool_in_flight(pool)
        reserved = review_pool_reserve.get(pool, 0)
        if in_flight + admitted + reserved < eligible * pool_spawns_per_eligible:
            return None
        payload = {"reason": "pool_budget", "provider": provider,
                   "pool": pool, "eligible": eligible, "admitted": admitted,
                   "in_flight": in_flight}
        if reserved:
            payload["reserved_for_review"] = reserved
        return payload

    def charge_pool(task_id, run_id=None):
        pool = admitted_routes.pop(task_id, None)
        if pool is not None:
            admitted_this_tick[pool] = admitted_this_tick.get(pool, 0) + 1
            if run_id is not None:
                _kb._stamp_run_pool(conn, int(run_id), pool)

    circuits: dict[str, int] = {}
    try:
        rl_trip = _kb._resolve_rate_limit_trip()
        circuits = _kb.rate_limit_circuits(conn, now=_tick_now, trip=rl_trip)
        if not dry_run:
            for _pool, _until in circuits.items():
                _kb._notify_rate_limit_circuit(board, _pool, _until, rl_trip, now=_tick_now)
    except Exception as exc:
        # Fail OPEN: a broken circuit query must never halt spawning.
        _kb._log.warning("kanban rate-limit circuit check failed (%s: %s)", type(exc).__name__, exc)
        circuits = {}
    cooling: dict[str, int] = {}
    try:
        cooling = _kb.cooling_providers(
            conn, now=_tick_now, window=_kb._resolve_credential_cooldown())
    except Exception as exc:
        # Fail OPEN, same as the circuit query above.
        _kb._log.warning("kanban credential cooldown check failed (%s: %s)", type(exc).__name__, exc)
        cooling = {}

    def pool_shed(task, route_provider, card_pinned):
        """Pool-shed rung (t_9038e9f3): a low-priority card on a claude RELAY
        pool whose eligible count is under a ``kanban.pool_shed`` threshold
        spawns on its profile's first healthy NON-pool rung instead. Card pins
        and per-sub routes are never shed; no such rung = no shed (admitted as
        before). Returns a fallback selection or None."""
        if not shed_tiers or card_pinned or task.pin_sub_reason:
            return None
        route = pool_route(route_provider)
        if route is None or route[1] is not None:
            return None
        eligible = pool_budget_eligible(route_provider, health_probes, health_cache,
                                        pool_urls, box_health=box_health)
        max_prio = shed_priority(shed_tiers, eligible)
        if max_prio is None or (task.priority or 0) > max_prio:
            return None
        skipped: list = []
        fallback = available_profile_fallback(
            task, health_probes, health_cache, min_eligible=min_eligible,
            pool_urls=pool_urls, skip_pools=frozenset(circuits), box_health=box_health,
            budget_available=lambda provider: pool_budget(provider) is None,
            skip_providers=frozenset(cooling), skipped=skipped, non_pool_only=True,
        )
        if fallback is None or fallback_flagship_banned(task.id, fallback[0]):
            return None
        payload = {"reason": "pool_shed", "provider": route_provider, "pool": route[0],
                   "eligible": eligible, "max_priority": max_prio,
                   "priority": task.priority or 0}
        _kb._log.info(
            "PHASE=kanban_pool_shed task=%s from=%s eligible=%s prio=%s<=%s to=%s/%s",
            task.id, route_provider, eligible, task.priority, max_prio, fallback[1], fallback[0],
        )
        admitted_routes[task.id] = pool_key(fallback[1])
        return fallback, payload

    def provider_admission(task_id, assignee):
        if (not health_probes and not any(pool_urls.values()) and not box_health
                and not circuits and not cooling):
            return False, None
        task = _kb.get_task(conn, task_id)
        if task is None:
            return False, None
        task.assignee = assignee
        card_pinned = bool(task.model_override or task.provider_override)
        # Health is judged on the EFFECTIVE route: lane override first (only
        # for cards with no pin of their own), so a capped profile default
        # under a healthy lane is admitted, and a capped lane still falls back.
        _kb.apply_lane_model_override(task, _lane_override_for(assignee), now=_tick_now)
        route_provider = effective_provider(task)
        circuit_pool = pool_key(route_provider)
        if circuit_pool in circuits:
            # An open circuit on THIS card's pool is a capacity verdict like
            # provider_capped: hold unless a rung on another open pool exists.
            payload = {"reason": "rate_limit_circuit", "provider": route_provider,
                       "pool": circuit_pool, "until": circuits[circuit_pool]}
        elif (route_provider or "").strip().lower() in cooling:
            # The route's own credential is cooling: spawning would die at
            # auth (exit 75) and stamp a rate_limit backoff. Same verdict.
            payload = {"reason": "credential_cooldown", "provider": route_provider,
                       "until": cooling[(route_provider or "").strip().lower()]}
        else:
            payload = capped_provider(
                task, health_probes, health_cache, min_eligible=min_eligible,
                pool_urls=pool_urls, box_health=box_health,
            )
            if payload is None:
                payload = pool_budget(route_provider)
        if payload is None:
            shed = pool_shed(task, route_provider, card_pinned)
            if shed is not None:
                return False, shed
            admitted_routes[task_id] = circuit_pool
            return False, None
        from hermes_cli.model_policy import is_sub_pin_route, sub_pin_family_pool

        if task.pin_sub_reason and is_sub_pin_route(task.model_override, route_provider):
            # A deliberate pin (t_957ca870) bypasses the pool, not the
            # governors: a capped / cooling / circuit-open pinned sub WAITS. It
            # never drifts onto the profile ladder; only --pin-sub-fallback lets
            # it ride its family pool (claude-bpr / claude-apr), and only while
            # that pool is itself admissible.
            pool = sub_pin_family_pool(route_provider) if task.pin_sub_fallback else None
            if pool is not None:
                from dataclasses import replace as _dc_replace

                pool_task = _dc_replace(task, provider_override=pool)
                pool_blocked = (
                    pool_key(pool) in circuits
                    or pool in cooling
                    or capped_provider(
                        pool_task, health_probes, health_cache, min_eligible=min_eligible,
                        pool_urls=pool_urls, box_health=box_health,
                    ) is not None
                    or pool_budget(pool) is not None
                )
                if not pool_blocked:
                    admitted_routes[task_id] = pool_key(pool)
                    return False, ((task.model_override, pool), payload)
            payload = {**payload, "pin": route_provider,
                       "pin_fallback": pool if task.pin_sub_fallback else "wait"}
            result.respawn_guarded.append((task_id, payload["reason"]))
            _kb._log.info(
                "PHASE=kanban_dispatch_pin_held task=%s pin=%s reason=%s fallback=%s",
                task_id, route_provider, payload["reason"], payload["pin_fallback"],
            )
            if not dry_run:
                with _kbc.write_txn(conn):
                    _kb._append_deferred_event_once(conn, task_id, payload)
            return True, None
        skipped: list = []
        fallback = available_profile_fallback(
            task, health_probes, health_cache, min_eligible=min_eligible,
            pool_urls=pool_urls, skip_pools=frozenset(circuits), box_health=box_health,
            budget_available=lambda provider: pool_budget(provider) is None,
            skip_providers=frozenset(cooling), skipped=skipped,
        )
        if skipped:
            payload = {**payload, "fallback_skipped": skipped}
        if fallback is not None and fallback_flagship_banned(task_id, fallback[0]):
            # The flagship gate covers the post-fallback route too: a capped
            # pool must not become a side door onto a banned model. Defer
            # instead, exactly as if no healthy rung existed.
            payload = {**payload, "fallback_refused": {
                "model": fallback[0], "provider": fallback[1], "reason": "flagship",
            }}
            _kb._log.warning(
                "PHASE=kanban_flagship_fallback_refused task=%s model=%s provider=%s",
                task_id, fallback[0], fallback[1],
            )
            fallback = None
        if fallback is not None:
            admitted_routes[task_id] = pool_key(fallback[1])
            return False, (fallback, payload)
        result.respawn_guarded.append((task_id, payload["reason"]))
        if skipped:
            # Every rung is capped or cooling: HOLD (deferred, no spawn, no
            # run, so no rate_limited close and no backoff stamp).
            _kb._log.info(
                "PHASE=kanban_dispatch_fallback_held task=%s from=%s reason=%s skipped=%s",
                task_id, route_provider, payload["reason"], _kb._format_skipped_rungs(skipped),
            )
        if not dry_run:
            with _kbc.write_txn(conn):
                _kb._append_deferred_event_once(conn, task_id, payload)
        return True, None

    def note_lane_route(claimed, source):
        """Record the lane-override route on the run (in-memory only on the
        card) so a rate-limited close is charged to the pool that served it."""
        if source is None or not claimed.provider_override:
            return
        with _kbc.write_txn(conn):
            _kb._append_event(conn, claimed.id, "dispatch_lane_route", {
                "provider": claimed.provider_override, "model": claimed.model_override,
            }, run_id=claimed.current_run_id)

    def apply_dispatch_fallback(claimed, selection):
        if selection is None:
            return
        (model, provider), capped = selection
        claimed.model_override, claimed.provider_override = model, provider
        event = {"from_provider": capped["provider"], "to_provider": provider,
                 "to_model": model}
        if capped.get("reason") == "pool_shed":
            event.update({k: capped[k] for k in ("reason", "pool", "eligible", "max_priority")})
        if capped.get("fallback_skipped"):
            event["skipped"] = capped["fallback_skipped"]
        with _kbc.write_txn(conn):
            _kb._append_event(conn, claimed.id, "dispatch_provider_fallback", event,
                          run_id=claimed.current_run_id)

    def fallback_route_source(source, selection):
        if selection[1].get("reason") == "pool_shed":
            return (f"pool-shed({source}; {selection[1]['pool']} eligible "
                    f"{selection[1]['eligible']}, priority <= {selection[1]['max_priority']})")
        skipped = selection[1].get("fallback_skipped")
        if not skipped:
            return f"dispatch-fallback(capped {source})"
        return (f"dispatch-fallback(capped {source}; "
                f"skipped {_kb._format_skipped_rungs(skipped)})")

    try:
        from hermes_cli.config import load_config as _load_dispatch_config

        _model_policy_config = _load_dispatch_config()
    except Exception:
        _model_policy_config = {}

    def fallback_flagship_banned(task_id: str, model: Optional[str]) -> bool:
        """Whether a capped-pool fallback rung would put ``task_id`` on a
        flagship model this tick's policy bans (alias-aware), with no
        ``flagship override:`` comment on the card to authorize it."""
        from hermes_cli.model_policy import (
            FLAGSHIP_OVERRIDE_COMMENT_PREFIX,
            is_firepower_model,
        )

        if not is_firepower_model(model, _model_policy_config):
            return False
        return conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? "
            "AND lower(ltrim(body)) LIKE ? LIMIT 1",
            (task_id, f"{FLAGSHIP_OVERRIDE_COMMENT_PREFIX}%"),
        ).fetchone() is None

    def flagship_refused(task_id: str, assignee: Optional[str] = None) -> bool:
        """Main's flagship gate, applied to the route this spawn will USE.

        The route is the card pin when there is one, otherwise the active lane
        override (same precedence as :func:`apply_lane_model_override`). A
        lane is checked against the policy loaded for THIS tick, so a ban
        tightened after ``lane-model set`` cannot be walked around by the lane
        the old policy admitted. A lane route is authorized by the lane's own
        ``--allow-flagship``/``--firepower`` reason, or by the card's
        ``flagship override:`` comment. Profile defaults are out of scope, as
        on main: they are standing config, not a dispatch-time override.
        """
        from hermes_cli.model_policy import (
            FLAGSHIP_OVERRIDE_COMMENT_PREFIX,
            FLAGSHIP_REFUSAL_COMMENT_PREFIX,
            flagship_model_match,
            is_firepower_model,
        )

        task = _kb.get_task(conn, task_id)
        if task is None:
            return False
        lane: Optional[_kb.LaneModelOverride] = None
        if task.model_override:
            model = task.model_override
            if not flagship_model_match(model, _model_policy_config):
                return False
        else:
            lane = _lane_override_for(assignee or task.assignee)
            model = lane.model if lane is not None else None
            if not is_firepower_model(model, _model_policy_config):
                return False
            if (lane.firepower or "").strip():
                return False
        override = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? "
            "AND lower(ltrim(body)) LIKE ? LIMIT 1",
            (task_id, f"{FLAGSHIP_OVERRIDE_COMMENT_PREFIX}%"),
        ).fetchone()
        if override:
            return False

        result.flagship_refused.append(task_id)
        if dry_run:
            return True
        if lane is None:
            body = (
                f"{FLAGSHIP_REFUSAL_COMMENT_PREFIX} model "
                f"{model!r} is orchestrator-only; add "
                f"'{FLAGSHIP_OVERRIDE_COMMENT_PREFIX} <reason>' to authorize."
            )
        else:
            body = (
                f"{FLAGSHIP_REFUSAL_COMMENT_PREFIX} model {model!r} from the "
                f"lane-model override for {lane.assignee or '(board-wide)'} "
                f"({lane.route}) is orchestrator-only; re-set the lane with "
                f"--allow-flagship <reason>, clear it, or add "
                f"'{FLAGSHIP_OVERRIDE_COMMENT_PREFIX} <reason>' to this card."
            )
        already_logged = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body = ? LIMIT 1",
            (task_id, body),
        ).fetchone()
        if not already_logged:
            _kb.add_comment(conn, task_id, "dispatcher", body)
            _kb._log.warning(
                "PHASE=kanban_flagship_refused task=%s model=%s source=%s",
                task_id,
                model,
                "card-override" if lane is None else "lane-override",
            )
        return True

    def invalid_pin_refused(task_id: str) -> bool:
        """Park a card whose stored ``model_override`` names an id its lane does not serve.

        The writers refuse such a pin at mint; this catches a row written by hand, or
        before the writers validated (t_d564a8dd: ``claude-sonnet-4-8`` on claude-alr,
        8 h of 404s booked as ``rate_limited``). A ``capability`` block fires the
        ``kanban_task_blocked`` hook, so the owner is woken like any handback.
        """
        task = _kb.get_task(conn, task_id)
        if task is None or not task.model_override:
            return False
        from hermes_cli.kanban_provider_health import model_override
        from hermes_cli.model_registry import invalid_pin_reason

        model, provider = model_override(task)
        reason = invalid_pin_reason(provider, model)
        if reason is None:
            return False
        # Prism 68402070f7c9 (hermes-agent#1903): a pin written with ``--force-model`` carries a
        # ``model force: <provider>/<model> ...`` audit comment; honour it for THAT exact pair only,
        # so a later re-pin to a different unknown id is still caught here.
        if _kb.model_force_comment_present(conn, task_id, provider, model):
            _kb._log.info("PHASE=kanban_model_override_forced task=%s provider=%s model=%s",
                          task_id, provider, model)
            return False
        result.auto_blocked.append(task_id)
        if dry_run:
            return True
        valid = ", ".join(_kb._model_registry_valid_ids(provider)) or "(unreadable)"
        body = (
            f"{reason}; the lane serves [{valid}]. Re-pin with "
            f"`hermes kanban set-model {task_id} <model> --provider {provider}` "
            f"(or --force-model <reason>), then unblock."
        )
        _kb._log.warning("PHASE=kanban_model_override_invalid task=%s provider=%s model=%s",
                         task_id, provider, model)
        if _kb.block_task(conn, task_id, reason=reason, kind="capability"):
            _kb.add_comment(conn, task_id, "dispatcher", body)
        return True

    # Normalize default_assignee once: empty/whitespace string → None so the
    # rest of the loop can use ``if default_assignee:`` as a single check.
    # We also resolve profile_exists once here for the same reason.
    _default_assignee = (default_assignee or "").strip() or None
    _default_assignee_resolved = False
    if _default_assignee:
        # Same predicate as the spawn gate: ``kanban.dispatch_profiles``
        # gated (#110995), so a default this home may not claim is never
        # written onto an unassigned shared-board card.
        try:
            _pe = _profile_exists_fn()
            _default_assignee_resolved = _pe is None or bool(_pe(_default_assignee))
        except Exception:
            # Profiles module not importable (test stubs, exotic envs).
            # Trust the operator's config and try the assignment; the
            # downstream profile_exists check on the assigned row will
            # bucket it as nonspawnable if the profile genuinely isn't
            # there, with the existing diagnostic.
            _default_assignee_resolved = True
    ready_scan_complete = bool(ready_rows)
    # The ``placement.gate`` block (spec §4.1): the same for every spawn this tick.
    tick_gate = {
        "band": getattr(spillover, "band", None),
        "spill_reason": getattr(spillover, "spill_reason", None),
        "local_allowance": local_budget,
        "spawn_paused": str(spawn_paused) if spawn_paused else None,
        "total": {"budget_paused": bool(result.budget_paused), "max_spawn": max_spawn,
                  "max_in_progress": max_in_progress, "memory_pressure": result.memory_pressure,
                  "max_new": max_new},
    }
    # Record-only mode (spec §3.1 step 3, B2-ii): once a total budget or
    # every slot is used, the loop keeps walking the remaining rows and
    # records why each one waits instead of stopping silently.
    record_only: Optional[str] = None
    # Portable rows that reached their turn with local allowance left on THIS
    # board (spec v0.3 I-0 per-board key, Momus pass 3 R1); capability-``only``
    # rows are excluded (they never take a local slot, §3.4).
    _portable_room: set = set()
    for row_index, row in enumerate(ready_rows):
        route, route_arg = row_route.get(row["id"], ("local", None))
        if record_only is not None:
            result.placement_waits[row["id"]] = (
                record_only if record_only != "wall"
                else _wall_reason(route, route_arg, spillover))
            continue
        if ready_budget is not None and spawned >= ready_budget:
            ready_scan_complete = False
            if route_first:
                record_only = _pr.total(_pr.TOTAL_READY_BUDGET)
                result.placement_waits[row["id"]] = record_only
                continue
            break
        local_free = ready_local is None or local_spawned < ready_local
        if route == "portable" and local_free and not _caps_out.get("only", {}).get(row["id"]):
            _portable_room.add(row["id"])
        pool_free = spillover is not None and spillover.budget > 0
        if not local_free and not pool_free:
            ready_scan_complete = False
            if route_first:
                # No slot anywhere: this row and every one after it say why.
                record_only = "wall"
                result.placement_waits[row["id"]] = _wall_reason(route, route_arg, spillover)
                continue
            # No slot anywhere: same early stop the intersected budget gave.
            # The row that hit the wall still says why it waits.
            if spillover is not None:
                result.placement_waits[row["id"]] = (
                    _route_wait(route, route_arg, False, spillover)
                    or ((spillover.pin_host_refusal(route_arg) or "pin_host_full")
                        if route in ("pin", "origin") else spillover.full_reason())
                )
                # Host-bound cards behind the wall still say why (t_3e6c9b2b).
                for rest in ready_rows[row_index + 1:]:
                    r_route, r_arg = row_route.get(rest["id"], ("local", None))
                    if rest["id"] in result.placement_affinity and r_route == "local":
                        result.placement_waits[rest["id"]] = r_arg or "local_full"
            break
        wait = _route_wait(route, route_arg, local_free, spillover)
        if wait is not None:
            result.placement_waits[row["id"]] = wait
            continue
        _wait_reached.append(row["id"])
        if row["id"] in result.placement_affinity:
            _affinity_reached.add(row["id"])
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee: when the dispatcher hits an
            # unassigned ready task and an operator-configured fallback
            # exists, persist the assignment and proceed. This removes the
            # dashboard footgun where a task created without an assignee
            # parks in 'ready' forever even though the operator's intent
            # ("default") was perfectly clear (#27145). Mutating the row
            # (not just the in-memory view) keeps diagnostics and the
            # board state consistent: the task is now legitimately owned
            # by ``kanban.default_assignee``, not "unassigned but secretly
            # routed".
            if _default_assignee and _default_assignee_resolved:
                # Dry-run: show what WOULD happen (auto-assign + spawn) without
                # mutating the DB. Real run: mutate the row + emit the
                # 'assigned' event so the board state matches what just happened.
                if not dry_run:
                    try:
                        with _kbc.write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET assignee = ? WHERE id = ? "
                                "AND (assignee IS NULL OR assignee = '')",
                                (_default_assignee, row["id"]),
                            )
                            _kb._append_event(
                                conn, row["id"], "assigned",
                                {
                                    "assignee": _default_assignee,
                                    "source": "kanban.default_assignee",
                                },
                            )
                    except Exception:
                        _kb._log.debug(
                            "kanban dispatch: failed to apply default_assignee=%r "
                            "to task %s",
                            _default_assignee, row["id"], exc_info=True,
                        )
                        result.skipped_unassigned.append(row["id"])
                        if route_first:
                            result.placement_waits[row["id"]] = _pr.UNASSIGNED
                        continue
                row_assignee = _default_assignee
                result.auto_assigned_default.append(row["id"])
            else:
                result.skipped_unassigned.append(row["id"])
                if route_first:
                    result.placement_waits[row["id"]] = _pr.UNASSIGNED
                continue
        # Skip ready tasks whose assignee is not a real Hermes profile.
        # `_default_spawn` invokes ``hermes -p <assignee>`` which fails
        # with "Profile 'X' does not exist" when the assignee names a
        # control-plane lane (e.g. an interactive Claude Code terminal
        # like ``orion-cc`` / ``orion-research``) rather than a Hermes
        # profile. Those task lanes are pulled by terminals via
        # ``claim_task`` directly and should NEVER auto-spawn — the
        # subprocess would crash on startup, get reaped as a zombie,
        # the task would loop back to ``ready`` on next tick, and we'd
        # burn CPU forever (#kanban-dispatcher-crash-loop 2026-05-05).
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(row_assignee):
            _note_skipped_nonspawnable(conn, row["id"], row_assignee, dry_run=dry_run)
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            continue
        # Per-profile concurrency cap (#21582): even if there's global
        # headroom, refuse to spawn for an assignee that's already at
        # its in-flight cap. Prevents one profile's local model / API
        # quota / browser pool from being overwhelmed by a fan-out
        # while the global max_in_progress / max_spawn caps still allow
        # work on OTHER profiles.
        _row_cap = _cap_for(row_assignee) if _per_profile_cap is not None else None
        if _row_cap is not None:
            current = _per_profile_running.get(row_assignee, 0)
            if current >= _row_cap:
                result.skipped_per_profile_capped.append(
                    (row["id"], row_assignee, current)
                )
                if route_first:
                    result.placement_waits[row["id"]] = (
                        f"{_pr.PER_PROFILE_CAP} ({row_assignee} {current}/{_row_cap})")
                continue
        if flagship_refused(row["id"], row_assignee):
            continue
        if invalid_pin_refused(row["id"]):
            continue
        # Respawn guard: refuse to re-spawn when useful work is already
        # in-flight/recent, or when the last failure is a deterministic
        # blocker (quota / auth). The guard defers the spawn this tick so
        # the task gets a chance to clear (rate limits often reset in
        # seconds-to-minutes); the existing consecutive_failures counter
        # still trips the auto-block circuit breaker after failure_limit
        # consecutive failures, so a persistent auth error eventually
        # blocks via the normal path rather than on first occurrence.
        guard_detail: dict = {}
        guard_reason = check_respawn_guard(
            conn, row["id"], pr_state_resolver=pr_state_resolver,
            detail=guard_detail,
        )
        stale_owners = guard_detail.pop("pr_owner_stale", None)
        if guard_reason is None and stale_owners:
            guard_reason = _kb._reassign_stale_pr_owners(
                conn, row["id"], stale_owners, guard_detail, dry_run=dry_run,
            )
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            if guard_detail:
                result.respawn_guard_details[row["id"]] = guard_detail
            # Emit an event so operators can see why the task was
            # skipped when reading `hermes kanban tail` — without
            # this the task appears stuck in ready with no diagnosis.
            if not dry_run:
                with _kbc.write_txn(conn):
                    _kb._append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason, **guard_detail},
                    )
            continue
        deferred, fallback_selection = provider_admission(row["id"], row_assignee)
        if deferred:
            continue
        if reported_collision_scopes is None and not collision_scope_load_failed:
            try:
                reported_collision_scopes = _kb._load_dispatch_collision_scopes(conn)
            except Exception:
                collision_scope_load_failed = True
                _kb._log.exception(
                    "KANBAN DISPATCH COLLISION CHECK FAILED OPEN: could not "
                    "load reported scopes; dispatch continues"
                )
        if collision_scope_load_failed:
            result.collision_check_failed.append(row["id"])
        else:
            try:
                _kb._check_dispatch_file_collisions(
                    conn,
                    result,
                    task_id=row["id"],
                    body=row["body"],
                    reported_scopes=reported_collision_scopes or {},
                    dry_run=dry_run,
                )
            except Exception:
                result.collision_check_failed.append(row["id"])
                _kb._log.exception(
                    "KANBAN DISPATCH COLLISION CHECK FAILED OPEN for %s; "
                    "dispatch continues",
                    row["id"],
                )
        if _kb._card_skills_refused(conn, row["id"], row_assignee, result, dry_run=dry_run):
            continue
        if _kb._workspace_admission_refused(conn, row["id"], result, board=board, dry_run=dry_run):
            continue
        placed_host = None
        _decision = None
        _rid = None
        _take_refusal = None
        _cap_only = _caps_out.get("only", {}).get(row["id"])
        # A browser join is cut for the host _browser_gate saw (the Studio for a
        # portable row): remote-first would move it and the lease refuses
        # BROWSER_JOIN_HOST_MOVED every tick, so a joined row keeps that host.
        _remote_first = (getattr(spillover, "default_remote", False)
                         and row["id"] not in result.browser_joins)
        if route in ("pin", "origin") or (route == "portable" and (
                not local_free or _cap_only or _remote_first)):
            # A refused take() NEVER falls through to a local spawn (I-11),
            # except a portable row under kanban.placement.default: remote,
            # which spawns locally as the LAST RESORT (spec v0.3 §3.1 step 2).
            # A host-bound portable card only lands on a host the reach
            # matrix proves (t_1e9ea7e9); None = any pool host.
            _only = None
            _job = None
            if route == "portable":
                from hermes_cli import kanban_worker_pool as _kwp_take
                try:
                    _skills = json.loads(row["skills"] or "[]") or []
                except (TypeError, ValueError, IndexError, KeyError):
                    _skills = []
                _skills = tuple(s for s in _skills if isinstance(s, str))
                _targets = _manifest_targets(_skills, row_assignee) if _skills else {}
                _only = _kwp_take.reach_allowed_hosts(
                    body=row["body"], skills=_skills, pool=spillover.config, targets=_targets)
                if getattr(getattr(spillover, "signal", None), "placement_table", None) is not None:
                    from hermes_cli import kanban_headroom as _kh_job
                    _job = _kh_job.job_for_row(conn, row, affinity_targets=_targets)
                # A body naming Studio-only resources lands only on a host
                # whose caps cover them (t_aea826a9); never widen ``_only``.
                _need_hosts = _kwp_take.needs_allowed_hosts(row["body"], spillover.config)
                if _need_hosts is not None:
                    _only = _need_hosts if _only is None else tuple(
                        h for h in _only if h.lower() in {n.lower() for n in _need_hosts})
                if _cap_only:
                    # Hosts advertising the card's capability (t_90887924),
                    # intersected with the reach proof when both apply.
                    _only = tuple(_cap_only) if _only is None else \
                        tuple(h for h in _only if h in _cap_only)
            _board_slug = _kb._normalize_board_slug(board) or _kb.get_current_board()
            # Scorer-backed takes (pins included) book a reservation for this card.
            _take_kw = {} if getattr(getattr(spillover, "signal", None), "placement_table", None) is None \
                else {"job_id": row["id"], **({"job": _job} if _job is not None else {})}
            if _only is None:
                placed_host = spillover.take(row_assignee, pin=route_arg if route in ("pin", "origin") else None,
                                             board=_board_slug, **_take_kw)
            else:
                placed_host = spillover.take(row_assignee, only=_only, board=_board_slug, **_take_kw)
            _decision = getattr(spillover, "last_decision", None)
            _rid = getattr(spillover, "last_reservation", None)
            if placed_host is None:
                _refusal = spillover.refusal or _pr.POOL_FULL
                if route == "portable":
                    _take_refusal = _refusal
                if not _last_resort_allowed(route, local_free, _cap_only, spillover):
                    result.placement_waits[row["id"]] = _refusal
                    continue
            # Layer 0b (t_ecfd5591, F14): never spawn a card whose ATTACHED skills need
            # an enforced capability the chosen host lacks (a stale route's belt).
            _gap = _attached_skill_gap(row, placed_host.name, _caps) if placed_host is not None else None
            if _gap is not None:
                spillover.release(placed_host, _rid)
                result.placement_waits[row["id"]] = f"requires:{_gap}+layer0b:{placed_host.name}"
                result.placement_caps.setdefault(row["id"], {})["layer0b"] = f"{placed_host.name} lacks {_gap}"
                if not dry_run:
                    with _kbc.write_txn(conn):
                        _kb._append_event(conn, row["id"], "placement_gate_refused",
                                          {"layer": "0b", "host": placed_host.name, "cap": _gap})
                continue
        if _take_refusal is not None and placed_host is None:
            result.take_refusals[row["id"]] = _take_refusal
        if dry_run:
            result.spawned.append((row["id"], row_assignee, ""))
            charge_pool(row["id"])
            spawned += 1
            if placed_host is not None:
                result.placed.append((row["id"], placed_host.name))
            else:
                local_spawned += 1
            # Increment per-profile counter even in dry_run so the cap
            # check sees the would-be spawn on subsequent iterations.
            # Without this, dry_run reports every task as spawnable and
            # under-reports the capped subset (#21582).
            if _per_profile_cap is not None and row_assignee:
                _per_profile_running[row_assignee] = (
                    _per_profile_running.get(row_assignee, 0) + 1
                )
            continue
        try:
            claimed = _kb.claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
        except _kph.PauseHeld as exc:
            _pause_held_at_claim(conn, result, exc)
            claimed = None
        if claimed is None:
            if placed_host is not None:
                spillover.release(placed_host, _rid)
            continue
        # Route precedence at spawn (both lanes): card pin > active lane
        # override > profile default gives the INTENDED route; then main's
        # capped-pool dispatch fallback (#943) replaces that route iff its
        # provider is capped. provider_admission() already probed health and
        # picked the fallback against this same lane-applied route, and the
        # flagship gate ran on the lane route (flagship_refused) and on the
        # fallback model (fallback_flagship_banned), so what spawns is gated.
        # Mutating the in-memory claimed Task is deliberate — neither the lane
        # route nor the fallback may be persisted onto the card, or it would
        # outlive its TTL / the capacity window as a permanent pin.
        route_source = _kb.apply_lane_model_override(
            claimed, _lane_override_for(claimed.assignee), now=_tick_now,
        )
        note_lane_route(claimed, route_source)
        route_source = route_source or (
            "card-override" if claimed.model_override else "profile-default")
        route_source = _kb._pin_route_source(claimed, route_source)
        if fallback_selection is not None:
            apply_dispatch_fallback(claimed, fallback_selection)
            route_source = fallback_route_source(route_source, fallback_selection)
        from hermes_cli.kanban_workspace_policy import (
            WorkspaceUnavailable, validate_mount, validate_persisted, validate_target,
        )
        try:
            protected = _kb._validate_workspace_admission(
                claimed, board=board, conn=conn,
            )
            resolved_branch_name = None
            if claimed.workspace_kind == "worktree":
                workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
            else:
                workspace = _kbw.resolve_workspace(claimed, board=board)
            if protected is not None:
                validate_mount(
                    protected.root, expected_mount=protected.mount_path,
                )
                validate_target(protected.root, workspace)
                validate_persisted(workspace)
        except WorkspaceUnavailable as exc:
            _kb._release_claim_for_workspace_refusal(
                conn, claimed.id, result, str(exc),
            )
            if placed_host is not None:
                spillover.release(placed_host, _rid)  # nothing launched: refund
            continue
        except Exception as exc:
            if placed_host is not None:
                spillover.release(placed_host, _rid)  # nothing launched: refund
            # A workspace anchor that can never resolve (bare repo, non-repo
            # path, missing default_workdir) is a capability wall: retrying it
            # re-runs the identical git probe against the identical path. Block
            # it on failure #1 with a reason that names the operator fix,
            # instead of burning the whole retry budget (7 cards x 3 spawns,
            # 2026-09-20).
            permanent = _kb._unusable_workspace_reason(exc)
            auto = _record_task_failure(
                conn, claimed.id, permanent or f"workspace: {exc}",
                outcome="spawn_failed", release_claim=True, end_run=True,
                failure_limit=failure_limit,
                force_trip=permanent is not None,
                block_kind="capability" if permanent else None,
            )
            # Record EVERY spawn failure (not just breaker trips) so a
            # pre-circuit-breaker stall is visible to health telemetry.
            result.spawn_failed.append(claimed.id)
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        _kbw.set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
        _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        if not _kb._claim_still_held(conn, claimed):
            _kb._abort_lost_claim_spawn(conn, claimed, None)
            if placed_host is not None:
                spillover.release(placed_host, _rid)  # nothing launched: refund
            continue
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        launched = False
        _joined = None  # (join, lease env) once the s7 join took a lease for this claim
        try:
            # Back-compat: older spawn_fn signatures accept only
            # (task, workspace). Test stubs in the suite rely on that.
            # Introspect the callable and pass `board` only when supported.
            import inspect
            spawn_kwargs: dict = {}
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    spawn_kwargs["board"] = board
                if placed_host is not None:
                    if "placement" not in sig.parameters and not any(
                        p.kind is inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    ):
                        raise RuntimeError(
                            "worker-host spillover: spawn_fn takes no placement"
                        )
                    spawn_kwargs["placement"] = placed_host
            except (TypeError, ValueError):
                if placed_host is not None:
                    raise
            _join = result.browser_joins.get(claimed.id)
            if _join is not None:
                _benv, _btok = ((None, "BROWSER_JOIN_SPAWN_UNSUPPORTED") if not _accepts_kw(_spawn, "browser_env")
                                else _browser_join_lease(conn, claimed, _join, placed_host))
                if _btok is None and not _kb._claim_still_held(conn, claimed):
                    # The relay can take minutes: a claim lost meanwhile is not ours to clean up (Prism 375e680b84c1).
                    _browser_release_joined(conn, claimed, _join, _benv, "claim_lost")
                    _kb._abort_lost_claim_spawn(conn, claimed, None)
                    if placed_host is not None:
                        spillover.release(placed_host, _rid)
                    continue
                if _btok is not None:
                    if placed_host is not None:
                        spillover.release(placed_host, _rid)  # nothing launched: refund
                    _browser_join_refused(conn, claimed, _btok, result, failure_limit)
                    continue
                spawn_kwargs["browser_env"] = _benv
                _joined = (_join, _benv)
            from hermes_cli.kanban_continuation import record_placement
            if not record_placement(conn, claimed.id, claimed.current_run_id,
                                    placed_host.name if placed_host else None):
                # The run no longer owns the card: nothing launched, refund.
                if _join is not None:
                    _browser_release_joined(conn, claimed, _join, spawn_kwargs.get("browser_env"), "claim_lost")
                if placed_host is not None:
                    spillover.release(placed_host, _rid)
                continue
            pid = _spawn(claimed, str(workspace), **spawn_kwargs)
            launched = True
            if placed_host is not None:
                band = getattr(spillover, "band", None)
                spill_reason = getattr(spillover, "spill_reason", None)
                reason = "pin" if route in ("pin", "origin") else (
                    "band_paused" if band == "paused" else
                    "band_spilling" if band == "spilling" else "overflow")
                probe = spillover.probe(placed_host.name)
                from hermes_cli import kanban_headroom as _kh_res
                # _rid was written by take() under the ledger lock BEFORE launch.
                _kb._append_event(
                    conn, claimed.id, _kb._kwh.PLACED_EVENT,
                    {"host": placed_host.name, "target": placed_host.target,
                     "workspace": str(workspace), "reason": reason,
                     "band": band, "spill_reason": spill_reason, "probe": probe,
                     "local_gate": str(spawn_paused) if spawn_paused else None,
                     "pid": pid,
                     **({_kh_res.RESERVATION_KEY: _rid} if _rid else {}),
                     **({_kh_res.RESERVATION_VIA_KEY: _kh_res.VIA_ALLOC_CLIENT}
                        if _rid and spillover.reservation_via(_rid) else {}),
                     **({"rule": _decision.rule} if _rid and _decision is not None else {})},
                    run_id=claimed.current_run_id,
                )
                _kb._log.info(
                    "kanban dispatch: placed %s (%s) on worker host %s pid=%s "
                    "band=%s spill_reason=%s reason=%s probe=%s; local gate: %s",
                    claimed.id, claimed.assignee, placed_host.name, pid,
                    band, spill_reason, reason, probe, spawn_paused,
                )
            placement_record = _placement_payload(
                run_id=claimed.current_run_id, route=route, arg=route_arg,
                rule=row_rules.get(claimed.id), placed_host=placed_host, plan=spillover,
                entry=entry, gate=tick_gate, take_refusal=_take_refusal,
            )
            if pid and not _set_worker_pid(
                conn, claimed.id, int(pid), run_id=claimed.current_run_id,
                pool=admitted_routes.get(claimed.id), placement=placement_record,
            ):
                _kb._abort_lost_claim_spawn(conn, claimed, int(pid))
                if _joined is not None:
                    _browser_release_joined(conn, claimed, *_joined, "claim_lost_after_spawn")
                continue
            if not pid:
                # No pid reported (no ``spawned`` event): still one record.
                with _kbc.write_txn(conn):
                    _kb._append_event(conn, claimed.id, _pr.PLACEMENT_EVENT, placement_record,
                                      run_id=claimed.current_run_id)
            # Worker-lifecycle observer (RFC #58548): fires AFTER spawn_fn
            # returned and the PID (when reported) is durably persisted,
            # per the RFC timing contract. Best-effort — can never break
            # the dispatch loop.
            _kb._fire_worker_spawned_hook(
                conn, claimed, str(workspace), pid, board=board,
            )
            # NOTE: we intentionally do NOT reset consecutive_failures
            # here. A successful spawn proves the worker can start but
            # doesn't prove the run will succeed. Under unified
            # failure counting, resetting on spawn would let a task
            # that keeps timing out after spawn loop forever. The
            # counter is cleared only on successful completion (see
            # complete_task).
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            # placed is a subset of spawned: a placement the loop then aborted
            # never counts as one (the gateway books local spawns by id).
            if placed_host is not None:
                result.placed.append((claimed.id, placed_host.name))
            else:
                local_spawned += 1
            result.spawn_routes[claimed.id] = _kb.effective_worker_route(claimed)
            result.spawn_route_sources[claimed.id] = route_source
            charge_pool(claimed.id, claimed.current_run_id)
            spawned += 1
            # Track the new in-flight count for this profile so later
            # iterations in this same tick respect the per-profile cap
            # (#21582). Subsequent ticks re-query from the DB.
            if _per_profile_cap is not None and claimed.assignee:
                _per_profile_running[claimed.assignee] = (
                    _per_profile_running.get(claimed.assignee, 0) + 1
                )
        except Exception as exc:
            if placed_host is not None and not launched:
                spillover.release(placed_host, _rid)  # pre-launch failure: refund
            if _joined is not None and not launched:
                _browser_release_joined(conn, claimed, *_joined, "spawn_failed")  # Prism d598b2a71104
            # A host that cannot place the worker (no restart-safe scope,
            # eb28bc1bf7; no board mount on a pool host, t_9fb4d477) is not
            # the card's failure: the run is closed with ``infrastructure:
            # true`` and the breaker is never charged.
            host_fault = _host_fault(exc)
            infrastructure = host_fault is not None
            if infrastructure:
                _kb._log.warning(
                    "kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s",
                    claimed.id, exc,
                )
            auto = _record_task_failure(
                conn, claimed.id, str(exc),
                outcome="spawn_failed", release_claim=True, end_run=True,
                failure_limit=failure_limit, infrastructure=infrastructure,
                run_metadata_extra=host_fault or None,
            )
            # Record EVERY spawn failure (not just breaker trips) so a
            # pre-circuit-breaker stall is visible to health telemetry.
            result.spawn_failed.append(claimed.id)
            if auto:
                result.auto_blocked.append(claimed.id)

    pr_state_resolver.finish_tick(scan_complete=ready_scan_complete)
    if _affinity_evaluated and not dry_run:
        from hermes_cli.kanban_host_affinity import record_affinity_events
        from hermes_cli import kanban_host_capabilities as _khc
        _cap_comments = _khc.pending_comments(conn, result, _caps, _caps_out)
        record_affinity_events(conn, result, _affinity_evaluated, _affinity_reached)
        # The pin-lacks park blocks the card: ownership first, as for access contradictions.
        _drop_foreign(conn, _caps_out.get("pin_lacks") or {}, default_assignee)
        _khc.record_capability_events(conn, result, _caps, _caps_out, _affinity_evaluated,
                                      _cap_comments)
    _park_access_contradictions(conn, result, dry_run, default_assignee)
    _record_placement_waits(conn, result, row_route, dry_run, _wait_reached)
    result.rows_routed = len(ready_rows)
    result.portable_waiting = sum(
        1 for tid in result.placement_waits
        if row_route.get(tid, ("local", None))[0] == "portable")
    result.portable_waiting_with_room = sum(
        1 for tid in _portable_room
        if tid in result.placement_waits and _i0_counts(result.placement_waits[tid]))
    if not dry_run and result.placement_waits:
        try:
            from hermes_cli.kanban_drained_pin import notify_drained_pins
            notify_drained_pins(conn, board, result.placement_waits, int(time.time()))
        except Exception as exc:  # never break a dispatcher tick
            _kb._log.warning("kanban drained-pin notify failed: %s", exc)
    if not dry_run and result.unknown_pins:
        try:
            from hermes_cli.kanban_unknown_pin import comment_unknown_pins
            comment_unknown_pins(conn, result.unknown_pins)
        except Exception as exc:  # never break a dispatcher tick
            _kb._log.warning("kanban unknown-pin comment failed: %s", exc)
    _tick_routes(result, ready_rows, row_route, row_rules, spillover is not None, dry_run)

    # ---- review column dispatch ----
    # Review tasks are tasks that a worker moved to 'review' after
    # creating a PR.  The dispatcher spawns a review agent (loading
    # sdlc-review skill) that verifies the candidate and either approves
    # (→ done) or requests changes (→ ready/todo for the implementer).
    #
    # Same concurrency model as ready dispatch: review spawns count
    # against max_spawn alongside ready tasks, so the total number of
    # running workers stays bounded.
    # Auto-dispatch is enabled by default because Hermes bundles the
    # ``sdlc-review`` skill and reviewer workers can now approve, request
    # changes without block-loop accounting, or escalate a genuine blocker.
    # Human-only boards can disable it with ``kanban.review_dispatch``.
    #
    # ``review_rows`` was enumerated before the ready loop; when it is
    # non-empty the ready loop ran against ``ready_budget`` (one slot held
    # back) so this lane cannot be permanently starved by a sustained
    # ready backlog. The review loop itself still checks the FULL shared
    # ``spawn_budget`` — the reservation caps the ready lane, it does not
    # grant the review lane extra capacity. Same for the per-pool hold:
    # release it so reviewers are judged on the pool's real budget.
    review_pool_reserve.clear()
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        _review_origin_host = None
        if row['origin_host'] and _kcont_lost.mode(_root_kanban_cfg()) == 'enforce':
            if spillover is None:
                result.placement_waits[row['id']] = 'origin_host_absent'
                continue
            _rr = _kcont_lost.origin_route(conn, row, spillover.config, config=_root_kanban_cfg(),
                                           states=spillover.continuation_states, caps=_caps, dry_run=dry_run)
            if _rr and _rr[0] == 'wait':
                result.placement_waits[row['id']] = _rr[1]
                continue
            if _rr and _rr[0] == 'origin':
                _review_origin_host = next((h for h in spillover.config.pool_hosts if h.name == _rr[1]), None)
                _refusal = spillover.pin_host_refusal(_rr[1])
                if _review_origin_host is None or _refusal:
                    result.placement_waits[row['id']] = _refusal or 'origin_host_absent'
                    continue
        if _review_origin_host is None and local_budget is not None and local_spawned >= local_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(row["assignee"]):
            _note_skipped_nonspawnable(conn, row["id"], row["assignee"], dry_run=dry_run)
            result.skipped_nonspawnable.append(row["id"])
            continue
        _review_cap = _cap_for(row["assignee"]) if _per_profile_cap is not None else None
        if _review_cap is not None:
            current = _per_profile_running.get(row["assignee"], 0)
            if current >= _review_cap:
                result.skipped_per_profile_capped.append(
                    (row["id"], row["assignee"], current)
                )
                continue
        if flagship_refused(row["id"], row["assignee"]):
            continue
        if invalid_pin_refused(row["id"]):
            continue
        guard_detail = {}
        guard_reason = check_respawn_guard(
            conn, row["id"], lane="review", detail=guard_detail,
        )
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            if guard_detail:
                result.respawn_guard_details[row["id"]] = guard_detail
            if not dry_run:
                with _kbc.write_txn(conn):
                    _kb._append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason, **guard_detail},
                    )
            continue
        deferred, fallback_selection = provider_admission(row["id"], row["assignee"])
        if deferred:
            continue
        if _kb._card_skills_refused(conn, row["id"], row["assignee"], result, dry_run=dry_run):
            continue
        if _kb._workspace_admission_refused(conn, row["id"], result, board=board, dry_run=dry_run):
            continue
        if _review_origin_host is not None:
            _review_origin_host = spillover.take(row['assignee'], pin=_review_origin_host.name)
            if _review_origin_host is None:
                result.placement_waits[row['id']] = spillover.refusal
                continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            charge_pool(row["id"])
            spawned += 1
            local_spawned += 0 if _review_origin_host else 1
            if _per_profile_cap is not None:
                _per_profile_running[row["assignee"]] = (
                    _per_profile_running.get(row["assignee"], 0) + 1
                )
            continue
        try:
            claimed = _kb.claim_review_task(conn, row["id"], ttl_seconds=ttl_seconds)
        except _kph.PauseHeld as exc:
            _pause_held_at_claim(conn, result, exc)
            if _review_origin_host is not None:
                spillover.release(_review_origin_host, getattr(spillover, "last_reservation", None))
            continue
        if claimed is None:
            continue
        # Review spawns are workers too: a capacity window that re-routes the
        # ready lane but silently leaves reviewers on a capped provider would
        # strand exactly the lane that unblocks everything else. Same
        # precedence as the ready path: lane first, then capped-pool fallback.
        review_route_source = _kb.apply_lane_model_override(
            claimed, _lane_override_for(claimed.assignee), now=_tick_now,
        )
        note_lane_route(claimed, review_route_source)
        review_route_source = review_route_source or (
            "card-override" if claimed.model_override else "profile-default")
        review_route_source = _kb._pin_route_source(claimed, review_route_source)
        if fallback_selection is not None:
            apply_dispatch_fallback(claimed, fallback_selection)
            review_route_source = fallback_route_source(review_route_source, fallback_selection)
        from hermes_cli.kanban_workspace_policy import (
            WorkspaceUnavailable, validate_mount, validate_persisted, validate_target,
        )
        try:
            protected = _kb._validate_workspace_admission(
                claimed, board=board, conn=conn,
            )
            resolved_branch_name = None
            if claimed.workspace_kind == "worktree":
                workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
            else:
                workspace = _kbw.resolve_workspace(claimed, board=board)
            if protected is not None:
                validate_mount(
                    protected.root, expected_mount=protected.mount_path,
                )
                validate_target(protected.root, workspace)
                validate_persisted(workspace)
        except WorkspaceUnavailable as exc:
            _kb._release_claim_for_workspace_refusal(
                conn, claimed.id, result, str(exc),
            )
            continue
        except Exception as exc:
            # A workspace anchor that can never resolve (bare repo, non-repo
            # path, missing default_workdir) is a capability wall: retrying it
            # re-runs the identical git probe against the identical path. Block
            # it on failure #1 with a reason that names the operator fix,
            # instead of burning the whole retry budget (7 cards x 3 spawns,
            # 2026-09-20).
            permanent = _kb._unusable_workspace_reason(exc)
            auto = _record_task_failure(
                conn, claimed.id, permanent or f"workspace: {exc}",
                outcome="spawn_failed", release_claim=True, end_run=True,
                failure_limit=failure_limit,
                force_trip=permanent is not None,
                block_kind="capability" if permanent else None,
            )
            # Record EVERY spawn failure (not just breaker trips) so a
            # pre-circuit-breaker stall is visible to health telemetry.
            result.spawn_failed.append(claimed.id)
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        _kbw.set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
        _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        # Force-load the sdlc-review skill for review agents — it carries
        # the review logic (AC verification, merge, etc.). The mandatory
        # kanban lifecycle is already injected into every worker's system
        # prompt via KANBAN_GUIDANCE, so this is the only extra skill the
        # review agent needs.
        claimed.skills = list(
            dict.fromkeys([*(claimed.skills or []), "sdlc-review"])
        )
        if not _kb._claim_still_held(conn, claimed):
            _kb._abort_lost_claim_spawn(conn, claimed, None)
            continue
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            from hermes_cli.kanban_continuation import record_placement
            if not record_placement(conn, claimed.id, claimed.current_run_id,
                                    _review_origin_host.name if _review_origin_host else None):
                continue
            if _review_origin_host:
                _kb._kwh.prepare_remote_workspace(_review_origin_host, str(workspace))
            import inspect
            try:
                sig = inspect.signature(_spawn)
                kwargs = {'placement': _review_origin_host} if _review_origin_host else {}
                if "board" in sig.parameters:
                    kwargs['board'] = board
                pid = _spawn(claimed, str(workspace), **kwargs)
            except (TypeError, ValueError):
                if _review_origin_host:
                    raise
                pid = _spawn(claimed, str(workspace))
            if pid and not _set_worker_pid(
                conn, claimed.id, int(pid), run_id=claimed.current_run_id,
                pool=admitted_routes.get(claimed.id),
            ):
                _kb._abort_lost_claim_spawn(conn, claimed, int(pid))
                continue
            # Worker-lifecycle observer (RFC #58548): same contract as the
            # ready-lane fire above — after spawn + PID persistence.
            _kb._fire_worker_spawned_hook(
                conn, claimed, str(workspace), pid, board=board,
            )
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            result.spawn_routes[claimed.id] = _kb.effective_worker_route(claimed)
            result.spawn_route_sources[claimed.id] = review_route_source
            charge_pool(claimed.id, claimed.current_run_id)
            spawned += 1
            local_spawned += 1
            if _per_profile_cap is not None and claimed.assignee:
                _per_profile_running[claimed.assignee] = (
                    _per_profile_running.get(claimed.assignee, 0) + 1
                )
        except Exception as exc:
            # A host that cannot place the worker (no restart-safe scope,
            # eb28bc1bf7; no board mount on a pool host, t_9fb4d477) is not
            # the card's failure: the run is closed with ``infrastructure:
            # true`` and the breaker is never charged.
            host_fault = _host_fault(exc)
            infrastructure = host_fault is not None
            if infrastructure:
                _kb._log.warning(
                    "kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s",
                    claimed.id, exc,
                )
            auto = _record_task_failure(
                conn, claimed.id, str(exc),
                outcome="spawn_failed", release_claim=True, end_run=True,
                failure_limit=failure_limit, infrastructure=infrastructure,
                run_metadata_extra=host_fault or None,
            )
            # Record EVERY spawn failure (not just breaker trips) so a
            # pre-circuit-breaker stall is visible to health telemetry.
            result.spawn_failed.append(claimed.id)
            if auto:
                result.auto_blocked.append(claimed.id)
    if not dry_run:
        try:
            starved = find_priority_starved(conn, now=_tick_now)
            result.priority_starved = [
                (h["task_id"], h["ready_seconds"], h["reason"]) for h in starved
            ]
            if starved:
                _notify_priority_starved(conn, board, starved)
        except Exception as exc:  # never break a dispatcher tick
            _kb._log.warning("kanban priority-starvation check failed: %s", exc)
    return result


def _note_skipped_nonspawnable(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> None:
    """Record ``skipped_nonspawnable`` on the card ONCE (not once per tick,
    #122422): only when the newest event is not already the same skip."""
    if dry_run:
        return
    with _kbc.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if (last is None or last["kind"] != "skipped_nonspawnable"
                or last["payload"] != _kb._json_or_null({"assignee": assignee})):
            _kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        # Same invariant as gc_worker_logs: the deletion audit is never
        # removed by routine log maintenance. Today this function is only
        # ever called on `<logs>/<task_id>.log` (kanban_db.py:14287), so
        # this is unreachable -- but with backup_count=0 it does a bare
        # unlink(), so if a future caller ever points it at the audit the
        # record dies silently. Guarding the function is cheaper than
        # trusting every future call site (card t_63fb42f9, round 7).
        if _kb.is_deletion_audit_path(log_path):
            return
        if log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(
            backup_count,
            DEFAULT_LOG_BACKUP_COUNT,
            minimum=0,
        )
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _propagate_module_import_root(cmd: list[str], env: dict[str, str]) -> None:
    """Put the running install's package root on a module-form worker's path.

    ``_resolve_hermes_argv`` proves ``hermes_cli`` importable in THIS process,
    where a store-python shim has the repo root on ``sys.path`` in-process;
    the spawned child runs the bare ``sys.executable`` from the task workspace
    with a scrubbed ``PYTHONPATH`` and cannot import the package the parent
    just proved importable — it dies before any work and the board
    auto-blocks (#122299, #122487, #122500). Same-interpreter child, so the
    root is version-safe to propagate; ``hermes_cli.main``'s own bootstrap
    then owns dependency activation as usual. A resolved shim path owns its
    imports and is left alone. Same pin cron's external worker uses (#112729).
    """
    if cmd[1:3] != ["-m", "hermes_cli.main"]:
        return
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath

    pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[1])


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str, *, bind_home: bool = True):
    """Bind an assigned profile's runtime scope (secrets + terminal policy, optionally home) for
    one dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    ``bind_home=False`` for the spawn-env build: which variables may cross into a child is the
    DISPATCHER's ``terminal.env_passthrough`` policy (#109494, read through the home override) —
    only their VALUES come from the assignee's scope, so that branch binds the secret scope alone.
    Toolset resolution binds the home and the terminal policy, as it always has.

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tui_gateway.launch_profile_policy import launch_secret_scope, launch_terminal_env

    home = Path(hermes_home)
    is_launch_home = str(home.resolve()) == str(Path(get_process_hermes_home()).resolve())
    home_token = secret_token = terminal_token = None
    try:
        home_token = set_hermes_home_override(str(home)) if bind_home else None
        secret_token = set_secret_scope(
            launch_secret_scope(home) if is_launch_home else build_profile_secret_scope(home),
            profile_home=None if is_launch_home else str(home))
        terminal_token = install_profile_terminal_scope(
            home, env_overlay=launch_terminal_env() if is_launch_home else None) if bind_home else None
        yield
    finally:
        if terminal_token is not None:
            reset_terminal_scope(terminal_token)
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None
_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    # goal_mode rides the same `-q` path: cli.py runs the judge loop there too, so the
    # worker log keeps its live tool feed (forcing -Q blanked it).
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
    placement=None,
    browser_env: Optional[Mapping[str, str]] = None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the spawned child's PID so the dispatcher can detect crashes
    before the claim TTL expires. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    ``board`` pins the child's kanban context to that board: the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root env
    vars all resolve to the same board the dispatcher claimed the task
    from. Workers cannot accidentally see other boards.
    """
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name

    profile_arg = normalize_profile_name(task.assignee)

    prompt = f"work kanban task {task.id}"
    # Build the worker env through the shared subprocess-env seam (upstream
    # bc0a42fd96 / c718267ef3): a foreign-profile worker never inherits the
    # dispatcher's credentials, and the profile home is bound explicitly.
    from hermes_cli.profiles import resolve_profile_env
    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home, build_subprocess_env, strip_launch_profile_env
    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        profile_home = None
    routed = bool(profile_home) and _is_routed_home(profile_home)
    with (_worker_profile_scope(profile_home, bind_home=False) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # The dispatcher is detached from every conversation. Its worker must never
    # inherit routing mirrored by a previous gateway turn, even before the first
    # session binds ContextVars in this process.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)
    # GitHub lane is decided by the worker's PROFILE in the gh shim (spec D3); an inherited lane
    # (e.g. a dispatcher launched from a laned script) must not ride into the worker.
    env.pop("HERMES_GH_LANE", None)
    # The dispatcher's own agent.process_env_files overlay was resolved for ITS
    # profile (e.g. no bot git identity); the worker re-sources the files for
    # its own profile at startup, so hand it the pre-overlay env (t_45c11886).
    from hermes_cli.process_env_files import strip_overlay
    strip_overlay(env)

    # A dispatcher-spawned worker is its OWN single-session process, NOT the
    # gateway. The gateway sets _HERMES_GATEWAY=1 process-wide; copying it into
    # the worker (via dict(os.environ)) would misclassify the worker as "gateway"
    # and suppress its single-process os.environ session writes (the worker's
    # own session-id stamping relies on them). Pop it here — mirrors the restart
    # watcher (gateway/run.py) which pops it for the same reason.
    env.pop("_HERMES_GATEWAY", None)
    # INV-A7: the operator token is presented inline by a human operator and
    # must never ride into a worker by inheritance from the dispatcher's env.
    env.pop(_kb.OPERATOR_TOKEN_ENV, None)

    # A worker imports the runtime tree its argv's venv points at — never a
    # dispatcher's PYTHONPATH/PYTHONHOME. sys.path beats the venv's editable
    # finder, so a gateway pinned to a side-by-side release via its plist
    # (registry-pins v0.2) silently ran every worker on that release, and
    # runtime deploys never reached workers (t_e8c867d3: #1075 invisible,
    # 0 review_skipped). Mirrors the `hermes` shim's load-bearing unset,
    # which this direct venv exec bypasses.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, `env = dict(os.environ)` copies only the parent's
    # env, and when the child process starts `hermes -p <name>` the
    # _apply_profile_override() runs *before* hermes_constants is imported.
    # If HERMES_HOME is absent from the child's env, get_hermes_home() falls
    # back to Path.home() / ".hermes" (the DEFAULT profile root), ignoring the
    # profile-specific config entirely.  Fixes profile-scoped fallback_providers
    # being invisible to kanban workers.
    if profile_home:
        env["HERMES_HOME"] = profile_home
        strip_launch_profile_env(env, profile_home)
    # Hermeticity anchor (card t_43d5c42d / 2026-07-24 WAL incident). Export a
    # correct HERMES_REAL_HOME (and apply the HOME contract) BEFORE spawning,
    # exactly as every other subprocess spawn does (tools/environments/local.py).
    # We intentionally do NOT override HERMES_HOME with a tempdir here: a
    # dispatcher worker is a real production agent that must persist to the
    # profile's real state.db. But if the worker's TASK invokes the test suite,
    # tests/conftest.py re-hermeticizes with its own temp HERMES_HOME and its
    # DEFAULT_DB_PATH canary compares the resolved path against
    # HERMES_REAL_HOME. Without this export the canary falls back to
    # Path.home()/".hermes", which is wrong under container / profile-home /
    # custom-HOME layouts. Setting the anchor keeps that canary honest.
    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    # Bind this grant to the ONE process we are about to spawn. Popen cannot
    # know the child's pid before it execs, so stamp the single-use sentinel;
    # the child's CLI startup rewrites it to its own pid. Without this, every
    # ordinary subprocess the worker later launches (a nested `hermes chat`,
    # a script that shells out) inherits HERMES_KANBAN_* and is indistinguishable
    # from the worker itself — one such child completed its parent's card with
    # an unrelated summary while the owner was still running (2026-08-12).
    from agent.delegation_context import (
        KANBAN_OWNER_PID_ENV,
        KANBAN_OWNER_PID_PENDING,
    )
    env[KANBAN_OWNER_PID_ENV] = KANBAN_OWNER_PID_PENDING
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # A worker never writes the host's gateway service definitions: a scratch E2E gateway a worker
    # started rewrote ACE-AI's real unit on boot (t_8749a807). Inherited by every child it spawns.
    from hermes_cli.gateway_service_owner import INSTALL_DISABLED_ENV
    env[INSTALL_DISABLED_ENV] = "1"
    # Tag the worker's session so it lands in state.db as `kanban`, not as an
    # untitled `cli` row. A worker is a dispatcher-owned run whose transcript is
    # read on the board and in `hermes kanban log` — it is not a conversation
    # the user started, so every session-browsing surface (desktop sidebar, TUI
    # resume picker, session_search) filters it out by source. Without this the
    # sidebar renders one row per attempt, labeled with the worker's own prompt
    # ("work kanban task t_…").
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and
    # context-file loader anchor on the workspace, not whatever cwd the
    # dispatching gateway happened to export. The worker subprocess is already
    # launched with cwd=workspace, but TERMINAL_CWD takes precedence over the
    # process cwd in both file_tools._resolve_base_dir (#41312 — relative
    # write_file paths were landing in the gateway user's home) and
    # build_context_files_prompt (#34619 — workers loaded the dispatching
    # gateway's AGENTS.md instead of the task's). Setting it to the workspace
    # fixes both: the workspace is where the task's work actually happens.
    # Only pin a real, absolute directory — file_tools rejects relative /
    # sentinel TERMINAL_CWD values, so a non-dir workspace must NOT be set
    # here (leave the inherited value rather than write a meaningless one).
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    # The card's harness brain as claimed (t_a8f335c5; "" = none). The lane
    # runner re-reads tasks.brain and stops ``model_diverged`` if it moved
    # between this claim and its own read, as it does for model and effort.
    env[_kb.CARD_BRAIN_ENV] = getattr(task, "brain", None) or ""
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
        from hermes_cli.kanban_worker_exit import exit_file
        status_path = exit_file(_kb.kanban_db_path(board=board), task.id, task.current_run_id)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        env["HERMES_KANBAN_EXIT_FILE"] = str(status_path)
    else:
        env.pop("HERMES_KANBAN_EXIT_FILE", None)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # The card pin as CLAIMED (t_a30417c3): the worker's pin snapshot must
    # not read the mutable row later, where a next-dispatch ``set-model``
    # landing between this spawn and worker startup would become this run's
    # pin. Only the claim-time row pin, never the lane/rung route. Popped when
    # absent so a dispatcher running inside a worker never leaks its own.
    from hermes_cli.kanban_worker_route import CLAIMED_CARD_PIN_ENV
    if task.claimed_card_pin is not None:
        _pin_model, _pin_provider = task.claimed_card_pin
        env[CLAIMED_CARD_PIN_ENV] = _kb.json.dumps(
            {"model": _pin_model, "provider": _pin_provider})
    else:
        env.pop(CLAIMED_CARD_PIN_ENV, None)
    # Goal-loop mode: the worker reads these and wraps its run in the
    # Ralph-style /goal judge loop (see cli.py quiet-mode path). Only set
    # when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`hermes -p <name>`
    # rewrites HERMES_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `get_default_hermes_root()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — the final defense-in-depth pin. If the worker ever
    # resolves kanban paths without the DB / workspaces env vars, the
    # board slug still forces it to the right directory.
    resolved_board = _kb._normalize_board_slug(board) or _kb.get_current_board()
    env["HERMES_KANBAN_BOARD"] = resolved_board
    # HERMES_PROFILE is the author the kanban_comment tool defaults to.
    # `hermes -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task. A dispatcher
    # launched from an agent's shell carries the delegate_task descendant fence itself; the
    # worker it grants a task to must not (an inherited marker fences the worker's own
    # heartbeat + handoff — upstream, test_dispatcher_grants_only_the_assigned_worker_scope).
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)

    # A worker must NEVER boot the interactive TUI: an inherited HERMES_TUI=1
    # or a `display.interface: tui` in the profile's config would send the
    # quiet chat run into the Ink TUI, whose no-TTY bail-out exits 0 without
    # doing the task → "protocol violation" on every attempt. `--cli` is the
    # highest-precedence interface override; dropping the env var covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    # Worker-pool placement (kanban_worker_pool): same process, same grant,
    # tools on the worker host. Never inherit a placement from our own env.
    env.pop(_kb._kwh.PLACEMENT_ENV, None)
    # Browser lease (s7 join, t_2828292d): only the lease THIS spawn took, never one inherited from our env.
    from hermes_cli.kanban_browser_lease import LEASE_ENV_NAMES
    for _name in LEASE_ENV_NAMES:
        env.pop(_name, None)
    if browser_env:
        env.update({k: v for k, v in browser_env.items() if k in LEASE_ENV_NAMES})
    # Only a POOL placement's mkdir failure is a (host, board) fault the
    # planner can route around; the local-uid hop below is not (t_9fb4d477).
    pool_board = resolved_board if placement is not None else None
    if placement is None:
        # kanban.local_worker_uid (Q8 (a), t_405acdf0): a local worker's tools
        # run as a dedicated non-sudo uid over ssh to localhost. Unset = 502.
        from hermes_cli import kanban_local_worker_uid as _klu
        _local_user = _klu.configured_user()
        if _local_user:
            placement = _klu.local_host(_local_user)
    # The capability modes the worker's rail gates honour (t_8c474c2e, F5):
    # workers never read config.yaml for this, the dispatcher decides.
    from hermes_cli import kanban_host_capabilities as _khc
    try:
        env[_khc.CAPS_ENV] = _khc.spawn_env(_kb.kanban_home() / "fleet", _root_kanban_cfg())
    except Exception:
        env[_khc.CAPS_ENV] = _khc.env_value(_khc.WARN, ())
    if placement is not None:
        _kb._kwh.prepare_remote_workspace(placement, workspace, board=pool_board)
        _kb._kwh.apply_placement(
            env, placement, workspace, run_id=task.current_run_id,
            log_file=str(_kb.worker_log_path(task.id, board=board)))
        # Layer 0 inputs (t_ecfd5591, F1): the card's hard requirements and the
        # route hash, so the worker can tell a stale route at boot.
        try:
            from hermes_cli import kanban_placement_failfast as _pff
            _pff.stamp_placement(env, task, _kb.kanban_home() / "fleet")
        except Exception:
            _kb._log.debug("kanban dispatch: placement record stamp failed", exc_info=True)

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        "--cli",
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
    ]
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    # Bundled kanban-worker skill — only inject when it actually resolves for
    # the home the worker runs under (preloading a missing skill is fatal at
    # CLI startup; the lifecycle contract still ships via KANBAN_GUIDANCE).
    if _kb._kanban_worker_skill_available(env.get("HERMES_HOME")):
        cmd.extend(["--skills", "kanban-worker"])
    # Per-task force-loaded skills, one `--skills X` pair each. Dedupe against
    # the built-in so we don't double-load kanban-worker.
    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                cmd.extend(["--skills", sk])
    if task.model_override:
        # Provider resolution precedence: an explicit provider_override wins;
        # otherwise accept the fork's ``provider/model`` partition form encoded
        # in model_override itself.
        # Emission order is ``-m <model> --provider <name>`` — the shape the
        # module docstrings above already document ("passed to the worker as
        # ``-m <model> [--provider <name>]``") and the one the dispatcher's
        # contract test asserts (``--provider`` immediately after the model
        # value). argparse is order-insensitive, so this is presentation only;
        # both flags still travel as an adjacent pair, keeping ``ps`` output
        # readable and the pairing greppable.
        from hermes_cli.kanban_provider_health import model_override
        model, provider = model_override(task)
        # foreign_lane.shim_model_cap (model-switching spec 4.4 follow-up): a
        # foreign-lane shim follows the card's model (Q-M1 = B), so an Opus
        # card would run the shim on Opus too. When the profile sets a cap and
        # the card's model ranks above it, spawn the shim at the cap. The
        # harness still reads the card's model from the board, so only the
        # shim moves. Unset = today's behaviour.
        model, capped_from = _kb._apply_shim_model_cap(
            model, _kb._read_shim_model_cap(env.get("HERMES_HOME")), provider,
        )
        if capped_from:
            env[_kb.SHIM_MODEL_CAPPED_FROM_ENV] = capped_from
            _kb._log.info(
                "kanban spawn task=%s assignee=%s shim_model=%s shim_model_capped_from=%s",
                task.id, profile_arg, model, capped_from,
            )
        else:
            env.pop(_kb.SHIM_MODEL_CAPPED_FROM_ENV, None)
        cmd.extend(["-m", model])
        if provider:
            cmd.extend(["--provider", provider])
        # Structured spawn line so per-task model overrides are auditable
        # post-hoc ("why did this task cost Opus money"). Only emitted when
        # an override is actually set — a cleared/no-override task logs
        # nothing here, so a stale line never misattributes spend.
        _kb._log.info(
            "kanban spawn task=%s assignee=%s model_override=%s",
            task.id,
            profile_arg,
            task.model_override,
        )
    # Per-task thinking depth. Independent of the model override — a task can
    # run the profile's own model at a different depth — so this is its own
    # branch, not a nested one.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(env.get("HERMES_HOME"))
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    # Every worker needs the result-aware exit path, not only goal-mode runs.
    cmd.append("-Q")
    # Native foreign lane (harness-parity spec 9.6): a profile that sets
    # ``foreign_lane.worker_command`` is worked by a no-LLM runner that execs
    # the lane and makes its receipt's one board call. Same env, same owner
    # grant, same log; only the argv differs. Unset keeps the shim above.
    from hermes_cli.kanban_native_worker import worker_command as _native_worker_command
    if _native_worker_command(env.get("HERMES_HOME")) is not None:
        cmd = _kb._native_worker_argv(task, env.get("HERMES_HOME"))
        # Pin the runner to the tree THIS dispatcher imports, so the code that
        # chose the native path is the code that runs it. The runner drops it
        # again before it starts the lane.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    # Redirect output to a per-task log under <board-root>/logs/.
    # Anchored at the board root (not the shared kanban root), so
    # `hermes kanban log` on a specific board reads its own file and
    # logs don't collide across boards that happen to share task ids.
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    # Segment the append-mode log so the reaper can fingerprint THIS run's
    # output alone. Stamped AFTER rotation (a rotated log starts fresh, and
    # its first run still needs a boundary) and BEFORE Popen, so everything
    # the child writes lands after it.
    _kb._stamp_worker_log_run_boundary(log_path)

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")
    # Background CPU priority for the worker gateway. Niceness is inherited
    # across fork/exec, so this covers every process the worker later spawns
    # (terminal-tool children included) without policing them individually —
    # the seam the 2026-09-20 load-538 incident escaped through.
    cpu_priority_mode, cpu_nice = _kb.worker_cpu_priority_config()
    priority_preexec = _kb._build_worker_priority_preexec(cpu_nice)
    # Process-table ceiling for the whole worker tree (t_368e9873): a
    # self-respawning scratch script held 9,6xx of the uid's 10,666 slots on
    # 2026-10-01 and every fork on the host failed. RLIMIT_NPROC counts the
    # uid, so the worker tree stops at a reserve the gateways still own.
    from hermes_cli import kanban_proc_bounds as _kpb
    nproc_limit = _kpb.resolve_worker_nproc_limit()
    priority_preexec = _kpb.chain_preexec(
        priority_preexec, _kpb.worker_nproc_preexec_limit(nproc_limit),
    )
    # macOS: nice alone leaves the worker in the gateway's own QoS class and
    # I/O tier, so worker pytest/git storms still starve the resident gateway
    # (t_14c130aa). Clamp QoS via exec-form taskpolicy; it keeps the pid.
    darwin_prefix = _kb.worker_darwin_qos_prefix(cpu_priority_mode)
    # Module-form argv carries its import root to the worker (5392a83ccd) and
    # a Linux host wraps the worker in a restart-safe scope (eb28bc1bf7);
    # ``RestartSafeScopeUnavailable`` surfaces to the dispatcher as an
    # infrastructure refusal, never a card failure.
    _propagate_module_import_root(cmd, env)
    cmd = _restart_safe_worker_argv(task, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    spawn_cmd = [*darwin_prefix, *cmd]
    _kb._log.info(
        "PHASE=worker_spawn task=%s profile=%s cpu_priority=%s nice=%s darwin_policy=%s nproc=%s",
        task.id,
        profile_arg,
        cpu_priority_mode,
        cpu_nice,
        " ".join(darwin_prefix[1:]) or "-",
        nproc_limit if nproc_limit is not None else "-",
    )
    spawned_at = time.time()
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            spawn_cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            preexec_fn=priority_preexec,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    finally:
        log_f.close()  # the child owns its inherited descriptor
    with _kb._worker_processes_lock:
        _kb._recent_worker_exits.pop(proc.pid, None)
        _kb._worker_processes[proc.pid] = proc
    _kb._register_worker_identity(proc.pid, task.id, task.current_run_id, spawned_at)
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------
def _local_spawn_count(res) -> int:
    """Spawns of ``res`` that run on THIS host (placed ids excluded by id)."""
    placed = {tid for tid, _h in (getattr(res, "placed", None) or [])}
    return sum(1 for s in (getattr(res, "spawned", None) or []) if s[0] not in placed)


def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
    load_gate=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``hermes kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.

    Each tick resolves ``kanban.max_in_progress`` (explicit config, else
    the memory-derived default) exactly like the gateway-embedded
    dispatcher and ``hermes kanban dispatch`` — the standalone daemon must
    not be the one uncapped entry point (OOF-30).

    ``load_gate`` (a :class:`kanban_load_gate.LoadGate`) applies the same
    projected-load admission + per-tick burst cap the gateway-embedded
    dispatcher uses; ``hermes kanban daemon`` builds it from
    ``kanban.dispatch_load_gate``. ``None`` = ungated (test hook default).
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    gate_ticks = None
    if load_gate is not None:
        # The gateway's tick path (KWLB): LOCAL allowance + ONE pool plan, so
        # the daemon places remote work exactly as the gateway does.
        from gateway.kanban_gate_tick import live_boards, standalone_builder

        def _kanban_cfg() -> dict:
            try:
                from hermes_cli.config import load_config
                k = (load_config() or {}).get("kanban") or {}
            except Exception:
                return {}
            return k if isinstance(k, dict) else {}

        gate_ticks = standalone_builder(load_gate, _kanban_cfg)

    while not stop_event.is_set():
        try:
            # Resolve the global concurrency cap the same way the gateway
            # dispatcher and `hermes kanban dispatch` do (OOF-30): explicit
            # kanban.max_in_progress wins, otherwise the memory-derived
            # default applies. The standalone daemon previously passed no
            # cap at all — the shipped systemd path could still fan out an
            # entire backlog in one tick even with the derived default in
            # place everywhere else. Re-resolved every tick (config load is
            # mtime-cached) so operator edits apply without a restart.
            max_in_progress = resolve_max_in_progress(
                configured_max_in_progress()
            )
            gate_kwargs: dict = {}
            if gate_ticks is not None:
                tick = gate_ticks.build(live_boards())
                gate_kwargs = {"spawn_paused": tick.spawn_paused,
                               "spawn_limit": tick.local_allowance,
                               "spillover": tick.remote_plan}
                if load_gate.host_recovered():
                    with contextlib.closing(_kbc.connect()) as _rconn:
                        _kb.requeue_host_transient_blocks(
                            _rconn, note=f"kanban daemon load gate {load_gate.state}",
                        )
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    **gate_kwargs,
                )
            if gate_ticks is not None:
                # Placement 1b: publish kanban's reservations (no-op when
                # kanban.placement.read_signal is false).
                gate_ticks.record_placements(list(getattr(res, "placed", None) or []))
            if load_gate is not None:
                # The Studio gate books LOCAL spawns only (RC-2).
                load_gate.finish_tick(_local_spawn_count(res), logger=_kb._log)
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)

# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
