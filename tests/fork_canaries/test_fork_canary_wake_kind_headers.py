"""Fork canary: an internal wake's progress header names its KIND from producer metadata (t_17ba87a6).

Surfaces: ``gateway/wake_kind.py`` (vocabulary), ``gateway/response_filters.py::wake_progress_header``
(the header), ``gateway/platforms/event.py`` (``MessageEvent.wake_kind``), the producers
(``gateway/run_goals.py``, ``gateway/kanban_owner_wake.py``, ``gateway/kanban_watchers*.py``,
``gateway/run_notifications.py``, ``gateway/run_startup.py``, ...) and the turn wiring in
``gateway/run_turn.py`` that carries the producer's identity into the header's metadata.

2026-11-08: 38 /goal idle check-ins rendered ``🔔 wake · internal · t_c676b4f3`` because the header
parsed the wake TEXT (the goal mentioned a card) and Ace read them as kanban wakes. Upstream has
the header (fork PR #2127 extended it) but none of the kinds: a parity merge that resolves
``response_filters.py`` / ``run_goals.py`` / ``run_turn.py`` toward upstream drops the kind with
every other test green. Each arm asserts the contract for ONE kind; nothing snapshots the table.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.response_filters import display_kind_for_event, wake_progress_header
from gateway.session import SessionSource
from gateway.wake_kind import control_wake_identity, wake_display_metadata, wake_meta

FEATURE = "wake-kind-headers"
REPO = Path(__file__).resolve().parents[2]
SRC = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1")
GOAL_TEXT = "[Goal check-in 1/3: the session has been idle 30 min with this goal still active]\nGoal: land t_c676b4f3"


def header(event) -> str:
    """The header exactly as the turn builds it: display kind + the event's wake identity."""
    return wake_progress_header(event.text or "x", display_kind_for_event(event), wake_display_metadata(event))


def runner_goals():
    from gateway.run_goals import GatewayGoalsMixin
    return GatewayGoalsMixin


# ── kanban: the real owner-wake producer ────────────────────────────────────────────────────

class _Adapter:
    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        event._gateway_accepted = True
        self.events.append(event)


def _owner_wake(items, task_id="t_1a2b3c4d"):
    from gateway import kanban_owner_wake as ow
    adapter = _Adapter()
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    entry = SimpleNamespace(origin=SRC, session_key="agent:main:telegram:dm:c1", session_id="s1")
    card = {"board": None, "task_id": task_id, "title": "fix the thing", "items": items}
    assert asyncio.run(ow.deliver_turn(runner, entry, ow.render_prompt([card]), [card])) is True
    return adapter.events[0]


@pytest.mark.parametrize("items,sub", [
    ([{"kind": "review_requested", "trigger": "review_requested"}], "review"),
    ([{"kind": "completed", "trigger": "completed"}], "handback"),
    ([{"kind": "blocked", "trigger": "needs_input"}], "needs-input"),
    ([{"kind": "review_deadman", "trigger": "review_deadman"}], "deadman"),
    ([{"kind": "completed", "trigger": "boomerang_wake_resurface"}], "boomerang"),
])
def test_kanban_wake_names_sub_kind_and_card(items, sub):
    assert header(_owner_wake(items)) == f"🔔 kanban wake · {sub} · t_1a2b3c4d"


# ── goal / loop / heartbeat: the real _synthetic_prompt_event producer ──────────────────────

def test_goal_check_in_shows_no_card_from_its_text():
    """THE 2026-11-08 confusion: a goal wake whose text names a card shows no card id."""
    from gateway.run_goals import _goal_idle_wake_sub
    mgr = SimpleNamespace(state=SimpleNamespace(idle_wakes=1))
    # RED: gateway/response_filters.py L311 revert wake_progress_header to the regex-only form ->
    # "🔔 wake · internal · t_c676b4f3" (the exact 2026-11-08 header). The producer dropping its kind
    # (gateway/run_goals.py L929) is caught by test_every_gateway_wake_producer_names_its_kind below.
    event = runner_goals()._synthetic_prompt_event(
        SRC, GOAL_TEXT, internal=True, wake_kind="goal", wake_sub=_goal_idle_wake_sub(GOAL_TEXT, mgr, 3))
    head = header(event)
    assert head == "🔔 goal wake · check-in 1/3"
    assert "t_c676b4f3" not in head


def test_goal_continue_and_park_expired():
    from gateway.run_goals import _goal_idle_wake_sub
    cont = runner_goals()._synthetic_prompt_event(
        SRC, "[Continuing toward your standing goal]\nGoal: x", internal=True, wake_kind="goal", wake_sub="continue")
    assert header(cont) == "🔔 goal wake · continue"
    parked = "[Continuing toward your standing goal]\nGoal: ship t_c676b4f3"
    assert _goal_idle_wake_sub(parked, SimpleNamespace(state=None), 3) == "park expired"


def test_loop_wake():
    event = runner_goals()._synthetic_prompt_event(SRC, "check the deploy t_c676b4f3", internal=True, wake_kind="loop")
    assert header(event) == "🔔 loop wake"


def test_heartbeat_is_named_though_not_internal():
    event = runner_goals()._synthetic_prompt_event(SRC, "heartbeat: anything due?", wake_kind="heartbeat")
    event._heartbeat_session_id = "s1"
    assert not event.internal
    assert header(event) == "🔔 heartbeat"


# ── the rest of the vocabulary: the producer's event shape ──────────────────────────────────

def _internal(kind, text="[IMPORTANT: Background process proc_1 finished] t_c676b4f3", **detail):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=SRC, internal=True,
                        wake_kind=kind, metadata=wake_meta(**detail))


@pytest.mark.parametrize("kind,detail,expected", [
    ("delegation", {}, "🔔 delegation done"),
    ("process", {}, "🔔 process done"),
    ("restart", {}, "🔔 restart resume"),
    ("webhook", {"name": "msgraph"}, "🔔 webhook · msgraph"),
])
def test_kind_renders_its_own_label_and_never_a_text_card(kind, detail, expected):
    assert header(_internal(kind, **detail)) == expected


def test_watcher_wake_takes_its_name_from_the_caller_and_card_only_from_metadata():
    kind, detail = control_wake_identity({"wake_name": "review-failsafe"}, "[kanban review deadman] t_c676b4f3")
    event = _internal(kind, **{"sub": detail.get("wake_sub"), "name": detail.get("wake_name"),
                               "card_id": detail.get("card_id")})
    assert header(event) == "🔔 watcher wake · review-failsafe"
    kind, detail = control_wake_identity({"wake_name": "review-failsafe", "card_id": "t_3f625862"}, "x")
    event = _internal(kind, name=detail["wake_name"], card_id=detail["card_id"])
    assert header(event) == "🔔 watcher wake · review-failsafe · t_3f625862"


def test_only_card_id_metadata_adds_a_card():
    # RED: gateway/response_filters.py L311 (wake_progress_header) revert to the regex-only form
    # (label from `_WAKE_TAG_RE`/`_wake_fallback_label`, card from `re.search(r"\bt_[0-9a-f]{8}\b", message)`)
    # -> every arm here reads "🔔 wake · ..." and this one gains "· t_c676b4f3".
    assert header(_internal("process")) == "🔔 process done"
    assert header(_internal("process", card_id="t_0a0b0c0d")) == "🔔 process done · t_0a0b0c0d"


# ── producer contract: every gateway wake producer names its kind ───────────────────────────

def test_every_gateway_wake_producer_names_its_kind():
    # RED: gateway/run_goals.py L929 drop `wake_kind="goal",` from the goal idle-wake producer
    # -> "gateway/run_goals.py:<line> _synthetic_prompt_event(" is listed.
    from tests.gateway.test_wake_kind_producer_contract import unlabeled_wake_producers
    assert unlabeled_wake_producers() == []


def test_turn_carries_the_wake_identity_into_the_header_metadata():
    """Both ``_run_agent`` calls in run_turn.py (first turn + queued follow-up) spread
    ``wake_display_metadata(...)`` into ``persist_user_display_metadata``, the dict the header reads."""
    tree = ast.parse((REPO / "gateway" / "run_turn.py").read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_run_agent"):
            continue
        meta = next((k.value for k in node.keywords if k.arg == "persist_user_display_metadata"), None)
        if meta is None:
            continue
        if isinstance(meta, ast.BoolOp):  # ``{...} or None``
            meta = meta.values[0]
        spreads = [v for k, v in zip(meta.keys, meta.values) if k is None] if isinstance(meta, ast.Dict) else []
        sites.append(any(isinstance(v, ast.Call) and getattr(v.func, "id", "") == "wake_display_metadata"
                         for v in spreads))
    assert len(sites) >= 2 and all(sites), sites
