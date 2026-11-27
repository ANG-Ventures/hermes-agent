"""FWA hold records in the dispatcher (FWA pause/resume SPEC §5.1-§5.3, §9.1 AC1/AC4/AC8; card B3 t_0b56b707).

The fork re-implements hermes-home ``fwa.valid`` + ``fwa.accepting``; parity is the golden fixture set under
``tests/fixtures/fwa/`` (byte copies of hermes-home's), pinned by ``kanban_pause_hold.FWA_FIXTURES_SHA``.
Dispatcher arms reuse the hermetic board of ``test_kanban_pause_hold`` (real non-dry ``dispatch_once``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pause_hold as kph

from tests.hermes_cli.test_kanban_pause_hold import (  # noqa: F401  (fixtures + helpers of the base suite)
    MID, PLANS, _cards, _manifest, _no_real_spawn, _plan, _routes, _runs, _set, _tick, home,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fwa"


@pytest.fixture(autouse=True)
def _neutral_local_host(monkeypatch):
    """The local destination fence reads socket.gethostname(): pin it to a name no fixture holds, so the suite does
    not depend on which runner it lands on (a runner named ``ci`` is a ci-box alias in these fixtures)."""
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "fwa-test-runner.lan")
    monkeypatch.setattr(socket, "getfqdn", lambda name="": name)     # hermetic: no resolver lookup
    monkeypatch.setattr(kph, "_FQDN_MEMO", {})


@pytest.fixture(autouse=True)
def _follows_origin(monkeypatch):
    """This suite pins the origin-keyed fwa hold (its cards are unpinned ``do the thing`` bodies). Since t_72362742
    that is the ``KANBAN_PAUSE_FOLLOWS_ORIGIN=1`` kill switch, so the suite doubles as its dormant==identical
    proof; the pin-scoped default is covered by test_kanban_pause_hold_pinned.py."""
    monkeypatch.setenv(kph.FOLLOWS_ORIGIN_ENV, "1")


HID = "20261107T150000Z-ci-box-fwa00001"


def _fixtures_sha(d: Path) -> str:
    """SPEC §5.1 RC-5: sha256 over the sorted ``(name, bytes)`` of ``*.json`` (name, NUL, bytes, NUL per file)."""
    h = hashlib.sha256()
    for p in sorted(d.glob("*.json"), key=lambda p: p.name):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _hold(home, host="ci-box", *, lanes=("kanban", "ci", "prism"), since=None, until=None, hid=HID, raw=None,
          **extra):
    d = home / "state" / "fwa" / "holds" / host
    d.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    doc = {"schema": 1, "id": hid, "host": host, "kind": "pause", "lanes": list(lanes), "owner": "card:t_x",
           "drive": True, "by": "apollo", "card": "t_x", "reason": "fixture", "since": now - 60 if since is None
           else since, "until": now + 3600 if until is None else until}
    doc["end"] = {"kind": "time", "until": doc["until"]}      # §5.4a: every hold carries an end
    doc.update(extra)
    if doc.get("end") is None:
        doc.pop("end", None)
    p = d / f"{hid}.json"
    p.write_text(raw if raw is not None else json.dumps(doc), encoding="utf-8")
    return p


# -- AC8: fixture contract ---------------------------------------------------------------------------------------

def test_fixture_dir_hashes_to_the_published_constant():
    assert FIXTURES.is_dir() and list(FIXTURES.glob("*.json"))
    assert _fixtures_sha(FIXTURES) == kph.FWA_FIXTURES_SHA


def test_constant_is_readable_as_text_by_the_home_sweep():
    """§5.4 step 5 reads the constant out of this file with one regex, never by import."""
    import re
    text = Path(kph.__file__).read_text(encoding="utf-8-sig")
    m = re.findall(r'^FWA_FIXTURES_SHA = "([0-9a-f]{64})"$', text, re.M)
    assert m == [kph.FWA_FIXTURES_SHA]


CASES = sorted(p.name for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", CASES)
def test_fork_predicate_matches_every_golden_fixture(home, name):
    case = json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))
    host, now = case["host"], float(case["now"])
    d = kph.fwa_root() / host
    d.mkdir(parents=True, exist_ok=True)
    for h in case["holds"]:
        if "file" in h:                      # a file as bytes on disk: {file, raw, age_s} (unreadable / invalid)
            p = d / h["file"]
            p.write_text(h["raw"], encoding="utf-8")
        else:                                # a hold record, written as <id>.json
            p = d / f"{h['id']}.json"
            p.write_text(json.dumps(h), encoding="utf-8")
        mt = now - float(h.get("age_s", 0))   # mtime is case data: the unreadable bound is mtime + 24 h
        os.utime(p, (mt, mt))
    entries = kph.fwa_entries(host, {(kph._K_REGISTRY, str(kph.registry_path())): {}})
    for lane, want in case["expect"].items():
        got = kph.fwa_verdict(entries, lane, now)
        if want is True:
            assert got is None, (name, lane, got)
        elif want is False:
            assert got is not None and got[0] != kph.UNREADABLE, (name, lane, got)
        else:
            assert want == "unreadable" and got is not None and got[0] == kph.UNREADABLE, (name, lane, got)


# -- AC1 dispatcher cell: an fwa hold alone holds both layers ------------------------------------------------------

@pytest.mark.parametrize("mkplan", PLANS)
def test_fwa_hold_with_no_manifest_holds_every_card_of_the_host(home, mkplan):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        res, spawned = _tick(conn, mkplan())
        routes = _routes(res)
        got = {t for t, _h in spawned}
        for key in "ABDF":
            r = routes[ids[key]]
            assert r["outcome"] == "held" and r["code"] == "pause_hold", (key, r)
            assert r["detail"].startswith(f"ci-box:fwa:pause:{HID} by apollo until "), r["detail"]
            assert ids[key] not in got and _runs(conn, ids[key]) == 0
        assert ids["C"] in got                                    # another host's card is untouched
        assert res.placement_waits[ids["E"]].startswith(f"pause_hold (ci-box:fwa:pause:{HID}")
        assert ids["E"] not in got and _runs(conn, ids["E"]) == 0


def test_layer2_refuses_a_card_held_only_by_fwa(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        with pytest.raises(kph.PauseHeld) as ei:
            kb.claim_task(conn, ids["A"])
        assert ei.value.host == "ci-box" and HID in ei.value.manifest_id
        assert _runs(conn, ids["A"]) == 0


@pytest.mark.parametrize("lanes", [("ci",), ("reboot", "deploy"), ("prism", "ci-failover")])
def test_hold_without_lane_kanban_admits(home, lanes):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, lanes=lanes)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_expired_hold_never_holds(home):
    """I3: ``now >= until`` = accepting, even before the sweep archives the file."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        now = int(time.time())
        _hold(home, since=now - 7200, until=now - 1)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


@pytest.mark.parametrize("raw", ["{not json", "", '{"schema": 1, "id": "x"'], ids=["junk", "empty", "truncated"])
def test_unparseable_hold_file_holds_fail_closed(home, raw):
    """§5.4a (operator round 2): JSON that does not parse is a write in progress or disk damage, not a missing
    end, so it stays fail-closed until mtime + 24 h."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, raw=raw)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["detail"].startswith("ci-box:fwa:unreadable:"), r
        assert ids["A"] not in {t for t, _ in spawned}


def test_unreadable_hold_file_stops_holding_24h_after_its_mtime(home):
    """§5.2: a fail-closed file has an end too."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        p = _hold(home, raw="{not json")
        old = time.time() - kph.FWA_UNREADABLE_MAX_S - 5
        os.utime(p, (old, old))
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


_BAD_ENDS = {
    "absent": None,
    "not-a-dict": "2h",
    "unknown-kind": {"kind": "forever"},
    "until-mismatch": {"kind": "time", "until": 1},
    "grace-absent": {"kind": "reboot", "boot_id_at_pause": "b", "backstop_until": "NOW+1h"},
    "for-past-24h": "OVER",
    "reboot-no-backstop": {"kind": "reboot", "boot_id_at_pause": "b", "grace_s": 600},
    "reboot-grace-over-2h": {"kind": "reboot", "boot_id_at_pause": "b", "grace_s": 7201, "backstop_until": "NOW+1h"},
    "reboot-no-boot-id": {"kind": "reboot", "grace_s": 600, "backstop_until": "NOW+1h"},
}


@pytest.mark.parametrize("case", sorted(_BAD_ENDS))
def test_missing_or_malformed_end_is_expired_with_one_log_line(home, case, caplog):
    """§5.4a + operator round 2: a PARSEABLE hold whose ``end`` is absent or malformed is EXPIRED (does not hold),
    never fail-closed: "so things aren't just paused forever". One log line names the file."""
    now = int(time.time())
    end = _BAD_ENDS[case]
    if end == "OVER":
        p = _hold(home, since=now - 60, until=now - 60 + kph.FWA_MAX_HOLD_S + 1)
    elif isinstance(end, dict) and end.get("backstop_until") == "NOW+1h":
        p = _hold(home, end={**end, "backstop_until": now + 3600}, until=now + 3600)
    else:
        p = _hold(home, end=end)
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        caplog.set_level("WARNING", logger=kph.__name__)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}
    lines = [r.getMessage() for r in caplog.records if p.name in r.getMessage()]
    assert len(lines) == 1 and "expired" in lines[0], lines


def test_verdict_alone_treats_an_endless_doc_as_expired():
    """fwa_verdict is §5.4a-correct on its own, not only behind the loader: a dict with no end never holds."""
    now = 1794070000
    doc = {"schema": 1, "id": "x", "host": "ci-box", "lanes": ["kanban"], "since": now - 60, "until": now + 60}
    assert kph.fwa_verdict([doc], "kanban", now) is None
    doc["end"] = {"kind": "time", "until": now + 60}
    assert kph.fwa_verdict([doc], "kanban", now)[1] == "x"


def _reboot_hold(home, backstop, **end_extra):
    end = {"kind": "reboot", "boot_id_at_pause": "5f0c3b8e-1d2a-4c6e-9b7f-0a1b2c3d4e5f", "grace_s": 600,
           "backstop_until": backstop, **end_extra}
    return _hold(home, kind="maintenance", until=backstop, end=end)


def test_reboot_hold_is_live_until_its_backstop(home):
    """§5.4a: the fork cannot read the host's boot id per tick, so a reboot hold holds until backstop_until unless
    the home sweep archived it (the sweep owns the reboot check)."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _reboot_hold(home, now + 12 * 3600)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["detail"].startswith(f"ci-box:fwa:maintenance:{HID} by apollo until "), r
        assert ids["A"] not in {t for t, _ in spawned}


def test_reboot_hold_past_its_backstop_admits(home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _reboot_hold(home, now - 1)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_reboot_hold_released_by_sweep_stamps_admits(home):
    """Home ``fwa.effective_end``: the sweep stamped rebooted_at and seen_up_at >= rebooted_at + grace_s."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _reboot_hold(home, now + 12 * 3600, rebooted_at=now - 900, seen_up_at=now - 200)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_reboot_hold_inside_grace_still_holds(home):
    """Rebooted, but not yet seen up for grace_s: still held (the host is not back)."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _reboot_hold(home, now + 12 * 3600, rebooted_at=now - 300, seen_up_at=now - 10)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}


def test_reboot_hold_dark_after_reboot_keeps_its_backstop(home):
    """AC-END (ii): rebooted long ago but last seen up before grace elapsed (ssh dark since) -> the backstop governs."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _reboot_hold(home, now + 12 * 3600, rebooted_at=now - 900, seen_up_at=now - 800)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}


@pytest.mark.parametrize("doc", [{"schema": 1, "id": "x"}, {"schema": 2, "id": "x", "host": "ci-box", "lanes": ["kanban"]},
                                 {"schema": 1, "id": "x", "host": "ci-box", "lanes": []}, ["not", "an", "object"]],
                         ids=["no-host-lanes", "schema-2", "empty-lanes", "list"])
def test_structurally_invalid_hold_file_holds_fail_closed(home, doc):
    """Home ``fwa.valid`` != None (parseable but not a record) = unreadable: fail closed until mtime + 24 h. Only a
    structurally valid record with a bad END is expired."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, raw=json.dumps(doc))
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["detail"].startswith("ci-box:fwa:unreadable:"), r
        assert ids["A"] not in {t for t, _ in spawned}


def test_reboot_hold_archived_by_the_sweep_releases(home):
    """The sweep archives a released reboot hold out of holds/<host>/; the next tick admits."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        p = _reboot_hold(home, now + 12 * 3600)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}
        arch = home / "state" / "fwa" / "archive" / "ci-box"
        arch.mkdir(parents=True)
        p.rename(arch / p.name)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_lock_and_dot_files_are_not_holds(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        d = home / "state" / "fwa" / "holds" / "ci-box"
        d.mkdir(parents=True)
        (d / ".lock").write_text("", encoding="utf-8")
        (d / ".tmp.json").write_text("{not json", encoding="utf-8")
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_profile_home_fwa_hold_is_never_read(home):
    """§5.1 root rule: a hold under a PROFILE home must not hold (nor be the place the dispatcher looks)."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        prof = home / "profiles" / "alpha"
        _hold(prof)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_alias_directory_holds_the_canonical_host(home):
    """§5.2: ``ci``, ``ci.ace``, ``ci-box`` name one host."""
    _hold(home, host="ci")
    _registry(home, {"ci-box": {"aliases": ["ci", "ci.ace"]}})
    v = kph.decide("ci-box", "t_any", {})
    assert v is not None and v[0] == "wait" and HID in v[1]
    assert kph.decide("ace-ai", "t_any", {}) is None


# -- AC4: never cached across ticks ---------------------------------------------------------------------------------

def test_removing_the_hold_file_releases_on_the_next_tick(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        p = _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}
        p.unlink()
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


# -- dual-read (§5.3, §8 P1) and the kill switch (§11) --------------------------------------------------------------

def test_legacy_manifest_still_holds_with_no_fwa_hold(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        res, _spawned = _tick(conn, _plan())
        assert _routes(res)[ids["A"]]["detail"] == f"ci-box:{MID}"


def test_fwa_hold_beats_a_resume_pin(home):
    """A held host takes no run, pinned or not: a resumed manifest's owed pin does not route a card onto it."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": None}}])
        _hold(home)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and "fwa:" in r["detail"]
        assert ids["A"] not in {t for t, _ in spawned}


def _source(monkeypatch, value):
    monkeypatch.setattr(kb, "_board_home_kanban_cfg", lambda: {"placement": {"pause_hold_source": value}})


def test_kill_switch_manifest_ignores_fwa_holds(home, monkeypatch):
    _source(monkeypatch, "manifest")
    assert kph.source() == kph.SOURCE_MANIFEST
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}
        assert kph.holds(conn)["fwa"] == {}


@pytest.mark.parametrize("value,want", [("fwa", "fwa"), ("FWA", "fwa"), ("bogus", "fwa"), (None, "fwa"),
                                        ("manifest", "manifest"), (" Manifest ", "manifest")])
def test_source_parses_fail_closed(monkeypatch, value, want):
    _source(monkeypatch, value)
    assert kph.source() == want


# -- resume pin lapse (§5.3: resumed_at + 2 h, stamped or not) -------------------------------------------------------

def _resumed(home, tid, resumed_at):
    d = home / "state" / "fleet-pause" / "ci-box"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema": 2, "id": MID, "host": "ci-box", "phase": "resumed", "resumed_at": resumed_at,
        "units": [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}]}), encoding="utf-8")


def test_resume_pin_lapses_two_hours_after_resumed_at(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - kph.PIN_LAPSE_S - 5)
        assert kph.decide("ci-box", ids["A"], {}) is None
        res, _spawned = _tick(conn, _plan())
        assert _routes(res)[ids["A"]]["route"] != "remote-pin"


def test_resume_pin_holds_inside_the_lapse_window(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - 60)
        assert kph.decide("ci-box", ids["A"], {}) == ("pin", "ci-box")


def test_resume_pin_lapse_is_off_under_the_kill_switch(home, monkeypatch):
    _source(monkeypatch, "manifest")
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - kph.PIN_LAPSE_S - 5)
        assert kph.decide("ci-box", ids["A"], {}) == ("pin", "ci-box")


# -- read model ------------------------------------------------------------------------------------------------------

def test_placement_holds_lists_fwa_holds(home):
    _hold(home)
    _hold(home, hid="20261107T150000Z-ci-box-fwa00002", lanes=("ci",))
    rep = kph.holds()
    rows = {r["id"]: r for r in rep["fwa"]["ci-box"]}
    assert rows[HID]["holds_kanban"] is True
    assert rows["20261107T150000Z-ci-box-fwa00002"]["holds_kanban"] is False
    assert rep["source"] == "fwa"


# -- round 3 (operator 11-08 05:50; Prism d6a2cb37de48 / abd3aba4b5be / 616898796fc2) --------------------------------

def _registry(root, hosts):
    """A ``config/fleet-hosts.json`` under ``root`` (the kanban home, or a decoy)."""
    p = root / "config" / "fleet-hosts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hosts": hosts}), encoding="utf-8")
    return p


@pytest.fixture
def decoy_placement(tmp_path, monkeypatch):
    """placement.fleet_root() (PLACEMENT_HOME / the checkout) carries a DIFFERENT registry with no aliases: the hold
    reader must resolve aliases from the kanban home's registry, never from the placement root (abd3aba4b5be)."""
    d = tmp_path / "placement-root"
    _registry(d, {"ci-box": {"aliases": []}})
    monkeypatch.setenv("ALLOCATION_HOME", str(d))
    return d


@pytest.mark.parametrize("order", [("table", "ci-box"), ("ci-box", "table")], ids=["table-first", "ci-box-first"])
def test_host_named_table_shares_no_cache_slot_with_the_alias_registry(home, order):
    """d6a2cb37de48: a host literally named ``table`` must not collide with the alias-registry memo in either order."""
    _registry(home, {"ci-box": {"aliases": ["ci"]}, "table": {"aliases": []}})
    _hold(home)
    cache: dict = {}
    got = {h: kph.decide(h, "t_any", cache) for h in order}
    assert got["table"] is None, got
    assert got["ci-box"] is not None and got["ci-box"][1].startswith(f"pause_hold (ci-box:fwa:pause:{HID} "), got


def test_dispatcher_tick_with_a_host_named_table_holds_only_the_held_host(home):
    """d6a2cb37de48 through the real tick: one shared per-tick cache for every row of both lanes."""
    _registry(home, {"ci-box": {"aliases": ["ci"]}, "table": {"aliases": []}})
    with kb.connect_closing() as conn:
        g = kb.create_task(conn, title="card G", assignee="alpha", body="do the thing")   # routed FIRST
        _set(conn, g, status="ready", origin_host="table", priority=500)
        ids = _cards(conn)
        _hold(home)
        res, _spawned = _tick(conn, _plan())
        routes = _routes(res)
        assert routes[ids["A"]]["outcome"] == "held", routes[ids["A"]]
        assert routes[ids["A"]]["detail"].startswith(f"ci-box:fwa:pause:{HID} "), routes[ids["A"]]
        assert routes.get(g, {}).get("code") != "pause_hold", routes.get(g)
        assert g not in res.placement_waits or not str(res.placement_waits[g]).startswith("pause_hold"), res
        kb.claim_task(conn, g)                               # layer 2: the direct claim of the unheld card opens
        assert _runs(conn, g) == 1


@pytest.mark.parametrize("origin", ["ci", "CI.ACE", "cibox"])
def test_alias_origin_is_held_via_the_kanban_home_registry_both_layers(home, decoy_placement, origin):
    """abd3aba4b5be: the alias table is the OWNING kanban home's ``config/fleet-hosts.json`` (aliases + ssh_alias,
    as home ``fwa.resolve``), not placement's checkout table. Dispatcher tick AND direct claim refuse."""
    _registry(home, {"ci-box": {"aliases": ["ci", "ci.ace"], "net": {"ssh_alias": "cibox"}}})
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _set(conn, ids["A"], origin_host=origin)
        _hold(home)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["detail"].startswith(f"{origin}:fwa:pause:{HID} "), r
        assert ids["A"] not in {t for t, _ in spawned}
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, ids["A"])
        assert _runs(conn, ids["A"]) == 0


def test_hold_under_an_alias_dir_holds_the_canonical_origin_via_the_kanban_home_registry(home, decoy_placement):
    _registry(home, {"ci-box": {"aliases": ["ci"]}})
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, host="ci")
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, ids["A"])


def test_profile_home_registry_is_never_the_alias_table(home, decoy_placement):
    """§5.1 root rule for the registry too: aliases declared only under the PROFILE home do not bind."""
    _registry(home / "profiles" / "alpha", {"ci-box": {"aliases": ["ci"]}})
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _set(conn, ids["A"], origin_host="ci")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


_ROOT_LEVELS = {
    "fwa-dir-no-access": (("state", "fwa"), 0o000),
    "holds-dir-no-search": (("state", "fwa", "holds"), 0o600),
    "holds-dir-no-access": (("state", "fwa", "holds"), 0o000),
    "host-dir-no-access": (("state", "fwa", "holds", "ci-box"), 0o000),
}


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root bypasses mode bits")
@pytest.mark.parametrize("level", sorted(_ROOT_LEVELS))
def test_inaccessible_hold_tree_fails_closed_both_layers(home, level):
    """616898796fc2: an ABSENT hold root is no hold; a root (or host dir) that exists but cannot be stat'ed/listed is
    an IO/access failure and fails closed (unreadable). Path.is_dir() reads EACCES as "absent"; never use it here."""
    rel, mode = _ROOT_LEVELS[level]
    _hold(home)
    target = home.joinpath(*rel)
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        os.chmod(target, mode)
        try:
            res, spawned = _tick(conn, _plan())
            r = _routes(res)[ids["A"]]
            assert r["outcome"] == "held" and r["detail"].startswith("ci-box:fwa:unreadable:"), (level, r)
            assert ids["A"] not in {t for t, _ in spawned}
            with pytest.raises(kph.PauseHeld):
                kb.claim_task(conn, ids["A"])
            assert _runs(conn, ids["A"]) == 0
        finally:
            os.chmod(target, 0o755)


def test_absent_hold_root_is_no_hold(home):
    """The other half of 616898796fc2: no ``state/fwa`` at all = accepting (both layers)."""
    assert not (home / "state" / "fwa").exists()
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        assert kph.decide("ci-box", ids["A"], {}) is None
        kb.claim_task(conn, ids["A"])
        assert _runs(conn, ids["A"]) == 1


# -- round 3b (Prism round 1 @ c9450a3c: 1de03430bf61 / 71b4b90b5c73) -------------------------------------------------

@pytest.mark.parametrize("aliases", [7, True, "ci", {"a": 1}, [1, None, "ci"]], ids=["int", "bool", "str", "dict", "mixed"])
def test_malformed_registry_aliases_never_hold_the_fleet(home, aliases):
    """1de03430bf61: a bad optional ``aliases`` on an UNRELATED row resolves no aliases; it must not turn decide()
    into a fail-closed hold for every origin (dispatcher tick and direct claim)."""
    _registry(home, {"unrelated": {"aliases": aliases}, "ci-box": {"aliases": ["ci"]}})
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _res, spawned = _tick(conn, _plan())
        assert {ids["A"], ids["C"]} <= {t for t, _ in spawned}
        assert kph.decide("ace-ai", "t_any", {}) is None


def _unorigined(conn, body):
    tid = kb.create_task(conn, title="no origin", assignee="alpha", body=body)
    _set(conn, tid, status="ready", origin_host=None)
    return tid


def test_held_destination_refuses_a_body_pin_with_no_origin(home):
    """71b4b90b5c73: origin_host NULL + ``host: ci-box`` in the body: the fwa hold on ci-box still refuses the start,
    with the fwa reason in placement_wait, and no run is opened."""
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "host: ci-box\ndo the thing")
        _hold(home)
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        why = res.placement_waits.get(tid, "")
        assert why.startswith(f"pause_hold (ci-box:fwa:pause:{HID} ") and "stage=destination" in why, why
        assert _runs(conn, tid) == 0


def test_held_destination_is_skipped_by_a_portable_pick(home):
    """A portable card with no origin never lands on the held host; it places elsewhere in the pool."""
    with kb.connect_closing() as conn:
        tids = [_unorigined(conn, "do the thing") for _ in range(6)]
        _hold(home)
        plan = _plan()
        plan.default_remote = True                           # remote-first: the pool picks among ci-box / ace-ai
        _res, spawned = _tick(conn, plan)
        hosts = {h for t, h in spawned if t in tids}
        assert "ci-box" not in hosts, spawned
        assert "ace-ai" in hosts, spawned


def test_destination_fence_is_off_under_the_kill_switch(home, monkeypatch):
    """Dormant == identical: ``pause_hold_source: manifest`` ignores fwa holds on the destination too."""
    _source(monkeypatch, "manifest")
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "host: ci-box\ndo the thing")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert (tid, "ci-box") in spawned, spawned


def test_unheld_destination_pin_spawns(home):
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "host: ci-box\ndo the thing")
        _hold(home, host="ace-ai")
        _res, spawned = _tick(conn, _plan())
        assert (tid, "ci-box") in spawned, spawned


# -- round 3c (Prism round 2 @ 5ed8f821: fe6a1ad69c44) -----------------------------------------------------------------

def _local_is(monkeypatch, name):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: f"{name}.lan")


@pytest.mark.parametrize("mkplan", PLANS)
def test_held_local_host_refuses_a_local_start(home, monkeypatch, mkplan):
    """fe6a1ad69c44: the dispatcher runs on ci-box, which carries a live fwa kanban hold; a portable card with no
    origin must not start locally (local route, or last-resort fallback once the pool refuses)."""
    _local_is(monkeypatch, "ci-box")
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "do the thing")
        _hold(home)
        res, spawned = _tick(conn, mkplan())
        assert tid not in {t for t, _ in spawned}, spawned
        why = res.placement_waits.get(tid, "")
        assert why.startswith(f"pause_hold (ci-box:fwa:pause:{HID} ") and "stage=destination" in why, why
        assert _runs(conn, tid) == 0


def test_held_local_host_by_registry_alias_refuses_a_local_start(home, monkeypatch):
    _registry(home, {"ci-box": {"aliases": ["ci"]}})
    _local_is(monkeypatch, "ci")
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "do the thing")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}, spawned


def test_held_local_host_refuses_a_review_start(home, monkeypatch):
    _local_is(monkeypatch, "ci-box")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="review", assignee="alpha", body="do the thing")
        _set(conn, tid, status="review", origin_host=None)
        _hold(home)
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}, spawned
        assert "stage=destination" in res.placement_waits.get(tid, ""), res.placement_waits.get(tid)


def test_unheld_local_host_starts_locally(home, monkeypatch):
    _local_is(monkeypatch, "ace-media")
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "do the thing")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert tid in {t for t, _ in spawned}, spawned


def test_local_fence_is_off_under_the_kill_switch(home, monkeypatch):
    _source(monkeypatch, "manifest")
    _local_is(monkeypatch, "ci-box")
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "do the thing")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert tid in {t for t, _ in spawned}, spawned


# -- round 5 (Prism round 3 @ 18481866: b256ef26919e / 34935fb58782 / 9fb3e16068f3) ---------------------------------

def _deleg_board():
    from hermes_cli import kanban_delegations_board as kdb
    kb.create_board(kdb.DELEGATIONS_BOARD)
    return kdb.DELEGATIONS_BOARD


def test_spawn_now_refuses_a_local_start_on_a_held_host(home, monkeypatch):
    """b256ef26919e: the immediate delegation path (spawn_now) starts locally; an origin-less unit must not open a run
    while the dispatcher's host carries a live fwa kanban hold."""
    from hermes_cli import kanban_db_dispatch as kbd
    board = _deleg_board()
    _local_is(monkeypatch, "ci-box")
    _hold(home)
    calls = []
    with kb.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title="unit", assignee="alpha")
        with pytest.raises(kbd.SpawnNowError, match=r"pause_hold \(ci-box:fwa:.*stage=destination\)"):
            kbd.spawn_now(conn, tid, board=board, spawn_fn=lambda *a, **k: calls.append(a) or 4242)
        task = kb.get_task(conn, tid)
        assert calls == [] and task.status == "ready" and task.current_run_id is None


def test_spawn_now_starts_on_an_unheld_host_and_under_the_kill_switch(home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    board = _deleg_board()
    _local_is(monkeypatch, "ci-box")
    _hold(home)
    _source(monkeypatch, "manifest")                    # dormant == identical
    with kb.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title="unit", assignee="alpha")
        assert kbd.spawn_now(conn, tid, board=board, spawn_fn=lambda *a, **k: 4242) == 4242


def test_held_local_review_reserves_no_capacity_ahead_of_a_runnable_ready_card(home, monkeypatch):
    """34935fb58782: an origin-less review on a held local host cannot start, so it must not take the one spawn slot
    ahead of a lower-priority ready card pinned to an unheld remote host. Before the fix neither ran."""
    from hermes_cli import kanban_db_dispatch as kbd
    _local_is(monkeypatch, "ci-box")
    with kb.connect_closing() as conn:
        rid = kb.create_task(conn, title="review", assignee="alpha", body="do the thing", priority=10)
        _set(conn, rid, status="review", origin_host=None)
        tid = kb.create_task(conn, title="pinned", assignee="alpha", body="host: ace-ai\ndo the thing", priority=0)
        _set(conn, tid, status="ready", origin_host=None)
        _hold(home)
        spawned = []

        def spawn(task, workspace, *, board=None, placement=None):
            spawned.append((task.id, placement.name if placement else "studio"))
            return 4242

        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, spawn_limit=8, spillover=_plan(),
                                reconcile_orphans=False)
        assert (tid, "ace-ai") in spawned, (spawned, res.placement_waits)
        assert "stage=destination" in res.placement_waits.get(rid, ""), res.placement_waits
        assert _runs(conn, rid) == 0


def test_local_fence_reads_the_resolved_fqdn(home, monkeypatch):
    """9fb3e16068f3: ``gethostname`` returns a short name the registry does not know; the resolved FQDN carries the
    held host's name. The local fence must check the full resolved name, not only the stripped short form."""
    import socket
    _registry(home, {"ci-box": {"aliases": ["ci-box.tail0000.ts.net"]}})
    monkeypatch.setattr(socket, "gethostname", lambda: "ci")
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "ci-box.tail0000.ts.net" if name == "ci" else name)
    _hold(home)
    assert kph.local_names() == ["ci", "ci-box", "ci-box.tail0000.ts.net"]
    why = kph.local_destination_wait({})
    assert why is not None and "stage=destination" in why, why
    with kb.connect_closing() as conn:
        tid = _unorigined(conn, "do the thing")
        _res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}, spawned


def test_fqdn_is_resolved_once_per_process(home, monkeypatch):
    import socket
    calls = []
    monkeypatch.setattr(socket, "getfqdn", lambda name="": calls.append(name) or name)
    for _ in range(3):
        kph.local_names()
    assert calls == ["fwa-test-runner.lan"]


# -- round 6 (Prism round 4 @ 13b1957d: 96dd675bda52) ------------------------------------------------------------------

def test_held_portable_destination_is_filtered_case_insensitively(home, monkeypatch):
    """96dd675bda52: the pool names ``CI-BOX`` while the reach filter returns ``ci-box``. The held host must still be
    dropped from the portable pick, so the card places on the unheld host instead of waiting on the post-take belt."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    from tests.hermes_cli.test_kanban_pause_hold import _host
    hosts = [_host("CI-BOX"), _host("ace-ai")]
    cfg = kwp.PoolConfig(hosts=("CI-BOX", "ace-ai"), profiles=("alpha",), pool_hosts=tuple(hosts))
    plan = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    plan.band, plan.spill_reason, plan.default_remote = "admitting", None, True
    monkeypatch.setattr(kbd, "_portable_only", lambda *a, **k: (("ci-box", "ace-ai"), None))
    _hold(home, host="CI-BOX")
    assert kph.held_destinations(["CI-BOX", "ace-ai"], {}).keys() == {"CI-BOX"}
    with kb.connect_closing() as conn:
        tids = [_unorigined(conn, "do the thing") for _ in range(2)]
        res, spawned = _tick(conn, plan)
        assert {(t, "ace-ai") for t in tids} <= set(spawned), (spawned, res.placement_waits)
        assert not any(t in res.placement_waits for t in tids), res.placement_waits


# -- round 6 (Prism round 4 @ 13b1957d: b622dcc4adae / 5afa560835bc) ------------------------------------------------

def _label_card(holds_out, tid):
    return next(c for c in holds_out["cards"] if c["task_id"] == tid)


def _dest_label(conn, home, monkeypatch, body, origin):
    """A card the destination fence waited (a live hold on ci-box, the dispatcher's own host); its label is open."""
    _local_is(monkeypatch, "ci-box")
    tid = kb.create_task(conn, title="dest", assignee="alpha", body=body)
    _set(conn, tid, status="ready", origin_host=origin)
    hold = _hold(home)
    res, _spawned = _tick(conn, _plan())
    assert "stage=destination" in res.placement_waits.get(tid, ""), res.placement_waits
    return tid, hold


@pytest.mark.parametrize("body,origin", [
    pytest.param("do the thing", None, id="origin-less-local"),
    pytest.param("host: ci-box\ndo the thing", "ace-ai", id="body-pin-unheld-origin"),
])
def test_destination_label_reads_live_in_placement_holds(home, monkeypatch, body, origin):
    """b622dcc4adae / 5afa560835bc: a pause_hold label written by a destination fence is LIVE while that destination
    is held, even though decide(origin) holds nothing; it reads stale once the hold is gone."""
    with kb.connect_closing() as conn:
        tid, hold = _dest_label(conn, home, monkeypatch, body, origin)
        card = _label_card(kph.holds(conn), tid)
        assert card["live"] is True and card["now"] is not None, card
        assert "stage=destination" in card["now"][1], card
        hold.unlink()
        card = _label_card(kph.holds(conn), tid)
        assert card["live"] is False and card["now"] is None, card


def test_live_destination_label_is_not_cleared_as_stale(home, monkeypatch):
    """The stale-label sweep uses the same liveness: a destination label stays while its host is held (a tick that
    did not route the card again must not end it), and is cleared once the hold is gone."""
    with kb.connect_closing() as conn:
        tid, hold = _dest_label(conn, home, monkeypatch, "do the thing", None)
        empty = type("R", (), {"placement_waits": {}})()
        assert kph.clear_stale_labels(conn, empty, False, {}) == []
        hold.unlink()
        assert kph.clear_stale_labels(conn, empty, False, {}) == [tid]


def test_destination_label_liveness_is_off_under_the_kill_switch(home, monkeypatch):
    """Dormant == identical: under ``pause_hold_source: manifest`` no fwa destination holds, so the label is stale."""
    with kb.connect_closing() as conn:
        tid, _hold_path = _dest_label(conn, home, monkeypatch, "do the thing", None)
        _source(monkeypatch, "manifest")
        assert _label_card(kph.holds(conn), tid)["live"] is False
