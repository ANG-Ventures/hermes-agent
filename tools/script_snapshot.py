"""Run a SNAPSHOT of a shared-/tmp script, not the live path (card t_f4377203).

``/tmp`` is one namespace for every agent session on a host. On 2026-09-24 a sibling
session rewrote ``/tmp/act-1300.sh`` between Apollo's write and its run, and three stray
commands executed. A background process is the widest window: the command string is
fixed at spawn, but the file it names can change until the interpreter opens it (and a
shell script is re-read as it runs). ``terminal(background=true)`` of a single script
directly under ``/tmp`` therefore copies the script into the session's scratch dir and
runs the copy. A later overwrite of the source cannot change what runs. The sha256 is
logged and returned to the model.

Scope is deliberately narrow: only a command that is ONE invocation of a script
directly under ``/tmp`` (``bash|sh|zsh|python3 [flags] /tmp/x.sh [args]`` or the bare
path), without shell operators. Anything else is left alone. That includes repo
scripts, where ``$(dirname "$0")`` would change meaning.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import shutil
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_SCRIPT_EXTS = (".sh", ".bash", ".py")
_INTERPRETERS = re.compile(
    r"^(?:(?:/usr)?(?:/local)?(?:/opt/homebrew)?/bin/)?"
    r"(?:bash|sh|zsh|dash|ksh|python(?:3(?:\.\d+)?)?)$")
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")


def _shared_tmp_script(token: str) -> Optional[str]:
    path = os.path.normpath(token)
    if path.startswith("/private/tmp/"):
        path = path[len("/private"):]
    if os.path.dirname(path) == "/tmp" and path.lower().endswith(_SCRIPT_EXTS):
        return path
    return None


def snapshot_tmp_script_command(command: str, session_id: str) -> Tuple[str, Optional[dict]]:
    """Return ``(command, info)``. When *command* runs a shared-/tmp script, the command is
    rewritten to run a snapshot in the session scratch dir and *info* carries
    ``{"source", "snapshot", "sha256"}``. Otherwise the command is unchanged and info is None.
    Any failure leaves the command unchanged (the pre_tool_call hook still guards it)."""
    if not command or "/tmp/" not in command or _SHELL_OPERATORS.search(command):
        return command, None
    try:
        argv = shlex.split(command)
    except ValueError:
        return command, None
    if not argv:
        return command, None
    idx = 0
    if _INTERPRETERS.match(argv[0]):
        idx = 1
        while idx < len(argv) and argv[idx].startswith("-"):
            idx += 1
    if idx >= len(argv):
        return command, None
    source = _shared_tmp_script(argv[idx])
    if source is None or not os.path.isfile(source):
        return command, None
    try:
        from hermes_constants import get_session_scratch_dir
        scratch = get_session_scratch_dir(session_id) if session_id else None
        if scratch is None:
            return command, None
        snap_dir = scratch / "snapshots"
        snap_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(source, "rb") as fh:
            data = fh.read()
        sha = hashlib.sha256(data).hexdigest()
        snapshot = snap_dir / f"{sha[:12]}-{os.path.basename(source)}"
        tmp = snapshot.with_name(snapshot.name + f".{os.getpid()}.part")
        with open(tmp, "wb") as fh:
            fh.write(data)
        shutil.copymode(source, tmp)
        os.chmod(tmp, (os.stat(tmp).st_mode & 0o700) | 0o600)
        os.replace(tmp, snapshot)
    except Exception as exc:  # never block the spawn on the snapshot
        logger.debug("script snapshot skipped for %s: %s", source, exc)
        return command, None
    argv[idx] = str(snapshot)
    logger.info("background script snapshot: %s -> %s sha256=%s", source, snapshot, sha)
    return shlex.join(argv), {"source": source, "snapshot": str(snapshot), "sha256": sha}
