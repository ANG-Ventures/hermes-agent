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


class _Dispatcher:
    """A warm ``shell_hook_dispatcher.py --serve`` process: one request in flight at a time (t_e920d269).

    Starting a dispatcher per tool call was a fork+exec of the multi-GB gateway on every call (~57% of
    the gateway's hook GIL samples under load). A warm one answers request after request; each hook is
    still a fresh fork of the same pristine dispatcher, so what a hook sees does not change. It is keyed
    on (interpreter, cwd, env): a call whose child env differs (a routed profile's secret scope) gets
    its own process, exactly the env a per-call start would have given it.
    """

    __slots__ = ("key", "proc", "errlog")

    def __init__(self, key: Tuple[Any, ...], proc: "subprocess.Popen", errlog: Any) -> None:
        self.key, self.proc, self.errlog = key, proc, errlog

    def close(self) -> None:
        """Stop the process (idle or killed) and log its stderr tail when it died badly."""
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                    self.proc.wait(timeout=2)
                except Exception:
                    kill_process_tree(self.proc)
                    self.proc.wait(timeout=5)
            rc = self.proc.returncode
            if rc not in (0, None, -9):
                self.errlog.seek(0)
                logger.warning("shell hook dispatcher exited %s: %s", rc,
                               self.errlog.read()[-400:].decode("utf-8", errors="replace"))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("shell hook dispatcher close: %s", type(exc).__name__)
        finally:
            for f in (self.proc.stdout, self.errlog):
                try:
                    f.close()
                except Exception:
                    pass


# Idle warm dispatchers, oldest first. One is in here only while no request is in flight on it.
_idle: List[_Dispatcher] = []
_idle_lock = threading.Lock()
# Idle processes kept in total: enough for the gateway's concurrent tool calls; a busier burst
# starts extra ones, and the oldest idle one is closed when the pool is over the cap (this also
# retires a process whose key (env/cwd) no call uses any more).
_MAX_IDLE = 4


def _idle_dispatchers() -> List[_Dispatcher]:
    with _idle_lock:
        return list(_idle)


def _take_dispatcher(python: str, env: Dict[str, str]) -> _Dispatcher:
    key = (python, os.getcwd(), tuple(sorted(env.items())))
    found = None
    stale: List[_Dispatcher] = []
    with _idle_lock:
        for d in reversed(list(_idle)):
            if d.proc.poll() is not None:
                _idle.remove(d)
                stale.append(d)  # died while idle: replaced below
            elif found is None and d.key == key:
                _idle.remove(d)
                found = d
    for d in stale:
        d.close()
    if found is not None:
        return found
    errlog = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen([python, DISPATCHER, "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errlog, process_group=0, env=env)
    except BaseException:
        errlog.close()
        raise
    return _Dispatcher(key, proc, errlog)


def _give_back(d: _Dispatcher) -> None:
    evicted: List[_Dispatcher] = []
    with _idle_lock:
        if d.proc.poll() is None:
            _idle.append(d)
        else:
            evicted.append(d)
        while len(_idle) > _MAX_IDLE:
            evicted.append(_idle.pop(0))
    for e in evicted:
        e.close()


def close_dispatchers() -> None:
    """Stop every idle warm dispatcher (tests; an in-flight one is closed when its request ends)."""
    with _idle_lock:
        ds = list(_idle)
        _idle.clear()
    for d in ds:
        d.close()


def _start_dispatcher(python: str, items: List[Tuple[Any, str, List[str]]], slots: Dict[int, _Slot],
                      stdin_json: str) -> None:
    """Send ``items`` to a warm dispatcher and start a daemon thread that fills ``slots`` as results stream
    in. A slot left empty when the run ends tells its callback to spawn the hook the old way."""
    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import build_subprocess_env
    request = (json.dumps({"hooks": [{"script": os.path.expanduser(script), "args": args, "timeout": spec.timeout}
                                     for spec, script, args in items], "stdin": stdin_json}) + "\n").encode("utf-8")
    budget = max(spec.timeout for spec, _, _ in items) + _DISPATCH_SLACK_S
    mine = [slots[id(spec)] for spec, _, _ in items]

    def release_all() -> None:
        for slot in mine:
            slot.done.set()

    try:
        d = _take_dispatcher(python, build_subprocess_env(scrub_secrets=is_multiplex_active()))
    except Exception as exc:
        logger.warning("shell hook dispatcher could not start (%s): %s; spawning hooks one by one",
                       python, type(exc).__name__)
        release_all()
        return
    proc = d.proc
    killed = threading.Event()

    def kill_on_budget() -> None:
        killed.set()
        kill_process_tree(proc)

    timer = threading.Timer(budget, kill_on_budget)
    timer.daemon = True

    def run() -> None:
        ended = False
        try:
            try:
                # The dispatcher reads the whole line before it writes anything, so one thread can
                # write the request and then read the answer without a pipe deadlock.
                proc.stdin.write(request)
                proc.stdin.flush()
            except OSError:
                pass
            for raw in iter(proc.stdout.readline, b""):
                try:
                    row = json.loads(raw)
                    if row.get("end") is True:
                        ended = True
                        break
                    spec = items[int(row["i"])][0]
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    continue
                slot = slots[id(spec)]
                if not row.get("missing"):
                    slot.result = _result(row)
                slot.done.set()
            if not ended:
                proc.wait()
                if killed.is_set():
                    # The run outlived every hook's own timeout: those still unanswered timed out.
                    for slot in mine:
                        if not slot.done.is_set():
                            slot.result = _timed_out(budget)
        except Exception as exc:  # pragma: no cover - defensive: never strand a waiting callback
            logger.warning("shell hook dispatcher reader failed: %s", type(exc).__name__)
            ended = False
        finally:
            timer.cancel()
            # Hand the process back BEFORE waking the callbacks, so the next tool call finds it idle.
            if ended and not killed.is_set():
                _give_back(d)
            else:
                d.close()
            release_all()

    timer.start()
    threading.Thread(target=run, name="hermes-hook-dispatch", daemon=True).start()


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
