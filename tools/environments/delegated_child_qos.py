"""Darwin background QoS for every shell a delegate_task child spawns (t_ba9c58b0).

A delegated child's terminal commands start under ``/usr/sbin/taskpolicy -b``: darwin
background policy (priority 4, efficiency cores only, throttled I/O). The policy is
inherited by every descendant, so ``nohup``, ``sandbox-exec``, ``env -i``, an absolute
interpreter path or a script that runs the load itself all stay under it; only an
explicit ``taskpolicy -B`` on the process lifts it. On a Mac Studio (24 P + 8 E cores)
that caps a child's whole tree at the 8 E-cores, under load-fence's kill line
(more than ncpu/2 descendants each over 60% CPU).

Applied once per command at the two local spawn points: ``LocalEnvironment._run_bash``
(foreground commands and the snapshot bootstrap) and ``ProcessRegistry._scope_argv``
(``terminal(background=True)``, pipe and PTY). taskpolicy execs in place, so the pid,
process group and tree-kill paths are unchanged.

Scope: only a delegated child (``is_delegated_child_process_context``). The operator's own
session and a dispatched Kanban worker's own commands are untouched. No-op off macOS and
when ``/usr/sbin/taskpolicy`` is missing.

Off switch: ``<default HERMES root>/state/spawn-guard/delegated-background.disabled``,
read on every command (create it to stop, remove it to re-arm).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TASKPOLICY = "/usr/sbin/taskpolicy"
OFF_SWITCH_NAME = "delegated-background.disabled"
_IS_DARWIN = sys.platform == "darwin"


def off_switch_path() -> Path:
    """The kill-switch file, next to spawn_guard's ``delegated-tests.disabled``."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "state" / "spawn-guard" / OFF_SWITCH_NAME


def background_prefix() -> list[str]:
    """argv prefix that starts a delegated child's shell under darwin background QoS, or ``[]``."""
    if not _IS_DARWIN:
        return []
    try:
        from agent.delegation_context import is_delegated_child_process_context

        if not is_delegated_child_process_context():
            return []
    except Exception:
        return []
    try:
        if off_switch_path().exists():
            return []
    except Exception:
        pass  # an unresolvable root cannot hold the switch; keep the limit on
    if not os.access(TASKPOLICY, os.X_OK):
        return []
    return [TASKPOLICY, "-b"]
