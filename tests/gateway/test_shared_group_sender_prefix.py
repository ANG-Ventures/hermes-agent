import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_internal_event_never_gets_sender_prefix():
    """Boot auto-resume synthesizes MessageEvent(text='', internal=True) with
    the session origin's user_name. Stamping '[<user>] ' onto it (a) forged a
    ghost user message and (b) made the text non-empty, which skipped the
    reason-aware recovery system-note substitution downstream (it only fires
    on blank text) — 2026-07-10 live incident. Internal events must never be
    attributed to the human."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="", source=source, internal=True)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    # MUST stay blank so the recovery system note substitution can fire.
    assert result == ""


@pytest.mark.asyncio
async def test_internal_event_with_text_not_prefixed_either():
    """Synthetic continuations carry prompt text but no human sender —
    same rule: no impersonation."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="continue the plan", source=source, internal=True)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "continue the plan"
    assert "[Alice]" not in result


@pytest.mark.asyncio
async def test_preprocess_includes_slack_author_mention_for_shared_thread():
    """Shared Slack threads expose the current author's verifiable user ID
    next to the display name so 'mention me again' requests can bind the
    mention to the CURRENT speaker (#17916)."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.SLACK: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_id="U123",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="mention me again", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice | Slack user <@U123>] mention me again"


@pytest.mark.asyncio
async def test_slack_internal_event_gets_no_author_mention():
    """Sibling call path: the Slack author-mention variant of the same prefix.

    Salvaged from the wave3b worktree (2026-07-26): the Telegram prefix cases
    were covered here, but Slack formats the sender as a <@user_id> mention via
    a different adapter path -- an internal synthetic event must not get one.
    """
    runner = _make_runner(
        GatewayConfig(
            platforms={Platform.SLACK: PlatformConfig(enabled=True, token="fake")},
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_id="U123",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="continue", source=source, internal=True)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "continue"
    assert "U123" not in result
