"""Placed kanban workers register only placement-routed tools (Browser Sandboxes B1 (b), t_a2817825).

The uid boundary of ``kanban.local_worker_uid`` covers the terminal and file tools only (they run
over ssh as the placement's user). Every tool that spawns a LOCAL process (browser launchers,
``execute_code``, stdio MCP servers, delegation, the checkpoint manager) would run as the
dispatcher's uid, so a placed worker refuses them at registration and records a
``tool_refused_by_placement`` event naming the tool and the placement.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement_tools as kpt
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli.kanban_local_worker_uid import local_host

REPO = Path(__file__).resolve().parents[2]
STUDIO_LOCAL = local_host("_kanbanw")
ACE_AI = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user="kanbanw", slots=2,
                      capacity_pct=1.0, absence="required", profiles=(), state="active",
                      enabled=True, priority=0)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setattr(kpt, "_pending", {})
    monkeypatch.setattr(kpt, "_recorded", set())
    monkeypatch.delenv(kwh.PLACEMENT_ENV, raising=False)
    monkeypatch.delenv(kpt.REFUSED_ENV, raising=False)


def _placed(host, refused=None):
    env = {}
    kwh.apply_placement(env, host, "/tmp/ws")
    if refused is not None:
        env[kpt.REFUSED_ENV] = refused
    return env


# -- the policy -----------------------------------------------------------------

@pytest.mark.parametrize("host", [STUDIO_LOCAL, ACE_AI], ids=["studio-local", "pool-host"])
@pytest.mark.parametrize("toolset", ["browser", "browser-cdp", "browser-use", "code_execution",
                                     "computer_use", "delegation", "desktop_ui", "cronjob",
                                     "some-new-toolset"])
def test_local_spawners_and_unknown_toolsets_are_refused(host, toolset):
    env = _placed(host, kpt.spawn_env_value({}))
    assert kpt.refusal_reason("t", toolset, env) is not None


@pytest.mark.parametrize("toolset", sorted(kpt.ALLOWED_TOOLSETS) + ["mcp-twozero_td"])
def test_placement_routed_and_api_toolsets_register(toolset):
    env = _placed(STUDIO_LOCAL, kpt.spawn_env_value({}))
    assert kpt.refusal_reason("t", toolset, env) is None


def test_config_can_refuse_more_never_less():
    more = kpt.spawn_env_value({"placement": {kpt.KNOB: ["web_search", "vision"]}})
    env = _placed(STUDIO_LOCAL, more)
    assert kpt.refusal_reason("web_search", "web", env) == "refused_by_config"
    assert kpt.refusal_reason("vision_analyze", "vision", env) == "refused_by_config"
    assert kpt.refusal_reason("web_extract", "web", env) is None
    # An empty list removes the named defaults, but the floor still refuses every local spawner.
    env = _placed(STUDIO_LOCAL, kpt.spawn_env_value({"placement": {kpt.KNOB: []}}))
    assert kpt.refusal_reason("browser_navigate", "browser", env) == "not_on_allow_list"
    assert kpt.refusal_reason("execute_code", "code_execution", env) == "not_on_allow_list"


@pytest.mark.parametrize("cfg", [
    {"placement": {kpt.KNOB: "browser"}},
    {"placement": {kpt.KNOB: [1, "x"]}},
    {"placement": {kpt.KNOB: [""]}},
    {"placement": "on"},
], ids=["string", "non-str-item", "blank-item", "placement-not-a-mapping"])
def test_malformed_knob_fails_closed_to_the_allow_list(cfg):
    value = kpt.spawn_env_value(cfg)
    assert value == kpt.MALFORMED
    env = _placed(STUDIO_LOCAL, value)
    assert kpt.refusal_reason("browser_navigate", "browser", env) == "not_on_allow_list"
    assert kpt.refusal_reason("terminal", "terminal", env) is None


def test_unparseable_placement_env_still_counts_as_placed():
    assert kpt.placed_host({kwh.PLACEMENT_ENV: "{not json"}) == "unknown-placement"
    assert kpt.placed_host({}) is None


def test_stdio_mcp_dropped_http_kept(monkeypatch):
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    kept = kpt.refuse_stdio_mcp_servers({
        "build123d": {"command": "uv", "args": ["run", "x"]},
        "twozero_td": {"url": "http://localhost:40404/mcp"},
    })
    assert kept == {"twozero_td": {"url": "http://localhost:40404/mcp"}}
    assert ("mcp-stdio", "local_spawner", "mcp:build123d") in kpt._recorded


def test_unplaced_process_is_untouched():
    servers = {"build123d": {"command": "uv"}}
    assert kpt.refuse_stdio_mcp_servers(servers) is servers
    assert kpt.gate_registration("browser_navigate", "browser") is False
    assert kpt.gate_feature("checkpoints") is False


# -- the dispatcher hands the policy to every placed spawn -------------------------

def test_spawn_env_defaults_to_the_named_refusals():
    assert set(json.loads(kpt.spawn_env_value({}))) == set(kpt.DEFAULT_REFUSED)
    assert set(json.loads(kpt.spawn_env_value({"placement": {}}))) == set(kpt.DEFAULT_REFUSED)


# -- events land on the worker's own run ------------------------------------------

def test_refusals_land_as_run_events(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha")
        run_id = kb.claim_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr("agent.delegation_context.is_dispatcher_owned_worker_context", lambda: True)
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    assert kpt.gate_registration("browser_navigate", "browser")
    assert kpt.gate_registration("browser_click", "browser")
    assert kpt.gate_registration("execute_code", "code_execution")
    assert not kpt.gate_registration("terminal", "terminal")
    assert kpt.flush_events() == 2
    assert kpt.flush_events() == 0
    with kb.connect_closing() as conn:
        rows = conn.execute("SELECT run_id, payload FROM task_events WHERE task_id=? AND kind=?",
                            (tid, kpt.EVENT)).fetchall()
    payloads = sorted((json.loads(p) for _, p in rows), key=lambda p: p["toolset"])
    assert {r for r, _ in rows} == {run_id}
    assert payloads == [
        {"placement": "studio-local", "toolset": "browser", "tools": ["browser_click", "browser_navigate"],
         "reason": "not_on_allow_list"},
        {"placement": "studio-local", "toolset": "code_execution", "tools": ["execute_code"],
         "reason": "not_on_allow_list"},
    ]


# -- E2E: a real worker interpreter, real tool discovery ----------------------------

_PROBE = r"""
import json, logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
from hermes_cli import kanban_worker_hosts  # noqa: F401  (boot record, as main() takes it)
import model_tools
from tools.registry import registry
from tools import mcp_tool_config
names = sorted(e.name + "@" + e.toolset for e in registry.get_all_entries())
mcp = sorted(mcp_tool_config._load_mcp_config())
print(json.dumps({"tools": names, "mcp": mcp}))
"""


def _discover(tmp_path, placed):
    home = tmp_path / ("placed" if placed else "local")
    home.mkdir()
    (home / "config.yaml").write_text(
        "mcp_servers:\n"
        "  build123d:\n    command: uv\n    args: [run, x]\n"
        "  twozero_td:\n    url: http://localhost:40404/mcp\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "KANBAN_", "TERMINAL_"))}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(REPO))
    if placed:
        kwh.apply_placement(env, STUDIO_LOCAL, str(tmp_path))
        env[kpt.REFUSED_ENV] = kpt.spawn_env_value({})
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


def test_e2e_placed_worker_registers_no_local_spawner(tmp_path):
    out, log = _discover(tmp_path, placed=True)
    toolsets = {t.split("@", 1)[1] for t in out["tools"]}
    assert toolsets <= kpt.ALLOWED_TOOLSETS, toolsets - kpt.ALLOWED_TOOLSETS
    assert {"terminal@terminal", "read_file@file", "kanban_show@kanban"} <= set(out["tools"])
    assert out["mcp"] == ["twozero_td"]
    assert "tool_refused_by_placement: browser_navigate (toolset browser) on placement studio-local" in log
    assert "tool_refused_by_placement: execute_code (toolset code_execution) on placement studio-local" in log
    assert "tool_refused_by_placement: mcp:build123d (toolset mcp-stdio) on placement studio-local" in log


def test_e2e_unplaced_worker_keeps_every_tool(tmp_path):
    out, log = _discover(tmp_path, placed=False)
    assert "browser_navigate@browser" in out["tools"]
    assert "execute_code@code_execution" in out["tools"]
    assert out["mcp"] == ["build123d", "twozero_td"]
    assert "tool_refused_by_placement" not in log
