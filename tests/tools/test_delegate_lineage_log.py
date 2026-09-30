"""delegate_task emits a parseable lineage line once the child session exists.

Log consumers (scripts/web-call-tripwire.py LINEAGE_RE) rebuild delegation
trees from agent.log alone; field order and key=value spelling are the contract.
"""
from __future__ import annotations

import logging
import re

import pytest

LINEAGE_RE = re.compile(
    r"delegate_task child id=(\S+) session=(\S+) parent_session=(\S+) depth=(\d+)$"
)


def _build(monkeypatch, *, parent_session: str, child_session: str, parent_depth: int):
    import run_agent
    from tools import delegate_tool

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.valid_tool_names = {"terminal"}
            self.session_id = child_session

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})

    class Parent:
        enabled_toolsets = ["terminal"]
        valid_tool_names = {"terminal"}
        model = "test-model"
        provider = "test-provider"
        base_url = "http://example.invalid"
        api_mode = "chat_completions"
        platform = "cli"
        session_id = parent_session
        _delegate_depth = parent_depth

    return delegate_tool._build_child_agent(
        task_index=0,
        goal="lineage",
        context=None,
        toolsets=None,
        model=None,
        max_iterations=3,
        task_count=1,
        parent_agent=Parent(),
    )


def _lineage(caplog):
    return [
        LINEAGE_RE.search(r.getMessage())
        for r in caplog.records
        if r.name == "tools.delegate_tool" and LINEAGE_RE.search(r.getMessage())
    ]


@pytest.mark.parametrize("parent_depth", [0, 1])
def test_child_build_logs_one_lineage_line(monkeypatch, caplog, parent_depth):
    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    child = _build(
        monkeypatch,
        parent_session="20260930_parent_abc",
        child_session="20260930_child_xyz",
        parent_depth=parent_depth,
    )

    matches = _lineage(caplog)
    assert len(matches) == 1
    sub_id, child_sid, parent_sid, depth = matches[0].groups()
    assert sub_id == child._subagent_id
    assert child_sid == "20260930_child_xyz"
    assert parent_sid == "20260930_parent_abc"
    assert int(depth) == parent_depth + 1 == child._delegate_depth


def test_lineage_values_never_contain_spaces(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    _build(monkeypatch, parent_session="", child_session="a b", parent_depth=0)

    (m,) = _lineage(caplog)
    _sub_id, child_sid, parent_sid, _depth = m.groups()
    assert child_sid == "a_b"
    assert parent_sid == "-"
