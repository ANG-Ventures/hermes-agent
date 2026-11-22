"""The per-login sandbox desktop root is private (t_7dd130bf): under a 0002 umask a bare ``mkdir -p`` left
``/tmp/hermes-bot-desktop-<login>`` 775 on ace-ai and nas-fleet. The real shell runs against a tmp dir."""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from tools.bot_desktop import runtime, sandbox_host
from tools.environments import streams


class _Env:
    def __init__(self, tmp):
        self._tmp = str(tmp)

    def get_temp_dir(self):
        return self._tmp


def _local_run_in(env, argv, *, child_env=None, user=None, timeout=None, **_kw):
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                          env={**os.environ, **(child_env or {})}, preexec_fn=lambda: os.umask(0o002))


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _sh(env):
    return subprocess.run(["bash", "-c", f"umask 0002; set -e; {sandbox_host.private_root_sh(env)}"],
                          capture_output=True, text=True, check=False)


def test_first_create_is_0700_under_umask_0002(tmp_path):
    env = _Env(tmp_path)
    assert _sh(env).returncode == 0
    assert _mode(sandbox_host.remote_root(env)) == 0o700


def test_existing_own_0775_root_is_tightened(tmp_path):
    env = _Env(tmp_path)
    root = sandbox_host.remote_root(env)
    os.mkdir(root)
    os.chmod(root, 0o775)
    assert _sh(env).returncode == 0
    assert _mode(root) == 0o700


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns everything")
def test_foreign_owned_root_is_refused(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    monkeypatch.setattr(sandbox_host, "remote_root", lambda e: "/usr")  # a real dir another account owns
    before = _mode("/usr")
    proc = _sh(env)
    assert proc.returncode == 1 and "refusing to use it" in proc.stderr
    assert _mode("/usr") == before


def test_symlinked_root_is_refused_even_to_an_own_dir(tmp_path):
    env = _Env(tmp_path)
    target = tmp_path / "victim"
    target.mkdir()
    os.chmod(target, 0o755)
    os.symlink(target, sandbox_host.remote_root(env))
    proc = _sh(env)
    assert proc.returncode == 1 and "symlink" in proc.stderr
    assert _mode(target) == 0o755


def test_adopting_a_live_desktop_tightens_a_0775_root(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    root = sandbox_host.remote_root(env)
    os.mkdir(root)
    os.chmod(root, 0o775)
    monkeypatch.setattr(sandbox_host, "_published", lambda e, rdir: {"DISPLAY": ":20"})
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(sandbox_host, "_record", lambda e, rdir, profile, live: live)
    monkeypatch.setattr(streams, "run_in", _local_run_in)
    assert sandbox_host.start(env, "daedalus", geometry="1280x800") == {"DISPLAY": ":20"}
    assert _mode(root) == 0o700


def test_seed_makes_root_and_profile_dir_0700(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    monkeypatch.setattr(sandbox_host, "_published", lambda e, rdir: {})
    monkeypatch.setattr(sandbox_host, "stop", lambda e, p: False)
    monkeypatch.setattr(sandbox_host, "missing_binaries", lambda e: [])
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    calls = []

    def run_in(e, argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            return _local_run_in(e, argv, **kw)
        raise RuntimeError("stop after the seed")  # the launcher spawn is not under test

    monkeypatch.setattr(streams, "run_in", run_in)
    with pytest.raises(RuntimeError, match="stop after the seed"):
        sandbox_host.start(env, "daedalus", geometry="1280x800")
    assert _mode(sandbox_host.remote_root(env)) == 0o700
    assert _mode(sandbox_host._remote_dir(env, "daedalus")) == 0o700


def test_browser_socket_and_shots_dirs_are_0700(tmp_path, monkeypatch):
    from tools import browser_tool_session as bts
    env = _Env(tmp_path)
    root = sandbox_host.remote_root(env)
    os.mkdir(root)
    os.chmod(root, 0o775)
    monkeypatch.setattr(bts, "_browser_in_sandbox", lambda: True)
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":20"})
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda e: str(tmp_path / "home" / "browser-profile"))
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(streams, "run_in", _local_run_in)
    monkeypatch.setattr(streams, "remote_command", lambda e, argv, **kw: ["true"])
    bts._sandbox_wrap(["agent-browser", "open", "https://example.com"], {"PATH": "/usr/bin"}, "/unused")
    assert _mode(root) == 0o700
    assert _mode(f"{root}/agent-browser") == 0o700
    assert _mode(f"{root}/shots") == 0o700
    assert (tmp_path / "home" / "browser-profile").is_dir()


def test_symlinked_shots_dir_is_refused(tmp_path, monkeypatch):
    from tools import browser_tool_session as bts
    env = _Env(tmp_path)
    root = sandbox_host.remote_root(env)
    os.mkdir(root, 0o700)
    target = tmp_path / "victim"
    target.mkdir()
    os.chmod(target, 0o755)
    os.symlink(target, f"{root}/shots")
    monkeypatch.setattr(bts, "_browser_in_sandbox", lambda: True)
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":20"})
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda e: str(tmp_path / "home" / "browser-profile"))
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(streams, "run_in", _local_run_in)
    monkeypatch.setattr(streams, "remote_command", lambda e, argv, **kw: ["true"])
    with pytest.raises(RuntimeError, match="is a symlink"):
        bts._sandbox_wrap(["agent-browser", "open", "https://example.com"], {"PATH": "/usr/bin"}, "/unused")
    assert _mode(target) == 0o755
