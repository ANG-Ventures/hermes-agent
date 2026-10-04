"""Behavioral regression coverage for the wheel/sdist distribution guard."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _build_artifact(kind: str, tmp_path, *, nix_build: bool) -> subprocess.CompletedProcess[str]:
    """Invoke the real PEP 517 hook (build_sdist / build_wheel) as a subprocess.

    The wheel and sdist guards live in SEPARATE cmdclass entries in setup.py
    (the bdist_wheel one behind a try/except ImportError), so each hook needs
    its own regression coverage — a passing sdist test proves nothing about
    the wheel path.
    """
    env = os.environ.copy()
    # nix develop exports this too, so it must not grant permission to build
    # a distributable artifact.
    env["NIX_BUILD_TOP"] = "/build/devshell"
    if nix_build:
        env["HERMES_NIX_BUILD"] = "1"
    else:
        env.pop("HERMES_NIX_BUILD", None)
    # Redirect setuptools' scratch dirs (build/, *.egg-info) into tmp_path so
    # the allowed-marker build doesn't litter the real worktree.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    extra_cfg = tmp_path / "dist-extra.cfg"
    extra_cfg.write_text(
        f"[build]\nbuild_base = {scratch / 'build'}\n\n[egg_info]\negg_base = {scratch}\n",
        encoding="utf-8",
    )
    env["DIST_EXTRA_CONFIG"] = str(extra_cfg)
    # build_sdist writes its release tree (hermes_agent-<ver>/, a full copy of
    # the source) into the CWD and deletes it afterwards. With cwd=PROJECT_ROOT
    # every repo-wide rglob("*.py") guard running in a parallel worker saw that
    # tree appear and vanish mid-scan (FileNotFoundError, main CI d5032fe98).
    # Build from a tmp root that symlinks the checkout's top-level entries, so
    # the release tree lands under tmp_path and never inside the repo.
    src = tmp_path / "src"
    src.mkdir()
    for entry in PROJECT_ROOT.iterdir():
        name = entry.name
        if name in ("build", "dist") or name.endswith(".egg-info") or name.startswith("hermes_agent-"):
            continue
        (src / name).symlink_to(entry, target_is_directory=entry.is_dir())
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_{kind}; build_{kind}(r'{out}')".format(
                kind=kind, out=tmp_path
            ),
        ],
        cwd=src,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_artifact_build_rejects_nix_development_shell_environment(kind, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=False)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr
