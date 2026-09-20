"""Safety helpers for gateway interrupted-turn auto-continuation.

There are two distinct resume kinds:

* SIBLING auto-continue resumes a different session amputated by a gateway
  drain timeout.
* SELF resume-handoff starts a fresh synthesized turn for the session that
  intentionally initiated the restart, preserving its handoff note.

This module supports the existing boot-resume scheduler in ``gateway.run``. It
classifies persisted tool-call tails, names the taxonomy, and stores the
once-ever SIBLING auto-resume credit; it does not schedule turns itself.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
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


class AutoResumeAttemptStore:
    """Seven-day durable once-ever auto-resume credits.

    A malformed file poisons this store instance closed: every lookup reports an
    existing attempt and exactly one warning is emitted.  This prevents a corrupt
    safety backstop from silently granting fresh auto-resume credits.
    """

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._invalid = False
        self._warned = False

    def _warn_invalid(self, exc: Exception | str) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(
            "%s is unparseable; interrupted-turn auto-continuation fails closed "
            "to prompt for all sessions: %s",
            self.path.name,
            exc,
        )

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

        Absent is valid and means zero: the key was added after the rowid
        credits, so a file written by an older gateway has no counters and must
        keep loading (and vice versa — an older gateway ignores this key, which
        is why adding it needs no ``_STORE_VERSION`` bump).
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
        if self._invalid:
            return None
        if not self.path.exists():
            return [], {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            attempts = self._validate(raw)
            counters = self._validate_session_attempts(raw)
            cutoff = self._now() - AUTO_RESUME_ATTEMPT_TTL_SECONDS
            current = [item for item in attempts if item["attempted_at"] >= cutoff]
            fresh = {
                key: value
                for key, value in counters.items()
                if value["attempted_at"] >= cutoff
            }
            if len(current) != len(attempts) or len(fresh) != len(counters):
                self._write(current, fresh)
            return current, fresh
        except Exception as exc:
            self._invalid = True
            self._warn_invalid(exc)
            return None

    def _load(self) -> list[dict[str, Any]] | None:
        state = self._load_state()
        return None if state is None else state[0]

    def _write(
        self,
        attempts: list[dict[str, Any]],
        session_attempts: dict[str, dict[str, Any]],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": _STORE_VERSION,
                "attempts": attempts,
                "session_attempts": session_attempts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            if os.name == "posix":
                # The file fsync above protects contents; the directory fsync
                # makes the renamed entry itself survive a kernel/power crash.
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
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
                            self.path.parent,
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

    def has_attempt(self, session_key: str, assistant_rowid: int) -> bool:
        attempts = self._load()
        if attempts is None:
            return True
        return any(
            item["session_key"] == session_key
            and item["assistant_rowid"] == assistant_rowid
            for item in attempts
        )

    def consume(self, session_key: str, assistant_rowid: int) -> bool:
        """Record a scheduled auto continuation; false means fail closed."""

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
        try:
            self._write(attempts, session_attempts)
        except Exception as exc:
            self._invalid = True
            self._warn_invalid(exc)
            return False
        return True

    # ---- per-session boot-resume cap -------------------------------------
    #
    # Distinct grain from the (session_key, assistant_rowid) credits above and
    # deliberately NOT folded into them: the rowid credit answers "was THIS
    # interrupted turn already continued once", which resets whenever a restart
    # amputates a new turn. These counters answer "how many boots in a row have
    # we resumed this session without it getting anywhere", which is the
    # question the 2026-09-20 replay storm needed answered.

    def session_attempt_count(self, session_key: str) -> int | None:
        """Attempts recorded for ``session_key``; ``None`` when unknowable.

        A poisoned store reports ``None``, NOT a large number. It deliberately
        does not copy ``has_attempt``'s fail-closed posture, because the two
        denials are not the same size: ``has_attempt`` failing closed degrades
        one turn from ``auto`` to ``prompt`` — the transcript survives and the
        next user message continues it — whereas the cap's skip branch retires
        ``resume_pending``, which is irreversible and was measured stripping
        restart continuity from every session on the host at once.

        A store fault is evidence about the STORE. It is not evidence that some
        session spent a budget, so it must not be reported as one.
        """
        state = self._load_state()
        if state is None:
            return None
        return int(state[1].get(session_key, {}).get("count", 0))

    def session_cap_reached(self, session_key: str, max_attempts: int) -> bool:
        """True when ``session_key`` has provably exhausted its budget.

        Unknown is not over budget: an unreadable store answers ``False`` so a
        session with no recorded attempts keeps resuming and keeps its marker.
        """
        if max_attempts <= 0:
            return False
        count = self.session_attempt_count(session_key)
        if count is None:
            return False
        return count >= max_attempts

    def record_session_attempt(self, session_key: str) -> int | None:
        """Increment and persist ``session_key``'s counter; return the new count.

        ``None`` means the attempt could NOT be recorded — an unreadable store,
        or a write that failed. The caller treats that as lost accounting, not
        as a spent budget: an unrecordable attempt leaves the cap reading
        ``unknown``, which keeps resuming rather than capping on a store fault.
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
        try:
            self._write(attempts, session_attempts)
        except Exception as exc:
            self._invalid = True
            self._warn_invalid(exc)
            return None
        return count

    def clear_session_attempts(self, session_key: str) -> None:
        """Forget ``session_key``'s counter after real forward progress."""
        state = self._load_state()
        if state is None:
            return
        attempts, session_attempts = state
        if session_attempts.pop(session_key, None) is None:
            return
        try:
            self._write(attempts, session_attempts)
        except Exception as exc:
            self._invalid = True
            self._warn_invalid(exc)
