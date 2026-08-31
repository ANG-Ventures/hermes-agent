"""Regression: the built-in ticker must CLOSE its execution-ledger rows.

Bug (diagnosed live 2026-08-06 from a real 948-row backlog):

The scheduler has TWO firing bodies that share ``run_job``:

  * ``run_one_job``      — the external-provider path. Creates/uses an
    ``execution_id``, calls ``mark_execution_running`` before dispatch and
    ``finish_execution`` on every exit.
  * ``_process_one_job`` — the BUILT-IN TICKER path (tick → parallel/sequential
    pools → ``_process_job`` → here) and ``run_job_now``.

The tick creates the ledger row itself (``create_execution(job_id,
source="builtin")`` and hands the id down on the job dict as ``execution_id``),
but ``_process_one_job`` never read that key: it called neither
``mark_execution_running`` nor ``finish_execution``.

Consequence: every ticker-fired job ran fine and wrote its output file, while
its ledger row stayed ``claimed`` with ``started_at=None`` forever. On the next
scheduler start, ``recover_interrupted_executions()`` swept the whole backlog to
``unknown`` with "Scheduler restarted after this execution's owner exited before
a durable terminal state; whether side effects ran is unknown."

The live database showed the signature exactly: of 948 real ticker rows, 948
were ``unknown``, 0 had ever reached ``running``, and there was exactly ONE
``completed`` row in the entire history — while ``cron/output/<job>/`` proved
the jobs were executing successfully every single tick. A perfectly healthy
scheduler read as a crash-loop.

These tests assert the ledger calls happen on each exit path.
"""
import os
import re

SCHEDULER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "cron",
    "scheduler.py",
)


def _body(func_name: str) -> str:
    """Return the source text of a top-level function in cron/scheduler.py."""
    lines = open(SCHEDULER, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"def {func_name}"))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("def ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


class TestTickerClosesItsLedgerRows:
    def test_process_one_job_reads_the_execution_id(self):
        """The tick passes execution_id on the job dict; this body must read it."""
        body = _body("_process_one_job")
        assert 'job.get("execution_id")' in body, (
            "_process_one_job must read the execution_id the ticker hands it, "
            "otherwise every ticker-fired run strands its ledger row at 'claimed'"
        )

    def test_process_one_job_marks_running(self):
        body = _body("_process_one_job")
        assert "mark_execution_running(" in body, (
            "the attempt must advance to 'running' before the actual run, "
            "mirroring run_one_job"
        )

    def test_process_one_job_finishes_on_every_exit_path(self):
        """Success, exception, and dispatch-claim-rejected must all close."""
        body = _body("_process_one_job")
        assert body.count("finish_execution(") >= 3, (
            "every exit path (success, exception, dispatch-claim rejected) must "
            f"close the row; found {body.count('finish_execution(')} call(s)"
        )

    def test_mark_running_precedes_run_job(self):
        """Ordering matters: 'running' must be stamped BEFORE the work starts."""
        body = _body("_process_one_job")
        mark = body.index("mark_execution_running(")
        run = body.index("success, output, final_response, error = run_job(job)")
        assert mark < run, "mark_execution_running must precede run_job"

    def test_parity_with_the_sibling_firing_body(self):
        """Both firing bodies share run_job; both must do the same bookkeeping.

        This is the invariant that was violated — a second entry point grew
        without the ledger calls its sibling had.
        """
        # parity 2026-08-30: upstream restructured run_one_job into a thin
        # fire-claim wrapper; the ledger bookkeeping lives in the SHARED
        # _run_one_job_body both entry points call (OPERATOR-DECISION 1,
        # godfile-scheduler EVIDENCE). The contract holds at that shared body.
        ticker = _body("_process_one_job") + _body("_run_one_job_body")
        provider = _body("run_one_job") + _body("_run_one_job_body")
        for call in ("mark_execution_running(", "finish_execution("):
            assert (call in ticker) == (call in provider), (
                f"{call} present in only one firing body — the ledger contract "
                "must hold for every path that runs a job"
            )


class TestRecoverySemanticsUnchanged:
    def test_recovery_still_only_sweeps_non_live_owners(self):
        """Guard against 'fixing' the symptom by weakening recovery instead."""
        src = open(
            os.path.join(os.path.dirname(SCHEDULER), "executions.py"), encoding="utf-8"
        ).read()
        assert "_owner_is_live(" in src, (
            "recovery must still skip executions whose owner process is alive"
        )
        assert "status='unknown'" in src, "abandoned rows are still classified unknown"
