"""The conftest's version-cache restore puts back exactly the pre-test value, ``None`` included."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from tests import conftest as suite_conftest

_MOD = "hermes_cli.version_info"


def _drive(mutate):
    gen = suite_conftest._restore_version_info_cache.__wrapped__()
    next(gen)
    mutate()
    next(gen, None)


def test_none_cache_populated_during_test_is_cleared(monkeypatch):
    fake = SimpleNamespace(_cached_version_info=None)
    monkeypatch.setitem(sys.modules, _MOD, fake)
    _drive(lambda: setattr(fake, "_cached_version_info", "sandbox-identity"))
    assert fake._cached_version_info is None


def test_module_first_imported_during_test_is_cleared(monkeypatch):
    # setitem first so monkeypatch records (and restores) the real entry, then remove it.
    monkeypatch.setitem(sys.modules, _MOD, None)
    monkeypatch.delitem(sys.modules, _MOD)
    fake = SimpleNamespace(_cached_version_info=None)

    def _import_and_resolve():
        sys.modules[_MOD] = fake
        fake._cached_version_info = "sandbox-identity"

    _drive(_import_and_resolve)
    assert fake._cached_version_info is None


def test_populated_cache_is_put_back(monkeypatch):
    fake = SimpleNamespace(_cached_version_info="pre-test")
    monkeypatch.setitem(sys.modules, _MOD, fake)
    _drive(lambda: setattr(fake, "_cached_version_info", "sandbox-identity"))
    assert fake._cached_version_info == "pre-test"
