"""Cron #alerts deliveries take notify.py's audience split (t_6a4c207a).

Drives the real ``_deliver_result`` choke point with only the network send stubbed. The fleet
library is a stand-in with the same contract as hermes-home ``scripts/lib/agent_inbox.py``
(split_disabled, load_config, audience, inbox_dir, Inbox.record).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import scheduler
from cron.scheduler import _deliver_result

ALERTS = "1480528231286181948"
LOGS = "1480525090331561984"
DM = "1400000000000000001"

LIB = '''
import json, os
from pathlib import Path
def split_disabled(root):
    if os.environ.get("NOTIFY_AUDIENCE_SPLIT", "").strip() == "0":
        return True
    return (Path(root) / "state" / "notify-audience.DISABLED").exists()
def load_config(root):
    return json.loads((Path(root) / "fleet" / "notify-audience.json").read_text())
def audience(producer, message, cfg, explicit=None):
    if producer in set(cfg.get("human_producers") or []):
        return "human"
    head = (message or "").strip().split("\\n", 1)[0]
    if any(m and m in head for m in cfg.get("human_markers") or []):
        return "human"
    return "human" if cfg.get("default_audience") == "human" else "agent"
def inbox_dir(root):
    return Path(root) / "state" / "agent-inbox"
class Inbox:
    def __init__(self, d):
        self.dir = Path(d)
    def record(self, producer, sev, message, now=None):
        if (self.dir.parent / "inbox-boom").exists():
            raise OSError("disk full")
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"producer": producer, "sev": sev, "text": message}) + "\\n")
        return {"id": "0123456789ab"}
'''


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "agent_inbox.py").write_text(LIB, encoding="utf-8")
    (root / "fleet").mkdir()
    (root / "fleet" / "notify-audience.json").write_text(json.dumps({
        "default_audience": "agent", "human_markers": ["NEEDS-ACE"],
        "human_producers": ["courierwatch", "cron:ups-winddown"]}), encoding="utf-8")
    (root / "state").mkdir()
    home = root / "profiles" / "worker"  # profile home: library found by walking up
    home.mkdir(parents=True)
    monkeypatch.delenv("NOTIFY_AUDIENCE_SPLIT", raising=False)
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    return root


def _job(**extra):
    job = {"id": "scm1", "name": "studio-continuity-monitor", "no_agent": True,
           "script": "studio-continuity-monitor.sh", "deliver": f"discord:{ALERTS}"}
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


def _inbox(root):
    p = root / "state" / "agent-inbox" / "events.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_agent_audience_alerts_row_goes_to_inbox_not_posted(fleet):
    send = _deliver(_job(), "studio continuity: 2 checks stale")
    send.assert_not_called()
    rows = _inbox(fleet)
    assert len(rows) == 1
    assert rows[0]["producer"] == "cron:studio-continuity-monitor"
    assert rows[0]["text"] == "studio continuity: 2 checks stale"  # unwrapped, no -# cron footer
    assert rows[0]["sev"] == "warn"


def test_failed_run_goes_to_inbox_as_error(fleet):
    send = _deliver(_job(), "⚠️ **studio-continuity-monitor** · rc=1 · boom", success=False)
    send.assert_not_called()
    assert [r["sev"] for r in _inbox(fleet)] == ["error"]


def test_kill_switch_file_posts_as_today(fleet):
    (fleet / "state" / "notify-audience.DISABLED").touch()
    send = _deliver(_job(), "studio continuity: 2 checks stale")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == ALERTS
    assert _inbox(fleet) == []


def test_inbox_failure_fails_open_and_posts(fleet):
    (fleet / "state" / "inbox-boom").touch()
    send = _deliver(_job(), "studio continuity: 2 checks stale")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == ALERTS


def test_missing_config_fails_open_and_posts(fleet):
    (fleet / "fleet" / "notify-audience.json").unlink()
    send = _deliver(_job(), "studio continuity: 2 checks stale")
    send.assert_called_once()
    assert _inbox(fleet) == []


@pytest.mark.parametrize("name", ["courierwatch", "ups-winddown"])
def test_human_producer_posts(fleet, name):
    send = _deliver(_job(name=name), "package at the front door")
    send.assert_called_once()
    assert _inbox(fleet) == []


def test_human_marker_posts(fleet):
    send = _deliver(_job(), "NEEDS-ACE: plug the NAS back in")
    send.assert_called_once()
    assert _inbox(fleet) == []


@pytest.mark.parametrize("chat", [LOGS, DM])
def test_other_targets_unchanged(fleet, chat):
    send = _deliver(_job(no_agent=False, deliver=f"discord:{chat}"), "studio continuity ok")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == chat
    assert _inbox(fleet) == []


def test_only_the_alerts_target_of_a_fan_out_is_gated(fleet):
    send = _deliver(_job(no_agent=False, deliver=f"discord:{ALERTS},discord:{DM}"), "drift")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == DM
    assert len(_inbox(fleet)) == 1


def test_no_fleet_library_is_inert(tmp_path, monkeypatch):
    home = tmp_path / "plain-home"
    home.mkdir()
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    send = _deliver(_job(), "studio continuity: 2 checks stale")
    send.assert_called_once()



@pytest.mark.parametrize("no_agent", [True, False])
def test_inbox_text_is_redacted(fleet, no_agent):
    key = "sk-" + "A" * 32  # synthetic; the channel payload is redacted, so must the inbox be
    send = _deliver(_job(no_agent=no_agent), f"token {key}")
    send.assert_not_called()
    rows = _inbox(fleet)
    assert len(rows) == 1
    assert key not in rows[0]["text"]
    assert key not in (fleet / "state" / "agent-inbox" / "events.jsonl").read_text(encoding="utf-8")
