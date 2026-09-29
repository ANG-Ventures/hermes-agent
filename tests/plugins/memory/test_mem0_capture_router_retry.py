"""k122: a completed Arm-B staging write survives a crash/retry across midnight."""
import os
import stat
from pathlib import Path

import pytest

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


def test_staged_lookup_matches_turn_id_literally_not_as_a_glob(tmp_path):
    """turn_id 'turn[12]' must not pick up another turn's 'turn1.md' as its own."""
    extracted = []

    class _Router:
        _stage_world_facts = CaptureRouter._stage_world_facts
        route_turn = CaptureRouter.route_turn
        _write = staticmethod(CaptureRouter._default_write)
        def __init__(self):
            self._staging_dir = str(tmp_path / "staged")
            self._brain_inbox = str(tmp_path / "inbox")
            self._staging_mode = True
            self._now = lambda: __import__("datetime").datetime.now()
        def two_pass_extract(self, *args):
            extracted.append(args)
            raise RuntimeError("extraction reached")

        stats = {"fallback_passes": 0, "extract_errors": 0, "prefs_seen": 0,
                 "world_deduped": 0, "world_staged": 0, "turns_routed": 0}

    router = _Router()
    other = router._stage_world_facts([{"class": "world_entity", "content": "other"}],
                                       router._staging_dir, turn_id="turn1", session="s", ts=None)
    try:
        result = router.route_turn("user", "assistant", turn_id="turn[12]", session="s")
    except RuntimeError:
        result = {}
    assert extracted, "a different turn's staged file was taken as this turn's"
    assert result.get("staged_path") != other


def test_crash_mid_staging_write_leaves_no_file_at_the_final_path(tmp_path, monkeypatch):
    """FleetReview #1371 d82676bc: route_turn treats an existing <turn_id>.md as a completed
    stage, so a write that dies part-way must not leave a truncated file under that name."""
    import builtins
    import os
    target = tmp_path / "staged" / "2026-09-28" / "turn-3.md"
    real_open = builtins.open

    class _Dies:
        def __init__(self, fh):
            self._fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self._fh.close()
        def fileno(self):
            return self._fh.fileno()
        def write(self, content):
            self._fh.write(content[: len(content) // 2])
            self._fh.flush()
            raise OSError("process died mid-write")

    real_fdopen = os.fdopen
    # the write may go through open() or os.fdopen() (mkstemp temp file); make both die mid-write
    monkeypatch.setattr(builtins, "open", lambda path, *a, **k: _Dies(real_open(path, *a, **k)))
    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: _Dies(real_fdopen(fd, *a, **k)))
    try:
        CaptureRouter._default_write(str(target), "---\nclass: world_entity\n---\n- fact\n")
    except OSError:
        pass
    monkeypatch.undo()
    assert not target.exists()
    assert os.listdir(target.parent) == []


def test_staging_write_publishes_full_content(tmp_path):
    target = tmp_path / "staged" / "d" / "turn-4.md"
    CaptureRouter._default_write(str(target), "full content\n")
    assert target.read_text(encoding="utf-8") == "full content\n"
    assert [p.name for p in target.parent.iterdir()] == ["turn-4.md"]


@pytest.mark.parametrize("umask", [0o077, 0o022])
def test_staging_write_mode_honours_umask(tmp_path, umask):
    # the staged file must get exactly the mode open()+umask would give, never a fixed widened mode
    target = tmp_path / "staged" / "d" / f"turn-{umask:o}.md"
    old = os.umask(umask)
    try:
        CaptureRouter._default_write(str(target), "x\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~umask
