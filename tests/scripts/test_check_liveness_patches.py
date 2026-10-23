"""``scripts/check_liveness_patches.py``: a test may not replace a liveness primitive with a
callable that cannot end a poll loop (t_69572750: ``os.kill`` recorder, 34.6 GiB, host reset).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_liveness_patches", ROOT / "scripts" / "check_liveness_patches.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

# The incident shape, verbatim from tests/hermes_cli/test_kanban_pr_owner.py @ a0fe4e9410.
INCIDENT = '''
import os
def test_x(monkeypatch):
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
'''

# The fix that shipped in #1928: signal-0 probes reach the real primitive.
FIXED = '''
import os
def _recording_kill(signals):
    real = os.kill
    def _kill(pid, sig):
        if sig == 0:
            return real(pid, sig)
        signals.append((pid, sig))
    return _kill
def test_x(monkeypatch):
    signals = []
    monkeypatch.setattr(os, "kill", _recording_kill(signals))
'''


def _hits(src: str) -> list[str]:
    return lint.check_source(src, "t.py")


def test_incident_recorder_is_red_and_the_shipped_fix_is_green():
    (hit,) = _hits(INCIDENT)
    assert "os.kill" in hit and "t_69572750" in hit and "t.py:5" in hit
    assert _hits(FIXED) == []


def test_every_spelling_of_the_patch_is_seen():
    for src in (
        'import os\ndef test_x(monkeypatch):\n    monkeypatch.setattr("os.kill", lambda *a: None)\n',
        'import os\nfrom unittest.mock import patch\ndef test_x():\n    with patch("os.kill"):\n        pass\n',
        'import os\nfrom unittest.mock import patch\ndef test_x():\n    with patch("tools.mcp_tool.os.kill", return_value=None):\n        pass\n',
        'import os\nfrom unittest.mock import patch\ndef test_x():\n    with patch.object(os, "kill", lambda *a: None):\n        pass\n',
        'import os\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "waitpid", lambda *a: (0, 0))\n',
        'import socket\ndef test_x(monkeypatch):\n    monkeypatch.setattr(socket.socket, "connect", lambda self, a: None)\n',
        'import psutil\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)\n',
        'from unittest.mock import patch\ndef test_x():\n    with patch("psutil.pid_exists", return_value=True):\n        pass\n',
    ):
        assert len(_hits(src)) == 1, src


def test_replacements_that_can_end_the_loop_are_green():
    for src in (
        # raises
        'import os\ndef _dead(pid, sig):\n    raise ProcessLookupError()\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", _dead)\n',
        # exception class / instance as side_effect
        'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", side_effect=ProcessLookupError):\n        pass\n',
        'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", side_effect=ProcessLookupError(3)):\n        pass\n',
        # iterable side_effect: StopIteration ends it
        'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", side_effect=[None, ProcessLookupError()]):\n        pass\n',
        # forwards to the saved original
        'import os\ndef test_x(monkeypatch):\n    real = os.kill\n    monkeypatch.setattr(os, "kill", lambda pid, sig: real(pid, 0))\n',
        # Mock(side_effect=...) bound to a name
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    forbidden = Mock(side_effect=AssertionError("no"))\n    monkeypatch.setattr(os, "kill", forbidden)\n',
        # the lambda-raise idiom
        'import os\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no")))\n',
        # pytest.fail inside the stand-in
        'import os, pytest\ndef test_x(monkeypatch):\n    def _explode(pid, sig):\n        pytest.fail("no")\n    monkeypatch.setattr(os, "kill", _explode)\n',
        # pid_exists pinned False ends every wait-for-exit loop
        'import psutil\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)\n',
        # psutil.Process is not a liveness primitive
        'import psutil\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "Process", lambda pid: None)\n',
    ):
        assert _hits(src) == [], src


# ── Prism #1946 round 2 (t_4a3eba50). Every case below was GREEN (a miss) at 15aafdde99. ──

def test_return_value_never_raises_whatever_it_is():
    """3f238f646a10: ``return_value`` is returned on every call; a list or exception there is not a side_effect."""
    for rv in ("[]", "[None, ProcessLookupError()]", "ProcessLookupError", "ProcessLookupError(3)", "None"):
        src = f'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", return_value={rv}):\n        pass\n'
        assert len(_hits(src)) == 1, src
    src = 'from unittest.mock import patch\ndef test_x():\n    with patch("psutil.pid_exists", return_value=False):\n        pass\n'
    assert _hits(src) == []


def test_side_effect_is_judged_by_its_value_not_its_presence():
    """24f441782ae3: ``Mock(side_effect=None)`` / a non-raising callable side_effect never raises."""
    red = (
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", Mock(side_effect=None))\n',
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", Mock(side_effect=lambda *a: None))\n',
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    k = Mock(side_effect=lambda *a: None)\n    monkeypatch.setattr(os, "kill", k)\n',
        'import os\nfrom unittest.mock import MagicMock\ndef test_x(monkeypatch):\n    k = MagicMock(return_value=None)\n    monkeypatch.setattr(os, "kill", k)\n',
        'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", side_effect=None):\n        pass\n',
        'from unittest.mock import patch\ndef test_x():\n    with patch("os.kill", side_effect=lambda *a: None):\n        pass\n',
    )
    for src in red:
        assert len(_hits(src)) == 1, src
    green = (
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", Mock(side_effect=ProcessLookupError))\n',
        'import os\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    k = Mock(side_effect=[None, OSError()])\n    monkeypatch.setattr(os, "kill", k)\n',
    )
    for src in green:
        assert _hits(src) == [], src


def test_names_resolve_in_their_own_scope():
    """cc5475624092: two tests each defining ``fake_kill``; the second (non-raising) one is red."""
    src = (
        'import os\n'
        'def test_a(monkeypatch):\n'
        '    def fake_kill(pid, sig):\n'
        '        raise ProcessLookupError\n'
        '    monkeypatch.setattr(os, "kill", fake_kill)\n'
        'def test_b(monkeypatch):\n'
        '    seen = []\n'
        '    def fake_kill(pid, sig):\n'
        '        seen.append(sig)\n'
        '    monkeypatch.setattr(os, "kill", fake_kill)\n'
    )
    (hit,) = _hits(src)
    assert "t.py:10" in hit
    # same name rebound later in one scope: the binding at the patch site counts
    src = (
        'import os\n'
        'def test_c(monkeypatch):\n'
        '    def fake_kill(pid, sig):\n'
        '        pass\n'
        '    monkeypatch.setattr(os, "kill", fake_kill)\n'
        '    def fake_kill(pid, sig):\n'
        '        raise ProcessLookupError\n'
    )
    (hit,) = _hits(src)
    assert "t.py:5" in hit


def test_a_named_constant_true_pid_exists_is_red():
    """f5bbb3106cb7: ``def always_alive(pid): return True`` for psutil.pid_exists."""
    src = 'import psutil\ndef always_alive(pid):\n    return True\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "pid_exists", always_alive)\n'
    assert len(_hits(src)) == 1
    src = 'import psutil\nfrom unittest.mock import Mock\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "pid_exists", Mock(return_value=True))\n'
    assert len(_hits(src)) == 1
    src = 'import psutil\ndef gone_after(n):\n    def f(pid):\n        n[0] -= 1\n        return n[0] > 0\n    return f\ndef test_x(monkeypatch):\n    monkeypatch.setattr(psutil, "pid_exists", gone_after([3]))\n'
    assert _hits(src) == []


def test_mutually_recursive_helpers_do_not_crash_the_lint():
    """16935077eaee: ``fake_kill`` -> ``helper`` -> ``fake_kill``. The analysis must terminate
    and judge the pair by whether anything on the cycle raises; a cycle that never raises is red."""
    cycle = ('import os\n'
             'def helper(pid):\n    if pid == 0:\n        return None\n    return fake_kill(0, 0)\n'
             'def fake_kill(pid, sig):\n    return helper(pid)\n'
             'def test_x(monkeypatch):\n    monkeypatch.setattr(os, "kill", fake_kill)\n')
    assert len(_hits(cycle)) == 1
    raising = cycle.replace("        return None\n", "        raise ProcessLookupError(pid)\n")
    assert _hits(raising) == []


def test_suppress_marker_names_a_reason_and_silences_one_line():
    src = INCIDENT.replace("signals.append((pid, sig)))",
                           "signals.append((pid, sig)))  # liveness-patch: ok — terminate_pid is single-shot")
    assert _hits(src) == []


def test_baseline_is_a_ceiling_per_file_not_a_pass():
    failures, advisories = lint.apply_baseline({"a.py": ["h1", "h2"]}, {"a.py": 2}, strict=False)
    assert failures == [] and advisories == []
    failures, _ = lint.apply_baseline({"a.py": ["h1", "h2", "h3"]}, {"a.py": 2}, strict=False)
    assert failures[:3] == ["h1", "h2", "h3"] and "baseline allows 2" in failures[3]
    failures, _ = lint.apply_baseline({"new.py": ["h1"]}, {"a.py": 2}, strict=False)
    assert failures[:1] == ["h1"]
    failures, advisories = lint.apply_baseline({"a.py": ["h1"]}, {"a.py": 2}, strict=False)
    assert failures == [] and "shrink" in advisories[0]
    failures, advisories = lint.apply_baseline({}, {"a.py": 2}, strict=True)
    assert "drop" in failures[0] and advisories == []


def test_live_tree_is_clean_and_the_baseline_is_not_overstated():
    """Every _BASELINE entry still matches the tree (a burned-down file must drop its entry).

    One scan: ``--strict-baseline`` fails on everything the default run fails on, plus stale
    entries (``apply_baseline``), so a second whole-tree scan only doubled the wall clock
    (t_44dcc531)."""
    assert lint.main(["--strict-baseline"]) == 0


# The raw-text prefilter in front of the AST check must never skip a file the AST would flag:
# each spelling below decodes to ``patch("os.kill", return_value=None)`` (t_44dcc531,
# Prism 3c7e1b72c0b1).
_BS = chr(92)
_SPLIT_PATCHES = {
    "backslash-newline-in-literal": 'os.' + _BS + '\nkill',
    "backslash-crlf-in-literal": 'os.' + _BS + '\r\nkill',
    "backslash-newline-mid-tail": 'os.ki' + _BS + '\nll',
    "implicit-join": 'os.ki" "ll',
    "implicit-join-empty-literal": 'os.kill" "',
    "continuation-between-literals": 'os.k" ' + _BS + '\n "ill',
    "hex-escape": 'os.\\x6bill',
}


@pytest.mark.parametrize("target", list(_SPLIT_PATCHES.values()), ids=list(_SPLIT_PATCHES))
def test_a_split_spelling_of_the_patch_target_is_still_scanned(target):
    src = ('from unittest.mock import patch\ndef test_x():\n    with patch("' + target
           + '", return_value=None):\n        pass\n')
    assert lint.check_source(src), "the prefilter skipped a file the AST flags"
