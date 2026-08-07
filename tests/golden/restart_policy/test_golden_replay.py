"""Make the restart-policy golden corpus an ENFORCED gate, not a manual script.

🔴 Found 2026-08-06 during a deploy closeout: PR #456 extracted the fork-owned
restart-policy helpers out of ``gateway/run.py`` and shipped a 31-case golden
replay corpus to prove the move was behavior-preserving — but the runner has no
``__main__``, no pytest collection, and no CI reference. It was driven by hand
during development and has been INERT ever since: the corpus passes today, yet a
future refactor that broke these helpers would sail through CI untouched.

A verification artifact nobody runs is documentation, not a guard. This wrapper
costs one file and turns the existing corpus into a real regression gate.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_HERE = pathlib.Path(__file__).parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "restart_policy_golden_runner", _HERE / "runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _golden() -> dict:
    return json.loads((_HERE / "golden.json").read_text())


def test_golden_corpus_is_not_empty():
    """Guard the guard: an emptied corpus must fail loudly, not pass vacuously."""
    golden = _golden()
    assert len(golden) >= 31, f"golden corpus shrank to {len(golden)} cases"


@pytest.mark.parametrize("case_hash", sorted(_golden().keys()))
def test_restart_policy_matches_golden(case_hash: str):
    """Each pinned case must still produce byte-identical output after any refactor."""
    entry = _golden()[case_hash]
    runner = _load_runner()
    got = runner.run_case(entry["input"])
    assert got == entry["output"], (
        f"restart-policy behavior drifted for case {entry.get('name', case_hash)!r}:\n"
        f"  got={json.dumps(got, sort_keys=True, default=str)}\n"
        f"  exp={json.dumps(entry['output'], sort_keys=True, default=str)}"
    )
