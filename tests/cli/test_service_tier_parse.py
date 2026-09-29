"""agent.service_tier parse table — one table behind every config loader.

The CLI (``cli._parse_service_tier_config``), the messaging gateway
(``GatewayRunner._load_service_tier``) and ``hermes serve`` / TUI
(``tui_gateway.server._load_service_tier``) used to carry three hand-copied
parsers that only knew fast/priority. They must agree, including on
``ultrafast``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

PARSE_TABLE = [
    ("", None),
    (None, None),
    ("normal", None),
    ("default", None),
    ("standard", None),
    ("off", None),
    ("none", None),
    ("fast", "priority"),
    ("priority", "priority"),
    ("on", "priority"),
    ("FAST", "priority"),
    ("ultrafast", "ultrafast"),
    ("  UltraFast  ", "ultrafast"),
    ("turbo", None),
    ("flex", None),
]


@pytest.mark.parametrize("raw, expected", PARSE_TABLE)
def test_shared_parser(raw, expected):
    from hermes_cli.fast_mode_contracts import parse_service_tier

    assert parse_service_tier(raw) == expected


@pytest.mark.parametrize("raw, expected", PARSE_TABLE)
def test_cli_parser_matches_table(raw, expected):
    import cli

    assert cli._parse_service_tier_config(raw) == expected


@pytest.mark.parametrize("raw, expected", PARSE_TABLE)
def test_gateway_loader_matches_table(raw, expected):
    import gateway.run as gateway_run

    with patch.object(
        gateway_run,
        "_load_gateway_runtime_config",
        return_value={"agent": {"service_tier": raw}},
    ):
        assert gateway_run.GatewayRunner._load_service_tier() == expected


@pytest.mark.parametrize("raw, expected", PARSE_TABLE)
def test_serve_loader_matches_table(raw, expected):
    import tui_gateway.server as server

    with patch.object(server, "_load_cfg", return_value={"agent": {"service_tier": raw}}):
        assert server._load_service_tier() == expected


def test_unknown_word_warns_but_normal_words_do_not(caplog):
    import cli

    with caplog.at_level(logging.WARNING):
        assert cli._parse_service_tier_config("turbo") is None
    assert any("Unknown service_tier 'turbo'" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for word in ("", "normal", "fast", "ultrafast"):
            cli._parse_service_tier_config(word)
    assert not any("Unknown service_tier" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "tier, word",
    [(None, "normal"), ("", "normal"), ("priority", "fast"), ("ultrafast", "ultrafast")],
)
def test_service_tier_word_round_trips(tier, word):
    from hermes_cli.fast_mode_contracts import parse_service_tier, service_tier_word

    assert service_tier_word(tier) == word
    assert parse_service_tier(word) == (tier or None)
