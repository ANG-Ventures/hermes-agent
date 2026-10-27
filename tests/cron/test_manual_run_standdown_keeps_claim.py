"""A manual ``cron run --wait`` that stands down beside a live run never clears its fire_claim.

10-27 04:30 x-feed-brief: a scheduled run held the fire_claim; a wrapper's
``hermes cron run <id> --wait`` ran beside it, and its terminal ``mark_job_run`` (no
owner fence) cleared the claim. The live run's heartbeat then lost ownership and the
scheduler killed its own healthy run at 04:39:43 ("lost its durable fire claim ownership").
"""
from cron import jobs
import cron.scheduler as scheduler


def _live_claimed_job(tmp_path):
    job = jobs.create_job(prompt="brief", schedule="every 1h", name="x-feed-brief")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    return job["id"], claimed["fire_claim"]["by"]


def test_manual_run_beside_live_claim_defers_and_keeps_claim(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(scheduler, "run_job", lambda j: ran.append(j["id"]) or (True, "o", "r", None))
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    with jobs.use_cron_store(tmp_path / "cron"):
        job_id, owner_a = _live_claimed_job(tmp_path)

        result = scheduler.run_job_now(job_id, verbose=False)

        assert ran == [], "a manual run beside a live fire_claim must not execute the job"
        assert result["deferred_to"] == owner_a
        assert owner_a in result["error"]
        after = jobs.get_job(job_id)
        assert (after.get("fire_claim") or {}).get("by") == owner_a, "B cleared A's fire_claim"
        assert jobs.mark_job_run(job_id, True, expected_fire_owner=owner_a) is True
        assert jobs.get_job(job_id)["fire_claim"] is None


def test_manual_run_whose_sibling_claims_mid_run_does_not_clear_claim(tmp_path, monkeypatch):
    """B starts unclaimed; A claims while B runs. B's terminal write must not clear A's claim."""
    owners = []

    def run_while_a_claims(j):
        claimed = jobs.claim_job_for_fire(j["id"], return_job=True)
        owners.append(claimed["fire_claim"]["by"])
        return True, "o", "r", None

    monkeypatch.setattr(scheduler, "run_job", run_while_a_claims)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="brief", schedule="every 1h", name="x-feed-brief")

        scheduler.run_job_now(job["id"], verbose=False)

        assert (jobs.get_job(job["id"]).get("fire_claim") or {}).get("by") == owners[0]
        assert jobs.mark_job_run(job["id"], True, expected_fire_owner=owners[0]) is True


def test_manual_run_with_no_live_claim_still_runs(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(scheduler, "run_job", lambda j: ran.append(j["id"]) or (True, "o", "r", None))
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="brief", schedule="every 1h", name="x-feed-brief")

        result = scheduler.run_job_now(job["id"], verbose=False)

        assert ran == [job["id"]]
        assert result["success"] is True
        assert jobs.get_job(job["id"])["last_status"] == "ok"
