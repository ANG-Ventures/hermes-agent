"""Discord admission must not rebuild a PairingStore (legacy-dir migration + realpath syscalls) per message.

Measured: Apollo gateway loop blocked 10 s in ``_is_pairing_approved_user`` -> ``PairingStore().__init__`` ->
``_migrate_split_pairing_dirs`` (several ``Path.resolve``/``exists``/``glob``/json syscalls) on the asyncio loop.
"""

import json
from unittest.mock import patch

import pytest

import gateway.pairing as pairing
from gateway.pairing import PairingStore
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture(autouse=True)
def _reset_process_caches():
    for name in ("_MIGRATED_PAIRING_DIRS", "_SHARED_STORES"):
        cache = getattr(pairing, name, None)
        if cache is not None:
            cache.clear()
    yield
    for name in ("_MIGRATED_PAIRING_DIRS", "_SHARED_STORES"):
        cache = getattr(pairing, name, None)
        if cache is not None:
            cache.clear()


def _adapter():
    return object.__new__(DiscordAdapter)  # admission helpers need no __init__ state


def _split_home(tmp_path):
    home = tmp_path / "home"
    active = home / "platforms" / "pairing"
    legacy = home / "pairing"
    active.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (legacy / "discord-approved.json").write_text(json.dumps({"legacy": {"user_name": "L"}}))
    return home, active


def test_twenty_admission_checks_run_migration_once(tmp_path):
    home, active = _split_home(tmp_path)
    adapter = _adapter()
    with patch.object(pairing, "PAIRING_DIR", active), patch.object(pairing, "get_hermes_home", return_value=home), \
            patch.object(pairing, "_migrate_split_pairing_dirs", wraps=pairing._migrate_split_pairing_dirs) as spy:
        results = [adapter._is_pairing_approved_user("legacy") for _ in range(20)]
    assert all(results)
    assert spy.call_count == 1, f"migration ran {spy.call_count}x for 20 admission checks"


def test_second_construction_skips_migration_io(tmp_path):
    home, active = _split_home(tmp_path)
    with patch.object(pairing, "PAIRING_DIR", active), patch.object(pairing, "get_hermes_home", return_value=home):
        PairingStore()
        with patch.object(pairing, "_load_json_file", wraps=pairing._load_json_file) as loads:
            PairingStore()
    assert loads.call_count == 0, "split-dir migration re-read legacy files on every construction"


def test_reused_store_sees_approval_written_by_another_process(tmp_path):
    adapter = _adapter()
    with patch.object(pairing, "PAIRING_DIR", tmp_path):
        assert adapter._is_pairing_approved_user("4242") is False
        PairingStore()._approve_user("discord", "4242", "new")  # e.g. `hermes pairing approve`
        assert adapter._is_pairing_approved_user("4242") is True
        other = PairingStore()
        assert other.revoke("discord", "4242") is True
        assert adapter._is_pairing_approved_user("4242") is False


def test_failed_migration_is_retried_not_latched(tmp_path):
    home, active = _split_home(tmp_path)
    real_save = pairing._save_json_file
    calls = {"n": 0}

    def flaky_save(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        real_save(path, data)

    with patch.object(pairing, "PAIRING_DIR", active), patch.object(pairing, "get_hermes_home", return_value=home), \
            patch.object(pairing, "_save_json_file", side_effect=flaky_save):
        with pytest.raises(OSError):
            PairingStore()
        store = PairingStore()
    assert store.is_approved("discord", "legacy")


def test_admission_store_lookup_failure_fails_closed(tmp_path):
    adapter = _adapter()
    with patch.object(pairing, "PAIRING_DIR", tmp_path), \
            patch.object(PairingStore, "is_approved", side_effect=RuntimeError("boom")):
        assert adapter._is_pairing_approved_user("4242") is False


def test_shared_store_follows_pairing_dir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    with patch.object(pairing, "PAIRING_DIR", a):
        PairingStore()._approve_user("discord", "1", "x")
        assert _adapter()._is_pairing_approved_user("1") is True
    with patch.object(pairing, "PAIRING_DIR", b):
        assert _adapter()._is_pairing_approved_user("1") is False
