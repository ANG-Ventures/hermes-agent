"""Fork canary: a previous boot's ``platforms`` entry is dropped at startup (fork, t_83ff59d4).

After aegis restarted with whatsapp disabled, ``gateway_state.json`` still read ``whatsapp: fatal``
from the previous pid: the startup write reloads the on-disk record and only cleared ``<profile>:``
entries, so a platform this boot never runs kept its last state forever.
"""

import json
import os
import subprocess
import sys

from gateway import status


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _seed(home, platforms):
    (home / "gateway_state.json").write_text(json.dumps({"platforms": platforms}), encoding="utf-8")


def test_startup_drops_entries_from_a_previous_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, {
        "whatsapp": {"state": "fatal", "error_code": "whatsapp_not_paired",
                     "writer_pid": _dead_pid(), "writer_start_time": 12345},
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


def test_live_foreign_writer_entries_are_kept(tmp_path, monkeypatch):
    # Another still-running writer (e.g. a multiplexed peer, a draining predecessor) keeps its entries.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        start = status._get_process_start_time(peer.pid)
        _seed(tmp_path, {"reviewer:discord": {"state": "connected", "writer_pid": peer.pid, "writer_start_time": start},
                         "slack": {"state": "connected", "writer_pid": peer.pid, "writer_start_time": start}})

        status.write_runtime_status(drop_stale_platforms=True, reload_existing=True)

        assert set(status.read_runtime_status()["platforms"]) == {"reviewer:discord", "slack"}
    finally:
        peer.kill()
        peer.wait()


def test_any_write_drops_a_dead_stamped_writer(tmp_path, monkeypatch):
    # Not only the startup write: an ordinary status write also drops a dead writer's stamped entry.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, {"whatsapp": {"state": "fatal", "writer_pid": _dead_pid(), "writer_start_time": 1}})

    status.write_runtime_status(clear_profile_platforms=True, reload_existing=True)

    assert status.read_runtime_status()["platforms"] == {}


def test_ordinary_write_keeps_unstamped_entries(tmp_path, monkeypatch):
    # Unstamped entries are only judged at startup (drop_stale_platforms), when no live writer can own them.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, {"slack": {"state": "connected"}})

    status.write_runtime_status(reload_existing=True)

    assert set(status.read_runtime_status()["platforms"]) == {"slack"}


def test_draining_predecessor_entry_goes_after_it_exits(tmp_path, monkeypatch):
    # Prism 2129945e0725: the predecessor is still draining (alive) at the successor's startup write, so its
    # entry survives that write; it must be gone on the first ordinary write after the predecessor exits.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _seed(tmp_path, {"whatsapp": {"state": "fatal", "writer_pid": old.pid,
                                      "writer_start_time": status._get_process_start_time(old.pid)}})
        status.write_runtime_status(gateway_state="starting", clear_profile_platforms=True,
                                    drop_stale_platforms=True, reload_existing=True)
        assert set(status.read_runtime_status()["platforms"]) == {"whatsapp"}
    finally:
        old.kill()
        old.wait()

    status.write_runtime_status(gateway_state="running", reload_existing=True)

    assert status.read_runtime_status()["platforms"] == {}


def test_gateway_startup_write_passes_the_flag():  # noqa: source-proxy fork canary: the fork kwarg on the upstream startup write deep in start(); drop_stale_platforms behaviour is exercised above
    import inspect

    from gateway import run_startup

    src = inspect.getsource(run_startup)
    start = src.index('gateway_state="starting"')
    assert "drop_stale_platforms=True" in src[start:start + 400]
