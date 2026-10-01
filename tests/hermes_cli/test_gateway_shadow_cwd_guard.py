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


def test_shadow_import_refuses_cwd_tree_when_another_install_exists(tmp_path):
    checkout = tmp_path / "wt"
    (checkout / "hermes_cli").mkdir(parents=True)
    runtime = tmp_path / "runtime" / "hermes-agent"
    (runtime / "hermes_cli").mkdir(parents=True)
    reason = gateway_cli._shadow_import_reason(
        str(checkout), package_root=checkout, install_root=runtime
    )
    assert reason is not None and "imported instead of the installed tree" in reason


def test_shadow_import_allows_trusted_checkout_with_external_env(tmp_path):
    """Prism r2 False refusal: a deployed checkout started from its own root
    with a system/shared interpreter is not a shadow (no other install, or the
    editable install points back at the same tree)."""
    checkout = tmp_path / "deploy" / "hermes-agent"
    (checkout / "hermes_cli").mkdir(parents=True)
    profile = tmp_path / "profiles" / "apollo"
    profile.mkdir(parents=True)
    # system interpreter, nothing else provides hermes_cli
    assert gateway_cli._shadow_import_reason(
        str(checkout), package_root=checkout, install_root=None
    ) is None
    # external venv with an editable install of this same checkout
    assert gateway_cli._shadow_import_reason(
        str(checkout), package_root=checkout, install_root=checkout
    ) is None
    # service cwd is a profile dir, code is the install
    assert gateway_cli._shadow_import_reason(
        str(profile), package_root=checkout, install_root=tmp_path / "x"
    ) is None


def test_install_root_without_cwd_ignores_cwd_entry(tmp_path, monkeypatch):
    """The probe must not count the cwd entry itself as 'another install'."""
    shadow = tmp_path / "shadow"
    (shadow / "hermes_cli").mkdir(parents=True)
    (shadow / "hermes_cli" / "__init__.py").write_text("")
    monkeypatch.setattr(gateway_cli.sys, "path", ["", str(shadow), str(REPO_ROOT)])
    found = gateway_cli._install_root_without_cwd(shadow.resolve())
    assert found is None or found != shadow.resolve()


def test_launcher_argvs_keep_cwd_off_sys_path(monkeypatch, tmp_path):
    """Every gateway launcher argv this module generates carries -P (Prism r2
    Late guard / Guard bypass): the class fix is at the launcher, before any
    hermes_cli import, not in run_gateway."""
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: "/venv/bin/python")
    monkeypatch.setattr(gateway_cli, "_profile_arg", lambda *a, **k: "")
    # an installed tree (venv / editable): the only case a cwd can shadow
    monkeypatch.setattr(
        gateway_cli, "_launcher_install_root", lambda *a, **k: gateway_cli.PROJECT_ROOT
    )
    argvs = [
        gateway_cli._gateway_run_command(),
        gateway_cli._gateway_run_args_for_profile("default"),
        gateway_cli._timestamped_stderr_gateway_command(tmp_path / "e.log"),
        gateway_cli._timestamped_stderr_gateway_command(
            tmp_path / "e.log", external_supervisor=True
        ),
    ]
    for argv in argvs:
        m = argv.index("-m")
        assert "-P" in argv[1:m], argv
    tmp_home = tmp_path / "home"
    tmp_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_home))
    unit = gateway_cli.generate_systemd_unit(system=False)
    exec_start = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert exec_start, unit
    for line in exec_start:
        parts = line.split()
        m = parts.index("-m")
        assert "-P" in parts[1:m], line


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


def _write_shadow_package(root: Path, marker: Path) -> None:
    pkg = root / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('shadow executed')\n"
    )
    (pkg / "main.py").write_text(
        f"open({str(marker)!r}, 'w').write('shadow main executed')\n"
    )
    (pkg / "stderr_timestamp.py").write_text(
        f"open({str(marker)!r}, 'w').write('shadow stderr_timestamp executed')\n"
    )


def test_real_shadow_package_executes_without_launcher_flag(tmp_path):
    """Red-on-base proof for Prism r1/r2 Late guard: without -P a regular
    shadow package in the cwd runs before run_gateway() can refuse it."""
    shadow = tmp_path / "shadow"
    marker = tmp_path / "marker"
    _write_shadow_package(shadow, marker)
    env = {k: v for k, v in os.environ.items() if k != "HERMES_ALLOW_SHADOW_CWD"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
        cwd=shadow, env=env, capture_output=True, text=True, timeout=120,
    )
    assert marker.exists()


def test_launcher_argv_never_executes_real_shadow_package(tmp_path, monkeypatch):
    """The generated launcher argv (-P) imports the install, not a regular
    shadow package in the cwd; the install's guard then refuses the cwd."""
    shadow = tmp_path / "shadow"
    marker = tmp_path / "marker"
    _write_shadow_package(shadow, marker)
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: sys.executable)
    monkeypatch.setattr(gateway_cli, "_profile_arg", lambda *a, **k: "")
    monkeypatch.setattr(
        gateway_cli, "_launcher_install_root", lambda *a, **k: gateway_cli.PROJECT_ROOT
    )
    argv = gateway_cli._gateway_run_command()
    argv = [a for a in argv if a != "--replace"]
    env = {k: v for k, v in os.environ.items() if k != "HERMES_ALLOW_SHADOW_CWD"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        argv, cwd=shadow, env=env, capture_output=True, text=True, timeout=120
    )
    out = proc.stdout + proc.stderr
    assert not marker.exists(), marker.read_text()
    assert proc.returncode == gateway_cli.GATEWAY_SHADOW_CWD_EXIT_CODE, out
    assert f"cwd {shadow.resolve()} " in out


def _checkout_only_deploy(tmp_path: Path, marker: Path) -> Path:
    deploy = tmp_path / "deploy" / "hermes-agent"
    pkg = deploy / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(f"open({str(marker)!r}, 'w').write('deploy ran')\n")
    return deploy


def test_checkout_only_deploy_launcher_runs_without_pythonpath(tmp_path, monkeypatch):
    """Prism r1 on #1618 (Checkout startup): a deployed checkout whose launch
    interpreter has no hermes install gets a launcher argv that still imports
    it. The subprocess gets no injected PYTHONPATH."""
    marker = tmp_path / "marker"
    deploy = _checkout_only_deploy(tmp_path, marker)
    monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", deploy)
    flags = gateway_cli._gateway_safe_path_args(sys.executable)
    assert flags == ()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, *flags, "-m", "hermes_cli.main"],
        cwd=deploy, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.read_text() == "deploy ran"


def test_unconditional_safe_path_flag_breaks_checkout_only_deploy(tmp_path):
    """Red-on-fdb040c7 proof: an unconditional -P cannot import the checkout."""
    marker = tmp_path / "marker"
    deploy = _checkout_only_deploy(tmp_path, marker)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-S", "-P", "-m", "hermes_cli.main"],
        cwd=deploy, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert not marker.exists()


def test_safe_path_flag_emitted_only_for_this_install(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gateway_cli, "_launcher_install_root", lambda *a, **k: gateway_cli.PROJECT_ROOT
    )
    assert gateway_cli._gateway_safe_path_args("/py") == ("-P",)
    monkeypatch.setattr(gateway_cli, "_launcher_install_root", lambda *a, **k: None)
    assert gateway_cli._gateway_safe_path_args("/py") == ()
    monkeypatch.setattr(
        gateway_cli, "_launcher_install_root", lambda *a, **k: tmp_path / "other"
    )
    assert gateway_cli._gateway_safe_path_args("/py") == ()


def test_probe_ignores_this_process_script_dir_and_pythonpath(tmp_path, monkeypatch):
    """Prism r2 on #1618 (Incorrect detection): a checkout on THIS process's
    sys.path (script dir) or PYTHONPATH is not inherited by the service, so it
    must not make the launcher emit -P."""
    marker = tmp_path / "marker"
    deploy = _checkout_only_deploy(tmp_path, marker)
    monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(gateway_cli.sys, "path", [str(deploy)] + sys.path)
    monkeypatch.setenv("PYTHONPATH", str(deploy))
    monkeypatch.chdir(tmp_path)
    assert gateway_cli._gateway_safe_path_args(sys.executable) == ()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wrapper interpreter")
def test_probe_detects_real_install_of_this_tree(tmp_path, monkeypatch):
    """Positive arm: an interpreter whose startup environment provides THIS
    tree (simulated install via a wrapper) gets -P, and the -P launcher still
    imports it from a neutral cwd."""
    marker = tmp_path / "marker"
    deploy = _checkout_only_deploy(tmp_path, marker)
    wrapper = tmp_path / "py"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH={deploy} exec {sys.executable} "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", deploy)
    flags = gateway_cli._gateway_safe_path_args(str(wrapper))
    assert flags == ("-P",)
    proc = subprocess.run(
        [str(wrapper), *flags, "-m", "hermes_cli.main"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.read_text() == "deploy ran"
