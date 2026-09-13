"""Regression coverage for CLI async-delegation completion ownership."""

import queue

import pytest

from cli import HermesCLI


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            calls.append((session_key, owns_event(event)))
            return [(event, "completion payload")]

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    assert cli._pending_input.get_nowait() == "completion payload"
    assert claimed == [(event, "cli-idle")]
    assert completed == [(event, "claim-token")]


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


@pytest.mark.parametrize("failures", [1, 100])
def test_cli_receipt_failure_preserves_accepted_input_and_siblings(monkeypatch, caplog, failures):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    events = [{"type": "async_delegation", "event_id": "receipt-test"}, {"type": "completion"}]
    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        type("Registry", (), {"drain_notifications": lambda self, **kw: list(zip(events, ["first", "sibling"]))})(),
    )
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda *a: "claim")
    attempts = []

    def receipt(evt, claim):
        attempts.append(evt)
        if evt is events[0] and attempts.count(evt) <= failures:
            raise OSError("receipt storage unavailable")

    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", receipt)
    cli._drain_process_notifications("cli-idle")
    assert list(cli._pending_input.queue) == ["first", "sibling"]
    assert attempts.count(events[0]) == (2 if failures == 1 else 3)
    assert "receipt" in caplog.text.lower()


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )
