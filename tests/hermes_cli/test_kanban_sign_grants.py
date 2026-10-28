"""Scoped board-signing grants for same-model delegate children (t_19c567a8).

Contract: a granted comment from a delegate child is stored under the grant-marked author
``[grant:<id>] <by> (subagent)`` with ``task_comments.grant_id`` set (only
``operator_rows_with_live_grants`` promotes it back to ``by``), and ONLY inside the grant (card, verb, session, expiry,
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


def _marked(g):
    return f"[grant:{g['grant_id']}] apollo (subagent)"


def test_in_scope_granted_approve_lands_grant_marked_with_grant_id(board):
    _home, a, _b = board
    g = _mint(a)
    _child_comment(a, _approve(), g["grant_id"])
    rows = _rows(a)
    assert [(r["author"], r["grant_id"]) for r in rows] == [(_marked(g), g["grant_id"])]


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
    assert [(r["author"], r["grant_id"]) for r in _rows(a)] == [(_marked(g), g["grant_id"])]
    assert _rows(b) == []


def test_unquoted_yaml_on_enables_grants(board):
    """`sign_grants: on` (YAML 1.1 bool True) is the documented spelling and must enable the feature."""
    home, a, _b = board
    (home / "config.yaml").write_text("kanban:\n  sign_grants: on\n")
    assert ksg.enabled()
    (home / "config.yaml").write_text("kanban:\n  sign_grants: off\n")
    assert not ksg.enabled()


def test_granted_release_honours_if_latest_release(board):
    """--if-latest-release keeps its compare-and-append guard on the granted path."""
    from hermes_cli.kanban_close_on_guard import ReleaseSuperseded

    _home, a, _b = board
    g = _mint(a, verbs=["release"])
    with kb.connect_closing() as conn:
        first = kb.add_comment(conn, a, "apollo", "close-on: manual: held for review")
        kb.add_comment(conn, a, "apollo", "close-on: manual: newer hold")
    with delegated_child_context(), kb.connect_closing() as conn, pytest.raises(ReleaseSuperseded):
        kb.add_comment(conn, a, "apollo", "close-on: merged", session_ref=kb.derive_session_ref(CHILD_SID),
                       grant_id=g["grant_id"], if_latest_release=first)
    assert [r["grant_id"] for r in _rows(a)] == [None, None]


def test_journal_carries_grant_id(board, monkeypatch):
    """A restore must keep a granted row granted (voidable), never turn it into a plain operator comment."""
    from hermes_cli import kanban_journal

    _home, a, _b = board
    seen = []
    monkeypatch.setattr(kanban_journal, "append", lambda *args, **kw: seen.append(args) or True)
    g = _mint(a)
    _child_comment(a, _approve(), g["grant_id"])
    bodies = [p for (_b2, _t, kind, p) in seen if kind == "comment_body"]
    assert bodies and bodies[-1]["grant_id"] == g["grant_id"]


def _wrapped(body):
    """The inline-code form kanban_db._review_coverage_payload also accepts."""
    return "\n".join(f"`{ln}`" if ln.startswith("review_coverage:") else ln for ln in body.splitlines())


def test_backtick_wrapped_coverage_is_held_to_the_grant(board):
    """Prism 9e7e6653e13e: a backticked record is the same record to the kernel, so the grant reads it too."""
    _home, a, b = board
    assert ksg.body_verbs(_wrapped(_approve())) == {"approve"}
    assert ksg.body_pr_keys(_wrapped(_approve())) == {"ang-ventures/hermes-home#9001"}
    g = _mint(a, verbs=["verdict"])
    with pytest.raises(ksg.GrantRefused):
        _child_comment(a, _wrapped(_approve()), g["grant_id"])
    assert _rows(a) == []
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                     (b, "default", "done", 1,
                      json.dumps({"pr_url": "https://github.com/ANG-Ventures/hermes-home/pull/777"})))
    g2 = _mint(a)
    with pytest.raises(ksg.GrantRefused, match="authored"):
        _child_comment(a, _wrapped(_approve(pr="ANG-Ventures/hermes-home#777")), g2["grant_id"])
    assert _rows(a) == []


def test_coverage_close_on_is_a_release_verb(board):
    """Prism 1654e8f04fd7: a record's close_on is release authority, so a verdict-only grant cannot carry it."""
    _home, a, _b = board
    rec = {"verdict": "COMMENT", "head_sha": SHA, "close_on": "merged"}
    body = "note\nreview_coverage: " + json.dumps(rec)
    assert ksg.body_verbs(body) == {"verdict", "release"}
    g = _mint(a, verbs=["verdict"])
    with pytest.raises(ksg.GrantRefused, match="release"):
        _child_comment(a, body, g["grant_id"])
    assert _rows(a) == []
    g2 = _mint(a, verbs=["verdict", "release"])
    _child_comment(a, body, g2["grant_id"])
    assert [r["grant_id"] for r in _rows(a)] == [g2["grant_id"]]


@pytest.mark.parametrize("revoke", [False, True])
def test_granted_note_is_never_a_ruling(board, revoke):
    """Prism 5cba1d54401d / Apollo r8: a granted row is stored grant-marked, so the label readers
    find_ruled_parent and _ruled_since_block never take it for an operator ruling, live or revoked,
    with no per-reader grant filter; an operator's own comment still rules."""
    from hermes_cli.kanban_worker_policy import find_ruled_parent

    _home, a, _b = board
    with kb.connect_closing() as conn:
        kb.block_task(conn, a, reason="needs a ruling")
    g = _mint(a, verbs=["verdict"])
    _child_comment(a, "APOLLO RULED: go ahead", g["grant_id"])
    if revoke:
        with kb.connect_closing() as conn:
            ksg.revoke(conn, g["grant_id"])
    with kb.connect_closing() as conn:
        assert (find_ruled_parent(conn, [a]) or {}).get("why") != "ruling_comment"
        assert kb._ruled_since_block(conn, a) is False
        kb.add_comment(conn, a, "apollo", "APOLLO RULED: go ahead")
        assert find_ruled_parent(conn, [a])["why"] == "ruling_comment"
        assert kb._ruled_since_block(conn, a) is True


def test_live_grant_rows_are_promoted_only_through_the_one_function(board):
    """Apollo r8 (Prism 568d804ef684): operator_rows_with_live_grants is the ONE promotion. A live
    grant's row comes back as `by`; revoked, expired, a forged marked label without the grant_id
    column value, and another card's grant never do."""
    _home, a, b = board
    g = _mint(a)
    _child_comment(a, _approve(), g["grant_id"])
    with kb.connect_closing() as conn:
        rows = ksg.operator_rows_with_live_grants(conn, a)
        assert [(r["author"], r["grant_id"]) for r in rows] == [("apollo", g["grant_id"])]
        assert rows[0]["stored_author"] == _marked(g)
        # Forged label: an ordinary writer posts the marked author with no grant_id.
        kb.add_comment(conn, a, _marked(g), "forged note")
        # Another card: card B's comment names A's grant in its label.
        kb.add_comment(conn, b, _marked(g), "cross-card note")
        assert len(ksg.operator_rows_with_live_grants(conn, a)) == 1
        assert ksg.operator_rows_with_live_grants(conn, b) == []
        with kb.write_txn(conn):
            conn.execute("UPDATE sign_grants SET expires_at = 1 WHERE grant_id = ?", (g["grant_id"],))
        assert ksg.operator_rows_with_live_grants(conn, a) == []
        with kb.write_txn(conn):
            conn.execute("UPDATE sign_grants SET expires_at = ? WHERE grant_id = ?", (2**31, g["grant_id"]))
        assert len(ksg.operator_rows_with_live_grants(conn, a)) == 1
        ksg.revoke(conn, g["grant_id"])
        assert ksg.operator_rows_with_live_grants(conn, a) == []


def test_a_granted_row_is_never_an_operator_label(board):
    """The stored author fails every label predicate in this repo (is_operator_label, the ruling set,
    the human sentinel), so a reader that trusts the author honours nothing by label."""
    from hermes_cli.kanban_db import is_human_reviewer
    from hermes_cli.kanban_identity import is_operator_label

    _home, a, _b = board
    g = _mint(a)
    _child_comment(a, _approve(), g["grant_id"])
    (stored,) = [r["author"] for r in _rows(a)]
    assert not is_operator_label(stored) and not is_human_reviewer(stored)
    assert ksg.parse_granted_author(stored) == ("apollo", g["grant_id"])


def test_model_names_share_one_normaliser():
    """Prism 9b929567c500: the provider spelling and the wire spelling of one model are the same model."""
    assert ksg._norm_model("anthropic/claude-opus-4.6") == ksg._norm_model("claude-opus-4-6")
    assert ksg._norm_model("claude-opus-4-6") != ksg._norm_model("claude-sonnet-4-6")


def test_mint_accepts_one_model_in_two_spellings(board):
    _home, a, _b = board
    g = _mint(a, parent_model="anthropic/claude-opus-4.6", child_model="claude-opus-4-6")
    _child_comment(a, _approve(), g["grant_id"])
    assert [r["grant_id"] for r in _rows(a)] == [g["grant_id"]]


def test_a_granted_coverage_record_never_defines_the_seen_head(board):
    """Apollo r8: kanban_handback_new_head.seen_head trusts an operator label inside a send-back window;
    a granted record (live or not) is grant-marked, so it never redefines the head the reviewer saw."""
    from hermes_cli import kanban_handback_new_head as nh
    from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

    def cov(head):
        return "review_coverage: " + json.dumps({"lenses": {k: "done" for k in REQUIRED_REVIEW_LENSES},
                                                 "findings": 1, "items": ["x"], "review_minutes": 1,
                                                 "batch_id": "b", "head_sha": head})

    _home, a, _b = board
    g = _mint(a, verbs=["verdict", "request_changes"])
    _child_comment(a, cov("c" * 40), g["grant_id"])
    with kb.connect_closing() as conn:
        top = conn.execute("SELECT MAX(id) FROM task_comments").fetchone()[0]
        with kb.write_txn(conn):
            ev = conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                              (a, "changes_requested", json.dumps({"after_comment_id": top}), 1)).lastrowid
        assert nh.seen_head(conn, a, ev) is None
        kb.add_comment(conn, a, "apollo", cov("a" * 40))
        top = conn.execute("SELECT MAX(id) FROM task_comments").fetchone()[0]
        with kb.write_txn(conn):
            ev = conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                              (a, "changes_requested", json.dumps({"after_comment_id": top}), 2)).lastrowid
        assert nh.seen_head(conn, a, ev) == "a" * 40


def test_a_human_scheme_grant_never_stores_an_operator_label(board):
    """Prism 779afdc7e72e: a `human:` by stays out of the `human:` namespace once stored, so the
    startswith("human:") readers (is_operator_label, the close-on gate_setter) honour nothing; the
    one promotion restores the full `by`."""
    from hermes_cli import kanban_close_on_guard as guard
    from hermes_cli.kanban_identity import is_operator_label

    _home, a, _b = board
    g = _mint(a, by="human:apollo")
    _child_comment(a, _approve(), g["grant_id"], author="human:apollo")
    (stored,) = [r["author"] for r in _rows(a)]
    assert not stored.lower().startswith("human:")
    assert not is_operator_label(stored)
    from types import SimpleNamespace as NS

    setter = guard.gate_setter_from(NS(is_ours=lambda a: False, is_machine=lambda b: False),
                                    NS(is_operator=lambda a: False))
    assert setter("human:apollo", "x") and not setter(stored, _approve())
    with kb.connect_closing() as conn:
        assert [r["author"] for r in ksg.operator_rows_with_live_grants(conn, a)] == ["human:apollo"]


def test_a_release_line_naming_an_own_pr_is_refused(board):
    """Prism 6bfefb466a37: the parent's PR is recorded on card A; a release grant on unrelated card B
    must not release it through a standalone close-on line."""
    _home, a, b = board
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                     (a, "default", "done", 1, json.dumps({"pr_url": "https://github.com/acme/repo/pull/7"})))
    g = _mint(b, verbs=["verdict", "release"])
    with pytest.raises(ksg.GrantRefused, match="authored"):
        _child_comment(b, "close-on: https://github.com/acme/repo/pull/7", g["grant_id"])
    assert _rows(b) == []
    _child_comment(b, "close-on: https://github.com/acme/repo/pull/8", g["grant_id"])
    assert len(_rows(b)) == 1


def test_a_child_revokes_only_its_own_grant_by_its_executing_session(board):
    """Prism 27c3b03d5260: inside a delegate child, revoke() takes "own" from the session executing the call,
    never from an argument a sibling could copy off another child's row. Child B is refused A's grant; A
    revokes its own."""
    _home, a, _b = board
    g = _mint(a)
    with delegated_child_context("sibling-child-sid"):
        with kb.connect_closing() as conn, pytest.raises(ksg.GrantRefused, match="another session"):
            ksg.revoke(conn, g["grant_id"])
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT revoked_at FROM sign_grants WHERE grant_id=?", (g["grant_id"],)).fetchone()[0] is None
    with delegated_child_context(CHILD_SID):
        with kb.connect_closing() as conn:
            assert ksg.revoke(conn, g["grant_id"]) == 1
