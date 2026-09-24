"""Permanent guard for #942 mutant M43 (t_ac8357bb): a head content-match must not override a stamp.

Shape: the engine emits an UNSTAMPED synthetic row that is a role+content twin of
the stamped kept predecessor, placed immediately before it. The original successor
is dropped, so placement falls to predecessor + 1. The stamp is the authority, so
the notice must land AFTER the real (stamped) predecessor, never between the
synthetic twin and it.

M43 replaced the head loop's ``if head not in marked_outputs:
surviving.setdefault(original[head][0], head)`` with an unconditional
``surviving[original[head][0]] = head``; that yields [SYN, EV, P, SUM] instead of
[SYN, P, EV, SUM]. Originally Argus r9 probe test_r9m43_synthetic_twin.py.
"""
import pytest
from unittest.mock import patch

from agent.confab_notice import is_metadata_only_tool_notice
from tests.agent.test_confab_notice_e2e import VALID_NOTICE, notice_env  # noqa: F401


@pytest.mark.parametrize("stream", [False, True])
def test_unstamped_twin_does_not_override_stamped_predecessor(notice_env, stream, tmp_path):  # noqa: F811
    from plugins.context_engine.lcm.config import LCMConfig
    from plugins.context_engine.lcm.engine import LCMEngine

    make_agent, handler, db, sid, _ = notice_env
    handler.response_queue[:] = [("", {**VALID_NOTICE, "kind": "tool_call_as_text"}), ("First.", None)]
    make_agent(stream=stream).run_conversation("first", conversation_history=[], task_id="writer")
    event = next(m for m in db.get_messages_as_conversation(sid) if is_metadata_only_tool_notice(m))
    history = [{"role": "user", "content": "hi", "_qa_id": "P"}, event,
               {"role": "assistant", "content": "dropped " * 200, "_qa_id": "S"}]
    agent = make_agent(stream=stream)
    agent.session_id = f"m43-{stream}"
    cc = LCMEngine(config=LCMConfig(database_path=str(tmp_path / "lcm.db")), hermes_home=str(tmp_path))
    cc.on_session_start(agent.session_id, hermes_home=str(tmp_path))
    agent.context_compressor = cc
    try:
        with patch.object(cc, "compress", side_effect=lambda rows, **kw: [
            {"role": "user", "content": "hi", "_qa_id": "SYN"},
            {**rows[0], "_src_idx": 0},
            {"role": "assistant", "content": "summary", "_qa_id": "SUM"},
        ]):
            compressed, _ = agent._compress_context(history, "system", approx_tokens=120_000)
        tags = ["EV" if is_metadata_only_tool_notice(m) else m.get("_qa_id") for m in compressed]
        assert tags.index("EV") == tags.index("P") + 1, tags
    finally:
        cc.shutdown()
