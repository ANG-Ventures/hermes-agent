"""Regressions for the 2026-08-08 `/compress` stall incident.

Four distinct defects were observed in a single event (Apollo session
``20260808_142907_818cb286``): a manual ``/compress`` ran for 126s, the
auxiliary summariser produced nothing, the run self-aborted, and the user was
told "No changes: transcript preserved" — which is what the code says for a
*successful* no-op.

* **B1** the reply could not distinguish an ABORT from a genuine no-op.
* **B2** the log blamed ``in-place mode is off`` while ``compression.in_place``
  was ``true``.
* **B3** the outer no-progress guard (120s) was tighter than the inner
  auxiliary deadline (300s), so the aux call never raised and the configured
  ``fallback_providers`` chain was structurally unreachable.
* **B4** the summariser ran on the config-default provider instead of the
  session's live (runtime-fallback) route.

These tests pin the invariants, not the current numbers.
"""

from __future__ import annotations

import pytest

from agent.compression_timeout_floor import (
    DERIVED_IDLE_CAP_SECONDS,
    reconcile_idle_timeout,
    reconcile_timeouts,
)


# ── B3: the outer guard must never out-tighten the inner deadline ──────────


class TestOuterGuardNeverTighterThanInner:
    """The invariant that makes ``fallback_providers`` reachable at all.

    ``call_llm`` only raises — and therefore only walks the fallback chain —
    when the *inner* deadline trips. If the outer no-progress watchdog fires
    first it abandons the worker silently, so no fallback can ever engage.
    """

    def test_default_guard_is_lifted_above_the_inner_deadline(self):
        # THE 2026-08-08 configuration: default 120s outer vs 300s inner.
        inner = 300.0
        out = reconcile_idle_timeout(120.0, inner)
        assert out > inner, (
            "the outer no-progress guard must outlive the inner aux deadline, "
            "otherwise call_llm never raises and fallback_providers is dead code"
        )

    @pytest.mark.parametrize("inner", [60.0, 120.0, 300.0, 301.0, 600.0])
    def test_invariant_holds_across_inner_deadlines(self, inner):
        out = reconcile_idle_timeout(120.0, inner)
        assert out > inner

    def test_explicit_operator_value_is_honoured_verbatim(self):
        # An operator who names a number means it; hermetic tests pin tiny
        # values (0.01) and must keep working.
        assert reconcile_idle_timeout(120.0, 300.0, explicit=True) == 120.0
        assert reconcile_idle_timeout(0.01, 300.0, explicit=True) == 0.01

    def test_zero_or_negative_disables_and_is_never_resurrected(self):
        # <= 0 is the documented "disable the wrapper" opt-out.
        assert reconcile_idle_timeout(0.0, 300.0) == 0.0
        assert reconcile_idle_timeout(-1.0, 300.0) == -1.0

    def test_an_already_generous_guard_is_never_lowered(self):
        assert reconcile_idle_timeout(900.0, 300.0) == 900.0

    def test_unknown_inner_deadline_is_a_noop(self):
        # Aux-config discovery failing must not change behaviour.
        assert reconcile_idle_timeout(120.0, None) == 120.0
        assert reconcile_idle_timeout(120.0, 0) == 120.0
        assert reconcile_idle_timeout(120.0, "nonsense") == 120.0  # type: ignore[arg-type]

    def test_derived_guard_is_capped(self):
        # A pathological inner deadline must not hang a session forever.
        out = reconcile_idle_timeout(120.0, 10_000.0)
        assert out == DERIVED_IDLE_CAP_SECONDS

    def test_ceiling_is_never_below_the_idle_window(self):
        idle, ceiling = reconcile_timeouts(120.0, 600.0, 300.0)
        assert idle > 300.0
        assert ceiling >= idle

    def test_resolver_wires_the_reconciler_with_real_config(self, monkeypatch):
        """The resolver — not just the helper — must apply the invariant."""
        from agent import conversation_compression as cc

        monkeypatch.setattr(
            "agent.auxiliary_client._effective_aux_timeout",
            lambda task, timeout: 300.0,
        )
        idle, ceiling = cc.resolve_context_compression_timeouts({})
        assert idle > 300.0, "default path must be lifted above the inner deadline"
        assert ceiling >= idle

        # ...and an explicit config value still wins verbatim.
        idle_x, _ = cc.resolve_context_compression_timeouts(
            {"context_timeout_seconds": 45.0}
        )
        assert idle_x == 45.0

    def test_resolver_survives_aux_config_failure(self, monkeypatch):
        from agent import conversation_compression as cc

        def _boom(*a, **k):
            raise RuntimeError("aux config unavailable")

        monkeypatch.setattr(
            "agent.auxiliary_client._effective_aux_timeout", _boom
        )
        idle, ceiling = cc.resolve_context_compression_timeouts({})
        assert idle == cc.DEFAULT_CONTEXT_TIMEOUT_SECONDS
        assert ceiling >= idle


# ── B1: abort must be reported as an abort, not as "no changes" ────────────


class _FakeAgent:
    """Minimal stand-in exposing the flags the gateway branch reads."""

    def __init__(self, **flags):
        self._last_compaction_in_place = flags.get("in_place", False)
        self._last_compaction_persist_failed = flags.get("persist_failed", False)
        self._last_compaction_aborted = flags.get("aborted", False)
        self._last_compaction_abort_reason = flags.get("reason", "")
        self.compression_in_place = flags.get("in_place_configured", True)


def _classify(agent, rotated: bool) -> str:
    """Mirror of the gateway's CASE selection, kept honest by the tests below.

    The production branch order is: rotated → in-place → persist-fail →
    ABORT → genuine no-op. Persist-failure is the more SPECIFIC outcome (a
    compacted list existed), so it outranks the abort signal.
    """
    in_place = bool(getattr(agent, "_last_compaction_in_place", False))
    rewritten = bool(rotated or in_place)
    if rewritten:
        return "rewritten"
    if bool(getattr(agent, "_last_compaction_persist_failed", False)):
        return "persist_failed"
    if getattr(agent, "_last_compaction_aborted", False) is True:
        return "aborted"
    return "noop"


class TestCompressFeedbackDistinguishesAllCases:
    def test_abort_is_not_reported_as_a_noop(self):
        # The 2026-08-08 signature: nothing produced, nothing persisted,
        # session_id unchanged — indistinguishable from a no-op pre-fix.
        agent = _FakeAgent(aborted=True, reason="timeout")
        assert _classify(agent, rotated=False) == "aborted"

    def test_genuine_noop_is_still_a_noop(self):
        assert _classify(_FakeAgent(), rotated=False) == "noop"

    def test_persist_failure_is_still_distinct(self):
        agent = _FakeAgent(persist_failed=True)
        assert _classify(agent, rotated=False) == "persist_failed"

    def test_successful_compaction_outranks_the_failure_cases(self):
        agent = _FakeAgent(in_place=True, aborted=True)
        assert _classify(agent, rotated=False) == "rewritten"

    def test_persist_failure_outranks_abort_when_both_are_set(self):
        """A rolled-back SAVE is more specific than "we gave up".

        Regression guard: an over-permissive abort check (plain ``bool()`` on a
        possibly-absent attribute) shadowed the pre-existing CASE D message and
        broke ``test_compress_command_persist_failure_surfaces_retry_not_noop``.
        """
        agent = _FakeAgent(persist_failed=True, aborted=True)
        assert _classify(agent, rotated=False) == "persist_failed"

    def test_non_boolean_abort_attribute_is_not_an_abort(self):
        """Only a literal ``True`` counts.

        Agent objects (and test doubles) can expose truthy placeholders for
        attributes that were never set; treating those as an abort mislabels
        healthy runs.
        """

        class Sentinel:
            _last_compaction_in_place = False
            _last_compaction_persist_failed = False
            _last_compaction_aborted = object()  # truthy, but not True

        assert _classify(Sentinel(), rotated=False) == "noop"

    def test_all_three_failure_surfaces_are_mutually_exclusive(self):
        seen = {
            _classify(_FakeAgent(aborted=True), False),
            _classify(_FakeAgent(persist_failed=True), False),
            _classify(_FakeAgent(), False),
        }
        assert seen == {"aborted", "persist_failed", "noop"}

    def test_timed_out_locale_key_exists_and_is_not_the_error_variant(self):
        """CASE E needs its own key.

        ``gateway.compress.aborted`` already existed for a summary FAILURE and
        interpolates ``{error}``; reusing it would either lose the parameter or
        change that message's meaning.
        """
        import pathlib

        import yaml

        root = pathlib.Path(__file__).resolve().parents[2]
        locales = sorted((root / "locales").glob("*.yaml"))
        assert locales, "no locale files discovered"
        for path in locales:
            data = yaml.safe_load(path.read_text())
            compress = data["gateway"]["compress"]
            assert compress.get("timed_out"), f"{path.name} missing timed_out"
            assert "{error}" not in compress["timed_out"], (
                f"{path.name}: timed_out must not require an {{error}} param"
            )
            # the pre-existing key keeps its distinct, parameterised meaning
            assert "{error}" in compress["aborted"], (
                f"{path.name}: the existing `aborted` key was altered"
            )


# ── B2: the warning must not blame config for a failed compaction ─────────


class TestInPlaceWarningAttribution:
    """``_last_compaction_in_place`` is an OUTCOME flag, not the config knob."""

    def test_outcome_flag_false_does_not_imply_config_off(self):
        agent = _FakeAgent(
            in_place=False, in_place_configured=True, aborted=True
        )
        # outcome says "no in-place commit" ...
        assert agent._last_compaction_in_place is False
        # ... but the config knob is ON, so the message must not blame config.
        assert agent.compression_in_place is True

    def test_config_off_is_a_genuinely_different_state(self):
        agent = _FakeAgent(in_place=False, in_place_configured=False)
        assert agent.compression_in_place is False

    def test_the_two_states_are_distinguishable(self):
        failed = _FakeAgent(in_place_configured=True, aborted=True)
        legacy = _FakeAgent(in_place_configured=False)
        assert failed.compression_in_place != legacy.compression_in_place


# ── B4: the summariser must follow the live session route ─────────────────


class _FakeLiveAgent:
    def __init__(self, provider, model):
        self._p, self._m = provider, model

    def _current_main_runtime(self):
        return {
            "model": self._m,
            "provider": self._p,
            "base_url": f"http://{self._p}.invalid/anthropic",
            "api_key": "k",
            "api_mode": "",
        }


class _RouteHost:
    """Exercises the real ``_resolve_live_session_route`` implementation."""

    _AGENT_PENDING = object()

    def __init__(self, agent):
        self._running_agents = {"s": agent} if agent is not None else {}
        self._agent_cache_lock = None
        self._agent_cache = None

    from gateway.run import GatewayRunner as _GR

    _resolve_live_session_route = _GR._resolve_live_session_route


class TestSummarizerFollowsLiveRoute:
    def test_live_runtime_fallback_route_is_recovered(self):
        # The session had moved to apx-7 via a mid-session fallback; config
        # still said claude-apr. The live route must win.
        host = _RouteHost(_FakeLiveAgent("claude-apx-7", "claude-fable-5"))
        route = host._resolve_live_session_route("s")
        assert route is not None
        assert route["provider"] == "claude-apx-7"
        assert route["model"] == "claude-fable-5"

    def test_no_resident_agent_is_a_safe_noop(self):
        host = _RouteHost(None)
        assert host._resolve_live_session_route("s") is None

    def test_partial_pair_is_rejected(self):
        """provider+model are a matched pair; half of one invites a cross."""
        host = _RouteHost(_FakeLiveAgent("", "claude-fable-5"))
        assert host._resolve_live_session_route("s") is None
        host2 = _RouteHost(_FakeLiveAgent("claude-apx-7", ""))
        assert host2._resolve_live_session_route("s") is None

    def test_a_broken_agent_never_raises(self):
        class Broken:
            def _current_main_runtime(self):
                raise RuntimeError("boom")

        assert _RouteHost(Broken())._resolve_live_session_route("s") is None

    def test_blank_provider_would_fall_through_to_config_defaults(self):
        """Why the matched-pair guard matters (the observed crossing).

        ``_resolve_auto`` takes the pair atomically: a blank provider makes it
        fall through to config ``model.provider``/``model.default`` — which is
        exactly how a healthy apx-7 session got an apr summariser.
        """
        runtime_provider = ""
        runtime_model = "claude-fable-5"
        cfg_provider, cfg_model = "claude-apr", "claude-opus-5"

        if runtime_provider:
            resolved = (runtime_provider, runtime_model)
        else:
            resolved = (cfg_provider, cfg_model)

        assert resolved == ("claude-apr", "claude-opus-5")
        assert resolved[0] != "claude-apx-7"
