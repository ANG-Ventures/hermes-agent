"""Mint moratorium: a flag file stops automated producers minting NEW cards (t_fb98bedc).

Ace 2026-11-08: no automated producer creates a new kanban card; sending a finding back to the card that owns it
stays allowed. Pinned here: with ``<kanban root>/state/kanban-mint-moratorium`` present an automated
``created_by`` is refused before any insert and the refusal is ledgered; humans, the operator default and
worker-created cards are not; a re-mint that returns an existing card is not refused; ``--force`` mints and
ledgers; edits to an existing card are unaffected.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _flag_on():
    flag = kb.mint_moratorium_flag()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    return flag


def _rows(conn):
    p = kb.mint_cap_ledger_path(conn)
    if p is None or not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def test_flag_refuses_automated_creator_before_insert_and_ledgers(kanban_home):
    _flag_on()
    with kb.connect() as conn:
        with pytest.raises(kb.MintMoratoriumError):
            kb.create_task(conn, title="noise " * 40, created_by="noise-census")
        assert _count(conn) == 0
        rows = _rows(conn)
    assert [(r["outcome"], r["created_by"]) for r in rows] == [("moratorium_refused", "noise-census")]
    assert rows[0]["title"] == ("noise " * 40)[:120]


def test_no_flag_allows_automated_creator(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="a", created_by="noise-census")
        assert kb.get_task(conn, t) is not None
        assert _rows(conn) == []


@pytest.mark.parametrize("by", ["user", "operator", "apollo", "aegis", "human:ace", "", None])
def test_humans_and_operator_unaffected(kanban_home, by):
    _flag_on()
    with kb.connect() as conn:
        assert kb.create_task(conn, title="h", created_by=by)


def test_worker_created_card_unaffected(kanban_home):
    _flag_on()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="p", created_by="user")
        assert kb.create_task(conn, title="child", created_by="daedalus-opus", creator_task_id=parent)


def test_idempotent_remint_returning_existing_card_is_not_refused(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="a", created_by="main-tip-red", idempotency_key="k:1")
        s = kb.create_task(conn, title="b", created_by="main-tip-red", signature="sig:1")
        _flag_on()
        assert kb.create_task(conn, title="a", created_by="main-tip-red", idempotency_key="k:1") == t
        assert kb.create_task(conn, title="b", created_by="main-tip-red", signature="sig:1") == s
        assert _rows(conn) == []


def test_force_overrides_and_ledgers(kanban_home):
    _flag_on()
    with kb.connect() as conn:
        t = kb.create_task(conn, title="must", created_by="pager-owner-card", force_reason="operator: needed now")
        rows = _rows(conn)
        ev = [e.payload for e in kb.list_events(conn, t) if e.kind == "moratorium_forced"]
    assert [(r["outcome"], r["task_id"], r["reason"], r["created_by"]) for r in rows] == [
        ("moratorium_forced", t, "operator: needed now", "pager-owner-card")]
    assert ev == [{"created_by": "pager-owner-card", "reason": "operator: needed now"}]


def test_comment_reopen_and_sendback_on_existing_card_unaffected(kanban_home):
    with kb.connect() as conn:
        reopened = kb.create_task(conn, title="owner a", created_by="landing-queue")
        sent_back = kb.create_task(conn, title="owner b", assignee="alpha", created_by="landing-queue")
        _flag_on()
        before = _count(conn)
        kb.add_comment(conn, reopened, "prism-route", "finding: send back")
        assert kb.claim_task(conn, reopened) is not None
        assert kb.complete_task(conn, reopened, summary="done")
        ok, why = kb.reopen_task(conn, reopened, actor="prism-route", reason="CI red on its PR")
        assert ok, why
        assert kb.claim_task(conn, sent_back) is not None
        assert kb.request_review(conn, sent_back, summary="ready", force=True)
        ok, why = kb.request_changes(conn, sent_back, reason="fix the finding", claimer="apollo",
                                     operator="apollo: moratorium test send-back")
        assert ok, why
        assert _count(conn) == before
        assert _rows(conn) == []
