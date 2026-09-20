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
payload and anything user-visible or persisted. ``validate_confab_notice``
fails closed: an unknown version, a wrong ``kind``, a bad ``scope``, or a
non-string ``request_id`` yields ``None`` and a debug log. Callers must never
display or persist a payload this function rejected.

The one-notice-per-response half of the contract is enforced by the *callers*,
because only they can see a whole response: the streaming aggregator in
``agent/chat_completion_helpers.py`` marks its accumulator invalid and
forwards NO notice once a second valid notice appears in one stream.

Two reader-side helpers complete the contract:

* ``notice_from_display_row`` re-validates a persisted row before any surface
  presents the confirmed-catch claim, and requires an ``assistant`` role —
  ``display_kind`` alone is an open string any writer can set;
* ``should_announce_notice`` scopes the announce-once ledger to the current
  turn, so a colliding or restarted provider ``request_id`` can never silently
  suppress a genuine later warning.

The returned object is a fresh dict containing only the validated keys — a
provider cannot smuggle extra fields into ``display_metadata`` by attaching
them to the extension.
"""

from __future__ import annotations

import json
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

#: The only catch kind defined by v1 of the contract.
CONFAB_NOTICE_KIND = "scaffold_confab_removed"

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
    if kind != CONFAB_NOTICE_KIND:
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
        "kind": CONFAB_NOTICE_KIND,
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


#: Attribute holding the per-turn announce ledger on the agent.
_ANNOUNCE_LEDGER_ATTR = "_confab_notices_announced"


def notice_from_display_row(
    role: Any, display_kind: Any, display_metadata: Any
) -> Optional[Dict[str, Any]]:
    """Return the validated notice a persisted row genuinely carries, else ``None``.

    ``display_kind`` is an open-ended string column that any writer — a
    history importer, a migration, a malformed or hand-edited record — can
    populate. Claiming "confabulation caught" on the strength of that string
    alone asserts a CONFIRMED provider catch that may never have happened, on
    a row that may not even be a model reply.

    So a reader must clear two gates before presenting the claim:

    1. the row is an ``assistant`` turn — only a model reply can carry a
       catch; a user or system row tagged this way is malformed input, and
    2. ``display_metadata[CONFAB_NOTICE_KEY]`` re-validates against the same
       fail-closed v1 schema the wire payload had to pass.

    Returns the re-validated notice (so callers can key off ``kind`` /
    ``request_id``) or ``None`` when the row does not qualify.
    """
    if role != "assistant":
        return None
    if display_kind != CONFAB_NOTICE_DISPLAY_KIND:
        return None

    metadata = display_metadata
    # A row round-tripped through a store that serializes the column arrives
    # as JSON text; a reader must not credit it or reject it on shape alone.
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return None
    if not isinstance(metadata, dict):
        return None

    return validate_confab_notice(metadata.get(CONFAB_NOTICE_KEY))


def should_announce_notice(agent: Any, notice: Any, turn_id: Any) -> bool:
    """Return ``True`` the FIRST time *notice* is seen within *turn_id*.

    The user-facing warning must fire exactly once per accepted notice even
    when a retry or a provider failover re-normalizes the same response
    object. That is the only job of this ledger — and its scope is therefore
    the logical turn, never the agent's lifetime.

    A lifetime-scoped ledger keyed only on the provider-supplied
    ``request_id`` silently SUPPRESSES a genuine later warning whenever a
    bridge restarts its ID sequence, two providers issue the same ID, or an ID
    simply collides. Suppressing the signal this whole feature exists to
    deliver is strictly worse than announcing twice, so:

    * the key carries the turn identity as well as the request id, and
    * the ledger is evicted the moment the turn changes, so it cannot grow
      without bound or leak a key into a later turn.
    """
    if not isinstance(notice, dict):
        return False
    request_id = notice.get("request_id")

    ledger = getattr(agent, _ANNOUNCE_LEDGER_ATTR, None)
    # Evict on turn change: a previous turn's keys can never suppress this
    # turn's warning. Also repairs a ledger left behind in the pre-scoping
    # (bare set) shape by an older build.
    if not isinstance(ledger, dict) or ledger.get("turn_id") != turn_id:
        ledger = {"turn_id": turn_id, "keys": set()}
        setattr(agent, _ANNOUNCE_LEDGER_ATTR, ledger)

    key = (turn_id, request_id)
    if key in ledger["keys"]:
        return False
    ledger["keys"].add(key)
    return True
