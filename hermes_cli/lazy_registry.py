"""A ``dict`` whose first read runs registered one-shot fillers.

Two module-level registries are extended from model-provider plugin discovery:
``hermes_cli.config.OPTIONAL_ENV_VARS`` and ``hermes_cli.auth.PROVIDER_REGISTRY``.
Discovery imports every provider plugin (1-2 s on a populated home, and it logs
pin-factory lines), so running it at import time taxed every ``hermes kanban``
one-shot and every script that merely imported the kanban helpers. A
``LazyFilledDict`` defers that work to the first read of the mapping.

Semantics:

* Fillers run once each, in registration order, on the first read.
* Writes (``d[k] = v``, ``update``) never trigger them, so code that registers
  entries while the importing module is still initialising stays cheap.
* Reads made BY a filler (re-entrant, same thread) see the partial mapping and
  do not recurse. Other threads block until filling is complete.
* Deletions and ``setdefault``/``pop`` fill first, so they act on the same
  contents they would have seen when filling was eager.

Leaf module: stdlib only.
"""

import threading


class LazyFilledDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending: list = []
        self._filling = False
        self._fill_lock = threading.RLock()

    def add_filler(self, fn) -> None:
        with self._fill_lock:
            self._pending.append(fn)

    def _fill(self) -> None:
        if not (self._pending or self._filling):
            return
        with self._fill_lock:
            if self._filling:
                return
            self._filling = True
            try:
                while self._pending:
                    self._pending.pop(0)()
            finally:
                self._filling = False

    def __getitem__(self, key):
        self._fill()
        return super().__getitem__(key)

    def __contains__(self, key):
        self._fill()
        return super().__contains__(key)

    def __iter__(self):
        self._fill()
        return super().__iter__()

    def __len__(self):
        self._fill()
        return super().__len__()

    def __repr__(self):
        self._fill()
        return super().__repr__()

    def __eq__(self, other):
        self._fill()
        return super().__eq__(other)

    def __ne__(self, other):
        self._fill()
        return super().__ne__(other)

    __hash__ = None

    def __or__(self, other):
        self._fill()
        return dict(super().items()) | other

    def __ror__(self, other):
        self._fill()
        return other | dict(super().items())

    def __delitem__(self, key):
        self._fill()
        super().__delitem__(key)

    def __reduce__(self):
        self._fill()
        return (dict, (dict(super().items()),))

    def get(self, key, default=None):
        self._fill()
        return super().get(key, default)

    def keys(self):
        self._fill()
        return super().keys()

    def values(self):
        self._fill()
        return super().values()

    def items(self):
        self._fill()
        return super().items()

    def copy(self):
        self._fill()
        return dict(super().items())

    def setdefault(self, key, default=None):
        self._fill()
        return super().setdefault(key, default)

    def pop(self, key, *default):
        self._fill()
        return super().pop(key, *default)

    def popitem(self):
        self._fill()
        return super().popitem()
