"""Deliberate single-sub worker pin: ``--pin-sub "<reason>"`` (t_957ca870).

#1116 (t_141135aa) refused EVERY claude-apx-N / claude-bpx-N route because
cards pinned to claude-apx-0 (Ace's personal Mac sub, alias claude-api-proxy)
put workers on his personal sub. The hole was sub 0 + the aliases, not pinning.
This suite locks the opt-in:

* without --pin-sub every pin is still refused (the #1116 suite stays green);
* with --pin-sub an ADMITTED sub N >= 1 is pinnable, end to end: stored, shown
  as ``[PIN ...]``, dispatched on that sub with ``source=pin``, and the card's
  model AND effort ride the pinned route into the spawn argv;
* sub 0 (Ace's own sub, reserved out of every pool) is pinnable WITH the flag,
  as a last resort (Ace 2026-09-27 08:45);
* the pre-rename aliases and subs the usage registry does not admit are
  refused even WITH the flag;
* a capped pinned sub WAITS (no silent profile fallback); only
  --pin-sub-fallback lets it ride its family pool.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_provider_health as ph
from hermes_cli.model_policy import (
    SUB_PIN_COMMENT_PREFIX,
    pinned_sub_provider_error,
    sub_pin_admission_error,
)

OPUS = "claude-opus-5-5"
REGISTRY = {"subs": [
    {"key": "local", "enabled": True, "pool_enabled": False},
    {"key": "sub-vps-3", "enabled": True, "pool_enabled": True},
    # burn-in, not pooled: bpx serves it, apx is off (the shape of sub-vps-24)
    {"key": "sub-vps-24", "enabled": True, "pool_enabled": False,
     "burn_in_until": "2099-01-01T00:00:00Z"},
    {"key": "sub-vps-9", "enabled": False, "pool_enabled": True},
]}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "usage-registry.json"
    path.write_text(json.dumps(REGISTRY), encoding="utf-8")
    monkeypatch.setattr(ph, "_usage_registry_path", lambda: path)
    return path


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, registry):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "normal")
    getattr(ph, "_PROBE_STATE", {}).clear()
    kb.init_db()
    (home / "config.yaml").write_text(
        "providers:\n"
        "  claude-bpx-24:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n"
        "  claude-apx-24:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n"
        "  claude-bpx-3:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n"
        "  claude-bpx-0:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n"
        "  claude-api-proxy:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n"
        "  claude-bpr:\n    base_url: http://127.0.0.1:9/v1\n    api_key: t\n",
        encoding="utf-8",
    )
    yield home
    getattr(ph, "_PROBE_STATE", {}).clear()


def _cli(cmd: str, capsys) -> str:
    out = kc.run_slash(cmd)
    cap = capsys.readouterr()
    return (out or "") + cap.out + cap.err


def _created_id(text: str) -> str:
    import re

    return re.search(r"t_[0-9a-f]+", text).group(0)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", [
    "claude-bpx-24", "claude-bpx-3", "claude-apx-3", "claude-apx-0", "claude-bpx-0",
])
def test_admitted_sub_is_pinnable_with_reason(registry, provider):
    assert pinned_sub_provider_error(OPUS, provider) is not None  # #1116 default
    assert pinned_sub_provider_error(OPUS, provider, pin_sub_reason="why") is None
    assert pinned_sub_provider_error(f"{provider}/{OPUS}", None, pin_sub_reason="why") is None


@pytest.mark.parametrize("provider", [
    "claude-api-proxy", "claude-bridge", "claude-api-proxy-f3",
    "claude-proxy-f3", "claude-bridge-f7", "claude-subscription-proxy",
])
def test_aliases_refused_even_with_flag(registry, provider):
    err = pinned_sub_provider_error(OPUS, provider, pin_sub_reason="please")
    assert err and provider in err


@pytest.mark.parametrize("provider,needle", [
    ("claude-bpx-77", "not in the usage registry"),   # unregistered
    ("claude-bpx-9", "disabled"),                      # enabled != true
    ("claude-apx-24", "burn-in"),                      # apx off in burn-in
])
def test_unadmitted_sub_refused_even_with_flag(registry, provider, needle):
    err = pinned_sub_provider_error(OPUS, provider, pin_sub_reason="please")
    assert err and needle in err, err


def test_unreadable_registry_fails_closed(registry):
    registry.write_text("{not json", encoding="utf-8")
    assert "fail closed" in sub_pin_admission_error("claude-bpx-24")


def test_default_refusal_names_the_pool_and_the_opt_in(registry):
    err = pinned_sub_provider_error(OPUS, "claude-bpx-24")
    assert "claude-bpr" in err and "--pin-sub" in err
    assert "never pin" not in err


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def test_create_task_stores_pin_and_audits_it(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="daedalus-opus",
                             model_override=OPUS, provider_override="claude-bpx-24",
                             pin_sub_reason="idle full bars")
        task = kb.get_task(conn, tid)
        assert (task.provider_override, task.pin_sub_reason, task.pin_sub_fallback) == (
            "claude-bpx-24", "idle full bars", False)
        bodies = [c.body for c in kb.list_comments(conn, tid)]
        assert any(b.startswith(SUB_PIN_COMMENT_PREFIX) and "claude-bpx-24" in b for b in bodies)


def test_create_task_pin_sub_on_pool_route_is_refused(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="single-sub route"):
            kb.create_task(conn, title="x", assignee="a", model_override=OPUS,
                           provider_override="claude-bpr", pin_sub_reason="r")
        with pytest.raises(ValueError, match="requires --pin-sub"):
            kb.create_task(conn, title="x", assignee="a", model_override=OPUS,
                           provider_override="claude-bpx-24", pin_sub_fallback=True)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_a_later_unpinned_route_clears_the_pin(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        kb.set_model_override(conn, tid, OPUS, "claude-bpx-24", pin_sub_reason="r",
                              pin_sub_fallback=True)
        assert kb.get_task(conn, tid).pin_sub_fallback is True
        kb.set_model_override(conn, tid, OPUS, "claude-bpr")
        task = kb.get_task(conn, tid)
        assert (task.pin_sub_reason, task.pin_sub_fallback) == (None, False)
        kb.set_model_override(conn, tid, OPUS, "claude-bpx-24", pin_sub_reason="r")
        kb.set_task_model(conn, tid, OPUS)
        assert kb.get_task(conn, tid).pin_sub_reason is None


def test_lane_pin_requires_flag_and_admission(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="claude-bpr"):
            kb.set_lane_model_override(conn, provider="claude-bpx-24", model=OPUS,
                                       expires_at=10**10, reason="r", assignee="a")
        with pytest.raises(ValueError, match="pre-rename alias"):
            kb.set_lane_model_override(conn, provider="claude-bridge", model=OPUS,
                                       expires_at=10**10, reason="r", assignee="a",
                                       pin_sub_reason="x")
        lane = kb.set_lane_model_override(conn, provider="claude-bpx-24", model=OPUS,
                                          expires_at=10**10, reason="r", assignee="a",
                                          pin_sub_reason="lane test")
        assert lane.pin_sub_reason == "lane test"
        assert kb.get_lane_model_override(conn, assignee="a").pin_sub_reason == "lane test"


# ---------------------------------------------------------------------------
# CLI (the acceptance command shape) + visibility
# ---------------------------------------------------------------------------


def test_cli_set_model_pin_accepted_with_flag_refused_without(kanban_home, capsys):
    tid = _created_id(_cli("create 'scratch' --assignee daedalus-opus", capsys))
    refused = _cli(f"set-model {tid} --provider claude-bpx-24 --model {OPUS}", capsys)
    assert "pins one Claude subscription" in refused
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).provider_override is None

    out = _cli(f"set-model {tid} --provider claude-bpx-24 --model {OPUS} --effort high "
               "--pin-sub 'test'", capsys)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert (task.provider_override, task.model_override, task.reasoning_effort,
            task.pin_sub_reason) == ("claude-bpx-24", OPUS, "high", "test"), out

    assert "[PIN claude-bpx-24: test]" in _cli(f"show {tid}", capsys)
    assert "[PIN claude-bpx-24: test]" in _cli("list --all", capsys)
    pins = _cli("pins", capsys)
    assert tid in pins and "[PIN claude-bpx-24: test]" in pins


def test_cli_sub_zero_pinnable_only_with_flag(kanban_home, capsys):
    tid = _created_id(_cli("create 'scratch' --assignee daedalus-opus", capsys))
    out = _cli(f"set-model {tid} --provider claude-bpx-0 --model {OPUS}", capsys)
    assert "pins one Claude subscription" in out
    _cli(f"set-model {tid} --provider claude-bpx-0 --model {OPUS} --pin-sub 'last resort'",
         capsys)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert (task.provider_override, task.pin_sub_reason) == ("claude-bpx-0", "last resort")


def test_cli_alias_refused_with_flag(kanban_home, capsys):
    tid = _created_id(_cli("create 'scratch' --assignee daedalus-opus", capsys))
    out = _cli(f"set-model {tid} --provider claude-api-proxy --model {OPUS} --pin-sub 'x'",
               capsys)
    assert "pre-rename alias" in out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).pin_sub_reason is None


def test_pins_stale_lint_exit_code(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a", model_override=OPUS,
                             provider_override="claude-bpx-24", pin_sub_reason="r")
    import argparse

    assert kc._cmd_pins(argparse.Namespace(as_json=False, stale_hours=None)) == 0
    assert kc._cmd_pins(argparse.Namespace(as_json=False, stale_hours=1000)) == 0
    assert kc._cmd_pins(argparse.Namespace(as_json=False, stale_hours=0)) == 1
    out = capsys.readouterr().out
    assert tid in out and "STALE" in out
    assert kc._cmd_pins(argparse.Namespace(as_json=True, stale_hours=0)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] == [tid] and payload["pins"][0]["reason"] == "r"


# ---------------------------------------------------------------------------
# Dispatcher: pin honored end to end; governors still apply
# ---------------------------------------------------------------------------


def _spawner(seen):
    def spawn(task, workspace, **kwargs):
        seen.append((task.id, task.provider_override, task.model_override,
                     task.reasoning_effort))
        return 777777
    return spawn


def _pinned_card(conn, *, fallback=False):
    return kb.create_task(conn, title="pinned", assignee="a", model_override=OPUS,
                          provider_override="claude-bpx-24", reasoning_effort="xhigh",
                          pin_sub_reason="acceptance", pin_sub_fallback=fallback)


def test_dispatch_honors_pin_route_effort_and_source(kanban_home, monkeypatch):
    monkeypatch.setattr(ph, "capped_provider", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        tid = _pinned_card(conn)
        seen = []
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(seen))
    assert seen == [(tid, "claude-bpx-24", OPUS, "xhigh")]
    assert res.spawn_route_sources[tid] == "pin"


def test_spawn_argv_carries_pinned_provider_model_and_effort(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task = kb.get_task(conn, _pinned_card(conn))
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    captured = {}

    class FakeProc:
        pid = 4247

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = kanban_home / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    cmd = captured["cmd"]
    assert cmd[cmd.index("-m") + 1] == OPUS
    assert cmd[cmd.index("--provider") + 1] == "claude-bpx-24"
    assert cmd[cmd.index("--reasoning") + 1] == "xhigh"


def _cap_pinned_sub(monkeypatch):
    def capped(task, *a, **k):
        if task.provider_override == "claude-bpx-24":
            return {"reason": "provider_capped", "provider": "claude-bpx-24"}
        return None

    monkeypatch.setattr(ph, "capped_provider", capped)
    # A healthy profile rung exists: a pin must NOT silently take it.
    monkeypatch.setattr(ph, "available_profile_fallback",
                        lambda *a, **k: ("claude-opus-5", "claude-apr"))


def test_capped_pin_waits_by_default(kanban_home, monkeypatch):
    _cap_pinned_sub(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _pinned_card(conn)
        seen = []
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(seen))
        events = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='deferred'", (tid,))]
    assert seen == []
    assert (tid, "provider_capped") in res.respawn_guarded
    assert events and events[-1]["pin"] == "claude-bpx-24"
    assert events[-1]["pin_fallback"] == "wait"


def test_capped_pin_with_fallback_rides_family_pool(kanban_home, monkeypatch):
    _cap_pinned_sub(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _pinned_card(conn, fallback=True)
        seen = []
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(seen))
    assert seen == [(tid, "claude-bpr", OPUS, "xhigh")]
    assert res.spawn_route_sources[tid].startswith("dispatch-fallback(capped pin")
