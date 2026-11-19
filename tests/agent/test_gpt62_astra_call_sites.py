"""gpt-6.2-astra gets the Astra runtime treatment at every ``is_astra_model`` call site (t_eb830c20).

Each test names the call site it pins. Server facts behind them (measured 2026-11-18 on the codex
backend and api.openai.com): ``none``/``minimal`` effort 400, ``max`` 200; temperature, top_p and
logprobs 400 on api.openai.com; native compaction fires on the codex backend (output carries
``compaction`` items at compact_threshold=1000).
"""

import json

import pytest

ASTRA_62 = ("gpt-6.2-astra", "cpa/gpt-6.2-astra", "gpt-6.2-astra-900k", "openai-codex/GPT-6.2-ASTRA-900K")
SOL_62 = "gpt-6.2-sol"
CODEX_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_URL = "https://api.openai.com/v1"


@pytest.mark.parametrize("model", ASTRA_62)
def test_is_astra_model_covers_62(model):
    from agent.reasoning_effort import is_astra_model

    assert is_astra_model(model)


def test_is_astra_model_negative_sol_and_gpt6_kept():
    from agent.reasoning_effort import is_astra_model

    assert not is_astra_model(SOL_62)
    assert not is_astra_model("gpt-6.2-astra-pro")  # exact ids only, no prefix match
    assert is_astra_model("gpt-6-astra") and is_astra_model("gpt-6-astra-900k")  # ADD-KEEP


def test_astra_id_set_is_one_edit_per_base():
    """The next Astra is one base id; its ``-900k`` alias is derived."""
    from agent.reasoning_effort import ASTRA_BASE_IDS, ASTRA_MODEL_IDS

    assert ASTRA_MODEL_IDS == frozenset(b + s for b in ASTRA_BASE_IDS for s in ("", "-900k"))
    assert {"gpt-6-astra", "gpt-6.2-astra"} <= set(ASTRA_BASE_IDS)


# agent/native_compaction.py:38
@pytest.mark.parametrize("model", ASTRA_62)
def test_native_compaction_on_official_codex_oauth(model):
    from agent.native_compaction import is_native_compaction_model, resolve_native_compaction_capabilities

    assert is_native_compaction_model(model, provider="openai-codex", base_url=CODEX_URL)
    assert resolve_native_compaction_capabilities(
        model=model, base_url=CODEX_URL, provider="openai-codex", is_codex_backend=True,
    ) == {"native_compaction": True}


@pytest.mark.parametrize("model,provider,base_url", [
    ("gpt-6.2-astra", "cpa", "http://127.0.0.1:18812/v1"),
    ("gpt-6.2-astra", "openai", OPENAI_URL),  # Astra compaction is Codex-OAuth only, same as gpt-6-astra
    (SOL_62, "openai-codex", CODEX_URL),
])
def test_native_compaction_negatives(model, provider, base_url):
    from agent.native_compaction import is_native_compaction_model

    assert not is_native_compaction_model(model, provider=provider, base_url=base_url)


# agent/reasoning_effort.py:111
@pytest.mark.parametrize("model", ASTRA_62)
def test_codex_supported_efforts_62_astra(model):
    from agent.reasoning_effort import CODEX_ASTRA_EFFORTS, codex_supported_efforts

    assert codex_supported_efforts(model) == CODEX_ASTRA_EFFORTS


# agent/transports/codex.py:375 (route gate) + the sanitizer it guards
@pytest.mark.parametrize("model", ASTRA_62)
def test_official_api_sanitizer_applies(model):
    from agent.transports.codex import _is_official_openai_responses_route, _sanitize_astra_request_kwargs

    assert _is_official_openai_responses_route(model, OPENAI_URL)
    kwargs = {"reasoning": {"effort": "none"}, "temperature": 0.5, "top_p": 0.9, "top_logprobs": 2,
              "logprobs": True, "prompt_cache_retention": "24h",
              "include": ["reasoning.encrypted_content", "message.output_text.logprobs"]}
    _sanitize_astra_request_kwargs(kwargs, model, OPENAI_URL)
    assert kwargs == {"reasoning": {"effort": "low"}, "include": ["reasoning.encrypted_content"]}


def test_official_api_sanitizer_negatives():
    from agent.transports.codex import _is_official_openai_responses_route, _sanitize_astra_request_kwargs

    assert not _is_official_openai_responses_route(SOL_62, OPENAI_URL)
    assert not _is_official_openai_responses_route("gpt-6.2-astra", CODEX_URL)
    kwargs = {"reasoning": {"effort": "high"}, "temperature": 0.5}
    _sanitize_astra_request_kwargs(kwargs, SOL_62, OPENAI_URL)
    assert kwargs == {"reasoning": {"effort": "high"}, "temperature": 0.5}


# agent/transports/codex.py:390: deliberately NOT applied to gpt-6.2-astra. The branch downgrades an
# Astra on an undeclared relay to the legacy none..xhigh ladder; both origins 400 ``none`` for
# gpt-6.2-astra, so it keeps its own low..max ladder there (same as gpt-6.2-sol).
@pytest.mark.parametrize("model", ASTRA_62)
def test_undeclared_relay_keeps_62_ladder(model):
    from agent.reasoning_effort import CODEX_ASTRA_EFFORTS
    from agent.transports.codex import _codex_efforts_for_route

    assert _codex_efforts_for_route(model, "https://relay.example/v1") == CODEX_ASTRA_EFFORTS
    assert _codex_efforts_for_route(model, CODEX_URL, is_codex_backend=True) == CODEX_ASTRA_EFFORTS
    assert _codex_efforts_for_route(model, OPENAI_URL) == CODEX_ASTRA_EFFORTS


def test_undeclared_relay_gpt6_astra_unchanged():
    from agent.reasoning_effort import CODEX_LEGACY_EFFORTS
    from agent.transports.codex import _codex_efforts_for_route

    assert _codex_efforts_for_route("gpt-6-astra", "https://relay.example/v1") == CODEX_LEGACY_EFFORTS


# hermes_cli/codex_models.py:135
def test_codex_static_fallbacks_do_not_advertise_62_astra(monkeypatch, tmp_path):
    from hermes_cli import codex_models

    (tmp_path / "config.toml").write_text('model = "gpt-6.2-astra"\n', encoding="utf-8")
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6.2-astra", "priority": 0}, {"slug": "gpt-6.2-astra-900k", "priority": 1},
        {"slug": SOL_62, "priority": 2},
    ]}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex_models, "_fetch_models_from_api", lambda _token, **_kw: [])

    ids = codex_models.get_codex_model_ids(access_token="stale-token")
    assert "gpt-6.2-astra" not in ids and "gpt-6.2-astra-900k" not in ids
    assert SOL_62 in ids

    monkeypatch.setattr(codex_models, "_fetch_models_from_api",
                        lambda _token, **_kw: codex_models._finalize_codex_models(["gpt-6.2-astra"]))
    assert "gpt-6.2-astra" in codex_models.get_codex_model_ids(access_token="entitled-token")


# hermes_cli/models.py:2017
def test_openai_live_catalog_appends_62_astra(monkeypatch):
    from hermes_cli import models

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(models, "_get_model_config_dict", lambda: {})
    monkeypatch.setattr(models, "fetch_api_models", lambda *_a, **_kw: ["gpt-6.2-astra", "whisper-1"])
    monkeypatch.setitem(models._PROVIDER_MODELS, "openai", [SOL_62])

    assert "gpt-6.2-astra" in models._openai_catalog("openai", False)


# hermes_cli/models.py:2552
@pytest.mark.parametrize("model", ASTRA_62)
@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
def test_requires_account_discovery(provider, model):
    from hermes_cli.models import _model_requires_account_discovery

    assert _model_requires_account_discovery(provider, model)


def test_requires_account_discovery_negatives():
    from hermes_cli.models import _model_requires_account_discovery

    assert not _model_requires_account_discovery("openai-codex", SOL_62)
    assert not _model_requires_account_discovery("cpa", "gpt-6.2-astra")


# hermes_cli/models.py:2931
@pytest.mark.parametrize("model", ("gpt-6.2-astra", "openai/gpt-6.2-astra", "gpt-6.2-astra-900k"))
def test_copilot_effort_ladder(model):
    from agent.reasoning_effort import CODEX_ASTRA_EFFORTS
    from hermes_cli.models import _github_reasoning_efforts_for_model_id

    assert _github_reasoning_efforts_for_model_id(model) == list(CODEX_ASTRA_EFFORTS)


def test_copilot_effort_ladder_negative_sol():
    from hermes_cli.models import _github_reasoning_efforts_for_model_id

    assert _github_reasoning_efforts_for_model_id(SOL_62) == []
