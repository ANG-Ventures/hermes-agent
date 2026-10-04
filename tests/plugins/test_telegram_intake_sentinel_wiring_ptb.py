"""Intake sentinel call site, driven through the real PTB Application (fork #639, t_9c06f87f).

``TelegramAdapter._register_handlers`` registers
``app.add_handler(TypeHandler(Update, self._observe_intake_update), group=-1)``. The group ``-1``
priority is the whole feature: at group ``>= 0`` a functional handler can consume or discard an
update before the sentinel observes it, and the drop is silent again.

``_observe_intake_update`` itself is covered by ``tests/gateway/test_telegram_intake_sentinel.py``.
This file covers the registration half without reading source text:

* **wiring**: ``TelegramAdapter.connect()`` builds the Application through its real setup path
  (``Application.builder()``, ``TelegramApplication``, ``initialize()``, ``start()``) against a fake
  Bot API transport. The test asserts the sentinel is registered once, as a blocking ``TypeHandler``
  over every ``Update``, in a group that PTB runs before group 0. It checks both the initial build
  and the rebuild after a failed ``initialize()``.
* **visible effect**: real ``Application.process_update`` calls. The sentinel's ingest line must come
  before a group-0 handler runs, and must still appear when that handler raises or when no group-0
  handler accepts the update.

Mutation proof (each applied to plugins/platforms/telegram/adapter.py, then restored):
(a) delete the ``app.add_handler(TypeHandler(Update, self._observe_intake_update), group=-1)`` line;
(b) change its ``group=-1`` to ``group=0``. The failing test names are listed in the PR body.
"""

import asyncio
import importlib
import logging
import sys

import pytest


def _evict_mocked_telegram() -> None:
    """Drop MagicMock ``telegram`` stand-ins installed by other test files.

    Same trap as ``tests/plugins/test_telegram_polling_progress_ptb.py`` (registry entry
    "test hygiene: evict MagicMock telegram modules"): in a combined run a mock resident under
    ``telegram`` makes ``importorskip`` succeed and every import below bind MagicMock attributes.
    A real package carries a str ``__file__``; the mock does not.
    """
    poisoned = [
        name for name in list(sys.modules)
        if (name == "telegram" or name.startswith("telegram."))
        and not isinstance(getattr(sys.modules[name], "__file__", None), str)
    ]
    if not poisoned:
        return
    for name in poisoned:
        del sys.modules[name]
    for mod_name in ("plugins.platforms.telegram.update_admission", "plugins.platforms.telegram.adapter"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            importlib.reload(mod)


_evict_mocked_telegram()
pytest.importorskip("telegram", reason="python-telegram-bot not installed")
from telegram import Update  # noqa: E402
from telegram.error import NetworkError  # noqa: E402
from telegram.ext import MessageHandler, TypeHandler, filters  # noqa: E402
from telegram.request import BaseRequest  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

_GET_ME = (
    b'{"ok":true,"result":{"id":424242,"is_bot":true,'
    b'"first_name":"Test","username":"test_bot"}}'
)


class _FakeBotApi(BaseRequest):
    """Bot API transport double: answers getMe, acks everything else, never touches the network."""

    def __init__(self, *, get_me_failures: int = 0):
        self.get_me_failures = get_me_failures
        self.endpoints = []

    @property
    def read_timeout(self):
        return 10

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **_kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.endpoints.append(endpoint)
        if endpoint == "getMe":
            if self.get_me_failures:
                self.get_me_failures -= 1
                raise NetworkError("fake transport down")
            return 200, _GET_ME
        return 200, b'{"ok":true,"result":true}'


async def _connect(monkeypatch, *, get_me_failures: int = 0):
    """Run the real ``connect()``; stub only lock, transport, polling and post-connect housekeeping."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))
    general = _FakeBotApi(get_me_failures=get_me_failures)
    updates = _FakeBotApi()

    async def _requests():
        return general, updates

    async def _lock(*_args, **_kwargs):
        return True

    async def _no_polling(*, is_reconnect):
        return None

    monkeypatch.setattr(adapter, "_build_ptb_requests", _requests)
    monkeypatch.setattr(adapter, "_acquire_platform_lock_async", _lock)
    monkeypatch.setattr(adapter, "_start_polling_mode", _no_polling)
    monkeypatch.setattr(adapter, "_start_post_connect_housekeeping", lambda: None)
    monkeypatch.setattr(adapter, "_restart_task_attr", lambda _attr, coro: coro.close())
    assert await adapter.connect() is True, "real connect() did not complete against the fake Bot API"
    return adapter, general


async def _disconnect(adapter):
    app = adapter._app
    if app.running:
        await app.stop()
    await app.shutdown()
    flush = getattr(adapter, "_update_receipt_flush", None)
    if flush is not None:
        await asyncio.gather(flush, return_exceptions=True)


def _sentinel_registrations(app, adapter):
    """``(group, handler)`` for every handler whose callback is the adapter's intake sentinel.

    ``TelegramApplication.add_handler`` wraps callbacks for update admission (``functools.wraps``),
    so compare the unwrapped callback.
    """
    return [
        (group, handler)
        for group, handlers in app.handlers.items()
        for handler in handlers
        if getattr(handler.callback, "__wrapped__", handler.callback) == adapter._observe_intake_update
    ]


def _unhandled_update(app, update_id):
    """A real ``Update`` no core group-0 handler accepts (a poll), so a probe appended to group 0 runs."""
    return Update.de_json({
        "update_id": update_id,
        "poll": {
            "id": f"poll-{update_id}", "question": "q?", "total_voter_count": 0,
            "is_closed": False, "is_anonymous": True, "type": "regular",
            "allows_multiple_answers": False,
            # persistent_id: required since Bot API 9.6 (PTB 22.8); older PTB keeps it in api_kwargs.
            "options": [{"persistent_id": "a", "text": "a", "voter_count": 0},
                        {"persistent_id": "b", "text": "b", "voter_count": 0}],
        },
    }, app.bot)


def _text_update(app, update_id):
    return Update.de_json({
        "update_id": update_id,
        "message": {
            "message_id": 7, "date": 1_700_000_000, "text": "hello",
            "chat": {"id": 571820863, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Ace"},
        },
    }, app.bot)


class _Timeline(logging.Handler):
    """Records the sentinel's ingest line into the same ordered list the probe handlers write to."""

    def __init__(self, events):
        super().__init__(level=logging.INFO)
        self.events = events

    def emit(self, record):
        message = record.getMessage()
        if "Telegram intake: update_id=" in message:
            update_id = int(message.split("update_id=", 1)[1].split()[0])
            self.events.append(("sentinel", update_id))


@pytest.fixture
def timeline():
    events = []
    handler = _Timeline(events)
    log = tg_adapter.logger
    previous = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        yield events
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)


# --------------------------------------------------------------------------- #
# 1. Wiring: registered through the real setup path, in a group before 0
# --------------------------------------------------------------------------- #

def _assert_sentinel_wired(app, adapter):
    registrations = _sentinel_registrations(app, adapter)
    assert len(registrations) == 1, (
        f"the intake sentinel must be registered exactly once on the built Application, "
        f"found {len(registrations)}: {registrations!r}"
    )
    group, handler = registrations[0]
    assert group < 0, (
        f"the intake sentinel is in group {group}; PTB runs groups in ascending order, so at "
        "group >= 0 a functional handler can discard an update before the sentinel observes it"
    )
    functional_groups = [g for g, hs in app.handlers.items() for h in hs
                         if (g, h) not in registrations]
    assert functional_groups and group < min(functional_groups), (
        f"sentinel group {group} must run before every functional handler group {sorted(set(functional_groups))}"
    )
    assert isinstance(handler, TypeHandler) and handler.type is Update, (
        "the sentinel must observe ALL updates: a TypeHandler over telegram.Update"
    )
    # Blocking on purpose: block=False defers the callback to a task, observations can then land
    # out of order, and the sequential-id gap detector raises false alarms. PTB stores the default
    # as DefaultValue(True), so compare truthiness, not identity.
    assert bool(handler.block), "the sentinel must be a blocking handler"
    for update in (_text_update(app, 1), _unhandled_update(app, 2)):
        assert handler.check_update(update), f"the sentinel does not accept {update!r}"


@pytest.mark.asyncio
async def test_wiring_sentinel_registered_before_group_zero_on_connect(monkeypatch):
    adapter, _ = await _connect(monkeypatch)
    try:
        _assert_sentinel_wired(adapter._app, adapter)
    finally:
        await _disconnect(adapter)


@pytest.mark.asyncio
async def test_wiring_sentinel_registered_on_rebuilt_app_after_failed_initialize(monkeypatch):
    """The transient-init rebuild in ``_initialize_app_with_retries`` builds a NEW Application and
    re-registers handlers; the sentinel must be on the app that actually serves."""
    adapter, general = await _connect(monkeypatch, get_me_failures=1)
    try:
        assert general.endpoints.count("getMe") == 2, (
            f"expected one failed and one successful initialize, saw {general.endpoints!r}"
        )
        _assert_sentinel_wired(adapter._app, adapter)
    finally:
        await _disconnect(adapter)


# --------------------------------------------------------------------------- #
# 2. Visible effect: real process_update, ordering against group 0
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_effect_sentinel_logs_before_group_zero_handler_runs(monkeypatch, timeline):
    adapter, _ = await _connect(monkeypatch)
    app = adapter._app
    try:
        async def probe(update, _context):
            timeline.append(("group0", update.update_id))

        app.add_handler(TypeHandler(Update, probe), group=0)
        await app.process_update(_unhandled_update(app, 501))

        assert timeline == [("sentinel", 501), ("group0", 501)], (
            f"the sentinel's ingest line must precede the group-0 handler; timeline={timeline!r}"
        )
    finally:
        await _disconnect(adapter)


@pytest.mark.asyncio
async def test_effect_sentinel_logs_when_group_zero_handler_raises(monkeypatch, timeline):
    adapter, _ = await _connect(monkeypatch)
    app = adapter._app
    try:
        async def exploding(update, _context):
            timeline.append(("group0", update.update_id))
            raise RuntimeError("group-0 handler blew up")

        app.add_handler(TypeHandler(Update, exploding), group=0)
        await app.process_update(_unhandled_update(app, 601))

        assert timeline == [("sentinel", 601), ("group0", 601)], (
            f"a raising group-0 handler must not cost the ingest line; timeline={timeline!r}"
        )
    finally:
        await _disconnect(adapter)


@pytest.mark.asyncio
async def test_effect_sentinel_logs_when_group_zero_filters_the_update_out(monkeypatch, timeline):
    """No group-0 handler accepts a poll update (core message/callback/inline filters, plus a
    TEXT-only probe). The ingest line is then the only evidence the update ever arrived."""
    adapter, _ = await _connect(monkeypatch)
    app = adapter._app
    try:
        async def text_only(update, _context):
            timeline.append(("group0", update.update_id))

        app.add_handler(MessageHandler(filters.TEXT, text_only), group=0)
        await app.process_update(_unhandled_update(app, 701))
        await app.process_update(_unhandled_update(app, 702))

        assert timeline == [("sentinel", 701), ("sentinel", 702)], (
            f"filtered-out updates must still leave one ingest line each; timeline={timeline!r}"
        )
    finally:
        await _disconnect(adapter)
