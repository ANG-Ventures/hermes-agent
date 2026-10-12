"""Reasoning EFFORT on the /context + /usage last-turn card (t_09a1e42a).

The card printed ``• Model:`` but never the effort the turn ran at, because the
turn ledger only recorded reasoning TOKENS. These pins cover the whole path:

- the ledger writer records the RESOLVED effort (session override folded in,
  ``none`` when disabled), not the config default;
- ``render_last_turn_record`` prints ``• Reasoning: <effort>`` directly under
  ``• Model:``, and ``• Reasoning: —`` for a NULL so the slot is never silently
  absent;
- an OLD-schema turns.db migrates by add-column and its rows render ``—``;
- /usage (``_render_last_turn_card``) and /context (the shared renderer over
  ``compute_last_turn_record``) emit the same Reasoning line for one turn.
"""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from plugins.blackbox import store
from plugins.blackbox.last_turn import compute_last_turn_record, render_last_turn_record
from plugins.blackbox.record import TurnRecord


def _rec(**over):
    now = time.time()
    base = dict(
        found=True, platform="discord", chat_id="1", chat_name="x",
        model="claude-fable-5-1", provider="claude-alr", profile="default",
        ts_start=now - 10, ts_end=now, api_calls=1, tools="[]",
        input_tokens=100, output_tokens=50, cache_read=900, cache_write=0,
        reasoning=0, context_used=1000, context_length=1_000_000,
        cost_usd=0.01, cost_status="estimated",
    )
    base.update(over)
    return base


def _line_after_model(lines):
    model_i = next(i for i, l in enumerate(lines) if l.startswith("• Model:"))
    return lines[model_i + 1]


# --------------------------------------------------------------------------- #
# (a) + (b): renderer
# --------------------------------------------------------------------------- #
def test_render_reasoning_effort_directly_under_model():
    lines = render_last_turn_record(_rec(reasoning_effort="xhigh"))
    assert "• Model: claude-alr/claude-fable-5-1" in lines
    assert _line_after_model(lines) == "• Reasoning: xhigh"


def test_render_null_effort_shows_dash_not_absence():
    lines = render_last_turn_record(_rec(reasoning_effort=None))
    assert _line_after_model(lines) == "• Reasoning: —"
    # The slot is always rendered exactly once.
    assert sum(1 for l in lines if l.startswith("• Reasoning:")) == 1


def test_render_reasoning_sits_before_route_on_proxy_lanes():
    lines = render_last_turn_record(_rec(provider="cpa", model="claude-opus-4-8", reasoning_effort="high"))
    model_i = next(i for i, l in enumerate(lines) if l.startswith("• Model:"))
    assert lines[model_i + 1] == "• Reasoning: high"
    assert lines[model_i + 2].startswith("• Route:")


# --------------------------------------------------------------------------- #
# (c): the ledger writer records the RESOLVED effort
# --------------------------------------------------------------------------- #
def _effort(agent, calls=None):
    from agent.turn_finalizer import turn_reasoning_effort
    return turn_reasoning_effort(agent, calls)


CALL = [{"input_tokens": 1}]


def test_resolved_effort_is_the_session_override_not_the_config_default(monkeypatch):
    # The gateway folds a /reasoning override into the live agent config; the
    # ledger must read THAT, never re-resolve config.yaml.
    import hermes_constants
    monkeypatch.setattr(
        hermes_constants, "resolve_reasoning_config",
        lambda *a, **k: {"enabled": True, "effort": "medium"},
    )
    agent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "xhigh"})
    assert _effort(agent, CALL) == "xhigh"


def test_resolved_effort_none_when_reasoning_disabled():
    agent = SimpleNamespace(reasoning_config={"enabled": False})
    assert _effort(agent, CALL) == "none"


def test_no_api_call_records_unknown_not_the_configured_effort():
    # A turn that failed before its first request ran at no effort at all;
    # persisting the configured xhigh would fabricate one (Prism #1906 r2).
    agent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "xhigh"})
    assert _effort(agent, []) is None
    assert _effort(agent, None) is None


def test_unset_config_records_the_provider_default_that_ran():
    # No session/config value on a custom/OpenAI-compatible route: the profile
    # default (medium) is what the turn ran at, resolved through the real
    # unset_reasoning_default.
    agent = SimpleNamespace(
        reasoning_config=None, api_mode="chat_completions", provider="custom", model="some-model",
    )
    assert _effort(agent, CALL) == "medium"


def test_unset_config_ignores_the_ephemeral_continuation_disable():
    # The one-shot "answer without thinking" continuation rewrites the LAST
    # request's wire config to a disable; the turn still ran at the default
    # effort, so the ledger must not record "none" (Prism #1906 r2).
    agent = SimpleNamespace(
        reasoning_config=None, api_mode="chat_completions", provider="custom", model="some-model",
        _ephemeral_reasoning_off=True,
    )
    assert _wire(agent) == {"enabled": False, "effort": "none"}
    assert agent._wire_reasoning_config == {"enabled": False, "effort": "none"}
    assert _effort(agent, CALL) == "medium"


def _wire(agent):
    """Build the request's reasoning config through the REAL wire resolver."""
    from agent.chat_completion_helpers import _reasoning_config_for_wire
    return _reasoning_config_for_wire(agent)


def test_recovery_floor_records_the_effort_actually_sent_not_the_disable():
    # Route said "reasoning is mandatory": recovery keeps agent.reasoning_config
    # as the user's disable but sends the floor effort. The ledger must record
    # what ran (the floor), never "none" (Prism #1906 r1 3685d9ec2713).
    from agent.auxiliary_reasoning_floor import REASONING_FLOOR_EFFORT
    agent = SimpleNamespace(
        reasoning_config={"enabled": False}, api_mode="chat_completions",
        _reasoning_disable_rejected=True, _reasoning_floor_required=True,
    )
    sent = _wire(agent)
    assert sent == {"enabled": True, "effort": REASONING_FLOOR_EFFORT}
    assert _effort(agent, CALL) == REASONING_FLOOR_EFFORT


def test_recovery_omission_records_unknown_not_the_rejected_level():
    # Route rejected the configured LEVEL: recovery omits the reasoning fields
    # (route default applies). The requested level did not run; the effective
    # effort is unknown, so the row stays NULL and renders "—".
    agent = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "max"}, api_mode="chat_completions",
        _reasoning_effort_rejected=True,
    )
    assert _wire(agent) is None
    assert _effort(agent, CALL) is None
    assert _line_after_model(render_last_turn_record(_rec(reasoning_effort=None))) == "• Reasoning: —"


def test_recovery_relay_drop_of_a_disable_records_unknown():
    # Relay that does not know the field: recovery drops the disable, nothing
    # is sent, route default applies -> unknown.
    agent = SimpleNamespace(
        reasoning_config={"enabled": False}, api_mode="chat_completions",
        _reasoning_disable_rejected=True, _reasoning_floor_required=False,
    )
    assert _wire(agent) is None
    assert _effort(agent, CALL) is None


def test_on_session_end_persists_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import plugins.blackbox as bb
    bb._sessions.clear()
    monkeypatch.setattr(bb, "_config", lambda: {
        "enabled": True, "cost_alert_threshold_usd": 999.0,
        "store_text": True, "record_subagents": True,
    })
    monkeypatch.setattr(bb, "compute_turn_cost", lambda *a, **k: (0.0, "unknown", {}))
    usage = {"api_calls": 1, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0, "total_tokens": 15, "latency_s": 1.0,
             "calls": [], "context_used": 10, "context_length": 1000}
    for turn_id, effort in (("turn_x", "xhigh"), ("turn_m", "medium"), ("turn_n", "none")):
        bb._on_session_end(
            session_id="s1", completed=True, interrupted=False,
            model="claude-fable-5-1", platform="discord", provider="claude-alr",
            user_message="hi", final_response="ok", turn_usage=dict(usage),
            turn_id=turn_id, chat_id="42", chat_name="c", reasoning_effort=effort,
        )
        assert store.get_turn(turn_id)["reasoning_effort"] == effort


def _call():
    return {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 2, "latency_s": 0.1}


def test_emit_session_end_threads_resolved_effort_into_the_hook(monkeypatch):
    """The finalizer's hook fire carries ``reasoning_effort`` from the LIVE agent config."""
    import hermes_cli.lifecycle as lifecycle
    from agent import turn_finalizer as tf

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen.update(kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    agent = SimpleNamespace(
        session_id="s", model="m", platform="discord", provider="p",
        reasoning_config={"enabled": True, "effort": "xhigh"},
        _blackbox_compaction={}, context_compressor=None,
    )
    tf.emit_session_end(
        agent, turn_id="t1", effective_task_id=None, completed=True, failed=False,
        interrupted=False, turn_exit_reason=None, original_user_message="hi",
        final_response="ok", turn_calls=[_call()], provisional=False,
    )
    assert seen.get("reasoning_effort") == "xhigh"


def test_emit_session_end_with_no_calls_records_unknown(monkeypatch):
    import hermes_cli.lifecycle as lifecycle
    from agent import turn_finalizer as tf

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen.update(kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    agent = SimpleNamespace(
        session_id="s", model="m", platform="discord", provider="p",
        reasoning_config={"enabled": True, "effort": "xhigh"},
        _blackbox_compaction={}, context_compressor=None,
    )
    tf.emit_session_end(
        agent, turn_id="t0", effective_task_id=None, completed=False, failed=True,
        interrupted=False, turn_exit_reason="failed", original_user_message="hi",
        final_response="", turn_calls=[], provisional=False,
    )
    assert "reasoning_effort" in seen and seen["reasoning_effort"] is None


# --------------------------------------------------------------------------- #
# (d): old-schema turns.db migrates by add-column; old rows render "—"
# --------------------------------------------------------------------------- #
def test_old_schema_db_migrates_and_old_rows_render_dash(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "blackbox" / "turns.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, parent_turn_id TEXT, is_subagent INT,
            ts_start REAL, ts_end REAL, profile TEXT, provider TEXT, model TEXT,
            platform TEXT, chat_id TEXT, chat_name TEXT, api_calls INT, tools TEXT,
            input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,
            reasoning INT, context_used INT, context_length INT,
            last_cache_read INT, last_cache_write INT, last_uncached INT,
            comp_calls_json TEXT, cost_usd REAL, cost_status TEXT,
            interrupted INT, alerted INT DEFAULT 0, user_text TEXT, final_text TEXT
        )
        """)
    now = time.time()
    conn.execute(
        "INSERT INTO turns (turn_id, ts_start, ts_end, provider, model, platform, chat_id, "
        "api_calls, tools, input_tokens, output_tokens, cache_read, cache_write, reasoning, "
        "context_used, context_length) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("turn_old", now - 5, now, "claude-alr", "claude-fable-5-1", "discord", "7",
         1, "[]", 10, 5, 0, 0, 0, 10, 1000),
    )
    conn.commit(); conn.close()

    # Opening through the store runs the guarded add-column migration.
    c = store._connect()
    cols = {r[1] for r in c.execute("PRAGMA table_info(turns)").fetchall()}
    old_effort = c.execute("SELECT reasoning_effort FROM turns WHERE turn_id='turn_old'").fetchone()[0]
    c.close()
    assert "reasoning_effort" in cols
    assert old_effort is None
    store._connect().close()  # idempotent re-open

    rec = compute_last_turn_record("discord", "7")
    assert rec["found"] and rec["turn_id"] == "turn_old"
    assert _line_after_model(render_last_turn_record(rec)) == "• Reasoning: —"

    # A new row on the migrated db carries the value.
    store.insert_turn(TurnRecord(
        turn_id="turn_new", ts_start=now, ts_end=now + 1, provider="claude-alr",
        model="claude-fable-5-1", platform="discord", chat_id="7", reasoning_effort="xhigh",
    ))
    rec = compute_last_turn_record("discord", "7")
    assert rec["turn_id"] == "turn_new"
    assert _line_after_model(render_last_turn_record(rec)) == "• Reasoning: xhigh"


# --------------------------------------------------------------------------- #
# (e): /usage and /context share the renderer -> identical Reasoning line
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("effort, expected", [("xhigh", "• Reasoning: xhigh"), (None, "• Reasoning: —")])
def test_usage_and_context_emit_the_same_reasoning_line(tmp_path, monkeypatch, effort, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import gateway.slash_commands as SC

    chan = "1559049790157881428"
    now = time.time()
    store.insert_turn(TurnRecord(
        turn_id="turn_parity", ts_start=now - 3, ts_end=now, profile="default",
        provider="claude-alr", model="claude-fable-5-1", platform="discord", chat_id=chan,
        api_calls=1, input_tokens=10, output_tokens=5, reasoning_effort=effort,
    ))

    # /context path: the shared renderer over the channel-scoped record.
    context_lines = render_last_turn_record(compute_last_turn_record("discord", chan))

    # /usage path: the gateway card helper (event.source -> same renderer).
    owner = next(
        obj for name in dir(SC)
        if isinstance((obj := getattr(SC, name)), type) and "_render_last_turn_card" in obj.__dict__
    )
    source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id=chan)
    usage_lines = owner._render_last_turn_card(object.__new__(owner), source, None)

    assert _line_after_model(context_lines) == expected
    assert _line_after_model(usage_lines) == expected
    assert [l for l in usage_lines if l.startswith("• Reasoning:")] == \
           [l for l in context_lines if l.startswith("• Reasoning:")]
