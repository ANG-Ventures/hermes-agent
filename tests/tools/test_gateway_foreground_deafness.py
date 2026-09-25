"""Gateway turns must not go deaf behind long foreground tool calls.

Incident (2026-09-24, Apollo gateway pid 45339): Ace went unanswered for
20-30 min in several Discord sessions. py-spy showed 8 gateway turn threads
parked in terminal-tool waits; the children were a foreground
``for i in $(seq 1 80); do gh pr view 978 ...; sleep 45; done`` loop (59 min),
a watch script (38 min) and a usage refresh (71 min). The profile's
``TERMINAL_MAX_FOREGROUND_TIMEOUT=3600`` let the model hold a messaging
session hostage for an hour, and "long work goes to background or a kanban
card" was only a prompt rule, so it kept being re-lost.

Tool-structural fixes pinned here:

1. Messaging-gateway turns get their own foreground cap
   (``terminal.gateway_max_foreground_timeout``, default 600 s) that holds
   even when the general cap was raised; CLI/TUI/kanban turns are unchanged.
2. Foreground polling loops (for/while/until + ``sleep`` >= 30 s, or
   ``watch``) are refused in messaging-gateway turns, naming the fix.
3. Every tool call dispatched through the registry that is still outstanding
   after the watchdog interval logs ``PHASE=tool_wait_long`` (grep-able,
   no py-spy needed).
"""
from __future__ import annotations

import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

INCIDENT_LOOP = (
    "for i in $(seq 1 80); do gh pr view 978 --json state,mergeStateStatus; "
    "sleep 45; done"
)


def _make_env_config(**overrides):
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    config.update(overrides)
    return config


@pytest.fixture
def discord_turn():
    """Bind a real messaging-gateway session context (as gateway/run.py does)."""
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="discord",
        source="discord",
        chat_id="c1",
        session_key="agent:main:discord:dm:c1",
        cron_session="",
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def cli_turn():
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform="", source="cli", cron_session="")
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def raised_general_cap(monkeypatch):
    """The live Apollo profile: general cap raised to 3600 via .env."""
    monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "3600")
    monkeypatch.delenv("TERMINAL_GATEWAY_MAX_FOREGROUND_TIMEOUT", raising=False)


def _run_terminal(**kwargs):
    """Drive terminal_tool with a mocked backend; return (result, mock_env)."""
    from tools.terminal_tool import terminal_tool

    config = kwargs.pop("_config", None) or _make_env_config()
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    # Gateway turns resolve a per-session env key, so hand back the mock from
    # _create_environment instead of pre-seeding one cache slot.
    with patch("tools.terminal_tool._get_env_config", return_value=config), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {}), \
         patch("tools.terminal_tool._last_activity", {}), \
         patch("tools.terminal_tool._create_environment", return_value=mock_env), \
         patch("tools.terminal_tool._check_all_guards",
               return_value={"approved": True}, create=True):
        result = json.loads(terminal_tool(**kwargs))
    return result, mock_env


# ---------------------------------------------------------------------------
# (1) gateway foreground cap
# ---------------------------------------------------------------------------
class TestGatewayForegroundCap:
    def test_gateway_turn_rejects_timeout_above_gateway_cap(self, discord_turn, raised_general_cap):
        result, env = _run_terminal(command="sleep 1", timeout=3600)
        assert result.get("error"), result
        assert "3600" in result["error"]
        assert "600" in result["error"]
        assert "background=true" in result["error"]
        env.execute.assert_not_called()

    def test_gateway_turn_allows_timeout_at_gateway_cap(self, discord_turn, raised_general_cap):
        result, env = _run_terminal(command="echo hi", timeout=600)
        assert not result.get("error"), result
        env.execute.assert_called_once()

    def test_cli_turn_keeps_the_general_cap(self, cli_turn, raised_general_cap):
        result, env = _run_terminal(command="echo hi", timeout=3600)
        assert not result.get("error"), result
        env.execute.assert_called_once()

    def test_gateway_cap_is_configurable(self, discord_turn, raised_general_cap, monkeypatch):
        monkeypatch.setenv("TERMINAL_GATEWAY_MAX_FOREGROUND_TIMEOUT", "900")
        ok, _ = _run_terminal(command="echo hi", timeout=900)
        assert not ok.get("error"), ok
        bad, _ = _run_terminal(command="echo hi", timeout=901)
        assert "900" in bad.get("error", "")

    def test_gateway_cap_never_raises_the_general_cap(self, discord_turn, monkeypatch):
        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "300")
        monkeypatch.setenv("TERMINAL_GATEWAY_MAX_FOREGROUND_TIMEOUT", "900")
        result, _ = _run_terminal(command="echo hi", timeout=400)
        assert "300" in result.get("error", "")

    def test_gateway_turn_clamps_a_long_configured_default(self, discord_turn, raised_general_cap):
        """terminal.timeout: 3600 must not smuggle an hour-long wait past the cap."""
        _, env = _run_terminal(command="echo hi", _config=_make_env_config(timeout=3600))
        assert env.execute.call_args[1]["timeout"] == 600

    def test_cli_turn_keeps_a_long_configured_default(self, cli_turn, raised_general_cap):
        _, env = _run_terminal(command="echo hi", _config=_make_env_config(timeout=900))
        assert env.execute.call_args[1]["timeout"] == 900

    def test_config_key_is_bridged(self):
        from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert (
            TERMINAL_CONFIG_ENV_MAP["gateway_max_foreground_timeout"]
            == "TERMINAL_GATEWAY_MAX_FOREGROUND_TIMEOUT"
        )
        assert DEFAULT_CONFIG["terminal"]["gateway_max_foreground_timeout"] == 600

    def test_schema_advertises_the_gateway_cap(self, monkeypatch):
        import tools.terminal_tool as tt

        monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "3600")
        monkeypatch.delenv("TERMINAL_GATEWAY_MAX_FOREGROUND_TIMEOUT", raising=False)
        text = tt._timeout_param_description()
        assert "foreground max: 3600" in text
        assert "600s in messaging-gateway sessions" in text


# ---------------------------------------------------------------------------
# (2) polling-loop guard
# ---------------------------------------------------------------------------
LOOPS_REFUSED = [
    INCIDENT_LOOP,
    "until gh pr checks 978 | grep -q pass; do sleep 60; done",
    "while true; do curl -s localhost:8080/health; sleep 30; done",
    "for i in {1..40}; do ls /tmp/x && break; sleep 1m; done",
    "watch -n 60 gh pr view 978",
    "cd /tmp && watch 'ls -la'",
]
LOOPS_ALLOWED = [
    "for i in 1 2 3; do curl -s x; sleep 5; done",           # short sleeps
    "sleep 45; gh pr view 978",                               # one wait, no loop
    'git commit -m "for i in $(seq 1 9); do sleep 60; done"',  # quoted data
    "gh run watch 123 --exit-status",                         # bounded; cap covers it
    "for f in *.py; do ruff check $f; done",                  # loop, no sleep
]


class TestPollingLoopGuard:
    @pytest.mark.parametrize("cmd", LOOPS_REFUSED)
    def test_detector_flags_polling_loops(self, cmd):
        from tools.terminal_tool import _gateway_polling_loop_guidance

        msg = _gateway_polling_loop_guidance(cmd)
        assert msg, cmd
        assert "background=true" in msg
        assert "kanban" in msg

    @pytest.mark.parametrize("cmd", LOOPS_ALLOWED)
    def test_detector_leaves_ordinary_commands_alone(self, cmd):
        from tools.terminal_tool import _gateway_polling_loop_guidance

        assert _gateway_polling_loop_guidance(cmd) is None, cmd

    def test_gateway_turn_refuses_the_incident_loop(self, discord_turn):
        result, env = _run_terminal(command=INCIDENT_LOOP, timeout=600)
        assert result.get("status") == "error" or result.get("error")
        assert "background=true" in (result.get("error") or "")
        env.execute.assert_not_called()

    def test_background_calls_skip_the_guard(self, discord_turn):
        """The guard gates FOREGROUND only; background=true is the fix it names."""
        from tools.terminal_tool import terminal_tool

        with patch("tools.terminal_tool._gateway_polling_loop_guidance") as guard, \
             patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._create_environment", return_value=MagicMock()):
            try:
                terminal_tool(command=INCIDENT_LOOP, background=True, notify_on_complete=True)
            except Exception:
                pass  # background spawn plumbing is out of scope here
        guard.assert_not_called()

    def test_cli_turn_is_unchanged(self, cli_turn):
        result, env = _run_terminal(command=INCIDENT_LOOP, timeout=600)
        assert not result.get("error"), result
        env.execute.assert_called_once()


# ---------------------------------------------------------------------------
# (3) PHASE=tool_wait_long watchdog
# ---------------------------------------------------------------------------
@pytest.fixture
def fast_watchdog(monkeypatch):
    import tools.tool_wait_watchdog as wd

    monkeypatch.setattr(wd, "TOOL_WAIT_LONG_INTERVAL_S", 0.2)
    return wd


class TestToolWaitLongWatchdog:
    def test_outstanding_call_logs_phase_line(self, fast_watchdog, caplog):
        wd = fast_watchdog
        caplog.set_level(logging.WARNING, logger="tools.tool_wait_watchdog")
        with wd.track_tool_call("terminal", {"command": "sleep 45\n  gh pr view 978"}):
            time.sleep(0.75)
        lines = [r.getMessage() for r in caplog.records if "PHASE=tool_wait_long" in r.getMessage()]
        assert lines, caplog.text
        first = lines[0]
        assert "tool=terminal" in first
        assert "cmd='sleep 45 gh pr view 978'" in first
        assert "\n" not in first
        # repeats at every interval multiple, then a single done line
        assert len([ln for ln in lines if "tool_wait_long " in ln]) >= 2
        assert any("PHASE=tool_wait_long_done" in ln for ln in lines)

    def test_fast_call_logs_nothing(self, fast_watchdog, caplog):
        wd = fast_watchdog
        caplog.set_level(logging.WARNING, logger="tools.tool_wait_watchdog")
        with wd.track_tool_call("terminal", {"command": "echo hi"}):
            pass
        time.sleep(0.4)
        assert "PHASE=tool_wait_long" not in caplog.text
        assert wd.outstanding_count() == 0

    def test_long_cmd_is_truncated(self, fast_watchdog):
        wd = fast_watchdog
        desc = wd.describe_call("execute_code", {"code": "x = 1\n" * 500})
        assert len(desc) <= wd.MAX_CMD_CHARS + 1
        assert "\n" not in desc

    def test_e2e_real_registry_dispatch_of_real_terminal(self, fast_watchdog, caplog, cli_turn, tmp_path):
        """E2E through the real tool path: registry.dispatch -> terminal_tool ->
        local backend -> a real `sleep`. The watchdog must see it outstanding."""
        import model_tools  # noqa: F401  (registers the tools)
        from tools.registry import registry

        caplog.set_level(logging.WARNING, logger="tools.tool_wait_watchdog")
        out = registry.dispatch(
            "terminal",
            {"command": "sleep 0.7; echo done-e2e", "timeout": 30, "workdir": str(tmp_path)},
        )
        assert "done-e2e" in (out if isinstance(out, str) else json.dumps(out))
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "PHASE=tool_wait_long " in m and "tool=terminal" in m and "done-e2e" in m
            for m in msgs
        ), caplog.text

    def test_e2e_gateway_turn_refusal_through_handle_function_call(self, discord_turn, raised_general_cap):
        """E2E: the model-facing dispatcher refuses the incident loop in a
        Discord turn without ever spawning the shell."""
        from model_tools import handle_function_call

        with patch("tools.terminal_tool._create_environment") as create:
            out = handle_function_call(
                "terminal", {"command": INCIDENT_LOOP, "timeout": 3600}, task_id="t-e2e"
            )
        text = out if isinstance(out, str) else json.dumps(out)
        assert "background=true" in text
        create.assert_not_called()
