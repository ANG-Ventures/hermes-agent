"""Route kanban lifecycle lines to the card's HOME channel (t_808bc8e6).

Ace ruling 2026-10-02 17:30: a done / ready-for-review / blocked / changes-requested
line about a card goes to the channel the card was born in (its home session's
origin chat), not to ``kanban.lifecycle_channel`` (#logs). #logs is the fallback,
used only when the card has no resolvable home, with a one-word tag naming why:

* ``[no-home]``                     no session, an ``operator:`` pseudo-session, an
                                    unknown session, or a session with no chat
* ``[home-unreachable:<reason>]``   the gateway has no adapter for the home's
                                    platform, or the send to the home failed

``kanban.lifecycle_route: channel`` restores the old behaviour (every routed
line to ``lifecycle_channel``). With ``lifecycle_channel`` unset nothing here
runs: lines stay in the subscriber's chat, as before.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional

ROUTE_HOME = "home"
ROUTE_CHANNEL = "channel"
# Kinds that follow the card home. changes_requested is new here: before this
# module it always stayed in the subscriber's chat, and it still does when the
# card has no home (it needs a human, never a log channel).
HOME_ROUTE_KINDS = frozenset({"completed", "review_requested", "blocked", "changes_requested"})
HOME_CACHE_MAX = 4096
HOME_CACHE_TTL = 600.0  # a session row written after the card's first tick is picked up
DEFAULT_HOME_DIGEST_SECONDS = 120
# state.db could not be read (lock, I/O, import): the home is UNKNOWN, not absent.
# Never cached; the notifier leaves the claim unacked so the next tick retries.
LOOKUP_FAILED = "lookup-failed"


class SessionLookupError(RuntimeError):
    """state.db could not be read. Distinct from a missing row (``None``)."""


class HomeTarget(NamedTuple):
    platform: str
    chat_id: str
    thread_id: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.chat_id)


class HomeResolution(NamedTuple):
    """``target`` is None when the card has no home; ``reason`` says why."""
    target: Optional[HomeTarget]
    reason: str = ""


def home_from_row(session_id: Optional[str], row: Optional[dict]) -> HomeResolution:
    """state.db ``sessions`` row -> the home chat. Pure; no I/O."""
    sid = (session_id or "").strip()
    if not sid:
        return HomeResolution(None, "no-session")
    if sid.startswith("operator:"):
        return HomeResolution(None, "operator-home")
    if not row:
        return HomeResolution(None, "unknown-session")
    origin: Any = None
    raw = row.get("origin_json")
    if raw:
        try:
            import json as _json

            origin = _json.loads(raw)
        except (TypeError, ValueError):
            origin = None
    if not isinstance(origin, dict):
        return HomeResolution(None, "no-channel")
    platform = str(origin.get("platform") or "").strip().lower()
    chat_id = str(origin.get("chat_id") or "").strip()
    if not platform or not chat_id or platform in ("local", "cli", "tui", "api_server"):
        return HomeResolution(None, "no-channel")
    thread_id = str(origin.get("thread_id") or "").strip()
    if thread_id == chat_id:
        thread_id = ""  # a Discord thread IS the chat; no separate thread metadata
    return HomeResolution(HomeTarget(platform, chat_id, thread_id))


def read_session_row(session_id: str) -> Optional[dict]:
    """Blocking, READ-ONLY state.db lookup (FleetReview #987); worker thread only.

    ``None`` = the read worked and no such session exists. A failed read raises
    :class:`SessionLookupError`, so a locked state.db never looks like no home."""
    try:
        from hermes_state import SessionDB

        db = SessionDB(read_only=True)
        try:
            return db.get_session(session_id)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        raise SessionLookupError(f"state.db lookup for {session_id} failed: {exc}") from exc


class HomeCache:
    """Per-card cache keyed ``(board, task_id, session_id)``: a re-homed card
    (new session_id) misses and re-resolves on the next tick."""

    def __init__(self) -> None:
        self._d: dict[tuple, tuple[float, Optional[dict], HomeResolution]] = {}

    def get(self, board: str, task_id: str, session_id: Optional[str],
            now: Optional[float] = None) -> tuple[Optional[dict], HomeResolution]:
        import time as _time

        now = _time.time() if now is None else now
        key = (board or "", task_id, (session_id or "").strip())
        hit = self._d.get(key)
        if hit is not None and now - hit[0] < HOME_CACHE_TTL:
            return hit[1], hit[2]
        sid = key[2]
        try:
            row = read_session_row(sid) if sid and not sid.startswith("operator:") else None
        except SessionLookupError:
            return None, HomeResolution(None, LOOKUP_FAILED)  # not cached: retried next tick
        res = home_from_row(sid, row)
        self._d.pop(key, None)
        self._d[key] = (now, row, res)
        while len(self._d) > HOME_CACHE_MAX:
            self._d.pop(next(iter(self._d)))
        return row, res


class Route(NamedTuple):
    """Where one lifecycle line goes. ``target`` None = the subscriber's chat."""
    target: Optional[tuple[str, str]]
    thread_id: str = ""
    fallback: Optional[tuple[str, str]] = None  # #logs, used if the home send fails
    tag: str = ""                               # set when target IS the fallback
    is_home: bool = False


def route_lifecycle_line(
    channel: Optional[tuple[str, str]], mode: str, kind: str, payload: Optional[dict],
    task: Any, home: Optional[HomeResolution],
) -> Route:
    """Pure routing decision for one event. ``channel`` = parsed
    ``kanban.lifecycle_channel`` (the fallback)."""
    from gateway.kanban_watchers import LIFECYCLE_CHANNEL_KINDS, lifecycle_channel_target

    if channel is None:
        return Route(None)
    if mode != ROUTE_HOME:
        return Route(lifecycle_channel_target(channel, kind, payload, task))
    if kind not in HOME_ROUTE_KINDS:
        return Route(None)
    if kind in LIFECYCLE_CHANNEL_KINDS and lifecycle_channel_target(channel, kind, payload, task) is None:
        return Route(None)  # needs_input block on a p>=100 card stays with the subscriber
    target = home.target if home is not None else None
    if target is not None:
        return Route(target.key, target.thread_id, channel, "", True)
    if kind not in LIFECYCLE_CHANNEL_KINDS:
        return Route(None)  # changes_requested with no home: subscriber chat, as before
    return Route(channel, "", None, "no-home")


def unreachable_tag(reason: Any) -> str:
    word = re.sub(r"[^a-z0-9_-]+", "-", str(reason or "send-failed").lower()).strip("-")[:32]
    return f"home-unreachable:{word or 'send-failed'}"


def tag_line(msg: str, tag: str) -> str:
    """Append ``[tag]`` to the message's first line (the digest compacts on it)."""
    if not tag:
        return msg
    head, sep, rest = (msg or "").partition("\n")
    return f"{head} [{tag}]{sep}{rest}"


_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BARE_URL_RE = re.compile(r"(?<![<(\[])\bhttps?://[^\s<>)\]]+")


def format_for_platform(platform: str, text: str) -> str:
    """Telegram homes: no markdown headers. Discord homes: links in <> (no embeds)."""
    p = (platform or "").lower()
    if p == "telegram":
        return _HEADER_RE.sub("", text)
    if p == "discord":
        return _BARE_URL_RE.sub(lambda m: f"<{m.group(0)}>", text)
    return text


def parse_route_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    return ROUTE_CHANNEL if v == ROUTE_CHANNEL else ROUTE_HOME


def resolve_route_config() -> tuple[str, int]:
    """(``kanban.lifecycle_route``, ``kanban.lifecycle_home_digest_seconds``). Errors -> defaults."""
    from gateway.kanban_lifecycle_digest import parse_digest_seconds

    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        if not isinstance(kcfg, dict):
            kcfg = {}
    except Exception:
        kcfg = {}
    raw = kcfg.get("lifecycle_home_digest_seconds", DEFAULT_HOME_DIGEST_SECONDS)
    return parse_route_mode(kcfg.get("lifecycle_route")), parse_digest_seconds(raw)


def home_lookup_failed(home: Optional[HomeResolution]) -> bool:
    return home is not None and home.target is None and home.reason == LOOKUP_FAILED


def send_failed(res: Any) -> Optional[str]:
    """None when ``res`` is a delivered send; else the one-word reason."""
    if getattr(res, "success", True) is False:
        return getattr(res, "error_kind", None) or "send-failed"
    return None
