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


def test_backstop_wakes_once_per_cause_and_not_again(board):
    bad = _card("x\n" + PROSE)            # a card that got there anyway (stored before the refusal)
    plain = _card("plain work")
    fresh = _card("plain work too")
    op = None
    with kbc.connect_closing() as conn:
        op = kb.create_task(conn, title="op", assignee="apollo", body="x", session_id="none",
                            session_explicit=True)
    assert {_status(t) for t in (bad, plain, fresh, op)} == {"ready"}
    for t in (bad, plain, op):
        _age(t, 31 * 60)
    _age(fresh, 10 * 60)
    with kbc.connect_closing() as conn:
        woke = krw.wake_stranded(conn)
        assert sorted(woke) == sorted([(bad, krw.UNKNOWN_CAUSE), (plain, krw.STRANDED_CAUSE)])
        assert krw.wake_stranded(conn) == []                 # once, not again
        later = int(__import__("time").time()) + 7200      # only the young card has crossed 30 min since
        assert krw.wake_stranded(conn, now=later) == [(fresh, krw.STRANDED_CAUSE)]
        assert krw.wake_stranded(conn, now=later) == []
    [w] = _events(bad, krw.WAKE_EVENT)
    assert w["source"] == krw.WAKE_SOURCE and "host=studio" in w["text"]
    assert len(_events(plain, krw.WAKE_EVENT)) == 1
    assert len(_events(fresh, krw.WAKE_EVENT)) == 1
    assert _events(op, krw.WAKE_EVENT) == []                    # operator lane: never


def test_backstop_skips_a_live_rate_limit_cooldown(board):
    import time
    tid = _card("plain")
    _age(tid, 31 * 60)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded",
                             {"reason": "rate_limit_cooldown", "eligible_at": int(time.time()) + 900})
        assert krw.wake_stranded(conn) == []
        assert krw.wake_stranded(conn, now=int(time.time()) + 1000) == [(tid, krw.STRANDED_CAUSE)]


def test_dispatcher_tick_calls_the_backstop():
    src = (Path(kb.__file__).parent / "kanban_db_dispatch.py").read_text(encoding="utf-8")
    assert "from hermes_cli.kanban_requires_words import wake_stranded" in src
