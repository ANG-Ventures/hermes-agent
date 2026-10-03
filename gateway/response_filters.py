"""Gateway response filtering helpers.

These helpers operate at the gateway boundary: they decide whether a completed
agent turn should be delivered to the chat, not what should be persisted in the
conversation history.
"""

from __future__ import annotations

import unicodedata
from typing import Any

# Canonical model-emitted control token for intentional silence.
SILENT_REPLY_TOKEN = "NO_REPLY"

# Exact whole-response markers that mean "the agent intentionally chose not to
# reply".  Keep this list small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Models sometimes emit ``.NO_REPLY`` or ``*NO_REPLY*`` instead of the exact
    marker. Keep square brackets structural so malformed ``[SILENT`` does not
    become ``SILENT``.
    """
    start = 0
    end = len(text)
    while start < end and text[start] not in "[]" and unicodedata.category(text[start]).startswith("P"):
        start += 1
    while end > start and text[end - 1] not in "[]" and unicodedata.category(text[end - 1]).startswith("P"):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    fallback = _canonical_silence_candidate(stripped)
    return (exact, fallback)


def is_intentional_silence_response(response: Any) -> bool:
    """Return True only when ``response`` is exactly a silence marker.

    Substantive prose that merely mentions ``NO_REPLY`` or ``[SILENT]`` must be
    delivered normally.  A blank response is also not silence; blank output is
    handled by the empty-response failure path.
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False
    if len(stripped) > 64:
        return False
    return any(candidate in LIVE_GATEWAY_SILENT_MARKERS for candidate in _canonical_silence_candidates(stripped))


def is_autonomous_silence_response(response: Any) -> bool:
    """Loose silence matcher for autonomous lanes (cron, webhook).

    Autonomous lanes instruct the agent to emit ``[SILENT]`` when a tick
    produced nothing worth a human's attention, and models reliably bracket
    the marker with a short note explaining why they stayed quiet.  Unlike
    :func:`is_intentional_silence_response` (the interactive-chat rule, which
    demands the response be EXACTLY a marker), this suppresses when a marker
    is the whole response, sits on its own first or last line, or the
    bracketed sentinel opens the response (the documented
    ``[SILENT] No changes detected`` pattern).  A token buried mid-sentence
    in a genuine report is still delivered.

    Shares :data:`LIVE_GATEWAY_SILENT_MARKERS` so the interactive and
    autonomous marker sets can never drift apart.
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False

    def _strip_code_wrap(s: str) -> str:
        # Peel a whole-value markdown code fence, then any inline backtick span.
        # An agent cron (whose reply IS its final message) routinely formats the
        # literal sentinel, which would otherwise leak to the channel as noise.
        # Only *symmetric* wrapping is stripped, so real prose is untouched.
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

    def _is_token(line: str) -> bool:
        return (
            _canonical_silence_candidate(line) in LIVE_GATEWAY_SILENT_MARKERS
            or _canonical_silence_candidate(_strip_code_wrap(line))
            in LIVE_GATEWAY_SILENT_MARKERS
        )

    # Whole response is exactly a token (bare, code-spanned, or fenced).
    if _is_token(stripped):
        return True
    # Marker on its own first or last line (leading/trailing note on a
    # separate line — e.g. "2 deals filtered\n\n[SILENT]").
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and (_is_token(lines[0]) or _is_token(lines[-1])):
        return True
    # Bracketed sentinel used as a same-line prefix — the documented pattern
    # "[SILENT] No changes detected".  Restricted to the bracketed form so a
    # bare word like "Silent retry succeeded" is NOT swallowed.
    # Peel a leading inline code-span (any backtick-run length) first so
    # "`[SILENT]` note" / "``[SILENT]`` note" also count (same reflex).
    head = stripped
    if head.startswith("`"):
        run = len(head) - len(head.lstrip("`"))
        close = head.find("`" * run, run)
        if close != -1:
            head = head[run:close].strip()
    if head.upper().startswith("[SILENT]"):
        return True
    return False


def is_intentional_silence_agent_result(
    agent_result: dict | None, response: Any, *, internal: bool = False,
) -> bool:
    """Silence markers suppress delivery only for successful agent turns.

    ``internal`` marks a turn triggered by a system-generated gateway event
    (background-process completion, delegation batch, restore replay).  No
    human is waiting on those, so they get the autonomous-lane rule
    (:func:`is_autonomous_silence_response`: marker on its own first/last
    line); human turns keep the exact-marker rule.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
    if internal:
        return is_autonomous_silence_response(response)
    return is_intentional_silence_response(response)


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
    """Return True while ``text`` could still resolve to a silence marker.

    The streaming path accumulates the reply delta-by-delta and must decide,
    before the whole response is known, whether to show what it has so far.
    A buffer whose canonical form is a non-empty *prefix* of a silence marker
    (e.g. ``"NO"`` on the way to ``"NO_REPLY"``, or an exact marker that has
    not yet been terminated by stream-end) is held back so a raw marker is
    never edited onto the screen and then belatedly retracted.

    Anything that has already diverged from every marker (ordinary prose) —
    and anything longer than the marker cap — returns False so normal
    streaming resumes immediately.  This is the streaming counterpart to
    :func:`is_intentional_silence_response`, sharing the same marker set and
    canonicalization so the two never drift.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    for candidate in _canonical_silence_candidates(stripped):
        if candidate and any(marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS):
            return True
    return False
