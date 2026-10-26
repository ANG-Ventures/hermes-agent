"""Gateway response filtering helpers.

These decide whether a completed agent turn should be delivered to the chat,
not what should be persisted in conversation history.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

# Canonical model-emitted control token for intentional silence (fork).
SILENT_REPLY_TOKEN = "NO_REPLY"

# Exact whole-response markers meaning "the agent intentionally chose not to
# reply". Keep small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence. A lane that does not think in English
# translates the sentinel rather than dropping it, and the whole control token
# then reaches the user as content, so the translated forms are carried here
# too. zh-Hans is the only non-English locale this project ships documentation
# for, which is where the list stops.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]", "SILENT", "NO_REPLY", "NO REPLY",
    "[静默]", "静默", "[沉默]", "沉默",
})

# Bracketed markers drive the autonomous lane's prefix rule ("[SILENT] nothing
# new this tick"). Derived from the set above so a new marker cannot be added to
# one rule and forgotten in the other.
_BRACKETED_SILENCE_MARKERS = tuple(
    sorted(m for m in LIVE_GATEWAY_SILENT_MARKERS if m.startswith("["))
)

# The persisted user-row kind of a self-injected MessageEvent(internal=True) turn — the only
# machinery kind the gateway produces; only these may vanish on a bare silence marker.
INTERNAL_NOTIFICATION_DISPLAY_KIND = "internal_notification"
MACHINERY_DISPLAY_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND})

# Longer than any marker could plausibly be, even with stray punctuation.
_MARKER_LENGTH_CAP = 64


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _is_edge_punctuation(ch: str) -> bool:
    # Square brackets stay structural so malformed ``[SILENT`` cannot become ``SILENT``.
    return ch not in "[]" and unicodedata.category(ch).startswith("P")


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation (``.NO_REPLY``, ``*NO_REPLY*``) without erasing marker structure."""
    start, end = 0, len(text)
    while start < end and _is_edge_punctuation(text[start]):
        start += 1
    while end > start and _is_edge_punctuation(text[end - 1]):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: Any) -> tuple[str, ...]:
    """Canonical forms of a short marker-sized response; ``()`` when not a candidate at all."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not 0 < len(stripped) <= _MARKER_LENGTH_CAP:
        return ()
    depunctuated = _strip_edge_silence_punctuation(stripped)
    forms = (stripped,) if depunctuated == stripped else (stripped, depunctuated)
    return tuple(_canonical_silence_candidate(f) for f in forms)


def is_intentional_silence_response(response: Any) -> bool:
    """True only when ``response`` is exactly a silence marker.

    Prose that merely mentions ``NO_REPLY`` must be delivered normally. A blank
    response is not silence either — that is the empty-response failure path.
    """
    return any(c in LIVE_GATEWAY_SILENT_MARKERS for c in _canonical_silence_candidates(response))


def _strip_code_wrap(s: str) -> str:
    """Peel a whole-value markdown code fence, then any inline backtick span.

    An agent cron (whose reply IS its final message) routinely formats the literal sentinel,
    which would otherwise leak to the channel as noise.  Only *symmetric* wrapping is stripped,
    so real prose is untouched.
    """
    t = s.strip()
    if t.startswith("```") and t.endswith("```") and len(t) >= 6:
        inner = t[3:-3]
        # Drop the whole opening-fence info-string line (```, ```text, ...)
        # — anything up to the first newline is the fence header.
        if "\n" in inner:
            inner = inner.split("\n", 1)[1]
        t = inner.strip()
    while len(t) >= 2 and t[0] == "`" and t[-1] == "`":
        t = t.strip("`").strip()
    return t


def _is_marker_line(line: str) -> bool:
    """A line (or whole value) that is a silence token: bare, code-spanned, or fenced."""
    return is_intentional_silence_response(line) or is_intentional_silence_response(_strip_code_wrap(line))


def _opens_with_bracketed_marker(stripped: str) -> bool:
    # Peel a leading inline code-span (any backtick-run length) first so
    # "`[SILENT]` note" / "``[SILENT]`` note" also count (same reflex).
    head = stripped
    if head.startswith("`"):
        run = len(head) - len(head.lstrip("`"))
        close = head.find("`" * run, run)
        if close != -1:
            head = head[run:close].strip()
    return head.upper().startswith(_BRACKETED_SILENCE_MARKERS)


def is_autonomous_silence_response(response: Any) -> bool:
    """Loose silence matcher for autonomous lanes (cron, webhook).

    Models reliably bracket ``[SILENT]`` with a short note, so unlike the
    interactive EXACT rule this also suppresses when a marker sits on its own
    first/last line or the bracketed sentinel opens the response (``[SILENT] No
    changes detected``).  A token buried mid-sentence is still delivered.
    Shares :data:`LIVE_GATEWAY_SILENT_MARKERS` so the two sets cannot drift.
    """
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False

    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    # Bracketed form only for the prefix rule, so a bare "Silent retry succeeded" is NOT swallowed.
    # Same de-punctuating forms as the interactive rule, so ``【静默】`` / ``静默。`` cannot
    # be suppressed in chat yet delivered by cron.
    if _opens_with_bracketed_marker(stripped):
        return True
    # Whole response, or its own first/last line, is a token (bare, code-spanned, or fenced).
    return any(_is_marker_line(c) for c in (stripped, lines[0], lines[-1]))


# Internal turns: a marker on its own first/last line silences the reply only when what is left
# is a short note ("Already reported.") — at most this many words on one line.  Agents write
# "<summary>\n\nNO_REPLY" meaning "post this, no answer needed"; under the loose rule the summary
# vanished (555 replies / 24k words in 7 days, t_bb4059d2).  0 restores the loose rule.
SHORT_NOTE_WORDS = 12
SHORT_NOTE_WORDS_CONFIG_KEY = "internal_silence_short_note_words"  # under ``display:``


def internal_short_note_words(user_config: Any) -> int:
    """``display.internal_silence_short_note_words`` from a raw user config; default when unset/invalid."""
    display = user_config.get("display") if isinstance(user_config, dict) else None
    raw = display.get(SHORT_NOTE_WORDS_CONFIG_KEY) if isinstance(display, dict) else None
    if raw is None or isinstance(raw, bool):
        return SHORT_NOTE_WORDS
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return SHORT_NOTE_WORDS


def _configured_short_note_words() -> int:
    from gateway.run import _load_gateway_config  # profile-aware gateway home
    return internal_short_note_words(_load_gateway_config())


def _drop_edge_marker_lines(lines: list[str]) -> list[str]:
    """``lines`` without the blank/marker lines at either end."""
    start, end = 0, len(lines)
    while start < end and (not lines[start].strip() or _is_marker_line(lines[start])):
        start += 1
    while end > start and (not lines[end - 1].strip() or _is_marker_line(lines[end - 1])):
        end -= 1
    return lines[start:end]


def classify_internal_silence(
    response: Any, short_note_words: Optional[int] = None,
) -> tuple[bool, Optional[int]]:
    """``(silent, note_words)`` for an internal-turn reply.

    ``note_words`` is the word count of the prose beside an own-line marker, ``None`` when the
    reply is not that case (no marker, a bare marker, or the ``[SILENT] ...`` prefix form, which
    all keep the autonomous rule).  ``short_note_words`` ``None`` reads the gateway config.
    """
    if not is_autonomous_silence_response(response):
        return False, None
    stripped = response.strip()
    if _is_marker_line(stripped):
        return True, None
    lines = stripped.splitlines()
    nonblank = [ln for ln in lines if ln.strip()]
    if _opens_with_bracketed_marker(stripped) and not _is_marker_line(nonblank[0]):
        return True, None
    if short_note_words is None:
        short_note_words = _configured_short_note_words()
    if short_note_words <= 0:
        return True, None
    rest = [ln for ln in _drop_edge_marker_lines(lines) if ln.strip()]
    words = sum(_note_word_count(w) for ln in rest for w in ln.split())
    return words <= short_note_words and len(rest) <= 1, words


def _is_wide_letter(ch: str) -> bool:
    # CJK ideographs / kana / hangul: written without spaces, so each counts as a word.
    return ch.isalnum() and unicodedata.east_asian_width(ch) in ("W", "F")


def _note_word_count(token: str) -> int:
    """Words in one whitespace-split token.  A word carries a letter or digit (a bare "—" or "-"
    bullet is not one); each wide CJK letter is its own word, the rest of the token counts once."""
    wide = sum(1 for ch in token if _is_wide_letter(ch))
    narrow = any(ch.isalnum() and not _is_wide_letter(ch) for ch in token)
    return wide + (1 if narrow else 0)


def is_internal_silence_response(response: Any, short_note_words: Optional[int] = None) -> bool:
    """Silence rule for internal-event turns: :func:`is_autonomous_silence_response`, except that
    an own-line marker beside more than a short one-line note is delivered (marker stripped)."""
    return classify_internal_silence(response, short_note_words)[0]


def strip_internal_silence_marker_lines(response: Any) -> Any:
    """Drop the marker lines at either end of an internal reply that is being DELIVERED.

    On an internal turn those lines were control tokens for the gateway (any form the loose rule
    reads: ``NO_REPLY``, ``no reply``, `` `[SILENT]` ``), never content.  A reply that would be
    empty without them is returned unchanged for the silence rules to judge.
    """
    if not isinstance(response, str):
        return response
    lines = response.strip().split("\n")
    kept = _drop_edge_marker_lines(lines)
    if len(kept) == len(lines) or not kept:
        return response
    return "\n".join(kept).strip()


def is_intentional_silence_agent_result(
    agent_result: dict | None, response: Any, *, internal: bool = False,
    short_note_words: Optional[int] = None,
) -> bool:
    """Silence markers suppress delivery only for successful agent turns.

    ``internal`` marks a turn triggered by a system-generated gateway event
    (background-process completion, delegation batch, restore replay).  No
    human is waiting on those, so they get the autonomous-lane rule
    (:func:`is_autonomous_silence_response`: marker on its own first/last
    line); human turns keep the exact-marker rule.
    """
    if not isinstance(agent_result, dict) or agent_result.get("failed"):
        return False
    if internal:
        return is_internal_silence_response(response, short_note_words)
    return is_intentional_silence_response(response)


def display_kind_for_event(event: Any) -> str | None:
    """The persisted user-row kind for a gateway turn: only self-injected events are machinery.

    A scheduled heartbeat prompt is self-injected too (``_heartbeat_session_id`` is stamped only
    by the gateway poller, never inferred from inbound text), but it deliberately stays
    non-internal so authorization and the emergency stop still apply to it.
    """
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        return INTERNAL_NOTIFICATION_DISPLAY_KIND
    return None


def is_machinery_display_kind(display_kind: Any) -> bool:
    """Only a machinery turn may vanish on a bare silence marker; a human turn gets a visible fallback.

    The caller passes the current turn's persisted display kind instead of inferring it from the
    transcript: the inbound user row is not persisted yet, and a previous internal row must never
    authorize silence on a human turn.
    """
    return display_kind in MACHINERY_DISPLAY_KINDS


# A wake's own bracket tag (``[kanban review deadman] ...``): a short word label, never a
# ``[IMPORTANT: ...]`` / ``[ASYNC DELEGATION COMPLETE — id]`` sentence.
_WAKE_TAG_RE = re.compile(r"\A\s*\[([A-Za-z][A-Za-z0-9 _-]{0,39})\]")
_WAKE_CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")


def _wake_fallback_label(text: str, metadata: dict) -> str:
    head = text.lstrip()[:64].upper()
    if metadata.get("delegation_id") or head.startswith("[ASYNC DELEGATION"):
        return "delegation"
    if (metadata.get("notification_origin") == "process_registry_synthetic"
            or head.startswith(("[IMPORTANT: BACKGROUND PROCESS", "[BACKGROUND PROCESS"))):
        return "background process"
    return "internal"


def wake_progress_header(message: Any, display_kind: Any, metadata: Optional[dict] = None) -> Optional[str]:
    """The one header line opening an internal wake's progress bubbles, else ``None``.

    Internal wakes share the human's chat; an unattributed tool bubble from a silent wake reads
    as the agent dying mid-turn. Human turns get ``None`` so their bubbles stay unchanged.
    """
    if not is_machinery_display_kind(display_kind) or not isinstance(message, str) or not message.strip():
        return None
    tag = _WAKE_TAG_RE.match(message)
    label = " ".join(tag.group(1).split()) if tag else _wake_fallback_label(message, metadata or {})
    card = _WAKE_CARD_RE.search(message)
    if card:
        label = f"{label} · {card.group(0)}"
    from agent.i18n import t
    return t("gateway.progress.wake_head", label=label)


def wake_chrome_muted(display_kind: Any, source: Any, user_config: Optional[dict]) -> bool:
    """True when an internal wake runs in an act-now page channel: its chrome (tool-progress
    bubbles, interim messages, the "still working" heartbeat) is not a page, so it stays out
    (t_c40d3277: ~105 lines/day of "🔔 wake · internal" in fleet #alerts from a /loop watch
    homed there). The final reply is unchanged. ``display.wake_chrome_muted_chats`` lists chat
    ids; unset means the fleet #alerts channel on Discord; ``[]`` disables. Human turns never.
    """
    if not is_machinery_display_kind(display_kind):
        return False
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not chat_id:
        return False
    configured = ((user_config or {}).get("display") or {}).get("wake_chrome_muted_chats")
    if configured is None:
        from gateway.config import Platform
        if getattr(source, "platform", None) != Platform.DISCORD:
            return False
        from gateway.kanban_watchers import _needs_input_channels
        configured = [_needs_input_channels()[0]]
    if not isinstance(configured, (list, tuple, set)):
        configured = [configured]
    return chat_id in {str(c) for c in configured if c}


def silence_allowed(display_kind: Any, reply_expected: Optional[bool] = None) -> bool:
    """Whether a successful bare silence marker may remain silent for this turn."""
    return is_machinery_display_kind(display_kind) or reply_expected is False


def reply_expected_metadata(reply_expected: Optional[bool]) -> dict:
    """The persisted user row's ``reply_expected`` key, only when the adapter knew; crash recovery
    reads it back to judge a silence marker as the live turn did."""
    return {} if reply_expected is None else {"reply_expected": reply_expected}


# Literal model-emitted control tokens the trailing-line strip removes.  A
# subset of LIVE_GATEWAY_SILENT_MARKERS: the English-word forms ("SILENT",
# "NO REPLY") are excluded because a last line can legitimately say them.
TRAILING_STRIP_TOKENS = frozenset({SILENT_REPLY_TOKEN, "[SILENT]"})


def strip_trailing_silence_marker(response: Any) -> Any:
    """Drop a silence marker sitting alone on the LAST line of a delivered reply.

    Defensive counterpart to the silence rules above.  When a reply is going
    to be delivered anyway (a human turn under the exact-marker rule, where
    ``"short note\\nNO_REPLY"`` is prose, not silence), the trailing control
    token must not reach the chat as literal text (2026-10-03: a kanban
    lifecycle line pasted into a Telegram DM drew a "note + NO_REPLY" reply
    that was delivered verbatim, token included).

    Only a marker on its own final line is removed, and only when other
    content precedes it — a reply that IS the marker is left for the silence
    rules to suppress, and a token buried mid-sentence is untouched.

    Matching is STRICT, unlike the whole-response predicates: the last line
    must be a literal control token (:data:`TRAILING_STRIP_TOKENS`, exact
    case), optionally wrapped in symmetric markdown emphasis or backticks.
    The loose canonicalization (case-folding, ``NO REPLY`` / ``SILENT``,
    edge punctuation) is safe only when the WHOLE reply must be the marker;
    on the last line of a longer human reply it would delete real prose such
    as ``"No reply."`` or ``"Silent."`` (Prism #1668 c7fca8d2bc4c).

    Every consecutive trailing token line goes (``"note\\nNO_REPLY\\nNO_REPLY"``
    loses both), so the strip is idempotent: a path that cleans a reply twice
    delivers what a path that cleans it once does (Prism #1668 c7fca8d2bc4c).
    """
    if not isinstance(response, str):
        return response

    def _is_strip_token(line: str) -> bool:
        t = line.strip()
        while len(t) >= 2 and t[0] == t[-1] and t[0] in "*_`":
            t = t[1:-1].strip()
        return t in TRAILING_STRIP_TOKENS

    lines = response.rstrip().split("\n")
    end = len(lines)
    while end > 0 and (not lines[end - 1].strip() or _is_strip_token(lines[end - 1])):
        end -= 1
    if end == len(lines):
        return response
    head = "\n".join(lines[:end]).rstrip()
    if not head.strip():
        return response
    return head


def is_partial_silence_marker(text: Any) -> bool:
    """True while streamed ``text`` could still resolve to a silence marker.

    A buffer whose canonical form is a non-empty *prefix* of a marker (``"NO"`` on
    the way to ``"NO_REPLY"``, or an exact marker not yet terminated by stream-end)
    is held back so a raw marker is never shown and then retracted.  Divergence
    from every marker, or exceeding the cap, resumes normal streaming.
    """
    return any(
        c and any(marker.startswith(c) for marker in LIVE_GATEWAY_SILENT_MARKERS)
        for c in _canonical_silence_candidates(text)
    )
