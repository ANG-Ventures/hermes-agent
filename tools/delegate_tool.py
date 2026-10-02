#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with a fresh conversation, their own task_id
(terminal session, file-ops cache), the parent's toolsets minus child-blocked
tools, and a focused system prompt built from goal + context. Single-task and
batch (parallel) modes; top-level model calls run in the background while
orchestrator children wait for their own workers. The parent only ever sees
the delegation call and the summary result, never the child's intermediate
tool calls or reasoning.
"""

import logging
import time
import weakref
from typing import Any, Dict, List, Optional

from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb  # noqa: F401  (used via _ChildRun.await_child)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

# The delegate_tool_* siblings hold the pieces split out of this module; every name callers or patching tests reach as
# ``tools.delegate_tool.<name>`` is re-imported here. Mutable flag globals live only in their owning module.
from tools.delegate_tool_child_run import (  # noqa: F401
    _ChildRun, _attach_child, _build_child_goal_message, _build_result_entry, _dump_subagent_timeout_diagnostic, _fabricated_entry,
    _lease_child_credential, _merge_late_steer, _register_child, _start_heartbeat, _validate_child_output_schema,
)
from tools.delegate_tool_config import (  # noqa: F401
    _DEFAULT_MAX_CONCURRENT_CHILDREN, _get_child_timeout, _get_max_async_children, _get_max_concurrent_children,
    _get_max_spawn_depth, _get_oneshot_max_children, _get_orchestrator_enabled, _get_subagent_approval_callback, _get_worktree_isolation,
    _inherit_parent_capabilities, _load_config, _merge_request_overrides, _resolve_child_credential_pool,
    _resolve_child_runtime, _resolve_delegation_credentials,
    _subagent_auto_approve, _subagent_auto_deny,
)
from tools.delegate_tool_dispatch import _Batch, _announce_batch, _capture_origin, _run_batch
from tools.delegate_tool_progress import (  # noqa: F401
    DelegateEvent, SUBAGENT_FAILURE_STATUSES, _batch_prefix, _build_child_progress_callback,
    _build_child_system_prompt, _clean_error_text, _emit_parent_console, _quiet, _resolve_workspace_hint,
    _safe_progress, format_batch_tag, format_subagent_failure_line,
)
from tools.delegate_tool_registry import (  # noqa: F401
    _CONTROL_ACTIONS, _active_subagents, _active_subagents_lock, _capture_gateway_steer_authority,
    _handle_control_action, _is_descendant_of, _owns_subagent_record, _register_subagent, _unregister_subagent,
    get_subagent_attribution, interrupt_subagent, is_spawn_paused, list_active_subagents, set_spawn_paused,
    steer_subagent,
)
from tools.delegate_tool_tasks import (  # noqa: F401
    _MAX_TASK_IMAGES, _coerce_task_images, _coerce_task_schemas, _normalize_task_images, _normalize_task_list,
)
from tools.delegate_tool_toolsets import (  # noqa: F401
    DELEGATE_BLOCKED_TOOLS, _expand_parent_toolsets, _resolve_child_toolsets, _strip_blocked_tools,
)
from tools.delegate_tool_results import (  # noqa: F401
    _apply_summary_budget, _build_child_preserving_parent_tools, _run_child_lifecycle, _summarize_tool_arguments,
)

# Fork-side imports (child-lifecycle supervisor, late results, durable background recovery).
import contextvars
import copy
import enum
import json
import os
import re
import threading
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    wait as futures_wait,
)
from typing import Callable, Tuple

from agent.interrupt_compat import request_hard_interrupt
from tools import file_state
from tools.delegate_tool_config import _RUNTIME_PROVIDER_CUSTOM  # noqa: F401
# Sibling-owned helpers the fork's inline _run_single_child / delegate_task still call directly.
from tools.delegate_tool_registry import _close_subagent_steering  # noqa: F401
from tools.delegate_tool_results import (  # noqa: F401
    _extract_output_tail, _finalize_child_results, _looks_like_error_output, _stringify_tool_content,
)
from tools.delegate_tool_tasks import _recover_tasks_from_json_string, _validate_batch_tasks  # noqa: F401


_ROLES = frozenset({"leaf", "orchestrator"})

# Nested delegation is granted by depth/role in _build_child_agent, never by the
# model naming toolsets (there is no model-facing toolsets argument).
def _normalize_role(r: Optional[str]) -> str:
    """'leaf' | 'orchestrator'; None/empty/unknown -> 'leaf' (unknown warns)."""
    r_norm = str(r).strip().lower() if r else "leaf"
    if r_norm not in _ROLES:
        logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
        return "leaf"
    return r_norm

DEFAULT_MAX_ITERATIONS = 250

_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation

# Stale-heartbeat thresholds (cycles of _HEARTBEAT_INTERVAL with no progress). Progress = iteration, current_tool OR
# last_activity_ts advancing; an in-flight model wait refreshes last_activity_ts, so slow models are not "idle". Idle
# stays tight so a truly wedged child doesn't mask the gateway timeout; in-tool is much higher so legitimately long
# tools can finish.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 450s idle between turns → stale

_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 1200s stuck on same tool → stale


def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _open_child_session_db(parent_agent) -> Any:
    """DEDICATED SessionDB handle for the child, or None: the parent's handle can be closed by its own lifecycle while
    a background child still flushes (transcript silently dropped). It MUST open the same db FILE as the parent's
    handle (non-launch profiles), else lineage / session_search break; released by the child's close() via
    _owns_session_db."""
    # Each child gets a DEDICATED SessionDB connection instead of the parent's live object. The parent's
    # handle is owned by the parent's lifecycle (cron run_job's finally block, gateway session end, /new)
    # and can be closed while a fire-and-forget background child is still flushing on a daemon thread —
    # every subsequent flush then hits the closed handle and the child's transcript is silently dropped
    # (#81267). It MUST point at the same database FILE as the parent's handle: parents can hold non-default
    # per-profile handles (tui_gateway opens SessionDB(db_path=<profile>/ state.db) for non-launch
    # profiles), and a bare SessionDB() would write the child's transcript into the launch profile's db,
    # breaking parent_session_id lineage and session_search. AsyncSessionDB wrappers (gateway) forward
    # .db_path via __getattr__, so this works through them.
    parent_session_db = getattr(parent_agent, "_session_db", None)
    if parent_session_db is None:
        return None
    with _quiet("subagent: failed to open dedicated SessionDB; child persistence disabled", exc_info=True):
        from hermes_state_registry import acquire
        _parent_db_path = getattr(parent_session_db, "db_path", None)
        return acquire(_parent_db_path) if _parent_db_path is not None else acquire()
    return None


def _apply_child_cache_ttl(child) -> None:
    """A delegated child never uses the 1h cache tier. The tier is priced for a person who steps
    away between turns (2x write vs 1.25x for 5m, #14971); a subagent calls every few seconds for
    minutes and is gone, so it pays the 2x on every tool result and never collects the retention.
    Caching itself stays exactly as configured (disabled stays disabled)."""
    if getattr(child, "_cache_ttl", None) == "1h":
        child._cache_ttl = "5m"


_CHILD_CAP_MIN = 16_000  # below this a child compresses on every call; treat as a config error


def _child_compression_cap_tokens(raw) -> "int | None":
    """Validated ``delegation.compression_threshold_tokens``: an int >= 16000, or None for "no cap".

    Unset / ``0`` / ``false`` / ``null`` mean no subagent-specific cap: the child compacts at the
    same ratio trigger as everyone else (0.50 x window). A bool ``true`` (YAML) would coerce to 1
    and make every call compress; a string like ``"200k"`` would silently read as no cap. Both are
    config errors: warn and treat as unset so a typo never changes compaction behaviour."""
    if raw is None or raw is False or raw == 0:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) < _CHILD_CAP_MIN:
        logger.warning(
            "delegation.compression_threshold_tokens=%r is not a token count >= %d; ignoring it "
            "(children keep the ratio trigger).", raw, _CHILD_CAP_MIN,
        )
        return None
    return int(raw)


def _apply_child_compression_cap(child, delegation_cfg: dict) -> None:
    """Optional absolute cap on the child's compaction trigger, ``delegation.compression_threshold_tokens``
    (lower of it and any global ``compression.threshold_tokens``). Off by default: a 1M-window child
    compacts where its parent does. The compressor applies the cap on first window resolution, which
    happens after construction, so setting it here is exactly equivalent to config."""
    from agent.context_compressor import ContextCompressor

    cc = getattr(child, "context_compressor", None)
    if not isinstance(cc, ContextCompressor):
        return
    cap = _child_compression_cap_tokens((delegation_cfg or {}).get("compression_threshold_tokens"))
    if cap is None:
        return
    existing = cc.threshold_tokens_cap
    cc.threshold_tokens_cap = min(cap, existing) if isinstance(existing, int) and existing > 0 else cap
    if cc._threshold_tokens is not None:  # already resolved: re-clamp now
        cc._apply_threshold_tokens_cap()


# ── fork: steer ledger (#1549 / 1dcb249a12) ──
class _SteerLedger:
    """Per-child append-only record of every steer the child accepted.

    The I1 mechanism (docs/dev/delegate-child-lifecycle.md). ``for_child``
    wraps the child's ``steer`` and ``_drain_pending_steer`` and installs the
    agent loop's requeue/inject/consume sinks, so every producer (``steer_subagent``
    and any direct ``child.steer``, e.g. a late-result notification to a
    delegated orchestrator) is ledgered, and every drained batch carries the
    acceptance ids it holds. Delivery settles by id, never by inferring
    identity from text:

    - accept: entry appended, its piece appended to the mirrored slot.
    - drain: the slot is a suffix of the mirror (``interrupt`` clears it
      without telling us); the matched suffix becomes a batch with ids, the
      rest was dropped and stays open.
    - requeue: the batch goes back to the end of the mirrored slot.
    - inject: the batch was written into a message (``note_steer_injected``).
    - consume: a model response came back after the injection; injected
      batches are delivered. A turn that exits first leaves them open.

    ``missed()`` is "accepted and never delivered", in acceptance order,
    duplicates kept. ``deliver(text)`` (inject + consume) remains for callers
    that write and read in one step; text with no matching batch falls back to
    a line-aligned exact tiling of open entries.

    Durable copy: one JSON line per operation, appended to a file in the
    owning profile's delegation live dir (resolved on the spawning thread).
    Steer text is redacted there, as every line of that sandbox-mounted tree
    is; memory stays authoritative and unredacted. A failed write is logged,
    never raised.
    """

    _MAX_BATCHES = 64

    def __init__(self, path: Any = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._slot: List[Tuple[str, Optional[List[int]]]] = []
        self._batches: List[Dict[str, Any]] = []
        self._write_failed = False
        # Set by ``seal`` at the child's linearization point. A closed
        # ledger refuses every producer, including a direct ``child.steer()``
        # that never passes through the registry (Argus QA r2 C2).
        self._closed = False

    @classmethod
    def for_child(cls, child: Any, subagent_id: str) -> "_SteerLedger":
        path = None
        try:
            from pathlib import Path
            import uuid as _uuid

            transcript = getattr(child, "_live_transcript_path", None)
            if isinstance(transcript, str) and transcript:
                path = Path(transcript).with_suffix(".steer.jsonl")
            else:
                from tools.delegation_live_log import live_transcript_root

                # A dir per child, so the live-dir retention prune covers it.
                path = (
                    live_transcript_root()
                    / f"steer_{subagent_id}_{_uuid.uuid4().hex[:8]}"
                    / "steer.jsonl"
                )
        except Exception:
            logger.debug("steer ledger path unresolved for %s", subagent_id, exc_info=True)
        ledger = cls(path)
        ledger.attach(child, subagent_id)
        return ledger

    def attach(self, child: Any, subagent_id: str = "") -> None:
        try:
            orig_steer = getattr(child, "steer", None)
            orig_drain = getattr(child, "_drain_pending_steer", None)
            if callable(orig_steer):
                child.steer = lambda text, _o=orig_steer: self.steer_via(_o, text)
            if callable(orig_drain):
                child._drain_pending_steer = lambda _o=orig_drain: self.drain_via(_o)
            child._steer_ledger = self
            child._steer_delivery_sink = self.deliver
            child._steer_inject_sink = self.inject
            child._steer_consume_sink = self.consume
            child._steer_requeue_sink = self.requeue
        except Exception:
            logger.debug("could not attach steer ledger to %s", subagent_id, exc_info=True)

    def _append(self, op: Dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            if "text" in op:
                from tools.delegation_live_log import _redact

                op = {**op, "text": _redact(op["text"])}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**op, "ts": time.time()}, ensure_ascii=False) + "\n")
        except Exception:
            if not self._write_failed:
                self._write_failed = True
                logger.warning(
                    "steer ledger %s not writable; in-memory only", self.path, exc_info=True
                )

    def accept(self, text: str) -> int:
        with self._lock:
            seq = len(self._entries)
            self._entries.append({"seq": seq, "text": text.strip(), "state": "accepted"})
            self._append({"op": "accept", "seq": seq, "text": text.strip()})
            return seq

    def withdraw(self, seq: int) -> None:
        """The child did not take the text after the entry was written."""
        with self._lock:
            self._entries[seq]["state"] = "withdrawn"
            self._append({"op": "withdraw", "seq": seq})

    def seal(self) -> None:
        """No steer is accepted from now on; open entries stay as they are."""
        with self._lock:
            self._closed = True

    def steer_via(self, orig: Any, text: Any) -> bool:
        """The child's ``steer``: ledger entry first, then the real slot.

        Refused (False, no entry, slot untouched) once the ledger is closed:
        the child's record is final, so an accepted text could be neither
        delivered nor reported.
        """
        if not isinstance(text, str) or not text.strip():
            return bool(orig(text))
        with self._lock:
            if self._closed:
                logger.debug("steer refused: the child's steering is closed")
                return False
            seq = self.accept(text)
            try:
                ok = bool(orig(text))
            except BaseException:
                self.withdraw(seq)
                raise
            if ok:
                self._slot.append((text.strip(), [seq]))
            else:
                self.withdraw(seq)
            return ok

    def drain_via(self, orig: Any) -> Any:
        """The child's ``_drain_pending_steer``: bind the drained text to ids."""
        with self._lock:
            text = orig()
            pieces, self._slot = self._slot, []
            if not isinstance(text, str) or not text:
                return text  # cleared by interrupt: those entries stay open
            for k in range(len(pieces) + 1):
                if "\n".join(p[0] for p in pieces[k:]) == text:
                    seqs: Optional[List[int]] = []
                    for _t, ids in pieces[k:]:
                        if ids is None:
                            seqs = None
                            break
                        seqs.extend(ids)
                    break
            else:
                seqs = None  # text the ledger never saw: settle by text
            self._batches.append({"text": text, "seqs": seqs, "injected": False})
            del self._batches[: -self._MAX_BATCHES]
            return text

    def _take_batch(self, text: str, *, injected: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        for i in range(len(self._batches) - 1, -1, -1):
            b = self._batches[i]
            if b["text"] == text and (injected is None or b["injected"] is injected):
                return self._batches.pop(i)
        return None

    def requeue(self, text: str, put_back: Any) -> None:
        """The agent puts a drained batch back; it keeps its ids."""
        with self._lock:
            put_back()
            b = self._take_batch(text, injected=False)
            self._slot.append((text, b["seqs"] if b is not None else None))

    def inject(self, text: Any) -> None:
        """*text* was written into a message; delivered on the next ``consume``."""
        if not isinstance(text, str) or not text:
            return
        with self._lock:
            b = self._take_batch(text, injected=False)
            if b is None:
                b = {"text": text, "seqs": None}
            b["injected"] = True
            self._batches.append(b)

    def consume(self) -> None:
        """A model response came back after every injection so far."""
        with self._lock:
            done = [b for b in self._batches if b["injected"]]
            self._batches = [b for b in self._batches if not b["injected"]]
            for b in done:
                self._settle(b)

    def deliver(self, text: Any) -> None:
        """Consumer: *text* was written into the transcript and read."""
        if not isinstance(text, str) or not text:
            return
        with self._lock:
            b = self._take_batch(text, injected=False) or {"text": text, "seqs": None}
            self._settle(b)

    def _settle(self, batch: Dict[str, Any]) -> None:
        seqs = batch.get("seqs")
        if seqs is None:
            open_ = [e for e in self._entries if e["state"] == "accepted" and e["text"]]
            chosen = self._tile(batch["text"], open_)
            if chosen is None:
                chosen = self._aligned_spans(batch["text"], open_)
        else:
            chosen = [self._entries[s] for s in seqs if self._entries[s]["state"] == "accepted"]
        for e in sorted(chosen, key=lambda e: e["seq"]):
            e["state"] = "delivered"
            self._append({"op": "deliver", "seq": e["seq"]})

    @staticmethod
    def _tile(text: str, open_: List[Dict[str, Any]], budget: int = 10000) -> Optional[List[Dict[str, Any]]]:
        """Fallback for text with no batch: open entries whose texts, joined
        by "\n" in some order, equal *text*; newest acceptance preferred.

        Iterative (explicit stack), so a batch of thousands of steers cannot
        hit the recursion limit; *budget* bounds the search.
        """
        by_text: Dict[str, List[Dict[str, Any]]] = {}
        for e in sorted(open_, key=lambda e: -e["seq"]):
            by_text.setdefault(e["text"], []).append(e)
        texts = sorted(by_text, key=lambda t: -max(e["seq"] for e in by_text[t]))
        used: Dict[str, int] = {}
        picked: List[Dict[str, Any]] = []
        # Frame: (pos, next text index to try, text taken to reach pos or None).
        stack: List[List[Any]] = [[0, 0, None]]
        steps = 0
        while stack:
            frame = stack[-1]
            pos, i, _taken = frame
            if pos == len(text):
                return list(picked)
            steps += 1
            if steps > budget:
                return None
            advanced = False
            while i < len(texts):
                t = texts[i]
                i += 1
                n = used.get(t, 0)
                if n >= len(by_text[t]) or not text.startswith(t, pos):
                    continue
                end = pos + len(t)
                # A separator must be followed by another entry: a trailing
                # "\n" is not a tiling (Prism r2 32e5dda430de).
                if end != len(text) and (text[end] != "\n" or end + 1 == len(text)):
                    continue
                frame[1] = i
                used[t] = n + 1
                picked.append(by_text[t][n])
                stack.append([end if end == len(text) else end + 1, 0, t])
                advanced = True
                break
            if advanced:
                continue
            stack.pop()
            if frame[2] is not None:
                picked.pop()
                used[frame[2]] -= 1
        return None

    @staticmethod
    def _aligned_spans(text: str, open_: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fallback: newest-first, longest-first, line-aligned, disjoint spans."""
        remaining = "\n" + text + "\n"
        chosen = []
        for e in sorted(open_, key=lambda e: (-len(e["text"]), -e["seq"])):
            i = remaining.find("\n" + e["text"] + "\n")
            if i < 0:
                continue
            chosen.append(e)
            span = len(e["text"])
            remaining = remaining[: i + 1] + "\0" * span + remaining[i + 1 + span :]
        return chosen

    def missed(self) -> Optional[str]:
        with self._lock:
            return "\n".join(e["text"] for e in self._entries if e["state"] == "accepted") or None

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {"accepted": 0, "delivered": 0, "withdrawn": 0}
            for e in self._entries:
                out[e["state"]] += 1
            return out


def _steer_ledger_of(child: Any) -> Optional[_SteerLedger]:
    ledger = getattr(child, "_steer_ledger", None)
    return ledger if isinstance(ledger, _SteerLedger) else None


def _close_and_read_missed(subagent_id: Optional[str], child: Any) -> Optional[str]:
    """Close steer acceptance, then read what was accepted and never delivered."""
    if not subagent_id:
        return None
    _close_subagent_steering(subagent_id, child)
    ledger = _steer_ledger_of(child)
    return ledger.missed() if ledger is not None else None


# ── fork: timed_out_running / subtree reap / outcome classification (#1535 #1542 39e31cc975) ──
# Outcome of a delegate_task wait that hit child_timeout_seconds while the
# child (or its subtree) is still working. NOT a failure: the child keeps
# running and delivers its real result later. Reporting it as dead made a
# root re-launch the same brief, running two trees concurrently (2026-09-08).
TIMED_OUT_RUNNING = "timed_out_running"


def _live_subtree_records(root_agent: Any) -> List[Dict[str, Any]]:
    """Live registry records strictly BELOW *root_agent* in the spawn tree.

    Matches on the weakref identity chain and, as a fallback for a broken
    chain, on the ``parent_id`` subagent-id chain rooted at the root's id.
    """
    if root_agent is None:
        return []
    root_sid = getattr(root_agent, "_subagent_id", None)
    with _active_subagents_lock:
        records = list(_active_subagents.values())
    below_ids: set = set()
    if isinstance(root_sid, str) and root_sid:
        frontier = {root_sid}
        while frontier:
            nxt = {
                r.get("subagent_id")
                for r in records
                if r.get("parent_id") in frontier
                and r.get("subagent_id") not in below_ids
            }
            nxt.discard(None)
            below_ids |= nxt
            frontier = nxt
    out = []
    for r in records:
        agent = r.get("agent")
        if agent is root_agent:
            continue
        if r.get("subagent_id") in below_ids or _is_descendant_of(agent, root_agent):
            out.append(r)
    return out


def _reap_subtree(root_agent: Any, reason: str) -> List[str]:
    """Cancel every live descendant of *root_agent* (its own stop is the caller's).

    Called on a GENUINE failure of *root_agent* (exception, kill) so no orphan
    outlives its parent's reported failure. A descendant carrying an explicit
    internal ``_delegate_detach`` flag is left running. Returns reaped ids.
    """
    reaped: List[str] = []
    for r in _live_subtree_records(root_agent):
        agent = r.get("agent")
        if agent is None or getattr(agent, "_delegate_detach", False) is True:
            continue
        try:
            if request_hard_interrupt(agent, f"Parent subagent failed ({reason})"):
                reaped.append(str(r.get("subagent_id")))
        except Exception as exc:
            logger.debug("subtree reap of %s failed: %s", r.get("subagent_id"), exc)
    if reaped:
        logger.info(
            "delegate_task reap: parent=%s reason=%s reaped=%s",
            getattr(root_agent, "_subagent_id", None),
            reason,
            ",".join(reaped),
        )
    return reaped


def _classify_child_outcome(result: Dict[str, Any]) -> Tuple[str, str]:
    """One terminal-outcome classifier for a child turn that RETURNED a result.

    Returns ``(status, exit_reason)``. A child whose run_conversation returned
    ``failed=True`` (non-retryable API error, billing wall, content-policy
    block, ...) is ``failed``/``error`` even though its ``final_response``
    carries the error text: that text is not a usable answer, and calling it
    ``completed``/``max_iterations`` hid the failure and skipped the subtree
    reap (Argus QA t_86717544 F1/F4). The raise path stays the caller's.
    """
    summary = str(result.get("final_response") or "")
    if result.get("interrupted"):
        return "interrupted", "interrupted"
    if result.get("failed"):
        return "failed", "error"
    if summary and summary.strip() != "(empty)":
        return "completed", ("completed" if result.get("completed", False) else "max_iterations")
    return "failed", ("completed" if result.get("completed", False) else "max_iterations")


# ── fork: child timeout floor (#1573 7ff750b798) ──
# Floor for delegation.child_timeout_seconds. A leaf inside one silent tool
# call only refreshes last_activity_ts on the tool-activity heartbeat
# (agent/tool_executor.py::_TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S, 30 s) plus
# scheduling overhead. A cap at or below that interval expires before the
# first tick and hard-stops a live leaf mid-tool, so the floor must clear
# the heartbeat with margin: 2x the interval.
_CHILD_TIMEOUT_FLOOR_S = 60.0


# ── fork: child max wall / hung ceiling (#1598 #1599) ──
DEFAULT_CHILD_MAX_WALL_MULTIPLIER = 4.0


def _get_child_max_wall_seconds(child_timeout: Optional[float]) -> Optional[float]:
    """Absolute wall-clock ceiling for a child that went TIMED_OUT_RUNNING.

    The late-completion owner is progress-aware (no progress for
    child_timeout -> stopped), but progress-aware is not unbounded: a child
    that keeps making progress is stopped once it has run this long since
    its start. Config ``delegation.child_max_wall_seconds``: 0/unset (and any
    invalid or negative value) = DEFAULT_CHILD_MAX_WALL_MULTIPLIER x
    child_timeout, never below that multiple of child_timeout's own floor
    (``_CHILD_TIMEOUT_FLOOR_S``; identical for every configurable
    child_timeout); a
    positive value is used as-is, floored at child_timeout. It cannot be
    disabled. None when there is no child_timeout (then no wait ever
    returns TIMED_OUT_RUNNING, so there is nothing to bound).
    """
    if not child_timeout:
        return None
    floor = float(child_timeout)
    default = max(floor, _CHILD_TIMEOUT_FLOOR_S) * DEFAULT_CHILD_MAX_WALL_MULTIPLIER
    val = _load_config().get("child_max_wall_seconds")
    if val is None:
        return default
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        logger.warning(
            "delegation.child_max_wall_seconds=%r is not a valid number; "
            "using %sx child_timeout",
            val,
            DEFAULT_CHILD_MAX_WALL_MULTIPLIER,
        )
        return default
    if parsed < 0:
        logger.warning(
            "delegation.child_max_wall_seconds=%r cannot disable the ceiling; "
            "using %sx child_timeout",
            val,
            DEFAULT_CHILD_MAX_WALL_MULTIPLIER,
        )
        return default
    if parsed == 0:
        return default
    return max(floor, parsed)


# Default hung-child ceiling (delegation.hung_child_seconds). A hang is a
# liveness fault, not a budget, so it applies with child_timeout_seconds: 0.
# Measured reason (blackbox turns.db, 2026-06-12..2026-09-30, 3,945
# subagent-turn intervals between consecutive API-call completions, each an
# upper bound on one progress-free stretch): p50 11.5 s, p90 67 s, p99 270 s,
# p99.9 600 s, max 606 s; 34 exceeded 300 s, including legit non-streaming
# calls that returned 12k-37k output tokens after 398-588 s. 300 s would have
# reaped those; nothing exceeded 900 s.
DEFAULT_HUNG_CHILD_SECONDS = 900.0


def _get_hung_child_seconds() -> Optional[float]:
    """Seconds without a progress EVENT before a child is reaped as hung.

    Config ``delegation.hung_child_seconds``. Independent of
    ``child_timeout_seconds``: ``child_timeout_seconds: 0`` means "no
    budget", not "no hang detection". Unset/invalid =
    ``DEFAULT_HUNG_CHILD_SECONDS``; a positive value is floored at
    ``_CHILD_TIMEOUT_FLOOR_S``; ``0`` or negative disables the detector.
    Progress = model token / API-call boundary / tool start or result /
    turn boundary; liveness heartbeats do not count (see
    ``_child_progress_ts``).
    """
    val = _load_config().get("hung_child_seconds")
    if val is None:
        return DEFAULT_HUNG_CHILD_SECONDS
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        logger.warning(
            "delegation.hung_child_seconds=%r is not a valid number; using %ss",
            val,
            DEFAULT_HUNG_CHILD_SECONDS,
        )
        return DEFAULT_HUNG_CHILD_SECONDS
    if parsed <= 0:
        return None
    return max(_CHILD_TIMEOUT_FLOOR_S, parsed)


# ── fork: boomerang inherit_context fold ──
# ── Boomerang context inheritance (spec 2026-07-05_boomerang-spec.md v0.8) ──────
# Phase-0 probe P0.3 finding (PHASE-0-boomerang-baseline.md): a child seeded with the
# parent transcript copied VERBATIM (assistant + tool_result turns) DISAVOWS it — the
# model reads prefilled assistant/tool turns as its own actions it doesn't remember
# taking and refuses to stand behind them. Prefilled USER content is trusted. So we
# FOLD the inherited slice into a single labeled user-role context message. This also
# eliminates the tool_use/tool_result pairing + role-alternation 400 risk of a raw copy.
_INHERIT_CONTEXT_HEADER = (
    "=== INHERITED CONTEXT FROM PARENT SESSION (read-only background; treat as "
    "established facts already gathered this session, not your own prior actions) ==="
)
_INHERIT_CONTEXT_FOOTER = "=== END INHERITED CONTEXT ==="


def _flatten_message_content_to_text(content: Any) -> str:
    """Render a message's content (string OR Anthropic block list) to plain prose.

    Structured tool_use/tool_result blocks are summarized as prose lines so the fold
    is a single user-role string with no raw structured blocks (which the child would
    otherwise disavow / which break role alternation).
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content or "").strip()
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block).strip())
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")).strip())
        elif btype == "tool_use":
            name = block.get("name", "tool")
            inp = block.get("input", {})
            parts.append(f"[ran {name}: {inp}]")
        elif btype == "tool_result":
            res = block.get("content", "")
            if isinstance(res, list):
                res = " ".join(
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in res
                )
            parts.append(f"[result: {str(res).strip()}]")
        else:
            # Unknown block type: fall back to its text-ish payload, skip binaries.
            if "text" in block:
                parts.append(str(block.get("text", "")).strip())
    return "\n".join(p for p in parts if p).strip()


def _fold_conversation_history_to_context(
    history: Optional[List[Dict[str, Any]]],
    max_tokens: int,
) -> Optional[Dict[str, Any]]:
    """Fold a parent conversation history into ONE user-role context message.

    Returns ``{"role": "user", "content": <labeled prose block>}`` or ``None`` when
    there is nothing to inherit. Bounds the FOLDED TEXT (not a message array) to
    ~``max_tokens`` (≈4 chars/token), keeping the MOST RECENT turns and dropping the
    oldest first — the recent state is what the child needs. See P0.3.
    """
    if not history:
        return None
    # Render each turn newest-first as "Role: prose", accumulating under the char budget.
    char_budget = max(0, int(max_tokens)) * 4
    rendered: List[str] = []
    used = 0
    for msg in reversed(history):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip() or "unknown"
        text = _flatten_message_content_to_text(msg.get("content"))
        if not text:
            continue
        line = f"{role.capitalize()}: {text}"
        if used + len(line) > char_budget and rendered:
            break  # keep what we have (most-recent turns); drop older
        rendered.append(line)
        used += len(line) + 1
    if not rendered:
        return None
    rendered.reverse()  # restore chronological order for readability
    body = "\n".join(rendered)
    content = f"{_INHERIT_CONTEXT_HEADER}\n\n{body}\n\n{_INHERIT_CONTEXT_FOOTER}"
    return {"role": "user", "content": content}


# ── fork: child send-origin / cron-session rebind (cron approval gate reads ContextVar) ──
def _bind_child_send_origin(child):
    """Bind the routing-only send-origin for a subagent run from the parent
    origin captured on the child at spawn (``_blackbox_parent_platform/_chat_id``,
    plus ``_send_origin_thread_id``). Returns reset tokens for the ``finally``
    clear, or ``None`` when no parent origin is available (CLI/cron-spawned
    child) — in which case the child's bare sends fall to home, unchanged.

    Routing-only: sets dedicated ``_SEND_ORIGIN_*`` contextvars read ONLY by
    send_message's resolver. Does NOT touch ``HERMES_SESSION_PLATFORM``, so the
    child's approval/skills/TTS identity stays ``subagent`` (PRD v2 I5).
    """
    try:
        platform = (getattr(child, "_blackbox_parent_platform", "") or "").strip()
        chat_id = (getattr(child, "_blackbox_parent_chat_id", "") or "").strip()
        thread_id = (getattr(child, "_send_origin_thread_id", "") or "").strip()
        # `subagent` is the child's own identity, never a real channel — never
        # bind it as an origin (that's the un-chained grandchild case).
        if platform and platform.lower() != "subagent" and chat_id:
            from gateway.session_context import set_send_origin
            return set_send_origin(platform, chat_id, thread_id)
    except Exception as exc:
        logger.debug("send-origin bind skipped: %s", exc)
    return None


def _clear_child_send_origin(tokens):
    """Restore the send-origin contextvars after a child run. No-op when no
    origin was bound. Never raises."""
    if not tokens:
        return
    try:
        from gateway.session_context import clear_send_origin
        clear_send_origin(tokens)
    except Exception as exc:
        logger.debug("send-origin clear skipped: %s", exc)


def _bind_child_cron_session(child):
    """Re-bind the cron-session marker for a cron-spawned subagent run (B1).

    A cron job runs the agent with the ``HERMES_CRON_SESSION`` ContextVar set,
    but its subagents run in a bare ThreadPoolExecutor that does NOT inherit
    contextvars — so without this rebind the child would read cron=False and
    lose its approval deny-gating (auto-approving dangerous commands /
    execute_code that cron-mode would deny). Cron-ness was captured on the child
    at spawn (``_is_cron_child``) in the parent thread that held the ContextVar.

    Returns a reset token for the ``finally`` clear, or ``None`` when the parent
    was not a cron session (an interactive parent's child must NOT be marked
    cron). Never raises.
    """
    try:
        if getattr(child, "_is_cron_child", False):
            from gateway.session_context import set_cron_session
            return set_cron_session()
    except Exception as exc:
        logger.debug("cron-session bind skipped: %s", exc)
    return None


def _clear_child_cron_session(token):
    """Restore the cron-session marker after a child run. No-op when not bound.
    Never raises."""
    if token is None:
        return
    try:
        from gateway.session_context import clear_cron_session
        clear_cron_session(token)
    except Exception as exc:
        logger.debug("cron-session clear skipped: %s", exc)


# ── fork: late results + child lifecycle supervisor (#1535 #1542 #1585 f9d4df5a4d b18de09b6c) ──
_LATE_RESULT_MAX_CHARS = 8000

# Durable record of every TIMED_OUT_RUNNING child's late result. A steer into
# the parent is only a nudge: it is lost when the parent is idle, finished or
# interrupted (steer() refuses or raises). The record is written to
# cache/delegation/late/<late_result_id>.json and mirrored in memory, and
# delegate_task(action='list') returns it as ``late_results`` so the owning
# conversation can always read the result.
_late_results: Dict[str, Dict[str, Any]] = {}
_late_handles: Dict[str, "_LateCompletion"] = {}
_late_results_lock = threading.Lock()
_LATE_RESULTS_CAP = 64
_LATE_RESULT_RETENTION_SECONDS = 7 * 86400
_LATE_LIST_LIMIT = 20


class _LateCompletion:
    """Handle for one TIMED_OUT_RUNNING child's late-completion thread."""

    def __init__(
        self,
        late_result_id: str,
        *,
        absorbed: bool = False,
        late_dir: Any = None,
    ) -> None:
        self.late_result_id = late_result_id
        # Late-results dir of the OWNING profile, captured on the spawning
        # thread. The late thread persists here, never via a fresh
        # get_hermes_home() lookup (a secondary profile's home is a
        # context-local override, not the process env).
        self.late_dir = late_dir
        # True when an async batch/recovery unit joins this child and carries
        # its result in the unit's own completion event (no parent steer).
        self.absorbed = absorbed
        self.done = threading.Event()
        self.entry: Optional[Dict[str, Any]] = None
        self.result_path: Optional[str] = None


# Bounded wait for the live turn after a stop decision, before the result is
# persisted. Equals min(ceiling, 5) for every configurable child_timeout
# (floor _CHILD_TIMEOUT_FLOOR_S). Only delivery is bounded by it: persistence teardown waits
# for the turn to exit (docs/dev/delegate-child-lifecycle.md, I2).
_LATE_STOP_DRAIN_SECONDS = 5.0
# Children whose persistence teardown is waiting on a still-live turn.
_deferred_teardowns = 0
_deferred_teardowns_lock = threading.Lock()


def _count_deferred_teardown(delta: int) -> int:
    global _deferred_teardowns
    with _deferred_teardowns_lock:
        _deferred_teardowns += delta
        return _deferred_teardowns


class IllegalTransition(RuntimeError):
    """A child lifecycle event fired from a state that has no such edge."""


class _ChildLifecycle:
    """The late-path child lifecycle as an explicit state machine.

    Design of record: docs/dev/delegate-child-lifecycle.md. ``fire`` is the
    only way to change state. Accepted steer lives in the child's
    ``_SteerLedger`` (I1) and teardown goes through ``_teardown`` (I2); the
    machine only records where the late thread is and how many correction
    turns it started (I3: the count is written into the one record).
    """

    STATES = (
        "running",
        "timed_out_running",
        "correcting",
        "late_completed",
        "reaped",
        "persisted",
        "torn_down",
    )
    EVENTS = ("timeout", "correct", "finish", "stall", "persist", "amend", "teardown")
    EDGES = {
        ("running", "timeout"): "timed_out_running",
        ("timed_out_running", "correct"): "correcting",
        ("timed_out_running", "finish"): "late_completed",
        ("correcting", "finish"): "late_completed",
        ("timed_out_running", "stall"): "reaped",
        ("correcting", "stall"): "reaped",
        ("late_completed", "persist"): "persisted",
        ("reaped", "persist"): "persisted",
        ("persisted", "amend"): "persisted",
        ("persisted", "teardown"): "torn_down",
    }

    def __init__(
        self, *, subagent_id: Optional[str], child: Any, state: str = "running"
    ) -> None:
        self.subagent_id = subagent_id
        self.child = child
        self.state = state
        self.history: List[str] = [state]
        # The turn currently (or last) running off the late thread; None
        # when the last turn ran inline on the late thread itself.
        self.live: Any = None
        self.ledger = _steer_ledger_of(child) or _SteerLedger(None)
        self.corrections = 0
        self._lock = threading.RLock()

    def fire(self, event: str, *, live: Any = None) -> str:
        with self._lock:
            nxt = self.EDGES.get((self.state, event))
            if nxt is None:
                raise IllegalTransition(f"{self.state} --{event}--> (no such edge)")
            if event == "correct":
                self.live = live
                self.corrections += 1
            if event in ("finish", "stall") and self.subagent_id:
                # Linearization point: no steer is accepted after this.
                _close_subagent_steering(self.subagent_id, self.child)
            self.state = nxt
            self.history.append(nxt)
            return nxt

    @property
    def missed_steer(self) -> Optional[str]:
        return self.ledger.missed()


def _with_missed_steer(entry: Dict[str, Any], summary: str, pending: Optional[str]) -> None:
    """Report *pending* on *entry* as missed_steer, with a summary note.

    Idempotent over *summary* (the un-annotated text), so an amendment can
    re-derive both fields, including removing them.
    """
    entry.pop("missed_steer", None)
    entry["summary"] = summary or None
    if not (isinstance(pending, str) and pending.strip()):
        return
    entry["missed_steer"] = pending
    note = (
        "[steer did not land — the subagent finished before it could "
        f"be delivered: {pending}]"
    )
    entry["summary"] = f"{summary}\n\n{note}" if summary else note


def _late_results_dir():
    from hermes_constants import get_hermes_dir

    return get_hermes_dir("cache/delegation", "delegation_cache") / "late"


def _public_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe copy of a result entry without private ``_`` keys."""
    out = {k: v for k, v in entry.items() if not str(k).startswith("_")}
    try:
        json.dumps(out, ensure_ascii=False)
        return out
    except (TypeError, ValueError):
        return json.loads(json.dumps(out, ensure_ascii=False, default=str))


def _record_late_result(
    handle: "_LateCompletion",
    entry: Dict[str, Any],
    *,
    child: Any,
    parent_agent: Any,
) -> Optional[str]:
    """Persist a late result (file + in-memory mirror). Returns the file path."""
    owner_sid = str(getattr(child, "_parent_session_id", "") or "") or str(
        getattr(parent_agent, "session_id", "") or ""
    )
    record: Dict[str, Any] = {
        "late_result_id": handle.late_result_id,
        "owner_agent_session_id": owner_sid or None,
        "finished_at": time.time(),
        "absorbed_by_batch": handle.absorbed,
        "entry": _public_entry(entry),
    }
    path_str: Optional[str] = None
    try:
        d = handle.late_dir if handle.late_dir is not None else _late_results_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{handle.late_result_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        path_str = str(path)
        cutoff = time.time() - _LATE_RESULT_RETENTION_SECONDS
        for old in d.glob("*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        logger.warning(
            "delegate_task late result %s could not be written to disk; "
            "in-memory record only",
            handle.late_result_id,
            exc_info=True,
        )
    with _late_results_lock:
        _late_results[handle.late_result_id] = {
            **record,
            "result_path": path_str,
            "agent": child,
        }
        while len(_late_results) > _LATE_RESULTS_CAP:
            evicted = _late_results.pop(next(iter(_late_results)), None) or {}
            if not evicted.get("result_path"):
                # Memory was its only copy (the disk write failed): say so
                # loudly instead of dropping it silently.
                logger.error(
                    "delegate_task late result %s evicted with no durable "
                    "copy; entry=%s",
                    evicted.get("late_result_id"),
                    json.dumps(evicted.get("entry"), ensure_ascii=False, default=str)[:2000],
                )
    handle.result_path = path_str
    return path_str


def _owned_late_results(parent_agent: Any) -> List[Dict[str, Any]]:
    """Late results owned by *parent_agent*'s conversation (memory + disk)."""
    def _owned(rec: Dict[str, Any]) -> bool:
        probe = {
            "agent": rec.get("agent"),
            "owner_agent_session_id": rec.get("owner_agent_session_id"),
        }
        return _owns_subagent_record(probe, parent_agent)

    with _late_results_lock:
        snapshot = {k: dict(v) for k, v in _late_results.items()}
    # Ownership is decided BEFORE any limit: a global newest-N cut would hide
    # an owned result behind newer results of other conversations. The dir
    # is bounded by _LATE_RESULT_RETENTION_SECONDS pruning.
    records = {k: v for k, v in snapshot.items() if _owned(v)}
    try:
        files = list(_late_results_dir().glob("*.json"))
    except Exception:
        files = []
    for p in files:
        rid = p.stem
        if rid in snapshot:
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (isinstance(rec, dict) and isinstance(rec.get("entry"), dict)):
            continue
        if not _owned(rec):
            continue
        rec["result_path"] = str(p)
        records[rid] = rec
    out: List[Dict[str, Any]] = []
    for rid, rec in records.items():
        entry = rec.get("entry") or {}
        summary = entry.get("summary")
        if isinstance(summary, str) and len(summary) > _LATE_RESULT_MAX_CHARS:
            summary = summary[:_LATE_RESULT_MAX_CHARS] + " …[truncated; see result_path]"
        item: Dict[str, Any] = {
            "late_result_id": rid,
            "subagent_id": entry.get("subagent_id"),
            "task_index": entry.get("task_index"),
            "status": entry.get("status"),
            "summary": summary,
            "finished_at": rec.get("finished_at"),
            "result_path": rec.get("result_path"),
        }
        for k in (
            "error",
            "missed_steer",
            "steer_fate_unknown",
            "schema_valid",
            "schema_retries",
            "schema_errors",
        ):
            if k in entry:
                item[k] = entry[k]
        out.append(item)
    out.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
    return out[:_LATE_LIST_LIMIT]


def _activity_agents(child: Any) -> List[Any]:
    return [child] + [
        r.get("agent") for r in _live_subtree_records(child) if r.get("agent") is not None
    ]


def _child_progress_ts(summary: Dict[str, Any]) -> Any:
    """The agent's last progress EVENT time, not its last liveness tick.

    ``last_progress_event_ts`` is advanced by model tokens, API-call
    boundaries, tool start/result and turn boundaries, never by the wait /
    in-tool heartbeats that keep ``last_activity_ts`` fresh while one API
    call or one tool call blocks. Agents that do not report it (foreign
    shapes, older doubles) fall back to ``last_activity_ts``.
    """
    ts = summary.get("last_progress_event_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return ts
    return summary.get("last_activity_ts")


def _progress_signature(child: Any) -> Tuple[Any, ...]:
    """(api calls, current tool, progress ts) of the child and its live subtree."""
    sig = []
    for a in _activity_agents(child):
        try:
            s = a.get_activity_summary()
            sig.append(
                (id(a), s.get("api_call_count"), s.get("current_tool"), _child_progress_ts(s))
            )
        except Exception:
            sig.append((id(a), None, None, None))
    return tuple(sig)


def _subtree_idle_seconds(child: Any) -> float:
    """Seconds since the most recent progress event anywhere in the subtree.

    0.0 when no agent reports a numeric progress/activity timestamp (unknown
    is treated as active, never as hung).
    """
    idles = []
    now = time.time()
    for a in _activity_agents(child):
        try:
            ts = _child_progress_ts(a.get_activity_summary())
        except Exception:
            ts = None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            idles.append(max(0.0, now - float(ts)))
        else:
            return 0.0
    return min(idles) if idles else 0.0


def _timed_out_running_entry(
    *,
    task_index: int,
    child: Any,
    subagent_id: Optional[str],
    child_timeout: Optional[float],
    duration: float,
) -> Optional[Dict[str, Any]]:
    """Result entry for a wait that timed out while the child still works.

    Returns None (the timeout+diagnostic failure path) for a child that is
    not working: one that never reached its first LLM call and has no live
    descendants (#14726 wedge), or one whose whole subtree has shown no
    activity for child_timeout seconds (hung, e.g. blocked after its first
    API call). Only a child with recent activity is TIMED_OUT_RUNNING.
    """
    api_calls = 0
    try:
        api_calls = int(child.get_activity_summary().get("api_call_count", 0) or 0)
    except Exception:
        pass
    below = _live_subtree_records(child)
    if api_calls == 0 and not below:
        return None
    if child_timeout and _subtree_idle_seconds(child) >= float(child_timeout):
        logger.info(
            "delegate_task hung at timeout: subagent=%s idle>=%ss",
            subagent_id,
            child_timeout,
        )
        return None
    live = [
        {
            "subagent_id": subagent_id,
            "parent_id": getattr(child, "_parent_subagent_id", None),
            "live_transcript": getattr(child, "_live_transcript_path", None),
        }
    ] + [
        {
            "subagent_id": r.get("subagent_id"),
            "parent_id": r.get("parent_id"),
            "live_transcript": getattr(r.get("agent"), "_live_transcript_path", None),
        }
        for r in below
    ]
    live_ids = [e["subagent_id"] for e in live if e["subagent_id"]]
    logger.info(
        "delegate_task timed_out_running: subagent=%s live=%s timeout=%ss",
        subagent_id,
        ",".join(str(i) for i in live_ids),
        child_timeout,
    )
    return {
        "task_index": task_index,
        "status": TIMED_OUT_RUNNING,
        "summary": None,
        "exit_reason": TIMED_OUT_RUNNING,
        "api_calls": api_calls,
        "duration_seconds": duration,
        "timeout_seconds": child_timeout,
        "timed_out_after_seconds": duration,
        "subagent_id": subagent_id,
        "live_subagents": live,
        "control": {
            "list": {"action": "list"},
            "steer": {
                "action": "steer",
                "subagent_id": subagent_id,
                "message": "<course correction>",
            },
            "stop": {"action": "stop", "subagent_id": subagent_id},
        },
        "note": (
            f"Timed out ≠ dead: the wait hit {child_timeout}s but the subagent "
            "and the listed descendants are STILL RUNNING. Do NOT re-delegate "
            "the same task (that runs a second tree concurrently). When it "
            "finishes its result is recorded durably (late_result_path, and "
            "delegate_task(action='list') under late_results) and you are "
            "nudged; meanwhile use delegate_task(action='list'|'steer'|'stop') "
            "with the ids above. A subagent that stops making progress for "
            f"{child_timeout}s is stopped and reported as timeout."
        ),
        "_child_role": getattr(child, "_delegate_role", None),
    }


class _LateTurnStalled(Exception):
    """A supervised late-path turn hit the hang or wall ceiling."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "hung" | "wall"


class _ChildHung(TimeoutError):
    """The child made no progress event for delegation.hung_child_seconds."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"no progress for {seconds:g}s")
        self.seconds = seconds


def _wait_child_turn(
    future: Any,
    child: Any,
    child_timeout: Optional[float],
    hung_seconds: Optional[float],
) -> Any:
    """Wait for the child's first turn under the budget AND the hang ceiling.

    ``child_timeout`` is the budget (None = no budget): reaching it raises
    ``FuturesTimeoutError`` exactly like ``future.result(timeout=...)``.
    ``hung_seconds`` is the liveness bound, independent of the budget: no
    progress event anywhere in the subtree for that long raises
    ``_ChildHung``.
    """
    if not hung_seconds:
        return future.result(timeout=child_timeout)
    budget_deadline = (
        time.monotonic() + float(child_timeout) if child_timeout else None
    )
    outcome, raw = _supervise_child_future(
        future, child, float(hung_seconds), budget_deadline, from_now=True
    )
    if outcome == "done":
        return raw
    if outcome == "hung":
        raise _ChildHung(float(hung_seconds))
    raise FuturesTimeoutError()


def _supervise_child_future(
    future: Any,
    child: Any,
    ceiling: float,
    wall_deadline: Optional[float],
    *,
    from_now: bool = False,
) -> Tuple[str, Any]:
    """Wait for a child turn's future under progress + absolute-wall bounds.

    Returns ``("done", raw)``, ``("hung", None)`` (no progress anywhere in
    the child's subtree for *ceiling* seconds) or ``("wall", None)``
    (``time.monotonic()`` passed *wall_deadline*, progress or not). An
    exception the turn itself raised -- including a ``TimeoutError`` --
    re-raises here: a finished future is never polled again.

    *from_now* starts the hang clock at the call instead of back-dating it by
    the subtree's existing idle time (a just-submitted turn whose agent was
    built long ago, e.g. queued behind max_concurrent_children).
    """
    poll = max(0.05, min(5.0, ceiling / 4.0))
    last_sig = _progress_signature(child)
    last_progress = time.monotonic() - (
        0.0 if from_now else min(_subtree_idle_seconds(child), ceiling)
    )
    while True:
        wait = poll
        if wall_deadline is not None:
            remaining = wall_deadline - time.monotonic()
            if remaining <= 0 and not future.done():
                return "wall", None
            wait = max(0.01, min(poll, remaining))
        try:
            return "done", future.result(timeout=wait)
        except (FuturesTimeoutError, TimeoutError):
            if future.done():
                # Finished meanwhile, or finished by RAISING TimeoutError:
                # result() returns the value or re-raises the turn's own
                # exception for the caller's error path (a `continue` here
                # spun forever on a raised TimeoutError).
                return "done", future.result()
        now = time.monotonic()
        sig = _progress_signature(child)
        if sig != last_sig:
            last_sig, last_progress = sig, now
        elif now - last_progress >= ceiling:
            return "hung", None


def _apply_output_schema(
    child: Any,
    result: Dict[str, Any],
    *,
    task_index: int,
    child_task_id: Optional[str],
    stream_callback: Any,
    run_turn: Any = None,
) -> Tuple[Optional[bool], List[str], int]:
    """T1-24 structured-output validation + ONE bounded correction retry.

    Shared by the normal completion path and the late (timed_out_running)
    path so both enforce the same contract. Mutates *result* in place with
    the retry turn's answer/api_calls/messages. Returns
    ``(schema_valid, schema_errors, schema_retries)``; ``(None, [], 0)``
    when no schema was attached at dispatch.

    *run_turn(message)*, when given, runs the correction turn instead of a
    bare ``child.run_conversation`` (the late path supervises it); a
    ``_LateTurnStalled`` it raises propagates to the caller.
    """
    output_schema = getattr(child, "_delegate_output_schema", None)
    if not isinstance(output_schema, dict):
        return None, [], 0
    from tools.delegation_output_schema import (
        build_retry_message,
        validate_output,
    )

    first_text = result.get("final_response") or ""
    schema_valid, schema_errors = validate_output(first_text, output_schema)
    retries = 0
    if (
        not schema_valid
        and first_text.strip()
        and not result.get("interrupted", False)
    ):
        # Exactly one retry turn, carrying the validation errors verbatim
        # (no schema re-paste — the child already holds the contract).
        retries = 1
        retry_result = None
        try:
            if run_turn is not None:
                retry_result = run_turn(build_retry_message(schema_errors))
            else:
                with _inline_turn(child):
                    retry_result = child.run_conversation(
                        user_message=build_retry_message(schema_errors),
                        task_id=child_task_id,
                        stream_callback=stream_callback,
                    )
        except _LateTurnStalled:
            raise
        except Exception as retry_exc:
            logger.warning(
                "Subagent %d schema-retry turn failed: %s",
                task_index,
                retry_exc,
            )
        if isinstance(retry_result, dict):
            retry_text = retry_result.get("final_response") or ""
            if retry_text.strip():
                result["final_response"] = retry_text
            try:
                result["api_calls"] = int(result.get("api_calls", 0) or 0) + int(
                    retry_result.get("api_calls", 0) or 0
                )
            except (TypeError, ValueError):
                pass
            retry_messages = retry_result.get("messages")
            if isinstance(retry_messages, list) and isinstance(
                result.get("messages"), list
            ):
                result["messages"] = result["messages"] + retry_messages
            schema_valid, schema_errors = validate_output(retry_text, output_schema)
    return schema_valid, schema_errors, retries


def _start_late_completion(
    child_future: Any,
    *,
    child: Any,
    parent_agent: Any,
    task_index: int,
    subagent_id: Optional[str],
    child_start: float,
    child_progress_cb: Any,
    child_pool: Any,
    leased_cred_id: Any,
    child_timeout: Optional[float] = None,
    child_task_id: Optional[str] = None,
    stream_callback: Any = None,
    child_turn: Any = None,
) -> "_LateCompletion":
    """Own a TIMED_OUT_RUNNING child until it ends (or is stopped), then deliver.

    - Hang ceiling: the child and its live subtree must keep making progress
      (api calls / current tool / activity timestamp). No progress for
      child_timeout seconds → stop it, reap its subtree, report ``timeout``,
      release its lease.
    - Wall ceiling: progress-aware is not unbounded. Once the child has run
      ``delegation.child_max_wall_seconds`` (default 4x child_timeout) since
      its start it is stopped the same way, progress or not.
    - Same contract as the normal path: output_schema validation with one
      bounded retry, and any steer the child accepted but never consumed is
      reported as ``missed_steer``. The retry turn runs through *child_turn*
      (the spawn-time bindings) on a supervised worker under both ceilings.
    - Owning profile: the thread runs in a copy of the spawning thread's
      context and persists into the late dir captured here, so a secondary
      profile (context-local home override) keeps its own results.
    - Durable delivery: the result is recorded on disk + in memory (read via
      action='list' → late_results) BEFORE the parent steer, which is only a
      nudge. An async unit that joins this child (``absorbed``) carries the
      result in its own completion event instead of the steer.
    """
    import uuid as _uuid

    late_id = f"late-{subagent_id or task_index}-{_uuid.uuid4().hex[:8]}"
    try:
        late_dir = _late_results_dir()
    except Exception:
        logger.warning("delegate_task: late results dir unresolved", exc_info=True)
        late_dir = None
    handle = _LateCompletion(
        late_id,
        absorbed=getattr(child, "_delegate_join_late", False) is True,
        late_dir=late_dir,
    )
    with _late_results_lock:
        _late_handles[late_id] = handle
    ceiling = float(child_timeout) if child_timeout else None
    max_wall = _get_child_max_wall_seconds(ceiling)
    # The hang ceiling is the tighter of the budget's progress window and
    # the budget-independent delegation.hung_child_seconds; the wall cap
    # stays keyed on child_timeout.
    _hung_s = _get_hung_child_seconds()
    if ceiling and _hung_s:
        ceiling = min(ceiling, _hung_s)
    wall_deadline = child_start + max_wall if max_wall else None
    approval_cb = _get_subagent_approval_callback()
    # The explicit lifecycle (docs/dev/delegate-child-lifecycle.md). The
    # owner fires the entry edge; the first turn is the live turn.
    lc = _ChildLifecycle(subagent_id=subagent_id, child=child)
    lc.live = child_future
    lc.fire("timeout")

    def _supervised_turn(message: str) -> Any:
        """One follow-up child turn under the late path's hang/wall bounds."""
        from tools.daemon_pool import DaemonThreadPoolExecutor

        executor = DaemonThreadPoolExecutor(
            max_workers=1,
            initializer=_set_subagent_approval_cb,
            initargs=(approval_cb,),
        )
        try:
            if child_turn is not None:
                call, args = child_turn, (message,)
            else:
                def call(msg: str) -> Any:
                    return child.run_conversation(
                        user_message=msg,
                        task_id=child_task_id,
                        stream_callback=stream_callback,
                    )
                args = (message,)
            fut = _submit_turn(executor, child, contextvars.copy_context().run, call, *args)
            lc.fire("correct", live=fut)
            outcome, raw = _supervise_child_future(
                fut, child, float(ceiling or 0.0), wall_deadline
            )
            if outcome != "done":
                raise _LateTurnStalled(outcome)
            return raw
        finally:
            executor.shutdown(wait=False)

    def _correction_turn(message: str) -> Any:
        if ceiling:
            return _supervised_turn(message)
        # No ceiling: the turn runs inline on the late thread.
        lc.fire("correct", live=None)
        with _inline_turn(child):
            if child_turn is not None:
                return child_turn(message)
            return child.run_conversation(
                user_message=message,
                task_id=child_task_id,
                stream_callback=stream_callback,
            )

    def _late() -> None:
        error: Optional[str] = None
        result: Dict[str, Any] = {}
        stop: Optional[str] = None  # "hung" | "wall" | "retry_hung" | "retry_wall"
        turn_outlived_drain = False
        try:
            if ceiling is None:
                raw = child_future.result()
            else:
                outcome, raw = _supervise_child_future(
                    child_future, child, ceiling, wall_deadline
                )
                if outcome != "done":
                    stop = outcome
            result = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        schema_valid: Optional[bool] = None
        schema_errors: List[str] = []
        schema_retries = 0
        if stop is None and error is None:
            try:
                schema_valid, schema_errors, schema_retries = _apply_output_schema(
                    child,
                    result,
                    task_index=task_index,
                    child_task_id=child_task_id,
                    stream_callback=stream_callback,
                    run_turn=_correction_turn,
                )
            except _LateTurnStalled as stalled:
                stop = f"retry_{stalled.reason}"
            except Exception:
                logger.warning("late schema validation failed", exc_info=True)
        # I3: the attempt count is the machine's, not a local's.
        schema_retries = lc.corrections
        hung = stop is not None

        if hung:
            # Alive but stuck, or past the wall ceiling: bound it exactly like
            # the wait-time failure.
            if stop and stop.endswith("wall"):
                why = (
                    f"past the {max_wall}s wall ceiling "
                    "(delegation.child_max_wall_seconds)"
                )
            else:
                why = f"no progress for {ceiling}s"
            lc.fire("stall")  # closes steering
            try:
                if not request_hard_interrupt(
                    child, f"Subagent stopped: {why}"
                ) and hasattr(child, "_interrupt_requested"):
                    child._interrupt_requested = True
            except Exception:
                pass
            _reap_subtree(child, "hung")
            live = lc.live
            if live is not None:
                # Bounded drain of the turn that is actually running, so a
                # turn that unwinds promptly is reported settled.
                try:
                    live.result(timeout=_LATE_STOP_DRAIN_SECONDS)
                except BaseException:
                    pass
                # Still live: it may yet deliver a ledgered steer, so the
                # record can over-report missed_steer until it exits; the
                # exit callback then amends it.
                turn_outlived_drain = not live.done()
            api_calls = 0
            try:
                api_calls = int(child.get_activity_summary().get("api_call_count", 0) or 0)
            except Exception:
                pass
            if stop and stop.startswith("retry_"):
                error = (
                    f"Subagent schema-correction turn stalled ({why}) after it "
                    f"was reported {TIMED_OUT_RUNNING}; it was stopped and its "
                    "credential lease released. The first answer is kept."
                )
                # Keep the answer the child did produce; only the correction
                # turn is lost.
                result = {
                    "api_calls": api_calls,
                    "final_response": result.get("final_response"),
                }
            else:
                error = (
                    f"Subagent {'hung' if stop == 'hung' else 'stopped'}: {why} "
                    f"after it was reported {TIMED_OUT_RUNNING}; it was stopped "
                    "and its credential lease released."
                )
                result = {"api_calls": api_calls}
            logger.warning(
                "delegate_task late child stopped: subagent=%s reason=%s "
                "ceiling=%ss wall=%ss",
                subagent_id,
                stop,
                ceiling,
                max_wall,
            )
        else:
            lc.fire("finish")  # closes steering
        pending_steer = lc.missed_steer
        summary = str(result.get("final_response") or "")
        if hung:
            status, exit_reason = "timeout", "timeout"
        elif error:
            status, exit_reason = "error", "error"
        else:
            status, exit_reason = _classify_child_outcome(result)
        _model = getattr(child, "model", None)
        _cost = getattr(child, "session_estimated_cost_usd", 0.0)
        _cost = float(_cost) if isinstance(_cost, (int, float)) else 0.0
        entry: Dict[str, Any] = {
            "task_index": task_index,
            "subagent_id": subagent_id,
            "status": status,
            "summary": summary or None,
            "api_calls": result.get("api_calls", 0),
            "duration_seconds": round(time.monotonic() - child_start, 2),
            "model": _model if isinstance(_model, str) else None,
            "exit_reason": exit_reason,
            "truncated": exit_reason == "max_iterations",
            "after": TIMED_OUT_RUNNING,
            "late_result_id": late_id,
            "cost_usd": round(_cost, 6),
            "_child_role": getattr(child, "_delegate_role", None),
            "_child_cost_usd": _cost,
        }
        if hung:
            entry["timeout_seconds"] = ceiling
            entry["timeout_phase"] = {
                "wall": "wall_ceiling_after_timed_out_running",
                "retry_hung": "schema_retry_hung_after_timed_out_running",
                "retry_wall": "wall_ceiling_after_timed_out_running",
            }.get(stop or "", "hung_after_timed_out_running")
            if stop and stop.endswith("wall"):
                entry["max_wall_seconds"] = max_wall
            if turn_outlived_drain:
                entry["steer_fate_unknown"] = True
        if error:
            entry["error"] = error
        elif status == "failed":
            entry["error"] = result.get("error", "Subagent did not produce a response.")
        if not hung and (error or status == "failed"):
            # A genuine failure of this child, raised or returned: reap
            # whatever it spawned.
            _reap_subtree(child, "error")
        if not hung and isinstance(getattr(child, "_delegate_output_schema", None), dict):
            entry["schema_valid"] = bool(schema_valid)
            if schema_retries:
                entry["schema_retries"] = schema_retries
            if not schema_valid and schema_errors:
                entry["schema_errors"] = schema_errors
        _with_missed_steer(entry, summary, pending_steer)

        if child_progress_cb:
            try:
                child_progress_cb(
                    "subagent.complete",
                    preview=(summary or error or "")[:160],
                    status=status,
                    duration_seconds=entry["duration_seconds"],
                    summary=(summary or error or "")[:500],
                )
            except Exception as exc:
                logger.debug("late subagent.complete relay failed: %s", exc)
        if not handle.absorbed:
            # An absorbing async unit finalizes transcript + manifest itself.
            writer = getattr(child, "_live_writer", None)
            if writer is not None:
                try:
                    writer.finalize(entry)
                except Exception:
                    logger.debug("late live transcript finalize failed", exc_info=True)
            try:
                from tools.delegation_live_log import update_manifest_statuses

                deleg_id = getattr(child, "_delegation_id", None)
                if isinstance(deleg_id, str):
                    update_manifest_statuses(deleg_id, [entry])
            except Exception:
                logger.debug("late manifest update failed", exc_info=True)
        try:
            # Lease, registry and parent link go at the stop decision (#1535);
            # they hold no transcript data. Persistence teardown is below.
            _release_child_handles(
                child, parent_agent, subagent_id, child_pool, leased_cred_id
            )
        except Exception:
            logger.debug("late child handle release failed", exc_info=True)

        # Durable first: the steer below is only a nudge and can be lost.
        path = _record_late_result(handle, entry, child=child, parent_agent=parent_agent)
        lc.fire("persist")
        handle.entry = entry
        handle.done.set()
        if not handle.absorbed:
            with _late_results_lock:
                _late_handles.pop(late_id, None)

        delivered = False
        if not handle.absorbed:
            body = entry.get("summary") or error or "(no final response)"
            if len(body) > _LATE_RESULT_MAX_CHARS:
                body = body[:_LATE_RESULT_MAX_CHARS] + " …[truncated; see result file]"
            transcript = getattr(child, "_live_transcript_path", None)
            text = (
                f"[delegate_task late result] subagent {subagent_id} (task "
                f"{task_index}), earlier reported {TIMED_OUT_RUNNING}, has "
                f"finished: status={status}."
                + (f" Result: {path}." if path else "")
                + (f" Transcript: {transcript}." if transcript else "")
                + f"\n{body}"
            )
            steer = getattr(parent_agent, "steer", None)
            if callable(steer):
                try:
                    delivered = bool(steer(text))
                except Exception as exc:
                    logger.debug("late result steer into parent failed: %s", exc)
        logger.info(
            "delegate_task late completion: subagent=%s status=%s recorded=%s "
            "nudged=%s absorbed=%s",
            subagent_id,
            status,
            path or "memory-only",
            delivered,
            handle.absorbed,
        )

        # Post-persist (I1 + I2). Teardown goes through the one door, which
        # closes now or, while a turn of this child is live, when it exits.
        # A record written while the turn was live is re-derived from the
        # ledger once it exits: the fate of every accepted steer is then
        # known, whether the turn returned or raised.
        live = lc.live
        exit_ctx = contextvars.copy_context()

        def _settle(_fut: Any) -> None:
            try:
                if entry.get("steer_fate_unknown"):
                    lc.fire("amend")
                    entry.pop("steer_fate_unknown", None)
                    before = entry.get("missed_steer")
                    _with_missed_steer(entry, summary, lc.missed_steer)
                    _record_late_result(handle, entry, child=child, parent_agent=parent_agent)
                    handle.entry = entry
                    if entry.get("missed_steer") != before:
                        logger.info(
                            "delegate_task late result %s amended: the turn delivered "
                            "steer after the result was recorded",
                            late_id,
                        )
            except Exception:
                logger.warning("late steer amendment failed", exc_info=True)
            finally:
                try:
                    lc.fire("teardown")
                except IllegalTransition:
                    logger.warning("late child teardown out of order", exc_info=True)

        _teardown(child, "late_persisted", owner=True)
        if live is None:
            exit_ctx.copy().run(_settle, None)
        else:
            # A fresh copy per run: this callback may run right here (turn
            # already done) or on the worker thread, never in a context
            # that is already entered.
            live.add_done_callback(lambda f: exit_ctx.copy().run(_settle, f))


    # A bare Thread starts with an EMPTY context: run _late in a copy of the
    # spawning thread's context so profile-scoped state (the context-local
    # home override of a secondary profile, session vars) is the owner's for
    # teardown, manifest updates and the correction turn.
    owner_context = contextvars.copy_context()
    t = threading.Thread(
        target=owner_context.run,
        args=(_late,),
        name=f"delegate-late-{subagent_id or task_index}",
        daemon=True,
    )
    t.start()
    return handle


def _join_late_result(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a TIMED_OUT_RUNNING entry with the child's real late result.

    Used by async/recovery units, which must not finalize while a child is
    still live. Bounded: the late thread's hang ceiling ends a stuck child.
    """
    if entry.get("status") != TIMED_OUT_RUNNING:
        return entry
    with _late_results_lock:
        handle = _late_handles.get(str(entry.get("late_result_id") or ""))
    if handle is None:
        logger.warning(
            "delegate_task: no late-completion handle for %s; unit carries "
            "timed_out_running", entry.get("subagent_id"),
        )
        return entry
    handle.done.wait()
    with _late_results_lock:
        _late_handles.pop(handle.late_result_id, None)
    late = dict(handle.entry or {})
    late["task_index"] = entry.get("task_index", late.get("task_index"))
    return late


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    override_request_overrides: Optional[Dict[str, Any]] = None,
    # Tier keys (service_tier/speed) the user set explicitly in
    # delegation.request_overrides; kept verbatim through the inherit-branch
    # tier re-gate below.
    explicit_tier_overrides: Optional[Dict[str, Any]] = None,
    # Legacy (fork callers still pass it): upstream removed the dedicated
    # per-child output cap, the child inherits the route's own max_tokens.
    override_max_tokens: Optional[int] = None,

    # ACP transport overrides from trusted delegation config.
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # Configuration block that owns the selected provider/model route. Internal
    # callers such as /review pass auxiliary.review here so fallback policy is
    # not accidentally read from the general delegation block.
    routing_cfg: Optional[Dict[str, Any]] = None,
    # Orchestrator is explicit opt-in (fork #1541): a child is a LEAF unless the
    # caller passed role='orchestrator'; depth/kill switch only bound it.
    role: str = "leaf",
    # Boomerang: fold the parent's conversation history into a single user-role
    # context message and seed the child with it (spec v0.8 D-4 / P0.3). Default
    # False keeps every existing caller byte-identical (INV-5).
    inherit_context: bool = False,
    # Recovery reuses the exact fold captured before the original launch.
    materialized_prefill_messages: Optional[List[Dict[str, Any]]] = None,
    recovery_max_spawn_depth: Optional[int] = None,
    recovery_orchestrator_enabled: Optional[bool] = None,
    # Per-task skill promotion: names re-promoted to full descriptions in the
    # child's compact skills index (see agent/system_prompt.py). None/empty =
    # pure names-only index.
    skills: Optional[List[str]] = None,
):
    """Build (don't run) a child AIAgent on the main thread. override_* (from delegation config) replace parent
    inheritance so children can run on a different provider:model pair."""
    import uuid as _uuid
    from run_agent import AIAgent
    from agent.delegation_context import delegated_child_context
    # Orchestrator is explicit opt-in: a child is a LEAF unless the caller
    # passed role='orchestrator'. max_spawn_depth is only the ceiling and
    # orchestrator_enabled the kill switch. (Depth-derived promotion made
    # every child below the ceiling an orchestrator whether or not anyone
    # asked — the 2026-09-08 max_spawn_depth=5 fan-out burst.)
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = recovery_max_spawn_depth or _get_max_spawn_depth()
    orchestrator_enabled = (
        recovery_orchestrator_enabled
        if recovery_orchestrator_enabled is not None
        else _get_orchestrator_enabled()
    )
    requested_role = _normalize_role(role)
    effective_role = "leaf"
    if requested_role == "orchestrator":
        if not orchestrator_enabled:
            logger.info(
                "delegate_task: role='orchestrator' forced to leaf "
                "(delegation.orchestrator_enabled=false)"
            )
        elif child_depth >= max_spawn:
            logger.warning(
                "delegate_task: role='orchestrator' requested at depth=%d, "
                "the max_spawn_depth=%d floor; downgraded to leaf",
                child_depth, max_spawn,
            )
        else:
            effective_role = "orchestrator"

    # One subagent_id shared by the progress callback, spawn_requested event and
    # the live registry; parent_id is set when THIS parent is itself a subagent.
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)
    logger.info(
        "delegate_task spawn id=%s depth=%d role=%s (requested=%s, max_spawn_depth=%d)",
        subagent_id, child_depth, effective_role, requested_role, max_spawn,
    )

    # General delegation behavior (reasoning, compression, capabilities) stays
    # global. Only fallback policy follows the owner of a per-call route such
    # as auxiliary.review.
    delegation_cfg = _load_config()
    child_toolsets, child_disabled_toolsets = _resolve_child_toolsets(parent_agent, toolsets, effective_role)
    child_prompt = _build_child_system_prompt(
        goal, context, workspace_path=_resolve_workspace_hint(parent_agent), role=effective_role,
        max_spawn_depth=max_spawn, child_depth=child_depth,
    )
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Shared ref: session_id once the child exists, delegation_id once
    # delegate_task stamps it — both ride on every relayed event.
    child_session_ref: Dict[str, Any] = {}
    child_progress_cb = _build_child_progress_callback(
        task_index, goal, parent_agent, task_count, subagent_id=subagent_id, parent_id=parent_subagent_id,
        depth=max(0, child_depth - 1),  # 0 = first-level child for the UI
        model=model or getattr(parent_agent, "model", None), toolsets=child_toolsets, session_ref=child_session_ref,
    )
    rt = _resolve_child_runtime(
        parent_agent, delegation_cfg, parent_api_key, model=model, override_provider=override_provider,
        override_base_url=override_base_url, override_api_key=override_api_key, override_api_mode=override_api_mode,
        override_acp_command=override_acp_command,
        override_acp_args=override_acp_args,
        routing_cfg=routing_cfg,
    )
    if override_request_overrides is not None:
        # honored whenever set, incl. the inherit branch where
        # _resolve_delegation_credentials already merged OVER the parent's
        request_overrides = dict(override_request_overrides)
    else:
        request_overrides = {} if override_provider else dict(getattr(parent_agent, "request_overrides", {}) or {})
    parent_sid = getattr(parent_agent, "session_id", None)
    # ── Boomerang context inheritance (spec v0.8 D-4 / P0.3) ────────────
    # When inherit_context is set, fold the PARENT's conversation history into
    # ONE user-role context message and seed the child with it. Verbatim copy of
    # assistant/tool turns is DISAVOWED by the child (P0.3) — the fold is the fix.
    # Default path (inherit_context False) is byte-identical: keep the historical
    # boot-attr forward (INV-5).
    child_prefill_messages: Any = getattr(parent_agent, "prefill_messages", None)
    if materialized_prefill_messages is not None:
        child_prefill_messages = copy.deepcopy(materialized_prefill_messages)
    elif inherit_context:
        try:
            _boom = (delegation_cfg.get("boomerang") or {}) if isinstance(delegation_cfg, dict) else {}
            _max_tokens = int(_boom.get("inherit_max_tokens", 50000) or 50000)
            # Clamp against the child model's actual context window at delegation time.
            try:
                from agent.model_metadata import get_model_context_length
                _win = get_model_context_length(rt.get("model"))
                if _win and _win > 0:
                    _max_tokens = min(_max_tokens, int(_win * 0.25))
            except Exception:
                pass
            # Read the parent's LIVE transcript. The gateway populates
            # `_session_messages` (agent_init.py + conversation_loop.py); the CLI
            # console uses `conversation_history`. A gateway AIAgent has NO
            # `conversation_history` attr, so reading only that made the fold empty
            # in production (the E2E "INHERITED: no" bug). Prefer the gateway
            # source, fall back to the CLI one. Discriminate PRESENT-BUT-EMPTY from
            # ABSENT with `is None`: `_fold_...([], ...)` already returns None.
            _parent_history = getattr(parent_agent, "_session_messages", None)
            if _parent_history is None:
                _parent_history = getattr(parent_agent, "conversation_history", None)
            _folded = _fold_conversation_history_to_context(_parent_history, _max_tokens)
            if _folded is not None:
                child_prefill_messages = [_folded]
        except Exception as _inh_exc:
            logger.warning("boomerang inherit_context fold failed (continuing without): %s", _inh_exc)
    child_session_db = _open_child_session_db(parent_agent)
    with delegated_child_context():
        try:
            child = AIAgent(
                **rt, max_iterations=max_iterations, prefill_messages=child_prefill_messages,
                enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled_toolsets, quiet_mode=True,
                ephemeral_system_prompt=child_prompt, log_prefix=f"[subagent-{task_index}]", platform="subagent",
                side_agent=True,
                skip_context_files=True, skip_memory=True, clarify_callback=None,
                thinking_callback=(
                    (lambda text: _safe_progress(child_progress_cb, "_thinking", text) if text else None)
                    if child_progress_cb else None
                ),
                session_db=child_session_db, parent_session_id=parent_sid, request_overrides=request_overrides,
                tool_progress_callback=child_progress_cb,
                iteration_budget=None,  # fresh budget per subagent
            )
        except BaseException:
            # No child close() will ever run: release the dedicated handle here.
            if child_session_db is not None:
                with _quiet(None):
                    from hermes_state_registry import release_or_close
                    release_or_close(child_session_db)
            raise
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    _apply_child_cache_ttl(child)
    if not override_provider:
        _regate_inherited_child_tier(child, parent_agent, explicit_tier_overrides)
    # Per-task skill promotion (see agent/system_prompt.py): names the brief
    # wants re-promoted to full descriptions in the child's compact index.
    # Set BEFORE the first request — the system prompt is built lazily.
    child._delegate_skills = tuple(
        s.strip() for s in (skills or []) if isinstance(s, str) and s.strip()
    )
    if child_session_db is not None:
        child._owns_session_db = True  # released by the child's close(), never by the parent
    # Ownership transfer for the dedicated handle: the child's close() must release it (nothing else holds a
    # reference), and no parent teardown can close it out from under a background child (#81267).
    child_session_ref["session_id"] = getattr(child, "session_id", "") or ""
    # Lineage line: lets log consumers (web-call-tripwire) rebuild the
    # delegation tree from agent.log without reading state.db. Field order and
    # key=value spelling are a parsing contract; values must carry no spaces.
    logger.info(
        "delegate_task child id=%s session=%s parent_session=%s depth=%d",
        str(subagent_id).replace(" ", "_") or "-",
        str(child_session_ref["session_id"]).replace(" ", "_") or "-",
        str(parent_sid or "").replace(" ", "_") or "-",
        child_depth,
    )
    child._progress_identity_ref = child_session_ref
    child._delegate_depth, child._delegate_role = child_depth, effective_role  # post-degrade role
    child._subagent_id, child._parent_subagent_id = subagent_id, parent_subagent_id
    _apply_child_compression_cap(child, delegation_cfg)
    # Ownership chain for action=list/steer/stop; weakref so a finished parent
    # can be collected while a detached child record lingers in the registry.
    try:
        child._delegate_parent_ref = weakref.ref(parent_agent)
    except TypeError:
        child._delegate_parent_ref = None  # non-weakref-able test doubles
    # Sidebar marker: subagent sessions stay out of session pickers even when a
    # parent delete orphans them (mirrors /branch's ``_branched_from``).
    if parent_sid and getattr(child, "_session_init_model_config", None) is not None:
        child._session_init_model_config["_delegate_from"] = parent_sid
    # Shared pool lets children rotate credentials on rate limits.
    child_pool = _resolve_child_credential_pool(
        rt["provider"], parent_agent, rt["base_url"], effective_requested_provider=rt.get("requested_provider"),
    )
    if child_pool is not None:
        child._credential_pool = child_pool

    # I2 (fork #1595): the door goes on BEFORE the parent can see the child, so a parent
    # close/release_clients from here on is routed to _teardown. A close that wins before
    # the run takes its hold marks the slot closed, and _hold_run then returns False.
    _attach_owner_teardown(child)
    _attach_child(parent_agent, child)  # interrupt propagation
    # spawn_requested now — the child may queue for seconds when the pool is
    # saturated — then the subagent_start lifecycle hook.
    _safe_progress(child_progress_cb, "subagent.spawn_requested", preview=goal)
    with _quiet("subagent_start hook invocation failed", exc_info=True):
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "subagent_start", parent_session_id=parent_sid,
            parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "", parent_subagent_id=parent_subagent_id,
            child_session_id=getattr(child, "session_id", None), child_subagent_id=subagent_id,
            child_role=effective_role, child_goal=goal,
        )

    # Blackbox subagent attribution (decision #3): a subagent is recorded as
    # its OWN turn but attributed to the parent agent + parent session channel.
    # contextvars do NOT cross the ThreadPoolExecutor boundary that runs the
    # child (tools/delegate_tool.py), so capture the parent's channel identity
    # HERE — in the parent thread, before submit() — and stash it on the child.
    # The blackbox plugin reads these off the child agent in its on_session_end
    # handler. Best-effort: never let attribution break delegation.
    try:
        from gateway.session_context import get_session_env as _gse
        # Grandchild chaining (PRD v2 §5.5): when this spawn happens INSIDE a
        # subagent run (an orchestrator child spawning a grandchild), the
        # HERMES_SESSION_* vars are unset (the child runs with its own
        # `platform="subagent"` identity), so fall back to the routing-only
        # send-origin the parent child is holding — that carries the REAL
        # grandparent channel, making origin transitive through any nesting.
        try:
            from gateway.session_context import get_send_origin as _gso
            _so_plat, _so_chat, _so_thread = _gso()
        except Exception:
            _so_plat = _so_chat = _so_thread = ""
        child._blackbox_is_subagent = True
        child._blackbox_parent_turn_id = _gse("HERMES_SESSION_KEY", "") or getattr(
            parent_agent, "session_id", None)
        child._blackbox_parent_platform = (
            _gse("HERMES_SESSION_PLATFORM", "") or _so_plat
            or getattr(parent_agent, "platform", "") or "")
        child._blackbox_parent_chat_id = _gse("HERMES_SESSION_CHAT_ID", "") or _so_chat or ""
        child._blackbox_parent_chat_name = _gse("HERMES_SESSION_CHAT_NAME", "") or ""
        # Depth tracking (B1 fix): child depth = parent depth + 1. Use getattr
        # with None sentinel (B2) to detect missing parent depth and log warning.
        parent_depth = getattr(parent_agent, "_blackbox_depth", None)
        if parent_depth is not None:
            child._blackbox_depth = parent_depth + 1
        else:
            # Old code / parent missing depth attribute → assume direct child
            child._blackbox_depth = 1
            logger.warning(
                "blackbox depth fallback: parent agent has no _blackbox_depth, "
                "assuming child is depth 1 (direct child)"
            )
        # Routing-only thread id for the child's bare sends (kept separate from
        # the blackbox attribution fields, which don't carry a thread).
        child._send_origin_thread_id = _gse("HERMES_SESSION_THREAD_ID", "") or _so_thread or ""
        # Cron-session marker capture (B1): cron jobs can spawn subagents, but
        # the child runs in a bare ThreadPoolExecutor that does NOT inherit
        # contextvars — so without an explicit rebind a cron subagent would read
        # cron=False and LOSE its approval deny-gating (auto-approving dangerous
        # commands / execute_code). Capture cron-ness HERE, in the parent thread
        # that still holds the cron ContextVar, and re-bind it in the child-run
        # wrapper (_run_with_thread_capture). Mirrors the send-origin rebind.
        try:
            from gateway.session_context import is_cron_session as _ics
            child._is_cron_child = _ics()
        except Exception:
            child._is_cron_child = False
    except Exception as _bb_exc:
        logger.debug("blackbox subagent attribution skipped: %s", _bb_exc)

    return child


# ── fork: _run_single_child supervisor (whole-function; upstream's _ChildRun path stays importable in
#    tools/delegate_tool_child_run.py — see ledger R07-delegate POLICY-DIVERGENCE) ──
def _run_single_child(
    task_index: int,
    goal: str,
    child=None,
    parent_agent=None,
    *,
    owner_session_id: Optional[str] = None,
    owner_transport: Any = None,
    owner_session_record: Any = None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Run a pre-built child agent. Called from within a thread.
    Returns a structured result dict.
    """
    child_start = time.monotonic()

    # Get the progress callback from the child agent
    child_progress_cb = getattr(child, "tool_progress_callback", None)

    # Restore parent tool names using the value saved before child construction
    # mutated the global. This is the correct parent toolset, not the child's.
    import model_tools

    _saved_tool_names = getattr(
        child, "_delegate_saved_tool_names", list(model_tools._last_resolved_tool_names)
    )

    child_pool = getattr(child, "_credential_pool", None)
    leased_cred_id = None
    if child_pool is not None:
        leased_cred_id = child_pool.acquire_lease()
        if leased_cred_id is not None:
            try:
                leased_entry = child_pool.current()
                if leased_entry is not None and hasattr(child, "_swap_credential"):
                    outcome = child._swap_credential(leased_entry)
                    # ``_swap_credential`` refuses to install a keyless client and
                    # returns a non-SWAPPED SwapOutcome instead of raising. If the
                    # leased entry had no usable key the child keeps its own prior
                    # credential — surface that explicitly rather than silently
                    # running delegated work under stale/unexpected credential state.
                    from run_agent import SwapOutcome
                    if outcome is not None and outcome != SwapOutcome.SWAPPED:
                        logger.warning(
                            "Child not bound to leased credential %s: entry has no "
                            "usable key (%s); child retains its own credential.",
                            leased_cred_id,
                            getattr(outcome, "value", outcome),
                        )
                        child_pool.release_lease(leased_cred_id)
                        leased_cred_id = None
            except Exception as exc:
                logger.debug("Failed to bind child to leased credential: %s", exc)

    # Heartbeat: periodically propagate child activity to the parent so the
    # gateway inactivity timeout doesn't fire while the subagent is working.
    # Without this, the parent's _last_activity_ts freezes when delegate_task
    # starts and the gateway eventually kills the agent for "no activity".
    _heartbeat_stop = threading.Event()
    # Stale detection: track the child's (tool, iteration, activity_ts) across
    # heartbeat cycles. If none advances, count the cycle as stale.
    # Different thresholds for idle vs in-tool (see _HEARTBEAT_STALE_CYCLES_*).
    # last_activity_ts is the same liveness signal the async stall monitor
    # already uses (streamed chunks + direct_api_call mid-wait heartbeats).
    _last_seen_iter = [0]
    _last_seen_tool = [None]  # type: list
    _last_seen_activity_ts = [None]  # type: list
    _stale_count = [0]

    def _heartbeat_loop():
        while not _heartbeat_stop.wait(_HEARTBEAT_INTERVAL):
            if parent_agent is None:
                continue
            touch = getattr(parent_agent, "_touch_activity", None)
            if not touch:
                continue
            # Pull detail from the child's own activity tracker
            desc = f"delegate_task: subagent {task_index} working"
            try:
                child_summary = child.get_activity_summary()
                child_tool = child_summary.get("current_tool")
                child_iter = child_summary.get("api_call_count", 0)
                child_max = child_summary.get("max_iterations", 0)
                child_activity_ts = child_summary.get("last_activity_ts")

                # Stale detection: count cycles where iteration, current_tool,
                # AND last_activity_ts are all frozen. A child running a
                # legitimately long-running tool keeps current_tool set; a
                # child waiting on a slow model refreshes last_activity_ts
                # via direct_api_call's activity heartbeat — neither should
                # look stale at the idle threshold.
                iter_advanced = child_iter > _last_seen_iter[0]
                tool_changed = child_tool != _last_seen_tool[0]
                activity_advanced = (
                    child_activity_ts is not None
                    and (
                        _last_seen_activity_ts[0] is None
                        or child_activity_ts > _last_seen_activity_ts[0]
                    )
                )
                if iter_advanced or tool_changed or activity_advanced:
                    _last_seen_iter[0] = child_iter
                    _last_seen_tool[0] = child_tool
                    if child_activity_ts is not None:
                        _last_seen_activity_ts[0] = child_activity_ts
                    _stale_count[0] = 0
                else:
                    _stale_count[0] += 1

                # Pick threshold based on whether the child is currently
                # inside a tool call. In-tool threshold is high enough to
                # cover legitimately slow tools; idle threshold stays
                # tight so the gateway timeout can fire on a truly wedged
                # child.
                stale_limit = (
                    _HEARTBEAT_STALE_CYCLES_IN_TOOL
                    if child_tool
                    else _HEARTBEAT_STALE_CYCLES_IDLE
                )
                if _stale_count[0] >= stale_limit:
                    logger.warning(
                        "Subagent %d appears stale (no progress for %d "
                        "heartbeat cycles, tool=%s) — stopping heartbeat",
                        task_index,
                        _stale_count[0],
                        child_tool or "<none>",
                    )
                    break  # stop touching parent, let gateway timeout fire

                if child_tool:
                    desc = (
                        f"delegate_task: subagent running {child_tool} "
                        f"(iteration {child_iter}/{child_max})"
                    )
                else:
                    child_desc = child_summary.get("last_activity_desc", "")
                    if child_desc:
                        desc = (
                            f"delegate_task: subagent {child_desc} "
                            f"(iteration {child_iter}/{child_max})"
                        )
            except Exception:
                pass
            try:
                touch(desc)
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)

    # Register the live agent in the module-level registry so the TUI can
    # target it by subagent_id (kill, pause, status queries).  Unregistered
    # in the finally block, even when the child raises.  Test doubles that
    # hand us a MagicMock don't carry stable ids; skip registration then.
    _raw_sid = getattr(child, "_subagent_id", None)
    _subagent_id = _raw_sid if isinstance(_raw_sid, str) else None
    if _subagent_id:
        if owner_session_id is None:
            try:
                from gateway.session_context import get_session_env

                owner_session_id = get_session_env("HERMES_UI_SESSION_ID", "") or None
            except Exception:
                owner_session_id = None
        if owner_session_id and (
            owner_transport is None or owner_session_record is None
        ):
            owner_transport, owner_session_record = (
                _capture_gateway_steer_authority(owner_session_id)
            )
        _raw_depth = getattr(child, "_delegate_depth", 1)
        _tui_depth = max(0, _raw_depth - 1) if isinstance(_raw_depth, int) else 0
        _parent_sid = getattr(child, "_parent_subagent_id", None)
        # Durable ownership spine: the OWNING CONVERSATION's session id (the
        # same lineage the delivery path routes completions by). Sourced from
        # the child's _parent_session_id stamp so it stays correct even when
        # parent_agent has been rebuilt between dispatch and this run.
        _owner_agent_session_id = (
            str(getattr(child, "_parent_session_id", "") or "")
            or str(getattr(parent_agent, "session_id", "") or "")
        )
        _delegation_id = getattr(child, "_delegation_id", None)
        _register_subagent(
            {
                # I1: every steer accepted for this child is ledgered here.
                "steer_ledger": _SteerLedger.for_child(child, _subagent_id),
                "subagent_id": _subagent_id,
                "parent_id": _parent_sid if isinstance(_parent_sid, str) else None,
                "depth": _tui_depth,
                "goal": goal,
                "delegation_id": (
                    _delegation_id if isinstance(_delegation_id, str) else None
                ),
                "model": (
                    getattr(child, "model", None)
                    if isinstance(getattr(child, "model", None), str)
                    else None
                ),
                "started_at": time.time(),
                "status": "running",
                "tool_count": 0,
                "agent": child,
                # Durable conversation lineage for the model-facing control
                # plane (list/steer/stop). The weakref identity chain breaks
                # when the CLI rebuilds its AIAgent mid-session; this id is
                # the same spine completion delivery routes by.
                "owner_agent_session_id": _owner_agent_session_id or None,
                # Immutable live gateway/TUI session that commissioned this
                # child. Empty outside those hosts; RPC authority fails closed.
                "owner_session_id": owner_session_id,
                "owner_transport": owner_transport,
                "owner_session_record": owner_session_record,
            }
        )

    # Worktree-isolation state: populated inside the try once the child's
    # task id is known; the default no-op keeps every early error path safe.
    _worktree_info: Optional[Dict[str, str]] = None

    def _attach_worktree(entry_dict: Dict[str, Any]) -> None:
        """Inspect + prune the child worktree, reporting into the entry."""
        if _worktree_info is None:
            return
        try:
            from tools import subagent_worktree

            entry_dict["worktree"] = (
                subagent_worktree.finalize_subagent_worktree(_worktree_info)
            )
        except Exception as e:
            # finalize is written hard not to raise, but if it ever does the
            # state is unknown — emit the SAME schema the parent expects,
            # flagged, via the shared factory so the two producers of this
            # payload can never drift.
            logger.warning("worktree finalize failed: %s", e)
            try:
                from tools import subagent_worktree as _sw

                entry_dict["worktree"] = _sw.unproven_worktree_payload(
                    _worktree_info, f"finalize raised: {e}"
                )
            except Exception:
                # Import itself failed — inline the same shape rather than
                # dropping the flag (the parent must still see the warning).
                entry_dict["worktree"] = {
                    "path": _worktree_info.get("path", ""),
                    "branch": _worktree_info.get("branch", ""),
                    "commits": 0,
                    "dirty": False,
                    "pruned": False,
                    "inspection_failed": True,
                    "note": (
                        f"worktree finalize raised ({e}) and the reporting "
                        "helper was unavailable: 'commits' and 'dirty' are "
                        "UNKNOWN, not zero/clean. Inspect "
                        f"{_worktree_info.get('path', '')} before assuming "
                        "no work."
                    ),
                }

    # Set when a child_timeout fires while the child is still working: the
    # child is NOT stopped, the owner returns TIMED_OUT_RUNNING, and the
    # late-completion thread owns the registry/lease/close cleanup below.
    _detached = [False]
    # I2: the run holds the child; parent-driven closes defer until the run
    # (or, once detached, the late thread) releases it via _teardown.
    _attach_owner_teardown(child)  # idempotent; _build_child_agent did it
    _held = _hold_run(child)

    try:
        if not _held:
            raise RuntimeError(
                "delegated child was closed by its parent before its run started"
            )
        _heartbeat_thread.start()
        if child_progress_cb:
            try:
                child_progress_cb("subagent.start", preview=goal)
            except Exception as e:
                logger.debug("Progress callback start failed: %s", e)

        # File-state coordination: reuse the stable subagent_id as the child's
        # task_id so file_state writes, active-subagents registry, and TUI
        # events all share one key.  Falls back to a fresh uuid only if the
        # pre-built id is somehow missing.
        import uuid as _uuid

        child_task_id = _subagent_id or f"subagent-{task_index}-{_uuid.uuid4().hex[:8]}"
        parent_task_id = getattr(parent_agent, "_current_task_id", None)
        # Seed the child's session-cwd record from the parent's (cwd rearch):
        # children share the parent's container, and today they inherit the
        # parent's live env.cwd implicitly. Seeding at spawn preserves that
        # starting directory while keeping the child's subsequent `cd`s
        # isolated in its own record (a child's cd no longer bleeds back into
        # the parent once readers flip to the record store).
        try:
            from tools.terminal_tool import (
                get_session_cwd,
                record_session_cwd,
                register_container_alias,
            )

            record_session_cwd(child_task_id, get_session_cwd(parent_task_id))
            # Per-session container isolation (docker + container_persistent:
            # false) keys containers by session task_id. The child must share
            # the PARENT's container — register the alias so the child's
            # task_id resolves to the parent's container key.
            register_container_alias(child_task_id, parent_task_id)
        except Exception as e:
            logger.debug("Child cwd seed failed: %s", e)

        # Opt-in worktree isolation (delegation.worktree_isolation, inspired
        # by Muse Code's --subagent-worktree-isolation): give this child its
        # own git worktree branched from the parent repo's HEAD, and start its
        # terminal there. Git-only and local-backend-only; any failure
        # degrades silently to the shared-workspace behavior above.
        if _get_worktree_isolation():
            try:
                from tools import subagent_worktree

                if subagent_worktree.local_backend_active():
                    _parent_cwd = None
                    try:
                        from tools.terminal_tool import get_session_cwd as _gsc

                        _parent_cwd = _gsc(parent_task_id)
                    except Exception:
                        pass
                    _worktree_info = subagent_worktree.create_subagent_worktree(
                        _parent_cwd or _resolve_workspace_hint(parent_agent),
                        subagent_id=_subagent_id,
                    )
                else:
                    logger.debug(
                        "worktree isolation skipped: non-local terminal backend"
                    )
            except Exception as e:
                logger.debug("worktree isolation setup failed: %s", e)
            if _worktree_info is not None:
                try:
                    from tools.terminal_tool import record_session_cwd as _rsc

                    _rsc(child_task_id, _worktree_info["path"])
                except Exception as e:
                    logger.debug("worktree cwd seed failed: %s", e)
                # The child's context is already built; carry the isolation
                # contract on the goal message instead (same turn, no
                # system-prompt mutation).
                from tools.subagent_worktree import build_worktree_context_note

                goal = goal + build_worktree_context_note(_worktree_info)

        wall_start = time.time()
        parent_reads_snapshot = (
            list(file_state.known_reads(parent_task_id)) if parent_task_id else []
        )

        # Run child with an optional hard timeout (off by default —
        # result(timeout=None) blocks until the child finishes). Stuck-child
        # protection comes from the heartbeat staleness monitor instead.
        child_timeout = _get_child_timeout()
        hung_seconds = _get_hung_child_seconds()
        # Daemon worker (tools.daemon_pool): a timed-out child is abandoned
        # below; a stdlib non-daemon worker would then block interpreter
        # exit at atexit-join time if the child never unwinds.
        from tools.daemon_pool import DaemonThreadPoolExecutor
        _timeout_executor = DaemonThreadPoolExecutor(
            max_workers=1,
            # Install a non-interactive approval callback in the worker thread
            # so dangerous-command prompts from the subagent don't fall back to
            # input() and deadlock the parent's prompt_toolkit TUI.
            # Callback (deny vs approve) is governed by delegation.subagent_auto_approve.
            initializer=_set_subagent_approval_cb,
            initargs=(_get_subagent_approval_callback(),),
        )
        # Capture the worker thread so the timeout diagnostic can dump its
        # Python stack (see #14726 — 0-API-call hangs are opaque without it).
        _worker_thread_holder: Dict[str, Optional[threading.Thread]] = {"t": None}

        def _relay_child_text(delta: str) -> None:
            # Forward the child's streamed reply text up the progress relay so
            # gateway watch windows mirror it live (subagent.text → message.delta).
            # Inert under CLI/TUI: their progress handlers ignore non-tool events.
            if not delta or not child_progress_cb:
                return
            try:
                child_progress_cb("subagent.text", preview=delta)
            except Exception as e:
                logger.debug("Child text relay failed: %s", e)

        def _run_with_thread_capture():
            _worker_thread_holder["t"] = threading.current_thread()
            return _child_turn(goal)

        def _child_turn(user_message):
            # One child turn with the spawn-time bindings. Also the late
            # path's schema-correction turn (it must not run bare).
            # Bind the routing-only send-origin so the child's bare send_message/
            # react calls resolve to the PARENT's channel, not the global home
            # (PRD v2 RC#1). Uses dedicated contextvars read ONLY by the send
            # resolver — the child keeps its own `platform="subagent"` identity
            # for approval/skills/TTS. Cleared in finally (nestable: a grandchild
            # restores the child's origin, not blank).
            _origin_tokens = _bind_child_send_origin(child)
            # Re-bind the cron-session marker (B1) so a cron job's subagent keeps
            # its approval deny-gating — contextvars don't cross the executor
            # boundary, so we set it explicitly from the spawn-time capture.
            _cron_token = _bind_child_cron_session(child)
            from agent.delegation_context import delegated_child_context
            try:
                with delegated_child_context(str(getattr(child, "session_id", "") or "")):
                    return child.run_conversation(
                        user_message=user_message,
                        task_id=child_task_id,
                        stream_callback=_relay_child_text,
                    )
            finally:
                _clear_child_send_origin(_origin_tokens)
                _clear_child_cron_session(_cron_token)

        _child_context = contextvars.copy_context()
        _child_future = _submit_turn(
            _timeout_executor,
            child,
            _child_context.run,
            _run_with_thread_capture,
        )
        try:
            result = _wait_child_turn(
                _child_future, child, child_timeout, hung_seconds
            )
        except Exception as _timeout_exc:
            # Timed out ≠ dead. A child still working (it made API calls or
            # has live descendants) keeps running; report TIMED_OUT_RUNNING
            # with live handles so the caller waits/steers instead of
            # relaunching the same brief as a second concurrent tree.
            _hung = isinstance(_timeout_exc, _ChildHung)
            if isinstance(
                _timeout_exc, (FuturesTimeoutError, TimeoutError)
            ) and not _hung and not _child_future.done():
                _tor_entry = _timed_out_running_entry(
                    task_index=task_index,
                    child=child,
                    subagent_id=_subagent_id,
                    child_timeout=child_timeout,
                    duration=round(time.monotonic() - child_start, 2),
                )
                if _tor_entry is not None:
                    _detached[0] = True
                    _late_handle = _start_late_completion(
                        _child_future,
                        child=child,
                        parent_agent=parent_agent,
                        task_index=task_index,
                        subagent_id=_subagent_id,
                        child_start=child_start,
                        child_progress_cb=child_progress_cb,
                        child_pool=child_pool,
                        leased_cred_id=leased_cred_id,
                        child_timeout=child_timeout,
                        child_task_id=child_task_id,
                        stream_callback=_relay_child_text,
                        child_turn=_child_turn,
                    )
                    _tor_entry["late_result_id"] = _late_handle.late_result_id
                    if _late_handle.late_dir is not None:
                        # The same captured dir the late thread writes to.
                        _tor_entry["late_result_path"] = str(
                            _late_handle.late_dir / f"{_late_handle.late_result_id}.json"
                        )
                    return _tor_entry
            # No consumer boundary remains once this owner stops waiting for
            # the child. Close acceptance before any completion callback; the
            # ledger names every accepted steer that was not delivered.
            _late_pending_steer = _close_and_read_missed(_subagent_id, child)
            # Signal the child to stop so its thread can exit cleanly.
            try:
                interrupted = child is not None and request_hard_interrupt(child)
                if not interrupted and child is not None and hasattr(child, "_interrupt_requested"):
                    child._interrupt_requested = True
            except Exception:
                pass

            is_timeout = isinstance(_timeout_exc, (FuturesTimeoutError, TimeoutError))
            # This child is reported failed: no descendant may outlive that.
            _reap_subtree(child, "timeout" if is_timeout else "error")
            duration = round(time.monotonic() - child_start, 2)
            logger.warning(
                "Subagent %d %s after %.1fs",
                task_index,
                "timed out" if is_timeout else f"raised {type(_timeout_exc).__name__}",
                duration,
            )

            # When a subagent times out BEFORE making any API call, dump a
            # diagnostic to help users (and us) see what the child was doing.
            # See #14726 — without this, 0-API-call hangs are black boxes.
            diagnostic_path: Optional[str] = None
            child_api_calls = 0
            try:
                _summary = child.get_activity_summary()
                child_api_calls = int(_summary.get("api_call_count", 0) or 0)
            except Exception:
                pass
            if is_timeout and child_api_calls == 0:
                diagnostic_path = _dump_subagent_timeout_diagnostic(
                    child=child,
                    task_index=task_index,
                    # is_timeout implies a cap was configured (result(timeout=None)
                    # never raises FuturesTimeoutError); guard for the type checker.
                    timeout_seconds=float(
                        (hung_seconds if _hung else child_timeout) or 0.0
                    ),
                    duration_seconds=float(duration),
                    worker_thread=_worker_thread_holder.get("t"),
                    goal=goal,
                )
                if diagnostic_path:
                    logger.warning(
                        "Subagent %d 0-API-call timeout — diagnostic written to %s",
                        task_index,
                        diagnostic_path,
                    )

            if child_progress_cb:
                try:
                    child_progress_cb(
                        "subagent.complete",
                        preview=(
                            f"Timed out after {duration}s"
                            if is_timeout
                            else str(_timeout_exc)
                        ),
                        status="timeout" if is_timeout else "error",
                        duration_seconds=duration,
                        summary="",
                    )
                except Exception:
                    pass

            if _hung:
                _err = (
                    f"Subagent made no progress for {hung_seconds:g}s "
                    f"(delegation.hung_child_seconds) with {child_api_calls} "
                    f"API call(s) completed — blocked inside one API call or "
                    f"one tool call (liveness heartbeats are not progress). "
                    f"It was stopped."
                )
                if diagnostic_path:
                    _err += f" Diagnostic: {diagnostic_path}"
            elif is_timeout:
                if child_api_calls == 0:
                    _err = (
                        f"Subagent timed out after {child_timeout}s without "
                        f"making any API call — the child never reached its "
                        f"first LLM request (prompt construction, credential "
                        f"resolution, or transport may be stuck)."
                    )
                    if diagnostic_path:
                        _err += f" Diagnostic: {diagnostic_path}"
                else:
                    _err = (
                        f"Subagent timed out after {child_timeout}s with "
                        f"{child_api_calls} API call(s) completed — likely "
                        f"stuck on a slow API call, tool call, or unresponsive "
                        f"network request."
                    )
                    if diagnostic_path:
                        _err += f" Diagnostic: {diagnostic_path}"
            else:
                _err = str(_timeout_exc)

            _error_entry = {
                "task_index": task_index,
                "status": "timeout" if is_timeout else "error",
                "summary": None,
                "error": _err,
                "exit_reason": "timeout" if is_timeout else "error",
                "api_calls": child_api_calls,
                "duration_seconds": duration,
                "timeout_seconds": (
                    hung_seconds if _hung else child_timeout if is_timeout else None
                ),
                "timed_out_after_seconds": duration if is_timeout else None,
                "timeout_phase": (
                    "no_progress" if _hung
                    else "before_first_llm_call" if is_timeout and child_api_calls == 0
                    else "after_llm_calls" if is_timeout
                    else None
                ),
                "_child_role": getattr(child, "_delegate_role", None),
                "diagnostic_path": diagnostic_path,
            }
            if _late_pending_steer:
                _error_entry["missed_steer"] = _late_pending_steer
                _error_entry["error"] += (
                    " [steer did not land before the subagent stopped: "
                    f"{_late_pending_steer}]"
                )
            _attach_worktree(_error_entry)
            return _error_entry
        finally:
            # Shut down executor without waiting — if the child thread
            # is stuck on blocking I/O, wait=True would hang forever.
            _timeout_executor.shutdown(wait=False)

        # T1-24: structured-output contract validation + ONE bounded retry.
        # Runs only when a schema was attached at dispatch; schema-less
        # delegations take none of these branches and their result entry
        # stays byte-identical (wire-shape pinning).
        # Pattern from: github/copilot-cli ctx.agent(prompt, {schema}) —
        # PATTERN ONLY, no code copied.
        _output_schema = getattr(child, "_delegate_output_schema", None)
        _schema_valid, _schema_errors, _schema_retries = _apply_output_schema(
            child,
            result,
            task_index=task_index,
            child_task_id=child_task_id,
            stream_callback=_relay_child_text,
        )

        # Linearization boundary for registry steering. From this point on the
        # child cannot consume another steer. Closing under the registry lock
        # rejects any later caller; the ledger holds every earlier one.
        _missed_steer = _close_and_read_missed(_subagent_id, child)

        # Flush any remaining batched progress to gateway
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            try:
                child_progress_cb._flush()
            except Exception as e:
                logger.debug("Progress callback flush failed: %s", e)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        api_calls = result.get("api_calls", 0)

        # The child emits the literal "(empty)" sentinel (see run_agent.py) when
        # it gives up after repeated empty-LLM-response retries — typically a
        # transport bug (misrouted provider, adapter returning empty
        # ChatCompletion, etc.). Treat it as a failure so the parent surfaces
        # it instead of silently accepting zero-content "success". A returned
        # failed=True (non-retryable API error) is failed/error, not
        # completed/max_iterations; see _classify_child_outcome.
        status, exit_reason = _classify_child_outcome(result)

        # Build tool trace from conversation messages (already in memory).
        # Uses tool_call_id to correctly pair parallel tool calls with results.
        tool_trace: list[Dict[str, Any]] = []
        trace_by_id: Dict[str, Dict[str, Any]] = {}
        messages = result.get("messages") or []
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        arguments = fn.get("arguments", "")
                        entry_t = {
                            "tool": fn.get("name", "unknown"),
                            "args_bytes": len(arguments),
                            "input_summary": _summarize_tool_arguments(arguments),
                        }
                        tool_trace.append(entry_t)
                        tc_id = tc.get("id")
                        if tc_id:
                            trace_by_id[tc_id] = entry_t
                elif msg.get("role") == "tool":
                    content = _stringify_tool_content(msg.get("content", ""))
                    is_error = _looks_like_error_output(content)
                    result_meta = {
                        "result_bytes": len(content),
                        "status": "error" if is_error else "ok",
                    }
                    # Match by tool_call_id for parallel calls
                    tc_id = msg.get("tool_call_id")
                    target = trace_by_id.get(tc_id) if tc_id else None
                    if target is not None:
                        target.update(result_meta)
                    elif tool_trace:
                        # Fallback for messages without tool_call_id
                        tool_trace[-1].update(result_meta)

        # Extract token counts (safe for mock objects)
        _input_tokens = getattr(child, "session_prompt_tokens", 0)
        _output_tokens = getattr(child, "session_completion_tokens", 0)
        _model = getattr(child, "model", None)

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": _model if isinstance(_model, str) else None,
            "exit_reason": exit_reason,
            # Explicit, parent-visible truncation flag. A subagent that
            # exhausts its per-child iteration budget still returns a summary,
            # so `status` stays "completed" (see above) — without this the
            # parent can't tell truncated-but-summarized from cleanly-finished
            # work except by parsing the summary prose. exit_reason is computed
            # authoritatively from the child's `completed` flag.
            "truncated": exit_reason == "max_iterations",
            "tokens": {
                "input": (
                    _input_tokens if isinstance(_input_tokens, (int, float)) else 0
                ),
                "output": (
                    _output_tokens if isinstance(_output_tokens, (int, float)) else 0
                ),
            },
            "tool_trace": tool_trace,
            # Captured before the finally block calls child.close() so the
            # parent thread can fire subagent_stop with the correct role.
            # Stripped before the dict is serialised back to the model.
            "_child_role": getattr(child, "_delegate_role", None),
            # Captured before child.close() so the parent aggregator can fold
            # the child's total spend into the parent's session cost.  Port of
            # Kilo-Org/kilocode#9448 — previously the footer only reflected the
            # parent's direct API calls and under-counted subagent-heavy runs.
            # Stripped before the dict is serialised back to the model.
            "_child_cost_usd": (
                float(getattr(child, "session_estimated_cost_usd", 0.0) or 0.0)
                if isinstance(
                    getattr(child, "session_estimated_cost_usd", 0.0),
                    (int, float),
                )
                else 0.0
            ),
        }
        # Per-delegation spend, serialized back to the model alongside
        # tokens/api_calls so the parent can see what each delegation cost.
        # Mirrors _child_cost_usd (which is stripped pre-serialization and
        # only feeds the parent session rollup).
        # Inspired by: Perplexity Agent API result shape (idea-level).
        entry["cost_usd"] = round(entry["_child_cost_usd"], 6)
        _cost_status = getattr(child, "session_cost_status", None)
        entry["cost_status"] = (
            _cost_status if isinstance(_cost_status, str) and _cost_status
            else "unknown"
        )
        if status == "failed":
            entry["error"] = result.get("error", "Subagent did not produce a response.")
            # Same reap as the raise path below: a child that fails by
            # RETURNING must not leave its live subtree running.
            _reap_subtree(child, "error")

        # T1-24: schema-validation outcome — emitted ONLY when a schema was
        # requested, so legacy (schema-less) payloads keep their exact shape.
        if isinstance(_output_schema, dict):
            entry["schema_valid"] = bool(_schema_valid)
            if _schema_retries:
                entry["schema_retries"] = _schema_retries
            if not _schema_valid and _schema_errors:
                entry["schema_errors"] = _schema_errors

        # steer_subagent() returning True means "queued". Every queued steer
        # the child never wrote into a tool result is named here as MISSED
        # rather than silently absorbed (the ledger, not the finalizer's
        # "pending_steer", is the source: other paths empty that slot).
        if isinstance(_missed_steer, str) and _missed_steer.strip():
            entry["missed_steer"] = _missed_steer
            _miss_note = (
                "[steer did not land — the subagent finished before it could "
                f"be delivered: {_missed_steer}]"
            )
            entry["summary"] = f"{summary}\n\n{_miss_note}" if summary else _miss_note

        # Cross-agent file-state reminder.  If this subagent wrote any
        # files the parent had already read, surface it so the parent
        # knows to re-read before editing — the scenario that motivated
        # the registry.  We check writes by ANY non-parent task_id (not
        # just this child's), which also covers transitive writes from
        # nested orchestrator→worker chains.
        try:
            if parent_task_id and parent_reads_snapshot:
                sibling_writes = file_state.writes_since(
                    parent_task_id, wall_start, parent_reads_snapshot
                )
                if sibling_writes:
                    mod_paths = sorted(
                        {p for paths in sibling_writes.values() for p in paths}
                    )
                    if mod_paths:
                        reminder = (
                            "\n\n[NOTE: subagent modified files the parent "
                            "previously read — re-read before editing: "
                            + ", ".join(mod_paths[:8])
                            + (
                                f" (+{len(mod_paths) - 8} more)"
                                if len(mod_paths) > 8
                                else ""
                            )
                            + "]"
                        )
                        if entry.get("summary"):
                            entry["summary"] = entry["summary"] + reminder
                        else:
                            entry["stale_paths"] = mod_paths
        except Exception:
            logger.debug("file_state sibling-write check failed", exc_info=True)

        # Per-branch observability payload: tokens, cost, files touched, and
        # a tail of tool-call results.  Fed into the TUI's overlay detail
        # pane + accordion rollups (features 1, 2, 4).  All fields are
        # optional — missing data degrades gracefully on the client.
        _cost_usd = getattr(child, "session_estimated_cost_usd", None)
        _reasoning_tokens = getattr(child, "session_reasoning_tokens", 0)
        try:
            _files_read = list(file_state.known_reads(child_task_id))[:40]
        except Exception:
            _files_read = []
        try:
            _files_written_map = file_state.writes_since(
                "", wall_start, []
            )  # all writes since wall_start
        except Exception:
            _files_written_map = {}
        _files_written = sorted(
            {
                p
                for tid, paths in _files_written_map.items()
                if tid == child_task_id
                for p in paths
            }
        )[:40]

        _output_tail = _extract_output_tail(result, max_entries=8, max_chars=600)

        complete_kwargs: Dict[str, Any] = {
            "preview": summary[:160] if summary else entry.get("error", ""),
            "status": status,
            "duration_seconds": duration,
            "summary": summary[:500] if summary else entry.get("error", ""),
            "input_tokens": (
                int(_input_tokens) if isinstance(_input_tokens, (int, float)) else 0
            ),
            "output_tokens": (
                int(_output_tokens) if isinstance(_output_tokens, (int, float)) else 0
            ),
            "reasoning_tokens": (
                int(_reasoning_tokens)
                if isinstance(_reasoning_tokens, (int, float))
                else 0
            ),
            "api_calls": int(api_calls) if isinstance(api_calls, (int, float)) else 0,
            "files_read": _files_read,
            "files_written": _files_written,
            "output_tail": _output_tail,
        }
        if _cost_usd is not None:
            try:
                complete_kwargs["cost_usd"] = float(_cost_usd)
            except (TypeError, ValueError):
                pass

        if child_progress_cb:
            try:
                child_progress_cb("subagent.complete", **complete_kwargs)
            except Exception as e:
                logger.debug("Progress callback completion failed: %s", e)

        _attach_worktree(entry)
        return entry

    except Exception as exc:
        _late_pending_steer = _close_and_read_missed(_subagent_id, child)
        _reap_subtree(child, "error")
        duration = round(time.monotonic() - child_start, 2)
        logging.exception(f"[subagent-{task_index}] failed")
        if child_progress_cb:
            try:
                child_progress_cb(
                    "subagent.complete",
                    preview=str(exc),
                    status="failed",
                    duration_seconds=duration,
                    summary=str(exc),
                )
            except Exception as e:
                logger.debug("Progress callback failure relay failed: %s", e)
        _error_entry = {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
            "_child_role": getattr(child, "_delegate_role", None),
        }
        if _late_pending_steer:
            _error_entry["missed_steer"] = _late_pending_steer
            _error_entry["error"] += (
                " [steer did not land before the subagent stopped: "
                f"{_late_pending_steer}]"
            )
        # _attach_worktree defaults to a no-op when isolation never engaged.
        _attach_worktree(_error_entry)
        return _error_entry

    finally:
        # Stop the heartbeat thread so it doesn't keep touching parent activity
        # after the child has finished (or failed).  Guard the join: .start()
        # now lives inside the try block, so if it raised (OS thread
        # exhaustion) the thread was never started and Thread.join() would
        # raise RuntimeError.  ident is None until start() succeeds.
        _heartbeat_stop.set()
        if _heartbeat_thread.ident is not None:
            _heartbeat_thread.join(timeout=5)

        # Restore the parent's tool names so the process-global is correct
        # for any subsequent execute_code calls or other consumers.
        import model_tools

        saved_tool_names = getattr(child, "_delegate_saved_tool_names", None)
        if isinstance(saved_tool_names, list):
            model_tools._last_resolved_tool_names = list(saved_tool_names)

        if not _detached[0]:
            _release_child_resources(
                child,
                parent_agent,
                _subagent_id,
                child_pool,
                leased_cred_id,
            )


# ── fork: child resource release ──
def _release_child_resources(
    child: Any,
    parent_agent: Any,
    _subagent_id: Optional[str],
    child_pool: Any,
    leased_cred_id: Any,
) -> None:
    """Per-child teardown once the child's run has ended (or never started).

    Runs in _run_single_child's finally. The late-completion path calls the
    two halves separately (docs/dev/delegate-child-lifecycle.md, I2).
    """
    _release_child_handles(child, parent_agent, _subagent_id, child_pool, leased_cred_id)
    _teardown(child, "run_end", owner=True)


def _release_child_handles(
    child: Any,
    parent_agent: Any,
    _subagent_id: Optional[str],
    child_pool: Any,
    leased_cred_id: Any,
) -> None:
    """Registry entry, credential lease and parent link (no transcript data)."""
    # Drop the TUI-facing registry entry.  Safe to call even if the
    # child was never registered (e.g. ID missing on test doubles).
    if _subagent_id:
        _unregister_subagent(_subagent_id, agent=child)

    if child_pool is not None and leased_cred_id is not None:
        try:
            child_pool.release_lease(leased_cred_id)
        except Exception as exc:
            logger.debug("Failed to release credential lease: %s", exc)

    # Remove child from active tracking

    # Unregister child from interrupt propagation
    if hasattr(parent_agent, "_active_children"):
        try:
            lock = getattr(parent_agent, "_active_children_lock", None)
            if lock:
                with lock:
                    parent_agent._active_children.remove(child)
            else:
                parent_agent._active_children.remove(child)
        except (ValueError, UnboundLocalError) as e:
            logger.debug("Could not remove child from active_children: %s", e)


def _close_child_persistence(child: Any) -> None:
    """Close the child's SessionDB/tool resources and its relay session.

    Only the teardown door (``_teardown`` / ``_release_hold``) calls this,
    and never while the child's run or a turn of it is live (I2).
    """
    # Close tool resources (terminal sandboxes, browser daemons,
    # background processes, httpx clients) so subagent subprocesses
    # don't outlive the delegation.
    try:
        if hasattr(child, "close"):
            child.close()
    except Exception:
        logger.debug("Failed to close child agent after delegation")

    # The AIAgent turn boundary normally closes the child scope itself. This
    # fallback covers failures before that boundary starts, but must not pop
    # a scope while a timed-out child worker is still unwinding.
    try:
        from agent import relay_runtime

        runtime = relay_runtime.get_runtime(create=False)
        child_session_id = str(getattr(child, "session_id", "") or "")
        child_turn_is_active = relay_runtime.SESSION_COORDINATOR.has_active_turn(
            profile_key=relay_runtime.current_profile_key(),
            session_id=child_session_id,
        )
        if runtime is not None and child_session_id and not child_turn_is_active:
            runtime.unregister_subagent({"child_session_id": child_session_id})
    except Exception:
        logger.debug("Failed to close child Relay session after delegation")


# ── fork: owner-door / deferred teardown ──
# ---------------------------------------------------------------------------
# I2: the one teardown door (docs/dev/delegate-child-lifecycle.md).
#
# Every close of a delegated child's persistence -- delegate_task's own run
# end, the late path, a parent's AIAgent.close()/release_clients() reaching
# it through ``_owner_teardown``, an ancestor's recursive close -- calls
# ``_teardown``. It refuses (defers, logged) while the child is held: by the
# run that owns it (``_hold_run``, released by the owner's own teardown) or
# by any live turn (``_submit_turn`` / ``_inline_turn``). The deferred close
# runs once, when the last hold goes, in a copy of the requester's context.
# tests/tools/test_delegate_teardown_door.py fails if a new call site closes
# a child outside this door.
# ---------------------------------------------------------------------------
_door_lock = threading.Lock()
_door: "weakref.WeakKeyDictionary[Any, Dict[str, Any]]" = weakref.WeakKeyDictionary()


_door_strong: Dict[int, Tuple[Any, Dict[str, Any]]] = {}


def _door_slot(child: Any) -> Dict[str, Any]:
    try:
        slot = _door.get(child)
    except TypeError:  # not weak-referenceable: keep it strongly
        slot = _door_strong.get(id(child), (None, None))[1]
    if slot is None:
        slot = {"run": False, "turns": 0, "pending": None, "closed": False}
        try:
            _door[child] = slot
        except TypeError:
            _door_strong[id(child)] = (child, slot)
    return slot


def _hold_run(child: Any) -> bool:
    """The run that owns *child* holds it until it calls ``_teardown(owner=True)``.

    False (no hold taken) if the door already closed the child: a parent close
    won the race with the run's start, and the run must not use it.
    """
    with _door_lock:
        slot = _door_slot(child)
        if slot["closed"]:
            return False
        slot["run"] = True
        return True


def _release_hold(child: Any, *, run: bool = False, turn: bool = False) -> None:
    with _door_lock:
        slot = _door_slot(child)
        if run:
            slot["run"] = False
        if turn:
            slot["turns"] = max(0, slot["turns"] - 1)
        if slot["run"] or slot["turns"] or slot["pending"] is None or slot["closed"]:
            return
        reason, ctx = slot["pending"]
        slot["pending"] = None
        slot["closed"] = True
    waiting = _count_deferred_teardown(-1)
    logger.info(
        "delegate_task deferred teardown of %s runs now (%s; still deferred: %d)",
        getattr(child, "_subagent_id", None),
        reason,
        waiting,
    )
    ctx.run(_close_child_persistence, child)


def _submit_turn(executor: Any, child: Any, fn: Any, *args: Any) -> Any:
    """Submit one child turn; the child is held from submit until it exits."""
    with _door_lock:
        _door_slot(child)["turns"] += 1
    try:
        fut = executor.submit(fn, *args)
    except BaseException:
        _release_hold(child, turn=True)
        raise
    fut.add_done_callback(lambda _f: _release_hold(child, turn=True))
    return fut


class _inline_turn:
    """Hold *child* for a turn that runs on the calling thread."""

    def __init__(self, child: Any) -> None:
        self.child = child

    def __enter__(self) -> None:
        with _door_lock:
            _door_slot(self.child)["turns"] += 1

    def __exit__(self, *exc: Any) -> None:
        _release_hold(self.child, turn=True)


def _teardown(child: Any, reason: str, *, owner: bool = False) -> bool:
    """The only door to closing a delegated child. True if it closed now.

    ``owner=True`` is the run's own end (it releases the run hold first).
    Idempotent: a second request after the close is a no-op.
    """
    with _door_lock:
        slot = _door_slot(child)
        if owner:
            slot["run"] = False
        if slot["closed"]:
            return False
        if slot["run"] or slot["turns"]:
            if slot["pending"] is None:
                slot["pending"] = (reason, contextvars.copy_context())
                waiting = _count_deferred_teardown(1)
                logger.warning(
                    "delegate_task teardown of %s (%s) deferred: %s; it runs when "
                    "the child is released (deferred teardowns: %d)",
                    getattr(child, "_subagent_id", None),
                    reason,
                    f"{slot['turns']} live turn(s)" if slot["turns"] else "its run is active",
                    waiting,
                )
            return False
        slot["closed"] = True
        if slot["pending"] is not None:
            slot["pending"] = None
            _count_deferred_teardown(-1)
    _close_child_persistence(child)
    return True


def _attach_owner_teardown(child: Any) -> None:
    """Route parent-driven closes (AIAgent.close / release_clients) to the door."""
    try:
        child._owner_teardown = lambda reason="parent_close": _teardown(child, reason)
    except Exception:
        logger.debug("could not attach the teardown door", exc_info=True)


# ── fork: inherited tier re-gate (#1523 b3cad0b7df) ──
def _regate_inherited_child_tier(child, parent_agent, explicit_tier_overrides) -> None:
    """Re-gate the inherited tier override when the child's route differs.

    The inherit branch copies the parent's request_overrides verbatim, but
    those tier keys were gated against the PARENT's route. When
    delegation.model (or api_mode) moves the child elsewhere, e.g.
    gpt-6-astra ultrafast -> gpt-5.4-mini, re-derive the tier for the child's
    route from the parent's session tier. Tier keys the user set explicitly
    in delegation.request_overrides are a deliberate override and are kept.
    """
    same_route = all(
        getattr(child, attr, None) == getattr(parent_agent, attr, None)
        for attr in ("model", "provider", "api_mode")
    )
    if same_route:
        return
    from agent.agent_init import (
        _SERVICE_TIER_OVERRIDE_KEYS,
        _regate_service_tier_overrides,
    )

    if not getattr(child, "service_tier", None):
        child.service_tier = getattr(parent_agent, "service_tier", None)
    try:
        _regate_service_tier_overrides(child)
    except Exception:
        # Fail closed: an unresolvable route gets no tier override.
        logger.debug("subagent tier re-gate failed; dropping tier keys", exc_info=True)
        overrides = dict(getattr(child, "request_overrides", {}) or {})
        for key in _SERVICE_TIER_OVERRIDE_KEYS:
            overrides.pop(key, None)
        child.request_overrides = overrides
    explicit = {
        k: v
        for k, v in (explicit_tier_overrides or {}).items()
        if k in _SERVICE_TIER_OVERRIDE_KEYS
    }
    if explicit:
        child.request_overrides = {
            **dict(getattr(child, "request_overrides", {}) or {}),
            **explicit,
        }


# ── fork: durable background spec + restart recovery (t_1191e078 #1104; gateway/run.py caller) ──
def _build_durable_background_spec(
    *,
    task_list: List[Dict[str, Any]],
    shared_context: Optional[str],
    top_role: str,
    inherit_context: Optional[bool],
    cfg: Dict[str, Any],
    creds: Dict[str, Any],
    parent_agent,
    session_key: str,
    parent_session_id: Optional[str],
    origin_ui_session_id: str,
    origin_session_id: str = "",
    max_iterations: int,
    children: Optional[List[Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build the non-secret restart intent for a routable gateway session."""
    if not is_truthy_value(cfg.get("resume_on_restart"), default=True):
        return None, None
    try:
        from gateway.session_context import get_session_env

        source_name = get_session_env("HERMES_SESSION_SOURCE", "")
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        if source_name == "tui" or not platform:
            return None, None
        profile = get_session_env("HERMES_SESSION_PROFILE", "") or "default"
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        user_name = get_session_env("HERMES_SESSION_USER_NAME", "") or None
    except Exception:
        return None, None

    from gateway.status import get_current_boot_id

    boot_id = get_current_boot_id()
    if not boot_id or str(boot_id).endswith(":"):
        raise RuntimeError("gateway boot identity is unavailable")

    persisted_tasks = []
    for index, task in enumerate(task_list):
        persisted = copy.deepcopy(task)
        persisted["role"] = _normalize_role(task.get("role") or top_role)
        persisted["inherit_context"] = bool(
            task.get("inherit_context")
            if "inherit_context" in task
            else inherit_context
        )
        if persisted["inherit_context"] and children and index < len(children):
            persisted["materialized_prefill_messages"] = copy.deepcopy(
                getattr(children[index], "prefill_messages", None)
            )
        persisted_tasks.append(persisted)

    effective_agent = children[0] if children else parent_agent
    effective_provider = creds.get("provider") or getattr(effective_agent, "provider", None)
    if cfg.get("base_url"):
        credential_ref = {
            "source": "delegation_config",
            "parent_provider": (
                getattr(parent_agent, "custom_provider", None)
                or getattr(parent_agent, "provider", None)
            ),
        }
    else:
        credential_ref = {
            "source": "provider",
            "provider": cfg.get("provider") or effective_provider,
            "custom_provider": getattr(effective_agent, "custom_provider", None),
        }
    session_parts = str(session_key or "").split(":")
    chat_type = session_parts[3] if len(session_parts) > 4 else None
    parent_toolsets = (
        getattr(effective_agent, "enabled_toolsets", None)
        or getattr(effective_agent, "_enabled_toolsets", None)
    )
    execution = {
        "model": creds.get("model") or getattr(effective_agent, "model", None),
        "provider": effective_provider,
        "base_url": creds.get("base_url") or getattr(effective_agent, "base_url", None),
        "api_mode": creds.get("api_mode") or getattr(effective_agent, "api_mode", None),
        "acp_command": creds.get("command") or getattr(
            effective_agent, "acp_command", None
        ),
        "acp_args": list(
            creds.get("args") or getattr(effective_agent, "acp_args", None) or []
        ),
        "reasoning_config": copy.deepcopy(getattr(effective_agent, "reasoning_config", None)),
        "fallback_chain": copy.deepcopy(getattr(effective_agent, "fallback_model", None)),
        "service_tier": getattr(effective_agent, "service_tier", None),
        "providers_allowed": copy.deepcopy(getattr(effective_agent, "providers_allowed", None)),
        "providers_ignored": copy.deepcopy(getattr(effective_agent, "providers_ignored", None)),
        "providers_order": copy.deepcopy(getattr(effective_agent, "providers_order", None)),
        "provider_sort": getattr(effective_agent, "provider_sort", None),
        "provider_require_parameters": bool(
            getattr(effective_agent, "provider_require_parameters", False)
        ),
        "provider_data_collection": getattr(
            effective_agent, "provider_data_collection", None
        ),
        "openrouter_min_coding_score": getattr(
            effective_agent, "openrouter_min_coding_score", None
        ),
        "toolsets": list(parent_toolsets) if parent_toolsets else None,
        "max_iterations": max_iterations,
        "parent_depth": int(getattr(parent_agent, "_delegate_depth", 0) or 0),
        "max_spawn_depth": _get_max_spawn_depth(),
        "orchestrator_enabled": _get_orchestrator_enabled(),
        "workspace_hint": _resolve_workspace_hint(parent_agent),
        # Symbolic lookup data only. Credential bytes are re-resolved on resume.
        "credential_ref": credential_ref,
    }
    return {
        "profile": profile,
        "source": {
            "kind": "batch" if len(persisted_tasks) > 1 else "single",
            "tasks": persisted_tasks,
            "shared_context": shared_context,
        },
        "execution": execution,
        "route": {
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "origin_ui_session_id": origin_ui_session_id,
            # Raw api_server wake target (the X-Hermes-Session-Id the request
            # bound as chat_id). The gateway's completion router needs it to
            # self-post the wake when the session_key is a RAW id with no
            # parseable routing metadata (gateway/run.py `raw_sid`). Without it
            # on the durable route, a restart-safe background batch that
            # completes through the terminal-store path loses its wake target.
            "origin_session_id": origin_session_id,
            "platform": platform,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "user_name": user_name,
            "profile": profile,
        },
    }, str(boot_id)


def build_recovered_delegation_runner(
    record: Dict[str, Any],
    continuation: str,
    parent_agent,
) -> Callable[[], Dict[str, Any]]:
    """Reconstruct one persisted single/batch unit through ``delegate_task``."""
    source = record.get("source") or {}
    tasks = copy.deepcopy(source.get("tasks") or [])
    for task in tasks:
        original = task.get("context")
        task["context"] = f"{original}\n\n{continuation}" if original else continuation

    def _runner() -> Dict[str, Any]:
        from gateway.session_context import restore_session_vars, set_session_vars

        route = record.get("route") or {}
        execution = record.get("execution") or {}
        tokens = set_session_vars(
            platform=str(route.get("platform") or ""),
            source="gateway_recovery",
            chat_id=str(route.get("chat_id") or ""),
            thread_id=str(route.get("thread_id") or ""),
            user_id=str(route.get("user_id") or ""),
            user_name=str(route.get("user_name") or ""),
            session_key=str(route.get("session_key") or ""),
            session_id=str(route.get("parent_session_id") or ""),
            profile=str(route.get("profile") or record.get("profile") or "default"),
            cwd=str(execution.get("workspace_hint") or ""),
            async_delivery=True,
            ui_session_id=str(route.get("origin_ui_session_id") or ""),
        )
        try:
            raw = delegate_task(
                tasks=tasks,
                max_iterations=execution.get("max_iterations"),
                background=False,
                parent_agent=parent_agent,
                _recovery_spec=record,
            )
        finally:
            restore_session_vars(tokens)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("recovered delegate_task returned a non-object payload")
        return parsed

    return _runner


def _build_children(
    task_list: List[Dict[str, Any]], task_schemas: List[Optional[Dict[str, Any]]], creds: Dict[str, Any], *,
    top_role: str, max_iterations: int, parent_agent, routing_cfg: Dict[str, Any],
    live_deleg_id: Optional[str], live_writers: list, task_images: Optional[List[Optional[List[str]]]] = None,
) -> tuple[List[tuple], Optional[str]]:
    """Build every child on the main thread (construction is not thread-safe);
    ``(children, None)`` or ``([], error)`` on an explicit-pin preflight failure."""
    from tools.delegation_live_log import wrap_progress_callback
    from tools.delegation_output_schema import append_output_contract
    overrides = {
        "override_provider": creds["provider"], "override_base_url": creds["base_url"],
        "override_api_key": creds["api_key"], "override_api_mode": creds["api_mode"],
        "override_request_overrides": creds.get("request_overrides"),
        "override_acp_command": creds.get("command"),
        "override_acp_args": creds.get("args"),
        "routing_cfg": routing_cfg,
    }
    children = []
    for i, t in enumerate(task_list):
        _task_schema = task_schemas[i] if i < len(task_schemas) else None
        _child_context = t.get("context")
        if _task_schema is not None:
            _child_context = append_output_contract(_child_context, _task_schema)
        try:
            child = _build_child_preserving_parent_tools(
                task_index=i, goal=t["goal"], context=_child_context,
                toolsets=None,  # always inherit the parent's toolsets
                model=creds["model"], max_iterations=max_iterations, task_count=len(task_list),
                parent_agent=parent_agent, role=_normalize_role(t.get("role") or top_role), **overrides,
            )
        except ValueError as exc:
            return [], str(exc)
        if _task_schema is not None:
            with _quiet("Could not attach output schema to child %d", i):
                child._delegate_output_schema = _task_schema
        # Validated per-task images; absent on image-less tasks, which keep the text-only goal turn.
        _t_images = task_images[i] if task_images and i < len(task_images) else None
        if _t_images:
            with _quiet("Could not attach images to child %d", i):
                child._delegate_images = _t_images
        # Tee progress events into the live transcript (wrapper keeps the
        # _flush contract and swallows writer failures).
        _writer = live_writers[i] if i < len(live_writers) else None
        if _writer is not None:
            child.tool_progress_callback = wrap_progress_callback(getattr(child, "tool_progress_callback", None), _writer)
            child._live_transcript_path = str(_writer.path)
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
            _ident_ref = getattr(child, "_progress_identity_ref", None)
            if isinstance(_ident_ref, dict):
                _ident_ref["delegation_id"] = live_deleg_id
        children.append((i, t, child))
    return children, None


def _oneshot_spawn_budget(parent_agent: Any, requested: int) -> Optional[str]:
    """Charge *requested* children against the finite one-shot session's total (delegation.oneshot_max_children);
    the error text tells the model to do the work inline. Interactive and gateway sessions are never charged."""
    from agent.oneshot_footprint import is_single_query_session
    if not is_single_query_session():
        return None
    cap = _get_oneshot_max_children()
    if cap <= 0:
        return None
    spent = getattr(parent_agent, "_oneshot_children_spawned", 0)
    if spent + requested > cap:
        return (
            f"Delegation budget for this one-shot run is exhausted ({spent}/{cap} subagents used; "
            f"delegation.oneshot_max_children). Do the remaining work yourself in this session — reviewing "
            f"your own diff and running the tests inline is expected here, not a delegated review."
        )
    parent_agent._oneshot_children_spawned = spent + requested
    return None


# ── fork: delegate_task (whole-function; upstream's _Batch/_run_batch dispatch stays importable in
#    tools/delegate_tool_dispatch.py — see ledger R07-delegate POLICY-DIVERGENCE) ──
def delegate_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    role: Optional[str] = None,
    background: Optional[bool] = None,
    inherit_context: Optional[bool] = None,
    skills: Optional[List[str]] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    allow_flagship_reason: Optional[str] = None,
    images: Optional[List[str]] = None,
    action: Optional[str] = None,
    subagent_id: Optional[str] = None,
    message: Optional[str] = None,
    parent_agent=None,
    _recovery_spec: Optional[Dict[str, Any]] = None,
    credentials_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Spawn one or more child agents to handle delegated tasks, or control
    already-running ones.

    Spawn modes (action='spawn' or omitted):
      - Single: provide goal (+ optional context and role)
      - Batch:  provide tasks array [{goal, context, role}, ...]

    Control modes (synchronous, never backgrounded):
      - action='list'  -> live children of this conversation's spawn tree
      - action='steer' -> queue course-correction text into a running child
                          (subagent_id + message)
      - action='stop'  -> interrupt a running child early (subagent_id)

    The 'role' parameter controls whether a child can further delegate:
    'leaf' (default) cannot; 'orchestrator' retains the delegation
    toolset and can spawn its own workers, bounded by
    delegation.max_spawn_depth.  Per-task role beats the top-level one.

    Returns JSON with results array, one entry per task.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    # ── Control plane: list/steer/stop run synchronously and return here.
    # They never spawn, so they bypass the pause gate, depth limit, and the
    # async dispatch machinery entirely.
    normalized_action = (action or "").strip().lower()
    if normalized_action in _CONTROL_ACTIONS:
        return _handle_control_action(
            normalized_action, subagent_id, message, parent_agent
        )
    if normalized_action and normalized_action != "spawn":
        return tool_error(
            f"Unknown action '{action}'. Use spawn (default), list, steer, or stop."
        )

    model = str(model or "").strip() or None
    provider = str(provider or "").strip() or None
    if provider and not model:
        return tool_error("delegate_task provider requires a model override.")
    audit_reason = None
    if model:
        from hermes_cli.model_switch import resolve_model_pair_for_storage
        from hermes_cli.model_policy import flagship_model_match, validate_worker_model

        model, provider = resolve_model_pair_for_storage(model, provider)
        try:
            reason = validate_worker_model(model, allow_flagship_reason=allow_flagship_reason)
        except ValueError as exc:
            return tool_error(str(exc))
        if flagship_model_match(model):
            audit_reason = reason

    # Operator-controlled kill switch — lets the TUI freeze new fan-out
    # when a runaway tree is detected, without interrupting already-running
    # children.  Cleared via the matching `delegation.pause` RPC.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    # Normalise the top-level role once; per-task overrides re-normalise.
    top_role = _normalize_role(role)

    # Background (async) delegation now applies to BOTH single tasks and
    # batches. A batch is dispatched as ONE async unit: the whole fan-out runs
    # on the daemon executor, joins on every child (see _execute_and_aggregate
    # / dispatch_async_delegation_batch), and pushes a SINGLE completion event
    # carrying the consolidated per-task results. It re-enters the conversation
    # as one message once ALL children finish — the chat is not blocked while
    # they run.
    background = is_truthy_value(background, default=False) if background is not None else False

    # Depth limit — configurable via delegation.max_spawn_depth,
    # default 2 for parity with the original MAX_DEPTH constant.
    depth = getattr(parent_agent, "_delegate_depth", 0)
    recovery_execution = (_recovery_spec or {}).get("execution") or {}
    max_spawn = int(
        recovery_execution.get("max_spawn_depth") or _get_max_spawn_depth()
    )
    if depth >= max_spawn:
        return tool_error(
            f"Delegation depth limit reached (depth={depth}, "
            f"max_spawn_depth={max_spawn}). Raise "
            f"delegation.max_spawn_depth in config.yaml if deeper "
            f"nesting is required (no hard ceiling, but each level "
            f"multiplies API cost)."
        )

    # Load config
    cfg = _load_config()
    if _recovery_spec:
        # Recovery reuses this same live adapter but pins the effective,
        # non-secret settings captured before the original executor submit.
        # Credentials are deliberately absent and resolve again below.
        _execution = recovery_execution
        _credential_ref = _execution.get("credential_ref") or {}
        _recovered_provider = (
            _credential_ref.get("provider") or _execution.get("provider") or ""
        )
        # Only a direct-endpoint delegation (credential_ref.source ==
        # "delegation_config") pins its persisted base_url/api_mode. A NAMED
        # provider re-resolves its endpoint by name, like the original spawn
        # did: replaying the resolved base_url next to the name collapsed the
        # child to provider="custom" (no provider profile -> no relay routing
        # key) and would also pin a stale endpoint after a registry move.
        _direct_endpoint = (
            _credential_ref.get("source") == "delegation_config"
            or not _recovered_provider
            or _recovered_provider == _RUNTIME_PROVIDER_CUSTOM
        )
        cfg = dict(cfg)
        cfg.update({
            "model": _execution.get("model") or "",
            "provider": _recovered_provider,
            "base_url": (_execution.get("base_url") or "") if _direct_endpoint else "",
            "api_key": "",
            "api_mode": (_execution.get("api_mode") or "") if _direct_endpoint else "",
            "max_iterations": int(
                _execution.get("max_iterations") or DEFAULT_MAX_ITERATIONS
            ),
        })
    if model:
        cfg = dict(cfg)
        cfg["model"] = model
        if provider:
            cfg["provider"] = provider
            cfg["base_url"] = ""
            cfg["api_key"] = ""
            cfg["api_mode"] = ""
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # Model-supplied max_iterations is ignored — the config value is authoritative
    # so users get predictable budgets. The kwarg is retained for internal callers
    # and tests; a model-emitted value here would only shrink the budget and
    # surprise the user mid-run. Log and drop it if one slips through from a
    # cached tool schema or a stale provider.
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; "
            "using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    effective_max_iter = default_max_iter

    # Resolve delegation credentials (provider:model pair).
    # When delegation.provider is configured, this resolves the full credential
    # bundle (base_url, api_key, api_mode) via the same runtime provider system
    # used by CLI/gateway startup.  When unconfigured, returns None values so
    # children inherit from the parent.
    #
    # ``credentials_cfg`` (internal callers only — never model-facing) is a
    # per-call override shaped like the delegation config section
    # ({provider, model, base_url, api_key, api_mode}); the /review engine
    # uses it to route its reviewer subagent onto ``auxiliary.review``
    # without touching the global delegation pin.
    # Keep the route and its fallback policy together through child construction
    # (upstream c47bf78d68): ``routing_cfg`` rides into _build_child_agent.
    routing_cfg = credentials_cfg if credentials_cfg is not None else cfg
    try:
        creds = _resolve_delegation_credentials(routing_cfg, parent_agent)
    except ValueError as exc:
        return tool_error(str(exc))

    # Normalize to task list
    max_children = _get_max_concurrent_children()
    recovered_tasks, tasks_error = _recover_tasks_from_json_string(tasks)
    if tasks_error:
        return tool_error(tasks_error)
    if recovered_tasks is not None:
        tasks = recovered_tasks

    # Small models frequently emit an empty tasks array ([]) alongside a
    # single goal. Treat that as "no batch" instead of letting the batch
    # quality gate below reject the goal-derived single task ("Batch mode
    # requires at least 2 tasks") — the intent is unambiguous.
    if isinstance(tasks, list) and not tasks:
        tasks = None

    if tasks and isinstance(tasks, list):
        if len(tasks) > max_children:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate_task calls, or increase "
                f"delegation.max_concurrent_children in config.yaml."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        single_task: Dict[str, Any] = {
            "goal": goal, "context": context, "role": top_role,
            "inherit_context": inherit_context, "skills": skills,
        }
        if output_schema is not None:
            single_task["output_schema"] = output_schema
        task_list = [single_task]
    else:
        return tool_error(
            "No tasks provided. Pass tasks=[{goal: '...', context: '...'}, "
            "...] — one entry per subagent (a single task is a one-entry "
            "array)."
        )

    if not task_list:
        return tool_error("No tasks provided.")

    # Validate each task has a goal
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(
                f"Task {i} must be an object, got {type(task).__name__}."
            )
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    # Batch-only quality gate: catch malformed fan-outs (placeholder goals,
    # unexpanded multi-word template markers, 1-task batches) before any
    # child is spawned.  The single-`goal` form is deliberately exempt —
    # short goals are valid there.  Duplicate goals are allowed (best-of-N).
    # Inspired by: MoonshotAI/kimi-code agent-swarm.md validation rules (MIT).
    if tasks is not None and isinstance(tasks, list):
        batch_error = _validate_batch_tasks(task_list)
        if batch_error:
            return tool_error(batch_error)

    # T1-24: coerce/validate optional per-task output_schema up front so a
    # malformed schema fails the whole call loudly instead of spawning
    # children that can never satisfy their contract. Runs AFTER the
    # existing goal checks; schema-less tasks resolve to None and take no
    # new code paths downstream.
    from tools.delegation_output_schema import coerce_output_schema

    task_schemas: List[Optional[Dict[str, Any]]] = []
    for i, task in enumerate(task_list):
        raw_schema = task.get("output_schema")
        if raw_schema is None and len(task_list) == 1 and output_schema is not None:
            raw_schema = output_schema
        coerced_schema, schema_err = coerce_output_schema(raw_schema)
        if schema_err:
            return tool_error(f"Task {i} output_schema invalid: {schema_err}")
        task_schemas.append(coerced_schema)
    # Per-task images (upstream f3f5c4f7c7 / 230ca004a7): validated up front; absent on image-less tasks.
    task_images, _images_err = _coerce_task_images(task_list, images)
    if _images_err:
        return tool_error(_images_err)
    # One-shot sessions charge every child against delegation.oneshot_max_children (upstream a79d1d3a71).
    _budget_err = _oneshot_spawn_budget(parent_agent, len(task_list))
    if _budget_err:
        return tool_error(_budget_err)

    overall_start = time.monotonic()
    results = []

    n_tasks = len(task_list)
    # Track goal labels for progress display (truncated for readability)
    task_labels = [t["goal"][:40] for t in task_list]

    # Live transcripts: one pre-headered append-only log per task under
    # cache/delegation/live/<delegation_id>/task-<n>.log so the caller can
    # tail each child's operations while it runs (side-channel only — zero
    # effect on message content or prompt caching). Best-effort: on failure
    # live_paths is empty and delegation proceeds exactly as before.
    from tools.delegation_live_log import (
        create_live_transcripts,
        update_manifest_statuses,
        wrap_progress_callback,
    )

    live_deleg_id, live_writers, live_paths = create_live_transcripts(
        task_list, context, model=creds.get("model"), provider=creds.get("provider")
    )

    recovery_max_spawn_raw = recovery_execution.get("max_spawn_depth")
    recovery_max_spawn = (
        int(recovery_max_spawn_raw)
        if isinstance(recovery_max_spawn_raw, (int, str))
        else None
    )

    # Capture the ORIGINATING session's wake target BEFORE any child agent is
    # constructed: _build_child_agent() -> AIAgent() -> agent_init calls
    # set_current_session_id(child.session_id), which clobbers the
    # HERMES_SESSION_ID ContextVar and os.environ with the subagent's internal
    # id before the background-dispatch code below would read it. The
    # request-scoped chat_id binding (the raw X-Hermes-Session-Id on
    # api_server) is untouched by child construction, so read it here and
    # thread it through the dispatch.
    from tools.async_delegation import _current_origin_session_id

    _origin_wake_sid = _current_origin_session_id()
    try:
        from gateway.session_context import get_session_env

        _origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "")
    except Exception:
        _origin_ui_session_id = ""
    _origin_owner_transport, _origin_owner_session_record = (
        _capture_gateway_steer_authority(_origin_ui_session_id)
    )

    # Build all child agents on the main thread (thread-safe construction).
    # _build_child_preserving_parent_tools saves/restores the parent's
    # resolved tool names around each construction under a lock, so child
    # toolset resolution never leaks into the parent (shared with the plugin
    # subagent-lifecycle API).
    children = []
    for i, t in enumerate(task_list):
        # Per-task role beats top-level; normalise again so unknown
        # per-task values warn and degrade to leaf uniformly.
        effective_role = _normalize_role(t.get("role") or top_role)
        # T1-24: schema'd tasks get the contract appended to their context
        # so the child knows the expected output shape before it starts.
        _task_schema = task_schemas[i] if i < len(task_schemas) else None
        _child_context = t.get("context")
        if _task_schema is not None:
            from tools.delegation_output_schema import append_output_contract

            _child_context = append_output_contract(_child_context, _task_schema)
        try:
            child = _build_child_preserving_parent_tools(
                task_index=i,
                goal=t["goal"],
                context=_child_context,
                # Subagents always inherit the parent's toolsets; the model
                # cannot choose or narrow them (no model-facing toolsets arg).
                toolsets=None,
                model=creds["model"],
                max_iterations=effective_max_iter,
                task_count=n_tasks,
                parent_agent=parent_agent,
                override_provider=creds["provider"],
                override_base_url=creds["base_url"],
                override_api_key=creds["api_key"],
                override_api_mode=creds["api_mode"],
                override_request_overrides=creds.get("request_overrides"),
                explicit_tier_overrides=creds.get("explicit_tier_overrides"),
                override_max_tokens=creds.get("max_output_tokens"),
                override_acp_command=creds.get("command"),
                override_acp_args=creds.get("args"),
                routing_cfg=routing_cfg,
                role=effective_role,
                inherit_context=bool(
                    t.get("inherit_context")
                    if "inherit_context" in t
                    else inherit_context
                ),
                materialized_prefill_messages=t.get("materialized_prefill_messages"),
                recovery_max_spawn_depth=recovery_max_spawn,
                recovery_orchestrator_enabled=(
                    bool(recovery_execution.get("orchestrator_enabled"))
                    if "orchestrator_enabled" in recovery_execution
                    else None
                ),
                skills=(t.get("skills") if "skills" in t else skills),
            )
        except ValueError as exc:
            # Explicit-pin preflight failures (e.g. pinned delegation.command
            # missing from PATH) refuse the spawn loudly (#80450).
            return tool_error(str(exc))
        # Attach the validated schema for the completion-side validation
        # hook in _run_single_child. Absent (None) on schema-less tasks.
        if _task_schema is not None:
            try:
                child._delegate_output_schema = _task_schema
            except Exception:
                logger.debug("Could not attach output schema to child %d", i)
        # Validated per-task images; absent on image-less tasks, which keep the text-only goal turn.
        _t_images = task_images[i] if task_images and i < len(task_images) else None
        if _t_images:
            with _quiet("Could not attach images to child %d", i):
                child._delegate_images = _t_images
        # Tee the child's progress events into its live transcript log.
        # wrap_progress_callback preserves the inner callback contract
        # (including the _flush attribute) and never lets writer failures
        # reach the agent loop. When no parent display exists the inner
        # callback is None and the wrapper still records events.
        _writer = live_writers[i] if i < len(live_writers) else None
        if _writer is not None:
            child.tool_progress_callback = wrap_progress_callback(
                getattr(child, "tool_progress_callback", None), _writer
            )
            child._live_transcript_path = str(_writer.path)
            # Late-completion path finalizes after a timed_out_running return.
            child._live_writer = _writer
        # Delegation identity for the live registry + process-notification
        # attribution (child-started background processes report under it).
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
        children.append((i, t, child))

    # Record an accepted override only after task validation and child construction.
    # A rejected or unbuildable request must not leave a success-shaped audit.
    if audit_reason:
        logger.info("flagship override: delegate_task model=%s provider=%s reason=%s", model, provider, audit_reason)

    def _execute_and_aggregate(
        *, honor_parent_interrupt: bool = True, join_late: bool = False
    ) -> dict:
        """Run all built children (1 or N), join on them, aggregate results,
        fire subagent_stop hooks + cost rollup, and return the combined result
        dict. Used by BOTH the synchronous path and the background runner. In
        the background case this whole function runs on the daemon executor, so
        the parent turn isn't blocked — but the batch still JOINS on itself
        here (all children must finish) before producing ONE consolidated
        results block. That is the contract: fan-out runs in the background,
        waits on each other, and returns together.

        ``join_late``: the caller is an async/recovery unit whose completion
        event is the only delivery. It must not finalize while a
        timed_out_running child is still live, so each such entry is replaced
        by that child's real late result (bounded by the hang ceiling).
        """
        if join_late:
            for _ji, _jt, _jc in children:
                try:
                    _jc._delegate_join_late = True
                except Exception:
                    pass
        if n_tasks == 1:
            # Single task -- run directly (no thread pool overhead)
            _i, _t, child = children[0]
            result = _run_single_child(
                _i,
                _t["goal"],
                child,
                parent_agent,
                owner_session_id=_origin_ui_session_id or None,
                owner_transport=_origin_owner_transport,
                owner_session_record=_origin_owner_session_record,
            )
            results.append(result)
        else:
            # Batch -- run in parallel with per-task progress lines
            completed_count = 0
            spinner_ref = getattr(parent_agent, "_delegate_spinner", None)

            # Daemon workers (tools.daemon_pool): the `with` block still joins
            # normally, but if the parent is interrupted while a child is
            # wedged, the abandoned worker must not block interpreter exit.
            from tools.daemon_pool import DaemonThreadPoolExecutor
            with DaemonThreadPoolExecutor(max_workers=max_children) as executor:
                futures = {}
                for i, t, child in children:
                    child_context = contextvars.copy_context()
                    future = executor.submit(
                        child_context.run,
                        _run_single_child,
                        task_index=i,
                        goal=t["goal"],
                        child=child,
                        parent_agent=parent_agent,
                        owner_session_id=_origin_ui_session_id or None,
                        owner_transport=_origin_owner_transport,
                        owner_session_record=_origin_owner_session_record,
                    )
                    futures[future] = i

                # Poll futures with interrupt checking.  as_completed() blocks
                # until ALL futures finish — if a child agent gets stuck,
                # the parent blocks forever even after interrupt propagation.
                # Instead, use wait() with a short timeout so we can bail
                # when the parent is interrupted.
                # Map task_index -> child agent, so fabricated entries for
                # still-pending futures can carry the correct _delegate_role.
                _child_by_index = {i: child for (i, _, child) in children}

                pending = set(futures.keys())
                while pending:
                    if (
                        honor_parent_interrupt
                        and getattr(parent_agent, "_interrupt_requested", False) is True
                    ):
                        # Parent interrupted — collect whatever finished and
                        # abandon the rest.  Children already received the
                        # interrupt signal; we just can't wait forever.
                        # Reap their subtrees too (detached descendants are
                        # not on any _active_children propagation list).
                        for _ci, _ct, _cc in children:
                            _reap_subtree(_cc, "parent_interrupted")
                        for f in pending:
                            idx = futures[f]
                            if f.done():
                                try:
                                    entry = f.result()
                                except Exception as exc:
                                    entry = {
                                        "task_index": idx,
                                        "status": "error",
                                        "summary": None,
                                        "error": str(exc),
                                        "api_calls": 0,
                                        "duration_seconds": 0,
                                        "_child_role": getattr(
                                            _child_by_index.get(idx), "_delegate_role", None
                                        ),
                                    }
                            else:
                                entry = {
                                    "task_index": idx,
                                    "status": "interrupted",
                                    "summary": None,
                                    "error": "Parent agent interrupted — child did not finish in time",
                                    "api_calls": 0,
                                    "duration_seconds": 0,
                                    "_child_role": getattr(
                                        _child_by_index.get(idx), "_delegate_role", None
                                    ),
                                }
                            results.append(entry)
                            completed_count += 1
                        break

                    from concurrent.futures import wait as _cf_wait, FIRST_COMPLETED

                    done, pending = _cf_wait(
                        pending, timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        try:
                            entry = future.result()
                        except Exception as exc:
                            idx = futures[future]
                            entry = {
                                "task_index": idx,
                                "status": "error",
                                "summary": None,
                                "error": str(exc),
                                "api_calls": 0,
                                "duration_seconds": 0,
                                "_child_role": getattr(
                                    _child_by_index.get(idx), "_delegate_role", None
                                ),
                            }
                        results.append(entry)
                        completed_count += 1

                        # Print per-task completion line above the spinner
                        idx = entry["task_index"]
                        label = (
                            task_labels[idx] if idx < len(task_labels) else f"Task {idx}"
                        )
                        dur = entry.get("duration_seconds", 0)
                        status = entry.get("status", "?")
                        icon = (
                            "✓" if status == "completed"
                            else "…" if status == TIMED_OUT_RUNNING
                            else "✗"
                        )
                        remaining = n_tasks - completed_count
                        completion_line = f"{icon} [{idx+1}/{n_tasks}] {label}  ({dur}s)"
                        if spinner_ref:
                            try:
                                spinner_ref.print_above(completion_line)
                            except Exception:
                                _emit_parent_console(parent_agent, f"  {completion_line}")
                        else:
                            _emit_parent_console(parent_agent, f"  {completion_line}")

                        # Update spinner text to show remaining count
                        if spinner_ref and remaining > 0:
                            try:
                                spinner_ref.update_text(
                                    f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining"
                                )
                            except Exception as e:
                                logger.debug("Spinner update_text failed: %s", e)

            # Sort by task_index so results match input order
            results.sort(key=lambda r: r["task_index"])

        if join_late:
            results[:] = [_join_late_result(_e) for _e in results]

        # Cap subagent summaries against the parent's remaining context
        # headroom (split across the batch) before they enter the parent's
        # conversation. Full text is spilled to disk so nothing is lost.
        # Covers both the single-task and batch paths. See PR #9126.
        _finalize_child_results(results, task_list, children, parent_agent)

        total_duration = round(time.monotonic() - overall_start, 2)

        # Close out the live transcripts: terminal marker per task + manifest
        # status update. The files are retained (retention pruning happens on
        # future dispatches) — they double as the full-fidelity operational
        # record alongside the summary spill files.
        for entry in results:
            _idx = entry.get("task_index", -1)
            _w = (
                live_writers[_idx]
                if isinstance(_idx, int) and 0 <= _idx < len(live_writers)
                else None
            )
            if _w is not None:
                try:
                    _w.finalize(entry)
                except Exception:
                    logger.debug("Live transcript finalize failed", exc_info=True)
                if _idx < len(live_paths):
                    entry["live_transcript"] = live_paths[_idx]
        update_manifest_statuses(live_deleg_id, results)

        combined: Dict[str, Any] = {
            "results": results,
            "total_duration_seconds": total_duration,
        }
        if live_paths:
            combined["live_transcripts"] = list(live_paths)
        return combined

    # ----- Background dispatch: run the WHOLE batch as one async unit -----
    # When background is true, the entire fan-out runs on the daemon executor
    # via a single async delegation. _execute_and_aggregate() joins on every
    # child and produces ONE consolidated results block, which re-enters the
    # conversation as a single message when ALL children finish. The chat is
    # not blocked in the meantime. This is the contract: dispatch N subagents,
    # keep chatting, get the combined summaries back together at the end.
    if background:
        from tools.async_delegation import dispatch_async_delegation_batch
        from tools.approval import get_current_session_key

        # Finite sessions cannot route a detached subagent result back to the
        # agent after their turn/process ends. This includes stateless HTTP
        # requests (#10760) and one-shot Kanban workers (#63169). Fall back to
        # SYNCHRONOUS execution so the result returns in this same turn instead
        # of handing out a handle with no durable consumer. Mirrors the
        # pool-at-capacity inline fallback below.
        try:
            from gateway.session_context import async_delivery_supported
            _async_ok = async_delivery_supported()
        except Exception:
            _async_ok = True

        _wake_sid = ""
        if not _async_ok:
            # The adapter itself cannot push, but if a raw session id is
            # bound (the API server always binds one — see
            # ApiServerAdapter._bind_api_server_session), gateway.wake can
            # still reach the session by self-POSTing /v1/chat/completions
            # with that id in X-Hermes-Session-Id once the batch completes.
            # Only fall back to forced-sync execution when there is truly no
            # session id to wake. Uses the origin captured before child
            # construction (see _origin_wake_sid above) — reading
            # HERMES_SESSION_ID here would return the subagent's internal id.
            _wake_sid = _origin_wake_sid
            if _wake_sid:
                logger.info(
                    "delegate_task: async delivery unsupported on this "
                    "session, but a session id is bound (%s) — dispatching "
                    "in the background and waking the session via self-post "
                    "when it completes instead of forcing synchronous "
                    "execution.",
                    _wake_sid,
                )
                _async_ok = True

        if not _async_ok:
            logger.info(
                "delegate_task: async delivery unsupported on this session "
                "runtime; running the batch synchronously instead."
            )
            _sync_result = _execute_and_aggregate()
            if isinstance(_sync_result, dict):
                _sync_result["note"] = (
                    "background=true is not available in this session — it cannot "
                    "receive a detached subagent result after the turn ends (a "
                    "one-shot runner such as `hermes -z`, a cron job, a Kanban "
                    "worker, or a stateless HTTP endpoint). The subagent(s) ran "
                    "SYNCHRONOUSLY and the result is included above."
                )
            return json.dumps(_sync_result, ensure_ascii=False)

        _session_key = get_current_session_key(default="")
        try:
            from gateway.session_context import get_session_env

            _source = get_session_env("HERMES_SESSION_SOURCE", "")
            # Refresh from the same task-local source when available, but retain
            # the immutable value captured before child construction otherwise.
            _origin_ui_session_id = (
                get_session_env("HERMES_UI_SESSION_ID", "") or _origin_ui_session_id
            )
            # In desktop/TUI, the routable session key is the durable
            # AIAgent.session_id. Context compression can rotate that id during
            # the same turn before the TUI-side session dict is re-anchored;
            # if we capture the stale approval/session context key here, the
            # async completion becomes an orphan and any desktop poller may
            # consume it. Gateway chats are different: their session_key is the
            # platform conversation key (agent:main:...), so keep it there.
            if _source == "tui":
                _agent_session_id = str(getattr(parent_agent, "session_id", "") or "")
                if _agent_session_id:
                    _session_key = _agent_session_id
        except Exception:
            _source = ""
        if not _session_key:
            # CLI (single-process) path: the approval contextvar is only bound
            # during gateway/TUI turns and HERMES_SESSION_KEY is not in the CLI
            # environment, so the key resolves empty here. Since #64240 the CLI
            # drains completions through a positive-ownership filter keyed on
            # the durable AIAgent.session_id — an empty session_key would fail
            # closed and the CLI could never claim its own completions, while
            # a restored foreign event with an empty key could leak into any
            # unfiltered consumer (#64484). Stamp the parent's durable session
            # id instead; compression rotations are handled on the drain side
            # via resolve_resume_session_id lineage resolution.
            _agent_session_id = str(getattr(parent_agent, "session_id", "") or "")
            if _agent_session_id:
                _session_key = _agent_session_id
        _parent_session_id = getattr(parent_agent, "session_id", None)
        _child_agents = [c for (_, _, c) in children]

        # Detach every child from the parent's interrupt-propagation list — the
        # batch's lifecycle is owned by the async registry now, not the parent
        # turn. _build_child_agent attached them (correct for sync runs).
        if hasattr(parent_agent, "_active_children"):
            _ac_lock = getattr(parent_agent, "_active_children_lock", None)
            for _c in _child_agents:
                try:
                    if _ac_lock:
                        with _ac_lock:
                            parent_agent._active_children.remove(_c)
                    else:
                        parent_agent._active_children.remove(_c)
                except ValueError:
                    pass

        def _batch_runner():
            # This batch is detached from the foreground turn. Its lifecycle is
            # owned by the async registry and cancelled only via _batch_interrupt.
            return _execute_and_aggregate(
                honor_parent_interrupt=False, join_late=True
            )

        def _batch_interrupt():
            for _c in _child_agents:
                try:
                    interrupted = request_hard_interrupt(_c, "Async delegation cancelled")
                    if not interrupted and hasattr(_c, "_interrupt_requested"):
                        _c._interrupt_requested = True
                except Exception:
                    pass
                _reap_subtree(_c, "async_cancelled")

        def _batch_progress():
            # Progress token for the async registry's stale monitor: the
            # combined (api_call_count, current_tool, last_activity_ts) of
            # every child. last_activity_ts is ticked by _touch_activity on
            # every streamed chunk ("receiving stream response"), every tool
            # transition, and every API-call start/completion — so a child
            # streaming a long response is alive even though api_call_count
            # only advances when the call completes (same liveness signal as
            # the compaction inactivity budget, PR #71508). A fully frozen
            # token past the stale threshold means the detached batch is
            # wedged (e.g. stuck inside the first model API call — #60203).
            # in_tool=True while ANY child is inside a tool so legitimately
            # slow tools get the higher staleness ceiling, mirroring the
            # sync-path heartbeat monitor.
            parts = []
            in_tool = False
            for _c in _child_agents:
                try:
                    _summary = _c.get_activity_summary()
                    _tool = _summary.get("current_tool")
                    parts.append(
                        (
                            _summary.get("api_call_count", 0),
                            _tool,
                            _summary.get("last_activity_ts"),
                        )
                    )
                    in_tool = in_tool or bool(_tool)
                except Exception:
                    parts.append(None)
            return tuple(parts), in_tool

        _goals = [t["goal"] for t in task_list]
        try:
            _durable_spec, _gateway_boot_id = _build_durable_background_spec(
                task_list=task_list,
                shared_context=context,
                top_role=top_role,
                inherit_context=inherit_context,
                cfg=cfg,
                creds=creds,
                parent_agent=parent_agent,
                session_key=_session_key,
                parent_session_id=_parent_session_id,
                origin_ui_session_id=_origin_ui_session_id,
                origin_session_id=_wake_sid,
                max_iterations=effective_max_iter,
                children=_child_agents,
            )
        except Exception as exc:
            logger.error(
                "delegate_task: durable restart intent unavailable (%s); "
                "running synchronously rather than launching untracked work",
                exc,
            )
            _sync_result = _execute_and_aggregate()
            if isinstance(_sync_result, dict):
                _sync_result["note"] = (
                    "Restart-safe background persistence was unavailable, so "
                    "the subagent(s) ran SYNCHRONOUSLY and the result is included above."
                )
            return json.dumps(_sync_result, ensure_ascii=False)
        dispatch = dispatch_async_delegation_batch(
            goals=_goals,
            context=context,
            # Metadata for the completion block only; subagents inherit the
            # parent's toolsets (no model-facing toolsets arg).
            toolsets=None,
            role=top_role,
            model=creds["model"],
            session_key=_session_key,
            origin_ui_session_id=_origin_ui_session_id,
            origin_session_id=_wake_sid,
            parent_session_id=_parent_session_id,
            runner=_batch_runner,
            interrupt_fn=_batch_interrupt,
            max_async_children=_get_max_async_children(),
            durable_spec=_durable_spec,
            current_boot_id=_gateway_boot_id,
            # Reuse the live-transcript directory's id (when created) so the
            # returned delegation_id matches cache/delegation/live/<id>/.
            delegation_id=live_deleg_id,
            progress_fn=_batch_progress,
        )

        if (
            _durable_spec is not None
            and dispatch.get("status") == "rejected"
            and dispatch.get("reason") in {"registry_cap", "registry_error"}
        ):
            _sync_result = _execute_and_aggregate()
            if isinstance(_sync_result, dict):
                _sync_result["note"] = (
                    "Restart-safe background persistence was unavailable, so "
                    "the subagent(s) ran SYNCHRONOUSLY and the result is included above."
                )
            return json.dumps(_sync_result, ensure_ascii=False)

        if dispatch.get("status") == "dispatched":
            n = len(_goals)
            note = (
                "Subagent is running in the background. You and the user can "
                "keep working; its full result re-enters the conversation as a "
                "new message when it finishes. Do not wait or poll — just "
                "continue."
                if n == 1 else
                f"{n} subagents are running in parallel in the background. You "
                f"and the user can keep working; they wait on each other and "
                f"their consolidated results re-enter the conversation as a "
                f"single message once ALL of them finish. Do not wait or poll "
                f"— just continue."
            )
            payload = {
                "status": "dispatched",
                "mode": "background",
                "count": n,
                "delegation_id": dispatch["delegation_id"],
                "goals": _goals,
                "note": note,
            }
            _sids = [
                getattr(_c, "_subagent_id", None) for _c in _child_agents
            ]
            if any(isinstance(s, str) and s for s in _sids):
                payload["subagent_ids"] = _sids
                payload["control_hint"] = (
                    "While a child runs you can orchestrate it live with this "
                    "same tool: delegate_task(action='list') to see live "
                    "children, action='steer' with subagent_id + message to "
                    "redirect one, action='stop' with subagent_id to end one "
                    "early."
                )
            if live_paths:
                payload["live_transcripts"] = list(live_paths)
                payload["live_transcripts_hint"] = (
                    "Each subagent streams a human-readable transcript of its "
                    "operations to the file listed above (append-only, one per "
                    "task). Read or `tail -f` these paths at any time to watch "
                    "a child work while it runs."
                )
            return json.dumps(payload, ensure_ascii=False)

        # Pool at capacity / schedule failure — children are still attached
        # (we detach above only on the parent list, but the async unit was
        # never accepted, so re-attaching isn't needed: we just run inline).
        logger.info(
            "delegate_task: async pool at capacity (%s); running the whole "
            "batch synchronously instead.",
            dispatch.get("error", "rejected"),
        )
        _cap_result = _execute_and_aggregate()
        _fallback_event_id = dispatch.get("fallback_event_id")
        if _fallback_event_id:
            from tools.async_delegation import acknowledge_outbox_event

            acknowledge_outbox_event(
                _fallback_event_id,
                outcome="dropped",
                reason="fallback_ran",
                profile_home=dispatch.get("_registry_profile_home") or None,
            )
        if isinstance(_cap_result, dict):
            _cap_result["note"] = (
                "The background delegation pool was at capacity "
                "(delegation.max_concurrent_children), so the subagent(s) ran "
                "SYNCHRONOUSLY and the result is included above. Raise "
                "delegation.max_concurrent_children in config.yaml to allow "
                "more concurrent background delegations."
            )
        return json.dumps(_cap_result, ensure_ascii=False)

    # ----- Synchronous path -----
    # A recovered async unit (_recovery_spec) delivers only via its own
    # completion event, so it joins timed_out_running children too.
    return json.dumps(
        _execute_and_aggregate(join_late=bool(_recovery_spec)), ensure_ascii=False
    )


# ── OpenAI function-calling schema ──────────────────────────────────────────

def _build_top_level_description(*, independent_completions=None) -> str:
    """delegate_task description: ONLY guidance stated nowhere else in the schema
    (limits live in the 'tasks' parameter description, rebuilt per get_definitions())."""
    try:
        orchestration_available = _get_max_spawn_depth() >= 2 and _get_orchestrator_enabled()
    except Exception:
        orchestration_available = False
    # Mention recursion only where it's actually available. send_message is deliberately not named (gateway-internal
    # vocabulary); model_tools session-filters the list to tools the session has.
    # Delegation is opt-in per child via role='orchestrator' (fork #1541): mention recursion only where available.
    if orchestration_available:
        restrictions_rule = (
            "- Children cannot call clarify, memory, or cronjob.\n"
            "- Children cannot delegate unless you pass role='orchestrator' "
            f"(tree capped at max_spawn_depth={_get_max_spawn_depth()}).\n"
        )
    else:
        restrictions_rule = "- Children cannot call delegate_task, clarify, memory, or cronjob.\n"
    from tools.delegate_tool_config import _get_independent_completions

    if independent_completions is None:
        independent_completions = _get_independent_completions()
    delivery = (
        "each ungrouped task / `group` returns on its own"
        if independent_completions else "one message per call"
    )
    return _DESCRIPTION_HEAD.format(delivery=delivery) + restrictions_rule + _DESCRIPTION_TAIL

_DESCRIPTION_HEAD = (
    "Spawn subagents in isolated contexts; each gets its own conversation, terminal session, and toolset, and only its "
    "final summary returns to you. Pass every task in `tasks` — one entry spawns one subagent, several run in parallel "
    "(limit in the tasks description).\n\n"
    "Sessions without a later-result consumer (including one-shot CLI and cron) join parallel children "
    "and return results in this tool call. "
    "Otherwise runs in the background: dispatch returns live transcript paths and results re-enter "
    "as a new message when subagents finish ({delivery}). Background results are delivered only "
    "BETWEEN your turns: finish whatever does not depend on them, then give a one-line status and END YOUR TURN. Never "
    "wait or poll on transcripts, artifact files, or CI for a child. "
    "While children run, `action` (list/steer/stop) controls them live. "
    "A result with status 'timed_out_running' means timed out ≠ dead: that child is still working and delivers "
    "later; never re-delegate its task, use `action` on the listed ids.\n\n"
    "USE FOR: reasoning-heavy subtasks, work that would flood your context, or independent parallel workstreams.\n"
    "DO NOT USE FOR (use these instead):\n"
    "- Mechanical multi-step work with no reasoning needed -> execute_code\n"
    "- A single tool call -> call the tool directly\n"
    "- Tasks needing user interaction -> subagents cannot ask questions\n"
    "- Durable work that must survive this session -> cronjob or terminal(background=True, notify=True); /stop, /new, "
    "or process exit halts running subagents (whole tree); each returns an 'interrupted' completion with partial output.\n\n"
    "RULES:\n"
    "- Children know nothing of this conversation: pass everything needed via 'context', including any required "
    "output language, tone, or style (e.g. \"respond in Chinese\").\n"
    "- Child summaries are SELF-REPORTS, not verified facts: a child claiming \"uploaded successfully\" or "
    "\"file written\" may be wrong. For external side effects (uploads, remote writes, publishing), require a "
    "verifiable handle (URL, ID, absolute path) and verify it yourself before telling the user the operation "
    "succeeded.\n"
    "- Children cannot close tracked work: a child asked to close it returns findings instead; "
    "the parent applies the transition.\n"
)

_DESCRIPTION_TAIL = (
    "- Children inherit the parent model unless pinned via delegation.provider / delegation.model in config.yaml."
)

def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"The task(s), up to {max_children} in parallel for this user (set "
        "via delegation.max_concurrent_children). Each entry spawns one "
        "subagent with isolated context and terminal session; a single task "
        "is a one-entry array. Required when spawning."
    )

_ROLE_PARAM_DESCRIPTION = (
    "leaf (default): does the work itself, cannot delegate. orchestrator: "
    "may fan out; use only for a lead that must decompose. "
    "Gathering/research/coding workers are leaves."
)

def _build_role_param_description() -> str:
    """Description of the `role` parameter (top-level and per-task).

    Orchestrator is explicit opt-in: a child is a leaf unless its parent
    passes role='orchestrator'; delegation.max_spawn_depth is the ceiling
    and delegation.orchestrator_enabled the kill switch (see the
    role-resolution block in _build_child_agent). Kept as a function
    because external callers import this symbol.
    """
    return _ROLE_PARAM_DESCRIPTION

def _build_dynamic_schema_overrides() -> dict:
    """Per-call schema overrides (ToolEntry.dynamic_schema_overrides): every
    get_definitions() pass rewrites the descriptions to the user's actual limits."""
    from tools.delegate_tool_config import _get_independent_completions

    independent_completions = _get_independent_completions()
    overrides_params = {**DELEGATE_TASK_SCHEMA["parameters"]}
    # Copy properties so the static schema dict is never mutated.
    overrides_params["properties"] = {k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()}
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()

    if not independent_completions:
        tasks = overrides_params["properties"]["tasks"]
        tasks["items"] = {**tasks["items"], "properties": {
            k: v for k, v in tasks["items"]["properties"].items() if k != "group"
        }}

    return {
        "description": _build_top_level_description(independent_completions=independent_completions),
        "parameters": overrides_params,
    }

def _p(type_: str, description: str, **extra) -> dict:
    return {"type": type_, **extra, "description": description}

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # description / tasks.description are placeholders: the real text is built per get_definitions() call by
    # _build_dynamic_schema_overrides() so the model sees the user's actual max_concurrent_children / max_spawn_depth.
    # Lazy (not at import) so cli.CLI_CONFIG isn't forced to load before the test conftest redirects HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # The handler also accepts the legacy single-goal shape (top-level `goal`/`context`/`output_schema`),
            # wrapped into a one-entry batch at dispatch, and a per-task `role` (legacy, ignored: capability is
            # depth-derived). Both unadvertised on purpose (old transcripts only); do not re-add. No maxItems — the
            # runtime limit (delegation.max_concurrent_children) is enforced with a clear error in delegate_task().
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": _p(
                            "string",
                            "What this subagent should accomplish. Be specific and self-contained — it knows "
                            "nothing about your conversation history.",
                        ),
                        "context": _p(
                            "string",
                            "Background THIS child needs: file paths, error messages, constraints. Each child "
                            "sees only its own context — repeat shared background in every task that needs it.",
                        ),
                        "output_schema": _p(
                            "object",
                            "Optional JSON Schema this child's final answer must validate against (told to the "
                            "child up front; parent validates with one bounded correction retry; result gains "
                            "schema_valid, plus schema_errors on failure — the child's raw text is still returned "
                            "as summary, never discarded). Keep it forgiving — require only fields you will read.",
                        ),
                        "images": _p(
                            "array",
                            "Optional images this child must SEE (max 8): local file paths or http(s) URLs — e.g. a "
                            "screenshot the user sent, a design mock, a chart. Vision-capable children receive the "
                            "pixels on their first turn; non-vision children get path hints for vision_analyze. Text "
                            "files do NOT belong here — put paths in 'context' instead.",
                            items={"type": "string"},
                        ),
                        "group": _p(
                            "string",
                            "Optional result-delivery bucket within this call (only when delegation.independent_completions "
                            "is enabled; otherwise the whole call returns as one message). Tasks sharing a group return "
                            "together in ONE message; ungrouped tasks return individually as each finishes. This does not "
                            "order execution; if B needs A's output, dispatch B after A returns.",
                        ),
                        # Fork: per-task overrides of the top-level boomerang / skill-promotion / role params.
                        "inherit_context": _p(
                            "boolean",
                            "Per-task boomerang inheritance override. See top-level 'inherit_context'. Defaults to the "
                            "top-level value.",
                        ),
                        "skills": _p(
                            "array", "Per-task skill promotion override. See top-level 'skills'.", items={"type": "string"},
                        ),
                        "role": _p(
                            "string", "Per-task role; overrides the top-level 'role'.", enum=["leaf", "orchestrator"],
                        ),
                    },
                    "required": ["goal"],
                },
                "description": "(rebuilt at get_definitions() time)",
            },
            # Fork (boomerang / skill promotion / explicit orchestrator opt-in #1541 / audited model override).
            "inherit_context": _p(
                "boolean",
                "Boomerang: when true, fold this conversation's recent history into a single background context "
                "message the subagent inherits, so it sees the current session state without you writing a brief. "
                "Default false (the subagent starts blank + your goal/context).",
            ),
            "skills": _p(
                "array",
                "Skill names to promote in the subagent's skill index. Subagents get a compact names-only index by "
                "default; skills named here keep their full descriptions so the child loads them reliably. Pass the "
                "skills the task's domain needs (e.g. ['systematic-debugging']). The child can still browse/load ANY "
                "skill via skills_list/skill_view.",
                items={"type": "string"},
            ),
            "role": _p("string", _ROLE_PARAM_DESCRIPTION, enum=["leaf", "orchestrator"]),
            "model": _p("string", "Optional per-call model override. Flagship models require allow_flagship_reason."),
            "provider": _p("string", "Provider for per-call model override; requires model."),
            "allow_flagship_reason": _p(
                "string", "Nonblank audited justification for an explicit flagship model override.",
            ),
            # `background` (bool) is also accepted — DEPRECATED, ignored: top-level
            # delegations always run in the background. Unadvertised; do not re-add.
            "action": _p(
                "string",
                "Default 'spawn'. Live control of running children: "
                "'list' = ids/goals/status/transcripts; 'steer' = queue "
                "course-correction text into one child (subagent_id + "
                "message) without stopping it; 'stop' = end one child "
                "early (subagent_id; partial result still returns). "
                "Control actions return immediately; goal/tasks are ignored unless spawning.",
                enum=["spawn", "list", "steer", "stop"],
            ),
            "subagent_id": _p("string", "Target for action='steer'/'stop' (ids from the spawn response or action='list')."),
            "message": _p(
                "string",
                "For action='steer': the course correction, appended to "
                "the child's next tool result mid-run. Be directive and specific.",
            ),
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

def _model_background_value(args: dict, parent_agent=None) -> bool:
    """Background flag for the MODEL-facing dispatch path (registry fallback). Top-level delegations always run in the
    background — the model does not choose — for single tasks and fan-out batches alike (one async unit, one
    consolidated result); an orchestrator subagent (depth > 0) is the exception since it needs its workers' results
    within its own turn. The live path is ``run_agent._dispatch_delegate_task``; this mirrors it for the rare case
    the intercept is bypassed. Direct Python callers keep the synchronous default."""
    return not getattr(parent_agent, "_delegate_depth", 0) > 0

_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}

def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    """Drop trusted-config-only task fields from model-supplied tasks (same list object back when nothing changed)."""
    if not isinstance(tasks, list) or not any(isinstance(t, dict) and _MODEL_HIDDEN_TASK_FIELDS & t.keys() for t in tasks):
        return tasks
    return [{k: v for k, v in t.items() if k not in _MODEL_HIDDEN_TASK_FIELDS} if isinstance(t, dict) else t for t in tasks]


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    # Reject undeclared args (e.g. an imaginary per-call `model=`): the handler below
    # reads args by name, so anything not in the schema would be silently dropped
    # and the children would run on the config default (2026-09-09).
    strict_args=True,
    # The legacy single-goal shape is accepted by the handler but deliberately kept off
    # the model-facing schema (see the NOTE on DELEGATE_TASK_SCHEMA); list it here so
    # strict mode never rejects a valid goal= call.
    extra_accepted_args=["goal", "context", "role", "max_iterations", "background", "output_schema", "images"],
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"), context=args.get("context"), tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"), role=args.get("role"),
        background=_model_background_value(args, kw.get("parent_agent")),
        inherit_context=args.get("inherit_context"), skills=args.get("skills"), output_schema=args.get("output_schema"),
        model=args.get("model"), provider=args.get("provider"), allow_flagship_reason=args.get("allow_flagship_reason"),
        images=args.get("images"), action=args.get("action"), subagent_id=args.get("subagent_id"), message=args.get("message"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)
