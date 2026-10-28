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
# A runner started by this plist: `<python> ... hermes_cli.main ... cron runner` with nothing after it.
_RUNNER_CMDLINE = r"hermes_cli[.]main.* cron runner$"


class SourceMissing(Exception):
    pass


def account_home() -> Path:
    """Real account home (profile mode may repoint HOME)."""
    import pwd
    return Path(pwd.getpwuid(_uid()).pw_dir)


def _uid() -> int:
    return os.getuid()  # windows-footgun: ok — launchd (macOS) only


def runner_python() -> str:
    from hermes_cli._launchers import resolve_store_python
    return str(resolve_store_python(PROJECT_ROOT) or sys.executable)


def agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def runner_plist_path(home: Path) -> Path:
    return agents_dir(home) / f"{LABEL}.plist"


def gateway_plist_path(home: Path) -> Path:
    return agents_dir(home) / f"{GATEWAY_LABEL}.plist"


def _read_env(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise SourceMissing(str(path))
    return dict(plistlib.loads(path.read_bytes()).get("EnvironmentVariables") or {})


def _hermes_home(home: Path, env: Dict[str, str]) -> Path:
    return Path(env.get("HERMES_HOME") or home / ".hermes")


def _program_arguments() -> List[str]:
    from hermes_cli._launchers import runtime_command
    return runtime_command(PROJECT_ROOT, ["cron", "runner"], python=runner_python())


def render_plist(home: Path) -> bytes:
    """The runner plist. Env copied verbatim from the root gateway plist; raises SourceMissing."""
    env = _read_env(gateway_plist_path(home))
    vhome = _hermes_home(home, env)
    log = str(vhome / "logs" / LOG_NAME)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _program_arguments(),
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
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=60, check=False)


def _runner_pids() -> List[int]:
    out = subprocess.run(["pgrep", "-f", _RUNNER_CMDLINE], capture_output=True, text=True,
                         timeout=10, check=False).stdout
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def install(*, dry_run: bool = False, launchctl: Callable = _run_launchctl) -> int:
    home = account_home()
    try:
        refused = _machine_id_source(home)
        body = render_plist(home)
    except SourceMissing as exc:
        print(f"cron runner install: source plist {exc} not found; nothing written")
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
    tmp.write_bytes(body)
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
    launchctl("bootout", f"gui/{_uid()}/{LABEL}")  # not loaded is fine
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
