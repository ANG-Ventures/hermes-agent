"""Gateway/TUI rendering of the out-of-band confab notice.

Out of band must not mean invisible: a reloaded session has to still show a
reader that this assistant turn carried a confirmed self-confabulation catch.
``_history_to_messages`` is the gateway/desktop history serializer, so it is
the surface that must forward both presentation fields — with the request ID
preserved so historical triage can join back to bpx forensics (spec test 5).
"""

from __future__ import annotations

import json

from agent.confab_notice import CONFAB_NOTICE_DISPLAY_KIND, CONFAB_NOTICE_KEY
from tui_gateway.server import _history_to_messages

NOTICE = {
    "version": 1,
    "kind": "scaffold_confab_removed",
    "request_id": "3b264082",
    "scope": "visible",
    "grammar": "inbound",
}


def _history(display_metadata):
    return [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "All good here.",
            "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
            "display_metadata": display_metadata,
        },
    ]


class TestGatewayRender:
    def test_forwards_display_kind_and_metadata(self):
        out = _history_to_messages(_history({CONFAB_NOTICE_KEY: dict(NOTICE)}))
        assistant = [m for m in out if m["role"] == "assistant"][-1]

        assert assistant["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND
        assert assistant["display_metadata"][CONFAB_NOTICE_KEY] == NOTICE

    def test_request_id_survives_the_reload(self):
        """Spec test 5 — same request ID after a session reload."""
        out = _history_to_messages(_history({CONFAB_NOTICE_KEY: dict(NOTICE)}))
        assistant = [m for m in out if m["role"] == "assistant"][-1]

        meta = assistant["display_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta[CONFAB_NOTICE_KEY]["request_id"] == "3b264082"

    def test_assistant_text_is_not_replaced_or_suffixed(self):
        """The notice is an event rendering, not an assistant bubble suffix."""
        out = _history_to_messages(_history({CONFAB_NOTICE_KEY: dict(NOTICE)}))
        assistant = [m for m in out if m["role"] == "assistant"][-1]

        assert assistant["text"] == "All good here."

    def test_row_is_not_hidden_from_the_timeline(self):
        """A confab-notice turn still carries real model output — show it."""
        out = _history_to_messages(_history({CONFAB_NOTICE_KEY: dict(NOTICE)}))

        assert any(m["role"] == "assistant" for m in out)

    def test_clean_turn_has_no_presentation_fields(self):
        out = _history_to_messages(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "All good here."},
            ]
        )
        assistant = [m for m in out if m["role"] == "assistant"][-1]

        assert "display_kind" not in assistant
        assert "display_metadata" not in assistant
