"""Tests for the SSH remote execution environment backend."""

import contextlib
import json
import os
import shlex
import subprocess
import uuid
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments import ssh as ssh_env

_SSH_HOST = os.getenv("TERMINAL_SSH_HOST", "")
_SSH_USER = os.getenv("TERMINAL_SSH_USER", "")
_SSH_PORT = int(os.getenv("TERMINAL_SSH_PORT", "22"))
_SSH_KEY = os.getenv("TERMINAL_SSH_KEY", "")

_has_ssh = bool(_SSH_HOST and _SSH_USER)

requires_ssh = pytest.mark.skipif(
    not _has_ssh,
    reason="TERMINAL_SSH_HOST / TERMINAL_SSH_USER not set",
)


def _run(command, task_id="ssh_test", **kwargs):
    from tools.terminal_tool import terminal_tool
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


def _cleanup(task_id="ssh_test"):
    from tools.terminal_tool_lifecycle import cleanup_vm
    cleanup_vm(task_id)


class TestBuildSSHCommand:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    def test_base_flags(self, monkeypatch):
        # ControlMaster flags are POSIX-only (#73927): assert them only
        # where multiplexing is enabled so the test passes on Windows too.
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", True)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        for flag in ("ControlMaster=auto", "ControlPersist=300",
                      "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
            assert flag in cmd

    @pytest.mark.parametrize("multiplex", [True, False])
    def test_keepalive_flags(self, monkeypatch, multiplex):
        """t_c20e3ec8: a host that dies mid-command sends no RST/FIN; without ServerAlive* the call
        hangs to the tool timeout. With multiplexing this argv also starts the ControlMaster, the
        process that owns the TCP connection, so the flags must ride every built command."""
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", multiplex)
        env = SSHEnvironment(host="h", user="u")
        argv = env._build_ssh_command()
        for opt in ("ServerAliveInterval=5", "ServerAliveCountMax=2"):
            assert argv[argv.index(opt) - 1] == "-o"
        assert argv.index("ServerAliveInterval=5") < argv.index("u@h")

    def test_controlmaster_gated_off_on_windows(self, monkeypatch):
        """#73927: Windows OpenSSH has no Unix-domain ControlMaster, so the
        ControlPath/ControlMaster/ControlPersist options must be omitted —
        passing them fails the connection with 'getsockname failed'."""
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", False)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        assert "ControlMaster" not in cmd
        assert "ControlPath" not in cmd
        assert "ControlPersist" not in cmd
        # Non-multiplex flags must still be present — the backend works,
        # just without connection pooling.
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert env._build_ssh_command()[-1] == "u@h"


    def test_user_host_suffix(self):
        env = SSHEnvironment(host="h", user="u")
        assert env._build_ssh_command()[-1] == "u@h"

    def _capture_run_bash(self, monkeypatch, env, cmd="echo ok"):
        captured = {}

        def _fake_popen(cmd, stdin_data=None, **kwargs):
            captured["cmd"], captured["env"] = cmd, kwargs.get("env")
            return MagicMock()

        monkeypatch.setattr(ssh_env, "_popen_bash", _fake_popen)
        env._run_bash(cmd)
        return captured

    def test_run_bash_forwards_passthrough_by_sendenv_never_in_remote_argv(self, monkeypatch):
        """#14091: allowlisted names travel as ``-o SendEnv=NAME`` with values only in the ssh client env;
        provider credentials on the allowlist stay behind; a .env value fills an unset shell var."""
        import tools.env_passthrough as env_passthrough

        env = SSHEnvironment(host="h", user="u")
        monkeypatch.setenv("NEXTCLOUD_URL", "https://next.example")
        monkeypatch.delenv("NEXTCLOUD_PASS", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-forward")
        monkeypatch.setattr(env_passthrough, "get_all_passthrough",
                            lambda: frozenset({"NEXTCLOUD_URL", "NEXTCLOUD_PASS", "OPENAI_API_KEY"}))
        monkeypatch.setattr(ssh_env, "_load_hermes_env_vars", lambda: {"NEXTCLOUD_PASS": "from-dotenv"})

        captured = self._capture_run_bash(monkeypatch, env)

        sent = {a.split("=", 1)[1] for a in captured["cmd"] if a.startswith("SendEnv=")}
        assert sent == {"NEXTCLOUD_URL", "NEXTCLOUD_PASS"}
        assert captured["env"]["NEXTCLOUD_URL"] == "https://next.example"
        assert captured["env"]["NEXTCLOUD_PASS"] == "from-dotenv"
        remote_text = " ".join(captured["cmd"])
        assert "https://next.example" not in remote_text and "from-dotenv" not in remote_text
        assert "sk-must-not-forward" not in remote_text

    def test_run_bash_without_passthrough_inherits_env_unchanged(self, monkeypatch):
        import tools.env_passthrough as env_passthrough

        monkeypatch.setattr(env_passthrough, "get_all_passthrough", lambda: frozenset())
        captured = self._capture_run_bash(monkeypatch, SSHEnvironment(host="h", user="u"))
        assert not any(a.startswith("SendEnv=") for a in captured["cmd"])
        assert captured["env"] is None

    def test_run_bash_tags_remote_command_with_its_run_token(self, monkeypatch):
        """t_44017fbc: the remote ``bash -c`` records its pid where the kill path can find it."""
        env = SSHEnvironment(host="h", user="u")
        proc = MagicMock()
        captured = {}
        monkeypatch.setattr(ssh_env, "_popen_bash", lambda cmd, stdin_data=None, **kw: captured.update(cmd=cmd) or proc)
        assert env._run_bash("echo ok") is proc
        token = proc._hermes_run_token
        assert token and len(token) == 32
        assert captured["cmd"][-3:-1] == ["bash", "-c"]
        assert captured["cmd"][-1].startswith(shlex.quote(ssh_env._run_preamble(token))[:-1])
        assert ssh_env._run_pidfile(token) in ssh_env._run_preamble(token)

    def test_kill_process_kills_the_client_then_the_remote_group(self, monkeypatch):
        """t_44017fbc: killing only the local ssh client left the remote tree running for 30+ h."""
        env = SSHEnvironment(host="h", user="u")
        order = []
        proc = MagicMock(_hermes_run_token="ab12")
        proc.kill.side_effect = lambda: order.append("client")
        monkeypatch.setattr(env, "_run_ssh", lambda remote, timeout: order.append((remote, timeout)))
        env._kill_process(proc)
        assert order[0] == "client" and len(order) == 2
        remote, timeout = order[1]
        assert timeout <= 5
        assert remote.startswith("sh -c ") and ssh_env._run_pidfile("ab12") in remote and "TERM KILL" in remote

    def test_force_kill_runs_a_kill_only_remote_kill_off_thread(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        seen = []
        monkeypatch.setattr(env, "_run_ssh", lambda remote, timeout: seen.append(remote))
        proc = MagicMock(_hermes_run_token="ab12")
        env._force_kill_process(proc)
        proc.kill.assert_called_once()
        (remote,) = seen
        assert ssh_env._run_pidfile("ab12") in remote and "for sig in KILL;" in remote


def _group_sleeps(pgid):
    import psutil
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] == "sleep" and os.getpgid(p.pid) == pgid:
                out.append(p)
        except (ProcessLookupError, psutil.Error):
            pass
    return out


def _pidfile(token):
    return os.path.expandvars(ssh_env._run_pidfile(token))


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
@pytest.mark.parametrize("body", ["(sleep 600 &); sleep 601 & sleep 602; true", "sleep 601 & exec sleep 602"],
                         ids=["background-grandchild", "top-level-exec"])
def test_remote_group_kill_reaps_the_whole_tree(body, monkeypatch, tmp_path):
    """t_44017fbc: run the real remote kill script against a ``bash -c`` started the way sshd starts
    it (own session). Every sleep in its group dies, incl. a ``(cmd &)`` grandchild re-parented to
    init and a top-level ``exec`` that replaced the tagged bash; the pidfile is removed."""
    import stat
    import time

    monkeypatch.setenv("HOME", str(tmp_path))
    token = uuid.uuid4().hex
    tree = subprocess.Popen(["bash", "-c", ssh_env._run_script(token, body)], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    want = 3 if body.startswith("(") else 2
    try:
        deadline = time.monotonic() + 10
        while len(_group_sleeps(tree.pid)) < want and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(_group_sleeps(tree.pid)) == want
        assert os.path.exists(_pidfile(token))
        # owner-only, under the user's home: no other account can plant a pid for the kill to signal
        assert _pidfile(token).startswith(str(tmp_path) + os.sep)
        assert stat.S_IMODE(os.stat(os.path.dirname(_pidfile(token))).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(_pidfile(token)).st_mode) == 0o600
        rc = subprocess.run(["sh", "-c", ssh_env._remote_group_kill_command(token)], timeout=30).returncode
        assert rc == 0
        tree.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _group_sleeps(tree.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _group_sleeps(tree.pid) == []
        assert not os.path.exists(_pidfile(token))
    finally:
        for p in _group_sleeps(tree.pid):
            p.kill()
        tree.kill()
        tree.wait()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_pidfile(token))


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_finished_command_leaves_no_pidfile_and_kill_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    token = uuid.uuid4().hex
    subprocess.run(["bash", "-c", ssh_env._run_script(token, "true")], check=True, timeout=10)
    assert not os.path.exists(_pidfile(token))
    assert subprocess.run(["sh", "-c", ssh_env._remote_group_kill_command(token)], timeout=10).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
@pytest.mark.parametrize("body,rc", [
    # write_file's atomic-write script sets its own EXIT trap, then clears it (t_70090b16: one
    # leaked pidfile per write_file/patch on every pool host).
    ("trap 'rm -f /nonexistent' EXIT; true; trap - EXIT", 0),
    ("trap 'true' EXIT; exit 4", 4),
    ("exec true", 0),
    ("eval 'exec sh -c \"exit 5\"'", 5),
    ("kill -KILL $BASHPID", 128 + 9),
    ("echo \"$$\" >&2; true", 0),
], ids=["user-trap-cleared", "user-trap-exit", "top-level-exec", "eval-exec", "command-sigkilled", "dollar-dollar"])
def test_command_end_removes_pidfile_whatever_the_command_does(body, rc, monkeypatch, tmp_path):
    """t_70090b16: the pidfile goes when the command ends, even when the command replaces the
    EXIT trap, ``exec``s, or is SIGKILLed; exit status passes through."""
    monkeypatch.setenv("HOME", str(tmp_path))
    token = uuid.uuid4().hex
    res = subprocess.run(["bash", "-c", ssh_env._run_script(token, body)], timeout=10,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    assert res.returncode == rc
    assert not os.path.exists(_pidfile(token))
    if body.startswith("echo"):
        # ``$$`` is still the pid the pidfile recorded (the group leader the kill path signals)
        assert res.stderr.strip().isdigit()


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_stale_pidfile_sweep_removes_dead_keeps_live_and_foreign(monkeypatch, tmp_path):
    """t_70090b16: a wrapper killed outright cannot clean up; the next start sweeps files whose pid
    is dead, and touches nothing outside its own name pattern."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / ".hermes-run"
    run_dir.mkdir(mode=0o700)
    dead = subprocess.Popen(["true"])
    dead.wait()
    live = subprocess.Popen(["sleep", "60"])
    try:
        files = {
            "dead": (f"hermes-run-{uuid.uuid4().hex}.pid", f"{dead.pid}\n"),
            "live": (f"hermes-run-{uuid.uuid4().hex}.pid", f"{live.pid}\n"),
            "empty": (f"hermes-run-{uuid.uuid4().hex}.pid", ""),  # pid not written yet
            "foreign_name": (f"other-{uuid.uuid4().hex}.pid", f"{dead.pid}\n"),
            "short_token": ("hermes-run-abc.pid", f"{dead.pid}\n"),
        }
        for name, content in files.values():
            (run_dir / name).write_text(content)
        rc = subprocess.run(["sh", "-c", ssh_env._stale_pidfile_sweep_command()], timeout=30).returncode
        assert rc == 0
        left = {k for k, (name, _) in files.items() if (run_dir / name).exists()}
        assert left == {"live", "empty", "foreign_name", "short_token"}
    finally:
        live.kill()
        live.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_stale_pidfile_sweep_without_dir_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert subprocess.run(["sh", "-c", ssh_env._stale_pidfile_sweep_command()], timeout=10).returncode == 0
    assert not (tmp_path / ".hermes-run").exists()


class TestControlSocketPath:
    """Regression tests for issue #11840.

    macOS caps Unix domain socket paths at 104 bytes (sun_path). SSH
    appends a 16-byte random suffix to the control socket path when
    operating in ControlMaster mode. An IPv6 host embedded in the
    filename plus the deeply-nested macOS $TMPDIR easily blows past
    the limit, causing every tool call to fail immediately.
    """

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    # SSH appends ``.XXXXXXXXXXXXXXXX`` (17 bytes) to the ControlPath in
    # ControlMaster mode; the macOS sun_path field is 104 bytes including
    # the NUL terminator, so the usable path length is 103 bytes.
    _SSH_CONTROLMASTER_SUFFIX = 17
    _MAX_SUN_PATH = 103

    def test_fits_under_macos_socket_limit_with_ipv6_host(self, monkeypatch):
        """A realistic macOS $TMPDIR + IPv6 host must still produce a
        control socket path that fits once SSH appends its ControlMaster
        suffix (see issue #11840)."""
        # Simulate the macOS $TMPDIR shape from the issue traceback —
        # 48 bytes, the typical length of ``/var/folders/XX/YYYYYYYYY/T``.
        fake_tmp = "/var/folders/2t/wbkw5yb158jc3zhswgl7tz9c0000gn/T"
        monkeypatch.setattr("tools.environments.ssh.tempfile.gettempdir",
                            lambda: fake_tmp)
        # The simulated path doesn't exist on the test host — skip the
        # real mkdir so __init__ can proceed.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "mkdir", lambda *a, **k: None)

        env = SSHEnvironment(
            host="9373:9b91:4480:558d:708e:e601:24e8:d8d0",
            user="hermes",
            port=22,
        )

        total_len = len(str(env.control_socket)) + self._SSH_CONTROLMASTER_SUFFIX
        assert total_len <= self._MAX_SUN_PATH, (
            f"control socket path would exceed the {self._MAX_SUN_PATH}-byte "
            f"Unix domain socket limit once SSH appends its 16-byte suffix: "
            f"{env.control_socket} (+{self._SSH_CONTROLMASTER_SUFFIX} = {total_len})"
        )

    def test_path_is_deterministic_across_instances(self):
        """Same (user, host, port) must yield the same control socket so
        ControlMaster reuse works across reconnects."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        assert first.control_socket == second.control_socket

    def test_path_differs_for_different_targets(self):
        """Different (user, host, port) triples must produce different paths."""
        base = SSHEnvironment(host="h", user="u", port=22).control_socket
        assert SSHEnvironment(host="h", user="u", port=23).control_socket != base
        assert SSHEnvironment(host="h", user="v", port=22).control_socket != base
        assert SSHEnvironment(host="g", user="u", port=22).control_socket != base


class TestTerminalToolConfig:


    def test_ssh_persistent_respects_config(self, monkeypatch):
        """TERMINAL_PERSISTENT_SHELL=false disables SSH persistent by default."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.setenv("TERMINAL_PERSISTENT_SHELL", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False


class TestSSHPreflight:
    def test_ensure_ssh_available_raises_clear_error_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="SSH is not installed or not in PATH"):
            ssh_env._ensure_ssh_available()


    def test_ssh_environment_connects_when_ssh_exists(self, monkeypatch):
        called = {"count": 0}

        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")

        def _fake_establish(self):
            called["count"] += 1

        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", _fake_establish)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", lambda **kw: type("M", (), {"sync": lambda self, **k: None})())

        env = ssh_env.SSHEnvironment(host="example.com", user="alice")

        assert called["count"] == 1
        assert env.host == "example.com"
        assert env.user == "alice"

    def test_ssh_environment_can_skip_agent_file_sync(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(
            ssh_env.SSHEnvironment,
            "_ensure_remote_dirs",
            lambda self: pytest.fail("workspace browsing must not mutate the SSH target"),
        )
        monkeypatch.setattr(
            ssh_env,
            "FileSyncManager",
            lambda **_kw: pytest.fail("workspace browsing must not start agent file sync"),
        )

        env = ssh_env.SSHEnvironment(
            host="example.com",
            user="alice",
            sync_files=False,
        )

        assert env._sync_manager is None
        env._before_execute()


@pytest.fixture
def _mock_ssh_runtime(monkeypatch, tmp_path):
    hooks = {
        "_establish_connection": MagicMock(),
        "_detect_remote_home": MagicMock(return_value="/home/alice"),
        "_sweep_stale_pidfiles": MagicMock(),
        "_ensure_remote_dirs": MagicMock(),
        "init_session": MagicMock(),
    }
    monkeypatch.setattr(ssh_env.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
    for name, hook in hooks.items():
        monkeypatch.setattr(ssh_env.SSHEnvironment, name, hook)
    hooks["sync_factory"] = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(ssh_env, "FileSyncManager", hooks["sync_factory"])
    return hooks


class TestSSHProbeOnly:
    def test_probe_only_skips_state_sync_and_session_setup(self, _mock_ssh_runtime):
        env = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)
        env._before_execute()
        env.cleanup()

        _mock_ssh_runtime["_establish_connection"].assert_called_once_with()
        _mock_ssh_runtime["_detect_remote_home"].assert_not_called()
        _mock_ssh_runtime["_ensure_remote_dirs"].assert_not_called()
        _mock_ssh_runtime["sync_factory"].assert_not_called()
        _mock_ssh_runtime["init_session"].assert_not_called()

    def test_probe_only_control_socket_is_isolated(self, monkeypatch, _mock_ssh_runtime):
        control_exit_calls = []

        def _fake_run(*args, **kwargs):
            control_exit_calls.append(args[0])
            return subprocess.CompletedProcess([], 0)

        monkeypatch.setattr(ssh_env.subprocess, "run", _fake_run)

        normal = ssh_env.SSHEnvironment(host="example.com", user="alice")
        first_probe = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)
        second_probe = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)

        assert first_probe.control_socket != normal.control_socket
        assert second_probe.control_socket != first_probe.control_socket
        assert len(first_probe.control_socket.name) == len(normal.control_socket.name)

        normal.control_socket.touch()
        first_probe.control_socket.touch()
        first_probe.cleanup()

        assert normal.control_socket.exists()
        assert not first_probe.control_socket.exists()
        assert len(control_exit_calls) == 1


def _setup_ssh_env(monkeypatch, persistent: bool):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", _SSH_HOST)
    monkeypatch.setenv("TERMINAL_SSH_USER", _SSH_USER)
    monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true" if persistent else "false")
    if _SSH_PORT != 22:
        monkeypatch.setenv("TERMINAL_SSH_PORT", str(_SSH_PORT))
    if _SSH_KEY:
        monkeypatch.setenv("TERMINAL_SSH_KEY", _SSH_KEY)


@requires_ssh
class TestOneShotSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=False)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello")
        assert r["exit_code"] == 0
        assert "hello" in r["output"]


    def test_state_does_not_persist(self):
        _run("export HERMES_ONESHOT_TEST=yes")
        r = _run("echo $HERMES_ONESHOT_TEST")
        assert r["output"].strip() == ""


@requires_ssh
class TestPersistentSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=True)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello-persistent")
        assert r["exit_code"] == 0
        assert "hello-persistent" in r["output"]

    def test_env_var_persists(self):
        _run("export HERMES_PERSIST_TEST=works")
        r = _run("echo $HERMES_PERSIST_TEST")
        assert r["output"].strip() == "works"


    def test_large_output(self):
        r = _run("seq 1 1000")
        assert r["exit_code"] == 0
        lines = r["output"].strip().splitlines()
        assert len(lines) == 1000
        assert lines[0] == "1"
        assert lines[-1] == "1000"
