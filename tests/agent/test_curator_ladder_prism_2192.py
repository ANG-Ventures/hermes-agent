"""Prism round 1 on hermes-agent#2192 (t_776ae7bc): the curator's 60/120 ladder.

- bcd12aa78312: shared skills reach apply_automatic_transitions (curated_report lists local skills only).
- fd080ab7976d: a fleet tier library that exists but cannot be read skips the tick (no config.yaml fallback).
- 12ed22fe3321: the batch gate cache is keyed by fleet root.
- d73b5c4c3ad2: an iterator of aliases is read by both the idle lookup and the gate.
- d7a3b4d58362 / dc27fb68fc94: the effective config defaults are 60/120 (real loader, no _load_config stub).
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _write_skill(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: t\n---\n\nbody\n", encoding="utf-8")
    return d


def _record(days: int) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"created_by": "agent", "created_at": ts, "last_used_at": ts, "last_activity_at": ts,
            "use_count": 1, "state": "active"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    shared = home / "skills-shared"
    (shared / "devops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "skills:\n  external_dirs:\n    - %s\ncurator:\n  include_shared_dirs: true\n" % (shared / "devops"),
        encoding="utf-8")
    import agent.skill_utils as su
    importlib.reload(su)
    import tools.skill_usage as u
    importlib.reload(u)
    import agent.curator as c
    importlib.reload(c)
    import agent.curator_shared as cs
    importlib.reload(cs)
    monkeypatch.setattr(u, "_prune_builtins_enabled", lambda: False)
    return {"home": home, "shared": shared, "c": c, "u": u, "cs": cs}


def test_shared_skill_reaches_the_automatic_transition_loop(env, monkeypatch):
    c, u = env["c"], env["u"]
    _write_skill(env["shared"] / "devops", "idle-shared")
    data = u.load_usage()
    data["idle-shared"] = _record(61)
    u.save_usage(data)
    monkeypatch.setattr(c, "_fleet_ladder", lambda: None)
    seen = []
    monkeypatch.setattr(u, "archive_skill", lambda name: (seen.append(name), (True, "unsubscribed to x"))[1])
    counts = c.apply_automatic_transitions()
    assert seen == ["idle-shared"]
    assert counts["unsubscribed"] == 1


def test_shared_skill_excluded_when_include_shared_dirs_off(env, monkeypatch):
    c, u = env["c"], env["u"]
    _write_skill(env["shared"] / "devops", "idle-shared")
    data = u.load_usage()
    data["idle-shared"] = _record(61)
    u.save_usage(data)
    monkeypatch.setattr(c, "get_include_shared_dirs", lambda: False)
    assert [r["name"] for r in c._transition_rows(u)] == []


def test_unreadable_fleet_tier_skips_the_tick(env, monkeypatch):
    c, u = env["c"], env["u"]
    lib = env["home"] / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "skill_tier.py").write_text("raise RuntimeError('half-written')\n", encoding="utf-8")
    _write_skill(env["home"] / "skills", "local-20d")
    data = u.load_usage()
    data["local-20d"] = _record(20)
    u.save_usage(data)
    monkeypatch.setattr(c, "_load_config", lambda: {"stale_after_days": 14, "archive_after_days": 30})
    counts = c.apply_automatic_transitions()
    assert counts.get("skipped_unreadable_tier") == 1
    assert counts["marked_stale"] == 0 and counts["archived"] == 0
    assert u.get_record("local-20d")["state"] == "active"


def test_gate_cache_is_keyed_by_fleet_root(env, tmp_path):
    cs = env["cs"]
    gate = ("import argparse, json, os\nap = argparse.ArgumentParser(); ap.add_argument('--home');"
            " ap.add_argument('--json', action='store_true'); ap.add_argument('names', nargs='+')\n"
            "a = ap.parse_args(); bl = json.load(open(os.path.join(a.home, 'bl.json')))\n"
            "print(json.dumps({'ok': True, 'skills': {n: {'archivable': not bl.get(n), 'blockers': bl.get(n, [])}"
            " for n in a.names}}))\n")
    roots = []
    for name, bl in (("A", {}), ("B", {"s": ["pinned (cron/launchd/symlink)"]})):
        r = tmp_path / name
        (r / "scripts").mkdir(parents=True)
        (r / "scripts" / "skill-archive-gate.py").write_text(gate, encoding="utf-8")
        (r / "bl.json").write_text(json.dumps(bl), encoding="utf-8")
        roots.append(r)
    with cs.archive_batch():
        assert cs.archive_gate(["s"], roots[0]) == {"s": (True, [])}
        assert cs.archive_gate(["s"], roots[1]) == {"s": (False, ["pinned (cron/launchd/symlink)"])}


def test_alias_iterator_reaches_the_gate(env, monkeypatch):
    cs = env["cs"]
    d = _write_skill(env["shared"] / "devops", "dir-name")
    monkeypatch.setattr(cs, "ladder_days", lambda root=None: (60.0, 120.0))
    monkeypatch.setattr(cs, "shared_skill_idle_days",
                        lambda name, root, record=None, now=None, aliases=(): (list(aliases) and 121.0, 60.0, "x"))
    asked = []
    monkeypatch.setattr(cs, "archive_gate",
                        lambda names, root=None: (asked.append(list(names)),
                                                  {n: (n != "public-name", [] if n != "public-name" else ["pinned"])
                                                   for n in names})[1])
    ok, msg, _t = cs.archive_shared_skill(d, shared_root=env["shared"], aliases=iter(["public-name"]))
    assert asked == [["dir-name", "public-name"]]
    assert not ok and "pinned" in msg
    assert d.is_dir()


def test_effective_default_windows_are_60_120_with_the_real_loader(env):
    """No _load_config stub: DEFAULT_CONFIG merges in. A plain install (no fleet tier lib) reads 60/120."""
    c = env["c"]
    assert c._fleet_ladder() is None
    assert (c.get_stale_after_days(), c.get_archive_after_days()) == (60, 120)
