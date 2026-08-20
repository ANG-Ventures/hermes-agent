"""Compaction trigger-attribution coverage (2026-08-20 audit).

The contract: EVERY compaction — automatic or manual, on every surface —
names its trigger in the user-facing banner. Two structural guards:

1. **Producer/renderer vocabulary lockstep.** Every literal ``trigger_reason=``
   value passed by any call site in the codebase must render a NON-EMPTY,
   human-meaningful clause in ``_compaction_reason_clause``. This is scanned
   from source, so adding a new trigger with no clause arm fails here — the
   vocabulary cannot silently drift again (found live: ``session_hygiene``,
   ``idle_resume``, ``pre_api_pressure``, ``engine_preflight_maintenance``
   all rendered as "" while the arms only knew the gateway's normalized
   names).

2. **Manual surfaces pass the manual label.** Every manual /compress surface
   (gateway slash, TUI server helper, CLI, ACP adapter) must pass
   ``trigger_reason="manual_compress_command"`` into ``_compress_context`` so
   the core's ``trigger=`` log line and the announce never show UNATTRIBUTED
   for a user-fired compress.
"""

import re
from pathlib import Path

from agent.fork_ext.compaction_ext import _compaction_reason_clause
from agent.manual_compression_feedback import MANUAL_TRIGGER_CLAUSE

REPO = Path(__file__).resolve().parents[2]

# Files that may legitimately pass trigger_reason literals into the core.
_PRODUCER_GLOBS = [
    "agent/*.py",
    "agent/fork_ext/*.py",
    "gateway/*.py",
    "tui_gateway/*.py",
    "acp_adapter/*.py",
    "cli.py",
]

_LITERAL_RE = re.compile(r'trigger_reason=(?:"|\')([a-z_0-9]+)(?:"|\')')
# Gateway hygiene valve assigns via a variable; include its literal values.
_HYG_RE = re.compile(r'_hyg_reason\s*=\s*(?:"|\')([a-z_0-9]+)(?:"|\')')


def _collect_producer_reasons() -> set[str]:
    reasons: set[str] = set()
    for pattern in _PRODUCER_GLOBS:
        for f in REPO.glob(pattern):
            text = f.read_text(errors="replace")
            reasons.update(_LITERAL_RE.findall(text))
            reasons.update(_HYG_RE.findall(text))
    return reasons


def test_every_produced_reason_renders_a_clause():
    reasons = _collect_producer_reasons()
    # Sanity: the scan actually found the known producers (a broken glob must
    # not green this test vacuously).
    assert "manual_compress_command" in reasons
    assert "threshold" in reasons
    assert len(reasons) >= 8, reasons
    missing = {
        r: _compaction_reason_clause(r, None)
        for r in sorted(reasons)
        if not _compaction_reason_clause(r, None).strip()
    }
    assert not missing, (
        f"trigger_reason values with NO rendered clause (the 'no reason "
        f"stated' bug): {missing}. Add an arm to _compaction_reason_clause."
    )


def test_unknown_reason_is_never_silent():
    clause = _compaction_reason_clause("some_future_trigger", None)
    assert clause.strip(), "unknown trigger must render its raw label, not ''"
    assert "some_future_trigger" in clause


def test_manual_reason_renders_manual_clause_on_both_vocab():
    # The legacy 'manual' label and the live producer label render identically,
    # and both match the chokepoint clause so manual + automatic surfaces
    # describe the same event with the same words.
    assert _compaction_reason_clause("manual", None) == MANUAL_TRIGGER_CLAUSE
    assert (
        _compaction_reason_clause("manual_compress_command", None)
        == MANUAL_TRIGGER_CLAUSE
    )


def test_every_manual_surface_passes_the_manual_label():
    """The four manual /compress surfaces each pass the manual trigger label
    into _compress_context (source-pinned; a new manual surface added without
    attribution fails the producer-coverage test above only if it uses a NEW
    label — this one catches it passing NONE at all)."""
    surfaces = {
        "gateway": REPO / "gateway" / "slash_commands.py",
        "tui": REPO / "tui_gateway" / "server.py",
        "cli": REPO / "cli.py",
        "acp": REPO / "acp_adapter" / "server.py",
    }
    for name, path in surfaces.items():
        text = path.read_text(errors="replace")
        assert 'trigger_reason="manual_compress_command"' in text, (
            f"{name} surface ({path.name}) has a manual /compress path that "
            f"does not pass trigger_reason=manual_compress_command — its "
            f"compressions log trigger=UNATTRIBUTED"
        )
