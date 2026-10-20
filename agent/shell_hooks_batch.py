"""One interpreter start per tool call for Python shell hooks (t_f010356f).

A tool call fires every matching ``pre_tool_call`` / ``post_tool_call`` shell hook, and each one used
to be its own ``<python> <hook>.py`` process: on a loaded Mac the interpreter start alone measured
1.2-2.2 s, so ~20 hooks cost seconds of wall time per call. Here the FIRST shell-hook callback of an
``invoke_hook`` call starts ``agent/shell_hook_dispatcher.py`` once per interpreter for every eligible
hook that call will fire (it forks one child per hook, all at once). Each callback then waits for its
OWN hook's result only, as it streams back, so a hook's verdict arrives no later than it did when the
hook was spawned on its own turn.

The verdict path does not change: each result has the shape ``_spawn_once`` returns and goes through
the same self-heal wrapper and ``_evaluate_result``. Whatever the dispatcher cannot answer for (script
absent, dispatcher crash, bad output) is spawned the old way, so the interpreter's own error text and
the missing-hook self-heal stay as they were.

Eligible: ``<python...> <script>.py [args]`` on POSIX with no interpreter flags, fired inside an
``invoke_hook`` call. Kill switch: ``hooks.batch_python_hooks: false`` (one process per hook).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli._subprocess_compat import IS_WINDOWS, kill_process_tree, split_command_line

logger = logging.getLogger("agent.shell_hooks")

DISPATCHER = str(Path(__file__).with_name("shell_hook_dispatcher.py"))
# Seconds the whole dispatcher run gets past the slowest hook's own timeout before it is killed.
_DISPATCH_SLACK_S = 5.0
_lock = threading.Lock()


def batch_argv(spec: Any) -> Optional[Tuple[str, str, List[str]]]:
    """``(interpreter, script, args)`` when the hook can run in the dispatcher, else ``None``."""
    if IS_WINDOWS or not getattr(spec, "batch", False):
        return None
    try:
        argv = split_command_line(os.path.expanduser(spec.command))
    except ValueError:
        return None
    if len(argv) < 2 or not os.path.basename(argv[0]).lower().startswith("python"):
        return None
    if argv[1].startswith("-") or not argv[1].lower().endswith(".py"):
        return None
    return argv[0], argv[1], argv[2:]


class _Slot:
    __slots__ = ("done", "result", "taken")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[Dict[str, Any]] = None
        self.taken = False


def _result(row: Dict[str, Any]) -> Dict[str, Any]:
    def decode(b: Any) -> str:
        return base64.b64decode(b or "").decode("utf-8", errors="replace")
    return {"returncode": row.get("rc"), "stdout": decode(row.get("out")), "stderr": decode(row.get("err")),
            "timed_out": bool(row.get("timed_out")), "elapsed_seconds": float(row.get("elapsed") or 0.0),
            "error": None, "error_detail": None, "infra_failure": None}


def _timed_out(budget: float) -> Dict[str, Any]:
    return {"returncode": None, "stdout": "", "stderr": "", "timed_out": True, "elapsed_seconds": budget,
            "error": None, "error_detail": None, "infra_failure": None}


def _start_dispatcher(python: str, items: List[Tuple[Any, str, List[str]]], slots: Dict[int, _Slot],
                      stdin_json: str) -> None:
    """Start one dispatcher for ``items`` and a daemon thread that fills ``slots`` as results stream in.
    A slot left empty when the run ends tells its callback to spawn the hook the old way."""
    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import build_subprocess_env
    request = json.dumps({"hooks": [{"script": os.path.expanduser(script), "args": args, "timeout": spec.timeout}
                                    for spec, script, args in items], "stdin": stdin_json}).encode("utf-8")
    budget = max(spec.timeout for spec, _, _ in items) + _DISPATCH_SLACK_S
    mine = [slots[id(spec)] for spec, _, _ in items]

    def release_all() -> None:
        for slot in mine:
            slot.done.set()

    try:
        errlog = tempfile.TemporaryFile()
        proc = subprocess.Popen([python, DISPATCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                process_group=0, env=build_subprocess_env(scrub_secrets=is_multiplex_active()))
    except Exception as exc:
        logger.warning("shell hook dispatcher could not start (%s): %s; spawning hooks one by one",
                       python, type(exc).__name__)
        release_all()
        return

    killed = threading.Event()

    def kill_on_budget() -> None:
        killed.set()
        kill_process_tree(proc)

    timer = threading.Timer(budget, kill_on_budget)
    timer.daemon = True

    def feed() -> None:
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except OSError:
            pass

    def read() -> None:
        try:
            for raw in proc.stdout:
                try:
                    row = json.loads(raw)
                    spec = items[int(row["i"])][0]
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                slot = slots[id(spec)]
                if not row.get("missing"):
                    slot.result = _result(row)
                slot.done.set()
            proc.wait()
            if killed.is_set():
                # The run outlived every hook's own timeout: those still unanswered timed out.
                for slot in mine:
                    if not slot.done.is_set():
                        slot.result = _timed_out(budget)
            elif proc.returncode != 0:
                errlog.seek(0)
                logger.warning("shell hook dispatcher exited %s: %s", proc.returncode,
                               errlog.read()[-400:].decode("utf-8", errors="replace"))
        except Exception as exc:  # pragma: no cover - defensive: never strand a waiting callback
            logger.warning("shell hook dispatcher reader failed: %s", type(exc).__name__)
        finally:
            timer.cancel()
            errlog.close()
            release_all()

    timer.start()
    threading.Thread(target=feed, name="hermes-hook-dispatch-in", daemon=True).start()
    threading.Thread(target=read, name="hermes-hook-dispatch-out", daemon=True).start()


def _start_batch(specs: List[Any], stdin_json: str) -> Dict[int, _Slot]:
    slots = {id(spec): _Slot() for spec in specs}
    by_python: Dict[str, List[Tuple[Any, str, List[str]]]] = {}
    for spec in specs:
        python, script, args = batch_argv(spec)  # type: ignore[misc]  # specs are pre-filtered
        by_python.setdefault(python, []).append((spec, script, args))
    for python, items in by_python.items():
        _start_dispatcher(python, items, slots, stdin_json)
    return slots


def batched_result(spec: Any, stdin_json: str, tool_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """This hook's ``_spawn_once``-shaped result from the call's dispatcher run, or ``None`` when the
    caller must spawn it the old way (not eligible, not inside ``invoke_hook``, no result)."""
    if batch_argv(spec) is None:
        return None
    from hermes_cli.plugins_dispatch import current_hook_invocation
    invocation = current_hook_invocation.get()
    if invocation is None:
        return None
    key = ("shell_hooks_batch", spec.event, stdin_json)
    with _lock:
        slots = invocation.shared.get(key)
        if slots is None or id(spec) not in slots or slots[id(spec)].taken:
            group = []
            for cb in list(invocation.callbacks):
                other = getattr(cb, "_hermes_shell_spec", None)
                if (other is not None and other.event == spec.event and batch_argv(other) is not None
                        and (other.event not in ("pre_tool_call", "post_tool_call") or other.matches_tool(tool_name))):
                    group.append(other)
            if spec not in group:
                group.append(spec)
            slots = _start_batch(group, stdin_json)
            invocation.shared[key] = slots
        slot = slots[id(spec)]
        slot.taken = True
    slot.done.wait()
    return slot.result
