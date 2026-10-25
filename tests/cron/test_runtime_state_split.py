"""t_94e91559: hot cron runtime fields live in ``cron/runtime_state.db``, not ``jobs.json``.

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
    (tmp_path / "cron").mkdir()
    return tmp_path


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

def test_700_job_store_heartbeats_and_marks_leave_jobs_json_untouched(temp_home):
    """50 heartbeat_fire_claim + 20 mark_job_run on a 700-job store: jobs.json mtime/inode/bytes
    unchanged, exactly 70 row upserts."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    _write_store(temp_home, [_fixture_job(i, with_runtime=True) for i in range(700)])
    cj.load_jobs()  # one-shot migration happens here
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


def test_due_scan_pending_slot_stamp_does_not_rewrite_jobs_json(temp_home):
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


def test_definition_edit_still_rewrites_jobs_json_without_runtime(temp_home):
    from cron import jobs as cj

    created = cj.create_job(prompt="x", schedule="every 5m", name="edit-me")
    assert cj.claim_job_for_fire(created["id"]) is True
    cj.update_job(created["id"], {"name": "edited"})
    on_disk = json.loads(_jobs_file(temp_home).read_text(encoding="utf-8"))["jobs"][0]
    assert on_disk["name"] == "edited"
    assert "fire_claim" not in on_disk and "next_run_at" not in on_disk
    assert "completed" not in on_disk["repeat"]
    assert cj.get_job(created["id"])["fire_claim"]["by"]  # runtime survived the definition save


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

    loaded = cj.load_jobs()
    assert loaded == originals  # field-for-field, through the table
    on_disk = json.loads(_jobs_file(temp_home).read_text(encoding="utf-8"))["jobs"]
    assert all("last_run_at" not in j and "completed" not in j["repeat"] for j in on_disk)
    assert _db(temp_home).exists()

    # Second load: no-op (file already clean, nothing re-migrated).
    before = _snapshot(_jobs_file(temp_home))
    assert cj.load_jobs() == originals
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
    assert "fire_claim" not in json.loads(_jobs_file(temp_home).read_text())["jobs"][0]


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
    assert cron_runtime_export(None) == 0
    assert "1 job(s)" in capsys.readouterr().out
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
    cj.save_jobs([j for j in cj.load_jobs() if j["id"] == a["id"]], replace=True)
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
