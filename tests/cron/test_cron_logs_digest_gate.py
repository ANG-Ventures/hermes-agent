"""Cron #logs deliveries ride the fleet #logs digest spool (t_42a9c32b).

Drives the real ``_deliver_result`` choke point with only the network send
stubbed. The fleet library is a stand-in with the same contract as
hermes-home ``scripts/lib/logs_digest.py`` (LOGS_CHANNEL, state_dir, armed,
spool): armed = fresh ``flusher.heartbeat`` and no ``disabled`` file.
"""

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import scheduler
from cron.scheduler import _deliver_result

LOGS = "1480525090331561984"
ALERTS = "1480528231286181948"

LIB = '''
import json, os, time
from pathlib import Path
LOGS_CHANNEL = "1480525090331561984"
def state_dir(root):
    return Path(root) / "state" / "logs-digest"
def armed(d, now=None):
    if (d / "disabled").exists():
        return False
    try:
        return time.time() - (d / "flusher.heartbeat").stat().st_mtime < 5 * 3600
    except OSError:
        return False
def spool(d, producer, sev, message, now=None):
    if (d / "boom").exists():
        raise OSError("disk full")
    with open(d / "spool.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "producer": producer, "sev": sev, "text": message}) + "\\n")
'''


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "logs_digest.py").write_text(LIB, encoding="utf-8")
    home = root / "profiles" / "worker"  # profile home: library found by walking up
    home.mkdir(parents=True)
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    d = root / "state" / "logs-digest"
    d.mkdir(parents=True)
    (d / "flusher.heartbeat").touch()
    return d


def _job(**extra):
    job = {"id": "rgb1", "name": "ace-media-rgb-watch", "no_agent": True,
           "script": "ace-media-rgb-watch.sh", "deliver": f"discord:{LOGS}"}
    job.update(extra)
    return job


def _deliver(job, content, success=True):
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send:
        err = _deliver_result(job, content, success=success)
    assert err is None
    return send


def _spooled(d):
    p = d / "spool.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_green_no_agent_logs_row_is_spooled_not_posted(fleet):
    send = _deliver(_job(), "rgb drift corrected on ACE-MEDIA (2 zones)")
    send.assert_not_called()
    rows = _spooled(fleet)
    assert len(rows) == 1
    assert rows[0]["producer"] == "cron:ace-media-rgb-watch"
    assert rows[0]["text"] == "rgb drift corrected on ACE-MEDIA (2 zones)"  # unwrapped, no -# cron footer
    assert rows[0]["sev"] == "info"


def test_failed_run_posts_at_once(fleet):
    send = _deliver(_job(), "⚠️ **ace-media-rgb-watch** · rc=2 · boom", success=False)
    send.assert_called_once()
    assert _spooled(fleet) == []


def test_agent_row_posts_at_once(fleet):
    job = _job(name="morning-digest", no_agent=False)
    send = _deliver(job, "Good morning. Three things today ...")
    send.assert_called_once()
    assert _spooled(fleet) == []


def test_other_channel_posts_at_once(fleet):
    send = _deliver(_job(deliver=f"discord:{ALERTS}"), "rgb drift")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == ALERTS
    assert _spooled(fleet) == []


def test_disarmed_by_stale_heartbeat_posts_at_once(fleet):
    old = time.time() - 6 * 3600
    os.utime(fleet / "flusher.heartbeat", (old, old))
    send = _deliver(_job(), "rgb drift")
    send.assert_called_once()
    assert _spooled(fleet) == []


def test_disarmed_by_disabled_file_posts_at_once(fleet):
    (fleet / "disabled").touch()
    send = _deliver(_job(), "rgb drift")
    send.assert_called_once()
    assert _spooled(fleet) == []


def test_spool_error_fails_open_and_posts(fleet):
    (fleet / "boom").touch()
    send = _deliver(_job(), "rgb drift")
    send.assert_called_once()


def test_no_fleet_library_is_inert(tmp_path, monkeypatch):
    home = tmp_path / "plain-home"
    home.mkdir()
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    send = _deliver(_job(), "rgb drift")
    send.assert_called_once()


def test_spool_rides_only_the_logs_target_of_a_fan_out(fleet):
    job = _job(deliver=f"discord:{LOGS},discord:{ALERTS}")
    send = _deliver(job, "rgb drift")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == ALERTS
    assert len(_spooled(fleet)) == 1
