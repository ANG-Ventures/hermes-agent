"""Session-scoped context variables for the Hermes gateway.

Replaces the old ``os.environ``-based ``HERMES_SESSION_*`` state with task-local ``ContextVar``s
(inherited by ``run_in_executor`` threads), so concurrently handled messages no longer clobber each
other's routing ids.  ``get_session_env`` is a drop-in for ``os.getenv``.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import logging

logger = logging.getLogger(__name__)

# "Never set here" (falls back to os.environ for CLI/cron) vs "" = explicitly cleared (no fallback).
_UNSET: Any = object()

# Process-level latch: has set_session_vars() ever bound a session?  When engaged, the subprocess
# env bridge treats ContextVars as authoritative and an _UNSET var as "no session in THIS task".
_session_context_engaged: bool = False


def session_context_engaged() -> bool:
    """True if any session has been bound via set_session_vars in this process."""
    return _session_context_engaged


# --- Per-task session variables: bound by set_session_vars / cleared to "" by clear_session_vars;
# tuple ORDER is the positional order of ``values`` in set_session_vars (zipped).
# * SCOPE_ID: platform-neutral scope (guild / workspace / Matrix server) so async producers can
#   persist a completion's full routing origin (relay egress guards need it).
# * UI_SESSION_ID: in-process UI tab id, separate from the durable SESSION_ID, so a stale/rotated
#   durable key is not consumed by the wrong poller.
# * MESSAGE_ID: reply anchor keeping notifications inside the originating Telegram topic.
# * CRON_SESSION: tri-state — _UNSET = legacy env fallback; "1" = cron; "" = non-cron, masks env.
_SESSION_VARS = (
    _SESSION_PLATFORM, _SESSION_SOURCE, _SESSION_CHAT_ID, _SESSION_CHAT_TYPE,
    _SESSION_CHAT_NAME, _SESSION_THREAD_ID, _SESSION_USER_ID, _SESSION_USER_ID_ALT,
    _SESSION_USER_NAME, _SESSION_SCOPE_ID, _SESSION_KEY, _SESSION_ID,
    _SESSION_UI_SESSION_ID, _SESSION_MESSAGE_ID, _SESSION_PROFILE,
    _BROWSER_CONTROL_PRINCIPAL, _BROWSER_CONTROL_TRANSPORT_FAMILY, _CRON_SESSION, _SESSION_PARENT_CHAT_ID,
) = tuple(ContextVar(name, default=_UNSET) for name in (
    "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE", "HERMES_SESSION_CHAT_NAME", "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID", "HERMES_SESSION_USER_ID_ALT", "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_ID",
    "HERMES_UI_SESSION_ID", "HERMES_SESSION_MESSAGE_ID", "HERMES_SESSION_PROFILE",
    "HERMES_BROWSER_CONTROL_PRINCIPAL", "HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY",
    "HERMES_CRON_SESSION", "HERMES_SESSION_PARENT_CHAT_ID",
))

# Whether this channel can route an ASYNC completion back AFTER the turn ends (see
# ``async_delivery_supported()``).  _UNSET => supported (CLI, contextvar-unaware paths); stateless
# adapters (API server, Kanban workers) opt OUT via ``supports_async_delivery = False`` at bind.
_SESSION_ASYNC_DELIVERY = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)

# Request-local proof that the client resumes SessionDB history. No env fallback
# or child-process export: a bound id alone cannot authorize detached delivery.
_SESSION_HISTORY_DELIVERY = ContextVar("HERMES_SESSION_HISTORY_DELIVERY", default=_UNSET)

# Cron auto-delivery vars, set per-job in run_job() so concurrent jobs don't clobber.
_CRON_AUTO_DELIVER_PLATFORM = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)

# Legacy env-var name -> ContextVar for get_session_env (_SESSION_ASYNC_DELIVERY deliberately
# absent: it is a bool capability, read via async_delivery_supported).
_VAR_MAP = {var.name: var for var in (
    *_SESSION_VARS, _CRON_AUTO_DELIVER_PLATFORM, _CRON_AUTO_DELIVER_CHAT_ID,
    _CRON_AUTO_DELIVER_THREAD_ID,
)}


def _runtime_cwd(func: str, *args: Any) -> None:
    """Best-effort call of ``agent.runtime_cwd.<func>``; import/runtime failures are ignored."""
    try:
        from agent import runtime_cwd
        getattr(runtime_cwd, func)(*args)
    except Exception:
        pass


def set_current_session_id(session_id: str) -> None:
    """Synchronize ``HERMES_SESSION_ID`` across ContextVar and ``os.environ``.

    Long-lived single-process entrypoints like the CLI can rotate sessions via
    ``/new``, ``/resume``, ``/branch``, or compression splits without
    reconstructing the entire agent. Tools still consult
    ``get_session_env("HERMES_SESSION_ID")`` with an ``os.environ`` fallback,
    so both storage paths must move together when the active session changes.

    🔴 **Gateway-aware (PRD gateway-session-env-leak):** the gateway runs
    concurrent sessions in ONE process, so an unconditional ``os.environ`` write
    here would clobber every other concurrent session's id (the v3-latch bug
    class). Inside the gateway (``_HERMES_GATEWAY=1``) we bind the **contextvar
    only** — the per-turn ``_set_session_env`` already binds ``session_id`` into
    the contextvar, and every gateway-reachable reader is contextvar-first. The
    ``os.environ`` write is kept for **non-gateway single-process** entrypoints
    (CLI rotation, cron-standalone, dispatcher-spawned workers) whose tools read
    the ``os.environ`` fallback and where there is no concurrency to clobber.
    Delegated subagent children are the exception: they are constructed inside
    the parent process within ``delegated_child_context()``, and their
    ``AIAgent.__init__`` calls this same helper. Writing a child's internal
    session id to ``os.environ`` (process-global) would clobber the parent's
    ``HERMES_SESSION_ID`` for the rest of the process — leaking the child id
    into parent tools and subprocesses spawned after the child was built. The
    ContextVar write below is task-local and safe for concurrent children; only
    the process-global ``os.environ`` mirror is suppressed for delegated
    children. Root agents (CLI, gateway, cron) keep both paths.
    """
    import os

    _SESSION_ID.set(session_id)
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return
    except Exception:
        pass

    # Both guards are load-bearing and independent: the delegated-child check
    # above protects the PARENT's id within one process, and this gateway check
    # protects every CONCURRENT session's id inside the gateway process (the
    # v3-latch bug class — tests/gateway/test_no_gateway_session_env_writes.py).
    if os.environ.get("_HERMES_GATEWAY") != "1":
        os.environ["HERMES_SESSION_ID"] = session_id


@contextmanager
def scoped_current_session_id(session_id: str | None = None) -> Iterator[None]:
    """Bind a task-local session id and restore the prior value on exit; never touches
    ``os.environ``.  ``session_id=None`` is a pure save/restore boundary."""
    previous = _SESSION_ID.get()
    if session_id is not None:
        _SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _SESSION_ID.set(previous)


def source_route_metadata(source: Any, metadata: dict | None) -> dict | None:
    """Keep inbound route anchors for durable deliveries after the source is gone."""
    anchors = {key: str(value) for key in ("scope_id", "parent_chat_id")
               if (value := getattr(source, key, None))}
    return {**(metadata or {}), **anchors} if anchors else metadata


def set_session_vars(
    platform: str = "", source: str = "", chat_id: str = "", chat_type: str = "",
    chat_name: str = "", thread_id: str = "", user_id: str = "", user_id_alt: str = "",
    user_name: str = "", scope_id: str = "", session_key: str = "", session_id: str = "",
    message_id: str = "", profile: str = "", browser_control_principal: str = "",
    browser_control_transport_family: str = "", cwd: str = "", async_delivery: bool = True,
    ui_session_id: str = "", cron_session: Any = _UNSET, parent_chat_id: str = "",
    session_history_delivery: str | None = None,
) -> list:
    """Set all session context variables and return reset tokens.  Call
    ``clear_session_vars(tokens)`` in a ``finally``; not nestable, clearing resets every var
    to ``""`` rather than restoring prior values (tokens are accepted only for API compat).

    Call ``clear_session_vars(tokens)`` in a ``finally`` block when the handler
    exits and needs the explicit-empty state that suppresses ``os.environ``
    fallback. Nested scopes must instead call ``restore_session_vars(tokens)``
    so the outer binding survives.

    ``cwd`` pins the logical working directory for this context.

    ``async_delivery`` declares whether this session's channel can route a
    background completion back to the agent after the turn ends (see
    ``_SESSION_ASYNC_DELIVERY`` / ``async_delivery_supported``). Stateless
    request/response adapters (the API server) pass ``False``.

    ``cron_session`` is tri-state: ``_UNSET`` preserves legacy
    ``os.environ["HERMES_CRON_SESSION"]`` fallback, ``"1"`` marks a cron job,
    and ``""`` explicitly marks a non-cron session while masking leaked env.

    ``session_history_delivery`` declares whether the bound chat id is one the client can address again:
    ``"1"`` (audited producers — explicit session-id header, native API sessions, /v1/runs) or
    ``""`` / omitted (default-deny, #98619).  ``None`` leaves the var at ``_UNSET`` ("never
    declared"), which ``session_history_delivery_supported()`` treats as NOT capable — an omitted declaration
    cannot grant wake authority."""
    # Mark the session-context machinery engaged for this process. The
    # subprocess-env bridge uses this to switch from "os.environ fallback" to
    # "ContextVar-authoritative, strip on _UNSET" — see session_context_engaged.
    global _session_context_engaged
    _session_context_engaged = True
    values = (
        platform, source, chat_id, chat_type, chat_name, thread_id, user_id, user_id_alt,
        user_name, scope_id, session_key, session_id, ui_session_id, message_id, profile,
        browser_control_principal, browser_control_transport_family, cron_session, parent_chat_id,
    )
    tokens = [var.set(value) for var, value in zip(_SESSION_VARS, values)]
    tokens.append(_SESSION_ASYNC_DELIVERY.set(bool(async_delivery)))
    tokens.append(_SESSION_HISTORY_DELIVERY.set(_UNSET if session_history_delivery is None else session_history_delivery))
    try:
        # Keep the cwd token: restore_session_vars() unwinds nested scopes by token.
        from agent.runtime_cwd import set_session_cwd

        tokens.append(set_session_cwd(cwd))
    except Exception:
        pass
    return tokens


def restore_session_vars(tokens: list) -> None:
    """Restore a nested session binding from ContextVar reset tokens.

    Best-effort per token: a single bad token (reused, or created in a
    different Context on a retry path) must not abort restoration of the
    REMAINING vars — a partial restore would leave a mixed identity, the
    exact state this module exists to prevent.
    """
    for token in reversed(tokens or []):
        try:
            token.var.reset(token)
        except Exception:
            logger.warning(
                "session-context: failed to reset %s during nested restore",
                getattr(getattr(token, "var", None), "name", "<unknown>"),
                exc_info=True,
            )


def clear_session_vars(tokens: list) -> None:
    """Mark session context variables as explicitly cleared (``""``, not ``_UNSET``), so
    ``get_session_env`` returns empty instead of stale ``os.environ`` values.  Async-delivery
    goes back to ``_UNSET``: a cleared context is default-supported, not opted-out.  Wake
    capability goes back to ``_UNSET`` too — but for the opposite reason: a cleared context has
    declared nothing, and an undeclared capability FAILS CLOSED (#98619)."""
    for var in _SESSION_VARS:
        var.set("")
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    _SESSION_HISTORY_DELIVERY.set(_UNSET)
    _runtime_cwd("clear_session_cwd")


def reset_session_vars() -> list:
    """Reset every session var to ``_UNSET`` ("never bound here") for THIS context.  Call at
    the top of a fresh task *before* it binds: ``create_task`` snapshots the context, so B's
    task inherits A's already-set vars and a subprocess spawned before B binds would read A's
    identity.  ``_SESSION_ASYNC_DELIVERY`` and ``_SESSION_HISTORY_DELIVERY`` (outside ``_VAR_MAP``)
    are reset explicitly too.  Returns the reset tokens so a caller that only needed the
    pre-bind window clean can ``restore_session_vars`` the inherited binding afterwards."""
    tokens = [var.set(_UNSET) for var in _VAR_MAP.values()]
    tokens.append(_SESSION_ASYNC_DELIVERY.set(_UNSET))
    tokens.append(_SESSION_HISTORY_DELIVERY.set(_UNSET))
    try:
        # The logical cwd goes back to "never bound" too (not "" — that is the
        # cleared state), and its token joins the list so the restore is total.
        from agent import runtime_cwd

        tokens.append(runtime_cwd._SESSION_CWD.set(runtime_cwd._UNSET))
    except Exception:
        pass
    return tokens


def get_session_env(name: str, default: str = "") -> str:
    """Read a session var by legacy ``HERMES_SESSION_*`` name; drop-in for os.getenv.  The
    ContextVar wins if ever set here (even to ``""``); else ``os.environ``; else *default*."""
    var = _VAR_MAP.get(name)
    if var is not None and (value := var.get()) is not _UNSET:
        return value
    return os.getenv(name, default)


def bridge_session_env(env: dict) -> dict:
    """Write the ``HERMES_SESSION_*`` set into a child-process *env*, in place.

    THE one bridge for every spawn surface (terminal, execute_code), so the set a child sees
    cannot drift between them (t_be44b437). Cross-session leak guard: the ``os.environ`` mirror
    is last-writer-wins on a concurrent multi-session host, so once the session context is
    engaged ContextVars are authoritative: a bound value (incl. ``""``) wins and an ``_UNSET``
    var is STRIPPED, not inherited. An unengaged CLI keeps whatever *env* already carries."""
    for var_name, var in _VAR_MAP.items():
        value = var.get()
        if value is not _UNSET:
            env[var_name] = "" if value is None else str(value)
        elif _session_context_engaged:
            env.pop(var_name, None)
    return env


def resolve_current_session_id() -> str | None:
    """Resolve the active session id contextvar-first, os.environ fallback.

    THE single resolver for "which session is running right now", shared by
    every consumer that has to answer that question (comment provenance in
    ``tools.kanban_tools``, the ``execute_code`` child-env bridge). The rules
    below are subtle and were previously carried in one private copy; a second
    copy would drift.

    In the GATEWAY (concurrent sessions in one process) the per-turn contextvar
    is the only correct source — the process-global ``os.environ``
    ``HERMES_SESSION_ID`` can be clobbered by a concurrent session (the v3-latch
    bug class). In a single-process host (CLI, cron-standalone, ACP, a
    dispatcher-spawned worker) there is no bound contextvar and
    :func:`get_session_env` falls through to that process's correct
    ``os.environ`` value.

    🔴 Empty-contextvar fallthrough — GATED on ``not _HERMES_GATEWAY``: some
    non-gateway callers bind the contextvar to ``""`` (e.g. ACP binds
    ``set_session_vars(session_key=session_id)`` which leaves the session_id
    contextvar at its ``""`` default while writing the real id to
    ``os.environ``). A ``""`` contextvar is NOT ``_UNSET``, so
    :func:`get_session_env` returns ``""`` and would NOT fall through. We treat
    an empty contextvar value as "not bound" and consult ``os.environ`` — but
    ONLY outside the gateway. Inside the gateway, :func:`clear_session_vars`
    deliberately sets ``""`` to *suppress* the ``os.environ`` fallback (so a
    cleared post-turn read returns ``None``, never a stale/clobbered global),
    and the per-turn contextvar is authoritative — so we never fall through
    there. This mirrors :func:`set_current_session_id`'s own
    ``not _HERMES_GATEWAY`` guard and preserves both contracts. Returns ``None``
    when neither has a value.
    """
    import os

    try:
        val = get_session_env("HERMES_SESSION_ID")
        if val:
            return val
        # Empty/unset contextvar. Outside the gateway (ACP/CLI/worker — single
        # process, os.environ authoritative) fall through to os.environ. Inside
        # the gateway, "" means cleared-and-fallback-suppressed → return None
        # (the per-turn contextvar is the only correct source; a "" here is
        # never a missing bind because _set_session_env binds session_id).
        if os.environ.get("_HERMES_GATEWAY") == "1":
            return None
        return os.environ.get("HERMES_SESSION_ID") or None
    except Exception:
        return os.environ.get("HERMES_SESSION_ID") or None


# ---------------------------------------------------------------------------
# Send-origin context (routing-only) — fixes the subagent send_message leak.
# ---------------------------------------------------------------------------
# A delegate_task child runs in a bare ThreadPoolExecutor worker with an EMPTY
# contextvars context, so the HERMES_SESSION_* vars above are unset inside it.
# A subagent calling send_message/react with a bare target therefore had no
# session origin and silently fell back to the global home channel (the v2
# leak). These dedicated vars carry ONLY the routing origin (platform/chat/
# thread) the send resolver needs — they are read EXCLUSIVELY by
# send_message_tool's origin resolver, never by approval/skills/TTS/cache, so a
# child keeps its own `platform="subagent"` identity for everything else.
#
# CONTEXTVAR-ONLY by design: NO os.environ fallback. An os.environ fallback
# would re-introduce the exact cross-executor leakage we are fixing (a child
# would read the parent process-global). The delegate child-run wrapper binds
# these from the captured parent origin and clears them in a finally.
_SEND_ORIGIN_PLATFORM: ContextVar = ContextVar("HERMES_SEND_ORIGIN_PLATFORM", default=_UNSET)
_SEND_ORIGIN_CHAT_ID: ContextVar = ContextVar("HERMES_SEND_ORIGIN_CHAT_ID", default=_UNSET)
_SEND_ORIGIN_THREAD_ID: ContextVar = ContextVar("HERMES_SEND_ORIGIN_THREAD_ID", default=_UNSET)


def set_send_origin(platform: str, chat_id: str, thread_id: str = "") -> list:
    """Bind the routing-only send origin for the current context (e.g. a
    subagent run). Returns reset tokens; pass to ``clear_send_origin`` in a
    ``finally``. Contextvar-only — never touches os.environ.
    """
    return [
        _SEND_ORIGIN_PLATFORM.set(platform or ""),
        _SEND_ORIGIN_CHAT_ID.set(chat_id or ""),
        _SEND_ORIGIN_THREAD_ID.set(thread_id or ""),
    ]


def clear_send_origin(tokens: list) -> None:
    """Restore the send-origin vars to their prior values (true reset, so this
    IS nestable — a grandchild's clear restores the child's origin, not blank).
    ``tokens`` is the list returned by ``set_send_origin``; ``None``/empty is a
    safe no-op (used when no origin was bound).
    """
    if not tokens:
        return
    for var, token in zip(
        (_SEND_ORIGIN_PLATFORM, _SEND_ORIGIN_CHAT_ID, _SEND_ORIGIN_THREAD_ID),
        tokens,
    ):
        try:
            var.reset(token)
        except Exception:
            # Token from a different context (shouldn't happen — same thread
            # set+clear) — fall back to clearing to empty.
            var.set("")


def get_send_origin() -> tuple:
    """Return ``(platform, chat_id, thread_id)`` for the routing-only send
    origin, or ``("", "", "")`` when unset. Contextvar-only (no os.environ).
    """
    def _v(var):
        val = var.get()
        return "" if val is _UNSET else val

    return (
        _v(_SEND_ORIGIN_PLATFORM),
        _v(_SEND_ORIGIN_CHAT_ID),
        _v(_SEND_ORIGIN_THREAD_ID),
    )


def set_cron_session():
    """Mark the current context (a cron job's run, or a cron-spawned subagent)
    as a cron session. Returns a reset token; pass it to ``clear_cron_session``
    in a ``finally``. Contextvar-only — never touches os.environ (writing the
    process-global env is exactly the latch bug this replaces).

    The set is nestable via ``var.reset(token)`` so a cron subagent that re-binds
    the marker and then clears it restores the parent's value, not blank.
    """
    return _CRON_SESSION.set("1")


def clear_cron_session(token) -> None:
    """Restore the cron-session marker to its prior value. ``token`` is the
    value returned by ``set_cron_session``; ``None`` is a safe no-op (used when
    the marker was not bound — e.g. an interactive parent spawning a child).
    """
    if token is None:
        return
    try:
        _CRON_SESSION.reset(token)
    except Exception:
        # Token from a different context (shouldn't happen — same-thread
        # set+clear). Restore the _UNSET sentinel (NOT "") so get_session_env
        # still falls through to os.environ — setting "" would pin the
        # ContextVar to a non-_UNSET value and silently defeat the I5
        # os.environ back-compat fallback for CLI/standalone/test callers.
        _CRON_SESSION.set(_UNSET)


def is_cron_session() -> bool:
    """True when the current context is a cron job's run (or a cron-spawned
    subagent that re-bound the marker). Reads the ContextVar first, falling back
    to ``os.environ`` for CLI / standalone-scheduler / test compatibility (same
    resolution order as ``get_session_env``).

    This is the single context-aware reader that replaces the raw
    ``env_var_enabled("HERMES_CRON_SESSION")`` calls in approval + send_message.
    Because it goes through ``get_session_env`` (ContextVar -> os.environ ->
    default), a cron job's marker is isolated to its own task/thread lineage and
    can never be read by a concurrent interactive gateway turn.
    """
    from utils import is_truthy_value

    return is_truthy_value(get_session_env("HERMES_CRON_SESSION", ""), default=False)
# Surfaces that are not a human chat channel (gateway binds HERMES_SESSION_PLATFORM, CLI/TUI/
# desktop bind HERMES_SESSION_SOURCE, so both are consulted).  Default-deny: an unrecognized
# identity counts as messaging.  Mirrors LOCAL_SESSION_SOURCE_IDS in apps/desktop session-source.ts.
NON_MESSAGING_SESSION_SURFACES = frozenset({
    "", "api_server", "cli", "codex", "desktop", "gateway", "kanban", "local",
    "msgraph_webhook", "tool", "tui", "webhook",
})


def session_is_messaging_surface() -> bool:
    """Whether this turn is delivered over a human messaging channel (checks
    ``HERMES_PLATFORM``, then the session platform, then the session source)."""
    platform = os.getenv("HERMES_PLATFORM") or get_session_env("HERMES_SESSION_PLATFORM", "")
    idents = (platform, get_session_env("HERMES_SESSION_SOURCE", ""))
    idents = (str(v or "").strip().lower() for v in idents)
    return any(ident and ident not in NON_MESSAGING_SESSION_SURFACES for ident in idents)


def declare_stateless_channel() -> None:
    """Declare that this session cannot receive an async background completion.  Unlike
    ``set_session_vars(async_delivery=False)`` this does NOT latch ``_session_context_engaged``
    (flipping the subprocess env bridge), which a one-shot CLI must not do as a side effect.

    See NousResearch/hermes-agent#53027 and #63142.
    """
    _SESSION_ASYNC_DELIVERY.set(False)


def async_delivery_supported() -> bool:
    """Whether the current session can deliver a background completion later.  False for
    stateless channels (:func:`declare_stateless_channel`) and Kanban workers
    (``HERMES_KANBAN_TASK``: one-shot subprocesses whose parent disappears after the turn)."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    value = _SESSION_ASYNC_DELIVERY.get()
    return True if value is _UNSET else bool(value)


def session_history_delivery_supported() -> bool:
    """Whether this request declares a server-history consumer for detached results.

    Fail closed on omitted bindings; never borrow authority from the environment."""
    return _SESSION_HISTORY_DELIVERY.get() == "1"
