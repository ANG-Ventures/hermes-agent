"""The capture router's secret read must reach the real `op`, not the agent-session shim.

Fleet agent processes put ``var/gh-shim`` first on PATH; its ``op`` refuses ``read`` with exit 77
under HERMES_AGENT. The gateway's mem0 capture router is not a tool call, and when it hit the shim
both extraction legs went out with an empty bearer token (401 on every capture).
"""
import os
import stat

import pytest

from plugins.memory.mem0.capture_router import BridgeExtractor

pytestmark = pytest.mark.platforms("posix")


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def shim_then_real(tmp_path, monkeypatch):
    shim = tmp_path / "var" / "gh-shim"
    real = tmp_path / "bin"
    shim.mkdir(parents=True)
    real.mkdir()
    _script(shim / "op", "echo 'op: REFUSED under an agent session' >&2; exit 77")
    _script(real / "op", 'echo "secret-for-$2"')
    monkeypatch.delenv("OP_BIN", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim), str(real), "/usr/bin", "/bin"]))
    return shim, real


def test_op_read_skips_gh_shim_and_reaches_real_binary(shim_then_real):
    assert BridgeExtractor._op_read("op://V/item/field") == "secret-for-op://V/item/field"


def test_op_bin_override_wins(shim_then_real, tmp_path, monkeypatch):
    pinned = tmp_path / "pinned-op"
    _script(pinned, 'echo "pinned-$2"')
    monkeypatch.setenv("OP_BIN", str(pinned))
    assert BridgeExtractor._op_read("op://V/i/f") == "pinned-op://V/i/f"


def test_only_shim_on_path_still_fails_closed(tmp_path, monkeypatch):
    shim = tmp_path / "var" / "gh-shim"
    shim.mkdir(parents=True)
    _script(shim / "op", "exit 77")
    monkeypatch.delenv("OP_BIN", raising=False)
    monkeypatch.setenv("PATH", str(shim))
    assert BridgeExtractor._op_read("op://V/i/f") == ""
