"""One interpreter start per tool call for Python shell hooks (t_f010356f).

Every Python hook that runs through the dispatcher must look the same as one started on its own:
same exit status, same stdout bytes, same stderr bytes, so ``_evaluate_result`` reaches the same
verdict with the same message. The integration tests register real hook scripts from config, fire
``pre_tool_call`` through the plugin manager and count the interpreters started.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent import shell_hooks, shell_hooks_batch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the dispatcher forks (POSIX only)")

PAYLOAD = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                      "tool_input": {"command": "ls é"}, "session_id": "s", "cwd": "/", "extra": {}})

BODIES = {
    "allow": 'import json, sys\njson.load(sys.stdin)\nprint(json.dumps({"action": "allow", "argv": sys.argv[1:], '
             '"name": __name__, "file": __file__, "path0": sys.path[0]}))\n',
    "block_json": 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"action": "block", "message": "no é"}))\n',
    "exit2_stderr": 'import sys\nsys.stdin.read()\nsys.stderr.write("refused by policy\\n")\nsys.exit(2)\n',
    "crash_import": 'import json, sys\njson.load(sys.stdin)\nimport nonexistent_dispatch_probe_dep\n',
    "crash_raise": 'raise RuntimeError("synthetic hook crash")\n',
    "syntax": "if :\n",
    "exit_message": 'import sys\nsys.exit("bad thing")\n',
    "exit_none": 'raise SystemExit\n',
    "thread_atexit": ('import atexit, sys, threading, time\nsys.stdin.read()\n'
                      'atexit.register(lambda: print(\'{"action": "block", "message": "from atexit"}\'))\n'
                      'def w():\n    time.sleep(0.2)\n    sys.stderr.write("thread done\\n")\n'
                      'threading.Thread(target=w).start()\n'),
    "sibling_import": 'import json, sys, hook_sibling\nsys.stdin.read()\nprint(json.dumps({"x": hook_sibling.X}))\n',
}


@pytest.fixture
def hook_dir(tmp_path: Path) -> Path:
    for name, body in BODIES.items():
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    (tmp_path / "hook_sibling.py").write_text("X = 7\n", encoding="utf-8")
    return tmp_path


def _dispatch(hooks: list, payload: str = PAYLOAD) -> dict:
    request = json.dumps({"hooks": hooks, "stdin": payload})
    proc = subprocess.run([sys.executable, shell_hooks_batch.DISPATCHER], input=request.encode(),
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    rows = {}
    for line in proc.stdout.decode().splitlines():
        row = json.loads(line)
        for key in ("out", "err"):
            if key in row:
                row[key] = base64.b64decode(row[key])
        rows[row["i"]] = row
    return rows


def test_dispatcher_output_is_byte_identical_to_a_fresh_interpreter(hook_dir: Path) -> None:
    names = sorted(BODIES)
    rows = _dispatch([{"script": str(hook_dir / f"{n}.py"), "args": ["--flag"] if n == "allow" else [],
                       "timeout": 30} for n in names])
    assert sorted(rows) == list(range(len(names)))
    for i, name in enumerate(names):
        argv = [sys.executable, str(hook_dir / f"{name}.py")] + (["--flag"] if name == "allow" else [])
        ref = subprocess.run(argv, input=PAYLOAD.encode(), capture_output=True, timeout=30)
        got = rows[i]
        assert (got["rc"], got["out"], got["err"]) == (ref.returncode, ref.stdout, ref.stderr), name


def test_dispatcher_times_out_one_hook_without_holding_the_others(hook_dir: Path) -> None:
    slow = hook_dir / "slow.py"
    slow.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    rows = _dispatch([{"script": str(slow), "timeout": 1}, {"script": str(hook_dir / "block_json.py"), "timeout": 30}])
    assert rows[0]["timed_out"] is True and rows[0]["rc"] is None
    assert rows[1]["rc"] == 0 and rows[1]["elapsed"] < 1


def test_dispatcher_reports_an_absent_script_as_missing(hook_dir: Path) -> None:
    rows = _dispatch([{"script": str(hook_dir / "absent.py"), "timeout": 5}])
    assert rows[0] == {"i": 0, "missing": True}


# --- through the real registration + invoke_hook path --------------------------------------------

@pytest.fixture
def manager(monkeypatch, tmp_path: Path):
    from hermes_cli import plugins
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shell_hooks.reset_for_tests()
    getattr(shell_hooks_batch, "close_dispatchers", lambda: None)()  # a warm one from another test
    mgr = plugins.PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins, "_plugin_manager", mgr)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: mgr)
    yield mgr
    shell_hooks.reset_for_tests()
    getattr(shell_hooks_batch, "close_dispatchers", lambda: None)()


def _count_spawns(monkeypatch) -> list:
    """Record argv[1] of every hook-runner Popen (dispatcher or single hook)."""
    seen: list = []
    real = subprocess.Popen

    def popen(argv, *a, **kw):
        if isinstance(argv, list) and len(argv) > 1 and argv[0] == sys.executable:  # interpreters only, not git
            seen.append(argv[1])
        return real(argv, *a, **kw)

    monkeypatch.setattr(shell_hooks.subprocess, "Popen", popen)
    return seen


def _register(hook_dir: Path, names: list, *, batch: bool, fail_closed: bool = True) -> None:
    entries = [{"command": f"{sys.executable} {hook_dir / f'{n}.py'}", "matcher": "terminal",
                "timeout": 30, "fail_closed": fail_closed} for n in names]
    cfg = {"hooks": {"batch_python_hooks": batch, "pre_tool_call": entries}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == len(names)


NAMES = ["allow", "block_json", "exit2_stderr", "crash_import", "syntax", "thread_atexit", "sibling_import"]


def _fire(mgr) -> list:
    return mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={"command": "ls é"},
                           session_id="s", tool_call_id="c1", turn_id="t1")


def test_batched_verdicts_equal_per_hook_spawns(manager, monkeypatch, hook_dir: Path) -> None:
    spawns = _count_spawns(monkeypatch)
    _register(hook_dir, NAMES, batch=False)
    per_hook = _fire(manager)
    assert len(spawns) == len(NAMES) and shell_hooks_batch.DISPATCHER not in spawns

    manager._hooks.clear()
    shell_hooks.reset_for_tests()
    spawns.clear()
    _register(hook_dir, NAMES, batch=True)
    batched = _fire(manager)
    assert spawns == [shell_hooks_batch.DISPATCHER], spawns  # one interpreter for the whole call
    assert batched == per_hook
    assert {"action": "block", "message": "refused by policy"} in batched
    assert any("CRASHED" in r.get("message", "") for r in batched)


def test_n_batches_start_one_warm_dispatcher(manager, monkeypatch, hook_dir: Path) -> None:
    """t_e920d269: a dispatcher Popen per batch was ~57% of the gateway's hook GIL time (fork+exec of a
    multi-GB process). N tool calls reuse ONE warm dispatcher, and every verdict stays the same."""
    spawns = _count_spawns(monkeypatch)
    _register(hook_dir, NAMES, batch=True)
    first = _fire(manager)
    for i in range(4):
        assert manager.invoke_hook("pre_tool_call", tool_name="terminal", args={"command": "ls é"},
                                   session_id="s", tool_call_id=f"c{i + 2}", turn_id="t1") == first
    assert spawns == [shell_hooks_batch.DISPATCHER], spawns


def test_dead_warm_dispatcher_is_replaced(manager, monkeypatch, hook_dir: Path) -> None:
    spawns = _count_spawns(monkeypatch)
    _register(hook_dir, ["block_json"], batch=True)
    assert _fire(manager) == [{"action": "block", "message": "no é"}]
    idle = shell_hooks_batch._idle_dispatchers()
    assert len(idle) == 1
    for d in idle:
        d.proc.kill()
        d.proc.wait()
    assert _fire(manager) == [{"action": "block", "message": "no é"}]
    assert spawns == [shell_hooks_batch.DISPATCHER, shell_hooks_batch.DISPATCHER], spawns


def test_concurrent_batches_do_not_share_a_busy_dispatcher(manager, hook_dir: Path) -> None:
    import threading
    slow = hook_dir / "slow_ok.py"
    slow.write_text('import json, sys, time\nsys.stdin.read()\ntime.sleep(1.0)\nprint(json.dumps({"x": 1}))\n',
                    encoding="utf-8")
    _register(hook_dir, ["slow_ok"], batch=True)
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(_fire(manager))) for _ in range(3)]
    import time as _t
    t0 = _t.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(out) == 3 and _t.monotonic() - t0 < 2.8  # run side by side, not one after another


def test_serve_mode_answers_several_requests_on_one_process(hook_dir: Path) -> None:
    proc = subprocess.Popen([sys.executable, shell_hooks_batch.DISPATCHER, "--serve"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    try:
        for _ in range(3):
            req = {"hooks": [{"script": str(hook_dir / "block_json.py"), "timeout": 30},
                             {"script": str(hook_dir / "absent.py"), "timeout": 5}], "stdin": PAYLOAD}
            proc.stdin.write((json.dumps(req) + "\n").encode())
            proc.stdin.flush()
            rows = []
            while True:
                row = json.loads(proc.stdout.readline())
                if row.get("end"):
                    break
                rows.append(row)
            by_i = {r["i"]: r for r in rows}
            assert by_i[1] == {"i": 1, "missing": True}
            assert json.loads(base64.b64decode(by_i[0]["out"])) == {"action": "block", "message": "no é"}
    finally:
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0


def test_matcher_filters_before_any_process_starts(manager, monkeypatch, hook_dir: Path) -> None:
    spawns = _count_spawns(monkeypatch)
    _register(hook_dir, ["block_json"], batch=True)
    assert manager.invoke_hook("pre_tool_call", tool_name="read_file", args={}, tool_call_id="c2") == []
    assert spawns == []  # the matcher filters before any process starts


def test_absent_hook_gets_the_same_missing_hook_verdict(manager, monkeypatch, hook_dir: Path) -> None:
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *a: True)
    spawns = _count_spawns(monkeypatch)
    _register(hook_dir, ["block_json", "absent"], batch=False)
    per_hook = _fire(manager)
    manager._hooks.clear()
    shell_hooks.reset_for_tests()
    spawns.clear()
    _register(hook_dir, ["block_json", "absent"], batch=True)
    assert _fire(manager) == per_hook
    assert spawns == [shell_hooks_batch.DISPATCHER]
    assert per_hook[0] == {"action": "block", "message": "no é"} and per_hook[1]["action"] == "block"


def test_non_python_and_flagged_commands_are_not_batched() -> None:
    def spec(command: str) -> shell_hooks.ShellHookSpec:
        return shell_hooks.ShellHookSpec(event="pre_tool_call", command=command, batch=True)
    assert shell_hooks_batch.batch_argv(spec("/usr/bin/python3 /h/a.py --x")) == ("/usr/bin/python3", "/h/a.py", ["--x"])
    assert shell_hooks_batch.batch_argv(spec("/usr/bin/python3 -I /h/a.py")) is None
    assert shell_hooks_batch.batch_argv(spec("/h/a.sh")) is None
    assert shell_hooks_batch.batch_argv(spec("bash /h/a.py")) is None
    off = shell_hooks.ShellHookSpec(event="pre_tool_call", command="/usr/bin/python3 /h/a.py", batch=False)
    assert shell_hooks_batch.batch_argv(off) is None


def test_config_flag_parses_and_defaults_on() -> None:
    entry = [{"command": "/usr/bin/python3 /h/a.py"}]
    assert shell_hooks._parse_hooks_block({"pre_tool_call": entry})[0].batch is True
    assert shell_hooks._parse_hooks_block({"batch_python_hooks": False, "pre_tool_call": entry})[0].batch is False
    assert shell_hooks._parse_hooks_block({"batch_python_hooks": "no", "pre_tool_call": entry})[0].batch is True


def test_outside_invoke_hook_spawns_on_its_own(monkeypatch, hook_dir: Path) -> None:
    spawns = _count_spawns(monkeypatch)
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=f"{sys.executable} {hook_dir / 'block_json.py'}",
                                     batch=True)
    result = shell_hooks.run_once(spec, {"tool_name": "terminal", "args": {}})
    assert spawns == [str(hook_dir / "block_json.py")]
    assert result["parsed"] == {"action": "block", "message": "no é"}


def test_dispatcher_source_stays_python39_compatible() -> None:
    import ast
    ast.parse(Path(shell_hooks_batch.DISPATCHER).read_text(encoding="utf-8"), feature_version=(3, 9))
