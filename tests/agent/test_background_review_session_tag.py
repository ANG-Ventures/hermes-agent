"""Background-review log lines must carry the parent session tag (t_35e2029a).

The 2026-09-29 incident line ``LCM compaction #1: 989 messages -> 109`` had no
``[session_id]`` tag, so session-keyed greps missed the bg-review fork's
537 s compaction. Two gaps produced untagged lines:

* the bg-review thread only gained a session tag once ``run_conversation``
  reached ``build_turn_context``; anything logged before that (spawn guards,
  setup failures) or on the thread after teardown was untagged;
* pool workers launched through ``propagate_context_to_thread`` (the
  compaction worker among them) did not inherit the thread-local session tag.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import hermes_logging
from hermes_logging import clear_session_context, set_session_context
from tools.thread_context import propagate_context_to_thread


def _tag_of(record: logging.LogRecord) -> str:
    return getattr(record, "session_tag", "")


def test_propagated_pool_worker_inherits_session_tag(caplog):
    log = logging.getLogger("test.session_tag.worker")
    set_session_context("PARENT_SID_T35E2029A")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            wrapped = propagate_context_to_thread(
                lambda: log.warning("worker line")
            )
            with caplog.at_level(logging.WARNING, logger=log.name):
                pool.submit(wrapped).result()
            # The recycled worker thread must not keep the stale tag.
            leftover = pool.submit(
                lambda: getattr(hermes_logging._session_context, "session_id", None)
            ).result()
    finally:
        clear_session_context()

    records = [r for r in caplog.records if r.getMessage() == "worker line"]
    assert records, "worker log line not captured"
    assert _tag_of(records[0]) == " [PARENT_SID_T35E2029A]"
    assert leftover is None


def test_background_review_thread_lines_are_session_tagged(caplog, monkeypatch):
    """Every line on the bg-review thread carries the parent session tag."""
    import agent.background_review as br

    class _Parent:
        session_id = "PARENT_SID_T35E2029A"

    seen = {}

    def _fake_run(agent, messages_snapshot, prompt, task_cfg=None, review_run=None, **_kwargs):
        logging.getLogger("agent.background_review").warning("early review line")
        seen["tag_inside"] = getattr(
            hermes_logging._session_context, "session_id", None
        )

    monkeypatch.setattr(br, "_run_review_in_thread", _fake_run)
    target, _prompt = br.spawn_background_review_thread(
        _Parent(), [], review_skills=True, task_cfg={}
    )

    result = {}

    def _thread_main():
        target()
        result["tag_after"] = getattr(
            hermes_logging._session_context, "session_id", None
        )

    with caplog.at_level(logging.WARNING, logger="agent.background_review"):
        t = threading.Thread(target=_thread_main)
        t.start()
        t.join(10)

    records = [r for r in caplog.records if r.getMessage() == "early review line"]
    assert records, "review-thread log line not captured"
    assert _tag_of(records[0]) == " [PARENT_SID_T35E2029A]"
    assert seen["tag_inside"] == "PARENT_SID_T35E2029A"
    assert result["tag_after"] is None, "bg-review thread leaked its session tag"
