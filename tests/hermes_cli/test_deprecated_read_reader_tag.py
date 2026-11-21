"""Rename wave 3 step 6 (t_bab23582, hermes-home#13696 / t_5931d620): every deprecated-name-reads line the fork
writes names its reader (ppid, argv, parent, cwd), and a clock-shifted process writes no row.

Writers covered: the three byte-vendored fleet_placement copies (env:PLACEMENT_HOME), the fork's own
allocation_policy.count_deprecated_read (module/file names), and the `kanban placement` deprecated verb.

The tag never parses argv (Apollo 11-20 ruling after Prism 918779febe18 / 86eec040fec9): own process = the
executable's basename + sys.argv[0]; parent = its executable name only; cwd.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_cli import allocation_policy as ap
from hermes_cli import kanban_db as kb
from hermes_cli.fleet_placement import admission_demand, alloc_client, placement

COPIES = [placement, admission_demand, alloc_client]
REPO = Path(__file__).resolve().parents[2]
PY = os.path.basename(sys.executable)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vh = tmp_path / "vh"
    vh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(vh))
    monkeypatch.delenv("ALLOCATION_HOME", raising=False)
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.delenv("HERMES_CLOCK_SHIFT_DAYS", raising=False)
    monkeypatch.delenv("FAKETIME", raising=False)
    for mod in COPIES:
        monkeypatch.setattr(mod, "_OLD_ENV_COUNTED", set())
    monkeypatch.setattr(sys, "argv", ["/srv/x/reader.py", "--token", "ignored"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _logs(env):
    """Lines of every deprecated-name-reads.log under the sandbox (vendored copies: $HERMES_HOME; fork: kanban home)."""
    paths = {env / "vh" / "var" / "ci-box" / "deprecated-name-reads.log",
             kb.kanban_home() / "var" / "ci-box" / "deprecated-name-reads.log"}
    return [ln for p in paths if p.exists() for ln in p.read_text(encoding="utf-8-sig").splitlines()]


def _copy_writer(mod):
    def call(env, monkeypatch):
        monkeypatch.setenv("PLACEMENT_HOME", str(env / "old"))
        assert mod._allocation_home_env() == str(env / "old")
    return call


def _ap_writer(env, monkeypatch):
    ap.count_deprecated_read("module:hermes_cli.placement_policy")


def _verb_writer(env, monkeypatch):
    from hermes_cli import kanban as kcli
    kcli._count_deprecated_verb("placement", "allocation")


WRITERS = [(m.__name__.rsplit(".", 1)[1], _copy_writer(m), "env:PLACEMENT_HOME") for m in COPIES] + [
    ("allocation_policy", _ap_writer, "module:hermes_cli.placement_policy"),
    ("kanban_placement_verb", _verb_writer, "cli:kanban+placement"),
]


@pytest.mark.parametrize("name,call,label", WRITERS, ids=[w[0] for w in WRITERS])
def test_counter_line_names_the_reader(env, monkeypatch, name, call, label):
    before = set(_logs(env))
    call(env, monkeypatch)
    (line,) = [ln for ln in _logs(env) if ln not in before]
    # `cli:kanban placement` keeps its space (field 1-2); normalise it so the field checks line up.
    f = line.replace("cli:kanban placement", "cli:kanban+placement").split(" ")
    assert f[1] == label and f[2] == f"pid={os.getpid()}", line
    assert f[3] == f"ppid={os.getppid()}" and f[4] == f"argv={PY}+/srv/x/reader.py", line
    assert f[5].startswith("parent=") and f[5] != "parent=-" and "+" not in f[5] and f[6] == f"cwd={env}", line
    assert len(f) == 7, line


TOKEN = "ExampleSecret_123"
SECRETS = (TOKEN, "ghp" + "_T5931D620FAKEFAKEFAKEFAKE01", "x:hunter2secret@host", "FOO=envsecret")
READERS = {   # one real reader process per writer kind
    "kanban_placement_verb": "from hermes_cli import kanban as k; k._count_deprecated_verb('placement', 'allocation')",
    "placement": "from hermes_cli.fleet_placement import placement as p; p._allocation_home_env()",
    "allocation_policy": "from hermes_cli import allocation_policy as a; "
                         "a.count_deprecated_read('module:hermes_cli.placement_policy')",
}


def _run_reader(tmp_path, cmd, stdin=None):
    """Run a real reader with an isolated home; return its one counter line."""
    import subprocess
    envv = {k: v for k, v in os.environ.items()
            if k not in ("HERMES_CLOCK_SHIFT_DAYS", "FAKETIME", "ALLOCATION_HOME")}
    envv["HERMES_HOME"] = envv["HERMES_KANBAN_HOME"] = str(tmp_path / "vh")
    envv["PLACEMENT_HOME"] = str(tmp_path / "old")
    envv["PYTHONPATH"] = os.pathsep.join([str(REPO), str(tmp_path)] + ([envv["PYTHONPATH"]] if envv.get("PYTHONPATH") else []))
    (tmp_path / "vh").mkdir(exist_ok=True)
    subprocess.run(cmd, env=envv, cwd=tmp_path, input=stdin, text=True, encoding="utf-8", check=True, timeout=120,
                   capture_output=True)
    lines = [ln for p in tmp_path.rglob("deprecated-name-reads.log") for ln in p.read_text(encoding="utf-8-sig").splitlines()]
    assert len(lines) == 1, lines
    return lines[0]


OWN_CASES = {   # argv builder, stdin builder, expected argv= field
    "stdin-dash-m-token": (lambda t, c: [sys.executable, "-", "-m", TOKEN], lambda c: c, f"{PY}+-"),
    "dash-c-program-text": (lambda t, c: [sys.executable, "-c", f"{c}  # {SECRETS[1]}", "--token", TOKEN],
                            None, f"{PY}+-c"),
    "script-then-token-flag": (lambda t, c: [sys.executable, str(t / "reader.py"), "--token", SECRETS[1]],
                               None, f"{PY}+{{tmp}}/reader.py"),
    "script-then-token-positional": (lambda t, c: [sys.executable, str(t / "reader.py"), TOKEN,
                                                   f"https://{SECRETS[2]}/r.git"], None, f"{PY}+{{tmp}}/reader.py"),
    "module-then-token": (lambda t, c: [sys.executable, "-m", "t_reader_mod", "-m", TOKEN], None,
                          f"{PY}+{{tmp}}/t_reader_mod.py"),
}


@pytest.mark.parametrize("case", sorted(OWN_CASES))
@pytest.mark.parametrize("reader", sorted(READERS))
def test_own_argv_values_never_reach_the_line(tmp_path, reader, case):
    """Prism 918779febe18 / 86eec040fec9: `python3 - -m <token>`, `-c <program text>`, a script followed by
    `--token <token>` or a token-shaped positional, `-m mod -m <token>`: none of the token text is logged."""
    code = READERS[reader]
    (tmp_path / "reader.py").write_text(code + "\n", encoding="utf-8")
    (tmp_path / "t_reader_mod.py").write_text(code + "\n", encoding="utf-8")
    argv, stdin, want = OWN_CASES[case]
    line = _run_reader(tmp_path, argv(tmp_path, code), stdin(code) if stdin else None)
    assert f" argv={want.format(tmp=tmp_path)} " in line, line
    for s in SECRETS:
        assert s not in line and s.split("=")[-1] not in line and "hunter2" not in line, line


@pytest.mark.parametrize("reader", sorted(READERS))
def test_parent_is_its_executable_name_only(tmp_path, reader):
    """The parent side through a real process tree: `sh -s -m <token>` reading a script that runs the reader."""
    import shlex
    script = f"FOO=envsecret {shlex.quote(sys.executable)} -c {shlex.quote(READERS[reader])} --token {TOKEN}\n:\n"
    line = _run_reader(tmp_path, ["sh", "-s", "-m", TOKEN], stdin=script)
    assert f" argv={PY}+-c " in line and any(f" parent={p} " in line for p in ("sh", "dash", "bash")), line
    for s in SECRETS:
        assert s not in line and s.split("=")[-1] not in line, line


@pytest.mark.parametrize("var,val", [("HERMES_CLOCK_SHIFT_DAYS", "45"), ("FAKETIME", "+400d")])
@pytest.mark.parametrize("name,call,label", WRITERS, ids=[w[0] for w in WRITERS])
def test_clock_shifted_process_never_counts(env, monkeypatch, name, call, label, var, val):
    monkeypatch.setenv(var, val)
    before = _logs(env)
    call(env, monkeypatch)
    assert _logs(env) == before


def test_vendored_reader_tag_is_the_fork_reader_tag():
    """The fork's helper is the vendored text, docstring included (one shape of line in one log)."""
    import inspect
    texts = {m.__name__: inspect.getsource(m._reader_tag) for m in COPIES + [ap]}
    assert len(set(texts.values())) == 1, sorted(texts)
