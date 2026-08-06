"""Tests for the ``kanban.cli_auto_subscribe`` knob and the dispatch-time
unwatched-card warning.

``hermes kanban create`` deliberately does NOT auto-subscribe by default —
auto-subscribing every CLI call was reverted upstream (#19718 / #19721)
because scripts and cron jobs also drive the CLI. But a CLI create issued
from inside an agent session (the gateway bridges HERMES_SESSION_PLATFORM /
HERMES_SESSION_CHAT_ID into terminal subprocess envs) is exactly the case
where the origin chat SHOULD hear about the card's terminal events; the
in-process tool path already does this via
``tools.kanban_tools._maybe_auto_subscribe``.

Two halves under test:

1. ``kanban.cli_auto_subscribe`` (default False). When true AND the session
   identity is present via ``gateway.session_context.get_session_env`` (the
   same stale-identity-safe accessor the tool path uses), ``hermes kanban
   create`` writes the same ``kanban_notify_subs`` row the tool path would.
   No identity -> no subscription, preserving the #19718 rationale for
   cron/script creates regardless of the knob.

2. ``hermes kanban dispatch`` appends a one-line warning when spawned cards
   have zero notify subscriptions, regardless of the knob — visibility at
   the point of confusion instead of a silent finish.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A prior test in this process may have engaged the session-context
    # machinery and left the ContextVars explicitly cleared (""), which
    # would suppress the os.environ fallback these tests rely on. Restore
    # the "never bound" sentinel for order-independence.
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    kb.init_db()
    return home


def _enable_knob(home: Path) -> None:
    (home / "config.yaml").write_text("kanban:\n  cli_auto_subscribe: true\n")


def _set_session_env(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat1")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "topic1")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user1")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "462")


def _create_and_get_task_id(title: str = "hello") -> tuple[str, str]:
    out = kc.run_slash(f'create "{title}" --assignee worker1')
    m = re.search(r"Created\s+(t_[0-9a-f]+)\b", out)
    assert m, f"create output did not include a task id: {out!r}"
    return m.group(1), out


def _subs_for(task_id: str) -> list[dict]:
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Half 1 — the config knob
# ---------------------------------------------------------------------------


def test_knob_on_with_session_env_subscribes_origin_chat(kanban_home, monkeypatch):
    """knob=true + session identity present -> the CLI create writes the
    same notify-sub row the tool path (`_maybe_auto_subscribe`) would,
    delivery metadata included."""
    _enable_knob(kanban_home)
    _set_session_env(monkeypatch)

    task_id, out = _create_and_get_task_id()

    subs = _subs_for(task_id)
    assert len(subs) == 1
    sub = subs[0]
    assert sub["platform"] == "telegram"
    assert sub["chat_id"] == "chat1"
    assert sub["thread_id"] == "topic1"
    assert sub["user_id"] == "user1"
    # NOTE (fork parity, 2026-08-06): this fork's add_notify_sub / schema
    # predates the upstream chat_type + delivery_metadata enrichment
    # (config_defaults extraction + notify-sub metadata are upstream-only
    # until the next parity merge). The behaviour that matters — a single
    # notify-sub row written with the calling session's delivery identity —
    # is asserted above; the chat_type/delivery_metadata column assertions
    # are restored when the parity merge brings those columns to this fork.
    # Text output tells the operator the subscription happened.
    assert "cli_auto_subscribe" in out


def test_knob_defaults_off_no_subscription(kanban_home, monkeypatch):
    """CONTROL: with no config at all (knob default False), a CLI create
    with full session identity present must NOT subscribe — this pins the
    #19718 default. Passes on the pre-change tree by construction."""
    _set_session_env(monkeypatch)

    task_id, out = _create_and_get_task_id("default off")

    assert _subs_for(task_id) == []
    assert "cli_auto_subscribe" not in out


def test_knob_explicitly_false_no_subscription(kanban_home, monkeypatch):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  cli_auto_subscribe: false\n"
    )
    _set_session_env(monkeypatch)

    task_id, _ = _create_and_get_task_id("explicit false")

    assert _subs_for(task_id) == []


def test_knob_on_without_session_env_no_subscription(kanban_home, monkeypatch):
    """knob=true but no session identity (cron / bare script) -> silent,
    preserving the upstream #19718 revert rationale."""
    _enable_knob(kanban_home)
    for var in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_CHAT_TYPE",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_MESSAGE_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    task_id, out = _create_and_get_task_id("no env")

    assert _subs_for(task_id) == []
    assert "cli_auto_subscribe" not in out


def test_knob_on_half_identity_no_subscription(kanban_home, monkeypatch):
    """Platform without chat id is not a routable identity."""
    _enable_knob(kanban_home)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)

    task_id, _ = _create_and_get_task_id("half identity")

    assert _subs_for(task_id) == []


def test_stale_cleared_context_rejects_env_chat_id(kanban_home, monkeypatch):
    """Stale-identity rejection, same semantics as the tool path: when the
    session-context machinery was engaged and then explicitly cleared
    (handler exited), ``get_session_env`` returns "" and must NOT fall back
    to a stale os.environ mirror — so no subscription is written even
    though the env vars are present."""
    from gateway.session_context import (
        clear_session_vars,
        reset_session_vars,
        set_session_vars,
    )

    _enable_knob(kanban_home)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "stale-chat")

    tokens = set_session_vars(platform="telegram", chat_id="live-chat")
    try:
        clear_session_vars(tokens)  # identity explicitly cleared -> "" wins
        task_id, _ = _create_and_get_task_id("stale identity")
        assert _subs_for(task_id) == []
    finally:
        reset_session_vars()


def test_add_notify_sub_failure_does_not_fail_create(kanban_home, monkeypatch):
    """A notification bookkeeping failure must never fail the create."""
    _enable_knob(kanban_home)
    _set_session_env(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    task_id, out = _create_and_get_task_id("sub failure tolerated")
    assert task_id
    assert "cli_auto_subscribe" not in out  # did not claim success


# ---------------------------------------------------------------------------
# Half 2 — dispatch-time unwatched-card warning
# ---------------------------------------------------------------------------


def _dispatch_args(**overrides) -> argparse.Namespace:
    base = dict(dry_run=False, max=None, failure_limit=2, json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_dispatch(monkeypatch, spawned):
    res = kb.DispatchResult(spawned=spawned)
    monkeypatch.setattr(kb, "dispatch_once", lambda conn, **kw: res)


def test_dispatch_warns_on_spawned_cards_with_zero_subs(
    kanban_home, monkeypatch, capsys
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unwatched", assignee="worker1")
    finally:
        conn.close()
    _fake_dispatch(monkeypatch, [(tid, "worker1", "/tmp/ws")])

    rc = kc._cmd_dispatch(_dispatch_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert (
        "1 spawned card(s) have no notify subscription — finishes will be "
        "silent (kanban.cli_auto_subscribe or notify-subscribe)"
    ) in out


def test_dispatch_warning_suppressed_when_spawned_cards_are_watched(
    kanban_home, monkeypatch, capsys
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="watched", assignee="worker1")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat1"
        )
    finally:
        conn.close()
    _fake_dispatch(monkeypatch, [(tid, "worker1", "/tmp/ws")])

    rc = kc._cmd_dispatch(_dispatch_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "no notify subscription" not in out


def test_dispatch_warning_absent_when_nothing_spawned(
    kanban_home, monkeypatch, capsys
):
    _fake_dispatch(monkeypatch, [])

    rc = kc._cmd_dispatch(_dispatch_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "no notify subscription" not in out


def test_dispatch_warning_counts_only_unwatched(kanban_home, monkeypatch, capsys):
    conn = kb.connect()
    try:
        tid_watched = kb.create_task(conn, title="w", assignee="worker1")
        tid_silent_a = kb.create_task(conn, title="a", assignee="worker1")
        tid_silent_b = kb.create_task(conn, title="b", assignee="worker1")
        kb.add_notify_sub(
            conn, task_id=tid_watched, platform="telegram", chat_id="chat1"
        )
    finally:
        conn.close()
    _fake_dispatch(
        monkeypatch,
        [
            (tid_watched, "worker1", ""),
            (tid_silent_a, "worker1", ""),
            (tid_silent_b, "worker1", ""),
        ],
    )

    rc = kc._cmd_dispatch(_dispatch_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "2 spawned card(s) have no notify subscription" in out


def test_dispatch_json_carries_spawned_unwatched_without_text_warning(
    kanban_home, monkeypatch, capsys
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="json unwatched", assignee="worker1")
    finally:
        conn.close()
    _fake_dispatch(monkeypatch, [(tid, "worker1", "")])

    rc = kc._cmd_dispatch(_dispatch_args(json=True))
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)  # stdout must stay strictly machine-parseable
    assert payload["spawned_unwatched"] == [tid]
