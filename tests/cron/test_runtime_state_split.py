"""t_94e91559: hot cron runtime fields live in ``cron/runtime_state.db``; ``jobs.json`` is a
coalesced, read-only mirror of the merged view (rewritten at most once per
``HERMES_CRON_MIRROR_INTERVAL_S``, never read back for a job that has a row).

Real store under a temp HERMES_HOME throughout (no mocks of the store): the assertions are on the
actual bytes/mtime of jobs.json and the actual rows in the database.
"""

import json
import os
import sqlite3
import threading
import time

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    monkeypatch.delenv("HERMES_CRON_MIRROR_INTERVAL_S", raising=False)
    (tmp_path / "cron").mkdir()
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """Controls the mirror window clock (cron.runtime_state._now)."""
    from cron import runtime_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs, "_now", lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def _clean_running_state():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


def _jobs_file(home):
    return home / "cron" / "jobs.json"


def _db(home):
    return home / "cron" / "runtime_state.db"


def _fixture_job(i, *, with_runtime):
    """One realistic recurring job record (definition + optionally the runtime fields a live
    store carries)."""
    job = {
        "id": f"job{i:04d}abcd",
        "name": f"fixture-{i}",
        "prompt": "Summarise the overnight alerts and post the digest. " * 4,
        "skills": [], "skill": None, "model": None, "provider": None, "base_url": None,
        "script": f"fixture_{i}.py", "no_agent": True, "monitor_script": None,
        "monitor_url": None, "context_from": None,
        "schedule": {"kind": "interval", "minutes": 5 + i % 55, "display": f"every {5 + i % 55}m"},
        "schedule_display": f"every {5 + i % 55}m",
        "repeat": {"times": None},
        "enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None,
        "created_at": "2026-10-01T00:00:00-07:00", "deliver": "local", "origin": None,
        "enabled_toolsets": None, "workdir": None,
    }
    if with_runtime:
        job["repeat"]["completed"] = i * 3
        job.update({
            "next_run_at": "2026-10-24T20:00:00-07:00",
            "last_run_at": "2026-10-24T19:55:00-07:00",
            "last_status": "ok" if i % 7 else "error",
            "last_error": None if i % 7 else f"boom {i}",
            "last_delivery_error": None,
            "last_delivery_unverified": None,
            "failure_streak": 0 if i % 7 else 2,
            "error_repeat_streak": 0,
            "fire_claim": None,
            "monitor_state": None,
            "last_dispatch": {"scheduled_at": "2026-10-24T19:55:00-07:00",
                              "dispatched_at": "2026-10-24T19:55:01-07:00",
                              "lateness_seconds": 1.0, "kind": "on_time"},
        })
    return job


def _write_store(home, jobs):
    _jobs_file(home).write_text(json.dumps({"jobs": jobs, "updated_at": "x"}, indent=2),
                                encoding="utf-8")


def _snapshot(path):
    st = path.stat()
    return st.st_mtime_ns, st.st_ino, path.read_bytes()


# --- 1: runtime touches never read or write jobs.json --------------------------------------

def test_700_job_store_heartbeats_and_marks_leave_jobs_json_untouched(temp_home, clock):
    """50 heartbeat_fire_claim + 20 mark_job_run on a 700-job store inside one mirror window:
    jobs.json mtime/inode/bytes unchanged, exactly 70 row upserts."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    _write_store(temp_home, [_fixture_job(i, with_runtime=True) for i in range(700)])
    before_adopt = _snapshot(_jobs_file(temp_home))
    cj.load_jobs()  # one-shot adoption of the file's runtime into rows happens here
    assert _snapshot(_jobs_file(temp_home)) == before_adopt  # adoption never rewrites the file
    ids = [f"job{i:04d}abcd" for i in range(20)]
    owners = {}
    for jid in ids:
        assert cj.claim_job_for_fire(jid) is True
        owners[jid] = cj.get_job(jid)["fire_claim"]["by"]
    before = _snapshot(_jobs_file(temp_home))
    start = rs.UPSERT_COUNT

    for n in range(50):
        jid = ids[n % len(ids)]
        assert cj.heartbeat_fire_claim(jid, expected_owner=owners[jid]) is True
    for jid in ids:
        assert cj.mark_job_run(jid, True, expected_fire_owner=owners[jid]) is True

    assert rs.UPSERT_COUNT - start == 70
    assert _snapshot(_jobs_file(temp_home)) == before
    rec = cj.get_job(ids[0])
    assert rec["last_status"] == "ok" and rec["fire_claim"] is None
    assert rec["repeat"] == {"times": None, "completed": 1}


def test_due_scan_pending_slot_stamp_does_not_rewrite_jobs_json(temp_home, clock):
    """A tick that only stamps pending_slot / last_dispatch writes rows, not the file."""
    from cron import jobs as cj

    job = _fixture_job(1, with_runtime=False)
    job["next_run_at"] = "2020-01-01T00:00:00+00:00"
    _write_store(temp_home, [job, _fixture_job(2, with_runtime=False)])
    cj.load_jobs()
    before = _snapshot(_jobs_file(temp_home))
    cj.get_due_jobs()
    assert _snapshot(_jobs_file(temp_home)) == before
    rec = cj.get_job(job["id"])
    assert rec.get("last_dispatch") or rec.get("next_run_at") != "2020-01-01T00:00:00+00:00"


def test_definition_edit_writes_merged_view_immediately(temp_home, clock):
    """A definition save writes jobs.json at once as definitions + current rows, and resets the
    mirror window (nothing left dirty)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="edit-me")
    assert cj.claim_job_for_fire(created["id"]) is True
    cj.update_job(created["id"], {"name": "edited"})
    on_disk = json.loads(_jobs_file(temp_home).read_text(encoding="utf-8"))["jobs"][0]
    rec = cj.get_job(created["id"])
    assert on_disk["name"] == "edited"
    assert on_disk["fire_claim"] == rec["fire_claim"] and rec["fire_claim"]["by"]
    assert on_disk["next_run_at"] == rec["next_run_at"]
    with rs.connect(temp_home / "cron") as conn:
        assert rs.dirty_since(conn) is None


def test_remove_job_deletes_its_row(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="rm")
    assert cj.claim_job_for_fire(created["id"]) is True
    assert cj.remove_job(created["id"]) is True
    rows = sqlite3.connect(_db(temp_home)).execute("SELECT job_id FROM job_runtime").fetchall()
    assert rows == []


# --- 2: loss-of-claim semantics -----------------------------------------------------------

def test_two_owners_heartbeat_same_claim_exactly_one_keeps_it(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="race")
    jid = created["id"]
    assert cj.claim_job_for_fire(jid) is True
    owner_a = cj.get_job(jid)["fire_claim"]["by"]
    # Owner B recovers the claim (stale-lease reclaim: TTL 0), minting a fresh per-acquisition token.
    assert cj.claim_job_for_fire(jid, claim_ttl_seconds=0) is True
    owner_b = cj.get_job(jid)["fire_claim"]["by"]
    assert owner_a != owner_b

    results = {"a": [], "b": []}

    def beat(tag, owner):
        for _ in range(25):
            results[tag].append(cj.heartbeat_fire_claim(jid, expected_owner=owner))

    threads = [threading.Thread(target=beat, args=("a", owner_a)),
               threading.Thread(target=beat, args=("b", owner_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["a"] == [False] * 25
    assert results["b"] == [True] * 25
    assert cj.get_job(jid)["fire_claim"]["by"] == owner_b
    # The owner column the CAS keys on agrees with the claim.
    col = sqlite3.connect(_db(temp_home)).execute(
        "SELECT fire_claim_owner FROM job_runtime WHERE job_id=?", (jid,)).fetchone()[0]
    assert col == owner_b


def test_loser_run_is_fenced_out_by_real_heartbeat(temp_home, monkeypatch):
    """No stubbed heartbeat: another owner re-takes the claim mid-run, the REAL heartbeat thread
    sees the owner-guarded UPDATE miss twice, latches the cancel, and the loser's
    ``_abort_if_fire_claim_lost`` path fires (nothing delivered, no terminal write)."""
    import cron.scheduler as sched
    from cron.jobs import _with_job, claim_job_for_fire, create_job, get_job, save_jobs

    job = create_job(prompt="x", schedule="every 5m", name="loser")
    assert claim_job_for_fire(job["id"]) is True
    job = get_job(job["id"])
    delivered, aborted = [], []

    def fake_run_job(job, **kwargs):
        def re_own(jobs, _i, rec):
            rec["fire_claim"] = {**rec["fire_claim"], "by": "replacement:deadbeef"}
            save_jobs(jobs)

        _with_job(job["id"], re_own)
        cancel = kwargs["cancel_event"]
        deadline = time.monotonic() + 30
        while not cancel.is_set():
            assert time.monotonic() < deadline, "real heartbeat never latched the loss"
            time.sleep(0.01)
        # Same contract as run_job's _abort_if_fire_claim_lost.
        aborted.append(True)
        raise RuntimeError(f"Cron job '{job['name']}' lost its durable fire claim ownership")

    monkeypatch.setattr(sched, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(sched, "_FIRE_CLAIM_MISS_CONFIRM_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "_deliver_result", lambda job, content, **kw: delivered.append(content))

    sched.run_one_job(job)

    assert aborted == [True]
    assert delivered == []
    rec = get_job(job["id"])
    assert rec["fire_claim"]["by"] == "replacement:deadbeef"
    assert rec["last_status"] is None


# --- 3: migration round-trip ---------------------------------------------------------------

def test_migration_round_trip_and_export(temp_home):
    from cron import jobs as cj

    originals = [_fixture_job(i, with_runtime=True) for i in range(40)]
    originals[3]["run_claim"] = None  # absent vs None must both survive
    _write_store(temp_home, originals)

    before = _snapshot(_jobs_file(temp_home))
    loaded = cj.load_jobs()
    assert loaded == originals  # field-for-field, through the table
    assert _snapshot(_jobs_file(temp_home)) == before  # the file already IS the merged view
    rows = dict(sqlite3.connect(_db(temp_home)).execute("SELECT job_id, fields FROM job_runtime"))
    assert len(rows) == 40 and json.loads(rows[originals[3]["id"]])["run_claim"] is None

    # Second load: no-op (rows exist, nothing re-adopted, file untouched).
    from cron import runtime_state as rs
    start = rs.UPSERT_COUNT
    assert cj.load_jobs() == originals
    assert rs.UPSERT_COUNT == start
    assert _snapshot(_jobs_file(temp_home)) == before

    assert cj.export_runtime_to_json() == 40
    assert not _db(temp_home).exists()
    restored = json.loads(_jobs_file(temp_home).read_text(encoding="utf-8"))["jobs"]
    assert restored == originals  # dict equality: modulo key order (and top-level updated_at)


def test_existing_row_wins_over_runtime_put_back_into_the_file(temp_home):
    """A git level that restores runtime fields into jobs.json must not roll live state back."""
    from cron import jobs as cj

    job = _fixture_job(1, with_runtime=True)
    _write_store(temp_home, [job])
    cj.load_jobs()
    assert cj.claim_job_for_fire(job["id"]) is True
    live_claim = cj.get_job(job["id"])["fire_claim"]
    _write_store(temp_home, [job])  # stale snapshot with fire_claim=None
    assert cj.get_job(job["id"])["fire_claim"] == live_claim


def test_kill_switch_json_folds_rows_back(temp_home, monkeypatch):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="ks")
    assert cj.claim_job_for_fire(created["id"]) is True
    monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "json")
    rec = cj.get_job(created["id"])
    assert rec["fire_claim"]["by"]
    assert not _db(temp_home).exists()
    on_disk = json.loads(_jobs_file(temp_home).read_text())["jobs"][0]
    assert on_disk["fire_claim"]["by"] == rec["fire_claim"]["by"]
    # json mode keeps today's behaviour: a heartbeat rewrites the file.
    before = _snapshot(_jobs_file(temp_home))
    time.sleep(0.01)
    assert cj.heartbeat_fire_claim(created["id"], expected_owner=rec["fire_claim"]["by"]) is True
    assert _snapshot(_jobs_file(temp_home)) != before


def test_cli_runtime_export(temp_home, capsys):
    from cron import jobs as cj
    from hermes_cli.cron import cron_runtime_export

    created = cj.create_job(prompt="x", schedule="every 5m", name="cli")
    assert cj.claim_job_for_fire(created["id"]) is True
    assert cron_runtime_export(None) == 0  # --to-json alone: fold, keep the db
    assert "1 job(s)" in capsys.readouterr().out
    assert _db(temp_home).exists()

    class A:
        retire, drop_orphans = True, False
    assert cron_runtime_export(A()) == 0
    assert "removed runtime_state.db" in capsys.readouterr().out
    assert not _db(temp_home).exists()
    assert json.loads(_jobs_file(temp_home).read_text())["jobs"][0]["fire_claim"]["by"]


def test_merged_jobs_out_of_process_view_is_pre_split_shape_and_read_only(temp_home):
    """Monitors that read jobs.json directly get the old shape via runtime_state.merged_jobs,
    loaded BY PATH with no hermes imports, and it never writes."""
    import importlib.util
    import subprocess
    import sys

    from cron import jobs as cj

    originals = [_fixture_job(i, with_runtime=True) for i in range(5)]
    _write_store(temp_home, originals)
    cj.load_jobs()
    before = (_snapshot(_jobs_file(temp_home)), _db(temp_home).read_bytes())
    spec = importlib.util.spec_from_file_location("rs_by_path", cj._rs.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.merged_jobs(_jobs_file(temp_home)) == originals
    assert (_snapshot(_jobs_file(temp_home)), _db(temp_home).read_bytes()) == before
    # Bare interpreter, no repo on sys.path: stdlib-only import holds.
    code = ("import importlib.util,json,sys;s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(json.dumps(m.merged_jobs(sys.argv[2])[1]['last_status']))")
    out = subprocess.run([sys.executable, "-I", "-c", code, cj._rs.__file__,
                          str(_jobs_file(temp_home))], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == originals[1]["last_status"]


def test_replace_save_drops_orphan_rows(temp_home):
    from cron import jobs as cj

    a = cj.create_job(prompt="x", schedule="every 5m", name="a")
    b = cj.create_job(prompt="x", schedule="every 5m", name="b")
    assert cj.claim_job_for_fire(a["id"]) and cj.claim_job_for_fire(b["id"])
    cj.save_jobs([j for j in cj.load_jobs() if j["id"] == a["id"]], replace=True,
                 prune_runtime=True)
    ids = {r[0] for r in sqlite3.connect(_db(temp_home)).execute("SELECT job_id FROM job_runtime")}
    assert ids == {a["id"]}


# --- Prism round 1 (#2287): durability, permissions, export safety, repair, reconciliation -----

def test_db_and_sidecars_created_0600_under_umask_022_by_first_save(temp_home):
    from cron import jobs as cj

    old = os.umask(0o022)
    try:
        cj.create_job(prompt="x", schedule="every 5m", name="perm")
        cj.save_jobs([{"id": "j", "manual_run_prompt": "private"}])
    finally:
        os.umask(old)
    base = _db(temp_home)
    for suffix in ("", "-wal", "-shm"):
        path = type(base)(str(base) + suffix)
        if path.exists():
            assert oct(os.stat(path).st_mode & 0o777) == "0o600", path


def test_runtime_connections_use_synchronous_full(temp_home):
    from cron import runtime_state as rs

    with rs.connect(temp_home / "cron") as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_export_drops_file_runtime_keys_absent_from_row(temp_home):
    from cron import jobs as cj
    from cron import runtime_state as rs

    job = _fixture_job(1, with_runtime=False)
    _write_store(temp_home, [job])
    cj.load_jobs()
    rs.write_rows(temp_home / "cron", {job["id"]: {"last_status": "ok"}})
    stale = dict(job, manual_run_at="2026-01-01T00:00:00+00:00")
    _write_store(temp_home, [stale])  # file regained a cleared runtime key out of band
    cj.export_runtime_to_json(jobs=[stale])
    on_disk = json.loads(_jobs_file(temp_home).read_text())["jobs"][0]
    assert "manual_run_at" not in on_disk and on_disk["last_status"] == "ok"


def test_export_with_empty_row_still_strips(temp_home):
    from cron import jobs as cj
    from cron import runtime_state as rs

    job = _fixture_job(1, with_runtime=False)
    _write_store(temp_home, [job])
    cj.load_jobs()
    rs.write_rows(temp_home / "cron", {job["id"]: {}})
    stale = dict(job, fire_claim={"at": "x", "by": "dead"})
    cj.export_runtime_to_json(jobs=[stale])
    assert "fire_claim" not in json.loads(_jobs_file(temp_home).read_text())["jobs"][0]


@pytest.mark.parametrize("payload", [None, "{}", '{"jobs": 3}'], ids=["missing", "no-jobs", "bad-jobs"])
def test_export_refuses_missing_or_jobless_definitions(temp_home, payload):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="keep")
    assert cj.claim_job_for_fire(created["id"]) is True
    if payload is None:
        _jobs_file(temp_home).unlink()
    else:
        _jobs_file(temp_home).write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing runtime export"):
        cj.export_runtime_to_json()
    assert _db(temp_home).exists()
    rows = sqlite3.connect(_db(temp_home)).execute("SELECT job_id FROM job_runtime").fetchall()
    assert rows == [(created["id"],)]


def test_heartbeat_fast_path_honours_live_store_test_guard(temp_home, monkeypatch):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="guard")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]

    def refuse(_path):
        raise RuntimeError("Refusing to write the cron store to a NON-TEMP home")

    monkeypatch.setattr(cj, "_guard_against_test_write_to_live_store", refuse)
    with pytest.raises(RuntimeError, match="NON-TEMP"):
        cj.heartbeat_fire_claim(created["id"], expected_owner=owner)


def test_bare_list_repair_preserves_runtime_rows(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="repair")
    assert cj.mark_job_run(created["id"], True) is True
    before = cj.get_job(created["id"])
    defs = json.loads(_jobs_file(temp_home).read_text())["jobs"]
    _jobs_file(temp_home).write_text(json.dumps(defs), encoding="utf-8")  # bare list
    after = cj.load_jobs()[0]
    assert after["last_run_at"] == before["last_run_at"] and after["last_status"] == "ok"
    assert after["repeat"].get("completed") == before["repeat"].get("completed")


def test_unlocked_load_retries_when_definitions_change_between_reads(temp_home, monkeypatch):
    """A runtime-export landing between the definitions read and the row read must not yield
    jobs without their runtime: the unlocked load sees the stamp move and retries locked."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="race")
    assert cj.claim_job_for_fire(created["id"]) is True
    real_read = cj._read_runtime_rows
    fired = []

    def export_then_read(*a, **kw):
        if not fired:
            fired.append(True)
            monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "sqlite")
            cj.export_runtime_to_json()  # file regains runtime, db removed
        return real_read(*a, **kw)

    monkeypatch.setattr(cj, "_read_runtime_rows", export_then_read)
    job = next(j for j in cj.load_jobs() if j["id"] == created["id"])
    assert job["fire_claim"]["by"]


def test_two_phase_save_crash_before_rename_is_discarded(temp_home, monkeypatch):
    """Definition + runtime change in one save; the jobs.json rename fails: the old definitions
    stay, the staged runtime is discarded, and the old runtime is intact."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="2pc")
    for _ in range(3):
        cj.mark_job_run(created["id"], True)
    jid = created["id"]

    def boom(*a, **kw):
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(cj, "atomic_replace", boom)
        with pytest.raises(OSError):
            with cj._jobs_lock():
                jobs = cj.load_jobs()
                jobs[0]["schedule"] = {"kind": "interval", "minutes": 7, "display": "every 7m"}
                jobs[0]["repeat"]["completed"] = 0
                cj.save_jobs(jobs)
    job = cj.get_job(jid)
    assert job["schedule"]["minutes"] == 5 and job["repeat"]["completed"] == 3
    assert sqlite3.connect(_db(temp_home)).execute(
        "SELECT count(*) FROM pending_runtime").fetchone()[0] == 0


def test_two_phase_save_crash_after_rename_rolls_forward(temp_home, monkeypatch):
    """Crash between the jobs.json rename and the row apply: next load pairs the new schedule
    with the new counter (rolled forward from the pending generation the file names)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="2pc-fwd")
    for _ in range(3):
        cj.mark_job_run(created["id"], True)
    jid = created["id"]

    def die(*a, **kw):
        raise SystemExit("killed")

    with monkeypatch.context() as mp:
        mp.setattr(rs, "apply_pending", die)
        with pytest.raises(SystemExit):
            with cj._jobs_lock():
                jobs = cj.load_jobs()
                jobs[0]["schedule"] = {"kind": "interval", "minutes": 7, "display": "every 7m"}
                jobs[0]["repeat"]["completed"] = 0
                cj.save_jobs(jobs)
    job = cj.get_job(jid)
    assert job["schedule"]["minutes"] == 7 and job["repeat"]["completed"] == 0
    assert sqlite3.connect(_db(temp_home)).execute(
        "SELECT count(*) FROM pending_runtime").fetchone()[0] == 0


def test_fast_path_row_write_reconciles_concurrent_sibling(temp_home, monkeypatch):
    """Degraded-lock sibling writes last_status between our row read and row write; our update to
    an unrelated key must not revert it."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="recon")
    jid = created["id"]
    cj.mark_job_run(jid, False, "old error")
    real_read = cj._read_one_row

    def read_then_sibling(job_id):
        row = real_read(job_id)
        sibling = dict(row, last_status="ok", last_error=None)
        rs.write_rows(temp_home / "cron", {jid: sibling})
        return row

    with monkeypatch.context() as mp:
        mp.setattr(cj, "_read_one_row", read_then_sibling)
        cj.update_job(jid, {"last_delivery_error": "late failure"})
    job = cj.get_job(jid)
    assert job["last_delivery_error"] == "late failure"
    assert job["last_status"] == "ok" and job["last_error"] is None


def test_profile_export_folds_runtime_into_scrubbed_json(temp_home, tmp_path):
    from cron import runtime_state as rs

    cron = tmp_path / "staged" / "cron"
    cron.mkdir(parents=True)
    job = _fixture_job(1, with_runtime=False)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    rs.write_rows(cron, {job["id"]: {"manual_run_prompt": "ghp_secretsecret"}})
    assert rs.fold_staged_copy(cron) is True
    assert not any(p.name.startswith("runtime_state.db") for p in cron.iterdir())
    assert json.loads((cron / "jobs.json").read_text())["jobs"][0]["manual_run_prompt"] == \
        "ghp_secretsecret"


def test_quick_snapshot_lists_runtime_db():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/runtime_state.db" in _QUICK_STATE_FILES


# --- Mirror (#2287 review 21:3x): jobs.json is a coalesced read-only mirror of the merged view ---

def _raw(home, jid):
    return next(j for j in json.loads(_jobs_file(home).read_text())["jobs"] if j["id"] == jid)


def test_mirror_coalesces_heartbeats_into_one_write_at_window_boundary(temp_home, clock):
    """(a) 50 heartbeats inside one window -> 0 jobs.json writes; then exactly 1 at the boundary."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="mirror")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]
    assert cj.maybe_flush_runtime_mirror(force=True) is True  # start from a clean mirror
    before, writes = _snapshot(_jobs_file(temp_home)), rs.MIRROR_WRITES
    for _ in range(50):
        clock[0] += 0.5  # 25 s total: still inside the 30 s window
        assert cj.heartbeat_fire_claim(created["id"], expected_owner=owner) is True
    assert rs.MIRROR_WRITES == writes and _snapshot(_jobs_file(temp_home)) == before
    clock[0] += 6  # window boundary passes
    assert cj.maybe_flush_runtime_mirror() is True
    assert rs.MIRROR_WRITES == writes + 1
    assert cj.maybe_flush_runtime_mirror() is False  # nothing changed since: no write
    assert rs.MIRROR_WRITES == writes + 1


def test_mirror_raw_read_after_boundary_equals_rows(temp_home, clock):
    """(b) after the boundary a raw json.load(jobs.json) shows the row's last_run_at/next_run_at."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="raw")
    assert cj.mark_job_run(created["id"], True) is True
    rec = cj.get_job(created["id"])
    clock[0] += 31
    cj.get_due_jobs()  # the scheduler tick's section exit flushes the mirror
    raw = _raw(temp_home, created["id"])
    assert raw["last_run_at"] == rec["last_run_at"] and raw["last_run_at"]
    assert raw["next_run_at"] == cj.get_job(created["id"])["next_run_at"]
    assert raw["last_status"] == "ok" and raw["repeat"].get("completed") == 1


def test_cas_ignores_stale_claim_planted_in_mirror(temp_home):
    """(c) a different owner planted in the mirror file does not decide the CAS; the row does."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="cas")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]
    data = json.loads(_jobs_file(temp_home).read_text())
    data["jobs"][0]["fire_claim"] = {"at": "2026-10-24T00:00:00+00:00", "by": "intruder:1"}
    _jobs_file(temp_home).write_text(json.dumps(data), encoding="utf-8")
    assert cj.heartbeat_fire_claim(created["id"], expected_owner="intruder:1") is False
    assert cj.heartbeat_fire_claim(created["id"], expected_owner=owner) is True
    assert cj.get_job(created["id"])["fire_claim"]["by"] == owner


def test_git_level_with_older_runtime_loses_to_row_and_mirror_restores(temp_home, clock):
    """(d) a git level rewrites jobs.json with older runtime: load_jobs returns the row's values,
    and the next mirror write restores the file."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="level")
    jid = created["id"]
    cj.mark_job_run(jid, False, "old")
    cj.maybe_flush_runtime_mirror(force=True)
    old_file = _jobs_file(temp_home).read_text()  # merged view after the failed run
    cj.mark_job_run(jid, True)
    assert cj.maybe_flush_runtime_mirror(force=True) is True
    live = cj.get_job(jid)
    _jobs_file(temp_home).write_text(old_file, encoding="utf-8")  # the level (new inode)
    assert json.loads(old_file)["jobs"][0]["last_status"] == "error"
    assert cj.get_job(jid)["last_status"] == "ok"  # the row wins
    assert cj.get_job(jid)["last_run_at"] == live["last_run_at"]
    assert _raw(temp_home, jid)["last_status"] == "error"  # readers alone never write
    cj.get_due_jobs()  # next tick: the replaced file is detected and rewritten (no window wait)
    raw = _raw(temp_home, jid)
    assert raw["last_status"] == "ok" and raw["last_run_at"] == live["last_run_at"]


def test_mirror_interval_zero_writes_on_every_touch(temp_home, monkeypatch):
    from cron import jobs as cj
    from cron import runtime_state as rs

    monkeypatch.setenv("HERMES_CRON_MIRROR_INTERVAL_S", "0")
    created = cj.create_job(prompt="x", schedule="every 5m", name="zero")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]
    writes = rs.MIRROR_WRITES
    for _ in range(3):
        assert cj.heartbeat_fire_claim(created["id"], expected_owner=owner) is True
    assert rs.MIRROR_WRITES == writes + 3
    assert _raw(temp_home, created["id"])["fire_claim"] == cj.get_job(created["id"])["fire_claim"]


def test_stale_definition_save_keeps_sibling_row_run_state(temp_home, monkeypatch):
    """sqlite twin of test_cron_store_concurrency's instance-3 shape: under a degraded lock a
    sibling records a run (row) and a definition change (file) while we hold a stale snapshot;
    our unrelated definition edit must not revert the sibling's run state, in the row or the
    mirror."""
    import cron.jobs as cj
    from cron import runtime_state as rs

    monkeypatch.setattr(cj, "fcntl", None)
    monkeypatch.setattr(cj, "msvcrt", None)
    created = cj.create_job(prompt="x", schedule="every 5m", name="stale")
    jid = created["id"]
    with cj._jobs_lock():
        stale = cj.load_jobs()
        assert _raw(temp_home, jid).get("last_run_at") is None
        row = rs.read_all(temp_home / "cron")[jid]
        rs.write_rows(temp_home / "cron", {jid: dict(
            row, last_run_at="2026-09-20T16:07:28+00:00", last_status="ok")})
        data = json.loads(_jobs_file(temp_home).read_text())
        data["jobs"][0]["state"] = "completed"
        _jobs_file(temp_home).write_text(json.dumps(data), encoding="utf-8")
        stale[0]["paused_reason"] = "edited concurrently"
        cj.save_jobs(stale)
    rec = cj.get_job(jid)
    assert rec["last_run_at"] == "2026-09-20T16:07:28+00:00" and rec["last_status"] == "ok"
    assert rec["state"] == "completed" and rec["paused_reason"] == "edited concurrently"
    raw = _raw(temp_home, jid)
    assert raw["last_run_at"] == rec["last_run_at"] and raw["paused_reason"] == "edited concurrently"


# --- Prism round 2 (#2287 @ 87228725) ------------------------------------------------------

def _stage_crashed_generation(home, jid, field, value):
    """Simulate a save that crashed after renaming jobs.json (naming gen G) but before applying
    G's staged rows."""
    from cron import runtime_state as rs

    gen = "g" * 32
    with rs.transaction(home / "cron") as conn:
        row = rs.read_row(conn, jid) or {}
        rs.stage_pending(conn, gen, {jid: dict(row, **{field: value})})
    data = json.loads(_jobs_file(home).read_text())
    data["runtime_gen"] = gen
    _jobs_file(home).write_text(json.dumps(data), encoding="utf-8")
    return gen


def test_export_rolls_forward_pending_generation_first(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="pend-exp")
    cj.mark_job_run(created["id"], True)
    _stage_crashed_generation(temp_home, created["id"], "next_run_at", "2099-01-01T00:00:00+00:00")
    cj.export_runtime_to_json()
    assert _raw(temp_home, created["id"])["next_run_at"] == "2099-01-01T00:00:00+00:00"


def test_save_recovers_sibling_pending_generation_before_writing(temp_home):
    from cron import jobs as cj

    a = cj.create_job(prompt="x", schedule="every 5m", name="a")
    b = cj.create_job(prompt="x", schedule="every 5m", name="b")
    with cj._jobs_lock():
        snapshot = cj.load_jobs()
        _stage_crashed_generation(temp_home, a["id"], "last_status", "sibling")
        for job in snapshot:
            if job["id"] == b["id"]:
                job["name"] = "b2"
        cj.save_jobs(snapshot)
    assert cj.get_job(a["id"])["last_status"] == "sibling"
    assert cj.get_job(b["id"])["name"] == "b2"


def test_runtime_only_save_refreshes_baseline(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="base")
    cj.mark_job_run(created["id"], True)
    with cj._jobs_lock():
        jobs = cj.load_jobs()
        jobs[0]["last_status"] = "error"
        cj.save_jobs(jobs)
        jobs[0]["last_status"] = "ok"
        cj.save_jobs(jobs)
    assert cj.get_job(created["id"])["last_status"] == "ok"


def test_stage_and_apply_touch_only_their_generation(temp_home):
    from cron import runtime_state as rs

    cron = temp_home / "cron"
    with rs.transaction(cron) as conn:
        rs.stage_pending(conn, "gA", {"j1": {"last_status": "a"}})
        rs.stage_pending(conn, "gB", {"j2": {"last_status": "b"}})
        assert sorted(rs.pending_gens(conn)) == ["gA", "gB"]
        rs.apply_pending(conn, "gA")
        assert rs.pending_gens(conn) == ["gB"]
        rs.apply_pending(conn, "gB")
        assert rs._rows_of(conn) == {"j1": {"last_status": "a"}, "j2": {"last_status": "b"}}


@pytest.mark.parametrize("payload", [None, "{}", '{"jobs": 3}'], ids=["missing", "no-jobs", "bad"])
def test_fold_staged_copy_refuses_without_definitions(temp_home, tmp_path, payload):
    from cron import runtime_state as rs

    cron = tmp_path / "staged" / "cron"
    cron.mkdir(parents=True)
    rs.write_rows(cron, {"j": {"last_status": "ok"}})
    if payload is not None:
        (cron / "jobs.json").write_text(payload, encoding="utf-8")
    assert rs.fold_staged_copy(cron) is False
    assert rs.db_path(cron).exists()


def test_replace_save_is_authoritative_for_runtime(temp_home):
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="repl")
    cj.mark_job_run(created["id"], True)
    with cj._jobs_lock():
        snapshot = cj.load_jobs()
        row = rs.read_all(temp_home / "cron")[created["id"]]
        rs.write_rows(temp_home / "cron", {created["id"]: dict(row, last_status="error")})
        cj.save_jobs(snapshot, replace=True)
    assert cj.get_job(created["id"])["last_status"] == "ok"


def test_post_rename_failure_rolls_generation_forward(temp_home, monkeypatch):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="post")
    for _ in range(3):
        cj.mark_job_run(created["id"], True)

    def boom(*a, **kw):
        raise KeyboardInterrupt

    with monkeypatch.context() as mp:
        mp.setattr(cj, "fsync_directory", boom)
        with pytest.raises(KeyboardInterrupt):
            with cj._jobs_lock():
                jobs = cj.load_jobs()
                jobs[0]["schedule"] = {"kind": "interval", "minutes": 7, "display": "every 7m"}
                jobs[0]["repeat"]["completed"] = 0
                cj.save_jobs(jobs)
    job = cj.get_job(created["id"])
    assert job["schedule"]["minutes"] == 7 and job["repeat"]["completed"] == 0


@pytest.mark.parametrize("payload", ["{}", '{"jobs": 3}'], ids=["no-jobs", "bad"])
def test_kill_switch_refuses_export_of_jobless_payload(temp_home, monkeypatch, payload):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="ks-refuse")
    cj.mark_job_run(created["id"], True)
    _jobs_file(temp_home).write_text(payload, encoding="utf-8")
    monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "json")
    with pytest.raises(RuntimeError, match="refusing runtime export"):
        cj.load_jobs()
    assert _db(temp_home).exists()


def test_quick_snapshot_copies_jobs_and_runtime_db_under_one_cron_lock(temp_home, monkeypatch):
    """Prism b493dcb2080d: jobs.json and runtime_state.db are copied inside ONE cron store lock
    section, so a remove_job cannot land between the two copies."""
    import shutil

    from cron import jobs as cj
    from hermes_cli import backup

    created = cj.create_job(prompt="x", schedule="every 5m", name="snap")
    cj.mark_job_run(created["id"], True)
    held = {}
    real_copy2, real_db = shutil.copy2, backup._safe_copy_db

    def copy2(src, dst, *a, **kw):
        if str(src).endswith("cron/jobs.json"):
            held["jobs"] = getattr(cj._jobs_lock_state, "depth", 0)
        return real_copy2(src, dst, *a, **kw)

    def safe_copy_db(src, dst):
        if str(src).endswith("cron/runtime_state.db"):
            held["db"] = getattr(cj._jobs_lock_state, "depth", 0)
            held["section"] = id(cj._jobs_lock_state)
        return real_db(src, dst)

    monkeypatch.setattr(backup.shutil, "copy2", copy2)
    monkeypatch.setattr(backup, "_safe_copy_db", safe_copy_db)
    assert backup.create_quick_snapshot(hermes_home=temp_home)
    assert held.get("jobs", 0) >= 1 and held.get("db", 0) >= 1


def test_runtime_paths_on_missing_store_return_false(temp_home):
    """No jobs.json at all: the single-job fast path falls back cleanly (no unpack error)."""
    from cron import jobs as cj

    assert not _jobs_file(temp_home).exists()
    assert cj.heartbeat_fire_claim("nope", expected_owner="x") is False
    assert cj.heartbeat_run_claim("nope", expected_owner="x") is False
    assert cj.claim_job_for_fire("nope") is False


# --- 4: fresh profile ----------------------------------------------------------------------

def test_load_jobs_on_fresh_store_creates_table(temp_home):
    from cron import jobs as cj

    _write_store(temp_home, [_fixture_job(1, with_runtime=False)])
    assert not _db(temp_home).exists()
    assert [j["id"] for j in cj.load_jobs()] == ["job0001abcd"]
    assert _db(temp_home).exists()
    con = sqlite3.connect(_db(temp_home))
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("SELECT count(*) FROM job_runtime").fetchone()[0] == 0
    assert oct(os.stat(_db(temp_home)).st_mode & 0o777) == "0o600"


# --- 5: bench ------------------------------------------------------------------------------

@pytest.mark.slow_test(
    60,
    reason="t_94e91559 bench: 200 whole-file jobs.json rewrites (json baseline) then 200 row "
    "updates; measured 6.9 s + 0.9 s on ace-media under load1 ~25")
def test_bench_200_heartbeats_700_jobs_write_zero_bytes_to_jobs_json(temp_home):
    """700 jobs, 200 heartbeats.

    Measured on ace-media 2026-10-24 (load1 ~25/32 cpus), this test's fixture (1,102,095 B file):
      before (HERMES_CRON_RUNTIME_STATE=json): 6.85 s wall, 220,419,000 B written to jobs.json
        (every heartbeat a whole-file rewrite; the live 1.4 MB store would be ~290 MB);
      after (sqlite): 0.90 s wall, 0 B written to jobs.json (one row UPDATE per heartbeat).
    The test asserts the byte counts and prints both wall times.
    """
    from cron import jobs as cj

    def run(mode):
        os.environ["HERMES_CRON_RUNTIME_STATE"] = mode
        try:
            _write_store(temp_home, [_fixture_job(i, with_runtime=True) for i in range(700)])
            if _db(temp_home).exists():
                cj.export_runtime_to_json()
            cj.load_jobs()
            jid = "job0001abcd"
            assert cj.claim_job_for_fire(jid) is True
            owner = cj.get_job(jid)["fire_claim"]["by"]
            size, written = _jobs_file(temp_home).stat().st_size, 0
            ino = _jobs_file(temp_home).stat().st_ino
            t0 = time.perf_counter()
            for _ in range(200):
                assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
                st = _jobs_file(temp_home).stat()
                if st.st_ino != ino:
                    written += st.st_size
                    ino = st.st_ino
            return time.perf_counter() - t0, written, size
        finally:
            os.environ.pop("HERMES_CRON_RUNTIME_STATE", None)

    json_wall, json_bytes, size = run("json")
    sqlite_wall, sqlite_bytes, _ = run("sqlite")
    print(f"\nbench 700 jobs x 200 heartbeats: json {json_wall:.2f}s {json_bytes} B "
          f"(file {size} B); sqlite {sqlite_wall:.2f}s {sqlite_bytes} B")
    assert json_bytes >= 200 * size * 0.9  # baseline really rewrote the file each time
    assert sqlite_bytes == 0


# --- Apollo review of #2287 @ 7bab7b62 (Prism round 3 residue) -----------------------------

def test_profile_export_refuses_db_without_definitions(tmp_path, monkeypatch):
    """12e3f4b761ac: a runtime row carrying a credential plus a jobless jobs.json: the fold
    refuses, so the export must fail and write no archive (never ship the db unscrubbed)."""
    from cron import runtime_state as rs
    from hermes_cli import profiles

    profile_dir = tmp_path / "profiles" / "p"
    cron = profile_dir / "cron"
    cron.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model: x\n")
    (cron / "jobs.json").write_text("{}", encoding="utf-8")
    rs.write_rows(cron, {"j1": {"manual_run_prompt": "use ghp_" + "a" * 36}})
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda n: profile_dir)
    monkeypatch.setattr(profiles, "validate_profile_name", lambda n: None)
    out = tmp_path / "export.tar.gz"
    with pytest.raises(ValueError, match="runtime_state.db"):
        profiles.export_profile("p", str(out))
    assert not out.exists()
    assert rs.db_path(cron).exists()  # the live store is untouched


def test_invalid_jobs_field_repair_keeps_every_row(temp_home):
    """f4b938a70cd7: load_jobs' auto-repair of {"jobs": "invalid"} saves [] with replace=True;
    it must not run the orphan-row prune (the rows are the only copy of the runtime)."""
    from cron import jobs as cj

    a = cj.create_job(prompt="x", schedule="every 5m", name="a")
    b = cj.create_job(prompt="x", schedule="every 5m", name="b")
    cj.mark_job_run(a["id"], True)
    cj.mark_job_run(b["id"], False, "err")
    before = {r[0] for r in sqlite3.connect(_db(temp_home)).execute(
        "SELECT job_id FROM job_runtime")}
    assert before == {a["id"], b["id"]}
    _jobs_file(temp_home).write_text('{"jobs": "invalid"}', encoding="utf-8")
    assert cj.load_jobs() == []
    after = {r[0] for r in sqlite3.connect(_db(temp_home)).execute(
        "SELECT job_id FROM job_runtime")}
    assert after == before


def test_fold_and_merged_view_apply_pending_generation_the_file_names(temp_home, tmp_path):
    """64fce4a91b12: the file names generation g (new next_run_at), job_runtime is older, the
    pending row for g is new: merged_jobs and fold_staged_copy show the new value."""
    import shutil

    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="pend-fold")
    cj.mark_job_run(created["id"], True)
    new = "2099-01-01T00:00:00+00:00"
    _stage_crashed_generation(temp_home, created["id"], "next_run_at", new)
    assert rs.read_all(temp_home / "cron")[created["id"]].get("next_run_at") != new
    merged = rs.merged_jobs(_jobs_file(temp_home))
    assert merged[0]["next_run_at"] == new
    staged = tmp_path / "staged" / "cron"
    shutil.copytree(temp_home / "cron", staged)
    assert rs.fold_staged_copy(staged) is True
    assert json.loads((staged / "jobs.json").read_text())["jobs"][0]["next_run_at"] == new


def test_recovery_keeps_young_unnamed_generation_discards_old(temp_home, clock):
    """9a7350abcc1c: under a degraded lock a sibling's save may be between its stage and its
    rename. Recovery discards an unnamed generation only past max(2 x mirror interval, 60 s)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="young")
    cron = temp_home / "cron"
    with rs.transaction(cron) as conn:
        rs.stage_pending(conn, "s" * 32, {created["id"]: {"last_status": "sibling"}})

    def pending():
        with rs.connect(cron) as conn:
            return rs.pending_gens(conn)

    with cj._jobs_lock():
        cj._recover_pending_runtime(None)
    assert pending() == ["s" * 32]
    clock[0] += rs.pending_discard_after() - 1
    with cj._jobs_lock():
        cj._recover_pending_runtime(None)
    assert pending() == ["s" * 32]
    clock[0] += 2
    with cj._jobs_lock():
        cj._recover_pending_runtime(None)
    assert pending() == []
    assert rs.pending_discard_after() == 60.0  # default interval 30 s -> max(60, 60)


def test_definition_fast_path_save_reconciles_against_original_baseline(temp_home, monkeypatch):
    """280901e0a164: A's single-job edit renames J; while A's callback runs a degraded-lock
    sibling B pauses J; A then saves: J ends up paused AND renamed."""
    from cron import jobs as cj

    monkeypatch.setattr(cj, "fcntl", None)
    monkeypatch.setattr(cj, "msvcrt", None)
    created = cj.create_job(prompt="x", schedule="every 5m", name="orig")
    jid = created["id"]
    cj.maybe_flush_runtime_mirror(force=True)

    def edit(jobs, i, job):
        data = json.loads(_jobs_file(temp_home).read_text())  # sibling B: pause J
        for rec in data["jobs"]:
            if rec["id"] == jid:
                rec.update(enabled=False, state="paused", paused_reason="by B")
        _jobs_file(temp_home).write_text(json.dumps(data), encoding="utf-8")
        job["name"] = "edited"
        cj.save_jobs(jobs)
        return job

    cj._with_job(jid, edit)
    rec = cj.get_job(jid)
    assert rec["name"] == "edited"
    assert rec["enabled"] is False and rec["state"] == "paused" and rec["paused_reason"] == "by B"


def test_json_mode_save_with_live_db_logs_error_once(temp_home, monkeypatch, caplog):
    """7ffd35b7bd2a (accepted by-design): json mode with runtime_state.db still present logs ONE
    ERROR naming runtime-export."""
    import logging

    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="ks-warn")
    assert _db(temp_home).exists()
    monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "json")
    monkeypatch.setattr(cj, "_JSON_MODE_DB_WARNED", set())
    with caplog.at_level(logging.ERROR, logger=cj.logger.name):
        with cj._jobs_lock():
            jobs = json.loads(_jobs_file(temp_home).read_text())["jobs"]
            cj.save_jobs(jobs)
            cj.save_jobs(jobs)
    hits = [r for r in caplog.records if "runtime-export" in r.getMessage()
            and r.levelno == logging.ERROR]
    assert len(hits) == 1
    assert created["id"]


# --- Prism round 4 @ 35479f90 (Apollo send-back, run 38922) ---------------------------------

def test_two_phase_apply_keeps_sibling_row_write_between_stage_and_apply(temp_home, monkeypatch):
    """682ece27eb8b / 7bc8d98a18ae: A stages a definition + last_status change for J; a
    degraded-lock sibling B commits manual_run_at for J between A's stage and A's apply; A's
    apply must merge (B's key survives), not replace the whole row."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="a")
    jid = created["id"]
    cj.mark_job_run(jid, True)
    real_save = cj._save_jobs_unlocked

    def sibling_then_save(*a, **kw):
        with rs.transaction(temp_home / "cron") as conn:
            row = rs.read_row(conn, jid) or {}
            rs.upsert_rows(conn, {jid: dict(row, manual_run_at="2099-01-01T00:00:00+00:00")})
        return real_save(*a, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(cj, "_save_jobs_unlocked", sibling_then_save)
        with cj._jobs_lock():
            jobs = cj.load_jobs()
            jobs[0]["name"] = "renamed"
            jobs[0]["last_status"] = "error"
            cj.save_jobs(jobs)
    job = cj.get_job(jid)
    assert job["name"] == "renamed" and job["last_status"] == "error"
    assert job.get("manual_run_at") == "2099-01-01T00:00:00+00:00"


def test_recovery_roll_forward_merges_against_staging_base(temp_home):
    """7bc8d98a18ae: the crash-recovery roll-forward of a file-named generation merges too: a row
    write that landed after the stage survives unless the generation changed that key."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="rf")
    jid = created["id"]
    cj.mark_job_run(jid, True)
    gen = "h" * 32
    cron = temp_home / "cron"
    with rs.transaction(cron) as conn:
        base = rs.read_row(conn, jid)
        rs.stage_pending(conn, gen, {jid: dict(base, last_status="error")}, bases={jid: base})
    with rs.transaction(cron) as conn:  # sibling, after the stage
        rs.upsert_rows(conn, {jid: dict(base, manual_run_at="2099-01-01T00:00:00+00:00")})
    data = json.loads(_jobs_file(temp_home).read_text())
    data["runtime_gen"] = gen
    _jobs_file(temp_home).write_text(json.dumps(data), encoding="utf-8")
    assert rs.merged_jobs(_jobs_file(temp_home))[0]["manual_run_at"].startswith("2099")
    job = cj.get_job(jid)
    assert job["last_status"] == "error"
    assert job.get("manual_run_at") == "2099-01-01T00:00:00+00:00"


def test_fold_refuses_when_database_read_fails(temp_home, tmp_path):
    """1da94b741940: the jobs.json names a generation, job_runtime holds newer rows, the
    pending_runtime table is missing: the fold must refuse and keep the database, never fold the
    raw file view and delete the only copy of the rows."""
    import shutil

    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="unsafe-fold")
    cj.mark_job_run(created["id"], False, "newer error")
    staged = tmp_path / "staged" / "cron"
    shutil.copytree(temp_home / "cron", staged)
    data = json.loads((staged / "jobs.json").read_text())
    data["runtime_gen"] = "z" * 32
    for rec in data["jobs"]:
        rec["last_error"] = "older mirror value"
    (staged / "jobs.json").write_text(json.dumps(data), encoding="utf-8")
    con = sqlite3.connect(staged / rs.DB_NAME)
    con.execute("DROP TABLE pending_runtime")
    con.commit()
    con.close()
    assert rs.fold_staged_copy(staged) is False
    assert rs.db_path(staged).exists()
    assert json.loads((staged / "jobs.json").read_text())["jobs"][0]["last_error"] == \
        "older mirror value"  # not rewritten


def test_merged_jobs_reads_rows_and_pending_in_one_snapshot(temp_home, monkeypatch):
    """65a323e10656: an apply_pending that commits between merged_jobs' read of job_runtime and
    its read of pending_runtime must not make the committed update vanish (one read snapshot)."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="snap")
    jid = created["id"]
    cj.mark_job_run(jid, True)
    new = "2099-01-01T00:00:00+00:00"
    gen = _stage_crashed_generation(temp_home, jid, "next_run_at", new)
    real_connect = rs.sqlite3.connect
    fired = []

    class Conn:
        def __init__(self, inner):
            self._c = inner

        def execute(self, sql, *a):
            cur = self._c.execute(sql, *a)
            if "FROM job_runtime" in sql and not fired:
                rows = cur.fetchall()  # the job_runtime read is done; a writer applies now
                fired.append(1)
                with rs.transaction(temp_home / "cron") as w:
                    rs.apply_pending(w, gen)

                class _Done:
                    def fetchall(self_inner):
                        return rows
                return _Done()
            return cur

        def __getattr__(self, name):
            return getattr(self._c, name)

    def connect(*a, **kw):
        c = real_connect(*a, **kw)
        return Conn(c) if "mode=ro" in str(a[0] if a else "") else c

    monkeypatch.setattr(rs.sqlite3, "connect", connect)
    merged = rs.merged_jobs(_jobs_file(temp_home))
    assert fired
    assert merged[0]["next_run_at"] == new


def test_saved_definition_change_becomes_the_baseline(temp_home):
    """60b9541dae29: in one lock section, save a definition-only edit, then save the same list
    with the edit reverted: the revert must land (the baseline is what we saved, not the load)."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="orig")
    jid = created["id"]
    with cj._jobs_lock():
        jobs = cj.load_jobs()
        jobs[0]["name"] = "edited"
        cj.save_jobs(jobs)
        jobs[0]["name"] = "orig"
        cj.save_jobs(jobs)
    assert cj.get_job(jid)["name"] == "orig"


def test_claim_heartbeat_row_write_takes_permission_pass(temp_home, monkeypatch):
    """75ab1ed65c10: the heartbeat fast path writes through _runtime_txn, so a new db or sidecar
    it creates (a root CLI heartbeat) gets the 0600/ownership pass like every other row write."""
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="own")
    jid = created["id"]
    assert cj.claim_job_for_fire(jid) is True
    owner = cj.get_job(jid)["fire_claim"]["by"]
    calls = []
    real = cj._secure_runtime_db
    monkeypatch.setattr(cj, "_secure_runtime_db", lambda: (calls.append(1), real())[1])
    monkeypatch.setattr(cj.os, "geteuid", lambda: 0, raising=False)
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    assert calls


def test_export_lists_orphan_rows_and_keeps_them_in_db(temp_home, caplog):
    """747baaf507d1 / 36c93ec4f5fd: a row whose job is not in jobs.json (a git level dropped it) is
    never lost and no side file is written: runtime-export lists its id and the row stays in the
    db; --retire refuses while it exists, unless --drop-orphans."""
    from cron import jobs as cj
    from cron import runtime_state as rs
    from hermes_cli.cron import cron_runtime_export

    created = cj.create_job(prompt="x", schedule="every 5m", name="kept")
    cj.mark_job_run(created["id"], True)
    orphan = {"last_status": "ok", "repeat.completed": 4}
    rs.write_rows(temp_home / "cron", {"gone0000abcd": orphan})
    with caplog.at_level("WARNING"):
        assert cj.export_runtime_to_json(retire=False) == 1
    assert any("gone0000abcd" in r.getMessage() for r in caplog.records)
    assert rs.read_all(temp_home / "cron")["gone0000abcd"] == orphan
    assert _raw(temp_home, created["id"])["last_status"] == "ok"
    assert sorted(p.name for p in (temp_home / "cron").iterdir()
                  if "orphan" in p.name) == []

    before = _snapshot(_jobs_file(temp_home))
    with pytest.raises(cj.RuntimeOrphansRefused) as exc:
        cj.export_runtime_to_json()  # retire=True is the default
    assert exc.value.ids == ["gone0000abcd"]
    assert _db(temp_home).exists() and _snapshot(_jobs_file(temp_home)) == before
    assert rs.read_all(temp_home / "cron")["gone0000abcd"] == orphan

    class A:
        retire, drop_orphans = True, False
    assert cron_runtime_export(A()) == 2
    assert _db(temp_home).exists()
    A.drop_orphans = True
    assert cron_runtime_export(A()) == 0
    assert not _db(temp_home).exists()


def test_kill_switch_fold_keeps_orphan_rows(temp_home, monkeypatch):
    """The json kill switch's automatic fold must not raise on orphans and must not drop them:
    rows with a definition are folded into jobs.json, the orphan row stays in the db."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="ks-orphan")
    cj.mark_job_run(created["id"], True)
    rs.write_rows(temp_home / "cron", {"gone0000abcd": {"last_status": "ok"}})
    monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "json")
    assert cj.get_job(created["id"])["last_status"] == "ok"
    assert _raw(temp_home, created["id"])["last_status"] == "ok"
    assert rs.read_all(temp_home / "cron") == {"gone0000abcd": {"last_status": "ok"}}
    cj.load_jobs()  # no re-fold loop on an unchanged orphans-only db
    assert rs.read_all(temp_home / "cron") == {"gone0000abcd": {"last_status": "ok"}}


def test_repeat_completed_from_row_is_normalized(temp_home):
    """a73bd19ead9a: repeat.completed comes from the row now; a non-int/negative value there gets
    the same normalization load_jobs gives the file's value."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="rep", repeat=5)
    jid = created["id"]
    rs.write_rows(temp_home / "cron", {jid: {"repeat.completed": "2"}})
    assert cj.get_job(jid)["repeat"]["completed"] == 2
    rs.write_rows(temp_home / "cron", {jid: {"repeat.completed": -5}})
    assert next(j for j in cj.load_jobs() if j["id"] == jid)["repeat"]["completed"] == 0


def test_quick_snapshot_restore_takes_runtime_from_live_db(temp_home):
    """Interim (t_82c613d9 owns the pair restore): restoring a quick snapshot restores
    definitions; runtime_state.db is captured but never restored, so runtime bookkeeping comes
    from the live db (I1) and no pre-restore db copy is written into cron/."""
    from cron import jobs as cj
    from hermes_cli import backup

    created = cj.create_job(prompt="x", schedule="every 5m", name="snap", repeat=10)
    jid = created["id"]
    for _ in range(3):
        cj.mark_job_run(jid, True)
    snap = backup.create_quick_snapshot(hermes_home=temp_home)
    snap_dir = backup._quick_snapshot_root(temp_home) / snap
    assert (snap_dir / "cron" / "runtime_state.db").exists()  # captured
    cj.update_job(jid, {"name": "renamed"})
    for _ in range(4):
        cj.mark_job_run(jid, True)
    assert backup.restore_quick_snapshot(snap, hermes_home=temp_home)
    job = cj.get_job(jid)
    assert job["name"] == "snap" and job["repeat"]["completed"] == 7
    assert not [p for p in (temp_home / "cron").iterdir() if "pre-restore" in p.name]


def test_profile_export_captures_cron_pair_under_store_lock(tmp_path, monkeypatch):
    """12e3f4b761ac (current body): the export captures jobs.json and runtime_state.db as one
    unit under the profile's cron store lock, the db via the SQLite backup API."""
    from cron import jobs as cj
    from cron import runtime_state as rs
    from hermes_cli import backup_sqlite, profiles

    profile_dir = tmp_path / "profiles" / "p"
    cron = profile_dir / "cron"
    cron.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model: x\n")
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j1", "name": "n"}]}),
                                    encoding="utf-8")
    rs.write_rows(cron, {"j1": {"last_status": "ok"}})
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda n: profile_dir)
    monkeypatch.setattr(profiles, "validate_profile_name", lambda n: None)
    held = []
    real = backup_sqlite._safe_copy_db

    def spy(src, dst, **kw):
        if str(src).endswith(rs.DB_NAME):
            held.append(getattr(cj._jobs_lock_state, "depth", 0))
        return real(src, dst, **kw)

    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", spy)
    out = profiles.export_profile("p", str(tmp_path / "export.tar.gz"))
    assert out.exists()
    assert held and held[0] >= 1


def test_recovery_never_discards_generation_whose_owner_is_alive(temp_home, clock):
    """e52f4613c22a (Premature discard): a generation whose owning save still holds its lock is
    in flight however old it is; a free lock file means the owner died, discard at once."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    created = cj.create_job(prompt="x", schedule="every 5m", name="alive")
    cron = temp_home / "cron"

    def pending():
        with rs.connect(cron) as conn:
            return rs.pending_gens(conn)

    gen = "a" * 32
    with cj._gen_owner_lock(gen):
        with rs.transaction(cron) as conn:
            rs.stage_pending(conn, gen, {created["id"]: {"last_status": "x"}})
        clock[0] += rs.pending_discard_after() * 10
        with cj._jobs_lock():
            cj._recover_pending_runtime(None)
        assert pending() == [gen]
    dead = "d" * 32
    with rs.transaction(cron) as conn:
        rs.stage_pending(conn, dead, {created["id"]: {"last_status": "y"}})
    cj._gen_lock_path(dead).write_text("")  # its owner died holding nothing: lock free
    with cj._jobs_lock():
        cj._recover_pending_runtime(None)
    assert dead not in pending()


# --- Round 5 core keys (Apollo ruling 2026-10-25, option (a)) -----------------------------------

def test_save_rolls_forward_sibling_generation_published_before_our_rename(temp_home):
    """9083dea60b86 (I3), two-writer sequence: A's save recovers at entry; then a degraded-lock
    sibling B stages generation gB, renames jobs.json naming gB and dies (lock file free); then A
    renames over B's commit marker. B's committed rows must survive (rolled forward before the
    marker is erased), not be discarded later as an unnamed dead generation."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    a = cj.create_job(prompt="x", schedule="every 5m", name="a")
    b = cj.create_job(prompt="x", schedule="every 5m", name="b")
    cj.mark_job_run(a["id"], True)
    real = cj._stage_jobs_payload
    gen_b = "b" * 32
    fired = []

    def stage_then_sibling(jobs_file, jobs, runtime_gen=None, overlay=None):
        out = real(jobs_file, jobs, runtime_gen, overlay)
        if not fired:
            fired.append(1)
            with rs.transaction(temp_home / "cron") as conn:
                row = rs.read_row(conn, a["id"]) or {}
                rs.stage_pending(conn, gen_b, {a["id"]: dict(row, last_status="sibling")},
                                 bases={a["id"]: row})
            data = json.loads(_jobs_file(temp_home).read_text())
            data["runtime_gen"] = gen_b
            _jobs_file(temp_home).write_text(json.dumps(data), encoding="utf-8")
            cj._gen_lock_path(gen_b).write_text("")  # B died: its lock file is free
        return out

    with cj._jobs_lock():
        jobs = cj.load_jobs()
        for job in jobs:
            if job["id"] == b["id"]:
                job["name"] = "b2"
        cj._stage_jobs_payload = stage_then_sibling
        try:
            cj.save_jobs(jobs)
        finally:
            cj._stage_jobs_payload = real
    assert fired
    assert cj.get_job(b["id"])["name"] == "b2"
    assert cj.get_job(a["id"])["last_status"] == "sibling"
    with rs.connect(temp_home / "cron") as conn:
        assert rs.pending_gens(conn) == []


def test_first_load_reads_rows_of_db_created_after_existence_check(temp_home, monkeypatch):
    """8cd4fa44c9bb: the database is created (with rows) by a sibling between load_jobs'
    existence check and its read: open first, then decide, so the sibling's row is read rather
    than answered with {}."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    job = _fixture_job(1, with_runtime=False)
    _write_store(temp_home, [job])
    assert not _db(temp_home).exists()
    real = cj._runtime_write_allowed
    fired = []

    def sibling_creates_db():
        if not fired:
            fired.append(1)
            rs.write_rows(temp_home / "cron", {job["id"]: {"last_status": "sibling"}})
        return real()

    monkeypatch.setattr(cj, "_runtime_write_allowed", sibling_creates_db)
    loaded = cj.load_jobs()
    assert fired
    assert loaded[0]["last_status"] == "sibling"


@pytest.mark.parametrize("bad", ["orphan", "non_object"])
def test_fold_is_all_or_nothing(temp_home, tmp_path, bad):
    """93002a296759: a fold that cannot place every row (a row whose job is not in jobs.json, or
    a row that is not a JSON object) refuses: returns False, keeps the db, leaves jobs.json."""
    from cron import runtime_state as rs

    cron = tmp_path / "staged" / "cron"
    cron.mkdir(parents=True)
    job = _fixture_job(1, with_runtime=False)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    rs.write_rows(cron, {job["id"]: {"last_status": "ok"}})
    if bad == "orphan":
        rs.write_rows(cron, {"gone0000abcd": {"manual_run_prompt": "secret text"}})
    else:
        with rs.transaction(cron) as conn:
            conn.execute("INSERT INTO job_runtime(job_id, fields, updated_at) VALUES (?,?,0)",
                         ("weird0000abc", "[1, 2]"))
    before = (cron / "jobs.json").read_bytes()
    assert rs.fold_staged_copy(cron) is False
    assert rs.db_path(cron).exists()
    assert (cron / "jobs.json").read_bytes() == before


def test_profile_export_refuses_symlinked_staged_cron_dir(tmp_path, monkeypatch):
    """d8715e0ffdf8: a profile whose cron/ is a symlink (copytree keeps it) must not have the fold
    and the db removals run against the symlink target: the export refuses, the target's db and
    jobs.json are untouched."""
    from cron import runtime_state as rs
    from hermes_cli import profiles

    outside = tmp_path / "live-cron"
    outside.mkdir()
    job = _fixture_job(1, with_runtime=False)
    (outside / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    rs.write_rows(outside, {job["id"]: {"last_status": "ok"}})
    jobs_before = (outside / "jobs.json").read_bytes()
    profile_dir = tmp_path / "profiles" / "p"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model: x\n")
    (profile_dir / "cron").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda n: profile_dir)
    monkeypatch.setattr(profiles, "validate_profile_name", lambda n: None)
    out = tmp_path / "export.tar.gz"
    with pytest.raises(ValueError, match="symlink"):
        profiles.export_profile("p", str(out))
    assert not out.exists()
    assert rs.db_path(outside).exists()
    assert (outside / "jobs.json").read_bytes() == jobs_before
