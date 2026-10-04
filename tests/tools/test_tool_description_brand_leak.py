"""BL-desc — tool/parameter DESCRIPTIONS must be brand/provider/transport-neutral.

WHY THIS EXISTS (t_79ef440f, follow-up of t_71c254fd)
------------------------------------------------------
Every tool description this process ships inside ``tools[]`` egresses on ~every
request to whatever upstream the active provider points at. Measured
2026-10-03 on the real 53-tool surface: 46 brand/provider/transport
occurrences ('hermes' x35, 'openai' x5, 'anthropic' x3, 'claude' x2,
'kimi' x3, 'daedalus' x3, 'nous' x1) — enough to make the apx relay's
Layer-5 description strip load-bearing (SPEC-leak-prevention L7: tool
descriptions MUST be brand- and provider-neutral on the wire).

This gate is the SOURCE half of that fix: fail when any registered tool's
description (tool-level or any nested parameter ``description``) contains a
forbidden token from the SHARED leak corpus — the same ``bl-tokens.json``
the apx/bpx/pool BL gates consume (card t_0f8201a6, "ONE list, four
consumers"). Vendored byte-identically at ``tests/tools/bl-tokens.json`` and
pinned by SHA-256 below, so a silently-swapped or locally-edited corpus is a
RED, not a silent pass (SPEC L10/L11 anti-gutting).

SCOPE — fail-closed but not noise (SPEC L2):
  * ALL registered built-in tools' schemas are scanned (the registry is the
    choke point every description passes through).
  * Tools whose own NAME is the vendor surface (the x-lane: x_search,
    xai_video_edit, xai_video_extend) are adjudicated EXCLUDED below: their
    function is literally that vendor's API, the tool name already says it,
    and stripping the provider token from their descriptions degrades tool
    selection without removing any fingerprint. The exclusion is enumerated,
    not a glob, so a NEW tool can never ride it silently (SPEC L11).
  * ``allowed_substrings`` from the corpus are honored (vendor header names,
    the upstream host) — the false-positive guard that keeps the gate narrow
    enough to keep believing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO, "tests", "tools", "bl-tokens.json")

# SHA-256 of the vendored bl-tokens.json as imported from claude-apx
# (test/parity/bl-tokens.json, version 1, 2026-08-08, card t_0f8201a6).
# Re-vendor from the canonical copy and update this pin IN THE SAME COMMIT —
# a corpus change without a pin change is the gutting shape L11 exists to catch.
CORPUS_SHA256 = "9f4d8d635f035d4355543319bb9857eeb2cc7b277e246e68ff2e33e1a21b4c2b"

# Enumerated adjudicated exclusions (tool name -> why its descriptions may
# name a provider). An entry here is a hole in the gate; keep it short.
# NOT a glob: a new vendor tool must be added by hand, with the reason.
EXCLUDED_TOOLS = {
    "x_search": "xAI-only tool; the tool name and function ARE the vendor",
    "xai_video_edit": "xAI-only tool; the tool name and function ARE the vendor",
    "xai_video_extend": "xAI-only tool; the tool name and function ARE the vendor",
}


def _load_corpus():
    with open(CORPUS_PATH, "rb") as fh:
        raw = fh.read()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != CORPUS_SHA256:
        raise RuntimeError(
            f"bl-tokens.json sha256 mismatch: {sha} != pinned {CORPUS_SHA256} — "
            "the vendored corpus was edited without updating the pin (or vice versa)"
        )
    doc = json.loads(raw.decode("utf-8"))
    tokens = [e["value"] for e in doc["forbidden_tokens"]]
    phrases = [e["value"] for e in doc["forbidden_phrases"]]
    allowed = [e["value"] for e in doc.get("allowed_substrings", [])]
    # Fail closed on an empty corpus: 'no cases apply' is an error, never green.
    if not tokens and not phrases:
        raise RuntimeError("bl-tokens.json has no forbidden entries — refusing to report green")
    return tokens, phrases, allowed


def _description_strings(schema, path, out):
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k == "description" and isinstance(v, str):
                out.append((path, v))
            else:
                _description_strings(v, f"{path}.{k}", out)
    elif isinstance(schema, list):
        for i, item in enumerate(schema):
            _description_strings(item, f"{path}[{i}]", out)


def _hits(text, needles, allowed):
    low = text.lower()
    found = []
    for n in needles:
        start = 0
        while True:
            i = low.find(n.lower(), start)
            if i == -1:
                break
            span = (i, i + len(n))
            # Honor allowed_substrings: if this hit is fully inside an allowed
            # span, it is the corpus's own false-positive control, not a leak.
            covered = any(
                low.find(a.lower()) != -1
                and low.find(a.lower()) <= span[0]
                and span[1] <= low.find(a.lower()) + len(a)
                for a in allowed
            )
            if not covered:
                found.append((n, max(0, i - 40), min(len(text), i + len(n) + 40)))
            start = i + 1
    return found


def test_tool_descriptions_are_brand_neutral():
    tokens, phrases, allowed = _load_corpus()
    needles = tokens + phrases

    from tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    entries = registry._snapshot_entries()
    if not entries:
        raise RuntimeError("tool registry is empty — refusing to report green")

    failures = []
    for entry in entries:
        if entry.name in EXCLUDED_TOOLS:
            continue
        descs = []
        _description_strings(entry.schema, entry.name, descs)
        for path, text in descs:
            for needle, lo, hi in _hits(text, needles, allowed):
                failures.append(
                    f"{path}: forbidden token {needle!r} in description: "
                    f"...{text[lo:hi]!r}..."
                )
    assert not failures, (
        f"{len(failures)} brand/provider/transport token(s) in tool descriptions "
        f"(SPEC-leak-prevention L7; corpus bl-tokens.json v1):\n" + "\n".join(failures[:50])
    )


def test_core_toolset_has_no_brand_tokens():
    """The 53-tool core surface specifically: the one the relay layer measures."""
    tokens, phrases, allowed = _load_corpus()
    needles = tokens + phrases

    from toolsets import _HERMES_CORE_TOOLS
    from tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    entries = {e.name: e for e in registry._snapshot_entries()}
    missing = [n for n in _HERMES_CORE_TOOLS if n not in entries]
    assert not missing, f"core tools not registered: {missing}"

    failures = []
    for name in _HERMES_CORE_TOOLS:
        descs = []
        _description_strings(entries[name].schema, name, descs)
        for path, text in descs:
            for needle, lo, hi in _hits(text, needles, allowed):
                failures.append(f"{path}: {needle!r}: ...{text[lo:hi]!r}...")
    assert not failures, (
        f"{len(failures)} forbidden token(s) in the core tool surface:\n"
        + "\n".join(failures[:50])
    )


def test_exclusions_still_exist():
    """SPEC L11 anti-deletion: an exclusion whose tool vanished is drift."""
    from tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    names = {e.name for e in registry._snapshot_entries()}
    gone = sorted(set(EXCLUDED_TOOLS) - names)
    assert not gone, (
        f"excluded tools no longer registered: {gone} — remove the exclusion "
        "in the same commit that removes the tool (a stale exclusion is a "
        "silent hole in the gate)"
    )
