"""/branch (alias /fork) tells the MODEL it is a fork and carries the parent's overrides (t_424dcfb5).

Measured 2026-10-11: the child's transcript ended exactly where the parent's did (so the child
resumed the parent's in-flight work), and the child ran at the config reasoning default (medium)
while the parent ran at xhigh because the session-store ``reasoning_override`` /
``model_override_identity`` were never copied. Drives the REAL ``_handle_branch_command`` and
``_handle_merge_command`` against a REAL SessionStore + SessionDB; only the Discord adapter is a fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore, build_session_key
from gateway.slash_commands_branch_thread import (
    FORK_NOTICE_DISPLAY_KIND, fork_notice_content, is_fork_notice_row, parse_branch_flags,
)
from hermes_state import AsyncSessionDB


class _ThreadAdapter:
    def __init__(self, thread_id="777000"):
        self.thread_id = thread_id
        self.sent = []

    async def create_handoff_thread(self, parent_chat_id, name):
        return self.thread_id

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def archive_thread(self, thread_id):
        return True


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _runner(store, adapter, *, resolver=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner.config = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = store
    runner._session_db = AsyncSessionDB(store._db)
    runner._pending_skills_reload_notes = {}
    runner._session_model_override_unavailable = set()
    runner._reresolve_model_override_credentials = resolver or (
        lambda identity: {**identity, "api_key": "resolved-key", "base_url": "https://example.test/v1"})
    # Config reasoning default is medium: the parent's xhigh can only reach the child by inheritance.
    runner._load_reasoning_config = lambda model="": {"enabled": True, "effort": "medium"}
    return runner


def _channel_source():
    return SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="u1",
                         user_name="ann", scope_id="g9")


def _thread_source(thread_id="777000"):
    return SessionSource(platform=Platform.DISCORD, chat_id=thread_id, chat_type="thread",
                         thread_id=thread_id, parent_chat_id="123", user_id="u1", scope_id="g9")


def _seed(store, source, rows=(("user", "hello"), ("assistant", "world"))):
    entry = store.get_or_create_session(source)
    for role, content in rows:
        store._db.append_message(entry.session_id, role=role, content=content)
    return entry


def _set_parent_overrides(runner, store, key, *, reasoning=True, model=True):
    if reasoning:
        runner._set_session_reasoning_override(key, {"enabled": True, "effort": "xhigh"})
    if model:
        runner._set_session_model_override(key, {
            "model": "claude-fable-5-1", "provider": "claude-alr", "api_key": "k", "api_mode": "chat_completions",
        })
    entry = store.entry_for(key)
    if reasoning:
        assert entry.reasoning_override == {"enabled": True, "effort": "xhigh"}
    if model:
        assert entry.model_override_identity["model"] == "claude-fable-5-1"


# ----------------------------------------------------------------------------- flags

def test_parse_branch_flags_leading_only():
    assert parse_branch_flags("--fresh --here side quest") == (True, True, "side quest")
    assert parse_branch_flags("--here --fresh") == (True, True, "")
    assert parse_branch_flags("quest --fresh") == (False, False, "quest --fresh")
    assert parse_branch_flags("") == (False, False, "")


def test_merge_delta_rows_strips_only_the_complete_notice():
    """Prism P1 e8e07ff8b118: a human prompt that merely starts with the prefix is kept whole."""
    from gateway.slash_commands_branch_thread import merge_delta_rows

    notice = fork_notice_content(parent_session_id="p_1", row_count=2, platform=Platform.DISCORD, parent_ref="123")
    rows = [
        {"role": "user", "content": notice + "\n\nNEW q"},                        # repair-folded
        {"role": "user", "content": notice},                                      # display_kind lost
        {"role": "user", "content": "[Forked session] Please compare the two approaches."},
        {"role": "user", "content": "[Forked session] first para\n\nsecond"},
        {"role": "assistant", "content": notice},
    ]
    assert [m["content"] for m in merge_delta_rows(rows)] == [
        "NEW q",
        "[Forked session] Please compare the two approaches.",
        "[Forked session] first para\n\nsecond",
        notice,
    ]


# ----------------------------------------------------------------------------- (a) notice row

@pytest.mark.asyncio
async def test_branch_child_transcript_ends_with_fork_notice(store):
    source = _channel_source()
    parent = _seed(store, source)
    runner = _runner(store, _ThreadAdapter())

    await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))

    child = store.get_or_create_session(_thread_source())
    rows = store._db.get_messages(child.session_id)
    assert [r["content"] for r in rows[:2]] == ["hello", "world"]
    notice = rows[-1]
    assert notice["role"] == "user" and notice["display_kind"] == FORK_NOTICE_DISPLAY_KIND
    assert notice["content"] == fork_notice_content(
        parent_session_id=parent.session_id, row_count=2, platform=Platform.DISCORD, parent_ref="123")
    assert parent.session_id in notice["content"] and "at row 2" in notice["content"]
    # The notice is what the child REPLAYS to the model (load_transcript is the live-replay read),
    # and it is machinery, not a human turn.
    replay = store.load_transcript(child.session_id)
    assert is_fork_notice_row(replay[-1])
    from agent.context_compressor import is_user_originated_turn
    assert not is_user_originated_turn(replay[-1])
    # The parent transcript is untouched.
    assert [r["content"] for r in store._db.get_messages(parent.session_id)] == ["hello", "world"]
    # Branch point = the copied rows only; the notice sits after it (decision (1): excluded).
    import json
    mc = json.loads(store._db.get_session(child.session_id)["model_config"] or "{}")
    assert mc["_branch_point_len"] == 2


# ----------------------------------------------------------------------------- (b) inheritance

@pytest.mark.asyncio
async def test_branch_child_inherits_reasoning_and_model_overrides(store):
    source = _channel_source()
    _seed(store, source)
    parent_key = build_session_key(source)
    runner = _runner(store, _ThreadAdapter())
    _set_parent_overrides(runner, store, parent_key)

    reply = await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))

    child_key = build_session_key(_thread_source())
    child = store.entry_for(child_key)
    # Store entry (survives a restart) ...
    assert child.reasoning_override == {"enabled": True, "effort": "xhigh"}
    assert child.model_override_identity["model"] == "claude-fable-5-1"
    assert child.model_override_identity["provider"] == "claude-alr"
    # ... AND the live maps the first child turn reads.
    assert runner._session_reasoning_overrides[child_key] == {"enabled": True, "effort": "xhigh"}
    assert runner._session_model_overrides[child_key]["model"] == "claude-fable-5-1"
    assert runner._session_model_overrides[child_key]["api_key"] == "resolved-key"
    assert runner._resolved_effort_label(session_key=child_key) == "xhigh"
    # The reply names what was inherited, so a drop is visible.
    assert "claude-fable-5-1" in reply and "xhigh" in reply
    # The parent keeps its own overrides.
    assert runner._resolved_effort_label(session_key=parent_key) == "xhigh"
    assert store.entry_for(parent_key).model_override_identity["model"] == "claude-fable-5-1"


@pytest.mark.asyncio
async def test_branch_inherited_overrides_persist_in_store_file(store):
    """The child's entry round-trips through sessions persistence, like the restart rehydrate reads it."""
    source = _channel_source()
    _seed(store, source)
    runner = _runner(store, _ThreadAdapter())
    _set_parent_overrides(runner, store, build_session_key(source))

    await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))

    reloaded = SessionStore(sessions_dir=store.sessions_dir, config=GatewayConfig())
    reloaded._ensure_loaded()
    child = reloaded.entry_for(build_session_key(_thread_source()))
    assert child is not None
    assert child.reasoning_override == {"enabled": True, "effort": "xhigh"}
    assert child.model_override_identity["model"] == "claude-fable-5-1"


@pytest.mark.asyncio
async def test_branch_unresolvable_model_is_not_inherited_and_not_claimed(store):
    source = _channel_source()
    _seed(store, source)
    parent_key = build_session_key(source)
    runner = _runner(store, _ThreadAdapter())
    _set_parent_overrides(runner, store, parent_key)
    runner._reresolve_model_override_credentials = lambda identity: None  # provider gone

    reply = await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))

    child_key = build_session_key(_thread_source())
    assert store.entry_for(child_key).model_override_identity is None
    assert child_key not in runner._session_model_overrides
    assert "claude-fable-5-1" not in reply
    # Reasoning still comes across: it needs no credential.
    assert store.entry_for(child_key).reasoning_override == {"enabled": True, "effort": "xhigh"}
    assert "xhigh" in reply


# ----------------------------------------------------------------------------- (c) --fresh

@pytest.mark.asyncio
async def test_branch_fresh_yields_no_overrides(store):
    source = _channel_source()
    _seed(store, source)
    parent_key = build_session_key(source)
    runner = _runner(store, _ThreadAdapter())
    _set_parent_overrides(runner, store, parent_key)

    reply = await runner._handle_branch_command(MessageEvent(text="/fork --fresh idea", source=source))

    child_key = build_session_key(_thread_source())
    child = store.entry_for(child_key)
    assert child.reasoning_override is None and child.model_override_identity is None
    assert child_key not in runner._session_reasoning_overrides
    assert child_key not in runner._session_model_overrides
    assert runner._resolved_effort_label(session_key=child_key) == "medium"
    assert "xhigh" not in reply and "claude-fable-5-1" not in reply
    assert store._db.get_session_title(child.session_id) == "idea"  # the flag is not the title
    # The parent is untouched by --fresh.
    assert runner._resolved_effort_label(session_key=parent_key) == "xhigh"


@pytest.mark.asyncio
async def test_branch_fresh_here_drops_overrides_on_the_shared_key(store):
    source = _channel_source()
    _seed(store, source)
    key = build_session_key(source)
    runner = _runner(store, _ThreadAdapter())
    _set_parent_overrides(runner, store, key)

    await runner._handle_branch_command(MessageEvent(text="/fork --fresh --here idea", source=source))

    assert key not in runner._session_reasoning_overrides
    assert key not in runner._session_model_overrides
    assert store.entry_for(key).model_override_identity is None
    assert runner._resolved_effort_label(session_key=key) == "medium"


# ----------------------------------------------------------------------------- (e) no overrides

@pytest.mark.asyncio
async def test_branch_parent_without_overrides_yields_none_and_reply_claims_none(store):
    source = _channel_source()
    _seed(store, source)
    runner = _runner(store, _ThreadAdapter())

    reply = await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))

    child_key = build_session_key(_thread_source())
    child = store.entry_for(child_key)
    assert child.reasoning_override is None and child.model_override_identity is None
    assert child_key not in runner._session_reasoning_overrides
    assert child_key not in runner._session_model_overrides
    assert "Inherits" not in reply and "reasoning" not in reply.lower()


# ----------------------------------------------------------------------------- (d) merge delta

@pytest.mark.asyncio
async def test_merge_delta_excludes_fork_notice(store):
    """Decision (1): the notice sits AFTER ``_branch_point_len`` and is dropped from the merge delta —
    a fork with no new turns is still 'nothing new', and a fork with turns summarizes only them."""
    source = _channel_source()
    parent = _seed(store, source)
    adapter = _ThreadAdapter()
    runner = _runner(store, adapter)
    await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))
    thread = _thread_source()
    child = store.get_or_create_session(thread)
    assert is_fork_notice_row(store.load_transcript(child.session_id)[-1])

    seen = {}
    fake = MagicMock()

    def _cap(history, focus=None):
        seen["history"] = history
        return "DELTA SUMMARY"
    fake._generate_summary = _cap
    runner._build_merge_summarizer = lambda: fake
    runner._gateway_session_origin_for_id = lambda sid: None
    runner._evict_cached_agent = MagicMock()
    runner._resume_target_allowed = AsyncMock(return_value=True)

    # Nothing but the notice after the branch point -> no new turns, nothing folded.
    result = await runner._handle_merge_command(MessageEvent(text="/merge", source=thread))
    assert "nothing new" in result.lower() or "no turns" in result.lower()
    assert "history" not in seen
    assert [r["content"] for r in store._db.get_messages(parent.session_id)] == ["hello", "world"]

    # Real exploration after the notice -> only those rows reach the summarizer.
    store._db.append_message(child.session_id, role="user", content="NEW q")
    store._db.append_message(child.session_id, role="assistant", content="NEW a")
    result = await runner._handle_merge_command(MessageEvent(text="/merge", source=thread))
    assert "DELTA SUMMARY" in result or "Folded" in result
    contents = [m["content"] for m in seen["history"]]
    assert contents == ["NEW q", "NEW a"]
    assert not any(is_fork_notice_row(m) for m in seen["history"])
    parent_rows = [r["content"] for r in store._db.get_messages(parent.session_id)]
    assert any("DELTA SUMMARY" in c for c in parent_rows)


def _capture_merge(runner):
    seen = {}
    fake = MagicMock()

    def _cap(history, focus=None):
        seen["history"] = history
        return "DELTA SUMMARY"
    fake._generate_summary = _cap
    runner._build_merge_summarizer = lambda: fake
    runner._gateway_session_origin_for_id = lambda sid: None
    runner._evict_cached_agent = MagicMock()
    runner._resume_target_allowed = AsyncMock(return_value=True)
    return seen


@pytest.mark.asyncio
async def test_merge_delta_survives_fold_when_parent_was_mid_turn(store):
    """Prism P1 199166e82bdc: the parent's copied history ends with an UNANSWERED user row, so the
    live-replay alternation repair folds the notice AND the child's first prompt into that row and the
    repaired replay is shorter than ``_branch_point_len``. The delta must come from the durable rows."""
    source = _channel_source()
    parent = _seed(store, source, rows=(("user", "hello"), ("assistant", "world"), ("user", "mid-turn ask")))
    runner = _runner(store, _ThreadAdapter())
    await runner._handle_branch_command(MessageEvent(text="/fork idea", source=source))
    thread = _thread_source()
    child = store.get_or_create_session(thread)
    store._db.append_message(child.session_id, role="user", content="NEW q")
    store._db.append_message(child.session_id, role="assistant", content="NEW a")
    # The replay really does fold three user rows into one (the condition the bug needs).
    replay = store.load_transcript(child.session_id)
    assert len(replay) == 4 and "NEW q" in replay[2]["content"] and "mid-turn ask" in replay[2]["content"]

    seen = _capture_merge(runner)
    result = await runner._handle_merge_command(MessageEvent(text="/merge", source=thread))

    assert "DELTA SUMMARY" in result or "Folded" in result
    assert [m["content"] for m in seen["history"]] == ["NEW q", "NEW a"]
    parent_rows = [r["content"] for r in store._db.get_messages(parent.session_id)]
    assert parent_rows[:3] == ["hello", "world", "mid-turn ask"]
    assert any("DELTA SUMMARY" in c for c in parent_rows)


@pytest.mark.asyncio
async def test_merge_delta_after_branching_an_unexplored_branch_again(store):
    """Prism P1 b3522bab429c: fork, then fork the child before any human turn. The grandchild's copied
    history ends with the first notice; replay coalesces notice1 + notice2 + the first prompt, so an
    index into the repaired replay drops the prompt. Zero new turns stays 'nothing new', too."""
    source = _channel_source()
    _seed(store, source)
    adapter = _ThreadAdapter()
    runner = _runner(store, adapter)
    await runner._handle_branch_command(MessageEvent(text="/fork one", source=source))
    child1_src = _thread_source("777000")
    child1 = store.get_or_create_session(child1_src)
    adapter.thread_id = "777001"
    await runner._handle_branch_command(MessageEvent(text="/fork two", source=child1_src))
    child2_src = _thread_source("777001")
    child2 = store.get_or_create_session(child2_src)
    import json
    assert json.loads(store._db.get_session(child2.session_id)["model_config"])["_branched_from"] == child1.session_id
    raw = store._db.get_messages(child2.session_id)
    assert [r.get("display_kind") for r in raw] == [None, None, FORK_NOTICE_DISPLAY_KIND, FORK_NOTICE_DISPLAY_KIND]

    seen = _capture_merge(runner)
    result = await runner._handle_merge_command(MessageEvent(text="/merge", source=child2_src))
    assert "nothing new" in result.lower() or "no turns" in result.lower()
    assert "history" not in seen

    store._db.append_message(child2.session_id, role="user", content="NEW q")
    store._db.append_message(child2.session_id, role="assistant", content="NEW a")
    result = await runner._handle_merge_command(MessageEvent(text="/merge", source=child2_src))
    assert "DELTA SUMMARY" in result or "Folded" in result
    assert [m["content"] for m in seen["history"]] == ["NEW q", "NEW a"]
    # Folded into child1 (its parent), which keeps its own notice row intact.
    child1_rows = store._db.get_messages(child1.session_id)
    assert child1_rows[2]["display_kind"] == FORK_NOTICE_DISPLAY_KIND
    assert any("DELTA SUMMARY" in (r["content"] or "") for r in child1_rows)
