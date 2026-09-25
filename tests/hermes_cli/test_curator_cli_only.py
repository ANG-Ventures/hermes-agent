"""/curator has no gateway handler, so it must not be advertised to gateway surfaces."""
from hermes_cli.commands import COMMAND_REGISTRY, GATEWAY_KNOWN_COMMANDS


def test_curator_is_cli_only():
    cmd = next(c for c in COMMAND_REGISTRY if c.name == "curator")
    assert cmd.cli_only
    assert "curator" not in GATEWAY_KNOWN_COMMANDS or cmd.gateway_config_gate
