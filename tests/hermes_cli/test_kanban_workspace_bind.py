"""Traceability-vs-Placement spec v1.2, Phase 2b-i (card t_6d5bc652).

Placement stops reading links (``lineage_pins: none``); the constraints are
``workspace_of`` (explicit) and ``workspace_shared`` (canonical path, nesting
binds), co-located via ``bind_host``. Retention keeps its link key and unions
the new one under ``kanban.workspace.retention_key`` (D-S3), with the
``retention_shadow`` event where the keys disagree. Every deletion here runs
through the REAL ``safe_remove_workspace_dir`` (Momus pass 3 R-1: no mock of
the choke point). AC numbers are the spec's §10.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_placement_reasons as kpr
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture(autouse=True)
def _scan_ignores_real_home_cwds(monkeypatch):
    """Same seal as test_kanban_workspace_retention: the machine-wide cwd scan
    must not stat live fleet cwds under the real home."""
    from tests import conftest
    real = conftest._REAL_HERMES_ROOT_CANDIDATES
    original = kb._scan_process_cwds

    def scan():
        cwds = original()
        if cwds is None:
            return None
        kept = frozenset(c for c in cwds if not any(c == r or r in c.parents for r in real))
        return kept or None
    monkeypatch.setattr(kb, "_scan_process_cwds", scan)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path))  # ``~`` in a stored path expands via $HOME
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (h / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    getattr(kpr, "_unknown_switch_warned", {}).clear()  # per-test WARN throttle
    return h


def _cfg(monkeypatch, *, lineage=None, retention=None):
    from hermes_cli import config as _config
    k: dict = {}
    if lineage is not None:
        k["placement"] = {"lineage_pins": lineage}
    if retention is not None:
        k["workspace"] = {"retention_key": retention}
    monkeypatch.setattr(_config, "load_config_readonly", lambda: {"kanban": k})


def _host(name="ace-ai", slots=4):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=0)


def _plan(free=4, hosts=None):
    hosts = hosts or [_host("ace-ai", free), _host("ci-box", free)]
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16))
    p.band, p.spill_reason = "spilling", "load"
    return p


def _set(conn, tid, **cols):
    keys = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*cols.values(), tid))
    conn.commit()


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def _route(conn, tid, plan=None):
    return kbd._route_row(conn, _row(conn, tid), plan or _plan())


def _placed_on(conn, tid, host):
    """A current run on ``host`` with its worker_placed event (the record bind_host reads)."""
    now = int(time.time())
    cur = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                       "VALUES (?, 'alpha', 'running', ?)", (tid, now))
    run_id = cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, tid))
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kpr.WORKER_PLACED_EVENT, {"host": host}, run_id=run_id)


def _scratch(conn, tid, *, content=True) -> Path:
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / tid
    ws.mkdir(exist_ok=True)
    if content:
        (ws / "handoff.txt").write_text("parent output\n", encoding="utf-8")
    _set(conn, tid, workspace_kind="scratch", workspace_path=str(ws))
    return ws


def _shadows(conn, tid):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kbw.RETENTION_SHADOW_EVENT))]


def _mk(conn, title, **kw):
    kw.setdefault("assignee", "alpha")
    return kb.create_task(conn, title=title, **kw)


# -- AC-1: lineage never pins under ``none`` -------------------------------

def test_ac1_linked_children_and_parents_route_remote_policy_under_none(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        p1 = _mk(conn, "parent of derived")
        d = _mk(conn, "derived child", parents=[p1], parents_kind="derived-from")
        p2 = _mk(conn, "parent of blocks")
        b = _mk(conn, "blocks child, distinct path", body="host: any", parents=[p2])
        _set(conn, b, status="ready")
        got = {t: _route(conn, t) for t in (p1, d, p2, b)}
    assert got == {p1: ("portable", None), d: ("portable", None),
                   p2: ("portable", None), b: ("portable", None)}


def test_ac1_dispatch_spawns_the_blocks_child_on_the_pool(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        p = _mk(conn, "parent")
        b = _mk(conn, "blocks child", body="host: any", parents=[p])
        _set(conn, p, status="done")
        _set(conn, b, status="ready")
        spawned = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(
            (t.id, k["placement"].name if k.get("placement") else None)) or 1,
            max_spawn=8, spawn_paused="test", spawn_limit=0, spillover=_plan(),
            reconcile_orphans=False)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='placement'",
                          (b,)).fetchone()
    assert any(t == b and h for t, h in spawned), (spawned, res.placement_waits)
    assert json.loads(ev[0])["route"] == "remote-policy"


# -- AC-3: workspace_shared on the canonical path, bind_host co-locates ------

def test_ac3_equal_canonical_path_binds_local_then_to_the_placed_host(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        a = _mk(conn, "A")
        b = _mk(conn, "B")
        ws = _scratch(conn, a, content=False)
        _set(conn, b, workspace_kind="scratch", workspace_path=str(ws), status="ready")
        assert _route(conn, b) == ("local", None)
        rules: dict = {}
        kbd._route_row(conn, _row(conn, b), _plan(), rules_out=rules)
        assert rules[b] == f"workspace_shared:{a}"
        _placed_on(conn, a, "ci-box")
        rules = {}
        assert kbd._route_row(conn, _row(conn, b), _plan(), rules_out=rules) == ("pin", "ci-box")
        assert rules[b] == f"bind_host:ci-box(workspace_shared:{a})"


# -- AC-4 (WEAK, declared): workspace_of pins; dropped when the path is gone --

def test_ac4_workspace_of_pins_to_bind_host_and_drops_when_terminal_and_gone(home, monkeypatch):
    """WEAK by declaration (spec §10): the remote path is not probed, so this
    proves the pin, not that the state is actually on that host."""
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        # X ran on ace-ai: its files are there; the Studio copy of the path is
        # empty (P0-4 per-host trees). Local content would hold it local_files.
        ws = _scratch(conn, x, content=False)
        _placed_on(conn, x, "ace-ai")
        c = _mk(conn, "reader", workspace_of=x)
        _set(conn, c, status="ready")
        assert _route(conn, c) == ("pin", "ace-ai")
        _set(conn, x, status="done")
        ws.rmdir()
        # Terminal, and the Studio has no copy of the path: X's directory is
        # on ace-ai, which the dispatcher cannot see. Never decided by a local
        # isdir (Prism 81c519d969ef / bcd043ee8789): the bind holds.
        assert _route(conn, c) == ("pin", "ace-ai")
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                            (c, kpr.PLACEMENT_BIND_DROPPED_EVENT)).fetchone()[0] == 0


def test_ac4_local_target_terminal_and_gone_drops_the_bind_once(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        ws = _scratch(conn, x, content=False)  # X ran here (no worker_placed)
        c = _mk(conn, "reader", workspace_of=x)
        _set(conn, c, status="ready")
        assert _route(conn, c) == ("local", None)
        _set(conn, x, status="done")
        ws.rmdir()
        assert _route(conn, c) == ("portable", None)
        kbd._workspace_binds(conn, [_row(conn, c)])  # second tick: no second event
        evs = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                           (c, kpr.PLACEMENT_BIND_DROPPED_EVENT)).fetchall()
    assert [json.loads(e[0]) for e in evs] == [{"of": x, "why": "path_gone"}]


# -- Apollo r1 on #2032: host-aware binds (one resolver, resolve_bind) -------

def test_r1_workspace_of_chain_resolves_to_the_owner_host(home, monkeypatch):
    """Prism 6913ec20b7a9: R1 borrows X (ran on ci-box) and never ran; R2
    borrows R1. R2 runs where X's directory is, not locally."""
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        _scratch(conn, x, content=False)
        _placed_on(conn, x, "ci-box")
        r1 = _mk(conn, "R1", workspace_of=x)
        r2 = _mk(conn, "R2", workspace_of=r1)
        _set(conn, r2, status="ready")
        assert _route(conn, r2) == ("pin", "ci-box")
        assert kbw.resolve_bind(conn, r1) == (x, "ci-box")


def test_r1_workspace_of_cycle_terminates(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        a = _mk(conn, "A")
        b = _mk(conn, "B")
        ws = _scratch(conn, a, content=False)
        _set(conn, b, workspace_kind="scratch", workspace_path=str(ws))
        _set(conn, a, workspace_of=b)
        _set(conn, b, workspace_of=a)
        assert kbw.resolve_bind(conn, a)[1] is None
        _placed_on(conn, b, "ace-ai")
        assert kbw.resolve_bind(conn, a)[1] == "ace-ai"


def test_r1_workspace_shared_binds_to_the_placed_owner_not_an_unrun_reader(home, monkeypatch):
    """Prism aabb9c4f4a2f: the sorted-first sharer is an unrun reader; the
    owner ran on ci-box. The new sharer binds to ci-box."""
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        a, b = sorted([_mk(conn, "a"), _mk(conn, "b")])
        ws = _scratch(conn, b, content=False)
        _placed_on(conn, b, "ci-box")
        _set(conn, a, workspace_kind="scratch", workspace_path=str(ws), workspace_of=b)
        s = _mk(conn, "S")
        _set(conn, s, workspace_kind="scratch", workspace_path=str(ws), status="ready")
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, s), _plan(), rules_out=rules) == ("pin", "ci-box")
        # Same with a plain unrun sharer (no workspace_of) sorting first.
        _set(conn, a, workspace_of=None)
        assert _route(conn, s) == ("pin", "ci-box")


@pytest.mark.parametrize("lineage", ["none", "kinds"])
def test_r1_remote_bind_beats_local_only_refusals(home, monkeypatch, lineage):
    """Prism f399797453ae: a card bound to a ci-box directory is routed there
    even with Studio-side content at the same path and a blocks parent."""
    _cfg(monkeypatch, lineage=lineage)
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        _scratch(conn, x, content=True)  # Studio residue at the same path
        _placed_on(conn, x, "ci-box")
        r = _mk(conn, "R", workspace_of=x, parents=[x])
        _set(conn, r, status="ready")
        assert _route(conn, r) == ("pin", "ci-box")


def test_r1_portable_remote_bind_ignores_studio_content_and_links():
    """portable() on its own: a remote bind is a pin to the bind host even
    with Studio content at the path or a lineage link; a local bind is not."""
    pool = _plan().config
    bind = ("workspace_of:t_x", "t_x", "ci-box")
    for kw in ({"workspace_has_content": True}, {"workspace_has_content": False, "has_links": True}):
        assert kwp.portable(workspace_kind="scratch", assignee="alpha", body=None, pool=pool,
                            workspace_bind=bind, **kw) == (True, None, "pin")
    assert kwp.portable(workspace_kind="scratch", assignee="alpha", body=None, pool=pool,
                        workspace_has_content=True,
                        workspace_bind=("workspace_of:t_x", "t_x", None))[:2] == (
        False, "workspace_of:t_x")


def test_r1_remote_bind_that_cannot_run_there_waits_never_local(home, monkeypatch):
    """A remote-bound card a pool host cannot take (profile not allowlisted)
    waits on the rule; it never runs on the Studio against an empty path."""
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        _scratch(conn, x, content=False)
        _placed_on(conn, x, "ci-box")
        r = _mk(conn, "R", workspace_of=x, assignee="not-in-pool")
        _set(conn, r, status="ready")
        assert _route(conn, r) == ("wait", "not_portable:profile")


def _remote_rm(monkeypatch):
    calls: list = []

    class _P:
        returncode, stdout, stderr = 0, "removed\n", ""

    def run(argv, **kw):
        calls.append(argv)
        return _P()
    from hermes_cli import kanban_survivor as ks
    monkeypatch.setattr(ks.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("child", ["reader", "blocks"])
def test_r1_remote_removal_applies_the_retention_guard(home, monkeypatch, child):
    """Prism 4ea5f2217fd9: the REAL choke point with a remote_host (gc's
    path): owner terminal on ace-ai, a ready child => no ssh removal. The
    link-only child writes the retention_shadow the local branch writes."""
    _cfg(monkeypatch, retention="union")
    calls = _remote_rm(monkeypatch)
    with kb.connect_closing() as conn:
        p, c, ws = _p_with_child(conn, **({"reader": True} if child == "reader"
                                          else {"link_kind": "blocks"}))
        _set(conn, p, status="done")
        assert kb.safe_remove_workspace_dir(ws, task_id=p, reason="gc_archived", conn=conn,
                                            remote_host=_host("ace-ai")) is False
        assert calls == []
        assert _shadows(conn, p) == ([] if child == "reader" else
                                     [{"parent": p, "deferred_by": [[c, "blocks"]],
                                       "new_key_would": "delete"}])
        _set(conn, c, status="done")
        assert kb.safe_remove_workspace_dir(ws, task_id=p, reason="gc_archived", conn=conn,
                                            remote_host=_host("ace-ai")) is True
    assert len(calls) == 1


def test_r1_workspace_of_a_worktree_card_is_refused(home):
    """Prism 585c20849c68: the copy carries no branch_name, so a worktree
    reader would get its own wt/<id>. --workspace-of is scratch/dir only."""
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        _set(conn, x, workspace_kind="worktree", workspace_path=str(home / "repo" / ".worktrees" / x),
             branch_name=f"wt/{x}")
        with pytest.raises(ValueError, match="worktree"):
            _mk(conn, "c", workspace_of=x)


def test_workspace_of_body_pin_contradicting_the_bind_holds_the_card(home, monkeypatch):
    _cfg(monkeypatch, lineage="none")
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        _scratch(conn, x, content=False)
        c = _mk(conn, "reader", workspace_of=x)
        _placed_on(conn, x, "ace-ai")
        _set(conn, c, status="ready", body="host: ci-box")
        assert _route(conn, c) == ("wait", f"not_portable:workspace_of:{x}")


# -- AC-13: --workspace-of refusals and the copy -----------------------------

def test_ac13_workspace_of_refusals_name_the_fix_and_the_happy_path_copies(home, monkeypatch):
    fleet = home / "kanban" / "fleet"
    monkeypatch.setattr(kwp, "known_host_ids", lambda d: ("ci-box", "ace-media", "ace-ai"))
    monkeypatch.setattr(kwp, "pin_aliases", lambda d: {})
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="no such task"):
            _mk(conn, "c", workspace_of="t_deadbeef")
        bare = _mk(conn, "never ran")
        with pytest.raises(ValueError, match="no workspace path yet"):
            _mk(conn, "c", workspace_of=bare)
        x = _mk(conn, "X")
        ws = _scratch(conn, x)
        with pytest.raises(ValueError, match="drop the path"):
            _mk(conn, "c", workspace_of=x, workspace_path=str(ws.parent / "other"))
        with pytest.raises(ValueError, match="drop --workspace"):
            _mk(conn, "c", workspace_of=x, workspace_kind="dir")
        _placed_on(conn, x, "ci-box")
        with pytest.raises(ValueError, match="remove the host: line"):
            _mk(conn, "c", workspace_of=x, body="host: ace-media")
        ok = _mk(conn, "c", workspace_of=x, body="host: ci-box",
                 workspace_path=str(ws).replace(str(Path.home()), "~"))
        row = _row(conn, ok)
    assert (row["workspace_kind"], row["workspace_path"], row["workspace_of"]) == (
        "scratch", str(ws), x)
    del fleet


def test_ac13_kanban_create_tool_accepts_workspace_of(home):
    from tools import kanban_tools as kt
    with kb.connect_closing() as conn:
        x = _mk(conn, "X")
        ws = _scratch(conn, x)
    out = json.loads(kt._handle_create({"board": "default", "title": "reader", "assignee": "alpha",
                                        "workspace_of": x, "unhomed": True}))
    tid = out.get("task_id") or out.get("id")
    with kb.connect_closing() as conn:
        assert _row(conn, tid)["workspace_of"] == x
        assert _row(conn, tid)["workspace_path"] == str(ws)
    bad = json.loads(kt._handle_create({"board": "default", "title": "r2", "assignee": "alpha",
                                        "workspace_of": "t_nope", "unhomed": True}))
    assert "no such task" in json.dumps(bad)


# -- AC-14: kinds vs none differ only on linked rows -------------------------

def test_ac14_kinds_and_none_differ_only_on_linked_rows(home, monkeypatch):
    with kb.connect_closing() as conn:
        umb = _mk(conn, "umbrella", body="origin: discord")
        rows = [umb]
        for i, (body, kind) in enumerate([("host: any", "derived-from"), ("host: ace-ai", "derived-from"),
                                          (None, "blocks"), ("host: ace-ai", "blocks"),
                                          ("host: any", "blocks")]):
            rows.append(_mk(conn, f"child {i}", body=body, parents=[umb], parents_kind=kind))
        lone = [_mk(conn, f"lone {i}", body=b) for i, b in enumerate(
            [None, "host: ace-ai", "host: any", "host: studio"])]
        conn.execute("UPDATE tasks SET status='ready'")
        conn.commit()
        linked = set(kbd._linked_ids(conn, rows + lone, "legacy"))
        tables = {}
        for mode in ("kinds", "none"):
            _cfg(monkeypatch, lineage=mode)
            tables[mode] = {t: _route(conn, t) for t in rows + lone}
    differ = {t for t in rows + lone if tables["kinds"][t] != tables["none"][t]}
    assert differ and differ <= linked
    assert all(tables["kinds"][t] == tables["none"][t] for t in lone)


# -- AC-15: decompose backfill ------------------------------------------------

def test_ac15_backfill_only_nonterminal_equal_path_decompose_children(home):
    with kb.connect_closing() as conn:
        root = _mk(conn, "root")
        ws = _scratch(conn, root, content=False)
        kids = {}
        for name, status, path in (("eq", "ready", str(ws)), ("tilde", "todo",
                                    str(ws).replace(str(Path.home()), "~")),
                                   ("done", "done", str(ws)), ("other", "ready", str(ws) + "-x")):
            kids[name] = _mk(conn, name)
            _set(conn, kids[name], status=status, workspace_path=path)
            with kb.write_txn(conn):
                kb._append_event(conn, kids[name], "created", {"from_decompose_of": root})
        plain = _mk(conn, "plain same path, not decompose")
        _set(conn, plain, workspace_path=str(ws))
        first = kbc.backfill_workspace_of(conn)
        second = kbc.backfill_workspace_of(conn)
        got = {n: _row(conn, t)["workspace_of"] for n, t in kids.items()}
        assert _row(conn, plain)["workspace_of"] is None
    assert (first, second) == (2, 0)
    assert got == {"eq": root, "tilde": root, "done": None, "other": None}


def test_ac15_migration_adds_column_index_and_runs_backfill_on_a_legacy_db(home):
    with kb.connect_closing() as conn:
        root = _mk(conn, "root")
        ws = _scratch(conn, root, content=False)
        kid = _mk(conn, "decompose child")
        _set(conn, kid, status="ready", workspace_path=str(ws))
        with kb.write_txn(conn):
            kb._append_event(conn, kid, "created", {"from_decompose_of": root})
        conn.execute("DROP INDEX IF EXISTS idx_tasks_workspace_of")
        conn.execute("ALTER TABLE tasks DROP COLUMN workspace_of")
        conn.commit()
        assert "workspace_of" not in kbc._column_names(conn, "tasks")
        kbc._migrate_add_optional_columns(conn)
        conn.commit()
        idx = {r[1] for r in conn.execute("PRAGMA index_list(tasks)")}
        assert "idx_tasks_workspace_of" in idx
        assert _row(conn, kid)["workspace_of"] == root


# -- AC-20: canonical key, nesting, the R-E restriction ----------------------

def test_ac20_canonical_spelling_and_nesting_bind_siblings_do_not(home):
    with kb.connect_closing() as conn:
        b = _mk(conn, "B spawned")
        ws = _scratch(conn, b, content=False)
        tilde = str(ws).replace(str(Path.home()), "~")
        assert tilde.startswith("~")
        a, c, d = _mk(conn, "A"), _mk(conn, "C"), _mk(conn, "D")
        _set(conn, a, workspace_path=tilde)
        _set(conn, c, workspace_path=str(ws / "sub"))
        _set(conn, d, workspace_path=str(ws) + "2")
        shared = {t: kbw.workspace_shared(conn, t, _row(conn, t)["workspace_path"], role="reader")
                  for t in (a, c, d)}
    assert shared[a][:1] == [b] or b in shared[a]
    assert b in shared[c]
    assert b not in shared[d]


def test_ac20_r_e_nesting_never_binds_against_a_dir_ancestor(home):
    """Momus R-E: a ``dir:`` card on the workspaces root (or the kanban home)
    is not an owner a scratch card nests under."""
    with kb.connect_closing() as conn:
        broad = _mk(conn, "dir card on the workspaces root")
        root = kb.workspaces_root()
        root.mkdir(parents=True, exist_ok=True)
        _set(conn, broad, workspace_kind="dir", workspace_path=str(root))
        home_dir = _mk(conn, "dir card on the kanban home")
        _set(conn, home_dir, workspace_kind="dir", workspace_path=str(home))
        s = _mk(conn, "scratch card")
        ws = _scratch(conn, s, content=False)
        assert kbw.workspace_shared(conn, s, str(ws), role="reader") == []


def test_ac20_local_workspace_has_content_scans_the_canonical_path(home):
    with kb.connect_closing() as conn:
        t = _mk(conn, "t")
        ws = _scratch(conn, t)
    tilde = str(ws).replace(str(Path.home()), "~")
    assert kwh.local_workspace_has_content(tilde) is True


# -- AC-21: lineage_pins parse correction ------------------------------------

def test_ac21_unknown_lineage_pins_reads_legacy_with_one_warn(home, monkeypatch, caplog):
    with kb.connect_closing() as conn:
        p = _mk(conn, "parent")
        d = _mk(conn, "derived", body="host: any", parents=[p], parents_kind="derived-from")
        tables = {}
        for mode in ("legacy", "kinds", "none", "knids"):
            _cfg(monkeypatch, lineage=mode)
            with caplog.at_level(logging.WARNING):
                tables[mode] = _route(conn, d)
                _route(conn, d)
    assert tables["knids"] == tables["legacy"] == ("local", "not_portable:linked")
    assert tables["kinds"] == tables["none"] == ("portable", None)
    warns = [r for r in caplog.records if "lineage_pins" in r.getMessage()]
    assert len(warns) == 1


# -- AC-22 (rewritten v1.2) + Momus pass 3 R-1 arms ---------------------------

def _p_with_child(conn, *, link_kind=None, reader=False):
    p = _mk(conn, "parent P")
    ws = _scratch(conn, p)
    kw = {"workspace_of": p} if reader else {}
    if link_kind:
        kw.update(parents=[p], parents_kind=link_kind)
    c = _mk(conn, "child", **kw)
    _set(conn, c, status="ready")
    return p, c, ws


def test_ac22_blocks_child_retains_with_shadow_then_removes(home, monkeypatch):
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p, c, ws = _p_with_child(conn, link_kind="blocks")
        _set(conn, p, status="done")
        kbw._cleanup_workspace(conn, p)
        assert ws.is_dir()
        assert _shadows(conn, p) == [{"parent": p, "deferred_by": [[c, "blocks"]],
                                      "new_key_would": "delete"}]
        _set(conn, c, status="done")
        kbw._cleanup_workspace(conn, c)
    assert not ws.exists()


def test_ac22_derived_from_child_alone_retains_with_shadow(home, monkeypatch):
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p, c, ws = _p_with_child(conn, link_kind="derived-from")
        _set(conn, p, status="done")
        kbw._cleanup_workspace(conn, p)
        assert ws.is_dir()
        assert _shadows(conn, p)[0]["deferred_by"] == [[c, "derived-from"]]


def test_ac22_workspace_of_reader_retains_without_shadow_then_removes(home, monkeypatch):
    """R-1 (b): P and D terminal => removed through the REAL choke point
    (``_live_owners_of_path`` unmocked: D's copied path is a reader row)."""
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p, d, ws = _p_with_child(conn, reader=True)
        _set(conn, p, status="done")
        kbw._cleanup_workspace(conn, p)
        assert ws.is_dir() and _shadows(conn, p) == []
        _set(conn, d, status="done")
        kbw._cleanup_workspace(conn, d)
    assert not ws.exists()


@pytest.mark.parametrize("key", ["union", "links"])
def test_ac22_r1a_reader_completing_while_parent_blocked_keeps_the_dir(home, monkeypatch, key):
    """R-1 (a): D completes while P is blocked (P waits on D) => retained,
    under every retention key (under ``links`` the reader rule is the only
    guard: P is the sole owner and is not live)."""
    _cfg(monkeypatch, retention=key)
    with kb.connect_closing() as conn:
        p, d, ws = _p_with_child(conn, reader=True)
        _set(conn, p, status="blocked")
        _set(conn, d, status="done")
        kbw._cleanup_workspace(conn, d)
        assert kb.safe_remove_workspace_dir(ws, task_id=d, reason="t", conn=conn) is False
    assert (ws / "handoff.txt").exists()


def test_ac22_reader_with_a_reset_path_still_defers_by_workspace_of(home, monkeypatch):
    """The workspace_of arm on its own: D's path was cleared (``kanban
    workspace reset``), so no path sharing is left; D still defers P."""
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p, d, ws = _p_with_child(conn, reader=True)
        _set(conn, d, workspace_path=None)
        _set(conn, p, status="done")
        kbw._cleanup_workspace(conn, p)
        assert ws.is_dir() and _shadows(conn, p) == []
        _set(conn, d, status="done")
        kbw._cleanup_workspace(conn, d)
    assert not ws.exists()


def test_ac22_pre_deploy_deferred_parent_is_found_from_the_link(home, monkeypatch):
    """N1 2(ii): P already terminal and deferred on main (no shadow, no
    workspace_of anywhere); E completes after the deploy => P removed."""
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p, e, ws = _p_with_child(conn, link_kind="blocks")
        _set(conn, p, status="done")
        _set(conn, e, status="done")
        kbw._cleanup_workspace(conn, e)
    assert not ws.exists()


def test_ac22_links_mode_is_main_and_ignores_workspace_of(home, monkeypatch):
    _cfg(monkeypatch, retention="links")
    with kb.connect_closing() as conn:
        p, d, ws = _p_with_child(conn, reader=True)
        assert kbw._has_active_children(conn, p) is False
        p2, c2, _ws2 = _p_with_child(conn, link_kind="blocks")
        assert kbw._has_active_children(conn, p2) is True


# -- AC-24: shadow is a superset proof, once per episode ---------------------

def test_ac24_union_superset_of_links_and_shadow_once_per_episode(home, monkeypatch):
    with kb.connect_closing() as conn:
        parents, readers = [], 0
        for i in range(50):
            p = _mk(conn, f"P{i}")
            _scratch(conn, p, content=False)
            parents.append(p)
            if i % 3 == 0:
                _mk(conn, f"blk{i}", parents=[p])
            if i % 4 == 1:
                _mk(conn, f"der{i}", parents=[p], parents_kind="derived-from")
            if i % 10 == 2 and readers < 5:
                _mk(conn, f"rd{i}", workspace_of=p, parents=[p])
                readers += 1
        verdict = {k: {p: kbw._has_active_children(conn, p, k) for p in parents}
                   for k in kbw.RETENTION_KEYS}
        assert all(verdict["union"][p] for p in parents if verdict["links"][p])
        disagree = [p for p in parents if verdict["links"][p] and not verdict["workspace_of"][p]]
        assert readers == 5 and disagree
        _cfg(monkeypatch, retention="union")
        for _ in range(2):
            for p in parents:
                kbw._retention_defers(conn, p)
        count = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind=?",
                             (kbw.RETENTION_SHADOW_EVENT,)).fetchone()[0]
        assert count == len(disagree)
        child = conn.execute("SELECT child_id FROM task_links WHERE parent_id=?",
                             (disagree[0],)).fetchone()[0]
        with kb.write_txn(conn):
            kb._append_event(conn, child, "claimed", {"run_id": 1})
        kbw._retention_defers(conn, disagree[0])
        assert len(_shadows(conn, disagree[0])) == 2
        kbw._retention_defers(conn, disagree[0])
        assert len(_shadows(conn, disagree[0])) == 2  # no new boundary: no new event
        # A changed deferring set is a new episode (Momus pass 3 answer 2).
        _mk(conn, "late child", parents=[disagree[1]])
        kbw._retention_defers(conn, disagree[1])
        assert len(_shadows(conn, disagree[1])) == 2


# -- AC-25: retention_key parse, throttle, independence ----------------------

def test_ac25_retention_key_unknown_is_union_and_warns_once(home, monkeypatch, caplog):
    _cfg(monkeypatch, retention="unoin")
    with kb.connect_closing() as conn:
        p, _c, ws = _p_with_child(conn, link_kind="blocks")
        with caplog.at_level(logging.WARNING):
            for _ in range(1000):
                assert kbw.retention_key() == "union"
        _set(conn, p, status="done")
        kbw._cleanup_workspace(conn, p)
    assert ws.is_dir()
    assert len([r for r in caplog.records if "retention_key" in r.getMessage()]) == 1


def test_ac25_lineage_pins_and_retention_key_are_independent(home, monkeypatch):
    _cfg(monkeypatch, lineage="legacy", retention="workspace_of")
    with kb.connect_closing() as conn:
        p, c, ws = _p_with_child(conn, link_kind="blocks")
        rules: dict = {}
        kbd._route_row(conn, _row(conn, c), _plan(), rules_out=rules)
        assert rules[c] == "linked"
        assert kbw._has_active_children(conn, p) is False
    assert {k: kbw.retention_key({"workspace": {"retention_key": k}})
            for k in kbw.RETENTION_KEYS} == {k: k for k in kbw.RETENTION_KEYS}


# -- Momus pass 3 R-2 (ii): any error in the predicate defers -----------------

def test_r2_canon_oserror_defers_never_raises(home, monkeypatch):
    _cfg(monkeypatch, retention="union")
    with kb.connect_closing() as conn:
        p = _mk(conn, "P")
        ws = _scratch(conn, p)
        _set(conn, p, status="done")

        def boom(path):
            raise OSError("dead mount")
        monkeypatch.setattr(kbw, "canon", boom)
        assert kbw._has_active_children(conn, p) is True
        assert kb.safe_remove_workspace_dir(ws, task_id=p, reason="t", conn=conn) is False
    assert ws.is_dir()
