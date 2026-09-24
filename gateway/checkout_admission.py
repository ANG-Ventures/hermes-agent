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
* Acknowledgment. Each consumer periodically publishes a record
  (``consumers/<name>.json``) from a snapshot taken under the same process
  lock admissions use: the hold epoch it observed plus its admitted tickets
  and its own authoritative active-work count. Because admission
  (read-hold + register) and snapshot (read-hold + count) are serialized, a
  snapshot that observed epoch E counts every turn that was admitted before E
  was written. That is the late-arrival guarantee; it does not depend on poll
  timing.
* Verdict. :func:`evaluate` returns ``QUIESCENT`` only when a FREEZE hold is
  present and EVERY expected consumer has a fresh record, a live pid on this host, an
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
import json
import logging
import os
import re
import socket
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


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
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
            raw = self.hold_path.read_text(encoding="utf-8")
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
             reason: str = "", now: Optional[float] = None) -> HoldState:
        """Engage (or re-engage with a new epoch) the hold. Never opens it."""
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
            except HoldUnreadable:
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
    def consumer_path(self, name: str) -> Path:
        return self.consumers_dir / _consumer_filename(name)

    def write_consumer(self, name: str, record: Mapping[str, Any]) -> None:
        _atomic_write_json(self.consumer_path(name), record)

    def read_consumer(self, name: str) -> Optional[dict]:
        try:
            raw = self.consumer_path(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("consumer record is not an object")
        return data

    def consumer_names(self) -> list:
        try:
            files = sorted(self.consumers_dir.glob("*.json"))
        except OSError:
            return []
        return [f.name[:-5].replace("@", ":") for f in files]


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
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.consumer = validate_consumer_name(consumer)
        self.instance = uuid.uuid4().hex
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.boot_ts = clock()
        self._clock = clock
        self._active_work = active_work
        self._serving = serving
        self._lock = threading.RLock()
        self._tickets: dict = {}

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
                "published_at": self._clock(), "hold_epoch": hold_epoch,
                "hold_error": hold_error, "tickets": tickets,
                "active_work": work_total, "active_work_detail": work_detail,
                "active_work_error": work_error, "serving": serving,
            }

    def publish(self) -> dict:
        record = self.snapshot()
        self.store.write_consumer(self.consumer, record)
        return record

    def start_publisher_thread(self, interval: float = DEFAULT_PUBLISH_INTERVAL,
                               stop: Optional[threading.Event] = None) -> threading.Thread:
        stop = stop or threading.Event()

        def _loop():
            while not stop.is_set():
                try:
                    self.publish()
                except Exception:
                    logger.warning("checkout admission publish failed", exc_info=True)
                stop.wait(interval)

        th = threading.Thread(target=_loop, name=f"checkout-admission:{self.consumer}", daemon=True)
        th.start()
        return th

    async def publish_forever(self, interval: float = DEFAULT_PUBLISH_INTERVAL) -> None:
        """Asyncio publisher: the snapshot runs ON the loop (so it cannot
        interleave with loop-thread check-and-register code) and its freshness
        proves the loop itself is serving. Only the file write is offloaded."""
        import asyncio

        while True:
            try:
                record = self.snapshot()
                await asyncio.to_thread(self.store.write_consumer, self.consumer, record)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("checkout admission publish failed", exc_info=True)
            await asyncio.sleep(interval)


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
            capture_output=True, text=True, timeout=10, check=True,
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
def _consumer_verdict(name: str, rec: Optional[dict], hold: HoldState, *, now: float,
                      stale_after: float, host: str,
                      pid_alive: Callable[[int], bool]) -> dict:
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
    if not pid_alive(rec.get("pid")):
        out["why"] = "publishing pid is not alive"
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
            rec = store.read_consumer(name)
        except Exception as exc:
            rows.append({"consumer": name, "state": UNKNOWN, "why": f"record unreadable: {exc}", "work": None})
            continue
        rows.append(_consumer_verdict(name, rec, hold, now=now, stale_after=stale_after,
                                      host=host, pid_alive=pid_alive))
    # A live consumer the operator forgot to list would be mutated under.
    for name in store.consumer_names():
        if name in hold.expected:
            continue
        try:
            rec = store.read_consumer(name)
        except Exception:
            rec = None
        if not rec:
            continue
        fresh = isinstance(rec.get("published_at"), (int, float)) and now - rec["published_at"] <= stale_after
        if fresh and rec.get("host") == host and pid_alive(rec.get("pid")):
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
    if rec.get("host") != socket.gethostname() or not pid_alive(rec.get("pid")):
        out["why"] = "publisher pid not alive on this host"
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
            capture_output=True, text=True, timeout=60, check=True,
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

    if _git("cat-file", "-e", f"{sha}^{{commit}}") != 0:
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
        state = store.hold(args.owner, args.expect, mode=args.mode, reason=args.reason)
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
