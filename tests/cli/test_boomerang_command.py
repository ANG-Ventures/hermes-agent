"""Phase 3 of the /boomerang spec: the native CommandDef.

Verifies /boomerang resolves, surfaces in the gateway picker with an arg field,
and is not cli_only.
"""
from hermes_cli.commands import resolve_command


class TestBoomerangCommandDef:
    def test_boomerang_resolves(self):
        d = resolve_command("boomerang")
        assert d is not None
        assert d.name == "boomerang"

    def test_has_task_arg_hint_for_discord_picker(self):
        d = resolve_command("boomerang")
        assert d.args_hint == "<task>"

    def test_is_gateway_visible_not_cli_only(self):
        d = resolve_command("boomerang")
        assert not getattr(d, "cli_only", False)


# ── Boomerang v2 P4: entry-point mode flags (docs/boomerang-v2/README.md §1) ──

import asyncio

import pytest

from hermes_cli.boomerang_command import parse_boomerang_args


class TestBoomerangModeFlags:
    def test_default_is_fork(self):
        assert parse_boomerang_args("fix the flaky test") == ("fork", "fix the flaky test")

    def test_summary_flag_selects_fold(self):
        assert parse_boomerang_args("--summary fix it") == ("summary", "fix it")

    def test_branch_is_fork_alias(self):
        assert parse_boomerang_args("--branch fix it") == parse_boomerang_args("--fork fix it") == ("fork", "fix it")

    def test_flag_only_after_leading_position_is_task_text(self):
        assert parse_boomerang_args("explain --summary flags") == ("fork", "explain --summary flags")

    def test_conflicting_flags_refused(self):
        with pytest.raises(ValueError):
            parse_boomerang_args("--summary --fork x")

    def test_description_fits_platform_menu_cap(self):
        assert len(resolve_command("boomerang").description) <= 100


class _Event:
    def __init__(self, args):
        self._args = args
        self.text = f"/boomerang {args}"

    def get_command_args(self):
        return self._args


def _gateway_rewrite(args, monkeypatch):
    """Drive the real gateway handler with the skill absent (inline fallback seed)."""
    import agent.skill_commands as sc
    from gateway.run_inbound import GatewayInboundMixin
    monkeypatch.setattr(sc, "resolve_skill_command_key", lambda _name: None)
    ev = _Event(args)
    handled, reply = asyncio.run(GatewayInboundMixin._hm_cmd_boomerang(None, ev, None, "k"))
    return handled, reply, ev.text


class TestGatewayBoomerangSeed:
    @pytest.mark.parametrize("args,mode", [
        ("do x", "fork"), ("--fork do x", "fork"), ("--branch do x", "fork"), ("--summary do x", "summary"),
    ])
    def test_seed_carries_inherit_mode(self, args, mode, monkeypatch):
        handled, reply, text = _gateway_rewrite(args, monkeypatch)
        assert (handled, reply) == (False, None)
        assert f'inherit_mode="{mode}"' in text
        assert "do x" in text and "--" not in text.split("do x")[0]

    def test_flag_without_task_shows_usage(self, monkeypatch):
        handled, reply, _ = _gateway_rewrite("--summary", monkeypatch)
        assert handled and "--summary" in reply
