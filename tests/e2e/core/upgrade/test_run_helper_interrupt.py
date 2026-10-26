"""An interrupted ``H.run`` takes its sandbox with it (t_395344c0).

The harness wall-clock stop arrives as an async exception inside ``communicate()``. A child left
running there keeps its PM generation leased for the rest of the module, so the NEXT test sharing
the home sees that generation survive GC (test_generation_gc: "pm repair #1 kept superseded
generations built two days ago").
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from tests.e2e.core.upgrade import _helpers as H


class _Stop(BaseException):
    pass


@pytest.mark.platforms("posix")
def test_interrupt_inside_run_kills_the_child(tmp_path):
    pidfile = tmp_path / "pid"

    def fire(*_):
        raise _Stop()

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(2)
    try:
        with pytest.raises(_Stop):
            H.run(["/bin/sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"], env=dict(os.environ),
                  cwd=tmp_path, writable=[tmp_path], timeout=120)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f"child {pid} outlived the interrupted H.run")
