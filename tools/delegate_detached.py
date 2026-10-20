#!/usr/bin/env python3
"""Detached background delegation: run a durable background unit in its own process.

Spec: ``docs/dev/delegate-detached-background.md``. A background ``delegate_task`` unit that has a
restart-durable record runs its children in ONE child process (``python -m tools.delegate_detached
--job <dir>``, own session) instead of on a gateway thread, so a gateway restart does not kill it. The
gateway thread supervises the pid; a restarted gateway adopts the child (``tools.async_delegation_store.
claim_detached_adoptions``) and delivers its ``result.json`` through the normal ``_finalize`` path.

Job dir (keyed by attempt): ``spawn.json`` (gateway, before Popen), ``pid.json`` (gateway, after Popen),
``spec.json`` (input), ``child.json`` (child's first act), ``heartbeat.json`` (child, every
``HEARTBEAT_SECONDS``), ``result.json`` (child, atomic), ``child.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1  # spec.json / heartbeat.json / result.json protocol between gateway and child
HEARTBEAT_SECONDS = 10.0
SIGKILL_GRACE_SECONDS = 30.0
POLL_SECONDS = 1.0
_RUNTIME_ROOT = Path(__file__).resolve().parent.parent


# ── job dir ─────────────────────────────────────────────────────────────────
def jobs_root(profile_home: Path) -> Path:
    return Path(profile_home) / "state" / "detached-delegations"


def job_dir_for(profile_home: Path, attempt_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(attempt_id))
    return jobs_root(profile_home) / safe


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_job_dir(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)


# ── process identity ────────────────────────────────────────────────────────
def process_create_time(pid: int) -> Optional[float]:
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def pid_alive(pid: Any, create_time: Any) -> bool:
    """Same pid AND same kernel start time (pid reuse safe); a zombie counts as dead."""
    try:
        import psutil

        proc = psutil.Process(int(pid))
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return create_time is None or abs(proc.create_time() - float(create_time)) < 0.01
    except Exception:
        return False


def _argv_has_job(cmdline: list, job_dir: str) -> bool:
    return any(a == "--job" and i + 1 < len(cmdline) and cmdline[i + 1] == job_dir
               for i, a in enumerate(cmdline))


def find_job_process(job_dir: Path) -> Optional[Dict[str, Any]]:
    """Process whose argv carries the exact element pair ``--job <job_dir>`` (crash window between
    ``spawn.json`` and ``pid.json``). Exact element equality on a unique path, never a substring."""
    try:
        import psutil
    except Exception:
        return None
    target = str(job_dir)
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            if _argv_has_job(proc.info.get("cmdline") or [], target):
                return {"pid": proc.info["pid"], "create_time": proc.info["create_time"]}
        except Exception:
            continue
    return None


def job_identity(job_dir: Path, detached_block: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """``{pid, create_time}`` for the job's child: registry block, then pid.json, child.json, argv scan."""
    for source in (detached_block, _read_json(job_dir / "pid.json"), _read_json(job_dir / "child.json")):
        if isinstance(source, dict) and source.get("pid"):
            return {"pid": int(source["pid"]), "create_time": source.get("create_time")}
    if (job_dir / "spawn.json").exists():
        return find_job_process(job_dir)
    return None


def probe_job(job_dir: Path, detached_block: Optional[Dict[str, Any]] = None) -> str:
    """``result`` (result.json present) | ``alive`` | ``dead``."""
    if (job_dir / "result.json").exists():
        return "result"
    ident = job_identity(job_dir, detached_block)
    if ident and pid_alive(ident["pid"], ident["create_time"]):
        return "alive"
    return "dead"


def signal_group(pid: Any, create_time: Any, sig: int) -> bool:
    """Signal the child's process group (it is its own session leader) iff it is still the same process."""
    if not pid_alive(pid, create_time):
        return False
    try:
        os.killpg(int(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), sig)
            return True
        except OSError:
            return False


def terminate_group(pid: Any, create_time: Any, *, grace: float = SIGKILL_GRACE_SECONDS,
                    timer_factory: Callable[..., Any] = threading.Timer) -> bool:
    """SIGTERM now, SIGKILL after ``grace`` on a timer thread; never blocks the caller."""
    sent = signal_group(pid, create_time, signal.SIGTERM)
    if sent:
        timer = timer_factory(grace, signal_group, args=(pid, create_time, signal.SIGKILL))
        timer.daemon = True
        timer.start()
    return sent


# ── liveness ────────────────────────────────────────────────────────────────
def heartbeat_progress_fn(job_dir_ref: Callable[[], Optional[Path]]) -> Callable[[], tuple]:
    """``progress_fn`` for the async stale monitor: the child's own activity token from heartbeat.json."""
    def _progress() -> tuple:
        job_dir = job_dir_ref()
        data = _read_json(job_dir / "heartbeat.json") if job_dir else None
        if not data or data.get("schema_version") != SCHEMA_VERSION:
            return None, False  # unknown protocol reads as no progress: the stall monitor ends it
        token = data.get("token")
        return (json.dumps(token, sort_keys=True, default=str) if token is not None else None,
                bool(data.get("in_tool")))
    return _progress


def _children_token() -> tuple:
    """Activity of every live subagent in THIS process (only this unit's tree lives here)."""
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock

    with _active_subagents_lock:
        agents = [(sid, rec.get("agent")) for sid, rec in _active_subagents.items()]
    parts, in_tool = [], False
    for sid, agent in sorted(agents, key=lambda item: item[0]):
        try:
            summary = agent.get_activity_summary()
            tool = summary.get("current_tool")
            parts.append([sid, summary.get("api_call_count", 0), tool, summary.get("last_activity_ts")])
            in_tool = in_tool or bool(tool)
        except Exception:
            parts.append([sid, None, None, None])
    return parts, in_tool


class _Heartbeat:
    def __init__(self, job_dir: Path):
        self.job_dir = job_dir
        self.phase: Optional[list] = ["bootstrap", "start"]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="detached-heartbeat", daemon=True)

    def set_phase(self, step: Optional[str]) -> None:
        self.phase = ["bootstrap", step] if step else None
        self.beat()

    def beat(self) -> None:
        if self.phase is not None:
            token, in_tool = self.phase, False
        else:
            token, in_tool = _children_token()
        try:
            _write_json(self.job_dir / "heartbeat.json", {"schema_version": SCHEMA_VERSION, "token": token,
                                                         "in_tool": in_tool, "ts": time.time()})
        except OSError:
            logger.debug("detached heartbeat write failed", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.beat()

    def start(self) -> None:
        self.beat()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ── reconstruction (shared with gateway restart recovery) ───────────────────
def build_parent_and_runner(
    record: Dict[str, Any], continuation: Optional[str], delegation_cfg: Dict[str, Any], *,
    resolve_default: Callable[[], dict], resolve_for_provider: Callable[[str], dict],
    source: str = "gateway_recovery",
):
    """Rebuild the parent AIAgent from a durable record and return ``(runner, interrupt_fn, parent)``.

    The one reconstruction used by gateway restart recovery and by the detached child process.
    Credentials are re-resolved from the profile's own config/secrets, never read from the record."""
    from run_agent import AIAgent
    from tools.delegate_tool import _resolve_delegation_credentials, build_recovered_delegation_runner

    execution = record.get("execution") or {}
    route = record.get("route") or {}
    credential_ref = execution.get("credential_ref") or {}
    if credential_ref.get("source") == "delegation_config":
        from types import SimpleNamespace

        parent_provider = credential_ref.get("parent_provider")
        current_direct_key = str(delegation_cfg.get("api_key") or "").strip() or None
        if current_direct_key:
            parent_runtime = {}
        elif parent_provider:
            parent_runtime = resolve_for_provider(str(parent_provider))
        else:
            parent_runtime = resolve_default()
        parent_runtime = {k: v for k, v in parent_runtime.items() if k != "_fallback_notice"}
        direct = _resolve_delegation_credentials(
            {
                "model": execution.get("model"),
                "base_url": execution.get("base_url"),
                "api_mode": execution.get("api_mode"),
                # Read the current secret from config; never a credential from the record.
                "api_key": current_direct_key,
            },
            SimpleNamespace(**parent_runtime),
        )
        runtime = {
            "api_key": direct.get("api_key") or parent_runtime.get("api_key"),
            "base_url": direct.get("base_url"),
            "provider": direct.get("provider"),
            "api_mode": direct.get("api_mode"),
            "command": direct.get("command"),
            "args": list(direct.get("args") or []),
            "credential_pool": None,
        }
    else:
        requested_provider = credential_ref.get("provider") or execution.get("provider")
        runtime = resolve_for_provider(str(requested_provider)) if requested_provider else resolve_default()
    runtime = {k: v for k, v in runtime.items() if k != "_fallback_notice"}
    agent_kwargs = {
        **runtime,
        "model": execution.get("model") or "",
        "max_iterations": int(execution.get("max_iterations") or 45),
        "provider_require_parameters": bool(execution.get("provider_require_parameters", False)),
        "skip_memory": True,
        "skip_context_files": True,
    }
    if execution.get("acp_command"):
        agent_kwargs["command"] = execution["acp_command"]
        agent_kwargs["args"] = list(execution.get("acp_args") or [])
    # Parent-level toolsets (the dispatching parent's) when recorded; older records carry child 0's.
    enabled_toolsets = (execution["parent_toolsets"] if execution.get("parent_toolsets") is not None
                        else execution.get("toolsets"))
    for key, value in {
        "enabled_toolsets": enabled_toolsets,
        "disabled_toolsets": execution.get("parent_disabled_toolsets"),
        "platform": route.get("platform"),
        "session_id": route.get("parent_session_id"),
        "reasoning_config": execution.get("reasoning_config"),
        "service_tier": execution.get("service_tier"),
        "fallback_model": execution.get("fallback_chain"),
        "providers_allowed": execution.get("providers_allowed"),
        "providers_ignored": execution.get("providers_ignored"),
        "providers_order": execution.get("providers_order"),
        "provider_sort": execution.get("provider_sort"),
        "provider_data_collection": execution.get("provider_data_collection"),
        "openrouter_min_coding_score": execution.get("openrouter_min_coding_score"),
    }.items():
        if value is not None:
            agent_kwargs[key] = value
    parent = AIAgent(**agent_kwargs)
    setattr(parent, "_delegate_depth", int(execution.get("parent_depth") or 0))
    setattr(parent, "terminal_cwd", execution.get("workspace_hint") or None)
    runner = build_recovered_delegation_runner(record, continuation, parent, source=source)

    def _interrupt_parent() -> None:
        parent.interrupt("Recovered async delegation cancelled")

    return runner, _interrupt_parent, parent


# ── gateway side: spawn + supervise ─────────────────────────────────────────
def spawn_mode() -> str:
    """``in_process`` / ``scoped`` (restart-safe) or ``degraded`` (systemd without a user bus: stay a thread)."""
    from tools.process_registry import restart_safe_gateway_child_argv

    try:
        return restart_safe_gateway_child_argv(
            [sys.executable, "-c", "pass"], unit_suffix="delegation-probe",
            require_restart_safe_scope=False, outlives_parent=True,
        ).mode
    except Exception:
        return "degraded"


class DetachedSpawn:
    """A spawned detached child (gateway side)."""

    def __init__(self, job_dir: Path, pid: int, create_time: Optional[float], popen: Optional[subprocess.Popen]):
        self.job_dir, self.pid, self.create_time, self.popen = job_dir, pid, create_time, popen

    def block(self, attempt_id: str) -> Dict[str, Any]:
        return {"pid": self.pid, "create_time": self.create_time, "job_dir": str(self.job_dir),
                "attempt_id": attempt_id, "spawned_at": time.time()}


def spawn_child(*, record: Dict[str, Any], attempt_id: str, profile_home: Path,
                continuation: Optional[str], live_delegation_id: Optional[str]) -> Optional[DetachedSpawn]:
    """Spawn the detached child for one attempt; None when the host cannot make it restart-safe."""
    from tools.environments.local import served_profile_child_env
    from tools.process_registry import restart_safe_gateway_child_argv

    job_dir = job_dir_for(profile_home, attempt_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(job_dir, 0o700)
    except OSError:
        pass
    command = [sys.executable, "-m", "tools.delegate_detached", "--job", str(job_dir)]
    dispatch = restart_safe_gateway_child_argv(
        command, unit_suffix=f"delegation-{job_dir.name}",
        require_restart_safe_scope=False, outlives_parent=True,
    )
    if dispatch.mode == "degraded":
        remove_job_dir(job_dir)
        return None
    spec = {
        "schema_version": SCHEMA_VERSION, "record": record, "attempt_id": attempt_id, "continuation": continuation,
        "live_delegation_id": live_delegation_id, "profile_home": str(profile_home),
    }
    _write_json(job_dir / "spec.json", spec)
    _write_json(job_dir / "spawn.json", {"attempt_id": attempt_id, "intent_at": time.time()})
    env = served_profile_child_env(target_home=str(profile_home), inherit_credentials=True)
    env["PYTHONPATH"] = str(_RUNTIME_ROOT)
    workspace = (record.get("execution") or {}).get("workspace_hint")
    log_f = open(job_dir / "child.log", "ab")
    try:
        popen = subprocess.Popen(  # noqa: S603 -- fixed argv
            dispatch.argv, cwd=workspace if workspace and os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    finally:
        log_f.close()
    spawned = DetachedSpawn(job_dir, popen.pid, process_create_time(popen.pid), popen)
    _write_json(job_dir / "pid.json", {"pid": spawned.pid, "create_time": spawned.create_time})
    logger.info("async_delegation_detached_spawned attempt_id=%s pid=%s job_dir=%s",
                attempt_id, spawned.pid, job_dir)
    return spawned


class HandedOff(Exception):
    """The unit was handed to the next gateway boot; this supervisor must not finalize it."""


def wait_for_result(job_dir: Path, pid: int, create_time: Optional[float],
                    popen: Optional[subprocess.Popen] = None, *, poll: float = POLL_SECONDS,
                    abort: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Block until the child exits, then return its result (or an error result with the log tail)."""
    while True:
        if abort is not None and abort():
            raise HandedOff(str(job_dir))
        if popen is not None:
            if popen.poll() is not None:
                break
        elif not pid_alive(pid, create_time):
            break
        time.sleep(poll)
    result = _read_json(job_dir / "result.json")
    if result is not None:
        if result.pop("schema_version", None) != SCHEMA_VERSION:
            return {"status": "error", "summary": None,
                    "error": "Detached delegation child wrote an unknown result protocol version "
                             "(gateway upgraded mid-run?); its output is in " + str(job_dir / "child.log")}
        return result
    try:
        tail = (job_dir / "child.log").read_bytes()[-2048:].decode("utf-8", "replace")
    except OSError:
        tail = ""
    code = popen.returncode if popen is not None else None
    return {"status": "error", "summary": None,
            "error": f"Detached delegation child exited (code={code}) without a result. Log tail:\n{tail}"}


# ── child process entry ─────────────────────────────────────────────────────
def _child_main(job_dir: Path) -> int:
    _write_json(job_dir / "child.json", {"pid": os.getpid(), "create_time": process_create_time(os.getpid())})
    heartbeat = _Heartbeat(job_dir)
    heartbeat.start()
    spec = _read_json(job_dir / "spec.json") or {}
    if spec.get("schema_version") != SCHEMA_VERSION:
        _write_json(job_dir / "result.json", {
            "schema_version": SCHEMA_VERSION, "status": "error", "summary": None,
            "error": f"unknown detached spec version {spec.get('schema_version')!r}"})
        return 1
    record = spec.get("record") or {}
    result: Dict[str, Any]
    parent_box: Dict[str, Any] = {}
    cancelled = threading.Event()

    def _on_term(_signum, _frame) -> None:
        cancelled.set()
        parent = parent_box.get("parent")
        if parent is not None:
            try:
                parent.interrupt("Detached delegation cancelled")
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _on_term)
    try:
        from hermes_logging import setup_logging

        heartbeat.set_phase("logging")
        try:
            setup_logging()
        except Exception:
            pass
        heartbeat.set_phase("mcp")
        try:
            from hermes_cli.mcp_startup import _discover_mcp_tools_without_interactive_oauth

            _discover_mcp_tools_without_interactive_oauth()
        except Exception:
            logger.debug("detached child MCP discovery failed", exc_info=True)
        heartbeat.set_phase("agent")
        from hermes_cli.config import load_config
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_runtime_agent_kwargs_for_provider

        record = {**record, "live_delegation_id": spec.get("live_delegation_id")}
        continuation = spec.get("continuation")
        runner, _interrupt, parent = build_parent_and_runner(
            record, continuation, (load_config() or {}).get("delegation") or {},
            resolve_default=_resolve_runtime_agent_kwargs,
            resolve_for_provider=_resolve_runtime_agent_kwargs_for_provider,
            source="gateway_recovery" if continuation else "gateway_detached",
        )
        parent_box["parent"] = parent
        if cancelled.is_set():
            parent.interrupt("Detached delegation cancelled")
        heartbeat.set_phase(None)
        result = runner()
    except BaseException as exc:  # noqa: BLE001 -- the result file is the only channel back
        logger.exception("detached delegation child crashed")
        result = {"status": "error", "summary": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        heartbeat.stop()
    if not isinstance(result, dict):
        result = {"status": "error", "summary": None, "error": "runner returned a non-object result"}
    _write_json(job_dir / "result.json", {**result, "schema_version": SCHEMA_VERSION})
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.delegate_detached")
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    return _child_main(Path(args.job))


if __name__ == "__main__":
    sys.exit(main())
