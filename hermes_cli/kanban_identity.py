"""Shared identity helpers for dispatcher-spawned Kanban workers.

Two concerns live here:

* ``resolve_kanban_worker_chat_identity`` — the worker's chat id / display name.
* ``resolve_comment_provenance`` — the *trusted* per-run / per-session
  attribution stamped onto a comment. Two concurrent sessions running the SAME
  profile used to be indistinguishable on the board (both rendered as
  ``apollo``); this resolves the run id and a bounded session fingerprint from
  runtime context so they no longer are.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Optional

_log = logging.getLogger(__name__)


def resolve_kanban_worker_chat_identity(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(chat_id, chat_name)`` from the worker's pinned environment."""
    source = os.environ if env is None else env
    task_id = (source.get("HERMES_KANBAN_TASK") or "").strip()
    board = (source.get("HERMES_KANBAN_BOARD") or "").strip()
    chat_name = (
        " / ".join(part for part in ("kanban", board, task_id) if part)
        if task_id
        else ""
    )
    return task_id, chat_name


def _resolve_current_session_id() -> Optional[str]:
    """Resolve the active session id, contextvar-first with an env fallback.

    Delegates to ``tools.kanban_tools._current_session_id`` so the gateway
    concurrency rules (per-turn contextvar is authoritative; a cleared "" must
    not fall through to a clobbered global) are enforced in exactly one place.
    Falls back to the raw env var when that import is unavailable (a trimmed
    install without the agent toolset).
    """
    try:
        from tools.kanban_tools import _current_session_id

        return _current_session_id()
    except Exception:
        return os.environ.get("HERMES_SESSION_ID") or None


def _owns_dispatcher_run(env: Mapping[str, str] | None) -> bool:
    """True when this process may attest to ``HERMES_KANBAN_RUN_ID``.

    Ambient ``HERMES_KANBAN_*`` is inherited by every descendant process, so a
    nested Hermes subprocess would otherwise stamp its parent worker's run id
    onto its own comments — attributing a write to a run that never made it.
    Only meaningful for the live process env; an explicit ``env`` mapping is a
    caller-supplied snapshot (the dashboard passes one) and keeps its old
    semantics.
    """
    if env is not None:
        return True
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return True


def resolve_comment_provenance(
    task_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[Optional[int], Optional[str]]:
    """Return ``(run_id, session_ref)`` for a comment about ``task_id``.

    Both values come from trusted runtime context only — never from tool args
    or comment text — so a model cannot attribute its writes to another run or
    session (see ``kanban_db.add_comment``'s validation for the write-side
    choke point).

    ``run_id`` is only returned when ``HERMES_KANBAN_RUN_ID`` is scoped to
    ``task_id`` (same gate ``_worker_run_id`` applies to complete/block/
    heartbeat). A worker's run attests to its OWN card; stamping it on a
    cross-task comment would claim a write the run never made there.

    ``session_ref`` is the bounded fingerprint of the originating session id and
    is always safe to record: it identifies *which session wrote this*, which is
    the whole point on a cross-task handoff.
    """
    from hermes_cli.kanban_db import derive_session_ref

    source = os.environ if env is None else env
    run_id: Optional[int] = None
    if source.get("HERMES_KANBAN_TASK") == task_id and _owns_dispatcher_run(env):
        raw = source.get("HERMES_KANBAN_RUN_ID")
        if raw:
            try:
                run_id = int(raw)
            except (TypeError, ValueError):
                run_id = None
            else:
                if run_id <= 0:
                    run_id = None
    if env is None:
        session_id = _resolve_current_session_id()
    else:
        session_id = source.get("HERMES_SESSION_ID") or None
    return run_id, derive_session_ref(session_id)


def worker_ancestor_profile() -> Optional[str]:
    """Profile of the dispatcher-spawned worker this process runs under, or None.

    Read from process identity, not the environment: a worker shell can
    ``env -u HERMES_KANBAN_TASK HERMES_PROFILE=default`` and every env-derived
    identity then reads ``default`` (Prism 41fd439722e6, hermes-agent#1782).
    Candidates are this process, its parent chain, and its SESSION id: workers
    are spawned with ``start_new_session=True`` (worker pid == sid), so a
    double-forked helper that init has adopted still carries the worker's sid
    (Prism d11c14fcd7fe). A run counts only when its recorded ``worker_pid``
    is a candidate AND that pid's spawn fingerprint still matches (a recycled
    pid is never a worker; legacy and ``unverified`` rows are skipped). Every
    board is read at its PHYSICAL path, not through the caller's
    ``HERMES_KANBAN_DB`` pin, which would collapse every slug onto one DB
    (Prism 9e1b780a6387); the caller's write-target DB is read too.
    Unreadable psutil/board -> None.
    """
    try:
        import psutil

        me = psutil.Process()
        chain = [me.pid] + [p.pid for p in me.parents()]
    except Exception:
        return None
    try:
        chain.append(os.getsid(0))
    except (AttributeError, OSError):
        pass
    chain = list(dict.fromkeys(pid for pid in chain if pid > 1))
    if not chain:
        return None
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_dispatch import _process_fingerprint

        with kb.enumerating_boards():
            paths = {str(kb._board_db_path_ignoring_pin(b["slug"]))
                     for b in kb.list_boards() if b.get("slug")}
        try:
            paths.add(str(kb.kanban_db_path()))
        except Exception:
            pass
    except Exception:
        return None
    import sqlite3
    from pathlib import Path

    found: dict[int, list[tuple[str, str]]] = {}
    marks = ",".join("?" * len(chain))
    for raw in sorted(p for p in paths if p):
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
            try:
                rows = conn.execute(
                    f"SELECT worker_pid, profile, worker_started_at FROM task_runs "
                    f"WHERE worker_pid IN ({marks}) ORDER BY id DESC", tuple(chain),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for pid, profile, started in rows:
            if profile and isinstance(started, str) and "|" in started:
                found.setdefault(int(pid), []).append((str(profile), started))
    for pid in chain:  # nearest worker ancestor wins; the session id is last
        for profile, started in found.get(pid, ()):
            if _process_fingerprint(pid) == started:
                return profile
    return None


def is_operator_label(name: Optional[str]) -> bool:
    """An author label operator-trust readers honour: a ruling author, an
    operator profile, or ``human:<name>``."""
    from hermes_cli.kanban_db import OPERATOR_PROFILES
    from hermes_cli.kanban_worker_policy import RULING_AUTHORS

    v = (name or "").strip().lower()
    return v in RULING_AUTHORS or v in OPERATOR_PROFILES or v.startswith("human:")


def verified_profile_author(name: str) -> str:
    """``name`` (an env-derived profile identity), unless it claims an operator
    identity from inside a dispatched worker: then that worker's own profile.

    Only operator-trusted names are checked (``RULING_AUTHORS`` and
    ``OPERATOR_PROFILES``), so a worker's ordinary identity costs nothing.
    """
    if not is_operator_label(name):
        return name
    worker = worker_ancestor_profile()
    return worker if worker and worker != name else name


def safe_comment_provenance(
    task_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[Optional[int], Optional[str]]:
    """:func:`resolve_comment_provenance`, but it can never fail a write.

    Provenance is an ANNOTATION on a comment, never an admission gate. Comments
    are the coordination channel this whole change exists to protect, so if
    resolution raises for any reason — a trimmed install, a contextvar backend
    change, an unexpected env shape — the correct outcome is an unattributed
    comment, not a lost one. Degrades to ``(None, None)``, which every read
    surface renders as an explicit unknown rather than a guess.

    Use this at call sites. Use :func:`resolve_comment_provenance` when you
    actually want the failure.
    """
    try:
        return resolve_comment_provenance(task_id, env=env)
    except Exception:
        _log.debug("kanban: comment provenance resolution failed", exc_info=True)
        return None, None
