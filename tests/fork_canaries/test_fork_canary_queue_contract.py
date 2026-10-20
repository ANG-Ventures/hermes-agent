"""Fork canary: the /queue + /stop contract (Ace rulings 2026-10-12, -13 and -18).

Surface: CLI + every gateway surface (Telegram, Discord, Slack), busy and idle. Upstream has NEITHER
``gateway/queue_mgmt.py`` NOR ``hermes_cli/queue_verbs.py`` (checked 2026-10-18 against upstream/main), so a
parity merge that resolves ``gateway/run_busy.py`` / ``gateway/run_inbound.py`` / ``cli_loops_mixin.py``
toward upstream silently turns every ``/queue <verb>`` back into a queued PROMPT and makes ``/stop`` drop the
queue again. No import error, nothing else in the suite goes red: the user just gets the old behaviour.

One row per ruling, driven through the REAL entry points (``_busy_queue_command``, ``_hm_cmd_queue``,
``CLILoopsMixin._cmd_queue``) on a real ``GatewayRunner`` shell and a real ``BasePlatformAdapter``. These are
behaviour contracts, not snapshots: adding an alias or a new verb upstream never turns this file red.

Each test names the edit that turns it red. RED-proven 2026-10-18 (card t_36288a8e).
"""

import importlib
import queue as stdlib_queue
from types import SimpleNamespace

import pytest

from gateway import queue_mgmt as qm
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from hermes_cli.queue_verbs import QueueOp, parse_queue_payload

KEY = "agent:main:telegram:dm:7"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="m1")

    async def edit_message(self, chat_id, message_id, content, finalize=False) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="7", chat_type="dm")


def _ev(text, **kw):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), **kw)


@pytest.fixture
def rig():
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = _Adapter()
    runner.adapters = {adapter.platform: adapter}
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False)
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter


def _seed(runner, adapter, *texts, internal_at=None):
    events = [_ev(t, internal=(i == internal_at)) for i, t in enumerate(texts)]
    assert qm.rewrite_active(runner, KEY, adapter, events)


def _active(runner, adapter):
    return [e.text for e in qm.active_items(runner, KEY, adapter)]


def _held(runner):
    return [e.text for e in runner._session_state(KEY).conversation.held_queue]


# --------------------------------------------------------------------------- R1: one parser, exact verb shape
# Red if: the CLI or the gateway grows its own verb table again, or a verb starts swallowing trailing words.

@pytest.mark.parametrize("payload, kind", [
    ("list", "list"), ("rm 2", "rm"), ("move 2 1", "move"), ("edit 1 new", "edit"),
    ("clear", "clear"), ("run", "run"), ("help", "help"),
    ("rm", "rm"), ("rm all", "clear"),                                       # Ace 2026-10-18
    ("rm 3 things", "add"), ("clear the cache", "add"), ("run the tests", "add"),
    ("move 1 2 now", "add"), ("rm all of them", "add"), ("add rm 2", "add"),   # Ace 2026-10-13 R2
])
def test_verb_shape_is_exact(payload, kind):
    assert parse_queue_payload(payload).kind == kind


def test_cli_and_gateway_share_the_one_parser():  # noqa: source-proxy structural: one parser shared by both surfaces; the parsed behaviour is exercised by the R2 tests below
    # Red if cli_loops_mixin.py or queue_mgmt.classify stop calling parse_queue_payload.
    import inspect

    import hermes_cli.cli_loops_mixin as cli_mixin
    assert "parse_queue_payload" in inspect.getsource(cli_mixin.CLILoopsMixin._cmd_queue)
    assert "parse_queue_payload" in inspect.getsource(qm.classify)


# --------------------------------------------------------------------------- R2: verbs manage, never enqueue
# Red if run_busy._busy_queue_command / run_inbound._hm_cmd_queue stop routing op.is_management to
# queue_mgmt.apply_management (the pre-#1948 shape enqueued "/queue list" as the prompt "list").

@pytest.mark.asyncio
async def test_busy_list_is_management_not_a_prompt(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    reply = await runner._busy_queue_command(MessageEvent(text="/queue list", source=_source()), KEY, _source())
    assert reply.startswith("Queue (1 pending):") and _active(runner, adapter) == ["a"]


@pytest.mark.asyncio
async def test_idle_list_is_management_not_a_prompt(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    handled, reply = await runner._hm_cmd_queue(MessageEvent(text="/queue list", source=_source()), _source(), KEY)
    assert handled is True and reply.startswith("Queue (1 pending):")


# --------------------------------------------------------------------------- R3: bare rm lists, rm all clears
# Red if queue_verbs.parse_queue_payload drops the `kind == "rm"` no-number branch, or
# queue_mgmt.apply_management drops the `op.kind == "rm" and op.index is None` branch.

@pytest.mark.asyncio
async def test_bare_rm_lists_and_changes_nothing(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    reply = await runner._busy_queue_command(MessageEvent(text="/queue rm", source=_source()), KEY, _source())
    assert "1. a" in reply and "2. b" in reply and "/queue rm all" in reply
    assert _active(runner, adapter) == ["a", "b"] and adapter._pending_messages[KEY].text == "a"


@pytest.mark.asyncio
async def test_rm_all_clears_users_and_keeps_system_items(rig):
    runner, adapter = rig
    _seed(runner, adapter, "wake", "a", "b", internal_at=0)
    reply = await runner._busy_queue_command(MessageEvent(text="/queue rm all", source=_source()), KEY, _source())
    assert reply == "Cleared 2 queued prompts." and _active(runner, adapter) == ["wake"]


def test_cli_bare_rm_and_rm_all(monkeypatch):
    import cli
    from hermes_cli.cli_loops_mixin import CLILoopsMixin

    class _Shell(CLILoopsMixin):
        def __init__(self, *items):
            self._pending_input = stdlib_queue.Queue()
            for i in items:
                self._pending_input.put(i)

    printed = []
    monkeypatch.setattr(cli, "_cprint", lambda s="", *a, **k: printed.append(str(s)))
    sh = _Shell("a", "b")
    sh._cmd_queue("/queue rm")
    assert sh._pending_input_items() == ["a", "b"] and any("/queue rm all" in p for p in printed)
    sh._cmd_queue("/queue rm all")
    assert sh._pending_input_items() == []


# --------------------------------------------------------------------------- R4: /stop HOLDS, never drops
# Red if queue_mgmt.hold_user_items stops moving user items (or moves [system] ones), or stop_reply_suffix
# arms the run-next slot (the slot is a trigger: anything left there runs on the stop's tail drain).

def test_stop_holds_user_items_keeps_system_and_arms_nothing(rig):
    runner, adapter = rig
    _seed(runner, adapter, "wake", "a", "b", internal_at=0)
    assert qm.hold_user_items(runner, KEY, adapter) == 2
    assert _held(runner) == ["a", "b"] and _active(runner, adapter) == ["wake"]
    runner._stop_releases_queue = False
    assert qm.stop_reply_suffix(runner, KEY, adapter).startswith("\n2 queued prompt(s) held.")
    assert _held(runner) == ["a", "b"]


def test_verbs_on_held_store_never_arm_the_slot(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    assert qm.apply_management(runner, QueueOp("rm", index=1), KEY, adapter) == "Removed queue item 1: a"
    assert _held(runner) == ["b", "c"] and KEY not in adapter._pending_messages


# --------------------------------------------------------------------------- R5: /queue run releases, in order
# Red if restage_held reorders, or merged mode stops joining with MERGE_SEPARATOR / marking _queue_released
# (the mark is what keeps the inbound orphan rescue from running item 2 ahead of item 1, 2026-10-13).

@pytest.mark.asyncio
async def test_run_releases_oldest_first(rig):
    runner, adapter = rig
    runner._queue_release_mode = "per_item"
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    reply = await qm.release(runner, KEY, _source(), adapter, busy=False)
    assert reply.startswith("Released 3") and _active(runner, adapter) == ["a", "b", "c"] and _held(runner) == []


def test_merged_release_is_one_turn_in_order_and_marked(rig):
    runner, adapter = rig
    runner._queue_release_mode = "merged"
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    assert qm.restage_held(runner, KEY, adapter) == 3
    # Ace's ruling is literal: one turn, prompts joined in order by a BLANK LINE. Assert the text, not the constant.
    assert _active(runner, adapter) == ["a\n\nb\n\nc"]
    assert adapter._pending_messages[KEY]._queue_released is True


# --------------------------------------------------------------------------- R6: resets drop LOUDLY or carry
# Red if before_scope_clear stops classifying reasons: a user reset must report its count, an automatic one
# (no reply target) must carry the held items, never drop them silently.

def test_user_reset_drops_and_reports_auto_reset_carries(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    runner._clear_conversation_scope(KEY, reason="auto_reset")
    assert _held(runner) == ["a", "b"]
    runner._clear_conversation_scope(KEY, reason="session_reset")
    assert _held(runner) == []
    assert qm.take_reset_drop_suffix(runner, KEY) == "\nDropped 2 queued prompt(s) with the old conversation."
