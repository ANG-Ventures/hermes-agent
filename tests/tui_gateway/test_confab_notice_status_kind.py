"""The LIVE half of the confab-notice contract on the gateway status channel.

``_emit_status`` sends every lifecycle message through ``status_callback``
with kind ``lifecycle``. The desktop status handler renders nothing for
generic lifecycle text, so a confab notice emitted that way is silently
invisible on desktop — half of the "out of band must not mean invisible"
contract lost (FleetReview P1, PR #764).

``_status_update`` therefore re-tags the notice to its own kind, exactly as it
already does for auto-compaction. These tests pin that re-tag and, just as
importantly, pin that NOTHING ELSE changed kind.
"""

from __future__ import annotations

import pytest

from agent.confab_notice import CONFAB_NOTICE_TEXT
import tui_gateway.server as server


@pytest.fixture()
def emitted(monkeypatch):
    """Capture what _status_update puts on the wire."""
    events: list = []

    def _fake_emit(event, sid, payload):
        events.append((event, sid, payload))

    monkeypatch.setattr(server, "_emit", _fake_emit)
    return events


class TestConfabNoticeGetsItsOwnKind:
    def test_lifecycle_confab_notice_is_retagged(self, emitted):
        server._status_update("sess-1", "lifecycle", CONFAB_NOTICE_TEXT)

        assert len(emitted) == 1
        event, sid, payload = emitted[0]
        assert event == "status.update"
        assert sid == "sess-1"
        assert payload["kind"] == "confab_notice", (
            "the notice is still a generic 'lifecycle' status — the desktop "
            "status handler ignores those, so the live warning is invisible"
        )

    def test_the_text_survives_the_retag(self, emitted):
        """A driver renders payload['text']; losing it loses the warning."""
        server._status_update("sess-1", "lifecycle", CONFAB_NOTICE_TEXT)

        assert "onfabulation" in emitted[0][2]["text"]

    def test_notice_embedded_in_a_longer_lifecycle_line_is_still_caught(self, emitted):
        server._status_update("sess-1", "lifecycle", f"[agent] {CONFAB_NOTICE_TEXT}")

        assert emitted[0][2]["kind"] == "confab_notice"


class TestOtherStatusesAreUnaffected:
    """Non-vacuity: the re-tag must be narrow, not a blanket relabel."""

    def test_an_ordinary_lifecycle_status_stays_lifecycle(self, emitted):
        server._status_update("sess-1", "lifecycle", "switching to fallback model")

        assert emitted[0][2]["kind"] == "lifecycle"

    def test_compaction_still_retags_to_compacting(self, emitted):
        from agent.conversation_compression import COMPACTION_STATUS_MARKER

        server._status_update("sess-1", "lifecycle", f"{COMPACTION_STATUS_MARKER} now")

        assert emitted[0][2]["kind"] == "compacting"

    def test_a_non_lifecycle_kind_is_passed_through(self, emitted):
        server._status_update("sess-1", "warn", CONFAB_NOTICE_TEXT)

        assert emitted[0][2]["kind"] == "warn"

    def test_empty_body_emits_nothing(self, emitted):
        server._status_update("sess-1", "lifecycle", "   ")

        assert emitted == []
