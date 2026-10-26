"""The kanban-runtime unit runner (``hermes -p <profile> delegation-unit``), option C P2 §5.3.

Spec: docs/dev/delegate-background-on-kanban-runtime.md §5.3 and §9 P2 (card t_da065db9). The
runner runs in-process here with its card on a real sandboxed ``delegations`` board; the model is
replaced by ``runner_factory`` so a test can count model calls.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools import async_delegation_kanban as akb
from tools import async_delegation_store as store
from tools import delegation_unit_worker as duw

BOOT = "100:1.0"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "delegations")
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.create_board("delegations")
    ad._reset_for_tests()
    monkeypatch.setattr(duw, "_isolate_process_checkpoint", lambda task_id: None)
    yield root
    ad._reset_for_tests()


def _spec(cwd):
    return {
        "profile": "default",
        "source": {"kind": "single", "tasks": [{"goal": "g", "role": "leaf"}], "shared_context": None},
        "execution": {"model": "m", "provider": "p", "toolsets": ["file"], "max_iterations": 5,
                      "workspace_hint": str(cwd), "runtime": "kanban",
                      "route": {"requested_provider": None, "model": "m", "explicit_base_url": None}},
        "route": {"session_key": "k", "parent_session_id": "parent-1", "platform": "telegram",
                  "profile": "default"},
    }


@pytest.fixture
def unit(home, tmp_path, monkeypatch):
    """A dispatched unit: durable record + its claimed running card; the env a spawned runner has."""
    from hermes_cli import kanban_db as kb

    rec, _ = store.persist_dispatch(delegation_id="deleg_u1", durable_spec=_spec(tmp_path), boot_id=BOOT,
                                    dispatched_at=time.time())
    attempt = rec["attempt"]["attempt_id"]
    with kb.connect_closing(board="delegations") as conn:
        tid = kb.create_task(conn, title="deleg deleg_u1: g", assignee="default", workspace_kind="dir",
                             workspace_path=str(tmp_path), board="delegations",
                             body=akb.card_body("deleg_u1", attempt, home))
        claimed = kb.claim_task(conn, tid)
    store.set_kanban_block("deleg_u1", attempt, {"board": "delegations", "task_id": tid})
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock or "")
    return SimpleNamespace(did="deleg_u1", attempt=attempt, task_id=tid)


class Model:
    """Counts model calls; ``block`` holds the call until released (to cancel mid-run)."""

    def __init__(self, result=None, block=None, raises=None):
        self.calls, self.result, self.block, self.raises = 0, result, block, raises
        self.started = threading.Event()

    def factory(self, record, parent):
        def runner():
            self.calls += 1
            self.started.set()
            if self.block is not None:
                self.block.wait(30)
            if self.raises:
                raise self.raises
            return self.result or {"status": "completed", "summary": "unit answer", "api_calls": 1}
        return runner


def _parent(record):
    return SimpleNamespace(interrupt=lambda msg: None)


def _record(did):
    with store.locked_registry(write=False) as reg:
        return json.loads(json.dumps(reg["records"][did]))


def _card(task_id):
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board="delegations") as conn:
        return kb.get_task(conn, task_id)


def test_runner_writes_the_terminal_unqueued_and_completes_the_card(unit):
    model = Model()
    assert duw.run(runner_factory=model.factory, parent_factory=_parent) == 0
    assert model.calls == 1
    rec = _record(unit.did)
    assert rec["state"] == "done"
    terminal = [e for e in rec["outbox"] if e["type"] == "async_delegation"]
    assert len(terminal) == 1 and terminal[0]["queued_boot_id"] is None
    assert terminal[0]["payload"]["summary"] == "unit answer"
    assert _card(unit.task_id).status == "done"


def test_superseded_card_runner_exits_at_step_2_with_zero_model_calls(unit):
    assert store.supersede_attempt(unit.did, unit.attempt) is not None
    model = Model()
    assert duw.run(runner_factory=model.factory, parent_factory=_parent) == 0
    assert model.calls == 0
    assert _card(unit.task_id).status == "done"
    assert _record(unit.did)["state"] == "running", "the in-process attempt still owns the record"


def test_unit_cancelled_before_the_runner_reads_it_makes_zero_model_calls(unit):
    assert store.cancel_matching(parent_session_id="parent-1", reason="/stop") == 1
    model = Model()
    built = []
    assert duw.run(runner_factory=model.factory, parent_factory=lambda r: built.append(r)) == 0
    assert model.calls == 0 and built == [], "no parent, no model call"
    assert _record(unit.did)["state"] == "cancelled"
    assert not [e for e in _record(unit.did)["outbox"] if e["type"] == "async_delegation"]


def test_card_archived_mid_run_interrupts_and_writes_nothing(unit, monkeypatch):
    monkeypatch.setattr(duw, "CARD_POLL_SECONDS", 0.1)
    gate = threading.Event()
    interrupted = []
    model = Model(block=gate)

    def parent(record):
        return SimpleNamespace(interrupt=lambda msg: (interrupted.append(msg), gate.set()))

    beat_cls = duw._Beat
    monkeypatch.setattr(duw, "_Beat", lambda tid, cb: beat_cls(tid, cb, poll=0.1))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("rc", duw.run(runner_factory=model.factory,
                                                                     parent_factory=parent)))
    t.start()
    assert model.started.wait(10)
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board="delegations") as conn:
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (unit.task_id,))
        conn.commit()
    t.join(30)
    assert out.get("rc") == 0 and interrupted
    rec = _record(unit.did)
    assert rec["state"] == "running" and rec["outbox"] == [], "a cancelled unit writes nothing"


def test_exception_fails_the_attempt_with_the_traceback_and_completes_the_card(unit):
    model = Model(raises=RuntimeError("provider exploded"))
    assert duw.run(runner_factory=model.factory, parent_factory=_parent) == 0
    rec = _record(unit.did)
    assert rec["state"] == "failed"
    err = [e for e in rec["outbox"] if e["type"] == "async_delegation"][0]["payload"]["error"]
    assert "provider exploded" in err and "Traceback" in err
    assert _card(unit.task_id).status == "done"


def test_parent_build_failure_is_delivered_as_an_error(unit):
    def broken(record):
        raise ValueError("no credentials for custom:x")

    assert duw.run(runner_factory=Model().factory, parent_factory=broken) == 0
    assert _record(unit.did)["state"] == "failed"


def test_without_a_card_env_the_runner_refuses(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert duw.run(runner_factory=Model().factory, parent_factory=_parent) == 2


def test_main_never_exits_with_a_requeue_code(monkeypatch):
    monkeypatch.setattr(duw, "run", lambda: (_ for _ in ()).throw(RuntimeError("setup")))
    assert duw.main() == 0


def test_heartbeat_thread_beats_independent_of_child_activity(unit, monkeypatch):
    from hermes_cli import kanban_db as kb

    gate = threading.Event()
    model = Model(block=gate)
    beat_cls = duw._Beat
    monkeypatch.setattr(duw, "_Beat", lambda tid, cb: beat_cls(tid, cb, beat=0.2, poll=0.1))
    t = threading.Thread(target=lambda: duw.run(runner_factory=model.factory, parent_factory=_parent))
    t.start()
    assert model.started.wait(10)
    time.sleep(1.0)
    gate.set()
    t.join(30)
    with kb.connect_closing(board="delegations") as conn:
        beats = [e for e in kb.list_events(conn, unit.task_id) if e.kind == "heartbeat"]
    assert len(beats) >= 2


def test_build_parent_resolves_the_recorded_route_with_target_model(unit, monkeypatch):
    seen = {}

    def fake_resolve(**kw):
        seen.update(kw)
        return {"api_key": "sk-test-not-persisted", "base_url": "http://fake/v1", "provider": "custom",
                "requested_provider": kw.get("requested"), "api_mode": "chat_completions"}

    import sys
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    built = {}

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    record = _record(unit.did)
    record["execution"]["route"] = {"requested_provider": "custom:named", "model": "gpt-x",
                                    "explicit_base_url": None}
    duw.build_parent(record)
    assert seen == {"requested": "custom:named", "target_model": "gpt-x", "explicit_base_url": None}
    assert built["model"] == "gpt-x" and built["requested_provider"] == "custom:named"
    assert built["skip_memory"] is True and built["enabled_toolsets"] == ["file"]
    # The runner adds no provider pin derived from the route (children inherit the parent's routing).
    assert "override_provider" not in built


def test_hidden_subcommand_is_registered_and_suppressed():
    import argparse

    from hermes_cli.subcommands.delegation_unit import build_delegation_unit_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("chat", help="Chat")
    build_delegation_unit_parser(sub)
    args = parser.parse_args(["delegation-unit"])
    assert args.command == "delegation-unit" and callable(args.func)
    text = parser.format_help()
    assert "chat" in text and "delegation-unit" not in text


def test_runner_registry_read_is_one_locked_read(unit, monkeypatch):
    reads = []
    real = store.locked_registry

    def counting(*a, **kw):
        reads.append(kw.get("write", True))
        return real(*a, **kw)

    monkeypatch.setattr(store, "locked_registry", counting)
    gate = threading.Event()
    model = Model(block=gate)
    t = threading.Thread(target=lambda: duw.run(runner_factory=model.factory, parent_factory=_parent))
    t.start()
    assert model.started.wait(10)
    assert reads == [False], "one read-only registry read before the terminal write, no polling"
    gate.set()
    t.join(30)



def _build_with(unit, monkeypatch, execution_extra, cfg):
    seen, built = {}, {}

    def fake_resolve(**kw):
        seen.update(kw)
        return {"api_key": "parent-provider-key", "base_url": "http://fake/v1", "provider": "custom",
                "requested_provider": kw.get("requested"), "api_mode": "chat_completions",
                "credential_pool": object()}

    import sys
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    record = _record(unit.did)
    record["execution"].update(execution_extra)
    duw.build_parent(record)
    return built


def test_model_only_delegation_override_reaches_the_children(unit, monkeypatch):
    """Prism 235939999e6d: inherited route + delegation.model -> the parent carries the child model."""
    built = _build_with(unit, monkeypatch, {
        "model": "child-model", "credential_ref": {"source": "provider", "inherited_route": True},
        "route": {"requested_provider": "custom:x", "model": "parent-model", "explicit_base_url": None}}, {})
    assert built["model"] == "child-model"


def test_pinned_route_keeps_the_recorded_parent_model(unit, monkeypatch):
    built = _build_with(unit, monkeypatch, {
        "model": "child-model", "credential_ref": {"source": "provider", "provider": "openrouter"},
        "route": {"requested_provider": "custom:x", "model": "parent-model", "explicit_base_url": None}}, {})
    assert built["model"] == "parent-model"


def test_direct_endpoint_uses_the_current_delegation_api_key(unit, monkeypatch):
    """Prism 467e6bac5ed5: delegation.base_url + delegation.api_key -> that key, not the parent's."""
    built = _build_with(unit, monkeypatch, {
        "credential_ref": {"source": "delegation_config", "parent_provider": "openrouter"},
        "route": {"requested_provider": "openrouter", "model": "m",
                  "explicit_base_url": "https://delegate.example/v1"}},
        {"delegation": {"api_key": "delegation-key-B"}})
    assert built["api_key"] == "delegation-key-B" and built["credential_pool"] is None
    assert "delegation-key-B" not in json.dumps(_record(unit.did)), "never persisted (I4)"


def test_provider_route_keeps_the_resolved_key(unit, monkeypatch):
    built = _build_with(unit, monkeypatch, {"credential_ref": {"source": "provider"}},
                        {"delegation": {"api_key": "unused"}})
    assert built["api_key"] == "parent-provider-key"


# ── shell hooks: the unit runner is its own process, so it arms them itself ──
def test_the_unit_runner_arms_the_profiles_shell_hooks_before_building_the_parent(unit, home, tmp_path,
                                                                                 monkeypatch):
    """Red without the run() registration: the gateway's per-home plugin manager is not inherited by
    the spawned runner, so the unit's tool calls ran with no pre_tool_call policy."""
    import shlex
    import sys

    import hermes_yaml as yaml
    from agent import shell_hooks
    from hermes_cli import plugins

    script = tmp_path / "guard.py"
    script.write_text('import json\nprint(json.dumps({"decision":"block","reason":"unit-policy"}))\n')
    (home / "config.yaml").write_text(yaml.safe_dump({"hooks_auto_accept": True, "hooks": {"pre_tool_call": [
        {"command": shlex.join([sys.executable, str(script)]), "matcher": "write_file", "fail_closed": True}]}}))
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    plugins._reset_plugin_managers_for_tests()
    shell_hooks.reset_for_tests()
    seen = {}

    def _build(record):
        seen["block"] = plugins.get_pre_tool_call_block_message("write_file", {"path": "x", "content": "y"})
        return _parent(record)

    try:
        assert duw.run(runner_factory=Model().factory, parent_factory=_build) == 0
    finally:
        plugins._reset_plugin_managers_for_tests()
        shell_hooks.reset_for_tests()

    assert seen["block"] == "unit-policy", "the unit's tool calls would run with no shell-hook policy"


def test_direct_endpoint_without_a_delegation_key_inherits_the_parent_own_key(unit, monkeypatch):
    """36ceed09ccf5: delegation.base_url set, delegation.api_key unset -> the parent's own key (as
    in-process), resolved on the parent's route, never against the delegation endpoint."""
    import sys
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as rp

    calls = []

    def fake_resolve(**kw):
        calls.append(kw)
        key = "endpoint-resolved-key" if kw.get("explicit_base_url") else "parent-own-key"
        return {"api_key": key, "base_url": kw.get("explicit_base_url") or "https://openrouter.ai/api/v1",
                "provider": "custom", "requested_provider": kw.get("requested"), "api_mode": "chat_completions"}

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr(config_mod, "load_config", lambda: {"delegation": {"base_url": "https://delegate.example/v1"}})
    built = {}

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    record = _record(unit.did)
    record["execution"].update({
        "credential_ref": {"source": "delegation_config", "parent_provider": "openrouter"},
        "route": {"requested_provider": "openrouter", "model": "m",
                  "explicit_base_url": "https://delegate.example/v1"}})
    duw.build_parent(record)
    assert built["api_key"] == "parent-own-key"
    assert built["base_url"] == "https://delegate.example/v1", "the endpoint stays the delegation's"
    assert any(not c.get("explicit_base_url") and c.get("requested") == "openrouter" for c in calls)


def test_url_only_custom_parent_keeps_the_resolved_key_when_the_url_less_lookup_fails(unit, monkeypatch):
    """Prism 1b48ed1b0fa3: requested=custom with only a URL -> the URL-less own-key lookup raises; the
    key the first (URL) resolution found is kept and the parent is still built."""
    import sys
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as rp

    def fake_resolve(**kw):
        if not kw.get("explicit_base_url"):
            raise RuntimeError("custom provider has no endpoint configured")
        return {"api_key": "url-resolved-key", "base_url": kw["explicit_base_url"], "provider": "custom",
                "requested_provider": kw.get("requested"), "api_mode": "chat_completions"}

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr(config_mod, "load_config", lambda: {"delegation": {"base_url": "http://lab/v1"}})
    built = {}

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    record = _record(unit.did)
    record["execution"].update({
        "credential_ref": {"source": "delegation_config", "parent_provider": "custom"},
        "route": {"requested_provider": "custom", "model": "m", "explicit_base_url": "http://lab/v1"}})
    duw.build_parent(record)
    assert built["api_key"] == "url-resolved-key" and built["base_url"] == "http://lab/v1"



def _build_on_route(monkeypatch, unit, fake_resolve, route):
    import sys
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr(config_mod, "load_config", lambda: {"delegation": {"base_url": "http://deleg/v1"}})
    built = {}

    class FakeAgent:
        def __init__(self, **kw):
            built.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    record = _record(unit.did)
    record["execution"].update({"credential_ref": {"source": "delegation_config", "parent_provider": "custom"},
                                "route": route})
    duw.build_parent(record)
    return built


def test_anonymous_custom_parent_key_is_resolved_on_its_url_never_the_default_endpoint(unit, monkeypatch):
    """Prism 1b48ed1b0fa3: the profile has a DEFAULT custom endpoint; the parent ran on another URL ->
    its key is resolved on that URL, the default endpoint's key is never sent."""
    def fake_resolve(**kw):
        url = kw.get("explicit_base_url")
        key = {"http://parent/v1": "parent-url-key", "http://deleg/v1": "deleg-url-key"}.get(url, "default-endpoint-key")
        return {"api_key": key, "base_url": url or "http://default/v1", "provider": "custom",
                "requested_provider": kw.get("requested"), "api_mode": "chat_completions"}

    built = _build_on_route(monkeypatch, unit, fake_resolve, {
        "requested_provider": "custom", "model": "m", "explicit_base_url": "http://deleg/v1",
        "parent_base_url": "http://parent/v1"})
    assert built["api_key"] == "parent-url-key"
    assert built["base_url"] == "http://deleg/v1"


def test_anonymous_custom_parent_with_no_url_keyed_credential_fails_the_unit(unit, monkeypatch):
    """Prism 1b48ed1b0fa3: no credential for the parent's URL -> a clear error, no substitute key."""
    def fake_resolve(**kw):
        url = kw.get("explicit_base_url")
        key = "" if url == "http://parent/v1" else "default-endpoint-key"
        return {"api_key": key, "base_url": url or "http://default/v1", "provider": "custom",
                "requested_provider": kw.get("requested"), "api_mode": "chat_completions"}

    with pytest.raises(RuntimeError, match="http://parent/v1"):
        _build_on_route(monkeypatch, unit, fake_resolve, {
            "requested_provider": "custom", "model": "m", "explicit_base_url": "http://deleg/v1",
            "parent_base_url": "http://parent/v1"})


def test_parent_route_records_the_anonymous_parent_url_beside_a_delegation_endpoint():
    """Prism 1b48ed1b0fa3: delegation.base_url set + anonymous custom parent -> the parent's own URL is
    recorded so the worker can key its credential on it."""
    parent = SimpleNamespace(requested_provider="custom", provider="custom", model="m", base_url="http://parent/v1")
    route = akb.parent_route(parent, {"base_url": "http://deleg/v1"})
    assert route["explicit_base_url"] == "http://deleg/v1" and route["parent_base_url"] == "http://parent/v1"
