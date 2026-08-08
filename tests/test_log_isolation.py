"""Test-log isolation guard.

Two independent guards, merged 2026-08-07 (fork + upstream both authored one):


Regression test for the live-log pollution that tripped the compaction-stats-watch
cron with false pages: when tests exercise modules that call
``hermes_logging.setup_logging()`` (directly or via building a real AIAgent), the
root logger gets a ``RotatingFileHandler`` pointing at the REAL ``~/.hermes/logs/
agent.log`` — so WARNING records emitted by test code (e.g.
``COMPACTION_STATS_RECONCILE_FAILED``) land in the production log file and a
watcher pages on them.

The conftest autouse ``_isolate_log_handlers`` fixture must ensure the root logger
has NO file handler writing outside the per-test tempdir.

-- upstream guard --


`hermes_cli/main.py` calls `setup_logging()` at module scope, which resolves
`get_hermes_home()` and attaches rotating file handlers to the ROOT logger.
Importing it - which many test modules do, directly or transitively - wires
the whole pytest session's logging to `<HERMES_HOME>/logs/agent.log`.

If HERMES_HOME is not already sandboxed at that moment, that is the
operator's real log. Measured on a live install, 126 warnings in a personal
`agent.log` came from test runs rather than the running gateway: phantom
`FakeTree` Discord failures and `rejected invalid API key` entries from
`test_api_server_runs.py`. Noise like that makes genuine warnings hard to
find precisely when someone is debugging.

The per-test env fixture cannot close this: fixtures run after collection has
imported the test modules, and by then the handler holds an absolute path.
`tests/conftest.py` sets HERMES_HOME at module scope for that reason - this
guards the property so a refactor cannot quietly undo it.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest


def _root_file_handler_paths():
    paths = []
    for h in logging.getLogger().handlers:
        base = getattr(h, "baseFilename", None)
        if base:
            paths.append(base)
    return paths


def test_no_root_file_handler_outside_tmp(tmp_path):
    """No root-logger file handler may point at a path outside the test sandbox.
    The real ~/.hermes/logs/agent.log is the canonical leak target."""
    real_logs = str(Path.home() / ".hermes" / "logs")
    for p in _root_file_handler_paths():
        assert real_logs not in p, (
            f"root logger has a file handler at {p} (real ~/.hermes/logs) — "
            "test logging is leaking into the production log"
        )


def test_setup_logging_during_test_does_not_attach_real_handler(tmp_path, monkeypatch):
    """Even if a module calls setup_logging() mid-test, the guard keeps the real
    log path off the root logger (records can't reach ~/.hermes/logs)."""
    import hermes_logging

    # HERMES_HOME is already the per-test tempdir (via _hermetic_environment),
    # so setup_logging writes into tmp — assert it never targets the real home.
    hermes_logging._logging_initialized = False  # force re-attach
    hermes_logging.setup_logging()

    real_logs = str(Path.home() / ".hermes" / "logs")
    for p in _root_file_handler_paths():
        assert real_logs not in p, f"setup_logging attached a real-home handler: {p}"


def test_warning_during_test_does_not_write_real_agent_log(tmp_path):
    """A WARNING logged by 'agent.conversation_compression' during a test must not
    append to the real ~/.hermes/logs/agent.log (the exact watcher-tripping leak)."""
    real_agent_log = Path.home() / ".hermes" / "logs" / "agent.log"
    before = real_agent_log.stat().st_size if real_agent_log.exists() else None

    logging.getLogger("agent.conversation_compression").warning(
        "COMPACTION_STATS_RECONCILE_FAILED in-turn TEST-ISOLATION-PROBE should-not-leak"
    )

    if before is not None and real_agent_log.exists():
        after = real_agent_log.stat().st_size
        # the probe string must not be in the real log
        tail = real_agent_log.read_bytes()[-4096:]
        assert b"TEST-ISOLATION-PROBE" not in tail, (
            "a test WARNING leaked into the real ~/.hermes/logs/agent.log"
        )


def test_sibling_sandbox_dir_handler_is_stripped(tmp_path):
    """Greptile #114: a handler in a SIBLING sandbox dir (path that shares a string
    prefix but is not actually inside) must be stripped, not kept."""
    import sys as _sys
    from logging.handlers import RotatingFileHandler as _RFH

    _conftest = _sys.modules.get("conftest") or _sys.modules.get("tests.conftest")
    assert _conftest is not None

    # sandbox = .../tX ; sibling = .../tX1 (str-prefix match, but NOT inside)
    sandbox = tmp_path / "t0"
    sandbox.mkdir()
    sibling = tmp_path / "t01"
    (sibling / "logs").mkdir(parents=True)
    sib_log = sibling / "logs" / "agent.log"

    root = logging.getLogger()
    h = _RFH(str(sib_log), maxBytes=1024, backupCount=0, delay=True)
    root.addHandler(h)
    try:
        _conftest._strip_nonsandbox_file_handlers(str(sandbox))
        assert h not in root.handlers, "sibling-dir handler wrongly kept (str-prefix bug)"
    finally:
        if h in root.handlers:
            root.removeHandler(h)
        h.close()


def test_in_sandbox_handler_is_kept(tmp_path):
    """A handler genuinely INSIDE the sandbox is kept (the guard must not nuke
    legitimate per-test log handlers)."""
    import sys as _sys
    from logging.handlers import RotatingFileHandler as _RFH

    _conftest = _sys.modules.get("conftest") or _sys.modules.get("tests.conftest")
    assert _conftest is not None

    (tmp_path / "logs").mkdir()
    in_log = tmp_path / "logs" / "agent.log"
    root = logging.getLogger()
    h = _RFH(str(in_log), maxBytes=1024, backupCount=0, delay=True)
    root.addHandler(h)
    try:
        _conftest._strip_nonsandbox_file_handlers(str(tmp_path))
        assert h in root.handlers, "in-sandbox handler wrongly stripped"
    finally:
        if h in root.handlers:
            root.removeHandler(h)
        h.close()


def test_guard_strips_a_real_home_handler_attached_mid_session():
    """RED-proof of the guard: even if something attaches a RotatingFileHandler at
    the REAL ~/.hermes/logs (the intermittent import-order leak), the autouse
    _isolate_log_handlers fixture must have removed it before this test body runs,
    so a WARNING here cannot reach the real file.

    We simulate the leaked state by attaching a handler at the real path, then
    assert the guard (which runs at the START of every test via autouse) would
    catch it — by re-invoking the strip and confirming it removes our handler.
    """
    from logging.handlers import RotatingFileHandler as _RFH

    real_log = Path.home() / ".hermes" / "logs" / "agent.log"
    root = logging.getLogger()
    leaked = None
    try:
        # only attach if the real dir exists (don't create it in CI sandboxes)
        if real_log.parent.exists():
            leaked = _RFH(str(real_log), maxBytes=1024, backupCount=0, delay=True)
            root.addHandler(leaked)
            # the guard helper must classify + strip it. conftest is loaded by
            # pytest as a plugin; reach its module object via sys.modules.
            import sys as _sys
            _conftest = _sys.modules.get("conftest") or _sys.modules.get("tests.conftest")
            assert _conftest is not None, "conftest module not importable"
            _conftest._strip_nonsandbox_file_handlers()
            assert leaked not in root.handlers, "guard failed to strip the real-home handler"
    finally:
        if leaked is not None and leaked in root.handlers:
            root.removeHandler(leaked)
        if leaked is not None:
            leaked.close()



def _real_hermes_home() -> Path:
    """Where the operator's logs live, ignoring any test sandboxing."""
    return Path.home() / ".hermes"


def _all_file_destinations() -> list[str]:
    """Every file path the root logger can reach, including via a QueueHandler.

    Logging is routed through a queue, so the file handlers hang off the
    listener rather than the root logger - checking `root.handlers` alone
    reports nothing and looks falsely clean.
    """
    seen: list[str] = []

    def collect(handlers) -> None:
        for handler in handlers or ():
            path = getattr(handler, "baseFilename", None)
            if path:
                seen.append(str(path))
            listener = getattr(handler, "listener", None)
            if listener is not None:
                collect(getattr(listener, "handlers", ()))

    collect(logging.getLogger().handlers)

    try:
        import hermes_logging

        listener = getattr(hermes_logging, "_queue_listener", None)
        if listener is not None:
            collect(getattr(listener, "handlers", ()))
    except Exception:
        pass

    return seen


class TestLogIsolation:
    def test_hermes_home_is_sandboxed_before_imports(self):
        # Deliberately NOT os.environ: by test time the per-test `_isolate_env`
        # fixture has sandboxed HERMES_HOME, so reading it here would pass even
        # with the conftest block deleted. Assert the value captured at conftest
        # import, which is the moment that actually matters.
        from tests.conftest import HERMES_HOME_AT_CONFTEST_IMPORT as home

        assert home, "conftest must set HERMES_HOME before test modules import"
        assert Path(home).resolve() != _real_hermes_home().resolve(), (
            f"HERMES_HOME pointed at the operator's real home ({home}) when "
            "conftest loaded; import-time setup_logging() writes to their agent.log"
        )

    def test_importing_the_cli_does_not_target_the_real_logs(self):
        pytest.importorskip("hermes_cli.main")

        real_logs = str(_real_hermes_home() / "logs")
        offenders = [p for p in _all_file_destinations() if p.startswith(real_logs)]

        assert offenders == [], (
            "the test session is writing into the operator's real Hermes logs:\n  "
            + "\n  ".join(offenders)
        )
