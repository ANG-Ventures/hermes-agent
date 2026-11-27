"""Gateway completion watchers are armed at launch, not only after the turn ends.

A ``notify_on_complete`` process that finishes while its launching turn is still running
used to sit in ``pending_watchers`` until the post-turn drain, leaving the chat mute for
the whole turn (#112033).
"""

from types import SimpleNamespace

import pytest

from tools.process_registry import ProcessRegistry
from tools.terminal_tool_background import _register_completion_watcher


def _proc_session():
    return SimpleNamespace(
        id="proc_active", watcher_platform="telegram", watcher_chat_id="123",
        watcher_user_id="u", watcher_user_name="Ada", watcher_thread_id="",
        watcher_message_id="", parent_session_id="",
    )


def _install_runner(monkeypatch, runner):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)


def test_live_gateway_arms_watcher_at_registration(monkeypatch):
    armed = []
    _install_runner(monkeypatch, SimpleNamespace(arm_process_watcher=lambda w: armed.append(w) or True))
    registry = ProcessRegistry()

    _register_completion_watcher(registry, _proc_session(), "agent:main:telegram:dm:123")

    assert [w["session_id"] for w in armed] == ["proc_active"]
    assert armed[0]["notify_on_complete"] is True and armed[0]["chat_id"] == "123"
    assert registry.pending_watchers == []


def test_watcher_stays_pending_without_a_serving_gateway(monkeypatch):
    """No runner (CLI-side import) and a runner that is not serving both keep the
    startup / post-turn drain path."""
    registry = ProcessRegistry()
    _install_runner(monkeypatch, None)
    _register_completion_watcher(registry, _proc_session(), "agent:main:telegram:dm:123")

    _install_runner(monkeypatch, SimpleNamespace(arm_process_watcher=lambda w: False))
    _register_completion_watcher(registry, _proc_session(), "agent:main:telegram:dm:123")

    assert [w["session_id"] for w in registry.pending_watchers] == ["proc_active", "proc_active"]


@pytest.mark.parametrize("child,notify,patterns,fast", [
    (True, True, None, False), (True, False, ["READY"], False),
    (True, True, None, True), (False, True, None, False),
])
def test_real_background_child_never_arms_parent_delivery(tmp_path, monkeypatch, child, notify, patterns, fast):
    import json
    import threading
    from contextlib import nullcontext

    import tools.process_registry as pr
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.terminal_tool_background import spawn_background_process

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", reg)
    armed = []
    _install_runner(monkeypatch, SimpleNamespace(arm_process_watcher=lambda w: armed.append(w) or True))
    release = tmp_path / "release"
    command = "printf READY" if fast else f"while [ ! -f '{release}' ]; do sleep 0.02; done; printf READY"
    tokens = set_session_vars(platform="telegram", chat_id="123", session_id="parent",
                              session_key="agent:main:telegram:dm:123")
    try:
        with delegated_child_context("child") if child else nullcontext():
            result = json.loads(spawn_background_process(
                command=command, env=SimpleNamespace(env={}), env_type="local",
                effective_task_id="test-child", task_id="test-child", session_key="agent:main:telegram:dm:123",
                workdir=str(tmp_path), cwd=str(tmp_path), effective_pty=False,
                notify_on_complete=notify, watch_patterns=patterns, approval_note=None,
                pty_disabled_reason=None, heartbeat_seconds=60))
        session = reg.get(result["session_id"])
        print("CAPTURE", result, "actual_notify", session.notify_on_complete, "watchers", armed,
              "patterns", session.watch_patterns)
        actual_notify = session.notify_on_complete
        actual_patterns = list(session.watch_patterns)
        reg._write_checkpoint()
        checkpoint = json.loads((tmp_path / "processes.json").read_text())
        checkpoint_notify = checkpoint[0]["notify_on_complete"] if checkpoint else False
        release.touch()
        assert session._completion_event.wait(10)
        # Reader publishes after setting the exit event. Join it before inspecting the queue.
        for thread in threading.enumerate():
            if thread.name == f"proc-reader-{session.id}":
                thread.join(10)
        events = list(reg.completion_queue.queue)
        print("QUEUED", events)
        assert actual_notify is (not child and notify)
        assert bool(armed) is (not child and notify)
        assert not actual_patterns
        assert checkpoint_notify is (not child and notify)
        assert bool(events) is (not child and notify)
        if child and notify:
            assert result["notify_on_complete"] is False and "subagent_note" in result
    finally:
        release.touch()
        reg.kill_all(source="test")
        clear_session_vars(tokens)
