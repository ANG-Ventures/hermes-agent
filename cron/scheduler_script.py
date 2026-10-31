"""Cron pre-run script execution: timeouts, Windows venv bootstrap, process-tree termination,
and the claim-heartbeat thread that keeps a long script's run claim alive.

Split out of ``cron.scheduler``. Import names from this module directly (``cron.scheduler`` only
imports the few it calls itself). Origin-resident helpers and sibling split modules are reached
late-bound (``_sched`` / module refs at the bottom) so monkeypatching the defining module works.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from cron.env_settings import cron_env_setting
from cron.jobs import _ensure_cron_dir
from cron import script_outcome_records as _outcome_records
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from hermes_cli._subprocess_compat import windows_hide_flags

if TYPE_CHECKING:
    from cron.scheduler import _CancelEventLike

# Log-record parity with the origin module.
logger = logging.getLogger("cron.scheduler")


def _positive_int(raw) -> Optional[int]:
    """``int(float(raw))`` when > 0, else None; raises on unparsable input."""
    timeout = int(float(raw))
    return timeout if timeout > 0 else None


def _timeout_from_env_or_config(
    env_var: str, config_key: str, parse: Callable[[Any], Any], label: str):
    """Shared env → ``cron.<config_key>`` resolution. ``parse`` returns the value or None to keep
    looking; a parse error on the env var WARNs, on config DEBUGs. None when neither yields."""
    env_value = cron_env_setting(env_var).strip()
    if env_value:
        try:
            value = parse(env_value)
            if value is not None:
                return value
        except Exception:
            logger.warning("Invalid %s=%r; using config/default", env_var, env_value)
    try:
        cfg = _sched.load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get(config_key)
        if configured is not None:
            value = parse(configured)
            if value is not None:
                return value
    except Exception as exc:
        logger.debug("Failed to load %s from config: %s", label, exc)
    return None


def _get_script_timeout() -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if _sched._SCRIPT_TIMEOUT != _sched._DEFAULT_SCRIPT_TIMEOUT:
        try:
            timeout = _positive_int(_sched._SCRIPT_TIMEOUT)
            if timeout is not None:
                return timeout
        except Exception:
            logger.warning(
                "Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default",
                _sched._SCRIPT_TIMEOUT)
    resolved = _timeout_from_env_or_config(
        "HERMES_CRON_SCRIPT_TIMEOUT", "script_timeout_seconds", _positive_int,
        "cron script timeout",
    )
    return _sched._DEFAULT_SCRIPT_TIMEOUT if resolved is None else resolved


_DEFAULT_MEDIA_SEND_TIMEOUT = 300


def _get_media_send_timeout() -> int:
    """Per-attachment media-send timeout: HERMES_CRON_MEDIA_SEND_TIMEOUT env, then
    ``cron.media_send_timeout_seconds``, then 300s (long TTS audio can exceed a 30s window)."""
    resolved = _timeout_from_env_or_config(
        "HERMES_CRON_MEDIA_SEND_TIMEOUT", "media_send_timeout_seconds", _positive_int,
        "cron media-send timeout")
    return _DEFAULT_MEDIA_SEND_TIMEOUT if resolved is None else resolved


def _get_session_db_timeout() -> float:
    """Bound on run_job's SessionDB init: HERMES_CRON_SESSION_DB_TIMEOUT env, then
    ``cron.session_db_timeout_seconds`` (in DEFAULT_CONFIG), then 10s. Unlike sibling timeouts,
    0 is meaningful (unlimited, debugging opt-in), so values pass through untouched."""
    resolved = _timeout_from_env_or_config(
        "HERMES_CRON_SESSION_DB_TIMEOUT", "session_db_timeout_seconds", float,
        "cron.session_db_timeout_seconds")
    return 10.0 if resolved is None else resolved


def _read_windows_pyvenv_cfg(venv_dir: Path) -> dict[str, str]:
    try:
        lines = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    return {
        key.strip().lower(): value.strip()
        for key, value in (raw.split("=", 1) for raw in lines if "=" in raw)
    }


# Mirrors ``python script.py``: sys.path[0] is the script's directory (none under -P), a real
# ``__main__`` module that outlives the body (atexit/threads can still pickle its classes; a
# runpy temp module is swapped out when the body returns), plus the live checkout next.
_POSIX_SCRIPT_BOOTSTRAP = """\
import importlib.machinery, os, sys, types
repo, script = sys.argv[1], sys.argv[2]
sys.argv = [script] + sys.argv[3:]
if sys.flags.safe_path:
    sys.path.insert(0, repo)
else:
    sys.path[0:1] = [os.path.dirname(script), repo]
main = types.ModuleType("__main__")
main.__file__ = script
main.__loader__ = importlib.machinery.SourceFileLoader("__main__", script)
main.__cached__ = None
main.__builtins__ = __builtins__
sys.modules["__main__"] = main
with open(script, "rb") as f:
    code = compile(f.read(), script, "exec")
exec(code, main.__dict__)
"""


def _posix_cron_script_argv(script: Path) -> tuple[list[str], dict[str, str]]:
    """POSIX managed-store installs run cron ``.py`` scripts on the selected dependency venv's
    interpreter: the store Python has the repo and managed site-packages only on its in-process
    ``sys.path``, so its children import neither (#123044). No ``PYTHONPATH``: everything the
    script spawns would inherit it and a foreign interpreter would load the store's compiled
    extensions (#123440). The venv resolves Hermes from its generation's workspace snapshot,
    rebuilt only on a dependency change, so the bootstrap puts the live checkout first.
    Lazy installs are off for the script's process tree: a script importing ``hermes_bootstrap``
    could otherwise complete a source update and ``execv`` itself onto the bare store Python."""
    from hermes_cli._launchers import resolve_store_python
    from pm.environments import project_python

    repo = Path(__file__).resolve().parents[1]
    if resolve_store_python(repo) is None:
        return [sys.executable, str(script)], {}
    # project_python, not committed_venv as on Windows: a pre-PM venv selected before the first
    # commit runs on its OWN interpreter here, so there is no ABI mix (#122183).
    python = project_python(repo)
    if not python.is_file():
        # The caller's interpreter is the bare store Python here — the #123044 failure mode.
        raise RuntimeError(f"dependency environment interpreter is missing: {python}")
    return ([str(python), "-c", _POSIX_SCRIPT_BOOTSTRAP, str(repo), str(script)],
            {"HERMES_DISABLE_LAZY_INSTALLS": "1"})


def _windows_cron_python_invocation(python_exe: str) -> tuple[str, dict[str, str]]:
    """Hidden, output-capable Python invocation for Windows cron scripts. ``pythonw.exe`` loses
    captured output; uv venv launchers can re-exec the base console python and flash a window
    even with CREATE_NO_WINDOW, so run the base python directly with venv paths overlaid in env."""
    if sys.platform != "win32":
        return python_exe, {}

    interpreter = _sched.Path(python_exe)
    venv_dir = interpreter.parent.parent
    env_overlay: dict[str, str] = {}

    if interpreter.name.lower() == "pythonw.exe":
        sibling = interpreter.with_name("python.exe")
        if sibling.exists():
            interpreter = sibling

    from hermes_cli._launchers import resolve_store_python
    from pm.environments import committed_venv, site_packages as dependency_site

    repo = Path(__file__).resolve().parents[1]
    managed_python = resolve_store_python(repo)
    if managed_python is not None:
        # Only the committed generation may overlay the store Python: ``selected_venv``
        # falls back to the leftover pre-PM <root>/venv, a foreign ABI (#122183). With
        # nothing committed, fall through so the handed venv keeps its own interpreter
        # and site-packages (a bare store Python would die on its first import).
        environment = committed_venv(repo)
        if environment is not None:
            return str(managed_python), {"PYTHONPATH": os.pathsep.join(
                [str(repo), str(dependency_site(environment))])}

    cfg = _read_windows_pyvenv_cfg(venv_dir)
    home = cfg.get("home", "")
    site_packages = venv_dir / "Lib" / "site-packages"
    if "uv" in cfg and home:
        base_python = _sched.Path(home) / "python.exe"
        if base_python.exists() and site_packages.exists():
            interpreter = base_python
            env_overlay["VIRTUAL_ENV"] = str(venv_dir)
            pythonpath_entries = [
                str(_sched.Path(__file__).resolve().parents[1]), str(site_packages)]
            existing_pythonpath = os.environ.get("PYTHONPATH", "")
            if existing_pythonpath:
                pythonpath_entries.append(existing_pythonpath)
            env_overlay["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    return str(interpreter), env_overlay


def _terminate_cron_script_process(proc: subprocess.Popen) -> None:
    """Best-effort hard stop of a cron script and every child it spawned."""
    if proc.poll() is not None:
        return
    if sys.platform != "win32":
        _terminate_process_group(proc)
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                creationflags=windows_hide_flags(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.0)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """POSIX: TERM the script's process group, then KILL if ANY member survived (a survivor holds
    the pipe write ends open and the caller's communicate() would block on EOF forever)."""
    try:
        process_group = os.getpgid(proc.pid)
        os.killpg(process_group, signal.SIGTERM)  # windows-footgun: ok — POSIX-only branch
    except (ProcessLookupError, PermissionError, OSError):
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1.0)
    try:
        os.killpg(process_group, 0)  # windows-footgun: ok — POSIX-only branch
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress((ProcessLookupError, PermissionError, OSError)):
        os.killpg(process_group, getattr(signal, "SIGKILL", signal.SIGTERM))


def _terminate_cron_script_tree(proc: subprocess.Popen) -> None:
    """Terminate a script tree, then fall back to the local process-group path."""
    if proc.poll() is not None:
        # Already reaped: kill_process_tree would log a spurious "no signal" warning.
        return
    def fallback(reason: str, *args, exc_info: bool = False) -> None:
        logger.warning(
            reason + "; falling back to process-group termination", *args, exc_info=exc_info)
        _terminate_cron_script_process(proc)

    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return fallback("Cron script tree-kill received invalid pid %r", pid)
    try:
        # Function-local (monkeypatchable); separate try so an import problem is not
        # misreported as a kill failure.
        from agent.deadline import kill_process_tree
    except Exception:
        return fallback("agent.deadline.kill_process_tree unavailable", exc_info=True)
    try:
        if kill_process_tree(pid):
            return
    except Exception:
        return fallback("Cron script tree-kill failed for pid %s", pid, exc_info=True)
    fallback("Cron script tree-kill reported no signal for pid %s", pid)


def _script_tree_snapshot(proc: subprocess.Popen) -> Optional[list]:
    """psutil handles for the script leader and every descendant, taken BEFORE any signal: a setsid
    grandchild is reparented once its parent dies and is then unreachable from the leader (#85125).
    None when psutil is unavailable or the leader is already gone."""
    try:
        import psutil
        root = psutil.Process(proc.pid)
    except Exception:
        return None
    try:
        return [root] + root.children(recursive=True)
    except Exception:
        # A zombie leader has no live children of its own to list; anything else falls back.
        with contextlib.suppress(Exception):
            if root.status() == psutil.STATUS_ZOMBIE:
                return [root]
        return None


# Per-run token in every script child's env, inherited by everything it spawns (Prism
# 85c0395583d8): a TERM handler can fork a child that calls setsid() and exit, so the child is in no
# snapshot, descends from no surviving member and sits outside the script's process group. Ancestry
# cannot find it; the token in its environment can. Set on the Popen as ``_SCRIPT_RUN_TOKEN_ATTR``.
SCRIPT_RUN_TOKEN_ENV = "_HERMES_CRON_SCRIPT_RUN"
_SCRIPT_RUN_TOKEN_ATTR = "_hermes_script_run_token"
_SCRIPT_RUN_STARTED_ATTR = "_hermes_script_run_started"  # time.time() just before the Popen


class _UnprovenMember:
    """Stand-in for "the token scan could not run": counts as alive, so the re-fire is withheld
    rather than run beside an escaped copy that could not be ruled out."""

    pid = -1

    def is_running(self) -> bool:
        return True

    def status(self) -> str:
        return "unproven"

    def children(self, recursive: bool = False) -> list:
        return []

    def send_signal(self, _sig) -> None:
        return None


class _UnreadableMember:
    """A same-user process started after the script whose environment cannot be read (Prism
    952be3262777: a token-inheriting escapee can make itself non-dumpable). It may be ours, so it
    counts as alive while it runs and the re-fire is withheld; it may not be, so it is never
    signalled."""

    def __init__(self, process) -> None:
        self._process = process
        self.pid = process.pid

    def is_running(self) -> bool:
        return self._process.is_running()

    def status(self) -> str:
        return self._process.status()

    def children(self, recursive: bool = False) -> list:
        return []

    def send_signal(self, _sig) -> None:
        return None


# Re-derived by every token scan, never carried from an earlier snapshot (Prism 06a05538f9a0).
_SCAN_MARKERS = (_UnprovenMember, _UnreadableMember)


def _is_orphaned(cand, started: float) -> bool:
    """Whether ``cand``'s parent is init or a subreaper: pid 1, or a parent that predates the script
    and is named like one. Unreadable parent: not proven orphaned."""
    ppid = cand.ppid()
    if ppid <= 1:
        return True
    import psutil
    try:
        parent = psutil.Process(ppid)
        return (parent.create_time() < started
                and parent.name() in ("systemd", "init", "launchd", "tini", "s6-svscan"))
    except psutil.Error:
        return False


def _script_run_token_members(proc: subprocess.Popen) -> list:
    """Live processes whose environment carries ``proc``'s run token, wherever they were reparented
    or whichever session they moved to. [] when ``proc`` has no token; ``[_UnprovenMember()]`` when
    the scan itself fails. A process that is gone, or another user's, is skipped. An orphaned
    same-user process started since the script whose environ is unreadable is returned as an
    ``_UnreadableMember`` (Prism 952be3262777): counted alive, never signalled. Orphaned (parent is
    init or a subreaper, where an escapee of an exited parent lands) keeps an unrelated
    non-dumpable process with a live parent (sudo, ssh) from withholding the re-fire."""
    token = getattr(proc, _SCRIPT_RUN_TOKEN_ATTR, None)
    if not token:
        return []
    started = getattr(proc, _SCRIPT_RUN_STARTED_ATTR, None)
    try:
        import psutil
        found = []
        uid = os.getuid()  # windows-footgun: ok — POSIX-only caller (cron script runner)
        for cand in psutil.process_iter():
            if cand.pid == os.getpid():
                continue
            try:
                if cand.environ().get(SCRIPT_RUN_TOKEN_ENV) == token:
                    found.append(cand)
            except psutil.AccessDenied:
                with contextlib.suppress(psutil.Error):
                    if (started is not None and cand.uids().real == uid
                            and cand.create_time() >= started - 1.0
                            and cand.status() != psutil.STATUS_ZOMBIE
                            and _is_orphaned(cand, started)):
                        found.append(_UnreadableMember(cand))
            except psutil.Error:
                continue
        return found
    except Exception:
        logger.warning("cron script run-token scan failed for pid %s; treating the tree as alive",
                       getattr(proc, "pid", None), exc_info=True)
        return [_UnprovenMember()]


def _script_tree_refresh(proc: subprocess.Popen, snapshot: Optional[list]) -> Optional[list]:
    """``snapshot`` plus every descendant spawned since (Prism 361a15209668): the leader's live tree
    if it still runs, and the children of every snapshot member still running, so a process forked
    during the TERM grace is signalled and counted even after the leader is reaped. Also every
    process carrying the run's env token (Prism 85c0395583d8: a setsid escapee of an exited
    parent)."""
    if snapshot is None:
        return _script_tree_snapshot(proc)
    # A marker from an earlier scan says nothing about now: drop it, the scan below re-adds it only
    # if it still applies (Prism 06a05538f9a0).
    snapshot = [m for m in snapshot if not isinstance(m, _SCAN_MARKERS)]
    merged = list(snapshot)
    fresh = _script_tree_snapshot(proc) if proc.poll() is None else None
    # Members of the script's own process group (its pgid is the leader pid): a same-group child
    # whose parent already exited is reparented away from every snapshot member.
    group: list = []
    with contextlib.suppress(Exception):
        import psutil
        for cand in psutil.process_iter():
            with contextlib.suppress(Exception):
                if os.getpgid(cand.pid) == proc.pid:
                    group.append(cand)
    for member in list(snapshot) + (fresh or []) + group + _script_run_token_members(proc):
        found = [member] if member not in merged else []
        with contextlib.suppress(Exception):
            if member.is_running():
                found += member.children(recursive=True)
        merged += [m for m in found if m not in merged]
    return merged


def _script_tree_alive(proc: subprocess.Popen, snapshot: Optional[list]) -> bool:
    """Whether any member of ``snapshot`` still runs: same pid AND create_time (psutil's
    is_running, pid-reuse safe) and not a zombie, so a zombie-only tree counts as gone. Without a
    snapshot, falls back to probing the script's process group (its pgid is the leader pid)."""
    if snapshot is None:
        try:
            os.killpg(proc.pid, 0)  # windows-footgun: ok — POSIX-only caller (cron script runner)
        except ProcessLookupError:
            return False
        except Exception:
            return True
        return True
    import psutil
    for member in snapshot:
        try:
            if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            continue
        except Exception:
            return True  # cannot tell: count it alive, so no re-fire runs beside it
    return False


def _signal_script_tree(proc: subprocess.Popen, snapshot: Optional[list], sig: int) -> None:
    """Signal the whole script tree: the leader's live tree through the same unified tree-kill the
    timeout and cancel paths use (SIGKILL: ``_terminate_cron_script_tree``), then every snapshot
    member still running, which reaches descendants orphaned from the leader. No snapshot: the
    process group only."""
    if snapshot is None:
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, sig)  # windows-footgun: ok — POSIX-only caller (cron script runner)
        return
    if sig == getattr(signal, "SIGKILL", None):
        _terminate_cron_script_tree(proc)
    else:
        with contextlib.suppress(Exception):
            from agent.deadline import kill_process_tree
            kill_process_tree(proc.pid, sig=sig)
    for member in snapshot:
        with contextlib.suppress(Exception):
            member.send_signal(sig)  # psutil checks pid + create_time before signalling


def _drain_script_pipes(proc: subprocess.Popen) -> None:
    """Reap a terminated script without blocking forever: a surviving descendant can hold the pipe
    write ends open, so bound the drain and abandon the pipes (output is not needed)."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=5.0)
        return
    with contextlib.suppress(OSError):
        proc.kill()
    for stream in (proc.stdout, proc.stderr):
        with contextlib.suppress(OSError):
            if stream is not None:
                stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5.0)


def _windows_cron_bootstrap_argv(
    python_exe: str, env_overlay: dict[str, str], script_path: str) -> list[str]:
    """Bootstrap a cron script under the base interpreter with ``.pth`` support. Overlay mode puts
    the venv on ``PYTHONPATH``, but ``.pth`` files are only processed by ``site.addsitedir()``, so
    editable installs would be invisible; bootstrap via addsitedir + ``runpy.run_path`` (keeps
    ``__file__``/``sys.path[0]`` semantics). Plain invocation if the venv is unresolvable."""
    site_packages = next((Path(item) for item in env_overlay.get("PYTHONPATH", "").split(os.pathsep)
                          if Path(item).name == "site-packages"),
                         _sched.Path(env_overlay.get("VIRTUAL_ENV", "")) / "Lib" / "site-packages")
    if not site_packages.is_dir():
        # Warn: silent fallback would make "editable installs invisible" undiagnosable.
        logger.warning(
            "Windows cron script: venv site-packages %s not found; running "
            "without .pth processing (editable installs may be unimportable)",
            site_packages)
        return [python_exe, script_path]
    bootstrap = (
        "import os, runpy, site, sys;"
        f"site.addsitedir({str(site_packages)!r});"
        "script = sys.argv[1];"
        "sys.argv = [script] + sys.argv[2:];"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(script)));"
        "runpy.run_path(script, run_name='__main__')"
    )
    return [python_exe, "-c", bootstrap, script_path]


def _resolve_script_path(script_path: str) -> tuple[Optional[Path], Optional[str]]:
    """Validate a job script path; ``(path, None)`` or ``(None, error)``. Scripts MUST resolve
    inside HERMES_HOME/scripts/ (relative, absolute and ``~`` paths are all validated — path
    traversal / absolute-path injection); contract of lifecycle_guard._expand_candidate_path."""
    scripts_dir = _sched._get_hermes_home() / "scripts"
    _ensure_cron_dir(scripts_dir)
    scripts_dir_resolved = scripts_dir.resolve()

    # Reject NUL eagerly: on Windows Path ops raise ValueError *after* expanduser so the try below
    # would not catch it. str() first so the guard itself cannot raise on a non-str script_path.
    # Same ingestion contract as cron.lifecycle_guard._expand_candidate_path: a NUL-bearing value can never
    # name a real script, and on Windows the Path operations raise ValueError *after* expanduser (expanduser
    # never expands "~user" there, so the try below never fires) — reject eagerly so both platforms fail
    # cleanly instead of crashing the scheduler. str() first so the guard itself can never raise TypeError
    # on a non-str script_path (e.g. a Path passed by a future caller) — the guard must be crash-proof even
    # though every current call site passes a plain str (#86832 review).
    if "\x00" in str(script_path):
        return None, f"Blocked: script path contains a NUL byte: {script_path!r}"
    try:
        raw = _sched.Path(script_path).expanduser()
    except (ValueError, RuntimeError, OSError):
        # RuntimeError: unexpandable ``~`` (no resolvable HOME).
        return None, f"Blocked: script path is not a valid filesystem path: {script_path!r}"
    if raw.is_absolute():
        lexical = Path(os.path.abspath(raw))
        path = raw.resolve()
    else:
        lexical = Path(os.path.abspath(scripts_dir / raw))
        path = (scripts_dir / raw).resolve()

    # Traversal / absolute-path / symlink escape guard — MUST stay inside HERMES_HOME/scripts/.
    # One exception (fork t_04822736): an entry that lives lexically in a named profile's scripts
    # dir and is a symlink into the fleet-shared <root>/scripts is admitted — profiles legitimately
    # share scripts, and refusing that symlink left a job failing identically every tick for 19 h.
    if not _sched._script_path_admitted(path, lexical, scripts_dir, _sched._get_hermes_home()):
        return None, (
            f"Blocked: script path resolves outside the scripts directory "
            f"({scripts_dir_resolved}): {script_path!r}"
        )
    if not path.exists():
        # Scripts resolve against THIS profile's scripts/ dir by design (profiles never share files),
        # which is the usual reason a copied job cannot find a script that exists elsewhere (#94821).
        return None, (
            f"Script not found: {path}. Cron scripts are looked up only in this profile's folder "
            f"({scripts_dir_resolved}); if the job was copied from another profile, copy the script "
            f"there too, or edit the job with `hermes cron edit`."
        )
    if not path.is_file():
        return None, f"Script path is not a file: {path}"
    return path, None


def _resolve_cron_interpreter(interpreter: str) -> tuple[Optional[str], Optional[str]]:
    """``(python_exe, error)`` for a job's ``interpreter`` field. Checked at run time, not create
    time: a user venv can be rebuilt or moved while the job lives. Bare names are refused — they
    silently change meaning with PATH. The name (and the symlink target's name) must look like a
    Python: the lifecycle guard classifies ``.py`` scripts as Python and skips its shell reference
    walk, so ``interpreter=/bin/bash`` would run an unscanned ``.py`` body as shell. ``pythonw``
    is refused because it discards captured output (an agent job would go silently quiet)."""
    from cron.lifecycle_guard import _INTERPRETER_IMAGE_RE

    raw = interpreter.strip()
    try:
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            return None, (f"Interpreter must be an absolute or ~-prefixed path (got {raw!r}). "
                          "Bare names like 'python3' are not stable across PATH changes.")
        mode = resolved.stat().st_mode
        names = {resolved.name.lower(), resolved.resolve().name.lower()}
    except FileNotFoundError:
        return None, f"Interpreter not found: {raw}"
    except (RuntimeError, OSError) as exc:  # unknown ~user, unreadable parent, symlink loop
        return None, f"Unable to resolve interpreter path {raw!r}: {exc}"
    if not stat.S_ISREG(mode):
        return None, f"Interpreter path is not a file: {resolved}"
    if sys.platform != "win32" and not mode & 0o111:
        return None, f"Interpreter is not executable: {resolved}"
    if not all(_INTERPRETER_IMAGE_RE.match(name) and not name.startswith("pythonw")
               for name in names):
        return None, f"Interpreter must be a Python executable (python, python3, python3.12, ...): {resolved}"
    return str(resolved), None


def _script_argv(
    path: Path, interpreter: Optional[str] = None,
) -> tuple[Optional[list[str]], dict[str, str], Optional[str]]:
    """``(argv, env_overlay, error)`` for a validated script. Interpreter by extension — the
    shebang is deliberately NOT honoured (small, auditable surface): ``.sh``/``.bash`` → bash,
    else the job's ``interpreter`` when set, else a Python chosen by ``_posix_cron_script_argv``
    / ``_windows_cron_python_invocation``. Interpreter selection reads PM's install records and
    may raise; callers run this inside their ``try``."""
    if path.suffix.lower() in {".sh", ".bash"}:
        # which() finds Git Bash on Windows; None there → clear error instead of a "[WinError 2]".
        _bash = shutil.which("bash") or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
        if _bash is None:
            return None, {}, (
                f"Cannot run .sh/.bash script {path.name!r}: bash not found on PATH. "
                "On Windows, install Git for Windows (which ships Git Bash) "
                "or rewrite the script as Python (.py)."
            )
        return [_bash, str(path)], {}, None
    if isinstance(interpreter, str) and interpreter.strip():
        # A user venv gets none of the managed-store overlays: the repo bootstrap / PYTHONPATH
        # exist to run Hermes' own dependency venv and would shadow the user's packages.
        python_exe, err = _resolve_cron_interpreter(interpreter)
        return ([python_exe, str(path)] if python_exe else None), {}, err
    if sys.platform != "win32":
        argv, env_overlay = _posix_cron_script_argv(path)
        return argv, env_overlay, None
    python_exe, env_overlay = _windows_cron_python_invocation(sys.executable)
    if env_overlay:
        return _windows_cron_bootstrap_argv(python_exe, env_overlay, str(path)), env_overlay, None
    return [python_exe, str(path)], env_overlay, None


# Fleet default GitHub lane for cron script children (fork t_f0780685). A plain script that ran
# a bare `gh` without naming a lane spent the shared stored login. When this home ships the gh
# shim (var/gh-shim/gh), the child gets the shim first on PATH plus a DEFAULT lane the shim uses
# only when the script sets no lane itself (export / setdefault still win), and the script's
# path so the shim keeps audited stored-login sites on the stored login. No shim -> the env is
# untouched. Tests import both names from here; the facade (cron.scheduler) carries the same
# constant for github-apps-lint.
CRON_SCRIPT_DEFAULT_GH_LANE = "watchers"


def _apply_cron_default_gh_lane(env: dict, script: Path) -> None:
    return _sched._apply_cron_default_gh_lane(env, script)


def _run_job_script(
    script_path: str, workdir: Optional[str] = None,
    cancel_event: Optional[_CancelEventLike] = None, interpreter: Optional[str] = None,
    *, timeout_seconds: Optional[int] = None, job_name: Optional[str] = None,
    job_id: Optional[str] = None, execution_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Execute a cron job's script and return ``(success, output)``; on failure *output* is the
    error message for the LLM to report. Env goes through ``build_subprocess_env`` (SECURITY.md
    §2.3). ``workdir`` sets the subprocess cwd only; the Python process cwd is NEVER mutated (an
    ``os.chdir()`` would leak into concurrent gateway sessions).

    Args: script_path: Path to the script. Relative paths are resolved against HERMES_HOME/scripts/.
    Absolute and ~-prefixed paths are also validated to ensure they stay within the scripts dir. workdir:
    Optional absolute path to use as the script's cwd. When set, the subprocess runs in this directory
    instead of the scripts-dir parent. See #69396. interpreter: the job's optional Python for
    ``.py`` scripts (#8714). timeout_seconds: per-job ceiling (``_job_script_kwargs``) that may
    only LOWER the global ``cron.script_timeout_seconds`` cap. job_name / job_id: for the timeout
    log line and the shutdown-kill re-fire flag. execution_id: a no_agent job's attempt; when set
    the script's exit status is also written to a durable record so a run that outlives this
    process is graded by its real outcome after restart (cron/script_outcome_records.py).
    """
    path, err = _resolve_script_path(script_path)
    if path is None:
        return False, err
    # Refuse to START new script work once the gateway has begun draining: the ticker could
    # otherwise dispatch a fresh long-running script a second before SIGTERM and hand the drain
    # brand-new work to wait out (fork #1307).
    if _sched.is_shutting_down():
        # Not a job failure: the run body records it as a skip (cron/scheduler_shutdown_skip.py).
        from cron.scheduler_shutdown_skip import note_shutdown_refused
        note_shutdown_refused(job_id)
        return False, (
            f"Skipped: cron scheduler is shutting down (not starting {path.name})"
        )
    # The global cap is the hard ceiling; a per-job ceiling may only LOWER it (a 15-min job
    # with a 2 h ceiling otherwise piles up overlapping wedged instances, fork #1027).
    script_timeout = _get_script_timeout()
    if timeout_seconds is not None:
        try:
            _job_timeout = int(timeout_seconds)
        except (TypeError, ValueError):
            _job_timeout = 0
        if _job_timeout > 0:
            script_timeout = min(script_timeout, _job_timeout)
    outcome_record = None
    proc = None
    try:
        argv, env_overlay, err = _script_argv(path, interpreter)
        if argv is None:
            return False, err
        if execution_id and job_id:
            outcome_record = _outcome_records.begin(job_id, execution_id)
            if outcome_record is not None:
                argv = _outcome_records.wrap_argv(outcome_record, argv)
        from tools.environments.local import build_subprocess_env
        # Lossy decode only: keep the platform-default (locale) encoding — gating ``encoding=``
        # to win32 was deliberate (#66566: unconditional UTF-8 leaked into POSIX) — but
        # ``errors=`` must not stay 'strict': one stray non-UTF-8 byte in the script's stdout
        # or stderr raises UnicodeDecodeError in communicate() and fails the whole run,
        # discarding the output (#105582; the Windows branch decodes lossily per #45099).
        popen_kwargs: dict[str, Any] = {
            "start_new_session": True,
            "errors": "replace",
        }
        if sys.platform == "win32":
            popen_kwargs = {
                "creationflags": windows_hide_flags()
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                # Lossy UTF-8 decode — locale-mismatched bytes from the STT command must not raise in the
                # reader threads on non-UTF-8 Windows (#45099).
                # Lossy UTF-8 decode — locale-mismatched bytes from the TTS command must not raise in the
                # reader threads on non-UTF-8 Windows (#45099).
                "encoding": "utf-8",
                "errors": "replace"}
        # The process env is the LAUNCH profile's. For a job owned by a routed profile, drop that
        # profile's .env residue from the base first (no-op for the launch profile's own jobs), then
        # overlay the routed profile's own scope (which never enters os.environ under multiplex
        # semantics) before the sanitizer, which also overlays the names the owning profile declares
        # in terminal.env_passthrough from that scope (#114209). The factory snapshots the process
        # env itself — no raw copy at the spawn site (test_subprocess_env_guard).
        env = build_subprocess_env(strip_launch_profile=True)
        # A script child is a plain script, not an agent process. The gateway advertises itself
        # via the agent markers in its OWN os.environ (gateway.run.main), so without this every
        # cron script inherited them and fleet tooling keyed on them (the gh shim's profile-wins
        # lane resolution) misclassified laned no_agent crons as the gateway profile
        # (t_7fee0f83). Any agent a script launches re-advertises itself.
        for _agent_marker in ("AI_AGENT", "HERMES_AGENT"):
            env.pop(_agent_marker, None)
        # Same reason for the gateway's agent.process_env_files overlay (the gh lane PATH shim +
        # git credential helper, t_45c11886): a script child keeps the env it had before this
        # gateway sourced those files.
        from hermes_cli.process_env_files import strip_overlay

        strip_overlay(env)
        _apply_cron_default_gh_lane(env, path)
        # Mark the child as a cron script so ``hermes kanban create`` can
        # classify it (refused when no home resolves, D-O1 t_6281f908) and
        # record which job minted a card (the orphan watch maps it to the
        # job's deliver chat).
        env["HERMES_CRON_JOB_ID"] = str(job_id or job_name or path.name)
        # Runner role only: the _HERMES_GATEWAY marker a gateway-started script inherits (§5.6).
        from hermes_cli.process_bootstrap import apply_child_env
        apply_child_env(env)
        env.update(env_overlay)
        run_token = uuid.uuid4().hex
        env[SCRIPT_RUN_TOKEN_ENV] = run_token  # finds setsid escapees (Prism 85c0395583d8)
        run_started = time.time()
        # Subprocess cwd only (default: scripts-dir parent). NEVER os.chdir() the process.
        # Use the job's workdir as the subprocess cwd when configured, otherwise default to the scripts-dir
        # parent (back-compat). NEVER mutate the Python process cwd — that would leak into concurrent
        # gateway sessions (#69396).
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=workdir or str(path.parent), env=env, **popen_kwargs)
        setattr(proc, _SCRIPT_RUN_TOKEN_ATTR, run_token)
        setattr(proc, _SCRIPT_RUN_STARTED_ATTR, run_started)
        if outcome_record is not None:
            _outcome_records.note_pid(outcome_record, proc.pid)
        # Registered so the shutdown drain can TERMINATE this script (see
        # cron.scheduler.terminate_running_scripts); deregistered on EVERY exit so a later drain
        # never signals a dead or recycled pid. start_new_session (POSIX) / the win32
        # creationflags give the script its own group, so the drain never touches the gateway's.
        _proc_key = id(proc)
        with _sched._script_procs_lock:
            _sched._active_script_procs[_proc_key] = proc
            if job_id:
                _sched._script_proc_job_ids[_proc_key] = _sched._inflight_key(str(job_id))
        try:
            _started = time.monotonic()
            deadline = _started + script_timeout
            while True:
                # Tree-kill on cancel AND timeout: killpg misses setsid grandchildren (watchdogs,
                # backgrounded shell jobs); kill_process_tree snapshots descendants BEFORE signalling.
                if cancel_event is not None and cancel_event.is_set():
                    _terminate_cron_script_tree(proc)
                    _drain_script_pipes(proc)
                    return False, "Script cancelled because cron fire ownership was lost"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_cron_script_tree(proc)
                    _drain_script_pipes(proc)
                    # Phase 4a (#85125): a script timeout must leave ZERO living descendants. killpg only
                    # reaches the script's own process group — a grandchild that called setsid (backgrounded
                    # shell jobs, watchdogs) escapes it and keeps running after the job reports failure (#71148
                    # / #59549). agent.deadline.kill_process_tree snapshots the descendant set via psutil BEFORE
                    # signalling, so own-session grandchildren are reached too — the unified deadline layer's
                    # tree-kill (#85147, d6a5cb9725).
                    logger.warning(
                        "PHASE=cron_script_timeout job=%s elapsed=%d timeout=%d script=%s",
                        job_name or path.name, int(time.monotonic() - _started),
                        script_timeout, path.name,
                    )
                    return False, f"Script timed out after {script_timeout}s: {path}"
                try:
                    stdout_raw, stderr_raw = proc.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            with _sched._script_procs_lock:
                _sched._active_script_procs.pop(_proc_key, None)
                _skew_key = _sched._script_proc_job_ids.pop(_proc_key, None)
                _skew_cut = _skew_key is not None and _skew_key in _sched._skew_interrupted_job_ids
                if _skew_cut:
                    _sched._skew_interrupted_job_ids.discard(_skew_key)
                    if _skew_key not in _sched._refire_withheld_job_ids:
                        _sched._restart_killed_job_ids.add(_skew_key)

        stdout = (stdout_raw or "").strip()
        stderr = (stderr_raw or "").strip()

        # Redact secrets before ANY return path.
        try:
            from agent.redact import redact_sensitive_text
            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
        except Exception as e:
            logger.warning("Failed to redact sensitive text from output: %s", e)
            stdout = stderr = "[REDACTED - redaction failed]"

        if _skew_cut:
            # Cut off by the script runner's bounded code-skew drain: never a success, whatever the
            # script's own exit status (Prism b86087faa72f); its re-fire flag is set above.
            parts = [f"Script interrupted by the cron script runner's code-skew drain "
                     f"(exit {proc.returncode}); eligible for one re-fire after restart."]
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            return False, "\n".join(parts)
        if proc.returncode != 0:
            parts = [f"Script exited with code {proc.returncode}"]
            if (
                job_id
                and _sched.is_shutting_down()
                and _sched._is_shutdown_kill_returncode(proc.returncode)
            ):
                # Killed by the gateway shutdown drain (or its backstop), not a script failure:
                # flag it so run_one_job re-queues one fire (fork #462/#1429).
                _killed_key = _sched._inflight_key(str(job_id))
                with _sched._script_procs_lock:
                    if _killed_key not in _sched._refire_withheld_job_ids:
                        _sched._restart_killed_job_ids.add(_killed_key)
                parts.append(
                    "Killed by gateway shutdown mid-run; eligible for one "
                    "re-fire after restart.")
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            return False, "\n".join(parts)
        return True, stdout
    except Exception as exc:
        return False, f"Script execution failed: {exc}"
    finally:
        # A script that never started leaves no evidence worth keeping. Once it ran, the record
        # stays until the caller durably records the run (finish_execution discards it), so a
        # crash between here and mark_job_run still leaves settle() the real exit status.
        if outcome_record is not None and proc is None:
            _outcome_records.discard(outcome_record)


def _start_heartbeat_thread(loop_fn, name: str, fail_log) -> Optional[threading.Thread]:
    """Start ``loop_fn`` on a daemon thread inside a copy of the current context (multiplexed
    profile ContextVars). On failure calls ``fail_log()`` inside the except (traceback intact) and
    returns None."""
    thread = threading.Thread(
        target=contextvars.copy_context().run, args=(loop_fn,), name=name, daemon=True)
    try:
        thread.start()
    except Exception:
        fail_log()
        return None
    return thread


def _run_job_script_with_claim_heartbeat(
    job: dict, script_path: str, workdir: Optional[str] = None,
    cancel_event: Optional[_CancelEventLike] = None,
) -> tuple[bool, str]:
    """Run a cron script while heartbeating its owned one-shot claim. A long script can outlive
    the stale-claim TTL; without a heartbeat another scheduler would re-dispatch the one-shot.
    Recurring/unclaimed runs have no durable claim → no thread. The owner is captured from the
    dispatched job, never re-read, so a stale runner cannot extend a replacement owner's claim."""
    script_kwargs = _sched._job_script_kwargs(job)

    # no_agent only: the script IS the job. A pre-run script feeds an agent that dies with the
    # process, so its exit status is not the job's outcome.
    if job.get("no_agent") and job.get("execution_id"):
        script_kwargs = {**script_kwargs, "execution_id": str(job["execution_id"])}

    def run() -> tuple[bool, str]:
        return _run_job_script(script_path, workdir=workdir, cancel_event=cancel_event,
                               interpreter=job.get("interpreter"), **script_kwargs)

    schedule = job.get("schedule")
    claim = job.get("run_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if not (isinstance(schedule, dict) and schedule.get("kind") == "once" and owner):
        return run()

    job_id = str(job.get("id") or "")
    stop = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop.wait(_sched._RUN_CLAIM_HEARTBEAT_SECONDS):
            try:
                _sched.heartbeat_run_claim(job_id, expected_owner=owner)
            except Exception:
                logger.debug("Job '%s': script run_claim heartbeat failed", job_id, exc_info=True)

    heartbeat_thread = _start_heartbeat_thread(
        _heartbeat_loop, "cron-script-claim-heartbeat",
        lambda: logger.debug(
            "Job '%s': could not start script run_claim heartbeat", job_id, exc_info=True),
    )
    if heartbeat_thread is None:
        return run()

    try:
        return run()
    finally:
        stop.set()
        # Bounded join: the heartbeat may be blocked on another process's jobs-file lock.
        heartbeat_thread.join(timeout=1.0)


# Late-bound origin namespace (see module docstring). Imported LAST so this module is fully
# populated before ``scheduler`` re-exports from it.
from cron import scheduler as _sched  # noqa: E402
