"""Slot aliases P2 (t_df66be3d): a seat configured with a slot name (``sol``) runs on the
slot's current id for every lookup and on the wire, and follows a re-rendered view per turn.

Measured on the Studio runtime before this change: get_model_context_length("sol", provider="cpa")
returned the 256,000 unknown-model fallback and no price, while "gpt-6.2-sol" returned 1,050,000
and $2/$10. A slot-configured seat compressed at a quarter of its window and booked $0.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from hermes_cli.model_registry import slot_resolve

SOL_ID = "gpt-6.2-sol"
NEXT_SOL_ID = "gpt-6.3-sol"


def _write_view(root, slots, *, flat=False):
    body = dict(slots) if flat else {"_generated": "test", "rendered_from": "abc123", "slots": dict(slots)}
    path = root / "config" / "model-slots.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    # Same-size rewrites inside one mtime tick must still be seen: bump the mtime explicitly.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_view(root, {"sol": SOL_ID, "opus": "claude-opus-5-5"})
    return root


# ── resolver ──────────────────────────────────────────────────────────────────

def test_slot_resolves_to_id(home):
    assert slot_resolve("sol") == (SOL_ID, "sol")


def test_lane_prefix_kept(home):
    assert slot_resolve("cpa/sol") == (f"cpa/{SOL_ID}", "sol")


def test_flat_view_shape_accepted(home):
    _write_view(home, {"sol": SOL_ID, "rendered_from": "abc"}, flat=True)
    assert slot_resolve("sol") == (SOL_ID, "sol")
    assert slot_resolve("rendered_from") == ("rendered_from", None)


def test_literal_id_untouched(home):
    assert slot_resolve(SOL_ID) == (SOL_ID, None)
    assert slot_resolve("claude-fable-5-2") == ("claude-fable-5-2", None)


def test_missing_view_passes_through(home):
    (home / "config" / "model-slots.json").unlink()
    assert slot_resolve("sol") == ("sol", None)


def test_unreadable_view_passes_through(home):
    (home / "config" / "model-slots.json").write_text("{not json", encoding="utf-8")
    assert slot_resolve("sol") == ("sol", None)


def test_rerendered_view_seen_without_reload(home):
    assert slot_resolve("sol")[0] == SOL_ID
    _write_view(home, {"sol": NEXT_SOL_ID, "opus": "claude-opus-5-5"})
    assert slot_resolve("sol")[0] == NEXT_SOL_ID


# ── the agent: context length, price and wire id run on the resolved id ─────────

def _make_agent(model, provider="cpa"):
    cfg = {"model": {"default": model, "provider": provider}}
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        return AIAgent(
            model=model, provider=provider, api_key="fake-test-token",
            base_url="http://127.0.0.1:8317/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )


def test_slot_seat_gets_the_ids_context_and_price(home):
    from agent.model_metadata import get_model_context_length
    from agent.usage_pricing import get_pricing_entry

    literal = _make_agent(SOL_ID)
    seat = _make_agent("sol")

    assert seat.model == SOL_ID
    assert seat._model_slot == "sol"
    assert literal._model_slot is None
    # The bug class: the slot name alone falls to the unknown-model fallback.
    want_ctx = get_model_context_length(SOL_ID, provider="cpa")
    assert get_model_context_length("sol", provider="cpa") != want_ctx
    assert seat.context_compressor.context_length == literal.context_compressor.context_length == want_ctx
    assert get_pricing_entry(seat.model, provider="cpa") is not None
    assert get_pricing_entry(seat.model, provider="cpa") == get_pricing_entry(SOL_ID, provider="cpa")


def test_wire_carries_the_resolved_id(home):
    seat = _make_agent("sol")
    kwargs = seat._build_api_kwargs([{"role": "user", "content": "hi"}])
    assert kwargs["model"] == SOL_ID


def test_flip_moves_the_next_turn_without_reload(home):
    seat = _make_agent("sol")
    from agent.model_slots import model_label, refresh_slot_for_turn

    assert refresh_slot_for_turn(seat) is False  # unchanged view: no switch
    _write_view(home, {"sol": NEXT_SOL_ID})
    assert refresh_slot_for_turn(seat) is True
    assert seat.model == NEXT_SOL_ID
    assert seat._model_slot == "sol"
    assert seat._primary_runtime["model"] == NEXT_SOL_ID
    assert seat._build_api_kwargs([{"role": "user", "content": "hi"}])["model"] == NEXT_SOL_ID
    assert model_label(seat) == f"sol -> {NEXT_SOL_ID}"


def test_flip_waits_while_on_fallback(home):
    seat = _make_agent("sol")
    from agent.model_slots import refresh_slot_for_turn

    _write_view(home, {"sol": NEXT_SOL_ID})
    seat._fallback_activated = True
    assert refresh_slot_for_turn(seat) is False
    assert seat.model == SOL_ID


def test_view_gone_mid_session_keeps_the_id_in_use(home):
    seat = _make_agent("sol")
    from agent.model_slots import refresh_slot_for_turn

    (home / "config" / "model-slots.json").unlink()
    assert refresh_slot_for_turn(seat) is False
    assert seat.model == SOL_ID  # never a bare slot name on the wire


def test_missing_view_agent_passes_through(home):
    (home / "config" / "model-slots.json").unlink()
    seat = _make_agent("sol")
    assert seat.model == "sol"
    assert seat._model_slot is None


def test_literal_seat_untouched_by_refresh(home):
    agent = _make_agent(SOL_ID)
    from agent.model_slots import model_label, refresh_slot_for_turn

    _write_view(home, {"sol": NEXT_SOL_ID})
    assert refresh_slot_for_turn(agent) is False
    assert agent.model == SOL_ID
    assert model_label(agent) == SOL_ID


def test_switch_model_to_slot_keeps_slot(home):
    agent = _make_agent("claude-opus-5-5", provider="cpa")
    agent.switch_model("sol", "cpa", api_key="fake-test-token", base_url="http://127.0.0.1:8317/v1")
    assert agent.model == SOL_ID
    assert agent._model_slot == "sol"
    agent.switch_model(SOL_ID, "cpa", api_key="fake-test-token", base_url="http://127.0.0.1:8317/v1")
    assert agent._model_slot is None


# ── status line ───────────────────────────────────────────────────────────────

def _status_snapshot(agent_ns):
    from datetime import datetime
    from cli import HermesCLI

    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "sol"
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj.agent = agent_ns
    return cli_obj._get_status_bar_snapshot()


def _status_agent(**kw):
    from types import SimpleNamespace

    base = dict(
        model=SOL_ID, provider="cpa", base_url="", session_input_tokens=0, session_output_tokens=0,
        session_cache_read_tokens=0, session_cache_write_tokens=0, session_prompt_tokens=0,
        session_completion_tokens=0, session_total_tokens=0, session_api_calls=0,
        get_rate_limit_state=lambda: None,
        context_compressor=SimpleNamespace(last_prompt_tokens=0, context_length=1_050_000, compression_count=0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_line_shows_slot_and_id():
    assert _status_snapshot(_status_agent(_model_slot="sol"))["model_short"] == f"sol -> {SOL_ID}"


def test_status_line_literal_and_fallback_show_the_id_only():
    assert _status_snapshot(_status_agent())["model_short"] == SOL_ID
    snap = _status_snapshot(_status_agent(model="claude-opus-5-5", _model_slot="sol", _fallback_activated=True))
    assert snap["model_short"] == "claude-opus-5-5"
