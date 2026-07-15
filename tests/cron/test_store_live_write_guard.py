"""The cron store must refuse a TEST-context write to the LIVE production store.

Regression for the 2026-07-15 recurrence: a test/e2e harness (a real-agent
blackbox session or a kanban worker booted from a worktree that shares the live
``~/.hermes``) that runs the cron test suite OUTSIDE pytest's hermetic conftest
imports ``cron.jobs`` with ``HERMES_HOME`` pointing at the real home, then calls
``create_job`` with fixture data — leaking ``brief``/``claim job``/``paused job``
jobs into the LIVE ``cron/jobs.json`` and paging cron-health.

The import-freeze fix (PR #348) alone did not stop this: the harness runs with
the live home, so a live-resolving store correctly points AT the live store.
The durable defense is a write-chokepoint guard that fails LOUD when a pytest
context tries to write the production store without an explicit override.
"""
import os

import pytest

from cron import jobs as jobs_mod


def test_write_to_live_prod_store_under_pytest_is_refused(monkeypatch):
    """PYTEST_CURRENT_TEST + resolved store == the real prod store => RuntimeError."""
    from hermes_constants import _get_platform_default_hermes_home
    # Point HERMES_HOME at the real platform-native home (what a bypassed-conftest
    # harness effectively does) and ensure a pytest context is signalled.
    prod_home = _get_platform_default_hermes_home().resolve()
    monkeypatch.setenv("HERMES_HOME", str(prod_home))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_guard (call)")

    with pytest.raises(RuntimeError, match="LIVE production home"):
        jobs_mod.save_jobs([{"id": "leak", "prompt": "brief", "name": "brief"}])


def test_isolated_tempdir_write_under_pytest_is_allowed(tmp_path, monkeypatch):
    """A correctly-isolated test (store in a tempdir) writes fine under pytest."""
    (tmp_path / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_guard (call)")

    jobs_mod.save_jobs([{"id": "ok", "prompt": "x", "name": "t"}])
    assert (tmp_path / "cron" / "jobs.json").exists()


def test_explicit_use_cron_store_override_bypasses_the_guard(tmp_path, monkeypatch):
    """An explicit use_cron_store() scope is a deliberate target — never guarded,
    even if it happened to point at the prod path."""
    (tmp_path / "cron").mkdir(parents=True)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_guard (call)")
    with jobs_mod.use_cron_store(str(tmp_path)):
        jobs_mod.save_jobs([{"id": "ok", "prompt": "x", "name": "t"}])
    assert (tmp_path / "cron" / "jobs.json").exists()


def test_production_write_no_pytest_context_is_allowed(monkeypatch):
    """The guard must NOT fire for a real production write (no PYTEST_CURRENT_TEST)."""
    from hermes_constants import _get_platform_default_hermes_home
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    prod_jobs_file = _get_platform_default_hermes_home().resolve() / "cron" / "jobs.json"
    # Should not raise the guard's RuntimeError (we don't actually write — just
    # prove the guard is a no-op without the pytest signal).
    jobs_mod._guard_against_test_write_to_live_store(prod_jobs_file)
