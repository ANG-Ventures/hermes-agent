"""A file-name search rooted at a heavy root is refused before any walk (t_31e5ab30)."""

import os

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_search import HEAVY_FILENAME_ROOTS_ENV


class RecordingEnvironment:
    is_local = False
    cwd = "/repo"

    def __init__(self):
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("test -e "):  # a spaced path is a root list: the whole string does not exist
            return {"output": "not_found\n" if " /" in command[8:] else "exists\n", "returncode": 0}
        if command.startswith("command -v rg"):
            return {"output": "/usr/bin/rg\n", "returncode": 0}
        if "--version" in command:
            return {"output": "ripgrep 14.1.1\n", "returncode": 0}
        if "--files" in command:
            return {"output": "/repo/x.py\n", "returncode": 0}
        return {"output": "", "returncode": 1}

    @property
    def walks(self):
        return [c for c in self.commands if "--files" in c or c.startswith("set -o pipefail; find")]


@pytest.mark.parametrize("path", ["/Volumes/fleet-scratch/tmp", "/Volumes/fleet-scratch/tmp/", "/Volumes/fleet-scratch",
                                  "/", "/Volumes/fleet-scratch/tmp/../tmp", "/repo /Volumes/fleet-scratch/tmp"])
def test_heavy_root_refused_without_a_walk(path, monkeypatch):
    monkeypatch.delenv(HEAVY_FILENAME_ROOTS_ENV, raising=False)
    env = RecordingEnvironment()
    result = ShellFileOperations(env).search("*review_probe.py", path=path, target="files")
    assert result.error and result.error.startswith("File search refused:"), result.error
    assert "HERMES_FULL_WALK=1 rg --files" in result.error and f"{HEAVY_FILENAME_ROOTS_ENV}=off" in result.error
    assert env.walks == []


@pytest.mark.parametrize("path", ["/Volumes/fleet-scratch/tmp/read-409", "/Volumes/fleet-scratch/workspaces/default/t_1",
                                  "/repo", "."])
def test_narrower_root_still_walks(path, monkeypatch):
    monkeypatch.delenv(HEAVY_FILENAME_ROOTS_ENV, raising=False)
    env = RecordingEnvironment()
    result = ShellFileOperations(env).search("*.py", path=path, target="files")
    assert not result.error, result.error
    assert len(env.walks) == 1


@pytest.mark.parametrize("value", ["off", "OFF", ""])
def test_off_switch_walks_the_heavy_root(value, monkeypatch):
    monkeypatch.setenv(HEAVY_FILENAME_ROOTS_ENV, value)
    env = RecordingEnvironment()
    result = ShellFileOperations(env).search("*.py", path="/Volumes/fleet-scratch/tmp", target="files")
    assert not result.error, result.error
    assert len(env.walks) == 1


def test_env_list_replaces_the_default(monkeypatch):
    monkeypatch.setenv(HEAVY_FILENAME_ROOTS_ENV, "/data")
    env = RecordingEnvironment()
    ops = ShellFileOperations(env)
    assert ops.search("*.py", path="/data", target="files").error.startswith("File search refused:")
    assert not ops.search("*.py", path="/Volumes/fleet-scratch/tmp", target="files").error


def test_content_search_is_not_refused(monkeypatch):
    monkeypatch.delenv(HEAVY_FILENAME_ROOTS_ENV, raising=False)
    env = RecordingEnvironment()
    result = ShellFileOperations(env).search("needle", path="/Volumes/fleet-scratch/tmp", target="content")
    assert not (result.error or "").startswith("File search refused:")


def test_local_home_entry_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(HEAVY_FILENAME_ROOTS_ENV, "~")
    ops = ShellFileOperations(LocalEnvironment(str(tmp_path)))
    assert ops.search("*.py", path=str(tmp_path), target="files").error.startswith("File search refused:")
    (tmp_path / "sub").mkdir()
    assert not (ops.search("*.py", path=str(tmp_path / "sub"), target="files").error or "").startswith("File search refused:")


def test_remote_backend_skips_the_home_entry(monkeypatch):
    monkeypatch.setenv(HEAVY_FILENAME_ROOTS_ENV, os.pathsep.join(["~", "/data"]))
    env = RecordingEnvironment()
    env.cwd = os.path.expanduser("~")
    assert not ShellFileOperations(env).search("*.py", path=env.cwd, target="files").error
