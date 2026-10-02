"""P2 backfill remainder (t_cd88e043): small TRUE-at-HEAD rows, each red on base."""
import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]


# --- #47: a submodule gitfile (.git/modules/<x>) is not a dead linked worktree
def test_missing_submodule_gitdir_is_not_reported_as_dead_worktree(tmp_path):
    from hermes_cli import kanban_survivor as survivor

    sub = tmp_path / "ws" / "vendor" / "lib"
    sub.mkdir(parents=True)
    gone = tmp_path / "main" / ".git" / "modules" / "vendor" / "lib"
    (sub / ".git").write_text(f"gitdir: {gone}\n")
    # Must not raise the "dead linked worktree stub" refusal.
    assert survivor._explain_dead_worktree_stub(sub, "vendor/lib") is None

    # Control: the same shape under .git/worktrees/ still refuses.
    wt = tmp_path / "ws" / "baseline"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {tmp_path / 'main' / '.git' / 'worktrees' / 'baseline'}\n")
    with pytest.raises(survivor.SurvivorUnavailable, match="dead linked worktree stub"):
        survivor._explain_dead_worktree_stub(wt, "baseline")


# --- #64: the npm-install remedy must survive shell re-lexing
@pytest.mark.asyncio
async def test_whatsapp_npm_remedy_quotes_bridge_dir():
    from gateway.config import Platform
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = 19876
    adapter._bridge_script = "/tmp/wa bridge;x/bridge.js"
    adapter._session_path = Path("/tmp/test-wa-session")
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._reply_prefix = None
    adapter._send_read_receipts = False
    adapter._running = False
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._platform_lock_identity = None

    def _exists(path_obj):
        return not str(path_obj).endswith("node_modules")

    # parity 2026-10-01: upstream's connect() admits node through PM (pm.ensure) before the
    # preflight; stub both so the npm-install failure under test is the path that runs.
    with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
         patch("plugins.platforms.whatsapp.adapter.find_node_executable", return_value="/usr/bin/node"), \
         patch.object(Path, "exists", autospec=True, side_effect=_exists), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="x")), \
         patch("gateway.status.acquire_scoped_lock", return_value=(True, None)), \
         patch("gateway.status.release_scoped_lock"):
        assert await adapter.connect() is False
    msg = adapter.fatal_error_message or ""
    assert "cd '/tmp/wa bridge;x' && " in msg, msg


# --- #86: /kanban dashboard must not read config.yaml on the event loop
def test_kanban_dashboard_link_runs_off_the_loop_thread(monkeypatch):
    from gateway.slash_commands import GatewaySlashCommandsMixin
    import gateway.kanban_dashboard_link as link_mod

    seen = {}

    def fake_link(session_id):
        seen["thread"] = threading.get_ident()
        return f"https://example.test/kanban?session={session_id}"

    monkeypatch.setattr(link_mod, "dashboard_link", fake_link)

    async def entry_for(_key):
        return SimpleNamespace(session_id="s1")

    gateway = SimpleNamespace(
        async_session_store=SimpleNamespace(entry_for=entry_for),
        _session_key_for_source=lambda _source: "chat-key",
    )
    event = SimpleNamespace(text="/kanban dashboard", source=SimpleNamespace())

    async def main():
        seen["loop"] = threading.get_ident()
        return await GatewaySlashCommandsMixin._handle_kanban_command(
            cast(Any, gateway), cast(Any, event))

    assert asyncio.run(main()) == "https://example.test/kanban?session=s1"
    assert seen["thread"] != seen["loop"]


# --- #12 + #63: an exception in the interrupt-phase turn must not read as rc 0
def test_owns_transcript_driver_fails_when_the_turn_raises(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "_owns_transcript_driver_t", REPO / "tests/e2e/_owns_transcript_driver.py")
    drv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drv)

    marker = tmp_path / "it's here"  # quote-bearing path: #63

    class Agent:
        def run_conversation(self, prompt, **_kw):
            import shlex
            cmd = prompt[len("RUN_TOOL:"):]
            # the marker token must re-lex to the exact path
            assert shlex.split(cmd)[1] == str(marker)
            marker.touch()
            raise RuntimeError("boom in the turn")

    import hermes_state
    import agent.interrupt_compat as ic
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: object())
    monkeypatch.setattr(ic, "request_hard_interrupt", lambda *_a, **_k: None)
    monkeypatch.setattr(drv, "_agent", lambda *_a, **_k: Agent())
    monkeypatch.setattr(sys, "argv", ["drv", "interrupt", "p", "http://x", "sid", str(marker)])
    rc = drv.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc != 0 and "boom in the turn" in out.get("error", ""), (rc, out)


# --- #125 + #135: the no-delegation path has a batch_id the gate accepts, and
# both reviewer instructions name it.
def test_no_delegation_batch_id_is_documented_and_accepted():
    from hermes_cli import kanban_db as kb
    from agent.prompt_builder import KANBAN_GUIDANCE

    skill = (REPO / "skills/devops/sdlc-review/SKILL.md").read_text()
    assert '"n/a: <reason>"' in skill
    assert "batch_id to `n/a: <reason>`" in KANBAN_GUIDANCE

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, run_id INT, body TEXT)")
    cov = {"lenses": {k: "done" for k in ("contract", "execution", "cross-vendor", "mutation")},
           "findings": 1, "items": ["BEHAVIOUR: a real finding"], "review_minutes": 5,
           "batch_id": "n/a: delegate_task not in this toolset", "head_sha": "abcdef1"}
    conn.execute("INSERT INTO task_comments (task_id, run_id, body) VALUES (?,?,?)",
                 ("t_x", 1, "review_coverage: " + json.dumps(cov)))
    assert kb._validate_review_coverage(conn, "t_x", 1) is None
