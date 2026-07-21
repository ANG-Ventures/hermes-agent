"""Neutral provider/model string utility shared across surfaces.

Lives in a low-level module that BOTH ``gateway/runtime_footer.py`` and
``agent/conversation_compression.py`` can import without creating a
gateway↔agent circular import (gateway already imports agent).
"""

from __future__ import annotations

from typing import Optional, Tuple


def split_provider_model(
    provider: Optional[str], model: Optional[str]
) -> Tuple[str, str]:
    """Resolve a clean ``(provider, model)`` pair.

    When the ``model`` ALREADY carries a ``provider/`` prefix, that embedded
    prefix wins and any separately-supplied ``provider`` is ignored — this
    avoids an ugly triple like ``claude-app/claude-app/claude-opus-4-8`` when a
    caller passes both a provider and a prefixed model (the live config has
    ``model.default: claude-app/claude-opus-4-8`` AND ``model.provider:
    claude-app``). The model's own prefix is the more specific source.
    """
    prov = (provider or "").strip()
    mdl = (model or "").strip()
    if "/" in mdl:
        # The model carries its own provider prefix — it's authoritative.
        prov, _, mdl = mdl.partition("/")
    return prov, mdl


def format_provider_model(provider: Optional[str], model: Optional[str]) -> str:
    """Render ``provider/model`` (or bare ``model`` when no provider), de-duped."""
    prov, mdl = split_provider_model(provider, model)
    if prov and mdl:
        return f"{prov}/{mdl}"
    return mdl


import re as _re

# Bridge providers front Claude via the Claude Code CLI (subscription billing),
# which keeps its OWN resident CLI session per conversation. On these providers
# the operative context manager is the Claude Code CLI itself — NOT the harness
# context engine (LCM). LCM still INGESTS (the raw lcm.db store stays live, so
# lcm_grep/lcm_expand still work as a fallback), but LCM compaction does not
# shrink the CLI-side resident session, so labeling the compaction surface
# "engine: lcm" is misleading. We relabel the DISPLAY to "cc" on these providers.
#
# Matches: claude-bpr (pool face), claude-bpx-N (per-sub bridge), and the legacy
# names claude-bridge / claude-bridge-fN / claude-bpp. Does NOT match the native
# proxy lanes claude-apr / claude-apx-N (those forward Anthropic-native and the
# harness context engine works normally there).
_BRIDGE_PROVIDER_RE = _re.compile(
    r"^claude-(?:bpr|bpp|bpx-\d+|bridge(?:-f\d+)?)$"
)


def is_bridge_provider(provider: Optional[str]) -> bool:
    """True if ``provider`` fronts Claude via the Claude Code CLI bridge.

    Bridge providers keep a resident CLI session that manages context itself;
    the harness engine (LCM) ingests but is not the operative compactor. Used to
    relabel the compaction engine display from ``lcm`` to ``cc`` (Claude Code).
    """
    prov = (provider or "").strip()
    # A provider may arrive bare ("claude-bpx-15") or as a "provider/model"
    # string ("claude-bpx-15/claude-fable-5"); take the provider segment.
    if "/" in prov:
        prov = prov.split("/", 1)[0].strip()
    return bool(prov and _BRIDGE_PROVIDER_RE.match(prov))


def engine_display_label(
    engine_name: Optional[str], provider: Optional[str]
) -> Optional[str]:
    """The engine label to DISPLAY for a compaction surface.

    On bridge providers the LCM engine is relabeled ``cc`` (Claude Code) because
    the Claude Code CLI's own resident session — not LCM — is what actually
    manages the wire context. Everywhere else the label is the engine's real
    name. This is a DISPLAY-only mapping; it never changes gating or behavior
    (callers keep using the real ``engine_name == "lcm"`` test for that).
    """
    if engine_name == "lcm" and is_bridge_provider(provider):
        return "cc"
    return engine_name
