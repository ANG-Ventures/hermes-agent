"""The pytest temp root must never sit under the live Hermes root (t_f64dfdb6).

get_default_hermes_root() maps any HERMES_HOME under ~/.hermes back to
~/.hermes, so a per-test sandbox home created under the live root sends profile
writes to the REAL profiles/ dir. That leaked profiles/builder-auth, demo, worker
and work into a live home on 2026-09-23, from a run with TMPDIR under
~/.hermes/kanban/workspaces/<task>/tmp.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.conftest import _is_under_real_hermes_root, _real_hermes_root

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEAKY_TEST = (
    "tests/hermes_cli/test_web_server.py::TestNewEndpoints::"
    "test_profiles_create_builder_mcp_auth_is_profile_scoped"
)


def test_containment_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".hermes"
    assert _real_hermes_root() == root.resolve()
    assert _is_under_real_hermes_root(root)
    assert _is_under_real_hermes_root(root / "kanban" / "workspaces" / "t_x" / "tmp")
    assert not _is_under_real_hermes_root(tmp_path / ".hermes-other")
    assert not _is_under_real_hermes_root(tmp_path)


def test_session_sandbox_is_outside_live_root(tmp_path):
    from hermes_constants import get_default_hermes_root

    assert not _is_under_real_hermes_root(tempfile.gettempdir())
    assert not _is_under_real_hermes_root(tmp_path)
    assert get_default_hermes_root().resolve() != _real_hermes_root()


@pytest.mark.skipif(sys.platform == "win32", reason="HOME-based root is POSIX")
def test_tmpdir_under_live_root_does_not_leak_profiles(tmp_path):
    """E2E: rerun the original leaking test with TMPDIR inside a (fake) live root."""
    fake_home = tmp_path / "home"
    leaky_tmp = fake_home / ".hermes" / "kanban" / "workspaces" / "t_x" / "tmp"
    leaky_tmp.mkdir(parents=True)

    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    env.update(HOME=str(fake_home), TMPDIR=str(leaky_tmp), TEMP=str(leaky_tmp), TMP=str(leaky_tmp))

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", LEAKY_TEST],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    leaked = fake_home / ".hermes" / "profiles" / "builder-auth"
    assert not leaked.exists(), f"profile leaked into the live root: {leaked}\n{proc.stdout[-2000:]}"
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
