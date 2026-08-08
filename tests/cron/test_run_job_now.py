"""Tests for synchronous single-job execution (`hermes cron run <id> --wait`).

`run_job_now()` runs ONE job end-to-end (execute → save → deliver → mark) in the
calling thread and returns a structured result, without waiting for a scheduler
tick and regardless of whether the job is "due". This backs the CLI `--wait`
flag so slow jobs can be verified to completion in the foreground.
"""
import pytest

import cron.scheduler as scheduler


@pytest.fixture
def fake_job():
    return {"id": "abc123", "name": "slow-digest", "schedule": {"expr": "0 7 * * *"}}


def _patch_pipeline(monkeypatch, *, success=True, final_response="done", output="full-doc",
                    error=None, job=None):
    """Patch the scheduler's execute/save/deliver/mark seams and record calls."""
    calls = {"run_job": 0, "save": 0, "deliver": [], "deliver_success": [], "mark": []}

    def fake_run_job(j):
        calls["run_job"] += 1
        return success, output, final_response, error

    def fake_save(job_id, out):
        calls["save"] += 1
        return f"/tmp/{job_id}.md"

    def fake_deliver(j, content, success=True, adapters=None, loop=None):
        calls["deliver"].append(content)
        calls["deliver_success"].append(success)
        return None  # no delivery error

    def fake_mark(job_id, ok, err, delivery_error=None):
        calls["mark"].append((job_id, ok, err, delivery_error))

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", fake_save)
    monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)
    monkeypatch.setattr(scheduler, "mark_job_run", fake_mark)
    # run_job_now resolves an ID *or* a name (resolve_job_ref), not get_job.
    # Mirror the real resolver's semantics so a by-name lookup is genuinely
    # exercised rather than matching any string.
    def fake_resolve(ref):
        if job is None:
            return None
        if ref == job.get("id") or ref.lower() == (job.get("name") or "").lower():
            return job
        return None

    monkeypatch.setattr(scheduler, "resolve_job_ref", fake_resolve, raising=False)
    # get_job stays ID-ONLY (matching the real implementation) so a test that
    # passes by NAME proves run_job_now went through the resolver. Patching it
    # to return the job for any string would make the name tests vacuous —
    # they'd pass with or without the fix.
    monkeypatch.setattr(
        scheduler, "get_job",
        lambda jid: job if (job is not None and jid == job.get("id")) else None,
        raising=False,
    )
    return calls


class TestRunJobNow:
    def test_runs_job_synchronously_and_reports_success(self, monkeypatch, fake_job):
        calls = _patch_pipeline(monkeypatch, success=True, final_response="hello",
                                job=fake_job)
        result = scheduler.run_job_now("abc123", verbose=False)
        assert result["success"] is True
        assert result["final_response"] == "hello"
        assert result["job_id"] == "abc123"
        assert calls["run_job"] == 1
        assert calls["save"] == 1
        # success path delivers the real response
        assert calls["deliver"] == ["hello"]
        assert calls["mark"][0][:3] == ("abc123", True, None)

    def test_failure_surfaces_alert_and_marks_failed(self, monkeypatch, fake_job):
        calls = _patch_pipeline(monkeypatch, success=False, final_response="",
                                error="boom", job=fake_job)
        result = scheduler.run_job_now("abc123", verbose=False)
        assert result["success"] is False
        assert result["error"] == "boom"
        # Failed jobs still deliver. The error is now framed by
        # _summarize_cron_failure_for_delivery (a compact one-liner) rather than
        # handed raw to the wrapper — matching run_one_job's path. A non-transient
        # defect like "boom" is delivered (not suppressed) with a "failed:" frame.
        assert calls["deliver"], "failure should still deliver an alert"
        assert len(calls["deliver"]) == 1
        assert "boom" in calls["deliver"][0]
        assert "failed" in calls["deliver"][0].lower()
        assert calls["deliver_success"] == [False]
        assert calls["mark"][0][1] is False

    def test_unknown_job_id_returns_error_without_running(self, monkeypatch):
        calls = _patch_pipeline(monkeypatch, job=None)  # get_job → None
        result = scheduler.run_job_now("nope", verbose=False)
        assert result["success"] is False
        assert "not found" in result["error"].lower()
        assert calls["run_job"] == 0

    def test_silent_marker_skips_delivery(self, monkeypatch, fake_job):
        calls = _patch_pipeline(monkeypatch, success=True,
                                final_response=scheduler.SILENT_MARKER, job=fake_job)
        result = scheduler.run_job_now("abc123", verbose=False)
        assert result["success"] is True
        assert calls["deliver"] == [], "[SILENT] should suppress delivery"

    def test_does_not_require_job_to_be_due(self, monkeypatch, fake_job):
        """run_job_now bypasses get_due_jobs entirely — a not-due job still runs."""
        called = {"due": 0}
        monkeypatch.setattr(scheduler, "get_due_jobs",
                            lambda: called.__setitem__("due", called["due"] + 1) or [])
        _patch_pipeline(monkeypatch, success=True, final_response="x", job=fake_job)
        scheduler.run_job_now("abc123", verbose=False)
        assert called["due"] == 0, "run_job_now must not consult the due-list"


class TestRunJobNowAcceptsName:
    """`hermes cron run <name> --wait` must work, not just `<id>` (2026-08-08).

    run_job_now used the ID-only get_job(), so running a job by the name that
    `hermes cron list` prints returned "Job not found" — and our own alert
    templates emit that exact by-name command as their triage step, sending
    whoever follows them into a dead end.
    """

    def test_runs_job_referenced_by_name(self, monkeypatch, fake_job):
        calls = _patch_pipeline(monkeypatch, success=True, final_response="hi",
                                job=fake_job)
        result = scheduler.run_job_now("slow-digest", verbose=False)
        assert result["success"] is True, result.get("error")
        assert calls["run_job"] == 1

    def test_name_match_is_case_insensitive(self, monkeypatch, fake_job):
        calls = _patch_pipeline(monkeypatch, success=True, final_response="hi",
                                job=fake_job)
        result = scheduler.run_job_now("SLOW-DIGEST", verbose=False)
        assert result["success"] is True, result.get("error")
        assert calls["run_job"] == 1

    def test_id_still_works(self, monkeypatch, fake_job):
        """The by-ID path must not regress — it is what every cron alert links."""
        calls = _patch_pipeline(monkeypatch, success=True, final_response="hi",
                                job=fake_job)
        result = scheduler.run_job_now("abc123", verbose=False)
        assert result["success"] is True
        assert calls["run_job"] == 1

    def test_ambiguous_name_reports_the_candidate_ids_without_running(
            self, monkeypatch, fake_job):
        """Two jobs sharing a name must NOT silently run one of them."""
        calls = _patch_pipeline(monkeypatch, job=fake_job)

        def boom(ref):
            raise scheduler.AmbiguousJobReference(
                ref,
                [{"id": "aaa111", "name": "dup"}, {"id": "bbb222", "name": "dup"}],
            )

        monkeypatch.setattr(scheduler, "resolve_job_ref", boom, raising=False)
        result = scheduler.run_job_now("dup", verbose=False)
        assert result["success"] is False
        assert "ambiguous" in result["error"].lower()
        # the operator needs the IDs to disambiguate
        assert "aaa111" in result["error"] and "bbb222" in result["error"]
        assert calls["run_job"] == 0, "must not run a job on an ambiguous reference"
