"""The route-change status gate must accept both warning-glyph presentations.

The chat-pin notice is emitted with emoji presentation (U+26A0 U+FE0F) so it
renders as the warning emoji on Discord/Telegram. The gate that recognises
durable route announcements matches on that prefix — if it only accepted the
bare U+26A0, the notice would be silently filtered out and never delivered
(observed 2026-09-10: notice vanished entirely, adapter received nothing).
"""

from gateway.run import _is_model_route_change_status


def test_emoji_presentation_route_notice_is_recognised():
    assert _is_model_route_change_status(
        "\u26a0\ufe0f replying on a/b — this chat is pinned to c/d; reason"
    )


def test_bare_glyph_route_notice_still_recognised():
    """Back-compat: a plain-text producer must not be silently dropped."""
    assert _is_model_route_change_status(
        "\u26a0 replying on a/b — this chat is pinned to c/d; reason"
    )


def test_unrelated_warning_is_not_a_route_change():
    assert not _is_model_route_change_status("\u26a0\ufe0f Compression aborted")
    assert not _is_model_route_change_status("just text")


def test_other_route_prefixes_unaffected():
    assert _is_model_route_change_status("\U0001f504 Model fallback: x")
    assert _is_model_route_change_status("\U0001f500 Model switched: x")
