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
UC-1b, UC-5 and the two output-unknown bridge cases RED. UC-6/7/8 construct
canonical records directly and test downstream gates, not normalization.
"""

from __future__ import annotations

import pytest

from decimal import Decimal

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


def _production_row(store, turn_id):
    """The stored turn in the shape PRODUCTION hands the renderer.

    `store.get_turn` routes through `_row_to_dict`, which RENAMES columns
    (`cache_read` -> `cache_read_tokens`, likewise cache_write/reasoning). The
    production caller `plugins/blackbox/last_turn.py::compute_last_turn_record`
    does a raw `SELECT *` and passes the UNRENAMED keys — which is why the
    renderer reads `rec.get("cache_read", 0)`. Feeding it the `get_turn` shape
    silently resolves every cache lookup to 0, so the `• Cached:` row is never
    emitted and any assertion about cache or last-call lines would be testing a
    row shape that never occurs at runtime (r6 finding 12).
    """
    import sqlite3

    conn = sqlite3.connect(store._db_path())
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


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


@pytest.mark.parametrize("flag", ["output_tokens_unknown", "input_tokens_unknown",
                                  "cache_read_tokens_unknown", "cache_write_tokens_unknown",
                                  "usage_unknown"])
def test_uc7b_the_turn_rollup_absorbs_an_unknown_call(flag):
    """The shipped per-call -> per-turn roll-up is unknown-absorbing."""
    from agent.turn_finalizer import _rollup_turn_usage

    def call(**overrides):
        return {
            "input_tokens": 100,
            "output_tokens": 118,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 218,
            **overrides,
        }

    measured = call()
    unknown = call(output_tokens=0, **{flag: True})
    assert _rollup_turn_usage([measured, measured])[flag] is False
    assert _rollup_turn_usage([measured, unknown])[flag] is True
    assert _rollup_turn_usage([unknown, measured])[flag] is True
    assert _rollup_turn_usage([])[flag] is False


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
    # Pin the TOKEN ROW, not the block. `cost_status="unknown"` already emits
    # `• Turn Cost: n/a (unknown)` above, so a bare substring check on the
    # whole block passes even if the token row regresses to `Tokens out: 0`
    # — precisely the defect this test exists to catch (r6 finding 11).
    assert f"Tokens out: {UNKNOWN_TOKENS_LABEL}" in block
    assert "Tokens out: 0" not in block

    measured = "\n".join(
        render_last_turn_record(dict(row, output_tokens=118, output_tokens_unknown=0))
    )
    assert "118" in measured


@pytest.mark.parametrize("wire, input_unknown, output_unknown", [
    ({"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
      "prompt_tokens_unavailable": True, "unavailable": True}, True, False),
    ({"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
      "prompt_tokens_unavailable": True, "total_tokens_unavailable": True,
      "unavailable": True,
      "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": 0}}, True, False),
    ({"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
      "unavailable": True}, True, True),
    ({"prompt_tokens": 150, "completion_tokens": None, "total_tokens": None,
      "output_tokens_unavailable": True, "unavailable": True}, False, True),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
      "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0}}, False, False),
])
def test_bridge_input_and_full_unknown(wire, input_unknown, output_unknown, tmp_path, monkeypatch):
    from dataclasses import asdict
    import plugins.blackbox as bb
    import plugins.blackbox.store as store
    from plugins.blackbox.last_turn import render_last_turn_record

    usage = normalize_usage(wire, api_mode="chat_completions")
    assert usage.input_tokens_unknown is input_unknown
    assert usage.output_tokens_unknown is output_unknown
    assert usage.total_tokens_unknown is (input_unknown or output_unknown)
    cost = estimate_usage_cost("claude-sonnet-4-5", usage, provider="anthropic")
    assert (cost.amount_usd is None) is (input_unknown or output_unknown)
    assert (usage + CanonicalUsage()).total_tokens_unknown is usage.total_tokens_unknown
    call = asdict(usage)
    rec = bb._build_record(session_id="bridge", interrupted=False, model="claude-sonnet-4-5",
                           platform="cli", provider="anthropic", user_message="",
                           final_response="", turn_usage={**call, "calls": [call]},
                           cfg={"store_text": False}, kwargs={})
    assert rec is not None
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "turns.db")
    store.insert_turn(rec)
    row = _production_row(store, rec.turn_id)
    assert row is not None
    # Shape assertion (r6 finding 12): the renderer below reads the RAW column
    # names, so this round-trip must hand it the raw `SELECT *` shape that
    # production produces. `store.get_turn` renames these away via
    # `_row_to_dict`, silently resolving every cache lookup to 0.
    assert "cache_read" in row and "cache_write" in row
    assert bool(row["input_tokens_unknown"]) is input_unknown
    assert bool(row["output_tokens_unknown"]) is output_unknown
    assert (row["cost_usd"] is None) is (input_unknown or output_unknown)
    block = "\n".join(render_last_turn_record(row))
    if input_unknown:
        assert "Tokens in: unknown" in block
    if output_unknown:
        assert "Tokens out: unknown" in block
    if wire["completion_tokens"] == 50:
        assert "Tokens out: 50" in block


@pytest.mark.parametrize("key, flag", [
    ("cached_tokens", "cache_read_tokens_unknown"),
    ("cache_creation_tokens", "cache_write_tokens_unknown"),
])
def test_cache_null_is_not_a_measured_zero(key, flag):
    usage = normalize_usage({"prompt_tokens": 150, "completion_tokens": 50,
                             "prompt_tokens_details": {key: None}})
    assert getattr(usage, flag) is True
    assert usage.input_tokens_unknown is True
    assert usage.total_tokens_unknown is True
    assert estimate_usage_cost("claude-sonnet-4-5", usage, provider="anthropic").amount_usd is None


@pytest.mark.parametrize("flag", ["input_tokens_unknown", "cache_read_tokens_unknown",
                                  "cache_write_tokens_unknown", "usage_unknown"])
def test_new_unknown_flags_survive_blackbox(flag, tmp_path, monkeypatch):
    from dataclasses import asdict
    import plugins.blackbox as bb
    import plugins.blackbox.store as store
    from plugins.blackbox.last_turn import render_last_turn_record

    usage = CanonicalUsage(**{flag: True})
    assert estimate_usage_cost("claude-sonnet-4-5", usage, provider="anthropic").amount_usd is None
    call = asdict(usage)
    rec = bb._build_record(session_id="s", interrupted=False, model="claude-sonnet-4-5",
                           platform="cli", provider="anthropic", user_message="",
                           final_response="", turn_usage={**call, "calls": [call]},
                           cfg={"store_text": False}, kwargs={})
    assert getattr(rec, flag) is True
    assert rec.cost_usd is None
    assert rec.cost_status == "unknown"
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "turns.db")
    store.insert_turn(rec)
    row = _production_row(store, rec.turn_id)
    assert "cache_read" in row and "cache_write" in row, (
        "must be the raw SELECT * shape production renders (r6 finding 12); "
        "store.get_turn renames these and the renderer's lookups silently "
        "resolve to 0"
    )
    assert row[flag] == 1
    # Assert the TOKEN line, not just "unknown" anywhere in the block: a
    # cost_status of "unknown" already emits "• Turn Cost: n/a (unknown)", so a
    # substring check on the whole block passes even if the token rows regress
    # to "• Tokens in: 0 billed" — the exact defect this file exists to pin.
    block = "\n".join(render_last_turn_record(row))
    assert "Tokens in: unknown" in block


def test_sdk_absent_optional_fields_remain_legacy():
    from openai.types.completion_usage import CompletionUsage
    usage = normalize_usage(CompletionUsage.model_construct(
        prompt_tokens=150, completion_tokens=50, total_tokens=200))
    assert usage.total_tokens_unknown is False
    assert usage.input_tokens == 150


def test_explicit_null_cache_details_are_absent_not_unknown():
    """FleetReview findings 1+8 on f78eae23; ruling on t_a17c6966.

    A null CONTAINER means no cache breakdown, not an unknown COUNT.
    Null counts inside a present container remain unknown (separate pins).
    """
    usage = normalize_usage({"prompt_tokens": 150, "completion_tokens": 50,
                             "prompt_tokens_details": None})
    assert usage.cache_read_tokens == usage.cache_write_tokens == 0
    assert usage.cache_read_tokens_unknown is False
    assert usage.cache_write_tokens_unknown is False
    assert usage.input_tokens_unknown is False
    assert usage.input_tokens == 150
    assert usage.total_tokens_unknown is False
    assert estimate_usage_cost("claude-sonnet-4-5", usage, provider="anthropic").amount_usd is not None


def test_sdk_null_cache_and_flag_only_input_are_unknown():
    from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
    usage = normalize_usage(CompletionUsage.model_construct(
        prompt_tokens=150, completion_tokens=50, total_tokens=None,
        prompt_tokens_details=PromptTokensDetails.model_construct(cached_tokens=None)))
    assert usage.cache_read_tokens_unknown is True
    assert usage.input_tokens_unknown is True
    flagged = normalize_usage(_ModelExtraUsage(prompt_tokens=0, completion_tokens=50,
                              model_extra={"prompt_tokens_unavailable": True}))
    assert flagged.input_tokens_unknown is True
    assert flagged.output_tokens_unknown is False


def test_input_unknown_renders_both_cards_without_losing_measured_output():
    from plugins.blackbox.card import _tokens_in_label, _tokens_out_line, _cache_line
    from plugins.blackbox.last_turn import render_last_turn_record
    from plugins.blackbox.record import TurnRecord
    rec = TurnRecord(turn_id="t", input_tokens_unknown=True, output_tokens=50)
    assert _tokens_in_label(rec) == "unknown"
    assert _tokens_out_line(rec) == "50 out"
    assert _cache_line(rec) == "unknown"
    block = "\n".join(render_last_turn_record({"input_tokens_unknown": True, "output_tokens": 50}))
    assert "Tokens in: unknown" in block
    assert "Tokens out: 50" in block


def test_inactive_null_aliases_do_not_override_measured_counters():
    """Unified schemas may serialize the unused API dialect as null."""
    usage = normalize_usage(
        {
            "prompt_tokens": None,
            "completion_tokens": None,
            "input_tokens": 4000,
            "output_tokens": 100,
        },
        provider="meta",
        api_mode="chat_completions",
    )
    assert (usage.input_tokens, usage.output_tokens) == (4000, 100)
    assert usage.input_tokens_unknown is False
    assert usage.output_tokens_unknown is False


def test_unset_sdk_optional_is_not_a_wire_null():
    class Usage:
        prompt_tokens = 500
        completion_tokens = None
        output_tokens = 100
        model_fields_set = {"prompt_tokens", "output_tokens"}

    usage = normalize_usage(Usage(), provider="custom", api_mode="chat_completions")
    assert usage.output_tokens == 100
    assert usage.output_tokens_unknown is False


def test_null_aggregate_is_derived_from_measured_components():
    usage = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": None},
        provider="openai",
        api_mode="chat_completions",
    )
    assert usage.total_tokens == 120
    assert usage.total_tokens_unknown is False
    assert estimate_usage_cost("gpt-4o", usage, provider="openai").amount_usd is not None


def test_missing_response_usage_is_unknown_but_normalizer_no_call_stays_zero():
    """A real response omission differs from a known no-call normalization path."""
    from types import SimpleNamespace
    from agent.conversation_loop import _canonical_usage_from_response

    omitted = _canonical_usage_from_response(
        SimpleNamespace(usage=None), provider="custom", api_mode="chat_completions"
    )
    no_call = normalize_usage(None)
    assert omitted.usage_unknown is True
    assert omitted.total_tokens_unknown is True
    assert no_call.usage_unknown is False
    assert no_call.total_tokens == 0


def test_unknown_usage_never_replaces_the_context_anchor():
    from agent.conversation_loop import _capture_measured_usage_anchor

    messages = [{"role": "user", "content": "hello"}]
    unknown = CanonicalUsage(
        input_tokens=1000, output_tokens=0, output_tokens_unknown=True
    )
    measured = CanonicalUsage(input_tokens=1000, output_tokens=40)
    assert _capture_measured_usage_anchor(unknown, messages) is None
    assert _capture_measured_usage_anchor(measured, messages)["completion_tokens"] == 40


def test_unpriceable_moa_advisor_makes_known_subtotal_partial():
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage(input_tokens=100, output_tokens=20),
        provider="anthropic",
    )
    advisor = {
        "model": "claude-sonnet-4-5",
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 0,
        "output_tokens_unknown": True,
    }
    assert aggregator.amount_usd is not None
    assert _moa_session_cost_status(aggregator, [advisor]) == "partial"


def test_unpriceable_moa_aggregator_beside_priced_advisors_is_partial():
    """r4 finding 4's MIRROR case — the arm that was green without this pin.

    ``any_unpriceable`` used to be seeded ``False`` and set only from the
    advisor loop, so an unpriceable AGGREGATOR next to priced advisors returned
    ``"unknown"`` while the turn had really spent the advisor dollars that
    ``session_estimated_cost_usd`` already absorbed. ``partial`` is the status
    the helper's own docstring — and ``plugins/blackbox/sentinel.py``'s
    deliberate exclusion of ``"partial"`` from ``_UNPRICED_STATUSES`` — argues
    for.
    """
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage.fully_unknown(),
        provider="anthropic",
    )
    assert aggregator.amount_usd is None
    priced_advisor = {
        "model": "claude-sonnet-4-5",
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    assert (
        _moa_session_cost_status(aggregator, [priced_advisor], 0.0012) == "partial"
    )
    # Control: a NON-MoA turn (no advisors, no advisor cost) stays "unknown" —
    # seeding any_unpriceable from the aggregator must not manufacture a
    # partial out of a turn that priced nothing at all.
    assert _moa_session_cost_status(aggregator, []) == "unknown"


def test_a_measured_but_route_unpriceable_advisor_is_not_skipped():
    """r6 round-4 finding 1 — UNPRICEABLE is not the same question as UNMEASURED.

    The loop used to `continue` on any advisor whose usage flags were all False,
    so the only unpriceable advisor it could detect was one with UNKNOWN usage.
    But an advisor is just as commonly unpriceable with fully MEASURED tokens:
    an uncatalogued route makes `estimate_usage_cost` return
    `amount_usd=None, status="unknown"` with no flag set anywhere. Its real
    dollars never enter `advisor_cost` (only non-None advisor costs are summed),
    so the session lane reported `estimated` — a COMPLETE label — for a total
    that omits them, while `plugins/blackbox/cost.py` called the same turn
    `partial`. Two surfaces, one turn, and the optimistic one is what users read.
    """
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage(input_tokens=100, output_tokens=20),
        provider="anthropic",
    )
    assert aggregator.amount_usd is not None

    # Fully measured, no unknown flag anywhere — and unpriceable all the same.
    unpriceable_advisor = {
        "model": "some-uncatalogued-model",
        "provider": "someproxy",
        "input_tokens": 100_000,
        "output_tokens": 2_000,
        "cost_usd": None,
        "cost_status": "unknown",
    }
    assert not any(
        unpriceable_advisor.get(k) for k in ("input_tokens_unknown", "usage_unknown")
    )
    assert _moa_session_cost_status(aggregator, [unpriceable_advisor]) == "partial", (
        "an advisor whose real spend cannot enter the total makes the total "
        "incomplete, whether its tokens were measured or not"
    )


def test_a_priced_advisor_verdict_keeps_the_complete_label():
    """NARROWNESS control for the verdict read.

    Consulting each advisor's own verdict must not manufacture a `partial` out
    of a turn where every advisor really was priced.
    """
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage(input_tokens=100, output_tokens=20),
        provider="anthropic",
    )
    priced_advisor = {
        "model": "claude-sonnet-4-5",
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.0012"),
        "cost_status": "estimated",
    }
    assert (
        _moa_session_cost_status(aggregator, [priced_advisor], 0.0012)
        == aggregator.status
    )


def test_a_subscription_included_advisor_verdict_is_known_not_unpriceable():
    """An included route prices at a real $0 — that is KNOWN, not missing.

    `estimate_usage_cost` returns `amount_usd=Decimal("0"), status="included"`
    for a subscription route ABOVE its unknown-usage refusal, so the verdict
    read must class it with the priced advisors. This is the same ordering the
    flag-based fallback below already applies via `resolve_billing_route`.
    """
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage.fully_unknown(),
        provider="anthropic",
    )
    assert aggregator.amount_usd is None
    included_advisor = {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_unknown": True,
        "cost_usd": Decimal("0"),
        "cost_status": "included",
    }
    assert _moa_session_cost_status(aggregator, [included_advisor]) == "partial", (
        "the included advisor is KNOWN (free), the aggregator is not — partial"
    )


def test_an_advisor_without_a_verdict_still_falls_back_to_its_usage_flags():
    """Back-compat: a call record predating the carried verdict.

    `_build_moa_pricing_calls` copies advisor dicts through, and Blackbox
    re-prices them itself, so a record without `cost_usd`/`cost_status` keys
    must keep the original flag+route behaviour rather than silently reading a
    missing key as priced.
    """
    from agent.conversation_loop import _moa_session_cost_status

    aggregator = estimate_usage_cost(
        "claude-sonnet-4-5",
        CanonicalUsage(input_tokens=100, output_tokens=20),
        provider="anthropic",
    )
    verdictless_unmeasured = {
        "model": "claude-sonnet-4-5",
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 0,
        "output_tokens_unknown": True,
    }
    assert "cost_usd" not in verdictless_unmeasured
    assert _moa_session_cost_status(aggregator, [verdictless_unmeasured]) == "partial"


def test_the_moa_loop_really_carries_each_advisors_own_verdict(tmp_path, monkeypatch):
    """The producer half, by execution — the consumer above needs these keys.

    Drives the real `MoAClient` and reads the pricing-call records it emits, so
    the fix cannot be green on a consumer that no producer ever feeds.
    """
    from types import SimpleNamespace

    from agent.moa_loop import MoAClient

    (tmp_path / "config.yaml").write_text(
        "moa:\n  default_preset: default\n  presets:\n    default:\n"
        "      enabled: true\n      reference_models:\n"
        "        - provider: anthropic\n          model: claude-sonnet-4-5\n"
        "      aggregator:\n        provider: anthropic\n"
        "        model: claude-sonnet-4-5\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agent.moa_loop.call_llm",
        lambda **kw: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="advice", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 20},
            model="claude-sonnet-4-5",
        ),
    )
    client = MoAClient("default")
    client.chat.completions.create(
        model="default", messages=[{"role": "user", "content": "test"}]
    )
    calls = client.consume_reference_pricing_calls()

    assert len(calls) == 1
    assert "cost_usd" in calls[0], "the advisor's own verdict must ride the record"
    assert "cost_status" in calls[0]
    # This advisor IS catalogued, so its verdict is a real priced amount.
    assert calls[0]["cost_usd"] is not None
    assert calls[0]["cost_status"] != "unknown"


def test_an_omitted_payload_sets_every_discriminator_not_just_the_aggregate():
    """r4 finding 9 — the arm that was green without this pin.

    Consumers read these flags NARROWLY: ``output_tokens_unknown`` alone gates
    the thin card's output line and the ``out=`` API log, and
    ``prompt_tokens_unknown`` ORs only the three input flags. An
    aggregate-only ``usage_unknown`` therefore still lets a narrow reader
    present the placeholder 0 as a measurement.
    """
    from agent.usage_pricing import USAGE_UNKNOWN_FIELDS, prompt_tokens_unknown

    usage = CanonicalUsage.fully_unknown()
    for field in USAGE_UNKNOWN_FIELDS:
        assert getattr(usage, field) is True, field
    assert prompt_tokens_unknown(usage) is True
    assert usage.output_tokens_unknown is True
    # Control: a measured payload sets none of them.
    measured = CanonicalUsage(input_tokens=10, output_tokens=4)
    assert not any(getattr(measured, f) for f in USAGE_UNKNOWN_FIELDS)


def test_a_measured_cache_count_suppresses_a_null_alias_in_the_other_location():
    """r4 finding 11 — the arm that was green without this pin.

    A cache count arrives in EITHER the details container or a top-level
    alias. Because sibling-alias protection only looks at keys on the SAME
    object, OR-ing two independent ``_bucket_is_unknown`` calls let a JSON null
    on the dialect the provider is not speaking override a measured value in
    the other location — a unified OpenAI-compatible schema serialized without
    ``exclude_none`` emits exactly that shape.
    """
    mixed = {
        "prompt_tokens": 150,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 200},
        "cache_read_input_tokens": None,
    }
    usage = normalize_usage(mixed, provider="openai", api_mode="chat_completions")
    assert usage.cache_read_tokens == 200
    assert usage.cache_read_tokens_unknown is False
    assert usage.total_tokens_unknown is False
    assert estimate_usage_cost("gpt-4o", usage, provider="openai").amount_usd is not None

    # CONTROLS — the standing consensus must NOT be weakened by the above.
    # (a) a null count inside a PRESENT container, with no measured value
    #     anywhere, is still UNKNOWN.
    still_unknown = normalize_usage(
        {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": None},
        },
        provider="openai",
        api_mode="chat_completions",
    )
    assert still_unknown.cache_read_tokens_unknown is True
    # (b) an explicit unavailable FLAG wins even beside a measured count.
    flagged = normalize_usage(
        {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 200},
            "cache_read_tokens_unavailable": True,
        },
        provider="openai",
        api_mode="chat_completions",
    )
    assert flagged.cache_read_tokens_unknown is True


def test_the_compressor_payload_is_built_from_one_usage_object():
    """r6 round-4 finding 2 — window occupancy is the AGGREGATOR's, pre-fold.

    `canonical_usage` at the compressor call site is `aggregator + advisor
    fan-out`, and `CanonicalUsage.__add__` makes every unknown flag ABSORBING.
    So one advisor that returned no payload (seeded `fully_unknown()` by
    `moa_loop._run_reference`) set `input_tokens_unknown` on the fold and made
    the gate refuse a complete, MEASURED aggregator prompt count — leaving
    `context_compressor.last_prompt_tokens`, `turn_finalizer`'s `context_used`
    and the persisted occupancy carrying the previous call's reading.

    The anchor immediately below that site already reads `aggregator_usage` for
    exactly this reason. These pin the gate's predicate AND its payload.
    """
    from agent.conversation_loop import _compressor_usage_dict
    from agent.usage_pricing import prompt_tokens_unknown

    aggregator = CanonicalUsage(input_tokens=4000, output_tokens=120)
    unmeasured_advisor = CanonicalUsage.fully_unknown()
    folded = aggregator + unmeasured_advisor

    # The fold really is absorbing — this is what the gate used to be asked.
    assert prompt_tokens_unknown(folded) is True
    assert prompt_tokens_unknown(aggregator) is False

    payload = _compressor_usage_dict(aggregator)
    assert payload["prompt_tokens"] == 4000, (
        "the compressor must see THIS conversation's prompt, not a blanked fold"
    )
    assert payload["completion_tokens"] == 120
    assert payload["total_tokens"] == aggregator.total_tokens
    # Only the three legacy aggregate keys the compressor actually reads.
    assert set(payload) == {"prompt_tokens", "completion_tokens", "total_tokens"}


def test_the_compressor_payload_does_not_carry_advisor_fanout_tokens():
    """NARROWNESS control: the fix must not swap one wrong number for another.

    Reading the fold would also have DOUBLE-COUNTED advisor fan-out into window
    occupancy whenever every advisor was measured — tokens that were never part
    of this conversation's prompt.
    """
    from agent.conversation_loop import _compressor_usage_dict
    from agent.usage_pricing import prompt_tokens_unknown

    aggregator = CanonicalUsage(input_tokens=4000, output_tokens=120)
    measured_advisor = CanonicalUsage(input_tokens=90_000, output_tokens=2_000)
    folded = aggregator + measured_advisor

    assert prompt_tokens_unknown(folded) is False, "both measured — the gate passes"
    assert folded.prompt_tokens == 94_000
    assert _compressor_usage_dict(aggregator)["prompt_tokens"] == 4000, (
        "advisor fan-out is not window occupancy"
    )


def test_a_usageless_aggregator_still_refuses_the_compressor_update():
    """The r6-finding-7 direction is untouched by the pre-fold read.

    An UNMEASURED aggregator must still be refused — otherwise a placeholder
    zero overwrites the previous real occupancy reading. The pre-fold read
    narrows WHICH usage answers, not WHETHER unknown is refused.
    """
    from agent.usage_pricing import prompt_tokens_unknown

    assert prompt_tokens_unknown(CanonicalUsage.fully_unknown()) is True
    assert (
        prompt_tokens_unknown(
            CanonicalUsage(input_tokens=0, input_tokens_unknown=True)
        )
        is True
    )
