"""Regression for #80624 — concurrent creates must not be clobbered on save.

``no_agent`` watchdog jobs (and any CLI/tool create while the gateway ticker
or a ``cron remove`` is live) were vanishing from ``jobs.json`` when a writer
persisted a stale/smaller in-memory snapshot. The save path now merges
unexpected on-disk ids back unless the caller passes ``removed_ids``.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    (home / "scripts" / "watch.sh").write_text("#!/usr/bin/env bash\necho alert\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    return home


def test_stale_empty_save_preserves_concurrent_no_agent_create(hermes_env):
    """Gateway-style stale writer with [] must not wipe a concurrent create."""
    from cron.jobs import create_job, load_jobs, save_jobs

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    assert [j["id"] for j in load_jobs()] == [job["id"]]

    # Simulate a degraded-lock writer that still holds an empty snapshot from
    # before the create (the filed incident: jobs.json rewritten empty).
    save_jobs([])

    remaining = load_jobs()
    assert [j["id"] for j in remaining] == [job["id"]]
    assert remaining[0].get("no_agent") is True
    assert remaining[0].get("script") == "watch.sh"


def test_remove_other_job_preserves_concurrent_create(hermes_env):
    """``cron remove`` of job A must not drop job B created mid-flight."""
    from cron.jobs import create_job, load_jobs, save_jobs

    agent = create_job(
        prompt="hello",
        schedule="every 5m",
        name="agent",
        deliver="local",
    )
    # Stale remove payload: only knew about `agent`, never saw the watchdog.
    stale_after_remove = []
    save_jobs(stale_after_remove, removed_ids={agent["id"]})

    watchdog = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    # A second stale remove of `agent` (already gone) with empty payload —
    # must keep the watchdog that landed on disk in between.
    save_jobs([], removed_ids={agent["id"]})

    ids = {j["id"] for j in load_jobs()}
    assert ids == {watchdog["id"]}


def test_intentional_remove_still_deletes(hermes_env):
    from cron.jobs import create_job, get_job, remove_job

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    assert remove_job(job["id"]) is True
    assert get_job(job["id"]) is None


def test_replace_flag_allows_wholesale_rewrite(hermes_env):
    from cron.jobs import create_job, load_jobs, save_jobs

    create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    save_jobs([], replace=True)
    assert load_jobs() == []




def test_sibling_write_inside_section_is_merged(hermes_env):
    """A write that lands on disk after the section's load changes the stamp,
    so the save must re-merge instead of trusting its stale snapshot."""
    import cron.jobs as jobs
    from cron.jobs import create_job

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )

    with jobs._jobs_lock():
        current = jobs.load_jobs()
        (jobs._current_cron_store().jobs_file).write_text(
            json.dumps({"jobs": [dict(job), {"id": "bbbbbbbbbbbb", "name": "b"}]}),
            encoding="utf-8",
        )
        jobs._save_jobs_unlocked(current)
    ids = {j["id"] for j in jobs.load_jobs()}
    assert ids == {job["id"], "bbbbbbbbbbbb"}


def test_merge_does_not_mutate_caller_list(hermes_env):
    """The shrink-merge returns a new list; the caller's payload object must
    not grow as a side effect of save_jobs()."""
    from cron.jobs import create_job, save_jobs

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    my_payload = []  # stale snapshot from before the create
    save_jobs(my_payload)
    assert my_payload == [], "caller's list was mutated in place by the merge"


def test_save_over_corrupt_store_fails_closed(hermes_env):
    """A merging save over an unreadable jobs.json must refuse (the jobs in it
    are unknown, so overwriting would drop them) and leave the bytes intact;
    ``replace=True`` stays available as the explicit recovery rewrite."""
    import cron.jobs as jobs
    from cron.jobs import load_jobs, save_jobs

    jobs.ensure_dirs()
    jobs_file = jobs._current_cron_store().jobs_file
    corrupt = b'{"jobs": [{"id": "ccc'
    jobs_file.write_bytes(corrupt)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        save_jobs([{"id": "aaaaaaaaaaaa", "name": "a"}])
    assert jobs_file.read_bytes() == corrupt

    save_jobs([{"id": "aaaaaaaaaaaa", "name": "a"}], replace=True)
    assert [j["id"] for j in load_jobs()] == ["aaaaaaaaaaaa"]


def test_nested_create_survives_outer_stale_save(hermes_env):
    """A save inside a critical section invalidates the section's stamp, so
    an outer caller's later save with a pre-create payload must re-merge and
    keep the nested create (stamp refresh here would deterministically
    clobber it)."""
    import cron.jobs as jobs
    from cron.jobs import create_job, load_jobs, save_jobs

    seed = {"id": "aaaaaaaaaaaa", "name": "a"}
    save_jobs([seed], replace=True)

    with jobs._jobs_lock():
        stale = jobs.load_jobs()  # records the stamp
        created = create_job(
            prompt="x", schedule="every 5m", name="nested", deliver="local"
        )
        jobs._save_jobs_unlocked(stale)  # outer save with pre-create payload

    ids = {j["id"] for j in load_jobs()}
    assert created["id"] in ids, "nested create was clobbered by outer stale save"
    assert seed["id"] in ids


def test_stale_save_does_not_resurrect_a_sibling_api_removal(hermes_env):
    """t_0cae2938 (Studio 10-26, ffe40c023e7f): a writer loads, a sibling removes a job through the API
    (journaled `removed`), the writer saves its stale payload. The row must stay gone: written back it
    runs again with no `created` event, and the journal no longer describes the store."""
    import cron.jobs as jobs
    from cron.jobs import create_job, load_jobs
    from cron.lifecycle_journal import JOURNAL_FILENAME, record_removed

    keep = create_job(prompt="keep", schedule="every 5m", name="keep", deliver="local")
    gone = create_job(prompt="gone", schedule="every 5m", name="gone", deliver="local")
    jobs_file = jobs._current_cron_store().jobs_file

    with jobs._jobs_lock():
        stale = jobs.load_jobs()
        # the sibling's API removal, under a degraded lock: row off disk, `removed` journaled
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        data["jobs"] = [j for j in data["jobs"] if j["id"] != gone["id"]]
        jobs_file.write_text(json.dumps(data), encoding="utf-8")
        record_removed(gone["id"])
        for j in stale:
            if j["id"] == keep["id"]:
                j["name"] = "keep-renamed"   # this writer's own edit still lands
        jobs._save_jobs_unlocked(stale)

    by_id = {j["id"]: j for j in load_jobs()}
    assert gone["id"] not in by_id, "stale save resurrected a job the API removed"
    assert by_id[keep["id"]]["name"] == "keep-renamed"
    events = [json.loads(ln)["event"] for ln in (hermes_env / "cron" / JOURNAL_FILENAME).read_text().splitlines()
              if json.loads(ln)["job_id"] == gone["id"]]
    assert events == ["created", "removed"]


def test_stale_save_keeps_a_row_the_journal_does_not_say_was_removed(hermes_env):
    """The guard reads the journal, not mere absence: a row missing from disk with no `removed` event (a
    non-API writer dropped it) is written back as before (#80624's lost-write repair)."""
    import cron.jobs as jobs
    from cron.jobs import create_job, load_jobs

    lost = create_job(prompt="lost", schedule="every 5m", name="lost", deliver="local")
    jobs_file = jobs._current_cron_store().jobs_file
    with jobs._jobs_lock():
        stale = jobs.load_jobs()
        jobs_file.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        jobs._save_jobs_unlocked(stale)
    assert lost["id"] in {j["id"] for j in load_jobs()}
