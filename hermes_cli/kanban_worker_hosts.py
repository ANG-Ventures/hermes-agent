"""Kanban worker placement: run a spilled worker's tools on a pool host.

One board, one dispatcher. A spilled worker is claimed and spawned exactly
like a local one (same process, same pid-anchored owner grant, same kanban
tools on the same ``kanban.db``); only its terminal and file tools run on the
pool host, over the ``ssh`` terminal backend. Which hosts and which cards:
``kanban_worker_pool`` (KWLB v0.1). ``kanban.worker_hosts`` is retired.

The worker process receives ``KANBAN_WORKER_PLACEMENT`` (internal bridge,
JSON of ``TERMINAL_*`` values). ``tools.terminal_tool`` re-applies it after
the config.yaml -> env bridge, so a profile's ``terminal.backend: local``
cannot silently pull a spilled worker back onto the dispatcher host. A
re-apply that fails on a worker placed at boot is LOUD (PRD 5.6, RC-9): one
WARN per run and the ``placement_reapply_failed`` counter on the run.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from typing import Callable, Dict, MutableMapping, Optional

from hermes_cli.kanban_worker_pool import PLACED_EVENT, PoolHost, _ssh_argv  # noqa: F401

PLACEMENT_ENV = "KANBAN_WORKER_PLACEMENT"
# The synthetic placement of a Studio-local worker (``kanban_local_worker_uid``). Defined here,
# in the platform-independent module, so classifying a host never imports the POSIX uid code
# (t_ecfd5591 f6dcfd6d5efb).
LOCAL_HOST_NAME = "studio-local"
REAPPLY_FAILED_KEY = "placement_reapply_failed"

_PROBE_TIMEOUT_SECONDS = 8
_log = logging.getLogger(__name__)


def placement_env(host: PoolHost, workspace: str, *, run_id: Optional[int] = None,
                  log_file: Optional[str] = None) -> Dict[str, str]:
    """The ``TERMINAL_*`` values a spilled worker's tools must run under."""
    values = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": host.ssh_host,
        "TERMINAL_SSH_USER": host.ssh_user,
        "TERMINAL_SSH_PORT": str(host.ssh_port),
        "TERMINAL_CWD": workspace,
        # Every profile shares the one kanbanw login: no ~/.hermes sync up
        # (credentials stay on the Studio, PRD 5.8) and none back down into a
        # profile from a tree another profile wrote.
        "TERMINAL_SSH_SYNC_FILES": "false",
    }
    # N workers placed on one host in one tick shared one ControlMaster socket
    # and raced to create it; the loser printed "ControlSocket ... already
    # exists" into its tool output (t_3d487402). One socket per run.
    if run_id is not None:
        values["TERMINAL_SSH_CONTROL_SCOPE"] = f"run{int(run_id)}"
    # ssh's own diagnostics go to the worker's run log, never a tool result.
    if log_file:
        values["TERMINAL_SSH_LOG_FILE"] = str(log_file)
    return values


def apply_placement(env: Dict[str, str], host: PoolHost, workspace: str, *,
                    run_id: Optional[int] = None, log_file: Optional[str] = None) -> Dict[str, str]:
    values = placement_env(host, workspace, run_id=run_id, log_file=log_file)
    env.update(values)
    env[PLACEMENT_ENV] = json.dumps({"host": host.name, "env": values}, sort_keys=True)
    return values


class RemoteWorkspaceUnavailable(RuntimeError):
    """The pool host answered but could not create the card's workspace (no
    board mount, a root-owned parent): a HOST fault, never the card's
    (t_9fb4d477). The dispatcher closes the run ``infrastructure: true``."""

    def __init__(self, host: str, board: Optional[str], message: str):
        super().__init__(message)
        self.host, self.board = host, board


_BOARD_ROOT_RC = 86
# t_69e47673: a card's workspace on a POOL host is created 0770 (kanbanw + the board root's setgid group, never
# world-searchable): nothing but the worker uid reads it, and a 0775 tree put ~2M dirs inside the ci-box prism lane
# reset walk; a 0770 dir is pruned at depth 1. `mkdir -m` is umask-independent and touches the LAST component only,
# so an existing dir (a re-run, the board root) keeps its mode. The Studio local-uid hop keeps the umask (its access
# is the scratch-root ACL, kanbanw-studio-provision.sh).
WORKSPACE_MODE = "0770"


def _mkdir_mode(host: PoolHost) -> str:
    return f"-m {WORKSPACE_MODE} " if host.name != LOCAL_HOST_NAME else ""


class WorkerGitIdentityRefused(RuntimeError):
    """The pool host's worker shell would commit as an identity the fleet's
    pr-commit-identity policy refuses (t_db46f06b): a HOST fault, never the
    card's. 10-27 23:04 ace-media's kanbanw ~/.gitconfig turned into
    ``ci <ci@example.invalid>`` and every worker placed there produced a PR that
    could never land (hermes-home#7516, #7520), found 40 min later in CI."""

    def __init__(self, host: str, board: Optional[str], email: str):
        super().__init__(f"worker host {host}: git identity {email or 'UNSET'} is not admitted by "
                         f"identity-policy.sh; refusing to start a worker there "
                         f"(fix: fleet-host onboard {host} --fix, column K29)")
        self.host, self.board, self.email = host, board, email


# The fleet policy (hermes-home scripts/git-hooks/identity-policy.sh) under the running root. Absent = no policy on
# this install: the identity read is skipped, so a non-fleet install never refuses a host.
IDENTITY_POLICY_REL = os.path.join("scripts", "git-hooks", "identity-policy.sh")
_IDENT_MARK = "@@hermes-git-ident"
# The worker's terminal is a login shell (the ssh backend snapshots `bash -l`), so the identity it commits with is
# `git var` in a login shell: worker-git-identity.sh's exported env beats ~/.gitconfig. Bounded so a slow profile
# never fails the mkdir it rides on; an unread identity never refuses (only a READ, unadmitted one does). The
# shell's rc is printed: 128 is git itself refusing (no identity configured: a read), anything else is unread.
_IDENT_CMD = (f"printf '%s\\n' {_IDENT_MARK}; timeout 10 bash -lc 'cd /tmp && git var GIT_AUTHOR_IDENT && "
              "git var GIT_COMMITTER_IDENT' </dev/null 2>/dev/null; echo \"RC=$?\"")
_IDENT_TIMEOUT_SECONDS = 25


def identity_policy_path() -> Optional[str]:
    try:
        from hermes_constants import get_default_hermes_root
        path = os.path.join(str(get_default_hermes_root()), IDENTITY_POLICY_REL)
    except Exception:
        return None
    return path if os.path.isfile(path) else None


def identity_email_allowed(policy: str, email: str) -> bool:
    """``identity_email_allowed`` from the policy file itself (one list, no copy). Fails closed. Not cached: a
    policy edit must take effect on the next spawn in a running dispatcher, both ways (Prism 583e98241dfc)."""
    try:
        rc = subprocess.run(["bash", "-c", '. "$1" && identity_email_allowed "$2"', "_", policy, email],
                            capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


def _ident_emails(stdout: str) -> Optional[list]:
    """Author + committer emails after the marker; None when the identity was not read. A read with no
    identity (git var refused: no user.email) is [""], which nothing admits."""
    _pre, sep, tail = (stdout or "").partition(_IDENT_MARK + "\n")
    if not sep:
        return None
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    rc = lines[-1][3:] if lines and lines[-1].startswith("RC=") else None
    if rc == "128":
        return [""]                        # git: "Author identity unknown" -- it would refuse (or mint user@host)
    if rc != "0":
        return None                        # timeout, no bash, a broken profile, a truncated read: proves nothing
    import re
    emails = [m.group(1) for ln in lines[:-1] for m in [re.search(r"<([^>]*)>\s+\d+\s+[-+]\d{4}$", ln)] if m]
    return emails if len(emails) == 2 else None


def prepare_remote_workspace(host: PoolHost, workspace: str,
                             runner: Callable = subprocess.run, *,
                             board: Optional[str] = None) -> None:
    """Create the card's workspace at the same absolute path on the host and,
    on a pool host under the fleet identity policy, refuse one whose worker
    shell would commit as an unadmitted identity (t_db46f06b)."""
    # rc _BOARD_ROOT_RC only when the BOARD root itself is unusable (missing,
    # unwritable): a card-specific failure (its path exists as a file) stays a
    # card failure and is charged (Prism d72c091b628d).
    w, b = shlex.quote(workspace), shlex.quote(os.path.dirname(workspace.rstrip("/")) or "/")
    policy = identity_policy_path() if host.name != LOCAL_HOST_NAME else None
    ok_tail = f"{{ {_IDENT_CMD}; exit 0; }}" if policy else "exit 0"
    proc = runner(
        # The root is proven by CREATING under it (ENOSPC / inodes / quota pass
        # an access test; Prism d762ee26a639), then the probe dir is removed.
        _ssh_argv(host, f"mkdir -p {_mkdir_mode(host)}-- {w} && {ok_tail}; "
                        f"t=$(mktemp -d {b}/.hermes-root-probe.XXXXXX 2>/dev/null) && "
                        f"{{ rmdir -- \"$t\"; exit 1; }}; exit {_BOARD_ROOT_RC}"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_IDENT_TIMEOUT_SECONDS if policy else _PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        msg = (f"worker host {host.name}: mkdir {workspace} failed: "
               f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
        if rc != _BOARD_ROOT_RC or not board:
            raise RuntimeError(msg)  # ssh failed, a card-path fault, or no pool board: not a board fact
        raise RemoteWorkspaceUnavailable(host.name, board, msg)
    if policy:
        emails = _ident_emails(getattr(proc, "stdout", "") or "")
        if emails is None:
            _log.warning("kanban pool: %s worker git identity not read; not refusing", host.name)
            return
        bad = next((e for e in emails if not identity_email_allowed(policy, e)), None)
        if bad is not None:
            raise WorkerGitIdentityRefused(host.name, board, bad)


_identity_paged: Dict[str, float] = {}


def note_identity_fault(host: str, board: Optional[str], why: str) -> bool:
    """Route placement around the host (per board, ``BOARD_FAULT_SECONDS``) and page ONCE per host per
    quarantine window (#alerts, warn). True when a page was sent."""
    import time as _time
    from hermes_cli import kanban_worker_pool as _kwp

    if board:
        _kwp.mark_board_fault(host, board, why[:200])
    _log.warning("kanban pool: %s refused (no retry charged): %s", host, why)
    now = _time.monotonic()
    if now < _identity_paged.get(host, 0.0):
        return False
    _identity_paged[host] = now + _kwp.BOARD_FAULT_SECONDS
    from hermes_cli import kanban_budget as _kbudget

    script = _kbudget._notify_script_path()
    if script is None:
        return False  # a hermetic home never pages
    import sys
    return _kbudget._run_notify([sys.executable, script, "--sev", "warn", "--send", f"🪪 {why[:300]}"]) is not False


def note_board_fault(host: str, board: Optional[str], why: str) -> bool:
    """Quarantine the (host, board) pair for ``BOARD_FAULT_SECONDS`` and page
    ONCE per quarantine episode (#alerts, warn). True when a page was sent."""
    from hermes_cli import kanban_worker_pool as _kwp

    if not board:
        return False
    fresh = _kwp.board_fault(host, board) is None
    _kwp.mark_board_fault(host, board, why[:200])
    _log.warning("kanban pool: %s cannot host board %s for %ds (no retry charged): %s",
                 host, board, _kwp.BOARD_FAULT_SECONDS, why)
    if not fresh:
        return False
    from hermes_cli import kanban_budget as _kbudget

    script = _kbudget._notify_script_path()
    if script is None:
        return False  # a hermetic home never pages
    body = (f"🧱 Kanban pool host {host} cannot host board '{board}': {why[:200]}. "
            f"Placement skips {host} for that board for {_kwp.BOARD_FAULT_SECONDS // 60} min and the card "
            f"keeps its retries. Fix: `fleet-host board-mounts {host} --apply` (creates the per-board "
            f"workspace mount).")
    import sys
    return _kbudget._run_notify([sys.executable, script, "--sev", "warn", "--send", body]) is not False


def local_workspace_has_content(path: Optional[str]) -> bool:
    """True when a card's LOCAL workspace may hold files.

    Those files would not exist on the worker host, so such a card is not
    spilled. Only an unset or missing path counts as empty; any other scan
    failure (permissions, I/O) fails closed.
    """
    if not path:
        return False
    try:
        # The canonical key (spec §2.4.1): a stored ``~/x`` scans the expanded
        # directory, not a literal ``~`` that never exists.
        from hermes_cli.kanban_db_workspace import canon
        path = canon(path)
    except (OSError, RuntimeError, ValueError):
        return True
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def remote_workspace_has_content(placement_raw: Optional[str],
                                 runner: Callable = subprocess.run) -> bool:
    """True unless the placed host PROVES this run's workspace empty or absent.

    Workspaces are host-local (``TERMINAL_SSH_SYNC_FILES=false``), so a run
    re-routed elsewhere would leave anything here behind (t_ecfd5591
    0e9694bf2944). Any doubt (no placement, no path, ssh failure) is content.
    """
    if not placement_raw:
        return True
    try:
        env = json.loads(placement_raw).get("env") or {}
        workspace, ssh_host = env.get("TERMINAL_CWD"), env.get("TERMINAL_SSH_HOST")
        if not workspace or not ssh_host:
            return True
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]
        if str(env.get("TERMINAL_SSH_PORT") or "22") != "22":
            argv += ["-p", str(env["TERMINAL_SSH_PORT"])]
        target = f"{env['TERMINAL_SSH_USER']}@{ssh_host}" if env.get("TERMINAL_SSH_USER") else ssh_host
        # rc 0 = empty or proven absent, rc 3 = has entries; anything else (ssh 255, perms, I/O)
        # fails closed. ``[ -e ]`` is false for ENOENT, for a dangling link AND for a path under
        # an unsearchable parent, so absence is proven only when the nearest existing ancestor is
        # a searchable, readable directory (t_ecfd5591 parent-custody-permissions); a dangling
        # link stops the climb and counts as content. Presence is ``find``'s first entry printed
        # with its "$w/" prefix, so no filename (newline, blank) can read as empty, and a listing
        # error is find's own nonzero exit (24a15e048a61).
        script = (f"w={shlex.quote(workspace)}; p=$w; c=; "
                  'while [ ! -e "$p" ] && [ ! -L "$p" ]; do q=$(dirname -- "$p"); '
                  '[ "$q" = "$p" ] && exit 5; c=$(basename -- "$p"); p=$q; done; '
                  'if [ -z "$c" ]; then [ -d "$w" ] && [ ! -L "$w" ] || exit 3; '
                  'o=$(find "$w" -mindepth 1 -maxdepth 1 -print -quit) || exit 4; '
                  '[ -z "$o" ] && exit 0; exit 3; fi; '
                  '[ -d "$p" ] && [ -x "$p" ] && [ -r "$p" ] || exit 5; exit 0')
        proc = runner(argv + [target, script],
                      capture_output=True, text=True, encoding="utf-8", timeout=_PROBE_TIMEOUT_SECONDS,
                      stdin=subprocess.DEVNULL)
        return getattr(proc, "returncode", 1) != 0
    except Exception:
        _log.warning("placement custody probe failed; treating the workspace as holding work",
                     exc_info=True)
        return True


def _placed_host(raw: Optional[str]) -> Optional[str]:
    try:
        return str(json.loads(raw or "").get("host") or "") or None
    except (TypeError, ValueError, AttributeError):
        return None


def is_pool_host(host: Optional[str]) -> bool:
    """True for a real pool placement. The synthetic local-worker placement
    (``studio-local``) runs on the Studio itself, so every placement gate and the
    exit seam treat it as unplaced: one classification (t_ecfd5591 03985b00389e)."""
    return bool(host) and host != LOCAL_HOST_NAME


# Boot record (RC-9): the placement this worker process was spawned with.
# ``capture_boot_placement`` runs from ``main()`` before env files or the
# config bridge can touch the environment; the import-time read below only
# covers an entry point that skips ``main()``. First captured host wins and
# lives here, independent of the (mutable) environment.
BOOT_PLACED_HOST: Optional[str] = None


def capture_boot_placement(environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    global BOOT_PLACED_HOST
    if BOOT_PLACED_HOST is None:
        env = os.environ if environ is None else environ
        BOOT_PLACED_HOST = _placed_host(env.get(PLACEMENT_ENV))
    return BOOT_PLACED_HOST


capture_boot_placement()
_reapply = {"failed": 0, "warned": False}


def reapply_placement_env(environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """Re-assert a dispatcher placement over config-bridged ``TERMINAL_*``.

    Returns the host name when a placement was applied, else None.
    """
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    raw = env.get(PLACEMENT_ENV)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        values = data.get("env") or {}
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(values, dict):
        return None
    pending = {}
    for key, value in values.items():
        if not (isinstance(key, str) and key.startswith("TERMINAL_")):
            continue
        if not isinstance(value, str):
            # A malformed placement is NOT applied: returning the host here
            # would read as success to reapply_or_record (Prism 361860f7dee5).
            return None
        pending[key] = value
    if not pending:
        # A placement that names a host but carries no TERMINAL_* mapping
        # applied nothing: not a success (Prism r8 "false success" :134).
        return None
    env.update(pending)
    return str(data.get("host") or "") or None


def reapply_or_record(tool: str, environ: Optional[MutableMapping[str, str]] = None,
                      *, boot_host: Optional[str] = None) -> Optional[str]:
    """``reapply_placement_env`` for a tool bridge, loud on a placed worker.

    A worker placed at boot whose re-apply RAISES or returns None ran this
    call on the Studio: WARN once per run and bump the run's counter. An
    unplaced worker ignores a None re-apply (the normal local case).
    """
    boot = BOOT_PLACED_HOST if boot_host is None else boot_host
    why = "placement env missing"
    try:
        host = reapply_placement_env(environ)
    except Exception as exc:  # the counter is the point; never break the tool
        host, why = None, f"{type(exc).__name__}: {exc}"
    if host is not None or not boot:
        return host
    _reapply["failed"] += 1
    if not _reapply["warned"]:
        _reapply["warned"] = True
        _log.warning(
            "kanban worker placement re-apply failed on %s: %s; this call ran on the Studio",
            tool, why,
        )
    _record_reapply_failure(_reapply["failed"])
    return None


def reapply_failures() -> int:
    return int(_reapply["failed"])


def _record_reapply_failure(count: int) -> None:
    """Write the counter onto THIS run's metadata now (survives an abrupt
    death); ``kanban_db._end_run`` carries the key through the run's close."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
    except ValueError:
        return
    if not task_id:
        return
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            kb.merge_run_metadata(conn, run_id, {REAPPLY_FAILED_KEY: int(count)},
                                  keep_max=(REAPPLY_FAILED_KEY,))
        finally:
            conn.close()
    except Exception:
        _log.debug("could not record %s on run %s", REAPPLY_FAILED_KEY, run_id, exc_info=True)
