"""Windows essentials and the native Winsock control.

Actual production UTF-8 env and Unicode RPC are exercised in
`test_code_execution_modes.py`; never reproduce the production scrubber here.
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from tools.code_execution_env import _scrub_child_env
from tools import code_execution_env


def _no_passthrough(_):
    return False


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("passthrough", [False, True])
def test_native_essentials_and_passthrough_priority(windows, passthrough):
    # Platform is explicit input to this pure policy helper, not a fake host.
    essentials = {
        "SYSTEMROOT": r"C:\Windows", "SystemRoot": r"C:\Windows",
        "SystemDrive": "C:", "WINDIR": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe", "comspec": r"C:\Windows\System32\cmd.exe",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    }
    safe = {"PATH": r"C:\Windows\System32;C:\Python", "HOME": r"C:\Users\alice",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD;.PY",
            "USERPROFILE": r"C:\Users\alice", "TEMP": r"C:\Users\alice\Temp"}
    secret = {"OPENAI_API_KEY": "fake-provider", "GITHUB_TOKEN": "fake-github",
              "MY_PASSWORD": "fake-password", "TENOR_API_KEY": "fake-third-party",
              "RANDOM_UNKNOWN_VAR": "unknown"}
    result = _scrub_child_env({**essentials, **safe, **secret}, is_windows=windows,
                             is_passthrough=lambda k: passthrough and k == "TENOR_API_KEY")
    assert result == {**safe, **(essentials if windows else {}),
                      **({"TENOR_API_KEY": "fake-third-party"} if passthrough else {})}


# ``platforms("windows")`` rather than ``skipif(sys.platform != "win32")``: the
# dedicated Windows CI job selects its files by grepping for the marker, so a
# bare skipif is invisible to it — the file is never imported there and these
# tests run on no host at all.
@pytest.mark.platforms("windows")
class TestWindowsSocketSmokeTest:
    """Integration-ish smoke test: spawn a child Python with a scrubbed
    env and confirm it can create an AF_INET socket.  This is the
    regression that motivated the fix — without SYSTEMROOT the child
    hits WinError 10106 before any RPC is attempted."""

    def test_child_can_create_socket_with_scrubbed_env(self):
        scrubbed = _scrub_child_env(os.environ, is_passthrough=_no_passthrough)

        # Build a tiny child script that simply opens an AF_INET socket.
        script = textwrap.dedent("""
            import socket, sys
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
                print("OK")
                sys.exit(0)
            except OSError as exc:
                print(f"FAIL: {exc}")
                sys.exit(1)
        """).strip()

        result = subprocess.run(
            [sys.executable, "-c", script],
            env=scrubbed,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"Child failed to create socket with scrubbed env:\n"
            f"  stdout={result.stdout!r}\n"
            f"  stderr={result.stderr!r}\n"
            f"  scrubbed keys={sorted(scrubbed.keys())}"
        )
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# POSIX equivalence guard
# ---------------------------------------------------------------------------

def _legacy_posix_scrubber(source_env, is_passthrough):
    """Independent oracle for TestPosixEquivalence — a from-scratch reimpl of
    _scrub_child_env's POSIX behavior, used to prove the production helper does
    what we think it does.

    Deliberately updated for #27303 (the broad ``HERMES_`` prefix was dropped
    in favor of an explicit operational allowlist, and DSN/WEBHOOK were added
    to the secret substrings).  The original docstring said: if POSIX behavior
    legitimately needs to evolve, adjust this oracle on purpose so the churn is
    visible in review — that is what this change is.

    Parity sync 2026-10-01: upstream moved the helper to
    ``tools/code_execution_env.py`` and widened it (CREDS/BEARER/APIKEY secret
    substrings, ``HERMES_DELEGATED_CHILD_CONTEXT`` allowlisted); the oracle
    follows.  ``HERMES_AGENT`` is the fork's agent marker (t_45c11886).
    """
    _SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                          "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                          "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
    _SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                          "PASSWD", "AUTH", "DSN", "WEBHOOK", "CREDS", "BEARER",
                          "APIKEY")
    _HERMES_CHILD_ALLOWED = frozenset({
        "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_AGENT",  # agent marker for the gh shim (t_45c11886)
    })
    out = {}
    for k, v in source_env.items():
        if is_passthrough(k):
            out[k] = v
            continue
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue
        if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES):
            out[k] = v
            continue
        if k in _HERMES_CHILD_ALLOWED:
            out[k] = v
    return out


def _configured_timezone_child_env():
    return code_execution_env._build_child_env(
        rpc_endpoint="socket",
        rpc_token="token",
        tmpdir="/tmp/hermes-code-execution-test",
        child_python=sys.executable,
    )


class TestPosixEquivalence:
    """Lock in the invariant that _scrub_child_env(env, is_windows=False)
    behaves *bit-for-bit identically* to the pre-refactor inline scrubber.

    If this ever fails, it means somebody changed POSIX env-scrubbing
    behavior — maybe on purpose, maybe not.  Either way it should land
    as a deliberate, reviewed change (update _legacy_posix_scrubber
    above in the same PR).

    Rationale: the Windows-essentials patch refactored the scrubber into
    a helper.  Linux/macOS must not regress.  This class gates that.
    """

    _POSIX_SYNTHETIC_ENV = {
        # Safe-prefix matches
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/alice",
        "USER": "alice",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": "/bin/zsh",
        "LOGNAME": "alice",
        "TMPDIR": "/tmp",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "XDG_CONFIG_HOME": "/home/alice/.config",
        "PYTHONPATH": "/opt/lib",
        "VIRTUAL_ENV": "/home/alice/.venv",
        "CONDA_PREFIX": "/opt/conda",
        # HERMES_* handling (#27303): only the operational allowlist passes;
        # every other HERMES_* is dropped (the broad prefix was removed).
        "HERMES_HOME": "/home/alice/.hermes",        # allowlisted → kept
        "HERMES_PROFILE": "default",                 # allowlisted → kept
        "HERMES_AGENT": "true",                    # agent marker -> kept (t_45c11886)
        "HERMES_INTERACTIVE": "1",                   # not allowlisted → dropped
        "HERMES_BASE_URL": "https://api.internal",   # not allowlisted → dropped
        "HERMES_KANBAN_DB": "postgres://u:p@h/db",   # not allowlisted → dropped
        # Secret-substring blocks
        "OPENAI_API_KEY": "sk-xxx",
        "GITHUB_TOKEN": "ghp_xxx",
        "AWS_SECRET_ACCESS_KEY": "yyy",
        "MY_PASSWORD": "hunter2",
        "SENTRY_DSN": "https://abc@sentry.io/1",     # DSN substring → blocked
        "SLACK_WEBHOOK": "https://hooks.slack/x",    # WEBHOOK substring → blocked
        # Uncategorized — must be dropped
        "RANDOM_UNKNOWN": "drop-me",
        "DISPLAY": ":0",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent",
        # Passthrough candidate (also matches secret block by default)
        "TENOR_API_KEY": "tenor-xxx",
    }

    _WINDOWS_SYNTHETIC_ENV = {
        # Windows-essential names (must be dropped on POSIX, passed on Win)
        "SYSTEMROOT": r"C:\Windows",
        "SystemDrive": "C:",
        "WINDIR": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT",
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        # Safe-prefix matches (cross-platform)
        "PATH": r"C:\Python311;C:\Windows\System32",
        "HOME": r"C:\Users\alice",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
        # Secret-looking (always blocked)
        "OPENAI_API_KEY": "sk-xxx",
        "GITHUB_TOKEN": "ghp_xxx",
    }

    @pytest.mark.parametrize("env_name,env", [
        ("posix_synthetic", _POSIX_SYNTHETIC_ENV),
        ("windows_synthetic_on_posix", _WINDOWS_SYNTHETIC_ENV),
    ])
    @pytest.mark.parametrize("pt_name,pt", [
        ("no_passthrough", lambda _: False),
        ("tenor_passthrough", lambda k: k == "TENOR_API_KEY"),
        ("all_passthrough", lambda _: True),
    ])
    def test_posix_behavior_unchanged(self, env_name, env, pt_name, pt):
        """For every combination of (env shape × passthrough rule), the
        new helper with is_windows=False must produce the exact same dict
        as the legacy inline scrubber.

        We parametrize over three passthrough rules to cover the full
        surface: no passthrough, single-var passthrough (the common
        skill-registered case), and everything-passes (edge case that
        could expose precedence bugs)."""
        expected = _legacy_posix_scrubber(env, pt)
        actual = _scrub_child_env(env, is_passthrough=pt, is_windows=False)
        assert actual == expected, (
            f"POSIX behavior regressed for env={env_name}, passthrough={pt_name}\n"
            f"  only in legacy: {sorted(set(expected) - set(actual))}\n"
            f"  only in new:    {sorted(set(actual) - set(expected))}\n"
            f"  value diffs:    {[k for k in expected if k in actual and expected[k] != actual[k]]}"
        )


@pytest.mark.platforms("windows")
def test_windows_live_child_offset_matches_os_zone_when_timezone_is_configured(monkeypatch):
    """The user-visible contract of #112233: with ``timezone:`` configured, a real Windows child
    must report the OS zone's UTC offset — an IANA name in ``TZ`` made the MSVC runtime derive
    ``time.timezone == 0`` (+01:00 instead of -07:00) while ``time.tzname`` still read correctly."""
    import json

    monkeypatch.setattr("hermes_time.get_timezone_name", lambda: "America/Los_Angeles")
    child_env = _configured_timezone_child_env()
    assert "TZ" not in child_env

    # The runner starts Python with TZ=UTC; its cached timezone is not the OS zone.
    # Query a fresh control process with TZ removed, independently of the builder.
    control_env = os.environ.copy()
    control_env.pop("TZ", None)
    timestamp = str(time.time())  # Both children observe the same instant across DST changes.
    script = (
        "import datetime, json, sys, time; "
        "instant = datetime.datetime.fromtimestamp(float(sys.argv[1]), datetime.timezone.utc); "
        "print(json.dumps([time.timezone, instant.astimezone().utcoffset().total_seconds()]))"
    )

    def read_timezone(env):
        result = subprocess.run(
            [sys.executable, "-c", script, timestamp],
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert read_timezone(child_env) == read_timezone(control_env)
