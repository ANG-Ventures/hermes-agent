"""``kanban.review_policy`` — which cards need a same-card review lane.

Ace ruling 2026-09-24: tests are CI's job; the reviewer profile is milestone
QA only. Under ``milestone_only`` a plain slice card that calls
``request_review`` completes in place (``review_skipped`` event) instead of
parking in ``review`` on the reviewer; a ``[milestone]``-tagged card or a
parent card (has children) still goes to review. ``none`` skips every card;
``always`` (the default, and any unknown value) preserves the pre-policy
behaviour exactly.

Contract, not snapshot: every test relates the landed status / events to the
policy + card shape, never to a frozen payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fleet_profiles(monkeypatch: pytest.MonkeyPatch):
    installed = {"argus", "daedalus"}
    monkeypatch.setattr(kb, "spawnable_reviewer_profiles", lambda: sorted(installed))
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: str(name).lower() in installed)
    return installed


def _policy(monkeypatch: pytest.MonkeyPatch, value):
    monkeypatch.setattr(
        kb, "configured_review_policy",
        lambda: value if value in kb.REVIEW_POLICIES else "always",
    )


def _events(conn, tid, kind) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _claimed(conn, **kw) -> tuple[str, int]:
    tid = kb.create_task(conn, assignee="daedalus", **kw)
    kb.claim_task(conn, tid)
    return tid, kb.get_task(conn, tid).current_run_id


# ---------------------------------------------------------------------------
# configured_review_policy: value normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "always"), ("", "always"), ("always", "always"),
        ("milestone_only", "milestone_only"), ("Milestone-Only ", "milestone_only"),
        ("none", "none"), ("NONE", "none"),
        ("milestones", "always"),  # unknown ⇒ never silently skip review
        (7, "always"),
    ],
)
def test_configured_review_policy_normalises_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, raw, expected
) -> None:
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"review_policy": raw}})
    assert kb.configured_review_policy() == expected


# ---------------------------------------------------------------------------
# milestone_only
# ---------------------------------------------------------------------------


def test_milestone_only_slice_card_completes_in_place(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION GUARD: drop the policy branch in request_review and the card
    lands in ``review`` assigned to argus — this goes red."""
    _policy(monkeypatch, "milestone_only")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="slice: fix the thing")
        ok, reason = kb.request_review(
            conn, tid, summary="pushed branch; CI green https://ci/run/1",
            reviewer="argus", expected_run_id=run_id, with_reason=True,
        )
        assert (ok, reason) == (True, None)
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        # Never handed to the reviewer profile.
        assert task.assignee == "daedalus"
        assert _events(conn, tid, "review_requested") == []
        skipped = _events(conn, tid, "review_skipped")
        assert len(skipped) == 1
        assert skipped[0]["policy"] == "milestone_only"
        assert skipped[0]["reviewer_requested"] == "argus"
        assert "slice" in skipped[0]["why"]
        # The implementer's handoff survives as the completion evidence.
        run = kb.latest_run(conn, tid)
        assert run is not None and "CI green" in (run.summary or "")


def test_milestone_only_tagged_card_still_goes_to_review(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "milestone_only")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="[milestone] wave 2 lands")
        ok = kb.request_review(conn, tid, summary="done", reviewer="argus", expected_run_id=run_id)
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "argus"
        assert _events(conn, tid, "review_skipped") == []
        assert len(_events(conn, tid, "review_requested")) == 1


def test_milestone_only_tag_in_body_counts(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "milestone_only")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="untagged title", body="… [MILESTONE] …")
        assert kb.request_review(conn, tid, summary="done", reviewer="argus", expected_run_id=run_id)
        assert kb.get_task(conn, tid).status == "review"


def test_milestone_only_parent_card_still_goes_to_review(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card with children is a graph/parent card ⇒ milestone QA applies."""
    _policy(monkeypatch, "milestone_only")
    with kb.connect() as conn:
        parent, run_id = _claimed(conn, title="parent: wave")
        child = kb.create_task(conn, title="child slice", assignee="daedalus")
        kb.link_tasks(conn, parent, child)
        assert kb.request_review(conn, parent, summary="done", reviewer="argus", expected_run_id=run_id)
        assert kb.get_task(conn, parent).status == "review"


# ---------------------------------------------------------------------------
# none / always
# ---------------------------------------------------------------------------


def test_policy_none_skips_even_milestone_cards(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "none")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="[milestone] everything")
        assert kb.request_review(conn, tid, summary="done", reviewer="argus", expected_run_id=run_id)
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert _events(conn, tid, "review_skipped")[0]["why"] == "review_policy=none"


def test_policy_always_is_the_pre_policy_behaviour(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "always")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="slice")
        assert kb.request_review(conn, tid, summary="done", reviewer="argus", expected_run_id=run_id)
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "argus"
        assert _events(conn, tid, "review_skipped") == []


# ---------------------------------------------------------------------------
# Fences the skip path must keep
# ---------------------------------------------------------------------------


def test_skip_path_keeps_the_live_claim_fence(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unowned request on a live claim is refused, not completed."""
    _policy(monkeypatch, "none")
    with kb.connect() as conn:
        tid, _run_id = _claimed(conn, title="slice")
        ok, reason = kb.request_review(conn, tid, summary="done", with_reason=True)
        assert ok is False
        assert "live claim" in reason
        assert kb.get_task(conn, tid).status == "running"
        assert _events(conn, tid, "review_skipped") == []


def test_skip_path_force_overrides_the_fence(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "none")
    with kb.connect() as conn:
        tid, _run_id = _claimed(conn, title="slice")
        ok, reason = kb.request_review(conn, tid, summary="operator close", force=True, with_reason=True)
        assert (ok, reason) == (True, None)
        assert kb.get_task(conn, tid).status == "done"


def test_skip_path_wrong_run_id_is_refused(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, "none")
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="slice")
        ok, reason = kb.request_review(
            conn, tid, summary="done", expected_run_id=run_id + 1000, with_reason=True,
        )
        assert ok is False
        assert kb.get_task(conn, tid).status == "running"


def test_skip_path_falls_through_to_review_when_survivor_unavailable(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work the completion path cannot preserve must park in review, never vanish."""
    from hermes_cli.kanban_survivor import SurvivorUnavailable

    _policy(monkeypatch, "none")

    def _boom(*a, **k):
        raise SurvivorUnavailable("empty patch despite claimed code changes")

    monkeypatch.setattr(kb, "complete_task", _boom)
    with kb.connect() as conn:
        tid, run_id = _claimed(conn, title="slice")
        ok = kb.request_review(conn, tid, summary="done", reviewer="argus", expected_run_id=run_id)
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "argus"
        assert _events(conn, tid, "review_skipped") == []
        assert len(_events(conn, tid, "review_requested")) == 1
