"""Behavior contract for PER-CONTENT-CLASS token-estimate skew calibration.

Background
----------
The rough token estimator's error is a RATE error, and that rate is not uniform
across content. Measured against provider ground truth on 86 chunks replayed
from 22 real production sessions (``agent/content_class.py`` docstring; the
sweep is reproducible from ``~/.hermes/state.db``):

    class   n   median ratio   range
    text   44       1.007      0.933 - 1.117
    tool   42       1.132      1.032 - 1.320

A single global ratio blends those into a number that under-corrects tool-heavy
turns and over-corrects text-heavy ones. These tests assert the BEHAVIOR of the
fix, not the measured constants:

  1. a tool-heavy turn receives the TOOL-class ratio, not a blended one
  2. a class below the sample floor falls back to the GLOBAL calibration
  3. an UNDER-count is still correctable UPWARD (PR #506 / #529 regression band)
  4. WIRING: the classifier is actually consulted on the production estimate
     path, not merely importable
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import pytest

from agent.content_class import (
    CLASS_MEDIA,
    CLASS_TEXT,
    CLASS_TOOL,
    dominant_content_class,
)
from agent.context_engine import (
    _SKEW_SCALE_UP_MAX,
    ContextEngine,
    call_with_messages,
)


# --- Harness ---------------------------------------------------------------
#
# The calibration lives as concrete methods on the ABC, so the contract is
# exercised on a minimal REAL subclass — the production methods, unmodified,
# with only the abstract members stubbed. No reimplementation of the logic
# under test.


class _Engine(ContextEngine):
    def __init__(self, **overrides):
        self._recent_skews = []
        self._class_skews = {}
        self._last_rough_sent = 0
        self._last_rough_class = None
        self.rough_at_last_real = 0
        self._skew_floor = 0.7
        self._hard_frac = 0.95
        self.context_length = 200_000
        self.threshold_tokens = 150_000
        self.model = "test-model"
        self.provider = "test"
        self.compress_calls = []
        for k, v in overrides.items():
            setattr(self, k, v)

    # -- abstract members, stubbed --
    @property
    def name(self) -> str:
        return "test-engine"

    def update_from_response(self, usage):  # pragma: no cover - stub
        return None

    def should_compress(self, prompt_tokens=None):
        self.compress_calls.append(prompt_tokens)
        return bool(prompt_tokens is not None and prompt_tokens >= self.threshold_tokens)

    def compress(self, messages, *args, **kwargs):  # pragma: no cover - stub
        return messages

    # -- keep telemetry/persistence off the filesystem in tests --
    def _persist_skew_history(self):
        return None

    def _emit_skew_telemetry(self, *a, **kw):
        return None


def _engine(**overrides):
    return _Engine(**overrides)


def _note(engine, rough, messages=None):
    engine.note_rough_sent(rough, messages)


def _record(engine, real):
    engine.record_skew_from_real(real)


def _skew(engine, content_class=None):
    return engine._current_skew(content_class)


def _calibrated(engine, rough, messages=None):
    return engine.calibrated_tokens(rough, messages)


def _tool_heavy_messages(size: int = 4000):
    return [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * size},
    ]


def _text_heavy_messages(size: int = 4000):
    return [
        {"role": "user", "content": "y" * size},
        {"role": "assistant", "content": "z" * size},
    ]


def _train(engine, messages, ratio, times):
    """Feed ``times`` paired readings at ``ratio`` for ``messages``' class."""
    rough = 10_000
    for _ in range(times):
        _note(engine, rough, messages)
        _record(engine, int(rough * ratio))


# --- 1. Classifier --------------------------------------------------------


class TestClassifier:
    def test_tool_dominated_turn_classifies_as_tool(self):
        assert dominant_content_class(_tool_heavy_messages()) == CLASS_TOOL

    def test_text_dominated_turn_classifies_as_text(self):
        assert dominant_content_class(_text_heavy_messages()) == CLASS_TEXT

    def test_assistant_tool_calls_count_as_tool_not_text(self):
        """The most common agent message shape: a little prose, a big call."""
        msgs = [
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "f", "arguments": "a" * 4000},
                    }
                ],
            }
        ]
        assert dominant_content_class(msgs) == CLASS_TOOL

    def test_media_dominated_turn_classifies_as_media(self):
        msgs = [
            {"role": "user", "content": "look"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": "AAAA"},
                    }
                ],
            },
        ]
        assert dominant_content_class(msgs) == CLASS_MEDIA

    def test_media_weighed_at_pricing_cost_not_base64_length(self):
        """A tiny image must not out-vote a large real conversation.

        Weighing media by transport size is the bug agent/media_tokens.py
        exists to prevent; the classifier must not reintroduce it.
        """
        msgs = [
            {"role": "user", "content": "q" * 200_000},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": "B" * 400_000},
                    }
                ],
            },
        ]
        assert dominant_content_class(msgs) == CLASS_TEXT

    def test_mixed_turn_with_no_majority_returns_none(self):
        """No class holds a majority ⇒ the blended global ratio is correct."""
        msgs = [
            {"role": "user", "content": "y" * 4000},
            {"role": "tool", "tool_call_id": "c", "content": "x" * 4000},
        ]
        assert dominant_content_class(msgs) is None

    @pytest.mark.parametrize("bad", [None, [], [object()], "not-a-list"])
    def test_unusable_input_never_raises(self, bad):
        assert dominant_content_class(bad) in (None, CLASS_TEXT)


# --- 2. A tool-heavy turn receives the TOOL-class ratio -------------------


class TestPerClassRatioApplied:
    def test_tool_heavy_turn_gets_tool_ratio_not_blended(self):
        """The headline contract.

        Train BOTH classes to their own (very different) measured ratios, then
        read each class back: each must get ITS OWN ratio, and at least one of
        them must differ from the blended global median — which is precisely
        the error a single global ratio makes.
        """
        e = _engine()
        tool_msgs = _tool_heavy_messages()
        text_msgs = _text_heavy_messages()
        _train(e, text_msgs, 1.00, 3)
        _train(e, tool_msgs, 1.30, 3)

        tool_skew = _skew(e, CLASS_TOOL)
        text_skew = _skew(e, CLASS_TEXT)
        blended = _skew(e, None)

        assert tool_skew == pytest.approx(1.30, abs=0.01)
        assert text_skew == pytest.approx(1.00, abs=0.01)
        # The two classes are corrected differently — the point of the change.
        assert tool_skew != pytest.approx(text_skew, abs=0.01)
        # And the blend is demonstrably wrong for at least one of them: a
        # single global number cannot equal two different class ratios.
        assert (
            tool_skew != pytest.approx(blended, abs=0.01)
            or text_skew != pytest.approx(blended, abs=0.01)
        )

    def test_calibrated_tokens_applies_class_of_the_supplied_request(self):
        """Same rough count, different content ⇒ different calibrated count."""
        e = _engine()
        _train(e, _text_heavy_messages(), 1.00, 3)
        _train(e, _tool_heavy_messages(), 1.30, 3)

        rough = 100_000
        tool_cal = _calibrated(e, rough, _tool_heavy_messages())
        text_cal = _calibrated(e, rough, _text_heavy_messages())

        assert tool_cal > text_cal
        assert tool_cal == pytest.approx(130_000, rel=0.02)
        assert text_cal == pytest.approx(100_000, rel=0.02)

    def test_trigger_fires_for_tool_heavy_and_defers_for_text_heavy(self):
        """End-to-end decision difference at the same raw rough estimate."""
        seen = []
        e = _engine(should_compress=lambda t: (seen.append(t), t >= 150_000)[1])
        _train(e, _text_heavy_messages(), 1.00, 3)
        _train(e, _tool_heavy_messages(), 1.30, 3)

        decide = e.should_compress_calibrated
        rough = 120_000  # below threshold raw; above it only at the tool rate
        assert decide(rough, _tool_heavy_messages()) is True
        assert decide(rough, _text_heavy_messages()) is False


# --- 3. Fallback below the sample floor -----------------------------------


class TestSampleFloorFallback:
    def test_class_below_floor_falls_back_to_global(self, monkeypatch):
        """Two readings is below the default floor of 3 ⇒ global is used."""
        monkeypatch.setattr(
            "agent.context_engine._per_class_min_samples", lambda: 3
        )
        e = _engine()
        # Global history gets a distinctly different level than the class.
        _train(e, _text_heavy_messages(), 1.00, 4)
        _train(e, _tool_heavy_messages(), 1.30, 2)  # only 2 → below floor

        blended = _skew(e, None)
        assert _skew(e, CLASS_TOOL) == pytest.approx(blended)
        assert _skew(e, CLASS_TOOL) != pytest.approx(1.30, abs=0.01)

    def test_class_at_the_floor_engages(self, monkeypatch):
        monkeypatch.setattr(
            "agent.context_engine._per_class_min_samples", lambda: 3
        )
        e = _engine()
        _train(e, _text_heavy_messages(), 1.00, 4)
        _train(e, _tool_heavy_messages(), 1.30, 3)  # exactly at the floor
        assert _skew(e, CLASS_TOOL) == pytest.approx(1.30, abs=0.01)

    def test_floor_of_zero_disables_the_class_arm_entirely(self, monkeypatch):
        """Documented operator kill switch restores single-global-ratio."""
        monkeypatch.setattr(
            "agent.context_engine._per_class_min_samples", lambda: 0
        )
        e = _engine()
        _train(e, _text_heavy_messages(), 1.00, 4)
        _train(e, _tool_heavy_messages(), 1.30, 5)
        assert _skew(e, CLASS_TOOL) == pytest.approx(_skew(e, None))

    def test_unknown_class_uses_global(self):
        e = _engine()
        _train(e, _text_heavy_messages(), 1.00, 4)
        assert _skew(e, "no-such-class") == pytest.approx(_skew(e, None))

    def test_no_history_at_all_is_identity(self):
        assert _skew(_engine(), CLASS_TOOL) == 1.0

    def test_min_samples_knob_is_read_from_config_not_hardcoded(self, monkeypatch):
        """The tuning value is a config.yaml knob with an EXPLICIT reader."""
        import agent.context_engine as ce

        captured = {}

        def fake_load():
            captured["read"] = True
            return {"compression": {"skew_class_min_samples": 5}}

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", fake_load, raising=False
        )
        assert ce._per_class_min_samples() == 5
        assert captured.get("read"), "the knob must be READ, not assumed"

    def test_min_samples_knob_is_declared_in_config_defaults(self):
        """An undeclared knob is an inert knob (config set would warn)."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert "skew_class_min_samples" in DEFAULT_CONFIG["compression"]


# --- 4. Scale-up regression band (PR #506 / #529) -------------------------


class TestUndercountStillCorrectableUpward:
    """The per-class arm must not re-introduce the min(1.0, ...) clamp that
    PR #506 lifted from record_skew_from_real and _current_skew, nor the one
    PR #529 fixed in seed_skew_calibration."""

    def test_class_ratio_may_exceed_one(self):
        e = _engine()
        _train(e, _tool_heavy_messages(), 1.30, 3)
        assert _skew(e, CLASS_TOOL) > 1.0

    def test_class_ratio_is_bounded_by_scale_up_max(self):
        e = _engine()
        _train(e, _tool_heavy_messages(), 99.0, 3)
        assert _skew(e, CLASS_TOOL) <= _SKEW_SCALE_UP_MAX

    def test_global_ratio_may_still_exceed_one(self):
        """The pre-existing global behavior is untouched by the class arm."""
        e = _engine()
        _train(e, None, 1.30, 3)  # no messages ⇒ global-only recording
        assert _skew(e, None) > 1.0

    def test_undercount_correction_raises_the_calibrated_estimate(self):
        e = _engine()
        _train(e, _tool_heavy_messages(), 1.30, 3)
        rough = 100_000
        assert _calibrated(e, rough, _tool_heavy_messages()) > rough

    def test_class_ratio_respects_the_skew_floor(self):
        e = _engine(_skew_floor=0.7)
        _train(e, _tool_heavy_messages(), 0.2, 3)
        assert _skew(e, CLASS_TOOL) == pytest.approx(0.7)

    def test_seed_skew_calibration_band_unchanged(self):
        """#529's band must still accept ratios above 1.0."""
        e = _engine()
        e.seed_skew_calibration([1.4, 1.5])
        assert _skew(e, None) > 1.0


# --- 5. Recording hygiene -------------------------------------------------


class TestRecordingHygiene:
    def test_class_label_is_consumed_with_the_rough(self):
        """A stale label must not tag the NEXT turn's reading (T0 contract)."""
        e = _engine()
        _note(e, 10_000, _tool_heavy_messages())
        _record(e, 13_000)
        assert e._last_rough_class is None
        # An unpaired second reading records nothing anywhere.
        _record(e, 20_000)
        assert len(e._class_skews.get(CLASS_TOOL, [])) == 1

    def test_reset_clears_per_class_state(self):
        e = _engine()
        _train(e, _tool_heavy_messages(), 1.3, 3)
        assert e._class_skews
        e.reset_skew_calibration()
        assert e._class_skews == {}
        assert e._last_rough_class is None

    def test_class_history_is_bounded(self):
        e = _engine()
        _train(e, _tool_heavy_messages(), 1.2, 20)
        assert len(e._class_skews[CLASS_TOOL]) <= ContextEngine._SKEW_HISTORY

    def test_global_history_still_records_every_pair(self):
        """Per-class is ADDITIVE — the global arm must not be starved."""
        e = _engine()
        _train(e, _tool_heavy_messages(), 1.2, 3)
        assert e._recent_skews


# --- 6. WIRING: the classifier is consulted on the production path --------


class TestProductionPathWiring:
    """These are the assertions that fail if the feature is built but never
    reached — the classic 'wired in but dead' failure."""

    def test_note_rough_sent_accepts_and_uses_messages(self):
        e = _engine()
        _note(e, 5000, _tool_heavy_messages())
        assert e._last_rough_class == CLASS_TOOL

    def test_turn_context_preflight_passes_messages_to_the_calibration(self):
        """AST assertion over the real production preflight.

        agent/turn_context.py's preflight is the site that decides compaction
        for every CLI/gateway turn. If it calls the calibration without the
        message list, no request is ever classified and the whole per-class
        arm is dead code.
        """
        src = Path(inspect.getfile(_turn_context())).read_text()
        tree = ast.parse(src)
        calibration_names = {
            "note_rough_sent",
            "calibrated_tokens",
            "should_compress_calibrated",
            "_trigger_calibrated_tokens",
        }
        wired = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # call_with_messages(_compressor.<name>, rough, messages)
            if isinstance(fn, ast.Name) and fn.id.endswith("call_with_messages"):
                if node.args and isinstance(node.args[0], ast.Attribute):
                    wired.add(node.args[0].attr)
        missing = calibration_names - wired
        assert not missing, (
            "these calibration entry points are called WITHOUT the message "
            f"list, so their requests are never classified: {sorted(missing)}"
        )

    def test_conversation_loop_preflight_passes_messages(self):
        src = Path(inspect.getfile(_conversation_loop())).read_text()
        assert "_call_with_messages(" in src, (
            "conversation_loop's preflight compaction gate must route through "
            "call_with_messages so the request is classified"
        )

    def test_call_with_messages_degrades_for_old_signatures(self):
        """A plugin engine predating the parameter must keep working."""
        calls = []

        def legacy(rough):
            calls.append(rough)
            return rough * 2

        assert call_with_messages(legacy, 10, [{"role": "user", "content": "a"}]) == 20
        assert calls == [10]

    def test_call_with_messages_uses_the_new_signature_when_available(self):
        seen = {}

        def modern(rough, messages=None):
            seen["messages"] = messages
            return rough

        msgs = [{"role": "user", "content": "a"}]
        call_with_messages(modern, 10, msgs)
        assert seen["messages"] is msgs

    def test_call_with_messages_does_not_swallow_real_errors(self):
        def broken(rough, messages=None):
            raise ValueError("boom")

        with pytest.raises(ValueError):
            call_with_messages(broken, 10, [{"role": "user", "content": "a"}])


def _turn_context():
    import agent.turn_context as m

    return m


def _conversation_loop():
    import agent.conversation_loop as m

    return m
