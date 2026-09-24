"""Cron failure relay honours the fleet host-down gate (card t_8c04a5d2).

While a host's owner-deadman latch is armed, a cron delivery bound for #alerts
that is ABOUT that host is demoted to #logs with a ``[host-down: …]`` prefix
and a ledger row — never dropped. The policy mirrors notify.py's
``_host_down_decision`` (hermes-home, card t_0a889b04). These tests drive the
real ``_deliver_result`` choke point with only the network send stubbed.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import scheduler
from cron.scheduler import _deliver_result, _failure_streak_nudge, _summarize_cron_failure_for_delivery

ALERTS = "1480528231286181948"
LOGS = "1480525090331561984"

HOSTS = {
    "hosts": {
        "ace-ai": {
            "ips": ["192.168.1.216"],
            "names": ["ace-ai", "aceai"],
            "latch": "state/ace-ai-deadman/paged",
            "owner": "ace-ai-host-deadman",
            "dependents": ["qbt-private-missingfiles-monitor"],
        },
        "nas": {
            "ips": ["192.168.1.159"],
            "names": ["fleet nas", "nas"],
            "latch": "state/nas-deadman/paged",
            "owner": "nas-host-deadman",
            "dependents": [],
        },
    },
    "transport_signatures": ["Host is down", "rc=255"],
}


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "fleet-hosts.json").write_text(json.dumps(HOSTS))
    home = root / "profiles" / "worker"  # profile-scoped home: table found by walking up
    home.mkdir(parents=True)
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    return root


def _arm(root, host="ace-ai", since="2026-09-23T21:01:00Z"):
    latch = root / HOSTS["hosts"][host]["latch"]
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(since)
    return latch


def _qbt_job(**extra):
    job = {
        "id": "qbt1",
        "name": "qbt-private-missingfiles-monitor",
        "script": "qbt-private-missingfiles-monitor.py",
        "schedule": {"kind": "interval"},
        "failure_streak": 4,
        "deliver": f"discord:{ALERTS}",
    }
    job.update(extra)
    return job


def _deliver(job, content):
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send:
        err = _deliver_result(job, content)
    assert err is None
    send.assert_called_once()
    args = send.call_args[0]
    return str(args[2]), args[3]  # chat_id, message


def _failure_content(job, error):
    # Exactly what run_one_job composes for a failed run.
    return _summarize_cron_failure_for_delivery(job, error) + _failure_streak_nudge(job)


def test_a_latch_armed_qbt_failure_routes_to_logs_with_prefix_and_ledger(fleet):
    latch = _arm(fleet)
    job = _qbt_job()
    content = _failure_content(job, "cannot read torrents/info: RuntimeError")
    assert "failed 5 runs in a row" in content  # the streak nudge rides along

    chat, msg = _deliver(job, content)

    assert chat == LOGS
    assert "[host-down: ace-ai since 2026-09-23T21:01:00Z — deferred to ace-ai-host-deadman]" in msg
    assert "failed 5 runs in a row" in msg  # demoted, never dropped
    rows = [json.loads(l) for l in (latch.parent / "suppressed.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["producer"] == "qbt-private-missingfiles-monitor"
    assert rows[0]["head"] and set(rows[0]) == {"ts", "producer", "head"}


def test_b_latch_armed_output_names_a_different_up_host_is_unchanged(fleet):
    latch = _arm(fleet)
    job = _qbt_job(name="nas-scrub", script="nas-scrub.py")
    content = _failure_content(job, "zpool degraded on fleet NAS 192.168.1.159")

    chat, msg = _deliver(job, content)

    assert chat == ALERTS
    assert "[host-down" not in msg
    assert not (latch.parent / "suppressed.jsonl").exists()


def test_c_no_latch_is_unchanged(fleet):
    job = _qbt_job()
    content = _failure_content(job, "cannot read torrents/info: RuntimeError")

    chat, msg = _deliver(job, content)

    assert chat == ALERTS
    assert "[host-down" not in msg


def test_names_only_down_host_defers(fleet):
    _arm(fleet)
    job = _qbt_job(name="other", script="other.py")
    chat, msg = _deliver(job, _failure_content(job, "ssh to ace-ai failed"))
    assert chat == LOGS
    assert "[host-down: ace-ai since" in msg


def test_transport_signature_no_host_named_defers(fleet):
    _arm(fleet)
    job = _qbt_job(name="other", script="other.py")
    chat, _ = _deliver(job, _failure_content(job, "ssh exited rc=255"))
    assert chat == LOGS


def test_owner_deadman_itself_is_exempt(fleet):
    _arm(fleet)
    job = _qbt_job(name="ace-ai-host-deadman", script="ace-ai-host-deadman.py")
    chat, _ = _deliver(job, "ACE-AI is DOWN")
    assert chat == ALERTS


def test_non_alerts_target_untouched(fleet):
    _arm(fleet)
    job = _qbt_job(deliver="discord:999")
    chat, msg = _deliver(job, _failure_content(job, "cannot read torrents/info"))
    assert chat == "999" and "[host-down" not in msg


def test_fail_open_on_corrupt_table(fleet):
    _arm(fleet)
    (fleet / "scripts" / "lib" / "fleet-hosts.json").write_text("{not json")
    job = _qbt_job()
    chat, msg = _deliver(job, _failure_content(job, "cannot read torrents/info"))
    assert chat == ALERTS and "[host-down" not in msg


def test_no_table_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)
    job = _qbt_job()
    chat, _ = _deliver(job, "cannot read torrents/info")
    assert chat == ALERTS
