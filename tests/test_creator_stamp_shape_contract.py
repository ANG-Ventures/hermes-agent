"""Contract: creator-stamp shape discrimination has ONE home and NO drift.

The 2026-08-12 phantom-session regression (fork #588): ``tasks.session_id``
is a mixed-format column — session KEY on gateway-created tasks, RAW session
id on worker/CLI-created tasks — and #568 compared it against routing-index
keys unconditionally, silently emptying wake evidence for every worker card.

These tests lock the two properties that keep the fix from rotting:

1. **Behavioral contract** of :func:`creator_stamp_is_session_key` against
   the real formats both writers produce.
2. **Single-source contract** (AST): outside ``gateway/routing_identity.py``
   no production module inlines its own ``":" in <stamp>`` shape test on a
   creator/session stamp — the drift path that re-creates the bug class.
"""

from __future__ import annotations

import ast
from pathlib import Path

from gateway.routing_identity import creator_stamp_is_session_key

REPO = Path(__file__).resolve().parent.parent
CANONICAL_HOME = REPO / "gateway" / "routing_identity.py"

# Production files that consume the creator stamp. Extend when a new consumer
# appears — the sweep below also scans these for inlined shape tests.
CONSUMERS = [
    REPO / "gateway" / "kanban_watchers.py",
    REPO / "hermes_cli" / "kanban.py",
]

# Variable-name fragments that indicate a creator/session stamp operand.
_STAMP_NAME_HINTS = ("creator", "session_id", "session_key", "stamp")


def test_gateway_key_stamp_is_recognized():
    # Real shapes produced by gateway session keys (build_session_key).
    for key in (
        "agent:main:discord:group:1535189663533506600:117431298246705156",
        "agent:main:discord:thread:123:123",
        "agent:main:slack:dm:T0AB12CD3:C123",
        "agent:main:telegram:dm:571820863",
    ):
        assert creator_stamp_is_session_key(key), key


def test_raw_session_id_stamp_is_recognized_as_not_a_key():
    # Real shapes produced by hermes_state session ids (worker/CLI creates).
    for raw in (
        "20260811_220323_2eafab",
        "20260807_003606_6a51f1e0",
        "cron_b73b2f7eac9d_20260513_161548",
    ):
        assert not creator_stamp_is_session_key(raw), raw


def test_empty_and_none_are_not_keys():
    assert not creator_stamp_is_session_key("")
    assert not creator_stamp_is_session_key(None)


def _inlined_shape_tests(path: Path) -> list[str]:
    """Find ``":" in <name>`` / ``<name>.find(":")``-style shape tests whose
    operand looks like a creator/session stamp."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (
            isinstance(node.left, ast.Constant) and node.left.value == ":"
        ):
            continue
        if not any(isinstance(op, ast.In) for op in node.ops):
            continue
        operand = node.comparators[0]
        text = ast.dump(operand)
        if any(h in text for h in _STAMP_NAME_HINTS):
            hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_no_inlined_stamp_shape_tests_outside_canonical_home():
    """The discrimination lives in creator_stamp_is_session_key ONLY.

    An inlined ``":" in creator_...`` in a consumer is exactly how two
    copies drift apart (the class behind the original regression). If this
    fails, replace the inline test with a call to the canonical helper.
    (The guarded ImportError fallback in _cmd_notify_repair is allowed: it
    duplicates behavior only when the canonical home is unimportable, and
    is marked pragma no-cover.)
    """
    offenders: list[str] = []
    for path in CONSUMERS:
        for hit in _inlined_shape_tests(path):
            src_line = path.read_text().splitlines()[
                int(hit.rsplit(":", 1)[1]) - 1
            ]
            # The sanctioned fallback shadow inside the ImportError guard.
            if "def _stamp_is_key" in src_line or '":" in str(stamp' in src_line:
                continue
            offenders.append(hit)
    assert not offenders, (
        "inlined creator-stamp shape test(s) found — use "
        f"creator_stamp_is_session_key instead: {offenders}"
    )


def test_consumers_actually_import_the_canonical_helper():
    """Both stamp consumers must reference the canonical helper by name —
    guards against someone deleting the call while keeping behavior via a
    local copy."""
    for path in CONSUMERS:
        assert "creator_stamp_is_session_key" in path.read_text(), (
            f"{path} no longer references creator_stamp_is_session_key"
        )
