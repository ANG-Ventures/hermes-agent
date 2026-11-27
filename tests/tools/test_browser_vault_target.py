"""The navigation driver's current page, not an origin search, owns vault writes.

Only synthetic address data is used. The CDP supervisor and JavaScript execute
against real disposable Chromium pages; the CLI boundary uses that driver's page.
"""
import json
from types import SimpleNamespace

import pytest

from tools import browser_tool as bt, browser_vault_tool as vault
from tools.browser_supervisor import CDPSupervisor, SUPERVISOR_REGISTRY


@pytest.fixture(params=["fixture-task", "fixture-task::local"])
def pages(monkeypatch, request):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--remote-debugging-port=0", "--enable-automation"])
        context = browser.new_context()
        context.route("https://*.example.test/**", lambda route: route.fulfill(
            body='<input autocomplete="address-line1" name="address">', content_type="text/html"))
        wrong = context.new_page()
        wrong.goto("https://site.example.test/other")
        current = context.new_page()
        current.goto("https://site.example.test/current")
        # Connect the real supervisor to the same browser over its browser WS.
        endpoint = browser.new_browser_cdp_session().send("Browser.getBrowserCommandLine")
        # Playwright's pipe launch does not expose its chosen port. Obtain the
        # endpoint through DevToolsActivePort in its isolated user-data directory.
        from pathlib import Path
        profile = next(a.split("=", 1)[1] for a in endpoint["arguments"] if a.startswith("--user-data-dir="))
        port, ws_path = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[:2]
        sup = CDPSupervisor("fixture-task", f"ws://127.0.0.1:{port}{ws_path}")
        sup.start()
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: False)
        key = request.param
        session = {"owner_task_id": "fixture-task", "session_key": key}
        monkeypatch.setitem(bt._active_sessions, key, session)
        monkeypatch.setitem(bt._last_active_session_key, "fixture-task", key)
        monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda target: sup if target == key else None)
        driver = {"page": current}
        calls = []
        def command(key, name, args, **kwargs):
            calls.append((key, name))
            assert key == request.param
            assert name == "eval", "fixture must not create a browser or resolve credentials"
            return {"success": True, "data": {"result": driver["page"].evaluate(args[0])}}
        monkeypatch.setattr("tools.browser_tool_session._run_browser_command", command)
        meta = SimpleNamespace(id="fixture-address", kind="address", fill_supported=True,
                               origin="https://site.example.test", allowed_origins=(), label="Fixture")
        backend = SimpleNamespace(name="fixture", needs_unlock=False, get_meta=lambda _: meta,
                                  resolve_secret=lambda _: {"address_line1": "NONSECRET FIXTURE STREET"})
        monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
        try:
            yield current, wrong, driver, backend, meta, sup, calls
        finally:
            sup.stop()
            browser.close()


def fill():
    return json.loads(vault.browser_vault_fill("fixture-address", task_id="fixture-task"))


def test_unsupported_backend_cannot_reuse_builtin_session(pages, monkeypatch):
    current, wrong, _, _, _, _, calls = pages
    for mode in ("_is_camofox_mode", "_is_browser_use_cli_mode"):
        with monkeypatch.context() as patch:
            patch.setattr(bt, mode, lambda: True)
            out = fill()
            assert out["error_type"] == "page_binding_required", out
    assert calls == []
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""


def test_rejected_targets_do_not_receive_dialog_bridge(pages, monkeypatch):
    current, wrong, _, _, _, sup, _ = pages
    # A newly created unrelated page has never been supervised.
    untouched = current.context.new_page()
    untouched.goto("https://other.example.test/untouched")
    before = untouched.evaluate("({a: String(alert), c: String(confirm), p: String(prompt)})")
    marker = "fixtureOnlyCurrent"
    current.evaluate(f"window.{marker} = true")
    # Force the unrelated target first, independent of Chromium enumeration order.
    original = sup._cdp
    async def ordered(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == "Target.getTargets":
            result["result"]["targetInfos"].sort(key=lambda t: not t.get("url", "").endswith("/untouched"))
        return result
    monkeypatch.setattr(sup, "_cdp", ordered)
    assert sup.focus_page("", accept=f"window.{marker} === true")["ok"]
    assert untouched.evaluate("({a: String(alert), c: String(confirm), p: String(prompt)})") == before


def test_navigation_current_page_wins_over_first_same_origin_tab(pages):
    current, wrong, *_ = pages
    out = fill()
    assert out["success"], out
    assert current.locator("input").input_value() == "NONSECRET FIXTURE STREET"
    assert wrong.locator("input").input_value() == ""


def test_bound_origin_mismatch_never_searches_another_tab(pages):
    current, wrong, _, _, meta, *_ = pages
    current.goto("https://accounts.example.test/login")
    out = fill()
    assert out["error_type"] == "origin_mismatch", out
    assert wrong.locator("input").input_value() == ""
    assert current.locator("input").input_value() == ""


@pytest.mark.parametrize("race", ["same_origin_navigation", "cross_origin_navigation", "tab_switch", "session_replaced", "camofox_switch", "browser_use_switch"])
def test_page_change_during_resolution_fails_closed(pages, race, monkeypatch):
    current, wrong, driver, backend, *_ = pages
    def resolve(_):
        if race == "same_origin_navigation":
            current.goto("https://site.example.test/replaced")
        elif race == "cross_origin_navigation":
            current.goto("https://accounts.example.test/replaced")
        elif race == "tab_switch":
            driver["page"] = wrong
        elif race in {"camofox_switch", "browser_use_switch"}:
            mode = "_is_camofox_mode" if race == "camofox_switch" else "_is_browser_use_cli_mode"
            monkeypatch.setattr(bt, mode, lambda: True)
        else:
            key = bt._last_session_key("fixture-task")
            monkeypatch.setitem(bt._active_sessions, key, dict(bt._active_sessions[key]))
        return {"address_line1": "NONSECRET FIXTURE STREET"}
    backend.resolve_secret = resolve
    out = fill()
    assert not out["success"], out
    assert current.locator("input").input_value() == ""
    assert wrong.locator("input").input_value() == ""


def test_foreign_task_cannot_use_supervisor_or_current_page(pages):
    current, wrong, _, _, _, _, calls = pages
    out = json.loads(vault.browser_vault_fill("fixture-address", task_id="foreign-task"))
    assert not out["success"], out
    assert calls == []
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""


@pytest.mark.parametrize("race", ["navigation", "tab_switch", "session_swap", "camofox_switch", "browser_use_switch"])
def test_navigation_after_target_selection_is_atomically_refused(pages, monkeypatch, race):
    current, wrong, driver, _, _, sup, _ = pages
    focus = sup.focus_page

    def raced_focus(*args, **kwargs):
        result = focus(*args, **kwargs)
        if race == "navigation":
            current.goto("https://site.example.test/new-document")
        elif race == "tab_switch":
            driver["page"] = wrong
        elif race in {"camofox_switch", "browser_use_switch"}:
            mode = "_is_camofox_mode" if race == "camofox_switch" else "_is_browser_use_cli_mode"
            monkeypatch.setattr(bt, mode, lambda: True)
        else:
            key = bt._last_session_key("fixture-task")
            monkeypatch.setitem(bt._active_sessions, key, dict(bt._active_sessions[key]))
        return result

    monkeypatch.setattr(sup, "focus_page", raced_focus)
    out = fill()
    assert not out["success"], out
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""


# ---------------------------------------------------------------------------
# Same-origin iframe login (Apple idmsa: the sign-in widget is an iframe on the page's own origin)
# ---------------------------------------------------------------------------

_FRAME_PAGES = {
    "https://site.example.test/top-frame": '<iframe id="aid" src="https://site.example.test/widget"></iframe>',
    "https://site.example.test/top-both": ('<input type="password" name="toppw">'
                                           '<iframe id="pay" src="https://pay.other.test/widget"></iframe>'),
    "https://site.example.test/widget": ('<input autocomplete="username webauthn" id="account_name_text_field">'
                                         '<input type="text" name="password_hint">'
                                         '<input type="password" autocomplete="current-password" id="password_text_field">'),
    "https://pay.other.test/widget": '<input type="password" name="xpw"><input autocomplete="cc-number" name="cc">',
    "https://site.example.test/top-hidden-none": ('<input type="password" name="toppw">'
                                                  '<iframe style="display:none" src="https://site.example.test/widget"></iframe>'),
    "https://site.example.test/top-hidden-opacity": ('<input type="password" name="toppw">'
                                                     '<iframe style="opacity:0" src="https://site.example.test/widget"></iframe>'),
    "https://site.example.test/top-hidden-tiny": ('<input type="password" name="toppw">'
                                                  '<iframe style="width:1px;height:1px;border:0" src="https://site.example.test/widget"></iframe>'),
    "https://site.example.test/top-hidden-parent": ('<input type="password" name="toppw"><div style="visibility:hidden">'
                                                    '<iframe src="https://site.example.test/widget"></iframe></div>'),
}


@pytest.fixture
def frame_page():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--site-per-process"])
        context = browser.new_context()
        context.route("https://**/*", lambda route: route.fulfill(
            body=_FRAME_PAGES.get(route.request.url.split("?")[0], "<p>blank</p>"), content_type="text/html"))
        try:
            yield context.new_page()
        finally:
            browser.close()


def _frame(page, url):
    return next(f for f in page.frames if f.url == url)


def _inspect_and_select(page, nonce="n0nce"):
    from agent.vault_login_classifier import LoginControl, build_inspection_js, classify_login_control, select_password_fill
    raw = json.loads(page.evaluate(build_inspection_js(nonce)))
    classified = [c for c in (classify_login_control(LoginControl.from_dict(r)) for r in raw) if c]
    return raw, select_password_fill(classified, "FIXTURE-PW")


def _stamps(frame):
    return frame.evaluate("document.querySelectorAll('[data-hermes-vault-slot],[data-hermes-vault-frame]').length")


def test_same_origin_iframe_login_is_found_and_filled(frame_page):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto("https://site.example.test/top-frame")
    widget = _frame(frame_page, "https://site.example.test/widget")
    raw, fills = _inspect_and_select(frame_page)
    assert {r["name"].strip() for r in raw} >= {"account_name_text_field", "password_text_field"}, raw
    assert len(fills) == 1
    out = json.loads(frame_page.evaluate(build_fill_js(fills, "https://site.example.test", nonce="n0nce")))
    assert out == {"filled": 1}, out
    assert widget.locator("#password_text_field").input_value() == "FIXTURE-PW"
    assert widget.locator("#account_name_text_field").input_value() == ""
    assert _stamps(frame_page.main_frame) == 0 and _stamps(widget) == 0


def test_cross_origin_iframe_inputs_are_never_stamped_or_filled(frame_page):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto("https://site.example.test/top-both")
    pay = _frame(frame_page, "https://pay.other.test/widget")
    raw, fills = _inspect_and_select(frame_page)
    assert [r["name"].strip() for r in raw] == ["toppw"], raw
    assert _stamps(pay) == 0
    # Even an index that would exist if the frame had been walked resolves to nothing.
    every = [{"index": i, "token": "current-password", "value": "FIXTURE-PW"} for i in range(4)]
    out = json.loads(frame_page.evaluate(build_fill_js(every, "https://site.example.test", nonce="n0nce")))
    assert out == {"filled": 1}, out
    assert pay.locator("input[name=xpw]").input_value() == ""
    assert frame_page.locator("input[name=toppw]").input_value() == "FIXTURE-PW"


def test_fill_refuses_when_the_inspected_frame_left_the_origin(frame_page):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto("https://site.example.test/top-frame")
    _, fills = _inspect_and_select(frame_page)
    assert fills
    with frame_page.expect_event("framenavigated"):
        frame_page.evaluate("document.getElementById('aid').src = 'https://pay.other.test/widget'")
    pay = _frame(frame_page, "https://pay.other.test/widget")
    pay.wait_for_load_state()
    out = json.loads(frame_page.evaluate(build_fill_js(fills, "https://site.example.test", nonce="n0nce")))
    assert out.get("refused") == "origin_changed", out
    assert pay.locator("input[name=xpw]").input_value() == ""
    assert _stamps(frame_page.main_frame) == 0


def test_password_fill_in_a_frame_still_requires_type_password(frame_page):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto("https://site.example.test/top-frame")
    widget = _frame(frame_page, "https://site.example.test/widget")
    raw, _ = _inspect_and_select(frame_page)
    text_idx = next(r["index"] for r in raw if r["name"].strip() == "password_hint")
    fill = [{"index": text_idx, "token": "current-password", "value": "FIXTURE-PW"}]
    out = json.loads(frame_page.evaluate(build_fill_js(fill, "https://site.example.test", nonce="n0nce")))
    assert out == {"filled": 0}, out
    assert widget.locator("input[name=password_hint]").input_value() == ""
    assert _stamps(widget) == 0


@pytest.mark.parametrize("path", ["top-hidden-none", "top-hidden-opacity", "top-hidden-tiny", "top-hidden-parent"])
def test_hidden_same_origin_frame_is_never_stamped_or_filled(frame_page, path):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto(f"https://site.example.test/{path}")
    widget = _frame(frame_page, "https://site.example.test/widget")
    raw, fills = _inspect_and_select(frame_page)
    assert [r["name"].strip() for r in raw] == ["toppw"], raw
    assert _stamps(widget) == 0
    every = [{"index": i, "token": "current-password", "value": "FIXTURE-PW"} for i in range(4)]
    out = json.loads(frame_page.evaluate(build_fill_js(every, "https://site.example.test", nonce="n0nce")))
    assert out == {"filled": 1}, out
    assert widget.locator("#password_text_field").input_value() == ""
    assert frame_page.locator("input[name=toppw]").input_value() == "FIXTURE-PW"


def test_fill_refuses_when_the_inspected_frame_was_hidden(frame_page):
    from agent.vault_login_classifier import build_fill_js
    frame_page.goto("https://site.example.test/top-frame")
    widget = _frame(frame_page, "https://site.example.test/widget")
    _, fills = _inspect_and_select(frame_page)
    assert fills
    frame_page.evaluate("document.getElementById('aid').style.opacity = '0'")
    out = json.loads(frame_page.evaluate(build_fill_js(fills, "https://site.example.test", nonce="n0nce")))
    assert out == {"refused": "origin_changed", "found": "frame:hidden"}, out
    assert widget.locator("#password_text_field").input_value() == ""
    assert _stamps(frame_page.main_frame) == 0 and _stamps(widget) == 0
