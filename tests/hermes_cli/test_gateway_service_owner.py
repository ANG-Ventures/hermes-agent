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
_REAL_IS_CURRENT = gw.systemd_unit_is_current
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
    # Both homes may own a host service, so what refuses below is the OWNERSHIP check, not admission.
    monkeypatch.setattr(gw, "_native_service_homes", lambda: {homes.real.resolve(), homes.scratch.resolve()})
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
        # A generator-shaped launcher, so main's foreign-definition guard (#1742) admits the rewrite.
        systemd_unit.path.write_text("ExecStart=/opt/v/.hermes/bin/hermes gateway run --old\n"
                                     + f'Environment="HERMES_HOME={homes.real}"\n')
        # refresh's own test belt refuses a generated unit naming a pytest tmpdir; any marker-free body works.
        monkeypatch.setattr(gw, "generate_systemd_unit", lambda system=False, run_as_user=None: "ExecStart=new\n")
        assert gw.refresh_systemd_unit_if_needed(system=False) is True
        assert systemd_unit.path.read_text(encoding="utf-8") == "ExecStart=new\n"
        assert ("daemon-reload",) in systemd_unit.calls

    def test_force_install_does_not_overwrite_another_homes_unit(self, systemd_unit):
        with pytest.raises(SystemExit) as exc:
            gw.systemd_install(force=True, non_interactive=True)
        assert exc.value.code == 1
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

    @pytest.fixture
    def legacy_units(self, monkeypatch):
        removed = []
        monkeypatch.setattr(gw, "has_legacy_hermes_units", lambda: True)
        monkeypatch.setattr(gw, "remove_legacy_hermes_units",
                            lambda interactive=True, dry_run=False: removed.append(interactive) or (1, []))
        return removed

    def test_refused_install_removes_no_legacy_units(self, systemd_unit, legacy_units):
        # Removing the legacy units and then refusing the install left the host with no gateway at all.
        with pytest.raises(SystemExit):
            gw.systemd_install(non_interactive=True)
        assert legacy_units == []
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original

    def test_foreign_definition_refusal_removes_no_legacy_units(self, systemd_unit, homes, legacy_units, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))  # own home: the foreign-launcher guard refuses
        hand_managed = ("ExecStart=/opt/v/venv/bin/python -m hermes_cli.main gateway run --replace\n"
                        f'Environment="HERMES_HOME={homes.real}"\n')
        systemd_unit.path.write_text(hand_managed, encoding="utf-8")
        gw.systemd_install(non_interactive=True)
        assert legacy_units == []
        assert systemd_unit.path.read_text(encoding="utf-8") == hand_managed

    def test_admitted_install_still_removes_legacy_units(self, systemd_unit, homes, legacy_units):
        systemd_unit.path.unlink()
        gw.systemd_install(non_interactive=True)
        assert legacy_units == [False]
        assert systemd_unit.path.exists()

    def test_worker_kill_switch_blocks_direct_install(self, systemd_unit, homes, monkeypatch):
        # ensure_gateway_service / setup wizard / migrate call systemd_install without _cmd_install.
        systemd_unit.path.unlink()
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        with pytest.raises(SystemExit):
            gw.systemd_install(non_interactive=True)
        assert not systemd_unit.path.exists()
        assert systemd_unit.calls == []


    def test_refused_install_never_starts_the_other_homes_service(self, systemd_unit, monkeypatch):
        # `gateway install` starts the service after systemd_install returns; a refusal that returned
        # normally started the foreign-home gateway it had just refused to touch.
        started = []
        monkeypatch.setattr(gw, "systemd_start", lambda system=False: started.append(system))
        args = SimpleNamespace(start_now=True, start_on_login=True, force_unit_path=False)
        with pytest.raises(SystemExit):
            gw._install_systemd_from_cli(args, force=False, system=False, run_as_user=None)
        assert started == []
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original


    @pytest.fixture
    def system_scope(self, monkeypatch):
        monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
        monkeypatch.setattr(gw, "_read_systemd_user_from_unit", lambda path: None)
        monkeypatch.setattr(gw, "_ensure_system_service_linger", lambda *a, **k: True, raising=False)

    def test_system_install_checks_the_callers_home_before_adopting_the_units(
            self, systemd_unit, system_scope):
        # HERMES_HOME is explicitly the scratch home; the system unit pins the real one. Adopting the
        # unit's home first made the ownership check compare the unit with itself and --force rewrote it.
        with pytest.raises(SystemExit):
            gw.systemd_install(system=True, force=True, non_interactive=True)
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original
        assert ("daemon-reload",) not in systemd_unit.calls

    def test_system_refresh_checks_the_callers_home_before_adopting_the_units(
            self, systemd_unit, monkeypatch, capsys):
        def is_current(system=False):  # the production chokepoint adopts the unit's home, then compares
            gw._sync_hermes_home_from_systemd_unit(system=system)
            return False
        monkeypatch.setattr(gw, "systemd_unit_is_current", is_current)
        assert gw.refresh_systemd_unit_if_needed(system=True) is False
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original
        assert "Refusing to rewrite" in capsys.readouterr().out

    def test_sudo_stripped_home_still_adopts_the_system_unit(self, systemd_unit, homes, system_scope, monkeypatch):
        # sudo strips HERMES_HOME (the process falls back to its native default, root's): adoption is what
        # names the right home, and the unit is that home's own.
        monkeypatch.delenv("HERMES_HOME")
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {gw.get_hermes_home().resolve()})
        gw.systemd_install(system=True, force=True, non_interactive=True)
        assert Path(gw.get_hermes_home()) == homes.real
        assert f"HERMES_HOME={homes.real}" in systemd_unit.path.read_text(encoding="utf-8")

    def test_direct_install_refuses_a_scratch_home(self, systemd_unit, homes, monkeypatch):
        # setup / ensure_gateway_service call systemd_install without `gateway install`'s admission check.
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {homes.real.resolve()})
        systemd_unit.path.unlink()
        with pytest.raises(SystemExit):
            gw.systemd_install(non_interactive=True)
        assert not systemd_unit.path.exists()
        assert systemd_unit.calls == []


class TestRefusalLeavesTheEnvironmentAlone:
    """A refused mutation must not adopt the other home into os.environ (Prism on #1740: the setup wizard
    caught the refusal and went on with the FOREIGN home as its HERMES_HOME; a second refresh then passed)."""

    @pytest.fixture
    def system_scope(self, monkeypatch):
        monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
        monkeypatch.setattr(gw, "_read_systemd_user_from_unit", lambda path: None)
        monkeypatch.setattr(gw, "_ensure_system_service_linger", lambda *a, **k: True, raising=False)
        # The production compare, with its os.environ adoption: that adoption is what these tests pin.
        monkeypatch.setattr(gw, "systemd_unit_is_current", _REAL_IS_CURRENT)

    def test_refused_system_install_keeps_the_callers_home(self, systemd_unit, homes, system_scope):
        with pytest.raises(SystemExit):
            gw.systemd_install(system=True, force=True, non_interactive=True)
        assert Path(gw.get_hermes_home()) == homes.scratch
        assert gw._explicit_hermes_home() == homes.scratch
        # Nothing adopted: a second, forced attempt is refused the same way, not admitted as "its own".
        with pytest.raises(SystemExit):
            gw.systemd_install(system=True, force=True, non_interactive=True)
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original

    def test_refused_system_refresh_keeps_the_callers_home(self, systemd_unit, homes, system_scope):
        assert gw.refresh_systemd_unit_if_needed(system=True) is False
        assert Path(gw.get_hermes_home()) == homes.scratch
        assert gw.refresh_systemd_unit_if_needed(system=True) is False
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original

    def test_refused_system_uninstall_keeps_the_callers_home_and_the_unit(self, systemd_unit, homes, system_scope,
                                                                          monkeypatch):
        monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda action, system, **k: system)
        gw.systemd_uninstall(system=True)
        assert Path(gw.get_hermes_home()) == homes.scratch
        assert systemd_unit.path.exists()
        assert all(call[0] not in ("stop", "disable") for call in systemd_unit.calls)

    def test_setup_wizard_catch_path_keeps_the_callers_home(self, systemd_unit, homes, system_scope, monkeypatch):
        from hermes_cli import gateway_setup_wizard as wiz
        started = []
        monkeypatch.setattr(gw, "prompt_yes_no", lambda *a, **k: True)
        monkeypatch.setattr(gw, "is_wsl", lambda: False)
        monkeypatch.setattr(gw, "prompt_linux_gateway_install_scope", lambda: "system")
        monkeypatch.setattr(gw, "_default_system_service_user", lambda: "ace")
        monkeypatch.setattr(gw, "_system_service_identity",
                            lambda run_as_user=None: ("ace", "ace", str(homes.real.parent), 1000))
        monkeypatch.setattr(gw.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(gw, "_setup_service_action", lambda *a, **k: started.append(a))
        wiz._wizard_install_service("systemd")
        assert started == [], "a refused install must not start the other home's service"
        assert Path(gw.get_hermes_home()) == homes.scratch
        assert systemd_unit.path.read_text(encoding="utf-8") == systemd_unit.original


class TestRemovers:
    def test_legacy_unit_removal_skips_a_unit_pinning_another_home(self, systemd_unit, homes, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        own = tmp_path / "systemd" / "hermes.service"
        own.write_text(REAL_UNIT.format(home=homes.real), encoding="utf-8")
        monkeypatch.setattr(gw, "_find_legacy_hermes_units", lambda: [
            ("hermes.service", own, False), ("hermes-gateway-old.service", systemd_unit.path, False)])
        systemd_unit.path.write_text(REAL_UNIT.format(home=homes.scratch), encoding="utf-8")
        removed, remaining = gw.remove_legacy_hermes_units(interactive=False)
        assert removed == 1 and not own.exists()
        assert remaining == [systemd_unit.path] and systemd_unit.path.exists()

    def test_worker_kill_switch_blocks_legacy_unit_removal(self, systemd_unit, homes, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        monkeypatch.setattr(gw, "_find_legacy_hermes_units", lambda: [("hermes.service", systemd_unit.path, False)])
        removed, remaining = gw.remove_legacy_hermes_units(interactive=False)
        assert removed == 0 and remaining == [systemd_unit.path] and systemd_unit.path.exists()
        assert systemd_unit.calls == []

    def test_uninstall_leaves_another_homes_unit(self, systemd_unit, monkeypatch):
        monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda action, system, **k: system)
        gw.systemd_uninstall(system=False)
        assert systemd_unit.path.exists() and systemd_unit.calls == []

    def test_worker_kill_switch_blocks_uninstall_of_own_unit(self, systemd_unit, homes, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(homes.real))
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda action, system, **k: system)
        gw.systemd_uninstall(system=False)
        assert systemd_unit.path.exists() and systemd_unit.calls == []


class TestRunAsUserRemap:
    """P2 890f17d44d83: a --system unit installed with --run-as-user pins the SERVICE user's home
    (/home/alice/.hermes), not root's; the operator's reinstall from /root/.hermes owns it."""

    def test_reinstall_of_a_remapped_system_unit_is_admitted(self, systemd_unit, tmp_path, monkeypatch):
        root_home, alice_home = tmp_path / "root", tmp_path / "alice"
        for h in (root_home / ".hermes", alice_home / ".hermes"):
            h.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: root_home))
        monkeypatch.setenv("HERMES_HOME", str(root_home / ".hermes"))
        monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
        monkeypatch.setattr(gw, "_read_systemd_user_from_unit", lambda path: "alice")
        monkeypatch.setattr(gw, "_system_service_identity",
                            lambda run_as_user=None: ("alice", "alice", str(alice_home), 1001))
        monkeypatch.setattr(gw, "_ensure_system_service_linger", lambda *a, **k: True, raising=False)
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {(root_home / ".hermes").resolve()})
        systemd_unit.path.write_text(REAL_UNIT.format(home=alice_home / ".hermes"), encoding="utf-8")
        monkeypatch.setattr(gw, "generate_systemd_unit", lambda system=False, run_as_user=None:
                            "User=alice\n" + REAL_UNIT.format(home=alice_home / ".hermes"))
        gw.systemd_install(system=True, force=True, run_as_user="alice", non_interactive=True)
        assert systemd_unit.path.read_text(encoding="utf-8").startswith("User=alice")
        assert ("daemon-reload",) in systemd_unit.calls


class TestPinnedHomeParsing:
    """The owner check reads HERMES_HOME the way systemd does; a missed pin reads as "unowned"."""

    def test_comment_inside_a_continuation_does_not_lose_the_pin(self, tmp_path):
        # systemd.syntax: a comment line inside a continued line is skipped and the continuation goes on.
        unit = tmp_path / "u.service"
        unit.write_text('[Service]\nEnvironment="PATH=/usr/bin" \\\n# the home\n  "HERMES_HOME=/srv/other"\n',
                        encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/other"

    def test_only_the_service_section_pins(self, tmp_path):
        unit = tmp_path / "u.service"
        unit.write_text('[Unit]\nEnvironment="HERMES_HOME=/srv/other"\n[Service]\nExecStart=/x\n'
                        '[Install]\nEnvironment="HERMES_HOME=/srv/install"\n', encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) is None
        unit.write_text('[Service]\nEnvironment="HERMES_HOME=/srv/other"\n[Install]\nEnvironment=\n',
                        encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/other", "an [Install] reset must not clear [Service]"

    def test_environment_file_overrides_environment(self, tmp_path):
        env_file = tmp_path / "gw.env"
        env_file.write_text("HERMES_HOME=/srv/from-file\n", encoding="utf-8")
        unit = tmp_path / "u.service"
        unit.write_text(f'[Service]\nEnvironment="HERMES_HOME=/srv/inline"\nEnvironmentFile=-{env_file}\n',
                        encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/from-file"
        unit.write_text(f'[Service]\nEnvironment="HERMES_HOME=/srv/inline"\nEnvironmentFile=-{tmp_path}/missing\n',
                        encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/inline"

    def test_parser_follows_systemd_grammar_end_to_end(self):
        """ONE parser (vendored from the fleet gateway-unit lint): section state, continuation across a
        comment, quoted multi-assign words, specifier expansion, reset, EnvironmentFile and UnsetEnvironment."""
        from hermes_cli import gateway_unit_parse as p
        assert list(p.unit_assignments("[Service]\nA=1 \\\n# c\n 2\n")) == [("Service", "A", "1  2")]
        text = ('[Service]\nEnvironment="A=x y" B=%%h \\\n; note\n C=%h/.v\nUnsetEnvironment=B\n'
                '[Install]\nEnvironment=\n')
        assert p.environment_of([text], "/home/u") == {"A": "x y", "C": "/home/u/.v"}
        files = {"/etc/gw.env": 'C="/srv/file"\n'}
        assert p.environment_of(["[Service]\nEnvironment=C=/inline\nEnvironmentFile=/etc/gw.env\n"], None,
                                env_files=files) == {"C": "/srv/file"}

    @pytest.mark.parametrize("line", [
        'Environment="PATH=/usr/bin" "HERMES_HOME={home}"',
        "Environment=PATH=/usr/bin HERMES_HOME={home}",
        'Environment="HERMES_HOME=/elsewhere" "HERMES_HOME={home}"',
    ])
    def test_multi_assignment_lines_pin_the_home(self, tmp_path, line):
        unit = tmp_path / "u.service"
        unit.write_text("[Service]\n" + line.format(home="/srv/other") + "\n", encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/other"

    def test_later_assignment_wins_and_empty_environment_resets(self, tmp_path):
        unit = tmp_path / "u.service"
        unit.write_text('[Service]\nEnvironment="HERMES_HOME=/a"\nEnvironment="HERMES_HOME=/b"\n', encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/b"
        unit.write_text('[Service]\nEnvironment="HERMES_HOME=/a"\nEnvironment=\n', encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) is None

    @pytest.mark.parametrize("body", [
        'Environment = "HERMES_HOME={home}"',
        'Environment="PATH=/usr/bin" \\\n    "HERMES_HOME={home}"',
        'Environment=\\\n HERMES_HOME={home}',
    ])
    def test_spacing_and_continuation_lines_pin_the_home(self, tmp_path, body):
        unit = tmp_path / "u.service"
        unit.write_text("[Service]\n" + body.format(home="/srv/other") + "\n", encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == "/srv/other"

    def test_commented_assignment_pins_nothing(self, tmp_path):
        unit = tmp_path / "u.service"
        unit.write_text('[Service]\n# Environment="HERMES_HOME=/srv/other"\n', encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) is None

    def test_quoted_value_with_escapes_round_trips(self, tmp_path):
        unit = tmp_path / "u.service"
        unit.write_text("[Service]\n" + gw._systemd_env_line("HERMES_HOME", '/srv/o"d%dir'), encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == '/srv/o"d%dir'

    def test_multi_assignment_unit_of_another_home_is_not_overwritten(self, systemd_unit, homes):
        systemd_unit.path.write_text(
            f'[Service]\nEnvironment="PATH=/usr/bin" "HERMES_HOME={homes.real}"\n', encoding="utf-8")
        original = systemd_unit.path.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            gw.systemd_install(force=True, non_interactive=True)
        assert systemd_unit.path.read_text(encoding="utf-8") == original


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
        with pytest.raises(SystemExit):
            gateway_launchd.launchd_install(start_now=False)
        assert not plist.exists()


    def test_direct_install_refuses_a_scratch_home(self, tmp_path, homes, monkeypatch):
        plist = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {homes.real.resolve()})
        monkeypatch.setattr(gw, "_home_owns_bare_service_name", lambda home: False)
        monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
        monkeypatch.setattr(gw, "_launchctl_label_supervising_process", lambda label: False)
        monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<plist>scratch</plist>")
        monkeypatch.setattr(gw, "_prepare_service_launcher", lambda *a, **k: pytest.fail("launcher prepared"))
        monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda *a, **k: pytest.fail("launchctl ran"))
        with pytest.raises(SystemExit):
            gateway_launchd.launchd_install(start_now=False)
        assert not plist.exists()

    def test_start_self_heal_refuses_a_scratch_home(self, tmp_path, homes, monkeypatch):
        # `gateway start` regenerates a missing plist: an implicit install, so it takes install's admission.
        plist = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(gw, "_native_service_homes", lambda: {homes.real.resolve()})
        monkeypatch.setattr(gw, "_home_owns_bare_service_name", lambda home: False)
        monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
        monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<plist>scratch</plist>")
        monkeypatch.setattr(gw, "_prepare_service_launcher", lambda *a, **k: pytest.fail("launcher prepared"))
        monkeypatch.setattr(gateway_launchd, "_launchd_bootstrap_and_kickstart",
                            lambda *a, **k: pytest.fail("launchctl ran"))
        with pytest.raises(SystemExit):
            gateway_launchd.launchd_start()
        assert not plist.exists()


class TestMigration:
    def test_worker_kill_switch_refuses_before_any_service_is_removed(self, monkeypatch, tmp_path):
        from hermes_cli import gateway_migrate as gm
        monkeypatch.setenv(INSTALL_DISABLED_ENV, "1")
        for name in ("_remove_secondary_gateways", "_write_manifest", "_write_multiplex_flag", "_resume_target"):
            monkeypatch.setattr(gm, name, lambda *a, **k: pytest.fail("migration touched the host"))
        plan = SimpleNamespace(already_multiplexed=False, manifest={"version": 1}, blocked=False,
                               default_home=tmp_path)
        assert gm.apply_migration(plan) is False


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
        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: tmp_path / "no-unit.service")
        monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: tmp_path / "no-plist.plist")
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

    def test_same_named_unit_of_another_home_does_not_admit_a_scratch_home(self, cli, tmp_path, monkeypatch):
        # A host unit at this name pinning ANOTHER home made "a service is installed" true for every caller.
        other = cli.account / ".hermes"
        unit = tmp_path / "systemd" / "hermes-gateway.service"
        unit.parent.mkdir()
        unit.write_text(REAL_UNIT.format(home=other), encoding="utf-8")
        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit)
        monkeypatch.setattr(gw, "_is_service_installed", lambda: True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "srv-ci-scratch" / "profile"))
        with pytest.raises(SystemExit):
            gw._cmd_install(self._args())
        assert cli.installs == []

    def test_reinstall_of_a_unit_pinning_this_home_is_allowed(self, cli, tmp_path, monkeypatch):
        home = tmp_path / "srv-custom" / "home"
        home.mkdir(parents=True)
        unit = tmp_path / "systemd" / "hermes-gateway-abc.service"
        unit.parent.mkdir()
        unit.write_text(REAL_UNIT.format(home=home), encoding="utf-8")
        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit)
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


class TestChokepointLint:
    """``scripts/check_service_definition_writers.py`` (CI lint): every service-definition write/remove in
    the gateway modules reaches ``assert_may_mutate``; a writer that bypasses it is a lint failure."""

    @pytest.fixture
    def guard(self):
        import importlib.util
        repo = Path(gw.__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "check_service_definition_writers", repo / "scripts" / "check_service_definition_writers.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_shipped_gateway_modules_pass(self, guard):
        assert guard.main([]) == 0

    def test_a_bypassing_writer_is_flagged(self, guard, tmp_path, monkeypatch):
        bad = tmp_path / "hermes_cli" / "gateway_launchd.py"
        bad.parent.mkdir()
        bad.write_text(
            "def ok(plist_path):\n    assert_may_mutate(plist_path, 'x')\n    plist_path.write_text('a')\n"
            "def via_helper(plist_path):\n    ok(plist_path)\n    plist_path.unlink()\n"
            "def bad(unit_path):\n    unit_path.write_text('b')\n"
            "def marker(path):\n    path.write_text('not a definition')\n",
            encoding="utf-8")
        monkeypatch.setattr(guard, "ROOT", tmp_path)
        problems = guard.scan_file(bad)
        assert len(problems) == 1 and "bad() mutates unit_path" in problems[0]


class TestSpecifierHome:
    """Prism P1 b548fb4f89f3 (#1740): ``%h`` is the SERVICE MANAGER's home, never the caller's ``HOME``."""

    def test_user_unit_percent_h_uses_the_account_home_not_a_scratch_home(self, tmp_path, monkeypatch):
        alice, scratch = tmp_path / "home" / "alice", tmp_path / "srv" / "scratch"
        monkeypatch.setenv("HOME", str(scratch))
        monkeypatch.setenv("HERMES_REAL_HOME", str(alice))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: scratch))
        unit = tmp_path / "user" / "hermes-gateway.service"
        unit.parent.mkdir()
        unit.write_text('[Service]\nEnvironment="HERMES_HOME=%h/.hermes"\n', encoding="utf-8")
        assert gw._hermes_home_pinned_by_unit(unit) == f"{alice}/.hermes"
        from hermes_cli.gateway_service_owner import pinned_home
        assert pinned_home(unit) == f"{alice}/.hermes"

    def test_system_unit_percent_h_is_the_system_manager_home(self, monkeypatch):
        import pwd
        from hermes_cli.gateway_unit_parse import manager_home_for_unit
        monkeypatch.setenv("HOME", "/srv/scratch")
        assert manager_home_for_unit(Path("/etc/systemd/system/hermes-gateway.service")) == pwd.getpwuid(0).pw_dir


class TestUnknownServiceUser:
    """CI slice 10 on eb7f3ded: an unknown --run-as-user crashed the ownership check with a bare
    ValueError before the install's own handling; an unknown account is not a foreign home."""

    def test_unknown_run_as_user_does_not_crash_the_chokepoint(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: tmp_path / "u.service")

        def _identity(run_as_user=None):
            raise ValueError(f"Unknown user: {run_as_user}")

        monkeypatch.setattr(gw, "_system_service_identity", _identity)

        class _Stop(Exception):
            pass

        def _chokepoint(path, action, pinned, **kw):
            seen["pinned"] = pinned
            raise _Stop

        monkeypatch.setattr("hermes_cli.gateway_service_owner.assert_may_mutate", _chokepoint)
        with pytest.raises(_Stop):
            gw.systemd_install(system=True, run_as_user="nosuchuser-xyz", non_interactive=True)
        assert seen["pinned"] == gw._service_home_for_unit(tmp_path / "u.service", True)
