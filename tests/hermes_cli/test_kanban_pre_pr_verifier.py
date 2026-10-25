"""Forge L0 section 2.3 (a), card PRE t_7ff229a7: the pre-PR verifier runs in complete_task before the open-PR route.

The verifier itself (hermes-home scripts/lib/landable.py) is replaced by a stub script via KANBAN_PRE_PR_SCRIPT:
rc 1 + a JSON line with `text` = refused, anything else = landable / fail-open.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

PR = "o/r#7"


@pytest.fixture(autouse=True)
def _fixture_owners_are_fleet(monkeypatch):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    q = lambda repo, number: {"state": "OPEN"}  # noqa: E731
    monkeypatch.setattr(op, "_default_query", lambda: q)
    # the card's assignee is a real profile: the verifier runs with ITS env (Prism 3728a4da34a1)
    (home / "profiles" / "daedalus").mkdir(parents=True)
    (home / "profiles" / "daedalus" / ".env").write_text("GH_TOKEN=daedalus-token\n", encoding="utf-8")
    kb.init_db()
    return home


def _stub(tmp_path, monkeypatch, rc, text="pre-PR verifier refused o/r#7 @ x:\n  - draft: the PR is a draft -> ready it"):
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "landable_stub.py"
    script.write_text(
        "import json, sys\n"
        "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'landable': %r, 'text': %r}))\n"
        "sys.exit(%d)\n" % (str(calls), rc != 1, text, rc))
    monkeypatch.setenv("KANBAN_PRE_PR_SCRIPT", str(script))
    return calls


def _card(conn):
    tid = kb.create_task(conn, title="impl", assignee="daedalus")
    kb.claim_task(conn, tid)
    return tid


def _kinds(conn, tid):
    return [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def test_refused_pr_blocks_the_handoff_with_the_lander_text(kanban_home, tmp_path, monkeypatch):
    calls = _stub(tmp_path, monkeypatch, 1)
    with kb.connect() as conn:
        tid = _card(conn)
        with pytest.raises(kb.NotLandableError) as err:
            kb.complete_task(conn, tid, summary="shipped", survivor_pr=PR)
        assert "draft: the PR is a draft" in str(err.value) and "still in-flight" in str(err.value)
        assert err.value.prs == [PR]
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
        assert status == "running"
        assert "completion_blocked_pre_pr" in _kinds(conn, tid)
        assert "review_requested" not in _kinds(conn, tid)
    argv = json.loads(calls.read_text().splitlines()[0])
    assert argv == ["check", "--repo", "o/r", "--pr", "7", "--card", tid, "--worker", "daedalus", "--draft-ok"]


def test_drafts_stay_with_the_freshness_gate(kanban_home, tmp_path, monkeypatch):
    """Prism b1fdf6186eca: the verifier never pre-empts the freshness gate's draft handling (DraftPrError and the
    audited draft_ok override). The stub refuses unless it sees --draft-ok."""
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "landable_stub.py"
    script.write_text(
        "import json, sys\n"
        "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0 if '--draft-ok' in sys.argv else 1)\n" % str(calls))
    monkeypatch.setenv("KANBAN_PRE_PR_SCRIPT", str(script))
    monkeypatch.setattr("hermes_cli.kanban_pr_freshness.check", lambda *a, **k: {"prs": [], "draft_override": [PR]})
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    with kb.connect() as conn:
        tid = _card(conn)
        assert kb.complete_task(conn, tid, summary="ci vehicle", survivor_pr=PR, draft_ok="CI vehicle, left draft")
        assert "completion_blocked_pre_pr" not in _kinds(conn, tid)
    assert json.loads(calls.read_text().splitlines()[0])[-1] == "--draft-ok"


@pytest.mark.parametrize("rc", [0, 3])
def test_landable_or_unreadable_proceeds_to_the_open_pr_route(kanban_home, tmp_path, monkeypatch, rc):
    _stub(tmp_path, monkeypatch, rc)
    with kb.connect() as conn:
        tid = _card(conn)
        assert kb.complete_task(conn, tid, summary="shipped", survivor_pr=PR) is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "review"
        assert "completion_blocked_pre_pr" not in _kinds(conn, tid)


def test_prose_mention_is_not_verified(kanban_home, tmp_path, monkeypatch):
    calls = _stub(tmp_path, monkeypatch, 1)
    with kb.connect() as conn:
        tid = _card(conn)
        assert kb.complete_task(conn, tid, summary="see https://github.com/o/r/pull/7 for context") is True
    assert not calls.exists()


def test_branch_owned_pr_is_verified(kanban_home, tmp_path, monkeypatch):
    """Operator send-back on b1fdf6186eca: a PR the card owns by head branch is verified even when the handoff
    metadata names nothing."""
    calls = _stub(tmp_path, monkeypatch, 1)
    with kb.connect() as conn:
        tid = _card(conn)
        q = lambda repo, number: {"state": "OPEN", "head_ref": f"daedalus/{tid}-fix"}  # noqa: E731
        monkeypatch.setattr(op, "_default_query", lambda: q)
        with pytest.raises(kb.NotLandableError) as err:
            kb.complete_task(conn, tid, summary="shipped in https://github.com/o/r/pull/7")
        assert err.value.prs == [PR]
        assert "completion_blocked_pre_pr" in _kinds(conn, tid)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "running"
    assert calls.exists()


def test_pre_pr_off_runs_nothing(kanban_home, tmp_path, monkeypatch):
    """No override + pre_pr missing/off in <root>/config/landing-policy.json: no subprocess at all."""
    monkeypatch.delenv("KANBAN_PRE_PR_SCRIPT", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    root = tmp_path / "vroot"
    (root / "config").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    marker = tmp_path / "ran"
    (root / "scripts" / "lib" / "landable.py").write_text(
        "open(%r, 'w').write('x')\nimport json, sys\nprint(json.dumps({'landable': False}))\nsys.exit(1)\n" % str(marker))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda **k: root)
    ref = op.extract_pr_refs(PR)
    for doc in (None, {"pre_pr": "off"}):
        if doc is not None:
            (root / "config" / "landing-policy.json").write_text(json.dumps(doc))
        assert kb._pre_pr_refusals(ref, task_id="t_x", worker="daedalus") == []
        assert not marker.exists()
    (root / "config" / "landing-policy.json").write_text(json.dumps({"pre_pr": "on"}))
    assert [pr for pr, _ in kb._pre_pr_refusals(ref, task_id="t_x", worker="daedalus")] == ["o/r#7"]
    assert marker.exists()
    assert sys.executable


def test_verifier_crash_fails_open(kanban_home, tmp_path, monkeypatch):
    """Prism 0b37fb2ab9a0: CPython exits 1 on an uncaught exception. rc 1 is a refusal ONLY when the last stdout
    line is the verdict JSON; a raising verifier is 'did not run' and the handoff proceeds."""
    script = tmp_path / "landable_raises.py"
    script.write_text("print('gathering')\nraise KeyError('boom')\n")
    monkeypatch.setenv("KANBAN_PRE_PR_SCRIPT", str(script))
    with kb.connect() as conn:
        tid = _card(conn)
        assert kb.complete_task(conn, tid, summary="shipped", survivor_pr=PR) is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "review"
        assert "completion_blocked_pre_pr" not in _kinds(conn, tid)
    # a JSON last line that is NOT a refusal verdict (landable true / no key) is not a refusal either
    for line in ("{\"landable\": true}", "{\"pre_pr\": \"on\", \"error\": \"x\"}", "[1]"):
        script.write_text("import sys\nprint(%r)\nsys.exit(1)\n" % line)
        assert kb._pre_pr_refusals(op.extract_pr_refs(PR), task_id="t_x", worker="daedalus") == []


@pytest.mark.platforms("posix")
def test_verifier_runs_with_the_assignee_profile_env(kanban_home, tmp_path, monkeypatch):
    """Prism 3728a4da34a1: the subprocess gets the card's profile home + its own GH credentials, never the launch
    process's (a multiplexed gateway's os.environ is another profile's)."""
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    dump = tmp_path / "env.json"
    script = tmp_path / "landable_env.py"
    script.write_text("import json, os, sys\njson.dump(dict(os.environ), open(%r, 'w'))\n"
                      "print(json.dumps({'landable': False, 'text': 'refused'}))\nsys.exit(1)\n" % str(dump))
    monkeypatch.setenv("KANBAN_PRE_PR_SCRIPT", str(script))
    got = kb._pre_pr_refusals(op.extract_pr_refs(PR), task_id="t_x", worker="daedalus")
    assert got == [(PR, "refused")]
    env = json.loads(dump.read_text())
    assert env["GH_TOKEN"] == "daedalus-token"
    assert os.path.realpath(env["HERMES_HOME"]) == os.path.realpath(kanban_home / "profiles" / "daedalus")
    # an assignee whose profile cannot be resolved: did not run (fail-open), never the ambient login
    dump.unlink()
    assert kb._pre_pr_refusals(op.extract_pr_refs(PR), task_id="t_x", worker="ghost") == []
    assert not dump.exists()


def test_candidate_query_keeps_an_indeterminate_ambient_read(kanban_home, tmp_path, monkeypatch):
    """Prism 3a5427313c61: the assignee's read says NOT_FOUND (its profile cannot see the private repo) and the
    ambient read is indeterminate (None: timeout / rate limit). Before, that collapsed to NOT_FOUND and the PR
    dropped out of still_open, so its verifier never ran. None must survive: open_pr_refs keeps an unreadable PR
    as a candidate (fail-closed, t_1bd02e0b) and the verifier runs on it."""
    seen = []

    def fake_query(repo, number, env=None):
        seen.append(env is not None)
        return {"state": op.NOT_FOUND} if env is not None else None

    monkeypatch.setattr(op, "query_pr_state", fake_query)
    monkeypatch.setattr(op, "_default_query", lambda: op.query_pr_state)
    monkeypatch.setattr(kb, "_assignee_gh_env", lambda who: (True, {"GH_TOKEN": "daedalus-token"}))
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    calls = _stub(tmp_path, monkeypatch, 1)                 # the verifier refuses whatever it is handed
    with kb.connect() as conn:
        tid = _card(conn)
        with pytest.raises(kb.NotLandableError) as err:
            kb.complete_task(conn, tid, summary="shipped", survivor_pr=PR)
        assert err.value.prs == [PR]                         # the PR stayed a candidate and was verified
        assert "completion_blocked_pre_pr" in _kinds(conn, tid)
    assert seen[:2] == [True, False], seen                   # assignee first, then the ambient fallback
    assert calls.exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("answers_for", ["daedalus-token", "launch-token"])
def test_candidate_query_runs_as_the_assignee(kanban_home, tmp_path, monkeypatch, answers_for):
    """Prism 2125185a24e1: the open-PR candidate set is read with the SAME assignee env the verifier gets. A fake gh
    404s under the launch identity (another profile's token in a multiplexed gateway) and answers under the
    assignee's; the verifier must still run and refuse (on base the candidate set was empty and it was skipped).
    The converse (only the launch login can read it: an assignee with no login of its own) falls back to the
    ambient read, so the candidate set is never smaller than before."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$GH_TOKEN\" = %s ]; then\n"
        "  echo '{\"state\": \"open\", \"head\": {\"ref\": \"feature/x\"}}'; exit 0\n"
        "fi\n"
        "echo 'gh: Not Found (HTTP 404)' >&2; exit 1\n" % answers_for)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setattr(op, "_default_query", lambda: op.query_pr_state)
    if answers_for == "daedalus-token":
        assert op.query_pr_state("o/r", 7) == {"state": op.NOT_FOUND}  # the launch identity cannot see it
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)  # its own gh read: out of scope
    calls = _stub(tmp_path, monkeypatch, 1)
    with kb.connect() as conn:
        tid = _card(conn)
        with pytest.raises(kb.NotLandableError) as err:
            kb.complete_task(conn, tid, summary="shipped", survivor_pr=PR)
        assert err.value.prs == [PR]
        assert "completion_blocked_pre_pr" in _kinds(conn, tid)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "running"
    assert calls.exists()

