"""Forge L0 section 2.3 (a), card PRE t_7ff229a7: the pre-PR verifier runs in complete_task before the open-PR route.

The verifier itself (hermes-home scripts/lib/landable.py) is replaced by a stub script via KANBAN_PRE_PR_SCRIPT:
rc 1 + a JSON line with `text` = refused, anything else = landable / fail-open.
"""

from __future__ import annotations

import json
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
    assert argv == ["check", "--repo", "o/r", "--pr", "7", "--card", tid, "--worker", "daedalus"]


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


def test_pre_pr_off_runs_nothing(kanban_home, tmp_path, monkeypatch):
    """No override + pre_pr missing/off in <root>/config/landing-policy.json: no subprocess at all."""
    monkeypatch.delenv("KANBAN_PRE_PR_SCRIPT", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    root = tmp_path / "vroot"
    (root / "config").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    marker = tmp_path / "ran"
    (root / "scripts" / "lib" / "landable.py").write_text(
        "open(%r, 'w').write('x')\nimport sys\nsys.exit(1)\n" % str(marker))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda **k: root)
    ref = op.extract_pr_refs(PR)
    for doc in (None, {"pre_pr": "off"}):
        if doc is not None:
            (root / "config" / "landing-policy.json").write_text(json.dumps(doc))
        assert kb._pre_pr_refusals(ref, task_id="t_x", worker="w") == []
        assert not marker.exists()
    (root / "config" / "landing-policy.json").write_text(json.dumps({"pre_pr": "on"}))
    assert [pr for pr, _ in kb._pre_pr_refusals(ref, task_id="t_x", worker="w")] == ["o/r#7"]
    assert marker.exists()
    assert sys.executable
