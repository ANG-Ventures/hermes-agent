"""Dead-host drill for the SSH backend's keepalive (t_c20e3ec8).

A pool host that loses power mid-command sends no RST/FIN. Hermetic stand-in: a private
non-root sshd on 127.0.0.1 behind a TCP relay subprocess. The command runs through a
ControlMaster that the backend's own argv started; the relay is then SIGSTOPped, so the
path still holds TCP state but nothing reaches sshd any more. The mux client owns no TCP
connection, so only the master's ServerAlive* settings can notice; with them the client
returns rc 255 in about 10-15 s, without them it hangs until the tool timeout.
"""

import contextlib
import getpass
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tools.environments import ssh as ssh_env
from tools.environments.ssh import SSHEnvironment

_SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
pytestmark = pytest.mark.skipif(
    os.name == "nt" or not _SSHD or not shutil.which("ssh") or not shutil.which("ssh-keygen"),
    reason="needs POSIX OpenSSH client + sshd binaries")

_RELAY = textwrap.dedent("""
    import socket, sys, threading
    lsock = socket.socket(); lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", int(sys.argv[1]))); lsock.listen(8)
    def pump(a, b):
        try:
            while (data := a.recv(65536)):
                b.sendall(data)
        except OSError:
            pass
    while True:
        c, _ = lsock.accept()
        u = socket.create_connection(("127.0.0.1", int(sys.argv[2])))
        threading.Thread(target=pump, args=(c, u), daemon=True).start()
        threading.Thread(target=pump, args=(u, c), daemon=True).start()
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on {port}")


@pytest.fixture
def relayed_sshd(tmp_path):
    for name in ("host", "client"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name)], check=True)
    (tmp_path / "authorized_keys").write_text((tmp_path / "client.pub").read_text())
    sshd_port, relay_port = _free_port(), _free_port()
    # Short dir for the ControlMaster socket: macOS caps a socket path at 104 bytes and pytest's
    # tmp_path can exceed it.
    sock_dir = Path(tempfile.mkdtemp(prefix="vks", dir="/tmp"))
    (tmp_path / "sock_dir").write_text(str(sock_dir))
    (tmp_path / "sshd_config").write_text(textwrap.dedent(f"""\
        ListenAddress 127.0.0.1
        Port {sshd_port}
        HostKey {tmp_path / 'host'}
        AuthorizedKeysFile {tmp_path / 'authorized_keys'}
        PidFile {tmp_path / 'sshd.pid'}
        StrictModes no
        UsePAM no
        PasswordAuthentication no
        KbdInteractiveAuthentication no
        """))
    procs = [subprocess.Popen([_SSHD, "-D", "-e", "-f", str(tmp_path / "sshd_config")],
                              stderr=open(tmp_path / "sshd.log", "w"))]
    procs.append(subprocess.Popen([sys.executable, "-c", _RELAY, str(relay_port), str(sshd_port)]))
    try:
        _wait_port(sshd_port)
        _wait_port(relay_port)
        yield tmp_path, relay_port, procs[1]
    finally:
        for p in procs:
            with contextlib.suppress(ProcessLookupError):
                p.send_signal(signal.SIGCONT)
                p.kill()
            p.wait(timeout=5)
        shutil.rmtree(sock_dir, ignore_errors=True)


def _env(tmp_path, port) -> SSHEnvironment:
    """The real _build_ssh_command on a bare instance (the constructor would connect and sync)."""
    env = object.__new__(SSHEnvironment)
    # getpass, not os.getlogin(): hosted CI runners have no USER and no login session (getlogin -> OSError).
    env.host, env.user, env.port = "127.0.0.1", getpass.getuser(), port
    env.key_path, env.log_file = str(tmp_path / "client"), ""
    env.control_socket = Path((tmp_path / "sock_dir").read_text()) / "cm.sock"
    return env


def test_dead_host_mid_command_returns_inside_keepalive_window(relayed_sshd, monkeypatch):
    tmp_path, relay_port, relay = relayed_sshd
    monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", True)
    env = _env(tmp_path, relay_port)
    # -F /dev/null: the user's ssh config must not supply (or mask) a keepalive.
    hermetic = ["-F", "/dev/null", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}"]
    argv = env._build_ssh_command(extra_args=hermetic)
    assert "ServerAliveInterval=5" in argv and "ServerAliveCountMax=2" in argv
    try:
        # First command starts the ControlMaster from this argv (ControlPersist backgrounds it).
        first = subprocess.run(argv + ["echo up"], capture_output=True, text=True, timeout=30)
        assert first.returncode == 0 and first.stdout.strip() == "up", first.stderr
        assert subprocess.run(argv[:1] + ["-O", "check"] + argv[1:], capture_output=True,
                              timeout=10).returncode == 0, "no ControlMaster running"
        # Second command rides the master as a mux client; the host then "dies".
        t0 = time.monotonic()
        cmd = subprocess.Popen(argv + ["sleep 120"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2)
        relay.send_signal(signal.SIGSTOP)
        try:
            cmd.wait(timeout=45)
        except subprocess.TimeoutExpired:
            cmd.kill()
            pytest.fail("ssh still hung 45 s after the host stopped answering")
        elapsed = time.monotonic() - t0
        assert cmd.returncode == 255
        assert elapsed < 30, f"took {elapsed:.1f} s"
    finally:
        subprocess.run(argv[:1] + ["-O", "exit"] + argv[1:], capture_output=True, timeout=10)
