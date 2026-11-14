"""Fleet-priority FAULT TAXONOMY for the kanban door (Apollo ruling 2026-11-05 19:30, card t_0c5ef02e).

1. REGISTRY absent or unreadable (fleet/admission-priority.json): spawn-only, i.e. today's behaviour, but loud: a
   warning every tick and ONE page while door config exists on the box, cleared when the file is back.
2. CONFIG fault in fcfs/classes (pair token missing/empty, host table missing or without the host, typed
   auth/invalid): refuse every start, the card that hit it is blocked kind=capability, and the door is LATCHED (no
   further door calls) until the token, table or registry changes. ONE page.
3. TRANSPORT fault in fcfs/classes (allocd unreachable, timeout, 5xx, malformed reply): refuse new starts, the unit
   keeps its id, no latch. Retry on a capped backoff (doubling from the tick interval to at most 5 min); ONE page
   past 10 min, cleared on the first success.
4. ORDER refusals (queued_behind, capacity, not_enqueued): normal; no state here.

State is one small JSON file under the fleet root (var/kanban/door-faults.json), so a gateway restart neither
re-pages nor forgets a latch. Existing runs are never touched by anything here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

_log = logging.getLogger(__name__)

STATE_REL = Path("var") / "kanban" / "door-faults.json"
TOKENS_REL = Path("var") / "alloc" / "tokens"
TRANSPORT_MAX_BACKOFF_S = 300.0
TRANSPORT_PAGE_AFTER_S = 600.0


def _now() -> float:
    return time.time()


def _root() -> Path:
    from hermes_cli.fleet_placement import placement
    return Path(placement.fleet_root())


#: Same value and root rule as admission_demand.PRIORITY_REL / _root() (pinned by
#: test_registry_path_matches_the_vendored_library). Computed here so the fault-state and registry-read path never
#: imports the vendored admission_demand, whose module top imports POSIX-only fcntl/pwd (Prism 3e3d55eb04a0).
PRIORITY_REL = Path("fleet") / "admission-priority.json"
#: admission_demand._NONCE, the root's generation file (same no-import rule; pinned by
#: test_a_root_generation_repair_clears_the_latch).
ROOT_GENERATION_REL = ".placement-root-generation"


def _registry_path() -> Path:
    # The package's own root rule (bound onto admission_demand._root on first use); fleet_placement/__init__ has
    # no POSIX-only imports, so this path is safe on native Windows.
    from hermes_cli import fleet_placement
    return Path(fleet_placement._demand_root()) / PRIORITY_REL


def _table_path() -> Path:
    from hermes_cli.fleet_placement import placement
    return _root() / placement.TABLE_REL


def registry_state() -> str:
    """``ok`` | ``missing`` | ``unreadable``: our own existence/parse check, never the shared loader's defaults."""
    try:
        doc = json.loads(_registry_path().read_bytes())
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unreadable"
    return "ok" if isinstance(doc, dict) else "unreadable"


#: credentials the door path reads besides the kanban@<host> pair tokens: ``kanban`` authenticates the
#: existing-owner reservation upgrade (authorize_start existing_consumer), ``asap-operator`` the urgency verb.
_OTHER_TOKENS = ("kanban", "asap-operator")


def _kanban_tokens():
    d = _root() / TOKENS_REL
    try:
        return sorted(p for p in d.iterdir() if p.name.startswith("kanban@"))
    except OSError:
        return []


def _door_credentials():
    """EVERY credential file the door path reads (round 5, Prism d5a47f67cca8): repairing any of them must clear a
    latch it caused. Absent files are listed too, so creating one changes the fingerprint."""
    d = _root() / TOKENS_REL
    return [*_kanban_tokens(), *(d / name for name in _OTHER_TOKENS)]


def door_config_present() -> bool:
    """Any door config on this box: the host table or a kanban pair token."""
    return _table_path().exists() or bool(_kanban_tokens())


def fingerprint() -> str:
    """Changes when any door credential, the host table or the registry changes (path, mtime, size, mode, owner,
    ctime): a permission-only repair (chmod 000 -> 600) moves mode and ctime only (Prism 917208e77049)."""
    h = hashlib.sha256()
    # Root provisioning (#9789) gates every token read, so repairing it must clear the latch it caused
    # (Prism f6e8a5c31293): the receipt env value, the receipt file it names, and the root's generation file.
    receipt = (os.environ.get("PLACEMENT_ROOT_RECEIPT") or "").strip()
    h.update(f"receipt-env:{receipt};".encode())
    for p in (_registry_path(), _table_path(), *_door_credentials(), *((Path(receipt),) if receipt else ()),
              _root() / ROOT_GENERATION_REL):
        try:
            st = p.stat()
            h.update(f"{p}:{st.st_mtime_ns}:{st.st_size}:{st.st_mode}:{st.st_uid}:{st.st_ctime_ns};".encode())
        except OSError:
            h.update(f"{p}:-;".encode())
    return h.hexdigest()


def _lock(fh, *, lock: bool = True) -> None:
    """Exclusive whole-file lock/unlock: flock on POSIX, an msvcrt byte lock on native Windows (Prism df51bfc576d9:
    a POSIX-only lock made every state operation raise there)."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if lock else fcntl.LOCK_UN)


@contextlib.contextmanager
def _state():
    """Read-modify-write of the state file under a flock; atomic replace on write."""
    path = _root() / STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as lock:  # windows-footgun: ok (binary lock file, never read)
        _lock(lock)
        try:
            try:
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(doc, dict):
                    doc = {}
            except (OSError, ValueError):
                doc = {}
            before = json.dumps(doc, sort_keys=True)
            yield doc
            if json.dumps(doc, sort_keys=True) != before:
                fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".door-faults.")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, sort_keys=True)
                os.replace(tmp, path)
        finally:
            _lock(lock, lock=False)


def _page(text: str) -> bool:
    """Out-of-agent page through the fleet notifier (no-op where none is installed, e.g. a test home)."""
    from hermes_cli import kanban_budget
    script = kanban_budget._notify_script_path()
    if script is None:
        _log.warning("kanban door page (no notifier): %s", text)
        return False
    return kanban_budget._run_notify([sys.executable, script, "--sev", "warn", "--send", text])


def tick_interval() -> float:
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        value = float((kbd._root_kanban_cfg() or {}).get("dispatch_interval_seconds", 60) or 60)
    except Exception:
        value = 60.0
    return value if value > 0 else 60.0


# -- class 1 -------------------------------------------------------------------------------------------------------

def check_registry() -> str:
    """Once per tick. Returns the registry state; logs every tick and pages once while it is absent/unreadable."""
    state = registry_state()
    if state == "ok" and not (_root() / STATE_REL).exists():
        return state                      # steady state: no lock, no write
    with _state() as doc:
        if state != "ok":
            _log.warning("kanban door: priority registry %s (%s); running spawn-only", state, _registry_path())
            if not doc.get("registry_paged") and door_config_present():
                _page(f"kanban door: priority registry {state} ({_registry_path()}); running spawn-only")
                doc["registry_paged"] = _now()
        elif doc.pop("registry_paged", None) is not None:
            _log.warning("kanban door: priority registry is back; page cleared")
            _page("kanban door: priority registry is back; the spawn-only fallback page is cleared")
    return state


# -- class 2 -------------------------------------------------------------------------------------------------------

def latch(reason: str) -> None:
    """Latch the door after a config fault; page once per latch."""
    with _state() as doc:
        if doc.get("config"):
            return
        doc["config"] = {"reason": str(reason)[:300], "fingerprint": fingerprint(), "at": _now()}
        _log.error("kanban door: config fault, door LATCHED (no door calls) until token/table/registry change: %s",
                   reason)
        _page(f"kanban door config fault: {str(reason)[:200]}; every kanban start refused until the pair token, "
              "host table or priority registry changes")


def claim_block(task_id: str) -> bool:
    """True for the FIRST card that meets this latch: it is the one blocked kind=capability (the publish pass can
    latch before any start). Later cards are held without a block; the dispatcher stops claiming next tick."""
    with _state() as doc:
        cfg = doc.get("config")
        if not cfg or cfg.get("blocked"):
            return False
        cfg["blocked"] = str(task_id)
        return True


def latched():
    """The latch reason, or None. A changed token/table/registry clears the latch (the next start retries)."""
    with _state() as doc:
        cfg = doc.get("config")
        if not cfg:
            return None
        if cfg.get("fingerprint") != fingerprint():
            doc.pop("config", None)
            _log.warning("kanban door: config changed; latch cleared (%s)", cfg.get("reason"))
            return None
        return cfg.get("reason") or "config fault"


# -- class 3 -------------------------------------------------------------------------------------------------------

# Transport health is tracked PER VERB (round 3, t_0c5ef02e): ``authorize`` is the start boundary (enqueue,
# authorize_start and its lost-reply lookup), ``publish`` the waiting publisher. Only the verb's own answer clears
# its history: a healthy publish or lookup proves nothing about authorize_start (reader repro: authorize 503 for
# 732 s while publish/lookup answered gave twelve 60 s retries and no page). Any verb in backoff holds starts;
# one page per outage across verbs, one clear when the last verb recovers.
VERBS = ("authorize", "publish")


def _transport(doc) -> dict:
    t = doc.get("transport")
    return t if isinstance(t, dict) and all(isinstance(v, dict) for v in t.values()) else {}


def note_transport(err: str, verb: str = "authorize") -> None:
    if verb not in VERBS:
        raise ValueError(f"unknown door verb {verb!r}")
    now = _now()
    with _state() as doc:
        verbs = _transport(doc)
        prev = verbs.get(verb)
        interval = tick_interval() if not prev else min(TRANSPORT_MAX_BACKOFF_S, float(prev["interval"]) * 2)
        # `paged` is read from the EXISTING record before it is replaced (round 4): overwriting first dropped it, so
        # a single-verb outage re-paged on every retry past 10 min.
        t = {"since": now if not prev else prev["since"], "interval": interval, "next_at": now + interval,
             "error": str(err)[:300], "paged": bool(prev and prev.get("paged"))}
        verbs[verb] = t
        paged = any(v.get("paged") for v in verbs.values())
        if not paged and now - t["since"] > TRANSPORT_PAGE_AFTER_S:
            _page(f"kanban door: allocd {verb} unreachable for {int(now - t['since'])} s; kanban starts held: {err}")
            paged = True
        t["paged"] = paged
        doc["transport"] = verbs


def note_success(verb: str = "authorize") -> None:
    if verb not in VERBS:
        raise ValueError(f"unknown door verb {verb!r}")
    with _state() as doc:
        verbs = _transport(doc)
        t = verbs.pop(verb, None)
        if t is None:
            return
        if verbs:
            if t.get("paged"):
                for v in verbs.values():
                    v["paged"] = True             # the outage page is still open on the other verb
            doc["transport"] = verbs
            return
        doc.pop("transport", None)
        if t.get("paged"):
            _page("kanban door: allocd reachable again; kanban starts resumed")


def backoff_remaining():
    with _state() as doc:
        verbs = _transport(doc)
    if not verbs:
        return None
    left = max(float(v["next_at"]) for v in verbs.values()) - _now()
    return left if left > 0 else None


def hold_reason():
    """Why a door-mode tick must not start anything, or None."""
    reason = latched()
    if reason:
        return f"door latched: {reason}"
    left = backoff_remaining()
    if left:
        return f"door transport backoff {int(left)} s"
    return None
