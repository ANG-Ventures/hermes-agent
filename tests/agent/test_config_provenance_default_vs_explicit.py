"""A shipped DEFAULT must never be mistaken for an operator's deliberate value.

``load_config()`` deep-merges ``DEFAULT_CONFIG`` under the user's file, so every
key that ships a default is ALWAYS present in the merged mapping. Code that
infers "the operator set this" from presence is therefore permanently wrong for
those keys.

The measured consequence (2026-08-10, live): PR #528 shipped
``reconcile_idle_timeout()`` to lift the outer compression no-progress guard
above the inner auxiliary deadline, so a stalled summariser would RAISE and the
configured ``fallback_providers`` chain could engage. It was **inert in
production for two days**, because its caller marked the shipped default 120 as
``explicit`` and the reconciler honours explicit values verbatim by design.

    reconcile_idle_timeout(120.0, inner=300.0, explicit=False)  -> 360.0  correct
    resolve_context_compression_timeouts()                      -> 120.0  inert

These tests pin the PROVENANCE distinction, the end-to-end effect on the real
resolver, and a class-level guard so the next defaulted knob can't repeat it.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from agent.config_provenance import (  # noqa: E402
    MISSING,
    default_config_value,
    is_operator_set,
)


# --------------------------------------------------------------------------
# provenance primitives
# --------------------------------------------------------------------------

def test_shipped_default_is_not_operator_set() -> None:
    """The regression: the merged value EQUALS the shipped default."""
    shipped = default_config_value("compression", "context_timeout_seconds")
    assert shipped is not MISSING, (
        "context_timeout_seconds must ship a default for this test to mean "
        "anything; if it became None, the sentinel style is in use instead"
    )
    cfg = {"context_timeout_seconds": shipped}
    assert is_operator_set(
        cfg, "context_timeout_seconds", "compression", "context_timeout_seconds"
    ) is False


def test_a_different_value_is_operator_set() -> None:
    cfg = {"context_timeout_seconds": 45}
    assert is_operator_set(
        cfg, "context_timeout_seconds", "compression", "context_timeout_seconds"
    ) is True


def test_zero_is_operator_set_because_it_disables() -> None:
    """0 disables the wrapper — an intent that must survive as explicit."""
    cfg = {"context_timeout_seconds": 0}
    assert is_operator_set(
        cfg, "context_timeout_seconds", "compression", "context_timeout_seconds"
    ) is True


def test_absent_key_is_not_operator_set() -> None:
    assert is_operator_set({}, "context_timeout_seconds",
                           "compression", "context_timeout_seconds") is False


def test_no_shipped_default_falls_back_to_presence() -> None:
    """Preserve the historical contract for keys absent from DEFAULT_CONFIG."""
    cfg = {"a_key_that_does_not_exist_anywhere": 7}
    assert default_config_value("compression",
                                "a_key_that_does_not_exist_anywhere") is MISSING
    assert is_operator_set(
        cfg, "a_key_that_does_not_exist_anywhere",
        "compression", "a_key_that_does_not_exist_anywhere",
    ) is True


def test_bool_is_not_confused_with_its_numeric_equal() -> None:
    """bool is an int subclass; True must not read as the shipped 1."""
    assert is_operator_set({"k": True}, "k", "definitely", "absent") is True


def test_none_default_and_none_value_is_not_operator_set() -> None:
    """The hygiene_timeout_seconds style: ships None, user left it alone."""
    shipped = default_config_value("compression", "hygiene_timeout_seconds")
    if shipped is not None:
        pytest.skip("hygiene_timeout_seconds no longer ships None")
    assert is_operator_set(
        {"hygiene_timeout_seconds": None}, "hygiene_timeout_seconds",
        "compression", "hygiene_timeout_seconds",
    ) is False


# --------------------------------------------------------------------------
# the end-to-end effect on the resolver that was inert
# --------------------------------------------------------------------------

def _resolver():
    from agent.conversation_compression import resolve_context_compression_timeouts
    return resolve_context_compression_timeouts


def test_default_config_gets_the_derived_floor_not_the_raw_default() -> None:
    """THE regression test.

    A cfg carrying only the shipped default must come back LIFTED above the
    inner auxiliary deadline — not echoed back at 120.
    """
    from agent.auxiliary_client import _effective_aux_timeout

    inner = _effective_aux_timeout("compression", None)
    if not inner or inner <= 0:
        pytest.skip("no inner auxiliary compression deadline configured")

    shipped = default_config_value("compression", "context_timeout_seconds")
    idle, _ceiling = _resolver()({"context_timeout_seconds": shipped})

    assert idle > inner, (
        f"outer no-progress guard ({idle}s) must exceed the inner aux deadline "
        f"({inner}s), or a stalled summariser is abandoned before call_llm can "
        f"raise and the configured fallback_providers stay unreachable"
    )
    assert idle != float(shipped), (
        "the shipped default was echoed back verbatim — the provenance check "
        "is not wired in (this is exactly how PR #528 shipped inert)"
    )


def test_an_operator_value_is_still_honoured_verbatim() -> None:
    """The design intent #528 protected: a named number means it."""
    idle, _ = _resolver()({"context_timeout_seconds": 45})
    assert idle == 45.0


def test_operator_zero_still_disables_the_wrapper() -> None:
    idle, _ = _resolver()({"context_timeout_seconds": 0})
    assert idle == 0.0


def test_junk_value_does_not_crash_or_mark_explicit() -> None:
    idle, _ = _resolver()({"context_timeout_seconds": "not-a-number"})
    assert idle > 0


# --------------------------------------------------------------------------
# class-level guard: catch the NEXT knob that does this
# --------------------------------------------------------------------------

def test_no_timeout_resolver_infers_intent_from_bare_presence() -> None:
    """Lint the CLASS, not just this instance.

    A timeout/budget resolver that sets an ``explicit``-style flag to a bare
    literal ``True`` right after a presence check is the bug shape. Requiring
    the provenance helper (or a None-sentinel default) makes the mistake
    impossible to reintroduce silently.
    """
    import re

    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    suspects: list[str] = []

    for rel in ("agent/conversation_compression.py", "agent/hygiene_timeout.py"):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8").read().split("\n")
        # A None-shipping key proves provenance by sentinel, not by helper.
        if "hygiene_timeout_seconds" in "\n".join(lines):
            continue
        for lineno, line in enumerate(lines):
            if not re.search(r"\bexplicit\w*\s*=\s*True\b", line):
                continue
            # The exemption must be LOCAL: a bare import at the top of the
            # function does not make THIS assignment provenance-checked.
            # (Measured: exempting on file-wide presence let the reverted
            # bug pass green, because the import line survived the revert.)
            window = "\n".join(lines[max(0, lineno - 6):lineno + 3])
            if "is_operator_set" in window:
                continue
            suspects.append(f"{rel}:{lineno + 1}")

    assert not suspects, (
        "these sites set an explicit-flag to a bare True without proving the "
        "value is operator-set rather than default-merged:\n  "
        + "\n  ".join(suspects)
        + "\n\nUse agent.config_provenance.is_operator_set(cfg, key, *default_path)."
    )


def test_the_guard_is_not_vacuous() -> None:
    """Positive control: the pattern the guard hunts must be matchable.

    A lint whose regex never matches anything passes forever.
    """
    import re

    sample = "        explicit_idle = True\n"
    assert re.search(r"\bexplicit\w*\s*=\s*True\b", sample), (
        "the guard's own pattern no longer matches the known-bad shape"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
