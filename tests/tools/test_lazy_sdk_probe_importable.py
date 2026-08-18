"""An already-importable SDK must not be declared unavailable.

``lazy_deps.ensure()`` decides "missing" from installed *distribution
metadata* measured against the pinned version. That is the right question
for **installing**, and the wrong question for **using**: a host that can
already ``import modal`` — because it carries an off-pin version, or
because a test injected a fake into ``sys.modules`` — is perfectly able to
run the backend. Yet with installs gated off
(``security.allow_lazy_installs=false``, or a sealed venv) ``ensure()``
raises ``FeatureUnavailable`` there, and the terminal backends used to
escalate that refusal into a hard ``ImportError``.

The backends must probe the only question that actually gates them: can the
SDK be imported? These tests cover the shared helper and every backend that
uses it.
"""

from __future__ import annotations

import sys
import types

import pytest

import tools.lazy_deps as ld


@pytest.fixture()
def installs_refused(monkeypatch):
    """No distribution satisfies the pin, and lazy installs are gated off."""
    monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
    monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
    monkeypatch.setattr(
        ld, "_venv_pip_install",
        lambda *a, **kw: pytest.fail("no install may be attempted"),
    )


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------


class TestEnsureImportable:
    def test_importable_module_survives_a_refused_install(
        self, monkeypatch, installs_refused
    ):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.probe", ("zzzfake==1.0",))
        monkeypatch.setitem(sys.modules, "zzzfake", types.ModuleType("zzzfake"))

        ld.ensure_importable("test.probe", "zzzfake")  # must not raise

    def test_unimportable_module_still_raises_importerror(
        self, monkeypatch, installs_refused
    ):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.probe", ("zzzfake==1.0",))
        monkeypatch.delitem(sys.modules, "zzzfake", raising=False)

        with pytest.raises(ImportError, match="lazy installs disabled"):
            ld.ensure_importable("test.probe", "zzzfake")

    def test_satisfied_feature_never_probes_the_import(self, monkeypatch):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.probe", ("zzzfake==1.0",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        monkeypatch.delitem(sys.modules, "zzzfake", raising=False)

        # Satisfied means ensure() returns before any probe, so the absent
        # module must not turn into an error.
        ld.ensure_importable("test.probe", "zzzfake")

    def test_missing_lazy_deps_feature_key_is_still_gated_by_importability(
        self, monkeypatch, installs_refused
    ):
        # An unknown feature raises FeatureUnavailable before any install
        # logic; the importable SDK still wins.
        monkeypatch.setitem(sys.modules, "zzzfake", types.ModuleType("zzzfake"))
        ld.ensure_importable("test.not.a.registered.feature", "zzzfake")


# ---------------------------------------------------------------------------
# Every terminal backend that probes an SDK
# ---------------------------------------------------------------------------


def _fake_modal(monkeypatch):
    monkeypatch.setitem(sys.modules, "modal", types.ModuleType("modal"))


def _fake_vercel(monkeypatch):
    vercel_mod = types.ModuleType("vercel")
    sandbox_mod = types.ModuleType("vercel.sandbox")
    vercel_mod.sandbox = sandbox_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vercel", vercel_mod)
    monkeypatch.setitem(sys.modules, "vercel.sandbox", sandbox_mod)


def _fake_daytona(monkeypatch):
    monkeypatch.setitem(sys.modules, "daytona", types.ModuleType("daytona"))


BACKENDS = [
    ("tools.environments.modal", "_ensure_modal_sdk", _fake_modal),
    ("tools.environments.vercel_sandbox", "_ensure_vercel_sdk", _fake_vercel),
    ("tools.environments.daytona", "_ensure_daytona_sdk", _fake_daytona),
]


@pytest.mark.parametrize(
    "module_name,probe_name,install_fake",
    BACKENDS,
    ids=[b[0].rsplit(".", 1)[-1] for b in BACKENDS],
)
class TestBackendSdkProbes:
    def test_probe_accepts_an_importable_sdk(
        self, monkeypatch, installs_refused, module_name, probe_name, install_fake
    ):
        import importlib

        install_fake(monkeypatch)
        module = importlib.import_module(module_name)
        probe = getattr(module, probe_name)

        probe()  # must not raise — the SDK is right there

    def test_probe_rejects_a_genuinely_absent_sdk(
        self, monkeypatch, installs_refused, module_name, probe_name, install_fake
    ):
        import importlib

        module = importlib.import_module(module_name)
        probe = getattr(module, probe_name)

        # Poison every name the probe could import so the fallback cannot
        # find a real installation on the host running this suite.
        for name in ("modal", "vercel", "vercel.sandbox", "daytona"):
            monkeypatch.setitem(sys.modules, name, None)

        with pytest.raises(ImportError):
            probe()
