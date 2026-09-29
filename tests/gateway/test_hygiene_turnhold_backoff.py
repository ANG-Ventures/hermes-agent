"""Turn-hold deferrals back off per session and reset on a real compaction.

t_139733d1: a transcript too large to summarise inside the turn-hold budget
defers on EVERY attempt, and each attempt re-buys the summary model's prompt
ingestion for the whole transcript. A flat 60s retry-after made that per-turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.run import GatewayRunner, _hygiene_turnhold_retry_seconds


def test_turnhold_retry_backs_off_per_session_and_caps():
    gw = SimpleNamespace()
    delays = [_hygiene_turnhold_retry_seconds(gw, "s1") for _ in range(6)]
    assert delays[0] == gateway_run._HYGIENE_TURNHOLD_RETRY_SECONDS
    assert all(b > a for a, b in zip(delays[:3], delays[1:4])), delays
    assert max(delays) == gateway_run._HYGIENE_COOLDOWN_MAX_SECONDS
    # Independent per session.
    assert _hygiene_turnhold_retry_seconds(gw, "s2") == delays[0]


def test_real_compaction_resets_turnhold_backoff():
    runner = object.__new__(GatewayRunner)
    first = _hygiene_turnhold_retry_seconds(runner, "s1")
    assert _hygiene_turnhold_retry_seconds(runner, "s1") > first
    runner._clear_hygiene_compression_failures("s1")
    assert _hygiene_turnhold_retry_seconds(runner, "s1") == first
