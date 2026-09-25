# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""Ratchet: platform-PLUGIN coroutines must not reach an atomic write.

Card t_eb49443b.  2026-09-24, Apollo: 46 ``PHASE=event_loop_blocked
platform=discord site=utils.py:230 atomic_replace`` events in 40 minutes, up to
20 s each, while 30+ kanban workers contended for the internal SSD::

    gateway/kanban_watchers.py _kanban_notifier_watcher   (async)
      plugins/platforms/discord/adapter.py send           (async)
        _DiscordRestartRecoveryState.mark_channel_active  (plain def)
          _persist_locked                                 (plain def)
            utils.atomic_json_write -> atomic_replace     <- fsync + rename

WHY THE EXISTING GATE WAS GREEN.  ``test_no_atomic_write_reachable_from_loop``
derives its scope by walking the IMPORT graph from ``gateway/run.py``.  Platform
adapters under ``plugins/platforms/`` are loaded by the plugin registry at
runtime, not imported, so ``plugins/platforms/discord/adapter.py`` was never in
the derived set -- the incident chain was invisible to the call-graph walk even
though the engine resolves it trivially.  This module closes that scope gap:
it enumerates every ``plugins/platforms/**.py`` from the FILESYSTEM and runs the
same engine over it (plus the gateway modules, so shared helpers resolve).

Same ratchet semantics as the sibling: a NEW (module, coroutine, sink) pair
fails; a pair that disappears without the baseline being updated also fails.
"""

from __future__ import annotations

from pathlib import Path

from tests.gateway._loop_atomic_write_reachability import (
    derive_scanned_modules,
    find_onloop_atomic_write_sites,
    ratchet_key,
)

PLUGIN_ROOT = "plugins/platforms"
MIN_PLUGIN_MODULES = 20


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def plugin_modules(repo: Path) -> frozenset[str]:
    out = set()
    for p in (repo / PLUGIN_ROOT).rglob("*.py"):
        rel = p.relative_to(repo).as_posix()
        if "/tests/" in rel:
            continue
        out.add(rel)
    return frozenset(out)


def _plugin_sites(repo: Path) -> list[str]:
    modules = set(derive_scanned_modules(repo)) | set(plugin_modules(repo))
    return [
        s
        for s in find_onloop_atomic_write_sites(repo, modules)
        if s.startswith(PLUGIN_ROOT + "/")
    ]


# Pre-existing pairs in OTHER platform plugins, frozen when this gate landed.
# Not endorsed -- each is the same class -- but they sit on connect / startup /
# setup-command / dedup paths outside this incident; tracked for follow-up on
# card t_eb49443b.  Fix one: move it off-loop, then DELETE its line.
PLUGIN_REACHABLE_BASELINE = frozenset({
    "plugins/platforms/buzz/adapter.py connect -> os.replace",
    "plugins/platforms/feishu/adapter.py _handle_message_event_data -> atomic_json_write",
    "plugins/platforms/feishu/adapter.py connect -> os.replace",
    "plugins/platforms/feishu/adapter.py disconnect -> atomic_json_write",
    "plugins/platforms/google_chat/adapter.py _build_message_event -> os.replace",
    "plugins/platforms/google_chat/adapter.py _create_message -> os.replace",
    "plugins/platforms/google_chat/adapter.py _handle_setup_files_command -> atomic_replace",
    "plugins/platforms/google_chat/adapter.py _send_file -> os.replace",
    "plugins/platforms/irc/adapter.py connect -> os.replace",
    "plugins/platforms/line/adapter.py connect -> os.replace",
    "plugins/platforms/photon/adapter.py _start_sidecar -> os.replace",
})


def test_plugin_scope_is_enumerated_from_the_filesystem():
    """The scope gap that hid the incident: the adapter must be IN scope."""
    repo = _repo_root()
    mods = plugin_modules(repo)
    assert len(mods) >= MIN_PLUGIN_MODULES, sorted(mods)
    assert "plugins/platforms/discord/adapter.py" in mods
    # ...and it is exactly the file the import-derived scope does NOT reach,
    # which is why this sibling gate has to exist.
    assert "plugins/platforms/discord/adapter.py" not in derive_scanned_modules(repo)


def test_no_discord_coroutine_reaches_an_atomic_write():
    """The incident class, pinned at zero for the Discord adapter.

    Covers the hot path (``send`` / ``_handle_message`` ->
    ``mark_channel_active``), the non-conversational id tracker
    (``send`` -> ``mark_many``), shutdown (``disconnect`` -> ``flush``) and
    the slash-command sync state (``_run_post_connect_initialization_locked``).
    """
    live = [
        s for s in _plugin_sites(_repo_root())
        if s.startswith("plugins/platforms/discord/")
    ]
    assert not live, (
        "a Discord adapter coroutine can reach a blocking fsync/rename on the "
        "event loop again (2026-09-24: 46 event_loop_blocked in 40 min via "
        "send -> mark_channel_active -> atomic_json_write):\n  "
        + "\n  ".join(live)
    )


def test_platform_plugin_atomic_writes_do_not_grow():
    sites = _plugin_sites(_repo_root())
    chains = {ratchet_key(s): s for s in sites}
    current = set(chains)
    added = sorted(current - PLUGIN_REACHABLE_BASELINE)
    removed = sorted(PLUGIN_REACHABLE_BASELINE - current)
    assert not added, (
        "NEW platform-plugin coroutine(s) can reach a blocking atomic write. "
        "Move the write off-loop (asyncio.to_thread / "
        "gateway.platforms.helpers.CoalescingJsonWriter):\n"
        + "\n".join(f"  {chains[a]}" for a in added)
    )
    assert not removed, (
        "Baseline pair(s) no longer exist -- delete them from "
        "PLUGIN_REACHABLE_BASELINE:\n" + "\n".join(f"  {r}" for r in removed)
    )


def test_arm_the_2026_09_24_shape_is_red(tmp_path):
    """Mutation arm: the exact incident shape in a plugin file is named."""
    src = "\n".join([
        "from utils import atomic_json_write",
        "",
        "class _State:",
        "    def _persist_locked(self):",
        "        atomic_json_write('/tmp/x', {})",
        "",
        "    def mark_channel_active(self, cid):",
        "        self._persist_locked()",
        "",
        "class Adapter:",
        "    async def send(self, chat_id):",
        "        self._restart_recovery.mark_channel_active(chat_id)",
        "",
    ])
    p = tmp_path / "plugins/platforms/fake/adapter.py"
    p.parent.mkdir(parents=True)
    p.write_text(src)
    sites = find_onloop_atomic_write_sites(tmp_path, ["plugins/platforms/fake/adapter.py"])
    assert sites == [
        "plugins/platforms/fake/adapter.py send -> atomic_json_write "
        "via send>mark_channel_active>_persist_locked"
    ], sites


def test_arm_the_fixed_shape_is_green(tmp_path):
    """GREEN arm: schedule-only mark + to_thread flush must not read as the defect."""
    src = "\n".join([
        "import asyncio",
        "",
        "class _State:",
        "    def mark_channel_active(self, cid):",
        "        self._writer.schedule()",
        "",
        "    def flush(self):",
        "        self._writer.flush()",
        "",
        "class Adapter:",
        "    async def send(self, chat_id):",
        "        self._restart_recovery.mark_channel_active(chat_id)",
        "",
        "    async def disconnect(self):",
        "        await asyncio.to_thread(self._restart_recovery.flush)",
        "",
    ])
    p = tmp_path / "plugins/platforms/fake/adapter.py"
    p.parent.mkdir(parents=True)
    p.write_text(src)
    assert find_onloop_atomic_write_sites(tmp_path, ["plugins/platforms/fake/adapter.py"]) == []
