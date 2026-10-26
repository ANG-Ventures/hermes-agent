"""Invariant I4 (t_82c613d9): a cron store is the PAIR ``(jobs.json, runtime_state.db [+ pending
generations])``.

Since #2287 the runtime bookkeeping of every job lives in ``cron/runtime_state.db`` and
``cron/jobs.json`` is the definitions plus a coalesced mirror of the rows. A pending two-phase
generation lives in the database's ``pending_runtime`` table and is named by the file's
``runtime_gen``. Neither half means anything alone, so any code that copies, moves, restores,
imports or exports one half does the other in the same operation, under the cron store lock, and
scrubs both. This module is the ONE place that does that file handling; every leg (quick snapshot
capture/restore, cron-loss auto-restore, full backup/import, profile export/import,
``hermes cron runtime-export``) calls it. ``tests/cron/test_store_pair_lint.py`` fails on any
copy/move/rename/open-for-write of either half outside this module and the store itself.

  capture(home, dest)        both halves of ``home``'s live store into ``dest``, one lock section,
                             the db through the SQLite backup API (WAL-safe, pending rows included).
                             ``fold=True`` (exports) folds the rows into the copied jobs.json and
                             ships no database; a capture that cannot fold refuses.
  restore(src, home)         ``src``'s pair onto ``home``'s live store, one lock section. A
                             json-only source (a pre-#2287 snapshot, a legacy archive) replaces the
                             db with an empty one: the restored file's runtime is adopted on the
                             next load, never mixed with the live rows.
  restore_members(...)       same, from archive members (``hermes import``).
  publish_definitions(...)   a definition-only rewrite of the live jobs.json, under the lock.
  scrub(staged_root)         export/import rule for a STAGED tree: fold every cron db into its
                             jobs.json, then drop every ``runtime_state.db*`` (binary,
                             unscrubbable, or a foreign image); refuses when a fold cannot place
                             every row (a db-only store).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import IO, Callable, Dict, Iterator, List, Optional, Tuple

from cron import runtime_state as _rs

logger = logging.getLogger(__name__)

JOBS_NAME = "jobs.json"
DB_NAME = _rs.DB_NAME
CRON_DIR = "cron"
JOBS_REL = f"{CRON_DIR}/{JOBS_NAME}"
DB_REL = f"{CRON_DIR}/{DB_NAME}"
# Keys of a jobs.json payload that describe a specific database (pending generation id, mirror
# seq/epoch). A json-only restore drops them: the database they describe is not restored with it.
_DB_BOUND_KEYS = ("runtime_gen", _rs.MIRROR_SEQ_KEY, _rs.MIRROR_EPOCH_KEY)


class PairRefused(ValueError):
    """The pair operation refused; nothing was written to the live store (or the archive)."""


def is_pair_member(name: str) -> bool:
    """True for ``jobs.json`` and ``runtime_state.db`` (or any copy/sidecar of it)."""
    return name == JOBS_NAME or name.startswith(DB_NAME)


def pair_home(parts: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
    """The home-relative parts of the store a path belongs to, when *parts* names a pair member
    of the root store (``cron/jobs.json``) or a profile store (``profiles/<n>/cron/jobs.json``);
    else None."""
    if len(parts) < 2 or parts[-2] != CRON_DIR or not is_pair_member(parts[-1]):
        return None
    home = parts[:-2]
    if home == () or (len(home) == 2 and home[0] == "profiles"):
        return home
    return None


@contextlib.contextmanager
def lock(home: Path) -> Iterator[None]:
    """Hold *home*'s cron jobs lock (cross-process flock + in-process RLock), routed to *home*'s
    store. Refuses (``CronLockDegraded``, nothing done) when the cross-process lock could not be
    taken (ruling R4): the holder may be mid-write across the two files."""
    from cron.jobs import CronLockDegraded, _jobs_lock, _write_refused_by_r4, use_cron_store

    with use_cron_store(home), _jobs_lock():
        if _write_refused_by_r4():
            raise CronLockDegraded(
                f"the cron jobs lock is degraded (another process holds "
                f"{Path(home) / CRON_DIR / '.jobs.lock'}); refusing to handle jobs.json + "
                f"{DB_NAME} as a pair (nothing copied). Retry")
        yield


def _copy_db(src: Path, dst: Path) -> bool:
    from hermes_cli.backup_sqlite import _safe_copy_db

    return _safe_copy_db(src, dst)


def _restore_db(src: Path, dst: Path) -> bool:
    from hermes_cli.backup_restore import _safe_restore_db

    return _safe_restore_db(src, dst)


def _unlink_db_files(cron_dir: Path) -> None:
    """Every ``runtime_state.db*`` directly in *cron_dir* (main, sidecars, stray copies)."""
    with contextlib.suppress(FileNotFoundError):
        for entry in os.scandir(cron_dir):
            if entry.name.startswith(DB_NAME) and not entry.is_dir(follow_symlinks=False):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(entry.path)


def _private_write(path: Path, data: bytes) -> None:
    """Create *path* exclusively (never through a planted link) 0600 and write *data*."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _parse_payload(raw: bytes, origin: str) -> object:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PairRefused(f"{origin} is not valid JSON ({exc}); nothing restored") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise PairRefused(f"{origin} has no jobs list; nothing restored")
    return data


# --- capture -------------------------------------------------------------------------------

def capture(home: Path, dest: Path, *, fold: bool = False) -> List[str]:
    """Copy *home*'s live pair into directory *dest* (``dest/jobs.json``, ``dest/runtime_state.db``)
    inside ONE cron lock section: the db through the SQLite backup API, so the rows, the pending
    generations and the file that names them are from one moment. Returns the names written
    (``[]`` when *home* has no jobs.json; a db with no jobs.json is refused, the rows would be
    captured with no definitions to pair them with).

    ``fold=True`` (every export): the copied rows are folded into ``dest/jobs.json`` and no
    database is left in *dest*; a fold that cannot place every row refuses (``PairRefused``) and
    removes what it wrote, so the caller never ships an unscrubbed db or a half pair."""
    home, dest = Path(home), Path(dest)
    live = home / CRON_DIR
    if dest.is_dir():
        _discard(dest)  # a staging dir: never leave a pair member from another moment in it
    with lock(home):
        jobs_src, db_src = live / JOBS_NAME, live / DB_NAME
        if not jobs_src.is_file():
            if db_src.exists():
                raise PairRefused(f"{db_src} exists but {jobs_src} does not; refusing to capture "
                                  "runtime rows with no definitions")
            return []
        dest.mkdir(parents=True, exist_ok=True)
        _private_write(dest / JOBS_NAME, jobs_src.read_bytes())
        written = [JOBS_NAME]
        if db_src.exists():
            if not _copy_db(db_src, dest / DB_NAME):
                _discard(dest)
                raise PairRefused(f"could not capture {db_src} consistently (SQLite backup "
                                  "failed); nothing captured")
            written.append(DB_NAME)
    if fold and DB_NAME in written:
        if not _rs.fold_staged_copy(dest):
            _discard(dest)
            raise PairRefused(f"{db_src} cannot be folded in full into {jobs_src} (a row with "
                              "no definition, or an unreadable row); the database would ship "
                              "unscrubbed. Repair cron/jobs.json or run `hermes cron "
                              "runtime-export --to-json --retire` first")
        written = [JOBS_NAME]
    return written


def _discard(dest: Path) -> None:
    _unlink_db_files(dest)
    with contextlib.suppress(FileNotFoundError):
        (dest / JOBS_NAME).unlink()
    with contextlib.suppress(FileNotFoundError):
        (dest / _rs.FOLD_TMP_NAME).unlink()


# --- restore -------------------------------------------------------------------------------

def restore(src: Path, home: Path) -> List[str]:
    """Restore the pair in directory *src* onto *home*'s live store as ONE unit, under the store
    lock, with nothing written unless every check passes first.

    * ``src/jobs.json`` is required (a lone db has no definitions: refused).
    * ``src/runtime_state.db`` present: it replaces the live db (page-level restore, so a live
      connection sees it), rows AND pending generations; ``jobs.json`` then replaces the file.
    * no db in *src* (json-only snapshot or archive): the live rows, pending generations and
      mirror bookkeeping are cleared, then the file replaces jobs.json; its runtime fields are
      adopted on the next load. The file's db-bound keys are dropped (they named another db).

    The live db is copied aside (private temp, never into ``cron/``) before it is replaced, and
    put back if the jobs.json publish fails. Returns the names restored. Raises ``PairRefused`` /
    ``CronLockDegraded`` with the live store unchanged."""
    from cron import jobs as cj

    src, home = Path(src), Path(home)
    jobs_src, db_src = src / JOBS_NAME, src / DB_NAME
    if not jobs_src.is_file():
        raise PairRefused(f"{src} has no {JOBS_NAME}; refusing to restore runtime rows without "
                          "definitions (nothing restored)")
    raw = jobs_src.read_bytes()
    data = _parse_payload(raw, str(jobs_src))
    has_db = db_src.is_file()
    if not has_db:
        if isinstance(data, dict) and any(k in data for k in _DB_BOUND_KEYS):
            data = {k: v for k, v in data.items() if k not in _DB_BOUND_KEYS}
            raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    live = home / CRON_DIR
    with lock(home), cj.use_cron_store(home):
        live.mkdir(parents=True, exist_ok=True)
        jobs_file, db_file = live / JOBS_NAME, live / DB_NAME
        with tempfile.TemporaryDirectory(prefix="hermes-cron-pair-") as tmp:
            rollback = Path(tmp) / DB_NAME
            had_db = db_file.exists()
            if had_db and not _copy_db(db_file, rollback):
                raise PairRefused(f"could not copy the live {db_file} aside before the restore; "
                                  "nothing restored")
            if has_db:
                restored_db = (_restore_db(db_src, db_file) if had_db
                               else _copy_db(db_src, db_file))
                if not restored_db:
                    if not had_db:
                        _unlink_db_files(live)
                    raise PairRefused(f"{db_src} failed its integrity check or could not be "
                                      "restored; live store unchanged")
            elif had_db:
                with cj._runtime_txn() as conn:
                    conn.execute("DELETE FROM job_runtime")
                    conn.execute("DELETE FROM pending_runtime")
                    conn.execute("DELETE FROM mirror_state")
            try:
                _publish_jobs(cj, jobs_file, raw)
            except BaseException:
                if had_db:
                    _restore_db(rollback, db_file)
                elif has_db:
                    _unlink_db_files(live)
                raise
            if has_db:
                cj._secure_runtime_db()
        with cj._defs_cache_lock:
            cj._defs_cache.clear()
    logger.warning("cron store pair restored into %s from %s (%s)", live, src,
                   "jobs.json + runtime_state.db" if has_db else "jobs.json only; live rows cleared")
    return [JOBS_NAME, DB_NAME] if has_db else [JOBS_NAME]


def _publish_jobs(cj, jobs_file: Path, raw: bytes) -> None:
    stat_before = None
    with contextlib.suppress(OSError):
        stat_before = os.stat(jobs_file)
    cj._guard_against_test_write_to_live_store(jobs_file)
    tmp = jobs_file.with_name(f".{JOBS_NAME}.restore-{os.getpid()}.tmp")
    _private_write(tmp, raw)
    try:
        landed = cj._publish_jobs_json(str(tmp), jobs_file)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    cj.fsync_directory((landed or jobs_file).parent)
    cj._secure_file(jobs_file)
    cj._preserve_file_ownership(jobs_file, stat_before)
    cj._record_load_stamp(None)


def restore_members(members: Dict[str, Callable[[], IO[bytes]]], home: Path) -> List[str]:
    """``hermes import``: restore a pair given as archive members. *members* maps ``jobs.json`` /
    ``runtime_state.db`` to a callable returning a readable binary stream. Sidecars are never
    passed (an archived -wal describes another image). Staged privately, then ``restore``."""
    with tempfile.TemporaryDirectory(prefix="hermes-cron-import-") as tmp:
        staged = Path(tmp)
        for name in (JOBS_NAME, DB_NAME):
            opener = members.get(name)
            if opener is None:
                continue
            fd = os.open(str(staged / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with opener() as src, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        return restore(staged, home)


# --- exports ------------------------------------------------------------------------------

def scrub(staged_root: Path, *, what: str = "export") -> None:
    """Export rule (the secret scrubber skips binaries): every staged ``cron/`` dir with a
    ``runtime_state.db`` is folded into its jobs.json (``PairRefused`` when it cannot be folded in
    full), then every ``runtime_state.db*`` left anywhere in the tree is removed. Symlinks are
    never followed (a link is removed, not its target). Only call on a STAGED copy."""
    staged_root = Path(staged_root)
    for dirpath, dirnames, filenames in os.walk(staged_root, followlinks=False):
        here = Path(dirpath)
        if here.name == CRON_DIR and DB_NAME in filenames and not (here / DB_NAME).is_symlink():
            if not _rs.fold_staged_copy(here):
                raise PairRefused(
                    f"refusing {what}: {here.relative_to(staged_root)} has a {DB_NAME} that "
                    "cannot be folded in full into readable jobs.json definitions (the database "
                    "would ship unscrubbed); repair cron/jobs.json or run `hermes cron "
                    "runtime-export --to-json --retire` first")
        for name in filenames + [d for d in dirnames if (here / d).is_symlink()]:
            if name.startswith(DB_NAME):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(here / name)
                logger.info("%s: dropped %s (a cron runtime db copy never ships)", what,
                            (here / name).relative_to(staged_root))


# --- other legs ---------------------------------------------------------------------------

def publish_definitions(home: Path, doc: object) -> None:
    """Rewrite *home*'s live jobs.json with *doc* (a definition-only repair, e.g. the post-update
    prompt-field net) under the store lock, through the store's own jobs.json choke point. The
    db is not touched: its rows stay authoritative for runtime (a row always wins)."""
    from cron import jobs as cj

    raw = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    _parse_payload(raw, "definitions")
    with lock(home), cj.use_cron_store(home):
        _publish_jobs(cj, Path(home) / CRON_DIR / JOBS_NAME, raw)
        with cj._defs_cache_lock:
            cj._defs_cache.clear()


def read_definitions(jobs_src: Path) -> List[dict]:
    """The merged jobs of a SHIPPED jobs.json (a profile distribution), loaded through a private
    throwaway store, never the live one."""
    from cron import jobs as cj

    with tempfile.TemporaryDirectory(prefix="hermes_dist_cron_") as tmp:
        staged = Path(tmp) / CRON_DIR
        staged.mkdir()
        _private_write(staged / JOBS_NAME, Path(jobs_src).read_bytes())
        with cj.use_cron_store(tmp):
            return [j for j in cj.load_jobs() if isinstance(j, dict)]


def export_to_json(*, retire: bool, drop_orphans: bool = False,
                   home: Optional[Path] = None) -> int:
    """``hermes cron runtime-export --to-json``: fold the rows into jobs.json (and with *retire*
    drop the db) as one locked operation on *home*'s store (the active store when None)."""
    from cron import jobs as cj

    ctx = cj.use_cron_store(home) if home is not None else contextlib.nullcontext()
    with ctx:
        return cj.export_runtime_to_json(retire=retire, drop_orphans=drop_orphans)
