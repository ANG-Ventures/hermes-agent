"""Shell-hook failures must not disclose the hook command to the model.

A hook command is operator config and routinely carries inline credentials
(``sh -c 'export TOK=…; …'``). A fail-closed refusal is returned to the MODEL
as the tool result, so neither the raw command nor exception text derived from
it (shlex errors, OSError's rendering of argv) may reach it. The full
diagnostic stays on the operator channel: ``error_detail``, the log, and
``hermes hooks test`` / ``hermes hooks doctor``.
"""

from __future__ import annotations

import logging
import sys

import pytest

from agent import shell_hooks

_PLANTED = "hunter2PRODSup3rSecret"


def _spawn_result(**overrides):
    base = {
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "elapsed_seconds": 0.1,
        "error": None,
        "error_detail": None,
    }
    base.update(overrides)
    return base


def test_unparseable_command_is_not_disclosed():
    # Unbalanced quote: the shape that still holds the credential when the parse fails.
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=f"/bin/sh -c 'export TOK={_PLANTED}; echo hi", fail_closed=True,
    )
    r = shell_hooks._spawn(spec, "{}")

    assert r["error"] == "command cannot be parsed (ValueError)"
    assert "No closing quotation" in r["error_detail"], "the operator channel lost the reason"

    decision = shell_hooks._evaluate_result(spec, r)
    assert decision is not None and decision["action"] == "block"
    assert _PLANTED not in decision["message"]
    assert "No closing quotation" not in decision["message"]
    assert shell_hooks.hook_display_name(spec.command) in decision["message"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec semantics")
def test_spawn_failure_does_not_disclose_argv(tmp_path):
    # Executable bit set, no shebang, not a binary: the OS raises ENOEXEC, whose str()
    # renders the program path — here a directory in the path carries the credential.
    (tmp_path / f"tok-{_PLANTED}").mkdir()
    hook = tmp_path / f"tok-{_PLANTED}" / "hook.bin"
    hook.write_bytes(b"\x00\x01\x02\x03not-an-executable")
    hook.chmod(0o755)
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=str(hook), fail_closed=True)

    r = shell_hooks._spawn(spec, "{}")

    assert r["error"] and _PLANTED not in r["error"]
    assert r["error_detail"] and r["error_detail"] != r["error"], "EACCES/ENOEXEC must stay distinguishable"
    decision = shell_hooks._evaluate_result(spec, r)
    assert decision is not None and decision["action"] == "block"
    assert _PLANTED not in decision["message"]


def test_fail_closed_refusal_does_not_name_the_raw_command(tmp_path):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=f"{tmp_path}/missing-hook --token={_PLANTED}", fail_closed=True,
    )
    decision = shell_hooks._evaluate_result(spec, shell_hooks._spawn(spec, "{}"))

    assert decision is not None and decision["action"] == "block"
    assert _PLANTED not in decision["message"]
    assert "missing-hook#" in decision["message"], "the hook must stay identifiable"


def test_callback_name_does_not_carry_the_raw_command():
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=f"/usr/bin/env TOK={_PLANTED} /opt/guard.py")
    cb = shell_hooks._make_callback(spec)

    assert _PLANTED not in cb.__name__
    assert _PLANTED not in cb.__qualname__
    assert cb.__name__.startswith("shell_hook[pre_tool_call:")


def test_evaluate_result_logs_detail_but_blocks_with_redacted(caplog):
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command="/tmp/h.sh", fail_closed=True)
    r = _spawn_result(
        error="spawn failed (OSError)",
        error_detail="spawn failed: [Errno 13] Permission denied: '/tmp/h.sh'",
    )
    with caplog.at_level(logging.WARNING, logger="agent.shell_hooks"):
        decision = shell_hooks._evaluate_result(spec, r)

    assert "Errno 13" in caplog.text, "the log lost the operator detail"
    assert decision is not None and "Errno 13" not in decision["message"]
    assert "spawn failed (OSError)" in decision["message"]


@pytest.mark.parametrize("command, label", [
    ("/opt/hooks/guard.py", "guard.py"),
    ("/usr/bin/python3 /opt/hooks/guard.py --strict", "guard.py"),
    (f"sh -c 'export TOK={_PLANTED}; x'", "sh"),
    (f"env TOK={_PLANTED} guard", "env"),
    (f"https://user:{_PLANTED}@host/x", "x"),
    (f"TOK={_PLANTED}", "hook"),
    (f"/bin/sh -c 'export TOK={_PLANTED}", "sh"),
])
def test_hook_display_name_is_secret_free_and_stable(command, label):
    name = shell_hooks.hook_display_name(command)
    assert _PLANTED not in name
    assert name.startswith(f"{label}#")
    assert name == shell_hooks.hook_display_name(command)
    assert name != shell_hooks.hook_display_name(command + " ")
