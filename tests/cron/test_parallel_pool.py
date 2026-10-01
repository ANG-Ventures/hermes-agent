"""Tests for the persistent parallel pool and running-job guard in cron/scheduler.py.

These verify the fix for the tick-blocking issue where as_completed(timeout=600)
prevented the ticker thread from firing, causing all other jobs to be fast-forwarded.
"""

import concurrent.futures
import threading
import time

class TestRunningJobGuard:
    """_running_job_ids prevents double-dispatch of active jobs."""

    def test_running_set_prevents_double_dispatch(self, tmp_path, monkeypatch):
        """A job already in _running_job_ids is skipped on the next tick."""
        import cron.scheduler as sched

        # Reset state.
        sched._parallel_pools.clear()
        sched._parallel_pool_max_workers.clear()
        sched._running_job_ids.clear()

        job = {
            "id": "guard-job",
            "name": "guard-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        # Simulate the job already running.
        sched._running_job_ids.add(sched._inflight_key("guard-job"))

        dispatched = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "claim_job_for_fire", lambda *_a, **_kw: True)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: dispatched.append(j["id"]) or (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)
        assert n == 0  # skipped, not dispatched
        assert dispatched == []

        sched._running_job_ids.discard(sched._inflight_key("guard-job"))
        sched._shutdown_parallel_pool()

    def test_fire_claim_is_acquired_only_when_executor_worker_starts(self, monkeypatch):
        """Queue wait must not consume the durable claim TTL."""
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        job = {
            "id": "queued-job",
            "name": "queued",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        submitted = []
        claim_calls = []

        class DeferredPool:
            def submit(self, callback):
                future = concurrent.futures.Future()
                submitted.append((callback, future))
                return future

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda _workers: DeferredPool())
        monkeypatch.setattr(
            sched,
            "create_execution",
            lambda *_a, **_kw: {"id": "execution-1"},
        )
        monkeypatch.setattr(
            sched,
            "claim_job_for_fire",
            lambda job_id, **kwargs: claim_calls.append((job_id, kwargs))
            or {**job, "fire_claim": {"by": "worker-owner", "at": "now"}},
        )
        monkeypatch.setattr(sched, "run_one_job", lambda *_a, **_kw: True)

        assert sched.tick(verbose=False, sync=False) == 1
        assert claim_calls == []
        assert len(submitted) == 1

        callback, future = submitted[0]
        result = callback()
        future.set_result(result)

        assert claim_calls == [("queued-job", {"return_job": True})]
        assert sched._inflight_key("queued-job") not in sched._running_job_ids

    def test_create_execution_failure_does_not_wedge_running_set(self, tmp_path, monkeypatch):
        """create_execution failures clear the running lock and still allow next jobs."""
        import cron.scheduler as sched

        sched._parallel_pools.clear()
        sched._parallel_pool_max_workers.clear()
        sched._running_job_ids.clear()

        failing_job = {
            "id": "failing-job",
            "name": "failing-job",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        healthy_job = {
            "id": "healthy-job",
            "name": "healthy-job",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        called = []

        def create_execution_side_effect(job_id, source, **kwargs):
            if job_id == "failing-job":
                raise RuntimeError("execution ledger unavailable")
            return {"id": f"{job_id}-execution"}

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [failing_job, healthy_job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "create_execution", create_execution_side_effect)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: called.append(j["id"]) or (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "finish_execution", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "claim_dispatch", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            sched,
            "claim_job_for_fire",
            lambda job_id, **_kw: dict(
                healthy_job, fire_claim={"by": "test-owner", "at": "now"}
            )
            if job_id == "healthy-job"
            else None,
        )
        monkeypatch.setattr(sched, "mark_execution_running", lambda *_a, **_kw: {})
        monkeypatch.setattr(sched, "heartbeat_fire_claim", lambda *_a, **_kw: True)

        n = sched.tick(verbose=False)

        assert n == 1
        assert called == ["healthy-job"]
        assert sched._inflight_key("failing-job") not in sched._running_job_ids
        assert sched._inflight_key("healthy-job") not in sched._running_job_ids

        sched._shutdown_parallel_pool()

class TestSyncMode:
    """tick() blocks by default (sync=True); tick(sync=False) returns immediately."""

    def test_sync_true_blocks_and_returns_correct_count(self, tmp_path, monkeypatch):
        """sync=True waits for jobs and returns actual results."""
        import cron.scheduler as sched

        sched._parallel_pools.clear()
        sched._parallel_pool_max_workers.clear()
        sched._running_job_ids.clear()

        jobs = [
            {"id": f"job-{i}", "name": f"Job {i}", "prompt": "test",
             "schedule": "every 5m", "enabled": True,
             "next_run_at": "2020-01-01T00:00:00", "deliver": "local"}
            for i in range(3)
        ]

        monkeypatch.setattr(sched, "get_due_jobs", lambda: jobs)
        monkeypatch.setattr(sched, "claim_job_for_fire", lambda *_a, **_kw: True)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)
        assert n == 3

        sched._shutdown_parallel_pool()

    def test_sync_false_returns_immediately(self, tmp_path, monkeypatch):
        """sync=False returns before parallel jobs finish (optimistic count)."""
        import cron.scheduler as sched

        sched._parallel_pools.clear()
        sched._parallel_pool_max_workers.clear()
        sched._running_job_ids.clear()

        job = {
            "id": "slow-job",
            "name": "slow",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        barrier = threading.Barrier(2, timeout=10)
        job_returned = threading.Event()

        def slow_run(j, *, defer_agent_teardown=None, **_kw):
            # **_kw: upstream's fire-claim call site passes extra_prompt= /
            # cancel_event=; a narrow stub signature TypeErrors in the worker
            # thread and breaks the barrier (same **kwargs class as the
            # run_conversation stubs, wave-1 2026-08-30).
            try:
                barrier.wait()  # blocks until test thread also waits
                return True, "out", "resp", None
            finally:
                job_returned.set()

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        # Upstream's fire path now runs create_execution + claim_job_for_fire
        # BEFORE run_job (durable execution ledger); without these stubs the
        # real claim fails on the nonexistent store job and the worker exits
        # before run_job — breaking the barrier. Same stub pattern as
        # test_deferred_pool_claims_at_execution_start above.
        monkeypatch.setattr(
            sched, "create_execution", lambda *_a, **_kw: {"id": "execution-1"}
        )
        monkeypatch.setattr(
            sched,
            "claim_job_for_fire",
            lambda job_id, **_kw: {
                **job,
                "fire_claim": {"by": "test-owner", "at": "now"},
            },
        )
        monkeypatch.setattr(sched, "mark_execution_running", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "finish_execution", lambda *_a, **_kw: None)
        # The fire-claim heartbeat re-verifies ownership against the real store
        # before run_job; stub it to keep the claim "owned" for the stub job.
        monkeypatch.setattr(
            sched, "heartbeat_fire_claim", lambda *_a, **_kw: True
        )
        monkeypatch.setattr(sched, "run_job", slow_run)
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False, sync=False)  # opt-in: non-blocking

        assert n == 1  # optimistic count
        # Ordering witness (replaces `elapsed < 1.0`): the job is provably
        # still parked at the barrier — which only the test thread can
        # release — when tick() returns.  The barrier was already the real
        # witness; the stopwatch was redundant risk.
        assert not job_returned.is_set(), "tick(sync=False) waited for slow_run"

        # Let the job finish so cleanup works.
        barrier.wait()
        assert job_returned.wait(timeout=10)
        sched._shutdown_parallel_pool()
