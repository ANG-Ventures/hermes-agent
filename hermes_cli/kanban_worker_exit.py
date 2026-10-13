"""Run-scoped worker exit receipts, independent of process reaping order."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time


# Exit classes stamped into the receipt. All of them route to the
# retry-preserving ``KANBAN_RATE_LIMIT_EXIT_CODE``; they are kept distinct so
# telemetry (the receipt, the ``rate_limited`` event payload, the worker-exit
# hook) can tell a quota wall from a capped relay pool from a vendor that is
# simply full.
EXIT_CLASS_QUOTA = "quota"                        # rate_limit / billing
EXIT_CLASS_POOL_EXHAUSTED = "pool_exhausted"      # our relay: no eligible sub
EXIT_CLASS_UPSTREAM_CAPACITY = "upstream_capacity"  # vendor 529/503 overload
EXIT_CLASS_PINNED_PROVIDER = "pinned_provider_unavailable"  # card pin refused a failover

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


# A provider REJECTION is not a wall: HTTP 400/401/403/404 (bad request, bad credential,
# forbidden, unknown model) answers the same way on every retry. Incident t_d564a8dd: 24
# consecutive ``HTTP 404: model: claude-sonnet-4-8`` exits were booked ``rate_limited`` for 8 h
# because the pinned worker's refusal path mapped every retryable "unknown" onto the
# retry-preserving class. 429 and the relay's capacity 503 keep ``rate_limited``.
_PROVIDER_REJECTED_RE = re.compile(
    r"\bHTTP[\s:]*(?:400|401|403|404)\b[^\n]*", re.IGNORECASE,
)
_PROVIDER_WALL_RE = re.compile(r"\bHTTP[\s:]*(?:429|503|529)\b", re.IGNORECASE)
PROVIDER_REJECTED_TAIL_LINES = 8


def provider_rejected_status(text: str | None) -> str | None:
    """The provider's own ``HTTP 4xx ...`` status line from a failure text, else None.

    Reads only the LAST few non-empty lines (the run's verdict), so an earlier, already
    recovered 404 inside a long log never re-labels a later quota wall. A 429/503/529 in
    that tail wins: the run was walled, whatever came before.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    tail = "\n".join(lines[-PROVIDER_REJECTED_TAIL_LINES:])
    if _PROVIDER_WALL_RE.search(tail):
        return None
    match = _PROVIDER_REJECTED_RE.search(tail)
    return match.group(0).strip() if match else None


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
    if failure_reason == EXIT_CLASS_PINNED_PROVIDER:
        # A card-pinned worker refused a runtime failover and its pinned
        # provider kept failing (t_ed0289e3): a route outage, not a task error.
        return EXIT_CLASS_PINNED_PROVIDER
    return None


SAFEGUARD_REFUSAL_MARK = "safeguard_refusal"


def _park_needs_input(reason: str) -> bool:
    """Block THIS worker's running card ``needs_input`` with ``reason``, fenced to this run.

    Only the owning worker of a running card acts; anything else is a no-op.
    block_task's run CAS is off when expected_run_id is None, so a missing or
    malformed run id must not mutate the card (a stale worker could otherwise
    park a newer run). Raises on DB errors; callers own the logging.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    from agent.delegation_context import owns_kanban_worker_authority
    if not task_id or not owns_kanban_worker_authority():
        return False
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
    except ValueError:
        return False
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return bool(kb.block_task(conn, task_id, reason=reason, kind="needs_input",
                                  expected_run_id=run_id))
    finally:
        conn.close()


def block_on_rail_absent(message: str) -> bool:
    """End this worker's run on a ``^[A-Z_]+_ABSENT`` line (rail tool, skill_view gate).

    t_db491366: a worker that needs a capability its host lacks must not retry.
    t_ecfd5591 (F1): a PLACED worker's card is re-routed once by its own
    ``placement_reroute`` ledger (``requires: <cap>`` appended to the body) and a
    second miss parks it ``needs_input``; an unplaced worker parks ``needs_input``
    with the line verbatim. A first miss whose host-local workspace may hold work parks
    instead (custody, 0e9694bf2944). Never raises.
    """
    try:
        from hermes_cli import kanban_worker_hosts as kwh
        placed = kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))
        if kwh.is_pool_host(placed):
            from hermes_cli import kanban_placement_failfast as pff
            handled = pff.worker_exit_on_absent(str(message or "")) is not None
        else:  # unplaced: #1876 verbatim
            if "_RAIL_ABSENT:" not in str(message or ""):
                return False
            from agent.delegation_context import owned_kanban_task
            if not owned_kanban_task():  # in-process cron / delegate child: never park the worker's card
                return False
            handled = _park_needs_input(str(message))
        if handled:  # Hook A (t_b1100bf0): its own transaction, after the board decision committed
            try:  # best-effort: a hook failure must never turn a committed decision into "not handled"
                from tools.capability_gap_hooks import on_rail_absent
                on_rail_absent(str(message))
            except Exception:
                logging.getLogger(__name__).warning("rail-absent capability gap hook failed", exc_info=True)
        return handled
    except Exception:
        logging.getLogger(__name__).warning("rail-absent block failed", exc_info=True)
        return False


def block_on_safeguard_refusal(result) -> bool:
    """Park this worker's card as ``needs_input`` when its turn was refused.

    Ace ruling 2026-10-02 (t_997efe88): a Claude bridge/relay safeguard refusal
    is never answered by another model, and a respawn would re-send the same
    flagged content. The card goes ``needs_input`` with the refusal line as the
    block reason, so the EXISTING needs_input pager (#1612) posts it to the
    card's origin chat, where the request gets rephrased. Only the owning
    worker of a running card acts; anything else is a no-op. Never raises.
    """
    try:
        if not (isinstance(result, dict) and result.get("failed")):
            return False
        if result.get("error_code") != SAFEGUARD_REFUSAL_MARK:
            return False
        error = str(result.get("error") or "")
        task_id = os.environ.get("HERMES_KANBAN_TASK")
        from agent.delegation_context import owns_kanban_worker_authority
        if not task_id or not owns_kanban_worker_authority():
            return False
        from hermes_cli import kanban_db as kb
        # Fenced to THIS run: block_task's run CAS is off when expected_run_id is
        # None, so a missing/malformed run id must not mutate the card (a stale
        # worker could otherwise park a newer run).
        try:
            run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
        except ValueError:
            return False
        detail = error.replace("content_policy_blocked:", "", 1).strip()[:400]
        reason = (
            "safeguard_refusal: the model's safeguards refused this card's turn "
            "(no other model answers it, by ruling). Rephrase the card body/request "
            f"and unblock. Refusal: {detail}"
        )
        conn = kb.connect()
        try:
            return bool(kb.block_task(conn, task_id, reason=reason, kind="needs_input",
                                      expected_run_id=run_id))
        finally:
            conn.close()
    except Exception:
        logging.getLogger(__name__).warning("safeguard-refusal block failed", exc_info=True)
        return False


class WorkerExit(SystemExit):
    """Carry the final provider reason through CLI cleanup without error text."""

    def __init__(self, result):
        from agent.delegation_context import owns_kanban_worker_authority
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        self.failure_reason = result.get("failure_reason") if isinstance(result, dict) else None
        self.exit_class = None
        # Positive evidence for the dispatcher's reproduced-clean-exit shortcut.
        # A provider/bootstrap abort can also exit 0 on older wrappers, but it
        # cannot produce a successful model response. Fail closed: absent and
        # legacy receipts remain ineligible for the shortcut.
        self.model_turn_completed = bool(
            isinstance(result, dict)
            and not result.get("failed")
            and result.get("final_response")
        )
        code = 0
        # t_997efe88: a refused turn parks the card needs_input (refusal line in
        # the reason -> the needs_input pager posts it to the origin chat).
        self.blocked_on_refusal = block_on_safeguard_refusal(result)
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
        if code == 1 and os.environ.get("HERMES_KANBAN_TASK"):
            _print_failure_traceback(result)
        super().__init__(code)


def _print_failure_traceback(result) -> None:
    """Write the failing exception's traceback to the worker log (stderr).

    Without it an exit-1 worker log holds one bare error line before the run
    boundary and the cause needs archaeology in the profile errors.log
    (t_6b01c2d1).
    """
    tb = result.get("error_traceback") if isinstance(result, dict) else None
    if not isinstance(tb, str) or not tb.strip():
        return
    try:
        sys.stderr.write("kanban worker exit 1, failing exception:\n" + tb.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def report_exit(exc: BaseException | None) -> None:
    if isinstance(exc, SystemExit):
        code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
    else:
        code = 1 if exc is not None else 0
    write_exit_status(
        code,
        getattr(exc, "failure_reason", None),
        exit_class=getattr(exc, "exit_class", None),
        model_turn_completed=bool(getattr(exc, "model_turn_completed", False)),
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


def read_model_turn_completed(path: Path) -> bool:
    """Whether this run produced a successful model response.

    Missing/legacy/malformed receipts fail closed: they cannot authorize the
    reproduced-clean-exit shortcut.
    """
    return _read_receipt(path).get("model_turn_completed") is True


def write_exit_status(
    code: int,
    failure_reason: str | None = None,
    exit_class: str | None = None,
    model_turn_completed: bool = False,
) -> None:
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
            json.dump({
                "exit_code": code,
                "failure_reason": failure_reason,
                "exit_class": exit_class,
                "model_turn_completed": model_turn_completed is True,
                "ts": time.time(),
            }, stream)
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
