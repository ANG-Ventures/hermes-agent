"""``model.codex_context_policy`` — big Codex window by default (card t_73689428).

Ace 2026-09-30 11:17 PT: bare Codex slugs resolve to the live-verified large
window by default; ``-900k`` stays a legacy alias for the SAME window, and
``model.codex_context_policy: advertised`` flips one config (root or a single
profile) back to the old opt-in behaviour.

Every test here opts out of the suite-wide ``advertised`` pin
(tests/conftest.py ``_pin_codex_context_policy_advertised``) and drives the
knob through a REAL config.yaml read.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

from agent import model_metadata as mm

pytestmark = pytest.mark.real_codex_context_policy

_config_mod = importlib.import_module("hermes_cli.config")
_CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _use_config(monkeypatch, tmp_path, text):
    """Point the active config.yaml at a fresh file holding *text* (None = absent)."""
    path = tmp_path / "config.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(_config_mod, "get_config_path", lambda: path)
    _config_mod._RAW_CONFIG_CACHE.clear()
    _config_mod._LOAD_CONFIG_CACHE.clear()
    return path


@pytest.fixture
def large(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: large\n")


@pytest.fixture
def advertised(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: advertised\n")


def _codex_jwt(subject: str) -> str:
    """JWT-shaped test stand-in: the live-probe path gates on the token parsing as a JWT
    (a gateway key is not a ChatGPT credential and must stay off chatgpt.com, #121486);
    the signature itself is never verified client-side."""
    import base64 as _b64
    import json as _json

    def _enc(raw: bytes) -> str:
        return _b64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    header = _enc(b'{"alg":"RS256"}')
    payload = _enc(_json.dumps({"sub": subject}).encode())
    return f"{header}.{payload}.sig"


def _codex_ctx(model: str) -> int:
    """Resolve through get_model_context_length with the catalog unreachable
    (static fallback table = advertised 272K for every slug)."""
    fake = MagicMock()
    fake.status_code = 401
    fake.json.return_value = {}
    mm._codex_oauth_context_cache = {}
    with patch("agent.model_metadata.model_metadata_http.get", return_value=fake), \
         patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.save_context_length"):
        return mm.get_model_context_length(
            model=model, base_url=_CODEX_URL, api_key="expired-token",
            provider="openai-codex",
        )


# -- resolver under ``large`` ---------------------------------------------

@pytest.mark.parametrize(
    "model, expected",
    [
        ("openai-codex/gpt-6-sol", 872_000),
        ("gpt-6-sol", 872_000),
        ("gpt-6.1-sol", 900_000),
        ("gpt-6-sol-900k", 872_000),       # legacy alias -> SAME window
        ("gpt-6.1-sol-900k", 900_000),
        ("gpt-6-astra", 872_000),
        ("gpt-6-luna", 872_000),
        ("gpt-5.6-sol", 900_000),
        ("gpt-5.6-sol-2026-07-09", 900_000),
        ("gpt-5.4", 900_000),
        ("gpt-daybreak-blue-latest", 900_000),
    ],
)
def test_large_policy_eligible_slug_resolves_verified_window(large, model, expected):
    assert mm.codex_context_policy() == "large"
    assert mm._verified_codex_ctx_for_slug(model) == expected
    assert _codex_ctx(model.rsplit("/", 1)[-1]) == expected


def test_large_policy_bare_and_legacy_alias_are_identical(large):
    for base in sorted(mm._CODEX_900K_ELIGIBLE_BASES):
        assert mm._verified_codex_ctx_for_slug(base) is not None, base
        assert mm._verified_codex_ctx_for_slug(base) == mm._verified_codex_ctx_for_slug(
            base + "-900k"
        ), base


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4-mini", "gpt-6.1-sol-pro", "gpt-5.6-sol-pro"])
def test_large_policy_ineligible_slug_stays_advertised(large, model):
    assert mm._verified_codex_ctx_for_slug(model) is None
    assert _codex_ctx(model) == 272_000
    assert mm.codex_uses_large_window(model) is False


@pytest.mark.parametrize("alias", ["gpt-5.5-900k", "gpt-5.4-mini-900k", "gpt-6.1-sol-pro-900k"])
def test_large_policy_ineligible_900k_alias_is_not_a_variant(large, alias):
    assert mm.is_codex_context_variant(alias) is False
    assert mm._verified_codex_ctx_for_slug(alias) is None
    assert mm.strip_codex_context_variant_suffix(alias) == alias
    assert mm.codex_uses_large_window(alias) is False


def test_large_policy_non_stale_advertisement_still_trusted(large):
    """Catalog-moves rule unchanged: only the stale 272,000 is bumped."""
    for advertised_ctx in (372_000, 200_000, 1_050_000):
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {
            "models": [{"slug": "gpt-6-sol", "context_window": advertised_ctx}]
        }
        mm._codex_oauth_context_cache = {}
        with patch("agent.model_metadata.model_metadata_http.get", return_value=fake), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.save_context_length"):
            ctx = mm.get_model_context_length(
                # JWT-shaped: the live probe refuses a non-JWT credential aimed at chatgpt.com
                # (#121486), and this test needs the live catalog to be read.
                model="gpt-6-sol", base_url=_CODEX_URL, api_key=_codex_jwt("tok"),
                provider="openai-codex",
            )
        assert ctx == advertised_ctx


# -- resolver under ``advertised`` (flip-back) -------------------------------

def test_advertised_policy_restores_opt_in(advertised):
    assert mm.codex_context_policy() == "advertised"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") is None
    assert _codex_ctx("gpt-6-sol") == 272_000
    assert _codex_ctx("gpt-6.1-sol") == 272_000
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol-900k") == 872_000
    assert _codex_ctx("gpt-6-sol-900k") == 872_000
    assert _codex_ctx("gpt-6.1-sol-900k") == 900_000
    assert mm.codex_uses_large_window("gpt-6-sol") is False
    assert mm.codex_uses_large_window("gpt-6-sol-900k") is True


# -- knob reading -------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        None,                                   # no config.yaml at all
        "",                                     # empty file
        "model:\n  default: gpt-6-sol\n",       # model block without the knob
        "model: gpt-6-sol\n",                   # legacy string-shaped model key
        "model:\n  codex_context_policy: bogus\n",  # unknown value
    ],
)
def test_knob_absent_or_invalid_defaults_to_large(monkeypatch, tmp_path, text):
    _use_config(monkeypatch, tmp_path, text)
    assert mm.codex_context_policy() == "large"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") == 872_000


def test_knob_is_case_insensitive(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: ADVERTISED\n")
    assert mm.codex_context_policy() == "advertised"


def test_profile_config_overrides_root(monkeypatch, tmp_path):
    """Each profile reads its OWN config.yaml, so one agent flips back alone."""
    root = tmp_path / "root"
    profile = root / "profiles" / "daedalus"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("model:\n  codex_context_policy: large\n")
    (profile / "config.yaml").write_text("model:\n  codex_context_policy: advertised\n")

    active = {"path": profile / "config.yaml"}
    monkeypatch.setattr(_config_mod, "get_config_path", lambda: active["path"])
    _config_mod._RAW_CONFIG_CACHE.clear()
    assert mm.codex_context_policy() == "advertised"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") is None

    active["path"] = root / "config.yaml"
    assert mm.codex_context_policy() == "large"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") == 872_000


def test_knob_edit_is_picked_up_without_restart(monkeypatch, tmp_path):
    path = _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: large\n")
    assert mm.codex_context_policy() == "large"
    path.write_text("model:\n  codex_context_policy: advertised   # flipped back\n")
    import os
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert mm.codex_context_policy() == "advertised"


# -- compaction autoraise + 272K NUX banner -----------------------------------

@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.4", "gpt-daybreak-blue-latest"])
def test_large_policy_no_272k_autoraise_or_banner_for_bare_eligible(large, model):
    from agent.agent_init import _resolve_compression_threshold
    from agent.auxiliary_client import _compression_threshold_for_model, _is_codex_gpt54_or_gpt55

    assert _is_codex_gpt54_or_gpt55(model, "openai-codex") is False
    cthresh = _compression_threshold_for_model(model, "openai-codex")
    assert cthresh is None
    threshold, notice = _resolve_compression_threshold(
        0.50, cthresh, model=model, is_codex_autoraise=False,
    )
    assert threshold == 0.50
    assert notice is None  # no "caps context at 272K" banner


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.4", "gpt-daybreak-blue-latest"])
def test_advertised_policy_bare_eligible_keeps_272k_autoraise_and_banner(advertised, model):
    from agent.agent_init import _build_codex_gpt5_autoraise_notice, _resolve_compression_threshold
    from agent.auxiliary_client import _compression_threshold_for_model

    cthresh = _compression_threshold_for_model(model, "openai-codex")
    assert cthresh == 0.85
    _threshold, notice = _resolve_compression_threshold(
        0.50, cthresh, model=model, is_codex_autoraise=True,
    )
    assert notice is not None
    assert "caps context at 272K" in _build_codex_gpt5_autoraise_notice(notice, 272_000)


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4-mini"])
def test_large_policy_ineligible_keeps_272k_autoraise(large, model):
    from agent.auxiliary_client import _compression_threshold_for_model

    assert _compression_threshold_for_model(model, "openai-codex") == 0.85


# -- compressor (auto-compaction threshold + context-usage footer source) -----

@pytest.mark.parametrize(
    "policy_fixture, model, expected",
    [
        ("large", "gpt-6-sol", 872_000),
        ("large", "gpt-6.1-sol", 900_000),
        ("large", "gpt-6-sol-900k", 872_000),
        ("large", "gpt-5.5", 272_000),
        ("advertised", "gpt-6-sol", 272_000),
        ("advertised", "gpt-6-sol-900k", 872_000),
    ],
)
def test_compressor_sees_policy_window(request, policy_fixture, model, expected):
    request.getfixturevalue(policy_fixture)
    from agent.context_compressor import ContextCompressor

    fake = MagicMock()
    fake.status_code = 401
    fake.json.return_value = {}
    mm._codex_oauth_context_cache = {}
    with patch("agent.model_metadata.model_metadata_http.get", return_value=fake), \
         patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.save_context_length"):
        comp = ContextCompressor(
            model=model, threshold_percent=0.50, base_url=_CODEX_URL,
            api_key="expired-token", provider="openai-codex", quiet_mode=True,
        )
        assert comp.context_length == expected
        assert comp.threshold_tokens <= expected
        if expected >= 512_000:
            # Large window: the plain 50% trigger applies (no small-window floor).
            assert comp.threshold_tokens == int(expected * 0.50)


# -- LCM context engine cap ---------------------------------------------------

def test_lcm_cap_trusts_host_for_bare_eligible_under_large(large):
    from plugins.context_engine.lcm import codex_routing

    assert codex_routing._codex_oauth_context_cap("gpt-6-sol", "openai-codex") is None
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol", "openai-codex") is None
    assert codex_routing._codex_oauth_context_cap("gpt-5.5", "openai-codex") == 272_000


def test_lcm_cap_clamps_bare_eligible_under_advertised(advertised):
    from plugins.context_engine.lcm import codex_routing

    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol", "openai-codex") == 272_000
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openai-codex") is None


# -- picker + /model validation ----------------------------------------------

def test_large_policy_picker_hides_legacy_900k_entries(large):
    from hermes_cli.codex_models import DEFAULT_CODEX_MODELS, _finalize_codex_models

    models = _finalize_codex_models(list(DEFAULT_CODEX_MODELS))
    assert "gpt-6-sol" in models
    assert not [m for m in models if m.endswith("-900k")]


def test_advertised_policy_picker_mints_900k_entries(advertised):
    from hermes_cli.codex_models import DEFAULT_CODEX_MODELS, _finalize_codex_models

    models = _finalize_codex_models(list(DEFAULT_CODEX_MODELS))
    assert "gpt-6-sol-900k" in models


@pytest.mark.parametrize("pin", ["gpt-6-sol-900k", "gpt-6.1-sol-900k", "gpt-5.6-sol-900k"])
def test_large_policy_legacy_900k_pin_still_validates(large, pin):
    from hermes_cli.models_validate import validate_requested_model

    catalog = ["gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol", "gpt-5.5"]
    with patch("hermes_cli.models.provider_model_ids", return_value=catalog):
        result = validate_requested_model(pin, "openai-codex")
    assert result["accepted"] is True
    assert result.get("corrected_model") is None  # never auto-"fixed" to the base


def test_large_policy_ineligible_alias_hint_points_at_bare_slug(large):
    from hermes_cli.models_validate import validate_requested_model

    with patch("hermes_cli.models.provider_model_ids", return_value=["gpt-6-sol", "gpt-5.5"]):
        result = validate_requested_model("gpt-5.5-900k", "openai-codex")
    assert result["accepted"] is False
    assert "272K" in result["message"]
    assert "`gpt-6-sol`" in result["message"]


# -- wire: model id unchanged in both policies -------------------------------

@pytest.mark.parametrize("policy_fixture", ["large", "advertised"])
@pytest.mark.parametrize(
    "model, wire",
    [
        ("gpt-6-sol", "gpt-6-sol"),
        ("gpt-6.1-sol", "gpt-6.1-sol"),
        ("gpt-6-sol-900k", "gpt-6-sol"),
        ("gpt-6.1-sol-900k", "gpt-6.1-sol"),
        ("gpt-5.5", "gpt-5.5"),
    ],
)
def test_wire_model_id_unchanged(request, policy_fixture, model, wire):
    request.getfixturevalue(policy_fixture)
    import agent.transports.codex  # noqa: F401  (registers the transport)
    from agent.transports import get_transport

    transport = get_transport("codex_responses")
    kw = transport.build_kwargs(
        model=model, messages=[{"role": "user", "content": "Hi"}], tools=[],
        params={"is_codex_backend": True},
    )
    assert kw["model"] == wire


# -- merged-config read: managed scope + ${VAR} (t_27a85d2c, Prism P1 on #1557) --

def _use_managed(monkeypatch, tmp_path, text):
    """Point the managed-scope dir at a fresh dir holding config.yaml = *text*."""
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    path = managed / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    _config_mod._LOAD_CONFIG_CACHE.clear()
    return path


def test_managed_scope_policy_overrides_user_file(monkeypatch, tmp_path):
    """An administrator-pinned policy wins over the user's config.yaml."""
    _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: large\n")
    _use_managed(monkeypatch, tmp_path, "model:\n  codex_context_policy: advertised\n")
    assert mm.codex_context_policy() == "advertised"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") is None
    assert _codex_ctx("gpt-6-sol") == 272_000


def test_managed_scope_policy_edit_is_picked_up_without_restart(monkeypatch, tmp_path):
    """The read cache is keyed on the managed file too, so a managed edit lands."""
    import os

    _use_config(monkeypatch, tmp_path, "model:\n  codex_context_policy: advertised\n")
    managed = _use_managed(monkeypatch, tmp_path, "model:\n  codex_context_policy: advertised\n")
    assert mm.codex_context_policy() == "advertised"
    # Same size on purpose: only the mtime moves, the size signature does not.
    managed.write_text("model:\n  codex_context_policy: large     \n", encoding="utf-8")
    st = managed.stat()
    os.utime(managed, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert mm.codex_context_policy() == "large"
    assert mm._verified_codex_ctx_for_slug("gpt-6-sol") == 872_000


def test_policy_env_reference_is_expanded(monkeypatch, tmp_path):
    """``${VAR}`` in the knob expands like every other behavioural config value."""
    monkeypatch.setenv("CODEX_POLICY_T27A85D2C", "advertised")
    _use_config(
        monkeypatch, tmp_path,
        "model:\n  codex_context_policy: ${CODEX_POLICY_T27A85D2C}\n",
    )
    assert mm.codex_context_policy() == "advertised"
