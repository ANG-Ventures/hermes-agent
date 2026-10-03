"""One-time per-session notice when a Codex prompt crosses the 272K price tier.

Under ``model.codex_context_policy: large`` (default) an eligible bare Codex
slug runs with its live-verified large window (e.g. gpt-6.1-sol ~900K), so the
old 272K autoraise banner no longer fires. The ABOVE-272K per-request price
tier still exists, though (2x input, 1.5x output, 2x cache read on
gpt-6-sol/6.1-sol; the Codex subscription meters ~2x above 272K). This module
surfaces that once per session, the first time a real provider-reported
prompt count exceeds 272,000 tokens (t_a56d83c1).

Display-only: the notice goes through ``agent._emit_status`` (CLI print +
gateway ``status_callback("lifecycle", ...)``). It never touches the message
list, so the wire body and the prompt cache are unchanged.

Off switch: ``compression.codex_tier_notice: false``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-request price tier boundary on the Codex/OpenAI large-window families.
CODEX_PRICE_TIER_TOKENS = 272_000

# Sessions that already got the notice. In-process (the gateway may rebuild
# the agent per inbound message) plus a bounded per-profile marker file so a
# process restart mid-session does not repeat it.
_SEEN_LOCK = threading.Lock()
_SEEN_SESSIONS: "dict[str, None]" = {}
_MARKER_MAX_ENTRIES = 512


def _marker_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / ".codex_tier_notice_sessions"


def _load_marker() -> list:
    try:
        text = _marker_path().read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _session_seen(session_id: str) -> bool:
    with _SEEN_LOCK:
        if session_id in _SEEN_SESSIONS:
            return True
    return session_id in _load_marker()


def _record_session(session_id: str) -> None:
    with _SEEN_LOCK:
        _SEEN_SESSIONS[session_id] = None
        while len(_SEEN_SESSIONS) > _MARKER_MAX_ENTRIES:
            _SEEN_SESSIONS.pop(next(iter(_SEEN_SESSIONS)))
    try:
        entries = [e for e in _load_marker() if e != session_id]
        entries.append(session_id)
        entries = entries[-_MARKER_MAX_ENTRIES:]
        path = _marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        # Best-effort: an unwritable home only means a restart may re-notify.
        pass


def _provider_is_codex(provider: Optional[str]) -> bool:
    """True for providers that serve Codex-family slugs from a Codex sub.

    Uses the shared ``provider_serves_codex_subscription`` predicate when the
    host has it (t_c1403b02, cpa lane), else only ``openai-codex``. No
    provider is special-cased here.
    """
    prov = (provider or "").strip().lower()
    if not prov:
        return False
    try:
        from agent import model_metadata as _mm

        shared = getattr(_mm, "provider_serves_codex_subscription", None)
    except Exception:
        shared = None
    if callable(shared):
        try:
            return bool(shared(prov))
        except Exception:
            return prov == "openai-codex"
    return prov == "openai-codex"


def codex_tier_notice_applies(model: Optional[str], provider: Optional[str]) -> bool:
    """True when *model* on *provider* runs the large Codex window under ``large``.

    Never under ``advertised`` (the old autoraise banner covers that mode) and
    never for non-Codex providers.
    """
    if not _provider_is_codex(provider):
        return False
    from agent.model_metadata import (
        CODEX_CONTEXT_POLICY_LARGE,
        codex_context_policy,
        codex_uses_large_window,
    )

    if codex_context_policy() != CODEX_CONTEXT_POLICY_LARGE:
        return False
    return codex_uses_large_window(model)


def build_codex_tier_notice(model: Optional[str], threshold_tokens: Optional[int]) -> str:
    slug = (model or "").strip().rsplit("/", 1)[-1] or "codex"
    if isinstance(threshold_tokens, int) and threshold_tokens > 0:
        compact = f"{round(threshold_tokens / 1000)}K"
    else:
        compact = "the configured threshold"
    return (
        f"ℹ context passed 272K on {slug}: turns now price at the above-272K "
        f"tier (2× input); compaction at {compact}. "
        f"Flip back: model.codex_context_policy: advertised"
    )


def maybe_emit_codex_tier_notice(agent: Any, prompt_tokens: Any) -> Optional[str]:
    """Emit the tier notice once per session on the first crossing.

    ``prompt_tokens`` must be the provider-reported prompt count of the
    response just received. Returns the emitted text, or ``None``.
    Never raises.
    """
    try:
        if not getattr(agent, "_codex_tier_notice_enabled", True):
            return None
        if getattr(agent, "_codex_tier_notice_shown", False):
            return None
        try:
            tokens = int(prompt_tokens or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= CODEX_PRICE_TIER_TOKENS:
            return None
        model = getattr(agent, "model", None)
        if not codex_tier_notice_applies(model, getattr(agent, "provider", None)):
            return None
        session_id = str(getattr(agent, "session_id", "") or "")
        if session_id and _session_seen(session_id):
            agent._codex_tier_notice_shown = True
            return None
        compressor = getattr(agent, "context_compressor", None)
        threshold = getattr(compressor, "threshold_tokens", None)
        msg = build_codex_tier_notice(model, threshold)
        agent._codex_tier_notice_shown = True
        if session_id:
            _record_session(session_id)
        agent._emit_status(msg)
        return msg
    except Exception as exc:  # display-only; never break the turn
        logger.debug("codex tier notice failed: %s", exc)
        return None
