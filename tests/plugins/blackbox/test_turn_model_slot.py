"""Slot aliases P2 follow-up (t_5c87d313): every turn row records the slot asked for and the id it ran on.

A seat may name a model slot (``sol``); the runtime resolves it to the current id each turn. The turn
row now carries both: ``model`` is the resolved id (what the turn is priced by, at insert and on every
later reprice) and ``model_slot`` is the slot asked for (``''`` for a literal id, NULL on rows written
before the column existed). A slot the view cannot resolve is still recorded under the slot name and
books unpriced, as before.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

SOL_ID = "gpt-6.2-sol"      # cpa: $2 / $10 per M
FLIP_ID = "gpt-5.5"         # cpa: $5 / $30 per M
USAGE_CALL = dict(input_tokens=10_000, output_tokens=2_000, cache_read_tokens=0, cache_write_tokens=0)


def _write_view(root, slots):
    path = root / "config" / "model-slots.json"
    path.write_text(json.dumps({"rendered_from": "abc123", "slots": dict(slots)}), encoding="utf-8")
    st = path.stat()
    # slot_view caches on mtime: move it forward so a flip inside one second is seen.
    bump = getattr(_write_view, "_bump", 0) + 1
    _write_view._bump = bump
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1_000_000_000))


@pytest.fixture
def bb(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_view(root, {"sol": SOL_ID})
    import plugins.blackbox as bb

    bb._sessions.clear()
    monkeypatch.setattr(bb, "_config", lambda: {
        "enabled": True, "cost_alert_threshold_usd": 999.0, "alerts_enabled": False,
        "store_text": False, "record_subagents": True,
    })
    monkeypatch.setattr(bb, "_observe_pricing_sentinel", lambda *a, **k: None)
    bb._root = root
    return bb


def _end(bb, turn_id, model, **extra):
    usage = {"api_calls": 1, "input_tokens": USAGE_CALL["input_tokens"],
             "output_tokens": USAGE_CALL["output_tokens"], "cache_read_tokens": 0,
             "cache_write_tokens": 0, "latency_s": 1.0, "calls": [dict(USAGE_CALL)]}
    bb._on_session_end(
        session_id="s1", completed=True, interrupted=False, model=model, platform="discord",
        provider="cpa", user_message="hi", final_response="ok", turn_usage=usage,
        turn_id=turn_id, chat_id="42", chat_name="c", **extra,
    )
    from plugins.blackbox import store

    return store.get_turn(turn_id)


def _priced(model_id):
    from plugins.blackbox.cost import compute_turn_cost

    cost, status, _ = compute_turn_cost(model_id, "cpa", None, [dict(USAGE_CALL)])
    assert status == "estimated" and cost and cost > 0, (model_id, status, cost)
    return cost


def test_slot_turn_is_priced_by_its_resolved_id_and_records_the_slot(bb):
    # The live finalizer shape: agent.model is already the id, agent._model_slot the slot.
    row = _end(bb, "turn_slot", SOL_ID, model_slot="sol")
    assert row["model"] == SOL_ID
    assert row["model_slot"] == "sol"
    assert row["cost_status"] == "estimated"
    assert row["cost_usd"] == pytest.approx(_priced(SOL_ID))


def test_slot_name_reaching_the_recorder_is_resolved_at_record_time(bb):
    # A writer that hands the slot itself (an older caller, a lane-prefixed seat) must not leave
    # the slot in the model column: a later reprice would price it at whatever the slot is THEN.
    for turn_id, model in (("turn_bare", "sol"), ("turn_lane", "cpa/sol")):
        row = _end(bb, turn_id, model)
        assert row["model"] == SOL_ID, turn_id
        assert row["model_slot"] == model, turn_id
        assert row["cost_usd"] == pytest.approx(_priced(SOL_ID)), turn_id


def test_literal_turn_is_unchanged_and_records_an_empty_slot(bb):
    row = _end(bb, "turn_literal", SOL_ID, model_slot="")
    assert row["model"] == SOL_ID
    assert row["model_slot"] == ""
    assert row["cost_usd"] == pytest.approx(_priced(SOL_ID))
    # No model_slot kwarg at all (a host that predates it) with a literal id: same row.
    row = _end(bb, "turn_literal_nokw", SOL_ID)
    assert row["model"] == SOL_ID and row["model_slot"] == ""


def test_flip_between_two_turns_prices_each_by_its_own_id_now_and_on_reprice(bb):
    from plugins.blackbox import store
    from plugins.blackbox.cost import compute_turn_cost

    a = _end(bb, "turn_a", "sol")
    _write_view(bb._root, {"sol": FLIP_ID})
    b = _end(bb, "turn_b", "sol")
    assert (a["model"], a["model_slot"]) == (SOL_ID, "sol")
    assert (b["model"], b["model_slot"]) == (FLIP_ID, "sol")
    assert a["cost_usd"] == pytest.approx(_priced(SOL_ID))
    assert b["cost_usd"] == pytest.approx(_priced(FLIP_ID))
    assert a["cost_usd"] != pytest.approx(b["cost_usd"])

    # The reprice sweep reads the model column: after the flip, turn_a must still be priced as
    # gpt-6.2-sol, not as whatever ``sol`` points at today.
    con = sqlite3.connect(str(store._db_path()))
    con.execute("UPDATE turns SET cost_usd=NULL, cost_status=NULL, cost_uncached_usd=NULL, "
                "cost_cache_read_usd=NULL, cost_cache_write_usd=NULL, cost_output_usd=NULL")
    con.commit()
    con.close()

    def fn(m, p, t):  # tokens-reprice-sweep driver shape
        return compute_turn_cost(m, p, None, [dict(
            input_tokens=t["input_tokens"], output_tokens=t["output_tokens"],
            cache_read_tokens=t["cache_read_tokens"], cache_write_tokens=t["cache_write_tokens"])])

    store.reprice_unpriced(fn, apply=True)
    assert store.get_turn("turn_a")["cost_usd"] == pytest.approx(_priced(SOL_ID))
    assert store.get_turn("turn_b")["cost_usd"] == pytest.approx(_priced(FLIP_ID))


def test_slot_that_resolves_to_nothing_books_unpriced_under_the_slot(bb):
    (bb._root / "config" / "model-slots.json").unlink()
    row = _end(bb, "turn_unresolved", "sol", model_slot="sol")
    assert row["model"] == "sol"
    assert row["model_slot"] == "sol"
    assert row["cost_status"] == "unknown"
    assert row["cost_usd"] is None


def test_abandoned_turn_records_the_slot(bb):
    from plugins.blackbox import store

    bb._on_turn_abandoned(turn_id="turn_ab", session_id="s1", model=SOL_ID, provider="cpa",
                          platform="discord", chat_id="42", model_slot="sol", reason="shutdown")
    row = store.get_turn("turn_ab")
    assert row["model"] == SOL_ID and row["model_slot"] == "sol"


def _hook_kwargs(monkeypatch, agent, provisional=False):
    import hermes_cli.lifecycle as lifecycle
    from agent import turn_finalizer as tf

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        seen[name] = kwargs
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    call = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 2, "latency_s": 0.1}
    tf.emit_session_end(
        agent, turn_id="t1", effective_task_id=None, completed=True, failed=False,
        interrupted=False, turn_exit_reason=None, original_user_message="hi",
        final_response="ok", turn_calls=[call], provisional=provisional,
    )
    return seen


def _agent(**over):
    base = dict(session_id="s", model=SOL_ID, platform="discord", provider="cpa",
                reasoning_config=None, _blackbox_compaction={}, context_compressor=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_finalizer_hands_the_slot_to_the_hook(monkeypatch):
    seen = _hook_kwargs(monkeypatch, _agent(_model_slot="sol"))
    assert seen["on_session_end"]["model"] == SOL_ID
    assert seen["on_session_end"]["model_slot"] == "sol"
    seen = _hook_kwargs(monkeypatch, _agent())
    assert seen["on_session_end"]["model_slot"] == ""
    seen = _hook_kwargs(monkeypatch, _agent(_model_slot="sol", _current_turn_id="t1"), provisional=True)
    assert seen["on_turn_abandoned"]["model_slot"] == "sol"


def test_old_rows_keep_a_null_slot_after_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.blackbox import store

    db = tmp_path / "blackbox" / "turns.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE turns (turn_id TEXT PRIMARY KEY, ts_start REAL, ts_end REAL, provider TEXT, "
        "model TEXT, platform TEXT, chat_id TEXT, input_tokens INT, output_tokens INT, "
        "cost_usd REAL, cost_status TEXT)")
    now = time.time()
    conn.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("turn_old", now - 5, now, "cpa", "sol", "discord", "7", 10, 5, None, "unknown"))
    conn.commit()
    conn.close()

    c = store._connect()
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(turns)").fetchall()}
        old = c.execute("SELECT model, model_slot FROM turns WHERE turn_id='turn_old'").fetchone()
    finally:
        c.close()
    assert "model_slot" in cols
    assert tuple(old) == ("sol", None)
    store._connect().close()  # idempotent re-open


def test_signaled_worker_turn_records_the_slot(bb, monkeypatch):
    # A SIGTERM'd kanban worker (a slot-seated daedalus card) writes its row via cli_shutdown.
    import cli as cli_mod
    from hermes_cli import lifecycle
    from plugins.blackbox import store

    monkeypatch.setattr(cli_mod, "_handed_off_session_ids", set())
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            bb._on_session_end(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    agent = SimpleNamespace(
        session_id="sig", _current_turn_id="sig:t1", _current_task_id="", _current_api_request_id="",
        model=SOL_ID, provider="cpa", platform="cli", _model_slot="sol",
        interrupt=lambda *_a, **_k: None,
    )
    cli_mod._finalize_signaled_kanban_worker(SimpleNamespace(agent=agent, session_id="sig"), 15)
    row = store.get_turn("sig:t1")
    assert (row["model"], row["model_slot"], row["terminal_error"]) == (SOL_ID, "sol", "signal_15")


# ── fallback rungs (Apollo ruling 2026-11-20 03:40 on #483) ───────────────────
# model_slot names the seat that SERVED the turn, the same seat ``model`` describes. A turn that
# ends on a fallback rung records the rung's slot when the rung was named by a slot, '' when the
# rung was a literal id, and never the primary's slot.

OPUS_ID = "claude-opus-5-5"


def _fb_agent(primary, chain):
    from unittest.mock import MagicMock, patch

    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(model=primary, api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        fallback_model=chain)
        agent.client = MagicMock()
    return agent


def _fail_over(agent):
    from unittest.mock import MagicMock, patch

    client = MagicMock()
    client.base_url = "http://127.0.0.1:8317/v1"
    client.api_key = "fb-key"
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client",
              side_effect=lambda p, model=None, **kw: (client, model)),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        assert agent._try_activate_fallback() is True


def _record_via_finalizer(bb, monkeypatch, agent, turn_id):
    import hermes_cli.lifecycle as lifecycle
    from agent import turn_finalizer as tf
    from plugins.blackbox import store

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            bb._on_session_end(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    call = dict(USAGE_CALL, reasoning_tokens=0, latency_s=0.1,
                total_tokens=USAGE_CALL["input_tokens"] + USAGE_CALL["output_tokens"])
    agent._current_turn_id = turn_id
    tf.emit_session_end(
        agent, turn_id=turn_id, effective_task_id=None, completed=True, failed=False,
        interrupted=False, turn_exit_reason=None, original_user_message="hi",
        final_response="ok", turn_calls=[call],
    )
    return store.get_turn(turn_id)


def _rung_price(model_id, provider):
    from plugins.blackbox.cost import compute_turn_cost

    cost, status, _ = compute_turn_cost(model_id, provider, None, [dict(USAGE_CALL)])
    assert status == "estimated" and cost and cost > 0, (model_id, provider, status, cost)
    return cost


@pytest.mark.parametrize("rung, want_model, want_slot", [
    ({"provider": "openrouter", "model": "opus"}, OPUS_ID, "opus"),        # slot-named rung
    ({"provider": "cpa", "model": FLIP_ID}, FLIP_ID, ""),                  # literal rung
])
def test_fallback_turn_records_the_rungs_slot_never_the_primarys(bb, monkeypatch, rung, want_model, want_slot):
    _write_view(bb._root, {"sol": SOL_ID, "opus": OPUS_ID})
    agent = _fb_agent("sol", [dict(rung, base_url="http://127.0.0.1:8317/v1")])
    assert (agent.model, agent._model_slot) == (SOL_ID, "sol")
    _fail_over(agent)
    assert agent.model == want_model

    row = _record_via_finalizer(bb, monkeypatch, agent, f"turn_fb_{want_slot or 'literal'}")
    assert (row["model"], row["model_slot"]) == (want_model, want_slot)
    assert row["provider"] == rung["provider"]
    assert row["cost_usd"] == pytest.approx(_rung_price(want_model, rung["provider"]))
    # The primary's slot is unchanged: the next restored turn re-resolves sol.
    assert agent._model_slot == "sol"


# ── Prism round 2 on #483 ─────────────────────────────────────────────────────

def _reprice_all():
    from plugins.blackbox import store
    from plugins.blackbox.cost import compute_turn_cost

    def fn(m, p, t):
        return compute_turn_cost(m, p, None, [dict(
            input_tokens=t["input_tokens"], output_tokens=t["output_tokens"],
            cache_read_tokens=t["cache_read_tokens"], cache_write_tokens=t["cache_write_tokens"])])

    return store.reprice_unpriced(fn, apply=True)


def test_new_unresolved_slot_row_stays_unpriced_on_a_later_reprice(bb):
    # Prism 80c955779244: a turn recorded while the view could not resolve ``sol`` holds the slot
    # name in ``model``. Once the view is back, a reprice must not price it as today's target: the
    # turn's id was never observed. A pre-column row (model_slot NULL) keeps the legacy heal.
    from plugins.blackbox import store

    (bb._root / "config" / "model-slots.json").unlink()
    for turn_id, model in (("turn_unres_bare", "sol"), ("turn_unres_lane", "cpa/sol")):
        row = _end(bb, turn_id, model, model_slot="sol")
        assert (row["model"], row["model_slot"], row["cost_usd"]) == (model, "sol", None)
    legacy = _end(bb, "turn_legacy", "sol")
    con = sqlite3.connect(str(store._db_path()))
    con.execute("UPDATE turns SET model_slot=NULL WHERE turn_id='turn_legacy'")
    con.commit()
    con.close()

    _write_view(bb._root, {"sol": FLIP_ID})
    _reprice_all()
    assert store.get_turn("turn_unres_bare")["cost_usd"] is None
    assert store.get_turn("turn_unres_lane")["cost_usd"] is None
    assert legacy["cost_usd"] is None
    assert store.get_turn("turn_legacy")["cost_usd"] == pytest.approx(_priced(FLIP_ID))


def test_first_turn_after_a_startup_fallback_keeps_the_rungs_slot(bb, monkeypatch):
    # Prism be9aa8be8d26: the primary has no credentials at init, so the agent starts on a
    # slot-named rung. The init snapshot holds the rung's route; the first turn restores it and
    # clears _fallback_activated. The row must still name the rung's slot, not the primary's ('').
    from unittest.mock import MagicMock, patch

    from agent import fallback_policy as fp
    from agent.model_slots import served_slot
    from run_agent import AIAgent

    _write_view(bb._root, {"sol": SOL_ID})
    client = MagicMock()
    client.base_url = "http://127.0.0.1:8317/v1"
    client.api_key = "fb-key"

    def _resolve(provider, model=None, **kw):
        return (client, model) if provider == "custom" else (None, None)

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
        patch("hermes_cli.fallback_config.resolve_entry_api_key", lambda entry: None),
    ):
        agent = AIAgent(model="qwen3.6-plus", provider="alibaba-coding-plan",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        fallback_model=[{"provider": "custom", "model": "sol",
                                         "base_url": "http://127.0.0.1:8317/v1"}])
    agent.client = MagicMock()
    assert agent._fallback_activated and agent.provider == "custom"
    assert served_slot(agent) == "sol"

    monkeypatch.setattr("agent.fallback_wiring.restore_allowed",
                        lambda *a, **k: fp.Decision(True, None, "test"))
    monkeypatch.setattr("agent.agent_runtime_helpers._rebuild_primary_client", lambda *a, **k: None)
    agent._restore_primary_runtime()
    assert agent._fallback_activated is False and agent.provider == "custom"
    assert served_slot(agent) == "sol"


# ── t_a8fd1cfa: Prism keys from #483 ──────────────────────────────────────────

def test_new_row_with_empty_slot_is_not_read_through_todays_view_on_reprice(bb):
    # Prism c-78110dec20658413: the view is missing, a writer hands 'sol' with no model_slot
    # kwarg, so the row stores ('sol', ''). When the view comes back as sol -> gpt-5.5, a reprice
    # must not price it at gpt-5.5: no gpt-5.5 id was ever observed for that turn. Only pre-column
    # rows (model_slot NULL) may be read through today's view.
    from plugins.blackbox import store

    (bb._root / "config" / "model-slots.json").unlink()
    row = _end(bb, "turn_empty_slot", "sol")
    assert (row["model"], row["model_slot"], row["cost_usd"]) == ("sol", "", None)
    literal = _end(bb, "turn_literal_unpriced", FLIP_ID, model_slot="")
    con = sqlite3.connect(str(store._db_path()))
    con.execute("UPDATE turns SET cost_usd=NULL, cost_status=NULL, cost_uncached_usd=NULL, "
                "cost_cache_read_usd=NULL, cost_cache_write_usd=NULL, cost_output_usd=NULL "
                "WHERE turn_id='turn_literal_unpriced'")
    con.commit()
    con.close()
    assert literal["model_slot"] == ""

    _write_view(bb._root, {"sol": FLIP_ID})
    _reprice_all()
    assert store.get_turn("turn_empty_slot")["cost_usd"] is None
    # A literal-id row with an empty slot still reprices by its own id.
    assert store.get_turn("turn_literal_unpriced")["cost_usd"] == pytest.approx(_priced(FLIP_ID))


def test_cron_job_pinned_to_a_slot_records_the_slot(bb, monkeypatch):
    # Prism c-724441a3cc2b4984: cron resolved the slot and handed AIAgent only the id, so every
    # slot-pinned cron turn was recorded (id, ''). The id used for routing stays the resolved one.
    import cron.scheduler as sched
    from agent.model_slots import served_slot

    jc = sched._load_cron_job_config({"model": "sol"}, "j1", "job")
    assert (jc.model, jc.model_slot) == (SOL_ID, "sol")
    assert sched._load_cron_job_config({"model": SOL_ID}, "j1", "job").model_slot == ""

    for name, value in (("_load_prefill_messages", None), ("_guard_job_credential_exfil", None),
                        ("_preflight_or_block", None), ("_resolve_job_reasoning_config", None),
                        ("_job_fallback_chain", None), ("_load_credential_pool", None),
                        ("_init_cron_mcp_tools", None), ("_cron_preflight_enabled", False)):
        monkeypatch.setattr(sched, name, lambda *a, _v=value, **k: _v)
    monkeypatch.setattr(sched, "_resolve_job_runtime", lambda job, jid, c: ({"provider": "cpa"}, c.model))
    setup = sched._resolve_cron_agent_setup({"model": "sol"}, "j1", "job", jc)
    assert (setup.model, setup.model_slot) == (SOL_ID, "sol")

    seen = {}

    class _Agent:
        def __init__(self, **kw):
            seen.update(kw)
            self.model, self.provider = kw["model"], kw["provider"]
            self._model_slot, self._fallback_activated = None, False
            self._primary_runtime = {"model": kw["model"], "model_slot": None}

    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda *a, **k: None)
    monkeypatch.setattr(sched, "_resolve_cron_disabled_toolsets", lambda *a, **k: None)
    agent = sched._construct_cron_agent(_Agent, {}, {}, setup, workdir=None, session_id="s",
                                        session_db=None)
    assert seen["model"] == SOL_ID  # routing still gets the resolved id
    assert served_slot(agent) == "sol" and agent._primary_runtime["model_slot"] == "sol"
    row = _end(bb, "turn_cron", agent.model, model_slot=served_slot(agent))
    assert (row["model"], row["model_slot"]) == (SOL_ID, "sol")

    # A resolve-time fallback rung replaced the model: the slot no longer describes it.
    monkeypatch.setattr(sched, "_resolve_job_runtime", lambda job, jid, c: ({"provider": "x"}, "other-id"))
    assert sched._resolve_cron_agent_setup({"model": "sol"}, "j1", "job", jc).model_slot == ""
