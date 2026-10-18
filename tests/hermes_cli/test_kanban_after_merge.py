"""``after-merge`` link kind + the ``blocks`` close-condition deadlock lint.

t_a0afd4d7 (2026-10-16): two children were linked to their parent with
``--kind blocks`` to mean "start after the PR merges". ``blocks`` means "start
after the parent is DONE", and the parent's close condition WAS those children,
so neither could ever start. Pinned here, on a real sqlite board:

* ``blocks`` still gates on the parent closing (negative control);
* ``after-merge`` gates until the parent's recorded PR reads MERGED, then the
  dispatcher's release + recompute promotes the child while the parent is open;
* an OPEN or unreadable PR keeps the gate;
* ``link --kind blocks`` is refused when the parent's body names the child,
  with a fix text naming ``after-merge``; the CLI prints the gate in words.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_after_merge as am
from hermes_cli import kanban_db as kb

PR = "https://github.com/ANG-Ventures/hermes-agent/pull/4242"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # A stubbed PR oracle may write only to a positively-declared sandbox.
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    am._CACHE.clear()
    am._LAST_CHECKED.clear()
    kb.init_db()
    return home


def _oracle(state: str | None):
    calls: list[tuple[str, int]] = []

    def q(repo: str, number: int):
        calls.append((repo, number))
        return None if state is None else {"state": state}

    q.calls = calls
    return q


def _parent_with_pr(conn, pr: str = PR) -> str:
    """A parent whose run recorded its PR and that is now in ``review`` (open)."""
    parent = kb.create_task(conn, title="ship the fix", assignee="daedalus")
    kb.claim_task(conn, parent)
    run_id = kb.get_task(conn, parent).current_run_id
    conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                 (f'{{"pr_url": "{pr}"}}', run_id))
    conn.execute("UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = ?", (parent,))
    conn.commit()
    return parent


def test_blocks_edge_gates_until_parent_done(kanban_home):
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="residue", assignee="daedalus")
        assert kb.link_tasks(conn, parent, child, kind="blocks") is True
        assert am.release_merged_edges(conn, query_fn=_oracle("MERGED")) == []
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None


def test_after_merge_releases_on_merged_pr_while_parent_open(kanban_home):
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="acceptance drill", assignee="daedalus")
        assert kb.link_tasks(conn, parent, child, kind="after-merge") is True
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None  # claim-time gate honours it

        released = am.release_merged_edges(conn, query_fn=_oracle("MERGED"))
        assert released == [(parent, child)]
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, parent).status == "review"
        assert kb.claim_task(conn, child) is not None
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (child,))]
        assert "after_merge_released" in kinds


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", None])
def test_after_merge_holds_while_pr_not_merged(kanban_home, state):
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        q = _oracle(state)
        assert am.release_merged_edges(conn, query_fn=q) == []
        assert q.calls == [("ANG-Ventures/hermes-agent", 4242)]
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


def test_lookup_budget_rotates_across_parents(kanban_home):
    """One lookup per tick still reaches every parent: oldest-checked first."""
    with kb.connect() as conn:
        pairs = []
        for n in (1, 2, 3):
            parent = _parent_with_pr(conn, pr=f"{PR[:-4]}{n}")
            child = kb.create_task(conn, title=f"drill {n}", assignee="daedalus")
            kb.link_tasks(conn, parent, child, kind="after-merge")
            pairs.append((parent, child))
        def q(repo, number):  # parent 1's PR stays OPEN and is re-read each tick
            return {"state": "OPEN" if str(number).endswith("1") else "MERGED"}

        released = []
        for tick in range(3):  # ticks past the TTL, so OPEN is never a cache hit
            released += am.release_merged_edges(
                conn, query_fn=q, max_lookups=1, now=1000.0 + tick * 1000)
        assert sorted(released) == sorted(pairs[1:])


def test_release_skips_parent_whose_pr_set_changed_mid_tick(kanban_home, monkeypatch):
    """The lookup ran outside the txn: a PR recorded meanwhile is not released on."""
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        real = am._fleet_refs
        seen = []

        def refs(c, pid):
            seen.append(pid)
            out = real(c, pid)
            if len(seen) > 1:  # the re-read under the txn sees a second PR
                out = out + [type(out[0])("ANG-Ventures/hermes-agent", 7)]
            return out

        monkeypatch.setattr(am, "_fleet_refs", refs)
        assert am.release_merged_edges(conn, query_fn=_oracle("MERGED")) == []
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_AFTER_MERGE)]


def test_after_merge_without_recorded_pr_waits_for_parent_done(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="no pr yet", assignee="daedalus")
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        q = _oracle("MERGED")
        assert am.release_merged_edges(conn, query_fn=q) == []
        assert q.calls == []
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def _mark_done(conn, task_id: str, status: str = "done") -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()


# t_ab2499c5 (2026-10-18 02:02): A8 t_ccdca9ef was completed while its PR hermes-home#4662 was a
# reviewer-held open DRAFT; the old "or the parent is done" arm released both after-merge children.
def test_done_parent_with_open_draft_pr_keeps_child_gated(kanban_home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd

    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="integration cutover", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        _mark_done(conn, parent)
        q = _oracle("OPEN")  # a draft PR reads OPEN
        assert am.release_merged_edges(conn, query_fn=q) == []
        assert q.calls == [("ANG-Ventures/hermes-agent", 4242)]
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        assert kb._parents_satisfied(conn, child) is False
        # A child some other writer set ready is still demoted at claim time.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.claim_task(conn, child) is None
        assert kb.get_task(conn, child).status == "todo"
        # The real tick, too.
        am._CACHE.clear()
        monkeypatch.setattr(am, "_default_query", lambda: _oracle("OPEN"))
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_AFTER_MERGE)]


def test_done_parent_with_merged_pr_releases(kanban_home):
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="integration cutover", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        _mark_done(conn, parent)
        assert am.release_merged_edges(conn, query_fn=_oracle("MERGED")) == [(parent, child)]
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child) is not None


def test_done_parent_without_pr_releases_edge_on_close(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="no pr", assignee="daedalus")
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        _mark_done(conn, parent)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]


def test_prless_release_rereads_recorded_prs_inside_the_write_txn(kanban_home, monkeypatch):
    """A PR recorded on the parent between the PR-less read and the write txn keeps the gate."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="no pr yet", assignee="daedalus")
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        _mark_done(conn, parent)
        real_txn = kb.write_txn

        def txn_after_pr_recorded(c):
            # The parent's run records its PR after the outside read, before the txn.
            c.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                      "VALUES (?, 'daedalus', 'done', 0, ?)", (parent, f'{{"pr_url": "{PR}"}}'))
            c.commit()
            return real_txn(c)

        monkeypatch.setattr(kb, "write_txn", txn_after_pr_recorded)
        assert am._fleet_refs(conn, parent) == []
        assert am.release_prless_closed_parents(conn) == []
        monkeypatch.setattr(kb, "write_txn", real_txn)
        assert am._fleet_refs(conn, parent) != []
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_AFTER_MERGE)]
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


def test_creation_held_child_behind_prless_done_parent_promotes(kanban_home):
    """Prism 24333b300d1f: the PR-less release must not strand an initial_status=blocked child."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="no pr", assignee="daedalus")
        child = kb.create_task(conn, title="held drill", assignee="daedalus", parents=[parent],
                               parents_kind="after-merge", initial_status="blocked")
        assert kb.get_task(conn, child).status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "blocked"  # parent open: still held
        _mark_done(conn, parent)
        for _ in range(2):
            kb.recompute_ready(conn)
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]
        assert kb.get_task(conn, child).status == "ready"


def test_operator_blocked_child_behind_prless_done_parent_stays_blocked(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="no pr", assignee="daedalus")
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.block_task(conn, child, reason="operator hold", kind="needs_input")
        assert kb.get_task(conn, child).status == "blocked"
        _mark_done(conn, parent)
        for _ in range(2):
            kb.recompute_ready(conn)
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]
        assert kb.get_task(conn, child).status == "blocked"


def test_create_behind_done_parent_with_open_pr_starts_todo(kanban_home):
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        _mark_done(conn, parent)
        child = kb.create_task(conn, title="cutover", assignee="daedalus",
                               parents=[parent], parents_kind="after-merge")
        assert kb.get_task(conn, child).status == "todo"
        assert am.release_merged_edges(conn, query_fn=_oracle("OPEN")) == []
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


def test_dispatch_tick_releases_and_promotes_in_one_tick(kanban_home, monkeypatch):
    """The real tick: release before the lock, recompute_ready promotes."""
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(am, "_default_query", lambda: _oracle("MERGED"))
    with kb.connect() as conn:
        parent = _parent_with_pr(conn)
        child = kb.create_task(conn, title="drill", assignee="daedalus")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert kb.get_task(conn, child).status == "todo"  # dry run mutates nothing
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_DERIVED_FROM)]
        assert kb.get_task(conn, child).status in ("ready", "running")
        assert kb.get_task(conn, parent).status == "review"


def test_blocks_refused_when_parent_body_names_child(kanban_home):
    with kb.connect() as conn:
        child = kb.create_task(conn, title="prism residue", assignee="daedalus")
        parent = kb.create_task(
            conn, title="land the PR", assignee="daedalus",
            body=f"Close when the PR merges AND {child} (residue) is done.")
        with pytest.raises(ValueError) as err:
            kb.link_tasks(conn, parent, child, kind="blocks")
        assert "--kind after-merge" in str(err.value)
        assert kb.parent_links(conn, child) == []
        assert kb.link_tasks(conn, parent, child, kind="after-merge") is True
        assert kb.parent_links(conn, child) == [(parent, kb.LINK_KIND_AFTER_MERGE)]


def test_blocks_allowed_when_body_names_only_a_longer_id(kanban_home):
    with kb.connect() as conn:
        child = kb.create_task(conn, title="next step", assignee="daedalus")
        parent = kb.create_task(conn, title="first step", assignee="daedalus",
                                body=f"see {child}ff for history")
        assert kb.link_tasks(conn, parent, child, kind="blocks") is True


def test_cli_link_prints_gate(kanban_home):
    from hermes_cli import kanban

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="p", assignee="daedalus")
        child = kb.create_task(conn, title="c", assignee="daedalus")
        other = kb.create_task(conn, title="o", assignee="daedalus")
    out = kanban.run_slash(f"link {parent} {child}")
    assert f"Gate: {child} will not start until {parent} is DONE" in out
    out = kanban.run_slash(f"link {parent} {other} --kind after-merge")
    assert f"Gate: {other} will not start until {parent}'s PR is MERGED" in out


# Prism 50dc72190d84: a MERGED cached from one oracle is invisible to a different oracle.
def test_sandbox_merged_does_not_leak_to_live_oracle():
    if True:
        import hermes_cli.kanban_after_merge as am
        am._CACHE.clear()
        ref = type("R", (), {"repo": "ANG-Ventures/x", "number": 7})()
        sandbox = lambda repo, n: {"state": "MERGED"}
        live_calls = []

        def live(repo, n):
            live_calls.append((repo, n))
            return {"state": "OPEN"}

        assert am._state(ref, sandbox, 1000.0, [10]) == "MERGED"
        # the live oracle must be consulted, and must see OPEN, not the sandbox's MERGED
        assert am._state(ref, live, 1000.0, [10]) == "OPEN"
        assert live_calls == [("ANG-Ventures/x", 7)]

def test_same_oracle_still_caches_merged_for_good():
    if True:
        import hermes_cli.kanban_after_merge as am
        am._CACHE.clear()
        ref = type("R", (), {"repo": "ANG-Ventures/x", "number": 8})()
        calls = []

        def live(repo, n):
            calls.append(1)
            return {"state": "MERGED"}

        assert am._state(ref, live, 1000.0, [10]) == "MERGED"
        assert am._state(ref, live, 99999.0, [10]) == "MERGED"
        assert len(calls) == 1


# Prism 7aaa3acdd804: id() of a freed oracle is recycled. A sandbox oracle's MERGED, cached and then the oracle
# dropped, must not be inherited by a new live oracle that lands on the same id.
def test_freed_sandbox_oracle_id_reuse_does_not_leak_merged():
    import gc

    am._CACHE.clear()
    ref = type("R", (), {"repo": "ANG-Ventures/x", "number": 7})()

    def make(state, calls):
        def q(repo, n):
            calls.append((repo, n))
            return {"state": state}
        return q

    sandbox = make("MERGED", [])
    assert am._state(ref, sandbox, 1000.0, [10]) == "MERGED"
    del sandbox
    gc.collect()
    # CPython hands the freed slot to the next same-sized object, so some of these reuse the sandbox's id.
    for _ in range(50):
        calls: list = []
        live = make("OPEN", calls)
        assert am._state(ref, live, 1001.0, [10]) == "OPEN"
        assert calls == [("ANG-Ventures/x", 7)]
        del live
        gc.collect()
