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


def _lost_generation_writer(calls, pins, *, lost=True, pinned=False):
    """A writable handle on a runtime without setconfig (< 3.12): the open-time retain is bound."""
    db = _orphan_handle(calls)
    db.__dict__.update(read_only=False, _db_wal_generation_lost=lost, _connection_pinned=pinned,
                       _retire_connection=pins.append, _ckpt_conn=None)
    return db


def test_del_pins_a_lost_generation_while_finalizing():
    """Prism 24f888f2a0c1: the finalizing return must not skip the < 3.12 lost-generation pin."""
    calls, pins = [], []
    db = _lost_generation_writer(calls, pins)
    conn = db.__dict__["_conn"]
    db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
    assert pins == [conn] and pins[0] is conn
    assert db.__dict__["_connection_pinned"] is True
    assert calls == []
    db.__dict__["_conn"] = None


def test_del_does_not_pin_without_a_recorded_loss_while_finalizing():
    calls, pins = [], []
    db = _lost_generation_writer(calls, pins, lost=False)
    db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
    assert pins == [] and calls == []
    db.__dict__["_conn"] = None


def test_del_does_not_pin_twice_while_finalizing():
    calls, pins = [], []
    db = _lost_generation_writer(calls, pins, pinned=True)
    db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
    assert pins == [] and calls == []
    db.__dict__["_conn"] = None


def test_del_still_closes_a_forgotten_handle_at_runtime():
    calls = []
    db = _orphan_handle(calls)
    db.__del__(_finalizing=lambda: False)
    assert calls == ["close"]
    db.__dict__["_conn"] = None
