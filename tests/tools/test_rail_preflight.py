"""Fail-fast rail preflight for kanban workers (t_db491366).

A worker spilled to a pool host must learn on its FIRST browser/computer_use call that the
operator's browser or screen does not exist there, with a message that names the fix, and the
worker loop must park the card ``needs_input`` with that message instead of letting the model
retry. Hermetic: no real CDP, no real board outside ``tmp_path``.
"""

from __future__ import annotations

import json
import socket
import sys
from types import SimpleNamespace

import pytest

from tools import rail_preflight as rp
from hermes_cli import kanban_worker_hosts as kwh


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rp._emitted.clear()
    rp._probe_ok.clear()
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.delenv(kwh.PLACEMENT_ENV, raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    rp._emitted.clear()


def _worker(monkeypatch, task="t_rail0001"):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)  # fail-open grant == owning worker


def _placed(monkeypatch, host="ace-ai"):
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": host, "env": {"TERMINAL_ENV": "ssh"}}))


class _Resp:
    def __init__(self, status):
        self.status_code = status


def _cdp_requests(monkeypatch, *, answers: bool):
    import requests
    calls = []

    def fake_get(url, timeout=None, **kw):
        calls.append((url, timeout))
        if not answers:
            raise requests.ConnectionError("connection refused")
        return _Resp(200)
    monkeypatch.setattr(requests, "get", fake_get)
    return calls


EXPECTED_BROWSER = (
    "BROWSER_RAIL_ABSENT: no browser session on {host}. This card needs Ace's logged-in browser, "
    "which lives only on the Mac Studio. Add the line `host: studio` to the card body (or "
    "`requires: browser-session` once the affinity spec lands) and re-run; do not retry here."
)


def _navigate(monkeypatch, cdp_url):
    """browser_navigate through the REAL registry dispatch with a configured CDP override."""
    monkeypatch.setenv("BROWSER_CDP_URL", cdp_url)
    import model_tools  # noqa: F401  (triggers tool discovery)
    from tools.registry import registry
    return json.loads(registry.dispatch("browser_navigate", {"url": "https://example.com"}, task_id="rail"))


def test_placed_worker_with_live_loopback_cdp_gets_exact_message(monkeypatch):
    # The ace-ai case: a different Brave answers 127.0.0.1:9222, so liveness passes — placement must still refuse.
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-ai")
    calls = _cdp_requests(monkeypatch, answers=True)
    out = _navigate(monkeypatch, "http://127.0.0.1:9222")
    assert out.get("success") is False
    assert out["error"] == EXPECTED_BROWSER.format(host="ace-ai")
    assert calls == [], "a placed worker is refused before any CDP probe"


def test_unplaced_worker_unreachable_endpoint_gets_exact_message(monkeypatch):
    _worker(monkeypatch)
    monkeypatch.setattr(socket, "gethostname", lambda: "ace-media")
    calls = _cdp_requests(monkeypatch, answers=False)
    out = _navigate(monkeypatch, "http://127.0.0.1:9222")
    assert out["error"] == EXPECTED_BROWSER.format(host="ace-media")
    assert calls and calls[0][0] == "http://127.0.0.1:9222/json/version" and calls[0][1] == 2.0


def test_studio_worker_with_reachable_endpoint_passes_through(monkeypatch):
    _worker(monkeypatch)
    monkeypatch.setattr(socket, "gethostname", lambda: "mac-studio-m3u")
    _cdp_requests(monkeypatch, answers=True)
    assert rp.browser_rail_error("http://127.0.0.1:9222") is None
    assert rp.take_rail_absent() is None


def test_non_worker_session_is_never_refused(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    _placed(monkeypatch)
    _cdp_requests(monkeypatch, answers=False)
    assert rp.browser_rail_error("http://127.0.0.1:9222") is None


def test_remote_cdp_or_no_override_is_not_a_rail(monkeypatch):
    # A remote endpoint, a cloud provider, or the agent's own Chromium (no override) is not Ace's browser.
    _worker(monkeypatch)
    _placed(monkeypatch)
    assert rp.browser_rail_error("ws://10.0.0.5:9222") is None
    assert rp.browser_rail_error("") is None


def test_placed_worker_sandbox_browser_failure_reports_rail(monkeypatch):
    # Own-Chromium path: placement onto the pool host's sandbox failed -> name the Studio fix.
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-media")
    from tools import browser_tool_session as bts
    from tools.bot_desktop import runtime as bd

    def refused():
        raise RuntimeError("Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    monkeypatch.setattr(bd, "tool_placement", refused)
    assert bts._browser_command_preflight() == {"success": False, "error": EXPECTED_BROWSER.format(host="ace-media")}


def test_unplaced_sandbox_failure_keeps_generic_error(monkeypatch):
    _worker(monkeypatch)
    from tools import browser_tool_session as bts
    from tools.bot_desktop import runtime as bd

    def refused():
        raise RuntimeError("no screen")
    monkeypatch.setattr(bd, "tool_placement", refused)
    assert bts._browser_command_preflight()["error"] == "The browser cannot run here: no screen"


@pytest.mark.parametrize("url,expect", [
    ("http://127.0.0.1:9222", ("http", "http://127.0.0.1:9222/json/version")),
    ("http://127.0.0.1:9222/session", ("http", "http://127.0.0.1:9222/session/json/version")),
    ("ws://127.0.0.1:9222", ("http", "http://127.0.0.1:9222/json/version")),
    ("ws://127.0.0.1:9222/devtools/browser/abc", ("tcp", ("127.0.0.1", 9222))),
    ("ws://127.0.0.1:3000/cdp-proxy", ("tcp", ("127.0.0.1", 3000))),
    ("ws://127.0.0.1:3000/", ("tcp", ("127.0.0.1", 3000))),  # resolver passes a root-path WS URL through
    ("127.0.0.1:9222", ("http", "http://127.0.0.1:9222/json/version")),
])
def test_probe_follows_resolver_discovery_semantics(url, expect):
    assert rp._probe_target(url) == expect


@pytest.mark.parametrize("url", ["ws://127.0.0.1:3000/", "ws://127.0.0.1:9222", "http://127.0.0.1:9222/session",
                                 "ws://127.0.0.1:9222/devtools/browser/abc", "wss://127.0.0.1:443/x"])
def test_probe_discovers_exactly_when_the_resolver_does(url, monkeypatch):
    # Contract between the two: the probe hits HTTP discovery iff _resolve_cdp_override would.
    import requests
    from tools import browser_tool_cdp as cdp
    seen = []
    monkeypatch.setattr(requests, "get", lambda u, **kw: seen.append(u) or (_ for _ in ()).throw(requests.ConnectionError()))
    cdp._resolve_cdp_override(url)
    kind, target = rp._probe_target(url)
    assert (kind == "http") == bool(seen)
    if seen:
        assert target == seen[0]


def test_unplaced_linux_worker_keeps_its_own_screen(monkeypatch):
    # Bot Desktop / native Linux/Windows screens are supported: only a placed worker lacks the rail.
    _worker(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    assert rp.screen_rail_error() is None


EXPECTED_SCREEN = (
    "SCREEN_RAIL_ABSENT: no screen session on {host}. This card needs Ace's logged-in screen session, "
    "which lives only on the Mac Studio. Add the line `host: studio` to the card body (or "
    "`requires: browser-session` once the affinity spec lands) and re-run; do not retry here."
)


def _computer_use(monkeypatch, *, backend_error=None):
    """handle_computer_use with the lease/screen start stubbed and the backend either failing or working."""
    import contextlib
    from tools.bot_desktop import lease, runtime as bd
    from tools.computer_use import tool as cu
    monkeypatch.setattr(lease, "assert_agent_may_act", lambda: SimpleNamespace(epoch=0))
    monkeypatch.setattr(lease, "get", lambda: SimpleNamespace(epoch=0))
    monkeypatch.setattr(bd, "ensure_started_for_tool", lambda: None)
    calls = []

    class _Backend:
        def __getattr__(self, name):
            def op(*a, **k):
                calls.append(name)
                return {"ok": True}
            return op

    @contextlib.contextmanager
    def backend_for_call(session_id=""):
        if backend_error:
            raise RuntimeError(backend_error)
        yield _Backend()
    monkeypatch.setattr(cu, "_backend_for_call", backend_for_call)
    out = cu.handle_computer_use({"action": "list_apps"})
    return (json.loads(out) if isinstance(out, str) else out), calls


def test_computer_use_placed_worker_with_own_desktop_keeps_working(monkeypatch):
    # ace-ai's Bot Desktop inside the ssh sandbox is the worker's own screen: never a rail refusal.
    _worker(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    _placed(monkeypatch, "ace-ai")
    out, calls = _computer_use(monkeypatch)
    assert "RAIL_ABSENT" not in json.dumps(out)
    assert calls, "the backend was reached"
    assert rp.take_rail_absent() is None


def test_computer_use_placed_worker_without_desktop_gets_screen_message(monkeypatch):
    _worker(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    _placed(monkeypatch, "ace-ai")
    out, _ = _computer_use(monkeypatch, backend_error="Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    assert out["error"] == EXPECTED_SCREEN.format(host="ace-ai")
    assert "Xvnc" in out["detail"]
    assert rp.take_rail_absent() == EXPECTED_SCREEN.format(host="ace-ai")


def test_computer_use_unplaced_backend_failure_keeps_generic_error(monkeypatch):
    _worker(monkeypatch)
    out, _ = _computer_use(monkeypatch, backend_error="cua-driver missing")
    assert out["error"] == "computer_use backend unavailable: cua-driver missing"
    assert rp.take_rail_absent() is None


def test_background_cleanup_failure_never_queues_a_rail(monkeypatch):
    # The inactivity janitor's ``close`` hits the same preflight; only an active tool call may park.
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-media")
    from tools import browser_tool_session as bts
    from tools.bot_desktop import runtime as bd

    def refused():
        raise RuntimeError("Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    monkeypatch.setattr(bd, "tool_placement", refused)
    res = bts._run_browser_command("rail", "close", [], timeout=10)
    assert res["error"] == EXPECTED_BROWSER.format(host="ace-media")
    assert rp.take_rail_absent() is None


def test_active_browser_tool_call_with_sandbox_failure_queues_rail(monkeypatch):
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-media")
    from tools.bot_desktop import runtime as bd

    def refused():
        raise RuntimeError("Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    monkeypatch.setattr(bd, "tool_placement", refused)
    out = _navigate(monkeypatch, "")
    assert out["error"] == EXPECTED_BROWSER.format(host="ace-media")
    assert rp.take_rail_absent() == EXPECTED_BROWSER.format(host="ace-media")


def test_adapter_that_drops_the_error_still_queues_rail(monkeypatch):
    # browser_console swallows per-command failures; the rail must still park the card.
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-media")
    from tools.bot_desktop import runtime as bd
    from tools.registry import registry
    import model_tools  # noqa: F401

    def refused():
        raise RuntimeError("Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    monkeypatch.setattr(bd, "tool_placement", refused)
    registry.dispatch("browser_console", {}, task_id="rail")
    assert rp.take_rail_absent() == EXPECTED_BROWSER.format(host="ace-media")


def test_browser_cdp_on_placed_worker_uses_dispatcher_side_liveness(monkeypatch):
    # browser_cdp connects from the dispatcher process: a placed worker's loopback CDP IS the Studio's.
    _worker(monkeypatch)
    _placed(monkeypatch, "ace-ai")
    _cdp_requests(monkeypatch, answers=True)
    assert rp.browser_rail_error("http://127.0.0.1:9222", host_side=True) is None
    _cdp_requests(monkeypatch, answers=False)
    rp._probe_ok.clear()
    monkeypatch.setattr(socket, "gethostname", lambda: "mac-studio-m3u")
    assert rp.browser_rail_error("http://127.0.0.1:9222", host_side=True) == EXPECTED_BROWSER.format(host="mac-studio-m3u")


def test_pending_rail_is_drained_only_by_its_owning_worker(monkeypatch):
    _worker(monkeypatch, "t_owner001")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    _placed(monkeypatch, "ace-ai")
    message = rp.browser_rail_error("http://127.0.0.1:9222")
    parked = []
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda *a, **k: parked.append(1) or (_ for _ in ()).throw(AssertionError("parked")))
    from agent.delegation_context import non_dispatcher_owned_context
    with non_dispatcher_owned_context():  # in-process cron / delegate turn in the same process
        assert rp.take_rail_absent() is None
        from hermes_cli import kanban_worker_exit as kwe
        assert kwe.block_on_rail_absent(message) is False
    assert parked == [], "an in-process cron/delegate turn must never touch the worker's card"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other002")
    assert rp.take_rail_absent() is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owner001")
    assert rp.take_rail_absent() == message
    assert rp.take_rail_absent() is None


def test_rail_absent_parks_card_needs_input_verbatim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_exit as kwe
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="rail probe", assignee="daedalus")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    _worker(monkeypatch, tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    message = EXPECTED_BROWSER.format(host="ace-ai")
    assert kwe.block_on_rail_absent("some other error") is False
    assert kwe.block_on_rail_absent(message) is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert kb._latest_block_reason(conn, tid) == message
    finally:
        conn.close()


def test_tool_round_breaks_on_rail_absent_instead_of_retrying(monkeypatch):
    """The turn loop parks the card and ends the turn after the round that emitted the rail error."""
    from types import SimpleNamespace
    from agent import turn_tool_round as ttr
    from hermes_cli import kanban_worker_exit as kwe

    _worker(monkeypatch)
    _placed(monkeypatch, "ace-ai")
    parked = []
    monkeypatch.setattr(kwe, "block_on_rail_absent", lambda m: parked.append(m) or True)
    monkeypatch.setattr(ttr, "validate_tool_calls",
                        lambda *a, **k: SimpleNamespace(action="run", mixed_invalid_batch=False))
    monkeypatch.setattr(ttr, "stage_tool_call_message",
                        lambda agent, **k: ({"role": "assistant", "content": "", "tool_calls": []}, False))

    def execute(assistant_message, messages, task_id, n):
        rp.browser_rail_error("http://127.0.0.1:9222")
        messages.append({"role": "tool", "content": "x", "tool_call_id": "c1"})

    agent = SimpleNamespace(
        quiet_mode=True, verbose_logging=False, valid_tool_names={"browser_navigate"},
        _deduplicate_tool_calls=lambda c: c, _cap_delegate_task_calls=lambda c: c,
        _flush_messages_to_session_db=lambda *a: True, _emit_interim_assistant_message=lambda m: None,
        stream_delta_callback=None, _execute_tool_calls=execute, _incremental_persistence_failed=False,
        _tool_guardrail_halt_decision=None,
    )
    monkeypatch.setattr("hermes_cli.observability.shared_metrics_harness.finish_tool_round", lambda a: None)
    tc = SimpleNamespace(function=SimpleNamespace(name="browser_navigate", arguments="{}"), id="c1")
    msgs: list = []
    v = ttr.run_tool_round(
        agent, assistant_message=SimpleNamespace(tool_calls=[tc], content=""), finish_reason="tool_calls",
        messages=msgs, conversation_history=[], api_call_count=1, effective_task_id="rail", user_message="u",
        system_message=None, active_system_prompt=None, compression_attempts=0, max_compression_attempts=3,
        final_response=None, failed=False, _turn_exit_reason=None, truncated_tool_call_retries=0,
        current_turn_user_idx=0,
    )
    assert v.action == "break"
    assert v._turn_exit_reason == "rail_absent"
    assert parked == [EXPECTED_BROWSER.format(host="ace-ai")]
    assert v.final_response == parked[0]
