"""Every process that can run an agent turn must register config.yaml shell hooks.

Shell hooks live on the per-home plugin manager. A process that builds
``AIAgent`` without registering them runs every turn with no ``pre_tool_call``
/ ``post_tool_call`` shell hooks, so any hook-backed security gate silently
does nothing. That happened live: ``hermes serve`` (tui_gateway sessions over
/api/ws) never registered them, so clanker's taint gate never ran (t_e5708887).

Two guards here (the tui_gateway host itself is proven end to end in
tests/tui_gateway/test_profile_shell_hooks.py):

1. Behavioural, per CLI entrypoint: ``_prepare_agent_startup`` registers hooks
   for every command that hosts turns in-process, and not for management commands.
2. Ratchet: every non-test module that constructs ``AIAgent(...)`` is mapped to
   the host that registers hooks for it. A new construction site fails here
   until someone states which host covers it.
"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path

import pytest

from agent import shell_hooks

REPO = Path(__file__).resolve().parents[2]

# Hosts whose hook registration is proven by tests (this file + tests/tui_gateway/test_profile_shell_hooks.py).
CLI = "cli main(): _prepare_agent_startup (chat / -z oneshot / acp / rl)"
GATEWAY = "gateway run: gateway/run.py start registers hooks"
TUI_GATEWAY = ("tui_gateway._make_agent registers the session profile's hooks "
               "(serve, dashboard, TUI stdio backend; tests/tui_gateway/test_profile_shell_hooks.py)")
CRON = "cron run/tick (_AGENT_SUBCOMMANDS) or the gateway ticker"
CHILD = "in-process child of an already-registered host (same home, so the same per-home plugin manager)"
EXEMPT = "exempt"

# module path -> host that registers shell hooks for the turns it runs.
AGENT_CONSTRUCTION_HOSTS: dict[str, str] = {
    "acp_adapter/session.py": CLI,
    "agent/background_review.py": CHILD,
    "agent/curator.py": CHILD,
    "batch_runner.py": EXEMPT + ": standalone trajectory-generation runner, not a profile agent host",
    "cron/scheduler.py": CRON,
    "gateway/platforms/api_server.py": GATEWAY,
    "gateway/run.py": GATEWAY,
    "gateway/slash_commands.py": GATEWAY,
    "hermes_cli/cli_agent_setup_mixin.py": CLI,
    "hermes_cli/cli_commands_mixin.py": CLI,
    "hermes_cli/oneshot.py": CLI,
    "hermes_cli/prompt_size.py": EXEMPT + ": inspect-only agent, never runs a turn",
    "plugins/platforms/feishu/feishu_comment.py": GATEWAY,
    "run_agent.py": EXEMPT + ": `python run_agent.py` dev entrypoint",
    "tools/delegate_tool.py": CHILD,
    "tui_gateway/methods_prompt.py": TUI_GATEWAY + "; background agents reuse the session agent's home",
    "tui_gateway/server.py": TUI_GATEWAY,
}

_SKIP_PARTS = {"tests", "evals", "scripts", "venv", ".venv", "node_modules", ".git", "website"}


def _modules_constructing_aiagent() -> set[str]:
    found: set[str] = set()
    # os.walk, not Path.rglob: rglob raises FileNotFoundError when a concurrent test (xdist) removes a
    # __pycache__ mid-walk (heavy-ci 37033036338); os.walk skips a dir that vanished. Pruning in place
    # also keeps the walk out of venv/.git/node_modules.
    paths = []
    for root, dirs, files in os.walk(REPO):
        top = Path(root) == REPO
        dirs[:] = [d for d in dirs if d not in _SKIP_PARTS and d != "__pycache__" and not (top and d.startswith("."))]
        paths.extend(Path(root) / f for f in files if f.endswith(".py"))
    for path in paths:
        rel = path.relative_to(REPO)
        if set(rel.parts) & _SKIP_PARTS or rel.parts[0].startswith("."):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "AIAgent":
                found.add(rel.as_posix())
                break
    return found


def test_every_aiagent_construction_site_has_a_hook_registering_host():
    found = _modules_constructing_aiagent()
    unmapped = sorted(found - set(AGENT_CONSTRUCTION_HOSTS))
    stale = sorted(set(AGENT_CONSTRUCTION_HOSTS) - found)
    assert not unmapped, (
        "New AIAgent(...) construction site(s) with no declared shell-hook host: "
        f"{unmapped}. Add each to AGENT_CONSTRUCTION_HOSTS naming the process that "
        "calls agent.shell_hooks.register_from_config() before its "
        "turns run; if that process does not register hooks yet, fix it first."
    )
    assert not stale, f"AGENT_CONSTRUCTION_HOSTS lists modules with no AIAgent(...) call: {stale}"


# ── _prepare_agent_startup gate, per entrypoint ────────────────────────────


@pytest.fixture
def startup_spy(monkeypatch):
    """Record hook registration; neutralise plugin + MCP discovery side effects."""
    import hermes_cli.mcp_startup as mcp_startup
    import hermes_cli.plugins as plugins_mod
    import tools.mcp_tool as mcp_tool

    calls: list[dict] = []
    monkeypatch.setattr(
        shell_hooks,
        "register_from_config",
        lambda cfg, **kw: calls.append(kw) or [],
    )
    monkeypatch.setattr(plugins_mod, "start_background_plugin_discovery", lambda: None)
    monkeypatch.setattr(mcp_startup, "start_background_mcp_discovery", lambda **kw: None)
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda *a, **kw: None)
    return calls


# Every CLI command that hosts agent turns in its own process.
AGENT_HOST_INVOCATIONS = [
    {"command": None},
    {"command": "chat"},
    {"command": "acp"},
    {"command": "rl"},
    {"command": "gateway", "gateway_command": "run"},
    {"command": "cron", "cron_command": "run"},
    {"command": "cron", "cron_command": "tick"},
    {"command": "mcp", "mcp_action": "serve"},
]

NON_HOST_INVOCATIONS = [
    {"command": "hooks"},
    {"command": "gateway", "gateway_command": "status"},
    {"command": "cron", "cron_command": "list"},
    {"command": "dashboard"},
]


def _ns(fields: dict) -> argparse.Namespace:
    # tui=False keeps the chat path off the TUI launcher branch.
    return argparse.Namespace(tui=False, **fields)


@pytest.mark.parametrize("fields", AGENT_HOST_INVOCATIONS, ids=lambda f: " ".join(str(v) for v in f.values()))
def test_agent_host_commands_register_shell_hooks(fields, startup_spy, monkeypatch):
    from hermes_cli.main import _prepare_agent_startup

    monkeypatch.delenv("HERMES_TUI", raising=False)
    _prepare_agent_startup(_ns(fields))
    assert len(startup_spy) == 1, f"{fields} hosts agent turns but registered no shell hooks"


@pytest.mark.parametrize("fields", NON_HOST_INVOCATIONS, ids=lambda f: " ".join(str(v) for v in f.values()))
def test_management_commands_do_not_register_shell_hooks(fields, startup_spy):
    from hermes_cli.main import _prepare_agent_startup

    _prepare_agent_startup(_ns(fields))
    assert startup_spy == []
