"""Codex owner store: a borrowed-source row (e.g. ``mirror:*``, access-only)
must keep its 401 exhaustion, so an all-401 pool converges to "no entries"
and the fallback chain activates (t_f343f944).

Before the fix, ``to_dict()`` sanitized the row's tokens, so the owner
baseline never matched the disk generation: the exhausted status was never
persisted and ``sync()`` reset it from disk on every select. The pool handed
the same dead entry back forever (257 rotations in 63 s on ACE-AI, no
fallback, no route-change sink line).

Real filesystem + real pool + real recovery helper; nothing in the pool is
mocked. HTTP is not reached: the rows are access-only, so refresh is refused
before any POST.
"""

import json
import os
import types

import pytest

import agent.auxiliary_client as ac
import agent.credential_pool as cp
import hermes_cli.auth as auth
from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason

P = "openai-codex"
DEAD = "t_f343f944-invalid-access-token"


def mirror_row(ident, label, token=DEAD):
    # Shape of the live clanker row: pushed by an external keeper, access-only
    # (no refresh_token), source outside the persistable set.
    return dict(
        id=ident,
        label=label,
        source="mirror:codex-token-push-aceai",
        auth_type="oauth",
        priority=0,
        base_url="https://chatgpt.com/backend-api/codex",
        access_token=token,
        last_refresh="2026-09-29T23:21:30Z",
        request_count=0,
        last_status="ok",
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(auth, "_auth_file_path", lambda: profile / "auth.json")
    monkeypatch.setattr(auth, "_auth_lock_path", lambda: profile / "auth.lock")
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: tmp_path / "root-auth.json")
    monkeypatch.setattr(cp, "_load_config_safe", lambda: {})
    ac.clear_runtime_main()
    yield profile
    ac.clear_runtime_main()


def write_store(profile, rows):
    (profile / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {P: rows}})
    )


def disk_rows(profile):
    return json.loads((profile / "auth.json").read_text())["credential_pool"][P]


def test_borrowed_row_exhaustion_persists_and_survives_sync(home):
    write_store(home, [mirror_row("clanker-codex-promax", "oauth-2 mirror")])
    pool = cp.load_pool(P)
    entry = pool.select()
    assert entry is not None and entry.access_token == DEAD

    nxt = pool.mark_exhausted_and_rotate(
        status_code=401, api_key_hint=entry.runtime_api_key, credential_id=entry.id
    )

    assert nxt is None, "sole 401'd entry must not be handed back"
    (row,) = disk_rows(home)
    assert row["last_status"] == cp.STATUS_EXHAUSTED
    assert row["last_error_code"] == 401
    # The token material on disk is untouched (never blanked by sanitizing).
    assert row["access_token"] == DEAD
    # A fresh select re-syncs from disk and still sees the cooldown.
    assert pool.select() is None
    assert cp.load_pool(P).select() is None


def _agent(pool):
    a = types.SimpleNamespace()
    a.provider = P
    a.model = "gpt-6-astra"
    a.base_url = "https://chatgpt.com/backend-api/codex"
    a.api_mode = "codex_responses"
    a._credential_pool = pool
    first = pool.select()
    a.api_key = first.runtime_api_key
    a._credential_pool_entry_id = first.id
    a.swaps = []

    def _swap(entry):
        from run_agent import SwapOutcome

        a.swaps.append(entry.id)
        a.api_key = entry.runtime_api_key
        a._credential_pool_entry_id = entry.id
        return SwapOutcome.SWAPPED

    a._swap_credential = _swap
    a._is_entitlement_failure = lambda *x, **k: False
    return a


def test_two_entry_pool_both_401_stops_rotating_and_fallback_writes_sink(home, monkeypatch):
    write_store(
        home,
        [
            mirror_row("clanker-codex-promax", "oauth-2 mirror"),
            mirror_row("second-seat", "oauth-4 mirror"),
        ],
    )
    pool = cp.load_pool(P)
    agent = _agent(pool)

    # Drive the recovery helper exactly as the conversation loop does: every
    # attempt 401s, and ``recovered=True`` means "retry the request".
    attempts = 0
    while attempts < 50:
        attempts += 1
        recovered, _ = recover_with_credential_pool(
            agent,
            status_code=401,
            has_retried_429=False,
            classified_reason=FailoverReason.auth,
            error_context={"message": "401 Unauthorized"},
        )
        if not recovered:
            break

    # One rotation (entry 1 -> entry 2), then entry 2 fails and nothing is left.
    assert attempts <= 3, f"rotation did not converge: {attempts} attempts, swaps={agent.swaps}"
    assert len(agent.swaps) <= 2
    assert all(r["last_status"] == cp.STATUS_EXHAUSTED for r in disk_rows(home))

    # With the pool exhausted, the loop's next step is fallback activation;
    # the real helper writes the durable route-change sink line.
    fb_client = types.SimpleNamespace(
        api_key="gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        _custom_headers=None,
        default_headers=None,
    )
    monkeypatch.setattr(ac, "resolve_provider_client", lambda provider, model, **kw: (fb_client, model), raising=False)
    import agent.chat_completion_helpers as cch

    monkeypatch.setattr(cch, "get_model_context_length", lambda *a, **k: 1_000_000, raising=False)
    fa = _fallback_ready(agent)
    assert try_activate_fallback(fa, reason=FailoverReason.auth) is True
    sink = home / "state" / "model-route-changes.log"
    lines = sink.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "failover openai-codex/gpt-6-astra" in lines[0]
    assert "gemini/gemini-3.5-flash-lite" in lines[0]


class _Compressor:
    def __init__(self):
        self.context_length = 272_000
        self.threshold_percent = 0.5

    def update_model(self, *, model, context_length, **kw):
        self.context_length = context_length


def _fallback_ready(a):
    """Add the attributes ``try_activate_fallback`` reads (see test_route_change_sink_e2e)."""
    a.reasoning_config = {"enabled": True, "effort": "low"}
    a.reasoning_effort = "low"
    a._config_context_length = 272_000
    a._fallback_activated = False
    a._transport_cache = {}
    a.context_compressor = _Compressor()
    a._primary_runtime = None
    a._fallback_index = 0
    a._rate_limited_until = 0.0
    a._fallback_chain = [{"provider": "gemini", "model": "gemini-3.5-flash-lite"}]
    a.fallback_model = list(a._fallback_chain)
    a._snapshot_primary_runtime = lambda: None
    a._restore_primary_runtime = lambda: None
    a._try_activate_fallback = lambda *x, **k: False
    a._anthropic_prompt_cache_policy = lambda **k: (False, False)
    a._ensure_lmstudio_runtime_loaded = lambda: None
    a._is_azure_openai_url = lambda u: False
    a._is_direct_openai_url = lambda u: False
    a._provider_model_requires_responses_api = lambda *x, **k: False
    a._buffer_status = lambda *x, **k: None
    a._replace_primary_openai_client = lambda **k: None
    a._announced = []
    a.status_callback = lambda kind, msg: a._announced.append((kind, msg))
    a._emit_status = lambda message: a._announced.append(("lifecycle", message))
    a._vprint = lambda *x, **k: None
    a.log_prefix = ""
    a._last_fallback_announced = None
    return a
