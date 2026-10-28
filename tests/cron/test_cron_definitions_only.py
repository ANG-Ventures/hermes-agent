"""t_3c8fa4d6 (LTWC phase A): definitions-only ``cron/jobs.json`` + untracked ``jobs.runtime.json``.

With ``HERMES_CRON_DEFINITIONS_ONLY`` unset (default on) a runtime touch never changes jobs.json's
bytes; the coalesced flush writes the merged view to ``jobs.runtime.json``; a definition save writes
jobs.json with every runtime key stripped. ``=0`` restores the merged write to jobs.json and removes
the untracked mirror. Real store under a temp HERMES_HOME; no mocks of the store code.
"""

import json
import os

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    monkeypatch.delenv("HERMES_CRON_DEFINITIONS_ONLY", raising=False)
    monkeypatch.setenv("HERMES_CRON_MIRROR_INTERVAL_S", "0")  # flush on every touch
    (tmp_path / "cron").mkdir()
    return tmp_path


def _jobs(home):
    return home / "cron" / "jobs.json"


def _mirror(home):
    return home / "cron" / "jobs.runtime.json"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_definitions_only(data):
    from cron import runtime_state as rs

    assert not rs.file_has_runtime(data), data
    assert "runtime_gen" not in data
    for job in data["jobs"]:
        assert not any(k in rs.RUNTIME_FIELDS for k in job), job
        assert "completed" not in (job.get("repeat") or {})


def _claimed(cj, name):
    job = cj.create_job(prompt="x", schedule="every 5m", name=name)
    assert cj.claim_job_for_fire(job["id"]) is True
    return job["id"], cj.get_job(job["id"])["fire_claim"]["by"]


def test_definitions_only_mirror(temp_home):
    """Card acceptance: runtime touch -> jobs.json bytes unchanged; jobs.runtime.json carries the
    row (merged view, row values)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    jid, owner = _claimed(cj, "touch-me")
    cj.maybe_flush_runtime_mirror(force=True)
    before = _jobs(temp_home).read_bytes()
    _assert_definitions_only(json.loads(before))

    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True
    cj.maybe_flush_runtime_mirror(force=True)

    assert _jobs(temp_home).read_bytes() == before, "a runtime touch rewrote jobs.json"
    mirror = _load(_mirror(temp_home))
    row = rs.read_all(temp_home / "cron")[jid]
    rec = next(j for j in mirror["jobs"] if j["id"] == jid)
    assert rec["last_status"] == row["last_status"] == "ok"
    assert rec["last_run_at"] == row["last_run_at"]
    assert rec["repeat"]["completed"] == row["repeat.completed"] == 1
    assert rec["name"] == "touch-me"
    assert list(mirror)[:2] == [rs.MIRROR_EPOCH_KEY, rs.MIRROR_SEQ_KEY]
    # the in-agent merged view is unchanged by where the file mirror lives
    assert cj.get_job(jid)["last_status"] == "ok"


def test_definition_save_strips_runtime_and_refreshes_mirror(temp_home):
    """A definition edit rewrites jobs.json (definitions only, no updated_at) and the runtime
    mirror carries the new definition at once."""
    from cron import jobs as cj

    jid, owner = _claimed(cj, "before")
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True
    cj.update_job(jid, {"name": "after"})
    data = _load(_jobs(temp_home))
    _assert_definitions_only(data)
    assert [j["name"] for j in data["jobs"]] == ["after"]
    rec = _load(_mirror(temp_home))["jobs"][0]
    assert rec["name"] == "after" and rec["last_status"] == "ok"


def test_legacy_merged_jobs_json_is_stripped_once_without_losing_runtime(temp_home):
    """Upgrade path: a merged jobs.json from the old code is adopted into rows, then stripped by
    the first flush; the runtime survives in rows and in jobs.runtime.json; later flushes leave
    jobs.json's bytes alone."""
    from cron import jobs as cj

    job = {"id": "legacy00abcd", "name": "legacy", "prompt": "x", "enabled": True,
           "schedule": {"kind": "interval", "minutes": 5}, "repeat": {"times": None, "completed": 7},
           "last_status": "ok", "last_run_at": "2026-10-28T00:00:00+00:00",
           "next_run_at": "2099-01-01T00:00:00+00:00"}
    _jobs(temp_home).write_text(json.dumps({"_mirror_epoch": 1.0, "_mirror_seq": 3.0,
                                            "jobs": [job], "updated_at": "x"}), encoding="utf-8")
    cj.load_jobs()  # adoption; its section exit flushes (the mirror is missing: due at once)
    cj.maybe_flush_runtime_mirror(force=True)
    data = _load(_jobs(temp_home))
    _assert_definitions_only(data)
    assert data["jobs"][0]["repeat"] == {"times": None}
    rec = _load(_mirror(temp_home))["jobs"][0]
    assert (rec["last_status"], rec["repeat"]["completed"]) == ("ok", 7)
    merged = cj.get_job("legacy00abcd")
    assert merged["next_run_at"] == "2099-01-01T00:00:00+00:00"
    assert merged["repeat"]["completed"] == 7

    stable = _jobs(temp_home).read_bytes()
    cj.maybe_flush_runtime_mirror(force=True)
    assert _jobs(temp_home).read_bytes() == stable


def test_switch_zero_restores_merged_write_and_drops_mirror(temp_home, monkeypatch):
    """I4: HERMES_CRON_DEFINITIONS_ONLY=0 writes the merged view into jobs.json again (runtime
    keys + mirror mark) and removes the stale untracked mirror, so out-of-process readers fall back
    to jobs.json (I5)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    jid, owner = _claimed(cj, "toggle")
    cj.maybe_flush_runtime_mirror(force=True)
    assert _mirror(temp_home).exists()

    monkeypatch.setenv("HERMES_CRON_DEFINITIONS_ONLY", "0")
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True
    cj.maybe_flush_runtime_mirror(force=True)
    data = _load(_jobs(temp_home))
    assert rs.file_has_runtime(data)
    assert data["jobs"][0]["last_status"] == "ok"
    assert list(data)[:2] == [rs.MIRROR_EPOCH_KEY, rs.MIRROR_SEQ_KEY]
    assert not _mirror(temp_home).exists(), "stale jobs.runtime.json left behind with switch off"

    # and back on: jobs.json is stripped again, the mirror republished
    monkeypatch.delenv("HERMES_CRON_DEFINITIONS_ONLY")
    cj.maybe_flush_runtime_mirror(force=True)
    _assert_definitions_only(_load(_jobs(temp_home)))
    assert _load(_mirror(temp_home))["jobs"][0]["last_status"] == "ok"


def test_switch_zero_runtime_touch_rewrites_jobs_json(temp_home, monkeypatch):
    """The off arm is today's behaviour: a runtime touch reaches jobs.json at the flush."""
    from cron import jobs as cj

    monkeypatch.setenv("HERMES_CRON_DEFINITIONS_ONLY", "0")
    jid, owner = _claimed(cj, "legacy-mode")
    cj.maybe_flush_runtime_mirror(force=True)
    before = _jobs(temp_home).read_bytes()
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True
    cj.maybe_flush_runtime_mirror(force=True)
    assert _jobs(temp_home).read_bytes() != before
    assert _load(_jobs(temp_home))["jobs"][0]["last_status"] == "ok"
    assert not _mirror(temp_home).exists()


def test_lost_database_recovers_runtime_from_mirror(temp_home):
    """jobs.json no longer carries runtime, so a lost runtime_state.db recovers from the untracked
    mirror (the merged view the scheduler last published), not to 'never run'."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    jid, owner = _claimed(cj, "survivor")
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True
    cj.maybe_flush_runtime_mirror(force=True)
    expected = cj.get_job(jid)
    rs.drop_database(temp_home / "cron")
    got = cj.get_job(jid)
    assert (got["last_status"], got["last_run_at"], got["repeat"]) == (
        expected["last_status"], expected["last_run_at"], expected["repeat"])


def test_mirror_file_is_private(temp_home):
    from cron import jobs as cj

    _claimed(cj, "perm")
    cj.maybe_flush_runtime_mirror(force=True)
    if os.name == "posix":
        assert (os.stat(_mirror(temp_home)).st_mode & 0o777) == 0o600
