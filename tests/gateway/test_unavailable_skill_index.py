"""The unavailable-skill hint must not walk the skills tree per call, nor on the loop.

2026-09-24 04:51: an unknown /command made ``_check_unavailable_skill`` rglob +
read ~906 SKILL.md files synchronously on the gateway event loop; the loop
stalled past the 90 s liveness watchdog and Apollo was hard-killed.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from gateway import run as gateway_run
from gateway import shutdown_watchdog
from tools import skill_usage


def _write_skill(root: Path, rel: str, name: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: t\n---\nBody\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_index():
    gateway_run._invalidate_skill_slug_index()
    yield
    gateway_run._invalidate_skill_slug_index()


def test_index_walks_once_then_serves_from_cache(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _write_skill(root, "cat/alpha", "Alpha Skill")
    calls = []
    real = gateway_run._skill_slug_from_frontmatter
    monkeypatch.setattr(
        gateway_run, "_skill_slug_from_frontmatter",
        lambda p: (calls.append(p), real(p))[1],
    )
    idx1 = gateway_run._skill_slug_index((root,))
    idx2 = gateway_run._skill_slug_index((root,))
    assert "alpha-skill" in idx1 and idx2 is idx1
    assert len(calls) == 1  # second call did not re-read


def test_usage_sidecars_do_not_rebuild_index(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _write_skill(root, "cat/alpha", "Alpha")
    monkeypatch.setattr(skill_usage, "_skills_dir", lambda: root)
    reads = []
    real = gateway_run._skill_slug_from_frontmatter
    monkeypatch.setattr(
        gateway_run, "_skill_slug_from_frontmatter",
        lambda p: (reads.append(p), real(p))[1],
    )
    initial = gateway_run._skill_slug_index((root,))
    assert len(reads) == 1
    for writer in (
        lambda: skill_usage.bump_view("alpha"),
        lambda: skill_usage.bump_use("alpha"),
        lambda: skill_usage.record_create_destination("alpha", shared=False),
    ):
        writer()
        assert gateway_run._skill_slug_index((root,)) is initial
        assert len(reads) == 1


def test_hidden_curator_churn_does_not_rebuild_index(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "cat/alpha", "Alpha")
    initial = gateway_run._skill_slug_index((root,))
    for hidden in (".archive", ".curator_backups", ".hub"):
        _write_skill(root, f"{hidden}/obsolete", "Obsolete")
        assert gateway_run._skill_slug_index((root,)) is initial


def test_index_rebuilds_when_a_flat_skill_is_added(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "Alpha")
    assert "beta" not in gateway_run._skill_slug_index((root,))
    _write_skill(root, "beta", "Beta")
    assert "beta" in gateway_run._skill_slug_index((root,))


def test_index_rebuilds_when_a_flat_skill_is_removed(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "Alpha")
    assert "alpha" in gateway_run._skill_slug_index((root,))
    (root / "alpha" / "SKILL.md").unlink()
    (root / "alpha").rmdir()
    assert "alpha" not in gateway_run._skill_slug_index((root,))


def test_index_rebuilds_when_a_skill_is_added(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "cat/alpha", "Alpha")
    assert "beta" not in gateway_run._skill_slug_index((root,))
    time.sleep(0.01)
    _write_skill(root, "cat/beta", "Beta")  # bumps cat/ mtime
    assert "beta" in gateway_run._skill_slug_index((root,))


def test_invalidate_forces_rebuild(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "Alpha")
    first = gateway_run._skill_slug_index((root,))
    gateway_run._invalidate_skill_slug_index()
    assert gateway_run._skill_slug_index((root,)) is not first


def test_async_hint_is_bounded_and_does_not_block_the_loop(monkeypatch):
    release = threading.Event()

    def slow(_name):
        release.wait(5)
        return "hint"

    monkeypatch.setattr(gateway_run, "_check_unavailable_skill", slow)
    monkeypatch.setattr(gateway_run, "_UNAVAILABLE_SKILL_HINT_BUDGET_S", 0.2)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        start = time.monotonic()
        got = await gateway_run._check_unavailable_skill_async("nope")
        elapsed = time.monotonic() - start
        t.cancel()
        return got, elapsed, ticks

    try:
        got, elapsed, ticks = asyncio.run(main())
    finally:
        release.set()
    assert got is None
    assert elapsed < 1.0
    assert ticks >= 5  # loop kept dispatching while the walk ran off-loop


def test_watchdog_names_the_blocked_site():
    ready = threading.Event()
    stop = threading.Event()

    def blocked_in_repo_code():
        ready.set()
        stop.wait(5)

    th = threading.Thread(target=blocked_in_repo_code)
    th.start()
    try:
        ready.wait(2)
        site, stack = shutdown_watchdog.describe_blocked_loop_thread(th.ident)
    finally:
        stop.set()
        th.join()
    assert "test_unavailable_skill_index.py" in site
    assert "blocked_in_repo_code" in site
    assert "blocked_in_repo_code" in stack


def test_watchdog_site_line_is_parseable_by_restart_notice(caplog):
    from gateway.fork_ext.unclean_restart_notice import _BLOCKED_SITE_RE

    ready = threading.Event()
    stop = threading.Event()

    def blocked_in_repo_code():
        ready.set()
        stop.wait(5)

    th = threading.Thread(target=blocked_in_repo_code)
    th.start()
    try:
        ready.wait(2)
        with caplog.at_level("ERROR", logger="gateway.shutdown_watchdog"):
            shutdown_watchdog._log_blocked_loop_site(th.ident, 90)
    finally:
        stop.set()
        th.join()
    lines = [r.getMessage() for r in caplog.records]
    site_line = next(l for l in lines if "seconds=90 site=" in l)
    m = _BLOCKED_SITE_RE.search(site_line)
    assert m and m.group(1).endswith("blocked_in_repo_code"), site_line
    assert any("loop-thread stack:" in l and "blocked_in_repo_code" in l for l in lines)
