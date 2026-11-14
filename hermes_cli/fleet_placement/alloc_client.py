# SCHEDULE: manual   (a library consumers import; never scheduled; card t_552773f1)
"""alloc_client -- the thin library every work generator calls to get a host (Router Core Card A, t_552773f1).

    import alloc_client as ac
    r = ac.admit("kanban", job_id="t_1234", ttl_s=1800)          # AdmitResult
    if r.host: spawn on r.host, then ac.release(r.reservation_id, consumer="kanban") when the job ends
               (ac.renew(r.reservation_id, ttl_s=1800, consumer="kanban") while it runs)
    else:      r.reason says why; the caller queues (a refusal is final, it never spills)

ONE scorer, ONE ledger. allocd (scripts/allocd.py) is a process on the Studio that runs ``placement.admit`` -- the merged
scorer (headroom gate -> ci-box default_first -> overrides) with its reserve-at-dispatch ledger
(var/placement/headroom-reservations.json + admissions.jsonl) -- for every generator, so the kanban dispatcher, ci-placement,
the Prism pool and test-gate share one reservation ledger instead of one each. allocd adds no second accounting: its
answer IS ``placement.admit``'s answer on the same files, with the P0 reserve held back for non-P0 classes.

Fallback (the card's contract):
  * allocd UNREACHABLE (connection refused, 5xx, 503 router-off, a garbled reply, or a request timeout whose /health
    probe also fails) is a LIVENESS fault: the client logs ``allocd_unreachable`` to var/placement/events.jsonl and
    runs the SAME scorer in-process on the same ledger (fail-OPEN). "Unseen != lost": a request allocd never answered
    is not a refusal.
  * allocd SLOW is not allocd down (t_0c2a33a5): a request TIMEOUT is followed by one cheap GET /health
    (HEALTH_PROBE_S, inside the same budget). When it answers ok:true, allocd is alive and queued on STATE.lock under
    load: the client logs ``allocd_slow`` and answers deferred (admit -> reason ``allocd_slow``; release/renew -> False,
    renew(strict=True) raises Unreachable) and NEVER falls back. Falling back there took the ledger flock that allocd
    then blocked on, timing out the next callers too: a self-sustaining allocd_unreachable/allocd_wedged_lock storm.
    /health answers off STATE.lock, so ok:true proves the process, not progress (Prism 13942ef9c2c7): its monotonic
    ``admits``+``refusals`` are memoed across processes in var/placement/alloc-client-slow.json; the same pid with
    those counters frozen for > WEDGE_S is WEDGED -> ``allocd_wedged`` + the unreachable fallback. Any answered
    request clears the memo.
  * the fallback asks with ``reuse_job``: if allocd booked this job_id before its reply was lost, the fallback returns
    that row instead of booking a second one (zero double admits across the outage and on recovery).
  * a STALE or absent pressure vector stays CLOSED (fail-CLOSED), in allocd and in the fallback alike, because both
    run the same scorer over the same var/fleet-pressure.json (refreshed by the host push path, t_ddd33238).
  * a 409 from allocd is the scorer's refusal: final for this tick, never retried through the fallback.
  * a 400/401/403/404/422 (any 4xx but 408/409/410/429) is TERMINAL: ``Refused`` is raised and the fallback never
    runs, so a token refusal from allocd can never be bypassed by writing the ledger directly. A token file that
    exists but is empty or unreadable is the same (fail-CLOSED); only a MISSING file means "no auth".
  * the fallback is BOUNDED: it takes the ledger flock with LOCK_NB until the liveness budget (FALLBACK_BUDGET_S
    from the start of the call) runs out, then answers deferred ``allocd_wedged_lock`` -- an allocd wedged while
    holding the flock never hangs a consumer.

ONE ledger, also across the transport (split-ledger rule): the fallback writes the ledger in THIS fleet root, which is
allocd's ledger only on allocd's own host (``router.host`` in config/fleet-hosts.json, default mac-studio). A client
on any other host (a Linux consumer over the ssh mux) never falls back to its local ledger: with allocd unreachable
it gets a deferred ``allocd_unreachable_remote`` (logged loud), and its release/renew return False without writing.
The host is ``ALLOC_CLIENT_HOST`` or the hostname, resolved through the table's aliases.
A reservation this process booked in the fallback and later finds gone (/renew 410, or the fallback renew misses
it) is re-admitted with ``reuse_job`` for the RENEW's ttl_s, PINNED to the host the job runs on (``candidates``); if
that host cannot take it, renew() returns False and logs ``renew_host_lost`` (the caller decides). release/renew of
the old id then act on the replacement.

Transport (Ace Q3): allocd listens on 127.0.0.1 only; a Linux consumer reaches it through the control plane's existing
ssh mux (``ssh -O forward -L 18850:127.0.0.1:18850 <studio>``), never a LAN listener. ``ALLOCD_URL`` overrides the URL.
Auth: when var/alloc/tokens/<consumer> exists the client sends it as a bearer token (allocd then requires it). A
consumer that has ever had a token (``allocd.py --mint`` writes var/alloc/protected/<consumer> first; a client that
reads a token writes it too) is PROTECTED: from then on a MISSING token file is refused like an empty one, in allocd
and in the fallback alike, so the fallback never treats the owner's name as its credential (review r3). The
token is resolved FIRST, before the router-off shortcut or any fallback, so an empty/unreadable token file is refused
even with allocd off. release/renew take ``consumer`` as a REQUIRED argument and send the token of the reservation's
OWNER (known from this process's admit, or the local ledger row); a consumer that is not the owner is refused
client-side, and an owner unknown here is checked by allocd against the ledger row (403 -> Refused).

Stdlib only; python3.9+.
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Mapping, Optional

if not __package__:                      # hermes-home scripts/lib: siblings are top-level modules
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _sibling(name: str):
    """A sibling library module: ``<package>.<name>`` when this file is vendored inside a package (hermes-agent
    hermes_cli/fleet_placement/), else the top-level module on scripts/lib (hermes-home). Lazy, so a remote
    door-only client never imports the capacity scorer."""
    if name == "placement":
        if __package__:
            from . import placement as module
        else:
            import placement as module
    elif name == "admission_demand":
        if __package__:
            from . import admission_demand as module
        else:
            import admission_demand as module
    else:
        raise ValueError(f"unknown alloc_client sibling: {name}")
    return module


class _Placement:
    """Capacity/fallback dependencies are optional for remote door-only clients."""
    def __getattr__(self, name):
        return getattr(_sibling("placement"), name)


placement = _Placement()


def _fleet_root(root: Optional[Path] = None) -> Path:
    if __package__ == "fleet_admission":
        from .roots import resolve_root
        return resolve_root(root, source_file=__file__, package=__package__)
    if root is not None:
        return Path(root)
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    try:
        return placement.fleet_root()
    except ModuleNotFoundError:
        return Path(__file__).resolve().parents[2]

DEMAND_BINDINGS_REL = Path("var") / "alloc" / "demand-bindings"
DEFAULT_URL = "http://127.0.0.1:18850"
FALLBACK_BUDGET_S = 2.8              # THE budget: the whole call (request + /health probe + fallback flock) fits in it
HEALTH_PROBE_S = 0.5                 # the liveness read after a request timeout (slow vs wedged), inside the budget
TIMEOUT_S = 2.0                      # request deadline: budget less the probe leaves 0.3 s for the fallback flock
WEDGE_S = 3 * FALLBACK_BUDGET_S       # frozen /health counters this long = wedged, not slow (>= 3 timed-out calls)
SLOW_MEMO_REL = Path("var") / "placement" / "alloc-client-slow.json"   # cross-process: callers are short-lived
DEFAULT_ROUTER_HOST = "mac-studio"
PRESSURE_REL = Path("var") / "fleet-pressure.json"
EVENTS_REL = Path("var") / "placement" / "events.jsonl"
TOKENS_REL = Path("var") / "alloc" / "tokens"
ROUTER_OFF_REL = Path("var") / "alloc" / "router-off"
PROTECTED_REL = Path("var") / "alloc" / "protected"      # <consumer> marker: this consumer authenticates
P0 = "p0"
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{1,512}")


def _transport_error(exc: BaseException, req) -> str:
    """Only structured diagnostics: exception text can contain an unlabelled credential."""
    endpoint = urllib.parse.urlsplit(req.full_url)
    message = f"{type(exc).__name__}: {req.get_method()} host={endpoint.hostname} port={endpoint.port}"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, OSError) and type(reason.errno) is int:
        message += f" errno={reason.errno}"
    return message


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclasses.dataclass
class AdmitResult:
    host: Optional[str]
    reservation_id: Optional[str]
    reason: str                       # "" on a pick; the scorer's refusal otherwise
    source: str                       # allocd | fallback
    rule: str = ""
    band: Optional[str] = None
    detail: Optional[dict] = None
    consumer: str = ""                # the reservation's owner (release/renew send this consumer's token)
    job_id: Optional[str] = None       # pass back to admit(job_id=...) when retrying a deferred request


def url() -> str:
    return os.environ.get("ALLOCD_URL") or DEFAULT_URL


def pressure_path(path=None, *, root: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else _fleet_root(root=root) / PRESSURE_REL


def read_samples(path=None, *, now: float, root: Optional[Path] = None):
    """The scorer's samples from var/fleet-pressure.json. A missing / unreadable document = no samples = every host
    closed (fail-CLOSED), never an exception that a caller could mistake for a liveness fault."""
    try:
        doc = json.loads(pressure_path(path, root=root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    try:
        return placement.samples_from_pressure(doc, now=now)
    except placement.PlacementError:
        return {}


def log_event(kind: str, *, root: Optional[Path] = None, **fields) -> None:
    """Best-effort events.jsonl line (kanban-placement-watch counts kinds there). Never raises."""
    p = _fleet_root(root=root) / EVENTS_REL
    try:
        with _sibling("admission_demand").root_directory(_fleet_root(root=root), EVENTS_REL.parent) as directory:
            with (directory / p.name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, "at": time.time(), **fields}, sort_keys=True) + "\n")
    except OSError:
        pass
    sys.stderr.write(f"alloc_client: {kind} {json.dumps(fields, sort_keys=True)}\n")


class Refused(Exception):
    """A TERMINAL answer (bad request, bad/missing token, wrong owner): never a reason to fall back."""


class TokenUnreadable(Refused):
    """A token file exists but is empty or unreadable: fail closed, never "no auth"."""


class TokenUnavailable(TokenUnreadable):
    """The token could not be read or its protected marker recorded RIGHT NOW (an OSError: EMFILE, ENFILE, EIO, ...).
    Still fail closed (a TokenUnreadable, so no caller authenticates on it), but it says nothing about the credential:
    allocd answers it 503 (liveness), never 403 (auth), so a door never latches CONFIG FAULT on a server fd squeeze
    (t_e9b779f1, 2026-11-10 12:08)."""


#: authorize_start refusal enum (ruling p2-unknown-receipt-spent-recovery, 2026-11-05 10:15). Consumer action:
#:   spent_expired  RAISED: authorized earlier, lease gone. Run the kind's own existence check, release if absent,
#:                  then enqueue a NEW unit id. Never retry this id.
#:   auth, invalid  RAISED: config/identity/schema fault. Stop and page; never retried blind.
#:   queued_behind  returned (authorized False): an earlier unit holds the window. Wait, retry the SAME id.
#:   capacity       returned (authorized False): admission says no room. Wait, retry the SAME id.
#:   not_enqueued   returned (authorized False): id not in the door queue (withdrawn / heartbeat-expired /
#:                  never enqueued). enqueue() the SAME id again, then retry.
#: spent_live is never a refusal: it is the reused:true receipt of the live lease.
AUTHORIZE_REASONS = ("spent_expired", "auth", "invalid", "queued_behind", "capacity", "not_enqueued")
_RAISED_REASONS = ("spent_expired", "auth", "invalid")
#: token name of the operator capability that may mark a unit ASAP (mark_asap); the pair token alone never can
ASAP_OPERATOR = "asap-operator"


class RootRefused(Refused):
    """allocd answered 503 ``reason: root_refused`` (t_1cfb3845): its provisioned fleet root or receipt no longer
    proves itself. A CONFIG fault (taxonomy class 2: refuse, latch, page once), never a transport fault: no retry
    loop or local fallback can fix it, only an operator re-proof of the root on allocd's host. A Refused, so every
    caller that already treats Refused as terminal (admit/release/renew, tg_door._classify) gets it right."""


ROOT_REFUSED_REASON = "root_refused"


def _root_refused(exc: "urllib.error.HTTPError") -> Optional[RootRefused]:
    """The typed refusal when a 503 carries allocd's root_refused body, else None (a plain 503 stays transport)."""
    if exc.code != 503:
        return None
    try:
        body = json.loads(exc.read(65537) or b"{}")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if isinstance(body, dict) and body.get("reason") == ROOT_REFUSED_REASON:
        return RootRefused(f"allocd fleet root refused: {body.get('detail') or 'unspecified'}")
    return None


class AuthorizeRefused(Refused):
    """A terminal, TYPED authorize_start refusal; ``reason`` is one of spent_expired | auth | invalid."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _protected(consumer: str, *, root: Optional[Path] = None) -> bool:
    """True once ``consumer`` has had a token (minted, or read by any client/allocd): var/alloc/protected/<consumer>.
    A stat error other than "missing" counts as protected (fail closed)."""
    try:
        os.stat(_fleet_root(root=root) / PROTECTED_REL / consumer)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def mark_protected(consumer: str, *, root: Optional[Path] = None) -> None:
    """Record that ``consumer`` uses a token, so a later MISSING token file is refused, never "no auth"."""
    dm = _sibling("admission_demand")
    with dm.root_directory(_fleet_root(root=root), PROTECTED_REL) as directory:
        marker = directory / consumer
        with marker.open("a") as fh:
            os.fsync(fh.fileno())
        os.fsync(directory.fd)
        directory.validate()


def _token(consumer: str, *, root: Optional[Path] = None) -> Optional[str]:
    """The consumer's bearer token. None ONLY when the file is missing AND the consumer was never protected (no
    token ever minted or read for it); empty / unreadable / missing-for-a-protected-consumer raises TokenUnreadable.
    This is the one auth rule for allocd and the fallback alike: in the fallback the name is never the credential
    (review r3, Prism 092aba2d5cc6). An empty or non-plain consumer name is refused outright."""
    if not isinstance(consumer, str) or not consumer or consumer in (".", "..") or "/" in consumer or "\x00" in consumer:
        raise Refused(f"consumer name required (got {consumer!r})")
    p = _fleet_root(root=root) / TOKENS_REL / consumer
    try:
        tok = p.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        if _protected(consumer, root=root):
            raise TokenUnreadable(f"{p}: missing for a protected consumer") from None
        return None
    except UnicodeError:
        tok = None
    except OSError as exc:                 # EMFILE / ENFILE / EIO ...: this read failed; it says nothing of the token
        raise TokenUnavailable(f"{p}: unreadable now: {exc}") from exc
    except ValueError as exc:
        raise TokenUnreadable(f"{p}: unreadable: {exc}") from exc
    if tok is None:
        # UnicodeDecodeError retains the file bytes; do not retain it as __context__.
        raise TokenUnreadable(f"{p}: malformed")
    if not tok:
        raise TokenUnreadable(f"{p}: empty")
    if not _TOKEN_SHAPE.fullmatch(tok):
        raise TokenUnreadable(f"{p}: malformed")
    try:
        mark_protected(consumer, root=root)  # root generation checked even for warm clients
    except OSError as exc:
        raise TokenUnavailable(f"{p}: cannot record protected marker: {exc}") from exc
    return tok


class Unreachable(Exception):
    """allocd did not give an answer this client can act on (a liveness fault, not a refusal)."""


class TimedOut(Unreachable):
    """The request timed out: allocd may be SLOW (alive, queued) rather than down; /health tells them apart."""


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (socket.timeout, TimeoutError)) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)))


def _health(timeout: float):
    """GET /health: allocd's doc when it answers ok:true within ``timeout``, else None (down or wedged)."""
    try:
        with _OPENER.open(url().rstrip("/") + "/health", timeout=timeout) as resp:     # noqa: S310
            doc = json.loads(resp.read() or b"{}")
    except Exception:                      # noqa: BLE001 -- any failure here means "not provably alive"
        return None
    return doc if isinstance(doc, dict) and doc.get("ok") is True else None


def _clear_slow_memo(*, root: Optional[Path] = None) -> None:
    try:
        (_fleet_root(root=root) / SLOW_MEMO_REL).unlink()
    except OSError:
        pass


def _frozen_for(pid, progress: list, *, root: Optional[Path] = None) -> float:
    """Seconds this allocd (pid) has shown the SAME progress counters across timed-out calls (0.0 = first sighting
    or moved); records the sighting in the cross-process memo (atomic tmp+rename, best effort)."""
    p, now = _fleet_root(root=root) / SLOW_MEMO_REL, time.time()
    try:
        memo = json.loads(p.read_text())
    except (OSError, ValueError):
        memo = None
    admits, refusals = progress
    if not (isinstance(memo, dict) and (memo.get("pid"), memo.get("admits"), memo.get("refusals")) == (
            pid, admits, refusals) and isinstance(memo.get("first_seen_at"), (int, float))):
        memo = {"pid": pid, "admits": admits, "refusals": refusals, "first_seen_at": now}
    memo["last_seen_at"] = now
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(memo, sort_keys=True))
        os.replace(tmp, p)
    except OSError:
        pass
    return max(0.0, now - memo["first_seen_at"])


def _allocd_slow(exc: BaseException, t0: float, op: str, consumer: str, *, root: Optional[Path] = None, **ids) -> bool:
    """A request timeout on an allocd that still answers /health AND still makes progress: log ``allocd_slow``,
    True = defer (no fallback). Counters frozen > WEDGE_S on the same pid: log ``allocd_wedged``, False = fall back."""
    if not isinstance(exc, TimedOut):
        return False
    doc = _health(HEALTH_PROBE_S)          # never clamped to the budget left: a 0.05 s probe would read slow as down
    if doc is None:
        return False
    pid, progress = doc.get("pid", 0), [doc.get("admits"), doc.get("refusals")]
    frozen = _frozen_for(pid, progress, root=root)
    if frozen > WEDGE_S:
        log_event("allocd_wedged", consumer=consumer, op=op, pid=pid, admits=progress[0], refusals=progress[1],
                  frozen_s=round(frozen, 1), error=str(exc), url=url(), root=root, **ids)
        return False
    log_event("allocd_slow", consumer=consumer, op=op, waited_s=round(time.monotonic() - t0, 3), pid=pid,
              error=str(exc), url=url(), root=root, **ids)
    return True


_PUBLISHED_UNITS = {}


def _unit_request(workload, host, unit_id, op, *, need_cpu=None, need_mem_gb=None,
                  timeout=TIMEOUT_S, root: Optional[Path] = None):
    admission_demand = _sibling("admission_demand")
    body = {"schema": 1, "workload": workload, "host": host, "id": unit_id, "op": op}
    if op == "enqueue":
        body.update(need_cpu=need_cpu, need_mem_gb=need_mem_gb)
    admission_demand.validate_door(body, admission_demand.load_priority(root=root))
    identity = f"{workload}@{host}"
    token = _token(identity, root=root)
    if not token or not _protected(identity, root=root):
        raise Refused("provisioned demand publisher token required")
    code, result = _door_call(body, timeout=timeout, token=token, root=root)
    if (code != 200 or result.get("schema") != 1 or result.get("host") != host
            or result.get("workload") != workload or result.get("ok") is not True):
        raise Unreachable("invalid unit receipt")
    return result


def _door_call(body, *, timeout, token, headers=None, base_url=None, root: Optional[Path] = None):
    """POST /demand/publish. A 403 is TYPED: AuthorizeRefused("auth") (bad pair token, missing operator capability,
    or a live unit another pair owns: t_810c9012 round 4), never a bare Refused a caller must string-match."""
    try:
        code, result = _call("/demand/publish", body, timeout=timeout, token=token, headers=headers, base_url=base_url, root=root)
        if code != 200:
            raise AuthorizeRefused(result.get("reason", "invalid"), result.get("error", "door refused"))
        return code, result
    except Refused as exc:
        if isinstance(exc, AuthorizeRefused):
            raise
        if "HTTP 403" in str(exc):
            raise AuthorizeRefused("auth", str(exc)) from None
        if "HTTP 400" in str(exc):
            raise AuthorizeRefused("invalid", str(exc)) from None
        raise


def publish_queue(workload, host, queued, unit, *, unified, timeout=TIMEOUT_S, root: Optional[Path] = None):
    """Publish the class's aggregate queue (``queued`` units of ``unit`` = {cpus, ram_gb, gpu_mib}) for ``host``
    through the pair door; ``unified`` = the publisher's <WORKLOAD>_UNIFIED_ADMIT switch. The server stamps ``at``.
    Replaces the deleted local admission_demand.publish (t_810c9012 round 4: one code path, one owner model)."""
    admission_demand = _sibling("admission_demand")
    body = {"schema": 1, "workload": workload, "host": host, "op": "queue", "queued": int(queued),
            "unified": bool(unified), "unit": {k: float(unit[k]) for k in ("cpus", "ram_gb", "gpu_mib")}}
    admission_demand.validate_door(body, admission_demand.load_priority(root=root))
    identity = f"{workload}@{host}"
    token = _token(identity, root=root)
    if not token or not _protected(identity, root=root):
        raise Refused("provisioned demand publisher token required")
    code, result = _door_call(body, timeout=timeout, token=token, root=root)
    if (code != 200 or result.get("schema") != 1 or result.get("host") != host
            or result.get("workload") != workload or result.get("ok") is not True):
        raise Unreachable("invalid queue receipt")
    return result


def enqueue(workload, host, unit_id, need_cpu, need_mem_gb, *, timeout=TIMEOUT_S, root: Optional[Path] = None):
    """Assert one globally unique per-attempt ID; repeat preserves server arrival."""
    return _unit_request(workload, host, unit_id, "enqueue", need_cpu=need_cpu,
                         need_mem_gb=need_mem_gb, timeout=timeout, root=root)


def withdraw(workload, host, unit_id, *, timeout=TIMEOUT_S, root: Optional[Path] = None):
    """Idempotently withdraw only this caller's stable unit ID."""
    return _unit_request(workload, host, unit_id, "withdraw", timeout=timeout, root=root)


def mark_asap(workload, host, unit_id, *, timeout=TIMEOUT_S, root: Optional[Path] = None):
    """Mark ONE already-enqueued unit urgent; its arrival is kept. Operator only: sends the pair token AND the
    ``asap-operator`` capability; without it allocd answers 403 -> Refused. 409 (``not_enqueued`` / ``asap_cap``)
    -> Refused. Urgency is never inherited and never carried by enqueue (ruling p4-trusted-asap-unit-merge)."""
    admission_demand = _sibling("admission_demand")
    body = {"schema": 1, "workload": workload, "host": host, "id": unit_id, "op": "mark_asap"}
    admission_demand.validate_door(body, admission_demand.load_priority(root=root))
    identity = f"{workload}@{host}"
    token = _token(identity, root=root)
    if not token or not _protected(identity, root=root):
        raise Refused("provisioned demand publisher token required")
    op_token = _token(ASAP_OPERATOR, root=root)
    if not op_token:
        raise Refused("asap operator capability required")
    code, result = _door_call(body, timeout=timeout, token=token,
                              headers={"X-Operator-Authorization": f"Bearer {op_token}"}, root=root)
    if code == 409:
        raise Refused(f"mark_asap refused: {result.get('reason', 'conflict')}")
    if code != 200 or result.get("ok") is not True or result.get("host") != host:
        raise Unreachable("invalid mark_asap receipt")
    return result


def lookup_unit(workload, host, unit_id, *, timeout=TIMEOUT_S, root: Optional[Path] = None) -> dict:
    """Where is this unit now? Call it after a lost authorize_start reply, BEFORE any retry.
    Returns {state, ...}: ``waiting`` (+position, arrival, asap) | ``authorized`` (+reservation_id, until: the live
    lease, adopt it, never start twice) | ``spent_expired`` (reconcile as in AUTHORIZE_REASONS) | ``unknown``.
    Read-only on the server. Refused on auth/schema, Unreachable on transport."""
    identity = f"{workload}@{host}"
    token = _token(identity, root=root)
    if not token or not _protected(identity, root=root):
        raise Refused("provisioned demand publisher token required")
    code, result = _call("/demand/lookup", {"schema": 1, "workload": workload, "host": host, "id": unit_id},
                         timeout=timeout, token=token, root=root)
    if (code != 200 or result.get("schema") != 1 or result.get("id") != unit_id
            or result.get("state") not in ("waiting", "authorized", "spent_expired", "unknown")):
        raise Unreachable("invalid unit lookup receipt")
    return result


def publish_demand(workload: str, host: str, waiting: list, verdict: Mapping, *,
                   base_url: Optional[str] = None, timeout_s: float = TIMEOUT_S, root: Optional[Path] = None) -> dict:
    """Merge this process's pending IDs and LWW verdict; never replace a sibling queue.

    waiting contains stable id, need_cpu, need_mem_gb, asap:false; verdict contains
    admittable:bool, binding:str, deficit_cpu:float. Server stamps every clock.
    Refused covers authentication/validation; Unreachable covers transport/bad replies.
    """
    admission_demand = _sibling("admission_demand")
    req = {"schema": 1, "workload": workload, "host": host, "waiting": waiting, "verdict": dict(verdict)}
    admission_demand.validate_door(req, admission_demand.load_priority(root=root))
    identity = f"{workload}@{host}"
    token = _token(identity, root=root)
    if not token or not _protected(identity, root=root):
        raise Refused("provisioned demand publisher token required")
    data = json.dumps(req, allow_nan=False).encode()
    if len(data) > 65536:
        raise Refused("demand publication exceeds 64 KiB")
    request = urllib.request.Request((base_url or url()).rstrip("/") + "/demand/publish", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            result = json.loads(response.read(65537))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            error = AuthorizeRefused("auth", "demand publication refused HTTP 403")
        elif 400 <= exc.code < 500:
            error = Refused(f"demand publication refused HTTP {exc.code}")
        else:
            error = _root_refused(exc) or Unreachable(f"demand publication HTTP {exc.code}")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        error = Unreachable(_transport_error(exc, request))
    else:
        error = None
    if error is not None:
        raise error
    if not (isinstance(result, dict) and result.get("schema") == 1 and result.get("host") == host
            and result.get("workload") == workload and isinstance(result.get("door"), dict)
            and admission_demand._num(result["door"].get("at"))):
        raise Unreachable("invalid demand receipt")
    key = (workload, host, base_url or url(), str(_fleet_root(root).resolve()))
    current = {u["id"] for u in waiting}
    acknowledged = _PUBLISHED_UNITS.setdefault(key, set())
    acknowledged.update(current)  # publication succeeded, independently of later withdrawals
    for unit_id in acknowledged - current:
        # Only this process's acknowledged IDs, never a whole-pair replacement.
        # The same endpoint is used even when the caller provided an explicit URL.
        body = {"schema": 1, "workload": workload, "host": host, "id": unit_id, "op": "withdraw"}
        code, reply = _door_call(body, timeout=timeout_s, token=token, base_url=base_url, root=root)
        if code != 200 or reply.get("ok") is not True:
            raise Unreachable("withdraw unavailable")
        acknowledged.remove(unit_id)  # retain failed/unattempted withdrawals for the next publication
    return result


def authorize_start(workload: str, host: str, unit_id: str, *, ttl_s: float = 90,
                    timeout: float = TIMEOUT_S, existing_reservation_id: Optional[str] = None,
                    existing_consumer: Optional[str] = None, local_venue_override: bool = False, root: Optional[Path] = None) -> dict:
    """Consume ONE unit at the actual start boundary. No fallback; retry is idempotent.

    Return authorized, reservation_id, until, reused. A wait answer is {authorized: False, reason, detail} with
    reason queued_behind | capacity | not_enqueued; spent_expired / auth / invalid RAISE AuthorizeRefused (a Refused)
    with ``.reason`` set. See AUTHORIZE_REASONS for each consumer action. After a lost reply, lookup_unit() first.
    A reused true receipt never permits a second spawn: caller reconciles the original attempt.
    renew/release use consumer=f'{workload}@{host}', with existing lease lifecycle.
    """
    identity = f"{workload}@{host}"
    try:
        tok = _token(identity, root=root)
    except Refused as exc:
        raise AuthorizeRefused("auth", str(exc)) from None
    if not tok or not _protected(identity, root=root):
        raise AuthorizeRefused("auth", "provisioned demand publisher token required")
    body = {"schema": 1, "workload": workload, "host": host, "id": unit_id, "ttl_s": ttl_s,
            "local_venue_override": local_venue_override}
    headers = {}
    # Every refusal below is typed (round 2, finding 6): a bad argument is ``invalid``, a missing/empty/unreadable
    # capability credential is ``auth``; never a bare Refused a caller would have to string-match.
    if local_venue_override:
        try:
            local_token = _token("test-gate-local-venue", root=root)
        except Refused as exc:
            raise AuthorizeRefused("auth", str(exc)) from None
        if not local_token:
            raise AuthorizeRefused("auth", "trusted local venue capability required")
        headers["X-Local-Venue-Authorization"] = f"Bearer {local_token}"
    if existing_reservation_id is not None or existing_consumer is not None:
        if (not isinstance(existing_reservation_id, str) or not existing_reservation_id
                or not isinstance(existing_consumer, str) or not existing_consumer):
            raise AuthorizeRefused("invalid", "existing reservation and owner required together")
        try:
            owner_token = _token(existing_consumer, root=root)
        except TokenUnreadable as exc:
            raise AuthorizeRefused("auth", str(exc)) from None
        except Refused as exc:             # a malformed owner name
            raise AuthorizeRefused("invalid", str(exc)) from None
        if not owner_token:
            raise AuthorizeRefused("auth", "protected existing reservation owner required")
        body.update(existing_reservation_id=existing_reservation_id, existing_consumer=existing_consumer)
        headers["X-Reservation-Authorization"] = f"Bearer {owner_token}"
    try:
        code, result = _call("/demand/authorize", body, timeout=timeout, token=tok, headers=headers, root=root)
    except Refused as exc:                 # 400 / 403: schema or identity, never a wait
        raise AuthorizeRefused("invalid" if "HTTP 400" in str(exc) else "auth", str(exc)) from None
    if code in (409, 410):
        reason = result.get("reason")
        if code == 410:                    # a pre-enum allocd: a spent id
            reason = "spent_expired"
        if reason not in AUTHORIZE_REASONS:
            raise Unreachable(f"untyped authorize refusal {reason!r}")
        if reason in _RAISED_REASONS:
            raise AuthorizeRefused(reason, str(result.get("detail") or ""))
        return {"authorized": False, "reason": reason, "detail": str(result.get("detail") or "")}
    if (code != 200 or result.get("schema") != 1 or result.get("host") != host
            or result.get("workload") != workload or result.get("id") != unit_id
            or not isinstance(result.get("reservation_id"), str) or not result["reservation_id"]
            or type(result.get("reused")) is not bool):
        raise Unreachable("invalid authorization receipt")
    _OWNER[_state_key(result["reservation_id"], root)] = identity
    # The door now owns this lease's recovery lifecycle, including an idempotently reused upgrade.
    _FALLBACK_ISSUED.pop(_state_key(result["reservation_id"], root), None)
    return {**result, "authorized": True}


class RecoveryRefused(Refused):
    """Typed terminal accounting refusal; never retry as a new start."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclasses.dataclass(frozen=True)
class AccountingReceipt:
    workload: str
    host: str
    unit_id: str
    reservation_id: str
    container_id: str
    container_name: str
    until: float
    reused: bool


def recover_existing(workload, host, unit_id, reservation_id, *, container_id,
                     container_name, observed_age_s, ttl_s=300, timeout=TIMEOUT_S, root: Optional[Path] = None) -> AccountingReceipt:
    """Restore accounting only, NOT authorization to spawn.

    Caller is the trusted host supervisor: persist unit/RID/container identity first,
    freshly inspect existence, then send age since that observation (<=30s). The
    server authenticates this assertion; it does not independently inspect Docker.
    Lost reply: inspect again and retry SAME identities. No fallback or /admit.
    """
    dm = _sibling('admission_demand')
    if (workload != 'ci' or not dm._name(host) or not isinstance(unit_id, str)
            or not 0 < len(unit_id) <= 256 or not isinstance(reservation_id, str) or not reservation_id
            or not dm._num(observed_age_s) or not 0 <= observed_age_s <= 30
            or not dm._num(ttl_s) or not 1 <= ttl_s <= 1800
            or not dm._num(timeout) or not 0 < timeout <= 30):
        raise RecoveryRefused('invalid')
    identity = f'{workload}@{host}'
    try:
        token = _token(identity, root=root)
        if not token or not _protected(identity, root=root):
            raise Refused()
    except Refused:
        raise RecoveryRefused('auth') from None
    body = {'schema': 1, 'workload': workload, 'host': host, 'id': unit_id,
            'reservation_id': reservation_id, 'container_id': container_id,
            'container_name': container_name, 'observed_age_s': observed_age_s, 'ttl_s': ttl_s}
    try:
        code, out = _call('/demand/recover-existing', body, token=token, timeout=timeout, root=root)
    except Refused as exc:
        if 'HTTP 404' in str(exc):
            raise RecoveryRefused('unsupported') from None
        raise RecoveryRefused('invalid' if 'HTTP 400' in str(exc) else 'auth') from None
    if code in (409, 410):
        reason = out.get('reason')
        if reason not in ('not_consumed', 'manual_recovery', 'identity', 'released', 'stale_observation'):
            raise Unreachable('untyped accounting refusal')
        raise RecoveryRefused(reason)
    if (code != 200 or not isinstance(out, dict) or out.get('schema') != 1
            or any(out.get(k) != body[k] for k in ('workload', 'host', 'id', 'reservation_id',
                                                  'container_id', 'container_name'))
            or out.get('accounted') is not True or type(out.get('reused')) is not bool
            or not dm._num(out.get('until')) or out['until'] <= time.time()):
        raise Unreachable('invalid accounting receipt')
    _OWNER[_state_key(reservation_id, root)] = identity
    _FALLBACK_ISSUED.pop(_state_key(reservation_id, root), None)
    return AccountingReceipt(workload, host, unit_id, reservation_id, container_id,
                             container_name, float(out['until']), out['reused'])


def _router_host(*, root: Optional[Path] = None) -> str:
    try:
        doc = json.loads((_fleet_root(root=root) / placement.TABLE_REL).read_text(encoding="utf-8-sig"))
        r = doc.get("router") if isinstance(doc, dict) else None
        h = r.get("host") if isinstance(r, dict) else None
        return h if isinstance(h, str) and h else DEFAULT_ROUTER_HOST
    except (OSError, ValueError):
        return DEFAULT_ROUTER_HOST


def ledger_is_local(table=None, *, root: Optional[Path] = None) -> bool:
    """True when this process runs on allocd's host, i.e. the fallback's ledger IS allocd's ledger."""
    me = os.environ.get("ALLOC_CLIENT_HOST") or socket.gethostname()
    want = _router_host(root=root)
    try:
        table = table or placement.load_hosts(root=root)
    except placement.PlacementError:
        return False
    names = {me, me.split(".")[0]}
    mine = {table.resolve(n) or n for n in names}
    return (table.resolve(want) or want) in mine


# reservations booked by THIS process in the fallback: rid -> admit kwargs (re-admitted with reuse_job when gone)
_FALLBACK_ISSUED: dict = {}
_REPLACED: dict = {}                  # old rid -> the rid that replaced it
_OWNER: dict = {}                     # rid -> the consumer that booked it (from this process's AdmitResults)


def _state_key(rid, root):
    """Legacy source callers retain their keys; explicit roots never share recovery state."""
    return (str(_fleet_root(root).resolve()), rid) if root is not None or __package__ == "fleet_admission" else rid


def _current(rid: str, *, root: Optional[Path] = None) -> str:
    seen = set()
    while _state_key(rid, root) in _REPLACED and rid not in seen:
        seen.add(rid)
        rid = _REPLACED[_state_key(rid, root)]
    return rid


def _budget_left(t0: float) -> float:
    return max(0.05, FALLBACK_BUDGET_S - (time.monotonic() - t0))


def _owner(rid: str, consumer: str, *, root: Optional[Path] = None) -> str:
    """The reservation's owner: this process's AdmitResult, else the local ledger row (router host), else the caller's
    ``consumer`` (allocd then checks the row's owner token). A known owner that is not ``consumer`` is Refused."""
    _token(consumer, root=root)                       # validates the name (empty -> Refused) and the caller's own token file
    owner = _OWNER.get(_state_key(rid, root))
    if owner is None and "@" not in consumer and ledger_is_local(root=root):   # only the router host's ledger is allocd's (split-ledger rule)
        try:
            for row in placement._read_ledger(placement.reservations_path(root=root))["rows"]:
                if row.get("id") == rid:
                    owner = row.get("consumer")
                    break
        except Exception:                  # noqa: BLE001 -- unreadable/absent local ledger: allocd decides
            owner = None
    if owner and owner != consumer:
        raise Refused(f"reservation {rid} belongs to {owner!r}, not {consumer!r}")
    return owner or consumer


_UNSET = object()


def _call(path: str, body: dict, *, timeout: float, token=_UNSET, headers=None, base_url=None, root: Optional[Path] = None) -> tuple:
    data = json.dumps(body, sort_keys=True).encode()
    req = urllib.request.Request((base_url or url()).rstrip("/") + path, data=data, method="POST", headers={"Content-Type": "application/json"})
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    tok = _token(str(body.get("consumer") or ""), root=root) if token is _UNSET else token
    if tok is not None and (not isinstance(tok, str) or not _TOKEN_SHAPE.fullmatch(tok)):
        raise TokenUnreadable("explicit token: malformed")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        # no proxy: allocd is loopback / mux-forwarded, and an http_proxy env must never capture it
        with _OPENER.open(req, timeout=timeout) as resp:          # noqa: S310
            answer = resp.status, json.loads(resp.read() or b"{}")
        _clear_slow_memo(root=root)                 # allocd answered: whatever was frozen moved
        return answer
    except urllib.error.HTTPError as exc:
        _clear_slow_memo(root=root)                 # an HTTP status is an answer too: allocd's handler ran
        if exc.code in (409, 410):
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                if exc.code == 409:
                    # A malformed 409 remains a refusal, never an admission through fallback.
                    return 409, {"reason": "refused (unparseable 409 body)"}
                error = Unreachable(f"HTTP {exc.code} with an unparseable body")
            except (OSError, http.client.HTTPException) as read_exc:
                if exc.code == 409:
                    # allocd answered 409: an unreadable body is still a refusal, never Unreachable -> fallback.
                    return 409, {"reason": "refused (unreadable 409 body)"}
                error = Unreachable(_transport_error(read_exc, req))
        elif 400 <= exc.code < 500 and exc.code not in (408, 429):
            # TERMINAL: authentication/ownership refusals cannot use the fallback ledger.
            error = Refused(f"HTTP {exc.code} from allocd on {path}")
        else:
            error = _root_refused(exc) or Unreachable(f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        error = (TimedOut if _is_timeout(exc) else Unreachable)(_transport_error(exc, req))
    # Raising outside the handler avoids retaining the credential-bearing exception on
    # __context__, even for consumers that explicitly walk suppressed exception chains.
    raise error


def _receipt(code: int, out, field: str) -> bool:
    """A lifecycle reply: 200 must carry a boolean ``field`` (else Unreachable, so strict/fallback handling runs,
    never "gone" and never AttributeError: t_430834ff); any other code allocd answered (410 gone, 409) is False."""
    if code == 200:
        if not isinstance(out, dict) or type(out.get(field)) is not bool:
            raise Unreachable(f"invalid {field} receipt")
        return out[field]
    return False


def _fallback_admit(consumer: str, *, job_id: str, ttl_s: float, klass: Optional[str], footprint, job_kw: Mapping,
                    previous, why: str, t0: float, candidates=None, root: Optional[Path] = None) -> AdmitResult:
    table = placement.load_hosts(root=root)
    if not ledger_is_local(table, root=root):
        # split-ledger rule: a non-router host's ledger is not allocd's; booking there double-admits
        log_event("allocd_unreachable_remote", consumer=consumer, job_id=job_id, op="admit", error=why, url=url(), root=root)
        return AdmitResult(None, None, "allocd_unreachable_remote", "fallback", consumer=consumer)
    log_event("allocd_unreachable", consumer=consumer, job_id=job_id, op="admit", error=why, url=url(), root=root)
    now = time.time()
    job = placement.Job(**job_kw) if job_kw else None
    # the fallback is today's in-process gate: no router-only state (the P0 reserve is router-only, spec §3.8 RC7)
    try:
        d, rid = placement.admit(table, consumer, read_samples(now=now, root=root), ttl_s=ttl_s, job_id=job_id, now=None,
                                 footprint=footprint, job=job, previous=previous, reuse_job=True,
                                 candidates=candidates, lock_timeout_s=_budget_left(t0), root=root)
    except placement.LockTimeout as exc:
        log_event("allocd_wedged_lock", consumer=consumer, job_id=job_id, op="admit", error=str(exc), root=root)
        return AdmitResult(None, None, "allocd_wedged_lock", "fallback", consumer=consumer)
    if rid:
        _OWNER[_state_key(rid, root)] = consumer
        _FALLBACK_ISSUED[_state_key(rid, root)] = {"consumer": consumer, "job_id": job_id, "ttl_s": ttl_s, "klass": klass,
                                 # the footprint actually RESERVED, detached (Prism 5663666bbaac): never the caller's
                                 # mutable mapping or None (= today's default, which may change before a re-admit)
                                 "footprint": dataclasses.asdict(d.footprint), "job": dict(job_kw),
                                 "previous": previous, "host": d.host}
    return AdmitResult(d.host, rid, d.reason, "fallback", d.rule, None, d.as_row() if d.rows else None, consumer)


def admit(consumer: str, *, job_id: Optional[str] = None, ttl_s: float = 1800.0, klass: Optional[str] = None,
          footprint: Optional[Mapping] = None, job: Optional[Mapping] = None, previous: Optional[str] = None,
          timeout: float = TIMEOUT_S, fallback: bool = True, candidates: Optional[list] = None,
          unified: bool = False, stale_after_s: Optional[float] = None, root: Optional[Path] = None) -> AdmitResult:
    """Ask allocd for a host; on any liveness fault run the same scorer in-process (``fallback=False`` raises
    Unreachable instead: tests only). ``job`` = placement.Job fields (affinity, continuity, priority, core_class, footprint, profile).
    ``klass`` = "p0" for the reserve-entitled class (main/merge-queue CI, lander), else None.
    ``candidates`` = restrict the scorer to these hosts (renew's re-admit pins the job's host with it).
    ``unified`` = ask allocd to run the ONE rule (admit_class) on its own ledger for the booked row; a grant that ran it
    carries ``unified: true`` in its detail (Prism 11efd32500fc). ``stale_after_s`` = the caller's sample freshness.
    Retry a deferred result with ``job_id=result.job_id``: allocd may have committed the timed-out request.
    Raises ``Refused`` on a terminal answer (4xx, empty/unreadable token file): the fallback never runs for it."""
    t0 = time.monotonic()
    tok = _token(consumer, root=root)                 # FIRST: an unreadable token refuses even with the router off
    job_id = job_id or f"anon-{uuid.uuid4().hex[:12]}"
    body = {"consumer": consumer, "job_id": job_id, "ttl_s": ttl_s, "class": klass, "footprint": footprint,
            "job": dict(job or {}), "previous": previous}
    if candidates is not None:
        body["candidates"] = list(candidates)
    if unified:
        body["unified"] = True
        if stale_after_s is not None:
            body["stale_after_s"] = float(stale_after_s)
    try:
        if (_fleet_root(root=root) / ROUTER_OFF_REL).exists():
            raise Unreachable("router-off file present")
        code, out = _call("/admit", body, timeout=timeout, token=tok, root=root)
        if code == 200 and isinstance(out, dict) and out.get("host") and out.get("reservation_id"):
            rid = out["reservation_id"]          # both non-empty (Prism 5f29877ec7ef): a 200 missing either is garbled
            _OWNER[_state_key(rid, root)] = consumer
            return AdmitResult(out["host"], rid, "", "allocd", out.get("rule") or "", out.get("band"), out, consumer, job_id)
        if code == 409:                    # final whatever the body shape (Prism 5bebc530350f)
            out = out if isinstance(out, dict) else {}
            return AdmitResult(None, None, str(out.get("reason") or "refused"), "allocd", "", None, out, consumer, job_id)
        raise Unreachable(f"unexpected reply {code}")
    except Unreachable as exc:
        if not fallback:
            raise
        if _allocd_slow(exc, t0, "admit", consumer, job_id=job_id, root=root):
            return AdmitResult(None, None, "allocd_slow", "deferred", consumer=consumer, job_id=job_id)
        result = _fallback_admit(consumer, job_id=job_id, ttl_s=ttl_s, klass=klass, footprint=footprint,
                                 job_kw=dict(job or {}), previous=previous, why=str(exc), t0=t0, candidates=candidates, root=root)
        return dataclasses.replace(result, job_id=job_id)


def shadow_admit(consumer: str, *, job_id: Optional[str] = None, klass: Optional[str] = None,
                 footprint: Optional[Mapping] = None, job: Optional[Mapping] = None, previous: Optional[str] = None,
                 timeout: float = TIMEOUT_S, candidates: Optional[list] = None, root: Optional[Path] = None) -> dict:
    """Card B SHADOW stage: ask allocd what it WOULD pick for this job (``shadow: true``: scored against the live
    ledger, nothing reserved, nothing logged there). Returns allocd's reply ``{shadow, host, reason, rule, reasons}``.
    NEVER falls back to the in-process scorer: a shadow score exists only to be compared with the old path's
    decision, and a fallback would compare the scorer with itself. Raises ``Unreachable`` (allocd off / down /
    garbled) or ``Refused`` (4xx, token) instead; the consumer logs that as a disagreement with a reason."""
    tok = _token(consumer, root=root)
    body = {"consumer": consumer, "job_id": job_id or f"shadow-{uuid.uuid4().hex[:12]}", "ttl_s": 1.0, "class": klass,
            "footprint": footprint, "job": dict(job or {}), "previous": previous, "shadow": True}
    if candidates is not None:
        body["candidates"] = list(candidates)
    if (_fleet_root(root=root) / ROUTER_OFF_REL).exists():
        raise Unreachable("router-off file present")
    code, out = _call("/admit", body, timeout=timeout, token=tok, root=root)
    if code == 200 and isinstance(out, dict) and out.get("shadow") is True:
        # A shadow reply is PROMOTION EVIDENCE for the consumer (old vs new host). A reply missing the decision
        # fields would compare as host=None and record a false "agree" against an old refusal; fail loud instead.
        for field, types in (("host", (str, type(None))), ("reason", (str,)), ("rule", (str, type(None)))):
            if field not in out:
                raise Unreachable(f"malformed shadow reply: missing {field}")
            if not isinstance(out[field], types):
                raise Unreachable(f"malformed shadow reply: bad {field} type {type(out[field]).__name__}")
        return out
    raise Unreachable(f"unexpected shadow reply {code}")


def _local_ledger_op(op: str, rid: str, consumer: str, why: str, t0: float, fn, *, root: Optional[Path] = None) -> Optional[bool]:
    """Run ``fn`` on the local ledger when it IS allocd's (bounded flock); None = not run (remote / wedged lock).
    ``fn`` passes ``owner=`` so placement verifies the row's owner UNDER the ledger lock: a mismatch is Refused,
    never a mutation (Prism dc3cb6019576: _owner() falls back to the caller when its ledger read failed)."""
    if "@" in consumer or not ledger_is_local(root=root):
        log_event("allocd_unreachable_remote", consumer=consumer, reservation=rid, op=op, error=why, url=url(), root=root)
        return None
    log_event("allocd_unreachable", consumer=consumer, reservation=rid, op=op, error=why, root=root)
    try:
        return fn(_budget_left(t0))
    except placement.OwnerMismatch as exc:
        raise Refused(str(exc)) from exc
    except placement.LockTimeout as exc:
        log_event("allocd_wedged_lock", consumer=consumer, reservation=rid, op=op, error=str(exc), root=root)
        return None


def release(reservation_id: str, *, consumer: str, timeout: float = TIMEOUT_S, actual: Optional[Mapping] = None,
            actual_source: Optional[str] = None, root: Optional[Path] = None) -> bool:
    """End a reservation; allocd first, the same ledger in-process when allocd is unreachable AND this is allocd's
    host. Idempotent. ``consumer`` is required and must be the reservation's owner. Raises ``Refused`` on a terminal
    answer (never released behind allocd's back). ``actual`` + ``actual_source`` (FWA spec I-9): the job's measured
    use and who measured it, sent on both routes; a source the allow-list refuses still ends the lease, unrecorded."""
    t0 = time.monotonic()
    rid = _current(reservation_id, root=root)
    owner = _owner(rid, consumer, root=root)
    tok = _token(owner, root=root)
    body: dict = {"reservation_id": rid, "consumer": owner}
    if actual is not None:
        body.update(actual=dict(actual), actual_source=actual_source)

    refused = None

    def _local(lt):
        nonlocal refused
        try:
            return placement.release(rid, lock_timeout_s=lt, owner=owner, actual=actual, actual_source=actual_source, root=root)
        except placement.ActualRefused as exc:
            refused = str(exc)
            return placement.release(rid, lock_timeout_s=lt, owner=owner, root=root)
    try:
        code, out = _call("/release", body, timeout=timeout, token=tok, root=root)
        ok = _receipt(code, out, "released")
        refused = out.get("actual_refused") if isinstance(out, dict) else None
    except Unreachable as exc:
        slow = _allocd_slow(exc, t0, "release", consumer, reservation=rid, root=root)
        ok = False if slow else bool(_local_ledger_op("release", rid, consumer, str(exc), t0, _local, root=root))
    _FALLBACK_ISSUED.pop(_state_key(rid, root), None)
    if ok:
        _OWNER.pop(_state_key(rid, root), None)
    if refused:
        log_event("actual_refused", consumer=owner, reservation=rid, reason=refused, root=root)
        raise Refused(f"actual refused after lease release: {refused}")
    return ok


def _readmit(rid: str, consumer: str, ttl_s: float, timeout: float, *, root: Optional[Path] = None) -> bool:
    """A reservation THIS process booked in the fallback is gone: book its job again (reuse_job) for the RENEW's
    ``ttl_s``, PINNED to the host the job runs on; alias old -> new. Another host is never a successful renew: the
    job keeps running where it is, so a pick elsewhere would count capacity on a host it does not use. A LIVE row
    handed back by reuse_job keeps its own ``until``, so it is renewed for ``ttl_s`` too (False +
    ``renew_not_extended`` when that renew fails). The recovery metadata stays until a replacement is established
    (Prism 377ac5c1eaf8): a deferred/raised re-admit leaves it for the next renew to retry, and a replacement that
    could not be extended carries it, so recovery is never lost to one transient failure."""
    kw = _FALLBACK_ISSUED.get(_state_key(rid, root))
    if kw is None:
        return False
    host = kw.get("host")
    r = admit(kw["consumer"], job_id=kw["job_id"], ttl_s=ttl_s, klass=kw["klass"], footprint=kw["footprint"],
              job=kw["job"], previous=kw["previous"], timeout=timeout, candidates=[host] if host else None, root=root)
    if r.reservation_id and host and r.host != host:
        log_event("renew_host_lost", consumer=kw["consumer"], reservation=rid, host=host, got=r.host,
                  replacement=r.reservation_id, reason="reuse row on another host", root=root)
        return False
    if not r.reservation_id:
        log_event("renew_host_lost", consumer=kw["consumer"], reservation=rid, host=host, reason=r.reason, root=root)
        return False
    if r.reservation_id != rid:
        _REPLACED[_state_key(rid, root)] = r.reservation_id
        _FALLBACK_ISSUED.pop(_state_key(rid, root), None)
        if r.source == "allocd":
            _FALLBACK_ISSUED.pop(_state_key(r.reservation_id, root), None)
    if r.rule == "reuse" and not _extend(r.reservation_id, kw["consumer"], ttl_s, timeout, root=root):
        _FALLBACK_ISSUED.setdefault(_state_key(r.reservation_id, root), kw)   # the alias now routes here: keep recovery reachable
        # reuse_job hands back a live row AS IS (its own until): True would claim ttl_s the ledger does not hold
        log_event("renew_not_extended", consumer=kw["consumer"], reservation=rid, replacement=r.reservation_id,
                  host=r.host, ttl_s=ttl_s, root=root)
        return False
    log_event("alloc_fallback_readmit", consumer=kw["consumer"], reservation=rid,
              replacement=r.reservation_id, host=r.host, ttl_s=ttl_s, root=root)
    return True


def _extend(rid: str, owner: str, ttl_s: float, timeout: float, *, root: Optional[Path] = None) -> bool:
    """Renew ``rid`` for ``ttl_s`` (allocd, else the bounded local-ledger fallback); never re-admits."""
    t0 = time.monotonic()
    tok = _token(owner, root=root)
    try:
        code, out = _call("/renew", {"reservation_id": rid, "ttl_s": ttl_s, "consumer": owner}, timeout=timeout,
                          token=tok, root=root)
        return _receipt(code, out, "renewed")
    except Unreachable as exc:
        if _allocd_slow(exc, t0, "renew", owner, reservation=rid, root=root):
            return False
        return bool(_local_ledger_op("renew", rid, owner, str(exc), t0,
                                     lambda lt: placement.renew(rid, ttl_s=ttl_s, lock_timeout_s=lt, owner=owner, root=root), root=root))


def renew(reservation_id: str, *, ttl_s: float, consumer: str, timeout: float = TIMEOUT_S,
          strict: bool = False, root: Optional[Path] = None) -> bool:
    """Extend a reservation. ``consumer`` is required and must be the reservation's owner. A fallback-issued
    reservation found gone is re-admitted for ``ttl_s`` on its own host (True only then; ``renew_host_lost`` and
    False when that host cannot take it). Raises ``Refused`` on a terminal answer.

    False means the row is GONE (allocd 410, or the local ledger says so) EXCEPT when the renew was NOT RUN at all
    (allocd unreachable from a non-router host, or a wedged local lock): by default that is also False ("the row
    stands until its TTL"). A lease loop that must tell the two apart passes ``strict=True``: the not-run case then
    raises ``Unreachable`` so the caller retries instead of giving the lease up (Prism 8a88d52a0e47)."""
    t0 = time.monotonic()
    rid = _current(reservation_id, root=root)
    owner = _owner(rid, consumer, root=root)
    tok = _token(owner, root=root)
    try:
        code, out = _call("/renew", {"reservation_id": rid, "ttl_s": ttl_s, "consumer": owner}, timeout=timeout,
                          token=tok, root=root)
        ok = _receipt(code, out, "renewed")
    except Unreachable as exc:
        res = None if _allocd_slow(exc, t0, "renew", consumer, reservation=rid, root=root) else \
            _local_ledger_op("renew", rid, consumer, str(exc), t0,
                             lambda lt: placement.renew(rid, ttl_s=ttl_s, lock_timeout_s=lt, owner=owner, root=root), root=root)
        if res is None:
            if strict:
                raise                     # not run: the caller keeps the lease and retries
            return False                  # not run (remote / wedged / slow): the row stands until its TTL
        ok = res
    if not ok and _state_key(rid, root) in _FALLBACK_ISSUED:
        return _readmit(rid, owner, ttl_s, timeout, root=root)
    return ok
