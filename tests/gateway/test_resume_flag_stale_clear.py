"""Regression tests for stale ``resume_pending`` maintenance cleanup."""

import inspect
import logging
from datetime import datetime, timedelta

import pytest

import gateway.run as run_mod
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore
from hermes_cli.config import DEFAULT_CONFIG


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
    )


def _real_store(tmp_path, monkeypatch) -> SessionStore:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="none"),
    )
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _mark_pending(store: SessionStore, source: SessionSource, age: timedelta) -> str:
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key, "restart_interrupted")
    with store._lock:
        store._entries[entry.session_key].last_resume_marked_at = datetime.now() - age
    store.flush()
    return entry.session_key


@pytest.mark.asyncio
async def test_maintenance_clears_only_stale_unsuspended_flags_and_persists(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
    monkeypatch.delenv("HERMES_RESUME_FLAG_STALE_CLEAR", raising=False)
    store = _real_store(tmp_path, monkeypatch)

    stale_key = _mark_pending(store, _source("stale"), timedelta(days=2))
    fresh_key = _mark_pending(store, _source("fresh"), timedelta(hours=1))
    suspended_key = _mark_pending(store, _source("suspended"), timedelta(days=2))
    assert store.suspend_session(suspended_key)

    runner = object.__new__(run_mod.GatewayRunner)
    runner.session_store = store

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        cleared = await runner._clear_stale_resume_pending_flags()

    assert cleared == 1
    assert store._entries[stale_key].resume_pending is False
    assert store._entries[fresh_key].resume_pending is True
    assert store._entries[suspended_key].resume_pending is True
    assert stale_key in caplog.text
    assert "age" in caplog.text

    if store._db is not None:
        store._db.close()
    reloaded = _real_store(tmp_path, monkeypatch)
    entries = {entry.session_key: entry for entry in reloaded.snapshot_entries()}
    assert entries[stale_key].resume_pending is False
    assert entries[fresh_key].resume_pending is True
    assert entries[suspended_key].resume_pending is True
    if reloaded._db is not None:
        reloaded._db.close()


@pytest.mark.asyncio
async def test_maintenance_off_switch_preserves_stale_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
    monkeypatch.setenv("HERMES_RESUME_FLAG_STALE_CLEAR", "false")
    store = _real_store(tmp_path, monkeypatch)
    stale_key = _mark_pending(store, _source("disabled"), timedelta(days=2))

    runner = object.__new__(run_mod.GatewayRunner)
    runner.session_store = store

    assert await runner._clear_stale_resume_pending_flags() == 0
    assert store._entries[stale_key].resume_pending is True
    if store._db is not None:
        store._db.close()


def test_resume_flag_stale_clear_config_defaults_on_and_bridges(
    tmp_path, monkeypatch
):
    from hermes_cli.config import load_config

    assert DEFAULT_CONFIG["agent"]["resume_flag_stale_clear"] is True

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "agent:\n  resume_flag_stale_clear: false\n",
        encoding="utf-8",
    )
    loaded = load_config()
    assert loaded["agent"]["resume_flag_stale_clear"] is False

    monkeypatch.setenv("HERMES_RESUME_FLAG_STALE_CLEAR", "true")
    run_mod._bridge_agent_config_to_env(loaded["agent"])

    assert run_mod._resume_flag_stale_clear_enabled() is False


def test_resume_flag_stale_ttl_is_day_or_six_freshness_windows(monkeypatch):
    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
    assert run_mod._resume_flag_stale_ttl_secs() == 24 * 60 * 60

    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "20000")
    assert run_mod._resume_flag_stale_ttl_secs() == 6 * 20000


def test_hourly_watcher_wires_stale_resume_cleanup():
    source = inspect.getsource(run_mod.GatewayRunner._session_expiry_watcher)
    assert "await self._clear_stale_resume_pending_flags()" in source
