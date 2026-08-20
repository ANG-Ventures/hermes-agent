from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import shared_transport_guard as guard


@pytest.fixture(autouse=True)
def _reset_tailscale_cache():
    guard._reset_cache_for_tests()
    yield
    guard._reset_cache_for_tests()


@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("claude-apr", "http://127.0.0.1:18810/anthropic", True),
        ("claude-bpr", "http://127.0.0.1:18811/anthropic", True),
        ("custom", "http://100.64.0.1:18801/anthropic", True),
        ("custom", "http://100.127.255.254:18801/anthropic", True),
        ("custom", "http://100.63.255.255:18801/anthropic", False),
        ("custom", "http://100.128.0.1:18801/anthropic", False),
        ("custom", "https://api.anthropic.com", False),
        ("custom", "http://127.0.0.1:18801", False),
    ],
)
def test_route_uses_tailscale_only_for_known_relay_or_cgnat_tailnet(
    provider, base_url, expected
):
    assert guard.route_uses_tailscale(provider, base_url) is expected


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (1, "", "Tailscale is stopped.", True),
        (0, '{"BackendState":"Stopped"}', "", True),
        (0, '{"BackendState":"Running"}', "", False),
        (1, '{"BackendState":"NeedsLogin"}', "", None),
        (1, "not json", "permission denied", None),
    ],
)
def test_parse_tailscale_status_requires_explicit_stopped_evidence(
    returncode, stdout, stderr, expected
):
    assert guard._parse_tailscale_status(returncode, stdout, stderr) is expected


def test_peer_name_cannot_spoof_stopped_marker_inside_running_json():
    payload = (
        '{"BackendState":"Running","Peer":{"node":{"DNSName":'
        '"tailscale is stopped.example"}}}'
    )
    assert guard._parse_tailscale_status(0, payload, "") is False


def test_tailscale_status_is_process_cached(monkeypatch):
    calls = []

    def _probe():
        calls.append(True)
        return True, "backend_state=Stopped"

    monkeypatch.setattr(guard, "_probe_tailscale_status_uncached", _probe)

    assert guard.tailscale_status_down() == (True, "backend_state=Stopped")
    assert guard.tailscale_status_down() == (True, "backend_state=Stopped")
    assert len(calls) == 1


def test_uncached_probe_executes_status_json_and_parses_stopped(monkeypatch):
    observed = {}

    def _run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Tailscale is stopped.",
        )

    monkeypatch.setattr(guard, "_tailscale_binary", lambda: "/fake/tailscale")
    monkeypatch.setattr(guard.subprocess, "run", _run)

    assert guard._probe_tailscale_status_uncached() == (
        True,
        "backend_state=Stopped",
    )
    assert observed["argv"] == ["/fake/tailscale", "status", "--json"]
    assert observed["kwargs"]["check"] is False
    assert observed["kwargs"]["timeout"] == guard._TAILSCALE_STATUS_TIMEOUT_S


def test_summary_is_one_terse_causal_label():
    agent = MagicMock()
    agent.session_id = "session-1"
    agent._shared_transport_affected_routes = set()
    agent._shared_transport_summary_emitted = False

    guard.record_unavailable_route(agent, "claude-apr", "claude-fable-5")
    guard.record_unavailable_route(agent, "claude-apx-1", "claude-opus-5")
    guard.record_unavailable_route(agent, "claude-apx-2", "claude-opus-5")
    guard.emit_unavailable_summary(agent, evidence="backend_state=Stopped")
    guard.emit_unavailable_summary(agent, evidence="backend_state=Stopped")

    agent._buffer_status.assert_called_once_with("⚠️ Tailscale down")
