"""t_ba46cfe3: a granted identity lease works on a pool host with no worker desktop (mbp-2026, nas-fleet).

With ``terminal.backend: ssh`` the Bot Desktop placement is TERMINAL. A lease's page renders in the Studio's identity
browser, and agent-browser only has to reach the lease forwarder's loopback port on the pool host, so the browser
preflight must not start or require the sandbox screen for it. Without a lease the agent's own browser still needs
that screen, and its refusal is unchanged.
"""
from __future__ import annotations

import subprocess

import pytest

from tools import browser_lease
from tools import browser_tool_session as bts
from tools.bot_desktop import placement, runtime, sandbox_host
from tools.environments import streams

LEASE = "http://127.0.0.1:36753"
NO_SCREEN = "Bot Desktop needs Xvnc, xfwm4, xfce4-panel inside the terminal backend's sandbox"


@pytest.fixture
def placed_no_screen(monkeypatch, tmp_path):
    """ssh terminal backend (placement TERMINAL), no screen, and a screen that cannot come up (mbp-2026)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(placement, "_setting", lambda: "auto")
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: False)
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    starts: list[str] = []

    def _start(**kw):
        starts.append("start")
        raise RuntimeError(NO_SCREEN)

    monkeypatch.setattr(runtime, "start", _start)
    monkeypatch.setattr(runtime, "ensure_started_for_tool", lambda: starts.append("ensure"))
    return starts


def _lease(monkeypatch, url: str) -> None:
    """This process holds a live lease on ``url`` (what browser_navigate(lease_identity=...) leaves behind)."""
    monkeypatch.setattr(browser_lease, "active_url", lambda: url)
    monkeypatch.setattr(browser_lease, "override_url", lambda: url)


def test_lease_preflight_needs_no_screen_and_the_command_uses_the_lease_cdp(monkeypatch, placed_no_screen):
    _lease(monkeypatch, LEASE)
    assert placement.resolve().where == placement.TERMINAL
    assert bts._browser_command_preflight() == {"browser_cmd": "agent-browser"}
    assert placed_no_screen == []  # the screen was neither started nor required

    from tools.environments.ssh import SSHEnvironment
    env = SSHEnvironment.__new__(SSHEnvironment)
    env.user, env.host = "alexgierczyk", "mbp-2026"
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda e: "/Users/alexgierczyk/.hermes/bot-desktop/bp")
    probes: list[list[str]] = []
    monkeypatch.setattr(streams, "run_in", lambda e, argv, **kw: probes.append(argv) or
                        subprocess.CompletedProcess(argv, 0, b"/opt/homebrew/bin/agent-browser\n", b""))
    built: dict = {}
    monkeypatch.setattr(streams, "remote_command", lambda e, argv, *, child_env=None, user=None, interactive=True:
                        built.update(argv=list(argv), env=dict(child_env or {})) or ["ssh", "mbp-2026", "agent-browser"])
    monkeypatch.setattr(bts, "_agent_browser_command_env", lambda d: {"PATH": "/usr/bin", "DISPLAY": ":0"})
    monkeypatch.setattr(bts._cdp, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(bts._cloud, "_get_browser_engine", lambda: "auto")
    monkeypatch.setattr(bts._cloud, "_is_headed_mode", lambda: True)

    def _popen(argv, env, socket_dir, tag, stdin_payload=None):
        raise RuntimeError("spawn captured")

    monkeypatch.setattr(bts, "_popen_agent_browser", _popen)
    session = {"session_name": "cdp_t_ba46cfe3", "cdp_url": LEASE, "features": {"cdp_override": True}}
    _engine, result = bts._dispatch_browser_command("t", session, "agent-browser", "open",
                                                    ["https://github.com/settings/profile"], 30, None)
    assert result == {"success": False, "error": "spawn captured"}
    assert built["argv"][:3] == ["agent-browser", "--cdp", LEASE]
    assert "DISPLAY" not in built["env"]  # the Studio's DISPLAY never reaches the pool host
    assert any("command -v agent-browser" in " ".join(p) for p in probes)
    assert placed_no_screen == []  # headed mode still did not bring a screen up for the lease


@pytest.mark.parametrize("configured_cdp", ["", "http://127.0.0.1:9222"])
def test_no_lease_and_no_screen_keeps_the_refusal(monkeypatch, placed_no_screen, configured_cdp):
    """No live lease (none at all, or a configured loopback CDP that is not this process's lease): unchanged."""
    _lease(monkeypatch, "")
    if configured_cdp:
        monkeypatch.setenv("BROWSER_CDP_URL", configured_cdp)
    res = bts._browser_command_preflight()
    assert res["success"] is False
    assert res["error"] == f"The browser cannot run here: {NO_SCREEN}"
    assert placed_no_screen == ["start"]


def test_lease_on_a_host_without_agent_browser_names_what_is_missing(monkeypatch, placed_no_screen):
    _lease(monkeypatch, LEASE)
    from tools.environments.ssh import SSHEnvironment
    env = SSHEnvironment.__new__(SSHEnvironment)
    env.user, env.host = "alexgierczyk", "mbp-2026"
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(streams, "run_in", lambda e, argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b""))
    with pytest.raises(RuntimeError, match=r"BROWSER_LEASE_CLI_ABSENT: agent-browser is not on PATH for "
                                           r"alexgierczyk@mbp-2026"):
        bts._sandbox_wrap(["agent-browser", "--cdp", LEASE, "--json", "open", "x"], {"PATH": "/usr/bin"}, "/unused",
                          screenless=True)
    assert placed_no_screen == []
