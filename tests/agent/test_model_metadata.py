"""Tests for agent/model_metadata.py — token estimation, context lengths,
probing, caching, and error parsing.

Coverage levels:
  Token estimation       — concrete value assertions, edge cases
  Context length lookup  — resolution order, fuzzy match, cache priority
  API metadata fetch     — caching, TTL, canonical slugs, stale fallback
  Probe tiers            — descending, boundaries, extreme inputs
  Error parsing          — OpenAI, Ollama, Anthropic, edge cases
  Persistent cache       — save/load, corruption, update, provider isolation
"""

import math
import time

import pytest
import yaml
from unittest.mock import patch, MagicMock

from agent.model_metadata import (
    CONTEXT_PROBE_TIERS,
    DEFAULT_CONTEXT_LENGTHS,
    DEFAULT_FALLBACK_CONTEXT,
    _strip_provider_prefix,
    estimate_tokens_rough,
    estimate_messages_tokens_rough,
    get_model_context_length,
    get_next_probe_tier,
    get_cached_context_length,
    parse_context_limit_from_error,
    save_context_length,
    fetch_model_metadata,
    _MODEL_CACHE_TTL,
    estimate_request_tokens_rough,
)


# =========================================================================
# Token estimation
# =========================================================================


_AGG_MODELS_DEV_SAMPLE = {
    "anthropic": {"models": {"claude-fable-5": {"limit": {"context": 1000000}}}},
    "openai": {"models": {"gpt-5.6-sol": {"limit": {"context": 1050000}}}},
    "google": {"models": {"gemini-3-pro": {"limit": {"context": 1048576}}}},
    "deepseek": {"models": {"deepseek-v3": {"limit": {"context": 128000}}}},
    # A synthetic vendor id that matches NO hardcoded DEFAULT_CONTEXT_LENGTHS
    # substring — so the negative-gate test can prove the aggregator branch is
    # what supplies the window (vs. the catalog fuzzy-match at step 8).
    "othervendor": {"models": {"zzq-only-on-othervendor-1": {"limit": {"context": 333333}}}},
}

class TestEstimateTokensRough:
    def test_empty_string(self):
        assert estimate_tokens_rough("") == 0


    def test_known_length(self):
        # Classic (len+3)//4 rule for sparse/ASCII text (CJK-dense-aware
        # estimator, adopted from upstream): 400 // 4 = 100.
        assert estimate_tokens_rough("a" * 400) == 100





class TestEstimateMessagesTokensRough:



    def test_tool_call_message(self):
        """Tool call messages with no 'content' key still contribute tokens."""
        msg = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "1", "function": {"name": "terminal", "arguments": "{}"}}]}
        result = estimate_messages_tokens_rough([msg])
        assert result > 0
        assert result == math.ceil(len(str(msg)) / 3.5)

    def test_message_with_list_content(self):
        """Vision messages with multimodal content arrays.

        Image parts are counted at a flat ~1500-token rate per image
        rather than counting the base64 char length, so a tiny stub
        payload still registers as full image cost.
        """
        msg = {"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ]}
        result = estimate_messages_tokens_rough([msg])
        # Flat cost = 1500 per image plus the small text overhead. Allow
        # a small band so this isn't a change-detector for the exact
        # string representation.
        assert 1500 <= result < 2000

    def test_api_content_substitutes_for_content_not_added_to_it(self):
        """``api_content`` replaces ``content`` on the wire, so count one.

        ``turn_context.substitute_api_content()`` pops the sidecar and
        overwrites ``content`` at every API-bound build site. Counting both
        doubled the estimate for any message carrying a sidecar.
        """
        body = "cached prompt bytes " * 2000
        wire_shape = {"role": "user", "content": body}
        persisted_shape = {"role": "user", "content": body, "api_content": body}

        assert estimate_messages_tokens_rough([persisted_shape]) == \
            estimate_messages_tokens_rough([wire_shape])

    def test_api_content_is_counted_when_it_differs_from_content(self):
        """The sidecar is what's sent, so its size is the one that matters."""
        big_sidecar = "cached prompt bytes " * 2000
        msg = {"role": "user", "content": "short", "api_content": big_sidecar}

        result = estimate_messages_tokens_rough([msg])

        # Lower bound: fails if the sidecar were dropped rather than
        # substituted (which would undercount the real request).
        assert result >= (len(big_sidecar) // 4) * 0.9

    def test_non_string_api_content_does_not_displace_content(self):
        """Only a sidecar shape the wire actually substitutes may displace content.

        ``substitute_api_content()`` overwrites ``content`` only for a
        non-empty STRING sidecar on a user/assistant row; every other shape
        is popped and discarded, leaving the clean ``content`` on the wire.
        The shadow must mirror that guard — substituting unconditionally
        would drop the real content from the estimate and UNDERcount, which
        is the dangerous direction (compaction fires too late and the turn
        dies on a hard context error).
        """
        body = "clean stored content " * 2000
        baseline = estimate_messages_tokens_rough([{"role": "user", "content": body}])

        for bad_sidecar in (None, "", 42, ["not", "a", "string"]):
            msg = {"role": "user", "content": body, "api_content": bad_sidecar}
            assert estimate_messages_tokens_rough([msg]) >= baseline, bad_sidecar

        # Same for a role the substitution never applies to.
        tool_row = {"role": "tool", "content": body, "api_content": "ignored"}
        assert estimate_messages_tokens_rough([tool_row]) >= baseline

    def test_image_stripping_survives_shadow_extraction(self):
        """Non-regression for the ``_wire_message_shadow()`` extraction.

        Both estimator helpers now share one shadow builder; this pins the
        flat per-image accounting that the extraction moved, independent of
        the ``api_content`` fix (a valid sidecar is a string, so it cannot
        carry an image list).
        """
        import base64
        import os

        payload = "data:image/png;base64," + base64.b64encode(os.urandom(300_000)).decode()
        msg = {"role": "user",
               "content": [{"type": "image_url", "image_url": {"url": payload}}]}

        # Raw base64 would be ~100K tokens; the flat per-image model is ~1.5K.
        assert estimate_messages_tokens_rough([msg]) < 5_000



class TestEstimateRequestTokensRough:
    def test_caches_tools_estimate(self):
        messages = [{"role": "user", "content": "hello"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                },
            }
        ]

        # json.dumps is used for params sizing; ensure the tools estimate is cached
        # so repeated calls don't keep re-serializing the same schema list.
        with patch("agent.model_metadata.json.dumps", wraps=__import__("json").dumps) as dumps:
            estimate_request_tokens_rough(messages, system_prompt="x" * 8, tools=tools)
            estimate_request_tokens_rough(messages, system_prompt="x" * 8, tools=tools)
            assert dumps.call_count == 1

    def test_tools_cache_is_bounded(self):
        # A long-lived process builds many transient tool lists; the cache must
        # not grow without bound. Feed more distinct lists than the cap and
        # confirm the cache never exceeds it.
        import agent.model_metadata as mm

        mm._TOOLS_TOKENS_CACHE.clear()
        cap = mm._TOOLS_TOKENS_CACHE_MAX
        # Keep references so ids are not recycled mid-loop, forcing distinct keys.
        held = []
        for i in range(cap + 50):
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": f"tool_{i}",
                        "description": "d",
                        "parameters": {"type": "object"},
                    },
                }
            ]
            held.append(tools)
            mm._estimate_tools_tokens_rough(tools)
            assert len(mm._TOOLS_TOKENS_CACHE) <= cap
        assert len(mm._TOOLS_TOKENS_CACHE) == cap


# =========================================================================
# Default context lengths
# =========================================================================

class TestDefaultContextLengths:
    def test_nvidia_deepseek_v4_pro_context_is_endpoint_scoped(self):
        """NVIDIA's 262K NIM window must not lower DeepSeek V4 globally."""
        with patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
             patch("agent.model_metadata._query_ollama_api_show", return_value=None), \
             patch("agent.models_dev.lookup_models_dev_context", return_value=None):
            accepted_urls = (
                "https://integrate.api.nvidia.com/v1",
                "https://INTEGRATE.API.NVIDIA.COM/v1/",
                "https://integrate.api.nvidia.com:443/v1",
            )
            rejected_urls = (
                "http://integrate.api.nvidia.com/v1",
                "https://integrate.api.nvidia.com:8443/v1",
                "https://integrate.api.nvidia.com/v1/other",
                "https://integrate.api.nvidia.com/v1?route=other",
                "https://example.invalid/v1",
                "https://api.deepseek.com/v1",
                "https://openrouter.ai/api/v1",
            )

            for base_url in accepted_urls:
                assert get_model_context_length(
                    "deepseek-ai/deepseek-v4-pro",
                    provider="nvidia",
                    base_url=base_url,
                ) == 262_144

            for base_url in rejected_urls:
                assert get_model_context_length(
                    "deepseek-ai/deepseek-v4-pro",
                    provider="nvidia",
                    base_url=base_url,
                ) == 1_000_000

    def test_k3_context_is_scoped_to_confirmed_coding_endpoint(self):
        """The bare ``k3`` slug's 1 Mi context must not leak to unverified endpoints.

        The named ``kimi-k3`` / ``kimi-k3-cot`` slugs resolve to 1 Mi
        EVERYWHERE via DEFAULT_CONTEXT_LENGTHS — the window is a property of
        the model, served at 1M on api.moonshot.ai and api.moonshot.cn alike
        (verified against models.dev + OpenRouter live metadata). Only the
        bare ``k3`` slug, which exists solely on the Kimi Coding Plan
        endpoint, stays endpoint-scoped.
        """
        with patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
             patch("agent.model_metadata._query_ollama_api_show", return_value=None), \
             patch("agent.models_dev.lookup_models_dev_context", return_value=None):
            accepted_urls = (
                "https://api.kimi.com/coding",
                "https://API.KIMI.COM/coding/",
                "https://api.kimi.com:443/coding",
                "https://api.kimi.com/coding/v1",
            )
            rejected_urls = (
                "http://api.kimi.com/coding",
                "https://api.kimi.com:8443/coding",
                "https://api.kimi.com/coding/../other",
                "https://api.kimi.com/codingevil",
                "https://example.invalid/coding",
                "https://[api.kimi.com/coding",
                "https://api.moonshot.ai/v1",
                "https://api.moonshot.cn/v1",
            )

            for base_url in accepted_urls:
                for model in ("k3", "kimi-k3", "kimi-k3-cot"):
                    assert get_model_context_length(
                        model, provider="kimi-coding", base_url=base_url
                    ) == 1_048_576

            for base_url in rejected_urls:
                # Bare slug: endpoint-scoped, must NOT leak off-endpoint.
                assert get_model_context_length(
                    "k3", provider="kimi-coding", base_url=base_url
                ) != 1_048_576
                # Named slugs: global DEFAULT_CONTEXT_LENGTHS entry applies
                # everywhere the model is actually named kimi-k3.
                for model in ("kimi-k3", "kimi-k3-cot"):
                    assert get_model_context_length(
                        model, provider="kimi-coding", base_url=base_url
                    ) == 1_048_576


    def test_anthropic_1m_models_resolve_to_1m_via_fallback_table(self):
        """The 1M-context Anthropic models must resolve to 1,000,000 via the
        DEFAULT_CONTEXT_LENGTHS fallback table, NOT the ``"claude": 200000``
        catch-all.

        This is the load-bearing invariant for the whole Mac Studio fleet: the
        Claude models are served over OAuth through the local claude-api-proxy
        / claude-bridge providers, so steps 2-6 of get_model_context_length()
        (custom /models, API-key /v1/models, OpenRouter) all no-op and
        resolution lands on the fallback table. If a ``git pull`` reverts the
        opus/sonnet/fable rows in agent/model_metadata.py, every one of those
        models silently falls through to the ``"claude": 200000`` catch-all —
        a 5x under-count that drops the compression trigger from ~800k to
        ~160k tokens and truncates history mid-conversation across every agent.
        A bare table assertion (not a change-detector: 200000 vs 1000000 is a
        behavior boundary, not a snapshot) makes that revert loud.

        haiku-4-5 is deliberately NOT in this list — its real window is 200k,
        so it correctly resolves via the catch-all and must stay there.
        """
        from agent.model_metadata import get_model_context_length
        from unittest.mock import patch as mock_patch

        # Force fall-through to DEFAULT_CONTEXT_LENGTHS: no cache, no provider
        # metadata, no cross-provider registry hit.
        with mock_patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             mock_patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             mock_patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
             mock_patch("agent.models_dev.lookup_models_dev_context", return_value=None):
            one_m = (
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-fable-5",
                "claude-mythos-5",
            )
            for model_id in one_m:
                actual = get_model_context_length(model_id)
                assert actual == 1_000_000, (
                    f"{model_id}: expected 1000000 (1M window), got {actual}. "
                    f"If this is 200000 the DEFAULT_CONTEXT_LENGTHS row was "
                    f"reverted and the model fell through to the 'claude' "
                    f"catch-all — re-add the bare-id row."
                )
            # Negative control: haiku's real window IS 200k; it must NOT be
            # accidentally promoted to 1M by a too-broad rule.
            assert get_model_context_length("claude-haiku-4-5") == 200000, (
                "claude-haiku-4-5 should resolve to its real 200k window, not 1M"
            )

    def test_xai_oauth_grok_build_uses_xai_models_dev_context(self):
        """xAI OAuth should share the xAI provider metadata path.

        The xAI /v1/models endpoint does not currently include context fields
        for grok-build-0.1, so this guards against falling through to the
        generic "grok" 131k fallback when using OAuth credentials.
        """
        registry = {
            "xai": {
                "models": {
                    "grok-build-0.1": {
                        "limit": {"context": 256000, "output": 64000},
                    },
                },
            },
        }
        with patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata._query_ollama_api_show", return_value=None), \
             patch("agent.models_dev.fetch_models_dev", return_value=registry):
            assert get_model_context_length(
                "grok-build-0.1",
                provider="xai-oauth",
                base_url="https://api.x.ai/v1",
                api_key="oauth-token",
            ) == 256000

    def test_deepseek_v4_models_1m_context(self):
        from agent.model_metadata import get_model_context_length
        from unittest.mock import patch as mock_patch

        expected_keys = {
            "deepseek-v4-pro": 1_000_000,
            "deepseek-v4-flash": 1_000_000,
            "deepseek-chat": 1_000_000,
            "deepseek-reasoner": 1_000_000,
        }
        for key, value in expected_keys.items():
            assert key in DEFAULT_CONTEXT_LENGTHS, f"{key} missing"
            assert DEFAULT_CONTEXT_LENGTHS[key] == value, (
                f"{key} should be {value}, got {DEFAULT_CONTEXT_LENGTHS[key]}"
            )

        # Longest-first substring matching must resolve both the bare V4
        # ids (native DeepSeek) and the vendor-prefixed forms (OpenRouter
        # / Nous Portal) to 1M without probing down to the legacy 128K
        # ``deepseek`` substring fallback.
        with mock_patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             mock_patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
             mock_patch("agent.model_metadata.get_cached_context_length", return_value=None):
            cases = [
                ("deepseek-v4-pro", 1_000_000),
                ("deepseek-v4-flash", 1_000_000),
                ("deepseek/deepseek-v4-pro", 1_000_000),
                ("deepseek/deepseek-v4-flash", 1_000_000),
                ("deepseek-chat", 1_000_000),
                ("deepseek-reasoner", 1_000_000),
            ]
            for model_id, expected_ctx in cases:
                actual = get_model_context_length(model_id)
                assert actual == expected_ctx, (
                    f"{model_id}: expected {expected_ctx}, got {actual}"
                )

    def test_claude_frontier_models_never_fall_through_to_the_200k_catchall(self):
        """Every current Claude frontier id must resolve to its real 1M window.

        REGRESSION (2026-07-31): ``claude-opus-5`` shipped and was never added to
        DEFAULT_CONTEXT_LENGTHS. Every generation-5 sibling (sonnet-5, fable-5,
        mythos-5) was listed, so the omission was invisible -- opus-5 silently hit
        the ``"claude": 200000`` legacy catch-all and resolved at a FIFTH of its
        real window, capping compaction mid-conversation with no error anywhere.

        This asserts the RULE ("a frontier Claude id must never land on the legacy
        catch-all"), so the next rollout that forgets an id fails here instead of
        silently under-reporting context in production. The catch-all itself is
        still correct for genuine legacy Claude 3.x ids, which is asserted too.
        """
        from agent.model_metadata import get_model_context_length
        from unittest.mock import patch as mock_patch

        frontier = [
            "claude-opus-5",
            "claude-opus-5-fast",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
        ]
        for model_id in frontier:
            resolved = next(
                (
                    length
                    for key, length in sorted(
                        DEFAULT_CONTEXT_LENGTHS.items(),
                        key=lambda x: len(x[0]),
                        reverse=True,
                    )
                    if key in model_id
                ),
                None,
            )
            assert resolved == 1_000_000, (
                f"{model_id} resolved to {resolved} via the hardcoded catalog; a "
                f"frontier Claude model must be 1M, not the legacy 200K catch-all"
            )

        with mock_patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             mock_patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
             mock_patch("agent.model_metadata.get_cached_context_length", return_value=None):
            for model_id in frontier:
                actual = get_model_context_length(model_id)
                assert actual == 1_000_000, (
                    f"{model_id}: expected 1000000, got {actual}"
                )
            # The legacy catch-all must still apply to genuine Claude 3.x ids --
            # this fix must not blanket-promote every "claude" string to 1M.
            assert get_model_context_length("claude-3-opus-20240229") == 200000





# =========================================================================
# Codex OAuth context-window resolution (provider="openai-codex")
# =========================================================================

class TestCodexOAuthContextLength:
    """ChatGPT Codex OAuth context windows come from the authenticated
    /models catalogue and may differ from the static fallback table or the
    direct OpenAI API allocation. The fallback values below are conservative
    defaults used only when the live probe is unavailable.
    """

    def setup_method(self):
        import agent.model_metadata as mm
        mm._codex_oauth_context_cache = {}



    def test_live_catalogue_cache_is_scoped_to_access_token(self):
        """Different OAuth tokens must not share entitlement-specific metadata."""
        from agent import model_metadata as mm
        from agent.model_metadata import get_model_context_length

        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "models": [{"slug": "gpt-5.6-terra", "context_window": 272_000}]
        }
        second_response = MagicMock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "models": [{"slug": "gpt-5.6-terra", "context_window": 372_000}]
        }

        with patch(
            "agent.model_metadata.requests.get",
            side_effect=[first_response, second_response],
        ) as mock_get, patch("agent.model_metadata.save_context_length") as mock_save:
            first = get_model_context_length(
                "gpt-5.6-terra",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="token-account-a",
                provider="openai-codex",
            )
            first_again = get_model_context_length(
                "gpt-5.6-terra",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="token-account-a",
                provider="openai-codex",
            )
            second = get_model_context_length(
                "gpt-5.6-terra",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="token-account-b",
                provider="openai-codex",
            )

        assert (first, first_again, second) == (272_000, 272_000, 372_000)
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer token-account-a"
        assert mock_get.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer token-account-b"
        assert mock_save.call_count == 2
        assert all(
            "token-account" not in key
            for key in mm._codex_oauth_context_cache
        )

    def test_probe_failure_falls_back_to_hardcoded(self):
        """If the probe fails (non-200 / network error), we still return
        the hardcoded 272k rather than leaking through to models.dev 1.05M."""
        from agent.model_metadata import get_model_context_length

        fake_response = MagicMock()
        fake_response.status_code = 401
        fake_response.json.return_value = {}

        with patch("agent.model_metadata.requests.get", return_value=fake_response), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.save_context_length"):
            ctx = get_model_context_length(
                model="gpt-5.5",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="expired-token",
                provider="openai-codex",
            )
        assert ctx == 272_000


    @pytest.mark.parametrize(
        "stale_context,live_context",
        [(272_000, 372_000), (372_000, 272_000)],
        ids=("expansion", "rollback"),
    )
    def test_live_codex_context_replaces_stale_cache_in_both_directions(
        self, tmp_path, monkeypatch, stale_context, live_context
    ):
        """Authenticated metadata must replace stale disk values in either direction."""
        from agent import model_metadata as mm

        cache_file = tmp_path / "context_length_cache.yaml"
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        base_url = "https://chatgpt.com/backend-api/codex"
        stale_key = f"gpt-5.6-terra@{base_url}"
        other_key = "other-model@https://api.openai.com/v1/"
        import yaml as _yaml
        cache_file.write_text(_yaml.dump({"context_lengths": {
            stale_key: stale_context,
            other_key: 128_000,
        }}))

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "models": [{"slug": "gpt-5.6-terra", "context_window": live_context}]
        }
        # Exercise real persistence here: this test verifies that a live value
        # replaces the stale on-disk entry. Failure-path tests below mock the
        # writer because they assert that fallback values are not persisted.
        with patch("agent.model_metadata.requests.get", return_value=fake_response) as mock_get:
            ctx = mm.get_model_context_length(
                model="gpt-5.6-terra",
                base_url=base_url,
                api_key="fake-token",
                provider="openai-codex",
            )

        assert ctx == live_context
        mock_get.assert_called_once()
        remaining = _yaml.safe_load(cache_file.read_text()).get("context_lengths", {})
        assert remaining.get(stale_key) == live_context
        assert remaining.get(other_key) == 128_000




# =========================================================================
# Custom endpoint model metadata
# =========================================================================

class TestFetchEndpointModelMetadata:
    def setup_method(self):
        import agent.model_metadata as mm
        mm._endpoint_model_metadata_cache.clear()
        mm._endpoint_model_metadata_cache_time.clear()

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_failure_stops_after_first_candidate(self, status_code):
        import agent.model_metadata as mm

        response = MagicMock()
        response.status_code = status_code
        response.raise_for_status.side_effect = RuntimeError(str(status_code))

        with patch("agent.model_metadata.requests.get", return_value=response) as mock_get:
            result = mm.fetch_endpoint_model_metadata("https://custom.example/v1")

        assert result == {}
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["stream"] is True
        response.raise_for_status.assert_not_called()
        response.json.assert_not_called()
        response.close.assert_called_once()

    def test_auth_failure_empty_result_is_cached(self):
        import agent.model_metadata as mm

        response = MagicMock()
        response.status_code = 401
        response.raise_for_status.side_effect = RuntimeError("401")

        with patch("agent.model_metadata.requests.get", return_value=response) as mock_get:
            first = mm.fetch_endpoint_model_metadata("https://custom.example/v1")
            second = mm.fetch_endpoint_model_metadata("https://custom.example/v1")

        assert first == second == {}
        mock_get.assert_called_once()
        response.close.assert_called_once()

    def test_not_found_still_tries_alternate_candidate(self):
        import agent.model_metadata as mm

        not_found = MagicMock()
        not_found.status_code = 404
        not_found.raise_for_status.side_effect = RuntimeError("404")
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {
            "data": [{"id": "test/model", "context_length": 32768}]
        }

        with patch(
            "agent.model_metadata.requests.get",
            side_effect=[not_found, success],
        ) as mock_get:
            result = mm.fetch_endpoint_model_metadata("https://custom.example/v1")

        assert result["test/model"]["context_length"] == 32768
        assert mock_get.call_count == 2
        assert [call.args[0] for call in mock_get.call_args_list] == [
            "https://custom.example/v1/models",
            "https://custom.example/models",
        ]
        assert all(call.kwargs["stream"] is True for call in mock_get.call_args_list)
        not_found.json.assert_not_called()
        not_found.close.assert_called_once()
        success.close.assert_called_once()


# =========================================================================
# Nous Portal context-window resolution (provider="nous")
# =========================================================================

class TestNousPortalContextResolution:
    """Nous Portal /v1/models is authoritative for what Nous infra enforces
    and may diverge from the OpenRouter catalog.

    Invariants this class pins down:
      1. Portal value wins over the OR fallback.
      2. Portal-derived values are persisted to disk.
      3. OR-fallback values are NEVER persisted — otherwise a single portal
         blip would freeze the wrong value in via step-1 cache short-circuit.
      4. Pre-fix persistent-cache entries (seeded from the OR catalog) are
         bypassed at step 1 and overwritten once the portal responds.
      5. Pre-fix persistent-cache entries SURVIVE on disk when the portal
         is unreachable — no opportunistic invalidation that loses the only
         value we have.
    """

    def setup_method(self):
        import agent.model_metadata as mm
        mm._endpoint_model_metadata_cache.clear()
        mm._endpoint_model_metadata_cache_time.clear()



    @patch("agent.model_metadata.fetch_endpoint_model_metadata")
    @patch("agent.model_metadata.fetch_model_metadata")
    def test_openrouter_fallback_is_not_persisted(
        self, mock_or, mock_portal, tmp_path, monkeypatch
    ):
        """When the portal can't resolve a model (network blip, auth glitch,
        model not yet listed) we fall back to the OR catalog so the agent
        keeps working — but we must NOT write the OR value to disk.  Once
        cached on disk, step-1 short-circuits forever and the user is stuck
        with the wrong number until they manually clear the cache."""
        import agent.model_metadata as mm
        cache_file = tmp_path / "context_length_cache.yaml"
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        mock_portal.return_value = {}  # portal unreachable / model unknown
        mock_or.return_value = {
            "qwen/qwen3.6-plus": {"context_length": 1_000_000},
        }

        base_url = "https://inference-api.nousresearch.com/v1"
        ctx = mm.get_model_context_length(
            model="qwen3.6-plus",
            base_url=base_url,
            api_key="fake",
            provider="nous",
        )
        assert ctx == 1_000_000, "OR fallback should still serve the request"
        assert not cache_file.exists() or not yaml.safe_load(
            cache_file.read_text()
        ).get("context_lengths", {}), (
            "OR-fallback values must NOT be persisted — a single portal blip "
            "would otherwise freeze the wrong value in via step-1 cache hit"
        )

    @patch("agent.model_metadata.fetch_endpoint_model_metadata")
    @patch("agent.model_metadata.fetch_model_metadata")
    def test_stale_cache_is_bypassed_and_overwritten_by_portal(
        self, mock_or, mock_portal, tmp_path, monkeypatch
    ):
        """Users upgrading from pre-fix builds have ``qwen3.6-plus@…nous… =
        1000000`` (OR-derived) sitting in their cache file.  Step 1 must
        NOT short-circuit on that entry — step 5b reconciles against the
        portal and overwrites the persistent value with 262144."""
        import agent.model_metadata as mm
        cache_file = tmp_path / "context_length_cache.yaml"
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        base_url = "https://inference-api.nousresearch.com/v1"
        stale_key = f"qwen3.6-plus@{base_url}"
        other_key = "other-model@https://api.openai.com/v1"
        cache_file.write_text(yaml.dump({"context_lengths": {
            stale_key: 1_000_000,     # pre-fix OR-derived value
            other_key: 128_000,       # unrelated, must survive
        }}))

        mock_portal.return_value = {
            "qwen3.6-plus": {"context_length": 262_144},
        }
        mock_or.return_value = {}

        ctx = mm.get_model_context_length(
            model="qwen3.6-plus",
            base_url=base_url,
            api_key="fake",
            provider="nous",
        )
        assert ctx == 262_144, (
            f"Stale OR-derived cache entry should not have leaked through; got {ctx}"
        )

        remaining = yaml.safe_load(cache_file.read_text()).get("context_lengths", {})
        assert remaining.get(stale_key) == 262_144, (
            "Portal value should have overwritten the stale entry on disk"
        )
        assert remaining.get(other_key) == 128_000, (
            "Unrelated cache entries must not be touched"
        )




# =========================================================================
# get_model_context_length — resolution order
# =========================================================================

class TestGetModelContextLength:
    @patch("agent.model_metadata.fetch_model_metadata")
    def test_known_model_from_api(self, mock_fetch):
        mock_fetch.return_value = {
            "test/model": {"context_length": 32000}
        }
        assert get_model_context_length("test/model") == 32000








    @patch("agent.model_metadata.fetch_model_metadata")
    def test_api_missing_context_length_key(self, mock_fetch):
        """Model in API but without context_length → defaults to the top
        probe tier (currently 256K)."""
        mock_fetch.return_value = {"test/model": {"name": "Test"}}
        assert get_model_context_length("test/model") == CONTEXT_PROBE_TIERS[0]


    @patch("agent.model_metadata.fetch_model_metadata")
    def test_no_base_url_skips_cache(self, mock_fetch, tmp_path):
        """Without base_url, cache lookup is skipped."""
        mock_fetch.return_value = {}
        cache_file = tmp_path / "cache.yaml"
        with patch("agent.model_metadata._get_context_cache_path", return_value=cache_file):
            save_context_length("custom/model", "http://local", 32768)
            # No base_url → cache skipped → falls to probe tier
            result = get_model_context_length("custom/model")
            assert result == CONTEXT_PROBE_TIERS[0]


    @patch("agent.model_metadata.fetch_model_metadata")
    @patch("agent.model_metadata.fetch_endpoint_model_metadata")
    def test_custom_endpoint_without_metadata_falls_back_to_catalog(self, mock_endpoint_fetch, mock_fetch):
        """Custom endpoint with no metadata should fall back to the hardcoded
        catalog (not 256K) when the model name matches a known entry.

        Previously this returned CONTEXT_PROBE_TIERS[0] (256K) because the
        custom-endpoint branch short-circuited before the catalog lookup.
        See #38865.
        """
        mock_fetch.return_value = {}
        mock_endpoint_fetch.return_value = {}

        # GLM-5-TEE matches the "glm" entry in DEFAULT_CONTEXT_LENGTHS
        result = get_model_context_length(
            "zai-org/GLM-5-TEE",
            base_url="https://llm.chutes.ai/v1",
            api_key="test-key",
        )
        assert result == 202752  # "glm" entry in DEFAULT_CONTEXT_LENGTHS






    @patch("agent.model_metadata.fetch_model_metadata")
    def test_custom_endpoint_falls_back_to_hardcoded_catalog(self, mock_fetch):
        """Custom/proxied endpoint that fails all probes should still resolve
        via DEFAULT_CONTEXT_LENGTHS instead of returning 256K.

        Regression test for #38865: a corporate Anthropic proxy (custom
        base_url) caused the custom-endpoint branch to short-circuit before
        the catalog lookup, capping context at 256K even for models like
        claude-opus-4-8 that are in the hardcoded catalog with 1M.
        """
        mock_fetch.return_value = {}

        # Patch all the probe functions that the custom-endpoint branch calls
        # so they all fail (return None/empty), simulating a proxy that
        # doesn't expose Ollama or local-server endpoints.
        with (
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch(
                "agent.model_metadata._query_ollama_api_show",
                return_value=None,
            ),
            patch(
                "agent.model_metadata._query_local_context_length",
                return_value=None,
            ),
            patch(
                "agent.model_metadata.is_local_endpoint",
                return_value=False,
            ),
        ):
            # A known model behind a custom proxy should resolve to its
            # catalog value (1M), NOT the 256K fallback.
            ctx = get_model_context_length(
                "claude-opus-4-8",
                base_url="https://my-gateway.example.com/v1/claude",
            )
            assert ctx == 1000000, f"Expected 1000000, got {ctx}"

            # Another known model
            ctx2 = get_model_context_length(
                "claude-sonnet-4-6",
                base_url="https://my-gateway.example.com/v1/claude",
            )
            assert ctx2 == 1000000, f"Expected 1000000, got {ctx2}"

            # An unknown model on a custom endpoint should still fall back
            # to 256K (no catalog match).
            ctx3 = get_model_context_length(
                "totally-unknown-model",
                base_url="https://my-gateway.example.com/v1/claude",
            )
            assert ctx3 == DEFAULT_FALLBACK_CONTEXT, (
                f"Expected {DEFAULT_FALLBACK_CONTEXT}, got {ctx3}"
            )

    # ── Local vs non-local Ollama context resolution (#63122) ──────────

    @patch("agent.model_metadata.get_cached_context_length", return_value=None)
    @patch("agent.model_metadata.fetch_model_metadata", return_value={})
    @patch("agent.model_metadata._resolve_endpoint_context_length", return_value=None)
    @patch("agent.model_metadata._query_ollama_api_show", return_value=131072)
    @patch("agent.model_metadata._query_local_context_length", return_value=32768)
    @patch("agent.model_metadata.is_local_endpoint", return_value=True)
    @patch("agent.model_metadata.save_context_length")
    @patch("agent.model_metadata._maybe_cache_local_context_length")
    def test_local_ollama_prefers_num_ctx_over_gguf(
        self,
        mock_maybe_cache, mock_save,
        mock_is_local, mock_local_ctx,
        mock_ollama_show, mock_resolve_ep,
        mock_fetch, mock_cache,
    ):
        """Local Ollama: _query_local_context_length (num_ctx-first) must
        win over _query_ollama_api_show (GGUF-first).  The configured
        Modelfile num_ctx is the context value the local probe prefers;
        the GGUF training max can be larger and would create a false-safe
        window for compression (#63122)."""
        result = get_model_context_length(
            "my-model",
            base_url="http://localhost:11434",
        )
        assert result == 32768, (
            f"Expected configured Modelfile num_ctx (32768), got {result}. "
            "Local Ollama must prefer num_ctx over GGUF training max."
        )
        # The non-local-oriented probe must NOT fire when local probe succeeds
        mock_ollama_show.assert_not_called()
        # The local probe MUST be called exactly once
        mock_local_ctx.assert_called_once()




# =========================================================================
# Bedrock context resolution — must run BEFORE custom-endpoint probe
# =========================================================================

class TestBedrockContextResolution:
    """Regression tests for Bedrock context-length resolution order.

    Bug: because ``bedrock-runtime.<region>.amazonaws.com`` is not listed in
    ``_URL_TO_PROVIDER``, ``_is_known_provider_base_url`` returned False and
    the custom-endpoint probe at step 2 ran first — fetching ``/models`` from
    Bedrock (which it doesn't serve), returning the 128K default-fallback
    before execution ever reached the Bedrock branch.

    Fix: promote the Bedrock branch ahead of the custom-endpoint probe.
    """




    @patch("agent.model_metadata.fetch_endpoint_model_metadata")
    def test_bedrock_claude_4_6_ignores_stale_200k_cache(self, mock_fetch, tmp_path):
        """Old 200K Bedrock cache entries must not mask the 1M table entry."""
        cache_file = tmp_path / "context_length_cache.yaml"
        base_url = "https://bedrock-runtime.us-east-2.amazonaws.com"
        with patch("agent.model_metadata._get_context_cache_path", return_value=cache_file):
            save_context_length("us.anthropic.claude-sonnet-4-6", base_url, 200_000)
            ctx = get_model_context_length(
                "us.anthropic.claude-sonnet-4-6",
                provider="bedrock",
                base_url=base_url,
            )
        assert ctx == 1_000_000
        mock_fetch.assert_not_called()


    @patch("agent.model_metadata.fetch_endpoint_model_metadata")
    def test_non_bedrock_url_still_probes(self, mock_fetch):
        """Non-Bedrock hosts still reach the custom-endpoint probe."""
        mock_fetch.return_value = {"some-model": {"context_length": 50000}}
        ctx = get_model_context_length(
            "some-model",
            base_url="https://api.example.com/v1",
        )
        assert ctx == 50000
        assert mock_fetch.called


# =========================================================================
# _strip_provider_prefix — Ollama model:tag vs provider:model
# =========================================================================

class TestStripProviderPrefix:
    def test_known_provider_prefix_is_stripped(self):
        assert _strip_provider_prefix("local:my-model") == "my-model"
        assert _strip_provider_prefix("openrouter:anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"
        assert _strip_provider_prefix("anthropic:claude-sonnet-4") == "claude-sonnet-4"
        assert _strip_provider_prefix("stepfun:step-3.5-flash") == "step-3.5-flash"


    def test_http_urls_preserved(self):
        assert _strip_provider_prefix("http://example.com") == "http://example.com"
        assert _strip_provider_prefix("https://example.com") == "https://example.com"


    @patch("agent.model_metadata.fetch_model_metadata")
    def test_ollama_model_tag_not_mangled_in_context_lookup(self, mock_fetch):
        """Ensure 'qwen3.5:27b' is NOT reduced to '27b' during context length lookup.

        We mock a custom endpoint that knows 'qwen3.5:27b' — the full name
        must reach the endpoint metadata lookup intact.
        """
        mock_fetch.return_value = {}
        with patch("agent.model_metadata.fetch_endpoint_model_metadata") as mock_ep, \
             patch("agent.model_metadata._is_custom_endpoint", return_value=True):
            mock_ep.return_value = {"qwen3.5:27b": {"context_length": 32768}}
            result = get_model_context_length(
                "qwen3.5:27b",
                base_url="http://localhost:11434/v1",
            )
        assert result == 32768


# =========================================================================
# fetch_model_metadata — caching, TTL, slugs, failures
# =========================================================================

class TestClaudeProxyContextLengthContract:
    """Regression: the Claude api-proxy / bridge (claude-apr/apx/bpr/bpx) serve
    an OpenAI-compatible /v1/models that MUST carry a per-model context_length,
    so this harness sizes auto-compaction against the real window instead of
    the 128K probe-tier fallback.

    History (2026-07-10): the proxy's /v1/models emitted model IDs with NO
    context_length, so _resolve_endpoint_context_length returned None and the
    resolver cached 128K for claude-fable-5 (a 1M model) — auto-compacting a
    voice session at ~96K (0.75 * 128K) far below the real 1M window. Fixed by
    advertising context_length in the proxy + bridge catalogs. These tests lock
    the HARNESS side of that contract: given a proxy payload WITH context_length,
    the endpoint parser must extract it; given one WITHOUT, resolution must fall
    through to a probe tier (documenting the pre-fix failure so a future
    regression is unambiguous)."""

    # A realistic post-fix /v1/models body from claude-apx/proxy.js.
    _PROXY_MODELS_BODY = {
        "object": "list",
        "data": [
            {"id": "claude-fable-5", "object": "model", "created": 1, "owned_by": "anthropic",
             "context_length": 1000000, "context_window": 1000000, "max_input_tokens": 1000000},
            {"id": "claude-opus-4-8", "object": "model", "created": 1, "owned_by": "anthropic",
             "context_length": 1000000, "context_window": 1000000, "max_input_tokens": 1000000},
            {"id": "claude-sonnet-5", "object": "model", "created": 1, "owned_by": "anthropic",
             "context_length": 1000000, "context_window": 1000000, "max_input_tokens": 1000000},
            {"id": "claude-haiku-4-5", "object": "model", "created": 1, "owned_by": "anthropic",
             "context_length": 200000, "context_window": 200000, "max_input_tokens": 200000},
        ],
    }

    def _reset_endpoint_cache(self):
        import agent.model_metadata as mm
        mm._endpoint_model_metadata_cache = {}
        mm._endpoint_model_metadata_cache_time = {}

    def _mock_models_response(self, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        resp.ok = True
        return resp

    def test_proxy_payload_context_length_is_extracted_per_model(self):
        """The proxy advertises context_length; the endpoint parser must read
        1M for the large models and 200K for haiku — NOT a probe fallback."""
        import agent.model_metadata as mm
        self._reset_endpoint_cache()
        base_url = "http://100.92.54.25:18801/anthropic"  # a real apx tailscale endpoint shape

        with patch("agent.model_metadata.is_local_endpoint", return_value=False), \
             patch("agent.model_metadata.requests.get",
                   return_value=self._mock_models_response(self._PROXY_MODELS_BODY)):
            meta = mm.fetch_endpoint_model_metadata(base_url, api_key="", force_refresh=True)

        assert meta.get("claude-fable-5", {}).get("context_length") == 1_000_000
        assert meta.get("claude-opus-4-8", {}).get("context_length") == 1_000_000
        assert meta.get("claude-sonnet-5", {}).get("context_length") == 1_000_000
        assert meta.get("claude-haiku-4-5", {}).get("context_length") == 200_000

    def test_resolve_endpoint_context_length_returns_real_window_not_probe_fallback(self):
        """End-to-end: _resolve_endpoint_context_length on a proxy that advertises
        1M returns 1_000_000 — the exact value that stops the 96K over-compaction."""
        import agent.model_metadata as mm
        self._reset_endpoint_cache()
        base_url = "http://127.0.0.1:18810/anthropic"  # the claude-apr pool passthrough

        with patch("agent.model_metadata.is_local_endpoint", return_value=False), \
             patch("agent.model_metadata.requests.get",
                   return_value=self._mock_models_response(self._PROXY_MODELS_BODY)):
            ctx = mm._resolve_endpoint_context_length("claude-fable-5", base_url, api_key="")

        assert ctx == 1_000_000, (
            f"claude-fable-5 must resolve to its advertised 1M window, got {ctx} "
            "(a 128K here is the exact pre-fix bug: 0.75*128K=96K over-compaction)"
        )

    def test_missing_context_length_falls_through_to_probe_tier_documents_prefix_bug(self):
        """The PRE-FIX shape (no context_length) must NOT silently resolve to a
        real window — it returns None so the caller drops to a probe tier. This
        pins the failure mode: if the proxy ever regresses to an unadvertised
        catalog, resolution is None (probe fallback), never a wrong confident value."""
        import agent.model_metadata as mm
        self._reset_endpoint_cache()
        base_url = "http://100.92.54.25:18801/anthropic"
        prefix_bug_body = {
            "object": "list",
            "data": [{"id": "claude-fable-5", "object": "model", "created": 1, "owned_by": "anthropic"}],
        }

        with patch("agent.model_metadata.is_local_endpoint", return_value=False), \
             patch("agent.model_metadata.requests.get",
                   return_value=self._mock_models_response(prefix_bug_body)):
            ctx = mm._resolve_endpoint_context_length("claude-fable-5", base_url, api_key="")

        assert ctx is None, (
            "An unadvertised catalog must resolve to None (probe fallback), not a "
            f"confident wrong window; got {ctx}"
        )

    def test_full_resolution_end_to_end_through_local_proxy_all_three_layers(self, tmp_path, monkeypatch):
        """INTEGRATION guard for the whole 2026-07-10 three-bug chain.

        Each bug had its own unit test, yet the FULL get_model_context_length()
        path still returned 128K because the bugs composed: the endpoint is a
        LOOPBACK Claude proxy, so resolution goes local-server-detection →
        local ctx probe BEFORE the /v1/models catalog. This test drives the
        real get_model_context_length() against a faithful proxy mock that
        reproduces ALL of the proxy's surfaces at once, and asserts the
        composed result is the real 1M window — the exact thing that was broken
        while every unit test was green.

        The three layers it exercises in one path:
          1. detect_local_server_type must NOT classify the proxy as lm-studio
             (it answers /api/v1/models 200 with an OpenAI list envelope);
          2. the local ctx probe must read the Anthropic passthrough's
             max_input_tokens (1M), NOT max_tokens (128K output cap);
          3. the /v1/models catalog advertises context_length=1M as the backstop.
        """
        import agent.model_metadata as mm
        self._reset_endpoint_cache()
        mm._endpoint_probe_path_cache = {}
        mm._LOCAL_CTX_PROBE_CACHE = {}
        # Isolate the persistent cache so a poisoned disk row can't short-circuit.
        cache_file = tmp_path / "context_length_cache.yaml"
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        base_url = "http://127.0.0.1:18801/anthropic"  # loopback → the local path

        # Faithful Claude-proxy surface map, keyed by the path suffix each
        # probe hits. This is what claude-apx actually serves.
        openai_catalog = {
            "object": "list",
            "data": [
                {"id": "claude-fable-5", "object": "model", "created": 1,
                 "owned_by": "anthropic", "context_length": 1000000,
                 "context_window": 1000000, "max_input_tokens": 1000000},
            ],
        }
        ollama_tags = {"models": [{"name": "claude-fable-5", "model": "claude-fable-5",
                                   "context_length": 1000000, "details": {}}]}
        # The Anthropic passthrough of GET /v1/models/{id}: BOTH fields present.
        single_model = {"type": "model", "id": "claude-fable-5",
                        "max_input_tokens": 1000000, "max_tokens": 128000}

        def _resp(status, body):
            r = MagicMock(); r.status_code = status; r.ok = status < 400
            r.json.return_value = body
            r.raise_for_status = MagicMock()
            return r

        def httpx_get(url, *a, **k):
            if url.endswith("/api/v1/models"):
                return _resp(200, openai_catalog)      # NOT lm-studio shape
            if url.endswith("/api/tags"):
                return _resp(200, ollama_tags)          # ollama signature
            if url.endswith("/v1/models/claude-fable-5"):
                return _resp(200, single_model)         # the max_tokens trap
            if url.endswith("/v1/models"):
                return _resp(200, openai_catalog)
            return _resp(404, {})

        def requests_get(url, *a, **k):
            if url.rstrip("/").endswith("/models"):
                return self._mock_models_response(openai_catalog)
            return _resp(404, {})

        httpx_client = MagicMock()
        httpx_client.__enter__ = lambda s: httpx_client
        httpx_client.__exit__ = MagicMock(return_value=False)
        httpx_client.get.side_effect = httpx_get
        httpx_client.post.side_effect = lambda url, *a, **k: _resp(200, {})  # /api/show → {}

        with patch("httpx.Client", return_value=httpx_client), \
             patch("agent.model_metadata.requests.get", side_effect=requests_get):
            ctx = mm.get_model_context_length(
                model="claude-fable-5", base_url=base_url, api_key="",
                provider="claude-apx-1",
            )

        assert ctx == 1_000_000, (
            f"full local-proxy resolution must land on the real 1M window, got {ctx}. "
            "A 128K here means one of the three layers regressed (lm-studio "
            "misdetect / max_tokens-as-context / unadvertised catalog) and the "
            "chain collapsed the window again."
        )


class TestFetchModelMetadata:
    def _reset_cache(self):
        import agent.model_metadata as mm
        mm._model_metadata_cache = {}
        mm._model_metadata_cache_time = 0

    def _isolate_disk_cache(self, monkeypatch, tmp_path):
        import agent.model_metadata as mm
        cache_path = tmp_path / "openrouter_model_metadata.json"
        monkeypatch.setattr(mm, "_get_model_metadata_cache_path", lambda: cache_path)
        return cache_path



    def test_network_success_writes_disk_cache(self, tmp_path, monkeypatch):
        self._reset_cache()
        cache_path = self._isolate_disk_cache(monkeypatch, tmp_path)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"id": "live/model", "context_length": 67890, "name": "Live"}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("agent.model_metadata.requests.get", return_value=mock_response):
            fetch_model_metadata(force_refresh=True)

        assert cache_path.exists()
        assert "live/model" in cache_path.read_text(encoding="utf-8")

    def test_network_failure_falls_back_to_stale_disk_cache(self, tmp_path, monkeypatch):
        self._reset_cache()
        cache_path = self._isolate_disk_cache(monkeypatch, tmp_path)
        cache_path.write_text(
            '{"stale/model":{"context_length":50000,"name":"Stale","pricing":{}}}',
            encoding="utf-8",
        )
        old = time.time() - _MODEL_CACHE_TTL - 60
        import os
        os.utime(cache_path, (old, old))

        with patch("agent.model_metadata.requests.get", side_effect=Exception("Network error")):
            result = fetch_model_metadata(force_refresh=True)

        assert result["stale/model"]["context_length"] == 50000

    @patch("agent.model_metadata.requests.get")
    def test_caches_result(self, mock_get):
        self._reset_cache()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"id": "test/model", "context_length": 99999, "name": "Test"}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result1 = fetch_model_metadata(force_refresh=True)
        assert "test/model" in result1
        assert mock_get.call_count == 1

        result2 = fetch_model_metadata()
        assert "test/model" in result2
        assert mock_get.call_count == 1  # cached



    @patch("agent.model_metadata.requests.get")
    def test_canonical_slug_aliasing(self, mock_get):
        """Models with canonical_slug get indexed under both IDs."""
        self._reset_cache()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{
                "id": "anthropic/claude-3.5-sonnet:beta",
                "canonical_slug": "anthropic/claude-3.5-sonnet",
                "context_length": 200000,
                "name": "Claude 3.5 Sonnet"
            }]
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = fetch_model_metadata(force_refresh=True)
        # Both the original ID and canonical slug should work
        assert "anthropic/claude-3.5-sonnet:beta" in result
        assert "anthropic/claude-3.5-sonnet" in result
        assert result["anthropic/claude-3.5-sonnet"]["context_length"] == 200000





# =========================================================================
# Context probe tiers
# =========================================================================

class TestContextProbeTiers:
    def test_tiers_descending(self):
        for i in range(len(CONTEXT_PROBE_TIERS) - 1):
            assert CONTEXT_PROBE_TIERS[i] > CONTEXT_PROBE_TIERS[i + 1]


class TestGetNextProbeTier:
    def test_from_256k(self):
        assert get_next_probe_tier(256_000) == 128_000




    def test_from_8k_returns_none(self):
        assert get_next_probe_tier(8_000) is None






# =========================================================================
# Error message parsing
# =========================================================================

class TestParseContextLimitFromError:










    @pytest.mark.parametrize("msg,expected", [
        ("max_model_len 32768", 32768),
        ("max_model_len: 32768", 32768),
        ("max_model_len=32768", 32768),
        ("max_model_len (32768)", 32768),
        ("max_model_len is 32768", 32768),
        ("maximum model length 131072", 131072),
        ("maximum model length is 131072", 131072),
        ("maximum model length: 131072", 131072),
    ])
    def test_vllm_delimiter_variants(self, msg, expected):
        """vLLM emits the limit with various delimiters (space/colon/equals/
        paren/'is'). The parser must catch all of them — the original
        space-only patterns silently missed ':', '=', '(' and 'is' forms and
        fell through to None."""
        assert parse_context_limit_from_error(msg) == expected

    def test_get_context_length_from_vllm_max_model_len_error(self):
        from agent.model_metadata import get_context_length_from_provider_error

        msg = (
            "The engine prompt length 90000 exceeds the max_model_len 32768. "
            "Please reduce prompt."
        )
        assert get_context_length_from_provider_error(msg, 131072) == 32768






# =========================================================================
# Persistent context length cache
# =========================================================================

class TestContextLengthCache:


    def test_null_context_lengths_key_returns_empty(self, tmp_path):
        """``context_lengths:`` with no value parses as None — must behave
        like an empty cache instead of crashing every caller (#47135)."""
        cache_file = tmp_path / "cache.yaml"
        cache_file.write_text("context_lengths:\n")
        with patch("agent.model_metadata._get_context_cache_path", return_value=cache_file):
            assert get_cached_context_length("test/model", "http://x") is None
            # save must also survive the null key and repair the file
            save_context_length("test/model", "http://x", 32768)
            assert get_cached_context_length("test/model", "http://x") == 32768



    def test_idempotent_save(self, tmp_path):
        cache_file = tmp_path / "cache.yaml"
        with patch("agent.model_metadata._get_context_cache_path", return_value=cache_file):
            save_context_length("model", "http://x", 32768)
            save_context_length("model", "http://x", 32768)
            with open(cache_file) as f:
                data = yaml.safe_load(f)
            assert len(data["context_lengths"]) == 1




    @patch("agent.model_metadata.fetch_model_metadata")
    def test_cached_value_takes_priority(self, mock_fetch, tmp_path):
        mock_fetch.return_value = {}
        cache_file = tmp_path / "cache.yaml"
        with patch("agent.model_metadata._get_context_cache_path", return_value=cache_file):
            save_context_length("unknown/model", "http://local", 65536)
            assert get_model_context_length("unknown/model", base_url="http://local") == 65536



class TestGrok43StaleCacheGuard:
    """Pre-catalog builds resolved grok-4.3 via the generic 'grok-4' catch-all
    (256,000) and persisted it before the 'grok-4.3' (1M) catalog entry was
    added on 2026-05-15.  The step-1 cache guard must drop that stale value
    and re-resolve to 1M, while leaving correct grok-4 entries (256,000)
    untouched.
    """

    def test_suggests_grok_4_3(self):
        from agent.model_metadata import _model_name_suggests_grok_4_3
        assert _model_name_suggests_grok_4_3("grok-4.3")
        assert _model_name_suggests_grok_4_3("grok-4.3-latest")
        assert _model_name_suggests_grok_4_3("xai/grok-4.3")
        assert not _model_name_suggests_grok_4_3("grok-4")
        assert not _model_name_suggests_grok_4_3("grok-4-fast")
        assert not _model_name_suggests_grok_4_3("grok-4.20")

    def test_stale_grok_4_3_dropped_and_reresolves_to_1m(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import importlib
        import agent.model_metadata as mm
        importlib.reload(mm)
        base = "https://api.x.ai/v1"
        mm.save_context_length("grok-4.3", base, 256_000)
        ctx = mm.get_model_context_length(
            "grok-4.3", base_url=base, api_key="", provider="xai"
        )
        assert ctx == 1_000_000


    def test_grok_4_not_clobbered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import importlib
        import agent.model_metadata as mm
        importlib.reload(mm)
        base = "https://api.x.ai/v1"
        # 256,000 is the CORRECT value for plain grok-4 — guard must not touch it.
        for slug in ("grok-4", "grok-4-0709"):
            mm.save_context_length(slug, base, 256_000)
            ctx = mm.get_model_context_length(
                slug, base_url=base, api_key="", provider="xai"
            )
            assert ctx == 256_000, f"{slug} should stay 256000, got {ctx}"


class TestMoAContextLength:
    """MoA virtual provider resolves context from the aggregator slot, not 256K default."""

    def _write_moa_config(
        self, home, aggregator, custom_providers=None, providers=None
    ):
        import os
        os.makedirs(home, exist_ok=True)
        payload = {
            "moa": {
                "default_preset": "p",
                "presets": {
                    "p": {
                        "enabled": True,
                        "reference_models": [
                            {"provider": "openrouter", "model": "openai/gpt-5.5"}
                        ],
                        "aggregator": aggregator,
                    }
                },
            }
        }
        if custom_providers is not None:
            payload["custom_providers"] = custom_providers
        if providers is not None:
            payload["providers"] = providers
        with open(os.path.join(home, "config.yaml"), "w") as f:
            yaml.safe_dump(payload, f)

    def test_moa_resolves_from_aggregator(self, tmp_path, monkeypatch):
        home = str(tmp_path / ".hermes")
        monkeypatch.setenv("HERMES_HOME", home)
        self._write_moa_config(home, {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"})

        # The MoA preset name + virtual base_url would otherwise fall through to
        # the 256K default; instead it mirrors the aggregator's real window.
        agg_ctx = get_model_context_length(
            "anthropic/claude-opus-4.8", base_url="https://openrouter.ai/api/v1", provider="openrouter"
        )
        moa_ctx = get_model_context_length("p", base_url="http://127.0.0.1/v1", provider="moa")
        assert moa_ctx == agg_ctx


    def test_any_provider_helper_finds_model_across_providers(self):
        from agent import models_dev
        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE):
            assert models_dev.lookup_models_dev_context_any_provider("gpt-5.6-sol") == 1050000
            assert models_dev.lookup_models_dev_context_any_provider("gemini-3-pro") == 1048576
            assert models_dev.lookup_models_dev_context_any_provider("deepseek-v3") == 128000
            assert models_dev.lookup_models_dev_context_any_provider("GPT-5.6-SOL") == 1050000

    def test_any_provider_helper_returns_none_for_unknown(self):
        from agent import models_dev
        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE):
            assert models_dev.lookup_models_dev_context_any_provider("no-such-model-xyz") is None
            assert models_dev.lookup_models_dev_context_any_provider("") is None

    def test_aggregator_provider_resolves_cross_vendor_model(self):
        """INVARIANT: an aggregator provider (not in PROVIDER_TO_MODELS_DEV)
        resolves a cross-vendor model to the SAME window models.dev lists for it.

        Hermetic: the yunwu base_url is only recognized as a KNOWN provider when
        the yunwu plugin is installed (it registers yunwu.ai into _URL_TO_PROVIDER
        at import). That plugin lives outside the repo, so in a clean CI checkout
        the custom-endpoint probe (step 2) would fire against an unreachable
        yunwu.ai and return a probe-tier default BEFORE the aggregator branch.
        Patch _is_known_provider_base_url -> True (exactly what the installed
        plugin makes true at runtime) so step 2 is skipped, and neutralize the
        endpoint probe, making the test independent of plugin-provided state.
        """
        from agent import models_dev
        assert "yunwu" not in models_dev.PROVIDER_TO_MODELS_DEV
        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata._is_known_provider_base_url", return_value=True), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}):
            for model, expected in (("gpt-5.6-sol", 1050000), ("gemini-3-pro", 1048576), ("deepseek-v3", 128000)):
                ctx = get_model_context_length(model, base_url="https://yunwu.ai/v1", provider="yunwu")
                assert ctx == expected, f"{model}: got {ctx}, want {expected} (cross-provider path)"

    def test_stale_aggregator_cache_reconciles_with_cross_provider_registry(
        self, tmp_path, monkeypatch
    ):
        """An upgrade must self-heal values cached before aggregator resolution.

        PR #310 added the correct cross-provider lookup, but a pre-existing
        ``model@aggregator`` row still won at cache step 1 forever. Reproduce the
        live Yunwu failure: claude-fable-5 was stuck at 167K even though fresh
        resolution and the upstream model registry both say 1M.
        """
        import yaml as _yaml
        import agent.model_metadata as mm
        from agent import models_dev

        cache_file = tmp_path / "context_length_cache.yaml"
        stale_key = "claude-fable-5@https://yunwu.ai/v1"
        other_key = "unrelated@https://example.test/v1"
        cache_file.write_text(_yaml.safe_dump({"context_lengths": {
            stale_key: 167000,
            other_key: 128000,
        }}))
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE):
            ctx = mm.get_model_context_length(
                "claude-fable-5",
                base_url="https://yunwu.ai/v1",
                provider="yunwu",
            )

        assert ctx == 1000000
        remaining = (_yaml.safe_load(cache_file.read_text()) or {}).get("context_lengths", {})
        assert remaining[stale_key] == 1000000
        assert remaining[other_key] == 128000

    def test_aggregator_cache_is_not_downgraded_by_registry(
        self, tmp_path, monkeypatch
    ):
        """A transient registry underreport must not poison a correct cache."""
        import yaml as _yaml
        import agent.model_metadata as mm
        from agent import models_dev

        cache_file = tmp_path / "context_length_cache.yaml"
        key = "claude-fable-5@https://yunwu.ai/v1"
        cache_file.write_text(_yaml.safe_dump({"context_lengths": {key: 1000000}}))
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)
        underreported = {
            "anthropic": {
                "models": {"claude-fable-5": {"limit": {"context": 167000}}}
            }
        }

        with patch.object(models_dev, "fetch_models_dev", return_value=underreported):
            ctx = mm.get_model_context_length(
                "claude-fable-5",
                base_url="https://yunwu.ai/v1",
                provider="yunwu",
            )

        assert ctx == 1000000
        remaining = (_yaml.safe_load(cache_file.read_text()) or {}).get("context_lengths", {})
        assert remaining[key] == 1000000

    def test_provider_specific_cache_is_not_reconciled_as_aggregator(
        self, tmp_path, monkeypatch
    ):
        """Dedicated provider limits still win over the native model window."""
        import yaml as _yaml
        import agent.model_metadata as mm
        from agent import models_dev

        cache_file = tmp_path / "context_length_cache.yaml"
        key = "claude-fable-5@https://bedrock-runtime.us-east-1.amazonaws.com"
        cache_file.write_text(_yaml.safe_dump({"context_lengths": {key: 200000}}))
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_file)

        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE) as fetch:
            ctx = mm.get_model_context_length(
                "claude-fable-5",
                base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
                provider="bedrock",
            )

        assert ctx == 200000
        fetch.assert_not_called()

    def test_mapped_provider_does_not_use_cross_provider_guess(self):
        """A mapped provider that misses must NOT get a cross-provider guess.
        Uses a synthetic id present ONLY under a non-mapped vendor in the
        sample AND absent from the hardcoded catalog, so a resolution to its
        333333 window could ONLY come from the (wrongly-fired) cross-provider
        branch. The gate must block it → falls to DEFAULT_FALLBACK_CONTEXT.
        """
        from agent import models_dev
        assert "openai" in models_dev.PROVIDER_TO_MODELS_DEV
        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.models_dev.lookup_models_dev_context", return_value=None):
            ctx = get_model_context_length(
                "zzq-only-on-othervendor-1", base_url="https://api.openai.com/v1", provider="openai"
            )
            assert ctx == DEFAULT_FALLBACK_CONTEXT

    def test_aggregator_resolves_id_absent_from_hardcoded_catalog(self):
        """Positive twin: an aggregator DOES resolve a synthetic cross-vendor id
        that no hardcoded catalog entry would match — proving live models.dev,
        not the family catalog, is the source.
        """
        from agent import models_dev
        with patch.object(models_dev, "fetch_models_dev", return_value=_AGG_MODELS_DEV_SAMPLE), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata._is_known_provider_base_url", return_value=True), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}):
            ctx = get_model_context_length(
                "zzq-only-on-othervendor-1", base_url="https://yunwu.ai/v1", provider="yunwu"
            )
            assert ctx == 333333

class TestAggregatorGreptileFixes(TestMoAContextLength):
    """Regression tests for the two Greptile findings on the aggregator
    cross-provider context path (PR #310).

    Inherits ``_write_moa_config`` from :class:`TestMoAContextLength` — the
    four MoA per-model-context tests below reuse that harness helper.
    """

    def test_minimax_m3_underreport_corrected_on_aggregator(self):
        """P1: models.dev reports 512K for minimax-m3 but the real window is 1M.
        The step-5g guard only fires for MAPPED providers; the aggregator step
        7b must apply the SAME correction, else a yunwu-routed minimax-m3 gets
        the stale 512K instead of the catalog 1M."""
        import agent.model_metadata as mm
        from agent import models_dev
        # models.dev sample with the KNOWN-WRONG 512K minimax-m3 window.
        sample = {
            "minimax": {"models": {"minimax-m3": {"limit": {"context": 512000}}}},
        }
        catalog = mm.DEFAULT_CONTEXT_LENGTHS.get("minimax-m3")
        assert catalog and catalog > 512000, "catalog must carry the correct 1M"
        with patch.object(models_dev, "fetch_models_dev", return_value=sample), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata._is_known_provider_base_url", return_value=True), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}):
            ctx = mm.get_model_context_length(
                "minimax-m3", base_url="https://yunwu.ai/v1", provider="yunwu"
            )
            # Must be corrected UP to the catalog value, not the 512K underreport.
            assert ctx == catalog, f"got {ctx}, want catalog {catalog} (underreport guard)"

    def test_cross_provider_suffix_cloud_fallback(self):
        """P2: some providers store suffixed ids (model:cloud / model-cloud) in
        models.dev while the live API returns the bare name. The cross-provider
        lookup must try the suffix fallback, matching lookup_models_dev_context."""
        from agent import models_dev
        sample = {
            "someprov": {"models": {"kimi-fake-x:cloud": {"limit": {"context": 262144}}}},
        }
        with patch.object(models_dev, "fetch_models_dev", return_value=sample):
            # bare id misses exact+case-insensitive, hits the :cloud suffix pass.
            assert models_dev.lookup_models_dev_context_any_provider("kimi-fake-x") == 262144

    def test_cross_provider_suffix_dash_cloud_fallback(self):
        from agent import models_dev
        sample = {
            "someprov": {"models": {"kimi-fake-y-cloud": {"limit": {"context": 131072}}}},
        }
        with patch.object(models_dev, "fetch_models_dev", return_value=sample):
            assert models_dev.lookup_models_dev_context_any_provider("kimi-fake-y") == 131072


    def test_moa_custom_context_configures_compressor_threshold(
        self, tmp_path, monkeypatch
    ):
        from agent.context_compressor import ContextCompressor

        configured_context = 600_000
        home = str(tmp_path / ".hermes")
        monkeypatch.setenv("HERMES_HOME", home)
        self._write_moa_config(
            home,
            {"provider": "custom:example", "model": "example-model"},
            providers={
                "example": {
                    "api": "http://127.0.0.1:1/v1",
                    "default_model": "example-model",
                    "models": {
                        "example-model": {
                            "context_length": configured_context,
                        },
                    },
                }
            },
        )

        with patch(
            "agent.model_metadata._resolve_endpoint_context_length",
            return_value=None,
        ) as endpoint_probe:
            compressor = ContextCompressor(
                model="p",
                base_url="http://127.0.0.1/v1",
                provider="moa",
                threshold_percent=0.50,
                quiet_mode=True,
            )

        assert compressor.context_length == configured_context
        assert compressor.threshold_tokens == configured_context // 2
        endpoint_probe.assert_not_called()

    def test_moa_resolves_custom_provider_per_model_context(self, tmp_path, monkeypatch):
        home = str(tmp_path / ".hermes")
        monkeypatch.setenv("HERMES_HOME", home)
        self._write_moa_config(
            home,
            {"provider": "custom:example", "model": "example-model"},
            custom_providers=[
                {
                    "name": "example",
                    "base_url": "http://127.0.0.1:1/v1",
                    "model": "example-model",
                    "models": {
                        "example-model": {"context_length": 777_777},
                    },
                }
            ],
        )

        ctx = get_model_context_length(
            "p", base_url="http://127.0.0.1/v1", provider="moa"
        )

        assert ctx == 777_777
    def test_moa_resolves_canonical_provider_per_model_context(
        self, tmp_path, monkeypatch
    ):
        home = str(tmp_path / ".hermes")
        monkeypatch.setenv("HERMES_HOME", home)
        self._write_moa_config(
            home,
            {"provider": "custom:example", "model": "example-model"},
            providers={
                "example": {
                    "api": "http://127.0.0.1:1/v1",
                    "default_model": "example-model",
                    "models": {
                        "example-model": {"context_length": 888_888},
                    },
                }
            },
        )

        with patch(
            "agent.model_metadata._resolve_endpoint_context_length",
            return_value=None,
        ) as endpoint_probe:
            ctx = get_model_context_length(
                "p", base_url="http://127.0.0.1/v1", provider="moa"
            )

        assert ctx == 888_888
        endpoint_probe.assert_not_called()
    def test_moa_preserves_caller_supplied_custom_provider_context(
        self, tmp_path, monkeypatch
    ):
        home = str(tmp_path / ".hermes")
        monkeypatch.setenv("HERMES_HOME", home)
        self._write_moa_config(
            home,
            {"provider": "custom:example", "model": "example-model"},
            providers={
                "example": {
                    "api": "http://127.0.0.1:1/v1",
                    "default_model": "example-model",
                    "models": {"example-model": {}},
                }
            },
        )
        supplied = [
            {
                "name": "example",
                "base_url": "http://127.0.0.1:1/v1",
                "model": "example-model",
                "models": {
                    "example-model": {"context_length": 999_999},
                },
            }
        ]

        with patch(
            "agent.model_metadata._resolve_endpoint_context_length",
            return_value=None,
        ) as endpoint_probe:
            ctx = get_model_context_length(
                "p",
                base_url="http://127.0.0.1/v1",
                provider="moa",
                custom_providers=supplied,
            )

        assert ctx == 999_999
        endpoint_probe.assert_not_called()


# =========================================================================
# Fallback diagnostic logging
# =========================================================================

class TestFallbackWarning:
    """When all 9 detection methods fail, the 10th fallback should log a
    warning so users with small-context models (8K, 32K) don't silently get
    256K and hit hard-to-debug API context-length errors.

    The warning is deduped per (model, base_url) — the fallback result is
    deliberately never cached, so without dedup it would repeat on every
    resolution (e.g. once per gateway message via session hygiene).
    """

    @pytest.fixture(autouse=True)
    def _reset_warned_set(self):
        from agent import model_metadata as mm
        mm._FALLBACK_WARNED.clear()
        yield
        mm._FALLBACK_WARNED.clear()

    @staticmethod
    def _patch_all_lookups():
        from contextlib import ExitStack
        stack = ExitStack()
        for target, value in [
            ("agent.model_metadata.get_cached_context_length", None),
            ("agent.model_metadata.fetch_model_metadata", {}),
            ("agent.model_metadata.fetch_endpoint_model_metadata", {}),
            ("agent.model_metadata._query_ollama_api_show", None),
            ("agent.model_metadata._query_anthropic_context_length", None),
            ("agent.model_metadata._endpoint_scoped_context_length", None),
            ("agent.model_metadata._resolve_endpoint_context_length", None),
            ("agent.models_dev.lookup_models_dev_context", None),
        ]:
            stack.enter_context(patch(target, return_value=value))
        return stack

    def test_warning_emitted_on_fallback(self, caplog):
        import logging

        with self._patch_all_lookups():
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "totally-unknown-model-xyz",
                )

        assert result == DEFAULT_FALLBACK_CONTEXT
        # The warning must mention the model name and the config override hint.
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("totally-unknown-model-xyz" in r.getMessage() for r in warning_msgs)
        assert any("model.context_length" in r.getMessage() for r in warning_msgs)

    def test_warning_fires_once_per_model(self, caplog):
        """Repeated resolutions of the same unknown model warn only once."""
        import logging

        with self._patch_all_lookups():
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                for _ in range(3):
                    get_model_context_length("totally-unknown-model-xyz")

        fallback_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 1

    def test_warning_emitted_on_custom_endpoint_fallback(self, caplog):
        """The sibling step-3b fallback (custom/local endpoint, probes down,
        no catalog match) is the same silent-256K bug class and must warn too."""
        import logging

        with self._patch_all_lookups(), \
             patch("agent.model_metadata._query_local_context_length", return_value=None):
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "totally-unknown-model-xyz",
                    base_url="http://192.168.1.50:8080/v1",
                )

        assert result == DEFAULT_FALLBACK_CONTEXT
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("totally-unknown-model-xyz" in r.getMessage() for r in warning_msgs)
        assert any("model.context_length" in r.getMessage() for r in warning_msgs)

    def test_no_warning_when_cached(self, caplog):
        """No fallback warning when the context length is found in the cache."""
        import logging

        with patch(
            "agent.model_metadata.get_cached_context_length",
            return_value=32_000,
        ):
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "some-model",
                    base_url="http://127.0.0.1:1/v1",
                )

        assert result == 32_000
        fallback_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 0
