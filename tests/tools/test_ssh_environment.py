"""Tests for the SSH remote execution environment backend."""

import contextlib
import json
import os
import shlex
import subprocess
import time
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
_REAL_POPEN = subprocess.Popen  # TestBuildSSHCommand mocks subprocess.Popen/run module-wide

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

    def _run_remote_text_locally(self, captured, tmp_path):
        """Run the remote ``bash -c`` text the way the pool host's shell would, with a bare env."""
        remote = shlex.split(captured["cmd"][-1])[0]
        with _REAL_POPEN(["bash", "-c", remote], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}) as proc:
            return proc.communicate(timeout=20)[0].strip()

    def test_placed_worker_shell_carries_task_and_host_workspace(self, monkeypatch, tmp_path):
        """t_dad3fadc: a placed worker's remote shell had HERMES_KANBAN_TASK/WORKSPACE empty on every
        pool host (kanbanw and owner login alike): ssh carries no env. The task id and the HOST
        workspace (the placement's TERMINAL_CWD, not the Studio path) ride the command, with an owner
        marker no remote pid can equal, so a remote ``hermes`` never takes the grant."""
        monkeypatch.setenv("KANBAN_WORKER_PLACEMENT", json.dumps(
            {"host": "ace-media", "env": {"TERMINAL_CWD": "/Volumes/fleet-scratch/workspaces/default/t_abc"}}))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/Users/x/.hermes/kanban/workspaces/t_abc")
        monkeypatch.setenv("HERMES_KANBAN_DB", "/Users/x/.hermes/kanban.db")
        env = SSHEnvironment(host="h", user="u")
        captured = self._capture_run_bash(
            monkeypatch, env,
            'echo "$HERMES_KANBAN_TASK|$HERMES_KANBAN_WORKSPACE|$HERMES_KANBAN_OWNER_PID|${HERMES_KANBAN_DB-unset}"')
        task, ws, owner, db = self._run_remote_text_locally(captured, tmp_path).split("|")
        assert (task, ws, db) == ("t_abc", "/Volumes/fleet-scratch/workspaces/default/t_abc", "unset")
        with pytest.raises(ValueError):
            int(owner)  # owns_kanban_worker_authority() refuses a non-pid marker
        assert owner

    def test_unplaced_or_delegated_shell_gets_no_kanban_identity(self, monkeypatch, tmp_path):
        """No placement (a plain ssh backend) or a delegate_task child: nothing is exported."""
        from agent import delegation_context

        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
        probe = 'echo "[${HERMES_KANBAN_TASK-}]"'
        monkeypatch.delenv("KANBAN_WORKER_PLACEMENT", raising=False)
        captured = self._capture_run_bash(monkeypatch, SSHEnvironment(host="h", user="u"), probe)
        assert self._run_remote_text_locally(captured, tmp_path) == "[]"
        monkeypatch.setenv("KANBAN_WORKER_PLACEMENT", json.dumps({"host": "h", "env": {"TERMINAL_CWD": "/w"}}))
        monkeypatch.setattr(delegation_context, "is_delegated_child_process_context", lambda: True)
        captured = self._capture_run_bash(monkeypatch, SSHEnvironment(host="h", user="u"), probe)
        assert self._run_remote_text_locally(captured, tmp_path) == "[]"

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
    tree = subprocess.Popen(["bash", "-c", ssh_env._run_preamble(token) + body], start_new_session=True,
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
    subprocess.run(["bash", "-c", ssh_env._run_preamble(token) + "true"], check=True, timeout=10)
    assert not os.path.exists(_pidfile(token))
    assert subprocess.run(["sh", "-c", ssh_env._remote_group_kill_command(token)], timeout=10).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
@pytest.mark.parametrize("body,rc", [
    # write_file's atomic-write script sets its own EXIT trap, then clears it (t_70090b16: one
    # leaked pidfile per write_file/patch on every pool host).
    ("trap 'rm -f /nonexistent' EXIT; true; trap - EXIT", 0),
    ("trap 'true' EXIT; exit 4", 4),
    ("exec true", 0),
    ("kill -KILL $$", -9),
], ids=["user-trap-cleared", "user-trap-exit", "top-level-exec", "command-sigkilled"])
def test_next_command_sweeps_a_pidfile_the_command_left(body, rc, monkeypatch, tmp_path):
    """t_70090b16: a command that takes the EXIT trap, ``exec``s or is SIGKILLed leaves its pidfile;
    the next command on the host removes it, and keeps its own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    token = uuid.uuid4().hex
    res = subprocess.run(["bash", "-c", ssh_env._run_preamble(token) + body], timeout=10,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert res.returncode == rc
    nxt = uuid.uuid4().hex
    seen = subprocess.run(["bash", "-c", ssh_env._run_preamble(nxt) + f'[ -f "{_pidfile(nxt)}" ] && echo own; '
                           'echo "vars=${_vr_f-}${_vr_p-}"'],
                          timeout=10, capture_output=True, text=True, check=True).stdout.split()
    assert seen == ["own", "vars="]
    assert not os.path.exists(_pidfile(token))


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_kill_dollar_dollar_still_ends_the_command(monkeypatch, tmp_path):
    """``kill $$`` must stop the command itself, not a wrapper around it (Prism 8f40170a101e on #480:
    a subshell wrapper let the body run on, orphaned and out of the kill path's reach)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    marker = tmp_path / "survived"
    res = subprocess.run(["bash", "-c", ssh_env._run_preamble(uuid.uuid4().hex) + f"kill $$; sleep 1; touch {marker}"],
                         timeout=10)
    time.sleep(1.5)
    assert res.returncode == -15
    assert not marker.exists()


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
        rc = subprocess.run(["bash", "-c", ssh_env._stale_pidfile_sweep()], timeout=30).returncode
        assert rc == 0
        left = {k for k, (name, _) in files.items() if (run_dir / name).exists()}
        assert left == {"live", "empty", "foreign_name", "short_token"}
    finally:
        live.kill()
        live.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_stale_pidfile_sweep_without_dir_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert subprocess.run(["bash", "-c", ssh_env._stale_pidfile_sweep()], timeout=10).returncode == 0
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


class TestIdleReaperSparesARunningCommand:
    """t_db3261ec: the idle reaper (TERMINAL_LIFETIME_SECONDS=300, checked every 60 s) timed an env
    from its LAST CALL START, so a foreground command still running at 300-360 s had its env torn
    down under it: SSHEnvironment.cleanup() sends ``ssh -O exit`` to the master, the call returns
    255 with no output and the remote command runs on unowned. 209 of 209 such drops in the logged
    24 h sat within 3 s of 'Cleaned up inactive environment'."""

    @pytest.fixture
    def stale_env(self, monkeypatch):
        import time
        import tools.terminal_tool as tt
        from tools.environments import base
        env = MagicMock()
        monkeypatch.setitem(tt._active_environments, "t_reap", env)
        monkeypatch.setitem(tt._last_activity, "t_reap", time.time() - 340)
        yield env, base
        tt._active_environments.pop("t_reap", None)
        tt._last_activity.pop("t_reap", None)

    def test_env_with_a_live_foreground_command_is_not_reaped(self, stale_env, monkeypatch):
        from tools.terminal_tool_lifecycle import _cleanup_inactive_envs
        import tools.terminal_tool as tt
        env, base = stale_env
        proc = MagicMock()
        monkeypatch.setitem(base._live_foreground, id(proc), (env, proc))
        _cleanup_inactive_envs(300)
        env.cleanup.assert_not_called()
        assert tt._active_environments.get("t_reap") is env

    def test_idle_env_sharing_a_busy_envs_master_is_not_reaped(self, stale_env, monkeypatch):
        """Prism b928ca3e4fd8: cleanup() ends the ControlMaster, which every env on the same target and
        control scope shares (a delegate_task child in the same worker): reaping the idle one would drop
        the busy one's call."""
        from tools.terminal_tool_lifecycle import _cleanup_inactive_envs
        env, base = stale_env
        env.control_socket = "/tmp/hermes-ssh/abc.sock"
        busy = MagicMock(control_socket="/tmp/hermes-ssh/abc.sock")
        proc = MagicMock()
        monkeypatch.setitem(base._live_foreground, id(proc), (busy, proc))
        _cleanup_inactive_envs(300)
        env.cleanup.assert_not_called()

    def test_idle_env_without_a_command_is_still_reaped(self, stale_env):
        from tools.terminal_tool_lifecycle import _cleanup_inactive_envs
        import tools.terminal_tool as tt
        env, _ = stale_env
        _cleanup_inactive_envs(300)
        env.cleanup.assert_called_once()
        assert "t_reap" not in tt._active_environments


class TestLostConnectionIsNamedAndTheRemoteCommandDecided:
    """t_db3261ec: an ssh client that exits 255 while its remote command still runs (master gone)
    must not hand back a bare empty 255 and leave the command running: the remote group is killed
    and the result says so. A command that itself exited 255 is left alone."""

    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]), stderr=iter([]), stdin=MagicMock()))
        return SSHEnvironment(host="h", user="u", sync_files=False)

    def _wait(self, env, monkeypatch, output, rc, remote_stdout):
        from tools.environments.base import BaseEnvironment
        monkeypatch.setattr(BaseEnvironment, "_wait_for_process",
                            lambda self, proc, timeout=120, **kw: {"output": output, "returncode": rc})
        calls = []

        def fake_run_ssh(remote, timeout):
            calls.append(remote)
            if isinstance(remote_stdout, Exception):
                raise remote_stdout
            return subprocess.CompletedProcess([], 0, stdout=remote_stdout, stderr="")
        monkeypatch.setattr(env, "_run_ssh", fake_run_ssh)
        return env._wait_for_process(MagicMock(_hermes_run_token="ab12"), timeout=600), calls

    def test_dropped_connection_kills_the_remote_command_and_names_it(self, env, monkeypatch):
        r, calls = self._wait(env, monkeypatch, "02:37:04\n", 255, ssh_env._KILLED_MARK + "\n")
        assert len(calls) == 1 and ssh_env._run_pidfile("ab12") in calls[0]
        assert r["returncode"] == 255 and r["hermes_connection_lost"] is True
        assert r["output"].startswith("02:37:04\n")
        assert "ssh connection to u@h closed" in r["output"] and "has been killed" in r["output"]

    def test_unreachable_host_still_names_the_failure(self, env, monkeypatch):
        r, _ = self._wait(env, monkeypatch, "", 255, subprocess.TimeoutExpired("ssh", 5))
        assert r["hermes_connection_lost"] is True
        assert r["output"].startswith("[ssh connection to u@h closed") and "could not be checked" in r["output"]

    def test_no_marker_and_nothing_running_is_named_without_claiming_a_lost_link(self, env, monkeypatch):
        """Prism dbd92286d045: a chatty command SIGPIPEd by the drop leaves no pidfile, just like a command
        that ran ``exit 255``; neither prints the completion marker. Say which two it can be, never bare."""
        r, calls = self._wait(env, monkeypatch, "tick 31\n", 255, "")
        assert len(calls) == 1 and r["returncode"] == 255 and "hermes_connection_lost" not in r
        assert r["output"].startswith("tick 31\n[exit 255 without the completion marker")
        assert "no remote process is left" in r["output"]

    def test_completed_command_with_cwd_marker_costs_no_probe(self, env, monkeypatch):
        out = f"x\n{env._cwd_marker}/tmp{env._cwd_marker}git: seq=1\n"
        r, calls = self._wait(env, monkeypatch, out, 255, ssh_env._KILLED_MARK)
        assert calls == [] and "hermes_connection_lost" not in r

    def test_other_exit_codes_cost_no_probe(self, env, monkeypatch):
        r, calls = self._wait(env, monkeypatch, "", 1, ssh_env._KILLED_MARK)
        assert calls == [] and r == {"output": "", "returncode": 1}


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote shell")
def test_remote_group_kill_reports_whether_it_killed_a_live_command(monkeypatch, tmp_path):
    """The kill script prints the marker only when it signalled a live group, so the lost-connection
    path can tell 'still running, now killed' from 'already finished'."""
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    token = uuid.uuid4().hex
    tree = subprocess.Popen(["bash", "-c", ssh_env._run_preamble(token) + "sleep 603; true"], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not os.path.exists(_pidfile(token)) and time.monotonic() < deadline:
            time.sleep(0.05)
        out = subprocess.run(["sh", "-c", ssh_env._remote_group_kill_command(token)], capture_output=True,
                             text=True, timeout=30).stdout
        assert ssh_env._KILLED_MARK in out
        tree.wait(timeout=5)
        out = subprocess.run(["sh", "-c", ssh_env._remote_group_kill_command(token)], capture_output=True,
                             text=True, timeout=30).stdout
        assert ssh_env._KILLED_MARK not in out
    finally:
        tree.kill()
        tree.wait()
