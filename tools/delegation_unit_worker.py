"""The runner of one background ``delegate_task`` unit on the kanban runtime (option C, §5.3).

Entry: ``hermes -p <profile> delegation-unit`` (a hidden CLI subcommand), spawned by the
dispatcher's ``spawn_now`` for a card on the internal ``delegations`` board
(docs/dev/delegate-background-on-kanban-runtime.md). The CLI bootstrap has already applied the
profile override, the profile ``.env`` and ``agent.process_env_files``; the dispatcher's env gives
this process ``HERMES_KANBAN_TASK/RUN_ID/BOARD`` and a pending owner grant.

1. claim the owner grant; 2. read the card body, then the record, ONCE (not running on the card's
attempt -> complete ``superseded``, exit 0, no model call); 3. rebuild the parent ``AIAgent`` with the
CLI's resolver; 4. run the unit through the synchronous delegation path; 5. a heartbeat thread
(60 s beat, 5 s card read: ``archived`` = cancelled -> hard-interrupt, write nothing, exit 0);
6. ``append_terminal(queued_boot_id=None)`` + legacy mirror, then complete the card; 7. any
exception after step 2 -> ``fail_attempt`` with the traceback tail. Never exits with a requeue code.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("tools.delegation_unit_worker")

HEARTBEAT_SECONDS = 60.0
CARD_POLL_SECONDS = 5.0


def _log(msg: str) -> None:
    print(f"[delegation-unit] {msg}", flush=True)


def _kb():
    from hermes_cli import kanban_db as kb

    return kb


def _board() -> str:
    return os.environ.get("HERMES_KANBAN_BOARD") or "delegations"


def _run_id() -> Optional[int]:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    return int(raw) if raw.isdigit() else None


def _card_status(task_id: str) -> Optional[str]:
    kb = _kb()
    with kb.connect_closing(board=_board()) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row is not None else None


def complete_card(task_id: str, summary: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Close this run's card. A failed write leaves the card to the crash sweep (K2 parks it
    ``blocked``), which the gateway drains on too (§5.3.6)."""
    kb = _kb()
    try:
        with kb.connect_closing(board=_board()) as conn:
            return bool(kb.complete_task(conn, task_id, summary=summary,
                                         metadata=dict(metadata or {}, runtime="kanban"),
                                         expected_run_id=_run_id()))
    except Exception as exc:
        _log(f"card completion failed ({type(exc).__name__}: {exc}); the crash sweep parks it")
        return False


class _Beat(threading.Thread):
    """§5.3.5: pure liveness beat (independent of child activity) + the cancel read."""

    def __init__(self, task_id: str, on_cancel: Callable[[], None], *,
                 beat: float = HEARTBEAT_SECONDS, poll: float = CARD_POLL_SECONDS):
        super().__init__(daemon=True, name="delegation-unit-beat")
        self.task_id, self.on_cancel = task_id, on_cancel
        self.beat, self.poll = beat, poll
        self.stop = threading.Event()
        self.cancelled = threading.Event()

    def run(self) -> None:
        from hermes_cli import kanban_db_dispatch as kbd

        last_beat = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - last_beat >= self.beat:
                last_beat = now
                try:
                    with _kb().connect_closing(board=_board()) as conn:
                        _kb().heartbeat_claim(conn, self.task_id,
                                              claimer=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"))
                        kbd.heartbeat_worker(conn, self.task_id, note="delegation unit running",
                                             expected_run_id=_run_id())
                except Exception as exc:
                    _log(f"heartbeat failed: {exc}")
            try:
                if _card_status(self.task_id) == "archived":
                    self.cancelled.set()
                    self.on_cancel()
                    return
            except Exception as exc:
                _log(f"card read failed: {exc}")
            self.stop.wait(self.poll)


def read_unit(task_id: str):
    """Step 2: the card body, then the record (one locked read). ``(body, record)``; ``record`` is
    None when the unit is not ``running`` on the card's attempt."""
    from tools import async_delegation_kanban as akb
    from tools import async_delegation_store as store

    kb = _kb()
    with kb.connect_closing(board=_board()) as conn:
        task = kb.get_task(conn, task_id)
    body = akb.parse_card_body(task.body if task is not None else None)
    if not body.get("delegation_id") or not body.get("attempt_id"):
        raise RuntimeError(f"card {task_id} carries no delegation unit")
    home = Path(body.get("profile_home") or os.environ.get("HERMES_HOME") or "")
    with store.locked_registry(home, write=False) as registry:
        record = registry["records"].get(body["delegation_id"])
        record = json.loads(json.dumps(record)) if isinstance(record, dict) else None
    current = (record is not None and record.get("state") == "running"
               and (record.get("attempt") or {}).get("attempt_id") == body["attempt_id"])
    return body, (record if current else None)


def _prefill_messages() -> Optional[list]:
    from hermes_cli.cli_config_load import _load_prefill_messages, _resolve_prefill_messages_file
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    messages = _load_prefill_messages(_resolve_prefill_messages_file(load_config() or {}),
                                      base_dir=get_hermes_home())
    return messages or None


def build_parent(record: Dict[str, Any]):
    """Step 3: the parent ``AIAgent`` from the CLI's resolver, keyed on the recorded route; the
    parent never runs a turn. Children get the profile's own ``delegation:`` config (read by the
    sync path); no provider override derived from the route is ADDED here."""
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider

    execution = record.get("execution") or {}
    route = execution.get("route") or {}
    session_route = record.get("route") or {}
    runtime = resolve_runtime_provider(
        requested=route.get("requested_provider") or None,
        target_model=route.get("model") or execution.get("model") or None,
        explicit_base_url=route.get("explicit_base_url") or None)
    cfg = load_config() or {}
    pr = cfg.get("provider_routing") or {}
    kwargs: Dict[str, Any] = {
        "api_key": runtime.get("api_key"), "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"), "requested_provider": runtime.get("requested_provider"),
        "api_mode": runtime.get("api_mode"), "command": runtime.get("command"),
        "args": list(runtime.get("args") or []), "credential_pool": runtime.get("credential_pool"),
        "request_overrides": runtime.get("request_overrides"),
        "model": route.get("model") or execution.get("model") or "",
        "max_iterations": int(execution.get("max_iterations") or 45),
        "quiet_mode": True, "skip_memory": True, "skip_context_files": True,
        "prefill_messages": _prefill_messages(),
        "providers_allowed": pr.get("only"), "providers_ignored": pr.get("ignore"),
        "providers_order": pr.get("order"), "provider_sort": pr.get("sort"),
        "provider_require_parameters": bool(pr.get("require_parameters", False)),
        "provider_data_collection": pr.get("data_collection"),
    }
    enabled = (execution["parent_toolsets"] if execution.get("parent_toolsets") is not None
               else execution.get("toolsets"))
    for key, value in {"enabled_toolsets": enabled,
                       "disabled_toolsets": execution.get("parent_disabled_toolsets"),
                       "platform": session_route.get("platform"),
                       "session_id": session_route.get("parent_session_id"),
                       "reasoning_config": execution.get("reasoning_config"),
                       "service_tier": execution.get("service_tier"),
                       "fallback_model": execution.get("fallback_chain")}.items():
        if value is not None:
            kwargs[key] = value
    parent = AIAgent(**kwargs)
    setattr(parent, "_delegate_depth", int(execution.get("parent_depth") or 0))
    setattr(parent, "terminal_cwd", execution.get("workspace_hint") or None)
    logger.info("delegation_unit_route provider=%s requested=%s base_url=%s api_mode=%s model=%s only=%s "
                "ignore=%s data_collection=%s prefill=%d", getattr(parent, "provider", None),
                getattr(parent, "requested_provider", None), getattr(parent, "base_url", None),
                getattr(parent, "api_mode", None), getattr(parent, "model", None), pr.get("only"),
                pr.get("ignore"), pr.get("data_collection"), len(kwargs.get("prefill_messages") or []))
    return parent


def _isolate_process_checkpoint(task_id: str) -> None:
    """This process has its own ProcessRegistry: its checkpoint must not replace the gateway's. Its
    file edits are cross-process siblings of the gateway's and other units' (tools/file_state.py)."""
    import tools.file_state as file_state
    import tools.process_registry as process_registry_module

    file_state.FORCE_CROSS_PROCESS = True
    root = _kb().worker_logs_dir(board=_board())
    process_registry_module.CHECKPOINT_PATH = root / f"{task_id}.processes.json"
    process_registry_module.HANDOFF_UNAVAILABLE = (
        "handoff is unavailable in a background subagent unit: your parent runs in another process "
        "and cannot take over this one. Wait for the process to finish and report its result, or kill it.")


def _envelope_status(result: Dict[str, Any]) -> str:
    from tools.async_delegation import _envelope_status as classify

    return classify(result)


def run(*, runner_factory: Optional[Callable[[Dict[str, Any], Any], Callable[[], Any]]] = None,
        parent_factory: Optional[Callable[[Dict[str, Any]], Any]] = None) -> int:
    """The runner body. ``runner_factory`` / ``parent_factory`` are test seams."""
    from agent.delegation_context import claim_kanban_worker_authority
    from tools import async_delegation_store as store

    claim_kanban_worker_authority()
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    if not task_id:
        _log("HERMES_KANBAN_TASK is not set; this command is spawned by the kanban dispatcher only")
        return 2
    body, record = read_unit(task_id)
    delegation_id, attempt_id = body["delegation_id"], body["attempt_id"]
    home = Path(body.get("profile_home") or os.environ.get("HERMES_HOME") or "")
    if record is None:
        _log(f"{delegation_id} attempt {attempt_id} is not current: superseded, nothing to run")
        complete_card(task_id, "superseded", {"delegation_id": delegation_id, "outcome": "superseded"})
        return 0
    parent_box: Dict[str, Any] = {}

    def _cancel() -> None:
        parent = parent_box.get("parent")
        if parent is not None:
            try:
                parent.interrupt("Background delegation cancelled")
            except Exception:
                pass

    beat = _Beat(task_id, _cancel)
    beat.start()
    status = "error"
    try:
        _isolate_process_checkpoint(task_id)
        parent = (parent_factory or build_parent)(record)
        parent_box["parent"] = parent
        if beat.cancelled.is_set():
            _cancel()
        if runner_factory is not None:
            runner = runner_factory(record, parent)
        else:
            from tools.delegate_tool import build_recovered_delegation_runner

            runner = build_recovered_delegation_runner(
                {**record, "live_delegation_id": delegation_id}, None, parent, source="gateway_kanban")
        session_db = None
        if runner_factory is None:
            from tools.delegate_detached import _acquire_session_db

            session_db = _acquire_session_db()
            setattr(parent, "_session_db", session_db)
            setattr(parent, "_end_session_on_close", False)
        try:
            raw = runner()
        finally:
            if session_db is not None:
                from tools.delegate_detached import _release_session_db

                setattr(parent, "_session_db", None)
                _release_session_db(session_db)
        result = raw if isinstance(raw, dict) else {
            "status": "error", "summary": None, "error": "runner returned a non-object result"}
        status = _envelope_status(result)
        if beat.cancelled.is_set():
            _log(f"{delegation_id} cancelled (card archived): no result written")
            return 0
        payload = store.append_terminal(delegation_id, attempt_id, result, status,
                                        profile_home=home, queued_boot_id=None)
        if payload is not None:
            _mirror(payload, result, home)
    except BaseException as exc:  # noqa: BLE001 -- the registry is the only channel back
        if beat.cancelled.is_set():
            _log(f"{delegation_id} cancelled (card archived) during {type(exc).__name__}")
            return 0
        tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]
        _log(f"unit crashed: {tail}")
        store.fail_attempt(delegation_id, attempt_id, f"{type(exc).__name__}: {exc}\n{tail}",
                           profile_home=home, queued_boot_id=None)
        status = "error"
    finally:
        beat.stop.set()
    complete_card(task_id, status, {"delegation_id": delegation_id, "status": status})
    return 0


def _mirror(payload: Dict[str, Any], result: Dict[str, Any], home: Path) -> None:
    """Legacy SQLite mirror (``_persist_completion``) under the profile's home; never fatal."""
    from tools.async_delegation import _persist_completion
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(home)
    try:
        _persist_completion(payload, result)
    except Exception as exc:
        _log(f"terminal mirror failed: {exc}")
    finally:
        reset_hermes_home_override(token)


def main(argv: Optional[list] = None) -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 -- before step 2: nothing to fail, never requeue
        _log(f"setup failed: {type(exc).__name__}: {exc}")
        return 0


def cmd_delegation_unit(args) -> int:
    """``hermes -p <profile> delegation-unit`` (hidden)."""
    return main()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
