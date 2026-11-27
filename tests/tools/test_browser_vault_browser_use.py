"""Browser Use (the default backend): vault writes bind to the harness daemon's CURRENT target.

Real disposable Chromium + the real CDP supervisor. The harness daemon is emulated at its IPC
boundary (``_driver_query``) for the race matrix; ``test_real_harness_*`` drive the real
browser-harness CLI and daemon. Only synthetic, non-secret address data is used.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import browser_tool as bt, browser_use_cli as bu, browser_vault_tool as vault
from tools.browser_supervisor import CDPSupervisor, SUPERVISOR_REGISTRY

TASK = "fixture-task"
VALUE = "NONSECRET FIXTURE STREET"


def _target_id(page):
    return page.context.new_cdp_session(page).send("Target.getTargetInfo")["targetInfo"]["targetId"]


@pytest.fixture
def bu_pages(monkeypatch):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--remote-debugging-port=0", "--enable-automation"])
        context = browser.new_context()
        context.route("https://*.example.test/**", lambda route: route.fulfill(
            body='<input autocomplete="address-line1" name="address">', content_type="text/html"))
        wrong = context.new_page()          # same origin, opened FIRST: an origin search would pick it
        wrong.goto("https://site.example.test/other")
        current = context.new_page()
        current.goto("https://site.example.test/current")
        line = browser.new_browser_cdp_session().send("Browser.getBrowserCommandLine")["arguments"]
        profile = next(a.split("=", 1)[1] for a in line if a.startswith("--user-data-dir="))
        port, ws_path = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[:2]
        ws = f"ws://127.0.0.1:{port}{ws_path}"
        sup = CDPSupervisor(TASK, ws)
        sup.start()
        registry = {TASK: sup}
        monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task: registry.get(task))
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: True)

        def no_builtin(*_a, **_k):
            raise AssertionError("Browser Use vault ops must never drive the built-in browser stack")
        monkeypatch.setattr("tools.browser_tool_session._run_browser_command", no_builtin)
        monkeypatch.setattr(bu, "_LAST_ROUTE", {})
        bu._record_route(TASK, "", {"BU_CDP_WS": ws}, sup)

        # The harness daemon per BU_NAME: its attached target and pid.
        daemons = {"": {"page": current, "pid": 101}}
        calls = []

        def query(route, expression):
            calls.append(route["session"])
            daemon = daemons.get(route["session"])
            if daemon is None:
                return None
            page = daemon["page"]
            tid = _target_id(page)
            return {"target_id": tid, "session_id": f"s-{tid}", "pid": daemon["pid"],
                    "value": page.evaluate(expression)}
        monkeypatch.setattr(bu, "_driver_query", query)

        meta = SimpleNamespace(id="fixture-address", kind="address", fill_supported=True,
                               origin="https://site.example.test", allowed_origins=(), label="Fixture")
        backend = SimpleNamespace(name="fixture", needs_unlock=False, get_meta=lambda _: meta,
                                  resolve_secret=lambda _: {"address_line1": VALUE})
        monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
        try:
            yield SimpleNamespace(current=current, wrong=wrong, daemons=daemons, backend=backend,
                                  sup=sup, registry=registry, ws=ws, calls=calls)
        finally:
            sup.stop()
            browser.close()


def fill(task=TASK):
    return json.loads(vault.browser_vault_fill("fixture-address", task_id=task))


def values(env):
    return env.current.locator("input").input_value(), env.wrong.locator("input").input_value()


def test_fills_the_harness_current_page_not_the_first_same_origin_tab(bu_pages):
    out = fill()
    assert out["success"], out
    assert values(bu_pages) == (VALUE, "")


def test_no_browser_exec_route_refuses(bu_pages, monkeypatch):
    monkeypatch.setattr(bu, "_LAST_ROUTE", {})
    out = fill()
    assert out["error_type"] == "page_binding_required", out
    assert bu_pages.calls == [] and values(bu_pages) == ("", "")


def test_camofox_stays_fail_closed(bu_pages, monkeypatch):
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    out = fill()
    assert out["error_type"] == "page_binding_required", out
    assert bu_pages.calls == [] and values(bu_pages) == ("", "")


def test_foreign_task_refuses(bu_pages):
    out = fill("foreign-task")
    assert not out["success"], out
    assert bu_pages.calls == [] and values(bu_pages) == ("", "")


def _race(env, monkeypatch, race):
    if race == "navigation":
        env.current.goto("https://site.example.test/replaced")
    elif race == "cross_origin_navigation":
        env.current.goto("https://accounts.example.test/replaced")
    elif race == "tab_switch":
        env.daemons[""]["page"] = env.wrong
    elif race == "session_swap":       # browser_exec drove another BU_NAME daemon
        env.daemons["other"] = {"page": env.current, "pid": 202}
        bu._record_route(TASK, "other", {"BU_CDP_WS": env.ws}, env.sup)
    elif race == "daemon_restart":     # same name, new daemon process re-attached to the same tab
        env.daemons[""]["pid"] = 303
    elif race == "supervisor_replaced":
        env.registry[TASK] = SimpleNamespace(focus_page=lambda *a, **k: {"ok": False})
    elif race == "backend_switch":
        monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: False)


RACES = ["navigation", "cross_origin_navigation", "tab_switch", "session_swap", "daemon_restart",
         "supervisor_replaced", "backend_switch"]


@pytest.mark.parametrize("race", RACES)
def test_change_during_resolution_refuses(bu_pages, monkeypatch, race):
    def resolve(_):
        _race(bu_pages, monkeypatch, race)
        return {"address_line1": VALUE}
    bu_pages.backend.resolve_secret = resolve
    out = fill()
    assert not out["success"], out
    assert values(bu_pages) == ("", "")


@pytest.mark.parametrize("race", RACES)
def test_change_after_target_selection_refuses(bu_pages, monkeypatch, race):
    focus = bu_pages.sup.focus_page

    def raced(*args, **kwargs):
        result = focus(*args, **kwargs)
        _race(bu_pages, monkeypatch, race)
        return result
    monkeypatch.setattr(bu_pages.sup, "focus_page", raced)
    out = fill()
    assert not out["success"], out
    assert values(bu_pages) == ("", "")


def test_supervisor_selecting_another_tab_refuses(bu_pages, monkeypatch):
    """A second document carrying the witness (e.g. a same-origin opener copying it) must not be
    written just because the supervisor enumerated it first: the harness named its target."""
    env = bu_pages
    focus = env.sup.focus_page
    original = env.sup._cdp

    async def wrong_first(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == "Target.getTargets":
            result["result"]["targetInfos"].sort(key=lambda t: not t.get("url", "").endswith("/other"))
        return result

    def copy_witness(*args, **kwargs):
        witness = re.match(r'window\[(".*?")\] === true', kwargs["accept"]).group(1)
        env.wrong.evaluate(f"window[{witness}] = true")
        return focus(*args, **kwargs)
    monkeypatch.setattr(env.sup, "_cdp", wrong_first)
    monkeypatch.setattr(env.sup, "focus_page", copy_witness)
    out = fill()
    assert out.get("error_type") == "page_changed", out
    assert values(env) == ("", "")


# ── Real browser-harness CLI + daemon (CI runs it on Python 3.14) ─────────────────────────────

@pytest.fixture
def real_harness(bu_pages, monkeypatch, tmp_path):
    # tests/tools/conftest.py pins discovery to "not installed"; this lane wants the real CLI.
    monkeypatch.setattr(bu, "_find_cli", getattr(bu, "_find_cli_unpatched", bu._find_cli))
    if bu._find_cli() is None:
        if os.environ.get("VAULT_TEST_REQUIRE_CLI") == "1":
            pytest.fail("real browser-harness required by this CI lane")
        pytest.skip("real browser-harness required (Python >= 3.14)")
    monkeypatch.setattr(bu, "_driver_query", REAL_DRIVER_QUERY)
    monkeypatch.setattr(bu, "_LAST_ROUTE", {})
    # Isolate the daemon (socket/pid/log) from any real one; drive the fixture's Chromium only.
    # AF_UNIX paths are capped (~104 bytes on macOS): keep the socket dir short.
    runtime = tempfile.mkdtemp(prefix="bhv", dir="/tmp")
    monkeypatch.setenv("BH_RUNTIME_DIR", runtime)
    monkeypatch.setenv("BH_TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("BU_CDP_WS", bu_pages.ws)
    monkeypatch.setenv("BH_TAB_MARKER", "0")
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda env, task: bu_pages.sup)
    monkeypatch.setattr(bu, "_workspace_dir", lambda task: str(tmp_path / "ws"))
    names = []

    def run(code, session=""):
        names.append(session)
        out = json.loads(bu.browser_exec(code, session=session, task_id=TASK))
        assert out.get("success"), out.get("stderr") or out
        return out["output"]
    yield run
    for name in set(names):
        bu.browser_exec("from browser_harness.helpers import _send\n_send({'meta': 'shutdown'})",
                        session=name, task_id=TASK)
    shutil.rmtree(runtime, ignore_errors=True)


REAL_DRIVER_QUERY = bu._driver_query

_SWITCH = ("tab = next(t for t in list_tabs() if t['url'].endswith('{suffix}'))\n"
           "switch_tab(tab)\nprint(js('location.pathname'))\n")


@pytest.mark.parametrize("action", ["fill", "tab_switch", "session_swap", "navigation"])
def test_real_harness_binds_to_current_target(bu_pages, real_harness, action):
    assert "/current" in real_harness(_SWITCH.format(suffix="/current"))

    def resolve(_):
        if action == "tab_switch":
            real_harness(_SWITCH.format(suffix="/other"))
        elif action == "session_swap":
            real_harness(_SWITCH.format(suffix="/current"), session="vaultswap")
        elif action == "navigation":
            real_harness("goto_url('https://site.example.test/replaced')\nwait_for_load()\n")
        return {"address_line1": VALUE}
    bu_pages.backend.resolve_secret = resolve
    out = fill()
    assert out["success"] is (action == "fill"), out
    assert bu_pages.current.locator("input").input_value() == (VALUE if action == "fill" else "")
    assert bu_pages.wrong.locator("input").input_value() == ""


@pytest.mark.parametrize("action", ["fill", "navigation", "tab_switch"])
def test_real_harness_handler_holds_daemon_selection_lease(bu_pages, real_harness, monkeypatch, action):
    """The tool handler path on the real lease daemon: _fenced_page_op takes the daemon's selection
    lease, the document on the leased target carries the operation witness, and the write lands
    only there. A same-target navigation (new document) or a tab switch during the op refuses."""
    from tools import browser_use_target as target
    monkeypatch.setattr(target, "_routes", {})
    assert "/current" in real_harness(_SWITCH.format(suffix="/current"))
    switched, leased, drivers = [], [], []
    acquire, pin = target.acquire_lease, vault._pin_page
    monkeypatch.setattr(target, "acquire_lease", lambda route: leased.append(acquire(route)) or leased[-1])
    monkeypatch.setattr(vault, "_pin_page", lambda tid: drivers.append(pin(tid)) or drivers[-1])

    def resolve(_):
        if action == "navigation":      # same target, new document: the witness is gone
            bu_pages.current.goto("https://site.example.test/replaced")
        elif action == "tab_switch":    # another browser_exec tries to move the leased selection
            switched.append(json.loads(bu.browser_exec(_SWITCH.format(suffix="/other"), task_id=TASK)))
        return {"address_line1": VALUE}
    bu_pages.backend.resolve_secret = resolve
    try:
        out = json.loads(vault._handle_vault_fill({"handle": "fixture-address"}, task_id=TASK))
    finally:
        SUPERVISOR_REGISTRY.stop_all()
    assert out["success"] is (action == "fill"), out
    assert bu_pages.current.locator("input").input_value() == (VALUE if action == "fill" else "")
    assert bu_pages.wrong.locator("input").input_value() == ""
    # The real daemon granted a selection lease and the page was bound on the leased target.
    assert len(leased) == 1 and leased[0][3], leased
    assert [b.driver for b in drivers] == ["browser-use-lease"], drivers
    if action == "tab_switch":
        assert not switched[0].get("success"), switched
        assert "selection_leased" in json.dumps(switched[0]), switched
