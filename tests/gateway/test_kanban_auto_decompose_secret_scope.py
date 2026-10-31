"""Auto-decompose tick under multiplex.

Regression for #107955 / #57837: the tick runs off-turn in a fresh Context, so
``get_secret`` fails closed unless the tick installs the launch profile's scope.
Drives the gateway's real ``_auto_decompose_tick`` closure (the only copy since S7 D4
deleted the dead ``_KanbanDispatcher.auto_decompose_tick``).
"""

from __future__ import annotations

import pytest

from agent import secret_scope as ss

from tests.gateway.test_kanban_auto_decompose_board_scope import _one_decompose_iteration, loop  # noqa: F401


@pytest.mark.asyncio
async def test_auto_decompose_tick_reads_launch_profile_secrets_under_multiplex(loop):  # noqa: F811
    (loop.home / ".env").write_text("ANTHROPIC_API_KEY=launch-profile-key\n", encoding="utf-8")
    ss.set_multiplex_active(True)
    try:
        await _one_decompose_iteration(loop)
    finally:
        ss.set_multiplex_active(False)

    assert loop.seen["secret"] == "launch-profile-key"
    assert ss.current_secret_scope() is None
