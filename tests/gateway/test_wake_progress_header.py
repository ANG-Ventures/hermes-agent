"""Internal wake turns open every progress bubble with a "🔔 wake · <label>" line (t_cbc80c41).

A silent internal wake shares the human's chat; without the header its tool bubble reads as the
agent dying mid-turn. Human turns must render exactly as before.
"""

import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.response_filters import INTERNAL_NOTIFICATION_DISPLAY_KIND, wake_progress_header
from gateway.turn_context import TurnContext
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

INTERNAL = INTERNAL_NOTIFICATION_DISPLAY_KIND
DEADMAN = ('[kanban review deadman] review deadman tier 1: act on this card or comment why it waits. '
           'Card t_3f625862 "fix the thing" has sat in review 2h.')


# ── pure helper ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,metadata,label", [
    ("[kanban wake] something changed", {}, "kanban wake"),
    ("[kanban] board note", {}, "kanban"),
    (DEADMAN, {}, "kanban review deadman · t_3f625862"),
    ("[ASYNC DELEGATION COMPLETE — d_1]\nA background subagent finished.", {}, "delegation"),
    ("subagent finished", {"delegation_id": "d_1"}, "delegation"),
    ("[IMPORTANT: Background process proc_1 finished (exit code 0).]", {}, "background process"),
    ("watch matched", {"notification_origin": "process_registry_synthetic"}, "background process"),
    ("resume where you left off", {}, "internal"),
])
def test_internal_turn_header_label(text, metadata, label):
    assert wake_progress_header(text, INTERNAL, metadata) == f"🔔 wake · {label}"


@pytest.mark.parametrize("text,kind", [
    (DEADMAN, None),          # a human typing a bracket tag is still a human turn
    ("hello", None),
    ("", INTERNAL),
    ("   \n", INTERNAL),
    (None, INTERNAL),
])
def test_no_header(text, kind):
    assert wake_progress_header(text, kind, {}) is None


# ── editable progress bubbles ───────────────────────────────────────────────────────────────

async def _drive_progress(monkeypatch, items, header, grouping="grouped"):
    import gateway.run_turn_runner as rtr
    from gateway.run_turn_runner import TurnRunner

    clock = iter(range(0, 10**6, 100))  # every edit is outside the 1.5 s throttle window
    monkeypatch.setattr(rtr, "time", SimpleNamespace(monotonic=lambda: float(next(clock))))
    adapter = ProgressCaptureAdapter()
    q = queue.Queue()
    for item in items:
        q.put(item)
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.TELEGRAM), progress_queue=q,
                      _run_still_current=lambda: True, progress_grouping=grouping)
    ctx.wake_progress_header = header
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter)
    task = asyncio.create_task(TurnRunner(runner, ctx).send_progress_messages())
    for _ in range(100):  # drain the queue, then let the last send/typing tick settle
        if q.empty():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.8)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return adapter


@pytest.mark.asyncio
async def test_internal_turn_bubbles_carry_header_after_reset(monkeypatch):
    head = wake_progress_header(DEADMAN, INTERNAL, {})
    adapter = await _drive_progress(monkeypatch, ["💻 terminal: \"ls\"", ("__reset__",), "📖 read_file"], head)
    print("rendered bubble 1:", repr(adapter.sent[0]["content"]))
    assert [s["content"] for s in adapter.sent] == [
        "🔔 wake · kanban review deadman · t_3f625862\n💻 terminal: \"ls\"",
        "🔔 wake · kanban review deadman · t_3f625862\n📖 read_file",
    ]


@pytest.mark.asyncio
async def test_human_turn_bubbles_unchanged(monkeypatch):
    adapter = await _drive_progress(monkeypatch, ["💻 terminal: \"ls\"", ("__reset__",), "📖 read_file"], None)
    assert [s["content"] for s in adapter.sent] == ["💻 terminal: \"ls\"", "📖 read_file"]


@pytest.mark.asyncio
async def test_dedup_on_fresh_internal_bubble_keeps_header(monkeypatch):
    head = wake_progress_header("[kanban wake] x", INTERNAL, {})
    adapter = await _drive_progress(monkeypatch, [("__dedup__", "💻 terminal", 1)], head)
    assert adapter.sent[0]["content"] == "🔔 wake · kanban wake\n💻 terminal"


@pytest.mark.asyncio
async def test_separate_mode_header_once_on_first_message(monkeypatch):
    head = wake_progress_header("[kanban wake] x", INTERNAL, {})
    adapter = await _drive_progress(monkeypatch, ["💻 terminal", "📖 read_file"], head, grouping="separate")
    assert [s["content"] for s in adapter.sent] == ["🔔 wake · kanban wake\n💻 terminal", "📖 read_file"]


@pytest.mark.asyncio
async def test_overflow_first_group_keeps_header(monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from tests.gateway.test_run_progress_topics import SmallLimitProgressAdapter

    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.TELEGRAM))
    ctx.wake_progress_header = "🔔 wake · kanban wake"
    adapter = SmallLimitProgressAdapter()
    tr = TurnRunner(SimpleNamespace(), ctx)
    st = tr._progress_edit_state(adapter)
    for i in range(8):
        tr._progress_absorb(st, f"💻 terminal: \"command number {i}\"")
    await tr._roll_progress_overflow_if_needed(st)
    assert adapter.sent[0]["content"].startswith("🔔 wake · kanban wake\n")


# ── wiring + heartbeat ──────────────────────────────────────────────────────────────────────

def _bare_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._running_agents = {}
    runner._draining = runner._restart_requested = False
    runner._delivery_adapter_for = lambda source: None
    runner._run_still_current_fn = lambda key, gen: (lambda: True)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [(INTERNAL, "🔔 wake · kanban review deadman · t_3f625862"), (None, None)])
async def test_turn_context_carries_header_for_internal_turns_only(kind, expected):
    disp = MagicMock()
    disp.resolve_display_setting.return_value = False
    ctx, _, _ = _bare_runner()._run_agent_build_turn_context(
        disp, MagicMock(), message=DEADMAN, source=SimpleNamespace(chat_id="c", platform=Platform.TELEGRAM),
        session_key="sess", run_generation=1, persist_user_display_kind=kind, persist_user_display_metadata={},
    )
    assert ctx.wake_progress_header == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["🔔 wake · kanban review deadman · t_3f625862", None])
async def test_heartbeat_text_opens_with_header(monkeypatch, header):
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0.01")
    runner = _bare_runner()
    adapter = MagicMock()
    sends = []

    async def _send(chat_id, content, metadata=None):
        sends.append(content)
        runner._restart_requested = True  # one heartbeat, then stop
        return SimpleNamespace(success=True, message_id="hb-1")

    adapter.send = _send
    adapter.edit_message = AsyncMock()
    runner._delivery_adapter_for = lambda source: adapter
    runner._agent_activity_summary = staticmethod(lambda agent: None)
    agent = MagicMock()
    runner._running_agents["sess"] = agent
    disp = MagicMock()
    disp._display_surface_mode.return_value = "on"
    disp.resolve_display_setting.return_value = False
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform="telegram"), session_key="sess")
    ctx.wake_progress_header = header
    ctx.agent_holder[0] = agent
    await asyncio.wait_for(runner._run_agent_notify_long_running(disp, ctx, [None]), 5)
    assert len(sends) == 1
    print("rendered heartbeat:", repr(sends[0]))
    if header:
        assert sends[0].startswith(header + "\n⏳ Working")
    else:
        assert sends[0].startswith("⏳ Working")
