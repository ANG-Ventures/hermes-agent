"""Every compaction must name the arm that fired it.

Ace's standing rule (2026-08-07): "all compactions should have attribution.
always." A compaction whose cause is not recorded is a compaction nobody can
explain later — the exact class of bug behind the "why did it compact at 46%?"
incident, where the arm emitted a log line and nothing else.

Two layers, deliberately:
  * a STRUCTURAL lint over every ``_compress_context`` call site, which fails at
    PR time when someone adds a new one without ``trigger_reason``; and
  * a BEHAVIORAL check that the log line actually carries the trigger, so the
    lint cannot pass while the value is dropped on the floor.

The lint is the load-bearing half: a runtime monitor only tells you about an
unattributed compaction AFTER it has already confused someone.
"""
import ast
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every module that calls _compress_context. Add new ones here deliberately.
CALLER_FILES = [
    "agent/turn_context.py",
    "agent/conversation_loop.py",
    "gateway/run.py",
    "gateway/slash_commands.py",
]


def _compress_context_calls(path):
    """(lineno, has_trigger_reason) for every _compress_context call."""
    src = open(path, encoding="utf-8", errors="ignore").read()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != "_compress_context":
            continue
        has = any(kw.arg == "trigger_reason" for kw in node.keywords)
        found.append((node.lineno, has))
    return found


def test_lint_every_compress_call_site_names_its_trigger():
    """THE gate: a new call site without trigger_reason fails here, not in prod."""
    offenders = []
    total = 0
    for rel in CALLER_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            continue
        for lineno, has in _compress_context_calls(path):
            total += 1
            if not has:
                offenders.append(f"{rel}:{lineno}")
    assert total >= 8, f"only found {total} call sites — did the lint stop seeing them?"
    assert not offenders, (
        "these _compress_context call sites do not pass trigger_reason, so their "
        "compactions will log as UNATTRIBUTED:\n  " + "\n  ".join(offenders)
    )


def test_lint_finds_call_sites_at_all():
    """Positive control: a lint that silently matches nothing always passes."""
    path = os.path.join(REPO, "agent/turn_context.py")
    calls = _compress_context_calls(path)
    assert calls, "AST lint found zero call sites in turn_context.py — lint is broken"


def test_started_log_line_carries_the_trigger():
    """The value must reach the log, not just the signature."""
    src = open(os.path.join(REPO, "agent/conversation_compression.py"),
               encoding="utf-8", errors="ignore").read()
    assert "context compression started: session=%s trigger=%s" in src, (
        "the compression-started log line no longer includes trigger=%s"
    )
    assert "_trigger_label" in src


def test_missing_trigger_is_logged_as_a_warning():
    """A gap must be LOUD. Silence is how the original bug survived a day."""
    src = open(os.path.join(REPO, "agent/conversation_compression.py"),
               encoding="utf-8", errors="ignore").read()
    assert "context compression has no trigger_reason" in src
    idx = src.index("context compression has no trigger_reason")
    assert "logger.warning" in src[max(0, idx - 300):idx], (
        "the unattributed-compaction message must be a WARNING, not info"
    )


def test_trigger_reason_is_still_a_supported_parameter():
    """Guards against the forwarder silently dropping the kwarg."""
    src = open(os.path.join(REPO, "run_agent.py"), encoding="utf-8",
               errors="ignore").read()
    assert re.search(r"def _compress_context\(", src)
    assert "trigger_reason=trigger_reason" in src, (
        "run_agent._compress_context no longer forwards trigger_reason"
    )


@pytest.mark.parametrize("expected", [
    "engine_preflight_maintenance",
    "idle_resume",
    "pre_api_pressure",
    "session_hygiene",
    "manual_compress_command",
    "threshold",
    "overflow_413",
    "overflow_context",
    "tier_reduction",
])
def test_known_trigger_labels_are_wired(expected):
    """Each named arm actually appears at a call site.

    A label that exists only in a test is a label no compaction will ever emit.
    """
    hits = 0
    for rel in CALLER_FILES:
        path = os.path.join(REPO, rel)
        if os.path.exists(path):
            hits += open(path, encoding="utf-8", errors="ignore").read().count(
                f'trigger_reason="{expected}"')
    assert hits >= 1, f"no call site emits trigger_reason={expected!r}"
