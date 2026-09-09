"""Fail-closed process failures must prevent real terminal side effects.

Each dispatch runs in a fresh process with synthetic HOME/HERMES_HOME, real
config registration, real hook subprocesses, and the real terminal handler.
No providers, live profiles, credentials, or external services are needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from agent import shell_hooks


@pytest.mark.parametrize("returncode", [1, 3, 127, -15])
@pytest.mark.parametrize("stdout", [
    "", "{}", '{"action":"allow"}',
    '{"action":"modify","args":{"command":"echo unsafe"}}',
    '{"decision":"modify","tool_input":{"command":"echo unsafe"}}',
])
def test_abnormal_exit_cannot_authorize(returncode, stdout):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command="policy", fail_closed=True,
    )
    result = shell_hooks._evaluate_result(spec, {
        "returncode": returncode, "stdout": stdout, "stderr": "synthetic failure",
        "timed_out": False, "elapsed_seconds": 0.0, "error": None,
    })
    assert result is not None and result["action"] == "block"
    assert str(returncode) in result["message"]


@pytest.mark.parametrize("stdout", [
    "", " \n", "not JSON", "[]", "null", '"allow"',
    '{"action":"alllow"}', '{"action":"modify","args":[]}',
    '{"decision":"modify"}', '{"unexpected":"value"}',
    pytest.param("[" * 2000 + "]" * 2000, id="deep_json"),
])
def test_invalid_success_output_cannot_authorize(stdout):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command="policy", fail_closed=True,
    )
    result = shell_hooks._evaluate_result(spec, {
        "returncode": 0, "stdout": stdout, "stderr": "",
        "timed_out": False, "elapsed_seconds": 0.0, "error": None,
    })
    assert result is not None and result["action"] == "block"


@pytest.mark.parametrize("fail_closed", [False, True])
def test_callback_serialization_failure_respects_policy(fail_closed):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command="unused", fail_closed=fail_closed,
    )
    args = {}
    args["cycle"] = args
    result = shell_hooks._make_callback(spec)(tool_name="terminal", args=args)
    if fail_closed:
        assert result is not None and result["action"] == "block"
        assert "ValueError" in result["message"]
    else:
        assert result is None


# The original eight-case reproducer, followed by additional failure shapes.
# {} is the documented successful no-op/allow wire shape; keep that control.
_ORIGINAL_CASES = [
    pytest.param('print(\'{"action":"block","message":"synthetic policy"}\')', True, False, id="healthy_block"),
    pytest.param('raise RuntimeError("synthetic hook crash")', True, False, id="crashed_hook"),
    pytest.param("import nonexistent_hook_probe_dependency", True, False, id="import_failure"),
    pytest.param("if :", True, False, id="syntax_failure"),
    pytest.param("raise SystemExit(1)", True, False, id="exit_one"),
    pytest.param("import time; time.sleep(10)", True, False, id="timeout"),
    pytest.param('print("not JSON")', True, False, id="malformed_stdout"),
    pytest.param('print("{}")', False, False, id="explicit_allow_control"),
]
_MODIFY = (
    "import json, sys; p = json.load(sys.stdin); "
    "print(json.dumps({'action':'modify','args':{'command':"
    "p['tool_input']['command'].replace('blocked-write-sentinel', 'modified-write-sentinel')}}))"
)
_EXTRA_CASES = [
    pytest.param("pass", True, False, id="empty_stdout"),
    pytest.param('print("[]")', True, False, id="non_object_stdout"),
    pytest.param('print(\'{"action":"alllow"}\')', True, False, id="unknown_action"),
    pytest.param('print(\'{"action":"modify","args":[]}\')', True, False, id="invalid_modify"),
    pytest.param('print("[" * 2000 + "]" * 2000)', True, False, id="deep_json"),
    pytest.param('print(\'{"action":"allow"}\'); raise SystemExit(1)', True, False, id="exit_one_allow"),
    pytest.param(_MODIFY + "; raise SystemExit(3)", True, False, id="exit_three_modify"),
    pytest.param('print(\'{"action":"allow"}\'); raise SystemExit(2)', True, False, id="exit_two_allow"),
    pytest.param('print(\'{"action":"allow"}\')', False, False, id="explicit_action_allow"),
    pytest.param('print(\'{"decision":"allow"}\')', False, False, id="explicit_decision_allow"),
    pytest.param(_MODIFY, False, True, id="successful_modify"),
    pytest.param(_MODIFY.replace("'action':'modify','args'", "'decision':'modify','tool_input'"), False, True, id="successful_compat_modify"),
    pytest.param(
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)", True, False,
        id="signal", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX signal exit semantics"),
    ),
    pytest.param(
        _MODIFY + "; sys.stdout.flush(); import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        True, False, id="signal_after_modify",
        marks=pytest.mark.skipif(os.name == "nt", reason="POSIX signal exit semantics"),
    ),
]

_DISPATCH = textwrap.dedent("""
    import json, os, shlex, subprocess, sys
    from pathlib import Path

    repo, root = map(Path, sys.argv[1:3])
    sys.path.insert(0, str(repo))
    from agent import shell_hooks
    import model_tools
    from hermes_constants import get_hermes_home

    assert Path(shell_hooks.__file__).resolve() == repo / 'agent' / 'shell_hooks.py'
    assert Path(model_tools.__file__).resolve() == repo / 'model_tools.py'
    assert get_hermes_home().resolve() == root / 'hermes'
    assert Path.home().resolve() == root / 'home'
    quote = subprocess.list2cmdline if os.name == 'nt' else shlex.join
    config = {'hooks_auto_accept': True, 'hooks': {'pre_tool_call': [{
        'command': quote([sys.executable, str(root / 'policy.py')]),
        'matcher': 'terminal', 'timeout': 1, 'fail_closed': sys.argv[3] == 'true',
    }]}}
    specs = shell_hooks.register_from_config(config, accept_hooks=True)
    assert len(specs) == 1 and specs[0].fail_closed == (sys.argv[3] == 'true')
    definitions = model_tools.get_tool_definitions(enabled_toolsets=['terminal'], quiet_mode=True)
    names = [item['function']['name'] for item in definitions]
    assert 'terminal' in names
    target = root / 'blocked-write-sentinel.txt'
    command = quote([sys.executable, '-c',
        "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('SENTINEL')"])
    args = {'command': command, 'timeout': 15, 'workdir': str(root)}
    if sys.argv[4] == 'true':
        # Real callback serialization error, not an injected dispatcher mock.
        args['cycle'] = args
    result = model_tools.handle_function_call(
        'terminal', args,
        task_id='synthetic-hook-probe', session_id='synthetic-hook-probe',
        enabled_tools=names, enabled_toolsets=['terminal'],
    )
    print('PROBE_RESULT ' + json.dumps({
        'result': json.loads(result), 'source': shell_hooks.__file__,
        'original_exists': target.exists(),
        'modified_exists': (root / 'modified-write-sentinel.txt').exists(),
    }))
""")


def _dispatch_in_scratch(tmp_path, source, fail_closed=True, cyclic_args=False):
    root = tmp_path.resolve()
    (root / "home").mkdir()
    (root / "hermes").mkdir()
    (root / "policy.py").write_text(source + "\n", encoding="utf-8")
    env = {
        "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
        "HERMES_HOME": str(root / "hermes"), "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8", "TERMINAL_ENV": "local", "TERMINAL_CWD": str(root),
        "TERMINAL_PERSISTENT_SHELL": "false", "HERMES_ENABLE_PROJECT_PLUGINS": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
            if key in os.environ:
                env[key] = os.environ[key]
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", _DISPATCH, str(repo), str(root),
         str(fail_closed).lower(), str(cyclic_args).lower()],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        cwd=root, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = [line[len("PROBE_RESULT "):] for line in proc.stdout.splitlines()
            if line.startswith("PROBE_RESULT ")]
    assert len(rows) == 1, proc.stdout + proc.stderr
    return json.loads(rows[0])


def _assert_effect(tmp_path, row, blocked, modified):
    assert row["original_exists"] is (not blocked and not modified), row
    assert row["modified_exists"] is (not blocked and modified), row
    if blocked:
        assert "error" in row["result"], row
        assert "hook" in row["result"]["error"] or "synthetic policy" in row["result"]["error"], row
    else:
        assert row["result"]["exit_code"] == 0, row
        target = tmp_path / ("modified-write-sentinel.txt" if modified else "blocked-write-sentinel.txt")
        assert target.read_text() == "SENTINEL"


@pytest.mark.parametrize("source,blocked,modified", _ORIGINAL_CASES)
def test_original_terminal_gate(tmp_path, source, blocked, modified):
    _assert_effect(tmp_path, _dispatch_in_scratch(tmp_path, source), blocked, modified)


@pytest.mark.parametrize("source,blocked,modified", _EXTRA_CASES)
def test_extended_terminal_gate(tmp_path, source, blocked, modified):
    _assert_effect(tmp_path, _dispatch_in_scratch(tmp_path, source), blocked, modified)


@pytest.mark.parametrize("source,modified", [
    ("raise SystemExit(1)", False), ("pass", False), ('print("not JSON")', False),
    ('print(\'{"action":"alllow"}\')', False),
    (_MODIFY + "; raise SystemExit(1)", True),
])
def test_fail_open_terminal_compatibility(tmp_path, source, modified):
    _assert_effect(tmp_path, _dispatch_in_scratch(tmp_path, source, fail_closed=False), False, modified)


@pytest.mark.parametrize("fail_closed", [False, True])
def test_callback_exception_terminal_effect(tmp_path, fail_closed):
    row = _dispatch_in_scratch(tmp_path, 'print("{}")', fail_closed, cyclic_args=True)
    _assert_effect(tmp_path, row, fail_closed, False)
