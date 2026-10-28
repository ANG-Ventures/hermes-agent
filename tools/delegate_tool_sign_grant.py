"""``delegate_task(sign_grant=...)``: mint a scoped board-signing grant for a child (t_19c567a8).

The parent delegates a GRANT, not its identity (hermes_cli/kanban_sign_grants.py has the
design). This module is the spawn half: it checks what only the parent process can see (it
holds ``by`` itself, it is not a dispatched worker or a delegate child, the child inherited
its context and runs on its model), mints the board rows bound to the child's session, tells
the child how to sign and ledgers the grant. The grant is revoked when the PARENT's turn ends
(``agent/turn_finalizer.py`` -> :func:`revoke_turn_grants`) or at its expiry, whichever is first:
a grant never outlives the parent turn, and its stamps are void from then on.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SignGrantError(ValueError):
    """The spawn's ``sign_grant`` was refused; no child is started."""


def carries_sign_grant(args: Any) -> bool:
    """A model-issued delegate_task with a top-level or per-task ``sign_grant``. Such a call runs SYNCHRONOUSLY on
    the model dispatch paths: the grant ends with the parent's turn, so the child must finish inside it."""
    if not isinstance(args, dict):
        return False
    if args.get("sign_grant"):
        return True
    tasks = args.get("tasks")
    return isinstance(tasks, list) and any(isinstance(t, dict) and t.get("sign_grant") for t in tasks)


def _spec(raw: Any) -> Optional[Dict[str, Any]]:
    if raw in (None, {}, ""):
        return None
    if not isinstance(raw, dict):
        raise SignGrantError("sign_grant must be an object {cards, verbs, expires_in_min?, by?}")
    return raw


def _caller_may_grant(by: str) -> None:
    from agent.delegation_context import is_delegated_child_process_context
    from hermes_cli.kanban_identity import is_operator_label, operator_label_proven, worker_ancestor_profile

    if is_delegated_child_process_context():
        raise SignGrantError("a delegate child cannot pass on a signing grant")
    if os.environ.get("HERMES_KANBAN_TASK") or worker_ancestor_profile():
        raise SignGrantError("a dispatched kanban worker cannot mint a signing grant")
    if not is_operator_label(by):
        raise SignGrantError(f"sign_grant.by {by!r} is not an operator identity")
    if not operator_label_proven(by):
        raise SignGrantError(f"this process cannot prove it is {by!r} (operator token or its gateway)")


def _session_inherited(task: Dict[str, Any], inherit_context: Any, inherit_mode: Any) -> bool:
    from tools.delegate_tool_fork import resolve_inherit_mode

    ic = task.get("inherit_context") if "inherit_context" in task else inherit_context
    im = task.get("inherit_mode") if "inherit_mode" in task else inherit_mode
    return resolve_inherit_mode(ic, im) is not None


def mint_for_child(task: Dict[str, Any], child: Any, parent_agent: Any, *,
                   inherit_context: Any = None, inherit_mode: Any = None) -> Optional[Dict[str, Any]]:
    """Mint ``task['sign_grant']`` for ``child``; append the signing brief to ``task['goal']``.

    Returns the grant dict (``grant_id``, ``board``, ``cards`` ...) or None when the task
    carries no grant. Raises :class:`SignGrantError` on any refusal.
    """
    spec = _spec(task.get("sign_grant"))
    if spec is None:
        return None
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_sign_grants as ksg
    from hermes_cli.kanban_db_board_registry import board_of_task
    from hermes_cli.profiles import current_profile_name

    by = str(spec.get("by") or "apollo").strip()
    _caller_may_grant(by)
    if not _session_inherited(task, inherit_context, inherit_mode):
        raise SignGrantError("a signing grant needs inherit_context=true (the child must carry the rulings)")
    if getattr(parent_agent, "_delegate_depth", 0) > 0:
        raise SignGrantError("only a top-level session may mint a signing grant")
    cards = [str(c).strip() for c in spec.get("cards") or () if str(c).strip()]
    if not cards:
        raise SignGrantError("sign_grant.cards is empty")
    try:
        boards = {board_of_task(c) for c in cards}
    except Exception as exc:
        raise SignGrantError(f"sign_grant cards: {exc}") from exc
    if len(boards) != 1:
        raise SignGrantError(f"sign_grant cards span boards {sorted(boards)}; one grant covers one board")
    board = boards.pop()
    child_sid = str(getattr(child, "session_id", "") or "")
    parent_sid = str(getattr(parent_agent, "session_id", "") or "")
    profiles = {current_profile_name("default") or "default", by}
    try:
        with kb.connect_closing(board=board) as conn:
            grant = ksg.mint(
                conn, cards=cards, verbs=spec.get("verbs") or (), by=by,
                child_session_ref=kb.derive_session_ref(child_sid) or "",
                parent_session_ref=kb.derive_session_ref(parent_sid),
                parent_model=str(getattr(parent_agent, "model", "") or ""),
                child_model=str(getattr(child, "model", "") or ""),
                child_id=getattr(child, "_subagent_id", None),
                expires_in_min=spec.get("expires_in_min"),
                parent_profiles=profiles,
            )
    except ksg.GrantRefused as exc:
        raise SignGrantError(str(exc)) from exc
    grant["board"] = board
    task["goal"] = str(task.get("goal") or "") + _brief(grant)
    _ledger({"event": "sign_grant", "grant_id": grant["grant_id"], "child": grant.get("child_id"),
             "by": by, "board": board, "cards": len(cards), "verbs": grant["verbs"],
             "expires_at": grant["expires_at"], "at": int(time.time())})
    return grant


def _brief(grant: Dict[str, Any]) -> str:
    cards = " ".join(grant["cards"])
    return (
        f"\n\n[signing grant {grant['grant_id']}] You may sign board comments as `{grant['by']}` on "
        f"card(s) {cards} (board {grant['board']}) for verbs {', '.join(grant['verbs'])}, until "
        f"{time.strftime('%H:%M %Z', time.localtime(grant['expires_at']))}. Sign with: "
        f"`hermes kanban --board {grant['board']} comment <card> --author {grant['by']} "
        f"--grant {grant['grant_id']} --body-file <file>`; an APPROVE stamp goes through the one writer: "
        f"`python3 scripts/lib/operator_approve.py --card <card> --head <40hex> --pr <owner/repo#N> "
        f"--by {grant['by']} --grant {grant['grant_id']}` (needs verbs approve + release). Any other card, verb, or a PR "
        "your parent's runs authored is refused; the grant ends with your parent's turn."
    )


def register_turn_grants(parent_agent: Any, grants: List[Dict[str, Any]]) -> None:
    """Hand ``grants`` to the parent agent so its turn end revokes them."""
    if not grants:
        return
    held = parent_agent.__dict__.setdefault("_sign_grants", [])
    held.extend(grants)


def revoke_turn_grants(agent: Any) -> None:
    """Turn end: revoke every grant this agent minted during the turn."""
    held = getattr(agent, "__dict__", {}).pop("_sign_grants", None)
    if held:
        revoke(held)


def revoke(grants: List[Dict[str, Any]]) -> None:
    """Revoke every grant in ``grants`` (the child's run ended). Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_sign_grants as ksg

    for g in grants or ():
        try:
            with kb.connect_closing(board=g["board"]) as conn:
                ksg.revoke(conn, g["grant_id"])
            _ledger({"event": "sign_grant_revoke", "grant_id": g["grant_id"], "at": int(time.time())})
        except Exception:
            # Expiry still bounds it; say so loudly.
            logger.warning("sign grant %s could not be revoked; it lapses at its expiry",
                           g.get("grant_id"), exc_info=True)


def _ledger(event: Dict[str, Any]) -> None:
    try:
        from gateway.kanban_boomerang_wake import ledger_append

        ledger_append(event)
    except Exception:
        logger.debug("sign grant ledger append failed", exc_info=True)
