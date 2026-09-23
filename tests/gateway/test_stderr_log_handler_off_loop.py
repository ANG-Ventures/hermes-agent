"""The gateway's stderr log handler must never write on the caller's thread.

RED-proven 2026-09-23 on Apollo: launchd points stderr at logs/gateway.error.log;
the -v/-q stderr StreamHandler was attached DIRECTLY to root, so a WARNING on
the event loop became a synchronous disk write (PHASE=event_loop_blocked
seconds=10 site=adapter.py:2396 _liveness_loop ... logging/__init__.py:1113
stream.write). Known-bad revision: any tree where gateway/run.py calls
``logging.getLogger().addHandler(_stderr_handler)``.
"""
from __future__ import annotations

import ast
import logging
import threading
import time
from pathlib import Path

import hermes_logging

RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def test_source_contract_stderr_handler_is_queued():
    src = RUN_PY.read_text()
    assert "_stderr_handler = logging.StreamHandler(" in src
    # the direct attach is the bug
    assert "logging.getLogger().addHandler(_stderr_handler)" not in src
    assert "_queue_handler(_stderr_handler)" in src
    ast.parse(src)


class _BlockingStream:
    def __init__(self, block_s):
        self.block_s = block_s
        self.lines = []
        self.writer_threads = set()
    def write(self, s):
        self.writer_threads.add(threading.get_ident())
        time.sleep(self.block_s)
        self.lines.append(s)
    def flush(self):
        pass


def test_queued_stream_handler_does_not_block_the_caller():
    stream = _BlockingStream(1.0)
    h = logging.StreamHandler(stream)
    h.setLevel(logging.WARNING)
    hermes_logging._register_queued_handler(h)
    try:
        log = logging.getLogger("tests.gateway.stderr_offloop")
        t0 = time.monotonic()
        log.warning("loop-thread warning must return immediately")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"logger.warning blocked the caller for {elapsed:.2f}s"
        deadline = time.monotonic() + 5
        while not stream.lines and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stream.lines, "queued handler never wrote the record"
        assert threading.get_ident() not in stream.writer_threads, "write ran on the caller's thread"
    finally:
        try:
            hermes_logging._queued_file_handlers.remove(h)
        except Exception:
            pass
