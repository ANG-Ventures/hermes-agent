from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
HEALTH_SCRIPT = REPO_ROOT / "staging" / "scripts" / "compression-health.sh"
CRON_REGISTRATION = REPO_ROOT / "staging" / "cron" / "compression-health.json"


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_rtk(bin_dir: Path) -> Path:
    return _write_exe(
        bin_dir / "rtk",
        "#!/bin/sh\n"
        "if [ \"$1\" != rewrite ]; then echo unexpected >&2; exit 64; fi\n"
        "printf 'rtk %s\\n' \"$2\"\n"
        "exit 3\n",
    )


def _write_config(home: Path, platform: str) -> Path:
    if platform == "darwin":
        cfg = home / "Library" / "Application Support" / "rtk" / "config.toml"
    else:
        cfg = home / ".config" / "rtk" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '[hooks]\nexclude_commands = ["docker", "kubectl"]\n'
        '[telemetry]\nenabled = false\n',
        encoding="utf-8",
    )
    return cfg


def _run_health(tmp_path: Path, *, platform: str, home: Path, path: str, args: list[str] | None = None, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": path,
            "HERMES_COMPRESSION_HEALTH_PLATFORM": platform,
            "HERMES_COMPRESSION_HEALTH_TIMEOUT": "2",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HEALTH_SCRIPT), "--json", *(args or [])],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def _json_result(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.stdout.strip(), proc.stderr
    return json.loads(proc.stdout)


def test_macos_health_uses_profile_launchd_pid_and_gateway_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = _write_config(home, "darwin")
    fakebin = tmp_path / "fakebin"
    default_gateway = tmp_path / "default-gateway-bin"
    daedalus_gateway = tmp_path / "daedalus-gateway-bin"
    script_bin = tmp_path / "script-bin"
    for d in (fakebin, default_gateway, daedalus_gateway, script_bin):
        d.mkdir()
    rtk = _write_rtk(daedalus_gateway)
    _write_rtk(script_bin)
    _write_exe(
        fakebin / "launchctl",
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  ai.hermes.gateway) printf '{\\n\\t\"Label\" = \"ai.hermes.gateway\";\\n\\t\"PID\" = 111;\\n};\\n' ;;\n"
        "  ai.hermes.gateway-daedalus) printf '{\\n\\t\"Label\" = \"ai.hermes.gateway-daedalus\";\\n\\t\"PID\" = 222;\\n};\\n' ;;\n"
        "  *) exit 3 ;;\n"
        "esac\n",
    )
    _write_exe(
        fakebin / "ps",
        "#!/bin/sh\n"
        "pid=$3\n"
        "printf '  PID TT STAT TIME COMMAND\\n'\n"
        "if [ \"$pid\" = 111 ]; then printf '111 ?? S 0:00 hermes PATH=%s OTHER=x\\n' '" + str(default_gateway) + "'; exit 0; fi\n"
        "if [ \"$pid\" = 222 ]; then printf '222 ?? S 0:00 hermes PATH=%s:/usr/bin OTHER=x\\n' '" + str(daedalus_gateway) + "'; exit 0; fi\n"
        "exit 1\n",
    )

    proc = _run_health(
        tmp_path,
        platform="darwin",
        home=home,
        path=f"{fakebin}:{script_bin}:/usr/bin:/bin",
        args=["--profile", "daedalus"],
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = _json_result(proc)
    assert payload["status"] == "HEALTHY"
    assert payload["gateway_pid"] == 222
    assert payload["rtk_path"] == str(rtk)
    assert payload["config_path"] == str(config)
    assert "present-but-corrupting" in payload["scope_limit"]


def test_unhealthy_when_rtk_only_exists_on_script_path_not_gateway_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home, "darwin")
    fakebin = tmp_path / "fakebin"
    gateway_bin = tmp_path / "gateway-bin-without-rtk"
    script_bin = tmp_path / "script-bin"
    for d in (fakebin, gateway_bin, script_bin):
        d.mkdir()
    _write_rtk(script_bin)
    _write_exe(
        fakebin / "launchctl",
        "#!/bin/sh\nprintf 'PID\\tStatus\\tLabel\\n222\\t0\\tai.hermes.gateway-daedalus\\n'\n",
    )
    _write_exe(
        fakebin / "ps",
        "#!/bin/sh\nprintf '  PID TT STAT TIME COMMAND\\n222 ?? S 0:00 hermes PATH=%s:/usr/bin OTHER=x\\n' '" + str(gateway_bin) + "'\n",
    )

    proc = _run_health(
        tmp_path,
        platform="darwin",
        home=home,
        path=f"{fakebin}:{script_bin}:/usr/bin:/bin",
        args=["--profile", "daedalus"],
    )

    assert proc.returncode != 0
    payload = _json_result(proc)
    assert payload["status"] == "UNHEALTHY"
    assert payload["reason"] == "rtk not found on gateway PATH"
    assert payload["script_path_has_rtk"] is True
    assert str(script_bin / "rtk") not in payload.get("rtk_path", "")


def test_unhealthy_when_profile_gateway_not_running(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home, "darwin")
    fakebin = tmp_path / "fakebin"
    script_bin = tmp_path / "script-bin"
    fakebin.mkdir()
    script_bin.mkdir()
    _write_rtk(script_bin)
    _write_exe(fakebin / "launchctl", "#!/bin/sh\nexit 3\n")

    proc = _run_health(
        tmp_path,
        platform="darwin",
        home=home,
        path=f"{fakebin}:{script_bin}:/usr/bin:/bin",
        args=["--profile", "daedalus"],
    )

    assert proc.returncode != 0
    payload = _json_result(proc)
    assert payload["status"] == "UNHEALTHY"
    assert payload["reason"] == "gateway not running"


def test_linux_health_reads_proc_environ_and_linux_config_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = _write_config(home, "linux")
    fakebin = tmp_path / "fakebin"
    gateway_bin = tmp_path / "gateway-bin"
    proc_root = tmp_path / "proc"
    for d in (fakebin, gateway_bin, proc_root / "333"):
        d.mkdir(parents=True, exist_ok=True)
    rtk = _write_rtk(gateway_bin)
    (proc_root / "333" / "environ").write_bytes(
        b"USER=ace\0PATH=" + str(gateway_bin).encode() + b":/usr/bin\0OTHER=x\0"
    )
    _write_exe(
        fakebin / "systemctl",
        "#!/bin/sh\n"
        "if [ \"$1\" = --user ]; then shift; fi\n"
        "if [ \"$1\" = show ] && [ \"$3\" = --property=MainPID ] && [ \"$4\" = --value ]; then echo 333; exit 0; fi\n"
        "exit 4\n",
    )

    proc = _run_health(
        tmp_path,
        platform="linux",
        home=home,
        path=f"{fakebin}:/usr/bin:/bin",
        args=["--profile", "daedalus"],
        extra_env={"HERMES_COMPRESSION_HEALTH_PROC_ROOT": str(proc_root)},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = _json_result(proc)
    assert payload["status"] == "HEALTHY"
    assert payload["gateway_pid"] == 333
    assert payload["rtk_path"] == str(rtk)
    assert payload["config_path"] == str(config)


def test_health_probe_rewrites_but_does_not_execute_probe_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home, "darwin")
    fakebin = tmp_path / "fakebin"
    gateway_bin = tmp_path / "gateway-bin"
    for d in (fakebin, gateway_bin):
        d.mkdir()
    _write_rtk(gateway_bin)
    touchfile = tmp_path / "probe-created-this-file"
    _write_exe(
        fakebin / "launchctl",
        "#!/bin/sh\nprintf 'PID\\tStatus\\tLabel\\n222\\t0\\tai.hermes.gateway-daedalus\\n'\n",
    )
    _write_exe(
        fakebin / "ps",
        "#!/bin/sh\nprintf '  PID TT STAT TIME COMMAND\\n222 ?? S 0:00 hermes PATH=%s:/usr/bin OTHER=x\\n' '" + str(gateway_bin) + "'\n",
    )

    proc = _run_health(
        tmp_path,
        platform="darwin",
        home=home,
        path=f"{fakebin}:/usr/bin:/bin",
        args=["--profile", "daedalus", "--probe-command", f"touch {touchfile}"],
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not touchfile.exists()
    payload = _json_result(proc)
    assert payload["probe_command"] == f"touch {touchfile}"
    assert payload["probe_rewritten"] == f"rtk touch {touchfile}"


def test_cron_registration_is_no_agent_and_routes_only_unhealthy_loudly():
    data = json.loads(CRON_REGISTRATION.read_text(encoding="utf-8"))

    assert data["name"] == "compression-health"
    assert data["no_agent"] is True
    assert data["script"] == "compression-health.sh"
    assert data["deliver_unhealthy"] == "#alerts"
    assert data["deliver_healthy"] in {"#logs", "silent"}
    assert "--cron" in data["install_notes"]
