"""Run the unchanged nine-case preflight through real child interpreters."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "codex_preflight", ROOT / "scripts/probe_codex_profile_refresh.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize(
    "kwargs,posts",
    [
        ({"same_profile": True}, 1),
        ({}, 2),  # declared local mirrors are NOT silently joined
        ({"inherited": True}, 1),
        ({"inherited": True, "singleton": True}, 1),
        ({"same_profile": True, "serialized": True}, 1),
        ({"inherited": True, "serialized": True, "mode": "root-lock-only"}, 1),
        ({"independent": True}, 2),
        ({"inherited": True, "serialized": True, "mode": "crash"}, 1),
        ({"inherited": True, "serialized": True, "mode": "write-failure"}, 1),
    ],
)
def test_real_process_transactions(kwargs, posts):
    result = probe.run_case(**kwargs)
    assert sum(e["event"] == "POST" for e in result["events"]) == posts
    if kwargs.get("inherited"):
        assert result["profile_copies"] == 0
        if kwargs.get("mode") not in {"crash", "write-failure"}:
            assert result["root_unchanged"] is False
    if kwargs.get("mode") in {"crash", "write-failure"}:
        assert result["events"][-1]["event"] == "ERROR"
