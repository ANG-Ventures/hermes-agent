"""Bounded E2E matrix shard: rewritten tool arguments, LCM compressor."""
import pytest
from tests.agent.test_confab_notice_e2e import TestConfabNoticeEndToEnd as _MatrixChecks, notice_env


@pytest.mark.parametrize("event_position", range(1, 32, 2))
def test_notice_timeline(notice_env, event_position, tmp_path):
    _MatrixChecks()._check_rewritten_kept_rows_preserve_notice_timeline(
        notice_env, False, "lcm", "bigargs", event_position, tmp_path
    )
