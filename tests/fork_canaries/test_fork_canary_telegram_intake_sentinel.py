"""Fork canary: Telegram intake sentinel makes silent inbound drops visible (#639).

Surface: the Telegram platform adapter
(``plugins/platforms/telegram/adapter.py``), ingest boundary.

Regression context (2026-08-26): a user DM was fetched by the poller,
offset-confirmed to Telegram (``pending_update_count`` back to 0), and then
never reached any handler. The gateway was healthy throughout — no restart, no
409, no polling recovery — and the loss produced **zero** log evidence, so it
was unattributable after the fact. The defect worth fixing was that
undetectability.

The fork registers an observation-only ``TypeHandler`` in group ``-1`` so it
sees every update before any filter, authorization prefilter, or text batching
can discard one. Two signals:

* **Attribution** — one INFO line per update (id, chat, kind), so a "never
  arrived" report can be classified as transport loss (no line) vs in-process
  drop (line present, no handler work).
* **Gap detection** — Telegram ``update_id``s are sequential per bot, so a
  forward jump proves an update was consumed and offset-confirmed but never
  dispatched. Logged at WARNING with the exact missing range.

This file is a *complement* to the shipped
``tests/gateway/test_telegram_intake_sentinel.py``: it locks the properties a
parity merge is most likely to erode — the group ``-1`` registration priority
and the no-false-alarm rules — rather than re-testing the happy path.
"""

import inspect
import logging
import sys
from unittest.mock import MagicMock

import pytest


def _ensure_telegram_mock():
    """Minimal ``telegram`` stub. Matches the shipped sentinel test's helper."""
    if "telegram" in sys.modules and isinstance(
        getattr(sys.modules["telegram"], "__file__", None), str
    ):
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
    def __init__(self, update_id, chat_id=571820863, user_id=42):
        self.update_id = update_id
        self.effective_chat = MagicMock(id=chat_id)
        self.effective_user = MagicMock(id=user_id)
        self.message = MagicMock(text="hi", photo=None, voice=None, document=None)


def _adapter():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter.platform = MagicMock(value="telegram")
    adapter._intake_last_update_id = None
    return adapter


def _gap_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "intake gap" in r.getMessage()
    ]


def _intake_infos(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.INFO and "Telegram intake:" in r.getMessage()
    ]


# --------------------------------------------------------------------------- #
# Registration priority — the property that makes the sentinel *complete*
# --------------------------------------------------------------------------- #

def test_sentinel_is_registered_in_a_group_below_every_other_handler():
    """The sentinel is only trustworthy if nothing can discard an update
    before it observes one. Group ``-1`` guarantees it runs ahead of the
    default group ``0`` handlers (filters, auth prefilter, text batching).

    RED-PROVABLE: in plugins/platforms/telegram/adapter.py (~L2294) change
    ``group=-1`` to ``group=0`` (or drop the kwarg) — the source assertion
    below fails because no negative group remains on the sentinel
    registration."""
    src = inspect.getsource(TelegramAdapter)
    assert "_observe_intake_update" in src, (
        "the intake sentinel handler was removed from the adapter entirely"
    )
    # Locate the add_handler call that registers the sentinel and confirm it
    # carries a negative group.
    idx = src.find("TypeHandler(Update, self._observe_intake_update)")
    assert idx != -1, (
        "the sentinel is no longer registered as a TypeHandler over all Updates"
    )
    window = src[idx: idx + 400]
    assert "group=-1" in window, (
        "the intake sentinel lost its group=-1 priority; a group>=0 handler "
        "can discard an update before the sentinel observes it, which "
        "reopens the exact blind spot #639 closed."
    )


# --------------------------------------------------------------------------- #
# Gap detection
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_forward_gap_reports_the_exact_missing_range(caplog):
    """RED-PROVABLE: in ``_observe_intake_update``
    (plugins/platforms/telegram/adapter.py ~L2358) delete the
    ``logger.warning("[%s] Telegram intake gap: ...")`` call — the drop goes
    back to being invisible and this fails."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        await adapter._observe_intake_update(_FakeUpdate(100))
        await adapter._observe_intake_update(_FakeUpdate(104))

    warnings = _gap_warnings(caplog)
    assert len(warnings) == 1, "a 3-update forward gap was not reported"
    msg = warnings[0].getMessage()
    # The missing range must be actionable: 101..103, not just "a gap".
    assert "101" in msg and "103" in msg, (
        f"gap warning lost the exact missing id range: {msg!r}"
    )


@pytest.mark.asyncio
async def test_contiguous_stream_never_alarms(caplog):
    """A false-positive gap alert on every normal message would train the
    operator to ignore the signal.

    RED-PROVABLE: change the gap condition (adapter.py ~L2357) from
    ``update_id > previous + 1`` to ``update_id > previous``."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        for uid in (10, 11, 12, 13):
            await adapter._observe_intake_update(_FakeUpdate(uid))
    assert _gap_warnings(caplog) == [], "contiguous updates produced a false gap alert"


@pytest.mark.asyncio
async def test_first_update_after_connect_never_alarms(caplog):
    """A polling session legitimately starts at an arbitrary offset (cold boot
    uses ``drop_pending_updates=True``; a reconnect replays a backlog), so the
    first observed update has no meaningful predecessor.

    RED-PROVABLE: in adapter.py (~L2357) drop the ``previous is not None``
    guard — the very first update starts alarming against ``None``."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        await adapter._observe_intake_update(_FakeUpdate(999_999))
    assert _gap_warnings(caplog) == [], "first update of a session reported a gap"


@pytest.mark.asyncio
async def test_duplicate_and_reordered_updates_do_not_alarm(caplog):
    """Telegram can redeliver after a failed offset confirm, and PTB may
    dispatch out of order across queue drains — only a FORWARD jump proves
    loss.

    RED-PROVABLE: in adapter.py (~L2372) change the sentinel advance guard
    ``if previous is None or update_id > previous:`` to an unconditional
    ``self._intake_last_update_id = update_id`` — replaying 20 after 21 then
    seeing 22 manufactures a phantom gap."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        for uid in (20, 21, 20, 21, 22):  # duplicate + regression + resume
            await adapter._observe_intake_update(_FakeUpdate(uid))
    assert _gap_warnings(caplog) == [], (
        "duplicate/out-of-order redelivery produced a phantom gap alert"
    )


# --------------------------------------------------------------------------- #
# Attribution + robustness
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_every_update_leaves_an_attribution_line(caplog):
    """RED-PROVABLE: delete the trailing ``logger.info("[%s] Telegram intake:
    update_id=%d ...")`` call in ``_observe_intake_update``
    (plugins/platforms/telegram/adapter.py ~L2378)."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        for uid in (1, 2, 3):
            await adapter._observe_intake_update(_FakeUpdate(uid))
    infos = _intake_infos(caplog)
    assert len(infos) == 3, (
        f"expected one attribution line per update, got {len(infos)}"
    )
    joined = " ".join(r.getMessage() for r in infos)
    for uid in (1, 2, 3):
        assert f"update_id={uid}" in joined, f"update {uid} left no attribution line"


@pytest.mark.asyncio
async def test_malformed_updates_are_tolerated_not_raised(caplog):
    """The sentinel is observation-only and sits ahead of every handler — if it
    raises, it takes the whole ingest path down with it.

    RED-PROVABLE: in adapter.py (~L2348) remove the
    ``except (TypeError, ValueError): return`` around ``int(update_id)`` — the
    non-numeric case raises and this test errors out."""
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        await adapter._observe_intake_update(object())            # no update_id
        await adapter._observe_intake_update(_FakeUpdate(None))   # None id
        await adapter._observe_intake_update(_FakeUpdate("abc"))  # non-numeric
        await adapter._observe_intake_update(_FakeUpdate(7))      # then a good one
    assert adapter._intake_last_update_id == 7, (
        "a malformed update corrupted or blocked the sentinel's state"
    )
