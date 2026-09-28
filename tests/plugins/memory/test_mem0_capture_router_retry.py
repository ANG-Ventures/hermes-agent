"""k122: a completed Arm-B staging write survives a crash/retry across midnight."""
from pathlib import Path
from plugins.memory.mem0.capture_router import CaptureRouter

def test_retried_route_preserves_staged_facts_across_dates(tmp_path):
    """If the first route staged facts then the process died before mark_done,
    the retry must not re-extract/overwrite, even on the next calendar day."""
    from datetime import datetime, timezone

    class _Router:
        def __init__(self):
            self._staging_dir = str(tmp_path / "staged")
            self._staging_mode = True
            self._brain_inbox = str(tmp_path / "inbox")
            self._now = lambda: datetime(2026, 9, 27, tzinfo=timezone.utc)
            self.calls = 0
            self._write = CaptureRouter._default_write

        _stage_world_facts = CaptureRouter._stage_world_facts
        route_turn = CaptureRouter.route_turn

        def two_pass_extract(self, *args):
            self.calls += 1
            return {"prefs": {"candidates": []}, "world": {"candidates": [
                {"class": "world_entity", "content": f"version-{self.calls}"}]}}

        _classify = staticmethod(lambda c, allowed: c)
        stats = {"fallback_passes": 0, "extract_errors": 0, "prefs_seen": 0,
                 "world_deduped": 0, "world_staged": 0, "turns_routed": 0}

    router = _Router()
    # Real route with deterministic single fact; route_turn may require further fields,
    # so the first staged file is created via its own storage method.
    original = router._stage_world_facts([{"class": "world_entity", "content": "original"}],
                                          router._staging_dir, turn_id="turn-1", session="s", ts=None)
    router._now = lambda: datetime(2026, 9, 28, tzinfo=timezone.utc)
    result = router.route_turn("user", "assistant", turn_id="turn-1", session="s")
    assert result["staged_path"] == original
    assert router.calls == 0
    assert "original" in open(original, encoding="utf-8").read()
    assert list((tmp_path / "staged").rglob("turn-1.md")) == [Path(original)]


def test_retry_does_not_restage_when_destination_mode_changes(tmp_path):
    class _Router:
        _stage_world_facts = CaptureRouter._stage_world_facts
        route_turn = CaptureRouter.route_turn
        _write = staticmethod(CaptureRouter._default_write)
        def __init__(self):
            self._staging_dir = str(tmp_path / "staged")
            self._brain_inbox = str(tmp_path / "inbox")
            self._staging_mode = True
            self._now = lambda: __import__("datetime").datetime.now()
            self.calls = 0
        def two_pass_extract(self, *args):
            self.calls += 1
            raise AssertionError("already staged: must not re-extract")

    router = _Router()
    original = router._stage_world_facts([{"class": "world_entity", "content": "original"}],
                                          router._staging_dir, turn_id="turn-2", session="s", ts=None)
    router._staging_mode = False
    result = router.route_turn("user", "assistant", turn_id="turn-2", session="s")
    assert result["staged_path"] == original
    assert result["destination"] == "staging"
    assert list((tmp_path / "inbox").rglob("turn-2.md")) == []
