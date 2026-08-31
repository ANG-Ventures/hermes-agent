"""Phase 1 Task 1.4: transient-narration gate wired into the live capture router.

Drives the REAL CaptureRouter with an injected fake extractor (no network), asserts:
  - a transient world candidate is DROPPED and logged to _dropped-log.jsonl (RC2)
  - a durable world candidate is KEPT (staged)
  - toggling the filter OFF keeps BOTH (A/B escape hatch)
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # plugins/memory/mem0 on path
import capture_router as cr


class _FakeExtractor:
    """Returns fixed candidates per pass so route_turn never touches the network."""
    def __init__(self, world_cands):
        self._world = world_cands

    def extract(self, system_prompt, user, assistant):
        # world prompt contains the word 'world'; prefs prompt otherwise. Cheap discriminator.
        is_world = 'world' in (system_prompt or '').lower()
        return {
            "candidates": self._world if is_world else [],
            "usage": {}, "latency": 0.0, "provider": "test",
        }


def _router(tmp_path, world_cands, *, enabled=True):
    return cr.CaptureRouter(
        extractor=_FakeExtractor(world_cands),
        prefs_prompt="prefs pass",
        world_prompt="world pass",
        staging_dir=str(tmp_path / "staged"),
        transient_filter_enabled=enabled,
    )


TRANSIENT = {"content": "PR #15 landed at 9a51526", "class": "event", "confidence": 0.9}
DURABLE = {"content": "The mem0 self-host API uses the X-API-Key header", "class": "world_entity", "confidence": 0.9}


def test_transient_dropped_durable_kept(tmp_path, monkeypatch):
    # Inject a deterministic stub filter so this WIRING test is self-contained and does NOT depend
    # on ~/gbrain/scripts being present (Greptile #407: hard-asserting the real import breaks CI
    # runners without the gbrain filter). The production import stays fail-open in capture_router.
    monkeypatch.setattr(cr, "_is_transient", lambda text, **kw: text.startswith("PR #15 landed"))

    r = _router(tmp_path, [TRANSIENT, DURABLE], enabled=True)
    result = r.route_turn("u", "a", turn_id="t1", session="s1", ts="2026-07-20T00:00:00+00:00", stage=False)

    kept = [f["content"] for f in result["world_facts"]]
    assert DURABLE["content"] in kept
    assert TRANSIENT["content"] not in kept
    assert r.stats["transient_dropped"] == 1

    # Greptile #407 P1: in staging mode the drop-log lands in the STAGING dir, NOT the brain inbox.
    log = tmp_path / "staged" / "_dropped-log.jsonl"
    assert log.exists()
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["reason"] == "transient_narration"
    assert rows[0]["text_prefix"].startswith("PR #15 landed")


def test_toggle_off_keeps_both(tmp_path):
    r = _router(tmp_path, [TRANSIENT, DURABLE], enabled=False)
    result = r.route_turn("u", "a", turn_id="t2", session="s2", stage=False)
    kept = [f["content"] for f in result["world_facts"]]
    assert TRANSIENT["content"] in kept and DURABLE["content"] in kept
    assert r.stats["transient_dropped"] == 0
