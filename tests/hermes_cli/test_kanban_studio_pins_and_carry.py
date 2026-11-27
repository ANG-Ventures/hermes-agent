"""The Studio is the last resort for kanban work (t_e604c0f9; Ace 2026-11-08 16:13 / 16:38 / 17:17).

Three dispatcher rules pinned a card to the Studio before the FWA scorer ran (24 h census 16:38: workspace_kind 196,
local_files 173, host_studio 86 of 483 Studio spawns):

* a bare ``host: studio`` line holds only with a ``requires:`` reason the Studio serves; else it is dropped;
* ``local_files`` (the card's Studio scratch dir holds files) is continuity: local while the local gate admits, else
  the dir is carried to the FWA-chosen host;
* a ``worktree:<repo>/.worktrees/<id>`` target not made yet anchors on the repo, so the existing port applies;
  ``kanban create`` rewrites ``.worktrees/<x>`` targets and prints the rewrite.

Kill switch: the ONE shared setting (t_c2fce87e) ``host_preference.consumers.kanban`` in
fleet/allocation-policy.json; off / absent = every old pin, exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_worktree_port import SLUG, _mirrors, anchor  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import HOST_PREF, _write_policy

STUDIO_CAPS = frozenset({"browser-session", "studio-state", "control-plane", "docker"})


def _caps(mode=khc.WARN):
    """A valid map whose Studio row serves STUDIO_CAPS; warn mode, so no requires: word is a hard rule and the
    pin rule alone decides."""
    cmap = khc.CapabilityMap(capabilities=tuple(sorted(STUDIO_CAPS)),
                             hosts={khc.STUDIO_ROW: STUDIO_CAPS, "ace-ai": frozenset()})
    return khc.CapabilityTick(cmap, mode, (), ("ace-ai",))


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


@pytest.fixture(autouse=True)
def _carry_caps_on(monkeypatch):
    """t_f3a411c2: this suite pins the capped in-tick carry (kanban.placement.carry_caps: true, the kill switch).
    The default background carry with no cap is test_kanban_carry_background.py."""
    from hermes_cli import kanban_carry_background as _kcb
    monkeypatch.setattr(_kcb, "caps_on", lambda kcfg=None: True)


@pytest.fixture(autouse=True)
def _studio_last_resort_on(kanban_home, monkeypatch):
    """The live setting (hermes-home fleet/allocation-policy.json since #10047): kanban on, mac-studio last."""
    # kwp._studio_words() adds the live gethostname() short form: run on ACE-AI, ``host: ace-ai`` named
    # this machine and resolved to the Studio pin (off-box sweep red, t_c2a562c9). Pin a name no fixture holds.
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "fwa-test-runner.lan")
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)
    # Dispatcher tests replay cards minted before entry validation existed.
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n")


def _kill_switch(home: Path, value) -> None:
    """``value`` False = host_preference.consumers.kanban off (the kill switch)."""
    _write_policy(home / "fleet", {**HOST_PREF, "consumers": {**HOST_PREF["consumers"],
                                                               "kanban": "on" if value else "off"}})


def _route(conn, tid, caps, plan=None):
    rules: dict = {}
    carry: dict = {}
    route = kbd._route_row(conn, _row(conn, tid), plan or _plan(free=2), caps=caps, rules_out=rules,
                           carry_out=carry)
    return route, rules.get(tid), carry.get(tid, False)


# -- rule 3: host: studio ------------------------------------------------------------------------------------

def test_bare_host_studio_is_dropped_and_placed_by_fwa(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nfix the lint")
        assert _route(conn, tid, _caps())[:2] == (("portable", None), "policy")


@pytest.mark.parametrize("requires", ["studio-state", "browser-session", "docker", "control-plane-read",
                                      "studio-state (the board DBs)"])
def test_host_studio_with_a_studio_reason_stays_local(kanban_home, requires):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body=f"host: studio\nrequires: {requires}")
        route, rule, _ = _route(conn, tid, _caps())
    assert route[0] == "local" and rule.startswith(("host_studio", "access_bound")), (route, rule)


def test_host_studio_with_a_reason_the_studio_does_not_serve_is_dropped(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nrequires: none")
        assert _route(conn, tid, _caps())[0] == ("portable", None)


def test_no_capability_map_honours_every_studio_pin(kanban_home):
    """No map this tick = the vocabulary is unknown: Studio work never moves on an unreadable file."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        route, rule, _ = _route(conn, tid, None)
    assert route[0] == "local" and rule == "host_studio"


def test_kill_switch_restores_the_bare_studio_pin(kanban_home):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        route, rule, _ = _route(conn, tid, _caps())
    assert route[0] == "local" and rule == "host_studio"


def test_dropped_pin_keeps_the_prose_studio_signals(kanban_home):
    """The guard against sending Studio-only work remote: a dropped pin still meets studio_affinity."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha",
                             body="host: studio\nadd to cart in the signed-in browser")
        route, rule, _ = _route(conn, tid, _caps())
    assert route == ("local", "studio_affinity:studio_body") and rule == "studio_body"


def test_dropped_studio_pin_spawns_on_the_pool_while_the_studio_is_saturated(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert (tid, "ace-ai") in spawned


def test_create_prints_the_drop(kanban_home, monkeypatch):
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    note = kwp.mint_studio_pin_note("host: studio\nfix it", kanban_home / "fleet")
    assert note and "DROPPED" in note and "studio-state" in note
    assert kwp.mint_studio_pin_note("host: studio\nrequires: studio-state", kanban_home / "fleet") is None
    assert kwp.mint_studio_pin_note("host: ace-ai", kanban_home / "fleet") is None
    _kill_switch(kanban_home, False)
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet") is None


@pytest.mark.parametrize("host", ["studio", "mac-studio-m3u"])
def test_entry_refuses_bare_pin_and_edit_is_atomic(kanban_home, monkeypatch, host):
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: refuse\n")
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="accepted words:.*studio-state"):
            kb.create_task(conn, title="entry", body=f"host: {host}\nfix lint")
        tid = kb.create_task(conn, title="edit", body="host: any")
        original = kb.get_task(conn, tid).body
        with pytest.raises(ValueError, match="requires:"):
            kb.edit_task(conn, tid, body=f"host: {host}")
        assert kb.get_task(conn, tid).body == original
        assert kb.edit_task(conn, tid, body=f"host: {host}\nlaunchctl print gui/501")
        assert "requires: studio-state" in kb.get_task(conn, tid).body
        saved = kb.create_task(conn, title="saved", body=f"host: {host}\nrequires: studio-state\nfix lint")
        stored = kb.get_task(conn, saved).body
        assert stored.startswith("origin:")
        bare = stored.replace("requires: studio-state\n", "")
        with pytest.raises(ValueError, match="requires:"):
            kb.edit_task(conn, saved, body=bare)
        assert kb.get_task(conn, saved).body == stored
        assert kb.edit_task(conn, saved, body=bare + "\nlaunchctl print gui/501")
        assert "requires: studio-state" in kb.get_task(conn, saved).body
        for work in ("launchctl", "vault", "skill_view", "localhost:18810", "run ~/.hermes/scripts/check.py"):
            derived = kb.create_task(conn, title="derived", body=f"host: {host}\n{work}")
            assert "requires: studio-state" in kb.get_task(conn, derived).body
        from hermes_cli import kanban as cli
        output = cli.run_slash(f"--board default create 'derive cli' --body 'host: {host}\nlaunchctl print gui/501'")
        assert "auto-derived requires: studio-state" in output
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="disabled", body=f"host: {host}")
        assert kb.get_task(conn, tid).body.endswith(f"host: {host}")


@pytest.mark.parametrize("mode, refuses, warns", [
    (None, False, True), ("notice", False, True), ("refuse", True, False),
    ("off", False, False), (True, True, False), (False, False, False),
])
def test_entry_modes_preserve_bare_pins_and_notice_once(kanban_home, monkeypatch, capsys,
                                                       mode, refuses, warns):
    config = kanban_home / "config.yaml"
    if mode is None:
        config.unlink()
    else:
        config.write_text("kanban:\n  studio_pin_requires_reason: " + json.dumps(mode) + "\n")
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    from hermes_cli import kanban as cli
    with kb.connect_closing() as conn:
        if refuses:
            with pytest.raises(ValueError, match="accepted words:"):
                kb.create_task(conn, title="mode create", body="host: studio\nfix lint")
        else:
            output = cli.run_slash("--board default create 'mode create' --body 'host: studio\nfix lint'")
            assert ("Warning: bare host: studio" in output) == warns
            if warns:
                assert output.count("Warning: bare host: studio") == 1
                assert "accepted words:" in output and "studio-state" in output
            tid = conn.execute("SELECT id FROM tasks WHERE title='mode create'").fetchone()[0]
            assert kb.get_task(conn, tid).body.endswith("host: studio\nfix lint")
            before = conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
            assert sum(r[0].startswith("[placement] bare host: studio") for r in before) == int(warns)
            _route(conn, tid, _caps())
            after = conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
            assert sum(r[0].startswith("[placement] bare host: studio") for r in after) == 1
        tid = kb.create_task(conn, title="mode edit", body="host: any")
        original = kb.get_task(conn, tid).body
        stamped = original.replace("host: any", "host: mac-studio-m3u\nfix lint")
        capsys.readouterr()
        if refuses:
            with pytest.raises(ValueError, match="accepted words:"):
                kb.edit_task(conn, tid, body=stamped)
            assert kb.get_task(conn, tid).body == original
        else:
            assert kb.edit_task(conn, tid, body=stamped)
            assert kb.get_task(conn, tid).body == stamped
            output = capsys.readouterr().err
            assert ("Warning: bare host: studio" in output) == warns
            assert output.count("Warning: bare host: studio") == int(warns)
        if mode is not False and mode != "off":
            capsys.readouterr()
            derived = kb.create_task(conn, title="mode derived", body="host: studio\nlaunchctl print gui/501")
            assert "requires: studio-state" in kb.get_task(conn, derived).body
            assert "Warning: bare host: studio" not in capsys.readouterr().err


def test_derived_requires_on_stamped_edit_keeps_the_host_line_first(kanban_home, monkeypatch):
    """Prism c-d245cc2a9f4f: the derived line goes after the host directive, not after the provenance header."""
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: notice\n")
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="stamped", body="host: mac-studio-m3u")
        stored = kb.get_task(conn, tid).body
        assert stored.startswith("origin:")
        assert kb.edit_task(conn, tid, body=stored + "\nlaunchctl print gui/501")
        content = [ln for ln in kwp.without_header_lines(kb.get_task(conn, tid).body).splitlines() if ln.strip()]
        assert content[:2] == ["host: mac-studio-m3u", "requires: studio-state (auto: card names studio_launchd)"]
        route, rule, _ = _route(conn, tid, _caps())
        assert route[0] == "local", (route, rule)


def test_entry_notice_failure_does_not_fail_a_saved_create(kanban_home, monkeypatch, caplog):
    """Prism c-27ab31c46b5c: the card is already saved; a notice write failure is logged, not raised."""
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: notice\n")
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    from hermes_cli import kanban_studio_pin as ksp

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ksp, "note_drop", boom)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="saved", body="host: studio\nfix lint")
        assert kb.get_task(conn, tid).body.endswith("host: studio\nfix lint")
        assert kb.edit_task(conn, tid, body=kb.get_task(conn, tid).body + "\nmore")
    assert any(r.levelname == "WARNING" and "placement notice" in r.message for r in caplog.records)


def test_drop_notice_is_durable_and_warning(kanban_home, caplog):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", body="host: studio")
        for _ in range(2):
            kwp._ignored_pin_warned.clear()
            _route(conn, tid, _caps())
        notices = conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
        assert sum(r[0].startswith("[placement] bare host: studio dropped: add requires:") for r in notices) == 1
    assert any(r.levelname == "WARNING" and "bare `host: studio` dropped" in r.message for r in caplog.records)


def test_drop_notice_already_posted_takes_no_write_lock(kanban_home, monkeypatch):
    """Every tick re-routes every dropped-pin card: once its notice exists, routing must not write."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", body="host: studio")
        first = _route(conn, tid, _caps())
        writes = []

        def no_write(*a, **_k):
            writes.append(a)
            raise AssertionError("write_txn taken")
        monkeypatch.setattr(kb, "write_txn", no_write)
        assert _route(conn, tid, _caps()) == first
    assert writes == []


def test_drop_notice_failure_does_not_change_routing(kanban_home, monkeypatch, caplog):
    """A placement notice must never stop dispatch: a raising note_drop leaves the route unchanged."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", body="host: studio")
        expected = _route(conn, tid, _caps())
        conn.execute("DELETE FROM task_comments WHERE task_id=?", (tid,))
        conn.commit()

        def boom(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(kb, "write_txn", boom)
        assert _route(conn, tid, _caps()) == expected
    assert any(r.levelname == "WARNING" and "placement notice" in r.message for r in caplog.records)


# -- rule 2: local_files is continuity ------------------------------------------------------------------------

def _card_with_files(conn, body=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha", body=body)
    path = kbw.resolve_workspace(kb.get_task(conn, tid))
    Path(path).mkdir(parents=True, exist_ok=True)
    (Path(path) / "notes.md").write_text("prior run's work\n")
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, str(path)


@pytest.fixture
def carried(monkeypatch):
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None: calls.append((host.name, ws)))
    kwh._carry_scan_cache.clear()
    return calls


def test_local_files_runs_on_the_studio_while_its_gate_admits(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None)[1:] == ("local_files", True)
        plan = _plan(free=2)
        plan.default_remote = True  # remote-first for ordinary portable cards; continuity keeps this one home
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert (tid, None) in spawned and carried == []


def test_local_files_is_carried_to_the_pool_when_the_studio_is_full(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwh.CARRIED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert carried == [("ace-ai", path)]
    assert json.loads(ev[0]) == {"host": "ace-ai", "workspace": path, "path": path}


# -- Prism 86a85c6c299a / 6c03238719d2: a carried card is bound to the host holding the copy -------------------

@pytest.mark.parametrize("continuation_on_studio_room", [True, False])
def test_a_carried_card_reruns_on_the_carry_host_never_on_the_stale_studio_copy(kanban_home, carried,
                                                                              continuation_on_studio_room):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
        assert carried == [("ace-ai", path)]
        # the run ends; the card is ready again (retry / requeue) with the Studio copy still present
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        conn.commit()
        route, rule, carry = _route(conn, tid, None)
        assert route == ("pin", "ace-ai") and rule.startswith("bind_host:ace-ai") and not carry, (route, rule)
        kw = {"spawn_limit": 2} if continuation_on_studio_room else {"spawn_limit": 0, "spawn_paused": "x"}
        _res, spawned = _tick(conn, spillover=_plan(free=2), **kw)
    assert (tid, "ace-ai") in spawned and (tid, None) not in spawned
    assert carried == [("ace-ai", path)]  # never carried a second time over the remote work


def test_a_carried_card_whose_host_is_gone_waits_never_runs_local(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": path, "path": path})
        conn.commit()
        plan = _plan(free=2)
        plan.config = plan.config.__class__(**{**plan.config.__dict__, "profiles": ()})  # no host takes alpha
        route, _rule, _ = _route(conn, tid, None, plan)
    assert route[0] in ("wait", "pin") and route != ("local", None)


def test_a_repointed_carried_card_is_unbound(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/elsewhere"})
        conn.commit()
        assert kwh.carried_host(conn, tid, path) is None


# -- Prism e8e4061341d6: one remote-first predicate for the spawn loop and the door publisher ------------------

def test_door_publisher_waits_a_continuity_row_where_the_spawn_loop_starts_it(kanban_home):
    from hermes_cli.kanban_priority import waiting_by_host
    plan = _plan(free=2)
    plan.default_remote = True
    cards = [{"id": "lf", "assignee": "alpha", "asap": False}, {"id": "p", "assignee": "alpha", "asap": False}]
    routes = {"lf": ("portable", None), "p": ("portable", None)}
    w = waiting_by_host(cards, plan, routes=routes, local_host="studio", continuity={"lf": True})
    assert [c["id"] for c in w["studio"]] == ["lf"]
    assert all(c["id"] != "p" for c in w["studio"])
    assert kbd._portable_goes_remote(True, None, plan, continuity=True) is False
    assert kbd._portable_goes_remote(True, None, plan) is True


# -- Prism a39b11fdf16d (P0): nothing caller-controlled reaches a remote shell unquoted ------------------------

HOSTILE = ["/tmp/carry;touch /tmp/carry-pwn;#", "/tmp/w; rm -rf ~", "/tmp/$(touch /tmp/pwn)", "/tmp/`id`",
           "/tmp/a b", "/tmp/-rf", "/tmp/../etc", "/tmp/x\nid", "relative/path", "/tmp/a|b", "/tmp/a&b"]


@pytest.mark.parametrize("path", HOSTILE)
def test_carry_refuses_a_shell_active_path_before_any_remote_command(path):
    calls = []
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    assert not kwh.carry_path_safe(path)
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(path)
    with pytest.raises(RuntimeError, match="shell-inert"):
        kwh.carry_workspace(host, path, runner=lambda argv, **kw: calls.append(argv))
    assert calls == []


def test_carry_path_safe_admits_real_workspace_paths():
    assert kwh.carry_path_safe("/Volumes/fleet-scratch/workspaces/default/t_e604c0f9")
    assert kwh.carry_path_safe("/home/kanbanw/.hermes/kanban/boards/x-y/workspaces/t_1/")


def test_mkdir_quotes_hostile_paths():
    """prepare_remote_workspace (the other remote command in this diff) shlex-quotes the path."""
    import shlex as _sh
    calls = []

    class P:
        returncode, stderr = 0, ""
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    for path in HOSTILE[:4]:
        kwh.prepare_remote_workspace(host, path, runner=lambda argv, **kw: (calls.append(argv), P())[1])
        assert f"-- {_sh.quote(path)} " in calls[-1][-1]


# -- Prism 7eba47409a75: the kill switch restores the old worktree-target pin and the old create output --------

def test_kill_switch_keeps_a_not_yet_made_worktrees_target_local(kanban_home, anchor):
    _mirrors(kanban_home)
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
    assert rules[tid] == "workspace_kind"
    assert kwt.anchor_repo(tid, str(anchor / ".worktrees" / tid), None) is None


def test_kill_switch_leaves_kanban_create_byte_for_byte(kanban_home, anchor):
    _mirrors(kanban_home)
    _kill_switch(kanban_home, False)
    target = str(anchor / ".worktrees" / "fix-lint")
    assert kwt.mint_rewrite("worktree", target, kanban_home / "fleet") == (target, None)
    assert kwt.mint_rewrite("worktree", str(anchor), kanban_home / "fleet") == (str(anchor), None)


def test_local_files_carry_failure_is_a_spawn_failure_never_an_empty_dir_run(kanban_home, monkeypatch):
    def boom(host, ws, runner=None):
        raise RuntimeError("rsync rc=12")
    monkeypatch.setattr(kwh, "carry_workspace", boom)
    kwh._carry_scan_cache.clear()
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert spawned == [] and tid in res.spawn_failed


def test_local_files_kill_switch_keeps_the_old_pin(kanban_home, carried):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None) == (("local", None), "local_files", False)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert spawned == [] and carried == []


def test_local_files_too_big_to_carry_keeps_the_old_pin(kanban_home, carried, monkeypatch):
    monkeypatch.setattr(kwh, "carry_size_ok", lambda path, **_kw: False)
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None) == (("local", None), "local_files", False)


def test_local_files_with_another_studio_rule_stays_local(kanban_home, carried):
    """Only the files are continuity: a card some other rule keeps home is not carried. The other rule here is
    the legacy body list, which binds only with t_c82eded0's kill switch off (prose_pins_advisory: false)."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  studio_pin_requires_reason: false\n  placement:\n    prose_pins_advisory: false\n")
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn, body="add to cart in the signed-in browser")
        route, rule, carry = _route(conn, tid, None)
    assert route[0] == "local" and rule == "local_files" and not carry


def test_carry_size_ok_bounds(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "a").write_bytes(b"x" * 10)
    kwh._carry_scan_cache.clear()
    assert kwh.carry_size_ok(str(d))
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(str(d), max_bytes=5)
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(str(d), max_entries=0)
    assert not kwh.carry_size_ok(None)


@pytest.fixture(autouse=True)
def _fresh_carry_queue():
    # Keep the fixture usable against the pre-fix module for the behavioral RED control.
    queue = getattr(kwh, "_carry_waiting", {})
    queue.clear()
    yield
    queue.clear()


def _ten_files(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    for i in range(10):
        (d / str(i)).write_text("work")
    kwh._carry_scan_cache.clear()
    return str(d)


class _Clock:
    """Advances ``step`` seconds per read: every walk check costs measurable wall."""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def _no_walk(*_a, **_k):
    pytest.fail("walked a path the cache or the budget should have answered")


def test_carry_scan_past_its_wall_bound_is_refused_and_cached(tmp_path, monkeypatch):
    # t_915b8d2d: one unbounded walk held a live tick for minutes. Past max_seconds the path is refused, and the
    # refusal is cached so later ticks do not pay for it again.
    d = _ten_files(tmp_path, "slow")
    budget = kwh.CarryScanBudget(seconds=100.0, clock=_Clock(1.0))
    assert not kwh.carry_size_ok(d, budget=budget, max_seconds=0.5)
    assert budget.deferred == 0 and budget.spent_s > 0
    monkeypatch.setattr(kwh.os, "scandir", _no_walk)
    assert not kwh.carry_size_ok(d, budget=kwh.CarryScanBudget(seconds=100.0, clock=_Clock(1.0)))


def test_carry_scan_out_of_tick_budget_is_deferred_uncached(tmp_path, monkeypatch):
    # The tick's budget is spent: the row keeps the old rule THIS tick and is judged afresh on the next one.
    d = _ten_files(tmp_path, "later")
    spent = kwh.CarryScanBudget(seconds=0.0)
    with monkeypatch.context() as m:
        m.setattr(kwh.os, "scandir", _no_walk)
        assert not kwh.carry_size_ok(d, budget=spent)
    assert spent.deferred == 1
    assert kwh.carry_size_ok(d, budget=kwh.CarryScanBudget())


def test_carry_scan_cut_by_the_tick_budget_is_deferred_uncached(tmp_path):
    d = _ten_files(tmp_path, "cut")
    budget = kwh.CarryScanBudget(seconds=0.5, clock=_Clock(1.0))
    assert not kwh.carry_size_ok(d, budget=budget, max_seconds=10.0)
    assert budget.deferred == 1 and budget.left() <= 0
    assert kwh.carry_size_ok(d, budget=kwh.CarryScanBudget())


def test_carry_scan_cache_is_stamped_when_the_walk_ends(tmp_path, monkeypatch):
    # A walk that started TTL ago but just finished is fresh: a slow tick must not expire its own census.
    d = _ten_files(tmp_path, "stamp")
    clock = _Clock(0.0)
    budget = kwh.CarryScanBudget(seconds=1e9, clock=clock)
    real_walk = kwh.os.scandir

    def walk_then_jump(*a, **k):
        clock.t += kwh.CARRY_SCAN_TTL_SECONDS + 10
        return real_walk(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(kwh.os, "scandir", walk_then_jump)
        assert kwh.carry_size_ok(d, budget=budget, max_seconds=1e9)
    monkeypatch.setattr(kwh.os, "scandir", _no_walk)
    assert kwh.carry_size_ok(d, budget=kwh.CarryScanBudget(clock=clock))


def test_carry_budget_serves_waiting_paths_fifo_under_a_fixed_row_order(tmp_path, monkeypatch):
    # Worst case: every cache entry has expired by the next tick and the rows always arrive A, B, C. Each walk
    # costs 0.8 s of a 1.0 s budget, so without a queue A re-walks every tick and C is never judged.
    a, b, c = (_ten_files(tmp_path, n) for n in "abc")
    clock = _Clock(0.0)
    real_walk = kwh.os.scandir
    walked = []

    def costly_walk(root, *args, **kw):
        walked.append(root)
        clock.t += 0.8
        return real_walk(root, *args, **kw)

    monkeypatch.setattr(kwh.os, "scandir", costly_walk)
    answers = []
    for _tick in range(3):
        budget = kwh.CarryScanBudget(seconds=1.0, clock=clock)
        answers.append([kwh.carry_size_ok(p, budget=budget) for p in (a, b, c)])
        clock.t += kwh.CARRY_SCAN_TTL_SECONDS + 1
    assert answers[0][0] and answers[1][1] and answers[2][2], answers
    assert kwh.os.path.realpath(c) in {kwh.os.path.realpath(w) for w in walked}


def test_a_waiting_path_whose_card_left_is_forgotten(tmp_path):
    d = _ten_files(tmp_path, "gone")
    clock = _Clock(0.0)
    kwh.carry_size_ok(d, budget=kwh.CarryScanBudget(seconds=0.0, clock=clock))
    assert kwh._carry_waiting
    clock.t += kwh.CARRY_WAIT_FORGET_SECONDS + 1
    assert kwh.CarryScanBudget(clock=clock).admit_gen is None and not kwh._carry_waiting


def test_a_spent_carry_budget_never_holds_a_portable_peer(kanban_home, carried, monkeypatch):
    # Saturated Studio, two local_files rows and one plain portable row, and no carry budget left: the portable
    # row is placed this tick, the local_files rows keep the old rule (not carried) and are counted as deferred.
    monkeypatch.setattr(kwh, "CARRY_TICK_SCAN_SECONDS", 0.0)
    with kb.connect_closing() as conn:
        lf1, _ = _card_with_files(conn)
        lf2, _ = _card_with_files(conn)
        port = kb.create_task(conn, title="portable", assignee="alpha")
        res, spawned = _tick(conn, spillover=_plan(free=3), spawn_limit=0, spawn_paused="saturated:cpu")
    assert (port, "ace-ai") in spawned
    assert carried == []
    assert not any(t in (lf1, lf2) for t, _h in spawned)
    assert res.carry_scans_deferred == 2
    assert res.route_ms >= res.carry_scan_ms >= 0


def test_carry_scan_cache_respects_limits(tmp_path):
    d = tmp_path / "limits"
    d.mkdir()
    (d / "work").write_bytes(b"x" * 10)
    kwh._carry_scan_cache.clear()
    assert kwh.carry_size_ok(str(d), max_bytes=100)
    assert not kwh.carry_size_ok(str(d), max_bytes=5)


def test_carry_workspace_runs_mkdir_then_rsync():
    calls = []

    class P:
        returncode, stderr = 0, ""

    def runner(argv, **kw):
        calls.append(argv)
        return P()
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    ws = "/Volumes/fleet-scratch/workspaces/default/t_1"
    kwh.carry_workspace(host, ws + "/", runner=runner)
    assert calls[0][0] == "ssh" and f"mkdir -p -m 0770 -- {ws}" in calls[0][-1]
    assert calls[1][:2] == ["rsync", "-a"]
    assert calls[1][-2:] == [f"{ws}/", f"{host.target}:{ws}/"]


# -- rule 1 / 5: worktree targets ------------------------------------------------------------------------------

def _wt_card(conn, anchor, target_leaf=None, make=False):
    tid = kb.create_task(conn, title="wt", assignee="alpha", workspace_kind="worktree",
                         workspace_path=str(anchor))
    target = anchor / ".worktrees" / (target_leaf or tid)
    if make:
        target.mkdir(parents=True)
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(target), tid))
    conn.commit()
    return tid


def test_a_not_yet_made_worktrees_target_ports(kanban_home, anchor):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
    assert out[tid][0] == SLUG


@pytest.mark.parametrize("leaf,make", [(None, True), ("someone-elses-name", False)])
def test_an_existing_or_foreign_worktrees_target_stays_local(kanban_home, anchor, leaf, make):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor, leaf, make)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
    assert rules[tid] == "workspace_kind"


def test_create_rewrites_a_portable_worktree_target(kanban_home, anchor):
    _mirrors(kanban_home)
    path, note = kwt.mint_rewrite("worktree", str(anchor / ".worktrees" / "fix-lint"), kanban_home / "fleet")
    assert path == str(anchor.resolve()) and note.startswith("workspace rewritten:") and SLUG in note
    path, note = kwt.mint_rewrite("worktree", str(anchor), kanban_home / "fleet")
    assert path == str(anchor) and "portable" in note


def test_create_says_why_a_worktree_stays_local(kanban_home, anchor, monkeypatch):
    target = str(anchor / ".worktrees" / "fix-lint")
    # Derived mode (default since #176, t_1d2c5939): every GitHub repo ports; the retired `exclude` is ignored
    # (t_20fa16c7), so it no longer keeps the card on the Studio.
    (kanban_home / "fleet" / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "exclude": [SLUG]}))
    path, note = kwt.mint_rewrite("worktree", target, kanban_home / "fleet")
    assert path == str(anchor.resolve()) and note.startswith("workspace rewritten:")
    # The hand list (derived mode's kill switch): a repo with no row stays local.
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")
    _mirrors(kanban_home, slug="ANG-Ventures/something-else")
    path, note = kwt.mint_rewrite("worktree", target, kanban_home / "fleet")
    assert path == target and "stays Studio-local" in note and SLUG in note
    assert kwt.mint_rewrite("scratch", None, kanban_home / "fleet") == (None, None)


# -- scope add 2 (Apollo 21:3x): does the CLI one-shot gate zero REMOTE spawns? ------------------------------

def test_a_paused_local_gate_still_places_a_portable_card_remote(kanban_home):
    """What the CLI passes on a saturated Studio (spawn_paused=<reason>, spawn_limit=0) caps LOCAL spawns only:
    with a pool plan that has room, a portable card is placed remote this call."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p", assignee="alpha")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0,
                             spawn_paused="utilization: saturated:cpu -0.10 (binding cpu)")
    assert (tid, "ace-ai") in spawned


# -- item 7: the replay of a window of placement events ----------------------------------------------------------

def test_replay_counts_studio_placements_before_and_after(kanban_home, monkeypatch, anchor):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kanban_studio_pin_replay", Path(__file__).resolve().parents[2] / "scripts" / "kanban_studio_pin_replay.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    kwh._carry_scan_cache.clear()
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        bare = kb.create_task(conn, title="a", assignee="alpha", body="host: studio\nfix it")
        kept = kb.create_task(conn, title="b", assignee="alpha", body="host: studio\nrequires: studio-state")
        lf, _ = _card_with_files(conn)
        wt = _wt_card(conn, anchor)
        remote = kb.create_task(conn, title="r", assignee="alpha")
        for tid, route, rule in ((bare, "local", "host_studio"), (bare, "local", "host_studio"),
                                 (kept, "local", "host_studio"), (lf, "local", "local_files"),
                                 (wt, "local", "workspace_kind"), (remote, "remote-policy", "policy"),
                                 (remote, "local", "default")):
            kb._append_event(conn, tid, "placement", {"route": route, "rule": rule})
        conn.commit()
    res = rp.replay(kanban_home / "kanban.db", 24, kanban_home / "fleet")
    assert res["placements"] == 7 and res["studio_before"] == 6
    assert res["before"] == {"host_studio": 3, "local_files": 1, "workspace_kind": 1, "default": 1}
    assert res["after"] == {"fwa": 3, "host_studio": 1, "studio-while-room": 1, "default": 1}
    assert res["studio_after_pinned"] == 2
    assert res["moved"]["host_studio->fwa"] == [bare]
    assert "host_studio" in rp.render(res)


# -- Prism round 3 (ea59f384dc) ------------------------------------------------------------------------------

def test_a_studio_pin_a_skill_needs_is_kept(kanban_home, monkeypatch):
    """Prism f5bbe97bc028: a bare `host: studio` on a card whose ATTACHED skill needs an identity-only Studio
    capability (docker) is justified. Dropping it would make the card an unpinned docker card: wait forever."""
    cmap = khc.CapabilityMap(capabilities=tuple(sorted(STUDIO_CAPS)),
                             hosts={khc.STUDIO_ROW: STUDIO_CAPS, "ace-ai": frozenset()},
                             index={"docker-ops": ("docker",)}, identity_only=frozenset({"docker"}))
    caps = khc.CapabilityTick(cmap, khc.ENFORCE, ("docker",), ("ace-ai",))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nrun the compose stack",
                             skills=["docker-ops"])
        route, rule, _ = _route(conn, tid, caps)
        bare = kb.create_task(conn, title="y", assignee="alpha", body="host: studio\nfix the lint")
        assert _route(conn, bare, caps)[0] == ("portable", None)  # no such skill: still dropped
    # The skill's need is enforced by the capability verdict first, so the card is held local under that rule.
    assert route == ("local", "studio_affinity:requires:docker") and rule == "requires:docker", (route, rule)
    monkeypatch.setattr(khc, "load_map", lambda fleet: cmap)
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet", ["docker-ops"]) is None
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet") is not None


def test_carried_host_reads_the_persisted_spelling(kanban_home):
    """Prism 34bd0265a601: custody holds when the row now carries the spelling the claim persisted ("path"),
    not the one stored at carry time ("workspace")."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/old/spelling", "path": path})
        conn.commit()
        assert kwh.carried_host(conn, tid, path) == "ace-ai"
        assert kwh.carried_host(conn, tid, "/old/spelling") == "ace-ai"
        assert kwh.carried_host(conn, tid, None) is None


def test_a_shared_workspace_is_never_carried(kanban_home, carried):
    """Prism e25ae3385b2f: a carry copies onto the same path on the pool host, so a directory another card points
    at (or already carried) is not carried; that card keeps the old local_files rule."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        other = kb.create_task(conn, title="o", assignee="alpha")
        kbw.set_workspace_path(conn, other, path)
        assert kwh.carry_path_shared(conn, tid, path)
        # The shared-workspace pin fires before local_files: local, not carried.
        assert _route(conn, tid, None) == (("local", None), f"workspace_shared:{other}", False)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert carried == [] and all(t != tid for t, _h in spawned)


def test_a_workspace_another_card_carried_is_not_carried_again(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        other = kb.create_task(conn, title="o", assignee="alpha")
        kb._append_event(conn, other, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/x", "path": path})
        conn.commit()
        assert kwh.carry_path_shared(conn, tid, path)
        assert not kwh.carry_path_shared(conn, other, "/somewhere/else")
        assert _route(conn, tid, None)[1:] == ("local_files", False)


def test_a_carried_card_waits_when_there_is_no_pool_plan(kanban_home, carried):
    """Prism fd679a4c5702: with no plan this tick the bind is never computed; custody still holds, so the card
    waits pool_unavailable instead of running on the Studio's stale copy."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": path, "path": path})
        plain = kb.create_task(conn, title="p", assignee="alpha")
        conn.commit()
        _res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert (tid, None) not in spawned and (plain, None) in spawned


# -- t_e7c03f48: only content the remote cannot reproduce binds ------------------------------------------------
# t_e32e34f0's Studio scratch dir held a clean git worktree whose HEAD was the pushed PR head, plus logs and a patch;
# that kept it Studio-only (local_files) for 2 h+ while the pool sat idle.

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "-c", "init.defaultBranch=main", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def reproducible_workspace(tmp_path):
    """A workspace dir holding a clean `git worktree` (HEAD pushed) plus three logs, a patch and pr-body.md."""
    getattr(kwh, "_binding_cache", {}).clear()  # absent on main: red-first runs reach the assertion
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "README.md").write_text("x\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "HEAD:main")

    def make(ws: Path) -> Path:
        ws.mkdir(parents=True, exist_ok=True)
        repo = ws / "repo"
        _git(seed, "worktree", "add", "-q", "-b", f"feat-{ws.name}", str(repo), "HEAD")
        (repo / "fix.py").write_text("print(1)\n")
        _git(repo, "add", "fix.py")
        _git(repo, "commit", "-q", "-m", "fix")
        _git(repo, "push", "-q", "origin", "HEAD")
        for name in ("run.log", "test.log", "gate.log", "fix.patch", "pr-body.md"):
            (ws / name).write_text("throwaway\n")
        return repo
    return make


def _card_in(conn, make, body=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha", body=body)
    path = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    repo = make(path)
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, path, repo


def test_clean_pushed_worktree_plus_logs_is_portable(kanban_home, carried, reproducible_workspace):
    """RED ON BASE: ((portable, None), 'local_files', True) - any file bound the card (it was carried at best)."""
    with kb.connect_closing() as conn:
        tid, path, _repo = _card_in(conn, reproducible_workspace)
        assert _route(conn, tid, None) == (("portable", None), "policy", False)
        assert kwh.local_workspace_binding(str(path)) is None


def test_clean_pushed_worktree_is_portable_under_the_kill_switch_too(kanban_home, carried, reproducible_workspace):
    """The fix is in the classifier, not in the continuity carry: the old pin rule no longer sees content."""
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, _path, _repo = _card_in(conn, reproducible_workspace)
        assert _route(conn, tid, None) == (("portable", None), "policy", False)


def test_one_untracked_file_binds_and_is_named(kanban_home, carried, reproducible_workspace):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, path, repo = _card_in(conn, reproducible_workspace)
        (repo / "scratch.txt").write_text("unsaved work\n")
        assert _route(conn, tid, None) == (("local", None), "local_files", False)
        assert kwh.local_workspace_binding(str(path)) == "repo/scratch.txt"


def test_host_any_wait_reason_names_the_binding_path(kanban_home, carried, reproducible_workspace):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, _path, repo = _card_in(conn, reproducible_workspace, body="host: any\nfix it")
        (repo / "scratch.txt").write_text("unsaved work\n")
        route, rule, _ = _route(conn, tid, None)
    assert route == ("local", "not_portable:local_files(repo/scratch.txt)") and rule == "local_files"


def test_dirty_tracked_file_binds(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    (repo / "README.md").write_text("edited\n")
    assert kwh.local_workspace_binding(str(ws)) == "repo/README.md"


def test_unpushed_commit_binds(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    (repo / "more.py").write_text("x\n")
    _git(repo, "add", "more.py")
    _git(repo, "commit", "-q", "-m", "local only")
    assert (kwh.local_workspace_binding(str(ws)) or "").startswith("repo@unpushed:")


@pytest.mark.parametrize("name", ["notes.md", "session.jsonl", "pr-body.md.bak"])
def test_a_file_outside_the_throwaway_list_binds(reproducible_workspace, tmp_path, name):
    ws = tmp_path / "ws"
    reproducible_workspace(ws)
    (ws / name).write_text("x\n")
    assert kwh.local_workspace_binding(str(ws)) == name


def test_a_symlink_binds(tmp_path):
    getattr(kwh, "_binding_cache", {}).clear()  # absent on main: red-first runs reach the assertion
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "run.log").write_text("x\n")
    (ws / "link").symlink_to(tmp_path)
    assert kwh.local_workspace_binding(str(ws)) == "link"


def test_throwaway_only_workspace_is_portable_and_empty_is_none(tmp_path):
    getattr(kwh, "_binding_cache", {}).clear()  # absent on main: red-first runs reach the assertion
    ws = tmp_path / "ws"
    assert kwh.local_workspace_binding(str(ws)) is None          # missing
    ws.mkdir()
    assert kwh.local_workspace_binding(str(ws)) is None          # empty
    (ws / "logs").mkdir()
    (ws / "logs" / "a.log").write_text("x\n")
    (ws / "b.patch").write_text("x\n")
    assert kwh.local_workspace_binding(str(ws)) is None


def test_a_git_failure_binds(tmp_path):
    """A worktree whose gitdir is gone cannot be proven reproducible: fail closed."""
    getattr(kwh, "_binding_cache", {}).clear()  # absent on main: red-first runs reach the assertion
    ws = tmp_path / "ws"
    (ws / "repo").mkdir(parents=True)
    (ws / "repo" / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    assert (kwh.local_workspace_binding(str(ws)) or "").startswith("repo@git-")


# -- t_e7c03f48 Prism round 1: each of these let local-only work read "reproducible" --------------------------------

def test_an_edit_inside_a_checkout_between_two_calls_binds(reproducible_workspace, tmp_path):
    """Prism 5c1daae965e0: a cache keyed on the workspace root mtime missed an edit inside the checkout."""
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    assert kwh.local_workspace_binding(str(ws), now=0) is None
    (repo / "README.md").write_text("edited after the first call\n")
    assert kwh.local_workspace_binding(str(ws), now=1) == "repo/README.md"


def test_a_submodule_edit_hidden_by_ignore_all_binds(reproducible_workspace, tmp_path):
    """Prism 812f89f3c723: ``submodule.<x>.ignore=all`` must not hide a modified tracked file in a submodule."""
    sub_origin, sub_seed = tmp_path / "sub.git", tmp_path / "sub-seed"
    _git(tmp_path, "init", "-q", "--bare", str(sub_origin))
    _git(tmp_path, "clone", "-q", str(sub_origin), str(sub_seed))
    (sub_seed / "lib.py").write_text("x = 1\n")
    _git(sub_seed, "add", "lib.py")
    _git(sub_seed, "commit", "-q", "-m", "lib")
    _git(sub_seed, "push", "-q", "origin", "HEAD:main")
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "sub")
    _git(repo, "commit", "-q", "-m", "add sub")
    _git(repo, "push", "-q", "origin", "HEAD")
    _git(repo, "config", "submodule.sub.ignore", "all")
    assert kwh.local_workspace_binding(str(ws), now=0) is None
    (repo / "sub" / "lib.py").write_text("x = 2  # local only\n")
    kwh._binding_cache.clear()  # isolate the status override from the stale-local-verdict regression
    assert kwh.local_workspace_binding(str(ws), now=1) == "repo/sub"


def test_a_branch_deleted_on_the_remote_binds_despite_the_stale_tracking_ref(reproducible_workspace, tmp_path):
    """Prism 1c3c50990907: the local ``origin/<branch>`` ref outlives the remote branch; ask the remote."""
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    branch = f"feat-{ws.name}"
    _git(tmp_path, "--git-dir", str(tmp_path / "origin.git"), "branch", "-D", branch)
    tracking = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                              capture_output=True)
    assert tracking.returncode == 0  # the stale local tracking ref is still there
    assert (kwh.local_workspace_binding(str(ws), now=0) or "").startswith("repo@unpushed:")


def test_an_ls_remote_failure_binds_as_remote_unverified(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)

    def runner(argv, **kw):
        if "ls-remote" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: unable to access")
        return subprocess.run(argv, **kw)
    assert kwh.local_workspace_binding(str(ws), runner=runner, now=0) == "remote-unverified: repo"
    # Unverified answers may be cached: they only keep a card local. Re-ask after the bounded TTL.
    assert kwh.local_workspace_binding(str(ws), now=1) == "remote-unverified: repo"
    assert kwh.local_workspace_binding(str(ws), now=121) is None
    assert repo.exists()


def test_an_ls_remote_timeout_binds_as_remote_unverified(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    reproducible_workspace(ws)

    def runner(argv, **kw):
        if "ls-remote" in argv:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.run(argv, **kw)
    assert kwh.local_workspace_binding(str(ws), runner=runner, now=0) == "remote-unverified: repo"



def test_remote_containment_cache_is_keyed_by_head_but_status_always_runs(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.run(argv, **kw)

    assert kwh.local_workspace_binding(str(ws), runner=runner, now=0) is None
    assert kwh.local_workspace_binding(str(ws), runner=runner, now=1) is None
    assert sum("status" in a for a in calls) == 2
    assert sum("ls-remote" in a for a in calls) == 2
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local only")
    assert (kwh.local_workspace_binding(str(ws), runner=runner, now=2) or "").startswith("repo@unpushed:")
    assert sum("ls-remote" in a for a in calls) == 3
    assert (kwh.local_workspace_binding(str(ws), runner=runner, now=3) or "").startswith("repo@unpushed:")
    assert sum("ls-remote" in a for a in calls) == 3  # negative answer is safe to cache


def test_remote_containment_accepts_ancestor_of_a_current_remote_tip(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    _git(repo, "push", "-q", "origin", "HEAD")
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    assert kwh.local_workspace_binding(str(ws), now=0) is None


def test_remote_tip_not_present_locally_binds_as_unverified(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    _git(other, "checkout", "-q", "feat-ws")
    _git(other, "commit", "-q", "--allow-empty", "-m", "tip not fetched by workspace")
    _git(other, "push", "-q", "origin", "HEAD")
    assert kwh.local_workspace_binding(str(ws), now=0) == "remote-unverified: repo"
    assert repo.exists()


def test_remote_containment_rechecks_positive_after_deleted_remote_branch(reproducible_workspace, tmp_path):
    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    assert kwh.local_workspace_binding(str(ws), now=0) is None
    _git(tmp_path, "--git-dir", str(tmp_path / "origin.git"), "branch", "-D", "feat-ws")
    assert (kwh.local_workspace_binding(str(ws), now=1) or "").startswith("repo@unpushed:")
    assert repo.exists()


@pytest.mark.parametrize("source", ["fsmonitor", "hooks", "inherited-config"])
def test_workspace_git_probes_do_not_execute_checkout_config(reproducible_workspace, tmp_path, monkeypatch, source):
    """Prism 4d0629af60fb: dispatcher probes must not execute checkout-controlled programs."""
    import os
    import shlex

    ws = tmp_path / "ws"
    repo = reproducible_workspace(ws)
    marker = tmp_path / "executed"
    program = tmp_path / "marker-command"
    program.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    program.chmod(0o755)
    if source == "fsmonitor":
        _git(repo, "config", "core.fsmonitor", str(program))
    elif source == "hooks":
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(program.read_text())
        hook.chmod(0o755)
        _git(repo, "config", "core.hooksPath", str(hooks))
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(program))

    environments = []

    def runner(argv, **kw):
        environments.append(kw["env"])
        return subprocess.run(argv, **kw)

    assert kwh.local_workspace_binding(str(ws), runner=runner, now=0) is None
    assert not marker.exists(), f"{source} executed as the dispatcher"
    # Read-only probes do not trigger reference-transaction themselves; also pin the hooks override on
    # EVERY subprocess so the test catches an unhardened hooks path without introducing a write probe.
    for env in environments:
        overrides = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                     for i in range(int(env.get("GIT_CONFIG_COUNT", "0")))}
        assert overrides.get("core.hooksPath") == os.devnull
        assert overrides.get("core.fsmonitor") == "false"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
