"""A compaction that leaves the transcript as it was must not rebuild the system prompt
(class G, t_91b6a4c4).

Phase-1 measurement (t_38dd407a): ``messages[0]`` session context rolled "Thursday, October 15" ->
"Friday, October 16" with no compaction drop. Live trace (agent.log, session 20261011_152630_8af335e4,
2026-10-16 00:09:48): an idle_resume compaction committed ``messages=347->347`` and logged
"Compaction rebuilt a drifted system prompt (151265 -> 151263 chars)" — the rebuild re-rendered the
date line on a new day and was the only edit in the request. The test drives the real
``_compress_context`` with a session born yesterday and an engine that returns the same messages
(new dicts, extra bookkeeping keys), then checks the prompt the turn sends is unchanged.
"""

import copy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.agent.test_tool_call_incremental_persistence import _attach_real_session_db, _make_agent


def test_no_change_compaction_keeps_session_context_pin(tmp_path: Path) -> None:
    born = datetime.now() - timedelta(days=1)
    session_id = born.strftime("%Y%m%d_%H%M%S") + "_g0g0g0"
    agent = _make_agent()
    _attach_real_session_db(agent, tmp_path / "state.db", session_id)

    # The prompt the conversation has been sending since yesterday (same-day render, no "as of" line).
    import hermes_time
    with patch.object(hermes_time, "now", lambda: born.astimezone()):
        sent_prompt = agent._build_system_prompt("sys")
    assert "Today's date" not in sent_prompt
    agent._cached_system_prompt = sent_prompt
    # Today's rebuild WOULD differ (the date-rollover byte edit the measurement saw).
    assert "Today's date (as of the last context rebuild)" in agent._build_system_prompt("sys")

    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]

    def _same_transcript(msgs, **_kw):
        out = copy.deepcopy(msgs)
        for m in out:
            m["_engine_bookkeeping"] = True  # an engine's own keys; nothing a provider sees
        return out

    agent.context_compressor.compress = _same_transcript
    agent.context_compressor._last_compression_made_progress = True
    compressed, system_prompt = agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert [m["content"] for m in compressed] == [m["content"] for m in messages]
    assert system_prompt == sent_prompt
    assert agent._cached_system_prompt == sent_prompt
