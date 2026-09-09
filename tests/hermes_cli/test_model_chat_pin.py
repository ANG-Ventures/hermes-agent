"""The CLI can pin a destination without changing the profile default."""
import argparse
from types import SimpleNamespace

from gateway.chat_model_pins import ChatModelPins
from gateway.config import GatewayConfig
from hermes_cli.subcommands.model import build_model_parser


def test_model_chat_pin_and_clear(tmp_path, monkeypatch):
    from hermes_cli.main import cmd_model
    parser = argparse.ArgumentParser()
    build_model_parser(parser.add_subparsers(), cmd_model=cmd_model)
    config = GatewayConfig(sessions_dir=tmp_path)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "default-model", "provider": "openrouter"}})
    switches = []
    def switch(**kwargs):
        switches.append(kwargs)
        return SimpleNamespace(success=True, new_model="pinned-model", target_provider="openrouter", api_mode="chat_completions")
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", switch)
    args = parser.parse_args(["model", "--chat", "discord:123", "--set", "pinned-model", "--provider", "openrouter"])
    args.func(args)
    assert switches[0]["is_global"] is False
    assert ChatModelPins(tmp_path).get("main", "discord", "123")[1]["model"] == "pinned-model"
    args = parser.parse_args(["model", "--chat", "discord:123", "--clear"])
    args.func(args)
    assert ChatModelPins(tmp_path).get("main", "discord", "123") == (True, None)
