"""A shutdown mark pass writes sessions.json ONCE, not once per running session (t_edfc146c).

Each per-session ``_save()`` rewrites the whole routing index; on the Studio (1.5 MB, 10 running
sessions) the two passes cost ~17 s of the 50 s stop budget and the shutdown watchdog fired.
"""
import asyncio
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner

N_SESSIONS = 12


def _runner_with_sessions(tmp_path, monkeypatch, n=N_SESSIONS):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    runner.session_store = store
    keys = []
    for i in range(n):
        entry = store.get_or_create_session(
            SessionSource(platform=Platform.TELEGRAM, chat_id=str(100 + i), user_id="u1"))
        keys.append(entry.session_key)
        runner._running_agents[entry.session_key] = object()
    saves = []
    real_save = store._save
    monkeypatch.setattr(store, "_save", lambda *a, **k: (saves.append(1), real_save(*a, **k))[1])
    return runner, store, keys, saves


@pytest.mark.parametrize("interrupted", [False, True], ids=["pre_drain", "post_drain"])
def test_one_save_per_pass(tmp_path, monkeypatch, caplog, interrupted):
    from gateway.run import GatewayRunner

    runner, store, keys, saves = _runner_with_sessions(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        marked = asyncio.run(GatewayRunner._mark_running_sessions_resume_pending(
            runner, "pre-drain mark_resume_pending", interrupted=interrupted))

    assert sorted(marked) == sorted(keys)
    assert len(saves) == 1, f"expected ONE sessions.json save for the pass, got {len(saves)}"
    # Every mark is in memory AND durable: a fresh store reading the same dir sees all of them.
    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    reloaded._ensure_loaded()
    assert all(reloaded._entries[k].resume_pending for k in keys)
    # Per-session PHASE lines are kept; the pass logs its elapsed time.
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("PHASE=shutdown_mark" in m for m in msgs) == N_SESSIONS
    assert any(m.startswith("Shutdown phase: pre-drain mark_resume_pending marked 12 session(s) in ")
               for m in msgs), msgs


def test_failed_batched_save_does_not_abort_the_pass(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    runner, store, keys, _saves = _runner_with_sessions(tmp_path, monkeypatch, n=3)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", _boom)
    marked = asyncio.run(GatewayRunner._mark_running_sessions_resume_pending(runner, "mark"))
    assert sorted(marked) == sorted(keys)
    assert all(store._entries[k].resume_pending for k in keys)
    assert getattr(store, "_resume_mark_batch_depth", 0) == 0


def test_mark_outside_a_batch_still_saves_immediately(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    entry = store.get_or_create_session(
        SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="u1"))
    calls = []
    real = store._save
    store._save = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    assert store.mark_resume_pending(entry.session_key, "restart_timeout") is True
    assert len(calls) == 1
