"""kanban_notify_suppress: the redundancy predicate against the real 2026-10-11 rows (t_4c7da648).

Fixture: ``fixtures/kanban_notify_suppress_20261011.json`` -- task_events exported
from the default board on 2026-10-11. The five ``completed`` rows woke the home chat
(#dgx-spark) as echoes of the operator's own closes; the ``review_requested`` /
``block_loop_detected`` rows are real worker handbacks that must keep waking.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_notify_suppress as ns

FIXTURE = Path(__file__).parent / "fixtures" / "kanban_notify_suppress_20261011.json"
ECHO_IDS = {622950, 623122, 623162, 623690, 625564}
HOME_SID = "20261008_184838_dbe0955f"


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _event(row: dict) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _approve_for(fx, task_id: str, before_ts: int):
    """Mirror latest_approve() over the fixture's review_coverage comments."""
    for c in fx["review_coverage_comments"].get(task_id) or []:
        if int(c["created_at"]) > before_ts:
            continue
        rec = ns.parse_approve(c["body"])
        if rec is not None:
            return {"by": rec.get("reviewer") or c["author"], "ts": int(c["created_at"])}
    return None


def _card(fx, row: dict, operator: str = "default") -> dict:
    return {"session_id": fx["home_session_id"], "operator_profile": operator,
            "latest_approve": _approve_for(fx, row["task_id"], int(row["created_at"]))}


def test_fixture_rows_split_exactly_as_observed(fx):
    cfg = ns.parse_config({})
    verdicts = {row["id"]: ns.is_redundant(_event(row), _card(fx, row), None, cfg) for row in fx["events"]}
    suppressed = {eid for eid, (red, _r) in verdicts.items() if red}
    assert suppressed == ECHO_IDS, verdicts
    # the closer-written completes (no actor stamp) are caught via the operator's APPROVE
    assert verdicts[623162][1] == "closer-after-operator-approve"
    assert verdicts[623690][1] == "closer-after-operator-approve"
    assert verdicts[622950][1] == "self-session"
    # the handbacks are protected before any actor logic runs
    for eid in (622453, 623034, 623253, 624370, 622460):
        assert verdicts[eid] == (False, "protected-kind")


def test_worker_actor_never_suppresses(fx):
    cfg = ns.parse_config({})
    row = dict(next(r for r in fx["events"] if r["id"] == 622950))
    row.update(actor_profile="daedalus-opus", actor_session_id="20261011_150000_worker")
    assert ns.is_redundant(_event(row), _card(fx, row), None, cfg) == (False, "other-actor")


def test_unknown_operator_fails_open(fx):
    cfg = ns.parse_config({})
    row = dict(next(r for r in fx["events"] if r["id"] == 623162))   # closer-written, no actor
    card = {"session_id": "", "operator_profile": "", "latest_approve": _approve_for(fx, row["task_id"], 10**10)}
    assert ns.is_redundant(_event(row), card, None, cfg) == (False, "unknown-operator")
    # the operator's own session id still counts even with no profile resolved
    row2 = dict(next(r for r in fx["events"] if r["id"] == 622950))
    assert ns.is_redundant(_event(row2), {"session_id": HOME_SID, "operator_profile": ""}, None, cfg)[0] is True


@pytest.mark.parametrize("age_min, expect", [(29, True), (31, False)])
def test_closer_window_boundary(fx, age_min, expect):
    cfg = ns.parse_config({"notify_suppress": {"self_window_min": 30}})
    row = dict(next(r for r in fx["events"] if r["id"] == 623690))
    card = {"session_id": HOME_SID, "operator_profile": "default",
            "latest_approve": {"by": "apollo", "ts": int(row["created_at"]) - age_min * 60}}
    red, reason = ns.is_redundant(_event(row), card, None, cfg)
    assert red is expect, reason


def test_closer_without_operator_approve_is_not_redundant(fx):
    cfg = ns.parse_config({})
    row = dict(next(r for r in fx["events"] if r["id"] == 623690))
    card = {"session_id": HOME_SID, "operator_profile": "default", "latest_approve": None}
    assert ns.is_redundant(_event(row), card, None, cfg) == (False, "closer-without-operator-approve")
    # an APPROVE by a different operator does not cover this operator's chat
    card["latest_approve"] = {"by": "aegis", "ts": int(row["created_at"]) - 60}
    assert ns.is_redundant(_event(row), card, None, cfg)[0] is False


def test_disabled_suppresses_nothing(fx):
    cfg = ns.parse_config({"notify_suppress": {"enabled": False}})
    for row in fx["events"]:
        assert ns.is_redundant(_event(row), _card(fx, row), None, cfg) == (False, "disabled")


def test_protected_kinds_beat_config(fx):
    cfg = ns.parse_config({"notify_suppress": {"kinds": ["completed", "review_requested", "blocked"]}})
    row = next(r for r in fx["events"] if r["kind"] == "review_requested")
    ev = _event(dict(row, actor_profile="default", actor_session_id=HOME_SID))
    assert ns.is_redundant(ev, _card(fx, row), None, cfg) == (False, "protected-kind")
    # owner-wake last-blocker-done trigger is protected even on a self-made completed
    row = next(r for r in fx["events"] if r["id"] == 622950)
    assert ns.is_redundant(_event(row), _card(fx, row), None, cfg, trigger="last_blocker_done") == (False, "protected-kind")


def test_operator_label_aliases():
    assert ns.same_operator("default", "apollo")
    assert ns.same_operator("human:apollo", "default")
    assert not ns.same_operator("aegis", "default")
    assert not ns.same_operator("", "default")


def test_parse_config_defaults_and_coercion():
    cfg = ns.parse_config({})
    assert cfg.enabled and cfg.kinds == {"completed", "archived"} and cfg.self_window_s == 1800
    cfg = ns.parse_config({"notify_suppress": {"enabled": "off", "kinds": "completed", "self_window_min": "5",
                                               "closers": ["land-autopilot"]}})
    assert not cfg.enabled and cfg.kinds == {"completed"} and cfg.self_window_s == 300
    assert cfg.closers == ("land-autopilot",)
    assert ns.parse_config({"notify_suppress": "garbage"}) == ns.parse_config({})


def test_latest_approve_reads_newest_approve_before_event(fx, tmp_path):
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    rows = fx["review_coverage_comments"]["t_dbb4cdd9"]
    for c in sorted(rows, key=lambda c: c["created_at"]):
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     ("t_dbb4cdd9", c["author"], c["body"], c["created_at"]))
    conn.commit()
    got = ns.latest_approve(conn, "t_dbb4cdd9")
    assert got == {"by": "apollo", "ts": 1791762296}
    # before the APPROVE only CHANGES_REQUESTED records exist -> None
    assert ns.latest_approve(conn, "t_dbb4cdd9", before_ts=1791762000) is None
