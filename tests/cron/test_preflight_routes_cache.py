"""``_primary_profile_routes_for_current_home`` must not re-parse the primary YAML layers per call.

Every cron run (preflight + ``SharedRouteAdapters``) calls it; on Apollo a 25 s ``py-spy --gil``
profile put 6.5% of all GIL time in its ``read_yaml_layers`` call. The result is cached on the
stat signature of every file ``read_yaml_layers`` consults, so a config edit is still picked up.
"""

import os

import hermes_yaml as yaml
import pytest

import gateway.config_loader as config_loader
from cron import scheduler_preflight
from cron.scheduler_preflight import _primary_profile_routes_for_current_home
from hermes_cli import managed_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

WA_ROUTE = {"name": "sat-wa", "platform": "whatsapp", "chat_id": "123@g.us", "profile": "sat"}
TG_ROUTE = {"name": "sat-tg", "platform": "telegram", "chat_id": "-100", "profile": "sat"}


def _write(path, *routes):
    path.write_text(
        yaml.safe_dump({"gateway": {"multiplex_profiles": True, "profile_routes": list(routes)}}),
        encoding="utf-8",
    )


@pytest.fixture
def satellite(tmp_path, monkeypatch):
    root = tmp_path / "root"
    sat_home = root / "profiles" / "sat"
    sat_home.mkdir(parents=True)
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()
    getattr(scheduler_preflight, "_ROUTES_CACHE", {}).clear()

    calls = {"n": 0}
    real = config_loader.read_yaml_layers

    def counting(home):
        calls["n"] += 1
        return real(home)

    monkeypatch.setattr(config_loader, "read_yaml_layers", counting)
    token = set_hermes_home_override(str(sat_home))
    try:
        yield root, managed_dir, calls
    finally:
        reset_hermes_home_override(token)
        managed_scope.invalidate_managed_cache()
        getattr(scheduler_preflight, "_ROUTES_CACHE", {}).clear()


def test_unchanged_files_parse_once(satellite):
    root, _managed, calls = satellite
    _write(root / "config.yaml", WA_ROUTE)
    for _ in range(10):
        assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["whatsapp"]
    assert calls["n"] == 1


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_rewrite_or_touch_is_reread(satellite, layer):
    root, managed_dir, calls = satellite
    target = root / "config.yaml" if layer == "user" else managed_dir / "config.yaml"
    _write(target, WA_ROUTE)
    assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["whatsapp"]

    _write(target, TG_ROUTE)
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # beat coarse mtime
    assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["telegram"]
    n = calls["n"]

    st = target.stat()  # touch only: content unchanged, still re-read
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["telegram"]
    assert calls["n"] == n + 1


def test_file_appearing_is_reread(satellite):
    root, _managed, _calls = satellite
    assert _primary_profile_routes_for_current_home() == []
    _write(root / "config.yaml", WA_ROUTE)
    assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["whatsapp"]


def test_caller_mutation_does_not_leak(satellite):
    root, _managed, _calls = satellite
    _write(root / "config.yaml", WA_ROUTE)
    first = _primary_profile_routes_for_current_home()
    first.clear()
    first.append("junk")
    assert [r.platform for r in _primary_profile_routes_for_current_home()] == ["whatsapp"]
