"""Supervisor ownership is local to its interpreter, not inherited child interpreters."""
import json
import os
from types import SimpleNamespace

import pytest
from pathlib import Path
import subprocess
import sys
import venv


def test_foreign_child_drops_checkout_contract(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    parent = root / "venv"
    venv.EnvBuilder(with_pip=False).create(parent)
    child = tmp_path / "child"
    venv.EnvBuilder(with_pip=False).create(child)
    repo = Path(__file__).resolve().parents[2]
    (parent / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages' / 'supervisor_only.py').touch()
    inherited_site = parent / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    code = (f"import sys, os; sys.path.insert(0, {str(repo)!r}); "
            "from pathlib import Path; from pm.environments import activate_dependencies; "
            f"activate_dependencies(Path({str(root)!r})); "
            "assert 'HERMES_CHECKOUT_VENV' not in os.environ; "
            f"assert {str(root)!r} not in sys.path; "
            f"assert {str(inherited_site)!r} not in sys.path; "
            f"assert {str(unrelated)!r} in sys.path; "
            f"assert os.environ['PYTHONPATH'] == {str(unrelated)!r}; "
            "import importlib.util; assert importlib.util.find_spec('supervisor_only') is None; "
            "print('child started')")
    for python in (sys.executable, str(child / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))):
        result = subprocess.run([python, "-c", code],
                                env=dict(os.environ, HERMES_CHECKOUT_VENV=str(parent), HERMES_HOME=str(tmp_path / 'home'),
                                         PYTHONPATH=os.pathsep.join([str(root), str(inherited_site), str(unrelated)])),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "child started"


def test_owned_interpreter_removes_inherited_checkout(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    owned = root / "venv"
    venv.EnvBuilder(with_pip=False).create(owned)
    foreign = tmp_path / "other-checkout"
    foreign.mkdir()
    site = foreign / "venv/lib/python3.14/site-packages"
    site.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[2]
    code = (f"import sys; sys.path.insert(0, {str(repo)!r}); "
            "from pathlib import Path; from pm.environments import activate_dependencies; "
            f"activate_dependencies(Path({str(root)!r})); "
            f"assert {str(foreign)!r} not in sys.path, sys.path; "
            f"assert {str(site)!r} not in sys.path, sys.path")
    result = subprocess.run([str(owned / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')), "-c", code],
                            env=dict(os.environ, HERMES_CHECKOUT_VENV=str(owned), PYTHONPATH=os.pathsep.join([str(foreign), str(site)])),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize("consumer", ["resolver", "lease", "cron"])
def test_checkout_contract_owns_child_dependency_roots(tmp_path, monkeypatch, consumer):
    from pm import environments as paths
    from tools import browser_use_target
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    owned = root / "venv"
    site = paths.site_packages(owned)
    site.mkdir(parents=True)
    (owned / "pyvenv.cfg").write_text(f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = paths.install_state_dir(root)
    stale = state / "environments/stale/venv"
    stale.mkdir(parents=True)
    (stale / "pyvenv.cfg").touch()
    (state / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(stale)}}}))
    monkeypatch.setenv("HERMES_CHECKOUT_VENV", str(owned))
    monkeypatch.setattr(sys, "prefix", str(owned))
    if consumer == "resolver":
        assert paths.committed_venv(root) == owned
        assert paths.selected_venv(root) == owned
        assert paths.project_python(root) == paths.venv_python(owned)
    elif consumer == "cron":
        env = pin_hermes_tree_on_pythonpath({}, root)
        assert env["PYTHONPATH"].split(os.pathsep) == [str(root), str(site)]
    else:
        monkeypatch.setattr(browser_use_target, "__file__", str(root / "tools/browser_use_target.py"))
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='{"pid":42,"reply":{}}')
        monkeypatch.setattr(browser_use_target.subprocess, "run", run)
        route = SimpleNamespace(cdp_url="http://fixture.invalid", ready=True, env={})
        assert browser_use_target._daemon_request(route, {"meta": "lease_check"}) == (42, {})
        assert json.loads(calls[0][-1]) == [str(site)]
