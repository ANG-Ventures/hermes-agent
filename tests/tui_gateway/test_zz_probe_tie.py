"""Probe: a new pairing ledger whose mtime ties the current newest is invisible to _pairing_sig."""
import os
from pathlib import Path


def test_probe_tie(watcher_home):
    from tui_gateway import server
    home, events = watcher_home
    for name in ("work", "play"):
        (home / "profiles" / name / "platforms" / "pairing").mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n")
    a = home / "profiles" / "work" / "platforms" / "pairing" / "telegram-pending.json"
    a.write_text("{}")
    server._broadcast_watched_changes(now=0.0)
    b = home / "profiles" / "play" / "platforms" / "pairing" / "discord-approved.json"
    b.write_text("{}")
    st = a.stat()
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns))  # same coarse tick as a (Linux jiffy mtimes)
    server._broadcast_watched_changes(now=10.0)
    assert events == [("pairing.changed", {})]
