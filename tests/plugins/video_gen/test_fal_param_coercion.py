"""FAL video plugin: params ``_build_payload`` drops or snaps are surfaced in ``extra.coerced``."""

from __future__ import annotations

from unittest.mock import Mock


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
