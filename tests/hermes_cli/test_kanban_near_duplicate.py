"""Near-duplicate guard on the kanban create surfaces (CLI + kanban_create tool).

Sibling sessions re-filed identical cards minutes apart (t_276557d4/t_a0924f97,
t_9bdd0e42/t_f9020bbe on 2026-09-25) and each copy burned a worker slot. The
guard refuses a same-title card with >=0.8 title+body similarity created in
the last 24h, warns on other >=0.8 pairs, and ledgers ``--force`` overrides.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

TITLE = "Spec: local (self-hosted) CI for PRIVATE repos only, gated so it can never become the bottleneck"
BODY_A = (
    "Ace 2026-09-25 22:52: wants the Studio/ACE-AI/ACE-MEDIA cores doing CI for "
    "private repos (hosted minutes cost money there) but NEVER the stall we had tonight."
)
BODY_B = (
    "Ace 2026-09-25 22:52: wants Studio/ACE-AI/ACE-MEDIA cores doing CI for "
    "private repos (hosted minutes cost money there) but NEVER tonight's stall."
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (tid, kind),
        )
    ]


def test_same_title_refused_within_window(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=TITLE, body=BODY_A, assignee="daedalus")
        with pytest.raises(kb.NearDuplicateError) as exc:
            kb.create_task(
                conn, title=TITLE, body=BODY_B, assignee="daedalus",
                duplicate_guard=True,
            )
        assert first in str(exc.value)
        assert exc.value.duplicates[0]["id"] == first
        # Nothing was written for the refused card.
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_force_reason_files_and_ledgers(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=TITLE, body=BODY_A, assignee="daedalus")
        second = kb.create_task(
            conn, title=TITLE, body=BODY_B, assignee="daedalus",
            duplicate_guard=True, force_reason="re-filed after scope change",
        )
        forced = _events(conn, second, "near_duplicate_forced")
    assert second != first
    assert forced == [{
        "duplicates": [{"id": first, "score": forced[0]["duplicates"][0]["score"],
                        "same_title": True}],
        "reason": "re-filed after scope change",
    }]
    assert forced[0]["duplicates"][0]["score"] >= kb.NEAR_DUP_THRESHOLD


def test_shard_siblings_warn_but_are_not_refused(kanban_home):
    """A fan-out's shards differ in the title: warn + link, never refuse."""
    base = "review-lane drain SHARD {n}/4 (card ids t_{a}*-t_{b}*): close every review card whose PR is merged"
    body = "Close every review card in your id range whose PR merged; report counts."
    with kb.connect_closing() as conn:
        s2 = kb.create_task(conn, title=base.format(n=2, a=4, b=7), body=body)
        s3 = kb.create_task(
            conn, title=base.format(n=3, a=8, b="b"), body=body, duplicate_guard=True,
        )
        warning = kb.near_duplicate_warning(conn, s3)
    assert warning is not None
    assert warning["duplicates"][0]["id"] == s2
    assert warning["duplicates"][0]["same_title"] is False


def test_unrelated_card_and_old_duplicate_pass_clean(kanban_home):
    with kb.connect_closing() as conn:
        old = kb.create_task(conn, title=TITLE, body=BODY_A)
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (int(time.time()) - kb.NEAR_DUP_WINDOW_SECONDS - 60, old),
        )
        conn.commit()
        fresh = kb.create_task(conn, title=TITLE, body=BODY_B, duplicate_guard=True)
        other = kb.create_task(
            conn, title="Rotate the Decodo proxy credentials on ACE-AI",
            body="unrelated", duplicate_guard=True,
        )
        assert kb.near_duplicate_warning(conn, fresh) is None
        assert kb.near_duplicate_warning(conn, other) is None


def test_archived_card_is_not_a_duplicate(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=TITLE, body=BODY_A)
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (first,))
        conn.commit()
        kb.create_task(conn, title=TITLE, body=BODY_B, duplicate_guard=True)


def test_library_callers_unguarded_by_default(kanban_home):
    """Swarm/decompose/library paths are not the duplicate source; opt-in only."""
    with kb.connect_closing() as conn:
        kb.create_task(conn, title=TITLE, body=BODY_A)
        kb.create_task(conn, title=TITLE, body=BODY_A)


def test_cli_create_refuses_then_force_succeeds(kanban_home):
    first = json.loads(kc.run_slash(
        f"create '{TITLE}' --body 'first copy of the spec' --assignee alice --json"
    ))
    refused = kc.run_slash(
        f"create '{TITLE}' --body 'first copy of the spec' --assignee alice --json"
    )
    assert "near-duplicate" in refused and first["id"] in refused
    forced = json.loads(kc.run_slash(
        f"create '{TITLE}' --body 'first copy of the spec' --assignee alice "
        "--force 'second host needs its own card' --json"
    ))
    with kb.connect_closing() as conn:
        assert _events(conn, forced["id"], "near_duplicate_forced")[0]["reason"] == (
            "second host needs its own card"
        )
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


def test_tool_create_refuses_then_force_reason_succeeds(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt

    first = json.loads(kt._handle_create(
        {"title": TITLE, "body": BODY_A, "assignee": "peer"}
    ))
    assert first["ok"] is True
    refused = json.loads(kt._handle_create(
        {"title": TITLE, "body": BODY_B, "assignee": "peer"}
    ))
    assert "near-duplicate" in refused["error"]
    forced = json.loads(kt._handle_create({
        "title": TITLE, "body": BODY_B, "assignee": "peer",
        "force_reason": "not a duplicate: different host",
    }))
    assert forced["ok"] is True and forced["task_id"] != first["task_id"]
