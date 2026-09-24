"""CLI for the Hermes Kanban board — ``hermes kanban …`` subcommand.

Exposes the full Kanban command surface documented in the design spec
(``docs/hermes-kanban-v1-spec.pdf``).  All DB work is delegated to
``kanban_db``.  This module adds:

  * Argparse subcommand construction (``build_parser``).
  * Argument dispatch (``kanban_command``).
  * Output formatting (plain text + ``--json``).
  * A short shared helper that parses a single slash-style string
    (used by ``/kanban …`` in CLI and gateway) and forwards it to the
    argparse surface.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as ks
from hermes_cli.kanban_identity import safe_comment_provenance
from hermes_constants import get_default_hermes_root


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "todo":     "◻",
    "ready":    "▶",
    "running":  "●",
    "scheduled":"⏱",
    "blocked":  "⊘",
    "done":     "✓",
    "archived": "—",
}


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_respawn_guard_detail(detail: Optional[dict]) -> str:
    """`` — "<error>" recorded <ts> (Ns ago), eligible <ts>`` for a deferral
    the respawn guard derived from a stamped failure; empty otherwise."""
    if not detail:
        return ""
    now = int(time.time())
    parts = []
    err = detail.get("error")
    if err:
        err = " ".join(str(err).split())
        if len(err) > 120:
            err = err[:117] + "..."
        parts.append(f'"{err}"')
    recorded_at = detail.get("recorded_at")
    if recorded_at:
        parts.append(
            f"recorded {_fmt_ts(recorded_at)} ({max(0, now - int(recorded_at))}s ago)"
        )
    eligible_at = detail.get("eligible_at")
    if eligible_at:
        parts.append(f"eligible {_fmt_ts(eligible_at)}")
    return " — " + ", ".join(parts) if parts else ""


def _fmt_task_line(t: kb.Task) -> str:
    icon = _STATUS_ICONS.get(t.status, "?")
    assignee = t.assignee or "(unassigned)"
    tenant = f" [{t.tenant}]" if t.tenant else ""
    return f"{icon} {t.id}  {t.status:8s}  {assignee:20s}{tenant}  {t.title}"


def _fmt_links(links: list[tuple[str, str]]) -> str:
    """Render ``(id, kind)`` edges, annotating the non-gating ones.

    A plain id means the default ``blocks`` edge (what every edge used to
    be), so existing output is unchanged; a provenance edge is tagged so an
    operator can see at a glance that it does NOT gate dispatch.
    """
    return ", ".join(
        ident if kind == kb.LINK_KIND_BLOCKS else f"{ident} ({kind})"
        for ident, kind in links
    )


def _task_to_dict(t: kb.Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee": t.assignee,
        "status": t.status,
        "priority": t.priority,
        "tenant": t.tenant,
        "workspace_kind": t.workspace_kind,
        "workspace_path": t.workspace_path,
        "branch_name": t.branch_name,
        "project_id": t.project_id,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "result": t.result,
        "skills": list(t.skills) if t.skills else [],
        "max_retries": t.max_retries,
        "model_override": t.model_override,
        "provider_override": t.provider_override,
        "reasoning_effort": t.reasoning_effort,
        "session_id": t.session_id,
        "unhomed": bool(getattr(t, "unhomed", False)),
        "workflow_template_id": t.workflow_template_id,
        "current_step_key": t.current_step_key,
    }


def _run_state_kwargs(args: argparse.Namespace) -> Optional[dict[str, str]]:
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None
    if st is None:
        return {}
    return {"state_type": st, "state_name": sn}


def _parse_workspace_flag(value: str) -> tuple[str, Optional[str]]:
    """Parse ``--workspace`` into ``(kind, path|None)``.

    Accepts: ``scratch``, ``worktree``, ``worktree:<path>``, ``dir:<path>``.
    """
    if not value:
        return ("scratch", None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(
                f"--workspace {prefix} requires a path after the colon"
            )
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(
        f"unknown --workspace value {value!r}: use scratch, worktree, "
        "worktree:<path>, or dir:<path>"
    )


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch


def _check_dispatcher_presence(
    hermes_home: Optional[Path] = None,
) -> tuple[bool, str]:
    """Return ``(running, message)``.

    - ``running=True``: a gateway is alive for this HERMES_HOME and its
      config has ``kanban.dispatch_in_gateway`` on (default). Message
      is a short status line.
    - ``running=False``: either no gateway is running, or the gateway
      is running but the config flag is off. Message is human guidance
      explaining the next step.

    Used by ``hermes kanban create`` (and callers) to warn when a task
    will sit in ``ready`` because nothing is there to pick it up.
    Defensive against import failures and config-read errors — if the
    probe itself errors, we return ``(True, "")`` so we don't spam
    false warnings (better to miss a warning than to cry wolf).

    ``hermes_home`` scopes the probe to a named profile's directory. The
    dashboard plugin API passes it because the dashboard backend process can
    be running under a different HERMES_HOME than the profile the request
    targets, which otherwise produced a "no gateway is running" warning
    against a perfectly healthy profile gateway (#71211). CLI callers leave
    it ``None`` and keep the existing process-level behavior.

    NOTE (fork parity, 2026-08-07 parity merge): the upstream
    ``resolve_gateway_liveness`` ladder now lands in this fork's
    ``gateway.status`` via this merge, so the liveness-ladder enrichment
    that commit 11cffc4d5 prematurely carried (and #475 reverted to the
    fork ``get_running_pid`` probe to unbreak the import) is re-adopted here
    against the now-present symbol.
    """
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore
    except Exception:
        return (True, "")  # can't probe — silent
    try:
        # Same shared ladder the dashboard status endpoints use, so a
        # PID-file-less (launch-service-managed) or cross-container gateway
        # is not misreported as absent. use_cache=False: this is a one-shot
        # CLI/create-time probe, not a polling loop, and it must observe the
        # gateway's state right now rather than a cached snapshot.
        liveness = resolve_gateway_liveness(
            profile_dir=hermes_home, use_cache=False
        )
    except Exception:
        return (True, "")  # probe errored — silent
    if liveness.probe_error:
        # The resolver swallows per-rung failures so status endpoints never
        # 500. This caller must still fail OPEN: an unreadable probe means
        # "can't tell", not "no gateway", and warning on it cries wolf.
        return (True, "")
    pid = liveness.pid

    # Even if the gateway is up, dispatch_in_gateway may be off.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        dispatch_on = bool(cfg.get("kanban", {}).get("dispatch_in_gateway", True))
    except Exception:
        dispatch_on = True  # can't tell — assume default

    if pid and dispatch_on:
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid and not dispatch_on:
        return (
            False,
            "Gateway is running but kanban.dispatch_in_gateway=false in "
            "config.yaml — the task will sit in 'ready' until you flip it "
            "back on and restart the gateway, OR run the legacy "
            "standalone daemon (`hermes kanban daemon --force`)."
        )
    return (
        False,
        "No gateway is running — the task will sit in 'ready' until you "
        "start it. Run:\n"
        "    hermes gateway start\n"
        "The gateway hosts an embedded dispatcher (tick interval 60s by "
        "default); your task will be picked up on the next tick after "
        "the gateway comes up."
    )


# ---------------------------------------------------------------------------
# Argparse builder
# ---------------------------------------------------------------------------

def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree under an existing subparsers.

    Returns the top-level ``kanban`` parser so caller can ``set_defaults``.
    """
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description=(
            "Durable SQLite-backed task board shared across Hermes profiles. "
            "Tasks are claimed atomically, can depend on other tasks, and "
            "are executed by a named profile in an isolated workspace. "
            "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban "
            "or docs/hermes-kanban-v1-spec.pdf for the full design."
        ),
    )
    # --- global --board flag ---
    # Applies to every subcommand below. When set, scopes all reads and
    # writes to that board's DB. When omitted, resolves via the
    # HERMES_KANBAN_BOARD env var, then the persisted current-board
    # file, then "default". See kanban_db.get_current_board().
    kanban_parser.add_argument(
        "--board",
        default=None,
        metavar="<slug>",
        help=(
            "Board slug to operate on. Defaults to the current board "
            "(set via `hermes kanban boards switch <slug>` or the "
            "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards list` "
            "to see all boards."
        ),
    )
    sub = kanban_parser.add_subparsers(dest="kanban_action")

    # --- init ---
    sub.add_parser("init", help="Create kanban.db if missing (idempotent)")

    # --- boards (new in v2: multi-project support) ---
    p_boards = sub.add_parser(
        "boards",
        help="Manage kanban boards (one board per project / workstream)",
        description=(
            "Boards let you separate unrelated streams of work "
            "(projects, repos, domains) into isolated queues. Each "
            "board has its own DB, workspaces directory, and dispatcher "
            "loop — tasks on one board cannot collide with tasks on "
            "another. The first board is 'default' and always exists."
        ),
    )
    boards_sub = p_boards.add_subparsers(dest="boards_action")

    b_list = boards_sub.add_parser(
        "list", aliases=["ls"],
        help="List all boards with task counts",
    )
    b_list.add_argument("--json", action="store_true")
    b_list.add_argument("--all", action="store_true",
                        help="Include archived boards too")

    b_create = boards_sub.add_parser(
        "create", aliases=["new"],
        help="Create a new board",
    )
    b_create.add_argument("slug",
                          help="Board slug (kebab-case, e.g. atm10-server)")
    b_create.add_argument("--name", default=None,
                          help="Human-readable display name (defaults to Title Case of slug)")
    b_create.add_argument("--description", default=None,
                          help="Optional description")
    b_create.add_argument("--icon", default=None,
                          help="Optional emoji or single-character icon for the dashboard")
    b_create.add_argument("--color", default=None,
                          help="Optional hex color (e.g. '#8b5cf6') for the dashboard")
    b_create.add_argument("--switch", action="store_true",
                          help="Switch to the new board after creating it")
    b_create.add_argument("--default-workdir", default=None,
                          help="Default workspace path for tasks created on this board")

    b_rm = boards_sub.add_parser(
        "rm", aliases=["remove", "delete"],
        help="Archive (default) or delete a board",
    )
    b_rm.add_argument("slug")
    b_rm.add_argument("--delete", action="store_true",
                      help="Hard-delete the board directory instead of archiving it. "
                           "Default is to move it to boards/_archived/ so it's recoverable.")

    b_switch = boards_sub.add_parser(
        "switch", aliases=["use"],
        help="Set the active board for subsequent CLI calls",
    )
    b_switch.add_argument("slug")

    boards_sub.add_parser(
        "show", aliases=["current"],
        help="Print the currently-active board slug",
    )

    b_rename = boards_sub.add_parser(
        "rename",
        help="Change a board's human-readable display name (slug is immutable)",
    )
    b_rename.add_argument("slug")
    b_rename.add_argument("name", help="New display name")

    b_set_wd = boards_sub.add_parser(
        "set-default-workdir",
        help="Set the default workspace path for tasks on a board",
    )
    b_set_wd.add_argument("slug")
    b_set_wd.add_argument("path", nargs="?", default=None,
                          help="Absolute path to use as default workdir. Omit to clear.")

    b_export = boards_sub.add_parser(
        "export",
        help="Export a board to a portable .tar.gz archive",
        description=(
            "Package a board's tasks, comments, links, history, and file "
            "attachments into one archive that can be imported on another "
            "machine. Claims, worker PIDs, chat subscriptions, and paths "
            "belonging to this machine are stripped. Workspaces are never "
            "included — they are rebuilt on demand."
        ),
    )
    b_export.add_argument("slug", nargs="?", default=None,
                          help="Board to export (default: the current board)")
    b_export.add_argument("-o", "--output", default=None,
                          help="Archive path (default: ./<slug>.tar.gz)")
    b_export.add_argument("--no-attachments", action="store_true",
                          help="Skip attachment files, keeping the archive small")
    b_export.add_argument("--include-logs", action="store_true",
                          help="Include per-task worker logs")
    b_export.add_argument("--json", action="store_true")

    b_import = boards_sub.add_parser(
        "import",
        help="Import a board archive as a new board",
        description=(
            "Import a .tar.gz produced by `hermes kanban boards export`. "
            "The board always lands as a NEW board — the slug gains a "
            "numeric suffix if it is already taken — so an import can "
            "never overwrite or merge into a board you already have."
        ),
    )
    b_import.add_argument("archive", help="Path to the .tar.gz archive")
    b_import.add_argument("--as", dest="as_slug", default=None,
                          help="Slug for the imported board (default: from the archive)")
    b_import.add_argument("--switch", action="store_true",
                          help="Switch to the imported board afterwards")
    b_import.add_argument("--json", action="store_true")

    # --- create ---
    p_create = sub.add_parser("create", help="Create a new task")
    p_create.add_argument("title", help="Task title")
    p_create.add_argument("--body", default=None, help="Optional opening post")
    p_create.add_argument("--assignee", default=None, help="Profile name to assign")
    p_create.add_argument("--parent", action="append", default=[],
                          help="Parent task id (repeatable)")
    p_create.add_argument(
        "--parent-kind", default=kb.DEFAULT_LINK_KIND,
        choices=sorted(kb.VALID_LINK_KINDS),
        help=(
            "Semantics for every --parent edge. 'blocks' (default) holds this "
            "task until the parents are done; 'derived-from' records that a "
            "parent discovered/spawned this work without gating it."
        ),
    )
    p_create.add_argument("--workspace", default="scratch",
                          help="scratch | worktree | worktree:<path> | dir:<path> "
                               "(default: scratch)")
    p_create.add_argument("--branch", default=None,
                          help="Branch name for worktree tasks, e.g. wt/t6-wire")
    p_create.add_argument("--project", default=None,
                          help="Link to a project (id or slug). Anchors the task's "
                               "worktree under the project's primary repo with a "
                               "deterministic branch. See `hermes project list`.")
    p_create.add_argument("--tenant", default=None, help="Tenant namespace")
    p_create.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_create.add_argument("--triage", action="store_true",
                          help="Park in triage — a specifier will flesh out the spec and promote to todo")
    p_create.add_argument("--idempotency-key", default=None,
                          help="Dedup key. If a non-archived task with this key exists, "
                               "its id is returned instead of creating a duplicate.")
    p_create.add_argument("--max-runtime", default=None,
                          help="Per-task runtime cap. Accepts seconds (300) or "
                               "durations (90s, 30m, 2h, 1d). When exceeded, "
                               "the dispatcher SIGTERMs (then SIGKILLs) the worker "
                               "and re-queues the task.")
    p_create.add_argument("--created-by", default="user",
                          help="Author name recorded on the task (default: user)")
    p_create.add_argument("--skill", action="append", default=[], dest="skills",
                          help="Skill to force-load into the worker "
                               "(repeatable). The kanban lifecycle is already "
                               "injected automatically. Example: "
                               "--skill translation --skill github-code-review")
    p_create.add_argument("--max-retries", type=int, default=None,
                          metavar="N",
                          help="Per-task override for the consecutive-failure "
                               "circuit breaker. Trip on the Nth failure — "
                               "e.g. --max-retries 1 blocks on the first "
                               "failure (no retries), --max-retries 3 allows "
                               "two retries. Omit to use the dispatcher's "
                               "kanban.failure_limit config "
                               f"(default {kb.DEFAULT_FAILURE_LIMIT}).")
    p_create.add_argument("--model", default=None, dest="model_override",
                          help="Pin the worker to this model (passed as "
                               "-m <model>) without changing the profile's "
                               "configured model. Combine with --provider "
                               "when the model belongs to a different "
                               "backend than the profile's default.")
    p_create.add_argument("--provider", default=None, dest="provider_override",
                          help="Provider the --model belongs to (passed as "
                               "--provider <name> to the worker). Requires "
                               "--model.")
    p_create.add_argument(
        "--allow-flagship",
        "--firepower",
        default=None,
        dest="allow_flagship",
        metavar="REASON",
        help="Allow an orchestrator-only flagship model for this task. "
             "Requires a non-empty reason, recorded as a task comment. "
             "--firepower is an alias.",
    )
    p_create.add_argument(
        "--reasoning",
        "--effort",
        default=None,
        dest="reasoning_effort",
        metavar="LEVEL",
        help="Pin the worker's reasoning effort for this task (none, minimal, "
             "low, medium, high, xhigh, max, or ultra). 'none' disables "
             "thinking. Independent of --model; omit to inherit the "
             "assignee profile default.",
    )
    p_create.add_argument("--goal", action="store_true", dest="goal_mode",
                          help="Run the worker in a goal loop: after each "
                               "turn a judge checks the response against the "
                               "card title/body and, if not done, the worker "
                               "keeps going in the same session until the "
                               "judge agrees it's complete (or the turn "
                               "budget runs out, which blocks the card for "
                               "review). Best for open-ended cards one shot "
                               "rarely finishes.")
    p_create.add_argument("--goal-max-turns", type=int, default=None,
                          metavar="N", dest="goal_max_turns",
                          help="Turn budget for --goal workers (default 20). "
                               "Ignored without --goal.")
    p_create.add_argument("--initial-status",
                          choices=sorted(kb.VALID_INITIAL_STATUSES),
                          default="running",
                          help="Initial card status. Use 'blocked' for cards "
                               "that require immediate human ops (R3 gate) "
                               "to skip the brief running-to-blocked transition.")
    p_create.add_argument("--session", default=None, metavar="SESSION_ID",
                          help="Home session to stamp on the card (default: "
                               "$HERMES_SESSION_ID when set; 'none' = unstamped)")
    p_create.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- swarm ---
    p_swarm = sub.add_parser(
        "swarm",
        help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)",
    )
    p_swarm.add_argument("goal", help="Swarm goal / final outcome")
    p_swarm.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="PROFILE:TITLE[:SKILL,SKILL]",
        help="Parallel worker card (repeatable)",
    )
    p_swarm.add_argument("--verifier", required=True, help="Verifier profile")
    p_swarm.add_argument("--synthesizer", required=True, help="Synthesizer/writer profile")
    p_swarm.add_argument("--tenant", default=None, help="Tenant namespace")
    p_swarm.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_swarm.add_argument("--created-by", default=None, help="Creator/anchor profile")
    p_swarm.add_argument("--idempotency-key", default=None, help="Dedup key for the root card")
    p_swarm.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- list ---
    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("--mine", action="store_true",
                        help="Filter by $HERMES_PROFILE as assignee")
    p_list.add_argument("--assignee", default=None)
    p_list.add_argument("--status", default=None,
                        choices=sorted(kb.VALID_STATUSES))
    p_list.add_argument("--tenant", default=None)
    p_list.add_argument("--session", default=None,
                        help="Filter by originating chat/agent session id "
                             "(set on tasks created from inside an ACP loop)")
    p_list.add_argument("--home", "--this-session", action="store_true",
                        dest="home",
                        help="Only cards whose home session is this session "
                             "(== --session $HERMES_SESSION_ID)")
    p_list.add_argument("--all", action="store_true", dest="flat_all",
                        help="Flat board-wide view (no THIS SESSION / OTHER "
                             "SESSIONS grouping)")
    p_list.add_argument("--archived", action="store_true",
                        help="Include archived tasks")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument(
        "--sort",
        default=None,
        choices=sorted(kb.VALID_SORT_ORDERS.keys()),
        help="Sort order for listed tasks (default: priority)",
    )
    p_list.add_argument(
        "--workflow-template-id",
        default=None,
        metavar="ID",
        help="Restrict to tasks with this workflow_template_id",
    )
    p_list.add_argument(
        "--step-key",
        default=None,
        dest="current_step_key",
        metavar="KEY",
        help="Restrict to tasks with this current_step_key",
    )

    # --- show ---
    p_show = sub.add_parser("show", help="Show a task with comments + events")
    p_show.add_argument("task_id")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter listed runs by task_runs column",
    )
    p_show.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- assign ---
    p_assign = sub.add_parser("assign", help="Assign or reassign a task")
    p_assign.add_argument("task_id")
    p_assign.add_argument("profile", help="Profile name (or 'none' to unassign)")

    # --- set-model (per-task model/provider/effort override) ---
    p_set_model = sub.add_parser(
        "set-model",
        help="Set or clear a task's model/provider/effort override "
             "(takes effect on the next dispatch)",
    )
    p_set_model.add_argument(
        "task_ids", nargs="*", metavar="task_id",
        help="One or more task ids. Omit when selecting with "
             "--where/--all-active. The LAST positional is the model.",
    )
    p_set_model.add_argument("--model", default=None, help="Model id (alternative to the last positional).")
    p_set_model.add_argument("--model-json", default=None, help="Unified override object as JSON.")
    p_set_model.add_argument(
        "--provider", default=None,
        help="Provider the model belongs to (worker is spawned with "
             "--provider <name>). Cleared together with the model.",
    )
    p_set_model.add_argument(
        "--where", nargs="+", default=None, metavar="KEY=VALUE",
        help="Select cards by status/assignee instead of naming ids, e.g. "
             "--where status=running,ready assignee=daedalus-opus. Only "
             "active (non-done, non-archived) cards are considered.",
    )
    p_set_model.add_argument(
        "--all-active", action="store_true", dest="all_active",
        help="Select every active card on the board (excludes done/archived).",
    )
    p_set_model.add_argument(
        "--reclaim", action="store_true",
        help="Release the claim on selected RUNNING cards so the next "
             "dispatch respawns them on the new route. Without this a "
             "running worker keeps its old model until it finishes.",
    )
    p_set_model.add_argument(
        "--allow-flagship",
        "--firepower",
        default=None,
        dest="allow_flagship",
        metavar="REASON",
        help="Allow an orchestrator-only flagship model. Requires a non-empty "
             "reason, recorded as a task comment. --firepower is an alias.",
    )
    _effort_group = p_set_model.add_mutually_exclusive_group()
    _effort_group.add_argument(
        "--effort", default=None, dest="reasoning_effort", metavar="LEVEL",
        help="Per-task reasoning effort (worker is spawned with "
             "--reasoning <level>). 'none' is a real level — thinking "
             "off. Independent of the model: with --effort alone the "
             "model override is left untouched, and clearing the model "
             "never resets the effort.",
    )
    _effort_group.add_argument(
        "--clear-effort", action="store_true", dest="clear_effort",
        help="Clear the per-task reasoning effort — the worker falls back "
             "to its profile's own agent.reasoning_effort.",
    )

    # --- lane-model (board-level, time-boxed routing) ---
    p_lane_model = sub.add_parser(
        "lane-model",
        help="Time-boxed board-level model override applied at dispatch to "
             "cards without their own override",
    )
    _lane_sub = p_lane_model.add_subparsers(dest="lane_action")
    _lane_set = _lane_sub.add_parser(
        "set", help="Install a lane override that expires on its TTL",
    )
    _lane_set.add_argument(
        "route", nargs="?", default=None, metavar="PROVIDER/MODEL",
        help="Route to pin the lane to, e.g. claude-bpx-19/claude-opus-5. "
             "May also be given as --provider/--model.",
    )
    _lane_set.add_argument("--provider", default=None)
    _lane_set.add_argument("--model", default=None)
    _lane_set.add_argument("--model-json", default=None, help="Unified override object as JSON.")
    _lane_set.add_argument("--effort", default=None, dest="reasoning_effort")
    _lane_set.add_argument(
        "--ttl", default=None, required=False, metavar="DURATION",
        help="How long the override lives (30m, 2h, 1d). Required — an "
             "override without an expiry is just a config edit with extra steps.",
    )
    _lane_set.add_argument(
        "--reason", default=None,
        help="Why the lane is being re-routed (shown in show/stats).",
    )
    _lane_set.add_argument(
        "--assignee", default=None,
        help="Restrict the override to one profile. Omit for board-wide.",
    )
    _lane_set.add_argument(
        "--firepower", "--allow-flagship", default=None, dest="firepower",
        metavar="REASON",
        help="Required justification when the model is flagship/firepower-only "
             "(same flagship ban as create/set-model --allow-flagship).",
    )
    _lane_show = _lane_sub.add_parser(
        "show", help="Show active lane overrides and their remaining TTL",
    )
    _lane_show.add_argument("--json", action="store_true", dest="as_json")
    _lane_clear = _lane_sub.add_parser(
        "clear", help="Remove a lane override before its TTL elapses",
    )
    _lane_clear.add_argument(
        "--assignee", default=None,
        help="Lane to clear. Omit for the board-wide override.",
    )
    _lane_clear.add_argument(
        "--all", action="store_true", dest="clear_all",
        help="Clear every lane override on the board.",
    )

    # --- reclaim / reassign (recovery) ---
    p_reclaim = sub.add_parser(
        "reclaim",
        help="Release an active worker claim on a running task",
    )
    p_reclaim.add_argument("task_id")
    p_reclaim.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    p_reassign = sub.add_parser(
        "reassign",
        help="Reassign a task to a different profile, optionally reclaiming first",
    )
    p_reassign.add_argument("task_id")
    p_reassign.add_argument(
        "profile",
        help="New profile name (or 'none' to unassign)",
    )
    p_reassign.add_argument(
        "--reclaim", action="store_true",
        help="Release any active claim before reassigning (required if task is running)",
    )
    p_reassign.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    # --- diagnostics (board-wide health) ---
    p_diag = sub.add_parser(
        "diagnostics",
        aliases=["diag"],
        help="List active diagnostics on the current board",
    )
    p_diag.add_argument(
        "--severity",
        choices=["warning", "error", "critical"],
        default=None,
        help="Only show diagnostics at or above this severity",
    )
    p_diag.add_argument(
        "--task",
        default=None,
        help="Only show diagnostics for one task id",
    )
    p_diag.add_argument(
        "--json", action="store_true",
        help="Emit JSON (structured) instead of the default human table",
    )

    # --- link / unlink ---
    p_link = sub.add_parser("link", help="Add a parent->child dependency")
    p_link.add_argument("parent_id")
    p_link.add_argument("child_id")
    p_link.add_argument(
        "--kind", default=kb.DEFAULT_LINK_KIND,
        choices=sorted(kb.VALID_LINK_KINDS),
        help=(
            "Edge semantics. 'blocks' (default) holds the child until the "
            "parent is done. 'derived-from' records provenance only — the "
            "parent discovered/spawned the child, which stays independently "
            "dispatchable. A DEPLOY must never be gated on a DISCOVERY."
        ),
    )
    p_unlink = sub.add_parser("unlink", help="Remove a parent->child dependency")
    p_unlink.add_argument("parent_id")
    p_unlink.add_argument("child_id")

    # --- claim ---
    p_claim = sub.add_parser(
        "claim",
        help="Atomically claim a ready task (prints resolved workspace path)",
    )
    p_claim.add_argument("task_id")
    p_claim.add_argument("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS,
                         help="Claim TTL in seconds (default: 900)")

    # --- comment / complete / block / unblock / archive ---
    p_comment = sub.add_parser("comment", help="Append a comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("text", nargs="+", help="Comment body")
    p_comment.add_argument("--author", default=None,
                           help="Author name (default: $HERMES_PROFILE or 'user')")
    p_comment.add_argument("--max-len", type=int, default=None,
                           help="Trim the stored comment body to this many characters")

    # --- attach / attachments / attach-rm ---
    p_attach = sub.add_parser("attach", help="Attach a local file to a task")
    p_attach.add_argument("task_id")
    p_attach.add_argument("path", help="Path to the local file to attach")
    p_attach.add_argument("--content-type", default=None,
                          help="MIME type (default: guessed from the file extension)")
    p_attach.add_argument("--name", default=None,
                          help="Stored filename (default: the source file's basename)")
    p_attach.add_argument("--author", default=None,
                          help="uploaded_by label (default: $HERMES_PROFILE or 'user')")

    p_attachments = sub.add_parser("attachments", help="List a task's attachments")
    p_attachments.add_argument("task_id")
    p_attachments.add_argument("--json", action="store_true")

    p_attach_rm = sub.add_parser("attach-rm", help="Delete an attachment by id")
    p_attach_rm.add_argument("attachment_id", type=int)

    p_complete = sub.add_parser("complete", help="Mark one or more tasks done")
    p_complete.add_argument("task_ids", nargs="+",
                            help="One or more task ids (only --result applies to all of them)")
    p_complete.add_argument("--result", default=None, help="Result summary")
    p_complete.add_argument("--summary", default=None,
                            help="Structured handoff summary for downstream tasks. "
                                 "Falls back to --result if omitted.")
    p_complete.add_argument("--metadata", default=None,
                            help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                                 '"tests_run": 12}\'). Stored on the closing run.')
    p_complete.add_argument("--superseded-by", default=None, metavar="CARD|PR|SHA",
                            help="Evidence pointer for a card whose premise was already "
                                 "satisfied elsewhere. Closes it done with outcome "
                                 "'superseded'; no --result/--summary required, but the "
                                 "pointer must be non-empty (an unnamed supersede is a "
                                 "silent delete of the work).")
    p_complete.add_argument("--survivor-ref", default=None, action="append", metavar="[REPO=]URL#SHA",
                            help="Name an external survivor when the implementation lives on a "
                                 "remote, not in the workspace. Verified with git ls-remote "
                                 "AND required to name this task: the SHA must resolve to a "
                                 "single branch or tag tip whose ref name contains the task id. "
                                 "An unverifiable claim, or one on an unrelated-looking ref, "
                                 "refuses the completion (see --survivor-unbound). Repeatable: "
                                 "qualify each claim as <workspace-relative-repo>=<claim> when "
                                 "more than one recorded repository vanished.")
    p_complete.add_argument("--survivor-pr", default=None, action="append", metavar="[REPO=]OWNER/REPO#N",
                            help="Name an external survivor by pull request. Verified with "
                                 "gh pr view (state OPEN or MERGED) AND required to name this "
                                 "task; an unverifiable claim refuses the completion. Naming "
                                 "the task in the PR's head BRANCH binds the claim. A match "
                                 "only in the PR title or body is a mention, not a tie to this "
                                 "card's work, so it is recorded as an unbound claim (see "
                                 "--survivor-unbound) and never becomes standing authority to "
                                 "delete the workspace later. Repeatable; same <repo>= qualifier "
                                 "as --survivor-ref.")
    p_complete.add_argument("--survivor-unbound", action="append", nargs="?", const=True,
                            default=None, metavar="REPO",
                            help="Operator override: accept a --survivor-ref/--survivor-pr that "
                                 "is live but does NOT name this task (including one that names "
                                 "it only in a PR title or body, which is a mention), for the "
                                 "case where the work really did land on an unrelated-looking "
                                 "branch. PER-CLAIM: pass it bare for a single-claim completion, "
                                 "or repeat it with the <repo>= qualifier of each claim being "
                                 "overridden when there is more than one -- overriding one claim "
                                 "must not silently accept the others. The claim "
                                 "is still remote-verified; the override and the OS user (resolved "
                                 "from the real uid, not $USER) are recorded on the survivor and "
                                 "in the task event log. An unbound claim authorises THIS "
                                 "completion only: a later reclamation will not reuse it.")

    p_edit = sub.add_parser(
        "edit",
        aliases=["update"],
        help="Edit recovery fields on an already-completed task, or "
             "(re)stamp its home session with --session",
    )
    p_edit.add_argument("task_id")
    p_edit.add_argument(
        "--result",
        default=None,
        help="Backfilled task result text for a done task",
    )
    p_edit.add_argument(
        "--summary",
        default=None,
        help="Structured handoff summary. Falls back to --result if omitted.",
    )
    p_edit.add_argument(
        "--metadata",
        default=None,
        help="JSON dict of structured facts to store on the latest completed run.",
    )
    p_edit.add_argument(
        "--model",
        default=None,
        dest="model_override",
        metavar="MODEL",
        help="Set the per-task model override (passed to the worker as "
             "`hermes -m MODEL`). Omitting --model leaves the existing "
             "override untouched; use --clear-model to remove it.",
    )
    p_edit.add_argument(
        "--clear-model",
        action="store_true",
        dest="clear_model",
        help="Clear the per-task model override (revert to the assignee "
             "profile default). Mutually exclusive with --model.",
    )
    p_edit.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="(Re)stamp the card's home session ('none' = unstamped).",
    )

    p_block = sub.add_parser("block", help="Mark one or more tasks blocked")
    p_block.add_argument("task_id")
    p_block.add_argument("reason", nargs="*", help="Reason (also appended as a comment)")
    p_block.add_argument("--ids", nargs="+", default=None,
                         help="Additional task ids to block with the same reason (bulk mode)")
    p_block.add_argument(
        "--kind", default=None, choices=sorted(kb.VALID_BLOCK_KINDS),
        help=(
            "Typed block reason. 'dependency' waits in todo (auto-promoted "
            "when parents finish, no human); 'needs_input'/'capability' go to "
            "blocked for a human; 'transient' marks a maybe-flaky failure. "
            "Repeated same-kind re-blocks after unblock route the task to "
            "triage to break unblock loops. Omit for a generic block."
        ),
    )

    p_budget = sub.add_parser(
        "budget",
        help="Report per-board 24h worker spend against kanban.budget.usd_per_24h",
    )
    p_budget.add_argument(
        "--board", dest="budget_board", default=None,
        help="Report a single board instead of every board.",
    )
    p_budget.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON",
    )

    p_schedule = sub.add_parser("schedule", help="Park one or more tasks in Scheduled (waiting on time, not human input)")
    p_schedule.add_argument("task_id")
    p_schedule.add_argument("reason", nargs="*", help="Reason/timing note (also appended as a comment)")
    p_schedule.add_argument("--ids", nargs="+", default=None,
                            help="Additional task ids to schedule with the same reason (bulk mode)")

    p_unblock = sub.add_parser(
        "unblock",
        help="Return blocked/scheduled tasks to ready, or todo while parents remain open",
    )
    p_unblock.add_argument(
        "--reason",
        default=None,
        help="Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons.",
    )
    p_unblock.add_argument("task_ids", nargs="+")

    p_requeue = sub.add_parser("requeue", help="Explicitly retry a READY card held by the respawn guard")
    p_requeue.add_argument("task_id")
    p_requeue.add_argument("reason", nargs="+", help="Required operator reason")

    p_reopen = sub.add_parser(
        "reopen",
        help=(
            "Void a FALSE completion on a done task and requeue it "
            "(recovery path when a non-owner wrote a terminal result)"
        ),
    )
    p_reopen.add_argument("task_id")
    p_reopen.add_argument(
        "reason",
        nargs="+",
        help="Required audit-trail reason (recorded on the task_events row)",
    )
    p_reopen.add_argument(
        "--to",
        dest="to_status",
        choices=("ready", "todo"),
        default="ready",
        help="Status to return the task to (default: ready)",
    )
    p_reopen.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )

    p_request_review = sub.add_parser(
        "request-review",
        help="Move a task to 'review' (implementation done, awaiting review) — NOT a block",
    )
    p_request_review.add_argument("task_id")
    p_request_review.add_argument(
        "--summary", default=None,
        help="What was implemented and how it was verified — shown to the reviewer.",
    )
    p_request_review.add_argument(
        "--reviewer", default=None,
        help=(
            "Reviewer profile (or the explicit sentinel 'human' / "
            "'human:<name>'); reassigns the task before review dispatch. "
            "Defaults to config kanban.review_assignee. A reviewer that is "
            "neither an installed profile nor 'human' is refused."
        ),
    )
    p_request_review.add_argument(
        "--allow-same-actor", action="store_true",
        help=(
            "Permit the implementer to review their own work (normally "
            "refused). Recorded on the review_requested event."
        ),
    )
    p_request_review.add_argument(
        "--metadata", default=None,
        help="JSON object with structured reviewer handoff facts.",
    )
    p_request_review.add_argument(
        "--force", action="store_true",
        help=(
            "Override the live-claim guard: move a running, claimed task to "
            "review even without owning its run (clears the worker's claim)."
        ),
    )

    p_request_changes = sub.add_parser(
        "request-changes",
        help="Reviewer verdict: return the active review run to its implementer",
    )
    p_request_changes.add_argument("task_id")
    p_request_changes.add_argument(
        "reason", nargs="+", help="Concrete changes required before re-review",
    )

    p_reopen_review = sub.add_parser(
        "reopen-review",
        help="Send one or more review tasks back for changes (review -> ready/todo)",
    )
    p_reopen_review.add_argument("task_ids", nargs="+")
    p_reopen_review.add_argument(
        "--reason", default=None,
        help="Optional reason/note — recorded as a comment before reopening. Quote multi-word reasons.",
    )

    p_promote = sub.add_parser(
        "promote",
        help="Manually move one or more todo/blocked tasks to ready (recovery path)",
    )
    p_promote.add_argument("task_id")
    p_promote.add_argument(
        "reason",
        nargs="*",
        help="Audit-trail reason (recorded on the task_events row)",
    )
    p_promote.add_argument(
        "--ids",
        nargs="+",
        default=None,
        help="Additional task ids to promote with the same reason (bulk mode)",
    )
    p_promote.add_argument(
        "--force",
        action="store_true",
        help="Promote even if parent dependencies are not yet done/archived",
    )
    p_promote.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the promotion without mutating state",
    )
    p_promote.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )

    p_triage_resolve = sub.add_parser(
        "triage-resolve",
        help="Resolve a triage card with an explicit human decision "
             "(the supported exit from the unblock-loop escalation)",
    )
    p_triage_resolve.add_argument("task_id")
    p_triage_resolve.add_argument(
        "--to",
        required=True,
        choices=list(kb.TRIAGE_RESOLVE_TARGETS),
        help="Where the card goes. 'todo' re-queues it through normal parent "
             "gating; 'done'/'archived' close it. 'ready' is deliberately not "
             "offered — jumping the queue re-arms the unblock loop.",
    )
    p_triage_resolve.add_argument(
        "--reason",
        required=True,
        help="Why (required). Recorded on the triage_resolved event and as a "
             "comment — this is the human-in-the-loop decision the escalation "
             "asked for.",
    )
    p_triage_resolve.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )

    p_archive = sub.add_parser("archive", help="Archive one or more tasks")
    p_archive.add_argument("task_ids", nargs="*",
                           help="Task ids to archive (default mode)")
    p_archive.add_argument(
        "--rm",
        dest="purge_ids",
        nargs="+",
        default=None,
        help="Permanently delete already-archived task ids from the board",
    )

    # --- tail ---
    p_tail = sub.add_parser("tail", help="Follow a task's event stream")
    p_tail.add_argument("task_id")
    p_tail.add_argument("--interval", type=float, default=1.0)

    # --- dispatch ---
    p_disp = sub.add_parser(
        "dispatch",
        help="One dispatcher pass: reclaim stale, promote ready, spawn workers",
    )
    p_disp.add_argument("--dry-run", action="store_true",
                        help="Don't actually spawn processes; just print what would happen")
    p_disp.add_argument("--max", type=int, default=None,
                        help="Cap number of spawns this pass")
    p_disp.add_argument("--failure-limit", type=int,
                        default=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
                        help=f"Auto-block a task after this many consecutive non-success attempts "
                             f"(spawn_failed, timed_out, or crashed; default: {kb.DEFAULT_SPAWN_FAILURE_LIMIT})")
    p_disp.add_argument("--json", action="store_true")

    # --- daemon (deprecated) ---
    p_daemon = sub.add_parser(
        "daemon",
        help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`.",
    )
    p_daemon.add_argument("--interval", type=float, default=60.0,
                          help="Seconds between dispatch ticks (default: 60)")
    p_daemon.add_argument("--max", type=int, default=None,
                          help="Cap number of spawns per tick")
    p_daemon.add_argument("--failure-limit", type=int,
                          default=kb.DEFAULT_SPAWN_FAILURE_LIMIT)
    p_daemon.add_argument("--pidfile", default=None,
                          help="Write the daemon's PID to this file on start")
    p_daemon.add_argument("--verbose", "-v", action="store_true",
                          help="Log each tick's outcome to stdout")
    # Undocumented escape hatch for users who truly cannot run the gateway.
    # Intentionally excluded from --help so nobody discovers it casually and
    # keeps the old double-dispatcher pattern alive.
    p_daemon.add_argument("--force", action="store_true",
                          help=argparse.SUPPRESS)

    # --- watch ---
    p_watch = sub.add_parser(
        "watch",
        help="Live-stream task_events to the terminal (Ctrl+C to exit)",
    )
    p_watch.add_argument("--assignee", default=None,
                         help="Only show events for tasks assigned to this profile")
    p_watch.add_argument("--tenant", default=None,
                         help="Only show events from tasks in this tenant")
    p_watch.add_argument("--kinds", default=None,
                         help="Comma-separated event kinds to include "
                              "(e.g. 'completed,blocked,gave_up,crashed,timed_out')")
    p_watch.add_argument("--interval", type=float, default=0.5,
                         help="Poll interval in seconds (default: 0.5)")

    # --- stats ---
    p_stats = sub.add_parser(
        "stats", help="Per-status + per-assignee counts + oldest-ready age",
    )
    p_stats.add_argument("--json", action="store_true")

    # --- notify subscribe / list / remove ---
    p_nsub = sub.add_parser(
        "notify-subscribe",
        help="Subscribe a gateway source to a task's terminal events "
             "(used by /kanban subscribe in the gateway adapter)",
    )
    p_nsub.add_argument("task_id")
    p_nsub.add_argument("--platform", required=True)
    p_nsub.add_argument("--chat-id", required=True)
    p_nsub.add_argument("--thread-id", default=None)
    p_nsub.add_argument("--user-id", default=None)
    p_nsub.add_argument("--user-id-alt", default=None)
    p_nsub.add_argument(
        "--chat-type",
        choices=("dm", "group", "channel", "thread"),
        default=None,
        help="Originating source chat_type, recorded so the active-wake "
             "delivery modes resolve the operator's real session. Omit to "
             "leave an existing sub unchanged (new subs default to 'dm').",
    )
    p_nsub.add_argument(
        "--notifier-profile", default=None,
        help="Profile gateway that owns/delivers this subscription (default: active profile)",
    )
    p_nsub.add_argument(
        "--delivery-mode",
        # Single source of truth shared with the DB/watcher enum.
        choices=kb._NOTIFY_DELIVERY_MODES,
        default=None,
        help="How the kanban-notifier reacts to terminal events for this "
             "subscription: 'notify' (passive message only; default), "
             "'notify+wake' (message AND wake the destination gateway agent so "
             "it reads the full board context and replies in its own voice), or "
             "'wake' (wake the agent only, no passive message). Omit to leave an "
             "existing subscription's mode unchanged (new subs default to 'notify').",
    )

    p_nlist = sub.add_parser(
        "notify-list",
        help="List notification subscriptions (optionally for a single task)",
    )
    p_nlist.add_argument("task_id", nargs="?", default=None)
    p_nlist.add_argument("--json", action="store_true")

    p_nrm = sub.add_parser(
        "notify-unsubscribe",
        help="Remove a gateway subscription from a task",
    )
    p_nrm.add_argument("task_id")
    p_nrm.add_argument("--platform", required=True)
    p_nrm.add_argument("--chat-id", required=True)
    p_nrm.add_argument("--thread-id", default=None)

    p_nrepair = sub.add_parser(
        "notify-repair",
        help="Backfill missing key identity on legacy notify subscriptions",
        description=(
            "Repairs legacy notify-sub rows missing user_id, user_id_alt, or "
            "scope_id. Without the same identity fields as the creator, the "
            "wake injector can rebuild a SECOND session key for the same chat "
            "— one no human can send to, which therefore never receives the "
            "/reasoning or /model overrides the user set. The complete creator "
            "identity is read from the gateway routing index: a row is repaired "
            "only when exactly ONE routing identity exists for that chat, so "
            "the participant and workspace scope are unambiguous. A chat with no "
            "per-user session (cron / CLI / home-channel origins, which are "
            "legitimately user-less) is left alone — an identity is never "
            "invented. Idempotent and fill-a-hole only: existing identity fields "
            "are never re-pointed."
        ),
    )
    p_nrepair.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing",
    )
    p_nrepair.add_argument(
        "--all-boards", action="store_true",
        help="Sweep every board DB (default + boards/*/kanban.db), "
             "not just the active board",
    )
    p_nrepair.add_argument("--json", action="store_true")

    # --- log ---
    p_log = sub.add_parser(
        "log",
        help="Print the worker log for a task (from <kanban-root>/kanban/logs/)",
    )
    p_log.add_argument("task_id")
    p_log.add_argument("--tail", type=int, default=None,
                       help="Only print the last N bytes")

    # --- runs (per-attempt history for a task) ---
    p_runs = sub.add_parser(
        "runs",
        help="Show attempt history for a task (one row per run: profile, "
             "outcome, elapsed, summary)",
    )
    p_runs.add_argument("task_id")
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter runs by task_runs column",
    )
    p_runs.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- heartbeat (worker liveness signal) ---
    p_hb = sub.add_parser(
        "heartbeat",
        help="Emit a heartbeat event for a running task (worker liveness signal)",
    )
    p_hb.add_argument("task_id")
    p_hb.add_argument("--note", default=None,
                      help="Optional short note attached to the heartbeat event")

    # --- assignees ---
    p_asg = sub.add_parser(
        "assignees",
        help="List known profiles + per-profile task counts "
             "(union of ~/.hermes/profiles/ and current assignees on the board)",
    )
    p_asg.add_argument("--json", action="store_true")

    # --- context --- (for spawned workers)
    p_ctx = sub.add_parser(
        "context",
        help="Print the full context a worker sees for a task "
             "(title + body + parent results + comments).",
    )
    p_ctx.add_argument("task_id")

    # --- specify --- (triage → todo via auxiliary LLM)
    p_specify = sub.add_parser(
        "specify",
        help="Flesh out a triage-column task into a concrete spec "
             "(title + body) and promote it to todo. Uses the auxiliary "
             "LLM configured under auxiliary.triage_specifier.",
    )
    p_specify.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to specify (required unless --all is given)",
    )
    p_specify.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Specify every task currently in the triage column",
    )
    p_specify.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_specify.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'specifier')",
    )
    p_specify.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- decompose --- (triage → fan-out via auxiliary LLM + orchestrator)
    p_decompose = sub.add_parser(
        "decompose",
        help="Decompose a triage-column task into a graph of child tasks "
             "routed to specialist profiles by description. Falls back to "
             "specify-style single-task promotion when the task doesn't "
             "benefit from fan-out. Uses auxiliary.kanban_decomposer.",
    )
    p_decompose.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to decompose (required unless --all is given)",
    )
    p_decompose.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Decompose every task currently in the triage column",
    )
    p_decompose.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_decompose.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'decomposer')",
    )
    p_decompose.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- gc ---
    p_gc = sub.add_parser(
        "gc", help="Garbage-collect archived-task workspaces, old events, and old logs",
    )
    p_gc.add_argument("--event-retention-days", type=int, default=30,
                      help="Delete task_events older than N days for terminal tasks (default: 30)")
    p_gc.add_argument("--log-retention-days", type=int, default=30,
                      help="Delete worker log files older than N days (default: 30)")
    p_gc.add_argument("--done-retention-days", type=int, default=3,
                      help="Also remove workspaces of DONE tasks finished at least N days "
                           "ago; same survivor/liveness/audit gates as archived "
                           "(default: 3; negative disables)")
    p_gc.add_argument("--dry-run", action="store_true",
                      help="List the workspaces gc would try to remove; delete nothing")

    # --- clone ---
    p_clone = sub.add_parser(
        "clone",
        help="git clone that borrows objects from a shared fleet mirror",
        description=(
            "Clone a repo into a kanban workspace. For ANG-Ventures/* and "
            "Kyzcreig/* GitHub URLs the clone uses --reference-if-able "
            "against a bare mirror under <hermes root>/mirrors/, created "
            "lazily, so the checkout stores only objects the mirror lacks. "
            "Other URLs are cloned normally. Common git-clone options "
            "(-q, -b, --depth, --filter, --no-checkout, --single-branch, "
            "--no-tags, --origin, ...) are forwarded."
        ),
    )
    from hermes_cli.kanban_clone import add_arguments as _add_clone_arguments

    _add_clone_arguments(p_clone)

    # --- home-lint ---
    p_hl = sub.add_parser(
        "home-lint",
        help="Report open cards with no home session (exit 1); "
             "--backfill stamps them 'unhomed'",
    )
    p_hl.add_argument("--backfill", action="store_true",
                      help="Stamp each homeless open card 'unhomed' + comment")
    p_hl.add_argument("--dry-run", action="store_true",
                      help="With --backfill: list what would be stamped")
    p_hl.add_argument("--json", action="store_true")

    # --- repair ---
    p_repair = sub.add_parser(
        "repair",
        help="Check kanban.db integrity and auto-repair index-only corruption",
        description=(
            "Runs PRAGMA integrity_check on the board's DB and reports the "
            "result. When the failure consists only of index-scoped errors "
            "('wrong # of entries in index <name>' / 'row N missing from "
            "index <name>'), the corrupt file is quarantined to a "
            ".corrupt.<hash>.bak sibling first and the damaged indexes are "
            "rebuilt with REINDEX — the same narrow auto-repair the "
            "connect-time guard applies. Any other corruption class is "
            "reported and left untouched (fail-closed). Exits 0 when the DB "
            "is healthy or was repaired, non-zero when it is still corrupt."
        ),
    )
    p_repair.add_argument("--json", action="store_true",
                          help="Emit the repair report as JSON")
    p_repair.add_argument("--reclassify-quota-crashes", action="store_true",
                          help="Reclassify recorded quota crashes without counting task failures")
    p_repair.add_argument("--dry-run", action="store_true",
                          help="Report quota repairs without writes or schema migration")
    p_repair.add_argument("--board", default=argparse.SUPPRESS,
                          help="Board to repair (also accepted before the repair verb)")

    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    for _name in _HOME_GUARDED_ACTIONS:
        _p = sub.choices.get(_name)
        if _p is not None and "foreign_ok" not in {a.dest for a in _p._actions}:
            _p.add_argument(
                "--takeover",
                "--foreign-ok",
                dest="foreign_ok",
                default=None,
                metavar="REASON",
                help="Act on a card whose home session is another session "
                     "(or an unhomed card); records a takeover event and "
                     "posts REASON as a comment the home session sees.",
            )
    return kanban_parser


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …`` argparse dispatch.

    Returns a shell-style exit code (0 on success, non-zero on error).
    """
    action = getattr(args, "kanban_action", None)
    if not action:
        # No subaction given: print help via the stored parser reference.
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(
                "usage: hermes kanban <action> [options]\n"
                "Run 'hermes kanban --help' for the full list of actions.",
                file=sys.stderr,
            )
        return 0

    # Fast-fail for clearer CLI UX only. The durable trust boundary is lower in
    # hermes_cli.kanban_db, because children can import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        print(
            "kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI",
            file=sys.stderr,
        )
        return 1

    # Board-management commands operate on board metadata and the persisted
    # current-board pointer itself. They must ignore the shared `--board`
    # task-routing override; otherwise `/kanban --board beta boards show`
    # reports beta as the current board even when the on-disk pointer is
    # alpha.
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` applies to every subcommand below by way of an
    # env-var pin for the duration of this call. Using HERMES_KANBAN_BOARD
    # (rather than threading `board=` through 50+ kb.connect() sites)
    # keeps the patch small and inherits the exact same resolution the
    # dispatcher uses for workers — consistency is a feature here.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if not normed:
            print("kanban: --board requires a slug", file=sys.stderr)
            return 2
        # Boards other than 'default' must already exist — typoed slugs
        # would otherwise silently create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            print(
                f"kanban: board {normed!r} does not exist. "
                f"Create it with `hermes kanban boards create {normed}`.",
                file=sys.stderr,
            )
            return 1
        board_scope = kb.scoped_current_board(normed)

    # Auto-initialize the DB before dispatching any subcommand. init_db
    # is idempotent, so running it every invocation is cheap (one
    # SELECT against sqlite_master when tables already exist) and
    # prevents "no such table: tasks" on first use from a fresh
    # HERMES_HOME. Previously only `init` and `daemon` triggered
    # schema creation; `create` / `list` / every other command would
    # error out on a fresh install.
    with board_scope:
        # `repair` must dispatch BEFORE the auto-init below: on a corrupt DB
        # init_db() itself raises KanbanDbCorruptError, which would turn
        # every `hermes kanban repair` into "could not initialize database"
        # without ever reaching the repair path.
        if action == "repair":
            return _cmd_repair(args)
        # `clone` is a git operation with no board state; it must not depend
        # on (or initialize) kanban.db.
        if action == "clone":
            from hermes_cli.kanban_clone import clone

            return clone(args.url, args.dest, args.git_opts)
        try:
            kb.init_db()
        except Exception as exc:
            print(f"kanban: could not initialize database: {exc}", file=sys.stderr)
            return 1

        handlers = {
            "init":     _cmd_init,
            "create":   _cmd_create,
            "budget":   _cmd_budget,
            "swarm":    _cmd_swarm,
            "list":     _cmd_list,
            "ls":       _cmd_list,
            "show":     _cmd_show,
            "assign":   _cmd_assign,
            "set-model": _cmd_set_model,
            "lane-model": _cmd_lane_model,
            "reclaim":  _cmd_reclaim,
            "reassign": _cmd_reassign,
            "diagnostics": _cmd_diagnostics,
            "diag":     _cmd_diagnostics,
            "link":     _cmd_link,
            "unlink":   _cmd_unlink,
            "claim":    _cmd_claim,
            "comment":  _cmd_comment,
            "attach":   _cmd_attach,
            "attachments": _cmd_attachments,
            "attach-rm": _cmd_attach_rm,
            "complete": _cmd_complete,
            "edit":     _cmd_edit,
            "update":   _cmd_edit,
            "block":    _cmd_block,
            "schedule": _cmd_schedule,
            "unblock":  _cmd_unblock,
            "requeue":  _cmd_requeue,
            "reopen":   _cmd_reopen,
            "request-review": _cmd_request_review,
            "request-changes": _cmd_request_changes,
            "reopen-review":  _cmd_reopen_review,
            "promote":  _cmd_promote,
            "triage-resolve": _cmd_triage_resolve,
            "archive":  _cmd_archive,
            "tail":     _cmd_tail,
            "dispatch": _cmd_dispatch,
            "daemon":   _cmd_daemon,
            "watch":    _cmd_watch,
            "stats":    _cmd_stats,
            "log":      _cmd_log,
            "runs":     _cmd_runs,
            "heartbeat": _cmd_heartbeat,
            "assignees": _cmd_assignees,
            "notify-subscribe":   _cmd_notify_subscribe,
            "notify-list":        _cmd_notify_list,
            "notify-unsubscribe": _cmd_notify_unsubscribe,
            "notify-repair":      _cmd_notify_repair,
            "context":  _cmd_context,
            "specify":  _cmd_specify,
            "decompose":  _cmd_decompose,
            "gc":       _cmd_gc,
            "home-lint": _cmd_home_lint,
        }
        handler = handlers.get(action)
        if not handler:
            print(f"kanban: unknown action {action!r}", file=sys.stderr)
            return 2
        actor_scope = contextlib.nullcontext()
        caller_sid = _caller_session_id() if action in _HOME_GUARDED_ACTIONS else None
        if action in _HOME_GUARDED_ACTIONS and not caller_sid:
            # No chat-session identity (plain shell, cron opener, launchd):
            # the guard is session-vs-session, so an unattributed caller is
            # never refused -- behaviour is exactly as before the guard.
            print(
                "note: no session identity \u2014 home-session guard skipped",
                file=sys.stderr,
            )
        elif action in _HOME_GUARDED_ACTIONS:
            actor_scope = kb.mutation_actor(
                session_ids=(caller_sid,),
                profile=_profile_author(),
                foreign_ok=getattr(args, "foreign_ok", None),
                surface="cli",
            )
        try:
            with actor_scope:
                return int(handler(args) or 0)
        except (ValueError, RuntimeError) as exc:
            # A survivor refusal carries its operator-only hint on the
            # exception, not in the persisted message, so render it HERE --
            # at the boundary whose environment belongs to the caller actually
            # reading the text. See kanban_survivor.render_override_hint.
            from hermes_cli.kanban_survivor import render_override_hint

            print(f"kanban: {render_override_hint(exc)}", file=sys.stderr)
            return 1


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# Status/ownership subcommands policed by the home-session guard
# (kanban_db.check_home_session). Runtime actions (dispatch, daemon, gc,
# heartbeat) are deliberately absent: they are the execution lane. ``claim``
# is guarded: the verb is chat-reachable and a foreign claim would hold the
# home card's lease (the assignee / its dispatched worker stay exempt).
_HOME_GUARDED_ACTIONS: frozenset[str] = frozenset({
    "claim", "complete", "block", "unblock", "archive", "assign", "reassign",
    "reclaim", "set-model", "edit", "update", "promote", "triage-resolve",
    "schedule", "requeue", "reopen", "reopen-review", "request-review",
    "request-changes", "link", "specify", "decompose",
})


# The invoking chat session when the CLI runs IN-PROCESS for a gateway
# ``/kanban`` slash command. The gateway passes it explicitly to
# :func:`run_slash`; env is never consulted there (it is process-global and
# shared by every concurrent session).
_SLASH_SESSION_ID: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "kanban_slash_session_id", default=None
)


def _caller_session_id() -> Optional[str]:
    """THE caller-identity read for every CLI site: the ``create`` stamp
    (:func:`_resolve_session_flag`), ``show``'s home label, ``list --home`` and
    the guard's actor binding. Order: explicit slash-path session, then the
    ContextVar-first resolver shared with ``tools/kanban_tools``, then env."""
    explicit = (_SLASH_SESSION_ID.get() or "").strip()
    if explicit:
        return explicit
    try:
        from gateway.session_context import resolve_current_session_id

        resolved = (resolve_current_session_id() or "").strip()
        if resolved:
            return resolved
        if os.environ.get("_HERMES_GATEWAY") == "1":
            return None  # in-process: env is another session's, never ours
    except Exception:
        pass
    return (os.environ.get("HERMES_SESSION_ID") or "").strip() or None


def _resolve_session_flag(value: Optional[str]) -> Optional[str]:
    """``--session`` for create: explicit id, 'none' = NULL, omitted = env."""
    if value is None:
        return _caller_session_id()
    value = value.strip()
    return None if value.lower() in ("", "none") else value


def _home_label(session_id: Optional[str], *, unhomed: bool = False) -> str:
    if unhomed:
        return "unhomed (no session owns it; --takeover to act)"
    if not session_id:
        return "unstamped"
    caller = _caller_session_id()
    if caller and session_id in kb.home_ids(caller):
        return "this-session"
    return f"other ({session_id})"


def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init",
    "create",
    "swarm",
    "assign",
    "reclaim",
    "reassign",
    "link",
    "unlink",
    "claim",
    "comment",
    "attach",
    "attach-rm",
    "complete",
    "edit",
    "update",
    "block",
    "schedule",
    "unblock",
    "requeue",
    "reopen",
    "promote",
    "triage-resolve",
    "archive",
    "dispatch",
    "daemon",
    "repair",
    "heartbeat",
    "notify-subscribe",
    "notify-unsubscribe",
    "specify",
    "decompose",
    "gc",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create",
    "new",
    "rm",
    "remove",
    "delete",
    "switch",
    "use",
    "rename",
    "set-default-workdir",
})


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        boards_action = getattr(args, "boards_action", None) or "list"
        if boards_action not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    try:
        from agent.delegation_context import is_delegated_child_process_context

        return is_delegated_child_process_context()
    except Exception:
        return bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))


# ---------------------------------------------------------------------------
# Boards management (hermes kanban boards …)
# ---------------------------------------------------------------------------

def _dispatch_boards(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban boards <action>``.

    Boards management is deliberately separate from the task-level
    commands: it operates on the filesystem (board directories,
    ``current`` pointer, ``board.json``), not on the per-board SQLite
    DB, so a fresh HERMES_HOME that has never called ``kanban init``
    can still run ``boards create`` / ``boards list``.
    """
    sub = getattr(args, "boards_action", None) or "list"
    if sub in {"list", "ls"}:
        return _cmd_boards_list(args)
    if sub in {"create", "new"}:
        return _cmd_boards_create(args)
    if sub in {"rm", "remove", "delete"}:
        return _cmd_boards_rm(args)
    if sub in {"switch", "use"}:
        return _cmd_boards_switch(args)
    if sub in {"show", "current"}:
        return _cmd_boards_show(args)
    if sub == "rename":
        return _cmd_boards_rename(args)
    if sub == "set-default-workdir":
        return _cmd_boards_set_default_workdir(args)
    if sub == "export":
        return _cmd_boards_export(args)
    if sub == "import":
        return _cmd_boards_import(args)
    print(f"kanban boards: unknown action {sub!r}", file=sys.stderr)
    return 2


def _board_task_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe to call on an empty DB."""
    try:
        # Called once per board by ``boards list`` — enumeration, not
        # addressing. The extent covers ``connect_closing`` too, which
        # re-resolves the path internally.
        with kb.enumerating_boards():
            path = kb.kanban_db_path(board=slug)
            if not path.exists():
                return {}
            with kb.connect_closing(board=slug) as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _cmd_boards_list(args: argparse.Namespace) -> int:
    include_archived = bool(getattr(args, "all", False))
    boards = kb.list_boards(include_archived=include_archived)
    # Enrich each entry with task counts + whether it's the current board.
    current = kb.get_current_board()
    # Enumeration: the enrich loop asks every board on disk for its counts.
    # Scoping the loop body (not just `_board_task_counts`' own extent) keeps
    # any future per-board call added here covered by construction.
    for b in kb.enumerating_each(boards):
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_task_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    if getattr(args, "json", False):
        print(json.dumps(boards, indent=2, ensure_ascii=False))
        return 0
    # Human table: marker (•) for current, slug, display name, counts.
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        counts = b["counts"] or {}
        counts_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            or "(empty)"
        )
        name = b.get("name") or ""
        if b.get("archived"):
            name += " [archived]"
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {counts_str}")
    print()
    print(f"Current board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    return 0


def _cmd_boards_create(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards create: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards create: slug is required", file=sys.stderr)
        return 2
    already = kb.board_exists(normed) and normed != kb.DEFAULT_BOARD
    meta = kb.create_board(
        normed,
        name=args.name,
        description=args.description,
        icon=args.icon,
        color=args.color,
        default_workdir=args.default_workdir,
    )
    verb = "already exists" if already else "created"
    print(f"Board {meta['slug']!r} {verb}.")
    print(f"  Display name: {meta.get('name', '')}")
    print(f"  DB path:      {meta['db_path']}")
    if getattr(args, "switch", False):
        kb.set_current_board(meta["slug"])
        print(f"  Switched to {meta['slug']!r}.")
    else:
        print(f"  Use `hermes kanban boards switch {meta['slug']}` to make it current.")
    return 0


def _cmd_boards_rm(args: argparse.Namespace) -> int:
    # When the user runs `hermes kanban boards delete <slug>` (alias), the
    # boards_action is 'delete' but args.delete is never set to True because
    # the --delete flag belongs to the 'rm' subparser only.  Detect the alias
    # and treat it identically to `boards rm --delete` (fixes #23139).
    force_delete = getattr(args, "delete", False) or getattr(args, "boards_action", "") == "delete"
    try:
        res = kb.remove_board(args.slug, archive=not force_delete)
    except ValueError as exc:
        print(f"kanban boards rm: {exc}", file=sys.stderr)
        return 1
    if res["action"] == "archived":
        print(f"Board {res['slug']!r} archived → {res['new_path']}")
        print("Recover by moving the directory back to "
              "<root>/kanban/boards/<slug>/.")
    else:
        print(f"Board {res['slug']!r} deleted.")
    return 0


def _cmd_boards_switch(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards switch: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards switch: slug is required", file=sys.stderr)
        return 2
    if not kb.board_exists(normed):
        print(
            f"kanban boards switch: board {normed!r} does not exist. "
            f"Create it with `hermes kanban boards create {normed}`.",
            file=sys.stderr,
        )
        return 1
    kb.set_current_board(normed)
    print(f"Active board is now {normed!r}.")
    return 0


def _cmd_boards_show(args: argparse.Namespace) -> int:
    current = kb.get_current_board()
    meta = kb.read_board_metadata(current)
    counts = _board_task_counts(current)
    total = sum(counts.values())
    print(f"Current board: {current}")
    print(f"  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}")
    print(f"  Tasks:        {total} total"
          + (f" ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})"
             if counts else ""))
    return 0


def _cmd_boards_rename(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards rename: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards rename: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, name=args.name)
    print(f"Board {normed!r} renamed to {meta['name']!r}.")
    return 0


def _cmd_boards_set_default_workdir(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards set-default-workdir: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards set-default-workdir: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, default_workdir=args.path)
    new_val = meta.get("default_workdir")
    if new_val:
        print(f"Board {normed!r} default workdir set to {new_val!r}.")
    else:
        print(f"Board {normed!r} default workdir cleared.")
    return 0


def _cmd_boards_export(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_transfer
    from hermes_cli.sizefmt import format_bytes

    slug = args.slug or kb.get_current_board()
    output = args.output or f"{slug}.tar.gz"
    try:
        res = kanban_transfer.export_board(
            slug,
            output,
            include_attachments=not args.no_attachments,
            include_logs=args.include_logs,
        )
    except (OSError, ValueError) as exc:
        print(f"kanban boards export: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    counts = res["counts"]
    print(f"Exported board {res['board']!r} → {res['archive']}")
    print(f"  Size:        {format_bytes(res['size'])}")
    print(f"  Tasks:       {counts['tasks']}")
    print(f"  Comments:    {counts['task_comments']}")
    print(f"  Attachments: {counts['attachment_files']}")
    print("Import it with `hermes kanban boards import <archive>`.")
    return 0


def _cmd_boards_import(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_transfer

    try:
        res = kanban_transfer.import_board(
            args.archive, args.as_slug, activate=args.switch
        )
    except (OSError, ValueError) as exc:
        print(f"kanban boards import: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    print(f"Imported board {res['board']!r} ({res['name']}).")
    if res["renamed"]:
        print(f"  Renamed from {res['requested_board']!r} — that slug was taken.")
    print(f"  Path:  {res['path']}")
    print(f"  Tasks: {res['counts']['tasks']}")
    for warning in res["warnings"]:
        print(f"  Note:  {warning}")
    if res["activated"]:
        print(f"  Active board is now {res['board']!r}.")
    else:
        print(f"  Switch to it with `hermes kanban boards switch {res['board']}`.")
    return 0


# ---------------------------------------------------------------------------


def _parse_duration(val) -> Optional[int]:
    """Parse ``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds.

    Returns None for empty input. Raises ValueError on malformed input so
    the CLI can surface a usage error cleanly.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    # Bare integer → seconds.
    try:
        return int(s)
    except ValueError:
        pass
    # Suffixed form.
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            n = float(s[:-1])
        except ValueError as exc:
            raise ValueError(f"malformed duration {val!r}") from exc
        return int(n * units[s[-1]])
    raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")

    print()
    # Enumerate profiles on disk so the user knows what assignees are
    # already addressable. Multica does this auto-detection on its
    # daemon start; we do it here at init time instead because our
    # dispatcher doesn't need to enumerate — we just pass the name
    # through to `hermes -p <name>`.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can "
              f"be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.")
        print("Create one with `hermes -p <name> setup` before assigning tasks.")
    print()
    print("Next step: start the gateway so ready tasks actually get picked up.")
    print("  hermes gateway start")
    print()
    print(
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.heartbeat_worker(
            conn,
            args.task_id,
            note=getattr(args, "note", None),
            expected_run_id=_worker_run_id_for(args.task_id),
        )
    if not ok:
        print(f"cannot heartbeat {args.task_id} (not running?)", file=sys.stderr)
        return 1
    print(f"Heartbeat recorded for {args.task_id}")
    return 0


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    # Header
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        counts = entry["counts"] or {}
        count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(idle)"
        print(f"{entry['name']:20s}  {on_disk:8s}  {count_str}")
    return 0


def _maybe_cli_auto_subscribe(conn, task_id: str) -> bool:
    """Subscribe the originating chat to ``task_id`` when configured.

    Gated by ``kanban.cli_auto_subscribe`` (default False). ``hermes kanban
    create`` deliberately does not auto-subscribe by default — subscribing
    every CLI call was reverted upstream (#19718 / #19721) because scripts
    and cron jobs also drive the CLI. When the knob is on, only a create
    carrying a full gateway session identity (HERMES_SESSION_PLATFORM +
    HERMES_SESSION_CHAT_ID, read via the stale-safe
    ``gateway.session_context.get_session_env``) subscribes; bare CLI /
    cron / script creates stay silent regardless of the knob.

    Returns True when a subscription row was written. Never raises — a
    notification bookkeeping failure must not fail the create.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        if not cfg_get(load_config(), "kanban", "cli_auto_subscribe", default=False):
            return False
        from tools.kanban_tools import subscribe_calling_session

        return subscribe_calling_session(
            conn, task_id, require_platform_identity=True
        )
    except Exception:
        return False


def _cmd_create(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_worker_policy as _kwp

    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    if branch_name and ws_kind != "worktree":
        print("kanban: --branch is only valid with --workspace worktree", file=sys.stderr)
        return 2
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban: --max-runtime: {exc}", file=sys.stderr)
        return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban: --max-retries must be >= 1 (got {max_retries}); "
            "use 1 to trip on the first failure.",
            file=sys.stderr,
        )
        return 2
    try:
        with kb.connect_closing() as conn:
            task_id = kb.create_task(
                conn,
                title=args.title,
                body=args.body,
                assignee=args.assignee,
                created_by=args.created_by or _profile_author(),
                workspace_kind=ws_kind,
                workspace_path=ws_path,
                branch_name=branch_name,
                project_id=getattr(args, "project", None),
                tenant=args.tenant,
                priority=args.priority,
                parents=tuple(args.parent or ()),
                parents_kind=getattr(args, "parent_kind", None),
                triage=bool(getattr(args, "triage", False)),
                idempotency_key=getattr(args, "idempotency_key", None),
                max_runtime_seconds=max_runtime,
                skills=getattr(args, "skills", None) or None,
                max_retries=max_retries,
                model_override=getattr(args, "model_override", None),
                provider_override=getattr(args, "provider_override", None),
                flagship_override_reason=getattr(args, "allow_flagship", None),
                flagship_override_author=args.created_by or _profile_author(),
                reasoning_effort=getattr(args, "reasoning_effort", None),
                goal_mode=bool(getattr(args, "goal_mode", False)),
                goal_max_turns=getattr(args, "goal_max_turns", None),
                initial_status=getattr(args, "initial_status", "running"),
                forced_status=_kwp.resolve_park_status(
                    initial_status=getattr(args, "initial_status", "running"),
                    triage=bool(getattr(args, "triage", False)),
                ),
                session_id=_resolve_session_flag(getattr(args, "session", None)),
            )
            task = kb.get_task(conn, task_id)
            auto_subscribed = _maybe_cli_auto_subscribe(conn, task_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(_task_to_dict(task), indent=2, ensure_ascii=False))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        if auto_subscribed:
            print(
                "Subscribed the calling session for finish notifications "
                "(kanban.cli_auto_subscribe)."
            )

        # Warn when the task would sit in `ready` because no dispatcher is
        # present. Only warn on ready+assigned tasks — triage/todo are
        # expected to sit idle until promoted, and unassigned tasks
        # can't be dispatched. Skipped in --json mode so the stdout
        # stream stays strictly machine-parseable for callers (the JSON
        # response itself carries enough info for them to decide if
        # they want to check dispatcher presence separately).
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_budget(args: argparse.Namespace) -> int:
    """Read-only spend report. Never writes a pause marker or pages."""
    from hermes_cli import kanban_budget as kbudget

    rows = kbudget.board_budget_report(getattr(args, "budget_board", None))
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(kbudget.format_budget_report(rows))
    if rows and rows[0].get("ceiling_usd") is None:
        print(
            "\nNo ceiling configured — set kanban.budget.usd_per_24h in "
            "config.yaml to brake runaway fan-out."
        )
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        print(f"kanban swarm: {exc}", file=sys.stderr)
        return 2
    if not workers:
        print("kanban swarm: at least one --worker is required", file=sys.stderr)
        return 2
    with kb.connect_closing() as conn:
        created = ks.create_swarm(
            conn,
            goal=args.goal,
            workers=workers,
            verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer,
            tenant=args.tenant,
            created_by=args.created_by or _profile_author(),
            priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        print(json.dumps(created.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Swarm root: {created.root_id}")
        print("Workers: " + ", ".join(created.worker_ids))
        print(f"Verifier: {created.verifier_id}")
        print(f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    if getattr(args, "home", False):
        home = _caller_session_id()
        if not home:
            print(
                "kanban: --home needs $HERMES_SESSION_ID (not set in this "
                "shell); pass --session <id> explicitly",
                file=sys.stderr,
            )
            return 2
        if args.session and args.session != home:
            print("kanban: --home conflicts with --session", file=sys.stderr)
            return 2
        # Home = this session's lineage (id rotations inside one chat).
        home_session_ids = kb.home_ids(home)
        args.session = None
    else:
        home_session_ids = None
    with kb.connect_closing() as conn:
        # Cheap "mini-dispatch": recompute ready so list output reflects
        # dependencies that may have cleared since the last dispatcher tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn,
            assignee=assignee,
            status=args.status,
            tenant=args.tenant,
            session_id=args.session,
            session_ids=home_session_ids,
            include_archived=args.archived,
            order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id,
            current_step_key=args.current_step_key,
        )
        # Board-level triage/stranding health, computed after the same
        # recompute the dispatcher would do. Read-only and best-effort — a
        # listing must never fail because a banner couldn't be built.
        try:
            triage_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM tasks WHERE status = 'triage' ORDER BY id"
                )
            ]
            stranded = kb.find_stranded_by_triage(conn)
        except Exception:
            triage_ids, stranded = [], []
    if getattr(args, "json", False):
        print(json.dumps([_task_to_dict(t) for t in tasks], indent=2, ensure_ascii=False))
        return 0
    # Passive discoverability: when the user has multiple boards, surface
    # which one they're looking at in the list header. Single-board users
    # never see this — the feature stays invisible until you opt in.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        current = kb.get_current_board()
        other_count = len(all_boards) - 1
        print(
            f"Board: {current} "
            f"({other_count} other board{'s' if other_count != 1 else ''} — "
            f"`hermes kanban boards list`)\n"
        )
    _print_triage_banner(triage_ids, stranded)
    if not tasks:
        print("(no matching tasks)")
        return 0
    caller = None
    if not (getattr(args, "flat_all", False) or args.session
            or home_session_ids is not None):
        caller = _caller_session_id()
    if not caller:
        for t in tasks:
            print(_fmt_task_line(t))
        return 0
    print(_format_session_grouped(tasks, kb.home_ids(caller)))
    return 0


def _cmd_home_lint(args: argparse.Namespace) -> int:
    """Silent + exit 0 when every open card has a home; else list ids, exit 1.

    Designed for a no_agent cron (empty stdout = nothing delivered). With
    ``--backfill`` stamps the stragglers ``unhomed`` and exits 0.
    """
    with kb.connect_closing() as conn:
        if getattr(args, "backfill", False):
            ids = kb.backfill_unhomed(conn, dry_run=bool(args.dry_run))
            verb = "would stamp" if args.dry_run else "stamped"
            if args.json:
                print(json.dumps({"action": verb, "ids": ids}))
            elif ids:
                print(f"home-lint: {verb} {len(ids)} card(s) unhomed: {', '.join(ids)}")
            return 0
        ids = kb.find_homeless_open_tasks(conn)
    if args.json:
        print(json.dumps({"homeless_open": ids}))
    elif ids:
        print(
            f"kanban home-lint: {len(ids)} open card(s) have NO home session "
            f"(a create path is not stamping): {', '.join(ids)} -- "
            "fix the path; `hermes kanban home-lint --backfill` stamps them unhomed"
        )
    return 1 if ids else 0


def _format_session_grouped(tasks, home: "frozenset[str]") -> str:
    """Session-first listing: this session's cards in full, every other
    session's cards collapsed to one ``id · status · title`` line each.

    A session sees its OWN work first; foreign cards stay visible (for
    mentions/comments) but read as someone else's. ``--all`` = flat view.
    """
    mine = [t for t in tasks if t.session_id and t.session_id in home]
    others = [t for t in tasks if not (t.session_id and t.session_id in home)]
    lines = [f"THIS SESSION ({len(mine)})"]
    lines += [_fmt_task_line(t) for t in mine] or ["  (none)"]
    lines.append("")
    lines.append(
        f"OTHER SESSIONS ({len(others)}) -- not yours: comment, don't act "
        "(--takeover REASON to act; --all for the flat view)"
    )
    for t in others:
        tag = " [unhomed]" if t.unhomed else ""
        lines.append(f"  {t.id} \u00b7 {t.status} \u00b7 {t.title}{tag}")
    return "\n".join(lines)


def _print_triage_banner(triage_ids, stranded) -> None:
    """Header naming cards that need a human, and what they're freezing.

    ``triage`` is the only status no automation will ever clear — it is
    reached precisely because an unblocker gave up. In a flat listing it reads
    like any other bucket, so the cards that most need attention are the least
    visible, and their stranded children are invisible entirely.
    """
    ids = list(triage_ids or [])
    if not ids:
        return
    print(
        f"TRIAGE: {len(ids)} card(s) need a human decision — "
        f"{', '.join(ids)}"
    )
    stranded_kids = sorted({
        child for child, parent in (stranded or []) if parent in set(ids)
    })
    if stranded_kids:
        print(
            f"  ...stranding {len(stranded_kids)} downstream task(s): "
            f"{', '.join(stranded_kids)}"
        )
    print(
        "  resolve: hermes kanban triage-resolve <id> "
        "--to todo|done|archived --reason \"...\"\n"
    )


def _cmd_show(args: argparse.Namespace) -> int:
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban show: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    graph = None
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parent_links = kb.parent_links(conn, args.task_id)
        child_links = kb.child_links(conn, args.task_id)
        parents = [pid for pid, _kind in parent_links]
        children = [cid for cid, _kind in child_links]
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via ``task_runs.summary``; ``tasks.result`` is left NULL unless the caller explicitly passed
        # ``result=``. Surfacing the latest summary here keeps ``show`` from
        # looking like a no-op when the worker actually did real work.
        latest_summary = kb.latest_summary(conn, args.task_id)
        if not getattr(args, "json", False):
            graph = kb.task_graph_context(conn, task.id)

    if getattr(args, "json", False):
        payload = {
            "task": _task_to_dict(task),
            "home": _home_label(task.session_id, unhomed=task.unhomed),
            "latest_summary": latest_summary,
            "parents": parents,
            "children": children,
            "parent_links": [
                {"id": pid, "kind": kind} for pid, kind in parent_links
            ],
            "child_links": [
                {"id": cid, "kind": kind} for cid, kind in child_links
            ],
            "comments": [
                {
                    "author": c.author,
                    "author_display": kb.format_comment_author(
                        c.author, run_id=c.run_id, session_ref=c.session_ref
                    ),
                    "run_id": c.run_id,
                    "session_ref": c.session_ref,
                    "body": c.body,
                    "created_at": c.created_at,
                }
                for c in comments
            ],
            "events": [
                {
                    "kind": e.kind,
                    "payload": e.payload,
                    "created_at": e.created_at,
                    "run_id": e.run_id,
                }
                for e in events
            ],
            "runs": [
                {
                    "id": r.id,
                    "profile": r.profile,
                    "step_key": r.step_key,
                    "status": r.status,
                    "outcome": r.outcome,
                    "summary": r.summary,
                    "error": r.error,
                    "metadata": r.metadata,
                    "worker_pid": r.worker_pid,
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                for r in runs
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Task {task.id}: {task.title}")
    print(f"  status:    {task.status}")
    print(f"  assignee:  {task.assignee or '-'}")
    print(f"  session:   {task.session_id or (kb.UNHOMED_SESSION if task.unhomed else '-')}")
    print(f"  home:      {_home_label(task.session_id, unhomed=task.unhomed)}")
    if task.tenant:
        print(f"  tenant:    {task.tenant}")
    print(f"  workspace: {task.workspace_kind}" +
          (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        print(f"  branch:    {task.branch_name}")
    if task.skills:
        print(f"  skills:    {', '.join(task.skills)}")
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        print(f"  model:     {task.model_override}{_prov}")
    print(f"  reasoning: {task.reasoning_effort or 'inherit'}")
    # Effective retry threshold. Show the per-task override if set,
    # otherwise the dispatcher's resolved value from config (or the
    # default if config doesn't set it either). Helps operators see
    # why a task auto-blocked earlier/later than they expected.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            cfg_val = (cfg.get("kanban", {}) or {}).get("failure_limit")
        except Exception:
            cfg_val = None
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    print(f"  created:   {_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics section — surface active distress signals at the top
    # of show output so CLI users see them before scrolling through
    # comments / runs.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph)
    if diags:
        sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
        print(f"\n  Diagnostics ({len(diags)}):")
        for d in diags:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.title}")
            if d.data:
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Only show suggested actions in show output to keep it tight;
            # full list is available via `kanban diagnostics --task <id>`.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
    if task.started_at:
        print(f"  started:   {_fmt_ts(task.started_at)}")
    if task.completed_at:
        print(f"  completed: {_fmt_ts(task.completed_at)}")
    if parents:
        print(f"  parents:   {_fmt_links(parent_links)}")
    if children:
        print(f"  children:  {_fmt_links(child_links)}")
    if task.body:
        print()
        print("Body:")
        print(task.body)
    if task.result:
        print()
        print("Result:")
        print(task.result)
    elif latest_summary:
        # Worker handoff lives on the latest run, not on tasks.result.
        # Surface it at top-level so a glance at ``hermes kanban show <id>``
        # tells you what the worker did even if tasks.result is empty.
        print()
        print("Latest summary:")
        print(latest_summary)
    if comments:
        print()
        print(f"Comments ({len(comments)}):")
        for c in comments:
            author_disp = kb.format_comment_author(
                c.author, run_id=c.run_id, session_ref=c.session_ref
            )
            print(f"  [{_fmt_ts(c.created_at)}] {author_disp}: {c.body}")
    if events:
        print()
        print(f"Events ({len(events)}):")
        for e in events[-20:]:
            pl = f" {e.payload}" if e.payload else ""
            run_tag = f" [run {e.run_id}]" if e.run_id else ""
            print(f"  [{_fmt_ts(e.created_at)}]{run_tag} {e.kind}{pl}")
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = (max(0, r.ended_at - r.started_at)
                       if r.ended_at else None)
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  "
                  f"{_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    with kb.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    if not ok:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 1
    print(f"Assigned {args.task_id} to {profile or '(unassigned)'}")
    return 0


_ACTIVE_STATUSES = ("ready", "running", "todo", "blocked", "triage", "scheduled", "review")
"""Statuses a batch selector treats as live work.

Terminal rows (``done``/``archived``) are excluded: re-routing a finished card
changes nothing and would make ``--all-active`` report work it didn't do.
"""


def _known_providers() -> set[str]:
    """Every provider name this install can actually route to.

    Union of the built-in registry (plugins under ``plugins/model-providers``)
    and the user's ``providers:`` config block, which is where fleet-local
    pools like ``claude-bpx-19`` are defined. Names and aliases both count.
    """

    known: set[str] = set()
    try:
        import providers as _providers

        for profile in _providers.list_providers():
            name = getattr(profile, "name", None)
            if name:
                known.add(str(name))
            for alias in (getattr(profile, "aliases", None) or ()):
                known.add(str(alias))
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        configured = load_config().get("providers")
        if isinstance(configured, dict):
            known.update(str(k) for k in configured)
    except Exception:
        pass
    return known


def _validate_provider(provider: Optional[str]) -> Optional[str]:
    """Return an error message when ``provider`` isn't routable, else None.

    A typo'd provider is otherwise invisible until a worker spawns and fails.
    If discovery yields no providers, refuse instead of persisting an
    unvalidated route; configured custom providers remain usable.
    """

    if not provider:
        return None
    known = _known_providers()
    if not known:
        return "provider discovery unavailable (registry and configured providers are empty); refusing unvalidated route"
    if provider in known:
        return None
    suggestions = difflib.get_close_matches(provider, sorted(known), n=3, cutoff=0.6)
    hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
    return f"unknown provider {provider!r}{hint}"


def _parse_ttl(value: str) -> int:
    """Parse ``30s``/``45m``/``2h``/``1d`` (or bare seconds) into seconds."""

    raw = str(value or "").strip().lower()
    if not raw:
        raise ValueError("--ttl needs a duration (e.g. 2h)")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    multiplier = units.get(raw[-1], 1) if raw[-1] in units else 1
    number = raw[:-1] if raw[-1] in units else raw
    try:
        magnitude = float(number)
    except ValueError:
        raise ValueError(
            f"bad --ttl {value!r}; use a duration like 30m, 2h, or 1d"
        ) from None
    seconds = int(magnitude * multiplier)
    if seconds <= 0:
        raise ValueError(
            f"bad --ttl {value!r}; a lane override must expire in the future"
        )
    return seconds


def _format_ttl(seconds: int) -> str:
    """Compact human duration for route/status lines."""

    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            value = seconds / size
            # Drop a trailing '.0' so 2h reads as "2h", not "2.0h". Formatting
            # the integer case separately avoids a str.replace() here, which
            # the gate-dominance lint flags as a relocating call.
            if value == int(value):
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
    return f"{seconds}s"


def _parse_where(clauses: list[str]) -> tuple[dict[str, list[str]], Optional[str]]:
    """Parse ``--where status=running,ready assignee=x`` into a filter dict."""

    filters: dict[str, list[str]] = {}
    allowed = {"status", "assignee"}
    for clause in clauses or []:
        key, sep, raw = str(clause).partition("=")
        key = key.strip().lower()
        if not sep or not key:
            return {}, f"bad --where clause {clause!r}; expected key=value"
        if key not in allowed:
            return {}, (
                f"unsupported --where key {key!r}; "
                f"supported: {', '.join(sorted(allowed))}"
            )
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            return {}, f"--where {key} needs at least one value"
        filters.setdefault(key, []).extend(values)
    return filters, None


def _select_batch_tasks(
    conn,
    *,
    task_ids: list[str],
    where: dict[str, list[str]],
    all_active: bool,
) -> tuple[list, Optional[str]]:
    """Resolve the batch selection into concrete tasks.

    Explicit ids are authoritative and a missing one is an error — an operator
    naming five cards must not silently get four.
    """

    if task_ids:
        tasks = []
        for task_id in task_ids:
            task = kb.get_task(conn, task_id)
            if task is None:
                return [], f"no such task: {task_id}"
            if task.status not in _ACTIVE_STATUSES:
                return [], f"cannot set model override on {task.status} task {task_id}"
            tasks.append(task)
        return tasks, None

    rows = [
        task for task in kb.list_tasks(conn, limit=100000)
        if task.status in _ACTIVE_STATUSES
    ]
    if where:
        statuses = {s.lower() for s in where.get("status", [])}
        assignees = {a for a in where.get("assignee", [])}
        if statuses:
            rows = [t for t in rows if (t.status or "").lower() in statuses]
        if assignees:
            rows = [t for t in rows if (t.assignee or "") in assignees]
    elif not all_active:
        return [], "set-model needs task ids, --where, or --all-active"
    return rows, None


def _run_batch_reclaims(
    conn,
    tasks: list,
    *,
    reason: str,
) -> tuple[list[tuple[str, bool]], dict[str, str]]:
    """Reclaim every card of an already-committed route batch, never raising.

    This runs AFTER ``apply_batch_route_writes`` has committed, because
    ``reclaim_task`` SIGTERMs a live worker — an irreversible side effect a
    rollback could not undo, so it must not live inside the route
    transaction.

    That ordering makes every failure down here a *post-commit* failure: the
    routes are already durable, and an exception escaping this loop takes the
    receipt naming them with it. The operator is then left believing nothing
    happened while N cards carry a new route. Enumerating exception types is
    not a fix for that — the previous version caught ``(ValueError,
    RuntimeError)`` and a real ``sqlite3.OperationalError`` ("database is
    locked") from ``reclaim_task``'s own ``BEGIN IMMEDIATE`` walked straight
    past it. So this function catches by POSITION, not by type: anything
    raised after the commit becomes a per-card refusal string and the caller
    always gets a receipt.

    ``BaseException`` is deliberate. ``KeyboardInterrupt`` / ``SystemExit``
    mid-batch is the same honesty problem, so they are recorded, the loop
    stops, the remaining cards are reported as not attempted, and the caller
    still prints the receipt and exits non-zero.

    Returns ``(applied, errors)`` where ``applied`` has one entry per task in
    order and ``errors`` maps a task id to why its reclaim did not happen.
    """
    applied: list[tuple[str, bool]] = []
    errors: dict[str, str] = {}
    interrupted = False
    for task in tasks:
        if interrupted:
            applied.append((task.id, False))
            errors[task.id] = "reclaim not attempted (batch interrupted)"
            continue
        # A card that was not claimed at selection time has nothing to
        # reclaim, and reclaim_task returning False for it is the designed
        # no-op — not a race. Only a card we SAW claimed and then could not
        # reclaim changed state underneath us, and that is worth naming.
        was_claimed = task.status == "running" or task.claim_lock is not None
        try:
            redispatched = bool(kb.reclaim_task(conn, task.id, reason=reason))
        except (KeyboardInterrupt, SystemExit) as exc:
            interrupted = True
            applied.append((task.id, False))
            errors[task.id] = (
                f"reclaim interrupted ({exc.__class__.__name__}); "
                "the worker may already have been signalled"
            )
            continue
        except BaseException as exc:  # noqa: BLE001 - see docstring
            applied.append((task.id, False))
            errors[task.id] = (
                f"{exc.__class__.__name__}: {exc or 'no detail'} "
                "(the worker may already have been signalled — check "
                f"`hermes kanban show {task.id}`)"
            )
            continue
        if not redispatched and was_claimed:
            errors[task.id] = (
                "prior claim was not reclaimed (card changed after selection; "
                "it may be claimed again — inspect current status)"
            )
        applied.append((task.id, redispatched))
    return applied, errors


# Any `t_*` token is treated as an INTENDED task id, not just well-formed hex.
# Matching only `t_[0-9a-f]+` would silently reclassify a typo'd id (`t_nope`)
# as the model name, so the command would "succeed" against the wrong thing
# instead of reporting `no such task`.
_TASK_ID_RE = re.compile(r"^t_\w+$", re.IGNORECASE)


def _cli_model_override(args, *, allow_ttl=False):
    """Parse the shared override object; explicit flags may not conflict with JSON."""
    from hermes_cli.model_override import parse_model_override

    text = getattr(args, "model_json", None)
    flags = {
        key: value for key, value in (
            ("model", getattr(args, "model", None)),
            ("provider", getattr(args, "provider", None)),
            ("reasoning_effort", getattr(args, "reasoning_effort", None)),
            # set-model/create: dest=allow_flagship; lane-model: dest=firepower.
            ("firepower", getattr(args, "allow_flagship", None) or getattr(args, "firepower", None)),
            ("ttl", getattr(args, "ttl", None)),
        ) if value is not None and (allow_ttl or key != "ttl")
    }
    if text is not None:
        if flags:
            raise ValueError("--model-json cannot be combined with model override flags")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--model-json: {exc}") from exc
    else:
        raw = flags or None
    # CLI checks the resolved per-card/per-lane route below, including
    # provider-only inheritance; keep its actionable --firepower wording.
    return parse_model_override(raw, field="model", allow_ttl=allow_ttl, enforce_firepower=False)


def _split_ids_and_model(
    positionals: list[str],
) -> tuple[list[str], Optional[str], bool]:
    """Split ``<id>... [model]`` into ids, model, and whether a model was given.

    Task ids are recognised by shape (``t_<hex>``) rather than position, so
    ``set-model t_a t_b model-x`` and ``set-model model-x --where …`` both
    parse unambiguously and a batch can't silently swallow its model as a
    sixth card id.
    """

    ids = [tok for tok in positionals if _TASK_ID_RE.match(tok)]
    rest = [tok for tok in positionals if not _TASK_ID_RE.match(tok)]
    if len(rest) > 1 or (ids and any(tok.startswith("t-") or tok.startswith("t_") for tok in rest)):
        raise ValueError(f"invalid task id or extra positional: {', '.join(rest)}")
    if not rest:
        return ids, None, False
    return ids, rest[0], True


def _cmd_set_model(args: argparse.Namespace) -> int:
    positionals = list(getattr(args, "task_ids", None) or [])
    try:
        parsed_ids, raw_model, model_given = _split_ids_and_model(positionals)
        if raw_model is not None and getattr(args, "model", None) is not None:
            raise ValueError("model given both positionally and with --model")
        if (getattr(args, "provider", None) and getattr(args, "reasoning_effort", None)
                and not (raw_model or getattr(args, "model", None) or getattr(args, "model_json", None))):
            raise ValueError("--provider requires a model when combined with --effort")
        args.model = raw_model if model_given else getattr(args, "model", None)
        if getattr(args, "reasoning_effort", None) is not None:
            args.reasoning_effort = kb.normalize_reasoning_effort(args.reasoning_effort)
        clearing = args.model is None or str(args.model).lower() in {"none", "-", "null", ""}
        if clearing and not getattr(args, "model_json", None):
            args.model = None
        override = _cli_model_override(args) if (not clearing or getattr(args, "model_json", None)
                                                 or getattr(args, "provider", None)
                                                 or getattr(args, "reasoning_effort", None)) else None
    except ValueError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    args.task_ids = parsed_ids
    model = override.model if override else None
    provider = override.provider if override else getattr(args, "provider", None)
    model_given = model_given or bool(model) or bool(provider)
    effort = override.reasoning_effort if override else getattr(args, "reasoning_effort", None)
    clear_effort = bool(getattr(args, "clear_effort", False))
    # --allow-flagship (main) and --firepower (alias) share dest=allow_flagship.
    firepower_reason = (
        override.firepower if override and override.firepower
        else getattr(args, "allow_flagship", None)
    )
    from hermes_cli.model_policy import (
        canonical_model_pair,
        firepower_guard_error,
        is_firepower_model,
        override_comment,
    )
    if model:
        model, provider = canonical_model_pair(model, provider)
    # The two knobs are independent: with --effort/--clear-effort and no
    # positional model, the model override is left untouched (absent means
    # "unchanged" here, not "clear"). Without either effort flag the
    # historical contract holds — a missing or 'none' positional clears the
    # model/provider override.
    touch_model = model_given or (effort is None and not clear_effort)
    if provider and not touch_model:
        print("kanban: --provider requires a model", file=sys.stderr)
        return 2
    guard_error = firepower_guard_error(model, firepower_reason)
    if guard_error:
        print(f"kanban: {guard_error}", file=sys.stderr)
        return 2
    if effort is not None:
        # Validate BEFORE any write so `set-model <id> <model> --effort typo`
        # can't half-apply (model committed, effort rejected).
        if not str(effort).strip():
            print(
                "kanban: --effort needs a level (use --clear-effort to unset)",
                file=sys.stderr,
            )
            return 2
        try:
            effort = kb.normalize_reasoning_effort(effort)
        except ValueError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
    # Provider typos are refused BEFORE any write, and for the whole batch —
    # a half-applied batch is worse than a rejected one because the operator
    # can't tell which cards moved.
    if touch_model and provider:
        provider_error = _validate_provider(provider)
        if provider_error:
            print(f"kanban: {provider_error}", file=sys.stderr)
            return 2

    where, where_error = _parse_where(getattr(args, "where", None) or [])
    if where_error:
        print(f"kanban: {where_error}", file=sys.stderr)
        return 2
    all_active = bool(getattr(args, "all_active", False))
    task_ids = list(getattr(args, "task_ids", None) or [])
    reclaim = bool(getattr(args, "reclaim", False))
    if task_ids and (where or all_active):
        print(
            "kanban: pass task ids OR a selector (--where/--all-active), not both",
            file=sys.stderr,
        )
        return 2

    # Declared before the try so the post-commit receipt path below can still
    # name what moved even if the `with` block raises after the routes were
    # written.
    tasks: list = []
    inherited_models: dict[str, str] = {}
    applied: list[tuple[str, bool]] = []
    cleared_routes: dict[str, str] = {}
    reclaim_errors: dict[str, str] = {}
    skipped: dict[str, str] = {}
    batch_error: Optional[str] = None
    committed = False
    try:
        with kb.connect_closing() as conn:
            tasks, select_error = _select_batch_tasks(
                conn, task_ids=task_ids, where=where, all_active=all_active,
            )
            if select_error:
                print(f"kanban: {select_error}", file=sys.stderr)
                return 2
            if not tasks:
                print("kanban: selector matched no tasks", file=sys.stderr)
                return 1
            # A provider-only object retains each card's current model. Resolve
            # the whole batch before writing so a missing profile route cannot
            # leave earlier cards half-updated.
            if touch_model and provider and not model:
                for task in tasks:
                    inherited = task.model_override or kb.effective_worker_route(task).split("/", 1)[-1]
                    if not inherited or inherited == "unknown":
                        print(f"kanban: cannot resolve model for {task.id}", file=sys.stderr)
                        return 2
                    inherited, _ = canonical_model_pair(inherited, provider)
                    guard = firepower_guard_error(inherited, firepower_reason)
                    if guard:
                        print(f"kanban: {guard}", file=sys.stderr)
                        return 2
                    inherited_models[task.id] = inherited
            # Build the WHOLE batch first, then commit it in one transaction.
            # Looping over individually-committing setters is what let a card
            # that changed status after selection (archived/claimed by another
            # connection) fail midway with earlier cards already pinned — a
            # split route with no receipt. Prevalidation cannot close that
            # window; a single write_txn can.
            # The selection is re-checked inside the write transaction (see
            # apply_batch_route_writes): a card that stops matching between
            # this SELECT and the writer lock — completed, archived, claimed
            # out of a status=ready selector, reassigned — is not written.
            if task_ids:
                require_statuses = frozenset(_ACTIVE_STATUSES)
                require_assignees = None
            else:
                wanted = {s.lower() for s in where.get("status", [])}
                require_statuses = frozenset(
                    s for s in _ACTIVE_STATUSES if not wanted or s in wanted
                )
                require_assignees = (
                    frozenset(where["assignee"]) if where.get("assignee") else None
                )
            writes: list[kb.BatchRouteWrite] = []
            for task in tasks:
                if touch_model and not model and not provider:
                    lane = kb.get_lane_model_override(conn, assignee=task.assignee)
                    cleared_routes[task.id] = lane.route if lane else "profile-default"
                write = kb.BatchRouteWrite(
                    task_id=task.id,
                    require_statuses=require_statuses,
                    require_assignees=require_assignees,
                    skip_if_unmatched=not task_ids,
                )
                if touch_model:
                    effective_model = model or inherited_models.get(task.id)
                    firepower = is_firepower_model(effective_model)
                    write.touch_model = True
                    write.model = effective_model
                    write.provider = provider
                    if firepower:
                        write.audit_comment_author = _profile_author()
                        # Same comment main's create/set-model writes, so
                        # the dispatcher's flagship gate authorizes it.
                        write.audit_comment_body = override_comment(
                            firepower_reason,
                        )
                if effort is not None or clear_effort:
                    write.touch_effort = True
                    write.effort = None if clear_effort else effort
                writes.append(write)

            written = set(kb.apply_batch_route_writes(conn, writes, skipped=skipped))
            # PAST THIS LINE THE ROUTES ARE DURABLE. Nothing below may let an
            # exception escape without the operator learning which cards
            # moved — that is the whole honest-partial contract, and it is
            # why the reclaim loop catches by position rather than by type.
            committed = True
            tasks = [task for task in tasks if task.id in written]

            # --reclaim runs only AFTER the route batch has committed, and
            # deliberately NOT inside it: reclaim_task SIGTERMs a live worker,
            # an irreversible side effect that a rollback could not undo. Its
            # per-card outcome is reported individually below, so a reclaim
            # that refuses one card is visible rather than silent.
            #
            # A set-model on a RUNNING card is otherwise silent: the live
            # worker keeps its old route and the change only lands if the
            # card happens to be re-dispatched. --reclaim makes that explicit
            # by releasing the claim so the next tick respawns on the new
            # route.
            #
            # No status re-check inside the loop on purpose: reclaim_task is
            # already the authority on what is reclaimable (it refuses
            # anything not running/claimed, and preserves
            # blocked/triage/scheduled rather than laundering them into
            # ready). Re-testing status at this layer would be a second,
            # drifting copy of that rule.
            if reclaim:
                applied, reclaim_errors = _run_batch_reclaims(
                    conn, tasks,
                    reason=f"set-model reclaim -> {provider or ''}/{model or ''}".strip("/"),
                )
            else:
                applied = [(task.id, False) for task in tasks]
    except BaseException as exc:  # noqa: BLE001 - re-raised unless committed
        if not committed:
            # Nothing was written, so a bare error line is the whole truth.
            # Non-(ValueError|RuntimeError) still propagates: an unexpected
            # crash before any write has no receipt to protect, and
            # swallowing it would hide a real bug.
            if not isinstance(exc, (ValueError, RuntimeError)):
                raise
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        # Committed. Something between the commit and the end of the `with`
        # block failed anyway (connection teardown, an observer, an
        # interrupt). The routes are durable, so fall through to the receipt
        # instead of surfacing a bare error that names no card.
        batch_error = (
            f"{exc.__class__.__name__}: {exc or 'no detail'}"
        )
        if not applied:
            applied = [(task.id, False) for task in tasks]

    # A single explicitly-named card keeps the original human sentences —
    # that is an established user-facing contract and the per-card flow reads
    # better as prose. The machine-readable `t_x: route=... applies=...` line
    # is what a BATCH needs, where one line per card is the whole point.
    single = len(applied) == 1 and bool(task_ids)
    for task_id, redispatched in applied:
        # `--reclaim` asked for a redispatch. If it did not happen, saying
        # `applies=next-dispatch` alone is technically true but reads as
        # normal — the refusal line below is what names it, and this keeps
        # the two consistent.
        applies = "redispatch" if redispatched else "next-dispatch"
        if single:
            if touch_model and (model or provider):
                label = f"{provider}:{model or inherited_models[task_id]}" if provider else model
                suffix = (
                    " (reclaimed; redispatches now)" if redispatched
                    else " (applies on next dispatch)"
                )
                print(f"Set model override on {task_id}: {label}{suffix}")
            elif touch_model:
                print(f"Cleared model override on {task_id} "
                      f"(next route={cleared_routes[task_id]})")
            if clear_effort:
                print(f"Cleared reasoning effort on {task_id} "
                      "(worker uses its profile's agent.reasoning_effort)")
            elif effort is not None:
                print(f"Set reasoning effort on {task_id}: {effort} "
                      "(applies on next dispatch)")
            continue
        if touch_model and (model or provider):
            route = f"{provider}/{model or inherited_models[task_id]}" if provider else model
            print(f"{task_id}: route={route} applies={applies}")
        elif touch_model:
            print(
                f"{task_id}: route={cleared_routes[task_id]} applies={applies} "
                "(model override cleared)"
            )
        if clear_effort:
            print(f"{task_id}: effort=profile-default applies={applies}")
        elif effort is not None:
            print(f"{task_id}: effort={effort} applies={applies}")
    # Selector-chosen cards that stopped matching before the writer lock were
    # NOT written; name each one so the receipt covers every selected card.
    for task_id, why in skipped.items():
        print(f"{task_id}: skipped ({why}; no longer matches the selector) route unchanged")
    # A reclaim that failed after its route committed is named explicitly —
    # the receipt above already told the operator the route moved, so staying
    # quiet here would leave them believing the worker was respawned.
    for task_id, message in reclaim_errors.items():
        print(
            f"kanban: {task_id}: route applied but reclaim failed: {message}",
            file=sys.stderr,
        )
    if batch_error:
        # Post-commit failure outside the per-card reclaim loop. The receipt
        # above is still the truth about what was written; this names why the
        # command is nonetheless not a success.
        print(
            "kanban: routes above were applied, but the batch did not finish "
            f"cleanly: {batch_error}",
            file=sys.stderr,
        )
    if skipped and not applied:
        print("kanban: every selected card stopped matching; nothing written",
              file=sys.stderr)
        return 1
    return 1 if (reclaim_errors or batch_error) else 0


def _lane_label(assignee: Optional[str]) -> str:
    return assignee or "(board-wide)"


def _cmd_lane_model(args: argparse.Namespace) -> int:
    action = getattr(args, "lane_action", None) or "show"
    if action == "set":
        return _cmd_lane_model_set(args)
    if action == "clear":
        return _cmd_lane_model_clear(args)
    return _cmd_lane_model_show(args)


def _cmd_lane_model_set(args: argparse.Namespace) -> int:
    from hermes_cli.model_policy import (
        canonical_model_pair,
        firepower_guard_error,
        format_firepower_audit,
        is_firepower_model,
    )

    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    route = getattr(args, "route", None)
    if route and (provider or model or getattr(args, "model_json", None)):
        print("kanban: route cannot be combined with --model/--provider/--model-json", file=sys.stderr)
        return 2
    if route:
        # `provider/model` is the primary spelling; the split is on the FIRST
        # slash so model ids that themselves contain slashes survive intact.
        head, sep, tail = route.partition("/")
        if not sep or not head or not tail:
            print(
                f"kanban: bad route {route!r}; expected <provider>/<model>",
                file=sys.stderr,
            )
            return 2
        provider, model = head, tail
    if route:
        args.provider, args.model = provider, model
    try:
        parsed = _cli_model_override(args, allow_ttl=True)
    except ValueError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    provider, model = parsed.provider if parsed else None, parsed.model if parsed else None
    if model:
        model, provider = canonical_model_pair(model, provider)
    reasoning_effort = parsed.reasoning_effort if parsed else None
    if not provider or not model:
        print(
            "kanban: lane-model set needs <provider>/<model> "
            "(or --provider and --model)",
            file=sys.stderr,
        )
        return 2

    ttl_raw = parsed.ttl if parsed else None
    if not ttl_raw:
        # The TTL is the whole point: it is what makes this different from
        # editing a profile's config.yaml and forgetting to revert it.
        print(
            "kanban: lane-model set requires --ttl (e.g. --ttl 2h); "
            "an override with no expiry is a config edit, not a window",
            file=sys.stderr,
        )
        return 2
    try:
        ttl_seconds = _parse_ttl(ttl_raw)
    except ValueError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2

    provider_error = _validate_provider(provider)
    if provider_error:
        print(f"kanban: {provider_error}", file=sys.stderr)
        return 2

    firepower_reason = parsed.firepower if parsed else None
    guard_error = firepower_guard_error(model, firepower_reason)
    if guard_error:
        print(f"kanban: {guard_error}", file=sys.stderr)
        return 2

    assignee = getattr(args, "assignee", None)
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print("kanban: lane-model set requires --reason", file=sys.stderr)
        return 2
    now = int(time.time())
    with kb.connect_closing() as conn:
        override = kb.set_lane_model_override(
            conn,
            provider=provider,
            model=model,
            expires_at=now + ttl_seconds,
            reasoning_effort=reasoning_effort,
            reason=reason,
            assignee=assignee,
            firepower=firepower_reason,
            created_by=_profile_author(),
            now=now,
        )
    scope = f"assignee={assignee}" if assignee else "assignee=(board-wide)"
    print(
        f"lane-model set: route={override.route} {scope} "
        f"ttl={_format_ttl(ttl_seconds)} applies=next-dispatch"
    )
    if reason:
        print(f"  reason: {reason}")
    if is_firepower_model(model) and firepower_reason:
        print(f"  {format_firepower_audit(model, provider, firepower_reason)}")
    print(
        "  cards with their own --model/--provider override are unaffected; "
        "on expiry the lane falls back to the board-wide lane if one is "
        "active, else its profile default"
    )
    return 0


def _cmd_lane_model_show(args: argparse.Namespace) -> int:
    now = int(time.time())
    with kb.connect_closing() as conn:
        overrides = kb.list_lane_model_overrides(conn, now=now)
    if getattr(args, "as_json", False):
        print(json.dumps([
            {
                "assignee": row.assignee,
                "provider": row.provider,
                "model": row.model,
                "route": row.route,
                "reason": row.reason,
                "firepower": row.firepower,
                "created_by": row.created_by,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "ttl_remaining_seconds": row.ttl_remaining(now),
            }
            for row in overrides
        ], indent=2))
        return 0
    if not overrides:
        print("(no active lane-model overrides)")
        return 0
    for row in overrides:
        print(
            f"{_lane_label(row.assignee)}: route={row.route} "
            f"ttl={_format_ttl(row.ttl_remaining(now))} remaining"
        )
        if row.reason:
            print(f"  reason: {row.reason}")
        if row.firepower:
            print(f"  firepower: {row.firepower}")
        if row.created_by:
            print(f"  set by: {row.created_by}")
    return 0


def _cmd_lane_model_clear(args: argparse.Namespace) -> int:
    assignee = getattr(args, "assignee", None)
    clear_all = bool(getattr(args, "clear_all", False))
    with kb.connect_closing() as conn:
        if clear_all:
            # One transaction for the whole sweep — see
            # clear_all_lane_model_overrides. Listing and then deleting row by
            # row is the same partial-commit class as the set-model batch.
            removed = kb.clear_all_lane_model_overrides(conn)
        else:
            one = kb.clear_lane_model_override(conn, assignee=assignee)
            removed = [one] if one else []
        # Name what routes each cleared lane NOW: clearing an assignee lane
        # under a live board-wide lane does not return it to the profile
        # default, and saying so would be the same false receipt class.
        now = int(time.time())
        successors = {
            row.assignee: kb.lane_successor_label(
                kb.get_lane_model_override(conn, assignee=row.assignee, now=now)
            )
            for row in removed
        }
    if not removed:
        target = "any lane" if clear_all else _lane_label(assignee)
        print(f"no lane-model override set for {target}", file=sys.stderr)
        return 1
    for row in removed:
        print(
            f"Cleared lane-model override for {_lane_label(row.assignee)} "
            f"(was {row.route}); lane now routes via {successors[row.assignee]}"
        )
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.reclaim_task(
            conn, args.task_id,
            reason=getattr(args, "reason", None),
        )
        task = kb.get_task(conn, args.task_id) if ok else None
    if not ok:
        print(
            f"cannot reclaim {args.task_id} (not running, unknown id, or "
            "worker not proven dead — inspect 'hermes kanban tail' for "
            "reclaim_refused)",
            file=sys.stderr,
        )
        return 1
    status = task.status if task else None
    if status in ("blocked", "triage", "scheduled"):
        # A reclaim releases the claim; it does not promote. Say so, or the
        # operator assumes the card is queued and waits for a worker forever.
        print(
            f"Reclaimed {args.task_id} (status held at {status!r} — "
            f"run 'hermes kanban unblock {args.task_id}' to re-queue it)"
        )
    else:
        print(f"Reclaimed {args.task_id}" + (f" ({status})" if status else ""))
    return 0


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    reclaim_first = bool(getattr(args, "reclaim", False))
    # `--reclaim` SIGTERMs the live worker and releases the claim BEFORE the
    # assign is attempted. If the assign then refuses, the old "cannot
    # reassign (still running — pass --reclaim)" line was actively false: the
    # reclaim had already happened and the worker was already dead. Carry the
    # receipt so the failure path can say what really landed.
    receipt: dict = {}
    with kb.connect_closing() as conn:
        ok = kb.reassign_task(
            conn, args.task_id, profile,
            reclaim_first=reclaim_first,
            reason=getattr(args, "reason", None),
            receipt=receipt,
        )
    reclaimed = bool(receipt.get("reclaimed"))
    if not ok:
        if receipt.get("reclaim_error"):
            print(
                f"kanban: {args.task_id}: reclaim failed, task NOT reassigned: "
                f"{receipt['reclaim_error']}",
                file=sys.stderr,
            )
            return 1
        if reclaimed:
            detail = (
                f"; assign failed ({receipt['assign_error']}); assignment outcome "
                "may have changed — inspect the card"
                if receipt.get("assign_error") else
                "; assign refused (the card may have been claimed again) — "
                "inspect the current status before retrying"
            )
            print(
                f"kanban: {args.task_id}: claim WAS reclaimed (the prior "
                f"worker was signalled){detail}",
                file=sys.stderr,
            )
            return 1
        print(
            f"cannot reassign {args.task_id} "
            f"(unknown id, or still running — pass --reclaim to release first)",
            file=sys.stderr,
        )
        return 1
    print(
        f"Reassigned {args.task_id} to "
        f"{profile or '(unassigned)'}"
        + (" (claim reclaimed)" if reclaimed else "")
    )
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board. Wraps the same rule engine
    the dashboard uses, so CLI output matches what the UI shows.
    """
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kb.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                print(f"no such task: {args.task}", file=sys.stderr)
                return 1
            diags_by_task = {
                args.task: kd.compute_task_diagnostics(
                    task,
                    kb.list_events(conn, args.task),
                    kb.list_runs(conn, args.task),
                    graph=kb.task_graph_context(conn, args.task),
                    config=diag_config,
                )
            }
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute(
                "SELECT * FROM tasks WHERE status != 'archived'"
            ).fetchall())
            ids = [r["id"] for r in rows]
            if not ids:
                diags_by_task = {}
            else:
                placeholders = ",".join(["?"] * len(ids))
                ev_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    ev_by.setdefault(row["task_id"], []).append(row)
                run_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    run_by.setdefault(row["task_id"], []).append(row)
                graph_by = kb.task_graph_contexts(conn, ids)
                diags_by_task = {}
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(
                        r,
                        ev_by.get(tid, []),
                        run_by.get(tid, []),
                        graph=graph_by.get(tid),
                        config=diag_config,
                    )
                    if dl:
                        diags_by_task[tid] = dl

        # Severity filter.
        sev = getattr(args, "severity", None)
        if sev:
            for tid in list(diags_by_task.keys()):
                kept = [d for d in diags_by_task[tid] if kd.SEVERITY_ORDER.index(d.severity) >= kd.SEVERITY_ORDER.index(sev)]
                if kept:
                    diags_by_task[tid] = kept
                else:
                    del diags_by_task[tid]

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                tuple(diags_by_task.keys()),
            ):
                meta[r["id"]] = {
                    "title": r["title"], "status": r["status"],
                    "assignee": r["assignee"],
                }

    if getattr(args, "json", False):
        out_json = [
            {
                "task_id": tid,
                **meta.get(tid, {}),
                "diagnostics": [d.to_dict() for d in dl],
            }
            for tid, dl in diags_by_task.items()
        ]
        print(json.dumps(out_json, indent=2, ensure_ascii=False))
        return 0

    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    # Human-readable summary: grouped by task, severity-marked, with
    # suggested actions inline.
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    total = sum(len(dl) for dl in diags_by_task.values())
    print(
        f"{total} active diagnostic(s) across "
        f"{len(diags_by_task)} task(s):\n"
    )
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        title = m.get("title") or "(untitled)"
        status = m.get("status") or "?"
        assignee = m.get("assignee") or "(unassigned)"
        print(f"  {tid}  {status:8s}  @{assignee:18s}  {title}")
        for d in dl:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.kind}: {d.title}")
            if d.data:
                # Compact key:value pairs on one line.
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Suggested actions first.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    kind = getattr(args, "kind", None) or kb.DEFAULT_LINK_KIND
    with kb.connect_closing() as conn:
        kb.link_tasks(conn, args.parent_id, args.child_id, kind=kind)
    print(f"Linked {args.parent_id} -> {args.child_id} ({kind})")
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    if not ok:
        print(f"No such link: {args.parent_id} -> {args.child_id}", file=sys.stderr)
        return 1
    print(f"Unlinked {args.parent_id} -> {args.child_id}")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            # Report why
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"cannot claim {args.task_id}: status={existing.status} "
                f"lock={existing.claim_lock or '(none)'}",
                file=sys.stderr,
            )
            return 1
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}")
    print(f"Workspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    """Append a comment, attributed to the session that invoked this CLI.

    Provenance policy for this entry point (decided deliberately, not by
    omission):

    * Invoked **from inside a Hermes session** — an agent shelling out from
      ``terminal`` or ``execute_code``, which is how most CLI comments are
      actually written — the spawn path bridges ``HERMES_SESSION_ID`` into this
      process's env, so ``safe_comment_provenance`` resolves it and the row is
      attributed exactly like an in-process ``kanban_comment`` tool call.
    * Invoked **by a human in a bare shell**, with no Hermes session anywhere in
      the ancestry, there is genuinely no session to name. The comment is still
      written, with NULL provenance, and every read surface renders it
      ``<author> (provenance unknown)``. We deliberately do NOT synthesize a
      per-invocation id: a fresh uuid per ``hermes kanban comment`` call would
      make two comments from one operator look like two distinct sessions,
      which is the *same* ambiguity this field exists to remove, inverted.
      Saying "unknown" is the honest answer, and it is what the tri-state
      contract elsewhere in this codebase already does.
    """
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            print("kanban: --max-len must be positive", file=sys.stderr)
            return 2
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    run_id, session_ref = safe_comment_provenance(args.task_id)
    with kb.connect_closing() as conn:
        kb.add_comment(
            conn, args.task_id, author, body,
            run_id=run_id, session_ref=session_ref,
        )
    print(f"Comment added to {args.task_id}")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file to a task.

    Reads the file off disk, writes it under the task's attachments dir,
    and records the metadata row via the shared ``store_attachment_bytes``
    path (same code the dashboard upload and the agent tool use), so the
    25 MB cap and name-sanitisation behave identically everywhere.
    """
    import mimetypes

    src = Path(args.path).expanduser()
    if not src.is_file():
        print(f"kanban: no such file: {src}", file=sys.stderr)
        return 1
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kb.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(
                conn,
                args.task_id,
                name,
                data,
                content_type=content_type,
                uploaded_by=uploaded_by,
            )
    except kb.AttachmentTooLarge as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 1
    print(f"Attached {name} to {args.task_id} (attachment {att_id}, {len(data)} bytes)")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    """List a task's attachments."""
    with kb.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        atts = kb.list_attachments(conn, args.task_id)
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "id": a.id,
                "filename": a.filename,
                "content_type": a.content_type,
                "size": a.size,
                "uploaded_by": a.uploaded_by,
                "stored_path": a.stored_path,
                "created_at": a.created_at,
            }
            for a in atts
        ], indent=2))
        return 0
    if not atts:
        print(f"No attachments on {args.task_id}")
        return 0
    print(f"Attachments on {args.task_id}:")
    for a in atts:
        ct = a.content_type or "-"
        print(f"  [{a.id}] {a.filename}  ({a.size} bytes, {ct}, by {a.uploaded_by or '-'})")
        print(f"        {a.stored_path}")
    return 0


def _cmd_attach_rm(args: argparse.Namespace) -> int:
    """Delete an attachment by id (removes the row and the on-disk blob)."""
    with kb.connect_closing() as conn:
        removed = kb.delete_attachment(conn, args.attachment_id)
    if removed is None:
        print(f"no such attachment: {args.attachment_id}", file=sys.stderr)
        return 1
    print(f"Deleted attachment {args.attachment_id} ({removed.filename}) from {removed.task_id}")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    """Return this process's dispatcher run id, but only if it OWNS the run.

    ``HERMES_KANBAN_*`` is ordinary process environment, inherited verbatim by
    every descendant. Stamping the inherited ``HERMES_KANBAN_RUN_ID`` onto a
    write from a process that is not the dispatcher's worker attributes that
    write to a run it never made — and, worse, satisfies the ``expected_run_id``
    optimistic-concurrency guard that ``complete``/``heartbeat``/``request-review``
    rely on to keep a stale writer from closing a live card. The authority
    anchor is ``agent.delegation_context.owns_kanban_worker_authority``
    (``HERMES_KANBAN_OWNER_PID``); read it here so the CLI path is gated on the
    same predicate as the tool path (``tools.kanban_tools._owned_worker_task_id``).
    """
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if not is_dispatcher_owned_worker_context():
            return None
    except Exception:
        pass
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str, *, conn=None, task_id=None) -> Optional[str]:
    """CLI-surface wiring of the shared goal-mode handoff gate (complete + review)."""
    from hermes_cli import goals

    return goals.kanban_handoff_rejection(
        task, evidence, conn=conn, task_id=task_id,
        worker_run_id_for=_worker_run_id_for,
        judge_available=goals.goal_judge_available,
        judge=goals.judge_goal,
    )


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    summary = getattr(args, "summary", None)
    superseded_by = getattr(args, "superseded_by", None)
    raw_meta = getattr(args, "metadata", None)
    # Guard: structured handoff fields are per-run, so they'd be
    # copy-pasted identically across N runs — almost always a footgun.
    # Refuse instead of silently doing the wrong thing.
    survivor_ref = getattr(args, "survivor_ref", None)
    survivor_pr = getattr(args, "survivor_pr", None)
    survivor_unbound = getattr(args, "survivor_unbound", None) or None
    if len(ids) > 1 and (summary or raw_meta or survivor_ref or survivor_pr
                         or survivor_unbound or superseded_by):
        print(
            "kanban: --summary / --metadata / --superseded-by / --survivor-ref / "
            "--survivor-pr / --survivor-unbound are per-task "
            "and can't be used with multiple ids (would apply the same handoff, and record "
            "the same survivor, for every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.",
            file=sys.stderr,
        )
        return 2
    if survivor_unbound and not (survivor_ref or survivor_pr):
        print(
            "kanban: --survivor-unbound only relaxes the task-id binding on an explicit "
            "--survivor-ref/--survivor-pr claim; pass one, or drop the flag.",
            file=sys.stderr,
        )
        return 2
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            # Goal-mode judge gate (mirrors tools/kanban_tools.py). Apply it
            # to every terminal handoff so request-review cannot bypass the
            # acceptance contract that protects complete.
            task = kb.get_task(conn, tid)
            # A superseded close has no work for the judge to grade; gating it
            # would push the worker back into exiting silently.
            rejection = None if superseded_by is not None else _goal_mode_handoff_rejection(
                task,
                (summary or args.result or "").strip(),
                conn=conn, task_id=tid,
            )
            if rejection is not None:
                print(
                    f"kanban: goal completion of {tid} rejected by judge: {rejection}. "
                    f"Provide evidence matching the task's acceptance criteria.",
                    file=sys.stderr,
                )
                failed.append(tid)
                continue

            try:
                done = kb.complete_task(
                    conn, tid,
                    result=args.result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=_worker_run_id_for(tid),
                    survivor_ref=survivor_ref,
                    survivor_pr=survivor_pr,
                    survivor_unbound=survivor_unbound,
                    superseded_by=superseded_by,
                )
            except kb.EmptySupersedeError as supersede_err:
                failed.append(tid)
                print(f"cannot complete {tid}: {supersede_err}.", file=sys.stderr)
                continue
            if not done:
                failed.append(tid)
                print(f"cannot complete {tid} (unknown id or terminal state)", file=sys.stderr)
            else:
                print(f"Completed {tid}")
    return 0 if not failed else 1


def _cmd_edit(args: argparse.Namespace) -> int:
    raw_meta = getattr(args, "metadata", None)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2

    model_override = getattr(args, "model_override", None)
    clear_model = bool(getattr(args, "clear_model", False))
    if model_override is not None and clear_model:
        print(
            "kanban: --model and --clear-model are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    # The result-backfill edit is only attempted when --result is given;
    # --model / --clear-model are independent edits that apply to any task.
    do_result = getattr(args, "result", None) is not None
    do_model = model_override is not None or clear_model
    new_session = getattr(args, "session", None)
    do_session = new_session is not None

    if not do_result and not do_model and not do_session:
        print(
            "kanban: nothing to edit (pass --result, --model, --clear-model, "
            "or --session)",
            file=sys.stderr,
        )
        return 2

    rc = 0
    with kb.connect_closing() as conn:
        if do_session:
            sid = None if new_session.strip().lower() in ("", "none") else new_session.strip()
            if not kb.set_task_session(conn, args.task_id, sid):
                print(f"cannot restamp {args.task_id} (unknown id)", file=sys.stderr)
                return 1
            print(f"Home session of {args.task_id}: {sid or 'unstamped'}")
        if do_model:
            # --clear-model writes NULL; --model X writes X literally. The
            # None sentinel ("--model omitted") never reaches here.
            new_model = None if clear_model else model_override
            affected = kb.set_task_model(conn, args.task_id, new_model)
            if affected == 0:
                print(
                    f"cannot set model on {args.task_id} (unknown id)",
                    file=sys.stderr,
                )
                return 1
            if clear_model:
                print(f"Cleared model override on {args.task_id}")
            else:
                print(f"Set model override on {args.task_id}: {new_model}")
        if do_result:
            if not kb.edit_completed_task_result(
                conn,
                args.task_id,
                result=args.result,
                summary=getattr(args, "summary", None),
                metadata=metadata,
            ):
                print(
                    f"cannot edit {args.task_id} (unknown id or task is not done)",
                    file=sys.stderr,
                )
                return 1
            print(f"Edited {args.task_id}")
    return rc


def _cmd_block(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    kind = getattr(args, "kind", None)
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                _run_id, _sess_ref = safe_comment_provenance(tid)
                kb.add_comment(
                    conn, tid, author, f"BLOCKED: {reason}",
                    run_id=_run_id, session_ref=_sess_ref,
                )
            if not kb.block_task(
                conn,
                tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot block {tid}", file=sys.stderr)
            else:
                # Report where the task actually landed — dependency blocks go
                # to todo, and a tripped unblock-loop breaker routes to triage.
                landed = kb.get_task(conn, tid)
                where = landed.status if landed else "blocked"
                suffix = f": {reason}" if reason else ""
                if where == "todo":
                    print(f"{tid} → todo (dependency wait){suffix}")
                elif where == "triage":
                    print(
                        f"{tid} → triage (unblock loop detected — needs a "
                        f"human decision){suffix}"
                    )
                else:
                    print(f"Blocked {tid}{suffix}")
    return 0 if not failed else 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                _run_id, _sess_ref = safe_comment_provenance(tid)
                kb.add_comment(
                    conn, tid, author, f"SCHEDULED: {reason}",
                    run_id=_run_id, session_ref=_sess_ref,
                )
            if not kb.schedule_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot schedule {tid}", file=sys.stderr)
            else:
                print(f"Scheduled {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_unblock(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = reason.strip() or None
    author = _profile_author() if reason else None
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                _run_id, _sess_ref = safe_comment_provenance(tid)
                kb.add_comment(
                    conn, tid, author, f"UNBLOCK: {reason}",
                    run_id=_run_id, session_ref=_sess_ref,
                )
            if not kb.unblock_task(conn, tid):
                failed.append(tid)
                print(f"cannot unblock {tid} (not blocked/scheduled?)", file=sys.stderr)
            else:
                print(f"Unblocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_requeue(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip()
    with kb.connect_closing() as conn:
        ok, err = kb.requeue_task(conn, args.task_id, actor=_profile_author(), reason=reason)
    if not ok:
        print(f"cannot requeue {args.task_id}: {err}", file=sys.stderr)
        return 1
    print(f"Requeued {args.task_id}: {reason}")
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    """Reverse a false completion so the real work can be re-dispatched."""
    reason = " ".join(args.reason).strip() if args.reason else ""
    author = _profile_author()
    as_json = getattr(args, "json", False)
    to_status = getattr(args, "to_status", "ready")

    with kb.connect_closing() as conn:
        ok, err = kb.reopen_task(
            conn,
            args.task_id,
            actor=author,
            reason=reason,
            to_status=to_status,
        )

    if as_json:
        print(json.dumps({
            "task_id": args.task_id,
            "reopened": ok,
            "to_status": to_status,
            "reason": reason,
            "error": err,
        }, indent=2, ensure_ascii=False))
        return 0 if ok else 1

    if not ok:
        print(f"cannot reopen {args.task_id}: {err}", file=sys.stderr)
        return 1
    print(
        f"Reopened {args.task_id} -> {to_status} "
        f"(previous completion voided): {reason}"
    )
    return 0


def _cmd_request_review(args: argparse.Namespace) -> int:
    tid = args.task_id
    summary = getattr(args, "summary", None)
    if summary is not None:
        summary = summary.strip() or None
    raw_metadata = getattr(args, "metadata", None)
    metadata = None
    if raw_metadata:
        try:
            metadata = json.loads(raw_metadata)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    reviewer = getattr(args, "reviewer", None)
    with kb.connect_closing() as conn:
        rejection = _goal_mode_handoff_rejection(
            kb.get_task(conn, tid),
            summary or "",
            conn=conn, task_id=tid,
        )
        if rejection is not None:
            print(
                f"kanban: goal review handoff of {tid} rejected by judge: "
                f"{rejection}. Provide acceptance evidence matching the task.",
                file=sys.stderr,
            )
            return 1
        ok, reason = kb.request_review(
            conn,
            tid,
            summary=summary,
            metadata=metadata,
            reviewer=reviewer,
            expected_run_id=_worker_run_id_for(tid),
            force=bool(getattr(args, "force", False)),
            allow_same_actor=bool(getattr(args, "allow_same_actor", False)),
            with_reason=True,
        )
        if not ok:
            detail = reason or "not running/ready?"
            print(
                f"cannot request review for {tid}: {detail}",
                file=sys.stderr,
            )
            return 1
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(
            f"Requested review for {tid}"
            + (f": {display_summary}" if display_summary else "")
        )
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    with kb.connect_closing() as conn:
        ok, detail = kb.request_changes(
            conn,
            tid,
            reason=reason,
            expected_run_id=_worker_run_id_for(tid),
        )
        if not ok:
            print(
                f"cannot request changes for {tid}: {detail or 'invalid review state'}",
                file=sys.stderr,
            )
            return 1
        print(
            f"Requested changes for {tid}"
            + (f"; routed to {detail}" if detail else "")
        )
    return 0


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = str(kb.redact_review_value(reason.strip())).strip() or None
    author = _profile_author() if reason else None
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if not kb.reopen_review_task(conn, tid):
                failed.append(tid)
                print(f"cannot reopen {tid} (not in review?)", file=sys.stderr)
            else:
                if reason:
                    kb.add_comment(
                        conn,
                        tid,
                        author or "operator",
                        f"CHANGES REQUESTED: {reason}",
                    )
                print(f"Reopened {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    as_json = getattr(args, "json", False)
    extra_ids = list(getattr(args, "ids", None) or [])
    # Dedupe while preserving order; positional task_id always first.
    ids: list[str] = []
    seen: set[str] = set()
    for tid in [args.task_id, *extra_ids]:
        if tid not in seen:
            ids.append(tid)
            seen.add(tid)

    results: list[dict[str, object]] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(
                conn,
                tid,
                actor=author,
                reason=reason,
                force=bool(args.force),
                dry_run=bool(args.dry_run),
            )
            results.append({
                "task_id": tid,
                "promoted": ok,
                "dry_run": bool(args.dry_run),
                "forced": bool(args.force),
                "reason": reason,
                "error": err,
            })

    failed = [r for r in results if not r["promoted"]]
    if as_json:
        # Single-id stays a flat object for back-compat; bulk emits a list.
        payload: object = results[0] if len(results) == 1 else results
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not failed else 1

    tag = " (dry)" if args.dry_run else ""
    label = "Would promote" if args.dry_run else "Promoted"
    for r in results:
        if r["promoted"]:
            suffix = f": {reason}" if reason else ""
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return 0 if not failed else 1


def _cmd_triage_resolve(args: argparse.Namespace) -> int:
    """The supported exit from the ``triage`` column.

    ``triage`` means an automated unblocker already tried and failed
    repeatedly, so a human must decide. That escalation is correct and stays
    exactly as-is — this verb only supplies the exit it was missing.
    """
    with kb.connect_closing() as conn:
        ok, err = kb.triage_resolve_task(
            conn,
            args.task_id,
            to=args.to,
            reason=args.reason,
            actor=_profile_author(),
        )
        status = kb.get_task(conn, args.task_id).status if ok else None
    if getattr(args, "json", False):
        print(json.dumps({
            "task_id": args.task_id,
            "resolved": ok,
            "to": args.to,
            "status": status,
            "reason": args.reason,
            "error": err,
        }, indent=2, ensure_ascii=False))
        return 0 if ok else 1
    if not ok:
        print(f"cannot triage-resolve {args.task_id}: {err}", file=sys.stderr)
        return 1
    # The landing column may differ from --to: resolving to 'todo' runs
    # recompute_ready, which promotes to 'ready' when parents allow. Report
    # where the card ACTUALLY is so nobody waits on a card that already moved.
    landed = f" (now {status!r})" if status and status != args.to else ""
    print(f"Resolved {args.task_id} -> {args.to}{landed}: {args.reason}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        print("choose either task_ids to archive or --rm archived task_ids", file=sys.stderr)
        return 1
    if not ids and not purge_ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    failed: list[str] = []
    with kb.connect_closing() as conn:
        if purge_ids:
            for tid in purge_ids:
                if not kb.delete_archived_task(conn, tid):
                    failed.append(tid)
                    print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                else:
                    print(f"Deleted {tid}")
            return 0 if not failed else 1
        for tid in ids:
            if not kb.archive_task(conn, tid):
                failed.append(tid)
                print(f"cannot archive {tid}", file=sys.stderr)
            else:
                print(f"Archived {tid}")
    return 0 if not failed else 1


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")
    try:
        while True:
            with kb.connect_closing() as conn:
                events = kb.list_events(conn, args.task_id)
            for e in events:
                if e.id > last_id:
                    pl = f" {e.payload}" if e.payload else ""
                    print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                    last_id = e.id
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee as the fallback for unassigned ready
    # tasks (#27145), kanban.max_in_progress as the global concurrency cap
    # (#33488), kanban.max_in_progress_per_profile as the per-profile
    # cap (#21582), and kanban.max_spawn as the per-tick spawn limit
    # (#28805). Same semantics as the gateway dispatch path so behavior
    # matches whether the user runs the CLI directly or relies on the
    # gateway-embedded dispatcher.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None

        def _coerce_positive_int(value):
            if value is None:
                return None
            try:
                ival = int(value)
            except (TypeError, ValueError):
                return None
            return ival if ival >= 1 else None

        max_in_progress_per_profile = _coerce_positive_int(
            _kanban_cfg.get("max_in_progress_per_profile")
        )
        max_in_progress = _coerce_positive_int(_kanban_cfg.get("max_in_progress"))
        # Memory-derived default when unset (OOF-30/OOF-77) — same
        # fallback the gateway-embedded dispatcher applies, so behaviour
        # matches regardless of which path runs the tick.
        max_in_progress = kb.resolve_max_in_progress(max_in_progress)
        # CLI --max overrides config kanban.max_spawn when both are present;
        # CLI is the more explicit signal so it wins.
        cli_max = getattr(args, "max", None)
        max_spawn = cli_max if cli_max is not None else _coerce_positive_int(
            _kanban_cfg.get("max_spawn")
        )
    except Exception:
        default_assignee = None
        max_in_progress_per_profile = None
        max_in_progress = None
        max_spawn = getattr(args, "max", None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
        # Spawned cards nobody is watching will finish silently — surface
        # that at the point of confusion (every dispatch run) instead of
        # relying on the operator remembering to notify-subscribe. Computed
        # regardless of kanban.cli_auto_subscribe; best-effort so a
        # notification bookkeeping failure never fails the dispatch.
        spawned_unwatched: list[str] = []
        for _tid, _who, _ws in res.spawned:
            try:
                if not kb.list_notify_subs(conn, _tid):
                    spawned_unwatched.append(_tid)
            except Exception:
                pass
    from hermes_cli.model_policy import route_kind

    if getattr(args, "json", False):
        # The human-readable tick prints the awaiting-HUMAN detector below;
        # without this key a JSON consumer is exactly as blind as it was
        # before the detector existed. Same dry-run rule as the text path:
        # report, but only arm/send on a real tick.
        review_awaiting = _collect_review_awaiting_human(
            alert=not args.dry_run
        )
        print(json.dumps({
            "review_awaiting_human": review_awaiting,
            "reclaimed": res.reclaimed,
            "skipped_locked": res.skipped_locked,
            "budget_paused": getattr(res, "budget_paused", False),
            "lock_holder": res.lock_holder,
            "crashed": res.crashed,
            "timed_out": res.timed_out,
            "stale": res.stale,
            "auto_blocked": res.auto_blocked,
            "promoted": res.promoted,
            "spawned": [
                {
                    "task_id": tid,
                    "assignee": who,
                    "workspace": ws,
                    "route": res.spawn_routes.get(tid),
                    "route_kind": route_kind(res.spawn_routes.get(tid)),
                    "route_source": res.spawn_route_sources.get(tid),
                }
                for (tid, who, ws) in res.spawned
            ],
            "expired_lane_models": [
                {
                    "lane": lane,
                    "route": route,
                    "successor": (getattr(res, "expired_lane_successors", None) or {})
                    .get(lane, "profile default"),
                }
                for (lane, route) in getattr(res, "expired_lane_models", []) or []
            ],
            "spawned_unwatched": spawned_unwatched,
            "skipped_unassigned": res.skipped_unassigned,
            "flagship_refused": res.flagship_refused,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "stranded_by_triage": [
                {"task_id": child, "parent_id": parent}
                for (child, parent) in res.stranded_by_triage
            ],
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "respawn_guarded": [
                {"task_id": tid, "reason": reason,
                 **res.respawn_guard_details.get(tid, {})}
                for (tid, reason) in res.respawn_guarded
            ],
            "auto_assigned_default": res.auto_assigned_default,
            "collision_warnings": [
                {
                    "task_id": task_id,
                    "existing_task_id": existing_task_id,
                    "paths": paths,
                }
                for task_id, existing_task_id, paths in getattr(
                    res, "collision_warnings", []
                )
            ],
            "collision_scope_unknown": getattr(
                res, "collision_scope_unknown", []
            ),
            "collision_scope_unreported": [
                {"task_id": task_id, "unchecked_task_ids": unchecked_ids}
                for task_id, unchecked_ids in getattr(
                    res, "collision_scope_unreported", []
                )
            ],
            "collision_check_failed": getattr(
                res, "collision_check_failed", []
            ),
            "gate_auto_resolved": getattr(res, "gate_auto_resolved", []),
            "gate_closed_unmerged": getattr(res, "gate_closed_unmerged", []),
        }, indent=2))
        return 0
    if res.skipped_locked:
        print(kb.format_dispatch_lock_skip(res.lock_holder))
    if getattr(res, "budget_paused", False):
        # Loud: otherwise a budget-paused board prints "Spawned: 0" and is
        # byte-identical to an idle board — the exact false negative the
        # stranded-by-triage banner exists to prevent.
        print(
            "BUDGET PAUSED — this board's rolling-window worker spend has "
            "reached kanban.budget.usd_per_24h; no new workers spawned. "
            "Details: hermes kanban budget"
        )
    print(f"Reclaimed:    {res.reclaimed}")
    print(f"Crashed:      {len(res.crashed)}")
    if res.crashed:
        print(f"  {', '.join(res.crashed)}")
    print(f"Flagship refused: {len(res.flagship_refused)}")
    if res.flagship_refused:
        print(f"  {', '.join(res.flagship_refused)}")
    print(f"Timed out:    {len(res.timed_out)}")
    if res.timed_out:
        print(f"  {', '.join(res.timed_out)}")
    print(f"Stale:        {len(res.stale)}")
    if res.stale:
        print(f"  {', '.join(res.stale)}")
    print(f"Auto-blocked: {len(res.auto_blocked)}")
    if res.auto_blocked:
        print(f"  {', '.join(res.auto_blocked)}")
    print(f"Promoted:     {res.promoted}")
    gate_resolved = getattr(res, "gate_auto_resolved", [])
    if gate_resolved:
        print(
            f"Gate auto-resolved (referenced PR(s) merged): "
            f"{', '.join(gate_resolved)}"
        )
    gate_closed = getattr(res, "gate_closed_unmerged", [])
    if gate_closed:
        print(
            "WARNING — gate PR closed WITHOUT merging; card left blocked for "
            f"a human: {', '.join(gate_closed)}"
        )
    print(f"Spawned:      {len(res.spawned)}")
    for tid, who, ws in res.spawned:
        tag = " (dry)" if args.dry_run else ""
        route = res.spawn_routes.get(tid, "unknown/unknown")
        source = res.spawn_route_sources.get(tid)
        # source= names WHICH layer chose the route (card / lane window /
        # profile). Without it a lane override is invisible in the tick log
        # and looks like the profile default silently changed.
        source_part = f" source={source}" if source else ""
        print(
            f"  - {tid}  ->  {who}  @ {ws or '-'}  route={route}{source_part} "
            f"kind={route_kind(route)}{tag}"
        )
    successors = getattr(res, "expired_lane_successors", None) or {}
    for lane, route in getattr(res, "expired_lane_models", []) or []:
        print(
            f"lane-model expired ({lane}: {route}) -> "
            f"{successors.get(lane, 'profile default')}"
        )
    collision_warnings = getattr(res, "collision_warnings", [])
    if collision_warnings:
        print("WARNING — pre-dispatch file collision(s); dispatch continued:")
        for task_id, existing_task_id, paths in collision_warnings:
            print(
                f"  - {task_id} overlaps {existing_task_id}: "
                f"{', '.join(paths)}"
            )
    collision_scope_unknown = getattr(res, "collision_scope_unknown", [])
    if collision_scope_unknown:
        print(
            "WARNING — collision scope unknown (no explicit file paths): "
            f"{', '.join(collision_scope_unknown)}"
        )
    for task_id, unchecked_ids in getattr(
        res, "collision_scope_unreported", []
    ):
        print(
            f"WARNING — collision check partial for {task_id}; no reported "
            f"changed_files from: {', '.join(unchecked_ids)}"
        )
    collision_check_failed = getattr(res, "collision_check_failed", [])
    if collision_check_failed:
        print(
            "WARNING — collision check failed open; dispatch continued for: "
            f"{', '.join(collision_check_failed)}"
        )
    if spawned_unwatched:
        print(
            f"{len(spawned_unwatched)} spawned card(s) have no notify "
            "subscription — finishes will be silent "
            "(kanban.cli_auto_subscribe or notify-subscribe)"
        )
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    if res.skipped_per_profile_capped:
        for tid, who, current in res.skipped_per_profile_capped:
            print(
                f"Deferred ({who} at per-profile cap, {current} running): {tid}"
            )
    if res.respawn_guarded:
        for tid, reason in res.respawn_guarded:
            print(
                f"Deferred (respawn guard {reason}): {tid}"
                + _fmt_respawn_guard_detail(res.respawn_guard_details.get(tid))
            )
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — HUMAN review required): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    # --dry-run is documented (and mandated by the incident runbook) as the
    # SAFE probe, so it must not arm alerts or send: arming writes a durable
    # review_stale_alerted event, and the send fires a real Discord message.
    # The detector still PRINTS — that is the whole value of the line — it
    # just stops mutating the board it is only supposed to observe.
    _print_review_awaiting_human(alert=not args.dry_run)
    _print_stranded_by_triage(res.stranded_by_triage)
    return 0


def _notify_script_path():
    """Locate ``notify.py`` (the out-of-agent Discord/Telegram alert helper).

    Resolved under the *running* Hermes root, never a hardcoded ``~/.hermes``.
    These assets are root-level (shared across profiles), so
    ``get_default_hermes_root()`` is the right resolver: it maps a profile home
    ``<root>/profiles/<name>`` back to ``<root>`` while leaving a redirected
    home (a sandbox, a hermetic test home, CI) pointing at itself. Hardcoding
    the path made a sandboxed board fire a REAL alert to the live channel about
    cards that do not exist on the real board.
    """
    root = get_default_hermes_root()
    candidates = [
        str(root / "scripts" / "notify.py"),
        str(root / "skills-shared/general/scheduler/scripts/notify.py"),
        str(root / "skills/devops/scheduler/scripts/notify.py"),
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                return path
        except Exception:
            continue
    return None


def _send_review_stale_alert(entries) -> None:
    """Fire one #alerts message for cards that just crossed the threshold."""
    script = _notify_script_path()
    if script is None:
        return
    lines = [
        f"  • {e['task_id']} → {e['assignee'] or '(unassigned)'} "
        f"({e['age_minutes']}m, {e['reason']})"
        for e in entries[:10]
    ]
    body = (
        "🕰️ Kanban review lane awaiting a HUMAN\n"
        + "\n".join(lines)
        + "\nNo autonomous reviewer will pick these up. Reassign with: "
        "hermes kanban assign <id> argus"
    )
    try:
        subprocess.run(
            [sys.executable, str(script), "--send", body, "--channel", "discord"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass


def _collect_review_awaiting_human(*, alert: bool = True) -> list:
    """Return review cards nothing will ever spawn; arm+send only if ``alert``.

    Single owner of this detector's side-effect policy. Both the text tick and
    the ``--json`` tick go through here, so the two surfaces cannot drift into
    reporting different things — or into one of them writing when the other
    does not. ``alert=False`` makes the call strictly read-only: no
    ``review_stale_alerted`` event, no Discord send.
    """
    try:
        with kb.connect_closing() as conn:
            entries = kb.review_awaiting_human(conn)
            if entries and alert:
                fresh = kb.arm_review_stale_alerts(conn, entries)
                if fresh:
                    _send_review_stale_alert(fresh)
            return entries
    except Exception:
        return []


def _print_review_awaiting_human(*, alert: bool = True) -> None:
    """Report review cards nothing will ever spawn, and alert once each.

    Before this, the dispatcher printed those cards as "terminal lane, OK"
    and there was no other signal — 10 cards sat in review (one 2h+) with
    nothing raised (incident 2026-09-21).
    """
    entries = _collect_review_awaiting_human(alert=alert)
    try:
        line = kb.format_review_awaiting_human(entries)
    except Exception:
        return
    if line:
        print(line)


def _print_stranded_by_triage(stranded) -> None:
    """Name the subtrees frozen behind a human-gated parent.

    Without this, a board where one triaged parent holds a whole subtree
    prints ``Spawned: 0`` and nothing else — identical to an idle board. That
    false negative let a deploy card sit idle for hours. Group by parent so
    the operator sees how many cards each stuck decision is costing, and name
    the exact verb that clears it.
    """
    entries = list(stranded or [])
    if not entries:
        return
    by_parent: dict[str, list[str]] = {}
    for child, parent in entries:
        by_parent.setdefault(parent, []).append(child)
    n_children = len({child for child, _ in entries})
    print(
        f"STRANDED: {n_children} task(s) held in todo behind "
        f"{len(by_parent)} triaged/blocked parent(s) — nothing will spawn "
        f"until a human resolves them:"
    )
    for parent in sorted(by_parent):
        kids = ", ".join(sorted(by_parent[parent]))
        print(f"  - {parent} blocks: {kids}")
    print(
        "  resolve with: hermes kanban triage-resolve <parent-id> "
        "--to todo|done|archived --reason \"...\"  (or `unblock` if blocked)"
    )


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway.

    Left in as a stub so users with the old command in scripts/systemd
    units get a clear migration message instead of a cryptic
    "no such command" error. A ``--force`` escape hatch keeps the old
    standalone daemon alive for the rare edge case where someone truly
    cannot run the gateway (e.g. running on a host that forbids
    long-lived background services), but the default path exits 2
    with guidance so nobody accidentally keeps running two dispatchers
    against the same kanban.db.
    """
    # --force lets power users keep the standalone loop for one more
    # release cycle. Undocumented in `--help` so nobody discovers it
    # casually — intentional.
    if not getattr(args, "force", False):
        print(
            "hermes kanban daemon: DEPRECATED — the dispatcher now runs\n"
            "inside the gateway. To use kanban:\n"
            "\n"
            "    hermes gateway start       # starts the gateway + embedded dispatcher\n"
            "\n"
            "Ready tasks will be picked up on the next dispatcher tick\n"
            "(default: every 60 seconds). Configure via config.yaml:\n"
            "\n"
            "    kanban:\n"
            "      dispatch_in_gateway: true      # default\n"
            "      dispatch_interval_seconds: 60\n"
            "      failure_limit: 2              # consecutive non-success attempts before auto-block\n"
            "\n"
            "Running both the gateway AND this standalone daemon will\n"
            "race for claims. If you truly need the old standalone\n"
            "daemon (no gateway available), rerun with --force.",
            file=sys.stderr,
        )
        return 2

    # Legacy path — same logic as before, kept behind --force.
    # Make sure the DB exists before printing "started" so the user sees the
    # correct DB path and any init error surfaces immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force "
        f"(interval={args.interval}s, pid={os.getpid()}). "
        f"Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers "
        f"racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but fails to
    # spawn any worker. Catches broken profiles, PATH drift, missing venv,
    # credential loss — cases where the per-task circuit breaker auto-blocks
    # each task quietly but the operator has no signal that the dispatcher
    # itself is dysfunctional.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        spawned_any = bool(res.spawned)
        if ready_pending and not spawned_any:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Emit a warning once per HEALTH_WINDOW bad ticks (not every tick)
        # so log volume stays bounded while the problem persists.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            # Rate-limit repeats: at most one warning per 5 minutes.
            if now - health_state["last_warn_at"] >= 300:
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: "
                    f"ready queue non-empty for {health_state['bad_ticks']} "
                    f"consecutive ticks but 0 workers spawned successfully. "
                    f"Check profile health (venv, PATH, credentials) and "
                    f"`hermes kanban list --status ready` / "
                    f"`hermes kanban list --status blocked` for recent "
                    f"spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
            or res.parent_satisfied_sticky
        )
        if did_work:
            sticky_ids = sorted(res.parent_satisfied_sticky)
            sticky_summary = (
                f"parents_done_sticky={len(sticky_ids)}"
                + (f" ({', '.join(sticky_ids)})" if sticky_ids else "")
            )
            print(
                f"[{_fmt_ts(int(time.time()))}] "
                f"reclaimed={res.reclaimed} crashed={len(res.crashed)} "
                f"timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)} {sticky_summary}",
                flush=True,
            )

    def _ready_queue_nonempty() -> bool:
        """Cheap probe — is there at least one ready+assigned+unclaimed
        task whose assignee maps to a real Hermes profile (i.e. one the
        dispatcher would actually try to spawn for)?

        Filters out tasks assigned to control-plane lanes
        (e.g. ``orion-cc``, ``orion-research``) that are pulled by
        terminals via ``claim_task`` directly — those are correctly idle
        from the dispatcher's perspective, not stuck.
        """
        try:
            with kb.connect_closing() as conn:
                return kb.has_spawnable_ready(conn)
        except Exception:
            return False

    try:
        kb.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = (
        {k.strip() for k in args.kinds.split(",") if k.strip()}
        if args.kinds else None
    )
    cursor = 0
    print("Watching kanban events. Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()
        cursor = int(row["m"])

    try:
        while True:
            with kb.connect_closing() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
                    "       t.assignee, t.tenant "
                    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200",
                    (cursor,),
                ).fetchall()
            for r in rows:
                cursor = max(cursor, int(r["id"]))
                if kinds and r["kind"] not in kinds:
                    continue
                if args.assignee and r["assignee"] != args.assignee:
                    continue
                if args.tenant and r["tenant"] != args.tenant:
                    continue
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else None
                except Exception:
                    payload = None
                pl = f" {payload}" if payload else ""
                print(
                    f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                    f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                    flush=True,
                )
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        stats = kb.board_stats(conn)
        stats["review_awaiting_human"] = kb.review_awaiting_human(conn)
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    # Print active lane overrides FIRST: when spawns are being re-routed
    # board-wide, that's the context for every number below it.
    lane_overrides = stats.get("lane_model_overrides") or []
    if lane_overrides:
        print("Active lane-model overrides:")
        for row in lane_overrides:
            lane = row.get("assignee") or "(board-wide)"
            ttl = _format_ttl(int(row.get("ttl_remaining_seconds") or 0))
            reason = row.get("reason")
            suffix = f" — {reason}" if reason else ""
            print(
                f"  {lane}: route={row.get('provider')}/{row.get('model')} "
                f"ttl={ttl} remaining{suffix}"
            )
        print()
    print("By status:")
    for k in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        n = stats["by_status"].get(k, 0)
        # ``triage`` is the only status that CANNOT clear itself — it exists
        # because an automated unblocker already gave up. Printed flush with
        # every other bucket it reads like ordinary queue depth, so a card
        # needing a human hides in plain sight (and silently strands its
        # children). Mark it.
        flag = "  <- needs a human" if k == "triage" and n else ""
        print(f"  {k:8s}  {n}{flag}")
    if stats["by_status"].get("triage"):
        print(
            "\nResolve triage with: hermes kanban triage-resolve <id> "
            "--to todo|done|archived --reason \"...\""
        )
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {who:20s}  {parts}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    line = kb.format_review_awaiting_human(stats.get("review_awaiting_human") or [])
    if line:
        print(f"\n{line}")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        # Parity NOTE resolved (2026-08-07 parity merge): fork main's
        # add_notify_sub previously lacked upstream's chat_type param, so
        # #470 carried an adaptation here. The merge lands the full upstream
        # signature (kanban_db.add_notify_sub now accepts chat_type +
        # delivery_metadata), so the adaptation is retired and this is a
        # plain upstream-form call again.
        kb.add_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type,
            thread_id=args.thread_id, user_id=args.user_id,
            user_id_alt=getattr(args, "user_id_alt", None),
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=getattr(args, "delivery_mode", None),
        )
    print(f"Subscribed {args.platform}:{args.chat_id}"
          + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        subs = kb.list_notify_subs(conn, args.task_id)
    if getattr(args, "json", False):
        print(json.dumps(subs, indent=2, ensure_ascii=False))
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        owner = f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else ""
        dmode = s.get("delivery_mode") or "notify"
        mode = "" if dmode == "notify" else f"  mode={dmode}"
        ctype = s.get("chat_type") or "dm"
        ct = "" if ctype == "dm" else f"  chat_type={ctype}"
        uid_alt = f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else ""
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}"
              f"  (since event {s['last_event_id']}){owner}{ct}{uid_alt}{mode}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.remove_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
    if not ok:
        print("(no such subscription)", file=sys.stderr)
        return 1
    print(f"Unsubscribed from {args.task_id}")
    return 0


_RoutingIdentity = tuple[str, str, str]
"""``(user_id, user_id_alt, scope_id)`` from one durable routing entry."""
_RoutingEvidence = tuple[str, str, str, str, str]
"""Complete identity plus the creator's session key AND session id.

``tasks.session_id`` holds the creating turn's session KEY for
gateway-created tasks but a raw session ID for worker/CLI-created ones, so
evidence must carry both representations to bind either kind of creator
(the 2026-08-12 phantom regression: key-only equality returned empty
evidence for every worker card)."""
_RoutingLane = tuple[str, str, str, str]
"""``(platform, chat_id, chat_type, thread_id)`` evidence boundary."""


def _routing_participant_index(
) -> "dict[_RoutingLane, set[_RoutingEvidence]] | None":
    """Map one exact routing lane to complete identities seen in that lane.

    Built from the gateway routing index (``state.db``'s ``gateway_routing``
    table), which is the durable record of every session key the gateway has
    resolved. Each entry carries its ``origin`` — including the three fields a
    notify-sub row needs to reproduce its creator's key. The participant
    segment is ``user_id_alt or user_id``; retaining both raw fields lets the
    repair write the row faithfully rather than putting an alt id into the
    lower-priority ``user_id`` slot. ``scope_id`` carries Slack's workspace key.

    Chat type and thread id are part of the evidence boundary. A per-user thread
    entry cannot identify an adjacent group subscription merely because both
    share the same platform/chat id.

    Returns a SET per lane, deliberately: the caller repairs only after existing
    row fields narrow that set to exactly one identity. ``None`` means the
    durable index could not be read completely; acting on partial evidence could
    hide a second candidate, so callers must fail closed.
    """
    index: dict[_RoutingLane, set[_RoutingEvidence]] = {}
    try:
        from hermes_state import SessionDB
        from gateway.routing_identity import (
            effective_routing_lane,
            routing_key_carries_identity,
        )
    except Exception:
        return None
    try:
        db = SessionDB()
    except Exception:
        return None
    try:
        loader = getattr(db, "load_gateway_routing_entries", None)
        if not callable(loader):
            return None
        scopes = {""}
        try:
            from hermes_constants import get_hermes_home

            scopes.add(str((get_hermes_home() / "sessions").resolve()))
        except Exception:
            pass
        seen: dict[str, str] = {}
        for scope in scopes:
            try:
                entries = loader(scope=scope)
            except Exception:
                return None
            if isinstance(entries, dict):
                seen.update({str(k): str(v) for k, v in entries.items()})
        for session_key, entry_json in seen.items():
            try:
                entry = json.loads(entry_json)
            except Exception:
                return None
            if not isinstance(entry, dict):
                return None
            origin = entry.get("origin")
            if not isinstance(origin, dict):
                return None
            platform = str(origin.get("platform") or "").strip().lower()
            chat_id = str(origin.get("chat_id") or "").strip()
            chat_type = str(origin.get("chat_type") or "").strip().lower()
            thread_id = str(origin.get("thread_id") or "").strip()
            prospective_thread_id = str(
                origin.get("prospective_thread_id") or ""
            ).strip()
            user_id = str(origin.get("user_id") or "").strip()
            user_id_alt = str(origin.get("user_id_alt") or "").strip()
            scope_id = str(
                origin.get("scope_id") or origin.get("guild_id") or ""
            ).strip()
            if not platform or not chat_id or not (user_id_alt or user_id):
                continue
            if not routing_key_carries_identity(
                session_key,
                platform=platform,
                chat_id=chat_id,
                chat_type=chat_type,
                thread_id=thread_id,
                prospective_thread_id=prospective_thread_id,
                user_id=user_id,
                user_id_alt=user_id_alt,
                scope_id=scope_id,
            ):
                continue
            lane = effective_routing_lane(
                platform=platform,
                chat_id=chat_id,
                chat_type=chat_type,
                thread_id=thread_id,
                prospective_thread_id=prospective_thread_id,
            )
            index.setdefault(lane, set()).add((
                user_id, user_id_alt, scope_id, session_key,
                str(entry.get("session_id") or "").strip(),
            ))
    finally:
        try:
            db.close()
        except Exception:
            pass
    return index


def _cmd_notify_repair(args: argparse.Namespace) -> int:
    """Backfill missing key identity on legacy notify subscriptions.

    See the subparser description for why a partial identity splits a chat into
    two sessions. Evidence comes from the gateway routing index; a chat with no
    unambiguous identity is reported and left untouched.
    """
    index = _routing_participant_index()
    evidence_unavailable = index is None
    try:
        from gateway.routing_identity import (
            creator_stamp_is_session_key as _stamp_is_key,
            effective_routing_lane as _effective_lane,
        )
    except Exception:  # pragma: no cover - same guard as the index import
        def _stamp_is_key(stamp):
            return ":" in str(stamp or "")

        def _effective_lane(**_kwargs):
            # Fail CLOSED, never fall back to a raw tuple. A hand-built lane
            # is the 2026-09-12 bug itself: it cannot match the canonicalized
            # index and would silently report "no evidence" for every row.
            # ``_routing_participant_index`` shares this import, so if it is
            # unavailable the index is already ``None`` and the caller treats
            # evidence as unavailable rather than as absent.
            return None

    def _resolve(row: dict) -> "dict[str, str | None] | None":
        platform = str(row.get("platform") or "").strip().lower()
        chat_id = str(row.get("chat_id") or "").strip()
        chat_type = str(row.get("chat_type") or "").strip().lower()
        thread_id = str(row.get("thread_id") or "").strip()
        creator_session_id = str(row.get("creator_session_id") or "").strip()
        if not creator_session_id:
            # No creator provenance at all: repair is a PERMANENT write, so
            # never adopt on lane evidence alone (negative-control contract —
            # cron/CLI/home-channel origins are legitimately user-less).
            return None
        lane = _effective_lane(
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
        )
        if lane is None:
            # Canonicalizer unavailable => no trustworthy lane => no repair.
            return None
        evidence = set((index or {}).get(lane, set()))
        # ``tasks.session_id`` holds the creating turn's session KEY for
        # gateway-created tasks (always contains ':') but a RAW session id
        # for worker/CLI-created ones (never does). #568 compared it against
        # index keys unconditionally, so worker cards always yielded empty
        # evidence (the 2026-08-12 phantom regression). Discriminate:
        # key-shaped -> strict creator binding (the #568 intent); raw-id or
        # unstamped -> bind when the index knows the creator, else fall back
        # to the lane-wide evidence; the exactly-one rule below still
        # refuses on 0 or >1 candidates either way.
        if _stamp_is_key(creator_session_id):
            bound = {
                item for item in evidence if item[3] == creator_session_id
            }
        else:
            creator_bound = {
                item for item in evidence
                if creator_session_id and item[4] == creator_session_id
            }
            bound = creator_bound or evidence
        candidates = {item[:3] for item in bound}
        for position, field in enumerate(("user_id", "user_id_alt", "scope_id")):
            existing = str(row.get(field) or "").strip()
            if existing:
                candidates = {
                    identity for identity in candidates
                    if identity[position] == existing
                }
        if len(candidates) != 1:
            # 0 => a genuinely user-less origin (cron / CLI / home channel).
            # >1 => a shared chat; picking one would hijack another user's lane.
            return None
        user_id, user_id_alt, scope_id = next(iter(candidates))
        return {
            "user_id": user_id or None,
            "user_id_alt": user_id_alt or None,
            "scope_id": scope_id or None,
        }

    if getattr(args, "all_boards", False):
        results = []
        # --all-boards sweeps every board on disk: enumeration, not addressing.
        # The extent spans the body so the `connect_closing(board=slug)` below
        # is covered too.
        for meta in kb.enumerating_each(kb.list_boards()):
            slug = str(meta.get("slug") or "").strip()
            if not slug:
                continue
            try:
                with kb.connect_closing(board=slug) as conn:
                    board_rows = kb.backfill_notify_sub_user_ids(
                        conn, _resolve,
                        dry_run=bool(getattr(args, "dry_run", False)),
                        evidence_unavailable=evidence_unavailable,
                    )
            except Exception as exc:
                print(f"  (board {slug!r}: skipped — {exc})", file=sys.stderr)
                continue
            for r in board_rows:
                r["board"] = slug
            results.extend(board_rows)
    else:
        with kb.connect_closing() as conn:
            results = kb.backfill_notify_sub_user_ids(
                conn, _resolve, dry_run=bool(getattr(args, "dry_run", False)),
                evidence_unavailable=evidence_unavailable,
            )

    repaired = [r for r in results if r["action"] == "backfilled"]
    skipped = [r for r in results if r["action"] != "backfilled"]

    if getattr(args, "json", False):
        action_counts = {
            action: sum(r["action"] == action for r in results)
            for action in (
                "skipped_no_evidence", "skipped_evidence_unavailable",
                "skipped_raced",
            )
        }
        print(json.dumps({
            "dry_run": bool(getattr(args, "dry_run", False)),
            "considered": len(results),
            "backfilled": len(repaired),
            **action_counts,
            "rows": results,
        }, indent=2, ensure_ascii=False))
        return 0

    if not results:
        print("No incomplete notify-subscription identities — nothing to repair.")
        return 0

    verb = "Would backfill" if getattr(args, "dry_run", False) else "Backfilled"
    print(f"{verb} {len(repaired)} of {len(results)} incomplete subscription(s).")
    for row in repaired:
        thr = f":{row['thread_id']}" if row.get("thread_id") else ""
        print(f"  {row['task_id']:12s} {row['platform']}:{row['chat_id']}{thr}"
              f"  -> user_id={row['user_id']}"
              f" user_id_alt={row['user_id_alt']} scope_id={row['scope_id']}"
              f" ({', '.join(row['backfilled_fields'])})")
    if skipped:
        print(f"\nLeft untouched ({len(skipped)}).")
        if any(r["action"] == "skipped_evidence_unavailable" for r in skipped):
            print("  Routing evidence was unavailable; repair failed closed.")
        if any(r["action"] == "skipped_raced" for r in skipped):
            print("  A row changed during repair; no partial identity was written.")
        if any(r["action"] == "skipped_no_evidence" for r in skipped):
            print("  No unambiguous creator identity. User-less origins and shared")
            print("  chats remain on their legitimate shared per-chat session.")
        for row in skipped:
            thr = f":{row['thread_id']}" if row.get("thread_id") else ""
            print(f"  {row['task_id']:12s} {row['platform']}:{row['chat_id']}{thr}"
                  f"  [{row['action']}]")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        print(f"(no log for {args.task_id} — task may not have spawned yet)",
              file=sys.stderr)
        return 1
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban runs: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "id": r.id, "profile": r.profile, "status": r.status,
                "outcome": r.outcome, "started_at": r.started_at,
                "ended_at": r.ended_at, "summary": r.summary,
                "error": r.error, "metadata": r.metadata,
                "worker_pid": r.worker_pid, "step_key": r.step_key,
            } for r in runs
        ], indent=2, ensure_ascii=False))
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        if elapsed < 60:
            el = f"{elapsed}s"
        elif elapsed < 3600:
            el = f"{elapsed // 60}m"
        else:
            el = f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            # Indent and truncate long summaries to keep the table readable.
            summary = r.summary.splitlines()[0][:100]
            print(f"     → {summary}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _cmd_specify(args: argparse.Namespace) -> int:
    """Flesh out a triage task (or all of them) via auxiliary LLM,
    then promote to todo. Thin wrapper over ``kanban_specify``."""
    from hermes_cli import kanban_specify as spec

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = spec.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"specified": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: specify requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    fail_count = 0
    for tid in ids:
        try:
            outcome = spec.specify_task(tid, author=author)
        except kb.ForeignSessionMutationError as exc:
            outcome = spec.SpecifyOutcome(tid, False, str(exc))
        if outcome.ok:
            ok_count += 1
        else:
            fail_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            title_suffix = (
                f" — retitled: {outcome.new_title!r}"
                if outcome.new_title
                else ""
            )
            print(f"Specified {outcome.task_id} → todo{title_suffix}")
        else:
            print(
                f"kanban: specify {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: succeed if at least one promotion landed; exit 1 only when
    # every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all of them) out into a graph of child
    tasks via the auxiliary LLM, routed to specialist profiles by
    description. Thin wrapper over ``kanban_decompose``."""
    from hermes_cli import kanban_decompose as decomp

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = decomp.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"decomposed": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: decompose requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    for tid in ids:
        try:
            outcome = decomp.decompose_task(tid, author=author)
        except kb.ForeignSessionMutationError as exc:
            outcome = decomp.DecomposeOutcome(tid, False, str(exc))
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "fanout": outcome.fanout,
                "child_ids": outcome.child_ids,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            if outcome.fanout and outcome.child_ids:
                child_summary = ", ".join(outcome.child_ids)
                print(
                    f"Decomposed {outcome.task_id} → {len(outcome.child_ids)} "
                    f"children ({child_summary}); root promoted to todo"
                )
            else:
                title_suffix = (
                    f" — retitled: {outcome.new_title!r}"
                    if outcome.new_title
                    else ""
                )
                print(
                    f"Specified {outcome.task_id} → todo "
                    f"(no fanout){title_suffix}"
                )
        else:
            print(
                f"kanban: decompose {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove scratch workspaces of archived tasks, prune old events, and
    delete old worker logs."""

    scratch_root = kb.workspaces_root()
    dry_run = bool(getattr(args, "dry_run", False))
    done_days = getattr(args, "done_retention_days", 3)
    # DONE cards are swept too once they have been finished for done_days:
    # completion cleanup is best-effort and refusals were never retried, so
    # a done card's workspace otherwise lives until someone archives it.
    # Every removal still goes through the same survivor/liveness/audit gates.
    done_cutoff = (
        int(time.time()) - done_days * 24 * 3600 if done_days is not None and done_days >= 0
        else None
    )
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE status = 'archived' OR (status = 'done' AND ? IS NOT NULL "
            "AND COALESCE(completed_at, started_at, created_at) <= ?)",
            (done_cutoff, done_cutoff),
        ).fetchall()
    if dry_run:
        would = []
        for row in rows:
            if row["workspace_kind"] not in ("scratch", "worktree"):
                continue
            p = row["workspace_path"] or (
                str(scratch_root / row["id"]) if row["workspace_kind"] == "scratch" else None
            )
            if p and Path(p).is_dir():
                would.append((row["id"], p))
        for tid, p in would:
            print(f"would-try {tid} {p}")
        print(f"GC dry-run: {len(would)} workspace candidate(s); nothing removed "
              "(survivor/liveness gates are evaluated only on a real run)")
        return 0
    # One machine-wide process-cwd scan serves every candidate's liveness
    # probe in this run (card t_ee808d83); a per-path `lsof +D` walked each
    # workspace and timed out on large ones, so nothing was ever reclaimed.
    with kb.process_cwd_snapshot_scope():
        removed_ws = _gc_remove_workspaces(rows, scratch_root)

    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    with kb.connect_closing() as conn:
        removed_events = kb.gc_events(
            conn, older_than_seconds=event_days * 24 * 3600,
        )
    removed_logs = kb.gc_worker_logs(
        older_than_seconds=log_days * 24 * 3600,
    )
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


def _gc_remove_workspaces(rows, scratch_root: Path) -> int:
    removed_ws = 0
    for row in rows:
        if row["workspace_kind"] == "worktree":
            # Backstop for worktrees that escaped the completion/archive hook
            # (e.g. tasks archived before that hook existed). Same safety
            # predicate: only clean, fully-pushed worktrees are removed.
            wt_path = row["workspace_path"]
            if wt_path and Path(wt_path).is_dir():
                with kb.connect_closing() as conn:
                    # Liveness + audit now live inside the worktree lane too
                    # (card t_63fb42f9).
                    kb._cleanup_worktree_workspace(
                        row["id"], wt_path, row["branch_name"],
                        conn=conn, reason="gc_archived",
                    )
                if not Path(wt_path).is_dir():
                    removed_ws += 1
            continue
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        # The old guard here was a bare ``path.relative_to(scratch_root)``.
        # ``Path.relative_to`` SUCCEEDS on an equal path (it returns '.'), so an
        # archived row whose workspace_path was the workspaces ROOT itself would
        # pass the containment check and rmtree every live card's scratch dir in
        # one call -- the 2026-09-20 incident. safe_remove_workspace_dir requires
        # STRICT descendancy, refuses any card with a live run, audits both
        # outcomes, and performs the removal through
        # kanban_survivor.remove_workspace_dir so survivor preservation (#783)
        # still runs underneath.
        with kb.connect_closing() as conn:
            if kb.safe_remove_workspace_dir(
                path, task_id=row["id"], reason="gc_archived", conn=conn,
            ):
                removed_ws += 1
    return removed_ws


def _cmd_repair(args: argparse.Namespace) -> int:
    """Check DB integrity and apply the narrow index-REINDEX auto-repair.

    Dispatched BEFORE the auto ``kb.init_db()`` in :func:`kanban_command`
    (init itself refuses corrupt DBs), so this is reachable on exactly the
    boards that need it. Exit codes: 0 = healthy / repaired / no DB file,
    1 = still corrupt (non-index corruption, or REINDEX did not produce a
    clean re-check).
    """
    if getattr(args, "reclassify_quota_crashes", False):
        import sqlite3
        from hermes_cli.kanban_quota_repair import reclassify_quota_crashes

        conn = None
        try:
            dry_run = bool(getattr(args, "dry_run", False))
            conn = kb.connect_readonly() if dry_run else kb.connect()
            conn.row_factory = sqlite3.Row
            quota_report = reclassify_quota_crashes(conn, dry_run=dry_run)
        except Exception as exc:
            print(f"kanban repair: {exc}", file=sys.stderr)
            return 1
        finally:
            if conn is not None:
                conn.close()
        if getattr(args, "json", False):
            print(json.dumps(quota_report, indent=2))
        else:
            prefix = "Would repair" if dry_run else "Repaired"
            print(f"{prefix} {quota_report['reclassified']} quota-crashed runs; "
                  f"{len(quota_report['unblocked'])} cards unblocked.")
        return 0
    if getattr(args, "dry_run", False):
        print("kanban repair: --dry-run requires --reclassify-quota-crashes", file=sys.stderr)
        return 2
    try:
        report = kb.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        print(f"kanban repair: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": (
                str(report.backup_path) if report.backup_path else None
            ),
            "reindexed": report.reindexed,
        }, indent=2))
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    print(f"{report.db_path}: CORRUPT.", file=sys.stderr)
    for line in (report.messages or [])[:10]:
        print(f"  {line}", file=sys.stderr)
    if report.reindexed:
        print(
            f"  REINDEX ({', '.join(report.reindexed)}) attempted but "
            f"integrity_check is still failing:",
            file=sys.stderr,
        )
        for line in (report.post_repair_messages or [])[:10]:
            print(f"    {line}", file=sys.stderr)
    else:
        print(
            "  Not an index-only failure — automatic REINDEX repair does "
            "not apply (fail-closed).",
            file=sys.stderr,
        )
    if report.backup_path:
        print(f"  corrupt copy quarantined at: {report.backup_path}",
              file=sys.stderr)
    print(
        "  Recover manually (e.g. `sqlite3 kanban.db \".recover\"` into a "
        "fresh file) or move the file aside to start a new board.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Slash-command entry point (used by /kanban from CLI and gateway)
# ---------------------------------------------------------------------------

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `attach <id> <path>`  Attach a local file; `attachments <id>` to list
  `complete <id>…`      Mark task(s) done
  `request-review <id>` Enter first-class review; `request-changes <id> <reason>` returns an active review to its implementer
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str, *, session_id: Optional[str] = None) -> str:
    """Execute a ``/kanban …`` string and return captured stdout/stderr.

    ``rest`` is everything after ``/kanban`` (may be empty).  Used from
    both the interactive CLI (``self._handle_kanban_command``) and the
    gateway (``_handle_kanban_command``) so formatting is identical.

    ``session_id`` is the invoking chat session. The gateway passes it
    explicitly so ``create`` stamps it and the home-session guard sees it;
    ``None`` falls back to :func:`_caller_session_id`'s normal resolution.
    """
    token = _SLASH_SESSION_ID.set((session_id or "").strip() or None)
    try:
        return _run_slash(rest)
    finally:
        _SLASH_SESSION_ID.reset(token)


def _run_slash(rest: str) -> str:
    import io
    import contextlib

    tokens = shlex.split(rest) if rest and rest.strip() else []

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP

    # Single argparse tree rooted at "/kanban".  build_parser() expects a
    # subparsers action to attach to, so build a throwaway one and pull
    # the kanban_parser back out — then drive it directly so usage/error
    # text reads as ``/kanban`` (not ``/kanban-wrap kanban``).
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    _top_sub = _wrap.add_subparsers(dest="_top")
    kanban_parser = build_parser(_top_sub)
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    for _action in kanban_parser._actions:
        if isinstance(_action, argparse._SubParsersAction):
            for _name, _choice in _action.choices.items():
                _choice.prog = f"/kanban {_name}"
                _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in kanban_parser._actions:
                if isinstance(_action, argparse._SubParsersAction):
                    subparser = _action.choices.get(tokens[0])
                    if subparser is not None:
                        return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # ``-h`` / ``--help`` makes argparse print to stdout and SystemExit(0).
    # Capture both streams so neither the help text nor the error text
    # bypasses our buffer.
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out = buf_out.getvalue().rstrip()
        err = buf_err.getvalue().rstrip()
        # Help dump (exit 0) → return the captured help text directly.
        if exc.code in {0, None} and out:
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out = buf_out.getvalue().rstrip()
    err = buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
