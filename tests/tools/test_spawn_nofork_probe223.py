"""Operator probe from hermes-agent#223 round 2, kept as tests (t_aa2e2d4e).

Each arm runs in a FRESH interpreter so the process-wide hook cannot leak between arms; the
hooked arm must observe exactly what plain CPython observes.

- P1 (Prism 30cd3dabf7e0): a close-on-exec stdin the caller did not redirect must stay closed
  in the child. The hook used to add ``addinherit_np`` for every open 0/1/2 and leaked it.
- P2 (Prism b7f3918f1892): iterator ``args`` must reach the fallback unconsumed (a relative
  ``./script`` is ineligible, so the hook plans, refuses, and replays CPython's path).

Off macOS the hook never installs, so both arms are plain Popen and the tests pin the baseline.
"""

from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO = str(Path(__file__).resolve().parents[2])

_PRELUDE = r'''
import os, sys, subprocess
sys.path.insert(0, {repo!r})
if {arm!r} == "hooked":
    import tools.spawn_nofork as s
    assert s.install_process_hook() is (sys.platform == "darwin")
'''

P1 = r'''
r, w = os.pipe()
os.dup2(r, 0, inheritable=False)
os.write(w, b"secret"); os.close(w)
out = subprocess.run([sys.executable, "-c", "import os\ntry:\n print(os.read(0, 6))\nexcept OSError as e:\n print('closed', e.errno)"], capture_output=True, text=True, timeout=30)
print(out.stdout.strip() or out.stderr.strip()[-200:])
'''

P2 = r'''
import tempfile
d = tempfile.mkdtemp()
p = os.path.join(d, "script")
open(p, "w").write("#!/bin/sh\necho ran $1\n"); os.chmod(p, 0o755)
try:
    out = subprocess.run(iter(["./script", "arg"]), cwd=d, capture_output=True, text=True, timeout=30)
    print("rc", out.returncode, out.stdout.strip())
except Exception as e:
    print("EXC", type(e).__name__, e)
'''


def _arm(body: str, arm: str) -> str:
    code = _PRELUDE.format(repo=REPO, arm=arm) + body
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr[-500:]
    return r.stdout.strip()


def test_p1_close_on_exec_stdin_stays_closed_in_the_child():
    plain, hooked = _arm(P1, "plain"), _arm(P1, "hooked")
    assert plain == f"closed {errno.EBADF}"
    assert hooked == plain


def test_p2_iterator_args_reach_the_fallback_unconsumed():
    plain, hooked = _arm(P2, "plain"), _arm(P2, "hooked")
    assert plain == "rc 0 ran arg"
    assert hooked == plain
