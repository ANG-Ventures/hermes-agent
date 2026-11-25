"""``file_tools.impact_check``: a .py edit that changes a signature reports the callers it broke
in OTHER files as ``path:line`` inside the same write/patch result (real LocalEnvironment shell,
real git repo). Off by default; never blocks the edit."""

import subprocess

import pytest

import tools.file_operations as fo
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_impact import check_callers, impact_check_enabled, touches_signature

LIB = "def run(a, b=1):\n    return a + b\n\n\ndef keep(x):\n    return x\n"


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "lib.py").write_text(LIB)
    (tmp_path / "pkg" / "user.py").write_text("from pkg.lib import run\n\n\nrun(1, b=2)\n")
    (tmp_path / "pkg" / "rel.py").write_text("from .lib import run as go\n\ngo(1)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lib.py").write_text(
        "import pkg.lib\nfrom pkg import lib as L\n\n\ndef test_it():\n    assert pkg.lib.run(1, 2) == 3\n"
        "    assert L.keep(1) == 1\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


@pytest.fixture()
def ops(repo):
    return ShellFileOperations(LocalEnvironment(cwd=str(repo)), cwd=str(repo))


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(fo, "impact_check_enabled", lambda: True, raising=False)


def test_signature_change_reports_each_broken_caller_with_path_and_line(ops, repo, enabled):
    res = ops.patch_replace(str(repo / "pkg" / "lib.py"), "def run(a, b=1):", "def run(a, *, c):")
    assert res.success
    assert sorted(res.to_dict()["lint"]["broken_callers"]) == [
        "pkg/rel.py:3: TypeError: run() missing required argument(s): 'c'",
        "pkg/user.py:4: TypeError: run() got an unexpected keyword argument 'b'",
        "tests/test_lib.py:6: TypeError: run() takes 1 positional arguments but 2 were given",
    ]
    assert "def run(a, *, c):" in (repo / "pkg" / "lib.py").read_text()  # the edit landed


def test_removed_definition_reports_the_import_and_attribute_use(ops, repo, enabled):
    res = ops.write_file(str(repo / "pkg" / "lib.py"), "def run(a, b=1):\n    return a + b\n")
    assert sorted(res.to_dict()["lint"]["broken_callers"]) == [
        "tests/test_lib.py:7: AttributeError: 'keep' (removed by this edit)"]


def test_compatible_change_reports_nothing(ops, repo, enabled):
    res = ops.patch_replace(str(repo / "pkg" / "lib.py"), "def run(a, b=1):", "def run(a, b=1, *, c=None):")
    assert res.success and "broken_callers" not in res.to_dict()["lint"]


def test_body_only_edit_never_runs_git(ops, repo, enabled, monkeypatch):
    calls = []
    real = ops._exec
    monkeypatch.setattr(ops, "_exec", lambda cmd, **kw: (calls.append(cmd), real(cmd, **kw))[1])
    res = ops.patch_replace(str(repo / "pkg" / "lib.py"), "return a + b", "return b + a")
    assert res.success and "broken_callers" not in res.to_dict()["lint"]
    assert not [c for c in calls if "git" in c]


def test_off_by_default(ops, repo, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    assert impact_check_enabled() is False
    res = ops.patch_replace(str(repo / "pkg" / "lib.py"), "def run(a, b=1):", "def run(a, *, c):")
    assert res.success and "broken_callers" not in res.to_dict()["lint"]
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_tools": {"impact_check": True}})
    assert impact_check_enabled() is True


def test_failure_inside_the_check_never_breaks_the_write(ops, repo, enabled, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("exec down")
    monkeypatch.setattr(ops, "_exec", boom)
    assert check_callers(ops, str(repo / "pkg" / "lib.py"), LIB, "def run(a, *, c):\n    pass\n") == []


ML = "import x\n\n\ndef f(\n    a,\n    b=1,\n):\n    y = a\n    return y\n\n\nclass K:\n    def __init__(self, a):\n        self.a = a\n"


@pytest.mark.parametrize("old,new,touched", [
    ("    y = a\n", "    y = a + 1\n", False),               # body of f
    ("        self.a = a\n", "        self.a = a or 0\n", False),  # body of __init__
    ("import x\n", "import x, z\n", False),               # not a header
    ("    b=1,\n", "    b,\n", True),                       # param line of a multi-line header
    ("    def __init__(self, a):\n", "    def __init__(self, a, b):\n", True),
    ("class K:\n    def __init__(self, a):\n        self.a = a\n", "", True),  # removed class
])
def test_touches_signature_prefilter(old, new, touched):
    assert old in ML
    assert touches_signature(ML, ML.replace(old, new)) is touched
