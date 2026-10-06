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
    tid = json.loads(kc.run_slash("create 't' --assignee alice --json"))["id"]
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


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_worker_cannot_forge_operator_author(board, monkeypatch, capsys, token):
    author, payload = _comment_as(monkeypatch, "daedalus", board, "human:apollo", token)
    assert author == "daedalus"
    assert payload.get("claimed_author") == "human:apollo"
    assert "claimed_author" in capsys.readouterr().err


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
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo")
    assert author == "daedalus"
    assert payload.get("claimed_author") == "human:apollo"
    assert "claimed_author" in capsys.readouterr().err


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


def test_operator_label_needs_token_without_proven_worker(board, monkeypatch):
    """d11c14fcd7fe: missing worker ancestry (a reparented helper) is never
    proof of operator status, so an operator LABEL needs the token from any
    caller other than the label itself."""
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo")
    assert author == "unverified:default"
    assert payload.get("claimed_author") == "human:apollo"
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo", TOKEN)
    assert author == "human:apollo"


def test_worker_whose_profile_is_default_loses_the_exemption(board, monkeypatch):
    """26d8f8ddc376: a dispatched run recorded as ``default`` is still a worker;
    the service-label exemption is keyed on worker status, not on its label."""
    import os

    _seed_worker_run(board, os.getpid(), profile="default")
    author, payload = _comment_as(monkeypatch, "default", board, "land-autopilot")
    assert author == "default"
    assert payload.get("claimed_author") == "land-autopilot"
    author, payload = _comment_as(monkeypatch, "default", board, "human:apollo")
    assert author == "default"
    assert payload.get("claimed_author") == "human:apollo"


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
    assert kc.kanban_command(top.parse_args(["kanban", "comment", tid, body])) == 0
    with kb.connect() as conn:
        return conn.execute(
            "SELECT author FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()["author"]


@pytest.mark.parametrize("profile", ["apollo", "default", "aegis"])
def test_own_operator_name_without_proof_is_not_an_operator_label(board, monkeypatch, profile):
    """The card's verify: no worker ancestry, HERMES_PROFILE=<operator>, no
    token, ``comment`` -> the stored author is not an operator label."""
    from hermes_cli.kanban_identity import is_operator_label

    author = _bare_comment(monkeypatch, profile, board)
    assert author == f"unverified:{profile}"
    assert not is_operator_label(author)
    # ...and --author <own name> takes the same road (no own-name fast path).
    author, payload = _comment_as(monkeypatch, profile, board, profile)
    assert not is_operator_label(author)
    assert payload.get("claimed_author") == profile


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
    assert ki._worker_rows([12345]) is None
    assert ki._runs_under_operator_gateway("apollo") is False


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
