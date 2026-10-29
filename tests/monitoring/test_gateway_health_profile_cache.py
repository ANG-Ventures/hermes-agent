"""The gateway health emitter must not resolve the profile path on every runtime-status write.

get_active_profile_name() resolve()s three paths; it ran on the gateway event loop under every
heartbeat and stalled it for 10 s on a busy disk (Apollo, 2026-10-26).
"""
from __future__ import annotations

import pytest

import hermes_cli.profiles as profiles
from agent.monitoring import gateway_health


@pytest.fixture
def calls(monkeypatch, tmp_path):
    cache = getattr(gateway_health, "_profile_cache", None)
    if cache is not None:
        monkeypatch.setattr(gateway_health, "_profile_cache", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
    seen: list[int] = []

    def fake() -> str:
        seen.append(1)
        return "aegis"

    monkeypatch.setattr(profiles, "get_active_profile_name", fake)
    monkeypatch.setattr(gateway_health.emitter, "emit", lambda ev: None)
    return seen


def test_same_home_resolves_profile_once_across_transitions(calls):
    cur = {"gateway_state": "running", "platforms": {"telegram": {"state": "connected"}}}
    gateway_health.emit_runtime_status_transition({"gateway_state": "starting"}, cur)
    gateway_health.emit_runtime_status_transition({"gateway_state": "starting"}, cur)
    assert gateway_health._safe_profile() == "aegis"
    assert len(calls) == 1


def test_changed_home_is_recomputed(calls, monkeypatch, tmp_path):
    assert gateway_health._safe_profile() == "aegis"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
    assert gateway_health._safe_profile() == "aegis"
    assert len(calls) == 2


def test_failure_fails_open_and_is_not_cached(calls, monkeypatch):
    state = {"raise": True}

    def flaky() -> str:
        calls.append(1)
        if state["raise"]:
            raise OSError("disk")
        return "aegis"

    monkeypatch.setattr(profiles, "get_active_profile_name", flaky)
    assert gateway_health._safe_profile() == "default"
    state["raise"] = False
    assert gateway_health._safe_profile() == "aegis"
