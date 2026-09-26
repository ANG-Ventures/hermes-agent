"""A kanban worker's provider pin is honored, and any swap reaches the board.

t_4fe0700a: a card pinned to ``openai-codex`` (spawned ``-m gpt-6-sol-900k
--provider openai-codex``) ran on claude-bpr. The codex credential was in
cooldown, ``resolve_runtime_provider`` raised ``AuthError``, and the CLI's
auth-time fallback silently switched to the profile's first fallback entry.
No board event recorded it, and the rate-limited close was charged to the
openai-codex route instead of the bridge pool that actually served it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="a",
                             model_override="gpt-6-sol-900k",
                             provider_override="openai-codex")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                     "VALUES (?, 'a', 'running', ?)", (tid, int(time.time())))
        run_id = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid, run_id


@pytest.fixture(autouse=True)
def _fresh_card_pin_cache():
    from hermes_cli import kanban_worker_route

    kanban_worker_route._card_pin_cache.clear()
    yield
    kanban_worker_route._card_pin_cache.clear()


def _unpin_card(tid):
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET model_override=NULL, provider_override=NULL WHERE id=?",
                     (tid,))
        conn.commit()


def _events(tid, kind):
    with kb.connect_closing() as conn:
        return [(r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (tid, kind))]


class _CLI(CLIAgentSetupMixin):
    def __init__(self, explicit_provider):
        self._explicit_provider = explicit_provider
        self._kanban_pin_rate_limited = None
        self.requested_provider = explicit_provider or "claude-app"
        self.model = "gpt-6-sol-900k"
        self._explicit_api_key = None
        self._explicit_base_url = None
        self._fallback_model = [{"provider": "claude-bpr", "model": "claude-opus-5-5"}]
        self.api_key = None
        self.base_url = None
        self.provider = self.requested_provider
        self.api_mode = "chat_completions"
        self.acp_command = None
        self.acp_args = []
        self.agent = None

    def _normalize_model_for_provider(self, provider):
        return False


def _resolver(monkeypatch, *, fail, calls):
    def resolve(requested=None, **_kw):
        calls.append(requested)
        if requested in fail:
            raise AuthError("Codex credential is in cooldown.", provider=requested,
                            code=CODEX_RATE_LIMITED_CODE, relogin_required=False)
        return {"provider": requested, "api_key": "k", "base_url": f"http://{requested}/v1",
                "api_mode": "chat_completions"}
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)


def test_pinned_worker_refuses_auth_fallback_and_records_it(board, monkeypatch):
    tid, run_id = board
    calls = []
    _resolver(monkeypatch, fail={"openai-codex"}, calls=calls)
    cli = _CLI("openai-codex")

    assert cli._ensure_runtime_credentials() is False
    assert calls == ["openai-codex"]  # the bridge fallback was never resolved
    assert cli.requested_provider == "openai-codex"
    assert cli._kanban_pin_rate_limited
    refused = _events(tid, "worker_route_pin_refused")
    assert [r for r, _ in refused] == [run_id]
    assert refused[0][1]["provider"] == "openai-codex"
    assert refused[0][1]["rate_limited"] is True
    assert _events(tid, "worker_route_substituted") == []


def test_pinned_cooldown_exits_retry_preserving(board):
    from hermes_cli.kanban_worker_exit import EXIT_CLASS_QUOTA, WorkerExit

    exc = WorkerExit({"failed": True, "failure_reason": "rate_limit",
                      "error": "Codex credential is in cooldown."})
    assert exc.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert exc.exit_class == EXIT_CLASS_QUOTA


def test_pinned_worker_talks_to_its_pinned_provider(board, monkeypatch):
    tid, _ = board
    calls = []
    _resolver(monkeypatch, fail=set(), calls=calls)
    cli = _CLI("openai-codex")

    assert cli._ensure_runtime_credentials() is True
    assert cli.provider == "openai-codex"
    assert cli.base_url == "http://openai-codex/v1"
    assert calls == ["openai-codex"]
    assert _events(tid, "worker_route_pin_refused") == []
    assert _events(tid, "worker_route_substituted") == []


def test_unpinned_worker_auth_fallback_is_recorded(board, monkeypatch):
    tid, run_id = board
    calls = []
    _resolver(monkeypatch, fail={"openai-codex"}, calls=calls)
    cli = _CLI(None)
    cli.requested_provider = "openai-codex"  # profile default, not a --provider pin

    assert cli._ensure_runtime_credentials() is True
    assert cli.provider == "claude-bpr"
    sub = _events(tid, "worker_route_substituted")
    assert [r for r, _ in sub] == [run_id]
    assert sub[0][1]["stage"] == "auth"
    assert sub[0][1]["from_provider"] == "openai-codex"
    assert sub[0][1]["to_provider"] == "claude-bpr"


def test_non_worker_keeps_silent_fallback(board, monkeypatch):
    tid, _ = board
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    calls = []
    _resolver(monkeypatch, fail={"openai-codex"}, calls=calls)
    cli = _CLI("openai-codex")  # interactive `--provider` keeps the fallback chain

    assert cli._ensure_runtime_credentials() is True
    assert cli.provider == "claude-bpr"
    assert _events(tid, "worker_route_substituted") == []


def test_circuit_charges_worker_side_substitution(board):
    """The rate-limited close is charged to the pool that SERVED it."""
    tid, run_id = board
    now = int(time.time())
    with kb.connect_closing() as conn:
        ends = [now - 250, now - 200, now - 150, now - 100, now - 50]
        for ended in ends:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'a', 'rate_limited', 'rate_limited', ?, ?)", (tid, ended - 60, ended))
        conn.commit()
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {}  # codex: not a pool
        rows = conn.execute("SELECT id FROM task_runs WHERE task_id=? AND outcome='rate_limited'",
                            (tid,)).fetchall()
        with kb.write_txn(conn):
            for (rid,) in rows:
                kb._append_event(conn, tid, "worker_route_substituted", {
                    "stage": "auth", "from_provider": "openai-codex",
                    "to_provider": "claude-bpr", "to_model": "claude-opus-5-5",
                }, run_id=rid)
        assert kb.rate_limit_circuits(conn, now=now, trip=5) == {"claude-bpr": now - 50 + 600}


def test_runtime_failover_is_recorded_on_the_run(board, tmp_path):
    """The mid-turn failover path (try_activate_fallback) also reaches the board."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent.chat_completion_helpers import try_activate_fallback
    from tests.run_agent.test_fallback_reasoning_override import _make_reasoning_agent

    tid, run_id = board
    agent = _make_reasoning_agent()
    agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
    client = SimpleNamespace(api_key="fb-key", base_url="https://api.anthropic.com",
                             _custom_headers={})
    with patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(client, "claude-opus-4-8")), \
            patch("agent.credential_pool.load_pool", return_value=None), \
            patch("hermes_cli.config.load_config",
                  return_value={"model": {"default": "claude-fable-5", "provider": "claude-apr"}}):
        assert try_activate_fallback(agent) is True

    sub = _events(tid, "worker_route_substituted")
    assert [r for r, _ in sub] == [run_id]
    assert sub[0][1]["stage"] == "runtime"
    assert (sub[0][1]["from_provider"], sub[0][1]["to_provider"]) == ("claude-apr", "claude-apx-1")


def test_lane_override_worker_keeps_auth_fallback(board, monkeypatch):
    """t_16642ede: a board-wide lane override spawns ``--provider`` but never
    writes it to the card, so it is not a pin: a failing primary auth still
    falls back, and the swap is recorded on the run."""
    tid, run_id = board
    _unpin_card(tid)
    calls = []
    _resolver(monkeypatch, fail={"openai-codex"}, calls=calls)
    cli = _CLI("openai-codex")  # --provider from the lane override

    assert cli._ensure_runtime_credentials() is True
    assert calls == ["openai-codex", "claude-bpr"]
    assert cli.provider == "claude-bpr"
    assert cli._kanban_pin_rate_limited is None
    assert _events(tid, "worker_route_pin_refused") == []
    sub = _events(tid, "worker_route_substituted")
    assert [r for r, _ in sub] == [run_id]
    assert (sub[0][1]["stage"], sub[0][1]["from_provider"], sub[0][1]["to_provider"]) == (
        "auth", "openai-codex", "claude-bpr")


def test_dispatch_rung_off_the_card_pin_keeps_auth_fallback(board, monkeypatch):
    """A capped-pool rung that spawned the card on a provider other than its
    pin is already off the pin; like the runtime check, it keeps fallback."""
    tid, run_id = board  # card pins openai-codex
    calls = []
    _resolver(monkeypatch, fail={"claude-apr"}, calls=calls)
    cli = _CLI("claude-apr")  # --provider from the dispatch fallback rung

    assert cli._ensure_runtime_credentials() is True
    assert cli.provider == "claude-bpr"
    assert _events(tid, "worker_route_pin_refused") == []
    assert [r for r, _ in _events(tid, "worker_route_substituted")] == [run_id]
