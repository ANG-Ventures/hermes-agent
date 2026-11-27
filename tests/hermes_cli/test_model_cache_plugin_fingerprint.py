"""t_9523d203: the Studio ``cpa`` catalog flapped to the plugin's offline snapshot.

``_credential_fingerprint`` folded the mtime of ``$HERMES_HOME/auth.json`` (and five other
credential files) into EVERY non-Codex provider's fingerprint. ``cpa`` reads none of them: its key
is ``CPA_KEY``. Any credential-pool write therefore changed cpa's fingerprint, the live disk row
stopped matching, and the next probe that failed (a CLIProxyAPI restart) stored the
``fallback_models`` snapshot over the live catalog, because ``_live_result_entry`` only protects a
live row for the SAME fingerprint.

A plugin api-key provider's catalog is probed with exactly ``resolve_api_key_provider_credentials``
(``_profile_live_catalog``), so its fingerprint is that resolved key + base URL and nothing else.
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

import hermes_cli.models as mod
from providers.base import ProviderProfile

LIVE = ["cpa/gpt-6.2-sol", "cpa/kimi-k3.1-pro", "cpa/grok"]
SNAPSHOT = ["cpa/kimi-k3", "cpa/kimi-k3-256k"]

_PROFILE = ProviderProfile(
    name="cpa", api_mode="chat_completions", env_vars=("CPA_KEY", "KIMI_CODE_KEY", "CPA_BASE_URL"),
    base_url="http://127.0.0.1:18812/v1", auth_type="api_key", fallback_models=tuple(SNAPSHOT))


@pytest.fixture
def cpa_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text("{}")
    creds = {"api_key": "key-a", "base_url": "http://127.0.0.1:18812/v1"}
    with patch("providers.get_provider_profile", side_effect=lambda p: _PROFILE if p == "cpa" else None), \
         patch.object(mod, "_configured_relay_base_url", return_value=""), \
         patch.object(mod, "_api_key_credentials", side_effect=lambda p: (creds["api_key"], creds["base_url"])):
        yield tmp_path, creds


def _bump_mtime(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_auth_json_write_does_not_change_a_plugin_api_key_fingerprint(cpa_home):
    home, _ = cpa_home
    before = mod._credential_fingerprint("cpa")
    _bump_mtime(home / "auth.json")
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    assert mod._credential_fingerprint("cpa") == before


def test_the_resolved_key_or_base_url_still_changes_it(cpa_home):
    _, creds = cpa_home
    first = mod._credential_fingerprint("cpa")
    creds["api_key"] = "key-b"
    second = mod._credential_fingerprint("cpa")
    creds["base_url"] = "http://127.0.0.1:18813/v1"
    third = mod._credential_fingerprint("cpa")
    assert len({first, second, third}) == 3


def test_failed_probe_after_an_auth_json_write_keeps_the_live_row(cpa_home):
    """The flap end to end: live row on disk, auth.json written, then a probe during a proxy
    restart degrades to the snapshot. The live row must stay, and stay served."""
    home, _ = cpa_home
    cache = {"cpa": {"fp": mod._credential_fingerprint("cpa"), "at": time.time() - 7200, "models": list(LIVE)}}
    _bump_mtime(home / "auth.json")
    with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
         patch.object(mod, "_save_provider_models_cache") as save, \
         patch.object(mod, "provider_model_ids", return_value=mod.CuratedFallbackModels(SNAPSHOT)):
        out = mod.cached_provider_model_ids("cpa", force_refresh=True)
    assert out == LIVE
    assert cache["cpa"]["models"] == LIVE and "fallback" not in cache["cpa"]
    save.assert_not_called()


def test_providers_outside_the_generic_probe_keep_file_mtimes(cpa_home):
    """Copilot's catalog reads ~/.config/github-copilot/hosts.json itself: unchanged behaviour."""
    home, _ = cpa_home
    hosts = home / ".config" / "github-copilot" / "hosts.json"
    hosts.parent.mkdir(parents=True)
    hosts.write_text("{}")
    before = mod._credential_fingerprint("copilot")
    _bump_mtime(hosts)
    assert mod._credential_fingerprint("copilot") != before
