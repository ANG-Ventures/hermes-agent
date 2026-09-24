"""Head-twin compaction and replay, separated for CI's per-file deadline."""
import pytest
from tests.agent.test_confab_notice_e2e import TestConfabNoticeEndToEnd as _Checks, notice_env


@pytest.mark.parametrize("stream", [False, True], ids=["non_stream", "stream"])
@pytest.mark.parametrize("engine", ["builtin", "lcm"])
@pytest.mark.parametrize("event_position", [1, 3, 7, 19])
def test_head_twin_order(notice_env, stream, engine, event_position, tmp_path):
    _Checks()._check_compaction_event_stays_between_original_neighbours_with_head_twins(
        notice_env, stream, engine, event_position, tmp_path
    )
