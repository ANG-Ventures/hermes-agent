"""Real agent-browser subprocess + Hermes tool-session vault smoke; no real secrets."""
import json
import os
import shutil
import time

import pytest

from tools import browser_tool as bt, browser_tool_session as session, browser_vault_tool as vault
from tests.tools.test_browser_vault_target import pages  # noqa: F401

# Capture the production function before the shared CDP fixture installs its adapter.
_REAL_COMMAND = session._run_browser_command


@pytest.mark.parametrize("action", ["fill", "origin_mismatch", "tab_switch", "session_swap"])
def test_real_cli_tool_session(pages, monkeypatch, action):
    current, wrong, _, backend, _, _, _ = pages
    task = "fixture-task"
    key = bt._last_session_key(task)
    info = bt._active_sessions[key]
    # This is the same disposable Chromium as the supervisor, not a real user browser.
    info.update(session_name="vault_cli_fixture", cdp_url=pages[5].cdp_url,
                created_at=time.time(), last_activity=time.time())
    binary = os.environ.get("VAULT_TEST_AGENT_BROWSER") or shutil.which("agent-browser")
    if not binary:
        if os.environ.get("VAULT_TEST_REQUIRE_CLI") == "1":
            pytest.fail("real agent-browser binary required by this CI lane")
        pytest.skip("real agent-browser binary required")
    # Isolate package discovery from fleet PM state; execution/transport is real.
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(__import__("pathlib").Path(os.environ["HERMES_HOME"]) / "tools"))
    monkeypatch.setattr("tools.browser_tool_install._find_agent_browser", lambda **_: binary)
    monkeypatch.setattr("tools.browser_tool_install._chromium_installed", lambda: True)
    monkeypatch.setattr(session, "_run_browser_command", _REAL_COMMAND)

    def command(name, args):
        out = _REAL_COMMAND(key, name, args)
        assert out.get("success"), out
        return out.get("data") or {}

    try:
        tabs = command("tab", ["list"])
        rows = tabs.get("tabs", [])
        index = next(t["tabId"] for t in rows if t["url"].endswith("/current"))
        command("tab", [str(index)])
        assert command("eval", ["location.pathname"])["result"] == "/current"
        if action == "origin_mismatch":
            current.goto("https://accounts.example.test/login")
        elif action in {"tab_switch", "session_swap"}:
            def resolve(_):
                if action == "tab_switch":
                    other = next(t["tabId"] for t in rows if t["url"].endswith("/other"))
                    command("tab", [str(other)])
                else:
                    monkeypatch.setitem(bt._active_sessions, key, dict(info))
                return {"address_line1": "NONSECRET FIXTURE STREET"}
            backend.resolve_secret = resolve
        out = json.loads(vault.browser_vault_fill("fixture-address", task_id=task))
        assert out["success"] is (action == "fill"), out
        assert current.locator("input").input_value() == (
            "NONSECRET FIXTURE STREET" if action == "fill" else "")
        assert wrong.locator("input").input_value() == ""
    finally:
        _REAL_COMMAND(key, "close", [])
