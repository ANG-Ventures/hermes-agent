"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

import pytest

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
    from hermes_cli.kanban_workspace_policy import STRANDED_RECOVERY_COMMAND

    summary = _format_workspace_refused_summary([
        ("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch/kanban-workspaces"),
        ("t_stranded", "stranded_by_mount_loss: /Volumes/ramscratch/kanban-workspaces/t_stranded"),
    ])
    assert summary == (
        "workspace_refused=2 (stranded_by_mount_loss: t_stranded; "
        "workspaces_root_unmounted: t_missing) "
        f"| recover stranded scratch cards: {STRANDED_RECOVERY_COMMAND}"
    )


def test_workspace_refused_summary_names_recovery_only_when_stranded():
    from hermes_cli.kanban_workspace_policy import STRANDED_RECOVERY_COMMAND as cmd

    unmounted = _format_workspace_refused_summary([
        ("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch/kanban-workspaces"),
    ])
    assert cmd not in unmounted
    stranded = _format_workspace_refused_summary([
        ("t_a", "stranded_by_mount_loss: /x/t_a"),
    ])
    assert stranded.endswith(cmd)


class _FakeLatch:
    """In-memory stand-in for the durable per-card claim/release."""

    def __init__(self):
        self.paged = set()
        self.next_id = 0

    def claim(self, board, entries):
        out = []
        for task_id, reason in entries:
            if (board, task_id, reason) in self.paged:
                continue
            self.next_id += 1
            self.paged.add((board, task_id, reason))
            out.append((task_id, reason, (board, task_id, reason)))
        return out

    def release(self, board, ids):
        for key in ids:
            self.paged.discard(key)


def test_workspace_refusal_notifier_pages_once_per_card_across_healthy_ticks():
    latch = _FakeLatch()
    notifier = _WorkspaceRefusalOutageNotifier(latch.claim, latch.release)
    deliveries = []

    def send(board, summary):
        deliveries.append((board, summary))
        return True

    refused = [("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch")]
    assert notifier.observe("default", [], send) is False
    assert notifier.observe("default", refused, send) is True
    assert notifier.observe("default", refused, send) is False
    # A tick that did not admission-check the card (cap/guard skip) is NOT
    # recovery: it must not re-arm the page (t_ff4197d3, t_ca81dfe2).
    assert notifier.observe("default", [], send) is False
    assert notifier.observe("default", refused, send) is False
    assert len(deliveries) == 1
    assert "hermes kanban show t_missing" in deliveries[0][1]
    # A second card joining the outage is a change: it pages, alone.
    both = refused + [("t_other", "workspaces_root_unmounted: /Volumes/ramscratch")]
    assert notifier.observe("default", both, send) is True
    assert "t_other" in deliveries[1][1] and "t_missing" not in deliveries[1][1]
    assert "+1 already paged" in deliveries[1][1]


def test_workspace_refusal_notifier_retries_until_delivery_succeeds():
    latch = _FakeLatch()
    notifier = _WorkspaceRefusalOutageNotifier(latch.claim, latch.release)
    outcomes = iter([False, True])
    attempts = []

    def send(board, summary):
        attempts.append((board, summary))
        return next(outcomes)

    refused = [("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch")]
    assert notifier.observe("default", refused, send) is False
    assert notifier.observe("default", refused, send) is True
    assert notifier.observe("default", refused, send) is False
    assert len(attempts) == 2


def test_workspace_refusal_notifier_falls_back_to_process_latch_when_db_fails():
    def broken(board, entries):
        raise RuntimeError("db locked")

    notifier = _WorkspaceRefusalOutageNotifier(broken, broken)
    deliveries = []
    send = lambda board, summary: deliveries.append(summary) or True  # noqa: E731
    refused = [("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch")]
    for _ in range(3):
        notifier.observe("default", refused, send)
    assert len(deliveries) == 1


def test_workspace_refusal_tick_observer_uses_delivery_latch(monkeypatch):
    import gateway.kanban_watchers as kw

    deliveries = []

    def send(board, summary):
        deliveries.append((board, summary))
        return True

    monkeypatch.setattr(kw, "_send_workspace_refusal_alert", send)
    latch = _FakeLatch()
    notifier = _WorkspaceRefusalOutageNotifier(latch.claim, latch.release)
    refused = _FakeResult(workspace_refused=[
        ("t_missing", "workspaces_root_unmounted: /Volumes/ramscratch"),
    ])
    healthy = _FakeResult()

    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 1
    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 0
    assert _observe_workspace_refusal_outages(notifier, [("default", healthy)]) == 0
    assert _observe_workspace_refusal_outages(notifier, [("default", refused)]) == 0
    assert len(deliveries) == 1


def test_workspace_refusal_sender_uses_default_profile_error_route(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    script = tmp_path / ".hermes" / "scripts" / "notify.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(script.parent.parent))  # the active root
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
    item = {"task_id": "t_test", "clear_verb": 'hermes kanban requeue t_test "<reason>"'}
    notifier = _GuardStuckNotifier()
    sent = []
    def send(board, row):
        sent.append((board, row))
        return True
    assert notifier.observe([("default", item)], send) == 1
    assert notifier.observe([("default", item)], send) == 0
    assert sent[0][1]["clear_verb"] == 'hermes kanban requeue t_test "<reason>"'
    assert _stall_streak_is_bad(True, True, [("default", _FakeResult())], guard_stuck=True)
    assert notifier.observe([], send, observed_boards=set()) == 0  # lock/probe failure: unknown, not recovered
    assert notifier.observe([("default", item)], send) == 0
    assert notifier.observe([], send, observed_boards={"default"}) == 0  # observed absence (blip)
    assert notifier.observe([("default", item)], send) == 0  # same episode: silent
    new_episode = {**item, "guarded_since": 1_000}
    assert notifier.observe([("default", new_episode)], send) == 1  # guard reset -> new episode


def test_guard_stuck_notifier_survives_restart_and_reminds_every_6h(tmp_path):
    from gateway.kanban_watchers import _GUARD_STUCK_REMIND_SECONDS, _GuardStuckNotifier
    item = {"task_id": "t_test", "reason": "active_pr", "guarded_since": 100,
            "clear_verb": "hermes kanban --board default requeue t_test '<reason>'"}
    state = tmp_path / "state" / "guard.json"
    sent = []
    def send(board, row):
        sent.append(row["task_id"])
        return True
    t0 = 10_000.0
    assert _GuardStuckNotifier(state).observe([("default", item)], send, now=t0) == 1
    # Gateway restart: a fresh notifier reads the ledger and stays silent.
    restarted = _GuardStuckNotifier(state)
    assert restarted.observe([("default", item)], send, now=t0 + 60) == 0
    # Streak briefly stale (card absent from the probe), then back: same episode.
    assert restarted.observe([], send, observed_boards={"default"}, now=t0 + 600) == 0
    assert restarted.observe([("default", item)], send, now=t0 + 1200) == 0
    # Still stuck 6h after the page: exactly one reminder.
    assert restarted.observe([("default", item)], send, now=t0 + _GUARD_STUCK_REMIND_SECONDS) == 1
    assert restarted.observe([("default", item)], send,
                             now=t0 + _GUARD_STUCK_REMIND_SECONDS + 60) == 0
    assert sent == ["t_test", "t_test"]


def test_guard_stuck_notifier_retries_failed_send_after_unobserved_tick():
    from gateway.kanban_watchers import _GuardStuckNotifier
    item = {"task_id": "t_test", "clear_verb": 'hermes kanban requeue t_test "<reason>"'}
    notifier = _GuardStuckNotifier()
    calls = []
    def send(board, row):
        calls.append(board)
        return len(calls) > 1
    assert notifier.observe([("default", item)], send) == 0
    assert notifier.observe([], send, observed_boards=set()) == 0
    assert notifier.observe([("default", item)], send) == 1
    assert len(calls) == 2


def test_guard_stuck_probe_distinguishes_empty_board_from_skipped_or_failed(monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import kanban_db as kb
    from gateway.kanban_watchers import _guard_stuck_cards

    @contextmanager
    def connect(*, board):
        if board == "failed":
            raise OSError("probe failed")
        yield object()

    monkeypatch.setattr(kb, "connect_closing", connect)
    monkeypatch.setattr(kb, "respawn_guard_stuck_tasks", lambda conn, **kw: [])
    cards, observed = _guard_stuck_cards([
        ("healthy", _FakeResult()),
        ("locked", _FakeResult(skipped_locked=True)),
        ("failed", _FakeResult()),
    ])
    assert cards == []
    assert observed == {"healthy"}


def test_guard_stuck_sender_routes_to_alerts(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace
    from gateway.kanban_watchers import _send_guard_stuck_alert
    script = tmp_path / ".hermes" / "scripts" / "notify.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(script.parent.parent))  # the active root
    calls = []
    monkeypatch.setattr("gateway.kanban_watchers.subprocess.run", lambda argv, **kw: (calls.append((argv, kw)) or SimpleNamespace(returncode=0)))
    assert _send_guard_stuck_alert("default", {"task_id": "t_test", "clear_verb": 'hermes kanban requeue t_test "<reason>"'})
    argv, kwargs = calls[0]
    assert argv[argv.index("--channel") + 1] == "discord"
    assert argv[argv.index("--sev") + 1] == "error"
    assert 'hermes kanban requeue t_test "<reason>"' in argv[argv.index("--send") + 1]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert _send_guard_stuck_alert("default", {
        "task_id": "t_pid", "reason": "prior_worker_still_alive",
        "prev_pid": 97056, "clear_verb": "hermes kanban show t_pid",
    })
    pid_message = calls[-1][0][calls[-1][0].index("--send") + 1]
    assert "prior_worker_still_alive" in pid_message
    assert "97056" in pid_message
    assert "hermes kanban show t_pid" in pid_message
    assert "requeue alone cannot bypass" in pid_message
    assert "READY card: prior_worker_still_alive" in pid_message  # no status -> ready door
    assert _send_guard_stuck_alert("default", {
        "task_id": "t_rev", "reason": "prior_worker_still_alive", "status": "review",
        "prev_pid": 97057, "clear_verb": "hermes kanban show t_rev",
    })
    review_message = calls[-1][0][calls[-1][0].index("--send") + 1]
    assert "REVIEW card: prior_worker_still_alive" in review_message
    assert "READY" not in review_message


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




@pytest.mark.parametrize("which", ["workspace_refusal", "guard_stuck"])
def test_dispatcher_alerts_resolve_notify_from_the_active_root(tmp_path, monkeypatch, which):
    """FleetReview #952: a redirected Hermes home must page through ITS notify.py,
    never the live home-dir one (and page nothing when it has none)."""
    from pathlib import Path
    from types import SimpleNamespace

    import gateway.kanban_watchers as kw

    user_home = tmp_path / "user"
    for root in (user_home / ".hermes", user_home / ".hermes"):
        live = root / "scripts" / "notify.py"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    calls = []
    monkeypatch.setattr(
        "gateway.kanban_watchers.subprocess.run",
        lambda argv, **kw_: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    def send():
        if which == "workspace_refusal":
            return kw._send_workspace_refusal_alert("default", "workspace_refused=1")
        return kw._send_guard_stuck_alert("default", {"task_id": "t_x", "clear_verb": "v"})

    assert send() is False and calls == []  # sandbox has no notify.py: no live page
    mine = sandbox / "skills-shared" / "general" / "scheduler" / "scripts" / "notify.py"
    mine.parent.mkdir(parents=True)
    mine.write_text("", encoding="utf-8")
    assert send() is True
    assert calls[-1][1] == str(mine)


def test_guard_stuck_notifier_pages_each_guard_reason_of_one_card():
    """C6 (#956): a card that moves from prior_worker_still_alive to the
    active_pr guard gets the second page and its own recovery verb."""
    from gateway.kanban_watchers import _GuardStuckNotifier
    notifier = _GuardStuckNotifier()
    sent = []
    def send(board, row):
        sent.append(row["reason"])
        return True
    alive = {"task_id": "t_test", "reason": "prior_worker_still_alive", "clear_verb": "x"}
    active_pr = {"task_id": "t_test", "reason": "active_pr", "clear_verb": "hermes kanban requeue t_test"}
    assert notifier.observe([("default", alive)], send) == 1
    assert notifier.observe([("default", active_pr)], send) == 1
    assert sent == ["prior_worker_still_alive", "active_pr"]



def test_active_pr_is_one_episode_per_card_and_pr(tmp_path):
    """r19 (t_f1af5dcd): a restarted streak on the SAME open PR is the same
    episode (6h reminder only); a different PR is a new one."""
    from gateway.kanban_watchers import _GUARD_STUCK_REMIND_SECONDS, _GuardStuckNotifier
    pr = "https://github.com/o/r/pull/9"
    item = {"task_id": "t_x", "reason": "active_pr", "pr": pr, "guarded_since": 100, "clear_verb": "x"}
    notifier = _GuardStuckNotifier(tmp_path / "g.json")
    sent = []
    send = lambda board, row: sent.append(row.get("pr")) or True
    t0 = 10_000.0
    assert notifier.observe([("default", item)], send, now=t0) == 1
    assert notifier.observe([("default", {**item, "guarded_since": 5_000})], send, now=t0 + 60) == 0
    assert notifier.observe([("default", {**item, "pr": pr + "0"})], send, now=t0 + 120) == 1
    assert notifier.observe([("default", {**item, "guarded_since": 9})], send,
                            now=t0 + _GUARD_STUCK_REMIND_SECONDS) == 1  # the 6h reminder
    assert sent == [pr, pr + "0", pr]


def test_active_pr_key_honours_pre_r19_ledger_entry(tmp_path):
    """The deploy must not re-page every open episode once: a legacy
    ``board|card|active_pr|<guarded_since>`` entry counts as the last page."""
    import json
    from gateway.kanban_watchers import _GUARD_STUCK_REMIND_SECONDS, _GuardStuckNotifier
    state = tmp_path / "g.json"
    state.write_text(json.dumps({"default|t_x|active_pr|1790739715": 10_000.0}))
    item = {"task_id": "t_x", "reason": "active_pr", "pr": "https://github.com/o/r/pull/9",
            "guarded_since": 1790739715, "clear_verb": "x"}
    notifier = _GuardStuckNotifier(state)
    send = lambda board, row: True
    assert notifier.observe([("default", item)], send, now=10_000.0 + 600) == 0
    # Prism #1530 r2: the legacy time was migrated to the per-PR key and
    # persisted, so a streak reset on the SAME PR (new guarded_since) stays silent,
    # also across a restart.
    reset = {**item, "guarded_since": 1790750000}
    assert _GuardStuckNotifier(state).observe([("default", reset)], send, now=10_000.0 + 650) == 0
    # Prism #1530: a DIFFERENT streak/PR of the same card is not covered by it.
    other = {**item, "pr": "https://github.com/o/r/pull/10", "guarded_since": 1790745000}
    assert notifier.observe([("default", other)], send, now=10_000.0 + 700) == 1
    assert notifier.observe([("default", item)], send,
                            now=10_000.0 + _GUARD_STUCK_REMIND_SECONDS) == 1


def test_legacy_entry_is_consumed_by_one_pr_of_the_streak(tmp_path):
    """Prism #1530 (6782f1a4e186): the legacy ``…|active_pr|<guarded_since>``
    entry names no PR, so it may silence only ONE PR of that streak. A card
    that acquires PR B inside the same streak (same guarded_since, within 6h
    of PR A's page) must page for B, also across a restart."""
    import json
    from gateway.kanban_watchers import _GuardStuckNotifier
    state = tmp_path / "g.json"
    state.write_text(json.dumps({"default|t_x|active_pr|1790739715": 10_000.0}))
    a = {"task_id": "t_x", "reason": "active_pr", "pr": "https://github.com/o/r/pull/9",
         "guarded_since": 1790739715, "clear_verb": "x"}
    b = {**a, "pr": "https://github.com/o/r/pull/10"}
    sent = []
    send = lambda board, row: sent.append(row["pr"]) or True
    assert _GuardStuckNotifier(state).observe([("default", a)], send, now=10_600.0) == 0
    assert _GuardStuckNotifier(state).observe([("default", b)], send, now=10_700.0) == 1
    assert sent == [b["pr"]]
    assert "default|t_x|active_pr|1790739715" not in json.loads(state.read_text())
    # A itself stays covered by its migrated per-PR key.
    assert _GuardStuckNotifier(state).observe([("default", a)], send, now=10_800.0) == 0


def test_legacy_entry_left_by_a_prior_migration_is_consumed(tmp_path):
    """Prism #1534 (a52a75cf4895): a ledger migrated by the pre-consume code holds
    BOTH the per-PR key and the legacy key. Observing PR A (canonical hit) must
    still drop the legacy key, so PR B of the same streak pages."""
    import json
    from gateway.kanban_watchers import _GuardStuckNotifier
    pr_a = "https://github.com/o/r/pull/9"
    state = tmp_path / "g.json"
    state.write_text(json.dumps({"default|t_x|active_pr|1790739715": 10_000.0,
                                 f"default|t_x|active_pr|pr={pr_a}": 10_000.0}))
    a = {"task_id": "t_x", "reason": "active_pr", "pr": pr_a,
         "guarded_since": 1790739715, "clear_verb": "x"}
    b = {**a, "pr": "https://github.com/o/r/pull/10"}
    send = lambda board, row: True
    assert _GuardStuckNotifier(state).observe([("default", a)], send, now=10_600.0) == 0
    assert "default|t_x|active_pr|1790739715" not in json.loads(state.read_text())
    assert _GuardStuckNotifier(state).observe([("default", b)], send, now=10_700.0) == 1
    # Prism #1534 (886ea41b20e6): restart straight onto PR B from the two-entry
    # ledger; the legacy time already belongs to A, so B pages.
    state.write_text(json.dumps({"default|t_x|active_pr|1790739715": 10_000.0,
                                 f"default|t_x|active_pr|pr={pr_a}": 10_000.0}))
    assert _GuardStuckNotifier(state).observe([("default", b)], send, now=10_700.0) == 1
    assert "default|t_x|active_pr|1790739715" not in json.loads(state.read_text())


def test_active_pr_page_names_the_wanted_verb():
    from gateway.kanban_watchers import _active_pr_detail
    requeue = "hermes kanban --board default requeue t_x '<reason>'"
    base = {"task_id": "t_x", "reason": "active_pr", "clear_verb": requeue,
            "pr": "https://github.com/ANG-Ventures/hermes-home/pull/1838"}
    timed_out = _active_pr_detail("default", {**base, "last_outcome": "timed_out"})
    assert f"Wanted: **REQUEUE** (worker resumes on its PR): `{requeue}`" in timed_out
    assert "fleet-merge.sh ANG-Ventures/hermes-home 1838 --by" in timed_out  # the alternative
    for interrupted in ("stale", "stalled", "changes_requested", "cohort_death", None, "future_kind"):
        page = _active_pr_detail("default", {**base, "last_outcome": interrupted})
        assert "Wanted: **REQUEUE**" in page, interrupted
    finished = _active_pr_detail("default", {**base, "last_outcome": "completed"})
    assert finished.index("Wanted: **LAND**") < finished.index(requeue)
    assert "fleet-merge.sh ANG-Ventures/hermes-home 1838" in finished
    no_pr = _active_pr_detail("default", {**base, "pr": None, "last_outcome": None})
    assert "Wanted: **REQUEUE**" in no_pr and "fleet-merge" not in no_pr


def test_land_verb_rejects_shell_metacharacters_and_quotes_args():
    """Prism #1530: the PR URL is card text; it must never reach a shell command raw."""
    import shlex
    from gateway.kanban_watchers import _land_verb
    verb = _land_verb("https://github.com/ANG-Ventures/hermes-home/pull/1838")
    assert shlex.split(verb)[1:4] == ["ANG-Ventures/hermes-home", "1838", "--by"]
    for bad in ("https://github.com/$(id)/repo/pull/9", "https://github.com/o/r;rm -rf ~/pull/9",
                "https://github.com/o/`x`/pull/9", "https://github.com/o/../pull/9",
                "http://github.com/o/r/pull/9x", "https://evil.example/github.com/o/r/pull/9"):
        assert _land_verb(bad) is None, bad

def test_guard_stuck_pages_spend_a_bounded_time_per_tick(monkeypatch):
    """FleetReview #79: each page is a subprocess.run(timeout=30), run serially
    inside the dispatcher tick with no overall bound. Past the budget the rest
    wait (unrecorded) for the next tick."""
    import time as _time

    from gateway import kanban_watchers as kw

    monkeypatch.setattr(kw, "_GUARD_STUCK_PAGE_BUDGET_S", 0.05, raising=False)
    items = [{"task_id": f"t_{i}", "clear_verb": "x"} for i in range(5)]
    sent = []

    def slow_send(board, row):
        sent.append(row["task_id"])
        _time.sleep(0.1)
        return True

    notifier = kw._GuardStuckNotifier()
    assert notifier.observe([("default", it) for it in items], slow_send) == 1
    assert sent == ["t_0"]
    assert notifier.observe([("default", it) for it in items], slow_send) == 1
    assert sent == ["t_0", "t_1"]  # the unsent ones go out on later ticks
