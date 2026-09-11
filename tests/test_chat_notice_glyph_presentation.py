"""Chat-facing warning glyphs must carry the emoji-presentation selector.

A bare U+26A0 renders as thin monochrome text on Discord/Telegram/desktop
instead of the yellow warning emoji, so notices that reach a messaging surface
look broken next to the other severity glyphs (which are emoji-presentation).

Scope is deliberately narrow: only modules whose notices are delivered to a chat
surface. CLI/TUI/doctor/setup output stays monochrome on purpose — that is
terminal chrome, where text presentation is correct and intended.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

CHAT_FACING = (
    "agent/chat_completion_helpers.py",
    "agent/conversation_compression.py",
    "agent/agent_init.py",
    "plugins/platforms/discord/adapter.py",
    "plugins/platforms/telegram/adapter.py",
)

BARE_WARNING = re.compile("\u26a0(?!\ufe0f)")


@pytest.mark.parametrize("rel", CHAT_FACING)
def test_chat_facing_warning_glyphs_use_emoji_presentation(rel):
    path = ROOT / rel
    source = path.read_text(encoding="utf-8")

    offenders = [
        f"{rel}:{lineno}: {line.strip()[:100]}"
        for lineno, line in enumerate(source.splitlines(), 1)
        if BARE_WARNING.search(line)
    ]

    assert not offenders, (
        "bare U+26A0 (text presentation) in a chat-facing notice; append U+FE0F "
        "so it renders as the warning emoji on Discord/Telegram:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_detects_a_bare_glyph():
    """The guard must actually fire — a bare glyph is not silently accepted."""
    assert BARE_WARNING.search("\u26a0 stale route")
    assert not BARE_WARNING.search("\u26a0\ufe0f stale route")


def test_desktop_leading_glyph_regex_tolerates_the_selector():
    """The desktop notice store strips the severity glyph; it must accept VS16."""
    store = (ROOT / "apps/desktop/src/store/agent-notices.ts").read_text(encoding="utf-8")
    match = re.search(r"const LEADING_GLYPH = /([^/]+)/u", store)
    assert match, "LEADING_GLYPH regex not found in agent-notices.ts"
    assert "\\uFE0F?" in match.group(1), (
        "LEADING_GLYPH must keep the optional \\uFE0F so emoji-presentation "
        "severity glyphs are still stripped"
    )
