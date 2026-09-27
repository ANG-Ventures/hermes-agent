"""Kanban refuses single-sub pin routes at set time (t_141135aa, Ace 2026-09-25).

Kanban routes are worker lanes and ride the pools (claude-bpr / claude-apr).
A card or lane pinned to ``claude-apx-N`` / ``claude-bpx-N`` — or to a
pre-rename alias of one (``claude-api-proxy`` is claude-apx-0, Ace's personal
Mac sub) — is refused by every writer, with the pool alternative named.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.model_policy import pinned_sub_provider_error


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


PINS = [
    "claude-api-proxy", "claude-proxy", "claude-subscription-proxy", "claude-bridge",
    "claude-api-proxy-f3", "claude-proxy-f12", "claude-bridge-f7",
    "claude-api-proxy-failover2", "claude-bridge-fallback4",
    "claude-apx-0", "claude-apx-15", "claude-bpx-3",
]
POOLS = ["claude-bpr", "claude-apr", "openai-codex", None, "claude-apx", "claude-bridge-x"]


@pytest.mark.parametrize("provider", PINS)
def test_predicate_refuses_every_pin_spelling_and_names_the_pool(provider):
    err = pinned_sub_provider_error("claude-opus-5-5", provider)
    assert err and provider in err
    assert ("claude-bpr" in err) or ("claude-apr" in err)
    # the same pin carried as a provider/ prefix on the model string
    assert pinned_sub_provider_error(f"{provider}/claude-opus-5-5", None)
    # case-insensitive
    assert pinned_sub_provider_error("claude-opus-5-5", provider.upper())


@pytest.mark.parametrize("provider", POOLS)
def test_predicate_allows_pools_and_other_vendors(provider):
    assert pinned_sub_provider_error("claude-opus-5-5", provider) is None


def test_bridge_lane_names_bpr_and_proxy_lane_names_apr():
    assert "claude-bpr" in pinned_sub_provider_error("m", "claude-bpx-2")
    assert "claude-apr" in pinned_sub_provider_error("m", "claude-api-proxy")


def test_create_task_refuses_pin(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="claude-apx-0"):
            kb.create_task(conn, title="x", assignee="daedalus-opus",
                           model_override="claude-opus-5-5", provider_override="claude-apx-0")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_set_model_override_refuses_pin_and_keeps_existing_route(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="argus",
                             model_override="claude-opus-5-5", provider_override="claude-bpr")
        with pytest.raises(ValueError, match="pre-rename alias"):
            kb.set_model_override(conn, tid, "claude-opus-5-5", "claude-api-proxy")
        with pytest.raises(ValueError, match="claude-apx-15"):
            kb.set_model_override(conn, tid, "claude-apx-15/claude-opus-5-5")
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == ("claude-opus-5-5", "claude-bpr")
        events = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='model_override_set'",
            (tid,)).fetchone()[0]
        assert events == 0


def test_set_task_model_refuses_prefixed_pin(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="argus")
        with pytest.raises(ValueError, match="claude-bpx-3"):
            kb.set_task_model(conn, tid, "claude-bpx-3/claude-opus-5-5")


def test_lane_model_db_layer_refuses_pin(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="claude-apr"):
            kb.set_lane_model_override(conn, provider="claude-apx-0", model="claude-opus-5-5",
                                       expires_at=10**10, reason="r", assignee="daedalus-opus")
        assert kb.list_lane_model_overrides(conn, include_expired=True) == []


def test_lane_model_cli_refuses_pin_with_pool_hint(kanban_home, capsys):
    (kanban_home / "config.yaml").write_text(
        "providers:\n  claude-apx-0:\n    base_url: http://127.0.0.1:9999/v1\n    api_key: t\n",
        encoding="utf-8")
    out = kc.run_slash("lane-model set claude-apx-0/claude-opus-5-5 --ttl 1h --reason cap")
    captured = capsys.readouterr()
    text = (out or "") + captured.out + captured.err
    assert "claude-apr" in text, text
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn, include_expired=True) == []


def test_pool_route_still_writes(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="argus")
        assert kb.set_model_override(conn, tid, "claude-opus-5-5", "claude-bpr")
        kb.set_lane_model_override(conn, provider="claude-bpr", model="claude-opus-5-5",
                                   expires_at=10**10, reason="r", assignee="argus")
        assert kb.get_lane_model_override(conn, assignee="argus").provider == "claude-bpr"
