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
    monkeypatch.setattr(br, "_review_exit_fence", threading.Event())
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




def _run_review_fork():
    """Run the review worker against a permissive parent; True when the fork's
    ``run_conversation`` (its first provider call) was entered."""
    from unittest.mock import MagicMock, patch

    from tests.agent.test_background_review_tool_call_guard import _fake_parent

    parent = MagicMock()
    for key, value in vars(_fake_parent(MagicMock())).items():
        setattr(parent, key, value)
    parent._active_children = []
    parent._background_review_agent = None
    with (
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.AIAgent") as mock_aiagent,
        patch("tools.terminal_tool.set_approval_callback"),
    ):
        mock_aiagent.return_value.run_conversation.return_value = {"messages": []}
        br._run_review_in_thread(parent, [{"role": "user", "content": "hi"}], "review", None)
    return mock_aiagent.return_value.run_conversation.called


def test_review_fork_runs_before_exit(ledger):
    assert _run_review_fork() is True


def test_cli_exit_fences_a_review_that_has_not_started_its_request(ledger):
    """r31 G: 7 of 8 orphans were review forks still being BUILT when cleanup
    snapshotted the registry; they then made their first provider call during
    interpreter shutdown and died with no turns row. After the exit snapshot no
    review request may be admitted."""
    assert not br.background_reviews_fenced()
    cli_mod._record_abandoned_review_turns("cli_exit")
    assert br.background_reviews_fenced()
    assert _run_review_fork() is False


def test_registered_fork_without_a_turn_id_is_interrupted_not_skipped(ledger):
    """Prism #1631 P1: a fork admitted before the fence but not yet past
    build_turn_context (no _current_turn_id) is unrecordable; the exit snapshot
    hard-interrupts it so its loop breaks before the first provider call."""
    calls = []
    fork = SimpleNamespace(
        _current_turn_id=None,
        hard_interrupt=lambda message=None, **_k: calls.append(message),
    )
    br._live_review_agents[id(fork)] = fork
    recorded = SimpleNamespace(_current_turn_id=TID)
    br._live_review_agents[id(recorded)] = recorded

    out = br.fence_background_reviews_and_snapshot()

    assert out == [recorded]
    assert calls == ["process exiting"]
    assert br.admit_background_review(None, fork) is False
