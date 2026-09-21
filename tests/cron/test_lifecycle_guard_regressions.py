"""PR #791: exercise the operator path and real journal writer/pruner race."""
import json
import multiprocessing
from datetime import timedelta

import pytest

from cron import jobs, lifecycle_journal as journal
from hermes_cli import cron as cli


@pytest.fixture
def store(tmp_path):
    with jobs.use_cron_store(tmp_path):
        yield tmp_path


@pytest.fixture(params=["builtin", "external"])
def status_path(request, monkeypatch):
    monkeypatch.setattr(cli, "_active_cron_provider_name", lambda: request.param)
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr("gateway.status.is_gateway_runtime_lock_active", lambda: False)


@pytest.mark.parametrize("state", ["paused", "disabled", "completed"])
def test_status_does_not_call_present_inactive_jobs_vanished(store, status_path, state, capsys):
    job = jobs.create_job(prompt="x", schedule="in 10 hours", name=state)
    jobs.update_job(job["id"], {"enabled": False, "state": state})
    assert jobs.list_jobs(include_disabled=False) == []
    cli.cron_status()
    output = capsys.readouterr().out
    assert "No active jobs" in output
    assert "VANISHED" not in output
    assert "UNAVAILABLE" not in output


def test_status_reports_actual_loss(store, status_path, capsys):
    job = jobs.create_job(prompt="x", schedule="in 10 hours", name="lost")
    (store / "cron/jobs.json").write_text(json.dumps({"jobs": []}))
    cli.cron_status()
    output = capsys.readouterr().out
    assert "VANISHED" in output
    assert job["id"] in output


def test_old_loss_is_not_certified_as_zero_unaccounted(store, status_path, capsys):
    jobs.create_job(prompt="x", schedule="in 10 hours")
    path = store / "cron/lifecycle.jsonl"
    record = json.loads(path.read_text())
    record["at"] = (journal._hermes_now() - timedelta(hours=48)).isoformat()
    path.write_text(json.dumps(record) + "\n")
    (store / "cron/jobs.json").write_text(json.dumps({"jobs": []}))
    summary = journal.check_vanished_jobs().summary()
    assert "0 unaccounted" not in summary
    assert "older jobs not checked" in summary
    assert "24" in summary
    cli.cron_status()
    output = capsys.readouterr().out
    assert "older jobs not checked" in output
    assert "0 unaccounted" not in output


def test_guard_rejects_caller_filtered_snapshot(store):
    jobs.create_job(prompt="x", schedule="in 10 hours")
    with pytest.raises(TypeError):
        journal.check_vanished_jobs(jobs=[])


def test_append_and_prune_fail_closed_on_lock_timeout(store, monkeypatch):
    journal.record_created("recent")
    path = store / "cron/lifecycle.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({"event": "created", "job_id": "old",
                            "at": "2020-01-01T00:00:00+00:00"}) + "\n")
    before = path.read_bytes()
    with journal._journal_lock(path):
        # Real second file-lock acquisition fails. Advance only its deadline,
        # not the host/platform or the lock implementation.
        clock = iter([0.0, 10.0, 20.0, 30.0])
        monkeypatch.setattr(journal.time, "monotonic", lambda: next(clock))
        journal.record_created("must-not-write-unlocked")
        assert journal.prune() == 0
        assert path.read_bytes() == before
    monkeypatch.undo()
    journal.record_created("after-release")
    assert "after-release" in path.read_text()


def test_append_does_not_resurrect_deleted_profile(tmp_path):
    home = tmp_path / "profiles/deleted"
    with jobs.use_cron_store(home):
        journal.record_created("orphan")
    assert not home.exists()


def _append_in_process(home, ready, start, done):
    with jobs.use_cron_store(home):
        ready.set()
        if start.wait(15):
            journal.record_created("CONCURRENT")
            done.set()


def test_prune_preserves_concurrent_process_append(store, monkeypatch):
    import utils

    journal.record_created("recent")
    path = store / "cron/lifecycle.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({"event": "created", "job_id": "old",
                            "at": "2020-01-01T00:00:00+00:00"}) + "\n")
    ctx = multiprocessing.get_context("spawn")
    ready, start, done = ctx.Event(), ctx.Event(), ctx.Event()
    writer = ctx.Process(target=_append_in_process, args=(store, ready, start, done))
    replace = utils.atomic_replace

    def pause_before_replace(src, dst):
        start.set()
        # Unlocked code completes the append here, then erases it. Locked
        # code holds the writer until replace finishes; verify persisted data,
        # not a negative timing assertion about whether the writer ran.
        done.wait(3)
        return replace(src, dst)

    monkeypatch.setattr(utils, "atomic_replace", pause_before_replace)
    writer.start()
    try:
        assert ready.wait(15)
        assert journal.prune() == 1
        assert done.wait(15)
        writer.join(15)
        assert writer.exitcode == 0
        assert "CONCURRENT" in path.read_text()
        assert "recent" in path.read_text()
    finally:
        if writer.is_alive():
            writer.terminate()
            writer.join(5)
