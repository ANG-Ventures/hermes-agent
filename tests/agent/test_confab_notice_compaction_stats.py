"""UI-only notices must not change LCM's exact in-turn accounting."""
import copy
from unittest.mock import MagicMock, patch

from agent import compaction_stats
from agent.confab_notice import is_metadata_only_tool_notice
from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from tests.agent.test_confab_notice_e2e import VALID_NOTICE, notice_env
from tests.context_engine.test_compaction_render_e2e import _tool_heavy_turn


def test_lcm_stats_identical_with_ui_only_events(notice_env, tmp_path):
    make_agent, handler, db, sid, _ = notice_env
    handler.response_queue[:] = [("", {**VALID_NOTICE, "kind": "tool_call_as_text"}), ("First.", None)]
    make_agent().run_conversation("first", conversation_history=[], task_id="writer")
    event = next(row for row in db.get_messages_as_conversation(sid)
                 if is_metadata_only_tool_notice(row))
    baseline = _tool_heavy_turn(40)

    def run(events):
        history = copy.deepcopy(baseline)
        for pos in reversed(events):
            history.insert(pos, dict(event))
        agent = make_agent()
        agent.session_id = f"stats-{len(events)}"
        cc = LCMEngine(config=LCMConfig(
            database_path=str(tmp_path / f"lcm-{len(events)}.db"),
            fresh_tail_count=4, leaf_chunk_tokens=120, context_threshold=0.01,
        ), hermes_home=str(tmp_path))
        cc.update_model("test-model", 200_000, provider="unit-test")
        cc.on_session_start(agent.session_id, hermes_home=str(tmp_path), model="test-model",
                            provider="unit-test", context_length=200_000, platform="pytest")
        agent.context_compressor = cc
        seen = []
        original = compaction_stats.build_inturn_stats
        def capture(**kwargs):
            stats = original(**kwargs)
            seen.append((kwargs, stats))
            return stats
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "compacted summary"
        response.usage = None
        try:
            with patch.object(compaction_stats, "build_inturn_stats", side_effect=capture), \
                    patch("agent.auxiliary_client.call_llm", return_value=response):
                agent._compress_context(history, "system", approx_tokens=120_000)
            assert cc._last_compression_status == "compacted"
            assert len(seen) == 1
            args, stats = seen[0]
            assert not stats.approx_attribution, "Option B must remain exact"
            assert all(not is_metadata_only_tool_notice(row) for row in args["messages"])
            assert all(not is_metadata_only_tool_notice(row) for row in args["compressed"])
            return (stats.pre_messages, stats.post_messages, stats.folded_count,
                    stats._kept_pre_messages, stats.kept_messages, stats.anchor_messages,
                    stats._kept_pre_tokens, stats.folded_tokens)
        finally:
            cc.shutdown()

    control = run([])
    assert run([1]) == control
    assert run([1, 50]) == control
