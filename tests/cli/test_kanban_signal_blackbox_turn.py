"""SIGTERM'd kanban workers must leave a blackbox ``turns`` row (no orphan calls).

The kanban-worker signal path ends in ``os._exit(0)``, which skips the
``on_session_end`` emit the KeyboardInterrupt path does. Blackbox writes the
``turns`` row only from ``on_session_end``, so every externally-killed worker
left ``turn_api_calls`` rows with no parent turn (blackbox-orphan-guard,
2026-09-28: 45 of 50 orphans; t_3d0fa352).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import cli as cli_mod
from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import orphans, store


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650,
    })
    monkeypatch.setattr(cli_mod, "_handed_off_session_ids", set())
    store._connect().close()
    return store._db_path()


def _worker_cli(session_id, turn_id):
    agent = SimpleNamespace(
        session_id=session_id, _current_turn_id=turn_id, _current_task_id="",
        _current_api_request_id="", model="claude-opus-5-5", platform="cli",
        interrupt=lambda *_a, **_k: None,
    )
    return SimpleNamespace(agent=agent, session_id=session_id)


def _in_flight_call(turn_id):
    blackbox._on_session_start(session_id=turn_id.split(":")[0])
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100.0, provider="claude-bpr", model="claude-opus-5-5",
        usage=CanonicalUsage(input_tokens=10, output_tokens=5),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=200, relay_synthetic=False, route_id=None,
    )


def _orphans(db):
    import sqlite3
    with sqlite3.connect(db) as conn:
        return orphans.orphan_turns(conn, since=0, settle_before=1e12)


def _route_session_end_to_blackbox(monkeypatch):
    from hermes_cli import lifecycle

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            blackbox._on_session_end(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)


def test_signaled_worker_turn_gets_a_turns_row(ledger, monkeypatch):
    _route_session_end_to_blackbox(monkeypatch)
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)
    turn_id = "20260927_184011_0a4ea3:d794eb73:81a06ab9"
    _in_flight_call(turn_id)
    assert _orphans(ledger), "precondition: orphan before finalize"

    cli_mod._finalize_signaled_kanban_worker(_worker_cli(turn_id.split(":")[0], turn_id), 15)

    assert _orphans(ledger) == []
    import sqlite3
    with sqlite3.connect(ledger) as conn:
        # Flagged as a signal kill (t_50422844), and priced from the turn's own
        # ledger rows rather than recorded as a 0-token turn.
        assert conn.execute(
            "SELECT interrupted, terminal_error, api_calls, input_tokens, output_tokens "
            "FROM turns WHERE turn_id = ?", (turn_id,),
        ).fetchone() == (1, "signal_15", 1, 10, 5)


def test_signal_after_the_turn_finalized_leaves_the_real_row_alone(ledger, monkeypatch):
    """Prism P1 on #1504: _current_turn_id is never cleared, so a SIGTERM that
    lands after the finalizer wrote the real row must not upsert an
    interrupted/signal_15 row over it."""
    _route_session_end_to_blackbox(monkeypatch)
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)
    turn_id = "20260929_160000_c0ffee:t_done:0badcafe"
    _in_flight_call(turn_id)
    blackbox._on_session_end(
        session_id=turn_id.split(":")[0], turn_id=turn_id, completed=True, failed=False,
        turn_exit_reason="text_response(stop)", model="claude-opus-5-5",
        provider="claude-bpr", final_response="all done", platform="cli",
    )
    worker = _worker_cli(turn_id.split(":")[0], turn_id)
    worker.agent._session_end_emitted_turn_id = turn_id  # the finalizer's marker

    cli_mod._finalize_signaled_kanban_worker(worker, 15)

    import sqlite3
    with sqlite3.connect(ledger) as conn:
        assert conn.execute(
            "SELECT interrupted, terminal_error, final_text FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone() == (0, None, "all done")


def test_flush_runs_before_session_end_and_failures_are_contained(monkeypatch):
    order = []
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store",
                        lambda _cli: (order.append("flush"), 1 / 0))
    monkeypatch.setattr(cli_mod, "_emit_interrupted_session_end",
                        lambda _cli, reason, terminal_error=None: order.append((reason, terminal_error)))
    cli_mod._finalize_signaled_kanban_worker(SimpleNamespace(agent=None), 15)
    assert order == ["flush", ("signal_15", "signal_15")]


def test_kanban_signal_path_calls_the_finalizer():  # noqa: source-proxy wiring of a closure nested in main() that only a real signal reaches and that ends in os._exit; the finalizer itself is exercised behaviourally above
    src = inspect.getsource(cli_mod)
    handler = src[src.index("def _signal_handler_q("):]
    handler = handler[:handler.index("os._exit(0)\n", handler.index("_lg.shutdown()"))]
    assert "_finalize_signaled_kanban_worker(cli, signum)" in handler
