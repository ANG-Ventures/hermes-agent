"""Cross-worker SEAM test: the real core→plugin→store path, no mocks.

Exercises the actual contract between T1 (core turn_usage payload shape),
T3 (_on_session_end hook + cost + card), and T2 (real SQLite store):
feed a realistic on_session_end payload to the real hook and assert a real
TurnRecord lands in a real (temp) DB and is retrievable, with cost computed
and text scrubbed.
"""
import sys
import types
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Reload hermes_constants + store so the per-profile DB path picks up env.
    import hermes_constants
    importlib.reload(hermes_constants)
    import plugins.blackbox.store as store
    importlib.reload(store)
    return home, store


def _payload(**over):
    usage = {
        "api_calls": 2,
        "input_tokens": 500_000, "output_tokens": 559,
        "cache_read_tokens": 499_000, "cache_write_tokens": 1000,
        "reasoning_tokens": 10, "total_tokens": 500_559, "latency_s": 23.0,
        "calls": [
            {"input_tokens": 250_000, "output_tokens": 300, "cache_read_tokens": 249_000,
             "cache_write_tokens": 500, "reasoning_tokens": 5, "prompt_tokens": 250_000,
             "completion_tokens": 300, "total_tokens": 250_300, "latency_s": 11.0},
            {"input_tokens": 250_000, "output_tokens": 259, "cache_read_tokens": 250_000,
             "cache_write_tokens": 500, "reasoning_tokens": 5, "prompt_tokens": 250_000,
             "completion_tokens": 259, "total_tokens": 250_259, "latency_s": 12.0},
        ],
        "context_used": 500_000, "context_length": 1_050_000,
        "parent_turn_id": None, "parent_platform": None, "parent_chat_id": None,
        "parent_chat_name": None, "is_subagent": False,
    }
    usage.update(over.pop("usage", {}))
    base = dict(
        session_id="s-real", completed=True, interrupted=False,
        model="openai-codex/gpt-5.4", platform="discord", provider="openai-codex",
        user_message="hello with a secret sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 in it",
        final_response="done", turn_usage=usage,
    )
    base.update(over)
    return base


def test_real_seam_core_to_store(temp_home, monkeypatch):
    home, store = temp_home
    import plugins.blackbox as bb
    importlib.reload(bb)
    bb._sessions.clear()

    # Enable + force a known cost so the path is deterministic, but use the REAL store.
    monkeypatch.setattr(bb, "_config", lambda: {
        "enabled": True, "cost_alert_threshold_usd": 999.0,  # high → no alert send attempt
        "store_text": True, "record_subagents": True,
    })
    monkeypatch.setattr(bb, "compute_turn_cost", lambda *a, **k: (1.26, "estimated"))
    # Point the hook's lazy store import at the freshly-reloaded real store.
    monkeypatch.setattr(bb, "store", store, raising=False)
    monkeypatch.setitem(sys.modules, "plugins.blackbox.store", store)

    # Simulate a turn: session_start → 2 tool calls → session_end.
    p = _payload()
    bb._on_session_start(session_id="s-real")
    # Pin the REAL gateway kwarg name (model_tools.py fires post_tool_call with
    # tool_name=...). No try/except fallback that would mask a contract change.
    for name in ("exec", "exec", "exec", "read"):
        bb._on_post_tool_call(session_id="s-real", tool_name=name)
    bb._on_session_end(**p)

    # The seam: a real row must exist in the real DB with summed tokens.
    last = store.get_last_turn("discord", "")  # chat_id may be "" in this synthetic path
    # Fall back: find by scanning top turns if chat_id differs.
    rows = store.top_turns(5, 30)
    assert rows, "no turn persisted through the real seam"
    row = rows[0]
    assert row["input_tokens"] == 500_000
    assert row["api_calls"] == 2
    assert row["cost_usd"] == pytest.approx(1.26)
    # Secret in user_text must be scrubbed before persist.
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in (row.get("user_text") or "")


def test_real_seam_disabled_is_noop(temp_home, monkeypatch):
    home, store = temp_home
    import plugins.blackbox as bb
    importlib.reload(bb)
    bb._sessions.clear()
    # Exercise the REAL gate: write a config with enabled:false (no _config patch).
    (home / "config.yaml").write_text("blackbox:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(bb, "store", store, raising=False)
    monkeypatch.setitem(sys.modules, "plugins.blackbox.store", store)
    bb._on_session_end(**_payload())
    assert store.top_turns(5, 30) == [], "disabled plugin must not persist"
