"""S7 D4: the auto-decompose loop names its board; it never flips a process-wide pointer.

One iteration of the loop body for board ``subs-ace`` runs in a worker thread. Inside it,
``list_triage_ids`` must (i) receive ``board="subs-ace"`` and (ii) see ``subs-ace`` from a bare
``get_current_board()``; a second thread's bare ``get_current_board()`` during the same iteration
must read ``default``. Mutations: drop the ``scoped_current_board`` wrapper -> (ii) reads
``default``; drop ``board=slug`` -> (i) fails; restore an ``os.environ`` flip -> the second thread
is unaffected after S7 (nothing reads the var), which I4(c) pins at source level.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb


def test_loop_body_scopes_its_board_per_context_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.create_board("subs-ace")

    inside = threading.Event()
    release = threading.Event()
    seen: dict = {}

    def fake_list_triage_ids(*, board=None, tenant=None):
        seen["board_kw"] = board
        seen["ambient"] = kb.get_current_board()
        inside.set()
        assert release.wait(5)
        return []

    decomp = SimpleNamespace(list_triage_ids=fake_list_triage_ids, decompose_task=None)
    worker = threading.Thread(target=kw.auto_decompose_board, args=(kb, decomp, "subs-ace", 3))
    worker.start()
    try:
        assert inside.wait(5)
        seen["other_thread"] = kb.get_current_board()
    finally:
        release.set()
        worker.join(5)

    assert seen["board_kw"] == "subs-ace"  # (i) explicit board, no ambient read
    assert seen["ambient"] == "subs-ace"  # (ii) callees see the board via the ContextVar
    assert seen["other_thread"] == kb.DEFAULT_BOARD  # never process-wide
