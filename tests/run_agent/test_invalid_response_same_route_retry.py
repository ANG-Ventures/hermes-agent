"""Same-route retry of an empty tool_use 200 (t_d35beb85).

A billed response with ``stop_reason=tool_use`` and an EMPTY ``content`` list
(17x/24 h on claude-alr, 6 seats) is retried ONCE on the same route before the
loop fails over, and every occurrence is counted in
``state/model-route-changes.log`` with its retry outcome. Drives the real
``run_conversation`` loop against a mocked client.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.run_agent._run_agent_helpers import _mock_response


def _empty_tool_use(out=462, seat="sub-vps-2", relay_retried=None):
    ph = {"x-pool-served-by": seat,
          "x-pool-route-id": "9ed7daf6460344d4b0e3f9f0bbcd2888"}
    if relay_retried is not None:
        ph["x-pool-empty-content-retried"] = relay_retried
    return SimpleNamespace(
        content=[], stop_reason="tool_use", model="claude-fable-5-1",
        usage=SimpleNamespace(output_tokens=out, input_tokens=10,
                              prompt_tokens=10, completion_tokens=out, total_tokens=10 + out),
        pool_headers=ph,
    )


def _fast_time():
    t = [1000.0]

    def _adv():
        t[0] += 500.0
        return t[0]

    m = MagicMock()
    m.time.side_effect = _adv
    m.sleep = MagicMock()
    m.monotonic.return_value = 12345.0
    return m


def _run(agent, responses):
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = list(responses)
    from agent import conversation_loop as _cl
    from agent import turn_response_check as _trc

    # The invalid-response branch lives in agent.turn_response_check on this fork
    # (extracted from conversation_loop); its backoff comes from agent.retry_utils.
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.time", _fast_time()),
        patch.object(_cl, "time", _fast_time()),
        patch.object(_trc, "time", _fast_time()),
        patch("agent.retry_utils.jittered_backoff", lambda *a, **k: 0.0),
    ):
        return agent.run_conversation("hello")


def _count_rows():
    p = os.path.join(os.environ["HERMES_HOME"], "state", "model-route-changes.log")
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p, encoding="utf-8").read().splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == "invalid_response":
            rows.append(dict(t.split("=", 1) for t in parts[2:]))
    return rows


def test_empty_tool_use_retries_same_route_then_succeeds(agent):
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    with patch.object(agent, "_try_activate_fallback", return_value=False) as fo:
        result = _run(agent, [_empty_tool_use(), _mock_response(content="ok")])
    assert result.get("final_response") == "ok", result
    assert agent.client.chat.completions.create.call_count == 2
    fo.assert_not_called()  # recovered in place: no cross-provider failover
    rows = _count_rows()
    assert [r["retry_outcome"] for r in rows] == ["retry_ok"]
    assert rows[0]["served_by"] == "sub-vps-2" and rows[0]["output_tokens"] == "462"


def test_empty_tool_use_repeat_counts_retry_same_and_fails_over(agent):
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    seen = []

    def _fo(*a, **k):
        pend = getattr(agent, "_pending_fallback_error", None) or {}
        seen.append(dict(pend.get("floor") or {}))
        return False

    with patch.object(agent, "_try_activate_fallback", side_effect=_fo):
        result = _run(agent, [_empty_tool_use(out=n) for n in (462, 377, 1, 1, 1, 1)])
    assert result.get("failed") is True
    # First failover attempt comes right after the one same-route retry, and
    # carries the repeat mark (skip same-provider entries).
    assert seen and seen[0].get("repeat") is True
    rows = _count_rows()
    assert rows[0]["retry_outcome"] == "retry_same"
    assert rows[0]["output_tokens"] == "462"  # the row is the FIRST occurrence


def test_other_invalid_shape_keeps_eager_fallback(agent):
    """choices=[] (chat-completions empty) is not the retried shape."""
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    bad = SimpleNamespace(choices=[], model="m", usage=None)
    calls = []
    with patch.object(agent, "_try_activate_fallback",
                      side_effect=lambda *a, **k: calls.append(1) or False):
        _run(agent, [bad] * 6)
    # The first invalid response went straight to the failover attempt.
    assert calls and agent.client.chat.completions.create.call_count >= 1
    assert all(r["retry_outcome"] == "fallback" for r in _count_rows())


def test_relay_gave_up_skips_same_route_retry_and_fails_over(agent):
    """t_9d411670: the relay (claude-pool #193) already retried the same seat
    and one other seat. The harness must not re-enter that ladder: the FIRST
    invalid response goes to the failover, marked repeat (skip this provider)."""
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    seen = []

    def _fo(*a, **k):
        pend = getattr(agent, "_pending_fallback_error", None) or {}
        seen.append((agent.client.chat.completions.create.call_count,
                     dict(pend.get("floor") or {})))
        return False

    with patch.object(agent, "_try_activate_fallback", side_effect=_fo):
        _run(agent, [_empty_tool_use(relay_retried="gave_up")]
             + [_empty_tool_use(out=1)] * 5)
    assert seen, "no failover attempted"
    calls_at_first_failover, floor = seen[0]
    assert calls_at_first_failover == 1  # no same-route retry before failover
    assert floor.get("repeat") is True
    rows = _count_rows()
    assert rows[0]["retry_outcome"] == "relay_gave_up"
    assert "retry_same" not in [r["retry_outcome"] for r in rows]


def test_relay_absorbed_header_keeps_same_route_retry(agent):
    """Only ``gave_up`` short-circuits; any other header value keeps the
    one warm same-route retry (the relay header is advisory)."""
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    with patch.object(agent, "_try_activate_fallback", return_value=False) as fo:
        result = _run(agent, [_empty_tool_use(relay_retried="1"),
                              _mock_response(content="ok")])
    assert result.get("final_response") == "ok", result
    fo.assert_not_called()
    assert [r["retry_outcome"] for r in _count_rows()] == ["retry_ok"]


def test_relay_gave_up_row_carries_the_relay_ladder(agent):
    """t_3afed4f8: the 2026-10-09 19:30:54 give-up. The banner said ``×4 from
    local ... rotate→sub-vps-11`` while the row only had served_by=sub-vps-11.
    The row carries the attempt count, every billed seat in order, the ladder
    and the request ids, consistent with the rider built from the same floor."""
    from agent import fallback_policy as fp

    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    gave_up = _empty_tool_use(out=277, seat="sub-vps-11", relay_retried="gave_up")
    gave_up.pool_headers.update({
        "x-pool-empty-content-attempts": "local,local,local,sub-vps-11",
        "x-pool-empty-content-perturbations": "none,none,drop_fgts,drop_fgts+rotate",
        "x-pool-empty-content-request-ids": "req_a1,req_b2,req_c3,req_d4",
    })
    floors = []

    def _fo(*a, **k):
        floors.append(dict((getattr(agent, "_pending_fallback_error", None) or {}).get("floor") or {}))
        return False

    with patch.object(agent, "_try_activate_fallback", side_effect=_fo):
        _run(agent, [gave_up] + [_empty_tool_use(out=1)] * 5)
    row = [r for r in _count_rows() if r["retry_outcome"] == "relay_gave_up"][0]
    assert row["served_by"] == "sub-vps-11"  # the seat that answered last
    assert row["relay_attempts"] == "4"
    assert row["relay_seats"] == "local,local,local,sub-vps-11"
    assert row["relay_ladder"] == "none,none,drop_fgts,drop_fgts+rotate"
    assert row["relay_request_ids"] == "req_a1,req_b2,req_c3,req_d4"
    # Same floor -> the rider says the same count and the same first seat.
    rider = fp.format_cause_rider({"trigger_class": fp.INVALID_RESPONSE_CLASS,
                                   "from_provider": "claude-alr", "floor": floors[0]})
    assert f"×{row['relay_attempts']} from {row['relay_seats'].split(',')[0]} " in rider, rider
    assert "rotate→sub-vps-11 −fgts" in rider, rider


def test_row_without_relay_headers_has_no_relay_tokens(agent):
    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    with patch.object(agent, "_try_activate_fallback", return_value=False):
        _run(agent, [_empty_tool_use(relay_retried="gave_up")] + [_empty_tool_use(out=1)] * 5)
    row = _count_rows()[0]
    assert row["retry_outcome"] == "relay_gave_up"
    assert not {"relay_attempts", "relay_seats", "relay_ladder", "relay_request_ids"} & set(row)


def test_relay_gave_up_response_is_logged_rejected_once(agent, caplog):
    """The repeat-mark re-stash of the same response must not log it twice."""
    import logging

    agent._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    caplog.set_level(logging.WARNING, logger="agent.fallback_events")
    with patch.object(agent, "_try_activate_fallback", return_value=False):
        _run(agent, [_empty_tool_use(out=277, relay_retried="gave_up")]
             + [_empty_tool_use(out=1)] * 5)
    hits = [r for r in caplog.records
            if "rejected by the loop" in r.getMessage() and "output_tokens=277" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in hits]
