"""Dead-backend (402/401) circuit breaker for web_search / web_extract."""
import json
import logging
import time

import pytest

from tools import web_backend_breaker as bb
from tools import web_tools

PAYMENT = ("Payment Required: Failed to search. Insufficient credits to perform "
           "this request.")


class Provider:
    def __init__(self, name, calls, *, search=None, extract=None):
        self.name = name
        self.calls = calls
        self._search = search
        self._extract = extract

    def supports_search(self):
        return self._search is not None

    def supports_extract(self):
        return self._extract is not None

    def is_available(self):
        return True

    def search(self, query, limit):
        self.calls.append(self.name)
        return dict(self._search)

    def extract(self, urls, **kwargs):
        self.calls.append(self.name)
        return [dict(r, url=u) for r, u in zip(self._extract * len(urls), urls)]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {
        "search_backend": "firecrawl", "extract_backend": "firecrawl",
        "search_fallbacks": ["exa"], "extract_fallbacks": ["exa"],
        "dead_backend_cooldown_seconds": 3600,
    }
    monkeypatch.setattr("agent.web_search_provider.get_provider_env",
                        lambda key: "test-key" if key == "EXA_API_KEY" else "")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: cfg)
    monkeypatch.setattr(bb, "_web_config", lambda: cfg)

    async def safe(url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)
    calls, providers = [], {}
    monkeypatch.setattr("agent.web_search_registry.get_provider", providers.get)
    return calls, providers, cfg, tmp_path


def _search(q):
    return json.loads(web_tools.web_search_tool(f"{q} {time.time_ns()}"))


@pytest.mark.parametrize("err,status", [
    (PAYMENT, 402),
    ("HTTP 402", 402),
    ("401 Unauthorized: invalid api key", 401),
    ("Invalid API key provided", 401),
    ("HTTP 429 Too Many Requests", None),
    ("HTTP 500 upstream error", None),
    ("timed out", None),
    ("", None),
])
def test_classify_dead(err, status):
    assert bb.classify_dead(err) == status


def test_402_opens_breaker_skips_backend_and_logs_once(setup, caplog):
    calls, providers, _cfg, _home = setup
    ok = {"success": True, "data": {"web": [{"url": "https://ok.example"}]}}
    providers.update(firecrawl=Provider("firecrawl", calls, search={"success": False, "error": PAYMENT}),
                     exa=Provider("exa", calls, search=ok))
    caplog.set_level(logging.DEBUG, logger="tools.web_backend_breaker")
    for i in range(3):
        assert _search(f"q{i}")["success"] is True
    # Firecrawl was tried exactly once; the next two calls went straight to exa.
    assert calls == ["firecrawl", "exa", "exa", "exa"]
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "is dead" in r.getMessage()]
    assert len(warnings) == 1


def test_cooldown_expiry_reprobes_quietly_then_success_closes(setup, monkeypatch, caplog):
    calls, providers, _cfg, home = setup
    ok = {"success": True, "data": {"web": [{"url": "https://ok.example"}]}}
    fc = Provider("firecrawl", calls, search={"success": False, "error": PAYMENT})
    providers.update(firecrawl=fc, exa=Provider("exa", calls, search=ok))
    caplog.set_level(logging.DEBUG, logger="tools.web_backend_breaker")
    now = [1_000_000.0]
    monkeypatch.setattr(bb.time, "time", lambda: now[0])

    _search("a")
    now[0] += 3601  # cooldown lapsed: next call probes firecrawl again
    _search("b")
    assert calls == ["firecrawl", "exa", "firecrawl", "exa"]
    assert sum("is dead" in r.getMessage() for r in caplog.records) == 1

    now[0] += 3601
    fc._search = ok  # credits refilled
    calls.clear()
    assert _search("c")["success"] is True
    assert calls == ["firecrawl"]
    state = json.loads((home / "state" / "web_backend_breaker.json").read_text())
    assert "firecrawl" not in state
    assert any("recovered" in r.getMessage() for r in caplog.records)


def test_transient_errors_do_not_trip(setup):
    calls, providers, _cfg, _home = setup
    ok = {"success": True, "data": {"web": [{"url": "https://ok.example"}]}}
    providers.update(firecrawl=Provider("firecrawl", calls, search={"success": False, "error": "HTTP 429"}),
                     exa=Provider("exa", calls, search=ok))
    _search("x")
    _search("y")
    assert calls == ["firecrawl", "exa", "firecrawl", "exa"]


def test_cooldown_zero_disables_breaker(setup):
    calls, providers, cfg, _home = setup
    cfg["dead_backend_cooldown_seconds"] = 0
    ok = {"success": True, "data": {"web": [{"url": "https://ok.example"}]}}
    providers.update(firecrawl=Provider("firecrawl", calls, search={"success": False, "error": PAYMENT}),
                     exa=Provider("exa", calls, search=ok))
    _search("x")
    _search("y")
    assert calls == ["firecrawl", "exa", "firecrawl", "exa"]


def _wait_lines(path, n, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and len(path.read_text().splitlines()) >= n:
            break
        time.sleep(0.05)
    return path.read_text().splitlines() if path.exists() else []


def _wait_paged(home, backend, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = json.loads((home / "state" / "web_backend_breaker.json").read_text())
        if "paging" not in state.get(backend, {}):
            return state[backend]
        time.sleep(0.05)
    raise AssertionError("alert thread never finished")


def test_alert_command_fires_once_per_episode(setup, tmp_path):
    _calls, _providers, cfg, home = setup
    out = tmp_path / "pages.txt"
    cfg["dead_backend_alert_command"] = f'echo "$WEB_BACKEND $WEB_BACKEND_STATUS" >> {out}'
    t0 = 2_000_000.0
    assert bb.record_failure("firecrawl", PAYMENT, now=t0) is True
    assert _wait_lines(out, 1) == ["firecrawl 402"]
    _wait_paged(home, "firecrawl")
    # Re-trips in the same episode (e.g. hourly re-probes) do not page again.
    assert bb.record_failure("firecrawl", PAYMENT, now=t0 + 3601) is False
    assert bb.record_failure("firecrawl", PAYMENT, now=t0 + 7202) is False
    time.sleep(0.3)
    assert out.read_text().splitlines() == ["firecrawl 402"]
    # Recovery closes the episode; a later death is a new episode and pages.
    bb.record_success("firecrawl")
    assert bb.record_failure("firecrawl", PAYMENT, now=t0 + 9000) is True
    assert _wait_lines(out, 2) == ["firecrawl 402", "firecrawl 402"]


def test_failed_alert_is_retried_on_next_trip(setup, tmp_path):
    _calls, _providers, cfg, home = setup
    out = tmp_path / "attempts.txt"
    cfg["dead_backend_alert_command"] = f"echo try >> {out}; exit 3"
    t0 = 3_000_000.0
    bb.record_failure("firecrawl", PAYMENT, now=t0)
    assert _wait_lines(out, 1) == ["try"]
    entry = _wait_paged(home, "firecrawl")
    assert not entry.get("paged")
    cfg["dead_backend_alert_command"] = f"echo ok >> {out}"
    bb.record_failure("firecrawl", PAYMENT, now=t0 + 3601)
    assert _wait_lines(out, 2) == ["try", "ok"]
    assert _wait_paged(home, "firecrawl").get("paged") is True


@pytest.mark.parametrize("site_error", ["401 Unauthorized", "402 Payment Required"])
@pytest.mark.asyncio
async def test_target_site_401_402_does_not_trip_shared_breaker(setup, site_error):
    calls, providers, _cfg, _home = setup
    urls = ["https://a.example", "https://b.example"]
    providers.update(
        firecrawl=Provider("firecrawl", calls, extract=[{"content": "", "error": site_error}]),
        exa=Provider("exa", calls, extract=[{"content": "ok", "error": None}]),
    )
    out1 = json.loads(await web_tools.web_extract_tool(urls))
    out2 = json.loads(await web_tools.web_extract_tool([u + "/2" for u in urls]))
    assert calls == ["firecrawl", "exa", "firecrawl", "exa"]
    assert all(not r.get("error") for r in out1["results"] + out2["results"])
    assert bb.open_until("firecrawl") is None


def test_stale_page_completion_cannot_ack_new_episode(setup, monkeypatch):
    _calls, _providers, cfg, home = setup
    # Keep the alert worker queued so the first episode is removed before it
    # finishes. Then a second episode begins. The old success must not ack it.
    monkeypatch.setattr(bb, "_fire_alert", lambda *args: None)
    cfg["dead_backend_alert_command"] = "true"
    bb.record_failure("firecrawl", PAYMENT, now=1_000_000.0)
    first = bb._entry("firecrawl")
    bb.record_success("firecrawl")
    bb.record_failure("firecrawl", PAYMENT, now=1_000_001.0)
    second = bb._entry("firecrawl")
    assert first["alert_id"] != second["alert_id"]
    bb._mark_paged("firecrawl", first["episode_start"], first["alert_id"],
                   ok=True, note="old alert")
    entry = bb._entry("firecrawl")
    assert entry["alert_id"] == second["alert_id"]
    assert not entry.get("paged")
    assert "paging" in entry


def test_stale_alert_attempt_cannot_clear_new_attempt(setup, monkeypatch):
    _calls, _providers, cfg, home = setup
    monkeypatch.setattr(bb, "_fire_alert", lambda *args: None)
    cfg["dead_backend_alert_command"] = "true"
    bb.record_failure("firecrawl", PAYMENT, now=1_000_000.0)
    first = bb._entry("firecrawl")
    # The alert in flight has timed out and a new call attempts delivery.
    bb.record_failure("firecrawl", PAYMENT, now=1_000_121.0)
    second = bb._entry("firecrawl")
    assert first["alert_id"] != second["alert_id"]
    bb._mark_paged("firecrawl", first["episode_start"], first["alert_id"],
                   ok=False, note="old alert failed")
    assert bb._entry("firecrawl")["alert_id"] == second["alert_id"]
    assert "paging" in bb._entry("firecrawl")


def test_alert_thread_keeps_profile_home_and_scrubs_default_secrets(setup, monkeypatch):
    _calls, _providers, cfg, home = setup
    seen = []
    cfg["dead_backend_alert_command"] = "fake pager"
    monkeypatch.setenv("UNRELATED_DEFAULT_PROFILE_SECRET", "do-not-copy")

    class Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(kwargs["env"])
        return Proc()

    monkeypatch.setattr(bb.subprocess, "run", fake_run)
    bb.record_failure("firecrawl", PAYMENT)
    _wait_paged(home, "firecrawl")
    assert len(seen) == 1
    assert seen[0]["HERMES_HOME"] == str(home)
    assert "UNRELATED_DEFAULT_PROFILE_SECRET" not in seen[0]
