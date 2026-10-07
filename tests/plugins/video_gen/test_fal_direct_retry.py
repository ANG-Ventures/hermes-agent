"""FAL video plugin: direct-path retry under one idempotency key, and surfaced param coercion."""

from __future__ import annotations

from unittest.mock import Mock

import pytest


class _HTTPError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = Mock(status_code=status, headers=headers or {})


class TestDirectRetry:
    """Direct-FAL path (FAL_KEY): transient submit failures retry under ONE idempotency key."""

    @pytest.fixture
    def direct(self, monkeypatch):
        from plugins.video_gen import fal as fal_plugin
        from tools import fal_common
        sent, sleeps, outcomes = [], [], []

        class FakeClient:
            def submit(self, endpoint, arguments=None, headers=None):
                sent.append(dict(headers))
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return Mock(request_id="req-1", get=lambda: {"video": {"url": "https://fake/out.mp4"}})

        monkeypatch.setattr(fal_plugin, "_load_fal_client", lambda: FakeClient())
        monkeypatch.setattr(fal_plugin, "_resolve_managed_fal_video_gateway", lambda: None)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
        monkeypatch.setattr(fal_common.call_direct_fal_with_retry, "__kwdefaults__",
                            {"sleep": sleeps.append, "rand": lambda lo, hi: hi})
        return fal_plugin, sent, sleeps, outcomes

    def test_429_retry_after_waits_that_long_and_reuses_the_key(self, direct):
        fal_plugin, sent, sleeps, outcomes = direct
        outcomes[:] = [_HTTPError(429, {"Retry-After": "3"}), "ok"]
        handle = fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert handle.request_id == "req-1"
        assert handle.get()["video"]["url"] == "https://fake/out.mp4"
        assert sum(sleeps) == pytest.approx(3.0)
        assert len(sent) == 2 and len({h["x-idempotency-key"] for h in sent}) == 1

    def test_503_three_times_then_success_sends_four_with_one_key(self, direct):
        fal_plugin, sent, sleeps, outcomes = direct
        outcomes[:] = [_HTTPError(503), _HTTPError(503), _HTTPError(503), "ok"]
        fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert len(sent) == 4 and len({h["x-idempotency-key"] for h in sent}) == 1
        assert sum(sleeps) == pytest.approx(0.5 + 1.0 + 2.0)  # exp backoff, jitter pinned at 100%

    def test_400_is_not_retried(self, direct):
        fal_plugin, sent, sleeps, outcomes = direct
        outcomes[:] = [_HTTPError(400), "ok"]
        with pytest.raises(_HTTPError):
            fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert len(sent) == 1 and sleeps == []

    def test_submit_read_timeout_is_not_resent_but_connect_error_is(self, direct):
        import httpx
        fal_plugin, sent, sleeps, outcomes = direct
        outcomes[:] = [httpx.ReadTimeout("read"), "ok"]  # request may have been accepted -> resend could double-bill
        with pytest.raises(httpx.ReadTimeout):
            fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert len(sent) == 1
        sent.clear()
        outcomes[:] = [httpx.ConnectError("refused"), "ok"]  # never reached FAL -> safe to resend
        fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert len(sent) == 2

    def test_interrupt_stops_the_wait(self, direct, monkeypatch):
        fal_plugin, sent, sleeps, outcomes = direct
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)
        outcomes[:] = [_HTTPError(429, {"Retry-After": "30"}), "ok"]
        with pytest.raises(RuntimeError, match="interrupted"):
            fal_plugin._submit_fal_video_request("fal-ai/x", {"prompt": "p"})
        assert len(sent) == 1 and sleeps == []


class TestCoercionSurfaced:
    def _generate(self, monkeypatch, **kwargs):
        from plugins.video_gen import fal as fal_plugin
        captured = {}

        def submit(endpoint, payload):
            captured["payload"] = payload
            return Mock(request_id="r", get=lambda: {"video": {"url": "https://fake/out.mp4"}})

        monkeypatch.setattr(fal_plugin, "_fal_video_available", lambda: True)
        monkeypatch.setattr(fal_plugin, "_load_fal_client", lambda: object())
        monkeypatch.setattr(fal_plugin, "_submit_fal_video_request", submit)
        return fal_plugin.FALVideoGenProvider().generate("x", **kwargs), captured["payload"]

    def test_dropped_resolution_is_reported_and_not_sent(self, monkeypatch):
        result, payload = self._generate(monkeypatch, model="pixverse-v6", resolution="4k")
        assert "resolution" not in payload
        assert result["coerced"]["resolution"] == {"requested": "4k", "applied": None}

    def test_snapped_duration_is_reported(self, monkeypatch):
        from plugins.video_gen.fal import FAL_FAMILIES
        result, payload = self._generate(monkeypatch, model="veo3.1", duration=5)
        coerced = result["coerced"]["duration"]
        assert coerced["requested"] == 5
        assert coerced["applied"] in FAL_FAMILIES["veo3.1"]["duration_enum"]
        assert payload["duration"] == f"{coerced['applied']}s"
        assert "resolution" not in result["coerced"] and "aspect_ratio" not in result["coerced"]
