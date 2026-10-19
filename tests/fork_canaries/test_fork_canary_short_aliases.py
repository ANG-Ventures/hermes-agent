"""Fork canary: one- and two-letter slash shortcuts (Ace rulings 2026-10-18).

Ace: "make /s a shortcut for /stop, /q for /queue", then "/steer gets st", and "add regression testing
for all of those new aliases ... so we don't lose them whenever we update Hermes to upstream parity,
eg /m and /r". Upstream binds ``s`` to ``steer`` and has no ``m`` / ``r`` / ``st`` alias, so a parity
merge that resolves ``hermes_cli/commands.py`` toward upstream silently repoints /s back to /steer and
drops /m /r /st. No import error and nothing else goes red: Ace's /s just stops interrupting the agent.

The registry is the single source for every surface (CLI dispatch, gateway, Telegram/Discord menus);
the TUI keeps its own local registry (``ui-tui/src/app/slash/commands/*.ts``), pinned by
``ui-tui/src/__tests__/slashParity.test.ts``.

These are contracts on the shortcut mapping only; adding commands or aliases elsewhere never turns this
file red. Each test names the edit that turns it red. RED-proven 2026-10-18 (card t_92c49fb6).
"""

import importlib
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli.commands import resolve_command

SHORTCUTS = {"s": "stop", "st": "steer", "q": "queue", "m": "model", "r": "reasoning"}


# Red if: commands.py loses aliases=("s",) on stop / ("st",) on steer / ("q",) on queue /
# ("m",) on model / ("r",) on reasoning, or upstream's aliases=("s",) on steer comes back.
@pytest.mark.parametrize("alias, name", sorted(SHORTCUTS.items()))
def test_shortcut_resolves_to_its_command(alias, name):
    cmd = resolve_command(alias)
    assert cmd is not None, f"/{alias} no longer resolves"
    assert cmd.name == name
    assert resolve_command(f"/{alias.upper()}").name == name  # leading slash + case still normalized


# Red if: an alias is ever added that shadows one of these full names (exact names win).
@pytest.mark.parametrize("name", ["sessions", "steer", "stop", "start", "status", "model", "reasoning", "retry",
                                  "queue"])
def test_full_names_still_resolve_to_themselves(name):
    assert resolve_command(name).name == name


def _runner():
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(quick_commands={})  # no config alias: the registry alone must do it
    runner._check_slash_access = lambda source, canonical: None

    async def _no_hooks(event, source, quick_key, command, canonical):
        return False, None, None

    runner._hm_command_hooks = _no_hooks
    return runner


def _ev(text):
    source = SessionSource(platform=Platform.DISCORD, chat_id="7", chat_type="dm")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source), source


# Red if: the gateway stops canonicalizing a typed alias, or "s" stops meaning stop. With no
# quick_commands configured, /m and /r reach /model and /reasoning only through the registry.
@pytest.mark.parametrize("typed, canonical, args", [
    ("/s", "stop", ""), ("/st nudge it", "steer", "nudge it"),
    ("/m gpt-5.5 --provider openrouter", "model", "gpt-5.5 --provider openrouter"),
    ("/r high", "reasoning", "high"), ("/q later", "queue", "later"),
])
@pytest.mark.asyncio
async def test_gateway_resolves_typed_shortcut(typed, canonical, args):
    event, source = _ev(typed)
    handled, _result, _command, got = await _runner()._hm_resolve_command(event, source, "k")
    assert handled is False
    assert got == canonical
    assert event.get_command_args() == args


# Red if: /s on a busy gateway session stops reaching the hard-kill stop handler (it would hit
# _busy_steer_command if upstream's steer alias returns).
@pytest.mark.asyncio
async def test_busy_gateway_s_dispatches_stop_handler():
    stop_def = resolve_command("s")
    assert stop_def.busy_policy == "interrupt_then_dispatch"
    assert stop_def.busy_handler == "stop"

    runner = _runner()
    called = []

    async def _stop(event, quick_key, source):
        called.append("stop")
        return "stopped"

    async def _steer(event, quick_key, source):
        called.append("steer")
        return "steered"

    runner._busy_stop_command = _stop
    runner._busy_steer_command = _steer
    event, source = _ev("/s")
    handled, result = await runner._hm_busy_slash_or_photo(event, source, "k")
    assert (handled, result, called) == (True, "stopped", ["stop"])


# ── TUI / Desktop JSON-RPC (tui_gateway) ───────────────────────────────────────────────────────
# Prism P1s on #2116: slash.exec keyed its routing on the RAW typed name, so a new alias routed
# differently from its command. /st skipped the pending-input bypass (steer) and fell into the slash
# worker, which has no reader for that queue; /m <model> ran in the worker but skipped the live
# _apply_model_switch mirror, so Desktop's live session never switched.


def _tui_session(**extra):
    import threading
    return {"agent": SimpleNamespace(), "session_key": "k", "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", **extra}


# Red if: slash.exec stops canonicalizing before the _PENDING_INPUT_COMMANDS check (/st -> worker).
@pytest.mark.parametrize("typed, canonical", [("st", "steer"), ("q", "queue"), ("steer", "steer")])
def test_tui_slash_exec_routes_alias_like_its_command(monkeypatch, typed, canonical):
    from tui_gateway import server
    seen = []
    monkeypatch.setitem(server._methods, "command.dispatch",
                        lambda rid, params: seen.append(params["name"]) or {"result": {"type": "exec"}})
    server._sessions["sid"] = _tui_session()
    try:
        server._methods["slash.exec"]("1", {"session_id": "sid", "command": f"/{typed} check the logs"})
    finally:
        server._sessions.pop("sid", None)
    assert seen == [canonical], f"/{typed} must reach command.dispatch as {canonical}, not the slash worker"


# Red if: the live side-effect mirror keys on the raw name again (/m <model> never switches live).
@pytest.mark.parametrize("typed", ["m", "model"])
def test_tui_model_alias_applies_live_switch(monkeypatch, typed):
    from tui_gateway import server
    calls = []
    monkeypatch.setattr(server, "_apply_model_switch", lambda sid, sess, raw, **kw: calls.append(raw) or {})
    session = _tui_session(agent=SimpleNamespace(model="old"))
    server._mirror_slash_side_effects("sid", session, f"/{typed} gpt-5.5 --provider openrouter")
    assert calls == ["gpt-5.5 --provider openrouter"]
