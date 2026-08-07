"""Regression guard: session ContextVars must be pristine at every test start.

``clear_session_vars`` intentionally leaves the ``_VAR_MAP`` contextvars bound
to ``""`` ("explicitly cleared" — suppresses the ``os.environ`` fallback in
``get_session_env``; correct production semantics). Contextvars are
process-global for the pytest main thread, so without the shared autouse
``_isolate_session_contextvars`` fixture (tests/conftest.py) any test file
that exercises set/clear leaks the ``""`` bindings into every later file of a
single-process run, shadowing ``monkeypatch.setenv("HERMES_SESSION_*", ...)``
identities (the test_kanban_session_attribution ->
test_kanban_tools::test_create_subscribes_gateway_session ordering failure;
the canonical per-file-subprocess runner and CI's sharding hid it).

Each test here asserts the pristine ``_UNSET`` state FIRST, then deliberately
poisons the context through the production set/clear path. Whichever test runs
second (any order) fails its start-assert if the isolation fixture is removed
— so this file alone mutation-proves the fixture in one process.
"""

import os

import gateway.session_context as sc


def _assert_pristine_start() -> None:
    """Every _VAR_MAP contextvar (and the async-delivery flag) is _UNSET."""
    leaked = {
        name: var.get()
        for name, var in sc._VAR_MAP.items()
        if var.get() is not sc._UNSET
    }
    assert not leaked, (
        "session ContextVars leaked into this test from an earlier test "
        f"(fixture _isolate_session_contextvars missing/broken?): {leaked!r}"
    )
    assert sc._SESSION_ASYNC_DELIVERY.get() is sc._UNSET


def _assert_environ_fallback_live(monkeypatch) -> None:
    """With _UNSET contextvars, get_session_env must consult os.environ —
    the exact resolution the leak broke downstream."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "guard-platform")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "guard-chat")
    assert sc.get_session_env("HERMES_SESSION_PLATFORM", "") == "guard-platform"
    assert sc.get_session_env("HERMES_SESSION_CHAT_ID", "") == "guard-chat"


def _poison_via_production_path() -> None:
    """Bind then clear the full session identity — the production sequence
    that leaves every string var holding "" in this context."""
    tokens = sc.set_session_vars(
        platform="leaky-platform",
        chat_id="leaky-chat",
        session_id="leaky-session",
        session_key="leaky-key",
        async_delivery=False,
    )
    sc.clear_session_vars(tokens)
    # Sanity: the poison is real — "" (not _UNSET) is now bound.
    assert sc._SESSION_PLATFORM.get() == ""
    assert sc.get_session_env("HERMES_SESSION_PLATFORM", "") == ""


def test_contextvars_pristine_then_poison_a(monkeypatch):
    _assert_pristine_start()
    _assert_environ_fallback_live(monkeypatch)
    _poison_via_production_path()


def test_contextvars_pristine_then_poison_b(monkeypatch):
    _assert_pristine_start()
    _assert_environ_fallback_live(monkeypatch)
    _poison_via_production_path()


def test_environ_writes_do_not_bind_contextvars(monkeypatch):
    """monkeypatch.setenv alone must leave the contextvars _UNSET — env-based
    identity (the worker/CLI path) rides the fallback, never the vars."""
    _assert_pristine_start()
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    assert sc._SESSION_PLATFORM.get() is sc._UNSET
    assert os.environ["HERMES_SESSION_PLATFORM"] == "telegram"
