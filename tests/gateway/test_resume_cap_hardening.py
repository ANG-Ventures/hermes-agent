"""Dispatch accounting and rollback-safe session counters."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.auto_resume import AutoResumeAttemptStore
from gateway.run import GatewayRunner
from tests.gateway.test_boot_resume_attempt_cap import (
    _runner, _source, _seed, _INTERRUPTED_TAIL, _remark, _boot,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "dispatch_cancel", "failure", "success"])
async def test_charge_only_after_dispatch(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, adapter, db = _runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_run_startup_resume_event",
                        GatewayRunner._run_startup_resume_event.__get__(runner))
    monkeypatch.setattr(runner, "_maybe_notify_unclean_restart", AsyncMock())
    dispatch_error = {
        "failure": RuntimeError("dispatch failed"),
        "dispatch_cancel": asyncio.CancelledError(),
    }.get(outcome)
    adapter.handle_message = AsyncMock(side_effect=dispatch_error)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    try:
        for _ in range(3 if outcome == "cancel" else 1):
            _remark(runner, entry)
            await runner._prepare_boot_resume_work_check()
            assert _boot(runner) == 1
            tasks = list(runner._background_tasks)
            if outcome == "cancel":
                for task in tasks:
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if outcome == "success":
                assert results == [None]
        assert runner._get_auto_resume_attempt_store().session_attempt_count(
            entry.session_key) == (1 if outcome == "success" else 0)
        if outcome == "cancel":
            adapter.handle_message.assert_not_called()
        else:
            adapter.handle_message.assert_awaited_once()
    finally:
        db.close()


def test_v2_survives_legacy_repair_and_warns_once(tmp_path, caplog):
    path = tmp_path / "attempts.json"
    legacy = {"version": 1, "attempts": [], "session_attempts": {
        "s": {"count": 3, "attempted_at": 1000.0}}}
    path.write_text(json.dumps(legacy))
    store = AutoResumeAttemptStore(path, now=lambda: 1000.0)
    assert store.session_resume_verdict("s", 3) == (False, 3)
    ledger = path.with_name(path.stem + ".sessions-v2.json")
    assert ledger.exists()
    assert json.loads(ledger.read_text())["version"] == 2
    saved = ledger.read_bytes()
    # Exact output shape of the shipped v1 repair writer.
    path.write_text(json.dumps({"version": 1, "attempts": [], "session_attempts": {}}))
    forward = AutoResumeAttemptStore(path, now=lambda: 1000.0)
    for _ in range(3):
        assert forward.session_attempt_count("s") == 3
    assert ledger.read_bytes() == saved
    warnings = [r.message for r in caplog.records if "trusting" in r.message]
    assert len(warnings) == 1
    assert path.name in warnings[0] and ledger.name in warnings[0]


def test_v2_counter_updates_preserve_legacy_shape(tmp_path):
    path = tmp_path / "attempts.json"
    legacy = {"version": 1, "attempts": []}
    path.write_text(json.dumps(legacy))
    store = AutoResumeAttemptStore(path)
    assert store.record_session_attempt("s") == 1
    assert json.loads(path.read_text()) == legacy


@pytest.mark.parametrize("legacy_bytes", ["{torn", '{"version": 1, "attempts": []}'])
def test_existing_v2_survives_corrupt_or_counterless_v1(tmp_path, legacy_bytes):
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path)
    assert store.record_session_attempt("s") == 1
    path.write_text(legacy_bytes)
    assert AutoResumeAttemptStore(path).session_attempt_count("s") == 1


def test_empty_migration_is_not_reimported(tmp_path):
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path, now=lambda: 1000.0)
    assert store.session_resume_verdict("s", 3) == (True, 0)
    path.write_text(json.dumps({"version": 1, "attempts": [], "session_attempts": {
        "s": {"count": 50, "attempted_at": 1000.0}}}))
    assert AutoResumeAttemptStore(path, now=lambda: 1000.0).session_attempt_count("s") == 0


def test_v2_repair_keeps_legacy_credits(tmp_path):
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path)
    assert store.consume("s", 42)
    store.session_path.write_text("{torn")
    assert store.record_session_attempt("s") == 1
    fresh = AutoResumeAttemptStore(path)
    assert fresh.has_attempt("s", 42)
    assert fresh.session_attempt_count("s") == 1


def test_concurrent_session_accounting_does_not_lose_increments(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = AutoResumeAttemptStore(tmp_path / "attempts.json")
    assert store.session_resume_verdict("s", 100) == (True, 0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: store.record_session_attempt("s"), range(24)))
    assert sorted(counts) == list(range(1, 25))
    assert AutoResumeAttemptStore(store.path).session_attempt_count("s") == 24
