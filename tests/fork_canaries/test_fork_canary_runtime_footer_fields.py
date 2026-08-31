"""Fork canary: gateway runtime footer fields (provider_model / context_full /
reasoning) and the byte-stability doctrine around them.

Surface: ``gateway/runtime_footer.py`` — the one-line metadata footer the
gateway appends to final replies on messaging surfaces (Discord/Telegram/etc.),
toggled by ``/footer on|off``.

Fork status: PR #80661 is still **OUTSTANDING** upstream (per the 2026-08-29
absorption census) — ``provider_model``, ``context_full``, and ``reasoning`` are
absent from upstream's ``gateway/runtime_footer.py``. The sibling ``latency``
field from #71990 WAS absorbed (via #77611), which means this exact file is a
live merge seam: upstream owns part of it and the fork owns the rest. A parity
merge that resolves ``runtime_footer.py`` toward upstream silently drops all
three fork fields and the footer quietly renders shorter — no exception, no
test failure, just less information.

Two contracts are locked here:

* the three fork fields render their documented shapes; and
* the **skip-silently** rule — a field whose data is missing must contribute
  nothing rather than emit ``?%`` / an empty slot. That rule is what lets the
  footer stay byte-stable across turns, which is why upstream accepted the
  ``latency`` field on the same design.
"""

import pytest

from gateway.runtime_footer import (
    _DEFAULT_FIELDS,
    _split_provider_model,
    format_runtime_footer,
)


# --------------------------------------------------------------------------- #
# The fork fields exist and are the defaults
# --------------------------------------------------------------------------- #

def test_fork_fields_are_the_default_footer_set():
    """RED-PROVABLE: in gateway/runtime_footer.py (~L51) change
    ``_DEFAULT_FIELDS`` to upstream's shape (e.g. ``("model", "context_pct",
    "cwd")``) — the fork fields vanish from every default-config footer."""
    assert {"provider_model", "context_full", "reasoning"} <= set(_DEFAULT_FIELDS), (
        f"fork footer fields dropped out of _DEFAULT_FIELDS; got {_DEFAULT_FIELDS!r}. "
        f"PR #80661 is still outstanding upstream, so a merge resolved toward "
        f"upstream silently shortens the footer."
    )


# --------------------------------------------------------------------------- #
# provider_model
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "provider,model,expected",
    [
        # Plain pair.
        ("claude-apr", "claude-opus-4-8", "claude-apr/claude-opus-4-8"),
        # Model already carries its own prefix — that prefix is AUTHORITATIVE
        # and the separately-supplied provider is ignored (no "unset/a/b").
        ("claude-apr", "claude-bridge-f3/claude-opus-4-8",
         "claude-bridge-f3/claude-opus-4-8"),
        (None, "claude-bridge-f3/claude-opus-4-8", "claude-bridge-f3/claude-opus-4-8"),
        # Provider unknown, bare model → model alone, never "unset/model".
        (None, "claude-opus-4-8", "claude-opus-4-8"),
        ("", "claude-opus-4-8", "claude-opus-4-8"),
    ],
)
def test_provider_model_renders_a_clean_pair(provider, model, expected):
    """RED-PROVABLE: in ``_split_provider_model``
    (gateway/runtime_footer.py ~L94) delete the "the model carries its own
    provider prefix — it's authoritative" branch — the prefixed-model cases
    start rendering a doubled ``provider/provider/model``."""
    footer = format_runtime_footer(
        model=model,
        provider=provider,
        context_tokens=0,
        context_length=None,
        fields=("provider_model",),
    )
    assert footer.strip() == expected, (
        f"provider_model rendered {footer.strip()!r}, expected {expected!r}"
    )


def test_split_provider_model_never_emits_an_unset_placeholder():
    """RED-PROVABLE: change ``_split_provider_model``
    (gateway/runtime_footer.py ~L91) to return ``(prov or "unset", mdl)``."""
    prov, mdl = _split_provider_model(None, "claude-opus-4-8")
    assert prov in ("", None), f"provider placeholder leaked into the footer: {prov!r}"
    assert mdl == "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# context_full
# --------------------------------------------------------------------------- #

def test_context_full_humanizes_both_sides_with_a_percentage():
    """``context_full`` is the fork's richer replacement for upstream's bare
    ``context_pct``: ``used/window (pct)``, both humanized.

    RED-PROVABLE: in gateway/runtime_footer.py (~L196) delete the
    ``elif field == "context_full":`` branch — the field silently renders
    nothing."""
    footer = format_runtime_footer(
        model="m",
        provider="p",
        context_tokens=50_200,
        context_length=1_000_000,
        fields=("context_full",),
    ).strip()
    assert "/" in footer and "%" in footer, (
        f"context_full lost its used/window (pct) shape: {footer!r}"
    )
    assert "50" in footer and ("1M" in footer or "1000" in footer), (
        f"context_full stopped humanizing its operands: {footer!r}"
    )
    assert "5%" in footer, f"context_full percentage wrong: {footer!r}"


def test_context_percentage_is_clamped_to_0_100():
    """A context overshoot (tokens > window, which happens right before
    compaction) must not render 137%.

    RED-PROVABLE: in gateway/runtime_footer.py (~L198) drop the
    ``max(0, min(100, ...))`` clamp."""
    footer = format_runtime_footer(
        model="m", provider="p",
        context_tokens=1_370_000, context_length=1_000_000,
        fields=("context_full",),
    )
    assert "100%" in footer, f"context percentage not clamped: {footer!r}"


# --------------------------------------------------------------------------- #
# reasoning
# --------------------------------------------------------------------------- #

def test_reasoning_renders_with_its_r_prefix():
    """RED-PROVABLE: in gateway/runtime_footer.py (~L205) delete the
    ``elif field == "reasoning":`` branch, or drop the ``r:`` prefix from
    ``parts.append(f"r:{r}")``."""
    footer = format_runtime_footer(
        model="m", provider="p", context_tokens=0, context_length=None,
        reasoning="high", fields=("reasoning",),
    ).strip()
    assert footer == "r:high", f"reasoning field rendered {footer!r}"


# --------------------------------------------------------------------------- #
# The skip-silently / byte-stability doctrine
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"context_tokens": 0, "context_length": None}, "context_full"),
        ({"context_tokens": 0, "context_length": 0}, "context_full"),
        ({"context_tokens": 0, "context_length": None, "reasoning": None}, "reasoning"),
        ({"context_tokens": 0, "context_length": None, "reasoning": "  "}, "reasoning"),
    ],
)
def test_missing_data_renders_nothing_not_a_placeholder(kwargs, field):
    """A partially-populated footer beats a line with ``?%`` or empty slots —
    this is the rule that keeps the footer byte-stable and is exactly the
    design upstream endorsed when absorbing the sibling ``latency`` field.

    RED-PROVABLE: in gateway/runtime_footer.py, make the ``context_full``
    branch (~L196) append ``"?"`` unconditionally instead of guarding on
    ``context_length and context_length > 0``."""
    footer = format_runtime_footer(
        model=None, provider=None, fields=(field,), **kwargs
    )
    assert footer.strip() == "", (
        f"{field} emitted a placeholder {footer!r} instead of skipping silently"
    )


def test_field_order_follows_the_caller_supplied_sequence():
    """``fields`` is an ordered config list — the footer must honour it so a
    user's ``footer.fields`` ordering is respected.

    RED-PROVABLE: in ``format_runtime_footer`` (gateway/runtime_footer.py
    ~L181) iterate ``sorted(fields)`` instead of ``fields``."""
    forward = format_runtime_footer(
        model="claude-opus-4-8", provider="claude-apr",
        context_tokens=100, context_length=1000, reasoning="low",
        fields=("provider_model", "reasoning"),
    )
    reverse = format_runtime_footer(
        model="claude-opus-4-8", provider="claude-apr",
        context_tokens=100, context_length=1000, reasoning="low",
        fields=("reasoning", "provider_model"),
    )
    assert forward != reverse, "footer ignored the caller's field ordering"
    assert forward.index("claude-apr") < forward.index("r:low")
    assert reverse.index("r:low") < reverse.index("claude-apr")


def test_unknown_field_names_are_ignored_without_raising():
    """Forward/backward config compatibility: a config naming a field this
    build doesn't have must not crash the reply path.

    RED-PROVABLE: add an ``else: raise ValueError(field)`` to the field
    dispatch chain in ``format_runtime_footer``."""
    footer = format_runtime_footer(
        model="m", provider="p", context_tokens=0, context_length=None,
        fields=("definitely_not_a_field", "reasoning"), reasoning="low",
    )
    assert "r:low" in footer
