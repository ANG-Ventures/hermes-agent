"""The review-assignee gate: a reviewer must be spawnable, or explicitly human.

INCIDENT 2026-09-21: ``request_review`` accepted ANY string as ``reviewer``
(the literal placeholder ``"reviewer"``), reassigned the card to it, and the
dispatcher then skipped it forever as a "non-spawnable assignee — terminal
lane, OK". Ten cards sat in ``review`` for hours, one for 2h+, and nothing
alerted because nothing was wrong from the dispatcher's point of view.

The invariant these tests pin down is enforced at the moment the reviewer is
SET (``resolve_reviewer`` inside ``request_review``), not at dispatch time:

* a reviewer that is neither an installed profile nor the ``human`` sentinel
  is REFUSED, with a message listing the spawnable reviewer profiles;
* ``reviewer=None`` resolves to config ``kanban.review_assignee`` — never to
  the implementer, never to a placeholder;
* reviewer == implementer is refused unless ``allow_same_actor``, which is
  recorded on the ``review_requested`` event;
* cards nothing will ever spawn are made LOUD via ``review_awaiting_human``,
  whose alert is one-shot per review episode and re-arms on claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fleet_profiles(monkeypatch: pytest.MonkeyPatch):
    """Pretend a real fleet is installed: argus/daedalus/momus exist."""
    installed = {"argus", "daedalus", "momus"}
    monkeypatch.setattr(
        kb, "spawnable_reviewer_profiles", lambda: sorted(installed)
    )
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: str(name).lower() in installed
    )
    return installed


def _review_event(conn, tid) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None, "no review_requested event"
    return json.loads(row["payload"])


# ---------------------------------------------------------------------------
# (1) Refusal of a non-profile reviewer — the actual incident
# ---------------------------------------------------------------------------


def test_placeholder_reviewer_is_refused_and_card_stays_running(
    kanban_home: Path, fleet_profiles
) -> None:
    """The literal 'reviewer' placeholder must not park the card.

    MUTATION GUARD: delete the ``profile_exists`` branch in
    ``resolve_reviewer`` and this test goes red — the transition succeeds and
    the card lands in review assigned to a string nothing can spawn.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        ok, reason = kb.request_review(
            conn, tid,
            summary="done",
            reviewer="reviewer",
            expected_run_id=run_id,
            with_reason=True,
        )

    assert ok is False
    assert "not an installed profile" in reason
    # The refusal names what a caller may actually use.
    assert "argus" in reason
    assert "human" in reason

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # Refused, so the card never entered the parked state.
    assert task.status == "running"
    assert task.assignee == "daedalus"


@pytest.mark.parametrize("bogus", ["reviewer", "qa-team", "someone", "TBD"])
def test_any_non_profile_string_is_refused(
    kanban_home: Path, fleet_profiles, bogus: str
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer=bogus,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            with_reason=True,
        )
    assert ok is False
    assert "not an installed profile" in reason


def test_real_profile_reviewer_is_accepted_and_reassigns(
    kanban_home: Path, fleet_profiles
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer="argus",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            with_reason=True,
        )
        assert (ok, reason) == (True, None)
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "argus"
        assert _review_event(conn, tid)["reviewer"] == "argus"


@pytest.mark.parametrize("sentinel", ["human", "HUMAN", "human:ace"])
def test_explicit_human_sentinel_is_allowed(
    kanban_home: Path, fleet_profiles, sentinel: str
) -> None:
    """A deliberate human lane is legal — it just must be EXPLICIT."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer=sentinel,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            with_reason=True,
        )
        assert (ok, reason) == (True, None)
        assert kb.get_task(conn, tid).assignee == sentinel.casefold()


# ---------------------------------------------------------------------------
# (2) Default resolves to kanban.review_assignee — never the implementer
# ---------------------------------------------------------------------------


def test_default_reviewer_resolves_to_configured_review_assignee(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "argus")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok = kb.request_review(
            conn, tid, summary="done",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        # NOT left with the implementer as their own reviewer.
        assert task.assignee == "argus"


def test_configured_default_equal_to_implementer_is_refused(
    kanban_home: Path, fleet_profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must never silently produce same-actor review."""
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "daedalus")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="done",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            with_reason=True,
        )
    assert ok is False
    assert "same-actor" in reason


# ---------------------------------------------------------------------------
# (3) Same-actor refusal + logged override
# ---------------------------------------------------------------------------


def test_same_actor_review_is_refused(kanban_home: Path, fleet_profiles) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer="daedalus",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            with_reason=True,
        )
        assert ok is False
        assert "same-actor review is refused" in reason
        assert kb.get_task(conn, tid).status == "running"


def test_same_actor_override_is_allowed_and_logged(
    kanban_home: Path, fleet_profiles
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        ok = kb.request_review(
            conn, tid, summary="done", reviewer="daedalus",
            allow_same_actor=True,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert ok is True
        assert kb.get_task(conn, tid).assignee == "daedalus"
        # The override is auditable, not silent.
        assert _review_event(conn, tid)["allow_same_actor"] is True


def test_allow_same_actor_is_not_stamped_on_ordinary_review(
    kanban_home: Path, fleet_profiles
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus")
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="done", reviewer="argus", allow_same_actor=True,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert "allow_same_actor" not in _review_event(conn, tid)


# ---------------------------------------------------------------------------
# (4) The parked state is LOUD
# ---------------------------------------------------------------------------


def test_review_awaiting_human_flags_non_spawnable_assignee(
    kanban_home: Path, fleet_profiles
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parked", assignee="daedalus")
        # Simulate the incident shape directly (the gate now prevents
        # reaching it through request_review).
        conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer' WHERE id=?",
            (tid,),
        )
        conn.commit()

        entries = kb.review_awaiting_human(conn)
        assert [e["task_id"] for e in entries] == [tid]
        assert entries[0]["reason"] == "non_spawnable"

        line = kb.format_review_awaiting_human(entries)
        assert "awaiting HUMAN" in line
        assert tid in line


def test_spawnable_review_card_is_not_flagged_until_stale(
    kanban_home: Path, fleet_profiles
) -> None:
    import time

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="daedalus")
        conn.execute(
            "UPDATE tasks SET status='review', assignee='argus', started_at=? "
            "WHERE id=?",
            (int(time.time()), tid),
        )
        conn.commit()
        assert kb.review_awaiting_human(conn, stale_minutes=30) == []

        # Same card, aged past the threshold.
        conn.execute(
            "UPDATE tasks SET started_at=? WHERE id=?",
            (int(time.time()) - 60 * 45, tid),
        )
        conn.commit()
        entries = kb.review_awaiting_human(conn, stale_minutes=30)
        assert [e["reason"] for e in entries] == ["stale"]
        assert entries[0]["age_minutes"] >= 45


def test_format_review_awaiting_human_is_silent_when_clean(
    kanban_home: Path,
) -> None:
    assert kb.format_review_awaiting_human([]) is None


def test_stale_alert_fires_once_and_rearms_on_new_review_episode(
    kanban_home: Path, fleet_profiles
) -> None:
    """One alert per review episode; a new episode re-arms it."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parked", assignee="daedalus")
        conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer' WHERE id=?",
            (tid,),
        )
        conn.commit()

        entries = kb.review_awaiting_human(conn)
        # First crossing fires.
        assert [e["task_id"] for e in kb.arm_review_stale_alerts(conn, entries)] == [tid]
        # Second tick does NOT re-fire.
        assert kb.arm_review_stale_alerts(conn, kb.review_awaiting_human(conn)) == []
        # ...and a third tick still doesn't (the marker is durable).
        assert kb.arm_review_stale_alerts(conn, kb.review_awaiting_human(conn)) == []

        # A NEW review episode (the card went back to a worker and returned)
        # re-arms the alert: request_review leaves a newer review_requested
        # event than the alert marker.
        conn.execute(
            "UPDATE tasks SET status='running', assignee='daedalus' WHERE id=?",
            (tid,),
        )
        conn.commit()
        ok = kb.request_review(conn, tid, summary="round 2", reviewer="argus")
        assert ok is True
        # Age it past the threshold so it qualifies again.
        conn.execute(
            "UPDATE tasks SET started_at=? WHERE id=?",
            (int(__import__("time").time()) - 60 * 90, tid),
        )
        conn.commit()

        refired = kb.arm_review_stale_alerts(conn, kb.review_awaiting_human(conn))
        assert [e["task_id"] for e in refired] == [tid]


def test_claimed_review_card_is_never_flagged(
    kanban_home: Path, fleet_profiles
) -> None:
    """A card actively under review is not 'awaiting a human'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="under review", assignee="daedalus")
        conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer', "
            "claim_lock='host:123' WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kb.review_awaiting_human(conn) == []


# ---------------------------------------------------------------------------
# (5) Degraded environments must not be bricked by the gate
# ---------------------------------------------------------------------------


def test_gate_fails_open_when_no_profiles_are_installed(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare checkout has no profiles; refusing everything would brick it."""
    monkeypatch.setattr(kb, "spawnable_reviewer_profiles", lambda: [])
    canonical, error = kb.resolve_reviewer("anything", "worker")
    assert error is None
    assert canonical == "anything"


def test_is_human_reviewer_shapes() -> None:
    assert kb.is_human_reviewer("human")
    assert kb.is_human_reviewer("  Human ")
    assert kb.is_human_reviewer("human:ace")
    assert not kb.is_human_reviewer("humanoid")
    assert not kb.is_human_reviewer("argus")
    assert not kb.is_human_reviewer(None)


# ---------------------------------------------------------------------------
# (6) CLI surfaces: the word "OK" stops lying, and the flag is wired
# ---------------------------------------------------------------------------


def _parked_review_card(age_minutes: int = 480) -> str:
    """Create a review card that has sat unclaimed past the stale threshold."""
    import time

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parked", assignee="daedalus")
        conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer', "
            "claim_lock=NULL, started_at=? WHERE id=?",
            (int(time.time()) - 60 * age_minutes, tid),
        )
        conn.commit()
    return tid


def _event_counts() -> dict:
    with kb.connect() as conn:
        return dict(
            conn.execute(
                "SELECT kind, COUNT(*) FROM task_events GROUP BY kind"
            ).fetchall()
        )


def _dispatch_args(**over):
    """A minimal argparse namespace accepted by ``_cmd_dispatch``."""
    import argparse

    from hermes_cli import kanban as kc

    root = argparse.ArgumentParser()
    kc.build_parser(root.add_subparsers(dest="cmd"))
    argv = ["kanban", "dispatch"]
    if over.pop("dry_run", False):
        argv.append("--dry-run")
    if over.pop("json", False):
        argv.append("--json")
    ns = root.parse_args(argv)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_dry_run_prints_the_detector_but_writes_NOTHING(
    kanban_home: Path, fleet_profiles, monkeypatch, capsys
) -> None:
    """``--dry-run`` is the documented SAFE probe — it must not mutate.

    Regression: the detector called ``arm_review_stale_alerts`` on every
    dispatch path including ``--dry-run``, so a read-only probe appended a
    durable ``review_stale_alerted`` event (and attempted a real Discord
    send). That is the same mechanism that wrote 17 events onto 16 production
    cards during this card's own build.
    """
    from hermes_cli import kanban as kc

    tid = _parked_review_card()
    sends: list = []
    monkeypatch.setattr(kc, "_send_review_stale_alert", lambda e: sends.append(e))

    before = _event_counts()
    rc = kc._cmd_dispatch(_dispatch_args(dry_run=True))
    out = capsys.readouterr().out
    after = _event_counts()

    assert rc == 0
    # The detector still SPEAKS — that is the whole point of the line.
    assert "awaiting HUMAN" in out and tid in out
    # ...but it wrote nothing and sent nothing.
    assert after == before, f"dry-run mutated task_events: {before} -> {after}"
    assert after.get("review_stale_alerted", 0) == 0
    assert sends == []


def test_real_tick_does_arm_and_send(
    kanban_home: Path, fleet_profiles, monkeypatch, capsys
) -> None:
    """The dry-run guard must not disarm the real dispatcher tick."""
    from hermes_cli import kanban as kc

    tid = _parked_review_card()
    sends: list = []
    monkeypatch.setattr(kc, "_send_review_stale_alert", lambda e: sends.append(e))

    rc = kc._cmd_dispatch(_dispatch_args(dry_run=False))
    capsys.readouterr()

    assert rc == 0
    assert _event_counts().get("review_stale_alerted", 0) == 1
    assert [e["task_id"] for e in sends[0]] == [tid]


def test_dispatch_json_carries_the_detector(
    kanban_home: Path, fleet_profiles, monkeypatch, capsys
) -> None:
    """A JSON consumer must not be blinder than the text tick."""
    from hermes_cli import kanban as kc

    tid = _parked_review_card()
    monkeypatch.setattr(kc, "_send_review_stale_alert", lambda e: None)

    rc = kc._cmd_dispatch(_dispatch_args(json=True, dry_run=True))
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert [e["task_id"] for e in payload["review_awaiting_human"]] == [tid]
    # --json --dry-run obeys the same read-only rule as the text path.
    assert _event_counts().get("review_stale_alerted", 0) == 0


def test_notify_script_is_resolved_under_HERMES_HOME(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandboxed/test/CI home must not reach the LIVE notify.py.

    Regression: the path was hardcoded to ``~/.hermes/scripts/notify.py``, so
    a hermetic run fired a real Discord alert to the live channel about cards
    that do not exist on the real board.
    """
    from hermes_cli import kanban as kc

    sandbox = tmp_path / "sandbox_home"
    (sandbox / "scripts").mkdir(parents=True)
    (sandbox / "scripts" / "notify.py").write_text("# sandbox\n")
    monkeypatch.setenv("HERMES_HOME", str(sandbox))

    resolved = kc._notify_script_path()
    assert resolved is not None
    assert Path(resolved).is_relative_to(sandbox), resolved

    # An empty sandbox resolves to nothing rather than falling back to live.
    empty = tmp_path / "empty_home"
    empty.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(empty))
    assert kc._notify_script_path() is None


def test_dry_run_wording_no_longer_calls_a_parked_card_OK(
    kanban_home: Path, fleet_profiles, monkeypatch, capsys
) -> None:
    """'terminal lane, OK' hid the incident; it must name the human need."""
    from hermes_cli import kanban as kc

    _parked_review_card()
    monkeypatch.setattr(kc, "_send_review_stale_alert", lambda e: None)

    kc._cmd_dispatch(_dispatch_args(dry_run=True))
    out = capsys.readouterr().out

    assert "terminal lane, OK" not in out
    assert "HUMAN review required" in out


def test_cli_exposes_allow_same_actor_and_reviewer_flags() -> None:
    import argparse

    from hermes_cli import kanban as kc

    root = argparse.ArgumentParser()
    kc.build_parser(root.add_subparsers(dest="cmd"))
    ns = root.parse_args(
        ["kanban", "request-review", "t_x", "--reviewer", "argus",
         "--allow-same-actor"]
    )
    assert ns.reviewer == "argus"
    assert ns.allow_same_actor is True

    default_ns = root.parse_args(["kanban", "request-review", "t_x"])
    assert default_ns.reviewer is None
    assert default_ns.allow_same_actor is False


def test_request_review_tool_schema_names_the_legal_reviewer_forms() -> None:
    """The model-facing description must not invite a placeholder."""
    from tools.kanban_tools import KANBAN_REQUEST_REVIEW_SCHEMA as schema

    desc = schema["parameters"]["properties"]["reviewer"]["description"]
    assert "argus" in desc
    assert "human" in desc
    assert "kanban.review_assignee" in desc
