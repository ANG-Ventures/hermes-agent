"""A previous boot's ``platforms`` entry is dropped at startup.

After a restart with whatsapp disabled, ``gateway_state.json`` still read ``whatsapp: fatal``
from the previous pid: the startup write reloads the on-disk record and only cleared ``<profile>:``
entries, so a platform this boot never runs kept its last state forever.
"""

import json
import os

from gateway import status


def _seed(home, platforms):
    (home / "gateway_state.json").write_text(json.dumps({"platforms": platforms}), encoding="utf-8")


def test_startup_drops_entries_from_a_previous_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, {
        "whatsapp": {"state": "fatal", "error_code": "whatsapp_not_paired",
                     "writer_pid": 58324, "writer_start_time": 12345},
        "legacy": {"state": "fatal"},  # pre-stamp record: no writer identity at all
    })

    status.write_runtime_status(
        gateway_state="starting", clear_profile_platforms=True, drop_stale_platforms=True, reload_existing=True)

    assert status.read_runtime_status()["platforms"] == {}


def test_startup_keeps_entries_this_process_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status.write_runtime_status(platform="telegram", platform_state="connected", reload_existing=True)

    status.write_runtime_status(
        gateway_state="starting", clear_profile_platforms=True, drop_stale_platforms=True, reload_existing=True)

    platforms = status.read_runtime_status()["platforms"]
    assert set(platforms) == {"telegram"}
    assert platforms["telegram"]["writer_pid"] == os.getpid()


def test_same_pid_different_start_time_is_stale(tmp_path, monkeypatch):
    # PID reuse: same number, different process incarnation.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    me = status._get_process_start_time(os.getpid())
    _seed(tmp_path, {"whatsapp": {"state": "fatal", "writer_pid": os.getpid(),
                                  "writer_start_time": (me or 0) + 10_000}})

    status.write_runtime_status(drop_stale_platforms=True, reload_existing=True)

    assert status.read_runtime_status()["platforms"] == {}


def test_without_the_flag_entries_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, {"whatsapp": {"state": "fatal", "writer_pid": 58324, "writer_start_time": 1}})

    status.write_runtime_status(clear_profile_platforms=True, reload_existing=True)

    assert set(status.read_runtime_status()["platforms"]) == {"whatsapp"}


def test_gateway_startup_write_passes_the_flag():
    import inspect

    from gateway import run_startup

    src = inspect.getsource(run_startup)
    start = src.index('gateway_state="starting"')
    assert "drop_stale_platforms=True" in src[start:start + 400]
