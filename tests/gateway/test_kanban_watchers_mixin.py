"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


# --- stuck-detector: benign decline vs genuine fault ---------------------------
#
# The dispatcher warns "check profile health (venv, PATH, credentials)" when the
# ready queue is non-empty but nothing spawns for N consecutive ticks. That
# condition is ALSO the healthy steady state when the dispatcher DECLINES to
# spawn (every profile at its concurrency cap, an assignee 429-rate-limited, the
# board lock held elsewhere). The detector must only count a tick as "bad" when
# the zero-spawn reflects a real fault — otherwise a large fan-out or a provider
# 429 window mis-fires the warning for hours (observed 2026-07-11, ~2h).

from dataclasses import dataclass, field  # noqa: E402

from gateway.kanban_watchers import (  # noqa: E402
    _format_parent_satisfied_sticky_summary,
    _WorkspaceRefusalOutageNotifier,
    _format_respawn_guarded_summary,
    _format_workspace_refused_summary,
    _observe_workspace_refusal_outages,
    _send_workspace_refusal_alert,
    _stall_streak_is_bad,
)


@dataclass
class _FakeResult:
    """Minimal stand-in for kanban_db.DispatchResult (only the buckets the
    stuck-detector consults)."""

    spawned: list = field(default_factory=list)
    skipped_per_profile_capped: list = field(default_factory=list)
    rate_limited: list = field(default_factory=list)
    respawn_guarded: list = field(default_factory=list)
    skipped_locked: bool = False
    spawn_failed: list = field(default_factory=list)
    auto_blocked: list = field(default_factory=list)
    workspace_refused: list = field(default_factory=list)


def test_stall_idle_queue_is_not_bad():
    # No spawnable work → never a stall regardless of results.
    assert _stall_streak_is_bad(False, False, [("b", _FakeResult())]) is False


def test_stall_something_spawned_is_not_bad():
    # We spawned this tick → not a stall even with a full queue.
    assert _stall_streak_is_bad(True, True, [("b", _FakeResult(spawned=[("t", "p", "w")]))]) is False


def test_stall_per_profile_cap_is_benign_not_bad():
    # Ready work, zero spawns, but every eligible profile is at its cap → healthy.
    res = _FakeResult(skipped_per_profile_capped=[("t1", "daedalus", 3)])
    assert _stall_streak_is_bad(True, False, [("b", res)]) is False


def test_stall_rate_limited_is_benign_not_bad():
    # Assignee bounced off a provider 429 and the task was released to ready → healthy.
    res = _FakeResult(rate_limited=["t1"])
    assert _stall_streak_is_bad(True, False, [("b", res)]) is False


def test_stall_lock_held_is_benign_not_bad():
    # Another dispatcher process holds the board lock this tick → healthy.
    res = _FakeResult(skipped_locked=True)
    assert _stall_streak_is_bad(True, False, [("b", res)]) is False


def test_gateway_respawn_guard_summary_groups_reasons():
    summary = _format_respawn_guarded_summary([
        ("t_open1", "active_pr"),
        ("t_recent", "recent_success"),
        ("t_open2", "active_pr"),
    ])
    assert summary == (
        "respawn_guarded=3 (active_pr: t_open1, t_open2; "
        "recent_success: t_recent)"
    )


def test_gateway_tick_summary_counts_and_names_parent_satisfied_sticky_cards():
    assert _format_parent_satisfied_sticky_summary(["t_beta", "t_alpha"]) == (
        "parents_done_sticky=2 (t_alpha, t_beta)"
    )


def test_gateway_workspace_refused_summary_names_reason_and_tasks():
    summary = _format_workspace_refused_summary([
        ("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch/kanban-workspaces"),
        ("t_stranded", "stranded_by_mount_loss: /Volumes/ramscratch/kanban-workspaces/t_stranded"),
    ])
    assert summary == (
        "workspace_refused=2 (stranded_by_mount_loss: t_stranded; "
        "workspaces_root_unmounted: t_missing)"
    )


def test_workspace_refusal_notifier_delivers_once_per_outage_and_rearms():
    notifier = _WorkspaceRefusalOutageNotifier()
    deliveries = []

    def send(board, summary):
        deliveries.append((board, summary))
        return True

    refused = [("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch")]
    assert notifier.observe("default", [], send) is False
    assert len(deliveries) == 0
    assert notifier.observe("default", refused, send) is True
    assert len(deliveries) == 1
    assert notifier.observe("default", refused, send) is False
    assert len(deliveries) == 1
    assert notifier.observe("default", [], send) is False
    assert notifier.observe("default", refused, send) is True
    assert len(deliveries) == 2


def test_workspace_refusal_notifier_retries_until_delivery_succeeds():
    notifier = _WorkspaceRefusalOutageNotifier()
    outcomes = iter([False, True])
    attempts = []

    def send(board, summary):
        attempts.append((board, summary))
        return next(outcomes)

    refused = [("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch")]
    assert notifier.observe("default", refused, send) is False
    assert notifier.observe("default", refused, send) is True
    assert len(attempts) == 2


def test_workspace_refusal_tick_observer_uses_delivery_latch(monkeypatch):
    import gateway.kanban_watchers as kw

    deliveries = []

    def send(board, summary):
        deliveries.append((board, summary))
        return True

    monkeypatch.setattr(kw, "_send_workspace_refusal_alert", send)
    notifier = _WorkspaceRefusalOutageNotifier()
    refused = _FakeResult(workspace_refused=[
        ("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch"),
    ])
    healthy = _FakeResult()

    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 1
    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 0
    assert len(deliveries) == 1
    assert _observe_workspace_refusal_outages(notifier, [("default", healthy)]) == 0
    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 1
    assert len(deliveries) == 2


def test_workspace_refusal_sender_uses_default_profile_error_route(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    script = tmp_path / ".hermes" / "scripts" / "notify.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("gateway.kanban_watchers.subprocess.run", run)
    assert _send_workspace_refusal_alert("default", "workspace_refused=1")
    argv, kwargs = calls[0]
    assert argv[argv.index("--profile") + 1] == "default"
    assert argv[argv.index("--sev") + 1] == "error"
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_guard_stuck_notifier_pages_once_and_rearms():
    from gateway.kanban_watchers import _GuardStuckNotifier, _stall_streak_is_bad
    item = {"task_id": "t_test", "clear_verb": 'kanban requeue t_test "<reason>"'}
    notifier = _GuardStuckNotifier()
    sent = []
    def send(board, row):
        sent.append((board, row))
        return True
    assert notifier.observe([("default", item)], send) == 1
    assert notifier.observe([("default", item)], send) == 0
    assert sent[0][1]["clear_verb"] == 'kanban requeue t_test "<reason>"'
    assert _stall_streak_is_bad(True, True, [("default", _FakeResult())], guard_stuck=True)
    assert notifier.observe([], send) == 0
    assert notifier.observe([("default", item)], send) == 1


def test_guard_stuck_sender_routes_to_alerts(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace
    from gateway.kanban_watchers import _send_guard_stuck_alert
    script = tmp_path / ".hermes" / "scripts" / "notify.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr("gateway.kanban_watchers.subprocess.run", lambda argv, **kw: (calls.append((argv, kw)) or SimpleNamespace(returncode=0)))
    assert _send_guard_stuck_alert("default", {"task_id": "t_test", "clear_verb": 'kanban requeue t_test "<reason>"'})
    argv, kwargs = calls[0]
    assert argv[argv.index("--channel") + 1] == "discord"
    assert argv[argv.index("--sev") + 1] == "error"
    assert "kanban requeue t_test" in argv[argv.index("--send") + 1]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_stall_respawn_guard_is_benign_not_bad():
    res = _FakeResult(respawn_guarded=[("t1", "recent_success")])
    assert _stall_streak_is_bad(True, False, [("b", res)]) is False


def test_stall_bare_zero_spawn_with_no_reason_IS_bad():
    # Ready work, zero spawns, and NO benign decline to explain it → genuine
    # stall suspect (the true-positive the warning exists to catch: broken
    # venv/PATH/creds that fails silently before the circuit breaker trips).
    assert _stall_streak_is_bad(True, False, [("b", _FakeResult())]) is True


def test_stall_auto_blocked_fault_IS_bad_even_with_benign_sibling():
    # A circuit-breaker auto_block is a real fault and must count even if
    # ANOTHER board this tick declined benignly (cap saturated).
    faulted = _FakeResult(auto_blocked=["t1"])
    capped = _FakeResult(skipped_per_profile_capped=[("t2", "athena", 2)])
    assert _stall_streak_is_bad(True, False, [("b1", faulted), ("b2", capped)]) is True


def test_stall_early_spawn_failure_IS_bad_even_with_benign_sibling():
    # Cross-board masking regression (Greptile #304 P2): board A has an EARLY,
    # pre-circuit-breaker spawn failure (spawn_failed populated, but not yet
    # auto_blocked), while board B is benignly rate-limited the same tick. The
    # benign decline on B must NOT mask the genuine fault on A — spawn_failed is
    # a fault immediately (failure #1), so the tick counts.
    early_fail = _FakeResult(spawn_failed=["t1"])  # not yet auto_blocked
    rate_limited = _FakeResult(rate_limited=["t2"])
    assert _stall_streak_is_bad(True, False, [("A", early_fail), ("B", rate_limited)]) is True


def test_stall_workspace_refusal_IS_bad_even_with_benign_sibling():
    refused = _FakeResult(workspace_refused=[
        ("t1", "workspaces_root_unmounted: /Volumes/ramscratch"),
    ])
    capped = _FakeResult(skipped_per_profile_capped=[("t2", "athena", 2)])
    assert _stall_streak_is_bad(True, False, [("A", refused), ("B", capped)]) is True


def test_stall_none_results_bare_stall_is_bad():
    # Defensive: a None board result contributes nothing; a bare stall still counts.
    assert _stall_streak_is_bad(True, False, [("b", None)]) is True

