"""PHASE=delivery_stages: per-stage timing of the ``response ready`` -> send tail (t_0072cc4d).

A slow turn logs ONE line naming the stage it was parked on; a fast turn logs nothing. The
readout script turns those lines into percentiles plus the dominant stage per slow turn.
"""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path

import pytest

from tests.gateway.test_turn_lease_flush_window import _Rig, _msg

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "delivery-lag-p99.py"


def _phase_lines(caplog):
    return [r.getMessage() for r in caplog.records if "PHASE=delivery_stages" in r.getMessage()]


@pytest.mark.asyncio
async def test_slow_post_turn_hooks_logs_one_line_naming_hooks(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path)
    real_hooks = rig.runner._hmwa_post_turn_hooks

    async def slow_hooks(*args, **kwargs):
        await asyncio.sleep(3)
        return await real_hooks(*args, **kwargs)

    rig.runner._hmwa_post_turn_hooks = slow_hooks
    caplog.set_level(logging.INFO, logger="gateway.run")
    await rig.adapter.handle_message(_msg("slow"))
    await rig.settle(expect_calls=1, timeout=10)

    lines = _phase_lines(caplog)
    assert len(lines) == 1, lines
    fields = dict(kv.split("=", 1) for kv in lines[0].split() if "=" in kv)
    stages = {s: float(fields[s]) for s in ("footer", "hooks", "compress", "persist", "branch", "deliver")}
    assert max(stages, key=stages.get) == "hooks"
    assert float(fields["ready_to_send_s"]) >= stages["hooks"] >= 3.0
    assert "deliver_pre_send" in fields


@pytest.mark.asyncio
async def test_fast_turn_logs_nothing(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO, logger="gateway.run")
    await rig.adapter.handle_message(_msg("fast"))
    await rig.settle(expect_calls=1)
    assert rig.adapter.sent, "turn did not deliver"
    assert _phase_lines(caplog) == []


def test_readout_percentiles_and_dominant_stage(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("delivery_lag_p99", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "gateway.log"
    log.write_text(
        "2026-10-26 05:37:41,100Z INFO gateway.run: PHASE=delivery_stages session=a ready_to_send_s=25.90 "
        "footer=0.10 hooks=0.20 compress=0.00 persist=24.00 branch=0.00 deliver=1.60 deliver_pre_send=1.50\n"
        "2026-10-26 05:39:11,000Z INFO gateway.run: PHASE=delivery_stages session=b ready_to_send_s=14.00 "
        "footer=0.00 hooks=13.50 compress=0.00 persist=0.30 branch=0.00 deliver=0.20 deliver_pre_send=0.10\n"
        "2026-10-26 05:40:00,000Z INFO gateway.run: PHASE=delivery_stages session=c ready_to_send_s=3.00 "
        "footer=2.80 hooks=0.10 compress=0.00 persist=0.10 branch=0.00 deliver=0.00 deliver_pre_send=0.00\n"
        "2026-10-26 05:40:01,000Z INFO gateway.run: response ready: platform=discord chat=1 session=a\n"
    )
    since = 1792990000  # 2026-10-26 04:46:40Z, window covers all three
    rc = mod.main(["--log", str(log), "--since", str(since), "--window", "7200", "--slow-s", "5"])
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0
    slow, summary = out[:-1], out[-1]
    assert [(r["session"], r["dominant"]) for r in slow] == [("a", "persist"), ("b", "hooks")]
    assert summary["n"] == 3
    assert summary["p50_s"] == 14.0 and summary["p99_s"] == 25.9 and summary["max_s"] == 25.9
    assert summary["dominant_counts"] == {"persist": 1, "hooks": 1, "footer": 1}


@pytest.mark.asyncio
async def test_slow_pre_send_post_turn_hook_logs_post_handler_line(monkeypatch, tmp_path, caplog):
    """The span after PHASE=delivery_stages ends and before the adapter sends (t_04b5d027): a slow
    /loop completion there is named as post_turn, and nothing is booked to the delivery stages."""
    rig = _Rig(monkeypatch, tmp_path)

    async def slow_loop_completion(**_kwargs):
        await asyncio.sleep(2)

    rig.runner._post_turn_loop_completion = slow_loop_completion
    caplog.set_level(logging.INFO)
    await rig.adapter.handle_message(_msg("slow tail"))
    await rig.settle(expect_calls=1, timeout=10)

    assert rig.adapter.sent, "turn did not deliver"
    assert _phase_lines(caplog) == []
    lines = [r.getMessage() for r in caplog.records if "PHASE=post_handler_stages" in r.getMessage()]
    assert len(lines) == 1, lines
    fields = dict(kv.split("=", 1) for kv in lines[0].split() if "=" in kv)
    parts = {k: float(fields[k]) for k in ("post_turn", "goal_pin", "unwind", "extract", "pre_send")}
    assert max(parts, key=parts.get) == "post_turn"
    assert float(fields["stages_end_to_send_s"]) >= parts["post_turn"] >= 2.0


@pytest.mark.asyncio
async def test_pre_send_post_turn_hook_makes_no_activity_write(monkeypatch, tmp_path):
    """t_04b5d027: the pre-send /loop completion resolved the session with touch_activity=True, a
    routing upsert (state.db write) that waited out a foreign writer's lock hold before the send."""
    from gateway.run_goals import GatewayGoalsMixin

    rig = _Rig(monkeypatch, tmp_path)
    calls, in_hook = [], [False]
    lookup = rig.runner.session_store.get_or_create_session

    def _spy(*args, **kwargs):
        if in_hook[0]:
            calls.append((kwargs.get("touch_activity", True), any("user turn" in str(m) for m in rig.adapter.sent)))
        return lookup(*args, **kwargs)

    rig.runner.session_store.get_or_create_session = _spy
    real = GatewayGoalsMixin._run_post_turn_hooks

    async def _hooks(self, **kwargs):
        in_hook[0] = True
        try:
            return await real(self, **kwargs)
        finally:
            in_hook[0] = False

    monkeypatch.setattr(GatewayGoalsMixin, "_run_post_turn_hooks", _hooks)
    await rig.adapter.handle_message(_msg("user turn"))
    await rig.settle(expect_calls=1, timeout=10)

    assert rig.adapter.sent, "turn did not deliver"
    pre_send = [touch for touch, replied in calls if not replied]
    assert pre_send, "the pre-send loop-completion hook should resolve the session"
    assert not any(pre_send), calls
