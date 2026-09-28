"""C5 #41 (PR #978): the aux cost sink is a dict shared with worker threads
(a detached stalled compression worker keeps a copied context); its
read-modify-writes must not lose updates."""

from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace

from agent import auxiliary_client as aux


class _YieldingDict(dict):
    """Yields the GIL inside every read, as a real interleaving would."""

    def get(self, key, default=None):
        value = super().get(key, default)
        time.sleep(0.0005)
        return value


def test_concurrent_calls_on_one_sink_lose_no_updates():
    sink = _YieldingDict()
    threads_n, calls_n = 8, 25

    def worker(ctx):
        for _ in range(calls_n):
            ctx.run(aux._record_aux_call_cost,
                    SimpleNamespace(usage=None, model="m"), {}, streamed=False)

    with aux.aux_cost_sink(sink):
        ctxs = [contextvars.copy_context() for _ in range(threads_n)]
    threads = [threading.Thread(target=worker, args=(c,)) for c in ctxs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sink["calls"] == threads_n * calls_n
    assert sink["unknown"] is True
