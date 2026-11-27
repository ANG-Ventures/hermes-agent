"""Shell hook dispatcher: compile once, opt-in prefilter, per-hook timing log (t_492356b3).

The hook contract (fresh-process view, same bytes and exit status as ``python <hook>``) is held by
tests/agent/test_shell_hooks_batch.py, which now runs every hook through the cached code object.
These tests pin what is new: a hook edited on disk is recompiled, a declared prefilter skips only a
provable miss and the skip is an allow in every hook mode, and each run logs per-hook milliseconds.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import shell_hooks, shell_hooks_batch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the dispatcher forks (POSIX only)")


def _payload(command: str, tool: str = "terminal") -> str:
    return json.dumps({"hook_event_name": "pre_tool_call", "tool_name": tool,
                       "tool_input": {"command": command}, "session_id": "s", "cwd": "/", "extra": {}})


@pytest.fixture(scope="module")
def disp():
    spec = importlib.util.spec_from_file_location("shd_under_test", shell_hooks_batch.DISPATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Serve:
    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, shell_hooks_batch.DISPATCHER, "--serve"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def ask(self, hooks: list, payload: str) -> dict:
        self.proc.stdin.write((json.dumps({"hooks": hooks, "stdin": payload}) + "\n").encode())
        self.proc.stdin.flush()
        rows = {}
        while True:
            row = json.loads(self.proc.stdout.readline())
            if row.get("end"):
                return rows
            for key in ("out", "err"):
                if key in row:
                    row[key] = base64.b64decode(row[key])
            rows[row["i"]] = row

    def close(self) -> None:
        self.proc.stdin.close()
        assert self.proc.wait(timeout=10) == 0


@pytest.fixture
def serve():
    s = _Serve()
    yield s
    s.close()


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# --- compile once --------------------------------------------------------------------------------

def test_load_compiles_once_per_file_version(disp, tmp_path: Path) -> None:
    hook = tmp_path / "h.py"
    hook.write_text("V = 1\n", encoding="utf-8")
    code1, _ = disp._load(str(hook))
    code2, _ = disp._load(str(hook))
    assert code1 is not None and code1 is code2  # cached: no second compile
    assert code1.co_filename == str(hook)
    _bump_mtime(hook)
    code3, _ = disp._load(str(hook))
    assert code3 is not code1


def test_load_leaves_an_uncompilable_hook_to_the_child(disp, tmp_path: Path) -> None:
    hook = tmp_path / "bad.py"
    hook.write_text("if :\n", encoding="utf-8")
    assert disp._load(str(hook)) == (None, None)
    assert str(hook) not in disp._compiled


def test_load_does_not_cache_a_hook_written_while_it_is_read(disp, tmp_path: Path, monkeypatch) -> None:
    """Prism 4fd96f14c9d0: an edit landing between read() and the closing fstat() must not pin the old
    bytes under the new file's key."""
    hook = tmp_path / "race.py"
    hook.write_text('V = "old"\n', encoding="utf-8")
    real_fstat = disp.os.fstat
    calls = []

    def fstat(fd):
        calls.append(fd)
        if len(calls) == 2:  # after read(), before the closing stat: the edit lands now
            hook.write_text('V = "new"\n', encoding="utf-8")
            _bump_mtime(hook)
        return real_fstat(fd)

    monkeypatch.setattr(disp.os, "fstat", fstat)
    assert disp._load(str(hook)) == (None, None)
    assert str(hook) not in disp._compiled
    monkeypatch.setattr(disp.os, "fstat", real_fstat)
    code, _ = disp._load(str(hook))
    ns: dict = {}
    exec(code, ns)
    assert ns["V"] == "new" and disp._load(str(hook))[0] is code


def test_serve_recompiles_a_hook_edited_on_disk(serve, tmp_path: Path) -> None:
    """Same size, new mtime: the warm dispatcher runs the new source, not the cached code."""
    hook = tmp_path / "ver.py"
    hook.write_text('import sys\nsys.stdin.read()\nprint("v1")\n', encoding="utf-8")
    hooks = [{"script": str(hook), "timeout": 30}]
    assert serve.ask(hooks, _payload("ls"))[0]["out"] == b"v1\n"
    assert serve.ask(hooks, _payload("ls"))[0]["out"] == b"v1\n"
    hook.write_text('import sys\nsys.stdin.read()\nprint("v2")\n', encoding="utf-8")
    _bump_mtime(hook)
    assert serve.ask(hooks, _payload("ls"))[0]["out"] == b"v2\n"
    hook.write_text("if :\n", encoding="utf-8")  # now broken: the child reports it as before
    _bump_mtime(hook)
    row = serve.ask(hooks, _payload("ls"))[0]
    assert row["rc"] == 1 and b"SyntaxError" in row["err"]


# --- prefilter -----------------------------------------------------------------------------------

@pytest.mark.parametrize("source, expected", [
    (b'HOOK_PREFILTER = {"tokens": ["git", "gh"]}\n', {"tokens": ("git", "gh")}),
    (b'HOOK_PREFILTER: dict = {"tools": ["terminal"]}\n', {"tools": frozenset({"terminal"})}),
    (b'X = 1\n', None),
    (b'HOOK_PREFILTER = {"tokens": []}\n', None),          # empty: not a declaration
    (b'HOOK_PREFILTER = {"tokens": [""]}\n', None),
    (b'HOOK_PREFILTER = {"tokens": ["git"], "x": [1]}\n', None),
    (b'HOOK_PREFILTER = {"tokens": [f()]}\n', None),       # not a literal: never executed
    (b'HOOK_PREFILTER = {"tokens": ["a"]}\nHOOK_PREFILTER = {"tokens": ["b"]}\n', None),
    (b'def f():\n    HOOK_PREFILTER = {"tokens": ["a"]}\n', None),  # not top level
    (b'HOOK_PREFILTER = ["git"]\n', None),
    (b'HOOK_PREFILTER = {"tool_input_tokens": ["git", "{"]}\n', {"tool_input_tokens": ("git", "{")}),
    (b'HOOK_PREFILTER = {"tool_input_tokens": []}\n', None),
])
def test_prefilter_declaration_is_parsed_strictly(disp, source: bytes, expected) -> None:
    assert disp._read_prefilter(source) == expected


@pytest.mark.parametrize("decl, payload, skips", [
    ({"tokens": ("git",)}, _payload("ls -la"), True),
    ({"tokens": ("git",)}, _payload("cd x && git push"), False),
    ({"tokens": ("git",)}, _payload("g''it push"), False),      # shell quoting does not hide it
    ({"tokens": ("git push",)}, _payload("git   push"), False),  # nor does extra whitespace
    ({"tokens": ("gh",)}, _payload("ls", tool="execute_code"), True),
    ({"tools": frozenset({"terminal"})}, _payload("ls", tool="read_file"), True),
    ({"tools": frozenset({"terminal"})}, _payload("ls"), False),
    ({"tools": frozenset({"terminal"}), "tokens": ("git",)}, _payload("ls"), True),
])
def test_prefilter_skips_only_a_provable_miss(disp, decl, payload: str, skips: bool) -> None:
    assert disp._prefilter_skips(decl, disp._payload_view(payload.encode())) is skips


def test_prefilter_sees_escaped_tokens_in_json_keys(disp) -> None:
    """Prism d94179e7de9f: a token present only in a (JSON-escaped) key is a match, not a miss."""
    payload = json.dumps({"tool_name": "terminal", "tool_input": {"café.txt": "x"}}).encode()
    assert b"caf\\u00e9" in payload  # ensure_ascii: the raw text does not carry the token
    assert disp._prefilter_skips({"tokens": ("café",)}, disp._payload_view(payload)) is False


def test_prefilter_runs_the_hook_when_a_string_does_not_shell_split(disp) -> None:
    """Prism 3d73a10a918e: `g''it push # '` is valid shell (the quote is in a comment) but
    shlex.split(comments=False) raises; a hook parsing it with comments=True sees `git push`."""
    view = disp._payload_view(_payload("g''it push # '").encode())
    assert disp._prefilter_skips({"tokens": ("git push",)}, view) is False
    tools_only = disp._payload_view(_payload("g''it push # '", tool="read_file").encode())
    assert disp._prefilter_skips({"tools": frozenset({"terminal"})}, tools_only) is True


def test_prefilter_never_skips_on_an_unreadable_payload_or_unnamed_tool(disp) -> None:
    decl = {"tools": frozenset({"terminal"}), "tokens": ("git",)}
    assert disp._payload_view(b"not json") is None
    assert disp._prefilter_skips(decl, None) is False
    no_tool = json.dumps({"hook_event_name": "on_session_start", "extra": {"x": "git"}}).encode()
    assert disp._prefilter_skips({"tools": frozenset({"terminal"})}, disp._payload_view(no_tool)) is False


# --- tool_input_tokens (t_7d9b8ad4): tokens scoped to the decoded strings under tool_input ----------

_BRACE = ("{", ",")


def _wire(command: str) -> bytes:
    """The payload exactly as the agent writes it to a hook's stdin (envelope braces and ", ")."""
    return shell_hooks._serialize_payload("pre_tool_call", {
        "tool_name": "terminal", "args": {"command": command}, "session_id": "s"}).encode("utf-8")


def test_tool_input_tokens_ignore_the_json_envelope(disp) -> None:
    """The raw-JSON matcher can never skip on `{`/`,`; the scoped one sees only the call's strings."""
    payload = _wire("ls -la")
    assert b"{" in payload and b", " in payload
    view = disp._payload_view(payload)
    assert disp._prefilter_skips({"tokens": _BRACE}, view) is False  # why the scoped key exists
    assert disp._prefilter_skips({"tool_input_tokens": _BRACE}, view) is True


def test_tool_input_tokens_skip_a_non_git_command(disp) -> None:
    """Mutation canary: pointed back at the raw JSON, `{` matches the envelope and this never skips."""
    view = disp._payload_view(_wire("ls -la"))
    assert disp._prefilter_skips({"tool_input_tokens": ("git", "{")}, view) is True


@pytest.mark.parametrize("command", [
    "echo {x",                    # a `{` inside the command still matches a `{` token
    "{a,é} {c,é}",
    "{a,é} reset --hard",
    "a{b,é}c=1",
    "G{IT_DIR,}=/x",
    "g''it push",                 # shlex re-join still applies
    "git   push",
])
def test_tool_input_tokens_match_brace_and_git_shapes(disp, command: str) -> None:
    view = disp._payload_view(_wire(command))
    assert disp._prefilter_skips({"tool_input_tokens": ("git",) + _BRACE}, view) is False


def test_tool_input_tokens_cover_keys_and_nested_values_under_tool_input(disp) -> None:
    payload = json.dumps({"tool_name": "terminal", "tool_input": {"cmds": [{"git": "x"}]}}).encode()
    assert disp._prefilter_skips({"tool_input_tokens": ("git",)}, disp._payload_view(payload)) is False
    nested = json.dumps({"tool_name": "terminal", "tool_input": {"cmds": ["ls", {"a": "git st"}]}}).encode()
    assert disp._prefilter_skips({"tool_input_tokens": ("git",)}, disp._payload_view(nested)) is False


def test_tool_input_tokens_ignore_strings_outside_tool_input(disp) -> None:
    payload = json.dumps({"tool_name": "terminal", "tool_input": {"command": "ls"},
                          "cwd": "/repo/git", "extra": {"note": "git {"}}).encode()
    view = disp._payload_view(payload)
    assert disp._prefilter_skips({"tokens": ("git",)}, view) is False
    assert disp._prefilter_skips({"tool_input_tokens": ("git", "{")}, view) is True


def test_tool_input_tokens_never_skip_without_a_readable_tool_input(disp) -> None:
    decl = {"tool_input_tokens": ("git",)}
    for data in ({"tool_name": "terminal"}, {"tool_name": "terminal", "tool_input": None},
                 {"tool_name": "terminal", "tool_input": "ls"}):
        assert disp._prefilter_skips(decl, disp._payload_view(json.dumps(data).encode())) is False
    assert disp._prefilter_skips(decl, disp._payload_view(_wire("g''it push # '"))) is False  # no split
    assert disp._prefilter_skips(decl, disp._payload_view(b"[1]")) is False  # not an object


@pytest.mark.parametrize("fail_closed", [True, False])
def test_a_tool_input_tokens_skip_is_an_allow_in_every_mode(manager, tmp_path: Path,
                                                            fail_closed: bool) -> None:
    hook = tmp_path / "scoped_blocker.py"
    hook.write_text('HOOK_PREFILTER = {"tool_input_tokens": ["git", "{", ","]}\nimport json, sys\n'
                    'sys.stdin.read()\nprint(json.dumps({"action": "block", "message": "refused"}))\n',
                    encoding="utf-8")
    _register([hook], fail_closed=fail_closed)
    assert _fire(manager, "ls -la") == []
    blocked = [{"action": "block", "message": "refused"}]
    assert _fire(manager, "git push") == blocked
    assert _fire(manager, "{a,é} reset --hard") == blocked


def test_serve_skips_a_declared_miss_without_forking(serve, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    hook = tmp_path / "gitonly.py"
    hook.write_text('HOOK_PREFILTER = {"tokens": ["git"]}\nimport sys\nsys.stdin.read()\n'
                    f'open({str(marker)!r}, "a").write("x")\nprint("{{}}")\n', encoding="utf-8")
    plain = tmp_path / "plain.py"
    plain.write_text('import sys\nsys.stdin.read()\nprint("plain")\n', encoding="utf-8")
    hooks = [{"script": str(hook), "timeout": 30}, {"script": str(plain), "timeout": 30}]
    rows = serve.ask(hooks, _payload("ls -la"))
    assert rows[0] == {"i": 0, "rc": 0, "out": b"{}", "err": b"", "timed_out": False, "elapsed": 0.0,
                       "skipped": True}
    assert rows[1]["out"] == b"plain\n" and "skipped" not in rows[1]  # undeclared: runs as today
    assert not marker.exists()
    rows = serve.ask(hooks, _payload("git status"))
    assert rows[0]["rc"] == 0 and "skipped" not in rows[0] and marker.read_text() == "x"


def test_bench_script_runs_and_reports_both_sides() -> None:
    bench = Path(shell_hooks_batch.DISPATCHER).parent.parent / "scripts" / "bench_shell_hooks.py"
    proc = subprocess.run([sys.executable, str(bench), "--synthetic", "--n", "2", "--warmup", "1",
                           "--before", shell_hooks_batch.DISPATCHER], capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hooks"] == 15
    for side in ("before", "after"):
        assert out[side]["calls"] == 2 and out[side]["hook_rows_nonzero_rc"] == 0
        assert len(out[side]["hook_mean_ms"]) == 15


# --- through invoke_hook: verdicts and the timing log --------------------------------------------

@pytest.fixture
def manager(monkeypatch, tmp_path: Path):
    from hermes_cli import plugins
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shell_hooks.reset_for_tests()
    shell_hooks_batch.close_dispatchers()
    mgr = plugins.PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins, "_plugin_manager", mgr)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: mgr)
    yield mgr
    shell_hooks.reset_for_tests()
    shell_hooks_batch.close_dispatchers()


def _register(paths: list, *, fail_closed: bool) -> None:
    entries = [{"command": f"{sys.executable} {p}", "matcher": "terminal", "timeout": 30,
                "fail_closed": fail_closed} for p in paths]
    cfg = {"hooks": {"batch_python_hooks": True, "pre_tool_call": entries}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == len(paths)


def _fire(mgr, command: str) -> list:
    return mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={"command": command},
                           session_id="s", tool_call_id="c1", turn_id="t1")


@pytest.mark.parametrize("fail_closed", [True, False])
def test_a_skipped_hook_is_an_allow_in_every_mode(manager, tmp_path: Path, fail_closed: bool) -> None:
    hook = tmp_path / "blocker.py"
    hook.write_text('HOOK_PREFILTER = {"tokens": ["git"]}\nimport json, sys\nsys.stdin.read()\n'
                    'print(json.dumps({"action": "block", "message": "git refused"}))\n', encoding="utf-8")
    _register([hook], fail_closed=fail_closed)
    assert _fire(manager, "ls -la") == []
    assert _fire(manager, "git push") == [{"action": "block", "message": "git refused"}]


def test_each_dispatcher_run_logs_per_hook_milliseconds(manager, monkeypatch, tmp_path: Path) -> None:
    logdir = tmp_path / "hook-dispatch"
    monkeypatch.setattr(shell_hooks_batch, "_latency_dir", lambda: str(logdir))
    slow = tmp_path / "slow hook.py"
    slow.write_text('import sys, time\nsys.stdin.read()\ntime.sleep(0.3)\nprint("{}")\n', encoding="utf-8")
    fast = tmp_path / "fast.py"
    fast.write_text('import sys\nsys.stdin.read()\nprint("{}")\n', encoding="utf-8")
    gated = tmp_path / "gated.py"
    gated.write_text('HOOK_PREFILTER = {"tokens": ["git"]}\nimport sys\nsys.stdin.read()\nprint("{}")\n',
                     encoding="utf-8")
    entries = [{"command": f"{sys.executable} '{slow}'", "matcher": "terminal", "timeout": 30}] + [
        {"command": f"{sys.executable} {p}", "matcher": "terminal", "timeout": 30} for p in (fast, gated)]
    cfg = {"hooks": {"batch_python_hooks": True, "pre_tool_call": entries}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 3
    assert _fire(manager, "ls") == []
    deadline = time.monotonic() + 10
    files = []
    while time.monotonic() < deadline and not files:  # written after the callbacks are released
        files = list(logdir.glob("*.tsv")) if logdir.exists() else []
        time.sleep(0.05)
    assert len(files) == 1 and files[0].name == time.strftime("%Y%m%d%H", time.gmtime()) + ".tsv"
    (line,) = files[0].read_text(encoding="utf-8").splitlines()
    ts, total_ms, slowest, cells = line.split(" ")
    assert ts.isdigit() and total_ms.isdigit() and int(total_ms) >= 300
    assert slowest == "slow_hook"
    per_hook = dict(cell.split("=") for cell in cells.split(","))
    assert set(per_hook) == {"slow_hook", "fast", "gated"} and per_hook["gated"] == "skip"
    assert int(per_hook["slow_hook"]) >= 300 and int(per_hook["fast"]) < int(per_hook["slow_hook"])


def test_timing_log_follows_the_calling_profile_home(manager, tmp_path: Path) -> None:
    """Prism fadc5208e248: the reader thread does not inherit ContextVars, so the log directory is
    resolved in the caller's scope; a routed profile's row lands in its own home, not the launch one."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    launch = Path(os.environ["HERMES_HOME"])
    routed = tmp_path / "routed-home"
    routed.mkdir()
    hook = tmp_path / "ok.py"
    hook.write_text('import sys\nsys.stdin.read()\nprint("{}")\n', encoding="utf-8")
    _register([hook], fail_closed=True)
    token = set_hermes_home_override(routed)
    try:
        assert _fire(manager, "ls") == []
    finally:
        reset_hermes_home_override(token)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not list((routed / "logs" / "hook-dispatch").glob("*.tsv")):
        time.sleep(0.05)
    assert len(list((routed / "logs" / "hook-dispatch").glob("*.tsv"))) == 1
    assert not (launch / "logs" / "hook-dispatch").exists()


def test_bench_isolates_home_for_hooks_that_use_path_home(tmp_path: Path) -> None:
    """Prism 96f2a41718c6: the default scratch home covers HOME too, so Path.home() writes stay there."""
    bench = Path(shell_hooks_batch.DISPATCHER).parent.parent / "scripts" / "bench_shell_hooks.py"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "home_writer.py").write_text(
        'import sys\nfrom pathlib import Path\nsys.stdin.read()\n'
        'Path.home().joinpath(".bench-probe").write_text("bench")\nprint("{}")\n', encoding="utf-8")
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    env = dict(os.environ, HOME=str(fake_home))
    proc = subprocess.run([sys.executable, str(bench), "--hooks-dir", str(hooks), "--n", "1", "--warmup", "0",
                           "--before", shell_hooks_batch.DISPATCHER], capture_output=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    assert not (fake_home / ".bench-probe").exists()
