"""Every agent-facing background-completion turn must tell the model how to stay silent.

Contract (Ace, 2026-09-27): the gateway drops a reply that is exactly the silence token
(gateway/response_filters.SILENT_REPLY_TOKEN); an injected completion turn that never
names that token makes agents answer their own completions with "already handled" posts.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.response_filters import SILENT_REPLY_TOKEN, is_intentional_silence_response
from gateway.run import GatewayRunner
from tools.process_registry import COMPLETION_SILENCE_HINT, format_process_notification


def test_hint_names_the_token_the_gateway_actually_suppresses():
    assert SILENT_REPLY_TOKEN in COMPLETION_SILENCE_HINT
    assert is_intentional_silence_response(SILENT_REPLY_TOKEN)


def test_single_process_completion_carries_hint_for_every_status():
    for evt in (
        {"type": "completion", "session_id": "proc_a", "command": "true", "exit_code": 0, "output": ""},
        {"type": "completion", "session_id": "proc_b", "command": "false", "exit_code": 1, "output": "boom"},
        {"type": "completion", "session_id": "proc_c", "command": "x", "exit_code": -15,
         "completion_reason": "killed", "output": ""},
    ):
        text = format_process_notification(evt)
        assert COMPLETION_SILENCE_HINT in text and text.endswith("]")


def test_coalesced_process_batch_carries_hint_and_no_ambiguous_wording():
    entries = [("t", {"session_id": f"proc_{i}", "exit_code": 0, "output": "ok"}, None) for i in range(3)]
    text = GatewayRunner._format_coalesced_process_completions(entries)
    assert COMPLETION_SILENCE_HINT in text and "absorb it silently" not in text


def test_coalesced_delegation_batch_carries_hint():
    text = GatewayRunner._format_coalesced_async_delegations(["[A]", "[B]"])
    assert COMPLETION_SILENCE_HINT in text and "absorb it silently" not in text


# ---------------------------------------------------------------------------
# Mechanism, not doc (t_ec17f15f): the hint above was present in the injected
# turn and the model still replied, 3x (09-27 #health, 09-29 #context
# 1554641732849639485, 09-30 #context 1554855776600850496). Ace 2026-09-30:
# display.background_process_agent_notify (default off) keeps the echo from
# reaching the agent; bounded jobs use process(action=wait).
# ---------------------------------------------------------------------------

class _OneShotRegistry:
    def __init__(self, session):
        self._sessions = [session]

    def get(self, session_id):
        return self._sessions.pop(0) if self._sessions else None

    def is_completion_consumed(self, session_id):
        return False

    def suppress_completion(self, session, replay):
        if getattr(session, "completion_required", False):
            return False
        session._replay_completion = replay
        return True


def _run_watcher(monkeypatch, tmp_path, *, exit_code, output, mode=None,
                 completion_reason="exited", completion_required=False,
                 producer_mode=None):
    import gateway.run as gateway_run
    import tools.process_registry as pr_module

    yaml = "display:\n  background_process_notifications: concise\n"
    if mode is not None:
        yaml += f"  background_process_agent_notify: {mode}\n"
    (tmp_path / "config.yaml").write_text(yaml, encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    session = SimpleNamespace(
        output_buffer=output, exited=True, exit_code=exit_code,
        command="git-land-private.sh", completion_reason=completion_reason,
        termination_source="", started_at=0.0, completion_required=completion_required,
    )
    monkeypatch.setattr(pr_module, "process_registry", _OneShotRegistry(session))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_enqueue_process_completion_notification", enqueue)

    watcher = {
        "session_id": "proc_land", "check_interval": 0, "platform": "telegram",
        "chat_id": "123", "notify_on_complete": True,
    }
    if producer_mode is not None:
        watcher["agent_notify_mode"] = producer_mode
    asyncio.run(runner._run_process_watcher(watcher))
    return enqueue, adapter


def _assert_silent(enqueue, adapter):
    enqueue.assert_not_awaited()
    adapter.send.assert_not_awaited()          # no fall-through to a chat post
    adapter.handle_message.assert_not_awaited()


@pytest.mark.parametrize("mode", [None, "off"])  # None = unset -> default off
@pytest.mark.parametrize("exit_code,output", [(0, ""), (0, "landed 23e6cd458\n"), (1, "boom\n")])
def test_off_injects_no_agent_turn_for_any_exit(monkeypatch, tmp_path, mode, exit_code, output):
    _assert_silent(*_run_watcher(monkeypatch, tmp_path, exit_code=exit_code,
                                 output=output, mode=mode))


@pytest.mark.parametrize("exit_code,output", [(0, ""), (1, ""), (0, "ok\n")])
def test_on_still_injects_every_completion(monkeypatch, tmp_path, exit_code, output):
    enqueue, _ = _run_watcher(monkeypatch, tmp_path, exit_code=exit_code, output=output, mode="on")
    enqueue.assert_awaited_once()
    text, evt = enqueue.await_args.args
    assert evt["exit_code"] == exit_code and COMPLETION_SILENCE_HINT in text


@pytest.mark.parametrize("output", ["", "   \n\t\n"])
def test_empty_success_mode_silences_clean_silent_exit(monkeypatch, tmp_path, output):
    _assert_silent(*_run_watcher(monkeypatch, tmp_path, exit_code=0, output=output,
                                 mode="empty-success"))


@pytest.mark.parametrize("exit_code,output,reason", [
    (1, "", "exited"),                  # failure, no output
    (0, "landed 23e6cd458\n", "exited"),  # success with output
    (0, "", "killed"),                  # terminated, not a clean exit
])
def test_empty_success_mode_still_injects_the_rest(monkeypatch, tmp_path, exit_code, output, reason):
    enqueue, _ = _run_watcher(monkeypatch, tmp_path, exit_code=exit_code, output=output,
                              mode="empty-success", completion_reason=reason)
    enqueue.assert_awaited_once()


def test_knob_default_is_off_and_declared_in_schema(monkeypatch, tmp_path):
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    import gateway.run as gateway_run

    assert DEFAULT_CONFIG["display"]["background_process_agent_notify"] == "off"
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    for yaml, want in [("display: {}\n", "off"), ("display:\n  background_process_agent_notify: bogus\n", "off"),
                       ("display:\n  background_process_agent_notify: false\n", "off"),
                       ("display:\n  background_process_agent_notify: true\n", "on"),
                       ("display:\n  background_process_agent_notify: empty_success\n", "empty-success")]:
        (tmp_path / "config.yaml").write_text(yaml, encoding="utf-8")
        assert GatewayRunner._load_background_agent_notify_mode() == want, yaml


def test_terminal_schema_steers_bounded_jobs_to_process_wait():
    from tools.terminal_tool import TERMINAL_SCHEMA, TERMINAL_TOOL_DESCRIPTION

    props = TERMINAL_SCHEMA["parameters"]["properties"]
    notify = props["notify"]["description"]
    background = props["background"]["description"]
    for text in (notify, background, TERMINAL_TOOL_DESCRIPTION):
        assert "process(action" in text and "wait" in text
        assert "nearly every bounded" not in text
        assert "add notify=true for bounded" not in text
        assert "Pair with notify=true" not in text
    assert "Do NOT use notify=true for bounded jobs" in notify
    assert "notify=['pattern'" in notify   # readiness patterns stay documented


# Prism P1 (round 1): flows that structurally depend on the completion turn must
# still get it under the default-off knob.
@pytest.mark.parametrize("mode", [None, "off", "empty-success"])
def test_completion_required_session_injects_under_suppressing_modes(monkeypatch, tmp_path, mode):
    enqueue, _ = _run_watcher(monkeypatch, tmp_path, exit_code=0, output="", mode=mode,
                              completion_required=True)
    enqueue.assert_awaited_once()


def test_require_completion_marks_by_session_id_and_pid():
    from tools.process_registry import ProcessRegistry, ProcessSession

    reg = ProcessRegistry()
    a = ProcessSession(id="proc_a", command="x", pid=4242)
    b = ProcessSession(id="proc_b", command="y", pid=4343)
    reg._running[a.id] = a
    reg._finished[b.id] = b
    assert not a.completion_required and not b.completion_required
    assert reg.require_completion(session_id="proc_b") == 1 and b.completion_required
    assert reg.require_completion(pid=4242) == 1 and a.completion_required
    assert reg.require_completion(session_id="nope", pid=1) == 0
    # marking never turns a silent (non-notify) process into a notifying one
    assert not a.notify_on_complete


def test_goal_wait_barriers_mark_the_process(monkeypatch):
    import hermes_cli.goals as goals
    import tools.process_registry as pr_module

    calls = []

    class _Reg:
        def require_completion(self, session_id=None, pid=None):
            calls.append((session_id, pid))
            return 1

    monkeypatch.setattr(pr_module, "process_registry", _Reg())
    goals._DB_CACHE.clear()
    try:
        mgr = goals.GoalManager(session_id="t-ec17-goal")
        mgr.set("ship it", max_turns=5)
        mgr.wait_on_session("proc_ci", reason="CI")
        mgr.wait_on(4242, reason="CI pid")
    finally:
        goals._DB_CACHE.clear()
    assert calls == [("proc_ci", None), (None, 4242)]


def test_bot_dm_delivery_spawn_requires_completion(monkeypatch):
    import json
    import tools.bot_mode_dm as bot_mode_dm
    import tools.terminal_tool as terminal_tool_module

    calls = []

    def fake_terminal_tool(command, **kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "started", "session_id": "proc_dm1"})

    monkeypatch.setattr(terminal_tool_module, "terminal_tool", fake_terminal_tool)
    bot_mode_dm._spawn_delivery("true", "@peer", dm_file=None, task_id=None, agent=None)
    assert calls and calls[0]["notify_on_complete"] is True
    assert calls[0]["_completion_required"] is True


# Prism round 2.
@pytest.mark.parametrize("producer,config,injects", [("on", "off", True), ("off", "on", False)])
def test_mode_resolved_in_producing_profile_wins(monkeypatch, tmp_path, producer, config, injects):
    enqueue, _ = _run_watcher(monkeypatch, tmp_path, exit_code=1, output="x\n", mode=config,
                              producer_mode=producer)
    assert enqueue.await_count == (1 if injects else 0)


def test_goal_parking_after_suppressed_exit_replays_the_turn(monkeypatch, tmp_path):
    """Lost-wakeup race: the process exits (turn suppressed) BEFORE the goal
    parks; require_completion must replay the stashed completion turn."""
    import gateway.run as gateway_run
    import tools.process_registry as pr_module
    from tools.process_registry import ProcessRegistry, ProcessSession

    (tmp_path / "config.yaml").write_text("display: {}\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "procs.json")
    reg = ProcessRegistry()
    sess = ProcessSession(id="proc_ci", command="ci-watch", exited=True, exit_code=0,
                          notify_on_complete=True)
    reg._finished[sess.id] = sess
    monkeypatch.setattr(pr_module, "process_registry", reg)

    runner = GatewayRunner(GatewayConfig())
    runner.adapters[Platform.TELEGRAM] = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_enqueue_process_completion_notification", enqueue)

    async def main():
        await runner._run_process_watcher({
            "session_id": "proc_ci", "check_interval": 0, "platform": "telegram",
            "chat_id": "123", "notify_on_complete": True,
        })
        assert enqueue.await_count == 0          # default off: suppressed
        # goal parks from the agent thread, after the exit
        assert await asyncio.to_thread(reg.require_completion, session_id="proc_ci") == 1
        for _ in range(50):
            if enqueue.await_count:
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[1]["session_id"] == "proc_ci"
    # replay is one-shot
    assert sess._replay_completion is None


def test_completion_required_and_mode_survive_checkpoint_round_trip(monkeypatch, tmp_path):
    import json
    import os
    import tools.process_registry as pr_module
    from tools.process_registry import ProcessRegistry, ProcessSession

    ckpt = tmp_path / "procs.json"
    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", ckpt)
    reg = ProcessRegistry()
    s = ProcessSession(id="proc_live", command="sleep 999", pid=os.getpid(), task_id="t1",
                       notify_on_complete=True, completion_required=True, agent_notify_mode="on")
    reg._running[s.id] = s
    reg._write_checkpoint()
    entry = json.loads(ckpt.read_text())[0]
    assert entry["completion_required"] is True and entry["agent_notify_mode"] == "on"

    reg2 = ProcessRegistry()
    assert reg2.recover_from_checkpoint() == 1
    r = reg2.get("proc_live")
    assert r.completion_required is True and r.agent_notify_mode == "on"


@pytest.mark.parametrize("raw,want", [(None, "off"), ("", "off"), (False, "off"), (True, "on"),
                                      ("EMPTY_SUCCESS", "empty-success"), ("bogus", "off")])
def test_normalize_agent_notify_mode(raw, want):
    from tools.process_registry import normalize_agent_notify_mode
    assert normalize_agent_notify_mode(raw) == want


def test_spawn_stamps_producer_profile_mode(tmp_path, monkeypatch):
    import tools.process_registry as pr_module
    import yaml
    home = __import__("os").environ["HERMES_HOME"]
    from pathlib import Path
    Path(home, "config.yaml").write_text(
        yaml.safe_dump({"display": {"background_process_agent_notify": "empty_success"}}))
    assert pr_module.resolve_agent_notify_mode() == "empty-success"
