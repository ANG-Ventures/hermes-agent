"""prompt.submit ``system_context`` reaches the model as SYSTEM text (t_c6793d84).

The clanker voice client used to prefix ``[origin_room=<room>]`` onto the USER
text of every warm turn. Models mirror a leading bracketed user line into a
share of replies (2-16% of fresh turns measured on gemini-3.5-flash-lite), and
the voice path speaks the reply. Caller-owned turn metadata therefore gets its
own field: prompt.submit ``system_context`` is appended to the agent's
ephemeral system prompt for that turn only, and the user message is the bare
utterance.

The contract pinned here is the REQUEST SHAPE, not model output: during the
turn the ephemeral system prompt carries the context, the user message does
not, and both are back to their pre-turn values afterwards.
"""
from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server

ROOM_LINE = "Turn metadata (trusted): origin_room=kitchen"


class _InlineThread:
    """Run the turn synchronously so tests observe its final state."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "gw-session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        "transport": None,
        **extra,
    }


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


def _recording_agent(base_ephemeral):
    seen: dict = {}
    agent = types.SimpleNamespace(
        session_id="agent-sid",
        ephemeral_system_prompt=base_ephemeral,
        clear_interrupt=lambda: None,
    )

    def run_conversation(user_message, **kwargs):
        seen["user_message"] = user_message
        seen["ephemeral_during_turn"] = agent.ephemeral_system_prompt
        return {"final_response": "pong"}

    agent.run_conversation = run_conversation
    return agent, seen


class TestRequestShape:
    def test_context_goes_to_system_not_user_text(self, turn_env):
        agent, seen = _recording_agent(None)
        session = _session(agent=agent, running=True, turn_system_context=ROOM_LINE)
        server._run_prompt_submit("rid", "ui-sid", session, "Reply with pong.")
        assert seen["user_message"] == "Reply with pong."
        assert "origin_room" not in seen["user_message"]
        assert seen["ephemeral_during_turn"] == ROOM_LINE

    def test_appends_after_the_profile_ephemeral_prompt(self, turn_env):
        agent, seen = _recording_agent("PROFILE PERSONA")
        session = _session(agent=agent, running=True, turn_system_context=ROOM_LINE)
        server._run_prompt_submit("rid", "ui-sid", session, "go")
        assert seen["ephemeral_during_turn"] == "PROFILE PERSONA\n\n" + ROOM_LINE

    def test_restored_after_the_turn(self, turn_env):
        agent, _ = _recording_agent("PROFILE PERSONA")
        session = _session(agent=agent, running=True, turn_system_context=ROOM_LINE)
        server._run_prompt_submit("rid", "ui-sid", session, "go")
        assert agent.ephemeral_system_prompt == "PROFILE PERSONA"

    def test_restored_when_the_turn_raises(self, turn_env):
        agent = types.SimpleNamespace(
            session_id="agent-sid",
            ephemeral_system_prompt=None,
            clear_interrupt=lambda: None,
        )

        def boom(*a, **k):
            raise RuntimeError("provider down")

        agent.run_conversation = boom
        session = _session(agent=agent, running=True, turn_system_context=ROOM_LINE)
        server._run_prompt_submit("rid", "ui-sid", session, "go")
        assert agent.ephemeral_system_prompt is None

    def test_no_context_leaves_the_system_prompt_alone(self, turn_env):
        agent, seen = _recording_agent("PROFILE PERSONA")
        session = _session(agent=agent, running=True)
        server._run_prompt_submit("rid", "ui-sid", session, "go")
        assert seen["ephemeral_during_turn"] == "PROFILE PERSONA"


class TestNormalization:
    def test_non_string_is_ignored(self):
        assert server._turn_system_context({"room": "kitchen"}) == ""
        assert server._turn_system_context(None) == ""

    def test_bounded(self):
        out = server._turn_system_context("x" * 50_000)
        assert len(out) == server.TURN_SYSTEM_CONTEXT_MAX_CHARS

    def test_compose(self):
        assert server._with_turn_system_context(None, "ctx") == "ctx"
        assert server._with_turn_system_context("base", "ctx") == "base\n\nctx"
        assert server._with_turn_system_context("base", "") == "base"


class TestSubmitRecording:
    """``prompt.submit`` stores the context per submit, like ``surface``."""

    @pytest.fixture
    def busy_session(self):
        session = _session(running=True)
        server._sessions["sid"] = session
        yield session
        server._sessions.pop("sid", None)

    def _submit(self, **params):
        return server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "what is this?", "queued": True, **params}
        )

    def test_recorded(self, busy_session):
        self._submit(system_context=ROOM_LINE)
        assert busy_session["turn_system_context"] == ROOM_LINE

    def test_next_submit_without_it_clears_it(self, busy_session):
        self._submit(system_context=ROOM_LINE)
        self._submit()
        assert busy_session["turn_system_context"] == ""


def test_session_create_advertises_the_capability():
    """Clients feature-detect on this key before moving metadata out of text."""
    import inspect

    from tui_gateway import methods_session

    assert '"turn_system_context": True' in inspect.getsource(methods_session)


class TestAdvertisedOnResumeAndInfo:
    """A resumed session must advertise the capability too (t_4ab7901d).

    session.create was the only payload carrying the key, so a client that
    feature-detects on a resumed session (or on a session.info event) read the
    feature as unsupported and kept the metadata in the user text.
    """

    def test_session_info(self):
        agent = types.SimpleNamespace(model="m", provider="p", session_id="k")
        info = server._session_info(agent, _session(agent=agent))
        assert info["turn_system_context"] is True

    def test_lazy_resume_info(self):
        assert server._lazy_resume_info("/tmp")["turn_system_context"] is True

    def test_fallback_info_for_unbuilt_session(self):
        session = _session()
        session["agent"] = None
        assert server._fallback_session_info(session)["turn_system_context"] is True


class _FakeSupervisor:
    def __init__(self):
        self.frames = []
        self.callback = None

    def submit_turn(self, frame, *, on_complete=None):
        self.frames.append(frame)
        self.callback = on_complete
        return frame["request_id"]


class TestComputeHostTurns:
    """turn_isolation turns carry system_context to the child (t_4ab7901d).

    With dashboard.turn_isolation on, prompt.submit hands the turn to the
    compute host as a ``turn.start`` frame and the child runs
    _run_prompt_submit against ITS OWN session record. The context therefore
    has to ride the frame and be re-applied in the child, or the model never
    sees it.
    """

    @pytest.fixture
    def isolated(self, monkeypatch):
        sup = _FakeSupervisor()
        session = _session()
        session["agent"] = None
        session["agent_ready"] = threading.Event()
        server._sessions["iso-sid"] = session
        monkeypatch.setattr(server, "_load_cfg", lambda: {"dashboard": {"turn_isolation": True}})
        monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: sup)
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda _s: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda _s: None)
        yield sup
        server._sessions.pop("iso-sid", None)

    def _submit(self, **params):
        return server.handle_request(
            {
                "id": "submit",
                "method": "prompt.submit",
                "params": {"session_id": "iso-sid", "text": "Reply with pong.", **params},
            }
        )

    def test_frame_carries_the_context(self, isolated):
        resp = self._submit(system_context=ROOM_LINE)
        assert resp["result"]["turn_isolation"] is True
        assert isolated.frames[0]["system_context"] == ROOM_LINE
        assert "origin_room" not in isolated.frames[0]["text"]

    def test_frame_without_context_is_empty(self, isolated):
        self._submit()
        assert isolated.frames[0]["system_context"] == ""

    @staticmethod
    def _run_in_child(monkeypatch, frame, child_session):
        from tui_gateway.compute_host import ComputeHost

        emitted = []
        host = ComputeHost(heartbeat_secs=0)
        monkeypatch.setattr(host, "emit", emitted.append)
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda _s: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda _s: None)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
        server._sessions[frame["sid"]] = child_session
        try:
            host._run_real_turn(frame)
        finally:
            server._sessions.pop(frame["sid"], None)
        return emitted

    def test_child_applies_frame_context_to_the_model(self, turn_env, monkeypatch):
        agent, seen = _recording_agent("PROFILE PERSONA")
        frame = {
            "type": "turn.start",
            "sid": "child-sid",
            "request_id": "r1",
            "session_key": "gw-session-key",
            "text": "Reply with pong.",
            "system_context": ROOM_LINE,
        }
        emitted = self._run_in_child(monkeypatch, frame, _session(agent=agent))
        assert [f["type"] for f in emitted][-1] == "turn.end", emitted
        assert seen["user_message"] == "Reply with pong."
        assert seen["ephemeral_during_turn"] == "PROFILE PERSONA\n\n" + ROOM_LINE
        assert agent.ephemeral_system_prompt == "PROFILE PERSONA"

    def test_child_clears_stale_context_when_frame_omits_it(self, turn_env, monkeypatch):
        agent, seen = _recording_agent("PROFILE PERSONA")
        frame = {
            "type": "turn.start",
            "sid": "child-sid",
            "request_id": "r2",
            "session_key": "gw-session-key",
            "text": "go",
        }
        child = _session(agent=agent, turn_system_context=ROOM_LINE)
        self._run_in_child(monkeypatch, frame, child)
        assert seen["ephemeral_during_turn"] == "PROFILE PERSONA"
        assert child["turn_system_context"] == ""

    def test_parent_frame_round_trips_through_the_child(self, isolated, turn_env, monkeypatch):
        self._submit(system_context=ROOM_LINE)
        frame = dict(isolated.frames[0])
        frame["sid"] = "child-sid"
        agent, seen = _recording_agent(None)
        self._run_in_child(monkeypatch, frame, _session(agent=agent))
        assert seen["ephemeral_during_turn"] == ROOM_LINE
        assert seen["user_message"] == "Reply with pong."
