"""SessionDB.__del__ must not run close() while the interpreter finalizes.

close() lazily imports hermes_state_registry; during finalization that import
blocks forever on the import lock, so a gateway process that dropped a shared
SessionDB without closing it hung at exit instead of exiting (t_a29b8266, the
cell-5 delivery-outbox conformance boots).
"""

from hermes_state import SessionDB


def _orphan_handle(calls):
    db = object.__new__(SessionDB)
    db.__dict__["_conn"] = object()  # an open handle the caller forgot to close
    db.close = lambda: calls.append("close")
    return db


def test_del_skips_close_while_finalizing():
    calls = []
    db = _orphan_handle(calls)
    db.__del__(_finalizing=lambda: True)
    assert calls == []
    db.__dict__["_conn"] = None  # the real GC-time __del__ is then a no-op


def test_del_still_closes_a_forgotten_handle_at_runtime():
    calls = []
    db = _orphan_handle(calls)
    db.__del__(_finalizing=lambda: False)
    assert calls == ["close"]
    db.__dict__["_conn"] = None
