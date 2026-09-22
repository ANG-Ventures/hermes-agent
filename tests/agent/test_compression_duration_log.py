"""Compaction summariser calls must emit one measurable duration line.

The gap this closes: ``compression_attempt`` telemetry is populated by
``ContextCompressor._record_aux_compression_call``, so an install running a context-engine
plugin (which calls ``auxiliary_client.call_llm(task="compression")`` directly) logged an
envelope with no provider, no model and no auxiliary duration. A six-minute compaction was
unattributable. The duration line rides the ``call_llm`` choke point instead, which every
context engine funnels through.

Behaviour contracts only — no frozen wording beyond the grep-stable event prefix, which
monitors key on and is therefore part of the contract on purpose.
"""

import asyncio
import logging

import pytest

from agent.compression_duration_log import (
    DURATION_EVENT,
    format_duration_line,
    should_log_duration,
)


class TestShouldLogDuration:
    """Only the compaction summariser earns a standing per-call line."""

    def test_compression_task_logs(self):
        assert should_log_duration("compression") is True

    @pytest.mark.parametrize("task", ["vision", "title", "session_search", "extraction"])
    def test_other_auxiliary_tasks_do_not_log(self, task):
        # A line per call for every auxiliary task is log spam, not telemetry.
        assert should_log_duration(task) is False

    @pytest.mark.parametrize("task", [None, "", "   "])
    def test_missing_task_does_not_log(self, task):
        assert should_log_duration(task) is False

    @pytest.mark.parametrize("task", ["Compression", "COMPRESSION", "  compression  "])
    def test_task_match_is_normalized(self, task):
        # A padded or differently-cased task name must not silently drop telemetry.
        assert should_log_duration(task) is True


class TestFormatDurationLine:
    """The line must carry the four fields the card asked for, and the budget."""

    def test_carries_provider_model_attempts_and_seconds(self):
        line = format_duration_line(
            provider="claude-apr", model="claude-opus-5", attempts=1,
            seconds=7.99, outcome="ok",
        )
        assert line.startswith(DURATION_EVENT)
        for field in ("provider=claude-apr", "model=claude-opus-5", "attempts=1",
                      "seconds=8.0", "outcome=ok"):
            assert field in line

    def test_budget_is_rendered_when_known(self):
        # Elapsed alone cannot show a deadline kill; the budget next to it can.
        line = format_duration_line(
            provider="p", model="m", attempts=2, seconds=300.4, outcome="failed",
            budget_seconds=300.0,
        )
        assert "budget=300s" in line
        assert "attempts=2" in line

    @pytest.mark.parametrize("budget", [None, 0, 0.0, -1])
    def test_absent_or_nonpositive_budget_is_omitted_not_faked(self, budget):
        # A wrong budget is worse than an absent one for a deadline judgement.
        line = format_duration_line(
            provider="p", model="m", attempts=1, seconds=1.0, outcome="ok",
            budget_seconds=budget,
        )
        assert "budget=" not in line

    def test_unknown_route_is_labelled_not_blank(self):
        line = format_duration_line(
            provider="", model=None, attempts=1, seconds=0.0, outcome="ok",
        )
        assert "provider=unknown" in line
        assert "model=unknown" in line

    def test_attempts_floored_at_one(self):
        # A served call made at least one physical request; 0 would be a lie.
        line = format_duration_line(
            provider="p", model="m", attempts=0, seconds=1.0, outcome="ok",
        )
        assert "attempts=1" in line

    def test_negative_seconds_clamped(self):
        line = format_duration_line(
            provider="p", model="m", attempts=1, seconds=-5.0, outcome="ok",
        )
        assert "seconds=0.0" in line


def _compression_lines(caplog):
    return [r.getMessage() for r in caplog.records if DURATION_EVENT in r.getMessage()]


class TestCallLlmEmitsDuration:
    """The wiring: a real ``call_llm`` invocation must produce the line."""

    @pytest.fixture
    def stub_impl(self, monkeypatch):
        """Replace only the provider request, keeping the real ``call_llm`` wrapper."""
        from agent import auxiliary_client

        def _install(side_effect=None, result="summary"):
            def _impl(**kwargs):
                if side_effect is not None:
                    raise side_effect
                return result
            monkeypatch.setattr(auxiliary_client, "_call_llm_impl", _impl)
            return auxiliary_client

        return _install

    def test_successful_compression_call_logs_duration(self, stub_impl, caplog):
        aux = stub_impl()
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            aux.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
        lines = _compression_lines(caplog)
        assert len(lines) == 1, lines
        assert "outcome=ok" in lines[0]
        assert "seconds=" in lines[0]

    def test_failed_compression_call_still_logs_duration(self, stub_impl, caplog):
        # The stall/failure case is the one actually worth measuring; if only the happy
        # path logged, the expensive event would stay invisible.
        aux = stub_impl(side_effect=TimeoutError("agy deadline exceeded"))
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            with pytest.raises(TimeoutError):
                aux.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
        lines = _compression_lines(caplog)
        assert len(lines) == 1, lines
        assert "outcome=failed" in lines[0]

    def test_non_compression_task_logs_nothing(self, stub_impl, caplog):
        aux = stub_impl()
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            aux.call_llm(task="vision", messages=[{"role": "user", "content": "x"}])
        assert _compression_lines(caplog) == []

    def test_route_snapshot_reports_the_route_that_served_the_call(self, monkeypatch, caplog):
        """A fallback must be visible: the line names the ROUTE THAT SERVED, not the one asked for.

        Regression: the first cut of this feature read only the relay context, which
        ``_set_relay_auxiliary_route`` stamps ONCE at dispatch. A live dead-primary E2E
        printed ``provider=custom`` while ``claude-apr`` actually did the work — telemetry
        that would point the next investigation at the wrong backend.
        """
        from agent import auxiliary_client

        def _impl(**kwargs):
            # Mirror what the recovery ladder does on a fallback rung: record the route
            # that actually served into route_info (NOT the relay context).
            auxiliary_client._record_route_info(
                kwargs.get("route_info"), "served-prov", "served-model")
            return "summary"

        monkeypatch.setattr(auxiliary_client, "_call_llm_impl", _impl)
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            auxiliary_client.call_llm(
                task="compression", provider="stalled-prov",
                messages=[{"role": "user", "content": "x"}],
            )
        lines = _compression_lines(caplog)
        assert len(lines) == 1, lines
        assert "provider=served-prov" in lines[0]
        assert "model=served-model" in lines[0]
        assert "stalled-prov" not in lines[0]

    def test_attribution_works_without_caller_supplied_route_info(self, monkeypatch, caplog):
        """Context engines (LCM) pass no ``route_info`` — and they are exactly the callers
        whose compactions were unattributable. The route dict must be allocated internally,
        so a fallback is still named for a caller that never opted in."""
        from agent import auxiliary_client

        seen = {}

        def _impl(**kwargs):
            seen["route_info"] = kwargs.get("route_info")
            auxiliary_client._record_route_info(kwargs.get("route_info"), "fb-prov", "fb-model")
            return "summary"

        monkeypatch.setattr(auxiliary_client, "_call_llm_impl", _impl)
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            # No route_info kwarg at all — the LCM call shape.
            auxiliary_client.call_llm(
                task="compression", messages=[{"role": "user", "content": "x"}],
            )
        assert isinstance(seen["route_info"], dict), "route_info must be allocated, not None"
        lines = _compression_lines(caplog)
        assert len(lines) == 1, lines
        assert "provider=fb-prov" in lines[0]

    def test_caller_supplied_route_info_is_still_populated(self, monkeypatch):
        """Allocating internally must not stop a caller's own dict from being filled."""
        from agent import auxiliary_client

        def _impl(**kwargs):
            auxiliary_client._record_route_info(kwargs.get("route_info"), "p", "m")
            return "summary"

        monkeypatch.setattr(auxiliary_client, "_call_llm_impl", _impl)
        caller_dict: dict = {}
        auxiliary_client.call_llm(
            task="compression", messages=[{"role": "user", "content": "x"}],
            route_info=caller_dict,
        )
        assert caller_dict == {"provider": "p", "model": "m"}

    def test_telemetry_failure_never_breaks_the_call(self, monkeypatch, caplog):
        """Best-effort by contract: a broken logger must not fail a working compaction."""
        from agent import auxiliary_client

        monkeypatch.setattr(auxiliary_client, "_call_llm_impl", lambda **kw: "summary")
        monkeypatch.setattr(
            auxiliary_client, "_relay_auxiliary_route_snapshot",
            lambda: (_ for _ in ()).throw(RuntimeError("snapshot exploded")),
        )
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            assert auxiliary_client.call_llm(
                task="compression", messages=[{"role": "user", "content": "x"}],
            ) == "summary"
        assert _compression_lines(caplog) == []

    def test_async_path_has_duration_parity(self, monkeypatch, caplog):
        """An engine summarising on the async client must not be a blind spot."""
        from agent import auxiliary_client

        async def _impl(**kwargs):
            return "summary"

        monkeypatch.setattr(auxiliary_client, "_async_call_llm_impl", _impl)
        with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
            asyncio.run(auxiliary_client.async_call_llm(
                task="compression", messages=[{"role": "user", "content": "x"}],
            ))
        lines = _compression_lines(caplog)
        assert len(lines) == 1, lines
        assert "outcome=ok" in lines[0]
