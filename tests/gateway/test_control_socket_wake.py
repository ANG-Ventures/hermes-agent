"""Control-socket ``wake`` verb (t_0fe12e34).

A detached process starts an agent turn in a chat through the gateway's own
control socket instead of minting a carrier kanban card. Routing must be the
kanban notifier's (``_build_wake_source``): the same live-routing identity
fill and phantom refusal, so a wake lands in the human's real session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.control_socket import (
    GatewayControlServer,
    wake_gateway_session,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

CHAT = "1535189663533506600"
HUMAN = "117431298246705156"
OTHER = "220000000000000001"


class RecordingAdapter:
    def __init__(self) -> None:
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):  # pragma: no cover
        return None

    async def handle_message(self, event):
        self.handled.append(event)
        # gateway.wake.admit_internal_event requires this receipt; a handler
        # returning None is not acceptance (same double as the notifier suites).
        event._gateway_accepted = True


class RaisingAdapter(RecordingAdapter):
    async def handle_message(self, event):
        raise RuntimeError("adapter refused")


class NonPushAdapter(RecordingAdapter):
    supports_async_delivery = False


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = SessionDB(db_path=tmp_path / "state.db")
    return s


def _runner(store, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store = store
    return runner


def _human_turn(store, user_id=HUMAN):
    return store.get_or_create_session(
        SessionSource(
            platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
            user_id=user_id,
        )
    )


def _params(**kw):
    p = {"platform": "discord", "chat_id": CHAT, "chat_type": "group",
         "text": "dispatch finished: brief at /tmp/x.md"}
    p.update(kw)
    return p


# -- routing through the shared helper ---------------------------------------

def test_wake_lands_in_the_humans_own_session(store):
    human = _human_turn(store)
    adapter = RecordingAdapter()
    result = asyncio.run(_runner(store, adapter)._deliver_control_wake(_params()))

    assert result["delivered"] is True, result
    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert event.internal is True
    assert event.text == "dispatch finished: brief at /tmp/x.md"
    assert event.source.user_id == HUMAN
    woken = store.get_or_create_session(event.source)
    assert woken.session_key == human.session_key
    assert woken.session_id == human.session_id


def test_named_user_is_authoritative_over_live_evidence(store):
    _human_turn(store)
    adapter = RecordingAdapter()
    result = asyncio.run(
        _runner(store, adapter)._deliver_control_wake(_params(user_id=OTHER))
    )
    assert result["delivered"] is True
    assert adapter.handled[0].source.user_id == OTHER


def test_two_humans_refuse_to_pick_a_participant(store):
    _human_turn(store, HUMAN)
    _human_turn(store, OTHER)
    adapter = RecordingAdapter()
    result = asyncio.run(_runner(store, adapter)._deliver_control_wake(_params()))
    assert result["delivered"] is True
    assert adapter.handled[0].source.user_id is None


def test_thread_id_and_profile_reach_the_source(store):
    adapter = RecordingAdapter()
    result = asyncio.run(
        _runner(store, adapter)._deliver_control_wake(
            _params(chat_type="thread", thread_id="999", user_id=HUMAN)
        )
    )
    assert result["delivered"] is True
    src = adapter.handled[0].source
    assert src.thread_id == "999"
    assert src.chat_type == "thread"


@pytest.mark.parametrize("missing", ["platform", "chat_id", "text", "chat_type"])
def test_missing_required_field_is_refused_without_delivery(store, missing):
    adapter = RecordingAdapter()
    params = _params()
    params[missing] = ""
    result = asyncio.run(_runner(store, adapter)._deliver_control_wake(params))
    assert result["delivered"] is False
    assert result["error"]
    assert adapter.handled == []


def test_unknown_platform_and_missing_adapter_fail(store):
    runner = _runner(store, RecordingAdapter())
    r1 = asyncio.run(runner._deliver_control_wake(_params(platform="nope")))
    assert r1["delivered"] is False and "unknown platform" in r1["error"]
    r2 = asyncio.run(runner._deliver_control_wake(_params(platform="telegram")))
    assert r2["delivered"] is False and "no connected telegram adapter" in r2["error"]


def test_non_push_adapter_is_refused(store):
    adapter = NonPushAdapter()
    result = asyncio.run(_runner(store, adapter)._deliver_control_wake(_params()))
    assert result["delivered"] is False
    assert adapter.handled == []


def test_adapter_exception_reports_failed(store):
    result = asyncio.run(
        _runner(store, RaisingAdapter())._deliver_control_wake(_params(user_id=HUMAN))
    )
    assert result["delivered"] is False
    assert "adapter refused" in result["error"]


# -- the verb on the socket ---------------------------------------------------

def test_request_handler_receives_params(tmp_path):
    seen = []
    server = GatewayControlServer(
        home=tmp_path,
        request_handlers={"wake": lambda p: seen.append(p) or {"delivered": True}},
    )
    raw = json.dumps({"verb": "wake", "id": 3, "params": {"a": 1}}).encode()
    response = json.loads(server.handle_request_line(raw).decode())
    assert response == {"ok": True, "protocol": 1, "result": {"delivered": True}, "id": 3}
    assert seen == [{"a": 1}]


def test_non_object_params_is_an_error(tmp_path):
    server = GatewayControlServer(
        home=tmp_path, request_handlers={"wake": lambda p: {"delivered": True}},
    )
    raw = json.dumps({"verb": "wake", "params": [1]}).encode()
    response = json.loads(server.handle_request_line(raw).decode())
    assert response["ok"] is False


def test_unknown_verb_lists_wake(tmp_path):
    server = GatewayControlServer(
        home=tmp_path, request_handlers={"wake": lambda p: {}},
    )
    response = json.loads(server.handle_request_line(b'{"verb": "x"}').decode())
    assert "wake" in response["supported_verbs"]
    assert "identify" in response["supported_verbs"]


def test_client_returns_none_without_a_gateway(tmp_path):
    assert wake_gateway_session(
        tmp_path, platform="discord", chat_id=CHAT, chat_type="group", text="hi",
        timeout=0.5,
    ) is None


@pytest.mark.skipif(sys.platform == "win32", reason="unix socket transport")
def test_wake_roundtrip_over_real_socket_into_runner(tmp_path, store):
    """client -> real unix socket -> executor-thread handler -> loop coroutine
    -> runner._deliver_control_wake -> adapter.handle_message, the same
    marshalling gateway/run.py wires."""
    human = _human_turn(store)
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)

    async def scenario():
        loop = asyncio.get_running_loop()

        def handler(params):
            fut = asyncio.run_coroutine_threadsafe(
                runner._deliver_control_wake(params), loop
            )
            return fut.result(timeout=10)

        server = GatewayControlServer(
            home=tmp_path / "gw", request_handlers={"wake": handler}
        )
        (tmp_path / "gw").mkdir()
        assert await server.start()
        try:
            return await loop.run_in_executor(
                None,
                lambda: wake_gateway_session(
                    tmp_path / "gw", platform="discord", chat_id=CHAT,
                    chat_type="group", text="woken over the socket", timeout=10,
                ),
            )
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result is not None and result["delivered"] is True, result
    assert adapter.handled[0].text == "woken over the socket"
    woken = store.get_or_create_session(adapter.handled[0].source)
    assert woken.session_id == human.session_id


def test_query_without_params_is_unchanged_for_observation_verbs(tmp_path):
    server = GatewayControlServer(home=tmp_path, verb_handlers={"status": lambda: {"x": 1}})
    raw = json.dumps({"verb": "status"}).encode()
    assert json.loads(server.handle_request_line(raw).decode())["result"] == {"x": 1}


# -- t_51b6e95f (Prism round 1 on #1675) --------------------------------------

def _mux_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = SessionStore(sessions_dir=tmp_path / "sessions",
                     config=GatewayConfig(multiplex_profiles=True))
    s._db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(SessionStore, "_active_profile_name",
                        staticmethod(lambda: "default"))
    return s


def _profile_turn(store, user_id, profile=None):
    return store.get_or_create_session(
        SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type="group",
                      user_id=user_id, profile=profile)
    )


def _mux_runner(store, adapter):
    runner = _runner(store, RecordingAdapter())
    runner._profile_adapters = {"coder": {Platform.DISCORD: adapter}}
    runner._active_profile_name = lambda: "default"
    return runner


def test_wake_identity_ignores_other_profiles_participants(tmp_path, monkeypatch):
    """Two profiles, two humans, one channel: the coder wake adopts coder's human."""
    store = _mux_store(tmp_path, monkeypatch)
    _profile_turn(store, OTHER)                     # default profile's human
    coder = _profile_turn(store, HUMAN, "coder")
    assert coder.session_key.startswith("agent:coder:")
    adapter = RecordingAdapter()
    result = asyncio.run(
        _mux_runner(store, adapter)._deliver_control_wake(_params(profile="coder"))
    )
    assert result["delivered"] is True, result
    src = adapter.handled[0].source
    assert src.user_id == HUMAN
    assert store.get_or_create_session(src).session_key == coder.session_key


def test_wake_never_adopts_another_profiles_participant(tmp_path, monkeypatch):
    store = _mux_store(tmp_path, monkeypatch)
    _profile_turn(store, OTHER)                     # only default has evidence
    adapter = RecordingAdapter()
    result = asyncio.run(
        _mux_runner(store, adapter)._deliver_control_wake(_params(profile="coder"))
    )
    assert result["delivered"] is True, result
    assert adapter.handled[0].source.user_id is None


class _PipeTransport:
    def __init__(self) -> None:
        self.written: list = []
        self.closed = asyncio.Event()

    def write(self, data):
        self.written.append(data)

    def is_closing(self):
        return self.closed.is_set()

    def close(self):
        self.closed.set()


def test_windows_pipe_runs_request_handlers_off_the_loop():
    """The pipe protocol must not call a (possibly blocking) handler on the loop.

    The handler is the wake shape: it schedules a coroutine on the gateway
    loop and blocks for its result. Inline on the loop thread that deadlocks
    and freezes the loop until the timeout.
    """
    import threading
    from gateway.control_socket import _PipeControlProtocol

    async def main():
        loop = asyncio.get_running_loop()
        seen = {}

        async def on_loop():
            return "ran"

        def handler(params):
            seen["thread"] = threading.get_ident()
            fut = asyncio.run_coroutine_threadsafe(on_loop(), loop)
            return {"delivered": fut.result(timeout=2)}

        server = GatewayControlServer(Path("/nonexistent"), request_handlers={"wake": handler})
        proto = _PipeControlProtocol(server)
        transport = _PipeTransport()
        proto.connection_made(transport)
        proto.data_received(json.dumps({"verb": "wake", "params": {}}).encode() + b"\n")
        await asyncio.wait_for(transport.closed.wait(), 5)
        return seen, transport.written, threading.get_ident()

    seen, written, loop_thread = asyncio.run(main())
    assert seen["thread"] != loop_thread
    assert json.loads(written[0])["result"] == {"delivered": "ran"}


# -- t_51b6e95f (Prism round 2 on #1675) --------------------------------------

def test_profile_route_resolved_before_participant_lookup(tmp_path, monkeypatch):
    """No profile in the request, chat routed to coder: the lookup must search
    coder's namespace and stamp coder, so the wake continues coder's human."""
    store = _mux_store(tmp_path, monkeypatch)
    coder = _profile_turn(store, HUMAN, "coder")
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)
    runner._active_profile_name = lambda: "default"
    runner._profile_name_for_source = (
        lambda src: "coder" if src.chat_id == CHAT else None
    )
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert result["delivered"] is True, result
    src = adapter.handled[0].source
    assert src.profile == "coder"
    assert src.user_id == HUMAN
    assert store.get_or_create_session(src).session_key == coder.session_key


def test_rejected_profile_route_fails_the_wake(store):
    from gateway.profile_routing import ProfileRouteRejected

    adapter = RecordingAdapter()
    runner = _runner(store, adapter)

    def _reject(src):
        raise ProfileRouteRejected("r")

    runner._profile_name_for_source = _reject
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert result["delivered"] is False
    assert adapter.handled == []


def test_handlers_never_run_on_the_loops_default_executor(tmp_path):
    """A blocked handler must not occupy the default pool its coroutine needs.

    Shrink the loop's default executor to ONE thread. The handler blocks on a
    loop coroutine that itself does asyncio.to_thread (as handle_message can).
    On the shared default pool that deadlocks until the handler's timeout.
    """
    import concurrent.futures as cf
    import threading

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(cf.ThreadPoolExecutor(max_workers=1))
        names = {}

        async def needs_default_pool():
            return await asyncio.to_thread(lambda: "pooled")

        def handler(params):
            names["handler"] = threading.current_thread().name
            fut = asyncio.run_coroutine_threadsafe(needs_default_pool(), loop)
            return {"v": fut.result(timeout=3)}

        server = GatewayControlServer(home=tmp_path / "gw", request_handlers={"wake": handler})
        (tmp_path / "gw").mkdir()
        assert await server.start()
        try:
            from gateway.control_socket import query_gateway_control
            reply = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                cf.ThreadPoolExecutor(max_workers=1),
                lambda: query_gateway_control(tmp_path / "gw", "wake", params={}, timeout=8),
            ), 10)
        finally:
            await server.stop()
        return reply, names

    reply, names = asyncio.run(scenario())
    assert names["handler"].startswith("gw-control")
    assert reply == {"v": "pooled"}, reply


# -- contention gate (t_74bf5296) ---------------------------------------------

def test_wake_downgraded_under_host_contention(store, tmp_path):
    """Athena/advisor wakes ride this verb: under load the caller gets
    ``downgraded`` and sends a plain notify; no turn runs."""
    from hermes_cli.kanban_wake_gate import WakeGate

    _human_turn(store)
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)
    runner._kanban_wake_gate = WakeGate(
        {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48}}},
        ncpu=32, loadavg=lambda: (71.0, 70.0, 60.0), lane_probe=lambda p: None,
        state_file=tmp_path / "wake_gate.json",
    )
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert result["delivered"] is False
    assert result["downgraded"] == "host load 71"
    assert adapter.handled == []


def _lane_gate(tmp_path, capped_profile):
    from hermes_cli.kanban_wake_gate import WakeGate

    probed = []

    def probe(profile):
        probed.append(profile)
        return f"lane prov-{profile} capped" if profile == capped_profile else None

    gate = WakeGate(
        {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48}}},
        ncpu=32, loadavg=lambda: (1.0, 1.0, 1.0), lane_probe=probe,
        state_file=tmp_path / "wake_gate.json",
    )
    return gate, probed


def test_omitted_profile_is_gated_as_the_gateways_active_profile(store, tmp_path):
    """Prism #1679 P1 86be5c93cd27: with no ``profile`` the verb probed the
    literal ``default`` lane. A gateway started with ``-p coder`` serves
    coder, so coder's lane is the one that runs the turn."""
    _human_turn(store)
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)
    runner._active_profile_name = lambda: "coder"
    runner._kanban_wake_gate, probed = _lane_gate(tmp_path, "coder")
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert probed == ["coder"], probed
    assert result["downgraded"] == "lane prov-coder capped", result
    assert adapter.handled == []


def test_capped_default_lane_does_not_downgrade_a_coder_gateway(store, tmp_path):
    _human_turn(store)
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)
    runner._active_profile_name = lambda: "coder"
    runner._kanban_wake_gate, probed = _lane_gate(tmp_path, "default")
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert "default" not in probed, probed
    assert not result.get("downgraded"), result


def test_omitted_profile_is_gated_as_the_routed_profile(store, tmp_path):
    """``profile_routes`` can send this chat to another profile; the gate
    probes the same profile ``_build_wake_source`` resolves."""
    _human_turn(store)
    adapter = RecordingAdapter()
    runner = _runner(store, adapter)
    runner._active_profile_name = lambda: "default"
    runner._profile_name_for_source = lambda source: "routed"
    runner._kanban_wake_gate, probed = _lane_gate(tmp_path, "routed")
    result = asyncio.run(runner._deliver_control_wake(_params()))
    assert probed == ["routed"], probed
    assert result["downgraded"] == "lane prov-routed capped", result
