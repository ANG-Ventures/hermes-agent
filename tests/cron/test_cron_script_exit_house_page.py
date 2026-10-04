"""no_agent script-exit pages render in the house shape (t_4bcf8c20).

Before (09-29, 9 pages in 6 h): "⚠️ Cronjob Failed: X / 🪪 Job ID / ------ / ⚠️ Cron 'X' failed:
Script exited with code 1 stdout: <177 whitespace-collapsed chars>..." -- the subject said twice,
no rc in the subject, the cause truncated mid-word.
After: subject = job + rc, cause = the script's first stdout line, ask = its own next action,
the rest counted; findings (exit 1) optionally re-route via cron.findings_deliver_map.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import cron.scheduler as sched
from cron.scheduler import (
    _deliver_result,
    _findings_deliver_job,
    _repeated_script_error_page,
    _summarize_cron_failure_for_delivery,
)

ERR = (
    "Script exited with code 1\n"
    "stdout:\n"
    "⚠️ FLEET MODEL DRIFT — 1 finding(s)\n"
    "• .hermes/t_d90b64f2: FAIL host-load brief matches 'Benchmark' without an explicit host constraint\n"
    "fix: add host: to the brief (kanban t_d90b64f2)\n"
    "-------------\n"
    "checked 412 briefs in 3.2s"
)
JOB = {"id": "89c26ec1e405", "name": "fleet-model-drift-watch", "no_agent": True,
       "deliver": "discord:1480528231286181948"}


def test_script_exit_is_subject_rc_cause_ask():
    out = _summarize_cron_failure_for_delivery(JOB, ERR)
    assert out.splitlines() == [
        "⚠️ **fleet-model-drift-watch** · rc=1",
        "FLEET MODEL DRIFT — 1 finding(s)",
        "fix: add host: to the brief (kanban t_d90b64f2)",
        "-# +2 more output line(s) saved in the cron output",
    ]


def test_long_cause_line_is_clipped_not_collapsed_into_one_blob():
    err = "Script exited with code 2\nstdout:\n" + "x" * 900 + "\nsecond"
    first, cause = _summarize_cron_failure_for_delivery(JOB, err).splitlines()[:2]
    assert first == "⚠️ **fleet-model-drift-watch** · rc=2"
    assert len(cause) == 300 and cause.endswith("…")


def test_stderr_only_uses_stderr_first_line():
    err = "Script exited with code 2\nstderr:\nTraceback (most recent call last):\n  x\nKeyError: 'a'"
    lines = _summarize_cron_failure_for_delivery(JOB, err).splitlines()
    assert lines[:2] == ["⚠️ **fleet-model-drift-watch** · rc=2", "KeyError: 'a'"]


def test_colon_header_joins_its_content_line_and_wrapped_ask_joins_its_tail():
    err = ("Script exited with code 1\nstdout:\n⚠️ **gbrain deploy parity** (09:50 PDT):\n"
           "live tree 816372a97 != master a84a9fea8 (ahead 1 / behind 0)\n"
           "Fix: `fleet.sh deploy gbrain --restart` then re-verify with\n`verify-shipped.py --repo gbrain`.\nfooter")
    lines = _summarize_cron_failure_for_delivery(JOB, err).splitlines()
    assert lines[1] == "gbrain deploy parity (09:50 PDT): live tree 816372a97 != master a84a9fea8 (ahead 1 / behind 0)"
    assert lines[2] == "Fix: `fleet.sh deploy gbrain --restart` then re-verify with `verify-shipped.py --repo gbrain`."
    assert lines[3] == "-# +1 more output line(s) saved in the cron output"


def test_agent_job_keeps_the_old_summary():
    job = dict(JOB, no_agent=False)
    assert _summarize_cron_failure_for_delivery(job, ERR).startswith("⚠️ Cron 'fleet-model-drift-watch' failed:")


def test_timeout_contract_is_not_reshaped():
    out = _summarize_cron_failure_for_delivery(JOB, "Script timed out after 60s: /x.sh")
    # Merged copy table wording ("its script timed out"); the point is that the runner's timeout
    # contract is never reshaped into the house page.
    assert out.startswith("⚠️ Cron 'fleet-model-drift-watch' failed: its script timed out.")


def _send(job, content):
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.TELEGRAM: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send_mock:
        _deliver_result(job, content, success=False)
    return send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]


def test_wrapper_does_not_restate_the_subject():
    job = dict(JOB, deliver="origin", origin={"platform": "telegram", "chat_id": "1"})
    sent = _send(job, _summarize_cron_failure_for_delivery(JOB, ERR))
    lines = sent.splitlines()
    assert lines[0] == "⚠️ **fleet-model-drift-watch** · rc=1"
    assert "Cronjob Failed" not in sent
    assert lines[-1].startswith("-# cron fleet-model-drift-watch · job 89c26ec1e405")
    assert len(lines) <= 8


def test_wrapper_still_heads_a_non_script_failure():
    job = {"id": "j", "name": "morning-digest", "deliver": "origin",
           "origin": {"platform": "telegram", "chat_id": "1"}}
    assert _send(job, "RuntimeError: boom").startswith("⚠️ **Cronjob Failed: morning-digest**")


def test_stuck_page_is_house_shaped_and_keeps_the_cause():
    job = dict(JOB, last_status="error", last_error=ERR, error_repeat_streak=2)
    page = _repeated_script_error_page(job, ERR)
    assert page
    lines = page.splitlines()
    assert lines[0] == "⚠️ **fleet-model-drift-watch** · stuck: same error 3 runs in a row".replace("⚠️", "🚨")
    assert lines[1] == "Cause: FLEET MODEL DRIFT — 1 finding(s)"
    assert lines[2].startswith("Fix: ")
    assert "t_d90b64f2" in page and "412" in page  # full cause folded, ids/numbers kept
    assert len(lines) <= 8


def test_findings_reroute_only_exit1_with_report_when_mapped():
    mapping = {"cron": {"findings_deliver_map": {"discord:1480528231286181948": "discord:1480525090331561984"}}}
    with patch.object(sched, "load_config", return_value=mapping):
        assert _findings_deliver_job(JOB, ERR)["deliver"] == "discord:1480525090331561984"
        blind = ERR.replace("code 1", "code 2")
        assert _findings_deliver_job(JOB, blind)["deliver"] == JOB["deliver"]  # exit 2 still pages
        no_report = "Script exited with code 1\nstderr:\nboom"
        assert _findings_deliver_job(JOB, no_report)["deliver"] == JOB["deliver"]  # exit 1, no report = down
        empty_then_err = "Script exited with code 1\nstdout:\n  \nstderr:\nboom"
        assert _findings_deliver_job(JOB, empty_then_err)["deliver"] == JOB["deliver"]  # blank stdout section
        other = dict(JOB, deliver="discord:1552284606907023370")
        assert _findings_deliver_job(other, ERR)["deliver"] == other["deliver"]  # unmapped target
        assert _findings_deliver_job(dict(JOB, no_agent=False), ERR) is not None
        assert _findings_deliver_job(dict(JOB, no_agent=False), ERR)["deliver"] == JOB["deliver"]
    with patch.object(sched, "load_config", return_value={}):
        assert _findings_deliver_job(JOB, ERR)["deliver"] == JOB["deliver"]  # default: unchanged
