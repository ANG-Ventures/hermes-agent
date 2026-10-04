"""Gateway service-definition writes belong to the home the definition pins (t_8749a807).

2026-10-04: a kanban worker started a scratch E2E gateway on ACE-AI with
``HERMES_HOME=/srv/ci/scratch/...``. Its service name collided with the host's bare
``hermes-gateway`` unit and the on-boot refresh rewrote that unit (WorkingDirectory and
HERMES_HOME pointed at the scratch dir), so every later restart died ``200/CHDIR``.
The collision is simulated by pointing the unit/plist path at a file that pins another home.
"""

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gw
from hermes_cli import gateway_launchd
from hermes_cli.gateway_service_owner import INSTALL_DISABLED_ENV

_REAL_RETIRE_DROPIN = gw._retire_hermes_replace_dropin
LEGACY_DROPIN = ("# Added to end the gateway respawn storm\n[Service]\nExecStart=\n"
                 "ExecStart=/usr/bin/hermes gateway run --replace\n")

REAL_UNIT = (
    "[Service]\n"
    "ExecStart=/home/ace/.hermes/hermes-agent/.hermes/bin/hermes gateway run\n"
    "WorkingDirectory={home}\n"
    'Environment="HERMES_HOME={home}"\n'
    "SuccessExitStatus=75\n"
)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    real = tmp_path / "account" / ".hermes"
    scratch = tmp_path / "srv-ci-scratch" / "t_d284ff38" / "profile"
    real.mkdir(parents=True)
    scratch.mkdir(parents=True)
    # The process is the scratch gateway; the temp-dir guard is the OTHER defence
    # (/srv/ci/scratch is not a temp dir), so it is out of the way here.
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.setattr(gw, "_refuse_temp_home_service_write", lambda definition, kind: False)
    return SimpleNamespace(real=real, scratch=scratch)


@pytest.fixture
def systemd_unit(tmp_path, homes, monkeypatch):
    unit = tmp_path / "systemd" / "hermes-gateway.service"
    unit.parent.mkdir()
    unit.write_text(REAL_UNIT.format(home=homes.real), encoding="utf-8")
    calls = []
    monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit)
    monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: False)
    monkeypatch.setattr(gw, "_retire_hermes_replace_dropin", lambda system=False: False)
    monkeypatch.setattr(gw, "_prepare_service_launcher", lambda system=False, run_as_user=None: None)
    monkeypatch.setattr(gw, "_run_systemctl", lambda args, **kw: calls.append(tuple(args)) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(gw, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gw, "_ensure_linger_enabled", lambda *a, **k: True)
    monkeypatch.setattr(gw, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gw, "print_legacy_unit_warning", lambda: None)
    monkeypatch.setattr(gw, "generate_systemd_unit", lambda system=False, run_as_user=None:
                        REAL_UNIT.format(home=Path(gw.get_hermes_home())))
    return SimpleNamespace(path=unit, calls=calls, original=unit.read_text(encoding="utf-8"))


class TestSystemdWriters:
    def test_boot_refresh_leaves_unit_pinned_to_another_home_untouched(self, systemd_unit, capsys):
        assert gw.refresh_systemd_unit_if_needed(system=False) is False
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original
        assert ("daemon-reload",) not in systemd_unit.calls
        assert "Refusing to rewrite" in capsys.readouterr().out

    def test_boot_refresh_still_rewrites_its_own_stale_unit(self, systemd_unit, homes, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        systemd_unit.path.write_text("ExecStart=old\n" + f'Environment="HERMES_HOME={homes.real}"\n')
        # refresh's own test belt refuses a generated unit naming a pytest tmpdir; any marker-free body works.
        monkeypatch.setattr(gw, "generate_systemd_unit", lambda system=False, run_as_user=None: "ExecStart=new\n")
        assert gw.refresh_systemd_unit_if_needed(system=False) is True
        assert systemd_unit.path.read_text(encoding="utf-8") == "ExecStart=new\n"
        assert ("daemon-reload",) in systemd_unit.calls

    def test_force_install_does_not_overwrite_another_homes_unit(self, systemd_unit):
        gw.systemd_install(force=True, non_interactive=True)
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original
        assert ("daemon-reload",) not in systemd_unit.calls

    def test_force_unit_path_repoints_the_unit(self, systemd_unit, homes):
        gw.systemd_install(force_unit_path=True, non_interactive=True)
        assert f"HERMES_HOME={homes.scratch}" in systemd_unit.path.read_text(encoding="utf-8")

    def test_worker_kill_switch_blocks_refresh_of_own_unit(self, systemd_unit, homes, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        systemd_unit.path.write_text("ExecStart=old\n" + f'Environment="HERMES_HOME={homes.real}"\n')
        monkeypatch.setattr(gw, "generate_systemd_unit", lambda system=False, run_as_user=None: "ExecStart=new\n")
        assert gw.refresh_systemd_unit_if_needed(system=False) is False
        assert systemd_unit.path.read_text(encoding="utf-8").startswith("ExecStart=old")
        assert ("daemon-reload",) not in systemd_unit.calls


    @pytest.fixture
    def current_unit_with_dropin(self, systemd_unit, homes, monkeypatch):
        # The unit is current, so the only pending mutation is the legacy --replace drop-in.
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: True)
        monkeypatch.setattr(gw, "_retire_hermes_replace_dropin", _REAL_RETIRE_DROPIN)
        dropin = systemd_unit.path.parent / f"{systemd_unit.path.name}.d" / "20-replace.conf"
        dropin.parent.mkdir()
        dropin.write_text(LEGACY_DROPIN, encoding="utf-8")
        return dropin

    def test_worker_kill_switch_blocks_dropin_retirement_on_current_unit(
            self, systemd_unit, current_unit_with_dropin, monkeypatch):
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        assert gw.refresh_systemd_unit_if_needed(system=False) is False
        assert current_unit_with_dropin.exists()
        assert ("daemon-reload",) not in systemd_unit.calls

    def test_current_unit_still_retires_its_dropin(self, systemd_unit, current_unit_with_dropin):
        assert gw.refresh_systemd_unit_if_needed(system=False) is True
        assert not current_unit_with_dropin.exists()
        assert ("daemon-reload",) in systemd_unit.calls

    def test_force_unit_path_repoints_an_existing_system_unit(self, systemd_unit, homes, monkeypatch):
        # Under --system the installed unit's home is adopted into os.environ (sudo strips HERMES_HOME);
        # an explicit repoint must keep the caller's home instead.
        monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
        monkeypatch.setattr(gw, "_read_systemd_user_from_unit", lambda path: None)
        gw.systemd_install(system=True, force_unit_path=True, non_interactive=True)
        assert f"HERMES_HOME={homes.scratch}" in systemd_unit.path.read_text(encoding="utf-8")
        assert Path(gw.get_hermes_home()) == homes.scratch

    def test_worker_kill_switch_blocks_direct_install(self, systemd_unit, homes, monkeypatch):
        # ensure_gateway_service / setup wizard / migrate call systemd_install without _cmd_install.
        systemd_unit.path.unlink()
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        gw.systemd_install(non_interactive=True)
        assert not systemd_unit.path.exists()
        assert systemd_unit.calls == []


class TestLaunchdWriters:
    def test_boot_refresh_leaves_plist_pinned_to_another_home_untouched(self, tmp_path, homes, monkeypatch):
        plist = tmp_path / "ai.hermes.gateway.plist"
        plist.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway",
                                          "EnvironmentVariables": {"HERMES_HOME": str(homes.real)}}))
        original = plist.read_bytes()
        monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
        monkeypatch.setattr(gw, "launchd_plist_is_current", lambda: False)
        monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<plist>scratch</plist>")
        monkeypatch.setattr(gw, "_prepare_service_launcher", lambda *a, **k: pytest.fail("launcher prepared"))

        assert gateway_launchd.refresh_launchd_plist_if_needed() is False
        assert plist.read_bytes() == original

    def test_worker_kill_switch_blocks_direct_install(self, tmp_path, homes, monkeypatch):
        plist = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
        monkeypatch.setattr(gw, "_launchctl_label_supervising_process", lambda label: False)
        monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<plist>scratch</plist>")
        monkeypatch.setattr(gw, "_prepare_service_launcher", lambda *a, **k: pytest.fail("launcher prepared"))
        monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda *a, **k: pytest.fail("launchctl ran"))
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        gateway_launchd.launchd_install(start_now=False)
        assert not plist.exists()


class TestInstallCommand:
    @pytest.fixture
    def cli(self, tmp_path, monkeypatch):
        account = tmp_path / "account"
        installs = []
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {(account / ".hermes").resolve()})
        monkeypatch.setattr(gw, "is_managed", lambda: False)
        monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda force=False: None)
        monkeypatch.setattr(gw, "_service_mgmt_blocked", lambda: False)
        monkeypatch.setattr(gw, "_service_backend", lambda: "systemd")
        monkeypatch.setattr(gw, "refuses_container_user_scope_install", lambda system: False)
        monkeypatch.setattr(gw, "_home_owns_bare_service_name", lambda home: False)
        monkeypatch.setattr(gw, "_is_service_installed", lambda: False)
        monkeypatch.setattr(gw, "_install_systemd_from_cli", lambda args, **kw: installs.append(args))
        return SimpleNamespace(account=account, installs=installs)

    @staticmethod
    def _args(force_unit_path=False):
        return SimpleNamespace(force=False, system=False, run_as_user=None, if_missing=False,
                               force_unit_path=force_unit_path)

    def test_scratch_home_is_refused(self, cli, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "srv-ci-scratch" / "profile"))
        with pytest.raises(SystemExit) as exc:
            gw._cmd_install(self._args())
        assert exc.value.code == 1
        assert cli.installs == []
        assert "--force-unit-path" in capsys.readouterr().out

    def test_scratch_home_installs_with_force_unit_path(self, cli, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "srv-ci-scratch" / "profile"))
        gw._cmd_install(self._args(force_unit_path=True))
        assert len(cli.installs) == 1

    @pytest.mark.parametrize("rel", [".hermes", ".hermes/profiles/coder"])
    def test_account_tree_homes_install(self, cli, monkeypatch, rel):
        home = cli.account / rel
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        gw._cmd_install(self._args())
        assert len(cli.installs) == 1

    def test_worker_kill_switch_refuses_even_the_default_home(self, cli, monkeypatch):
        home = cli.account / ".hermes"
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        with pytest.raises(SystemExit):
            gw._cmd_install(self._args())
        assert cli.installs == []


def test_execute_code_children_keep_the_worker_kill_switch():
    from tools.code_execution_env import _HERMES_CHILD_ALLOWED
    assert INSTALL_DISABLED_ENV in _HERMES_CHILD_ALLOWED
