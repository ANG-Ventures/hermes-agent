"""Hold one idle read-only connection on every live kanban board, so no other opener is ever the
first or the last connection to a board's ``-shm`` (t_1460fa32).

Why (t_2cfa2782, Mac Studio dtrace): a read-only open+close of an idle WAL board that no other
process has open costs about 37 KB of SSD writes. The opener takes the DMS lock alone, ftruncates
the ``-shm`` and rebuilds the wal-index, and the last close fsyncs those pages. With ONE idle
connection held by another process, 570 open/close cycles wrote 0 B. Every opener on the host (the
cron script loader, ``/usr/bin/python3`` scripts, gh-webhook-consumer, land_evaluator, workers)
then reads the existing wal-index instead of rebuilding it.

Who runs it: the process that holds the machine-global dispatcher lock (the gateway-embedded
dispatcher, or ``hermes kanban daemon``). There is exactly one per kanban root, it already
enumerates every board each tick, and it is long-lived. When it restarts, the anchor goes with it:
boards are unanchored until the next leader takes the lock (about one dispatch interval), which is
the pre-anchor behaviour for that window, not a fault.

The held connection is ``mode=ro`` with no open transaction: it holds only the shared DMS byte of
the ``-shm`` (which is the point), no read mark and no write lock, so it never blocks a writer, a
checkpoint or a WAL reset. Cost: 3 fds per board (db, ``-wal``, ``-shm``).

Every ``interval`` seconds a pass stats each held board and closes the connection when the path is
gone or its ``(dev, ino)`` changed (a board deleted, archived or replaced), then reopens on the new
file. The board list is re-read every ``rescan_every`` passes. A deleted board's inode therefore
stays allocated for at most one pass.

WAL: before the anchor, the last close of a board checkpointed it and removed the ``-wal``. With the
anchor there is no last close, so the anchor does that job: when a board's ``-wal`` is non-empty and
has not been written for ``quiet_s`` seconds, or is larger than ``wal_cap`` bytes, it runs
``PRAGMA wal_checkpoint(TRUNCATE)`` on a short-lived ``mode=rw`` connection with no busy wait (a
busy board is retried next pass, never waited on). That keeps idle boards at a 0-byte ``-wal`` (so
:mod:`hermes_cli.sqlite_quiet_memo` keeps answering their probes from memory) and bounds a busy
board's ``-wal`` at ``wal_cap`` plus one pass of growth. Writers still checkpoint on their own
(``wal_autocheckpoint``, ``journal_size_limit``, the dispatcher's PASSIVE checkpoint).

Off switches. Read when the dispatcher takes the lock: ``HERMES_KANBAN_BOARD_ANCHOR=0`` in the
environment, or ``kanban.board_anchor: false`` in config.yaml. Read on EVERY pass, so no restart is
needed (``<kanban home>/kanban/``, i.e. ``~/.hermes/kanban/`` on the Studio):

- ``board-anchor.off``: the pass closes every held connection and does nothing else until the file
  is removed; the next pass after removal reopens them.
- ``board-anchor-checkpoint.off``: keep holding the boards but run no ``wal_checkpoint`` (the
  writers' own checkpoints still run).

Each switch is logged once when it appears and once when it goes. One INFO line
(``kanban board anchor: held=.. opened=.. closed=.. truncated=.. busy_skipped=..``, cumulative
since start except ``held``) is logged when the counts changed, at most once every
``log_every`` seconds (600).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

_log = logging.getLogger(__name__)

ENV_SWITCH = "HERMES_KANBAN_BOARD_ANCHOR"
DEFAULT_INTERVAL_S = 5.0
DEFAULT_RESCAN_EVERY = 6
DEFAULT_QUIET_S = 30.0
DEFAULT_WAL_CAP_BYTES = 16 * 1024 * 1024
DEFAULT_LOG_EVERY_S = 600.0
OFF_FILE = "board-anchor.off"
CHECKPOINT_OFF_FILE = "board-anchor-checkpoint.off"


def default_switch_dir() -> Path:
    """``<kanban home>/kanban``: where the live off-switch files are looked for."""
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "kanban"


def enabled(kanban_cfg: Optional[dict] = None) -> bool:
    """False when ``HERMES_KANBAN_BOARD_ANCHOR`` is a false-y value or ``kanban.board_anchor`` is
    false; True otherwise."""
    env = os.environ.get(ENV_SWITCH, "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    if isinstance(kanban_cfg, dict) and kanban_cfg.get("board_anchor", True) is False:
        return False
    return True


def live_board_db_paths() -> list[Path]:
    """The resolved ``kanban.db`` of every live board, plus every phantom board dir
    (``boards/<slug>/kanban.db`` with no ``board.json``). ``list_boards()`` opens each phantom
    once per process to prove it empty, so a short-lived script that lists boards would otherwise
    rebuild every phantom's ``-shm`` on each run. ``boards/_archived`` is never included."""
    from hermes_cli import kanban_db as kb

    found: list[str] = [os.fspath(m["db_path"]) for m in kb.list_boards(include_archived=False)
                        if m.get("db_path")]
    root = kb.boards_root()
    try:
        children = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        try:
            if not kb._normalize_board_slug(child.name) or kb.skip_reserved_board_dir(child):
                continue
        except ValueError:  # "_archived", dotfiles, anything that is not a board slug
            continue
        db = child / "kanban.db"
        if db.is_file() and not (child / "board.json").exists():
            found.append(os.fspath(db))
    out: list[Path] = []
    seen: set[str] = set()
    for p in found:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            out.append(Path(real))
    return out


def _ident(path: str) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _open_idle(path: str) -> Optional[sqlite3.Connection]:
    """A ``mode=ro`` connection that has mapped the board's ``-shm`` and holds no transaction."""
    try:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=0,
                               isolation_level=None, check_same_thread=False)
    except sqlite3.Error:
        return None
    try:
        # A first read maps the wal-index; autocommit ends the read transaction at once.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _truncate_wal(path: str) -> bool:
    """``wal_checkpoint(TRUNCATE)`` without waiting. True when it completed (the -wal is now 0 B);
    False when it was busy or failed (retried on a later pass)."""
    try:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True, timeout=0,
                               isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        conn.execute("PRAGMA synchronous=FULL")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return bool(row) and row[0] == 0
    except sqlite3.Error:
        _log.debug("kanban board anchor: checkpoint of %s failed", path, exc_info=True)
        return False
    finally:
        conn.close()


class BoardAnchor:
    """See the module docstring. ``refresh()`` is one pass; ``start()``/``stop()`` run it on a
    daemon thread every ``interval`` seconds."""

    def __init__(
        self,
        paths: Callable[[], Iterable[Path]] = live_board_db_paths,
        *,
        interval: float = DEFAULT_INTERVAL_S,
        rescan_every: int = DEFAULT_RESCAN_EVERY,
        quiet_s: float = DEFAULT_QUIET_S,
        wal_cap: int = DEFAULT_WAL_CAP_BYTES,
        checkpoint: bool = True,
        switch_dir: Optional[Path] = None,
        log_every: float = DEFAULT_LOG_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._paths_fn = paths
        self.interval = max(float(interval), 0.5)
        self.rescan_every = max(int(rescan_every), 1)
        self.quiet_s = float(quiet_s)
        self.wal_cap = int(wal_cap)
        self.checkpoint = bool(checkpoint)
        self._paths: list[str] = []
        self._passes = 0
        self._held: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._switch_dir = Path(switch_dir) if switch_dir is not None else None
        self.log_every = float(log_every)
        self._clock = clock
        self.totals = {"opened": 0, "closed": 0, "truncated": 0, "busy_skipped": 0}
        self._off = False  # board-anchor.off seen on the last pass
        self._ckpt_off = False  # board-anchor-checkpoint.off seen on the last pass
        self._logged_counts: Optional[tuple] = None
        self._logged_at: Optional[float] = None

    # -- live switches --------------------------------------------------------------------------
    def _switch_path(self, name: str) -> Optional[Path]:
        if self._switch_dir is None:
            try:
                self._switch_dir = default_switch_dir()
            except Exception:
                return None
        return self._switch_dir / name

    def _switch_on(self, name: str) -> bool:
        p = self._switch_path(name)
        return p is not None and os.path.exists(p)

    def _read_switches(self) -> tuple[bool, bool]:
        off = self._switch_on(OFF_FILE)
        ckpt_off = self._switch_on(CHECKPOINT_OFF_FILE)
        if off != self._off:
            _log.info("kanban board anchor: %s %s", self._switch_path(OFF_FILE),
                      "present: closing every held connection until it is removed" if off
                      else "removed: holding boards again")
        if ckpt_off != self._ckpt_off:
            _log.info("kanban board anchor: %s %s", self._switch_path(CHECKPOINT_OFF_FILE),
                      "present: no -wal checkpoints until it is removed" if ckpt_off
                      else "removed: -wal checkpoints resume")
        self._off, self._ckpt_off = off, ckpt_off
        return off, ckpt_off

    def _maybe_log(self, held: int) -> None:
        counts = (held, self.totals["opened"], self.totals["closed"], self.totals["truncated"],
                  self.totals["busy_skipped"])
        if counts == self._logged_counts:
            return
        now = self._clock()
        if self._logged_at is not None and now - self._logged_at < self.log_every:
            return
        self._logged_counts, self._logged_at = counts, now
        _log.info("kanban board anchor: held=%d opened=%d closed=%d truncated=%d busy_skipped=%d"
                  " checkpoint=%s", *counts, "off" if (self._ckpt_off or not self.checkpoint)
                  else "on")

    # -- one pass -------------------------------------------------------------------------------
    def refresh(self) -> dict:
        off, ckpt_off = self._read_switches()
        if off:
            with self._lock:
                closed = len(self._held)
                for path in list(self._held):
                    self._close(path)
            self.totals["closed"] += closed
            self._passes = 0  # rescan the board list on the first pass after the switch goes
            self._maybe_log(0)
            return {"held": 0, "opened": 0, "closed": closed, "truncated": 0, "busy_skipped": 0,
                    "off": True}
        if self._passes % self.rescan_every == 0:
            try:
                self._paths = [os.fspath(p) for p in self._paths_fn()]
            except Exception:
                _log.debug("kanban board anchor: board list unavailable; keeping %d",
                           len(self._paths), exc_info=True)
        self._passes += 1
        want = set(self._paths)
        opened = closed = truncated = busy = 0
        do_ckpt = self.checkpoint and not ckpt_off
        with self._lock:
            for path in [p for p in self._held if p not in want]:
                self._close(path)
                closed += 1
            for path in self._paths:
                key = _ident(path)
                held = self._held.get(path)
                if held is not None and held[0] != key:
                    self._close(path)
                    closed += 1
                    held = None
                if key is None:
                    continue
                if held is None:
                    conn = _open_idle(path)
                    if conn is None:
                        continue
                    if _ident(path) != key:  # replaced between the stat and the open
                        conn.close()
                        continue
                    self._held[path] = (key, conn)
                    opened += 1
                if do_ckpt and self._wal_due(path):
                    if _truncate_wal(path):
                        truncated += 1
                        _log.debug("kanban board anchor: truncated the -wal of %s", path)
                    else:
                        busy += 1
            held_n = len(self._held)
        for k, v in (("opened", opened), ("closed", closed), ("truncated", truncated),
                     ("busy_skipped", busy)):
            self.totals[k] += v
        self._maybe_log(held_n)
        return {"held": held_n, "opened": opened, "closed": closed, "truncated": truncated,
                "busy_skipped": busy, "off": False}

    def _wal_due(self, path: str) -> bool:
        try:
            st = os.stat(path + "-wal")
        except OSError:
            return False
        if st.st_size <= 0:
            return False
        return st.st_size > self.wal_cap or (time.time() - st.st_mtime) >= self.quiet_s

    def _close(self, path: str) -> None:
        held = self._held.pop(path, None)
        if held is not None:
            try:
                held[1].close()
            except sqlite3.Error:
                pass

    def held_paths(self) -> list[str]:
        with self._lock:
            return sorted(self._held)

    def close_all(self) -> None:
        with self._lock:
            for path in list(self._held):
                self._close(path)

    # -- thread ---------------------------------------------------------------------------------
    def start(self) -> "BoardAnchor":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="kanban-board-anchor",
                                            daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                _log.debug("kanban board anchor: pass failed", exc_info=True)
            self._stop.wait(self.interval)
        self.close_all()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout)
        self.close_all()


def start_if_enabled(kanban_cfg: Optional[dict] = None) -> Optional[BoardAnchor]:
    """Start the anchor thread for the dispatcher leader, or return None when switched off."""
    if not enabled(kanban_cfg):
        _log.info("kanban board anchor: off (%s=0 or kanban.board_anchor=false)", ENV_SWITCH)
        return None
    try:
        anchor = BoardAnchor().start()
    except Exception:
        _log.warning("kanban board anchor: could not start", exc_info=True)
        return None
    _log.info("kanban board anchor: holding an idle read-only connection on every live board;"
              " live off switches: %s, %s", anchor._switch_path(OFF_FILE),
              anchor._switch_path(CHECKPOINT_OFF_FILE))
    return anchor
