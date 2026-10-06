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


def _ancestry() -> list[int]:
    """This process, its parent chain, then its session id (pids > 1, deduped)."""
    try:
        import psutil

        me = psutil.Process()
        chain = [me.pid] + [p.pid for p in me.parents()]
    except Exception:
        return []
    try:
        chain.append(os.getsid(0))
    except (AttributeError, OSError):
        pass
    return list(dict.fromkeys(pid for pid in chain if pid > 1))


def _worker_rows(chain: list[int]) -> tuple[dict[int, list[tuple[str, str]]], bool]:
    """``{pid: [(profile, worker_started_at), ...]}`` for every ``task_runs`` row
    whose ``worker_pid`` is in ``chain``, on every board (newest first).

    Every board is read at its PHYSICAL path, not through the caller's
    ``HERMES_KANBAN_DB`` pin, which would collapse every slug onto one DB
    (Prism 9e1b780a6387); the caller's write-target DB is read too. Rows
    without a profile or fingerprint string are dropped.

    Returns ``(rows, complete)``. ``complete`` is False when the board list or
    ANY board was unreadable: an unread board may hold the worker row, so
    absence is never proven from an incomplete scan (Prism c6316b5d4c3b),
    while a verified match on a readable board still identifies the worker
    (Prism c4bf93c192f3).
    """
    try:
        from hermes_cli import kanban_db as kb

        with kb.enumerating_boards():
            paths = {str(kb._board_db_path_ignoring_pin(b["slug"]))
                     for b in kb.list_boards() if b.get("slug")}
        try:
            paths.add(str(kb.kanban_db_path()))
        except Exception:
            pass
    except Exception:
        return {}, False
    import sqlite3
    from pathlib import Path

    found: dict[int, list[tuple[str, str]]] = {}
    complete = True
    if not chain:
        return found, complete
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
            complete = False
            continue
        for pid, profile, started in rows:
            if profile and isinstance(started, str) and started:
                found.setdefault(int(pid), []).append((str(profile), started))
    return found, complete


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
    pid is never a worker; legacy and ``unverified`` rows are skipped).
    Unreadable psutil/board -> None.
    """
    chain = _ancestry()
    if not chain:
        return None
    found, _complete = _worker_rows(chain)
    if not found:
        return None
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    for pid in chain:  # nearest worker ancestor wins; the session id is last
        for profile, started in found.get(pid, ()):
            if "|" in started and _process_fingerprint(pid) == started:
                return profile
    return None


def is_operator_label(name: Optional[str]) -> bool:
    """An author label operator-trust readers honour: a ruling author, an
    operator profile, or ``human:<name>``."""
    from hermes_cli.kanban_db import OPERATOR_PROFILES
    from hermes_cli.kanban_worker_policy import RULING_AUTHORS

    v = (name or "").strip().lower()
    return v in RULING_AUTHORS or v in OPERATOR_PROFILES or v.startswith("human:")


# Prefix on an operator label the caller could not prove it holds (t_3b9dbdb1).
# A PREFIX, so the result can never match an operator label: a suffix left
# ``human:apollo-unverified`` inside the ``human:`` namespace (Prism
# 58ee6155c6e1). No operator-trust reader honours the comment, but the board
# still shows who claimed to write it.
UNVERIFIED_AUTHOR_PREFIX = "unverified:"

# Operator labels that name the ROOT profile's gateway (Apollo runs as ``default``).
_ROOT_PROFILE_LABELS = frozenset({"default", "apollo"})


def _operator_gateway_pids(name: str) -> frozenset[int]:
    """Verified pids of the live gateway(s) that serve operator profile
    ``name``: the gateway that home owns, and the host multiplexer when it
    serves that profile (a served profile owns no gateway.pid, and a
    multiplexer launched from a named profile records its identity there, not
    at the root; Prism cfdcb98d4fbe). Empty for ``human:*``, ``ace``/``user``
    and unknown profiles."""
    from pathlib import Path

    from hermes_constants import get_default_hermes_root

    v = (name or "").strip().lower()
    root = Path(get_default_hermes_root())
    if v in _ROOT_PROFILE_LABELS:
        home = root
    else:
        from hermes_cli.kanban_db import OPERATOR_PROFILES

        if v not in OPERATOR_PROFILES:
            return frozenset()
        home = root / "profiles" / v
        if not home.is_dir():
            return frozenset()
    pids: set[int] = set()
    try:
        from gateway.status import live_gateway_pid_for_home

        pid = live_gateway_pid_for_home(home)
        if pid:
            pids.add(int(pid))
    except Exception:
        _log.debug("kanban: operator gateway pid lookup failed", exc_info=True)
    try:
        from gateway.status import multiplexer_liveness_for_profile

        served = multiplexer_liveness_for_profile(home)
        if served and served[0]:
            pids.add(int(served[0]))
    except Exception:
        _log.debug("kanban: operator multiplexer lookup failed", exc_info=True)
    return frozenset(pids)


def _runs_under_operator_gateway(name: str) -> bool:
    """True when this process IS, or descends from, the live gateway of
    operator profile ``name`` with no dispatched worker in between.

    The gateway's in-process ``/kanban`` and tool calls, its cron scripts and
    the terminal shells of its own sessions all sit under it. A worker's
    helper either still sits under its worker (a worker row on the path) or
    was reparented to init (the gateway is no longer an ancestor). A worker
    row counts here even when its fingerprint is ``unverified``: authorship
    fails closed, unlike kill/reap (t_3b9dbdb1).
    """
    gws = _operator_gateway_pids(name)
    if not gws:
        return False
    try:
        import psutil

        me = psutil.Process()
        path = [me.pid] + [p.pid for p in me.parents()]
    except Exception:
        return False
    at = next((i for i, pid in enumerate(path) if pid in gws), None)
    if at is None:
        return False
    gw = path[at]
    below = path[:at]
    try:
        below.append(os.getsid(0))
    except (AttributeError, OSError):
        pass
    below = [pid for pid in dict.fromkeys(below) if pid > 1 and pid != gw]
    found, complete = _worker_rows(below)
    if not complete:  # an unreadable board is indeterminate, never "no worker"
        return False
    if not found:
        return True
    from hermes_cli.kanban_db_dispatch import (
        UNVERIFIED_WORKER_FINGERPRINT,
        _process_fingerprint,
    )

    for pid, rows in found.items():
        for _profile, started in rows:
            if started == UNVERIFIED_WORKER_FINGERPRINT or _process_fingerprint(pid) == started:
                return False
    return True


def routing_profile_identity(name: str) -> str:
    """``name`` for ROUTING and FILTERS (notify subscription owner, ``list
    --mine``): a dispatched worker's own profile when one is an ancestor, else
    ``name`` unchanged. Never persisted as an author, so it needs no operator
    proof and never takes the ``unverified:`` prefix, which would route to no
    gateway and match no card (Prism 7c91838738d2 / 6c97e67077d9).
    """
    if not is_operator_label(name):
        return name
    return worker_ancestor_profile() or name


def operator_label_proven(name: str) -> bool:
    """The caller may PERSIST operator label ``name`` as itself: it presents the
    operator token, or it runs under that operator profile's live gateway with
    no dispatched worker in between. The env name alone proves nothing."""
    from hermes_cli import kanban_db as kb

    if kb._operator_token_state() == "ok":
        return True
    return _runs_under_operator_gateway(name)


def verified_profile_author(name: str) -> str:
    """The author label to persist for env-derived profile identity ``name``.

    Only operator labels (``is_operator_label``) are checked, so a worker's
    ordinary identity costs nothing:

    * inside a dispatched worker: that worker's own profile (Prism 41fd439722e6);
    * outside one: ``name`` only with :func:`operator_label_proven`, else
      ``unverified:<name>``. An orphaned, setsid'd helper of a worker has no
      worker in its ancestry and could otherwise write ``apollo`` by setting
      ``HERMES_PROFILE`` (Prism e8be54683982 / 05c79d97284b, t_3b9dbdb1).
    """
    if not is_operator_label(name):
        return name
    worker = worker_ancestor_profile()
    if worker:
        return worker
    if operator_label_proven(name):
        return name
    return f"{UNVERIFIED_AUTHOR_PREFIX}{name.strip()}"


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
