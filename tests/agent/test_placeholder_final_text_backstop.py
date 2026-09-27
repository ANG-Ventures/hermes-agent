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


def test_bridge_closer_proceeding_is_a_known_placeholder():
    assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=True, already_nudged=False) == "empty"
    assert cl.classify_placeholder_final_text(PROCEEDING + "\n", prior_was_tool=True, already_nudged=False) == "empty"
    assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=True, already_nudged=True) == "notice"
    assert cl.classify_placeholder_final_text(PROCEEDING, prior_was_tool=False, already_nudged=False) == "notice"


def test_unknown_closer_shape_after_tools_gets_one_nudge_never_a_notice():
    # <= 3 words ending in "." right after tool results: auto-continue once...
    for text in ("Continuing.", "Tool results received.", "OK."):
        assert cl.classify_placeholder_final_text(text, prior_was_tool=True, already_nudged=False) == "empty", text
        # ...but a repeat is delivered as-is: a real short reply must reach the user
        assert cl.classify_placeholder_final_text(text, prior_was_tool=True, already_nudged=True) is None, text
        # and without tool results it is an ordinary reply
        assert cl.classify_placeholder_final_text(text, prior_was_tool=False, already_nudged=False) is None, text


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
    assert PROCEEDING in cl._PLACEHOLDER_FINAL_TEXTS


def _loop_source():
    return pathlib.Path(cl.__file__).read_text()


def test_backstop_is_wired_at_the_final_text_seam():
    """The classifier must run where ``final_response`` is derived from the
    assistant message, BEFORE the partial-stream recovery / empty ladder, and
    the empty route must clear the streamed buffer so partial-stream recovery
    cannot resurrect the placeholder. Mutation-proven: remove the wiring block
    and this fails."""
    src = _loop_source()
    seam = 'final_response = assistant_message.content or ""'
    i = src.index(seam)
    window = src[i:i + 2500]
    assert "classify_placeholder_final_text(" in window, "classifier not called at the final-text seam"
    assert "_TURN_ENDED_WITHOUT_REPLY" in window, "notice route not wired"
    assert 'agent._current_streamed_assistant_text = ""' in window, "streamed buffer not cleared (partial-stream recovery would resurrect the placeholder)"
    # the wiring precedes the partial-stream recovery block (which is the
    # first consumer of the streamed buffer after the seam)
    call_at = i + window.index("classify_placeholder_final_text(")
    assert call_at < src.index("Partial stream recovery", i)


def test_classifier_signature_is_keyword_only():
    tree = ast.parse(_loop_source())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "classify_placeholder_final_text")
    assert [a.arg for a in fn.args.kwonlyargs] == ["prior_was_tool", "already_nudged"]
