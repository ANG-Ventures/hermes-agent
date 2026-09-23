"""Dispatch admission pauses on host load without stopping board maintenance."""

import sys

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _DispatcherSettings


def test_load_gate_hysteresis_across_ticks(monkeypatch):
    from gateway import kanban_watchers_dispatcher as mod
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 32)
    sample = {"load": 26.0}
    monkeypatch.setattr(mod.os, "getloadavg", lambda: (sample["load"], 0, 0))
    gate = _KanbanDispatcher(None, _DispatcherSettings(60, 24, None, 2, 0, True, None, 24))
    assert gate.load_fence_active()
    sample["load"] = 25.0
    assert gate.load_fence_active()  # between 75% and 80%: still latched
    sample["load"] = 23.9
    assert not gate.load_fence_active()
    sample["load"] = 25.0
    assert not gate.load_fence_active()


def test_load_gate_applies_to_all_boards_before_dispatch(monkeypatch):
    from gateway import kanban_watchers_dispatcher as mod
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(mod.os, "getloadavg", lambda: (40.0, 0, 0))
    calls = []
    monkeypatch.setattr(mod, "_kbc", lambda: type("Connect", (), {"connect": staticmethod(lambda board: DummyConnection())}))
    monkeypatch.setattr(mod, "_kbd", lambda: type("Dispatch", (), {"dispatch_once": staticmethod(lambda conn, **kwargs: calls.append(kwargs))}))
    gate = _KanbanDispatcher(type("Board", (), {"kanban_db_path": staticmethod(lambda slug: __import__("pathlib").Path("/nonexistent"))}), _DispatcherSettings(60, 24, None, 2, 0, True, None, 24))
    gate.tick_once_for_board("one")
    gate.tick_once_for_board("two")
    assert [call["max_spawn"] for call in calls] == [0, 0]


class DummyConnection:
    def close(self):
        pass
