"""Boomerang v2 QA F-1 (t_230d37a3): a fork child built through the REAL entry point
(``delegate_task`` -> ``_run_single_child``) gets the parent's transcript and the fork frame
on its first request.

The INV-2 tests in test_delegate_tool_fork_mode.py call ``fork_run_inputs`` themselves, so they
passed while the live supervisor (``_run_single_child``) ran the child on the bare goal: 0 parent
turns, ~55k cold write, read_share 0.22 on every production fork of 2026-10-19. This test goes
through ``delegate_task`` with only the provider stream stubbed (Argus's inv1_probe shape).
"""
from __future__ import annotations

import copy
import json
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools import delegate_tool
from tools.delegate_tool import delegate_task

from tests.tools.test_delegate_tool_fork_mode import _SEAT, _PIN, _patches, _warm_parent, _wire_agent


class _Stop(Exception):
    status_code = 400


def _delegate(parent, **kw):
    """Run delegate_task for real; capture every CHILD provider request (the stream call raises)."""
    captured: list = []

    def _req_client(self, *a, **k):
        c = MagicMock()

        def _stream(**req):
            captured.append(copy.deepcopy(req))
            raise _Stop("captured first request")
        c.messages.stream.side_effect = _stream
        return c

    real_build = delegate_tool._build_child_agent

    def _build_and_wire(*a, **k):
        c = real_build(*a, **k)
        return _wire_agent(c, model=c.model) if getattr(c, "provider", None) == "claude-alr" else c

    with ExitStack() as st:
        _patches(st)
        st.enter_context(patch.object(AIAgent, "_create_request_anthropic_client", _req_client))
        st.enter_context(patch.object(AIAgent, "_persist_session", lambda self, *a, **k: None))
        st.enter_context(patch.object(AIAgent, "_save_trajectory", lambda self, *a, **k: None))
        st.enter_context(patch("tools.delegate_tool._load_config", return_value={}))
        st.enter_context(patch("tools.delegate_tool._build_child_agent", side_effect=_build_and_wire))
        delegate_task(goal="verify the canary", parent_agent=parent, background=False, **kw)
    return captured


def _text(req) -> str:
    return json.dumps(req.get("messages"), default=str)


@pytest.mark.slow_test(120, reason="two real AIAgent turns through delegate_task")
def test_real_entrypoint_fork_child_carries_parent_transcript_and_frame():
    parent = _warm_parent()
    parent.last_served_at = time.time() - 30
    reqs = _delegate(parent, inherit_context=True, inherit_mode="fork")
    assert reqs, "the child made no provider request"
    first = reqs[0]
    assert "Kestrel" in _text(first), "the parent's turn is missing from the fork child's prefix"
    assert first["messages"][-1]["role"] == "user"
    tail = json.dumps(first["messages"][-1], default=str)
    assert "BOOMERANG FORK" in tail and "verify the canary" in tail
    assert len(first["messages"]) > 1
    assert first["extra_headers"][_PIN] == _SEAT


@pytest.mark.slow_test(120, reason="two real AIAgent turns through delegate_task")
def test_real_entrypoint_fold_child_is_unchanged():
    """INV-1: a non-fork child still runs on the goal alone (no transcript, no frame)."""
    parent = _warm_parent()
    reqs = _delegate(parent, inherit_context=False)
    assert reqs
    assert "Kestrel" not in _text(reqs[0]) and "BOOMERANG FORK" not in _text(reqs[0])
    assert _PIN not in (reqs[0].get("extra_headers") or {})
