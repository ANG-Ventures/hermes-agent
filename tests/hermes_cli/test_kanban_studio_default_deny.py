"""Mac Studio default-deny (t_062a5948, Ace 2026-11-11): a worker starts on the Studio only for an allowlisted
capability or a live operator override; otherwise the card waits, named. Switch read per tick, default off.

Rules pinned here:
1. Off by default: a Studio-only card still runs on the Studio (t_9965b8d5 unchanged).
2. On: a capability not on the allowlist waits ``studio_default_deny:<cap> (...)``; on the allowlist it runs.
3. The switch flips within one tick (ROOT config re-read on the next ``_route_row``, no restart).
4. Operator-only cards (operator assignee) are exempt.
5. ``studio-override`` is operator-token only, needs ``--capability`` and ``--why``; ``--once`` is used up by the
   next spawn; ``--until`` holds until its time; each override writes one ledger line.
6. A held card writes ONE ``mechanical_wake`` per hold episode (silent wake to its home session).
7. A hold is a structural wait: the starvation report names a fix for it.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_studio_allowlist as ksa

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_pins_and_carry import _row, _studio_last_resort_on  # noqa: F401
from tests.hermes_cli.test_kanban_studio_capability_only import _caps


def _cfg(home, enabled=None, allow=None):
    text = "kanban:\n  studio_pin_requires_reason: false\n"
    if enabled is not None:
        text += f"  placement:\n    studio_default_deny:\n      enabled: {str(enabled).lower()}\n"
        if allow is not None:
            text += "      allow: [" + ", ".join(allow) + "]\n"
    (home / "config.yaml").write_text(text)


def _route(conn, tid):
    return kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=_caps(), rules_out={}, carry_out={},
                          studio_out={})


def _card(conn, body="requires: studio-state", assignee="alpha"):
    return kb.create_task(conn, title="s", assignee=assignee, body=body)


# -- 1/2. off by default; on = allowlist or wait -----------------------------------------------------------------

def test_default_off_studio_only_card_still_runs_local(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert _route(conn, tid)[0] == "local"


def test_on_and_not_allowlisted_waits_naming_the_capability(kanban_home):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        route = _route(conn, tid)
    assert route[0] == "wait", route
    assert route[1].startswith("studio_default_deny:studio-state ("), route
    assert "declared" in route[1] and "no pool host" in route[1]


def test_on_and_allowlisted_runs_local(kanban_home):
    _cfg(kanban_home, True, ["studio-state"])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert _route(conn, tid)[0] == "local"


def test_portable_card_is_untouched(kanban_home):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn, body="fix the lint")
        assert _route(conn, tid) == ("portable", None)


def test_multi_part_capability_needs_every_part():
    assert ksa.allowed("control-plane,obsidian-vault", {"control-plane", "obsidian-vault"})
    assert not ksa.allowed("control-plane,obsidian-vault", {"control-plane"})
    assert ksa.allowed("workspace_kind:origin_not_github", {"workspace_kind"})
    assert not ksa.allowed(None, {"x"}) and not ksa.allowed("", {"x"})


# -- 3. one tick to flip ---------------------------------------------------------------------------------------

def test_switch_flips_within_one_tick_without_restart(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert _route(conn, tid)[0] == "local"
        _cfg(kanban_home, True, [])
        assert _route(conn, tid)[0] == "wait"
        _cfg(kanban_home, False, [])
        assert _route(conn, tid)[0] == "local"


def test_full_tick_holds_and_never_spawns_on_the_studio(kanban_home, monkeypatch):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=0), spawn_limit=2)
    assert spawned == [], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:studio-state"), res.placement_waits


# -- 4. operator-only exempt -----------------------------------------------------------------------------------

def test_operator_assignee_is_exempt(kanban_home):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee="aegis")
        assert _route(conn, tid)[0] == "local"


# -- 5. the override verb ---------------------------------------------------------------------------------------

@pytest.fixture
def operator(kanban_home, monkeypatch):
    p = kb.operator_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tok")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    return kanban_home


def test_override_refused_without_operator_token(kanban_home, monkeypatch):
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused, match="operator-only"):
            ksa.override(conn, tid, "studio-state", "needs the live board db")


@pytest.mark.parametrize("cap,why,match", [("", "needs the live board", "--capability"),
                                           ("a,b", "needs the live board", "ONE capability"),
                                           ("studio-state", "because", "--why")])
def test_override_needs_capability_and_reason(operator, cap, why, match):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused, match=match):
            ksa.override(conn, tid, cap, why)


def test_once_override_releases_one_run_then_asks_again(operator):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert _route(conn, tid)[0] == "wait"
        p = ksa.override(conn, tid, "studio-state", "reads the live board db", actor="apollo")
        assert p["once"] and p["held_capability"] is None
        assert _route(conn, tid)[0] == "local"
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 1})
        assert _route(conn, tid)[0] == "wait"
    lines = [json.loads(x) for x in ksa.ledger_path(operator).read_text().splitlines()]
    assert len(lines) == 1
    row = lines[0]
    assert row["task_id"] == tid and row["capability"] == "studio-state" and row["by"] == "apollo"
    assert row["why"] == "reads the live board db" and row["fix_card"] is None and row["once"] is True


def _spawn_run(conn, tid, host=None):
    """One run's start events: ``worker_placed`` naming ``host`` (None = a Studio start, no event), then ``spawned``."""
    run_id = conn.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM task_events").fetchone()[0]
    with kb.write_txn(conn):
        if host is not None:
            kb._append_event(conn, tid, "worker_allocated", {"host": host, "pid": 1}, run_id=run_id)
        kb._append_event(conn, tid, "spawned", {"pid": 1}, run_id=run_id)


def test_once_override_survives_a_pool_host_run(operator):
    """t_a94fafef: a run on ace-media never started on the Studio, so it does not use up a --once override."""
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        ksa.override(conn, tid, "studio-state", "reads the live board db", actor="apollo")
        _spawn_run(conn, tid, host="ace-media")
        assert ksa.live_override(conn, tid, "studio-state") is not None
        assert _route(conn, tid)[0] == "local"
        assert ksa.unspent_override_note(conn, tid) == "studio-override studio-state unspent (once)"


def test_once_override_spent_by_a_studio_run_and_refuses_the_second(operator):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        ksa.override(conn, tid, "studio-state", "reads the live board db", actor="apollo")
        _spawn_run(conn, tid, host="ace-media")
        assert _route(conn, tid)[0] == "local"
        _spawn_run(conn, tid)                      # first Studio start: uses it up
        assert ksa.live_override(conn, tid, "studio-state") is None
        assert _route(conn, tid)[0] == "wait"      # a second Studio start is refused
        assert ksa.unspent_override_note(conn, tid) is None


def test_worker_placed_with_studio_host_name_spends_it(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        ksa.override(conn, tid, "studio-state", "reads the live board db", actor="apollo")
        _spawn_run(conn, tid, host="mac-studio")
        assert ksa.live_override(conn, tid, "studio-state") is None


def test_pool_placement_names_the_unspent_override(operator, monkeypatch):
    """t_a94fafef part 2: a card placed off the Studio while its override waits says so on worker_placed."""
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn, body=None)               # portable: the pool takes it
        ksa.override(conn, tid, "studio-state", "reads the live board db", actor="apollo")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == [(tid, "ace-ai")], spawned
        (raw,) = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_allocated'",
                              (tid,)).fetchone()
        assert json.loads(raw)["studio_override_unspent"] == "studio-override studio-state unspent (once)"
        assert ksa.live_override(conn, tid, "studio-state") is not None


def test_until_override_holds_across_spawns_until_its_time(operator):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        ksa.override(conn, tid, "studio-state", "standing reason for this card", until="+1h", now=1_000_000)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 1})
        row = _row(conn, tid)
        why = {"capability": "studio-state", "source": "declared", "evidence": "x"}
        assert ksa.decide(conn, row, why, enabled=True, allow=frozenset(), now=1_000_000 + 1800) is None
        assert ksa.decide(conn, row, why, enabled=True, allow=frozenset(), now=1_000_000 + 3601) is not None


def test_override_for_another_capability_does_not_release(operator):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn)
        ksa.override(conn, tid, "browser-session", "needs the signed-in browser", need="signed-in-identity")
        assert _route(conn, tid)[0] == "wait"


def test_override_names_an_open_fix_card(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        fix = kb.create_task(conn, title="gap", assignee="alpha", body="x", idempotency_key="studio-capgap:studio-state")
        p = ksa.override(conn, tid, "studio-state", "reads the live board db")
    assert p["fix_card"] == fix


def test_cli_prints_one_line_and_refuses_with_rc1(operator, capsys):
    import argparse
    with kb.connect_closing() as conn:
        tid = _card(conn)
    ok = ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="studio-state",
                                                    why="reads the live board db", until=None, board=None))
    out = capsys.readouterr().out
    assert ok == 0 and f"{tid} may start on the Studio for studio-state once" in out, out
    bad = ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="", why="reads the live board",
                                                     until=None, board=None))
    assert bad == 1 and "REFUSED" in capsys.readouterr().err


def test_verb_is_registered_and_denied_to_subagents():
    import argparse
    from hermes_cli import kanban as kcli
    import argparse
    from hermes_cli import kanban_parser as kp
    assert "studio-override" in kcli._DELEGATED_CHILD_DENIED_ACTIONS
    top = argparse.ArgumentParser()
    kp.build_parser(top.add_subparsers(dest="cmd"))
    ns = top.parse_args(["kanban", "studio-override", "t_00000001", "--capability", "studio-state",
                         "--why", "reads the live board", "--until", "+2h"])
    assert ns.kanban_action == "studio-override" and ns.capability == "studio-state" and ns.until == "+2h"
    with pytest.raises(SystemExit):
        top.parse_args(["kanban", "studio-override", "t_00000001", "--why", "x y z"])  # no --capability
    with pytest.raises(SystemExit):
        top.parse_args(["kanban", "studio-override", "t_00000001", "--capability", "c", "--why", "x y z",
                        "--once", "--until", "+1h"])


# -- 6. one wake per hold episode -------------------------------------------------------------------------------

def test_hold_wakes_the_home_session_once_per_episode(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        waits = {tid: "studio_default_deny:studio-state (declared: requires: studio-state; no pool host has it)"}
        assert ksa.notify_holds(conn, waits) == [tid]
        assert ksa.notify_holds(conn, waits) == []
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {})
        assert ksa.notify_holds(conn, waits) == [tid]
        evs = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake'",
                           (tid,)).fetchall()
    assert len(evs) == 2
    p = json.loads(evs[0][0])
    assert p["source"] == "studio-default-deny" and "studio-override" in p["text"]


def test_full_tick_writes_the_wake(kanban_home, monkeypatch):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _tick(conn, spillover=_plan(free=0), spawn_limit=2)
        n = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='mechanical_wake'",
                         (tid,)).fetchone()[0]
    assert n == 1


def test_hold_pages_once_after_60_minutes(kanban_home):
    class Pager:
        def __init__(self):
            self.sent = []

        def page(self, key, text):
            self.sent.append((key, text))
            return "page"

    pager = Pager()
    with kb.connect_closing() as conn:
        tid = _card(conn)
        waits = {tid: "studio_default_deny:studio-state (declared: x; no pool host has it)"}
        ksa.notify_holds(conn, waits, pager_factory=lambda: pager)
        t0 = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='mechanical_wake'",
                          (tid,)).fetchone()[0]
        ksa.notify_holds(conn, waits, now=t0 + 3599, pager_factory=lambda: pager)
        assert pager.sent == []
        ksa.notify_holds(conn, waits, now=t0 + 3600, pager_factory=lambda: pager)
        ksa.notify_holds(conn, waits, now=t0 + 7200, pager_factory=lambda: pager)
    assert len(pager.sent) == 1 and pager.sent[0][0] == f"held:{tid}" and "60 min" in pager.sent[0][1]


# -- 7. the 60-minute page rides the structural-hold report -------------------------------------------------------

def test_hold_is_structural_for_the_starvation_report():
    from hermes_cli.kanban_placement_starvation import fix_for
    fix = fix_for("studio_default_deny:studio-state")
    assert fix and "studio-override" in fix


# -- Prism r1 on #251: local starts that never reach _route_row, effective assignee, structural details ----------

def test_unplanned_tick_holds_a_studio_only_card(kanban_home, monkeypatch):
    """827ad8491946: no pool plan (disabled/refused/empty) used to default every card to a local start."""
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn)
        res, spawned = _tick(conn, spillover=None, spawn_limit=2)
    assert spawned == [], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:studio-state"), res.placement_waits


def test_unplanned_tick_holds_an_undeclared_card_as_pool_unavailable(kanban_home, monkeypatch):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn, body="fix the lint")
        res, spawned = _tick(conn, spillover=None, spawn_limit=2)
    assert spawned == [], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:pool_unavailable"), res.placement_waits


def test_unplanned_tick_allowlisted_and_off_still_run_local(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _card(conn)
        _cfg(kanban_home, True, ["studio-state"])
        _res, spawned = _tick(conn, spillover=None, spawn_limit=2)
        assert [t for t, _h in spawned] == [tid]
    with kb.connect_closing() as conn:
        tid2 = _card(conn)
        _cfg(kanban_home, False, [])
        _res, spawned = _tick(conn, spillover=None, spawn_limit=2)
        assert [t for t, _h in spawned] == [tid2]


def _review_card(conn, body="requires: studio-state"):
    tid = _card(conn, body=body)
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    conn.commit()
    return tid


def test_review_lane_local_start_is_held(kanban_home, monkeypatch):
    """827ad8491946: the review-spawn loop never consulted the rule."""
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _review_card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
    assert tid not in [t for t, _h in spawned], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:studio-state"), res.placement_waits


def test_review_lane_allowlisted_runs(kanban_home, monkeypatch):
    _cfg(kanban_home, True, ["review_lane"])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _review_card(conn, body="review the diff")
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
    assert tid in [t for t, _h in spawned], spawned


def test_operator_exemption_uses_the_effective_assignee(kanban_home):
    """An unassigned card the dispatcher hands to an operator default is exempt; a case variant is not."""
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid = _card(conn, assignee=None)
        row = _row(conn, tid)
        assert kbd._route_row(conn, row, _plan(free=2), "aegis", caps=_caps(), rules_out={}, carry_out={},
                              studio_out={})[0] == "local"
        why = {"capability": "studio-state", "source": "declared", "evidence": "x"}
        assert ksa.decide(conn, row, why, enabled=True, allow=frozenset(), assignee="Aegis") is not None


def test_structural_capability_with_parenthesized_detail_is_matched():
    """e3e (Prism) on #251: ``linked(derived-from->t_x)`` is the ``linked`` rule."""
    assert ksa.allowed("linked(derived-from->t_1234abcd)", {"linked"})
    assert ksa.head("linked(derived-from->t_1)") == "linked"


# -- Prism r2 on #251: review-lane holds notified + persisted, browser-joined local starts --------------------------

def test_review_lane_hold_wakes_and_persists_its_wait(kanban_home, monkeypatch):
    """03ee93f37df3 / 8ba6f0531198: the notify pass ran before the review loop collected its holds."""
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = _review_card(conn)
        _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        kinds = [k for (k,) in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        wait = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='allocation_wait' "
                            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert kinds.count("mechanical_wake") == 1, kinds
    assert wait is not None and json.loads(wait[0])["reason"].startswith("studio_default_deny:studio-state"), kinds


def test_browser_joined_portable_local_start_is_held(kanban_home, monkeypatch):
    """827ad8491946 (r2): a portable row whose browser join was cut for the Studio starts locally, past _route_row."""
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())

    def gate(conn, ready_rows, result, row_route, dry_run, default_assignee=None, **kw):
        for r in ready_rows:
            result.browser_joins[r["id"]] = {"plans": [{"host": "ci-box"}], "source_host": "studio"}

    monkeypatch.setattr(kbd, "_browser_gate", gate)
    monkeypatch.setattr(kbd, "_browser_join_lease", lambda conn, claimed, join, placed_host: ({}, None))
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None, browser_env=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    with kb.connect_closing() as conn:
        tid = _card(conn, body="fix the lint")
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spawn_paused=None,
                                spillover=_plan(free=2), reconcile_orphans=False)
    assert (tid, "studio") not in spawned, spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:browser_join"), res.placement_waits


# -- Prism on #264 (t_5d63270d) -------------------------------------------------------------------------------

def test_held_review_reserves_no_ready_lane_capacity(kanban_home, monkeypatch):
    """c-4f52c4dd8765315a: with one slot left, a review default-deny will hold must not keep an equal-priority
    ready card from starting (``_review_ahead`` / review pool reserve / door publication before the hold)."""
    _cfg(kanban_home, True, ["studio-state"])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        rid = _review_card(conn, body="review the diff")
        tid = _card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=0), spawn_limit=1)
    assert [t for t, _h in spawned] == [tid], (spawned, res.placement_waits)
    assert res.placement_waits[rid].startswith("studio_default_deny:review_lane"), res.placement_waits


def test_admit_to_the_studio_is_held_until_an_override_is_recorded(operator, monkeypatch):
    """c-e7592fee5f5910ad: ``kanban admit --host studio`` on a portable card skipped the local-start check
    (``force_host is None``). It is held, the refusal names the override, and the override is the audit record."""
    from hermes_cli import kanban_admit as ka
    _cfg(operator, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    with kb.connect_closing() as conn:
        tid = _card(conn, body="fix the lint")
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
        assert spawned == [], spawned
        assert res.placement_waits[tid].startswith("studio_default_deny:operator_admit"), res.placement_waits
        line = ka.refusal_from(res, tid)
        assert f"hermes kanban studio-override {tid} --capability operator_admit" in line, line
        ksa.override(conn, tid, "operator_admit", "operator ruling needs the studio", actor="apollo")
        kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
        kinds = [k for (k,) in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    assert spawned == [(tid, "studio")], spawned
    assert kinds.index("studio_override") < kinds.index("spawned"), kinds


def test_a_cleared_placement_wait_ends_the_hold_episode(kanban_home):
    """c-51c01dfda9372e31: ``placement_wait {cleared: true}`` ends the episode in both scans: the next hold wakes
    the home session again and its 60-minute clock restarts."""
    class Pager:
        sent: list = []

        def page(self, key, text):
            self.sent.append(key)
            return "page"

    pager = Pager()
    with kb.connect_closing() as conn:
        tid = _card(conn)
        waits = {tid: "studio_default_deny:studio-state (declared: x; no pool host has it)"}
        assert ksa.notify_holds(conn, waits) == [tid]
        # the first episode began two hours ago, then its wait cleared
        conn.execute("UPDATE task_events SET created_at = created_at - 7200 WHERE task_id=? AND kind='mechanical_wake'",
                     (tid,))
        conn.commit()
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "allocation_wait", {"cleared": True})
        assert ksa.notify_holds(conn, waits, pager_factory=lambda: pager) == [tid]
        ksa.notify_holds(conn, waits, pager_factory=lambda: pager)
    assert pager.sent == [], pager.sent  # the new episode began at the second wake, not two hours ago


def test_override_warning_uses_the_live_override_match(operator, capsys):
    """c-8a854f5bdb779ec7: an override naming the capability HEAD releases ``workspace_kind:x`` (live_override),
    so the command must not warn that it does not."""
    import argparse
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "allocation_wait",
                             {"reason": "studio_default_deny:workspace_kind:origin_not_github (structural: x)"})
        why = {"capability": "workspace_kind:origin_not_github", "source": "structural", "evidence": "x"}
        ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="workspace_kind",
                                                   why="the port is not built yet", until=None, board=None))
        released = ksa.decide(conn, _row(conn, tid), why, enabled=True, allow=frozenset()) is None
    err = capsys.readouterr().err
    assert released and "WARNING" not in err, err
    ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="browser-session",
                                               why="the signed in browser", until=None, board=None,
                                               need="signed-in-identity"))
    assert "does not release it" in capsys.readouterr().err


def test_local_review_placement_records_the_resolved_capability(kanban_home, monkeypatch):
    """c-0d4f28b3daf38893: the review placement event names what the gate resolved (declared before review_lane),
    so the replay need not assume review_lane."""
    _cfg(kanban_home, True, ["studio-state", "review_lane"])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        declared = _review_card(conn)
        plain = _review_card(conn, body="review the diff")
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        got = {}
        for tid in (declared, plain):
            raw = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='allocation' "
                               "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            p = json.loads(raw[0])
            p = json.loads(p) if isinstance(p, str) else p
            got[tid] = (p.get("entry"), (p.get("studio_capability") or {}).get("capability"))
    assert {declared, plain} <= {t for t, _h in spawned}, spawned
    assert got == {declared: ("review", "studio-state"), plain: ("review", "review_lane")}, got


# -- 6. a browser-session override declares its need (t_9d6a5d75) ------------------------------------------------
# Operator ruling 11-19: the --why text is not parsed; --need is a fixed set and only signed-in-identity (and other,
# recorded) goes through.
REFUSED_NEEDS = [("public-page", "browser-public"), ("lan-reach", "lan-reach"), ("ssh", "fleet-ssh"),
                 ("gmail", "studio-state")]


def _overrides(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
                        (tid, ksa.OVERRIDE_EVENT)).fetchone()[0]


@pytest.mark.parametrize("need,instead", REFUSED_NEEDS)
def test_browser_override_with_a_non_identity_need_is_refused_naming_the_capability(operator, need, instead):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: browser-session")
        with pytest.raises(ksa.OverrideRefused) as exc:
            ksa.override(conn, tid, "browser-session", "needs a signed-in browser for checkout", need=need)
        assert _overrides(conn, tid) == 0
    assert f"--capability {instead} instead" in str(exc.value), str(exc.value)


@pytest.mark.parametrize("need", [None, "", "browser", "Signed In"])
def test_browser_override_without_a_valid_need_is_refused(operator, need):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: browser-session")
        with pytest.raises(ksa.OverrideRefused) as exc:
            ksa.override(conn, tid, "browser-session", "the signed-in Amazon identity", need=need)
        assert _overrides(conn, tid) == 0
    assert "needs --need, one of: signed-in-identity" in str(exc.value)


@pytest.mark.parametrize("need", ["signed-in-identity", "other"])
def test_browser_override_with_identity_or_other_is_recorded_with_its_need(operator, need):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: browser-session")
        p = ksa.override(conn, tid, "browser-session", "no browser needed, only SSH and Gmail", need=need)
        assert _overrides(conn, tid) == 1
    assert p["need"] == need
    line = json.loads(ksa.ledger_path().read_text().splitlines()[-1])
    assert line["need"] == need  # `other` is counted from the ledger


def test_the_split_identity_name_also_declares_its_need():
    # t_f935cb6f: browser-identity is browser-session's target; overriding it by the new name is the same hold.
    assert "needs --need" in ksa.need_refusal("browser-identity", None)
    assert ksa.need_refusal("browser-identity", "signed-in-identity") is None
    assert ksa.need_refusal("browser-public", None) is None


def test_need_is_only_checked_for_browser_session(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        assert ksa.override(conn, tid, "studio-state", "reads the live board db")["capability"] == "studio-state"
    assert ksa.need_refusal("studio-state", None) is None


def test_parser_need_choices_match_the_need_table():
    import argparse
    from hermes_cli import kanban_parser as kp
    top = argparse.ArgumentParser()
    kp.build_parser(top.add_subparsers(dest="cmd"))
    args = top.parse_args(["kanban", "studio-override", "t_x", "--capability", "browser-session", "--why", "a b c",
                           "--need", "lan-reach"])
    assert args.need == "lan-reach"
    with pytest.raises(SystemExit):
        top.parse_args(["kanban", "studio-override", "t_x", "--capability", "browser-session", "--why", "a b c",
                        "--need", "browser"])
    sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction)).choices["kanban"]
    so = next(a for a in sub._actions if isinstance(a, argparse._SubParsersAction)).choices["studio-override"]
    assert tuple(next(a for a in so._actions if a.dest == "need").choices) == tuple(ksa.NEEDS)
