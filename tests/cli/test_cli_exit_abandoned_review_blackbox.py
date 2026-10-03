"""A CLI exiting under an in-flight background review must leave a turns row.

daedalus 2026-09-29 19:15:59: the review fork's turn ``...:a92c4fba`` started
after the one-shot worker's main turn finalized, hit a refused relay, and was
still retrying on its daemon thread when the process exited at 19:16:00. Its
call row stayed with no ``turns`` row. The gateway already records such forks
at shutdown (t_ab2f510b); the CLI exit paths did not (t_99b95957).
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

import cli as cli_mod
from agent import background_review as br
from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import orphans, store

HOOKS: list = []


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650,
    })
    monkeypatch.setattr(blackbox, "_provisional_turns", {})
    monkeypatch.setattr(cli_mod, "_handed_off_session_ids", set())
    monkeypatch.setattr(br, "_live_review_agents", {})
    monkeypatch.setattr(br, "_host_exit_reason", None)
    store._connect().close()
    HOOKS.clear()

    from hermes_cli import lifecycle

    def invoke_hook(name, **kwargs):
        HOOKS.append(name)
        if name == "on_session_end":
            blackbox._on_session_end(**kwargs)
        elif name == "on_turn_abandoned":
            blackbox._on_turn_abandoned(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    return store._db_path()


def _fork(turn_id):
    return SimpleNamespace(
        session_id=turn_id.split(":")[0], _current_turn_id=turn_id,
        _current_task_id=turn_id.split(":")[1], model="claude-opus-5-5",
        platform="cli", provider="claude-bpr", context_compressor=None,
        _blackbox_turn_calls=(turn_id, []), _session_messages=None,
        _active_children=[], _active_children_lock=threading.Lock(),
    )


def _failed_call(turn_id):
    blackbox._on_session_start(session_id=turn_id.split(":")[0])
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100.0, provider="claude-bpr", model="claude-opus-5-5",
        usage=CanonicalUsage(input_tokens=0, output_tokens=0),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=None, relay_synthetic=False, route_id=None,
    )


def _orphan_ids(db):
    with sqlite3.connect(db) as conn:
        return sorted(t for t, _ts, _e in orphans.orphan_turns(conn, since=0, settle_before=1e12))


def _interrupted(db, turn_id):
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT interrupted FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
    return row and row[0]


TID = "20260929_183543_c8520c:599d58c5-1251-4d1b-beb9-99095bc89f36:a92c4fba"


def test_cli_cleanup_records_an_in_flight_review_turn(ledger, monkeypatch):
    _failed_call(TID)
    fork = _fork(TID)
    br._live_review_agents[id(fork)] = fork
    assert _orphan_ids(ledger) == [TID], "precondition"

    monkeypatch.setattr(cli_mod, "_cleanup_done", False)
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
    monkeypatch.setattr(cli_mod, "_arm_exit_watchdog", lambda *a, **k: None)
    cli_mod._run_cleanup(notify_session_finalize=False)

    assert _orphan_ids(ledger) == []
    assert _interrupted(ledger, TID) == 1
    assert HOOKS == ["on_turn_abandoned"]


def test_signaled_worker_records_an_in_flight_review_turn(ledger, monkeypatch):
    _failed_call(TID)
    fork = _fork(TID)
    br._live_review_agents[id(fork)] = fork
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)

    cli_mod._finalize_signaled_kanban_worker(SimpleNamespace(agent=None, session_id=None), 15)

    assert _orphan_ids(ledger) == []
    assert _interrupted(ledger, TID) == 1


def test_review_turn_that_already_emitted_is_not_re_recorded(ledger, monkeypatch):
    _failed_call(TID)
    fork = _fork(TID)
    fork._session_end_emitted_turn_id = TID
    br._live_review_agents[id(fork)] = fork

    cli_mod._record_abandoned_review_turns("cli_exit")

    assert HOOKS == []
