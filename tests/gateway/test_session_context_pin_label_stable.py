"""A thread's session-context label must not depend on which Discord path delivered the turn
(class H, t_91b6a4c4).

Phase-1 measurement (t_38dd407a): ``messages[0]`` re-rendered "#dgx-spark / Suspension" <->
"#Suspension" with no rename. The session-context pin is keyed on the source's ``chat_name``; the
message path (``_format_thread_chat_name``) names a thread ``Guild / #parent / thread``, while the
native-slash path (``_build_slash_event``) named it ``Guild / #thread``. Alternating a slash command
and a message in one thread re-keyed the pin each time and rewrote the system prompt's context.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.gateway.test_discord_channel_prompts import _ensure_discord_mock, _make_adapter
from tests.gateway.test_prompt_tail_freeze import _make_runner


def _thread(discord_mod):
    guild = SimpleNamespace(name="Daemonarchy", id=1480524732964278294)
    parent = SimpleNamespace(name="dgx-spark", id=1557932597236080680, guild=guild, type=None)
    thread = discord_mod.Thread()
    thread.id = 1560031316173394112
    thread.name = "Suspension"
    thread.guild = guild
    thread.parent = parent
    thread.parent_id = parent.id
    return thread


def _session_context(source):
    from gateway.config import Platform
    from gateway.session import SessionContext

    return SessionContext(source=source, connected_platforms=[Platform.DISCORD], home_channels={})


def test_slash_and_message_paths_share_session_context_pin(monkeypatch):
    _ensure_discord_mock()
    discord_mod = sys.modules["discord"]
    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: True)
    adapter = _make_adapter()
    captured = []

    def _capture(**kwargs):
        from gateway.config import Platform
        from gateway.session import SessionSource

        captured.append(kwargs)
        return SessionSource(platform=Platform.DISCORD, chat_id=kwargs["chat_id"], chat_name=kwargs["chat_name"],
                             chat_type=kwargs["chat_type"], user_id=kwargs["user_id"],
                             user_name=kwargs["user_name"], thread_id=kwargs.get("thread_id"),
                             parent_chat_id=kwargs.get("parent_chat_id"))

    adapter.build_source = _capture
    adapter._get_effective_topic = MagicMock(return_value=None)
    thread = _thread(discord_mod)

    message_label = adapter._format_thread_chat_name(thread)
    interaction = SimpleNamespace(channel_id=thread.id, channel=thread, guild_id=thread.guild.id,
                                  user=SimpleNamespace(id=117431298246705156, display_name="Ace"))
    slash_event = adapter._build_slash_event(interaction, "/status")
    assert captured[-1]["chat_name"] == message_label

    from gateway.session_identity import replace_source

    runner = _make_runner()
    message_source = replace_source(slash_event.source, chat_name=message_label)
    first = runner._pinned_session_context_prompt(_session_context(message_source), False, "sk")  # noqa: SLF001
    slash = runner._pinned_session_context_prompt(_session_context(slash_event.source), False, "sk")  # noqa: SLF001
    again = runner._pinned_session_context_prompt(_session_context(message_source), False, "sk")  # noqa: SLF001
    assert slash is first and again is first
