"""S5 (spec §5.7, §7 S5): ``hermes cron runner install|uninstall|lint``.

Every test runs against a scratch account home (``account_home`` patched) and an injected
``launchctl``/``pgrep``: no plist is ever loaded into a live launchd domain.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys

import pytest

from hermes_cli import cron_runner_plist as crp

GATEWAY_ENV = {
    "PATH": "/opt/venv/bin:/opt/homebrew/bin:/usr/bin:/bin",
    "HERMES_HOME": None,  # filled per test with the scratch home
    "HERMES_SUPERVISED_CHILD": "1",
}


class FakeLaunchd:
    """Records launchctl/pgrep calls; ``alive`` is the runner process set pgrep reports."""

    def __init__(self, alive=(), exits_after_bootout=True):
        self.calls = []
        self.alive = list(alive)
        self.exits_after_bootout = exits_after_bootout

    def launchctl(self, *args):
        self.calls.append(("launchctl", *args))
        if args and args[0] == "bootout" and self.exits_after_bootout:
            self.alive = []
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    def pgrep(self):
        self.calls.append(("pgrep",))
        return list(self.alive)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    vhome = home / ".hermes"
    vhome.mkdir()
    monkeypatch.setattr(crp, "account_home", lambda: home)
    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
    monkeypatch.setattr(crp, "runner_python", lambda: "/opt/venv/bin/python3")
    env = dict(GATEWAY_ENV, HERMES_HOME=str(vhome))
    return home, vhome, env


def _write_gateway(home, env):
    path = home / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist"
    path.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "EnvironmentVariables": env}))
    return path


def _runner_path(home):
    return home / "Library" / "LaunchAgents" / "ai.hermes.cron-script-runner.plist"


# ---- unit -------------------------------------------------------------------------------------

def test_generated_plist_matches_spec_and_copies_gateway_env(scratch):
    home, vhome, env = scratch
    _write_gateway(home, env)
    data = plistlib.loads(crp.render_plist(home))
    assert data["Label"] == "ai.hermes.cron-script-runner"
    assert data["EnvironmentVariables"] == env
    assert data["ProgramArguments"][0] == "/opt/venv/bin/python3"
    assert data["ProgramArguments"][-2:] == ["cron", "runner"]
    assert any("hermes_cli.main" in a for a in data["ProgramArguments"])
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["ProcessType"] == "Interactive" and data["ExitTimeOut"] == 30
    assert data["SoftResourceLimits"] == {"NumberOfFiles": 4096}
    assert data["HardResourceLimits"] == {"NumberOfFiles": 8192}
    assert data["WorkingDirectory"] == str(vhome)
    log = str(vhome / "logs" / "cron-script-runner.log")
    assert data["StandardOutPath"] == log and data["StandardErrorPath"] == log


def test_lint_green_after_install_red_on_source_path_mutation(scratch, capsys):
    home, _vhome, env = scratch
    gw = _write_gateway(home, env)
    fake = FakeLaunchd()
    assert crp.install(launchctl=fake.launchctl) == 0
    assert crp.lint() == 0
    mutated = dict(env, PATH="/usr/bin:/bin")
    gw.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "EnvironmentVariables": mutated}))
    assert crp.lint() == 1
    assert "PATH" in capsys.readouterr().out


def test_lint_allows_xpc_keys_only(scratch):
    home, _vhome, env = scratch
    gw = _write_gateway(home, env)
    assert crp.install(launchctl=FakeLaunchd().launchctl) == 0
    gw.write_bytes(plistlib.dumps({"EnvironmentVariables": dict(env, XPC_SERVICE_NAME="x")}))
    assert crp.lint() == 0
    gw.write_bytes(plistlib.dumps({"EnvironmentVariables": dict(env, EXTRA="1")}))
    assert crp.lint() == 1


def test_install_bootstraps_into_gui_domain(scratch):
    home, vhome, env = scratch
    _write_gateway(home, env)
    fake = FakeLaunchd()
    assert crp.install(launchctl=fake.launchctl) == 0
    path = _runner_path(home)
    assert plistlib.loads(path.read_bytes())["Label"] == crp.LABEL
    boot = [c for c in fake.calls if c[:2] == ("launchctl", "bootstrap")]
    assert boot and boot[-1][2].startswith("gui/") and boot[-1][3] == str(path)
    assert (vhome / "logs").is_dir()


def test_install_never_touches_the_gateway_plist(scratch):
    home, _vhome, env = scratch
    gw = _write_gateway(home, env)
    before = gw.read_bytes()
    assert crp.install(launchctl=FakeLaunchd().launchctl) == 0
    assert gw.read_bytes() == before


def test_uninstall_boots_out_removes_plist_and_confirms_process_gone(scratch):
    home, _vhome, env = scratch
    _write_gateway(home, env)
    fake = FakeLaunchd(alive=[4242])
    assert crp.install(launchctl=fake.launchctl) == 0
    assert crp.uninstall(launchctl=fake.launchctl, pgrep=fake.pgrep, wait_s=0) == 0
    assert not _runner_path(home).exists()
    assert ("launchctl", "bootout", f"gui/{crp._uid()}/{crp.LABEL}") in fake.calls
    assert ("pgrep",) in fake.calls


def test_uninstall_imports_no_cron_modules():
    """§12: uninstall runs BEFORE any S1/S2/S3 revert, so it must not depend on cron/*."""
    code = ("import sys; import hermes_cli.cron_runner_plist as m; "
            "assert not [k for k in sys.modules if k == 'cron' or k.startswith('cron.')], "
            "sorted(k for k in sys.modules if k.startswith('cron'))")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=crp.PROJECT_ROOT)


# ---- E2E (CLI parser -> handler) -----------------------------------------------------------

def _cli(argv):
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_install_dry_run_prints_lintable_plist_and_writes_nothing(scratch, capsys, tmp_path):
    home, vhome, env = scratch
    _write_gateway(home, env)
    assert _cli(["cron", "runner", "install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert not _runner_path(home).exists() and not (vhome / "logs").exists()
    body = out[out.index("<?xml"):]
    assert plistlib.loads(body.encode())["Label"] == crp.LABEL
    if shutil.which("plutil"):
        f = tmp_path / "dry.plist"
        f.write_text(body)
        subprocess.run(["plutil", "-lint", str(f)], check=True)


def test_cli_rearm_and_bare_runner_still_parse():
    import argparse

    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
    assert parser.parse_args(["cron", "runner"]).runner_action is None
    assert parser.parse_args(["cron", "runner", "rearm"]).runner_action == "rearm"
    assert parser.parse_args(["cron", "runner", "uninstall"]).runner_action == "uninstall"


# ---- negative -------------------------------------------------------------------------------

def test_missing_source_plist_exits_2_and_writes_nothing(scratch):
    home, vhome, _env = scratch
    fake = FakeLaunchd()
    assert crp.install(launchctl=fake.launchctl) == 2
    assert crp.install(dry_run=True, launchctl=fake.launchctl) == 2
    assert not _runner_path(home).exists() and not (vhome / "logs").exists()
    assert fake.calls == []


@pytest.mark.parametrize("where", ["environ", "gateway_plist", "dotenv"])
def test_install_refuses_78_when_machine_id_set(scratch, monkeypatch, where):
    home, vhome, env = scratch
    if where == "environ":
        monkeypatch.setenv("HERMES_MACHINE_ID", "x")
    elif where == "gateway_plist":
        env = dict(env, HERMES_MACHINE_ID="x")
    else:
        (vhome / ".env").write_text("FOO=1\nHERMES_MACHINE_ID=x\n")
    _write_gateway(home, env)
    fake = FakeLaunchd()
    assert crp.install(launchctl=fake.launchctl) == 78
    assert crp.install(dry_run=True, launchctl=fake.launchctl) == 78
    assert not _runner_path(home).exists()
    assert fake.calls == []


def test_uninstall_fails_when_process_survives_bootout(scratch, capsys):
    home, _vhome, env = scratch
    _write_gateway(home, env)
    fake = FakeLaunchd(alive=[4242], exits_after_bootout=False)
    assert crp.install(launchctl=fake.launchctl) == 0
    assert crp.uninstall(launchctl=fake.launchctl, pgrep=fake.pgrep, wait_s=0) == 1
    assert not _runner_path(home).exists()
    assert "4242" in capsys.readouterr().out


def test_lint_exit_2_when_runner_plist_missing(scratch):
    home, _vhome, env = scratch
    _write_gateway(home, env)
    assert crp.lint() == 2
