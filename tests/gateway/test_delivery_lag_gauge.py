"""Reply delivery-lag gauge (t_aeae3fe7): ``response ready`` -> ``Sending response``.

On 2026-10-24 Discord replies reached users 1-16 min after the agent finished; nothing in the log
said so. ``send_final_ledgered`` now reports ``delivery_lag_s`` from the stamp the turn runner sets
at ``response ready``, and ``scripts/delivery-lag-p99.py`` turns a gateway log into percentiles.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult

_ROOT = Path(__file__).resolve().parents[2]


class _Adapter(BasePlatformAdapter):
    connect = disconnect = get_chat_info = send = None  # abstract slots; never called here


def _adapter():
    adapter = object.__new__(_Adapter)
    adapter._final_delivery_adapter = lambda source: SimpleNamespace(
        name="discord", _send_with_retry=AsyncMock(return_value=SendResult(success=True, message_id="1")))
    adapter._record_delivery_obligation = AsyncMock(return_value=None)
    adapter._release_turn_marker = AsyncMock()
    return adapter


def _event(**extra):
    return SimpleNamespace(source=SimpleNamespace(chat_id="42"), **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [True, False])
async def test_sending_response_line_carries_delivery_lag(caplog, monkeypatch, stamped):
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "stop_reply_clock", lambda *a, **k: None)
    event = _event(_response_ready_at=time.monotonic() - 3.25) if stamped else _event()
    with caplog.at_level(logging.INFO, logger=base.logger.name):
        await _adapter().send_final_ledgered(event, "sk", "hello", {}, reply_to=None)
    lines = [r.getMessage() for r in caplog.records if "Sending response" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("[discord] Sending response (5 chars) to 42")
    if stamped:
        assert "delivery_lag_s=3.2" in lines[0] or "delivery_lag_s=3.3" in lines[0], lines[0]
    else:
        assert "delivery_lag_s" not in lines[0]


def _script():
    spec = importlib.util.spec_from_file_location("delivery_lag_p99", _ROOT / "scripts" / "delivery-lag-p99.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_FIXTURE = """\
2026-10-24 14:00:00,000 INFO gateway.run: response ready: platform=discord chat=111 session=a time=3.0s api_calls=1 response=10 chars
2026-10-24 14:00:02,000 INFO gateway.platforms.base: [discord] Sending response (10 chars) to 111
2026-10-24 14:01:00,000 INFO gateway.run: response ready: platform=discord chat=222 session=b time=3.0s api_calls=1 response=10 chars
2026-10-24 14:02:40,000 INFO gateway.platforms.base: [discord] Sending response (10 chars) to 222
2026-10-24 14:03:00,000 INFO gateway.run: response ready: platform=telegram chat=333 session=c time=3.0s api_calls=1 response=10 chars
2026-10-24 14:03:50,000 INFO gateway.platforms.base: [telegram] Sending response (10 chars) to 333
2026-10-24 14:10:00,000 INFO gateway.platforms.base: [discord] Sending response (10 chars) to 111 delivery_lag_s=4.0
2026-10-24 14:20:00,000 INFO gateway.platforms.base: [discord] Sending response (10 chars) to 444 delivery_lag_s=1.0
"""


def test_script_percentiles_and_exit_codes(tmp_path, capsys):
    mod = _script()
    log = tmp_path / "gateway.log"
    log.write_text(_FIXTURE, encoding="utf-8")
    since = str(mod._epoch("2026-10-24 13:00:00"))
    args = ["--log", str(log), "--since", since, "--window", "7200"]
    assert mod.main(args) == 0
    import json
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    total = out[-1]
    # discord only: lags 2.0 (paired), 100.0 (paired), 4.0 and 1.0 (gauge); telegram's 50 s excluded
    assert (total["n"], total["p50_s"], total["p90_s"], total["p99_s"], total["max_s"]) == (4, 2.0, 100.0, 100.0, 100.0)
    assert out[0]["hour"] == "2026-10-24 14:00" and out[0]["n"] == 4
    assert mod.main(args + ["--max-p99-s", "5"]) == 1
    assert mod.main(args + ["--max-p99-s", "500"]) == 0
    assert mod.main(args + ["--platform", "all", "--max-p99-s", "500"]) == 0
    capsys.readouterr()
    assert mod.main(["--log", str(log), "--since", "0", "--window", "10"]) == 2
    assert mod.main(["--log", str(tmp_path / "missing.log")]) == 2


def test_threshold_compares_the_unrounded_p99(tmp_path, capsys):
    """Prism a23d23dcf728: 5.04 s rounds to 5.0 for display but must still FAIL a 5.01 s limit."""
    mod = _script()
    log = tmp_path / "gateway.log"
    log.write_text(
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=111 session=a time=1.0s api_calls=1 response=1 chars\n"
        "2026-10-24 14:00:05,040 INFO x: [discord] Sending response (10 chars) to 111\n", encoding="utf-8")
    assert mod.main(["--log", str(log), "--since", "0", "--window", "2000000000", "--max-p99-s", "5.01"]) == 1
    assert mod.main(["--log", str(log), "--since", "0", "--window", "2000000000", "--max-p99-s", "5.05"]) == 0
