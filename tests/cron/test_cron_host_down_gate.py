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
    (root / "scripts" / "lib" / "fleet-hosts.json").write_text(json.dumps(HOSTS), encoding="utf-8")
    home = root / "profiles" / "worker"  # profile-scoped home: table found by walking up
    home.mkdir(parents=True)
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: home)
    return root


def _arm(root, host="ace-ai", since="2026-09-23T21:01:00Z"):
    latch = root / HOSTS["hosts"][host]["latch"]
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(since, encoding="utf-8")
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
    rows = [json.loads(l) for l in (latch.parent / "suppressed.jsonl").read_text(encoding="utf-8").splitlines()]
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


def test_owner_exemption_is_scoped_to_the_host_it_owns(fleet):
    """ace-ai's deadman reporting on a DOWN nas must not bypass nas's gate (#1026 C4)."""
    _arm(fleet)
    nas_latch = _arm(fleet, "nas")
    job = _qbt_job(name="ace-ai-host-deadman", script="ace-ai-host-deadman.py")
    chat, msg = _deliver(job, "rsync to fleet nas 192.168.1.159 failed")
    assert chat == LOGS
    assert "[host-down: nas since" in msg and "deferred to nas-host-deadman" in msg
    assert (nas_latch.parent / "suppressed.jsonl").exists()


def test_owner_note_about_its_own_host_still_pages_with_another_host_down(fleet):
    _arm(fleet)
    _arm(fleet, "nas")
    job = _qbt_job(name="ace-ai-host-deadman", script="ace-ai-host-deadman.py")
    for content in ("ACE-AI is DOWN", "ACE-AI is DOWN; fleet nas also unreachable", "ssh exited rc=255"):
        chat, msg = _deliver(job, content)
        assert chat == ALERTS and "[host-down" not in msg, content


def test_non_alerts_target_untouched(fleet):
    _arm(fleet)
    job = _qbt_job(deliver="discord:999")
    chat, msg = _deliver(job, _failure_content(job, "cannot read torrents/info"))
    assert chat == "999" and "[host-down" not in msg


def test_fail_open_on_corrupt_table(fleet):
    _arm(fleet)
    (fleet / "scripts" / "lib" / "fleet-hosts.json").write_text("{not json", encoding="utf-8")
    job = _qbt_job()
    chat, msg = _deliver(job, _failure_content(job, "cannot read torrents/info"))
    assert chat == ALERTS and "[host-down" not in msg


def test_no_table_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)
    job = _qbt_job()
    chat, _ = _deliver(job, "cannot read torrents/info")
    assert chat == ALERTS


def test_ledger_not_written_when_demoted_delivery_fails(fleet):
    """k88: the ledger row counts a DELIVERED deferral; a failed send must not
    leave a row behind (the retry path would otherwise double-count it)."""
    from gateway.config import Platform

    latch = _arm(fleet)
    job = _qbt_job()
    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"error": "Discord send failed"})):
        err = _deliver_result(job, _failure_content(job, "cannot read torrents/info"))
    assert err and "Discord send failed" in err
    assert not (latch.parent / "suppressed.jsonl").exists()


def test_ledger_not_written_when_only_a_same_id_other_platform_target_delivers(fleet):
    """#logs failed; a telegram target with the SAME chat id succeeded. The deferral
    never reached #logs, so no ledger row."""
    from gateway.config import Platform

    latch = _arm(fleet)
    job = _qbt_job(deliver=f"discord:{ALERTS},telegram:{LOGS}")
    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig, Platform.TELEGRAM: pconfig}

    async def send(platform, pcfg, chat_id, *a, **k):
        if platform == Platform.DISCORD:
            return {"error": "Discord send failed"}
        return {"success": True}

    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform", new=send):
        err = _deliver_result(job, _failure_content(job, "cannot read torrents/info"))
    assert err and "Discord send failed" in err
    assert not (latch.parent / "suppressed.jsonl").exists()


def test_ledger_row_is_flushed_as_soon_as_logs_delivery_succeeds(fleet):
    """The #logs send landed; the row must be on disk before later targets run,
    so an abort or worker exit during them cannot lose a delivered deferral."""
    from gateway.config import Platform

    latch = _arm(fleet)
    ledger = latch.parent / "suppressed.jsonl"
    job = _qbt_job(deliver=f"discord:{ALERTS},telegram:12345")
    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig, Platform.TELEGRAM: pconfig}
    seen_at_later_target = []

    async def send(platform, pcfg, chat_id, *a, **k):
        if platform == Platform.TELEGRAM:
            seen_at_later_target.append(ledger.exists())
        return {"success": True}

    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform", new=send):
        _deliver_result(job, _failure_content(job, "cannot read torrents/info"))
    assert seen_at_later_target == [True]


def test_prefix_rides_only_the_demoted_target(fleet):
    """FleetReview #31: the host-down prefix was applied to the SHARED content,
    so every target (not only the demoted #alerts one) got it."""
    from gateway.config import Platform

    _arm(fleet)
    job = _qbt_job(deliver=f"discord:{ALERTS},discord:999")
    content = _failure_content(job, "cannot read torrents/info: RuntimeError")
    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.DISCORD: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send:
        assert _deliver_result(job, content) is None
    sent = {str(c[0][2]): c[0][3] for c in send.call_args_list}
    assert set(sent) == {LOGS, "999"}
    assert "[host-down: ace-ai since" in sent[LOGS]
    assert "[host-down" not in sent["999"]
