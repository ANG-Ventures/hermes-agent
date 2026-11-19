"""Forge L0 SB-K: the kernel ``sendback`` handoff kind (spec §2.4).

A producer (CI red, Prism, lander, conflict, revert) sends a PR defect straight
back to the card's implementer. The kernel contract pinned here: ``sendback``
is a handoff kind the respawn guard and dispatcher treat exactly like
``changes_requested``, and its payload reaches the next worker prompt.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "https://github.com/example/repo/pull/44"


def _payload(**over):
    p = {
        "v": 1,
        "key": "example/repo#44@" + "a" * 40 + ":ci-red:lints-fast",
        "repo": "example/repo", "pr": 44, "head": "a" * 40,
        "source": "ci-red", "reason": "ci-red:lints-fast", "retry": "after_change",
        "summary": "lints-fast red on your diff: tests/test_x.py::test_y",
        "evidence": {"failing_ids": ["tests/test_x.py::test_y"]},
        "round": 1,
    }
    p.update(over)
    return p


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_query_github_pr_state", lambda repo, number: "OPEN")
    clock = {"now": int(time.time())}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    kb.init_db()
    return SimpleNamespace(path=home, clock=clock)


def _tick(home, s=5):
    home.clock["now"] += s


def _stopped_in_review(conn, home, title):
    """Implementer opened a PR, requested review and exited (no live worker)."""
    tid = kb.create_task(conn, title=title, assignee="dev")
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
    _tick(home)
    assert kb.request_review(
        conn, tid, summary="PR ready", reviewer="human",
        expected_run_id=claimed.current_run_id,
    )
    _tick(home)
    return tid


def _spawned_ids(conn):
    spawned: list[str] = []
    kbd.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: (spawned.append(task.id) or 4242),
    )
    return spawned


def test_kernel_sendback_is_handoff_kind(kanban_home: SimpleNamespace) -> None:
    """``sendback`` sits in both handoff kind sets ``changes_requested`` is in,
    and lifts ``active_pr`` on a ready card with an open PR for one spawn."""
    assert "sendback" in kb._RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS
    assert kbd._is_handoff_event("sendback", None) is True
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready with PR", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kbd.check_respawn_guard(conn, tid) is None
        assert tid in _spawned_ids(conn)
        # Consumed by that spawn, exactly like an operator requeue.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL, "
                "worker_pid=NULL WHERE id=?", (tid,),
            )
        # The spawn consumed the requeue intent, but the handoff itself still
        # names the implementer until it posts a newer PR comment (#111910).
        assert kbd.check_respawn_guard(conn, tid) is None
        _tick(kanban_home)
        kb.add_comment(conn, tid, author="dev", body=f"Pushed fix to {PR}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("kind", ["changes_requested", "sendback"])
def test_handoff_survives_consumed_intent_like_changes_requested(
    kanban_home: SimpleNamespace, kind: str,
) -> None:
    """Both kinds lift ``active_pr`` through the handoff query, not only the
    one-shot operator intent a spawn consumes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=kind, assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR}")
        _tick(kanban_home)
        kb._append_event(conn, tid, kind, {"reason": "fix"})
        kb._append_event(conn, tid, "spawned", {"pid": 99999999})
        assert not kb._unused_operator_intent_after_pr(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_sendback_respawns_stopped_worker_exactly_as_changes_requested(
    kanban_home: SimpleNamespace,
) -> None:
    from tests.kanban_review_helpers import covered_request_changes

    with kbc.connect() as conn:
        a = _stopped_in_review(conn, kanban_home, "changes requested")
        b = _stopped_in_review(conn, kanban_home, "sendback")

        rclaim = kb.claim_review_task(conn, a)
        ok, who = covered_request_changes(
            conn, a, reason="fix lints", expected_run_id=rclaim.current_run_id,
        )
        assert (ok, who) == (True, "dev")
        ok, delivery = kb.request_sendback(conn, b, payload=_payload())
        assert (ok, delivery) == (True, "requeued")

        for tid in (a, b):
            t = kb.get_task(conn, tid)
            assert (t.status, t.assignee, t.current_run_id) == ("ready", "dev", None)
            assert kbd.check_respawn_guard(conn, tid) is None
        spawned = _spawned_ids(conn)
        assert a in spawned and b in spawned


def test_sendback_payload_renders_into_worker_prompt(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="prompt", assignee="dev")
        assert "Send-backs on your PR" not in kb.build_worker_context(conn, tid)
        kb.request_sendback(conn, tid, payload=_payload())
        ctx = kb.build_worker_context(conn, tid)
    assert "## Send-backs on your PR" in ctx
    assert "ci-red: ci-red:lints-fast r1 on example/repo#44 @ aaaaaaaaaaaa" in ctx
    assert "lints-fast red on your diff" in ctx
    assert "tests/test_x.py::test_y" in ctx


def test_sendback_to_running_implementer_is_in_flight(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running", assignee="dev")
        run = kb.claim_task(conn, tid)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "in_flight")
        t = kb.get_task(conn, tid)
        assert (t.status, t.current_run_id) == ("running", run.current_run_id)
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"]
        assert len(ev) == 1 and ev[0].payload["delivery"] == "in_flight"


def test_sendback_refusals_write_nothing(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="refuse", assignee="dev")
        for bad in (
            _payload(source="apollo"),
            _payload(retry="operator"),
            _payload(summary=" "),
            "not a dict",
        ):
            ok, _ = kb.request_sendback(conn, tid, payload=bad)  # type: ignore[arg-type]
            assert not ok
        # An active review run belongs to the reviewer, not the implementer.
        r = _stopped_in_review(conn, kanban_home, "in review")
        kb.claim_review_task(conn, r)
        assert kb.request_sendback(conn, r, payload=_payload())[0] is False
        assert kb.request_sendback(conn, "t_nope", payload=_payload()) == (False, "task not found")
        for t in (tid, r):
            assert not [e for e in kb.list_events(conn, t) if e.kind == "sendback"]


@pytest.mark.parametrize("prior_round", [False, True])
def test_rereview_after_sendback_keeps_reviewer_provenance(
    kanban_home: SimpleNamespace, prior_round: bool,
) -> None:
    """Reviewer provenance is read from the newest review_requested event:
    review -> sendback -> request_review(reviewer=None) re-routes to the
    original reviewer, with or without an earlier changes_requested round."""
    from tests.kanban_review_helpers import covered_request_changes

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rereview", assignee="dev")
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="PR ready", reviewer="human:apollo",
            expected_run_id=claimed.current_run_id,
        )
        if prior_round:
            rclaim = kb.claim_review_task(conn, tid)
            assert covered_request_changes(
                conn, tid, reason="round 1", expected_run_id=rclaim.current_run_id,
            )[0]
            again = kb.claim_task(conn, tid)
            _tick(kanban_home)
            assert kb.request_review(
                conn, tid, summary="fixed", expected_run_id=again.current_run_id,
            )
        _tick(kanban_home)
        # A producer cannot forge handoff provenance; the kernel records only
        # the implementer (the reviewer lives on review_requested).
        spoof = _payload(reviewer="mallory", implementer="mallory")
        assert kb.request_sendback(conn, tid, payload=spoof) == (True, "requeued")
        sb = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"][-1]
        assert sb.payload["implementer"] == "dev" and "reviewer" not in sb.payload
        fix = kb.claim_task(conn, tid)
        assert "## Send-backs on your PR" in kb.build_worker_context(conn, tid)
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="fixed sendback", expected_run_id=fix.current_run_id,
        )
        rr = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"][-1]
        assert rr.payload["reviewer"] == "human:apollo"
        # Answered by that handback: not replayed into the next prompt.
        assert "## Send-backs on your PR" not in kb.build_worker_context(conn, tid)


def test_rereview_resolves_newest_review_requested_reviewer(
    kanban_home: SimpleNamespace,
) -> None:
    """changes_requested(reviewer=A) -> request_review(reviewer=B) -> the card
    leaves review with no handoff (a reopened parent demotes it) -> sendback
    -> request_review(None) resolves B, the reviewer of the newest review
    cycle, never A from the older changes_requested event."""
    from tests.kanban_review_helpers import covered_request_changes

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="a-then-b", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="human:alice",
            expected_run_id=claimed.current_run_id,
        )
        rclaim = kb.claim_review_task(conn, tid)
        assert covered_request_changes(
            conn, tid, reason="round 1", expected_run_id=rclaim.current_run_id,
        )[0]
        again = kb.claim_task(conn, tid)
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="v2", reviewer="human:bob",
            expected_run_id=again.current_run_id,
        )
        # Parent reopens: the review claim demotes the card to todo, no handoff.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        assert kb.claim_review_task(conn, tid) is None
        assert kb.get_task(conn, tid).status == "todo"
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        fix = kb.claim_task(conn, tid)
        assert fix is not None
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="v3", expected_run_id=fix.current_run_id,
        )
        rr = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"][-1]
        assert rr.payload["reviewer"] == "human:bob"


def test_sendback_resume_phase_survives_block_unblock(kanban_home: SimpleNamespace) -> None:
    """review -> sendback -> block -> unblock resumes ``ready`` (the
    implementer's phase), never the older review resume phase."""
    with kbc.connect() as conn:
        tid = _stopped_in_review(conn, kanban_home, "park after sendback")
        # A prior park left a review resume phase as the newest lifecycle row.
        kb._append_event(conn, tid, "unblocked", {"status": "review", "resume_status": "review"})
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb._resume_status_from_events(conn, tid) == "ready"
        _tick(kanban_home)
        assert kb.block_task(conn, tid, reason="wait", kind="needs_input")
        _tick(kanban_home)
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("ready", "dev")


def test_sendback_to_todo_promotes_to_ready_with_implementer(
    kanban_home: SimpleNamespace,
) -> None:
    """review -> sendback lands ``todo`` behind an open parent -> the parent
    finishes -> promotion lands ``ready`` with the implementer, not review."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="todo sendback", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="PR ready", reviewer="human",
            expected_run_id=claimed.current_run_id,
        )
        kb._append_event(conn, tid, "status", {"status": "review", "resume_status": "review"})
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).status == "todo"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        kb.recompute_ready(conn)
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("ready", "dev")


@pytest.mark.parametrize("alias", ["resume_status", "retry_status", "source_status"])
def test_sendback_strips_every_resume_phase_alias(
    kanban_home: SimpleNamespace, alias: str,
) -> None:
    """A producer cannot steer the resume phase through any alias the reader
    honours: todo sendback with ``<alias>=review`` -> parent done -> ready."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open parent", assignee="planner")
        tid = kb.create_task(conn, title="forge", assignee="dev", parents=[parent])
        assert kb.get_task(conn, tid).status == "todo"
        ok = kb.request_sendback(conn, tid, payload=_payload(**{alias: "review"}))
        assert ok == (True, "requeued")
        sb = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"][-1]
        assert kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("ready", "dev")
        assert all(sb.payload.get(k) != "review" for k in kb.RESUME_PHASE_KEYS)


def test_sendback_on_review_demoted_todo_restores_implementer(
    kanban_home: SimpleNamespace,
) -> None:
    """review -> reopened parent demotes to todo (assignee still the reviewer)
    -> sendback -> parent done -> ready with the implementer as assignee."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="demoted", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="PR ready", reviewer="argus",
            expected_run_id=claimed.current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        assert kb.claim_review_task(conn, tid) is None
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("todo", "argus")
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        sb = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"][-1]
        assert sb.payload["implementer"] == "dev"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        kb.recompute_ready(conn)
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("ready", "dev")


def _long_sendback(tag: str, n_chars: int = 9_000) -> str:
    """Numbered items, one per line, until ``n_chars``; the last line is unique."""
    lines, i = [], 0
    while sum(len(x) + 1 for x in lines) < n_chars:
        i += 1
        lines.append(f"{i}. {tag} item {i}: " + "fix the thing at this line. " * 4)
    lines.append(f"{tag} LAST ITEM: the ninth thousand character")
    return "\n".join(lines)


def _review_round(conn, home, tid, reason):
    """The worker hands back, the reviewer sends it back with ``reason``."""
    from tests.kanban_review_helpers import covered_request_changes

    claimed = kb.claim_task(conn, tid)
    _tick(home)
    assert kb.request_review(conn, tid, summary="PR ready", reviewer="human",
                             expected_run_id=claimed.current_run_id)
    _tick(home)
    rclaim = kb.claim_review_task(conn, tid)
    assert covered_request_changes(conn, tid, reason=reason,
                                   expected_run_id=rclaim.current_run_id)[0]
    _tick(home)


def test_long_changes_requested_reaches_worker_context_whole(kanban_home: SimpleNamespace) -> None:
    """t_5949eafd: a 9,000-character send-back is not cut at the 4 KiB field cap."""
    reason = _long_sendback("newest")
    assert len(reason) > 9_000 > kb._CTX_MAX_FIELD_BYTES
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="long review", assignee="dev")
        _review_round(conn, kanban_home, tid, reason)
        ctx = kb.build_worker_context(conn, tid)
    assert reason in ctx
    assert "truncated" not in ctx


def test_long_sendback_event_reaches_worker_context_whole(kanban_home: SimpleNamespace) -> None:
    summary = _long_sendback("producer")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="long sendback", assignee="dev")
        assert kb.request_sendback(conn, tid, payload=_payload(summary=summary))[0]
        ctx = kb.build_worker_context(conn, tid)
    assert summary in ctx


def test_older_send_back_cut_on_a_line_and_names_the_command(kanban_home: SimpleNamespace) -> None:
    """Only the newest send-back is exempt; an older one is cut at a line end and the
    marker names the command that prints it whole."""
    old, new = _long_sendback("older"), _long_sendback("newest")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="two rounds", assignee="dev")
        _review_round(conn, kanban_home, tid, old)
        _review_round(conn, kanban_home, tid, new)
        ctx = kb.build_worker_context(conn, tid)
    assert new in ctx
    assert "older LAST ITEM" not in ctx
    marker = next(i for i, line in enumerate(ctx.splitlines())
                  if f"whole text: `hermes kanban --board {kb.get_current_board()} show {tid} --json "
                  "--state-type outcome --state-name changes_requested`" in line)
    assert ctx.splitlines()[marker].startswith("… [truncated, ")
    # The line ahead of the marker is a whole line of the older send-back.
    assert ctx.splitlines()[marker - 1] in [line.rstrip() for line in old.splitlines()]


def test_older_sendback_events_cannot_push_out_the_newest(kanban_home: SimpleNamespace) -> None:
    """Prism 36303e1376d8: of several unanswered sendback events only the newest is whole,
    so older ones stay within the field cap and cannot crowd it out of a bounded show."""
    olds = [_long_sendback(f"older{i}") for i in range(4)]
    new = _long_sendback("newest")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="many sendbacks", assignee="dev")
        for i, text in enumerate(olds + [new]):
            assert kb.request_sendback(conn, tid, payload=_payload(
                key=f"example/repo#44@{'a' * 40}:ci-red:job{i}", summary=text))[0]
        ctx = kb.build_worker_context(conn, tid)
    assert new in ctx
    for i in range(4):
        assert f"older{i} LAST ITEM" not in ctx
    assert ctx.count(f"whole text: `hermes kanban --board {kb.get_current_board()} show {tid} --json`") == 4
    assert len(ctx) < len(new) + 4 * (kb._CTX_MAX_FIELD_BYTES + 200) + 4_000


def test_cap_lines_keeps_a_line_that_ends_exactly_at_the_cap() -> None:
    """Prism 88adbe35004b: a cut that already falls on a line end drops no whole line."""
    cap = kb._CTX_MAX_FIELD_BYTES
    first = "x" * (cap - 6)
    s = first + "\nlast!\n" + "tail line\n" * 50
    assert s[cap] == "\n"
    out = kb._ctx_cap_lines(s, "cmd")
    assert out.splitlines()[:2] == [first, "last!"]
    one_line = "word " * (cap // 2)
    cut = kb._ctx_cap_lines(one_line, "cmd").splitlines()[0]
    assert one_line.startswith(cut) and one_line[len(cut)] == " "
