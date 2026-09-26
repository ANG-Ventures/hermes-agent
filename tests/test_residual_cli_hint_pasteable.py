"""A printed remedy must survive a real paste at the residual hint sites.

Residual of the #889/#891 unreachable-remedy class. Each site interpolated a
``HERMES_HOME``-derived path (or an interpreter/executable path resolved under
one) bare into a backticked span the operator is told to paste. For a home
holding a space -- Google Drive's ``My Drive``, or the Windows
``C:/Users/<First Last>`` default where a space is the NORM -- the printed
remedy splits into two words and the paste does something other than what the
message says.

THE PASTE SIMULATOR IS A REAL SHELL, and every print path here is the REAL
one: the message builder is driven with real values under a real spaced
``HERMES_HOME``, its output is CAPTURED, the backticked span is extracted from
that captured text, and the words come from ``/bin/bash``. Asserting on the
string's shape -- or simulating the paste with ``shlex`` while the
implementation decides safety with ``shlex`` -- is the tautology round 1 of the
grandparent card was blocked for.

The groups, each measured breaking before the fix (group 3, the staged
runtime-parity-check.py copy, was dropped with ``staging/``):

1. ``tools/self_repo_guard.py``   -- ``git clone --shared <root> <scratch>/<task>``
2. ``plugins/platforms/whatsapp/adapter.py`` -- ``cd <bridge> && <npm> install``
4. ``hermes_cli/doctor.py`` + ``gateway/run.py`` -- ``<sys.executable> -m pip ...``
5. ``gateway/run.py``             -- ``hermes skills install <path>``
6. ``hermes_cli/plugins_cmd.py``  -- ``hermes plugins install <source> ...``
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
import shutil as _shutil
import subprocess
import sys
import uuid
from contextlib import redirect_stdout
from pathlib import Path

import pytest

BASH = _shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"),
    reason="the paste oracle needs a real POSIX shell",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# the oracle
# --------------------------------------------------------------------------
def _bash_words(printed: str, cwd: str):
    """The argv a REAL bash produces for `printed`, or None if bash refuses.

    NUL-delimited so a word containing whitespace survives the round trip.
    """
    proc = subprocess.run(
        [str(BASH), "-c", 'printf "%s\\0" ' + printed],
        capture_output=True,
        cwd=cwd,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    return out.split("\0")[:-1] if out else []


def _span(message: str, needle: str) -> str:
    """The backticked command as the operator sees it on screen.

    rich hard-wraps at the console width, so the captured text is unwrapped
    before the span is extracted.
    """
    flat = " ".join(message.split())
    spans = [s for s in re.findall(r"`([^`]+)`", flat) if needle in s]
    assert spans, f"no {needle!r} span reached the screen:\n{message}"
    return spans[0]


def _spaced_home(tmp_path: Path) -> Path:
    """A HERMES_HOME holding a space, as Google Drive and Windows both give."""
    home = tmp_path / "My Drive" / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home


# --------------------------------------------------------------------------
# 1. tools/self_repo_guard.py -- the guard's whole job is routing the operator
#    somewhere safe, and #889 fixed only HALF of this span.
# --------------------------------------------------------------------------
@requires_bash
def test_self_repo_guard_clone_hint_pastes_as_two_paths(tmp_path, monkeypatch):
    from tools import self_repo_guard

    home = _spaced_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = tmp_path / "My Drive" / "hermes-agent"
    root.mkdir(parents=True)

    printed = _span(self_repo_guard._block_message("git commit", root), "git clone")
    printed = printed.replace("<task>", "t_123")
    words = _bash_words(printed, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        "git", "clone", "--shared",
        str(root),
        f"{self_repo_guard._scratch_dir_hint()}/t_123",
    ], f"printed {printed!r} produced {words!r}"


def test_self_repo_guard_keeps_the_readable_form_for_an_ordinary_path(
    tmp_path, monkeypatch
):
    """Over-fix guard: a path needing no escaping must not grow quotes.

    Only the ROOT is asserted bare. The scratch span is quoted even for an
    ordinary home because the ``<task>`` PLACEHOLDER is itself shell syntax
    (``<`` is a redirect), so there is no spelling of that span a real shell
    reads as a plain word -- the operator substitutes a task name into it
    either way.
    """
    from tools import self_repo_guard

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = tmp_path / "hermes-agent"
    root.mkdir()

    message = " ".join(self_repo_guard._block_message("git commit", root).split())
    assert f"`git clone --shared {root} " in message, message


# --------------------------------------------------------------------------
# 2. plugins/platforms/whatsapp/adapter.py -- `cd <dir> && <npm> install`.
#    This span is a COMPOUND COMMAND, not a word list, so the honest oracle
#    EXECUTES it and asks the callee what it actually received.
# --------------------------------------------------------------------------
@requires_bash
@pytest.mark.parametrize("arm", ["returncode", "exception"])
def test_whatsapp_npm_install_hint_pastes_as_one_dir_and_one_binary(
    tmp_path, monkeypatch, arm
):
    """Both reachable arms print the same remedy, so both must be gated.

    ``returncode`` is the ``install_result.returncode != 0`` branch;
    ``exception`` is the ``except Exception`` arm around the same subprocess
    (``TimeoutExpired``/``OSError`` -- ``npm`` never ran, or never finished).
    They carry BYTE-IDENTICAL spans, which is exactly why one behavioural test
    could not gate the other.
    """
    import plugins.platforms.whatsapp.adapter as wa
    from gateway.platforms.base import PlatformConfig

    home = _spaced_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    bridge = home / "scripts" / "whatsapp-bridge"
    bridge.mkdir(parents=True)
    (bridge / "package.json").write_text('{"name":"b"}', encoding="utf-8")
    bridge_js = bridge / "bridge.js"
    bridge_js.write_text("// bridge\n", encoding="utf-8")

    # A REAL npm under a path with a space -- the shape Windows ships portable
    # Node in ("C:/Program Files/..."). Under --probe it records the argv and
    # cwd bash actually handed it; otherwise it fails, which is what drives
    # the adapter to the remedy in the first place.
    argv_out = tmp_path / "npm-argv"
    npm_dir = home / "Program Files" / "node"
    npm_dir.mkdir(parents=True)
    npm = npm_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--probe" ]; then\n'
        f'  printf "%s\\0" "$@" > "{argv_out}"\n'
        f'  printf "CWD=%s\\0" "$PWD" >> "{argv_out}"\n'
        "  exit 0\n"
        "fi\n"
        "echo 'npm failed' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    monkeypatch.setattr(wa, "find_node_executable", lambda cmd: str(npm))
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)

    session = home / "whatsapp-session"
    session.mkdir(parents=True)
    (session / "creds.json").write_text("{}", encoding="utf-8")

    adapter = wa.WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={"bridge_script": str(bridge_js), "session_path": str(session)},
        )
    )

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        adapter,
        "_set_fatal_error",
        lambda code, message, *, retryable: captured.setdefault("message", message),
    )
    monkeypatch.setattr(
        adapter, "_acquire_platform_lock_async", _always_true_async
    )

    with redirect_stdout(io.StringIO()):
        if arm == "exception":
            # The `except Exception` arm: npm never produced a returncode.
            # Patched only across connect() so the oracle's own subprocess
            # calls below run against the real implementation.
            def _explode(*args, **kwargs):
                raise OSError("npm could not be executed")

            with monkeypatch.context() as patched:
                patched.setattr(subprocess, "run", _explode)
                connected = asyncio.run(adapter.connect())
        else:
            connected = asyncio.run(adapter.connect())

    assert connected is False
    assert "message" in captured, "the npm-install remedy site was never reached"

    printed = _span(captured["message"], "npm")
    proc = subprocess.run(
        [str(BASH), "-c", printed.replace(" install", " --probe")],
        capture_output=True,
        cwd=str(tmp_path),
    )

    assert proc.returncode == 0, (
        f"bash refused the printed remedy {printed!r}: "
        f"{proc.stderr.decode('utf-8', 'replace').strip()}"
    )
    fields = argv_out.read_text().split("\0")[:-1]
    assert fields == ["--probe", f"CWD={bridge}"], (
        f"printed {printed!r} reached npm as {fields!r}"
    )


async def _always_true_async(*args, **kwargs):
    return True


# --------------------------------------------------------------------------
# 4. {sys.executable} -- a venv under a spaced HERMES_HOME splits.
# --------------------------------------------------------------------------
@requires_bash
def test_certifi_repair_hint_pastes_as_one_interpreter(tmp_path, monkeypatch):
    from agent import ssl_guard
    from agent.errors import SSLConfigurationError
    from hermes_cli import doctor

    spaced_python = tmp_path / "My Drive" / "venv" / "bin" / "python"
    spaced_python.parent.mkdir(parents=True)
    spaced_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(spaced_python))

    def _broken(*args, **kwargs):
        raise SSLConfigurationError("cacert.pem missing")

    monkeypatch.setattr(ssl_guard, "verify_ca_bundle_with_fallback", _broken)

    issues: list[str] = []
    with redirect_stdout(io.StringIO()):
        doctor.check_certificates(should_fix=False, issues=issues)

    printed = _span(" ".join(issues), "pip install")
    words = _bash_words(printed, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        str(spaced_python), "-m", "pip", "install",
        "--force-reinstall", "certifi",
    ], f"printed {printed!r} produced {words!r}"


@requires_bash
def test_pynacl_install_hint_pastes_as_one_interpreter(tmp_path, monkeypatch):
    """Site 2 of the same group, in the gateway's voice-join failure path."""
    import gateway.run as gateway_run

    spaced_python = tmp_path / "My Drive" / "venv" / "bin" / "python"
    spaced_python.parent.mkdir(parents=True)
    spaced_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(spaced_python))

    source = Path(gateway_run.__file__).read_text(encoding="utf-8")
    literal = re.search(r"Install with: `([^`]*PyNaCl)`", source).group(1)
    printed = literal.replace("{hint_value(sys.executable)}",
                              gateway_run.hint_value(sys.executable))
    assert "{" not in printed, printed

    words = _bash_words(printed, str(tmp_path))
    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        str(spaced_python), "-m", "pip", "install", "PyNaCl",
    ], f"printed {printed!r} produced {words!r}"


# --------------------------------------------------------------------------
# 5. gateway/run.py -- `hermes skills install <path>`. The install path is
#    relative to HERMES_OPTIONAL_SKILLS, an operator-set env var.
# --------------------------------------------------------------------------
@requires_bash
def test_skill_install_hint_pastes_as_one_path(tmp_path, monkeypatch):
    from gateway.run import _check_unavailable_skill

    home = _spaced_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    optional = home / "optional-skills"
    skill = optional / "My Category" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_OPTIONAL_SKILLS", str(optional))

    message = _check_unavailable_skill("demo-skill")
    assert message, "the skill-install hint site was never reached"

    printed = _span(message, "skills install")
    words = _bash_words(printed, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        "hermes", "skills", "install", "official/My Category/demo-skill",
    ], f"printed {printed!r} produced {words!r}"


# --------------------------------------------------------------------------
# 6. hermes_cli/plugins_cmd.py -- `hermes plugins install <source> ...`. The
#    recorded source is a source SPEC (local path or URL), not a normalised
#    plugin id, so it is not covered by the [a-z0-9_-] argument that ruled the
#    other 57 residual sites out.
# --------------------------------------------------------------------------
SPACED_SOURCE_SUFFIX = "plugin checkouts/demo-plugin"
_PLACEHOLDER = "<40-character commit SHA>"
_SHA = "b" * 40


def _pin_a_spaced_source(home: Path, monkeypatch) -> str:
    """Record a pinned plugin whose source spec holds a space."""
    import hermes_cli.plugins_cmd as plugins_cmd

    monkeypatch.setenv("HERMES_HOME", str(home))
    source = f"file://{home}/{SPACED_SOURCE_SUFFIX}"

    plugins_dir = Path(plugins_cmd._plugins_dir())
    (plugins_dir / "demo-plugin").mkdir(parents=True, exist_ok=True)

    meta = Path(plugins_cmd._install_metadata_path())
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {"demo-plugin": {"pinned": True, "revision": _SHA, "source": source}}
        ),
        encoding="utf-8",
    )
    return source


def _pastable(message: str) -> str:
    """The span with the placeholder filled in, as the operator would paste it."""
    return _span(message, "plugins install").replace(_PLACEHOLDER, _SHA)


@requires_bash
def test_pinned_plugin_cli_hint_pastes_as_one_source(tmp_path, monkeypatch):
    """Site 1: `hermes plugins update` on a pinned plugin."""
    import hermes_cli.plugins_cmd as plugins_cmd

    home = _spaced_home(tmp_path)
    source = _pin_a_spaced_source(home, monkeypatch)
    # rich wraps at the console width and will hard-break mid-word on a long
    # path; that is a terminal-rendering limit, not the quoting decision under
    # test, so give it a console wide enough to emit the span intact.
    monkeypatch.setenv("COLUMNS", "1000")

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        plugins_cmd.cmd_update("demo-plugin")

    printed = _pastable(buf.getvalue())
    words = _bash_words(printed, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        "hermes", "plugins", "install", source, "--force", "--ref", _SHA,
    ], f"printed {printed!r} produced {words!r}"


@requires_bash
def test_pinned_plugin_dashboard_hint_pastes_as_one_source(tmp_path, monkeypatch):
    """Site 2: the dashboard's update path returns the same remedy."""
    import hermes_cli.plugins_cmd as plugins_cmd

    home = _spaced_home(tmp_path)
    source = _pin_a_spaced_source(home, monkeypatch)

    result = plugins_cmd.dashboard_update_user_plugin("demo-plugin")
    assert result["ok"] is False, result

    printed = _pastable(result["error"])
    words = _bash_words(printed, str(tmp_path))

    assert words is not None, f"bash refused the printed remedy {printed!r}"
    assert words == [
        "hermes", "plugins", "install", source, "--force", "--ref", _SHA,
    ], f"printed {printed!r} produced {words!r}"


def test_pinned_plugin_hint_keeps_the_readable_form_for_an_ordinary_source(
    tmp_path, monkeypatch
):
    """Over-fix guard: an ordinary owner/repo source must not grow quotes."""
    import hermes_cli.plugins_cmd as plugins_cmd

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = "https://github.com/owner/repo.git"

    plugins_dir = Path(plugins_cmd._plugins_dir())
    (plugins_dir / "demo-plugin").mkdir(parents=True, exist_ok=True)
    meta = Path(plugins_cmd._install_metadata_path())
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {"demo-plugin": {"pinned": True, "revision": _SHA, "source": source}}
        ),
        encoding="utf-8",
    )

    result = plugins_cmd.dashboard_update_user_plugin("demo-plugin")
    assert f"`hermes plugins install {source} --force" in " ".join(
        result["error"].split()
    ), result["error"]


# --------------------------------------------------------------------------
# class guard -- a new bare interpolation cannot land silently
# --------------------------------------------------------------------------
def test_no_residual_site_reintroduces_a_bare_interpolation():
    """Pins the source shape of all eight fixed sites as one class.

    Each needle carries its EXPECTED COUNT, not just presence. Two of these
    spans appear twice in their file (the whatsapp adapter's returncode and
    exception arms; the plugins_cmd CLI and dashboard paths), and a
    presence-only assertion cannot see one of a duplicated pair being
    reverted -- the needle is still there. The count is what makes a
    single-site regression visible to this guard.
    """
    expectations = {
        "tools/self_repo_guard.py": {
            "git clone --shared {hint_value(str(root))}": 1,
            "{hint_value(f'{scratch}/<task>')}": 1,
        },
        "plugins/platforms/whatsapp/adapter.py": {
            # :612 returncode arm and :626 exception arm, byte-identical.
            "cd {hint_value(str(bridge_dir))} && {hint_value(_npm_bin)} install": 2,
        },
        "hermes_cli/doctor.py": {
            "{hint_value(sys.executable)} -m pip install --force-reinstall certifi": 1,
        },
        "gateway/run.py": {
            "{hint_value(sys.executable)} -m pip install PyNaCl": 1,
            "hermes skills install {hint_value(install_path)}": 1,
        },
        "hermes_cli/plugins_cmd.py": {
            # CLI (:1106) and dashboard (:2855) paths, byte-identical.
            "hermes plugins install {recorded_source} --force": 2,
        },
    }
    for rel, needles in expectations.items():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for needle, expected in needles.items():
            assert source.count(needle) == expected, (
                f"{rel} has {source.count(needle)} of the escaped form, "
                f"expected {expected}: {needle}"
            )

    plugins = (REPO_ROOT / "hermes_cli" / "plugins_cmd.py").read_text(encoding="utf-8")
    assert plugins.count("recorded_source = hint_value(") == 1
    assert plugins.count("hint_value(str(install_record.get(\"source\"") == 2, (
        "both pinned-plugin sites must route their source through hint_value"
    )
