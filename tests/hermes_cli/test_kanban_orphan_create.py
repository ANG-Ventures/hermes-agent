"""Orphan cards option A, rung 1: refuse at birth / warn by hand (t_6281f908).

Ace 2026-10-03 14:42 (D-O1, D-O2). ``create`` from a cron, a worker, or the
gateway in-process CLI with no resolvable home is refused unless ``--unhomed``
or ``--session`` is given; a hand-typed CLI/TUI create warns and is allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

SID = "20261003_150000_homed"
ORIGINS = ("worker", "cron", "gateway", "hand", "script")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID",
                "HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_CRON_JOB_ID",
                "HERMES_CRON_SCRIPT", "HERMES_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(kb.ALLOW_UNHOMED_CREATE_ENV, raising=False)  # arm the refusal
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_process_is_gateway", lambda: False)
    monkeypatch.setattr("sys.stdin", _Stdin(False))
    kb.init_db()
    return home


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self, *a):
        return ""


def _as(origin: str, monkeypatch, *, worker_card: str = "") -> None:
    """Put this process in ``origin``'s shoes."""
    if origin == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", worker_card)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    elif origin == "cron":
        monkeypatch.setenv("HERMES_CRON_JOB_ID", "abc123")
        monkeypatch.setenv("HERMES_CRON_SCRIPT", "/x/scripts/main-red-watch.py")
    elif origin == "gateway":
        monkeypatch.setattr(kb, "_process_is_gateway", lambda: True)
    elif origin == "hand":
        monkeypatch.setattr("sys.stdin", _Stdin(True))


def _unhomed_worker_card() -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="w", session_id=None, session_explicit=True)


@pytest.mark.parametrize("origin", ORIGINS)
def test_classify_create_origin(kanban_home, monkeypatch, origin):
    _as(origin, monkeypatch, worker_card="t_x")
    assert kb.classify_create_origin() == origin


def test_tui_slash_worker_counts_as_hand(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")  # tui_gateway/slash_worker.py
    assert kb.classify_create_origin() == "hand"


def test_worker_outranks_cron_and_gateway(kanban_home, monkeypatch):
    for o in ("cron", "gateway", "worker"):
        _as(o, monkeypatch, worker_card="t_x")
    assert kb.classify_create_origin() == "worker"


# --- the matrix: class x {no home, --unhomed, --session, homed parent} -------


@pytest.mark.parametrize("origin,refused", [
    ("worker", True), ("cron", True), ("gateway", True), ("script", True),
    ("hand", False),
])
def test_no_home_no_flag(kanban_home, monkeypatch, capsys, origin, refused):
    _as(origin, monkeypatch, worker_card=_unhomed_worker_card())
    out = kc.run_slash("create 'orphan?' --assignee daedalus --json")
    with kb.connect_closing() as conn:
        titles = [t.title for t in kb.list_tasks(conn)]
    if refused:
        assert f"refused create ({origin})" in out and "--unhomed" in out
        assert "orphan?" not in titles
    else:
        assert "orphan?" in titles
        assert "has no home session" in out  # D-O2: warns, allowed


@pytest.mark.parametrize("origin", ORIGINS)
def test_explicit_unhomed_always_allowed(kanban_home, monkeypatch, origin):
    _as(origin, monkeypatch, worker_card=_unhomed_worker_card())
    created = json.loads(kc.run_slash("create 'on purpose' --unhomed --json"))
    assert created["session_id"] is None and created["unhomed"] is True


@pytest.mark.parametrize("origin", ORIGINS)
def test_explicit_session_always_allowed(kanban_home, monkeypatch, origin):
    _as(origin, monkeypatch, worker_card=_unhomed_worker_card())
    created = json.loads(kc.run_slash(f"create 'mine' --session {SID} --json"))
    assert created["session_id"] == SID


@pytest.mark.parametrize("origin", ORIGINS)
def test_homed_parent_resolves_home(kanban_home, monkeypatch, origin):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="p", session_id=SID, session_explicit=True)
    _as(origin, monkeypatch, worker_card=_unhomed_worker_card())
    created = json.loads(kc.run_slash(f"create 'child' --parent {parent} --json"))
    assert created["session_id"] == SID


def test_hand_create_with_session_stamps_it_and_does_not_warn(kanban_home, monkeypatch):
    _as("hand", monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_ID", SID)
    out = kc.run_slash("create 'typed' --json")
    assert json.loads(out.split("\n\n")[0])["session_id"] == SID
    assert "has no home session" not in out


def test_unhomed_is_exclusive_with_session_and_home(kanban_home):
    assert "exclusive" in kc.run_slash(f"create 'x' --unhomed --session {SID}")
    assert "exclusive" in kc.run_slash("create 'x' --unhomed --home operator")


def test_minted_by_provenance_on_created_event(kanban_home, monkeypatch):
    """Rung 2 reads ``minted_by`` to map a card to its cron job / minting run."""
    worker_card = _unhomed_worker_card()
    _as("cron", monkeypatch)
    cron_card = json.loads(kc.run_slash("create 'c' --unhomed --json"))["id"]
    monkeypatch.delenv("HERMES_CRON_JOB_ID")
    monkeypatch.delenv("HERMES_CRON_SCRIPT")
    _as("worker", monkeypatch, worker_card=worker_card)
    worker_child = json.loads(kc.run_slash("create 'w' --unhomed --json"))["id"]
    with kb.connect_closing() as conn:
        def minted(tid):
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='created'",
                (tid,)).fetchone()
            return json.loads(row[0])["minted_by"]
        assert minted(cron_card) == {"class": "cron", "cron_job": "abc123",
                                     "cron_script": "main-red-watch.py"}
        assert minted(worker_child) == {"class": "worker", "task": worker_card, "run_id": 42}


# --- the kanban_create tool -------------------------------------------------


def test_tool_create_refused_without_home_and_allowed_with_unhomed(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    _as("worker", monkeypatch, worker_card=_unhomed_worker_card())
    monkeypatch.setattr(kt, "_current_session_id", lambda: None)
    import tools.async_delegation as ad
    monkeypatch.setattr(ad, "_current_origin_session_id", lambda: None)
    refused = json.loads(kt._handle_create({"title": "t", "assignee": "daedalus"}))
    assert "refused create (worker)" in refused.get("error", "")
    ok = json.loads(kt._handle_create({"title": "t2", "assignee": "daedalus",
                                       "unhomed": True}))
    assert ok.get("task_id"), ok
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, ok["task_id"]).unhomed


def test_library_default_still_unhomed(kanban_home, monkeypatch):
    """``require_home`` stays a CLI/tool policy; library callers are unchanged."""
    _as("cron", monkeypatch)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="lib")
        assert kb.get_task(conn, tid).unhomed
