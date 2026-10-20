"""GoalManager idle wakes (gateway safety net) and the no-progress stop."""
import time

import pytest

from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _goal(sid, text="ship it"):
    mgr = goals.GoalManager(session_id=sid)
    mgr.set(text)
    return mgr


def test_timed_park_wakes_with_the_continuation_once_the_deadline_passes(hermes_home):
    mgr = _goal("timed")
    mgr.wait_for_seconds(600, reason="CI")
    now = time.time()
    assert mgr.idle_wake_prompt(now + 300) is None              # still parked
    prompt = mgr.idle_wake_prompt(now + 601)
    assert prompt and prompt.startswith("[Continuing toward your standing goal]")
    assert mgr.state.waiting_until == 0.0                         # barrier cleared
    assert goals.load_goal("timed").waiting_until == 0.0          # and persisted
    assert mgr.state.idle_wakes == 0                              # a lifted park does not spend a check-in


def test_process_park_is_left_to_the_completion_turn_until_the_age_cap(hermes_home):
    mgr = _goal("pid")
    mgr._park("watcher", waiting_on_session="proc_x")
    since = mgr.state.waiting_since
    assert mgr.idle_wake_prompt(since + 60) is None
    assert mgr.idle_wake_prompt(since + goals._MAX_BARRIER_WAIT_S - 1) is None
    prompt = mgr.idle_wake_prompt(since + goals._MAX_BARRIER_WAIT_S + 1)
    assert prompt and "ship it" in prompt
    assert prompt.startswith(goals.GOAL_CHECKIN_PREFIX)           # the "check what's stuck" check-in
    assert "session proc_x" in prompt and "watcher" in prompt
    assert mgr.state.waiting_on_session is None
    assert mgr.state.idle_wakes == 1                              # counts against the cap


def test_stuck_process_rechecks_back_off_and_cap(hermes_home):
    mgr = _goal("stuck")
    m = 60
    t = time.time()
    for n, gap in ((1, 30), (2, 60), (3, 120)):
        mgr._park("build", waiting_on_pid=999999)
        mgr.state.waiting_since = t
        mgr._save()
        assert mgr.idle_wake_prompt(t + (gap - 1) * m) is None
        prompt = mgr.idle_wake_prompt(t + gap * m)
        assert prompt and f"{n}/3" in prompt
        t += gap * m
    mgr._park("build", waiting_on_pid=999999)
    mgr.state.waiting_since = t
    mgr._save()
    assert mgr.idle_wake_prompt(t + 240 * m) is None              # capped until a real event (inside the 6 h horizon)


def test_per_goal_checkin_minutes_overrides_and_disables(hermes_home):
    mgr = _goal("per-goal")
    t0 = mgr.state.last_turn_at = time.time()
    mgr.set_checkin_minutes(10)
    assert goals.load_goal("per-goal").checkin_minutes == 10      # persisted
    assert mgr.idle_wake_prompt(t0 + 9 * 60, checkin_minutes=30) is None
    assert mgr.idle_wake_prompt(t0 + 10 * 60, checkin_minutes=30)
    mgr.set_checkin_minutes(0)
    assert mgr.idle_wake_prompt(t0 + 10 * 3600, checkin_minutes=30) is None


def test_quiet_goal_checkins_back_off_and_cap_until_a_real_event(hermes_home):
    mgr = _goal("quiet")
    t0 = mgr.state.last_turn_at = time.time()
    mgr._save()
    m = 60
    assert mgr.idle_wake_prompt(t0 + 29 * m) is None
    p1 = mgr.idle_wake_prompt(t0 + 30 * m)
    assert p1 and p1.startswith(goals.GOAL_CHECKIN_PREFIX) and "1/3" in p1
    assert mgr.idle_wake_prompt(t0 + 30 * m + 1) is None          # recorded: no double fire
    assert mgr.idle_wake_prompt(t0 + 30 * m + 59 * m) is None     # second interval is 60 min
    t1 = t0 + 30 * m + 60 * m
    assert "2/3" in mgr.idle_wake_prompt(t1)
    assert mgr.idle_wake_prompt(t1 + 119 * m) is None             # third is 120 min (4x cap)
    t2 = t1 + 120 * m
    assert "3/3" in mgr.idle_wake_prompt(t2)
    assert mgr.idle_wake_prompt(t2 + 10 * 60 * m) is None         # capped at 3
    # A real event (user message / worker result) re-arms the check-ins.
    assert mgr.state.idle_wakes == 3
    from unittest.mock import patch
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)):
        mgr.evaluate_after_turn("did a thing", real_event=True)
    assert mgr.state.idle_wakes == 0


def test_checkins_off_and_stale_goals_never_wake(hermes_home):
    mgr = _goal("off")
    now = time.time()
    assert mgr.idle_wake_prompt(now + 3600, checkin_minutes=0) is None
    mgr.state.created_at = mgr.state.last_turn_at = now - goals._IDLE_WAKE_HORIZON_S - 60
    mgr._save()
    assert mgr.idle_wake_prompt(now) is None


def test_no_progress_stop_pauses_after_n_toolless_continue_turns(hermes_home):
    from unittest.mock import patch
    mgr = _goal("np")
    with patch.object(goals, "judge_goal", return_value=("continue", "keep going", False, None, False)):
        d = mgr.evaluate_after_turn("status", made_tool_calls=False, no_progress_turns=3)
        assert d["should_continue"]
        d = mgr.evaluate_after_turn("did work", made_tool_calls=True, no_progress_turns=3)
        assert mgr.state.toolless_turns == 0                       # a tool call resets the streak
        mgr.evaluate_after_turn("status", made_tool_calls=False, no_progress_turns=3)
        mgr.evaluate_after_turn("status", made_tool_calls=False, no_progress_turns=3)
        d = mgr.evaluate_after_turn("status", made_tool_calls=False, no_progress_turns=3)
    assert d["status"] == "paused" and not d["should_continue"]
    assert "no progress" in mgr.state.paused_reason


def test_no_progress_stop_disabled_and_unknown_tool_use_never_counts(hermes_home):
    from unittest.mock import patch
    mgr = _goal("np-off")
    with patch.object(goals, "judge_goal", return_value=("continue", "k", False, None, False)):
        for _ in range(6):
            assert mgr.evaluate_after_turn("s", made_tool_calls=False, no_progress_turns=0)["should_continue"]
            assert mgr.evaluate_after_turn("s", made_tool_calls=None)["should_continue"]


def test_list_active_goal_session_ids(hermes_home):
    _goal("a"); _goal("b").clear()
    assert goals.list_active_goal_session_ids() == ["a"]


def test_synthetic_text_detection():
    assert goals.is_goal_synthetic_text(goals.CONTINUATION_PROMPT_TEMPLATE.format(goal="g"))
    assert goals.is_goal_synthetic_text(goals.GOAL_CHECKIN_PROMPT_TEMPLATE.format(n=1, max=3, goal="g", idle_min=30))
    assert not goals.is_goal_synthetic_text("hey can you check the PR")
