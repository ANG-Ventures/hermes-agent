"""Shared fixtures for the hermes-agent test suite.

Hermetic-test invariants enforced here (see AGENTS.md for rationale):

1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS, etc.)
   are unset before every test. Local developer keys cannot leak in.
2. **Isolated Hermes homes.** HERMES_HOME and the platform-default root
   resolve inside a per-test tempdir. Profile/root resolution can inspect
   both without probing production state. HOME and Path.home() stay intact
   for subprocesses and non-Hermes paths. Explicit test overrides still win.
3. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
4. **No HERMES_SESSION_* inheritance** — the agent's current gateway
   session must not leak into tests.

These invariants make the local test run match CI closely. Gaps that
remain (CPU count, worker count) are addressed by the canonical
test runner at ``scripts/run_tests.sh``.
"""

import asyncio
import atexit
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── sys.modules leak gate ───────────────────────────────────────────────────
# Fails a test that purges sys.modules of watched modules (run_agent, agent.*,
# tools.*, hermes_*, gateway.*, plugins.*) without restoring them. That bug class
# cost ~46 suite failures from ONE fixture (PR #538) and ~20 more from a second:
# later importers get a brand-new module object, so a subsequent test patches an
# orphaned copy while the code under test imports a different one.
# Opt out with @pytest.mark.allow_sys_modules_purge (and say why).
pytest_plugins = ["tests.sys_modules_leak_gate"]


def _strip_nonsandbox_file_handlers(sandbox_prefix=None):
    """Remove any logging file handler (root + named loggers) whose target file
    lives OUTSIDE the per-test sandbox — chiefly the real ``~/.hermes/logs/
    agent.log`` handler that ``hermes_logging.setup_logging()`` may have attached
    to the root logger before HERMES_HOME was redirected.

    Without this, a WARNING emitted by code-under-test (e.g.
    ``COMPACTION_STATS_RECONCILE_FAILED``) appends to the PRODUCTION log and trips
    the compaction-stats watcher cron with false pages. Also flips
    ``hermes_logging._logging_initialized`` back to False so a later in-test
    ``setup_logging()`` re-attaches against the (now sandboxed) HERMES_HOME.
    """
    import logging as _logging

    sandbox_root = Path(sandbox_prefix).resolve() if sandbox_prefix else None
    loggers = [_logging.getLogger()] + [
        _logging.getLogger(name) for name in list(_logging.root.manager.loggerDict)
        if isinstance(_logging.getLogger(name), _logging.Logger)
    ]
    for lg in loggers:
        for h in list(getattr(lg, "handlers", [])):
            base = getattr(h, "baseFilename", None)
            if not base:
                continue
            # keep handlers that write INSIDE the test sandbox; strip the rest.
            # Use real path-containment (relative_to), NOT str.startswith — a
            # string prefix would treat sibling dirs (/tmp/x/t0 vs /tmp/x/t01) as
            # inside (Greptile #114).
            if sandbox_root is not None:
                try:
                    Path(base).resolve().relative_to(sandbox_root)
                    continue  # inside the sandbox → keep
                except ValueError:
                    pass  # outside → fall through to strip
            try:
                lg.removeHandler(h)
                h.close()
            except Exception:
                pass
    # force re-init so a later setup_logging() re-attaches against sandboxed home
    try:
        import hermes_logging
        hermes_logging._logging_initialized = False
    except Exception:
        pass

# ── Sandbox HERMES_HOME before ANY test module is imported ──────────────────
# `hermes_cli/main.py` calls `setup_logging()` at MODULE level, which resolves
# `get_hermes_home()` and attaches rotating file handlers to the ROOT logger.
# So merely importing it - which many test modules do, directly or
# transitively - points the whole pytest session's logging at the operator's
# real `~/.hermes/logs/agent.log` and `errors.log`.
#
# The `_isolate_env` fixture below also sandboxes HERMES_HOME, but fixtures run
# AFTER collection imports test modules, by which point the handler already
# holds an absolute path to the real log. Measured on a live install: 126
# warnings in the operator's agent.log came from test runs, not the gateway -
# enough noise to make genuine warnings hard to find.
#
# conftest is imported before any test module, so setting it here closes that
# window. The per-test fixture still applies for everything after import.
#
# ── Keep the pytest temp root OUTSIDE the live Hermes root (t_f64dfdb6) ────
# get_default_hermes_root() maps ANY HERMES_HOME that resolves under the
# native ~/.hermes back to ~/.hermes itself (that is how a profile home finds
# its root). Kanban workers can run with TMPDIR=~/.hermes/kanban/workspaces/
# <task>/tmp, which puts tmp_path - and so the per-test sandbox home - under the
# live root: every profile write (create_profile, POST /api/profiles, SOUL
# writes) then lands in the REAL ~/.hermes/profiles/. That is how
# profiles/demo, worker, builder-auth and work appeared 2026-09-23 04:03-04:05.
# Relocate the temp root before pytest or the session sandbox below derive any
# path from it; the per-test guard in _hermetic_environment refuses whatever
# still slips through (--basetemp, PYTEST_DEBUG_TEMPROOT).
def _real_hermes_root() -> Path:
    """The platform-native Hermes root (mirrors hermes_constants's default)."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return (base / "hermes").resolve()
    return (Path.home() / ".hermes").resolve()


def _is_under_real_hermes_root(path) -> bool:
    """True when *path* is the live Hermes root or anywhere beneath it."""
    try:
        Path(path).resolve().relative_to(_real_hermes_root())
        return True
    except ValueError:
        return False
    except Exception:
        return True  # unresolvable: treat as unsafe


def _outside_tmp_base() -> str:
    for candidate in ("/tmp", "/var/tmp"):
        if os.path.isdir(candidate) and not _is_under_real_hermes_root(candidate):
            return candidate
    return str(Path.home())


if _is_under_real_hermes_root(tempfile.gettempdir()):
    _SAFE_TMP_ROOT = tempfile.mkdtemp(prefix="hermes-pytest-tmp-", dir=_outside_tmp_base())
    for _tmp_var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[_tmp_var] = _SAFE_TMP_ROOT
    tempfile.tempdir = None  # drop gettempdir()'s cache so it re-reads TMPDIR
    atexit.register(shutil.rmtree, _SAFE_TMP_ROOT, True)

# ORDER MATTERS: the kanban write guard's deny-list (further down) must know
# the REAL Hermes root — capture it BEFORE the sandbox rewires HERMES_HOME,
# otherwise the deny-list would point at the throwaway tempdir and the guard
# would silently stop protecting the operator's actual ~/.hermes (#69385).
_PRE_SANDBOX_KANBAN_OVERRIDE = os.environ.get("HERMES_KANBAN_HOME", "").strip()
_PRE_SANDBOX_HERMES_HOME = os.environ.get("HERMES_HOME", "")

# Capture before any test fixture can override Path.home()/LOCALAPPDATA.
from hermes_constants import _get_platform_default_hermes_home

_NATIVE_HERMES_PARENT = _get_platform_default_hermes_home().parent


def _hermes_home_points_at_production(value: str) -> bool:
    """True when a pre-set HERMES_HOME resolves to the real production root.

    Gateway-launched shells (and developer shells that ``export
    HERMES_HOME=~/.hermes``) hand pytest the PRODUCTION home. Historically
    the session sandbox below honored any pre-set value, so collection-time
    imports (logging handlers, ``hermes_state.DEFAULT_DB_PATH``) froze paths
    inside the real ``~/.hermes`` — the escape vector that landed pytest
    fixture rows (chat-1 / wx-chat sessions, /tmp/pytest-of-* routing
    scopes) in the live state.db and flipped its journal mode under the
    WAL-mode gateway writer. Only a genuinely custom (non-production)
    HERMES_HOME is honored now.
    """
    if not value:
        return True
    try:
        # The platform-default root, not a hardcoded ``~/.hermes``: Windows installs live under
        # ``%LOCALAPPDATA%\hermes``, and a dev shell exporting that path used to be honored as
        # "custom", pinning import-time paths (``tui_gateway.server._hermes_home``) to the live
        # install so the state.db guard tripped on every store-touching test (#112692).
        from hermes_state_guard import _real_platform_state_root

        resolved = Path(value).expanduser().resolve()
        real_root = _real_platform_state_root() or (Path.home() / ".hermes").resolve()
    except Exception:
        return True
    if resolved == real_root:
        return True
    # Profile home directly under the production root: <root>/profiles/<name>
    return resolved.parent.name == "profiles" and resolved.parent.parent == real_root


# ``import hermes_bootstrap`` (transitively: any entry-point module) runs
# ``export_scratch_tmp_env()``, which points TMPDIR/TMP/TEMP at
# ``<HERMES_HOME>/cache/scratch`` unless a temp var is already set — and a
# Hermes-launched shell (agent terminal, ``hermes`` child) arrives with that
# redirect already applied, tagged by HERMES_SCRATCH_DIR. Either way the tmp
# root ends up INSIDE a guarded real home (the operator's, or a custom one
# honored below), so the session sandbox, pytest's basetemp and every
# ``tempfile`` default in the code under test trip the real-home guard. Strip
# Hermes' own export (the marker tells it apart from a user-set var), and
# relocate even user-set temp directories inside a guarded home. Pin the
# system default so the import-time hook stays a no-op. The parallel runner
# exports its own disk-backed TMPDIR anyway.
from hermes_constants import SCRATCH_DIR_MARKER_ENV, SCRATCH_TMP_ENV_VARS

_HERMES_EXPORTED_TMP = os.environ.get(SCRATCH_DIR_MARKER_ENV, "")
if _HERMES_EXPORTED_TMP:
    for _key in SCRATCH_TMP_ENV_VARS:
        if os.environ.get(_key, "").strip() == _HERMES_EXPORTED_TMP:
            del os.environ[_key]
    del os.environ[SCRATCH_DIR_MARKER_ENV]

from hermes_state_guard import _real_platform_state_root

_real_test_root = _real_platform_state_root() or (Path.home() / ".hermes").resolve()
_guarded_tmp_roots = [_real_test_root]
_custom_test_home = os.environ.get("HERMES_HOME")
if _custom_test_home:
    _guarded_tmp_roots.append(Path(_custom_test_home).expanduser().resolve())
for _key in SCRATCH_TMP_ENV_VARS:
    _value = os.environ.get(_key)
    if _value:
        _path = Path(_value).expanduser().resolve()
        if any(_path.is_relative_to(_root) for _root in _guarded_tmp_roots):
            del os.environ[_key]
tempfile.tempdir = None  # re-resolve after stripping guarded temp directories
os.environ.setdefault("TMPDIR", tempfile.gettempdir())

if _hermes_home_points_at_production(os.environ.get("HERMES_HOME", "")):
    _SESSION_HERMES_HOME = tempfile.mkdtemp(prefix="hermes-test-home-")
    os.environ["HERMES_HOME"] = _SESSION_HERMES_HOME
    # Marker for re-imported conftest module bodies (xdist workers exec this
    # file more than once): the second import sees the already-redirected
    # sandbox in the env and must not register it as a guarded "real" root.
    os.environ["HERMES_TEST_SANDBOX_HOME"] = _SESSION_HERMES_HOME
    atexit.register(shutil.rmtree, _SESSION_HERMES_HOME, True)

# PYTHONPYCACHEPREFIX is a bytecode-mirror escape hatch: when set (the
# bundled desktop app exports it as %LOCALAPPDATA%\hermes\pycache),
# importlib/pytest write .pyc files to <prefix>/<absolute source path>
# instead of next to the sources. Un-scrubbed, that mirror lands under
# the REAL hermes home and trips the real-home tripwire on any module
# imported after sandboxing (test_find_shell was the first to bite).
# Clear it so bytecode goes back beside the (already sandboxed) sources.
os.environ.pop("PYTHONPYCACHEPREFIX", None)
try:
    sys.pycache_prefix = None
except AttributeError:
    pass

# Subprocess-surviving isolation marker (#82770). PYTEST_CURRENT_TEST /
# PYTEST_VERSION are pytest's own vars, and tests that spawn children
# routinely rebuild the child env and strip them ("the subprocess must look
# like a real CLI") — which used to disarm hermes_state's live-DB guard in
# the child at the same moment the child lost the HERMES_HOME redirect.
# HERMES_TEST_ISOLATION is OUR marker: exported here (before any test module
# imports), inherited by every child by default, and honored by
# hermes_state_guard._running_under_pytest() as a test-context signal. A child
# that carries it and still resolves the production state.db fails hard.
# Tests that legitimately need a child to look like a non-test process AND
# open a real DB must export HERMES_STATE_DB_GUARD_BYPASS=1 in that child's
# env instead of stripping markers.
os.environ["HERMES_TEST_ISOLATION"] = os.environ.get("HERMES_HOME", "") or "1"

# Lazy-install kill-switch, set before any test module is imported. The per-test
# fixture below sets it too, but collection runs first: agent/bedrock_adapter.py
# calls lazy_deps.ensure() at import time, so collecting a file that imports it
# ran a real `uv pip install boto3` into the shared venv while other files raced
# on whether botocore was importable yet.
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

#: HERMES_HOME as it stood when conftest was imported - i.e. before any test
#: module could import code that configures logging. Recorded so the guard in
#: tests/test_log_isolation.py can assert the sandbox existed AT THAT MOMENT.
#: Reading os.environ from inside a test is useless here: the per-test
#: `_isolate_env` fixture has sandboxed it by then, so the check would pass
#: even with this block removed.
HERMES_HOME_AT_CONFTEST_IMPORT = os.environ.get("HERMES_HOME", "")

# ── Host-rendezvous isolation ───────────────────────────────────────────────
# ``gateway/host_rendezvous.py`` publishes ONE record per role per OS USER, in
# ``$HERMES_GATEWAY_LOCK_DIR`` else ``$XDG_STATE_HOME/hermes/gateway-locks`` —
# deliberately outside HERMES_HOME, because the host singleton spans profiles.
# Under the per-file parallel runner that directory is shared by ~40 pytest
# subprocesses: one test that boots a real gateway publishes a record, and every
# other file's lifecycle code then correctly attaches to a gateway that has
# nothing to do with it. Give each pytest PROCESS its own rendezvous dir.
#
# A caller-supplied value always wins (both here and in the per-test fixture
# below) — otherwise the documented override is a silent no-op.
HOST_LOCK_DIR_AT_CONFTEST_IMPORT = os.environ.get("HERMES_GATEWAY_LOCK_DIR", "")
if not HOST_LOCK_DIR_AT_CONFTEST_IMPORT:
    # Deterministic per-PID name, not mkdtemp: the parallel runner SIGKILLs a worker on timeout,
    # which never runs atexit, so a random dir per run leaked one directory per killed worker.
    # A fixed name is reused by the next process with that PID, and dead siblings are swept here.
    _LOCK_DIR_PREFIX = "hermes-test-gateway-locks-"
    _LOCK_DIR_ROOT = Path(tempfile.gettempdir())
    for _stale in _LOCK_DIR_ROOT.glob(f"{_LOCK_DIR_PREFIX}*"):
        try:
            _stale_pid = int(_stale.name[len(_LOCK_DIR_PREFIX):])
        except ValueError:
            continue
        try:
            os.kill(_stale_pid, 0)
        except OSError:
            shutil.rmtree(_stale, ignore_errors=True)
    _SESSION_LOCK_DIR = str(_LOCK_DIR_ROOT / f"{_LOCK_DIR_PREFIX}{os.getpid()}")
    shutil.rmtree(_SESSION_LOCK_DIR, ignore_errors=True)
    os.environ["HERMES_GATEWAY_LOCK_DIR"] = _SESSION_LOCK_DIR
    atexit.register(shutil.rmtree, _SESSION_LOCK_DIR, True)


# ── File-level scheduling isolation ──────────────────────────────────────────
# Tests run via ``scripts/run_tests.sh``, which runs the per-file runner
# (``scripts/run_tests_parallel.py``) on every host: every file in its own
# freshly-spawned ``python -m pytest <file>`` subprocess — cross-file state
# leakage is impossible. Intra-file ordering is the test author's
# responsibility on every host — if test A in foo.py mutates state that
# test B in foo.py reads, that's a real bug to fix in the file (it would
# also bite anyone running ``pytest tests/foo.py`` directly).
#
# See ``scripts/run_tests.sh`` for the runner.


# Topic modules split out to keep this file under the size gate. They are
# imported rather than listed in ``pytest_plugins``: this is not the rootdir
# conftest (that is the repo root), and pytest fails a run that loads a
# non-root conftest carrying ``pytest_plugins`` after startup (e.g. ``pytest .``).
# Fixtures imported here register exactly as if they were defined here.
from tests._fixtures.env_filter import _HERMES_BEHAVIORAL_VARS, _looks_like_credential
from tests._fixtures.live_system_guard import (  # noqa: F401 — _live_system_guard registers here
    _GATEWAY_LOOKALIKE_MARK,
    _LIVE_SYSTEM_GUARD_BYPASS_MARK,
    _live_system_guard,
)
from tests._fixtures.platform_gating import _platforms_gate_reason, _reject_contradictory_platform_marks


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, tmp_path_factory, monkeypatch):
    """Blank out all credential/behavioral env vars so local and CI match.

    Also redirects HOME and HERMES_HOME to per-test tempdirs so code that
    reads ``~/.hermes/*`` can't touch the real one, and pins TZ/LANG so
    datetime/locale-sensitive tests are deterministic.
    """
    # 1. Blank every credential-shaped env var that's currently set.
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    # 2. Blank behavioral HERMES_* vars that could change test semantics.
    for name in _HERMES_BEHAVIORAL_VARS:
        monkeypatch.delenv(name, raising=False)

    # Honcho's fallback host/config resolution legitimately reads the user's
    # global ~/.honcho/config.json. Keep HOME stable (subprocess tests depend
    # on it), but pin the host so ordinary tests cannot inherit a developer's
    # defaultHost and silently select the wrong nested config block. Tests of
    # custom host resolution override/delete this explicitly.
    monkeypatch.setenv("HERMES_HONCHO_HOST", "hermes")

    # 3. Isolate both inputs to profile/root resolution. HERMES_HOME alone
    #    is insufficient: get_default_hermes_root() resolves the native root
    #    too, to distinguish standard profiles from custom deployments.
    #    Patch only the Hermes default, not HOME/Path.home(). Subprocesses need
    #    a stable HOME. Hardcoded real-home I/O must still trip the guard.
    import hermes_constants

    platform_default = hermes_constants._get_platform_default_hermes_home

    def isolated_platform_default() -> Path:
        root = platform_default()
        # Explicit Path.home()/LOCALAPPDATA overrides in individual tests
        # still select their own layout. Suffix changes retain their name.
        return tmp_path / root.name if root.parent == _NATIVE_HERMES_PARENT else root

    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", isolated_platform_default
    )
    fake_hermes_home = tmp_path / "hermes_test"
    fake_hermes_home.mkdir()
    (fake_hermes_home / "sessions").mkdir()
    (fake_hermes_home / "cron").mkdir()
    (fake_hermes_home / "memories").mkdir()
    (fake_hermes_home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_hermes_home))
    # 3-GUARD (t_f64dfdb6): a sandbox home under the live root is no sandbox -
    # get_default_hermes_root() resolves it to the REAL ~/.hermes, so profile
    # writes escape into the operator's profiles/. Refuse instead of leaking.
    if _is_under_real_hermes_root(fake_hermes_home):
        raise RuntimeError(
            f"HERMETICITY VIOLATION: per-test HERMES_HOME {fake_hermes_home} is "
            f"under the live Hermes root {_real_hermes_root()}; profile writes "
            "would land in the real profiles/. Point TMPDIR / --basetemp / "
            "PYTEST_DEBUG_TEMPROOT outside it. See t_f64dfdb6."
        )
    # A test that pins the process home (hermes_constants.pin_process_hermes_home) must not
    # leak that module-global into the next test's routed-profile decisions.
    try:
        import hermes_constants as _hc
        monkeypatch.setattr(_hc, "_PINNED_PROCESS_HERMES_HOME", None, raising=False)
    except Exception:
        pass
    # Per-TEST host-rendezvous dir (see the session-level block at the top): the
    # host gateway/serve record is shared per OS user by design, so without this
    # one test's published owner makes the next test's lifecycle code attach to it.
    # HOME is deliberately NOT redirected above, so an unpinned run would read and
    # write the developer's live ~/.local/state/hermes/gateway-locks.
    # Skipped when the caller supplied the variable, so an explicit override still
    # works (tests of the resolution rule itself rely on that).
    if not HOST_LOCK_DIR_AT_CONFTEST_IMPORT:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway-locks"))
    # Relay 0.9 normally discovers the user's XDG plugins.toml. Select an empty
    # per-test user file instead so tests cannot activate a developer's plugins,
    # without changing XDG_CONFIG_HOME for unrelated Hermes code under test.
    # Outside tmp_path: tests that list or git-status their tmp dir must not see it.
    relay_plugins = tmp_path_factory.getbasetemp() / "relay-plugins.toml"
    if not relay_plugins.exists():
        relay_plugins.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_NEMO_RELAY_PLUGINS_TOML", str(relay_plugins))
    # Keep the subprocess-surviving isolation marker pointed at THIS test's
    # home (#82770): children spawned by the test inherit it by default, so
    # hermes_state's live-DB guard stays armed in them even when the test
    # strips pytest's own PYTEST_* vars from the child env.
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(fake_hermes_home))
    # t_09fea045: `kanban create` refuses to mint an unhomed card; fixtures that
    # create cards without a session predate that. The refusal's own tests
    # (test_kanban_operator_home.py) unset this.
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNHOMED_CREATE", "1")
    # And never let a developer-shell (or leaked child) bypass disarm the
    # guard for in-process code under test.
    monkeypatch.delenv("HERMES_STATE_DB_GUARD_BYPASS", raising=False)
    # t_4853212d: the suite (and its gateway-run subprocess rigs) runs gateways
    # from a checkout, often one under /Volumes/fleet-scratch. Disarm the
    # shadow-cwd boot guard; its own tests delete this.
    monkeypatch.setenv("HERMES_ALLOW_SHADOW_CWD", "1")

    # 3a-GUARD (2026-07-24 WAL incident): hermeticity canary — after the
    # redirect, the resolved state.db path must live under the sandbox. If it
    # resolves to the REAL production path, some import froze the location
    # before the redirect (the DEFAULT_DB_PATH class of bug) — hard-fail the
    # test rather than silently letting the suite contend on prod WAL.
    # Only a module that is ALREADY imported can have frozen the path; one
    # imported later resolves against the redirected home. Skipping the
    # import here saves ~0.4 s per pytest run for files that never touch
    # it (t_24f73ced).
    _hs_guard = sys.modules.get("hermes_state")
    if _hs_guard is not None:
        _resolved = _hs_guard.DEFAULT_DB_PATH
        _real_home = Path(os.environ.get("HERMES_REAL_HOME", str(Path.home() / ".hermes")))
        if _resolved == _real_home / "state.db":
            raise RuntimeError(
                f"HERMETICITY VIOLATION: state.db resolved to PRODUCTION path {_resolved} "
                "despite HERMES_HOME redirect. See 2026-07-24 incident / t_43d5c42d."
            )

    # 3b. hermes_state computes ``DEFAULT_DB_PATH = get_hermes_home() / "state.db"``
    #     at import time. When the module is first imported at collection (any
    #     test file with a top-level ``from hermes_state import ...``) that
    #     happens BEFORE this fixture ever runs, so every argless
    #     ``SessionDB()`` in every test opens the developer's REAL state.db —
    #     reading real sessions into assertions and writing test rows into the
    #     real profile. Re-pin the constant to this test's home. (Several test
    #     files already do this locally; this makes it an invariant. On the fork
    #     DEFAULT_DB_PATH is lazily resolved via module __getattr__, so the 3a
    #     canary passes; this re-pin is belt-and-suspenders for any module that
    #     captured the value.)
    # 3c. Multi-profile hosting is a process-global latch (``set_multiplex_active`` and the
    #     launch-env snapshot flip once and stay). A test that routes one RPC/request to a named
    #     profile would otherwise leave every later test in the file fail-closed (unscoped
    #     ``get_env_value`` in a test body raises). Reset the latch per test.
    secret_scope_mod = sys.modules.get("agent.secret_scope")
    if secret_scope_mod is not None and hasattr(secret_scope_mod, "_MULTIPLEX_ACTIVE"):
        monkeypatch.setattr(secret_scope_mod, "_MULTIPLEX_ACTIVE", False)
    if secret_scope_mod is not None and hasattr(secret_scope_mod, "_AUTO_PINNED_HOME"):
        monkeypatch.setattr(secret_scope_mod, "_AUTO_PINNED_HOME", None)
    launch_policy_mod = sys.modules.get("tui_gateway.launch_profile_policy")
    if launch_policy_mod is not None and hasattr(launch_policy_mod, "_snapshot"):
        monkeypatch.setattr(launch_policy_mod, "_snapshot", None)
    tui_server_mod = sys.modules.get("tui_gateway.server")
    if tui_server_mod is not None and hasattr(tui_server_mod, "_served_profile_homes"):
        monkeypatch.setattr(tui_server_mod, "_served_profile_homes", set())

    hermes_state_mod = sys.modules.get("hermes_state")
    if hermes_state_mod is not None and hasattr(hermes_state_mod, "DEFAULT_DB_PATH"):
        monkeypatch.setattr(
            hermes_state_mod, "DEFAULT_DB_PATH", fake_hermes_home / "state.db"
        )

    # 3d. Strip any root-logger file handler pointing OUTSIDE the test sandbox.
    #     hermes_logging.setup_logging() attaches a RotatingFileHandler at
    #     ``<home>/logs/agent.log`` to the ROOT logger and remembers it via a
    #     module global. If any module under test calls it (directly, or by
    #     building a real AIAgent) before HERMES_HOME is redirected — an
    #     import-order race — the handler targets the REAL ~/.hermes/logs and
    #     every WARNING a test emits (e.g. COMPACTION_STATS_RECONCILE_FAILED)
    #     appends to the PRODUCTION log, which then trips the compaction-stats
    #     watcher cron with false pages (2026-06-27). Strip those handlers at the
    #     start of every test so a leaked WARNING can never reach the live log.
    _strip_nonsandbox_file_handlers(str(tmp_path))

    # 4. Deterministic locale / timezone / hashseed. CI runs in UTC with
    #    C.UTF-8 locale; local dev often doesn't. Pin everything.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    # 4b. Disable AWS IMDS lookups. Without this, any test that ends up
    #     calling has_aws_credentials() / resolve_aws_auth_env_var()
    #     (e.g. provider auto-detect, status command, cron run_job) burns
    #     ~2s waiting for the metadata service at 169.254.169.254 to time
    #     out. Tests don't run on EC2 — IMDS is always unreachable here.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
    # Tirith auto-installs from GitHub when enabled and missing. Unit tests
    # should never perform that implicit network/bootstrap path; Tirith-specific
    # tests opt back in by patching the security config directly.
    monkeypatch.setenv("TIRITH_ENABLED", "false")
    # On-demand extras (pm.sync_venv) install mid-test-run by design —
    # _allow_lazy_installs() fails open for users. Unit tests must never reach
    # pip/the network: with the SDK absent, any agent init whose tool checks
    # touch a lazy feature (e.g. check_tts_requirements →
    # ensure("tts.elevenlabs")) spawns a real pip install — which hangs to the
    # suite timeout under tests that set fake proxy env vars. The kill-switch
    # makes ensure() raise FeatureUnavailable immediately instead.
    # extras tests override this var in both directions.
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")

    # 5. Reset plugin singleton so tests don't leak plugins from
    #    ~/.hermes/plugins/ (which, per step 3, is now empty — but the
    #    singleton might still be cached from a previous test).
    try:
        _plugins_mod = sys.modules.get("hermes_cli.plugins")
        if _plugins_mod is not None:
            monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
            # Also clear the keyed per-home manager cache (and any plugin
            # submodules it left in sys.modules) so a manager built for a
            # previous test's tmp_path HERMES_HOME can't leak forward. Paths
            # are unique per test, so collisions are unlikely, but a full
            # reset keeps this fixture the single source of plugin-state
            # hygiene rather than relying on path uniqueness.
            _plugins_mod._reset_plugin_managers_for_tests()
    except Exception:
        pass

    # 5b. Reset auxiliary_client module-level runtime caches. Under the
    #     canonical per-file-subprocess runner these can't leak across
    #     files, but a single-process multi-file run (e.g. ``pytest
    #     tests/agent/``) shares one interpreter, and three caches here
    #     otherwise carry state from one test into the next:
    #       • the "recently 402'd" unhealthy-provider cache — a real
    #         AIAgent construction in an earlier test marks nous/openrouter
    #         unhealthy (600s TTL), making _resolve_auto skip a *mocked*
    #         provider in a later test_auxiliary_main_first test (it returns
    #         None and the assertion fails). This was a concrete 5-failure
    #         cascade.
    #       • the runtime-main override (set_runtime_main / clear_runtime_main).
    #       • the resolved-client cache keyed by provider config.
    #     Each ships its own reset entrypoint already (authored "for tests");
    #     we just wire them in, same as the _plugin_manager reset above.
    try:
        _aux_mod = sys.modules.get("agent.auxiliary_client")
        if _aux_mod is not None:
            _aux_mod._reset_aux_unhealthy_cache()
            _aux_mod.clear_runtime_main()
            _aux_mod._client_cache.clear()
    except Exception:
        pass

    # 5c. Reset the models.dev capability cache. agent.models_dev caches the
    #     provider/model registry in a module-level dict (_models_dev_cache,
    #     1h TTL). test_models_dev.py assigns a tiny 6-provider SAMPLE_REGISTRY
    #     to it directly (no teardown), and test_vision_routing_31179's
    #     _fresh_modules() reimports auxiliary_client/image_routing but does
    #     NOT drop agent.models_dev — so a later vision test reads the stale
    #     6-key cache (missing real capability metadata), _lookup_supports_vision
    #     returns the wrong answer, and "skip text-only main / use vision-capable
    #     main" assertions flip. Reset to an empty, expired cache so the next
    #     test refetches (or stubs) cleanly. Tests that exercise the cache set
    #     it explicitly after this fixture runs, so this does not interfere.
    try:
        _md_mod = sys.modules.get("agent.models_dev")
        if _md_mod is not None:
            _md_mod._models_dev_cache = {}
            _md_mod._models_dev_cache_time = 0
            _md_mod._MODELS_DEV_TO_PROVIDER = None
    except Exception:
        pass

    # 5d. Reset the active-skin singleton. hermes_cli.skin_engine caches the
    #     active skin in module globals (_active_skin / _active_skin_name),
    #     lazily initialised by get_active_skin() and mutated by
    #     set_active_skin()/init_skin_from_config(). Once any test (or a config
    #     load reading a leaked display.skin) switches away from "default", the
    #     cached skin persists and a later test that asserts default behaviour
    #     (e.g. get_cute_tool_message's "┊" tool prefix) reads the wrong skin
    #     (ares→"╎", daylight/poseidon→"│"). Reset to the lazy-init state so
    #     each test starts on "default"; tests that need a skin set it
    #     explicitly after this fixture runs.
    try:
        _skin_mod = sys.modules.get("hermes_cli.skin_engine")
        if _skin_mod is not None:
            _skin_mod._active_skin = None
            _skin_mod._active_skin_name = "default"
    except Exception:
        pass

    # Explicitly clear provider-specific base URL overrides that don't match
    # the generic credential-shaped env-var filter above.
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    monkeypatch.delenv("GMI_BASE_URL", raising=False)


# Backward-compat alias — old tests reference this fixture name. Keep it
# as a no-op wrapper so imports don't break.
@pytest.fixture(autouse=True)
def _isolate_hermes_home(_hermetic_environment):
    """Alias preserved for any test that yields this name explicitly."""
    return None


@pytest.fixture(autouse=True)
def _isolate_fallback_sticky_store():
    """Fresh process-global sticky store per test (fallback spec §4.2): its
    in-memory map outlives the per-test HERMES_HOME sandbox otherwise."""
    try:
        from agent import fallback_sticky_store as _fss
        from agent import fallback_wiring as _fw
    except Exception:  # noqa: BLE001
        yield
        return
    _fss._DEFAULT = None
    _fw._last_primary_note.clear()
    _fw._purged = False
    yield
    _fss._DEFAULT = None
    _fw._last_primary_note.clear()


@pytest.fixture(autouse=True)
def _isolate_session_contextvars():
    """Reset every gateway session ContextVar to ``_UNSET`` around each test.

    ``gateway.session_context.clear_session_vars`` intentionally leaves the
    ``_VAR_MAP`` contextvars bound to ``""`` ("explicitly cleared" — it
    suppresses the ``os.environ`` fallback in ``get_session_env``; correct
    production semantics). But contextvars are process-global for the pytest
    main thread, so in any single-process multi-file run a test file that
    exercises set/clear (e.g. tests/tools/test_kanban_session_attribution.py)
    leaks the ``""`` bindings across the file boundary, shadowing later
    tests' ``monkeypatch.setenv("HERMES_SESSION_*", ...)`` identities — the
    test_create_subscribes_gateway_session ordering failure. The canonical
    per-file-subprocess runner hides this; plain ``pytest tests/tools/``,
    ``-p randomly``, or a CI re-slice exposes it.

    Setup binds the whole family to ``_UNSET`` (the fresh-process state every
    test expects); teardown restores the pre-test bindings via the reset
    tokens. Uses the production ``reset_session_vars``/``restore_session_vars``
    helpers so the covered set can never drift from ``_VAR_MAP`` (they also
    cover ``_SESSION_ASYNC_DELIVERY`` and the runtime-cwd contextvar, which
    live outside the map). Do NOT special-case individual var names here —
    a partial reset is the falsified fix (a leaked ``""`` in any unlisted
    var still poisons downstream tests).

    Regression guard: tests/tools/test_session_contextvar_isolation.py.
    """
    # Not imported yet => every var still holds its declared default, which is
    # exactly what reset_session_vars() would set. Importing it here costs
    # ~0.15 s per run (gateway -> hermes_state -> config) for tests that never
    # touch a session var (t_24f73ced).
    sc = sys.modules.get("gateway.session_context")
    if sc is None:
        # reset_session_vars() also resets the runtime-cwd var; that module
        # can be imported (and bound) without session_context.
        rc = sys.modules.get("agent.runtime_cwd")
        token = rc.reset_session_cwd() if rc is not None else None
        yield
        if token is not None:
            rc._SESSION_CWD.reset(token)
        # The test may have imported session_context and bound vars: put the
        # whole family back to the fresh-process _UNSET state (no pre-test
        # tokens exist to restore, and fresh IS the pre-test state).
        sc = sys.modules.get("gateway.session_context")
        if sc is not None:
            sc.reset_session_vars()
        return
    tokens = sc.reset_session_vars()
    yield
    sc.restore_session_vars(tokens)


@pytest.fixture(autouse=True)
def _reset_foreground_exit_fence():
    """A test that drives a hard-exit path raises the one-way foreground-spawn fence; lower it after."""
    yield
    if (base := sys.modules.get("tools.environments.base")) is not None:
        base._exit_fenced = False


@pytest.fixture(autouse=True)
def _neutralize_kanban_memory_guard(request, monkeypatch):
    """Pin the kanban dispatcher's memory guard to "no data" for every test.

    The dispatcher consults live system memory before spawning (OOF-30/
    OOF-77: memory-derived default cap + pressure-based spawn restriction).
    Left un-patched, dispatch tests would pass or fail based on how loaded
    the CI runner happens to be. Defaulting the sample to ``{}`` makes the
    derived cap ``None`` and the pressure level ``"unknown"`` — i.e. the
    pre-guard behaviour every existing test was written against. Tests that
    exercise the guard itself opt out with
    ``@pytest.mark.real_memory_guard`` or patch the seam directly.
    """
    if request.node.get_closest_marker("real_memory_guard"):
        return
    try:
        from hermes_cli import kanban_db_dispatch as _kbd_mod
    except Exception:
        return
    monkeypatch.setattr(_kbd_mod, "_system_memory_sample", lambda: {}, raising=False)


@pytest.fixture(autouse=True)
def _neutralize_git_safe_directory_read(request, monkeypatch):
    """Skip the ``git config --get-all safe.directory`` pre-read in ``noninteractive_git_env()``.

    Many tests fake ``subprocess.run``/``Popen`` with a fixed sequence of expected git calls;
    the pre-read is an extra spawn that would trip them. Tests of the carve-out itself opt in
    with ``@pytest.mark.real_safe_directory``.
    """
    if request.node.get_closest_marker("real_safe_directory"):
        return
    try:
        from hermes_cli import _subprocess_compat
    except Exception:
        return
    monkeypatch.setattr(_subprocess_compat, "_user_safe_directories", lambda base_env: [], raising=False)


@pytest.fixture(autouse=True)
def _close_leaked_session_dbs():
    """Close every SessionDB a test constructed but forgot to close.

    Root cause of OOM incident 20260816: ~40 files under tests/hermes_cli/
    build ``SessionDB(...)`` directly and never call ``close()``. Each open
    instance holds the writer connection (state.db + -wal fds), up to
    ``_READ_POOL_MAX`` pooled read connections, per-connection SQLite page
    caches, and — once token accounting has run — an ``atexit`` registration
    that pins the instance alive until interpreter exit. Under the sanctioned
    per-file-process runner this is invisible, but a raw single-process
    ``pytest tests/hermes_cli/`` accumulated 16-25 GB RSS and had to be
    OOM-killed three times in one day.

    Rather than editing every test file, ``SessionDB.__init__`` registers each
    instance in ``hermes_state_guard._test_instance_registry`` (a WeakSet,
    populated only when the ``HERMES_TEST_ISOLATION`` marker is set — i.e.
    only under this suite). This teardown closes whatever the test left open.
    ``close()`` is idempotent (``self._conn`` is None afterwards) and also
    unregisters the pinning atexit hook, so instances become collectable.

    Snapshotting the registry BEFORE the test and closing only NEW instances
    is deliberately avoided: closing pre-existing instances is harmless (they
    were leaked by an earlier test in the same process) and the simpler
    close-everything sweep is what actually bounds the process.

    Instances opened through ``hermes_state_registry.acquire()`` are skipped:
    on those ``close()`` releases a refcount rather than closing, so a sweep
    would silently retire a shared generation that a wider-scoped fixture
    still holds. The registry owns that lifecycle (``close_all()``).

    Before the sweep, the auto-title upgrade threads a turn spawned are joined
    (bounded): they hold the turn's SessionDB and write to it (and print to
    ``sys.stdout``) after the turn returns, so left running they race this
    close (``_reopen_after_close_locked`` on a daemon thread), the next test's
    capture, and interpreter finalization — the ``Fatal Python error`` /
    SIGSEGV shape of #113186, seen from ``tests/gateway/test_timestamp_sidecar_replay.py``.
    """
    yield
    # sys.modules lookup, not import: a file that never touched title_generator spawned
    # nothing. Tests that swap in a stub module (tui_gateway golden transcript) have no
    # real threads either, so a stub without the helper is the same "nothing to join" case.
    wait = getattr(sys.modules.get("agent.title_generator"), "wait_for_title_upgrades", None)
    if wait is not None:
        wait()
    try:
        from hermes_state_guard import _test_instance_registry as registry
    except Exception:
        return
    if not registry:
        return
    for db in list(registry):
        if getattr(db, "_shared_registry_owned", False):
            continue
        try:
            db.close()
        except Exception:
            # Teardown must never fail a passing test; a close that raises
            # (cross-thread ProgrammingError, already-closed) leaves at most
            # the one connection for the next sweep / process exit.
            pass


@pytest.fixture(autouse=True)
def _pin_codex_context_policy_advertised(request, monkeypatch):
    """Pin ``model.codex_context_policy`` to ``advertised`` for every test.

    The runtime default is ``large`` (bare eligible Codex slugs resolve to
    the live-verified window; card t_73689428). The pre-existing suite was
    written against the ``advertised`` opt-in behaviour and must keep
    passing UNCHANGED under it, so it is pinned here. Tests that exercise
    the knob itself (default, config read, ``large`` behaviour) opt out with
    ``@pytest.mark.real_codex_context_policy``.
    """
    if request.node.get_closest_marker("real_codex_context_policy"):
        return
    try:
        from agent import model_metadata as _mm
    except Exception:
        return
    monkeypatch.setattr(
        _mm, "_codex_context_policy_from_config", lambda: "advertised",
        raising=False,
    )


@pytest.fixture(autouse=True)
def _neutralize_webbrowser(monkeypatch):
    """Record browser-open attempts instead of opening real browser windows."""
    import webbrowser as _webbrowser

    opened: list[object] = []

    def _record(url=None, *_args, **_kwargs):
        opened.append(url)
        return True

    class _RecordingBrowser:
        def open(self, url, *_args, **_kwargs):
            return _record(url)

        def open_new(self, url, *_args, **_kwargs):
            return _record(url)

        def open_new_tab(self, url, *_args, **_kwargs):
            return _record(url)

    browser = _RecordingBrowser()

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(_webbrowser, name, _record, raising=False)
    monkeypatch.setattr(_webbrowser, "get", lambda *_args, **_kwargs: browser)

    return opened


@pytest.fixture(autouse=True)
def _neutralize_macos_keychain_creds(request, monkeypatch):
    """Default Anthropic credential resolution away from the real macOS Keychain."""
    if request.node.get_closest_marker(_ALLOW_MACOS_KEYCHAIN_MARK):
        return None

    # Patch the implementation owner (agent.anthropic_credentials) AND the
    # adapter re-export: after the adapter godfile split, the real call
    # executes inside agent.anthropic_credentials, so patching only the
    # adapter alias silently stopped intercepting Keychain reads.
    #
    # ``read_claude_code_credentials()`` has TWO sources: the Keychain and
    # ``~/.claude/.credentials.json``. Stubbing only the Keychain still lets a
    # developer's real Claude Code login leak in from the FILE — outside
    # HERMES_HOME, so the sandbox redirect can't catch it. That injects a
    # phantom ``source=claude_code`` entry into every anthropic credential
    # pool built in tests (with a per-run random id), which silently changes
    # pool arithmetic: a fixture that writes a ONE-entry auth.json gets a
    # TWO-entry pool, so single-entry guards never fire.
    #
    # Scoped, NOT blanket: tests that legitimately exercise this path point
    # HOME at a tmp_path and write their own fake credentials file (see
    # tests/hermes_cli/test_codex_cli_model_picker.py::claude_code_only_env).
    # Only suppress the read when it would resolve to the REAL developer home
    # (captured from HERMES_REAL_HOME's parent, or pwd, before any redirect).
    try:
        _rh = os.environ.get("HERMES_REAL_HOME", "").strip()
        if _rh:
            _real_home_dir = Path(_rh).expanduser().parent
        else:
            import pwd as _pwd

            _real_home_dir = Path(_pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        _real_home_dir = None
    _real_claude_creds = (
        (_real_home_dir / ".claude" / ".credentials.json") if _real_home_dir else None
    )

    def _make_hermetic_read_from_file(_orig_read_file, _mod_ref):
        def _hermetic_read_from_file(*_args, **_kwargs):
            if _real_claude_creds is None:
                return None
            try:
                # Resolve the target the READER will actually use, by calling
                # the same seam it calls -- ``claude_code_credentials_path()``
                # -- rather than re-deriving ``Path.home()/.claude/...`` here.
                #
                # parity 2026-08-30: re-deriving the path was too narrow. It
                # only detected redirects performed via ``Path.home``/``HOME``,
                # so a fixture that redirects the SUPPORTED way -- patching
                # ``agent.anthropic_credentials.claude_code_credentials_path``
                # (upstream's own borrowed-row / rotation-verdict suites do
                # exactly this) -- still looked like the real developer home and
                # had its OWN fake credentials file suppressed. That left
                # ``load_pool("anthropic")`` with an empty pool and reds shaped
                # like ``StopIteration`` on the expected ``claude_code`` row.
                # Resolving through the seam keeps the fork's leak guard intact
                # (an unredirected read still resolves to the real home and is
                # suppressed) while honouring every redirect style.
                _path_fn = getattr(_mod_ref, "claude_code_credentials_path", None)
                if callable(_path_fn):
                    target = Path(_path_fn())
                else:
                    target = Path.home() / ".claude" / ".credentials.json"
                if target.resolve() != _real_claude_creds.resolve():
                    # Redirected to a sandbox: this is a deliberate fixture, let
                    # the real reader run against the fake file.
                    if _orig_read_file is None:
                        return None
                    return _orig_read_file(*_args, **_kwargs)
            except Exception:
                return None
            return None

        return _hermetic_read_from_file

    for _module_name in ("agent.anthropic_credentials", "agent.anthropic_adapter"):
        try:
            _mod = importlib.import_module(_module_name)
        except Exception:
            continue
        monkeypatch.setattr(
            _mod,
            "_read_claude_code_credentials_from_keychain",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
        monkeypatch.setattr(
            _mod,
            "_read_claude_code_credentials_from_file",
            _make_hermetic_read_from_file(
                getattr(_mod, "_read_claude_code_credentials_from_file", None),
                _mod,
            ),
            raising=False,
        )
        # The #98334 refresh write also mirrors into the Keychain; keep that out of
        # the real store in any test that hasn't explicitly opted in.
        monkeypatch.setattr(
            _mod,
            "_mirror_claude_code_credentials_to_keychain",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
    return None


# ── Kanban write guard (#69283) ─────────────────────────────────────────────
# When hermetic isolation is bypassed (stale checkout, wrong rootdir, direct
# invocation), kanban writes silently pollute the real ~/.hermes. This autouse
# fixture patches ``kanban_db_connect.connect`` to refuse writes whose resolved DB
# path lands under the REAL kanban root (captured at import time, before any
# fixture rewires the environment). A deny-list is used instead of an
# allow-list because test-level fixtures legitimately move HERMES_HOME to
# sibling directories — an allow-list captured at setup time would see the
# stale autouse-set value and falsely reject hermetic tests (#69385 review).


def _capture_real_kanban_root() -> Path:
    """Resolve the REAL kanban root from the pre-test environment.

    Uses the pre-sandbox environment snapshot taken at the very top of this
    file (before the session HERMES_HOME sandbox rewired the env), so the
    deny-list keeps pointing at the operator's actual root. Mirrors
    ``kanban_db.kanban_home()`` resolution order:
    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty
    2. the real (pre-sandbox) Hermes root otherwise
    """
    if _PRE_SANDBOX_KANBAN_OVERRIDE:
        return Path(_PRE_SANDBOX_KANBAN_OVERRIDE).expanduser().resolve()
    if _PRE_SANDBOX_HERMES_HOME and not _hermes_home_points_at_production(
        _PRE_SANDBOX_HERMES_HOME
    ):
        # HERMES_HOME was genuinely set to a CUSTOM root before the sandbox
        # (production-pointing values are sandboxed away above, in which case
        # the env still holds the tempdir and the resolver would be wrong) —
        # honor it via the normal resolver (it may be a profile dir whose
        # root matters).
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root().resolve()
    # No pre-existing HERMES_HOME: the real root is the platform default,
    # NOT the sandbox tempdir now sitting in the env.
    return (Path.home() / ".hermes").resolve()


_REAL_KANBAN_ROOT = _capture_real_kanban_root()


@pytest.fixture(autouse=True)
def _kanban_write_guard(_hermetic_environment, monkeypatch):
    """Fail-closed guard: refuse kanban writes that target the REAL root.

    Uses a **deny-list**: only blocks writes where the resolved DB path
    (explicit ``db_path`` or ``kanban_db_path()``) lands under the real
    ``~/.hermes`` captured at import time. Hermetic tests that legitimately
    move HERMES_HOME to sibling tempdirs are unaffected.

    Only patches when ``hermes_cli.kanban_db_connect`` is *already imported*
    — a ``sys.modules`` probe, not an import — so the guard never drags the
    kanban module into unrelated test processes.

    Uses ``monkeypatch.setattr`` so pytest restores ``connect`` automatically
    after each test (no stacked wrappers or state leakage across tests).
    """
    _kdb = sys.modules.get("hermes_cli.kanban_db")
    _kdbc = sys.modules.get("hermes_cli.kanban_db_connect")
    if _kdb is None or _kdbc is None:
        return

    # The sys.modules probe can observe the module MID-IMPORT: a fixture
    # boundary firing while another test's lazy `import hermes_cli.kanban_db`
    # is still executing sees a partially initialized module whose `connect`
    # doesn't exist yet (AttributeError flake, caught in a full-suite run).
    # A half-imported module has no callers yet either — nothing to guard
    # this round; the next test's fixture will patch the completed module.
    _orig_connect = getattr(_kdbc, "connect", None)
    if _orig_connect is None or getattr(_kdb, "kanban_db_path", None) is None:
        return

    def _guarded_connect(db_path=None, *args, **kwargs):
        if db_path is not None:
            resolved = Path(db_path).expanduser().resolve()
        else:
            resolved = (
                _kdb.kanban_db_path(board=kwargs.get("board"))
                .expanduser()
                .resolve()
            )
        try:
            resolved.relative_to(_REAL_KANBAN_ROOT)
        except ValueError:
            # Resolved path is NOT under the real root — safe to write.
            return _orig_connect(db_path, *args, **kwargs)
        raise RuntimeError(
            f"kanban_write_guard: kanban DB path resolved to {resolved}, "
            f"which is under the REAL kanban root ({_REAL_KANBAN_ROOT}). "
            f"Hermetic isolation has been bypassed — refusing to write "
            f"to the real ~/.hermes. See #69283."
        )

    monkeypatch.setattr(_kdbc, "connect", _guarded_connect)


# ── Live state.db write guard ───────────────────────────────────────────────
# Companion to the kanban guard above, for the MAIN state database.
# ``hermes_state._ensure_test_isolation`` (the single choke point every
# ``SessionDB()`` construction goes through) refuses, under pytest, any DB
# path that resolves inside the REAL Hermes root. This fixture wires the
# test-side knobs:
#   • honors ``@pytest.mark.live_system_guard_bypass`` (the established
#     escape-hatch marker) by disabling the state-db guard for that test;
#   • injects the pre-sandbox CUSTOM production root (Docker/portable
#     installs where HERMES_HOME is not ~/.hermes) into the guard's
#     deny-list, mirroring the kanban deny-list capture above.
# The guard itself is env-activated (PYTEST_CURRENT_TEST / PYTEST_VERSION),
# so subprocess children that import hermes_state directly are covered even
# without this fixture.


@pytest.fixture(autouse=True)
def _state_db_write_guard(request, monkeypatch):
    _hs = sys.modules.get("hermes_state")
    if _hs is None or not hasattr(_hs, "_STATE_DB_GUARD_BYPASS"):
        yield
        return
    if request.node.get_closest_marker("live_system_guard_bypass") is not None:
        monkeypatch.setattr(_hs, "_STATE_DB_GUARD_BYPASS", True)
        yield
        return
    extra_roots = []
    if _PRE_SANDBOX_HERMES_HOME and not _hermes_home_points_at_production(
        _PRE_SANDBOX_HERMES_HOME
    ):
        extra_roots.append(
            Path(_PRE_SANDBOX_HERMES_HOME).expanduser().resolve()
        )
    monkeypatch.setattr(
        _hs, "_STATE_DB_GUARD_EXTRA_DENY_ROOTS", tuple(extra_roots)
    )
    yield


# ── Module-level state reset — replaced by per-file process isolation ───────
#
# ``scripts/run_tests_parallel.py`` runs each test FILE in its own freshly
# spawned pytest subprocess, so heavy co-scheduling pollution (module-level
# dicts / sets / ContextVars shared by many files) cannot cross file
# boundaries at all. Within a single file, ordering is the author's
# responsibility. If your tests in the same file share mutable state, either
# reset it explicitly in a fixture or split them across files.
#
# The skill ``test-suite-cascade-diagnosis`` documents the cascade patterns
# this replaces; the running example was ``test_command_guards`` failing
# 12/15 CI runs because ``tools.approval._session_approved`` carried
# approvals from one test's session into another's.


# ── tui_gateway.server shared-module state isolation ───────────────────────
#
# ``tui_gateway.server`` registers its RPC handlers in a module-level
# ``_methods`` dict at import time and keeps per-session state in module
# globals (sessions, child-run registry, config cache, DB handle). The
# canonical per-file process isolation above hides any leakage, but a direct
# multi-file invocation (``pytest tests/tui_gateway/ tests/tui_gateway/test_tui_gateway_server.py``,
# or plain ``pytest tests/``) shares one interpreter: a test that stubs
# ``_methods["slash.exec"]`` or leaves an active-session lease behind breaks
# unrelated tests in later files. This fixture snapshots the cheap-to-copy
# globals before each test and restores them after, so any file combination
# is order-independent. It is a near no-op (one sys.modules lookup) while
# the module has not been imported.
#
# The case this cannot cover — the module is first imported *during* a test
# that also mutates ``_methods`` — is handled by the importing files' own
# ``server`` fixtures (tests/tui_gateway/test_protocol.py and friends), which
# snapshot immediately after the import.

_TUI_SERVER_MODULE = "tui_gateway.server"


def _teardown_tui_server_sessions(mod) -> None:
    """Close leftover sessions through the production teardown boundary.

    Besides returning active-session leases, this finalizes the session,
    unregisters notification state, and closes its agent and slash worker.
    """
    sessions = getattr(mod, "_sessions", None)
    if not isinstance(sessions, dict):
        return
    for sid in list(sessions):
        mod._close_session_by_id(sid, end_reason="test_cleanup")


@pytest.fixture(autouse=True)
def _reset_tui_gateway_server_state():
    mod = sys.modules.get(_TUI_SERVER_MODULE)
    snapshot = None
    if mod is not None:
        snapshot = {
            "methods": dict(mod._methods),
            "cfg": (mod._cfg_cache, mod._cfg_sig, mod._cfg_path),
            "db": (mod._db, mod._db_error),
            "real_stdout": mod._real_stdout,
        }

    yield

    mod = sys.modules.get(_TUI_SERVER_MODULE)
    if mod is None:
        return

    # This finalizer can run before the test's own monkeypatch undo, so a
    # global may still be replaced with a non-dict test double — skip those
    # (monkeypatch restores the real, pre-test object afterwards anyway).
    sessions = mod._sessions
    if isinstance(sessions, dict):
        _teardown_tui_server_sessions(mod)
    for name in (
        "_pending",
        "_pending_prompt_payloads",
        "_answers",
        "_child_mirrors",
        "_active_child_runs",
    ):
        obj = getattr(mod, name, None)
        if isinstance(obj, dict):
            obj.clear()

    if snapshot is not None:
        mod._methods.clear()
        mod._methods.update(snapshot["methods"])
        mod._cfg_cache, mod._cfg_sig, mod._cfg_path = snapshot["cfg"]
        mod._db, mod._db_error = snapshot["db"]
        mod._real_stdout = snapshot["real_stdout"]
    else:
        # First imported during this test — reset to import-time defaults
        # for the globals we could not snapshot (``_methods`` is left to
        # the importing file's fixture, see block comment above).
        mod._cfg_cache = None
        mod._cfg_sig = None
        mod._cfg_path = None
        mod._db = None
        mod._db_error = None

    # A leaked context-local Hermes home override redirects every later
    # ``get_hermes_home()`` call (active-session registry, config paths)
    # to a stale per-test tmpdir. Force the main-thread ContextVar back
    # to its default.
    try:
        from hermes_constants import get_hermes_home_override, set_hermes_home_override

        if get_hermes_home_override() is not None:
            set_hermes_home_override(None)
    except Exception:
        pass


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal hermes config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Per-test timeout — handled by the isolation plugin ─────────────────────
#
# The subprocess-per-test plugin enforces the configured ``isolate_timeout``
# ini key by terminating the child if it overruns. The old SIGALRM-based
# fixture (POSIX-only, didn't work on Windows) is gone.


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.

    On Python 3.12+, ``asyncio.get_event_loop_policy().get_event_loop()`` with no
    *running* loop emits DeprecationWarning; skip that path and install a fresh
    loop via ``new_event_loop()`` instead.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if loop is None and sys.version_info < (3, 12):
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


_REQUIRES_WAL_MARK = "requires_wal"


def _wal_is_usable() -> bool:
    """True when Hermes will actually put a database into WAL mode here.

    Hermes refuses journal_mode=WAL on SQLite builds carrying the upstream
    WAL-reset corruption bug (3.7.0–3.51.2, excluding backports 3.50.7 /
    3.44.6) and falls back to DELETE. On such a build NO ``-wal`` sidecar is
    ever created, so a test asserting on WAL frames, ``-wal`` file size, or
    checkpoint behaviour cannot pass — it is testing a mode the runtime
    declined to enable, not a regression.

    This matters because the interpreter running the tests and the interpreter
    running Hermes can link DIFFERENT SQLite versions: a repo ``.venv`` on
    3.50.4 (vulnerable → DELETE) alongside a Hermes managed runtime on 3.53.1
    (fixed → WAL). The same test then passes in one and fails in the other.

    IMPORTANT: this must NOT import ``hermes_state``. That module computes
    ``DEFAULT_DB_PATH`` from ``get_hermes_home()`` at import time, so importing
    it during collection — before the per-test ``_isolate_hermes_home`` fixture
    redirects ``HERMES_HOME`` — permanently caches the DEVELOPER'S REAL
    ``~/.hermes/state.db`` for the whole session. Tests then read live
    production sessions instead of a tempdir. The version predicate is
    duplicated from ``hermes_state._is_sqlite_wal_reset_vulnerable`` (upstream
    fixed ranges, stable) rather than imported, and
    ``test_conftest_wal_gate.py`` pins the two implementations in agreement.
    """
    info = sqlite3.sqlite_version_info
    if info < (3, 7, 0):
        return True  # pre-WAL library: cannot hit the race
    if info >= (3, 51, 3):
        return True  # fixed upstream
    if (3, 50, 7) <= info < (3, 51, 0):
        return True  # 3.50.x backport
    if (3, 44, 6) <= info < (3, 45, 0):
        return True  # 3.44.x backport
    return False


# ── Audio-playback guard ───────────────────────────────────────────────────
#
# Same class of incident as the live-system guard (``tests/_fixtures/live_system_guard.py``),
# different primitive:
# a test run spoke the string "partial answer complete" out of the developer's
# speakers. That string is a test fixture
# (``tests/tui_gateway/test_tui_gateway_server.py``'s fake ``final_response``), and the
# route it took is fully in-process — no leaked shell variable required:
#
#   1. ``test_voice_toggle_tts_branch_also_carries_record_key`` drives the
#      ``voice.toggle`` RPC with ``action="tts"``. The handler
#      (``tui_gateway/server.py``) flips the flag by writing the *real*
#      process environment: ``os.environ["HERMES_VOICE_TTS"] = "1"``. The
#      test's ``monkeypatch.delenv(..., raising=False)`` records no undo entry
#      (pytest only records an undo when the key was present), so the "1"
#      survives teardown and persists for the rest of the pytest process.
#   2. Any later test in that process that drives a turn to completion hits
#      the TTS dispatch in ``prompt.submit``, which checks
#      ``_voice_tts_enabled()`` — now true — and fires
#      ``hermes_cli.voice.speak_text(final_response)`` on a daemon thread.
#   3. ``speak_text`` needs no API key to be audible: ``tools/tts_tool.py``
#      defaults to the ``edge`` provider, which is keyless.
#
# Because the flag is set from *inside* the process, ``scripts/run_tests.sh``'s
# ``env -i`` does not help, and neither does env-blanking on its own — the
# hermetic fixture blanks at test setup, and step 1 re-sets it mid-test. So we
# also intercept the primitive that does the damage, exactly as the
# live-system guard intercepts ``os.kill`` rather than trusting every caller
# to mock it:
#
#  • ``hermes_cli.voice.speak_text`` — the synth+playback entry point both
#    gateway call sites late-import, so patching the module attribute catches
#    them wherever they import it from.
#  • ``hermes_cli.voice.play_audio_file`` — the module-level binding
#    ``speak_text`` actually plays through. Patching the binding inside
#    ``hermes_cli.voice`` (not ``tools.voice_mode``) keeps the real function
#    available to the tests that legitimately exercise it with a mocked
#    audio backend (``tests/tools/test_voice_mode.py``).
#
# Config cannot re-open this hole: the ``tts:`` section of ``config.yaml``
# only selects *which* provider speaks, never *whether* to speak — that gate
# is the env var alone.

_AUDIO_GUARD_BYPASS_MARK = "real_audio_playback"
_ALLOW_MACOS_KEYCHAIN_MARK = "allow_macos_keychain"


def _relocate_basetemp_outside_operator_home(config) -> None:
    """Move pytest's basetemp out of the operator's platform-native Hermes home.

    Every per-test sandbox is ``<basetemp>/.../hermes_test``. ``get_default_hermes_root()``
    prefers the platform-native home whenever ``HERMES_HOME`` sits *under* it, so a basetemp
    inside ``~/.hermes`` (or ``%LOCALAPPDATA%\\hermes``, where ``TEMP`` commonly lives on
    Windows) turns the sandbox back into the live install and ``get_profile_dir("default")``
    writes fixtures over the operator's config.yaml / .env / MEMORY.md (#111101).
    """
    from hermes_constants import _get_platform_default_hermes_home

    native = _get_platform_default_hermes_home().resolve()
    factory = config._tmp_path_factory
    given = factory._given_basetemp
    candidate = given if given is not None else Path(
        os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()
    )
    if not candidate.resolve().is_relative_to(native):
        return
    # The system temp dir may itself be inside the home (Windows TEMP under the
    # Hermes home). The repo is no escape either: the default install checks it
    # out *inside* the home (~/.hermes/hermes-agent). The relocated basetemp goes
    # into ONE prunable root outside the home, never loose into the operator's
    # $HOME (123 ``hermes-pytest-basetemp-*`` dirs piled up there in a day, one per
    # test file the per-file runner spawned). It is removed when this pytest exits
    # and, for runs that were killed before that, swept once it is 24h idle.
    safe = Path(tempfile.mkdtemp(prefix="b-", dir=_pytest_disk_temp_root(native)))
    assert not safe.resolve().is_relative_to(native), (
        f"pytest basetemp {safe} still resolves inside the operator's Hermes home {native}; "
        "refusing to run the suite against the live install (pass --basetemp outside it)"
    )
    factory._given_basetemp = safe
    config.option.basetemp = str(safe)
    config._hermes_relocated_basetemp = safe


def _pytest_disk_temp_root(native: Path) -> Path:
    """The root for relocated basetemps: the disk-backed runner root when the host has
    one (``scripts/run_tests_parallel.py::_runner_scratch_root``), else a plain (not
    dot-prefixed — hidden-dir search tests would see every fixture as hidden) sibling of
    the native home. Entries idle for a day are swept on the way in."""
    from hermes_constants_scratch import prune_idle_entries

    if os.name != "nt" and os.path.isdir("/var/tmp"):  # no-tmp: ok — disk-backed FHS root
        root = Path("/var/tmp/hermes-pytest")  # no-tmp: ok — /var/tmp is disk-backed by FHS, never tmpfs
    else:
        root = native.parent / "hermes-pytest"
    root.mkdir(parents=True, exist_ok=True)
    prune_idle_entries(root, 24, frozenset())
    return root


def _remove_relocated_basetemp(config) -> None:
    safe = getattr(config, "_hermes_relocated_basetemp", None)
    if safe is not None:
        shutil.rmtree(safe, ignore_errors=True)


def _pinned_mcp_sdk_version() -> str:
    """The ``mcp==X`` pin carried by the ``[mcp]`` extra in pyproject.toml."""
    import tomllib

    with open(Path(__file__).resolve().parent.parent / "pyproject.toml", "rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]
    for req in extras["mcp"]:
        if req.startswith("mcp=="):
            return req.split("==", 1)[1].strip()
    raise RuntimeError("pyproject.toml [mcp] extra no longer pins mcp==X")


@pytest.fixture
def require_mcp_2_sdk():
    """Skip tests that pin mcp 2.0-only behaviour when an older SDK is installed.

    The runtime deliberately supports both SDK generations (the dual streamable-client probe in
    mcp_tool), so a stale ``mcp`` distribution imports fine and presence-only guards let these
    tests through — where they fail later with opaque SDK errors. Compare the installed
    distribution against the pin so the outcome is an explicit skip with an actionable reason.
    """
    from importlib.metadata import PackageNotFoundError, version as dist_version

    from packaging.version import Version

    pinned = _pinned_mcp_sdk_version()
    try:
        found = dist_version("mcp")
    except PackageNotFoundError:
        pytest.skip(f"requires mcp=={pinned} (not installed); install the [mcp] extra")
    if Version(found) < Version(pinned):
        pytest.skip(f"requires mcp=={pinned} (found {found}); install the [mcp] extra")


def pytest_unconfigure(config):  # noqa: D401 — pytest hook
    _remove_relocated_basetemp(config)


@pytest.hookimpl(trylast=True)  # after _pytest.tmpdir has built config._tmp_path_factory
def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register markers used by hermetic conftest, and raise the soft open-file
    limit so a SINGLE-PROCESS run of the whole suite doesn't exhaust FDs.

    macOS defaults RLIMIT_NOFILE to 256. The per-file CI runner
    (``run_tests_parallel.py``) never hits this — each file gets a fresh
    interpreter. But running the suite (or a whole subsystem like
    ``tests/gateway/``) in one process accumulates open handles across thousands
    of tests and dies with ``OSError: [Errno 24] Too many open files`` — which
    masquerades as thousands of unrelated test errors. Raising the *soft* limit
    toward the (effectively unlimited) hard limit makes single-process /
    pollution-sweep runs work, without changing any test behaviour or hiding a
    real per-file leak (each CI subprocess still starts at the OS default).
    """
    _relocate_basetemp_outside_operator_home(config)
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        # resource is POSIX-only; a setrlimit refusal is non-fatal — the suite
        # still runs per-file under CI where the default limit suffices.
        pass

    config.addinivalue_line(
        "markers",
        "allow_sys_modules_purge: test intentionally leaves sys.modules purged "
        "(opts out of the sys.modules leak gate — say why in a comment)",
    )
    config.addinivalue_line(
        "markers",
        f"{_LIVE_SYSTEM_GUARD_BYPASS_MARK}: bypass the live-system guard "
        "(only for tests that genuinely need real os.kill / subprocess "
        "behaviour — e.g. PTY tests that signal their own child).",
    )
    config.addinivalue_line(
        "markers", "allow_real_home_io: explicitly bypass the test-only home I/O guard."
    )
    config.addinivalue_line(
        "markers", "real_release_channels: keep the real R2 channel reader (no local source-branch stub)."
    )
    config.addinivalue_line(
        "markers",
        f"{_GATEWAY_LOOKALIKE_MARK}: the test spawns and reaps its own stub "
        "child whose argv matches the gateway runtime matcher; only the "
        "real-gateway spawn check is lifted, os.kill stays guarded.",
    )
    config.addinivalue_line(
        "markers",
        "real_safe_directory: run the real `git config --get-all safe.directory` pre-read in "
        "noninteractive_git_env() (autouse fixture otherwise stubs it to no entries).",
    )
    config.addinivalue_line(
        "markers",
        f"{_REQUIRES_WAL_MARK}: test needs the runtime to actually enable "
        "SQLite WAL mode; skipped on builds where Hermes falls back to "
        "journal_mode=DELETE for the WAL-reset bug.",
    )
    config.addinivalue_line(
        "markers",
        f"{_AUDIO_GUARD_BYPASS_MARK}: bypass the audio-playback guard (only "
        "for tests that genuinely need real TTS synthesis and speaker "
        "playback — there are none in the default suite).",
    )
    config.addinivalue_line(
        "markers",
        "platforms(*specs, arch=None, arch_negate=False): run only on hosts "
        "matching at least one spec — linux/macos/windows/posix/any, "
        "'not X' negation, optional arch filter (e.g. arch='arm64')",
    )
    config.addinivalue_line(
        "markers",
        "platforms(*specs, arch=None, arch_negate=False): run only on hosts "
        "matching at least one spec — linux/macos/windows/posix/any, "
        "'not X' negation, optional arch filter (e.g. arch='arm64')",
    )
    config.addinivalue_line(
        "markers",
        f"{_ALLOW_MACOS_KEYCHAIN_MARK}: allow a test to exercise the macOS "
        "Keychain credential reader with its own subprocess/platform mocks.",
    )
    config.addinivalue_line(
        "markers",
        "require_symlinks: skip the test if symbolic links cannot be "
        "created in the current environment (needs admin/developer mode "
        "on Windows).",
    )
    config.addinivalue_line(
        "markers",
        "real_codex_context_policy: bypass the autouse fixture that pins "
        "model.codex_context_policy to 'advertised' — only for tests that "
        "exercise the knob (default 'large', config read, large behaviour).",
    )
    config.addinivalue_line(
        "markers",
        "real_memory_guard: bypass the autouse fixture that pins the kanban "
        "dispatcher's memory guard to 'no data' — only for tests that "
        "exercise the guard itself with their own patched samples.",
    )
    # NOTE: platforms("linux") / platforms("macos") / platforms("windows") are declared in
    # pyproject.toml's ``markers`` list, not here — they are part of the
    # project's public marker vocabulary (``pytest --markers``, and the CI
    # lanes select on them), whereas the marks above are conftest-internal
    # guards. Declaring them in both places just meant two descriptions that
    # could drift apart.

    # The pyproject addopts pin ``--timeout-method=signal`` relies on
    # ``signal.SIGALRM``, which does not exist on Windows — pytest-timeout
    # raises AttributeError at timer setup and the whole run aborts before any
    # test executes. Fall back to the thread-based timer on Windows so the
    # suite runs natively there (POSIX keeps the more reliable signal method).
    if sys.platform == "win32" and getattr(config.option, "timeout_method", None) == "signal":
        config.option.timeout_method = "thread"


_symlink_supported_cache = None


def _check_symlink_support() -> bool:
    global _symlink_supported_cache
    if _symlink_supported_cache is not None:
        return _symlink_supported_cache

    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            src.touch()
            lnk = Path(d) / "lnk"
            lnk.symlink_to(src)
            _symlink_supported_cache = True
            return True
    except OSError:
        _symlink_supported_cache = False
        return False


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item):
    """Join the turn's auto-title threads INSIDE capture, before pytest snaps it.

    A title thread that prints its failure warning while capture's
    ``readouterr`` swaps the fd crashed the interpreter (SIGSEGV in
    ``_pytest/capture.py::snap``). The teardown join in
    ``_close_leaked_session_dbs`` runs after that snap, too late for this race.
    """
    try:
        return (yield)
    finally:
        wait = getattr(sys.modules.get("agent.title_generator"), "wait_for_title_upgrades", None)
        if wait is not None:
            wait()


def pytest_runtest_setup(item):
    if item.get_closest_marker("require_symlinks"):
        if not _check_symlink_support():
            pytest.skip(
                "Environment does not support symbolic links "
                "(requires admin/developer mode on Windows)"
            )


def pytest_collection_modifyitems(config, items):  # noqa: D401 — pytest hook
    """Apply host-OS gating, then skip ``requires_wal`` where WAL is unusable.

    OS gating: a test marked ``platforms(...)`` runs only on hosts its
    specs match. See the block comment in ``tests/_fixtures/platform_gating.py``
    for why these tests are skipped rather than run against a patched ``sys.platform``.

    WAL gating is cheaper and more honest than each test hand-rolling a
    version check: the reason string names the actual linked version so the
    skip is diagnosable rather than mysterious.
    """
    _reject_contradictory_platform_marks(items)

    # platforms() gating: skip items whose specs exclude this host. The skip
    # markers (not -m expressions) are the authoritative host filter on
    # every lane, so a lane selects with plain ``-m platforms`` and lets the
    # specs decide per-test.
    for item in items:
        reason = _platforms_gate_reason(item)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))

    if _wal_is_usable():
        return

    reason = (
        f"SQLite {sqlite3.sqlite_version} has the WAL-reset bug — Hermes uses "
        "journal_mode=DELETE here, so no -wal sidecar exists to assert on"
    )
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker(_REQUIRES_WAL_MARK) is not None:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _audio_playback_guard(request, monkeypatch):
    """Stub TTS synthesis + speaker playback for every test.

    See the block comment above for the incident this closes. Defence in
    depth behind ``_HERMES_BEHAVIORAL_VARS``: the env blanking stops the flag
    leaking *between* tests, this stops the speakers ever opening even when a
    test sets the flag *itself* (which the ``voice.toggle`` RPC handler does,
    by writing ``os.environ`` directly).

    Deliberately silent rather than raising: unlike a stray ``os.kill``, a
    stray ``speak_text`` is dispatched on a daemon thread whose exception
    nobody would ever see, so a hard failure would neither stop the test nor
    surface. Silence is the whole point. Tests that genuinely want real audio
    can opt out with ``@pytest.mark.real_audio_playback``.
    """
    if request.node.get_closest_marker(_AUDIO_GUARD_BYPASS_MARK):
        yield
        return

    try:
        import hermes_cli.voice as _voice
    except Exception:
        # Optional audio deps missing — nothing importable to speak with.
        yield
        return

    def _blocked_speak_text(text, *args, **kwargs):
        return None

    def _blocked_play_audio_file(path, *args, **kwargs):
        return False

    if hasattr(_voice, "speak_text"):
        monkeypatch.setattr(_voice, "speak_text", _blocked_speak_text)
    if hasattr(_voice, "play_audio_file"):
        monkeypatch.setattr(_voice, "play_audio_file", _blocked_play_audio_file)

    yield


@pytest.fixture(autouse=True)
def _isolate_computer_use_approval_state():
    """Reset the computer-use explicit approval callback after every test.

    ``tools.computer_use.tool._approval_callback`` is a module-global handed to
    the shared approval gate as its explicit callback, where it takes precedence
    over the per-thread terminal one. A test that installs it and does not
    reset it poisons every later computer-use test in the same process: a
    leaked callback that raises becomes a deny, a leaked one that blocks (the
    real CLI one waits on an answer queue) hangs the whole single-process run.
    Both symptoms are order-dependent. Teardown-only, so tests that install
    their own callback keep it for their own duration.
    """
    yield
    try:
        # Only a module some test imported can hold leaked approval state.
        _cu_tool = sys.modules.get("tools.computer_use.tool")
        if _cu_tool is None:
            return
        _cu_tool.set_approval_callback(None)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _moa_caches_isolated():
    """Clear module-level MoA cold-start caches before each test.

    ``agent.moa_loop`` caches the resolved preset and each slot's provider
    runtime at module level (keyed on config mtime / provider+model) so the
    tool loop doesn't re-resolve them serially on every iteration. Tests
    monkeypatch resolvers and config paths, so a cache entry leaked from one
    test would poison the next. Clear both around every test.
    """
    # A module that is not imported has empty caches; one a test imports
    # mid-run is cleared at teardown (t_24f73ced: skip the import cost).
    moa = sys.modules.get("agent.moa_loop")
    if moa is not None:
        moa._preset_cache.clear()
        moa._runtime_cache.clear()
    yield
    moa = sys.modules.get("agent.moa_loop")
    if moa is not None:
        moa._preset_cache.clear()
        moa._runtime_cache.clear()


@pytest.fixture(autouse=True)
def _kanban_stubbed_liveness_implies_identity(monkeypatch):
    """A stubbed ``kanban_db._pid_alive`` also vouches for owner identity.

    t_0ae83825: termination/liveness paths now require a live PID to have been
    created inside its run's causal window (``_pid_started_in_claim``). Tests
    that stub ``_pid_alive`` over a SYNTHETIC pid (12345, 999_998, ...) are
    asserting "this is our live worker"; the real create-time probe cannot
    read a synthetic pid and would fail closed (``unverified``). While
    ``_pid_alive`` is stubbed, identity therefore follows the stub. With the
    real ``_pid_alive`` the real identity probe runs untouched, and a test that
    patches ``_pid_started_in_claim`` itself still wins.
    """
    kb = sys.modules.get("hermes_cli.kanban_db")
    if kb is None or not hasattr(kb, "_real_pid_started_in_claim"):
        yield
        return
    real_alive = kb._pid_alive
    real_started = kb._real_pid_started_in_claim

    def _started(pid, claimed_at, spawned_at, start_token=None):
        if kb._pid_alive is not real_alive:
            return True
        return real_started(pid, claimed_at, spawned_at, start_token)

    monkeypatch.setattr(kb, "_pid_started_in_claim", _started)
    yield


# ── Real-home tripwire (universal read/write guard) ──────────────────────────
# The hermetic sandbox redirects get_hermes_home(), but TWO escape classes
# remain: (a) code hardcoding Path.home()/".hermes" (the exact restatement
# class AGENTS.md bans — the Path.home()/.hermes/profiles bug the 2026-09-03
# deployment review caught in pm/plugins_state.py), and (b) imports freezing
# real-home paths before fixtures run. The kanban guard (#69283) covers one
# subsystem; this covers EVERY file operation: any open()/mkdir/stat-family
# call resolving under the REAL hermes root fails the test immediately
# with a message naming the path — reads AND writes (a read of production
# state is as much a leak as a write: it drags fixture rows and real config
# into test assertions).
#
# The real root is captured at conftest import (pre-sandbox), honoring a
# genuinely-custom pre-set HERMES_HOME exactly like the kanban deny-list
# (_hermes_home_points_at_production governs which values count).
_REAL_HERMES_ROOT_CANDIDATES: list[Path] = []


def _capture_real_hermes_root() -> list[Path]:
    """The real root(s) to refuse: the default ~/.hermes plus a pre-sandbox
    custom HERMES_HOME when one was set. Both are guarded — the default
    because hardcoded restatements hit it; the custom one because
    deployment-shaped tests (Docker /opt/data) must not touch the operator's
    real custom root either."""
    import platform

    roots: list[Path] = []
    try:
        default_root = (Path.home() / ".hermes").resolve()
        roots.append(default_root)
    except Exception:
        pass
    # native-Windows default: %LOCALAPPDATA%\hermes (get_hermes_home's
    # platform-native path) — guard it too
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        try:
            win_root = (Path(localappdata) / "hermes").resolve()
            if win_root not in roots:
                roots.append(win_root)
        except Exception:
            pass
    if _PRE_SANDBOX_HERMES_HOME and not _hermes_home_points_at_production(
        _PRE_SANDBOX_HERMES_HOME
    ):
        try:
            custom = Path(_PRE_SANDBOX_HERMES_HOME).expanduser().resolve()
            # The live session sandbox is test-owned, never a guarded root
            # (a re-imported conftest body sees it as _PRE_SANDBOX_HERMES_HOME).
            sandbox = os.environ.get("HERMES_TEST_SANDBOX_HOME", "")
            if sandbox and custom == Path(sandbox).expanduser().resolve():
                return roots
            if custom not in roots:
                roots.append(custom)
        except Exception:
            pass
    return roots


_REAL_HERMES_ROOT_CANDIDATES = _capture_real_hermes_root()
# Captured before any test can patch sys.platform, HOME or XDG_*: a test that runs the real
# GUI uninstall or update swap would otherwise delete the developer's own Hermes app. Only the
# ones present (none on CI runners, so the guard costs nothing there), each literal and resolved.
from hermes_cli.gui_uninstall import packaged_gui_app_paths  # noqa: E402

_REAL_INSTALLED_GUI_APPS = sorted({
    os.path.normcase(form) for app in packaged_gui_app_paths() if os.path.lexists(app)
    for form in (os.path.abspath(app), os.path.realpath(app))})


@pytest.fixture(autouse=True)
def _forbid_real_hermes_home_io(monkeypatch, request):
    """Guard Python file/metadata/deletion calls and SQLite against real state.

    Native libraries and subprocesses still need their own temporary-home
    contracts. The opt-out is for explicit guard tests, never implicit repair.
    """
    if request.node.get_closest_marker("allow_real_home_io"):
        return
    from tests.home_io_guard import HomeIOGuard

    HomeIOGuard(lambda: _REAL_HERMES_ROOT_CANDIDATES, lambda: _REAL_INSTALLED_GUI_APPS).install(monkeypatch)


@pytest.fixture
def real_bash() -> str:
    """A bash that runs shell scripts: on the Windows runners PATH resolves ``bash`` to
    System32's WSL launcher, which prints a UTF-16 "no installed distributions" notice and
    exits 1. Prefer Git for Windows' bash there; elsewhere the PATH one is real."""
    found = shutil.which("bash")
    if sys.platform == "win32" and (
            not found or any(marker in found.lower() for marker in ("system32", "windowsapps"))):
        for rel in (("Git", "bin", "bash.exe"), ("Git", "usr", "bin", "bash.exe")):
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")).joinpath(*rel)
            if candidate.exists():
                return str(candidate)
    return found or "bash"
