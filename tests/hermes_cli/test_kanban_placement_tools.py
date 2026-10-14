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
    more = kpt.spawn_env_value({"placement": {kpt.KNOB: ["x_search", "image_gen"]}})
    env = _placed(STUDIO_LOCAL, more)
    assert kpt.refusal_reason("x_search", "x_search", env) == "refused_by_config"
    assert kpt.refusal_reason("image_generate", "image_gen", env) == "refused_by_config"
    assert kpt.refusal_reason("ha_call_service", "homeassistant", env) is None
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


def test_failed_audit_write_is_kept_for_the_next_flush(tmp_path, monkeypatch):
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
    assert kpt.gate_registration("execute_code", "code_execution")
    real_connect = kb.connect

    def broken_connect(*a, **k):
        raise OSError("database is locked")

    monkeypatch.setattr(kb, "connect", broken_connect)
    assert kpt.flush_events() == 0
    monkeypatch.setattr(kb, "connect", real_connect)
    assert kpt.flush_events() == 1
    assert kpt.flush_events() == 0
    with kb.connect_closing() as conn:
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                            (tid, kpt.EVENT)).fetchall()
    assert [json.loads(p)["tools"] for (p,) in rows] == [["execute_code"]]


def test_checkpoints_follow_the_boot_record_when_env_is_cleared(monkeypatch):
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    kwh.capture_boot_placement()
    monkeypatch.delenv(kwh.PLACEMENT_ENV)
    assert kpt.at_agent_init(True) is False
    assert ("checkpoints", "local_spawner", "checkpoints") in kpt._recorded


def test_checkpoints_untouched_when_unplaced():
    assert kpt.at_agent_init(True) is True
    assert kpt.at_agent_init(False) is False
    assert not kpt._recorded


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


# -- E2E: the process tree, not the registry ------------------------------------------

_TREE_PROBE = r"""
import json, os, subprocess, sys, threading, time
from hermes_cli import kanban_worker_hosts  # noqa: F401
import model_tools  # noqa: F401
from tools import mcp_tool_discovery
import importlib.util
if importlib.util.find_spec("mcp") is None:
    print("null")
    sys.exit(0)

me = os.getpid()
seen = {}
stop = threading.Event()

def walk():
    while not stop.is_set():
        out = subprocess.run(["ps", "-axo", "uid=,pid=,ppid=,args="], capture_output=True, text=True).stdout
        rows = [l.split(None, 3) for l in out.splitlines() if l.strip()]
        kids, tree = {}, {me}
        for r in rows:
            kids.setdefault(int(r[2]), []).append(r)
        stack = [me]
        while stack:
            for r in kids.get(stack.pop(), []):
                pid = int(r[1])
                if pid not in tree:
                    tree.add(pid)
                    stack.append(pid)
                    if not (len(r) > 3 and r[3].startswith("ps ")):
                        seen[pid] = r[3] if len(r) > 3 else ""
        time.sleep(0.1)

t = threading.Thread(target=walk, daemon=True)
t.start()
try:
    mcp_tool_discovery.discover_mcp_tools()
except Exception as exc:  # noqa: BLE001
    print("discover failed:", exc, file=sys.stderr)
time.sleep(1.5)
stop.set()
t.join()
print(json.dumps(sorted(seen.values())))
"""


def _tree(tmp_path, placed):
    home = tmp_path / ("placed" if placed else "local")
    home.mkdir()
    marker = f"{tmp_path}/stdio-mcp-marker"
    (home / "config.yaml").write_text(
        "mcp_servers:\n"
        f"  stdio_probe:\n    command: /bin/sh\n    args: [-c, 'sleep 30; : {marker}']\n"
        "    timeout: 3\n    connect_timeout: 3\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "KANBAN_", "TERMINAL_"))}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(REPO))
    if placed:
        kwh.apply_placement(env, STUDIO_LOCAL, str(tmp_path))
        env[kpt.REFUSED_ENV] = kpt.spawn_env_value({})
    proc = subprocess.run([sys.executable, "-c", _TREE_PROBE], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    if seen is None:
        pytest.skip("mcp SDK not installed in this interpreter")
    return seen, marker


def _not_provenance(cmds):
    # version_info's import-time `git rev-parse/describe` on the install dir is a read-only
    # provenance read, not a tool (Apollo's arm-7 wording); everything else is a local spawn.
    return [c for c in cmds if not (c.lstrip("(").startswith("git") or "/git " in c)]


@pytest.mark.platforms("posix")
def test_e2e_placed_worker_spawns_no_local_descendant(tmp_path):
    local, _ = _tree(tmp_path, placed=False)
    assert _not_provenance(local), local  # control: the walker sees the stdio server + supervisor
    placed, _ = _tree(tmp_path, placed=True)
    assert _not_provenance(placed) == [], placed



# -- E2E: retained vs withheld HANDLERS, observed from OUTSIDE the worker -------------------
# The worker runs real registry discovery + dispatch. The observer is this pytest process: it
# walks the worker's descendants with ps while the worker runs. The local binaries the mixed
# backends exec (rsvg-convert, pdftotext) are PATH shims that sleep, so every spawn is visible to
# a polling observer; process creation itself is never mocked. Only network payloads are fixtures.

_HANDLER_PROBE = r"""
import asyncio, json, sys
from hermes_cli import kanban_worker_hosts  # noqa: F401  (boot record, as main() takes it)
import model_tools  # noqa: F401
from tools.registry import registry
import tools.vision_tools as vt
import tools.web_tools as wt
import tools.web_pdf_local as wpl

PDF = sys.argv[1].encode()

async def _download(url, max_bytes):  # network payload fixture; extraction stays real
    return url, PDF, None

async def _safe(url):
    return True

vt._should_use_native_vision_fast_path = lambda: True  # no model call; prep is the spawn branch
wt.async_is_safe_url = _safe
wpl._download = _download
svg = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0IiBoZWlnaHQ9IjQiLz4="
calls = {
    "vision_analyze": {"image_url": svg, "question": "?"},
    "web_extract": {"urls": ["https://example.invalid/doc.pdf"]},
    "todo_list": {},
}
out = {}
for name, args in calls.items():
    out[name] = {"registered": registry.get_entry(name) is not None,
                 "result": str(registry.dispatch(name, args))[:300]}
print("RESULT " + json.dumps(out), flush=True)
"""

# A one-page PDF whose text layer reads "placement".
_PDF = ("%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 50]/Contents 4 0 R"
        "/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        "4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 10 20 Td (placement) Tj ET\nendstream endobj\n"
        "5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def _shim_dir(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    for name in ("rsvg-convert", "pdftotext"):
        path = shims / name
        path.write_text("#!/bin/sh\nsleep 1.5\necho placement\n")
        path.chmod(0o755)
    return shims


def _descendants(root: int) -> list:
    out = subprocess.run(["ps", "-axo", "uid=,pid=,ppid=,args="], capture_output=True, text=True).stdout
    kids = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3:
            kids.setdefault(int(parts[2]), []).append(parts)
    found, stack = [], [root]
    while stack:
        for parts in kids.get(stack.pop(), []):
            stack.append(int(parts[1]))
            found.append((int(parts[0]), parts[3] if len(parts) > 3 else ""))
    return found


def _observe_handlers(tmp_path, host):
    import time
    label = host.name if host else "unplaced"
    home = tmp_path / label
    home.mkdir()
    (home / "config.yaml").write_text("web:\n  local_pdf: true\n")
    shims = _shim_dir(home)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "KANBAN_", "TERMINAL_"))}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(REPO), PATH=f"{shims}:{env.get('PATH', '')}")
    if host is not None:
        kwh.apply_placement(env, host, str(tmp_path))
        env[kpt.REFUSED_ENV] = kpt.spawn_env_value({})
    proc = subprocess.Popen([sys.executable, "-c", _HANDLER_PROBE, _PDF], cwd=str(tmp_path), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen = set()
    deadline = time.monotonic() + 240
    while proc.poll() is None and time.monotonic() < deadline:
        seen.update(_descendants(proc.pid))
        time.sleep(0.05)
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr[-2000:]
    line = [l for l in stdout.splitlines() if l.startswith("RESULT ")][-1]
    spawned = sorted(cmd for _, cmd in seen if str(shims) in cmd)
    return json.loads(line[len("RESULT "):]), spawned, sorted(seen)


@pytest.mark.platforms("posix")
def test_e2e_unplaced_control_reaches_the_local_spawn_branches(tmp_path):
    calls, spawned, _ = _observe_handlers(tmp_path, None)
    assert calls["vision_analyze"]["registered"] and calls["web_extract"]["registered"]
    assert any("rsvg-convert" in c for c in spawned), (spawned, calls)
    assert any("pdftotext" in c for c in spawned), (spawned, calls)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("host", [STUDIO_LOCAL, ACE_AI], ids=["studio-local", "pool-host"])
def test_e2e_placed_worker_withholds_mixed_backends_before_any_local_child(tmp_path, host):
    calls, spawned, seen = _observe_handlers(tmp_path, host)
    assert spawned == [], spawned
    assert _not_provenance([c for _, c in seen]) == [], seen
    for name in ("vision_analyze", "web_extract"):
        assert not calls[name]["registered"], calls
        assert "Unknown tool" in calls[name]["result"], calls
    # The retained control really executes.
    assert calls["todo_list"]["registered"] and "Unknown tool" not in calls["todo_list"]["result"], calls


@pytest.mark.parametrize("toolset", ["web", "vision", "context_engine", "memory_write",
                                     kpt.MEMORY_PROVIDER_TOOLSET])
def test_mixed_backend_toolsets_are_withheld(toolset):
    env = _placed(ACE_AI, kpt.spawn_env_value({}))
    assert kpt.refusal_reason("t", toolset, env) == "not_on_allow_list"


def test_injected_schemas_follow_the_floor(monkeypatch):
    schemas = [{"name": "lcm_grep"}, {"name": "lcm_status"}]
    assert kpt.refuse_injected_schemas(schemas, "context_engine") is schemas  # unplaced: untouched
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    assert kpt.refuse_injected_schemas(schemas, "context_engine") == []
    assert kpt.refuse_injected_schemas([{"name": "x"}], kpt.MEMORY_PROVIDER_TOOLSET) == []
    assert ("context_engine", "not_on_allow_list", "lcm_grep") in kpt._recorded
    assert (kpt.MEMORY_PROVIDER_TOOLSET, "not_on_allow_list", "x") in kpt._recorded


def test_memory_provider_tools_are_not_injected_into_a_placed_agent(monkeypatch):
    from types import SimpleNamespace
    from agent import memory_manager as mm

    class _Mgr:
        providers = []

        def get_all_tool_schemas(self):
            return [{"name": "honcho_search", "description": "", "parameters": {"type": "object"}}]

    def _agent():
        return SimpleNamespace(_memory_manager=_Mgr(), tools=[{"type": "function", "function": {"name": "memory"}}],
                               valid_tool_names={"memory"}, enabled_toolsets=None, disabled_toolsets=None)

    unplaced = _agent()
    assert mm.inject_memory_provider_tools(unplaced) == 1
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    placed = _agent()
    assert mm.inject_memory_provider_tools(placed) == 0
    assert "honcho_search" not in placed.valid_tool_names


def test_inline_shell_never_runs_in_a_placed_worker(monkeypatch, tmp_path):
    from agent import skill_preprocessing as sp
    marker = tmp_path / "ran"
    cfg = {"inline_shell": True, "template_vars": False}
    body = f"x !`touch {marker}` y"
    sp.preprocess_skill_content(body, tmp_path, skills_cfg=cfg)
    assert marker.exists()  # control: the branch spawns bash when unplaced
    marker.unlink()
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "studio-local", "env": {}}))
    assert sp.preprocess_skill_content(body, tmp_path, skills_cfg=cfg) == body
    assert not marker.exists()
