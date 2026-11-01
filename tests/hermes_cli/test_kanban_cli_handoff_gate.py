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
    (home / "hooks" / "access_bound_create_policy.py").write_text(HOOK, encoding="utf-8")
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
    return [json.loads(x) for x in p.read_text(encoding="utf-8-sig").splitlines()] if p.exists() else []


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


def test_policy_off_marker_does_not_disarm_the_handoff_rule(fleet, monkeypatch, capsys):
    """The marker disarms the pool pin only; the handoff rule's only override is the ledgered quote (as in the hook)."""
    (fleet / "state").mkdir(exist_ok=True)
    (fleet / "state" / "access-bound-create-policy-off").write_text("", encoding="utf-8")
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    assert _run("reassign", tid, "human:apollo") == 2
    assert "aegis-no-apollo-handoff" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus"



def test_torn_ledger_write_is_rolled_back_and_refused(fleet, monkeypatch, capsys):
    """ENOSPC mid-append: the half line is truncated away, the call refused, the next append is valid JSONL."""
    import os
    ledger = fleet / "state" / "aegis-apollo-handoffs.jsonl"
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text('{"seed": 1}\n', encoding="utf-8")
    before = ledger.read_bytes()
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)
    real_write = os.write

    def torn(fd, data):
        real_write(fd, bytes(data)[: len(data) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", torn)
    assert _run("reassign", tid, "human:apollo") == 2
    assert "could not be written" in capsys.readouterr().err
    assert ledger.read_bytes() == before
    assert _assignee(tid) == "daedalus"
    monkeypatch.setattr(os, "write", real_write)
    assert _run("reassign", tid, "human:apollo") == 0
    rows = _ledger(fleet)
    assert len(rows) == 2 and rows[1]["card"] == tid


def _scoped(fleet, mapping):
    from agent.secret_scope import set_secret_scope
    return set_secret_scope(mapping, profile_home=str(fleet / "profiles" / "aegis"))


def test_quote_is_read_from_the_acting_profile_scope_not_launch_env(fleet, monkeypatch, capsys):
    """In-process gateway /kanban: os.environ is the launch profile's; only the routed scope's quote counts."""
    from agent.secret_scope import reset_secret_scope
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)  # launch profile's env
    token = _scoped(fleet, {})
    try:
        assert _run("reassign", tid, "human:apollo") == 2
    finally:
        reset_secret_scope(token)
    assert "aegis-no-apollo-handoff" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus" and _ledger(fleet) == []
    monkeypatch.delenv("AEGIS_HANDOFF_APOLLO")
    token = _scoped(fleet, {"AEGIS_HANDOFF_APOLLO": QUOTE})
    try:
        assert _run("reassign", tid, "human:apollo") == 0
    finally:
        reset_secret_scope(token)
    assert _assignee(tid) == "human:apollo" and len(_ledger(fleet)) == 1


def test_env_pin_cannot_unname_an_aegis_home(fleet, monkeypatch, capsys):
    """HERMES_PROFILE=default under HERMES_HOME=<root>/profiles/aegis is still an aegis session."""
    tid = _card()
    aegis_home = fleet / "profiles" / "aegis"
    aegis_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(aegis_home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert _run("reassign", tid, "human:apollo") == 2
    assert "aegis-no-apollo-handoff" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus"


def test_create_override_ledgers_the_created_id_not_the_title(fleet, monkeypatch):
    """Prism 92795be78ffb: the ledger's ``card`` is a task id for every verb, create included."""
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)
    assert _run("create", "t_lookalike title", "--assignee", "human:apollo", "--unhomed") == 0
    with kbc.connect_closing() as conn:
        (task,) = kb.list_tasks(conn)
    rows = _ledger(fleet)
    auth = [r for r in rows if r["verb"] == "create"]
    bound = [r for r in rows if r["verb"] == "create-bound"]
    assert len(auth) == 1 and auth[0]["card"] is None and auth[0]["title"] == "t_lookalike title"
    assert len(bound) == 1 and bound[0]["card"] == task.id and bound[0]["row_id"] == auth[0]["row_id"]


def test_ledger_follows_the_kanban_home_not_the_fleet_root(fleet, monkeypatch):
    """Prism 6b5a28f52a8c: the ledger lives where the board resolved (kanban_home), not at the fleet
    root the hook policy loads from (a sandbox under the native root resolved to the live fleet)."""
    from hermes_cli import kanban_access_bound_pin as pin
    other = fleet.parent / "elsewhere"
    monkeypatch.setattr(pin, "_fleet_root", lambda: fleet)
    monkeypatch.setattr(pin, "_ledger_root", lambda: other)
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)
    assert _run("reassign", tid, "human:apollo") == 0
    assert _ledger(fleet) == []
    assert [r["card"] for r in _ledger(other)] == [tid]


def test_ledger_root_is_kanban_home(fleet, monkeypatch):
    """_ledger_root() is kanban_db.kanban_home(), the sandbox-aware resolver, not _fleet_root()."""
    from hermes_cli import kanban_access_bound_pin as pin
    from hermes_cli import kanban_db
    sentinel = fleet.parent / "resolved-board"
    monkeypatch.setattr(kanban_db, "kanban_home", lambda: sentinel)
    monkeypatch.setattr(pin, "_fleet_root", lambda: fleet)
    assert pin._ledger_root() == sentinel


def test_no_file_lock_platform_refuses_the_handoff(fleet, monkeypatch, capsys):
    """Prism 57e574655dc8 / 1671fc3d2bcc: without fcntl the rollback is unsafe, so nothing is written
    and the hand-off fails closed."""
    tid = _card()
    monkeypatch.setenv("HERMES_PROFILE", "aegis")
    monkeypatch.setenv("AEGIS_HANDOFF_APOLLO", QUOTE)
    from hermes_cli import kanban_access_bound_pin as pin
    monkeypatch.setattr(pin, "_lock_module", lambda: None)
    assert _run("reassign", tid, "human:apollo") == 2
    assert "could not be written" in capsys.readouterr().err
    assert _assignee(tid) == "daedalus" and _ledger(fleet) == []
