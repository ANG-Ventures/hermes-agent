"""Absent-hook self-heal for shell hooks (t_1fb8de95; incident t_82a5c853, 2026-09-24 03:44).

A hook's exit is a policy verdict only if the hook's files were on disk when it ran. When a
stray ``git sparse-checkout`` removed ``hooks/``, every ``fail_closed`` hook exited 2 with
"can't open file" and the runner treated that as a policy BLOCK, locking every agent out of
its shell. This module keeps that from recurring:

* Absence is MEASURED from the filesystem: the hook script is stat'ed before exec, and the
  hook directory's tracked listing in HEAD is compared with what exists on disk. Hook output
  text is never used to classify a failure, so a present hook whose stderr happens to
  mention ``ImportError`` still yields its own verdict.
* The repair writes ONLY tracked paths that are ABSENT, taken from the owning checkout's
  HEAD object store (``git archive``). The owning checkout is resolved from the hook path
  itself, never from the active profile home, because profile homes run hooks from the
  shared root. It works under sparse checkout. Existing files are never rewritten, so
  uncommitted edits in the live hooks directory survive. Skip-worktree bits are cleared
  only on the files that were written. Sparse-checkout configuration is never modified.
* Every repair attempt pages #alerts through the fleet notify front door, deduplicated
  per hook for ten minutes across processes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.shell_hooks")

MISSING_HOOK_POLICIES: Tuple[str, ...] = ("restore_then_fail_closed", "fail_closed", "fail_open_and_page")
DEFAULT_MISSING_HOOK_POLICY = "restore_then_fail_closed"
PAGE_INTERVAL_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 5
_MAX_ARCHIVE_BYTES = 8_000_000

_page_lock = threading.Lock()
_pages: Dict[Tuple[str, str], float] = {}


def reset_pages_for_tests() -> None:
    with _page_lock:
        _pages.clear()


def _git(cwd: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=text, timeout=_GIT_TIMEOUT_SECONDS)


def owning_checkout(path: Path) -> Optional[Path]:
    """The git checkout that owns ``path``: the toplevel of its nearest existing ancestor."""
    path = path.resolve()
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    try:
        probe = _git(ancestor, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("could not locate the checkout owning shell hook %s: %s", path, exc)
        return None
    if probe.returncode != 0:
        return None
    root = Path(probe.stdout.strip()).resolve()
    return root if path.is_relative_to(root) else None


def _hook_dir(script: Path) -> Optional[Tuple[Path, Path]]:
    root = owning_checkout(script)
    if root is None:
        return None
    rel = script.resolve().relative_to(root).parent
    return None if rel == Path(".") else (root, rel)


def _absent(root: Path, rel: Path) -> Optional[List[str]]:
    """HEAD-tracked paths under ``rel`` that do not exist on disk (``None`` = listing unavailable)."""
    try:
        listing = _git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", f"{rel.as_posix()}/")
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("could not list tracked shell hook files under %s: %s", root / rel, exc)
        return None
    if listing.returncode != 0:
        return None
    return [p for p in listing.stdout.split("\0") if p and not os.path.lexists(root / p)]


def absent_tracked_files(script: Path) -> List[str]:
    """Tracked files in the hook's directory that are absent on disk. Empty when not in a checkout."""
    loc = _hook_dir(script)
    return (_absent(*loc) or []) if loc else []


def _sparse_note(root: Path) -> str:
    try:
        sparse = _git(root, "config", "--get", "core.sparseCheckout").stdout.strip().lower() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if sparse:
        logger.error("checkout %s is SPARSE (core.sparseCheckout=true): hook files were excluded by sparse-checkout", root)
        return f"; checkout {root} is SPARSE (core.sparseCheckout=true)"
    return ""


def _publish_absent(dest: Path, data: bytes, mode: int) -> bool:
    """Atomically create ``dest`` only if it does not exist; never a partial file, never a clobber.

    The bytes go to a temp file in the same directory, which is then hard-linked into place.
    ``os.link`` refuses an existing destination (including a dangling symlink), so a file
    that appeared since the absence check is left untouched.
    """
    tmp = dest.with_name(f".{dest.name}.hook-restore-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def restore_absent_files(script: Path) -> Tuple[bool, str]:
    """Write the ABSENT tracked files of the hook directory from HEAD. Existing files are never rewritten.

    Returns ``(ok, outcome)``. ``ok`` means that afterwards the script exists and no tracked file
    in its directory is absent.
    """
    script = script.resolve()
    loc = _hook_dir(script)
    if loc is None:
        return False, f"restore failed: {script} is not inside a git checkout"
    root, rel = loc
    absent = _absent(root, rel)
    if absent is None:
        return False, f"restore failed: HEAD listing of {root / rel} unavailable"
    if not absent:
        return script.is_file(), (f"nothing tracked is absent under {root / rel}" if script.is_file()
                                  else f"restore failed: {script} is not tracked in HEAD of {root}")
    wanted = set(absent)
    written: List[str] = []
    try:
        archive = _git(root, "archive", "--format=tar", f"HEAD:{rel.as_posix()}", text=False)
        if archive.returncode or len(archive.stdout) > _MAX_ARCHIVE_BYTES:
            return False, f"restore failed: HEAD:{rel.as_posix()} unavailable or too large"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
            for member in bundle:
                relpath = (rel / member.name).as_posix()
                if relpath not in wanted or not member.isfile():
                    continue
                dest = root / relpath
                if any(p.is_symlink() for p in dest.parents if p.is_relative_to(root) and p != root):
                    return False, f"restore failed: symlink on the path to {dest}"
                content = bundle.extractfile(member)
                if content is None or os.path.lexists(dest):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if _publish_absent(dest, content.read(), member.mode & 0o777):
                    written.append(relpath)
    except (OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        logger.error("could not restore shell hook files under %s from HEAD: %s", root / rel, exc)
        return False, f"restore failed under {root / rel}: {type(exc).__name__}"
    note = _sparse_note(root)
    if written:
        try:
            indexed = _git(root, "update-index", "--no-skip-worktree", "--", *written)
            if indexed.returncode:
                note += f"; update-index --no-skip-worktree rc={indexed.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            note += f"; update-index failed ({type(exc).__name__})"
    remaining = _absent(root, rel)
    ok = remaining == [] and script.is_file()
    outcome = (f"{'restored' if ok else 'restore incomplete:'} {len(written)} absent tracked file(s) under "
               f"{root / rel} from HEAD object store{note}")
    if remaining:
        outcome += f"; still absent: {len(remaining)}"
    logger.error("shell hook self-heal: %s", outcome)
    return ok, outcome


def _may_be_refusal(r: Dict[str, Any]) -> bool:
    """Cheap gate on WHEN absence is checked after a run. It never decides the classification."""
    rc = r.get("returncode")
    return bool(r.get("error") or r.get("timed_out") or (rc not in (0, None))
                or "block" in (r.get("stdout") or ""))


def _infra_result(script: Path, outcome: str) -> Dict[str, Any]:
    return {"returncode": None, "stdout": "", "stderr": "", "timed_out": False, "elapsed_seconds": 0.0,
            "error": "hook files missing", "error_detail": outcome,
            "infra_failure": str(script), "restore_outcome": outcome}


def spawn_with_self_heal(spec: Any, stdin_json: str, spawn_once: Callable[[Any, str], Dict[str, Any]],
                         script: Optional[Path]) -> Dict[str, Any]:
    """Run the hook, repairing an absent script or tracked sibling first (or once after a refusal)."""
    if script is None:
        return spawn_once(spec, stdin_json)
    restore = spec.missing_hook_policy != "fail_closed"
    outcome: Optional[str] = None
    if not script.is_file():
        if not restore:
            return _infra_result(script, "restore disabled by hooks.missing_hook_policy=fail_closed")
        ok, outcome = restore_absent_files(script)
        if not ok:
            return _infra_result(script, outcome)
        r = spawn_once(spec, stdin_json)
    else:
        r = spawn_once(spec, stdin_json)
        if _may_be_refusal(r) and absent_tracked_files(script):
            if not restore:
                return _infra_result(script, "tracked hook files absent; restore disabled by hooks.missing_hook_policy=fail_closed")
            ok, outcome = restore_absent_files(script)
            if not ok:
                return _infra_result(script, outcome)
            r = spawn_once(spec, stdin_json)
    if outcome:
        r["restore_outcome"] = outcome
    return r


def _shared_root() -> Path:
    from agent import shell_hooks

    home = shell_hooks.get_hermes_home().expanduser().resolve()
    return home.parent.parent if home.parent.name == "profiles" else home


def page_missing_hook(path: str, outcome: str = "files missing") -> bool:
    """Deliver one #alerts page via the fleet notify front door. Returns delivery success."""
    notify = _shared_root() / "scripts" / "notify"
    if not notify.is_file():
        logger.error("missing shell hook %s: fleet notify front door %s unavailable (%s)", path, notify, outcome)
        return False
    try:
        proc = subprocess.run(
            [str(notify), "--severity", "high", "--source", "shell-hooks",
             "--body", f"shell hook {path}: {outcome} (infrastructure failure, not a policy verdict)"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("missing shell hook %s: page delivery failed: %s", path, exc)
        return False
    if proc.returncode != 0:
        logger.error("missing shell hook %s: page delivery failed (rc=%d): %s", path, proc.returncode, proc.stderr[:200])
    return proc.returncode == 0


def page_once(path: str, outcome: str) -> None:
    """Page at most once per hook per window, across processes (state file under the profile home)."""
    from agent import shell_hooks

    home = shell_hooks.get_hermes_home()
    key = (str(home), path)
    with _page_lock:
        now = time.time()
        state_dir = home / "state"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "missing-hook-pages.lock").open("a+") as lock:
                if shell_hooks.fcntl is not None:
                    shell_hooks.fcntl.flock(lock, shell_hooks.fcntl.LOCK_EX)
                state_file = state_dir / "missing-hook-pages.json"
                try:
                    stamps = json.loads(state_file.read_text(encoding="utf-8"))
                    if not isinstance(stamps, dict):
                        stamps = {}
                except (OSError, ValueError):
                    stamps = {}
                if now - float(stamps.get(path, 0)) >= PAGE_INTERVAL_SECONDS and shell_hooks._page_missing_hook(path, outcome):
                    stamps[path] = now
                    tmp = state_file.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(stamps), encoding="utf-8")
                    os.replace(tmp, state_file)
                    _pages[key] = now
        except OSError as exc:
            logger.error("missing shell hook %s: page dedup state unavailable: %s", path, exc)
            if now - _pages.get(key, 0) >= PAGE_INTERVAL_SECONDS and shell_hooks._page_missing_hook(path, outcome):
                _pages[key] = now


def missing_hook_verdict(spec: Any, r: Dict[str, Any], *, page: bool, display: str) -> Optional[Dict[str, Any]]:
    """Verdict for a hook whose files are absent after the repair attempt. Never a policy verdict."""
    path = str(r["infra_failure"])
    outcome = str(r.get("restore_outcome") or r.get("error_detail") or r.get("error") or "hook could not run")
    closed = spec.fail_closed and spec.missing_hook_policy != "fail_open_and_page"
    outcome = f"hook files missing: {outcome}"
    logger.error("shell hook infrastructure failure %s (%s); %s", path, outcome,
                 "failing closed" if closed else "failing open")
    if page:
        page_once(path, outcome)
    if not closed:
        return None
    owner = owning_checkout(Path(path))
    return {
        "action": "block",
        "message": (f"hook {display} infrastructure failure: hook missing and unrecoverable "
                    f"(owning checkout {owner or '<none>'}); this is not a policy verdict. "
                    "The hook never ran. Restore its files, do not disable the guard."),
        "error_class": "hook_infrastructure_failure",
    }
