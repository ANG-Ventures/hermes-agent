"""Source contracts: the turn result must carry the served provider + reasoning.

Regression (2026-08-08, parity merge aa27fd8be): upstream's TurnRunner
extraction rebuilt ``run_sync``'s result dicts WITHOUT the fork's
``_resolved_provider`` / ``_resolved_reasoning_config`` keys and dropped the
``_announce_and_persist_served_route`` call site entirely.  Observable damage:

* the runtime footer degraded from ``claude-apx-15/claude-opus-5 · …`` to a
  bare ``claude-fable-5 · …`` (build_footer_line renders ``provider/model``
  only when the result carries a provider), and
* ``last_served_identity`` was never persisted again — the recovery/announce
  machinery in ``_announce_and_persist_served_route`` became dead code while
  its unit tests stayed green (they call the method directly).

These are deliberately SOURCE contracts (the fork's established pattern for
the run.py god-file): they pin that the wiring exists, complementing the
behavioural unit tests that pin what each piece does in isolation.
"""
from __future__ import annotations

import inspect
import re

import gateway.run as gw_run


def _run_sync_source() -> str:
    return inspect.getsource(gw_run.TurnRunner.run_sync)


def test_turn_result_carries_served_provider():
    src = _run_sync_source()
    assert re.search(r'_resolved_provider\s*=\s*getattr\(_agent,\s*"provider"', src), (
        "run_sync no longer resolves the served provider — the footer's "
        "provider/model field will silently degrade to the bare model"
    )
    assert src.count('"provider": _resolved_provider') >= 2, (
        "both run_sync result dicts (success + failure) must carry the served "
        "provider for the runtime footer"
    )


def test_turn_result_carries_live_reasoning_config():
    src = _run_sync_source()
    assert src.count('"reasoning_config": _resolved_reasoning_config') >= 2, (
        "run_sync result dicts must carry the live reasoning config — "
        "_footer_reasoning_label prefers it over the session-resolver fallback"
    )


def test_announce_and_persist_served_route_is_not_orphaned():
    """The single writer of ``last_served_identity`` must have a live call site."""
    src = _run_sync_source()
    assert "_announce_and_persist_served_route(" in src, (
        "run_sync must invoke _announce_and_persist_served_route — without it "
        "last_served_identity is never persisted and every recovery/re-init "
        "announce silently dies (its unit tests keep passing; only this wiring "
        "check fails)"
    )
    # And the call must feed the resolved identity, not constants.
    call = src.split("_announce_and_persist_served_route(", 1)[1][:400]
    assert "served_provider=_resolved_provider" in call
    assert "served_model=_resolved_model" in call
