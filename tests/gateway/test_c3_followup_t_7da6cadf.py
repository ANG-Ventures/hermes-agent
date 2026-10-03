"""C3 re-bucket follow-up (card t_7da6cadf): one pin per fixed FleetReview row.

#970 has its own file (test_no_network_cold_models_dev.py) and #942 is a
desktop vitest (apps/desktop/src/lib/confab-notice-hydration.test.ts).
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import textwrap
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #976
def test_976_hygiene_agent_build_uses_the_session_id_snapshot():
    """_build_hyg_agent runs in a worker thread after awaits: it must read the
    _hyg_old_sid snapshot, never the live session_entry (a /new or rotation can
    move session_entry.session_id meanwhile)."""
    # Upstream moved the hygiene turn body from gateway/run.py into gateway/run_turn.py.
    builds = []
    for mod in ("run.py", "run_turn.py"):
        tree = ast.parse((REPO / "gateway" / mod).read_text(encoding="utf-8"))
        builds += [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_hyg_agent"]
    assert len(builds) == 1
    live = [n.lineno for n in ast.walk(builds[0])
            if isinstance(n, ast.Name) and n.id == "session_entry"]
    assert live == [], f"_build_hyg_agent reads the live session_entry at gateway/run.py:{live}"


# --------------------------------------------------------------------------- #1043
def _store(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionSource, SessionStore
    from gateway.config import Platform

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    entry = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="u"))
    return store, entry.session_key


def test_1043_post_turn_clear_keeps_a_mark_written_during_the_turn(tmp_path):
    store, key = _store(tmp_path)
    assert store.mark_resume_pending(key, "restart_timeout")
    at_turn_start = store._entries[key].last_resume_marked_at
    # A concurrent shutdown drain re-marks the session while the turn runs.
    store._entries[key].last_resume_marked_at = at_turn_start + timedelta(seconds=5)
    assert store.clear_resume_pending(key, marked_at=at_turn_start) is False
    assert store._entries[key].resume_pending is True
    # The mark the turn started from is cleared as before.
    assert store.clear_resume_pending(key, marked_at=store._entries[key].last_resume_marked_at) is True
    assert store._entries[key].resume_pending is False


def test_1043_gate_passes_the_turn_start_mark(tmp_path):
    """The runner hands the snapshot through; legacy callers (no marked_at)
    keep the old clear-whatever-is-there behaviour."""
    store, key = _store(tmp_path)
    store.mark_resume_pending(key, "restart_timeout")
    fresh = store._entries[key].last_resume_marked_at
    assert store.clear_resume_pending(key) is True  # legacy path unchanged
    store.mark_resume_pending(key, "restart_timeout")
    assert store.clear_resume_pending(key, marked_at=fresh - timedelta(seconds=1)) is False
    src = "".join((REPO / "gateway" / mod).read_text(encoding="utf-8") for mod in ("run.py", "run_turn.py"))
    assert "marked_at=_run_start_resume_marked_at" in src


# --------------------------------------------------------------------------- #1095
def test_1095_early_imported_bundled_profile_is_re_registered(tmp_path, monkeypatch):
    import providers
    from providers.base import ProviderProfile

    name = "c3f-probe"
    plugin = tmp_path / "c3f_probe"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(textwrap.dedent(f"""
        from providers import register_provider
        from providers.base import ProviderProfile
        bundled = ProviderProfile(name="{name}", base_url="https://bundled.example/v1")
        register_provider(bundled)
    """))
    mod_name = "plugins.model_providers.c3f_probe"
    monkeypatch.delitem(sys.modules, mod_name, raising=False)
    # 1. a direct early import (before discovery) registers the bundled profile
    import importlib.util
    spec = importlib.util.spec_from_file_location(mod_name, plugin / "__init__.py",
                                                  submodule_search_locations=[str(plugin)])
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, mod_name, mod)
    spec.loader.exec_module(mod)
    try:
        # 2. a pip entry-point plugin of the same name registers during discovery
        providers.register_provider(ProviderProfile(name=name, base_url="https://pip.example/v1"))
        assert providers._REGISTRY[name].base_url == "https://pip.example/v1"
        # 3. the bundled step reaches the already-imported module: bundled wins again
        providers._import_plugin_dir(plugin, "bundled")
        assert providers._REGISTRY[name].base_url == "https://bundled.example/v1"
    finally:
        # Same restore as tests/providers/test_entry_point_discovery.py: the
        # registry is additive, so reset it and re-run real discovery.
        from hermes_cli import provider_seam

        monkeypatch.delitem(sys.modules, mod_name, raising=False)
        providers._PROFILES_BY_MODULE.pop(mod_name, None)
        provider_seam._reset("_REGISTRY", "_ALIASES")
        providers._discovered = False
        for m in [m for m in sys.modules if m.startswith("plugins.model_providers.")]:
            del sys.modules[m]
        providers._discover_providers()


# --------------------------------------------------------------------------- #1198
def test_1198_runtime_stage_refusals_do_not_cool_the_provider():
    from hermes_cli import kanban_db as kb

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, ended_at INTEGER)")
    conn.execute("CREATE TABLE task_events (run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)")
    now = 10_000
    conn.execute("INSERT INTO task_runs VALUES (1, NULL), (2, NULL), (3, NULL)")
    ev = "worker_route_pin_refused"
    rows = [
        (1, ev, json.dumps({"stage": "runtime", "provider": "claude-bpr", "rate_limited": True}), now - 10),
        (2, ev, json.dumps({"stage": "auth", "provider": "openai-codex", "rate_limited": True}), now - 10),
        (3, ev, json.dumps({"provider": "legacy-prov", "rate_limited": True}), now - 10),  # pre-stage events
    ]
    conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?)", rows)
    cooling = kb.cooling_providers(conn, now=now, window=600)
    assert "claude-bpr" not in cooling
    assert cooling["openai-codex"] == now - 10 + 600
    assert "legacy-prov" in cooling


# --------------------------------------------------------------------------- #1254 :121
@pytest.fixture
def _pef():
    from hermes_cli import process_env_files as pef

    saved = dict(pef._OVERLAY)
    pef._OVERLAY.clear()
    yield pef
    pef._OVERLAY.clear()
    pef._OVERLAY.update(saved)


def test_1254_child_process_can_strip_an_inherited_overlay(tmp_path, monkeypatch, _pef):
    f = tmp_path / "lane.sh"
    f.write_text("export PEF_C3F_TOKEN=lane\n")
    cfg = {"agent": {"process_env_files": [str(f)]}}
    monkeypatch.delenv("PEF_C3F_TOKEN", raising=False)
    monkeypatch.delenv(_pef._INHERITED_ENV, raising=False)
    # Parent agent process applies the overlay.
    _pef.apply_process_env_files(cfg)
    try:
        assert os.environ["PEF_C3F_TOKEN"] == "lane"
        inherited = dict(os.environ)  # what a child agent process starts with
        # Child agent process: same config, already-overlaid env -> its own diff is empty.
        _pef._OVERLAY.clear()
        monkeypatch.setattr(os, "environ", inherited)
        assert _pef.apply_process_env_files(cfg) == {}
        # A plain script spawned by the CHILD must still lose the overlay.
        script_env = dict(inherited)
        _pef.strip_overlay(script_env)
        assert "PEF_C3F_TOKEN" not in script_env
        assert _pef._INHERITED_ENV not in script_env
    finally:
        monkeypatch.undo()
        os.environ.pop("PEF_C3F_TOKEN", None)
        os.environ.pop(_pef._INHERITED_ENV, None)


# --------------------------------------------------------------------------- #1032
def test_1032_resolution_that_changes_the_provider_revalidates_base_url(monkeypatch):
    from cron import jobs
    import tools.cronjob_tools as ct

    seen = []

    def _validate(provider, base_url):
        seen.append((provider, base_url))
        return "blocked" if provider == "anthropic" else None

    monkeypatch.setattr(ct, "_validate_cron_base_url", _validate)
    # provider rewritten by resolution (custom -> anthropic) with an off-host base_url
    with pytest.raises(ValueError, match="blocked"):
        jobs._revalidate_resolved_provider("my-custom", "anthropic", "https://evil.example/v1")
    # unchanged provider: the tool already validated this pair
    jobs._revalidate_resolved_provider("anthropic", "anthropic", "https://evil.example/v1")
    # no base_url: nothing to exfiltrate to
    jobs._revalidate_resolved_provider("my-custom", "anthropic", None)
    assert seen == [("anthropic", "https://evil.example/v1")]
    src = (REPO / "cron" / "jobs.py").read_text(encoding="utf-8")
    assert src.count("_revalidate_resolved_provider(") == 3  # def + create_job + update_job
