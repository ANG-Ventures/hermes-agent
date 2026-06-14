from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
# rtk-rewrite is a home-local (gitignored) plugin; in this isolated worktree its
# work-in-progress copy lives under staging/ (deployed to ~/.hermes/plugins/ at release).
RTK_PLUGIN = REPO_ROOT / "staging" / "plugins" / "rtk-rewrite" / "__init__.py"


def load_rtk_module():
    assert RTK_PLUGIN.exists(), f"rtk-rewrite plugin missing at {RTK_PLUGIN}"
    spec = importlib.util.spec_from_file_location("test_rtk_rewrite_plugin", RTK_PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("test_rtk_rewrite_plugin", None)
    sys.modules["test_rtk_rewrite_plugin"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rtk(monkeypatch):
    module = load_rtk_module()
    monkeypatch.setattr(module, "_compression_debug_enabled", lambda: False)
    monkeypatch.setattr(module, "_emit_blackbox_event", lambda args, event: None)
    return module


def _set_compression_debug_config(tmp_path, monkeypatch, enabled: bool):
    home = tmp_path / ("debug-on" if enabled else "debug-off")
    home.mkdir()
    (home / "config.yaml").write_text(
        "compression:\n  debug_log: " + ("true" if enabled else "false") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_COMPRESSION_DEBUG_LOG", raising=False)
    import hermes_constants

    importlib.reload(hermes_constants)
    return home


def _rewrite_to(text: str, rc: int = 3):
    return subprocess.CompletedProcess(["rtk", "rewrite", text], rc, text, "")


def _mock_rewrite(monkeypatch, rtk, stdout="rtk git status", rc=3):
    monkeypatch.setattr(
        rtk.subprocess,
        "run",
        lambda *a, **k: _rewrite_to(stdout, rc=rc),
    )


def test_debug_flag_on_emits_structured_line(tmp_path, monkeypatch, caplog):
    _set_compression_debug_config(tmp_path, monkeypatch, True)
    rtk = load_rtk_module()
    monkeypatch.setattr(rtk, "_emit_blackbox_event", lambda args, event: None)
    _mock_rewrite(monkeypatch, rtk)
    args = {"command": "git status"}

    with caplog.at_level(logging.DEBUG, logger=rtk.logger.name):
        rtk._pre_tool_call(tool_name="terminal", args=args, tool_call_id="tc-1")

    messages = [r.getMessage() for r in caplog.records if r.name == rtk.logger.name]
    assert len(messages) == 1
    assert messages[0].startswith("rtk_rewrite_command ")
    payload = json.loads(messages[0].split(" ", 1)[1])
    assert payload["cmd"] == "git status"
    assert payload["outcome"] == "rewritten"
    assert payload["rc"] == 3
    assert payload["input_bytes"] == len(b"git status")
    assert payload["output_bytes"] == len(b"rtk git status")
    assert payload["tool_call_id"] == "tc-1"


def test_debug_flag_off_is_silent(tmp_path, monkeypatch, caplog):
    _set_compression_debug_config(tmp_path, monkeypatch, False)
    rtk = load_rtk_module()
    monkeypatch.setattr(rtk, "_emit_blackbox_event", lambda args, event: None)
    _mock_rewrite(monkeypatch, rtk)
    args = {"command": "git status"}

    with caplog.at_level(logging.DEBUG, logger=rtk.logger.name):
        rtk._pre_tool_call(tool_name="terminal", args=args)

    assert [r for r in caplog.records if r.name == rtk.logger.name] == []
    assert args["command"] == "rtk git status"


def test_logger_debug_raise_does_not_change_rewrite_bytes(rtk, monkeypatch):
    _mock_rewrite(monkeypatch, rtk)
    args = {"command": "git status"}
    rtk._pre_tool_call(tool_name="terminal", args=args)
    baseline = args["command"].encode("utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("debug sink down")

    monkeypatch.setattr(rtk, "_compression_debug_enabled", lambda: True)
    monkeypatch.setattr(rtk.logger, "debug", boom)
    args = {"command": "git status"}
    rtk._pre_tool_call(tool_name="terminal", args=args)

    assert args["command"].encode("utf-8") == baseline


def test_blackbox_emit_raise_does_not_change_rewrite_bytes(monkeypatch):
    import plugins.blackbox as blackbox

    rtk = load_rtk_module()
    _mock_rewrite(monkeypatch, rtk)
    monkeypatch.setattr(rtk, "_compression_debug_enabled", lambda: False)
    args = {"command": "git status"}
    rtk._pre_tool_call(tool_name="terminal", args=args)
    baseline = args["command"].encode("utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("blackbox sink down")

    monkeypatch.setattr(blackbox, "emit_compression_tool_event", boom)
    args = {"command": "git status"}
    rtk._pre_tool_call(tool_name="terminal", args=args)

    assert args["command"].encode("utf-8") == baseline


@pytest.fixture
def blackbox_home(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    import plugins.blackbox as blackbox
    import plugins.blackbox.store as store

    importlib.reload(hermes_constants)
    importlib.reload(store)
    importlib.reload(blackbox)
    blackbox._sessions.clear()
    monkeypatch.setattr(
        blackbox,
        "_config",
        lambda: {
            "enabled": True,
            "cost_alert_threshold_usd": 999.0,
            "store_text": True,
            "record_subagents": True,
            "alerts_enabled": False,
        },
    )
    monkeypatch.setattr(blackbox, "compute_turn_cost", lambda *a, **k: (0.0, "estimated"))
    monkeypatch.setattr(blackbox, "_turn_id", lambda: "turn_scrub")
    monkeypatch.setitem(sys.modules, "plugins.blackbox.store", store)
    return home, blackbox, store


def _secret_command() -> tuple[str, list[str]]:
    op_ref = "op://" + "Engineering" + "/Hermes API/password"
    aws_key = "AKIA" + ("A" * 16)
    bearer_value = "bearer" + ("B" * 24)
    auth_header = "Authorization: " + "Bearer " + bearer_value
    key_name = "AWS" + "_ACCESS" + "_KEY" + "_ID="
    cmd = (
        "git status && op read "
        + "'"
        + op_ref
        + "'"
        + " && "
        + key_name
        + aws_key
        + " curl -H '"
        + auth_header
        + "' https://example.invalid"
    )
    return cmd, [op_ref, aws_key, bearer_value]


def test_command_scrubbed_in_debug_log_and_blackbox_db(monkeypatch, caplog, blackbox_home):
    home, blackbox, store = blackbox_home
    rtk = load_rtk_module()
    command, raw_values = _secret_command()
    rewritten = "rtk " + command
    monkeypatch.setattr(
        rtk.subprocess,
        "run",
        lambda *a, **k: _rewrite_to(rewritten, rc=3),
    )
    monkeypatch.setattr(rtk, "_compression_debug_enabled", lambda: True)

    blackbox._on_session_start(session_id="s-scrub")
    args = {"command": command}
    with caplog.at_level(logging.DEBUG, logger=rtk.logger.name):
        rtk._pre_tool_call(tool_name="terminal", args=args)
    blackbox._on_post_tool_call(
        session_id="s-scrub",
        tool_name="terminal",
        args=args,
        result="ok",
    )
    blackbox._on_session_end(
        session_id="s-scrub",
        model="m",
        platform="cli",
        provider="p",
        turn_usage={"api_calls": 1, "calls": []},
    )

    log_text = "\n".join(r.getMessage() for r in caplog.records if r.name == rtk.logger.name)
    calls = store.get_tool_calls("turn_scrub")
    assert len(calls) == 1
    args_preview = calls[0]["args_preview"]
    db_text = (home / "blackbox" / "turns.db").read_bytes().decode("utf-8", errors="ignore")

    assert "rtk_rewrite_command" in log_text
    assert '"compression"' in args_preview
    assert '"outcome": "rewritten"' in args_preview
    assert "op://***" in log_text
    assert "op://***" in args_preview
    assert "Bearer ***" in log_text
    assert "Bearer ***" in args_preview
    for raw in raw_values:
        assert raw not in log_text
        assert raw not in args_preview
        assert raw not in db_text
