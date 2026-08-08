"""Unit tests for hermes_cli.session_recap."""
from __future__ import annotations

import json


from hermes_cli.session_recap import build_recap


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text=None, tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_call(name, args):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _tool_result(content="ok"):
    return {"role": "tool", "content": content}










def test_tool_preview_length_truncates_long_user_prompt():
    long = "x " * 500
    out = build_recap([_user(long)])
    ask_line = [l for l in out.splitlines() if "Last ask" in l][0]
    assert len(ask_line) < 300  # truncated with ellipsis
    assert "…" in ask_line




def test_files_touched_are_backtick_wrapped():
    """Recap file paths must be inline-code wrapped.

    The gateway scans outbound message text for bare local file paths and
    auto-uploads matches as native attachments. An absolute/``~`` path in the
    recap would otherwise leak the file's contents into the chat. Wrapping in
    backticks puts the path inside an inline-code span, which the gateway's
    bare-path detector explicitly skips.
    """
    msgs = [
        _user("read the config"),
        _assistant(
            tool_calls=[_tool_call("read_file", {"path": "/etc/some_config.yaml"})]
        ),
        _tool_result(),
        _assistant("done"),
    ]
    out = build_recap(msgs)
    files_line = [l for l in out.splitlines() if "Files touched" in l][0]
    assert "`/etc/some_config.yaml`" in files_line
    # The path must not appear un-backticked: stripping all inline-code spans
    # from the line should leave no trace of the path.
    import re

    stripped = re.sub(r"`[^`\n]+`", "", files_line)
    assert "/etc/some_config.yaml" not in stripped


def test_recap_paths_survive_gateway_bare_path_detector():
    """End-to-end-ish: recap paths must live inside an inline-code span.

    Reproduces the original bug where ``/status`` attached config.yaml: a
    touched file outside the gateway cwd rendered as a bare absolute path and
    the gateway's outbound bare-path detector auto-uploaded it. That detector
    skips paths inside inline-code spans, so the structural guarantee we pin
    here is that every recap file path sits inside backticks.
    """
    import re

    msgs = [
        _user("read the config"),
        _assistant(
            tool_calls=[
                _tool_call("read_file", {"path": "/Users/nobody/.hermes/config.yaml"})
            ]
        ),
        _tool_result(),
        _assistant("done"),
    ]
    recap = build_recap(msgs)
    code_spans = [(m.start(), m.end()) for m in re.finditer(r"`[^`\n]+`", recap)]
    path_pos = recap.find("/Users/nobody/.hermes/config.yaml")
    assert path_pos != -1
    assert any(s <= path_pos < e for s, e in code_spans), (
        "recap file path must live inside an inline-code span so the gateway "
        "bare-path auto-attach detector skips it"
    )


def test_escape_sequences_sanitized_in_previews():
    """Recap previews must not carry raw terminal escapes (codex#31494 class)."""
    msgs = [
        _user("please \x1b[2J\x1b]0;pwned\x07 do the thing"),
        _assistant("done \x9b31m with it\x07"),
    ]
    out = build_recap(msgs)
    assert "\x1b" not in out
    assert "\x9b" not in out
    assert "\x07" not in out
    assert "do the thing" in out
    assert "with it" in out
