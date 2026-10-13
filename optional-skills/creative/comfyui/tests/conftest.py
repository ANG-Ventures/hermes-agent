"""Pytest configuration for the comfyui skill test suite.

Adds `scripts/` to sys.path so tests can `from _common import ...`, and
provides a few common fixtures.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"

sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def sd15_workflow() -> dict:
    return json.loads((WORKFLOWS / "sd15_txt2img.json").read_text(encoding="utf-8"))


@pytest.fixture
def flux_workflow() -> dict:
    return json.loads((WORKFLOWS / "flux_dev_txt2img.json").read_text(encoding="utf-8"))


@pytest.fixture
def video_workflow() -> dict:
    return json.loads((WORKFLOWS / "wan_video_t2v.json").read_text(encoding="utf-8"))


@pytest.fixture
def workflows_dir() -> Path:
    return WORKFLOWS


@pytest.fixture
def scripts_dir() -> Path:
    return SCRIPTS


@pytest.fixture
def cloud_key() -> str | None:
    """Cloud API key if set, otherwise None.

    Tests that need cloud connectivity should skip when this is None.
    """
    return os.environ.get("COMFY_CLOUD_API_KEY")


def pytest_collection_modifyitems(config, items):
    """Auto-skip cloud tests when no API key is set."""
    if os.environ.get("COMFY_CLOUD_API_KEY"):
        return
    skip_cloud = pytest.mark.skip(reason="Set COMFY_CLOUD_API_KEY to run cloud tests")
    for item in items:
        if "cloud" in item.keywords:
            item.add_marker(skip_cloud)


# t_6274f5cb (Prism 7f79b8c956d0): this directory carries its own pytest.ini, so a run started
# from it (``cd .../tests && pytest``, ``-c tests/pytest.ini``) makes THIS the config dir, pytest
# stops looking for conftests above it, and the repo-root ``conftest.py`` (the per-test wall-clock
# + RSS + RLIMIT_AS bounds) never loads. Register the harness module as a plugin here: pytest's
# ``pytest_addoption``/``pytest_configure`` are historic hooks, so a plugin registered during
# configure still receives both. Under a repo-root run the harness is already imported by the
# root conftest and this is a no-op (``pytest_plugins`` is not allowed in a non-root conftest).
def pytest_configure(config):  # noqa: D401 — pytest hook
    if any(m in sys.modules for m in ("pytest_bounds", "tests._fixtures.bounds")):
        return
    for parent in Path(__file__).resolve().parents:
        for candidate in (parent / "pytest_bounds.py", parent / "tests" / "_fixtures" / "bounds.py"):
            if candidate.is_file():
                import importlib.util

                spec = importlib.util.spec_from_file_location("hermes_test_bounds", candidate)
                module = importlib.util.module_from_spec(spec)
                sys.modules["hermes_test_bounds"] = module
                spec.loader.exec_module(module)
                config.pluginmanager.register(module, "hermes_test_bounds")
                return
