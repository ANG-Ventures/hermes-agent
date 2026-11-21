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
    monkeypatch.setattr(sys, "argv", ["/x/test roots.py", "-c", "probe", "ignored"])
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
    assert f[3] == f"ppid={os.getppid()}" and f[4] == "argv=/x/test+roots.py+-c+probe", line
    assert f[5].startswith("parent=") and f[5] != "parent=-" and f[6] == f"cwd={env}", line
    assert len(f) == 7, line


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
