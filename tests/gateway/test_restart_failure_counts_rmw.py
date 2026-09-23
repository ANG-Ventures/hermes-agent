# fork-only: behavioural + class-sweep regression for the counts-file RMW race.
"""Every mutation of the restart-failure-counts file must be one atomic RMW.

``atomic_json_write`` makes each WRITE atomic.  It does NOT make the
read-modify-write PAIR atomic, and every mutator of
``.restart_failure_counts`` is a pair::

    counts = self._load_restart_failure_counts()
    ... mutate ...
    self._save_restart_failure_counts(counts)

This was harmless only by accident, because all five call sites ran inline on
the event loop and the loop serialized them.  The deferred-restart off-loop
change broke that accident: ``_record_restart_replay_mark`` is now handed to
``asyncio.to_thread`` (``gateway/run.py`` ``record_replay=``) while
``_clear_restart_replay_marks`` (from ``async def _handle_message_with_agent``)
and ``_increment_restart_failure_counts`` (from ``async def _stop_impl_body``)
remain loop-reachable.  Two overlapping cycles each read the pre-state and the
later save clobbers the earlier one.

The consequence is not cosmetic: ``replay_marks`` is the F2 replay-loop
breaker's evidence.  A lost update either drops a real relapse (the breaker
never arms, so a looping session keeps restarting the gateway) or persists a
stale snapshot (a spurious auto-suspend that clears a live session's history).

The fix is a choke point -- ``GatewayRunner._restart_failure_counts_rmw()`` --
holding a per-path process lock across load+mutate+save.  The sweep below is
the ENFORCEMENT: a new mutator that hand-rolls the pair fails the gate without
anyone having to remember a list.
"""

from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest

import gateway.run as run_mod


# ---------------------------------------------------------------------------
# Behaviour: two concurrent RMW cycles must both survive.
# ---------------------------------------------------------------------------


class _CountsRunner:
    """A bare object carrying the real counts-file RMW methods.

    Constructing a full GatewayRunner needs the whole gateway; the race lives
    entirely in these four methods plus the path, so this binds exactly those.
    """

    _STUCK_LOOP_THRESHOLD = run_mod.GatewayRunner._STUCK_LOOP_THRESHOLD

    _decode_restart_failure_entry = run_mod.GatewayRunner.__dict__[
        "_decode_restart_failure_entry"
    ]
    _encode_restart_failure_entry = run_mod.GatewayRunner.__dict__[
        "_encode_restart_failure_entry"
    ]
    _load_restart_failure_counts = run_mod.GatewayRunner._load_restart_failure_counts
    _save_restart_failure_counts = run_mod.GatewayRunner._save_restart_failure_counts
    _restart_failure_counts_rmw = run_mod.GatewayRunner._restart_failure_counts_rmw

    def __init__(self, path: Path) -> None:
        self._path = path

    def _restart_failure_counts_path(self) -> Path:
        return self._path


def _one_cycle(runner: _CountsRunner, key: str, gate: threading.Event, hold: float) -> None:
    """One mutation written exactly the way the production sites are written."""
    with runner._restart_failure_counts_rmw() as counts:
        # Both threads have now READ. Whoever saves last wins if the pair is
        # not serialized.
        gate.wait(timeout=5)
        time.sleep(hold)
        counts[key] = {"count": 1, "replay_marks": [time.time()], "armed": False}


def test_concurrent_counts_mutations_do_not_lose_an_update(tmp_path):
    """Assert on the DURABLE FILE, not on which API was called.

    On an unguarded ``load -> mutate -> save`` this fails with only one key in
    the file: the slower cycle's save overwrites the faster one's with a
    snapshot that predates it.
    """
    path = tmp_path / "restart_failure_counts.json"
    runner = _CountsRunner(path)

    gate = threading.Event()
    threads = [
        threading.Thread(target=_one_cycle, args=(runner, "session-A", gate, 0.25)),
        threading.Thread(target=_one_cycle, args=(runner, "session-B", gate, 0.05)),
    ]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(10)
        assert not t.is_alive()

    durable = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert sorted(durable) == ["session-A", "session-B"], (
        "a concurrent read-modify-write on the restart-failure-counts file lost "
        f"an update; durable keys = {sorted(durable)}. atomic_json_write makes "
        "each WRITE atomic, not the load/mutate/save PAIR — a lost replay_marks "
        "update either drops a real relapse or persists a stale snapshot."
    )


def test_the_rmw_does_not_write_when_the_body_changed_nothing(tmp_path):
    """The read-only / already-recorded paths must not rewrite the file.

    ``_record_restart_replay_mark`` returns early when the request id was
    already recorded, and ``_clear_restart_replay_marks`` returns early when
    there is no entry.  The context manager resumes on a normal ``__exit__``
    even after an early ``return``, so an unconditional save there would
    rewrite (and fsync) the file on every no-op call.
    """
    path = tmp_path / "restart_failure_counts.json"
    runner = _CountsRunner(path)

    with runner._restart_failure_counts_rmw() as counts:
        counts["s"] = {"count": 2, "replay_marks": [], "armed": False}
    assert path.exists()
    mtime = path.stat().st_mtime_ns

    time.sleep(0.01)
    with runner._restart_failure_counts_rmw() as counts:
        assert "s" in counts  # read only

    assert path.stat().st_mtime_ns == mtime, (
        "a read-only RMW cycle rewrote the counts file"
    )


def test_an_exception_in_the_body_persists_nothing(tmp_path):
    """A half-applied mutation must never reach disk."""
    path = tmp_path / "restart_failure_counts.json"
    runner = _CountsRunner(path)

    with runner._restart_failure_counts_rmw() as counts:
        counts["keep"] = {"count": 1, "replay_marks": [], "armed": False}

    with pytest.raises(RuntimeError):
        with runner._restart_failure_counts_rmw() as counts:
            counts["torn"] = {"count": 9, "replay_marks": [], "armed": False}
            raise RuntimeError("boom")

    durable = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(durable) == ["keep"], (
        f"a failed RMW body persisted a partial mutation: {sorted(durable)}"
    )


# ---------------------------------------------------------------------------
# CLASS SWEEP: no method may hand-roll the pair.
# ---------------------------------------------------------------------------

_LOAD = "_load_restart_failure_counts"
_SAVE = "_save_restart_failure_counts"
_RMW = "_restart_failure_counts_rmw"


def _run_py() -> Path:
    return Path(run_mod.__file__).resolve()


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            names.add(sub.func.attr)
        elif isinstance(sub, ast.With):
            for item in sub.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    names.add(call.func.attr)
    return names


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_no_function_hand_rolls_the_counts_read_modify_write():
    """Every mutator must go through the locked choke point.

    This is the CLASS gate, not an inventory: any function that calls both the
    loader and the saver is doing an unguarded pair, whatever it is named and
    whenever it is added.  The only legal co-occurrence is inside
    ``_restart_failure_counts_rmw`` itself.
    """
    tree = ast.parse(_run_py().read_text(encoding="utf-8"))

    offenders = []
    for fn in _functions(tree):
        if fn.name == _RMW:
            continue
        names = _calls(fn)
        if _LOAD in names and _SAVE in names:
            offenders.append(f"{fn.name} (line {fn.lineno})")

    assert not offenders, (
        "these functions hand-roll a load/mutate/save on the restart-failure-"
        f"counts file instead of using `{_RMW}()`: {offenders}. "
        "atomic_json_write makes each write atomic, not the pair — with the "
        "deferred-restart arm off-loop, two of these can interleave and lose "
        "an update."
    )


def test_the_choke_point_is_actually_used():
    """Keep the sweep above non-vacuous.

    If a refactor renamed the loader/saver, the sweep would go green because it
    finds nothing at all.  Pin that the real mutators exist and route through
    the choke point.  Measured on the commit that added this gate: 6 mutator
    call sites.
    """
    tree = ast.parse(_run_py().read_text(encoding="utf-8"))

    users = [fn.name for fn in _functions(tree) if _RMW in _calls(fn) and fn.name != _RMW]
    assert len(users) >= 6, (
        f"only {len(users)} function(s) use `{_RMW}()` ({sorted(users)}); the "
        "sweep above is green for the wrong reason"
    )

    rmw = [fn for fn in _functions(tree) if fn.name == _RMW]
    assert len(rmw) == 1, f"expected exactly one {_RMW} definition, found {len(rmw)}"
    body_calls = _calls(rmw[0])
    assert _LOAD in body_calls and _SAVE in body_calls, (
        f"{_RMW} no longer performs the load/save pair it is supposed to own"
    )
