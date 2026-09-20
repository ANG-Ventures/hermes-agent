"""Validation for the out-of-band ``hermes_confab_notice`` response extension.

A bridge that detects and strips self-confabulated scaffold text from a model
reply must still tell the user the reply was contaminated — that signal is
load-bearing for triage. Delivering it *in band* (appended to assistant
``content``) mutates conversation history and is replayed upstream on every
full-history recovery, so the notice moves to a top-level response-envelope
extension instead:

.. code-block:: json

    {"hermes_confab_notice": {"version": 1,
                              "kind": "scaffold_confab_removed",
                              "request_id": "3b264082",
                              "scope": "visible",
                              "grammar": "inbound"}}

This module owns the *only* gate between that untrusted provider-supplied
payload and anything user-visible or persisted. It fails closed: an unknown
version, a wrong ``kind``, a bad ``scope``, a non-string ``request_id``, or a
duplicate notice in one response yields ``None`` and a debug log. Callers must
never display or persist a payload this function rejected.

The returned object is a fresh dict containing only the validated keys — a
provider cannot smuggle extra fields into ``display_metadata`` by attaching
them to the extension.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Top-level key the bridge attaches to the completion / final usage chunk.
CONFAB_NOTICE_FIELD = "hermes_confab_notice"

#: Key under which the validated notice is carried on
#: ``NormalizedResponse.provider_data`` and inside ``display_metadata``.
CONFAB_NOTICE_KEY = "confab_notice"

#: ``display_kind`` stamped on the assistant row carrying a notice.
CONFAB_NOTICE_DISPLAY_KIND = "confab_notice"

#: The only schema version this consumer understands.
CONFAB_NOTICE_VERSION = 1

#: Original scaffold kind; retained for callers that distinguish status-only notices.
CONFAB_NOTICE_KIND = "scaffold_confab_removed"

# Tool guards may be the entire visible turn. Unlike the scaffold status,
# these local instructions become assistant content and may be replayed so
# the model can recover. Never interpolate provider-supplied labels here.
TOOL_CALL_NOTICE_TEXT = {
    "tool_call_unparseable": (
        "Tool call not executed: the tool-call JSON could not be parsed. "
        "Re-issue the tool call with valid JSON matching the tool schema."
    ),
    "tool_call_as_text": (
        "Tool call not executed: it was written as text rather than a native tool call. "
        "Re-issue the call using the native tool-calling interface, not a text block."
    ),
}
CONFAB_NOTICE_KINDS = (CONFAB_NOTICE_KIND, *TOOL_CALL_NOTICE_TEXT)

#: Allowed ``scope`` values.
CONFAB_NOTICE_SCOPES = ("visible", "intermediate", "both")

#: User-facing status line. Out of band, but never invisible — this is the
#: current-turn half of the triage contract.
CONFAB_NOTICE_TEXT = (
    "⚠️ Confabulation caught: the provider detected and removed "
    "self-fabricated scaffold text from this reply."
)

# Defensive bound — ``request_id`` and ``grammar`` are short opaque labels.
_MAX_LABEL_LEN = 256


def validate_confab_notice(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a sanitized copy of *raw* if it is a valid v1 notice, else ``None``.

    Fails closed on every deviation from the contract. Never raises.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.debug(
                "Ignoring %s: expected object, got %s",
                CONFAB_NOTICE_FIELD,
                type(raw).__name__,
            )
        return None

    version = raw.get("version")
    # bool is an int subclass — True would otherwise pass as version 1.
    if isinstance(version, bool) or version != CONFAB_NOTICE_VERSION:
        logger.debug("Ignoring %s: unsupported version %r", CONFAB_NOTICE_FIELD, version)
        return None

    kind = raw.get("kind")
    if kind not in CONFAB_NOTICE_KINDS:
        logger.debug("Ignoring %s: unknown kind %r", CONFAB_NOTICE_FIELD, kind)
        return None

    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        logger.debug(
            "Ignoring %s: request_id must be a non-empty string, got %r",
            CONFAB_NOTICE_FIELD,
            request_id,
        )
        return None
    if len(request_id) > _MAX_LABEL_LEN:
        logger.debug("Ignoring %s: request_id too long", CONFAB_NOTICE_FIELD)
        return None

    scope = raw.get("scope")
    if scope not in CONFAB_NOTICE_SCOPES:
        logger.debug("Ignoring %s: invalid scope %r", CONFAB_NOTICE_FIELD, scope)
        return None
    if kind != CONFAB_NOTICE_KIND and scope != "visible":
        logger.debug("Ignoring %s: tool-call scope must be visible", CONFAB_NOTICE_FIELD)
        return None

    # ``grammar`` is the detector's bounded label, or null when several catches
    # cannot be represented by one label. Absent is treated as null.
    grammar = raw.get("grammar")
    if grammar is not None:
        if not isinstance(grammar, str) or not grammar.strip():
            logger.debug("Ignoring %s: invalid grammar %r", CONFAB_NOTICE_FIELD, grammar)
            return None
        if len(grammar) > _MAX_LABEL_LEN:
            logger.debug("Ignoring %s: grammar too long", CONFAB_NOTICE_FIELD)
            return None

    return {
        "version": CONFAB_NOTICE_VERSION,
        "kind": kind,
        "request_id": request_id,
        "scope": scope,
        "grammar": grammar,
    }


def extract_confab_notice(obj: Any) -> Optional[Dict[str, Any]]:
    """Pull ``hermes_confab_notice`` off a completion / chunk and validate it.

    Reads the attribute first (SimpleNamespace stubs, permissive SDK models)
    and falls back to the OpenAI SDK's ``model_extra`` bag, which is where an
    unknown top-level field lands on a pydantic response model. Returns the
    sanitized notice or ``None``.
    """
    if obj is None:
        return None
    raw = getattr(obj, CONFAB_NOTICE_FIELD, None)
    if raw is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            raw = extra.get(CONFAB_NOTICE_FIELD)
    if raw is None and isinstance(obj, dict):
        raw = obj.get(CONFAB_NOTICE_FIELD)
    if raw is None:
        return None
    return validate_confab_notice(raw)
