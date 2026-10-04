"""Independent core/optional dependency and reviewed CVE policies."""
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

# setuptools is declared in the [dev] group and is the build backend, but
# guard the import so a runner without it skips these packaging checks
# instead of erroring out collection for the whole shard (it used to be
# picked up ambiently from the CI image; newer ubuntu-latest images don't
# ship it in the test venv).
find_packages = pytest.importorskip("setuptools", exc_type=ImportError).find_packages

REPO_ROOT = Path(__file__).resolve().parents[1]


def _packages_find_include():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["packages"]["find"]["include"]


def test_every_on_disk_subpackage_is_covered_by_packages_find():
    """Regression test for #34701 (and the bug class behind #34034 / #28149).

    ``[tool.setuptools.packages.find]`` ``include`` is hand-maintained. Every
    top-level package is listed twice — bare (``hermes_cli``) for the package
    itself and ``hermes_cli.*`` for its subpackages — EXCEPT when someone
    forgets the wildcard. v0.15.x listed ``hermes_cli`` without ``hermes_cli.*``,
    so the wheel shipped ``hermes_cli/*.py`` but dropped the ``dashboard_auth``
    and ``proxy`` subpackages. The dashboard then died on every install with
    ``ModuleNotFoundError: No module named 'hermes_cli.dashboard_auth'``.

    This drives setuptools' own discovery against the live tree: every package
    that exists on disk and would be found by a permissive ``<name>.*`` scan
    must also be found by the actual ``include`` list. A subpackage added under
    any listed package without the matching wildcard fails here instead of in a
    user's container.
    """
    include = _packages_find_include()

    # What the real include list actually selects.
    selected = set(find_packages(where=str(REPO_ROOT), include=include))

    # Top-level packages we ship (bare names in the include list, no wildcard).
    top_level = sorted({name for name in include if "." not in name})

    # For each shipped top-level package, every on-disk subpackage must be
    # covered by the include list.
    expected = set(
        find_packages(
            where=str(REPO_ROOT),
            include=[pattern for name in top_level for pattern in (name, f"{name}.*")],
        )
    )

    missing = sorted(expected - selected)
    assert not missing, (
        "These packages exist on disk but are dropped from the wheel because "
        "[tool.setuptools.packages.find] include is missing a wildcard. Add the "
        f"matching '<name>.*' entry in pyproject.toml: {missing}"
    )




def test_test_dependencies_are_group_only_in_manifest_and_lock():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    hermes = next(package for package in lock["package"] if package["name"] == manifest["project"]["name"])
    assert manifest["tool"]["uv"]["default-groups"] == []
    assert "dev" in manifest["dependency-groups"]
    assert "dev" not in manifest["project"]["optional-dependencies"]
    assert "dev" in hermes["dev-dependencies"]
    assert "dev" not in hermes.get("optional-dependencies", {})


def test_core_and_optional_speech_dependencies():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = {Requirement(dep).name for dep in project["dependencies"]}
    assert "packaging" in core  # Runtime code imports it directly, not transitively.
    assert "faster-whisper" not in core
    assert "faster-whisper" in {
        Requirement(dep).name for dep in project["optional-dependencies"]["stt-whisper"]
    }


def test_starlette_server_pins_and_lock_exclude_cve_2026_48710():
    # BadHost's reviewed fixed boundary is independent of today's exact pin.
    floor = Version("1.0.1")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    found = set()
    for extra, specs in metadata["project"]["optional-dependencies"].items():
        for requirement in map(Requirement, specs):
            if requirement.name != "starlette":
                continue
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "==", (extra, requirement)
            assert Version(pins[0].version) >= floor, (extra, requirement)
            found.add(extra)
    assert {"web", "mcp", "computer-use"} <= found
    dev = [req for req in map(Requirement, metadata["dependency-groups"]["dev"])
           if req.name == "starlette"]
    assert len(dev) == 1
    pins = list(dev[0].specifier)
    assert len(pins) == 1 and pins[0].operator == "==" and Version(pins[0].version) >= floor
    versions = [Version(row["version"]) for row in lock["package"] if row["name"] == "starlette"]
    assert versions and all(version >= floor for version in versions)
