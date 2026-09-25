"""Copy-on-write generation seam for the provider registry containers.

Nine module-level containers that describe which providers exist
(``PROVIDER_REGISTRY``, ``_PROVIDER_MODELS``, ``CANONICAL_PROVIDERS``, ...)
are bound AT THEIR DEFINITION SITES to the facade types below. A facade keeps
its ``isinstance`` identity (``GuardedDict`` is a ``dict``, ``GuardedList`` a
``list``) so every existing ``from module import NAME`` reference keeps
working, but:

* every Python-level read is served from the committed :class:`Generation`,
  an immutable per-generation copy (mappingproxy / tuple / frozenset), so a
  reader iterating while another thread registers a provider can never see a
  torn container or hit ``dictionary changed size during iteration``;
* every write is a copy-and-swap of that ONE container under a single
  ``RLock`` (compare-and-swap: the swap re-derives if another writer swapped
  since the build), and
* :func:`publish` swaps several containers in ONE generation reference swap,
  so a multi-surface registration is observable whole or not at all through a
  :func:`snapshot`.

Base storage MIRRORS the generation: after every swap, still under the lock,
the delta is written into the facade's own ``dict``/``list`` storage with
explicit base-class calls. C-level consumers that read base storage directly
(``json.dumps`` without ``indent``, ``x + []``, ``PyDict_Next``) therefore see
the same entries instead of an empty container. The mirror is monotone
additive: removal methods raise ``TypeError`` — nothing in the tree removes a
provider at runtime.

Multi-container readers bind one generation with ``g = snapshot()`` and read
``g.PROVIDER_REGISTRY``, ``g.CANONICAL_PROVIDERS``, ... so every surface they
consult belongs to one committed state. Single-container readers need nothing.

The seam is provider-agnostic: it never imports provider code. Refresh
callbacks (:func:`register_refresh`) let an optional plugin register names
lazily; with none registered :func:`refresh` is a no-op.

Lock order: seam lock -> anything else. The lock is only ever held for the
in-memory swap and mirror write; never across imports, discovery, network,
executor joins or callbacks.
"""

from __future__ import annotations

import collections.abc
import logging
import threading
import types
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "FACADES",
    "Generation",
    "GuardedDict",
    "GuardedList",
    "GuardedSet",
    "SeamCollision",
    "current",
    "publish",
    "refresh",
    "register_refresh",
    "snapshot",
]

REFRESH_REASONS = frozenset({"picker", "typed", "request"})
_COMMITTED = "committed"

_lock = threading.RLock()
FACADES: dict[str, Any] = {}
_OWNERS: dict[str, str] = {}
_KINDS: dict[str, str] = {}

# Test instrumentation only: when set, called with "build" (after a write's
# optimistic build, before the lock) and "swap" (inside the lock, before the
# reference swap). Barrier tests park a publisher here; production leaves it
# None.
_park: Optional[Callable[[str], None]] = None


class SeamCollision(ValueError):
    """A :func:`publish` delta would replace an entry another name owns."""


class Generation:
    """One committed, immutable state of every registered container.

    Containers are reachable as attributes named after their module-level
    name (``g.PROVIDER_REGISTRY``) or by subscription (``g["_REGISTRY"]``).
    ``committed`` maps a lane to the frozenset of names published for it.
    ``providers_list`` is a lazily filled memo slot owned by
    ``providers.list_providers`` (a new generation always starts empty).
    """

    __slots__ = ("_containers", "committed", "providers_list")

    def __init__(self, containers: Mapping[str, Any], committed: Mapping[str, frozenset]):
        object.__setattr__(self, "_containers", types.MappingProxyType(dict(containers)))
        object.__setattr__(self, "committed", types.MappingProxyType(dict(committed)))
        object.__setattr__(self, "providers_list", None)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._containers[name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name: str) -> Any:
        return self._containers[name]

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "providers_list":
            raise AttributeError(f"Generation is frozen ({name})")
        # Memo slot: filled without the seam lock. Two racing fillers compute
        # the same value from this same frozen generation, so last-write-wins
        # is harmless and never crosses generations.
        object.__setattr__(self, name, value)

    def get(self, name: str, default: Any = None) -> Any:
        return self._containers.get(name, default)

    def names(self) -> tuple[str, ...]:
        return tuple(self._containers)

    def __repr__(self) -> str:
        return f"<Generation containers={len(self._containers)} lanes={sorted(self.committed)}>"


_current = Generation({}, {})


def current() -> Generation:
    """Return the committed generation."""
    return _current


_snapshot_hooks: list[Callable[[], None]] = []


def add_snapshot_hook(hook: Callable[[], None]) -> None:
    """Run ``hook`` before every :func:`snapshot`.

    For containers whose facade materializes lazily on first read (the
    plugin auto-extend of ``CANONICAL_PROVIDERS``): a snapshot reads the
    generation directly, so it must latch the same trigger a facade read
    would. A hook must be cheap once latched.
    """
    if hook not in _snapshot_hooks:
        _snapshot_hooks.append(hook)


def snapshot() -> Generation:
    """Pin one generation for a multi-container read (``g = snapshot()``).

    Same object :func:`current` returns, after the lazy-container hooks ran.
    """
    for hook in _snapshot_hooks:
        hook()
    return _current


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def _freeze(kind: str, data: Any) -> Any:
    if kind == "dict":
        return types.MappingProxyType(dict(data))
    if kind == "list":
        return tuple(data)
    return frozenset(data)


class _Plan:
    """A derived next generation plus the base-storage mirror operations."""

    __slots__ = ("generation", "mirror")

    def __init__(self, generation: Generation, mirror: list):
        self.generation = generation
        self.mirror = mirror  # [(name, kind, payload)]


def _transact(build: Callable[[Generation], Optional[_Plan]]) -> Generation:
    """Optimistic build, then compare-and-swap under the seam lock.

    ``build`` derives the next generation from a base; it must be pure (it
    may run twice). If another writer swapped since the optimistic build, the
    plan is re-derived from the fresh base inside the lock, so no update is
    lost.
    """
    global _current
    base = _current
    plan = build(base)
    if _park is not None:
        _park("build")
    with _lock:
        if _current is not base:
            base = _current
            plan = build(base)
        if plan is None:
            return base
        if _park is not None:
            _park("swap")
        _current = plan.generation
        for name, kind, payload in plan.mirror:
            facade = FACADES.get(name)
            if facade is None:
                continue
            if kind == "dict":
                for key, value in payload:
                    dict.__setitem__(facade, key, value)
            elif kind == "list":
                for entry in payload:
                    list.append(facade, entry)
        return _current


def _with(base: Generation, updates: Mapping[str, Any], committed: Optional[Mapping] = None) -> Generation:
    containers = dict(base._containers)
    containers.update(updates)
    return Generation(containers, base.committed if committed is None else committed)


def _write_one(name: str, derive: Callable[[Any], tuple[Any, Any]]) -> Generation:
    """Copy-and-swap one container. ``derive(frozen) -> (new_frozen, payload)``."""
    kind = _KINDS[name]

    def build(base: Generation) -> Optional[_Plan]:
        new, payload = derive(base[name])
        if new is None:
            return None
        return _Plan(_with(base, {name: new}), [(name, kind, payload)])

    return _transact(build)


def _register(facade: Any, owner: str, name: str, kind: str, data: Any) -> None:
    """Bind ``facade`` as the container ``name`` and seed its generation entry."""
    global _current
    frozen = _freeze(kind, data)
    with _lock:
        FACADES[name] = facade
        _OWNERS[name] = owner
        _KINDS[name] = kind
        _current = _with(_current, {name: frozen})


def owner_of(name: str) -> str:
    """Module name that defines the container ``name``."""
    return _OWNERS[name]


def publish(delta: Mapping[str, Any]) -> Generation:
    """Publish additions to several containers in ONE generation swap.

    ``delta`` maps a container name to the entries to add: a mapping for a
    dict container (insert; an existing key with an EQUAL value is a no-op,
    with a different value it is a :class:`SeamCollision`), a sequence for a
    list container (entries not already present are appended), an iterable
    for a set container (union). The reserved key ``"committed"`` maps a lane
    to names committed for it; if every such name is already committed the
    call is an idempotent no-op returning the current generation.

    Returns the generation that holds the delta. Nothing is swapped if any
    part of the delta is rejected.
    """
    unknown = [n for n in delta if n != _COMMITTED and n not in _KINDS]
    if unknown:
        raise KeyError(f"unknown seam container(s): {sorted(unknown)}")

    def build(base: Generation) -> Optional[_Plan]:
        committed_delta = delta.get(_COMMITTED) or {}
        if committed_delta and all(
            frozenset(names) <= base.committed.get(lane, frozenset())
            for lane, names in committed_delta.items()
        ):
            return None
        updates: dict[str, Any] = {}
        mirror: list = []
        for name, entries in delta.items():
            if name == _COMMITTED:
                continue
            kind = _KINDS[name]
            cur = base[name]
            if kind == "dict":
                added = []
                for key, value in dict(entries).items():
                    if key in cur:
                        if cur[key] == value:
                            continue
                        raise SeamCollision(f"{name}[{key!r}] is already owned by another entry")
                    added.append((key, value))
                if added:
                    merged = dict(cur)
                    merged.update(added)
                    updates[name] = types.MappingProxyType(merged)
                    mirror.append((name, kind, added))
            elif kind == "list":
                added = []
                for entry in entries:
                    if entry not in cur and entry not in added:
                        added.append(entry)
                if added:
                    updates[name] = cur + tuple(added)
                    mirror.append((name, kind, added))
            else:
                extra = frozenset(entries) - cur
                if extra:
                    updates[name] = cur | extra
                    mirror.append((name, kind, ()))
        committed = None
        if committed_delta:
            committed = dict(base.committed)
            for lane, names in committed_delta.items():
                committed[lane] = committed.get(lane, frozenset()) | frozenset(names)
        if not updates and committed is None:
            return None
        return _Plan(_with(base, updates, committed), mirror)

    return _transact(build)


def _restore(gen: Generation) -> None:
    """Swap back to a saved generation and rewrite every mirror to match.

    Test-isolation support only: the one non-additive operation, used by
    fixtures that must undo registrations between tests.
    """
    global _current
    with _lock:
        _current = gen
        for name, facade in FACADES.items():
            try:
                data = gen[name]
            except KeyError:
                continue
            kind = _KINDS[name]
            if kind == "dict":
                dict.clear(facade)
                dict.update(facade, data)
            elif kind == "list":
                list.clear(facade)
                list.extend(facade, data)


# ---------------------------------------------------------------------------
# Facades
# ---------------------------------------------------------------------------

def _additive(*_a, **_k):
    raise TypeError("additive container: provider registry entries cannot be removed or reordered")


class GuardedDict(dict):
    """``dict`` facade over one generation container (mirror storage)."""

    __slots__ = ("_seam_name",)

    def __init__(self, owner: str, name: str, data: Any = ()):
        dict.__init__(self)
        seed = dict(data)
        dict.update(self, seed)
        self._seam_name = name
        _register(self, owner, name, "dict", seed)

    def _data(self) -> Mapping:
        return _current[self._seam_name]

    # -- reads ------------------------------------------------------------
    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())

    def __contains__(self, key):
        return key in self._data()

    def get(self, key, default=None):
        return self._data().get(key, default)

    def keys(self):
        return self._data().keys()

    def values(self):
        return self._data().values()

    def items(self):
        return self._data().items()

    def copy(self):
        return dict(self._data())

    def __eq__(self, other):
        return dict(self._data()) == other

    def __ne__(self, other):
        return dict(self._data()) != other

    def __or__(self, other):
        return dict(self._data()) | other

    def __ror__(self, other):
        return dict(other) | dict(self._data())

    def __repr__(self):
        return repr(dict(self._data()))

    def __reduce_ex__(self, protocol):
        return (dict, (dict(self._data()),))

    def __copy__(self):
        cls, args = self.__reduce_ex__(4)[:2]
        return cls(*args)

    def __deepcopy__(self, memo):
        import copy

        cls, args = self.__reduce_ex__(4)[:2]
        return copy.deepcopy(cls(*args), memo)

    __hash__ = None  # type: ignore[assignment]

    # -- writes (copy-and-swap of this one container) -----------------------
    def __setitem__(self, key, value):
        def derive(cur):
            merged = dict(cur)
            merged[key] = value
            return types.MappingProxyType(merged), [(key, value)]

        _write_one(self._seam_name, derive)

    def setdefault(self, key, default=None):
        def derive(cur):
            if key in cur:
                return None, None
            merged = dict(cur)
            merged[key] = default
            return types.MappingProxyType(merged), [(key, default)]

        return _write_one(self._seam_name, derive)[self._seam_name][key]

    def update(self, *args, **kwargs):
        pairs = list(dict(*args, **kwargs).items())
        if not pairs:
            return

        def derive(cur):
            merged = dict(cur)
            merged.update(pairs)
            return types.MappingProxyType(merged), pairs

        _write_one(self._seam_name, derive)

    def __ior__(self, other):
        self.update(other)
        return self

    __delitem__ = pop = popitem = clear = _additive


class GuardedList(list):
    """``list`` facade over one generation container (mirror storage)."""

    __slots__ = ("_seam_name",)

    def __init__(self, owner: str, name: str, data: Any = ()):
        list.__init__(self)
        seed = list(data)
        list.extend(self, seed)
        self._seam_name = name
        _register(self, owner, name, "list", seed)

    def _data(self) -> tuple:
        return _current[self._seam_name]

    # -- reads ------------------------------------------------------------
    def __getitem__(self, item):
        value = self._data()[item]
        return list(value) if isinstance(item, slice) else value

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())

    def __contains__(self, item):
        return item in self._data()

    def __reversed__(self):
        return reversed(self._data())

    def index(self, *args):
        return self._data().index(*args)

    def count(self, item):
        return self._data().count(item)

    def copy(self):
        return list(self._data())

    def __eq__(self, other):
        return list(self._data()) == other

    def __ne__(self, other):
        return list(self._data()) != other

    def __add__(self, other):
        return list(self._data()) + other

    def __mul__(self, n):
        return list(self._data()) * n

    __rmul__ = __mul__

    def __repr__(self):
        return repr(list(self._data()))

    def __reduce_ex__(self, protocol):
        return (list, (list(self._data()),))

    def __copy__(self):
        cls, args = self.__reduce_ex__(4)[:2]
        return cls(*args)

    def __deepcopy__(self, memo):
        import copy

        cls, args = self.__reduce_ex__(4)[:2]
        return copy.deepcopy(cls(*args), memo)

    __hash__ = None  # type: ignore[assignment]

    # -- writes -------------------------------------------------------------
    def append(self, entry):
        self.extend((entry,))

    def extend(self, entries):
        added = tuple(entries)
        if not added:
            return

        def derive(cur):
            return cur + added, added

        _write_one(self._seam_name, derive)

    def __iadd__(self, entries):
        self.extend(entries)
        return self

    __setitem__ = __delitem__ = insert = pop = remove = clear = _additive
    sort = reverse = __imul__ = _additive


class GuardedSet(collections.abc.Set):
    """Read-only-``Set`` facade with an additive ``add``.

    Deliberately NOT a ``set`` subclass: ``set(x)`` / ``s | x`` on a real set
    subclass copy its hash table directly and skip ``__iter__``. It has no
    base storage, so it has nothing to mirror.
    """

    __slots__ = ("_seam_name",)

    def __init__(self, owner: str, name: str, data: Any = ()):
        self._seam_name = name
        _register(self, owner, name, "set", data)

    def _data(self) -> frozenset:
        return _current[self._seam_name]

    def __contains__(self, item) -> bool:
        return item in self._data()

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def __repr__(self) -> str:
        return repr(set(self._data()))

    def copy(self) -> set:
        return set(self._data())

    def __reduce_ex__(self, protocol):
        return (set, (set(self._data()),))

    def __copy__(self):
        cls, args = self.__reduce_ex__(4)[:2]
        return cls(*args)

    def __deepcopy__(self, memo):
        import copy

        cls, args = self.__reduce_ex__(4)[:2]
        return copy.deepcopy(cls(*args), memo)

    def add(self, item) -> None:
        def derive(cur):
            if item in cur:
                return None, None
            return cur | {item}, ()

        _write_one(self._seam_name, derive)

    discard = remove = pop = clear = _additive


# ---------------------------------------------------------------------------
# Refresh callbacks
# ---------------------------------------------------------------------------

_refresh_callbacks: list[Callable[[str, Optional[str]], None]] = []
_refresh_state = threading.local()


def register_refresh(cb: Callable[[str, Optional[str]], None]) -> None:
    """Register a refresh callback ``cb(reason, name)``; idempotent per callable."""
    with _lock:
        if cb not in _refresh_callbacks:
            _refresh_callbacks.append(cb)


def refresh(reason: str, name: Optional[str] = None) -> None:
    """Give registered callbacks a chance to publish names before a lookup.

    ``reason`` is ``picker`` (full scan), ``typed`` or ``request`` (``name``
    is the requested provider, or None). Same-thread re-entry — a callback
    whose own work reaches a refresh trigger — is a no-op. A callback failure
    is logged and never propagates into the caller's lookup.
    """
    if reason not in REFRESH_REASONS:
        raise ValueError(f"unknown refresh reason: {reason!r}")
    if not _refresh_callbacks or getattr(_refresh_state, "depth", 0):
        return
    _refresh_state.depth = 1
    try:
        for cb in tuple(_refresh_callbacks):
            try:
                cb(reason, name)
            except Exception:
                logger.warning("provider refresh callback failed (reason=%s)", reason, exc_info=True)
    finally:
        _refresh_state.depth = 0
