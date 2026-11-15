"""Fleet pause hold: a card of a paused host spawns nowhere (fleet pause/resume SPEC v0.3 §5.8, t_133d6efd).

The manifest ``<kanban_home()>/state/fleet-pause/<host>/manifest.json`` is written by ``scripts/fleet-pause``
(hermes-home). ONE predicate, :func:`decide`, evaluated in two layers:

* layer 1, :func:`gate_rows`: a post-route pass in ``_dispatch_once_locked`` after every route is known and
  before any spawn (ready lane before ``_browser_gate``, review lane after ``_browser_hold_review_rows``), so the
  plan branch, the no-plan branch and every way a row became ready pass through it;
* layer 2, :func:`refuse_if_held`: the first statement of the two run openers ``_claim_and_open_run`` and
  ``_open_review_run``, the only functions that turn a ready/review row into a running row + ``task_runs`` row.
  A held card raises :class:`PauseHeld` before the CAS; the caller's ``write_txn`` rolls back.

Path root is ``kanban_home()``, never a profile ``HERMES_HOME``. A missing manifest = no pause; an unreadable one
holds every card of that host (fail closed). Kill switch ``kanban.placement.pause_hold: on|off`` (default on),
read by both layers through :func:`enabled`.

Stale-hold fail-safes (t_0c5a95ab, Ace 2026-11-06 20:31 "the CI box is not paused anymore ... some kind of fail safe"):

* a hold-phase manifest past ``lease_until + LEASE_GRACE_S`` whose ``driver.pid`` is not alive is treated as
  resumed: :func:`decide` holds nothing and #alerts gets ONE "stale pause manifest auto-cleared" page per manifest
  id (latch ``<host>/stale-cleared.<id>.json``);
* a resume pin is owed only while the unit still awaits its first run (``resume.new_run_id`` unset and
  ``resume.action`` not a settled one); a pin that conflicts with the card's own route waits at most
  ``PIN_CONFLICT_GRACE_S`` after ``resumed_at`` and then routes ordinarily, so a resumed host never holds;
* :func:`clear_stale_labels` ends, on every live tick, the open ``placement_wait pause_hold`` of a card the current
  manifest no longer holds (``{cleared: true}``), so no reader sees a stale label for a card the dispatcher skips
  (no_worker / human lane cards are never routed, so their last label never moved);
* :func:`holds` is the read model behind ``hermes kanban placement-holds``.

Override: ``hermes kanban placement-holds --clear <id>`` ends one card's label (a task_event naming --operator);
``KANBAN_PAUSE_HOLD_STALE=0`` in the dispatcher's env turns off the lease/driver auto-clear and the pin-conflict
grace, so a stale manifest holds exactly as before.

FWA hold records (FWA pause/resume SPEC §5.1-§5.3, card B3 t_0b56b707): :func:`decide` DUAL-READS. A card of host H
is held when the legacy manifest holds it (above) OR a live fwa hold under ``<kanban_home()>/state/fwa/holds/<H>/``
covers lane ``kanban``. :func:`fwa_valid` / :func:`fwa_verdict` re-implement hermes-home ``scripts/lib/fwa.py``
``valid`` + ``accepting`` (the fork cannot import hermes-home); parity is held by the golden fixtures under
``tests/fixtures/fwa/`` (byte copies of hermes-home's), pinned by :data:`FWA_FIXTURES_SHA`, which the home sweep
reads from this file as text (§5.4 step 5). A hold past ``until`` never holds (I3); an unknown lane is ignored (§5.1).

End condition (SPEC v0.4 §5.4a, Ace: "so things aren't just paused forever"): every hold carries
``end: {"kind": "time", "until": T}`` (``--for``/``--until``) or ``{"kind": "reboot", "boot_id_at_pause",
"grace_s", "backstop_until": T}`` (``--until-reboot``), with ``T == until <= since + 24 h`` (the kind names are
hermes-home ``fwa.END_KINDS``; the fixtures pin them). Two failure cases, two directions:

* a structurally valid record whose ``end`` is ABSENT or MALFORMED is EXPIRED: it does not hold, and one warning per
  file names it (:func:`fwa_end_error`). The home sweep pages and archives it;
* a file that does not parse as JSON (a write in progress, disk damage) or is structurally invalid (no schema/id/
  host/lanes) holds (fail closed, §5.2) until its mtime + 24 h.

A ``reboot`` hold ends when the host's boot id changed AND the host answered for ``grace_s``, or at
``backstop_until``, whichever is first. The fork cannot read a host's boot id cheaply per tick, so here a reboot hold
is live until ``backstop_until`` UNLESS the home sweep has archived it (the sweep owns the reboot check and moves the
file out of ``holds/<host>/`` on release, which the next tick sees, I4) or has stamped ``end.rebooted_at`` and
``end.seen_up_at >= rebooted_at + grace_s`` on it (:func:`fwa_effective_end`, as home ``fwa.effective_end``). The
resume pin lapses at
``resumed_at + 2 h`` (§5.3). Kill switch ``kanban.placement.pause_hold_source: fwa|manifest`` (default ``fwa``,
§11): ``manifest`` drops the fwa arm and the pin lapse, which is the pre-B3 behaviour byte for byte.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

CODE = "pause_hold"
REFUSED_EVENT = "claim_refused_pause_hold"
UNREADABLE = "unreadable"
HOLD_PHASES = frozenset({"draining", "paused", "paused-forced", "paused-partial"})
PIN_PHASES = frozenset({"resuming", "resumed"})
STATUS_FILE = "hold-status.json"
_UNREADABLE = object()
#: a hold-phase manifest stays a hold this long past ``lease_until`` (= hermes-home host_window.GRACE_S).
LEASE_GRACE_S = 900
#: a resume pin that conflicts with the card's own route waits this long after ``resumed_at``, then lets go.
PIN_CONFLICT_GRACE_S = 900
#: ``unit.resume.action`` values under which no post-resume run is owed (fleet-pause stamps them).
SETTLED_ACTIONS = frozenset({"no_run_owed", "already_moved", "none"})
STALE_EVENT_REASON = "stale pause manifest auto-cleared"
STALE_ENV = "KANBAN_PAUSE_HOLD_STALE"
_warned_stale: set = set()

#: sha256 over the sorted ``(name, bytes)`` of ``tests/fixtures/fwa/*.json`` (SPEC §5.1, RC-5). MUST equal hermes-home
#: ``scripts/lib/fwa.py`` ``FWA_FIXTURES_SHA``; the home sweep compares the two as text (§5.4 step 5).
FWA_FIXTURES_SHA = "f69a00a12f3acd5c783ae2bfae0e4aac39d13a24825d5241e7c882469acdcf69"
FWA_LANE = "kanban"
FWA_SCHEMA = 1
FWA_MAX_HOLD_S = 24 * 3600
#: an unreadable hold file holds at most this long past its mtime (§5.2: a fail-closed file has an end too)
FWA_UNREADABLE_MAX_S = 24 * 3600
#: a resume pin lapses this long after ``resumed_at`` whether or not ``new_run_id`` was stamped (§5.3)
PIN_LAPSE_S = 2 * 3600
SOURCE_FWA, SOURCE_MANIFEST = "fwa", "manifest"
#: §5.4a end kinds (hermes-home ``fwa.END_KINDS``) and the reboot-hold grace cap
FWA_END_KINDS = ("time", "reboot")
FWA_MAX_GRACE_S = 7200
_warned_endless: set = set()
#: kill switch (env): an fwa hold follows ``origin_host`` for every card, pinned or not (pre-t_72362742)
FOLLOWS_ORIGIN_ENV = "KANBAN_PAUSE_FOLLOWS_ORIGIN"
#: ``placement_rerouted`` reason when an unpinned card leaves its fwa-held origin host for the allocator
REROUTE_EVENT = "placement_rerouted"
REROUTE_REASON = "origin_host_paused"


class _Unreadable:
    """An fwa hold file that cannot be parsed or fails :func:`fwa_valid`; ``mtime`` bounds how long it holds."""

    __slots__ = ("name", "mtime")

    def __init__(self, name: str, mtime: Optional[float]):
        self.name, self.mtime = name, mtime


class PauseHeld(Exception):
    """A run opener refused a card whose origin host is paused."""

    def __init__(self, host: str, manifest_id: str, task_id: str = ""):
        super().__init__(f"held by fleet-pause {manifest_id} (host {host})")
        self.host, self.manifest_id, self.task_id = host, manifest_id, task_id

    @property
    def reason(self) -> str:
        return f"{CODE} ({self.host}:{self.manifest_id} stage=claim)"


def root() -> Path:
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "state" / "fleet-pause"


def enabled() -> bool:
    """``kanban.placement.pause_hold`` from the ROOT config; anything but an explicit off is on."""
    try:
        from hermes_cli import kanban_db as _kb
        placement = (_kb._board_home_kanban_cfg() or {}).get("placement") or {}
        value = placement.get("pause_hold", "on") if isinstance(placement, dict) else "on"
    except Exception:
        _log.warning("kanban pause_hold: root config unreadable; hold stays on", exc_info=True)
        return True
    return value is not False and str(value).strip().lower() not in ("off", "false", "0", "no")


def source() -> str:
    """``kanban.placement.pause_hold_source`` (SPEC §11 B3 kill switch): ``fwa`` (default) dual-reads the fwa hold
    records and the legacy manifest; ``manifest`` reads the manifest only, exactly as before B3. An unreadable config
    or any other value is ``fwa``: the fwa arm only ADDS holds, so it is the fail-closed side."""
    try:
        from hermes_cli import kanban_db as _kb
        placement = (_kb._board_home_kanban_cfg() or {}).get("placement") or {}
        value = placement.get("pause_hold_source", SOURCE_FWA) if isinstance(placement, dict) else SOURCE_FWA
    except Exception:
        return SOURCE_FWA
    return SOURCE_MANIFEST if str(value).strip().lower() == SOURCE_MANIFEST else SOURCE_FWA


def fwa_root() -> Path:
    """``<kanban_home()>/state/fwa/holds``: the FLEET root, never a profile home (SPEC §5.1 root rule)."""
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "state" / "fwa" / "holds"


def _epoch(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def fwa_end_error(doc) -> Optional[str]:
    """hermes-home ``fwa.end_error`` (SPEC v0.4 §5.4a): None when ``doc`` carries a well-formed end that agrees with
    ``since``/``until``, else why not. A STRUCTURALLY valid hold with an end error is EXPIRED (it never holds), never
    fail-closed. Shapes: ``{"kind": "time", "until": T}``; ``{"kind": "reboot", "boot_id_at_pause": B,
    "grace_s": 0..7200, "backstop_until": T [, "rebooted_at": R, "seen_up_at": U]}`` (R/U are stamped by the home
    sweep); ``T == until`` and ``since < until <= since + 24 h`` either way."""
    end = doc.get("end")
    if not isinstance(end, dict):
        return "no end condition"
    since, until = doc.get("since"), doc.get("until")
    if not (_epoch(since) and _epoch(until)):
        return "since/until not numbers"
    if not since < until <= since + FWA_MAX_HOLD_S:
        return f"until must be in (since, since + {FWA_MAX_HOLD_S}s]"
    kind = end.get("kind")
    if kind == "time":
        return None if end.get("until") == until else "end.until != until"
    if kind == "reboot":
        b = end.get("boot_id_at_pause")
        if not isinstance(b, str) or not b.strip():
            return "end.boot_id_at_pause missing"
        g = end.get("grace_s")
        if not (_epoch(g) and 0 <= g <= FWA_MAX_GRACE_S):
            return f"end.grace_s must be 0..{FWA_MAX_GRACE_S}"
        if end.get("backstop_until") != until:
            return "end.backstop_until != until (the backstop is mandatory)"
        for k in ("rebooted_at", "seen_up_at"):
            if end.get(k) is not None and not _epoch(end.get(k)):
                return f"end.{k} not a number"
        return None
    return f"end.kind {kind!r} not in {FWA_END_KINDS}"


def fwa_valid(doc) -> bool:
    """hermes-home ``fwa.valid`` (SPEC §5.1): STRUCTURAL validity only. False = unreadable, which holds (fail
    closed) until mtime + 24 h. The end is judged by :func:`fwa_end_error` (bad end = expired). Unknown lanes do NOT
    invalidate a doc (RC-5)."""
    if not isinstance(doc, dict) or doc.get("schema") != FWA_SCHEMA:
        return False
    for key in ("id", "host"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            return False
    lanes = doc.get("lanes")
    return isinstance(lanes, list) and bool(lanes) and all(isinstance(x, str) for x in lanes)


def fwa_effective_end(doc) -> float:
    """hermes-home ``fwa.effective_end``: ``until``; for a reboot end, ``rebooted_at + grace_s`` once the sweep has
    stamped ``seen_up_at >= rebooted_at + grace_s``, never later than the backstop. The fork never reads a boot id
    itself: an unstamped reboot hold is live until its backstop unless the sweep archived it."""
    end = doc.get("end") or {}
    until = float(doc["until"])
    rb, up = end.get("rebooted_at"), end.get("seen_up_at")
    if end.get("kind") == "reboot" and _epoch(rb) and _epoch(up):
        due = float(rb) + float(end.get("grace_s") or 0)
        if float(up) >= due:
            return min(until, due)
    return until


def fwa_verdict(entries, lane: str, now: float):
    """hermes-home ``fwa.accepting`` over already-loaded entries (dicts, or :class:`_Unreadable`) with
    ``on_unreadable="hold"``: ``None`` = accepting, else ``(kind, id, doc_or_None)`` of the first hold covering
    ``lane``. A structurally invalid entry holds every lane until ``mtime + 24 h`` (no mtime = holds); a valid doc
    with an end error is expired (§5.4a); else it holds iff ``since <= now < effective_end`` and ``lane in lanes``
    (I3: expired never holds)."""
    for e in entries:
        if isinstance(e, _Unreadable) or not fwa_valid(e):
            mtime = e.mtime if isinstance(e, _Unreadable) else None
            if mtime is None or now < mtime + FWA_UNREADABLE_MAX_S:
                return (UNREADABLE, e.name if isinstance(e, _Unreadable) else str((e or {}).get("id") or "?"), None)
            continue
        if fwa_end_error(e) is not None:
            continue                                        # §5.4a: no / a malformed end = expired, never a hold
        if e["since"] <= now < fwa_effective_end(e) and lane in e["lanes"]:
            return (str(e.get("kind") or "pause"), e["id"], e)
    return None


#: per-tick memo keys of the fwa arm. The legacy manifest memo keys are host STRINGS (``cache[host]``); every fwa key
#: is a tuple whose first item names its namespace, so no host name (``table``, ``fwa``, ...) can alias another slot
#: (Prism d6a2cb37de48).
_K_REGISTRY = "fwa:registry"
_K_ROOT = "fwa:root"
_K_ENTRIES = "fwa:entries"
_K_PINS = "fwa:pins"


def registry_path() -> Path:
    """The alias registry of the OWNING kanban home: ``<kanban_home()>/config/fleet-hosts.json``, the same file
    hermes-home ``fwa._host_table`` reads (Prism abd3aba4b5be). Never ``placement.fleet_root()`` (the checkout or
    PLACEMENT_HOME) and never a profile home."""
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "config" / "fleet-hosts.json"


def _registry(cache: dict) -> dict:
    """``{canonical_id: {id, aliases..., ssh_alias}}`` lower-cased, as home ``fwa.resolve`` matches names. Read once
    per tick per kanban home; a missing or unreadable registry resolves no aliases (home ``_host_table`` returns {})."""
    path = registry_path()
    key = (_K_REGISTRY, str(path))
    if key in cache:
        return cache[key]
    table: dict = {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        hosts = doc.get("hosts") if isinstance(doc, dict) else None
        for hid, row in (hosts if isinstance(hosts, dict) else {}).items():
            row = row if isinstance(row, dict) else {}
            aliases = row.get("aliases")                   # optional: only a list counts (Prism 1de03430bf61)
            aliases = aliases if isinstance(aliases, list) else ()
            names = {str(hid).lower(), *(a.lower() for a in aliases if isinstance(a, str))}
            ssh = (row.get("net") or {}).get("ssh_alias") if isinstance(row.get("net"), dict) else None
            if isinstance(ssh, str) and ssh:
                names.add(ssh.lower())
            table[str(hid).lower()] = names
    except FileNotFoundError:
        pass
    except Exception:                                      # any bad shape resolves no aliases, never a fleet hold
        _log.warning("kanban pause_hold: %s unreadable; fwa hold aliases unresolved", path)
        table = {}
    cache[key] = table
    return table


def _host_names(host: str, cache: dict) -> set:
    """``host`` plus every name of the registry group(s) that contain it, lower-cased (SPEC §5.2)."""
    low = host.strip().lower()
    names = {low}
    for group in _registry(cache).values():
        if low in group:
            names |= group
    return names


def _root_listing(cache: dict):
    """``(base, names, error)`` for ``fwa_root()``, memoised per tick. ABSENT (FileNotFoundError: no ``state``, no
    ``state/fwa`` or no ``holds``) is ``([], None)`` = no hold; any other OSError (EACCES on the root or a parent, a
    file where a directory belongs, EIO) is ``([], exc)`` = fail closed (Prism 616898796fc2: ``Path.is_dir()`` folds
    EACCES into "absent", so it is never used on this path)."""
    base = fwa_root()
    key = (_K_ROOT, str(base))
    if key not in cache:
        try:
            cache[key] = (base, sorted(n for n in os.listdir(base) if not n.startswith(".")), None)
        except FileNotFoundError:
            cache[key] = (base, [], None)
        except OSError as exc:
            cache[key] = (base, [], exc)
    return cache[key]


def _host_files(d: Path):
    """``(files, error)`` of one host directory. ``None`` files = not a directory or gone (not a hold); an error =
    the directory exists but cannot be listed (fail closed)."""
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".json") and not n.startswith(".")), None
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as exc:
        return None, exc


def fwa_entries(host: str, cache: dict) -> list:
    """Every hold file of ``host`` (any alias's directory), parsed: a valid dict or an :class:`_Unreadable`. Per-tick
    memo in ``cache`` (I4: never persisted across ticks). Lock, dot and tmp files are not holds. A hold root or host
    directory that exists but cannot be read is ONE unreadable entry with no mtime: it holds until it reads again."""
    base, listing, err = _root_listing(cache)
    key = (_K_ENTRIES, str(base), host)
    if key in cache:
        return cache[key]
    out: list = []
    if err is not None:
        _log.warning("kanban pause_hold: %s unlistable (%s); every %s card is HELD", base, err, host)
        out.append(_Unreadable(str(base), None))
    names = _host_names(host, cache)
    for name in listing:
        if name.lower() not in names:
            continue
        d = base / name
        files, ferr = _host_files(d)
        if ferr is not None:
            _log.warning("kanban pause_hold: %s unlistable (%s); every %s card is HELD", d, ferr, host)
            out.append(_Unreadable(name, None))
            continue
        for fname in files or ():
            p = d / fname
            mtime = None
            try:
                mtime = p.stat().st_mtime
                doc = json.loads(p.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                continue                                    # archived between listdir and read: not a hold
            except (OSError, ValueError, RecursionError):   # ValueError covers UnicodeDecodeError
                out.append(_Unreadable(p.name, mtime))
                continue
            if not fwa_valid(doc):
                out.append(_Unreadable(p.name, mtime))
                continue
            end_err = fwa_end_error(doc)
            if end_err is not None:
                _note_endless(p, mtime, end_err)
                continue
            out.append(doc)
    cache[key] = out
    return out


def _note_endless(path: Path, mtime: Optional[float], why: str) -> None:
    """ONE warning per (file, mtime) per process: a hold with no / a malformed end is treated as expired."""
    key = (str(path), mtime)
    if key in _warned_endless:
        return
    _warned_endless.add(key)
    _log.warning("kanban pause_hold: fwa hold %s has %s; treated as expired, it does not hold (SPEC §5.4a)",
                 path, why)


def _fwa_hold(origin_host: str, cache: dict, now: Optional[float] = None):
    """``("wait", "pause_hold (<host>:fwa:<kind>:<id> ...)")`` when an fwa hold covers lane kanban, else None."""
    if source() != SOURCE_FWA:
        return None
    t = time.time() if now is None else float(now)
    hit = fwa_verdict(fwa_entries(origin_host, cache), FWA_LANE, t)
    if hit is None:
        return None
    kind, hid, doc = hit
    if doc is None:
        return ("wait", f"{CODE} ({origin_host}:fwa:{UNREADABLE}:{hid})")
    until = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(fwa_effective_end(doc)))
    by = str(doc.get("by") or doc.get("owner") or "?").replace("(", "[").replace(")", "]")
    return ("wait", f"{CODE} ({origin_host}:fwa:{kind}:{hid} by {by} until {until})")


def destination_wait(host: Optional[str], cache: dict) -> Optional[str]:
    """The placement_wait reason when an fwa hold covers lane kanban on the host a worker would START on, else None
    (Prism 71b4b90b5c73: the origin fence alone lets a ``host:`` pin or a portable pick place new work on a held
    host). The fwa arm only: with ``pause_hold_source: manifest`` or ``pause_hold: off`` it is None, so dormant ==
    pre-B3. Never raises; an unexpected failure holds that destination (fail closed)."""
    if not host or not enabled():
        return None
    try:
        hit = _fwa_hold(str(host), cache)
    except Exception:
        _log.warning("kanban pause_hold: destination check for %s failed; destination HELD", host, exc_info=True)
        return f"{CODE} ({host}:fwa:{UNREADABLE} stage=destination)"
    return None if hit is None else f"{hit[1][:-1]} stage=destination)"


def follows_origin() -> bool:
    """Kill switch ``KANBAN_PAUSE_FOLLOWS_ORIGIN=1`` (dispatcher env): an fwa hold freezes every card whose
    ``origin_host`` is the held host, pinned or not, exactly as before t_72362742. Default off."""
    return os.environ.get(FOLLOWS_ORIGIN_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def _pin_words(cache: dict):
    """``(known_hosts, aliases)`` of the pool registry, read once per tick.

    Errors propagate to decide's fail-closed handler: a missing alias map is
    not proof that a direct claim is unpinned (there is no destination fence).
    """
    key = (_K_PINS,)
    if key not in cache:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_worker_pool as _kwp
        fleet = _kb.kanban_home() / "fleet"
        cache[key] = (tuple(_kwp.known_host_ids(fleet, strict=True)),
                      dict(_kwp.pin_aliases(fleet, strict=True)))
    return cache[key]


def pinned_to(body: Optional[str], host: str, cache: dict) -> bool:
    """Whether the card's effective ``host:`` pin names ``host`` (by id, registry alias or fwa registry name;
    ``host: studio`` names ``mac-studio``). No pin, ``host: any`` or a pin to another host is NOT pinned: such a card
    is placed by the allocator, so a hold on its last host does not freeze it (t_72362742)."""
    from hermes_cli import kanban_worker_pool as _kwp
    names = _host_names(str(host), cache)
    known, aliases = _pin_words(cache)
    pin, ignored = _kwp.resolve_pin(body, (*known, *names), aliases)
    word = pin if pin is not None else ignored
    if not word or word == _kwp.PIN_ANY:
        return False
    if word == _kwp.PIN_STUDIO:
        return _kwp.STUDIO_HOST in names
    return str(word).lower() in names


def local_host() -> str:
    """The dispatcher's own host as an fwa directory name: ``socket.gethostname()`` short form, lower-cased. Its
    registry aliases (``<kanban_home()>/config/fleet-hosts.json``) are resolved by :func:`_host_names`, so a hold
    written under the canonical id (``mac-studio``, ``ci-box``) closes a local start too."""
    import socket
    try:
        return socket.gethostname().split(".", 1)[0].strip().lower()
    except OSError:
        return ""


_FQDN_MEMO: dict = {}


def _fqdn(name: str) -> str:
    """``socket.getfqdn(name)`` once per process per hostname (a resolver lookup; never per tick on the hot path)."""
    if name not in _FQDN_MEMO:
        import socket
        try:
            _FQDN_MEMO[name] = str(socket.getfqdn(name) or "")
        except Exception:
            _FQDN_MEMO[name] = ""
    return _FQDN_MEMO[name]


def local_names() -> list:
    """Every name the dispatcher's own host may be held under, short form first: ``gethostname()`` and its resolved
    FQDN, each as written and with the domain stripped (Prism 9fb3e16068f3: a host whose ``gethostname`` is short
    but whose registry/hold name is only reachable from the full name must still be fenced). Lower-cased, de-duped."""
    import socket
    try:
        raw = str(socket.gethostname() or "").strip().lower()
    except OSError:
        raw = ""
    out: list = []
    for full in (raw, _fqdn(raw).strip().lower() if raw else ""):
        for n in (full.split(".", 1)[0], full):
            if n and n not in out:
                out.append(n)
    return out


def local_destination_wait(cache: dict) -> Optional[str]:
    """:func:`destination_wait` for a LOCAL start (no pool host chosen: a local route, the last-resort fallback,
    Prism fe6a1ad69c44, or an immediate ``spawn_now`` launch, Prism b256ef26919e), over every name in
    :func:`local_names`. An unknown hostname is no destination check (the origin fence still applies)."""
    for host in local_names():
        why = destination_wait(host, cache)
        if why is not None:
            return why
    return None


def held_destinations(names, cache: dict) -> dict:
    """``{host: reason}`` for every candidate pool host a live fwa hold closes to new kanban work this tick."""
    out = {}
    for n in names or ():
        why = destination_wait(n, cache)
        if why is not None:
            out[n] = why
    return out


DEST_STAGE = "stage=destination"


def label_verdict(origin_host: Optional[str], task_id: str, detail, cache: dict):
    """Whether an open ``pause_hold`` label still holds, as :func:`decide` returns it. A label a DESTINATION fence
    wrote (detail ``<host>:fwa:... stage=destination``) is judged by that host, which it names first: its card may
    have no origin, or an unheld one (Prism b622dcc4adae / 5afa560835bc). Any other label is judged by its origin."""
    if isinstance(detail, str) and detail.rstrip().endswith(DEST_STAGE):
        why = destination_wait(detail.split(":", 1)[0].strip(), cache)
        return None if why is None else ("wait", why)
    return decide(origin_host, task_id, cache)


def _valid(doc) -> bool:
    """Every field :func:`decide` dereferences has the shape it expects (Prism aceb690c4071)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("phase"), str) or not doc.get("host"):
        return False
    units = doc.get("units")
    if units is None:
        return True
    if not isinstance(units, list):
        return False
    for unit in units:
        if not isinstance(unit, dict):
            return False
        resume = unit.get("resume")
        if resume is not None and not isinstance(resume, dict):
            return False
    return True


def _load(host: str, cache: dict):
    if host in cache:
        return cache[host]
    path = root() / host / "manifest.json"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        cache[host] = None
        return None
    except (OSError, ValueError):   # ValueError: UnicodeDecodeError on non-UTF-8 bytes
        _log.warning("kanban pause_hold: %s unreadable; every %s card is HELD", path, host)
        cache[host] = _UNREADABLE
        return _UNREADABLE
    try:
        doc = json.loads(raw)
        if not _valid(doc):
            raise ValueError("manifest shape")
    except (ValueError, RecursionError):
        _log.warning("kanban pause_hold: %s unreadable; every %s card is HELD", path, host)
        doc = _UNREADABLE
    cache[host] = doc
    return doc


def decide(origin_host: Optional[str], task_id: str, cache: dict, *, body: Optional[str] = None):
    """None | ("wait", "pause_hold (<host>:<id>)") | ("pin", host). Pure over the manifest contents.

    ``body`` (a READY card's body; None = unknown) scopes the fwa arm (t_72362742): a live fwa hold on the origin
    holds the card only when its ``host:`` pin names that host (:func:`pinned_to`). An unpinned card is left to the
    allocator, which never places it on the held host (destination fence). ``body=None`` (review lane, labels of
    unknown lane) and ``KANBAN_PAUSE_FOLLOWS_ORIGIN=1`` keep the origin-keyed hold. The legacy manifest arm is
    always origin-keyed: its resume pins return drained work to its host.

    Never raises: any unexpected failure holds the host's card (fail closed), so neither layer can take a
    dispatch tick or a claim down with something other than :class:`PauseHeld`."""
    try:
        return _decide(origin_host, task_id, cache, body)
    except Exception:
        _log.warning("kanban pause_hold: decide(%s) failed; card %s HELD", origin_host, task_id, exc_info=True)
        if origin_host:
            cache[str(origin_host)] = _UNREADABLE
        return ("wait", f"{CODE} ({origin_host}:{UNREADABLE})")


def _decide(origin_host, task_id, cache, body=None):
    """Dual-read (SPEC §5.3, §8 P1): a legacy wait stands; else a live fwa hold on lane kanban waits (it beats a
    resume pin: a held host takes no run, pinned or not); else the legacy pin / None."""
    if not origin_host:
        return None
    legacy = _decide_manifest(origin_host, task_id, cache)
    if legacy is not None and legacy[0] == "wait":
        return legacy
    fwa = _fwa_hold(str(origin_host), cache)
    if fwa is not None and body is not None and not follows_origin() and not pinned_to(body, str(origin_host), cache):
        return legacy                                       # unpinned: the allocator re-places it (t_72362742)
    return fwa or legacy


def _decide_manifest(origin_host, task_id, cache):
    doc = _load(str(origin_host), cache)
    if doc is None:
        return None
    if doc is _UNREADABLE:
        return ("wait", f"{CODE} ({origin_host}:{UNREADABLE})")
    phase = doc.get("phase")
    if phase in HOLD_PHASES:
        why = stale_reason(doc)
        if why:
            _note_stale(str(origin_host), doc, why)
            return None
        return ("wait", f"{CODE} ({origin_host}:{doc.get('id') or '?'})")
    if phase in PIN_PHASES:
        if _pin_lapsed(doc):
            return None
        for unit in doc.get("units") or ():
            if unit.get("lane") == "kanban" and unit.get("id") == task_id:
                return ("pin", str(origin_host)) if owes_run(unit) else None
    return None


def _pin_lapsed(doc: dict, now: Optional[float] = None) -> bool:
    """SPEC §5.3 (I-A class): a ``resumed`` manifest's pins lapse at ``resumed_at + PIN_LAPSE_S`` whether or not
    ``new_run_id`` was stamped. ``resuming`` never lapses here (the driver is still running). Off under
    ``pause_hold_source: manifest``."""
    if doc.get("phase") != "resumed" or source() != SOURCE_FWA:
        return False
    at = doc.get("resumed_at")
    if not _epoch(at):
        return False
    t = time.time() if now is None else float(now)
    return t >= float(at) + PIN_LAPSE_S


def owes_run(unit: dict) -> bool:
    """A resumed kanban unit still owed its first post-resume run (the resume pin applies)."""
    resume = unit.get("resume") or {}
    return resume.get("new_run_id") is None and resume.get("action") not in SETTLED_ACTIONS


def stale_failsafe_on() -> bool:
    """Kill switch ``KANBAN_PAUSE_HOLD_STALE=0`` (env, no deploy needed): turns off the lease/driver auto-clear and the
    resume pin-conflict grace, so a stale manifest holds as it did before t_0c5a95ab. Default on."""
    return os.environ.get(STALE_ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def _driver_alive(doc: dict) -> bool:
    """The manifest's ``driver.pid`` exists on THIS host. No pid / a non-int = dead. Cross-platform via psutil (a
    core dependency); a probe that cannot run counts as alive, so the hold stands (fail closed)."""
    pid = (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        return True


def stale_reason(doc: dict, now: Optional[float] = None) -> Optional[str]:
    """Why a HOLD-phase manifest no longer holds, or None while it does: its lease (+ LEASE_GRACE_S) ran out and no
    driver process is alive. A manifest without a numeric ``lease_until`` never goes stale here (fail closed), and
    nothing goes stale with ``KANBAN_PAUSE_HOLD_STALE=0``."""
    if not stale_failsafe_on():
        return None
    t = time.time() if now is None else float(now)
    lease = doc.get("lease_until")
    if not isinstance(lease, (int, float)) or isinstance(lease, bool):
        return None
    if t < float(lease) + LEASE_GRACE_S or _driver_alive(doc):
        return None
    pid = (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None
    return (f"phase {doc.get('phase')} lease expired {int((t - float(lease)) // 60)} min ago and the driver "
            f"(pid {pid}) is gone")


def _pager():
    """``(argv_prefix)`` sender for the stale page, or None in a hermetic home (no notify.py: never page)."""
    from hermes_cli import kanban_budget as _kbudget
    script = _kbudget._notify_script_path()
    if script is None:
        return None
    import sys
    return lambda body: _kbudget._run_notify([sys.executable, script, "--sev", "error", "--send", body]) is not False


def _note_stale(host: str, doc: dict, why: str) -> None:
    """ONE #alerts page per stale manifest id, latched on disk only after the send went out. Never raises."""
    mid = str(doc.get("id") or "?")
    try:
        latch = root() / host / f"stale-cleared.{mid}.json"
        if latch.exists():
            return
        if mid not in _warned_stale:
            _warned_stale.add(mid)
            _log.warning("kanban pause_hold: %s %s treated as resumed: %s", host, mid, why)
        send = _pager()
        if send is None:
            return
        body = (f"🧹 {STALE_EVENT_REASON}: fleet-pause {host} {mid} ({why}). The dispatcher no longer holds {host} "
                f"cards on it. Resume or abandon it properly: `fleet-pause status {host}`, then "
                f"`fleet-pause resume {host}` / `fleet-pause abandon {host} --reason ...`.")
        if send(body):
            latch.write_text(json.dumps({"id": mid, "host": host, "why": why, "at": int(time.time())}),
                             encoding="utf-8")
    except Exception:
        _log.warning("kanban pause_hold: stale page for %s %s failed", host, mid, exc_info=True)


def _row(r, key):
    try:
        return r[key]
    except (IndexError, KeyError):
        return None


def held_routes(rows, cache: dict) -> dict:
    """Ready-lane PRE-ROUTE pass (Prism 76b7233abe29): ``{task_id: ("wait", reason)}`` for every row the hold
    keeps. The dispatcher routes only the rows NOT in this dict, so ``origin_route`` (which can persist
    ``origin_host=NULL`` under continuation=enforce) never sees a held card. Empty when the switch is off."""
    if not rows or not enabled():
        return {}
    out = {}
    for r in rows:
        verdict = decide(_row(r, "origin_host"), r["id"], cache, body=_row(r, "body"))
        if verdict is not None and verdict[0] == "wait":
            out[r["id"]] = verdict
    return out


def _pin_conflict(r, current, host: str, conn=None, pool=None, caps=None) -> Optional[str]:
    """Why a resume pin to ``host`` must WAIT instead of being applied, or None:

    * an explicit ``host: studio`` pin, or a ``local`` route carrying a rule (access-bound, studio affinity,
      unknown pin, not_portable) (Prism 536374336fb5); ``("local", None)`` and ``portable`` may be pinned;
    * a ``pin``/``origin`` route to ANOTHER host (Prism c3421355edd2): the resume fence never runs elsewhere;
    * the origin's hard checks (registry, assignee, hard capabilities, Studio-only access, split brain), the
      same ones ``origin_route`` applies, but without its probe arm, so an empty probe never clears the origin
      of a held/resuming card (Prism 904f143f5a38). Needs a pool plan; the caller waits planless anyway."""
    from hermes_cli import kanban_worker_pool as _kwp
    body = _row(r, "body")
    if body:
        pin, _ignored = _kwp.resolve_pin(body, ())
        if pin == _kwp.PIN_STUDIO:
            return "host_studio"
    if current and current[0] == "local" and current[1]:
        return str(current[1])
    if current and current[0] in ("pin", "origin") and current[1] and str(current[1]).lower() != host.lower():
        return f"routed_to:{current[1]}"
    if conn is not None and pool is not None:
        from hermes_cli import kanban_continuation as _kcont
        why = _kcont.origin_hard_refusal(conn, r, pool, host, caps)
        if why:
            return f"origin_refused:{why}"
    return None


def _pin_conflict_expired(host: str, cache: dict, now: Optional[float] = None) -> bool:
    """A ``resumed`` manifest whose ``resumed_at`` is more than PIN_CONFLICT_GRACE_S ago. ``resuming`` never
    expires here: the resume driver is still running and the pin is its fence."""
    doc = cache.get(str(host))
    if not stale_failsafe_on() or not isinstance(doc, dict) or doc.get("phase") != "resumed":
        return False
    at = doc.get("resumed_at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    t = time.time() if now is None else float(now)
    return t - float(at) >= PIN_CONFLICT_GRACE_S


def _write_status(mode: str, now: float) -> None:
    from hermes_cli import kanban_db_dispatch as _kbd
    try:
        interval = float((_kbd._root_kanban_cfg() or {}).get("dispatch_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        interval = 60.0
    path = root() / STATUS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{STATUS_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"mode": mode, "pid": os.getpid(), "updated_at": int(now),
                                   "interval_s": interval}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _log.warning("kanban pause_hold: could not write %s", path, exc_info=True)


def gate_rows(conn, rows, result, row_route: dict, dry_run: bool, cache: dict, *, lane: str,
              planned: bool = True, entry: str = "gateway", pins_out: Optional[dict] = None,
              pool=None, caps=None):
    """Layer 1. Ready lane: writes ``row_route`` and returns ``rows``. Review lane: records the wait in
    ``result.placement_waits``, writes a resume pin into ``pins_out`` (Prism 01f1e61334ff) and returns the rows
    that may proceed.

    ``hold-status.json`` is published on the ready lane only by a live GATEWAY tick (RC-P3-2: a one-shot
    ``hermes kanban dispatch`` runs the same code from disk before the gateway restarts and must not vouch for it).
    """
    on = enabled()
    if lane == "ready" and not dry_run and entry == "gateway":
        _write_status("on" if on else "off", time.time())
    if not on or not rows:
        return rows
    kept = []
    for r in rows:
        verdict = decide(_row(r, "origin_host"), r["id"], cache,
                         body=_row(r, "body") if lane == "ready" else None)
        if verdict is None:
            kept.append(r)
            continue
        if verdict[0] == "wait":
            if lane == "review":
                result.placement_waits[r["id"]] = verdict[1]
                continue
            row_route[r["id"]] = verdict
            kept.append(r)
            continue
        current = row_route.get(r["id"]) if lane == "ready" else None
        conflict = _pin_conflict(r, current, verdict[1], conn, pool, caps)
        if conflict is not None and _pin_conflict_expired(verdict[1], cache):
            # The pin can never be honoured (the card's own route forbids the origin) and the card can never earn
            # the first run that would retire the pin: past the grace it routes ordinarily (t_0c5a95ab).
            kept.append(r)
            continue
        if conflict is not None:
            why = f"{CODE} ({verdict[1]}:resume_pin_conflict:{conflict})"
            if lane == "review":
                result.placement_waits[r["id"]] = why
                continue
            row_route[r["id"]] = ("wait", why)
            kept.append(r)
            continue
        if lane == "review":
            if not planned:
                result.placement_waits[r["id"]] = "pool_unavailable"
                continue
            if pins_out is not None:
                pins_out[r["id"]] = verdict[1]
            kept.append(r)
            continue
        kept.append(r)
        if current and current[0] in ("pin", "origin", "wait"):
            continue  # same destination (a conflict waited above), or another gate's wait: leave it
        # RC-P3-1: with no pool plan a pin route reaches spillover.take(None); wait like a body pin does (I-11).
        row_route[r["id"]] = verdict if planned else ("wait", "pool_unavailable")
    return kept if lane == "review" else rows


def _open_pause_labels(conn) -> list:
    """``[(task_id, origin_host, detail)]``: ready/review cards whose LATEST placement_wait is an open (not cleared,
    no episode end after it) ``pause_hold``."""
    from hermes_cli import kanban_placement_reasons as _pr
    end = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    rows = conn.execute(
        "SELECT t.id, t.origin_host, e.payload FROM tasks t "
        "JOIN task_events e ON e.id = (SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind IN (?, ?)) "
        "WHERE t.status IN ('ready', 'review') AND NOT EXISTS (SELECT 1 FROM task_events x "
        "  WHERE x.task_id = t.id AND x.id > e.id AND x.kind IN (%s))" % ",".join("?" * len(end)),
        (*_pr.kind_names(_pr.PLACEMENT_WAIT_EVENT), *end),
    ).fetchall()
    out = []
    for tid, origin, raw in rows:
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict) or p.get("cleared"):
            continue
        code = p.get("code") or str(p.get("reason") or "").split(" ", 1)[0]
        if code == CODE:
            out.append((tid, origin, p.get("detail")))
    return out


def clear_stale_labels(conn, result, dry_run: bool, cache: dict) -> list:
    """End the open ``pause_hold`` placement_wait of every card the CURRENT manifest no longer holds and this tick
    did not hold for another reason: one ``{cleared: true, reason}`` event each. Returns the cleared ids.

    The hold itself is always re-derived from the manifest by :func:`decide`; this only retires the LABEL a card
    keeps when the dispatcher never routes it again (no_worker, a human-lane assignee, a card whose origin_host was
    dropped). 11-06: t_1bf66aac / t_6aa3a051 read ``pause_hold (ci-box:...)`` 37 h after ci-box resumed and its
    manifest was archived. Best-effort, never raises; nothing on a dry run or with the switch off."""
    if dry_run or not enabled():
        return []
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        waits = getattr(result, "placement_waits", None) or {}
        stale = []
        for tid, origin, detail in _open_pause_labels(conn):
            if tid in waits:
                continue                           # this tick holds it: its own wait row is current
            verdict = label_verdict(origin, tid, detail, cache)
            if verdict is not None and verdict[0] == "wait":
                continue
            doc = cache.get(str(origin)) if origin else None
            now_reads = (f"{str(detail).split(':', 1)[0]} has no live fwa kanban hold"
                         if isinstance(detail, str) and detail.rstrip().endswith(DEST_STAGE) else
                         "card has no origin_host" if not origin else
                         f"{origin} manifest phase {doc.get('phase')}" if isinstance(doc, dict) else
                         f"{origin} has no manifest.json")
            stale.append((tid, f"{CODE} ({detail}) no longer holds: {now_reads}"))
        if not stale:
            return []
        with _kb.write_txn(conn):
            for tid, why in stale:
                _kb._append_event(conn, tid, _pr.PLACEMENT_WAIT_EVENT, {"cleared": True, "reason": why})
        return [t for t, _ in stale]
    except Exception:
        _log.warning("kanban pause_hold: could not clear stale pause_hold labels", exc_info=True)
        return []


def holds(conn=None, *, now: Optional[float] = None) -> dict:
    """Read model for ``hermes kanban placement-holds``: every host manifest under :func:`root` with whether it
    holds right now, and (with ``conn``) the cards carrying an open ``pause_hold`` label, each marked live/stale."""
    t = time.time() if now is None else float(now)
    hosts = {}
    base = root()
    for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        doc = _load(d.name, {})
        if doc is None:
            hosts[d.name] = {"manifest": None, "holds": False, "why": "no manifest.json (archived or never paused)"}
            continue
        if doc is _UNREADABLE:
            hosts[d.name] = {"manifest": None, "holds": True, "why": "manifest unreadable: every card HELD"}
            continue
        phase = doc.get("phase")
        stale = stale_reason(doc, t) if phase in HOLD_PHASES else None
        owed = [u.get("id") for u in doc.get("units") or () if u.get("lane") == "kanban" and owes_run(u)] \
            if phase in PIN_PHASES else []
        hosts[d.name] = {
            "manifest": doc.get("id"), "phase": phase, "lease_until": doc.get("lease_until"),
            "resumed_at": doc.get("resumed_at"),
            "driver_pid": (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None,
            "driver_alive": _driver_alive(doc),
            "holds": phase in HOLD_PHASES and not stale,
            "why": stale or (f"phase {phase}" if phase in HOLD_PHASES else f"phase {phase}: not a hold"),
            "pins_owed": owed,
            "pin_conflict_expired": _pin_conflict_expired(d.name, {d.name: doc}, t),
        }
    fwa: dict = {}
    if source() == SOURCE_FWA:
        memo: dict = {(_K_REGISTRY, str(registry_path())): {}}   # one row per directory: no alias fan-in here
        base, listing, err = _root_listing(memo)
        if err is not None:
            fwa[str(base)] = [{"file": str(base), "unreadable": True, "holds_kanban": True}]
        for name in listing:
            if _host_files(base / name) == (None, None):
                continue                                    # a stray file at the root, not a host directory
            rows = []
            for e in fwa_entries(name, memo):
                if isinstance(e, _Unreadable) and e.name == str(base):
                    continue                                # the root itself: reported once above
                if isinstance(e, _Unreadable):
                    rows.append({"file": e.name, "unreadable": True,
                                 "holds_kanban": fwa_verdict([e], FWA_LANE, t) is not None})
                    continue
                rows.append({"id": e["id"], "kind": e.get("kind"), "lanes": e["lanes"], "by": e.get("by"),
                             "owner": e.get("owner"), "reason": e.get("reason"), "since": e["since"],
                             "until": e["until"], "holds_kanban": fwa_verdict([e], FWA_LANE, t) is not None})
            fwa[name] = rows
    cards = []
    if conn is not None:
        cache: dict = {}
        for tid, origin, detail in _open_pause_labels(conn):
            verdict = label_verdict(origin, tid, detail, cache)
            cards.append({"task_id": tid, "origin_host": origin, "label": detail,
                          "live": verdict is not None and verdict[0] == "wait",
                          "now": None if verdict is None else list(verdict)})
    return {"now": int(t), "hosts": hosts, "fwa": fwa, "source": source(), "cards": cards}


def clear_card(conn, task_id: str, operator: str) -> bool:
    """Operator verb: end ``task_id``'s open pause_hold label now (``{cleared: true}``). It does NOT lift a live
    hold: the next tick re-derives it from the manifest and writes a fresh wait. True when a label was open."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_placement_reasons as _pr
    if not any(tid == task_id for tid, _o, _d in _open_pause_labels(conn)):
        return False
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, _pr.PLACEMENT_WAIT_EVENT,
                          {"cleared": True, "reason": f"operator clear: {operator}"})
    return True


def refuse_if_held(conn, task_id: str) -> None:
    """Layer 2: raise :class:`PauseHeld` for a card whose origin host is in a hold phase. Caller holds the txn;
    the raise rolls it back, so nothing is written here (RC-P3-3: the caller records the refusal)."""
    if not enabled():
        return
    row = conn.execute("SELECT origin_host, status, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    origin = row[0] if row is not None else None
    if not origin:
        return
    # A review opener keeps the origin-keyed hold; any other claim is scoped by the card's pin (t_72362742).
    verdict = decide(origin, task_id, {}, body=None if row[1] == "review" else (row[2] or ""))
    if verdict is not None and verdict[0] == "wait":
        manifest_id = verdict[1].split(":", 1)[1].rstrip(")")
        raise PauseHeld(str(origin), manifest_id, task_id)


def record_refusal(conn, exc: PauseHeld, *, stage: str = "claim") -> None:
    """The ``claim_refused_pause_hold`` event, in its OWN txn after the opener's txn rolled back. Best-effort."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    try:
        with _kbc.write_txn(conn):
            _kb._append_event(conn, exc.task_id, REFUSED_EVENT,
                              {"host": exc.host, "manifest_id": exc.manifest_id, "stage": stage})
    except Exception:
        _log.warning("kanban pause_hold: could not record the refusal for %s", exc.task_id, exc_info=True)
