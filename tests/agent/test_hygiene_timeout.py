"""Session-hygiene compression deadline + repeated-failure escalation.

RED-proof: with ``resolve_hygiene_timeout_seconds`` returning a flat 30.0 (the
pre-fix hardcode), every test in ``TestDeadlineNeverTighterThanInner`` fails.
"""

import pytest

from agent.hygiene_timeout import (
    DEFAULT_HYGIENE_FAILURE_ALERT_AFTER,
    DEFAULT_HYGIENE_TIMEOUT_SECONDS,
    MAX_DERIVED_HYGIENE_TIMEOUT_SECONDS,
    format_repeated_failure_alert,
    resolve_failure_alert_after,
    resolve_hygiene_timeout_seconds,
    should_alert_loudly,
)


class TestDeadlineNeverTighterThanInner:
    """The outer wall-clock guard must never be tighter than the inner LLM
    deadline it wraps — that is not a safety net, it is a guaranteed kill."""

    def test_derives_from_auxiliary_compression_timeout(self):
        # The live Apollo shape: aux timeout 300, no explicit hygiene knob.
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {"enabled": True},
            {"compression": {"provider": "auto", "model": "", "timeout": 300}},
        )
        assert seconds == 300.0
        assert explicit is False

    def test_default_when_nothing_configured(self):
        seconds, explicit = resolve_hygiene_timeout_seconds({}, {})
        assert seconds == DEFAULT_HYGIENE_TIMEOUT_SECONDS
        assert explicit is False

    def test_inner_below_default_does_not_lower_the_guard(self):
        # A tiny aux timeout must not make hygiene twitchier than it was.
        seconds, _ = resolve_hygiene_timeout_seconds(
            {}, {"compression": {"timeout": 5}}
        )
        assert seconds == DEFAULT_HYGIENE_TIMEOUT_SECONDS

    def test_derived_value_is_capped(self):
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {}, {"compression": {"timeout": 86400}}
        )
        assert seconds == MAX_DERIVED_HYGIENE_TIMEOUT_SECONDS
        assert explicit is False

    @pytest.mark.parametrize("junk", [None, "abc", [], {"timeout": "later"}, True])
    def test_junk_auxiliary_config_falls_back_to_default(self, junk):
        seconds, _ = resolve_hygiene_timeout_seconds({}, {"compression": junk})
        assert seconds == DEFAULT_HYGIENE_TIMEOUT_SECONDS

    @pytest.mark.parametrize("bad", [0, -1, "nope", None, float("nan")])
    def test_nonpositive_inner_timeout_ignored(self, bad):
        seconds, _ = resolve_hygiene_timeout_seconds({}, {"compression": {"timeout": bad}})
        assert seconds == DEFAULT_HYGIENE_TIMEOUT_SECONDS


class TestExplicitKnobStillWins:
    """An operator who sets the knob gets exactly what they asked for."""

    def test_explicit_beats_derivation(self):
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {"hygiene_timeout_seconds": 45},
            {"compression": {"timeout": 300}},
        )
        assert seconds == 45.0
        assert explicit is True

    def test_explicit_tiny_value_is_honoured_unclamped(self):
        # The existing hermetic gateway tests set 0.01 to force a timeout.
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {"hygiene_timeout_seconds": 0.01}, {"compression": {"timeout": 300}}
        )
        assert seconds == 0.01
        assert explicit is True

    def test_explicit_above_derived_cap_is_honoured(self):
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {"hygiene_timeout_seconds": 5000}, {"compression": {"timeout": 300}}
        )
        assert seconds == 5000.0
        assert explicit is True

    @pytest.mark.parametrize("bad", [0, -3, "soon", None])
    def test_invalid_explicit_falls_through_to_derivation(self, bad):
        seconds, explicit = resolve_hygiene_timeout_seconds(
            {"hygiene_timeout_seconds": bad}, {"compression": {"timeout": 300}}
        )
        assert seconds == 300.0
        assert explicit is False


class TestRepeatedFailureIsLoud:
    """A session that can never compress grows unboundedly — say so."""

    def test_quiet_below_the_threshold(self):
        assert should_alert_loudly(1, 3) is False
        assert should_alert_loudly(2, 3) is False

    def test_loud_at_and_above_the_threshold(self):
        assert should_alert_loudly(3, 3) is True
        assert should_alert_loudly(9, 3) is True

    def test_zero_disables_the_alert(self):
        assert should_alert_loudly(100, 0) is False

    def test_resolve_alert_after_default_and_overrides(self):
        assert resolve_failure_alert_after({}) == DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
        assert resolve_failure_alert_after({"hygiene_failure_alert_after": 5}) == 5
        assert resolve_failure_alert_after({"hygiene_failure_alert_after": 0}) == 0
        # junk -> default
        assert (
            resolve_failure_alert_after({"hygiene_failure_alert_after": "many"})
            == DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
        )
        assert (
            resolve_failure_alert_after({"hygiene_failure_alert_after": -2})
            == DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
        )

    def test_alert_names_the_growth_and_the_knob(self):
        msg = format_repeated_failure_alert(3, 300.0, 1044, 669556)
        assert "3 times" in msg
        assert "1,044 messages" in msg
        assert "669,556 tokens" in msg
        assert "compression.hygiene_timeout_seconds" in msg
        assert "300.0s" in msg
        # No-data-loss semantics must survive the louder wording.
        assert "No messages have been dropped" in msg
        # It must state the actual consequence, not just "it failed".
        assert "growing" in msg

    def test_alert_without_size_data(self):
        msg = format_repeated_failure_alert(4, 30.0)
        assert "4 times" in msg
        assert "(now " not in msg
