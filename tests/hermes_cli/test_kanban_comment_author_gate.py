"""``kanban comment --author`` cannot forge an operator author (Prism gate spec §8b).

Before: ``env -u HERMES_KANBAN_TASK hermes kanban comment --author human:apollo``
from a worker shell stored ``human:apollo`` as the comment author, and every
operator-trust reader (``_ruled_since_block``, the merge pass's changes_hold)
reads ``task_comments.author``. Now a non-operator caller without the operator
token writes as itself and the requested label is kept as ``claimed_author``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

TOKEN = "t0ken-for-tests"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_TASK", "HERMES_PROFILE_NAME", kb.OPERATOR_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    (home / "kanban").mkdir(exist_ok=True)
    kb.operator_token_path().write_text(TOKEN + "\n", encoding="utf-8")
    tid = json.loads(kc.run_slash("--board default create 't' --assignee alice --json"))["id"]
    return tid


@pytest.fixture
def operator_gateway(monkeypatch):
    """The root operator gateway is this test process's PARENT: the process sits
    in that gateway's tree, like Apollo's cron scripts and terminal shells."""
    import os

    from hermes_cli import kanban_identity as ki

    monkeypatch.setattr(
        ki, "_operator_gateway_pids",
        lambda name: frozenset({os.getppid()}) if name.strip().lower() in ("default", "apollo") else frozenset(),
    )
    return os.getppid()


def _comment_as(monkeypatch, profile, tid, author, token=None):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if token is None:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, token)
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    args = top.parse_args(["kanban", "comment", tid, "--author", author, "forged ruling"])
    assert kc.kanban_command(args) == 0
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented' "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
    return row["author"], json.loads(ev["payload"])


def _comment_rows(tid):
    with kb.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)
        ).fetchone()[0], conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='commented'", (tid,)
        ).fetchone()[0]


def _refused_as(monkeypatch, capsys, profile, tid, author, token=None):
    """``--author <operator label>`` without a valid token: rc != 0, nothing
    written, one stderr line naming the fix (t_6572add1)."""
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if token is None:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, token)
    before = _comment_rows(tid)
    capsys.readouterr()
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    args = top.parse_args(["kanban", "comment", tid, "--author", author, "forged ruling"])
    assert kc.kanban_command(args) != 0
    assert _comment_rows(tid) == before
    out = capsys.readouterr()
    assert "Comment added" not in out.out
    assert out.err.strip() == (
        f"--author {author} needs {kb.OPERATOR_TOKEN_ENV}="
        "$(cat ~/.hermes/kanban/operator-token); not posted"
    )


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_worker_cannot_forge_operator_author(board, monkeypatch, capsys, token):
    _refused_as(monkeypatch, capsys, "daedalus", board, "human:apollo", token)


# ── t_6572add1: --author <operator> without the token was a SILENT downgrade
# (rc 0, "Comment added", stored as unverified:default / default) that every
# operator-trust reader ignored: 182 rows in 24 h, a deadman FAILSAFE page on a
# card Apollo had answered. It now refuses and writes nothing.


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_author_apollo_without_token_is_refused(board, monkeypatch, capsys, token):
    _refused_as(monkeypatch, capsys, "default", board, "apollo", token)


def test_author_apollo_refused_even_from_the_operator_gateway_tree(
        board, monkeypatch, capsys, operator_gateway):
    """The 156 measured rows came from a proven ``default`` caller asking for
    ``apollo``: that downgrade (to ``default``) is refused as well."""
    _refused_as(monkeypatch, capsys, "default", board, "apollo")


def test_author_apollo_with_token_is_apollo_and_no_run(board, monkeypatch):
    author, payload = _comment_as(monkeypatch, "default", board, "apollo", TOKEN)
    assert author == "apollo"
    assert "claimed_author" not in payload
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT run_id FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (board,)
        ).fetchone()
    assert row["run_id"] is None


def test_no_author_keeps_the_unverified_path(board, monkeypatch):
    assert _bare_comment(monkeypatch, "default", board) == "unverified:default"


def test_operator_token_honours_author(board, monkeypatch):
    author, payload = _comment_as(monkeypatch, "daedalus", board, "human:apollo", TOKEN)
    assert author == "human:apollo"
    assert "claimed_author" not in payload


def test_operator_profile_keeps_its_service_label(board, monkeypatch, operator_gateway):
    """Fleet crons run as the operator profile and pick NON-operator labels on
    purpose (land-autopilot, themis): those must not collapse to ``default``."""
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "land-autopilot"
    assert "claimed_author" not in payload


# ── Prism 41fd439722e6 (#1782 post-merge): the operator-profile exemption was
# keyed on the caller-selected HERMES_PROFILE. A worker shell could run
# ``env -u HERMES_KANBAN_TASK HERMES_PROFILE=default ... --author human:apollo``.
# The exemption now holds only when the process is not running under a
# dispatched worker, read from process ancestry (task_runs.worker_pid + its
# spawn fingerprint), which the environment cannot rewrite.


def _seed_worker_run(tid, pid, profile="daedalus", fingerprint=None):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    fp = _process_fingerprint(pid) if fingerprint is None else fingerprint
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, worker_pid, "
            "worker_started_at, started_at) VALUES (?, ?, 'running', ?, ?, 0)",
            (tid, profile, pid, fp),
        )
        conn.commit()


def test_worker_selecting_default_profile_cannot_forge_author(board, monkeypatch, capsys):
    """RED on aaac711c6: the forged label was stored as the comment author."""
    import os

    _seed_worker_run(board, os.getpid())
    _refused_as(monkeypatch, capsys, "default", board, "human:apollo")


def test_worker_selecting_default_profile_writes_as_itself_without_author(board, monkeypatch):
    """The bare default (no --author) is the same env-derived name: it must not
    read ``default`` (a RULING_AUTHORS label) inside a worker either."""
    import os

    _seed_worker_run(board, os.getpid())
    monkeypatch.setenv("HERMES_PROFILE", "default")
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    assert kc.kanban_command(top.parse_args(["kanban", "comment", board, "APOLLO RULED x"])) == 0
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (board,)
        ).fetchone()
    assert row["author"] == "daedalus"


def test_worker_with_operator_token_still_honoured(board, monkeypatch):
    import os

    _seed_worker_run(board, os.getpid())
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo", TOKEN)
    assert author == "human:apollo"
    assert "claimed_author" not in payload


@pytest.mark.parametrize("fingerprint", ["0|1", ""])
def test_unproven_worker_row_is_not_a_worker(board, monkeypatch, operator_gateway, fingerprint):
    """A recycled pid (fingerprint mismatch) or a legacy row never re-labels
    the operator: the gate only acts on a proven worker."""
    import os

    _seed_worker_run(board, os.getpid(), fingerprint=fingerprint or None)
    if not fingerprint:
        with kb.connect() as conn:
            conn.execute("UPDATE task_runs SET worker_started_at=NULL")
            conn.commit()
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "land-autopilot"
    assert "claimed_author" not in payload


def test_unverified_spawn_fails_closed_for_authorship(board, monkeypatch, operator_gateway):
    """An ``unverified`` spawn row (create time unreadable) is not re-labelled to
    its worker profile, but it never lends operator authorship either: unlike
    kill/reap, authorship fails closed (t_3b9dbdb1)."""
    import os

    _seed_worker_run(board, os.getpid(), fingerprint="unverified")
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "unverified:default"
    assert payload.get("claimed_author") == "land-autopilot"


def test_agent_tool_identity_uses_worker_ancestry(board, monkeypatch):
    """Same class on the tool path: _persisted_identity is env-derived too."""
    import os

    from tools import kanban_tools as kt

    _seed_worker_run(board, os.getpid())
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kt._persisted_identity() == "daedalus"
    monkeypatch.setenv("HERMES_PROFILE", "argus")
    assert kt._persisted_identity() == "argus"   # non-operator names are not rewritten


def test_triage_sweep_author_is_gated(board, monkeypatch):
    """``specify``/``decompose --author`` writes the audit comment author: same gate."""
    seen = {}

    class _Mod:
        @staticmethod
        def list_triage_ids(tenant=None):
            return []

    def run_one(tid, author):
        seen["author"] = author
        return type("O", (), {"ok": True, "task_id": tid})()

    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    args = argparse.Namespace(all_triage=False, author="human:apollo", json=False,
                              tenant=None, task_id=board)
    kc._run_triage_sweep(args, "specify", _Mod, run_one, "specified", (), lambda o: "ok")
    assert seen["author"] == "daedalus"


# ── Prism round 1 on #1791 ──────────────────────────────────────────────────


def test_operator_label_needs_token_without_proven_worker(board, monkeypatch, capsys):
    """d11c14fcd7fe: missing worker ancestry (a reparented helper) is never
    proof of operator status, so an operator LABEL needs the token from any
    caller other than the label itself."""
    _refused_as(monkeypatch, capsys, "default", board, "human:apollo")
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo", TOKEN)
    assert author == "human:apollo"


def test_worker_whose_profile_is_default_loses_the_exemption(board, monkeypatch, capsys):
    """26d8f8ddc376: a dispatched run recorded as ``default`` is still a worker;
    the service-label exemption is keyed on worker status, not on its label."""
    import os

    _seed_worker_run(board, os.getpid(), profile="default")
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "default"
    assert payload.get("claimed_author") == "land-autopilot"
    _refused_as(monkeypatch, capsys, "default", board, "human:apollo")


@pytest.mark.usefixtures("kanban_pins")  # pins a kanban path on purpose (t_65791cd2)
def test_worker_on_another_board_is_found_under_a_repinned_db(board, monkeypatch, tmp_path):
    """9e1b780a6387: ``HERMES_KANBAN_DB`` repinned to board B must not hide the
    worker run recorded on board A."""
    import os

    from hermes_cli.kanban_identity import worker_ancestor_profile

    _seed_worker_run(board, os.getpid())          # board A = default
    kb.create_board("other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb._board_db_path_ignoring_pin("other")))
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert worker_ancestor_profile() == "daedalus"


# ── t_3b9dbdb1 (Prism e8be54683982 / 05c79d97284b, residue of #1791): the
# caller's OWN operator name was a fast path. An orphaned, setsid'd helper of a
# worker (reparented to init, new session: no worker pid in its ancestry or
# session) set HERMES_PROFILE=apollo and stored ``apollo`` with no run_id,
# which find_ruled_parent / _ruled_since_block read as a ruling.


def _bare_comment(monkeypatch, profile, tid, token=None, body="APOLLO RULED: go"):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if token is None:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, token)
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    # --board: the identity under test, not D3's card-id derivation (which fails closed when
    # another board is unreadable, as one test below arranges).
    assert kc.kanban_command(top.parse_args(["kanban", "--board", "default", "comment", tid, body])) == 0
    with kb.connect() as conn:
        return conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()["author"]


@pytest.mark.parametrize("profile", ["apollo", "default", "aegis"])
def test_own_operator_name_without_proof_is_not_an_operator_label(board, monkeypatch, capsys, profile):
    """The card's verify: no worker ancestry, HERMES_PROFILE=<operator>, no
    token, ``comment`` -> the stored author is not an operator label."""
    from hermes_cli.kanban_identity import is_operator_label

    author = _bare_comment(monkeypatch, profile, board)
    assert author == f"unverified:{profile}"
    assert not is_operator_label(author)
    # ...and --author <own name> gets no own-name fast path: refused.
    _refused_as(monkeypatch, capsys, profile, board, profile)


def test_token_carrying_operator_script_still_writes_apollo(board, monkeypatch):
    assert _bare_comment(monkeypatch, "apollo", board, TOKEN) == "apollo"
    author, payload = _comment_as(monkeypatch, "apollo", board, "apollo", TOKEN)
    assert author == "apollo"
    assert "claimed_author" not in payload


def test_operator_gateway_tree_keeps_its_name(board, monkeypatch, operator_gateway):
    """The gateway's in-process /kanban, its cron scripts and its sessions'
    shells sit in the operator gateway's process tree: no token needed."""
    assert _bare_comment(monkeypatch, "apollo", board) == "apollo"
    assert _bare_comment(monkeypatch, "default", board) == "default"


def test_reparented_helper_outside_the_gateway_tree_is_unverified(board, monkeypatch):
    """A live operator gateway that is NOT an ancestor (the helper was
    reparented to init) lends nothing."""
    import subprocess
    import sys

    from hermes_cli import kanban_identity as ki

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(ki, "_operator_gateway_pids", lambda name: frozenset({other.pid}))
        assert _bare_comment(monkeypatch, "apollo", board) == "unverified:apollo"
    finally:
        other.kill()
        other.wait()


def test_worker_under_the_operator_gateway_is_still_its_worker(board, monkeypatch, operator_gateway):
    """The dispatcher is the gateway: a worker below it is never the gateway."""
    import os

    _seed_worker_run(board, os.getpid())
    assert _bare_comment(monkeypatch, "apollo", board) == "daedalus"


def test_tool_identity_needs_proof_too(board, monkeypatch):
    """Same class on the tool path (_persisted_identity, Prism 05c79d97284b)."""
    from tools import kanban_tools as kt

    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kt._persisted_identity() == "unverified:default"
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, TOKEN)
    assert kt._persisted_identity() == "default"


def test_unverified_comment_is_not_a_ruling(board, monkeypatch):
    """End to end: the downgraded comment does not rule a parent."""
    from hermes_cli.kanban_worker_policy import find_ruled_parent

    _bare_comment(monkeypatch, "apollo", board)
    with kb.connect() as conn:
        ruled = find_ruled_parent(conn, [board])
    assert (ruled or {}).get("why") != "ruling_comment"
    _bare_comment(monkeypatch, "apollo", board, TOKEN)
    with kb.connect() as conn:
        assert find_ruled_parent(conn, [board])["why"] == "ruling_comment"


# ── Prism round 1 on #1793 ───────────────────────────────────────────────────


def test_unproven_human_label_is_not_an_operator_label(board, monkeypatch):
    """58ee6155c6e1: a suffix kept ``human:apollo-unverified`` inside the
    ``human:`` namespace that ``is_operator_label`` trusts."""
    from hermes_cli.kanban_identity import is_operator_label

    author = _bare_comment(monkeypatch, "human:apollo", board)
    assert author == "unverified:human:apollo"
    assert not is_operator_label(author)


def test_unreadable_board_is_never_proof_of_no_worker(board, monkeypatch, operator_gateway):
    """c6316b5d4c3b: a board that cannot be read may hold the worker row, so the
    gateway-tree proof is denied rather than read as "no worker below"."""
    import sqlite3

    from hermes_cli import kanban_identity as ki

    real = sqlite3.connect

    def locked(database, *a, **kw):
        if "mode=ro" in str(database):
            raise sqlite3.OperationalError("database is locked")
        return real(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", locked)
    assert ki._worker_rows([12345]) == ({}, False)
    assert ki._runs_under_operator_gateway("apollo") is False


def test_unreadable_other_board_keeps_a_verified_worker(board, monkeypatch):
    """c4bf93c192f3 (#1793 r2): one unreadable board must not discard a verified
    worker row on a readable board, or a worker with HERMES_PROFILE=default
    stores ``unverified:default`` instead of its own profile."""
    import os
    import sqlite3

    from hermes_cli import kanban_identity as ki

    _seed_worker_run(board, os.getpid())
    kb.create_board("other")
    other = str(kb._board_db_path_ignoring_pin("other").resolve())
    real = sqlite3.connect

    def one_locked(database, *a, **kw):
        if "mode=ro" in str(database) and other in str(database):
            raise sqlite3.OperationalError("database is locked")
        return real(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", one_locked)
    rows, complete = ki._worker_rows([os.getpid()])
    assert complete is False and os.getpid() in rows
    assert ki.worker_ancestor_profile() == "daedalus"
    assert _bare_comment(monkeypatch, "default", board) == "daedalus"


def test_routing_identity_is_never_prefixed(board, monkeypatch):
    """7c91838738d2 / 6c97e67077d9 (#1793 r2): notify ownership and --mine are
    routing, not authorship. An unproven operator shell keeps its name there
    (a gateway delivers only subscriptions its own profile owns)."""
    from hermes_cli.kanban_identity import routing_profile_identity

    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kc._profile_identity() == "default"
    assert kc._profile_author() == "unverified:default"
    assert routing_profile_identity("argus") == "argus"
    out = kc.run_slash(f"notify-subscribe {board} --platform discord --chat-id 1")
    with kb.connect() as conn:
        owners = {r[0] for r in conn.execute(
            "SELECT notifier_profile FROM kanban_notify_subs WHERE task_id=?", (board,))}
    assert owners == {"default"}, out


def test_multiplexer_serving_the_profile_counts_as_its_gateway(board, monkeypatch):
    """cfdcb98d4fbe: a served profile owns no gateway.pid; the host multiplexer
    that serves it is its gateway."""
    import os

    from gateway import status as gs
    from hermes_cli import kanban_identity as ki

    (kb.kanban_home() / "profiles" / "aegis").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gs, "live_gateway_pid_for_home", lambda home: None)
    monkeypatch.setattr(
        gs, "multiplexer_liveness_for_profile",
        lambda home: (os.getppid(), {}) if str(home).endswith("aegis") else None,
    )
    assert ki._operator_gateway_pids("aegis") == frozenset({os.getppid()})
    assert _bare_comment(monkeypatch, "aegis", board) == "aegis"



# ── t_238f3f04: a `deadman: hold <ISO>` stored as ``unverified:<name>`` holds
# nothing (the deadman honours operator_identity.comment_can_hold authors
# only), yet it printed "Comment added" and exited 0: 132 such rows in 12 h,
# two tier-2 pages to Ace. An unverified hold is now refused, nothing written.

HOLD = "deadman: hold 2099-01-01T00:00:00Z (waits on Prism)"
# The deadman reader's honoured authors (home scripts/lib/operator_identity.py
# HUMAN_OPERATORS); an ``unverified:`` row is never one of them.
_HONOURED = ("apollo", "ace", "default", "human", "aegis")


def _post(monkeypatch, profile, tid, body, author=None, token=None):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if token is None:
        monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, token)
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    argv = ["kanban", "--board", "default", "comment", tid, body]
    if author:
        argv[5:5] = ["--author", author]
    return kc.kanban_command(top.parse_args(argv))


def _last_author(tid):
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
    return row["author"], row["body"]


@pytest.mark.parametrize("body", [HOLD, "Waiting on CI.\ndeadman: hold 2099-01-01T00:00:00Z"])
def test_unverified_deadman_hold_is_refused_and_writes_nothing(board, monkeypatch, capsys, body):
    before = _comment_rows(board)
    capsys.readouterr()
    assert _post(monkeypatch, "default", board, body) != 0
    assert _comment_rows(board) == before
    out = capsys.readouterr()
    assert "Comment added" not in out.out
    err = out.err.strip()
    assert len(err.splitlines()) == 1
    assert "unverified:default" in err and "not posted" in err
    assert f"{kb.OPERATOR_TOKEN_ENV}=$(cat ~/.hermes/kanban/operator-token)" in err
    assert "--author apollo" in err


def test_deadman_hold_from_proven_session_is_stored_and_honoured(board, monkeypatch, operator_gateway):
    assert _post(monkeypatch, "default", board, HOLD) == 0
    author, body = _last_author(board)
    assert (author, body) == ("default", HOLD)
    assert author in _HONOURED


def test_deadman_hold_with_token_is_stored_and_honoured(board, monkeypatch):
    assert _post(monkeypatch, "default", board, HOLD, author="apollo", token=TOKEN) == 0
    author, body = _last_author(board)
    assert (author, body) == ("apollo", HOLD)
    assert author in _HONOURED


def test_ordinary_unverified_comment_still_posts(board, monkeypatch, capsys):
    for body in ("progress: waiting on CI", "the deadman: hold line is ignored mid-sentence"):
        assert _post(monkeypatch, "default", board, body) == 0
        assert _last_author(board) == ("unverified:default", body)
        assert "Comment added" in capsys.readouterr().out


# ── t_5a5e36db: agent PR 299 refused the unverified hold in the CLI verb only.
# The kanban_comment tool (probe F, stored ``unverified:default``) and a worker
# run (probe G, stored under the worker's name) still stored a hold the deadman
# ignores. The refusal now sits in kanban_db.add_comment, which every door shares.


def _tool_comment(tid, body):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_comment({"task_id": tid, "body": body}))


@pytest.mark.parametrize("profile,author", [("default", "unverified:default"), ("daedalus", "daedalus")])
def test_tool_deadman_hold_from_non_operator_is_refused_and_writes_nothing(board, monkeypatch, profile, author):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    before = _comment_rows(board)
    out = _tool_comment(board, HOLD)
    assert "ok" not in out, out
    msg = json.dumps(out)
    assert f"from {author} holds nothing" in msg and "--author apollo" in msg and kb.OPERATOR_TOKEN_ENV in msg
    assert _comment_rows(board) == before


def test_worker_run_deadman_hold_is_refused_and_writes_nothing(board, monkeypatch, capsys):
    import os

    _seed_worker_run(board, os.getpid())
    before = _comment_rows(board)
    capsys.readouterr()
    assert _post(monkeypatch, "default", board, HOLD) != 0
    assert _comment_rows(board) == before
    out = capsys.readouterr()
    assert "Comment added" not in out.out
    err = out.err.strip()
    assert len(err.splitlines()) == 1
    assert "from daedalus holds nothing" in err and "--author apollo" in err and "not posted" in err


def test_worker_run_hold_shapes_pr299_allows_still_post(board, monkeypatch):
    import os

    _seed_worker_run(board, os.getpid())
    for body in ("the deadman: hold line is ignored mid-sentence", "> deadman: hold 2099-01-01T00:00:00Z",
                 "`deadman: hold 2099-01-01T00:00:00Z`", "deadman: hold", "deadman: STOP"):
        monkeypatch.setenv("HERMES_PROFILE", "default")
        assert _tool_comment(board, body).get("ok") is True, body
        assert _last_author(board) == ("daedalus", body)


def test_add_comment_refuses_hold_from_any_writer(board):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="holds nothing"):
            kb.add_comment(conn, board, "unverified:default", HOLD)
        with pytest.raises(ValueError, match="holds nothing"):
            kb.add_comment(conn, board, "apollo (subagent)", HOLD)
        kb.add_comment(conn, board, "apollo", HOLD)
    assert _last_author(board) == ("apollo", HOLD)


# Prism e233b2a07b96: block/schedule write their status comment AFTER the transition, so a hold
# refused in add_comment left the card blocked with no comment. The hold is judged before the transition.
@pytest.mark.parametrize("verb", ["block", "schedule"])
def test_lifecycle_reason_with_hold_is_refused_before_the_transition(board, monkeypatch, verb):
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    with kb.connect() as conn:
        status = kb.get_task(conn, board).status
    before = _comment_rows(board)
    top = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(top.add_subparsers(dest="command"))
    argv = ["kanban", "--board", "default", verb, board, "waiting\n" + HOLD]
    argv[5:5] = ["--at", "2099-01-01T00:00:00+00:00"] if verb == "schedule" else ["--kind", "needs_input"]
    try:
        rc = kc.kanban_command(top.parse_args(argv))
    except ValueError as exc:
        assert "holds nothing" in str(exc)
        rc = 1
    assert rc != 0
    with kb.connect() as conn:
        assert kb.get_task(conn, board).status == status
    assert _comment_rows(board) == before
