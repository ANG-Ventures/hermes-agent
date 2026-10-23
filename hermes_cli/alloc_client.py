# SCHEDULE: manual   (a library consumers import; never scheduled; card t_552773f1)
# VENDORED from hermes-home ``scripts/lib/alloc_client.py`` @ 2fe117cbcb (hermes-home#4462 merge, t_dc9a0f9a + t_314b3ea5;
# sha256 fbbd8a806628a79acc2d0ff981d75c29b48d3eb5c9f2333a51efa88519971cb3) for the kanban dispatcher (t_4187c8f1): the fork may not import hermes-home code. The ONLY
# local change is the placement import (hermes_cli.placement, not sys.path + import placement); tests/hermes_cli/test_kanban_allocd_route.py checks
# the rest against the canonical file (ALLOC_CLIENT_CANONICAL).
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
  * allocd UNREACHABLE (connection refused, timeout, 5xx, 503 router-off, a garbled reply) is a LIVENESS fault: the
    client logs ``allocd_unreachable`` to var/placement/events.jsonl and runs the SAME scorer in-process on the same
    ledger (fail-OPEN). "Unseen != lost": a request allocd never answered is not a refusal.
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
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Mapping, Optional

from hermes_cli import placement  # VENDORED: the fork's placement copy (hermes-home: sys.path + import placement)

DEFAULT_URL = "http://127.0.0.1:18850"
TIMEOUT_S = 2.0                      # spec §6: a client-side 2 s timeout fires only when allocd is wedged
FALLBACK_BUDGET_S = 2.8              # the whole call (allocd attempt + fallback flock wait) answers within this
DEFAULT_ROUTER_HOST = "mac-studio"
PRESSURE_REL = Path("var") / "fleet-pressure.json"
EVENTS_REL = Path("var") / "placement" / "events.jsonl"
TOKENS_REL = Path("var") / "alloc" / "tokens"
ROUTER_OFF_REL = Path("var") / "alloc" / "router-off"
PROTECTED_REL = Path("var") / "alloc" / "protected"      # <consumer> marker: this consumer authenticates
P0 = "p0"
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


def url() -> str:
    return os.environ.get("ALLOCD_URL") or DEFAULT_URL


def pressure_path(path=None) -> Path:
    return Path(path) if path is not None else placement.fleet_root() / PRESSURE_REL


def read_samples(path=None, *, now: float):
    """The scorer's samples from var/fleet-pressure.json. A missing / unreadable document = no samples = every host
    closed (fail-CLOSED), never an exception that a caller could mistake for a liveness fault."""
    try:
        doc = json.loads(pressure_path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    try:
        return placement.samples_from_pressure(doc, now=now)
    except placement.PlacementError:
        return {}


def log_event(kind: str, **fields) -> None:
    """Best-effort events.jsonl line (kanban-placement-watch counts kinds there). Never raises."""
    p = placement.fleet_root() / EVENTS_REL
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "at": time.time(), **fields}, sort_keys=True) + "\n")
    except OSError:
        pass
    sys.stderr.write(f"alloc_client: {kind} {json.dumps(fields, sort_keys=True)}\n")


class Refused(Exception):
    """A TERMINAL answer (bad request, bad/missing token, wrong owner): never a reason to fall back."""


class TokenUnreadable(Refused):
    """A token file exists but is empty or unreadable: fail closed, never "no auth"."""


def _protected(consumer: str) -> bool:
    """True once ``consumer`` has had a token (minted, or read by any client/allocd): var/alloc/protected/<consumer>.
    A stat error other than "missing" counts as protected (fail closed)."""
    try:
        os.stat(placement.fleet_root() / PROTECTED_REL / consumer)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


_DURABLE_MARKERS: set = set()         # marker paths THIS process fsync'd (file + dir); a visible marker is not proof


def mark_protected(consumer: str) -> None:
    """Record that ``consumer`` uses a token, so a later MISSING token file is refused, never "no auth"."""
    d = placement.fleet_root() / PROTECTED_REL
    d.mkdir(parents=True, exist_ok=True)
    # durable before any token use succeeds (Prism f5e6f9863e5c): a crash that loses the marker while a later token
    # removal survives would read as "no auth"; fsync the file AND its directory entry (OSError -> caller fails closed)
    fd = os.open(str(d / consumer), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    _DURABLE_MARKERS.add(str(d / consumer))


def _token(consumer: str) -> Optional[str]:
    """The consumer's bearer token. None ONLY when the file is missing AND the consumer was never protected (no
    token ever minted or read for it); empty / unreadable / missing-for-a-protected-consumer raises TokenUnreadable.
    This is the one auth rule for allocd and the fallback alike: in the fallback the name is never the credential
    (review r3, Prism 092aba2d5cc6). An empty or non-plain consumer name is refused outright."""
    if not isinstance(consumer, str) or not consumer or consumer in (".", "..") or "/" in consumer or "\x00" in consumer:
        raise Refused(f"consumer name required (got {consumer!r})")
    p = placement.fleet_root() / TOKENS_REL / consumer
    try:
        tok = p.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        if _protected(consumer):
            raise TokenUnreadable(f"{p}: missing for a protected consumer") from None
        return None
    except (OSError, ValueError) as exc:
        raise TokenUnreadable(f"{p}: unreadable: {exc}") from exc
    if not tok:
        raise TokenUnreadable(f"{p}: empty")
    # an EXISTING marker is not enough (Prism 768d6f7d34aa): one left visible by a failed fsync is not durable, so
    # re-commit until THIS process has seen both fsyncs succeed for it
    if str(placement.fleet_root() / PROTECTED_REL / consumer) not in _DURABLE_MARKERS:
        try:
            mark_protected(consumer)       # first use protects it: a token removed later never reads as "no auth"
        except OSError as exc:
            # fail closed (review r5, Prism ad9b504687a4): an unwritable marker would let a later token removal read
            # as "no auth" for a consumer that WAS authenticated
            raise TokenUnreadable(f"{p}: cannot record protected marker: {exc}") from exc
    return tok


class Unreachable(Exception):
    """allocd did not give an answer this client can act on (a liveness fault, not a refusal)."""


def _router_host() -> str:
    try:
        doc = json.loads((placement.fleet_root() / placement.TABLE_REL).read_text(encoding="utf-8-sig"))
        r = doc.get("router") if isinstance(doc, dict) else None
        h = r.get("host") if isinstance(r, dict) else None
        return h if isinstance(h, str) and h else DEFAULT_ROUTER_HOST
    except (OSError, ValueError):
        return DEFAULT_ROUTER_HOST


def ledger_is_local(table=None) -> bool:
    """True when this process runs on allocd's host, i.e. the fallback's ledger IS allocd's ledger."""
    me = os.environ.get("ALLOC_CLIENT_HOST") or socket.gethostname()
    want = _router_host()
    try:
        table = table or placement.load_hosts()
    except placement.PlacementError:
        return False
    names = {me, me.split(".")[0]}
    mine = {table.resolve(n) or n for n in names}
    return (table.resolve(want) or want) in mine


# reservations booked by THIS process in the fallback: rid -> admit kwargs (re-admitted with reuse_job when gone)
_FALLBACK_ISSUED: dict = {}
_REPLACED: dict = {}                  # old rid -> the rid that replaced it
_OWNER: dict = {}                     # rid -> the consumer that booked it (from this process's AdmitResults)


def _current(rid: str) -> str:
    seen = set()
    while rid in _REPLACED and rid not in seen:
        seen.add(rid)
        rid = _REPLACED[rid]
    return rid


def _budget_left(t0: float) -> float:
    return max(0.05, FALLBACK_BUDGET_S - (time.monotonic() - t0))


def _owner(rid: str, consumer: str) -> str:
    """The reservation's owner: this process's AdmitResult, else the local ledger row (router host), else the caller's
    ``consumer`` (allocd then checks the row's owner token). A known owner that is not ``consumer`` is Refused."""
    _token(consumer)                       # validates the name (empty -> Refused) and the caller's own token file
    owner = _OWNER.get(rid)
    if owner is None and ledger_is_local():   # only the router host's ledger is allocd's (split-ledger rule)
        try:
            for row in placement._read_ledger(placement.reservations_path())["rows"]:
                if row.get("id") == rid:
                    owner = row.get("consumer")
                    break
        except Exception:                  # noqa: BLE001 -- unreadable/absent local ledger: allocd decides
            owner = None
    if owner and owner != consumer:
        raise Refused(f"reservation {rid} belongs to {owner!r}, not {consumer!r}")
    return owner or consumer


_UNSET = object()


def _call(path: str, body: dict, *, timeout: float, token=_UNSET) -> tuple:
    data = json.dumps(body, sort_keys=True).encode()
    req = urllib.request.Request(url() + path, data=data, method="POST", headers={"Content-Type": "application/json"})
    tok = _token(str(body.get("consumer") or "")) if token is _UNSET else token
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        # no proxy: allocd is loopback / mux-forwarded, and an http_proxy env must never capture it
        with _OPENER.open(req, timeout=timeout) as resp:          # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (409, 410):
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError as e:
                if exc.code == 409:
                    # a 409 IS the scorer's refusal whatever its body (Prism 5bebc530350f): never Unreachable, which
                    # would run the fallback (no P0 reserve) and admit what allocd refused
                    return 409, {"reason": "refused (unparseable 409 body)"}
                raise Unreachable(f"HTTP {exc.code} with an unparseable body") from e
        if 400 <= exc.code < 500 and exc.code not in (408, 429):
            # TERMINAL: a token/owner/shape refusal must never be bypassed through the fallback's ledger write
            raise Refused(f"HTTP {exc.code} from allocd on {path}") from exc
        raise Unreachable(f"HTTP {exc.code}") from exc         # 5xx, 503 router-off, 408/429
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unreachable(f"{type(exc).__name__}: {exc}") from exc


def _fallback_admit(consumer: str, *, job_id: str, ttl_s: float, klass: Optional[str], footprint, job_kw: Mapping,
                    previous, why: str, t0: float, candidates=None) -> AdmitResult:
    table = placement.load_hosts()
    if not ledger_is_local(table):
        # split-ledger rule: a non-router host's ledger is not allocd's; booking there double-admits
        log_event("allocd_unreachable_remote", consumer=consumer, job_id=job_id, op="admit", error=why, url=url())
        return AdmitResult(None, None, "allocd_unreachable_remote", "fallback", consumer=consumer)
    log_event("allocd_unreachable", consumer=consumer, job_id=job_id, op="admit", error=why, url=url())
    now = time.time()
    job = placement.Job(**job_kw) if job_kw else None
    # the fallback is today's in-process gate: no router-only state (the P0 reserve is router-only, spec §3.8 RC7)
    try:
        d, rid = placement.admit(table, consumer, read_samples(now=now), ttl_s=ttl_s, job_id=job_id, now=None,
                                 footprint=footprint, job=job, previous=previous, reuse_job=True,
                                 candidates=candidates, lock_timeout_s=_budget_left(t0))
    except placement.LockTimeout as exc:
        log_event("allocd_wedged_lock", consumer=consumer, job_id=job_id, op="admit", error=str(exc))
        return AdmitResult(None, None, "allocd_wedged_lock", "fallback", consumer=consumer)
    if rid:
        _OWNER[rid] = consumer
        _FALLBACK_ISSUED[rid] = {"consumer": consumer, "job_id": job_id, "ttl_s": ttl_s, "klass": klass,
                                 # the footprint actually RESERVED, detached (Prism 5663666bbaac): never the caller's
                                 # mutable mapping or None (= today's default, which may change before a re-admit)
                                 "footprint": dataclasses.asdict(d.footprint), "job": dict(job_kw),
                                 "previous": previous, "host": d.host}
    return AdmitResult(d.host, rid, d.reason, "fallback", d.rule, None, d.as_row() if d.rows else None, consumer)


def admit(consumer: str, *, job_id: Optional[str] = None, ttl_s: float = 1800.0, klass: Optional[str] = None,
          footprint: Optional[Mapping] = None, job: Optional[Mapping] = None, previous: Optional[str] = None,
          timeout: float = TIMEOUT_S, fallback: bool = True, candidates: Optional[list] = None) -> AdmitResult:
    """Ask allocd for a host; on any liveness fault run the same scorer in-process (``fallback=False`` raises
    Unreachable instead: tests only). ``job`` = placement.Job fields (affinity, continuity, priority, core_class, footprint, profile).
    ``klass`` = "p0" for the reserve-entitled class (main/merge-queue CI, lander), else None.
    ``candidates`` = restrict the scorer to these hosts (renew's re-admit pins the job's host with it).
    Raises ``Refused`` on a terminal answer (4xx, empty/unreadable token file): the fallback never runs for it."""
    t0 = time.monotonic()
    tok = _token(consumer)                 # FIRST: an unreadable token refuses even with the router off
    job_id = job_id or f"anon-{uuid.uuid4().hex[:12]}"
    body = {"consumer": consumer, "job_id": job_id, "ttl_s": ttl_s, "class": klass, "footprint": footprint,
            "job": dict(job or {}), "previous": previous}
    if candidates is not None:
        body["candidates"] = list(candidates)
    try:
        if (placement.fleet_root() / ROUTER_OFF_REL).exists():
            raise Unreachable("router-off file present")
        code, out = _call("/admit", body, timeout=timeout, token=tok)
        if code == 200 and isinstance(out, dict) and out.get("host") and out.get("reservation_id"):
            rid = out["reservation_id"]          # both non-empty (Prism 5f29877ec7ef): a 200 missing either is garbled
            _OWNER[rid] = consumer
            return AdmitResult(out["host"], rid, "", "allocd", out.get("rule") or "", out.get("band"), out, consumer)
        if code == 409:                    # final whatever the body shape (Prism 5bebc530350f)
            out = out if isinstance(out, dict) else {}
            return AdmitResult(None, None, str(out.get("reason") or "refused"), "allocd", "", None, out, consumer)
        raise Unreachable(f"unexpected reply {code}")
    except Unreachable as exc:
        if not fallback:
            raise
        return _fallback_admit(consumer, job_id=job_id, ttl_s=ttl_s, klass=klass, footprint=footprint,
                               job_kw=dict(job or {}), previous=previous, why=str(exc), t0=t0, candidates=candidates)


def shadow_admit(consumer: str, *, job_id: Optional[str] = None, klass: Optional[str] = None,
                 footprint: Optional[Mapping] = None, job: Optional[Mapping] = None, previous: Optional[str] = None,
                 timeout: float = TIMEOUT_S, candidates: Optional[list] = None) -> dict:
    """Card B SHADOW stage: ask allocd what it WOULD pick for this job (``shadow: true``: scored against the live
    ledger, nothing reserved, nothing logged there). Returns allocd's reply ``{shadow, host, reason, rule, reasons}``.
    NEVER falls back to the in-process scorer: a shadow score exists only to be compared with the old path's
    decision, and a fallback would compare the scorer with itself. Raises ``Unreachable`` (allocd off / down /
    garbled) or ``Refused`` (4xx, token) instead; the consumer logs that as a disagreement with a reason."""
    tok = _token(consumer)
    body = {"consumer": consumer, "job_id": job_id or f"shadow-{uuid.uuid4().hex[:12]}", "ttl_s": 1.0, "class": klass,
            "footprint": footprint, "job": dict(job or {}), "previous": previous, "shadow": True}
    if candidates is not None:
        body["candidates"] = list(candidates)
    if (placement.fleet_root() / ROUTER_OFF_REL).exists():
        raise Unreachable("router-off file present")
    code, out = _call("/admit", body, timeout=timeout, token=tok)
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


def _local_ledger_op(op: str, rid: str, consumer: str, why: str, t0: float, fn) -> Optional[bool]:
    """Run ``fn`` on the local ledger when it IS allocd's (bounded flock); None = not run (remote / wedged lock).
    ``fn`` passes ``owner=`` so placement verifies the row's owner UNDER the ledger lock: a mismatch is Refused,
    never a mutation (Prism dc3cb6019576: _owner() falls back to the caller when its ledger read failed)."""
    if not ledger_is_local():
        log_event("allocd_unreachable_remote", consumer=consumer, reservation=rid, op=op, error=why, url=url())
        return None
    log_event("allocd_unreachable", consumer=consumer, reservation=rid, op=op, error=why)
    try:
        return fn(_budget_left(t0))
    except placement.OwnerMismatch as exc:
        raise Refused(str(exc)) from exc
    except placement.LockTimeout as exc:
        log_event("allocd_wedged_lock", consumer=consumer, reservation=rid, op=op, error=str(exc))
        return None


def release(reservation_id: str, *, consumer: str, timeout: float = TIMEOUT_S) -> bool:
    """End a reservation; allocd first, the same ledger in-process when allocd is unreachable AND this is allocd's
    host. Idempotent. ``consumer`` is required and must be the reservation's owner. Raises ``Refused`` on a terminal
    answer (never released behind allocd's back)."""
    t0 = time.monotonic()
    rid = _current(reservation_id)
    owner = _owner(rid, consumer)
    tok = _token(owner)
    try:
        code, out = _call("/release", {"reservation_id": rid, "consumer": owner}, timeout=timeout, token=tok)
        ok = code == 200 and bool(out.get("released"))
    except Unreachable as exc:
        ok = bool(_local_ledger_op("release", rid, consumer, str(exc), t0,
                                   lambda lt: placement.release(rid, lock_timeout_s=lt, owner=owner)))
    _FALLBACK_ISSUED.pop(rid, None)
    if ok:
        _OWNER.pop(rid, None)
    return ok


def _readmit(rid: str, consumer: str, ttl_s: float, timeout: float) -> bool:
    """A reservation THIS process booked in the fallback is gone: book its job again (reuse_job) for the RENEW's
    ``ttl_s``, PINNED to the host the job runs on; alias old -> new. Another host is never a successful renew: the
    job keeps running where it is, so a pick elsewhere would count capacity on a host it does not use. A LIVE row
    handed back by reuse_job keeps its own ``until``, so it is renewed for ``ttl_s`` too (False +
    ``renew_not_extended`` when that renew fails). The recovery metadata stays until a replacement is established
    (Prism 377ac5c1eaf8): a deferred/raised re-admit leaves it for the next renew to retry, and a replacement that
    could not be extended carries it, so recovery is never lost to one transient failure."""
    kw = _FALLBACK_ISSUED.get(rid)
    if kw is None:
        return False
    host = kw.get("host")
    r = admit(kw["consumer"], job_id=kw["job_id"], ttl_s=ttl_s, klass=kw["klass"], footprint=kw["footprint"],
              job=kw["job"], previous=kw["previous"], timeout=timeout, candidates=[host] if host else None)
    if r.reservation_id and host and r.host != host:
        log_event("renew_host_lost", consumer=kw["consumer"], reservation=rid, host=host, got=r.host,
                  replacement=r.reservation_id, reason="reuse row on another host")
        return False
    if not r.reservation_id:
        log_event("renew_host_lost", consumer=kw["consumer"], reservation=rid, host=host, reason=r.reason)
        return False
    if r.reservation_id != rid:
        _REPLACED[rid] = r.reservation_id
        _FALLBACK_ISSUED.pop(rid, None)
        if r.source == "allocd":
            _FALLBACK_ISSUED.pop(r.reservation_id, None)
    if r.rule == "reuse" and not _extend(r.reservation_id, kw["consumer"], ttl_s, timeout):
        _FALLBACK_ISSUED.setdefault(r.reservation_id, kw)   # the alias now routes here: keep recovery reachable
        # reuse_job hands back a live row AS IS (its own until): True would claim ttl_s the ledger does not hold
        log_event("renew_not_extended", consumer=kw["consumer"], reservation=rid, replacement=r.reservation_id,
                  host=r.host, ttl_s=ttl_s)
        return False
    log_event("alloc_fallback_readmit", consumer=kw["consumer"], reservation=rid,
              replacement=r.reservation_id, host=r.host, ttl_s=ttl_s)
    return True


def _extend(rid: str, owner: str, ttl_s: float, timeout: float) -> bool:
    """Renew ``rid`` for ``ttl_s`` (allocd, else the bounded local-ledger fallback); never re-admits."""
    t0 = time.monotonic()
    tok = _token(owner)
    try:
        code, out = _call("/renew", {"reservation_id": rid, "ttl_s": ttl_s, "consumer": owner}, timeout=timeout,
                          token=tok)
        return code == 200 and bool(out.get("renewed"))
    except Unreachable as exc:
        return bool(_local_ledger_op("renew", rid, owner, str(exc), t0,
                                     lambda lt: placement.renew(rid, ttl_s=ttl_s, lock_timeout_s=lt, owner=owner)))


def renew(reservation_id: str, *, ttl_s: float, consumer: str, timeout: float = TIMEOUT_S,
          strict: bool = False) -> bool:
    """Extend a reservation. ``consumer`` is required and must be the reservation's owner. A fallback-issued
    reservation found gone is re-admitted for ``ttl_s`` on its own host (True only then; ``renew_host_lost`` and
    False when that host cannot take it). Raises ``Refused`` on a terminal answer.

    False means the row is GONE (allocd 410, or the local ledger says so) EXCEPT when the renew was NOT RUN at all
    (allocd unreachable from a non-router host, or a wedged local lock): by default that is also False ("the row
    stands until its TTL"). A lease loop that must tell the two apart passes ``strict=True``: the not-run case then
    raises ``Unreachable`` so the caller retries instead of giving the lease up (Prism 8a88d52a0e47)."""
    t0 = time.monotonic()
    rid = _current(reservation_id)
    owner = _owner(rid, consumer)
    tok = _token(owner)
    try:
        code, out = _call("/renew", {"reservation_id": rid, "ttl_s": ttl_s, "consumer": owner}, timeout=timeout,
                          token=tok)
        ok = code == 200 and bool(out.get("renewed"))
    except Unreachable as exc:
        res = _local_ledger_op("renew", rid, consumer, str(exc), t0,
                               lambda lt: placement.renew(rid, ttl_s=ttl_s, lock_timeout_s=lt, owner=owner))
        if res is None:
            if strict:
                raise                     # not run: the caller keeps the lease and retries
            return False                  # not run (remote / wedged): the row stands until its TTL
        ok = res
    if not ok and rid in _FALLBACK_ISSUED:
        return _readmit(rid, owner, ttl_s, timeout)
    return ok
