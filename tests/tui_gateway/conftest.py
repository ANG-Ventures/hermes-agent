"""Shared isolation for the ``tui_gateway.server`` RPC method table.

``tui_gateway/server.py`` builds its route table ONCE, at import time, via the
``@method("route")`` decorator writing into the module-level ``_methods`` dict.
Production never mutates ``_methods`` again after import — every runtime lookup
(``handle_request``, ``dispatch``, ``tui_gateway/compute_host.py``) is
read-only. The dict is effectively immutable module state for the life of the
process.

Several tests in this directory install fake handlers into that table to drive
``dispatch()``'s pool-vs-inline routing (``session.compress``,
``complete.path``/``complete.slash`` in ``test_protocol.py``;
``session.list``/``pet.info``/``prompt.submit`` in
``test_inline_rpc_gil_starvation.py``). Because the table is module state and
the per-file ``server`` fixtures deliberately do NOT reload the module (a
reload re-registers the module's atexit hooks and races the stderr buffer at
interpreter shutdown), an unscoped stub LEAKS for the rest of the pytest
process: every later test that exercises the real route gets the stub.

That is a pure test-isolation defect — ``server.py`` itself has no cross-file
state leak here — but it is invisible until a later file happens to depend on a
clobbered route, at which point it surfaces only under full-directory ordering.

Two layers of defence:

1. Every stubbing site uses ``monkeypatch.setitem(server._methods, ...)``, so
   the stub is unwound by pytest at test teardown by construction.
2. This autouse belt-and-braces fixture snapshots the table around each test
   and repairs it if anything still escapes.

The fixture must NOT import ``tui_gateway.server`` itself. Several ``server``
fixtures here import it under ``patch.dict("sys.modules", {...})`` that stubs
``hermes_constants``/``hermes_state``; an eager conftest import would win the
race and change what the whole directory sees (it makes
``agent.pet.store.pets_dir()`` bind a real ``get_hermes_home``, etc.). Read
``sys.modules`` instead and no-op until something else has done the import.
"""

import sys

import pytest

_PRISTINE: dict = {}


@pytest.fixture(autouse=True)
def _restore_server_method_table():
    """Repair ``tui_gateway.server._methods`` if a test leaked a stub route."""

    def _table():
        module = sys.modules.get("tui_gateway.server")
        return getattr(module, "_methods", None) if module is not None else None

    table = _table()
    if table is not None and not _PRISTINE:
        _PRISTINE.update(table)
    try:
        yield
    finally:
        table = _table()
        if table is None:
            return
        if not _PRISTINE:
            _PRISTINE.update(table)
        elif table != _PRISTINE:
            table.clear()
            table.update(_PRISTINE)
