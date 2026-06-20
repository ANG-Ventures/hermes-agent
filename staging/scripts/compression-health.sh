#!/usr/bin/env bash
set -euo pipefail

_python_bin="${PYTHON:-python3}"
exec "${_python_bin}" - "$@" <<'PY'
from __future__ import print_function

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ACCEPTED_REWRITE_RETURN_CODES = set([0, 3])
SCOPE_LIMIT = (
    "Detects missing/unresolvable rtk and no-rewrite probes under the gateway PATH; "
    "does not detect a present-but-corrupting rtk. Corruption is caught by per-command logs and the benchmark battery."
)


def _platform():
    forced = os.environ.get("HERMES_COMPRESSION_HEALTH_PLATFORM", "").strip().lower()
    if forced in ("darwin", "mac", "macos"):
        return "darwin"
    if forced in ("linux",):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _profile_from_env():
    for key in ("HERMES_PROFILE", "HERMES_ACTIVE_PROFILE", "HERMES_PROFILE_NAME"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "default"


def _run(argv, env=None, timeout=5):
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
        timeout=timeout,
    )


def _launchd_labels(profile):
    if profile in ("", "default", "apollo"):
        return ["ai.hermes.gateway"]
    return [
        "ai.hermes.gateway-" + profile,
        "gateway-" + profile,
    ]


def _systemd_units(profile):
    if profile in ("", "default", "apollo"):
        return ["hermes-gateway.service", "ai.hermes.gateway.service"]
    return [
        "hermes-gateway-" + profile + ".service",
        "hermes-gateway@" + profile + ".service",
        "ai.hermes.gateway-" + profile + ".service",
        "gateway-" + profile + ".service",
    ]


def _parse_launchctl_pid(output, label):
    # `launchctl list <label>` emits an old-style plist on modern macOS:
    #     "Label" = "ai.hermes.gateway-daedalus";
    #     "PID" = 65980;
    # Older/fake launchctl output may be the tabular `PID Status Label` form;
    # support both so tests and live probes use the same resolver.
    if ('"Label" = "' + label + '"') in output:
        match = re.search(r'"PID"\s*=\s*([0-9]+)\s*;', output)
        if match:
            try:
                pid = int(match.group(1))
            except ValueError:
                pid = 0
            if pid > 0:
                return pid
    for raw in output.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        if parts[-1] != label:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def resolve_gateway_pid(profile, platform):
    if platform == "darwin":
        for label in _launchd_labels(profile):
            try:
                result = _run(["launchctl", "list", label], timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            pid = _parse_launchctl_pid(result.stdout, label)
            if pid:
                return pid, label
        return None, None

    if platform == "linux":
        for unit in _systemd_units(profile):
            for prefix in (["systemctl", "--user"], ["systemctl"]):
                try:
                    result = _run(
                        prefix + ["show", unit, "--property=MainPID", "--value"],
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if result.returncode != 0:
                    continue
                try:
                    pid = int((result.stdout or "").strip())
                except ValueError:
                    continue
                if pid > 0:
                    scope = "user" if "--user" in prefix else "system"
                    return pid, scope + ":" + unit
        return None, None

    return None, None


def _extract_path_assignment(text):
    # PATH values do not contain whitespace; ps eww emits process argv plus KEY=VALUE tokens.
    match = re.search(r"(?:^|\s)PATH=([^\s]+)", text or "")
    if match:
        return match.group(1)
    return ""


def resolve_gateway_path(pid, platform):
    """Return PATH from the running gateway process, not this script's PATH."""
    if platform == "darwin":
        try:
            result = _run(["ps", "eww", "-p", str(pid)], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return _extract_path_assignment(result.stdout)

    if platform == "linux":
        proc_root = Path(os.environ.get("HERMES_COMPRESSION_HEALTH_PROC_ROOT", "/proc"))
        environ = proc_root / str(pid) / "environ"
        try:
            raw = environ.read_bytes()
        except OSError:
            return ""
        for item in raw.split(b"\0"):
            if item.startswith(b"PATH="):
                return item[5:].decode("utf-8", "replace")
        return ""

    return os.environ.get("PATH", "")


def _which_on_path(name, path_value):
    for entry in (path_value or "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def _home_candidates():
    candidates = []
    override = os.environ.get("HERMES_COMPRESSION_HEALTH_HOME", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    home_env = os.environ.get("HOME", "").strip()
    if home_env:
        candidates.append(Path(home_env).expanduser())
    try:
        import pwd

        candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser())
    except Exception:
        pass
    seen = set()
    out = []
    for path in candidates:
        key = str(path)
        if key and key not in seen:
            seen.add(key)
            out.append(path)
    return out or [Path.home()]


def _rtk_config_path_for_home(platform, home):
    if platform == "darwin":
        return home / "Library" / "Application Support" / "rtk" / "config.toml"
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home).expanduser() if config_home else home / ".config"
    return base / "rtk" / "config.toml"


def _check_config(platform):
    first_path = None
    for home in _home_candidates():
        path = _rtk_config_path_for_home(platform, home)
        if first_path is None:
            first_path = path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "[hooks]" not in text:
            return path, False, "rtk config missing hooks section"
        return path, True, ""
    return first_path or _rtk_config_path_for_home(platform, Path.home()), False, "rtk config not found"


def _probe_rtk(rtk_path, gateway_path, probe_command, timeout):
    env = os.environ.copy()
    env["PATH"] = gateway_path
    try:
        result = _run(["rtk", "rewrite", probe_command], env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, "rtk rewrite timed out", None
    except OSError as exc:
        return False, None, "rtk probe failed: " + str(exc), None

    rewritten = (result.stdout or "").strip()
    if result.returncode not in ACCEPTED_REWRITE_RETURN_CODES:
        detail = "rtk rewrite exited " + str(result.returncode)
        stderr = (result.stderr or "").strip()
        if stderr:
            detail += ": " + stderr
        return False, result.returncode, detail, rewritten
    if not rewritten or rewritten == probe_command:
        return False, result.returncode, "probe did not rewrite", rewritten
    return True, result.returncode, "", rewritten


def _base_payload(profile, platform, probe_command):
    return {
        "profile": profile,
        "platform": platform,
        "probe_command": probe_command,
        "scope_limit": SCOPE_LIMIT,
    }


def check_health(profile, probe_command, platform, timeout):
    payload = _base_payload(profile, platform, probe_command)
    pid, unit = resolve_gateway_pid(profile, platform)
    payload["gateway_unit"] = unit
    if not pid:
        payload.update({"status": "UNHEALTHY", "reason": "gateway not running"})
        return payload
    payload["gateway_pid"] = pid

    gateway_path = resolve_gateway_path(pid, platform)
    payload["gateway_path"] = gateway_path
    if not gateway_path:
        payload.update({"status": "UNHEALTHY", "reason": "gateway PATH unavailable"})
        return payload

    script_rtk = shutil.which("rtk")
    payload["script_path_has_rtk"] = bool(script_rtk)

    rtk_path = _which_on_path("rtk", gateway_path)
    if not rtk_path:
        payload.update({"status": "UNHEALTHY", "reason": "rtk not found on gateway PATH"})
        return payload
    payload["rtk_path"] = rtk_path

    config_path, config_ok, config_reason = _check_config(platform)
    payload["config_path"] = str(config_path)
    if not config_ok:
        payload.update({"status": "UNHEALTHY", "reason": config_reason})
        return payload

    ok, rc, reason, rewritten = _probe_rtk(rtk_path, gateway_path, probe_command, timeout)
    payload["probe_rc"] = rc
    payload["probe_rewritten"] = rewritten
    if not ok:
        payload.update({"status": "UNHEALTHY", "reason": reason})
        return payload

    payload.update({"status": "HEALTHY", "reason": "ok"})
    return payload


def _human(payload):
    status = payload.get("status", "UNKNOWN")
    bits = [
        status + " compression-health",
        "profile=" + str(payload.get("profile", "")),
        "reason=" + str(payload.get("reason", "")),
    ]
    if payload.get("gateway_pid"):
        bits.append("gateway_pid=" + str(payload.get("gateway_pid")))
    if payload.get("rtk_path"):
        bits.append("rtk=" + str(payload.get("rtk_path")))
    if payload.get("config_path"):
        bits.append("config=" + str(payload.get("config_path")))
    lines = [" ".join(bits)]
    if payload.get("probe_rewritten"):
        lines.append("probe_rewritten=" + str(payload.get("probe_rewritten")))
    lines.append("limit: " + SCOPE_LIMIT)
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description="Check live Hermes compression/rtk hook health.")
    parser.add_argument("--profile", default=_profile_from_env(), help="Hermes profile/gateway to check")
    parser.add_argument("--probe-command", default="git status", help="Read-only command string to pass to `rtk rewrite` (not executed)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--cron", action="store_true", help="Cron mode: healthy is silent, unhealthy is loud/non-zero")
    parser.add_argument("--quiet", action="store_true", help="Suppress healthy human output")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("HERMES_COMPRESSION_HEALTH_TIMEOUT", "2")), help="rtk probe timeout in seconds")
    args = parser.parse_args(argv)

    platform = _platform()
    payload = check_health(args.profile, args.probe_command, platform, args.timeout)
    healthy = payload.get("status") == "HEALTHY"

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif args.cron and healthy:
        # no_agent cron treats wakeAgent:false as a silent run.
        print("wakeAgent: false")
    elif args.quiet and healthy:
        pass
    else:
        print(_human(payload))

    return 0 if healthy else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
