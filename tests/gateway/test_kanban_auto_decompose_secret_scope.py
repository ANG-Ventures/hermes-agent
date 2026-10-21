"""Auto-decompose tick under multiplex.

Regression for #107955 / #57837: the tick runs off-turn in a fresh Context, so
``get_secret`` fails closed unless the tick installs the launch profile's scope.
Drives the loop the gateway runs (``kanban_watchers.auto_decompose_tick``); the
dispatcher-class copy this test used to drive had no runtime caller and was deleted (S7 D4).
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from agent import secret_scope as ss
from gateway import kanban_watchers as kw
from gateway.kanban_watchers_common import _to_thread_process_service


def test_auto_decompose_tick_reads_launch_profile_secrets_under_multiplex(monkeypatch, tmp_path):
    import hermes_cli
    from hermes_cli import kanban_db as kb

    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=launch-profile-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "default"}])

    seen = {}

    def fake_decompose(task_id, board=None, author=None):
        seen["value"] = ss.get_secret("ANTHROPIC_API_KEY")
        return SimpleNamespace(ok=True, fanout=False, child_ids=None, reason=None)

    fake = SimpleNamespace(list_triage_ids=lambda board=None: ["t1"], decompose_task=fake_decompose)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)

    ss.set_multiplex_active(True)
    try:
        # Same hop the gateway uses: fresh Context, no inherited per-turn scope.
        decomposed = asyncio.run(_to_thread_process_service(kw.auto_decompose_tick, kb, 5))
    finally:
        ss.set_multiplex_active(False)

    assert decomposed == 1
    assert seen["value"] == "launch-profile-key"
    assert ss.current_secret_scope() is None
