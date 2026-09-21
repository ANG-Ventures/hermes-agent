"""Provider auto-extend must be LAZY, never at ``hermes_cli.models`` import.

Regression for the circular-import class: ``hermes_cli/models.py`` (and
``agent/model_metadata.py``) used to call ``providers.list_providers()`` in
their module bodies. ``list_providers()`` imports every model-provider plugin,
and plugins routinely do ``from hermes_cli.models import _PROVIDER_MODELS,
CANONICAL_PROVIDERS`` at their own import time — so discovery ran against a
partially initialised ``hermes_cli.models``:

* models-first import order  -> plugin raises ``ImportError: cannot import name
  'CANONICAL_PROVIDERS' from partially initialized module``, and
  ``_PROVIDER_MODELS`` was empty for every pin.
* discovery-first order      -> the plugin's own provider never reached
  ``CANONICAL_PROVIDERS``.

Both orders are exercised here against a real temp ``HERMES_HOME`` plugin root,
in a subprocess (import order is a process-global property, so each case needs
a fresh interpreter).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PLUGIN_NAME = "zz-lazy-canonical-probe"

# The plugin does exactly what every claude-apx-N / claude-bpx-N clone does:
# imports the two models.py surfaces at its own import time, then registers.
PLUGIN_INIT = textwrap.dedent(
    """
    import builtins

    _status = {}
    try:
        from hermes_cli.models import _PROVIDER_MODELS, CANONICAL_PROVIDERS
        _status["import_ok"] = True
        _status["n_provider_models"] = len(_PROVIDER_MODELS)
    except Exception as exc:
        _status["import_ok"] = False
        _status["error"] = "%s: %s" % (type(exc).__name__, exc)
    builtins._LAZY_CANONICAL_PROBE_STATUS = _status

    from providers import register_provider
    from providers.base import ProviderProfile

    register_provider(ProviderProfile(
        name="zz-lazy-canonical-probe",
        display_name="Lazy Canonical Probe",
        description="Lazy Canonical Probe (test fixture)",
        base_url="https://probe.invalid/v1",
        env_vars=("ZZ_LAZY_CANONICAL_PROBE_KEY",),
    ))
    """
)

DRIVER = textwrap.dedent(
    """
    import builtins, json, sys

    order = sys.argv[1]
    out = {"order": order}
    if order == "models-first":
        import hermes_cli.models as models
        import providers
        providers.list_providers()
    else:
        import providers
        providers.list_providers()
        import hermes_cli.models as models

    out["plugin"] = getattr(
        builtins, "_LAZY_CANONICAL_PROBE_STATUS",
        {"import_ok": None, "error": "plugin was never imported"},
    )
    out["registry_has_probe"] = any(
        p.name == "zz-lazy-canonical-probe" for p in __import__("providers").list_providers()
    )
    slugs = [p.slug for p in models.CANONICAL_PROVIDERS]
    out["probe_in_canonical"] = "zz-lazy-canonical-probe" in slugs
    out["n_canonical"] = len(slugs)
    out["label"] = models._PROVIDER_LABELS.get("zz-lazy-canonical-probe")
    out["in_known_names"] = "zz-lazy-canonical-probe" in models._KNOWN_PROVIDER_NAMES

    # Picker + /model label resolution must still work and must still contain
    # the bundled canonical providers (no behaviour change for existing rows).
    from hermes_cli.model_switch import list_picker_providers  # noqa: F401
    out["picker_ok"] = True
    out["nous_label"] = models._PROVIDER_LABELS.get("nous")
    out["custom_label"] = models._PROVIDER_LABELS.get("custom")

    print("PROBE_JSON " + json.dumps(out))
    """
)


def _run_order(tmp_path: Path, order: str) -> dict:
    home = tmp_path / "hermes_home"
    plugin_dir = home / "plugins" / "model-providers" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(PLUGIN_INIT)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {PLUGIN_NAME}\nkind: model-provider\nversion: 0.0.1\n"
        "description: lazy-canonical regression fixture\n"
    )

    driver = tmp_path / f"driver_{order}.py"
    driver.write_text(DRIVER)

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, str(driver), order],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_JSON ")]
    assert lines, (
        f"driver produced no verdict (rc={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-4000:]}"
    )
    return json.loads(lines[-1][len("PROBE_JSON "):])


@pytest.mark.parametrize("order", ["models-first", "discovery-first"])
def test_plugin_importing_models_surfaces_does_not_hit_partial_module(tmp_path, order):
    """A provider plugin may import _PROVIDER_MODELS/CANONICAL_PROVIDERS itself."""
    result = _run_order(tmp_path, order)
    plugin = result["plugin"]
    assert plugin["import_ok"] is True, (
        f"[{order}] plugin failed to import models surfaces: {plugin.get('error')}"
    )
    # The bug also emptied _PROVIDER_MODELS for every pin under models-first.
    assert plugin["n_provider_models"] > 0, (
        f"[{order}] plugin saw an EMPTY _PROVIDER_MODELS "
        f"({plugin['n_provider_models']} entries)"
    )


@pytest.mark.parametrize("order", ["models-first", "discovery-first"])
def test_plugin_provider_reaches_canonical_providers(tmp_path, order):
    """Auto-extend still lands the plugin's provider, under either order."""
    result = _run_order(tmp_path, order)
    assert result["registry_has_probe"] is True, (
        f"[{order}] fixture never registered — test is vacuous"
    )
    assert result["probe_in_canonical"] is True, (
        f"[{order}] provider missing from CANONICAL_PROVIDERS "
        f"(tail={result['n_canonical']} entries)"
    )
    assert result["label"] == "Lazy Canonical Probe", (
        f"[{order}] _PROVIDER_LABELS not extended: {result['label']!r}"
    )
    assert result["in_known_names"] is True, (
        f"[{order}] _KNOWN_PROVIDER_NAMES not extended"
    )


@pytest.mark.parametrize("order", ["models-first", "discovery-first"])
def test_picker_and_label_resolution_unchanged(tmp_path, order):
    """No behaviour change for the picker or existing /model label resolution."""
    result = _run_order(tmp_path, order)
    assert result["picker_ok"] is True
    assert result["nous_label"] == "Nous Portal", (
        f"[{order}] canonical label regressed: {result['nous_label']!r}"
    )
    assert result["custom_label"] == "Custom endpoint", (
        f"[{order}] 'custom' special case regressed: {result['custom_label']!r}"
    )
    # The bundled canonical list is ~50 rows; a collapse to near-zero means the
    # lazy wrapper served an unextended/empty list.
    assert result["n_canonical"] > 40, (
        f"[{order}] CANONICAL_PROVIDERS collapsed to {result['n_canonical']} entries"
    )


def test_models_import_does_not_trigger_provider_discovery(tmp_path):
    """The load-bearing invariant: importing models must not import plugins.

    This is what actually distinguishes the fix from the bug — everything else
    could in principle be satisfied by a different import order.
    """
    home = tmp_path / "hermes_home"
    plugin_dir = home / "plugins" / "model-providers" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    # A plugin that writes a marker file the moment it is imported.
    marker = tmp_path / "plugin_was_imported"
    (plugin_dir / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n"
    )
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {PLUGIN_NAME}\nkind: model-provider\nversion: 0.0.1\n"
    )

    driver = tmp_path / "import_only.py"
    driver.write_text("import hermes_cli.models  # noqa: F401\nprint('IMPORTED')\n")

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "IMPORTED" in proc.stdout, (
        f"import failed (rc={proc.returncode})\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    assert not marker.exists(), (
        "importing hermes_cli.models ran provider plugin discovery — the "
        "auto-extend is not lazy"
    )
