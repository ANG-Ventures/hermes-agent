# fork-only: behavioural regressions for the delivery-ack barrier registry.
"""The delivery-ack barrier registry must be generation-ordered and
generation-scoped.

``BasePlatformAdapter._delivery_ack_callbacks`` is ONE SLOT per
``session_key``.  The SELF deferred-restart arm registers a barrier there
before arming, waits on it, and drops it again if the arm never happened
(``gateway/run.py`` ``_arm_deferred_restart_after_release``).  Because the
arm is now dispatched OFF the loop, two arms for the same session can be in
flight at once, and their register/cancel calls interleave arbitrarily.

With an unconditional write and a key-only pop, that interleaving corrupts a
LIVE barrier in two ways.  Either failure has the same user-visible shape: a
turn's final send is delivered, its ack is dropped on the floor, and the arm
that was waiting on it blocks until its delivery timeout expires and records
UNKNOWN delivery state for a restart that actually completed.

Regressions for the two FleetReview P1s on record ``c89198135``:
``gateway/run.py:33595`` ("unconditional pre-arm barrier registration
clobbers a live delivery barrier on a double-release") and
``gateway/platforms/base.py:5786`` ("generation-blind cancel").

The oracle is BEHAVIOUR -- does the live generation's acknowledgement still
fire its callback -- not which method was called.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter


class _StubAdapter(BasePlatformAdapter):
    platform = MagicMock(value="telegram")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {}


@pytest.fixture()
def adapter() -> _StubAdapter:
    obj = _StubAdapter.__new__(_StubAdapter)
    obj._delivery_ack_callbacks = {}
    obj._post_delivery_callbacks = {}
    return obj


def test_a_stale_older_arm_cannot_clobber_the_live_barrier(adapter):
    """An older generation's registration must not displace a newer live one.

    Both arms write the same slot.  If the lagging one wins, the newer turn's
    ack fires the WRONG callback (or none), and the newer arm waits out its
    full delivery timeout for an ack that already happened.
    """
    fired: list[str] = []

    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("live"), generation=9
    )
    # A lagging older arm arrives late and registers for generation 3.
    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("stale"), generation=3
    )

    assert adapter.acknowledge_response_delivery("s", generation=9) is True, (
        "the live generation's delivery ack was refused: a stale older arm "
        "overwrote its barrier, so it will block until its delivery timeout "
        "and record UNKNOWN delivery for a restart that actually completed"
    )
    assert fired == ["live"]


def test_a_newer_arm_still_replaces_an_older_barrier(adapter):
    """Non-vacuity: ordering must not become "first registration wins".

    A legitimate re-arm for a later generation has to take the slot.  A guard
    that refused every replacement would pass the test above for the wrong
    reason and break the normal path.
    """
    fired: list[str] = []

    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("old"), generation=3
    )
    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("new"), generation=9
    )

    assert adapter.acknowledge_response_delivery("s", generation=9) is True
    assert fired == ["new"]


def test_a_stale_cleanup_cannot_cancel_a_newer_live_barrier(adapter):
    """Cancelling after a failed arm must not delete a newer turn's barrier.

    ``_arm_deferred_restart_after_release`` cancels the barrier it registered
    when its own arm produced nothing.  A key-only pop takes whatever is in
    the slot, so a newer turn that registered in the meantime silently loses
    its barrier.
    """
    fired: list[str] = []

    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("gen3"), generation=3
    )
    adapter.register_delivery_ack_callback(
        "s", lambda: fired.append("gen9"), generation=9
    )

    # The gen-3 arm failed and cleans up after itself.
    assert adapter.cancel_delivery_ack_callback("s", generation=3) is False

    assert adapter.acknowledge_response_delivery("s", generation=9) is True, (
        "a stale cleanup deleted the live newer barrier; that turn's delivery "
        "ack is dropped and its arm blocks to timeout"
    )
    assert fired == ["gen9"]


def test_a_cleanup_still_cancels_its_own_barrier(adapter):
    """Non-vacuity: the cleanup must still do its job for its OWN generation.

    A guard that refused every cancel would pass the test above and leave a
    stale one-shot callback behind to swallow a later turn's ack -- the very
    thing the cleanup exists to prevent.
    """
    adapter.register_delivery_ack_callback("s", lambda: None, generation=3)

    assert adapter.cancel_delivery_ack_callback("s", generation=3) is True
    assert adapter.acknowledge_response_delivery("s", generation=3) is False


def test_the_production_cleanup_passes_its_generation():
    """The call site must actually scope its cleanup.

    The registry guard is only reachable if ``gateway/run.py`` passes the
    generation it registered with.  This is the wiring half of the fix: with
    a generation-aware API and a key-only call site, the defect is unchanged.
    """
    from gateway import run as run_mod

    src = inspect.getsource(run_mod.GatewayRunner._arm_deferred_restart_after_release)

    assert "cancel_delivery_ack_callback(" in src, (
        "the arm no longer cleans up its barrier at all; this test needs "
        "rewriting against the new shape"
    )
    call_start = src.index("cancel_delivery_ack_callback(")
    call_text = src[call_start : call_start + 200]
    assert "generation=" in call_text, (
        "gateway/run.py cancels the delivery barrier by session_key alone, so "
        "the generation guard in the registry is never reached and a stale "
        "cleanup still deletes a newer live barrier:\n" + call_text
    )


def test_ungenerationed_registrations_keep_their_unconditional_semantics(adapter):
    """``generation=None`` carries no ordering information, so it must not be
    ordered.  Older callers (and tests) that register without a generation
    keep last-write-wins."""
    fired: list[str] = []

    adapter.register_delivery_ack_callback("s", lambda: fired.append("first"))
    adapter.register_delivery_ack_callback("s", lambda: fired.append("second"))

    assert adapter.acknowledge_response_delivery("s") is True
    assert fired == ["second"]
