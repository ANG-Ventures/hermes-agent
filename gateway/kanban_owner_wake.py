"""Kanban events WAKE the owning operator session (t_1ae0c35b, Ace 2026-10-03 11:05).

Incident 2026-10-03: Phase 4 (t_e6b3713d) sat BLOCKED 04:14 -> 10:18 with its
fix merged and deployed. The lifecycle only POSTED "blocked" to the home chat;
the Apollo session that owned the card never got a turn, and acted only when
Ace typed "Proceed". The pager and the digest notify a HUMAN. This module gives
the OWNING SESSION a turn.

A card's owner is its home session (``tasks.session_id``). When that session is
live in THIS gateway's session store, its origin is a messaging chat, and this
gateway runs an operator profile (``kanban.wake_owner_profiles``, default
default/aegis), these events enqueue one handoff turn into it:

(a) ``blocked`` with ``kind=needs_input``;
(b) ``blocked`` whose reason names a time or precondition ("respawn me at…",
    "waiting on PR#…", "deploy: …", "after the next restart window");
(c) ``stalled`` / ``crashed`` / ``gave_up`` / ``timed_out``, or a ``reclaimed``
    with a stale heartbeat;
(d) ``completed`` of a card that was the LAST ``blocks`` parent of one or more
    open children;
(e) ``review_requested`` whose fleet PR is red (a failed check-run) or dirty.

Not on: heartbeats, a review handback whose PR is green or pending (the merge
pass owns it), a ``completed`` with no gated children (the digest covers it),
or an event the owning session made itself.

Coalescing: one wake per card per :data:`COALESCE_SECONDS`; events that arrive
inside the window are held and listed in the next turn. All cards due for one
session in a tick go out as ONE turn. The turn is an internal
``MessageEvent(allow_gateway_control=False)`` pinned to the session's key and
id (the plugin-injection shape), so a busy session queues it like any inbound
message and it can never run a slash command.

Opt-outs: ``kanban.wake_owner_session: false`` (global) or a card body line
``wake: off``. A card homed to a worker, cron or CLI session (not in a
gateway's store, or no chat origin) gets no wake: today's Discord line only.

State (board cursors, pending events, last-wake times) lives in
``<HERMES_HOME>/gateway/kanban_owner_wake.json`` so a restart neither replays
history nor drops held events. A first run with no state starts
:data:`FIRST_RUN_LOOKBACK_SECONDS` back.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

COALESCE_SECONDS = 600.0
FIRST_RUN_LOOKBACK_SECONDS = 600
PENDING_MAX_AGE_SECONDS = 3600.0
ROW_RETRY_LIMIT = 10  # ~10 ticks: a row that keeps failing to classify is skipped, loudly
SCAN_LIMIT = 500
REASON_MAX = 1500
DEFAULT_OWNER_PROFILES = ("default", "aegis")
STATE_FILENAME = "kanban_owner_wake.json"

SCAN_KINDS = (
    "blocked", "stalled", "crashed", "gave_up", "timed_out", "reclaimed",
    "completed", "review_requested",
)
STUCK_KINDS = frozenset({"stalled", "crashed", "gave_up", "timed_out"})
# Kinds the per-subscription notify+wake path already turns into a wake. A
# card with such a sub on the owner's chat gets its plain wake from there;
# this module adds only (d) children-ready and (e) red/dirty PR.
SUB_WAKE_KINDS = frozenset({"blocked", "crashed", "gave_up", "timed_out"})

TRIGGER_NEEDS_INPUT = "needs_input"
TRIGGER_PRECONDITION = "blocked_precondition"
TRIGGER_STUCK = "stuck"
TRIGGER_LAST_BLOCKER = "last_blocker_done"
TRIGGER_RED_HANDBACK = "red_handback"

_WAKE_OFF_RE = re.compile(r"^\s*wake\s*:\s*off\s*$", re.I | re.M)
# A block reason that names WHEN or ON WHAT the card can move again.
_PRECONDITION_RE = re.compile(
    r"respawn me|re-?spawn (at|after|when)|waiting (on|for)|\bwait(s)? (on|for)\b"
    r"|\bdeploy\s*:|unblock (me )?(after|when|once)|\bat or after\b|\buntil\b"
    r"|\bonce\b|after the next|\bnext (gateway )?restart\b|restart window"
    r"|\bPR\s*#?\d+|\b[\w.-]+/[\w.-]+#\d+|\b\d{1,2}:\d{2}\s*(PT|PDT|PST|UTC|Z)\b",
    re.I,
)
_NON_CHAT_PLATFORMS = frozenset({"local", "cli", "tui", "api_server", "webhook", "cron"})
_RED_CONCLUSIONS = frozenset({
    "failure", "timed_out", "cancelled", "action_required", "startup_failure",
})

PrHealthFn = Callable[[str, int], Optional[dict]]


class PrHealthUnknown(RuntimeError):
    """A fleet PR's health could not be read (timeout, rate limit). The event is
    retried on later ticks, not consumed as a green handback (Prism P1)."""


# --- pure classification ----------------------------------------------------


def wake_off(body: Optional[str]) -> bool:
    return bool(body) and bool(_WAKE_OFF_RE.search(body or ""))


def names_precondition(reason: Optional[str]) -> bool:
    return bool(reason) and bool(_PRECONDITION_RE.search(reason or ""))


def classify_block(payload: Optional[dict]) -> Optional[str]:
    p = payload or {}
    if str(p.get("kind") or "") == "needs_input":
        return TRIGGER_NEEDS_INPUT
    if names_precondition(str(p.get("reason") or "")):
        return TRIGGER_PRECONDITION
    return None


def is_stuck(kind: str, payload: Optional[dict]) -> bool:
    if kind in STUCK_KINDS:
        return True
    return kind == "reclaimed" and bool((payload or {}).get("heartbeat_stale"))


def pr_is_red_or_dirty(health: Optional[dict]) -> Optional[str]:
    """``"red: a, b"`` / ``"dirty"`` / ``"red: …; dirty"`` or None (green, pending, unknown)."""
    if not health or str(health.get("state") or "").lower() != "open":
        return None
    parts = []
    failing = [str(n) for n in (health.get("failing") or []) if n]
    if failing:
        parts.append("red: " + ", ".join(failing[:6]))
    if str(health.get("mergeable_state") or "").lower() == "dirty":
        parts.append("dirty (merge conflict)")
    return "; ".join(parts) or None


def _clip(text: Any, limit: int = REASON_MAX) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def describe(item: dict) -> str:
    """One sentence for one triggering event (reason verbatim)."""
    trig = item["trigger"]
    reason = item.get("reason") or ""
    if trig == TRIGGER_NEEDS_INPUT:
        out = "blocked, needs_input"
    elif trig == TRIGGER_PRECONDITION:
        out = "blocked on a precondition"
    elif trig == TRIGGER_STUCK:
        out = f"stuck ({item.get('kind')})"
    elif trig == TRIGGER_LAST_BLOCKER:
        kids = ", ".join(f"{c['id']} [{c['status']}]" for c in item.get("children") or [])
        out = (f"done, and it was the last blocker of {kids}; those children are now "
               "unblocked: arm or dispatch them")
    elif trig == TRIGGER_RED_HANDBACK:
        out = f"handed back for review but the PR cannot land ({item.get('pr_state')})"
    else:
        out = str(item.get("kind"))
    if reason:
        out += f'. Reason, verbatim: "{reason}"'
    return out


def render_prompt(cards: list[dict]) -> str:
    """One paragraph per card. Not a question to Ace."""
    paras = []
    for card in cards:
        events = "; then ".join(describe(i) for i in card["items"])
        paras.append(
            f"[kanban wake] Card {card['task_id']} \"{_clip(card.get('title'), 160)}\" "
            f"(board {card.get('board') or 'default'}), a card this session owns: {events}. "
            "Resolve it and get the chain back on track; reply in the home chat with what you did."
        )
    return "\n\n".join(paras)


# --- PR health (REST only; GraphQL is the shared per-user bucket) ------------


def _gh_json(path: str) -> Optional[Any]:
    try:
        proc = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except ValueError:
        return None


def query_pr_health(repo: str, number: int) -> Optional[dict]:
    """``{state, mergeable_state, failing: [check names]}`` or None (unknown)."""
    pr = _gh_json(f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict) or not pr.get("state"):
        return None
    out = {"state": "merged" if pr.get("merged_at") else str(pr["state"]),
           "mergeable_state": str(pr.get("mergeable_state") or ""), "failing": []}
    sha = (pr.get("head") or {}).get("sha")
    if out["state"] == "open" and sha:
        runs = _gh_json(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        for run in (runs or {}).get("check_runs") or []:
            if isinstance(run, dict) and str(run.get("conclusion") or "") in _RED_CONCLUSIONS:
                out["failing"].append(str(run.get("name") or "?"))
    return out


def default_pr_health() -> Optional[PrHealthFn]:
    # Same seam as kanban_open_pr._default_query: no live GitHub from pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return query_pr_health


def handback_pr_state(kb: Any, conn: Any, task: dict, payload: Optional[dict],
                      run_id: Optional[int], pr_health: Optional[PrHealthFn],
                      memo: dict) -> Optional[str]:
    if pr_health is None:
        return None
    from hermes_cli import kanban_open_pr as _op

    meta = None
    if run_id is not None:
        try:
            run = kb.get_run(conn, int(run_id))
            meta = getattr(run, "metadata", None) if run else None
        except Exception:
            meta = None
    refs = _op.extract_pr_refs((payload or {}).get("summary"), task.get("result"),
                               metadata=meta if isinstance(meta, dict) else None)
    states = []
    for ref in refs:
        if not _op.is_fleet_ref(ref):
            continue
        key = (ref.repo.lower(), int(ref.number))
        if key not in memo:
            try:
                memo[key] = pr_health(ref.repo, int(ref.number))
            except Exception:
                memo[key] = None
        if memo[key] is None:
            raise PrHealthUnknown(f"{ref.repo}#{ref.number}")
        bad = pr_is_red_or_dirty(memo[key])
        if bad:
            states.append(f"{ref.repo}#{ref.number} {bad}")
    return "; ".join(states) or None


def gated_ready_children(kb: Any, conn: Any, task_id: str) -> list[dict]:
    """Open children whose every ``blocks`` parent is now done/archived."""
    out = []
    for child_id, kind in kb.child_links(conn, task_id):
        if kind != kb.LINK_KIND_BLOCKS:
            continue
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (child_id,)).fetchone()
        if row is None or row["status"] in ("done", "archived", "running", "review"):
            continue
        if not kb._parents_satisfied(conn, child_id):  # noqa: SLF001 -- the dispatcher's own gate
            continue
        out.append({"id": child_id, "status": row["status"]})
    return out


# --- state ------------------------------------------------------------------


class WakeState:
    """Board cursors + held events + last-wake times, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cursors: dict[str, int] = {}
        self.pending: dict[str, dict] = {}   # "board|task" -> card dict
        self.last_wake: dict[str, float] = {}
        self.row_failures: dict[str, int] = {}  # "board|event" -> failed classifications
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.cursors = {str(k): int(v) for k, v in (raw.get("cursors") or {}).items()}
            self.pending = dict(raw.get("pending") or {})
            self.last_wake = {str(k): float(v) for k, v in (raw.get("last_wake") or {}).items()}

    def write_owner_wake_state(self, now: float) -> None:
        """Blocking (fsync + rename): call via ``asyncio.to_thread`` only."""
        self.last_wake = {k: v for k, v in self.last_wake.items() if now - v < COALESCE_SECONDS * 6}
        data = {"cursors": self.cursors, "pending": self.pending, "last_wake": self.last_wake}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".owner-wake-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            logger.warning("kanban owner-wake: cannot persist state %s: %s", self.path, exc)


def state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gateway" / STATE_FILENAME


def resolve_settings() -> tuple[bool, tuple[str, ...]]:
    """(``kanban.wake_owner_session``, ``kanban.wake_owner_profiles``). Errors -> defaults."""
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        if not isinstance(kcfg, dict):
            kcfg = {}
    except Exception:
        kcfg = {}
    enabled = kcfg.get("wake_owner_session", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "off", "no")
    profiles = kcfg.get("wake_owner_profiles") or DEFAULT_OWNER_PROFILES
    if isinstance(profiles, str):
        profiles = [p.strip() for p in profiles.split(",")]
    return bool(enabled), tuple(str(p).strip() for p in profiles if str(p).strip())


# --- scan (worker thread) ---------------------------------------------------


def _initial_cursor(conn: sqlite3.Connection, now: float) -> int:
    row = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE created_at >= ?",
        (int(now - FIRST_RUN_LOOKBACK_SECONDS),),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0]) - 1
    row = conn.execute("SELECT MAX(id) FROM task_events").fetchone()
    return int(row[0] or 0) if row else 0


def scan(state: WakeState, now: float, pr_health: Optional[PrHealthFn],
         boards: Optional[Iterable[dict]] = None) -> int:
    """Read new events on every board into ``state.pending``. Returns events queued."""
    from hermes_cli import kanban_db as kb

    if boards is None:
        try:
            boards = kb.list_boards(include_archived=False)
        except Exception:
            boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
    queued = 0
    seen: set[str] = set()
    memo: dict = {}
    for meta in boards:
        slug = str(meta.get("slug") or kb.DEFAULT_BOARD)
        try:
            path = Path(meta["db_path"]).expanduser() if meta.get("db_path") else kb.kanban_db_path(slug)
            resolved = str(path.resolve())
        except Exception:
            continue
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            conn = kb.connect_readonly(db_path=path)
        except Exception as exc:
            logger.debug("kanban owner-wake: cannot open board %s: %s", slug, exc)
            continue
        conn.row_factory = sqlite3.Row
        try:
            if slug not in state.cursors:
                state.cursors[slug] = _initial_cursor(conn, now)
            cursor = state.cursors[slug]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
            actor = "e.actor_session_id" if "actor_session_id" in cols else "NULL"
            marks = ",".join("?" for _ in SCAN_KINDS)
            rows = conn.execute(
                f"SELECT e.id, e.task_id, e.kind, e.payload, e.run_id, e.created_at, "
                f"{actor} AS actor_sid, t.session_id AS home_sid, t.title, t.body, "
                f"t.result FROM task_events e JOIN tasks t ON t.id = e.task_id "
                f"WHERE e.id > ? AND e.kind IN ({marks}) ORDER BY e.id LIMIT ?",
                (cursor, *SCAN_KINDS, SCAN_LIMIT),
            ).fetchall()
            for r in rows:
                # The cursor moves past a row only once it is classified and
                # queued; a failure leaves it for the next tick (Prism P1).
                fkey = f"{slug}|{r['id']}"
                try:
                    item = _classify_row(kb, conn, r, pr_health, memo)
                except Exception as exc:
                    n = state.row_failures.get(fkey, 0) + 1
                    state.row_failures[fkey] = n
                    if n < ROW_RETRY_LIMIT:
                        logger.warning("kanban owner-wake: event %s on %s failed to classify "
                                       "(%s); retrying next tick", r["id"], slug, exc)
                        break
                    logger.warning("kanban owner-wake: event %s on %s failed %d times (%s); "
                                   "skipping it", r["id"], slug, n, exc)
                    item = None
                state.row_failures.pop(fkey, None)
                if item is not None:
                    item["queued_at"] = now
                    key = f"{slug}|{r['task_id']}"
                    card = state.pending.setdefault(key, {
                        "board": slug, "task_id": r["task_id"], "home_sid": r["home_sid"] or "",
                        "title": r["title"] or "", "items": [],
                    })
                    card["home_sid"] = r["home_sid"] or card.get("home_sid") or ""
                    card["items"].append(item)
                    queued += 1
                state.cursors[slug] = max(state.cursors[slug], int(r["id"]))
            # A busy board drains over several ticks, LIMIT rows at a time.
        except Exception as exc:
            logger.warning("kanban owner-wake: scan of board %s failed: %s", slug, exc)
        finally:
            conn.close()
    return queued


def _classify_row(kb: Any, conn: Any, r: Any, pr_health: Optional[PrHealthFn],
                  memo: dict) -> Optional[dict]:
    if wake_off(r["body"]):
        return None
    try:
        payload = json.loads(r["payload"]) if r["payload"] else None
    except ValueError:
        payload = None
    kind = r["kind"]
    item = {"event_id": int(r["id"]), "kind": kind, "actor_sid": r["actor_sid"] or ""}
    if kind == "blocked":
        trig = classify_block(payload)
        if trig is None:
            return None
        item.update(trigger=trig, reason=_clip((payload or {}).get("reason")))
    elif is_stuck(kind, payload):
        err = (payload or {}).get("error") or (payload or {}).get("reason") or ""
        item.update(trigger=TRIGGER_STUCK, reason=_clip(err, 400))
    elif kind == "completed":
        kids = gated_ready_children(kb, conn, r["task_id"])
        if not kids:
            return None
        item.update(trigger=TRIGGER_LAST_BLOCKER, children=kids)
    elif kind == "review_requested":
        bad = handback_pr_state(kb, conn, {"result": r["result"]}, payload, r["run_id"],
                                pr_health, memo)
        if not bad:
            return None
        item.update(trigger=TRIGGER_RED_HANDBACK, pr_state=bad)
    else:
        return None
    return item


# --- owner resolution + delivery (event loop) -------------------------------


def read_card(kb: Any, board: str, task_id: str) -> Optional[dict]:
    """Current ``{session_id, body, title}`` of a card, or None if it is gone.

    Read at DELIVERY time: a held event must follow a re-homed card and honour a
    ``wake: off`` added after it was queued (Prism P1s)."""
    conn = kb.connect_readonly(board=board)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT session_id, body, title FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def resolve_owner(store: Any, home_sid: str, read_row: Callable[[str], Optional[dict]]):
    """The live session-store entry that owns a card, or None.

    ``tasks.session_id`` is either a raw session id or (gateway-created cards)
    a session KEY. A raw id whose session was rotated (compression, /new)
    is followed through the state.db row's ``session_key``. Raises
    ``kanban_home_route.SessionLookupError`` if state.db cannot be read."""
    sid = (home_sid or "").strip()
    if not sid or sid.startswith("operator:") or store is None:
        return None
    if ":" in sid:
        return store.lookup_by_session_key(sid)
    entry = store.lookup_by_session_id(sid)
    if entry is not None:
        return entry
    row = read_row(sid)
    key = str((row or {}).get("session_key") or "").strip()
    return store.lookup_by_session_key(key) if key else None


def is_chat_origin(entry: Any) -> bool:
    origin = getattr(entry, "origin", None)
    if origin is None or not getattr(origin, "chat_id", None):
        return False
    plat = str(getattr(getattr(origin, "platform", None), "value", getattr(origin, "platform", "")))
    return plat.lower() not in _NON_CHAT_PLATFORMS


def has_wake_sub(kb: Any, board: str, task_id: str, origin: Any, profile: str) -> bool:
    """A notify+wake / wake sub already wakes the OWNER's session for this card.

    Only a sub that positively names the owner (same platform, chat, thread,
    participant and notifier profile) counts. A sub naming another participant
    wakes that participant's session, and an identity-less sub resolves at wake
    time; neither proves the owner was woken, so the owner wake stays (a
    duplicate turn is the safe failure, a lost one is not)."""
    try:
        conn = kb.connect_readonly(board=board)
    except Exception:
        return False
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    except Exception:
        return False
    finally:
        conn.close()
    def _s(v: Any) -> str:
        return str(v or "").strip()

    plat = _s(getattr(getattr(origin, "platform", None), "value", "")).lower()
    chat = _s(getattr(origin, "chat_id", ""))
    thread = _s(getattr(origin, "thread_id", ""))
    if thread == chat:
        thread = ""
    who = {_s(getattr(origin, "user_id", "")), _s(getattr(origin, "user_id_alt", ""))} - {""}
    for r in rows:
        r = dict(r)
        if _s(r.get("delivery_mode")) not in ("notify+wake", "wake"):
            continue
        sub_thread = _s(r.get("thread_id"))
        if sub_thread == chat:
            sub_thread = ""
        sub_who = {_s(r.get("user_id")), _s(r.get("user_id_alt"))} - {""}
        if (_s(r.get("platform")).lower() == plat and _s(r.get("chat_id")) == chat
                and sub_thread == thread and sub_who and sub_who & who
                and (_s(r.get("notifier_profile")) or "default") == (profile or "default")):
            return True
    return False


async def deliver_turn(runner: Any, entry: Any, text: str) -> None:
    """Enqueue one handoff turn into ``entry``'s session. Raises on no route."""
    from gateway.platforms.base import MessageEvent, MessageType

    source = dataclasses.replace(entry.origin)
    adapter = runner._adapter_for_source(source)  # noqa: SLF001
    if adapter is None:
        raise RuntimeError(f"no live adapter for {getattr(source.platform, 'value', source.platform)}")
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        allow_gateway_control=False,
        metadata={
            "kanban_owner_wake": True,
            "gateway_session_key": entry.session_key,
            # Not strict: a turn queued behind a busy session survives a
            # compression rotation. run.py follows the pinned id through its
            # verified compression lineage and still drops it after a /new
            # (_resolve_async_delegation_session; Prism P1).
            "gateway_session_id": entry.session_id,
        },
    )
    await adapter.handle_message(event)


async def tick(runner: Any, *, now: Optional[float] = None,
               pr_health: Optional[PrHealthFn] = None, use_default_pr_health: bool = True,
               state: Optional[WakeState] = None) -> int:
    """One owner-wake pass. Returns the number of turns enqueued."""
    import asyncio

    from gateway import kanban_home_route as _hr
    from hermes_cli import kanban_db as kb

    enabled, profiles = resolve_settings()
    if not enabled:
        return 0
    try:
        active = runner._active_profile_name()  # noqa: SLF001
    except Exception:
        active = "default"
    hosted = {active} | {str(p) for p in (getattr(runner, "_profile_adapters", None) or {})}
    if not hosted & set(profiles):
        return 0  # a worker-only gateway hosts no operator session: skip the scan
    if getattr(runner, "_draining", False):
        return 0
    now = time.time() if now is None else now
    if state is None:
        state = getattr(runner, "_kanban_owner_wake_state", None)
        if state is None:
            state = WakeState(state_path())
            runner._kanban_owner_wake_state = state
    if pr_health is None and use_default_pr_health:
        pr_health = default_pr_health()
    await asyncio.to_thread(scan, state, now, pr_health)

    store = getattr(runner, "session_store", None)
    by_session: dict[str, tuple[Any, list[tuple[str, dict]]]] = {}
    for key, card in list(state.pending.items()):
        # Expire ITEMS by their own age, never a fresh event with an old card.
        fresh = [i for i in card.get("items") or []
                 if now - float(i.get("queued_at", now)) <= PENDING_MAX_AGE_SECONDS]
        if len(fresh) != len(card.get("items") or []):
            logger.warning("kanban owner-wake: dropping %d event(s) of %s held > %ds",
                           len(card["items"]) - len(fresh), key, PENDING_MAX_AGE_SECONDS)
            card["items"] = fresh
        if not fresh:
            state.pending.pop(key, None)
            continue
        if now - state.last_wake.get(key, 0.0) < COALESCE_SECONDS:
            continue  # held: the next turn after the window lists it
        try:
            current = await asyncio.to_thread(read_card, kb, card["board"], card["task_id"])
        except Exception as exc:
            logger.warning("kanban owner-wake: cannot re-read %s (%s); retrying", key, exc)
            continue
        if current is None or wake_off(current.get("body")):
            state.pending.pop(key, None)  # card gone, or opted out while held
            continue
        card["home_sid"] = str(current.get("session_id") or "")
        card["title"] = current.get("title") or card.get("title") or ""
        try:
            entry = await asyncio.to_thread(resolve_owner, store, card["home_sid"],
                                            _hr.read_session_row)
        except _hr.SessionLookupError as exc:
            logger.warning("kanban owner-wake: home of %s unknown (%s); retrying", key, exc)
            continue
        # The operator gate applies to the OWNER's profile: one multiplexing
        # gateway serves several profiles from one store (Prism P1).
        owner_profile = str(getattr(getattr(entry, "origin", None), "profile", None)
                            or active or "default")
        if entry is None or not is_chat_origin(entry) or owner_profile not in profiles:
            logger.debug("kanban owner-wake: %s has no live operator home here; line only", key)
            state.pending.pop(key, None)
            continue
        own = {str(card.get("home_sid") or ""), str(getattr(entry, "session_id", "") or "")}
        items = [i for i in card["items"] if not (i.get("actor_sid") and i["actor_sid"] in own)]
        if items and await asyncio.to_thread(has_wake_sub, kb, card["board"], card["task_id"],
                                             entry.origin, owner_profile):
            items = [i for i in items if i["kind"] not in SUB_WAKE_KINDS]
        if not items:
            state.pending.pop(key, None)
            continue
        card = dict(card, items=items)
        by_session.setdefault(entry.session_key, (entry, []))[1].append((key, card))

    turns = 0
    for session_key, (entry, cards) in by_session.items():
        text = render_prompt([c for _k, c in cards])
        try:
            await deliver_turn(runner, entry, text)
        except Exception as exc:
            logger.warning("kanban owner-wake: wake of %s failed (%s); held for retry",
                           session_key, exc)
            continue
        turns += 1
        for key, card in cards:
            state.pending.pop(key, None)
            state.last_wake[key] = now
        logger.info(
            "kanban owner-wake: woke %s for %s",
            session_key,
            ", ".join(f"{c['task_id']}({'+'.join(i['trigger'] for i in c['items'])})"
                      for _k, c in cards),
        )
    await asyncio.to_thread(state.write_owner_wake_state, now)
    return turns
