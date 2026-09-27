"""Fork canary: provider-registry generation seam (``hermes_cli/provider_seam.py``, #1072).

The seam exists only in this fork. A parity merge can drop it with no error of its own:
``provider_seam.py`` is a fork-only file, but each binding and each refresh trigger is ONE line
inside a contended upstream file (``models.py``, ``auth.py``, ``providers/__init__.py``,
``model_switch.py``, ``runtime_provider.py``). If a conflict in one of those files is resolved
toward upstream, the line goes and nothing else fails. The container goes back to a plain
``dict``/``list``, which tears under concurrent registration, or the pin-factory plugin's hot
registration stops firing on that path.

``tests/test_provider_registry_seam.py`` covers the seam contract. This file adds the one check
that file does not make: the three most-contended refresh call sites (``switch_model``,
``resolve_runtime_provider``, ``list_authenticated_providers``) each run the trigger BEFORE they
do any other work.

The callback raises ``_Stop``, which is a ``BaseException``. ``refresh()`` swallows only
``Exception``, so ``_Stop`` propagates and aborts the call right at the trigger. No network, no
credential resolution, no model catalog runs.
"""

import sys

import pytest

import providers  # noqa: F401  (binds _REGISTRY/_ALIASES)
import hermes_cli.auth  # noqa: F401  (binds PROVIDER_REGISTRY)
import hermes_cli.models  # noqa: F401  (binds the five catalog containers)
import hermes_cli.providers  # noqa: F401  (binds HERMES_OVERLAYS)
from hermes_cli import provider_seam


class _Stop(BaseException):
    pass


EXPECTED_BINDINGS = {
    "_REGISTRY": "providers",
    "_ALIASES": "providers",
    "PROVIDER_REGISTRY": "hermes_cli.auth",
    "HERMES_OVERLAYS": "hermes_cli.providers",
    "_PROVIDER_MODELS": "hermes_cli.models",
    "CANONICAL_PROVIDERS": "hermes_cli.models",
    "_PROVIDER_LABELS": "hermes_cli.models",
    "_PROVIDER_ALIASES": "hermes_cli.models",
    "_KNOWN_PROVIDER_NAMES": "hermes_cli.models",
}


def test_every_registry_container_is_still_its_seam_facade():
    # RED if a merge rewrites e.g. ``PROVIDER_REGISTRY = _LazyProviderRegistry(__name__, ...)`` in
    # hermes_cli/auth.py back to a plain ``{...}`` literal.
    for name, owner in EXPECTED_BINDINGS.items():
        facade = provider_seam.FACADES.get(name)
        assert facade is not None, f"{owner}.{name} is no longer registered with provider_seam"
        assert getattr(sys.modules[owner], name) is facade, f"{owner}.{name} is not its seam facade"


@pytest.fixture
def stop_at_refresh(monkeypatch):
    seen: list = []

    def cb(reason, name):
        seen.append((reason, name))
        raise _Stop

    monkeypatch.setattr(provider_seam, "_refresh_callbacks", [cb])
    return seen


def test_switch_model_refreshes_the_typed_provider_first(stop_at_refresh):
    # RED if a merge drops the ``provider_seam.refresh("typed", ...)`` line near the top of
    # hermes_cli/model_switch.py::switch_model.
    from hermes_cli.model_switch import switch_model

    with pytest.raises(_Stop):
        switch_model("seamcanary:some-model", "openrouter", "x", probe_catalog=False)
    assert stop_at_refresh == [("typed", "seamcanary")]


def test_resolve_runtime_provider_refreshes_the_requested_provider_first(stop_at_refresh):
    # RED if a merge drops ``provider_seam.refresh("request", ...)`` at the top of
    # hermes_cli/runtime_provider.py::resolve_runtime_provider.
    from hermes_cli.runtime_provider import resolve_runtime_provider

    with pytest.raises(_Stop):
        resolve_runtime_provider(requested="seamcanary")
    assert stop_at_refresh == [("request", "seamcanary")]


def test_list_authenticated_providers_refreshes_before_listing(stop_at_refresh):
    # RED if a merge drops ``provider_seam.refresh("picker")`` from
    # hermes_cli/model_switch.py::list_authenticated_providers.
    from hermes_cli.model_switch import list_authenticated_providers

    with pytest.raises(_Stop):
        list_authenticated_providers()
    assert stop_at_refresh == [("picker", None)]
