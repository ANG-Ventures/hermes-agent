"""Fixtures shared across hermes_cli tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_kanban_process_registry(monkeypatch):
    """Retained fake Popen handles must not leak into the next test's tick."""
    from hermes_cli import kanban_db
    monkeypatch.setattr(kanban_db, "_worker_processes", {})
    # One dict object shared by the facade (writer: _record_worker_returncode) and
    # kanban_db_dispatch (reader: _classify_worker_exit). Rebinding the facade name
    # would split them, so clear the shared object instead.
    monkeypatch.setattr(kanban_db, "_worker_identities", {})
    kanban_db._recent_worker_exits.clear()
    yield
    kanban_db._recent_worker_exits.clear()


@pytest.fixture(autouse=True)
def _no_dangling_staged_refs(request, monkeypatch):
    """Post-condition on every request_review / complete_task / block_task:
    no committed row names a staged attachment copy that is gone (t_f577ddd5).
    Checked whether the call returned or raised. Tests that delete a staged
    copy on purpose opt out with ``@pytest.mark.allow_dangling_staged_refs``."""
    if request.node.get_closest_marker("allow_dangling_staged_refs"):
        return
    from hermes_cli import kanban_db
    from tests.hermes_cli._kanban_ref_integrity import assert_no_dangling_staged_refs

    def _checked(name):
        real = getattr(kanban_db, name)

        def wrapper(conn, task_id, *args, **kwargs):
            try:
                return real(conn, task_id, *args, **kwargs)
            finally:
                assert_no_dangling_staged_refs(conn, task_id, after=name)

        return wrapper

    for name in ("request_review", "complete_task", "block_task"):
        monkeypatch.setattr(kanban_db, name, _checked(name))


@pytest.fixture(autouse=True)
def _quiet_host_loadavg(monkeypatch):
    """Pin host load to idle: `kanban dispatch` consults the real load gate
    (t_689b81b7), so an unpinned test would pause on a busy CI runner.
    Gate tests override this with their own sample_loadavg patch."""
    import os
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)


@pytest.fixture(autouse=True)
def _hermetic_posix_descendants(request, monkeypatch):
    """Stub the dashboard kill's descendant snapshot to ``{}`` (root-only kill).

    ``_posix_descendants`` runs a real ``ps -A`` and walks the host's process tree from the
    PIDs it is handed. Tests hand it synthetic PIDs (7001, 4242, 12345...); when one collides
    with a live process on the CI runner, its real children are swept into the kill and the
    test's ``os.kill`` fake records them (macOS lane, main 95bfafc3ec: ``[7001, 7012, 7012]``).
    Tests that spawn real process trees opt out with ``@pytest.mark.real_posix_descendants``.
    """
    if request.node.get_closest_marker("real_posix_descendants"):
        return
    from hermes_cli import dashboard_procs
    monkeypatch.setattr(dashboard_procs, "_posix_descendants", lambda roots: {})


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
    if request.node.get_closest_marker("real_concurrent_gate"):
        # Same opt-out as the concurrent-instances stub above: a test asserting that the frozen
        # updater surface resolves to the REAL objects must see the real purge too.
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


@pytest.fixture(autouse=True)
def _source_channels_resolve_locally(request, monkeypatch):
    """Every unflagged ``hermes update`` resolves its channel through R2; tests must
    not reach the network for that. Default every channel to a ``source-branch``
    record delivering ``origin/<name>`` through the documented seam. Channel tests
    that model records themselves re-patch ``_resolve_channel`` after this runs;
    the marker opts out entirely for tests of the reader's own network path.
    """
    if request.node.get_closest_marker("real_release_channels"):
        return
    from hermes_cli import source_releases
    from hermes_cli.release_channels import ChannelResolution

    def resolve(name, repository):
        record = {"schema": 1, "name": name, "repository": repository, "policy": "source-branch",
                  "state": "active", "identity": None, "nextSequence": 1, "head": None,
                  "delivery": {"kind": "source-branch", "branch": name}}
        return ChannelResolution(record, record, None)

    monkeypatch.setattr(source_releases, "_resolve_channel", resolve)


@pytest.fixture(autouse=True)
def _discharge_host_update_obligation():
    """Start and end every ``hermes_cli`` test with NO host update-restart obligation.

    The record is host-scoped on purpose (one multiplexer per host), so it lives in the
    per-OS-USER host state dir — not in the per-test ``HERMES_HOME``. The root conftest pins
    that dir per test only when the caller supplied no ``HERMES_GATEWAY_LOCK_DIR`` (#118097
    keeps the documented override working), so with one set every test in a file shares it and
    a test that arms the obligation makes the next one read a restart it never owed. Clearing
    the record — rather than re-pinning the dir — leaves that override rule untouched.
    """

    def _clear() -> None:
        try:
            from hermes_cli.update_host_obligation import clear_host_obligation

            clear_host_obligation()
        except Exception:
            # Import/env failure here must never error an unrelated test.
            pass

    _clear()
    yield
    _clear()


@pytest.fixture
def isolated_source_completion(monkeypatch):
    """Unit-test the completion tail in-process; real transport is tested separately."""
    from hermes_cli import update_cmd, update_completion

    monkeypatch.setattr("hermes_cli.source_build.build_update_products", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.venv_sync.publish_launchers", lambda *a: None)

    def complete(request):
        update_completion._complete_selected(request)
        return {"exit_code": 0, "receipt": update_completion._read_terminal_receipt(request),
                "windows_resume": request["windows_resume"]}

    monkeypatch.setattr(update_cmd, "run_completion", complete)


@pytest.fixture(autouse=True)
def _reset_prompt_toolkit_output_cache():
    """Clear prompt_toolkit's cached AppSession output around each CLI test.

    See the module docstring for the capsys/prompt_toolkit interaction this
    guards against.
    """

    def _clear() -> None:
        try:
            from prompt_toolkit.application.current import get_app_session

            get_app_session()._output = None
        except Exception:
            # prompt_toolkit not importable / internal shape changed — the
            # tests that rely on this simply keep their prior behavior.
            pass

    _clear()
    yield
    _clear()

@pytest.fixture
def probe_root(tmp_path):
    """A fixture checkout the installation launcher can boot from.

    ``runtime_command`` prepends the checkout root and runs ``import hermes_bootstrap``
    before the probe body, exactly as production does. Tests that point the import
    guard at a scratch tree need that module present, or the probe dies before its
    health marker — a developer venv whose editable ``.pth`` shadows the root hides
    the dependency, CI's clean environment does not.
    """
    (tmp_path / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    return tmp_path
