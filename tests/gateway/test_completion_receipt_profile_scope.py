"""Completion receipts run off-loop in the caller's profile scope (Prism 0d646d6f5979).

``loop.run_in_executor`` does not copy contextvars (``to_thread`` did). The SQLite half of the
receipt (``complete_completion_delivery``) is not producer-scoped and resolves ``state.db`` from
the ambient home override, so the executor hop must carry the caller's context.
"""
import asyncio
from types import SimpleNamespace

from tests.gateway.test_completion_delivery import (
    AdmittingHandler,
    _async_event,
    _runner,
    isolated_registry,  # noqa: F401 — autouse fixture for real temp storage
)
from tools import async_delegation as ad
from hermes_constants import (
    get_hermes_home_override,
    reset_hermes_home_override,
    set_hermes_home_override,
)


def test_receipt_worker_sees_the_callers_profile_home(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "secondary"
    profile_home.mkdir(parents=True)
    event = _async_event("deleg_scoped_receipt")
    event.pop("_registry_profile_home", None)  # unstamped: only the ambient scope names the home
    seen = []

    def receipt(evt, claim_id):
        seen.append(get_hermes_home_override())
        return True

    monkeypatch.setattr(ad, "complete_event_delivery_with_retry", receipt)
    runner = _runner(SimpleNamespace(handle_message=AdmittingHandler()))

    async def exercise():
        token = set_hermes_home_override(profile_home)
        try:
            return await runner._deliver_completion_notification_scoped("done", event)
        finally:
            reset_hermes_home_override(token)

    assert asyncio.run(exercise()) is True
    assert seen == [str(profile_home)]
