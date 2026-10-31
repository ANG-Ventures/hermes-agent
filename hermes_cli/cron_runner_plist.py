"""LaunchAgent for the cron script runner (spec §5.7, slice S5).

``hermes cron runner install [--dry-run]`` writes
``~/Library/LaunchAgents/ai.hermes.cron-script-runner.plist`` and bootstraps it into ``gui/<uid>``;
``uninstall`` boots it out, removes the plist and confirms no runner process is left; ``lint`` fails
when the runner plist's ``EnvironmentVariables`` differ from ``ai.hermes.gateway.plist``'s (``XPC_*``
keys are launchd's own and are ignored).

Spec §12: ``uninstall`` runs BEFORE any S1/S2/S3 revert, so this module imports nothing from
``cron/`` (pinned by a test). It never edits the gateway plist or ``config.yaml``.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LABEL = "ai.hermes.cron-script-runner"
GATEWAY_LABEL = "ai.hermes.gateway"
LOG_NAME = "cron-script-runner.log"
EXIT_CONFIG = 78  # same refusal code as the runner (I6)
EXIT_SOURCE_MISSING = 2
REFUSED_KEY = "HERMES_MACHINE_ID"
_XPC = re.compile(r"^XPC_")
# A runner started by this plist: `<python> ... hermes_cli.main ... cron runner` with nothing after it,
# or `hermes cron runner` once main._set_process_title() rewrote argv via setproctitle.
_RUNNER_CMDLINE = r"(hermes_cli[.]main.*|^hermes) cron runner$"
# launchctl bootout of a label that is not loaded: 3 "No such process", 113 "Could not find service".
_BOOTOUT_NOT_LOADED = (3, 113)


class SourceMissing(Exception):
    pass


def account_home() -> Path:
    """Real account home (profile mode may repoint HOME)."""
    import pwd
    return Path(pwd.getpwuid(_uid()).pw_dir)


def _uid() -> int:
    return os.getuid()  # windows-footgun: ok — launchd (macOS) only


def agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def runner_plist_path(home: Path) -> Path:
    return agents_dir(home) / f"{LABEL}.plist"


def gateway_plist_path(home: Path) -> Path:
    return agents_dir(home) / f"{GATEWAY_LABEL}.plist"


def _read_plist(path: Path) -> dict:
    if not path.is_file():
        raise SourceMissing(str(path))
    return plistlib.loads(path.read_bytes())


def _read_env(path: Path) -> Dict[str, str]:
    return dict(_read_plist(path).get("EnvironmentVariables") or {})


def _hermes_home(home: Path, env: Dict[str, str]) -> Path:
    return Path(env.get("HERMES_HOME") or home / ".hermes")


def _program_arguments(gateway: dict, path: Path) -> List[str]:
    """Spec §5.7: ``[<runtime venv python>, -m, hermes_cli.main, cron, runner]``, the python being the
    gateway plist's own argv[0], so both processes import through one interpreter (§5.6)."""
    argv = list(gateway.get("ProgramArguments") or [])
    if len(argv) < 3 or argv[1:3] != ["-m", "hermes_cli.main"]:
        raise SourceMissing(f"{path} (ProgramArguments is not `<python> -m hermes_cli.main ...`)")
    return [argv[0], "-m", "hermes_cli.main", "cron", "runner"]


def render_plist(home: Path) -> bytes:
    """The runner plist. Env, python and session type copied from the root gateway plist;
    raises SourceMissing."""
    gw_path = gateway_plist_path(home)
    gateway = _read_plist(gw_path)
    env = dict(gateway.get("EnvironmentVariables") or {})
    vhome = _hermes_home(home, env)
    log = str(vhome / "logs" / LOG_NAME)
    session = {k: gateway[k] for k in ("LimitLoadToSessionType",) if k in gateway}
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _program_arguments(gateway, gw_path),
        "EnvironmentVariables": env,
        "WorkingDirectory": str(vhome),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ExitTimeOut": 30,
        "SoftResourceLimits": {"NumberOfFiles": 4096},
        "HardResourceLimits": {"NumberOfFiles": 8192},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        **session,  # same launchd session type as the gateway (Local Network grant)
    })


def _dotenv_has(path: Path, key: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line.startswith("#") and line.partition("=")[0].strip() == key:
            return True
    return False


def _machine_id_source(home: Path) -> Optional[str]:
    """Where HERMES_MACHINE_ID would reach the runner (I6), or None."""
    if REFUSED_KEY in os.environ:
        return "this process's environment"
    env = _read_env(gateway_plist_path(home))
    if REFUSED_KEY in env:
        return str(gateway_plist_path(home))
    dotenv = _hermes_home(home, env) / ".env"
    if _dotenv_has(dotenv, REFUSED_KEY):
        return str(dotenv)
    return None


def _run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, check=False)


def _runner_pids() -> List[int]:
    out = subprocess.run(["pgrep", "-f", _RUNNER_CMDLINE], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=10, check=False).stdout
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def install(*, dry_run: bool = False, launchctl: Callable = _run_launchctl) -> int:
    home = account_home()
    try:
        refused = _machine_id_source(home)
        body = render_plist(home)
    except SourceMissing as exc:
        print(f"cron runner install: source plist {exc} missing or unusable; nothing written")
        return EXIT_SOURCE_MISSING
    if refused:
        print(f"cron runner install: refusing, {REFUSED_KEY} is set in {refused} "
              "(I6: no shared claim identity); nothing written")
        return EXIT_CONFIG
    if dry_run:
        sys.stdout.write(body.decode())
        return 0
    path = runner_plist_path(home)
    env = _read_env(gateway_plist_path(home))
    (_hermes_home(home, env) / "logs").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{_uid()}"
    launchctl("bootout", f"{domain}/{LABEL}")  # re-install: unload the old definition first
    tmp = path.with_suffix(".plist.tmp")
    # 0600 regardless of umask: the env copied from the gateway plist may hold credentials.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)  # a stale tmp keeps its old mode through O_CREAT
        fh.write(body)
    tmp.replace(path)
    res = launchctl("bootstrap", domain, str(path))
    if res.returncode != 0:
        print(f"cron runner install: wrote {path}; launchctl bootstrap {domain} failed "
              f"(rc {res.returncode}): {(res.stderr or '').strip()}")
        return 1
    print(f"cron runner install: {path} loaded into {domain} as {LABEL}")
    return 0


def uninstall(*, launchctl: Callable = _run_launchctl, pgrep: Callable = _runner_pids,
              wait_s: float = 35.0) -> int:
    """Boot out, remove the plist, confirm no runner process remains (spec §12 step 2)."""
    home = account_home()
    path = runner_plist_path(home)
    res = launchctl("bootout", f"gui/{_uid()}/{LABEL}")
    if res.returncode not in _BOOTOUT_NOT_LOADED and res.returncode != 0:
        # The job may still be registered; KeepAlive would respawn it. Keep the plist for a retry.
        print(f"cron runner uninstall: launchctl bootout failed (rc {res.returncode}): "
              f"{(res.stderr or '').strip()}; plist left in place")
        return 1
    if path.exists():
        path.unlink()
        print(f"cron runner uninstall: removed {path}")
    deadline = time.monotonic() + wait_s  # ExitTimeOut is 30 s
    while True:
        pids = pgrep()
        if not pids or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if pids:
        print(f"cron runner uninstall: runner process still alive: pid(s) {', '.join(map(str, pids))}")
        return 1
    print("cron runner uninstall: no runner process left")
    return 0


def lint() -> int:
    """0 = runner env equals gateway env (ignoring XPC_*); 1 = differs; 2 = a plist is missing."""
    home = account_home()
    try:
        gw = _read_env(gateway_plist_path(home))
        rn = _read_env(runner_plist_path(home))
    except SourceMissing as exc:
        print(f"cron runner lint: {exc} not found")
        return 2
    gw = {k: v for k, v in gw.items() if not _XPC.match(k)}
    rn = {k: v for k, v in rn.items() if not _XPC.match(k)}
    diff = sorted(k for k in gw.keys() | rn.keys() if gw.get(k) != rn.get(k))
    if diff:
        print(f"cron runner lint: EnvironmentVariables differ from {GATEWAY_LABEL}: {', '.join(diff)}; "
              "run 'hermes cron runner install' to re-copy")
        return 1
    print("cron runner lint: env matches the gateway plist")
    return 0
