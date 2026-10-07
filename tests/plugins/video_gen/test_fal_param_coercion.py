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


class TestSentDurationParse:
    """The applied duration is read from the sent value; only the unit suffix is stripped (5.0 was read as 50)."""

    def test_numeric_and_suffixed_durations_keep_their_value(self):
        from plugins.video_gen.fal import _sent_duration_seconds
        assert [_sent_duration_seconds(v) for v in (5, 5.0, "5", "5s", "8s", 5.5, "5.5s")] == [5, 5, 5, 5, 8, 5.5, 5.5]
        assert _sent_duration_seconds(None) is None

    def test_float_or_decimal_sent_duration_is_not_reported_as_coerced(self):
        from plugins.video_gen.fal import _coerced_params
        for sent in (5, 5.0, "5", "5s"):
            assert _coerced_params({"duration": sent}, duration=5, aspect_ratio="", resolution="") == {}
        assert _coerced_params({"duration": "5.5s"}, duration=5, aspect_ratio="", resolution="") == {
            "duration": {"requested": 5, "applied": 5.5}}

    def test_success_response_duration_matches_sent_float(self, monkeypatch):
        from plugins.video_gen import fal as fal_plugin
        real_build = fal_plugin._build_payload

        def build(*args, **kwargs):  # an int-duration family whose payload carries a float
            payload = real_build(*args, **kwargs)
            payload["duration"] = float(payload["duration"])
            return payload

        monkeypatch.setattr(fal_plugin, "_build_payload", build)
        result, payload = TestCoercionSurfaced()._generate(monkeypatch, model="minimax-h3", duration=6)
        assert payload["duration"] == 6.0
        assert result["duration"] == 6
        assert "duration" not in result.get("coerced", {})
