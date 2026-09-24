"""Argus r2 B2: item 1 stamps a dispatched worker's fan-out with the HUMAN
home, so the home-session guard must still let that worker mutate the cards
it fanned out (descendants of its dispatched card, or cards its own run
created) -- on BOTH the tool and the CLI surface -- while an unrelated card
that merely shares the same human home stays refused."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt

HUMAN = "20260922_181235_human"
WORKER_RUN = "20260924_worker_run"
ORIGIN = "origin: discord #x (1) \u00b7 session " + HUMAN
KT = "HERMES_KANBAN_TASK"


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (KT, "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="orchestrate", assignee="daedalus",
                                session_id=HUMAN, body=ORIGIN + "\n\nspec")
        # Same human home, NOT part of the worker's fan-out.
        unrelated = kb.create_task(conn, title="unrelated", assignee="argus",
                                   session_id=HUMAN, body=ORIGIN)
    monkeypatch.setenv(KT, parent)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER_RUN)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    assert kb.home_guard_mode() == "refuse"
    return parent, unrelated


def _create(title, parents=None):
    args = {"title": title, "assignee": "argus"}
    if parents:
        args["parents"] = parents
    return json.loads(kt._handle_create(args))["task_id"]


def _links():
    with kb.connect_closing() as conn:
        return {(r[0], r[1]) for r in conn.execute(
            "SELECT parent_id, child_id FROM task_links")}


def _tool_link(parent_id, child_id):
    return json.loads(kt._with_mutation_actor(kt._handle_link)(
        {"parent_id": parent_id, "child_id": child_id}))


def test_worker_links_its_own_children_tool_and_cli(worker_env):
    parent, _ = worker_env
    a, b, c, d = (_create(t, [parent]) for t in "ABCD")
    with kb.connect_closing() as conn:
        assert {kb.get_task(conn, x).session_id for x in (a, b, c, d)} == {HUMAN}
    _tool_link(a, b)
    out = kc.run_slash(f"link {c} {d}")
    links = _links()
    assert (a, b) in links
    assert (c, d) in links, out


def test_worker_owns_grandchildren_through_task_links(worker_env):
    parent, _ = worker_env
    a = _create("A", [parent])
    g1, g2 = _create("G1", [a]), _create("G2", [a])
    _tool_link(g1, g2)
    assert (g1, g2) in _links()


def test_worker_owns_unparented_cards_its_own_run_created(worker_env):
    """No task_links edge: ownership comes from the run's ``created`` event."""
    e, f = _create("E"), _create("F")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, f).session_id == HUMAN
    out = kc.run_slash(f"link {e} {f}")
    assert (e, f) in _links(), out


def test_worker_stays_refused_on_unrelated_card_with_same_home(worker_env):
    parent, unrelated = worker_env
    a = _create("A", [parent])
    res = _tool_link(a, unrelated)
    assert "refused link" in json.dumps(res)
    out = kc.run_slash(f"link {a} {unrelated}")
    assert "refused link" in out
    assert (a, unrelated) not in _links()


def test_other_run_created_card_is_not_owned(worker_env, monkeypatch):
    """A card another run created (different actor session) is not the
    worker's just because it shares the human home."""
    monkeypatch.setenv("HERMES_SESSION_ID", "20260924_other_run")
    other = _create("other")  # created by a different run of the same card
    monkeypatch.setenv(KT, "t_someone_else")
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER_RUN)
    with kb.connect_closing() as conn:
        assert not kb._worker_owns_card(conn, other, worker_env[0], (WORKER_RUN,))


def test_worker_owns_precreated_descendants_via_links_only(worker_env, monkeypatch):
    """Children created by the HUMAN (not this run) under the worker's card:
    ownership comes only from the task_links descent."""
    parent, _ = worker_env
    saved = {k: os.environ.pop(k) for k in (KT, "HERMES_SESSION_ID", "HERMES_PROFILE")}
    with kb.connect_closing() as conn:
        x = kb.create_task(conn, title="X", assignee="argus", parents=(parent,),
                           session_id=HUMAN)
        y = kb.create_task(conn, title="Y", assignee="argus", parents=(parent,),
                           session_id=HUMAN)
    os.environ.update(saved)
    res = _tool_link(x, y)
    assert (x, y) in _links(), res


@pytest.mark.parametrize("verb", ["assign {card} bob", "archive {card}"])
def test_every_guarded_cli_verb_shares_the_exemption(worker_env, verb):
    """The exemption lives in check_home_session (the single guard), so any
    guarded verb -- not only link -- follows it: own child allowed, unrelated
    same-home card refused."""
    parent, unrelated = worker_env
    child = _create("child", [parent])
    ok = kc.run_slash(verb.format(card=child))
    assert "refused" not in ok, ok
    bad = kc.run_slash(verb.format(card=unrelated))
    assert "refused" in bad, bad


# --- Argus r3 C3/C4: the exemption's boundary (direction + event kind) ------


@pytest.fixture
def family_env(tmp_path, monkeypatch):
    """ROOT -> {W, S}; U unrelated. All share the human home and are created
    with NO worker identity, so only the task_links walk can own anything.
    The worker is dispatched for W."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (KT, "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root", assignee="apollo",
                              session_id=HUMAN, body=ORIGIN)
        w = kb.create_task(conn, title="W", assignee="daedalus", parents=(root,),
                           session_id=HUMAN, body=ORIGIN)
        s = kb.create_task(conn, title="S", assignee="argus", parents=(root,),
                           session_id=HUMAN, body=ORIGIN)
        u = kb.create_task(conn, title="U", assignee="argus",
                           session_id=HUMAN, body=ORIGIN)
    monkeypatch.setenv(KT, w)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER_RUN)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    assert kb.home_guard_mode() == "refuse"
    return {"root": root, "w": w, "s": s, "u": u}


def _status(task_id):
    with kb.connect_closing() as conn:
        return kb.get_task(conn, task_id).status


@pytest.mark.parametrize("target", ["root", "s"])
def test_worker_refused_on_ancestor_and_sibling_cli(family_env, target):
    """C3: ownership walks UP from the target to W only. W's ancestor and
    W's sibling are not W's fan-out, so a guarded CLI verb stays refused
    and the DB row is unchanged."""
    card = family_env[target]
    own = _create("A", [family_env["w"]])
    kc.run_slash(f"archive {own}")
    assert _status(own) == "archived"  # control: own child is allowed
    out = kc.run_slash(f"archive {card}")
    assert "refused" in out, out
    assert _status(card) != "archived"


@pytest.mark.parametrize("target", ["root", "s"])
def test_worker_refused_on_ancestor_and_sibling_tool(family_env, target):
    """C3 on the tool surface: link(own child -> ancestor/sibling) is
    guarded on the child end and must write no row."""
    card = family_env[target]
    own = _create("A", [family_env["w"]])
    res = _tool_link(own, card)
    assert "refused link" in json.dumps(res), res
    assert (own, card) not in _links()


def test_worker_comment_does_not_adopt_unrelated_card(family_env):
    """C4: only the ``created`` event proves this run made a card. A comment
    the worker run writes on an unrelated same-home card must not make it
    owned: a guarded mutation afterwards is still refused."""
    u = family_env["u"]
    kc.run_slash(f'comment {u} "fyi"')
    with kb.connect_closing() as conn:
        kinds = {r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND actor_session_id = ?", (u, WORKER_RUN))}
    assert kinds and "created" not in kinds, kinds  # the comment is attributed
    out = kc.run_slash(f"archive {u}")
    assert "refused" in out, out
    assert _status(u) != "archived"
