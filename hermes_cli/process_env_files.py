"""Apply ``agent.process_env_files`` to an agent PROCESS's own environment.

``terminal.shell_init_files`` only reaches the terminal tool's shells. Everything
else an agent process spawns -- a gateway's in-process ``gh``/``git`` calls, the
kanban dispatcher's workers, execute_code sandboxes -- inherits the process env
as it was at exec time. ``agent.process_env_files`` lists POSIX-sh files that are
sourced ONCE at process start (``hermes`` main) and whose resulting exports are
merged into ``os.environ``, so every child inherits them.

Contract:
  * Files are sourced by ``/bin/sh`` in order, with the process's current env
    (profile home and agent marker already set). Missing files are skipped.
  * Only the DIFF is applied: keys the files add, change or unset. Shell
    bookkeeping (PWD, SHLVL, ``_`` ...) is ignored.
  * Fail-open: a timeout, non-zero exit or unreadable output leaves the env
    untouched and logs a warning. Process start never fails because of it.
  * The applied diff is remembered so a spawn site that must NOT carry it
    (cron script children are plain scripts, not agent processes) can undo it
    with :func:`strip_overlay`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Dict, List, MutableMapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Keys the sourcing shell / dump interpreter set on their own; never a file's intent.
_SHELL_NOISE = frozenset({"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"})
_TIMEOUT_SECONDS = 20.0

# key -> (value before apply or None if unset, value after apply or None if unset)
_OVERLAY: Dict[str, Tuple[Optional[str], Optional[str]]] = {}


def configured_files(cfg: Optional[dict]) -> List[str]:
    """Expanded, existing paths from ``agent.process_env_files`` (order kept)."""
    agent_cfg = (cfg or {}).get("agent") or {}
    raw = agent_cfg.get("process_env_files") if isinstance(agent_cfg, dict) else None
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if not item:
            continue
        try:
            path = os.path.expandvars(os.path.expanduser(str(item)))
        except Exception:
            continue
        if os.path.isfile(path):
            out.append(path)
    return out


def compute_overlay(
    files: List[str], env: Optional[MutableMapping[str, str]] = None
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Source ``files`` in a child ``sh`` and return the env diff they produce."""
    base = dict(os.environ if env is None else env)
    if not files:
        return {}
    dump = "import json,os,sys;sys.stdout.write(json.dumps(dict(os.environ)))"
    script = 'for __pef in "$@"; do . "$__pef" >/dev/null 2>&1; done; unset __pef; exec "$PEF_PY" -c "$PEF_DUMP"'
    child_env = dict(base)
    child_env["PEF_PY"] = sys.executable
    child_env["PEF_DUMP"] = dump
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", script, "process-env-files", *files],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("agent.process_env_files: sourcing failed (%s); env unchanged", exc)
        return {}
    if proc.returncode != 0:
        logger.warning(
            "agent.process_env_files: sourcing exited %s; env unchanged", proc.returncode
        )
        return {}
    try:
        after = json.loads(proc.stdout)
        if not isinstance(after, dict):
            raise ValueError("not a mapping")
    except Exception as exc:
        logger.warning("agent.process_env_files: unreadable env dump (%s); env unchanged", exc)
        return {}
    after.pop("PEF_PY", None)
    after.pop("PEF_DUMP", None)
    diff: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for key in set(base) | set(after):
        if key in _SHELL_NOISE:
            continue
        old, new = base.get(key), after.get(key)
        if old != new:
            diff[key] = (old, None if new is None else str(new))
    return diff


def apply_process_env_files(
    cfg: Optional[dict], env: Optional[MutableMapping[str, str]] = None
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Source the configured files and merge their exports into ``env``
    (default ``os.environ``). Returns the applied diff; ``{}`` when nothing is
    configured or sourcing failed."""
    target = os.environ if env is None else env
    diff = compute_overlay(configured_files(cfg), target)
    for key, (_old, new) in diff.items():
        if new is None:
            target.pop(key, None)
        else:
            target[key] = new
    if env is None and diff:
        _OVERLAY.clear()
        _OVERLAY.update(diff)
        logger.info("agent.process_env_files applied: %s", ",".join(sorted(diff)))
    return diff


def strip_overlay(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Undo this process's applied overlay in a child env (in place).

    A key is restored only while it still holds the value the overlay set, so a
    later deliberate change to that key is never reverted."""
    for key, (old, new) in _OVERLAY.items():
        if key == "PATH" and new is not None and env.get("PATH"):
            # PATH is routinely re-edited after start (venv/tool dirs), so match
            # components rather than the whole value: drop only what we added.
            before = set((old or "").split(os.pathsep))
            added = {p for p in new.split(os.pathsep) if p and p not in before}
            env["PATH"] = os.pathsep.join(
                p for p in env["PATH"].split(os.pathsep) if p not in added
            )
            continue
        if env.get(key) != new:
            continue
        if old is None:
            env.pop(key, None)
        else:
            env[key] = old
    return env
