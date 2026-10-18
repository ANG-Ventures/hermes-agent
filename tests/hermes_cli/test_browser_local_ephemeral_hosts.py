"""browsers.local_ephemeral_hosts (t_2dca69d0): ACE-AI / ACE-MEDIA serve their own workers' browser:ephemeral.

A worker host listed there gets a lease plan on ITSELF and the s7 join relays to it (dispatch_relay.py over ssh);
an unlisted host keeps the ci-box default; identity leases and Studio-local ephemerals are never relay-joinable.
"""
import json

from hermes_cli import kanban_browser_gate as kbg
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_needs as kn

LANE = "browser-phase1-ephemeral"


def _reg(tmp_path, local):
    p = tmp_path / "browser-hosts.json"
    p.write_text(json.dumps({"browsers": {"mode": "on", "migrated_lanes": [LANE], "local_ephemeral_hosts": local}}))
    return kn.load_registry_file(p)


def _plan(reg, worker_host):
    return kbg.decide(kn.EPHEMERAL, lane=LANE, registry=reg, assignee="x", body="", minted_by=None,
                      lookup=lambda n: None, worker_host=worker_host)


def test_listed_host_plans_itself_and_is_relay_joinable(tmp_path):
    reg = _reg(tmp_path, ["ace-ai", "ACE-MEDIA"])
    assert reg.local_ephemeral == ("ace-ai", "ace-media")
    for h in ("ace-ai", "ace-media"):
        p = _plan(reg, h)
        assert p.host == h
        assert kbd._relay_joinable([p], h, reg.local_ephemeral)


def test_unlisted_host_keeps_ci_box(tmp_path):
    reg = _reg(tmp_path, ["ace-media"])
    p = _plan(reg, "ace-ai")
    assert p.host == "ci-box"
    assert kbd._relay_joinable([p], "ace-ai", reg.local_ephemeral)
    assert _plan(_reg(tmp_path, []), "ace-media").host == "ci-box"


def test_absent_key_is_today(tmp_path):
    p = tmp_path / "browser-hosts.json"
    p.write_text(json.dumps({"browsers": {"mode": "on", "migrated_lanes": [LANE]}}))
    reg = kn.load_registry_file(p)
    assert reg.local_ephemeral == ()
    assert _plan(reg, "ace-ai").host == "ci-box"


def test_never_joinable_studio_local_or_identity_or_plan_for_another_host(tmp_path):
    reg = _reg(tmp_path, ["ace-ai", "studio"])
    studio = _plan(reg, "studio")
    assert studio.host == "studio" and not kbd._relay_joinable([studio], "studio", reg.local_ephemeral)
    ident = kbg.Plan("browser:amazon-alex", LANE, "read", identity="id-amazon-alex", host="ace-ai")
    assert not kbd._relay_joinable([ident], "ace-ai", reg.local_ephemeral)
    # a plan cut for ace-ai never joins for a worker that moved to ace-media
    assert not kbd._relay_joinable([kbg.Plan(kn.EPHEMERAL, LANE, "act", host="ace-ai")], "ace-media",
                                   reg.local_ephemeral)
