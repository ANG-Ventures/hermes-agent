"""Rename wave 3 step 6 (t_bab23582, hermes-home#13696 / t_5931d620): every deprecated-name-reads line the fork
writes names its reader (ppid, argv, parent, cwd), and a clock-shifted process writes no row.

Writers covered: the three byte-vendored fleet_placement copies (env:PLACEMENT_HOME), the fork's own
allocation_policy.count_deprecated_read (module/file names), and the `kanban placement` deprecated verb.
"""
from __future__ import annotations

import os
import sys

import pytest

from hermes_cli import allocation_policy as ap
from hermes_cli import kanban_db as kb
from hermes_cli.fleet_placement import admission_demand, alloc_client, placement

COPIES = [placement, admission_demand, alloc_client]


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
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3", "-I", "-c", "probe", "ignored"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _logs(env):
    """Lines of every deprecated-name-reads.log under the sandbox (vendored copies: $HERMES_HOME; fork: kanban home)."""
    paths = {env / "vh" / "var" / "ci-box" / "deprecated-name-reads.log",
             kb.kanban_home() / "var" / "ci-box" / "deprecated-name-reads.log"}
    return [ln for p in paths if p.exists() for ln in p.read_text(encoding="utf-8").splitlines()]


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
    assert f[3] == f"ppid={os.getppid()}" and f[4] == "argv=python3+-c", line
    assert f[5].startswith("parent=") and f[5] != "parent=-" and f[6] == f"cwd={env}", line
    assert len(f) == 7, line


SECRETS = ("ghp_T5931SECRETtoken0123456789", "x:hunter2secret@host", "FOO=envsecret")


@pytest.mark.parametrize("argv,want", [
    (["/usr/bin/python3", "-u", "{script}", "--token", SECRETS[0], f"https://{SECRETS[1]}/r.git"], "python3+{script}"),
    (["/opt/bin/python3.13", "-m", "pkg.mod", "--token", SECRETS[0]], "python3.13+-m+pkg.mod"),
    (["/bin/sh", "-c", f"{SECRETS[2]} hermes kanban placement --token {SECRETS[0]}"], "sh+-c"),
    (["/usr/bin/curl", "-H", f"Authorization: token {SECRETS[0]}", f"https://{SECRETS[1]}/r"], "curl"),
], ids=["script", "module", "dash-c", "no-script"])
@pytest.mark.parametrize("name,call,label", WRITERS, ids=[w[0] for w in WRITERS])
def test_counter_line_records_names_never_argument_values(env, monkeypatch, name, call, label, argv, want):
    """Prism 918779febe18 / Apollo 11-20: a command line can carry a credential (--token <v>, a URL password, an
    env-prefix or a `-c` body). The line records the executable basename, then `-c`, `-m <module>` or a script path."""
    script = env / "reader.py"
    script.write_text("")
    argv = [a.replace("{script}", str(script)) for a in argv]
    want = want.replace("{script}", str(script))
    monkeypatch.setattr(sys, "orig_argv", argv)
    before = set(_logs(env))
    call(env, monkeypatch)
    (line,) = [ln for ln in _logs(env) if ln not in before]
    assert f" argv={want} " in line, line
    for s in SECRETS:
        assert s.split("=")[-1] not in line, line


def test_parent_command_line_values_never_reach_the_line(tmp_path):
    """The parent side through a real process tree, on the deprecated verb's path:
    `sh -c 'FOO=<secret> python3 -c <count the verb> --token <secret>'`."""
    import shlex
    import subprocess
    code = "from hermes_cli import kanban as k; k._count_deprecated_verb('placement', 'allocation')"
    inner = f"{SECRETS[2]} {shlex.quote(sys.executable)} -c {shlex.quote(code)} --token {SECRETS[0]}; :"
    envv = {k: v for k, v in os.environ.items() if k not in ("HERMES_CLOCK_SHIFT_DAYS", "FAKETIME")}
    envv.pop("HERMES_KANBAN_SANDBOX", None)
    envv["HERMES_HOME"] = envv["HERMES_KANBAN_HOME"] = str(tmp_path / "vh")
    envv["PYTHONPATH"] = os.pathsep.join([str(__import__("pathlib").Path(__file__).resolve().parents[2])]
                                         + ([envv["PYTHONPATH"]] if envv.get("PYTHONPATH") else []))
    (tmp_path / "vh").mkdir()
    subprocess.run(["sh", "-c", inner], env=envv, cwd=tmp_path, check=True, timeout=120, capture_output=True)
    lines = [ln for p in tmp_path.rglob("deprecated-name-reads.log") for ln in p.read_text().splitlines()]
    assert len(lines) == 1, lines
    assert " cli:kanban placement " in lines[0], lines
    assert f" argv={os.path.basename(sys.executable)}+-c " in lines[0] and " parent=sh+-c " in lines[0], lines
    for s in SECRETS:
        assert s.split("=")[-1] not in lines[0], lines


@pytest.mark.parametrize("var,val", [("HERMES_CLOCK_SHIFT_DAYS", "45"), ("FAKETIME", "+400d")])
@pytest.mark.parametrize("name,call,label", WRITERS, ids=[w[0] for w in WRITERS])
def test_clock_shifted_process_never_counts(env, monkeypatch, name, call, label, var, val):
    monkeypatch.setenv(var, val)
    before = _logs(env)
    call(env, monkeypatch)
    assert _logs(env) == before


def test_vendored_reader_tag_is_the_fork_reader_tag():
    """The fork's copy of the helper stays the vendored text (one shape of line in one log)."""
    import inspect
    body = inspect.getsource(placement._reader_tag).split('"""')[-1]
    assert inspect.getsource(ap._reader_tag).split('"""')[-1] == body
