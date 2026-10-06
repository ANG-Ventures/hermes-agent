"""`hermes gateway install` / the refresh-on-start path must not overwrite a gateway service
definition this CLI did not generate.

Incident 2026-10-04, two hosts in one morning:
* Linux: a worker ran `hermes gateway install` with a scratch HERMES_HOME and rewrote the real user's
  hand-managed `hermes-gateway.service` (ExecStart on a checkout venv).
* macOS: a backtick inside a double-quoted `--handoff "..."` string made zsh run `hermes gateway
  install`, which replaced an operator-hardened LaunchAgent (`venv/bin/python -m hermes_cli.main
  gateway run --replace`) with the generated `osascript` form. That form could not boot on that
  install; launchd crash-looped it ~40x and the gateway was dead for 28 minutes.

Rule under test: a definition whose launcher is not the one the generator writes is FOREIGN and is
left untouched by `refresh_*_if_needed` and by `install` without `--force` /
HERMES_ALLOW_FOREIGN_SERVICE_OVERWRITE=1. A generated definition is refreshed/reinstalled as before.
"""
import subprocess
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway_cli

LABEL = "ai.hermes.gateway"
DOMAIN = "gui/501"

FLEET_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>/Users/x/.hermes/runtime/hermes-agent/venv/bin/python</string>
    <string>-m</string><string>hermes_cli.main</string>
    <string>gateway</string><string>run</string><string>--replace</string>
  </array>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
"""

GENERATED_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/osascript</string><string>-l</string><string>JavaScript</string>
    <string>-e</string><string>ObjC.import("stdlib"); $.system("exec /Users/x/.hermes/.hermes/bin/hermes gateway run")</string>
  </array>
</dict></plist>
"""

CHECKOUT_UNIT = """[Unit]
Description=Hermes Gateway
[Service]
Environment="HERMES_HOME=/home/ace/.hermes"
ExecStart="/home/ace/.hermes/hermes-agent/venv/bin/python" "-m" "hermes_cli.main" "gateway" "run"
"""

GENERATED_UNIT = """[Unit]
Description=Hermes Gateway
[Service]
Environment="HERMES_HOME=/home/ace/.hermes"
ExecStart="/home/ace/.hermes/.hermes/bin/hermes" "gateway" "run"
"""

# Nix/developer installs (no store Python): installation_command falls back to runtime_command.
DEV_GENERATED_UNIT = """[Unit]
Description=Hermes Gateway
[Service]
ExecStart="/home/ace/src/hermes-agent/venv/bin/python" "-I" "-c" "import os, sys, runpy; import hermes_bootstrap; runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)" "gateway" "run"
"""


# ---------------------------------------------------------------- predicate
@pytest.mark.parametrize(
    "text, kind, generated",
    [
        (FLEET_PLIST, "launchd plist", False),
        (GENERATED_PLIST, "launchd plist", True),
        ("\ufeff" + GENERATED_PLIST, "launchd plist", True),
        (CHECKOUT_UNIT, "systemd unit", False),
        (GENERATED_UNIT, "systemd unit", True),
        (DEV_GENERATED_UNIT, "systemd unit", True),
        # A quoted interpreter path with a space is still the generator's runtime_command shape.
        (DEV_GENERATED_UNIT.replace("/home/ace/src/", "/home/ace/Hermes Project/"), "systemd unit", True),
        (GENERATED_UNIT.replace("/home/ace/", "/home/a ce/"), "systemd unit", True),
        (CHECKOUT_UNIT.replace('"-m"', '"-I" "-c" "import os"'), "systemd unit", False),
        ("[Service]\nExecStart=/home/ace/.hermes/.hermes/bin/hermes gateway run\n", "systemd unit", True),
        ("not a service definition at all", "systemd unit", False),
        # systemd executable prefixes are not part of the path
        ("[Service]\nExecStart=-/home/ace/.hermes/bin/hermes gateway run\n", "systemd unit", True),
        ("[Service]\nExecStart=@/home/ace/.hermes/bin/hermes hermes gateway run\n", "systemd unit", True),
        # several ExecStart= lines: every one must be ours
        ("[Service]\nExecStart=/home/ace/.hermes/bin/hermes gateway run\nExecStart=/usr/bin/true\n", "systemd unit", False),
        ("[Service]\nExecStart=/usr/bin/true\nExecStart=/home/ace/.hermes/bin/hermes gateway run\n", "systemd unit", False),
        ("not a service definition at all", "launchd plist", False),
        # launchd runs `Program` instead of ProgramArguments[0]: a wrapper there is foreign
        (GENERATED_PLIST.replace("<key>ProgramArguments</key>", "<key>Program</key><string>/usr/local/bin/wrap</string><key>ProgramArguments</key>"), "launchd plist", False),
        (GENERATED_PLIST.replace("<key>ProgramArguments</key>", "<key>Program</key><string>/usr/bin/osascript</string><key>ProgramArguments</key>"), "launchd plist", True),
        # a commented-out generated block does not make the active (foreign) ProgramArguments ours
        (FLEET_PLIST.replace("<key>ProgramArguments</key>", "<!-- <key>ProgramArguments</key><array><string>/usr/bin/osascript</string></array> --><key>ProgramArguments</key>", 1), "launchd plist", False),
        # systemd accepts whitespace before the key and around `=`
        ("[Service]\nExecStart=/home/ace/.hermes/bin/hermes gateway run\n  ExecStart =\n ExecStart = /srv/venv/bin/python -m hermes_cli.main\n", "systemd unit", False),
        ("[Service]\n  ExecStart = /home/ace/.hermes/bin/hermes gateway run\n", "systemd unit", True),
    ],
)
def test_generated_shape_predicate(text, kind, generated):
    assert gateway_cli._service_definition_is_hermes_generated(text, kind) is generated


def test_refuse_reads_the_installed_file_not_the_generated_one(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, raising=False)
    p = tmp_path / f"{LABEL}.plist"
    p.write_text(FLEET_PLIST, encoding="utf-8")
    assert gateway_cli._refuse_foreign_service_overwrite(p, "launchd plist") is True
    out = capsys.readouterr().out
    assert "not generated by this CLI" in out and "--force" in out and gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV in out
    p.write_text(GENERATED_PLIST, encoding="utf-8")
    assert gateway_cli._refuse_foreign_service_overwrite(p, "launchd plist") is False
    assert gateway_cli._refuse_foreign_service_overwrite(tmp_path / "missing.plist", "launchd plist") is False


@pytest.mark.platforms("posix")
def test_unreadable_definition_is_refused_not_overwritten(tmp_path, capsys, monkeypatch):
    """Ownership cannot be established for a definition we cannot read, so it is left alone."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    monkeypatch.delenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, raising=False)
    p = tmp_path / "hermes-gateway.service"
    p.write_text(CHECKOUT_UNIT, encoding="utf-8")
    p.chmod(0o200)
    try:
        assert gateway_cli._refuse_foreign_service_overwrite(p, "systemd unit") is True
        assert "cannot read it" in capsys.readouterr().out
        assert gateway_cli._refuse_foreign_service_overwrite(p, "systemd unit", force=True) is False
    finally:
        p.chmod(0o600)
    assert p.read_text(encoding="utf-8") == CHECKOUT_UNIT


@pytest.mark.parametrize("how", ["force", "env"])
def test_operator_overrides_unlock_the_overwrite(tmp_path, monkeypatch, how):
    monkeypatch.delenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, raising=False)
    p = tmp_path / "hermes-gateway.service"
    p.write_text(CHECKOUT_UNIT, encoding="utf-8")
    if how == "env":
        monkeypatch.setenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, "1")
        assert gateway_cli._refuse_foreign_service_overwrite(p, "systemd unit") is False
    else:
        assert gateway_cli._refuse_foreign_service_overwrite(p, "systemd unit", force=True) is False


# ---------------------------------------------------------------- launchd paths
@pytest.fixture
def launchd(tmp_path, monkeypatch):
    state = SimpleNamespace(plist=tmp_path / "LaunchAgents" / f"{LABEL}.plist", launchctl=[], launchers=[])
    state.plist.parent.mkdir(parents=True)
    monkeypatch.delenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, raising=False)
    monkeypatch.setattr(gateway_cli, "_service_backend", lambda *a, **k: "launchd")
    monkeypatch.setattr(gateway_cli, "is_managed", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: state.plist)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: LABEL)
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: DOMAIN)
    monkeypatch.setattr(gateway_cli, "generate_launchd_plist", lambda: GENERATED_PLIST)
    monkeypatch.setattr(gateway_cli, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway_cli, "_prepare_service_launcher", lambda *a, **k: state.launchers.append(1))
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr(gateway_cli, "_launchctl_label_supervising_process", lambda label: False)
    monkeypatch.setattr(gateway_cli, "_launchctl_supervised_pid", lambda label: None)
    monkeypatch.setattr(gateway_cli, "_launchctl_bootstrap", lambda *a, **k: state.launchctl.append(("bootstrap",) + a))
    monkeypatch.setattr(gateway_cli, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: True)
    monkeypatch.setattr(gateway_cli, "_wait_for_pid_exit", lambda *a, **k: True)
    monkeypatch.setattr(gateway_cli, "_setup_service_action", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.gateway_launchd._spawn_deferred_launchd_reload", lambda **k: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    # Home admission/ownership (t_8749a807) is covered in test_gateway_service_owner.py; the launcher shape is pinned here.
    monkeypatch.setattr("hermes_cli.gateway_service_owner.home_may_install_service", lambda home: True)

    def fake_run(cmd, *a, **k):
        state.launchctl.append(tuple(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def test_refresh_leaves_a_fleet_managed_plist_alone(launchd, capsys):
    """The 2026-10-04 Mac path: start/install's refresh saw a plist != generated and rewrote it."""
    launchd.plist.write_text(FLEET_PLIST, encoding="utf-8")
    assert gateway_cli.refresh_launchd_plist_if_needed() is False
    assert launchd.plist.read_text(encoding="utf-8") == FLEET_PLIST
    assert launchd.launchers == [] and launchd.launchctl == []
    assert "not generated by this CLI" in capsys.readouterr().out


def test_refresh_still_updates_an_outdated_generated_plist(launchd):
    launchd.plist.write_text(GENERATED_PLIST.replace("JavaScript", "AppleScript"), encoding="utf-8")
    assert gateway_cli.refresh_launchd_plist_if_needed() is True
    assert launchd.plist.read_text(encoding="utf-8") == GENERATED_PLIST
    assert launchd.launchers == [1]


def test_install_refuses_a_fleet_managed_plist_unless_forced(launchd, monkeypatch):
    launchd.plist.write_text(FLEET_PLIST, encoding="utf-8")
    monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda: False)
    monkeypatch.setattr(gateway_cli, "refresh_launchd_plist_if_needed",
                        lambda: gateway_cli._refuse_foreign_service_overwrite(launchd.plist, "launchd plist") is False)
    with pytest.raises(SystemExit) as exc:  # a normal return let callers start the protected plist
        gateway_cli.launchd_install(force=False)
    assert exc.value.code == 1
    assert launchd.plist.read_text(encoding="utf-8") == FLEET_PLIST
    assert launchd.launchers == []
    gateway_cli.launchd_install(force=True)
    assert launchd.plist.read_text(encoding="utf-8") == GENERATED_PLIST
    assert launchd.launchers == [1]


# ---------------------------------------------------------------- systemd paths
@pytest.fixture
def systemd(tmp_path, monkeypatch):
    state = SimpleNamespace(unit=tmp_path / "hermes-gateway.service", systemctl=[], launchers=[])
    monkeypatch.delenv(gateway_cli._FOREIGN_SERVICE_OVERWRITE_ENV, raising=False)
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: state.unit)
    monkeypatch.setattr(gateway_cli, "_sync_hermes_home_from_systemd_unit", lambda system=False: None)
    monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: GENERATED_UNIT)
    monkeypatch.setattr(gateway_cli, "_retire_hermes_replace_dropin", lambda system=False: False)
    monkeypatch.setattr(gateway_cli, "_read_systemd_user_from_unit", lambda p: None)
    monkeypatch.setattr(gateway_cli, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway_cli, "_prepare_service_launcher", lambda *a, **k: state.launchers.append(1))
    monkeypatch.setattr(gateway_cli, "_run_systemctl", lambda args, **k: state.systemctl.append(tuple(args)))
    monkeypatch.delenv("HERMES_DISABLE_SERVICE_UNIT_REFRESH", raising=False)
    # The units pin /home/ace/.hermes, not the test home; ownership (t_8749a807) is covered in
    # test_gateway_service_owner.py, the launcher shape is what these tests pin.
    monkeypatch.setattr("hermes_cli.gateway_service_owner.definition_belongs_to_home", lambda *a: True)
    return state


def test_refresh_leaves_a_checkout_managed_unit_alone(systemd, capsys):
    """The 2026-10-04 Linux path (t_d284ff38): the unit on a checkout venv was treated as outdated."""
    systemd.unit.write_text(CHECKOUT_UNIT, encoding="utf-8")
    assert gateway_cli.refresh_systemd_unit_if_needed(system=False) is False
    assert systemd.unit.read_text(encoding="utf-8") == CHECKOUT_UNIT
    assert systemd.launchers == [] and systemd.systemctl == []
    assert "not generated by this CLI" in capsys.readouterr().out


def test_refresh_still_updates_an_outdated_generated_unit(systemd):
    systemd.unit.write_text(GENERATED_UNIT.replace("Hermes Gateway", "Old"), encoding="utf-8")
    assert gateway_cli.refresh_systemd_unit_if_needed(system=False) is True
    assert systemd.unit.read_text(encoding="utf-8") == GENERATED_UNIT
    assert ("daemon-reload",) in systemd.systemctl


@pytest.mark.parametrize("store_python", [False, True], ids=["nix-or-developer", "store"])
def test_the_real_generator_output_is_classified_as_generated(tmp_path, monkeypatch, store_python):
    """Whatever ExecStart generate_systemd_unit writes on this install kind, the guard must own it,
    or refresh refuses the CLI's own unit on every start/update (Prism P1 on #1742)."""
    import hermes_cli._launchers as launchers

    monkeypatch.setattr(launchers, "resolve_store_python",
                        lambda root: (tmp_path / "bin" / "python3") if store_python else None)
    unit = gateway_cli.generate_systemd_unit(system=False)
    assert gateway_cli._service_definition_is_hermes_generated(unit, "systemd unit") is True


def test_refresh_updates_an_outdated_developer_install_unit(systemd):
    systemd.unit.write_text(DEV_GENERATED_UNIT.replace("Hermes Gateway", "Old"), encoding="utf-8")
    assert gateway_cli.refresh_systemd_unit_if_needed(system=False) is True
    assert systemd.unit.read_text(encoding="utf-8") == GENERATED_UNIT


def test_install_repair_path_refuses_a_protected_plist_without_a_force_hint(launchd, monkeypatch, capsys):
    """Prism P1 (gateway_launchd.py repair path): a protected plist must not fall into the 'could not be
    reloaded ... run --force' recovery text — that hint points the operator at the exact override."""
    launchd.plist.write_text(FLEET_PLIST, encoding="utf-8")
    monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda: False)
    monkeypatch.setattr(gateway_cli, "_launchctl_label_supervising_process", lambda label: True)
    called = []
    monkeypatch.setattr(gateway_cli, "refresh_launchd_plist_if_needed", lambda: called.append(1) or False)
    with pytest.raises(SystemExit):
        gateway_cli.launchd_install(force=False)
    out = capsys.readouterr().out
    assert called == []
    assert "not generated by this CLI" in out
    assert "could not be reloaded" not in out and "Repairing outdated" not in out
    assert launchd.plist.read_text(encoding="utf-8") == FLEET_PLIST


@pytest.mark.parametrize("fresh", [False, True], ids=["repair-path", "fresh-write-path"])
def test_systemd_install_refusing_a_foreign_unit_exits_nonzero(systemd, monkeypatch, fresh):
    """Prism P1 b55e7a3ef445 (#1740): the foreign-overwrite refusal returned normally, so
    `_install_systemd_from_cli` went on to `systemd_start` the protected unit and migration took the refused
    pre-install for success and removed every secondary. It now exits 1 on both systemd install paths."""
    systemd.unit.write_text(CHECKOUT_UNIT, encoding="utf-8")
    monkeypatch.setattr(gateway_cli, "systemd_unit_is_current", lambda system=False: False)
    monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway_service_owner.assert_may_mutate", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        # force_unit_path skips the repair branch, reaching the fresh-write guard with force=False.
        gateway_cli.systemd_install(force_unit_path=fresh, non_interactive=True)
    assert exc.value.code == 1
    assert systemd.unit.read_text(encoding="utf-8") == CHECKOUT_UNIT
    assert systemd.launchers == []
