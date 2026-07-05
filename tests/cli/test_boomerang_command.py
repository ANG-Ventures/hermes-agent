"""Phase 3 of the /boomerang spec: the native CommandDef + /br alias.

Verifies /boomerang and /br resolve to a single CommandDef (the /branch->/fork
precedent), surface in the gateway picker with an arg field, and are not cli_only.
"""
from hermes_cli.commands import resolve_command


class TestBoomerangCommandDef:
    def test_boomerang_resolves(self):
        d = resolve_command("boomerang")
        assert d is not None
        assert d.name == "boomerang"

    def test_br_alias_resolves_to_same_commanddef(self):
        d_boom = resolve_command("boomerang")
        d_br = resolve_command("br")
        assert d_br is not None
        assert d_br.name == "boomerang"
        # Alias parity: one handler, two names (mirrors /branch -> /fork).
        assert d_br is d_boom

    def test_has_task_arg_hint_for_discord_picker(self):
        d = resolve_command("boomerang")
        assert d.args_hint == "<task>"

    def test_is_gateway_visible_not_cli_only(self):
        d = resolve_command("boomerang")
        assert not getattr(d, "cli_only", False)
