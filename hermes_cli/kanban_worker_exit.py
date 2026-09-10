"""Run-scoped worker exit receipts, independent of process reaping order."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time


# Exit classes stamped into the receipt. All three route to the
# retry-preserving ``KANBAN_RATE_LIMIT_EXIT_CODE``; they are kept distinct so
# telemetry (the receipt, the ``rate_limited`` event payload, the worker-exit
# hook) can tell a quota wall from a capped relay pool from a vendor that is
# simply full.
EXIT_CLASS_QUOTA = "quota"                        # rate_limit / billing
EXIT_CLASS_POOL_EXHAUSTED = "pool_exhausted"      # our relay: no eligible sub
EXIT_CLASS_UPSTREAM_CAPACITY = "upstream_capacity"  # vendor 529/503 overload

# Upstream CAPACITY overload — the vendor (Anthropic/OpenAI 529-style) is
# full. Matched only when the classifier already stamped
# ``failure_reason == "overloaded"`` (503/529 status or an overload-flavoured
# body), so an app-level 500, an assertion, or an OOM (``server_error`` /
# ``unknown`` / ``tool_error``) can never reach this list. Kept narrow on
# purpose: an unrecognised "overloaded" body still exits 1.
_UPSTREAM_CAPACITY_PATTERNS = (
    "overloaded",
    "capacity",
    "try again later",
    "service unavailable",
    "service temporarily unavailable",
)
_UPSTREAM_CAPACITY_STATUS_RE = re.compile(r"\b(?:503|529)\b")


def is_quota_exit(failure_reason: str | None, error: str = "") -> bool:
    """Provider quota wall: rate-limited or billing-exhausted (#655)."""
    return failure_reason in ("rate_limit", "billing")


def is_pool_exhausted_exit(failure_reason: str | None, error: str = "") -> bool:
    """Our local multi-sub relay has no eligible sub (#655)."""
    from agent.error_classifier import _POOL_EXHAUSTED_PATTERNS

    if failure_reason == "pool_exhausted":
        return True
    return failure_reason == "overloaded" and any(
        p in str(error).lower() for p in _POOL_EXHAUSTED_PATTERNS
    )


def is_upstream_capacity_exit(failure_reason: str | None, error: str = "") -> bool:
    """The model provider itself is over capacity (529 / 503 / overloaded).

    Same remedy as a quota wall — park, cool down, retry without spending a
    retry — but a different cause, so it is a separate predicate.
    """
    if failure_reason != "overloaded":
        return False
    if is_pool_exhausted_exit(failure_reason, error):
        return False
    text = str(error).lower()
    return any(p in text for p in _UPSTREAM_CAPACITY_PATTERNS) or bool(
        _UPSTREAM_CAPACITY_STATUS_RE.search(text)
    )


def worker_exit_class(failure_reason: str | None, error: str = "") -> str | None:
    """Name the retry-preserving class a failed result belongs to, or None."""
    if is_quota_exit(failure_reason, error):
        return EXIT_CLASS_QUOTA
    if is_pool_exhausted_exit(failure_reason, error):
        return EXIT_CLASS_POOL_EXHAUSTED
    if is_upstream_capacity_exit(failure_reason, error):
        return EXIT_CLASS_UPSTREAM_CAPACITY
    return None


class WorkerExit(SystemExit):
    """Carry the final provider reason through CLI cleanup without error text."""

    def __init__(self, result):
        from agent.delegation_context import owns_kanban_worker_authority
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        self.failure_reason = result.get("failure_reason") if isinstance(result, dict) else None
        self.exit_class = None
        code = 0
        if isinstance(result, dict) and result.get("failed"):
            code = 1
            exit_class = worker_exit_class(self.failure_reason, result.get("error", ""))
            if (
                exit_class is not None
                and os.environ.get("HERMES_KANBAN_TASK")
                and owns_kanban_worker_authority()
            ):
                code = KANBAN_RATE_LIMIT_EXIT_CODE
                self.exit_class = exit_class
        super().__init__(code)


def report_exit(exc: BaseException | None) -> None:
    if isinstance(exc, SystemExit):
        code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
    else:
        code = 1 if exc is not None else 0
    write_exit_status(
        code,
        getattr(exc, "failure_reason", None),
        exit_class=getattr(exc, "exit_class", None),
    )


def exit_file(db_path: Path, task_id: str, run_id: int) -> Path:
    return db_path.parent / "runs" / f"{task_id}.{run_id}.exit.json"


def _read_receipt(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            payload = json.loads(stream.read(4096))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def read_exit_status(path: Path) -> int | None:
    code = _read_receipt(path).get("exit_code")
    if type(code) is int and 0 <= code <= 255:
        return code
    return None


def read_exit_class(path: Path) -> str | None:
    """The receipt's exit class, or None for a legacy/absent/unclassed receipt."""
    value = _read_receipt(path).get("exit_class")
    return value if isinstance(value, str) and value else None


def write_exit_status(code: int, failure_reason: str | None = None,
                      exit_class: str | None = None) -> None:
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
                       "exit_class": exit_class, "ts": time.time()}, stream)
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
