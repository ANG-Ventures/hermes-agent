"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Shared plumbing (thread
offload, board enumeration, singleton lock, live-config coercers) lives in
``kanban_watchers_common``; the event-formatter table and the standalone
``_KanbanNotification`` / ``_notifier_collect`` pipeline in
``kanban_watchers_notifier``; ``_KanbanDispatcher`` in ``kanban_watchers_dispatcher``.
This module keeps the fleet's notifier loop (lane-failure dedupe, self-echo wake
suppression, wake-identity resolution, card home line) and the leadership-aware
dispatcher loop (standby retry, guard-stuck episodes, land queue, workspace
refusal paging) as the loop bodies ``GatewayRunner`` runs.
"""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, Optional

from agent.i18n import t
from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import (
    _adapter_for_subscription,
    _safe_review_reason,
    _served_profile_scope,
    _wake_scope_id,
)
from gateway.routing_identity import (
    creator_stamp_is_session_key,
    effective_routing_lane,
    routing_key_carries_identity,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

class _WakeRoutingIdentity(NamedTuple):
    """One coherent routing identity from a live session entry."""

    user_id: str
    user_id_alt: str
    scope_id: str

    @property
    def participant(self) -> str:
        return self.user_id_alt or self.user_id


def resolve_wake_identity(
    sub_user_id: Optional[str],
    sub_user_id_alt: Optional[str],
    sub_scope_id: Optional[str],
    live_identities: Iterable[_WakeRoutingIdentity],
) -> _WakeRoutingIdentity:
    """Resolve one coherent wake identity without repointing a named row.

    A subscription that already names a participant is authoritative. Otherwise
    exactly one complete live identity may fill the missing participant and
    scope together. Ambiguity is measured over the complete tuple so two Slack
    workspaces carrying the same user id cannot collapse into one candidate.
    An existing scope-only row narrows evidence to that scope. Zero or multiple
    candidates preserve the row as-is, degrading to the shared key.
    """
    existing = _WakeRoutingIdentity(
        str(sub_user_id or "").strip(),
        str(sub_user_id_alt or "").strip(),
        str(sub_scope_id or "").strip(),
    )
    if existing.participant:
        return existing
    candidates = {
        _WakeRoutingIdentity(
            str(identity.user_id or "").strip(),
            str(identity.user_id_alt or "").strip(),
            str(identity.scope_id or "").strip(),
        )
        for identity in live_identities
        if str(identity.participant or "").strip()
    }
    if existing.scope_id:
        candidates = {
            identity for identity in candidates
            if identity.scope_id == existing.scope_id
        }
    if len(candidates) != 1:
        return existing
    return next(iter(candidates))


def resolve_wake_participant(
    sub_user_id: Optional[str],
    live_participants: Iterable[str],
) -> Optional[str]:
    """Pick the participant a kanban wake should be delivered as.

    This is the *prevention* half of the phantom-session fix. #555 made an
    identity-less ``kanban_notify_subs`` row RECOVERABLE (``add_notify_sub``
    backfills, ``notify-repair`` collapses existing rows); it did not stop the
    wake itself from opening a user-less session key in the meantime. Every
    identity-less row still mints the bare key on its FIRST wake, before any
    repair can run.

    So resolve at the point of harm instead: when the row names no participant
    but the gateway's own live routing index shows exactly ONE per-user session
    for that chat, wake as that participant — the key the human's own messages
    already resolve to. One session for the channel, not two.

    Rules, in order:

    * ``sub_user_id`` set  -> always wins, untouched. The row's own identity is
      authoritative; a second human subscribing in the same chat must never be
      able to repoint the first one's lane.
    * exactly one live participant -> adopt it. This is evidence, not a guess:
      the gateway resolved that key from a real inbound message in this chat.
    * zero participants -> ``None``. A cron / CLI / home-channel origin is
      legitimately user-less and must keep delivering to the shared per-chat
      session. Never fabricate an identity.
    * two or more -> ``None``. A shared channel has no single right answer, and
      picking one would route a system notification into some human's private
      per-user session. Same refusal ``notify-repair`` already makes.

    Pure and side-effect free so the decision is testable without a gateway;
    the caller owns the evidence (``_live_chat_participants``) and the write.
    """
    identity = resolve_wake_identity(
        sub_user_id,
        None,
        None,
        (
            _WakeRoutingIdentity(str(p or "").strip(), "", "")
            for p in live_participants
        ),
    )
    return identity.participant or None


# Benign-decline buckets on a ``DispatchResult``: the dispatcher looked at the
# board and chose NOT to spawn for a self-clearing reason (a concurrency cap is
# saturated, a worker bounced off a provider rate-limit / quota wall, another
# process holds the board lock, or a respawn guard is cooling a task down).
# None of these are operator-actionable — the work resumes on a later tick — so
# a zero-spawn tick explained by any of them must NOT count toward the "stuck"
# streak. See the field docstrings on ``kanban_db.DispatchResult``.
_BENIGN_DECLINE_FIELDS = (
    "skipped_per_profile_capped",
    "rate_limited",
    "respawn_guarded",
    "skipped_locked",
)
# Genuine-fault buckets: the dispatcher TRIED to spawn and the attempt failed.
# ``spawn_failed`` is populated on EVERY spawn failure this tick (workspace
# resolution or worker launch), so an early, pre-circuit-breaker failure on one
# board is a fault immediately — it can't be masked by a benign decline on a
# different board and silently reset the streak. ``auto_blocked`` is the subset
# that additionally tripped the circuit breaker (broken venv / PATH / credential
# loss → repeated spawn_failed). Either forces the tick to count.
_FAULT_FIELDS = (
    "workspace_refused",
    "spawn_failed",
    "auto_blocked",
)


def format_home_line(session_id: Optional[str], row: Optional[dict] = None) -> str:
    """``home: <platform> #<channel> \u00b7 session <id>`` for a card's home
    session, or ``""`` when the card has none. ``row`` is the state.db
    ``sessions`` row (``origin_json`` / ``source`` / ``display_name``); when it
    is missing only the session id is shown."""
    sid = (session_id or "").strip()
    if not sid:
        return ""
    origin: dict = {}
    if row:
        raw = row.get("origin_json")
        if raw:
            try:
                import json as _json

                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    origin = parsed
            except (TypeError, ValueError):
                origin = {}
    platform = (origin.get("platform") or (row or {}).get("source") or "").strip()
    channel = str(
        origin.get("chat_name") or (row or {}).get("display_name")
        or origin.get("chat_id") or ""
    ).strip().lstrip("#")
    where = platform
    if channel:
        where = f"{where} #{channel}" if where else f"#{channel}"
    return f"home: {where} \u00b7 session {sid}" if where else f"home: session {sid}"


def _resolve_home_line(session_id: Optional[str]) -> str:
    """Blocking state.db lookup; call from a worker thread only."""
    sid = (session_id or "").strip()
    if not sid:
        return ""
    row = None
    try:
        from hermes_state import SessionDB

        # Read-only: a writable SessionDB runs schema init and waits up to
        # _WRITE_PATIENCE_S on a locked state.db for a pure lookup, once per
        # subscription per tick, after the cursor has advanced (FleetReview #987).
        db = SessionDB(read_only=True)
        try:
            row = db.get_session(sid)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception:
        row = None
    return format_home_line(sid, row)


def _session_origin(session_id: str) -> Optional[dict]:
    """The ``origin_json`` dict a session was opened from, or None.

    Blocking state.db lookup; call from a worker thread only.
    """
    row = None
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            row = db.get_session(session_id)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception:
        return None
    raw = (row or {}).get("origin_json")
    if not raw:
        return None
    try:
        import json as _json

        origin = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(origin, dict):
        return None
    return origin


def _origin_is_subscriber(origin: Optional[dict], sub: dict) -> bool:
    """True only when ``origin`` is provably the subscriber's own session.

    The wake targets ONE session: the subscriber participant's (group
    sessions are per user by default) under the notifier profile. So chat
    alone is not enough: platform, chat, thread, participant and profile
    must all match. Anything missing fails open (not self), so a wake is
    never dropped on a guess.
    """
    if not isinstance(origin, dict):
        return False

    def _s(v) -> str:
        return str(v or "").strip()

    if (
        _s(origin.get("platform")).lower() != _s(sub.get("platform")).lower()
        or _s(origin.get("chat_id")) != _s(sub.get("chat_id"))
        or _s(origin.get("thread_id")) != _s(sub.get("thread_id"))
        or not _s(sub.get("chat_id"))
    ):
        return False
    sub_users = {_s(sub.get("user_id")), _s(sub.get("user_id_alt"))} - {""}
    actor_users = {_s(origin.get("user_id")), _s(origin.get("user_id_alt"))} - {""}
    if not sub_users or not (sub_users & actor_users):
        return False
    actor_profile = _s(origin.get("profile"))
    sub_profile = _s(sub.get("notifier_profile")) or "default"
    return bool(actor_profile) and actor_profile == sub_profile


def self_caused_event_ids(sub: dict, events, origin_of=None) -> set[int]:
    """Ids of claimed events made by a session living in ``sub``'s own chat.

    Waking a chat about a transition its own session just made is an echo:
    the agent already knows, and spends a turn saying "echo of my own close,
    nothing further" (t_a4890a77: 72 of 482 pings in Ace's chat over 48h,
    25 visible echo replies). The passive line still posts; only the wake is
    skipped. Events with no recorded actor, or an actor whose origin is
    unknown or another chat/participant/profile, still wake. Matching is by
    the actor session's recorded origin only (no ``actor == chat_id``
    shortcut). The caller drops the wake only for events whose passive line
    was actually delivered.
    """
    origin_of = origin_of or _session_origin
    out: set[int] = set()
    cache: dict[str, bool] = {}
    for ev in events or []:
        actor = (getattr(ev, "actor_session_id", None) or "").strip()
        if not actor:
            continue
        if actor not in cache:
            cache[actor] = _origin_is_subscriber(origin_of(actor), sub)
        if cache[actor]:
            out.add(ev.id)
    return out


# LoadGate moved to hermes_cli.kanban_load_gate (shared with the standalone
# `hermes kanban daemon` loop); re-exported here for existing importers.
from hermes_cli.kanban_load_gate import LoadGate  # noqa: E402,F401


def _format_spawn_routes(routes, sources=None) -> str:
    """Format provider/model and source for every spawned task."""

    from hermes_cli.model_policy import route_kind

    entries = dict(routes or {})
    if not entries:
        return "routes=-"
    sources = dict(sources or {})
    return "routes=" + "; ".join(
        f"{task_id} route={route} source={sources.get(task_id, 'profile-default')} "
        f"kind={route_kind(route)}"
        for task_id, route in entries.items()
    )


def _format_lane_expiry(lane, route, successor="profile default") -> str:
    return f"lane-model expired -> {successor} ({lane}: {route})"


def _log_dispatch_tick(logger, slug, res) -> None:
    """Log route choices and expiries, including ticks with no new workers."""
    if res is None:
        return
    successors = getattr(res, "expired_lane_successors", None) or {}
    for lane, route in (getattr(res, "expired_lane_models", None) or []):
        logger.info(
            "kanban dispatcher [%s]: %s",
            slug,
            _format_lane_expiry(lane, route, successors.get(lane, "profile default")),
        )
    spawned = getattr(res, "spawned", None)
    guarded = getattr(res, "respawn_guarded", None)
    parent_satisfied_sticky = getattr(res, "parent_satisfied_sticky", None)
    if spawned or guarded or parent_satisfied_sticky:
        # Quiet by default — log only actionable tick activity, including
        # guarded tasks and satisfied dependency graphs still held by an
        # explicit worker/operator block.
        logger.info(
            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d %s %s %s",
            slug,
            len(spawned or []),
            res.reclaimed,
            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
            res.promoted,
            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            _format_spawn_routes(
                getattr(res, "spawn_routes", None),
                getattr(res, "spawn_route_sources", None),
            ),
            _format_respawn_guarded_summary(guarded),
            _format_parent_satisfied_sticky_summary(parent_satisfied_sticky),
        )


def _format_parent_satisfied_sticky_summary(task_ids) -> str:
    """Count and name explicit holds whose dependencies are already done."""
    ids = sorted(str(task_id) for task_id in (task_ids or []))
    if not ids:
        return "parents_done_sticky=0"
    return f"parents_done_sticky={len(ids)} ({', '.join(ids)})"


def _format_respawn_guarded_summary(guarded) -> str:
    """Format guarded task ids by reason for the per-tick gateway log."""
    entries = list(guarded or [])
    if not entries:
        return "respawn_guarded=0"
    grouped: dict[str, list[str]] = {}
    for task_id, reason in entries:
        grouped.setdefault(str(reason), []).append(str(task_id))
    details = "; ".join(
        f"{reason}: {', '.join(task_ids)}"
        for reason, task_ids in grouped.items()
    )
    return f"respawn_guarded={len(entries)} ({details})"


def _format_workspace_refused_summary(refused) -> str:
    """Format mount-admission refusals by stable reason for the tick log."""
    entries = list(refused or [])
    if not entries:
        return "workspace_refused=0"
    grouped: dict[str, list[str]] = {}
    for task_id, detail in entries:
        reason = str(detail).split(":", 1)[0]
        grouped.setdefault(reason, []).append(str(task_id))
    details = "; ".join(
        f"{reason}: {', '.join(sorted(task_ids))}"
        for reason, task_ids in sorted(grouped.items())
    )
    summary = f"workspace_refused={len(entries)} ({details})"
    if "stranded_by_mount_loss" in grouped:
        from hermes_cli.kanban_workspace_policy import STRANDED_RECOVERY_COMMAND

        summary += f" | recover stranded scratch cards: {STRANDED_RECOVERY_COMMAND}"
    return summary


class _WorkspaceRefusalOutageNotifier:
    """Page each refused card once per refusal episode (post-on-change).

    The latch is durable and per card (``claim_workspace_refusal_pages``):
    an in-memory per-board latch re-armed whenever a tick happened not to
    admission-check the card (cap / guard / provider skip), on every gateway
    restart, and in every failover dispatcher (t_ca81dfe2). If the board DB
    cannot be reached the claim falls back to an in-process latch so a real
    outage still pages once instead of every tick.
    """

    def __init__(self, claim=None, release=None) -> None:
        self._claim = claim or _claim_refusal_pages
        self._release = release or _release_refusal_pages
        self._fallback: set[tuple[str, str, str]] = set()

    def observe(self, board: str, refused, send: Callable[[str, str], bool]) -> bool:
        entries = [(str(t), str(r)) for t, r in (refused or [])]
        if not entries:
            return False
        try:
            claimed = self._claim(board, entries)
            durable = True
        except Exception:
            logger.exception(
                "kanban dispatcher: durable refusal-page latch failed on %s; "
                "using in-process latch", board,
            )
            durable = False
            claimed = [
                (t, r, None) for t, r in entries
                if (board, t, r) not in self._fallback
            ]
        if not claimed:
            return False
        summary = _format_workspace_refused_summary([(t, r) for t, r, _ in claimed])
        if len(claimed) < len(entries):
            summary += f" (+{len(entries) - len(claimed)} already paged)"
        if len(claimed) == 1:
            summary += f"\nInspect: `hermes kanban show {claimed[0][0]}`"
        if not send(board, summary):
            if durable:
                try:
                    self._release(board, [eid for _t, _r, eid in claimed])
                except Exception:
                    logger.exception(
                        "kanban dispatcher: could not release refusal-page claim on %s",
                        board,
                    )
            return False
        if not durable:
            self._fallback.update((board, t, r) for t, r, _ in claimed)
        return True


def _claim_refusal_pages(board: str, entries):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board=board) as conn:
        return kb.claim_workspace_refusal_pages(conn, entries)


def _release_refusal_pages(board: str, event_ids) -> None:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board=board) as conn:
        kb.release_workspace_refusal_pages(conn, event_ids)


def _alert_notify_script() -> Optional[Path]:
    """notify.py under the RUNNING Hermes root (same resolver as the kanban
    CLI's alerts), never a hardcoded ``~/.hermes``: a redirected/sandboxed
    home must not page the live channel about its own boards."""
    try:
        from hermes_cli.kanban import _notify_script_path

        found = _notify_script_path()
    except Exception:
        logger.debug("kanban dispatcher: notify.py lookup failed", exc_info=True)
        return None
    return Path(found) if found else None


def _send_workspace_refusal_alert(board: str, summary: str) -> bool:
    """Best-effort #alerts page through the fleet notify boundary."""
    script = _alert_notify_script()
    if script is None:
        logger.error("kanban dispatcher: notify.py unavailable; workspace outage page not delivered")
        return False
    message = (
        "🛑 **Kanban dispatcher** · Workspace admission refused\n"
        f"Board: `{board}`\n{summary}\n"
        "The dispatcher refused before spawn; inspect the configured workspace mount. "
        "No durable-disk fallback was created."
    )
    try:
        proc = subprocess.run(
            [
                sys.executable, str(script), "--send", message,
                "--channel", "discord", "--profile", "default", "--sev", "error",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        logger.exception("kanban dispatcher: workspace outage page failed")
        return False
    if proc.returncode != 0:
        logger.error(
            "kanban dispatcher: workspace outage page not delivered (rc=%d)",
            proc.returncode,
        )
        return False
    return True


def _send_proc_slot_alert(gate) -> bool:
    """Page #alerts once when the load gate enters ``proc_paused``.

    t_b660edb6: the Studio climbed to its per-uid process limit over 3 h
    with load1 at 7-8 and nothing paged; at the limit every fork (hooks,
    cron shells, this page's own subprocess) fails. The gate trips at
    ``proc_pause_fraction`` of the limit, while a fork still works.
    """
    script = _alert_notify_script()
    if script is None:
        logger.error("kanban dispatcher: notify.py unavailable; process-slot page not delivered")
        return False
    from hermes_cli import kanban_load_gate as _klg

    top = ", ".join(f"{name} x{n}" for name, n in _klg.top_user_proc_families(3))
    message = (
        "🛑 **Kanban dispatcher** · host running out of process slots\n"
        f"{gate.last_reason}\n"
        f"Top process names for this user: {top or 'unreadable'}\n"
        "Spawns are paused. At the limit every fork() fails with EAGAIN "
        "(hooks fail closed, cron shells die, the gateway watchdog exits). "
        "Find the forker: `python3 fleet/exec-flight-recorder.py --grep . --since <now-30m>` "
        "and count by ppid."
    )
    try:
        proc = subprocess.run(
            [
                sys.executable, str(script), "--send", message,
                "--channel", "discord", "--profile", "default", "--sev", "error",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        logger.exception("kanban dispatcher: process-slot page failed")
        return False
    return proc.returncode == 0


def _host_recovery_note(gate) -> Optional[str]:
    """Requeue note when this tick's gate admits, else None (no requeue)."""
    if not gate.host_recovered():
        return None
    l1 = "?" if gate.load1 is None else f"{gate.load1:.1f}"
    return (
        f"dispatcher load gate {gate.state}, load1={l1}, "
        f"procs={gate.procs if gate.procs is not None else '?'}"
        f"/{gate.proc_limit or '?'}"
    )


def _requeue_host_transient_on(conn, slug: str, note: Optional[str]) -> list:
    """Unblock host-exhaustion ``transient`` cards on an OPEN board conn.

    Runs on the dispatch tick's own connection (no extra board open; a
    corrupt board already fails at connect). Errors are logged, never raised.
    """
    from hermes_cli import kanban_db as kb

    if not note:
        return []
    try:
        ids = kb.requeue_host_transient_blocks(conn, note=note)
    except Exception:
        # Never cost the board its dispatch tick.
        logger.exception("kanban dispatcher: transient requeue failed on %s", slug)
        return []
    if ids:
        logger.warning(
            "kanban dispatcher [%s]: auto-requeued %d host-transient block(s) "
            "after host recovery (%s): %s",
            slug, len(ids), note, ", ".join(ids),
        )
    return ids


def _observe_workspace_refusal_outages(notifier, results) -> int:
    """Process one full dispatcher tick; skipped boards do not imply recovery."""
    delivered = 0
    for board, result in results or []:
        if result is None:
            continue
        refused = getattr(result, "workspace_refused", None) or []
        delivered += int(
            notifier.observe(board, refused, _send_workspace_refusal_alert)
        )
    return delivered


def _guard_stuck_cards(results) -> tuple[list[tuple[str, dict]], set[str]]:
    """Probe guarded cards; only successful board probes can prove recovery."""
    from hermes_cli import kanban_db as kb

    cards = []
    observed_boards = set()
    for board, result in results or []:
        if result is None or getattr(result, "skipped_locked", False):
            continue
        try:
            with kb.connect_closing(board=board) as conn:
                cards.extend(
                    (board, item)
                    for item in kb.respawn_guard_stuck_tasks(conn, board=board)
                )
            observed_boards.add(board)
        except Exception:
            logger.exception("kanban dispatcher: guard-stuck probe failed on %s", board)
    return cards, observed_boards


# A still-stuck guard episode re-pages this often; a new episode pages at once.
_GUARD_STUCK_REMIND_SECONDS = 6 * 3600


def _guard_stuck_state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / "kanban-guard-stuck-pages.json"


_GUARD_STUCK_PAGE_BUDGET_S = 30.0


class _GuardStuckNotifier:
    """Page once per guard EPISODE, remind every 6h; retry failed sends.

    An episode is ``(board, card, reason, guarded_since)``: ``guarded_since``
    only moves when an event that can change the guard's answer resets the
    streak, so a probe blip (streak briefly stale) or a gateway restart is the
    same episode and stays silent. With ``state_path`` the ledger survives a
    restart. 2026-09-27: in-memory state re-paged t_a3ce620f x3 and t_779040d0
    x2 within 2.5h (18:13 restart re-paged at 18:14).
    """

    def __init__(self, state_path: Optional[Path] = None,
                 remind_seconds: int = _GUARD_STUCK_REMIND_SECONDS) -> None:
        self._state_path = state_path
        self._remind = int(remind_seconds)
        self._sent: dict[str, float] = self._load()

    @staticmethod
    def _key(board: str, item: dict) -> str:
        reason = str(item.get("reason") or "active_pr")
        # An active_pr hold is ONE episode per (card, PR): a requeue/respawn
        # that restarts the streak on the same open PR is not a new episode
        # (r19, t_f1af5dcd). Other guards keep the streak-start episode.
        episode = (f"pr={item['pr']}" if reason == "active_pr" and item.get("pr")
                   else str(item.get("guarded_since") or ""))
        return "|".join((str(board), str(item["task_id"]), reason, episode))

    def _last_sent(self, key: str, item: dict) -> Optional[float]:
        """Last page time for ``key``. An active_pr key also honours the pre-r19
        ledger entry of the SAME streak (``…|active_pr|<guarded_since>``) so the
        deploy does not re-page every open episode once; only that exact legacy
        key counts, so a card's different PR still pages (Prism #1530)."""
        last = self._sent.get(key)
        if "|active_pr|pr=" not in key or not item.get("guarded_since"):
            return last
        legacy = key.split("|pr=", 1)[0] + f"|{item['guarded_since']}"
        # Consume it: the legacy key names no PR, so it may silence only the
        # first PR observed for this streak; a PR the card acquires later in
        # the SAME streak (same guarded_since) still pages (Prism #1530). Popped
        # on a canonical hit too: a ledger migrated by the pre-consume code
        # holds both keys (Prism #1534 a52a75cf4895).
        legacy_at = self._sent.pop(legacy, None)
        pr_prefix = key.split("|pr=", 1)[0] + "|pr="
        if legacy_at is not None and any(
                k != key and k.startswith(pr_prefix) and at == legacy_at
                for k, at in self._sent.items()):
            # Already migrated to another PR of this card: that PR owns the page
            # time; this one was never paged (Prism #1534 886ea41b20e6).
            legacy_at = None
        if last is None and legacy_at is not None:
            # Migrate: the canonical per-PR key now carries the page time, so a
            # later streak reset on the same PR still finds it (Prism #1530 r2).
            # observe() persists it (the ledger differs from its snapshot).
            last = self._sent[key] = legacy_at
        return last
        legacy = key.split("|pr=", 1)[0] + f"|{item['guarded_since']}"
        # Consume it: the legacy key names no PR, so it may silence only the
        # first PR observed for this streak; a PR the card acquires later in
        # the SAME streak (same guarded_since) still pages (Prism #1530).
        last = self._sent.pop(legacy, None)
        if last is not None:
            # Migrate: the canonical per-PR key now carries the page time, so a
            # later streak reset on the same PR still finds it (Prism #1530 r2).
            # observe() persists it (the ledger differs from its snapshot).
            self._sent[key] = last
        return last

    def _load(self) -> dict[str, float]:
        if self._state_path is None:
            return {}
        try:
            import json

            data = json.loads(self._state_path.read_text(encoding="utf-8-sig"))
            return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            import json

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(f"{self._state_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._sent, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            logger.warning("kanban dispatcher: guard-stuck page ledger not saved", exc_info=True)

    def observe(self, cards, send, observed_boards=None, now: Optional[float] = None) -> int:
        """Page due episodes. Sends run serially (each up to 30 s) inside the
        dispatcher tick, so one call spends at most ``_GUARD_STUCK_PAGE_BUDGET_S``
        on them (FleetReview #79); an unsent page is not recorded and goes
        out on a later tick."""
        deadline = time.monotonic() + _GUARD_STUCK_PAGE_BUDGET_S
        now = time.time() if now is None else float(now)
        current = {self._key(board, item) for board, item in cards}
        # A land-request is tracked apart from the page (``<key>|land``) so an
        # enqueue does not spend the (card, PR) page slot (Prism #1545).
        current |= {f"{key}|land" for key in current}
        if observed_boards is None:
            observed_boards = {board for board, _ in cards}
        before = dict(self._sent)
        # Forget an episode only once it is gone from an observed board AND its
        # last page is older than the reminder: a blip keeps the same key.
        self._sent = {
            key: at for key, at in self._sent.items()
            if key in current or key.split("|", 1)[0] not in observed_boards
            or now - at < self._remind
        }
        delivered = 0
        for board, item in cards:
            key = self._key(board, item)
            last = self._last_sent(key, item)
            # An active_pr episode (card + PR) pages ONCE: the hold is correct and
            # the verb does not change, so a 6h reminder restates the same page
            # (Ace r31 G: house-voice t_82169667 paged 19:02, 02:39, 08:40).
            # The land-request still runs: a PR paged while red/unfinished must
            # be enqueued once it turns mergeable (Prism #1631 7279b2c05f7a).
            land_only = last is not None and "|active_pr|pr=" in key
            if land_only and not _land_candidate(item):
                continue
            if not land_only and last is not None and now - last < self._remind:
                continue
            if time.monotonic() >= deadline:
                break
            land_key = f"{key}|land"
            land_at = self._sent.get(land_key)
            probe = dict(item)
            if land_at is not None and now - land_at < self._remind:
                probe["land_enqueued_at"] = land_at
            if land_only:
                probe["land_only"] = True
                if send(board, probe) and probe.get("land_request") == "enqueued":
                    self._sent[land_key] = now
                continue
            if send(board, probe):
                if probe.get("land_request") == "enqueued":
                    # Enqueued, nobody paged: a later non-merged queue outcome
                    # still pages once on the next tick (Prism #1545).
                    self._sent[land_key] = now
                elif probe.get("land_request") != "pending":
                    self._sent[key] = now
                    delivered += 1
        if self._sent != before:
            self._save()
        return delivered


# Run outcomes after which the worker had FINISHED its deliverable. Any other
# last outcome (timed_out, crashed, rate_limited, reclaimed, stale, stalled,
# changes_requested, cohort_death, gave_up, ... or one added later) means it was
# interrupted or sent back, so the safe default is REQUEUE (Prism #1530 r2).
_FINISHED_RUN_OUTCOMES = frozenset({"completed", "review_requested"})


def _github_pr(pr_url) -> Optional[tuple[str, int]]:
    """``(owner/repo, number)`` for a GitHub-legal PR URL, else None.

    The URL comes from card text, so owner/repo must be GitHub-legal names
    (no shell metacharacters); anything else is not a PR (Prism #1530).
    """
    import re

    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/"
                     r"([A-Za-z0-9._-]{1,100})/pull/([0-9]{1,9})/?", str(pr_url or ""))
    if not m or m.group(2) in (".", ".."):
        return None
    return f"{m.group(1)}/{m.group(2)}", int(m.group(3))


def _land_verb(pr_url: str) -> Optional[str]:
    """``fleet-merge.sh <owner/repo> <n> …`` for a GitHub PR URL, else None.

    The URL comes from card text, so owner/repo must be GitHub-legal names
    (no shell metacharacters) and every argument is shell-quoted; anything
    else renders no LAND command at all (Prism #1530).
    """
    import shlex

    parsed = _github_pr(pr_url)
    if parsed is None:
        return None
    return "~/.hermes/scripts/fleet-merge.sh " + shlex.join(
        [parsed[0], str(parsed[1]), "--by", "<you>", "--reason", "<why safe>"])


def _active_pr_detail(board: str, item: dict) -> str:
    """Name the ONE verb the operator should run for an active_pr hold.

    The hold is correct: the card's PR is open, so a respawn would duplicate
    it. What the operator must decide is whether that PR is the finished
    deliverable (LAND it, then complete the card) or a worker's unfinished
    branch (REQUEUE so the worker resumes on it). The last run's outcome
    decides the recommendation; both commands are given (r19, t_f1af5dcd).
    """
    outcome = item.get("last_outcome")
    land = _land_verb(item.get("pr"))
    unfinished = outcome not in _FINISHED_RUN_OUTCOMES or land is None
    lines = ["READY card held behind its open PR (active_pr >30 min)"]
    if item.get("pr"):
        lines.append(f"Holding PR: {item['pr']} · last run: `{outcome or 'none'}`")
    if item.get("land_queue"):
        lines.append(f"Land queue already stopped on this PR: `{item['land_queue']}` (see the card comment)")
    if unfinished:
        lines.append(f"Wanted: **REQUEUE** (worker resumes on its PR): `{item['clear_verb']}`")
        if land:
            lines.append(f"Or, if the PR is complete + CI-green, LAND it: `{land}`")
    else:
        lines.append(f"Wanted: **LAND** the PR if CI is green, then complete the card: `{land}`")
        lines.append(f"Or, if it needs more work, REQUEUE: `{item['clear_verb']}`")
    return "\n".join(lines)


def _land_queue_prior_outcome(root: Path, repo: str, number: int) -> Optional[str]:
    """Status of a FINISHED land-queue row for ``repo#number``, else None.

    A finished row that is not ``merged`` means the queue already tried and
    stopped (FAILED / DIRTY / GAVE UP, card commented): a human is needed, so
    the hold pages instead of enqueueing the same PR again.
    """
    import json

    done = root / "state" / "apollo-land-queue.done"
    found = None
    try:
        paths = sorted(done.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if (isinstance(row, dict) and str(row.get("repo", "")).lower() == repo.lower()
                and str(row.get("pr")) == str(number)):
            found = str(row.get("status") or "finished")
    return found


def _enqueue_active_pr_land(board: str, item: dict) -> Optional[bool]:
    """Enqueue a land-request for a finished card's mergeable holding PR.

    None: not landable here (page the operator). True: enqueued (or already
    queued); ``item["land_request"]`` says which (``enqueued`` /
    ``pending``) so the page slot stays free for a later queue failure
    (Prism #1545). False: the enqueue failed (page instead). Landable means the
    guard held on ``RESPAWN_GUARD_HOLD_MERGEABLE`` (OPEN, mergeable, no red
    check), the last run FINISHED its deliverable, the PR's head branch names
    this card (``pr_owned``), the URL is a legal GitHub
    PR, and the land queue has not already given up on this PR. The queue
    waits for CI green and lands through fleet-merge.sh, so fleet policy
    still decides; a refusal comments the card. r19 spec (t_f1af5dcd), wired
    t_5a9deed5: a green holding PR needs a land-request, not a human.
    """
    from hermes_cli import kanban_db as kb

    if item.get("reason") not in (None, "active_pr"):
        return None
    if item.get("hold") != kb.RESPAWN_GUARD_HOLD_MERGEABLE:
        return None
    if item.get("last_outcome") not in _FINISHED_RUN_OUTCOMES:
        return None
    if item.get("pr_owned") is not True:
        # A card comment may name any PR: land only a PR whose head branch
        # names this card; unknown ownership pages (Prism #1545 cd0457757028).
        return None
    parsed = _github_pr(item.get("pr"))
    if parsed is None:
        return None
    repo, number = parsed
    try:
        from hermes_constants import get_hermes_home

        root = Path(get_hermes_home())
    except Exception:
        logger.debug("kanban dispatcher: hermes home unresolved; no land-request", exc_info=True)
        return None
    script = root / "scripts" / "apollo_land_queue.py"
    if not script.is_file():
        return None
    prior = _land_queue_prior_outcome(root, repo, number)
    if prior is not None and prior != "merged":
        item["land_queue"] = prior
        return None
    if item.get("land_enqueued_at") is not None:
        # Already enqueued in this reminder window and the queue has not
        # stopped on the PR: wait silently, no second enqueue (Prism #1545).
        item["land_request"] = "pending"
        return True
    argv = [sys.executable, str(script), "enqueue", "--card", str(item["task_id"]),
            "--repo", repo, "--pr", str(number), "--board", str(board),
            "--item", f"{item['task_id']}: active_pr hold, PR mergeable, "
                      f"last run {item.get('last_outcome')} (dispatcher land-request)"]
    try:
        proc = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except Exception:
        logger.exception("kanban dispatcher: active_pr land-request enqueue failed")
        return False
    if proc.returncode != 0:
        logger.error("kanban dispatcher: active_pr land-request enqueue rc=%s for %s#%s: %s",
                     proc.returncode, repo, number, (proc.stderr or proc.stdout or "").strip()[:200])
        return False
    logger.info("kanban dispatcher [%s]: active_pr hold %s -> land-request enqueued for %s#%s (%s)",
                board, item["task_id"], repo, number, (proc.stdout or "").strip()[:160])
    item["land_request"] = "enqueued"
    return True


def _land_candidate(item: dict) -> bool:
    """Cheap pre-check: could ``_enqueue_active_pr_land`` act on this hold?"""
    from hermes_cli import kanban_db as kb

    return (item.get("reason") in (None, "active_pr")
            and item.get("hold") == kb.RESPAWN_GUARD_HOLD_MERGEABLE
            and item.get("last_outcome") in _FINISHED_RUN_OUTCOMES
            and item.get("pr_owned") is True)


def _send_guard_stuck_alert(board: str, item: dict) -> bool:
    """Land a finished card's mergeable PR, else page #alerts with the verb.

    ``land_only``: the (card, PR) episode already paged; try the land-request,
    never page again."""
    if item.get("reason") != "prior_worker_still_alive":
        enqueued = _enqueue_active_pr_land(board, item)
        if enqueued or item.get("land_only"):
            return bool(enqueued)
    script = _alert_notify_script()
    if script is None:
        logger.error("kanban dispatcher: notify.py unavailable; guard-stuck page not delivered")
        return False
    if item.get("reason") == "prior_worker_still_alive":
        # Both the ready and review claim doors refuse; name the card's lane.
        lane = str(item.get("status") or "ready").upper()
        detail = (
            f"{lane} card: prior_worker_still_alive claim rejected >15 min\n"
            f"Prior PID: `{item.get('prev_pid')}` · Inspect: `{item['clear_verb']}`\n"
            "Verify the previous owner before intervening; requeue alone cannot bypass the claim guard."
        )
    else:
        detail = _active_pr_detail(board, item)
    message = (
        f"🛑 **Kanban dispatcher** · {detail}\n"
        f"Board: `{board}` · Card: `{item['task_id']}`\n"
        "-# Pages once per stuck episode (active_pr: once per card + PR; other holds remind every 6h)."
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--send", message, "--channel", "discord",
             "--profile", "default", "--sev", "error"],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        logger.exception("kanban dispatcher: guard-stuck page failed")
        return False
    return proc.returncode == 0


# Needs-input pager (t_c8ca40b4): a card waiting on a human ruling pages its
# origin channel once per (card, reason), then "still waiting" every 2 h.
_NEEDS_INPUT_REPAGE_SECONDS = 2 * 3600


def _needs_input_state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / "kanban-needs-input-pages.json"


def _resolve_needs_input_pager_settings(load_config: Callable[[], Any]) -> "tuple[bool, bool]":
    """``(enabled, include_dependency)`` from ``kanban.needs_input_pager`` /
    ``kanban.needs_input_pager_dependency``, read every tick. A config read
    error keeps the pager ON: silence is the failure this exists to stop."""
    try:
        cfg = load_config()
    except Exception:
        return True, False
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    kcfg = kcfg if isinstance(kcfg, dict) else {}
    return (bool(kcfg.get("needs_input_pager", True)),
            bool(kcfg.get("needs_input_pager_dependency", False)))


# Kinds whose passive line ``kanban.lifecycle_channel`` re-routes (t_f7bba206).
# Failure lines (gave_up/crashed/timed_out/stalled), changes_requested and
# triage routing stay in the subscriber's chat: they need a human.
LIFECYCLE_CHANNEL_KINDS = frozenset({"completed", "review_requested", "blocked"})
# A needs_input block on a card at or above this priority stays in the origin
# chat (the needs-input pager pages there too).
LIFECYCLE_CHANNEL_KEEP_PRIORITY = 100


def kanban_artifact_names_tag(paths: list) -> str:
    """``artifacts: a.txt, b.tgz`` for a line that carries no upload (t_4bfc46a3)."""
    names = [os.path.basename(str(p)) for p in paths]
    shown = ", ".join(names[:5])
    if len(names) > 5:
        shown += f" +{len(names) - 5} more"
    return f"artifacts: {shown}"


def parse_lifecycle_channel(value: Any) -> "Optional[tuple[str, str]]":
    """``"platform:chat_id"`` -> ``(platform, chat_id)``; None when unset or malformed."""
    if not isinstance(value, str) or ":" not in value:
        return None
    platform, _, chat_id = value.strip().partition(":")
    platform, chat_id = platform.strip().lower(), chat_id.strip()
    if not platform or not chat_id:
        return None
    return platform, chat_id


def lifecycle_channel_target(
    channel: "Optional[tuple[str, str]]", kind: str, payload: Optional[dict], task: Any,
) -> "Optional[tuple[str, str]]":
    """Target for a done / ready-for-review / blocked line, or None to keep
    the subscriber's own chat. A needs_input block on a priority >= 100 card
    stays with the subscriber."""
    if channel is None or kind not in LIFECYCLE_CHANNEL_KINDS:
        return None
    if kind == "blocked" and (payload or {}).get("kind") == "needs_input":
        try:
            priority = int(getattr(task, "priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        if priority >= LIFECYCLE_CHANNEL_KEEP_PRIORITY:
            return None
    return channel


def _resolve_lifecycle_channel() -> "Optional[tuple[str, str]]":
    """``kanban.lifecycle_channel``, read per notifier tick. Errors -> unset."""
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        return parse_lifecycle_channel(kcfg.get("lifecycle_channel") if isinstance(kcfg, dict) else None)
    except Exception:
        return None


def _resolve_lifecycle_digest_seconds() -> int:
    """``kanban.lifecycle_digest_seconds``, read per notifier tick. Errors -> 0 (off)."""
    from gateway.kanban_lifecycle_digest import resolve_digest_seconds

    return resolve_digest_seconds()


def _needs_input_cards(results, include_dependency: bool = False
                       ) -> tuple[list[tuple[str, dict]], set[str]]:
    """Probe each ticked board; only successful probes can prove a card unblocked."""
    from hermes_cli import kanban_db as kb

    cards = []
    observed_boards = set()
    for board, result in results or []:
        if result is None or getattr(result, "skipped_locked", False):
            continue
        try:
            with kb.connect_closing(board=board) as conn:
                cards.extend(
                    (board, item) for item in kb.needs_input_page_candidates(
                        conn, include_dependency=include_dependency)
                )
            observed_boards.add(board)
        except Exception:
            logger.exception("kanban dispatcher: needs-input probe failed on %s", board)
    return cards, observed_boards


class _NeedsInputPager(_GuardStuckNotifier):
    """Page once per ``(board, card, reason-hash)``; re-page "still waiting"
    every 2 h while the card stays blocked. A new reason is a new page. The
    ledger persists, so a gateway restart does not re-page."""

    def __init__(self, state_path: Optional[Path] = None,
                 remind_seconds: int = _NEEDS_INPUT_REPAGE_SECONDS) -> None:
        super().__init__(state_path, remind_seconds)
        self._rotation = 0

    @staticmethod
    def _key(board: str, item: dict) -> str:
        import hashlib

        digest = hashlib.sha1(str(item.get("reason") or "").encode("utf-8")).hexdigest()[:12]
        return "|".join((str(board), str(item["task_id"]), digest))

    def observe(self, cards, send, observed_boards=None, now: Optional[float] = None) -> int:
        deadline = time.monotonic() + _GUARD_STUCK_PAGE_BUDGET_S
        now = time.time() if now is None else float(now)
        current = {self._key(board, item) for board, item in cards}
        if observed_boards is None:
            observed_boards = {board for board, _ in cards}
        before = dict(self._sent)
        # Forget a key once its card left an observed board AND the last page
        # is older than the re-page window: an unblock/re-block blip on the
        # same reason stays deduped.
        self._sent = {
            key: at for key, at in self._sent.items()
            if key in current or key.split("|", 1)[0] not in observed_boards
            or now - at < self._remind
        }
        delivered = 0
        # Rotate the start each tick: failing sends stay due, and without the
        # rotation a run of them at the head of the list would spend the page
        # budget every tick and starve the cards behind them.
        cards = list(cards)
        if cards:
            start = self._rotation % len(cards)
            cards = cards[start:] + cards[:start]
            self._rotation += 1
        for board, item in cards:
            key = self._key(board, item)
            last = self._sent.get(key)
            if last is not None and now - last < self._remind:
                continue
            if time.monotonic() >= deadline:
                break
            if send(board, item, last is not None):
                self._sent[key] = now
                delivered += 1
        if self._sent != before:
            self._save()
        return delivered


def _notify_send(script: Path, message: str, extra: list) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--send", message, "--channel", "discord",
             "--profile", "default", *extra],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        logger.exception("kanban dispatcher: needs-input page failed")
        return False
    return proc.returncode == 0


def _send_needs_input_page(board: str, item: dict, still_waiting: bool = False) -> bool:
    """Post to the card's origin channel (numeric id); mirror to #alerts at
    priority >= 200. A card with no origin channel pages #alerts instead, so
    a priority >= 100 card is never silent. True when the primary post landed."""
    script = _alert_notify_script()
    if script is None:
        logger.error("kanban dispatcher: notify.py unavailable; needs-input page not delivered")
        return False
    tid = str(item["task_id"])
    board_flag = "" if board in ("", "default", None) else f"--board {board} "
    reason = " ".join(str(item.get("reason") or "").split())[:300]
    lead = "⚠️ still waiting: " if still_waiting else "⚠️ "
    message = (
        f"{lead}`{tid}` needs a ruling: {reason} — `hermes kanban {board_flag}show {tid}`\n"
        f"-# p{item.get('priority', 0)} · {str(item.get('title') or '')[:120]} · "
        f"re-pages every 2 h while blocked · opt out: `hermes kanban {board_flag}edit {tid} --no-page`"
    )
    channel = item.get("channel")
    if channel is not None and not str(channel).isdigit():
        channel = None  # numeric channel ids only
    if channel:
        ok = _notify_send(script, message, ["--target", str(channel)])
        if item.get("alerts") and not _notify_send(script, message, ["--sev", "error"]):
            logger.error("kanban dispatcher: needs-input #alerts mirror failed for %s", tid)
        return ok
    return _notify_send(script, message, ["--sev", "error"])


def _stall_streak_is_bad(ready_pending, any_spawned, results, *, guard_stuck=False) -> bool:
    """Decide whether a dispatcher tick counts toward the "stuck" streak.

    A tick is "bad" (stall-suspect) only when there is spawnable work,
    nothing was spawned, AND the zero-spawn is not explained by a benign,
    self-clearing decline (concurrency cap saturated / provider rate-limit /
    board lock held / respawn guard). A genuine hard fault (circuit-breaker
    ``auto_blocked``) always counts even if a benign decline co-occurs on
    another board.

    This is the fix for the false "check profile health (venv, PATH,
    credentials)" warning that fired for ~2h during a provider 429 window /
    large fan-out, when the dispatcher was healthy but throttled.
    """
    if guard_stuck:
        return True
    if not ready_pending or any_spawned:
        return False
    declined_benign = False
    fault_seen = False
    for _slug, res in (results or []):
        if res is None:
            continue
        for name in _FAULT_FIELDS:
            if getattr(res, name, None):
                fault_seen = True
        for name in _BENIGN_DECLINE_FIELDS:
            if getattr(res, name, None):
                declined_benign = True
    return fault_seen or not declined_benign


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _live_chat_participants(
        self, platform: Any, chat_id: str, thread_id: Optional[str] = None,
        chat_type: Optional[str] = None,
        creator_session_key: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> set[_WakeRoutingIdentity]:
        """Complete identities the gateway has resolved for this chat.

        Evidence for :func:`resolve_wake_participant`. Read from the live
        ``session_store`` routing index — the same structure ``notify-repair``
        mines out of ``state.db``'s ``gateway_routing`` table, except in-process
        and current, so a wake can use it BEFORE a repair pass ever runs.

        Only entries whose key shape proves the required identity count.
        Deliberately narrow:

        * ``origin.user_id_alt or origin.user_id`` must be present — that is the
          participant segment selected by ``build_session_key``. An entry with
          neither is exactly the phantom we're refusing to feed.
        * the entry's own chat and chat type must match, so a participant in
          another channel or a group entry sharing a DM id can never be adopted.
        * group/channel and no-chat DM entries must be keyed per-user
          (``key.endswith(":" + participant)``). A shared thread session
          legitimately has no participant segment; adopting one would route to
          a per-user key nobody in that thread reads.
        * Slack DMs with a chat id intentionally omit the participant from the
          key. Their exact ``slack:dm:<scope>:<chat>[:<thread>]`` tail instead
          proves the workspace scope the wake needs to reconstruct that key.

        The key's own shape is the honest test — reading config flags would
        ignore ``thread_sessions_per_user`` and the DM/thread branches of
        ``build_session_key``.

        * the entry's key must sit in the namespace the wake will key into
          (``profile``; ``agent:main`` for the default). In a multiplexed
          gateway two profiles can hold different participants in one chat;
          another profile's participant is not evidence for this one, and
          counting it turns a single real match into a 2-way refusal or adopts
          a foreign identity (t_51b6e95f). With multiplexing off every key is
          ``agent:main`` and nothing is filtered.

        Returns complete ``(user_id, user_id_alt, scope_id)`` tuples rather than
        participant strings so alternate ids and Slack workspace scope cannot be
        selected from different entries. Returns a SET, never a pick: the caller
        refuses on 0 and on >1. Never raises — a routing-index read failure
        degrades to "no evidence", preserving the pre-change behaviour exactly.
        """
        store = getattr(self, "session_store", None)
        if store is None or not chat_id:
            return set()
        platform_value = getattr(platform, "value", platform)
        want_chat = str(chat_id)
        want_thread = str(thread_id or "")
        want_chat_type = str(chat_type or "")
        # 🔴 BOTH operands of the lane comparison must be canonicalized.
        # ``origin_lane`` below runs through ``effective_routing_lane``, which
        # rewrites a Discord guild ``channel`` to ``group`` (the spelling
        # ``build_session_key`` actually keys on). Building this side from the
        # RAW subscription string made a ``channel``-spelled row structurally
        # unmatchable — a 0% match rate, not a flaky one — so the wake found no
        # identity, keyed a bare ``group:<chat>`` session and minted the
        # phantom. #659 linted the SOURCE axis (no adapter may label a Discord
        # chat ``channel``); rows persisted before that, and any non-adapter
        # producer, still arrive spelled the legacy way. Normalizing one side
        # of a compared pair is the whole bug (2026-09-12, Discord #curator).
        want_lane = effective_routing_lane(
            platform=platform_value,
            chat_id=want_chat,
            chat_type=want_chat_type,
            thread_id=want_thread,
        )
        want_creator_key = str(creator_session_key or "")
        want_ns_prefix = ""
        resolve_key_profile = getattr(store, "_resolve_profile_for_key", None)
        try:
            from gateway.session import SessionSource, _session_key_namespace
            key_profile = resolve_key_profile(
                SessionSource(platform=platform, chat_id=want_chat,
                              profile=profile or None)
            ) if callable(resolve_key_profile) else None
            if key_profile is not None:
                want_ns_prefix = _session_key_namespace(key_profile) + ":"
        except Exception as exc:
            logger.debug(
                "kanban notifier: profile namespace unresolved for %s/%s: %s",
                platform_value, want_chat, exc,
            )
            return set()
        found: set[_WakeRoutingIdentity] = set()
        try:
            with store._lock:  # noqa: SLF001 -- documented private access
                store._ensure_loaded_locked()  # noqa: SLF001
                entries = dict(store._entries)  # noqa: SLF001
        except Exception as exc:
            logger.debug(
                "kanban notifier: routing index unavailable for %s/%s: %s",
                platform_value, want_chat, exc,
            )
            return set()
        creator_found: set[_WakeRoutingIdentity] = set()
        # ``tasks.session_id`` holds the creating turn's session KEY for
        # gateway-created tasks (always contains ':') but a RAW session id for
        # worker/CLI-created ones (never does). #568 compared it against
        # routing-index keys unconditionally, so every worker card got empty
        # evidence and the #562 prevention became dead code (the 2026-08-12
        # phantom regression). Discriminate:
        # * key-shaped creator -> strict binding stands (#568 intent): only
        #   the creator's own entry is evidence; a creator entry without an
        #   identity (the shared per-chat session) correctly yields none.
        # * raw-id creator -> match entries by their session_id; when the
        #   creator is unknown to the index (worker sessions never route),
        #   fall back to the lane-wide exactly-one rule (#562), which still
        #   refuses on 0 or >1 participants.
        creator_is_key = creator_stamp_is_session_key(want_creator_key)
        for key, entry in entries.items():
            if want_ns_prefix and not str(key).startswith(want_ns_prefix):
                continue
            is_creator = bool(want_creator_key) and (
                str(key) == want_creator_key
                if creator_is_key
                else str(getattr(entry, "session_id", "") or "")
                == want_creator_key
            )
            origin = getattr(entry, "origin", None)
            if origin is None:
                continue
            user_id = str(getattr(origin, "user_id", "") or "").strip()
            user_id_alt = str(
                getattr(origin, "user_id_alt", "") or ""
            ).strip()
            scope_id = str(
                getattr(origin, "scope_id", "")
                or getattr(origin, "guild_id", "")
                or ""
            ).strip()
            participant = user_id_alt or user_id
            if not participant:
                continue
            origin_platform = getattr(origin, "platform", None)
            if str(getattr(origin_platform, "value", origin_platform)) != str(
                platform_value
            ):
                continue
            origin_chat_type = str(
                getattr(origin, "chat_type", "") or ""
            )
            origin_lane = effective_routing_lane(
                platform=origin_platform,
                chat_id=getattr(origin, "chat_id", None),
                chat_type=origin_chat_type,
                thread_id=getattr(origin, "thread_id", None),
                prospective_thread_id=getattr(
                    origin, "prospective_thread_id", None
                ),
            )
            if origin_lane != want_lane:
                continue
            if not routing_key_carries_identity(
                key,
                platform=origin_platform,
                chat_id=want_chat,
                chat_type=origin_chat_type,
                thread_id=want_thread,
                prospective_thread_id=getattr(
                    origin, "prospective_thread_id", None
                ),
                user_id=user_id,
                user_id_alt=user_id_alt,
                scope_id=scope_id,
            ):
                continue
            identity = _WakeRoutingIdentity(user_id, user_id_alt, scope_id)
            found.add(identity)
            if is_creator:
                creator_found.add(identity)
        if creator_is_key:
            # Gateway-created task: the creator's own entry is the ONLY valid
            # evidence (#568). A shared-session creator yields none — correct.
            return creator_found
        # Worker/CLI-created (raw id) or unstamped task: bind to the creator's
        # entry when the index knows it, else the lane-wide exactly-one rule
        # (#562) — the caller still refuses on 0 or >1.
        return creator_found or found

    def _build_wake_source(
        self,
        plat: Any,
        adapter: Any,
        sub: dict,
        *,
        profile: Optional[str] = None,
        creator_session_key: Optional[str] = None,
    ) -> "tuple[Any, _WakeRoutingIdentity]":
        """Build the ``SessionSource`` a push wake is delivered as.

        One routing rule for every wake entry point: the kanban notifier's
        ``_push_wake`` (``sub`` = a ``kanban_notify_subs`` row) and the
        control-socket ``wake`` verb (``sub`` = the request's origin fields).
        Returns ``(source, identity)`` so callers can log an adopted identity.

        With no ``profile``, the chat's ``profile_routes`` match is resolved
        HERE and stamped on the source, so participant lookup and the eventual
        dispatch key into the same namespace. Left unset, the lookup searched
        the default namespace while ingress routed the wake to the route's
        profile and keyed a participant-less phantom there (t_51b6e95f). A
        route to an unserved profile raises ``ProfileRouteRejected``, the
        same fail-closed outcome inbound traffic gets.
        """
        from gateway.session import SessionSource

        if not profile:
            route_profile = getattr(self, "_profile_name_for_source", None)
            if callable(route_profile):
                profile = route_profile(SessionSource(
                    platform=plat,
                    chat_id=sub["chat_id"],
                    chat_type=str(sub.get("chat_type") or "") or "group",
                    thread_id=sub.get("thread_id") or None,
                    scope_id=sub.get("scope_id") or None,
                )) or None

        # Rebuild the creator's real session scope from the chat_type
        # persisted on the subscription row (#56580). build_session_key()
        # keys DMs (":dm:<chat_id>") on a wholly different shape from
        # group/thread, so the old hardcoded "group" mis-routed DM/thread
        # creators into a fresh session. Legacy rows written before the
        # column existed may still carry chat_type in delivery_metadata
        # (#60600 rows) — fall back to that, then to "group" (the historical
        # default that suits the dashboard/group flows). handle_message()
        # get_or_create_session's the target, so a mismatch only ever
        # degrades to a fresh session, never an exception.
        chat_type = str(sub.get("chat_type") or "").strip()
        if not chat_type:
            delivery_meta = sub.get("delivery_metadata")
            if isinstance(delivery_meta, dict):
                chat_type = str(delivery_meta.get("chat_type") or "").strip()
        chat_type = chat_type or "group"
        # PREVENTION (card 5 / #555 follow-up): a row with no participant
        # makes build_session_key() drop the participant segment, so the
        # wake opens a SECOND, chat-unreachable session key for this chat —
        # the phantom. #555 can only repair such a row after the fact; by
        # then the phantom already exists. Resolve it here instead, from the
        # gateway's own live routing index, before the key is ever built.
        # Refuses (leaves user_id None, delivering to the shared per-chat
        # session) unless exactly one per-user participant is known for this
        # chat — see resolve_wake_participant. The subscription's own scope
        # falls back to the adapter/metadata scope (upstream's
        # _wake_scope_id) so Slack wakes key the workspace either way.
        identity = resolve_wake_identity(
            sub.get("user_id"),
            sub.get("user_id_alt"),
            sub.get("scope_id") or _wake_scope_id(adapter, sub),
            self._live_chat_participants(
                plat,
                sub["chat_id"],
                sub.get("thread_id") or None,
                chat_type,
                creator_session_key,
                profile,
            ),
        )
        source = SessionSource(
            platform=plat,
            chat_id=sub["chat_id"],
            chat_type=chat_type,
            thread_id=sub.get("thread_id") or None,
            user_id=identity.user_id or None,
            user_id_alt=identity.user_id_alt or None,
            profile=profile or None,
            scope_id=identity.scope_id or None,
        )
        return source, identity

    async def _deliver_control_wake(self, params: dict) -> dict:
        """Run one cross-process wake (control-socket ``wake`` verb).

        ``params``: platform, chat_id, chat_type, text (required); thread_id,
        user_id, user_id_alt, scope_id, profile (optional). Routing is the
        notifier's (``_build_wake_source``: live-routing identity fill,
        phantom refusal), delivery is ``gateway.wake.deliver_wake``. Answers
        ``{"delivered": bool, "error": str}`` and never raises.
        """
        from gateway.config import Platform as _Platform
        from gateway.wake import adapter_supports_push, deliver_wake

        def _fail(error: str) -> dict:
            logger.warning("control wake refused: %s", error)
            return {"delivered": False, "error": error}

        platform_str = str(params.get("platform") or "").strip().lower()
        chat_id = str(params.get("chat_id") or "").strip()
        chat_type = str(params.get("chat_type") or "").strip()
        text = str(params.get("text") or "")
        profile = str(params.get("profile") or "").strip()
        if not platform_str or not chat_id or not text.strip():
            return _fail("platform, chat_id and text are required")
        if not chat_type:
            # A guessed chat_type keys a different session shape (DM vs
            # group) and wakes a phantom; the caller must know it.
            return _fail("chat_type is required")
        try:
            plat = _Platform(platform_str)
        except ValueError:
            return _fail(f"unknown platform {platform_str!r}")
        adapter = self._authorization_adapter(
            plat, None if profile in ("", "default") else profile
        )
        if adapter is None:
            return _fail(
                f"no connected {platform_str} adapter for profile "
                f"{profile or 'default'}"
            )
        if not adapter_supports_push(adapter):
            return _fail(f"{platform_str} adapter cannot push a wake turn")
        sub = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "thread_id": str(params.get("thread_id") or "").strip(),
            "user_id": str(params.get("user_id") or "").strip(),
            "user_id_alt": str(params.get("user_id_alt") or "").strip(),
            "scope_id": str(params.get("scope_id") or "").strip(),
        }
        try:
            source, identity = self._build_wake_source(
                plat, adapter, sub, profile=profile or None,
            )
            await deliver_wake(adapter, text=text, source=source)
        except Exception as exc:
            logger.warning(
                "control wake failed on %s/%s: %s", platform_str, chat_id, exc,
                exc_info=True,
            )
            return {"delivered": False, "error": f"{type(exc).__name__}: {exc}"}
        logger.info(
            "control wake: woke %s/%s thread=%s profile=%s participant=%s",
            platform_str, chat_id, sub["thread_id"] or "-",
            profile or "default", identity.participant or "-",
        )
        return {
            "delivered": True,
            "error": "",
            "chat_type": source.chat_type,
            "user_id": source.user_id or "",
        }

    def _owns_kanban_dispatcher_lock(self) -> bool:
        """Return whether this gateway currently owns the singleton lock."""
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)
    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``,
        ``review_requested``, ``changes_requested``,
        ``block_loop_detected``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. The subscription is removed after that
        delivery once the task is ``done`` or ``archived``
        (``kanban_db.NOTIFY_SUB_FINAL_STATUSES``): the terminal line arrives,
        then the row is gone so it can never wake the origin session again.
        A controller that reopens a ``done`` card re-subscribes explicitly.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Each gateway polls only subscriptions owned by profiles whose
        adapters it hosts. The dispatch-owning gateway also handles legacy
        subscriptions without a profile stamp.
        """
        # Dispatch and delivery have separate ownership. A deployment may run
        # one dispatcher while each profile has its own gateway credentials;
        # those adapter-owning gateways must still poll and deliver their own
        # subscriptions. Legacy rows without a notifier_profile are visible
        # only while this process holds the actual singleton dispatcher lock.
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
            # Upstream decomposed the facade without re-exports: notify helpers live here.
            from hermes_cli import kanban_db_notify as _kbn
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        try:
            from hermes_cli.config import load_config as _load_config

            cfg = _load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); continuing enabled", exc)
            kanban_cfg = {}
        if not kanban_cfg.get("notify_in_gateway", True):
            logger.info("kanban notifier: disabled via config kanban.notify_in_gateway=false")
            return

        # "status" covers dashboard drag-drop and `_set_status_direct()`
        # writes — surface those transitions to subscribers too.
        # ``review_requested`` wakes the origin subscriber like a block does,
        # but is not a block (see kanban_db.request_review); the task is not
        # archived, so the subscription stays alive and later review
        # cycles keep notifying.
        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "stalled", "status", "archived", "unblocked", "block_loop_detected", "review_requested", "changes_requested")
        # Subscriptions are removed after delivery once the task is done or
        # archived (kanban_db.NOTIFY_SUB_FINAL_STATUSES, t_6d6e9467). We used
        # to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is archived lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        # Raised from 3 to 12 (~60s at the 5s tick cadence): now that a
        # reported SendResult(success=False) also lands here (see the
        # delivery loop below), a transient Telegram/API outage of a few
        # ticks must NOT permanently unsubscribe a live review-gate channel.
        # A genuinely dead chat still drops, just ~60s later — a fine trade
        # for an unattended gate where a false drop means silent work pileup.
        MAX_SEND_FAILURES = 12
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        from gateway.kanban_notify_failures import (
            LaneFailureDedupe,
            format_failure_notice,
            format_timed_out_notice,
        )

        lane_dedupe: LaneFailureDedupe = getattr(self, "_kanban_lane_dedupe", None) or LaneFailureDedupe()
        self._kanban_lane_dedupe = lane_dedupe
        from gateway.kanban_lifecycle_digest import LifecycleDigest

        lifecycle_digest: LifecycleDigest = (
            getattr(self, "_kanban_lifecycle_digest", None) or LifecycleDigest()
        )
        self._kanban_lifecycle_digest = lifecycle_digest
        from gateway import kanban_home_route as _hr

        home_cache: _hr.HomeCache = getattr(self, "_kanban_home_cache", None) or _hr.HomeCache()
        self._kanban_home_cache = home_cache
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC cadence. Subscriptions survive ``done`` (it is
        # reversible), so boards that never archive would otherwise
        # accumulate rows scanned on every 5s tick forever. The sweep is a
        # single DELETE per board, gated to once per watcher startup and at
        # most once per hour thereafter — cheap relative to the tick's own
        # per-sub claims. Retention is kanban.done_sub_retention_days in
        # config.yaml (default 30; 0 disables), re-read at each sweep so a
        # config change applies without a restart.
        _GC_INTERVAL_SECONDS = 3600.0
        _gc_next_at = 0.0  # 0 → sweep on the first tick after startup

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _gc_retention_days = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    try:
                        from hermes_cli.config import load_config as _load_cfg

                        _kanban_cfg = (_load_cfg() or {}).get("kanban") or {}
                        _gc_retention_days = int(
                            _kanban_cfg.get("done_sub_retention_days", 30)
                        )
                    except Exception:
                        # Fail safe on the shipped default; the sweep itself
                        # treats <= 0 as disabled.
                        _gc_retention_days = 30

                def _collect():
                    deliveries: list[dict] = []
                    # t_dcc4ed08: one memoised REST oracle per tick for the
                    # landed-close-gate check on changes_requested events.
                    from gateway import kanban_close_gate as _cg
                    from hermes_cli import kanban_open_pr as _open_pr

                    _cg_query = getattr(self, "_kanban_close_gate_query", None) or _open_pr.memo_query()
                    include_unowned = self._owns_kanban_dispatcher_lock()
                    notifier_profiles = {notifier_profile}
                    notifier_profiles.update(
                        str(profile).strip()
                        for profile in getattr(self, "_profile_adapters", {})
                        if str(profile).strip()
                    )
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    # Widen to every platform any secondary profile has live,
                    # not just the default profile's. This is only a coarse
                    # pre-filter to skip claiming events for subs nobody can
                    # possibly deliver — the precise per-profile check (via
                    # gateway/authz_mixin.py::_authorization_adapter, which
                    # forbids default-profile fallback) still runs at delivery
                    # time below, rewinding the claim if it resolves to None.
                    # Without this, a subscription owned by a secondary
                    # profile on a platform the DEFAULT profile never
                    # connected (e.g. beta owns discord, default doesn't) was
                    # dropped here before ever being claimed — no rewind
                    # applies to an unclaimed event, so it silently never
                    # retries.
                    for _profile_adapter_map in getattr(self, "_profile_adapters", {}).values():
                        active_platforms.update(
                            getattr(platform, "value", str(platform)).lower()
                            for platform in _profile_adapter_map.keys()
                        )
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    # The extent spans each loop BODY, not just the path
                    # resolve: the body's `count_notify_subs(board=slug)` and
                    # `connect(board=slug)` re-resolve internally and would
                    # otherwise land outside it.
                    for board_meta in _kb.enumerating_each(boards):
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        # Zero-subscription early exit: probe the board with a
                        # cheap read-only connection BEFORE the writable
                        # `connect()`. A board with no subscriptions has
                        # nothing to notify, and the writable open (schema
                        # init/migration on first open, WAL/-shm sidecars,
                        # checkpoint traffic) is exactly the per-tick cost
                        # this skip avoids.
                        try:
                            from hermes_cli import kanban_db_notify as _kbn
                            if _kbn.count_notify_subs(
                                board=slug,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            ) == 0:
                                logger.debug(
                                    "kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                                    slug, sorted(notifier_profiles),
                                )
                                continue
                        except Exception as exc:
                            logger.debug(
                                "kanban notifier: read-only subscription probe failed "
                                "for board %s (%s); falling back to writable open",
                                slug, exc,
                            )
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            if _gc_due:
                                # Hourly (plus once at startup) stale-sub GC:
                                # drop subscriptions for tasks that have been
                                # ``done`` untouched past the retention
                                # window. Best-effort — a failed sweep never
                                # blocks delivery; the next hourly gate
                                # retries it.
                                try:
                                    _purged = _kbn.purge_stale_done_notify_subs(
                                        conn,
                                        max_age_days=_gc_retention_days,
                                    )
                                    if _purged:
                                        logger.info(
                                            "kanban notifier: purged %d stale done-task subscription(s) on board %s (retention %dd)",
                                            _purged, slug, _gc_retention_days,
                                        )
                                except Exception as _gc_exc:
                                    logger.debug(
                                        "kanban notifier: stale-sub GC failed for board %s: %s",
                                        slug, _gc_exc,
                                    )
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kbn.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            )
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                try:
                                    owner_profile = sub.get("notifier_profile") or None
                                    platform = (sub.get("platform") or "").lower()
                                    if platform not in active_platforms:
                                        logger.debug(
                                            "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                            sub.get("task_id"), platform or "<missing>",
                                        )
                                        continue
                                    # Durable route check BEFORE claiming (upstream
                                    # bdd7192bcf/5579fd5cdf): a served profile with no
                                    # adapter of its own may still deliver through the
                                    # primary bot (profile_routes-pinned chat, or an
                                    # api_server session its own store owns); one that
                                    # resolves to nothing is skipped unclaimed.
                                    try:
                                        _route_plat = _Platform(platform)
                                    except ValueError:
                                        _route_plat = None
                                    if _route_plat is not None and _adapter_for_subscription(
                                        self, _route_plat, sub, owner_profile or notifier_profile,
                                    ) is None:
                                        logger.debug(
                                            "kanban notifier: subscription for %s owned by profile %s has no deliverable adapter on %s; skipping",
                                            sub.get("task_id"), owner_profile or notifier_profile, platform,
                                        )
                                        continue
                                    old_cursor, cursor, events = _kbn.claim_unseen_events_for_sub(
                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    if not events:
                                        continue
                                    task = _kb.get_task(conn, sub["task_id"])
                                    # Ping carries the card's home so a session
                                    # receiving a forwarded ping can tell whether
                                    # the card is its own. Resolved here (worker
                                    # thread), never on the event loop.
                                    # t_808bc8e6: the same read also resolves the
                                    # card's home CHAT, cached per card.
                                    _home_sid = getattr(task, "session_id", None) if task else None
                                    _home_row, home_res = home_cache.get(slug, sub["task_id"], _home_sid)
                                    home_line = format_home_line(_home_sid, _home_row)
                                    close_gates: dict = {}
                                    for _ev in events:
                                        if _ev.kind != "changes_requested":
                                            continue
                                        try:
                                            _g = _cg.classify(_kb, conn, sub["task_id"], _ev, _cg_query)
                                        except Exception as _cg_exc:
                                            logger.debug("kanban notifier: close-gate read failed for %s: %s",
                                                         sub["task_id"], _cg_exc)
                                            _g = None
                                        if _g:
                                            close_gates[_ev.id] = _g
                                    logger.debug(
                                        "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                        len(events), sub["task_id"], slug, old_cursor, cursor,
                                    )
                                    deliveries.append({
                                        "sub": sub,
                                        "old_cursor": old_cursor,
                                        "cursor": cursor,
                                        "events": events,
                                        "task": task,
                                        "board": slug,
                                        "home": home_line,
                                        "home_res": home_res,
                                        "close_gates": close_gates,
                                        "self_event_ids": self_caused_event_ids(sub, events),
                                    })
                                except Exception as sub_exc:
                                    # Isolate per-subscription failures so one
                                    # bad subscription cannot block delivery for
                                    # all other subscriptions in this tick.
                                    logger.warning(
                                        "kanban notifier: subscription for %s on board %s failed: %s",
                                        sub.get("task_id"), slug, sub_exc,
                                    )
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                lifecycle_channel = _resolve_lifecycle_channel() if deliveries else None
                # kanban.lifecycle_digest_seconds (t_d62bd921): batch routed lines.
                _digest_window = (
                    _resolve_lifecycle_digest_seconds() if lifecycle_channel else 0
                )
                # t_808bc8e6: routed lines go to the card's home chat; the
                # lifecycle channel is the fallback. Home lines fold on their
                # own (short) window, one digest per home chat.
                _route_mode, _home_window = (
                    _hr.resolve_route_config() if lifecycle_channel else (_hr.ROUTE_CHANNEL, 0)
                )
                # (board, task, event id, channel) already posted to the
                # lifecycle channel; bounded, survives across ticks so a
                # rewound sibling sub cannot re-post the same receipt.
                lifecycle_sent = getattr(self, "_kanban_lifecycle_sent", None)
                if lifecycle_sent is None:
                    lifecycle_sent = {}
                    self._kanban_lifecycle_sent = lifecycle_sent
                # One message per failure event, one per lane-wide cause.
                lane_dedupe.plan(deliveries)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = _adapter_for_subscription(self, plat, sub, sub_profile or None)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    if (
                        lifecycle_channel is not None
                        and _route_mode == _hr.ROUTE_HOME
                        and _hr.home_lookup_failed(d.get("home_res"))
                        and any(ev.kind in _hr.HOME_ROUTE_KINDS for ev in d["events"])
                    ):
                        # state.db unreadable: the home is unknown, not absent.
                        # Leave the claim unacked (never [no-home]); the next
                        # tick re-resolves (Prism P1, t_04013ffa).
                        logger.warning(
                            "kanban notifier: home lookup for %s failed; rewinding claim to retry",
                            sub["task_id"],
                        )
                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    # Per-subscription failure-counter key. Hoisted out of the
                    # event loop: the wake self-post path (in the loop's
                    # ``else`` clause) needs it even when every event in the
                    # claim was skipped before reaching the send site.
                    sub_key = (
                        sub["task_id"], sub["platform"],
                        sub["chat_id"], sub.get("thread_id") or "",
                    )
                    mode = sub.get("delivery_mode") or "notify"
                    wake_agent = mode in ("notify+wake", "wake")
                    send_passive = mode != "wake"
                    # Worker handoff carried into the synthetic wake turn below
                    # (#70752): without it the woken creator only sees
                    # "Task X completed" and re-decomposes work that already
                    # exists on the board.
                    wake_handoff = ""
                    wake_review_detail = ""
                    # Events whose passive line was confirmed delivered; only
                    # these may have their wake skipped as a self-echo.
                    _sent_event_ids: set[int] = set()
                    for ev in d["events"]:
                        kind = ev.kind
                        lane_key = None
                        _fold = None  # t_dcc4ed08: operator-batch fold key for the digest
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                                wake_handoff = h
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                                wake_handoff = r
                            superseded_by = (
                                ev.payload.get("superseded_by") if ev.payload else None
                            )
                            if superseded_by:
                                # A card whose premise was already satisfied is
                                # NOT the same shape as one whose work this
                                # worker did. Name the evidence, and do not let
                                # it read as a crash — before this disposition
                                # existed the same situation arrived as
                                # "gave up (retries exhausted)".
                                msg = (
                                    f"↩️ {board_tag}{tag}Kanban {sub['task_id']} closed"
                                    f" — premise superseded by "
                                    f"{str(superseded_by)[:160]} — {title}{handoff}"
                                )
                            else:
                                msg = (
                                    f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                    f" — {title}{handoff}"
                                )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            if (ev.payload or {}).get("stopped_early") == "reproduced_clean_exit":
                                # NOT a crash: the worker exited cleanly with
                                # nothing to do, twice identically. "gave up
                                # after repeated spawn failures" sent operators
                                # hunting a failure that never happened.
                                repeats = int((ev.payload or {}).get("identical_violations") or 2)
                                msg = (
                                    f"🧭 {board_tag}{tag}Kanban {sub['task_id']} needs input: "
                                    f"its worker finished with NOTHING TO DO {repeats}x "
                                    f"identically (no crash) — retrying reproduces it. If the "
                                    f"card's premise was already satisfied, close it with "
                                    f"`hermes kanban complete {sub['task_id']} --superseded-by "
                                    f"<card|PR|sha>`; otherwise re-scope it.{err}"
                                )
                            else:
                                directive = d["failure_directives"].get(ev.id)
                                if directive is None:
                                    continue
                                msg = format_failure_notice(
                                    kind, ev.payload, task_id=sub["task_id"],
                                    board_tag=board_tag, tag=tag,
                                    assignee=who or "", directive=directive,
                                )
                                lane_key = directive.get("lane_key")
                        elif kind == "crashed":
                            directive = d["failure_directives"].get(ev.id)
                            if directive is None:
                                continue
                            msg = format_failure_notice(
                                kind, ev.payload, task_id=sub["task_id"],
                                board_tag=board_tag, tag=tag,
                                assignee=who or "", directive=directive,
                            )
                            lane_key = directive.get("lane_key")
                        elif kind == "timed_out":
                            msg = format_timed_out_notice(
                                ev.payload, task_id=sub["task_id"],
                                board_tag=board_tag, tag=tag,
                            )
                        elif kind == "stalled":
                            age = int((ev.payload or {}).get("progress_age_seconds") or 0)
                            msg = (
                                f"⚠ {board_tag}{tag}Kanban {sub['task_id']} worker stalled "
                                f"({age}s no progress despite heartbeats); "
                                "dispatcher will reclaim if still idle"
                            )
                        elif kind == "status":
                            new_status = ""
                            if ev.payload and ev.payload.get("status"):
                                new_status = str(ev.payload["status"])
                            msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
                        elif kind == "review_requested":
                            # Implementation complete; task moved to the
                            # first-class review lane. Wake the origin thread.
                            handoff = ""
                            if ev.payload and ev.payload.get("summary"):
                                summary = str(ev.payload["summary"])
                                handoff = f"\n{summary[:200]}"
                                # Carry the worker's handoff into the wake turn
                                # like ``completed`` does: a reviewer woken with
                                # a bare "ready for review" has to re-read the
                                # board to learn what was implemented.
                                lines = summary.strip().splitlines()
                                wake_handoff = (
                                    lines[0][:200] if lines else summary[:200]
                                )
                            msg = (
                                f"👀 {board_tag}{tag}Kanban {sub['task_id']} ready for review"
                                f" — {title}{handoff}"
                            )
                        elif kind == "changes_requested":
                            payload = ev.payload or {}
                            reason = _safe_review_reason(payload.get("reason"))
                            reviewer = _safe_review_reason(payload.get("reviewer"), 48)
                            implementer = _safe_review_reason(payload.get("implementer"), 48)
                            reason_text = reason or "reviewer feedback requires changes"
                            _gate = (d.get("close_gates") or {}).get(ev.id)
                            if _gate:
                                # t_dcc4ed08: the PR landed; the card waits on a
                                # native close gate. Not a block, not a send-back.
                                gate_text = _safe_review_reason(_gate.get("gate")) or reason_text
                                msg = (
                                    f"⏳ {board_tag}Kanban {sub['task_id']} landed · close on: "
                                    f"{gate_text}"
                                    + (f" — implementer @{implementer}" if implementer else "")
                                )
                                _fold = {
                                    "key": f"close-gate:{_gate['batch']}" if _gate.get("batch") else None,
                                    "task_id": sub["task_id"], "gate": gate_text,
                                    "implementer": implementer, "board_tag": board_tag,
                                    "batch": _gate.get("batch") or "",
                                }
                                wake_review_detail = f"landed; close on: {gate_text}"
                            else:
                                provenance = ""
                                if reviewer:
                                    provenance += f" — reviewer @{reviewer}"
                                if implementer:
                                    provenance += f" → implementer @{implementer}"
                                msg = (
                                    f"🛑 {board_tag}Kanban {sub['task_id']} review requested "
                                    f"changes/BLOCK: {reason_text}{provenance}"
                                )
                                wake_review_detail = reason_text
                        elif kind == "block_loop_detected":
                            # A task re-blocked for the same cause past the
                            # recurrence limit and was routed to `triage` for a
                            # human decision. This is the ONE transition that
                            # exists to force human attention, yet it emits no
                            # `blocked`/`status` event — so before adding it to
                            # TERMINAL_KINDS it produced zero notification and
                            # the task stalled in triage silently. Ping loudly.
                            reason = ""
                            recurrences = None
                            if ev.payload:
                                if ev.payload.get("reason"):
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                                recurrences = ev.payload.get("recurrences")
                            rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
                            msg = (
                                f"🛑 {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
                                f" — needs a human decision{rc}{reason}"
                            )
                        else:
                            # archived / unblocked are claimed by TERMINAL_KINDS
                            # (so the cursor advances past them and they can't
                            # wedge a later completed/blocked event behind an
                            # unclaimed row) but are intentionally SILENT: an
                            # archive needs no user ping, and unblocked is an
                            # internal transition. They are also excluded from
                            # _WAKE_KINDS below, so they never wake the creator.
                            continue
                        if d.get("home"):
                            msg += "\n" + d["home"]
                        delivery_metadata = sub.get("delivery_metadata")
                        metadata: dict[str, Any] = (
                            dict(delivery_metadata)
                            if isinstance(delivery_metadata, dict)
                            else {}
                        )

                        if sub.get("thread_id") and not metadata.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        # kanban.lifecycle_channel (t_f7bba206): done / ready
                        # for review / blocked lines go to a log channel; the
                        # wake below still targets the subscriber.
                        send_adapter, send_chat_id = adapter, sub["chat_id"]
                        _route = _hr.route_lifecycle_line(
                            lifecycle_channel, _route_mode, kind, ev.payload, task,
                            d.get("home_res"),
                        )
                        _routed = _route.target
                        _route_tag = _route.tag
                        _route_window = _home_window if _route.is_home else _digest_window
                        # (target, adapter, window) for a home whose send fails.
                        _fallback = None
                        if _routed is not None:
                            _routed_adapter = self._kanban_route_adapter(_routed[0], sub_profile)
                            if _route.is_home and _route.fallback is not None:
                                _fb_adapter = self._kanban_route_adapter(
                                    _route.fallback[0], sub_profile,
                                )
                                if _fb_adapter is not None:
                                    _fallback = (_route.fallback, _fb_adapter, _digest_window)
                                if _routed_adapter is None and _fallback is not None:
                                    # This gateway cannot post to the home
                                    # platform at all: fall back now.
                                    _routed, _routed_adapter, _route_window = _fallback
                                    _route_tag = _hr.unreachable_tag("no-adapter")
                                    _fallback = None
                                    _route = _route._replace(is_home=False, thread_id="")
                            if _routed_adapter is not None:
                                send_adapter, send_chat_id = _routed_adapter, _routed[1]
                                metadata = {"thread_id": _route.thread_id} if _route.thread_id else {}
                                msg = _hr.format_for_platform(
                                    _routed[0], _hr.tag_line(msg, _route_tag),
                                )
                            else:
                                _routed = None
                        # Handoff artifacts upload only beside a line posted
                        # NOW to a conversation (subscriber chat or an
                        # immediate home line). A log-channel line or a held
                        # digest line names the files instead (t_4bfc46a3:
                        # 45 empty-content uploads/24 h in #logs).
                        _art_paths: list[str] = []
                        _art_upload = False
                        if kind in ("completed", "review_requested"):
                            try:
                                _art_paths = self._kanban_artifact_paths(
                                    send_adapter, getattr(ev, "payload", None), task,
                                )
                            except Exception as art_exc:
                                logger.debug(
                                    "kanban notifier: artifact scan for %s failed: %s",
                                    sub["task_id"], art_exc,
                                )
                            _art_upload = bool(_art_paths) and (
                                _routed is None or (_route.is_home and _route_window <= 0)
                            ) and (
                                (_routed[0] if _routed else platform_str, str(send_chat_id))
                                != lifecycle_channel
                            )
                            if _art_paths and not _art_upload:
                                msg = _hr.tag_line(msg, kanban_artifact_names_tag(_art_paths))
                        if _routed is not None:
                            # Receipts land in exactly one place (t_484a3c72):
                            # a card with several subscribers routes the same
                            # event to the log channel once, not once per sub.
                            _lc_key = (board_slug or "", sub["task_id"], ev.id, _routed)
                            if _lc_key in lifecycle_sent:
                                logger.debug(
                                    "kanban notifier: %s event %s for %s already in the lifecycle channel",
                                    kind, ev.id, sub["task_id"],
                                )
                                continue
                        # Adapters with no push channel (the API server —
                        # ``supports_async_delivery = False``) can NEVER
                        # satisfy a text-send: ``send()`` always reports
                        # SendResult(success=False) by design (see
                        # ApiServerAdapter.send()). Treating that as a
                        # delivery failure would rewind/drop the subscription
                        # forever and — because the wake dispatch below lives
                        # in this loop's ``else`` clause — would also make the
                        # wake-on-completion path (the actual fix for the
                        # api_server wrong-session bug) unreachable. So for
                        # non-push adapters, skip the doomed send attempt
                        # entirely: there is nothing to text-notify, the
                        # creator is woken via the self-post below instead.
                        from gateway.wake import adapter_supports_push

                        if not adapter_supports_push(adapter) and wake_agent:
                            logger.debug(
                                "kanban notifier: adapter %s has no push "
                                "channel; skipping text ping for %s, relying "
                                "on wake self-post instead",
                                platform_str, sub["task_id"],
                            )
                            # Do NOT reset the failure counter here: on this
                            # path the wake self-post below IS the delivery,
                            # so the counter is resolved (reset or bumped) by
                            # the self-post outcome, not by skipping the send.
                            continue
                        if not send_passive:
                            # Wake-only subscriptions intentionally skip the
                            # visible platform message. The retained wake path
                            # below is the sole delivery — the failure counter
                            # is resolved (reset or bumped) by the wake
                            # outcome there, not by skipping the send here.
                            continue
                        # A target the adapter classified as gone (deleted
                        # chat/thread) never comes back: drop on the first
                        # failure instead of burning MAX_SEND_FAILURES ticks.
                        _target_gone = False
                        try:
                            async with _served_profile_scope(self, plat, sub, sub_profile):
                                if _routed is not None and _route_window > 0:
                                    # Held for the lifecycle digest: the line is
                                    # recorded, so the event counts as delivered
                                    # (cursor, wake and failure counter unchanged).
                                    # One batch per destination (t_808bc8e6).
                                    lifecycle_digest.add(
                                        _routed, send_adapter, msg, _route_window, time.time(),
                                        metadata=metadata, fallback=_fallback,
                                        fold=_fold if _fold and _fold.get("key") else None,
                                    )
                                    _send_res = None
                                else:
                                    # Pings and artifact uploads read the SUBSCRIBER
                                    # profile's media policy / display language, not the
                                    # launch profile's (upstream 284d220ba4).
                                    try:
                                        _send_res = await send_adapter.send(
                                            send_chat_id, msg, metadata=metadata,
                                        )
                                        _home_fail = (
                                            _hr.send_failed(_send_res) if _fallback is not None else None
                                        )
                                    except Exception as _home_exc:
                                        # A raised home send is a failed home send:
                                        # fall back like success=False (the digest
                                        # path does the same). No fallback: re-raise
                                        # into the failure counter below.
                                        if _fallback is None:
                                            raise
                                        _send_res = None
                                        _home_fail = type(_home_exc).__name__
                                    if _home_fail is not None:
                                        # Home chat gone / bot cannot post: the
                                        # same line goes to the fallback, tagged.
                                        logger.warning(
                                            "kanban notifier: home %s:%s unreachable for %s (%s); "
                                            "falling back to %s:%s",
                                            _routed[0], _routed[1], sub["task_id"], _home_fail,
                                            _fallback[0][0], _fallback[0][1],
                                        )
                                        _fb_msg = _hr.tag_line(msg, _hr.unreachable_tag(_home_fail))
                                        if _art_upload:
                                            # The fallback is the log channel: name
                                            # the files there, upload nothing.
                                            _art_upload = False
                                            _fb_msg = _hr.tag_line(
                                                _fb_msg, kanban_artifact_names_tag(_art_paths),
                                            )
                                        if _fallback[2] > 0:
                                            lifecycle_digest.add(
                                                _fallback[0], _fallback[1], _fb_msg, _fallback[2],
                                                time.time(),
                                            )
                                            _send_res = None
                                        else:
                                            _send_res = await _fallback[1].send(
                                                _fallback[0][1], _fb_msg, metadata={},
                                            )
                                # A SendResult(success=False) without an exception
                                # (returned by push-capable adapters on a genuine
                                # transient failure) must count as a FAILED
                                # delivery — otherwise the cursor advances and the
                                # event is permanently lost. Adapters returning
                                # None (or anything non-SendResult shaped) keep
                                # the legacy "no exception == delivered" contract.
                                if getattr(_send_res, "success", True) is False:
                                    # A gone LOG channel must not drop the
                                    # subscriber's sub (its wake still matters).
                                    _target_gone = _routed is None and (
                                        getattr(_send_res, "error_kind", None) == "not_found"
                                    )
                                    raise RuntimeError(
                                        "adapter send() reported failure: "
                                        f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                                    )
                                logger.debug(
                                    "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                    kind, sub["task_id"], platform_str, send_chat_id, board_slug,
                                )
                                _sent_event_ids.add(ev.id)
                                if _routed is not None:
                                    lifecycle_sent[_lc_key] = None
                                    while len(lifecycle_sent) > 4096:
                                        lifecycle_sent.pop(next(iter(lifecycle_sent)))
                                # After delivering the text notification, surface
                                # any artifact paths the worker referenced in
                                # ``kanban_complete(summary=..., artifacts=[...])``
                                # (or the legacy ``result`` field) as native
                                # uploads. ``extract_local_files`` finds bare
                                # absolute paths in the summary;
                                # ``send_document`` / ``send_image_file`` uploads
                                # them. Both handoff kinds stage files for exactly
                                # this: a review-bound card's files exist so the
                                # human sees them at handoff time (f3357b5031).
                                # Retry exposure matches ``completed`` (the sub
                                # cursor is rewound only when a send failed).
                                # Never into the log channel or under a held
                                # line (t_4bfc46a3): ``_art_upload`` gates it.
                                if _art_upload:
                                    try:
                                        await self._deliver_kanban_artifacts(
                                            adapter=send_adapter,
                                            chat_id=send_chat_id,
                                            metadata=metadata,
                                            event_payload=getattr(ev, "payload", None),
                                            task=task,
                                            paths=_art_paths,
                                        )
                                    except Exception as art_exc:
                                        logger.debug(
                                            "kanban notifier: artifact delivery for %s failed: %s",
                                            sub["task_id"], art_exc,
                                        )
                                lane_dedupe.mark_sent(lane_key)
                                # Reset the failure counter on success.
                                sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES or _target_gone:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await _to_thread_process_service(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All text pings delivered (or intentionally skipped
                        # for non-push adapters, whose delivery is the wake
                        # self-post below). Whether the cursor may advance now
                        # depends on the adapter class:
                        #
                        # * push-capable: the text send WAS the delivery, so
                        #   advance immediately (pre-existing behavior); the
                        #   wake injection below stays best-effort.
                        # * non-push (api_server): the wake self-post IS the
                        #   delivery. Advancing first would let a failed /
                        #   retry-exhausted self-post (swallowed by the
                        #   best-effort except) permanently lose the event.
                        #   So the self-post runs FIRST and the cursor only
                        #   advances after it succeeds — a failure rewinds the
                        #   claim exactly like a failed send() above, so the
                        #   next tick retries.
                        # done/archived end subscription ownership once the
                        # claimed events are delivered (t_6d6e9467).
                        task_terminal = _kb.notify_sub_is_final(task)
                        # Kinds that hand a decision back to the origin, so the
                        # origin has to take a turn. ``review_requested`` (the
                        # implementation is done and waits for a reviewer),
                        # ``changes_requested`` (a reviewer BLOCKed and work
                        # returns to the implementer) and ``block_loop_detected``
                        # (routed to triage) belong here for the same reason
                        # ``blocked`` does. ``status`` / ``archived`` /
                        # ``unblocked`` stay out: bookkeeping.
                        _WAKE_KINDS = (
                            "completed", "gave_up", "crashed", "timed_out",
                            "blocked", "review_requested", "changes_requested",
                            "block_loop_detected",
                        )
                        # A transition made from this chat's own session is
                        # not news to it: post the line, skip the wake
                        # (t_a4890a77). Only for events whose passive line was
                        # CONFIRMED sent: delivery_mode='wake', non-push
                        # (api_server) subs and events skipped without a send
                        # never land in _sent_event_ids, so their wake (the
                        # sole delivery) stays.
                        _self_ids = set(d.get("self_event_ids") or ()) & _sent_event_ids
                        _wake_kinds = (
                            {
                                ev.kind for ev in d["events"]
                                if ev.kind in _WAKE_KINDS and ev.id not in _self_ids
                            }
                            if wake_agent
                            else set()
                        )
                        if wake_agent and _self_ids and not _wake_kinds:
                            logger.info(
                                "kanban notifier: wake skipped for %s on %s/%s: "
                                "transition made by this chat's own session",
                                sub["task_id"], platform_str, sub["chat_id"],
                            )
                        from gateway.wake import adapter_supports_push as _adapter_push_ok

                        _is_push_adapter = _adapter_push_ok(adapter)
                        _session_key = ""
                        _synth = ""
                        if _wake_kinds:
                            if _is_push_adapter:
                                _session_key = getattr(task, "session_id", None) or ""
                            else:
                                # Non-push (api_server) wakes go to the
                                # subscription's delivery destination —
                                # sub["chat_id"] IS the raw session id the
                                # subscriber registered with. task.session_id
                                # is worker/creator provenance and may point
                                # at a WORKER session for child tasks with
                                # inherited subscriptions; falling back to it
                                # only when chat_id is empty (legacy rows).
                                _session_key = (
                                    sub["chat_id"]
                                    or getattr(task, "session_id", None)
                                    or ""
                                )
                        if _wake_kinds:
                            _title = (task.title if task else sub["task_id"])[:120]
                            _assignee = task.assignee if task else ""
                            _parts = []
                            if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                            if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                            if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                            if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                            if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                            if "review_requested" in _wake_kinds: _parts.append(t("gateway.kanban.wake.review_requested"))
                            if "changes_requested" in _wake_kinds: _parts.append(t("gateway.kanban.wake.changes_requested"))
                            if "block_loop_detected" in _wake_kinds: _parts.append(t("gateway.kanban.wake.block_loop_detected"))
                            _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                            _synth = t(
                                "gateway.kanban.wake.message",
                                task_id=sub["task_id"],
                                status=_status,
                                title=_title,
                                assignee=_assignee,
                                board=board_slug,
                            )
                            if d.get("home"):
                                _synth += "\n" + d["home"]
                            # Graph-safe wake turn (#70752): carry the worker's
                            # completion handoff into the synthetic turn and
                            # label it as an automatic notification so the woken
                            # creator inspects the board instead of
                            # re-decomposing work that already exists.
                            if wake_handoff:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.handoff",
                                    summary=wake_handoff,
                                )
                            if wake_review_detail:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.review_detail",
                                    reason=wake_review_detail,
                                )
                            _synth += "\n\n" + t(
                                "gateway.kanban.wake.guidance"
                            )

                        if not _is_push_adapter and _wake_kinds and _session_key:
                            # Wake self-post IS the delivery on this path —
                            # it must succeed BEFORE the cursor advances.
                            from gateway.wake import deliver_wake

                            try:
                                # A served profile's raw-session wake runs
                                # in-process under THAT profile's scope (upstream
                                # 5579fd5cdf): the shared listener's /p/<profile>/
                                # self-post would need the profile's own
                                # API_SERVER_KEY, and an unprefixed self-post
                                # would resume the session in the DEFAULT store.
                                async with _served_profile_scope(self, plat, sub, sub_profile) as _served_profile:
                                    await deliver_wake(
                                        adapter,
                                        text=_synth,
                                        session_id=_session_key,
                                        profile=_served_profile,
                                    )
                                logger.info(
                                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                )
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake self-post failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the self-post — the event
                                    # is NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        async def _push_wake() -> None:
                            """Wake the creator session behind a push adapter.

                            Shared by the wake-only (pre-advance, delivery)
                            and notify+wake (post-advance, best-effort)
                            branches below; raises on failure so the caller
                            decides whether to rewind or merely log.
                            """
                            from gateway.wake import deliver_wake

                            _source, _wake_identity = self._build_wake_source(
                                plat, adapter, sub,
                                profile=sub_profile or None,
                                creator_session_key=_session_key,
                            )
                            if _wake_identity.participant and not (
                                sub.get("user_id") or sub.get("user_id_alt")
                            ):
                                logger.info(
                                    "kanban notifier: identity-less sub for "
                                    "%s on %s/%s resolved to participant %s "
                                    "from live routing (phantom session "
                                    "prevented)",
                                    sub["task_id"], platform_str,
                                    sub["chat_id"], _wake_identity.participant,
                                )
                            # deliver_wake preserves the synthetic
                            # MessageEvent/handle_message path for
                            # push-capable adapters (the non-push /
                            # self-post branch is handled BEFORE the
                            # cursor advance above).
                            await deliver_wake(
                                adapter,
                                text=_synth,
                                session_id=_session_key,
                                source=_source,
                            )
                            logger.info(
                                "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                            )

                        if _is_push_adapter and not send_passive and _wake_kinds:
                            # Wake-only (delivery_mode='wake') push sub: the
                            # text ping was intentionally skipped above, so
                            # the wake IS the sole delivery. It must succeed
                            # BEFORE the cursor advances — advancing first
                            # would let a failed wake (previously swallowed
                            # by the best-effort except below) permanently
                            # lose the event. Mirrors the non-push
                            # (api_server) self-post ordering above.
                            try:
                                await _push_wake()
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake-only delivery failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the wake — the event is
                                    # NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        # Delivery complete (text ping for push adapters, wake
                        # self-post for non-push, wake injection for wake-only
                        # push subs): advance cursor. The cursor is the dedup
                        # mechanism — it prevents re-delivery of the same
                        # event on subsequent ticks.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        if not _is_push_adapter:
                            # Nothing left to deliver on this path (the wake,
                            # if any, already succeeded above).
                            sub_fail_counts.pop(sub_key, None)
                        # Unsubscribe once the task is done/archived, AFTER
                        # delivery (every failure path above ``continue``s
                        # before reaching here). A reopened ``done`` card is
                        # re-subscribed explicitly by its controller; leaving
                        # the row would keep waking the origin session.
                        if _is_push_adapter and send_passive and _wake_kinds:
                            # notify+wake: the text ping above was the
                            # delivery and the cursor has advanced; the wake
                            # injection stays best-effort.
                            try:
                                await _push_wake()
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await _to_thread_process_service(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            if len(lifecycle_digest):
                try:
                    await lifecycle_digest.flush(time.time())
                except Exception as exc:
                    logger.warning("kanban lifecycle digest flush failed: %s", exc)
            # t_1ae0c35b: needs_input / precondition blocks, stuck workers,
            # last-blocker-done and red handbacks wake the OWNING operator
            # session with a handoff turn; the lines above still post.
            try:
                from gateway import kanban_owner_wake as _owner_wake

                await _owner_wake.tick(self)
            except Exception as exc:
                logger.warning("kanban owner-wake tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    if len(lifecycle_digest):
                        try:
                            await lifecycle_digest.flush(time.time(), force=True)
                        except Exception:
                            pass
                    return
                await asyncio.sleep(1)

    def _kanban_route_adapter(self, platform: str, profile: Optional[str]):
        """Live adapter for a lifecycle-line destination platform, or None."""
        from gateway.config import Platform

        try:
            return self._authorization_adapter(Platform(platform), profile or None)
        except ValueError:
            return None

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> None:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        conn = _kbc.connect(board=board)
        try:
            getattr(_kbn, op)(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", **extra,
            )
        finally:
            conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    @staticmethod
    def _kanban_artifact_paths(adapter, event_payload: Optional[dict], task) -> list:
        """Existing, delivery-safe artifact paths of a handoff: the payload
        ``artifacts`` list, then paths in the summary, then the legacy
        ``task.result``. Deduplicated; missing files are skipped."""
        raw_paths: list[str] = []
        prose_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                prose_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            prose_paths += adapter.extract_local_files(str(task.result))[0]
        # A staged copy and the scratch original it was copied from are the
        # same deliverable; on a review handoff the original still exists, so
        # prose mentions of it must not upload the file a second time.
        staged_names = {os.path.basename(p) for p in raw_paths}
        raw_paths += [p for p in prose_paths if os.path.basename(p) not in staged_names]
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return []

        from gateway.platforms.base import BasePlatformAdapter
        return BasePlatformAdapter.filter_local_delivery_paths(candidates)

    async def _deliver_kanban_artifacts(
        self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task,
        paths: Optional[list] = None,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        ``paths`` (from ``_kanban_artifact_paths``) skips the scan.
        """
        candidates = (
            list(paths) if paths is not None
            else self._kanban_artifact_paths(adapter, event_payload, task)
        )
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    async def _kanban_dispatcher_watcher(self) -> None:
        """Own leadership until the watcher and any in-flight service work exit."""
        if getattr(self, "_kanban_dispatcher_watcher_active", False):
            logger.warning("kanban dispatcher: watcher already active; ignoring duplicate start")
            return
        if self._owns_kanban_dispatcher_lock():
            logger.error("kanban dispatcher: retained leadership; refusing duplicate start")
            return
        self._kanban_dispatcher_watcher_active = True
        pending = None

        async def service(func, *args):
            nonlocal pending
            pending = asyncio.create_task(_to_thread_process_service(func, *args))
            # Cancelling to_thread cannot stop its worker. Keep its task alive
            # so the lock cannot pass to a standby while it is still writing.
            return await asyncio.shield(pending)

        def release(finished):
            if finished.cancelled():
                # Event-loop teardown may cancel even the shielded service.
                # Its thread can still be writing: retain the lock until process
                # exit rather than allowing another gateway to race that work.
                logger.error(
                    "kanban dispatcher: service cancelled at loop teardown; "
                    "leadership retained until process exit to protect in-flight writes"
                )
                return
            finished.exception()  # consume failures after watcher cancellation
            self._release_kanban_dispatcher_lock()
            self._kanban_dispatcher_watcher_active = False

        try:
            await self._run_kanban_dispatcher(service)
        finally:
            if pending is not None and (not pending.done() or pending.cancelled()):
                pending.add_done_callback(release)
            else:
                self._release_kanban_dispatcher_lock()
                self._kanban_dispatcher_watcher_active = False

    async def _run_kanban_dispatcher(self, service) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway competes for the machine-global dispatcher lock;
        a contending gateway retries on the dispatch cadence until shutdown.
        Takeover waits at most one retry interval plus the existing five-second
        startup grace (normally about 65 seconds), excluding in-flight work.
        A missing lock backend never authorizes unprotected dispatch.
        No separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
            # Upstream decomposed the facade without re-exports: dispatch helpers live here.
            from hermes_cli import kanban_db_dispatch as _kbd
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
            if not math.isfinite(interval):
                raise ValueError("dispatch interval must be finite")
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        waiting_logged = False
        unavailable_logged = False
        while self._running:
            # A standby may wait for days: honor disablement before each attempt,
            # and use fresh leader policy rather than its stale boot snapshot.
            env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
            if env_override in {"0", "false", "no", "off"}:
                return
            try:
                cfg = _load_config()
                kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
            except Exception:
                logger.exception("kanban dispatcher: cannot refresh standby configuration; disabled")
                return
            if not kanban_cfg.get("dispatch_in_gateway", True):
                return
            _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
            if _lock_state == "held":
                break
            if _lock_state == "unavailable":
                if not unavailable_logged:
                    logger.error(
                        "kanban dispatcher: lock unavailable at %s; "
                        "dispatch disabled until exclusion can be established", _lock_path,
                    )
                    unavailable_logged = True
            elif not waiting_logged:
                logger.info(
                    "kanban dispatcher: another gateway holds the dispatcher "
                    "lock (%s); standing by, retrying every %.1fs.", _lock_path, interval,
                )
                waiting_logged = True
            # Match the tick sleep's shutdown responsiveness, without dispatching
            # or reaping anything until leadership has actually been acquired.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
        else:
            return
        self._kanban_dispatcher_lock_handle = _lock_handle
        if waiting_logged or unavailable_logged:
            logger.warning("kanban dispatcher: assumed leadership after standby (%s)", _lock_path)
        else:
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info("kanban dispatcher: max_in_progress=%s", max_in_progress)
        # When the operator never set kanban.max_in_progress, fall back to a
        # memory-derived default (OOF-30/OOF-77): unbounded fan-out on small
        # hosted VMs has repeatedly swap-thrashed the whole machine. Explicit
        # config always wins; None stays None on hosts where total memory
        # can't be read (macOS/Windows dev machines).
        effective_max_in_progress = _kbd.resolve_max_in_progress(max_in_progress)
        if max_in_progress is None and effective_max_in_progress is not None:
            logger.info(
                "kanban dispatcher: kanban.max_in_progress unset; using "
                "memory-derived default max_in_progress=%d "
                "(set kanban.max_in_progress in config.yaml to override)",
                effective_max_in_progress,
            )
        max_in_progress = effective_max_in_progress

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # kanban.reconcile_orphans (config.yaml, default true): each tick,
        # requeue 'running' cards whose claim bookkeeping is broken (no
        # valid claim, dead/gone worker) — the zombie-card reconciliation
        # pass. Set false to keep orphans frozen for manual forensics.
        reconcile_orphans = bool(kanban_cfg.get("reconcile_orphans", True))

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if isinstance(raw_per_profile, dict):
            # Mapping shape {default: N, <profile>: M}: resolved per assignee
            # by kanban_db.resolve_per_profile_cap at spawn time. Validate
            # the values here so a typo is loud at boot, not silent forever.
            _clean: dict[str, int] = {}
            for _k, _v in raw_per_profile.items():
                try:
                    _iv = int(_v)
                except (TypeError, ValueError):
                    _iv = 0
                if _iv >= 1:
                    _clean[str(_k)] = _iv
                else:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile[%r]=%r "
                        "is not a positive int; ignoring that entry",
                        _k, _v,
                    )
            max_in_progress_per_profile = _clean or None
            if max_in_progress_per_profile:
                logger.info(
                    "kanban dispatcher: max_in_progress_per_profile=%s",
                    max_in_progress_per_profile,
                )
        elif raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # kanban.dispatch_load_gate — pause SPAWNS (never reclaims) while the
        # host's 1-minute load is over its core count; resume with hysteresis.
        # See LoadGate for the 2026-09-24 incident this encodes.
        try:
            _ncpu = os.cpu_count() or 1
        except Exception:
            _ncpu = 1
        load_gate = LoadGate(kanban_cfg.get("dispatch_load_gate"), _ncpu)
        if load_gate.enabled:
            logger.info(
                "kanban dispatcher: load gate armed pause_above=%.1f resume_below=%.1f "
                "ncpu=%d worker_load_cost=%.1f ramp_seconds=%.0f max_spawn_per_tick=%d "
                "load5_floor=%s",
                load_gate.pause_above, load_gate.resume_below, load_gate.ncpu,
                load_gate.worker_load_cost, load_gate.ramp_seconds,
                load_gate.max_spawn_per_tick, load_gate.load5_floor,
            )
        def _sample_spawn_pause() -> "tuple[Optional[int], Optional[str]]":
            """(allowance, reason) for this tick; (None, None) = no limit.

            Host-wide running count feeds the gate's load-per-worker slope
            (t_bf26e8f1); CPU busy is sampled inside ``admit_now``.
            """
            running = None
            if load_gate.enabled:
                from hermes_cli import kanban_load_gate as _klg_mod

                running = _klg_mod.count_running_workers()
            return load_gate.admit_now(running=running)

        _proc_paged = {"episode": False}

        def _finish_gate_tick(spawned: int) -> None:
            load_gate.finish_tick(spawned, logger=logger)
            if load_gate.state != "proc_paused":
                _proc_paged["episode"] = False
            elif not _proc_paged["episode"]:
                # A failed send stays unpaged, so the next tick retries.
                _proc_paged["episode"] = _send_proc_slot_alert(load_gate)

        # Round-robin cursor for the per-board allowance split and the
        # wall-clock start of each board's current zero-spawn streak while
        # the gate had allowance (published to `hermes kanban diagnostics`).
        from hermes_cli import kanban_load_gate as _klg
        _board_rr = {"tick": 0}
        _board_starved_since: dict[str, float] = {}

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Per-board rate limiter for the stranded-subtree warning. A triage
        # decision can legitimately sit for hours; warn every 5 min per board
        # rather than every tick.
        last_stranded_warn_at: dict[str, int] = {}
        last_unwoken_warn_at: dict[str, int] = {}
        last_workspace_refusal_warn: dict[str, tuple[str, int]] = {}
        workspace_refusal_notifier = _WorkspaceRefusalOutageNotifier()
        guard_stuck_notifier = _GuardStuckNotifier(_guard_stuck_state_path())
        needs_input_pager = _NeedsInputPager(_needs_input_state_path())
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            # Called once per board per dispatch tick — enumeration, not
            # addressing. Unscoped this is the single loudest source of pin
            # contradiction warnings on the dispatcher's own path.
            with _kb.enumerating_boards():
                path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            # The guard class lives in ``kanban_db_connect`` since upstream's
            # extraction; the facade no longer re-exports it, and a missing
            # attribute here silently disarmed the quarantine (every tick
            # logged a traceback instead of pausing the board).
            from hermes_cli import kanban_db_connect as _kbc
            corrupt_guard_error = getattr(_kbc, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str, budget_cache: "Optional[dict]" = None, spawn_paused: "Optional[str]" = None, spawn_limit: "Optional[int]" = None, requeue_note: "Optional[str]" = None) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                _requeue_host_transient_on(conn, slug, requeue_note)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                from hermes_cli import kanban_db_dispatch as _kbd
                return _kbd.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                    spawn_paused=spawn_paused,
                    spawn_limit=spawn_limit,
                    reconcile_orphans=reconcile_orphans,
                    budget_cache=budget_cache,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            # One budget cache per TICK, shared across boards: the per-profile
            # turn ledgers are the same files for every board, so without this
            # an N-board host re-reads every ledger N times per tick.
            budget_cache: dict = {}
            # Load gate: ONE allowance per tick, shared across every board —
            # the host's run queue is one resource no matter which board the
            # worker came from. It is SPLIT round-robin across the boards
            # that have spawnable work, rotating the first pick every tick
            # (t_f78d1938: consumed in fixed board order, default first, the
            # subs-ace board got 0 spawns for 93 min with 7 ready P1 cards).
            _allowance, _spawn_paused = _sample_spawn_pause()
            # Host-exhaustion transient blocks clear when the host does
            # (t_b660edb6); requeued inside each board's dispatch tick.
            _requeue_note = _host_recovery_note(load_gate)
            _tick_spawned = 0
            _demand: list[tuple[str, int]] = []
            if _allowance is not None and not _spawn_paused:
                _review_on = _kbd.review_dispatch_enabled()
                for b in _kb.enumerating_each(boards):
                    slug = b.get("slug") or _kb.DEFAULT_BOARD
                    # A board quarantined as corrupt (same fingerprint, inside
                    # its retry window) is not opened by the demand pre-scan
                    # either: the dispatch tick below skips it, so a probe
                    # here would only re-hit the corrupt file every tick.
                    _q = disabled_corrupt_boards.get(slug)
                    if (
                        _q is not None
                        and _q[0] == _board_db_fingerprint(slug)
                        and time.monotonic() - _q[1] < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                    ):
                        _demand.append((slug, 0))
                        continue
                    _dconn = None
                    try:
                        _dconn = _kb.connect(board=slug)
                        _n = _kb.count_spawnable_demand(
                            _dconn,
                            default_assignee=default_assignee,
                            include_review=_review_on,
                        )
                    except Exception:
                        _n = 0
                    finally:
                        if _dconn is not None:
                            try:
                                _dconn.close()
                            except Exception:
                                pass
                    _demand.append((slug, _n))
            _quotas = _klg.split_allowance(
                _allowance, _demand, start=_board_rr["tick"],
            )
            _board_rr["tick"] += 1
            _demand_by = dict(_demand)
            # Allowance no board claimed by demand (total demand < allowance)
            # stays available to any board — the demand count must never be
            # the reason a board spawns less than it did before the split.
            _spare = (
                None if _allowance is None
                else max(0, _allowance - sum(q or 0 for q in _quotas.values()))
            )
            _board_stats: dict = {}
            # Enumeration extent spans the whole per-board tick body, not just
            # the fingerprint's path resolve: `_tick_once_for_board` also calls
            # `connect(board=slug)`, which re-resolves internally. Scoping only
            # the resolve left the tick emitting a burst of contradiction
            # warnings that then silenced later single-board misreadings.
            for b in _kb.enumerating_each(boards):
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                _quota = _quotas.get(slug, 0) or 0
                _limit = None if _allowance is None else _quota + (_spare or 0)
                _paused = _spawn_paused
                if _paused is None and _limit is not None and _limit <= 0:
                    _paused = (
                        f"load gate: this tick's allowance of {_allowance} "
                        f"spawn(s) is used by other boards"
                    )
                res = _tick_once_for_board(
                    slug, budget_cache, _paused,
                    None if _paused else _limit,
                    requeue_note=_requeue_note,
                )
                out.append((slug, res))
                _n = len(getattr(res, "spawned", None) or []) if res is not None else 0
                _tick_spawned += _n
                if _spare is not None:
                    # Quota this board did not use (concurrency cap, demand
                    # over-count) goes back to the pool for the boards after
                    # it; spawns past its quota came out of the pool.
                    _spare = max(0, _spare + _quota - _n)
                _ready = _demand_by.get(slug, 0)
                if not _demand:
                    # Gate paused/disabled: no split happened, so no board
                    # was starved BY the split — leave the streaks alone.
                    pass
                elif _ready > 0:
                    logger.info(
                        "kanban dispatcher [%s]: ready=%d quota=%d spawned=%d "
                        "starved=%d allowance=%s",
                        slug, _ready, _quota, _n,
                        _ready if _n == 0 else 0, _allowance,
                    )
                    _prev = _board_starved_since.get(slug)
                    if _n > 0:
                        _board_starved_since.pop(slug, None)
                    elif _prev is None:
                        _board_starved_since[slug] = time.time()
                else:
                    _board_starved_since.pop(slug, None)
                _board_stats[slug] = {
                    "ready": _ready,
                    "quota": _quota,
                    "spawned": _n,
                    "starved_since": _board_starved_since.get(slug),
                }
            if _allowance is not None and not _spawn_paused:
                load_gate.boards = {
                    k: v for k, v in _board_stats.items() if v["ready"] > 0
                }
            _finish_gate_tick(_tick_spawned)
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            # Only probe the review column when autonomous review dispatch is
            # actually on. With ``review_dispatch`` off (the default — no
            # sdlc-review agent), a task parked in 'review' is "correctly idle"
            # waiting for a human, not a stuck dispatcher; probing it here would
            # fire a false "dispatcher stuck" warning that never clears. Shares
            # the exact gate the dispatcher uses so the two can't drift.
            _review_probe = _kbd.review_dispatch_enabled()
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in _kb.enumerating_each(boards):
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kbd.has_spawnable_ready(conn):
                        return True
                    if _review_probe and _kbd.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            # Enumeration: sweeps every board on disk looking for triage cards.
            # The body pins HERMES_KANBAN_BOARD and lets the decomposer connect
            # with no board kwarg, so the resolves happen deep inside the call —
            # only a body-spanning extent reaches them.
            for b in _kb.enumerating_each(boards):
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await service(_kbd.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Global emergency stop (`hermes pause`): skip auto-decompose
                # and dispatch entirely — no new workers while paused. Running
                # workers finish naturally; zombie reaping above still runs.
                if not _kanban_dispatch_allowed():
                    ready_pending = False
                    guard_stuck = []
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live each tick so a user
                    # flipping kanban.auto_decompose=false to STOP runaway fan-out
                    # takes effect on the next tick, not on gateway restart (#49638).
                    _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                    if _ad_enabled:
                        await service(_auto_decompose_tick, _ad_per_tick)
                    results = await service(_tick_once)
                    # Notification is off-loop. One page per (card, reason)
                    # per refusal episode, latched on the card; a failed
                    # delivery releases the claim so the next tick retries.
                    await service(
                        _observe_workspace_refusal_outages,
                        workspace_refusal_notifier,
                        results,
                    )
                    any_spawned = False
                    for slug, res in (results or []):
                        spawned = getattr(res, "spawned", None) if res is not None else None
                        refused = getattr(res, "workspace_refused", None) if res is not None else None
                        if spawned:
                            any_spawned = True
                        if refused:
                            summary = _format_workspace_refused_summary(refused)
                            now_s = int(time.time())
                            previous_summary, previous_at = (
                                last_workspace_refusal_warn.get(slug, ("", 0))
                            )
                            if summary != previous_summary or now_s - previous_at >= 300:
                                logger.error(
                                    "kanban dispatcher tick [%s]: %s",
                                    slug, summary,
                                )
                                last_workspace_refusal_warn[slug] = (summary, now_s)
                        _log_dispatch_tick(logger, slug, res)
                        # Stranded subtrees: children held in ``todo`` behind a
                        # parent only a human can clear. This CANNOT reach the
                        # stall detector below — that gate requires a non-empty
                        # ready queue, and a fully-stranded board has none, so a
                        # triaged parent freezing a whole subtree looked exactly
                        # like an idle board. Warn on its own path, rate-limited
                        # so a long-lived triage decision doesn't spam every tick.
                        stranded = (
                            getattr(res, "stranded_by_triage", None)
                            if res is not None else None
                        )
                        if stranded:
                            now_s = int(time.time())
                            if now_s - last_stranded_warn_at.get(slug, 0) >= 300:
                                parents = sorted({p for _c, p in stranded})
                                children = sorted({c for c, _p in stranded})
                                logger.warning(
                                    "kanban dispatcher [%s]: %d task(s) STRANDED "
                                    "behind %d triaged/blocked parent(s) — no "
                                    "worker will spawn until a human resolves "
                                    "them. parents=%s stranded=%s. Resolve with "
                                    "`hermes kanban triage-resolve <id> --to "
                                    "todo|done|archived --reason \"...\"`.",
                                    slug, len(children), len(parents),
                                    ", ".join(parents), ", ".join(children),
                                )
                                last_stranded_warn_at[slug] = now_s
                        # Scheduled cards with NO timed wake, parked > 24h: the
                        # dispatcher will never move them (t_6915068e). Same
                        # STRANDED channel, hourly — the condition is day-scale.
                        unwoken = (
                            getattr(res, "unwoken_scheduled", None)
                            if res is not None else None
                        )
                        if unwoken:
                            now_s = int(time.time())
                            if now_s - last_unwoken_warn_at.get(slug, 0) >= 3600:
                                from hermes_cli.kanban_db import format_unwoken_scheduled
                                logger.warning(
                                    "kanban dispatcher [%s]: %s",
                                    slug, format_unwoken_scheduled(unwoken).replace("\n", " "),
                                )
                                last_unwoken_warn_at[slug] = now_s
                    # Health telemetry (aggregate across boards).
                    #
                    # A tick with ready work but zero spawns is only a REAL stall
                    # when the dispatcher tried and FAILED (broken venv/PATH/creds
                    # → spawn_failed / circuit-breaker auto_block). It is NOT a
                    # stall when the dispatcher DECLINED for a benign, self-clearing
                    # reason: every eligible profile is at its concurrency cap, a
                    # worker bailed on a provider rate-limit / quota wall (released
                    # to ``ready`` without counting a failure), or another process
                    # holds the board's dispatch lock. Those "declined" states look
                    # identical to a hard failure through the ``not any_spawned``
                    # lens, which historically mis-fired the "check profile health
                    # (venv, PATH, credentials)" warning for hours during a large
                    # fan-out or a provider-side 429 window (the assignee was simply
                    # throttled, not broken). ``_stall_streak_is_bad`` consults the
                    # DispatchResult buckets so telemetry can tell "busy/throttled"
                    # from "genuinely stuck" instead of guessing.
                    guard_stuck, observed_boards = await service(_guard_stuck_cards, results)
                    guard_pages = await service(
                        guard_stuck_notifier.observe, guard_stuck, _send_guard_stuck_alert,
                        observed_boards,
                    )
                    if guard_pages:
                        logger.error("kanban dispatcher: %d guarded card(s) STUCK; "
                                     "#alerts paged with diagnostics", guard_pages)
                    # Needs-input pager (t_c8ca40b4): a card waiting on a human
                    # ruling pages its origin channel; config re-read per tick.
                    _nip_on, _nip_dep = _resolve_needs_input_pager_settings(_load_config)
                    if _nip_on:
                        nip_cards, nip_boards = await service(
                            _needs_input_cards, results, _nip_dep)
                        nip_pages = await service(
                            needs_input_pager.observe, nip_cards,
                            _send_needs_input_page, nip_boards,
                        )
                        if nip_pages:
                            logger.warning("kanban dispatcher: paged %d needs-input "
                                           "card(s) to their origin channel", nip_pages)
                    ready_pending = await service(_ready_nonempty)
                    if _stall_streak_is_bad(ready_pending, any_spawned, results,
                                            guard_stuck=bool(guard_stuck)):
                        bad_ticks += 1
                    else:
                        bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        if guard_stuck:
                            logger.warning(
                                "kanban dispatcher STUCK: %d card(s) continuously "
                                "guarded. See per-card diagnostics in #alerts.",
                                len(guard_stuck),
                            )
                        else:
                            logger.warning(
                                "kanban dispatcher stuck: ready queue non-empty for "
                                "%d consecutive ticks but 0 workers spawned, with no "
                                "benign decline (cap/rate-limit/lock) to explain it. "
                                "Check profile health (venv, PATH, credentials) and "
                                "`hermes kanban list --status ready`.",
                                bad_ticks,
                            )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
