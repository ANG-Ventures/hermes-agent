"""``kanban show`` prints the repo-hold wait flag (t_9d046260, W9-3).

2026-10-03: hermes-agent was HELD (``fleet-merge --hold 1624``); the lander refused every other land rc=27 and the
review cards (#1685 t_cb70d390, #1684 t_6281f908) sat in the review list with nothing saying why. The lander now
posts ONE ``[fleet-merge] HELD-REPO`` comment; ``show`` surfaces it until the hold is released or expires.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_held_repo import held_repo

REPO = "ANG-Ventures/hermes-agent"


def flag(until="2099-01-01T00:00:00Z", hold="hold-T"):
    return (f"[fleet-merge] HELD-REPO {REPO}#1685 @ {hold}: ⏸ held behind {REPO}#1624 until {until} · "
            f"resume-on: pr-merged {REPO}#1624\nfleet-merge refused rc=27")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def review_card(*comments):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="one running card per PR", assignee="daedalus")
        for body in comments:
            kb.add_comment(conn, tid, "fleet-merge", body)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
    return tid


def held_lines(tid):
    return [ln for ln in kc.run_slash(f"show {tid}").splitlines() if ln.strip().startswith("held:")]


def test_show_prints_the_hold_in_force(kanban_home):
    tid = review_card(flag())
    assert held_lines(tid) == [f"  held:      {REPO}#1685 behind {REPO}#1624 until 2099-01-01T00:00:00Z (hold-T)"]
    out = json.loads(kc.run_slash(f"show {tid} --json"))
    assert out["held_repo"]["held"] == f"{REPO}#1624"


def test_released_or_expired_hold_is_not_printed(kanban_home):
    assert held_lines(review_card(flag(), "[fleet-merge] HOLD-RELEASED hold-T")) == []
    assert held_lines(review_card(flag(until="2001-01-01T00:00:00Z"))) == []


def test_a_new_hold_after_a_release_is_printed_again():
    bodies = [flag(hold="hold-A"), "[fleet-merge] HOLD-RELEASED hold-A", flag(hold="hold-B")]
    assert held_repo(bodies, now=0)["hold"] == "hold-B"
    assert held_repo(bodies + ["[fleet-merge] HOLD-RELEASED hold-B"], now=0) is None


def test_a_quoted_flag_is_not_a_flag():
    assert held_repo(["Apollo: see the earlier " + flag()], now=0) is None
