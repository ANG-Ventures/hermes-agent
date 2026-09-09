"""Discover bypasses of the canonical session-key builder, across all producers."""
import ast
from pathlib import Path


def test_no_producer_interpolates_literal_session_namespace():
    root = Path(__file__).resolve().parents[2]
    bypasses = []
    for directory in ("gateway", "cron", "tools", "plugins", "hermes_cli"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr) or not node.values:
                    continue
                first = node.values[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("agent:main:"):
                    bypasses.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not bypasses, f"Session-key producers must use build_session_key: {bypasses}"


def test_discord_sources_do_not_hardcode_legacy_channel_type():
    root = Path(__file__).resolve().parents[2]
    bypasses = []
    for file in ("gateway/run.py", "gateway/kanban_watchers.py", "cron/scheduler.py",
                 "plugins/platforms/discord/adapter.py"):
        tree = ast.parse((root / file).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(k.arg == "chat_type" and isinstance(k.value, ast.Constant)
                   and k.value.value == "channel" for k in node.keywords):
                bypasses.append(f"{file}:{node.lineno}")
    assert not bypasses, f"Resolve channel types from platform metadata: {bypasses}"
