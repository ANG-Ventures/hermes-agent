"""C7 backfill, hermes-agent slice C1 (t_dc31b7bf): durable-state defects in agent/.

One regression per confirmed finding; the k-id is the row in the C7 adjudication.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.usage_pricing import normalize_usage


# ── k74 / k75: native Anthropic aux usage keeps its cache buckets ────────────


def _anthropic_aux_response(wire_usage):
    from agent.auxiliary_client import _AnthropicCompletionsAdapter
    from agent.transports.types import NormalizedResponse

    adapter = _AnthropicCompletionsAdapter(MagicMock(name="anthropic_client"),
                                           "claude-sonnet-4-6", is_oauth=False)
    normalized = NormalizedResponse(content="ok", tool_calls=None, reasoning=None,
                                    finish_reason="stop")
    with patch("agent.anthropic_adapter.create_anthropic_message",
               return_value=SimpleNamespace(usage=wire_usage)), \
            patch("agent.transports.get_transport") as get_transport:
        get_transport.return_value.normalize_response.return_value = normalized
        return adapter.create(model="claude-sonnet-4-6",
                              messages=[{"role": "user", "content": "hi"}], max_tokens=64)


def test_anthropic_aux_adapter_preserves_cache_read_and_write():
    wire = SimpleNamespace(input_tokens=120, output_tokens=40,
                           cache_read_input_tokens=9000, cache_creation_input_tokens=700)
    usage = _anthropic_aux_response(wire).usage
    # Both aux consumers (aux_accounting ledger, compaction cost sink) read
    # OpenAI-shaped usage as the chat_completions dialect.
    canonical = normalize_usage(usage, provider="", api_mode="chat_completions")
    assert canonical.input_tokens == 120
    assert canonical.output_tokens == 40
    assert canonical.cache_read_tokens == 9000
    assert canonical.cache_write_tokens == 700


def test_anthropic_aux_adapter_without_cache_fields_is_unchanged():
    wire = SimpleNamespace(input_tokens=120, output_tokens=40)
    usage = _anthropic_aux_response(wire).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (120, 40, 160)
    canonical = normalize_usage(usage, provider="", api_mode="chat_completions")
    assert (canonical.cache_read_tokens, canonical.cache_write_tokens) == (0, 0)


def test_compaction_cost_prices_the_cache_buckets():
    from agent.auxiliary_client import _aux_cost_sink, _record_aux_call_cost
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    wire = SimpleNamespace(input_tokens=120, output_tokens=40,
                           cache_read_input_tokens=9000, cache_creation_input_tokens=700)
    response = _anthropic_aux_response(wire)
    sink = {}
    token = _aux_cost_sink.set(sink)
    try:
        _record_aux_call_cost(response, {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                              streamed=False)
    finally:
        _aux_cost_sink.reset(token)
    want = estimate_usage_cost(
        "claude-sonnet-4-6",
        CanonicalUsage(input_tokens=120, output_tokens=40,
                       cache_read_tokens=9000, cache_write_tokens=700),
        provider="anthropic",
    ).amount_usd
    assert want is not None and not sink.get("unknown"), sink
    assert abs(sink["usd"] - float(want)) < 1e-9, (sink["usd"], want)


# ── k80: an otherwise-unrecognized 403 failover is auth ──────────────────────


def test_bare_403_classifies_as_auth():
    from agent.fallback_events import classify_text

    assert classify_text("Forbidden", http_status=403) == "auth"
    assert classify_text("Forbidden", http_status=401) == "auth"
    # Text precedence is kept: a 403 that names a rate limit is still that.
    assert classify_text("rate limit exceeded", http_status=403) == "rate_upstream"
    assert classify_text("Forbidden", http_status=500) == "unclassified"


# ── k83: hook restore never writes through a planted temp-name symlink ──────


def test_publish_absent_does_not_follow_a_planted_symlink(tmp_path, monkeypatch):
    import threading

    from agent import shell_hooks_missing as shm

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("precious\n")
    os.chmod(outside, 0o600)
    dest = hooks / "policy.py"
    # The name the pre-fix code derived from pid + thread id.
    planted = dest.with_name(f".{dest.name}.hook-restore-{os.getpid()}-{threading.get_ident()}")
    planted.symlink_to(outside)

    assert shm._publish_absent(dest, b"print('restored')\n", 0o755) is True

    assert outside.read_text() == "precious\n"
    assert (os.stat(outside).st_mode & 0o777) == 0o600
    assert dest.read_bytes() == b"print('restored')\n"
    assert (os.stat(dest).st_mode & 0o777) == 0o755
    assert planted.is_symlink()  # someone else's file: left alone
    assert sorted(p.name for p in hooks.iterdir()) == sorted([dest.name, planted.name])


# ── k84: a turn with no provider call and no compaction has no usage row ────


def _session_end_usage(agent, turn_calls):
    from agent import turn_finalizer

    seen = []
    with patch("hermes_cli.lifecycle.invoke_hook",
               lambda name, **kw: seen.append(kw.get("turn_usage")) or []):
        turn_finalizer.emit_session_end(
            agent, turn_id="t1", effective_task_id="task", completed=False,
            failed=False, interrupted=True, turn_exit_reason="interrupted",
            original_user_message="hi", final_response="", turn_calls=turn_calls,
        )
    assert len(seen) == 1
    return seen[0]


def _bare_agent(compaction):
    return SimpleNamespace(session_id="s", model="m", platform="cli", provider="p",
                           _blackbox_compaction=compaction)


def test_callless_turn_without_compaction_reports_no_usage():
    # turn_context seeds exactly this dict on every turn.
    assert _session_end_usage(_bare_agent({"idle_compaction_fired": False}), []) is None


def test_callless_turn_with_real_compaction_still_reports_it():
    comp = {"idle_compaction_fired": True, "compaction_tokens_before": 900,
            "compaction_tokens_after": 300}
    assert _session_end_usage(_bare_agent(comp), []) == comp


# ── k86: the saved handoff is fsynced before it is published ────────────────


def test_turn_handoff_write_is_fsynced_before_replace(tmp_path, monkeypatch):
    from agent import turn_handoff

    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd):
        events.append(("fsync", fd))
        return real_fsync(fd)

    def _replace(src, dst):
        events.append(("replace", None))
        return real_replace(src, dst)

    monkeypatch.setattr(turn_handoff.os, "fsync", _fsync)
    monkeypatch.setattr(turn_handoff.os, "replace", _replace)
    handoff = {"version": 1, "created_at": __import__("time").time(), "reason": "x"}
    assert turn_handoff.write_turn_handoff("chat:1", handoff, root=tmp_path) is True
    kinds = [k for k, _ in events]
    assert "replace" in kinds and "fsync" in kinds[: kinds.index("replace")], events
    # ... and the directory entry is synced after the rename.
    assert "fsync" in kinds[kinds.index("replace") + 1:], events
    assert turn_handoff.peek_turn_handoff("chat:1", root=tmp_path)["reason"] == "x"
