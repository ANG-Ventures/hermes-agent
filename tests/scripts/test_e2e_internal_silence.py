"""Verdict logic of scripts/e2e_internal_silence.py (the live E2E for t_bb4059d2)."""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "e2e_internal_silence.py"
_spec = importlib.util.spec_from_file_location("e2e_internal_silence", _PATH)
e2e = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = e2e  # dataclasses resolve their module through sys.modules
_spec.loader.exec_module(e2e)

P = e2e.Posted


def test_long_arm_passes_only_on_the_tokened_message_without_marker():
    assert e2e.arm_verdict("long", "ab12", []) == ("none", False, False)
    other = P("1", 1.0, "started")
    assert e2e.arm_verdict("long", "ab12", [other]) == ("none", False, False)
    assert e2e.arm_verdict("long", "ab12", [other, P("2", 2.0, "Done. E2E-LONG-ab12 ok.")]) == ("2", False, True)
    assert e2e.arm_verdict("long", "ab12", [P("3", 2.0, "E2E-LONG-ab12\n\nNO_REPLY")]) == ("3", True, False)


def test_quiet_arms_fail_on_any_post_after_the_completion():
    for arm in ("short", "bare"):
        assert e2e.arm_verdict(arm, "x", []) == ("none", False, True)
        assert e2e.arm_verdict(arm, "x", [P("9", 1.0, "Already handled.\nNO_REPLY")]) == ("9", True, False)


def test_prompts_name_the_arm_shape():
    assert "E2E-LONG-ab12" in e2e.prompt_for("long", "ab12")
    assert "Already handled." in e2e.prompt_for("short", "ab12")
    assert "notify_on_complete=true" in e2e.prompt_for("bare", "ab12")
