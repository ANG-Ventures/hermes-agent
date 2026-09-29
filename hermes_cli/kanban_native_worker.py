"""Native foreign-lane worker: run a profile's lane command with no LLM shim.

A profile whose ``config.yaml`` sets ``foreign_lane.worker_command`` is worked
by this module instead of ``hermes -p <profile> chat -q ...``. The dispatcher
spawns ``python -m hermes_cli.kanban_native_worker -m <model> ...`` with the
same worker env it gives a shim (owner grant, run id, workspace, board pins).
This process then:

1. claims the dispatcher's owner grant for its own pid, so it is the one
   process allowed to write the card (the lane it starts inherits a pid that
   is not its own and is refused, exactly like a shim's terminal child);
2. builds the lane argv from ``worker_command`` (a list of strings; the
   placeholders ``{task_id}``, ``{workspace}`` and ``{tests_cmd}`` are
   substituted per argument), taking the test command from the card body's
   single ``Test command: `...``` line;
3. runs the lane, heartbeating the card while it works;
4. reads the receipt (the lane's last stdout line) and makes its ``handback``
   board call, unchanged, through the same tool handler a shim would call;
5. blocks the card itself when the lane exits without a usable receipt
   ("exited without reporting"), so a native run always ends in one board
   write.

The ``-m/--provider/--reasoning`` flags are not used here. They carry the
route the dispatcher resolved at spawn so the lane's model gate (which reads
its parent's argv, as it reads a shim's) can compare it with the card.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

HANDBACK_TOOLS = ("kanban_request_review", "kanban_block")
HEARTBEAT_SECONDS = 120
TERM_GRACE_SECONDS = 30
READER_DRAIN_SECONDS = 10
_TEST_CMD_RE = re.compile(r"^[ \t>*-]*Test command:\s*`([^`]+)`", re.I | re.M)


def _profile_config(profile_home: Optional[str]) -> dict:
    if not profile_home:
        return {}
    path = Path(profile_home) / "config.yaml"
    if not path.is_file():
        return {}
    try:
        from hermes_cli.config import read_user_config_raw

        cfg = read_user_config_raw(path)
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def worker_command(profile_home: Optional[str]) -> Any:
    """The raw ``foreign_lane.worker_command`` value, or None when unset.

    The dispatcher's switch: any non-empty value selects the native spawn. A
    malformed value still goes native so the card blocks with the reason,
    rather than silently running the LLM shim the operator switched off.
    """
    lane = _profile_config(profile_home).get("foreign_lane")
    if not isinstance(lane, dict):
        return None
    value = lane.get("worker_command")
    return value or None


def tests_command(body: str) -> tuple[Optional[str], str]:
    """The card's one named test command, or (None, why)."""
    found = list(dict.fromkeys(m.strip() for m in _TEST_CMD_RE.findall(body or "")))
    if not found:
        return None, "card names no test command (expected one line `Test command: `<cmd>``)"
    if len(found) > 1:
        return None, f"card names {len(found)} different test commands: {found}"
    return found[0], ""


def build_argv(template: Any, *, task_id: str, workspace: str, tests_cmd: Optional[str]) -> list[str]:
    if not isinstance(template, list) or not template or not all(isinstance(a, str) for a in template):
        raise ValueError("foreign_lane.worker_command must be a non-empty list of strings")
    values = {"{task_id}": task_id, "{workspace}": workspace, "{tests_cmd}": tests_cmd or ""}
    argv = []
    for arg in template:
        for key, val in values.items():
            arg = arg.replace(key, val)
        argv.append(os.path.expanduser(arg))
    return argv


def parse_receipt(stdout_text: str) -> Optional[dict]:
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _tool_call(tool: str, args: dict) -> tuple[bool, str]:
    from tools import kanban_tools as kt

    handler = {
        "kanban_request_review": kt._handle_request_review,
        "kanban_block": kt._handle_block,
        "kanban_heartbeat": kt._handle_heartbeat,
    }[tool]
    raw = handler(dict(args))
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return False, str(raw)
    if isinstance(parsed, dict) and parsed.get("error"):
        return False, str(parsed["error"])
    return True, str(raw)


def _block(reason: str) -> bool:
    ok, detail = _tool_call("kanban_block", {"reason": reason[:1000]})
    _log(f"board kanban_block ok={ok}: {detail}")
    return ok


def _log(msg: str) -> None:
    print(f"[native-worker] {msg}", flush=True)


def handback(receipt: Optional[dict], rc: Optional[int], stderr_tail: str) -> bool:
    """Make the ONE board call for this run. Returns True when the board took it."""
    hb = (receipt or {}).get("handback") if isinstance(receipt, dict) else None
    if not isinstance(hb, dict):
        why = stderr_tail.strip().splitlines()[-1] if stderr_tail.strip() else "no output"
        return _block(f"native worker: lane exited rc={rc} without reporting (no receipt handback): {why}")
    tool, args = hb.get("tool"), hb.get("args")
    if tool not in HANDBACK_TOOLS or not isinstance(args, dict):
        return _block(f"native worker: receipt handback not allowed (tool={tool!r}); "
                      f"only {', '.join(HANDBACK_TOOLS)} are")
    args = {k: v for k, v in args.items() if k != "task_id"}  # always this worker's own card
    ok, detail = _tool_call(tool, args)
    _log(f"board {tool} ok={ok}: {detail}")
    if ok:
        return True
    if tool == "kanban_block":
        return False
    return _block(f"native worker: board refused {tool}: {detail}")


class _Heartbeat(threading.Thread):
    def __init__(self, started: float):
        super().__init__(daemon=True)
        self.started = started
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                _tool_call("kanban_heartbeat", {"note": f"native lane running {int(time.time() - self.started)}s"})
            except Exception as exc:  # a missed heartbeat must not kill the run
                _log(f"heartbeat failed: {exc}")


def run() -> int:
    from agent.delegation_context import claim_kanban_worker_authority

    claim_kanban_worker_authority()
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE") or ""
    if not task_id:
        _log("setup: HERMES_KANBAN_TASK is not set; this module is spawned by the kanban dispatcher only")
        return 2

    template = worker_command(os.environ.get("HERMES_HOME"))
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        _log(f"setup: task {task_id} not found")
        return 2

    tests_cmd = None
    if isinstance(template, list) and any("{tests_cmd}" in str(a) for a in template):
        tests_cmd, why = tests_command(task.body or "")
        if tests_cmd is None:
            return 0 if _block(f"native worker: {why}") else 1
    try:
        argv = build_argv(template, task_id=task_id, workspace=workspace, tests_cmd=tests_cmd)
    except ValueError as exc:
        return 0 if _block(f"native worker: setup: {exc}") else 1

    env = dict(os.environ)
    # The dispatcher pins this process's import tree with PYTHONPATH; the lane
    # is a foreign program and gets the env a shim's terminal would give it.
    env.pop("PYTHONPATH", None)
    env["KFL_SHIM_PID"] = str(os.getpid())  # the lane's watchdog anchor (run_lane find_shim)

    _log(f"lane argv: {shlex.join(argv)}")
    started = time.time()
    err_file = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(argv, cwd=workspace or None, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err_file, env=env, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        return 0 if _block(f"native worker: lane could not start: {exc}") else 1

    def _forward(signum, _frame):
        _log(f"signal {signum}: stopping lane pid {proc.pid}")
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(TERM_GRACE_SECONDS)
        except Exception:
            proc.kill()
        # No board write: the dispatcher's timeout/reclaim path owns this card.
        os._exit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):  # SIGHUP is POSIX-only
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        signal.signal(sig, _forward)
    beat = _Heartbeat(started)
    beat.start()
    out_lines: list[str] = []

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_lines.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    rc = proc.wait()
    # A grandchild the lane left behind (tmux server, a harness tool) may still
    # hold the pipe; the receipt was written before the lane exited.
    reader.join(READER_DRAIN_SECONDS)
    beat.stop.set()
    err_file.seek(0)
    stderr_text = err_file.read().decode("utf-8", "replace")
    if stderr_text:
        sys.stdout.write(stderr_text)
    _log(f"lane exited rc={rc} after {time.time() - started:.1f}s")
    return 0 if handback(parse_receipt("".join(out_lines)), rc, stderr_text) else 1


def main() -> int:
    code = 1
    try:
        code = run()
        return code
    finally:
        try:
            from hermes_cli.kanban_worker_exit import write_exit_status

            write_exit_status(code)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
