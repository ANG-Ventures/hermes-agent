"""Intake sentinel: prove a silently-dropped inbound update becomes visible.

Regression context (2026-08-26): a user DM was fetched by the poller,
offset-confirmed to Telegram (``pending_update_count`` returned to 0), and then
never reached any handler. The gateway was healthy the whole time — no restart,
no 409 conflict, no polling recovery — and produced **zero** log evidence, so
the loss was unattributable after the fact.

These tests lock the two properties that make that class detectable:

1. every update is observed at the ingest boundary, before any filter or
   batching can discard it; and
2. a forward ``update_id`` gap between consecutively observed updates is
   reported at WARNING, because Telegram ids are sequential per bot and a jump
   proves an update was consumed but never dispatched.
"""

import logging
import sys
from unittest.mock import MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    telegram_mod.error.NetworkError = type("NetworkError", (OSError,), {})
    telegram_mod.error.TimedOut = type("TimedOut", (OSError,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


class _FakeUpdate:
    """Minimal stand-in carrying only what the sentinel reads."""

    def __init__(self, update_id, chat_id=571820863, user_id=42):
        self.update_id = update_id
        self.effective_chat = MagicMock(id=chat_id)
        self.effective_user = MagicMock(id=user_id)
        self.message = MagicMock()
        self.edited_message = None
        self.channel_post = None
        self.edited_channel_post = None
        self.callback_query = None
        self.inline_query = None
        self.my_chat_member = None
        self.chat_member = None
        self.poll = None
        self.poll_answer = None


def _adapter():
    config = PlatformConfig(enabled=True, extra={})
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = config
    # ``name`` is a property deriving from ``platform``; the sentinel only uses
    # it for the log prefix, so supply the enum the property reads.
    adapter.platform = MagicMock(value="telegram")
    adapter._intake_last_update_id = None
    return adapter


def _gap_records(caplog):
    return [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "intake gap" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_every_update_is_logged_at_ingest_boundary(caplog):
    """Attribution: the sentinel records each update_id it observes."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        await adapter._observe_intake_update(_FakeUpdate(1000))
        await adapter._observe_intake_update(_FakeUpdate(1001))

    intake = [r.getMessage() for r in caplog.records if "Telegram intake:" in r.getMessage()]
    assert len(intake) == 2
    assert "update_id=1000" in intake[0]
    assert "update_id=1001" in intake[1]


@pytest.mark.asyncio
async def test_sequential_updates_report_no_gap(caplog):
    """A contiguous stream must never produce a false positive."""
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        for update_id in range(500, 510):
            await adapter._observe_intake_update(_FakeUpdate(update_id))

    assert _gap_records(caplog) == []
    assert adapter._intake_last_update_id == 509


@pytest.mark.asyncio
async def test_forward_gap_is_reported_with_missing_id_range(caplog):
    """The regression itself: a consumed-but-undispatched update must alarm.

    This is the assertion that would have caught the 2026-08-26 silent drop.
    """
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        await adapter._observe_intake_update(_FakeUpdate(100))
        # 101 and 102 were fetched + offset-confirmed but never dispatched.
        await adapter._observe_intake_update(_FakeUpdate(103))

    gaps = _gap_records(caplog)
    assert len(gaps) == 1, "a forward update_id jump must be reported exactly once"
    message = gaps[0].getMessage()
    assert "2 update(s)" in message
    assert "101..102" in message


@pytest.mark.asyncio
async def test_first_update_of_a_session_never_reports_a_gap(caplog):
    """A session starts at an arbitrary offset — that is not a loss."""
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        await adapter._observe_intake_update(_FakeUpdate(987654))

    assert _gap_records(caplog) == []


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_updates_do_not_alarm(caplog):
    """Telegram may redeliver; only a FORWARD jump proves loss."""
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        await adapter._observe_intake_update(_FakeUpdate(200))
        await adapter._observe_intake_update(_FakeUpdate(200))  # redelivery
        await adapter._observe_intake_update(_FakeUpdate(199))  # reordered

    assert _gap_records(caplog) == []
    # The high-water mark must not regress, or the next update would false-alarm.
    assert adapter._intake_last_update_id == 200


@pytest.mark.asyncio
async def test_malformed_update_is_ignored_without_raising(caplog):
    """Observation must never disturb the dispatch path."""
    adapter = _adapter()
    bad = _FakeUpdate(300)
    bad.update_id = "not-an-int"
    with caplog.at_level(logging.WARNING):
        await adapter._observe_intake_update(bad)
        await adapter._observe_intake_update(_FakeUpdate(None))

    assert _gap_records(caplog) == []
    assert adapter._intake_last_update_id is None


@pytest.mark.asyncio
async def test_new_polling_generation_resets_the_sentinel(caplog):
    """A reconnect replays a backlog — the stale high-water mark must clear.

    Without the reset, the first update after a reconnect would be compared
    against the previous session's id and emit a false gap.
    """
    import asyncio

    adapter = _adapter()
    adapter._polling_teardown_started = False
    adapter._polling_generation = 3
    adapter._polling_progress_verifier_task = None
    adapter._polling_progress_event = asyncio.Event()
    adapter._polling_progress_accepting = False
    adapter._send_path_degraded = False

    await adapter._observe_intake_update(_FakeUpdate(9000))
    assert adapter._intake_last_update_id == 9000

    adapter._begin_polling_generation()
    assert adapter._intake_last_update_id is None

    with caplog.at_level(logging.WARNING):
        await adapter._observe_intake_update(_FakeUpdate(12345))
    assert _gap_records(caplog) == []


def test_handlers_are_registered_from_a_single_site():
    """Structural guard: the sentinel cannot be wired on only one path.

    Handler registration used to be duplicated between initial connect and the
    rebuild-on-retry path, so a new handler had to be remembered twice. Both
    paths now call _register_handlers, and the intake sentinel must be in a
    group that runs strictly before the functional handlers.
    """
    import inspect

    from plugins.platforms.telegram import adapter as tg_adapter

    source = inspect.getsource(tg_adapter.TelegramAdapter)
    # Exactly one place constructs the handler set.
    assert source.count("def _register_handlers") == 1
    assert source.count("self._register_handlers(self._app)") == 2, (
        "both the initial-connect and rebuild-on-retry paths must share the "
        "single registration site"
    )

    registrar = inspect.getsource(tg_adapter.TelegramAdapter._register_handlers)
    # Strip the docstring: it *discusses* block=False, and a guard that greps
    # prose would match its own explanation.
    code_only = registrar.split('"""')[-1]
    assert "TypeHandler" in code_only
    assert "group=-1" in code_only, "the sentinel must observe before handling"
    # The sentinel must be BLOCKING. block=False defers the callback to
    # Application.create_task, which (a) never runs when the app isn't running
    # and (b) allows out-of-order observation — which for a sequential-id gap
    # detector manufactures false alarms. A separate group already guarantees
    # it cannot consume the update.
    assert "block=False" not in code_only, (
        "the sentinel must observe in order; block=False defers it to a task"
    )
