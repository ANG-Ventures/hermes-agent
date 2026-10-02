"""Durable transcript persistence for ``AIAgent`` (mixin; MRO-resolved from ``run_agent``): SQLite flush
with intrinsic ``_DB_PERSISTED_MARKER`` dedup, ephemeral-scaffolding filtering, explicit
trajectory export."""
import hashlib

import logging
import re
from contextlib import nullcontext

from typing import Any, Dict, List, Optional, Tuple

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    _DB_PERSISTED_MARKER,
    ContextCompressor,
    _newest_checkpoint_carrier,
    _strip_persistence_markers,
    drop_shadowed_checkpoints,
    user_originated_turn_view,
)
from agent.lazy_forward import forward as _forward, forward_static as _forward_static
from agent.memory_manager import sanitize_context
from agent.message_sanitization import _INTERRUPT_CLOSE_FINISH_REASON

from agent.tool_dispatch_helpers import _is_multimodal_tool_result, _multimodal_text_summary
from agent.trajectory import save_trajectory as _save_trajectory_to_file
from agent.message_metadata import (
    DB_ROW_SNAPSHOT, MERGED_TURN_PREFIX, REPAIR_BOOKKEEPING_FIELDS, TOOL_CALL_UID, copy_identity_fields,
    tool_call_uid_from_history)
from agent.transcript_repair import sync_flushed_message_markers


logger = logging.getLogger("run_agent")  # origin module's name: log records / caplog filters unchanged

# Flags marking ephemeral recovery scaffolding the loop pops before appending the real response.
# Persistence must skip them or a resumed session replays synthetic turns / breaks prefix-cache reuse.
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",  # verify-on-stop nudge; the assistant candidate itself is NOT synthetic
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",  # kanban worker stop-guard
    "_dropped_toolcall_nudge",  # internal retry instruction; must not replay as user context
)

_IMAGE_PART_TYPES = {"image", "image_url", "input_image"}
# Reasoning/codex fields are role-gated (assistant-only) inside _insert_message_rows.
_ROW_REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items", "codex_message_items")
_PERSIST_AFTER_ADMISSION_INTERRUPT = "_persist_after_admission_interrupt"


def _is_ephemeral_scaffolding(msg: Any) -> bool:
    """True when ``msg`` is internal recovery scaffolding that must never reach the durable transcript."""
    return isinstance(msg, dict) and any(msg.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS)


# `_DB_PERSISTED_MARKER` (agent.context_compressor) is the intrinsic "already written to SQLite" marker: an
# id(msg) set can alias a freed dict's address onto a new message, a key on the dict cannot. The `_` prefix is
# mandatory (wire sanitizers strip `_` keys). CONTRACT: the marker asserts the dict's CONTENT is durable as
# written — any in-place mutation that must persist MUST pop it (turn_finalizer, context_compressor).


def _safe_session_filename_component(session_id: str) -> str:
    """Path-safe component for a (possibly untrusted ``X-Hermes-Session-Id``) ID: non ``[A-Za-z0-9_-]`` → ``_``,
    capped, plus a content hash when changed so distinct IDs cannot collide."""
    raw = str(session_id or "").strip()
    sanitized = re.sub(r"[^\w-]", "_", raw).strip("._")[:96] or "session"
    if raw and sanitized == raw:
        return sanitized
    return f"{sanitized}_{hashlib.sha256(raw.encode('utf-8', errors='surrogatepass')).hexdigest()[:12]}"


def _override_replaces_content(msg: Dict, content: Any, override: Any) -> bool:
    """May the persist override replace ``content``? A plain-text override must not replace native image/audio
    blocks (a list override is the clean multimodal payload and does), nor a message MERGED with a compaction
    summary (overwriting would drop the summary)."""
    return (
        override is not None
        and not msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
        and (not isinstance(content, list) or isinstance(override, list))
    )


def _content_with_turn_override(msg: Dict, content: Any, override: Any) -> Any:
    """Replace only the absorbed current turn, leaving unanswered history intact. The join matches
    ``_merge_consecutive_users``; a row that no longer holds the prefix takes the plain override."""
    prefix = msg.get(MERGED_TURN_PREFIX)
    if isinstance(content, str) and isinstance(override, str) and isinstance(prefix, str):
        if content == prefix or content.startswith(prefix + "\n\n"):
            return prefix + "\n\n" + override if override else prefix
    return override


def durable_user_row_content(agent, msg: Dict, content: Any, api_content: Any) -> Tuple[Any, Any]:
    """``(content, api_content)`` as the current turn's user row is written: the persist override is the
    clean transcript, the live content is what the wire sent — so when they differ and nothing else was
    injected, the live bytes ARE the sidecar. Shared by the flush and the turn-start stamp so the stamp
    matches the row the flush wrote."""
    override = getattr(agent, "_persist_user_message_override", None)
    if _override_replaces_content(msg, content, override):
        if api_content is None and isinstance(content, str) and content != override:
            api_content = content
        content = _content_with_turn_override(msg, content, override)
    return content, api_content


def _summary_display_kind(msg: Dict) -> Any:
    """Standalone handoffs are hidden so they never occupy the active user slot in retry/undo dispatch;
    merge-into-tail carriers keep their prior visibility."""
    if (
        msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
        and user_originated_turn_view(msg) is None
        and (
            ContextCompressor.classify_summary_content(msg.get("content")) == "standalone"
            or not msg.get("_compressed_summary_has_user_turn")
        )
    ):
        return "hidden"
    return msg.get("display_kind")


def _durable_content(content: Any) -> Any:
    """Text-only DB projection: multimodal envelopes → summary; part lists keep text, images → ``[screenshot]``."""
    if _is_multimodal_tool_result(content):
        return _multimodal_text_summary(content)
    if not isinstance(content, list):
        return content
    txt = [
        str(p.get("text", "")) if p.get("type") == "text" else "[screenshot]"
        for p in content
        if isinstance(p, dict) and (p.get("type") == "text" or p.get("type") in _IMAGE_PART_TYPES)
    ]
    return "\n".join(txt) if txt else None


def _persist_lock(agent):
    """Close and turn-start persistence can run on separate CLI threads: one critical section.

    ``__init__`` always creates ``_session_persist_lock``; only ``object.__new__``-built test stubs lack it
    (they run unlocked, matching the historical ``if persist_lock is None`` branch).
    """
    lock = getattr(agent, "_session_persist_lock", None)
    return nullcontext() if lock is None else lock


def adopt_unanswered_turn(history: List[Dict[str, Any]], query: Any, agent: Any) -> bool:
    """Re-stage the transcript's unanswered tail row as THIS turn's user message; True when adopted.

    A dispatcher's re-run of a failed delivery turn resumes the DM its first attempt already persisted
    instead of appending it again. Rows loaded from the store are born durable (``_rows_to_conversation``),
    so handing the tail row back as ``agent._pending_cli_user_message`` makes ``_stage_turn_user_message``
    reuse it as this turn's user dict and the flush writes no second row. What differs per lane is only HOW
    the dispatcher knows the DM is unanswered:

    * ``hermes_cli.quiet_single_query.adopt_unanswered_turn`` — the delivery lanes' re-run is a fresh CLI
      process, told so through ``tools.bot_relay.RESUME_UNANSWERED_TURN_ENV``.
    * ``gateway.platforms.api_server`` — the peer-DM lane re-runs the turn in-process and calls this
      directly on the agent it just built for the re-run (#115325).

    The DM is not always the literal tail: a turn that died mid-way persisted its tool scaffolding — assistant
    ``tool_calls`` rows and their ``tool`` results — behind the DM before the failure text was built, and the
    dispatcher retries that too. The DM is still unanswered while nothing after it is a plain assistant reply,
    so it is adopted and the failed attempt's scaffolding leaves the in-memory transcript: the re-run starts
    the turn over from the DM (the rows stay in the DB as the record of the failed attempt; the re-run's
    answer lands after them as a valid continuation). Anything else declines — no user row at the tail, or a
    different text there — so a person's deliberate re-send of the same text is never swallowed.
    """
    idx = next((i for i in range(len(history) - 1, -1, -1)
                if isinstance(history[i], dict) and history[i].get("role") == "user"), None)
    if idx is None or history[idx].get("content") != query:
        return False
    if not all(isinstance(row, dict) and (row.get("role") == "tool" or (row.get("role") == "assistant" and row.get("tool_calls")))
               for row in history[idx + 1:]):
        return False
    tail = history[idx]
    del history[idx:]
    tail[_DB_PERSISTED_MARKER] = True
    agent._pending_cli_user_message = tail
    return True


# --- flush phases (module-level so the flush also works bound onto duck-typed agents) ---

def _db_flush_seed_ids(agent) -> set:
    """One-shot ``_flushed_db_message_ids`` seed (same session, after a non-empty flush); the scan translates
    it to markers and the flush clears it."""
    current_session_id = getattr(agent, "session_id", None)
    same_session = getattr(agent, "_flushed_db_message_session_id", None) == current_session_id
    seed_ids = getattr(agent, "_flushed_db_message_ids", None) if same_session and agent._last_flushed_db_idx != 0 else None
    if not same_session or agent._last_flushed_db_idx == 0:
        # Fork row bookkeeping is per session: a new session id must not inherit row ids / content refs.
        agent._flushed_db_row_ids = {}
        agent._flushed_db_content_refs = {}
        agent._interrupt_close_repersisted_ids = set()
    agent._flushed_db_message_session_id = current_session_id
    return seed_ids if isinstance(seed_ids, set) else set()


def _db_flush_fork_state(agent) -> Tuple[Dict[int, int], Dict[int, Any], set]:
    """``(flushed_row_ids, content_refs, repersisted_ids)`` — the fork's in-place re-persist bookkeeping.

    ``flushed_row_ids`` maps ``id(msg)`` → durable row id; ``content_refs`` maps a flushed ``tool`` row id →
    the exact content object written (content is replaced, never edited, so identity detects a /steer or
    run-budget append after the per-result flush); ``repersisted_ids`` holds ``id(msg)`` of rows whose
    ``interrupt_close`` flag is already durable. Created lazily: ``object.__new__`` test agents lack them.
    """
    flushed_row_ids = getattr(agent, "_flushed_db_row_ids", None)
    if not isinstance(flushed_row_ids, dict):
        flushed_row_ids = {}
        agent._flushed_db_row_ids = flushed_row_ids
    content_refs = getattr(agent, "_flushed_db_content_refs", None)
    if not isinstance(content_refs, dict):
        content_refs = {}
        agent._flushed_db_content_refs = content_refs
    repersisted_ids = getattr(agent, "_interrupt_close_repersisted_ids", None)
    if not isinstance(repersisted_ids, set):
        repersisted_ids = set()
        agent._interrupt_close_repersisted_ids = repersisted_ids
    return flushed_row_ids, content_refs, repersisted_ids


# Fork flush helpers live beside the flush (upstream moved it here); a lazy `from run_agent import`
# fails closed whenever run_agent is swapped in sys.modules, and the row is silently never written.
# Fields the fork stamps onto an ALREADY-FLUSHED message dict in place. The
# incremental flush's bounded prefix scan matches on object identity, which
# proves "same object" but NOT "same content" — so a message carrying one of
# these must never be skipped as part of the matched prefix, or the in-place
# stamp is silently lost on re-flush (an interrupted turn then reads back as a
# clean one). Keep this list in sync with any new in-place mutation site.
_MUTABLE_FLUSH_STATE_FIELDS = ("finish_reason",)


def _has_mutable_flush_state(msg: Any) -> bool:
    """True when *msg* carries a field the fork may have stamped in place."""
    return isinstance(msg, dict) and any(
        msg.get(field) is not None for field in _MUTABLE_FLUSH_STATE_FIELDS
    )


def _persisted_content_projection(msg: Dict[str, Any], content: Any) -> Any:
    """The ``content`` value the flush writes for *msg* (image-free text).

    Shared by the first write and the in-place content re-persist so both
    store the same projection of a multimodal list.
    """
    from agent.message_sanitization import _multimodal_message_text_projection

    _multimodal_projection = _multimodal_message_text_projection(
        {**msg, "content": content}
    )
    if _multimodal_projection is not None:
        return _multimodal_projection
    if isinstance(content, list):
        # List of OpenAI-style content parts: strip images, keep text.
        _txt = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                _txt.append(str(p.get("text", "")))
            elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
                _txt.append("[screenshot]")
        return "\n".join(_txt) if _txt else None
    return content


def _tool_content_mutated_since_flush(msg: Any, content_refs: Dict[int, Any]) -> bool:
    """True when a flushed ``role:"tool"`` row's live content was replaced.

    The flush is append-only, but a mid-turn /steer and the run-budget notice
    append text to the current turn's newest tool result AFTER the sequential
    executor already flushed it. ``content_refs`` maps the row id to the exact
    content object that was written; content is replaced (never edited in
    place), so an identity check is O(1) and catches every such writer.
    """
    if not isinstance(msg, dict) or msg.get("role") != "tool":
        return False
    row_id = msg.get("_db_persisted_row_id")
    if not isinstance(row_id, int) or row_id not in content_refs:
        return False
    return content_refs[row_id] is not msg.get("content")


def _db_flush_scan_start(agent, messages: List[Dict]) -> int:
    """Skip the identity-matched, still-marked prefix of the previous flush's snapshot.

    Identity proves the same dict OBJECT, not the same CONTENT: the fork mutates an already-flushed
    tail in place (``close_interrupted_tool_sequence`` stamps ``finish_reason="interrupt_close"``; a
    /steer appends to the newest tool result). Stop the prefix skip at the first message carrying a
    mutation-sensitive field so those rows are always re-examined (parity merge 2026-08-08).
    """
    _, content_refs, _ = _db_flush_fork_state(agent)
    scan_start = 0
    for prev, cur in zip(getattr(agent, "_db_flush_scan_prefix", None) or (), messages):
        if (
            cur is not prev or not cur.get(_DB_PERSISTED_MARKER)
            or _has_mutable_flush_state(cur) or _tool_content_mutated_since_flush(cur, content_refs)
        ):
            break
        scan_start += 1
    return scan_start


def _db_flush_repersist_in_place(agent, msg: Dict, flushed_row_ids: Dict[int, int], content_refs: Dict[int, Any],
                                 repersisted_ids: set) -> None:
    """Re-persist the two fields the fork stamps onto an ALREADY-FLUSHED dict in place.

    * ``finish_reason="interrupt_close"`` (``close_interrupted_tool_sequence`` on a plain-text assistant
      tail): one targeted ``update_message_finish_reason`` so the resume discriminator survives reload.
    * A flushed ``tool`` row whose content was replaced (the /steer marker or run-budget notice appended
      after the per-result flush): stamp the sent bytes as the row's ``api_content`` sidecar so replay
      sends what was sent and keeps the steer (t_a17e2305).
    """
    msg_id = id(msg)
    if _tool_content_mutated_since_flush(msg, content_refs):
        row_id = msg["_db_persisted_row_id"]
        sent = _persisted_content_projection(msg, msg.get("content"))
        try:
            if isinstance(sent, str) and sent:
                agent._session_db.set_message_api_content(agent.session_id, row_id, sent)
            content_refs[row_id] = msg.get("content")
        except Exception as e:
            logger.warning("tool api_content re-persist failed (row=%s): %s", row_id, e)
    if msg.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON and msg_id not in repersisted_ids:
        row_id = msg.get("_row_id") or msg.get("_db_persisted_row_id") or flushed_row_ids.get(msg_id)
        if isinstance(row_id, int):
            try:
                agent._session_db.update_message_finish_reason(agent.session_id, row_id, _INTERRUPT_CLOSE_FINISH_REASON)
                repersisted_ids.add(msg_id)
            except Exception as e:
                logger.warning("interrupt_close re-persist failed (row=%s): %s", row_id, e)


def _db_flush_row(agent, msg: Dict, is_current_turn_user: bool) -> Dict[str, Any]:
    """Build the session-db row for ``msg``, applying the persist override to THIS row only."""
    role = msg.get("role", "unknown")
    content = msg.get("content")
    # api_content sidecar: exact bytes sent to the API when they differ from clean content (replay parity).
    api_content = msg.get("api_content") if isinstance(msg.get("api_content"), str) else None
    timestamp = msg.get("timestamp")
    # Load-bearing for restart drain-window recovery dedup (has_platform_message_id).
    platform_id = msg.get("platform_message_id") or msg.get("message_id")
    if is_current_turn_user and role == "user":
        content, api_content = durable_user_row_content(agent, msg, content, api_content)
        ov_timestamp = getattr(agent, "_persist_user_message_timestamp", None)
        timestamp = timestamp if ov_timestamp is None else ov_timestamp
        # The interrupted-turn platform id (#48677 backfill dedupe) is stamped on the WRITTEN row so a
        # later backfill-on-reconnect sees the turn is already persisted; the live dict is never mutated
        # (parity with content/timestamp). Drain-window message-loss SPEC D-10.
        ov_platform_id = getattr(agent, "_persist_user_message_platform_id", None)
        if ov_platform_id is not None and not platform_id:
            platform_id = ov_platform_id
    if api_content == content:
        api_content = None
    # get_messages_as_conversation replays rows through sanitize_context().strip(); capture the sent bytes
    # when they would differ (compared in wire form).
    if (
        api_content is None and role in ("user", "assistant") and isinstance(content, str) and content
        and sanitize_context(content).strip() != content.strip()
    ):
        api_content = content
    # Key order is the divert-JSONL wire order (divert_session_transcript_jsonl).
    row = {
        "role": role, "content": _durable_content(content),
        "tool_name": msg.get("tool_name") or (msg.get("name") if role == "tool" else None),
        "tool_calls": msg["tool_calls"] if isinstance(msg.get("tool_calls"), list) else None,
        "tool_call_id": msg.get("tool_call_id"), "effect_disposition": msg.get("effect_disposition"),
        "token_count": msg.get("token_count"), "finish_reason": msg.get("finish_reason"),
        **{k: msg.get(k) for k in _ROW_REASONING_KEYS},
        "_compressed_summary": bool(msg.get(COMPRESSED_SUMMARY_METADATA_KEY)),
        "timestamp": timestamp, "api_content": api_content,
        "display_kind": _summary_display_kind(msg), "display_metadata": msg.get("display_metadata"),
        "platform_message_id": platform_id,
        "observed": bool(msg.get("observed")),
    }
    if isinstance(msg.get("_row_id"), int):
        row["_row_id"] = msg["_row_id"]
    # The merge witness rides on the survivor's row (an owned column: a row-addressed rewrite of the
    # survivor writes it too), so a restart sees which rows the composite folded.
    copy_identity_fields(msg, row)
    if isinstance(msg.get(DB_ROW_SNAPSHOT), str):
        row[DB_ROW_SNAPSHOT] = msg[DB_ROW_SNAPSHOT]
    return row


def _db_flush_collect(agent, messages: List[Dict], conversation_history: Optional[List[Dict]],
                      replay_history: bool = False):
    """Scan for un-flushed messages; returns ``(rows, msgs)`` to write in one transaction. ``replay_history``
    (session-row heal) writes the history prefix again instead of stamping it as already durable."""
    seed_ids = _db_flush_seed_ids(agent)
    history_ids = {id(item) for item in (conversation_history or []) if isinstance(item, dict)}
    ov_idx = getattr(agent, "_persist_user_message_idx", None)
    # Also match the staged CLI dict by identity — the close safety-net may flush a shortened snapshot whose
    # turn index refers to the full history.
    pending_cli_message = getattr(agent, "_pending_cli_user_message", None)
    batch_rows: List[Dict[str, Any]] = []
    batch_msgs: List[Dict] = []
    tool_uid_owners: dict = {}  # tool_call_uid_from_history memo; the scanned dicts outlive this loop
    flushed_row_ids, content_refs, repersisted_ids = _db_flush_fork_state(agent)
    # Superseded-turn write gate (/stop, /new, stale-agent eviction — see ``_persist_superseded``): resolved
    # ONCE, fail-open (any error reading it leaves suppression OFF — a dropped real row is data loss, I5).
    try:
        persist_superseded = bool(getattr(agent, "_persist_superseded", False))
    except Exception:
        persist_superseded = False
    # Pairing safety: the assistant(tool_calls) row flushes in a DIFFERENT flush from its tool result within
    # one iteration, so the suppressed-id set lives on the AGENT (lazily, only when superseded) and survives
    # the whole drain (Greptile-B1′).
    suppressed_tool_call_ids: Optional[set] = None
    if persist_superseded:
        suppressed_tool_call_ids = getattr(agent, "_superseded_suppressed_tool_call_ids", None)
        if not isinstance(suppressed_tool_call_ids, set):
            suppressed_tool_call_ids = set()
            agent._superseded_suppressed_tool_call_ids = suppressed_tool_call_ids
    suppressed_superseded_rows = 0
    for msg_idx in range(_db_flush_scan_start(agent, messages), len(messages)):
        msg = messages[msg_idx]
        # Append-only flush: a mid-turn persist of scaffolding would commit a synthetic turn the end-of-turn
        # drop cannot un-write. Skip regardless of position.
        if not isinstance(msg, dict) or _is_ephemeral_scaffolding(msg):
            continue
        if msg.get(_DB_PERSISTED_MARKER):
            # Already durable by marker; the fork may still have stamped a field in place.
            _db_flush_repersist_in_place(agent, msg, flushed_row_ids, content_refs, repersisted_ids)
            continue
        # Already durable (history copy or caller-seeded): stamp so future flushes skip it.
        is_history = id(msg) in history_ids
        if (
            (is_history and not replay_history) or id(msg) in seed_ids
        ) and not msg.get(_PERSIST_AFTER_ADMISSION_INTERRUPT):
            msg[_DB_PERSISTED_MARKER] = True
            continue
        # Superseded-turn write gate: runs AFTER every "already durable" skip above, so it only ever sees a
        # genuinely NEW row. 🔴 CARVE-OUT (I1): the interrupt-close tail MUST still persist — it is the
        # role-alternation repair and the restart-loop backstop (#45230/#49201/#49243). 🔴 PAIRING (B1): a
        # ``tool`` result is suppressed ONLY when its owning assistant(tool_calls) was also suppressed; an
        # already-durable owner is skipped above and never recorded, so its result lands (no #48879 orphan).
        if persist_superseded and msg.get("finish_reason") != _INTERRUPT_CLOSE_FINISH_REASON:
            if msg.get("role") == "tool":
                if msg.get("tool_call_id") in suppressed_tool_call_ids:
                    suppressed_superseded_rows += 1
                    continue
            elif msg.get("role") == "assistant":
                tcs = msg.get("tool_calls")
                if isinstance(tcs, list):
                    for tc in tcs:
                        tcid = (tc.get("id") or tc.get("tool_call_id")) if isinstance(tc, dict) else getattr(tc, "id", None)
                        if tcid:
                            suppressed_tool_call_ids.add(tcid)
                suppressed_superseded_rows += 1
                continue
            else:
                # FAIL-OPEN on any other role: a zombie writes only assistant+tool rows, and dropping a real
                # user message would be data loss (I5). Persist normally; log for diagnosability.
                logger.debug(
                    "persist gate: superseded turn produced an unexpected new %r row for session %s — "
                    "persisting (fail-open)", msg.get("role"), getattr(agent, "session_id", "?"),
                )
        if getattr(agent, "_mute_notification_reply", False) and not is_history:
            # Only new rows, never the cached history prefix. Keep evidence/model
            # context intact while transcript pollers omit unsolicited presentation.
            msg["display_kind"] = "hidden"
            msg["display_metadata"] = {**(msg.get("display_metadata") or {}), "notification_category": "diagnostic"}
        if msg.get("role") == "tool" and not msg.get(TOOL_CALL_UID):
            # A result whose call was flushed in an earlier batch: pair it with the uid that assistant row
            # minted (same-batch pairing happens inside the insert).
            if (tool_uid := tool_call_uid_from_history(messages, msg_idx, tool_uid_owners)) is not None:
                msg[TOOL_CALL_UID] = tool_uid
        batch_rows.append(_db_flush_row(agent, msg, ov_idx == msg_idx or msg is pending_cli_message))
        batch_msgs.append(msg)
    if suppressed_superseded_rows:
        logger.info(
            "persist: suppressed %d superseded-turn content row(s) for session %s (turn was /stop'd or "
            "/new'd; interrupt-close tail preserved)", suppressed_superseded_rows, getattr(agent, "session_id", "?"),
        )
    return batch_rows, batch_msgs


def _db_flush_record_row_ids(agent, batch_rows: List[Dict[str, Any]], batch_msgs: List[Dict],
                             batch_row_ids: List[Any]) -> None:
    """Fork bookkeeping after a committed batch: positional row-id capture for the in-place re-persist path
    (``_db_persisted_row_id`` on the live dict, ``_flushed_db_row_ids``, tool ``content_refs``)."""
    flushed_row_ids, content_refs, repersisted_ids = _db_flush_fork_state(agent)
    for idx, written in enumerate(batch_msgs):
        row_id = batch_row_ids[idx] if idx < len(batch_row_ids) else None
        if not isinstance(row_id, int) and isinstance(written.get("_row_id"), int):
            # Repaired-in-place rows get no freshly-inserted id but carry their durable one.
            row_id = written["_row_id"]
        if isinstance(row_id, int):
            flushed_row_ids[id(written)] = row_id
            written["_db_persisted_row_id"] = row_id
            if written.get("role") == "tool":
                content_refs[row_id] = written.get("content")
            # Appended already carrying the flag ⇒ durably persisted; no later re-persist needed.
            if written.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON:
                repersisted_ids.add(id(written))
        elif written.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON:
            # Without a row id a later in-place interrupt_close mutation cannot be re-persisted; a lost flag
            # falls back to the old "skip unfinished work" resume. append_messages_batch fills row_ids_out
            # with ints in prod — a miss means a mock/altered API, so make it loud.
            logger.warning(
                "flush: append_messages_batch returned no row id (%r) for an interrupt_close message; "
                "in-place re-persist will be skipped", row_id,
            )


def _db_flush_write(agent, batch_rows: List[Dict[str, Any]], batch_msgs: List[Dict], messages: List[Dict]) -> None:
    """One transaction for the turn's new rows: on failure nothing lands and no markers are stamped."""
    if not batch_rows:
        return
    batch_row_ids: List[int] = []
    agent._session_db.append_messages_batch(
        session_id=agent.session_id, messages=batch_rows,
        compression_lock_holder=getattr(agent, "_active_compression_lock_holder", None),
        row_ids_out=batch_row_ids,
        turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
        turn_lease_ttl_seconds=getattr(agent, "_active_session_turn_lease_ttl_seconds", 300.0) or 300.0,
    )
    sync_flushed_message_markers(batch_msgs, batch_rows)
    _db_flush_record_row_ids(agent, batch_rows, batch_msgs, batch_row_ids)
    if _newest_checkpoint_carrier(batch_msgs, "codex_reasoning_items") >= 0:
        # The insert already rewrote the older rows (SessionDB._drop_shadowed_checkpoint_rows); mirror it on
        # the live transcript so forks/compaction built from memory carry one checkpoint too. Markers stay:
        # the rows are durable exactly as the dicts now read.
        drop_shadowed_checkpoints(messages)
    session_id = getattr(agent, "session_id", None)
    if session_id:
        try:
            agent._session_db.recompute_effective_last_active(session_id)
        except Exception as e:
            logger.warning("Session DB effective_last_active recompute failed: %s", e)


def _db_flush_adopt_compression_tip(agent) -> bool:
    """Adopt the live continuation of a compression-closed session. Same-id tip = no continuation; a tip
    whose row is missing or already ended is not adopted either."""
    old_id = agent.session_id
    try:
        tip = agent._session_db.get_compression_tip(old_id)
    except Exception as tip_exc:
        logger.warning("compression tip lookup failed for %s: %s", old_id, tip_exc)
        return False
    if not tip or tip == old_id:
        return False
    try:
        tip_row = agent._session_db.get_session(tip)
    except Exception:
        tip_row = None
    if tip_row is None or tip_row.get("ended_at") is not None:
        return False
    logger.warning("Adopted live compression tip %s for closed session %s; retrying flush once", tip, old_id)
    agent.session_id, agent._flushed_db_message_ids, agent._last_flushed_db_idx = tip, set(), 0
    agent._compression_adoption_failed = False
    return True


def _db_flush_session_row_gone(agent, session_id: Optional[str]) -> bool:
    """True only when ``session_id``'s row is confirmed absent; a failed lookup is not proof."""
    try:
        return agent._session_db.get_session(session_id) is None
    except Exception as lookup_exc:
        logger.warning("session row lookup failed for %s: %s", session_id, lookup_exc)
        return False


def _db_flush_failed(agent, e: Exception, batch_rows: List[Dict[str, Any]], adoption_budget: int,
                     messages: List[Dict]) -> Optional[str]:
    """Classify a failed flush and name the one retry the caller should take, or None to fail closed.

    ``"adopted"``: a compression-closed session moved onto its live tip. ``"healed"``: the session row was
    deleted under the live agent and has been recreated (unparented for a delegate child whose parent went
    with it); flush markers are reset so the caller replays the full in-memory transcript. Either retry is
    taken at most once (``adoption_budget``)."""
    agent._db_flush_scan_prefix = None  # full re-scan next flush: an exception mid-loop leaves mixed dispositions
    # The only place the SQLite error is visible before it becomes a bare False — classify it so the turn-end
    # explanation names the real cause.
    from hermes_state import StateDbCorruptError, StateDbReplacedError, classify_persistence_error, divert_session_transcript_jsonl
    from hermes_state_errors import CompressionSessionClosedError
    agent._last_persistence_error_cause = classify_persistence_error(e)
    if agent._last_persistence_error_cause == "session_row_missing":
        # The session row was removed under this live agent (`hermes sessions delete`, the Desktop/web
        # delete, bulk prune, a profile-repair move, an in-place store rebuild — none visible to the
        # cached agent, so the cached `_session_db_created` flag is stale and every later append hits
        # the FK). The deletion already erased the session's message rows with it, so the durable
        # transcript is empty: drop the stale flag, reset the flush markers, and replay the FULL
        # in-memory transcript onto the recreated row — not just the current tail (#123583).
        # The FK class also covers the sessions table's own parent/system-prompt FKs, and create is an
        # upsert: only heal when the row is really gone, or the replay would duplicate a live transcript.
        if adoption_budget <= 0 or not _db_flush_session_row_gone(agent, agent.session_id):
            return None
        _strip_persistence_markers(messages)
        # The durable history prefix is gone with the row: keep a replay pending until a write succeeds, so
        # a failed recreate or a failed retry write can't let a later flush stamp that prefix durable.
        agent._session_row_replay_pending = agent.session_id
        agent._flushed_db_message_ids = set()
        agent._last_flushed_db_idx = 0
        agent._session_db_created = False
        agent._ensure_db_session()
        parent_id = agent._parent_session_id
        if not agent._session_db_created and parent_id and _db_flush_session_row_gone(agent, parent_id):
            # Delegate child whose parent was deleted (cascade): the row's own parent FK would reject every
            # recreate. Create it unparented for this call only; the relay and hooks still use the parent id.
            agent._parent_session_id = None
            try:
                agent._ensure_db_session()
            finally:
                agent._parent_session_id = parent_id
        if not agent._session_db_created:
            # Row creation failed too (transient store trouble): don't append into a guaranteed
            # rollback — keep the batch unmarked so the next flush retries the whole thing. That flush
            # recreates the row up front (no FK error, no heal); the pending replay covers the history prefix.
            logger.warning("Session DB row for %s is missing and could not be recreated; will retry next flush",
                           getattr(agent, "session_id", None))
            return None
        logger.warning("Session DB row for %s was removed under the live agent; recreated it and replaying the transcript",
                       getattr(agent, "session_id", None))
        return "healed"
    if isinstance(e, (StateDbReplacedError, StateDbCorruptError)):
        # A replaced/quarantined handle will not take this batch again — keep it on disk.
        try:
            # The CAS digest / adopted row are local repair bookkeeping, not transcript payload.
            divert_session_transcript_jsonl(getattr(agent, "session_id", "") or "",
                                            [{k: v for k, v in r.items() if k not in REPAIR_BOOKKEEPING_FIELDS}
                                             for r in batch_rows])
        except Exception:
            logger.warning("JSONL divert failed after state.db %s for %s",
                           agent._last_persistence_error_cause, getattr(agent, "session_id", None), exc_info=True)
    if isinstance(e, CompressionSessionClosedError):
        # Compression race: another path rotated this session mid-write. Retry exactly once on the live tip; a
        # second closed-parent write fails closed.
        if adoption_budget > 0 and _db_flush_adopt_compression_tip(agent):
            return "adopted"
        agent._compression_adoption_failed = True  # lets the turn explanation name rotation, not full-disk advice
    logger.warning("Session DB append_message failed: %s", e)
    return None



class SessionPersistenceMixin:
    """Session DB flush and trajectory persistence (see module docstring)."""

    def _apply_persist_user_message_override(self, messages: List[Dict]) -> None:
        """Rewrite the current-turn user message in place: some paths send an API-only variant that must not
        leak into transcripts or resumed history."""
        idx = getattr(self, "_persist_user_message_idx", None)
        override = getattr(self, "_persist_user_message_override", None)
        timestamp = getattr(self, "_persist_user_message_timestamp", None)
        platform_id = getattr(self, "_persist_user_message_platform_id", None)
        if idx is None or (override is None and timestamp is None and platform_id is None):
            return
        msg = messages[idx] if 0 <= idx < len(messages) else None
        if not (isinstance(msg, dict) and msg.get("role") == "user"):
            return
        if _override_replaces_content(msg, msg.get("content"), override):
            msg["content"] = _content_with_turn_override(msg, msg.get("content"), override)
        if timestamp is not None:
            msg["timestamp"] = timestamp
        if platform_id is not None:  # load-bearing for restart drain-window recovery dedup (has_platform_message_id)
            msg["platform_message_id"] = platform_id

    def _persist_session(self, messages: List[Dict], conversation_history: List[Dict] = None):
        """Save to SQLite on any exit path. Trailing empty-response scaffolding is dropped from
        the live list; the persist override is applied to the DB row only.

        The persist user-message *override* is NOT applied here — it is resolved inside
        ``_flush_messages_to_session_db`` and written only to the DB row, never mutating the live message
        list used by the API call (#48677 is thus closed for every persist caller, not just this one).
        """
        from agent.agent_runtime_helpers import note_turn_persisted
        with _persist_lock(self):
            # Only the scaffolding goes here. Closing a tool tail this uncovers is the exit
            # owner's job (``_close_transcript_tail``, ``abort_turn_on_interrupt``): only it knows
            # the reason to record.
            self._drop_trailing_empty_response_scaffolding(messages)
            self._session_messages = messages
            self._flush_messages_to_session_db(messages, conversation_history)
            # Drain async token-accounting deltas at every persist point; cheap no-op when nothing queued.
            if self._session_db is not None:
                self._session_db.flush_token_counts()
            note_turn_persisted(self)

    def _drop_trailing_empty_response_scaffolding(self, messages: List[Dict]) -> None:
        """Pop empty-response retry scaffolding from the tail. The
        assistant(tool_calls) / tool rows before it stay: they were saved before the tools ran, so
        dropping them from the live history only makes the model repeat a side effect the durable
        transcript already records."""
        def tail(*keys: str) -> bool:
            return bool(messages) and isinstance(messages[-1], dict) and any(messages[-1].get(k) for k in keys)

        while tail("_empty_recovery_synthetic", "_empty_terminal_sentinel"):
            messages.pop()

    _repair_message_sequence = _forward("agent.agent_runtime_helpers", "repair_message_sequence")

    def _flush_messages_to_session_db(self, messages: List[Dict], conversation_history: Optional[List[Dict]] = None):
        """Serialize direct and turn-boundary session flushes per agent."""
        # Duck-typed stubs bind only this method (tests/agent/test_persist_platform_message_id.py); fall back
        # to the mixin's own unlocked flush so the real path still runs end to end.
        flush_unlocked = getattr(self, "_flush_messages_to_session_db_unlocked", None)
        if flush_unlocked is None:
            flush_unlocked = SessionPersistenceMixin._flush_messages_to_session_db_unlocked.__get__(self, type(self))
        with _persist_lock(self):
            return flush_unlocked(messages, conversation_history)

    def _flush_messages_to_session_db_unlocked(
        self, messages: List[Dict], conversation_history: Optional[List[Dict]] = None, _adoption_budget: int = 1,
    ):
        """Persist un-flushed messages to SQLite. Dedup is the intrinsic ``_DB_PERSISTED_MARKER`` on each written
        dict — not positional slices (drift after sequence repair) nor an ``id(msg)`` set (address reuse). The
        persist override touches the written row only. A compression-closed session adopts its live tip and
        retries exactly once.

        Deduplicates via an intrinsic ``_DB_PERSISTED_MARKER`` stamped on each written message dict, so
        repeated calls (from multiple exit paths) only write truly new messages — preventing the
        duplicate-write bug (#860) without relying on positional slices that can drift after
        message-sequence repair, and without a retained ``id(msg)`` set that CPython could alias onto a
        freed-then-reused address (#50372). The ``_flushed_db_message_ids`` attribute is now only a one-shot
        seed (translated to markers, then cleared each flush), not a persisted set.
        """
        # Persistence-isolated agents (background review fork) share the parent's session_id for cache warmth;
        # a write here would land the curator's turn in the user's real history.
        if getattr(self, "_persist_disabled", False) or not self._session_db:
            return None
        batch_rows: List[Dict[str, Any]] = []
        try:
            if not self._session_db_created:  # retry row creation if the earlier attempt failed transiently
                self._ensure_db_session()
            # getattr: object.__new__ test agents flush without running AIAgent init.
            replay = getattr(self, "_session_row_replay_pending", None) == self.session_id
            batch_rows, batch_msgs = _db_flush_collect(self, messages, conversation_history, replay)
            _db_flush_write(self, batch_rows, batch_msgs, messages)
            self._session_row_replay_pending = None
            # Markers are now the sole truth; reset the one-shot seed so no id() outlives this flush.
            self._flushed_db_message_ids = set()
            self._last_flushed_db_idx = len(messages)
            # Snapshot for the bounded scan — only on full success, so a partial list is never treated as settled.
            self._db_flush_scan_prefix = messages[:]
            return True
        except Exception as e:
            retry = _db_flush_failed(self, e, batch_rows, _adoption_budget, messages)
            if retry is None:
                return False
            # After a heal the pending replay re-sends the history prefix instead of stamping it durable.
            return self._flush_messages_to_session_db_unlocked(messages, conversation_history, _adoption_budget=0)

    def _get_messages_up_to_last_assistant(self, messages: List[Dict]) -> List[Dict]:
        """Messages before the last assistant turn (rollback point for a malformed final answer); all if none."""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                return messages[:i]
        return messages.copy()

    _format_tools_for_system_message = _forward("agent.system_prompt", "format_tools_for_system_message")
    _convert_to_trajectory_format = _forward("agent.agent_runtime_helpers", "convert_to_trajectory_format")

    def _save_trajectory(self, messages: List[Dict[str, Any]], user_query: str, completed: bool):
        """Save conversation trajectory to JSONL file."""
        if not self.save_trajectories:
            return
        trajectory = self._convert_to_trajectory_format(messages, user_query, completed)
        _save_trajectory_to_file(trajectory, self.model, completed)

    _extract_api_error_context = _forward_static("agent.agent_runtime_helpers", "extract_api_error_context")
    _dump_api_request_debug = _forward("agent.agent_runtime_helpers", "dump_api_request_debug")
