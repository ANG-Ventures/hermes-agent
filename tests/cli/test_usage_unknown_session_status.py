"""Cumulative CLI displays preserve unknown even when counters are zero."""
from types import SimpleNamespace
from unittest.mock import Mock
import datetime

import pytest


@pytest.mark.parametrize('unknown', [False, True])
def test_session_status_total(unknown):
    from cli import HermesCLI

    shell = HermesCLI.__new__(HermesCLI)
    shell._session_db = None
    shell.session_id = 'test-session'
    shell.session_start = datetime.datetime.now()
    shell.agent = SimpleNamespace(session_total_tokens=0, session_usage_unknown=unknown)
    shell._console_print = Mock()
    shell._show_session_status()
    text = shell._console_print.call_args.args[0]
    assert f"Tokens: {'unknown' if unknown else '0'}" in text


def test_zero_counter_unknown_total_is_visible():
    from cli import HermesCLI

    shell = HermesCLI.__new__(HermesCLI)
    shell.model = "test-model"
    shell.session_start = datetime.datetime.now()
    shell.conversation_history = []
    shell.agent = SimpleNamespace(
        model="test-model", session_total_tokens=0, session_usage_unknown=True,
        get_rate_limit_state=lambda: None,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000, compression_count=0
        ),
    )
    shell._status_bar_field_set_cache = frozenset({"total_tokens"})
    text = shell._build_status_bar_text(width=120)
    assert "Σunknown" in text
