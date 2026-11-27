"""delegate_task placement="pool" (t_30537365, plans/2026-11-13_delegate-children-on-pool-hosts-ASSESSMENT.md §7).

Real ``_run_single_child`` and terminal resolution; only the allocator, the pool file read and the ssh probe are
stubbed (the live proof runs after a gateway restart).
"""
from __future__ import annotations

import contextvars
import threading
import types

import pytest

from tests.tools.test_delegate_late_completion import (  # noqa: F401
    _Agent,
    _clean_registry,
    fleet_home,
)
from tools import delegate_pool_placement as dpp

_REAL_LEASE_SLUG = dpp._lease_slug

_HOST = types.SimpleNamespace(name="ace-ai", ssh_host="192.0.2.16", ssh_user="kanbanw", ssh_port=22,
                              enabled=True, state="active", target="kanbanw@192.0.2.16")


@pytest.fixture
def pool(monkeypatch):
    """Knob on, one pool host, a granting allocator and a passing probe; records lease traffic."""
    rec = {"admit": [], "release": [], "probe": [], "cleanup": [], "rm": []}
    monkeypatch.setattr(dpp, "mode", lambda: dpp.MODE_OPT_IN)
    monkeypatch.setattr(dpp, "_pool_hosts", lambda: [_HOST])

    def _admit(child_task_id, hosts):
        rec["admit"].append(child_task_id)
        return _HOST, f"rid-{child_task_id}"

    monkeypatch.setattr(dpp, "_admit", _admit)
    monkeypatch.setattr(dpp, "_workspace_for", lambda cid: f"/srv/ws/delegate-{cid}")
    monkeypatch.setattr(dpp, "_lease_slug", lambda cid: cid)  # deterministic keys; the real one is tested below
    import hermes_cli.kanban_worker_hosts as kwh
    monkeypatch.setattr(kwh, "prepare_remote_workspace", lambda host, ws: rec["probe"].append((host.name, ws)))
    import hermes_cli.kanban_headroom as kh
    monkeypatch.setattr(kh, "release_alloc", lambda rid: rec["release"].append(rid) or True)
    import tools.terminal_tool_lifecycle as ttl
    monkeypatch.setattr(ttl, "cleanup_vm", lambda key, **_kw: rec["cleanup"].append(key))
    monkeypatch.setattr(dpp, "_remove_workspace", lambda lease: rec["rm"].append(lease.workspace))
    monkeypatch.setattr(dpp, "_renew_loop", lambda lease: None)
    monkeypatch.setattr(dpp, "_start_cleanup", dpp._remove_then_release)  # inline: assertions see rm + release
    dpp.COUNTERS.clear()
    return rec


def test_not_requested_is_a_no_op(pool):
    assert dpp.acquire("sa-1", None) == (None, None)
    assert dpp.acquire("sa-1", object()) == (None, None)  # a MagicMock attribute on a test double
    assert pool["admit"] == [] and dpp.counters() == {}


def test_knob_off_fails_open_loudly(pool, monkeypatch):
    monkeypatch.setattr(dpp, "mode", lambda: dpp.MODE_OFF)
    lease, note = dpp.acquire("sa-1", "pool")
    assert lease is None and "LOCALLY" in note and "pool_placement is off" in note
    assert dpp.counters() == {"fallback:knob_off": 1}
    assert pool["admit"] == []


def test_mode_reads_the_delegation_knob(monkeypatch):
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_load_config", lambda: {"pool_placement": "opt_in"})
    assert dpp.mode() == dpp.MODE_OPT_IN
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    assert dpp.mode() == dpp.MODE_OFF
    monkeypatch.setattr(dt, "_load_config", lambda: {"pool_placement": "yes"})
    assert dpp.mode() == dpp.MODE_OFF


def test_no_lease_fails_open(pool, monkeypatch):
    monkeypatch.setattr(dpp, "_admit", lambda cid, hosts: (None, "no lease (no_headroom)"))
    lease, note = dpp.acquire("sa-1", "pool")
    assert lease is None and "no_headroom" in note
    assert dpp.counters() == {"fallback:no_lease": 1}


def test_probe_failure_releases_the_lease_and_fails_open(pool, monkeypatch):
    import hermes_cli.kanban_worker_hosts as kwh

    def _boom(host, ws):
        raise RuntimeError("ssh: connect timed out")

    monkeypatch.setattr(kwh, "prepare_remote_workspace", _boom)
    lease, note = dpp.acquire("sa-1", "pool")
    assert lease is None and "probe of ace-ai failed" in note
    assert pool["release"] == ["rid-sa-1"]
    assert dpp.counters() == {"fallback:probe_failed": 1}


def test_placed_lease_carries_the_kwlb_ssh_values(pool):
    lease, note = dpp.acquire("sa-1", "pool")
    assert lease is not None and "ace-ai" in note and "kanbanw@192.0.2.16" in note
    assert lease.env_key == "delegate-pool:sa-1"
    v = lease.values
    assert v["TERMINAL_ENV"] == "ssh" and v["TERMINAL_SSH_HOST"] == "192.0.2.16"
    assert v["TERMINAL_SSH_USER"] == "kanbanw" and v["TERMINAL_CWD"] == "/srv/ws/delegate-sa-1"
    assert v["TERMINAL_SSH_SYNC_FILES"] == "false"  # nothing from the operator's home goes there
    assert v["TERMINAL_SSH_CONTROL_SCOPE"] == "dlg-sa-1"
    assert pool["probe"] == [("ace-ai", "/srv/ws/delegate-sa-1")]
    assert dpp.counters() == {"placed": 1}


def test_two_leases_for_one_child_id_share_no_key(pool, monkeypatch):
    # Prism 5a85f4228eb5: the child id is lossy (_safe_id) and short, and release() deletes the workspace on a
    # scratch root other gateways share, so every lease gets its own slug.
    monkeypatch.setattr(dpp, "_lease_slug", _REAL_LEASE_SLUG)
    a, _ = dpp.acquire("sa/1", "pool")
    b, _ = dpp.acquire("sa_1", "pool")  # same _safe_id
    c, _ = dpp.acquire("", "pool")
    leases = [a, b, c]
    for attr in ("env_key", "workspace"):
        assert len({getattr(x, attr) for x in leases}) == 3, attr
    assert len({x.values["TERMINAL_SSH_CONTROL_SCOPE"] for x in leases}) == 3
    for x in leases:  # one slug per lease: env key, workspace and control scope agree
        slug = x.env_key[len(dpp.ENV_KEY_PREFIX):]
        assert x.workspace.endswith(f"delegate-{slug}") and x.values["TERMINAL_SSH_CONTROL_SCOPE"] == f"dlg-{slug}"
    assert a.env_key.startswith("delegate-pool:sa_1-")


def test_bound_scope_and_env_key_are_the_childs_only(pool, monkeypatch):
    from tools.terminal_scope import terminal_env
    from tools.terminal_tool import _resolve_container_task_id

    monkeypatch.setenv("TERMINAL_ENV", "local")
    lease, _ = dpp.acquire("sa-1", "pool")
    outside = _resolve_container_task_id("sa-1")
    seen = {}

    def _child():
        with dpp.bound(lease):
            seen["key"] = _resolve_container_task_id("sa-1")
            seen["env"] = terminal_env("TERMINAL_ENV")
            seen["host"] = terminal_env("TERMINAL_SSH_HOST")
            # a parallel tool worker the child starts copies its context
            box = {}
            t = threading.Thread(target=contextvars.copy_context().run,
                                 args=(lambda: box.update(k=_resolve_container_task_id("x")),))
            t.start()
            t.join()
            seen["worker_key"] = box["k"]

    contextvars.copy_context().run(_child)
    assert seen == {"key": "delegate-pool:sa-1", "env": "ssh", "host": "192.0.2.16",
                    "worker_key": "delegate-pool:sa-1"}
    # the parent's context never saw any of it
    assert dpp.bound_env_key() is None
    assert _resolve_container_task_id("sa-1") == outside != "delegate-pool:sa-1"
    assert terminal_env("TERMINAL_ENV") == "local"


def test_concurrent_children_each_run_in_their_own_workspace(pool, monkeypatch, tmp_path):
    # Live proof deleg_d964266b: three pool children of one gateway session all recorded and read their cwd
    # under the parent's approval session key, so child 0 cd'ed into child 1's workspace. Real terminal_tool
    # on the local backend; each lease points TERMINAL_CWD at its own directory.
    import json

    from tools.approval_context import set_current_session_key
    from tools.terminal_tool import get_session_cwd, record_session_cwd, terminal_tool

    monkeypatch.setattr(dpp, "_workspace_for", lambda cid: str(tmp_path / f"delegate-{cid}"))
    ids = ["sa-0-aaaa", "sa-1-bbbb", "sa-2-cccc"]
    leases = {}
    for cid in ids:
        lease, _ = dpp.acquire(cid, "pool")
        (tmp_path / f"delegate-{cid}").mkdir()
        lease.values.update(TERMINAL_ENV="local")  # same scope shape, no ssh
        record_session_cwd(lease.env_key, lease.workspace)  # what _run_single_child seeds
        leases[cid] = lease
    pwds = {cid: [] for cid in ids}
    barrier = threading.Barrier(len(ids))

    def _child(cid):
        set_current_session_key("agent:main:discord:parent-session")  # children inherit the parent's key
        with dpp.bound(leases[cid]):
            for _ in range(2):
                barrier.wait()
                out = json.loads(terminal_tool("pwd", task_id=cid))
                pwds[cid].append(out["output"].strip())

    threads = [threading.Thread(target=contextvars.copy_context().run, args=(_child, cid)) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        for cid in ids:
            assert pwds[cid] == [leases[cid].workspace] * 2, (cid, pwds)
        assert len({p[-1] for p in pwds.values()}) == 3
        assert get_session_cwd("agent:main:discord:parent-session") is None  # the parent's record is untouched
    finally:
        import tools.terminal_tool as tt
        for lease in leases.values():
            dpp.release(lease)
            env = tt._active_environments.pop(lease.env_key, None)  # the fixture stubs cleanup_vm
            if env is not None:
                env.cleanup()


def test_refusal_scope_stays_refused(pool):
    from tools.terminal_scope import (TerminalPolicyRefusal, TerminalPolicyUnavailable, reset_terminal_scope,
                                      set_terminal_scope, terminal_env)
    lease, _ = dpp.acquire("sa-1", "pool")
    tok = set_terminal_scope(TerminalPolicyRefusal("profile config unreadable"))
    try:
        with dpp.bound(lease):
            with pytest.raises(TerminalPolicyUnavailable):
                terminal_env("TERMINAL_ENV")
    finally:
        reset_terminal_scope(tok)


def test_release_is_idempotent(pool):
    lease, _ = dpp.acquire("sa-1", "pool")
    dpp.release(lease)
    dpp.release(lease)
    dpp.release(None)
    assert pool["cleanup"] == ["delegate-pool:sa-1"]
    assert pool["release"] == ["rid-sa-1"]
    assert pool["rm"] == ["/srv/ws/delegate-sa-1"]


def _child_that_reports(seen):
    from tools.terminal_scope import terminal_env
    from tools.terminal_tool import _resolve_container_task_id, get_session_cwd

    def _answers(self):
        seen["key"] = _resolve_container_task_id(self._subagent_id)
        seen["cwd"] = get_session_cwd("agent:main:discord:parent-session")  # any key: the bound lease's record
        seen["host"] = terminal_env("TERMINAL_SSH_HOST")
        seen["goal"] = self.calls[-1]
        return {"final_response": "done", "completed": True, "api_calls": 1}
    return _answers


def test_run_single_child_places_and_releases_on_success(fleet_home, pool):
    import tools.delegate_tool as dt

    seen = {}
    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-pool", parent=parent, behavior=_child_that_reports(seen))
    dt._attach_placement(child, {"goal": "g", "placement": "pool"})
    entry = dt._run_single_child(0, "pool goal", child, parent)
    assert entry["status"] == "completed", entry
    assert seen["key"] == "delegate-pool:sa-0-pool" and seen["host"] == "192.0.2.16"
    assert seen["cwd"] == "/srv/ws/delegate-sa-0-pool"  # the spawn seeds the record the child's turn reads
    assert "[pool placement]" in seen["goal"] and "ace-ai" in seen["goal"]
    assert pool["release"] == ["rid-sa-0-pool"]  # released on the normal exit path
    assert dpp.bound_env_key() is None


def test_run_single_child_releases_when_the_child_raises(fleet_home, pool):
    import tools.delegate_tool as dt

    def _raise(self):
        raise RuntimeError("model exploded")

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-boom", parent=parent, behavior=_raise)
    dt._attach_placement(child, {"goal": "g", "placement": "pool"})
    entry = dt._run_single_child(0, "pool goal", child, parent)
    assert entry["status"] != "completed"
    assert pool["release"] == ["rid-sa-0-boom"]


def test_run_single_child_without_placement_is_unchanged(fleet_home, pool):
    import tools.delegate_tool as dt

    seen = {}
    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-local", parent=parent, behavior=_child_that_reports(seen))
    entry = dt._run_single_child(0, "plain goal", child, parent)
    assert entry["status"] == "completed"
    assert seen["key"] != "delegate-pool:sa-0-local" and seen["goal"] == "plain goal"
    assert pool["admit"] == [] and pool["release"] == []


def test_schema_advertises_the_field():
    import tools.delegate_tool as dt
    prop = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]["placement"]
    assert prop["enum"] == ["pool"] and "pool_placement" in prop["description"]


def test_config_default_is_off():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["delegation"]["pool_placement"] == "off"
