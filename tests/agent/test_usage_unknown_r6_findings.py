"""r6 FleetReview regressions — the sinks the r5 head still fed a measured zero.

Every test here drives real code (``normalize_usage``, a real ``MoAClient``
fan-out, a real ``SessionDB``, the shipped ``/usage`` renderer). Each names the
finding it pins and carries a narrowness control, so a fix cannot be bought by
making the UNKNOWN refusal unreachable.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS,
    CanonicalUsage,
    estimate_usage_cost,
    normalize_usage,
    prompt_tokens_unknown,
)


class _Wire:
    """A wire-shaped usage payload: attribute access, ``in``, and ``get``."""

    def __init__(self, d):
        self._d = dict(d)
        self.model_extra = {}
        for k, v in d.items():
            setattr(self, k, v)

    def __contains__(self, k):
        return k in self._d

    def get(self, k, default=None):
        return self._d.get(k, default)

    def keys(self):
        return self._d.keys()


# --------------------------------------------------------------------------
# F4 — a wire-null cache alias beside measured input/output must not make the
#      turn permanently unpriceable.
# --------------------------------------------------------------------------


def test_f4_null_top_level_cache_alias_beside_measured_counts_is_a_measured_zero():
    usage = normalize_usage(
        _Wire(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            }
        ),
        provider="anthropic",
        api_mode="anthropic_messages",
    )
    assert usage.cache_read_tokens_unknown is False
    assert usage.cache_write_tokens_unknown is False
    assert usage.total_tokens_unknown is False
    assert prompt_tokens_unknown(usage) is False
    # The whole point: the turn must still price. An unpriceable row is also
    # skipped by reprice_unpriced, so the NULL cost would never heal.
    assert estimate_usage_cost(
        "claude-sonnet-4-5", usage, provider="anthropic"
    ).amount_usd is not None


def test_f4_narrowness_an_explicit_unavailable_flag_still_wins():
    usage = normalize_usage(
        _Wire(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": None,
                "cache_read_tokens_unavailable": True,
            }
        ),
        provider="anthropic",
        api_mode="anthropic_messages",
    )
    assert usage.cache_read_tokens_unknown is True


def test_f4_narrowness_a_null_inside_a_present_container_is_still_unknown():
    """The standing consensus: null COUNT in a present container -> UNKNOWN."""
    usage = normalize_usage(
        {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": None},
        },
        provider="openai",
        api_mode="chat_completions",
    )
    assert usage.cache_read_tokens_unknown is True


def test_f4_narrowness_a_null_alias_on_an_UNMEASURED_payload_stays_unknown():
    """The "rest of the payload was measured" precondition must be load-bearing."""
    usage = normalize_usage(
        _Wire(
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_input_tokens": None,
            }
        ),
        provider="anthropic",
        api_mode="anthropic_messages",
    )
    assert usage.cache_read_tokens_unknown is True
    assert usage.total_tokens_unknown is True


# --------------------------------------------------------------------------
# F5 — a usage-less MoA advisor response must not be a measured zero.
# --------------------------------------------------------------------------


def _moa_client(monkeypatch, tmp_path, usage_payload):
    from agent.moa_loop import MoAClient

    (tmp_path / "config.yaml").write_text(
        "moa:\n  default_preset: default\n  presets:\n    default:\n"
        "      enabled: true\n      reference_models:\n"
        "        - provider: anthropic\n          model: claude-sonnet-4-5\n"
        "      aggregator:\n        provider: anthropic\n        model: claude-sonnet-4-5\n"
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
            usage=usage_payload,
            model="claude-sonnet-4-5",
        ),
    )
    client = MoAClient("default")
    client.chat.completions.create(
        model="default", messages=[{"role": "user", "content": "test"}]
    )
    return client


def test_f5_usageless_moa_advisor_is_unknown_not_a_confident_zero(monkeypatch, tmp_path):
    from plugins.blackbox.cost import compute_turn_cost

    client = _moa_client(monkeypatch, tmp_path, None)
    calls = client.consume_reference_pricing_calls()
    assert len(calls) == 1
    # Every discriminator, not just the aggregate: consumers read them narrowly.
    for key in USAGE_UNKNOWN_FIELDS:
        assert calls[0].get(key) is True, key

    # The consequence the finding named: priced at a confident $0 instead of
    # refusing, the advisor's real spend silently vanishes from the turn.
    cost, status, _ = compute_turn_cost(
        "default", "moa", None, [{"pricing_calls": calls}]
    )
    assert cost is None
    assert status == "unknown"


def test_f5_narrowness_a_measured_moa_advisor_still_prices(monkeypatch, tmp_path):
    from plugins.blackbox.cost import compute_turn_cost

    client = _moa_client(
        monkeypatch, tmp_path, _Wire({"input_tokens": 100, "output_tokens": 50})
    )
    calls = client.consume_reference_pricing_calls()
    assert len(calls) == 1
    assert not any(calls[0].get(key) for key in USAGE_UNKNOWN_FIELDS)
    cost, status, _ = compute_turn_cost(
        "default", "moa", None, [{"pricing_calls": calls}]
    )
    assert cost is not None
    assert status != "unknown"


# --------------------------------------------------------------------------
# F13 — the evicted/resumed lane must not persist 'unknown' over priced
#       dollars. The guard lives at the DURABLE chokepoint, so a NEW agent
#       object (zeroed in-memory accumulator) cannot strand the row.
# --------------------------------------------------------------------------


@pytest.fixture()
def session_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    try:
        db.close()
    except Exception:
        pass


def _status(db, sid):
    with db._lock:
        row = db._conn.execute(
            "SELECT cost_status, estimated_cost_usd FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
    return row["cost_status"], row["estimated_cost_usd"]


def test_f13_resumed_session_is_not_relabelled_unknown_over_priced_dollars(session_db):
    sid = "resumed-session"
    session_db.create_session(sid, "test")
    # Turn 1: a real, priced turn (the "previous agent" that has since been
    # evicted).
    session_db.update_token_counts(
        sid, input_tokens=4000, output_tokens=120,
        estimated_cost_usd=0.0112, cost_status="estimated",
        model="claude-sonnet-4-5", billing_provider="anthropic", api_call_count=1,
    )
    assert _status(session_db, sid) == ("estimated", pytest.approx(0.0112))

    # Turn 2: a NEW agent resumes this session_id. Its in-memory
    # session_estimated_cost_usd is 0.0 (agent_init.py sets it on every
    # construction and never rehydrates), so the caller-side guard computes
    # 'unknown'. The durable row still holds the dollars.
    session_db.update_token_counts(
        sid, cost_status="unknown",
        model="claude-sonnet-4-5", billing_provider="anthropic", api_call_count=1,
    )
    status, dollars = _status(session_db, sid)
    assert status == "partial", (
        "'unknown' is outside the reprice allowlist, so relabelling a session "
        "that still holds priced dollars strands it permanently"
    )
    assert dollars == pytest.approx(0.0112), "the real dollars must survive"


def test_f13_narrowness_a_session_with_no_priced_dollars_is_still_unknown(session_db):
    sid = "never-priced"
    session_db.create_session(sid, "test")
    session_db.update_token_counts(
        sid, cost_status="unknown",
        model="claude-sonnet-4-5", billing_provider="anthropic", api_call_count=1,
    )
    status, _ = _status(session_db, sid)
    assert status == "unknown", (
        "the refusal must stay reachable — a session with no priced spend at "
        "all really is wholly unmeasured"
    )


def test_f13_narrowness_a_measured_status_still_overwrites(session_db):
    sid = "heals"
    session_db.create_session(sid, "test")
    session_db.update_token_counts(
        sid, cost_status="unknown",
        model="claude-sonnet-4-5", billing_provider="anthropic", api_call_count=1,
    )
    assert _status(session_db, sid)[0] == "unknown"
    session_db.update_token_counts(
        sid, input_tokens=10, output_tokens=5, estimated_cost_usd=0.001,
        cost_status="estimated",
        model="claude-sonnet-4-5", billing_provider="anthropic", api_call_count=1,
    )
    assert _status(session_db, sid)[0] == "estimated"


# --------------------------------------------------------------------------
# F1 — the session-scoped status must not label a wholly-unpriced MODEL row
#      "partial" on the Spend-by-model breakdown.
# --------------------------------------------------------------------------


def _model_rows(db, sid):
    with db._lock:
        rows = db._conn.execute(
            "SELECT model, cost_status, estimated_cost_usd FROM session_model_usage "
            "WHERE session_id = ? ORDER BY model",
            (sid,),
        ).fetchall()
    return {r["model"]: (r["cost_status"], r["estimated_cost_usd"]) for r in rows}


def test_f1_a_wholly_unpriced_model_row_is_unknown_not_partial(session_db):
    sid = "model-switch"
    session_db.create_session(sid, "test")
    # Model Y earns $2.00 of real spend.
    session_db.update_token_counts(
        sid, input_tokens=1000, output_tokens=100, estimated_cost_usd=2.0,
        cost_status="estimated", model="model-y",
        billing_provider="anthropic", api_call_count=1,
    )
    # User /model-switches to Z, whose first call returns no usage. The SESSION
    # has known dollars, so the caller hands down 'partial' — but none of them
    # are Z's.
    session_db.update_token_counts(
        sid, cost_status="partial", model="model-z",
        billing_provider="anthropic", api_call_count=1,
    )
    rows = _model_rows(session_db, sid)
    assert rows["model-y"][0] == "estimated"
    assert rows["model-z"][0] == "unknown", (
        "nothing about model Z was priced; presenting its $0.00 as 'partial' "
        "claims some of it was measured (r6 finding 1)"
    )
    assert rows["model-z"][1] == pytest.approx(0.0)


def test_f1_narrowness_a_model_row_with_its_own_dollars_is_partial(session_db):
    sid = "own-dollars"
    session_db.create_session(sid, "test")
    session_db.update_token_counts(
        sid, input_tokens=1000, output_tokens=100, estimated_cost_usd=2.0,
        cost_status="estimated", model="model-y",
        billing_provider="anthropic", api_call_count=1,
    )
    # A SECOND call on the SAME model is unpriceable. That row really does hold
    # priced dollars, so it is incomplete — 'partial' — not wholly unmeasured.
    session_db.update_token_counts(
        sid, cost_status="unknown", model="model-y",
        billing_provider="anthropic", api_call_count=1,
    )
    assert _model_rows(session_db, sid)["model-y"][0] == "partial"


# --------------------------------------------------------------------------
# F7 — the compressor gate must test for a MEASURED prompt count, not for the
#      presence of a usage object.
# --------------------------------------------------------------------------


def test_f7_a_null_prompt_count_does_not_zero_the_compressor_reading():
    """A payload that PRESENTS a usage object but nulls the prompt count."""
    usage = normalize_usage(
        _Wire({"prompt_tokens": None, "completion_tokens": 50}),
        provider="openai",
        api_mode="chat_completions",
    )
    # This is the predicate the gate now uses; it must refuse this payload.
    assert prompt_tokens_unknown(usage) is True


def test_f7_narrowness_an_output_only_unknown_keeps_the_prompt_reading():
    """`prompt_tokens_unknown`, not `total_tokens_unknown`: an unmeasured
    OUTPUT bucket must not discard a good PROMPT occupancy reading."""
    usage = normalize_usage(
        _Wire({"prompt_tokens": 4000, "completion_tokens": None}),
        provider="openai",
        api_mode="chat_completions",
    )
    assert usage.output_tokens_unknown is True
    assert usage.total_tokens_unknown is True
    assert prompt_tokens_unknown(usage) is False, (
        "the compressor gate must still accept a measured prompt count"
    )


def test_f7_narrowness_a_fully_measured_payload_still_updates():
    usage = normalize_usage(
        _Wire({"prompt_tokens": 4000, "completion_tokens": 120}),
        provider="openai",
        api_mode="chat_completions",
    )
    assert prompt_tokens_unknown(usage) is False


# --------------------------------------------------------------------------
# F8 — the resident /usage lane must carry the UNKNOWN discriminators, or its
#      unknown branches are dead code and it renders a measured-looking 0.
# --------------------------------------------------------------------------


def test_f8_resident_snapshot_flags_make_the_renderer_refuse_a_zero_total():
    """Drive the REAL resident producer, not a hand-built dict.

    This is the seam the finding named: the producer built five flagless ints,
    so the renderer's unknown branches were dead code on this lane.

    TEST-REPIN (r6 round-4 finding 4, superseding this test's original
    `last_turn_usage` fixture): the discriminators now come from the ABSORBING
    SESSION-LEVEL latch (`agent.session_*_unknown`), not from
    `agent.last_turn_usage`. `last_turn_usage` is rewritten on every provider
    call, so it described the last CALL while the five numbers describe the whole
    SESSION — and finding 4 showed the original fixture (session totals equal to
    its last turn) could not tell the two apart, so it pinned the per-call read.
    The MIXED orderings that discriminate them are pinned below.
    """
    from gateway.slash_commands import (
        _resident_thin_snapshot, render_thin_last_turn_lines,
    )

    unmeasured_agent = SimpleNamespace(
        session_input_tokens=0, session_output_tokens=0,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_input_tokens_unknown=True, session_output_tokens_unknown=True,
        session_usage_unknown=True,
    )
    snap = _resident_thin_snapshot(unmeasured_agent)
    assert snap.get("output_tokens_unknown") is True, (
        "the producer must carry the discriminators, or the renderer cannot act"
    )
    flagged = "\n".join(render_thin_last_turn_lines(snap, "test"))
    assert "Total (billed in+out): 0" not in flagged
    assert "unknown" in flagged


def test_f4_earlier_unmeasured_call_is_not_erased_by_a_later_measured_one():
    """Mode 1 of finding 4: the flag must survive a later MEASURED call.

    The per-call read could not see this: `last_turn_usage` carries the FINAL
    call's clean verdict, so the cumulative total — which really is missing the
    earlier call's tokens — rendered as an exact-looking number. The absorbing
    session latch is what makes the order irrelevant.
    """
    from gateway.slash_commands import (
        _resident_thin_snapshot, render_thin_last_turn_lines,
    )

    agent = SimpleNamespace(
        session_input_tokens=400_000, session_output_tokens=12_338,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        # The latch, set by the EARLIER unmeasured call and never cleared.
        session_input_tokens_unknown=True, session_output_tokens_unknown=True,
        session_usage_unknown=False,
        # The final call was measured — this is exactly what the old per-call
        # read consulted, and why it reported the total as exact.
        last_turn_usage={
            "input_tokens": 400_000, "output_tokens": 12_338,
            "input_tokens_unknown": False, "output_tokens_unknown": False,
            "usage_unknown": False,
        },
    )
    snap = _resident_thin_snapshot(agent)
    assert snap.get("input_tokens_unknown") is True
    assert snap.get("output_tokens_unknown") is True
    text = "\n".join(render_thin_last_turn_lines(snap, "test"))
    assert "412,338" not in text, (
        "a cumulative total missing an unmeasured call must not read as exact"
    )
    assert "Total (billed in+out): unknown" in text


def test_f4_a_measured_session_is_not_collapsed_by_one_unmeasured_last_call():
    """Mode 2 of finding 4: 412k MEASURED tokens must not read as `unknown`.

    The mirror failure of the per-call read. Nothing here sets the session latch,
    so the session's own numbers stand even though the most recent CALL was
    unmeasured — the latch is a property of the aggregate, and this aggregate
    never lost a measurement.
    """
    from gateway.slash_commands import (
        _resident_thin_snapshot, render_thin_last_turn_lines,
    )

    agent = SimpleNamespace(
        session_input_tokens=400_000, session_output_tokens=12_338,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_input_tokens_unknown=False, session_output_tokens_unknown=False,
        session_usage_unknown=False,
        last_turn_usage={
            "input_tokens": 0, "output_tokens": 0,
            "input_tokens_unknown": True, "output_tokens_unknown": True,
            "usage_unknown": True,
        },
    )
    snap = _resident_thin_snapshot(agent)
    assert not any(k.endswith("_unknown") for k in snap)
    text = "\n".join(render_thin_last_turn_lines(snap, "test"))
    assert "Total (billed in+out): 412,338" in text
    assert "unknown" not in text


def test_f4_the_session_latch_is_cleared_by_reset_session_state():
    """The latch is absorbing WITHIN a session, not across a reset.

    `reset_session_state` zeroes the five counters, so it must clear their
    discriminator too — otherwise a fresh session's genuinely measured totals
    inherit the old session's `unknown` forever.
    """
    from agent.usage_pricing import USAGE_UNKNOWN_FIELDS
    from gateway.slash_commands import _resident_thin_snapshot
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    for flag in USAGE_UNKNOWN_FIELDS:
        setattr(agent, f"session_{flag}", True)
    AIAgent.reset_session_state(agent)

    for flag in USAGE_UNKNOWN_FIELDS:
        assert getattr(agent, f"session_{flag}") is False, flag
    assert not any(k.endswith("_unknown") for k in _resident_thin_snapshot(agent))


def test_f8_narrowness_a_measured_resident_session_still_renders_numbers():
    from gateway.slash_commands import (
        _resident_thin_snapshot, render_thin_last_turn_lines,
    )

    measured_agent = SimpleNamespace(
        session_input_tokens=4000, session_output_tokens=120,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_input_tokens_unknown=False, session_output_tokens_unknown=False,
        session_usage_unknown=False,
    )
    snap = _resident_thin_snapshot(measured_agent)
    assert not any(k.endswith("_unknown") for k in snap), (
        "a measured session must not acquire a spurious unknown flag"
    )
    text = "\n".join(render_thin_last_turn_lines(snap, "test"))
    assert "Total (billed in+out): 4,120" in text
    assert "unknown" not in text


def test_f8_narrowness_no_last_turn_yet_is_not_forced_unknown():
    """A fresh agent with no turn recorded must not be labelled unmeasured."""
    from gateway.slash_commands import _resident_thin_snapshot

    fresh = SimpleNamespace(
        session_input_tokens=0, session_output_tokens=0,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0, last_turn_usage=None,
    )
    assert not any(k.endswith("_unknown") for k in _resident_thin_snapshot(fresh))


# --------------------------------------------------------------------------
# F6 — the legacy latch must not mark a genuinely zero-token turn UNKNOWN.
# --------------------------------------------------------------------------


def test_f6_legacy_latch_leaves_a_never_accounted_zero_token_row_measured(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.blackbox import store

    db_path = store._db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, parent_turn_id TEXT, is_subagent INT,
            ts_start REAL, ts_end REAL, profile TEXT, provider TEXT, model TEXT,
            platform TEXT, chat_id TEXT, chat_name TEXT, api_calls INT, tools TEXT,
            input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,
            reasoning INT, context_used INT, context_length INT, cost_usd REAL,
            cost_status TEXT, interrupted INT, alerted INT DEFAULT 0,
            user_text TEXT, final_text TEXT
        )
        """
    )
    # Interrupted before any API call fired: a MEASURED zero, never accounted.
    legacy.execute(
        "INSERT INTO turns (turn_id, input_tokens, output_tokens, cache_read, "
        "cache_write, cost_usd, cost_status) "
        "VALUES ('interrupted', 0, 0, 0, 0, NULL, NULL)"
    )
    # The old code itself could not account this one: genuinely ambiguous.
    legacy.execute(
        "INSERT INTO turns (turn_id, input_tokens, output_tokens, cache_read, "
        "cache_write, cost_usd, cost_status) "
        "VALUES ('ambiguous', 0, 0, 0, 0, NULL, 'unknown')"
    )
    legacy.commit()
    legacy.close()

    with store._connect() as conn:
        got = {
            r[0]: r[1]
            for r in conn.execute("SELECT turn_id, usage_unknown FROM turns")
        }
    assert got["interrupted"] == 0, (
        "latching a genuinely zero-token turn is irreversible: reprice_unpriced "
        "short-circuits on any unknown flag, so it can never heal to "
        "priced_zero again (r6 finding 6)"
    )
    assert got["ambiguous"] == 1, (
        "the latch must stay reachable for the population it exists for"
    )


# --------------------------------------------------------------------------
# Standing contracts that must NOT be weakened by any of the above.
# --------------------------------------------------------------------------


def test_control_no_call_normalization_is_still_a_measured_zero():
    usage = normalize_usage(None)
    assert usage.usage_unknown is False
    assert usage.total_tokens_unknown is False
    assert usage.total_tokens == 0


def test_control_a_null_details_container_is_still_a_measured_zero():
    usage = normalize_usage(
        _Wire({"prompt_tokens": 100, "completion_tokens": 50,
               "prompt_tokens_details": None}),
        provider="openai",
        api_mode="chat_completions",
    )
    assert usage.cache_read_tokens_unknown is False
    assert usage.total_tokens_unknown is False


def test_control_fully_unknown_still_sets_every_discriminator():
    usage = CanonicalUsage.fully_unknown()
    for key in USAGE_UNKNOWN_FIELDS:
        assert getattr(usage, key) is True, key


def test_control_an_uninitialised_latch_attribute_is_not_an_unknown():
    """A non-bool `session_*_unknown` must read as NOT-unknown, not as truthy.

    The latch is written as a real bool by `agent/agent_init.py` (False) and
    `agent/conversation_loop.py` (True), so any other value means the attribute
    was never initialised on this object. The dominant such object is a
    `MagicMock`, which auto-creates every attribute access as a truthy child —
    so the original `if getattr(agent, f"session_{flag}", False):` collapsed a
    fully measured session to `unknown` on the resident lane and turned
    `tests/gateway/test_usage_command.py` red in CI.

    Every other pin in this file builds its agent from `SimpleNamespace` or a
    real `AIAgent`, which is precisely why none of them could see this. The five
    counters beside these flags are coerced through `as_int` for the same
    reason; this is the flags' half of that contract.
    """
    from unittest.mock import MagicMock

    from gateway.slash_commands import (
        _resident_thin_snapshot, render_thin_last_turn_lines,
    )

    agent = MagicMock()
    agent.session_input_tokens = 35_000
    agent.session_output_tokens = 10_000
    agent.session_cache_read_tokens = 5_000
    agent.session_cache_write_tokens = 2_000
    agent.session_reasoning_tokens = 0

    snap = _resident_thin_snapshot(agent)
    assert not any(k.endswith("_unknown") for k in snap), (
        "a mock's auto-created attribute is not a declared UNKNOWN"
    )
    text = "\n".join(render_thin_last_turn_lines(snap, "resident"))
    assert "35,000" in text
    assert "unknown" not in text


def test_control_the_latch_still_fires_for_a_real_declared_unknown():
    """NARROWNESS: `is True` must not stop a genuine latch from being read."""
    from unittest.mock import MagicMock

    from gateway.slash_commands import _resident_thin_snapshot

    agent = MagicMock()
    agent.session_input_tokens = 35_000
    agent.session_output_tokens = 10_000
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_usage_unknown = True

    assert _resident_thin_snapshot(agent).get("usage_unknown") is True
