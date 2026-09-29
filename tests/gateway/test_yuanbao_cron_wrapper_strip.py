"""Yuanbao strips the scheduler's cron delivery wrapper (t_cb147820).

The wrapper is built by ``cron.scheduler._deliver_result``; this test builds it
through that real path so a wrapper-shape change cannot silently leave the
Yuanbao stripper matching nothing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import _deliver_result
from gateway.platforms.yuanbao import MessageSender


def _wrapped(content: str, success: bool = True) -> str:
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send_mock:
        _deliver_result(
            {"id": "j1", "name": "nightly", "deliver": "origin",
             "origin": {"platform": "telegram", "chat_id": "1"}},
            content,
            success=success,
        )
    return send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]


def test_strips_house_shape_success_footer():
    body = "🔴 **disk low**\n/ at 97%"
    assert MessageSender.strip_cron_wrapper(_wrapped(body)) == body


def test_strips_footer_keeps_failure_header():
    out = MessageSender.strip_cron_wrapper(_wrapped("boom", success=False))
    assert out == "⚠️ **Cronjob Failed: nightly**\nboom"


def test_leaves_unwrapped_content_alone():
    body = "plain text\n-# a producer's own subtext line"
    assert MessageSender.strip_cron_wrapper(body) == body
