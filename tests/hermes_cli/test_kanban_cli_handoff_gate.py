"""`kanban create|assign|reassign` enforce hermes-home's ``aegis-no-apollo-handoff`` on the REAL
parsed assignee (t_d26f1385).

The pre_tool_call hook reads the command text, so ``A=human; B=apollo; ... "$A:$B"`` or an
assignee computed inside a script reaches the board unread. The CLI holds the parsed value, so it
asks the fleet root's hook module (``handoff_refusal`` + its constants) and refuses with rc 2, or
ledgers an ``AEGIS_HANDOFF_APOLLO`` override to state/aegis-apollo-handoffs.jsonl.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# Stand-in for hermes-home hooks/access_bound_create_policy.py: the handoff surface the CLI uses.
HOOK = '''
HANDOFF_RULE = "aegis-no-apollo-handoff"
HANDOFF_ENV = "AEGIS_HANDOFF_APOLLO"
HANDOFF_MIN = 20
HANDOFF_PROFILE = "aegis"
APOLLO_ASSIGNEES = {"human:apollo", "apollo", "default", "human:default"}
def pool_hosts(root):
    return set()
def verdict(body, hosts):
    return None
def handoff_refusal(verb, assignee, short):
    why = "quote too short" if short else "no quote"
    return f"BLOCKED by {HANDOFF_RULE}: an Aegis session may not {verb} a card to `{assignee}` ({why})"
'''
QUOTE = "Ace: hand t_x to Apollo, he owns that lane now"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "access_bound_create_policy.py").write_text(HOOK)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "AEGIS_HANDOFF_APOLLO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(*argv: str) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def _card(assignee: str = "daedalus") -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="probe", assignee=assignee)


def _assignee(tid: str):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).assignee


def _count() -> int:
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _ledger(home: Path) -> list:
    p = home / "state" / "aegis-apollo-handoffs.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_aegis_reassign_to_apollo_is_refused_and_nothing_written(fleet, monkeypatch, capsys):
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    assert _run("reassign", tid, "human:apollo") == 2
    assert "aegis-no-apollo-handoff" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus"
    assert _ledger(fleet) == []
    assert _run("assign", tid, "Apollo") == 2
    assert _assignee(tid) == "daedalus"


def test_aegis_create_with_apollo_assignee_is_refused(fleet, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    assert _run("create", "probe", "--assignee", "human:apollo", "--unhomed") == 2
    assert "aegis-no-apollo-handoff" in capsys.readouterr().err
    assert _count() == 0


def test_aegis_reassign_with_quote_is_allowed_and_ledgered_once(fleet, monkeypatch):
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)
    assert _run("reassign", tid, "human:apollo") == 0
    assert _assignee(tid) == "human:apollo"
    rows = _ledger(fleet)
    assert len(rows) == 1
    assert rows[0]["card"] == tid and rows[0]["assignee"] == "human:apollo"
    assert rows[0]["quote"] == QUOTE and rows[0]["profile"] == "aegis" and rows[0]["verb"] == "reassign"


def test_aegis_short_quote_is_refused(fleet, monkeypatch, capsys):
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", "ok go")
    assert _run("reassign", tid, "human:apollo") == 2
    assert "quote too short" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus" and _ledger(fleet) == []


def test_default_profile_reassign_to_apollo_is_allowed(fleet):
    tid = _card()
    assert _run("reassign", tid, "human:apollo") == 0
    assert _assignee(tid) == "human:apollo"
    assert _ledger(fleet) == []


@pytest.mark.parametrize("target", ["human:aegis", "daedalus-opus"])
def test_aegis_reassign_to_non_apollo_is_allowed(fleet, monkeypatch, target):
    tid = _card("worker-x")
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    assert _run("reassign", tid, target) == 0
    assert _assignee(tid) == target


def test_missing_hook_module_fails_open(fleet, monkeypatch):
    (fleet / "hooks" / "access_bound_create_policy.py").unlink()
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    assert _run("reassign", tid, "human:apollo") == 0
    assert _assignee(tid) == "human:apollo"
    assert _run("create", "probe2", "--assignee", "human:apollo", "--unhomed") == 0
