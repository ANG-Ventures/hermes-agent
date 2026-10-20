"""The suite pins TOKIO_WORKER_THREADS=4 before any Relay import (conftest invariant 5, 2026-10-19): on a 256-thread
host the per-CPU Tokio pool made Relay initialize() hang inside tests. RED with the conftest line removed."""
import os


def test_tokio_worker_threads_is_bounded_by_default():
    assert os.environ.get("TOKIO_WORKER_THREADS") == "4"


def test_explicit_caller_value_wins(monkeypatch):
    # setdefault semantics: a lane that sets its own value is not overridden by the suite
    monkeypatch.setenv("TOKIO_WORKER_THREADS", "8")
    import importlib, tests.conftest as c  # noqa: F401  (re-import is a no-op; the env must stay the caller's)
    importlib.reload(c)
    assert os.environ["TOKIO_WORKER_THREADS"] == "8"
