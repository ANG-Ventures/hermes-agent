"""``execute_code`` must SURFACE a guard block, never return empty success.

The gateway-lifecycle guard refuses a ``terminal()`` call by RETURNING a
result dict. Inside the ``execute_code`` sandbox that is a value, not an
error — so the overwhelmingly common script shape ::

    r = terminal("sudo launchctl bootstrap system /path/to.plist")

runs to completion without inspecting ``r``, and ``execute_code`` reports
``status=success``, ``exit_code=0``, ``output=""``. Measured 2026-09-20: a
silent block, strictly worse than the direct terminal tool, which at least
prints why it refused.

The generated ``hermes_tools`` stub therefore raises ``ToolCallBlocked`` on
any result carrying the guard's marker key, so the refusal lands in the
cell's traceback and ``execute_code`` reports ``status=error`` with a
non-zero exit code.
"""

from __future__ import annotations

import json

from cron.lifecycle_guard import GATEWAY_LIFECYCLE_BLOCK_MARKER
from tools.code_execution_tool import (
    SANDBOX_ALLOWED_TOOLS,
    _TOOL_STUBS,
    generate_hermes_tools_module,
)


def _stub_namespace(tool_names, call_result, transport="uds"):
    """Exec the generated module with ``_call`` replaced by a canned result."""
    source = generate_hermes_tools_module(list(tool_names), transport=transport)
    namespace: dict = {}
    exec(compile(source, "<hermes_tools>", "exec"), namespace)
    namespace["_call"] = lambda tool, args: call_result
    return namespace


def _blocked_result():
    """The exact shape tools/terminal_tool.py returns on a lifecycle block."""
    return {
        "output": "",
        "exit_code": 1,
        "error": (
            "Blocked: launchctl submit registers a persistent KeepAlive job "
            "and is unsafe from inside the gateway process."
        ),
        "status": "error",
        "blocked_by": GATEWAY_LIFECYCLE_BLOCK_MARKER,
    }


class TestStubRaisesOnBlock:
    def test_terminal_stub_raises_on_a_blocked_result(self):
        namespace = _stub_namespace(["terminal"], _blocked_result())
        blocked = namespace["ToolCallBlocked"]
        try:
            namespace["terminal"]("sudo launchctl bootstrap system /tmp/x.plist")
        except blocked as exc:
            assert "was blocked and did not run" in str(exc)
            assert "launchctl submit" in str(exc)
        else:
            raise AssertionError("a blocked terminal() call returned instead of raising")

    def test_blocked_marker_key_matches_the_producer(self):
        """Contract test: the stub's key is the guard's key.

        The stub is generated source and cannot import the guard, so the key
        is duplicated. Derive BOTH sides here rather than retyping either —
        a rename on the producer side must fail this test, not silently
        disarm the stub.
        """
        source = generate_hermes_tools_module(["terminal"])
        assert '_BLOCKED_MARKER_KEY = "blocked_by"' in source
        result = _blocked_result()
        assert result["blocked_by"] == GATEWAY_LIFECYCLE_BLOCK_MARKER
        namespace = _stub_namespace(["terminal"], result)
        assert namespace["_BLOCKED_MARKER_KEY"] in result

    def test_every_generated_stub_routes_through_the_guard(self):
        """Not just terminal(): every tool the sandbox exposes.

        Calls each stub with placeholder arguments derived from its own
        signature, so a newly-added tool is covered without editing this
        test.
        """
        import inspect

        names = sorted(SANDBOX_ALLOWED_TOOLS & set(_TOOL_STUBS))
        assert names, "no sandbox tool stubs to check"
        namespace = _stub_namespace(names, _blocked_result())
        blocked = namespace["ToolCallBlocked"]
        checked = []
        for name in names:
            func = namespace[name]
            signature = inspect.signature(func)
            args = [
                ["x"] if parameter.annotation is list else "x"
                for parameter in signature.parameters.values()
                if parameter.default is inspect.Parameter.empty
            ]
            try:
                func(*args)
            except blocked:
                checked.append(name)
                continue
            raise AssertionError(f"{name}() did not raise on a blocked result")
        assert checked == names

    def test_file_transport_stub_raises_too(self):
        """Remote backends generate a different header, same contract."""
        namespace = _stub_namespace(
            ["terminal"], _blocked_result(), transport="file"
        )
        blocked = namespace["ToolCallBlocked"]
        try:
            namespace["terminal"]("launchctl submit -l x -- /bin/true")
        except blocked:
            return
        raise AssertionError("file-transport stub returned a blocked result")


class TestNormalResultsUnaffected:
    def test_successful_result_passes_through_unchanged(self):
        ok = {"output": "hi\n", "exit_code": 0, "error": None}
        namespace = _stub_namespace(["terminal"], ok)
        assert namespace["terminal"]("echo hi") == ok

    def test_ordinary_nonzero_exit_is_not_a_block(self):
        """A command that RAN and failed must still return, not raise.

        This is the discriminating half: if the stub raised on any non-zero
        exit code, every legitimate `grep` miss or `exit 7` would explode.
        """
        failed = {"output": "", "exit_code": 7, "error": None}
        namespace = _stub_namespace(["terminal"], failed)
        assert namespace["terminal"]("exit 7") == failed

    def test_error_status_without_the_marker_is_not_a_block(self):
        """`status: error` alone is not a refusal — only the marker is."""
        errored = {
            "output": "",
            "exit_code": -1,
            "error": "Failed to execute command: boom",
            "status": "error",
        }
        namespace = _stub_namespace(["terminal"], errored)
        assert namespace["terminal"]("boom") == errored

    def test_non_dict_result_passes_through(self):
        namespace = _stub_namespace(["terminal"], "raw string result")
        assert namespace["terminal"]("echo hi") == "raw string result"

    def test_falsy_marker_value_is_not_a_block(self):
        """A key present but empty must not trip the guard path."""
        result = {"output": "", "exit_code": 0, "blocked_by": ""}
        namespace = _stub_namespace(["terminal"], result)
        assert namespace["terminal"]("echo hi") == result


class TestTerminalToolStampsTheMarker:
    """The producer side: both refusal returns must carry the marker."""

    def test_both_block_returns_carry_the_marker(self):
        import re
        from pathlib import Path

        import tools.terminal_tool as terminal_tool

        source = Path(terminal_tool.__file__).read_text(encoding="utf-8")
        # Every `"status": "error"` return in the supervised-gateway guard
        # block must be stamped. Locate the guard region lexically.
        start = source.index("if _is_supervised_gateway_process():")
        end = source.index("# Validate before the source guard resolves", start)
        region = source[start:end]
        blocks = re.findall(r'"status": "error",', region)
        markers = re.findall(
            r'"blocked_by": _GATEWAY_LIFECYCLE_BLOCK_MARKER,', region
        )
        assert len(blocks) == 2, f"expected 2 guard refusals, found {len(blocks)}"
        assert len(markers) == len(blocks), (
            f"{len(blocks)} guard refusals but only {len(markers)} stamped "
            "with the block marker — an unstamped refusal is a silent block "
            "inside execute_code"
        )

    def test_marker_survives_json_round_trip(self):
        payload = json.dumps(_blocked_result(), ensure_ascii=False)
        assert json.loads(payload)["blocked_by"] == GATEWAY_LIFECYCLE_BLOCK_MARKER
