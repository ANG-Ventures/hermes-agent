"""Browser Sandboxes P1-1 dispatcher half (t_ce5498d0): the per-tick mint/lease/revoke client, the R3(c)
refusal exit shape, and R1(a)'s named-allowlist forward of the lease env into a placed shell.

The dispatch token is held in the client object only: these tests prove it never reaches a worker's
placement env, the lease env, or the ssh client env of a placed shell.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hermes_cli import kanban_browser_lease as kbl
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp
from tools.environments import ssh as ssh_env
from tools.environments.ssh import SSHEnvironment

DISPATCH_NAME = "BROWSER_DISPATCH_" + "TOKEN"  # the env-var name D17 forbids in any worker surface


class FakeBrowserd:
    """Speaks browserd's wire shape; records every request."""

    def __init__(self):
        self.reqs, self.live = [], None

    def __call__(self, sock, req):
        self.reqs.append(req)
        verb, auth = req["verb"], req.get("auth") or {}
        if verb == "mint":
            if self.live:
                return {"ok": False, "refusal": "BROWSER_DISPATCH_TOKEN_LIVE"}
            self.live = "d" * 64
            return {"ok": True, "token": self.live}
        if verb == "revoke":
            assert auth.get("dispatch") == self.live
            self.live = None
            return {"ok": True}
        if auth.get("dispatch") != self.live or not self.live:
            return {"ok": False, "refusal": "BROWSER_DISPATCH_TOKEN_REQUIRED"}
        if verb == "lease":
            return {"ok": True, "lease_id": 7, "lease_token": "l" * 64, "lease_class": req["args"]["class"],
                    "lease_expires_at": 1800.0, "lease_url": "wss://cdp.browsers.ace/" + req["args"]["name"]}
        if verb == "register-run":
            return {"ok": True, "run_token": "r" * 64}
        if verb == "release":
            return {"ok": True}
        return {"ok": False, "refusal": "BROWSER_VERB_UNKNOWN"}


def test_tick_mints_once_leases_with_token_and_revokes_last():
    bd = FakeBrowserd()
    c = kbl.TickLeaseClient("/sock", "tick1", call=bd)
    try:
        lease = c.lease("id-amazon-alex", klass="read", card="t_c", run_id="9", source_host="ace-ai")
        rt = c.register_run("t_c", "9")
    finally:
        c.revoke()
    verbs = [r["verb"] for r in bd.reqs]
    assert verbs == ["mint", "lease", "register-run", "revoke"]
    assert bd.reqs[1]["args"]["source_host"] == "ace-ai"  # R1(a): bound to the TOOL host
    assert bd.live is None
    # R3(b): the next tick mints on the healthy path (no TOKEN_LIVE)
    c2 = kbl.TickLeaseClient("/sock", "tick2", call=bd)
    c2.lease("id-amazon-alex", klass="read", card="t_d", run_id="10", source_host="studio")
    c2.revoke()
    assert [r["verb"] for r in bd.reqs][-3:] == ["mint", "lease", "revoke"]
    env = kbl.lease_env(lease, rt)
    assert set(env) == set(kbl.LEASE_ENV_NAMES)
    assert DISPATCH_NAME not in env and bd.reqs[0] and "d" * 64 not in json.dumps(env)


def test_refusal_raises_with_token():
    bd = FakeBrowserd()
    bd.live = "x"  # a token is live from somewhere else
    c = kbl.TickLeaseClient("/sock", "t", call=bd)
    with pytest.raises(kbl.BrowserdRefused) as ei:
        c.lease("id-a", klass="read", card="t", run_id="1", source_host="studio")
    assert ei.value.token == "BROWSER_DISPATCH_TOKEN_LIVE"


@pytest.mark.parametrize("token", sorted(kbl.DISPATCHER_FACING))
def test_r3c_every_dispatcher_refusal_is_ready_no_reroute_one_page_per_hour(token):
    paged = {}
    first = kbl.refusal_exit(token, last_paged=paged, now=1000.0)
    assert first == {"status": "ready", "event": (kbl.REFUSED_EVENT, {"token": token}), "page": True, "reroute": False}
    assert kbl.refusal_exit(token, last_paged=paged, now=1000.0 + 3599)["page"] is False
    assert kbl.refusal_exit(token, last_paged=paged, now=1000.0 + 3600)["page"] is True


def test_r3c_the_six_and_only_the_six():
    assert len(kbl.DISPATCHER_FACING) == 6
    with pytest.raises(ValueError):
        kbl.refusal_exit("BROWSER_LEASE_AMBIGUOUS", last_paged={})  # worker-facing: t_ecfd5591's shape


def test_dispatch_token_name_not_in_placement_env_or_allowlist():
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user="kanbanw", slots=1, capacity_pct=0.8,
                        absence="required", profiles=(), state="active", enabled=True, priority=0)
    env: dict = {}
    kwh.apply_placement(env, host, "/w", run_id=1)
    assert DISPATCH_NAME not in json.dumps(env)
    assert DISPATCH_NAME not in kbl.LEASE_ENV_NAMES
    assert all(n.startswith("BROWSER_") and "DISPATCH" not in n for n in kbl.LEASE_ENV_NAMES)


def _capture(monkeypatch):
    import subprocess
    import tools.env_passthrough as env_passthrough
    monkeypatch.setattr(env_passthrough, "get_all_passthrough", lambda: frozenset())
    # Same seams as tests/tools/test_ssh_environment.py: no real ssh during SSHEnvironment.__init__.
    monkeypatch.setattr("tools.environments.ssh.subprocess.run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                        lambda *a, **k: MagicMock(stdout=iter([]), stderr=iter([]), stdin=MagicMock()))
    monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
    captured = {}

    def fake_popen(cmd, stdin_data=None, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        return MagicMock()
    monkeypatch.setattr(ssh_env, "_popen_bash", fake_popen)
    SSHEnvironment(host="h", user="u")._run_bash("echo ok")
    return captured


def test_r1a_placed_shell_receives_lease_env_by_named_sendenv(monkeypatch):
    monkeypatch.setenv("KANBAN_WORKER_PLACEMENT", json.dumps({"host": "ace-ai", "env": {"TERMINAL_ENV": "ssh"}}))
    for n in kbl.LEASE_ENV_NAMES:
        monkeypatch.setenv(n, "v-" + n)
    monkeypatch.setenv(DISPATCH_NAME, "must-never-travel")
    monkeypatch.setenv("BROWSER_SOMETHING_ELSE", "nope")
    cap = _capture(monkeypatch)
    sent = {a.split("=", 1)[1] for a in cap["cmd"] if a.startswith("SendEnv=")}
    assert sent == set(kbl.LEASE_ENV_NAMES)
    assert DISPATCH_NAME not in sent and "BROWSER_SOMETHING_ELSE" not in sent
    remote = " ".join(cap["cmd"])
    assert "v-BROWSER_LEASE_TOKEN" not in remote  # values ride the client env, never argv
    assert cap["env"]["BROWSER_LEASE_TOKEN"] == "v-BROWSER_LEASE_TOKEN"


def test_r1a_unplaced_shell_forwards_nothing(monkeypatch):
    monkeypatch.delenv("KANBAN_WORKER_PLACEMENT", raising=False)
    monkeypatch.setenv("BROWSER_LEASE_TOKEN", "x")
    cap = _capture(monkeypatch)
    assert not any(a.startswith("SendEnv=") for a in cap["cmd"])
