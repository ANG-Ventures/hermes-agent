"""Reasoning effort reaches every provider through a capability flag, never a provider-name allowlist.

The chat-completions transport used to emit top-level ``reasoning_effort`` only for routes it
recognised by name (Kimi, TokenHub, LM Studio); any other OpenAI-compatible provider — a proxy
fronting several vendors, chief among them — silently sent nothing and ran at the upstream default
(``cpa/gpt-6-astra --reasoning high`` → ``{effort: medium}``, t_a8f9a94d). Now a profile declares
``supports_reasoning_effort`` + ``supported_reasoning_efforts(model)`` and the transport fills the
field for it; a level outside the declared set is clamped to the nearest weaker one WITH a notice.
"""

from __future__ import annotations

import logging

import pytest

from agent.reasoning_effort import (
    CODEX_GPT56_EFFORTS, EffortRoute, _CLAMP_NOTICED, kimi_effort_route, profile_route_for,
    resolve_route_effort, resolve_wire_effort, tokenhub_effort_route,
)
from agent.transports import get_transport
import agent.transports.chat_completions  # noqa: F401
from providers.base import ProviderProfile

MSGS = [{"role": "user", "content": "hi"}]


def _cc():
    return get_transport("chat_completions")


class _Proxy(ProviderProfile):
    """A multi-vendor proxy: one flag, a per-model vocabulary."""

    def supported_reasoning_efforts(self, model):
        bare = (model or "").rsplit("/", 1)[-1]
        if bare.startswith("gpt-6"):
            return CODEX_GPT56_EFFORTS
        if bare.startswith("three-level"):
            return ("low", "medium", "high")
        if bare.startswith("no-knob"):
            return ()
        return None


def _proxy(name="any-proxy", **kw):
    return _Proxy(name=name, base_url="http://127.0.0.1:9/v1", supports_reasoning_effort=True, **kw)


def _kw(profile, model, reasoning_config, **params):
    return _cc().build_kwargs(model=model, messages=MSGS, provider_profile=profile, reasoning_config=reasoning_config, **params)


@pytest.fixture(autouse=True)
def _fresh_notices():
    _CLAMP_NOTICED.clear()
    yield
    _CLAMP_NOTICED.clear()


class TestCapabilityFlag:
    def test_regression_proxy_gpt_high_reaches_the_wire(self):
        """The measured defect: ``--reasoning high`` on a proxied Codex model carried no effort."""
        kw = _kw(_proxy(), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert kw["reasoning_effort"] == "high"

    @pytest.mark.parametrize("name", ["any-proxy", "zzz", "cpa", "not-kimi"])
    def test_field_follows_the_flag_not_the_provider_name(self, name):
        kw = _kw(_proxy(name=name), "three-level-model", {"enabled": True, "effort": "medium"})
        assert kw["reasoning_effort"] == "medium"

    def test_profile_without_the_flag_gets_nothing(self):
        plain = ProviderProfile(name="plain", base_url="http://127.0.0.1:9/v1")
        kw = _kw(plain, "three-level-model", {"enabled": True, "effort": "high"})
        assert "reasoning_effort" not in kw

    def test_declared_empty_vocabulary_omits_the_field(self):
        kw = _kw(_proxy(), "no-knob-model", {"enabled": True, "effort": "high"})
        assert "reasoning_effort" not in kw

    def test_undeclared_model_gets_the_openai_compat_vocabulary(self):
        kw = _kw(_proxy(), "mystery-model", {"enabled": True, "effort": "xhigh"})
        assert kw["reasoning_effort"] == "xhigh"

    def test_unset_effort_stays_unset(self):
        assert "reasoning_effort" not in _kw(_proxy(), "gpt-6-astra", None)

    def test_disabled_without_none_level_omits(self):
        assert "reasoning_effort" not in _kw(_proxy(), "three-level-model", {"enabled": False})

    def test_disabled_with_none_level_sends_none(self):
        assert _kw(_proxy(), "gpt-6-astra", {"enabled": False})["reasoning_effort"] == "none"

    def test_profile_hooks_that_already_emit_a_control_win(self):
        class Own(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {"reasoning": {"effort": "custom-tier"}}, {}

        kw = _kw(Own(name="own", supports_reasoning_effort=True), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert "reasoning_effort" not in kw
        assert kw["extra_body"]["reasoning"] == {"effort": "custom-tier"}

    def test_caller_schema_named_reasoning_is_data_not_a_control(self):
        """Prism r1 P1: a ``reasoning`` *property name* inside a caller-supplied schema (guided
        decoding, structured output) must not suppress the user's explicit effort."""
        kw = _kw(
            _proxy(), "gpt-6-astra", {"enabled": True, "effort": "high"},
            extra_body_additions={"guided_json": {"properties": {"reasoning": {"type": "string"}}}},
        )
        assert kw["reasoning_effort"] == "high"
        assert kw["extra_body"]["guided_json"]["properties"]["reasoning"] == {"type": "string"}

    def test_hook_override_that_emits_no_control_still_gets_the_field(self):
        """A profile overriding build_api_kwargs_extras only for unrelated options has not
        handled reasoning; the flag still fills the field (main and aux paths alike)."""
        class Headers(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"extra_headers": {"X-Thing": "1"}}

        kw = _kw(Headers(name="hdr", supports_reasoning_effort=True), "three-level-model", {"enabled": True, "effort": "xhigh"})
        assert kw["reasoning_effort"] == "high" and kw["extra_headers"] == {"X-Thing": "1"}

    def test_profile_schema_named_reasoning_is_data_not_a_control(self):
        """Prism r2 P1: a PROFILE hook that emits a structured-output schema with a property
        named ``reasoning`` (top-level ``response_format`` or ``extra_body.guided_json``) has not
        put a reasoning control on the wire; the user's explicit effort is still emitted."""
        schema = {"type": "json_schema", "json_schema": {"schema": {"properties": {"reasoning": {"type": "string"}, "verbosity": {"type": "integer"}}}}}

        class TopLevelSchema(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"response_format": schema}

        class GuidedSchema(_Proxy):
            def build_extra_body(self, **context):
                return {"guided_json": {"properties": {"reasoning": {"type": "string"}}}}

        kw = _kw(TopLevelSchema(name="rf", supports_reasoning_effort=True), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert kw["reasoning_effort"] == "high" and kw["response_format"] == schema
        kw = _kw(GuidedSchema(name="gj", supports_reasoning_effort=True), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert kw["reasoning_effort"] == "high"
        assert kw["extra_body"]["guided_json"]["properties"]["reasoning"] == {"type": "string"}

    def test_profile_control_under_extra_body_wins(self):
        """The one nesting that IS a control location: ``extra_body.thinking`` from a kwargs hook."""
        class Thinking(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"extra_body": {"thinking": {"type": "enabled"}}}

        kw = _kw(Thinking(name="th", supports_reasoning_effort=True), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert "reasoning_effort" not in kw

    def test_profile_own_top_level_reasoning_effort_wins(self):
        class Own(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"reasoning_effort": "custom-tier"}

        kw = _kw(Own(name="own-top", supports_reasoning_effort=True), "gpt-6-astra", {"enabled": True, "effort": "high"})
        assert kw["reasoning_effort"] == "custom-tier"

    def test_request_override_beats_the_generic_field(self):
        kw = _kw(_proxy(), "gpt-6-astra", {"enabled": True, "effort": "high"}, request_overrides={"reasoning_effort": "low"})
        assert kw["reasoning_effort"] == "low"

    def test_profile_overrides_hook_is_consulted(self):
        class K(_Proxy):
            def supported_reasoning_efforts(self, model):
                return ("low", "high", "max")

            def reasoning_effort_overrides(self, model):
                return {"medium": "high"}

        kw = _kw(K(name="k", supports_reasoning_effort=True), "m", {"enabled": True, "effort": "medium"})
        assert kw["reasoning_effort"] == "high"


class TestClampWithNotice:
    def test_clamped_level_is_announced_once_per_route_and_level(self, caplog):
        with caplog.at_level(logging.WARNING, logger="agent.reasoning_effort"):
            for _ in range(3):
                kw = _kw(_proxy(), "three-level-model", {"enabled": True, "effort": "xhigh"})
        assert kw["reasoning_effort"] == "high"
        notices = [r for r in caplog.records if "sent as" in r.getMessage()]
        assert len(notices) == 1
        msg = notices[0].getMessage()
        assert "any-proxy/three-level-model" in msg and "'xhigh'" in msg and "'high'" in msg and "low/medium/high" in msg

    def test_supported_level_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="agent.reasoning_effort"):
            _kw(_proxy(), "three-level-model", {"enabled": True, "effort": "high"})
        assert not [r for r in caplog.records if "sent as" in r.getMessage()]

    def test_route_default_is_never_announced(self, caplog):
        with caplog.at_level(logging.WARNING, logger="agent.reasoning_effort"):
            sent = resolve_wire_effort(None, ("low", "high"), default="medium", route="r")
        assert sent == "low"
        assert not caplog.records

    def test_ladder_is_monotonic_on_every_declared_route(self):
        ladder = ("minimal", "low", "medium", "high", "xhigh", "max")
        for route in (
            kimi_effort_route("kimi-k3"), kimi_effort_route("kimi-k2.6"), tokenhub_effort_route(),
            profile_route_for(_proxy(), "three-level-model"), profile_route_for(_proxy(), "gpt-6-astra"),
        ):
            sent = [resolve_route_effort({"enabled": True, "effort": lvl}, route) for lvl in ladder]
            ranks = [ladder.index(s) for s in sent]
            assert ranks == sorted(ranks), (route.label, dict(zip(ladder, sent)))


class TestLegacyRouteIsDataNotName:
    """The unregistered-provider path takes an ``EffortRoute``; bare name flags are inert."""

    @pytest.mark.parametrize("flag", ["is_kimi", "is_tokenhub", "is_lmstudio"])
    def test_provider_name_flags_do_not_emit(self, flag):
        kw = _cc().build_kwargs(model="kimi-k3", messages=MSGS, reasoning_config={"enabled": True, "effort": "high"}, **{flag: True})
        assert "reasoning_effort" not in kw

    def test_route_object_emits_regardless_of_model_name(self):
        route = EffortRoute(("low", "medium", "high"), label="whatever")
        kw = _cc().build_kwargs(model="anything", messages=MSGS, reasoning_config={"enabled": True, "effort": "max"}, effort_route=route)
        assert kw["reasoning_effort"] == "high"

    def test_kimi_route_keeps_its_thinking_toggle(self):
        kw = _cc().build_kwargs(model="kimi-k3", messages=MSGS, reasoning_config={"enabled": False}, effort_route=kimi_effort_route("kimi-k3"))
        assert kw["extra_body"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in kw

    def test_legacy_route_resolution_is_by_host(self):
        from types import SimpleNamespace

        from agent.chat_completion_helpers import _legacy_effort_route

        kimi = _legacy_effort_route(SimpleNamespace(base_url="https://api.moonshot.ai/v1", _base_url_lower="https://api.moonshot.ai/v1", model="kimi-k3"))
        assert kimi is not None and kimi.thinking_toggle and kimi.default == "high"
        hub = _legacy_effort_route(SimpleNamespace(base_url="https://tokenhub.tencentmaas.com/v1", _base_url_lower="https://tokenhub.tencentmaas.com/v1", model="hunyuan"))
        assert hub is not None and hub.default == "high"
        assert _legacy_effort_route(SimpleNamespace(base_url="https://example.invalid/v1", _base_url_lower="https://example.invalid/v1", model="m")) is None


class TestAuxiliaryPath:
    def test_aux_call_kwargs_carry_the_flagged_effort(self, monkeypatch):
        import providers
        from agent.auxiliary_client import _build_call_kwargs

        proxy = _proxy(name="aux-proxy")
        monkeypatch.setattr(providers, "get_provider_profile", lambda name: proxy if name == "aux-proxy" else None)
        kw = _build_call_kwargs("aux-proxy", "three-level-model", MSGS, reasoning_config={"enabled": True, "effort": "max"}, base_url="http://127.0.0.1:9/v1")
        assert kw["reasoning_effort"] == "high"
        assert "reasoning" not in (kw.get("extra_body") or {})

    def test_aux_hook_override_without_a_control_still_emits(self, monkeypatch):
        """Prism r1 P1: a flagged profile whose build_api_kwargs_extras override emits no reasoning
        control (headers only / empty for this model) must still get the effort on aux calls."""
        import providers
        from agent.auxiliary_client import _build_call_kwargs

        class Headers(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"extra_headers": {"X-Thing": "1"}}

        hdr = Headers(name="aux-hdr", base_url="http://127.0.0.1:9/v1", supports_reasoning_effort=True)
        monkeypatch.setattr(providers, "get_provider_profile", lambda name: hdr if name == "aux-hdr" else None)
        kw = _build_call_kwargs("aux-hdr", "three-level-model", MSGS, reasoning_config={"enabled": True, "effort": "xhigh"}, base_url="http://127.0.0.1:9/v1")
        assert kw["reasoning_effort"] == "high"
        assert "reasoning" not in (kw.get("extra_body") or {})
        # explicit disable is honoured the same way (no 'none' level on this route → omitted, no fallback)
        kw = _build_call_kwargs("aux-hdr", "three-level-model", MSGS, reasoning_config={"enabled": False}, base_url="http://127.0.0.1:9/v1")
        assert "reasoning_effort" not in kw and "reasoning" not in (kw.get("extra_body") or {})

    def test_aux_profile_schema_named_reasoning_is_data_not_a_control(self, monkeypatch):
        """Prism r2 P1, aux projection: a profile-emitted response_format schema whose property is
        named ``reasoning`` must not suppress the user's effort on auxiliary calls either."""
        import providers
        from agent.auxiliary_client import _build_call_kwargs

        schema = {"type": "json_schema", "json_schema": {"schema": {"properties": {"reasoning": {"type": "string"}}}}}

        class Schema(_Proxy):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                return {}, {"response_format": schema}

        prof = Schema(name="aux-schema", base_url="http://127.0.0.1:9/v1", supports_reasoning_effort=True)
        monkeypatch.setattr(providers, "get_provider_profile", lambda name: prof if name == "aux-schema" else None)
        kw = _build_call_kwargs("aux-schema", "gpt-6-astra", MSGS, reasoning_config={"enabled": True, "effort": "high"}, base_url="http://127.0.0.1:9/v1")
        assert kw["reasoning_effort"] == "high" and kw["response_format"] == schema


class TestNoProviderNameGate:
    """Contract over every registered profile: the reasoning wire controls a request carries are a
    function of the profile's declared capabilities and hooks, never of its name. (The AST form of
    this check is banned by the root rubric — tests never read source — so it is behavioural: the
    same profile under a different name must produce byte-identical reasoning controls.)"""

    def _controls(self, profile, model):
        from agent.reasoning_effort import REASONING_CONTROL_KEYS

        kw = _kw(profile, model, {"enabled": True, "effort": "high"}, supports_reasoning=True)
        flat = {k: v for k, v in kw.items() if k in REASONING_CONTROL_KEYS}
        flat.update({f"extra_body.{k}": v for k, v in (kw.get("extra_body") or {}).items() if k in REASONING_CONTROL_KEYS})
        return flat

    def test_renaming_a_profile_changes_no_reasoning_control(self):
        import copy

        from providers import _REGISTRY, _discover_providers

        _discover_providers()
        checked = 0
        for name, profile in list(_REGISTRY.items()):
            if profile.api_mode != "chat_completions" or name != profile.name:
                continue
            model = profile.fallback_models[0] if profile.fallback_models else "some-model"
            renamed = copy.copy(profile)
            renamed.name = f"renamed-{name}"
            assert self._controls(profile, model) == self._controls(renamed, model), name
            checked += 1
        assert checked >= 10

    def test_every_flagged_profile_emits_for_a_ladder_level(self):
        from providers import _REGISTRY, _discover_providers

        _discover_providers()
        for name, profile in list(_REGISTRY.items()):
            if profile.api_mode != "chat_completions" or not getattr(profile, "supports_reasoning_effort", False):
                continue
            model = profile.fallback_models[0] if profile.fallback_models else "some-model"
            if profile.supported_reasoning_efforts(model) == ():
                continue
            assert self._controls(profile, model), name
