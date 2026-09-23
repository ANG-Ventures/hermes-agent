"""Regressions for the nine P1 findings raised against PR #791's cron guard.

Each test pins a behavior the landed implementation got wrong, and fails
against that implementation. Findings are numbered as they were reported:

1/2. creation and removal journalled AFTER releasing the jobs lock
3.   torn append fuses the next record into a malformed line
4.   journal and store read as independent (racy) snapshots
5.   malformed records silently discarded => `ok` cannot be trusted
6.   causality inferred from wall-clock timestamps instead of append order
7.   window_hours may exceed retention, so a window reports over pruned data
"""
import json
from datetime import timedelta

import pytest

from cron import jobs, lifecycle_journal as journal


@pytest.fixture
def store(tmp_path):
    with jobs.use_cron_store(tmp_path):
        yield tmp_path


def _journal(store):
    return store / "cron" / "lifecycle.jsonl"


# --------------------------------------------------------------- #1 and #2
# Journalling outside the jobs lock lets a concurrent writer interleave its
# record between our store write and our journal append, which inverts the
# append order the guard reads as causality.

def test_create_journals_while_holding_the_jobs_lock(store, monkeypatch):
    depths = []
    real = journal.record_created
    monkeypatch.setattr(
        journal, "record_created",
        lambda jid, **kw: (depths.append(getattr(jobs._jobs_lock_state, "depth", 0)),
                           real(jid, **kw))[1])

    jobs.create_job(prompt="x", schedule="in 10 hours", name="locked")

    assert depths and all(d > 0 for d in depths), (
        f"record_created ran at jobs-lock depth {depths} — must be > 0")


def test_remove_journals_while_holding_the_jobs_lock(store, monkeypatch):
    job = jobs.create_job(prompt="x", schedule="in 10 hours", name="locked")
    depths = []
    real = journal.record_removed
    monkeypatch.setattr(
        journal, "record_removed",
        lambda jid, **kw: (depths.append(getattr(jobs._jobs_lock_state, "depth", 0)),
                           real(jid, **kw))[1])

    jobs.save_jobs([], removed_ids=[job["id"]])

    assert depths and all(d > 0 for d in depths), (
        f"record_removed ran at jobs-lock depth {depths} — must be > 0")


# ------------------------------------------------------------------- #3
def test_torn_record_does_not_swallow_the_next_event(store):
    """A crash-torn tail must not fuse with, and lose, the next append."""
    journal.record_created("BEFORE")
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write('{"event": "created", "job_id": "TOR')  # killed mid-record

    journal.record_created("AFTER_TEAR")

    parsed = []
    for line in _journal(store).read_text().splitlines():
        if not line.strip():
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            parsed.append(None)
    ids = [p.get("job_id") for p in parsed if p]
    assert "BEFORE" in ids
    assert "AFTER_TEAR" in ids, (
        "the post-tear event fused into the torn line and was lost")


# ------------------------------------------------------------------- #4
def test_reconciliation_reads_one_coherent_snapshot(store, monkeypatch):
    """Journal and store must be read under the jobs lock, not independently."""
    depths = {}
    real_load = jobs.load_jobs
    monkeypatch.setattr(
        jobs, "load_jobs",
        lambda: (depths.__setitem__("load_jobs",
                                    getattr(jobs._jobs_lock_state, "depth", 0)),
                 real_load())[1])
    real_read = journal.read_entries_with_health
    monkeypatch.setattr(
        journal, "read_entries_with_health",
        lambda **kw: (depths.__setitem__("read_entries",
                                         getattr(jobs._jobs_lock_state, "depth", 0)),
                      real_read(**kw))[1])

    journal.check_vanished_jobs()

    assert depths.get("read_entries", 0) > 0, "journal read was unlocked"
    assert depths.get("load_jobs", 0) > 0, "store read was unlocked"


def test_guard_does_not_poison_an_outer_sections_load_baseline(store):
    """The guard's own load_jobs must not clobber a caller's merge baseline."""
    jobs.create_job(prompt="x", schedule="in 10 hours", name="outer")
    with jobs._jobs_lock():
        loaded = jobs.load_jobs()
        stamp = getattr(jobs._jobs_lock_state, "load_stamp", None)
        baseline = getattr(jobs._jobs_lock_state, "load_baseline", None)
        journal.check_vanished_jobs()
        assert getattr(jobs._jobs_lock_state, "load_stamp", None) == stamp
        assert getattr(jobs._jobs_lock_state, "load_baseline", None) == baseline
        jobs.save_jobs(loaded)


# ------------------------------------------------------------------- #5
def test_unparseable_record_prevents_a_green_verdict(store):
    """`ok` must never be reported over a record the guard could not read."""
    journal.record_created("A")
    jobs.save_jobs([{"id": "A", "name": "a"}], replace=True)
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write("{not json at all\n")

    report = journal.check_vanished_jobs()

    assert report.status == journal.STATUS_UNAVAILABLE
    assert report.should_alert
    assert "unparseable" in (report.detail or "")


def test_record_with_an_unusable_timestamp_also_prevents_green(store):
    journal.record_created("A")
    jobs.save_jobs([{"id": "A", "name": "a"}], replace=True)
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "created", "job_id": "B",
                            "at": "not-a-timestamp"}) + "\n")

    assert journal.check_vanished_jobs().status == journal.STATUS_UNAVAILABLE


@pytest.mark.parametrize("invalid_fields", [
    pytest.param({"job_id": "A"}, id="event-missing"),
    pytest.param({"event": None, "job_id": "A"}, id="event-null"),
    pytest.param({"event": "", "job_id": "A"}, id="event-empty"),
    pytest.param({"event": "   ", "job_id": "A"}, id="event-whitespace"),
    pytest.param({"event": "typo", "job_id": "A"}, id="event-unknown"),
    pytest.param({"event": 1, "job_id": "A"}, id="event-number"),
    pytest.param({"event": True, "job_id": "A"}, id="event-bool"),
    pytest.param({"event": [], "job_id": "A"}, id="event-list"),
    pytest.param({"event": {}, "job_id": "A"}, id="event-object"),
    pytest.param({"event": journal.EVENT_CREATED}, id="job-id-missing"),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": None}, id="job-id-null"),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": ""}, id="job-id-empty"),
    pytest.param(
        {"event": journal.EVENT_CREATED, "job_id": "   "},
        id="job-id-whitespace",
    ),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": 123}, id="job-id-number"),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": True}, id="job-id-bool"),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": []}, id="job-id-list-empty"),
    pytest.param(
        {"event": journal.EVENT_CREATED, "job_id": ["A"]},
        id="job-id-list-nonempty",
    ),
    pytest.param({"event": journal.EVENT_CREATED, "job_id": {}}, id="job-id-object-empty"),
    pytest.param(
        {"event": journal.EVENT_CREATED, "job_id": {"id": "A"}},
        id="job-id-object-nonempty",
    ),
])
def test_every_semantically_invalid_record_shape_prevents_green(
    store, invalid_fields
):
    """Every shape reconciliation used to skip must make health unknown."""
    jobs.save_jobs([], replace=True)
    record = {"at": journal._hermes_now().isoformat(), **invalid_fields}
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    report = journal.check_vanished_jobs()

    assert report.status == journal.STATUS_UNAVAILABLE
    assert report.should_alert
    assert "invalid" in (report.detail or "")


@pytest.mark.parametrize(
    "invalid_record",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("record", id="string"),
        pytest.param(123, id="number"),
    ],
)
def test_non_object_json_record_prevents_green(store, invalid_record):
    jobs.save_jobs([], replace=True)
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write(json.dumps(invalid_record) + "\n")

    assert journal.check_vanished_jobs().status == journal.STATUS_UNAVAILABLE


# ------------------------------------------------------------------- #6
def test_removal_after_a_create_is_honoured_despite_a_skewed_clock(store):
    """Causality comes from append order, not from writer wall clocks."""
    journal.record_created("SKEWED", name="job")
    now = journal._hermes_now()
    # A remover whose clock is five minutes slow appends AFTER the create.
    with open(_journal(store), "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "event": "removed", "job_id": "SKEWED",
            "at": (now - timedelta(minutes=5)).isoformat(),
        }) + "\n")

    report = journal.check_vanished_jobs()

    assert report.status == journal.STATUS_OK, (
        "an intentional removal was reported as a loss because its timestamp "
        "predates the create it followed")


def test_a_create_appended_after_a_removal_is_still_a_real_loss(store):
    """The inverse must keep working: re-created, then lost, is `vanished`."""
    journal.record_removed("REUSED", reason="first life")
    journal.record_created("REUSED", name="second life")

    report = journal.check_vanished_jobs()

    assert report.status == journal.STATUS_VANISHED
    assert [v["job_id"] for v in report.vanished] == ["REUSED"]


# ------------------------------------------------------------------- #7
def test_window_beyond_retention_is_refused_not_certified(store):
    """A window wider than retention reports over data that was pruned away."""
    assert journal.MAX_WINDOW_HOURS == journal._RETENTION_DAYS * 24.0
    assert journal.DEFAULT_WINDOW_HOURS <= journal.MAX_WINDOW_HOURS

    journal.record_created("OLD")
    report = journal.check_vanished_jobs(
        window_hours=journal.MAX_WINDOW_HOURS + 1)

    assert report.status == journal.STATUS_UNAVAILABLE
    assert report.should_alert
    assert "retention" in (report.detail or "")


def test_a_window_at_retention_is_still_allowed(store):
    journal.record_created("A")
    jobs.save_jobs([{"id": "A", "name": "a"}], replace=True)

    report = journal.check_vanished_jobs(window_hours=journal.MAX_WINDOW_HOURS)

    assert report.status == journal.STATUS_OK
