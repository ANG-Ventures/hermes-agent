"""The fork's half-turn /undo + /redo unit (#49, #353, #339) is retired.

/undo is upstream's single-user-turn rewind again on every surface, and /redo
does not exist. #356 (no empty user row on auto-resume) is a separate unit and
stays, so its helper must still be present.
"""

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_redo_is_not_registered():
    from hermes_cli.commands import COMMAND_REGISTRY

    names = {c.name for c in COMMAND_REGISTRY}
    aliases = {a for c in COMMAND_REGISTRY for a in (c.aliases or ())}
    assert "undo" in names
    assert "redo" not in names
    assert "redo" not in aliases


def test_redo_stack_module_is_gone():
    assert not (REPO / "hermes_undo.py").exists()
    assert importlib.util.find_spec("hermes_undo") is None


def test_tui_gateway_has_no_redo_method():
    import tui_gateway.server as server

    assert "session.undo" in server._methods
    assert "session.redo" not in server._methods
    assert not hasattr(server, "_undo_session_core")
    assert not hasattr(server, "_redo_session_core")


def test_registry_entry_for_undo_redo_is_retired():
    features = json.loads((REPO / "docs/sync/fork-features.json").read_text())
    for entry in features:
        assert "/redo" not in entry["feature"], entry["feature"]
        assert "hermes_undo.py" not in entry.get("paths", [])


def test_empty_resume_row_suppression_is_kept():
    # #356 is not part of this unit (lead ruled it UNRESOLVED).
    from agent.turn_context import maybe_stamp_empty_resume_row

    class _Agent:
        _suppress_user_turn_persist = True

    agent = _Agent()
    row = {"role": "user", "content": ""}
    assert maybe_stamp_empty_resume_row(agent, row) is True
    assert row["_empty_resume_synthetic"] is True
    assert agent._suppress_user_turn_persist is False
