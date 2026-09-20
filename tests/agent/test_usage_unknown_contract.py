"""UNKNOWN USAGE MUST SURVIVE THE CONSUMER — contract pins.

THE DEFECT THIS CLOSES (kanban card t_83d8ce73). claude-bpx PR #186 makes the
bridge egress an honest unknown when its CLI transcript reconciliation misses
(measured: ~40% of parallel-batch turns). Its wire shape, copied verbatim from
that PR's `bridge/test/unit-usage-unknown-sink.test.js` / `docs/SPEC-parallel-
stop-usage-undercount.md`:

    {"prompt_tokens": 150, "completion_tokens": null, "total_tokens": null,
     "output_tokens_unavailable": true, "prompt_tokens_details": {...}}

Before this card, hermes-agent's `normalize_usage -> _usage_count -> _to_int`
did `int(value or 0)` and ignored the discriminator, so the explicit null landed
as a canonical output of **0** and the turn logged `out=0` — indistinguishable
from a measured dead call, and a *harder* misread than the undercount it
replaced. That 0 then flowed into the pricing lane, the blackbox turn ledger and
every /usage renderer as if it had been measured.

The pins, mirroring the producer-side UUS-* numbering:

  UC-1  null + unavailable -> UNKNOWN (never a measured 0)
  UC-2  a real integer -> unchanged (no regression for measuring providers)
  UC-3  a MISSING usage object -> unknown-free zeros, not a false unknown
  UC-4  a provider that never sends the discriminator keeps integers
  UC-5  unknown is ABSORBING across a sum (a total missing a term is unknown)
  UC-6  pricing REFUSES an unknown turn (status=unknown, amount None)
  UC-7  the blackbox turn ledger persists the discriminator, never a bare 0
  UC-8  every renderer reads "unknown", never "0", via the SHARED display rule

MUTATION (card requirement 4): reinstating the `or 0` coercion — i.e. dropping
the `output_tokens_unknown` discriminator from `normalize_usage` — turns UC-1,
UC-5, UC-6, UC-7 and UC-8 RED.
"""

from __future__ import annotations

import pytest

from agent.usage_pricing import (
    UNKNOWN_TOKENS_LABEL,
    CanonicalUsage,
    estimate_usage_cost,
    format_token_count,
    normalize_usage,
)


# The bridge's exact egress payload on the unknown path (server.js usagePayload).
BRIDGE_UNKNOWN_WIRE = {
    "prompt_tokens": 150,
    "completion_tokens": None,
    "total_tokens": None,
    "output_tokens_unavailable": True,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
}

# The same bridge, measured path. Byte-identical to the pre-card payload: no
# discriminator key at all (UUS-8 pins that on the producer side).
BRIDGE_MEASURED_WIRE = {
    "prompt_tokens": 150,
    "completion_tokens": 118,
    "total_tokens": 268,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
}


class _ModelExtraUsage:
    """A typed SDK usage object, as the OpenAI client actually delivers it.

    The client's lenient `construct_type` path parks unrecognised wire keys in
    ``model_extra`` rather than on the model, so the discriminator is NOT a
    plain attribute. Measured in PR #186 review round 2 against openai 2.28.0.
    """

    def __init__(self, **fields):
        self.model_extra = fields.pop("model_extra", {})
        for key, value in fields.items():
            setattr(self, key, value)


def test_uc1_null_plus_unavailable_is_unknown_not_zero():
    usage = normalize_usage(
        BRIDGE_UNKNOWN_WIRE, provider="custom", api_mode="chat_completions"
    )

    assert usage.output_tokens_unknown is True, (
        "the bridge's explicit unknown must survive as an UNKNOWN state; "
        "a null coerced to 0 is indistinguishable from a measured dead call"
    )
    assert usage.total_tokens_unknown is True, (
        "a total missing its whole output term is not a measurement"
    )
    # The input side settled at request time and is still a real measurement.
    assert usage.input_tokens == 150


def test_uc1b_discriminator_in_model_extra_is_read():
    """The typed-SDK path: the flag arrives in model_extra, not as an attr."""
    usage = normalize_usage(
        _ModelExtraUsage(
            prompt_tokens=150,
            completion_tokens=None,
            total_tokens=None,
            model_extra={"output_tokens_unavailable": True},
        ),
        provider="custom",
        api_mode="chat_completions",
    )
    assert usage.output_tokens_unknown is True


def test_uc2_a_real_integer_is_unchanged():
    usage = normalize_usage(
        BRIDGE_MEASURED_WIRE, provider="custom", api_mode="chat_completions"
    )

    assert usage.output_tokens == 118
    assert usage.input_tokens == 150
    assert usage.total_tokens == 268
    assert usage.output_tokens_unknown is False, (
        "a measured turn must not be flagged unknown — the discriminator is "
        "unknown-only, so no consumer has to learn to ignore a new state"
    )


@pytest.mark.parametrize("missing", [None, {}, ""])
def test_uc3_a_missing_usage_object_is_not_a_false_unknown(missing):
    """ABSENT != UNKNOWN.

    A response that carries no usage object at all is not a provider declaring
    an unknown — it is a provider saying nothing. Flagging it unknown would
    poison every no-usage path (streaming stubs, empty-guard probes) that has
    always meant zero here.
    """
    usage = normalize_usage(missing)

    assert usage.output_tokens == 0
    assert usage.output_tokens_unknown is False


def test_uc4_providers_without_the_discriminator_keep_integers():
    """Backward compatibility: every pre-existing provider is untouched."""
    anthropic = normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
        },
        provider="anthropic",
        api_mode="anthropic_messages",
    )
    assert (anthropic.input_tokens, anthropic.output_tokens) == (100, 50)
    assert anthropic.output_tokens_unknown is False

    codex = normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 20},
        },
        api_mode="codex_responses",
    )
    assert codex.output_tokens == 50
    assert codex.output_tokens_unknown is False

    openai = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 50},
        provider="openai",
        api_mode="chat_completions",
    )
    assert openai.output_tokens == 50
    assert openai.output_tokens_unknown is False


def test_uc5_unknown_is_absorbing_across_a_sum():
    """A sum missing one side's output term is itself unmeasured."""
    measured = normalize_usage(
        BRIDGE_MEASURED_WIRE, provider="custom", api_mode="chat_completions"
    )
    unknown = normalize_usage(
        BRIDGE_UNKNOWN_WIRE, provider="custom", api_mode="chat_completions"
    )

    assert (measured + unknown).output_tokens_unknown is True
    # Order must not matter: unknown absorbs from either side.
    assert (unknown + measured).output_tokens_unknown is True
    # Two measured halves stay measured (no regression).
    assert (measured + measured).output_tokens_unknown is False
    assert (measured + measured).output_tokens == 236


def test_uc6_pricing_refuses_an_unknown_turn():
    """No cost may be computed from an unmeasured output term."""
    unknown = CanonicalUsage(
        input_tokens=150, output_tokens=0, output_tokens_unknown=True
    )
    result = estimate_usage_cost(
        "claude-sonnet-4-5", unknown, provider="anthropic"
    )

    assert result.status == "unknown"
    assert result.amount_usd is None, (
        "pricing an unmeasured output as $0 yields a dollar figure short by the "
        "whole output while LOOKING priced — strictly worse than declining"
    )

    # The identical usage WITHOUT the flag prices normally — proving the flag,
    # not the zeros, is what withholds the price.
    measured = CanonicalUsage(input_tokens=150, output_tokens=0)
    assert estimate_usage_cost(
        "claude-sonnet-4-5", measured, provider="anthropic"
    ).amount_usd is not None


def test_uc6b_blackbox_cost_refuses_and_never_prices_zero():
    """The turn ledger's pricing reconciler honours the same discriminator."""
    from plugins.blackbox.cost import compute_turn_cost

    total, status, perclass = compute_turn_cost(
        "claude-sonnet-4-5",
        "anthropic",
        None,
        [{"input_tokens": 150, "output_tokens": 0, "output_tokens_unknown": True}],
    )
    assert status == "unknown"
    assert total is None
    assert all(v is None for v in perclass.values())

    # A genuinely all-zero turn (no discriminator) is still costless, not
    # unpriced — the priced_zero fast path must not regress.
    assert compute_turn_cost(
        "claude-sonnet-4-5", "anthropic", None, [{"input_tokens": 0, "output_tokens": 0}]
    )[1] == "priced_zero"


def test_uc7_the_turn_ledger_persists_the_discriminator(tmp_path, monkeypatch):
    """A round-trip through the blackbox store keeps unknown distinguishable."""
    import plugins.blackbox.store as store
    from plugins.blackbox.record import TurnRecord

    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "blackbox" / "turns.db")

    rec = TurnRecord(
        turn_id="turn_unknown_contract",
        input_tokens=150,
        output_tokens=0,
        output_tokens_unknown=True,
        cost_usd=None,
        cost_status="unknown",
    )
    store.insert_turn(rec)
    row = store.get_turn("turn_unknown_contract")

    assert row is not None
    assert row["output_tokens_unknown"] == 1, (
        "the ledger must record that this 0 is absence of data, not a "
        "measurement — otherwise every downstream reader re-spells it measured"
    )

    measured = TurnRecord(
        turn_id="turn_measured_contract", input_tokens=150, output_tokens=118
    )
    store.insert_turn(measured)
    assert store.get_turn("turn_measured_contract")["output_tokens_unknown"] == 0


def test_uc7b_the_turn_rollup_absorbs_an_unknown_call(monkeypatch):
    """The per-call -> per-turn roll-up in turn_finalizer is unknown-absorbing.

    A turn is several API calls. If ANY call's output was unmeasured, the turn's
    SUMMED output is missing a term — so the sum is not a measurement. This pins
    the exact expression turn_finalizer builds, lifted from source, rather than
    re-implementing it here (which would test nothing).
    """
    import ast
    import inspect
    import textwrap

    import agent.turn_finalizer as tf

    src = inspect.getsource(tf)
    tree = ast.parse(src)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "output_tokens_unknown":
                found.append(value)

    assert found, (
        "turn_finalizer must roll the UNKNOWN discriminator up from the "
        "per-call accumulator into the per-turn usage dict, or the blackbox "
        "ledger records the turn's 0 output as a measurement"
    )
    expr = ast.unparse(found[0])
    # Evaluate the SHIPPED expression against real call lists.
    def rollup(calls):
        return eval(  # noqa: S307 — evaluating our own source, not user input
            textwrap.dedent(expr), {}, {"_turn_calls": calls}
        )

    measured = {"output_tokens": 118}
    unknown = {"output_tokens": 0, "output_tokens_unknown": True}
    assert rollup([measured, measured]) is False
    assert rollup([measured, unknown]) is True, "unknown must absorb"
    assert rollup([unknown, measured]) is True, "order must not matter"
    assert rollup([]) is False


def test_uc7c_the_plugin_ingest_seam_reads_the_flag():
    """blackbox._build_record must carry the flag onto the TurnRecord."""
    import plugins.blackbox as bb

    rec = bb._build_record(
        session_id="s",
        interrupted=False,
        model="claude-sonnet-4-5",
        platform="cli",
        provider="anthropic",
        user_message="",
        final_response="",
        turn_usage={
            "input_tokens": 150,
            "output_tokens": 0,
            "output_tokens_unknown": True,
            "calls": [
                {
                    "input_tokens": 150,
                    "output_tokens": 0,
                    "output_tokens_unknown": True,
                }
            ],
        },
        cfg={"store_text": False},
        kwargs={},
    )

    assert rec is not None
    assert rec.output_tokens_unknown is True
    # And the turn is left UNPRICED rather than priced from the phantom zero.
    assert rec.cost_usd is None
    assert rec.cost_status == "unknown"


def test_uc8_the_display_rule_is_shared_and_says_unknown():
    """One display rule, three renderers. None of them may print 0."""
    from plugins.blackbox.card import _tokens_out_line, humanize_tokens
    from plugins.blackbox.last_turn import _humanize_tok
    from plugins.blackbox.record import TurnRecord

    # The shared lib owns the spelling.
    assert format_token_count(0, unknown=True) == UNKNOWN_TOKENS_LABEL
    assert UNKNOWN_TOKENS_LABEL != "0"

    # Renderer 1 (alert card) and renderer 2 (/usage + /context last-turn card)
    # both route through it and agree.
    assert humanize_tokens(0, unknown=True) == UNKNOWN_TOKENS_LABEL
    assert _humanize_tok(0, unknown=True) == UNKNOWN_TOKENS_LABEL

    # And the card line itself reads unknown rather than "0 out".
    line = _tokens_out_line(
        TurnRecord(turn_id="t", output_tokens=0, output_tokens_unknown=True)
    )
    assert UNKNOWN_TOKENS_LABEL in line
    assert "0 out" not in line

    # A measured turn is unchanged.
    assert humanize_tokens(118) == "118"
    assert "118" in _tokens_out_line(TurnRecord(turn_id="t", output_tokens=118))


def test_uc8b_last_turn_card_renders_unknown_not_a_silent_omission():
    from plugins.blackbox.last_turn import render_last_turn_record

    row = {
        "turn_id": "t",
        "input_tokens": 150,
        "output_tokens": 0,
        "output_tokens_unknown": 1,
        "cost_usd": None,
        "cost_status": "unknown",
        "api_calls": 1,
    }
    block = "\n".join(render_last_turn_record(row))

    assert "Tokens out" in block, (
        "omitting the row entirely reads as 'nothing generated' — say unknown"
    )
    assert UNKNOWN_TOKENS_LABEL in block

    measured = "\n".join(
        render_last_turn_record(dict(row, output_tokens=118, output_tokens_unknown=0))
    )
    assert "118" in measured
