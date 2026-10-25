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
    from datetime import datetime, timezone
    hour = datetime.fromtimestamp(mod._epoch("2026-10-24 14:00:00"), timezone.utc).strftime("%Y-%m-%d %H:00Z")
    assert out[0]["hour"] == hour and out[0]["n"] == 4  # hours are UTC (Prism fdd03dbb8e37)
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


def test_nanosecond_offset_timestamps_parse_the_timezone(tmp_path, monkeypatch):
    """Prism 5175c734a604: a 9-digit fraction left the offset unmatched, so the line read as host-local
    time and a 3 s lag across mixed offsets clamped to 0 and PASSed."""
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    mod = _script()
    log = tmp_path / "gateway.log"
    log.write_text(
        "2026-10-25T06:00:00.123456789+09:00 INFO x: response ready: platform=discord chat=1\n"
        "2026-10-24T21:00:03.123456789Z INFO x: [discord] Sending response (10 chars) to 1\n", encoding="utf-8")
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "1"]) == 1
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "3.01"]) == 0
    monkeypatch.delenv("TZ")
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.mark.asyncio
async def test_gauge_keeps_precision_for_the_threshold(tmp_path, caplog, monkeypatch):
    """Prism 46a62b2e0ef8: the gauge printed %.1f, so 5.04 s became 5.0 and PASSed a 5.01 s limit on
    the preferred (gauge) path. The field now carries the raw measurement."""
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "stop_reply_clock", lambda *a, **k: None)
    monkeypatch.setattr(base, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: 105.04}))
    event = _event(_response_ready_at=100.0)
    with caplog.at_level(logging.INFO, logger=base.logger.name):
        await _adapter().send_final_ledgered(event, "sk", "hello", {}, reply_to=None)
    line = next(r.getMessage() for r in caplog.records if "Sending response" in r.getMessage())
    mod = _script()
    log = tmp_path / "gateway.log"
    log.write_text(f"2026-10-24 14:00:05,040 INFO gateway.platforms.base: {line}\n", encoding="utf-8")
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "5.01"]) == 1
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "5.05"]) == 0


@pytest.mark.asyncio
async def test_gauge_logs_the_full_float_not_three_decimals(tmp_path, caplog, monkeypatch):
    """Prism 17b1ec3cef71: ``%.3f`` turned 5.0104 s into 5.010, which PASSed a 5.01 s limit. The log
    carries the full float; only the script's summary rounds, for display."""
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "stop_reply_clock", lambda *a, **k: None)
    monkeypatch.setattr(base, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: 105.0104}))
    event = _event(_response_ready_at=100.0)
    with caplog.at_level(logging.INFO, logger=base.logger.name):
        await _adapter().send_final_ledgered(event, "sk", "hello", {}, reply_to=None)
    line = next(r.getMessage() for r in caplog.records if "Sending response" in r.getMessage())
    mod = _script()
    assert float(mod._LAG.search(line).group(1)) == 105.0104 - 100.0, line
    log = tmp_path / "gateway.log"
    log.write_text(f"2026-10-24 14:00:05,010 INFO gateway.platforms.base: {line}\n", encoding="utf-8")
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "5.01"]) == 1
    assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000", "--max-p99-s", "5.011"]) == 0


def test_turn_id_pairs_only_within_the_same_chat(tmp_path, capsys):
    """Prism 5d7636b702b4: a send in chat 2 carrying turn t1 paired with chat 1's ready for t1 and
    reported a fabricated 1 s sample. Turn pairing now requires the same (platform, chat)."""
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=1 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:01,000 INFO x: [discord] Sending response (10 chars) to 2 turn=t1\n",
        "--max-p99-s", "5")
    assert rc == 2 and out["verdict"] == "BLIND" and out["n"] == 0, out
    assert out["unpaired"] == 2, out  # chat 2's orphan send + chat 1's unmatched ready
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=1 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:01,000 INFO x: [discord] Sending response (10 chars) to 2 turn=t1\n"
        "2026-10-24 14:00:30,000 INFO x: [discord] Sending response (10 chars) to 1 turn=t1\n")
    assert rc == 0 and (out["n"], out["max_s"], out["unpaired"]) == (1, 30.0, 1), out


def test_same_turn_id_in_two_chats_keeps_both_readies(tmp_path, capsys):
    """Prism dceb77e823cc: chat 2's ready for turn t1 overwrote chat 1's, so chat 1's 30 s send was
    dropped and a 5 s gate PASSed on p99=1 s. Readies are keyed by (platform, chat, turn)."""
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-10-24 14:00:00,000 INFO x: response ready: platform=discord chat=1 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:10,000 INFO x: response ready: platform=discord chat=2 turn=t1 time=1.0s\n"
        "2026-10-24 14:00:11,000 INFO x: [discord] Sending response (10 chars) to 2 turn=t1\n"
        "2026-10-24 14:00:30,000 INFO x: [discord] Sending response (10 chars) to 1 turn=t1\n",
        "--max-p99-s", "5")
    assert rc == 1 and (out["n"], out["p99_s"], out["unpaired"]) == (2, 30.0, 0), out



def test_backwards_pair_across_dst_fall_back_is_unpaired_not_zero(tmp_path, monkeypatch, capsys):
    """Prism a6da10393688: naive-local timestamps across the New York fall-back (01:59:59 EDT ready,
    01:00:01 EST send) differ by -3598 s; clamping to 0 s fabricated an instant delivery that PASSed
    a 5 s gate. A backwards pair is counted unpaired: no sample, BLIND, exit 2."""
    if not hasattr(time, "tzset"):
        pytest.skip("no time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-11-01 01:59:59 INFO x: response ready: platform=discord chat=c session=a\n"
        "2026-11-01 01:00:01 INFO x: [discord] Sending response (1 chars) to c\n",
        "--max-p99-s", "5")
    assert rc == 2 and out["n"] == 0 and out["unpaired"] == 1, out

def test_dst_fall_back_hours_stay_separate(tmp_path, monkeypatch, capsys):
    """Prism fdd03dbb8e37: 08:30Z and 09:30Z on 2026-11-01 are both 01:30 in Los Angeles; a local-time
    hour key merged them into one summary."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()
    import json
    try:
        mod = _script()
        log = tmp_path / "gateway.log"
        log.write_text(
            "2026-11-01T08:30:00Z INFO x: [discord] Sending response (10 chars) to 1 delivery_lag_s=1\n"
            "2026-11-01T09:30:00Z INFO x: [discord] Sending response (10 chars) to 1 delivery_lag_s=100\n",
            encoding="utf-8")
        assert mod.main(["--log", str(log), "--since", "0", "--window", "4000000000"]) == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [(r["hour"], r["n"]) for r in rows if "hour" in r] == [
            ("2026-11-01 08:00Z", 1), ("2026-11-01 09:00Z", 1)]
    finally:
        monkeypatch.delenv("TZ")
        if hasattr(time, "tzset"):
            time.tzset()



def test_ambiguous_fall_back_hour_pair_is_not_measured(tmp_path, monkeypatch, capsys):
    """Prism 129fd71e629f: offset-free 01:10 EDT (ready) and 01:11 EST (send) on the New York fall-back
    both read as the FIRST 01:xx, so the 3660 s lag read as 60 s and PASSed a 120 s gate. Either side
    fold-ambiguous: unpaired, BLIND."""
    if not hasattr(time, "tzset"):
        pytest.skip("no time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-11-01 01:10:00 INFO x: response ready: platform=discord chat=c session=a\n"
        "2026-11-01 01:11:00 INFO x: [discord] Sending response (1 chars) to c\n",
        "--max-p99-s", "120")
    assert rc == 2 and out["n"] == 0 and out["unpaired"] == 1, out
    # an unambiguous pair the same night is still measured
    rc, out = _run(mod, tmp_path,
        "2026-11-01 02:10:00 INFO x: response ready: platform=discord chat=c session=a\n"
        "2026-11-01 02:11:00 INFO x: [discord] Sending response (1 chars) to c\n",
        "--max-p99-s", "120")
    assert rc == 0 and (out["n"], out["max_s"]) == (1, 60.0), out



@pytest.mark.parametrize("b_ready,b_send", [
    ("2026-11-01T05:30:00Z", "2026-11-01T05:31:00Z"),          # 226a7bd762a7: 60 s
    ("2026-11-01 01:30:00-04:00", "2026-11-01 01:31:00-04:00"),  # b079624ea4b1: offset-qualified, 60 s
])
def test_ambiguous_ready_never_voids_another_chats_zoned_ready(tmp_path, monkeypatch, capsys, b_ready, b_send):
    """Prism 226a7bd762a7 / b079624ea4b1: ambiguity was kept per EPOCH, so chat a's offset-free
    01:30 (fold-ambiguous, same instant as 05:30Z) dropped chat b's valid zoned 60 s sample and a
    window with one 0.1 s gauge sample PASSed 5 s. The flag now rides on each ready record."""
    if not hasattr(time, "tzset"):
        pytest.skip("no time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    _LAST[:] = [capsys]
    mod = _script()
    rc, out = _run(mod, tmp_path,
        "2026-11-01 01:30:00 INFO x: response ready: platform=discord chat=a session=a\n"
        f"{b_ready} INFO x: response ready: platform=discord chat=b session=b\n"
        f"{b_send} INFO x: [discord] Sending response (1 chars) to b\n"
        "2026-11-01T05:32:00Z INFO x: [discord] Sending response (1 chars) to z delivery_lag_s=0.1\n",
        "--max-p99-s", "5")
    assert rc == 1 and out["n"] == 2 and out["max_s"] == 60.0, out
