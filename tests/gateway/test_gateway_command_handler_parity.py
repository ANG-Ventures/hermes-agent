"""Invariant: every gateway-advertised slash command has a real dispatch handler.

Regression guard for the silent-orphan class: a CommandDef lands in
COMMAND_REGISTRY without cli_only=True, so it is auto-derived into
GATEWAY_KNOWN_COMMANDS and auto-printed by gateway_help_lines() -- but nobody
adds a `canonical == "<name>"` branch in gateway/run.py.

Failure mode is SILENT: the known-command gate in gateway/run.py suppresses the
"Unknown command" notice (the name IS known), so the text falls through to the
LLM as a plain user turn. The user sees the command advertised in /help, types
it, and gets a hallucinated conversational reply instead of the feature.
"""

from __future__ import annotations

import pytest

from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


# Commands that legitimately have no dispatch entry because they are dispatched
# by another documented mechanism. Add here ONLY with a reason.
_DISPATCHED_ELSEWHERE: dict[str, str] = {}


def _handled_canonicals() -> set[str]:
    """Canonical names the gateway actually dispatches on.

    2026-10 parity merge: upstream replaced the per-command ``canonical == "x"``
    ladders in gateway/run.py with handler TABLES — ``_PLAIN_COMMANDS`` /
    ``_IDLE_COMMANDS`` (gateway/run_busy.py, resolved through
    ``_command_handler_table`` → ``_handle_<name>_command``), the idle-path
    built-ins ``_HM_CANONICAL_COMMANDS`` (gateway/run_inbound.py →
    ``_hm_cmd_<name>``) and the busy-path ``_BUSY_SPECIAL_HANDLERS``. Read the
    live tables and prove every entry resolves to a real method on
    ``GatewayRunner`` — a name in a table with no method would AttributeError
    at dispatch time, the same silent-orphan class this test exists for.
    """
    from gateway.run import GatewayRunner

    names: set[str] = set()
    for name in (*GatewayRunner._PLAIN_COMMANDS, *GatewayRunner._IDLE_COMMANDS):
        attr = GatewayRunner._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command")
        assert callable(getattr(GatewayRunner, attr, None)), (
            f"/{name} is in the gateway handler table but GatewayRunner.{attr} does not exist"
        )
        names.add(name)
    for name in GatewayRunner._HM_CANONICAL_COMMANDS:
        assert callable(getattr(GatewayRunner, f"_hm_cmd_{name}", None)), (
            f"/{name} is in _HM_CANONICAL_COMMANDS but GatewayRunner._hm_cmd_{name} does not exist"
        )
        names.add(name)
    for key, attr in GatewayRunner._BUSY_SPECIAL_HANDLERS.items():
        assert callable(getattr(GatewayRunner, attr, None)), (
            f"busy handler {key!r} names GatewayRunner.{attr}, which does not exist"
        )
        names.add(key)
    return names


def test_every_gateway_known_command_has_a_dispatch_handler():
    handled = _handled_canonicals()

    orphans = []
    for name in sorted(GATEWAY_KNOWN_COMMANDS):
        cmd = resolve_command(name)
        if cmd is None or cmd.name != name:
            continue  # alias; canonical is checked on its own pass
        if cmd.name in _DISPATCHED_ELSEWHERE:
            continue
        if cmd.name not in handled:
            orphans.append(cmd.name)

    assert not orphans, (
        "Gateway-advertised commands with no dispatch handler in the gateway tables: "
        + ", ".join("/" + o for o in orphans)
        + ". These pass the GATEWAY_KNOWN_COMMANDS gate (so the user gets NO "
        "'Unknown command' notice) and fall through to the LLM as raw text. "
        "Either add a `canonical == \"<name>\"` branch, mark the CommandDef "
        "cli_only=True, or register it in _DISPATCHED_ELSEWHERE with a reason."
    )


def test_harness_detects_a_synthetic_orphan(monkeypatch):
    """Negative probe: prove the harness would actually catch an orphan — a table
    entry with no backing method, and an advertised name no table covers."""
    from gateway.run import GatewayRunner

    handled = _handled_canonicals()
    assert "model" in handled
    assert "definitely-not-a-command" not in handled
    monkeypatch.setattr(
        GatewayRunner, "_IDLE_COMMANDS", (*GatewayRunner._IDLE_COMMANDS, "definitely-not-a-command"),
    )
    with pytest.raises(AssertionError, match="definitely-not-a-command"):
        _handled_canonicals()
