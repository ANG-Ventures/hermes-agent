"""The Discord /skill catalog scan must not do per-skill realpath walks.

2026-10-01/02 (t_620ba53d): ``discord_skill_commands_by_category`` ran
``Path.resolve()`` on every one of ~1000 skill paths on the gateway event-loop
thread during Discord connect. Each realpath is several ``lstat()`` calls that
drop the GIL; with a GIL-busy thread alive (a kanban dispatcher tick) every
re-acquire waits out the switch interval, so a 0.05s scan took ~210s, the loop
missed 3 liveness probes and the watchdog exited 75 on 13 consecutive boots.

These tests pin: lexical matching first (no resolve per skill), the resolve
fallback for paths reached through a symlink, hub exclusion on both forms, and
that the two event-loop call sites run the scan in a worker thread.
"""
from __future__ import annotations

import ast
import inspect
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_skill(root: Path, rel: str) -> str:
    d = root / rel
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text("---\nname: x\n---\n")
    return str(md)


def _cmds(paths: dict[str, str]) -> dict:
    return {
        f"/{name}": {"name": name, "description": name, "skill_md_path": p}
        for name, p in paths.items()
    }


def _run(skills_dir: Path, cmds: dict, ext_dirs=()):
    from hermes_cli.commands import discord_skill_commands_by_category

    with patch("agent.skill_commands.get_skill_commands", return_value=cmds), patch(
        "tools.skills_tool.SKILLS_DIR", skills_dir
    ), patch(
        "agent.skill_utils.get_external_skills_dirs", return_value=list(ext_dirs)
    ), patch("agent.skill_utils.get_project_skills_dirs", return_value=[]):
        return discord_skill_commands_by_category(reserved_names=set())


def test_lexical_paths_are_not_resolved_per_skill(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    paths = {f"s{i:03d}": _make_skill(skills, f"cat/s{i:03d}") for i in range(50)}

    real_resolve = Path.resolve
    calls: list[str] = []

    def counting_resolve(self, *a, **kw):
        calls.append(str(self))
        return real_resolve(self, *a, **kw)

    with patch.object(Path, "resolve", counting_resolve):
        cats, unc, hidden = _run(skills, _cmds(paths))

    assert sorted(n for n, _d, _k in cats["cat"]) == sorted(paths)
    assert unc == [] and hidden == 0
    # Only the roots (skills dir + hub dir) are resolved, never the 50 skills.
    assert not [c for c in calls if c.endswith("SKILL.md")], calls
    assert len(calls) <= 4, calls


def test_symlinked_skill_path_still_matches_via_resolve_fallback(tmp_path: Path) -> None:
    real_root = tmp_path / "real-skills"
    md = _make_skill(real_root, "media/gif-search")
    link_root = tmp_path / "linked"
    os.symlink(real_root, link_root)
    linked_md = str(link_root / "media" / "gif-search" / "SKILL.md")

    # Scan root is the REAL dir; the registry reports the path via the symlink.
    cats, unc, hidden = _run(real_root, _cmds({"gif-search": linked_md}))
    assert [n for n, _d, _k in cats["media"]] == ["gif-search"]

    # And the reverse: scan root given via the symlink, path reported real.
    cats, unc, hidden = _run(link_root, _cmds({"gif-search": md}))
    assert [n for n, _d, _k in cats["media"]] == ["gif-search"]


def test_hub_skills_excluded(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    keep = _make_skill(skills, "cat/keep")
    hub = _make_skill(skills, ".hub/cat/hubbed")
    cats, unc, hidden = _run(skills, _cmds({"keep": keep, "hubbed": hub}))
    names = [n for n, _d, _k in cats.get("cat", [])] + [n for n, _d, _k in unc]
    assert names == ["keep"]


def test_outside_every_root_is_dropped(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    stray = _make_skill(tmp_path / "elsewhere", "cat/stray")
    cats, unc, hidden = _run(skills, _cmds({"stray": stray}))
    assert cats == {} and unc == []


def _source_of(obj) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(obj)))


def test_discord_connect_registers_slash_commands_off_loop() -> None:
    from plugins.platforms.discord.adapter import DiscordAdapter

    tree = _source_of(DiscordAdapter.connect)
    direct, threaded = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "_register_slash_commands":
            direct.append(node.lineno)
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "_register_slash_commands"
        ):
            threaded.append(node.lineno)
    assert not direct, f"_register_slash_commands called on the loop at {direct}"
    assert threaded, "connect() must run _register_slash_commands via asyncio.to_thread"


@pytest.mark.asyncio
async def test_reload_skills_runs_sync_refresh_off_loop_thread() -> None:
    import threading

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    seen: dict[str, int] = {}

    class SyncAdapter:
        name = "sync-platform"

        def refresh_skill_group(self):
            seen["thread"] = threading.get_ident()
            return (1, 0)

    runner.adapters = {"discord": SyncAdapter()}
    runner._session_key_for_source = lambda src: None
    runner._pending_skills_reload_notes = {}

    from unittest.mock import MagicMock

    event = MagicMock()
    with patch(
        "agent.skill_commands.reload_skills",
        return_value={"added": [], "removed": [], "total": 1},
    ):
        await runner._handle_reload_skills_command(event)

    assert seen["thread"] != threading.get_ident()
