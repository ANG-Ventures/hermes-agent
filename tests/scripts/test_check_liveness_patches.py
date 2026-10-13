"""``scripts/check_liveness_patches.py``: a test may not replace a liveness primitive with a
callable that cannot end a poll loop (t_69572750: ``os.kill`` recorder, 34.6 GiB, host reset).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
    """Every _BASELINE entry still matches the tree (a burned-down file must drop its entry)."""
    assert lint.main([]) == 0
    assert lint.main(["--strict-baseline"]) == 0
