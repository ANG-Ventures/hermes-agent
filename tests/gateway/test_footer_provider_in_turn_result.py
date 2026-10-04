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

PAIRED TESTS. This file pins only the PRODUCER half. The CONSUMER half (the
``build_footer_line`` call in ``GatewayTurnMixin._hmwa_runtime_footer_line``,
gateway/run_turn.py) and the rendered end-to-end footer are pinned by
``tests/gateway/test_footer_consumer_in_turn.py``. On 2026-10-04 (t_829a3079) this
file stayed green while the consumer dropped ``provider=``. The next split that moves
either half must move both tests.

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
    # Parity 2026-10-01: upstream's run_sync builds ONE ``usage`` dict that both result paths
    # (success + empty-response) share through ``common``; the fork's served provider and live
    # reasoning config ride that dict (names follow upstream's ``resolved_*`` locals).
    src = _run_sync_source()
    assert re.search(r'resolved_provider\s*=\s*getattr\(agent,\s*"provider"', src), (
        "run_sync no longer resolves the served provider — the footer's "
        "provider/model field will silently degrade to the bare model"
    )
    assert '"provider": resolved_provider' in src, (
        "the run_sync usage dict (shared by the success + failure results) must carry the "
        "served provider for the runtime footer"
    )
    assert "**common" in src or "common" in src, "both result paths must share the usage dict"


def test_turn_result_carries_live_reasoning_config():
    src = _run_sync_source()
    assert '"reasoning_config": getattr(agent, "reasoning_config"' in src, (
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
    assert "served_provider=resolved_provider" in call
    assert "served_model=resolved_model" in call
