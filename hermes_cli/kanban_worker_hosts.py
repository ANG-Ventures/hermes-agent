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
import re
import shlex
import subprocess
from typing import Callable, Dict, List, Mapping, MutableMapping, Optional, Tuple

from hermes_cli.kanban_worker_pool import PLACED_EVENT, PoolHost, _ssh_argv  # noqa: F401
from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

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
        # Every profile shares the host's one worker login: no ~/.hermes sync up
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


def prepare_remote_workspace(host: PoolHost, workspace: str,
                             runner: Callable = subprocess.run, *,
                             board: Optional[str] = None) -> None:
    """Create the card's workspace at the same absolute path on the host."""
    # rc _BOARD_ROOT_RC only when the BOARD root itself is unusable (missing,
    # unwritable): a card-specific failure (its path exists as a file) stays a
    # card failure and is charged (Prism d72c091b628d).
    w, b = shlex.quote(workspace), shlex.quote(os.path.dirname(workspace.rstrip("/")) or "/")
    proc = runner(
        # The root is proven by CREATING under it (ENOSPC / inodes / quota pass
        # an access test; Prism d762ee26a639), then the probe dir is removed.
        _ssh_argv(host, f"mkdir -p {_mkdir_mode(host)}-- {w} && exit 0; "
                        f"t=$(mktemp -d {b}/.hermes-root-probe.XXXXXX 2>/dev/null) && "
                        f"{{ rmdir -- \"$t\"; exit 1; }}; exit {_BOARD_ROOT_RC}"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        msg = (f"worker host {host.name}: mkdir {workspace} failed: "
               f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
        if rc != _BOARD_ROOT_RC or not board:
            raise RuntimeError(msg)  # ssh failed, a card-path fault, or no pool board: not a board fact
        raise RemoteWorkspaceUnavailable(host.name, board, msg)


class WorkerGitIdentityRefused(RuntimeError):
    """The pool host's worker shell would commit as an identity the fleet's pr-commit-identity policy refuses, and
    the host ALONE resolves a refused one (t_db46f06b): a HOST fault. 10-27 23:04 ace-media's kanbanw ~/.gitconfig
    turned into ``ci <ci@example.invalid>`` and every worker placed there produced a PR that could never land
    (hermes-home#7516, #7520), found 40 min later in CI."""

    def __init__(self, host: str, board: Optional[str], email: str):
        super().__init__(f"worker host {host}: git identity {email or 'UNSET'} is not admitted by "
                         f"identity-policy.sh; refusing to start a worker there "
                         f"(fix: fleet-host onboard {host} --fix, column K29)")
        self.host, self.board, self.email = host, board, email


class WorkerGitIdentityProfileRefused(RuntimeError):
    """The refused identity is the assignee PROFILE's: the same worker shell without the profile's forwarded git
    names is admitted (Prism f57827c258e9 / 39ff2e41e51a / b381c849ec78). A CARD fault: the retry is charged and
    no host is quarantined or paged, so one bad profile .env cannot fence every pool host off the board."""

    def __init__(self, host: str, board: Optional[str], email: str, profile: Optional[str], variable: str):
        super().__init__(f"worker profile {profile or '?'}: forwarded {variable}={email or 'UNSET'} is not admitted "
                         f"by identity-policy.sh (host {host}'s own identity is admitted); refusing to start the "
                         f"worker (fix: {variable} in profile {profile or '?'}'s env / .env / process_env_files)")
        self.host, self.board, self.email, self.profile, self.variable = host, board, email, profile, variable


# The fleet policy (hermes-home scripts/git-hooks/identity-policy.sh) under the running root. Absent = no policy on
# this install: the identity probe is skipped, so a non-fleet install never refuses a host.
IDENTITY_POLICY_REL = os.path.join("scripts", "git-hooks", "identity-policy.sh")
# One child boot (~2-3 s) + the ssh terminal's connect + login-shell snapshot + git var.
_IDENT_PROBE_TIMEOUT_SECONDS = 60


class WorkerIdentityProbeError(RuntimeError):
    """The probe booted but its ssh terminal could not launch (Prism f7be64ebab1a: ``ssh -E <log>`` with no log dir
    fails before the remote command runs). The worker's terminal would fail the same way, so the spawn is refused
    with the reason, never admitted as "unread"."""


def worker_spawn_cwd(workspace: str) -> Optional[str]:
    """The cwd a worker (and its identity probe) is launched in: the workspace when it exists, else inherited.
    ONE selection for both launches (Prism a924d74cd9c0): ``agent.process_env_files`` resolves relative paths
    against cwd, so a probe in another cwd can read a different overlay than the worker loads."""
    return workspace if os.path.isdir(workspace) else None


def identity_policy_path() -> Optional[str]:
    try:
        from hermes_constants import get_default_hermes_root
        path = os.path.join(str(get_default_hermes_root()), IDENTITY_POLICY_REL)
    except Exception:
        return None
    return path if os.path.isfile(path) else None


def identity_email_allowed(policy: str, email: str) -> bool:
    """``identity_email_allowed`` from the policy file itself (one list, no copy). Fails closed. Not cached: a
    policy edit takes effect on the next spawn in a running dispatcher, both ways (Prism 583e98241dfc)."""
    try:
        rc = subprocess.run(["bash", "-c", '. "$1" && identity_email_allowed "$2"', "_", policy, email],
                            capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


def _run_ident_probe(profile: str, workspace: str, env: Mapping[str, str], *, without_profile_git: bool,
                     runner: Callable) -> Optional[dict]:
    """The worker's own terminal, booted as the worker boots, prints its commit identity (kanban_worker_ident_probe).
    None when the probe itself did not answer (boot failure, timeout): that proves nothing about the identity."""
    import sys
    from hermes_cli import kanban_worker_ident_probe as _probe
    argv = [sys.executable, "-P", "-m", "hermes_cli.kanban_worker_ident_probe", "--profile", profile,
            "--workspace", workspace]
    if without_profile_git:
        argv.append("--without-profile-git")
    child_env = dict(env)
    # The code that decides is the code that probes: pin the child to this dispatcher's tree.
    child_env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=_IDENT_PROBE_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, env=child_env,
                      cwd=worker_spawn_cwd(workspace))
    except (OSError, subprocess.SubprocessError):
        _log.warning("kanban pool: identity probe for %s did not run", profile, exc_info=True)
        return None
    for line in reversed((getattr(proc, "stdout", "") or "").splitlines()):
        if line.startswith(_probe.MARK):
            try:
                return json.loads(line[len(_probe.MARK):])
            except ValueError:
                return None
    _log.warning("kanban pool: identity probe for %s answered nothing (rc=%s): %s", profile,
                 getattr(proc, "returncode", None), (getattr(proc, "stderr", "") or "").strip()[-300:])
    return None


def check_worker_identity(host: PoolHost, workspace: str, env: Mapping[str, str], profile: str, *,
                          board: Optional[str] = None, runner: Callable = subprocess.run) -> None:
    """Refuse a spawn whose worker would commit as an identity the fleet policy refuses (t_db46f06b).

    ``env`` is the env the worker is about to be launched with, placement applied. The probe launches the worker's
    real terminal from it (no model of the worker's environment); a refusal is re-probed WITHOUT the profile's
    forwarded git names: refused there too = the host's fault, admitted there = the profile's. An unread identity
    never refuses."""
    policy = identity_policy_path() if host.name != LOCAL_HOST_NAME else None
    if policy is None:
        return
    first = _run_ident_probe(profile, workspace, env, without_profile_git=False, runner=runner)
    if (first or {}).get("error"):
        raise WorkerIdentityProbeError(f"worker host {host.name}: identity probe error, refusing to start a "
                                       f"worker: {first['error']}")
    emails = (first or {}).get("emails")
    if emails is None:
        _log.warning("kanban pool: %s worker git identity not read; not refusing", host.name)
        return
    bad = next((e for e in emails if not identity_email_allowed(policy, e)), None)
    if bad is None:
        return
    forwarded = (first or {}).get("forwarded") or {}
    if forwarded:
        host_only = _run_ident_probe(profile, workspace, env, without_profile_git=True, runner=runner)
        host_emails = (host_only or {}).get("emails")
        if host_emails is not None and all(identity_email_allowed(policy, e) for e in host_emails):
            var = next((n for n in sorted(forwarded) if forwarded[n] == bad), None) or "/".join(sorted(forwarded))
            raise WorkerGitIdentityProfileRefused(host.name, board, bad, profile, var)
    raise WorkerGitIdentityRefused(host.name, board, bad)


_identity_paged: Dict[str, float] = {}


def note_identity_fault(host: str, board: Optional[str], why: str) -> bool:
    """Route placement around the host (per board, ``BOARD_FAULT_SECONDS``) and page ONCE per host per
    quarantine window (#alerts, warn). True when a page was sent. The once-latch is set only by a page that
    went out (Prism 5a786096bb00): a failed send is retried on the next refusal, never silently swallowed."""
    import time as _time
    from hermes_cli import kanban_worker_pool as _kwp

    if board:
        _kwp.mark_board_fault(host, board, why[:200])
    _log.warning("kanban pool: %s refused (no retry charged): %s", host, why)
    now = _time.monotonic()
    if now < _identity_paged.get(host, 0.0):
        return False
    from hermes_cli import kanban_budget as _kbudget

    script = _kbudget._notify_script_path()
    if script is None:
        return False  # a hermetic home never pages
    import sys
    sent = _kbudget._run_notify([sys.executable, script, "--sev", "warn", "--send", f"🪪 {why[:300]}"]) is not False
    if sent:
        _identity_paged[host] = now + _kwp.BOARD_FAULT_SECONDS
    else:
        _log.error("kanban pool: identity page for %s NOT sent; will retry on the next refusal: %s", host, why)
    return sent


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


# local_files binds only on content the remote cannot reproduce (t_e7c03f48): t_e32e34f0's Studio scratch dir held a
# clean worktree whose HEAD was the pushed PR head plus logs and a patch, and that alone kept it Studio-only for 2 h+
# while the pool sat idle. A placed run starts from a fresh directory on the pool host (workspaces are host-local,
# TERMINAL_SSH_SYNC_FILES=false) and re-clones from the remote; the Studio directory is left in place, untouched.
WORKSPACE_THROWAWAY_GLOBS = ("*.log", "*.patch", "pr-body.md")
WORKSPACE_SCAN_MAX_ENTRIES = 2000
WORKSPACE_GIT_TIMEOUT_SECONDS = 20
WORKSPACE_LS_REMOTE_TIMEOUT_SECONDS = 10
# Only negative/unverified remote-containment answers are cached, keyed by checkout and exact HEAD sha.
# Positive containment is re-proven every call: its only remote ref can be deleted inside the TTL. The local half
# (``git status``) also runs every call: checkout edits never touch the workspace root's mtime (5c1daae965e0).
WORKSPACE_BINDING_TTL_SECONDS = 120
_binding_cache: Dict[tuple, tuple] = {}


def _throwaway(name: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatchcase(name, g) for g in WORKSPACE_THROWAWAY_GLOBS)


def _show(rel: str) -> str:
    """``rel`` safe for a one-line reason detail (no parens, no control characters), bounded."""
    text = "".join(c if c.isprintable() and c not in "()" else "?" for c in rel)
    return text[:200] or "."


def _remote_contains(git: Callable, head: str) -> Optional[bool]:
    """True when HEAD equals, or is an ancestor of, a branch/tag tip a remote reports NOW (``git ls-remote``) and
    that tip is present locally; False when every remote answered, every tip is local, and none contains HEAD; None
    when containment cannot be proven (no remote, ls-remote failed or timed out, a tip is not present locally).
    Local remote-tracking refs are never consulted: a branch deleted on the remote leaves them behind (Prism
    1c3c50990907)."""
    rem = git("remote", timeout=WORKSPACE_GIT_TIMEOUT_SECONDS)
    names = (rem.stdout or b"").decode("utf-8", "replace").split() if rem.returncode == 0 else []
    if not names:
        return None
    tips: set = set()
    for name in names:
        ls = git("ls-remote", "--heads", "--tags", name, timeout=WORKSPACE_LS_REMOTE_TIMEOUT_SECONDS)
        if ls.returncode != 0:
            return None
        tips.update(line.split()[0] for line in (ls.stdout or b"").decode("ascii", "replace").splitlines()
                    if line.strip())
    if head in tips:
        return True
    if not tips:
        return False
    order = sorted(tips)
    bc = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
             input="".join(f"{x}^{{commit}}\n" for x in order).encode())
    if bc.returncode != 0:
        return None
    out = (bc.stdout or b"").decode("ascii", "replace").splitlines()
    present = [x for x, line in zip(order, out) if line.endswith(" commit")]
    unknown = len(present) < len(order)  # a tip we never fetched: it might contain HEAD
    if present:
        rl = git("rev-list", "-n1", head, "--stdin", input="".join(f"^{x}\n" for x in present).encode())
        if rl.returncode != 0:
            return None
        if not (rl.stdout or b"").strip():
            return True  # HEAD is reachable from a tip the remote reports now
    return None if unknown else False


def _git_binding(repo: str, rel: str, runner: Callable, t: float) -> Optional[str]:
    """The first thing in git checkout ``repo`` the remote cannot reproduce, or None: a dirty tracked path (submodule
    edits included, whatever ``submodule.<x>.ignore`` says), an untracked file outside
    :data:`WORKSPACE_THROWAWAY_GLOBS`, a HEAD no remote tip contains (``@unpushed:<sha>``), or a HEAD whose
    containment the remote could not confirm now (``remote-unverified: <rel>``). Ignored files are the repo's own
    throwaway list. Any git failure binds (fail closed)."""
    from hermes_cli._subprocess_compat import noninteractive_git_env

    env = noninteractive_git_env(os.environ)
    # Every call here asks what THIS checkout holds. In a partial clone a tip the store lacks would otherwise be
    # fetched object by object from the promisor (t_3833cb99: 12 of 5,078 tips answered in 120 s); missing is the
    # answer (`_remote_contains` -> None), never a reason to fetch.
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_NO_LAZY_FETCH": "1"})
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)

    def git(*args, timeout=WORKSPACE_GIT_TIMEOUT_SECONDS, input=None):
        io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        return runner(["git", "-C", repo, *args], capture_output=True, env=env, timeout=timeout, **io)

    def join(p: str) -> str:
        return p if rel in ("", ".") else f"{rel}/{p}"

    try:
        st = git("-c", "status.showUntrackedFiles=all", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                 "--ignore-submodules=none")
        if st.returncode != 0:
            return f"{rel or '.'}@git-status-failed"
        recs = (st.stdout or b"").split(b"\0")
        i = 0
        while i < len(recs):
            rec = recs[i].decode("utf-8", "replace")
            i += 1
            if len(rec) < 4:
                continue
            code, path = rec[:2], rec[3:]
            if code[0] in "RC":
                i += 1  # the rename/copy source record follows
            if code == "??" and _throwaway(os.path.basename(path.rstrip("/"))):
                continue
            return join(path)
        hp = git("rev-parse", "--verify", "HEAD")
        head = (hp.stdout or b"").decode("ascii", "replace").strip()
        if hp.returncode != 0 or not head:
            return f"{rel or '.'}@git-head-unreadable"
        key = (os.path.realpath(repo), head)
        hit = _binding_cache.get(key)
        if hit is not None and t - hit[0] < WORKSPACE_BINDING_TTL_SECONDS:
            contained = hit[1]
        else:
            try:
                contained = _remote_contains(git, head)
            except (OSError, subprocess.SubprocessError, ValueError):
                contained = None
            if contained is not True:  # cache only fail-closed answers; a containing remote ref can vanish now
                if len(_binding_cache) > 4096:
                    _binding_cache.clear()
                _binding_cache[key] = (t, contained)
        if contained is None:
            return f"remote-unverified: {rel or '.'}"
        if not contained:
            return f"{rel or '.'}@unpushed:{head[:12]}"
    except (OSError, subprocess.SubprocessError, ValueError):
        return f"{rel or '.'}@git-failed"
    return None


def local_workspace_binding(path: Optional[str], *, runner: Callable = subprocess.run,
                            now: Optional[float] = None) -> Optional[str]:
    """None when the card's LOCAL workspace holds nothing a pool host cannot reproduce; else the first binding
    path, relative to the workspace (the ``local_files`` reason detail).

    Reproducible = (a) a git checkout that is clean (no dirty tracked path, no untracked file outside
    :data:`WORKSPACE_THROWAWAY_GLOBS`) with HEAD contained in a live remote tip, and (b) files matching
    :data:`WORKSPACE_THROWAWAY_GLOBS`. Anything else binds: another file, a symlink, a special file, an unpushed
    HEAD, a HEAD the remote cannot confirm now, a scan or git error, more than :data:`WORKSPACE_SCAN_MAX_ENTRIES`
    entries outside checkouts. An empty or missing workspace is None (:func:`local_workspace_has_content`). The
    scan and ``git status`` run on every call; only negative/unverified remote answers are cached (per HEAD sha,
    for :data:`WORKSPACE_BINDING_TTL_SECONDS`). Positive containment is always re-proven.
    """
    import time as _time
    if not local_workspace_has_content(path):
        return None
    try:
        from hermes_cli.kanban_db_workspace import canon
        root = str(canon(path))
    except (OSError, RuntimeError, ValueError):
        return "."
    t = _time.monotonic() if now is None else now
    seen = [0]

    def walk(d: str, rel: str) -> Optional[str]:
        if os.path.lexists(os.path.join(d, ".git")):
            return _git_binding(d, rel, runner, t)
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return rel or "."
        for e in entries:
            r = e.name if not rel else f"{rel}/{e.name}"
            seen[0] += 1
            if seen[0] > WORKSPACE_SCAN_MAX_ENTRIES:
                return f"{r}@scan-limit"
            try:
                if e.is_symlink():
                    return r
                if e.is_dir(follow_symlinks=False):
                    sub = walk(e.path, r)
                    if sub is not None:
                        return sub
                elif not (e.is_file(follow_symlinks=False) and _throwaway(e.name)):
                    return r
            except OSError:
                return r
        return None

    verdict = walk(root, "")
    return None if verdict is None else _show(verdict)


# local_files continuity overflow (t_e604c0f9): a card whose Studio scratch dir holds files runs on the Studio while
# its gate admits; when it does not, the dir is carried to the chosen pool host (same path) and the card runs there.
# The bounds keep the in-tick scan and copy short; they are a cost bound, not a capacity count: a bigger dir keeps
# the old rule (Studio or wait).
# t_f3a411c2: these two caps and the in-tick copy now apply only under ROOT kanban.placement.carry_caps: true (the
# kill switch); by default the carry has no size cap and runs off the tick (kanban_carry_background).
CARRY_MAX_BYTES = 1024 ** 3
CARRY_MAX_ENTRIES = 20000
CARRY_TIMEOUT_SECONDS = 300
CARRY_SCAN_TTL_SECONDS = 300
# Wall bounds on the in-tick size census (t_915b8d2d): on a loaded Studio one walk took minutes and every ready
# local_files row walked in the routing pass, so a tick ran 4-30 min and new portable cards waited for it. A path
# whose walk outlives CARRY_SCAN_MAX_SECONDS is refused (cached, like any refusal); once a tick has spent
# CARRY_TICK_SCAN_SECONDS on walks, the rest of its rows are refused uncached and walked on a later tick.
CARRY_SCAN_MAX_SECONDS = 2.0
CARRY_TICK_SCAN_SECONDS = 5.0
# The spawn-time re-walk of a set-aside path carried as files (outside the tick, before an rsync of up to
# CARRY_TIMEOUT_SECONDS): long enough for any tree under the caps, so a big one does not fail the spawn.
CARRY_SPAWN_WALK_SECONDS = 30.0
CARRY_WAIT_FORGET_SECONDS = 900
CARRIED_EVENT = "workspace_carried"
# portable() bind rule of a carried card: once carried, every later run binds to the host holding the copy
# (custody, independent of placement.continuation); the Studio original is never run against or carried again.
CARRY_BIND_RULE = "workspace_carry"
_carry_scan_cache: Dict[tuple, tuple] = {}
_carry_clone_cache: Dict[tuple, tuple] = {}  # skip_clones census key -> clone paths it skipped (t_9965b8d5)
_carry_split_usage: Dict[object, tuple] = {}  # root -> (bytes, entries, caps) of the last skip_clones census (F4)
_carry_worktree_cache: Dict[object, tuple] = {}  # root -> linked-worktree rels the last skip_clones census set aside
_worktree_usage_cache: Dict[str, tuple] = {}  # worktree realpath -> (at, (bytes, entries) or None)
_carry_setaside_cache: Dict[object, tuple] = {}  # root -> every clone/worktree rel the last skip_clones census set aside
_worktree_partial: Dict[str, tuple] = {}  # worktree realpath -> (at, pending dirs, bytes, entries) of a timed-out walk
_carry_waiting: Dict[tuple, tuple] = {}  # cache key -> (budget generation first deferred, last asked); FIFO
_carry_gen = 0
# The rsync DESTINATION ``host:path`` is parsed by the remote shell on rsync < 3 (macOS 2.6.9) and by openrsync,
# and ``--protect-args`` is not portable across them (Prism a39b11fdf16d). So a carried path is limited to a
# charset no shell interprets: absolute, components of [A-Za-z0-9._@+-], no empty / . / .. component, none
# starting with '-'. Anything else is not carried (the card keeps the old local_files rule).
_CARRY_COMPONENT_RE = re.compile(r"[A-Za-z0-9_@+.][A-Za-z0-9._@+-]*")


def carry_path_safe(path: Optional[str]) -> bool:
    """True when ``path`` may be named on a remote rsync command line verbatim (see ``_CARRY_COMPONENT_RE``)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    parts = path.rstrip("/").split("/")[1:]
    return bool(parts) and all(p not in (".", "..") and _CARRY_COMPONENT_RE.fullmatch(p) for p in parts)


def carried_host(conn, task_id: str, workspace_path: Optional[str]) -> Optional[str]:
    """The pool host holding this card's carried workspace, or None. The newest carry event counts, and only while
    the card still points at that workspace (an operator who re-pointed it has moved it on)."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (task_id, CARRIED_EVENT)).fetchone()
    if row is None:
        return None
    try:
        doc = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    host = doc.get("host")
    # Either spelling the carry recorded counts: the stored path at carry time and the path the claim then
    # persisted (Prism 34bd0265a601: the persisted one is what the row holds on every later run).
    if not isinstance(host, str) or not host or workspace_path is None \
            or workspace_path not in (doc.get("workspace"), doc.get("path")):
        return None
    return host.lower()


def carried_ids(conn, task_ids) -> set:
    """The subset of ``task_ids`` with any carry event (one query; :func:`carried_host` decides custody)."""
    ids = [t for t in task_ids if t]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r[0] for r in conn.execute(f"SELECT DISTINCT task_id FROM task_events WHERE kind=? AND task_id IN ({marks})",
                                       (CARRIED_EVENT, *ids))}


def carry_path_shared(conn, task_id: str, workspace_path: Optional[str]) -> bool:
    """True when another card points at ``workspace_path`` or already carried it (Prism e25ae3385b2f): a carry
    copies onto the same path on the pool host, so a shared directory would overwrite that card's remote work.
    Such a card is not carried (it keeps the old local_files rule). A missing path counts as shared."""
    if not workspace_path:
        return True
    if conn.execute("SELECT 1 FROM tasks WHERE id<>? AND workspace_path=? LIMIT 1",
                    (task_id, workspace_path)).fetchone():
        return True
    needle = json.dumps(workspace_path)
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE kind=? AND task_id<>? AND instr(payload, ?) > 0",
                                   (CARRIED_EVENT, task_id, needle)):
        try:
            doc = json.loads(payload or "{}")
        except (TypeError, ValueError):
            return True
        if workspace_path in (doc.get("workspace"), doc.get("path")):
            return True
    return False


class CarryScanBudget:
    """One tick's wall budget for :func:`carry_size_ok` walks. ``spent_s`` and ``deferred`` feed the tick record."""

    def __init__(self, seconds: Optional[float] = None, clock: Optional[Callable[[], float]] = None):
        import time as _time
        self.clock = clock or _time.monotonic
        self.seconds = CARRY_TICK_SCAN_SECONDS if seconds is None else seconds
        self.spent_s = 0.0
        self.deferred = 0
        # FIFO across ticks (no starvation under the fixed row order): while any path waits from an earlier tick,
        # only the oldest-waiting generation may walk; every other path is deferred and queued behind it. A
        # waiting path not asked for in CARRY_WAIT_FORGET_SECONDS (its card left ready) is forgotten.
        global _carry_gen
        _carry_gen += 1
        self.gen = _carry_gen
        t = self.clock()
        for k, (_g, seen) in list(_carry_waiting.items()):
            if t - seen > CARRY_WAIT_FORGET_SECONDS:
                del _carry_waiting[k]
        self.admit_gen = min((g for g, _s in _carry_waiting.values()), default=None)

    def left(self) -> float:
        return self.seconds - self.spent_s

    def may_walk(self, key) -> bool:
        if self.admit_gen is None:
            return True
        w = _carry_waiting.get(key)
        return w is not None and w[0] <= self.admit_gen

    def defer(self, key) -> None:
        self.deferred += 1
        w = _carry_waiting.get(key)
        _carry_waiting[key] = (w[0] if w else self.gen, self.clock())


def carry_size_ok(path: Optional[str], *, max_bytes: int = CARRY_MAX_BYTES,
                  max_entries: int = CARRY_MAX_ENTRIES, now: Optional[float] = None,
                  budget: Optional[CarryScanBudget] = None,
                  max_seconds: float = CARRY_SCAN_MAX_SECONDS, skip_clones: bool = False) -> bool:
    """True when the LOCAL workspace is small enough to carry (bytes and entries, symlinks not followed) and its
    path is :func:`carry_path_safe`. Any scan error is False (the card keeps the old rule). Cached per path and
    limits for ``CARRY_SCAN_TTL_SECONDS`` from the END of the walk: the dispatcher asks every tick.

    The walk uses a cooperative deadline: once a check observes ``max_seconds`` elapsed, the refusal is cached.
    Tick-budget exhaustion instead queues the path FIFO for a later tick without caching a refusal.
    Checks run between directories and every 256 entries. Canonicalization or a blocking scandir/lstat can
    overrun the deadline and still delay routing; this is not a hard filesystem timeout. A refused carry
    retains this card's old local_files rule rather than authorizing a transfer with an incomplete census."""
    import time as _time
    if not path or not carry_path_safe(path):
        return False
    try:
        from hermes_cli.kanban_db_workspace import canon
        root = canon(path)
    except (OSError, RuntimeError, ValueError):
        return False
    clock = budget.clock if budget is not None else _time.monotonic
    wt_on = skip_clones and worktree_carry_on()
    key = (root, max_bytes, max_entries) + (("skip_clones",) if skip_clones else ()) + (("worktrees",) if wt_on else ())
    t = clock() if now is None else now
    hit = _carry_scan_cache.get(key)
    if hit is not None and t - hit[0] < CARRY_SCAN_TTL_SECONDS:
        return hit[1]
    if budget is not None and (budget.left() <= 0 or not budget.may_walk(key)):
        budget.defer(key)
        return False
    start = clock()
    tick_bound = budget is not None and budget.left() < max_seconds
    stop = start + (budget.left() if tick_bound else max_seconds)
    total = entries = 0
    ok = True
    timed_out = False
    # Stream entries rather than materializing os.walk's per-directory lists.
    # Reuse the no-follow stat for both size and recursion; symlinks count but
    # never recurse, including links to directories and dangling links.
    import stat
    pending = [root]
    clones: List[str] = []
    worktrees: List[str] = []
    try:
        while pending and ok:
            current = pending.pop()
            # Like os.walk(followlinks=False): a queued child swapped for a symlink after its stat is skipped.
            if current is not root and os.path.islink(current):
                continue
            with os.scandir(current) as scan:
                for entry in scan:
                    entries += 1
                    info = entry.stat(follow_symlinks=False)
                    total += info.st_size
                    if entries > max_entries or total > max_bytes:
                        ok = False
                        break
                    if stat.S_ISDIR(info.st_mode):
                        if skip_clones and os.path.isdir(os.path.join(entry.path, ".git")) \
                                and not os.path.islink(os.path.join(entry.path, ".git")):
                            # t_9965b8d5: a git clone is re-cloned on the pool host, never copied or counted
                            clones.append(os.path.relpath(entry.path, root))
                            continue
                        if wt_on and _is_linked_worktree(entry.path):
                            # t_d2f06cf6: a linked worktree (``.git`` is a gitdir FILE) is set aside like a clone.
                            # The split plan decides it: rebuilt from origin when recoverable, else its files are
                            # censused (worktree_usage) and carried, and they count toward the caps.
                            worktrees.append(os.path.relpath(entry.path, root))
                            clones.append(worktrees[-1])
                            continue
                        pending.append(entry.path)
                    if entries % 256 == 0 and clock() >= stop:
                        ok, timed_out = False, True
                        break
            if ok and clock() >= stop:
                ok, timed_out = False, True
    except OSError:
        ok = False
    end = clock()
    if budget is not None:
        budget.spent_s += max(0.0, end - start)
    if timed_out and tick_bound:
        budget.defer(key)
        return False
    _carry_waiting.pop(key, None)
    if timed_out:
        _log.info("carry census of %s refused: walk passed %.1fs at %d entries", root, max_seconds, entries)
    _carry_scan_cache[key] = (end if now is None else now, ok)
    if skip_clones:
        _carry_clone_cache[key] = tuple(sorted(clones)) if ok else ()
        _carry_split_usage[root] = (total, entries, max_bytes, max_entries)
        _carry_worktree_cache[root] = tuple(sorted(worktrees)) if ok else ()
        _carry_setaside_cache[root] = tuple(sorted(clones)) if ok else ()
    return ok


WORKTREE_CARRY_KNOB = "worktree_split_carry"


def worktree_carry_on(kcfg: Optional[Mapping] = None) -> bool:
    """``kanban.placement.worktree_split_carry`` in the ROOT config (t_d2f06cf6, default on): a linked worktree in an
    over-cap scratch workspace is set aside and rebuilt from origin like a clone (its stash entries ride only with
    ``worktree_stash_carry`` on, :func:`stash_carry_on`). False/"off"/0 = the rule before t_d2f06cf6: a linked worktree counts as
    files toward the caps and any stash keeps a clone from being rebuilt. Unreadable config reads as the default."""
    if kcfg is None:
        try:
            from hermes_cli import kanban_db as _kdb
            kcfg = _kdb._board_home_kanban_cfg()
        except Exception:
            kcfg = {}
    placement = kcfg.get("placement") if isinstance(kcfg, Mapping) else None
    val = placement.get(WORKTREE_CARRY_KNOB, True) if isinstance(placement, Mapping) else True
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(val)


STASH_CARRY_KNOB = "worktree_stash_carry"


def stash_carry_on(kcfg: Optional[Mapping] = None) -> bool:
    """``kanban.placement.worktree_stash_carry`` in the ROOT config (t_d2f06cf6, operator split 07:20 11-12, default
    OFF): with it on, a rebuilt checkout's stash entries move host to host as a bundle (:func:`carry_stash`). Off, the
    rebuild touches no stash on either host (no bundle, no fetch, no ``stash store``, no stash precondition); the
    entries stay in the Studio's git dir and the spawn records how many. Only read while ``worktree_split_carry`` is
    on. Unreadable config reads as the default."""
    if kcfg is None:
        try:
            from hermes_cli import kanban_db as _kdb
            kcfg = _kdb._board_home_kanban_cfg()
        except Exception:
            kcfg = {}
    placement = kcfg.get("placement") if isinstance(kcfg, Mapping) else None
    val = placement.get(STASH_CARRY_KNOB, False) if isinstance(placement, Mapping) else False
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(val)


def _stash_carried() -> bool:
    """The stash rides the rebuild only when both the split carry and the stash carry are on."""
    return worktree_carry_on() and stash_carry_on()


def stash_count(repo: str, runner: Callable = subprocess.run) -> Optional[int]:
    """How many entries ``git stash list`` shows for ``repo`` (the shared list for a linked worktree), for the record
    only; None when git cannot say. Never a precondition (stash carry off)."""
    try:
        proc = runner(["git", "-C", repo, "stash", "list", "--format=%H"], capture_output=True, text=True,
                      encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return sum(1 for line in (proc.stdout or "").splitlines() if line.strip())


def _is_linked_worktree(path: str) -> bool:
    """True when ``path/.git`` is a regular file naming a gitdir (a linked worktree), not a symlink."""
    dotgit = os.path.join(path, ".git")
    try:
        if os.path.islink(dotgit) or not os.path.isfile(dotgit):
            return False
        with open(dotgit, encoding="utf-8-sig") as f:
            return f.read(4096).startswith("gitdir: ")
    except (OSError, UnicodeError):
        return False


def _gitfile_dirs(path: str) -> Optional[Tuple[str, str, bool]]:
    """``(gitdir, common dir, shared)`` for a checkout whose ``.git`` is a gitdir FILE, else None. ``shared`` is True
    only when the gitdir holds a ``commondir`` file (a linked worktree sharing one repo with other checkouts); a
    ``--separate-git-dir`` repo or a submodule checkout also has a gitfile but owns its whole git dir (operator item
    2 at d77ebe045e). Raises OSError/UnicodeError/ValueError on an unreadable pointer."""
    if not _is_linked_worktree(path):
        return None
    with open(os.path.join(path, ".git"), encoding="utf-8-sig") as f:
        gitdir = os.path.realpath(os.path.join(path, f.read(4096).strip()[8:]))
    cfile = os.path.join(gitdir, "commondir")
    if not os.path.isfile(cfile):
        return gitdir, gitdir, False
    with open(cfile, encoding="utf-8-sig") as f:
        return gitdir, os.path.realpath(os.path.join(gitdir, f.read().strip())), True


def clone_linked_worktrees(repo: str, runner: Callable = subprocess.run) -> Optional[Tuple[str, ...]]:
    """Realpaths of every OTHER worktree of the clone at ``repo`` (``git worktree list``), or None when git cannot
    list them (fail closed)."""
    try:
        proc = runner(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True,
                      encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        if getattr(proc, "returncode", 1) != 0:
            return None
        me = os.path.realpath(repo)
        return tuple(os.path.realpath(line[9:]) for line in (proc.stdout or "").splitlines()
                     if line.startswith("worktree ") and os.path.realpath(line[9:]) != me)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def split_worktree_loss(workspace: str, recs, runner: Callable = subprocess.run) -> Optional[str]:
    """None, or the reason a split carry would lose git state (t_d2f06cf6): a clone in ``recs`` is rebuilt from
    origin, so its worktree metadata, the worktrees' indexes and any commit only its .git holds stay behind on the
    Studio. Every linked worktree of it inside ``workspace`` must be rebuilt too (in ``recs``); one that is not would
    land on the pool host as files whose ``.git`` names a gitdir that does not exist there."""
    try:
        ws = os.path.realpath(workspace).rstrip("/") + "/"
        rebuilt = {os.path.realpath(os.path.join(workspace, r["rel"])) for r in recs}
    except (OSError, ValueError):
        return "workspace unreadable"
    for r in recs:
        try:
            dirs = _gitfile_dirs(os.path.join(workspace, r["rel"]))
        except (OSError, UnicodeError, ValueError):
            return f"clone {r['rel']}: git dir unreadable"
        if dirs is not None and dirs[2]:
            # Prism 78347eb52e27 / c-acee245679ee7db1: a rebuilt LINKED worktree leaves the common dir (the primary's
            # .git) where it is; the primary and its other worktrees keep their git state. Only rebuilding the
            # checkout that holds the common dir strands its worktrees.
            continue
        others = clone_linked_worktrees(os.path.join(workspace, r["rel"]), runner)
        if others is None:
            return f"clone {r['rel']}: worktree list unreadable"
        for path in others:
            if (path + "/").startswith(ws) and path not in rebuilt:
                return (f"clone {r['rel']} holds the git state of worktree {os.path.relpath(path, ws)}, which "
                        f"cannot be rebuilt from origin")
    return None


def carried_gitdir_loss(workspace: str, carried, rebuilt, nested: bool = True) -> Optional[str]:
    """None, or the reason a set-aside path carried AS FILES would arrive with a dangling git dir (Prism 805c10f0cb3d,
    78347eb52e27). The carry copies the workspace to the SAME path on the pool host minus the ``rebuilt`` rels, so a
    linked worktree in ``carried`` keeps a working ``.git`` only when its gitdir and common dir sit inside the
    workspace and outside every rebuilt rel. A gitdir that no longer exists on the Studio (a dead stub) loses nothing
    by the copy. Anything unreadable fails closed. ``nested`` False (t_f3a411c2): only the checkout at each carried rel
    itself, not the repos below it."""
    try:
        ws = os.path.realpath(workspace).rstrip("/") + "/"
        gone = tuple(os.path.realpath(os.path.join(workspace, r)).rstrip("/") + "/" for r in rebuilt)
    except (OSError, ValueError):
        return "workspace unreadable"

    def _travels(p: str) -> bool:
        p = p.rstrip("/") + "/"
        return p.startswith(ws) and not any(p.startswith(g) for g in gone)

    def _alternates_loss(name: str, common: str) -> Optional[str]:
        # Prism ca4b50199bda / c-86a12a77bcdd7173: objects borrowed through objects/info/alternates (followed
        # transitively, as git does) must travel too, or the copy arrives without them.
        seen, todo = set(), [os.path.join(common, "objects")]
        while todo:
            objs = os.path.realpath(todo.pop())
            if objs in seen:
                continue
            seen.add(objs)
            if not _travels(objs):
                # Prism 519874fe750e: the repo's own objects dir symlinked out of the carry
                return (f"{name} is carried as files but its object store {objs} does not travel with it "
                        f"(outside the workspace or in a rebuilt clone)")
            # advisory 03:2x + operator 07:20 item 2: a symlink ANYWHERE under the object store (pack, info, a
            # fan-out dir, a pack or loose object file) that leaves the carry arrives dangling; the realpath of the
            # store root alone does not see it. Symlinked dirs are checked, never entered.
            walk = [objs] if os.path.isdir(objs) else []
            while walk:
                with os.scandir(walk.pop()) as scan:
                    for e in scan:
                        if e.is_symlink():
                            if not _travels(os.path.realpath(e.path)):
                                return (f"{name} is carried as files but {e.path} is a symlink to "
                                        f"{os.path.realpath(e.path)}, which does not travel with it")
                        elif e.is_dir(follow_symlinks=False):
                            walk.append(e.path)
            alt = os.path.join(objs, "info", "alternates")
            if not os.path.lexists(alt):
                continue
            with open(alt, encoding="utf-8-sig", newline="") as f:
                # advisory 03:2x: git splits on LF only and never trims a line; a trailing space is part of the path
                for line in f.read().split("\n"):
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith('"'):
                        # Prism bd234dd77562: git C-quotes an alternate path; never read a quoted one as relative
                        return f"{name} is carried as files but has a quoted alternate {line} (not resolved here)"
                    target = os.path.realpath(os.path.join(objs, line))
                    if not _travels(target):
                        return (f"{name} is carried as files but borrows objects through an alternate {target} "
                                f"that does not travel with it (outside the workspace or in a rebuilt clone)")
                    todo.append(target)
        return None

    def _repo_loss(repo: str, name: str) -> Optional[str]:
        try:
            dirs = _gitfile_dirs(repo)
            if dirs is None:
                common = os.path.join(repo, ".git")
                if os.path.islink(common) and not _travels(os.path.realpath(common)):
                    return (f"{name} is carried as files but its .git symlink resolves to "
                            f"{os.path.realpath(common)}, which does not travel with it")
                if not os.path.isdir(common):
                    return None
            else:
                gitdir, common, _shared = dirs
                if not os.path.isdir(gitdir):
                    return None  # dead stub: git already cannot use it on the Studio; the files are all there is
                for p in (gitdir, common):
                    if not _travels(p):
                        return (f"worktree {name} is carried as files but its git dir {p} does not travel with it "
                                f"(outside the workspace or in a rebuilt clone)")
            return _alternates_loss(name, common)
        except (OSError, UnicodeError, ValueError):
            return f"{name}: git dir unreadable"

    for rel in carried:
        # Prism 23e5001bba80 / c-480d52ee4b039482: every repo at or under a carried rel (a worktree nested in a clone
        # carried as files, say), not only the top-level one. Symlinks are never followed; .git dirs not entered.
        top = os.path.join(workspace, rel)
        stack = [(top, rel)]
        while stack:
            d, name = stack.pop()
            if os.path.lexists(os.path.join(d, ".git")):
                lost = _repo_loss(d, name)
                if lost is not None:
                    return lost
            if not nested:
                break
            try:
                with os.scandir(d) as scan:
                    subs = [e for e in scan if e.name != ".git" and e.is_dir(follow_symlinks=False)]
            except OSError:
                return f"{name}: unreadable"
            stack.extend((e.path, f"{name}/{e.name}") for e in subs)
    return None


def carry_split_setaside(path: Optional[str]) -> Tuple[str, ...]:
    """Every clone and linked-worktree rel the last split census of ``path`` set aside (operator F3: one that is
    not rebuilt is carried as files and counted, so the spawn re-check needs the whole list)."""
    try:
        from hermes_cli.kanban_db_workspace import canon
        return _carry_setaside_cache.get(canon(path), ())
    except (OSError, RuntimeError, ValueError, TypeError):
        return ()


def carry_split_worktrees(path: Optional[str]) -> Tuple[str, ...]:
    """The linked-worktree rels (a subset of :func:`carry_split`'s) the last split census of ``path`` set aside."""
    try:
        from hermes_cli.kanban_db_workspace import canon
        return _carry_worktree_cache.get(canon(path), ())
    except (OSError, RuntimeError, ValueError, TypeError):
        return ()


def worktree_usage(path: str, *, max_bytes: int, max_entries: int, fresh: bool = False,
                   max_seconds: float = CARRY_SCAN_MAX_SECONDS,
                   budget: Optional[CarryScanBudget] = None) -> Optional[Tuple[int, int]]:
    """``(bytes, entries)`` of the directory ``path`` (symlinks counted, never followed), or None when the walk
    errors or has not finished. Stops once a cap is passed (the tallies then already exceed it). Cached for
    ``CARRY_SCAN_TTL_SECONDS`` unless ``fresh`` (the spawn-time re-check, review F3).

    Operator F4 (t_d2f06cf6): a walk that outlives ``max_seconds`` (or the tick ``budget``) is not a cached refusal.
    It is checkpointed at a directory boundary and resumed by the next call, so a big carried worktree finishes on a
    later tick instead of reading ``census unavailable`` on every tick. A ``fresh`` walk never resumes a checkpoint."""
    import stat
    import time as _time
    try:
        key = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    t = _time.monotonic()
    hit = _worktree_usage_cache.get(key)
    if not fresh and hit is not None and t - hit[0] < CARRY_SCAN_TTL_SECONDS:
        # a walk stopped at a cap is only a lower bound: reusable while it still exceeds the caps asked now
        if hit[1] is None or hit[2] or hit[1][0] > max_bytes or hit[1][1] > max_entries:
            return hit[1]
    if budget is not None:
        if budget.left() <= 0:
            budget.deferred += 1
            return None
        max_seconds = min(max_seconds, budget.left())
    stop = t + max_seconds
    part = None if fresh else _worktree_partial.pop(key, None)
    if part is not None and t - part[0] < CARRY_SCAN_TTL_SECONDS:
        pending, total, entries = list(part[1]), part[2], part[3]
    else:
        pending, total, entries = [path], 0, 0
    usage: Optional[Tuple[int, int]] = None
    timed_out = False
    try:
        while pending and total <= max_bytes and entries <= max_entries:
            current = pending.pop()
            if current != path and os.path.islink(current):
                continue
            with os.scandir(current) as scan:
                for entry in scan:
                    entries += 1
                    info = entry.stat(follow_symlinks=False)
                    total += info.st_size
                    if total > max_bytes or entries > max_entries:
                        break
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
            # checkpoint only here, after a whole directory: a resumed walk never counts a directory twice
            if pending and total <= max_bytes and entries <= max_entries and _time.monotonic() >= stop:
                timed_out = True
                break
        if not timed_out:
            usage = (total, entries)
    except OSError:
        usage = None
    if budget is not None:
        budget.spent_s += max(0.0, _time.monotonic() - t)
    if timed_out:
        if len(_worktree_partial) > 4096:
            _worktree_partial.clear()
        _worktree_partial[key] = (t, tuple(pending), total, entries)
        if budget is not None:
            budget.deferred += 1
        return None
    if len(_worktree_usage_cache) > 4096:
        _worktree_usage_cache.clear()
    complete = usage is not None and not pending and total <= max_bytes and entries <= max_entries
    _worktree_usage_cache[key] = (_time.monotonic(), usage, complete)
    return usage


def carry_split(path: Optional[str], *, budget: Optional[CarryScanBudget] = None,
                max_bytes: int = CARRY_MAX_BYTES, max_entries: int = CARRY_MAX_ENTRIES) -> Optional[Tuple[str, ...]]:
    """t_9965b8d5 (operator 2026-11-10 15:3x): a workspace over the carry limit is carried WITHOUT its git clones,
    which the pool host re-clones from origin at the same branch and commit. Returns the clone paths (relative to
    the workspace, non-empty) when everything else fits the limits, else None (nothing to split, still over, scan
    refused or deferred). A clone path must be shell-inert like the workspace itself."""
    if not carry_size_ok(path, budget=budget, max_bytes=max_bytes, max_entries=max_entries, skip_clones=True):
        return None
    try:
        from hermes_cli.kanban_db_workspace import canon
        root = canon(path)
    except (OSError, RuntimeError, ValueError):
        return None
    clones = _carry_clone_cache.get((root, max_bytes, max_entries, "skip_clones")
                                    + (("worktrees",) if worktree_carry_on() else ()), ())
    if not clones or not all(carry_path_safe(f"{str(root).rstrip('/')}/{c}") for c in clones):
        return None
    return clones


def stash_entries(repo: str, runner: Callable = subprocess.run) -> Optional[Tuple[Tuple[str, int, str], ...]]:
    """The stash entries a rebuild of the checkout at ``repo`` must carry, newest first, as ``(commit, unix ts,
    reflog subject)``; None when unreadable (operator design at 2119cc07, t_d2f06cf6: never guess who owns a stash).

    Every entry of the list the checkout sees: a clone's own, or for a linked worktree the whole list shared through
    the common dir (operator 03:2x: no timestamp filter; a clock or GIT_COMMITTER_DATE can date a new stash before
    the worktree, and all 35 entries of the home repo bundle in 2.8 MB / 5.5 s). Carrying another checkout's entry
    too costs a few objects; the originals stay in the common dir untouched.

    ``refs/stash`` with no reflog lists as empty (rc 0) while the ref still holds a stash: when the ref resolves and
    the list does not include its commit, the stash is unreadable (None), never an empty carry."""
    try:
        st = runner(["git", "-C", repo, "stash", "list", "--date=unix", "--format=%H%x09%gd%x09%gs"],
                    capture_output=True, text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        ref = runner(["git", "-C", repo, "rev-parse", "-q", "--verify", "refs/stash"], capture_output=True,
                     text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(st, "returncode", 1) != 0 or getattr(ref, "returncode", 2) not in (0, 1):
        return None
    out = []
    for line in (st.stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            sha, sel, subject = line.split("\t", 2)
            ts = int(sel[sel.index("{") + 1:sel.index("}")])
        except (ValueError, IndexError):
            return None
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
            return None
        out.append((sha, ts, subject))
    live = (getattr(ref, "stdout", "") or "").strip() if getattr(ref, "returncode", 1) == 0 else ""
    if live and live not in {e[0] for e in out}:
        return None  # advisory 03:2x: refs/stash holds a commit the list does not show (reflog expired or removed)
    return tuple(out)


def _stash_label(entry) -> str:
    return f"stash entry {entry[0][:12]} ({entry[2][:80]})"


# t_d2f06cf6 (operator 02:0x): the bundle build runs inside the dispatcher tick, so it gets a wall-clock ceiling far
# below CARRY_TIMEOUT_SECONDS. Measured on the Studio: 0.5 s for 17 entries once origin's default branch is excluded,
# 47 to 70 s when only HEAD was. A build that hits the ceiling is remembered for STASH_BUNDLE_RETRY_SECONDS: the card
# then WAITS at routing with the reason named (stash_bundle_deferred) instead of re-blocking every tick.
STASH_BUNDLE_TIMEOUT_SECONDS = 30
STASH_BUNDLE_RETRY_SECONDS = 3600
_stash_bundle_timeouts: Dict[tuple, Tuple[float, str]] = {}


class StashBundleTimeout(WorkspaceUnavailable, RuntimeError):
    """The stash bundle build hit STASH_BUNDLE_TIMEOUT_SECONDS. A WorkspaceUnavailable: the dispatcher releases the
    claim and refunds the reservation without charging a worker failure, and routing makes the card wait."""


def _stash_bundle_key(repo: str, entries, head: str) -> tuple:
    return (os.path.normpath(str(repo)), tuple(e[0] for e in entries), str(head))


def stash_bundle_deferred(repo: str, entries, head: str, now: Optional[float] = None) -> Optional[str]:
    """The wait reason when a bundle of exactly these ``entries`` over ``head`` timed out less than
    STASH_BUNDLE_RETRY_SECONDS ago, else None. A new stash entry or a moved HEAD is a new key: tried again."""
    import time
    if not entries:
        return None
    key = _stash_bundle_key(repo, entries, head)
    hit = _stash_bundle_timeouts.get(key)
    if hit is None:
        return None
    if (time.monotonic() if now is None else now) - hit[0] >= STASH_BUNDLE_RETRY_SECONDS:
        _stash_bundle_timeouts.pop(key, None)
        return None
    return hit[1]


def stash_bundle(repo: str, entries, head: str, runner: Callable = subprocess.run) -> bytes:
    """A git bundle holding the commits of every stash entry in ``entries`` that the rebuilt clone does not already
    hold: ``head`` (the commit the clone lands on) and origin's default branch as this checkout last saw it
    (``refs/remotes/origin/HEAD``) are excluded, so the main commits between a worktree's base and a stash's base never
    ride (operator 02:0x: 109 MB -> 1.8 MB). The pool clone may lack that default-branch sha (the Studio's tracking ref
    can be ahead of what the clone fetched): :func:`stash_store_script` fetches any missing prerequisite from origin
    and refuses (rc 10) when it cannot, so a partial list is never stored.

    Built in a throwaway bare repo that borrows ``repo``'s objects through an alternate: nothing in ``repo`` or its
    common dir is written. The whole build is bounded by STASH_BUNDLE_TIMEOUT_SECONDS; hitting it raises
    :class:`StashBundleTimeout` and is remembered (:func:`stash_bundle_deferred`). Raises RuntimeError naming the entry
    on any other failure."""
    import tempfile
    import time
    deadline = time.monotonic() + STASH_BUNDLE_TIMEOUT_SECONDS
    what = ", ".join(map(_stash_label, entries))

    def _timed_out():
        reason = (f"stash bundle of {repo} exceeded {STASH_BUNDLE_TIMEOUT_SECONDS}s ({what}); "
                  f"retried after {STASH_BUNDLE_RETRY_SECONDS}s or when the stash or HEAD changes")
        _stash_bundle_timeouts[_stash_bundle_key(repo, entries, head)] = (time.monotonic(), reason)
        if len(_stash_bundle_timeouts) > 4096:
            _stash_bundle_timeouts.clear()
        return StashBundleTimeout(reason)

    def _git(*args, what, ok_rc=(0,)):
        left = deadline - time.monotonic()
        if left <= 0:
            raise _timed_out()
        try:
            proc = runner(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=left, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise _timed_out()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"stash carry of {repo}: {what}: {exc}")
        if getattr(proc, "returncode", 1) not in ok_rc:
            raise RuntimeError(f"stash carry of {repo}: {what} rc={getattr(proc, 'returncode', '?')}: "
                               f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
        return proc

    try:
        dirs = _gitfile_dirs(repo)
        objects = os.path.realpath(os.path.join(dirs[1] if dirs else os.path.join(repo, ".git"), "objects"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"stash carry of {repo}: git dir unreadable: {exc}")
    # origin's default branch, as this checkout last fetched it (shared refs in a linked worktree); rc 1 = none
    base = (getattr(_git("-C", repo, "rev-parse", "-q", "--verify", "refs/remotes/origin/HEAD^{commit}",
                         what="origin default branch", ok_rc=(0, 1)), "stdout", "") or "").strip()
    excl = [f"^{head}"] + ([f"^{base}"] if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base) else [])
    # TemporaryDirectory, not mkdtemp + rmtree: workspace deletion has one choke point (test_kanban_survivor).
    with tempfile.TemporaryDirectory(prefix="kanban-stash-", ignore_cleanup_errors=True) as tmp:
        bare, out = os.path.join(tmp, "b.git"), os.path.join(tmp, "stash.bundle")
        _git("init", "-q", "--bare", bare, what="bundle repo")
        alt = os.path.join(bare, "objects", "info", "alternates")
        with open(alt, "w", encoding="utf-8") as f:  # windows-footgun: ok — a write; a BOM breaks the path
            f.write(objects + "\n")
        refs = []
        for i, e in enumerate(entries):
            _git("-C", bare, "update-ref", f"refs/carry/{i}", e[0], what=_stash_label(e))
            refs.append(f"refs/carry/{i}")
        # advisory 03:2x P2: every entry already reachable from the exclusions = nothing to send (``git bundle
        # create`` refuses an empty bundle). b"" tells the store script the commits are already in the clone.
        count = (getattr(_git("-C", bare, "rev-list", "--count", *refs, *excl, what=f"commits of {what}"),
                         "stdout", "") or "").strip()
        if count == "0":
            return b""
        _git("-C", bare, "bundle", "create", out, *refs, *excl, what=f"bundle of {what}")
        _git("-C", bare, "bundle", "verify", "-q", out, what=f"bundle verify of {what}")
        if os.path.getsize(out) > CARRY_MAX_BYTES:
            raise RuntimeError(f"stash carry of {repo}: bundle over the carry byte cap")
        with open(out, "rb") as f:
            return f.read()


def stash_store_script(dest: str, entries) -> str:
    """Remote sh: unbundle the stash commits read on stdin into the rebuilt clone at ``dest`` and store each entry
    with its original subject, oldest first (so ``stash@{0}`` is the newest again), then PROVE the list is exactly
    ``entries`` (rc 8). Re-run safe: a clone already holding exactly that list is left alone; one holding any other
    stash is refused (rc 9), never added to. A bundle whose prerequisites the clone lacks even after one fetch of
    origin is refused (rc 10), nothing stored. An EMPTY payload (every entry already reachable from what the clone
    holds, :func:`stash_bundle`) unbundles nothing: the clone must hold each entry commit, after at most one fetch of
    origin, else rc 10."""
    d = shlex.quote(dest)
    want = " ".join(e[0] for e in entries)
    stores = "; ".join(f"git -C \"$d\" -c user.name=kanban-carry -c user.email=kanban-carry@localhost stash store "
                       f"-q -m {shlex.quote(e[2])} {e[0]}" for e in reversed(tuple(entries)))
    return (f"set -e; d={d}; want={shlex.quote(want)}; "
            "have=$(git -C \"$d\" stash list --format=%H | tr '\\n' ' ' | sed 's/ $//'); "
            "if [ \"$have\" = \"$want\" ]; then cat >/dev/null; exit 0; fi; [ -z \"$have\" ] || exit 9; "
            "t=$(mktemp); trap 'rm -f \"$t\"' EXIT; cat >\"$t\"; "
            "if [ -s \"$t\" ]; then "
            # t_d2f06cf6 (operator 02:0x): the bundle leaves out origin's default branch as the Studio saw it. A clone
            # missing those commits fetches origin once, else refuses (rc 10) before any entry is stored.
            "if ! git -C \"$d\" bundle verify -q \"$t\" >/dev/null 2>&1; then "
            "git -C \"$d\" fetch -q origin >/dev/null 2>&1 || true; "
            "git -C \"$d\" bundle verify -q \"$t\" >&2 || exit 10; fi; "
            "git -C \"$d\" bundle unbundle \"$t\" >/dev/null; "
            "else "
            # advisory 03:2x P2: nothing to send; the entry commits must already be in the clone
            "h() { for s in $want; do git -C \"$d\" cat-file -e \"$s^{commit}\" 2>/dev/null || return 1; done; }; "
            "if ! h; then git -C \"$d\" fetch -q origin >/dev/null 2>&1 || true; "
            "h || { echo \"stash commits missing from the clone\" >&2; exit 10; }; fi; "
            "fi; "
            f"{stores}; "
            "have=$(git -C \"$d\" stash list --format=%H | tr '\\n' ' ' | sed 's/ $//'); "
            "[ \"$have\" = \"$want\" ] || exit 8")


def carry_stash(host: PoolHost, repo: str, dest: str, entries, head: str, runner: Callable = subprocess.run,
                bundle: Optional[bytes] = None, reservation: Optional[str] = None) -> None:
    """Move the stash ``entries`` of the Studio checkout ``repo`` to the rebuilt clone at ``dest`` on ``host``:
    a bundle over the pool ssh (never GitHub), stored there with the original subjects, in order. Raises naming the
    entries on any failure.

    ``reservation``: the id take() booked for this card on ``host`` (the dispatcher's ``_rid``). The store ssh runs
    under it (ALLOC_RESERVATION in its env) and takes no reservation of its own; without one it refuses."""
    if not entries:
        return
    if not reservation:
        raise RuntimeError(f"stash carry to {host.name}: no placement reservation for this card; "
                           f"{', '.join(map(_stash_label, entries))} not carried")
    payload = bundle if bundle is not None else stash_bundle(repo, entries, head, runner)
    env = dict(os.environ, ALLOC_RESERVATION=str(reservation))
    try:
        proc = runner(_ssh_argv(host, stash_store_script(dest, entries)), input=payload, capture_output=True,
                      timeout=CARRY_TIMEOUT_SECONDS, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"stash carry to {host.name}: {', '.join(map(_stash_label, entries))}: {exc}")
    if getattr(proc, "returncode", 1) != 0:
        err = getattr(proc, "stderr", b"") or b""
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        raise RuntimeError(f"stash carry to {host.name} rc={getattr(proc, 'returncode', '?')}: "
                           f"{', '.join(map(_stash_label, entries))}: {err.strip()[:200]}")


def assess_clone(repo: str, runner: Callable = subprocess.run) -> Tuple[Optional[dict], Optional[str]]:
    """``(record, None)`` when the clone at ``repo`` can be rebuilt from origin on a pool host, else
    ``(None, reason)``. The rules are the worktree port's (t_1d2c5939): a GitHub origin, on a branch, nothing
    uncommitted or untracked; its ignored files (minus regenerable caches) are listed in ``ignored`` and carried; a branch origin has or is behind (``push`` then carries the unpushed commits,
    force-with-lease pinned to the origin sha seen here); a branch that diverged from origin is refused."""
    from pathlib import Path as _P
    from hermes_cli import kanban_worktree_port as _kwt

    def _out(*args):
        try:
            proc = runner(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8",
                          timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else None

    slug = _kwt.origin_slug(_P(repo), runner)
    if not slug:
        return None, "origin_not_github"
    branch = _out("branch", "--show-current")
    if not branch:
        return None, "detached_head"
    if not _kwt._clean_on_branch(_P(repo), branch, runner):
        return None, "dirty_tree"
    # Prism 425d85646d96: a stash lives only in the Studio's git dir and a re-clone drops it. t_d2f06cf6 (operator
    # design at 2119cc07): it no longer keeps the checkout from being rebuilt; its entries ride to the rebuilt clone
    # as a bundle (carry_stash) when kanban.placement.worktree_stash_carry is on; off (default, operator split
    # 07:20 11-12) no stash is read here at all, so none can refuse. With the split switch off, any stash keeps the
    # old refusal.
    entries = ()
    if not worktree_carry_on() or stash_carry_on():
        entries = stash_entries(repo, runner)
        if entries is None:
            return None, "stash_unreadable"
        if entries and not worktree_carry_on():
            return None, "stash_present"
    # Prism 38aae6c981c1/62448b5b6868/72087e167990/30ed122cf9a9: a re-clone cannot restore ignored files (datasets,
    # .env, built state). Operator F4: they are carried into the rebuilt clone (carry_clones), except regenerable
    # caches, which are dropped; listed one per FILE so the carry copies exactly these paths.
    ignored = clone_ignored_files(repo, runner)
    if ignored is None:
        return None, "dirty_tree"
    local = _out("rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    if not local:
        return None, "no_local_branch"
    remote = _out("rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}") or ""
    push = None
    if remote != local:
        # Review F1: the dispatcher never pushes a scratch clone's commits to origin's default branch (no PR there).
        head = _out("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD") or ""
        if branch in {"main", "master", head.split("/", 1)[-1]} - {""}:
            return None, "unpushed_commits_on_default_branch"
        if not head:
            # Prism ca6624b1adff: without origin/HEAD the default branch is unknown (a `trunk` would slip past the
            # names above), so no push is authorized. A level clone (no push) still re-clones.
            return None, "unpushed_commits_default_branch_unknown"
        if remote and _out("merge-base", "--is-ancestor", remote, local) is None:
            return None, "diverged_from_origin"
        push = (repo, branch, remote, local)
    rec = {"slug": slug, "branch": branch, "sha": local, "push": push, "ignored": ignored}
    if entries:
        rec["stash"] = entries
    return rec, None


# Operator F4 (2026-11-10 20:5x): ignored paths a re-clone may DROP because a run regenerates them. Anything else
# ignored is carried. Matched on any path component (a directory name, or a file name for .DS_Store).
REGENERABLE_CACHE_NAMES = frozenset({
    "__pycache__",    # CPython bytecode, rewritten on import
    ".pytest_cache",  # pytest cache (lastfailed, nodeids), rebuilt on the next run
    ".ruff_cache",    # ruff lint cache
    ".mypy_cache",    # mypy incremental cache
    ".ty_cache",      # ty incremental cache
    ".tox",           # tox envs, rebuilt by tox
    ".DS_Store",      # Finder metadata, no content
})


def clone_ignored_files(repo: str, runner: Callable = subprocess.run) -> Optional[Tuple[str, ...]]:
    """The clone-relative git-ignored FILES of ``repo`` a re-clone would lose, regenerable caches left out
    (operator F4); None when git cannot list them."""
    try:
        proc = runner(["git", "-C", repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"],
                      capture_output=True, text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return tuple(sorted(p for p in (proc.stdout or "").split("\0") if p and not regenerable_cache(p)))


def regenerable_cache(rel: str) -> bool:
    """True when the clone-relative ignored path ``rel`` lies in a regenerable cache (operator F4): any component in
    :data:`REGENERABLE_CACHE_NAMES`, or a ``*.egg-info`` directory (setuptools metadata, rebuilt by an install)."""
    parts = rel.strip("/").split("/")
    return any(p in REGENERABLE_CACHE_NAMES for p in parts) or any(p.endswith(".egg-info") for p in parts[:-1])


def carry_split_fits(path: Optional[str], extra_bytes: int, extra_entries: int, *,
                     rebuilt: Optional[set] = None, fresh: bool = False, why_not: Optional[dict] = None,
                     budget: Optional[CarryScanBudget] = None) -> Optional[str]:
    """None when the split carry's remainder (the last :func:`carry_split` census of ``path``, against the caps that
    census used) PLUS the clones' carried ignored files (``extra_*``) PLUS every set-aside clone or linked worktree
    NOT in ``rebuilt`` (t_d2f06cf6 + operator F3: its files ride the carry) stays within the caps, else the reason
    naming the cap (operator F4) and, per path carried as files, why it is not rebuilt (``why_not``, operator F5).
    ``rebuilt`` None = every set-aside path is rebuilt (the pre-worktree callers)."""
    try:
        from hermes_cli.kanban_db_workspace import canon
        root = canon(path)
    except (OSError, RuntimeError, ValueError, TypeError):
        return "carry census unavailable"
    used = _carry_split_usage.get(root)
    if used is None:
        return "carry census unavailable"
    total, entries = used[0] + extra_bytes, used[1] + extra_entries
    carried_wt = [] if rebuilt is None else [w for w in _carry_setaside_cache.get(root, ()) if w not in rebuilt]
    why_not = why_not or {}

    worktrees = set(_carry_worktree_cache.get(root, ()))

    def _named(rel):
        kind = "worktree" if rel in worktrees else "clone"
        return f"{kind} {rel} ({why_not[rel]})" if why_not.get(rel) else f"{kind} {rel}"
    for rel in carried_wt:
        usage = worktree_usage(os.path.join(str(root), rel), max_bytes=max(0, used[2] - total),
                               max_entries=max(0, used[3] - entries), fresh=fresh, budget=budget,
                               max_seconds=CARRY_SPAWN_WALK_SECONDS if fresh else CARRY_SCAN_MAX_SECONDS)
        if usage is None:
            if not fresh and os.path.realpath(os.path.join(str(root), rel)) in _worktree_partial:
                return f"carry census of {_named(rel)} not finished; it resumes next tick"
            return f"carry census of {_named(rel)} unavailable"
        total, entries = total + usage[0], entries + usage[1]
    what = "the clones' ignored files" + (f" and {', '.join(_named(w) for w in carried_wt)} carried as files "
                                         f"(not rebuildable)" if carried_wt else "")
    if total > used[2]:
        return f"over the carry byte cap with {what}: {total} > {used[2]} bytes"
    if entries > used[3]:
        return f"over the carry entry cap with {what}: {entries} > {used[3]} entries"
    return None


_clone_assess_cache: Dict[tuple, tuple] = {}  # (canonical path, HEAD key) -> (monotonic at, (record, reason))


def _clone_head_key(repo: str) -> Optional[tuple]:
    """A no-subprocess fingerprint of the clone's HEAD (review F3): the ``.git/HEAD`` text, the sha its branch ref
    holds (loose ref, else packed-refs) and the index mtime. None (do not cache) when any part cannot be read."""
    git = common = os.path.join(repo, ".git")
    try:
        if os.path.isfile(git):
            # t_d2f06cf6: a linked worktree: HEAD and index in its gitdir, branch refs in the common dir
            with open(git, encoding="utf-8-sig") as f:
                line = f.read(4096).strip()
            if not line.startswith("gitdir: "):
                return None
            git = os.path.join(repo, line[8:])
            common = git
            if os.path.isfile(os.path.join(git, "commondir")):
                with open(os.path.join(git, "commondir"), encoding="utf-8-sig") as f:
                    common = os.path.join(git, f.read().strip())
        with open(os.path.join(git, "HEAD"), encoding="utf-8-sig") as f:
            head = f.read().strip()
        sha = head
        if head.startswith("ref: "):
            ref = head[5:]
            loose = os.path.join(common, *ref.split("/"))
            if os.path.isfile(loose):
                with open(loose, encoding="utf-8-sig") as f:
                    sha = f.read().strip()
            else:
                sha = ""
                with open(os.path.join(common, "packed-refs"), encoding="utf-8-sig") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            sha = parts[0]
                            break
                if not sha:
                    return None
        index = os.stat(os.path.join(git, "index")).st_mtime_ns if os.path.exists(os.path.join(git, "index")) else 0
        return (head, sha, index)
    except (OSError, UnicodeError):
        return None


def assess_clone_cached(repo: str, runner: Callable = subprocess.run,
                        now: Optional[float] = None) -> Tuple[Optional[dict], Optional[str]]:
    """:func:`assess_clone` cached for ``CARRY_SCAN_TTL_SECONDS`` per clone path and HEAD (review F3): the routing
    pass asks every tick under the dispatch lock, and one assessment is several git subprocesses including
    ``git status --ignored --untracked-files=all``. A clone whose HEAD, branch ref or index moved is re-assessed."""
    import time as _time
    key = _clone_head_key(repo)
    if key is None:
        return assess_clone(repo, runner)
    try:
        ck = (os.path.realpath(repo),) + key
    except (OSError, ValueError):
        return assess_clone(repo, runner)
    t = _time.monotonic() if now is None else now
    hit = _clone_assess_cache.get(ck)
    if hit is not None and t - hit[0] < CARRY_SCAN_TTL_SECONDS:
        rec, why = hit[1]
        return (dict(rec) if rec is not None else None), why
    rec, why = assess_clone(repo, runner)
    # ``git status`` may refresh (rewrite) the index: store under the fingerprint as the assessment LEFT it.
    after = _clone_head_key(repo)
    if after is not None:
        if len(_clone_assess_cache) > 4096:
            _clone_assess_cache.clear()
        _clone_assess_cache[(ck[0],) + after] = (t, ((dict(rec) if rec is not None else None), why))
    return rec, why


def _raise(exc: OSError) -> None:
    raise exc


def carry_workspace(host: PoolHost, workspace: str, runner: Callable = subprocess.run,
                    exclude: Tuple[str, ...] = ()) -> None:
    """Copy the local ``workspace`` to the same path on ``host`` (rsync -a over the pool ssh options); raise on any
    failure. Never deletes on the target: an existing remote file of the same name is overwritten, others stay."""
    src = workspace.rstrip("/") or "/"
    if not carry_path_safe(src):
        raise RuntimeError(f"carry to {host.name}: refused, {src!r} is not a shell-inert path")
    # The same mkdir every placed run gets (mode 0770, board-root probe); the copy below runs inside the
    # reservation take() booked for this card on this host, so it places no work of its own.
    prepare_remote_workspace(host, src, runner)
    ssh = _ssh_argv(host, "")[:-2]  # the ssh options, without target and command
    # ``exclude``: workspace-relative clone paths the host re-clones instead (t_9965b8d5), anchored at the root.
    excl = [f"--exclude=/{rel.strip('/')}/" for rel in exclude]
    proc = runner(["rsync", "-a", *excl, "-e", " ".join(shlex.quote(a) for a in ssh), "--", f"{src}/",
                   f"{host.target}:{src}/"],
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=CARRY_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"carry to {host.name}: rsync {src} rc={getattr(proc, 'returncode', '?')}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


def clone_moved_since_routing(repo: str, branch: str, sha: str, runner: Callable = subprocess.run) -> Optional[str]:
    """None when the Studio clone at ``repo`` is still clean on ``branch`` with HEAD at ``sha``, else the reason
    (review F2, 22:1x). Uncached on purpose: the routing record may come from :func:`assess_clone_cached`, whose
    key cannot see an unstaged edit or a new untracked file, and the file carry excludes the clone, so rebuilding
    a clone that changed since routing would lose that work."""
    from pathlib import Path as _P
    from hermes_cli import kanban_worktree_port as _kwt
    if not _kwt._clean_on_branch(_P(repo), branch, runner):
        return f"is no longer clean on {branch}"
    try:
        cur = runner(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8",
                     timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "HEAD unreadable"
    if getattr(cur, "returncode", 1) != 0 or (cur.stdout or "").strip() != sha:
        return f"HEAD moved off {str(sha)[:12]}"
    if worktree_carry_on() and not stash_carry_on():
        return None  # operator split 07:20: the stash stays on the Studio and is no precondition
    entries = stash_entries(repo, runner)
    if entries is None:
        return "stash unreadable"
    if entries and not worktree_carry_on():
        return "has a stash"  # Prism 425d85646d96: written since routing; the re-clone would drop it
    return None


def carry_clones_check(host: PoolHost, workspace: str, clones, runner: Callable = subprocess.run,
                       reservation: Optional[str] = None) -> list:
    """Every spawn-time re-check of a split carry (state moved since routing, git state a rebuild or file copy would
    lose, the caps), uncached, raising on the first refusal. The dispatcher calls it BEFORE ``carry_workspace``
    copies anything (P2 c-38aac54b3e75a212 / Prism 06179c2d4439) and hands the result to :func:`carry_clones`.
    Returns ``[(clone, dest, ignored files, (stash entries, bundle bytes))]``. t_d2f06cf6: the stash entries are read
    afresh here (one written since routing rides too) and their bundle is built before anything is copied, so a
    failed bundle refuses the spawn naming the entry, with nothing on the pool host. ``reservation``: the card's
    reservation on ``host``; a stash to carry with none refuses here, before the copy (advisory 03:2x), since the
    store ssh must run under it (:func:`carry_stash`)."""
    # Every check runs for every clone before anything is pushed, rebuilt or copied (nothing half-carried).
    staged = []
    extra_bytes = extra_entries = 0
    for c in clones:
        dest = f"{workspace.rstrip('/')}/{c['rel'].strip('/')}"
        if not carry_path_safe(dest):
            raise RuntimeError(f"carry to {host.name}: refused, clone path {dest!r} is not shell-inert")
        # Review F2 (22:1x): uncached re-check before anything is pushed or rebuilt (see clone_moved_since_routing).
        moved = clone_moved_since_routing(dest, c["branch"], c["sha"], runner)
        if moved:
            raise RuntimeError(f"carry to {host.name}: clone {c['rel']} {moved}")
        # operator F4: the clone's ignored files (not regenerable caches) land at the same paths inside it. Listed
        # afresh here, not from the routing's cached record: a file written since then is carried too.
        ignored = clone_ignored_files(dest, runner)
        if ignored is None:
            raise RuntimeError(f"carry to {host.name}: cannot list the ignored files of {c['rel']}")
        for ign in ignored:
            try:
                extra_bytes += os.lstat(os.path.join(dest, ign)).st_size
            except OSError:
                raise RuntimeError(f"carry to {host.name}: clone {c['rel']} ignored file {ign!r} unreadable")
            extra_entries += 1
        stash = (), None
        if not _stash_carried():
            # operator split 07:20 11-12: no stash step; the count left on the Studio is for the record only
            c["stash_left"] = stash_count(dest, runner)
        else:
            entries = stash_entries(dest, runner)
            if entries is None:
                raise RuntimeError(f"carry to {host.name}: clone {c['rel']} stash unreadable")
            if entries and not reservation:
                raise RuntimeError(f"carry to {host.name}: clone {c['rel']}: no placement reservation for this card; "
                                   f"{', '.join(map(_stash_label, entries))} not carried")
            if entries:
                try:
                    stash = entries, stash_bundle(dest, entries, c["sha"], runner)
                except StashBundleTimeout as exc:
                    raise StashBundleTimeout(f"carry to {host.name}: clone {c['rel']}: {exc}")
                except RuntimeError as exc:
                    raise RuntimeError(f"carry to {host.name}: clone {c['rel']}: {exc}")
        staged.append((c, dest, ignored, stash))
    rebuilt = {c["rel"].strip("/") for c in clones}
    lost = split_worktree_loss(workspace, clones, runner)
    if lost is not None:
        raise RuntimeError(f"carry to {host.name}: {lost}")  # t_d2f06cf6: re-checked uncached, before any push
    dangling = carried_gitdir_loss(workspace, sorted(set(carry_split_setaside(workspace)) - rebuilt), rebuilt)
    if dangling is not None:
        raise RuntimeError(f"carry to {host.name}: {dangling}")  # Prism 805c10f0cb3d: re-read before any push
    if extra_entries or set(carry_split_setaside(workspace)) - rebuilt:
        # Review F3 (22:1x): the spawn-time list gets the routing's caps again; a file that grew or appeared since
        # routing can push the carry over, and then the spawn fails with the same named reason the wait line uses.
        # t_d2f06cf6: a clone or linked worktree carried as files (not rebuilt) is re-walked here, uncached.
        over = carry_split_fits(workspace, extra_bytes, extra_entries, rebuilt=rebuilt, fresh=True)
        if over is not None:
            raise RuntimeError(f"carry to {host.name}: {over}")
    return staged


def carry_clones(host: PoolHost, workspace: str, clones, fleet_dir, runner: Callable = subprocess.run,
                 reservation: Optional[str] = None, staged: Optional[list] = None) -> None:
    """Rebuild each skipped clone on ``host`` (t_9965b8d5): push its unpushed commits first (force-with-lease
    pinned to the origin sha the routing saw), clone origin at ``<workspace>/<rel>`` on the same branch, then
    put HEAD on the exact commit. Raises on any failure (a spawn failure: claim released, nothing half-run).
    ``reservation``: the card's reservation on ``host``, threaded to :func:`carry_stash`. ``staged``: the return of
    the :func:`carry_clones_check` the dispatcher ran before the copy (t_d2f06cf6, operator 02:0x: the stash bundle
    is built once per spawn); None = run the check here. A ``staged`` result was taken before ``carry_workspace``
    copied, so each checkout is re-read cheaply first (advisory 03:2x): HEAD and branch unchanged, still clean, and
    the same stash list. Any difference refuses the spawn naming it; the bundle is never rebuilt in this tick."""
    from hermes_cli import kanban_worktree_port as _kwt
    if staged is None:
        staged = carry_clones_check(host, workspace, clones, runner, reservation=reservation)
    else:
        for c, dest, ignored, (entries, _bundle) in staged:
            moved = clone_moved_since_routing(dest, c["branch"], c["sha"], runner)
            if moved:
                raise RuntimeError(f"carry to {host.name}: clone {c['rel']} {moved} during the copy")
            # operator 07:20 item 3: the ignored files are listed AGAIN after the copy; a difference refuses, the
            # same way a moved HEAD does (a file written during the copy would otherwise be left behind)
            now_ignored = clone_ignored_files(dest, runner)
            if now_ignored is None:
                raise RuntimeError(f"carry to {host.name}: cannot list the ignored files of {c['rel']} after the copy")
            if tuple(now_ignored) != tuple(ignored):
                raise RuntimeError(f"carry to {host.name}: clone {c['rel']} ignored files changed during the copy "
                                   f"({len(ignored)} staged, {len(now_ignored)} now)")
            if _stash_carried():
                now = stash_entries(dest, runner)
                if now is None:
                    raise RuntimeError(f"carry to {host.name}: clone {c['rel']} stash unreadable after the copy")
                if tuple(now) != tuple(entries):
                    raise RuntimeError(f"carry to {host.name}: clone {c['rel']} stash changed during the copy "
                                       f"({len(entries)} staged, {len(now)} now)")
    for c, dest, ignored, (entries, bundle) in staged:
        if c.get("push") is not None:
            _kwt.push_card_branch(tuple(c["push"]), runner)
        route = _kwt.derived_route(c["slug"], fleet_dir, (host.name,))
        mirror = _kwt.mirror_for((c["slug"], *route), host.name) if route else \
            f"{_kwt.DERIVED_MIRROR_ROOT}/{c['slug']}.git"
        # the one allowlisted clone spawn, told to end on the Studio's commit (rc 5 otherwise: raises)
        _kwt.clone_on_host(host, dest, c["slug"], mirror, c["branch"], runner, sha=c["sha"], exact=True)
        if ignored:
            ssh = _ssh_argv(host, "")[:-2]
            proc = runner(["rsync", "-a", "--from0", "--files-from=-", "-e", " ".join(shlex.quote(a) for a in ssh),
                           "--", f"{dest}/", f"{host.target}:{dest}/"],
                          input="\0".join(ignored) + "\0", capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=CARRY_TIMEOUT_SECONDS)
            if getattr(proc, "returncode", 1) != 0:
                raise RuntimeError(f"carry to {host.name}: ignored files of {c['rel']} rc="
                                   f"{getattr(proc, 'returncode', '?')}: "
                                   f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
        if entries:
            # t_d2f06cf6: the Studio checkout's stash entries, stored on the rebuilt clone in their original order
            carry_stash(host, dest, dest, entries, c["sha"], runner, bundle=bundle, reservation=reservation)


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
    if host == boot or (host is not None and not boot):
        return host
    # A re-apply that lands on ANOTHER host than the boot placement also ran
    # this call off the boot host: same counter (Prism 2f2377ff612f).
    where = "on the Studio" if host is None else f"on {host}, not boot host {boot}"
    if host is not None:
        why = "placement env names another host"
    _reapply["failed"] += 1
    if not _reapply["warned"]:
        _reapply["warned"] = True
        _log.warning(
            "kanban worker placement re-apply failed on %s: %s; this call ran %s",
            tool, why, where,
        )
    _record_reapply_failure(_reapply["failed"])
    return host


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
