"""Tests for probe-cache follow-ups on the #29988/#37595/#50572 salvage.

Covers:
- _query_ollama_api_show TTL caching (positive-only, namespaced key)
- persistent context-cache key normalization (trailing-slash dedup)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Module-level caches must not leak between tests."""
    from agent import model_metadata
    model_metadata._LOCAL_CTX_PROBE_CACHE.clear()
    model_metadata._endpoint_probe_path_cache.clear()
    yield
    model_metadata._LOCAL_CTX_PROBE_CACHE.clear()
    model_metadata._endpoint_probe_path_cache.clear()


def _mock_show_response(ctx=131072):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "model_info": {"llama.context_length": ctx},
        "parameters": "",
    }
    return resp


def _client_mock(resp):
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    return client


class TestOllamaApiShowCaching:
    def test_positive_result_cached_within_ttl(self):
        from agent.model_metadata import _query_ollama_api_show

        client = _client_mock(_mock_show_response(131072))
        with patch("httpx.Client", return_value=client):
            first = _query_ollama_api_show("llama3", "http://127.0.0.1:11434")
            second = _query_ollama_api_show("llama3", "http://127.0.0.1:11434")

        assert first == second == 131072
        assert client.post.call_count == 1  # second call served from cache

    def test_failure_never_memoized(self):
        """A down server must be re-probed on the next call (startup race)."""
        from agent.model_metadata import _query_ollama_api_show

        bad = MagicMock()
        bad.status_code = 404
        client = _client_mock(bad)
        with patch("httpx.Client", return_value=client):
            assert _query_ollama_api_show("llama3", "http://127.0.0.1:11434") is None
            assert _query_ollama_api_show("llama3", "http://127.0.0.1:11434") is None

        assert client.post.call_count == 2  # None was NOT cached

    def test_ttl_expiry_reprobes(self):
        """After the 30s TTL lapses, the next call must hit the network again."""
        from agent import model_metadata
        from agent.model_metadata import _query_ollama_api_show
        import time as _time

        client = _client_mock(_mock_show_response(131072))
        with patch("httpx.Client", return_value=client):
            _query_ollama_api_show("llama3", "http://127.0.0.1:11434")
            # Age the entry past the TTL.
            ((key, (val, _ts)),) = list(model_metadata._LOCAL_CTX_PROBE_CACHE.items())
            model_metadata._LOCAL_CTX_PROBE_CACHE[key] = (
                val, _time.monotonic() - model_metadata._LOCAL_CTX_PROBE_TTL_SECONDS - 1,
            )
            _query_ollama_api_show("llama3", "http://127.0.0.1:11434")

        assert client.post.call_count == 2  # expired entry re-probed

    def test_cache_key_does_not_collide_with_local_ctx_probe(self):
        """The ollama_show namespace must not read _query_local_context_length rows."""
        from agent import model_metadata
        from agent.model_metadata import _query_ollama_api_show
        import time as _time

        # Seed a same-(model,url) entry under the sibling probe's key shape.
        model_metadata._LOCAL_CTX_PROBE_CACHE[("llama3", "http://127.0.0.1:11434")] = (
            999, _time.monotonic(),
        )

        client = _client_mock(_mock_show_response(131072))
        with patch("httpx.Client", return_value=client):
            result = _query_ollama_api_show("llama3", "http://127.0.0.1:11434")

        assert result == 131072  # probed for real, not the sibling's 999
        assert client.post.call_count == 1


class TestDetectLocalServerTypeCache:
    """#29988: detect_local_server_type memoized with a bounded TTL."""

    def _get_client(self, server_type="ollama"):
        ollama_resp = MagicMock()
        ollama_resp.status_code = 200
        ollama_resp.json.return_value = {"models": []}
        miss = MagicMock()
        miss.status_code = 404

        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        def _get(url, *a, **k):
            if url.endswith("/api/tags"):
                return ollama_resp
            return miss

        client.get.side_effect = _get
        return client

    def test_second_call_served_from_cache(self):
        from agent.model_metadata import detect_local_server_type

        client = self._get_client()
        with patch("httpx.Client", return_value=client):
            first = detect_local_server_type("http://127.0.0.1:11434")
            calls_after_first = client.get.call_count
            second = detect_local_server_type("http://127.0.0.1:11434")

        assert first == second == "ollama"
        assert client.get.call_count == calls_after_first  # no new HTTP traffic

    def test_ttl_expiry_allows_server_swap_redetection(self):
        """Stopping Ollama and starting LM Studio on the same port must be
        re-detected once the TTL lapses — the cache is bounded, not
        process-lifetime."""
        from agent import model_metadata
        from agent.model_metadata import detect_local_server_type
        import time as _time

        client = self._get_client()
        with patch("httpx.Client", return_value=client):
            assert detect_local_server_type("http://127.0.0.1:11434") == "ollama"

        # Age the entry past the TTL, then swap the backend behind the URL.
        ((key, (val, _ts)),) = list(model_metadata._endpoint_probe_path_cache.items())
        model_metadata._endpoint_probe_path_cache[key] = (
            val, _time.monotonic() - model_metadata._ENDPOINT_PROBE_TTL_SECONDS - 1,
        )

        lmstudio_resp = MagicMock()
        lmstudio_resp.status_code = 200
        # LM Studio's native listing shape (entries keyed under "models" with
        # LM Studio-specific fields). detect_local_server_type discriminates on
        # this shape, not on a bare 200, so the fixture has to be a payload a
        # real LM Studio would send.
        lmstudio_resp.json.return_value = {
            "models": [
                {
                    "key": "qwen/qwen3-4b",
                    "type": "llm",
                    "state": "loaded",
                    "loaded_instances": [{"config": {"context_length": 8192}}],
                }
            ]
        }
        swap_client = MagicMock()
        swap_client.__enter__ = lambda s: swap_client
        swap_client.__exit__ = MagicMock(return_value=False)

        def _get(url, *a, **k):
            if url.endswith("/api/v1/models"):
                return lmstudio_resp
            miss = MagicMock(); miss.status_code = 404
            return miss

        swap_client.get.side_effect = _get
        with patch("httpx.Client", return_value=swap_client):
            assert detect_local_server_type("http://127.0.0.1:11434") == "lm-studio"


class TestLocalhostIPv4SiblingSites:
    """#37595 widened: every probe helper rewrites localhost→127.0.0.1,
    not just detect_local_server_type."""

    def test_helper_rewrites_all_forms(self):
        from agent.model_metadata import _localhost_to_ipv4

        assert _localhost_to_ipv4("http://localhost:1234/v1") == "http://127.0.0.1:1234/v1"
        assert _localhost_to_ipv4("http://localhost/v1") == "http://127.0.0.1/v1"
        assert _localhost_to_ipv4("http://localhost") == "http://127.0.0.1"
        # Non-localhost passes through untouched.
        assert _localhost_to_ipv4("http://192.168.1.10:8080") == "http://192.168.1.10:8080"
        assert _localhost_to_ipv4("https://api.openai.com/v1") == "https://api.openai.com/v1"
        assert _localhost_to_ipv4("") == ""

    def test_rewrite_is_host_only_not_substring(self):
        """A URL that merely EMBEDS 'http://localhost' in its path/query must
        not be corrupted — only the URL's own host is rewritten."""
        from agent.model_metadata import _localhost_to_ipv4

        proxied = "https://proxy.example.com/route?upstream=http://localhost:11434"
        assert _localhost_to_ipv4(proxied) == proxied
        # Host must be a full label: localhost.example.com is NOT localhost.
        assert _localhost_to_ipv4("http://localhost.example.com/v1") == (
            "http://localhost.example.com/v1"
        )

    def test_ollama_api_show_probes_ipv4(self):
        from agent.model_metadata import _query_ollama_api_show

        client = _client_mock(_mock_show_response(131072))
        with patch("httpx.Client", return_value=client):
            _query_ollama_api_show("llama3", "http://localhost:11434")

        assert client.post.call_args[0][0].startswith("http://127.0.0.1:11434")

    def test_query_ollama_num_ctx_probes_ipv4(self):
        from agent.model_metadata import query_ollama_num_ctx

        client = _client_mock(_mock_show_response(131072))
        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"), \
             patch("httpx.Client", return_value=client):
            query_ollama_num_ctx("llama3", "http://localhost:11434")

        assert client.post.call_args[0][0].startswith("http://127.0.0.1:11434")


class TestContextCacheKeyNormalization:
    def test_trailing_slash_variants_share_one_entry(self, tmp_path, monkeypatch):
        from agent import model_metadata

        monkeypatch.setattr(
            model_metadata, "_get_context_cache_path",
            lambda: tmp_path / "context_lengths.yaml",
        )

        model_metadata.save_context_length("m1", "http://host/v1/", 200_000)
        # Both slash variants resolve to the same row.
        assert model_metadata.get_cached_context_length("m1", "http://host/v1") == 200_000
        assert model_metadata.get_cached_context_length("m1", "http://host/v1/") == 200_000

        cache = model_metadata._load_context_cache()
        assert list(cache.keys()) == ["m1@http://host/v1"]

    def test_legacy_unnormalized_row_still_honored(self, tmp_path, monkeypatch):
        """Rows written pre-normalization (trailing slash in key) must not force a re-probe."""
        import yaml
        from agent import model_metadata

        path = tmp_path / "context_lengths.yaml"
        monkeypatch.setattr(model_metadata, "_get_context_cache_path", lambda: path)
        path.write_text(yaml.dump({"context_lengths": {"m1@http://host/v1/": 128_000}}))

        assert model_metadata.get_cached_context_length("m1", "http://host/v1/") == 128_000

    def test_legacy_slashed_row_found_with_normalized_caller(self, tmp_path, monkeypatch):
        """Reverse migration direction: old row has the slash, current runtime
        passes the normalized no-slash URL — must still hit, not re-probe."""
        import yaml
        from agent import model_metadata

        path = tmp_path / "context_lengths.yaml"
        monkeypatch.setattr(model_metadata, "_get_context_cache_path", lambda: path)
        path.write_text(yaml.dump({"context_lengths": {"m1@http://host/v1/": 128_000}}))

        assert model_metadata.get_cached_context_length("m1", "http://host/v1") == 128_000

    def test_invalidate_clears_both_key_shapes(self, tmp_path, monkeypatch):
        import yaml
        from agent import model_metadata

        path = tmp_path / "context_lengths.yaml"
        monkeypatch.setattr(model_metadata, "_get_context_cache_path", lambda: path)
        path.write_text(yaml.dump({"context_lengths": {
            "m1@http://host/v1": 128_000,
            "m1@http://host/v1/": 64_000,
        }}))

        model_metadata._invalidate_cached_context_length("m1", "http://host/v1/")
        cache = model_metadata._load_context_cache()
        assert "m1@http://host/v1" not in cache
        assert "m1@http://host/v1/" not in cache

    def test_invalidate_with_normalized_caller_clears_legacy_row(self, tmp_path, monkeypatch):
        """Reverse direction: invalidating with the no-slash URL must also
        drop a legacy slashed row, or the next lookup resurrects stale data."""
        import yaml
        from agent import model_metadata

        path = tmp_path / "context_lengths.yaml"
        monkeypatch.setattr(model_metadata, "_get_context_cache_path", lambda: path)
        path.write_text(yaml.dump({"context_lengths": {"m1@http://host/v1/": 64_000}}))

        model_metadata._invalidate_cached_context_length("m1", "http://host/v1")
        assert model_metadata.get_cached_context_length("m1", "http://host/v1") is None
        assert model_metadata.get_cached_context_length("m1", "http://host/v1/") is None

    def test_invalidate_also_drops_in_memory_probe_entries(self, tmp_path, monkeypatch):
        """Disk invalidation must clear the in-memory TTL rows too, or the
        next resolution inside the TTL window re-persists the stale value."""
        import time as _time
        from agent import model_metadata

        path = tmp_path / "context_lengths.yaml"
        monkeypatch.setattr(model_metadata, "_get_context_cache_path", lambda: path)

        now = _time.monotonic()
        model_metadata._LOCAL_CTX_PROBE_CACHE[("m1", "http://host/v1")] = (999, now)
        model_metadata._LOCAL_CTX_PROBE_CACHE[("ollama_show", "m1", "http://host/v1")] = (999, now)

        model_metadata._invalidate_cached_context_length("m1", "http://host/v1")

        assert ("m1", "http://host/v1") not in model_metadata._LOCAL_CTX_PROBE_CACHE
        assert ("ollama_show", "m1", "http://host/v1") not in model_metadata._LOCAL_CTX_PROBE_CACHE


class TestLMStudioDetectionRequiresNativePayload:
    """A bare 200 on /api/v1/models is not evidence of LM Studio.

    Other OpenAI-compatible local servers serve that path too (e.g. a loopback
    proxy exposing it for model-name validation). Misdetecting one sends the
    caller into the LM Studio metadata parser, which reads ``payload["models"]``
    — a key the OpenAI listing envelope does not have — so ALL advertised
    metadata is discarded and the caller falls back to a probe-tier default.
    """

    def _detect(self, payload):
        from agent.model_metadata import detect_local_server_type

        hit = MagicMock()
        hit.status_code = 200
        hit.json.return_value = payload
        hit.text = ""
        miss = MagicMock()
        miss.status_code = 404
        miss.text = ""
        miss.json.return_value = {}

        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)
        client.get.side_effect = (
            lambda url, *a, **k: hit if url.endswith("/api/v1/models") else miss
        )
        with patch("httpx.Client", return_value=client):
            return detect_local_server_type("http://127.0.0.1:8080/v1")

    def test_openai_listing_envelope_is_not_lm_studio(self):
        """The standard OpenAI listing shape must never classify as LM Studio,
        whatever its entries carry."""
        assert self._detect(
            {"object": "list", "data": [{"id": "some-model", "object": "model"}]}
        ) != "lm-studio"
        # Even when the entries carry rich metadata, the envelope decides.
        assert self._detect(
            {
                "object": "list",
                "data": [{"id": "m", "object": "model", "context_length": 1_000_000}],
            }
        ) != "lm-studio"

    def test_lm_studio_native_payload_is_still_detected(self):
        """The feature must survive the fix: LM Studio's own shapes still
        classify, including an idle server with nothing loaded."""
        assert self._detect(
            {
                "models": [
                    {
                        "key": "qwen/qwen3-4b",
                        "loaded_instances": [{"config": {"context_length": 8192}}],
                    }
                ]
            }
        ) == "lm-studio"
        # Idle LM Studio: running, no model loaded.
        assert self._detect({"models": []}) == "lm-studio"
        # `type` + `state` together are an acceptable weaker signal.
        assert self._detect(
            {"data": [{"id": "m", "type": "llm", "state": "loaded"}]}
        ) == "lm-studio"

    def test_unparseable_or_ambiguous_payloads_fail_closed(self):
        """Fail closed, never open: an unrecognised body must not classify as
        LM Studio, so the caller continues to the OpenAI-compatible path."""
        assert self._detect("not-a-dict") != "lm-studio"
        assert self._detect({}) != "lm-studio"
        # An empty `data` list is indistinguishable from an idle OpenAI server.
        assert self._detect({"data": []}) != "lm-studio"

    def test_detection_agrees_with_what_the_lm_studio_parser_can_read(self):
        """Contract between the detector and its consumer: this module's LM
        Studio parsers read ``payload["models"]``. Classifying a payload as
        LM Studio when that key is absent guarantees the parser yields nothing,
        which is exactly the bug. So a NON-empty payload may only be called
        LM Studio if the parser could actually extract a model id from it."""
        from agent.model_metadata import _is_lmstudio_models_payload

        def parser_can_read(payload):
            # Mirrors fetch_endpoint_model_metadata's LM Studio branch.
            if not isinstance(payload, dict):
                return False
            return any(
                isinstance(m, dict) and (m.get("key") or m.get("id"))
                for m in payload.get("models", []) or []
            )

        payloads = [
            {"object": "list", "data": [{"id": "m", "object": "model"}]},
            {"data": [{"id": "m", "object": "model", "context_length": 1_000_000}]},
            {"models": [{"key": "qwen/qwen3-4b", "loaded_instances": []}]},
            {"models": [{"id": "m", "max_context_length": 4096}]},
            {"data": [{"id": "m", "type": "llm", "state": "loaded"}]},
            {},
            {"models": []},
        ]
        for payload in payloads:
            resp = MagicMock()
            resp.json.return_value = payload
            classified = _is_lmstudio_models_payload(resp)
            if classified and payload.get("models"):
                assert parser_can_read(payload), (
                    f"classified as LM Studio but the LM Studio parser reads "
                    f"nothing from it: {payload}"
                )


class TestOpenAICompatProxyMetadataSurvivesDetection:
    """End-to-end: an OpenAI-compatible server's advertised context window must
    reach the caller. The LM Studio branch of fetch_endpoint_model_metadata
    returns early, so a misdetection discards the payload outright rather than
    falling through to the working OpenAI parser."""

    def test_advertised_context_window_is_not_discarded(self):
        from agent import model_metadata

        payload = {
            "object": "list",
            "data": [
                {"id": "big-ctx-model", "object": "model", "context_length": 1_000_000}
            ],
        }
        model_metadata._endpoint_model_metadata_cache.clear()
        model_metadata._endpoint_model_metadata_cache_time.clear()

        probe = MagicMock()
        probe.status_code = 200
        probe.json.return_value = payload
        probe.text = ""
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)
        client.get.side_effect = lambda url, *a, **k: probe

        def _requests_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = payload
            resp.raise_for_status = lambda: None
            return resp

        with patch("httpx.Client", return_value=client), patch(
            "requests.get", side_effect=_requests_get
        ):
            metadata = model_metadata.fetch_endpoint_model_metadata(
                "http://127.0.0.1:8080/v1", force_refresh=True
            )

        assert metadata.get("big-ctx-model", {}).get("context_length") == 1_000_000
