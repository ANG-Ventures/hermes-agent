"""Context-local state for delegate_task child execution.

The parent Hermes process may itself be a Kanban dispatcher worker with
HERMES_KANBAN_* variables in process env. delegate_task children run inside the
same Python process, but they are not dispatcher-owned Kanban workers. This
module lets code paths that resolve tool schemas or spawn subprocesses fail
closed for delegated children without mutating global os.environ for the parent.

Cron jobs need the same treatment for the same reason: ``cronjob(action="run")``
executes ``run_job()`` in-process, so a cron agent fired from inside a Kanban
worker would otherwise inherit that worker's dispatcher identity.
``non_dispatcher_owned_context()`` covers both cases.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_delegated_child_context",
    default=False,
)

# Set for any in-process execution that is NOT the dispatcher-owned worker even
# though the worker's HERMES_KANBAN_* vars are legitimately in os.environ (cron
# jobs fired via the `cronjob` tool).  Kept separate from
# _DELEGATED_CHILD_CONTEXT so the delegate_task-specific behaviour attached to
# that flag (subprocess env scrubbing, its own error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_non_dispatcher_owned_context",
    default=False,
)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

# Records WHICH process the dispatcher's Kanban grant belongs to. See
# ``owns_kanban_worker_authority`` — this is the anchor that makes worker
# authorization non-transitive across an ordinary subprocess boundary.
KANBAN_OWNER_PID_ENV = "HERMES_KANBAN_OWNER_PID"

# Single-use sentinel the dispatcher stamps instead of a pid. ``Popen`` cannot
# know the child's pid before it execs, so the dispatcher writes this and the
# first Hermes CLI process to boot rewrites it to its own pid
# (``claim_kanban_worker_authority``). Because the grant is consumed on the way
# in, every LATER process in that tree inherits a resolved pid that is not its
# own and therefore cannot re-claim it.
KANBAN_OWNER_PID_PENDING = "pending"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    KANBAN_OWNER_PID_ENV,
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity.

    Child construction calls ``set_current_session_id`` internally, so even a
    context entered without an id must restore the parent's ContextVar.  Child
    execution passes its explicit id and receives it only for this scope.
    """
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        # Import lazily: session_context calls is_delegated_child_context() when
        # deciding whether the compatibility os.environ mirror is safe.
        from gateway.session_context import scoped_current_session_id

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task.

    A Kanban worker is a normal CLI agent whose default toolset includes
    ``cronjob``; ``cronjob(action="run")`` runs ``run_job()`` inside the worker's
    own process, where ``HERMES_KANBAN_TASK`` is legitimately set.  Without this
    marker the cron agent is misread as that worker: the kanban toolset is
    force-added, the worker protocol is injected into its system prompt, and
    ``kanban_complete`` defaults ``task_id`` to ``$HERMES_KANBAN_TASK`` — letting
    an unrelated cron job close the worker's task and overwrite real results.

    Scoped via ContextVar rather than by clearing ``os.environ``: the env is
    process-global and shared with the worker's own claim heartbeat, the
    gateway's Kanban watchers, and concurrent cron jobs on the parallel pool, so
    mutating it would starve the worker's claim and race those readers.
    """
    token = _NON_DISPATCHER_OWNED_CONTEXT.set(True)
    try:
        yield
    finally:
        _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


def owns_kanban_worker_authority() -> bool:
    """Return True only when THIS process is the dispatcher's granted worker.

    The dispatcher's ``HERMES_KANBAN_*`` vars are ambient process environment,
    and ambient environment is inherited by *every* child process. That makes
    them proof that a Kanban grant exists somewhere in this process tree — not
    proof that it belongs to the process reading them. An ordinary nested
    ``hermes chat`` launched from a worker's own shell inherited the whole set
    and completed its parent's card with an unrelated summary while the owning
    worker was still running (2026-08-12, card ``t_09b90233``).

    ``HERMES_KANBAN_OWNER_PID`` closes that hole. The dispatcher stamps the pid
    it is about to spawn, and this predicate requires that pid to equal
    ``os.getpid()``. A child inherits the *grant* but cannot inherit the
    *identity the grant names*: its pid necessarily differs, so authority stops
    at exactly one process. Authorization becomes explicit and non-transitive
    instead of ambient.

    Fails OPEN when the marker is absent so pre-existing surfaces keep working:
    a hand-driven ``HERMES_KANBAN_TASK=... hermes chat``, an older dispatcher
    that predates the stamp, and every test that sets only the task var. Those
    callers are unchanged. Once the stamp IS present it is authoritative, which
    is what makes a dispatcher-spawned worker's children fail closed.
    """
    import os

    owner = (os.environ.get(KANBAN_OWNER_PID_ENV) or "").strip()
    if not owner:
        return True
    if owner == KANBAN_OWNER_PID_PENDING:
        # The grant was issued but never claimed by a booting CLI. Treat it as
        # unowned rather than as everyone's: an unclaimed grant must not become
        # a second authority for an inheriting child.
        return False
    try:
        return int(owner) == os.getpid()
    except (TypeError, ValueError):
        # A corrupt marker is not a grant. Refuse rather than guess.
        return False


def claim_kanban_worker_authority() -> bool:
    """Bind a pending dispatcher grant to THIS process. Idempotent.

    Called once during CLI startup. Converts the dispatcher's single-use
    ``pending`` sentinel into this process's concrete pid, so the grant is
    consumed exactly once by the process the dispatcher actually spawned.
    Every later process in the tree inherits the resolved pid, sees it is not
    its own, and is refused.

    Returns True when this call bound the grant. Re-entrant calls from the
    owning process return True without rewriting; any other state is left
    untouched so a non-worker CLI can never mint authority for itself.
    """
    import os

    owner = (os.environ.get(KANBAN_OWNER_PID_ENV) or "").strip()
    if owner == KANBAN_OWNER_PID_PENDING:
        os.environ[KANBAN_OWNER_PID_ENV] = str(os.getpid())
        return True
    return owner == str(os.getpid())


def is_dispatcher_owned_worker_context() -> bool:
    """Return True only when this execution owns the dispatcher's Kanban task.

    The single predicate every ``HERMES_KANBAN_*`` identity gate should use
    before trusting those vars.  False for delegate_task children, for cron
    jobs fired in-process from a worker, and for ordinary child processes that
    merely inherited a worker's environment.
    """
    if _DELEGATED_CHILD_CONTEXT.get():
        return False
    if _NON_DISPATCHER_OWNED_CONTEXT.get():
        return False
    return owns_kanban_worker_authority()


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token-based form of :func:`non_dispatcher_owned_context`.

    For callers whose scope is a long ``try`` with a matching ``finally`` rather
    than a ``with`` block (``cron.scheduler.run_job``).  Pair with
    :func:`exit_non_dispatcher_owned_context`.
    """
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    import os

    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(
        os.environ.get(DELEGATED_CHILD_ENV_MARKER)
    )


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Return *env* with dispatcher-only Kanban variables removed."""
    cleaned = dict(env)
    for key in KANBAN_ENV_KEYS:
        cleaned.pop(key, None)
    cleaned[DELEGATED_CHILD_ENV_MARKER] = "1"
    return cleaned


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return an env override only when delegated-child lineage must cross fork.

    Most subprocess call sites historically used ``env=None`` to inherit the
    process environment.  In a ``delegate_task`` child, inheriting as-is leaks
    parent dispatcher ``HERMES_KANBAN_*`` vars while losing the ContextVar in
    the new process.  This helper preserves normal ``env=None`` semantics for
    non-delegated calls, and only materializes a scrubbed env when the lineage
    marker must be propagated across a child-process boundary.
    """
    if not is_delegated_child_process_context():
        return None if env is None else dict(env)

    if env is None:
        import os

        env = os.environ
    return scrub_kanban_env(env)
