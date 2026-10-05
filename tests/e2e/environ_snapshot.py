"""A consistent copy of ``os.environ`` that is safe off the main thread.

e2e suites drive child processes from pool threads (module-scoped fixtures) while
the main thread keeps running tests, and every test's autouse hermetic fixture
sets and deletes env vars (``HERMES_HONCHO_HOST`` and friends). Iterating
``os.environ`` (its ``.items()``, a ``**`` splat, a ``dict()`` copy) reads the
key list first and each value afterwards, so a concurrent delete raises
``KeyError`` and a concurrent insert ``RuntimeError: dictionary changed size``.
Both are retried here; a retry only happens on an actual race.
"""

from __future__ import annotations

import os


def environ_snapshot() -> dict[str, str]:
    while True:
        try:
            return dict(os.environ)
        except (KeyError, RuntimeError):
            continue
