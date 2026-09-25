"""The restart-failure counts file is mutated by read-modify-write cycles; overlapping cycles must
not lose updates (a cleared session key must not be resurrected by a concurrent clear)."""

import asyncio
import json
import threading
import time

import pytest

import utils
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def runner_home(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    return runner, tmp_path / runner._STUCK_LOOP_FILE


@pytest.mark.asyncio
async def test_concurrent_clears_do_not_resurrect_each_others_keys(runner_home, monkeypatch):
    runner, path = runner_home
    path.write_text(json.dumps({"s:a": 1, "s:b": 1, "s:c": 2}))

    real_write = utils.atomic_json_write
    in_write = threading.Event()

    def slow_write(p, data, **kw):
        # Widen the window between read and write so an unserialized pair interleaves.
        in_write.set()
        time.sleep(0.2)
        return real_write(p, data, **kw)

    monkeypatch.setattr(utils, "atomic_json_write", slow_write)

    await asyncio.gather(
        runner._clear_restart_failure_count("s:a"),
        runner._clear_restart_failure_count("s:b"),
    )

    assert in_write.is_set()
    assert json.loads(path.read_text()) == {"s:c": 2}


@pytest.mark.asyncio
async def test_clear_last_key_unlinks_file(runner_home):
    runner, path = runner_home
    path.write_text(json.dumps({"s:a": 1}))
    await runner._clear_restart_failure_count("s:a")
    assert not path.exists()


@pytest.mark.asyncio
async def test_clear_tolerates_corrupt_file(runner_home):
    runner, path = runner_home
    path.write_text("[1, 2")
    await runner._clear_restart_failure_count("s:a")  # must not raise
