"""``owns_kanban_worker_authority`` requires a Kanban grant to exist at all.

Regression for t_39f5f042: with no ``HERMES_KANBAN_*`` env the predicate failed
OPEN (no owner-pid stamp -> True), so every script/cron ``hermes kanban create``
was classed as a dispatcher worker and skipped the boomerang home default (#72).
The fail-open for a present task var with no owner stamp is kept.
"""
from __future__ import annotations

import os

import pytest

from agent import delegation_context as dc


@pytest.fixture
def no_kanban_env(monkeypatch):
    for key in (*dc.KANBAN_ENV_KEYS, "HERMES_KANBAN_TASK", dc.DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("task_value", [None, "", "   "])
def test_no_task_grant_is_not_a_worker(no_kanban_env, monkeypatch, task_value):
    if task_value is not None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_value)
    assert dc.owns_kanban_worker_authority() is False
    assert dc.is_dispatcher_owned_worker_context() is False


def test_task_var_without_owner_stamp_still_fails_open(no_kanban_env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_handdriven")
    assert dc.owns_kanban_worker_authority() is True
    assert dc.is_dispatcher_owned_worker_context() is True


def test_owner_stamp_binds_to_exactly_this_pid(no_kanban_env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    monkeypatch.setenv(dc.KANBAN_OWNER_PID_ENV, str(os.getpid()))
    assert dc.owns_kanban_worker_authority() is True
    monkeypatch.setenv(dc.KANBAN_OWNER_PID_ENV, str(os.getpid() + 1))
    assert dc.owns_kanban_worker_authority() is False
