"""LCM active-context assembly must not emit consecutive assistant rows.

Root cause (2026-07-31): when LCM assembles the active replay context it drops
the token-heavy ``tool`` rows (``_sanitize_tool_pairs``) to shed scaffolding,
but never merges the assistant narration rows that become adjacent once the
tool results between them are gone. A long agentic turn (N tool steps, each
with its own narration preamble) therefore produced ~N consecutive bare
assistant rows in the persisted active context. On every subsequent load the
gateway's ``repair_message_sequence`` had to collapse them — which is why the
footer's raw ``message_count`` over-read the repaired ``len(history)`` the
hygiene valve enforced (1463 shown vs 1003 checked in the reported incident).

The active context LCM emits must already be alternation-clean: no two
consecutive assistant messages (the one exception the gateway repair honors is
Codex Responses interim turns, which carry encrypted continuation state and
must replay verbatim).

The raw store and DAG stay lossless independent of this — the granular rows
remain recoverable via lcm_grep / lcm_expand; this only sanitizes the active
replay context handed back to the provider.
"""

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine


def _engine(tmp_path, *, context_length=200_000):
    config = LCMConfig(
        database_path=str(tmp_path / "lcm.db"),
        fresh_tail_count=1,
        leaf_chunk_tokens=1,
        context_threshold=0.01,
    )
    engine = LCMEngine(config=config, hermes_home=str(tmp_path))
    engine.update_model("unit-test-model", context_length, provider="unit-test")
    engine.on_session_start(
        "session-1",
        hermes_home=str(tmp_path),
        model="unit-test-model",
        provider="unit-test",
        context_length=context_length,
        platform="pytest",
    )
    return engine


def _consecutive_assistant_runs(messages):
    """Count adjacent assistant/assistant pairs in a message sequence."""
    return sum(
        1
        for i in range(1, len(messages))
        if messages[i].get("role") == "assistant"
        and messages[i - 1].get("role") == "assistant"
    )


def _agentic_turn_with_tool_steps(n_steps):
    """The production shape LCM RE-INGESTS every turn: already-stripped bare
    assistant narration rows sitting adjacent.

    LCM's active-context assembly strips ``tool_calls`` off each assistant turn
    and drops the ``tool`` results (to shed token-heavy scaffolding), then
    PERSISTS that stripped set. On the next turn it reloads those bare rows and
    runs them back through ``_sanitize_active_context_messages`` — where they
    arrive as consecutive bare assistants (no tool_calls, finish_reason=None),
    exactly as observed in the live state.db (60 active rows / 52 adjacencies).
    Each row carries no tool_calls, so ``_sanitize_tool_pairs`` leaves the
    adjacency untouched — the merge is what closes it.
    """
    msgs: list[dict] = [
        {"role": "system", "content": "You are testing LCM."},
        {"role": "user", "content": "Do the multi-step task."},
    ]
    for i in range(n_steps):
        msgs.append(
            {
                "role": "assistant",
                "content": f"Step {i}: let me run the next probe.",
                "finish_reason": None,
            }
        )
    return msgs


def test_sanitize_active_context_has_no_consecutive_assistants(tmp_path):
    """After tool-pair sanitize drops tool rows, adjacent assistants must merge."""
    engine = _engine(tmp_path)
    # 6 tool steps → 6 assistant(tool_calls) rows, each followed by a tool row.
    messages = _agentic_turn_with_tool_steps(6)

    sanitized = engine._sanitize_active_context_messages(
        messages, insert_missing_tool_stubs=False
    )

    # The bug: _sanitize_tool_pairs drops the tool rows (insert_missing_tool_stubs
    # =False), leaving 6 consecutive assistant rows. The fix must merge them.
    assert _consecutive_assistant_runs(sanitized) == 0, (
        "active context emitted consecutive assistant rows: "
        f"{[m.get('role') for m in sanitized]}"
    )


def test_merged_assistant_preserves_all_narration_text(tmp_path):
    """Merging must concatenate content — no narration step may be lost."""
    engine = _engine(tmp_path)
    messages = _agentic_turn_with_tool_steps(4)

    sanitized = engine._sanitize_active_context_messages(
        messages, insert_missing_tool_stubs=False
    )

    merged_text = "\n".join(
        m["content"]
        for m in sanitized
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    )
    for i in range(4):
        assert f"Step {i}:" in merged_text, f"lost narration for step {i}"


def test_codex_interim_turns_are_not_merged(tmp_path):
    """Codex Responses interim turns carry encrypted continuation state and must
    replay verbatim — the merge must exempt them (same as the gateway repair)."""
    engine = _engine(tmp_path)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "interim 1",
            "codex_reasoning_items": [{"id": "rs_1"}],
            "finish_reason": "incomplete",
        },
        {
            "role": "assistant",
            "content": "interim 2",
            "codex_reasoning_items": [{"id": "rs_2"}],
            "finish_reason": "incomplete",
        },
    ]

    sanitized = engine._sanitize_active_context_messages(
        messages, insert_missing_tool_stubs=False
    )

    interim = [
        m
        for m in sanitized
        if m.get("role") == "assistant" and m.get("codex_reasoning_items")
    ]
    assert len(interim) == 2, "codex interim turns must NOT be merged"
