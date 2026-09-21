"""Full-loop regression: a successful response with NO usage payload is accounted
— and does NOT push an unmeasured zero into any durable or display sink.

Two FleetReview rounds shaped this file.

r3 finding 5 (`Missing Usage`): the per-call commit block was gated on
``response.usage`` being truthy, so an omitted payload never reached session
counters, ``last_turn_usage``, the Blackbox ``_turn_calls`` accumulator,
pricing, or persistence — the call was reported as if it had never happened.

r4 findings 1-3 (`last_turn_* = 0`, `cost_status` downgrade, compressor
zeroing): widening that guard then pushed the UNKNOWN's PLACEHOLDER ZEROS into
sinks that carry no UNKNOWN discriminator, which is the same silent-zero defect
one level further down — and worse, because the persisted ``last_turn_*``
snapshot is written ``COALESCE(?, existing)``, so a 0 DESTROYS the previous
turn's real measurement.

These pins therefore drive the real ``AIAgent.run_conversation()`` against a
REAL ``SessionDB`` over TWO turns — measured, then usage-less. A ``MagicMock``
session_db cannot observe this class (it records the call but not its effect on
a row), and a single turn has no prior measurement to destroy.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

_SESSION_ID = "usageless-session"


def _response(*, usage=None, content="successful response"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    kwargs = {"choices": [choice], "model": "test/model"}
    if usage is not None:
        kwargs["usage"] = SimpleNamespace(**usage)
    return SimpleNamespace(**kwargs)


_MEASURED = {"prompt_tokens": 4000, "completion_tokens": 120, "total_tokens": 4120}


def _make_agent(session_db, response):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=_SESSION_ID,
            platform="telegram",
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = response
    # Pin a route that HAS a pricing entry, so "unpriceable" can only come from
    # the usage being unknown rather than from a missing catalog entry.
    agent.model = "gpt-4o"
    agent.provider = "openai"
    agent.base_url = None
    return agent


@pytest.fixture
def real_session_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def _set_response(agent, response):
    agent.client.chat.completions.create.return_value = response


def _persisted_snapshot(db):
    db.flush_token_counts()
    return db.get_last_turn_usage(_SESSION_ID)


@pytest.fixture
def captured_turn_usage(monkeypatch):
    """Capture the ``turn_usage`` kwarg the on_session_end hook receives."""
    import hermes_cli.lifecycle as lifecycle

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen["turn_usage"] = kwargs.get("turn_usage")

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    return seen


def test_usageless_success_reaches_the_real_accounting_path(
    captured_turn_usage, real_session_db
):
    """The whole commit block runs, carrying aggregate UNKNOWN rather than nothing."""
    agent = _make_agent(real_session_db, _response(usage=None))

    result = agent.run_conversation("hello")

    assert result["final_response"] == "successful response"
    # The call happened and is counted — not silently dropped.
    assert agent.session_api_calls == 1
    # Live last-turn snapshot exists and declares itself unmeasured.
    assert agent.last_turn_usage is not None
    assert agent.last_turn_usage["usage_unknown"] is True
    # Pricing refused rather than inventing a measured $0 spend. No prior
    # priced dollars exist on this session, so "unknown" is the honest label.
    assert agent.session_cost_status == "unknown"
    assert agent.session_estimated_cost_usd == 0
    # Blackbox per-turn accumulator saw the physical call, flagged unknown.
    turn_usage = captured_turn_usage["turn_usage"]
    assert turn_usage is not None
    assert turn_usage["api_calls"] == 1
    assert turn_usage["usage_unknown"] is True
    assert len(turn_usage["calls"]) == 1
    assert turn_usage["calls"][0]["usage_unknown"] is True


def test_usageless_turn_does_not_destroy_the_persisted_last_turn_snapshot(
    real_session_db,
):
    """r4 finding 1: COALESCE(0, existing) is 0 — an UNKNOWN must write NULL."""
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")

    measured = _persisted_snapshot(real_session_db)
    assert measured is not None
    assert measured["input_tokens"] == 4000
    assert measured["output_tokens"] == 120

    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    after = _persisted_snapshot(real_session_db)
    assert after is not None
    assert after["input_tokens"] == 4000, (
        "an unmeasured call must not overwrite the previous turn's real "
        "persisted split with an unmeasured zero"
    )
    assert after["output_tokens"] == 120


def test_persisted_usage_card_still_renders_the_real_total(real_session_db):
    """The evicted-agent lane must not render a measured-looking 0."""
    from gateway.slash_commands import render_thin_last_turn_lines

    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")
    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    snapshot = _persisted_snapshot(real_session_db)
    text = "\n".join(render_thin_last_turn_lines(snapshot, "persisted"))
    assert "Total (billed in+out): 4,120" in text
    assert "Total (billed in+out): 0" not in text


def test_usageless_turn_does_not_relabel_a_session_with_priced_dollars(
    real_session_db,
):
    """r4 finding 2: one unpriceable call is `partial`, not wholly `unknown`."""
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")

    assert agent.session_cost_status == "estimated"
    priced = agent.session_estimated_cost_usd
    assert priced > 0

    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    assert agent.session_estimated_cost_usd == priced
    assert agent.session_cost_status == "partial", (
        "a session that already holds real priced dollars is incomplete, not "
        "wholly unmeasured; `unknown` is outside the repricing allowlist and "
        "would never heal"
    )


def test_usageless_turn_does_not_zero_the_context_compressor(real_session_db):
    """r4 finding 3: update_from_response() assigns display fields unconditionally."""
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")

    assert agent.context_compressor.last_prompt_tokens == 4000

    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    assert agent.context_compressor.last_prompt_tokens == 4000, (
        "an unmeasured response must not overwrite the context meter / "
        "persisted context_used with a measured-looking zero"
    )


def test_usageless_turn_does_not_zero_the_blackbox_context_used(
    captured_turn_usage, real_session_db
):
    """The compressor is not the only reader — pin the DURABLE sink too.

    ``turn_finalizer`` reads ``context_compressor.last_prompt_tokens`` for both
    the session entry's ``last_prompt_tokens`` and the Blackbox turn record's
    ``context_used``. Asserting only the compressor attribute leaves the two
    sinks the class-sweep actually named covered by derivation rather than by
    execution, so pin the value where it lands.
    """
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")
    assert captured_turn_usage["turn_usage"]["context_used"] == 4000

    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    assert captured_turn_usage["turn_usage"]["context_used"] == 4000, (
        "an unmeasured response must not write a measured-looking 0 into the "
        "Blackbox turn ledger's context_used"
    )


def test_a_present_payload_with_a_null_prompt_count_does_not_zero_the_compressor(
    captured_turn_usage, real_session_db
):
    """r6 finding 7: the gate was a PRESENCE test, not a MEASUREMENT test.

    The sibling tests above all omit ``usage`` entirely, so ``if
    getattr(response, "usage", None):`` short-circuited and the compressor was
    protected by accident. A provider that DOES send a usage object but nulls
    the prompt count (``prompt_tokens: null``, or an explicit
    ``prompt_tokens_unavailable``) sails through a presence test and stamps
    ``last_prompt_tokens`` — and therefore the status-bar context meter, the
    persisted session entry, and the Blackbox ``context_used`` — to 0, which is
    the previous real occupancy reading destroyed.
    """
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")
    assert agent.context_compressor.last_prompt_tokens == 4000
    assert captured_turn_usage["turn_usage"]["context_used"] == 4000

    _set_response(
        agent,
        _response(usage={"prompt_tokens": None, "completion_tokens": 50,
                         "total_tokens": None}),
    )
    agent.run_conversation("turn two")

    assert agent.context_compressor.last_prompt_tokens == 4000, (
        "a PRESENT usage object whose prompt count is null is still unmeasured "
        "— it must not overwrite the real context reading with a zero"
    )
    assert captured_turn_usage["turn_usage"]["context_used"] == 4000


def test_an_output_only_unknown_still_updates_the_compressor(real_session_db):
    """Narrowness control for r6 finding 7.

    The gate uses ``prompt_tokens_unknown``, not ``total_tokens_unknown``, so
    an unmeasured OUTPUT bucket must NOT discard a perfectly good prompt
    occupancy reading. Without this control the fix could be bought by simply
    refusing more often.
    """
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")
    assert agent.context_compressor.last_prompt_tokens == 4000

    _set_response(
        agent,
        _response(usage={"prompt_tokens": 7777, "completion_tokens": None,
                         "total_tokens": None}),
    )
    agent.run_conversation("turn two")

    assert agent.context_compressor.last_prompt_tokens == 7777, (
        "the prompt count WAS measured; only the output bucket was unknown, so "
        "the context meter must advance"
    )


def test_usageless_turn_does_not_drag_the_velocity_average(real_session_db):
    """An unmeasured output is not 0 tok/s — the deques carry no discriminator."""
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")

    outputs = list(agent._api_output_history)
    latencies = list(agent._api_latency_history)
    assert outputs == [120]

    _set_response(agent, _response(usage=None))
    agent.run_conversation("turn two")

    assert list(agent._api_output_history) == outputs
    assert list(agent._api_latency_history) == latencies


def test_usageless_turn_renders_unknown_not_a_measured_zero(real_session_db):
    """The shipped /usage thin renderer must refuse to present the zeros."""
    from gateway.slash_commands import render_thin_last_turn_lines

    agent = _make_agent(real_session_db, _response(usage=None))
    agent.run_conversation("hello")

    text = "\n".join(render_thin_last_turn_lines(agent.last_turn_usage, "resident"))
    assert "Total (billed in+out): unknown" in text
    assert "Total (billed in+out): 0" not in text


def test_measured_response_control_is_unaffected(
    captured_turn_usage, real_session_db
):
    """Control: a normal measured response still accounts exactly as before."""
    agent = _make_agent(
        real_session_db,
        _response(usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}),
    )

    agent.run_conversation("hello")

    assert agent.session_api_calls == 1
    assert agent.session_total_tokens == 18
    assert agent.last_turn_usage["usage_unknown"] is False
    assert agent.session_cost_status != "unknown"
    assert _persisted_snapshot(real_session_db) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    turn_usage = captured_turn_usage["turn_usage"]
    assert turn_usage["usage_unknown"] is False
    assert turn_usage["output_tokens"] == 7


def test_two_measured_turns_still_advance_the_snapshot(real_session_db):
    """Control: retaining on UNKNOWN must not freeze the snapshot generally."""
    agent = _make_agent(real_session_db, _response(usage=_MEASURED))
    agent.run_conversation("turn one")

    _set_response(
        agent,
        _response(usage={"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}),
    )
    agent.run_conversation("turn two")

    after = _persisted_snapshot(real_session_db)
    assert after["input_tokens"] == 50
    assert after["output_tokens"] == 9


def test_no_call_normalization_still_means_a_known_zero():
    """Settled contract control: ``normalize_usage(None)`` is not an unknown."""
    from agent.usage_pricing import normalize_usage

    no_call = normalize_usage(None)
    assert no_call.usage_unknown is False
    assert no_call.total_tokens_unknown is False
    assert no_call.total_tokens == 0
