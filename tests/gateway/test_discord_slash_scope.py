"""Per-profile Discord slash menu scope and stale registry cleanup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig, load_gateway_config
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _parse_slash_command_scope,
)


def _adapter(tmp_path, monkeypatch, scope="all"):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token", extra={"slash_commands": scope}))


def _client(names=("status", "stop"), existing=("status", "stop"), guild_existing=("old",)):
    commands = [SimpleNamespace(name=n, type=SimpleNamespace(value=1), to_dict=lambda tree, n=n: {
        "name": n, "description": f"{n} command", "type": 1, "options": [],
    }) for n in names]
    tree = SimpleNamespace()
    tree.get_commands = lambda: list(commands)
    tree.remove_command = lambda name, **kw: commands.__setitem__(slice(None), [c for c in commands if c.name != name])
    tree.fetch_commands = AsyncMock(side_effect=lambda *, guild=None: [
        SimpleNamespace(id=i, name=n, type=SimpleNamespace(value=1), to_dict=lambda n=n: {
            "name": n, "description": f"{n} command", "type": 1, "options": [],
        }) for i, n in enumerate(guild_existing if guild else existing, 1)
    ])
    tree.sync = AsyncMock(return_value=[])
    http = SimpleNamespace(bulk_upsert_guild_commands=AsyncMock(), delete_global_command=AsyncMock(),
                           upsert_global_command=AsyncMock(), edit_global_command=AsyncMock())
    client = SimpleNamespace(tree=tree, http=http, application_id=123,
                             guilds=[SimpleNamespace(id=456)])
    return client


@pytest.mark.parametrize("raw,mode,names", [
    ("all", "all", set()), (True, "all", set()),
    ("none", "none", set()), (False, "none", set()),
    (["status", "/stop"], "list", {"status", "stop"}),
    ("status,stop", "list", {"status", "stop"}),
    ([], "none", set()),
])
def test_parse_scope(raw, mode, names):
    actual_mode, actual_names = _parse_slash_command_scope(raw)
    assert (actual_mode, actual_names) == (mode, names)


def test_scope_filters_all_registration_paths(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch, ["status"])
    adapter._client = _client(names=("status", "stop", "plugin", "skill"))
    adapter._apply_slash_command_scope(adapter._client.tree)
    assert [c.name for c in adapter._client.tree.get_commands()] == ["status"]
    adapter._client = _client(names=("status", "stop"))
    _adapter_all = _adapter(tmp_path, monkeypatch)
    _adapter_all._client = adapter._client
    _adapter_all._apply_slash_command_scope(adapter._client.tree)
    assert [c.name for c in adapter._client.tree.get_commands()] == ["status", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected_names,expected_global_delete,expected_guild_delete", [
    ("none", [], 2, 1),
    (["status"], ["status"], 1, 1),
    ("all", ["status", "stop"], 0, 0),
])
async def test_connect_reconciles_scope_and_stale_guild_commands(tmp_path, monkeypatch, scope,
                                                                 expected_names, expected_global_delete,
                                                                 expected_guild_delete):
    adapter = _adapter(tmp_path, monkeypatch, scope)
    adapter._client = _client()
    adapter._apply_slash_command_scope(adapter._client.tree)
    assert [c.name for c in adapter._client.tree.get_commands()] == expected_names
    adapter._state_write_off_loop = AsyncMock()
    adapter._command_sync_skip_reason = MagicMock(return_value=None)
    adapter._check_command_registry_drift = AsyncMock()
    monkeypatch.setattr(adapter, "_get_discord_command_sync_policy", lambda: "safe")
    await adapter._run_post_connect_initialization()
    # A restricted scope bulk-overwrites globals in one atomic request, including
    # the empty set. The default path does not touch identical global entries.
    assert adapter._client.tree.sync.await_count == (1 if expected_global_delete else 0)
    assert adapter._client.http.bulk_upsert_guild_commands.await_count == expected_guild_delete
    if expected_guild_delete:
        adapter._client.http.bulk_upsert_guild_commands.assert_awaited_once_with(123, 456, [])
    # On a second connect the restricted scope still reconciles, never trusting
    # the desired-tree fingerprint as proof Discord has no stale commands.
    await adapter._run_post_connect_initialization()
    assert adapter._client.tree.sync.await_count == (2 if expected_global_delete else 0)


def test_prefix_fallback_is_restricted_and_command_only(tmp_path, monkeypatch):
    none = _adapter(tmp_path, monkeypatch, "none")
    assert none._rewrite_prefix_command("!status") == "/status"
    assert none._rewrite_prefix_command("!stop now") == "/stop now"
    assert none._rewrite_prefix_command("!not-a-command") == "!not-a-command"
    all_scope = _adapter(tmp_path, monkeypatch)
    assert all_scope._rewrite_prefix_command("!status") == "!status"


def test_yaml_scope_roundtrip(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("platforms:\n  discord:\n    enabled: true\ndiscord:\n  slash_commands:\n    - status\n    - stop\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_gateway_config()
    from gateway.config import Platform
    assert config.platforms[Platform.DISCORD].extra["slash_commands"] == ["status", "stop"]
