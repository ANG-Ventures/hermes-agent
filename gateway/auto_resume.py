"""Safety helpers for gateway interrupted-turn auto-continuation.

There are two distinct resume kinds:

* SIBLING auto-continue resumes a different session amputated by a gateway
  drain timeout.
* SELF resume-handoff starts a fresh synthesized turn for the session that
  intentionally initiated the restart, preserving its handoff note.

This module supports the existing boot-resume scheduler in ``gateway.run``. It
classifies persisted tool-call tails, names the taxonomy, and stores the
once-ever SIBLING auto-resume credit; it does not schedule turns itself.

Session counters live in a separate sessions-v2 ledger, migrated once from v1.
Rollback to any legacy release is supported: legacy gateways do not enforce the
v2 cap and never open its ledger. Rolling forward resumes from persisted v2
counts (legacy-era attempts are not counted). Legacy repairs cannot erase v2;
a reader seeing empty legacy counters alongside retained v2 counters warns once
and trusts v2. The seven-day TTL still intentionally refills the cap.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
import threading
from functools import wraps
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from agent.replay_cleanup import is_interrupted_tool_result

logger = logging.getLogger(__name__)

RESUME_KIND_SIBLING = "sibling"
RESUME_KIND_SELF = "self"

_SELF_RESUME_REASONS = frozenset(
    {
        "restart_interrupted",
        "restart_consumed_interrupted",
    }
)


def resume_kind_for_reason(reason: str | None) -> str:
    """Return the authority-table resume kind for a persisted reason."""
    return RESUME_KIND_SELF if reason in _SELF_RESUME_REASONS else RESUME_KIND_SIBLING

AUTO_RESUME_ATTEMPT_TTL_SECONDS = 7 * 24 * 60 * 60
_STORE_VERSION = 1

# How many consecutive boot auto-resumes of the SAME session may be scheduled
# before the gateway stops resuming it. The once-ever credit above is keyed on
# ``(session_key, assistant_rowid)``, which is the wrong grain for a session
# that is re-amputated every boot: each restart interrupts a NEW assistant row,
# so the credit is fresh every time, and a ``kind=self`` resume skips the credit
# entirely. Both were live on 2026-09-20, when one dead Discord session replayed
# its whole ~450k-char history across ten overnight boots. The counter here is
# per-SESSION and boot-independent; forward progress on a resumed turn clears it
# (see ``GatewayRunner._apply_post_turn_resume_gate``), so only resumes that
# achieved nothing accumulate. ``<= 0`` disables the cap.
DEFAULT_AUTO_RESUME_MAX_ATTEMPTS = 3

# Auto-resume is allowlist-based.  Unknown tools fail closed because plugins and
# MCP servers can expose arbitrary side effects under names core cannot classify.
_READ_ONLY_TOOLS = frozenset(
    {
        "browser_get_images",
        "browser_snapshot",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
        "lcm_describe",
        "lcm_doctor",
        "lcm_expand",
        "lcm_expand_query",
        "lcm_grep",
        "lcm_load_session",
        "lcm_status",
        "mem0_profile",
        "mem0_search",
        "read_file",
        "search_files",
        "session_search",
        "skill_view",
        "skills_list",
        "vision_analyze",
        "web_extract",
        "web_search",
    }
)

# These known core surfaces mutate state or can dispatch mutations.  A persisted
# result proves completion and is safe to continue past; a missing result is the
# exact ambiguous tail the mechanical gate must stop.
_MUTATING_TOOLS = frozenset(
    {
        "browser_back",
        "browser_click",
        "browser_navigate",
        "browser_press",
        "browser_scroll",
        "browser_type",
        "clarify",
        "delegate_task",
        "execute_code",
        "ha_call_service",
        "kanban_block",
        "kanban_comment",
        "kanban_complete",
        "kanban_create",
        "kanban_heartbeat",
        "kanban_link",
        "memory",
        "mem0_conclude",
        "patch",
        "process",
        "skill_manage",
        "terminal",
        "todo",
        "write_file",
    }
)


@dataclass(frozen=True)
class InterruptedTurnAssessment:
    """Mechanical auto-resume disposition for one persisted interrupted turn."""

    turn_rowid: int | None
    auto_eligible: bool
    suspect_tool: str | None = None
    reason: str | None = None


# A finish_reason that proves the assistant ended its turn on purpose. Every
# other value (None, "tool_calls", "interrupt_close", "verification_required",
# anything a future provider invents) is treated as unproven and therefore
# resumable — this gate only ever SKIPS work on positive evidence.
_COMPLETED_FINISH_REASONS = frozenset({"stop"})

# Rows the gateway persists as bookkeeping, never as conversation. run.py
# appends a ``session_meta`` row (model, platform) AFTER a new session's first
# turn is persisted, so every single-turn transcript ends
# ``assistant(stop) -> session_meta``; system injections (restart notes) are
# likewise dropped before the agent sees history (``_last_transcript_timestamp``
# in gateway/run.py skips the same set). Judging ``rows[-1]`` without skipping
# these made every finished first-turn session look unanswered and fail closed
# into a full resume turn (2026-09-20: four fresh sessions re-prompted on one
# marker-less boot, one of them ``/stop``ped).
_TRANSCRIPT_METADATA_ROLES = frozenset({"session_meta", "system"})


def has_resumable_work(messages: Iterable[dict[str, Any]]) -> bool:
    """True when the persisted tail leaves work a boot-resume turn could continue.

    ``resume_pending`` is a HEDGE, not a diagnosis: ``stop()`` marks every
    running session before the drain so a SIGKILL mid-drain cannot lose
    in-flight work, and ``suspend_recently_active()`` re-marks everything that
    looked recently active after an unclean exit. Both are correct. What is
    missing is the counterpart: when the process is killed before the drain's
    clear-the-hedge pass runs, sessions whose turn had ALREADY finished keep
    the marker, and the next boot spends a full LLM turn "recovering" a
    conversation that ended with a delivered answer.

    The persisted transcript is the ground truth the marker lacks. This answers
    the only question that matters at schedule time — *is anything actually
    unfinished?* — and it fails CLOSED: anything unrecognized, unreadable, or
    ambiguous reports True and resumes exactly as before. A skip requires
    positive proof of completion: the last row is an assistant message, with no
    unanswered tool calls, non-empty content, and an explicit ``stop``.
    """
    rows = [
        row
        for row in messages
        if isinstance(row, dict)
        and row.get("role") not in _TRANSCRIPT_METADATA_ROLES
    ]
    if not rows:
        # No conversational transcript to reason about — keep today's behaviour.
        return True

    tail = rows[-1]
    if tail.get("role") != "assistant":
        # A trailing user/tool/other row means the assistant never answered it.
        return True
    if tail.get("tool_calls") not in (None, [], "", {}):
        # Tool calls with no results after them: the turn was cut mid-flight.
        return True
    if tail.get("finish_reason") not in _COMPLETED_FINISH_REASONS:
        return True

    content = tail.get("content")
    if isinstance(content, str):
        return not content.strip()
    # Non-string content (native blocks) counts as delivered when non-empty.
    return content in (None, [], {}, "")


def _message_id(row: dict[str, Any]) -> int | None:
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_nonempty_human_message(row: dict[str, Any]) -> bool:
    if row.get("role") != "user":
        return False
    content = row.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return content not in (None, [], {})


def _turn_segment(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [row for row in messages if isinstance(row, dict)]
    start = 0
    for index, row in enumerate(rows):
        if _is_nonempty_human_message(row):
            start = index
    return rows[start:]


def _stable_turn_rowid(rows: list[dict[str, Any]]) -> int | None:
    """Return the original interrupted turn's cross-boot-stable assistant rowid.

    Startup resume notes are API-only and persist an empty user row.  On a second
    interruption, assistant rows have been appended after that synthetic boundary;
    choosing the last assistant would mint a fresh credit.  The assistant immediately
    before the first empty synthetic user remains the original turn's last assistant
    row across every restart.  Before any synthetic boundary exists, the first
    ``interrupt_close`` row is the original interruption marker; otherwise use the
    current last assistant row.
    """

    for index, row in enumerate(rows):
        if row.get("role") == "user" and row.get("content") == "":
            prior = [
                _message_id(candidate)
                for candidate in rows[:index]
                if candidate.get("role") == "assistant"
            ]
            prior = [rowid for rowid in prior if rowid is not None]
            return prior[-1] if prior else None

    interrupted = [
        _message_id(row)
        for row in rows
        if row.get("role") == "assistant"
        and row.get("finish_reason") == "interrupt_close"
    ]
    interrupted = [rowid for rowid in interrupted if rowid is not None]
    if interrupted:
        return interrupted[0]

    assistants = [
        _message_id(row) for row in rows if row.get("role") == "assistant"
    ]
    assistants = [rowid for rowid in assistants if rowid is not None]
    return assistants[-1] if assistants else None


def _tool_name(call: Any) -> tuple[str | None, str | None]:
    if not isinstance(call, dict):
        return None, None
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id:
        return None, None
    if not isinstance(function, dict):
        return None, call_id
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, call_id
    return name.strip(), call_id


def assess_interrupted_turn(
    messages: Iterable[dict[str, Any]],
) -> InterruptedTurnAssessment:
    """Classify a persisted interrupted-turn tail conservatively.

    Read-only calls may be incomplete.  Known mutating calls require a matching
    persisted tool-result row.  Unknown or malformed calls and orphaned results are
    ambiguous and therefore prompt-only.
    """

    rows = _turn_segment(messages)
    turn_rowid = _stable_turn_rowid(rows)
    if turn_rowid is None:
        return InterruptedTurnAssessment(
            turn_rowid=None,
            auto_eligible=False,
            reason="missing persisted assistant rowid",
        )

    results: dict[str, list[tuple[int, Any]]] = {}
    for index, row in enumerate(rows):
        if row.get("role") != "tool":
            continue
        call_id = row.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            return InterruptedTurnAssessment(
                turn_rowid=turn_rowid,
                auto_eligible=False,
                reason="unclassifiable tool result",
            )
        results.setdefault(call_id, []).append((index, row.get("content")))

    seen_call_ids: set[str] = set()
    for index, row in enumerate(rows):
        raw_calls = row.get("tool_calls")
        if raw_calls in (None, []):
            continue
        if row.get("role") != "assistant" or not isinstance(raw_calls, list):
            return InterruptedTurnAssessment(
                turn_rowid=turn_rowid,
                auto_eligible=False,
                reason="unclassifiable tool call",
            )
        for call in raw_calls:
            name, call_id = _tool_name(call)
            if name is None or call_id is None:
                return InterruptedTurnAssessment(
                    turn_rowid=turn_rowid,
                    auto_eligible=False,
                    suspect_tool=name,
                    reason="unclassifiable tool call",
                )
            if call_id in seen_call_ids:
                return InterruptedTurnAssessment(
                    turn_rowid=turn_rowid,
                    auto_eligible=False,
                    suspect_tool=name,
                    reason="duplicate tool call id",
                )
            seen_call_ids.add(call_id)
            completed = any(
                position > index and not is_interrupted_tool_result(content)
                for position, content in results.get(call_id, [])
            )
            if name in _READ_ONLY_TOOLS:
                continue
            if name in _MUTATING_TOOLS:
                if completed:
                    continue
                return InterruptedTurnAssessment(
                    turn_rowid=turn_rowid,
                    auto_eligible=False,
                    suspect_tool=name,
                    reason=f"incomplete mutating tool call: {name}",
                )
            return InterruptedTurnAssessment(
                turn_rowid=turn_rowid,
                auto_eligible=False,
                suspect_tool=name,
                reason=f"unclassifiable tool call: {name}",
            )

    orphaned = set(results) - seen_call_ids
    if orphaned:
        return InterruptedTurnAssessment(
            turn_rowid=turn_rowid,
            auto_eligible=False,
            reason="unclassifiable orphaned tool result",
        )

    return InterruptedTurnAssessment(turn_rowid=turn_rowid, auto_eligible=True)


def _serialized_store_call(method):
    """Serialize read/modify/write across dispatch workers sharing one store."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class AutoResumeAttemptStore:
    """Seven-day durable once-ever auto-resume credits plus per-session counters.

    Two distinct store faults, with deliberately different handling, because
    both directions of "just latch it off" were measured causing real harm on
    PR #761:

    * **Unreadable** (torn write, bad edit, truncation). This file is a counter
      cache, not precious data, so it is REPAIRED: reset to an empty valid
      store and carry on counting. Latching reads off instead left the
      per-session cap permanently disabled, which reproduced the very replay
      storm the cap exists to stop (10/10 uncapped boot resumes). The credits
      recorded before the reset are genuinely lost, so ``has_attempt`` — and
      only ``has_attempt`` — keeps failing closed for the rest of the process
      rather than minting fresh ones off an emptied file.
    * **Unwritable** (perms, full disk, bad chown). This cannot self-heal, and
      an unrecordable attempt means the cap has no way to bound anything. The
      store reports that honestly via :meth:`session_resume_verdict` so the
      scheduler can stop replaying instead of replaying silently forever. It
      does NOT retire ``resume_pending``: the transcript is untouched and the
      next real user message continues the conversation, so the denial is
      reversible the moment the disk is.
    """

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()
        self.path = Path(path)
        self.session_path = self.path.with_name(self.path.stem + ".sessions-v2.json")
        self._warned_legacy_reset = False
        self._legacy_extra = {}
        self._now = now
        # Rowid credits were destroyed by a repair; has_attempt must not mint
        # fresh ones off the emptied file for the remainder of this process.
        self._credits_lost = False
        # Persistence is known-broken: nothing this store is told can be
        # recorded, so nothing it reports can bound anything.
        self._degraded = False
        # A successful write proves the path is persistable; probed lazily once.
        self._persist_proven = False
        self._warned_unreadable = False
        self._warned_degraded = False

    # ---- fault handling ---------------------------------------------------

    def _warn_unreadable(self, exc: Exception | str) -> None:
        if self._warned_unreadable:
            return
        self._warned_unreadable = True
        logger.warning(
            "%s was unreadable and has been reset to an empty counter store "
            "(%s). Per-turn auto-resume credits recorded before the reset are "
            "lost, so interrupted turns fall back to prompt mode for the rest "
            "of this process; per-session boot-resume counting continues from "
            "zero.",
            self.path.name,
            exc,
        )

    def _warn_degraded(self, exc: Exception | str) -> None:
        if self._warned_degraded:
            return
        self._warned_degraded = True
        logger.warning(
            "%s cannot be written (%s); the per-session boot auto-resume cap "
            "has no durable counter, so boot resumes are SKIPPED (resume_pending "
            "is left set and the transcript is untouched) rather than replayed "
            "unbounded. Fix the permissions or free space at %s.",
            self.path.name,
            exc,
            self.path.parent,
        )

    def _degrade(self, exc: Exception) -> None:
        self._degraded = True
        self._persist_proven = False
        self._warn_degraded(exc)

    def _persist(
        self,
        attempts: list[dict[str, Any]],
        session_attempts: dict[str, dict[str, Any]],
    ) -> bool:
        """Write the store, converting a write failure into the degraded latch.

        Never raises: a failed write must not be mistaken for an unreadable
        file (which would trigger a repair that empties a perfectly good store
        on a host whose only problem is a read-only directory).
        """
        try:
            self._write(attempts, session_attempts)
        except Exception as exc:
            self._degrade(exc)
            return False
        self._persist_proven = True
        return True

    def _repair(self, exc: Exception) -> bool:
        """Reset an unreadable store to empty. True when the reset landed."""
        self._credits_lost = True
        self._warn_unreadable(exc)
        try:
            self._write_json(self.session_path, {"version": 2, "session_attempts": {}})
        except Exception as write_exc:
            self._degrade(write_exc)
            return False
        return True

    def _validate(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
            raise ValueError("unsupported or missing store version")
        attempts = raw.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError("attempts must be a list")
        validated: list[dict[str, Any]] = []
        for item in attempts:
            if not isinstance(item, dict):
                raise ValueError("attempt entry must be an object")
            session_key = item.get("session_key")
            rowid = item.get("assistant_rowid")
            attempted_at = item.get("attempted_at")
            if not isinstance(session_key, str) or not session_key:
                raise ValueError("attempt session_key must be a non-empty string")
            if isinstance(rowid, bool) or not isinstance(rowid, int) or rowid <= 0:
                raise ValueError("attempt assistant_rowid must be a positive integer")
            if isinstance(attempted_at, bool) or not isinstance(attempted_at, (int, float)):
                raise ValueError("attempt attempted_at must be numeric")
            validated.append(
                {
                    "session_key": session_key,
                    "assistant_rowid": rowid,
                    "attempted_at": float(attempted_at),
                }
            )
        return validated

    def _validate_session_attempts(self, raw: Any) -> dict[str, dict[str, Any]]:
        """Validate the per-session boot-resume counters.

        Absent in legacy v1 is valid and means zero for one-time migration.
        Once a v2 ledger exists, legacy counters are never imported again.
        """
        counters = raw.get("session_attempts") if isinstance(raw, dict) else None
        if counters is None:
            return {}
        if not isinstance(counters, dict):
            raise ValueError("session_attempts must be an object")
        validated: dict[str, dict[str, Any]] = {}
        for session_key, item in counters.items():
            if not isinstance(session_key, str) or not session_key:
                raise ValueError("session_attempts key must be a non-empty string")
            if not isinstance(item, dict):
                raise ValueError("session_attempts entry must be an object")
            count = item.get("count")
            attempted_at = item.get("attempted_at")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("session_attempts count must be a non-negative integer")
            if isinstance(attempted_at, bool) or not isinstance(attempted_at, (int, float)):
                raise ValueError("session_attempts attempted_at must be numeric")
            validated[session_key] = {
                "count": count,
                "attempted_at": float(attempted_at),
            }
        return validated

    def _load_state(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]] | None:
        """Return ``(credits, counters)``, repairing an unreadable file.

        ``None`` means only one thing now: the store could not be made usable
        because the *disk* is the problem (the repair write itself failed).
        Unreadable-but-writable resolves to an empty store, so a bad file can
        never leave the cap permanently blind.
        """
        if self._degraded:
            return None
        raw = {"version": _STORE_VERSION, "attempts": []}
        attempts = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                attempts = self._validate(raw)
            except Exception as exc:
                self._credits_lost = True
                self._warn_unreadable(exc)
                raw = {"version": _STORE_VERSION, "attempts": []}
                if not self._persist_legacy([]):
                    return None
        # Preserve the legacy shape (including frozen v1 counters, if present).
        self._legacy_extra = {k: v for k, v in raw.items() if k not in {"version", "attempts"}}
        try:
            if self.session_path.exists():
                ledger = json.loads(self.session_path.read_text(encoding="utf-8"))
                if not isinstance(ledger, dict) or ledger.get("version") != 2:
                    raise ValueError("unsupported session ledger version")
                if "session_attempts" not in ledger:
                    raise ValueError("missing session_attempts")
                counters = self._validate_session_attempts(ledger)
                if counters and not raw.get("session_attempts") and not self._warned_legacy_reset:
                    self._warned_legacy_reset = True
                    logger.warning(
                        "%s has no legacy counters alongside %s; legacy may have reset "
                        "them; trusting the v2 ledger, not reimporting v1.",
                        self.path.name, self.session_path.name,
                    )
            else:
                counters = self._validate_session_attempts(raw)
                # Establish ownership even for an empty migration, so a later
                # rollback cannot reintroduce obsolete v1 counters.
                if not self._persist(attempts, counters):
                    return None
        except Exception as exc:
            return (attempts, {}) if self._repair(exc) else None
        cutoff = self._now() - AUTO_RESUME_ATTEMPT_TTL_SECONDS
        current = [item for item in attempts if item["attempted_at"] >= cutoff]
        fresh = {
            key: value
            for key, value in counters.items()
            if value["attempted_at"] >= cutoff
        }
        if len(current) != len(attempts) or len(fresh) != len(counters):
            # A TTL prune failing to persist is a real persistence fault; the
            # in-memory view is still accurate, so return it and let the
            # degraded latch govern the next call.
            self._persist(current, fresh)
        return current, fresh

    def _load(self) -> list[dict[str, Any]] | None:
        state = self._load_state()
        return None if state is None else state[0]

    def _write(
        self,
        attempts: list[dict[str, Any]],
        session_attempts: dict[str, dict[str, Any]],
    ) -> None:
        # Write v2 first: a crash or legacy writer can never erase its counters.
        self._write_json(self.session_path, {"version": 2, "session_attempts": session_attempts})
        self._write_json(self.path, {**self._legacy_extra, "version": _STORE_VERSION, "attempts": attempts})

    def _persist_legacy(self, attempts: list[dict[str, Any]]) -> bool:
        try:
            self._write_json(self.path, {"version": _STORE_VERSION, "attempts": attempts})
        except Exception as exc:
            self._degrade(exc)
            return False
        return True

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            if os.name == "posix":
                # The file fsync above protects contents; the directory fsync
                # makes the renamed entry itself survive a kernel/power crash.
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    try:
                        os.fsync(directory_fd)
                    except OSError as exc:
                        unsupported = {
                            errno.EINVAL,
                            errno.ENOTSUP,
                            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                        }
                        if exc.errno not in unsupported:
                            raise
                        logger.warning(
                            "Directory fsync is unsupported for %s; atomic rename "
                            "completed without a directory durability barrier: %s",
                            path.parent,
                            exc,
                        )
                finally:
                    os.close(directory_fd)
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @_serialized_store_call
    def has_attempt(self, session_key: str, assistant_rowid: int) -> bool:
        """True when this interrupted turn already spent its once-ever credit.

        Fails CLOSED, and is the ONE consumer that still does. Two faults reach
        here and both mean the same thing for this question — the credit ledger
        cannot be trusted — so both deny: a degraded store (``None``), and a
        repaired one (``_credits_lost``), where the file now parses but is
        empty precisely because the prior credits were discarded. Reporting
        "no attempt recorded" off an emptied ledger would mint a fresh
        unattended replay for every interrupted turn on the host.

        The denial is small and reversible: the resume drops from ``auto`` to
        ``prompt``, the transcript is untouched, the user gets a banner, and
        the next message continues the conversation. That is why this may fail
        closed while the cap must not — see :meth:`session_resume_verdict`.
        """
        attempts = self._load()
        if attempts is None or self._credits_lost:
            return True
        return any(
            item["session_key"] == session_key
            and item["assistant_rowid"] == assistant_rowid
            for item in attempts
        )

    @_serialized_store_call
    def consume(self, session_key: str, assistant_rowid: int) -> bool:
        """Record a scheduled auto continuation; false means fail closed.

        Fails CLOSED alongside ``has_attempt``, and for the same reason: an
        unrecordable credit would be re-granted on the next boot. False demotes
        the resume to ``prompt`` mode, which is the bounded degradation.
        """

        state = self._load_state()
        if state is None:
            return False
        attempts, session_attempts = state
        if any(
            item["session_key"] == session_key
            and item["assistant_rowid"] == assistant_rowid
            for item in attempts
        ):
            return False
        attempts.append(
            {
                "session_key": session_key,
                "assistant_rowid": assistant_rowid,
                "attempted_at": float(self._now()),
            }
        )
        return self._persist(attempts, session_attempts)

    # ---- per-session boot-resume cap -------------------------------------
    #
    # Distinct grain from the (session_key, assistant_rowid) credits above and
    # deliberately NOT folded into them: the rowid credit answers "was THIS
    # interrupted turn already continued once", which resets whenever a restart
    # amputates a new turn. These counters answer "how many boots in a row have
    # we resumed this session without it getting anywhere", which is the
    # question the 2026-09-20 replay storm needed answered.

    @_serialized_store_call
    def session_attempt_count(self, session_key: str) -> int | None:
        """Attempts recorded for ``session_key``; ``None`` when unknowable.

        ``None`` now means only "persistence is broken" — an unreadable file
        repairs to an empty store and legitimately reports ``0``. Reporting a
        store fault as a large number (the ``1_000_000`` sentinel this
        replaced) capped every session on the host at once.
        """
        state = self._load_state()
        if state is None:
            return None
        return int(state[1].get(session_key, {}).get("count", 0))

    def _persistable(
        self,
        attempts: list[dict[str, Any]],
        session_attempts: dict[str, dict[str, Any]],
    ) -> bool:
        """True when this store can durably record what it is told.

        Probed by rewriting the state already on disk — atomic, idempotent, and
        done at most once per instance. It has to happen at CHECK time rather
        than at record time: ``record_session_attempt`` runs after the resume is
        already scheduled, so discovering there that the counter cannot be
        persisted is one full transcript replay too late, every boot, forever.
        """
        if self._degraded:
            return False
        if self._persist_proven:
            return True
        return self._persist(attempts, session_attempts)

    @_serialized_store_call
    def session_resume_verdict(
        self, session_key: str, max_attempts: int
    ) -> tuple[bool, int | None]:
        """Decide whether ``session_key`` may be boot-resumed again.

        Returns ``(allowed, attempts_or_None)``. ``attempts is None`` on a
        denial means "this store cannot bound anything" rather than "budget
        spent", and the caller MUST leave ``resume_pending`` set for it — the
        session is owed a resume it simply cannot account for, so the denial has
        to stay reversible.

        Three outcomes, each learned from a measured failure on PR #761:

        * under budget, or the cap disabled → allowed.
        * provably at/over budget → denied with a count. Retiring the marker is
          correct here: the evidence is about the SESSION.
        * persistence broken → denied with ``None``. Answering "allowed" here
          is what reproduced the incident — 10 boots, 10 full-transcript
          replays, zero cap lines, because nothing could ever be counted.
        """
        if max_attempts <= 0:
            return True, None
        state = self._load_state()
        if state is None:
            return False, None
        attempts, session_attempts = state
        if not self._persistable(attempts, session_attempts):
            return False, None
        count = int(session_attempts.get(session_key, {}).get("count", 0))
        return count < max_attempts, count

    @_serialized_store_call
    def session_cap_reached(self, session_key: str, max_attempts: int) -> bool:
        """True when ``session_key`` may not be boot-resumed again."""
        allowed, _count = self.session_resume_verdict(session_key, max_attempts)
        return not allowed

    @_serialized_store_call
    def record_session_attempt(self, session_key: str) -> int | None:
        """Increment and persist ``session_key``'s counter; return the new count.

        ``None`` means the attempt could NOT be recorded. The bound does not
        depend on this call succeeding: ``session_resume_verdict`` proves the
        store is writable BEFORE allowing the resume, so a failure here means
        the disk broke between the two and the next boot's check will deny.
        """
        state = self._load_state()
        if state is None:
            return None
        attempts, session_attempts = state
        count = int(session_attempts.get(session_key, {}).get("count", 0)) + 1
        session_attempts[session_key] = {
            "count": count,
            "attempted_at": float(self._now()),
        }
        if not self._persist(attempts, session_attempts):
            return None
        return count

    @_serialized_store_call
    def clear_session_attempts(self, session_key: str) -> None:
        """Forget ``session_key``'s counter after real forward progress.

        Fails OPEN by omission: if the counter cannot be cleared the session
        keeps a budget it has earned back, which costs at most a skipped resume
        that a user message undoes. The opposite — pretending it cleared — is
        what would license an unbounded replay.
        """
        state = self._load_state()
        if state is None:
            return
        attempts, session_attempts = state
        if session_attempts.pop(session_key, None) is None:
            return
        self._persist(attempts, session_attempts)
