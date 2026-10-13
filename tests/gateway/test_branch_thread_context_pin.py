"""/branch into a NEW Discord thread keeps the parent's session-context bytes (t_84dabcc7).

Measured on the puppet lane (t_1908b8bc): the thread child's first turn re-rendered the
"## Current Session Context" paragraph from the thread source (``thread: <id>``,
``Multi-user thread``). That paragraph sits ahead of the tool schemas and the copied history in the
cached prefix, so the child re-wrote ~3.8k tokens: 61% prefix hit vs 99% for ``/branch --here``.

The fix seeds the child's pin with the parent's bytes at /branch time; the child's first human turn
adopts them and carries the thread's own facts on its user message (sidecar note) instead.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import (
    BRANCH_INHERITED_PIN_KEY, SessionContext, SessionSource, SessionStore, branch_thread_context_note,
    build_session_context_prompt,
)
from tests.gateway.test_internal_event_pin_wiring import KEY, _capture, _human_source, _make_runner
from hermes_state import AsyncSessionDB

THREAD_ID = "1552000000000000777"


def _child_thread_source() -> SessionSource:
    # The shape a follow-up typed in the branch thread arrives on (branch_dest_source + adapter).
    parent = _human_source()
    return SessionSource(
        platform=Platform.DISCORD, chat_id=THREAD_ID, chat_type="thread", thread_id=THREAD_ID,
        parent_chat_id=parent.chat_id, chat_name="Guild / side quest", user_id=parent.user_id,
        user_name="Ace", scope_id=parent.scope_id, message_id="1552671843494666999",
    )


async def _turn(runner, source, key):
    event = MessageEvent(text="hi", source=source, message_id=source.message_id)
    await runner._handle_message_with_agent(event, source, key, 1)


def _child_key(runner) -> str:
    # The handler routes on the store entry's key; give the child thread its own entry, as the
    # real SessionStore does for a new thread (the shared fixture store answers KEY for everything).
    from datetime import datetime
    from gateway.session import SessionEntry

    key = runner._session_key_for_source(_child_thread_source())
    parent_entry = runner.session_store.get_or_create_session.return_value
    child_entry = SessionEntry(
        session_key=key, session_id="sess-child", created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2), platform=Platform.DISCORD, chat_type="thread")
    runner.session_store.get_or_create_session.return_value = None
    runner.session_store.get_or_create_session.side_effect = (
        lambda src, **kw: child_entry if src.thread_id == THREAD_ID else parent_entry)
    return key


@pytest.mark.asyncio
async def test_branch_thread_child_first_turn_replays_parent_context_bytes(monkeypatch):
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)

    await _turn(runner, _human_source(), KEY)
    parent_bytes = calls[0]["context_prompt"]
    # The fixture only proves something if the thread source renders DIFFERENT bytes.
    child_render = build_session_context_prompt(SessionContext(
        source=_child_thread_source(), connected_platforms=[], home_channels={}))
    assert child_render != parent_bytes and f"thread: {THREAD_ID}" in child_render

    # What /branch does for a new-thread child (wired in _handle_branch_command).
    CHILD_KEY = _child_key(runner)
    assert CHILD_KEY != KEY
    assert runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    await _turn(runner, _child_thread_source(), CHILD_KEY)

    child_first, child_second = calls[1]["context_prompt"], calls[2]["context_prompt"]
    assert child_first == parent_bytes, "branch child re-rendered its session context (cold prefix)"
    assert child_second == parent_bytes, "inherited pin must stay byte-stable on later turns"


@pytest.mark.asyncio
async def test_branch_thread_note_is_staged_on_first_child_turn_only(monkeypatch):
    runner = _make_runner(monkeypatch)
    _capture(runner, [])
    staged: list[list[str]] = []
    orig = runner._set_pending_turn_sidecar_notes
    runner._set_pending_turn_sidecar_notes = lambda k, notes: (staged.append(list(notes)), orig(k, notes))

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    await _turn(runner, _child_thread_source(), CHILD_KEY)

    notes = [n for batch in staged for n in batch if "[Session context for this branch]" in n]
    assert len(notes) == 1
    assert f"**Thread:** `{THREAD_ID}`" in notes[0] and '"Guild / side quest"' in notes[0]


# ------------------------------------------------- aborted first turn keeps the pin adoptable
# Prism d78579970af8 / b6bd0d66ca29 on #1923: the sentinel was re-keyed BEFORE the two early-outs
# in ``_hmwa_prepare_turn``, so a child whose first turn aborted kept the parent's bytes with no
# note ever delivered (Discord tools then target the parent channel).

async def _first_turn_aborts_then_succeeds(runner, abort):
    staged: list[list[str]] = []
    orig = runner._set_pending_turn_sidecar_notes
    runner._set_pending_turn_sidecar_notes = lambda k, notes: (staged.append(list(notes)), orig(k, notes))

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()
    undo = abort(runner)
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert staged == [], "an aborted turn must stage nothing"
    pin = runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin
    assert pin is not None and pin[0] == BRANCH_INHERITED_PIN_KEY, "aborted turn consumed the inherited pin"
    undo()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    notes = [n for batch in staged for n in batch if "[Session context for this branch]" in n]
    assert len(notes) == 1, "retry after an aborted first turn never delivered the branch note"


@pytest.mark.asyncio
async def test_history_unreadable_early_out_keeps_inherited_pin_adoptable(monkeypatch):
    from gateway.session_transcript import TranscriptReadError

    runner = _make_runner(monkeypatch)
    _capture(runner, [])

    def abort(r):
        store = r.session_store
        prev = store.load_transcript.side_effect
        store.load_transcript.side_effect = TranscriptReadError("db locked")
        return lambda: setattr(store.load_transcript, "side_effect", prev)

    await _first_turn_aborts_then_succeeds(runner, abort)


@pytest.mark.asyncio
async def test_rejected_inbound_text_early_out_keeps_inherited_pin_adoptable(monkeypatch):
    runner = _make_runner(monkeypatch)
    _capture(runner, [])

    def abort(r):
        prev = r._prepare_profile_scoped_inbound_message_text

        async def _drop(**kw):
            return None

        r._prepare_profile_scoped_inbound_message_text = _drop
        return lambda: setattr(r, "_prepare_profile_scoped_inbound_message_text", prev)

    await _first_turn_aborts_then_succeeds(runner, abort)


def test_inherited_pin_never_crosses_privacy_policy(monkeypatch):
    runner = _make_runner(monkeypatch)
    ctx = SessionContext(source=_human_source(), connected_platforms=[], home_channels={})
    runner._pinned_session_context_prompt(ctx, False, KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    child_ctx = SessionContext(source=_child_thread_source(), connected_platforms=[], home_channels={})
    assert runner._stage_inherited_context_pin(child_ctx, True, CHILD_KEY) is None
    assert runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin is None
    assert runner._commit_inherited_context_pin(child_ctx, True, CHILD_KEY) is False


def test_branch_thread_context_note_renders_thread_facts():
    note = branch_thread_context_note(SessionContext(
        source=_child_thread_source(), connected_platforms=[], home_channels={},
        shared_multi_user_session=True))
    assert "**Session type:** Multi-user thread" in note
    assert '**Source:** Discord thread "Guild / side quest"' in note
    assert f"**Thread:** `{THREAD_ID}` (discord tools `channel_id`; `\"origin\"` delivers here)" in note
    assert note.count(THREAD_ID) == 1, "thread id is the costliest token run; render it once"


# ---------------------------------------------------------------- the real /branch handler wiring

class _ThreadAdapter:
    async def create_handoff_thread(self, parent_chat_id, name):
        return "777000"

    async def send(self, chat_id, text, **kwargs):
        return None


@pytest.mark.asyncio
async def test_real_branch_command_seeds_child_pin_with_parent_bytes(tmp_path, monkeypatch):
    import hermes_state
    from gateway.run import GatewayRunner

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: _ThreadAdapter()}
    runner._profile_adapters = {}
    runner.config = {}
    runner._background_tasks = set()
    runner._running_agents, runner._running_agents_ts, runner._busy_ack_ts = {}, {}, {}
    runner._pending_approvals, runner._update_prompt_pending = {}, {}
    runner._agent_cache_lock = None
    runner.session_store = store
    runner._session_db = AsyncSessionDB(store._db)
    runner._pending_skills_reload_notes = {}

    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="u1",
                           user_name="ann", scope_id="g9")
    parent = store.get_or_create_session(source)
    store._db.append_message(parent.session_id, role="user", content="hello")
    store._db.append_message(parent.session_id, role="assistant", content="world")
    parent_key = runner._session_key_for_source(source)
    parent_bytes = runner._pinned_session_context_prompt(
        SessionContext(source=source, connected_platforms=[], home_channels={}), False, parent_key)

    await runner._handle_branch_command(MessageEvent(text="/branch side quest", source=source))

    child_source = SessionSource(platform=Platform.DISCORD, chat_id="777000", chat_type="thread",
                                 thread_id="777000", parent_chat_id="123", user_id="u1", scope_id="g9")
    child_key = runner._session_key_for_source(child_source)
    assert child_key != parent_key
    pin = runner._peek_session_state(child_key).conversation.ephemeral_pin
    assert pin is not None and pin[1] == parent_bytes

    # /branch --here keeps the same key; nothing is inherited (the key's own pin rules apply).
    runner2_key_state = runner._peek_session_state(parent_key)
    await runner._handle_branch_command(MessageEvent(text="/branch --here again", source=source))
    assert runner2_key_state.conversation.ephemeral_pin is None


# ------------------------------------------- routing the inherited bytes do not override (r2)
# Apollo r2 on #1923 (Prism 89f1a3a47431 / b6bd0d66ca29): the parent's pinned bytes also state
# Connected Platforms, Home Channels and the non-origin delivery options. The branch note overrides
# only source/ids/origin, so the parent bytes may be served only while they still say exactly what
# this thread would render for the rest; and an internal first turn must get the note too.

def _set_homes(runner, *homes):
    from gateway.config import HomeChannel

    table = {p: HomeChannel(platform=p, chat_id=cid, name=name) for p, cid, name in homes}
    runner.config.get_connected_platforms = lambda: list(table)
    runner.config.get_home_channel = lambda p: table.get(p)


@pytest.mark.asyncio
async def test_inherited_pin_survives_when_home_channels_unchanged(monkeypatch):
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    _set_homes(runner, (Platform.DISCORD, "111", "ops"), (Platform.TELEGRAM, "222", "phone"))

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert "**Home Channels" in calls[0]["context_prompt"]
    assert calls[1]["context_prompt"] == calls[0]["context_prompt"], "unchanged homes must keep the warm prefix"


@pytest.mark.parametrize("child_homes", [
    pytest.param([(Platform.DISCORD, "999", "new-home"), (Platform.TELEGRAM, "222", "phone")], id="sethome"),
    pytest.param([(Platform.DISCORD, "111", "ops")], id="fewer-homes"),
])
@pytest.mark.asyncio
async def test_home_channel_drift_drops_inherited_pin_and_renders_own_context(monkeypatch, child_homes):
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    staged: list[list[str]] = []
    orig = runner._set_pending_turn_sidecar_notes
    runner._set_pending_turn_sidecar_notes = lambda k, notes: (staged.append(list(notes)), orig(k, notes))
    _set_homes(runner, (Platform.DISCORD, "111", "ops"), (Platform.TELEGRAM, "222", "phone"))

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    _set_homes(runner, *child_homes)  # /sethome (or a platform drop) after the parent last rendered
    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)

    child = calls[1]["context_prompt"]
    assert child != calls[0]["context_prompt"], "stale parent homes served as current"
    assert child == runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin[1]
    for _p, cid, name in child_homes:
        assert f'"{name}" (ID: "{cid}")' in child
    assert not any("[Session context for this branch]" in n for batch in staged for n in batch), \
        "own render needs no override note"


@pytest.mark.asyncio
async def test_internal_first_child_turn_gets_branch_note_and_keeps_sentinel(monkeypatch):
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    staged: list[list[str]] = []
    orig = runner._set_pending_turn_sidecar_notes
    runner._set_pending_turn_sidecar_notes = lambda k, notes: (staged.append(list(notes)), orig(k, notes))

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()

    wake = SessionSource(platform=Platform.DISCORD, chat_id=THREAD_ID, chat_type="thread",
                         thread_id=THREAD_ID, user_id=_human_source().user_id, scope_id=_human_source().scope_id)
    await runner._handle_message_with_agent(
        MessageEvent(text="[kanban] wake", source=wake, internal=True), wake, CHILD_KEY, 1)
    internal_notes = [n for batch in staged for n in batch if "[Session context for this branch]" in n]
    assert len(internal_notes) == 1, "internal first turn read the parent's channel with no correction"
    assert f"`{THREAD_ID}`" in internal_notes[0]
    pin = runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin
    assert pin[0] == BRANCH_INHERITED_PIN_KEY, "internal turn must leave adoption to the first human turn"
    assert calls[1]["context_prompt"] == calls[0]["context_prompt"]

    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert len([n for batch in staged for n in batch if "[Session context for this branch]" in n]) == 1
    assert runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin[0] != BRANCH_INHERITED_PIN_KEY
    assert calls[2]["context_prompt"] == calls[0]["context_prompt"]


def test_branch_note_scopes_its_override_and_vouches_for_the_rest():
    note = branch_thread_context_note(SessionContext(
        source=_child_thread_source(), connected_platforms=[], home_channels={}))
    assert "Home Channels and delivery options still hold" in note
    assert "Current Session Context" not in note, "a blanket override would void the still-true homes"


# ---------------------------------------------- the three paths that served the pin without the note
# Prism eb4a633bdc78 / f813c37414ba / 80879ea93439 on #1923 (t_09e413e8), fixed as one class: the
# correction note is staged the moment the inherited pin is served, rides every execution path
# (local agent AND gateway proxy), and the sentinel is adopted only once the annotated user row
# has been persisted, so any turn that ends without it stages the note again.

def _stage_spy(runner):
    staged: list[list[str]] = []
    orig = runner._set_pending_turn_sidecar_notes
    runner._set_pending_turn_sidecar_notes = lambda k, notes: (staged.append(list(notes)), orig(k, notes))
    return staged


def _branch_notes(staged):
    return [n for batch in staged for n in batch if "[Session context for this branch]" in n]


@pytest.mark.asyncio
async def test_hygiene_eviction_on_first_child_turn_still_delivers_the_note(monkeypatch):
    """Prism eb4a633bdc78: detached hygiene compression evicts the cached agent (clearing the pin)
    between the pin being served and the note being staged. The parent bytes went out with no
    correction."""
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    staged = _stage_spy(runner)

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()

    async def _hygiene_evicts(event, source, session_entry, session_key, history, *_a):
        runner._evict_cached_agent(session_key)  # _hmwa_hygiene_detached_attempt's finally
        return history

    runner._hmwa_run_session_hygiene = _hygiene_evicts
    await _turn(runner, _child_thread_source(), CHILD_KEY)

    assert calls[1]["context_prompt"] == calls[0]["context_prompt"], "turn served the inherited bytes"
    notes = _branch_notes(staged)
    assert len(notes) == 1, "parent ids reached the model with no branch correction"
    assert f"`{THREAD_ID}`" in notes[0]
    # The eviction dropped the pin; the next turn renders the thread's own context (no note needed).
    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert f"thread: {THREAD_ID}" in calls[2]["context_prompt"]
    assert _branch_notes(staged) == []


@pytest.mark.parametrize("shape", ["cancelled", "failed"])
@pytest.mark.asyncio
async def test_first_child_turn_that_does_not_persist_restages_the_note(monkeypatch, shape):
    """Prism 80879ea93439: the sentinel was re-keyed during turn preparation. A first run that was
    cancelled (/stop superseded its generation: result discarded, nothing persisted) or failed
    kept the parent bytes for every later turn and never emitted the correction again."""
    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    staged = _stage_spy(runner)

    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()

    good_run_agent = runner._run_agent
    current = {"ok": True}
    runner._is_session_run_current = lambda k, g: current["ok"]

    async def _bad_run_agent(**kw):
        calls.append(kw)
        if shape == "cancelled":
            current["ok"] = False  # /stop mid-run: the result is discarded, no row persisted
            return {"final_response": "", "messages": [], "tools": [], "history_offset": 0}
        return {"final_response": "", "messages": [], "tools": [], "history_offset": 0, "failed": True,
                "error": "503", "api_calls": 1}

    runner._run_agent = _bad_run_agent
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert len(_branch_notes(staged)) == 1, "the first run must still be handed the note"
    pin = runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin
    assert pin is not None and pin[0] == BRANCH_INHERITED_PIN_KEY, \
        f"{shape} first turn adopted the pin without persisting the correction"

    current["ok"] = True
    runner._run_agent = good_run_agent
    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert len(_branch_notes(staged)) == 1, "retry after a lost first turn never re-delivered the note"
    assert calls[-1]["context_prompt"] == calls[0]["context_prompt"]
    assert runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin[0] != BRANCH_INHERITED_PIN_KEY

    staged.clear()
    await _turn(runner, _child_thread_source(), CHILD_KEY)
    assert _branch_notes(staged) == [], "an adopted pin must not keep re-staging the note"


@pytest.mark.asyncio
async def test_internal_first_turn_restages_until_a_human_turn_persists(monkeypatch):
    """A kanban wake on the child reads the parent bytes too: it gets the note every time until the
    first human turn commits the pin (Prism b6bd0d66ca29, kept under the stage/commit split)."""
    runner = _make_runner(monkeypatch)
    _capture(runner, [])
    staged = _stage_spy(runner)
    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    staged.clear()
    wake = SessionSource(platform=Platform.DISCORD, chat_id=THREAD_ID, chat_type="thread",
                         thread_id=THREAD_ID, user_id=_human_source().user_id, scope_id=_human_source().scope_id)
    for _ in range(2):
        await runner._handle_message_with_agent(
            MessageEvent(text="[kanban] wake", source=wake, internal=True), wake, CHILD_KEY, 1)
    assert len(_branch_notes(staged)) == 2
    assert runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin[0] == BRANCH_INHERITED_PIN_KEY


# ------------------------------------------------------------------- the gateway proxy path

def _proxy_session():
    from tests.gateway.test_proxy_mode import _FakeSession, _FakeSSEResponse
    return _FakeSession(_FakeSSEResponse(
        status=200, sse_chunks=['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"]))


@pytest.mark.asyncio
async def test_proxied_child_first_turn_forwards_the_note_and_persists_it(monkeypatch):
    """Prism f813c37414ba: with ``gateway.proxy_url`` set, ``_run_agent_inner`` returns through
    ``_run_agent_via_proxy`` before the local TurnRunner consumes the staged notes. The remote got
    the parent's session facts and the unmodified user message."""
    from unittest.mock import patch
    from tests.gateway.test_proxy_mode import _patch_aiohttp

    runner = _make_runner(monkeypatch)
    calls: list[dict] = []
    _capture(runner, calls)
    await _turn(runner, _human_source(), KEY)
    CHILD_KEY = _child_key(runner)
    runner._inherit_branch_context_pin(KEY, CHILD_KEY)
    parent_bytes = calls[0]["context_prompt"]

    session = _proxy_session()
    monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
    monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)

    async def _through_proxy(**kw):
        with patch("gateway.run._load_gateway_config", return_value={}), _patch_aiohttp(session), \
                patch("aiohttp.ClientTimeout"):
            return await runner._run_agent_via_proxy(
                message=kw["message"], context_prompt=kw["context_prompt"], history=kw["history"],
                source=kw["source"], session_id=kw["session_id"], session_key=kw["session_key"],
                run_generation=kw.get("run_generation"))

    runner._run_agent = _through_proxy
    runner.session_store.append_to_transcript.reset_mock()
    await _turn(runner, _child_thread_source(), CHILD_KEY)

    sent = session.captured_json["messages"]
    assert sent[0] == {"role": "system", "content": parent_bytes}, "proxied turn must still serve the warm bytes"
    assert sent[-1]["role"] == "user" and "[Triggering message id" in sent[-1]["content"]
    assert "[Session context for this branch]" in sent[-1]["content"], "remote model saw the parent ids uncorrected"
    assert f"`{THREAD_ID}`" in sent[-1]["content"]
    # The sent bytes are the user row's api_content sidecar: later replays (local or proxied)
    # keep the correction, and the clean text stays the durable content.
    user_rows = [c.args[1] for c in runner.session_store.append_to_transcript.call_args_list
                 if c.args[1].get("role") == "user"]
    assert user_rows and user_rows[0]["content"].endswith("hi")
    assert user_rows[0].get("api_content") == sent[-1]["content"]
    assert "[Session context for this branch]" not in user_rows[0]["content"]
    assert runner._peek_session_state(CHILD_KEY).conversation.ephemeral_pin[0] != BRANCH_INHERITED_PIN_KEY
    assert runner._peek_session_state(CHILD_KEY).conversation.sidecar_notes == []


@pytest.mark.asyncio
async def test_proxy_replays_api_content_and_restages_notes_on_failure(monkeypatch):
    from unittest.mock import patch
    from gateway.run import GatewayRunner
    from tests.gateway.test_proxy_mode import _FakeSSEResponse, _FakeSession, _make_runner as _proxy_runner, \
        _make_source, _patch_aiohttp

    runner = _proxy_runner()
    monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
    monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
    history = [
        {"role": "user", "content": "hi", "api_content": "hi\n\n[Session context for this branch] x"},
        {"role": "assistant", "content": "hello"},
    ]
    session = _proxy_session()
    GatewayRunner._set_pending_turn_sidecar_notes(runner, "sk", ["[note A]", "[note B]"])
    with patch("gateway.run._load_gateway_config", return_value={}), _patch_aiohttp(session), \
            patch("aiohttp.ClientTimeout"):
        result = await runner._run_agent_via_proxy(
            message="again", context_prompt="sys", history=history, source=_make_source(),
            session_id="s", session_key="sk")
    sent = session.captured_json["messages"]
    assert sent[1]["content"] == history[0]["api_content"], "proxy replayed content, not the sent bytes"
    assert sent[-1]["content"] == "again\n\n[note A]\n\n[note B]"
    assert result["messages"][0] == {"role": "user", "content": "again", "api_content": sent[-1]["content"]}
    assert GatewayRunner._consume_pending_turn_sidecar_notes(runner, "sk") == [], "delivered notes must not replay"

    # A proxy error persists no annotated row: the notes are owed to the next turn.
    GatewayRunner._set_pending_turn_sidecar_notes(runner, "sk", ["[note C]"])
    failing = _FakeSession(_FakeSSEResponse(status=503, error_text="down"))
    with patch("gateway.run._load_gateway_config", return_value={}), _patch_aiohttp(failing), \
            patch("aiohttp.ClientTimeout"):
        result = await runner._run_agent_via_proxy(
            message="x", context_prompt="", history=[], source=_make_source(), session_id="s", session_key="sk")
    assert result["api_calls"] == 0
    assert GatewayRunner._consume_pending_turn_sidecar_notes(runner, "sk") == ["[note C]"]
