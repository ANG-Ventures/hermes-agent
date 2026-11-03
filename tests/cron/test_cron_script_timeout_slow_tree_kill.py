"""PHASE=cron_script_timeout reports elapsed at the deadline, not after a slow tree-kill.

On a loaded host the tree-kill (worker-spare board reads, TERM->KILL escalation) took ~2 s, so
the log line read elapsed=5 for a 3 s timeout and the end-to-end test went red on main (t_af9df369).
"""
import time

import pytest

from tests.cron.test_cron_script_job_timeout import (
    test_term_ignoring_script_is_killed_at_its_interval as _killed_at_interval,
)


@pytest.mark.platforms("posix")
def test_timeout_elapsed_is_measured_before_a_slow_tree_kill(hermes_env, monkeypatch, caplog):
    import agent.deadline as deadline

    real_kill = deadline.kill_process_tree

    def slow_kill(pid, *args, **kwargs):
        time.sleep(2.5)
        return real_kill(pid, *args, **kwargs)

    monkeypatch.setattr(deadline, "kill_process_tree", slow_kill)
    _killed_at_interval(hermes_env, monkeypatch, caplog)
