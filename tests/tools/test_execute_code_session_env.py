"""execute_code children carry the same gateway session identity the terminal tool does.

Regression for t_be44b437: the kernel env had only ``HERMES_SESSION_ID``, so a
``hermes kanban create`` from execute_code had no ``HERMES_SESSION_PLATFORM`` /
``CHAT_ID``, never subscribed the gateway chat, and the card finished silently.
"""

from __future__ import annotations

import os
import sys

import pytest

from gateway.session_context import (
    _VAR_MAP,
    clear_session_vars,
    reset_session_vars,
    set_session_vars,
)
from tools.code_execution_env import _build_child_env
from tools.environments.local import _make_run_env

_BOUND = dict(
    platform="discord", source="gateway", chat_id="1554668201428918292", chat_type="group",
    chat_name="runpod-ai", thread_id="t1", user_id="u1", user_id_alt="u1alt", user_name="ace",
    scope_id="guild1", session_key="agent:main:discord:group:1554668201428918292",
    session_id="20261007_112633_f9beb5f8", message_id="m1", profile="apollo",
    ui_session_id="ui1", cron_session="", parent_chat_id="p1",
)


@pytest.fixture(autouse=True)
def _gateway_session(monkeypatch):
    for name in _VAR_MAP:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    reset_session_vars()
    tokens = set_session_vars(**_BOUND)
    yield
    clear_session_vars(tokens)
    reset_session_vars()


def _kernel_env() -> dict:
    return _build_child_env(rpc_endpoint="sock", rpc_token="tok", tmpdir="/tmp/hermes-test",
                            child_python=sys.executable)


def _session_names(env: dict) -> set:
    return {k for k in env if k in _VAR_MAP or k.startswith("HERMES_SESSION_")}


def test_kernel_sees_the_bound_gateway_identity():
    env = _kernel_env()
    assert env["HERMES_SESSION_PLATFORM"] == "discord"
    assert env["HERMES_SESSION_CHAT_ID"] == "1554668201428918292"
    assert env["HERMES_SESSION_ID"] == "20261007_112633_f9beb5f8"


def test_kernel_and_terminal_export_the_same_session_names_and_values():
    """One source of truth: whatever the terminal child gets, the kernel child gets."""
    terminal = _make_run_env({})
    kernel = _kernel_env()
    assert _session_names(kernel) == _session_names(terminal)
    assert {k: kernel[k] for k in _session_names(kernel)} == {
        k: terminal[k] for k in _session_names(terminal)}


def test_contextvar_wins_over_a_foreign_process_global(monkeypatch):
    """Inside the gateway os.environ is last-writer-wins across sessions: never copy it."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "FOREIGN-chat")
    env = _kernel_env()
    assert env["HERMES_SESSION_PLATFORM"] == "discord"
    assert env["HERMES_SESSION_CHAT_ID"] == "1554668201428918292"


def test_cleared_context_does_not_leak_the_process_global(monkeypatch):
    clear_session_vars(set_session_vars(platform="discord", chat_id="c", session_id="s",
                                        cron_session=""))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "FOREIGN-chat")
    env = _kernel_env()
    assert not env.get("HERMES_SESSION_PLATFORM")
    assert not env.get("HERMES_SESSION_CHAT_ID")


def test_remote_per_call_identity_matches_the_local_kernel():
    """The remote per-call twin ships an env file; it carries the same identity as the kernel,
    and every other session name as "" so an earlier session's value is overwritten."""
    from tools.code_execution_env import _session_identity_env
    kernel = _kernel_env()
    remote = _session_identity_env()
    assert set(remote) == set(_VAR_MAP)
    assert remote["HERMES_SESSION_PLATFORM"] == "discord"
    assert remote["HERMES_SESSION_CHAT_ID"] == "1554668201428918292"
    assert {k: v for k, v in remote.items() if v} == {
        k: kernel[k] for k in _session_names(kernel) if kernel[k] and k != "HERMES_SESSION_SCRATCH"}


def test_kernel_cell_applies_the_current_turns_identity(monkeypatch):
    """A kernel outlives its first turn: each cell carries the CURRENT identity and the runner
    applies it before exec (Prism r1 on #1818). Drives the real runner cell source."""
    import contextlib, io, json, os, traceback  # noqa: E401 — the runner source's own imports
    from tools.code_execution_env import _session_identity_env
    from tools.code_kernel import RUNNER_CELL_SOURCE
    ns = {"_CAPTURE_LIMIT": 100_000, "os": os, "io": io, "contextlib": contextlib,
          "traceback": traceback, "json": json}
    exec(RUNNER_CELL_SOURCE, ns)
    probe = "import os; print(os.environ.get('HERMES_SESSION_USER_ID', '<unset>'))"
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "first-turn-user")  # what the kernel spawned with

    payload, _ = ns["run_cell"]({"id": "1", "code": probe, "env": _session_identity_env()}, 1)
    assert payload["stdout"].strip() == "u1"

    tokens = set_session_vars(platform="discord", chat_id="1554668201428918292",
                              session_id="next-turn", cron_session="")
    try:
        payload, _ = ns["run_cell"]({"id": "2", "code": probe, "env": _session_identity_env()}, 2)
    finally:
        clear_session_vars(tokens)
    assert payload["stdout"].strip() == "<unset>"
