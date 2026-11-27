"""A ``requires:`` line of unknown words is refused at create/edit; a stranded ready card wakes once (t_c754f664).

t_00dabd92 (2026-11-21) carried ``requires: host=studio reason: Bambu Studio slicing and the Bambuddy queue
tooling run on the Studio``: every word read as a capability, the card held on the Studio and sat ready
4 h 5 min with no wake.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_access_bound_pin as kabp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_requires_words as krw

PROSE = ("requires: host=studio reason: Bambu Studio slicing and the Bambuddy queue tooling "
         "run on the Studio")
LEGAL = "requires: lan-reach (slicer/queue API on 3dprinter.ace)"
MAP = {
    "schema": 1,
    "capabilities": {"lan-reach": {}, "browser-public": {}, "studio-state": {}},
    "hosts": {"studio": {"capabilities": ["lan-reach", "browser-public", "studio-state"]},
              "ace-ai": {"capabilities": ["lan-reach", "browser-public"]}},
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "fleet").mkdir(parents=True)
    (home / "fleet" / khc.MAP_FILE).write_text(json.dumps(MAP), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_OWNER_PID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kabp, "_fleet_root", lambda: home)   # no studio hook module: it fails open
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _parse(*argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return root.parse_args(["kanban", *argv])


def _ids():
    with kbc.connect_closing() as conn:
        return [t.id for t in kb.list_tasks(conn)]


def _events(tid, kind):
    with kbc.connect_closing() as conn:
        return [e.payload for e in kb.list_events(conn, tid) if e.kind == kind]


def _card(body="work"):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="t", assignee="peer", body=body, session_id="none",
                              session_explicit=True)


def test_refusal_names_words_known_caps_and_both_shapes(board):
    why = krw.refusal("x\n" + PROSE + "\n")
    assert why and "host=studio" in why and "bambuddy" in why and "and" in why
    assert "lan-reach, studio-state" in why                       # the known list
    assert "requires: <capability>" in why and "host: <pool host>" in why
    assert krw.refusal("x\n" + LEGAL + "\n") is None
    assert krw.refusal("x\nrequires: none\n") is None
    assert krw.refusal("no capability line, the word requires: mid-sentence") is None


def test_absent_or_invalid_map_refuses_nothing(board):
    (board / "fleet" / khc.MAP_FILE).write_text("{not json", encoding="utf-8")
    assert krw.refusal(PROSE) is None
    (board / "fleet" / khc.MAP_FILE).unlink()
    assert krw.refusal(PROSE) is None


def test_cli_create_refuses_the_t_00dabd92_line(board, capsys):
    args = _parse("create", "print plate", "--body", "slice it\n" + PROSE, "--unhomed", "--assignee", "peer")
    assert kc._cmd_create(args) == 2
    assert "host=studio" in capsys.readouterr().err
    assert _ids() == []


def test_cli_create_accepts_a_capability_with_a_note(board):
    args = _parse("create", "print plate", "--body", "slice it\n" + LEGAL, "--unhomed", "--assignee", "peer")
    assert kc._cmd_create(args) == 0
    assert len(_ids()) == 1


def test_cli_create_override_stores_and_records_event(board):
    args = _parse("create", "print plate", "--body", "slice it\nrequires: lan-reach, bambu",
                  "--unhomed", "--assignee", "peer", "--requires-unknown-ok", "new capability lands tomorrow")
    assert kc._cmd_create(args) == 0
    [tid] = _ids()
    [ev] = _events(tid, krw.OVERRIDE_EVENT)
    assert ev["words"] == ["bambu"] and ev["verb"] == "create"
    assert ev["reason"] == "new capability lands tomorrow"


def test_tool_create_refuses_the_line(board):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"board": "default", "title": "t", "assignee": "peer",
                                        "body": "slice it\n" + PROSE, "unhomed": True}))
    assert out.get("ok") is not True and "host=studio" in json.dumps(out), out
    assert _ids() == []
    ok = json.loads(kt._handle_create({"board": "default", "title": "t", "assignee": "peer",
                                       "body": "slice it\n" + LEGAL, "unhomed": True}))
    assert ok.get("ok") is True, ok


def test_cli_edit_body_refuses_then_override_records(board, capsys):
    tid = _card()
    with kbc.connect_closing() as conn:
        before = kb.get_task(conn, tid).body
    assert kc._cmd_edit(_parse("edit", tid, "--body", "slice it\n" + PROSE)) == 2
    assert "host=studio" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).body == before            # nothing written
    assert kc._cmd_edit(_parse("edit", tid, "--body", "slice it\n" + LEGAL)) == 0
    assert _events(tid, krw.OVERRIDE_EVENT) == []
    assert kc._cmd_edit(_parse("edit", tid, "--body", "x\nrequires: bambu",
                               "--requires-unknown-ok", "operator knows best here")) == 0
    [ev] = _events(tid, krw.OVERRIDE_EVENT)
    assert ev["words"] == ["bambu"] and ev["verb"] == "edit"


def _age(tid, seconds_ago):
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (seconds_ago, tid))
        conn.execute("UPDATE task_status_audit SET changed_at = changed_at - ? WHERE task_id = ?",
                     (seconds_ago, tid))


def _status(tid):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def test_backstop_wakes_once_for_an_unknown_word_and_not_again(board):
    bad = _card("x\n" + PROSE)            # a card that got there anyway (stored before the refusal)
    young = _card("x\nrequires: bambu")
    plain = _card("plain work")           # merely unclaimed: ordinary queueing, never woken
    with kbc.connect_closing() as conn:
        op = kb.create_task(conn, title="op", assignee="apollo", body="x\nrequires: bambu",
                            session_id="none", session_explicit=True)
    assert {_status(t) for t in (bad, young, plain, op)} == {"ready"}
    for t in (bad, plain, op):
        _age(t, 31 * 60)
    _age(young, 10 * 60)
    with kbc.connect_closing() as conn:
        assert krw.wake_stranded(conn) == [(bad, krw.UNKNOWN_CAUSE)]
        assert krw.wake_stranded(conn) == []                 # once, not again
        later = int(__import__("time").time()) + 7200      # only the young card has crossed 30 min since
        assert krw.wake_stranded(conn, now=later) == [(young, krw.UNKNOWN_CAUSE)]
        assert krw.wake_stranded(conn, now=later) == []
    [w] = _events(bad, krw.WAKE_EVENT)
    assert w["source"] == krw.WAKE_SOURCE and w["cause"] == krw.UNKNOWN_CAUSE and "host=studio" in w["text"]
    assert len(_events(young, krw.WAKE_EVENT)) == 1
    assert _events(plain, krw.WAKE_EVENT) == []                 # unclaimed alone: no wake
    assert _events(op, krw.WAKE_EVENT) == []                    # operator lane: never


def test_switch_off_stores_the_line_and_writes_no_wake(board):
    """``kanban.requires_unknown_guard: false`` turns off both halves."""
    (board / "config.yaml").write_text(json.dumps({"kanban": {krw.SWITCH: False}}), encoding="utf-8")
    assert krw.enabled() is False
    args = _parse("create", "print plate", "--body", "slice it\n" + PROSE, "--unhomed", "--assignee", "peer")
    assert kc._cmd_create(args) == 0
    [tid] = _ids()
    with kbc.connect_closing() as conn:
        assert PROSE in kb.get_task(conn, tid).body
    assert _events(tid, krw.OVERRIDE_EVENT) == []
    _age(tid, 31 * 60)
    with kbc.connect_closing() as conn:
        assert krw.wake_stranded(conn) == []
        kb.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
    assert _events(tid, krw.WAKE_EVENT) == []
    (board / "config.yaml").write_text(json.dumps({"kanban": {krw.SWITCH: "nope"}}), encoding="utf-8")
    assert krw.enabled() is True                               # non-bool -> default ON


def test_saturated_dispatch_tick_still_runs_the_backstop(board):
    """max_spawn reached returns before the spawn loop; the wake runs before that gate (Prism bbaa4c47da9a)."""
    tid = _card("x\nrequires: bambu")
    _age(tid, 31 * 60)
    with kbc.connect_closing() as conn:
        res = kb.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
    assert res.spawned == []
    [w] = _events(tid, krw.WAKE_EVENT)
    assert w["cause"] == krw.UNKNOWN_CAUSE


def test_idempotent_hit_records_no_override(board):
    """A create that returns an existing card did not store this body: no audit row (Prism c460b998ad15)."""
    assert kc._cmd_create(_parse("create", "print plate", "--body", "x\n" + LEGAL, "--unhomed",
                                 "--assignee", "peer", "--idempotency-key", "slice-1")) == 0
    assert kc._cmd_create(_parse("create", "print plate", "--body", "x\nrequires: typo", "--unhomed",
                                 "--assignee", "peer", "--idempotency-key", "slice-1",
                                 "--requires-unknown-ok", "temporary")) == 0
    [tid] = _ids()
    assert _events(tid, krw.OVERRIDE_EVENT) == []


def test_override_refused_inside_the_gateway(board, monkeypatch, capsys):
    """The operator flag is CLI-only: a gateway /kanban chat command cannot use it (Prism b882bd7e5b1c)."""
    monkeypatch.setattr(kb, "_process_is_gateway", lambda: True)
    assert kc._cmd_create(_parse("create", "p", "--body", "x\nrequires: bambu", "--unhomed",
                                 "--assignee", "peer", "--requires-unknown-ok", "why")) == 2
    assert "operator CLI flag" in capsys.readouterr().err
    assert _ids() == []
    monkeypatch.setattr(kb, "_process_is_gateway", lambda: False)
    tid = _card()
    monkeypatch.setattr(kb, "_process_is_gateway", lambda: True)
    assert kc._cmd_edit(_parse("edit", tid, "--body", "x\nrequires: bambu",
                               "--requires-unknown-ok", "why")) == 2
    assert _events(tid, krw.OVERRIDE_EVENT) == []
