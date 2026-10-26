"""An import of the contract catalog that an exception cuts short can be retried.

Mirror CI (hermes-agent-ci run 39914545139, t_cbc07d73): the bounds watchdog's async stop landed
inside a topic module while ``tui_gateway.server`` was first imported. Python dropped the half-run
modules from ``sys.modules`` but the registry tables kept their entries, so every later test that
imported the server failed with ``contract declared twice``. Runs in a subprocess: the interrupted
import must not leave a half-built catalog in this test process.
"""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

_PROBE = r"""
import sys

class Stop(BaseException):
    pass

armed = [True]

def local(frame, event, arg):
    if event == "return" and armed[0] and frame.f_locals.get("name") == "projects.list":
        armed[0] = False
        raise Stop
    return local

def trace(frame, event, arg):
    code = frame.f_code
    if event == "call" and code.co_name == "method" and code.co_filename.endswith("registry.py"):
        return local
    return None

sys.settrace(trace)
try:
    import tui_gateway.contracts
except Stop:
    pass
sys.settrace(None)
assert not armed[0], "the probe never interrupted the import"
assert "tui_gateway.contracts.projects_pets" not in sys.modules

import tui_gateway.contracts
from tui_gateway.contracts import projects_pets, registry

assert registry.METHODS["projects.list"].result is projects_pets.ProjectsPayload
try:
    registry.method("projects.list", params=projects_pets.ProfileParams, result=projects_pets.ProjectsPayload)
except RuntimeError as exc:
    assert "declared twice" in str(exc)
else:
    raise AssertionError("a declaration from another module must still be refused")
print("ok")
"""


def test_catalog_import_retries_after_an_interrupted_first_import():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=_REPO, capture_output=True, text=True, timeout=50,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("ok")
