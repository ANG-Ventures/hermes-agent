# SCHEDULE: manual (library imported by each class's own scheduler; nothing runs this file on a cadence)
"""admission_demand: the I/O half of the ONE admission rule (t_e5124426). admission.admit_class() is pure; this module
reads its two inputs from disk and gives every caller the same kill switch.

  load_priority(path=None) -> admission.Priority     fleet/admission-priority.json (order, line, holdback_units)
  store_door(req, *, owner, root=None, now=None)    the ONE writer of var/placement/demand/<workload>.json, called
                                                     only by allocd's POST /demand/publish (alloc_client is the client:
                                                     enqueue / withdraw / mark_asap / publish_queue). There is no local
                                                     publish path (t_810c9012 round 4 cut): one owner model, the pair.
  read_demand(host, root=None, now=None, resolve=None) -> [admission.Demand]
                                                     every class's FRESH queue that may run on ``host``
  unified(workload, env=None) -> bool                the per-caller kill switch: <WORKLOAD>_UNIFIED_ADMIT=0 = the
                                                     caller's old local rule (one release, card step 4)
  published_unified(workload, root=None, now=None) -> bool | None
                                                     that switch as the class's admitter published it (demand file
                                                     ``unified``), for a reader in another process

Demand file (one per class, atomic replace; the class's own scheduler writes it every tick):
  {"schema": 1, "workload": "prism", "at": <epoch>, "queued": 64, "hosts": ["ci-box", "ace-ai"] | null,
   "unit": {"cpus": 1.0, "ram_gb": 4.0, "gpu_mib": 0}, "unified": true}
``unified`` = the publisher's <WORKLOAD>_UNIFIED_ADMIT switch (absent in a file from an older publisher).
``hosts`` null = may run anywhere. A file older than demand_max_age_s, unreadable, or malformed holds nothing back
(said once on stderr): a dead publisher must never starve the other classes, and the host line still protects the box.
"""
from __future__ import annotations

import contextlib
import contextvars
import errno
import secrets
import stat
import dataclasses
import fcntl
import hashlib
import json
import math
import pwd
import re
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Mapping, Optional

try:
    from . import admission
except ImportError:  # scripts/lib on sys.path
    import admission

SCHEMA = 1
PRIORITY_REL = Path("fleet") / "admission-priority.json"
# Every priority_mode priority_from_doc accepts. scripts/lib/priority_view.py MODE_TEXT describes each one;
# scripts/tests/test_priority_view.py fails when a mode is added here without a description (t_8b071574).
PRIORITY_MODES = ("spawn-only", "fcfs", "classes")
DEMAND_REL = Path("var") / "placement" / "demand"
DEFAULT_MAX_AGE_S = 120.0
_SAID: set = set()


def _root(root: Optional[Path] = None) -> Path:
    if __package__ == "fleet_admission":
        from .roots import resolve_root
        return resolve_root(root, source_file=__file__, package=__package__)
    if root is not None:
        return Path(root)
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2]
    try:   # t_5d7db1b6: a release of the fleet home answers the home (its receipt names the home, never a release)
        from fleet_root import release_home
    except ImportError:   # a partial copy without the resolver is never a release
        return here
    return Path(release_home(here) or here)


def _say(key: str, msg: str) -> None:
    if key not in _SAID:
        _SAID.add(key)
        sys.stderr.write(f"admission_demand: {msg}\n")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _read_policy(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say(f"policy:{path}", f"{path} unreadable ({exc}); built-in priority defaults")
        return {}
    return doc if isinstance(doc, dict) else {}


def validate_priority(doc: Mapping) -> List[str]:
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    order = doc.get("order")
    if not (isinstance(order, list) and order and all(isinstance(x, str) and x for x in order)
            and len(set(order)) == len(order)):
        errs.append("order must be a non-empty list of distinct class names")
    line = doc.get("line")
    if not (_num(line) and 0 < line <= admission.HOST_LINE):
        errs.append(f"line must be a number in (0, {admission.HOST_LINE}] (Ace: no host above 90 % busy)")
    hb = doc.get("holdback_units")
    if not (isinstance(hb, int) and not isinstance(hb, bool) and 0 <= hb <= 64):
        errs.append("holdback_units must be an int in [0, 64]")
    age = doc.get("demand_max_age_s")
    if not (_num(age) and age > 0):
        errs.append("demand_max_age_s must be a number > 0")
    return errs


def priority_from_doc(doc: Mapping) -> admission.Priority:
    """Normalize independently: one bad field never discards a valid line/order."""
    defaults = admission.Priority()
    # Only a foreign schema discards the file. Every field, legacy or additive, degrades on its own:
    # one bad holdback_units never resets a valid line/order/priority_mode (round 2, finding 4).
    if doc.get("schema", SCHEMA) != SCHEMA:
        _say(f"schema:{doc.get('schema')!r}", f"priority schema must be {SCHEMA}; built-in defaults")
        return defaults
    values = {}
    checks = {
        "order": lambda v: isinstance(v, (list, tuple)) and bool(v) and all(isinstance(x, str) and x for x in v)
                 and len(set(v)) == len(v),
        "line": lambda v: _num(v) and 0 < v <= admission.HOST_LINE,
        "holdback_units": lambda v: type(v) is int and 0 <= v <= 64,
        "demand_max_age_s": lambda v: _num(v) and v > 0,
        "priority_mode": lambda v: v in PRIORITY_MODES,
        "door_window_units": lambda v: type(v) is int and 1 <= v <= 64,
        "door_hold_max_s": lambda v: _num(v) and v > 0,
        "verdict_trust_cpu": lambda v: _num(v) and v >= 0,
        "asap_max_units_per_host": lambda v: type(v) is int and 0 <= v <= 64,
        "preempt_budget_per_tick": lambda v: type(v) is int and 0 <= v <= 64,
    }
    for name, check in checks.items():
        value = doc.get(name, getattr(defaults, name))
        if not check(value):
            _say(f"field:{name}:{value!r}", f"invalid {name}; using default")
            value = getattr(defaults, name)
        values[name] = tuple(value) if name == "order" else value
    classes = doc.get("classes", {})
    values["classes"] = {}
    if isinstance(classes, dict):
        for name, fields in classes.items():
            if not isinstance(fields, dict):
                continue
            values["classes"][name] = {
                "footprint_class": fields.get("footprint_class", name) if isinstance(fields.get("footprint_class", name), str) else name,
                "age_step_s": fields.get("age_step_s", 1800) if fields.get("age_step_s", 1800) in (0, 1800) else 1800,
                "yield_on": fields.get("yield_on", "start") if fields.get("yield_on", "start") in ("start", "growth") else "start"}
    return admission.Priority(**values)


def load_priority(path=None, root=None) -> admission.Priority:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    return priority_from_doc(_read_policy(p))


def priority_document(*, root=None) -> dict:
    p = _root(root) / PRIORITY_REL
    try:
        raw = p.read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        raw, doc = b"", {}
    return {"priority": dataclasses.asdict(priority_from_doc(doc)),
            "priority_sha256": hashlib.sha256(raw).hexdigest()}


def max_age_s(path=None, root=None) -> float:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    doc = _read_policy(p)
    v = doc.get("demand_max_age_s") if doc else None
    return float(v) if _num(v) and v > 0 else DEFAULT_MAX_AGE_S


def _name(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value) is not None


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NONCE = ".placement-root-generation"
_ACTIVE_DIRECTORIES: contextvars.ContextVar[tuple] = contextvars.ContextVar("allocation_active_directories", default=())
#: Per-request stage clock (t_bb190bec). allocd installs a callable for one request: clock(label) charges the time
#: since the last switch to the current label, makes ``label`` current and returns the previous one. Storage switches
#: only at boundaries the caller cannot see (root setup with its fsyncs, writer flock wait) and restores the caller's
#: label. Default None: every other caller and every vendored copy runs exactly as before.
STAGE_MARK: contextvars.ContextVar = contextvars.ContextVar("allocation_stage_mark", default=None)


@contextlib.contextmanager
def _stage(label: str):
    clock = STAGE_MARK.get()
    if clock is None:
        yield
        return
    previous = clock(label)
    try:
        yield
    finally:
        clock(previous)


def validate_active_directories():
    for directory in _ACTIVE_DIRECTORIES.get():
        directory.validate()


def _identity(fd):
    st = os.fstat(fd)
    return [st.st_dev, st.st_ino]


class RootRefused(OSError):
    """The provisioned fleet root or its receipt no longer proves itself: a real replacement, never a transient.
    allocd answers it 503 ``reason: root_refused`` (alloc_client raises it as a terminal Refused); the operator
    re-proves the root (allocd.py --provision-root). Subclasses OSError so every existing storage catch still fires.
    ``on_refused`` (one per thread, set by allocd per request) is told of every refusal, even one a route swallows."""

    on_refused: contextvars.ContextVar = contextvars.ContextVar("root_refused_hook", default=None)

    def __init__(self, reason):
        super().__init__(reason)
        hook = RootRefused.on_refused.get()
        if hook is not None:
            hook(self)


#: t_f9dd5ccc: a receipt replaced by a symlink (ELOOP under O_NOFOLLOW), a receipt parent replaced by a regular file
#: (ENOTDIR) or a receipt replaced by a directory (EISDIR) is a structural replacement, not a transient I/O fault.
_STRUCTURAL_ERRNOS = (errno.ELOOP, errno.ENOTDIR, errno.EISDIR)


def _refuse_structural(exc: OSError) -> None:
    """Re-raise a structural-replacement OSError from the receipt/root walk as RootRefused; anything else returns."""
    if not isinstance(exc, RootRefused) and exc.errno in _STRUCTURAL_ERRNOS:
        raise RootRefused(f"receipt path replaced ({errno.errorcode[exc.errno]}: {exc.filename or exc})") from exc

def _same_across_boots(recorded, current):
    """t_1cfb3845: a receipt's persisted [st_dev, st_ino] compared across a reboot is the INODE only.

    macOS APFS (Studio 2026-11-11 17:11: 16777234 -> 16777232, same inode, same nonce), Linux anonymous devices
    (ZFS / overlay / btrfs: major 0, numbered at mount) and NVMe partitions (259:N, N in probe order) renumber st_dev
    at boot, so comparing it refused every write after a reboot. st_dev was there to catch a DIFFERENT filesystem
    under the same path with a colliding inode; the root's random 192-bit generation nonce (checked right after
    this) still catches that, as it catches any recreate. Within one process st_dev is stable, so every live
    fd-versus-path check keeps the full [st_dev, st_ino].

    Known residual (accepted, not built for): inode + nonce cannot tell a device renumber from a snapshot/clone of
    the root mounted in its place (same inode, same nonce, different filesystem), so a rolled-back or cloned root
    mounted at the same path is accepted. The old full-pair rule did not catch an in-place rollback either, and it
    refused every write after every reboot. Pending provisioning (provision_root) has no nonce yet and keeps the
    strict pair."""
    return (isinstance(recorded, list) and len(recorded) == 2 and len(current) == 2
            and recorded[1] == current[1])


def _absolute(path):
    # Do not resolve symlinks: the final component must be checked by O_NOFOLLOW.
    path = Path(path)
    if ".." in path.parts:
        raise OSError("parent traversal forbidden")
    return Path(os.path.abspath(path))


def _read_at(fd, name, *, bounded=False):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(handle, "r", encoding="utf-8") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError("regular file required")
        text = fh.read(65537) if bounded else fh.read()
        if bounded and len(text) > 65536:
            raise OSError("receipt too large")
        return text


def _note_write(name, nbytes, started):
    """t_2f30f5b5: tell the request's clock which whole file it rewrote, how big, and how long it took."""
    clock = STAGE_MARK.get()
    note = getattr(clock, "note_write", None)
    if note is not None:
        note(name, nbytes, time.monotonic() - started)


#: t_1d801469: who HOLDS the ledger flock / demand writer, not only who waits. Every hold of either lock adds its held
#: time to the current request's clock (span ``ledger_flock_held`` / ``demand_writer_held``), and any hold of
#: HOLD_LOG_S or more, in ANY process that locks through these two functions (allocd, alloc_client's local fallback,
#: test-gate), appends one line to var/placement/flock-holds.jsonl: {at, lock, held_ms, wait_ms, pid, proc, caller,
#: route, consumer}. A WAIT of HOLD_LOG_S or more is logged too: a long wait with no logged hold over the same
#: interval was held by a process with its own placement copy (the kanban dispatcher's hermes_cli.fleet_placement).
#: Diagnostics only: no fsync, never raises.
HOLDS_REL = Path("var") / "placement" / "flock-holds.jsonl"
HOLD_LOG_S = 0.25
HOLDS_MAX_BYTES = 8 * 1024 * 1024
HOLD_LINES_DROPPED = 0      # lines dropped because another process held the log's sidecar flock (never waited for)
_HOLD_SKIP = ("admission_demand.py:_writer", "placement.py:_locked", "admission_demand.py:_note_hold",
              "allocd.py:door_locks")


def _hold_caller() -> str:
    """file:function:line of the innermost frame that is not the lock machinery (the code that held the lock)."""
    f = sys._getframe(2)
    while f is not None:
        name = os.path.basename(f.f_code.co_filename)
        if name != "contextlib.py" and f"{name}:{f.f_code.co_name}" not in _HOLD_SKIP:
            return f"{name}:{f.f_code.co_name}:{f.f_lineno}"
        f = f.f_back
    return "?"


def _note_hold(lock: str, log_path, waited: float, held: float) -> None:
    try:
        clock = STAGE_MARK.get()
        spans = getattr(clock, "spans", None)
        if isinstance(spans, dict):
            spans[f"{lock}_held"] = spans.get(f"{lock}_held", 0.0) + held
        if held < HOLD_LOG_S and waited < HOLD_LOG_S:
            return
        at = time.time()  # fixture-clock-ok: diagnostics stamp only, joins live logs; no test reads it
        line = {"at": at, "lock": lock, "held_ms": round(held * 1000.0, 1),
                "wait_ms": round(waited * 1000.0, 1), "pid": os.getpid(),
                "proc": os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else sys.executable,
                "caller": _hold_caller(), "route": getattr(clock, "route", None),
                "consumer": getattr(clock, "consumer", None)}
        path = Path(log_path)
        data = (json.dumps(line, sort_keys=True) + "\n").encode()
        # The log is shared across processes: check, rotate and append under one sidecar flock, so a second
        # rotator cannot replace the full .1 archive with the fresh active file another process just made.
        # Never WAIT for it (a caller may still hold the demand writer): busy = drop the line and count the drop.
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                global HOLD_LINES_DROPPED
                HOLD_LINES_DROPPED += 1
                if clock is not None and hasattr(clock, "hold_lines_dropped"):
                    clock.hold_lines_dropped += 1
                return
            try:
                if os.lstat(path).st_size + len(data) > HOLDS_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)  # closing the only fd on the open file description drops the flock
    except Exception as exc:  # noqa: BLE001 - a diagnostics fault never touches the lock holder
        _say(f"holds:{type(exc).__name__}", f"flock hold line not written: {exc}")


def _atomic_at(fd, name, data, mode=0o644):
    started = time.monotonic()
    tmp = ".placement-" + secrets.token_hex(12)
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            # t_2f30f5b5: one stage per syscall class, so a multi-second hold names write vs fsync vs rename
            with _stage("store_write"):
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), mode)
            with _stage("store_fsync"):
                os.fsync(fh.fileno())
        with _stage("store_rename"):
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        with _stage("store_dir_fsync"):
            os.fsync(fd)
        _note_write(name, len(data), started)
    finally:
        try:
            os.unlink(tmp, dir_fd=fd)
        except FileNotFoundError:
            pass


def _open_receipt_parent(path):
    """Walk from the existing filesystem anchor without following any symlink.

    Only traversal fds are retained during lookup; no ancestor is read or synced.
    """
    flags = (getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY))
             | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = os.open(path.anchor, flags)
    try:
        parts = path.parent.parts[1:]
        for index, part in enumerate(parts):
            child = os.open(part, _DIR_FLAGS if index == len(parts) - 1 else flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if not parts:
            child = os.open(".", _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _receipt_path(root, receipt=None):
    supplied = receipt if receipt is not None else os.environ.get("PLACEMENT_ROOT_RECEIPT")
    if not supplied:
        raise OSError("PLACEMENT_ROOT_RECEIPT required; provision fleet root explicitly")
    path = _absolute(supplied)
    if path == root or root in path.parents:
        raise OSError("receipt must be external to fleet root")
    return path


def provision_root(root, *, anchor, receipt):
    """Offline installation boundary; caller excludes runtime writers/root replacement.

    Anchor and external receipt parent are operator-attested already durable directories.
    Re-run with the SAME inputs after interruption; never promote pending from runtime.
    """
    root, anchor = _absolute(root), _absolute(anchor)
    receipt = _receipt_path(root, receipt)
    try:
        parts = root.relative_to(anchor).parts
    except ValueError as exc:
        raise OSError("root must descend from explicit durable anchor") from exc
    if not parts or len(parts) > 64:
        raise OSError("root chain requires 1..64 components")
    # Receipt parent must predate every directory this operation may create.
    first = anchor / parts[0]
    if receipt == first or first in receipt.parents:
        raise OSError("receipt is inside provisioned ancestor chain")
    fds = []
    try:
        afd = os.open(anchor, _DIR_FLAGS)
        fds.append(afd)
        rfd = _open_receipt_parent(receipt)
        fds.append(rfd)
        # Reject aliases through symlink ancestors as well as lexical containment.
        if first.resolve() == receipt.parent.resolve() or first.resolve() in receipt.parent.resolve().parents:
            raise OSError("receipt aliases provisioned ancestor chain")
        intent = {"schema": 1, "state": "pending", "root": str(root), "anchor": str(anchor),
                  "anchor_identity": _identity(afd), "generation": secrets.token_hex(24)}
        try:
            old = json.loads(_read_at(rfd, receipt.name, bounded=True))
        except FileNotFoundError:
            old = None
        except (ValueError, TypeError) as exc:
            raise OSError("malformed provisioning intent; operator reconciliation required") from exc
        if old is not None and not isinstance(old, dict):
            raise OSError("malformed provisioning intent; operator reconciliation required")
        if old is not None and old.get("state") == "pending":
            # Strict [st_dev, st_ino] here, on purpose (round 2): a pending intent has no nonce yet, and a mount
            # point's inode is the same on any filesystem mounted there, so inode alone cannot bind the anchor.
            for key in ("schema", "root", "anchor", "anchor_identity"):
                if old.get(key) != intent[key]:
                    raise OSError("pending intent changed; operator reconciliation required")
            intent = old
        _atomic_at(rfd, receipt.name, json.dumps(intent, sort_keys=True))
        chain = [afd]
        for part in parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=chain[-1])
            except FileExistsError:
                pass
            child = os.open(part, _DIR_FLAGS, dir_fd=chain[-1])
            fds.append(child)
            chain.append(child)
        _atomic_at(chain[-1], _NONCE, intent["generation"])
        for fd in reversed(chain):
            os.fsync(fd)
        st = os.stat(root, follow_symlinks=False)
        if _identity(chain[-1]) != [st.st_dev, st.st_ino]:
            raise OSError("root changed during provisioning")
        ready = {**intent, "state": "ready", "root_identity": _identity(chain[-1])}
        _atomic_at(rfd, receipt.name, json.dumps(ready, sort_keys=True))
        return ready
    finally:
        for fd in reversed(fds):
            os.close(fd)


class RootDirectory:
    """Retained fd chain; all mutation is relative, never mkdir -p from a path."""
    #: t_f9dd5ccc: told of every validate() that PASSED on this thread (allocd sets it per request). A refused-root
    #: episode closes only on a 2xx whose request proved the root here, never on a 2xx that never opened it.
    on_validated: contextvars.ContextVar = contextvars.ContextVar("root_validated_hook", default=None)

    def __init__(self, root, receipt, rfd, doc, fds, parts):
        self.root, self.receipt, self.rfd, self.doc = root, receipt, rfd, doc
        self.fds, self.parts = fds, parts
        self.fd = fds[-1]

    def validate(self):
        try:
            current = json.loads(_read_at(self.rfd, self.receipt.name, bounded=True))
            parent_fd = _open_receipt_parent(self.receipt)
            try:
                if _identity(parent_fd) != _identity(self.rfd):
                    raise RootRefused("receipt parent replaced")
            finally:
                os.close(parent_fd)
            st = os.stat(self.root, follow_symlinks=False)
            live = _identity(self.fds[0])
            # live fd vs path: full [st_dev, st_ino]; receipt vs live: inode + nonce (_same_across_boots)
            if (current != self.doc or [st.st_dev, st.st_ino] != live
                    or not _same_across_boots(self.doc["root_identity"], live)):
                raise RootRefused("provisioned root/receipt replaced")
            if _read_at(self.fds[0], _NONCE, bounded=True) != self.doc["generation"]:
                raise RootRefused("root generation changed")
            for i, part in enumerate(self.parts):
                st = os.stat(part, dir_fd=self.fds[i], follow_symlinks=False)
                if [st.st_dev, st.st_ino] != _identity(self.fds[i + 1]):
                    raise RootRefused("descendant chain replaced")
        except FileNotFoundError as exc:
            raise RootRefused(f"root or receipt missing: {exc.filename or exc}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise RootRefused("invalid root receipt") from exc
        except OSError as exc:
            _refuse_structural(exc)
            raise
        hook = RootDirectory.on_validated.get()
        if hook is not None:
            hook()

    def __truediv__(self, name):
        if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
            raise OSError("single filename required")
        return RootFile(self, name)

    def replace(self, name, data, mode=0o644):
        self.validate()
        _atomic_at(self.fd, name, data, mode)
        self.validate()


class RootFile:
    """The Path operations used by demand and ledger storage, bound to a directory fd."""
    def __init__(self, directory, name):
        self.parent, self.name = directory, name

    def __str__(self):
        return str(self.parent.root.joinpath(*self.parent.parts, self.name))

    def stat(self):
        return os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)

    def exists(self):
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def is_symlink(self):
        try:
            return stat.S_ISLNK(self.stat().st_mode)
        except FileNotFoundError:
            return False

    _MODES = {"r": os.O_RDONLY, "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
              "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a+": os.O_RDWR | os.O_CREAT | os.O_APPEND}

    def open(self, mode="r", encoding=None):
        if mode not in self._MODES:   # Prism 699dec37cb76/74b201659f91: never map an unknown mode onto append
            raise ValueError(f"unsupported RootFile mode {mode!r}")
        fd = os.open(self.name, self._MODES[mode] | os.O_NOFOLLOW, 0o644, dir_fd=self.parent.fd)
        return os.fdopen(fd, mode, encoding=encoding)

    def read_text(self, encoding="utf-8"):
        return _read_at(self.parent.fd, self.name)


#: t_5e814402: receipt versions whose file + parent directory THIS process has fsync'd. The receipt only changes at
#: provisioning, by an atomic rename (a new inode and a new ctime), so a version synced once stays durable; a fresh
#: process starts empty and repays once. Re-syncing it on every request was 4-6 of a /renew's 15 fsyncs, and on the
#: Studio each one waits whenever other jobs saturate the Data volume. The descendant chain is still re-synced on
#: every acknowledgement (test_every_ack_syncs_descendants_even_with_reused_identity).
_RECEIPT_SYNCED: set = set()


@contextlib.contextmanager
def root_directory(root, relative=Path("."), *, mode=0o755):
    root = _absolute(root)
    receipt = _receipt_path(root)
    parts = Path(relative).parts
    if Path(relative).is_absolute() or ".." in parts or len(parts) > 64:
        raise OSError("bounded root-relative directory required")
    clock = STAGE_MARK.get()
    # t_bb190bec: open + receipt/chain fsyncs + validate re-walks are root setup (the clock names the lock phase),
    # never the caller's stage
    resume = clock("root_setup") if clock is not None else None
    fds, rfd = [], None
    try:
        try:
            rfd = _open_receipt_parent(receipt)
            doc = json.loads(_read_at(rfd, receipt.name, bounded=True))
        except FileNotFoundError as exc:
            raise RootRefused(f"root receipt missing: {receipt}") from exc
        except (ValueError, TypeError) as exc:
            raise RootRefused("invalid root receipt") from exc
        except OSError as exc:
            _refuse_structural(exc)
            raise
        if (not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("state") != "ready"
                or doc.get("root") != str(root) or not isinstance(doc.get("generation"), str)
                or not isinstance(doc.get("root_identity"), list)):
            raise RootRefused("fleet root provisioning incomplete")
        # Repay ready-rename sync debt even in a fresh runtime process; once per receipt version per process
        # (t_5e814402, _RECEIPT_SYNCED).
        handle = os.open(receipt.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=rfd)
        try:
            st, pst = os.fstat(handle), os.fstat(rfd)
            synced = (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_size, pst.st_dev, pst.st_ino)
            if synced not in _RECEIPT_SYNCED:
                os.fsync(handle)
                os.fsync(rfd)
                _RECEIPT_SYNCED.add(synced)
        finally:
            os.close(handle)
        fds.append(os.open(root, _DIR_FLAGS))  # NEVER create root
        directory = RootDirectory(root, receipt, rfd, doc, fds, ())
        directory.validate()
        for index, part in enumerate(parts):
            try:
                os.mkdir(part, mode=mode if index == len(parts) - 1 else 0o755, dir_fd=fds[-1])
            except FileExistsError:
                pass
            fds.append(os.open(part, _DIR_FLAGS, dir_fd=fds[-1]))
        directory = RootDirectory(root, receipt, rfd, doc, fds, parts)
        # Inodes may be reused after deletion: existence/identity is not a durable
        # proof. Re-sync the bounded descendant chain on every acknowledgement.
        for fd in reversed(fds):
            os.fsync(fd)
        directory.validate()
        context_reset = _ACTIVE_DIRECTORIES.set((*_ACTIVE_DIRECTORIES.get(), directory))
        if clock is not None:   # setup done: the body is the caller's time again
            clock(resume)
            clock = None
        try:
            yield directory
            directory.validate()
        finally:
            _ACTIVE_DIRECTORIES.reset(context_reset)
    finally:
        for fd in reversed(fds):
            os.close(fd)
        if rfd is not None:
            os.close(rfd)
        if clock is not None:   # refused before the yield: hand the caller its label back
            clock(resume)


def make_dirs_durable(directory, *, mode=0o777, through=None) -> None:
    """Compatibility helper: explicit provisioned root required, never infer an anchor."""
    base = _absolute(through if through is not None else _root())
    with root_directory(base, _absolute(directory).relative_to(base), mode=mode):
        pass


@contextlib.contextmanager
def _writer(root=None, timeout_s=None, *, directory=None):
    try:
        scope = root_directory(_root(root), DEMAND_REL) if directory is None else contextlib.nullcontext(directory)
        with scope as directory:
            timing = None
            try:
                with (directory / ".writer.lock").open("a") as fh:
                    t_wait = time.monotonic()
                    with _stage("writer_wait"):
                        if timeout_s is None:
                            fcntl.flock(fh, fcntl.LOCK_EX)
                        else:
                            deadline = time.monotonic() + max(0, timeout_s)
                            while True:
                                try:
                                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    break
                                except BlockingIOError:
                                    if time.monotonic() >= deadline:
                                        raise TimeoutError('demand writer busy') from None
                                    time.sleep(0.01)
                    t_held = time.monotonic()
                    try:
                        yield directory
                        directory.validate()
                    finally:
                        timing = (t_held - t_wait, time.monotonic() - t_held)   # the hold ends as the fd closes below
            finally:
                # t_1d801469: write the hold line only after .writer.lock is closed, never while the writer is held
                if timing is not None:
                    _note_hold("demand_writer", _root(root) / HOLDS_REL, *timing)
    except OwnershipRefused as refusal:
        # Nested ledger contexts have unwound and the writer fd is closed.
        # Delivery can block/fail, but must never hold either admission flock.
        refusal.page()
        raise DoorConflict("auth") from refusal


def _replace(path, doc):
    with _stage("store_serialize"):
        data = json.dumps(doc, sort_keys=True, allow_nan=False)
    if isinstance(path, RootFile):
        path.parent.replace(path.name, data)
        return
    base = _absolute(_root())
    with root_directory(base, _absolute(path).parent.relative_to(base)) as directory:
        directory.replace(path.name, data)
    return


def _owned(path, workload, root=None, *, uid=...):
    """All merged files belong to allocd's service UID; publisher identity is checked before merging.

    owners maps each kind to its registered filesystem service user (or numeric UID).
    Missing policy retains same-UID compatibility, never trusts a different filesystem owner.
    ``uid``: the caller's _owner_uid(workload, root), already resolved (t_2b90a362).
    """
    uid = _owner_uid(workload, root) if uid is ... else uid
    try:
        return uid is not None and path.stat().st_uid == uid and not path.is_symlink()
    except OSError:
        return False


def _owner_uid(workload, root=None):
    """The configured filesystem owner of ``workload``'s store (UID), None when the policy names no resolvable one."""
    owners = _read_policy(_root(root) / PRIORITY_REL).get("owners", {})
    owner = owners.get(workload, os.getuid()) if isinstance(owners, dict) else os.getuid()
    try:
        return owner if type(owner) is int else pwd.getpwnam(owner).pw_uid
    except (KeyError, TypeError):
        return None


class DemandStoreUnreadable(OSError):
    """An owned demand store exists but cannot be read/parsed. It carries the consumed tombstones (anti-replay), so
    every writer refuses to replace it (allocd answers 503) instead of treating it as empty (Prism e478e39ea54b)."""


def _existing(path, workload, root=None, *, uid=...):
    """``uid``: _owner_uid(workload, root) resolved by the caller BEFORE it took the demand writer (t_2b90a362: the
    policy read + passwd lookup ran twice per store_door inside the writer). Omitted = resolved here, as before."""
    if uid is ...:
        with _stage("owner_lookup"):
            uid = _owner_uid(workload, root)
    absent = not path.exists() and not path.is_symlink()
    # allocd is the single writer, and _replace always leaves a file owned by THIS process's UID. If the policy names
    # another owner, read_demand would hide whatever we write, so EVERY write is refused before any mutation, whether
    # the store is absent or already exists (never accept-then-hide; t_430834ff). An existing store must also carry
    # the configured owner.
    if uid != os.getuid() or (not absent and not _owned(path, workload, root, uid=uid)):
        # Carry only immutable delivery facts through lock unwinding, never an open fd.
        raise OwnershipRefused(str(path), workload, _root(root))
    if absent:
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemandStoreUnreadable(f"{path}: unreadable demand store ({exc}); refusing to replace it") from exc
    if not isinstance(doc, dict) or doc.get("workload") != workload:
        raise DemandStoreUnreadable(f"{path}: malformed demand store; refusing to replace it")
    return doc


class OwnershipRefused(Exception):
    """Internal unwind signal: _writer delivers exactly once after releasing its flock."""

    def __init__(self, path, workload, root):
        super().__init__(path)
        self.path, self.workload, self.root = path, workload, root

    def page(self):
        from episode_pager import EpisodePager
        try:
            outcome = EpisodePager("admission-demand", state_dir=self.root / "state/pager").page(
                f"unowned:{self.workload}", f"Admission store ownership refused: {self.path}; no writes permitted")
        except Exception as exc:
            raise DemandStoreUnreadable(f"ownership refusal page failed for {self.path}: {exc}") from exc
        if outcome == "failed":
            raise DemandStoreUnreadable(f"ownership refusal page failed for {self.path}; retry next request")


class DoorConflict(Exception):
    """A well-formed door operation the current queue state refuses (HTTP 409 with ``reason``)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_door(req, registry=None):
    """Shared remote wire schema. No client clock or remote urgency is authoritative."""
    registry = registry or admission.Priority()
    if not isinstance(req, dict) or req.get("schema") != SCHEMA:
        raise ValueError("schema:1 required")
    if not _name(req.get("workload")) or not _name(req.get("host")):
        raise ValueError("workload and canonical host required")
    op = req.get("op", "publish")
    if op not in ("publish", "enqueue", "withdraw", "mark_asap", "queue"):
        raise ValueError("unknown demand operation")
    if op == "queue":
        # The class's aggregate queue (legacy holdback input). The server stamps ``at``; hosts is the pair's host.
        q, unit = req.get("queued"), req.get("unit")
        if type(q) is not int or q < 0 or type(req.get("unified")) is not bool or not isinstance(unit, dict):
            raise ValueError("queue requires queued:int>=0, unified:bool, unit")
        if not all(_num(unit.get(k)) and unit[k] >= 0 for k in ("cpus", "ram_gb", "gpu_mib")) or "at" in req:
            raise ValueError("invalid queue unit or client timestamp")
        return req
    if op in ("enqueue", "withdraw", "mark_asap"):
        if not isinstance(req.get("id"), str) or not 0 < len(req["id"]) <= 256:
            raise ValueError("stable pending-unit id required")
        if op in ("withdraw", "mark_asap"):
            # store_door persists any supplied verdict for these ops too: never an unchecked one (Prism 6a8c1f6cc5cd)
            if req.get("verdict") is not None:
                _validate_verdict(req["verdict"])
            return req
    waiting = ([{"id": req["id"], "need_cpu": req.get("need_cpu"),
                 "need_mem_gb": req.get("need_mem_gb"), "asap": req.get("asap", False),
                 **({"at": req["at"]} if "at" in req else {})}] if op == "enqueue" else req.get("waiting"))
    verdict = req.get("verdict")
    if not isinstance(waiting, list) or len(waiting) > registry.door_window_units:
        raise ValueError("waiting exceeds door window")
    ids = set()
    for unit in waiting:
        if not isinstance(unit, dict) or not isinstance(unit.get("id"), str) or not 0 < len(unit["id"]) <= 256:
            raise ValueError("stable pending-unit id required")
        if unit["id"] in ids:
            raise ValueError("duplicate pending-unit id")
        ids.add(unit["id"])
        if unit.get("asap") is not False:
            raise ValueError("remote ASAP forbidden")
        if not (_num(unit.get("need_cpu")) and unit["need_cpu"] > 0
                and _num(unit.get("need_mem_gb")) and unit["need_mem_gb"] >= 0):
            raise ValueError("invalid waiting footprint")
        if "at" in unit:
            raise ValueError("client arrival timestamps forbidden")
    if op == "enqueue" and verdict is None:
        return req
    _validate_verdict(verdict)
    return req


def _validate_verdict(verdict):
    if not isinstance(verdict, dict) or type(verdict.get("admittable")) is not bool:
        raise ValueError("boolean admittable required")
    if not isinstance(verdict.get("binding"), str) or len(verdict["binding"]) > 128:
        raise ValueError("binding required")
    if not _num(verdict.get("deficit_cpu")) or verdict["deficit_cpu"] < 0 or "at" in verdict:
        raise ValueError("invalid verdict deficit or timestamp")


def consumed_units(doc):
    """Workload-wide spend index, including legacy per-host tombstones (never freshness-gated)."""
    consumed = dict(doc.get("consumed", {}))
    for state in doc.get("by_host", {}).values():
        for unit_id, value in state.get("consumed", {}).items():
            consumed.setdefault(unit_id, value)
    return consumed


#: allocd refuses any door lease (/demand/authorize, /demand/recover-existing) with ttl_s above this.
DOOR_TTL_MAX_S = 1800.0
#: How long a spent unit's tombstone outlives its lease (t_d53a92f3). Counted from the FIRST store_door that sees the
#: reservation no longer live in the ledger (``gone_at``), never from authorization: a renewed lease protects its
#: tombstone for as long as it lives, and an allocd outage stamps nothing. After the lease ends a holder can still
#: present the id for a lost-reply lookup/authorize retry (test-gate's wait budget is <= 600 s, backoff cap 300 s) or
#: a recover_existing on its next reap after a failed renew. Twice the longest lease covers both.
CONSUMED_HORIZON_S = 2 * DOOR_TTL_MAX_S
#: t_1d801469: a tombstone whose lease its holder RELEASED (``released: true``, set by placement.release under the
#: writer) is kept this long after ``gone_at`` instead of CONSUMED_HORIZON_S. The holder ended that job itself, so no
#: lost-reply authorize/lookup retry of it can still be in flight past test-gate's 600 s wait budget, and a
#: recover_existing of a released unit is refused either way (``released`` inside the horizon, ``not_consumed`` after).
#: Most CI units end by release, so this is what bounds ci.json (955 KB on 2026-11-13 at ~1,800 authorizes/h).
RELEASED_HORIZON_S = 900.0
LEDGER_REL = Path("var") / "placement" / "headroom-reservations.json"   # placement.RESERVATIONS_REL


def _ledger_until(root):
    """{reservation id: until} of every ledger row (live or lapsed); None when the ledger is missing or unreadable
    (then nothing is pruned)."""
    try:
        doc = json.loads((_root(root) / LEDGER_REL).read_text(encoding="utf-8"))
        rows = doc["rows"]
        if not isinstance(rows, list):
            return None  # Prism 711334fa5a20: a parseable but malformed ledger is unreadable, never "empty"
        return {r["id"]: (float(r["until"]) if _num(r.get("until")) and not r.get("released") else -math.inf)
                for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def bound_consumed(consumed, *, root=None, now):
    """Drop tombstones whose lease has been gone for CONSUMED_HORIZON_S; stamp ``gone_at`` on newly gone ones.

    A tombstone whose reservation is live is never dropped. ``gone_at`` is re-stamped whenever any liveness evidence
    postdates it (the ledger row's ``until``, a recover_existing ``observed_at``, the grant ``at``), so a lease that
    came back after the stamp restarts the clock. No readable ledger, or a record without a reservation id, prunes
    nothing (fail closed)."""
    until = _ledger_until(root)
    if until is None:
        return consumed
    out = {}
    for unit_id, record in consumed.items():
        if isinstance(record, dict) and isinstance(record.get("reservation_id"), str):
            rid = record["reservation_id"]
            last_live = max([until.get(rid, -math.inf)]
                            + [record[k] for k in ("at", "observed_at") if _num(record.get(k))])
            if last_live > now:
                record = {k: v for k, v in record.items() if k != "gone_at"}
            elif not _num(record.get("gone_at")) or record["gone_at"] > now or record["gone_at"] < last_live:
                record = {**record, "gone_at": now}
            elif now - record["gone_at"] > (RELEASED_HORIZON_S if record.get("released") is True
                                            else CONSUMED_HORIZON_S):
                continue
        out[unit_id] = record
    return out


def store_door(req, *, owner: str, root=None, now=None):
    """Atomic per-unit merge; the server owns every clock and every owner.

    ``owner`` is the AUTHENTICATED pair identity (``workload@host``) allocd checked before calling. ONE owner per
    unit id (t_810c9012 round 4): the pair that enqueued a live id owns it; enqueue / publish-refresh / withdraw /
    mark_asap of a live id owned by another pair raises DoorConflict("auth") and changes nothing. A unit gone stale
    (not re-asserted for demand_max_age_s) is gone: a later enqueue by any pair is a NEW record with a NEW
    server-stamped arrival. A caller-supplied ``at`` is refused by validate_door.
    """
    if not isinstance(owner, str) or owner.count("@") != 1 or not all(map(_name, owner.split("@"))):
        raise ValueError("authenticated owner identity required")
    registry = load_priority(root=root)
    validate_door(req, registry)
    workload, host = req["workload"], req["host"]
    # t_2b90a362: the owner policy read and its passwd lookup run BEFORE the writer is taken (the policy file is
    # config, never written under the writer); the writer then holds only read-merge-write of the store.
    with _stage("owner_lookup"):
        uid = _owner_uid(workload, root)
    with _writer(root) as directory:
        now = float(time.time() if now is None else now)
        path = directory / f"{workload}.json"
        doc = _existing(path, workload, root, uid=uid)
        if req.get("op") == "queue":
            # Class-level aggregate (legacy holdback input), last writer wins; ``hosts`` keeps every pair host that
            # published within demand_max_age_s, so two hosts' publishers never hide each other's restriction.
            # Each host keeps ITS OWN publication time (``host_at``): one live publisher never keeps a stopped
            # publisher's host alive (t_430834ff). A legacy doc without host_at dates its hosts by the doc ``at``.
            host_at = doc.get("host_at")
            if not isinstance(host_at, dict):
                host_at = ({str(h): doc["at"] for h in doc["hosts"]} if _num(doc.get("at"))
                           and isinstance(doc.get("hosts"), list) else {})
            host_at = {str(h): t for h, t in host_at.items()
                       if _num(t) and 0 <= now - t <= registry.demand_max_age_s}
            host_at[host] = now
            doc.update(schema=SCHEMA, workload=workload, at=now, queued=req["queued"],
                       hosts=sorted(host_at), host_at=host_at,
                       unit={k: float(req["unit"][k]) for k in ("cpus", "ram_gb", "gpu_mib")},
                       unified=req["unified"])
            _replace(path, doc)
            return {"at": now, "queued": req["queued"]}
        by_host = doc.get("by_host", {})
        if not isinstance(by_host, dict):
            by_host = {}
        previous = by_host.get(host, {})
        fresh = _num(previous.get("at")) and 0 <= now - previous["at"] <= registry.demand_max_age_s
        # Live units only: a stale unit is dropped here, so its id re-enqueues as a NEW record (new arrival, new owner).
        waiting = {u["id"]: u for u in previous.get("waiting", [])
                   if isinstance(u, dict) and isinstance(u.get("id"), str) and _num(u.get("at"))
                   and fresh and _num(u.get("seen_at", previous.get("at")))
                   and 0 <= now - u.get("seen_at", previous["at"]) <= registry.demand_max_age_s}
        op = req.get("op", "publish")
        # Bound FIRST (Prism 1ac0b2cc1555): an id whose tombstone just expired is a fresh unit for this very call,
        # so the expiry must precede the waiting filter, or the enqueue is dropped with no record of either kind.
        consumed = bound_consumed(consumed_units(doc), root=root, now=now)
        touched = [req["id"]] if op in ("enqueue", "withdraw", "mark_asap") else [u["id"] for u in req["waiting"]]
        if op in ("enqueue", "publish"):
            for other_host, state in by_host.items():
                if other_host == host or not _num(state.get("at")) or not 0 <= now - state["at"] <= registry.demand_max_age_s:
                    continue
                if any(u.get("id") in touched and u.get("id") not in consumed
                       and _num(u.get("seen_at", state["at"]))
                       and 0 <= now - u.get("seen_at", state["at"]) <= registry.demand_max_age_s
                       for u in state.get("waiting", [])):
                    raise DoorConflict("invalid")  # re-placement requires withdraw on the old host first
        if any(i in waiting and waiting[i].get("owner") != owner for i in touched):
            raise DoorConflict("auth")       # another pair's live unit: never honoured, never silently skipped
        if op == "withdraw":
            waiting.pop(req["id"], None)
        elif op == "mark_asap":
            # Operator-only verb (authenticated by allocd): urgency on an EXISTING unit, arrival untouched.
            unit = waiting.get(req["id"])
            if unit is None or req["id"] in consumed:
                raise DoorConflict("not_enqueued")
            if not unit.get("asap") and sum(1 for u in waiting.values()
                                            if u.get("asap") is True) >= registry.asap_max_units_per_host:
                raise DoorConflict("asap_cap")
            waiting[req["id"]] = {**unit, "asap": True}
        else:
            incoming = ([{"id": req["id"], "need_cpu": req["need_cpu"],
                          "need_mem_gb": req["need_mem_gb"]}] if op == "enqueue" else req["waiting"])
            for u in incoming:
                # A re-assertion keeps first-seen arrival and any operator urgency; it never grants urgency.
                known = waiting.get(u["id"], {})
                waiting[u["id"]] = {"id": u["id"], "owner": owner, "at": min(known.get("at", now), now),
                    "seen_at": now, "need_cpu": u["need_cpu"], "need_mem_gb": u["need_mem_gb"],
                    "asap": known.get("asap") is True}
        # Consumed IDs are anti-replay/fencing records. TTL applies to the ledger charge, not to authority to start
        # again or recover retired work; a tombstone is dropped only CONSUMED_HORIZON_S after its lease is gone.
        waiting = [u for uid, u in waiting.items() if uid not in consumed]
        verdict = ({**req["verdict"], "at": now} if req.get("verdict") is not None else previous.get("verdict"))
        if waiting and (not isinstance(verdict, dict) or not _num(verdict.get("at"))
                        or not 0 <= now - verdict["at"] <= registry.demand_max_age_s):
            # No fresh capacity fact: conservatively retain strict ordering, but do
            # not fabricate positive headroom or a numeric deficit.
            verdict = {"admittable": False, "binding": "cpu", "deficit_cpu": 0, "at": now}
        # ONE workload-wide index (t_d53a92f3): legacy per-host copies were merged by consumed_units above.
        for state in by_host.values():
            if isinstance(state, dict):
                state.pop("consumed", None)
        by_host[host] = {"at": now, "waiting": waiting, "verdict": verdict}
        doc.update(schema=SCHEMA, workload=workload, by_host=by_host, consumed=consumed)
        _replace(path, doc)
    state = by_host[host]
    if op == "enqueue":
        ordered = sorted(waiting, key=lambda u: (not u.get("asap"), u["at"], u["id"]))
        unit = next((u for u in ordered if u["id"] == req["id"]), None)
        state = {**state, "arrival": unit["at"] if unit else None,
                 "position": next((i + 1 for i, u in enumerate(ordered) if u["id"] == req["id"]), None)}
    return state


def waiting_window(units, registry=None):
    """Shared publisher window: urgency, observed arrival, deterministic stable ID."""
    registry = registry or admission.Priority()
    return sorted(units, key=lambda u: (u.get("asap") is not True, u.get("at", math.inf),
                                        str(u.get("id", ""))))[:registry.door_window_units]


def _spread_names(doc, now: float, max_age: float,
                  resolve: Optional[Callable[[str], Optional[str]]] = None):
    """The FRESH, alias-resolved, distinct host names a class queue was published for: None = any host (``hosts``
    null), ``...`` = malformed. One rule for read_demand's spread and GET /demand's ``hosts`` list (t_e36a076f)."""
    hosts = doc.get("hosts")
    if hosts is None:
        return None
    if not isinstance(hosts, list):
        return ...
    host_at = doc.get("host_at")
    if isinstance(host_at, dict):
        # per-host freshness: a host whose own publisher stopped expires on its own clock (t_430834ff)
        hosts = [h for h in hosts if _num(host_at.get(str(h))) and abs(now - host_at[str(h)]) <= max_age]
    # with a resolver (placement's HostTable.resolve), an alias names its canonical host (Prism 364936434c3d)
    return {((resolve(str(h)) if resolve else None) or str(h)).lower() for h in hosts}


def _demand_of(doc, now: float, max_age: float, host: str,
               resolve: Optional[Callable[[str], Optional[str]]] = None) -> Optional[admission.Demand]:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    wl, at, q, unit = doc.get("workload"), doc.get("at"), doc.get("queued"), doc.get("unit")
    if not (isinstance(wl, str) and wl and _num(at) and isinstance(q, int) and not isinstance(q, bool)
            and isinstance(unit, dict)):
        return None
    if now - float(at) > max_age or float(at) - now > max_age:
        return None
    names = _spread_names(doc, now, max_age, resolve)
    spread = 1
    if names is ...:
        return None
    if names is not None:
        want = (resolve(host) if resolve else None) or host
        if want.lower() not in names:
            return None
        spread = len(names)      # the queue's units start on one of these hosts, never on each (t_d8103e2f)
    cpus, ram, gpu = unit.get("cpus"), unit.get("ram_gb"), unit.get("gpu_mib", 0.0)
    if not (_num(cpus) and _num(ram) and _num(gpu) and cpus >= 0 and ram >= 0 and gpu >= 0):
        return None
    return admission.Demand(wl, q, admission.Estimate(float(cpus), float(ram), float(gpu), f"demand:{wl}"),
                            hosts=spread)


def read_demand(host: str, *, root=None, now: Optional[float] = None, max_age: Optional[float] = None,
                resolve: Optional[Callable[[str], Optional[str]]] = None,
                full_queue: bool = False, loaded: Optional[Mapping[str, dict]] = None,
                hosts_out: Optional[dict] = None) -> List[admission.Demand]:
    """Every FRESH class queue that may run on ``host``. ``resolve`` (name -> canonical host id or None) folds aliases
    on both sides of a ``hosts`` restriction; without it the match is case-insensitive string equality.
    ``full_queue`` retains all fresh units for lookup/authorization; publication uses the bounded default.
    ``loaded`` {workload: doc}: a store the caller already read with _existing (ownership checked) under the writer;
    used as is instead of parsing that file a second time under the locks (t_1d801469: ci.json is ~0.9 MB).
    ``hosts_out`` {workload: sorted host list | None}: filled for every returned row with the SAME fresh, resolved,
    distinct names its ``hosts`` spread counts (None = any host), so GET /demand publishes the list (t_e36a076f)."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    out: List[admission.Demand] = []
    d = _root(root) / DEMAND_REL
    try:
        names = sorted(p for p in d.glob("*.json"))
    except OSError:
        return out
    for p in names:
        if loaded is not None and p.stem in loaded:
            doc = loaded[p.stem]
            if not isinstance(doc, dict) or doc.get("workload") != p.stem:
                continue
        else:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _say(f"demand:{p}", f"{p} unreadable: holds nothing back")
                continue
            workload = doc.get("workload") if isinstance(doc, dict) else None
            if workload != p.stem or not _owned(p, workload, root):
                continue
        workload = p.stem
        dm = _demand_of(doc, now, max_age, host, resolve)
        spread_names = _spread_names(doc, now, max_age, resolve) if dm is not None else None
        by_host = doc.get("by_host", {})
        canonical = (resolve(host) if resolve else None) or host
        # EVERY fresh per-host state that resolves to ``host`` (a resource domain maps parent and guests to one id):
        # a stale or empty parent state never hides a guest's fresh queue (Prism 3fb8f5172cf9).
        states = [s for h, s in by_host.items()
                  if (((resolve(h) if resolve else None) or h).lower() == canonical.lower())
                  and isinstance(s, dict) and _num(s.get("at")) and abs(now - s["at"]) <= max_age] \
            if isinstance(by_host, dict) else []
        if states:
            state = max(states, key=lambda s: s["at"])     # newest state's verdict / at
            waiting, seen_ids = [], set()
            consumed = consumed_units(doc)
            for st, u in ((st, u) for st in states for u in (st.get("waiting") or [])):
                if not isinstance(u, dict):
                    continue
                if u.get("id") in consumed or u.get("id") in seen_ids:
                    continue
                seen_at = u.get("seen_at", st["at"])
                if not _num(seen_at) or not 0 <= now - seen_at <= max_age:
                    continue
                # freshness is the heartbeat (seen_at, checked above); arrival is only the ordering key, so a live
                # unit re-asserted for more than 7 days stays visible and authorizable (t_430834ff)
                at, cpu, mem = u.get("at"), u.get("need_cpu"), u.get("need_mem_gb")
                if not (_num(at) and _num(cpu) and cpu > 0 and _num(mem) and mem >= 0):
                    continue
                seen_ids.add(u.get("id"))
                waiting.append(admission.WaitingUnit(min(at, now), admission.Estimate(cpu, mem),
                                                      u.get("asap") is True, str(u.get("id", ""))))
            queued = len(waiting)
            waiting.sort(key=lambda u: (not u.asap, u.at, u.id))
            if not full_queue:
                waiting = waiting[:load_priority(root=root).door_window_units]
            v = state.get("verdict")
            verdict = None
            if (isinstance(v, dict) and isinstance(v.get("admittable"), (bool, int))
                    and isinstance(v.get("binding"), str) and _num(v.get("deficit_cpu"))
                    and v["deficit_cpu"] >= 0 and _num(v.get("at"))):
                verdict = admission.DoorVerdict(int(v["admittable"]), v["binding"], v["deficit_cpu"], min(v["at"], now))
            unit = dm.unit if dm else (waiting[0].need if waiting else admission.Estimate())
            dm = admission.Demand(workload, dm.queued if dm else queued, unit,
                                  tuple(waiting), verdict, state["at"], dm.hosts if dm else 1)
        if dm is not None:
            out.append(dm)
            if hosts_out is not None:
                hosts_out[workload] = sorted(spread_names) if isinstance(spread_names, set) else None
    return out


def door_document(d: admission.Demand) -> dict:
    return {"workload": d.workload, "queued": d.queued, "at": d.at,
            "unit": dataclasses.asdict(d.unit),
            "waiting": [{"id": u.id, "at": u.at, "need_cpu": u.need.cpus,
                         "need_mem_gb": u.need.ram_gb, "asap": u.asap} for u in d.waiting],
            "verdict": dataclasses.asdict(d.verdict) if d.verdict else None, "hosts": d.hosts}


def doors_from_document(doc: Mapping, *, now: float) -> List[admission.Demand]:
    """Decode the common GET transport without consumer-side priority logic."""
    if doc.get("schema") != SCHEMA or not isinstance(doc.get("host"), str):
        raise ValueError("invalid demand response")
    registry = priority_from_doc(doc.get("priority", {}))
    out = []
    for row in doc.get("door", []):
        # Reuse the disk parser's validation/freshness rules through a synthetic host doc.
        if not isinstance(row, dict) or not _name(row.get("workload")):
            continue
        at = row.get("at")
        if not _num(at) or abs(now - at) > registry.demand_max_age_s:
            continue
        waiting = []
        for u in row.get("waiting", [])[:registry.door_window_units]:
            if not isinstance(u, dict):
                continue
            t, cpu, mem = u.get("at"), u.get("need_cpu"), u.get("need_mem_gb")
            # the row's ``at`` (server publication) is the freshness check above; arrival only orders (t_430834ff)
            if not (_num(t) and _num(cpu) and cpu > 0 and _num(mem) and mem >= 0):
                continue
            waiting.append(admission.WaitingUnit(min(t, now), admission.Estimate(cpu, mem),
                                                  u.get("asap") is True, str(u.get("id", ""))))
        v = row.get("verdict")
        verdict = None
        if (isinstance(v, dict) and type(v.get("admittable")) in (int, bool)
                and isinstance(v.get("binding"), str) and _num(v.get("deficit_cpu"))
                and v["deficit_cpu"] >= 0 and _num(v.get("at"))):
            verdict = admission.DoorVerdict(int(v["admittable"]), v["binding"], v["deficit_cpu"], min(v["at"], now))
        unit = row.get("unit", {})
        need = admission.Estimate(unit.get("cpus", 0), unit.get("ram_gb", 0), unit.get("gpu_mib", 0))
        # the aggregate queue count is its own fact (legacy holdback), never the bounded window's length (6e3523a0dbe8)
        q = row.get("queued")
        queued = q if type(q) is int and q >= 0 else len(waiting)
        h = row.get("hosts")
        out.append(admission.Demand(row["workload"], queued, need, tuple(waiting), verdict, at,
                                    h if type(h) is int and h >= 1 else 1))
    return out


def unified(workload: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """<WORKLOAD>_UNIFIED_ADMIT (e.g. PRISM_UNIFIED_ADMIT, TEST_GATE_UNIFIED_ADMIT): "0" = the caller's old rule."""
    env = os.environ if env is None else env
    key = workload.upper().replace("-", "_") + "_UNIFIED_ADMIT"
    return str(env.get(key, "1")).strip() != "0"


def published_unified(workload: str, *, root=None, now: Optional[float] = None,
                      max_age: Optional[float] = None) -> Optional[bool]:
    """The kill switch as ``workload``'s admitter last published it (var/placement/demand/<workload>.json ``unified``),
    or None: no file, unreadable, malformed, no ``unified`` key (an older publisher) or older than demand_max_age_s.
    A reader in ANOTHER process than the admitter reads the mode here, never its own env (Prism 59574d3a240b)."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    try:
        doc = json.loads((_root(root) / DEMAND_REL / f"{workload}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("workload") != workload:
        return None
    at, mode = doc.get("at"), doc.get("unified")
    if not _num(at) or abs(now - float(at)) > max_age or not isinstance(mode, bool):
        return None
    return mode


def workload_of(consumer: str) -> str:
    """placement consumer -> admission workload class (the names in fleet/admission-priority.json)."""
    return {"athena": "kanban"}.get(consumer, consumer)
