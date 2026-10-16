"""scripts/mem0_host_literal_lint.py (PRD-portability AC5, card t_c0586226): red on a planted mem0 literal,
green after removal, allow-list needs a reason. Hermetic tmp git repos; no network."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

LINT = Path(__file__).resolve().parents[2] / "scripts" / "mem0_host_literal_lint.py"
LAN = ".".join(["192", "168", "1", "18"])   # assembled so this file never reads as a literal itself


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _repo(tmp_path, files):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=_env())
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, env=_env())
    return tmp_path


def _run(repo, allow=None):
    a = repo / "allow.txt"
    a.write_text(allow or "", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    return subprocess.run([sys.executable, str(LINT), "--repo", str(repo), "--allow", str(a)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())


def test_planted_lan_literal_is_red_then_green_after_removal(tmp_path):
    repo = _repo(tmp_path, {"svc/client.py": f'MEM0_API_URL = "http://{LAN}:8890"\n'})
    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "FAIL svc/client.py:1" in r.stdout
    (repo / "svc" / "client.py").write_text('MEM0_API_URL = "https://mem0.ace"\n', encoding="utf-8")
    r = _run(repo)
    assert r.returncode == 0, r.stdout
    assert "MEM0_HOST_LITERAL=ok 0" in r.stdout


def test_loopback_env_and_mem0_json_host_shapes(tmp_path):
    repo = _repo(tmp_path, {
        "deploy/env.sh": "export MEM0_HOST=http://127.0.0.1:8888\n",
        "config/mem0.json": '{\n  "host": "http://localhost:8888"\n}\n',
        "other/app.py": 'URL = "http://127.0.0.1:8080"  # some other service\n',
    })
    out = _run(repo).stdout
    assert "FAIL deploy/env.sh:1" in out and "FAIL config/mem0.json:2" in out, out
    assert "other/app.py" not in out


def test_tests_fixtures_noqa_and_fleet_name_pass(tmp_path):
    repo = _repo(tmp_path, {
        "tests/test_x.py": "MEM0_HOST='http://127.0.0.1:1'\n",
        "pkg/fixtures/mem0.json": '{"host": "http://127.0.0.1:1"}\n',
        "pkg/test_y.py": "MEM0_HOST='http://127.0.0.1:1'\n",
        "pkg/z.py": "MEM0_HOST = 'http://127.0.0.1:1'  # noqa: mem0-host-literal\nMEM0_HOST = 'https://mem0.ace'\n",
    })
    r = _run(repo)
    assert r.returncode == 0, r.stdout


def test_allow_list_needs_a_reason_and_reports_stale_entries(tmp_path):
    repo = _repo(tmp_path, {"docs/setup.md": "mem0 --host http://localhost:8888\n"})
    assert _run(repo).returncode == 1
    r = _run(repo, "docs/setup.md upstream default in a generic example\n")
    assert r.returncode == 0, r.stdout
    r = _run(repo, "docs/setup.md\n")
    assert r.returncode == 2 and "has no reason" in r.stderr
    r = _run(repo, "docs/setup.md generic example\ngone.py removed file\n")
    assert r.returncode == 0 and "INFO allow entry gone.py has no hit" in r.stdout


def test_no_git_tree_is_graded_by_walk_not_parent_checkout(tmp_path):
    """Off-box CI ships `git archive | tar`: no .git. The lint must grade that tree (a planted literal
    is red) and must not borrow a parent checkout's ls-files (GIT_CEILING_DIRECTORIES)."""
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True, env=_env())
    inner = outer / "tree"
    (inner / "scripts").mkdir(parents=True)
    (inner / "scripts" / "clean.py").write_text("HOST = os.environ['MEM0_HOST']\n", encoding="utf-8")
    a = inner / "allow.txt"
    a.write_text("", encoding="utf-8")
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(inner), "--allow", str(a)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MEM0_HOST_LITERAL=ok" in r.stdout, r.stdout
    (inner / "scripts" / "bad.py").write_text("MEM0_HOST = 'http://192.168.1.18:8890'\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(inner), "--allow", str(a)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 1, r.stdout + r.stderr
    assert "scripts/bad.py" in r.stdout, r.stdout


def _unreadable(path):
    """A tracked file whose read fails with an OSError other than not-found, as root too: a symlink to
    itself (ELOOP). chmod 000 would not do, root reads through it."""
    os.symlink(path.name, path)


@pytest.mark.platforms("posix")
def test_unreadable_tracked_file_is_an_error_not_ok(tmp_path):
    """Prism 1b16ed63fb04 (t_8b70c8bb): a read error was swallowed and the tree certified clean."""
    repo = _repo(tmp_path, {"svc/clean.py": "HOST = 'https://mem0.ace'\n"})
    _unreadable(repo / "svc" / "mem0_client.py")
    r = _run(repo)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "MEM0_HOST_LITERAL=error" in r.stderr, r.stderr
    assert "MEM0_HOST_LITERAL=ok" not in r.stdout, r.stdout


def test_tracked_but_deleted_file_is_still_skipped(tmp_path):
    repo = _repo(tmp_path, {"svc/clean.py": "HOST = 'https://mem0.ace'\n", "svc/gone.py": "x = 1\n"})
    a = repo / "allow.txt"
    a.write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    (repo / "svc" / "gone.py").unlink()
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(repo), "--allow", str(a)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MEM0_HOST_LITERAL=ok" in r.stdout, r.stdout


@pytest.mark.platforms("posix")
def test_no_git_tree_unreadable_file_or_dir_is_an_error(tmp_path):
    """Walk fallback: an unreadable file errors (ELOOP), and an unreadable directory is not dropped by
    os.walk's default silent onerror."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    a = tree / "allow.txt"
    a.write_text("", encoding="utf-8")
    _unreadable(tree / "scripts" / "mem0_client.py")
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(tree), "--allow", str(a)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 2, r.stdout + r.stderr
    (tree / "scripts" / "mem0_client.py").unlink()
    if os.geteuid() == 0:
        return   # root lists a 000 directory; the file arm above already ran
    locked = tree / "locked"
    locked.mkdir()
    (locked / "c.py").write_text("MEM0_HOST = 'http://127.0.0.1:8888'\n", encoding="utf-8")
    locked.chmod(0)
    try:
        r = subprocess.run([sys.executable, str(LINT), "--repo", str(tree), "--allow", str(a)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    finally:
        locked.chmod(0o755)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "MEM0_HOST_LITERAL=error" in r.stderr, r.stderr


def test_this_repo_is_green():
    r = subprocess.run([sys.executable, str(LINT)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr
