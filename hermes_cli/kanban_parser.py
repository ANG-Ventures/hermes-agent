"""Argparse tree for ``hermes kanban …`` (``build_parser``).

The subcommand tree is declared as data — one ``_cmd(...)`` record per
subcommand holding its ``add_parser`` kwargs and an ordered tuple of
``add_argument`` specs — and materialised by ``_add_commands``. Order of
records and arguments is the order argparse renders in ``--help``.
"""

from __future__ import annotations

import argparse

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn


def _arg(*flags: str, **kw):
    return (flags, kw)


def _group(*args, **kw):
    """A mutually exclusive group of ``_arg`` specs (``kw`` → ``add_mutually_exclusive_group``)."""
    return ("__group__", {"args": tuple(args), **kw})


def _cmd(name: str, args=(), *, children=None, setup=None, **parser_kw):
    """``children`` = ``(dest, [specs])`` for a nested subparser group;
    ``setup`` = callable(parser) for verbs whose arguments live with their
    implementation module (``clone``, ``base-check``)."""
    return (name, parser_kw, tuple(args), children, setup)


def _add_arguments(p: argparse.ArgumentParser, args) -> None:
    for flags, kw in args:
        if flags == "__group__":
            kw = dict(kw)
            specs = kw.pop("args")
            _add_arguments(p.add_mutually_exclusive_group(**kw), specs)
        else:
            p.add_argument(*flags, **kw)


def _add_commands(sub: argparse._SubParsersAction, specs) -> None:
    for name, parser_kw, args, children, setup in specs:
        p = sub.add_parser(name, **parser_kw)
        _add_arguments(p, args)
        if setup is not None:
            setup(p)
        if children:
            dest, child_specs = children
            _add_commands(p.add_subparsers(dest=dest), child_specs)


def _json_flag(**kw):
    return _arg("--json", action="store_true", **kw)


def _reason(help: str):
    return _arg("--reason", help=help)


def _nonnegative_int(value: str) -> int:
    """argparse type for retention days: a negative window builds a future cutoff
    that matches every row, so reject it at the CLI boundary before any sweep."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("retention days must be >= 0 (0 disables that sweep)")
    return parsed


def _run_state_args(type_help: str):
    return (
        _arg("--state-type", choices=("status", "outcome"), help=f"With --state-name: {type_help}"),
        _arg("--state-name", metavar="VALUE",
             help="With --state-type: keep runs whose column equals this value"),
    )


def _triage_sweep_args(verb: str, Verb: str, noun: str):
    """Shared ``specify`` / ``decompose`` arguments."""
    return (
        _arg("task_id", nargs="?", help=f"Task id to {verb} (required unless --all is given)"),
        _arg("--all", dest="all_triage", action="store_true", help=f"{Verb} every task currently in the triage column"),
        _arg("--tenant", help="When used with --all, restrict the sweep to this tenant"),
        _arg("--author",
             help=f"Author name recorded on the audit comment (default: $HERMES_PROFILE or '{noun}')"),
        _json_flag(help="Emit one JSON object per task on stdout"),
    )


def _bulk_ids(verb: str):
    return _arg("--ids", nargs="+", help=f"Additional task ids to {verb} with the same reason (bulk mode)")


def _clone_setup(p: argparse.ArgumentParser) -> None:
    from hermes_cli.kanban_clone import add_arguments
    add_arguments(p)


def _base_check_setup(p: argparse.ArgumentParser) -> None:
    from hermes_cli.kanban_branch_base import add_arguments
    add_arguments(p)


_TASK_ID = _arg("task_id")
_TASK_IDS = _arg("task_ids", nargs="+")
_SLUG = _arg("slug")
_TENANT = _arg("--tenant", help="Tenant namespace")
_PRIORITY = _arg("--priority", type=int, default=0, help="Priority tiebreaker")
_RECLAIM_REASON = _reason("Human-readable reason (recorded on the reclaimed event)")
_NOTIFY_TARGET = (
    _arg("--platform", required=True),
    _arg("--chat-id", required=True),
    _arg("--thread-id"),
)
_STEP_HANDOFF = (
    _arg("--summary", help="Structured handoff summary. Falls back to --result if omitted."),
    _arg("--metadata", help="JSON dict of structured facts to store on the latest completed run."),
)
_PIN_SUB_HELP = (
    "Deliberately pin this card to ONE Claude sub (--provider claude-bpx-N / claude-apx-N) "
    "with a non-empty reason, recorded as a 'sub pin:' comment and shown as [PIN ...] in "
    "show/list. Pins provider + model + effort together (--model/--effort). Workers ride the "
    "pool by default; pre-rename aliases (claude-api-proxy, claude-bridge, -fN) and subs not "
    "enabled in the usage registry are refused even with this flag. Sub 0 is a last-resort pin "
    "(only when Ace asks or every other sub is capped). A capped pinned sub WAITS (no profile fallback)."
)
_PIN_SUB = _arg("--pin-sub", dest="pin_sub", metavar="REASON", help=_PIN_SUB_HELP)
_PIN_SUB_FALLBACK = _arg(
    "--pin-sub-fallback", action="store_true", dest="pin_sub_fallback",
    help="With --pin-sub: when the pinned sub is capped/cooling, ride its family pool "
         "(bpx-N -> claude-bpr, apx-N -> claude-apr) instead of waiting.",
)
_ALLOW_FLAGSHIP = _arg(
    "--allow-flagship", "--firepower", dest="allow_flagship", metavar="REASON",
    help="Allow an orchestrator-only flagship model for this task. Requires a non-empty "
         "reason, recorded as a task comment. --firepower is an alias.",
)
_HANDBACK_FLAGS = (
    _arg("--request-changes", dest="request_changes", metavar="REASON",
         help="Required to hand a card in review with an open PR back: records "
              "changes_requested (operator), status ready, assignee = implementer"),
    _arg("--worker-ok", action="store_true", dest="worker_ok",
         help="Allow a worker profile on a no-worker (operator-only) card; "
              "clears the flag and records no_worker_cleared"),
)

_BOARD_SPECS = [
    _cmd("list", [
        _json_flag(),
        _arg("--all", action="store_true", help="Include archived boards too"),
    ], aliases=["ls"], help="List all boards with task counts"),
    _cmd("create", [
        _arg("slug", help="Board slug (kebab-case, e.g. atm10-server)"),
        _arg("--name", help="Human-readable display name (defaults to Title Case of slug)"),
        _arg("--description", help="Optional description"),
        _arg("--icon", help="Optional emoji or single-character icon for the dashboard"),
        _arg("--color", help="Optional hex color (e.g. '#8b5cf6') for the dashboard"),
        _arg("--switch", action="store_true", help="Switch to the new board after creating it"),
        _arg("--default-workdir", help="Default workspace path for tasks created on this board"),
    ], aliases=["new"], help="Create a new board"),
    _cmd("rm", [
        _SLUG,
        _arg("--delete", action="store_true",
             help="Hard-delete the board directory instead of archiving it. "
                  "Default is to move it to boards/_archived/ so it's recoverable."),
    ], aliases=["remove", "delete"], help="Archive (default) or delete a board"),
    _cmd("switch", [_SLUG], aliases=["use"], help="Set the active board for subsequent CLI calls"),
    _cmd("show", aliases=["current"], help="Print the currently-active board slug"),
    _cmd("rename", [_SLUG, _arg("name", help="New display name")],
         help="Change a board's human-readable display name (slug is immutable)"),
    _cmd("set-default-workdir", [
        _SLUG,
        _arg("path", nargs="?", help="Absolute path to use as default workdir. Omit to clear."),
    ], help="Set the default workspace path for tasks on a board"),
    _cmd("export", [
        _arg("slug", nargs="?", help="Board to export (default: the current board)"),
        _arg("-o", "--output", help="Archive path (default: ./<slug>.tar.gz)"),
        _arg("--no-attachments", action="store_true", help="Skip attachment files, keeping the archive small"),
        _arg("--include-logs", action="store_true", help="Include per-task worker logs"),
        _json_flag(),
    ], help="Export a board to a portable .tar.gz archive", description=(
        "Package a board's tasks, comments, links, history, and file attachments into one archive "
        "that can be imported on another machine. Claims, worker PIDs, chat subscriptions, and "
        "paths belonging to this machine are stripped. Workspaces are never included — they are "
        "rebuilt on demand."
    )),
    _cmd("import", [
        _arg("archive", help="Path to the .tar.gz archive"),
        _arg("--as", dest="as_slug", help="Slug for the imported board (default: from the archive)"),
        _arg("--switch", action="store_true", help="Switch to the imported board afterwards"),
        _json_flag(),
    ], help="Import a board archive as a new board", description=(
        "Import a .tar.gz produced by `hermes kanban boards export`. The board always lands as a "
        "NEW board — the slug gains a numeric suffix if it is already taken — so an import can "
        "never overwrite or merge into a board you already have."
    )),
]

_LANE_MODEL_SPECS = [
    _cmd("set", [
        _arg("route", nargs="?", metavar="PROVIDER/MODEL",
             help="Route to pin the lane to, e.g. claude-bpx-19/claude-opus-5. "
                  "May also be given as --provider/--model."),
        _arg("--provider"),
        _arg("--model"),
        _arg("--model-json", help="Unified override object as JSON."),
        _arg("--effort", dest="reasoning_effort"),
        _arg("--ttl", metavar="DURATION",
             help="How long the override lives (30m, 2h, 1d). Required — an "
                  "override without an expiry is just a config edit with extra steps."),
        _reason("Why the lane is being re-routed (shown in show/stats)."),
        _arg("--assignee", help="Restrict the override to one profile. Omit for board-wide."),
        _arg("--firepower", "--allow-flagship", dest="firepower", metavar="REASON",
             help="Required justification when the model is flagship/firepower-only "
                  "(same flagship ban as create/set-model --allow-flagship)."),
        _arg("--pin-sub", dest="pin_sub", metavar="REASON",
             help="Authorize a single-sub lane (claude-bpx-N / claude-apx-N) with a "
                  "reason. Same refusals as set-model --pin-sub (pre-rename "
                  "aliases, subs not enabled in the usage registry); a capped "
                  "pinned lane holds its cards."),
    ], help="Install a lane override that expires on its TTL"),
    _cmd("show", [_json_flag(dest="as_json")], help="Show active lane overrides and their remaining TTL"),
    _cmd("clear", [
        _arg("--assignee", help="Lane to clear. Omit for the board-wide override."),
        _arg("--all", action="store_true", dest="clear_all", help="Clear every lane override on the board."),
    ], help="Remove a lane override before its TTL elapses"),
]

_WORKSPACE_SPECS = [
    _cmd("reset", [
        _arg("task_ids", nargs="*"),
        _arg("--all-stranded", action="store_true", help="Reset every scratch card whose persisted path is gone"),
        _arg("--dry-run", action="store_true"),
        _reason("Recorded on the workspace_reset event"),
    ], help="Clear a stranded scratch card's dead workspace_path so the dispatcher recreates <root>/<board>/<id>"),
]

# Top-level ``hermes kanban <action>`` records, in ``--help`` order.
_SPECS = [
    _cmd("init", help="Create kanban.db if missing (idempotent)"),
    _cmd("boards", children=("boards_action", _BOARD_SPECS),
         help="Manage kanban boards (one board per project / workstream)",
         description=(
             "Boards let you separate unrelated streams of work (projects, repos, domains) into "
             "isolated queues. Each board has its own DB, workspaces directory, and dispatcher "
             "loop — tasks on one board cannot collide with tasks on another. The first board is "
             "'default' and always exists."
         )),
    _cmd("create", [
        _arg("title", help="Task title"),
        _arg("--body", help="Optional opening post"),
        _arg("--body-file", metavar="PATH",
             help="Read the opening post from a file ('-' = stdin), so bodies with embedded "
                  "newlines, backticks, $(...) or flag-like lines survive shell quoting. "
                  "Mutually exclusive with --body."),
        _arg("--assignee", help="Profile name to assign"),
        _arg("--parent", action="append", default=[], help="Parent task id (repeatable)"),
        _arg("--parent-kind", default=kb.DEFAULT_LINK_KIND, choices=sorted(kb.VALID_LINK_KINDS),
             help="Semantics for every --parent edge. 'blocks' (default) holds this "
                  "task until the parents are done; 'derived-from' records that a "
                  "parent discovered/spawned this work without gating it."),
        _arg("--workspace",
             help="scratch | worktree | worktree:<path> | dir:<path> (default: scratch; "
                  "an explicit 'scratch' also opts out of a project-scoped board's project)"),
        _arg("--branch", help="Branch name for worktree tasks, e.g. wt/t6-wire"),
        _arg("--project",
             help="Link to a project (id or slug). Anchors the task's "
                  "worktree under the project's primary repo with a "
                  "deterministic branch. See `hermes project list`."),
        _TENANT,
        _PRIORITY,
        _arg("--triage", action="store_true",
             help="Park in triage — a specifier will flesh out the spec and promote to todo"),
        _arg("--idempotency-key",
             help="Dedup key. If a non-archived task with this key exists, "
                  "its id is returned instead of creating a duplicate."),
        _arg("--max-runtime",
             help="Per-task runtime cap. Accepts seconds (300) or durations (90s, "
                  "30m, 2h, 1d). When exceeded, the dispatcher SIGTERMs (then "
                  "SIGKILLs) the worker and re-queues the task."),
        _arg("--force", dest="force_reason", metavar="REASON",
             help="File the card even though a non-archived card with the same title was "
                  "created in the last 24h (near-duplicate guard). The reason is recorded "
                  "as a near_duplicate_forced event."),
        _arg("--created-by", default="user", help="Author name recorded on the task (default: user)"),
        _arg("--skill", action="append", default=[], dest="skills",
             help="Skill to force-load into the worker (repeatable). The kanban "
                  "lifecycle is already injected automatically. Example: --skill "
                  "translation --skill github-code-review"),
        _arg("--max-retries", type=int, metavar="N",
             help="Per-task override for the consecutive-failure "
                  f"circuit breaker. Trip on the Nth failure — e.g. --max-retries 1 blocks on the "
                  f"first failure (no retries), --max-retries 3 allows two retries. Omit to use "
                  f"the dispatcher's kanban.failure_limit config (default "
                  f"{kb.DEFAULT_FAILURE_LIMIT})."),
        _arg("--model", dest="model_override",
             help="Pin the worker to this model (passed as -m <model>) without "
                  "changing the profile's configured model. Combine with --provider "
                  "when the model belongs to a different backend than the profile's default."),
        _arg("--provider", dest="provider_override",
             help="Provider the --model belongs to (passed as --provider <name> to "
                  "the worker). Requires --model."),
        _ALLOW_FLAGSHIP,
        _PIN_SUB,
        _PIN_SUB_FALLBACK,
        _arg("--reasoning", "--effort", dest="reasoning_effort", metavar="LEVEL",
             help="Pin the worker's reasoning effort for this task (none, minimal, "
                  "low, medium, high, xhigh, max, or ultra). 'none' disables "
                  "thinking. Independent of --model; omit to inherit the "
                  "assignee profile default."),
        _arg("--brain", default=None, dest="brain", metavar="LANE",
             help="Per-card harness brain for a foreign-lane worker (cc-worker): "
                  f"{kb.CARD_BRAIN_ALLOWED}. Overrides the profile's "
                  "foreign_lane.brain for this card only; omit to inherit it."),
        _arg("--completion-contract", metavar="CONTRACT",
             help="local-only (default), OWNER/REPO for publication, or exact GitHub PR URL; required CI gates done."),
        _arg("--goal", action="store_true", dest="goal_mode",
             help="Run the worker in a goal loop: after each turn a judge checks the "
                  "response against the card title/body and, if not done, the worker "
                  "keeps going in the same session until the judge agrees it's "
                  "complete (or the turn budget runs out, which blocks the card for "
                  "review). Best for open-ended cards one shot rarely finishes."),
        _arg("--goal-max-turns", type=int, metavar="N", dest="goal_max_turns",
             help="Turn budget for --goal workers (default 20). Ignored without --goal."),
        _arg("--initial-status", choices=sorted(kb.VALID_INITIAL_STATUSES), default="running",
             help="Initial card status. Use 'blocked' for cards "
                  "that require immediate human ops (R3 gate) "
                  "to skip the brief running-to-blocked transition."),
        _arg("--session", metavar="SESSION_ID",
             help="Home session to stamp on the card (default: $HERMES_SESSION_ID when set; "
                  "'none' = unstamped). An explicit --session WINS over a --parent's home; "
                  "omitted, the child follows the parent's current home."),
        _arg("--home", metavar="operator",
             help="Home the card on the fleet operator pseudo-session (operator:apollo) that "
                  "any operator-profile session may act on. For cron/script minters with no "
                  "session of their own: create REFUSES a card that would be born unhomed. "
                  "Accepts 'operator' or 'operator:<name>'; exclusive with --session."),
        _json_flag(help="Emit JSON output"),
    ], help="Create a new task"),
    _cmd("swarm", [
        _arg("goal", help="Swarm goal / final outcome"),
        _arg("--worker", action="append", default=[], metavar="PROFILE:TITLE[:SKILL,SKILL]",
             help="Parallel worker card (repeatable)"),
        _arg("--verifier", required=True, help="Verifier profile"),
        _arg("--synthesizer", required=True, help="Synthesizer/writer profile"),
        _TENANT,
        _PRIORITY,
        _arg("--created-by", help="Creator/anchor profile"),
        _arg("--idempotency-key", help="Dedup key for the root card"),
        _json_flag(help="Emit JSON output"),
    ], help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)"),
    _cmd("list", [
        _arg("--mine", action="store_true", help="Filter by $HERMES_PROFILE as assignee"),
        _arg("--assignee"),
        _arg("--status", choices=sorted(kb.VALID_STATUSES)),
        _arg("--tenant"),
        _arg("--session",
             help="Filter by originating chat/agent session id (set on tasks created from inside an ACP loop)"),
        _arg("--home", "--this-session", action="store_true", dest="home",
             help="Only cards whose home session is this session (== --session $HERMES_SESSION_ID)"),
        _arg("--all", action="store_true", dest="flat_all",
             help="Flat board-wide view (no THIS SESSION / OTHER SESSIONS grouping)"),
        _arg("--archived", action="store_true", help="Include archived tasks"),
        _json_flag(),
        _arg("--sort", choices=sorted(kb.VALID_SORT_ORDERS.keys()),
             help="Sort order for listed tasks (default: priority)"),
        _arg("--workflow-template-id", metavar="ID", help="Restrict to tasks with this workflow_template_id"),
        _arg("--step-key", dest="current_step_key", metavar="KEY",
             help="Restrict to tasks with this current_step_key"),
    ], aliases=["ls"], help="List tasks"),
    _cmd("show", [_TASK_ID, _json_flag(), *_run_state_args("filter listed runs by task_runs column")],
         help="Show a task with comments + events"),
    _cmd("pins", [
        _json_flag(dest="as_json"),
        _arg("--stale-hours", type=float, dest="stale_hours",
             help="Exit 1 when any card pin is at least this old (daily lint)."),
    ], help="List live deliberate sub pins (--pin-sub) on cards and lanes"),
    _cmd("assign", [
        _TASK_ID,
        _arg("profile", help="Profile name (or 'none' to unassign)"),
        *_HANDBACK_FLAGS,
    ], help="Assign or reassign a task"),
    _cmd("set-model", [
        _arg("task_ids", nargs="*", metavar="task_id",
             help="One or more task ids. Omit when selecting with --where/--all-active. "
                  "The LAST positional is the model."),
        _arg("--model", help="Model id (alternative to the last positional)."),
        _arg("--model-json", help="Unified override object as JSON."),
        _arg("--provider",
             help="Provider the model belongs to (worker is spawned with "
                  "--provider <name>). Cleared together with the model."),
        _arg("--where", nargs="+", metavar="KEY=VALUE",
             help="Select cards by status/assignee instead of naming ids, e.g. "
                  "--where status=running,ready assignee=daedalus-opus. Only "
                  "active (non-done, non-archived) cards are considered."),
        _arg("--all-active", action="store_true", dest="all_active",
             help="Select every active card on the board (excludes done/archived)."),
        _arg("--reclaim", action="store_true",
             help="Release the claim on selected RUNNING cards so the next dispatch respawns "
                  "them on the new route. Without this a running worker keeps its old model "
                  "until it finishes. The running worker is terminated; the next run is a "
                  "FRESH session seeded from the card body + comments + the same workspace, "
                  "so post a checkpoint comment before reclaiming mid-task."),
        _arg("--live", action="store_true",
             help="Switch selected RUNNING workers to the new route/effort at their next loop "
                  "iteration, WITHOUT aborting them: same process, same conversation, same "
                  "workspace. Costs one prompt-cache miss on a model/provider change "
                  "(effort-only is near-free). Cards that are not running just get the "
                  "next-dispatch write. Needs an explicit model and/or --effort (clears are "
                  "refused); cannot be combined with --reclaim."),
        _arg("--allow-flagship", "--firepower", dest="allow_flagship", metavar="REASON",
             help="Allow an orchestrator-only flagship model. Requires a non-empty "
                  "reason, recorded as a task comment. --firepower is an alias."),
        _PIN_SUB,
        _PIN_SUB_FALLBACK,
        _group(
            _arg("--effort", dest="reasoning_effort", metavar="LEVEL",
                 help="Per-task reasoning effort (worker is spawned with --reasoning <level>). "
                      "'none' is a real level — thinking off. Independent of the model: with "
                      "--effort alone the model override is left untouched, and clearing the "
                      "model never resets the effort."),
            _arg("--clear-effort", action="store_true", dest="clear_effort",
                 help="Clear the per-task reasoning effort — the worker falls back "
                      "to its profile's own agent.reasoning_effort."),
        ),
        _group(
            _arg("--brain", default=None, dest="brain", metavar="LANE",
                 help="Per-card harness brain for a foreign-lane worker (cc-worker): "
                      f"{kb.CARD_BRAIN_ALLOWED}. Card > lane > profile "
                      "foreign_lane.brain; applies on the next dispatch. Independent "
                      "of the model: with --brain alone the model override is left "
                      "untouched."),
            _arg("--clear-brain", action="store_true", dest="clear_brain",
                 help="Clear the per-card brain; the worker uses its profile's "
                      "foreign_lane.brain."),
        ),
    ], help="Set or clear a task's model/provider/effort override (takes effect on the next dispatch)",
       description=(
           "Pin a card's worker to a provider + model + effort. Any route is pinnable; a single "
           "Claude sub (claude-bpx-N/apx-N) additionally needs --pin-sub \"<reason>\". The pin "
           "applies on the next dispatch; no gateway restart. Visible in show/list as [PIN ...] "
           "and in the run's 'spawned' event pool. Docs: website/docs/user-guide/features/kanban.md, "
           "section 'Pinning a kanban card/worker: provider, model, effort'."
       )),
    _cmd("lane-model", children=("lane_action", _LANE_MODEL_SPECS),
         help="Time-boxed board-level model override applied at dispatch to cards without their own override"),
    _cmd("reclaim", [_TASK_ID, _RECLAIM_REASON], help="Release an active worker claim on a running task"),
    _cmd("reassign", [
        _TASK_ID,
        _arg("profile", help="New profile name (or 'none' to unassign)"),
        _arg("--reclaim", action="store_true",
             help="Release any active claim before reassigning (required if task is running)"),
        _RECLAIM_REASON,
        *_HANDBACK_FLAGS,
    ], help="Reassign a task to a different profile, optionally reclaiming first"),
    _cmd("diagnostics", [
        _arg("--severity", choices=["warning", "error", "critical"],
             help="Only show diagnostics at or above this severity"),
        _arg("--task", help="Only show diagnostics for one task id"),
        _json_flag(help="Emit JSON (structured) instead of the default human table"),
    ], aliases=["diag"], help="List active diagnostics on the current board"),
    _cmd("link", [
        _arg("parent_id"), _arg("child_id"),
        _arg("--kind", default=kb.DEFAULT_LINK_KIND, choices=sorted(kb.VALID_LINK_KINDS),
             help="Edge semantics. 'blocks' (default) holds the child until the parent is done. "
                  "'derived-from' records provenance only — the parent discovered/spawned the "
                  "child, which stays independently dispatchable. A DEPLOY must never be gated "
                  "on a DISCOVERY."),
    ], help="Add a parent->child dependency"),
    _cmd("unlink", [_arg("parent_id"), _arg("child_id")], help="Remove a parent->child dependency"),
    _cmd("claim", [
        _TASK_ID,
        _arg("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS, help="Claim TTL in seconds (default: 900)"),
        _arg("--review", action="store_true", help="Claim a parked review run (human-only boards)"),
    ], help="Atomically claim a ready task (prints resolved workspace path)"),
    _cmd("comment", [
        _TASK_ID,
        _arg("text", nargs="*", help="Comment body (or use --body-file)"),
        _arg("--body-file", metavar="PATH",
             help="Read the comment body from PATH ('-' = stdin). Use this for "
                  "text with backticks or $(...): the shell never sees it."),
        _arg("--author", help="Author name (default: $HERMES_PROFILE or 'user')"),
        _arg("--max-len", type=int, help="Trim the stored comment body to this many characters"),
    ], help="Append a comment"),
    _cmd("attach", [
        _TASK_ID,
        _arg("path", help="Path to the local file to attach"),
        _arg("--content-type", help="MIME type (default: guessed from the file extension)"),
        _arg("--name", help="Stored filename (default: the source file's basename)"),
        _arg("--author", help="uploaded_by label (default: $HERMES_PROFILE or 'user')"),
    ], help="Attach a local file to a task"),
    _cmd("attachments", [_TASK_ID, _json_flag()], help="List a task's attachments"),
    _cmd("attach-rm", [_arg("attachment_id", type=int)], help="Delete an attachment by id"),
    _cmd("complete", [
        _arg("task_ids", nargs="+", help="One or more task ids (only --result applies to all of them)"),
        _arg("--result", help="Result summary"),
        _arg("--summary",
             help="Structured handoff summary for downstream tasks. Falls back to --result if omitted."),
        _arg("--metadata",
             help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                  '"tests_run": 12}\'). Stored on the closing run.'),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: complete a running, claimed task "
                  "even without owning its run (closes the worker's run)."),
        _arg("--superseded-by", metavar="CARD|PR|SHA",
             help="Evidence pointer for a card whose premise was already satisfied elsewhere. "
                  "Closes it done with outcome 'superseded'; no --result/--summary required, "
                  "but the pointer must be non-empty (an unnamed supersede is a silent delete "
                  "of the work)."),
        _arg("--draft-ok", metavar="REASON",
             help="Audited per-card override for the DRAFT-PR refusal: the handoff names a "
                  "draft PR that is intentionally left open (e.g. a CI vehicle for an upstream "  # windows-footgun: ok (string literal, not a call)
                  "PR). The draft is not routed to review; a completion_draft_override event "
                  "records the PRs and REASON. An empty REASON is refused."),
        _arg("--survivor-ref", action="append", metavar="[REPO=]URL#SHA",
             help="Name an external survivor when the implementation lives on a remote, not in "
                  "the workspace. Verified with git ls-remote AND required to name this task: "
                  "the SHA must resolve to a single branch or tag tip whose ref name contains "
                  "the task id. An unverifiable claim, or one on an unrelated-looking ref, "
                  "refuses the completion (see --survivor-unbound). Repeatable: qualify each "
                  "claim as <workspace-relative-repo>=<claim> when more than one recorded "
                  "repository vanished."),
        _arg("--survivor-none", action="store_true",
             help="Record work deployed outside a repository; requires --reason naming an "
                  "existing follow-up card; never authorizes workspace deletion."),
        _reason("Why --survivor-none has no remote ref; include the follow-up card id."),
        _arg("--survivor-pr", action="append", metavar="[REPO=]OWNER/REPO#N",
             help="Name an external survivor by pull request. Verified with the GitHub REST "
                  "API (state OPEN or MERGED) AND required to name this task; an unverifiable "
                  "claim refuses the completion. Naming the task in the PR's head BRANCH binds "
                  "the claim. A match only in the PR title or body is a mention, not a tie to "
                  "this card's work, so it is recorded as an unbound claim (see "
                  "--survivor-unbound) and never becomes standing authority to delete the "
                  "workspace later. Repeatable; same <repo>= qualifier as --survivor-ref."),
        _arg("--survivor-unbound", action="append", nargs="?", const=True, metavar="REPO",
             help="Operator override: accept a --survivor-ref/--survivor-pr that is live but "
                  "does NOT name this task (including one that names it only in a PR title or "
                  "body, which is a mention), for the case where the work really did land on an "
                  "unrelated-looking branch. PER-CLAIM: pass it bare for a single-claim "
                  "completion, or repeat it with the <repo>= qualifier of each claim being "
                  "overridden when there is more than one -- overriding one claim must not "
                  "silently accept the others. The claim is still remote-verified; the override "
                  "and the OS user (resolved from the real uid, not $USER) are recorded on the "
                  "survivor and in the task event log. An unbound claim authorises THIS "
                  "completion only: a later reclamation will not reuse it."),
    ], help="Mark one or more tasks done"),
    _cmd("priority", [
        _TASK_ID,
        _arg("priority", type=int, help="New priority (integer, default 0)"),
    ], help="Set a task's dispatch priority (higher dispatches first among ready cards; ties go "
            "oldest-first). Records a priority_set event with actor + old/new. Same as edit --priority."),
    _cmd("edit", [
        _TASK_ID,
        _arg("--title", help="Replace the task title"),
        _arg("--body", help="Replace the task body"),
        _arg("--priority", type=int, metavar="N",
             help="Set the dispatch priority (higher first); records priority_set."),
        _arg("--result", help="Backfilled task result text for a done task"),
        *_STEP_HANDOFF,
        _arg("--model", dest="model_override", metavar="MODEL",
             help="Set the per-task model override (passed to the worker as `hermes -m MODEL`). "
                  "Omitting --model leaves the existing override untouched; use --clear-model to remove it."),
        _arg("--clear-model", action="store_true", dest="clear_model",
             help="Clear the per-task model override (revert to the assignee profile default). "
                  "Mutually exclusive with --model."),
        _arg("--session", metavar="SESSION_ID",
             help="(Re)stamp the card's home session ('none' = unstamped)."),
        _group(
            _arg("--no-worker", action="store_const", const=True, dest="no_worker", default=None,
                 help="Mark the card operator-only: the dispatcher never spawns it and "
                      "assign/reassign refuse worker profiles (even with --operator)"),
            _arg("--worker-ok", action="store_const", const=False, dest="no_worker",
                 help="Clear the no-worker flag"),
        ),
        # fork #1614: per-card skill list; fork #1612: needs-input pager opt-out.
        _arg("--skill", action="append", default=[], dest="skills", metavar="NAME",
             help="Append a skill to force-load into the worker (repeatable; "
                  "applies from the next spawn). Records skills_set."),
        _arg("--clear-skills", action="store_true", dest="clear_skills",
             help="Empty the card's skill list (applied before any --skill)."),
        _group(
            _arg("--no-page", action="store_const", const=False, dest="page", default=None,
                 help="Opt the card out of the needs-input pager (no origin-channel "
                      "page when it blocks on a human ruling)"),
            _arg("--page", action="store_const", const=True, dest="page",
                 help="Re-enable the needs-input pager for the card"),
        ),
    ], aliases=["update"],
       help="Edit task fields or recovery fields on an already-completed task, or (re)stamp its home session with --session"),
    _cmd("block", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason (also appended as a comment)"),
        _bulk_ids("block"),
        _arg("--kind", choices=sorted(kb.VALID_BLOCK_KINDS),
             help="Typed block reason. 'dependency' waits in todo (auto-promoted when "
                  "parents finish, no human); 'needs_input'/'capability' go to "
                  "blocked for a human; 'transient' marks a maybe-flaky failure. "
                  "Repeated same-kind re-blocks after unblock route the task to "
                  "triage to break unblock loops. Omit for a generic block."),
    ], help="Mark one or more tasks blocked"),
    _cmd("budget", [
        _arg("--board", dest="budget_board", help="Report a single board instead of every board."),
        _json_flag(help="Emit machine-readable JSON"),
    ], help="Report per-board 24h worker spend against kanban.budget.usd_per_24h"),
    _cmd("schedule", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason/timing note (also appended as a comment)"),
        _bulk_ids("schedule"),
        _group(
            _arg("--at", metavar="TS",
                 help="Timed wake: epoch seconds or ISO-8601 (naive = local time). The "
                      "dispatcher returns the card to ready on its first tick at/after TS. "
                      "Works on an already-scheduled card."),
            _arg("--now", action="store_true",
                 help="Timed wake at now: the dispatcher returns the card to ready on its next tick"),
        ),
    ], help="Park one or more tasks in Scheduled (waiting on time, not human input)"),
    _cmd("unblock", [
        _reason("Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons."),
        _TASK_IDS,
    ], help="Return blocked/scheduled tasks to ready, or todo while parents remain open"),
    _cmd("workspace", children=("workspace_action", _WORKSPACE_SPECS),
         help="Workspace maintenance (recover scratch cards stranded by mount loss)"),
    _cmd("requeue", [_TASK_ID, _arg("reason", nargs="+", help="Required operator reason")],
         help="Explicitly retry a READY card held by the respawn guard"),
    _cmd("reopen", [
        _TASK_ID,
        _arg("reason", nargs="+", help="Required audit-trail reason (recorded on the task_events row)"),
        _arg("--to", dest="to_status", choices=("ready", "todo", "review"), default="ready",
             help="Status to return the task to (default: ready; review = done-with-open-PR, "
                  "owned by kanban.review_assignee)"),
        _json_flag(help="Emit machine-readable JSON result"),
    ], help="Void a FALSE completion on a done task and requeue it "
            "(recovery path when a non-owner wrote a terminal result)"),
    _cmd("request-review", [
        _TASK_ID,
        _arg("--summary", help="What was implemented and how it was verified — shown to the reviewer."),
        _arg("--reviewer",
             help="Reviewer profile (or the explicit sentinel 'human' / 'human:<name>'); "
                  "reassigns the task before review dispatch. Defaults to config "
                  "kanban.review_assignee. A reviewer that is neither an installed profile "
                  "nor 'human' is refused."),
        _arg("--allow-same-actor", action="store_true",
             help="Permit the implementer to review their own work (normally refused). "
                  "Recorded on the review_requested event."),
        _arg("--metadata", help="JSON object with structured reviewer handoff facts."),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: move a running, claimed "
                  "task to review even without owning its run (clears the worker's claim)."),
    ], help="Move a task to 'review' (implementation done, awaiting review) — NOT a block"),
    _cmd("request-changes", [
        _TASK_ID,
        _arg("reason", nargs="+",
             help="Concrete changes required; first post a current-run review_coverage JSON comment"),
        _arg("--coverage",
             help="Review coverage JSON; records a run-attributed comment before transition (human CLI)"),
        # ``--operator "<who: why>"`` comes from the home-guard loop in build_parser;
        # on request-changes it is also the operator send-back (coverage waived).
    ], help="Reviewer verdict: return the active review run to its implementer"),
    _cmd("reopen-review", [
        _TASK_IDS,
        _reason("Optional reason/note — recorded as a comment before reopening. Quote multi-word reasons."),
    ], help="Retired: claim review and request-changes with a full coverage comment "
            "instead (operators: request-changes --operator \"<who: why>\")"),
    _cmd("promote", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Audit-trail reason (recorded on the task_events row)"),
        _bulk_ids("promote"),
        _arg("--dry-run", action="store_true", help="Validate the promotion without mutating state"),
        _json_flag(help="Emit machine-readable JSON result"),
    ], help="Manually move one or more todo/blocked tasks to ready (recovery path)"),
    _cmd("triage-resolve", [
        _TASK_ID,
        _arg("--to", required=True, choices=list(kb.TRIAGE_RESOLVE_TARGETS),
             help="Where the card goes. 'todo' re-queues it through normal parent "
                  "gating; 'done'/'archived' close it. 'ready' is deliberately not "
                  "offered — jumping the queue re-arms the unblock loop."),
        _arg("--reason", required=True,
             help="Why (required). Recorded on the triage_resolved event and as a "
                  "comment — this is the human-in-the-loop decision the escalation asked for."),
        _json_flag(help="Emit machine-readable JSON result"),
    ], help="Resolve a triage card with an explicit human decision "
            "(the supported exit from the unblock-loop escalation)"),
    _cmd("archive", [
        _arg("task_ids", nargs="*", help="Task ids to archive (default mode)"),
        _arg("--rm", dest="purge_ids", nargs="+",
             help="Permanently delete already-archived task ids from the board"),
    ], help="Archive one or more tasks"),
    _cmd("tail", [_TASK_ID, _arg("--interval", type=float, default=1.0)], help="Follow a task's event stream"),
    _cmd("dispatch", [
        _arg("--dry-run", action="store_true", help="Don't actually spawn processes; just print what would happen"),
        _arg("--max", type=int,
             help="Spawn at most N MORE workers this call (additive; not a running-count "
                  "ceiling). Intersects the host load gate (kanban.dispatch_load_gate), never raises it"),
        _arg("--max-running", type=int,
             help="Running-count ceiling for this board: spawn nothing once N are running "
                  "(default: kanban.max_spawn)"),
        _arg("--ignore-load-gate", action="store_true",
             help="Bypass kanban.dispatch_load_gate for this call. Prints load1/pause_above "
                  "and records a load_gate_override event on every card it spawns"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT,
             help=f"Auto-block a task after this many consecutive non-success attempts "
                  f"(spawn_failed, timed_out, or crashed; default: {kbd.DEFAULT_FAILURE_LIMIT})"),
        _json_flag(),
    ], help="One dispatcher pass: reclaim stale, promote ready, spawn workers"),
    _cmd("daemon", [
        _arg("--interval", type=float, default=60.0, help="Seconds between dispatch ticks (default: 60)"),
        _arg("--max", type=int, help="Cap number of spawns per tick"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT),
        _arg("--pidfile", help="Write the daemon's PID to this file on start"),
        _arg("--verbose", "-v", action="store_true", help="Log each tick's outcome to stdout"),
        # Escape hatch for hosts that truly cannot run the gateway; hidden from
        # --help so nobody casually keeps the double-dispatcher pattern alive.
        _arg("--force", action="store_true", help=argparse.SUPPRESS),
    ], help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`."),
    _cmd("watch", [
        _arg("--assignee", help="Only show events for tasks assigned to this profile"),
        _arg("--tenant", help="Only show events from tasks in this tenant"),
        _arg("--kinds",
             help="Comma-separated event kinds to include (e.g. 'completed,blocked,gave_up,crashed,timed_out')"),
        _arg("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)"),
    ], help="Live-stream task_events to the terminal (Ctrl+C to exit)"),
    _cmd("stats", [_json_flag()], help="Per-status + per-assignee counts + oldest-ready age"),
    _cmd("home-index", [
        _arg("--check", action="store_true", help="Report drift only; exit 1 when drift >= 1"),
        _json_flag(),
    ], help="Rebuild (default) or --check the cross-board home-card index from every board; "
            "prints drift (missing/extra/stale rows)"),
    _cmd("notify-subscribe", [
        _TASK_ID,
        *_NOTIFY_TARGET,
        _arg("--user-id"),
        _arg("--user-id-alt"),
        _arg("--chat-type", choices=("dm", "group", "channel", "thread"),
             help="Originating source chat_type, recorded so the active-wake delivery "
                  "modes resolve the operator's real session. Omit to leave an "
                  "existing sub unchanged (new subs default to 'dm')."),
        _arg("--parent-chat-id",
             help="Parent channel ID for a thread or forum post, used for multiplex profile routing."),
        _arg("--guild-id",
             help="Discord guild ID, used for multiplex profile routing."),
        _arg("--notifier-profile",
             help="Profile gateway that owns/delivers this subscription (default: active profile)"),
        # choices: single source of truth shared with the DB/watcher enum.
        _arg("--delivery-mode", choices=kbn._NOTIFY_DELIVERY_MODES,
             help="How the kanban-notifier reacts to terminal events for this "
                  "subscription: 'notify' (passive message only; default), "
                  "'notify+wake' (message AND wake the destination gateway agent so "
                  "it reads the full board context and replies in its own voice), or "
                  "'wake' (wake the agent only, no passive message). Omit to leave an "
                  "existing subscription's mode unchanged (new subs default to 'notify')."),
        _arg("--wake", action="store_true",
             help="Shorthand for --delivery-mode notify+wake. Wake is opt-in only: "
                  "each wake is a full agent turn that queues the human's messages."),
        _arg("--also", action="store_true",
             help="Add this chat even when another live chat already subscribes to "
                  "the card. Default: one subscriber chat per card and platform; a "
                  "live owner is kept and a dead (24 h idle) one is replaced."),
    ], help="Subscribe a gateway source to a task's terminal events (used by /kanban subscribe in the gateway adapter)"),
    _cmd("notify-list", [_arg("task_id", nargs="?"), _json_flag()],
         help="List notification subscriptions (optionally for a single task)"),
    _cmd("notify-unsubscribe", [_TASK_ID, *_NOTIFY_TARGET], help="Remove a gateway subscription from a task"),
    _cmd("notify-repair", [
        _arg("--dry-run", action="store_true", help="Report what would change without writing"),
        _arg("--all-boards", action="store_true",
             help="Sweep every board DB (default + boards/*/kanban.db), not just the active board"),
        _arg("--dedupe", action="store_true",
             help="Instead of the identity backfill: list cards with more than one "
                  "subscription per platform, keep the card's home-session chat "
                  "(else the oldest), drop the rest. Dry-run unless --apply."),
        _arg("--apply", action="store_true",
             help="With --dedupe: delete the extra subscriptions."),
        _json_flag(),
    ], help="Backfill missing key identity on legacy notify subscriptions", description=(
        "Repairs legacy notify-sub rows missing user_id, user_id_alt, or scope_id. Without the "
        "same identity fields as the creator, the wake injector can rebuild a SECOND session key "
        "for the same chat — one no human can send to, which therefore never receives the "
        "/reasoning or /model overrides the user set. The complete creator identity is read from "
        "the gateway routing index: a row is repaired only when exactly ONE routing identity "
        "exists for that chat, so the participant and workspace scope are unambiguous. A chat "
        "with no per-user session (cron / CLI / home-channel origins, which are legitimately "
        "user-less) is left alone — an identity is never invented. Idempotent and fill-a-hole "
        "only: existing identity fields are never re-pointed."
    )),
    _cmd("log", [_TASK_ID, _arg("--tail", type=int, help="Only print the last N bytes")],
         help="Print the worker log for a task (from <kanban-root>/kanban/logs/)"),
    _cmd("runs", [_TASK_ID, _json_flag(), *_run_state_args("filter runs by task_runs column")],
         help="Show attempt history for a task (one row per run: profile, outcome, elapsed, summary)"),
    _cmd("heartbeat", [
        _TASK_ID,
        _arg("--note", help="Optional short note attached to the heartbeat event"),
    ], help="Emit a heartbeat event for a running task (worker liveness signal)"),
    _cmd("assignees", [_json_flag()],
         help="List known profiles + per-profile task counts (union of ~/.hermes/profiles/ and current assignees on the board)"),
    _cmd("context", [_TASK_ID],
         help="Print the full context a worker sees for a task (title + body + parent results + comments)."),
    _cmd("specify", _triage_sweep_args("specify", "Specify", "specifier"),
         help="Flesh out a triage-column task into a concrete spec (title + "
              "body) and promote it to todo. Uses the auxiliary LLM "
              "configured under auxiliary.triage_specifier."),
    _cmd("decompose", _triage_sweep_args("decompose", "Decompose", "decomposer"),
         help="Decompose a triage-column task into a graph of child tasks "
              "routed to specialist profiles by description. Falls back "
              "to specify-style single-task promotion when the task "
              "doesn't benefit from fan-out. Uses auxiliary.kanban_decomposer."),
    _cmd("gc", [
        _arg("--event-retention-days", type=_nonnegative_int, default=30,
             help="Delete task_events older than N days for terminal tasks (default: 30; 0 disables)"),
        _arg("--log-retention-days", type=_nonnegative_int, default=30,
             help="Delete worker log files older than N days (default: 30; 0 disables)"),
        _arg("--done-retention-days", type=int, default=3,
             help="Also remove workspaces of DONE tasks finished at least N days ago; same "
                  "survivor/liveness/audit gates as archived (default: 3; negative disables)"),
        _arg("--dry-run", action="store_true",
             help="List the workspaces gc would try to remove; delete nothing"),
    ], help="Garbage-collect archived-task workspaces, old events, and old logs"),
    _cmd("clone", setup=_clone_setup,
         help="git clone that borrows objects from a shared fleet mirror",
         description=(
             "Clone a repo into a kanban workspace. For ANG-Ventures/* and Kyzcreig/* GitHub "
             "URLs the clone uses --reference-if-able against a bare mirror under "
             "<hermes root>/mirrors/, created lazily, so the checkout stores only objects the "
             "mirror lacks. A local-path source is cloned with --no-local (never hard-linked), "
             "borrowing from its origin's mirror when that is a fleet repo. Other URLs are "
             "cloned normally. Common git-clone options (-q, -b, --depth, --filter, "
             "--no-checkout, --single-branch, --no-tags, --origin, ...) are forwarded."
         )),
    _cmd("base-check", setup=_base_check_setup,
         help="fail if a worker branch sits on a stale/foreign base (run before push/handback)",
         description=(
             "Fetch the checkout's remote trunk and fail (exit 1) when the branch does not merge "
             "cleanly, carries commits the card did not author, changes files outside the card's "
             "declared scope, or is more than --max-behind commits behind. Prints the re-port "
             "remediation."
         )),
    _cmd("home-lint", [
        _arg("--backfill", action="store_true", help="Stamp each homeless open card 'unhomed' + comment"),
        _arg("--dry-run", action="store_true", help="With --backfill: list what would be stamped"),
        _json_flag(),
        _arg("--open-prs", action="store_true",
             help="List done cards (last 7 days) whose result names a still-OPEN GitHub PR (exit 1 when any)"),
    ], help="Report open cards with no home session (exit 1); --backfill stamps them 'unhomed'"),
    _cmd("repair", [
        _json_flag(help="Emit the repair report as JSON"),
        _arg("--reclassify-quota-crashes", action="store_true",
             help="Reclassify recorded quota crashes without counting task failures"),
        _arg("--dry-run", action="store_true",
             help="Report quota repairs without writes or schema migration"),
        _arg("--board", default=argparse.SUPPRESS,
             help="Board to repair (also accepted before the repair verb)"),
    ], help="Check kanban.db integrity and auto-repair index-only corruption",
       description=(
           "Runs PRAGMA integrity_check on the board's DB and reports the result. When the "
           "failure consists only of index-scoped errors ('wrong # of entries in index <name>' / "
           "'row N missing from index <name>'), the corrupt file is quarantined to a "
           ".corrupt.<hash>.bak sibling first and the damaged indexes are rebuilt with REINDEX — "
           "the same narrow auto-repair the connect-time guard applies. Any other corruption "
           "class is reported and left untouched (fail-closed). Exits 0 when the DB is healthy "
           "or was repaired, non-zero when it is still corrupt."
       )),
]


def _add_home_guard_flags(sub: argparse._SubParsersAction) -> None:
    """``--takeover`` / ``--operator`` (+ ``--keep-home``/``--transfer-home`` on the
    re-homing verbs) on every subcommand the home-session guard polices."""
    from hermes_cli.kanban import _HOME_GUARDED_ACTIONS

    for name in _HOME_GUARDED_ACTIONS:
        p = sub.choices.get(name)
        if p is None or "foreign_ok" in {a.dest for a in p._actions}:
            continue
        p.add_argument(
            "--takeover", "--foreign-ok", dest="foreign_ok", metavar="REASON",
            help="Act on a card whose home session is another session (or an unhomed card); "
                 "records a takeover event and posts REASON as a comment the home session "
                 "sees. On assign/unblock/promote/triage-resolve it also RE-HOMES the card to "
                 "your session (an OWNERSHIP TRANSFER: children and pings follow; the event "
                 "keeps previous_session + previous_home); reclaim KEEPS the home unless "
                 "--transfer-home. Never on complete, and never for cron/sweep actors. "
                 "Re-home without a status change: hermes kanban edit <id> --session <sid> --takeover R.",
        )
        if name in kb.REHOME_ON_TAKEOVER_ACTIONS:
            home = p.add_mutually_exclusive_group()
            home.add_argument(
                "--keep-home", dest="takeover_home", action="store_const", const="keep", default=None,
                help="With --takeover: act on the card but leave its home session and home "
                     "chat as they are (the default for reclaim).",
            )
            home.add_argument(
                "--transfer-home", dest="takeover_home", action="store_const", const="transfer",
                help="With --takeover: move the card's home to your session and chat -- an "
                     "ownership transfer (the default for assign/unblock/promote/triage-resolve).",
            )
        p.add_argument(
            "--operator", dest="operator", metavar="WHO: WHY",
            help="Operator profiles (apollo/default, aegis) applying a relayed human decision "
                 "to a foreign card; records an operator_override event (status/timing verbs also post "
                 "a FOREIGN CHANGE comment; cite the ruling as 'msg <discord id>').",
        )


def _intermix_optional_positionals(parser: argparse.ArgumentParser) -> None:
    """Let options appear before trailing ``*``/``?`` positionals.

    Stock argparse binds an optional positional (``nargs="*"``/``"?"``) to
    ``[]``/default the moment it meets an option, so
    ``comment <id> --author X "text"`` failed with ``unrecognized arguments:
    text`` once ``text`` became ``nargs="*"`` (#1166). Every leaf parser in
    the kanban tree that owns such a positional is switched to
    ``parse_known_intermixed_args``; parsers with sub-commands are walked,
    not converted (intermixed parsing cannot host subparsers).
    """
    subparser_actions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if subparser_actions:
        seen: set[int] = set()
        for action in subparser_actions:
            for child in action.choices.values():
                if id(child) not in seen:
                    seen.add(id(child))
                    _intermix_optional_positionals(child)
        return
    if not any(
        not a.option_strings and a.nargs in ("*", "?") for a in parser._actions
    ):
        return
    base = type(parser)
    if getattr(base, "_kanban_intermixed", False):
        return

    class _Intermixed(base):  # type: ignore[misc, valid-type]
        _kanban_intermixed = True

        def parse_known_args(self, args=None, namespace=None):
            # parse_known_intermixed_args re-enters parse_known_args on
            # py<3.12; the guard makes those inner passes the stock parser.
            if getattr(self, "_kanban_in_intermixed", False):
                return super().parse_known_args(args, namespace)
            self._kanban_in_intermixed = True
            try:
                return self.parse_known_intermixed_args(args, namespace)
            finally:
                self._kanban_in_intermixed = False

    parser.__class__ = _Intermixed


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree; returns the ``kanban`` parser."""
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description="Durable SQLite-backed task board shared across Hermes profiles. "
                    "Tasks are claimed atomically, can depend on other tasks, and "
                    "are executed by a named profile in an isolated workspace. "
                    "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban.",
    )
    # --board scopes every subcommand to one board's DB; when omitted the
    # resolution is HERMES_KANBAN_BOARD, then the persisted current-board
    # file, then "default" (kanban_db.get_current_board()).
    kanban_parser.add_argument("--board", default=None, metavar="<slug>",
                               help="Board slug to operate on. Defaults to the current board (set "
                                    "via `hermes kanban boards switch <slug>` or the "
                                    "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards "
                                    "list` to see all boards.")
    sub = kanban_parser.add_subparsers(dest="kanban_action")
    _add_commands(sub, _SPECS)
    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    _add_home_guard_flags(sub)
    _intermix_optional_positionals(kanban_parser)
    return kanban_parser
