"""t_811e92d7: a job dropped from jobs.json by a NON-store writer is put back by the store.

2026-10-23 16:18..16:20 PT a process outside cron.jobs copied an older committed blob of
cron/jobs.json over the live store (a git checkout/restore of the scheduler-owned file; same
class as the 10-14 and 10-22 rewinds, t_1a2ccffd). Every job created since that blob vanished
with NO `removed` lifecycle event, and the scheduler's next tick then journalled 25 retention
`removed` events for the stale completed one-shots the blob brought back. Seven live jobs, three
with 10-25/10-27/11-02 deadlines, and the integrity watch that would have paged it, were gone.

The store's own removal paths all thread ``removed_ids`` and journal it. The bad path is a
writer that bypasses the store. The store cannot stop that write, so it refuses the removal
on its next locked read: rows it last wrote, that are now absent and were never journalled
removed, are restored. These tests drop a job through that bad path.
"""

from __future__ import annotations

import json
import logging

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    return home


def _external_rewrite(home, keep_ids):
    """What `git checkout <ref> -- cron/jobs.json` does: whole-file replace, no lock, no journal."""
    path = home / "cron" / "jobs.json"
    doc = json.loads(path.read_text())
    doc["jobs"] = [j for j in doc["jobs"] if j["id"] in keep_ids]
    tmp = path.with_name("jobs.json.git-tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)


def _ids(jobs):
    return sorted(j["id"] for j in jobs)


def test_external_drop_is_restored_on_next_locked_read(store, caplog):
    from cron.jobs import create_job, get_due_jobs, load_jobs

    a = create_job(prompt="a", schedule="every 1h", name="a", deliver="local")
    b = create_job(prompt="b", schedule="2099-01-01T09:00:00", name="deadline-oneshot", deliver="local")
    _external_rewrite(store, keep_ids={a["id"]})
    assert _ids(load_jobs()) == [a["id"]]  # the bad path really dropped it

    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        get_due_jobs()  # any locked read; the gateway ticker does this every tick

    after = load_jobs()
    assert _ids(after) == sorted([a["id"], b["id"]])
    restored = next(j for j in after if j["id"] == b["id"])
    assert restored["name"] == "deadline-oneshot" and restored["next_run_at"] == b["next_run_at"]
    assert "cron.store_external_drop" in caplog.text and b["id"] in caplog.text


def test_journalled_remove_is_not_restored(store):
    from cron.jobs import create_job, get_due_jobs, load_jobs, remove_job

    a = create_job(prompt="a", schedule="every 1h", name="a", deliver="local")
    b = create_job(prompt="b", schedule="every 1h", name="b", deliver="local")
    assert remove_job(b["id"])
    get_due_jobs()
    assert _ids(load_jobs()) == [a["id"]]


def test_external_drop_of_a_journalled_remove_is_not_resurrected(store):
    """A row the journal says was removed stays removed even if the shadow still lists it
    (degraded-lock race between two store writers)."""
    from cron import lifecycle_journal
    from cron.jobs import create_job, get_due_jobs, load_jobs

    a = create_job(prompt="a", schedule="every 1h", name="a", deliver="local")
    b = create_job(prompt="b", schedule="every 1h", name="b", deliver="local")
    lifecycle_journal.record_removed(b["id"], reason="sibling writer")
    _external_rewrite(store, keep_ids={a["id"]})
    get_due_jobs()
    assert _ids(load_jobs()) == [a["id"]]


def test_external_add_is_kept_and_nothing_resurrected_without_a_drop(store):
    """home-reconcile carry_added_jobs/apply_pending write the file outside the store and only
    ADD rows: the store keeps them and restores nothing."""
    from cron.jobs import create_job, get_due_jobs, load_jobs

    a = create_job(prompt="a", schedule="every 1h", name="a", deliver="local")
    path = store / "cron" / "jobs.json"
    doc = json.loads(path.read_text())
    carried = dict(doc["jobs"][0], id="0123456789ab", name="carried")
    doc["jobs"].append(carried)
    path.write_text(json.dumps(doc))
    get_due_jobs()
    assert _ids(load_jobs()) == sorted([a["id"], "0123456789ab"])


def test_no_shadow_is_a_no_op(store):
    """A store last written by an older build (no shadow) is read as-is."""
    from cron.jobs import get_due_jobs, load_jobs

    (store / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
    get_due_jobs()
    assert load_jobs() == []
