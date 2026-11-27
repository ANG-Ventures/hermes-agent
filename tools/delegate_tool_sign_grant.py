"""``delegate_task(sign_grant=...)``: mint a scoped board-signing grant for a child (t_19c567a8).

The parent delegates a GRANT, not its identity (hermes_cli/kanban_sign_grants.py has the
design). This module is the spawn half: it checks what only the parent process can see (it
holds ``by`` itself, it is not a dispatched worker or a delegate child, the child inherited
its context and runs on its model), mints the board rows bound to the child's session, tells
the child how to sign and ledgers the grant. A FOREGROUND grant is revoked when the PARENT's turn ends
(``agent/turn_facade.py``, the turn's outer finally -> :func:`revoke_turn_grants`) or at its expiry, whichever is first.
A BACKGROUND grant (t_4548202f) belongs to its batch instead: :func:`track_batch_grants` records it in this process's
owner file and the batch's own end decides it per child (:func:`end_batch_grants`): a child that returned
``completed`` CLOSES its grant (no new write; what it signed stays valid until ``expires_at``, so land-on-approve and
the parent's fold can still read the stamp), anything else REVOKES it (error, timeout, interrupt, cancel/stale
interrupt, a refused admission, a dispatch exception), and a gateway start revokes every grant whose owner process is
gone (:func:`sweep_orphaned_batch_grants`). A revoked grant's stamps are void from revocation on; a closed grant's
stamps are void from its expiry on, or from a later revoke.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SignGrantError(ValueError):
    """The spawn's ``sign_grant`` was refused; no child is started."""


def carries_sign_grant(args: Any) -> bool:
    """A model-issued delegate_task with a top-level or per-task ``sign_grant``. Such a call runs in the
    FOREGROUND on the model dispatch paths unless it passes ``background: true`` (t_4548202f)."""
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
    # operator_label_proven's gateway-ancestry arm is PROCESS-scoped: under the multiplexer every
    # served profile's session descends from the gateway that serves default, so it would let a
    # daedalus session mint apollo authority (Prism 08a8789d8e59). The identity a session may hand
    # on is its OWN profile's, resolved per session; only a presented operator token speaks for any.
    from hermes_cli import kanban_db as kb

    if kb._operator_token_state() != "ok":
        own = _session_identities()
        if by.lower() not in own:
            raise SignGrantError(f"sign_grant.by {by!r} is not this session's identity (profile "
                                 f"{sorted(own)[0] if own else '?'!r}); only the operator token signs for another")
    if not operator_label_proven(by):
        raise SignGrantError(f"this process cannot prove it is {by!r} (operator token or its gateway)")


def _session_identities() -> frozenset:
    """Labels the CURRENT session's profile signs as: its profile name, and for the root profile
    (Apollo runs as ``default``) both root labels. ``current_profile_name`` reads the per-session
    HERMES_HOME override first, so a served profile answers for itself, never for the host."""
    from hermes_cli.kanban_identity import _ROOT_PROFILE_LABELS
    from hermes_cli.profiles import current_profile_name

    own = str(current_profile_name("default") or "default").strip().lower()
    return _ROOT_PROFILE_LABELS if own in _ROOT_PROFILE_LABELS else frozenset({own})


def _session_inherited(task: Dict[str, Any], inherit_context: Any, inherit_mode: Any) -> bool:
    from tools.delegate_tool_fork import resolve_inherit_mode

    ic = task.get("inherit_context") if "inherit_context" in task else inherit_context
    im = task.get("inherit_mode") if "inherit_mode" in task else inherit_mode
    return resolve_inherit_mode(ic, im) is not None


#: The task's goal before the signing brief was appended: the durable restart intent persists this one, so a
#: relaunched generation is never told it holds a grant (t_4548202f).
GOAL_BEFORE_GRANT = "_goal_before_grant"


def mint_for_child(task: Dict[str, Any], child: Any, parent_agent: Any, *,
                   inherit_context: Any = None, inherit_mode: Any = None,
                   batch_bound: bool = False) -> Optional[Dict[str, Any]]:
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
    if not getattr(child, "_context_inherited", None):
        # Requested is not delivered: a failed or empty summary fold builds the child without the transcript.
        raise SignGrantError("inherit_context was requested but the child was built without the parent's "
                             "transcript (summary fold failed or empty; see the 'inherit_context fold' log)")
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
    # The model the parent's delegating call was DISPATCHED on (stamped at the dispatch edge, the
    # source revoke_on_model_drift reads), not agent.model: middleware can send another model
    # without changing it (Prism c37b9eed0ae3). Unknown means unbindable: refuse.
    parent_model = str(getattr(parent_agent, "_dispatched_model", "") or "")
    if not parent_model:
        raise SignGrantError("the parent's dispatched model is unknown; a grant binds the model that ran the delegation")
    child_sid = str(getattr(child, "session_id", "") or "")
    parent_sid = str(getattr(parent_agent, "session_id", "") or "")
    profiles = {current_profile_name("default") or "default", by}
    try:
        with kb.connect_closing(board=board) as conn:
            grant = ksg.mint(
                conn, cards=cards, verbs=spec.get("verbs") or (), by=by,
                child_session_ref=kb.derive_session_ref(child_sid) or "",
                parent_session_ref=kb.derive_session_ref(parent_sid),
                parent_model=parent_model,
                child_model=str(getattr(child, "model", "") or ""),
                child_id=getattr(child, "_subagent_id", None),
                expires_in_min=spec.get("expires_in_min"),
                parent_profiles=profiles,
            )
    except ksg.GrantRefused as exc:
        raise SignGrantError(str(exc)) from exc
    grant["board"] = board
    try:
        # The child carries its grants so a later model swap revokes them (revoke_on_model_drift).
        child.__dict__.setdefault("_held_sign_grants", []).append(dict(grant))
        task.setdefault(GOAL_BEFORE_GRANT, task.get("goal"))
        task["goal"] = str(task.get("goal") or "") + _brief(grant, batch_bound=batch_bound)
        _ledger({"event": "sign_grant", "grant_id": grant["grant_id"], "child": grant.get("child_id"),
                 "by": by, "board": board, "cards": len(cards), "verbs": grant["verbs"],
                 "expires_at": grant["expires_at"], "at": int(time.time())})
    except BaseException:
        # Minted but never handed back: nothing may hold it live. A failed revoke goes to the parent's
        # turn registry so revoke_turn_grants retries it (Prism 9c236305da52).
        if revoke([grant]):
            register_turn_grants(parent_agent, [grant])
        raise
    return grant


def mint_for_children(children: List[Any], parent_agent: Any, *, inherit_context: Any = None,
                      inherit_mode: Any = None, batch_bound: bool = False) -> List[Dict[str, Any]]:
    """Mint every child's grant, all or nothing: on ANY exception (a refusal, an error, an
    interrupt) the grants already minted are revoked before it propagates (Prism cd87b39be0ea)."""
    minted: List[Dict[str, Any]] = []
    try:
        for _i, t, child in children:
            g = mint_for_child(t, child, parent_agent, inherit_context=inherit_context, inherit_mode=inherit_mode,
                               batch_bound=batch_bound)
            if g:
                g["task_index"] = _i  # the batch end decides close vs revoke from THIS child's result
                minted.append(g)
    except BaseException:
        failed = set(revoke(minted))
        register_turn_grants(parent_agent, [g for g in minted if str(g.get("grant_id")) in failed])
        raise
    return minted


def _brief(grant: Dict[str, Any], *, batch_bound: bool = False) -> str:
    cards = " ".join(grant["cards"])
    return (
        f"\n\n[signing grant {grant['grant_id']}] You may sign board comments as `{grant['by']}` on "
        f"card(s) {cards} (board {grant['board']}) for verbs {', '.join(grant['verbs'])}, until "
        f"{time.strftime('%H:%M %Z', time.localtime(grant['expires_at']))}. Sign with: "
        f"`hermes kanban --board {grant['board']} comment <card> --author {grant['by']} "
        f"--grant {grant['grant_id']} --body-file <file>`. A verdict or APPROVE is that same command with a body "
        f"carrying a `review_coverage: {{...}}` line (verdict, head_sha, pr, by={grant['by']}); APPROVE needs verb "
        "approve, and a close-on line or close_on field needs verb release. The merge-gate readers that honour a live "
        "granted row live in hermes-home (home#7376, home#7570). Any other card, verb, or a PR your parent's runs "
        "authored is refused, and so is a write from any session but yours; the grant ends with "
        + ("this delegation batch, and what you signed under it stays valid until that expiry time."
           if batch_bound else "your parent's turn.")
    )


def register_turn_grants(parent_agent: Any, grants: List[Dict[str, Any]]) -> None:
    """Hand ``grants`` to the parent agent so its turn end revokes them."""
    if not grants:
        return
    held = parent_agent.__dict__.setdefault("_sign_grants", [])
    held.extend(grants)


def revoke_turn_grants(agent: Any) -> None:
    """Turn end: revoke every grant this agent minted during the turn. Never raises.

    It runs from the turn's outer finally, so raising there would replace a finished turn's
    result or mask its exception (Prism 9c5349bddc34). A grant leaves the registry only once
    revoked: a failed set is retried once, and whatever still fails is logged (one line naming
    the ids), marked ``revoke_failed`` and stays registered so the next turn end retries it
    (Prism 53347c16a468). Its expiry bounds it meanwhile: a shortened-TTL leak, not new authority."""
    d = getattr(agent, "__dict__", None)
    held = d.get("_sign_grants") if d is not None else None
    if not held:
        return
    failed = set(revoke(held))
    if failed:
        failed = set(revoke([g for g in held if str(g.get("grant_id")) in failed]))
    remaining = [g for g in held if str(g.get("grant_id")) in failed]
    if not remaining:
        d.pop("_sign_grants", None)
        return
    for g in remaining:
        g["revoke_failed"] = True
    d["_sign_grants"] = remaining
    logger.error("sign grant(s) %s could not be revoked at turn end; marked revoke_failed, the next turn end "
                 "retries them and they lapse at their expiry at the latest", sorted(failed))


# ── Background batches (t_4548202f) ─────────────────────────────────────────────────────────────────────
# A background grant outlives the parent turn but never its batch. The batch runs in THIS process (delegate_tool keeps
# a granted unit off the kanban and detached runtimes, whose children have other sessions), so the owner of record is
# this process: one JSON file per process under the shared kanban root, named by its pid AND its epoch creation time
# (psutil, so a reused pid, also after a host reboot, never names the same file) and written only by that process.
# A gateway start revokes the grants of every file whose process is gone; a dead process never comes back, so the
# sweeper is the only writer left for its file. A relaunched generation is rebuilt without its grant.
_BATCH_LOCK = threading.Lock()
_CREATE_TIME_TOLERANCE_S = 2.0


def _batch_dir():
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "sign_grants" / "batches"


def _process_created(pid: int) -> Optional[float]:
    """Epoch creation time of ``pid`` (None when it does not exist)."""
    import psutil

    try:
        return float(psutil.Process(int(pid)).create_time())
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError, psutil.AccessDenied):
        # AccessDenied: the pid now belongs to a process we may not read, so it is not our gateway.
        return None


def _owner_alive(pid: Any, created: Any) -> bool:
    if not pid or created is None:
        return False
    now = _process_created(int(pid))
    return now is not None and abs(now - float(created)) <= _CREATE_TIME_TOLERANCE_S


def _own_path():
    pid = os.getpid()
    created = _process_created(pid)
    if created is None:
        raise RuntimeError("this process's creation time is unreadable")
    # Millisecond precision: a pid reused within the same second still names another file (Prism a6eae8ca8e9c).
    return _batch_dir() / f"{pid}-{int(created * 1000)}.json", pid, created


def _read_owner(path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("sign grant owner file %s is unreadable", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _write_owner(path, data: Dict[str, Any]) -> None:
    from utils import atomic_json_write

    if data.get("grants"):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def track_batch_grants(grants: List[Dict[str, Any]], *, delegation_id: Optional[str]) -> None:
    """Record ``grants`` as owned by a background batch of this process. Raises when the record cannot be written:
    a grant the restart sweep cannot find would outlive a dead batch, so the caller revokes and refuses."""
    if not grants:
        return
    path, pid, created = _own_path()
    with _BATCH_LOCK:
        data = _read_owner(path) or {"pid": pid, "created": created, "grants": []}
        data["grants"] = list(data.get("grants") or ()) + [
            {"grant_id": g["grant_id"], "board": g["board"], "delegation_id": delegation_id,
             "expires_at": g.get("expires_at")} for g in grants]
        _write_owner(path, data)


def untrack_batch_grants(grant_ids: set) -> None:
    """Drop ``grant_ids`` from this process's owner record (they are revoked, or the turn owns them now)."""
    path, _pid, _created = _own_path()
    with _BATCH_LOCK:
        data = _read_owner(path)
        if not data:
            return
        data["grants"] = [g for g in data.get("grants") or () if str(g.get("grant_id")) not in grant_ids]
        _write_owner(path, data)


def end_batch_grants(grants: List[Dict[str, Any]], results: Optional[List[Dict[str, Any]]] = None) -> None:
    """Batch end, decided per child: a grant whose child's entry in ``results`` has ``status == "completed"`` is
    CLOSED (no new write; its rows stand until expiry); every other grant is REVOKED (rows void now). ``results``
    None (cancel, stale interrupt, a refused admission, a dispatch exception, a runner that raised) revokes all.
    Each ended grant leaves the owner file. Never raises; idempotent. A close that fails falls back to a revoke; a
    grant whose revoke fails twice stays in the owner file, so the next gateway start retries it; its expiry bounds
    it meanwhile."""
    if not grants:
        return
    try:
        done = {r.get("task_index") for r in results or () if isinstance(r, dict) and r.get("status") == "completed"}
        closing = [g for g in grants if results is not None and g.get("task_index") in done]
        revoking = [g for g in grants if g not in closing]
        not_closed = set(close(closing))
        revoking += [g for g in closing if str(g.get("grant_id")) in not_closed]
        failed = set(revoke(revoking))
        if failed:
            failed = set(revoke([g for g in revoking if str(g.get("grant_id")) in failed]))
        untrack_batch_grants({str(g.get("grant_id")) for g in grants} - failed)
        if failed:
            logger.error("sign grant(s) %s could not be revoked at batch end; the next gateway start retries them "
                         "and they lapse at their expiry at the latest", sorted(failed))
    except Exception:
        logger.error("sign grant batch-end close/revoke failed", exc_info=True)


def revoke_batch_grants(grants: List[Dict[str, Any]]) -> None:
    """Revoke every grant of a batch that did not end normally (see :func:`end_batch_grants`)."""
    end_batch_grants(grants, None)


def sweep_orphaned_batch_grants() -> int:
    """Gateway start: revoke every tracked grant whose owner process is not alive (a dead batch). Returns the number
    revoked. Never raises. A live owner (another gateway, a CLI session) keeps its grants."""
    revoked = 0
    try:
        root = _batch_dir()
        files = sorted(root.glob("*.json")) if root.is_dir() else []
    except Exception:
        logger.warning("sign grant orphan sweep could not list owner files", exc_info=True)
        return 0
    for path in files:
        try:
            with _BATCH_LOCK:
                data = _read_owner(path)
                if data is None:
                    continue
                pid, created = data.get("pid"), data.get("created")
                if _owner_alive(pid, created):
                    continue
                grants = [g for g in data.get("grants") or () if isinstance(g, dict) and g.get("grant_id")]
                failed = set(revoke(grants))
                data["grants"] = [g for g in grants if str(g.get("grant_id")) in failed]
                _write_owner(path, data)
            revoked += len(grants) - len(failed)
            _ledger({"event": "sign_grant_orphan_sweep", "owner_pid": pid, "revoked": len(grants) - len(failed),
                     "failed": sorted(failed), "at": int(time.time())})
            logger.info("sign grant orphan sweep: owner pid %s is gone; revoked %d grant(s)%s", pid,
                        len(grants) - len(failed), f", {len(failed)} failed (retried next start)" if failed else "")
        except Exception:
            logger.warning("sign grant orphan sweep failed for %s", path, exc_info=True)
    return revoked


def revoke_on_model_drift(agent: Any, model: Any) -> None:
    """Dispatch edge of a child's model call: a grant is minted for one model, so the first call
    on any other model (fallback activation, primary restore, a middleware reroute) revokes it
    before that model's output can sign anything (Prism 6b322f502e4d)."""
    held = getattr(agent, "__dict__", {}).get("_held_sign_grants")
    if not held:
        return
    from hermes_cli.kanban_sign_grants import _norm_model

    drifted = [g for g in held if _norm_model(g.get("model")) != _norm_model(model)]
    if not drifted:
        return
    logger.warning("child model is now %r; revoking sign grant(s) %s minted for another model",
                   model, [g.get("grant_id") for g in drifted])
    failed = revoke(drifted)
    # A grant that failed to revoke stays held, so the next call tries again.
    agent.__dict__["_held_sign_grants"] = [g for g in held if g not in drifted or str(g.get("grant_id")) in failed]
    if failed:
        # Fail closed: the other model must not run while a grant it was never given is live.
        raise RuntimeError(f"sign grant(s) {failed} could not be revoked on a model change to {model!r}")


def revoke(grants: List[Dict[str, Any]]) -> List[str]:
    """Revoke every grant in ``grants``; returns the grant ids that could not be revoked. Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_sign_grants as ksg

    failed: List[str] = []
    for g in grants or ():
        try:
            with kb.connect_closing(board=g["board"]) as conn:
                ksg.revoke(conn, g["grant_id"])
            _ledger({"event": "sign_grant_revoke", "grant_id": g["grant_id"], "at": int(time.time())})
        except Exception:
            # Expiry still bounds it; say so loudly.
            logger.warning("sign grant %s could not be revoked; it lapses at its expiry",
                           g.get("grant_id"), exc_info=True)
            failed.append(str(g.get("grant_id")))
    return failed


def close(grants: List[Dict[str, Any]]) -> List[str]:
    """Close every grant in ``grants`` (its child completed); returns the grant ids that could not be closed.
    Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_sign_grants as ksg

    failed: List[str] = []
    for g in grants or ():
        try:
            with kb.connect_closing(board=g["board"]) as conn:
                ksg.close(conn, g["grant_id"])
            _ledger({"event": "sign_grant_close", "grant_id": g["grant_id"], "at": int(time.time())})
        except Exception:
            logger.warning("sign grant %s could not be closed; revoking it instead", g.get("grant_id"), exc_info=True)
            failed.append(str(g.get("grant_id")))
    return failed


def _ledger(event: Dict[str, Any]) -> None:
    try:
        from gateway.kanban_boomerang_wake import ledger_append

        ledger_append(event)
    except Exception:
        logger.debug("sign grant ledger append failed", exc_info=True)
