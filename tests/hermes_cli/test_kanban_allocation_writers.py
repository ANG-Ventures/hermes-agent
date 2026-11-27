"""Rename wave 3 step 3 (t_7be48220): writers emit the allocation event kinds; ``kanban allocation`` is the
verb and ``kanban placement`` a deprecated alias that says so on stderr and is counted.

Readers accept both names (step 2, test_kanban_event_kind_aliases.py); historical rows are never rewritten.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement_reasons as pr

REPO = Path(__file__).resolve().parents[2]
_WRITE_CALLS = {"_append_event", "_journal_append"}


def test_writer_constants_are_the_allocation_names():
    assert (pr.PLACEMENT_EVENT, pr.PLACEMENT_WAIT_EVENT, pr.WORKER_PLACED_EVENT, pr.PLACEMENT_HINT_EVENT,
            pr.PLACEMENT_REROUTED_EVENT) == ("allocation", "allocation_wait", "worker_allocated",
                                             "allocation_hint", "allocation_rerouted")
    assert set(pr.KIND_ALIASES.values()) == {pr.PLACEMENT_EVENT, pr.PLACEMENT_WAIT_EVENT, pr.WORKER_PLACED_EVENT,
                                             pr.PLACEMENT_HINT_EVENT, pr.PLACEMENT_REROUTED_EVENT}
    from hermes_cli import kanban_headroom as kh
    from hermes_cli import kanban_pause_hold as kph
    from hermes_cli import kanban_placement_block as kpb
    from hermes_cli import kanban_worker_pool as kwp
    assert kwp.PLACED_EVENT == "worker_allocated" and kh.RESERVATION_EVENTS[0] == "worker_allocated"
    assert kph.REROUTE_EVENT == kpb.REROUTED_EVENT == pr.PLACEMENT_REROUTED_EVENT


def _old_kind_writes(path: Path) -> list:
    """``(line, kind)`` of every event write in ``path`` whose kind argument is an old placement name."""
    with warnings.catch_warnings():  # a module's own invalid escapes are not this test's concern
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    old_consts = {}  # module-level NAME = "<old kind>"
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and node.value.value in pr.KIND_ALIASES):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    old_consts[t.id] = node.value.value
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
        if name not in _WRITE_CALLS or len(node.args) < 3:
            continue
        kind = node.args[2]
        if isinstance(kind, ast.Constant) and kind.value in pr.KIND_ALIASES:
            hits.append((node.lineno, kind.value))
        elif isinstance(kind, ast.Name) and kind.id in old_consts:
            hits.append((node.lineno, old_consts[kind.id]))
    return hits


def test_no_runtime_writer_emits_an_old_kind_name():
    found = {}
    for sub in ("hermes_cli", "gateway", "tools"):
        for path in sorted((REPO / sub).rglob("*.py")):
            hits = _old_kind_writes(path)
            if hits:
                found[str(path.relative_to(REPO))] = hits
    assert found == {}


def test_old_kind_write_detector_is_not_blind(tmp_path):
    src = tmp_path / "m.py"
    src.write_text('EV = "placement_wait"\n'
                   'def f(kb, c):\n'
                   '    kb._append_event(c, "t", "worker_placed", {})\n'
                   '    kb._append_event(c, "t", EV, {})\n'
                   '    kb._append_event(c, "t", "allocation_wait", {})\n')
    assert _old_kind_writes(src) == [(3, "worker_placed"), (4, "placement_wait")]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing():
        kb.create_board(slug="default", name="Default")
    return h


def _parse(*argv):
    import argparse

    from hermes_cli.kanban_parser import build_parser

    top = argparse.ArgumentParser()
    build_parser(top.add_subparsers(dest="cmd"))
    return top.parse_args(["kanban", *argv])


def test_allocation_verb_and_no_placement_alias(home, capsys):
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_read import is_read

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha")
    log = home / "var" / "ci-box" / "deprecated-name-reads.log"

    args = _parse("allocation", tid)
    assert args.kanban_action == "allocation" and is_read(args)
    assert kcli._HANDLERS["allocation"](args) == 0
    new = capsys.readouterr()
    assert tid in new.out and "deprecated" not in new.err and not log.exists()

    assert "placement" not in kcli._HANDLERS     # t_5931d620: the old verb alias is gone
    with pytest.raises(SystemExit):
        _parse("placement", tid)
    capsys.readouterr()
    assert not log.exists()
