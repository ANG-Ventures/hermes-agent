"""Priority door adapter contracts (P4); no live scheduler state."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_asap_is_explicit_persisted_and_never_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # the door state root resolves PLACEMENT_HOME first (same pin as test_kanban_priority_door.board; reader rv70e)
    monkeypatch.setenv("ALLOCATION_HOME", str(tmp_path / "fleet-root"))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    # the operator is PROVEN (operator token), never a caller-supplied string (round 5, Prism b37716b1f3e9)
    (kb.kanban_home() / "kanban").mkdir(parents=True, exist_ok=True)
    kb.operator_token_path().write_text("op\n", encoding="utf-8")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "op")
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    from hermes_cli.kanban_priority import set_asap

    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee=None)
        assert set_asap(conn, parent, True)
        child = kb.create_task(conn, title="child", assignee=None, parents=[parent])
        assert kb.get_task(conn, parent).asap is True
        assert kb.get_task(conn, child).asap is False
        monkeypatch.setenv("HERMES_KANBAN_TASK", child)
        with pytest.raises(PermissionError, match="operator"):
            set_asap(conn, child, True)
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        assert set_asap(conn, parent, False)
        assert kb.get_task(conn, parent).asap is False


def test_waiting_uses_structural_capacity_not_pressure_band(monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.kanban_priority import waiting_by_host

    plan = SimpleNamespace(
        hosts={"ai": SimpleNamespace(slots=3, enabled=True, state="active", profiles=("coder",))},
        slots={"ai": 0}, taken={}, disabled=(),
        detail={"ai": {"running": 1, "band": "hot"}},
        board_refusal=lambda host, board: None,
    )
    cards = [
        {"id": "local", "assignee": "coder", "asap": False},
        {"id": "portable", "assignee": "coder", "asap": False},
        {"id": "urgent", "assignee": "coder", "asap": True},
    ]
    routes = {"local": ("local", "worktree"), "portable": ("portable", None),
              "urgent": ("pin", "ai")}
    waiting = waiting_by_host(cards, plan, routes=routes, local_host="studio", board="default")
    # one host per unit: the door refuses a live id on a second host (hermes-home #9223), so local-first
    assert [r["id"] for r in waiting["studio"]] == ["local", "portable"]
    assert [r["id"] for r in waiting["ai"]] == ["urgent"]
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch._portable_only", lambda *args: (("ai",), None))
    w = waiting_by_host(cards, plan, routes=routes, local_host="studio", conn=object())
    assert [r["id"] for r in w["ai"]] == ["urgent", "portable"] and [r["id"] for r in w["studio"]] == ["local"]
    plan.detail["ai"]["running"] = 2                                   # one structural slot left: ASAP takes it
    w = waiting_by_host(cards, plan, routes=routes, local_host="studio", conn=object())
    assert [r["id"] for r in w["ai"]] == ["urgent"] and all(r["id"] != "portable" for r in w["studio"])
    plan.detail["ai"]["band"] = "ok"
    assert waiting_by_host(cards, plan, routes=routes, local_host="studio", conn=object()) == w   # band is not read
    monkeypatch.undo()
    plan.detail["ai"]["running"] = 3
    assert waiting_by_host(cards, plan, routes=routes, local_host="studio")["ai"] == []
    plan.detail["ai"]["running"] = 0
    plan.detail["ai"]["reachable"] = False
    assert waiting_by_host(cards, plan, routes=routes, local_host="studio")["ai"] == []
    plan.detail["ai"]["reachable"] = True
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch._portable_only",
                        lambda *args: (("elsewhere",), None))
    assert [r["id"] for r in waiting_by_host(
        cards, plan, routes=routes, local_host="studio", conn=object())["ai"]] == ["urgent"]


def test_binding_verdict_has_no_fictional_cpu_deficit():
    from hermes_cli.kanban_priority import door_verdict

    assert door_verdict(False, "psi_cpu_some60", {"cpu": .25}, 32) == {
        "admittable": False, "binding": "psi_cpu_some60", "deficit_cpu": 0.0,
    }
    assert door_verdict(False, "cpu", {"cpu": -.125}, 32)["deficit_cpu"] == 4
    assert door_verdict(False, "unknown", {}, 32)["deficit_cpu"] == 0
    # Adapter carries CPU/PSI/memory bindings without converting PSI into cores.
    from types import SimpleNamespace
    from hermes_cli.kanban_priority import verdicts_for_plan
    from unittest.mock import patch
    plan = SimpleNamespace(hosts={"ai": object()}, signal=SimpleNamespace(placement_table=object()),
                           detail={"ai": {"ncpu": 32}})
    for reason, binding, deficit in (("saturated:cpu -0.12", "cpu", 3.84),
                                      ("saturated:psi_cpu_some60 -0.2", "psi_cpu_some60", 0),
                                      ("saturated:mem -0.1", "mem", 0),
                                      ("closed:stale", "unknown", 0)):
        with patch("hermes_cli.kanban_headroom._class_verdict", return_value=(False, reason)):
            verdict = verdicts_for_plan(plan)["ai"]
        assert verdict["binding"] == binding
        assert verdict["deficit_cpu"] == deficit


def test_waiting_treats_the_prose_preference_as_soft(monkeypatch):
    """Since t_efd1497a the prose host preference is soft: a card whose preferred hosts are full waits on any
    other eligible host, not only the preferred ones; with room, the preferred host is chosen first (t_8c580b20,
    Prism 77c519869eff). A hard set under the preference still bounds it.
    Mutation: read _ProsePreferred as the hard set in waiting_by_host -> the card waits nowhere (RED)."""
    from types import SimpleNamespace
    from hermes_cli.kanban_db_dispatch import _ProsePreferred
    from hermes_cli.kanban_priority import waiting_by_host

    def host():
        return SimpleNamespace(slots=1, enabled=True, state="active", profiles=("coder",))

    plan = SimpleNamespace(hosts={"ai": host(), "media": host(), "nas": host()}, slots={}, taken={}, disabled=(),
                           detail={"ai": {"running": 1}, "media": {"running": 0}, "nas": {"running": 0}},
                           board_refusal=lambda h, b: None)
    cards = [{"id": "drop", "assignee": "coder", "asap": False}]
    routes = {"drop": ("portable", None)}

    def wait(pref, hard):
        monkeypatch.setattr("hermes_cli.kanban_db_dispatch._portable_only",
                            lambda *a: (_ProsePreferred(pref, hard), None))
        monkeypatch.setattr("hermes_cli.kanban_db_dispatch._portable_goes_remote", lambda *a, **k: True)
        w = waiting_by_host(cards, plan, routes=routes, local_host="studio", conn=object())
        return [h for h, rows in w.items() if rows]

    assert wait(("ai",), None) == ["media"]                  # preferred host full: any eligible host
    assert wait(("nas",), None) == ["nas"]                   # preferred host has room: it goes first
    assert wait(("ai",), ("ai", "nas")) == ["nas"]           # the hard set still bounds the fallback
    assert wait(("ai",), ("ai",)) == []                      # nothing eligible with room: no host
