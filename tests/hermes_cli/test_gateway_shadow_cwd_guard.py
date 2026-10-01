"""Gateway boot refuses a cwd / sys.path[0] that shadows the install (t_4853212d)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_cli.gateway as gateway_cli

REPO_ROOT = Path(gateway_cli.__file__).resolve().parent.parent


def _reason(path, tmp_path, forbidden=None):
    install = tmp_path / "install"
    (install / "hermes_cli").mkdir(parents=True, exist_ok=True)
    return gateway_cli._shadow_cwd_reason(
        path,
        package_root=install,
        forbidden_roots=forbidden if forbidden is not None else [tmp_path / "fleet" / "kanban" / "workspaces"],
    )


def test_predicate_allows_install_root_and_plain_dirs(tmp_path):
    plain = tmp_path / "profiles" / "apollo"
    plain.mkdir(parents=True)
    assert _reason(tmp_path / "install", tmp_path) is None
    assert _reason(plain, tmp_path) is None


def test_predicate_refuses_kanban_workspace(tmp_path):
    ws = tmp_path / "fleet" / "kanban" / "workspaces" / "t_abc"
    ws.mkdir(parents=True)
    reason = _reason(ws, tmp_path)
    assert reason is not None and "kanban" in reason


def test_predicate_refuses_foreign_checkout(tmp_path):
    checkout = tmp_path / "some-worktree"
    (checkout / "hermes_cli").mkdir(parents=True)
    reason = _reason(checkout, tmp_path)
    assert reason is not None and "hermes_cli/" in reason


def test_default_forbidden_roots_include_fleet_scratch_and_workspaces():
    roots = {str(p) for p in gateway_cli._shadow_cwd_forbidden_roots()}
    assert "/Volumes/fleet-scratch" in roots
    assert any(r.endswith(os.path.join("kanban", "workspaces")) for r in roots)


def test_shadow_import_refuses_cwd_loaded_tree_with_foreign_env(tmp_path):
    checkout = tmp_path / "wt"
    (checkout / "hermes_cli").mkdir(parents=True)
    runtime_venv = tmp_path / "runtime" / "venv"
    runtime_venv.mkdir(parents=True)
    reason = gateway_cli._shadow_import_reason(
        str(checkout), package_root=checkout, prefix=str(runtime_venv)
    )
    assert reason is not None and "imported code tree" in reason


def test_shadow_import_allows_own_venv_and_other_cwd(tmp_path):
    checkout = tmp_path / "wt"
    (checkout / "venv").mkdir(parents=True)
    profile = tmp_path / "profiles" / "apollo"
    profile.mkdir(parents=True)
    # checkout running its own venv (CI / dev clone)
    assert gateway_cli._shadow_import_reason(
        str(checkout), package_root=checkout, prefix=str(checkout / "venv")
    ) is None
    # service cwd is a profile dir, code is the install
    assert gateway_cli._shadow_import_reason(
        str(profile), package_root=checkout, prefix=str(tmp_path / "elsewhere")
    ) is None


def test_guard_exits_nonzero_not_75(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_ALLOW_SHADOW_CWD", raising=False)
    checkout = tmp_path / "wt"
    (checkout / "hermes_cli").mkdir(parents=True)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(gateway_cli.sys, "path", [str(checkout)] + sys.path[1:])
    with pytest.raises(SystemExit) as exc:
        gateway_cli._guard_shadow_cwd()
    assert exc.value.code not in (0, 75, None)


def test_guard_escape_hatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ALLOW_SHADOW_CWD", "1")
    checkout = tmp_path / "wt"
    (checkout / "hermes_cli").mkdir(parents=True)
    monkeypatch.chdir(checkout)
    gateway_cli._guard_shadow_cwd()  # no SystemExit


def test_gateway_run_subprocess_refuses_shadow_cwd(tmp_path):
    shadow = tmp_path / "kanban-ws"
    (shadow / "hermes_cli").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "HERMES_ALLOW_SHADOW_CWD"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        # The launchd shape: `python -m hermes_cli.main gateway run`, so
        # sys.path[0] is the cwd. The empty hermes_cli/ is a namespace dir;
        # the real package on PYTHONPATH still wins the import.
        [sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
        cwd=shadow,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == gateway_cli.GATEWAY_SHADOW_CWD_EXIT_CODE, out
    assert proc.returncode not in (0, 75)
    assert "Refusing to start the gateway" in out
    # sys.path[0] may be the repo root (itself refused when the checkout is
    # under /Volumes/fleet-scratch); the cwd line is reported either way, with
    # the forbidden-root or foreign-hermes_cli/ reason depending on tmp_path.
    assert f"cwd {shadow.resolve()} " in out


@pytest.mark.skipif(
    Path(sys.prefix).resolve() == REPO_ROOT or REPO_ROOT in Path(sys.prefix).resolve().parents,
    reason="interpreter env lives inside this checkout (CI .venv): not a shadow import",
)
def test_gateway_run_subprocess_refuses_real_shadow_package():
    """A real checkout in the cwd is what gets imported (Prism r1 P1)."""
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_ALLOW_SHADOW_CWD", "PYTHONPATH")}
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == gateway_cli.GATEWAY_SHADOW_CWD_EXIT_CODE, out
    assert "imported code tree" in out
