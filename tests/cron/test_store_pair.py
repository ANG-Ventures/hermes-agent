"""I4 (t_82c613d9): a cron store is the pair (jobs.json, runtime_state.db [+ pending generations]).
Every leg that copies, restores, imports or exports one half does the other in the same
operation, under the cron store lock, and no exported archive carries ``runtime_state.db*``.

One test per leg, driven only through each leg's public entry point (no store_pair import), on a
real store under a temp HERMES_HOME. ``tests/cron/test_store_pair_lint.py`` is the static half.
"""

import json
import sqlite3
import tarfile
import threading
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    monkeypatch.delenv("HERMES_CRON_MIRROR_INTERVAL_S", raising=False)
    return h


def _rows(cron_dir):
    """Runtime rows read straight from the db file (empty when there is none)."""
    db = Path(cron_dir) / "runtime_state.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {jid: json.loads(f) for jid, f in con.execute("SELECT job_id, fields FROM job_runtime")}
    finally:
        con.close()


def _completed(rows, jid):
    return (rows.get(jid) or {}).get("repeat.completed")


def _job(cj, name, runs):
    jid = cj.create_job(prompt="p", schedule="every 5m", name=name, repeat=100)["id"]
    for _ in range(runs):
        cj.mark_job_run(jid, True)
    return jid


def _pair_consistent(jobs_doc, rows):
    """The two halves describe the same set of jobs."""
    ids = {j["id"] for j in jobs_doc["jobs"]}
    return set(rows) == ids  # every scheduled job carries a row (next_run_at) from create_job


def _no_db_names(names):
    return [n for n in names if Path(n).name.startswith("runtime_state.db")]


# --- leg: capture (quick snapshot, `hermes backup`, automatic full backup) ------------------

def _capture_with_sibling_create(home, monkeypatch, capture, *, zip_leg):
    """Run *capture* while a sibling thread creates a job right after the FIRST pair member was
    captured (quick snapshot: as the db half is copied; zip legs: after the first of the two
    halves is archived, whichever comes first). Inside one lock section the sibling waits and
    lands in neither half; two independent copies pair a definition with no row or the reverse."""
    from cron import jobs as cj
    from hermes_cli import backup, backup_sqlite

    done, errors = [], []

    def sibling():
        try:
            _job(cj, "sibling", 1)
        except BaseException as exc:  # surfaced below: a sibling that never ran proves nothing
            errors.append(exc)

    def fire():
        if not done:
            t = threading.Thread(target=sibling, daemon=True)
            done.append(t)
            t.start()
            t.join(0.5)  # finishes at once without the lock; blocks while capture holds it

    if zip_leg:
        real_zip = backup._write_zip_file

        def write_zip_file(zf, path, arcname):
            real_zip(zf, path, arcname)
            if Path(arcname).name in ("jobs.json", "runtime_state.db"):
                fire()

        monkeypatch.setattr(backup, "_write_zip_file", write_zip_file)
    else:
        real_db = backup_sqlite._safe_copy_db

        def copy_db(src, dst, **kw):
            if Path(src).name == "runtime_state.db":
                fire()
            return real_db(src, dst, **kw)

        monkeypatch.setattr(backup_sqlite, "_safe_copy_db", copy_db)
        monkeypatch.setattr(backup, "_safe_copy_db", copy_db)
    try:
        result = capture()
    finally:
        if done:
            done[0].join(10)
    assert done and not errors, (done, errors)
    assert any(j["name"] == "sibling" for j in cj.load_jobs())
    return result


@pytest.mark.parametrize("leg", ["quick_snapshot", "hermes_backup", "automatic_full_backup"])
def test_capture_takes_the_pair_in_one_lock_section(home, tmp_path, monkeypatch, leg):
    """Prism 30138fa3c582 / 4c96c12e94f2 / 5ba6308fb494 / e615d634eba2 / 277467fde015."""
    from cron import jobs as cj
    from hermes_cli import backup

    _job(cj, "a", 2)
    out = tmp_path / "out"
    out.mkdir()
    if leg == "quick_snapshot":
        snap = _capture_with_sibling_create(
            home, monkeypatch, lambda: backup.create_quick_snapshot(hermes_home=home),
            zip_leg=False)
        d = backup._quick_snapshot_root(home) / snap / "cron"
        doc, rows = json.loads((d / "jobs.json").read_text()), _rows(d)
    else:
        zpath = out / "b.zip"
        if leg == "hermes_backup":
            _capture_with_sibling_create(home, monkeypatch, lambda: backup.run_backup(
                Namespace(output=str(zpath), census_out=None, keep=0)), zip_leg=True)
        else:
            _capture_with_sibling_create(
                home, monkeypatch, lambda: backup._write_full_zip_backup(zpath, home),
                zip_leg=True)
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            doc = json.loads(zf.read("cron/jobs.json"))
            assert "cron/runtime_state.db" in names
            (out / "x").mkdir()
            (out / "x" / "runtime_state.db").write_bytes(zf.read("cron/runtime_state.db"))
        rows = _rows(out / "x")
    assert rows and _pair_consistent(doc, rows), (sorted(rows), [j["id"] for j in doc["jobs"]])


# --- leg: restore (quick snapshot) ----------------------------------------------------------

@pytest.mark.parametrize("shape", ["pair", "json_only", "live_db_lost"])
def test_quick_snapshot_restore_moves_the_pair(home, shape):
    """Prism 731075bc7763 / b5190aaf4349 (pair), 68eae47a394e / b493dcb2080d (json-only snapshot
    with a live db), 8f1196456086 (live db lost), e3dd44d3f3ec (no pre-restore copy in cron/)."""
    from cron import jobs as cj
    from hermes_cli import backup

    jid = _job(cj, "snap", 3)
    snap = backup.create_quick_snapshot(hermes_home=home)
    sdir = backup._quick_snapshot_root(home) / snap
    if shape == "json_only":
        # A snapshot taken before #2287: jobs.json carries the runtime, there is no db.
        data = json.loads((sdir / "cron" / "jobs.json").read_text())
        data["jobs"][0]["repeat"]["completed"] = 3
        data["jobs"][0]["last_status"] = "from-snapshot"
        (sdir / "cron" / "jobs.json").write_text(json.dumps(data))
        (sdir / "cron" / "runtime_state.db").unlink()
        meta = json.loads((sdir / "manifest.json").read_text())
        meta["files"].pop("cron/runtime_state.db")
        (sdir / "manifest.json").write_text(json.dumps(meta))
    for _ in range(4):
        cj.mark_job_run(jid, True)
    cj.update_job(jid, {"name": "renamed"})
    if shape == "live_db_lost":
        for p in (home / "cron").glob("runtime_state.db*"):
            p.unlink()

    assert backup.restore_quick_snapshot(snap, hermes_home=home)
    job = cj.get_job(jid)
    assert job["name"] == "snap"
    assert job["repeat"]["completed"] == 3  # runtime from the snapshot, not the live db
    if shape == "json_only":
        assert job["last_status"] == "from-snapshot"
    else:
        assert _completed(_rows(home / "cron"), jid) == 3
    assert sorted(p.name for p in (home / "cron").iterdir()
                  if p.name.startswith("runtime_state.db")) in ([], ["runtime_state.db"],
                                                                ["runtime_state.db",
                                                                 "runtime_state.db-shm",
                                                                 "runtime_state.db-wal"])


# --- leg: cron-loss auto-restore ------------------------------------------------------------

def test_cron_loss_auto_restore_restores_the_pair(home):
    """Prism 10c3c30e47f6: the post-update net restored jobs.json only, so the restored jobs had
    no runtime rows (or the rows of the jobs that replaced them)."""
    from cron import jobs as cj
    from hermes_cli import backup

    a, b = _job(cj, "a", 2), _job(cj, "b", 5)
    snap = backup.create_quick_snapshot(hermes_home=home)
    cj.remove_job(a)
    cj.remove_job(b)
    _job(cj, "desktop-only", 1)

    assert backup.restore_cron_jobs_if_emptied(snap, hermes_home=home)
    rows = _rows(home / "cron")
    assert _completed(rows, a) == 2 and _completed(rows, b) == 5
    doc = json.loads((home / "cron" / "jobs.json").read_text())
    assert _pair_consistent(doc, rows)
    assert {j["id"]: j["repeat"]["completed"] for j in cj.load_jobs()} == {a: 2, b: 5}


# --- leg: full import (`hermes import`) ----------------------------------------------------

def _archive(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _db_bytes(tmp_path, rows):
    from cron import runtime_state as rs

    d = tmp_path / "srcdb"
    d.mkdir(exist_ok=True)
    rs.write_rows(d, rows)
    con = sqlite3.connect(d / "runtime_state.db")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return (d / "runtime_state.db").read_bytes()


@pytest.mark.parametrize("shape", ["json_only", "db_only", "pair", "stray_copy"])
def test_full_import_restores_the_pair_or_nothing(home, tmp_path, monkeypatch, shape):
    """Prism d9d06b40ad21 / 43dce05aba1d: a JSON-only (legacy) archive kept the live rows, which
    then won over the imported runtime. A db-only archive must not install rows with no
    definitions; a stray ``runtime_state.db.*`` copy is never installed."""
    import hermes_cli.gateway as gw
    from cron import jobs as cj
    from hermes_cli.backup import run_import

    monkeypatch.setattr(gw, "ensure_gateway_service", lambda **kw: True)
    monkeypatch.setattr(gw, "_is_service_running", lambda: True)
    jid = _job(cj, "live", 7)
    definition = {k: v for k, v in cj.get_job(jid).items()}
    definition["name"] = "imported"
    definition["repeat"] = {"times": 100, "completed": 2}
    definition["last_status"] = "from-archive"
    members = {"config.yaml": "model: x\n"}
    if shape != "db_only":
        members["cron/jobs.json"] = json.dumps({"jobs": [definition]})
    if shape in ("db_only", "pair"):
        members["cron/runtime_state.db"] = _db_bytes(
            tmp_path, {jid: {"repeat.completed": 2, "last_status": "from-archive"}})
    if shape == "stray_copy":
        members["cron/runtime_state.db.pre-restore-x"] = b"SQLite format 3\x00junk"
    _archive(tmp_path / "b.zip", members)
    before = (home / "cron" / "jobs.json").read_bytes()

    rc = run_import(Namespace(zipfile=str(tmp_path / "b.zip"), force=True))
    assert not (home / "cron" / "runtime_state.db.pre-restore-x").exists()
    if shape == "db_only":
        assert rc == 1
        assert (home / "cron" / "jobs.json").read_bytes() == before
        assert _completed(_rows(home / "cron"), jid) == 7
        return
    job = cj.get_job(jid)
    assert job["name"] == "imported"
    assert job["repeat"]["completed"] == 2 and job["last_status"] == "from-archive"


# --- leg: profile export ---------------------------------------------------------------------

def _profile(tmp_path, monkeypatch):
    from hermes_cli import profiles

    pdir = tmp_path / "profiles" / "p"
    (pdir / "cron").mkdir(parents=True)
    (pdir / "config.yaml").write_text("model: x\n")
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda n: pdir)
    monkeypatch.setattr(profiles, "validate_profile_name", lambda n: None)
    return profiles, pdir


@pytest.mark.parametrize("stray", ["runtime_state.db.pre-restore-20261025", "runtime_state.db.bak"])
def test_profile_export_ships_no_runtime_db_copy(tmp_path, monkeypatch, stray):
    """Prism 0191332b0702: a pre-restore (or any other) copy of runtime_state.db in cron/ shipped
    unscrubbed in the archive. Walk the whole archive: no ``runtime_state.db*`` member at all, and
    the rows ride folded inside the scrubbed jobs.json."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    profiles, pdir = _profile(tmp_path, monkeypatch)
    with cj.use_cron_store(pdir):
        jid = _job(cj, "exp", 2)
    rs.write_rows(tmp_path / "copy", {jid: {"last_error": "sk-ant-api03-" + "A" * 40}})
    (pdir / "cron" / stray).write_bytes((tmp_path / "copy" / "runtime_state.db").read_bytes())
    out = profiles.export_profile("p", str(tmp_path / "export.tar.gz"))
    with tarfile.open(out) as tf:
        names = tf.getnames()
        doc = json.loads(tf.extractfile(next(m for m in tf.getmembers()
                                             if m.name.endswith("cron/jobs.json"))).read())
    assert _no_db_names(names) == []
    assert doc["jobs"][0]["repeat"]["completed"] == 2


# --- leg: profile import --------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["pair", "stray_copy", "db_only"])
def test_profile_import_installs_no_unpaired_runtime_db(tmp_path, monkeypatch, shape):
    """A hand-built / older archive carrying cron/runtime_state.db (or a copy of it): the db is
    folded into the archive's jobs.json, or the import refuses; it is never installed beside a
    jobs.json from another moment, and a db-only store is never installed."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda n: tmp_path / "profiles" / n)
    src = tmp_path / "src" / "q" / "cron"
    src.mkdir(parents=True)
    (src.parent / "config.yaml").write_text("model: x\n")
    if shape != "db_only":
        (src / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j1", "name": "n"}]}))
    db = _db_bytes(tmp_path, {"j1": {"last_status": "ok"}})
    (src / ("runtime_state.db.pre-restore-1" if shape == "stray_copy" else "runtime_state.db")) \
        .write_bytes(db)
    archive = tmp_path / "q.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src.parent, arcname="q")

    if shape == "db_only":
        with pytest.raises(ValueError):
            profiles.import_profile(str(archive))
        assert not (tmp_path / "profiles" / "q").exists()
        return
    pdir = profiles.import_profile(str(archive))
    assert _no_db_names(p.name for p in (pdir / "cron").iterdir()) == []
    job = json.loads((pdir / "cron" / "jobs.json").read_text())["jobs"][0]
    assert job.get("last_status") == ("ok" if shape == "pair" else None)


# --- leg: `hermes cron runtime-export` -----------------------------------------------------

def test_runtime_export_folds_the_pair_and_writes_no_side_file(home, capsys):
    """36c93ec4f5fd: runtime-export never writes a side file (the orphans file was dropped from
    #2287); --retire folds every row into jobs.json and removes the db and its sidecars together;
    an orphan row refuses the retire and leaves both halves as they were."""
    from cron import jobs as cj
    from cron import runtime_state as rs
    from hermes_cli.cron import cron_runtime_export

    jid = _job(cj, "x", 4)
    rs.write_rows(home / "cron", {"orphan": {"last_status": "ok"}})
    before = (home / "cron" / "jobs.json").read_bytes()
    assert cron_runtime_export(Namespace(retire=True, drop_orphans=False)) == 2
    assert (home / "cron" / "jobs.json").read_bytes() == before
    assert set(_rows(home / "cron")) == {jid, "orphan"}

    assert cron_runtime_export(Namespace(retire=True, drop_orphans=True)) == 0
    names = sorted(p.name for p in (home / "cron").iterdir())
    assert [n for n in names if n.startswith("runtime_state") or "orphan" in n] == [], names
    doc = json.loads((home / "cron" / "jobs.json").read_text())
    assert doc["jobs"][0]["repeat"]["completed"] == 4



# --- Prism round 1 on #2331 (round-2 send-back) ----------------------------------------------

def _fail_runtime_db_copy(monkeypatch, how):
    """Make the SQLite capture of the LIVE runtime_state.db fail: ``refused`` returns False (the
    store_pair capture refuses), ``oserror`` raises."""
    from hermes_cli import backup, backup_sqlite

    real = backup_sqlite._safe_copy_db

    def copy_db(src, dst, **kw):
        if Path(src).name == "runtime_state.db" and Path(src).parent.parent.name == "home":
            if how == "oserror":
                raise OSError("simulated read failure")
            return False
        return real(src, dst, **kw)

    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", copy_db)
    monkeypatch.setattr(backup, "_safe_copy_db", copy_db)


@pytest.mark.parametrize("leg", ["quick_snapshot", "hermes_backup"])
def test_capture_refuses_a_symlinked_pair_member(home, tmp_path, monkeypatch, leg):
    """Prism 8661dfe2d768: the backup walkers never follow a link, but the pair capture reopened
    ``cron/jobs.json`` through one and archived a file outside the home."""
    from cron import jobs as cj
    from hermes_cli import backup

    _job(cj, "a", 1)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"jobs": [{"id": "SECRET-OUTSIDE", "name": "x"}]}))
    (home / "cron" / "jobs.json").unlink()
    (home / "cron" / "jobs.json").symlink_to(outside)
    if leg == "quick_snapshot":
        snap = backup.create_quick_snapshot(hermes_home=home)
        sdir = backup._quick_snapshot_root(home) / (snap or "none")
        assert not (sdir / "cron" / "jobs.json").exists()
        if snap:
            meta = json.loads((sdir / "manifest.json").read_text())
            assert "cron/runtime_state.db" in meta["failed_dbs"]
            assert "cron/jobs.json" not in meta["files"]
    else:
        zpath = tmp_path / "out" / "b.zip"
        zpath.parent.mkdir()
        assert backup.run_backup(Namespace(output=str(zpath), census_out=None, keep=0)) is False
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            assert not {"cron/jobs.json", "cron/runtime_state.db"} & set(names), names
            assert not any(b"SECRET-OUTSIDE" in zf.read(n) for n in names)


def test_import_refuses_a_cron_dir_linked_outside_the_home(home, tmp_path, monkeypatch):
    """Prism dbf3e89fdfa5: the pair branch of `hermes import` skipped the per-file containment
    check, so a ``cron`` dir symlinked outside HERMES_HOME was written through."""
    import hermes_cli.gateway as gw
    from hermes_cli.backup import run_import

    monkeypatch.setattr(gw, "ensure_gateway_service", lambda **kw: True)
    monkeypatch.setattr(gw, "_is_service_running", lambda: True)
    outside = tmp_path / "outside-cron"
    outside.mkdir()
    (home / "cron").rmdir()
    (home / "cron").symlink_to(outside)
    _archive(tmp_path / "b.zip", {"config.yaml": "model: x\n",
                                  "cron/jobs.json": json.dumps({"jobs": [{"id": "j", "name": "n"}]})})
    rc = run_import(Namespace(zipfile=str(tmp_path / "b.zip"), force=True))
    assert rc == 1
    assert list(outside.iterdir()) == []


def test_failed_restore_keeps_a_db_backup_beside_the_live_store(home, monkeypatch):
    """Prism daf60a454f51: the failed-restore cleanup removed every ``runtime_state.db*`` in the
    LIVE cron dir, including an operator's ``runtime_state.db.bak``."""
    from cron import jobs as cj
    from hermes_cli import backup, backup_sqlite

    _job(cj, "a", 2)
    snap = backup.create_quick_snapshot(hermes_home=home)
    for p in (home / "cron").glob("runtime_state.db*"):
        p.unlink()
    (home / "cron" / "runtime_state.db.bak").write_bytes(b"operator copy")
    real = backup_sqlite._safe_copy_db

    def copy_db(src, dst, **kw):
        if snap in str(src):  # the snapshot db cannot be installed
            Path(dst).write_bytes(b"half-written")
            return False
        return real(src, dst, **kw)

    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", copy_db)
    backup.restore_quick_snapshot(snap, hermes_home=home)
    assert (home / "cron" / "runtime_state.db.bak").read_bytes() == b"operator copy"
    assert not (home / "cron" / "runtime_state.db").exists()


@pytest.mark.parametrize("how", ["refused", "oserror"])
def test_quick_snapshot_keeps_jobs_json_when_the_db_capture_fails(home, monkeypatch, how):
    """Prism 5aa2d785f5af: a refused pair capture dropped jobs.json from the snapshot (the file
    the cron-loss net restores from), and an OSError aborted the whole snapshot."""
    from cron import jobs as cj
    from hermes_cli import backup

    _job(cj, "a", 2)
    _fail_runtime_db_copy(monkeypatch, how)
    snap = backup.create_quick_snapshot(hermes_home=home)
    assert snap
    sdir = backup._quick_snapshot_root(home) / snap
    meta = json.loads((sdir / "manifest.json").read_text())
    assert "cron/jobs.json" in meta["files"] and (sdir / "cron" / "jobs.json").is_file()
    assert "cron/runtime_state.db" in meta["failed_dbs"]
    assert not (sdir / "cron" / "runtime_state.db").exists()


@pytest.mark.parametrize("leg", ["restore", "auto_restore"])
def test_snapshot_with_a_failed_db_keeps_the_live_rows(home, leg):
    """Prism 72fe227c70de: a snapshot whose runtime db failed (or was skipped) was read as a
    pre-#2287 json-only snapshot, and its restore wiped every live runtime row."""
    from cron import jobs as cj
    from hermes_cli import backup

    a, b = _job(cj, "a", 2), _job(cj, "b", 3)
    snap = backup.create_quick_snapshot(hermes_home=home)
    # The shape a failed db capture leaves: jobs.json kept, the db absent and named in failed_dbs.
    sdir = backup._quick_snapshot_root(home) / snap
    (sdir / "cron" / "runtime_state.db").unlink()
    meta = json.loads((sdir / "manifest.json").read_text())
    meta["files"].pop("cron/runtime_state.db")
    meta["failed_dbs"] = ["cron/runtime_state.db"]
    (sdir / "manifest.json").write_text(json.dumps(meta))
    for _ in range(4):
        cj.mark_job_run(a, True)
    if leg == "restore":
        cj.update_job(a, {"name": "renamed"})
        assert backup.restore_quick_snapshot(snap, hermes_home=home)
        assert cj.get_job(a)["name"] == "a"
    else:
        cj.remove_job(b)
        assert backup.restore_cron_jobs_if_emptied(snap, hermes_home=home)
    rows = _rows(home / "cron")
    assert _completed(rows, a) == 6  # live runtime kept, not cleared
    assert cj.get_job(a)["repeat"]["completed"] == 6


@pytest.mark.parametrize("leg", ["hermes_backup", "automatic_full_backup"])
def test_zip_never_publishes_a_half_pair(home, tmp_path, monkeypatch, leg):
    """Prism 077416e0ccaf: a write failure between the two halves left cron/jobs.json in the
    archive without its db, and the archive was published."""
    from cron import jobs as cj
    from hermes_cli import backup

    _job(cj, "a", 2)
    real = backup._write_zip_file

    def write_zip_file(zf, path, arcname):
        if arcname.endswith("cron/runtime_state.db"):
            raise OSError("simulated write failure")
        return real(zf, path, arcname)

    monkeypatch.setattr(backup, "_write_zip_file", write_zip_file)
    out = tmp_path / "out"
    out.mkdir()
    zpath = out / "b.zip"
    if leg == "hermes_backup":
        with pytest.raises(SystemExit) as ei:
            backup.run_backup(Namespace(output=str(zpath), census_out=None, keep=0))
        assert ei.value.code == 1
    else:
        assert backup._write_full_zip_backup(zpath, home) is None
    assert [p.name for p in out.iterdir() if p.suffix == ".zip"] == []
