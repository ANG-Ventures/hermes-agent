"""t_db945925: two workers on one pool-host login, each holding a lease, must not share an agent-browser daemon.

A lease command passes ``--cdp`` and no ``--session``, so agent-browser uses session "default" in its socket dir. With
one socket dir per ssh login, two lease sessions on one machine would both talk to ``<dir>/default.sock``.
"""
from __future__ import annotations

import subprocess

import pytest

from tools import browser_lease
from tools import browser_tool_session as bts
from tools.bot_desktop import placement, runtime, sandbox_host
from tools.environments import streams


@pytest.fixture
def ssh_login(monkeypatch):
    """ssh terminal backend as the machine owner's login (every worker on mbp-2026 since 2026-11-20)."""
    monkeypatch.setattr(placement, "_setting", lambda: "auto")
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    from tools.environments.ssh import SSHEnvironment
    env = SSHEnvironment.__new__(SSHEnvironment)
    env.user, env.host = "alexgierczyk", "mbp-2026"
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(sandbox_host, "remote_root", lambda e: "/tmp/hermes-bot-desktop-alexgierczyk")
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda e: "/Users/alexgierczyk/.hermes/bot-desktop/bp")
    ran: list[str] = []
    monkeypatch.setattr(streams, "run_in", lambda e, argv, **kw: ran.append(" ".join(argv)) or
                        subprocess.CompletedProcess(argv, 0, b"/opt/homebrew/bin/agent-browser\n", b""))
    monkeypatch.setattr(streams, "remote_command", lambda e, argv, *, child_env=None, user=None, interactive=True:
                        ["ssh", "mbp-2026", *argv, f"SOCK={child_env['AGENT_BROWSER_SOCKET_DIR']}"])
    monkeypatch.setattr(browser_lease, "_end_hooks", [])
    monkeypatch.setattr(bts, "_lease_socket_dirs", {})
    return ran


def _sock(lease_url: str, session: str, screenless: bool = True) -> str:
    wrapped, _env = bts._sandbox_wrap(["agent-browser", "--cdp", lease_url, "--json", "snapshot"], {"PATH": "/usr/bin"},
                                      "/unused", screenless=screenless, session_name=session)
    return wrapped[-1].removeprefix("SOCK=")


def test_two_lease_sessions_on_one_login_get_separate_daemon_namespaces(ssh_login):
    a = _sock("http://127.0.0.1:36753", "cdp_aaaaaaaaaa")
    b = _sock("http://127.0.0.1:41022", "cdp_bbbbbbbbbb")
    assert a != b
    assert a == "/tmp/hermes-bot-desktop-alexgierczyk/agent-browser-cdp_aaaaaaaaaa"
    assert len(f"{a}/default.sock") < 104  # macOS sun_path limit
    assert any(a in r and "mkdir -p -m 0700" in r for r in ssh_login)
    assert any(b in r and "mkdir -p -m 0700" in r for r in ssh_login)


def test_non_lease_session_keeps_the_shared_dir(ssh_login, monkeypatch):
    """``--session <unique name>`` already keeps non-lease daemons apart inside the one dir: unchanged."""
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":1"})  # a worker desktop is up
    assert _sock("", "h_cccccccccc", screenless=False) == "/tmp/hermes-bot-desktop-alexgierczyk/agent-browser"


def test_lease_end_removes_each_lease_socket_dir(ssh_login):
    a = _sock("http://127.0.0.1:36753", "cdp_aaaaaaaaaa")
    ssh_login.clear()
    browser_lease._ended()
    assert len(ssh_login) == 1 and f"rm -rf -- {a}" in ssh_login[0] and "default.pid" in ssh_login[0]
    assert bts._lease_socket_dirs == {}
    ssh_login.clear()
    browser_lease._ended()  # nothing left to remove
    assert ssh_login == []


def test_close_lease_runs_the_end_hooks(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(browser_lease, "_end_hooks", [])
    browser_lease.on_lease_end(lambda: calls.append("hook"))
    monkeypatch.setattr(browser_lease, "_end", lambda lease, timeout=0: calls.append("end"))
    monkeypatch.setattr(browser_lease, "_active", object())
    browser_lease.close_lease()
    assert calls == ["end", "hook"]
