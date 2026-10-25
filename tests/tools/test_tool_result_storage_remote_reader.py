"""A spill stub must name a path the RECEIVING agent can open (t_ad44f109).

Measured on t_94e91559: a dispatcher-placed pool worker (agent on the Studio, tools over
``TERMINAL_ENV=ssh`` on ACE-MEDIA, ``TERMINAL_SSH_SYNC_FILES=false``) read its card as its FIRST
tool call. No terminal env existed yet, so ``get_active_env`` returned None, the storage layer
took the host-side branch and handed the worker a Studio ``$HERMES_HOME/cache/spillover`` path
that its ssh-backed read_file could not open.
"""

import os
import subprocess
import sys

import pytest

from tools.tool_result_storage import PERSISTED_OUTPUT_TAG, extract_persisted_path, maybe_persist_tool_result

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash-backed fake remote")


class _FakeRemoteEnv:
    """A remote-like backend: commands run in bash with a DIFFERENT home and temp dir than
    the writer process, the way an ssh pool host does."""

    def __init__(self, reader_root):
        self.home = reader_root / "home"
        self.tmp = reader_root / "tmp"
        self.home.mkdir(parents=True)
        self.tmp.mkdir(parents=True)

    def get_temp_dir(self):
        return str(self.tmp)

    def execute(self, cmd, timeout=None, stdin_data=None, **_kw):
        r = subprocess.run(["bash", "-c", cmd], input=stdin_data or "", capture_output=True,
                           text=True, timeout=timeout, env={**os.environ, "HOME": str(self.home)})
        return {"output": r.stdout + r.stderr, "returncode": r.returncode}

    def read_file(self, path):
        r = self.execute(f"cat -- {path!r}")
        return r["output"] if r["returncode"] == 0 else None


@pytest.fixture
def writer_home(tmp_path, monkeypatch):
    home = tmp_path / "writer" / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    import tools.tool_result_storage as trs
    monkeypatch.setattr(trs, "_spillover_pruned_homes", set())
    return home


def _content():
    return "HEAD-MARKER\n" + ("x" * 79 + "\n") * 2000 + "TAIL-MARKER newest review\n"


def test_no_env_yet_on_remote_backend_never_names_a_writer_side_path(writer_home):
    """First tool call of a placed worker: env=None, backend ssh. RED at main (stub named the
    writer's spillover path)."""
    content = _content()
    result = maybe_persist_tool_result(content=content, tool_name="kanban_show",
                                       tool_use_id="toolu_first", env=None, threshold=30_000)
    assert str(writer_home) not in result
    assert extract_persisted_path(result) is None
    # Bounded inline head+tail preview that says so.
    assert "HEAD-MARKER" in result and "TAIL-MARKER newest review" in result
    assert "not saved" in result
    assert len(result) < len(content) // 2


def test_remote_env_stub_path_is_readable_by_the_reader(writer_home, tmp_path):
    """With the env up, the stub's path must open on the reader's side."""
    env = _FakeRemoteEnv(tmp_path / "reader")
    content = _content()
    result = maybe_persist_tool_result(content=content, tool_name="kanban_show",
                                       tool_use_id="toolu_second", env=env, threshold=30_000)
    assert PERSISTED_OUTPUT_TAG in result
    path = extract_persisted_path(result)
    assert path and str(writer_home) not in path
    assert env.read_file(path) == content


def test_local_backend_without_env_keeps_host_spill(tmp_path, monkeypatch):
    """No regression for the plain local case: the host path is the reader's path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    import tools.tool_result_storage as trs
    monkeypatch.setattr(trs, "_spillover_pruned_homes", set())
    result = maybe_persist_tool_result(content=_content(), tool_name="kanban_show",
                                       tool_use_id="toolu_local", env=None, threshold=30_000)
    path = extract_persisted_path(result)
    assert path and os.access(path, os.R_OK)
