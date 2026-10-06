"""Regressions from parity merge 2d966948b07 (t_91575e79), pinned on the runtime side.

1. A registered provider-PROFILE alias (``claude-api-proxy-f3`` -> ``claude-apx-3``) is an
   explicit inline provider: ``alias/model`` and ``alias:model`` switch to the profile. A lossy
   vendor shorthand (``openai`` -> openrouter) is not a profile alias and still never steals
   ``openai/gpt-5.5``.
2. ``list_authenticated_providers`` binds ONE ``provider_seam.snapshot()`` in its own frame and
   threads it to the sections, so a hot registration cannot tear one listing across generations.
"""

import types

import pytest

from hermes_cli import model_switch, provider_seam
from hermes_cli import model_switch_providers as msp


class _Prof:
    def __init__(self, name):
        self.name = name


def _fake_profiles(monkeypatch, table):
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda n: table.get(n))


def test_profile_alias_matches_its_canonical_profile(monkeypatch):
    _fake_profiles(monkeypatch, {"claude-api-proxy-f3": _Prof("claude-apx-3")})
    assert model_switch._inline_provider_matches_exact_id("claude-api-proxy-f3", "claude-apx-3")
    assert model_switch._inline_provider_matches_exact_id("Claude-API-Proxy-F3", "claude-apx-3")
    assert not model_switch._inline_provider_matches_exact_id("claude-api-proxy-f3", "claude-apx-4")


def test_lossy_vendor_alias_is_not_a_profile_alias(monkeypatch):
    _fake_profiles(monkeypatch, {})
    assert not model_switch._inline_provider_matches_exact_id("openai", "openrouter")
    assert model_switch._inline_provider_matches_exact_id("openrouter", "openrouter")


@pytest.mark.parametrize("raw", ["claude-api-proxy-f3/claude-opus-5", "claude-api-proxy-f3:claude-opus-5"])
def test_inline_parse_accepts_a_profile_alias(monkeypatch, raw):
    _fake_profiles(monkeypatch, {"claude-api-proxy-f3": _Prof("claude-apx-3")})
    pdef = types.SimpleNamespace(id="claude-apx-3")
    monkeypatch.setattr(model_switch, "resolve_provider_full", lambda name, *a, **k: pdef)
    assert model_switch._parse_inline_provider_model(raw, "anthropic") == ("claude-apx-3", "claude-opus-5")


class _Stop(BaseException):
    pass


def test_picker_binds_exactly_one_snapshot_in_its_own_frame(monkeypatch):
    import sys
    binds = []
    orig = provider_seam.snapshot

    def counting_snapshot(*a, **k):
        binds.append(sys._getframe(1).f_code.co_name)
        return orig(*a, **k)

    def stop(*a, **k):
        raise _Stop

    monkeypatch.setattr(provider_seam, "snapshot", counting_snapshot)
    monkeypatch.setattr(provider_seam, "_refresh_callbacks", [])
    import agent.models_dev as md
    monkeypatch.setattr(md, "fetch_models_dev", stop)  # first work after the bind
    with pytest.raises(_Stop):
        msp.list_authenticated_providers()
    assert binds.count("list_authenticated_providers") == 1, binds


def test_sections_read_the_bound_generation_not_module_globals():
    cp = types.SimpleNamespace(slug="gen-only", label="Gen Only")
    g = types.SimpleNamespace(CANONICAL_PROVIDERS=(cp,), PROVIDER_REGISTRY={}, HERMES_OVERLAYS={},
                              _PROVIDER_MODELS={"gen-only": ["m1"]})
    assert msp._registry(g, "CANONICAL_PROVIDERS") == (cp,)
    assert msp._build_curated_lists("", "", "", non_blocking=True, g=g)["gen-only"] == ["m1"]
    from hermes_cli.auth import PROVIDER_REGISTRY
    assert msp._registry(None, "PROVIDER_REGISTRY") is PROVIDER_REGISTRY
