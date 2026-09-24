"""Parallel-tool compaction and replay, separated for CI's per-file deadline."""
import pytest
from tests.agent.test_confab_notice_e2e import TestConfabNoticeEndToEnd as _Checks, notice_env


@pytest.mark.parametrize("stream", [False, True], ids=["non_stream", "stream"])
@pytest.mark.parametrize("engine", ["builtin", "lcm"])
@pytest.mark.parametrize("event_position", [3, 38, 47])
@pytest.mark.parametrize("fallback", [False, True])
def test_parallel_tool_results(notice_env, stream, engine, event_position, fallback, tmp_path):
    _Checks()._check_compaction_event_does_not_split_parallel_tool_results(
        notice_env, stream, engine, event_position, fallback, tmp_path
    )
