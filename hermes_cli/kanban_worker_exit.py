"""Run-scoped worker exit receipts, independent of process reaping order."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import time


class WorkerExit(SystemExit):
    """Carry the final provider reason through CLI cleanup without error text."""

    def __init__(self, result):
        from agent.delegation_context import owns_kanban_worker_authority
        from agent.error_classifier import _POOL_EXHAUSTED_PATTERNS
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        self.failure_reason = result.get("failure_reason") if isinstance(result, dict) else None
        code = 0
        if isinstance(result, dict) and result.get("failed"):
            code = 1
            pool_exhausted = self.failure_reason == "pool_exhausted" or (
                self.failure_reason == "overloaded"
                and any(p in str(result.get("error", "")).lower() for p in _POOL_EXHAUSTED_PATTERNS)
            )
            if (
                os.environ.get("HERMES_KANBAN_TASK")
                and owns_kanban_worker_authority()
                and (self.failure_reason in ("rate_limit", "billing") or pool_exhausted)
            ):
                code = KANBAN_RATE_LIMIT_EXIT_CODE
        super().__init__(code)


def report_exit(exc: BaseException | None) -> None:
    if isinstance(exc, SystemExit):
        code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
    else:
        code = 1 if exc is not None else 0
    write_exit_status(code, getattr(exc, "failure_reason", None))


def exit_file(db_path: Path, task_id: str, run_id: int) -> Path:
    return db_path.parent / "runs" / f"{task_id}.{run_id}.exit.json"


def read_exit_status(path: Path) -> int | None:
    try:
        with path.open(encoding="utf-8") as stream:
            payload = json.loads(stream.read(4096))
        code = payload.get("exit_code")
        if type(code) is int and 0 <= code <= 255:
            return code
    except (OSError, ValueError, AttributeError):
        pass
    return None


def write_exit_status(code: int, failure_reason: str | None = None) -> None:
    """Atomically publish only from the owning worker; never record error text."""
    from agent.delegation_context import owns_kanban_worker_authority

    target = os.environ.get("HERMES_KANBAN_EXIT_FILE")
    if not target or not owns_kanban_worker_authority():
        return
    temp = None
    try:
        path = Path(target)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temp = Path(stream.name)
            json.dump({"exit_code": code, "failure_reason": failure_reason,
                       "ts": time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError:
        logging.getLogger(__name__).warning("Could not publish worker exit receipt", exc_info=True)
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
