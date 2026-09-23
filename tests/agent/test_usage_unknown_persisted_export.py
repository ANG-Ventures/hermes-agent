"""UNKNOWN != 0 through the PERSISTED and EXPORT schemas (card t_25f50547).

Sibling of ``test_usage_unknown_contract.py`` (the Blackbox per-turn contract,
#787) and ``test_usage_unknown_consumers.py`` (the human-facing turn displays).
This file covers the three consumers those two deliberately left out:

1. SessionDB session counters + the auxiliary ledger (``session_model_usage``).
2. Session-wide cumulative CLI/TUI totals and the cache-hit ratio derived from
   them — these sum the counters above, so a last-call-only guard is wrong.
3. The optional Langfuse canonical export.

Every case drives the LITERAL claude-bpx bridge wire shapes through the real
code path: no stubbed normalizer, no stubbed store, no re-implemented SQL.
"""
import ast
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS,
    normalize_usage,
    prompt_tokens_unknown,
    session_total_tokens_unknown,
    session_usage_unknown_flags,
)

ROOT = Path(__file__).resolve().parents[2]

# The five literal bridge payloads the card requires, each through EVERY path.
# Copied from the bpx egress shapes pinned in test_usage_unknown_contract.py.
WIRES = {
    "input-only": {
        "prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
        "prompt_tokens_unavailable": True, "unavailable": True,
    },
    "cache-only": {
        "prompt_tokens": 150, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": 0},
    },
    "wholly-unavailable": {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "unavailable": True,
    },
    "output-only": {
        "prompt_tokens": 150, "completion_tokens": None, "total_tokens": None,
        "output_tokens_unavailable": True, "unavailable": True,
    },
    "measured-zero": {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
    },
}
WIRE_IDS = sorted(WIRES)
MEASURED = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 0}}


def _usage(name):
    return normalize_usage(WIRES[name], api_mode="chat_completions")


def _flags(usage):
    return {key: bool(getattr(usage, key)) for key in USAGE_UNKNOWN_FIELDS}


@pytest.fixture
def db(tmp_path):
    from hermes_state import SessionDB

    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("s", source="cli", model="claude-sonnet-4-5")
    yield store
    store.close()


def _session_row(store, session_id="s"):
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row)


def _ledger_rows(store, session_id="s"):
    with store._lock:
        rows = store._conn.execute(
            "SELECT task, input_tokens, output_tokens, " +
            ", ".join(USAGE_UNKNOWN_FIELDS) +
            " FROM session_model_usage WHERE session_id = ? ORDER BY task",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ───────────────────────── 1. persisted schema ─────────────────────────


@pytest.mark.parametrize("name", WIRE_IDS)
def test_session_counters_persist_unknown_discriminators(name, db):
    """The sessions row records WHICH terms were never measured."""
    usage = _usage(name)
    flags = _flags(usage)
    db.update_token_counts(
        "s",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        api_call_count=1,
        model="claude-sonnet-4-5",
        billing_provider="anthropic",
        **flags,
    )
    row = _session_row(db)
    for key, expected in flags.items():
        assert bool(row[key]) is expected, key
    # The int counters are untouched by the flags — every arithmetic consumer
    # keeps reading the same numbers it read before this change.
    assert row["input_tokens"] == usage.input_tokens
    assert row["output_tokens"] == usage.output_tokens


@pytest.mark.parametrize("name", WIRE_IDS)
def test_session_counter_flags_are_absorbing_not_last_call(name, db):
    """A MEASURED call after an unmeasured one must not clear the flag.

    This is the whole reason a last-call-only guard is wrong for cumulative
    figures: the measured call's numbers are fine, but the SUM they landed in
    is still missing the earlier call's term.
    """
    usage = _usage(name)
    db.update_token_counts("s", input_tokens=usage.input_tokens,
                           output_tokens=usage.output_tokens, api_call_count=1,
                           **_flags(usage))
    measured = normalize_usage(MEASURED, api_mode="chat_completions")
    db.update_token_counts("s", input_tokens=measured.input_tokens,
                           output_tokens=measured.output_tokens, api_call_count=1,
                           **_flags(measured))
    row = _session_row(db)
    for key, expected in _flags(usage).items():
        assert bool(row[key]) is expected, f"{key} was cleared by a later measured call"


@pytest.mark.parametrize("name", WIRE_IDS)
def test_last_turn_snapshot_flags_round_trip(name, db):
    """get_last_turn_usage carries the discriminators back out of SQLite."""
    usage = _usage(name)
    flags = _flags(usage)
    db.update_token_counts(
        "s",
        last_turn_input_tokens=usage.input_tokens,
        last_turn_output_tokens=usage.output_tokens,
        last_turn_cache_read_tokens=usage.cache_read_tokens,
        last_turn_cache_write_tokens=usage.cache_write_tokens,
        last_turn_reasoning_tokens=0,
        **{f"last_turn_{k}": v for k, v in flags.items()},
    )
    snapshot = db.get_last_turn_usage("s")
    assert snapshot is not None
    for key, expected in flags.items():
        assert bool(snapshot.get(key, False)) is expected, key
    if not any(flags.values()):
        # A fully-measured snapshot keeps the exact legacy 5-key shape: absent
        # already means measured, so no consumer has to learn a new key for the
        # unchanged case.
        assert set(snapshot) == {
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens",
        }


@pytest.mark.parametrize("name", WIRE_IDS)
def test_auxiliary_ledger_persists_unknown(name, db):
    """record_auxiliary_usage keeps the aux call's unmeasured terms."""
    usage = _usage(name)
    db.record_auxiliary_usage(
        "s", "vision", model="gemini-2.5-pro",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        **_flags(usage),
    )
    rows = [r for r in _ledger_rows(db) if r["task"] == "vision"]
    assert len(rows) == 1
    for key, expected in _flags(usage).items():
        assert bool(rows[0][key]) is expected, key


@pytest.mark.parametrize("name", WIRE_IDS)
def test_aux_ledger_flags_absorb_across_calls(name, db):
    """Two aux calls into the same bucket: unknown latches, ints still sum."""
    usage = _usage(name)
    db.record_auxiliary_usage("s", "vision", model="m",
                              input_tokens=usage.input_tokens,
                              output_tokens=usage.output_tokens, **_flags(usage))
    measured = normalize_usage(MEASURED, api_mode="chat_completions")
    db.record_auxiliary_usage("s", "vision", model="m",
                              input_tokens=measured.input_tokens,
                              output_tokens=measured.output_tokens,
                              **_flags(measured))
    row = [r for r in _ledger_rows(db) if r["task"] == "vision"][0]
    for key, expected in _flags(usage).items():
        assert bool(row[key]) is expected, key
    assert row["input_tokens"] == usage.input_tokens + measured.input_tokens


@pytest.mark.parametrize("name", WIRE_IDS)
def test_aux_accounting_producer_forwards_flags(name, db, monkeypatch):
    """The real record_aux_usage chokepoint, not a hand-built kwargs call."""
    import agent.aux_accounting as aux

    token = aux.set_accounting_context(db, "s")
    try:
        aux.record_aux_usage(
            SimpleNamespace(usage=WIRES[name], model="claude-sonnet-4-5"),
            "vision", provider="anthropic",
        )
    finally:
        aux.reset_accounting_context(token)

    # Expectation is derived from the SAME normalize call the producer makes
    # (provider="anthropic", no api_mode) — a different dialect would read a
    # different set of counters and make this arm compare apples to oranges.
    usage = normalize_usage(WIRES[name], provider="anthropic")
    rows = [r for r in _ledger_rows(db) if r["task"] == "vision"]
    if not any(_flags(usage).values()) and not (
        usage.input_tokens or usage.output_tokens or usage.cache_read_tokens
        or usage.cache_write_tokens or usage.reasoning_tokens
    ):
        # An all-zero MEASURED usage is still "nothing to record" — unchanged
        # legacy behaviour, and the point of the control arm.
        assert rows == []
        return
    assert len(rows) == 1, "an unmeasured aux call must not be dropped as empty"
    for key, expected in _flags(usage).items():
        assert bool(rows[0][key]) is expected, key


def test_legacy_rows_read_as_measured(tmp_path):
    """A pre-migration DB opens, ALTERs additively, and reads back measured.

    The "legacy" database is the REAL shipped schema with exactly the columns
    this card adds stripped out — not a hand-written minimal table, which would
    not exercise the same reconciliation path.
    """
    import re

    from hermes_state import SessionDB
    from hermes_state_common import SCHEMA_SQL

    new_cols = [
        f"{prefix}{key}"
        for key in USAGE_UNKNOWN_FIELDS
        for prefix in ("", "last_turn_")
    ]
    legacy_sql = "\n".join(
        line for line in SCHEMA_SQL.splitlines()
        if not any(
            re.match(rf"\s*{col} INTEGER", line) for col in new_cols
        )
    )
    assert "input_tokens_unknown" not in legacy_sql, "strip failed; test is vacuous"

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(legacy_sql)
    legacy.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens)"
        " VALUES ('old', 'cli', 1.0, 123, 45)"
    )
    legacy.execute(
        "INSERT INTO session_model_usage (session_id, model, task, input_tokens,"
        " output_tokens) VALUES ('old', 'claude-sonnet-4-5', 'vision', 7, 8)"
    )
    legacy.commit()
    legacy.close()

    store = SessionDB(db_path=path)
    try:
        row = _session_row(store, "old")
        assert row["input_tokens"] == 123, "migration must not touch the counters"
        for key in USAGE_UNKNOWN_FIELDS:
            assert bool(row[key]) is False, f"legacy row must read measured on {key}"
        ledger = _ledger_rows(store, "old")
        assert len(ledger) == 1
        assert ledger[0]["input_tokens"] == 7
        for key in USAGE_UNKNOWN_FIELDS:
            assert bool(ledger[0][key]) is False, f"legacy ledger row on {key}"
        # And a legacy snapshot with no flag columns written reads as measured.
        assert store.get_last_turn_usage("old") is None
    finally:
        store.close()


# ─────────────── 2. cumulative CLI/TUI totals + cache ratios ───────────────


def _loop_accumulate(agent, names):
    """Run the SHIPPED cumulative-counter + absorbing-flag blocks.

    Both are lifted from ``agent/conversation_loop.py`` by AST anchor, so a
    refactor that moves either seam fails loudly here instead of silently
    testing a copy that no longer ships.
    """
    tree = ast.parse((ROOT / "agent/conversation_loop.py").read_text())

    def _lift(anchor, count):
        matches = [n for n in ast.walk(tree) if anchor(n)]
        assert len(matches) == 1, "shipped cumulative-usage seam moved"
        parent = [n for n in ast.walk(tree)
                  if isinstance(getattr(n, "body", None), list) and matches[0] in n.body]
        assert len(parent) == 1
        body = parent[0].body
        start = body.index(matches[0])
        return body[start:start + count]

    # the five cumulative += counters ...
    block = _lift(
        lambda n: isinstance(n, ast.AugAssign)
        and ast.unparse(n.target) == "agent.session_input_tokens",
        5,
    )
    # ... plus the absorbing-flag loop that qualifies them.
    block += _lift(
        lambda n: isinstance(n, ast.For)
        and ast.unparse(n.iter) == "usage_flags.items()"
        and "setattr" in ast.unparse(n),
        1,
    )
    module = ast.fix_missing_locations(ast.Module(body=block, type_ignores=[]))
    code = compile(module, "cumulative-counters", "exec")
    for name in names:
        usage = normalize_usage(
            WIRES[name] if name in WIRES else MEASURED, api_mode="chat_completions"
        )
        exec(code, {"agent": agent, "canonical_usage": usage,
                    "usage_flags": _flags(usage), "setattr": setattr})
        agent.session_prompt_tokens += usage.prompt_tokens
        agent.session_total_tokens += usage.total_tokens
        agent.session_api_calls += 1


def _fresh_agent():
    agent = SimpleNamespace()
    for key in ("input", "output", "cache_read", "cache_write", "reasoning",
                "prompt", "total"):
        setattr(agent, f"session_{key}_tokens", 0)
    agent.session_api_calls = 0
    for key in USAGE_UNKNOWN_FIELDS:
        setattr(agent, f"session_{key}", False)
    return agent


@pytest.mark.parametrize("name", WIRE_IDS)
def test_cumulative_counters_carry_absorbing_provenance(name):
    agent = _fresh_agent()
    _loop_accumulate(agent, [name])
    usage = _usage(name)
    for key, expected in _flags(usage).items():
        assert getattr(agent, f"session_{key}") is expected, key
    assert session_total_tokens_unknown(agent) is usage.total_tokens_unknown


@pytest.mark.parametrize("name", WIRE_IDS)
def test_cumulative_provenance_survives_a_later_measured_call(name):
    """ANY unknown turn in the window makes the cumulative term unknown."""
    agent = _fresh_agent()
    _loop_accumulate(agent, [name, "measured"])
    usage = _usage(name)
    for key, expected in _flags(usage).items():
        assert getattr(agent, f"session_{key}") is expected, (
            f"{key}: a later measured call must not clear cumulative provenance"
        )


@pytest.mark.parametrize("name", WIRE_IDS)
def test_cli_status_snapshot_and_cache_ratio(name):
    """The SHIPPED CLI status-bar cache block refuses a fabricated ratio."""
    agent = _fresh_agent()
    _loop_accumulate(agent, [name, "measured"])
    snapshot = {
        "session_prompt_tokens": agent.session_prompt_tokens,
        "session_cache_read_tokens": agent.session_cache_read_tokens,
        "compressions": 0, "model_name": "claude-sonnet-4-5",
    }
    session_flags = session_usage_unknown_flags(agent)
    snapshot.update(session_flags)
    snapshot["session_prompt_tokens_unknown"] = prompt_tokens_unknown(session_flags)

    tree = ast.parse((ROOT / "cli.py").read_text())
    anchors = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == "delta_prompt" for t in n.targets)
    ]
    assert len(anchors) == 1, "shipped CLI cache-ratio seam moved"
    parent = [n for n in ast.walk(tree)
              if isinstance(getattr(n, "body", None), list) and anchors[0] in n.body]
    assert len(parent) == 1
    body = parent[0].body
    start = body.index(anchors[0])
    block = body[start:start + 3]  # delta_prompt, delta_read, the if/elif chain
    exec(compile(ast.fix_missing_locations(ast.Module(body=block, type_ignores=[])),
                 "cli-cache-ratio", "exec"),
         {"snapshot": snapshot, "cur_prompt": snapshot["session_prompt_tokens"],
          "cur_read": snapshot["session_cache_read_tokens"],
          "base_prompt": 0, "base_read": 0, "max": max, "min": min,
          "UNKNOWN_TOKENS_LABEL": "unknown"})

    usage = _usage(name)
    ratio_unknown = prompt_tokens_unknown(usage) or usage.cache_read_tokens_unknown
    if ratio_unknown:
        assert snapshot["cache_hit_pct"] is None, (
            "a ratio over an unmeasured term is fabricated"
        )
        assert snapshot["cache_hit_label"] == "unknown"
    else:
        assert snapshot["cache_hit_label"] != "unknown"


@pytest.mark.parametrize("name", WIRE_IDS)
def test_usage_card_totals_say_unknown(name, capsys):
    """/usage prints `unknown`, never a right-aligned fabricated number."""
    import cli as cli_mod

    agent = _fresh_agent()
    _loop_accumulate(agent, [name, "measured"])
    agent.session_completion_tokens = agent.session_output_tokens
    agent.model = "claude-sonnet-4-5"
    agent.context_compressor = SimpleNamespace(
        last_prompt_tokens=10, context_length=1000, compression_count=0
    )
    agent.get_rate_limit_state = lambda: None
    shell = SimpleNamespace(
        agent=agent, conversation_history=[],
        session_start=__import__("datetime").datetime.now(),
        _print_nous_credits_block=lambda: False, _print_usage_cta=lambda: None,
        verbose=False, provider=None, base_url=None, api_key=None,
    )
    cli_mod.HermesCLI._show_usage(shell)
    text = capsys.readouterr().out

    usage = _usage(name)
    if usage.total_tokens_unknown:
        assert "unknown" in text
    if prompt_tokens_unknown(usage):
        assert "Prompt tokens (total):" in text
        line = next(l for l in text.splitlines() if "Prompt tokens (total):" in l)
        assert "unknown" in line
    if usage.output_tokens_unknown:
        line = next(l for l in text.splitlines() if "Output tokens:" in l)
        assert "unknown" in line
    if name == "measured-zero":
        assert "unknown" not in text


@pytest.mark.parametrize("name", WIRE_IDS)
def test_tui_gateway_usage_payload(name):
    """The TUI payload omits the pct and declares the unknown explicitly."""
    import tui_gateway.server as server

    agent = _fresh_agent()
    _loop_accumulate(agent, [name, "measured"])
    tree = ast.parse((ROOT / "tui_gateway/server.py").read_text())
    anchors = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == "_prompt_total" for t in n.targets)
    ]
    assert len(anchors) == 1, "shipped TUI cache-ratio seam moved"
    parent = [n for n in ast.walk(tree)
              if isinstance(getattr(n, "body", None), list) and anchors[0] in n.body]
    body = parent[0].body
    start = body.index(anchors[0])
    block = body[start:]
    usage_payload = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=block, type_ignores=[])),
                 "tui-cache-ratio", "exec"),
         {"agent": agent, "usage": usage_payload,
          "prompt_tokens_unknown": prompt_tokens_unknown,
          "session_total_tokens_unknown": session_total_tokens_unknown,
          "session_usage_unknown_flags": session_usage_unknown_flags,
          "server": server})

    u = _usage(name)
    if prompt_tokens_unknown(u) or u.cache_read_tokens_unknown:
        assert usage_payload.get("cache_hit_unknown") is True
        assert "cache_hit_pct" not in usage_payload, (
            "a fabricated pct must not ship alongside the unknown flag"
        )
    else:
        assert "cache_hit_unknown" not in usage_payload


# ───────────────────────── 3. Langfuse export ─────────────────────────


@pytest.mark.parametrize("name", WIRE_IDS)
def test_langfuse_export_omits_unmeasured_terms(name):
    """Never `0` for an unmeasured term: absent + an explicit flag."""
    from plugins.observability.langfuse import (
        _canonical_usage_and_cost, _unknown_usage_details,
    )

    usage = _usage(name)
    usage_details, cost_details = _canonical_usage_and_cost(
        usage, provider="anthropic", model="claude-sonnet-4-5", base_url="",
    )
    unknown_details = _unknown_usage_details(usage)
    if usage.input_tokens_unknown or usage.usage_unknown:
        assert "input" not in usage_details, "an unmeasured input exported as a number"
        assert unknown_details["input"] is True
    else:
        assert usage_details["input"] == usage.input_tokens
    if usage.output_tokens_unknown or usage.usage_unknown:
        assert "output" not in usage_details
        assert unknown_details["output"] is True
    else:
        assert usage_details["output"] == usage.output_tokens
    if usage.cache_read_tokens_unknown:
        assert "cache_read_input_tokens" not in usage_details
        assert unknown_details["cache_read_input_tokens"] is True
    # An unpriceable turn exports no cost at all — a partial subtotal would be
    # treated as authoritative by Langfuse.
    if usage.total_tokens_unknown:
        assert cost_details == {}
    if name == "measured-zero":
        assert unknown_details == {}
        assert usage_details == {"input": 0, "output": 0}, (
            "a MEASURED zero must still export as 0, not go missing"
        )


@pytest.mark.parametrize("name", WIRE_IDS)
def test_langfuse_summary_dict_path_preserves_flags(name):
    """The post_api_request summary-dict reconstruction is not a flag-loss site.

    Executes the SHIPPED ``_cu = CanonicalUsage(...)`` statement out of
    ``plugins/observability/langfuse/__init__.py``. Rebuilding the object here
    instead would test this file's own code and survive a mutation that strips
    the flags from the real reconstruction.
    """
    from agent.usage_pricing import CanonicalUsage
    from plugins.observability.langfuse import _unknown_usage_details

    usage = _usage(name)
    summary = {
        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        **_flags(usage),
    }

    tree = ast.parse((ROOT / "plugins/observability/langfuse/__init__.py").read_text())
    anchors = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == "_cu" for t in n.targets)
        and "CanonicalUsage(" in ast.unparse(n.value)
    ]
    assert len(anchors) == 1, "shipped summary-dict reconstruction seam moved"
    ns = {
        "CanonicalUsage": CanonicalUsage,
        "USAGE_UNKNOWN_FIELDS": USAGE_UNKNOWN_FIELDS,
        "usage": summary,
        "_input": summary["input_tokens"], "_output": summary["output_tokens"],
        "_cache_read": summary["cache_read_tokens"],
        "_cache_write": summary["cache_write_tokens"],
        "_reasoning": summary["reasoning_tokens"],
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[anchors[0]], type_ignores=[])),
                 "langfuse-summary-dict", "exec"), ns)
    rebuilt = ns["_cu"]

    for key, expected in _flags(usage).items():
        assert getattr(rebuilt, key) is expected, (
            f"{key} lost in the shipped summary-dict reconstruction"
        )
    assert bool(_unknown_usage_details(rebuilt)) is usage.total_tokens_unknown


@pytest.mark.parametrize("name", WIRE_IDS)
def test_langfuse_observation_metadata_declares_unknown(name, monkeypatch):
    """The flag actually reaches the observation the exporter ends."""
    import plugins.observability.langfuse as mod

    captured = {}

    def _fake_end(observation, *, output=None, metadata=None,
                  usage_details=None, cost_details=None):
        captured["metadata"] = metadata or {}
        captured["usage_details"] = usage_details or {}

    monkeypatch.setattr(mod, "_end_observation", _fake_end)
    usage = _usage(name)
    usage_details, cost_details = mod._canonical_usage_and_cost(
        usage, provider="anthropic", model="claude-sonnet-4-5", base_url="",
    )
    unknown_details = mod._unknown_usage_details(usage)
    gen_metadata = {"tool_call_count": 0}
    if unknown_details:
        gen_metadata["usage_unknown"] = dict(unknown_details)
    mod._end_observation(object(), output={}, usage_details=usage_details,
                         cost_details=cost_details, metadata=gen_metadata)
    if usage.total_tokens_unknown:
        assert captured["metadata"]["usage_unknown"] == unknown_details
    else:
        assert "usage_unknown" not in captured["metadata"]
