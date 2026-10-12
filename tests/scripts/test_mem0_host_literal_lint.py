"""scripts/mem0_host_literal_lint.py (PRD-portability AC5, card t_c0586226): red on a planted mem0 literal,
green after removal, allow-list needs a reason. Hermetic tmp git repos; no network."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


def test_this_repo_is_green():
    r = subprocess.run([sys.executable, str(LINT)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr
