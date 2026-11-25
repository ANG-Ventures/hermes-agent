"""Cron #logs deliveries take the #logs audience split (t_2b76e2d8).

Drives the real ``_deliver_result`` choke point with only the network send stubbed. The fleet
library is a stand-in with the same contract as hermes-home ``scripts/lib/agent_inbox.py``
(logs_split_disabled, load_config, audience, write_agent_log, inbox_dir, Inbox.record).
"""

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import scheduler
from cron.scheduler import _deliver_result

ALERTS = "1480528231286181948"
LOGS = "1480525090331561984"
DM = "1400000000000000001"

LIB = '''
import hashlib, json, os
from pathlib import Path
def split_disabled(root):
    return False
def logs_split_disabled(root):
    if os.environ.get("NOTIFY_LOGS_AUDIENCE_SPLIT", "").strip() == "0":
        return True
    return (Path(root) / "state" / "notify-logs-audience.DISABLED").exists()
def load_config(root):
    return json.loads((Path(root) / "fleet" / "notify-audience.json").read_text())
def audience(producer, message, cfg, explicit=None):
    if producer in set(cfg.get("human_producers") or []):
        return "human"
    head = (message or "").strip().split("\\n", 1)[0]
    if any(m and m in head for m in cfg.get("human_markers") or []):
        return "human"
    lines = (cfg.get("human_logs_lines") or {}).get("lines") or []
    if any(head.startswith(ln["first_line_prefix"]) for ln in lines):
        return "human"
    return "human" if cfg.get("default_audience") == "human" else "agent"
def write_agent_log(root, producer, sev, message, now=None, **extra):
    if (Path(root) / "state" / "log-boom").exists():
        raise OSError("disk full")
    d = Path(root) / "state" / "agent-logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "day.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "t", "producer": producer, "sev": sev,
                            "first_line": (message or "").strip().split("\\n", 1)[0],
                            "body_sha256": hashlib.sha256((message or "").encode()).hexdigest(),
                            "text": message, **extra}) + "\\n")
    return p
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
        "human_producers": ["courierwatch"],
        "human_logs_producers": ["cron:weekly-review", "daily-journal"],
        "human_logs_lines": {"lines": [{"name": "gbrain-daily",
                                        "first_line_prefix": "🧠 **gbrain daily**"}]}}),
        encoding="utf-8")
    (root / "state").mkdir()
    home = root / "profiles" / "worker"  # profile home: library found by walking up
    home.mkdir(parents=True)
    monkeypatch.delenv("NOTIFY_LOGS_AUDIENCE_SPLIT", raising=False)
    monkeypatch.delenv("NOTIFY_AUDIENCE", raising=False)
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    return root


def _job(**extra):
    job = {"id": "tfa1", "name": "tv-fleet-app-storage-audit", "no_agent": True,
           "script": "tv-fleet-app-storage-audit.py", "deliver": f"discord:{LOGS}"}
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


def _rows(root, rel):
    p = root / "state" / rel
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _log(root):
    return _rows(root, "agent-logs/day.jsonl")


def _inbox(root):
    return _rows(root, "agent-inbox/events.jsonl")


def test_agent_row_writes_one_log_row_and_posts_nothing(fleet):
    send = _deliver(_job(no_agent=False), "fleet allocation: 3 hosts idle\nmore detail")
    send.assert_not_called()
    rows = _log(fleet)
    assert len(rows) == 1
    r = rows[0]
    assert r["producer"] == "cron:tv-fleet-app-storage-audit"
    assert r["sev"] == "info"
    assert r["first_line"] == "fleet allocation: 3 hosts idle"
    assert r["text"] == "fleet allocation: 3 hosts idle\nmore detail"  # unwrapped, no -# footer
    assert r["body_sha256"] == hashlib.sha256(r["text"].encode()).hexdigest()
    assert _inbox(fleet) == []


def test_failed_agent_row_also_makes_one_inbox_item(fleet):
    text = "⚠️ **tv-fleet-app-storage-audit** · rc=1 · failed 11 runs in a row"
    send = _deliver(_job(), text, success=False)
    send.assert_not_called()
    assert [r["sev"] for r in _log(fleet)] == ["error"]
    inbox = _inbox(fleet)
    assert len(inbox) == 1
    assert inbox[0]["producer"] == "cron:tv-fleet-app-storage-audit"
    assert inbox[0]["sev"] == "error"


@pytest.mark.parametrize("name", ["weekly-review", "daily-journal"])
def test_human_producer_from_config_posts(fleet, name):
    send = _deliver(_job(name=name, no_agent=False), "📓 Daily Log — 2026-11-16")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == LOGS
    assert _log(fleet) == []


def test_row_declaring_itself_human_posts(fleet):
    send = _deliver(_job(audience="human"), "🩺 X2D Print Doctor: nozzle clog")
    send.assert_called_once()
    assert _log(fleet) == []


def test_shared_human_rules_still_post(fleet):
    send = _deliver(_job(), "NEEDS-ACE: swap the filament spool")
    send.assert_called_once()
    assert _log(fleet) == []


def test_kill_switch_file_restores_posting(fleet):
    (fleet / "state" / "notify-logs-audience.DISABLED").touch()
    send = _deliver(_job(no_agent=False), "fleet allocation: 3 hosts idle")
    send.assert_called_once()
    assert _log(fleet) == []


def test_kill_switch_env_restores_posting(fleet, monkeypatch):
    monkeypatch.setenv("NOTIFY_LOGS_AUDIENCE_SPLIT", "0")
    send = _deliver(_job(), "x rc=2", success=False)
    send.assert_called_once()
    assert _log(fleet) == [] and _inbox(fleet) == []


def test_log_write_failure_fails_open_and_posts(fleet):
    (fleet / "state" / "log-boom").touch()
    send = _deliver(_job(), "clock-shift-nightly rc=2", success=False)
    send.assert_called_once()
    assert str(send.call_args[0][2]) == LOGS


def test_inbox_failure_on_failed_run_posts(fleet):
    (fleet / "state" / "inbox-boom").touch()
    send = _deliver(_job(), "clock-shift-nightly rc=2", success=False)
    send.assert_called_once()


def test_unreadable_config_posts(fleet):
    (fleet / "fleet" / "notify-audience.json").write_text("{not json", encoding="utf-8")
    send = _deliver(_job(no_agent=False), "fleet allocation: 3 hosts idle")
    send.assert_called_once()
    assert _log(fleet) == []


def test_config_without_human_logs_list_is_unarmed(fleet):
    cfg = fleet / "fleet" / "notify-audience.json"
    data = json.loads(cfg.read_text())
    del data["human_logs_producers"]
    cfg.write_text(json.dumps(data), encoding="utf-8")
    send = _deliver(_job(name="daily-journal", no_agent=False), "📓 Daily Log — 2026-11-16")
    send.assert_called_once()
    assert _log(fleet) == []


def test_older_library_without_logs_split_posts(fleet):
    lib = fleet / "scripts" / "lib" / "agent_inbox.py"
    lib.write_text(LIB.replace("def write_agent_log(", "def _gone("), encoding="utf-8")
    send = _deliver(_job(no_agent=False), "fleet allocation: 3 hosts idle")
    send.assert_called_once()


@pytest.mark.parametrize("chat", [ALERTS, DM])
def test_other_targets_unchanged(fleet, chat):
    send = _deliver(_job(no_agent=False, deliver=f"discord:{chat}",
                         name="courierwatch"), "package at the front door")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == chat
    assert _log(fleet) == []


def test_only_the_logs_target_of_a_fan_out_is_gated(fleet):
    send = _deliver(_job(no_agent=False, deliver=f"discord:{LOGS},discord:{DM}"), "drift")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == DM
    assert len(_log(fleet)) == 1


def test_log_text_is_redacted(fleet):
    key = "sk-" + "A" * 32  # synthetic; the channel payload is redacted, so must the log be
    send = _deliver(_job(no_agent=False), f"token {key}")
    send.assert_not_called()
    assert key not in (fleet / "state" / "agent-logs" / "day.jsonl").read_text(encoding="utf-8")


def test_no_fleet_library_is_inert(tmp_path, monkeypatch):
    home = tmp_path / "plain-home"
    home.mkdir()
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    send = _deliver(_job(), "fleet allocation: 3 hosts idle")
    send.assert_called_once()


DIGEST_LIB = '''
import json
from pathlib import Path
LOGS_CHANNEL = "1480525090331561984"
def state_dir(root):
    return Path(root) / "state" / "logs-digest"
def armed(d, now=None):
    return (d / "flusher.heartbeat").exists()
def spool(d, producer, sev, message, now=None):
    with open(d / "spool.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"producer": producer, "sev": sev, "text": message}) + "\\n")
'''


@pytest.fixture()
def fleet_with_digest(fleet):
    (fleet / "scripts" / "lib" / "logs_digest.py").write_text(DIGEST_LIB, encoding="utf-8")
    d = fleet / "state" / "logs-digest"
    d.mkdir(parents=True)
    (d / "flusher.heartbeat").touch()
    return fleet


def _spool(root):
    return _rows(root, "logs-digest/spool.jsonl")


def test_agreed_human_line_posts_now_and_skips_the_digest_spool(fleet_with_digest):
    # t_b3cb7cf7: the 4 h digest rollup is agent-audience, so a human line spooled there is never seen.
    send = _deliver(_job(name="gbrain-daily-digest"), "🧠 **gbrain daily** · 412 pages, 3 new")
    send.assert_called_once()
    assert str(send.call_args[0][2]) == LOGS
    assert _spool(fleet_with_digest) == []
    assert _log(fleet_with_digest) == []


def test_agent_line_still_goes_to_the_agent_log_with_the_digest_armed(fleet_with_digest):
    send = _deliver(_job(), "fleet allocation: 3 hosts idle")
    send.assert_not_called()
    assert len(_log(fleet_with_digest)) == 1
    assert _spool(fleet_with_digest) == []


def test_split_off_still_spools_a_green_no_agent_line(fleet_with_digest):
    (fleet_with_digest / "state" / "notify-logs-audience.DISABLED").touch()
    send = _deliver(_job(), "🧠 **gbrain daily** · 412 pages, 3 new")
    send.assert_not_called()
    assert len(_spool(fleet_with_digest)) == 1
