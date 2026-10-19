"""docs/sync/slash-shortcuts.md is a dump of COMMAND_REGISTRY's short aliases, never hand-authored."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_committed_shortcuts_doc_matches_registry():
    spec = importlib.util.spec_from_file_location("dump_slash_shortcuts", ROOT / "scripts" / "dump_slash_shortcuts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.check() == 0, "docs/sync/slash-shortcuts.md is stale — run scripts/dump_slash_shortcuts.py"
