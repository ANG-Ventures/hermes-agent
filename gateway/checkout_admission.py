"""Operator-controlled admission hold for the consumers of one shared checkout.

Problem (t_e8017c37): several long-lived processes -- e.g. two messaging
gateways and one ``serve`` backend -- import code from ONE git checkout.
Fetching/merging that checkout under a live turn corrupts it. Idle polls plus
markers do not stop a turn that arrives just after the last poll, and a
bounded wait followed by a signal interrupts real work.

Contract
========

* The **hold** is a durable file (``hold.json``) in a directory shared by every
  consumer of the checkout (default: ``<git-common-dir>/checkout-admission``).
  It carries an epoch, an owner token, a mode and the set of EXPECTED
  consumers. It has no TTL: only an explicit ``release`` by the same owner
  removes it, so it survives failed imports, reloads, restarts and probes.
* Every consumer process owns one :class:`AdmissionGate`. Every ingress calls
  :meth:`AdmissionGate.admit` (or :meth:`AdmissionGate.check`) and the check
  reads the hold file on EVERY call -- there is no cached "drain" flag that a
  late watcher tick could miss, and a freshly restarted process is closed
  from its first admission.
* Modes. ``drain``: new external turns are refused; internal continuations of
  already-admitted work (restart replays, background-completion events,
  queued follow-ups) are admitted AND COUNTED, so nothing is lost and the
  drain cannot finish until they are done. ``freeze``: every admission is
  refused. Operators move drain -> freeze once quiescent, verify again, then
  mutate the checkout.
* Acknowledgment. Each consumer periodically publishes its OWN record
  (``consumers/<name>/<instance>.json``; t_3275003d: no process ever writes
  another's record, so same-name publishers cannot hide each other) from a
  snapshot taken under the same process
  lock admissions use: the hold epoch it observed plus its admitted tickets
  and its own authoritative active-work count. Because admission
  (read-hold + register) and snapshot (read-hold + count) are serialized, a
  snapshot that observed epoch E counts every turn that was admitted before E
  was written. That is the late-arrival guarantee; it does not depend on poll
  timing.
* Verdict. :func:`evaluate` returns ``QUIESCENT`` only when a FREEZE hold is
  present and EVERY expected consumer has a fresh record, a live publisher
  lease (legacy records without one: a live pid) on this host, an
  acknowledgment of the CURRENT epoch and zero work. Missing, stale, corrupt,
  unacknowledged, foreign-host or unexpected-live-consumer evidence is
  ``UNKNOWN``; nonzero work is ``BUSY``; zero work under a drain hold is
  ``DRAINED`` (freeze next -- it is not mutation authority). :func:`wait_for_quiescence` never
  signals or interrupts anything: on timeout it returns ``DEFER``.

Operator CLI: ``python -m gateway.checkout_admission --help``.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

try:  # POSIX only; the hold is an operator feature of POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SCHEMA = 1
MODES = ("drain", "freeze")
DEFAULT_STALE_AFTER = 20.0
DEFAULT_PUBLISH_INTERVAL = 2.0
# Publisher lease (t_eceadca2): every record carries a per-process random
# ``token``; a thread independent of the serving loop rewrites the record at
# least every LEASE_S. A record is live while its lease is younger than
# LEASE_EXPIRY_FACTOR * LEASE_S, measured on the store's own mtime clock (a
# reader) or on the gate's monotonic clock (a same-name peer). No pid, /proc
# or writer wall clock is involved, so pid reuse, namespaces and cross-host
# skew cannot fake or hide liveness.
DEFAULT_LEASE_S = 30.0
LEASE_EXPIRY_FACTOR = 3
# Write-if-changed (t_dd40e9f2): a publish whose record matches the last one
# written (ignoring the fields below) is skipped until the last write is this
# old, so an idle publisher writes every 10 s instead of every 2 s. Each
# write was mkstemp + fsync + rename + dir fsync on the checkout's drive (on
# ACE-AI ~2.7k of them per 30 min drove the OS drive's ext4 journal; the
# fsyncs are gone too, see write_consumer). Kept
# well under DEFAULT_STALE_AFTER so published_at never reads stale; any change
# (a new hold epoch, a ticket, a work count) still writes on the next tick.
DEFAULT_REFRESH_S = 10.0
_VOLATILE_FIELDS = ("published_at", "lease_seq", "lease_released")
QUIESCENT, DRAINED, BUSY, UNKNOWN, NOT_HELD, DEFER = (
    "QUIESCENT", "DRAINED", "BUSY", "UNKNOWN", "NOT_HELD", "DEFER",
)
_CONSUMER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CONFIG_KEY = "checkout_admission"


class HoldUnreadable(RuntimeError):
    """``hold.json`` exists but cannot be parsed/validated (fail closed)."""


class HoldConflict(RuntimeError):
    """The requested operator action conflicts with the current hold."""


class AdmissionRefused(RuntimeError):
    """A new unit of work was refused because the checkout is held."""

    def __init__(self, reason: str, *, epoch: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.epoch = epoch


def validate_consumer_name(name: str) -> str:
    if not isinstance(name, str) or not _CONSUMER_RE.fullmatch(name):
        raise ValueError(f"invalid consumer name {name!r} (want e.g. gateway:default)")
    return name


def _consumer_filename(name: str) -> str:
    return validate_consumer_name(name).replace(":", "@") + ".json"


LEGACY = ""  # record key of the pre-t_3275003d shared consumers/<name>.json
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _validate_instance(instance: str) -> str:
    if not isinstance(instance, str) or not _INSTANCE_RE.fullmatch(instance):
        raise ValueError(f"invalid publisher instance {instance!r}")
    return instance



def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, durable: bool = True) -> None:
    """Atomic replace. ``durable=False`` skips the file and directory fsyncs:
    each one forces an ext4 journal commit (t_dd40e9f2). Only for records whose
    loss in a crash is the correct reading (a publisher's liveness ack)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            if durable:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        if durable:
            _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _preserve_unreadable(src: Path) -> Path:
    """Copy ``src`` to a NEW uniquely named audit file; never touches an existing one.

    The bytes go to a private temp file first (fsynced), then are installed
    with ``os.link``, which fails rather than overwrite -- so repeated
    recoveries in the same second, or a crash mid-copy, cannot truncate or
    corrupt an earlier audit snapshot.
    """
    directory = src.parent
    fd, tmp = tempfile.mkstemp(prefix=f".{src.name}.audit.", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        dest = directory / f"hold.unreadable.{time.time_ns()}.{uuid.uuid4().hex[:8]}.json"
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise
        except OSError:
            # Filesystem without hard links (some SMB/FUSE mounts): reserve
            # the unique name with O_EXCL (fails rather than overwrite), then
            # atomically swap the complete temp over our own placeholder.
            os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            try:
                os.replace(tmp, dest)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(dest)
                raise
        _fsync_dir(directory)
        return dest
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        dfd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    import psutil  # core dependency; os.kill(pid, 0) is not a probe on Windows

    return bool(psutil.pid_exists(pid))


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _host_boot_id() -> Optional[str]:
    """Kernel boot UUID (Linux): unlike any timestamp, immune to clock steps."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def _pid_ns_id() -> Optional[str]:
    """Identity of this process's PID namespace (Linux ``/proc/self/ns/pid``). A recorded pid only names a process
    we can inspect when it was recorded in OUR pid namespace (Prism f-0d05a4e1ac34ca32: containers can share
    hostname, store, boot id and time namespace but not pids). None where unsupported."""
    try:
        return os.readlink("/proc/self/ns/pid") or None
    except OSError:
        return None


def _time_ns_id() -> Optional[str]:
    """Identity of this process's time namespace (Linux ``/proc/self/ns/time``, e.g. ``time:[4026531834]``).
    ``/proc/<pid>/stat`` start times are relative to the READER's time namespace, so a boot-relative offset
    only compares across publishers in the same one (Prism f-4c7533074d58f2a7). None where unsupported."""
    try:
        return os.readlink("/proc/self/ns/time") or None
    except OSError:
        return None


def _start_offset(pid: int) -> Optional[float]:
    """Seconds from host boot to ``pid``'s start, on a basis a wall-clock step
    cannot move: the kernel's own boot-relative ``starttime`` (field 22 of
    ``/proc/<pid>/stat``, clock ticks since boot) read directly, so no boot
    timestamp enters the arithmetic at all (Prism bc03ae51308a: psutil < 6
    computed ``create_time`` from a CACHED btime while ``boot_time()`` read a
    fresh one, so the two terms could straddle a clock step). None where there
    is no procfs: the identity there is ``_create_time``."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        # comm may contain spaces/parens: split after the LAST ')'. starttime is
        # field 22 (1-based) = index 19 of the fields after state.
        fields = data[data.rfind(b")") + 2:].split()
        return int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError, AttributeError):
        return None


def _create_time_is_stable() -> bool:
    """Whether psutil's create_time() is a kernel-stored timestamp (macOS, Windows, BSD: yes) rather than
    ``btime + starttime/CLK_TCK`` recomputed from the CURRENT wall clock (Linux: no, so a clock step between
    the publisher's sample and ours shifts it; Prism f-0d05a4e1ac34ca32). Only a stable create_time may
    clear the collision flag."""
    return not (sys.platform.startswith("linux") or sys.platform == "android")  # Android: Linux kernel (Prism 1865f7896adc)


def _create_time(pid: int) -> Optional[float]:
    """Fallback identity without procfs (macOS, Windows): psutil's absolute
    process creation timestamp. The kernel stores it once at process start and
    a later clock step does not rewrite it, so both sides of the comparison read
    the same stored value; ``boot_time()`` never enters (Prism: a
    ``create_time - boot_time`` difference moved with the clock on these hosts
    and rejected a genuine live publisher)."""
    import psutil

    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error:
        return None


def _same_live_process(pid: Any, rec: Mapping[str, Any]) -> bool:
    """LEGACY records only (no lease ``token``; written before t_eceadca2;
    remove after one release). Leased records never reach this.

    True unless there is positive evidence that ``pid`` is no longer the
    process that wrote ``rec`` (dead, other host boot, or a reused pid).

    Inconclusive evidence (missing/non-finite fields, psutil errors) keeps the
    record live: a false "shared" defers a deploy, a false "gone" lets it
    mutate under a busy consumer.
    """
    # The pid names a process we can inspect ONLY inside our own PID namespace (Prism f-0d05a4e1ac34ca32: containers
    # share hostname, store, boot id and time namespace, not pids). A recorded namespace that differs from ours, or is
    # absent while the kernel exposes one, means every local read of that pid (liveness, start time) is about an
    # unrelated process: inconclusive, the record stays live.
    cur_pns = _pid_ns_id()
    if cur_pns and rec.get("pid_ns") != cur_pns:
        # A record with NO pid_ns predates #2075: a dead pid still clears it, as before #2075, or the first
        # restart onto this code pins the sticky flag on its dead predecessor forever (Prism ff3c2cf86472).
        # A live pid there stays inconclusive.
        return "pid_ns" in rec or _pid_alive(pid)
    if not _pid_alive(pid):
        return False
    import psutil

    try:
        boot_id, off, ct = rec.get("boot_id"), rec.get("start_offset"), rec.get("create_time")
        cur_boot = _host_boot_id()
        # An EMPTY recorded boot_id is missing evidence, not another boot (Prism f-6a6148c60a0851c2).
        if isinstance(boot_id, str) and boot_id and cur_boot and boot_id != cur_boot:
            return False
        if _finite(off):
            # The offset is only comparable inside one time namespace (Prism f-4c7533074d58f2a7): a recorded
            # namespace that differs from ours, or is missing on either side, makes the offset inconclusive
            # (keep the record live); then only a recorded absolute create_time may clear it.
            tns, cur_tns = rec.get("time_ns"), _time_ns_id()
            if tns and cur_tns and tns == cur_tns:
                cur = _start_offset(pid)
                if cur is not None and abs(cur - off) > 1:
                    return False
            elif _finite(ct) and _create_time_is_stable():
                cur = _create_time(pid)
                if cur is not None and abs(cur - ct) > 1:
                    return False
            # Linux with an incomparable offset: NOTHING clock-step-safe is left to compare, so the record
            # stays live (Prism f-0d05a4e1ac34ca32 / f-78f5d20661de8262). A stale record ages out via boot_id.
        elif _finite(ct) and _create_time_is_stable():
            cur = _create_time(pid)
            if cur is not None and abs(cur - ct) > 1:
                return False
        # A record from a pre-boot_id publisher carries only wall-clock
        # boot_ts, which a clock step can make look stale: keep it live.
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


@dataclass(frozen=True)
class HoldState:
    epoch: str
    owner: str
    mode: str
    expected: tuple
    created_at: float
    reason: str = ""

    def refuses(self, *, internal: bool) -> bool:
        return (not internal) or self.mode == "freeze"

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA, "epoch": self.epoch, "owner": self.owner,
            "mode": self.mode, "expected": list(self.expected),
            "created_at": self.created_at, "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Any) -> "HoldState":
        try:
            if not isinstance(data, dict) or data.get("schema") != SCHEMA:
                raise ValueError("schema")
            epoch, owner, mode = data["epoch"], data["owner"], data["mode"]
            expected = data["expected"]
            created = float(data["created_at"])
            if not (isinstance(epoch, str) and epoch and isinstance(owner, str) and owner):
                raise ValueError("epoch/owner")
            if mode not in MODES:
                raise ValueError("mode")
            if not isinstance(expected, list) or not expected:
                raise ValueError("expected")
            names = tuple(validate_consumer_name(n) for n in expected)
            return cls(epoch, owner, mode, names, created, str(data.get("reason") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise HoldUnreadable(f"hold.json is invalid: {exc}") from exc


class HoldStore:
    """Filesystem layout shared by the operator and every consumer."""

    def __init__(self, directory: os.PathLike | str):
        self.directory = Path(directory)
        self.hold_path = self.directory / "hold.json"
        self.consumers_dir = self.directory / "consumers"
        self.lock_path = self.directory / "operator.lock"

    # -- hold ---------------------------------------------------------------
    def read_hold(self) -> Optional[HoldState]:
        """``None`` = open. Raises :class:`HoldUnreadable` on any doubt."""
        try:
            raw = self.hold_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HoldUnreadable(f"hold.json unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HoldUnreadable("hold.json is not JSON") from exc
        return HoldState.from_json(data)

    @contextlib.contextmanager
    def _operator_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def hold(self, owner: str, expected: Iterable[str], *, mode: str = "drain",
             reason: str = "", now: Optional[float] = None,
             recover_unreadable: bool = False) -> HoldState:
        """Engage (or re-engage with a new epoch) the hold. Never opens it.

        An unreadable hold has lost its owner, so replacing it (which lets the
        new owner release it) requires the explicit ``recover_unreadable``
        operator override; the unreadable bytes are kept for audit.
        """
        if not owner:
            raise ValueError("owner token required")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        names = tuple(dict.fromkeys(validate_consumer_name(n) for n in expected))
        if not names:
            raise ValueError("at least one expected consumer is required")
        with self._operator_lock():
            try:
                current = self.read_hold()
            except HoldUnreadable as exc:
                if not recover_unreadable:
                    raise HoldConflict(
                        f"{exc}; its owner is unknown -- re-hold with "
                        "--recover-unreadable to take it over") from exc
                # Copy, don't move: hold.json stays in place (closed) until
                # _atomic_write_json swaps the replacement in, so a crash or
                # write failure here can never leave admission open.
                with contextlib.suppress(OSError):
                    _preserve_unreadable(self.hold_path)
                logger.warning("checkout admission: %s took over an unreadable hold", owner)
                current = None  # re-holding over garbage keeps it CLOSED
            if current is not None and current.owner != owner:
                raise HoldConflict(f"hold is owned by {current.owner!r}")
            state = HoldState(uuid.uuid4().hex, owner, mode, names,
                              time.time() if now is None else now, reason)
            _atomic_write_json(self.hold_path, state.to_json())
            return state

    def release(self, owner: str) -> None:
        """Explicit operator release -- the ONLY way the hold opens."""
        with self._operator_lock():
            current = self.read_hold()  # unreadable -> raise, stay closed
            if current is None:
                raise HoldConflict("no hold is engaged")
            if current.owner != owner:
                raise HoldConflict(f"hold is owned by {current.owner!r}")
            os.unlink(self.hold_path)
            _fsync_dir(self.directory)

    # -- consumer records ---------------------------------------------------
    # Every publisher writes ONLY its own record, consumers/<name>/<instance>.json
    # (t_3275003d): no process ever rewrites another's record, so two same-name
    # publishers cannot hide each other's work and there is nothing to lock.
    # consumers/<name>.json (key LEGACY) is the shared record pre-t_3275003d
    # code writes; it is still read, so a mixed old/new fleet is judged whole.
    def consumer_path(self, name: str, instance: Optional[str] = None) -> Path:
        """The legacy shared record (``instance`` None/LEGACY) or one publisher's own record."""
        if not instance:
            return self.consumers_dir / _consumer_filename(name)
        return self.consumers_dir / _consumer_filename(name)[:-5] / f"{_validate_instance(instance)}.json"

    def write_consumer(self, name: str, record: Mapping[str, Any]) -> None:
        inst = record.get("instance")
        valid = isinstance(inst, str) and bool(_INSTANCE_RE.fullmatch(inst))
        # Not fsynced (t_dd40e9f2): an ack is a liveness record rewritten every
        # refresh_s; one lost in a crash reads as stale, which is the truth.
        _atomic_write_json(self.consumer_path(name, inst if valid else None), record, durable=False)

    def _record_paths(self, name: str) -> list:
        """``(key, path, stat)`` of every record of ``name``, newest write first."""
        cands = [(LEGACY, self.consumer_path(name))]
        sub = self.consumers_dir / _consumer_filename(name)[:-5]
        # os.scandir, not Path.glob: glob swallows EACCES/EIO and yields nothing,
        # which reads as "no other publisher" (Prism 7ef91f0ded22). Only a missing
        # directory is empty; any other failure propagates so readers fail closed.
        try:
            with os.scandir(sub) as it:
                names = [e.name for e in it]
        except (FileNotFoundError, NotADirectoryError):
            names = []
        cands += [(n[:-5], sub / n) for n in names
                  if n.endswith(".json") and not n.startswith(".") and _INSTANCE_RE.fullmatch(n[:-5])]
        out = []
        for key, path in cands:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            out.append((key, path, st))
        out.sort(key=lambda t: t[2].st_mtime_ns, reverse=True)
        return out

    @staticmethod
    def _load(path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("consumer record is not an object")
        return data

    def consumer_records(self, name: str) -> list:
        """``(key, record)`` for every record of ``name``, newest first. Raises on
        any unreadable record (the caller fails closed); a record removed
        between listing and reading is skipped."""
        out = []
        for key, path, _st in self._record_paths(name):
            try:
                out.append((key, self._load(path)))
            except FileNotFoundError:
                continue
        return out

    def scan_records(self, name: str) -> list:
        """``(key, signature, record-or-None)`` for a publisher's peer scan: an
        unreadable record is None, never an exception. The signature changes on
        every write (atomic replace: new inode and mtime)."""
        out = []
        for key, path, st in self._record_paths(name):
            try:
                rec: Optional[dict] = self._load(path)
            except FileNotFoundError:
                continue
            except Exception:
                rec = None
            out.append((key, (st.st_ino, st.st_mtime_ns, st.st_size), rec))
        return out

    def read_consumer(self, name: str, instance: Optional[str] = None) -> Optional[dict]:
        """One record: ``instance``'s (``LEGACY`` = the shared legacy file), or the newest of ``name``."""
        if instance is not None:
            try:
                return self._load(self.consumer_path(name, instance))
            except FileNotFoundError:
                return None
        recs = self.consumer_records(name)
        return recs[0][1] if recs else None

    def consumer_names(self) -> list:
        try:
            entries = sorted(self.consumers_dir.iterdir())
        except OSError:
            return []
        names = set()
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.name.endswith(".json") and p.is_file():
                names.add(p.name[:-5].replace("@", ":"))
            elif p.is_dir():
                names.add(p.name.replace("@", ":"))
        return sorted(names)

    def record_age(self, name: str, instance: Optional[str] = None) -> Optional[float]:
        """Seconds since a record of ``name`` was last written (``instance``'s;
        LEGACY = the legacy file; None = the newest), on the STORE's
        clock: a scratch file is created (and removed) in the same directory
        and the two mtimes are compared, so writer/reader wall-clock skew never
        enters. None when either mtime cannot be read (inconclusive)."""
        if instance is None:
            try:
                paths = self._record_paths(name)
            except OSError:
                return None  # unlistable: inconclusive, never fresh
            path = paths[0][1] if paths else self.consumer_path(name)
        else:
            path = self.consumer_path(name, instance)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".lease-clock.", dir=str(self.consumers_dir))
        except OSError:
            return None
        try:
            now = os.fstat(fd).st_mtime
            written = os.stat(path).st_mtime
        except OSError:
            return None
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return now - written


def _lease_expiry(rec: Mapping[str, Any], default_lease_s: float) -> float:
    lease_s = rec.get("lease_s")
    ok = _finite(lease_s) and float(lease_s) > 0  # type: ignore[arg-type]
    return LEASE_EXPIRY_FACTOR * (float(lease_s) if ok else default_lease_s)  # type: ignore[arg-type]


def _is_leased(rec: Mapping[str, Any]) -> bool:
    tok = rec.get("token")
    return isinstance(tok, str) and bool(tok)


def _lease_fresh(store: HoldStore, name: str, rec: Mapping[str, Any],
                 key: Optional[str] = None) -> Optional[bool]:
    """One-shot reader (no history): a leased record is live while its
    store-relative age is under the lease expiry. None = age unreadable
    (unverifiable, never proof of liveness). ``key`` names the record file
    (an instance, or LEGACY); None = the newest record of ``name``."""
    age = store.record_age(name, key)
    return None if age is None else age < _lease_expiry(rec, DEFAULT_LEASE_S)


# ---------------------------------------------------------------------------
# Consumer side
# ---------------------------------------------------------------------------
@dataclass
class Ticket:
    id: str
    identity: str
    internal: bool
    started_at: float
    _gate: "AdmissionGate" = field(repr=False, default=None)  # type: ignore[assignment]
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._gate._release(self.id)


ActiveWork = Callable[[], "int | Mapping[str, int]"]


class AdmissionGate:
    """Per-process admission fence + acknowledgment publisher."""

    def __init__(self, store: HoldStore, consumer: str, *,
                 active_work: Optional[ActiveWork] = None,
                 serving: Optional[Callable[[], Mapping[str, Any]]] = None,
                 clock: Callable[[], float] = time.time,
                 lease_s: float = DEFAULT_LEASE_S,
                 monotonic: Callable[[], float] = time.monotonic,
                 refresh_s: float = DEFAULT_REFRESH_S):
        self.store = store
        self.consumer = validate_consumer_name(consumer)
        self.instance = uuid.uuid4().hex
        self.token = secrets.token_hex(16)  # lease identity, random per process start
        self.lease_s = float(lease_s)
        self._mono = monotonic
        self._write_lock = threading.RLock()  # mark + write, loop vs lease thread
        self._seq = 0
        self._last_record: Optional[dict] = None
        self.refresh_s = float(refresh_s)  # 0 = write on every tick (the pre-t_dd40e9f2 behaviour)
        self._last_write_at: Optional[float] = None  # our monotonic clock, any write (lease renewals too)
        # Last write of a FRESH snapshot (a new published_at). Kept apart from
        # _last_write_at: a renewal rewrites the old published_at, so counting
        # it would let renewals skip every unchanged publish until it read stale.
        self._last_publish_at: Optional[float] = None
        self._lease_stop = threading.Event()
        self._lease_thread: Optional[threading.Thread] = None
        # foreign lease token -> {"pid", "instance", "alive_at" (our monotonic), "expiry" (s)}
        self._peers: dict = {}
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.boot_ts = clock()
        self.boot_id = _host_boot_id()
        try:
            self.start_offset: Optional[float] = _start_offset(self.pid)
        except Exception:
            self.start_offset = None
        self.time_ns = _time_ns_id() if self.start_offset is not None else None
        self.pid_ns = _pid_ns_id()
        # create_time is ALWAYS recorded (Prism f-4c7533074d58f2a7): the absolute timestamp is the fallback
        # identity when a reader cannot compare the boot-relative offset (other time namespace, no procfs).
        self.create_time: Optional[float] = _create_time(self.pid)
        self._clock = clock
        self._active_work = active_work
        self._serving = serving
        self._lock = threading.RLock()
        self._tickets: dict = {}
        self._foreign: Optional[tuple] = None  # (pid, instance) sharing our name
        self._foreign_sig: Optional[tuple] = None  # (key, file signature) it was captured from

    def set_active_work(self, fn: Optional[ActiveWork]) -> None:
        with self._lock:
            self._active_work = fn

    def set_serving(self, fn: Optional[Callable[[], Mapping[str, Any]]]) -> None:
        with self._lock:
            self._serving = fn

    # -- admission ------------------------------------------------------------
    def _refusal(self, internal: bool) -> Optional[AdmissionRefused]:
        try:
            hold = self.store.read_hold()
        except HoldUnreadable as exc:
            return AdmissionRefused(f"checkout admission state is UNKNOWN ({exc})")
        if hold is not None and hold.refuses(internal=internal):
            return AdmissionRefused(
                f"checkout held ({hold.mode}) by {hold.owner}", epoch=hold.epoch)
        return None

    def check(self, *, internal: bool = False) -> Optional[AdmissionRefused]:
        """Return the refusal (or None) under the admission lock.

        Only safe when the caller registers the work in state that
        ``active_work`` counts WITHOUT yielding to anything that could run a
        :meth:`snapshot` in between (e.g. same asyncio loop tick, or while
        still holding this gate's :meth:`locked` context).
        """
        with self._lock:
            return self._refusal(internal)

    @contextlib.contextmanager
    def locked(self):
        """Hold the admission lock across a caller's check-and-register."""
        with self._lock:
            yield

    def admit(self, identity: str, *, internal: bool = False) -> Ticket:
        with self._lock:
            refusal = self._refusal(internal)
            if refusal is not None:
                raise refusal
            ticket = Ticket(uuid.uuid4().hex, str(identity)[:200], internal,
                            self._clock(), self)
            self._tickets[ticket.id] = ticket
            return ticket

    @contextlib.contextmanager
    def ticket(self, identity: str, *, internal: bool = False):
        t = self.admit(identity, internal=internal)
        try:
            yield t
        finally:
            t.release()

    def _release(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)

    # -- acknowledgment ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            hold_epoch = hold_error = None
            try:
                hold = self.store.read_hold()
                hold_epoch = hold.epoch if hold else None
            except HoldUnreadable as exc:
                hold_error = str(exc)
            tickets = [
                {"identity": t.identity, "internal": t.internal, "age": round(self._clock() - t.started_at, 3)}
                for t in self._tickets.values()
            ]
            work_total: Optional[int] = None
            work_detail: dict = {}
            work_error = None
            if self._active_work is None:
                work_error = "consumer did not register an active-work source"
            else:
                try:
                    raw = self._active_work()
                    if isinstance(raw, Mapping):
                        work_detail = {str(k): int(v) for k, v in raw.items()}
                        # Per entry, not just the sum: +N/-N would cancel to a
                        # false QUIESCENT while work is still active.
                        if any(v < 0 for v in work_detail.values()):
                            raise ValueError("negative work count")
                        work_total = sum(work_detail.values())
                    else:
                        work_total = int(raw)
                    if work_total < 0:
                        raise ValueError("negative work count")
                except Exception as exc:  # evidence failure -> UNKNOWN
                    work_total, work_error = None, f"{type(exc).__name__}: {exc}"
            serving = None
            if self._serving is not None:
                try:
                    serving = dict(self._serving())
                except Exception as exc:
                    serving = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "schema": SCHEMA, "consumer": self.consumer, "pid": self.pid,
                "host": self.host, "instance": self.instance, "boot_ts": self.boot_ts,
                "boot_id": self.boot_id, "start_offset": self.start_offset, "create_time": self.create_time, "time_ns": self.time_ns, "pid_ns": self.pid_ns,
                "token": self.token, "lease_s": self.lease_s,
                "published_at": self._clock(), "hold_epoch": hold_epoch,
                "hold_error": hold_error, "tickets": tickets,
                "active_work": work_total, "active_work_detail": work_detail,
                "active_work_error": work_error, "serving": serving,
            }

    def _mark_shared_record(self, record: dict) -> dict:
        """Flag a consumer name another LIVE publisher also writes under.

        Every publisher writes only its own record (t_3275003d), so this only
        READS the other records of our name and marks OUR record; the reader
        (``evaluate``) also counts live records itself. The mark is the
        survivor-side evidence: a live peer whose record a store-clock step
        makes look expired to a reader still leaves our record shared.
        Liveness of the other publisher is its LEASE, never its pid
        (t_eceadca2): a record carrying a lease ``token`` that is not ours is
        a peer. It stays live until LEASE_EXPIRY_FACTOR * its ``lease_s``
        passes on our monotonic clock without that record being rewritten
        (or it writes a released record).

        Records with no token predate the lease and keep the pid-identity
        check (``_same_live_process``) for one release.
        """
        try:
            scan = self.store.scan_records(self.consumer)
        except Exception:
            scan = []
        # Read our clock AFTER the records (t_142f9fcb): a clock read before the
        # scan predates a write the scan still sees, and a stall in between would
        # anchor that peer early and clear it early by the stall.
        now = self._mono()
        foreign_seen = None
        for key, sig, prev in scan:
            if key == self.instance or not isinstance(prev, dict):
                continue  # our own record; an unreadable one is the reader's UNKNOWN
            if _is_leased(prev):
                tok = prev["token"]
                if tok == self.token:
                    continue
                self._maybe_prune(key, prev)
                if prev.get("lease_released"):
                    self._peers.pop(tok, None)
                    continue
                # Known peer: its record changed since our last scan, so it
                # wrote since then; alive now on our monotonic clock, no
                # wall/mtime comparison (a clock step can never expire a live
                # peer). First sight: the record may be a crashed publisher's
                # leftover (a pid reused after reboot), so age it on the
                # store's own mtime clock.
                known = self._peers.get(tok)
                if known is not None:
                    alive_at = now if known.get("sig") != (key, sig) else known["alive_at"]
                else:
                    age = self.store.record_age(self.consumer, key)
                    alive_at = now if age is None else self._mono() - max(0.0, age)  # clock after the age
                self._peers[tok] = {
                    "pid": prev.get("pid"), "instance": prev.get("instance"),
                    "alive_at": alive_at, "expiry": _lease_expiry(prev, self.lease_s),
                    "sig": (key, sig),
                }
            elif (foreign_seen is None and prev.get("instance") not in (None, self.instance)
                    and prev.get("host") == self.host and prev.get("pid") != self.pid):
                foreign_seen = (key, sig, prev)  # newest legacy-shape record first
        if foreign_seen is not None and foreign_seen[:2] != self._foreign_sig:
            # Captured once per write of that record: the identity check below
            # stays sticky on what we captured, as before t_3275003d.
            _k, _s, prev = foreign_seen
            self._foreign_sig = foreign_seen[:2]
            self._foreign = (prev.get("pid"), prev.get("instance"),
                             {k: prev[k] for k in ("boot_id", "start_offset", "create_time", "time_ns", "pid_ns") if k in prev})
        for tok, peer in list(self._peers.items()):
            if now - peer["alive_at"] >= peer["expiry"]:
                del self._peers[tok]
        foreign = self._foreign
        if foreign is not None and not _same_live_process(foreign[0], foreign[2]):
            self._foreign = foreign = None
        if self._peers:
            peer = max(self._peers.values(), key=lambda p: p["alive_at"])
            record["shared_with"] = {"pid": peer["pid"], "instance": peer["instance"]}
        elif foreign is not None:
            record["shared_with"] = {"pid": foreign[0], "instance": foreign[1]}
        return record

    def _maybe_prune(self, key: str, rec: Mapping[str, Any]) -> None:
        """Remove another instance's record of our name ONLY when its writer
        provably cannot write again: same host and either another kernel boot
        or a dead pid in our pid namespace, and only once its own lease could
        have expired. That covers released records of stopped processes too
        (one file per clean restart must not pile up). A record of a live pid
        is kept whatever it says: that process could publish again, and an
        unlink racing its rewrite would delete a live record. The legacy
        shared file is never removed: a live pre-t_3275003d process may own it."""
        if key == LEGACY or rec.get("host") != self.host:
            return
        age = self.store.record_age(self.consumer, key)
        if age is None or age < _lease_expiry(rec, self.lease_s):
            return  # never sooner than its own lease could expire
        boot, cur_boot = rec.get("boot_id"), self.boot_id
        known_boot = isinstance(boot, str) and bool(boot) and bool(cur_boot)
        other_boot = known_boot and boot != cur_boot
        dead = (known_boot and boot == cur_boot and self.pid_ns is not None
                and rec.get("pid_ns") == self.pid_ns
                and isinstance(rec.get("pid"), int) and not _pid_alive(rec["pid"]))
        if other_boot or dead:
            with contextlib.suppress(OSError, ValueError):
                os.unlink(self.store.consumer_path(self.consumer, key))

    def _marked(self, record: dict) -> dict:
        record = dict(record)
        record.pop("shared_with", None)
        record.pop("lease_released", None)
        return self._mark_shared_record(record)

    def _write(self, record: dict, *, release: bool = False, marked: bool = False) -> dict:
        """Mark + write as one step (the serving loop and the lease thread
        both write); stamps the lease on success. Writes OUR record only."""
        with self._write_lock:
            record = dict(record) if marked else self._marked(record)
            if release:
                record["lease_released"] = True
            self._seq += 1
            record["lease_seq"] = self._seq  # every write is a visible change to a peer's scan
            self.store.write_consumer(self.consumer, record)
            self._last_record = record
            self._last_write_at = self._mono()
            return record

    def _written_within(self, seconds: float, last: Optional[float] = None) -> bool:
        last = self._last_write_at if last is None else last
        return last is not None and self._mono() - last < seconds

    def _publish(self, record: dict) -> dict:
        """Write a fresh snapshot unless it says nothing new (write-if-changed):
        same content as the last record written, and that write younger than
        ``refresh_s``. The peer scan (shared mark, prune) still runs every
        tick, and a change in the shared mark is a change. Returns the record
        now on disk."""
        with self._write_lock:
            record = self._marked(record)
            last = self._last_record
            if (last is not None and self._last_publish_at is not None
                    and self._written_within(self.refresh_s, self._last_publish_at)
                    and not last.get("lease_released")
                    and all(record.get(k) == last.get(k)
                            for k in set(record) | set(last) if k not in _VOLATILE_FIELDS)):
                return last
            written = self._write(record, marked=True)
            self._last_publish_at = self._last_write_at
            return written

    def renew_lease(self) -> Optional[dict]:
        """Rewrite the last record so the lease stays fresh even while the
        serving loop is stalled (its ``published_at`` still ages, so a stalled
        loop reads stale, while a peer still sees this process as alive).
        A renewal racing a stop is dropped so it cannot undo the release.
        Skipped while a publish wrote in the last ``refresh_s`` (at most half
        a lease): that write already renewed it."""
        with self._write_lock:
            last = self._last_record
            if last is None or self._lease_stop.is_set():
                return None
            if self._written_within(min(self.refresh_s, self.lease_s / 2)):
                return last
            return self._write(last)

    def start_lease(self) -> threading.Thread:
        """Renew the lease every ``lease_s`` from a thread independent of the
        serving loop. Idempotent."""
        th = self._lease_thread
        if th is not None and th.is_alive():
            return th
        self._lease_stop.clear()

        def _loop():
            while not self._lease_stop.wait(self.lease_s):
                try:
                    self.renew_lease()
                except Exception:
                    logger.warning("checkout admission lease renewal failed", exc_info=True)

        th = threading.Thread(target=_loop, name=f"checkout-admission-lease:{self.consumer}", daemon=True)
        th.start()
        self._lease_thread = th
        return th

    def stop_lease(self) -> None:
        """Stop renewing and, if we ever published, write a released record so
        a same-name successor (a restart) does not wait out our lease. A crash
        skips this; its lease then expires after LEASE_EXPIRY_FACTOR * lease_s."""
        self._lease_stop.set()
        last = self._last_record
        if last is None:
            return
        try:
            with self._write_lock:
                # Our own record: a successor under this name writes its own
                # file, so the release can never overwrite it (t_3275003d).
                self._write(last, release=True)
        except Exception:
            logger.warning("checkout admission lease release failed", exc_info=True)

    def publish(self) -> dict:
        return self._publish(self.snapshot())

    def start_publisher_thread(self, interval: float = DEFAULT_PUBLISH_INTERVAL,
                               stop: Optional[threading.Event] = None) -> threading.Thread:
        stop = stop or threading.Event()
        self.start_lease()

        def _loop():
            try:
                while not stop.is_set():
                    try:
                        self.publish()
                    except Exception:
                        logger.warning("checkout admission publish failed", exc_info=True)
                    stop.wait(interval)
            finally:
                self.stop_lease()

        th = threading.Thread(target=_loop, name=f"checkout-admission:{self.consumer}", daemon=True)
        th.start()
        return th

    async def publish_forever(self, interval: float = DEFAULT_PUBLISH_INTERVAL) -> None:
        """Asyncio publisher: the snapshot runs ON the loop (so it cannot
        interleave with loop-thread check-and-register code) and its freshness
        proves the loop itself is serving. Only the file write is offloaded;
        the lease is renewed off-loop."""
        import asyncio

        self.start_lease()
        try:
            while True:
                try:
                    record = self.snapshot()
                    await asyncio.to_thread(self._publish, record)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("checkout admission publish failed", exc_info=True)
                await asyncio.sleep(interval)
        finally:
            # The released-record write fsyncs; keep it off the loop like every other write here.
            # A bare executor future, not shield(to_thread(...)): to_thread is a coroutine, so shield
            # wraps it in its own Task, and asyncio.run() shutdown cancels every pending Task. Under a
            # saturated default executor that cancel drops the still-QUEUED job and the released record
            # is never written (Prism ad88cba956ad). shutdown_default_executor() drains a queued future.
            # copy_context().run: keep to_thread's contextvar hand-off (Prism 0d646d6f5979 class).
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(
                None, contextvars.copy_context().run, self.stop_lease))


# ---------------------------------------------------------------------------
# Process wiring (config.yaml -> gate)
# ---------------------------------------------------------------------------
_process_gates: dict = {}
_process_lock = threading.Lock()


def default_directory(code_root: Optional[Path] = None) -> Optional[Path]:
    """``<git-common-dir>/checkout-admission`` of the checkout this code runs from."""
    root = Path(code_root or Path(__file__).resolve().parents[1])
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=True,
        ).stdout.strip()
    except Exception:
        return None
    return Path(out) / "checkout-admission" if out else None


def _load_settings() -> dict:
    import importlib

    try:
        cfg = importlib.import_module("hermes_cli.config").load_config_readonly()
    except Exception:
        logger.warning("checkout admission: config.yaml unreadable; gate disabled", exc_info=True)
        return {}
    section = cfg.get(CONFIG_KEY) if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def gate_from_settings(kind: str, settings: Mapping[str, Any]) -> Optional[AdmissionGate]:
    """Build a gate from a ``checkout_admission`` config section, or None.

    ``consumers`` maps a process kind (``gateway`` / ``serve``) to its consumer
    name, so one profile's config.yaml can name both of its processes.
    A misconfigured-but-enabled section yields None and logs an error: that
    consumer then never acknowledges, so every verdict is UNKNOWN (DEFER)
    rather than a silently open fence.
    """
    if not settings or not settings.get("enabled"):
        return None
    consumers = settings.get("consumers") or {}
    name = consumers.get(kind) if isinstance(consumers, dict) else None
    try:
        name = validate_consumer_name(str(name))
    except ValueError:
        logger.error("checkout admission enabled but consumers.%s is missing/invalid", kind)
        return None
    directory = settings.get("dir")
    path = Path(os.path.expanduser(directory)) if directory else default_directory()
    if path is None:
        logger.error("checkout admission enabled but no shared directory resolved")
        return None
    return AdmissionGate(HoldStore(path), name)


def process_gate(kind: str) -> Optional[AdmissionGate]:
    """The (cached) gate for this process kind, or None when disabled."""
    with _process_lock:
        if kind not in _process_gates:
            _process_gates[kind] = gate_from_settings(kind, _load_settings())
        return _process_gates[kind]


def _reset_process_gates_for_tests() -> None:
    with _process_lock:
        _process_gates.clear()


# ---------------------------------------------------------------------------
# Operator side
# ---------------------------------------------------------------------------
def _not_live_why(store: HoldStore, name: str, rec: Mapping[str, Any],
                  pid_alive: Callable[[int], bool], key: Optional[str] = None) -> Optional[str]:
    """Why ``rec``'s publisher is not live, or None. A leased record is judged
    by its lease only (released, or older than its expiry on the store's mtime
    clock); ``pid_alive`` is consulted only for a legacy record without one.
    ``key`` names the record file (instance or LEGACY)."""
    if _is_leased(rec):
        if rec.get("lease_released"):
            return "publisher released its lease (stopped)"
        fresh = _lease_fresh(store, name, rec, key)
        if fresh is None:
            return "publisher lease age unverifiable (store clock unreadable)"
        if not fresh:
            return "publisher lease expired"
        return None
    return None if pid_alive(rec.get("pid")) else "publishing pid is not alive"


def _acked_idle_under_freeze(rec: Mapping[str, Any], hold: HoldState) -> bool:
    """The record's last write acknowledged THIS freeze epoch with zero work,
    zero tickets and no evidence errors. Freeze refuses every admission, so
    that publisher cannot have admitted work since: its record may drop out
    on lease expiry alone."""
    work, tickets = rec.get("active_work"), rec.get("tickets")
    return (hold.mode == "freeze" and rec.get("hold_epoch") == hold.epoch
            and not rec.get("hold_error") and not rec.get("active_work_error")
            and isinstance(work, int) and not isinstance(work, bool) and work == 0
            and isinstance(tickets, list) and not tickets)


def _publisher_gone(store: HoldStore, name: str, key: str, rec: Mapping[str, Any], *,
                    host: str, pid_alive: Callable[[int], bool],
                    hold: Optional[HoldState] = None) -> bool:
    """True only on POSITIVE evidence that this record's publisher no longer
    publishes AND holds no work: a released lease; an expired lease (store
    clock) whose last write acknowledged the current freeze idle; or a writer
    the pid-identity check shows cannot write again (dead, another boot, a
    reused pid). An expired lease alone only proves the publisher stopped
    renewing: a SIGSTOPped one still holds its tickets (Prism 706463591712).
    Unverifiable age, a foreign host's record or a malformed record is NOT
    gone: it is judged, and judged UNKNOWN."""
    if rec.get("schema") != SCHEMA or rec.get("consumer") != name:
        return False
    if _is_leased(rec):
        if rec.get("lease_released"):
            return True
        if _lease_fresh(store, name, rec, key) is not False:
            return False
        if hold is not None and _acked_idle_under_freeze(rec, hold):
            return True
    # Legacy untokened record, or an expired lease that may still hold work:
    # the same pid-identity check a publisher applies decides; inconclusive
    # stays live.
    if rec.get("host") != host:
        return False
    pid = rec.get("pid")
    return not pid_alive(pid) or not _same_live_process(pid, rec)


def _name_verdict(name: str, recs: list, hold: HoldState, *, now: float, stale_after: float,
                  host: str, pid_alive: Callable[[int], bool], store: HoldStore) -> dict:
    """One row for a listed consumer name from EVERY record published under it
    (t_3275003d: one record per publisher). Records whose publisher is gone
    drop out by their own lease. More than one remaining publisher is
    UNKNOWN (a shared name: one record can never vouch for another
    process's work); exactly one is judged on its own record; none: the
    newest record says why."""
    kw = dict(now=now, stale_after=stale_after, host=host, pid_alive=pid_alive, store=store)
    if not recs:
        return _consumer_verdict(name, None, hold, **kw)
    live = [(k, r) for k, r in recs
            if not _publisher_gone(store, name, k, r, host=host, pid_alive=pid_alive, hold=hold)]
    if len(live) > 1:
        return {"consumer": name, "state": UNKNOWN, "work": None, "publishers": len(live),
                "instances": [r.get("instance") for _k, r in live],
                "why": f"consumer name shared by {len(live)} live publishers"}
    k, r = live[0] if live else recs[0]
    return _consumer_verdict(name, r, hold, key=k, **kw)


def _unlisted_may_be_live(rec: Mapping[str, Any], pid_alive: Callable[[int], bool]) -> bool:
    """An UNLISTED consumer blocks unless we can show it is gone. A lease does
    not prove that by expiring (a SIGSTOPped or stalled publisher stops
    renewing but still holds work), so the same-host pid probe decides: a dead
    pid cannot be the writer -> ignore; a live one -> live, unless its lease
    was released. A reused pid therefore reads live: fail safe (an extra
    DEFER), and only for consumers the hold does not name."""
    if not pid_alive(rec.get("pid")):
        return False
    return not (_is_leased(rec) and rec.get("lease_released"))


def _consumer_verdict(name: str, rec: Optional[dict], hold: HoldState, *, now: float,
                      stale_after: float, host: str,
                      pid_alive: Callable[[int], bool],
                      store: HoldStore, key: Optional[str] = None) -> dict:
    out: dict = {"consumer": name, "state": UNKNOWN, "why": None, "work": None}
    if rec is None:
        out["why"] = "no acknowledgment record"
        return out
    if rec.get("schema") != SCHEMA or rec.get("consumer") != name:
        out["why"] = "record schema/name mismatch"
        return out
    out.update(pid=rec.get("pid"), instance=rec.get("instance"),
               tickets=rec.get("tickets"), detail=rec.get("active_work_detail"))
    if rec.get("host") != host:
        out["why"] = f"record from foreign host {rec.get('host')!r}; pid liveness unverifiable"
        return out
    why = _not_live_why(store, name, rec, pid_alive, key)
    if why:
        out["why"] = why
        return out
    try:
        age = now - float(rec.get("published_at"))
    except (TypeError, ValueError):
        out["why"] = "record has no timestamp"
        return out
    out["age"] = round(age, 3)
    if age > stale_after or age < -stale_after:
        out["why"] = f"record stale ({age:.1f}s > {stale_after}s)"
        return out
    if rec.get("hold_error"):
        out["why"] = f"consumer cannot read hold: {rec['hold_error']}"
        return out
    if rec.get("shared_with"):
        out["why"] = f"consumer name shared by another live process {rec['shared_with']!r}"
        return out
    if rec.get("hold_epoch") != hold.epoch:
        out["why"] = "current hold epoch not yet acknowledged"
        return out
    work = rec.get("active_work")
    tickets = rec.get("tickets")
    if not isinstance(work, int) or not isinstance(tickets, list):
        out["why"] = rec.get("active_work_error") or "active-work evidence missing"
        return out
    total = work + len(tickets)
    out["work"] = total
    out["state"] = BUSY if total else QUIESCENT
    out["why"] = None
    return out


def evaluate(store: HoldStore, *, now: Optional[float] = None,
             stale_after: float = DEFAULT_STALE_AFTER,
             pid_alive: Callable[[int], bool] = _pid_alive,
             host: Optional[str] = None) -> dict:
    now = time.time() if now is None else now
    host = host or socket.gethostname()
    try:
        hold = store.read_hold()
    except HoldUnreadable as exc:
        return {"verdict": UNKNOWN, "why": str(exc), "consumers": []}
    if hold is None:
        return {"verdict": NOT_HELD, "why": "no hold engaged", "consumers": []}
    rows = []
    for name in hold.expected:
        try:
            recs = store.consumer_records(name)
        except Exception as exc:
            rows.append({"consumer": name, "state": UNKNOWN, "why": f"record unreadable: {exc}", "work": None})
            continue
        rows.append(_name_verdict(name, recs, hold, now=now, stale_after=stale_after,
                                  host=host, pid_alive=pid_alive, store=store))
    # A live consumer the operator forgot to list would be mutated under.
    for name in store.consumer_names():
        if name in hold.expected:
            continue
        try:
            recs = store.consumer_records(name)
        except Exception as exc:
            # Cannot tell whether an unlisted consumer is live: fail closed.
            rows.append({"consumer": name, "state": UNKNOWN, "work": None,
                         "why": f"unexpected consumer record unreadable: {exc}"})
            continue
        # Freshness of published_at is NOT a filter here: a live publisher whose
        # serving loop stalled may still hold work (C7 k90). A released lease
        # clears it; an expired one needs the pid probe to show it dead.
        if any(rec and rec.get("host") == host and _unlisted_may_be_live(rec, pid_alive)
               for _key, rec in recs):
            rows.append({"consumer": name, "state": UNKNOWN, "work": None,
                         "why": "live consumer not in the hold's expected set"})
    states = {r["state"] for r in rows}
    verdict = UNKNOWN if UNKNOWN in states else BUSY if BUSY in states else QUIESCENT
    # In drain mode internal continuations may still be admitted after the
    # snapshot, so zero work there only means "ready to freeze". Mutation
    # authority is QUIESCENT under a freeze epoch, nothing else.
    if verdict == QUIESCENT and hold.mode != "freeze":
        verdict = DRAINED
    return {"verdict": verdict, "epoch": hold.epoch, "mode": hold.mode,
            "owner": hold.owner, "consumers": rows}


def wait_for_quiescence(store: HoldStore, timeout: float, *, poll: float = 1.0,
                        stale_after: float = DEFAULT_STALE_AFTER,
                        sleep: Callable[[float], None] = time.sleep,
                        monotonic: Callable[[], float] = time.monotonic,
                        **kw) -> dict:
    """Poll :func:`evaluate` until QUIESCENT (freeze) / DRAINED (drain).
    Never signals, never kills.

    On deadline the last report is returned with ``verdict=DEFER`` (and the
    underlying state in ``last``); NOT_HELD returns immediately.
    """
    deadline = monotonic() + max(0.0, timeout)
    while True:
        report = evaluate(store, stale_after=stale_after, **kw)
        if report["verdict"] in (QUIESCENT, DRAINED, NOT_HELD):
            return report
        remaining = deadline - monotonic()
        if remaining <= 0:
            return dict(report, verdict=DEFER, last=report["verdict"])
        sleep(min(poll, remaining))


def probe(store: HoldStore, consumer: str, *, url: Optional[str] = None,
          timeout: float = 3.0, stale_after: float = DEFAULT_STALE_AFTER,
          now: Optional[float] = None, pid_alive: Callable[[int], bool] = _pid_alive,
          http_get: Optional[Callable[[str, float], tuple]] = None) -> dict:
    """Non-mutating serving probe: fresh loop-published record + optional GET."""
    now = time.time() if now is None else now
    out: dict = {"consumer": consumer, "ok": False}
    try:
        rec = store.read_consumer(consumer)
    except Exception as exc:
        out["why"] = f"record unreadable: {exc}"
        return out
    if rec is None:
        out["why"] = "no record: consumer never published (not running the gate?)"
        return out
    age = now - float(rec.get("published_at") or 0)
    out.update(pid=rec.get("pid"), instance=rec.get("instance"), age=round(age, 3),
               serving=rec.get("serving"), hold_epoch=rec.get("hold_epoch"))
    if rec.get("host") != socket.gethostname() or _not_live_why(store, consumer, rec, pid_alive):
        out["why"] = "publisher not alive on this host"
        return out
    if age > stale_after:
        out["why"] = f"record stale ({age:.1f}s): serving loop not publishing"
        return out
    serving = rec.get("serving")
    if isinstance(serving, dict) and serving.get("ok") is False:
        out["why"] = f"consumer reports not serving: {serving}"
        return out
    if url:
        getter = http_get or _http_get
        try:
            status, body = getter(url, timeout)
        except Exception as exc:
            out["why"] = f"GET {url} failed: {type(exc).__name__}: {exc}"
            return out
        out["http_status"] = status
        if status != 200:
            out["why"] = f"GET {url} -> {status}"
            return out
    out["ok"] = True
    return out


def _http_get(url: str, timeout: float) -> tuple:
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied loopback URL
        return resp.status, resp.read(65536)


def _gh_check_runs(slug: str) -> Callable[[str], list]:
    def fetch(sha: str) -> list:
        out = subprocess.run(
            ["gh", "api", "--paginate", "--jq", ".check_runs[] | {name, status, conclusion}",
             f"repos/{slug}/commits/{sha}/check-runs"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=True,
        ).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    return fetch


OK_CONCLUSIONS = {"success", "skipped", "neutral"}


def check_pin(sha: str, *, repo: os.PathLike | str, remote_ref: str,
              check_runs: Callable[[str], list],
              run: Callable[..., Any] = subprocess.run) -> dict:
    """Contract for the SHA a cutover may merge: 40-hex, present locally, an
    ancestor of (i.e. landed on) ``remote_ref``, and CI-green (every check run
    completed with success/skipped/neutral, at least one success)."""
    problems = []
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        return {"ok": False, "sha": sha, "problems": ["pin must be a full 40-char lowercase hex SHA"]}

    def _git(*args) -> int:
        return run(["git", "-C", str(repo), *args], capture_output=True, text=True).returncode

    # A local tracking ref can be stale (e.g. after a remote force-push):
    # refresh it before trusting ancestry (C7 k92).
    remote, _, branch = remote_ref.partition("/")
    if not (remote and branch) or _git("fetch", "--quiet", remote, branch) != 0:
        problems.append(f"could not refresh {remote_ref} from the remote")
    elif _git("cat-file", "-e", f"{sha}^{{commit}}") != 0:
        problems.append("commit not present locally (fetch first)")
    elif _git("merge-base", "--is-ancestor", sha, remote_ref) != 0:
        problems.append(f"commit is not on {remote_ref}")
    try:
        runs = check_runs(sha)
    except Exception as exc:
        runs = None
        problems.append(f"CI provenance unavailable: {type(exc).__name__}: {exc}")
    if runs is not None:
        if not runs:
            problems.append("no CI check runs recorded for this commit")
        bad = [r for r in runs if r.get("status") != "completed" or r.get("conclusion") not in OK_CONCLUSIONS]
        if bad:
            problems.append("non-green checks: " + ", ".join(
                f"{r.get('name')}={r.get('conclusion') or r.get('status')}" for r in bad[:10]))
        if runs and not any(r.get("conclusion") == "success" for r in runs):
            problems.append("no successful check run")
    return {"ok": not problems, "sha": sha, "problems": problems}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
EXIT = {QUIESCENT: 0, DRAINED: 1, NOT_HELD: 2, BUSY: 3, DEFER: 3, UNKNOWN: 4}


def _resolve_store(args) -> HoldStore:
    directory = args.dir or (_load_settings().get("dir")) or default_directory()
    if not directory:
        raise SystemExit("cannot resolve admission directory; pass --dir")
    return HoldStore(os.path.expanduser(str(directory)))


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m gateway.checkout_admission",
                                description="Shared-checkout admission hold (operator CLI).")
    p.add_argument("--dir", help="shared admission directory (default: config or <git-common-dir>/checkout-admission)")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hold", help="engage/re-epoch the hold (never opens it)")
    h.add_argument("--owner", required=True)
    h.add_argument("--expect", action="append", required=True, help="consumer name; repeat")
    h.add_argument("--mode", choices=MODES, default="drain")
    h.add_argument("--reason", default="")
    h.add_argument("--recover-unreadable", action="store_true",
                   help="take over an unreadable hold.json (its owner is unknown)")
    r = sub.add_parser("release", help="explicitly open the hold")
    r.add_argument("--owner", required=True)
    s = sub.add_parser("status", help="one verdict (exit 0 QUIESCENT[freeze], 1 DRAINED[drain], 2 NOT_HELD, 3 BUSY, 4 UNKNOWN)")
    s.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER)
    w = sub.add_parser("wait", help="wait for QUIESCENT; DEFER (exit 3) on timeout, never signals")
    w.add_argument("--timeout", type=float, required=True)
    w.add_argument("--poll", type=float, default=1.0)
    w.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER)
    pr = sub.add_parser("probe", help="non-mutating serving probe for one consumer")
    pr.add_argument("consumer")
    pr.add_argument("--url", help="optional GET health URL, e.g. http://127.0.0.1:9121/api/health")
    pr.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER)
    pc = sub.add_parser("pin-check", help="validate a 40-hex CI-green SHA on the remote ref")
    pc.add_argument("sha")
    pc.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    pc.add_argument("--remote-ref", default="origin/main")
    pc.add_argument("--slug", required=True, help="GitHub owner/repo for check runs")
    args = p.parse_args(argv)

    if args.cmd == "pin-check":
        res = check_pin(args.sha, repo=args.repo, remote_ref=args.remote_ref,
                        check_runs=_gh_check_runs(args.slug))
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 5
    store = _resolve_store(args)
    if args.cmd == "hold":
        state = store.hold(args.owner, args.expect, mode=args.mode, reason=args.reason,
                           recover_unreadable=args.recover_unreadable)
        print(json.dumps(state.to_json(), indent=2))
        return 0
    if args.cmd == "release":
        store.release(args.owner)
        print(json.dumps({"released": True}))
        return 0
    if args.cmd == "status":
        rep = evaluate(store, stale_after=args.stale_after)
    elif args.cmd == "wait":
        rep = wait_for_quiescence(store, args.timeout, poll=args.poll, stale_after=args.stale_after)
    else:
        res = probe(store, args.consumer, url=args.url, stale_after=args.stale_after)
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 6
    print(json.dumps(rep, indent=2))
    return EXIT[rep["verdict"]]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
