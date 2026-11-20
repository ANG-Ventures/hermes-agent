"""SSH remote execution environment with ControlMaster connection persistence."""

import contextlib
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Iterable

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _popen_bash
from tools.environments.file_sync import (
    FileSyncManager, iter_sync_files, quoted_mkdir_command, quoted_rm_command, unique_parent_dirs)
from tools.environments.remote_common import (
    bash_argv, client_env_with, load_hermes_env_vars, prepend_unset, resolve_passthrough_env, run_capture)

logger = logging.getLogger(__name__)

# Windows OpenSSH has no Unix-socket ControlMaster: ControlPath/ControlMaster options
# fail the connection outright ('getsockname failed: Not a socket'). Skip multiplexing there.
# Skip multiplexing there; each command pays a fresh connection but the backend works. See #73927.
_SSH_MULTIPLEX = os.name != "nt"

# Module-level binding: tests patch ``ssh._load_hermes_env_vars`` to fake the .env file.
_load_hermes_env_vars = load_hermes_env_vars


def _placed_lease_env_names() -> tuple[str, ...]:
    """Browser-lease names a dispatcher-placed worker forwards into its placed shell (Browser Sandboxes
    pass-3 R1(a)): CDP is driven from the placed shell, so the lease must reach it. A NAMED allowlist
    (``kanban_browser_lease.LEASE_ENV_NAMES``), never ``*``, and only on a placed worker; the
    dispatcher's own credential is not in the list. Values ride SendEnv; the host's sshd must
    ``AcceptEnv`` these names (hermes-home ``fleet/ssh/browser-lease-acceptenv.conf``)."""
    if not os.environ.get("KANBAN_WORKER_PLACEMENT"):
        return ()
    from hermes_cli.kanban_browser_lease import LEASE_ENV_NAMES
    return tuple(n for n in LEASE_ENV_NAMES if os.environ.get(n))


# exits 97 unless the forced command fleet-worker-shell set FLEET_WORKER_SESSION=1 (t_3911e84c);
# kept byte-identical to hermes_cli.kanban_worker_pool.WORKER_SESSION_GUARD (a test pins it)
WORKER_SESSION_GUARD = ('[ "${FLEET_WORKER_SESSION:-}" = 1 ] || '
                        '{ echo "not a fleet worker session" >&2; exit 97; }; ')


def control_socket_path(user: str, host: str, port: int, control_scope: str = "", *, probe: str = "",
                        key_path: str = "") -> Path:
    """The plain ControlMaster socket for ``user@host:port`` (+ a kanban run's ``control_scope``). The kanban
    release reads a finished run's footprint over this same master (t_26acfaa2), so both name it here.
    ``key_path`` keys an unscoped socket (t_3911e84c): a master never re-authenticates, so a keyed and an
    unkeyed environment for one user@host:port (a pool-placed delegate child on an owner login) must not
    share one. A scoped socket belongs to one kanban run, whose key is fixed, and keeps its name."""
    socket_key = f"{user}@{host}:{port}" + (f":{control_scope}" if control_scope else "")
    if key_path and not control_scope:
        socket_key = f"{socket_key}:key:{key_path}"
    if probe:
        socket_key = f"{socket_key}:probe:{probe}"
    return Path(tempfile.gettempdir()) / "hermes-ssh" / f"{hashlib.sha256(socket_key.encode()).hexdigest()[:16]}.sock"


def control_sockets(plain: Path) -> list[Path]:
    """``plain`` plus every SendEnv-set sibling master beside it (shared 8-char target prefix)."""
    plain = Path(plain)
    siblings = sorted(plain.parent.glob(f"{plain.stem[:8]}*.sock")) if plain.parent.is_dir() else []
    return [plain, *(s for s in siblings if s != plain)]


_RUN_MARK = "hermes-run-"


_RUN_DIR = "$HOME/.hermes-run"


def _run_pidfile(token: str) -> str:
    """Shell path (``$HOME`` expands remotely) of ``token``'s pidfile. Under the remote user's own
    home in a 0700 dir, so no other account can plant a pid for the kill path to signal."""
    return f"{_RUN_DIR}/{_RUN_MARK}{token}.pid"


def _run_preamble(token: str) -> str:
    """First lines of every remote command: sweep dead pidfiles, then record the ``bash -c`` pid
    (= its process group, as sshd starts each command in its own session) where the kill path can
    find it. A file, not the argv: a top-level ``exec`` replaces the argv but keeps the pid and the
    group. The EXIT trap is best effort (a command that replaces it, ``exec``s or is SIGKILLed
    leaves its file), so the next command on the host removes such leftovers (t_70090b16)."""
    f = _run_pidfile(token)
    return (f": {_RUN_MARK}{token}\n"
            f"{_stale_pidfile_sweep()}\n"
            f"(umask 077; mkdir -p \"{_RUN_DIR}\" && echo $$ >| \"{f}\") 2>/dev/null; "
            f"trap \"rm -f \\\"{f}\\\"\" EXIT\n")


def _stale_pidfile_sweep() -> str:
    """Shell that removes this writer's pidfiles (``hermes-run-<32 hex>.pid`` only) whose pid is
    gone. Builtins only (no fork unless a file is removed); an empty or non-numeric file (pid not
    written yet) is kept; its loop variables are unset so the command never sees them."""
    name = _RUN_MARK + "[0-9a-f]" * 32 + ".pid"
    return (f"for _vr_f in \"{_RUN_DIR}\"/{name}; do "
            "[ -f \"$_vr_f\" ] || continue; _vr_p=; read -r _vr_p < \"$_vr_f\" 2>/dev/null; "
            "case \"$_vr_p\" in ''|*[!0-9]*) continue;; esac; "
            "kill -0 \"$_vr_p\" 2>/dev/null || rm -f -- \"$_vr_f\"; done 2>/dev/null; unset _vr_f _vr_p")


_KILLED_MARK = "hermes-run-killed"


def _remote_group_kill_command(token: str, *, grace: bool = True) -> str:
    """Remote ``sh`` that signals the process group of the command that wrote ``token``'s pidfile
    (TERM, 1 s, KILL; KILL only without ``grace``), so ``cmd &`` children die too. A pid that is
    not its own group leader is signalled alone. No pidfile (the command already exited) = no-op.
    Prints ``_KILLED_MARK`` once a signal reached a live process."""
    sigs = "TERM KILL" if grace else "KILL"
    f = _run_pidfile(token)
    script = (
        f"p=$(cat \"{f}\" 2>/dev/null); rm -f \"{f}\"; "
        "case \"$p\" in ''|*[!0-9]*) exit 0;; esac; "
        "g=$(ps -o pgid= -p \"$p\" | tr -d ' '); [ -n \"$g\" ] || exit 0; "
        "[ \"$g\" = \"$p\" ] && t=\"-$g\" || t=\"$p\"; "
        f"for sig in {sigs}; do kill -s $sig -- \"$t\" 2>/dev/null || exit 0; echo {_KILLED_MARK}; "
        "[ $sig = TERM ] && sleep 1; done; exit 0")
    return "sh -c " + shlex.quote(script)


def _ensure_ssh_available() -> None:
    """Fail fast with a clear error when the SSH client is unavailable."""
    for tool in ("ssh", "scp"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool.upper()} is not installed or not in PATH. "
                               "Install OpenSSH client: apt install openssh-client")


def _sync_error(reason: str, subject: str, what: str = "the SSH connection") -> EnvironmentConnectionError:
    return EnvironmentConnectionError(
        reason, retry_hint=f"{subject} failed — verify {what} is healthy, then retry.")


class SSHEnvironment(BaseEnvironment):
    """Run commands on a remote machine over SSH.

    Spawn-per-call: every execute() spawns a fresh ``ssh ... bash -c`` process.
    Session snapshot preserves env vars across calls; CWD persists via in-band
    stdout markers. Uses SSH ControlMaster for connection reuse.
    """

    # Passthrough values are re-forwarded on every command (see _run_bash), so like docker/local
    # they stay out of the remote snapshot under multiplex.
    _profile_scoped_passthrough = True
    _sudo_nopasswd_probe_supported = True

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = "",
                 probe_only: bool = False, sync_files: bool = True,
                 control_scope: str = "", log_file: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host, self.user, self.port, self.key_path = host, user, port, key_path
        self.log_file = log_file
        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # Short, deterministic socket name: the path must stay under macOS's 104-byte sun_path
        # limit (raw user@host:port + SSH's 16-byte suffix under a deep $TMPDIR exceeds it), and
        # stability across reconnects keeps ControlMaster reuse working. A probe gets its own
        # per-instance socket so its cleanup() can never close the agent's shared master.
        # ``control_scope`` (a kanban run) gives concurrent processes to one target their own
        # master instead of racing to create the shared one.
        self.control_socket = control_socket_path(
            user, host, port, control_scope, probe=self._session_id if probe_only else "", key_path=key_path)
        _ensure_ssh_available()
        self._establish_connection()
        if probe_only:
            self._sync_manager = None
            return
        self._remote_home_detected = False
        self._remote_home = self._detect_remote_home()
        self._sync_manager = None
        if sync_files:
            self._ensure_remote_dirs()
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
                upload_fn=self._scp_upload, delete_fn=self._ssh_delete,
                bulk_upload_fn=self._ssh_bulk_upload, bulk_download_fn=self._ssh_bulk_download)
            self._sync_manager.sync(force=True)
        self.init_session()

    def _control_socket_for(self, send_env: tuple[str, ...]) -> Path:
        """One ControlMaster per SendEnv name-set, beside the plain target socket: a mux master only
        relays the env names it was itself started with and silently drops the rest, so a passthrough
        command must ride a master that knows its names. scp/sync/probes keep the plain socket."""
        plain = Path(self.control_socket)
        if not send_env:
            return plain
        # <target-id[:8]><names-hash[:8]>.sock: same length as the plain socket (macOS's 104-byte
        # sun_path cap) and prefix-globbable so cleanup() finds every sibling without extra state.
        digest = hashlib.sha256(" ".join(send_env).encode()).hexdigest()[:8]
        return plain.with_name(f"{plain.stem[:8]}{digest}.sock")

    def _control_sockets(self) -> list[Path]:
        """The plain socket plus every SendEnv-set sibling (shared 8-char target prefix)."""
        return control_sockets(self.control_socket)

    def _target_flags(self, port_flag: str) -> list:
        """Port/key flags shared by ssh (``-p``) and scp (``-P``)."""
        flags = [port_flag, str(self.port)] if self.port != 22 else []
        # IdentitiesOnly: with a key named, offer only it, never the agent's or the default keys
        # (a pool owner login accepts the owner's own key too, past the worker wrapper). An unreadable
        # key turns auth off: ssh would fall back to the ssh_config IdentityFile, the owner's key (t_3911e84c).
        if not self.key_path:
            return flags
        # expanded once, here: os.access() reads a literal "~/..." as missing (a04c57100fc5)
        key = os.path.expanduser(self.key_path)
        # IdentityAgent=none: no agent key (the owner's, if loaded) is ever offered (fb4485599af6)
        flags += ["-i", key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]
        if not os.access(key, os.R_OK):
            flags += ["-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
                      "-o", "KbdInteractiveAuthentication=no"]
        return flags

    def _build_ssh_command(self, extra_args: list | None = None, send_env: Iterable[str] = (),
                           log_to_file: bool = False) -> list:
        send_env = tuple(sorted(send_env))
        cmd = ["ssh"]
        if log_to_file and self.log_file:
            # The command path merges stderr into the tool result: ssh's own diagnostics
            # ("ControlSocket ... already exists") go to the log; the remote command's stderr stays.
            cmd.extend(["-E", self.log_file])
        if _SSH_MULTIPLEX:
            cmd.extend(["-o", f"ControlPath={self._control_socket_for(send_env)}",
                        "-o", "ControlMaster=auto", "-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"])
        # A host that dies mid-command sends no RST/FIN: without keepalives the call hangs until the
        # tool timeout. sshd answers these, not the command, so a silent command stays up. Only the
        # process that owns the TCP connection uses them: the ControlMaster, which inherits this argv
        # from the first command that starts it (a mux client's own value is ignored).
        cmd.extend(["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"])
        # Names only; values ride the ssh client's own environment (never the remote command text).
        cmd.extend(arg for name in send_env for arg in ("-o", f"SendEnv={name}"))
        cmd.extend(self._target_flags("-p"))
        cmd.extend(extra_args or [])
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _guarded(self, remote_cmd: str) -> str:
        """``remote_cmd`` behind the worker-session guard when a worker key is pinned (t_3911e84c
        fb4485599af6): ssh may still log in with the ssh_config IdentityFile, the owner's key, past the
        forced command that exports FLEET_WORKER_SESSION=1. Same text as kanban_worker_pool's guard."""
        return WORKER_SESSION_GUARD + remote_cmd if self.key_path else remote_cmd

    def _run_ssh(self, remote_cmd: str, timeout: float) -> subprocess.CompletedProcess:
        """Run one remote shell command over the multiplexed connection, capturing output."""
        return run_capture(self._build_ssh_command() + [self._guarded(remote_cmd)], timeout=timeout)

    def _run_ssh_checked(self, remote_cmd: str, timeout: float, reason: str, subject: str) -> None:
        result = self._run_ssh(remote_cmd, timeout=timeout)
        if result.returncode != 0:
            raise _sync_error(f"{reason}: {result.stderr.strip()}", subject)

    def _establish_connection(self):
        try:
            result = self._run_ssh("echo 'SSH connection established'", timeout=15)
        except subprocess.TimeoutExpired:
            raise EnvironmentConnectionError(
                f"SSH connection to {self.user}@{self.host} timed out",
                retry_hint=(f"Check that {self.host} is up and reachable on port {self.port} "
                            "and that sshd is running, then retry."))
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            raise EnvironmentConnectionError(
                f"SSH connection failed: {error_msg}",
                retry_hint=(f"Check that {self.host} is up, sshd is running on port {self.port}, and "
                            f"{self.user} can log in with the configured key, then retry — "
                            "the connection is re-established automatically."))

    def _detect_remote_home(self) -> str:
        """Detect the remote user's home directory."""
        with contextlib.suppress(Exception):
            result = self._run_ssh("echo $HOME", timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                logger.debug("SSH: remote home = %s", result.stdout.strip())
                self._remote_home_detected = True
                return result.stdout.strip()
        return "/root" if self.user == "root" else f"/home/{self.user}"

    def _ensure_remote_dirs(self) -> None:
        """Create base ~/.hermes directory tree on remote in one SSH call."""
        base = f"{self._remote_home}/.hermes"
        self._run_ssh(quoted_mkdir_command([base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]),
                      timeout=10)

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via scp over ControlMaster."""
        self._run_ssh(f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}", timeout=10)
        scp_cmd = ["scp"] + (["-o", f"ControlPath={self.control_socket}"] if _SSH_MULTIPLEX else [])
        scp_cmd += self._target_flags("-P") + [host_path, f"{self.user}@{self.host}:{remote_path}"]
        result = run_capture(scp_cmd, timeout=30)
        if result.returncode != 0:
            raise _sync_error(f"scp failed: {result.stderr.strip()}", f"File sync to {self.user}@{self.host}")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files in one tar-over-SSH stream: local ``tar c`` piped through one SSH
        connection to remote ``tar x``, after a single batched ``mkdir -p``."""
        if not files:
            return
        base = f"{self._remote_home}/.hermes"
        parents = unique_parent_dirs(files)
        if parents:
            # Directory list goes over stdin, not argv: a real skills tree
            # (thousands of dirs) overflows the remote shell's single-argument
            # limit ("/bin/bash: Argument list too long") and the whole sync
            # then fails and retries on every tool call. (Not run_capture: that
            # closes stdin.)
            result = subprocess.run(
                self._build_ssh_command() + [self._guarded("xargs -0 mkdir -p --")],
                input="\0".join(parents) + "\0", capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30)
            if result.returncode != 0:
                raise _sync_error(f"remote mkdir failed: {result.stderr.strip()}",
                                  f"Remote directory setup on {self.host}")

        # Symlink staging avoids fragile GNU tar --transform rules. On Windows
        # without Developer Mode symlink creation raises OSError winerror 1314;
        # only that case falls back to a plain copy, other OSErrors re-raise.
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-bulk-") as staging:
            for host_path, remote_path in files:
                try:
                    rel_remote = os.path.relpath(remote_path, base)
                except ValueError as exc:
                    raise RuntimeError(f"remote path {remote_path!r} is not under sync base {base!r}") from exc
                if rel_remote == "." or rel_remote.startswith("../"):
                    raise RuntimeError(f"remote path {remote_path!r} escapes sync base {base!r}")
                staged = os.path.join(staging, rel_remote)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError as e:
                    if getattr(e, "winerror", None) != 1314:
                        raise
                    shutil.copy2(host_path, staged)

            # --no-overwrite-dir keeps tar from stamping the staging dir's mode onto
            # existing dirs (e.g. /home/<user>); a umask-002 0775 home breaks sshd StrictModes.
            ssh_cmd = self._build_ssh_command() + [
                self._guarded(f"tar xf - --no-overwrite-dir -C {shlex.quote(base)}")]
            tar_proc = subprocess.Popen(["tar", "-chf", "-", "-C", staging, "."], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ssh_proc = subprocess.Popen(ssh_cmd, stdin=tar_proc.stdout,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception:
                tar_proc.kill()
                tar_proc.wait()
                raise
            tar_proc.stdout.close()  # let tar_proc receive SIGPIPE if ssh_proc exits early
            try:
                _, ssh_stderr = ssh_proc.communicate(timeout=120)
                # communicate() (not wait()) drains stderr so tar can't deadlock on >PIPE_BUF errors.
                if tar_proc.poll() is None:
                    _, tar_stderr_raw = tar_proc.communicate(timeout=10)
                else:
                    tar_stderr_raw = tar_proc.stderr.read() if tar_proc.stderr else b""
            except subprocess.TimeoutExpired:
                for proc in (tar_proc, ssh_proc):
                    proc.kill()
                for proc in (tar_proc, ssh_proc):
                    proc.wait()  # kill both first, then reap: never wait on one while the other blocks
                raise EnvironmentConnectionError(
                    "SSH bulk upload timed out",
                    retry_hint=f"Bulk file sync to {self.host} timed out — check the connection and retry.")
            if tar_proc.returncode != 0:
                raise RuntimeError(f"tar create failed (rc={tar_proc.returncode}): "
                                   f"{tar_stderr_raw.decode(errors='replace').strip()}")
            if ssh_proc.returncode != 0:
                raise _sync_error(f"tar extract over SSH failed (rc={ssh_proc.returncode}): "
                                  f"{ssh_stderr.decode(errors='replace').strip()}",
                                  f"File sync over SSH to {self.host}", what="the connection")
        logger.debug("SSH: bulk-uploaded %d file(s) via tar pipe", len(files))

    def _ssh_bulk_download(self, dest: Path) -> None:
        """Download remote .hermes/ as a tar archive."""
        # Tar from / with the full path so archive entries keep absolute paths
        # (home/user/.hermes/skills/f.py), matching _pushed_hashes keys.
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        # Live sockets inside .hermes (gateway.sock and friends) cannot be archived: tar prints
        # "socket ignored" and some builds exit 2, which failed every sync-back and left a
        # multi-GB temp tar behind on each retry. Exclude them up front.
        ssh_cmd = self._build_ssh_command() + [
            self._guarded(f"tar cf - --exclude='*.sock' -C / {shlex.quote(rel_base)}")]
        with open(dest, "wb") as f:
            result = subprocess.run(ssh_cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.PIPE, timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            # A socket not named *.sock is the only rc=2 we knowingly accept, and only when
            # nothing else was reported — anchored to the diagnostic suffix so a filename merely
            # containing "socket ignored" cannot sneak through. Every other status still fails.
            diagnostic_lines = [line for line in stderr.splitlines() if line.strip()]
            tolerated = result.returncode == 2 and bool(diagnostic_lines) and all(
                line.endswith(": socket ignored") for line in diagnostic_lines)
            if not tolerated:
                raise _sync_error(f"SSH bulk download failed: {stderr}",
                                  f"File sync from {self.host}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        self._run_ssh_checked(quoted_rm_command(remote_paths), 10, "remote rm failed",
                              f"Remote file cleanup on {self.host}")

    def _before_execute(self) -> None:
        if self._sync_manager is not None:
            self._sync_manager.sync()  # rate-limited internally

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Forward the passthrough allowlist (skill ``required_environment_variables`` +
        ``terminal.env_passthrough``) the way docker does: ``SendEnv`` carries the names, the ssh
        client's env carries the values, so secrets never enter the remote ``bash -c`` argv. The
        remote sshd must ``AcceptEnv`` them (#14091). Profile-scoped names missing from the active
        scope are unset remotely so a shared host cannot serve another profile's value."""
        values, unset_names = resolve_passthrough_env(explicit_forward=_placed_lease_env_names(),
                                                      hermes_env_loader=_load_hermes_env_vars)
        token = uuid.uuid4().hex
        remote = _run_preamble(token) + self._guarded(prepend_unset(cmd_string, unset_names))
        cmd = self._build_ssh_command(send_env=values, log_to_file=True) + bash_argv(shlex.quote(remote), login)
        client_env = client_env_with(values)
        proc = _popen_bash(cmd, stdin_data, env=client_env) if client_env is not None else _popen_bash(cmd, stdin_data)
        proc._hermes_run_token = token
        return proc

    def _kill_process(self, proc):
        """Kill the local ssh client, then the REMOTE command's process group. Killing only the
        client leaves the remote ``bash -c`` tree running: without a pty sshd sends it no
        signal, so a silent ``grep -r`` ran on for 30+ h after its tool call timed out (t_44017fbc)."""
        super()._kill_process(proc)
        if token := getattr(proc, "_hermes_run_token", None):
            self._remote_group_kill(token, grace=True)

    def _force_kill_process(self, proc):
        """Hard-exit path: the client dies inline; the KILL-only remote kill runs on a daemon
        thread given 0.25 s to start its ssh child, which then finishes it after this process exits."""
        super()._kill_process(proc)
        if token := getattr(proc, "_hermes_run_token", None):
            t = threading.Thread(target=self._remote_group_kill, args=(token,), kwargs={"grace": False},
                                 daemon=True)
            t.start()
            t.join(0.25)

    def _remote_group_kill(self, token: str, *, grace: bool) -> bool | None:
        """True: a live remote command was signalled; False: none was running; None: unknown."""
        try:
            result = self._run_ssh(_remote_group_kill_command(token, grace=grace), timeout=5)
        except Exception:
            logger.debug("SSH: remote process-group kill failed", exc_info=True)
            return None
        if getattr(result, "returncode", 0) != 0:
            return None
        return _KILLED_MARK in (getattr(result, "stdout", "") or "")

    def _wait_for_process(self, proc, timeout: int = 120, **kwargs) -> dict:
        """An ssh client that exits 255 without the command's cwd marker may have lost its
        connection (master ended, host gone) while the remote command still runs; sshd sends a
        tty-less command no signal, so it would run on unowned (t_db3261ec). Kill its group and say
        so. With nothing left running the result says it was the command's own 255 or a dropped link."""
        result = super()._wait_for_process(proc, timeout, **kwargs)
        token = getattr(proc, "_hermes_run_token", None)
        output = result.get("output") or ""
        if (result.get("returncode") != 255 or not token or self._cwd_marker in output
                or result.get("hermes_interrupted") or result.get("hermes_timed_out")):
            return result
        killed = self._remote_group_kill(token, grace=True)
        if killed is False:
            # No completion marker and nothing left running: the command ran ``exit 255`` itself, or
            # the link dropped and the command then died (a chatty one gets SIGPIPE). Name both.
            note = ("[exit 255 without the completion marker: the command exited 255 itself or the ssh "
                    f"connection to {self.user}@{self.host} closed; no remote process is left]")
            result["output"] = f"{output.rstrip(chr(10))}\n{note}" if output.strip() else note
            return result
        fate = ("the remote command was still running and has been killed" if killed
                else "the remote command's state could not be checked")
        note = f"[ssh connection to {self.user}@{self.host} closed before the command finished (exit 255); {fate}]"
        logger.warning("SSH: %s", note)
        result["output"] = f"{output.rstrip(chr(10))}\n{note}" if output.strip() else note
        result["hermes_connection_lost"] = True
        return result

    def cleanup(self):
        if self._sync_manager:
            logger.info("SSH: syncing files from sandbox...")
            self._sync_manager.sync_back()
        for socket in self._control_sockets():
            if not socket.exists():
                continue
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                cmd = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
            with contextlib.suppress(OSError):
                socket.unlink()
