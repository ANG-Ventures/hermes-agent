"""The fleet-wide kanban door latch is for config faults only (card t_b18b346e).

``kanban_priority_faults.latch()`` stops every kanban start on every host until ``fingerprint()`` moves, and the
fingerprint covers only door config: the pair/owner tokens, the host table, the priority registry and the root
generation/receipt. A refusal about ONE reservation (its owner record, a row already gone) is repaired by none of
those files, so if it reaches the latch the fleet stays latched until a human touches a file that was never broken.
That happened twice on 2026-11-17 (02:03:23, 02:13:33): a lost upgrade reply, then the door's cancel ``release``
was Refused on alloc_client's stale process-local owner, and the Refused became DoorFault('config') -> latch.

Two locks:

* a source contract over every ``faults.latch(`` site in hermes_cli. For each site it finds the region whose
  exceptions reach the latch (the ``try`` body of the handler that latches, or the ``try`` bodies around every call
  of a helper that latches), follows same-module and door-module calls from there, and requires every
  per-reservation verb (``.release(`` / ``.renew(``) it reaches to sit in a ``try`` whose ``Refused`` handler
  neither re-raises nor raises DoorFault. Only a credential error (``TokenUnreadable``: its repair rewrites a token
  file that ``fingerprint()`` covers) may propagate from such a call.
* the lost-reply path end to end with the REAL vendored alloc_client against a loopback allocd: the pool lease is
  booked as ``kanban`` in this process, the upgrade reply is lost, lookup answers ``authorized``, and the door's
  cancel release now reaches allocd as ``kanban@<host>`` and succeeds. Nothing is left to expire at its TTL and the
  door is not latched.

DOES NOT COVER: publish-path refusals of a unit verb (enqueue / withdraw / publish_demand typed auth or invalid)
still latch through ``kanban_priority._classify``; that is taxonomy class 2 as ruled (t_0c5ef02e), not a
reservation refusal. A reservation verb reached through a callee outside hermes_cli's door modules (another module's
function, a callable passed in) is not followed.

This fork already lands source contracts on kanban code (see test_kanban_placement_block.py); the invariant here is
"no future edit may route a reservation refusal into the latch", which no single behaviour test can enumerate.
"""
from __future__ import annotations

import ast
import time
import warnings
from pathlib import Path

import pytest

import hermes_cli
from hermes_cli.fleet_placement import alloc_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_priority_faults as faults
from tests.hermes_cli.test_kanban_door_dispatch import Allocd, fleet, ok_door  # noqa: F401  (fleet is a fixture)

PKG = Path(hermes_cli.__file__).resolve().parent
RESERVATION_VERBS = {"release", "renew"}
#: exceptions a reservation verb may let reach the latch: each one's repair changes a file fingerprint() covers
FINGERPRINTED_ERRORS = {"TokenUnreadable"}


# -- the source contract ---------------------------------------------------------------------------------------------

def _exc_names(node):
    """Bare class names an ``except`` clause catches (``client.Refused`` -> ``Refused``)."""
    if node is None:
        return {"<bare>"}
    elts = node.elts if isinstance(node, ast.Tuple) else [node]
    out = set()
    for e in elts:
        if isinstance(e, ast.Attribute):
            out.add(e.attr)
        elif isinstance(e, ast.Name):
            out.add(e.id)
    return out


class _Module:
    def __init__(self, name, src):
        self.name = name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)      # other modules' escape sequences are not this test's
            self.tree = ast.parse(src)
        self.parent = {}
        for p in ast.walk(self.tree):
            for c in ast.iter_child_nodes(p):
                self.parent[c] = p
        self.defs = {n.name: n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.aliases = {}                       # local name -> hermes_cli module name
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ImportFrom) and n.module == "hermes_cli":
                for a in n.names:
                    self.aliases[a.asname or a.name] = a.name

    def enclosing_def(self, node):
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def ancestors(self, node):
        while node in self.parent:
            child, node = node, self.parent[node]
            yield child, node


def _is_latch(call):
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "latch"
            and isinstance(call.func.value, ast.Name))


def _load(sources):
    return {name: _Module(name, src) for name, src in sources.items()}


def _real_sources():
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(PKG.glob("*.py"))}


def _latch_regions(mods):
    """[(site label, module, [statements whose exceptions reach the latch])] for every latch call in hermes_cli."""
    out = []
    for m in mods.values():
        for call in (n for n in ast.walk(m.tree) if _is_latch(n)):
            if m.aliases.get(call.func.value.id, call.func.value.id) not in ("kanban_priority_faults", "faults"):
                continue
            fn = m.enclosing_def(call)
            label = f"{m.name}.{fn.name if fn else '<module>'}:{call.lineno}"
            out.append((label, m, _regions_for(m, call, set())))
    return out


def _regions_for(m, node, seen):
    """The try bodies whose exceptions flow into ``node``: the nearest enclosing handler's try in the same function;
    when there is none, the function is a latch helper and the regions are those around each of its call sites."""
    fn = m.enclosing_def(node)
    for child, anc in m.ancestors(node):
        if anc is fn:
            break
        if isinstance(anc, ast.ExceptHandler):
            return list(m.parent[anc].body)
    if fn is None or fn.name in seen:
        return []
    seen.add(fn.name)
    regions = []
    for call in ast.walk(m.tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == fn.name:
            regions.extend(_regions_for(m, call, seen))
    return regions


def _resolve(m, mods, call):
    f = call.func
    if isinstance(f, ast.Name) and f.id in m.defs:
        return m, m.defs[f.id]
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        target = mods.get(m.aliases.get(f.value.id, ""))
        if target is not None and f.attr in target.defs:
            return target, target.defs[f.attr]
    return None


def _guarded(m, call):
    """True when the reservation verb sits in a try whose Refused handler keeps the refusal away from the latch."""
    for child, anc in m.ancestors(call):
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if not (isinstance(anc, ast.Try) and child in anc.body):
            continue
        for h in anc.handlers:
            names = _exc_names(h.type)
            raises = [r for r in ast.walk(h) if isinstance(r, ast.Raise)]
            if names <= FINGERPRINTED_ERRORS:
                continue                            # e.g. TokenUnreadable: may propagate, its repair moves the fingerprint
            if "Refused" in names or "<bare>" in names or "BaseException" in names or "Exception" in names:
                bad = [r for r in raises if r.exc is None or (
                    isinstance(r.exc, ast.Call) and getattr(r.exc.func, "id", getattr(r.exc.func, "attr", "")) == "DoorFault")]
                return not bad
    return False


def offenders(sources):
    """Every reservation verb reachable from a latch region without a Refused guard, as ``site -> module.fn:line``."""
    mods = _load(sources)
    regions = _latch_regions(mods)
    found = []
    for label, m, stmts in regions:
        stack = [(m, s) for s in stmts]
        visited = set()
        while stack:
            mod, node = stack.pop()
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                if isinstance(call.func, ast.Attribute) and call.func.attr in RESERVATION_VERBS:
                    if not _guarded(mod, call):
                        fn = mod.enclosing_def(call)
                        found.append(f"{label} <- {mod.name}.{fn.name if fn else '<module>'}:{call.lineno} "
                                     f".{call.func.attr}()")
                    continue
                hit = _resolve(mod, mods, call)
                if hit and (hit[0].name, hit[1].name) not in visited:
                    visited.add((hit[0].name, hit[1].name))
                    stack.append(hit)
    return regions, sorted(set(found))


def test_every_latch_site_is_found_and_no_reservation_refusal_reaches_it():
    regions, bad = offenders(_real_sources())
    sites = {label.split(":")[0] for label, _, _ in regions}
    # positive discovery: the door start path and both publish-pass sites (a vacuous walk finds none)
    assert {"kanban_priority_door.start", "kanban_priority._classify", "kanban_priority._publish_waiting"} <= sites, sites
    assert all(stmts for _, _, stmts in regions), [label for label, _, s in regions if not s]
    assert bad == []


def test_the_contract_names_a_reservation_release_that_would_latch():
    """The pre-#400 door: the lost-reply path called client.release directly, so its Refused reached
    start()'s `except client.Refused -> DoorFault('config')` and latched the fleet."""
    src = _real_sources()
    door_src = src["kanban_priority_door"]
    current = '_cancel_unreceipted(client, seen["reservation_id"], hid, "lost authorization reply")'
    assert current in door_src
    src["kanban_priority_door"] = door_src.replace(
        current, 'client.release(seen["reservation_id"], consumer=f"kanban@{hid}")')
    _, bad = offenders(src)
    assert any("kanban_priority_door.start" in b and "kanban_priority_door._authorize" in b and ".release()" in b
               for b in bad), bad


def test_a_refused_handler_that_reraises_is_not_a_guard():
    src = _real_sources()
    door_src = src["kanban_priority_door"]
    current = ('        raise DoorCancelled(f"{why}: release of {rid} refused ({exc}); lease ends at its TTL, "\n'
               '                            "retry with new ID") from None\n')
    assert current in door_src
    src["kanban_priority_door"] = door_src.replace(current, "        raise\n")
    _, bad = offenders(src)
    assert any("kanban_priority_door._cancel_unreceipted" in b for b in bad), bad


# -- the lost-reply path, real vendored client ----------------------------------------------------------------------

HOST = "mac-studio"
RID = "pool-row-1"


def _claim():
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="lost-reply", assignee=None)
        return tid, kb.claim_task(conn, tid)


def test_lost_upgrade_reply_cancel_release_succeeds_as_the_pair_and_nothing_waits_for_ttl(fleet, monkeypatch):
    fleet.registry("fcfs")
    fleet.table()
    fleet.token()
    allocd = Allocd()
    ok = ok_door()
    owner = {RID: "kanban"}                                   # allocd's row owner

    def answer(path, body):
        if path == "/admit":
            return 200, {"host": HOST, "reservation_id": RID, "rule": "test"}
        if path == "/demand/authorize":
            assert body.get("existing_reservation_id") == RID
            owner[RID] = f"kanban@{HOST}"                     # allocd retags the row, then the reply is lost
            return 200, {"schema": 1, "workload": "kanban", "host": HOST, "id": body["id"],
                         "reservation_id": RID, "until": time.time() + 90, "reused": False}
        if path == "/demand/lookup":
            return 200, {"schema": 1, "workload": "kanban", "host": HOST, "id": body["id"],
                         "state": "authorized", "reservation_id": RID, "until": time.time() + 90}
        if path == "/release":
            if owner.get(body["reservation_id"]) != body["consumer"]:
                return 403, {"error": "owner mismatch"}
            owner.pop(body["reservation_id"])
            return 200, {"released": True}
        return ok(path, body)

    allocd.answer = answer
    monkeypatch.setenv("ALLOCD_URL", allocd.url)
    monkeypatch.setattr(alloc_client, "_OWNER", {})
    # the gateway's kanban_headroom books the pool lease as the base owner in this same process
    booked = alloc_client.admit("kanban", job_id="pool", ttl_s=90, fallback=False)
    assert booked.reservation_id == RID
    real_call = alloc_client._call

    def lose_authorize_reply(path, *a, **kw):
        result = real_call(path, *a, **kw)
        if path == "/demand/authorize":
            raise alloc_client.TimedOut("TimeoutError: POST /demand/authorize")
        return result

    monkeypatch.setattr(alloc_client, "_call", lose_authorize_reply)
    tid, task = _claim()
    with kbc.connect_closing() as conn:
        with pytest.raises(door.DoorCancelled) as cancelled:
            with door.start(conn, task, HOST, reservation_id=RID, client=alloc_client):
                pytest.fail("an unreceipted lease must never spawn")
    assert not isinstance(cancelled.value, door.DoorFault)
    assert "TTL" not in str(cancelled.value), cancelled.value          # the lease was released, not stranded
    releases = [b for p, b in allocd.calls if p == "/release"]
    assert releases == [{"reservation_id": RID, "consumer": f"kanban@{HOST}"}]
    assert RID not in owner                                             # allocd holds nothing for this start
    assert faults.latched() is None
    assert fleet.pages == []
