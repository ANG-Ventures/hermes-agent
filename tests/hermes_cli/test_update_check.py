"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh AND HEAD matches, return cached value without a fetch.

    The cache key includes the local HEAD SHA, so the cached entry must carry
    the current HEAD for the fast-path to fire. A cheap ``git rev-parse HEAD``
    still runs to read the current HEAD, but no ``git fetch`` / ``rev-list``
    network/compute work happens.
    """
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    head = "a" * 40
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({"ts": time.time(), "behind": 3, "ver": __version__, "head": head})
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fake_run(cmd, *a, **k):
        # Only the HEAD read should occur; fetch/rev-list must NOT.
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return MagicMock(returncode=0, stdout=head + "\n")
        raise AssertionError(f"unexpected git call on cache hit: {cmd}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = check_for_updates()

    assert result == 3


def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    # ORDERING WITNESSES — replace `assert elapsed < 1.0`, which made the OS
    # scheduler part of the assertion. The fact it stood in for is that the
    # check is still *running* when prefetch_update_check() returns, i.e. the
    # caller did not wait for it.
    release = threading.Event()
    entered = threading.Event()
    check_returned = threading.Event()

    def _blocking_check():
        entered.set()
        try:
            release.wait(timeout=10.0)
            return 5
        finally:
            check_returned.set()

    with patch.object(banner, "check_for_updates", side_effect=_blocking_check):
        banner.prefetch_update_check()

        assert not check_returned.is_set(), (
            "prefetch_update_check blocked on check_for_updates — it ran "
            "inline instead of on the background thread"
        )
        assert entered.wait(timeout=10.0), "the update check never ran off-thread"
        assert not check_returned.is_set()

        # Let the background thread finish and confirm it published its result.
        release.set()
        assert banner._update_check_done.wait(timeout=10)
        assert banner._update_result == 5


