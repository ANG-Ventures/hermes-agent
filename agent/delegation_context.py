"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, overload

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

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
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
    # Fork worker identity: exit-status sidecar path and the non-transitive owner pid (#586).
    "HERMES_KANBAN_EXIT_FILE", KANBAN_OWNER_PID_ENV,
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


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


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use.

    False for delegate_task children, for cron jobs fired in-process from a worker, and
    (fork #586) for ordinary child processes that merely inherited a worker's environment.
    """
    if is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get():
        return False
    return owns_kanban_worker_authority()


def owned_kanban_task() -> str:
    """The board task this execution OWNS: ``HERMES_KANBAN_TASK`` for the dispatcher-owned
    worker, ``""`` otherwise. Tool access is not worker identity — a profile can expose the
    kanban toolset interactively, and children/cron runs inherit the env var — so every
    reader that turns the task id into worker behaviour (guidance, stop nudge, terminal
    outcomes) goes through this one helper."""
    if not is_dispatcher_owned_worker_context():
        return ""
    return (os.environ.get("HERMES_KANBAN_TASK") or "").strip()


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


def _fenced_kanban_root() -> str:
    """The board root this process's Kanban lineage lives under (``kanban_home()``); ``"1"`` when it
    cannot be resolved, which readers treat as "fence every board" (the pre-path marker)."""
    try:
        from hermes_cli.kanban_db import kanban_home
        return str(kanban_home())
    except Exception:
        return "1"


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove worker identity, retaining board/location and an inherited write fence.

    TASK absence alone would promote a descendant to an orchestrator. The marker
    survives later execs, including scripts that remove TASK themselves. This is
    cooperative runtime scoping, not confinement of code with direct SQLite access.

    The marker's value is the fenced board ROOT, so the fence applies to the lineage's
    board and not to every Kanban DB the descendant touches: a child running a repro
    against a temp ``HERMES_HOME`` got a silently read-only board there. An inherited
    path-valued marker is kept (a grandchild that moved HERMES_HOME must not re-fence
    onto its scratch root and unfence the real one).
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited and inherited != "1" else _fenced_kanban_root()
    return cleaned


def kanban_path_is_fenced(path: "os.PathLike[str] | str") -> bool:
    """Whether Kanban mutations at *path* (a board DB or board-metadata root) are denied for this
    process: always for an in-process delegate child (the parent's own board); for a spawned
    descendant only when *path* is the dispatcher-pinned ``HERMES_KANBAN_DB`` or lies under the
    fenced root the marker carries. A legacy ``"1"`` marker fences everything."""
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    marker = os.environ.get(DELEGATED_CHILD_ENV_MARKER, "")
    if not marker:
        return False
    if marker == "1":
        return True
    from pathlib import Path
    target = Path(path).expanduser().resolve()
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned and target == Path(pinned).expanduser().resolve():
        return True
    try:
        target.relative_to(Path(marker).expanduser().resolve())
    except ValueError:
        return False
    return True


@overload
def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]: ...


@overload
def delegated_child_subprocess_env(env: None = None) -> dict[str, str] | None: ...


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry worker/delegate descendant denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    Dispatcher workers and supervised tool transports grant their own explicit scope.
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK") or env.get(DELEGATED_CHILD_ENV_MARKER)))):
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
