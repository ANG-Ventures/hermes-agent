"""Kanban worker authority: the sites that must NOT trust ambient env.

Companion to ``test_kanban_worker_authority_isolation.py``, which covers the
authority ANCHOR (``HERMES_KANBAN_OWNER_PID`` — the dispatcher stamps a
single-use ``pending`` sentinel, the booting worker CLI binds it to its own pid)
and the two lifecycle handlers gated on it.

This file covers the remaining read-side trust decisions that still resolved
``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID`` straight out of ``os.environ``,
plus the fork-boundary seal that keeps the anchor's deliberate fail-open from
becoming a bypass.

Two distinct hazards are asserted here:

1. **Ambient read** — a non-owning process (a nested ``hermes``, an in-process
   cron job, a delegate_task child) reads the inherited task/run pins and
   attributes its own writes to the owning worker's run. The
   ``expected_run_id`` values are not cosmetic: ``complete_task`` /
   ``heartbeat_worker`` / ``request_review`` use them as an optimistic-
   concurrency guard, so an inherited run id lets a non-owner satisfy the very
   check that exists to stop a stale writer from closing a live card.

2. **Fail-open leak** — ``owns_kanban_worker_authority`` returns True when the
   owner marker is ABSENT (back-compat for hand-driven workers and pre-stamp
   dispatchers). That is safe only while the marker travels with the task id.
   A child env carrying ``HERMES_KANBAN_TASK`` WITHOUT the marker re-opens the
   whole 2026-08-12 hole, which is exactly what an ``env_passthrough`` opt-in on
   the task var produces.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def as_non_owner(monkeypatch: pytest.MonkeyPatch):
    """This process holds a worker's env but the grant names ANOTHER process."""
    from agent.delegation_context import KANBAN_OWNER_PID_ENV

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_victim")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "4242")
    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, str(os.getpid() + 1))
    return "t_victim"


@pytest.fixture
def as_owner(monkeypatch: pytest.MonkeyPatch):
    """This process IS the dispatcher's worker (positive control)."""
    from agent.delegation_context import (
        KANBAN_OWNER_PID_ENV,
        KANBAN_OWNER_PID_PENDING,
        claim_kanban_worker_authority,
    )

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_victim")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "4242")
    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, KANBAN_OWNER_PID_PENDING)
    assert claim_kanban_worker_authority() is True
    return "t_victim"


# ---------------------------------------------------------------------------
# 1. Ambient reads in the tool layer
# ---------------------------------------------------------------------------

def test_non_owner_cannot_stamp_the_owners_run_id(as_non_owner):
    """``_worker_run_id`` feeds ``expected_run_id`` on complete/block/review.

    Returning the inherited run id lets a non-owner PASS the optimistic-
    concurrency guard that exists to keep a stale writer from closing a live
    card — the guard would confirm the impostor as the current run owner.
    """
    import tools.kanban_tools as kt

    assert kt._worker_run_id(as_non_owner) is None


def test_owner_still_gets_its_own_run_id(as_owner):
    """Positive control: the real worker must keep passing its own guard."""
    import tools.kanban_tools as kt

    assert kt._worker_run_id(as_owner) == 4242


def test_non_owner_does_not_stamp_worker_session_metadata(as_non_owner):
    """``worker_session_id`` is a TRUSTED attribution field on the board row."""
    import tools.kanban_tools as kt

    out = kt._stamp_worker_session_metadata(as_non_owner, {"k": "v"})
    assert out == {"k": "v"}
    assert "worker_session_id" not in (out or {})


def test_orchestrator_only_refusal_does_not_fire_for_a_non_owner(as_non_owner):
    """A nested process is not a worker, so it must not get the WORKER error.

    ``_require_orchestrator_tool`` refuses with "dispatcher-spawned workers
    must use kanban_complete…" — advice that is actively wrong for a process
    that has no card of its own. Its Kanban surface is decided by the profile
    toolset, exactly like any other non-worker process.
    """
    import tools.kanban_tools as kt

    assert kt._require_orchestrator_tool("kanban_list") is None


def test_orchestrator_only_refusal_still_fires_for_the_real_worker(as_owner):
    """Positive control: workers stay off the board-routing tools."""
    import tools.kanban_tools as kt

    err = kt._require_orchestrator_tool("kanban_list")
    assert err is not None and "orchestrator-only" in err


# ---------------------------------------------------------------------------
# 2. Ambient reads in the CLI layer (`hermes kanban ...`)
# ---------------------------------------------------------------------------

def test_cli_run_id_is_not_inherited_by_a_non_owner(as_non_owner):
    """The CLI path had NO ownership gate at all before this change.

    ``hermes kanban complete/heartbeat/request-review`` all stamp
    ``expected_run_id=_worker_run_id_for(tid)``. A nested shell running the CLI
    inherits the pins and satisfies the guard — the same corruption as the tool
    path, through a different door.
    """
    import hermes_cli.kanban as kcli

    assert kcli._worker_run_id_for(as_non_owner) is None


def test_cli_run_id_still_resolves_for_the_real_worker(as_owner):
    import hermes_cli.kanban as kcli

    assert kcli._worker_run_id_for(as_owner) == 4242


def test_comment_provenance_is_not_attributed_to_the_owners_run(as_non_owner):
    """A comment's ``run_id`` is trusted provenance, never caller-supplied.

    ``add_comment`` validates the pair at its write choke point, so an
    inherited run id would be durably recorded as "run 4242 said this".
    """
    from hermes_cli.kanban_identity import resolve_comment_provenance

    run_id, session_ref = resolve_comment_provenance(as_non_owner)
    assert run_id is None
    # The session fingerprint is always safe and must still be produced.
    assert session_ref is None or isinstance(session_ref, str)


def test_comment_provenance_still_attributed_for_the_real_worker(as_owner):
    from hermes_cli.kanban_identity import resolve_comment_provenance

    run_id, _ = resolve_comment_provenance(as_owner)
    assert run_id == 4242


def test_explicit_env_snapshot_keeps_its_semantics(as_non_owner):
    """An explicit ``env=`` mapping is a caller-supplied snapshot.

    The dashboard resolves provenance for a task it is not running, passing the
    env it means. Gating that on THIS process's authority would break it, so
    the ownership check applies only to the live-process read.
    """
    from hermes_cli.kanban_identity import resolve_comment_provenance

    run_id, _ = resolve_comment_provenance(
        "t_other",
        env={"HERMES_KANBAN_TASK": "t_other", "HERMES_KANBAN_RUN_ID": "77"},
    )
    assert run_id == 77


# ---------------------------------------------------------------------------
# 3. The send_message gate
# ---------------------------------------------------------------------------

def test_send_message_is_not_force_enabled_for_a_non_owner(
    as_non_owner, monkeypatch
):
    """``HERMES_KANBAN_TASK`` alone force-enabled ``send_message``.

    The var is inherited, so any nested subprocess got an unconditional
    messaging tool regardless of profile/gateway state. The worker keeps it;
    an inheriting process falls back to the ordinary platform/gateway rules.
    """
    import tools.send_message_tool as smt

    monkeypatch.setattr(
        smt, "_is_dispatcher_owned_worker_process", lambda: False
    )
    # Must not short-circuit True on the inherited task var alone; whatever it
    # returns now comes from the normal gateway/platform path.
    assert smt._check_send_message() is not True


def test_send_message_still_force_enabled_for_the_real_worker(as_owner):
    """Positive control: workers must keep their notify channel."""
    import tools.send_message_tool as smt

    assert smt._check_send_message() is True


# ---------------------------------------------------------------------------
# 4. The fork-boundary seal (the fail-open leak)
# ---------------------------------------------------------------------------

def test_child_env_carrying_the_task_id_is_never_left_unowned(as_owner):
    """A child env with the task id but NO owner marker fails OPEN.

    ``owns_kanban_worker_authority`` returns True on an absent marker by design
    (hand-driven workers, pre-stamp dispatchers). So an env that readmits
    ``HERMES_KANBAN_TASK`` while dropping the marker hands the child full
    authority. Every builder in ``tools.environments.local`` funnels through
    ``_scrub_delegated_child_kanban_env``, which must seal the gap.
    """
    from agent.delegation_context import KANBAN_OWNER_PID_ENV
    from tools.environments.local import _scrub_delegated_child_kanban_env

    sealed = _scrub_delegated_child_kanban_env(
        {"HERMES_KANBAN_TASK": "t_victim", "HERMES_KANBAN_RUN_ID": "4242"}
    )
    assert sealed.get(KANBAN_OWNER_PID_ENV), (
        "a child env carrying HERMES_KANBAN_TASK with no owner marker fails "
        "OPEN in owns_kanban_worker_authority — the child resolves itself as "
        "the dispatcher-spawned worker"
    )
    # And the pid it names must not be claimable by the child: it is ours.
    assert sealed[KANBAN_OWNER_PID_ENV] == str(os.getpid())


def test_seal_does_not_manufacture_a_grant_without_a_task(as_owner):
    """No task id in the child env ⇒ nothing to seal; do not invent a marker."""
    from agent.delegation_context import KANBAN_OWNER_PID_ENV
    from tools.environments.local import _scrub_delegated_child_kanban_env

    out = _scrub_delegated_child_kanban_env({"PATH": "/usr/bin"})
    assert KANBAN_OWNER_PID_ENV not in out


def test_seal_preserves_an_existing_owner_marker(as_owner):
    """An env that already names an owner is left alone (idempotent)."""
    from agent.delegation_context import KANBAN_OWNER_PID_ENV
    from tools.environments.local import _scrub_delegated_child_kanban_env

    out = _scrub_delegated_child_kanban_env(
        {"HERMES_KANBAN_TASK": "t_victim", KANBAN_OWNER_PID_ENV: "999999"}
    )
    assert out[KANBAN_OWNER_PID_ENV] == "999999"


def test_code_execution_sandbox_env_is_sealed_under_passthrough(as_owner):
    """``env_passthrough`` can readmit the task var but not the owner marker.

    The marker has no passthrough entry of its own, so an operator who opts
    ``HERMES_KANBAN_TASK`` through produces precisely the fail-open shape.
    """
    from agent.delegation_context import KANBAN_OWNER_PID_ENV
    from tools.code_execution_tool import _scrub_child_env

    scrubbed = _scrub_child_env(
        {"HERMES_KANBAN_TASK": "t_victim", "HERMES_KANBAN_RUN_ID": "4242"},
        is_passthrough=lambda k: k == "HERMES_KANBAN_TASK",
        is_windows=False,
    )
    assert scrubbed.get("HERMES_KANBAN_TASK") == "t_victim"
    assert scrubbed.get(KANBAN_OWNER_PID_ENV) == str(os.getpid()), (
        "passthrough readmitted the task id into the sandbox without an owner "
        "marker — the sandboxed child resolves as the dispatcher's worker"
    )
