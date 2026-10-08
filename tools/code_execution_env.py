"""Child-process environment for execute_code: env scrubbing, interpreter and cwd resolution.

Both the per-call remote path and the local session kernel build their child
env through ``_build_child_env`` so the security rules (secret scrubbing,
PYTHONPATH hygiene, UTF-8 forcing, TZ) cannot drift between them.
"""

import logging
import os
import platform
import re
import subprocess
import sys
from typing import Dict

# Logger name kept as the origin module's so existing log expectations hold.
logger = logging.getLogger("tools.code_execution_tool")

_IS_WINDOWS = platform.system() == "Windows"

# Scrub order: secret-substring block first; whatever is left must match a safe
# prefix, the exact-name HERMES_ allowlist, or (Windows) an OS-essential name.
# The broad "HERMES_" prefix is deliberately NOT safe — it leaked config vars
# without a secret substring (HERMES_BASE_URL, HERMES_KANBAN_DB, *_WEBHOOK).
# HERMES_RPC_SOCKET / HERMES_RPC_DIR / TZ / HOME are injected after scrubbing.
_SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP", "SHELL",
                      "LOGNAME", "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
# "PASS" is intentionally absent: it false-positives on BYPASS_CACHE /
# COMPASS_DIR / PASSENGER_HOST while PASSWORD/PASSWD already cover credentials.
_SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH", "DSN",
                      "WEBHOOK", "CREDS", "BEARER", "APIKEY")

# Non-secret runtime-location flags that repo-root modules a sandbox script
# imports may read at import time. HERMES_DELEGATED_CHILD_CONTEXT must ride
# along or a child that imports Hermes code loses the Kanban mutation guard
# while still inheriting HERMES_HOME.
_HERMES_CHILD_ALLOWED = frozenset({
    "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV", "HERMES_DELEGATED_CHILD_CONTEXT",
    # Agent-process marker ("true"): the gh shim resolves the lane from the
    # profile home only when it is present (t_45c11886).
    "HERMES_AGENT",
    # Kanban-worker kill switch for gateway service writes (hermes_cli/gateway_service_owner.py).
    "HERMES_GATEWAY_INSTALL_DISABLED",
})

# Git lane env from agent.process_env_files / gh-lane-env.sh (t_45c11886). The
# generic scrub drops all of it (GIT_CONFIG_KEY_* and GIT_AUTHOR_* hit the KEY /
# AUTH secret substrings), which sent sandbox git back to the global
# ``gh auth git-credential`` helper and the operator's identity. Identity names
# are not secrets; the GIT_CONFIG_* group passes only when EVERY key is a
# credential helper (a command, never a secret value) -- anything else (e.g. an
# http.extraheader carrying a token) drops the whole group.
_GIT_IDENTITY_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                      "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
_GIT_HELPER_KEY_RE = re.compile(r"^credential\.(?:\S+\.)?helper$")
# A helper VALUE passes only as empty (list reset) or an absolute helper path
# with plain word arguments (``!/path/gh auth git-credential``). Anything else
# (shell snippets, ``password=...``, inline tokens) drops the group (C3 #1254).
_GIT_HELPER_VALUE_RE = re.compile(r"^(?:!?/[\w./+-]+(?: [A-Za-z][A-Za-z-]*)*)?$")


def _carry_git_lane_env(source_env, scrubbed):
    for name in _GIT_IDENTITY_VARS:
        if source_env.get(name):
            scrubbed[name] = source_env[name]
    try:
        count = int(source_env.get("GIT_CONFIG_COUNT", ""))
    except ValueError:
        return scrubbed
    if count <= 0 or count > 32:
        return scrubbed
    group = {"GIT_CONFIG_COUNT": str(count)}
    for i in range(count):
        key = source_env.get(f"GIT_CONFIG_KEY_{i}")
        value = source_env.get(f"GIT_CONFIG_VALUE_{i}", "")
        if key is None or not _GIT_HELPER_KEY_RE.match(key) or not _GIT_HELPER_VALUE_RE.match(value):
            return scrubbed
        group[f"GIT_CONFIG_KEY_{i}"] = key
        group[f"GIT_CONFIG_VALUE_{i}"] = value
    scrubbed.update(group)
    return scrubbed

# Windows-only: without these the CRT itself fails — socket.socket() raises
# WinError 10106 (Winsock can't find mswsock.dll) and subprocess can't resolve
# cmd.exe. Well-known OS paths, not secrets; the substring block still runs.
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PUBLIC", "ALLUSERSPROFILE",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERDOMAIN", "USERNAME",
    "HOMEDRIVE", "HOMEPATH", "COMPUTERNAME",
})


def _scrub_child_env(source_env, is_passthrough=None, is_windows=None):
    """Produce the scrubbed child-process env for execute_code.

    Rules, in order: (1) passthrough vars (skill/config-declared) resolve
    through the active profile secret scope — an absent scoped value is
    omitted; (2) secret-substring names are blocked; (3) safe prefixes pass;
    (4) operational HERMES_* pass by exact name; (5) on Windows the
    OS-essential allowlist passes by exact name.
    """
    try:
        from tools.env_passthrough import is_env_passthrough, resolve_passthrough_value, scoped_passthrough_additions
    except Exception:
        is_env_passthrough = lambda _: False  # noqa: E731
        resolve_passthrough_value = lambda _name, _fallback: None  # noqa: E731
        scoped_passthrough_additions = lambda _present: {}  # noqa: E731
    if is_passthrough is None:
        is_passthrough = is_env_passthrough
    if is_windows is None:
        is_windows = _IS_WINDOWS
    scrubbed = {}
    # Non-secret HERMES_* vars no allowlist admits are dropped on purpose; a script importing a
    # repo module that reads one would see it silently unset — log the drop, point at the opt-in.
    _dropped_hermes = []
    for k, v in source_env.items():
        if is_passthrough(k):
            resolved = resolve_passthrough_value(k, v)
            if resolved is not None:
                scrubbed[k] = resolved
            continue
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue
        if (any(k.startswith(p) for p in _SAFE_ENV_PREFIXES)
                or k in _HERMES_CHILD_ALLOWED
                or (is_windows and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS)):
            scrubbed[k] = v
        elif k.startswith("HERMES_"):
            _dropped_hermes.append(k)
    # Declared names only the bound profile scope holds (a routed profile's own .env / sources
    # never enter the process env) — the loop above sees only names ``source_env`` carries.
    scrubbed.update((k, v) for k, v in scoped_passthrough_additions(scrubbed).items() if is_passthrough(k))
    if _dropped_hermes:
        logger.debug(
            "execute_code: dropped %d non-allowlisted HERMES_* var(s) from the "
            "sandbox child env (%s). This is intentional hardening (#27303); if "
            "a sandbox script legitimately needs one, declare it via "
            "env_passthrough in the skill/config so it passes by explicit opt-in.",
            len(_dropped_hermes), ", ".join(sorted(_dropped_hermes)),
        )

    # The git lane in os.environ belongs to the gateway's LAUNCH profile. A
    # multiplexed secondary profile (context-local home) must not inherit its
    # identity/credential helper or its home: point the child at the active
    # profile's home and carry no lane (C3 #1254).
    try:
        from hermes_constants import get_hermes_home_override
        _active_home = get_hermes_home_override()
    except Exception:
        _active_home = None
    if _active_home and os.path.realpath(_active_home) != os.path.realpath(
            source_env.get("HERMES_HOME") or os.devnull):
        scrubbed["HERMES_HOME"] = _active_home
    else:
        _carry_git_lane_env(source_env, scrubbed)

    # delegate_task children are marked by a ContextVar, not os.environ, and the sandbox crosses
    # a process boundary: strip dispatcher-owned Kanban vars AFTER the scrub so an explicit
    # passthrough cannot re-grant a delegated child the parent's board mutation capability.
    from agent.delegation_context import (
        DELEGATED_CHILD_ENV_MARKER, delegated_child_subprocess_env,
    )
    scoped = delegated_child_subprocess_env(source_env)
    # Preserve location only when carrying the descendant fence, not for arbitrary
    # non-allowlisted HERMES_* values in otherwise ordinary execution environments.
    if scoped.get(DELEGATED_CHILD_ENV_MARKER):
        for key in (DELEGATED_CHILD_ENV_MARKER, "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
            if key in scoped:
                scrubbed[key] = scoped[key]
    scrubbed = delegated_child_subprocess_env(scrubbed)
    _inject_session_context(scrubbed, source_env)
    _inject_session_id(scrubbed, source_env)
    return scrubbed


def _inject_session_context(scrubbed, source_env):
    """Give the sandbox child the same ``HERMES_SESSION_*`` set the terminal tool exports.

    The scrub above drops every one of them (``HERMES_SESSION_KEY`` even trips the ``KEY``
    secret substring), so a ``hermes kanban create`` run from execute_code had no
    ``HERMES_SESSION_PLATFORM``/``CHAT_ID`` and could not subscribe the gateway chat: the card
    finished silently (t_be44b437). Seed the names from *source_env* exactly as the terminal's
    ``os.environ`` base does, then apply the terminal's own bridge, so ContextVars win and an
    unbound var is stripped once a session context is engaged (cross-session leak guard).
    ``_inject_session_id`` runs after this and keeps the final say on ``HERMES_SESSION_ID``."""
    from gateway.session_context import _VAR_MAP, bridge_session_env
    for name in _VAR_MAP:
        if name in source_env:
            scrubbed[name] = source_env[name]
    return bridge_session_env(scrubbed)


def _session_identity_env(source_env=None) -> Dict[str, str]:
    """EVERY ``HERMES_SESSION_*`` name for the current turn, ``""`` where unbound/cleared.

    For children that outlive or never saw the spawn-time env: the session kernel (local and
    remote) applies it at each cell boundary (``RUNNER_CELL_SOURCE`` sets non-empty values and
    pops empty ones), and the remote per-call env file ships it as-is. Absent names are sent as
    ``""`` rather than omitted so a prior turn's identity is cleared, not inherited. Same
    bridge + session-id resolver as ``_scrub_child_env``."""
    from gateway.session_context import _VAR_MAP
    src = os.environ if source_env is None else source_env
    identity = _inject_session_id(_inject_session_context({}, src), src)
    return {name: identity.get(name) or "" for name in _VAR_MAP}


def _inject_session_id(scrubbed, source_env):
    """Fork (#636/C3): bridge the live ``HERMES_SESSION_ID`` into the sandbox child's env.

    A per-turn identity, not user shell state, so deliberately NOT in ``_HERMES_CHILD_ALLOWED`` —
    passing it by exact name would copy it out of ``os.environ``, the wrong source inside the gateway
    (last-writer-wins across concurrent sessions). Sandbox scripts shell out to ``hermes kanban comment``
    etc., which resolve provenance from their own env; without this bridge those writes land with NULL
    provenance while the same in-process tool call is attributed correctly. Fail-open: unresolvable ⇒ the
    var is REMOVED (an inherited process-global would be exactly the foreign-identity leak). The resolver
    stays on the facade so tests/callers patch ``tools.code_execution_tool._resolved_session_id``."""
    from tools.code_execution_tool import _resolved_session_id
    resolved = _resolved_session_id(source_env)
    if resolved:
        scrubbed["HERMES_SESSION_ID"] = resolved
    else:
        scrubbed.pop("HERMES_SESSION_ID", None)
    return scrubbed


def _build_child_env(*, rpc_endpoint: str, rpc_token: str, tmpdir: str,
                     child_python: str) -> Dict[str, str]:
    """Build the scrubbed child environment both execution paths share."""
    from hermes_constants import apply_scratch_tmp_env, apply_subprocess_home_env, get_hermes_home_override
    child_env = _scrub_child_env(os.environ)
    child_env["HERMES_RPC_SOCKET"] = rpc_endpoint
    child_env["HERMES_RPC_TOKEN"] = rpc_token
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Force UTF-8 stdio and default file encoding: on Windows sys.stdout is bound to the console
    # code page (cp1252) and print("→") raises; harmless under a C/POSIX locale (containers).
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    # Only TZ reaches the child; HERMES_TIMEZONE is an internal setting (and under the multiplexed
    # gateway holds only the default profile's value — hermes_time resolves the routed profile's).
    from hermes_time import get_timezone_name

    _tz_name = get_timezone_name()
    # Windows CPython does not support IANA names in TZ.  Leaving TZ unset
    # preserves the OS-configured local timezone for the child process.
    if _tz_name and not _IS_WINDOWS:
        child_env["TZ"] = _tz_name
    child_env.pop("HERMES_TIMEZONE", None)
    apply_subprocess_home_env(child_env)
    # Multiplexed gateway/Desktop (#110303): the server process env carries the machine-default
    # HERMES_HOME, but this turn runs under a per-profile override (ContextVar bound per turn).
    # The scrub above passed the stale default through; rewrite it so skill scripts see the
    # active profile's home — the same per-turn rewrite apply_subprocess_home_env does for HOME.
    # No override (dedicated per-profile process) → leave the inherited value untouched.
    _home_override = get_hermes_home_override()
    if _home_override:
        child_env["HERMES_HOME"] = _home_override
        apply_scratch_tmp_env(child_env)  # TMPDIR follows the routed home, like HOME does
    # Per-session scratch dir, keyed on the bridged session id — same export as the terminal.
    from hermes_constants import apply_session_scratch_env
    apply_session_scratch_env(child_env)
    # PYTHONPATH: the staging dir (hermes_tools.py) must always be importable even when project
    # mode changes CWD. Hermes's root is added ONLY when the child runs in Hermes's Python env —
    # exposing Hermes's site-packages to an external interpreter can mix incompatible compiled
    # extensions (3.12 NumPy under a 3.9 venv). Inherited Hermes-owned entries are stripped first.
    # Before re-injecting PYTHONPATH, strip Hermes-owned entries that leaked through _scrub_child_env
    # (PYTHONPATH is in _SAFE_ENV_PREFIXES so it passes the scrub). External project interpreters
    # must not inherit Hermes dependencies (#74817). PM's own interpreter, however, can be a
    # bare bundled Python whose dependencies live in the selected generation, not sys.prefix.
    from tools.environments.local_pythonpath import (
        _strip_hermes_owned_pythonpath, _validated_runtime_venv, _same_path,
    )
    _runtime_path = None
    if child_python == sys.executable:
        runtime_venv = _validated_runtime_venv(child_env)
        if runtime_venv is not None:
            from pathlib import Path
            from pm.environments import site_packages
            candidate = site_packages(runtime_venv)
            # Restore only a dependency path the launcher actually supplied, not a newly
            # selected generation that this still-running interpreter has never loaded.
            if any(_same_path(Path(entry), candidate)
                   for entry in child_env.get("PYTHONPATH", "").split(os.pathsep) if entry):
                _runtime_path = str(candidate)
    _strip_hermes_owned_pythonpath(child_env)
    _existing_pp = child_env.get("PYTHONPATH", "")
    _pp_parts = [tmpdir]
    if _runtime_path is not None:
        _pp_parts.append(_runtime_path)
    if _uses_hermes_python_environment(child_python):
        _pp_parts.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    elif child_python not in _external_env_logged:
        # Surface once per interpreter so "import hermes_constants fails" is diagnosable.
        _external_env_logged.add(child_python)
        logger.info("execute_code: child interpreter %s is outside the Hermes "
                    "environment; hermes root omitted from PYTHONPATH", child_python)
    if _existing_pp:
        _pp_parts.append(_existing_pp)
    child_env["PYTHONPATH"] = os.pathsep.join(_pp_parts)
    return child_env


# Interpreter-probe caches: success-only dicts (FIFO-evicted at the cap) rather than lru_cache —
# a transient probe failure (fork pressure, 5s timeout) must not stick for the process lifetime.
_PROBE_CACHE_MAX = 32
_usable_python_cache: dict = {}
_python_prefix_cache: dict = {}

# Interpreter paths already reported as outside the Hermes environment.
_external_env_logged: set = set()


def _cache_probe_result(cache: dict, key: str, value):
    """Insert into a bounded probe cache, FIFO-evicting at the cap."""
    if len(cache) >= _PROBE_CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _probe_python(python_path: str, code: str, *, text: bool = False):
    """Run ``python_path -c code``; None if missing, unspawnable, or past the 5s timeout."""
    try:
        from agent.delegation_context import delegated_child_subprocess_env
        return subprocess.run(
            [python_path, "-c", code], timeout=5, capture_output=True, text=text,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            stdin=subprocess.DEVNULL, env=delegated_child_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def _is_usable_python(python_path: str) -> bool:
    """Whether the interpreter is Python 3.8+ (what the RPC stubs need); success cached, failure retried."""
    cached = _usable_python_cache.get(python_path)
    if cached is not None:
        return cached
    result = _probe_python(python_path, "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)")
    if result is None:
        return False
    usable = result.returncode == 0
    _cache_probe_result(_usable_python_cache, python_path, usable)
    return usable


def _python_environment_prefix(python_path: str) -> str:
    """Resolved ``sys.prefix`` reported by *python_path* ("" on failure; failures are not cached)."""
    cached = _python_prefix_cache.get(python_path)
    if cached is not None:
        return cached
    result = _probe_python(python_path, "import sys; print(sys.prefix)", text=True)
    if result is not None and result.returncode == 0 and result.stdout.strip():
        prefix = os.path.realpath(result.stdout.strip())
        _cache_probe_result(_python_prefix_cache, python_path, prefix)
        return prefix
    return ""


def _uses_hermes_python_environment(python_path: str) -> bool:
    """Whether *python_path* belongs to Hermes's active Python environment. Short-circuits when
    it IS the running interpreter (by path or realpath — covers ``uv run`` venvs) so no probe
    runs on the default strict path and a flaky probe can never drop the hermes root."""
    if python_path == sys.executable or os.path.realpath(python_path) == os.path.realpath(sys.executable):
        return True
    return _python_environment_prefix(python_path) == os.path.realpath(sys.prefix)


def _resolve_child_python(mode: str) -> str:
    """Child interpreter: ``sys.executable`` in strict mode; in project mode the active
    VIRTUAL_ENV/CONDA_PREFIX python if it exists and passes the 3.8+ probe, else ``sys.executable``."""
    if mode != "project":
        return sys.executable
    subdir, exe_names = ("Scripts", ("python.exe", "python3.exe")) if _IS_WINDOWS else ("bin", ("python", "python3"))
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var, "").strip()
        for exe in exe_names if root else ():
            candidate = os.path.join(root, subdir, exe)
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                continue
            if _is_usable_python(candidate):
                return candidate
            logger.info("execute_code: skipping %s=%s (Python version < 3.8 or broken). "
                        "Using sys.executable instead.", var, candidate)
            return sys.executable
    return sys.executable


def _resolve_child_cwd(mode: str, staging_dir: str, task_id: str = "") -> str:
    """Child cwd. Strict: the staging dir. Project mirrors the terminal/file-tool ladder so every
    file-writing path agrees: session cwd record (`cd` state) → registered ``session.cwd.set``
    override → TERMINAL_CWD → os.getcwd() → staging dir (never Popen on a missing cwd).

    (#56047)
    """
    if mode != "project":
        return staging_dir
    if task_id:
        try:
            from tools.terminal_tool import get_session_cwd
            recorded = get_session_cwd(task_id)
        except Exception:
            recorded = None
        if recorded and os.path.isdir(recorded):
            return recorded
        try:
            from tools.file_tools_paths import _registered_task_cwd_override
            session_cwd = _registered_task_cwd_override(task_id)
        except Exception:
            session_cwd = None
        if session_cwd and os.path.isdir(session_cwd):
            return session_cwd
    from agent.runtime_cwd import scope_terminal_cwd
    raw = scope_terminal_cwd().strip()
    for candidate in (os.path.expanduser(raw) if raw else "", os.getcwd()):
        if candidate and os.path.isdir(candidate):
            return candidate
    return staging_dir
