"""``hermes kanban …`` — dispatch (``kanban_command``), task-verb handlers, ``run_slash`` for ``/kanban``.
DB work lives in ``kanban_db``; siblings: ``kanban_parser`` (argparse, re-exported ``build_parser``),
``kanban_output`` (text/--json), ``kanban_boards`` (``boards …``), ``kanban_ops`` (dispatch/daemon/
tail/watch/gc/repair).
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
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_swarm as ks
from hermes_cli.kanban_output import (
    _ATTACHMENT_FIELDS, _RUNS_RUN_FIELDS, _SHOW_RUN_FIELDS, _STATUS_ICONS, _TASK_DICT_FIELDS,
    _bulk_apply, _err, _fmt_counts, _fmt_ts, _json_out, _obj_dict, _print_json,
)
# ``_fmt_task_line`` / ``_task_to_dict`` are defined below, not imported: the fork
# versions carry the workspace-refusal flag, pin badge and home/pin/no_worker
# fields the handlers here render (FOLLOWUP: port into kanban_output, then import).
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_ops import (
    _cmd_daemon, _kanban_config, _cmd_dispatch, _cmd_gc, _cmd_repair, _cmd_tail, _cmd_watch,
)
from hermes_cli.kanban_parser import build_parser  # noqa: F401  (re-exported: hermes_cli.main, run_slash)
from hermes_cli.kanban_pr_freshness import DraftPrError
from hermes_cli.kanban_branch_base import StaleBaseError
from hermes_cli.kanban_open_pr import ClosedUnmergedPrError
from hermes_cli.kanban_receipt import EXIT_NO_RECEIPT, ReceiptRequiredError
from hermes_cli.kanban_identity import safe_comment_provenance
from hermes_cli.kanban_held_repo import fmt_held_repo, held_repo
from hermes_constants import get_default_hermes_root


# --- Flag parsing helpers ---

def _none_profile(value: str) -> Optional[str]:
    """``none`` / ``-`` / ``null`` mean "unassign"."""
    return None if value.lower() in {"none", "-", "null"} else value


def _parse_metadata_flag(raw: Optional[str]) -> tuple[Optional[dict], int]:
    """Parse ``--metadata`` JSON; returns ``(dict|None, rc)`` with rc=2 on error."""
    if not raw:
        return None, 0
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        return None, _err(f"kanban: --metadata: {exc}", 2)
    return metadata, 0


def _fmt_respawn_guard_detail(detail: Optional[dict]) -> str:
    """`` — "<error>" recorded <ts> (Ns ago), eligible <ts>`` for a deferral
    the respawn guard derived from a stamped failure; empty otherwise."""
    if not detail:
        return ""
    now = int(time.time())
    parts = []
    pr = detail.get("pr")
    if pr:
        states = "/".join(
            str(detail[k]) for k in ("pr_state", "merge_state") if detail.get(k)
        )
        parts.append(f"{pr} ({states})" if states else str(pr))
    if detail.get("hold"):
        parts.append(str(detail["hold"]))
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


# ``review_requested``: a ready card handed straight to review leaves the
# ready-lane guard behind; its old hold is history, not the review hold.
_GUARD_DISPLAY_RESET_KINDS = frozenset(
    {"claimed", "spawned", "review_requested", *kb._RESPAWN_GUARD_FAILURE_RESET_KINDS}
)
# Guard reasons the review lane never records (it skips ``active_pr``), so a
# review card showing one would be displaying a stale ready-lane hold.
_READY_ONLY_GUARD_REASONS = frozenset({"active_pr"})


def _fmt_current_respawn_guard(status: str, events) -> str:
    """``<reason> — <pr> (<state>)`` for the respawn guard holding a queued card.

    Only the newest ``respawn_guarded`` event counts, and only when no
    claim/spawn or guard-resetting event (operator requeue, status change,
    reassign, ...) came after it: the guard let it go, or its answer may have
    changed and the next dispatch tick re-records it if it still holds.
    Empty for any other status or when nothing is holding the card.
    """
    if status not in ("ready", "review"):
        return ""
    held = None
    for ev in events:
        if ev.kind == "respawn_guarded":
            held = ev
        elif ev.kind in _GUARD_DISPLAY_RESET_KINDS:
            held = None
    if held is None:
        return ""
    payload = held.payload if isinstance(held.payload, dict) else {}
    if status == "review" and payload.get("reason") in _READY_ONLY_GUARD_REASONS:
        return ""
    line = str(payload.get("reason") or "?")
    pr = payload.get("pr")
    if pr:
        line += f" — {pr}"
        if payload.get("pr_state"):
            line += f" ({payload['pr_state']})"
        if payload.get("hold"):
            line += f": {payload['hold']}"
    else:
        line += _fmt_respawn_guard_detail(
            {k: payload.get(k) for k in ("error", "recorded_at", "eligible_at")
             if payload.get(k)}
        )
    return f"{line}  [as of {_fmt_ts(held.created_at)}]"


# Statuses on which an open workspace-refusal episode is still live news.
_REFUSAL_VISIBLE_STATUSES = frozenset({"todo", "ready", "review"})


def _fmt_refusal(state: Optional[dict]) -> str:
    """``WORKSPACE REFUSED (<reason>) since <ts>`` or ``""``."""
    if not state:
        return ""
    return (
        f"WORKSPACE REFUSED ({state.get('reason')}) since "
        f"{_fmt_ts(state.get('since'))}"
    )


def _fmt_task_line(t: kb.Task, refusal: Optional[dict] = None) -> str:
    icon = _STATUS_ICONS.get(t.status, "?")
    assignee = t.assignee or "(unassigned)"
    tenant = f" [{t.tenant}]" if t.tenant else ""
    flag = ""
    if refusal and t.status in _REFUSAL_VISIBLE_STATUSES:
        flag = f"  [{_fmt_refusal(refusal)}]"
    pin = _pin_badge(t)
    pin = f"  {pin}" if pin else ""
    return f"{icon} {t.id}  {t.status:8s}  {assignee:20s}{tenant}  {t.title}{pin}{flag}"


def _pin_badge(t) -> str:
    """``[PIN claude-bpx-N: <reason>]`` for a card's deliberate sub pin."""
    from hermes_cli.model_policy import format_pin_badge

    return format_pin_badge(
        getattr(t, "provider_override", None), getattr(t, "pin_sub_reason", None),
    )


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
    d = _obj_dict(t, _TASK_DICT_FIELDS)
    d["skills"] = list(t.skills) if t.skills else []
    d["reasoning_effort"] = t.reasoning_effort
    d["brain"] = getattr(t, "brain", None)
    d["pin_sub_reason"] = getattr(t, "pin_sub_reason", None)
    d["pin_sub_fallback"] = bool(getattr(t, "pin_sub_fallback", False))
    d["unhomed"] = bool(getattr(t, "unhomed", False))
    d["no_worker"] = bool(getattr(t, "no_worker", False))
    return d


def _run_state_kwargs(args: argparse.Namespace, cmd: str) -> tuple[Optional[dict[str, str]], int]:
    """``--state-type``/``--state-name`` must be given together: ``(kwargs, 0)`` or ``(None, 2)``."""
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None, _err(f"kanban {cmd}: pass both --state-type and --state-name, or omit both", 2)
    return ({} if st is None else {"state_type": st, "state_name": sn}), 0


def _parse_workspace_flag(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``--workspace`` -> ``(kind, path|None)``: ``scratch``, ``worktree``, ``worktree:<p>``, ``dir:<p>``.
    Omitted -> ``(None, None)`` so ``create_task`` can tell "default" from an explicit scratch."""
    if not value:
        return (None, None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(f"--workspace {prefix} requires a path after the colon")
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(f"unknown --workspace value {value!r}: use scratch, worktree, "
                                     "worktree:<path>, or dir:<path>")


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


def _check_dispatcher_presence(hermes_home: Optional[Path] = None) -> tuple[bool, str]:
    """``(running, message)`` for the "will anything dispatch this?" warning: True when a gateway is
    alive for this HERMES_HOME with ``kanban.dispatch_in_gateway`` on, else False + human guidance.
    Fails OPEN (probe/config errors -> ``(True, "")``) — a missed warning beats crying wolf.
    ``hermes_home`` scopes the probe to a profile dir (dashboard backend); CLI callers pass None.

    The dashboard plugin API passes it because the dashboard backend process can be running under a
    different HERMES_HOME than the profile the request targets, which otherwise produced a "no gateway is
    running" warning against a perfectly healthy profile gateway (#71211). CLI callers leave it ``None`` and
    keep the existing process-level behavior.

    NOTE (fork parity, 2026-08-07 parity merge): the upstream
    ``resolve_gateway_liveness`` ladder now lands in this fork's
    ``gateway.status`` via this merge, so the liveness-ladder enrichment
    that commit 11cffc4d5 prematurely carried (and #475 reverted to the
    fork ``get_running_pid`` probe to unbreak the import) is re-adopted here
    against the now-present symbol.
    """
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore

        # Same ladder as the dashboard status endpoints so PID-file-less / cross-container gateways
        # aren't misreported; use_cache=False because this one-shot probe must see the state now.
        liveness = resolve_gateway_liveness(profile_dir=hermes_home, use_cache=False)
    except Exception:
        return (True, "")  # can't probe — silent
    if liveness.probe_error:  # resolver swallows per-rung failures; "can't tell" != "no gateway"
        return (True, "")
    pid = liveness.pid
    # Even if the gateway is up, dispatch_in_gateway may be off (can't tell -> assume default).
    if pid and bool(_kanban_config().get("dispatch_in_gateway", True)):
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid:
        return (False, "Gateway is running but kanban.dispatch_in_gateway=false in "
                "config.yaml — the task will sit in 'ready' until you flip it "
                "back on and restart the gateway, OR run the legacy "
                "standalone daemon (`hermes kanban daemon --force`).")
    return (False, "No gateway is running — the task will sit in 'ready' until you "
            "start it. Run:\n    hermes gateway start\n"
            "The gateway hosts an embedded dispatcher (tick interval 60s by "
            "default); your task will be picked up on the next tick after "
            "the gateway comes up.")


# --- Command dispatch ---

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …``; returns a shell-style exit code."""
    action = getattr(args, "kanban_action", None)
    if not action:
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes kanban <action> [options]\n"
                  "Run 'hermes kanban --help' for the full list of actions.", file=sys.stderr)
        return 0

    # Fast-fail for UX only; the durable trust boundary is in kanban_db, since children can
    # import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        refusal = "delegate_task child contexts cannot mutate Kanban tasks via the CLI"
        _record_refused_block(args, refusal)
        return _err(f"kanban: {refusal}")

    refusal = _non_owner_lifecycle_refusal(args)
    if refusal:
        _record_refused_block(args, refusal)
        return _err(f"kanban: {refusal}")

    # `boards …` manages board metadata and the current-board pointer itself, so it must ignore
    # the `--board` routing override (else `--board beta boards show` reports beta).
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` pins HERMES_KANBAN_BOARD for the duration of this call so it inherits the
    # exact resolution the dispatcher uses for workers.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            return _err(f"kanban: {exc}", 2)
        if not normed:
            return _err("kanban: --board requires a slug", 2)
        # Boards other than 'default' must already exist — typoed slugs would otherwise silently
        # create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            return _err(f"kanban: board {normed!r} does not exist. "
                        f"Create it with `hermes kanban boards create {normed}`.")
        board_scope = kb.scoped_current_board(normed)

    with board_scope:
        # `repair` dispatches BEFORE auto-init: on a corrupt DB init_db() itself raises
        # KanbanDbCorruptError, which would turn every repair into "could not initialize database".
        if action == "repair":
            return _cmd_repair(args)
        # `clone` is a git operation with no board state; it must not depend
        # on (or initialize) kanban.db.
        if action == "clone":
            from hermes_cli.kanban_clone import clone

            return clone(args.url, args.dest, args.git_opts)
        if action == "base-check":
            from hermes_cli.kanban_branch_base import run as _base_check

            return _base_check(args)
        # init_db is idempotent (one sqlite_master SELECT when tables exist) and prevents
        # "no such table: tasks" on first use from a fresh HERMES_HOME.
        try:
            kb.init_db()
        except Exception as exc:
            return _err(f"kanban: could not initialize database: {exc}")

        handler = _HANDLERS.get(action)
        if not handler:
            return _err(f"kanban: unknown action {action!r}", 2)
        gated_flags = [
            flag for flag, dest in (("--takeover", "foreign_ok"), ("--operator", "operator"))
            if getattr(args, dest, None)
        ]
        if gated_flags and (
            action in kb.OPERATOR_FLAG_GATED_ACTIONS
            or action in kb.OPERATOR_ONLY_GATED_ACTIONS
        ):
            try:
                with kbc.connect_closing() as gate_conn:
                    kb.enforce_operator_flag_gate(
                        gate_conn, _lifecycle_target_ids(args), action,
                        flags=gated_flags,
                    )
            except kb.OperatorTokenRequiredError as exc:
                return _err(f"kanban: {exc}")
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
                operator=getattr(args, "operator", None),
                home=getattr(args, "takeover_home", None),
            )
        try:
            # The preflight above ran on its own connection; the scope re-runs
            # the same gate inside every write transaction the handler opens,
            # so a run claimed in between is never closed by a tokenless
            # override (t_920c6b4a).
            with kb.operator_flag_gate_scope(
                _lifecycle_target_ids(args), action, flags=gated_flags,
            ), actor_scope:
                return int(handler(args) or 0)
        except (ValueError, RuntimeError, PermissionError) as exc:
            # A survivor refusal carries its operator-only hint on the
            # exception, not in the persisted message, so render it HERE --
            # at the boundary whose environment belongs to the caller actually
            # reading the text. See kanban_survivor.render_override_hint.
            from hermes_cli.kanban_survivor import render_override_hint

            return _err(f"kanban: {render_override_hint(exc)}")


# --- Handlers ---

# Status/ownership subcommands policed by the home-session guard
# (kanban_db.check_home_session). Runtime actions (dispatch, daemon, gc,
# heartbeat) are deliberately absent: they are the execution lane. ``claim``
# is guarded: the verb is chat-reachable and a foreign claim would hold the
# home card's lease (the assignee / its dispatched worker stay exempt).
_HOME_GUARDED_ACTIONS: frozenset[str] = frozenset({
    "claim", "complete", "block", "unblock", "archive", "assign", "reassign",
    "reclaim", "set-model", "priority", "edit", "update", "promote", "triage-resolve",
    "schedule", "requeue", "reopen", "reopen-review", "request-review",
    "request-changes", "link", "unlink", "specify", "decompose", "workspace",
    "rehome",
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
    # ``_HERMES_GATEWAY=1`` is inherited by every descendant of the gateway,
    # including the terminal subprocess a chat turn runs ``hermes kanban``
    # in. Only the gateway PROCESS itself (which set the marker when
    # ``gateway.run`` was imported) has concurrent sessions sharing one
    # os.environ. A subprocess gets its own session id bridged per command
    # (tools/environments/local._inject_session_context_env, contextvar-
    # authoritative), so the env value there IS the caller's. Treating the
    # subprocess as in-gateway made every chat-turn ``claim --review`` bind
    # no session, and the following ``request-changes`` was refused
    # (t_0485b3ff: t_ddcd2170, t_c26be9b9, t_6500a97a stranded in running).
    # Importing ``gateway.run`` is not ownership either: it sets the marker at
    # import time and CLI tools import it lazily (FleetReview 09c07e5eb0a9).
    in_gateway = kb._process_is_gateway()
    try:
        from gateway.session_context import _SESSION_ID, resolve_current_session_id

        if in_gateway:
            # In-process gateway: ONLY a per-turn contextvar bound in this
            # context is ours. A plain slash command runs before that bind,
            # and the resolver's os.environ fallback is process-global --
            # another chat's session. Sessionless means None, never a
            # borrowed identity (FleetReview #951).
            bound = _SESSION_ID.get()
            return (bound.strip() or None) if isinstance(bound, str) else None
        resolved = (resolve_current_session_id() or "").strip()
        if resolved:
            return resolved
    except Exception:
        if in_gateway:
            return None
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
    if kb.is_operator_home(session_id):
        return f"operator ({session_id}; any operator-profile session may act)"
    caller = _caller_session_id()
    if caller and session_id in kb.home_ids(caller):
        return "this-session"
    return f"other ({session_id})"


def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    from hermes_cli.profiles import current_profile_name
    return current_profile_name("user") or "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init", "create", "swarm", "assign", "reclaim", "reassign", "link", "unlink",
    # "comment" is deliberately absent: a child may append a comment (t_70fcc2c3);
    # kanban_db.add_comment marks the author "(subagent)".
    "claim", "attach", "attach-rm", "complete", "edit", "update", "block",
    "schedule", "unblock", "requeue", "workspace", "reopen", "promote", "triage-resolve",
    "archive", "dispatch", "daemon", "repair",
    "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen-review",
    "gc",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "import",
})


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        if (getattr(args, "boards_action", None) or "list") not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    from agent.delegation_context import kanban_path_is_fenced

    return kanban_path_is_fenced(kb.kanban_home()) or kanban_path_is_fenced(kb.kanban_db_path())


#: Terminal/lifecycle writes a worker makes on its OWN card. Each one passes
#: ``expected_run_id=_worker_run_id_for(tid)``, which is ``None`` for a process
#: that does not own the grant, and ``None`` means "operator, no run guard".
_WORKER_LIFECYCLE_ACTIONS: frozenset[str] = frozenset({
    "complete",
    "block",
    "schedule",
    "request-review",
})


def _lifecycle_target_ids(args: argparse.Namespace) -> list[str]:
    ids = list(getattr(args, "task_ids", None) or [])
    if getattr(args, "task_id", None):
        ids.append(args.task_id)
    ids.extend(getattr(args, "ids", None) or [])
    return ids


def _record_refused_block(args: argparse.Namespace, refusal: str) -> None:
    """Comment a refused ``block`` on each target card so the refusal is visible on the board.

    Without it the card stays ``running`` with an idle worker and nothing says
    why (t_0809e21a). A comment is the one write a delegate child may make
    (``kanban_db.add_comment`` marks it ``(subagent)``); failures only warn.
    """
    if getattr(args, "kanban_action", None) != "block":
        return
    body = f"kanban block REFUSED, the block did not land and the card status is unchanged: {refusal}"
    for tid in _lifecycle_target_ids(args):
        try:
            run_id, session_ref = safe_comment_provenance(tid)
            with kbc.connect_closing() as conn:
                kb.add_comment(conn, tid, _profile_author(), body,
                               run_id=run_id, session_ref=session_ref)
        except Exception as exc:
            print(f"kanban: could not record the refused block on {tid}: {exc}", file=sys.stderr)


def _non_owner_lifecycle_refusal(args: argparse.Namespace) -> Optional[str]:
    """Refuse a lifecycle write on the ambient worker card by a non-owner.

    A process that inherited a worker's ``HERMES_KANBAN_TASK`` but does not hold
    the owner grant (``HERMES_KANBAN_OWNER_PID`` names another pid) gets
    ``expected_run_id=None`` from :func:`_worker_run_id_for`, which
    ``complete_task``/``block_task`` read as an operator override with no run
    guard. Without this gate such a process closes the live worker's card
    (2026-08-12, t_09b90233: a nested process closed its parent's card).

    Scope is the card the inherited env names. A shell with no worker env is an
    operator and is unaffected; so is the owning worker, and a hand-driven
    worker with no owner marker (``owns_kanban_worker_authority`` fails open).
    """
    if getattr(args, "kanban_action", None) not in _WORKER_LIFECYCLE_ACTIONS:
        return None
    ambient = os.environ.get("HERMES_KANBAN_TASK")
    if not ambient or ambient not in _lifecycle_target_ids(args):
        return None
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if is_dispatcher_owned_worker_context():
            return None
    except Exception:
        return None
    return (
        f"this process inherited worker env for {ambient} but does not hold "
        f"its owner grant; only the dispatcher's worker may "
        f"{args.kanban_action} that card (run it from a shell without the "
        f"worker env to act as an operator)"
    )


def _joined_words(words) -> Optional[str]:
    """Free-text positional ``nargs="*"`` words -> stripped string, or None when absent."""
    return " ".join(words).strip() if words else None


def _stripped_or_none(value: Optional[str]) -> Optional[str]:
    """``None`` stays ``None``; otherwise strip, and treat the empty string as ``None``."""
    return None if value is None else (value.strip() or None)


def _ok_or_err(ok, fail: str, done: str) -> int:
    """Single-mutation handlers: print ``done`` (rc 0) or ``fail`` to stderr (rc 1)."""
    if not ok:
        return _err(fail)
    print(done)
    return 0


def _bulk_ids(args: argparse.Namespace) -> list[str]:
    """Positional ``task_id`` plus ``--ids`` extras (bulk verbs)."""
    return [args.task_id] + list(getattr(args, "ids", None) or [])


def _require_ids(args: argparse.Namespace) -> tuple[list[str], int]:
    """``args.task_ids`` -> ``(ids, 0)`` or ``([], 1)`` after printing the standard error."""
    ids = list(args.task_ids or [])
    if not ids:
        return ids, _err("at least one task_id is required")
    return ids, 0


def _parse_duration(val) -> Optional[int]:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds; None for empty input;
    ValueError on malformed input."""
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    try:
        return int(s)  # bare integer → seconds
    except ValueError:
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (s and s[-1] in units):
        raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"malformed duration {val!r}") from exc
    return int(n * units[s[-1]])


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")
    print()
    # Profiles on disk == assignees already addressable.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.\n"
              "Create one with `hermes -p <name> setup` before assigning tasks.")
    print(
        "\nNext step: start the gateway so ready tasks actually get picked up.\n"
        "  hermes gateway start\n\n"
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbd.heartbeat_worker(conn, args.task_id, note=getattr(args, "note", None),
                                 expected_run_id=_worker_run_id_for(args.task_id))
    return _ok_or_err(ok, f"cannot heartbeat {args.task_id} (not running?)",
                      f"Heartbeat recorded for {args.task_id}")


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if _json_out(args, data):
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        print(f"{entry['name']:20s}  {on_disk:8s}  {_fmt_counts(entry['counts'] or {}, '(idle)')}")
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


def _read_body_file(path: str) -> str:
    """Read a card/comment body from ``path`` (``-`` = stdin).

    Shell callers pass markdown through ``--body-file`` / a quoted heredoc
    instead of a double-quoted argv string, where backticks and ``$(...)``
    are executed by the shell as command substitution (t_f7e11e44).
    """
    if path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def _cmd_create(args: argparse.Namespace) -> int:
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli import kanban_worker_policy as _kwp

    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        return _err(f"kanban: {exc}", 2)
    if branch_name and ws_kind != "worktree":
        return _err("kanban: --branch is only valid with --workspace worktree", 2)
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban: --max-runtime: {exc}", file=sys.stderr)
        return 2
    body_file = getattr(args, "body_file", None)
    if body_file is not None:
        if args.body is not None:
            print("kanban: --body and --body-file are mutually exclusive", file=sys.stderr)
            return 2
        try:
            args.body = _read_body_file(body_file)
        except OSError as exc:
            print(f"kanban: --body-file: {exc}", file=sys.stderr)
            return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban: --max-retries must be >= 1 (got {max_retries}); "
            "use 1 to trip on the first failure.",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "unhomed", False):
        if getattr(args, "session", None) is not None or (getattr(args, "home", None) or "").strip():
            print("kanban: --unhomed is exclusive with --session/--home", file=sys.stderr)
            return 2
        args.session = "none"
    home_flag = (getattr(args, "home", None) or "").strip()
    if home_flag:
        if getattr(args, "session", None) is not None:
            print("kanban: --home and --session are mutually exclusive", file=sys.stderr)
            return 2
        if home_flag == "operator":
            home_flag = kb.OPERATOR_HOME_SESSION
        if not kb.is_operator_home(home_flag):
            print(
                f"kanban: --home: expected 'operator' or 'operator:<name>', got {home_flag!r}",
                file=sys.stderr,
            )
            return 2
        args.session = home_flag
    try:
        with kb.connect_closing() as conn:
            _event_mark = kb.max_event_id(conn)
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
                pin_sub_reason=getattr(args, "pin_sub", None),
                pin_sub_fallback=bool(getattr(args, "pin_sub_fallback", False)),
                flagship_override_reason=getattr(args, "allow_flagship", None),
                flagship_override_author=args.created_by or _profile_author(),
                reasoning_effort=getattr(args, "reasoning_effort", None),
                brain=getattr(args, "brain", None),
                goal_mode=bool(getattr(args, "goal_mode", False)),
                goal_max_turns=getattr(args, "goal_max_turns", None),
                completion_contract=getattr(args, "completion_contract", None),
                initial_status=getattr(args, "initial_status", "running"),
                creator_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                                 if is_dispatcher_owned_worker_context() else None),
                forced_status=_kwp.resolve_park_status(
                    initial_status=getattr(args, "initial_status", "running"),
                    triage=bool(getattr(args, "triage", False)),
                ),
                session_id=_resolve_session_flag(getattr(args, "session", None)),
                session_explicit=getattr(args, "session", None) is not None,
                require_home=True,
                duplicate_guard=True,
                force_reason=getattr(args, "force_reason", None),
            )
            task = kb.get_task(conn, task_id)
            # Notice only when THIS call created the card and auto-added the
            # skill: an idempotent hit returns an older card whose created
            # event predates the watermark taken before the call.
            auto_added = bool(kb.created_skills_auto_added(
                conn, task_id, after_event_id=_event_mark))
            dup_warning = kb.near_duplicate_warning(conn, task_id)
            auto_subscribed = _maybe_cli_auto_subscribe(conn, task_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if (task.unhomed and getattr(args, "session", None) is None
            and not getattr(args, "json", False)):
        # D-O2 (t_6281f908): a hand-typed create with no session is allowed
        # but says so; the orphan watch will try to infer its home. Not with
        # --json: ``run_slash`` merges stderr into its output, and the JSON
        # carries ``"unhomed": true`` for a machine reader anyway.
        print(
            f"\n⚠  {task_id} has no home session (no session identity, no "
            "homed --parent): its lifecycle lines fall back to #logs until it "
            f"is re-homed (`hermes kanban rehome {task_id} --session <sid>`).",
            file=sys.stderr,
        )
    if getattr(args, "json", False):
        _print_json(_task_to_dict(task))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        if auto_added:
            print(
                f"Added skill {kb.MILESTONE_QA_SKILL} ([milestone] QA cards run "
                "Argus on the sdlc-review procedure)."
            )
        if auto_subscribed:
            print(
                "Subscribed the calling session for finish notifications "
                "(kanban.cli_auto_subscribe)."
            )
        if dup_warning:
            similar = ", ".join(
                f"{d['id']} ({d['score']:.2f})" for d in dup_warning.get("duplicates", [])
            )
            print(f"\n⚠  similar card(s) created in the last 24h: {similar}", file=sys.stderr)

        # Warn only for ready+assigned tasks that would sit without a dispatcher (triage/todo idle
        # by design, unassigned can't dispatch); skipped under --json so stdout stays parseable.
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
        return _err(f"kanban swarm: {exc}", 2)
    if not workers:
        return _err("kanban swarm: at least one --worker is required", 2)
    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn, goal=args.goal, workers=workers, verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer, tenant=args.tenant,
            created_by=args.created_by or _profile_author(), priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _print_json(created.as_dict())
    else:
        print(f"Swarm root: {created.root_id}\n"
              "Workers: " + ", ".join(created.worker_ids) + "\n"
              f"Verifier: {created.verifier_id}\n"
              f"Synthesizer: {created.synthesizer_id}")
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
        # A delegate_task child's connection is read-only; promotion is a
        # status mutation, so it lists the board as-is (the dispatcher promotes).
        if not kb._is_delegated_child():
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
        try:
            refusals = kb.workspace_refusal_state(
                conn, [t.id for t in tasks if t.status in _REFUSAL_VISIBLE_STATUSES],
            )
        except Exception:
            refusals = {}
    if _json_out(args, [_task_to_dict(t) for t in tasks]):
        return 0
    # Passive discoverability: only multi-board users see which board this is.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        other_count = len(all_boards) - 1
        print(f"Board: {kb.get_current_board()} ({other_count} other board{'s' if other_count != 1 else ''} — "
              f"`hermes kanban boards list`)\n")
    _print_triage_banner(triage_ids, stranded)
    other_boards = (
        _home_cards_on_other_boards(home_session_ids, args)
        if home_session_ids is not None else None
    )
    if not tasks:
        print("(no matching tasks)")
        if other_boards:
            print(other_boards)
        return 0
    caller = None
    if not (getattr(args, "flat_all", False) or args.session
            or home_session_ids is not None):
        caller = _caller_session_id()
    if not caller:
        for t in tasks:
            print(_fmt_task_line(t, refusals.get(t.id)))
        if other_boards:
            # --home spans every board (the cross-board index the
            # kanban-home-cards block reads, C5 #32), so its hint is true.
            print(other_boards)
        return 0
    print(_format_session_grouped(tasks, kb.home_ids(caller), refusals))
    return 0


def _home_cards_on_other_boards(
    session_ids: Any, args: argparse.Namespace,
) -> Optional[str]:
    """``list --home`` section: OPEN home cards on boards other than this one.

    One read of the cross-board home index (``kanban_home_index``), the same
    source the kanban-home-cards block renders from. Filters the index cannot
    evaluate (assignee/tenant/workflow/archived) skip the section; an
    unavailable index prints a one-line note instead of scanning boards.
    """
    if (args.assignee or getattr(args, "mine", False) or args.tenant
            or args.archived or getattr(args, "workflow_template_id", None)
            or getattr(args, "current_step_key", None)):
        return None
    from hermes_cli import kanban_home_index
    try:
        cards = kanban_home_index.open_cards(list(session_ids), timeout_s=1.0)
    except kanban_home_index.IndexUnavailable as exc:
        return f"(other boards: home index unavailable: {exc})"
    except Exception as exc:  # listing must never fail on the extra section
        return f"(other boards: home index read failed: {type(exc).__name__})"
    current = kb.get_current_board()
    rows = [
        c for c in cards
        if (c.get("board") or kb.DEFAULT_BOARD) != current
        and (not args.status or c.get("status") == args.status)
    ]
    if not rows:
        return None
    rows.sort(key=lambda c: (str(c.get("board")), str(c.get("id"))))
    lines = [f"\nOTHER BOARDS ({len(rows)} open home card{'s' if len(rows) != 1 else ''}):"]
    for c in rows:
        title = " ".join(str(c.get("title") or "").split())
        lines.append(f"  {c.get('id')}  {c.get('status')}  {title}  [board {c.get('board')}]")
    return "\n".join(lines)


def _cmd_home_lint(args: argparse.Namespace) -> int:
    """Silent + exit 0 when every open card has a home; else list ids, exit 1.

    Designed for a no_agent cron (empty stdout = nothing delivered). With
    ``--backfill`` stamps the stragglers ``unhomed`` and exits 0.
    """
    if getattr(args, "open_prs", False):
        from hermes_cli import kanban_open_pr
        with kb.connect_closing() as conn:
            hits = kanban_open_pr.find_done_with_open_pr(conn)
        if args.json:
            print(json.dumps({"done_with_open_pr": hits}))
        else:
            for hit in hits:
                print(
                    f"kanban home-lint: {hit['id']} is DONE but names OPEN PR(s) "
                    f"{', '.join(hit['open_prs'])} -- {hit['title']}"
                )
        return 1 if hits else 0
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


def _format_session_grouped(tasks, home: "frozenset[str]", refusals=None) -> str:
    """Session-first listing: this session's cards in full, every other
    session's cards collapsed to one ``id · status · title`` line each.

    A session sees its OWN work first; foreign cards stay visible (for
    mentions/comments) but read as someone else's. ``--all`` = flat view.
    """
    mine = [t for t in tasks if t.session_id and t.session_id in home]
    others = [t for t in tasks if not (t.session_id and t.session_id in home)]
    lines = [f"THIS SESSION ({len(mine)})"]
    refusals = refusals or {}
    lines += [_fmt_task_line(t, refusals.get(t.id)) for t in mine] or ["  (none)"]
    lines.append("")
    lines.append(
        f"OTHER SESSIONS ({len(others)}) -- not yours: comment, don't act "
        "(--takeover REASON to act; --all for the flat view)"
    )
    for t in others:
        tag = " [unhomed]" if t.unhomed else ""
        if refusals.get(t.id) and t.status in _REFUSAL_VISIBLE_STATUSES:
            tag += f" [{_fmt_refusal(refusals[t.id])}]"
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


def _print_diagnostics(diags, indent: str, *, with_kind: bool) -> None:
    """Shared human rendering for ``show`` and ``diagnostics`` (suggested actions only)."""
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    for d in diags:
        head = f"{d.kind}: {d.title}" if with_kind else d.title
        print(f"{indent}{sev_marker.get(d.severity, '?')} [{d.severity}] {head}")
        if d.data:
            bits = [f"{k}={','.join(str(x) for x in v)}" if isinstance(v, list) else f"{k}={v}"
                    for k, v in d.data.items()]
            if bits:
                print(f"{indent}   data: {' | '.join(bits)}")
        for a in d.actions:
            if a.suggested:
                print(f"{indent}   → {a.label}")


def _print_section(title: str, lines) -> None:
    """Blank line, ``title``, then each line (``show`` body sections)."""
    print()
    print(title)
    for line in lines:
        print(line)


def _cmd_show(args: argparse.Namespace) -> int:
    rsk, rc = _run_state_kwargs(args, "show")
    if rc:
        return rc
    graph = None
    want_json = getattr(args, "json", False)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            return _err(f"no such task: {args.task_id}")
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parent_links = kb.parent_links(conn, args.task_id)
        child_links = kb.child_links(conn, args.task_id)
        parents = [pid for pid, _kind in parent_links]
        children = [cid for cid, _kind in child_links]
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via task_runs.summary; tasks.result stays NULL unless set.
        latest_summary = kb.latest_summary(conn, args.task_id)
        refusal = (
            kb.workspace_refusal_state(conn, [task.id]).get(task.id)
            if task.status in _REFUSAL_VISIBLE_STATUSES else None
        )
        if not want_json:
            graph = kb.task_graph_context(conn, task.id)
        try:
            from hermes_cli import kanban_pr_owner as kpo

            pr_card_map = kpo.pr_card_map(conn, task.id)
        except kb.sqlite3.Error:  # older/minimal board schema: no PR list
            pr_card_map = {}

    held = held_repo([c.body for c in comments]) if task.status in ("review", "blocked", "ready") else None
    if want_json:
        _print_json({
            "task": _task_to_dict(task),
            "home": _home_label(task.session_id, unhomed=task.unhomed),
            "latest_summary": latest_summary,
            "workspace_refusal": refusal,
            "held_repo": held,
            "parents": parents, "children": children,
            "pr_cards": pr_card_map,
            "parent_links": [{"id": pid, "kind": kind} for pid, kind in parent_links],
            "child_links": [{"id": cid, "kind": kind} for cid, kind in child_links],
            "comments": [
                {
                    **_obj_dict(c, ("author", "body", "created_at", "run_id", "session_ref")),
                    "author_display": kb.format_comment_author(
                        c.author, run_id=c.run_id, session_ref=c.session_ref
                    ),
                }
                for c in comments
            ],
            "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs],
        })
        return 0

    def field(label: str, value) -> None:
        print(f"  {label + ':':<11}{value}")

    print(f"Task {task.id}: {task.title}")
    field("status", task.status + (f"  [{_fmt_refusal(refusal)}]" if refusal else ""))
    guard_line = _fmt_current_respawn_guard(task.status, events)
    if guard_line:
        field("guard", guard_line)
    if held:
        field("held", fmt_held_repo(held))
    field("assignee", task.assignee or "-")
    if task.priority:
        field("priority", task.priority)
    if task.no_worker:
        field("dispatch", "operator-only")
    field("session", task.session_id or (kb.UNHOMED_SESSION if task.unhomed else "-"))
    field("home", _home_label(task.session_id, unhomed=task.unhomed))
    if task.tenant:
        field("tenant", task.tenant)
    field("workspace", f"{task.workspace_kind}" + (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        field("branch", task.branch_name)
    if task.skills:
        field("skills", ", ".join(task.skills))
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{_prov}")
        if _pin_badge(task):
            _fb = "family pool" if task.pin_sub_fallback else "wait"
            field("pin", f"{_pin_badge(task)} (capped sub => {_fb})")
    field("reasoning", task.reasoning_effort or "inherit")
    if getattr(task, "brain", None):
        field("brain", task.brain)
    # Effective retry threshold (task > config > default) explains auto-blocks.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        cfg_val = _kanban_config().get("failure_limit")
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    field("created", f"{_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics up top so CLI users see distress signals before scrolling.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph)
    if diags:
        print(f"\n  Diagnostics ({len(diags)}):")
        _print_diagnostics(diags, "    ", with_kind=False)
    if task.started_at:
        field("started", _fmt_ts(task.started_at))
    if task.completed_at:
        field("completed", _fmt_ts(task.completed_at))
    if parents:
        field("parents", _fmt_links(parent_links))
    if children:
        field("children", _fmt_links(child_links))
    for pr, others in (pr_card_map or {}).items():
        field("pr-cards", f"{pr}: " + (", ".join(
            f"{o['id']} ({o['status']}, {o['assignee'] or '-'})" for o in others) or "none other"))
    if task.body:
        _print_section("Body:", [task.body])
    if task.result:
        _print_section("Result:", [task.result])
    elif latest_summary:
        _print_section("Latest summary:", [latest_summary])
    if comments:
        _print_section(f"Comments ({len(comments)}):", (
            f"  [{_fmt_ts(c.created_at)}] "
            f"{kb.format_comment_author(c.author, run_id=c.run_id, session_ref=c.session_ref)}: {c.body}"
            for c in comments))
    if events:
        _print_section(f"Events ({len(events)}):", (
            f"  [{_fmt_ts(e.created_at)}]{f' [run {e.run_id}]' if e.run_id else ''} {e.kind}"
            f"{f' {e.payload}' if e.payload else ''}" for e in events[-20:]))
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = max(0, r.ended_at - r.started_at) if r.ended_at else None
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  {_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _warn_review_assign_parked(task_id: str, profile: Optional[str]) -> None:
    """Warn when an assign landed on a ``review`` card: nothing dispatches it.

    Measured 2026-10-02/03 (t_3bd70b59, t_a57274a4, t_82169667): an operator
    posted a GO comment and ran ``assign <card> <worker>``; the card stayed in
    ``review`` for 4-8 h and the dispatcher never spawned anyone, because it
    only claims ``ready`` cards. The assign itself still succeeds (exit 0) --
    this names the status and the verb that actually moves the card.
    """
    if not kb.is_worker_assignee(profile):
        return
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None or task.status != "review":
        return
    print(
        f"WARNING: {task_id} is in status 'review'; assigning {profile!r} does NOT "
        f"dispatch it (the dispatcher only claims 'ready' cards), so the card stays "
        f"parked. To send it back to the worker run: hermes kanban request-changes "
        f"{task_id} \"<asks>\" --coverage '<json>'  (or: hermes kanban complete "
        f"{task_id} to close it).",
        file=sys.stderr,
    )


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    with kbc.connect_closing() as conn:
        try:
            ok = kb.assign_task(
                conn, args.task_id, profile,
                request_changes_reason=getattr(args, "request_changes", None),
                operator=_profile_author(),
                worker_ok=bool(getattr(args, "worker_ok", False)),
            )
        except (kb.ReviewHoldRequired, kb.NoWorkerFlagSet) as exc:
            return _err(str(exc))
    rc = _ok_or_err(ok, f"no such task: {args.task_id}",
                    f"Assigned {args.task_id} to {profile or '(unassigned)'}")
    if rc == 0:
        _warn_review_assign_parked(args.task_id, profile)
    return rc


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
    if not positionals and getattr(args, "task_id", None):
        # BRIDGE: upstream's table-driven kanban_parser spells this verb as
        # ``<task_id> [model]``; the fork parser (FOLLOWUP: kanban_parser.py)
        # collects ``task_ids`` for the batch/--where forms. Accept both.
        positionals = [args.task_id] + ([args.model] if getattr(args, "model", None) is not None else [])
        args.model = None
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
    clear_brain = bool(getattr(args, "clear_brain", False))
    brain_arg = getattr(args, "brain", None)
    touch_brain = clear_brain or brain_arg is not None
    brain = None
    if brain_arg is not None:
        # Validate BEFORE any write: a typo'd brain must not half-apply.
        try:
            brain = kb.normalize_card_brain(brain_arg)
        except ValueError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if brain is None:
            print("kanban: --brain needs a lane (use --clear-brain to unset)", file=sys.stderr)
            return 2
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
    touch_model = model_given or (effort is None and not clear_effort and not touch_brain)
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
    live = bool(getattr(args, "live", False))
    if live and reclaim:
        print("kanban: --live and --reclaim are exclusive (--live keeps the "
              "running worker; --reclaim aborts it)", file=sys.stderr)
        return 2
    if live and touch_brain:
        # The brain is the lane the harness process was launched through; a
        # running harness cannot change it in place.
        print("kanban: --brain applies on the next dispatch (drop --live, or "
              "use --reclaim)", file=sys.stderr)
        return 2
    if live and (clear_effort or (touch_model and not model and not provider)):
        # A clear resolves through lane overrides / the capped-pool ladder at
        # dispatch time, which a running worker cannot reproduce. Name the
        # route to switch to, or clear without --live (next dispatch).
        print("kanban: --live needs an explicit route or --effort level; "
              "clears apply on the next dispatch (drop --live)", file=sys.stderr)
        return 2
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
    live_runs: dict[str, int] = {}
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
                    live=live,
                )
                if touch_model:
                    effective_model = model or inherited_models.get(task.id)
                    firepower = is_firepower_model(effective_model)
                    write.touch_model = True
                    write.model = effective_model
                    write.provider = provider
                    write.pin_sub_reason = getattr(args, "pin_sub", None)
                    write.pin_sub_fallback = bool(getattr(args, "pin_sub_fallback", False))
                    if write.pin_sub_reason:
                        write.pin_sub_author = _profile_author()
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
                if touch_brain:
                    write.touch_brain = True
                    write.brain = None if clear_brain else brain
                writes.append(write)

            written = set(kb.apply_batch_route_writes(
                conn, writes, skipped=skipped, live_runs=live_runs,
            ))
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
        live_run = live_runs.get(task_id)
        applies = (
            "redispatch" if redispatched
            else f"live(run {live_run})" if live_run is not None
            else "next-dispatch"
        )
        if single:
            if touch_model and (model or provider):
                label = f"{provider}:{model or inherited_models[task_id]}" if provider else model
                suffix = (
                    " (reclaimed; redispatches now)" if redispatched
                    else f" (live: run {live_run} switches at its next turn, "
                         "context kept)" if live_run is not None
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
                when = (
                    f"live: run {live_run} switches at its next turn"
                    if live_run is not None else "applies on next dispatch"
                )
                print(f"Set reasoning effort on {task_id}: {effort} ({when})")
            if clear_brain:
                print(f"Cleared brain on {task_id} (worker uses its profile's foreign_lane.brain)")
            elif touch_brain:
                print(f"Set brain on {task_id}: {brain} "
                      f"({'reclaimed; redispatches now' if redispatched else 'applies on next dispatch'})")
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
        if clear_brain:
            print(f"{task_id}: brain=profile-default applies={applies}")
        elif touch_brain:
            print(f"{task_id}: brain={brain} applies={applies}")
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
    from hermes_cli.model_policy import pinned_sub_provider_error

    pin_sub_reason = getattr(args, "pin_sub", None)
    pin_error = pinned_sub_provider_error(model, provider, pin_sub_reason=pin_sub_reason)
    if pin_error:
        print(f"kanban: {pin_error}", file=sys.stderr)
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
            pin_sub_reason=pin_sub_reason,
        )
    scope = f"assignee={assignee}" if assignee else "assignee=(board-wide)"
    print(
        f"lane-model set: route={override.route} {scope} "
        f"ttl={_format_ttl(ttl_seconds)} applies=next-dispatch"
    )
    if override.pin_sub_reason:
        from hermes_cli.model_policy import format_pin_badge

        print(f"  {format_pin_badge(override.provider, override.pin_sub_reason)} "
              "(capped sub => cards wait)")
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
                "pin_sub_reason": row.pin_sub_reason,
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
        if row.pin_sub_reason:
            from hermes_cli.model_policy import format_pin_badge

            print(f"  {format_pin_badge(row.provider, row.pin_sub_reason)}")
        if row.created_by:
            print(f"  set by: {row.created_by}")
    return 0


def _cmd_pins(args: argparse.Namespace) -> int:
    """List every LIVE deliberate sub pin (cards + lanes) -- the daily lint.

    #1116's failure mode was a forgotten pin silently hogging one sub, so every
    live pin is listed with its age. ``--stale-hours N`` exits 1 when any card
    pin is older than N hours (for a cron lint); otherwise exit 0.
    """
    from hermes_cli.model_policy import format_pin_badge

    now = int(time.time())
    stale_hours = getattr(args, "stale_hours", None)
    rows = []
    with kb.connect_closing() as conn:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        for r in conn.execute(
            "SELECT t.id, t.status, t.assignee, t.title, t.model_override, "
            "t.provider_override, t.pin_sub_reason, t.pin_sub_fallback, t.created_at, "
            "(SELECT max(e.created_at) FROM task_events e WHERE e.task_id = t.id "
            " AND e.kind IN ('model_override_set', 'created')) AS pinned_at "
            f"FROM tasks t WHERE t.pin_sub_reason IS NOT NULL AND t.status IN ({placeholders}) "
            "ORDER BY pinned_at ASC",
            _ACTIVE_STATUSES,
        ).fetchall():
            pinned_at = int(r["pinned_at"] or r["created_at"] or now)
            rows.append({
                "kind": "card", "id": r["id"], "status": r["status"],
                "assignee": r["assignee"], "title": r["title"],
                "provider": r["provider_override"], "model": r["model_override"],
                "reason": r["pin_sub_reason"],
                "fallback": "pool" if r["pin_sub_fallback"] else "wait",
                "age_hours": round((now - pinned_at) / 3600, 1),
            })
        for lane in kb.list_lane_model_overrides(conn, now=now):
            if lane.pin_sub_reason:
                rows.append({
                    "kind": "lane", "id": lane.assignee or "(board-wide)",
                    "status": "active", "assignee": lane.assignee, "title": "",
                    "provider": lane.provider, "model": lane.model,
                    "reason": lane.pin_sub_reason, "fallback": "wait",
                    "age_hours": round((now - int(lane.created_at)) / 3600, 1),
                    "ttl_seconds": lane.ttl_remaining(now),
                })
    stale = [
        r for r in rows
        if stale_hours is not None and r["kind"] == "card" and r["age_hours"] >= stale_hours
    ]
    if getattr(args, "as_json", False):
        print(json.dumps({"pins": rows, "stale": [r["id"] for r in stale]}, indent=2))
    elif not rows:
        print("(no live sub pins -- every worker rides the pool)")
    else:
        for r in rows:
            badge = format_pin_badge(r["provider"], r["reason"])
            extra = (f"ttl={_format_ttl(r['ttl_seconds'])}" if r["kind"] == "lane"
                     else f"status={r['status']} assignee={r['assignee'] or '-'}")
            print(f"{r['kind']} {r['id']} {badge} model={r['model']} "
                  f"age={r['age_hours']}h fallback={r['fallback']} {extra}"
                  + (f"  {r['title']}" if r["title"] else ""))
        if stale:
            print(f"STALE (>= {stale_hours}h): {', '.join(r['id'] for r in stale)} -- "
                  "clear with `hermes kanban set-model <id> none` if no longer wanted")
    return 1 if stale else 0


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
            operator=getattr(args, "operator", None),
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
    profile = _none_profile(args.profile)
    reclaim_first = bool(getattr(args, "reclaim", False))
    # `--reclaim` SIGTERMs the live worker and releases the claim BEFORE the
    # assign is attempted. If the assign then refuses, the old "cannot
    # reassign (still running — pass --reclaim)" line was actively false: the
    # reclaim had already happened and the worker was already dead. Carry the
    # receipt so the failure path can say what really landed.
    receipt: dict = {}
    with kb.connect_closing() as conn:
        try:
            ok = kb.reassign_task(
                conn, args.task_id, profile,
                reclaim_first=reclaim_first,
                reason=getattr(args, "reason", None),
                receipt=receipt,
                request_changes_reason=getattr(args, "request_changes", None),
                operator=_profile_author(),
                worker_ok=bool(getattr(args, "worker_ok", False)),
            )
        except kb.NoWorkerFlagSet as exc:
            print(str(exc), file=sys.stderr)
            return 1
    reclaimed = bool(receipt.get("reclaimed"))
    if not ok and receipt.get("hold_error"):
        print(
            receipt["hold_error"]
            + (" (the claim WAS reclaimed first)" if reclaimed else ""),
            file=sys.stderr,
        )
        return 1
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
    _warn_review_assign_parked(args.task_id, profile)
    return 0


def _rows_by_task(conn, table: str, ids: list[str]) -> dict[str, list]:
    """``{task_id: [rows ordered by id]}`` for every id (empty list when none)."""
    by = {i: [] for i in ids}
    placeholders = ",".join(["?"] * len(ids))
    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids)):
        by.setdefault(row["task_id"], []).append(row)
    return by


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board via the same rule engine the dashboard uses."""
    from hermes_cli import kanban_diagnostics as kd
    # Honour kanban.default_assignee as the fallback for unassigned ready tasks (#27145),
    # kanban.max_in_progress as the global concurrency cap (#33488), kanban.max_in_progress_per_profile as
    # the per-profile cap (#21582), and kanban.max_spawn as the per-tick spawn limit (#28805). Same
    # semantics as the gateway dispatch path so behavior matches whether the user runs the CLI directly or
    # relies on the gateway-embedded dispatcher.
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kbc.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                return _err(f"no such task: {args.task}")
            diags_by_task = {args.task: kd.compute_task_diagnostics(
                task, kb.list_events(conn, args.task), kb.list_runs(conn, args.task),
                graph=kb.task_graph_context(conn, args.task), config=diag_config)}
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall())
            ids = [r["id"] for r in rows]
            diags_by_task = {}
            if ids:
                ev_by = _rows_by_task(conn, "task_events", ids)
                run_by = _rows_by_task(conn, "task_runs", ids)
                graph_by = kb.task_graph_contexts(conn, ids)
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(r, ev_by.get(tid, []), run_by.get(tid, []),
                                                     graph=graph_by.get(tid), config=diag_config)
                    if dl:
                        diags_by_task[tid] = dl

        sev = getattr(args, "severity", None)
        if sev:
            floor = kd.SEVERITY_ORDER.index(sev)
            diags_by_task = {tid: kept for tid, dl in diags_by_task.items()
                             if (kept := [d for d in dl if kd.SEVERITY_ORDER.index(d.severity) >= floor])}

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                                  tuple(diags_by_task.keys())):
                meta[r["id"]] = {k: r[k] for k in ("title", "status", "assignee")}

    # What this home believes it may claim on a shared board (#113620).
    allowlist = kbd.dispatch_profile_allowlist_summary()

    if getattr(args, "json", False):
        # Per-task rows unchanged; the home-scope allowlist rides as a trailing row
        # (task_id null) so existing `payload[0]["diagnostics"]` consumers keep working.
        _print_json([{"task_id": tid, **meta.get(tid, {}), "diagnostics": [d.to_dict() for d in dl]}
                     for tid, dl in diags_by_task.items()]
                    + [{"task_id": None, "dispatch_profiles": allowlist, "diagnostics": []}])
        return 0

    # Host-level dispatcher load gate (kanban.dispatch_load_gate), published
    # each tick by the gated dispatcher loop (gateway or `kanban daemon`).
    try:
        from hermes_cli import kanban_load_gate as _klg

        _gate_state = _klg.read_state()
        print(_klg.format_state_line(_gate_state))
        for _line in _klg.format_board_starvation_lines(_gate_state):
            print(_line)
    except Exception:
        pass

    print(f"kanban.dispatch_profiles: {allowlist}")
    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    total = sum(len(dl) for dl in diags_by_task.values())
    print(f"{total} active diagnostic(s) across {len(diags_by_task)} task(s):\n")
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        print(f"  {tid}  {m.get('status') or '?':8s}  @{m.get('assignee') or '(unassigned)':18s}  "
              f"{m.get('title') or '(untitled)'}")
        _print_diagnostics(dl, "    ", with_kind=True)
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    kind = getattr(args, "kind", None) or kb.DEFAULT_LINK_KIND
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        gated = kb.link_tasks(conn, args.parent_id, args.child_id, kind=kind,
                              expected_child_run_id=expected_child_run_id)
    print(f"Linked {args.parent_id} -> {args.child_id} ({kind})")
    if gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    return _ok_or_err(ok, f"No such link: {args.parent_id} -> {args.child_id}",
                      f"Unlinked {args.parent_id} -> {args.child_id}")


def _cmd_claim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        if args.review:
            session_ref = _operator_review_session_ref()
            if session_ref is None:
                # An unbound claim is a dead end (t_c3cf232e): no later
                # request-changes can inherit it, and it only parks the card in
                # ``running`` under this short-lived CLI pid. Refuse up front.
                print(
                    f"cannot claim {args.task_id} --review: this caller has no "
                    f"session identity (sessionless shell, cron job or delegate "
                    f"child), so no later request-changes could use the claim. "
                    f"Run it from the reviewing session, approve directly with "
                    f"`hermes kanban complete {args.task_id}`, or send it back "
                    f"with `hermes kanban request-changes {args.task_id} REASON "
                    f"--operator \"<who: why>\"` (no claim needed).",
                    file=sys.stderr,
                )
                return 1
            task = kb.claim_review_task(
                conn, args.task_id, ttl_seconds=args.ttl,
                session_ref=session_ref, operator_claim=True,
            )
        else:
            task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"no such task: {args.task_id}")
            return _err(f"cannot claim {args.task_id}: status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}\nWorkspace: {workspace}")
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
    body_file = getattr(args, "body_file", None)
    if body_file is not None and args.text:
        print("kanban: pass the comment as TEXT or --body-file, not both", file=sys.stderr)
        return 2
    if body_file is None and not args.text:
        print("kanban: comment body required (TEXT or --body-file)", file=sys.stderr)
        return 2
    if body_file is not None:
        try:
            body = _read_body_file(body_file).strip()
        except OSError as exc:
            print(f"kanban: --body-file: {exc}", file=sys.stderr)
            return 2
    else:
        body = " ".join(args.text).strip()
    if not body:
        print("kanban: comment body is empty", file=sys.stderr)
        return 2
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len must be positive", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author, claimed = _comment_author(args.author)
    if claimed is not None:
        print(
            f"kanban: --author {claimed!r} needs the operator token "
            f"({kb.OPERATOR_TOKEN_ENV}); commenting as {author!r}, "
            "the requested author is kept as claimed_author",
            file=sys.stderr,
        )
    run_id, session_ref = safe_comment_provenance(args.task_id)
    with kb.connect_closing() as conn:
        if run_id is None:
            # Human review lane: the session holding ``claim --review`` on this
            # card attests to that review run (the claim is the provenance).
            run_id = _operator_review_run_id(conn, args.task_id)
        kb.add_comment(
            conn, args.task_id, author, body,
            run_id=run_id, session_ref=session_ref, claimed_author=claimed,
        )
    print(f"Comment added to {args.task_id}")
    return 0


def _comment_author(requested: Optional[str]) -> tuple[str, Optional[str]]:
    """``(author, claimed_author)`` for a CLI comment.

    ``env -u HERMES_KANBAN_TASK hermes kanban comment --author human:apollo``
    wrote an operator-labelled comment from any worker shell (Prism gate spec
    §8b). ``--author`` may now never raise a caller above its own identity: it
    is honoured for an operator-profile caller (``RULING_AUTHORS``, already the
    label every reader trusts, so a chosen label grants nothing; fleet crons run
    there and deliberately pick NON-operator labels such as ``land-autopilot``),
    or with the operator token (the same gate as ``--operator``/``--takeover``).
    Any other caller writes as itself; the requested label survives only as
    ``claimed_author`` on the ``commented`` event, for forensics.
    """
    from hermes_cli.kanban_worker_policy import RULING_AUTHORS

    caller = _profile_author()
    want = (requested or "").strip()
    if (not want or want == caller or caller.strip().lower() in RULING_AUTHORS
            or kb._operator_token_state() == "ok"):
        return want or caller, None
    return caller, want


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file via the shared ``store_attachment_bytes`` path (same 25 MB cap and name
    sanitisation as the dashboard upload and agent tool)."""
    import mimetypes
    _worker_run_id_for(args.task_id)

    src = Path(args.path).expanduser()
    if not src.is_file():
        return _err(f"kanban: no such file: {src}")
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(conn, args.task_id, name, data, content_type=content_type,
                                               uploaded_by=uploaded_by)
    except kb.AttachmentTooLarge as exc:
        return _err(f"kanban: {exc}")
    print(f"Attached {name} to {args.task_id} (attachment {att_id}, {len(data)} bytes)")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        atts = kb.list_attachments(conn, args.task_id)
    if _json_out(args, [_obj_dict(a, _ATTACHMENT_FIELDS) for a in atts], ascii=True):
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
    with kbc.connect_closing() as conn:
        removed = kb.delete_attachment(conn, args.attachment_id)
    if removed is None:
        return _err(f"no such attachment: {args.attachment_id}")
    print(f"Deleted attachment {args.attachment_id} ({removed.filename}) from {removed.task_id}")
    return 0


def _operator_review_session_ref() -> Optional[str]:
    """Session fingerprint that may bind / use a human-lane review claim.

    ``None`` (never bindable) for a delegate_task child, for anything inside a
    cron job (in-process context flag or a ``cron_*`` session id), and for a
    caller with no session at all: those cannot prove they are the reviewer
    session, so they get no review run from :func:`_operator_review_run_id`.
    """
    try:
        from agent.delegation_context import (
            _NON_DISPATCHER_OWNED_CONTEXT,
            is_delegated_child_process_context,
        )

        if is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get():
            return None
    except Exception:
        return None
    session_id = _caller_session_id()
    if not session_id or session_id.startswith("cron_"):
        return None
    return kb.derive_session_ref(session_id)


def _review_override_caller_allowed() -> bool:
    """May this caller send back a parked review card on an explicit
    ``--operator`` / ``--takeover`` without a bindable session?

    A plain operator shell (no session identity) may. A delegate_task child or
    a cron job may not: they can never act as the human reviewer.
    """
    try:
        from agent.delegation_context import (
            _NON_DISPATCHER_OWNED_CONTEXT,
            is_delegated_child_process_context,
        )

        if is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get():
            return False
    except Exception:
        return False
    session_id = _caller_session_id() or ""
    return not session_id.startswith("cron_")


def _operator_review_run_id(conn, task_id: str) -> Optional[int]:
    """The active review run on ``task_id`` iff THIS session claimed it."""
    return kb.review_claim_run_for_session(
        conn, task_id, _operator_review_session_ref(),
    )


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

    An OWNING worker is scoped to its own card: asking for a run id on another
    task refuses (same rule as ``tools.kanban_tools._enforce_worker_task_ownership``).
    """
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if not is_dispatcher_owned_worker_context():
            return None
    except Exception:
        pass
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        raise ValueError(f"worker is scoped to task {env_tid}; refusing to mutate {task_id}")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if os.environ.get("HERMES_KANBAN_TASK") != task_id or not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str, *, conn=None, task_id=None,
                                 verdicts: Optional[dict] = None) -> Optional[str]:
    """CLI-surface wiring of the shared goal-mode handoff gate (complete + review).

    ``goals.kanban_handoff_rejection`` owns the policy (judge retries, transient
    block on a judge error for the owning worker, fail-open for operators) so the
    CLI and ``tools.kanban_tools`` cannot drift. The judge is wrapped to bind the
    per-task relay-affinity scope — the gate runs outside any agent turn (mirrors
    kanban_specify, #113669) — and to record the last verdict so the caller can
    pick ``blocked`` vs ``continue`` guidance (#100954).
    """
    from hermes_cli import goals

    def _judge(**kwargs):
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        scope_id = task_id or getattr(task, "id", None)
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{scope_id}")
        try:
            out = goals.judge_goal(**kwargs)
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
        if verdicts is not None:
            verdicts["verdict"] = out[0]
        return out

    return goals.kanban_handoff_rejection(
        task, evidence, conn=conn, task_id=task_id,
        worker_run_id_for=_worker_run_id_for,
        judge_available=goals.goal_judge_available,
        judge=_judge,
    )


def _goal_gate_error(conn, tid: str, evidence: str, handoff: str, blocked_hint: str,
                     continue_hint: str) -> Optional[str]:
    """Goal-mode judge gate shared by ``complete`` / ``request-review`` (mirrors tools/kanban_tools.py);
    applied to every terminal handoff so request-review can't bypass it. Returns the error line, or
    None to allow."""
    verdicts: dict = {}
    rejection = _goal_mode_handoff_rejection(
        kb.get_task(conn, tid), evidence, conn=conn, task_id=tid, verdicts=verdicts,
    )
    if rejection is None:
        return None
    if verdicts.get("verdict") == "blocked":
        return (f"kanban: goal {handoff} of {tid} rejected: judge ruled "
                f"the goal unachievable — {rejection}. {blocked_hint}")
    return f"kanban: goal {handoff} of {tid} rejected by judge: {rejection}. {continue_hint}"


def _last_event_id(conn, task_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    return int(row[0] or 0)


#: Events ``complete_task`` records when it does NOT close the card. It
#: returns a bare bool, so the CLI reads the reason back off the card rather
#: than printing a generic line (or nothing) while the card stays put
#: (t_1e080b8d: an operator saw rc=0 and no reason, card still in review).
_COMPLETION_OUTCOME_EVENTS = {
    "completion_route_refused": "open-PR review route refused",
    "completion_routed_to_review": "handoff names still-OPEN PR(s)",
    "workspace_held": "workspace held",
}


def _completion_outcome(conn, task_id: str, after_event: int) -> str:
    """Why this ``complete`` call did not mark ``task_id`` done, or ''."""
    kinds = tuple(_COMPLETION_OUTCOME_EVENTS)
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id",
        (task_id, after_event, *kinds),
    ).fetchall()
    parts = []
    for kind, payload in rows:
        try:
            data = json.loads(payload or "{}")
        except (TypeError, ValueError):
            data = {}
        detail = data.get("reason") or ", ".join(data.get("open_prs") or [])
        label = _COMPLETION_OUTCOME_EVENTS[kind]
        parts.append(f"{label}: {detail}" if detail else label)
    return "; ".join(parts)


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids, rc = _require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    superseded_by = getattr(args, "superseded_by", None)
    draft_ok = getattr(args, "draft_ok", None)
    external = getattr(args, "external", None)
    watcher = getattr(args, "watcher", None)
    raw_meta = getattr(args, "metadata", None)
    # Guard: structured handoff fields are per-run, so they'd be
    # copy-pasted identically across N runs — almost always a footgun.
    # Refuse instead of silently doing the wrong thing.
    survivor_ref = getattr(args, "survivor_ref", None)
    survivor_pr = getattr(args, "survivor_pr", None)
    survivor_unbound = getattr(args, "survivor_unbound", None) or None
    survivor_none = getattr(args, "survivor_none", False)
    survivor_reason = getattr(args, "reason", None)
    if len(ids) > 1 and (summary or raw_meta or survivor_ref or survivor_pr
                         or survivor_unbound or survivor_none or survivor_reason or superseded_by
                         or draft_ok is not None or external is not None or watcher is not None):
        return _err(
            "kanban: --summary / --metadata / --superseded-by / --draft-ok / --external / "
            "--watcher / --survivor-ref / "
            "--survivor-pr / --survivor-unbound / --survivor-none / --reason are per-task "
            "and can't be used with multiple ids (would apply the same handoff, and record "
            "the same survivor, for every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.", 2)
    if survivor_none != bool(survivor_reason) or (survivor_none and (survivor_ref or survivor_pr or survivor_unbound)):
        return _err("kanban: --survivor-none requires --reason and cannot combine with survivor claims", 2)
    if survivor_unbound and not (survivor_ref or survivor_pr):
        return _err(
            "kanban: --survivor-unbound only relaxes the task-id binding on an explicit "
            "--survivor-ref/--survivor-pr claim; pass one, or drop the flag.", 2)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            return _err(
                f"kanban: --metadata wants a JSON object, e.g. "
                f"--metadata '{{\"base_guard_override\": \"<reason>\"}}' ({exc})", 2)
    failed: list[str] = []
    no_receipt = False
    with kbc.connect_closing() as conn:
        for tid in ids:
            # Goal-mode judge gate (mirrors tools/kanban_tools.py). Apply it
            # to every terminal handoff so request-review cannot bypass the
            # acceptance contract that protects complete.
            # A superseded close has no work for the judge to grade; gating it
            # would push the worker back into exiting silently.
            gate_err = None if (superseded_by is not None or external is not None) else _goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.")
            if gate_err:
                print(gate_err, file=sys.stderr)
                failed.append(tid)
                continue

            last_event = _last_event_id(conn, tid)
            try:
                done = kb.complete_task(
                    conn, tid,
                    result=args.result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=_worker_run_id_for(tid),
                    force=bool(getattr(args, "force", False)),
                    survivor_ref=survivor_ref,
                    survivor_pr=survivor_pr,
                    survivor_unbound=survivor_unbound,
                    survivor_none=survivor_none,
                    survivor_reason=survivor_reason,
                    superseded_by=superseded_by,
                    draft_ok=draft_ok,
                    external=external,
                    watcher=watcher,
                )
            except kb.LiveClaimError:
                failed.append(tid)
                print(f"cannot complete {tid}: a live worker is running it. Wait for the "
                      f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                      f"--force to close its run and complete anyway.", file=sys.stderr)
                continue
            except kb.EmptyCompletionError as empty_err:
                failed.append(tid)
                print(f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                      f"describing what was done (an empty completion is not evidence).", file=sys.stderr)
                continue
            except ReceiptRequiredError as receipt_err:
                failed.append(tid)
                no_receipt = True
                print(f"cannot complete {tid}: {receipt_err}.", file=sys.stderr)
                continue
            except (kb.EmptySupersedeError, kb.EmptyDraftOverrideError,
                    kb.ExternalCloseError) as supersede_err:
                failed.append(tid)
                print(f"cannot complete {tid}: {supersede_err}.", file=sys.stderr)
                continue
            except (DraftPrError, StaleBaseError, ClosedUnmergedPrError) as draft_err:
                failed.append(tid)
                print(f"cannot complete {tid}: {draft_err}", file=sys.stderr)
                continue
            outcome = _completion_outcome(conn, tid, last_event)
            if not done:
                failed.append(tid)
                # complete_task returns bare False for a dependency refusal too;
                # name the open parents instead of claiming the id is unknown.
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers and not outcome:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    print(f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                          f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.",
                          file=sys.stderr)
                    continue
                print(
                    f"cannot complete {tid}: "
                    f"{outcome or kb.explain_complete_refusal(conn, tid, expected_run_id=_worker_run_id_for(tid))}",
                    file=sys.stderr,
                )
            else:
                after = kb.get_task(conn, tid)
                if getattr(after, "status", None) == "review":
                    print(f"Routed {tid} to review, NOT done: "
                          f"{outcome or 'handoff names a still-OPEN PR'}")
                elif external is not None:
                    print(f"Completed {tid} (external: {external.strip()}, watcher {watcher.strip()})")
                else:
                    print(f"Completed {tid}")
                override = _draft_override_of(conn, tid, last_event)
                if override:
                    print(f"  draft override recorded for {', '.join(override['prs'])}: "
                          f"{override['reason']}")
    if no_receipt:
        return EXIT_NO_RECEIPT
    return 0 if not failed else 1


def _draft_override_of(conn, tid: str, after_event_id):
    """This completion's ``completion_draft_override`` payload on ``tid``, or None."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND id > ? AND kind = "
        "'completion_draft_override' ORDER BY id DESC LIMIT 1", (tid, after_event_id or 0),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _set_priority(conn, task_id: str, priority: int) -> bool:
    ok, old = kb.set_task_priority(
        conn, task_id, priority, actor=_profile_author(),
    )
    if not ok:
        print(f"cannot set priority on {task_id} (unknown id)", file=sys.stderr)
        return False
    print(f"{task_id}: priority {old} -> {priority}")
    return True


def _cmd_priority(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        return 0 if _set_priority(conn, args.task_id, args.priority) else 1


def _cmd_edit(args: argparse.Namespace) -> int:
    raw_meta = getattr(args, "metadata", None)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            return _err(
                f"kanban: --metadata wants a JSON object, e.g. "
                f"--metadata '{{\"base_guard_override\": \"<reason>\"}}' ({exc})", 2)

    model_override = getattr(args, "model_override", None)
    clear_model = bool(getattr(args, "clear_model", False))
    if model_override is not None and clear_model:
        return _err("kanban: --model and --clear-model are mutually exclusive", 2)

    # The result-backfill edit is only attempted when --result is given;
    # --title/--body/--priority are plain field edits; --model / --clear-model /
    # --session / --no-worker are independent edits that apply to any task.
    result = getattr(args, "result", None)
    summary = getattr(args, "summary", None)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    do_result = result is not None
    do_fields = title is not None or body is not None
    do_model = model_override is not None or clear_model
    new_session = getattr(args, "session", None)
    do_session = new_session is not None
    no_worker = getattr(args, "no_worker", None)
    new_priority = getattr(args, "priority", None)
    page = getattr(args, "page", None)
    add_skills = list(getattr(args, "skills", None) or [])
    clear_skills = bool(getattr(args, "clear_skills", False))
    do_skills = bool(add_skills) or clear_skills

    if result is None and (summary is not None or raw_meta is not None):
        return _err("kanban edit: --summary and --metadata require --result", 2)
    if (not do_result and not do_fields and not do_model and not do_session
            and no_worker is None and new_priority is None and page is None and not do_skills):
        return _err(
            "kanban: nothing to edit (pass --title, --body, --result, --model, --clear-model, "
            "--session, --priority, --no-worker, --worker-ok, --no-page, --page, "
            "--skill or --clear-skills)", 2)

    rc = 0
    with kbc.connect_closing() as conn:
        if new_priority is not None and not _set_priority(conn, args.task_id, new_priority):
            return 1
        if no_worker is not None:
            if not kb.set_no_worker(
                conn, args.task_id, no_worker, operator=_profile_author(),
            ):
                return _err(f"cannot edit {args.task_id} (unknown id)")
            print(
                f"{args.task_id}: dispatch: "
                + ("operator-only (no-worker)" if no_worker else "worker-ok")
            )
        if page is not None:
            if not kb.set_needs_input_page(
                conn, args.task_id, page, operator=_profile_author(),
            ):
                print(f"cannot edit {args.task_id} (unknown id)", file=sys.stderr)
                return 1
            print(f"{args.task_id}: needs-input pager: " + ("on" if page else "off"))
        if do_skills:
            try:
                skills = kb.set_task_skills(
                    conn, args.task_id, add=add_skills, clear=clear_skills,
                    operator=_profile_author(),
                )
            except ValueError as exc:
                print(f"kanban: {exc}", file=sys.stderr)
                return 2
            if skills is None:
                print(f"cannot edit {args.task_id} (unknown id)", file=sys.stderr)
                return 1
            print(f"{args.task_id}: skills: {', '.join(skills) or '(none)'}")
        if do_session:
            sid = None if new_session.strip().lower() in ("", "none") else new_session.strip()
            if not kb.set_task_session(conn, args.task_id, sid):
                return _err(f"cannot restamp {args.task_id} (unknown id)")
            print(f"Home session of {args.task_id}: {sid or 'unstamped'}")
        if do_model:
            # --clear-model writes NULL; --model X writes X literally. The
            # None sentinel ("--model omitted") never reaches here.
            new_model = None if clear_model else model_override
            affected = kb.set_task_model(conn, args.task_id, new_model)
            if affected == 0:
                return _err(f"cannot set model on {args.task_id} (unknown id)")
            if clear_model:
                print(f"Cleared model override on {args.task_id}")
            else:
                print(f"Set model override on {args.task_id}: {new_model}")
        if do_result or do_fields:
            ok = kb.edit_task(
                conn, args.task_id, title=title, body=body,
                result=result, summary=summary, metadata=metadata,
            )
            if not ok:
                return _err(f"cannot edit {args.task_id} (unknown id, or --result used on a task that is not done)")
            print(f"Edited {args.task_id}")
    return rc


def _commented(conn, reason: Optional[str], author, prefix: str, op):
    """Wrap a per-task ``op`` so a ``reason`` is first recorded as a ``PREFIX: reason`` comment."""
    def run(tid):
        if reason:
            kb.add_comment(conn, tid, author, f"{prefix}: {reason}")
        return op(tid)
    return run


def _cmd_block(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    kind = getattr(args, "kind", None)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    failed: list[str] = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            # Provenance before the transition: blocking ends the run.
            _run_id, _sess_ref = safe_comment_provenance(tid) if reason else (None, None)
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
                if reason:
                    # Only after the transition landed: a refused or failed
                    # block leaves no "BLOCKED:" comment (C5 #20, FleetReview
                    # e6af55d359f5).
                    kb.add_comment(
                        conn, tid, author, f"BLOCKED: {reason}",
                        run_id=_run_id, session_ref=_sess_ref,
                    )
                # Report where it landed: dependency blocks -> todo, tripped unblock-loop breaker -> triage.
                landed = kb.get_task(conn, tid)
                where = landed.status if landed else "blocked"
                if where == "todo":
                    print(f"{tid} → todo (dependency wait){suffix}")
                elif kind == "dependency" and where == "blocked":
                    print(f"Blocked {tid} as needs_input (no open parent to wait on){suffix}")
                elif where == "triage":
                    # Only a typed owner-input block carries a question for a human.
                    verdict = ("needs a human decision"
                               if (getattr(landed, "block_kind", None) if landed else kind) == "needs_input"
                               else "orchestration attention needed")
                    print(f"{tid} → triage (unblock loop detected — {verdict}){suffix}")
                else:
                    print(f"Blocked {tid}{suffix}")
    return 0 if not failed else 1


def _parse_wake_at(value: str) -> int:
    """``--at`` value -> epoch seconds. Accepts epoch digits or ISO-8601
    (a naive timestamp is local time, like every other human-typed time)."""
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    ids = _bulk_ids(args)
    wake_at: Optional[int] = None
    if getattr(args, "now", False):
        wake_at = int(time.time())
    elif getattr(args, "at", None):
        try:
            wake_at = _parse_wake_at(args.at)
        except ValueError:
            print(f"invalid --at {args.at!r} (epoch seconds or ISO-8601)", file=sys.stderr)
            return 1
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            current = kb.get_task(conn, tid)
            # An already-scheduled card only gets its wake time (re)set; the
            # old path refused it outright, leaving no timed exit.
            already = (
                current is not None and current.status == "scheduled"
                and wake_at is not None
            )
            # Provenance before the transition: scheduling ends the run.
            _run_id, _sess_ref = safe_comment_provenance(tid) if reason else (None, None)

            def _status_comment() -> None:
                # Only after a mutation landed: a refused or failed schedule
                # leaves no "SCHEDULED:" comment (C5 #20, FleetReview
                # e6af55d359f5).
                if reason:
                    kb.add_comment(
                        conn, tid, author, f"SCHEDULED: {reason}",
                        run_id=_run_id, session_ref=_sess_ref,
                    )

            if not already and not kb.schedule_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot schedule {tid}", file=sys.stderr)
                continue
            if not already:
                _status_comment()
                print(f"Scheduled {tid}" + (f": {reason}" if reason else ""))
            if wake_at is None:
                continue
            ok, err = kb.set_schedule_wake(
                conn, tid, wake_at=wake_at, actor=author, reason=reason,
            )
            if ok and already:
                # The wake reset is the only mutation on an already-scheduled card.
                _status_comment()
            if not ok:
                failed.append(tid)
                print(f"cannot set wake for {tid}: {err}", file=sys.stderr)
            else:
                when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(wake_at))
                print(f"Wake set {tid} at {when} (dispatcher promotes on its next tick at/after)")
    return 0 if not failed else 1


def _cmd_unblock(args: argparse.Namespace) -> int:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err("kanban unblock is orchestrator-only; workers must hand off their assigned task")
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = _stripped_or_none(getattr(args, "reason", None))
    author = _profile_author() if reason else None
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            # The "UNBLOCK:" comment commits atomically with the transition
            # and only if it lands: no comment on a refused/failed unblock, and
            # no window where a respawned worker sees the card without the
            # reason (C5 #20, FleetReview e6af55d359f5 / aa67ba1c7513).
            comment = None
            if reason:
                _run_id, _sess_ref = safe_comment_provenance(tid)
                comment = (author, f"UNBLOCK: {reason}", _run_id, _sess_ref)
            if not kb.unblock_task(conn, tid, comment=comment):
                failed.append(tid)
                print(f"cannot unblock {tid} (not blocked/scheduled?)", file=sys.stderr)
            else:
                print(f"Unblocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_workspace(args: argparse.Namespace) -> int:
    if getattr(args, "workspace_action", None) != "reset":
        print("usage: kanban workspace reset (<task_id>... | --all-stranded) [--dry-run]",
              file=sys.stderr)
        return 2
    ids = list(args.task_ids or [])
    if bool(ids) == bool(args.all_stranded):
        print("kanban workspace reset: give task id(s) or --all-stranded (not both)",
              file=sys.stderr)
        return 2
    actor = _profile_author()
    verb = "Would reset" if args.dry_run else "Reset"
    refused = 0
    with kb.connect_closing() as conn:
        if args.all_stranded:
            ids = kb.stranded_workspace_candidates(conn)
            if not ids:
                print("No stranded scratch workspaces.")
                return 0
        for task_id in ids:
            task = kb.get_task(conn, task_id)
            previous = task.workspace_path if task else None
            try:
                ok, err = kb.reset_stranded_workspace(
                    conn, task_id, actor=actor, reason=args.reason, dry_run=args.dry_run,
                )
            except kb.ForeignSessionMutationError as exc:
                # One foreign-home card must not abort a board-wide sweep.
                ok, err = False, str(exc)
            if ok:
                print(f"{verb} {task_id} (was {previous})")
            else:
                refused += 1
                print(f"cannot reset {task_id}: {err}", file=sys.stderr)
    # --all-stranded keeps sweeping past a refusal, but the exit status must
    # still say "not every card was reset": a script checking rc must not read
    # a sweep that reset nothing (e.g. root still unmounted) as done (C6, #1037).
    if refused:
        print(f"kanban workspace reset: {refused} card(s) refused", file=sys.stderr)
        return 1
    return 0


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
    summary = _stripped_or_none(getattr(args, "summary", None))
    raw_metadata = getattr(args, "metadata", None)
    metadata = None
    if raw_metadata:
        try:
            metadata = json.loads(raw_metadata)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            return _err(
                f"kanban: --metadata wants a JSON object, e.g. "
                f"--metadata '{{\"base_guard_override\": \"<reason>\"}}' ({exc})", 2)
    reviewer = getattr(args, "reviewer", None)
    with kbc.connect_closing() as conn:
        gate_err = _goal_gate_error(
            conn, tid, summary or "", "review handoff",
            "Record the block with kanban block instead of requesting review.",
            "Provide acceptance evidence matching the task.")
        if gate_err:
            return _err(gate_err)
        try:
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
        except ReceiptRequiredError as receipt_err:
            print(f"cannot request review for {tid}: {receipt_err}", file=sys.stderr)
            return EXIT_NO_RECEIPT
        except (DraftPrError, StaleBaseError, ClosedUnmergedPrError) as draft_err:
            return _err(f"cannot request review for {tid}: {draft_err}")
        if not ok:
            return _err(f"cannot request review for {tid}: {reason or 'not running/ready?'}")
        if ok and reason:
            # Success with a diagnostic = the review was resolved WITHOUT a
            # reviewer session (kanban.review_policy=milestone_only). Say so;
            # "Requested review" would be a lie the worker then reasons from.
            print(f"{tid}: {reason}")
            return 0
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"Requested review for {tid}" + (f": {display_summary}" if display_summary else ""))
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    operator = (getattr(args, "operator", None) or "").strip() or None
    if args.coverage is not None:
        # Before any write (claim release, comment, transition): a refused
        # record must not land on the card (t_c5bfb48b).
        coverage_error = kb.review_coverage_text_error(args.coverage)
        if coverage_error:
            print(f"cannot request changes for {tid}: {coverage_error}", file=sys.stderr)
            return 1
    with kb.connect_closing() as conn:
        # The caller must hold the review run: as its dispatcher-owned worker,
        # or as the operator session that made ``claim --review`` (human lane).
        worker_run = _worker_run_id_for(tid)
        if operator is not None and worker_run is not None:
            # A dispatched reviewer run always carries full coverage.
            print(
                f"cannot request changes for {tid}: --operator is not for a "
                f"dispatched review run; post the review_coverage record",
                file=sys.stderr,
            )
            return 1
        held_run = worker_run if worker_run is not None else _operator_review_run_id(conn, tid)
        parked_session = None
        parked_override = False
        released_unbound = False
        if held_run is None and worker_run is None:
            # A review claim that bound NO session can never be held by
            # anyone; left alone it strands the card in running until the
            # TTL lapses (t_0485b3ff). Return it to review, then send back
            # through the parked path below in this same call.
            released_unbound = kb.release_unbound_review_claim(
                conn, tid, reason="request-changes: review claim bound no session",
            )
        if held_run is None:
            # Card parked in ``review`` with nobody holding it: the operator
            # session opens the review run itself and sends back in ONE txn
            # (same audit as ``claim --review`` + request-changes). Only a
            # session that could hold that claim may do this.
            task = kb.get_task(conn, tid)
            parked_override = False
            if task is not None and task.status == "review":
                parked_session = _operator_review_session_ref()
                # An explicit --operator / --takeover send-back needs no prior
                # ``claim --review`` and no bindable session (t_c3cf232e:
                # Apollo's operator send-back from a sessionless shell was
                # refused). Delegate children and cron jobs stay refused.
                parked_override = parked_session is None and (
                    operator is not None
                    or bool((getattr(args, "foreign_ok", None) or "").strip())
                ) and _review_override_caller_allowed()
            if parked_session is None and not parked_override:
                print(
                    f"cannot request changes for {tid}: this session does not hold its "
                    f"review run; claim it from the reviewing session first "
                    f"(hermes kanban claim {tid} --review), or, on a card parked in "
                    f"review, send it back with --operator \"<who: why>\" or "
                    f"--takeover REASON. Delegate children and cron jobs cannot "
                    f"hold a human-lane review claim."
                    + (
                        " The unbound review claim was released; the card is back in review."
                        if released_unbound else ""
                    ),
                    file=sys.stderr,
                )
                return 1
        ok, detail = kb.request_changes(
            conn,
            tid,
            reason=reason,
            expected_run_id=held_run,
            operator=operator,
            # The coverage comment is written inside the transition's
            # transaction, so a refused send-back rolls it back. ``claimer``
            # authors it; on a parked card it also opens the review run.
            coverage=args.coverage,
            claimer=_profile_author(),
            **(
                {"session_ref": parked_session}
                if parked_session is not None or parked_override
                else {}
            ),
        )
        if not ok:
            print(
                f"cannot request changes for {tid}: {detail or 'invalid review state'}",
                file=sys.stderr,
            )
            return 1
        if worker_run is None:
            # Human lane: the implementer resumes from the thread, so the
            # verdict lands there as the rework comment (the run summary
            # carries it too).
            _unused_run, session_ref = safe_comment_provenance(tid)
            kb.add_comment(
                conn, tid, _profile_author(),
                (
                    f"changes requested (operator send-back, {operator}): "
                    if operator else "changes requested (human review lane): "
                )
                + str(kb.redact_review_value(reason)),
                run_id=(
                    held_run if held_run is not None
                    else getattr(kb.latest_run(conn, tid), "id", None)
                ),
                session_ref=session_ref,
            )
        print(
            f"Requested changes for {tid}"
            + (f"; routed to {detail}" if detail else "")
        )
    return 0


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    """Retired verb: a review returns to its implementer only via request-changes."""
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    with kb.connect_closing() as conn:
        for tid in ids:
            kb.reopen_review_task(conn, tid)
            print(
                f"cannot reopen {tid}: legacy bypass retired; claim review and use "
                f"request-changes with full coverage (operator send-back: "
                f"hermes kanban request-changes {tid} \"<reason>\" --operator \"<who: why>\")",
                file=sys.stderr,
            )
    return 1


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    # Dedupe while preserving order; positional task_id always first.
    ids = list(dict.fromkeys(_bulk_ids(args)))
    dry_run = bool(args.dry_run)

    results: list[dict[str, object]] = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(conn, tid, actor=author, reason=reason, dry_run=dry_run)
            results.append({"task_id": tid, "promoted": ok, "dry_run": dry_run,
                            "reason": reason, "error": err})

    failed = [r for r in results if not r["promoted"]]
    if getattr(args, "json", False):
        # Single-id stays a flat object for back-compat; bulk emits a list.
        _print_json(results[0] if len(results) == 1 else results)
        return 0 if not failed else 1

    tag = " (dry)" if dry_run else ""
    label = "Would promote" if dry_run else "Promoted"
    suffix = f": {reason}" if reason else ""
    for r in results:
        if r["promoted"]:
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
        return _err("choose either task_ids to archive or --rm archived task_ids")
    if not ids and not purge_ids:
        return _err("at least one task_id is required")
    with kbc.connect_closing() as conn:
        if purge_ids:
            return _bulk_apply(purge_ids, lambda tid: kb.delete_archived_task(conn, tid), lambda tid: f"Deleted {tid}",
                               lambda tid: f"cannot delete {tid} (must already be archived)")

        failed = False
        for tid in ids:
            try:
                archived = kb.archive_task(conn, tid)
            except ClosedUnmergedPrError as closed_err:
                failed = True
                print(f"cannot archive {tid}: {closed_err}", file=sys.stderr)
                continue
            if not archived:
                failed = True
                print(f"cannot archive {tid}", file=sys.stderr)
            else:
                print(f"Archived {tid}")
        return 1 if failed else 0


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


def _cmd_session_closeout(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_session_closeout

    return kanban_session_closeout.run(args)


def _cmd_home_index(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_home_index

    check = bool(getattr(args, "check", False))
    report = kanban_home_index.resync(check_only=check)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        mode = "check" if check else "sync"
        print(
            f"home-index {mode}: boards={report['boards']} rows={report['rows']} "
            f"drift={report['drift']} (missing={report['missing']} "
            f"extra={report['extra']} stale={report['stale']}) "
            f"errors={len(report['errors'])} path={kanban_home_index.index_path()}"
        )
        for line in report["samples"]:
            print(f"  {line}")
        for err in report["errors"]:
            print(f"  error: {err}")
    if report["errors"]:
        return 2
    return 1 if (check and report["drift"]) else 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        stats = kb.board_stats(conn)
        stats["review_awaiting_human"] = kb.review_awaiting_human(conn)
    if _json_out(args, stats):
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
            print(f"  {who:20s}  {_fmt_counts(counts)}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    line = kb.format_review_awaiting_human(stats.get("review_awaiting_human") or [])
    if line:
        print(f"\n{line}")
    return 0


def rehome_apply(conn, task_id: str, sid: str, row: Optional[dict], target) -> bool:
    """Stamp ``sid`` as the card's home and subscribe its chat (``target``, a
    ``HomeTarget``). Shared by ``rehome`` and the gateway's orphan-menu reply
    (t_6281f908). False = unknown id."""
    if not kb.set_task_session(conn, task_id, sid):
        return False
    try:
        origin = json.loads((row or {}).get("origin_json") or "{}")
    except (TypeError, ValueError):
        origin = {}
    origin = origin if isinstance(origin, dict) else {}
    kbn.add_notify_sub(
        conn, task_id=task_id,
        platform=target.platform, chat_id=target.chat_id,
        thread_id=target.thread_id or None,
        chat_type=origin.get("chat_type") or None,
        user_id=origin.get("user_id") or None,
        scope_id=origin.get("scope_id") or None,
        notifier_profile=origin.get("profile") or _profile_author(),
    )
    return True


def _cmd_rehome(args: argparse.Namespace) -> int:
    """Re-home a card (t_808bc8e6): stamp ``--session`` as its home and
    subscribe that session's origin chat. The orphan alert names this verb."""
    from gateway.kanban_home_route import SessionLookupError, home_from_row, read_session_row

    sid = (args.session or "").strip()
    if not sid:
        print("kanban rehome: --session is empty", file=sys.stderr)
        return 2
    try:
        row = None if kb.is_operator_home(sid) else read_session_row(sid)
    except SessionLookupError as exc:
        print(f"kanban rehome: {exc}", file=sys.stderr)
        return 1
    home = home_from_row(sid, row)
    if home.target is None:
        print(
            f"kanban rehome: session {sid} has no chat to route to ({home.reason}); "
            "pass the session the card is discussed in",
            file=sys.stderr,
        )
        return 1
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        if not rehome_apply(conn, args.task_id, sid, row, home.target):
            print(f"cannot rehome {args.task_id} (unknown id)", file=sys.stderr)
            return 1
        target = home.target
    print(f"Re-homed {args.task_id} to session {sid} "
          f"({target.platform}:{target.chat_id}"
          + (f":{target.thread_id}" if target.thread_id else "") + ")")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    delivery_metadata = {
        key: value
        for key, value in (
            ("parent_chat_id", getattr(args, "parent_chat_id", None)),
            ("guild_id", getattr(args, "guild_id", None)),
        )
        if value
    }
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        outcome = kbn.add_notify_sub(
            conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type, thread_id=args.thread_id, user_id=args.user_id,
            user_id_alt=getattr(args, "user_id_alt", None),
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=(
                getattr(args, "delivery_mode", None)
                or ("notify+wake" if getattr(args, "wake", False) else None)
            ),
            delivery_metadata=delivery_metadata or None,
            also=bool(getattr(args, "also", False)),
            takeover=bool(getattr(args, "takeover", False)),
        )
        waker = kbn.card_waker(conn, args.task_id)
        wants_wake = (
            getattr(args, "delivery_mode", None) in kbn.NOTIFY_WAKE_MODES
            or getattr(args, "wake", False)
        )
        thread = args.thread_id or ""
        if wants_wake and outcome != "kept" and waker is not None and not (
            waker["platform"] == args.platform and waker["chat_id"] == args.chat_id
            and (waker.get("thread_id") or "") == thread
        ):
            print(f"wake held by {waker['platform']}:{waker['chat_id']}"
                  + (f" (profile {waker['notifier_profile']})"
                     if waker.get("notifier_profile") else "")
                  + f"; {args.platform}:{args.chat_id} subscribed notify. "
                  "Pass --takeover to move the wake.")
        if outcome == "kept":
            owners = [
                s for s in kbn.list_notify_subs(conn, args.task_id)
                if str(s.get("platform") or "").lower() == args.platform.lower()
            ]
            owner = owners[0] if owners else {}
            print(f"subscription kept: {owner.get('platform')}:{owner.get('chat_id')} "
                  f"owns {args.task_id} (live session); not subscribing "
                  f"{args.platform}:{args.chat_id}. Pass --also to add a second chat.")
            return 0
    verb = "Re-homed" if outcome == "rehomed" else "Subscribed"
    print(f"{verb} {args.platform}:{args.chat_id}" + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, args.task_id)
    if _json_out(args, subs):
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        dmode, ctype = s.get("delivery_mode") or "notify", s.get("chat_type") or "dm"
        extras = "".join((
            f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else "",
            "" if ctype == "dm" else f"  chat_type={ctype}",
            f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else "",
            "" if dmode == "notify" else f"  mode={dmode}",
            "  [waker]" if dmode in kbn.NOTIFY_WAKE_MODES and s.get("platform") != "api_server" else "",
        ))
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}  (since event {s['last_event_id']}){extras}")
    return 0


def _cmd_notify_status(args: argparse.Namespace) -> int:
    """Current wake-gate mode as last written by the delivering gateway."""
    from hermes_cli import kanban_wake_gate as _kwg

    state = _kwg.read_state()
    if getattr(args, "json", False):
        print(json.dumps(state or {}, indent=2, sort_keys=True))
        return 0 if state else 1
    if not state:
        print(f"wake gate: no state at {_kwg.state_path()} "
              "(no gateway has delivered a wake since this build loaded)")
        return 1
    age = int(time.time()) - int(state.get("updated_at") or 0)
    load1 = state.get("load1")
    load = f"{load1:.1f}" if isinstance(load1, (int, float)) else "?"
    print(f"wake gate: mode={state.get('mode')}  load1={load}  "
          f"pause_above={state.get('pause_above')}  "
          f"resume_below={state.get('resume_below')}  (updated {age}s ago)")
    for prof, reason in (state.get("lanes_capped") or {}).items():
        print(f"  {prof}: {reason} -> wakes sent as notify")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbn.remove_notify_sub(conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
                                  thread_id=args.thread_id)
    return _ok_or_err(ok, "(no such subscription)", f"Unsubscribed from {args.task_id}")


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
    if getattr(args, "dedupe", False):
        return _cmd_notify_repair_dedupe(args)
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


def _cmd_notify_repair_dedupe(args: argparse.Namespace) -> int:
    """``notify-repair --dedupe [--apply] [--all-boards] [--json]`` (t_484a3c72).

    One subscriber chat per card and platform. Lists every card with more
    than one; keeps the home-session chat (else the oldest row) and drops the
    rest when ``--apply`` is given.
    """
    apply = bool(getattr(args, "apply", False))
    if getattr(args, "all_boards", False):
        slugs = [
            str(m.get("slug") or "").strip()
            for m in kb.enumerating_each(kb.list_boards())
        ]
        slugs = [s for s in slugs if s]
    else:
        slugs = [None]
    rows: list[dict] = []
    # A board that could not be scanned is a FAILURE, not an all-clear: never
    # print "every card has one subscriber" or exit 0 over it (Prism P1
    # cf49dc4f0623, t_030662ba). The remaining boards are still scanned.
    failed: list[dict] = []
    for slug in slugs:
        try:
            ctx = (kb.connect_closing(board=slug) if slug is not None
                   else kb.connect_closing())
            with ctx as conn:
                board_rows = kb.dedupe_notify_subs(conn, apply=apply)
        except Exception as exc:
            failed.append({"board": slug or "default",
                           "error": f"{type(exc).__name__}: {exc}"})
            print(f"notify-repair --dedupe: board {slug or 'default'!r} FAILED — "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for r in board_rows:
            if slug is not None:
                r["board"] = slug
            rows.append(r)
    rc = 1 if failed else 0
    dropped = sum(len(r["dropped"]) for r in rows)
    if getattr(args, "json", False):
        print(json.dumps({"apply": apply, "cards": len(rows), "dropped": dropped,
                          "failed": failed, "rows": rows},
                         indent=2, ensure_ascii=False))
        return rc
    if failed:
        print(f"notify-repair --dedupe: {len(failed)} board(s) FAILED to scan: "
              + ", ".join(f["board"] for f in failed))
    if not rows:
        if not failed:
            print("notify-repair --dedupe: every card has one subscriber chat per platform.")
        return rc
    verb = "dropped" if apply else "would drop"
    print(f"notify-repair --dedupe: {len(rows)} card(s) with >1 subscriber chat; "
          f"{verb} {dropped} subscription(s)" + ("" if apply else " (dry run; --apply to write)"))
    for r in rows:
        board = f"[{r['board']}] " if r.get("board") else ""
        gone = ", ".join(f"{d['platform']}:{d['chat_id']}" for d in r["dropped"])
        print(f"  {board}{r['task_id']}  keep {r['kept']['platform']}:{r['kept']['chat_id']}"
              f" ({r['reason']})  {verb} {gone}")
        return rc


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        return _err(f"(no log for {args.task_id} — task may not have spawned yet)")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk, rc = _run_state_kwargs(args, "runs")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if _json_out(args, [_obj_dict(r, _RUNS_RUN_FIELDS) for r in runs]):
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        el = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m" if elapsed < 3600 else f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            print(f"     → {r.summary.splitlines()[0][:100]}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _run_triage_sweep(args: argparse.Namespace, verb: str, mod, run_one, json_key: str,
                      json_fields: tuple[str, ...], human_ok) -> int:
    """Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
    ``run_one(tid, author=...)`` per id, print JSON or human lines, exit code."""
    all_flag = bool(getattr(args, "all_triage", False))
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))
    tenant = getattr(args, "tenant", None)
    if args.task_id and all_flag:
        return _err("kanban: pass either a task id OR --all, not both", 2)
    if all_flag:
        ids = mod.list_triage_ids(tenant=tenant)
        if not ids:
            if want_json:
                print(json.dumps({json_key: 0, "total": 0}))
            else:
                print("No triage tasks" + (f" for tenant {tenant!r}" if tenant else "") + ".")
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        return _err(f"kanban: {verb} requires a task id or --all", 2)

    ok_count = 0
    for tid in ids:
        try:
            outcome = run_one(tid, author=author)
        except kb.ForeignSessionMutationError as exc:
            outcome = mod.SpecifyOutcome(tid, False, str(exc)) if hasattr(mod, "SpecifyOutcome") \
                else mod.DecomposeOutcome(tid, False, str(exc))
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps(_obj_dict(outcome, json_fields)))
        elif outcome.ok:
            print(human_ok(outcome))
        else:
            print(f"kanban: {verb} {outcome.task_id}: {outcome.reason}", file=sys.stderr)
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: exit 1 only when every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _retitled_suffix(outcome) -> str:
    return f" — retitled: {outcome.new_title!r}" if outcome.new_title else ""


def _cmd_specify(args: argparse.Namespace) -> int:
    """Spec a triage task (or all) via the auxiliary LLM, promote to todo."""
    from hermes_cli import kanban_specify as spec

    return _run_triage_sweep(args, "specify", spec, spec.specify_task, "specified",
                             ("task_id", "ok", "reason", "new_title"),
                             lambda o: f"Specified {o.task_id} → todo{_retitled_suffix(o)}")


def _decompose_ok_line(o) -> str:
    if o.fanout and o.child_ids:
        return (f"Decomposed {o.task_id} → {len(o.child_ids)} "
                f"children ({', '.join(o.child_ids)}); root promoted to todo")
    return f"Specified {o.task_id} → todo (no fanout){_retitled_suffix(o)}"


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all) out into child tasks via the auxiliary LLM."""
    from hermes_cli import kanban_decompose as decomp

    return _run_triage_sweep(args, "decompose", decomp, decomp.decompose_task, "decomposed",
                             ("task_id", "ok", "reason", "fanout", "child_ids", "new_title"), _decompose_ok_line)


_HANDLERS = {
    "init": _cmd_init, "create": _cmd_create, "budget": _cmd_budget, "swarm": _cmd_swarm,
    "list": _cmd_list, "ls": _cmd_list, "show": _cmd_show,
    "assign": _cmd_assign, "set-model": _cmd_set_model, "priority": _cmd_priority,
    "lane-model": _cmd_lane_model, "pins": _cmd_pins,
    "reclaim": _cmd_reclaim, "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics, "diag": _cmd_diagnostics,
    "link": _cmd_link, "rehome": _cmd_rehome, "unlink": _cmd_unlink, "claim": _cmd_claim,
    "comment": _cmd_comment, "attach": _cmd_attach,
    "attachments": _cmd_attachments, "attach-rm": _cmd_attach_rm,
    "complete": _cmd_complete, "edit": _cmd_edit, "update": _cmd_edit, "block": _cmd_block,
    "schedule": _cmd_schedule, "unblock": _cmd_unblock,
    "requeue": _cmd_requeue, "workspace": _cmd_workspace, "reopen": _cmd_reopen,
    "request-review": _cmd_request_review, "request-changes": _cmd_request_changes,
    "reopen-review": _cmd_reopen_review, "promote": _cmd_promote,
    "triage-resolve": _cmd_triage_resolve,
    "archive": _cmd_archive, "tail": _cmd_tail, "dispatch": _cmd_dispatch,
    "daemon": _cmd_daemon, "watch": _cmd_watch, "stats": _cmd_stats,
    "home-index": _cmd_home_index, "session-closeout": _cmd_session_closeout,
    "log": _cmd_log, "runs": _cmd_runs, "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees, "notify-subscribe": _cmd_notify_subscribe,
    "notify-list": _cmd_notify_list, "notify-status": _cmd_notify_status,
    "notify-unsubscribe": _cmd_notify_unsubscribe,
    "notify-repair": _cmd_notify_repair,
    "context": _cmd_context, "specify": _cmd_specify, "decompose": _cmd_decompose,
    "gc": _cmd_gc, "home-lint": _cmd_home_lint,
}


# --- Slash-command entry point (used by /kanban from CLI and gateway) ---

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
    # A typed CLI/TUI ``/kanban`` is hand-typed (D-O2, t_6281f908); the
    # gateway's in-process call is classified as ``gateway`` before this.
    hand = kb.HAND_TYPED_SLASH.set(True)
    try:
        return _run_slash(rest)
    finally:
        kb.HAND_TYPED_SLASH.reset(hand)
        _SLASH_SESSION_ID.reset(token)


def _run_slash(rest: str) -> str:
    import io

    # Non-posix split (Windows) keeps backslashes as path separators but
    # leaves quote characters in the tokens — strip a fully wrapping pair
    # so `"my task"` reaches argparse as `my task`, not `"my task"`.
    tokens = []
    if rest and rest.strip():
        for tok in shlex.split(rest, posix=os.name == "posix"):
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            tokens.append(tok)

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP
    # build_parser() needs a subparsers action to attach to: build a throwaway one and drive
    # kanban_parser directly so usage/error text reads ``/kanban``.
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    kanban_parser = build_parser(_wrap.add_subparsers(dest="_top"))
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    subparsers = [a for a in kanban_parser._actions if isinstance(a, argparse._SubParsersAction)]
    for _action in subparsers:
        for _name, _choice in _action.choices.items():
            _choice.prog = f"/kanban {_name}"
            _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in subparsers:
                subparser = _action.choices.get(tokens[0])
                if subparser is not None:
                    return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
        if exc.code in {0, None} and out:  # ``-h`` help dump
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

    out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
