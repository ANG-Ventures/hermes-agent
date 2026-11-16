"""``Board.env`` must not hand a child process the operator's GitHub auth (papercut pc-c682bf78).

The scratch HOME alone is not enough: an inherited ``GH_CONFIG_DIR`` points ``gh`` back at the
operator's config (and, on macOS, the login-keychain token that config names), and a fleet ``gh``
wrapper on PATH mints a live token without reading HOME at all. A survivor-pr worker spawned by this
harness could then hit real GitHub. The env pins a scratch ``GH_CONFIG_DIR``, drops every token var,
and puts a stub ``gh`` first on PATH that exits non-zero, unless the test opts in.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from tests.e2e.core.kanban._helpers import GH_STUB_RC, Board

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="harness is Linux-gated")

_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


@pytest.fixture
def operator_gh(tmp_path, monkeypatch):
    """The parent env of an operator with gh auth: a config dir holding a token, and token vars."""
    cfg = tmp_path / "operator-gh"
    cfg.mkdir()
    (cfg / "hosts.yml").write_text(
        "github.com:\n    oauth_token: gho_operatorleak\n    user: operator\n    git_protocol: https\n",
        encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(cfg))
    for var in _TOKEN_VARS:
        monkeypatch.setenv(var, "ghp_operatorleak")
    return cfg


def _gh(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("gh", path=env["PATH"])
    assert exe, "no gh on the child PATH"
    return subprocess.run([exe, *args], env=env, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, cwd=env["HOME"])


def test_env_pins_scratch_gh_config_and_drops_tokens(tmp_path, operator_gh):
    board = Board(root=tmp_path / "board", base_url="http://127.0.0.1:9/v1")
    env = board.env()
    assert env["GH_CONFIG_DIR"].startswith(str(board.root)), env.get("GH_CONFIG_DIR")
    assert env["GH_CONFIG_DIR"] != str(operator_gh)
    assert not [v for v in _TOKEN_VARS if env.get(v)], {v: env.get(v) for v in _TOKEN_VARS}


def test_gh_call_from_board_env_cannot_authenticate(tmp_path, operator_gh):
    board = Board(root=tmp_path / "board", base_url="http://127.0.0.1:9/v1")
    env = board.env()
    for args in (("auth", "status"), ("auth", "token"), ("api", "user")):
        proc = _gh(env, *args)
        out = proc.stdout + proc.stderr
        assert proc.returncode == GH_STUB_RC, f"gh {' '.join(args)} rc={proc.returncode}\n{out}"
        assert "operatorleak" not in out
        assert "e2e Board blocks real gh" in out


def test_opt_in_drops_the_stub_but_keeps_auth_scratch(tmp_path, operator_gh):
    board = Board(root=tmp_path / "board", base_url="http://127.0.0.1:9/v1", allow_real_gh=True)
    env = board.env()
    exe = shutil.which("gh", path=env["PATH"])
    assert exe is None or not exe.startswith(str(board.root)), exe
    assert env["GH_CONFIG_DIR"].startswith(str(board.root))
    assert not [v for v in _TOKEN_VARS if env.get(v)]
