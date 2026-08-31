"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _no_real_launchd_fleet_restart(request, monkeypatch):
    """Keep ``cmd_update`` tests away from the host's real launchd fleet.

    ``_cmd_update_impl``'s gateway-restart phase branches on the REAL host
    platform: on macOS it walks every installed ``ai.hermes.gateway*``
    LaunchAgent via ``launchctl`` — draining/kickstarting live gateways on a
    developer Mac and then failing the update (SystemExit 1,
    ``gateway_fleet_restart_incomplete``) when supervision verification
    can't line up with the test's sandboxed HERMES_HOME. Upstream's update
    tests are authored against Linux CI, where ``is_macos()`` is False and
    the phase is a no-op, so they never stub it.

    Neutralize the launchd phase by default; the files that test it
    directly (test_update_launchd_*.py) are opted out by filename so they
    keep exercising the real functions.
    """
    if "launchd" in request.node.fspath.basename:
        return
    try:
        from hermes_cli import update_cmd as _update_cmd
    except Exception:
        return
    monkeypatch.setattr(
        _update_cmd,
        "_restart_macos_launchd_gateways",
        lambda *_a, **_k: None,
        raising=False,
    )
    monkeypatch.setattr(
        _update_cmd,
        "_restart_launchd_gateway_after_update",
        lambda *_a, **_k: ([], []),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _no_stale_module_purge(request, monkeypatch):
    """Default ``_purge_stale_hermes_modules`` to a no-op in cmd_update tests.

    The real purge deletes ~70 live hermes modules from ``sys.modules`` so a
    post-update process resolves fresh code. Inside pytest that orphans every
    module object other tests hold references to (the PR #538 bug class); the
    fork's sys_modules leak gate rightly fails any test that lets the purge
    run un-restored. Files that test the purge itself opt out by name.
    """
    if "purge" in request.node.fspath.basename:
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main,
        "_purge_stale_hermes_modules",
        lambda *_a, **_k: None,
        raising=False,
    )
