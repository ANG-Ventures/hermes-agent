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
  publish_definitions(...)   a definition-only edit of the live jobs (read+edit+save, one lock).
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
import stat
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
PAIR_NAMES = (JOBS_NAME, DB_NAME)
_DB_SIDECARS = ("-wal", "-shm", "-journal")
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


def _unlink_created_db(db_file: Path) -> None:
    """Failed-restore cleanup in a LIVE store (Prism daf60a454f51): only the main db this attempt
    created and its SQLite sidecars. Never a ``runtime_state.db.bak`` / ``.pre-restore-*`` copy
    someone keeps beside it; prefix-wide removal is ``scrub``'s, on a staged tree only."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{db_file}{suffix}")


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

def _dir_identity(path: Path) -> Optional[Tuple[int, int]]:
    """``(st_dev, st_ino)`` of *path* itself (``os.lstat``), ``None`` when it is missing."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def capture(home: Path, dest: Path, *, fold: bool = False,
            identity: Optional[List[Tuple[int, int]]] = None) -> List[str]:
    """Copy *home*'s live pair into directory *dest* (``dest/jobs.json``, ``dest/runtime_state.db``)
    inside ONE cron lock section: the db through the SQLite backup API, so the rows, the pending
    generations and the file that names them are from one moment. Returns the names written
    (``[]`` when *home* has no jobs.json; a db with no jobs.json is refused, the rows would be
    captured with no definitions to pair them with).

    ``fold=True`` (every export): the copied rows are folded into ``dest/jobs.json`` and no
    database is left in *dest*; a fold that cannot place every row refuses (``PairRefused``) and
    removes what it wrote, so the caller never ships an unscrubbed db or a half pair.

    *identity*, when given, gets the ``(st_dev, st_ino)`` of the ``cron/`` directory the members
    were read from, sampled under the lock before the reads and checked after them (Prism
    a398ca30beda): the caller records the store it captured, never the path's later occupant. A
    directory replaced while it was read refuses, since the halves may come from two stores."""
    home, dest = Path(home), Path(dest)
    live = home / CRON_DIR
    if dest.is_dir():
        _discard(dest)  # a staging dir: never leave a pair member from another moment in it
    with lock(home):
        read_from = _dir_identity(live)
        jobs_src, db_src = live / JOBS_NAME, live / DB_NAME
        # The backup walkers never follow a link; the pair path must not reopen one (Prism
        # 8661dfe2d768): a member that is a symlink or not a regular file refuses the pair.
        has_jobs, has_db = _regular_member(jobs_src), _regular_member(db_src)
        if not has_jobs:
            if has_db:
                raise PairRefused(f"{db_src} exists but {jobs_src} does not; refusing to capture "
                                  "runtime rows with no definitions")
            return []
        dest.mkdir(parents=True, exist_ok=True)
        _private_write(dest / JOBS_NAME, jobs_src.read_bytes())
        written = [JOBS_NAME]
        if has_db:
            if not _copy_db(db_src, dest / DB_NAME):
                _discard(dest)
                raise PairRefused(f"could not capture {db_src} consistently (SQLite backup "
                                  "failed); nothing captured")
            written.append(DB_NAME)
        if read_from is None or _dir_identity(live) != read_from:
            _discard(dest)
            raise PairRefused(f"{live} was replaced while it was captured (renamed or swapped); "
                              "nothing captured, the halves may belong to two stores")
    if identity is not None:
        identity.append(read_from)
    if fold and DB_NAME in written:
        if not _rs.fold_staged_copy(dest):
            _discard(dest)
            raise PairRefused(f"{db_src} cannot be folded in full into {jobs_src} (a row with "
                              "no definition, or an unreadable row); the database would ship "
                              "unscrubbed. Repair cron/jobs.json or run `hermes cron "
                              "runtime-export --to-json --retire` first")
        written = [JOBS_NAME]
    return written


def capture_definitions(home: Path, dest: Path) -> bool:
    """Fallback for a quick snapshot whose pair capture failed: ``dest/jobs.json`` only, under
    the lock, and NO db (the caller records the db as failed so a restore keeps the live rows).
    Returns False when *home* has no jobs.json. Same link policy as ``capture``."""
    home, dest = Path(home), Path(dest)
    if dest.is_dir():
        _discard(dest)
    with lock(home):
        jobs_src = home / CRON_DIR / JOBS_NAME
        if not _regular_member(jobs_src):
            return False
        dest.mkdir(parents=True, exist_ok=True)
        _private_write(dest / JOBS_NAME, jobs_src.read_bytes())
    return True


def _regular_member(path: Path, verb: str = "capture") -> bool:
    """True for a regular, non-symlink file; False when absent; ``PairRefused`` for anything
    else (a symlink, a directory, a device): the pair is never handled through a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PairRefused(f"{path} is not a regular file (symlink or special file); refusing to "
                          f"{verb} the cron store pair through it (nothing {verb}d)")
    return True


def _refuse_linked_live_store(home: Path) -> None:
    """``PairRefused`` unless *home*'s live ``cron/`` (when present) is a real directory inside
    *home* and every live pair member (db sidecars included) is absent or a regular file: a
    restore never writes, nor clears rows, through a link into another store."""
    live = Path(home) / CRON_DIR
    try:
        st = os.lstat(live)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PairRefused(f"{live} is not a real directory (symlink or special file); refusing "
                          "to restore the cron store pair through it (nothing restored)")
    try:
        live.resolve().relative_to(Path(home).resolve())
    except ValueError:
        raise PairRefused(f"{live} resolves outside {home}; refusing to restore the cron store "
                          "pair (nothing restored)") from None
    for name in PAIR_NAMES + tuple(f"{DB_NAME}{s}" for s in _DB_SIDECARS):
        # Prism 6245695b492f: the db and its sidecars are written IN PLACE (page-level restore,
        # row clear); a hard link is a regular file whose other name is another store's db.
        if _regular_member(live / name, "restore") and name != JOBS_NAME \
                and os.lstat(live / name).st_nlink != 1:
            raise PairRefused(f"{live / name} is hard-linked ({os.lstat(live / name).st_nlink} "
                              "names); refusing to restore the cron store pair in place through "
                              "it (nothing restored)")


def _refuse_linked_source(src: Path, root: Optional[Path]) -> None:
    """``PairRefused`` when the source ``cron/`` DIRECTORY is a symlink or resolves outside
    *root* (its snapshot root; ``src.parent`` when None). Prism 17c97989d85c: the member
    lstat()s alone read another store's pair through a linked directory."""
    root = Path(root) if root is not None else src.parent
    try:
        if os.path.islink(src):
            raise ValueError
        src.resolve().relative_to(root.resolve())
    except ValueError:
        raise PairRefused(f"{src} is a symlink or resolves outside {root}; refusing to restore "
                          "the cron store pair from it (nothing restored)") from None


def _roll_back(cj, jobs_file: Path, old_jobs: Optional[bytes], db_file: Path,
               db_copy: Optional[Path], *, created_db: bool) -> None:
    """Put BOTH halves back after a restore failed or was interrupted part-way (Prism
    21c9d75bea4a): the db from its copy (or remove the db this attempt created), then jobs.json
    to its previous bytes (or remove it when there was none). Best effort, every step tried."""
    if db_copy is not None:
        with contextlib.suppress(Exception):
            _restore_db(db_copy, db_file)
    elif created_db:
        _unlink_created_db(db_file)
    try:
        current = jobs_file.read_bytes() if _regular_member(jobs_file, "restore") else None
    except (OSError, PairRefused):
        return
    if current == old_jobs:
        return
    with contextlib.suppress(Exception):
        if old_jobs is None:
            jobs_file.unlink()
        else:
            _publish_jobs(cj, jobs_file, old_jobs)


def _discard(dest: Path) -> None:
    _unlink_db_files(dest)
    with contextlib.suppress(FileNotFoundError):
        (dest / JOBS_NAME).unlink()
    with contextlib.suppress(FileNotFoundError):
        (dest / _rs.FOLD_TMP_NAME).unlink()


# --- restore -------------------------------------------------------------------------------

def restore(src: Path, home: Path, *, db_failed: bool = False,
            root: Optional[Path] = None) -> List[str]:
    """Restore the pair in directory *src* onto *home*'s live store as ONE unit, under the store
    lock, with nothing written unless every check passes first.

    * ``src/jobs.json`` is required (a lone db has no definitions: refused).
    * ``src/runtime_state.db`` present: it replaces the live db (page-level restore, so a live
      connection sees it), rows AND pending generations; ``jobs.json`` then replaces the file.
    * no db in *src* (json-only snapshot or archive): the live rows, pending generations and
      mirror bookkeeping are cleared, then the file replaces jobs.json; its runtime fields are
      adopted on the next load. The file's db-bound keys are dropped (they named another db).
    * no db in *src* because its capture FAILED or was skipped (``db_failed=True``, from the
      snapshot manifest's ``failed_dbs`` / ``oversized_skipped``; Prism 72fe227c70de): that is not
      a json-only snapshot. Only jobs.json is restored; the live db and its rows are kept (a row
      wins, I1), never cleared.

    The live db is copied aside (private temp, never into ``cron/``) before it is replaced, and
    put back if the jobs.json publish fails. Returns the names restored. Raises ``PairRefused`` /
    ``CronLockDegraded`` with the live store unchanged."""
    from cron import jobs as cj

    src, home = Path(src), Path(home)
    jobs_src, db_src = src / JOBS_NAME, src / DB_NAME
    # Links (Prism 4c71a06241bb / dd8a638040cd): the pair is never read from, nor written
    # through, a symlink, on either side, checked before the lock is taken or anything written.
    _refuse_linked_live_store(home)
    _refuse_linked_source(src, root)
    if not _regular_member(jobs_src, "restore"):
        raise PairRefused(f"{src} has no {JOBS_NAME}; refusing to restore runtime rows without "
                          "definitions (nothing restored)")
    raw = jobs_src.read_bytes()
    data = _parse_payload(raw, str(jobs_src))
    has_db = _regular_member(db_src, "restore")
    if not has_db:
        if isinstance(data, dict) and any(k in data for k in _DB_BOUND_KEYS):
            data = {k: v for k, v in data.items() if k not in _DB_BOUND_KEYS}
            raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    live = home / CRON_DIR
    with lock(home), cj.use_cron_store(home):
        _refuse_linked_live_store(home)  # again under the lock: nothing swapped in meanwhile
        live.mkdir(parents=True, exist_ok=True)
        jobs_file, db_file = live / JOBS_NAME, live / DB_NAME
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-cron-pair-") as tmp:
                rollback = Path(tmp) / DB_NAME
                had_db = db_file.exists()
                touches_db = has_db or (had_db and not db_failed)
                if had_db and touches_db and not _copy_db(db_file, rollback):
                    raise PairRefused(f"could not copy the live {db_file} aside before the "
                                      "restore; nothing restored")
                # Prism 21c9d75bea4a: BOTH halves are set aside before either is touched, and an
                # interrupt anywhere after the first write puts BOTH back, never the db alone.
                old_jobs = jobs_file.read_bytes() if _regular_member(jobs_file, "restore") else None
                try:
                    if has_db:
                        restored_db = (_restore_db(db_src, db_file) if had_db
                                       else _copy_db(db_src, db_file))
                        if not restored_db:
                            raise PairRefused(f"{db_src} failed its integrity check or could not "
                                              "be restored; live store unchanged")
                    elif touches_db:
                        with cj._runtime_txn() as conn:
                            conn.execute("DELETE FROM job_runtime")
                            conn.execute("DELETE FROM pending_runtime")
                            conn.execute("DELETE FROM mirror_state")
                    _publish_jobs(cj, jobs_file, raw)
                except BaseException:
                    _roll_back(cj, jobs_file, old_jobs, db_file, rollback if had_db and touches_db
                               else None, created_db=has_db and not had_db)
                    raise
                if has_db:
                    cj._secure_runtime_db()
        finally:
            with cj._defs_cache_lock:
                cj._defs_cache.clear()
    logger.warning("cron store pair restored into %s from %s (%s)", live, src,
                   "jobs.json + runtime_state.db" if has_db
                   else "jobs.json only; live rows kept (snapshot db capture failed)" if db_failed
                   else "jobs.json only; live rows cleared")
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

def publish_definitions(home: Path, edit: Callable[[List[dict]], bool]) -> bool:
    """A definition-only repair of *home*'s live jobs (e.g. the post-update prompt-field net):
    under the store lock, load the live jobs, let *edit* change them in place, and save through
    ``save_jobs`` (its shrink-merge) when *edit* returns True. Read, edit and write are one lock
    section (Prism 643fd504e41a): a job created or edited by a sibling is never dropped by a
    document read before the lock. The db rows stay authoritative for runtime. Returns whether
    anything was saved."""
    from cron import jobs as cj

    with lock(home), cj.use_cron_store(home):
        jobs = cj.load_jobs()
        if not edit(jobs):
            return False
        cj.save_jobs(jobs)
        with cj._defs_cache_lock:
            cj._defs_cache.clear()
    return True


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
