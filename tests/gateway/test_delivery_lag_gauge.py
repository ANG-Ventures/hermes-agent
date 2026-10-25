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


def _run(mod, tmp_path, text, *extra):
    import json
    log = tmp_path / "gateway.log"
    log.write_text(text, encoding="utf-8")
    rc = mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", *extra])
    return rc, json.loads(_LAST[0].readouterr().out.splitlines()[-1])


_LAST = []


def test_superseded_ready_is_dropped_not_paired_with_the_next_send(tmp_path, capsys):
    """Prism b0dbbe468ced: a cancelled reply's ``response ready`` must not pair with the NEXT turn's
    send in that chat (that reported 100 s here instead of 1 s). Same-chat pairing takes only the
    last ready; the superseded one is counted unpaired."""
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=111 session=a time=1.0s\n"
        "2026-10-24 14:01:39,000 INFO x: response ready: platform=discord chat=111 session=a time=1.0s\n"
        "2026-10-24 14:01:40,000 INFO x: [discord] Sending response (10 chars) to 111\n"
        "2026-10-24 14:05:00,000 INFO x: [discord] Sending response (10 chars) to 111\n")
    assert rc == 0
    assert (out["n"], out["max_s"], out["unpaired"]) == (1, 1.0, 2), out  # stale ready + orphan send


def test_pairs_by_turn_id_when_present(tmp_path, capsys):
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=111 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:10,000 INFO x: response ready: platform=discord chat=111 turn=t2 time=1.0s\n"
        "2026-10-24 14:00:11,000 INFO x: [discord] Sending response (10 chars) to 111 turn=t2\n"
        "2026-10-24 14:00:30,000 INFO x: [discord] Sending response (10 chars) to 111 turn=t1\n")
    assert rc == 0
    assert (out["n"], out["p50_s"], out["max_s"], out["unpaired"]) == (2, 1.0, 30.0, 0), out


def test_send_with_an_unknown_turn_never_takes_another_turns_ready(tmp_path, capsys):
    """Prism 35b6a882d9fe: readies for t1 and t2, then a send for t3: no valid sample (BLIND, exit 2),
    not a fabricated 1 s sample that PASSes a 5 s gate."""
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=1 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:10,000 INFO x: response ready: platform=discord chat=1 turn=t2 time=1.0s\n"
        "2026-10-24 14:00:11,000 INFO x: [discord] Sending response (10 chars) to 1 turn=t3\n",
        "--max-p99-s", "5")
    assert rc == 2 and out["verdict"] == "BLIND" and out["n"] == 0, out
    assert out["unpaired"] == 3, out


@pytest.mark.parametrize("ready_ts,send_ts", [
    ("2026-10-24T21:00:00Z", "2026-10-24T21:00:03Z"),
    ("2026-10-24T14:00:00-07:00", "2026-10-24T21:00:03+00:00"),
    ("2026-10-25T06:00:00.500+09:00", "2026-10-24T21:00:03.500Z"),
])
def test_utc_and_offset_timestamps_are_honoured(tmp_path, capsys, monkeypatch, ready_ts, send_ts):
    """Prism d756c5c3a7d8: ``Z`` and ``+HH:MM`` suffixes were dropped and the time read as local, so
    a run outside the log's timezone picked the wrong window and mixed-offset pairs got wrong lags.
    The host zone is forced off UTC (conftest pins TZ=UTC, where a dropped ``Z`` is invisible)."""
    if not hasattr(time, "tzset"):
        pytest.skip("no time.tzset")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    _LAST[:] = [capsys]
    mod = _script()
    assert mod._epoch(f"{send_ts} INFO x") == 1792875603.0 + (0.5 if "." in send_ts else 0.0)
    log = (f"{ready_ts} INFO x: response ready: platform=discord chat=1 session=a\n"
           f"{send_ts} INFO x: [discord] Sending response (10 chars) to 1\n")
    rc, out = _run(mod, tmp_path, log)
    assert rc == 0 and (out["n"], out["max_s"]) == (1, 3.0), out
    # window is absolute: 1 s around the UTC send instant catches it in any host timezone
    log_path = tmp_path / "gateway.log"
    assert mod.main(["--log", str(log_path), "--since", "1792875602.5", "--window", "2"]) == 0


@pytest.fixture(autouse=True)
def _restore_tzset():
    yield
    if hasattr(time, "tzset"):
        time.tzset()  # after monkeypatch restored TZ (fixture teardown order: this runs last)
