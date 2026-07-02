import asyncio
import concurrent.futures

import pytest

from agent.inactivity_watch import (
    InactivityDiagnostic,
    is_idle_past_limit,
    wait_for_future_or_inactivity,
    wait_for_task_or_inactivity,
    build_activity_diagnostic,
)


class FakeAgent:
    def __init__(self, seconds_since_activity=0.0):
        self.seconds_since_activity = seconds_since_activity

    def get_activity_summary(self):
        return {
            "last_activity_desc": "stream_delta",
            "seconds_since_activity": self.seconds_since_activity,
            "current_tool": "web_search",
            "api_call_count": 7,
            "max_iterations": 90,
        }


class BareAgent:
    pass


def test_idle_past_limit_fires():
    assert is_idle_past_limit(FakeAgent(seconds_since_activity=6.0), 5.0) is True


def test_active_agent_never_fires():
    assert is_idle_past_limit(FakeAgent(seconds_since_activity=4.9), 5.0) is False


def test_no_tracker_falls_back_to_not_idle_and_default_diagnostics():
    assert is_idle_past_limit(BareAgent(), 0.1) is False
    assert build_activity_diagnostic(BareAgent()) == InactivityDiagnostic(
        last_activity_desc="unknown",
        seconds_since_activity=0,
        current_tool=None,
        api_call_count=0,
        max_iterations=0,
    )


def test_sync_wait_reports_inactivity_timeout_for_pending_future():
    future = concurrent.futures.Future()

    result = wait_for_future_or_inactivity(
        future,
        agent=FakeAgent(seconds_since_activity=6.0),
        inactivity_limit=5.0,
        poll_interval=0.0,
    )

    assert result.timed_out is True
    assert result.result is None


@pytest.mark.asyncio
async def test_async_wait_reports_inactivity_timeout_for_pending_task():
    future = asyncio.Future()

    result = await wait_for_task_or_inactivity(
        future,
        get_agent=lambda: FakeAgent(seconds_since_activity=6.0),
        inactivity_limit=5.0,
        poll_interval=0.0,
    )

    assert result.timed_out is True
    assert result.result is None
