"""Exit-race orphans: a review fork whose turn outruns CLI cleanup's snapshot.

blackbox-orphan-guard 2026-10-02 (window since 10-01 07:43) named 8 orphans.
Seven are one-shot CLI workers whose post-turn background review raced the
exit (logs: ``agent.turn_context: conversation turn ... msg='Review the
conversation above'`` within ~0.1 s of ``CLI cleanup calling memory shutdown``):

* daedalus-fable 09:54:51.345 cleanup, 09:54:51.388 the fork's turn starts:
  the fork was not yet live when ``_record_abandoned_review_turns`` ran, so
  nothing recorded it, and its one 409 call landed a second later.
* daedalus/daedalus-opus 22:56, 23:01, 23:16, 00:19: cleanup's snapshot ran
  ~50 ms before the admitted fork bound ``_current_turn_id``, so it was
  skipped; its first call landed 8-17 s later during interpreter shutdown
  (``cannot schedule new futures after interpreter shutdown``).

The CLI exit must fence review forks: no fork turn may start after the
snapshot, and an admitted fork is waited for (bounded) until its turn id binds
so its row is written; later calls refresh that row.
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

    from hermes_cli import lifecycle

    def invoke_hook(name, **kwargs):
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
        platform="cli", provider="claude-alr", context_compressor=None,
        _blackbox_turn_calls=(turn_id, []), _session_messages=None,
        _active_children=[], _active_children_lock=threading.Lock(),
    )


def _call(turn_id, seq=0, status=200):
    blackbox.record_api_call(
        turn_id=turn_id, seq=seq, ts=100.0 + seq, provider="claude-alr",
        model="claude-opus-5-5",
        usage=CanonicalUsage(input_tokens=4, output_tokens=243),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=status, relay_synthetic=False, route_id=None,
    )


def _orphan_ids(db):
    with sqlite3.connect(db) as conn:
        return sorted(t for t, _ts, _e in orphans.orphan_turns(conn, since=0, settle_before=1e12))


def _row(db, turn_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT interrupted, output_tokens FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()


def _cleanup(monkeypatch):
    monkeypatch.setattr(cli_mod, "_cleanup_done", False)
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
    monkeypatch.setattr(cli_mod, "_arm_exit_watchdog", lambda *a, **k: None)
    cli_mod._run_cleanup(notify_session_finalize=False)


OPUS = "20261001_225020_7f28e2:038d4b16-ff16-4f38-8c82-22e928bd8edb:8e8e28b4"


def test_admitted_fork_that_binds_its_turn_after_cleanup_starts_is_recorded(ledger, monkeypatch):
    """daedalus-opus 23:16:12: cleanup ran ~50 ms before the fork bound its turn id.

    The pre-fix snapshot saw ``_current_turn_id=None`` and skipped the fork; its
    one call then landed 17 s later with no turns row.
    """
    fork = _fork(OPUS)
    fork._current_turn_id = None
    br._live_review_agents[id(fork)] = fork
    assert br.background_review_admitted(fork) is True

    def _bind_late():
        import time
        time.sleep(0.2)
        fork._current_turn_id = OPUS

    threading.Thread(target=_bind_late, daemon=True).start()
    _cleanup(monkeypatch)
    _call(OPUS)  # lands after the exit snapshot, as in production

    assert _orphan_ids(ledger) == []
    interrupted, out_tok = _row(ledger, OPUS)
    assert interrupted == 1
    assert out_tok == 243  # the late call refreshed the provisional row


def test_registered_fork_not_yet_admitted_is_refused_after_cleanup(ledger, monkeypatch):
    """daedalus-fable 09:54:51: cleanup at .345, the fork's turn began at .388."""
    fork = _fork(OPUS)
    fork._current_turn_id = None
    br._live_review_agents[id(fork)] = fork

    _cleanup(monkeypatch)

    assert br.background_review_admitted(fork) is False


def test_cli_exit_fences_a_review_turn_that_has_not_started(ledger, monkeypatch):
    """daedalus-fable 09:54: the fork's turn began 43 ms after cleanup ran."""
    _cleanup(monkeypatch)

    assert br.host_exit_reason() == "cli_exit"
    assert br.background_review_admitted(SimpleNamespace()) is False


def test_spawn_is_refused_after_the_exit_fence(ledger, monkeypatch):
    calls = []
    agent = SimpleNamespace(_background_review_lock=threading.Lock(),
                            _background_review_run=None)
    monkeypatch.setattr(br, "_host_exit_reason", "cli_exit")
    assert br.prepare_background_review_run(agent) is None
    assert calls == []


def test_no_fence_without_exit(ledger):
    assert br.host_exit_reason() is None
    assert br.background_review_admitted(SimpleNamespace()) is True
