"""No default Studio route for a pool-host capability block, no invented requires: word (t_9392860c).

Fixture: t_84b66cc3 run 84264 on ace-ai, 2026-11-21 09:03 PT. The worker had no ace@nas-fleet login; the reason
names no Studio resource. The dispatcher re-routed it to ``host: studio`` with ``requires: acoustic-bench`` (the
alphabetically first Studio-only word) and default-deny held it 30 min.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_placement_block as kpb
from hermes_cli import kanban_studio_capability as ksc
from hermes_cli import kanban_worker_pool as kwp

T84_REASON = (
    "unblock_when: ruling:t_84b66cc3-rig-drill-ace-at-nas-fleet. The code is done: mem0-selfhost-deploy#206 and "
    "hermes-home#13831 are open with merge hold, and the unit tests pass. The rig drill has not run. "
    "`scripts/swap/drill.sh run` on nas-fleet as ace (its phase 6b drives the new fence-on-return ticks) needs an "
    "ace@nas-fleet login. This worker only has fleetw@nas-fleet: publickey is denied for ace@192.168.1.154. Rerun "
    "the card on a host with ace@nas-fleet, or the operator runs the drill on #206's head.")
T84_BODY = "Repo: ANG-Ventures/mem0-selfhost-deploy\nrequires: nas-admin\nrun the rig drill"
KEYCHAIN_REASON = "The token is only in the Studio login keychain; this pool host has no keychain access."

# The live map's shape on 2026-11-21: studio-state, fleet-ssh, docker and nas-admin are on pool rows too.
_STUDIO = frozenset({"acoustic-bench", "android-sdk", "apple-apps", "browser-identity", "browser-session",
                     "control-plane", "imessage", "keychain", "obsidian-vault", "op-desktop", "studio-state",
                     "fleet-ssh", "docker", "nas-admin"})
_POOL_ROW = frozenset({"studio-state", "fleet-ssh", "docker", "nas-admin"})
CMAP = khc.CapabilityMap(capabilities=tuple(sorted(_STUDIO)), hosts={khc.STUDIO_ROW: _STUDIO, "ace-ai": _POOL_ROW})
STUDIO_ONLY = ksc.studio_only(CMAP)


def _host(name):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)


POOL = kwp.PoolConfig(hosts=("ace-ai", "ci-box"), profiles=("alpha",), pool_hosts=(_host("ace-ai"), _host("ci-box")))


def _c(reason, body=T84_BODY, strict=True):
    return kpb.classify(kind="capability", reason=reason, placed_host="ace-ai", body=body, pool=POOL,
                        rerouted_before=False, assignee="alpha", workspace_kind="scratch",
                        studio_reasons=STUDIO_ONLY, strict=strict)


# -- (1) no default route to the Studio --------------------------------------------------------------

def test_t84_reason_goes_to_the_owner_not_the_studio():
    assert STUDIO_ONLY is not None and "studio-state" not in STUDIO_ONLY
    action, arg = _c(T84_REASON)
    assert action == kpb.OWNER_WAKE
    assert "ace@nas-fleet login" in arg


def test_kill_switch_off_restores_the_studio_route():
    assert _c(T84_REASON, strict=False) == ("reroute", kwp.PIN_STUDIO)


def test_reason_naming_a_studio_only_capability_still_reroutes():
    assert _c(KEYCHAIN_REASON) == ("reroute", kwp.PIN_STUDIO)


def test_reason_asking_for_the_studio_with_no_capability_is_unroutable():
    action, why = _c("Needs a worker on the Mac Studio. Re-run with host: studio.")
    assert action == "unroutable" and "names no Studio-only capability" in why


def test_reason_naming_a_pool_host_still_reroutes_there():
    assert _c("Re-dispatch with host: ci-box, it has the login.") == ("reroute", "ci-box")


def test_switch_is_default_on_and_config_turns_it_off(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {})
    assert kpb.strict_enabled() is True
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {kpb.STRICT_SWITCH: "no"}})
    assert kpb.strict_enabled() is True                       # non-bool -> default
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {kpb.STRICT_SWITCH: False}})
    assert kpb.strict_enabled() is False


# -- (2) no invented word ----------------------------------------------------------------------------

def test_no_alphabetical_fallback_word():
    assert kpb.studio_requires_word(T84_REASON, STUDIO_ONLY, strict=True) is None
    assert kpb.pin_body(T84_BODY, kwp.PIN_STUDIO, reason=T84_REASON, studio_reasons=STUDIO_ONLY, pool=POOL,
                        strict=True) is None
    # The pre-t_9392860c behaviour, kept behind the kill switch: the first Studio-only word by sort order.
    assert kpb.studio_requires_word(T84_REASON, STUDIO_ONLY, strict=False) == "acoustic-bench"


def test_the_word_written_is_the_one_the_reason_supports():
    assert kpb.studio_requires_word(KEYCHAIN_REASON, STUDIO_ONLY, strict=True) == "keychain"
    assert kpb.studio_requires_word("needs the vault", STUDIO_ONLY, strict=True) == "obsidian-vault"
    assert kpb.studio_requires_word("open it in a browser", STUDIO_ONLY, strict=True) == "browser-session"
    # t_b4ca4ff0 (11-21): a notes-sync path under ~/Obsidian is not the Studio vault.
    assert kpb.studio_requires_word("the target /home/kanbanw/Obsidian/Ace Place/x.md needs the kanbanw login",
                                    STUDIO_ONLY, strict=True) is None
    # fleet-ssh is on pool rows, so a Studio pin cannot be justified by it.
    assert kpb.studio_requires_word("needs ssh to ci", STUDIO_ONLY, strict=True) is None
    out = kpb.pin_body(T84_BODY, kwp.PIN_STUDIO, reason=KEYCHAIN_REASON, studio_reasons=STUDIO_ONLY, pool=POOL,
                       strict=True)
    assert "keychain" in khc.declared_words(out) and "acoustic-bench" not in khc.declared_words(out)


# -- dispatcher tick ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in placement-block test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    monkeypatch.setattr(kwp, "read_pool", lambda *a, **kw: POOL)
    monkeypatch.setattr(ksc, "create_reasons", lambda cmap: STUDIO_ONLY)
    monkeypatch.setattr(kpb, "strict_enabled", lambda: True)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    return home


def _placed_block(conn, body, reason, boomerang=False):
    tid = kb.create_task(conn, title="t", assignee="alpha", workspace_kind="scratch")
    conn.execute("UPDATE tasks SET body=?, boomerang_wake=? WHERE id=?", (body, 1 if boomerang else 0, tid))
    conn.commit()
    assert kb.claim_task(conn, tid, claimer="alpha") is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_allocated", {"host": "ace-ai", "reason": "band_spilling"}, run_id=run_id)
    assert kb.block_task(conn, tid, reason=reason, kind="capability")
    return tid


def _events(conn, tid, kind):
    return [json.loads(r[0] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _tick(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **kw: None, max_spawn=0, reconcile_orphans=False)


def _body(conn, tid):
    return conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_tick_leaves_t84_blocked_and_wakes_the_owner_once(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, T84_BODY, T84_REASON)
        before = _body(conn, tid)
        res = _tick(conn)
        assert res.placement_rerouted == [] and res.placement_unroutable == []
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"
        assert _body(conn, tid) == before                    # no host: studio, no acoustic-bench
        assert "acoustic-bench" not in _body(conn, tid)
        assert _events(conn, tid, kpb.REROUTED_EVENT) == []
        owner = _events(conn, tid, kpb.OWNER_WAKE_EVENT)
        assert len(owner) == 1 and owner[0]["placed_host"] == "ace-ai"
        wakes = _events(conn, tid, "mechanical_wake")
        assert len(wakes) == 1 and wakes[0]["source"] == kpb.OWNER_WAKE_SOURCE
        assert "ace@nas-fleet login" in wakes[0]["text"]
        _tick(conn)                                          # once per block, not per tick
        assert len(_events(conn, tid, kpb.OWNER_WAKE_EVENT)) == 1
        assert len(_events(conn, tid, "mechanical_wake")) == 1


def test_tick_boomerang_card_relies_on_its_blocked_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, T84_BODY, T84_REASON, boomerang=True)
        _tick(conn)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "blocked"
        assert len(_events(conn, tid, kpb.OWNER_WAKE_EVENT)) == 1
        assert _events(conn, tid, "mechanical_wake") == []


def test_tick_reroutes_a_keychain_block_with_the_supported_word(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "read the token", KEYCHAIN_REASON)
        res = _tick(conn)
        assert res.placement_rerouted == [(tid, "studio")]
        words = khc.declared_words(_body(conn, tid))
        assert words == ["keychain"]


def test_hint_does_not_match_inside_a_hyphenated_capability():
    # Prism ff96bb11f1f1: "browser" inside "browser-identity" is that capability, not browser-session.
    both = frozenset({"browser-identity", "browser-session"})
    assert kpb.studio_requires_word("Needs browser-identity", both, strict=True) == "browser-identity"
    assert kpb.supported_studio_words("Needs browser-identity", both) == ("browser-identity",)
    assert kpb.studio_requires_word("open it in a browser", both, strict=True) == "browser-session"
