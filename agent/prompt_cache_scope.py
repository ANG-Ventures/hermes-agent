"""Rotation-stable logical cache scope for prompt_cache_key derivation.

Context-compression rotation (legacy ``compression.in_place: false`` mode)
mints a new physical ``session_id`` mid-conversation to segment the
transcript. The prompt-cache scope introduced by #79161 was derived from that
physical id, so every rotation moved the conversation into a fresh cache
bucket even though it is logically the same conversation continuing
(issue #79017).

``resolve_prompt_cache_scope()`` maps the physical session id to the ROOT of
its *compression lineage* — the pre-rotation session id — using
``SessionDB.get_compression_lineage()``, whose fork-aware semantics
(hardened in #79193) give exactly the scope boundaries the cache key needs.
NOT ``SessionDB.get_conversation_root`` / ``run_agent._conversation_root_id``
(the Portal-attribution walk): that one follows ``parent_session_id`` blindly,
collapsing /branch children and whole delegate trees into one id, which would
violate the #79161 isolation this scope must preserve. The two resolvers are
intentionally different — do not "deduplicate" them.

- compression-rotation children walk back to the original segment
  (rotation-stable scope — the fix);
- ``/new`` starts a lineage-less session (fresh scope);
- ``/branch`` children (``_branched_from``), delegate subagents
  (``_delegate_from``), and tool-tagged children (``source="tool"``) are
  explicit fork children and keep their own isolated scope, preserving the
  sibling/subagent isolation #79161 established;
- cron fires keep their physical ``cron_<job>_<ts>`` id here — the per-fire
  timestamp is stripped later by ``_cache_scope_from_session_id`` exactly as
  before.

The resolution is memoized per (agent, session_id): the lineage walk runs
once per transcript segment — NOT per API call — and re-runs only when
rotation actually changes ``agent.session_id`` (per the no-DB-on-the-hot-path
constraint recorded on #79017). Default installs compact in place and never
rotate, so they hit the memo forever and behave byte-identically to before.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MEMO_ATTR = "_prompt_cache_scope_memo"


def _lineage_root(session_id: str, session_db: Any) -> Optional[str]:
    """Return the compression-lineage root of *session_id*, or None.

    Defensive about the DB handle: test doubles and partially constructed
    agents can hand back non-list results — anything that is not a non-empty
    list/tuple whose first element is a non-empty string is ignored.
    """
    if session_db is None:
        return None
    try:
        lineage = session_db.get_compression_lineage(session_id)
    except Exception:
        logger.debug("prompt-cache scope lineage walk failed", exc_info=True)
        return None
    if isinstance(lineage, (list, tuple)) and lineage:
        root = lineage[0]
        if isinstance(root, str) and root:
            return root
    return None


def resolve_prompt_cache_scope(agent: Any) -> str:
    """Resolve the rotation-stable cache-scope id for *agent*'s conversation.

    Returns the compression-lineage ROOT of ``agent.session_id`` (the
    physical id itself when the session has no compression ancestry, no DB
    is attached, or the walk fails). The result is memoized on the agent
    keyed by the current session id, so the DB walk happens once per
    transcript segment rather than once per API call.
    """
    sid = str(getattr(agent, "session_id", None) or "")
    if not sid:
        return ""
    db = getattr(agent, "_session_db", None)
    # Memo key includes DB presence: an agent that starts DB-less and gains a
    # handle later (run_agent._get_session_db_for_recall lazily attaches one)
    # must re-resolve instead of staying pinned to the physical id.
    key = (sid, db is not None)
    memo = getattr(agent, _MEMO_ATTR, None)
    if isinstance(memo, tuple) and len(memo) == 2 and memo[0] == key:
        return _apply_fork_tag(agent, memo[1])
    return _apply_fork_tag(agent, _resolve_uncached(agent, sid, db, key))


def is_slot_keyed_cache_route(provider: Any, model: Any, base_url: Any = "") -> bool:
    """True when the cache key selects ONE server-side slot per conversation.

    xAI (direct ``xai``/``xai-oauth``, ``api.x.ai``, or Grok routed via
    OpenRouter as ``x-ai/grok-*``) pins its prompt cache to the server picked
    by ``x-grok-conv-id`` / ``prompt_cache_key``: two divergent request
    streams under one key evict each other. Anthropic, DeepSeek and Gemini
    caches are content-addressed and OpenAI's ``prompt_cache_key`` only routes
    over a prefix match, so sharing the key there is harmless (and saves the
    fork a cold write).
    """
    p = str(provider or "").strip().lower()
    if p in {"xai", "xai-oauth"}:
        return True
    if "api.x.ai" in str(base_url or "").lower():
        return True
    m = str(model or "").strip().lower()
    return m.startswith(("x-ai/grok-", "xai/grok-"))


# ``<scope>::<tag>`` — a double colon so gateway-style session keys that
# already contain single colons are never mistaken for a fork scope.
FORK_SCOPE_SEPARATOR = "::"


def is_fork_cache_scope(scope: Any) -> bool:
    """True when *scope* is a fork-derived scope (``<scope>::<tag>``)."""
    return isinstance(scope, str) and FORK_SCOPE_SEPARATOR in scope


def _apply_fork_tag(agent: Any, scope: str) -> str:
    """Derive ``<scope>::<tag>`` for a tagged fork on a slot-keyed provider.

    Background-review forks share the parent's ``session_id`` (and therefore
    its scope) so content-addressed caches serve them warm. On a slot-keyed
    provider that same key makes the fork evict the parent's conversation
    slot — measured on xai-oauth: parent's next call read 1,152 of ~356k
    prompt tokens after a fork. Re-evaluated per call (cheap; no DB), so a
    mid-run provider fallback picks the right shape.
    """
    tag = getattr(agent, "_prompt_cache_fork_tag", None)
    if not scope or not isinstance(tag, str) or not tag:
        return scope
    if not is_slot_keyed_cache_route(
        getattr(agent, "provider", ""),
        getattr(agent, "model", ""),
        getattr(agent, "base_url", ""),
    ):
        return scope
    return f"{scope}{FORK_SCOPE_SEPARATOR}{tag}"


def _resolve_uncached(agent: Any, sid: str, db: Any, key: tuple) -> str:
    root = _lineage_root(sid, db) if db is not None else None
    scope = root or sid
    # Memoize on a successful walk, or when there is no DB to consult at all,
    # or when the agent will never persist a row (background-review forks set
    # _persist_disabled but still hold a DB handle — without this, every API
    # call would re-run the lineage query forever).
    # A failed/empty walk on a persisting agent is NOT memoized: falling back
    # to the physical id is the correct degraded answer right now (row not
    # persisted yet, transient DB error), but pinning it for the whole segment
    # would keep the scope wrong after the session row lands.
    if (
        root is not None
        or db is None
        or getattr(agent, "_persist_disabled", False)
    ):
        try:
            setattr(agent, _MEMO_ATTR, (key, scope))
        except Exception:
            # Frozen/slotted test doubles — resolution still works, just
            # unmemoized.
            pass
    return scope


def resolve_prompt_cache_scope_safe(agent: Any) -> Optional[str]:
    """Never-raising variant of :func:`resolve_prompt_cache_scope`.

    Returns None on any failure (or when there is no scope). Consumers treat
    None/empty as "fall back to the physical session_id", so a resolution
    failure degrades to pre-#79017 behavior instead of blocking the caller —
    important at turn_context's call site, where an exception raised inside
    the ``set_runtime_main(...)`` argument list would otherwise skip the whole
    runtime binding, not just the cache scope.
    """
    try:
        return resolve_prompt_cache_scope(agent) or None
    except Exception:
        logger.debug("prompt-cache scope resolution failed", exc_info=True)
        return None
