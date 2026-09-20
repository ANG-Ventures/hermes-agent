"""The single definition of "is this process a test run".

Kept as a LEAF module — stdlib plus an optional ``psutil`` and nothing else — so any
module that needs the answer can import it without dragging in a dependency graph.
That matters concretely: ``hermes_state`` transitively imports ``agent.redact``, which
snapshots its enable-toggle at import time, so a module that reached the predicate
*through* ``hermes_state`` would pull that snapshot forward and freeze the toggle
before the config bridge had run.

Extracted verbatim from ``hermes_state`` (behaviour unchanged); ``hermes_state``
re-exports these names, so existing callers and tests are unaffected.
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional

try:  # Hard dependency in practice, but tolerate scaffold-phase imports.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]


#: Env marker exported by the hermetic test conftest at the same moment it
#: redirects ``HERMES_HOME`` to the per-session tmp isolation root.  Its
#: value is that isolation root.  Unlike ``PYTEST_*`` (owned by pytest, and
#: routinely scrubbed by tests that rebuild a child environment), this marker
#: is OURS: it declares "this process tree is running under Hermes test
#: isolation", and it inherits into subprocess children by default — so a
#: child that received the patched ``HERMES_HOME`` also received the marker,
#: and a child that resolves a production DB while carrying it is, by
#: definition, an isolation escape (#82770).
_TEST_ISOLATION_MARKER_ENV = "HERMES_TEST_ISOLATION"


def _running_under_pytest() -> bool:
    """True when this process (or a parent test process) is a pytest run."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("PYTEST_VERSION")
        or os.environ.get(_TEST_ISOLATION_MARKER_ENV)
    )


#: Names that identify a pytest launcher in a process command line.  Matched
#: against the *basename* of each argv token so ``/tmp/pytest-of-dev/...``
#: paths — which do show up in real argv — cannot false-positive.
_PYTEST_LAUNCHER_NAMES = frozenset(
    {"pytest", "py.test", "pytest.exe", "py.test.exe"}
)

#: Memoised ancestry answer.  The process tree above us does not change in a
#: way that matters here, and the walk must not cost anything on the hot path.
_PYTEST_ANCESTOR: Optional[bool] = None


def _process_looks_like_pytest(proc: Any) -> bool:
    """True when *proc*'s command line is a pytest invocation.

    Covers both ``pytest ...`` (launcher on argv[0]) and ``python -m pytest``
    (launcher as a bare ``pytest`` token).  A process whose command line we
    cannot read is treated as "not pytest": guessing the other way would
    refuse production opens for unrelated reasons.
    """
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        return False
    for arg in cmdline:
        try:
            token = str(arg).strip('"').strip("'")
            # Split on both separators on every host: os.path.basename is
            # POSIX-only under Linux and would leave a Windows-style path
            # intact, making the matcher's answer depend on the platform.
            name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        except Exception:
            continue
        if name in _PYTEST_LAUNCHER_NAMES:
            return True
    return False


def _has_pytest_ancestor() -> bool:
    """True when some ancestor process of this one is a pytest run.

    ``_running_under_pytest`` reads ``PYTEST_*`` env vars, which a child
    spawned with a rebuilt environment loses at the same moment it loses the
    ``HERMES_HOME`` redirect: that child aims at the production DB *and*
    disarms the guard in one step (#82770).  Ancestry is the one test-context
    signal that survives an env rebuild, so it backs the env check up.

    Fails open (``False``) when ``psutil`` is unavailable or the walk errors —
    that restores the previous env-only behaviour rather than blocking real
    user runs on a psutil hiccup.
    """
    global _PYTEST_ANCESTOR
    if _PYTEST_ANCESTOR is not None:
        return _PYTEST_ANCESTOR
    found = False
    if psutil is not None:
        try:
            for parent in psutil.Process().parents():
                if _process_looks_like_pytest(parent):
                    found = True
                    break
        except Exception:
            found = False
    _PYTEST_ANCESTOR = found
    return found


def _in_test_context() -> bool:
    """True when this process is a test run, by environment or by ancestry.

    Order matters for cost: the env probe is two dict lookups and covers the
    common in-process case, so the ancestry walk only runs for processes the
    environment claims are ordinary user runs — and its answer is memoised,
    so a real ``hermes`` invocation pays for at most one walk.
    """
    if _running_under_pytest():
        return True
    return _has_pytest_ancestor()
