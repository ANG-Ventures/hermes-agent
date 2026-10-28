"""Scoped board-signing grants for same-model delegate children (t_19c567a8).

Contract: a granted comment from a delegate child lands as the grant's ``by`` author with
``task_comments.grant_id`` set, and ONLY inside the grant (card, verb, session, expiry,
kill switch, never a PR the parent profile's runs authored); every refusal writes nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from agent.delegation_context import delegated_child_context  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_sign_grants as ksg  # noqa: E402

CHILD_SID = "child-session-1"
MODEL = "claude-opus-5-5"
SHA = "a" * 40


def _approve(pr="ANG-Ventures/hermes-home#9001"):
    rec = {"verdict": "APPROVE", "head_sha": SHA, "pr": pr, "by": "apollo"}
    return "apollo manual: APPROVE @ aaaaaaa\nreview_coverage: " + json.dumps(rec)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN") or k == "HERMES_DELEGATED_CHILD_CONTEXT"]:
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    (home / "config.yaml").write_text("kanban:\n  sign_grants: 'on'\n")
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="review card A")
        b = kb.create_task(conn, title="review card B")
    return home, a, b


def _mint(card, **kw):
    args = dict(cards=[card], verbs=["verdict", "approve"], by="apollo",
                child_session_ref=kb.derive_session_ref(CHILD_SID), parent_model=MODEL, child_model=MODEL,
                parent_profiles=["default", "apollo"])
    args.update(kw)
    with kb.connect_closing() as conn:
        return ksg.mint(conn, **args)


def _child_comment(card, body, grant_id, *, sid=CHILD_SID, author="apollo"):
    with delegated_child_context():
        with kb.connect_closing() as conn:
            return kb.add_comment(conn, card, author, body, session_ref=kb.derive_session_ref(sid), grant_id=grant_id)


def _rows(card):
    with kb.connect_closing() as conn:
        return [dict(r) for r in conn.execute("SELECT author, grant_id, body FROM task_comments WHERE task_id=?",
                                              (card,))]


def test_in_scope_granted_approve_lands_as_by_with_grant_id(board):
    _home, a, _b = board
    g = _mint(a)
    _child_comment(a, _approve(), g["grant_id"])
    rows = _rows(a)
    assert [(r["author"], r["grant_id"]) for r in rows] == [("apollo", g["grant_id"])]


def test_without_grant_the_child_comment_stays_subagent_marked(board):
    _home, a, _b = board
    with delegated_child_context():
        with kb.connect_closing() as conn:
            kb.add_comment(conn, a, "apollo", _approve())
    assert [(r["author"], r["grant_id"]) for r in _rows(a)] == [("apollo (subagent)", None)]


@pytest.mark.parametrize("case", ["other_card", "wrong_verb", "wrong_session", "wrong_author", "expired", "revoked"])
def test_granted_write_outside_the_grant_is_refused_and_writes_nothing(board, case):
    _home, a, b = board
    g = _mint(a, verbs=["verdict"] if case == "wrong_verb" else ["verdict", "approve"])
    card, sid, author = a, CHILD_SID, "apollo"
    if case == "other_card":
        card = b
    elif case == "wrong_session":
        sid = "some-other-session"
    elif case == "wrong_author":
        author = "ace"
    elif case == "expired":
        with kb.connect_closing() as conn, kb.write_txn(conn):
            conn.execute("UPDATE sign_grants SET expires_at = 1 WHERE grant_id = ?", (g["grant_id"],))
    elif case == "revoked":
        with kb.connect_closing() as conn:
            assert ksg.revoke(conn, g["grant_id"]) == 1
    with pytest.raises(ksg.GrantRefused):
        _child_comment(card, _approve(), g["grant_id"], sid=sid, author=author)
    assert _rows(card) == []


def test_mint_refuses_a_model_mismatch(board):
    _home, a, _b = board
    with pytest.raises(ksg.GrantRefused, match="model"):
        _mint(a, child_model="claude-sonnet-5")


def test_grant_never_covers_a_pr_the_parent_profile_authored(board):
    _home, a, b = board
    own = "https://github.com/ANG-Ventures/hermes-home/pull/777"
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                     (a, "default", "done", 1, json.dumps({"pr_url": own})))
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                     (b, "daedalus", "done", 1, json.dumps({"pr_url": own})))
    # Mint: the card's own PR came from the parent's profile.
    with pytest.raises(ksg.GrantRefused, match="own PR"):
        _mint(a)
    # Write: a card whose PR came from another profile is grantable, but an APPROVE naming the
    # parent profile's PR on it is still refused.
    g = _mint(b)
    with pytest.raises(ksg.GrantRefused, match="authored"):
        _child_comment(b, _approve(pr="ANG-Ventures/hermes-home#777"), g["grant_id"])
    _child_comment(b, _approve(pr="ANG-Ventures/hermes-home#778"), g["grant_id"])
    assert len(_rows(b)) == 1


def test_kill_switch_off_refuses_mint_and_every_granted_write(board):
    home, a, _b = board
    g = _mint(a)
    (home / "config.yaml").write_text("kanban:\n  sign_grants: 'off'\n")
    with pytest.raises(ksg.GrantRefused, match="off"):
        _child_comment(a, _approve(), g["grant_id"])
    with pytest.raises(ksg.GrantRefused, match="off"):
        _mint(a)
    assert _rows(a) == []


def test_parent_cannot_sign_under_a_child_grant(board):
    _home, a, _b = board
    g = _mint(a)
    with kb.connect_closing() as conn, pytest.raises(ksg.GrantRefused, match="delegate child"):
        kb.add_comment(conn, a, "apollo", _approve(), session_ref=kb.derive_session_ref(CHILD_SID),
                       grant_id=g["grant_id"])
    assert _rows(a) == []


def _cli(home, *args, sid):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
    env.pop("KANBAN_OPERATOR_TOKEN", None)
    env.update(HERMES_HOME=str(home), HERMES_DELEGATED_CHILD_CONTEXT=str(home), HERMES_SESSION_ID=sid,
               PYTHONPATH=str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""))
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False, timeout=120)


def test_cli_comment_grant_from_a_child_process(board, tmp_path):
    home, a, b = board
    g = _mint(a)
    body = tmp_path / "body.txt"
    body.write_text(_approve())
    ok = _cli(home, "comment", a, "--author", "apollo", "--grant", g["grant_id"], "--body-file", str(body),
              sid=CHILD_SID)
    assert ok.returncode == 0, ok.stderr
    refused = _cli(home, "comment", b, "--author", "apollo", "--grant", g["grant_id"], "--body-file", str(body),
                   sid=CHILD_SID)
    assert refused.returncode == 3 and "refused" in refused.stderr
    assert [(r["author"], r["grant_id"]) for r in _rows(a)] == [("apollo", g["grant_id"])]
    assert _rows(b) == []
