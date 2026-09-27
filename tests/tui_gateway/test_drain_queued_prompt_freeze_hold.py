"""C6 (FleetReview backfill, #1035): under a ``freeze`` checkout hold the
queued next-turn prompt is KEPT, never popped and discarded.

Before: ``_drain_queued_prompt`` popped the envelope, ``_run_prompt_submit``
refused under the hold and returned False, and the caller ignored it -- the
user's queued message was gone although the doc calls it "deferred"."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import tui_gateway.server as srv


class _Gate:
    def __init__(self, refusal):
        self.refusal = refusal

    def check(self, internal=False):
        return self.refusal


def _session():
    return {"history_lock": threading.Lock(), "running": False,
            "queued_prompt": {"text": "follow-up the user typed"},
            "queued_prompts": [{"text": "second"}]}


def test_freeze_hold_keeps_the_queued_prompt(monkeypatch):
    submitted = []
    monkeypatch.setattr(srv, "_checkout_gate_ref", _Gate(SimpleNamespace(reason="freeze")))
    monkeypatch.setattr(srv, "_run_prompt_submit", lambda *a, **k: submitted.append(a) or False)
    monkeypatch.setattr(srv, "_session_uses_compute_host", lambda s: False)
    session = _session()
    assert srv._drain_queued_prompt("r", "sid", session) is True
    assert submitted == []
    assert session["queued_prompt"] == {"text": "follow-up the user typed"}
    assert session["queued_prompts"] == [{"text": "second"}]
    assert session["running"] is False


def test_open_gate_still_dispatches(monkeypatch):
    submitted = []
    monkeypatch.setattr(srv, "_checkout_gate_ref", _Gate(None))
    monkeypatch.setattr(srv, "_run_prompt_submit", lambda *a, **k: submitted.append(a[3]) or True)
    monkeypatch.setattr(srv, "_session_uses_compute_host", lambda s: False)
    session = _session()
    srv._drain_queued_prompt("r", "sid", session)
    assert submitted == ["follow-up the user typed"]
    assert session["queued_prompt"] == {"text": "second"}
