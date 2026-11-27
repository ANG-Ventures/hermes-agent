"""Deadman-hold refusal after agent PR 307 (t_5b3065cf).

1. An author who CAN hold, refused because the body leads with an automation marker, was told
   "post it as the operator: ... --author apollo", which is refused again. The message now says the
   body is the problem and what works.
2. Writers that pre-check their comments with ``_validate_comment_args`` before a write (takeover /
   FOREIGN CHANGE, create audit, flagship override, set-model, batch route) did not pre-check the hold,
   so a hold line in a reason refused the comment after the change. Each is judged before any write now.
3. Kernel/automation writers (boomerang-wake, dispatcher) quote free text. A hold line in it is stored
   and logged, not raised: the deadman ignores those authors by name.

The fleet predicate (home ``scripts/lib/operator_identity.py``) is not in this checkout; ``_fake_lib``
reproduces the two functions ``check_hold_author`` reads, with the home rule's author list and its
leading-marker rule.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from hermes_cli import kanban_close_on_guard as guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_journal
from hermes_cli import model_registry

HOLD = "deadman: hold 2099-01-01T00:00:00Z"
REASON = "waits on Prism\n" + HOLD
HOME_SID = "20261113_000000_home"
OTHER_SID = "20261113_111111_other"
LANE = "claude-alr"
DEAD_ID = "claude-sonnet-4-8"


def _fake_lib():
    humans = ("apollo", "ace", "default", "human", "aegis")
    marks = ("[review-merge-pass]", "[review-failsafe]", "[changes-hold]", "[boomerang-wake]")

    def is_operator(author):
        a = str(author or "").strip().lower()
        return bool(a) and not a.endswith(("(subagent)", "[bot]")) and (a in humans or a.startswith("human:"))

    def comment_can_hold(author, body):
        return is_operator(author) and not str(body or "").lstrip().startswith(marks)

    return types.SimpleNamespace(is_operator=is_operator, comment_can_hold=comment_can_hold)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME",
                kb.OPERATOR_TOKEN_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HERMES_KANBAN_JOURNAL", raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    real_load = guard._load
    lib = _fake_lib()
    monkeypatch.setattr(guard, "_load", lambda name: lib if name == "operator_identity" else real_load(name))
    kb.init_db()
    return home


@pytest.fixture
def lane_catalog(monkeypatch):
    model_registry.clear_catalog_memo()
    monkeypatch.setattr(model_registry, "_fetch_catalog",
                        lambda provider, force_refresh=False: (f"{LANE}/claude-opus-5-5",) if provider == LANE else ())
    yield
    model_registry.clear_catalog_memo()


def _snapshot(conn, task_id=None):
    """Everything a refused write must leave as it was: rows, comments, events, journal lines."""
    board_slug = kb._journal_board_slug(conn)
    rows = [tuple(r) for r in conn.execute(
        "SELECT id, status, assignee, session_id, model_override, provider_override FROM tasks ORDER BY id")]
    comments = conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    return rows, comments, events, len(list(kanban_journal.read_entries(board_slug)))


# ── 1. the refusal message ──────────────────────────────────────────────────────────────────────────


def test_marker_led_hold_from_operator_says_the_body_is_the_problem(board):
    body = "[review-merge-pass] held\n" + HOLD
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        with pytest.raises(guard.HoldAuthorRefused) as exc:
            kb.add_comment(conn, tid, "apollo", body)
        msg = str(exc.value)
        assert "--author apollo" not in msg and kb.OPERATOR_TOKEN_ENV not in msg
        assert "automation" in msg and "own comment" in msg and "not posted" in msg
        # What the message says works, works.
        kb.add_comment(conn, tid, "apollo", HOLD)
        assert [c.body for c in kb.list_comments(conn, tid)] == [HOLD]


def test_non_operator_hold_keeps_the_operator_hint(board):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        with pytest.raises(guard.HoldAuthorRefused, match="--author apollo"):
            kb.add_comment(conn, tid, "daedalus", HOLD)


# ── 2. pre-checked writers judge the hold before any write ──────────────────────────────────────────


def test_takeover_reason_with_hold_changes_nothing(board):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME_SID)
        assert kb.block_task(conn, tid, reason="needs input", kind="needs_input")
        before = _snapshot(conn)
        with kb.mutation_actor(session_ids=(OTHER_SID,), profile="daedalus", foreign_ok=REASON):
            with pytest.raises(guard.HoldAuthorRefused):
                kb.unblock_task(conn, tid)
        assert _snapshot(conn) == before


def test_create_force_model_reason_with_hold_creates_nothing(board, lane_catalog):
    with kbc.connect() as conn:
        before = _snapshot(conn)
        with pytest.raises(guard.HoldAuthorRefused):
            kb.create_task(conn, title="forced", assignee="w", created_by="daedalus",
                           model_override=DEAD_ID, provider_override=LANE, force_model_reason=REASON)
        assert _snapshot(conn) == before


def test_set_model_force_reason_with_hold_changes_nothing(board, lane_catalog):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        before = _snapshot(conn)
        with pytest.raises(guard.HoldAuthorRefused):
            kb.set_model_override(conn, tid, DEAD_ID, LANE, force_model_reason=REASON, pin_sub_author="daedalus")
        assert _snapshot(conn) == before


def test_batch_route_force_reason_with_hold_changes_nothing(board, lane_catalog):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="w")
        b = kb.create_task(conn, title="b", assignee="w")
        before = _snapshot(conn)
        writes = [kb.BatchRouteWrite(task_id=t, touch_model=True, model=DEAD_ID, provider=LANE,
                                     force_model_reason=REASON, pin_sub_author="daedalus") for t in (a, b)]
        with pytest.raises(guard.HoldAuthorRefused):
            kb.apply_batch_route_writes(conn, writes)
        assert _snapshot(conn) == before


def test_flagship_override_reason_with_hold_changes_nothing(board, monkeypatch):
    """``user`` has flagship authority (an operator label) but the deadman does not honour it."""
    from hermes_cli import kanban_identity

    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: True)
    profile = board / "profiles" / "daedalus-fable"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: claude-fable-5-1\n  provider: claude-apr\n",
                                         encoding="utf-8")
    with kb.connect() as conn:
        before = _snapshot(conn)
        with pytest.raises(guard.HoldAuthorRefused):
            kb.create_task(conn, title="f", assignee="daedalus-fable",
                           flagship_override_reason=REASON, flagship_override_author="user")
        assert _snapshot(conn) == before


# ── 3. machine writers that embed free text ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("author", ["boomerang-wake", "dispatcher"])
def test_machine_author_free_text_with_hold_is_stored_not_raised(board, author):
    body = f"[boomerang-wake] surfaced: child said\n{HOLD} (delegation d1)"
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        kb.add_comment(conn, tid, author, body)
        assert [(c.author, c.body) for c in kb.list_comments(conn, tid)] == [(author, body)]


def test_boomerang_wake_surface_comment_with_hold_in_summary_is_written(board):
    from gateway import kanban_boomerang_wake as bw

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
    entry = {"task_id": tid, "delegation_id": "d1", "board": None}
    assert bw.record_surface(entry, "child summary\n" + HOLD) is True
    with kb.connect_closing() as conn:
        bodies = [c.body for c in kb.list_comments(conn, tid) if c.author == bw.AUTHOR]
    assert len(bodies) == 1 and HOLD in bodies[0]
