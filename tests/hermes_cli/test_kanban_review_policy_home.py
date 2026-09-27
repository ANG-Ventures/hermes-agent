"""kanban.review_* settings resolve from the BOARD HOME, not the worker profile.

Defect F1 (2026-09-25): ``configured_review_policy()`` / ``_assignee()`` /
``_max_review_rounds()`` read ``load_config()`` inside the worker process,
which resolves ``<root>/profiles/<p>/config.yaml``. The board policy lives in
``<root>/config.yaml``, so every worker saw the built-in ``all`` and the
board's ``review_policy`` was never applied (0 ``review_skipped`` events).

E2E: real imports, two real homes on disk (board root + worker profile),
``HERMES_HOME`` switched between them. ``load_config`` is NOT mocked.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".hermes"
    prof_w = root / "profiles" / "w"
    prof_v = root / "profiles" / "v"
    for d in (prof_w, prof_v):
        d.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(prof_w))
    return root, prof_w, prof_v


def _write_kanban(home: Path, **kanban) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": kanban} if kanban else {"model": {"default": "x"}}),
        encoding="utf-8",
    )


def _use(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_worker_profile_reads_board_home_review_policy(homes, monkeypatch):
    root, prof_w, _ = homes
    _write_kanban(root, review_policy="none")
    _write_kanban(prof_w)  # profile config exists but has no kanban key
    _use(monkeypatch, prof_w)
    assert kb.kanban_home() == root
    assert kb.configured_review_policy() == "none"


def test_board_home_wins_over_profile_value(homes, monkeypatch, caplog):
    root, prof_w, _ = homes
    _write_kanban(root, review_policy="none")
    _write_kanban(prof_w, review_policy="all")
    _use(monkeypatch, prof_w)
    with caplog.at_level(logging.DEBUG, logger=kb.__name__):
        assert kb.configured_review_policy() == "none"
    assert "source=board_home" in caplog.text


def test_profile_value_used_when_board_home_silent(homes, monkeypatch, caplog):
    root, prof_w, _ = homes
    _write_kanban(root)  # board home writes no kanban section
    _write_kanban(prof_w, review_policy="milestone_only", review_assignee="human",
                  max_review_rounds=7)
    _use(monkeypatch, prof_w)
    with caplog.at_level(logging.DEBUG, logger=kb.__name__):
        assert kb.configured_review_policy() == "milestone_only"
    assert "source=profile" in caplog.text
    assert kb.configured_review_assignee() == "human"
    assert kb.configured_max_review_rounds() == 7

    # neither home writes the keys -> built-in defaults
    _write_kanban(prof_w)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=kb.__name__):
        assert kb.configured_review_policy() == "all"
    assert "source=default" in caplog.text
    assert kb.configured_max_review_rounds() == kb.DEFAULT_MAX_REVIEW_ROUNDS


def test_review_assignee_and_max_rounds_follow_board_home(homes, monkeypatch):
    root, prof_w, _ = homes
    _write_kanban(root, review_assignee="argus", max_review_rounds=1)
    _write_kanban(prof_w, review_assignee="human", max_review_rounds=9)
    _use(monkeypatch, prof_w)
    assert kb.configured_review_assignee() == "argus"
    assert kb.configured_max_review_rounds() == 1


def test_invalid_board_home_policy_still_fails_to_none(homes, monkeypatch, caplog):
    root, prof_w, _ = homes
    _write_kanban(root, review_policy="garbage")
    _write_kanban(prof_w, review_policy="all")
    _use(monkeypatch, prof_w)
    with caplog.at_level(logging.WARNING, logger=kb.__name__):
        assert kb.configured_review_policy() == "none"
    assert "review_policy_invalid" in caplog.text

    (root / "config.yaml").write_text("kanban: [unclosed\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=kb.__name__):
        assert kb.configured_review_policy() == "none"
    assert "review_policy_invalid" in caplog.text


def test_request_review_from_worker_profile_completes_in_place_under_root_none(
    homes, monkeypatch,
):
    root, prof_w, _ = homes
    _write_kanban(root, review_policy="none", max_review_rounds=0)
    _write_kanban(prof_w, review_assignee="human")  # like the live daedalus profiles
    _use(monkeypatch, prof_w)
    kb.init_db()
    assert kb.kanban_db_path().parent == root
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="slice: add flag", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        ok, reason = kb.request_review(
            conn, tid, summary="PR green", expected_run_id=claimed.current_run_id,
            with_reason=True,
        )  # reviewer omitted
        assert ok is True and "review skipped" in reason, reason
        assert kb.get_task(conn, tid).status == "done"
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        assert "review_skipped" in kinds and "review_requested" not in kinds
        payload = json.loads(next(r["payload"] for r in rows if r["kind"] == "review_skipped"))
        assert payload["policy"] == "none"


def test_board_home_answer_stable_across_profile_switch_aba(homes, monkeypatch):
    root, prof_w, prof_v = homes
    _write_kanban(root, review_policy="milestone_only", review_assignee="argus",
                  max_review_rounds=2)
    _write_kanban(prof_w, review_policy="all", review_assignee="human", max_review_rounds=5)
    _write_kanban(prof_v, review_policy="none", review_assignee="momus")

    def answer():
        return (kb.configured_review_policy(), kb.configured_review_assignee(),
                kb.configured_max_review_rounds())

    expected = ("milestone_only", "argus", 2)
    _use(monkeypatch, prof_w)
    a1 = answer()
    _use(monkeypatch, prof_v)
    b = answer()
    _use(monkeypatch, prof_w)
    a2 = answer()
    _use(monkeypatch, root)
    r = answer()
    assert a1 == b == a2 == r == expected
