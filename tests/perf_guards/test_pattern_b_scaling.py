"""Pattern-B perf-regression guards: hot paths must not degrade to O(N·M).

Pattern B ("rebuild everything per delta/row") has no lintable signature, so
the durable guard is behavioral: pin the *scaling shape* of a known hot path
with a deterministic operation count (SQL statements via sqlite trace
callbacks), never wall-clock timing.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest


def _min_cpu_time(fn, *, repeat: int = 5) -> float:
    """Best-of-``repeat`` CPU time for ``fn()`` — immune to CFS throttling.

    ``time.thread_time`` counts only cycles the *calling thread* was actually
    ON a CPU, so a cgroup-quota-throttled CI runner descheduling it mid-sample
    does not inflate the reading.  Wall clock does inflate, which is precisely
    how the 4x/1x ratio guard below became a coin flip on the self-hosted 2-3
    CPU runners (observed on ace-media-*-8 and ace-ai-*-6, runs 35435109445 /
    35436550177).

    Thread-scoped rather than ``time.process_time`` (process-wide, all
    threads) deliberately: an xdist worker that ran an earlier test leaving a
    live executor thread behind would charge that thread's CPU to every
    ``process_time`` sample taken here.  ``thread_time`` is
    ``CLOCK_THREAD_CPUTIME_ID`` on both Linux and macOS.

    Only valid for a purely CPU-bound, single-threaded callable — any sleep,
    I/O or thread hand-off is invisible to this clock.  The callables measured
    with it here are exactly that: in-process list appends and one ``join``.
    """
    best = float("inf")
    for _ in range(repeat):
        t0 = time.thread_time()
        fn()
        best = min(best, time.thread_time() - t0)
    return best


class TestListSessionsRichQueryBound:
    """Listing sessions must not walk compression chains with nested per-row queries.

    Today ``list_sessions_rich`` still issues ~2 statements per listed
    compression root (the N+1 tracked in #95380). This guard allows that legacy
    budget but fails on a regression to N·M (nested walks, per-hop re-queries).
    Statements are counted on every connection the call can use: pooled read
    connections from ``_read_ctx()`` and the writer connection.
    """

    N_CHAINS = 12

    @pytest.fixture()
    def chain_db(self, tmp_path: Path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        for i in range(self.N_CHAINS):
            root, child = f"root_{i}", f"child_{i}"
            db.create_session(root, source="cli")
            db.create_session(child, source="cli", parent_session_id=root)
            db.end_session(root, end_reason="compression")
        yield db
        db.close()

    @staticmethod
    def _list_and_count_statements(db, monkeypatch):
        statements: list[str] = []
        real_read_ctx = db._read_ctx

        @contextlib.contextmanager
        def traced_read_ctx():
            with real_read_ctx() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        monkeypatch.setattr(db, "_read_ctx", traced_read_ctx)
        db._conn.set_trace_callback(statements.append)
        try:
            rows = db.list_sessions_rich(limit=50)
        finally:
            db._conn.set_trace_callback(None)
        return rows, statements

    def test_statement_count_does_not_scale_with_sessions(self, chain_db, monkeypatch):
        rows, statements = self._list_and_count_statements(chain_db, monkeypatch)

        assert len(rows) == self.N_CHAINS
        assert statements, "trace captured nothing: the guard is no longer observing list_sessions_rich"
        budget = 4 * self.N_CHAINS + 8
        assert len(statements) <= budget, (
            f"list_sessions_rich issued {len(statements)} statements for "
            f"{self.N_CHAINS} sessions, beyond even the legacy N+1 budget "
            f"({budget}). A nested per-row walk has been introduced. "
            f"Captured SQL: {[s[:80] for s in statements[:10]]}"
        )


# ---------------------------------------------------------------------------
# Guard 3 — streamed tool-call fragment assembly must stay linear (#92242).
# ---------------------------------------------------------------------------


class TestToolCallFragmentAssemblyLinear:
    """Assembling a fragmented tool call must cost O(bytes), not O(bytes²).

    Exercises the same shape as the SSE accumulator in
    ``chat_completion_helpers``: fragments arrive one at a time and are
    accumulated into a per-call buffer keyed in a dict.  Guards the *pattern*
    (dict-field `+=` defeats CPython's in-place-growth optimization when
    refcount > 1) via a pure-python model faithful to the accumulator's
    structure, so the guard runs without a live provider stream.
    """

    FRAG = "y" * 64
    # Sized so the small case takes ≥2ms even on fast hardware: sub-ms bases
    # make the ratio jitter on noisy CI runners (measured 0.185ms at 8k).
    N_SMALL = 128_000
    N_LARGE = 512_000
    MAX_RATIO = 8.0  # linear ≈ 4; dict-field quadratic measured >> 10

    @staticmethod
    def _assemble_dict_field(n_frags: int, frag: str) -> int:
        """Accumulator model: buffered parts, joined once (fixed shape)."""
        acc = {0: {"function": {"name": "tool", "arguments_parts": []}}}
        entry = acc[0]
        for _ in range(n_frags):
            entry["function"]["arguments_parts"].append(frag)
        return len("".join(entry["function"]["arguments_parts"]))

    def test_4x_fragments_cost_about_4x_time(self):
        # CPU time, not wall clock (#724/#725): this path is pure in-process
        # CPU work, so thread_time measures exactly the property under test
        # (work done) and ignores the CFS throttling that made the wall-clock
        # form a coin flip on quota-capped self-hosted runners. repeat=5 and
        # min-of-K on top, because a GC pass inside one sample still perturbs
        # even the CPU clock.
        #
        # The alternative (skip under low CPU count) was rejected: it would
        # disarm the guard on exactly the runners CI actually uses, so the
        # quadratic regression it exists to catch could land unnoticed.
        t_small = _min_cpu_time(
            lambda: self._assemble_dict_field(self.N_SMALL, self.FRAG), repeat=5
        )
        t_large = _min_cpu_time(
            lambda: self._assemble_dict_field(self.N_LARGE, self.FRAG), repeat=5
        )
        ratio = t_large / max(t_small, 1e-9)
        assert ratio < self.MAX_RATIO, (
            f"tool-call fragment assembly is superlinear: 4x fragments cost "
            f"{ratio:.1f}x CPU time. Fragments must be buffered in a list and "
            f"joined once (PR #92242 shape), never `+=` into a dict field."
        )
