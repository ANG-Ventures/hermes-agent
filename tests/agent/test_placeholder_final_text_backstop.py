"""Backstop for a known placeholder final text (card t_887f9584).

The Claude Code CLI closes a user-record transcript tail with a synthetic
assistant "No response requested."; a CLI-backed provider can surface it as
the whole final text and a model that has seen it in its own history can echo
it. Delivered verbatim it reads like a reply. The backstop routes it into the
existing once-only post-tool nudge (auto-continue once) and, failing that,
replaces it with a visible notice. Empty text and "[SILENT]" are NOT
placeholders — silent cron / no_agent turns keep their existing path.
"""

import ast
import pathlib

from agent import conversation_loop as cl

PLACEHOLDER = "No response requested."
# claude-bpx#248's text closer, echoed live on 2026-09-27 17:49Z (sub-vps-25).
PROCEEDING = "Proceeding."


def test_placeholder_after_tools_is_treated_as_empty_once():
    assert cl.classify_placeholder_final_text(PLACEHOLDER, prior_was_tool=True, already_nudged=False) == "empty"
    # whitespace / trailing newline variants are the same placeholder
    assert cl.classify_placeholder_final_text(PLACEHOLDER + "\n", prior_was_tool=True, already_nudged=False) == "empty"
    assert cl.classify_placeholder_final_text("  " + PLACEHOLDER + "  ", prior_was_tool=True, already_nudged=False) == "empty"


def test_bridge_closer_proceeding_is_a_known_placeholder_on_bridge_lanes():
    for prov in ("claude-bpr", "claude-bpx-8", "claude-bpx-16"):
        kw = {"provider": prov}
        assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=True, already_nudged=False, **kw) == "empty"
        assert cl.classify_placeholder_final_text(PROCEEDING + "\n", prior_was_tool=True, already_nudged=False, **kw) == "empty"
        assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=True, already_nudged=True, **kw) == "notice"
        assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=False, already_nudged=False, **kw) == "notice"


def test_proceeding_is_a_real_reply_on_every_other_provider():
    # FleetReview #1332 key 9172f2d63927 (t_4a1853f2): "Proceeding." is a valid
    # answer; only the claude-bpx bridge ever wrote it as a closer.
    for prov in (None, "", "anthropic", "openrouter", "openai-codex", "claude-apr",
                 "claude-apx-3", "claude-bpx", "claude-bpx-x", "my-claude-bpr"):
        for nudged in (False, True):
            for prior in (True, False):
                assert cl.classify_placeholder_final_text(
                    PROCEEDING, prior_was_tool=prior, already_nudged=nudged, provider=prov
                ) is None, (prov, prior, nudged)


def test_legacy_bridge_closer_in_history_survives_provider_switch():
    history = [
        {"role": "user", "content": "Run the tools"},
        {"role": "assistant", "content": PROCEEDING},
        {"role": "user", "content": "What happened?"},
    ]
    for prov in ("claude-apr", "openai-codex"):
        assert cl.classify_placeholder_final_text(
            PROCEEDING, prior_was_tool=True, already_nudged=False,
            provider=prov, history=history,
        ) == "empty"
        assert cl.classify_placeholder_final_text(
            PROCEEDING, prior_was_tool=False, already_nudged=False,
            provider=prov, history=history,
        ) == "notice"


def test_nonbridge_proceeding_with_no_assistant_closer_remains_a_reply():
    history = [{"role": "user", "content": PROCEEDING}]
    assert cl.classify_placeholder_final_text(
        PROCEEDING, prior_was_tool=True, already_nudged=False,
        provider="claude-apr", history=history,
    ) is None


def test_switch_seam_passes_retained_history():
    src = _loop_source()
    i = src.index("classify_placeholder_final_text(\n", src.index('final_response = _promoted or assistant_message.content or ""'))
    assert "history=messages" in src[i:i + 450]


def test_cli_closer_stays_global_for_every_provider():
    for prov in (None, "anthropic", "claude-bpr", "openrouter"):
        assert cl.classify_placeholder_final_text(
            PLACEHOLDER, prior_was_tool=True, already_nudged=False, provider=prov
        ) == "empty"


def test_seam_passes_the_agent_provider():
    src = _loop_source()
    i = src.index("classify_placeholder_final_text(\n", src.index('final_response = _promoted or assistant_message.content or ""'))
    assert 'provider=getattr(agent, "provider", None)' in src[i:i + 400]


def test_short_real_reply_after_tools_is_never_nudged():
    # t_96cf66d2: a shape rule (<= 3 words ending in ".") nudged "Done." and
    # cost an extra model call per short post-tool answer; it ejected every
    # merge-queue tree carrying it. Only exact known closers match.
    for text in ("Done.", "OK.", "Continuing.", "Tool results received."):
        for nudged in (False, True):
            for prior in (True, False):
                assert cl.classify_placeholder_final_text(
                    text, prior_was_tool=prior, already_nudged=nudged
                ) is None, (text, prior, nudged)


def test_negative_control_longer_or_unpunctuated_short_replies_are_untouched():
    for text in ("The sum is eight.", "Done", "Yes!", "Deployed to all boxes"):
        assert cl.classify_placeholder_final_text(text, prior_was_tool=True, already_nudged=False) is None, text


def test_placeholder_repeated_after_nudge_becomes_visible_notice():
    assert cl.classify_placeholder_final_text(PLACEHOLDER, prior_was_tool=True, already_nudged=True) == "notice"


def test_placeholder_with_no_tool_results_becomes_visible_notice():
    # nothing to re-process: the once-only nudge would be pointless
    assert cl.classify_placeholder_final_text(PLACEHOLDER, prior_was_tool=False, already_nudged=False) == "notice"


def test_negative_control_empty_and_silent_are_not_placeholders():
    for text in ("", "   ", "\n", "[SILENT]", "(empty)", None):
        assert cl.classify_placeholder_final_text(text, prior_was_tool=True, already_nudged=False) is None, text
        assert cl.classify_placeholder_final_text(text, prior_was_tool=False, already_nudged=True) is None, text


def test_negative_control_real_text_mentioning_the_placeholder_is_untouched():
    real = 'Sorry, that "No response requested" was a mistake. Here is the status.'
    assert cl.classify_placeholder_final_text(real, prior_was_tool=True, already_nudged=False) is None
    assert cl.classify_placeholder_final_text("No response requested. Here is why: ...", prior_was_tool=True, already_nudged=False) is None


def test_notice_text_is_the_documented_string():
    assert cl._TURN_ENDED_WITHOUT_REPLY == "(turn ended without a reply)"
    assert PLACEHOLDER in cl._PLACEHOLDER_FINAL_TEXTS
    assert PROCEEDING not in cl._PLACEHOLDER_FINAL_TEXTS
    assert PROCEEDING in cl._BRIDGE_CLOSER_FINAL_TEXTS


def _loop_source():
    # The final-text seam lives in agent/turn_final_response.py since upstream
    # decomposed run_conversation into turn_* phase modules (parity 2026-10-01).
    from agent import turn_final_response

    return pathlib.Path(turn_final_response.__file__).read_text()


def test_backstop_is_wired_at_the_final_text_seam():
    """The classifier must run where ``final_response`` is derived from the
    assistant message, BEFORE the partial-stream recovery / empty ladder, and
    the empty route must clear the streamed buffer so partial-stream recovery
    cannot resurrect the placeholder. Mutation-proven: remove the wiring block
    and this fails."""
    src = _loop_source()
    seam = 'final_response = _promoted or assistant_message.content or ""'
    i = src.index(seam)
    window = src[i:i + 2500]
    assert "classify_placeholder_final_text(" in window, "classifier not called at the final-text seam"
    assert "_TURN_ENDED_WITHOUT_REPLY" in window, "notice route not wired"
    assert 'agent._current_streamed_assistant_text = ""' in window, "streamed buffer not cleared (partial-stream recovery would resurrect the placeholder)"
    # the wiring precedes the empty-response recovery call (recover_empty_response
    # in agent/turn_empty_response.py hosts the partial-stream recovery, the first
    # consumer of the streamed buffer after the seam)
    call_at = i + window.index("classify_placeholder_final_text(")
    assert call_at < src.index("recover_empty_response(", i)
    from agent import turn_empty_response
    assert "Partial stream recovery" in pathlib.Path(turn_empty_response.__file__).read_text()


def test_classifier_signature_is_keyword_only():
    tree = ast.parse(pathlib.Path(cl.__file__).read_text())  # the classifier itself stays on the facade
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "classify_placeholder_final_text")
    assert [a.arg for a in fn.args.kwonlyargs] == ["prior_was_tool", "already_nudged", "provider", "history"]
