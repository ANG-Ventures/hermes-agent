"""Carry scanner parity and one-shot dispatch regressions (t_3489445d).

Routing deliberately uses main's synchronous CarryScanBudget path. Only the
filesystem census implementation differs; no background state is required.
"""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worker_hosts as kwh
from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import _write_policy


@pytest.fixture(autouse=True)
def _carry_caps_on(monkeypatch):
    """t_f3a411c2: this suite pins the capped in-tick carry (kanban.placement.carry_caps: true, the kill switch).
    The default background carry with no cap is test_kanban_carry_background.py."""
    from hermes_cli import kanban_carry_background as _kcb
    monkeypatch.setattr(_kcb, "caps_on", lambda kcfg=None: True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    kwh._carry_scan_cache.clear()
    kwh._carry_waiting.clear()
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None: None)


def test_one_shot_public_dispatch_cold_cache(kanban_home):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="lf", assignee="alpha")
        path = kbw.resolve_workspace(kb.get_task(conn, tid))
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "f.txt").write_text("x")
        kbw.set_workspace_path(conn, tid, str(path))
        _, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0,
                           spawn_paused="saturated:cpu")
    assert (tid, "ace-ai") in spawned


def test_census_does_not_build_os_walk_lists(tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")

    def forbidden(*args, **kwargs):
        raise AssertionError("census materializes os.walk lists")

    monkeypatch.setattr(os, "walk", forbidden)
    assert kwh.carry_size_ok(str(tmp_path))


@pytest.mark.parametrize("max_entries,max_bytes", [(20000, 10**9), (2, 10**9), (20000, 1)])
def test_verdict_matches_main_census(tmp_path, max_entries, max_bytes):
    """Oracle loop extracted from main 583676b772 carry_size_ok (no deadlines)."""
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "f").write_bytes(b"12345")
    (tmp_path / "top").write_bytes(b"abc")
    (tmp_path / "link").symlink_to(tmp_path / "dir", target_is_directory=True)
    (tmp_path / "dangling").symlink_to(tmp_path / "absent")
    total = entries = 0
    expected = True
    for dirpath, dirnames, filenames in os.walk(tmp_path, onerror=kwh._raise):
        for name in (*dirnames, *filenames):
            entries += 1
            total += os.lstat(os.path.join(dirpath, name)).st_size
            if entries > max_entries or total > max_bytes:
                expected = False
                break
        if not expected:
            break
    assert kwh.carry_size_ok(str(tmp_path), max_entries=max_entries,
                             max_bytes=max_bytes) is expected


def test_missing_root_matches_main(tmp_path):
    # os.walk onerror raises for a missing root; it is not an empty carryable tree.
    assert not kwh.carry_size_ok(str(tmp_path / "missing"))


def test_huge_directory_stops_at_cap(tmp_path, monkeypatch):
    """The iterator itself must stop, not just the stat loop after list building."""
    consumed = []

    class Entry:
        path = "/unused"

        def stat(self, **kwargs):
            return type("Stat", (), {"st_size": 0, "st_mode": 0})()

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            for i in range(20000):
                consumed.append(i)
                yield Entry()

    monkeypatch.setattr(os, "scandir", lambda path: Scan())
    assert not kwh.carry_size_ok(str(tmp_path), max_entries=2)
    assert len(consumed) == 3


@pytest.mark.parametrize("case", ["empty", "small", "at_entries", "over_entries",
                                  "at_bytes", "over_bytes", "dir_link", "file_link",
                                  "broken_link", "hardlinks", "deep", "non_utf8",
                                  "unreadable"])
def test_extracted_main_verdict_parity(tmp_path, monkeypatch, case):
    from tests.hermes_cli import carry_census_main_oracle as oracle
    root = tmp_path / "tree"
    root.mkdir()
    kwargs = {}
    if case in ("small", "at_entries", "over_entries", "at_bytes", "over_bytes"):
        (root / "f").write_bytes(b"abc")
        if case == "at_entries":
            kwargs["max_entries"] = 1
        if case == "over_entries":
            kwargs["max_entries"] = 0
        if case == "at_bytes":
            kwargs["max_bytes"] = 3
        if case == "over_bytes":
            kwargs["max_bytes"] = 2
    elif case in ("dir_link", "file_link", "broken_link"):
        target = tmp_path / "outside"
        if case == "dir_link":
            target.mkdir()
            (target / "uncounted").write_bytes(b"x" * 1000)
        elif case == "file_link":
            target.write_bytes(b"x" * 1000)
        (root / "link").symlink_to(target, target_is_directory=case == "dir_link")
        kwargs["max_entries"] = 1
    elif case == "hardlinks":
        (root / "f").write_bytes(b"abc")
        os.link(root / "f", root / "g")
        kwargs["max_bytes"] = 6
    elif case == "deep":
        leaf = root
        for _ in range(100):
            leaf = leaf / "d"
            leaf.mkdir()
        (leaf / "f").touch()
    elif case == "non_utf8":
        fd = os.open(os.fsencode(root) + b"/bad-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    elif case == "unreadable":
        locked = root / "locked"
        locked.mkdir()
        original = os.scandir

        def unreadable(path):
            if os.fspath(path) == str(locked):
                raise PermissionError("fixture unreadable directory")
            return original(path)

        monkeypatch.setattr(os, "scandir", unreadable)
    oracle._carry_scan_cache.clear()
    expected = oracle.carry_size_ok(str(root), **kwargs)
    assert kwh.carry_size_ok(str(root), **kwargs) is expected


def _clear():
    from tests.hermes_cli import carry_census_main_oracle as oracle
    for mod in (oracle, kwh):
        mod._carry_scan_cache.clear()
        mod._carry_waiting.clear()


def test_directory_swapped_for_a_symlink_after_its_stat(tmp_path, monkeypatch):
    """Prism 9ea482d5451b: a child directory is replaced by a symlink after the census has statted it and
    before the census descends into it. Main (os.walk, followlinks=False) rechecks and skips the link."""
    from tests.hermes_cli import carry_census_main_oracle as oracle
    other = tmp_path / "other"
    other.mkdir()
    (other / "a").touch()
    (other / "b").touch()
    real = os.scandir
    out = {}
    for label, mod in (("main", oracle), ("head", kwh)):
        root = tmp_path / ("tree-" + label)
        (root / "d").mkdir(parents=True)
        target = os.path.realpath(root)

        class Swap:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.it.close()

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    return next(self.it)
                except StopIteration:
                    d = root / "d"
                    if not d.is_symlink():
                        d.rmdir()
                        d.symlink_to(other, target_is_directory=True)
                    raise

            def close(self):
                self.it.close()

        def scandir(path, _target=target, _Swap=Swap, _root=root):
            it = real(path)
            return _Swap(it) if os.path.realpath(os.fspath(path)) == _target and not (_root / "d").is_symlink() else it

        _clear()
        with monkeypatch.context() as m:
            m.setattr(os, "scandir", scandir)
            out[label] = mod.carry_size_ok(str(root), max_entries=2, max_bytes=10 ** 9)
        assert (root / "d").is_symlink(), "the swap did not happen"
    assert out["main"] is True, out
    assert out["head"] is out["main"], out
