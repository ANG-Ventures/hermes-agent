"""Dead-letter cause capture for no-status / rejected-response failovers (t_b2e9ef12).

2026-10-01 11:25 PT: an alr fable->opus failover filed a dead-letter row with
http_status/exc_name/reason all null and body ''. The relay had answered 200
(route 5b9217ed, billed); the loop rejected the response at a reason-less
floor site, and nothing was stashed. These tests pin:

* a socket failure (dead port, hung server, reset, DNS) files exc_name AND a
  named ``socket_cause`` plus endpoint/elapsed, never nulls;
* a billed response the loop rejects files the floor ``site`` it died at;
* every dead-letter row carries a non-``unclassified`` ``cause``;
* the instrumentation is inert on the class/rider (same text as before).
"""

import socket
import threading

import anthropic
import pytest

from agent import fallback_events as fbe
from agent.error_classifier import FailoverReason
from tests.agent.test_fallback_events_ledger import (  # noqa: F401
    _fail_over,
    _home,
    _rows,
)
from tests.agent.test_fallback_unclassified_dead_letter import _dead


def _call(base_url, timeout=2.0):
    c = anthropic.Anthropic(api_key="test-key", base_url=base_url,
                            max_retries=0, timeout=timeout)
    try:
        c.messages.create(model="m", max_tokens=1,
                          messages=[{"role": "user", "content": "hi"}])
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError("call unexpectedly succeeded")


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _server(behaviour):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen()
    held = []

    def run():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            if behaviour == "hang":
                held.append(conn)  # never answers
            else:  # "reset": read the request, then hard-close
                try:
                    conn.recv(65536)
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    b"\x01\x00\x00\x00\x00\x00\x00\x00")
                finally:
                    conn.close()

    threading.Thread(target=run, daemon=True).start()
    return s


def _failover_on(monkeypatch, err, elapsed=1.25):
    """Stash ``err`` the way the loop's except-branch does, then fail over."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _agent, _patch_resolver

    _patch_resolver(monkeypatch)
    a = _agent()
    fbe.stash_api_error(a, err, getattr(err, "status_code", None),
                        {"message": str(err)}, elapsed_s=elapsed)
    assert try_activate_fallback(a, reason=None) is True
    return a


@pytest.mark.parametrize("make,want_cause,want_exc", [
    (lambda: (f"http://127.0.0.1:{_dead_port()}", None), "connect_refused",
     "APIConnectionError"),
    (lambda: (None, "hang"), "read_timeout", "APITimeoutError"),
    (lambda: (None, "reset"), None, "APIConnectionError"),
])
def test_real_socket_failure_files_exc_and_cause(_home, monkeypatch, make, want_cause,
                                                 want_exc):
    url, behaviour = make()
    srv = None
    if behaviour:
        srv = _server(behaviour)
        url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    try:
        err = _call(url, timeout=1.0)
    finally:
        if srv is not None:
            srv.close()
    a = _failover_on(monkeypatch, err)
    rows = _rows(_home)
    assert len(rows) == 1
    r = rows[0]
    # The fallback_events row names the exception AND the socket cause.
    assert r["exc_name"] == want_exc
    # A peer reset surfaces as conn_reset / remote_protocol / read_error
    # depending on where in the HTTP exchange the RST lands.
    if want_cause is None:
        assert r["socket_cause"] in ("conn_reset", "remote_protocol", "read_error")
    else:
        assert r["socket_cause"] == want_cause
    assert r["trigger_class"] == "conn" and r["floor_site"] is None
    # Classified as conn: no floor rendered, so no dead-letter row (inert).
    assert _dead(_home) == []


def test_unrecognised_no_status_exception_dead_letters_with_cause(_home, monkeypatch):
    """A wrapper the text table and exc-name list do not know, with an empty
    message, floors to "unclassified": its dead-letter row must still carry
    exc_name + socket_cause + endpoint + elapsed, not nulls."""
    class RelayClientGlitch(Exception):
        pass
    srv = _server("hang")
    try:
        inner = _call(f"http://127.0.0.1:{srv.getsockname()[1]}", timeout=1.0)
    finally:
        srv.close()
    try:
        raise RelayClientGlitch("") from inner
    except RelayClientGlitch as e:
        err = e
    err.request = inner.request
    _failover_on(monkeypatch, err)
    dead = _dead(_home)
    assert len(dead) == 1
    d = dead[0]
    assert d["exc_name"] == "RelayClientGlitch"
    assert d["socket_cause"] == "read_timeout" and d["cause"] == "read_timeout"
    assert d["endpoint"].startswith("127.0.0.1:")
    assert d["elapsed_s"] == 1.25
    assert d["exc_chain"][:2] == ["RelayClientGlitch", "APITimeoutError"]
    assert _rows(_home)[0]["socket_cause"] == "read_timeout"


def test_dns_failure_is_named_dns():
    class _GaiChain(Exception):
        pass
    try:
        try:
            raise socket.gaierror(8, "nodename nor servname provided")
        except socket.gaierror as inner:
            raise _GaiChain("Connection error.") from inner
    except _GaiChain as e:
        assert fbe.socket_cause(e) == "dns"


def test_connect_timeout_beats_inner_timeouterror():
    import httpx

    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            raise httpx.ConnectTimeout("timed out") from inner
    except httpx.ConnectTimeout as e:
        assert fbe.socket_cause(e) == "connect_timeout"


def test_http_status_error_has_no_socket_cause():
    class _Status(Exception):
        status_code = 500
    assert fbe.socket_cause(_Status("boom")) is None
    assert fbe.exc_chain(None) == []


def test_cycle_in_exc_chain_is_bounded():
    a, b = Exception("a"), Exception("b")
    a.__cause__, b.__cause__ = b, a
    assert fbe.exc_chain(a) == ["Exception", "Exception"]


class _Usage:
    output_tokens = 706


class _Resp:
    """A billed relay 200 the anthropic transport rejects (empty content
    without a terminal stop_reason): the 11:25 shape."""
    content = []
    stop_reason = "tool_use"
    usage = _Usage()
    pool_headers = {"x-pool-route-id": "5b9217ed061747d29e79488f7b7f30c8",
                    "x-pool-served-by": "sub-vps-20"}


def test_rejected_billed_response_files_floor_site(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _agent, _patch_resolver

    _patch_resolver(monkeypatch)
    a = _agent()
    fbe.stash_response_failure(a, "invalid_response", _Resp(),
                               detail="response.content invalid (not a non-empty list)",
                               elapsed_s=16.4)
    assert try_activate_fallback(a) is True
    # t_d35beb85: the floor NAMES the class now, so the announce renders no
    # floor branch and nothing reaches the dead-letter ledger.
    assert _dead(_home) == []
    r = _rows(_home)[0]
    assert r["floor_site"] == "invalid_response"
    assert r["trigger_class"] == "provider_invalid_response" and r["class_source"] == "floor"
    assert r["seat"] == "sub-vps-20"
    assert r["route_id"] == "5b9217ed061747d29e79488f7b7f30c8"
    assert r["err_hash"] == fbe.floor_err_hash(
        {"site": "invalid_response", "stop_reason": "tool_use", "content_blocks": 0})


def test_legacy_null_row_still_gets_a_cause():
    assert fbe.dead_letter_cause({"http_status": None, "exc_name": None}) == "no_evidence"
    assert fbe.dead_letter_cause({"http_status": 500, "trigger_class": "unclassified"}) == "http_500"
    assert fbe.dead_letter_cause({"headers": {"x-relay-error-class": "weird"}}) == "relay_weird"


def test_stash_response_failure_never_raises():
    fbe.stash_response_failure(object(), "invalid_response", object())  # no attrs
    fbe.stash_response_failure(None, "x")


def test_floor_detail_scrubbed_before_cut(_home, monkeypatch):
    """Prism r1: a URL password whose '@' falls past the 200-char cut must not
    survive into the dead-letter row."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _agent, _patch_resolver

    pw = "opaque-pw-" + "4242xyz"
    prefix = "x" * (200 - len("https://alice:") - len(pw))
    detail = f"{prefix}https://alice:{pw}@host.test/v1 failed"
    assert detail.index("@") >= 200  # the cut lands before the '@'
    _patch_resolver(monkeypatch)
    a = _agent()
    fbe.stash_response_failure(a, "invalid_response", _Resp(), detail=detail)
    floor = a._pending_fallback_error["floor"]
    assert pw not in floor["detail"]
    assert try_activate_fallback(a) is True
    assert pw not in str(_rows(_home)[0])


def test_endpoint_carries_no_userinfo_or_query():
    """Prism r1: the endpoint is host:port only, never URL userinfo/query."""
    import httpx

    pw = "opaque-pw-" + "9191abc"
    req = httpx.Request("POST", f"https://alice:{pw}@relay.test:18801/v1/messages?key={pw}")
    err = httpx.ConnectError("refused", request=req)
    assert fbe._endpoint(err) == "relay.test:18801"


# ── t_d35beb85: the empty tool_use 200 names its cause, hop and seat ─────

_LIVE_ROWS = [
    # (stamp, session tail, output_tokens, served_by, route_id) — the three
    # 2026-10-01/02 claude-alr rows in state/fallback-unclassified.jsonl.
    ("10-01 13:50", "1d72a62e", 302, "sub-vps-20", "5b9217ed061747d29e79488f7b7f30c8"),
    ("10-01 23:11", "52aefa", 377, "sub-vps-1", None),
    ("10-02 08:33", "c4d68f1b", 462, "sub-vps-2", "9ed7daf6460344d4b0e3f9f0bbcd2888"),
]


def _live_resp(out, served_by, route_id):
    usage = type("U", (), {"output_tokens": out})()
    ph = {"x-pool-served-by": served_by}
    if route_id:
        ph["x-pool-route-id"] = route_id
    return type("R", (), {"content": [], "stop_reason": "tool_use", "usage": usage,
                          "pool_headers": ph})()


def _alr_agent():
    import agent.auxiliary_client as ac
    from tests.agent.test_route_change_sink_e2e import _fake_agent

    ac.set_runtime_main("claude-alr", "claude-fable-5-1",
                        base_url="http://127.0.0.1:18801/anthropic",
                        api_key="primary-key", api_mode="anthropic_messages")
    a = _fake_agent(model="claude-fable-5-1", provider="claude-alr",
                    base_url="http://127.0.0.1:18801/anthropic",
                    api_mode="anthropic_messages")
    a._fallback_chain = [{"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    a.fallback_model = list(a._fallback_chain)
    a.session_id = "20260929_141542_c4d68f1b"
    a._current_turn_id = f"{a.session_id}:{a.session_id}:t1"
    return a


@pytest.mark.parametrize("stamp,sess,out,seat,route", _LIVE_ROWS)
def test_live_empty_tool_use_row_renders_named_rider(_home, monkeypatch, stamp, sess, out,
                                                     seat, route):
    """Replay of the live rows through the real failover + renderer: the
    exact cause/hop/sub text, never "unclassified" / "hop unknown"."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    fbe.stash_response_failure(a, "invalid_response", _live_resp(out, seat, route),
                               detail="response.content invalid (not a non-empty list)",
                               elapsed_s=7.19)
    assert try_activate_fallback(a) is True
    r = _rows(_home)[0]
    # t_6eddafcd: the chat line says "empty reply"; the raw stop_reason/blocks/out
    # stays in the route-changes log (record_invalid_response) and the ledger.
    want = f"empty reply · hop=relay-200 · sub={seat}"
    rider = r["notice_text"].split(" — ", 1)[1].rsplit(",", 1)[0]
    assert rider == want, r["notice_text"]
    assert "claude-alr/claude-fable-5-1 → claude-btpr/claude-fable-5-1" in r["notice_text"]
    for banned in ("unclassified", "hop unknown", "sub unknown"):
        assert banned not in r["notice_text"]
    assert r["trigger_class"] == "provider_invalid_response"
    assert _dead(_home) == []  # named class: not a sentinel miss


def test_rider_never_hop_sub_unknown_when_served_by_present():
    """Negative: with floor.served_by present the (hop unknown, sub unknown)
    floor never renders, whatever else is missing."""
    from agent import fallback_policy as fp

    for prov in ("claude-alr", "claude-apr", "custom:claude-alr", "something-else"):
        row = {"trigger_class": "provider_invalid_response", "from_provider": prov,
               "floor": {"site": "invalid_response", "served_by": "sub-vps-2"},
               "ts": 1790959999.0}
        text, floors = fp.cause_rider_with_floors(row)
        assert "hop unknown" not in text and "sub unknown" not in text, text
        assert "sub=sub-vps-2" in text and floors == ()


def test_floor_err_hash_keys_on_shape_not_tokens():
    a = {"site": "invalid_response", "stop_reason": "tool_use", "content_blocks": 0,
         "output_tokens": 302, "served_by": "sub-vps-20"}
    b = dict(a, output_tokens=462, served_by="sub-vps-2", route_id="x")
    assert fbe.floor_err_hash(a) == fbe.floor_err_hash(b)
    assert fbe.floor_err_hash(a) != fbe.floor_err_hash(dict(a, stop_reason="end_turn"))
    assert fbe.floor_err_hash(a) != fbe.floor_err_hash(dict(a, content_blocks=None))
    assert fbe.floor_err_hash({}) is None


def test_text_or_status_evidence_still_wins_over_floor():
    """The floor only names a call that had no http/exc/text class."""
    floor = {"site": "invalid_response"}
    assert fbe.classify_trigger(text="rate limit", floor=floor)[0] == "rate_upstream"
    assert fbe.classify_trigger(http_status=500, floor=floor)[0] == "unclassified"
    assert fbe.classify_trigger(floor=floor) == ("provider_invalid_response", "floor")
    assert fbe.classify_trigger() == ("unclassified", "text")


def test_empty_tool_use_floor_shape():
    R = lambda **k: type("R", (), k)()
    assert fbe.empty_tool_use_floor(R(stop_reason="tool_use", content=[]))
    assert not fbe.empty_tool_use_floor(R(stop_reason="tool_use", content=None))
    assert not fbe.empty_tool_use_floor(R(stop_reason="end_turn", content=[]))
    assert not fbe.empty_tool_use_floor(None)
    assert not fbe.empty_tool_use_floor(R(choices=[]))  # chat-completions shapes keep eager fallback


def test_invalid_response_count_row(_home):
    a = _alr_agent()
    resp = _live_resp(462, "sub-vps-2", "9ed7daf6460344d4b0e3f9f0bbcd2888")
    fbe.stash_response_failure(a, "invalid_response", resp)
    floor = a._pending_fallback_error["floor"]
    ok = type("R", (), {"usage": type("U", (), {"cache_read_input_tokens": 231000})()})()
    assert fbe.record_invalid_response(a, floor, "retry_ok", response=ok)
    line = (_home / "state" / "model-route-changes.log").read_text().splitlines()[-1]
    tok = dict(t.split("=", 1) for t in line.split()[2:])
    assert line.split()[1] == "invalid_response"
    assert tok["class"] == "provider_invalid_response" and tok["retry_outcome"] == "retry_ok"
    assert tok["served_by"] == "sub-vps-2" and tok["route_id"] == "9ed7daf6460344d4b0e3f9f0bbcd2888"
    assert tok["content_blocks"] == "0" and tok["output_tokens"] == "462"
    assert tok["cache_read"] == "231000" and tok["err_hash"] == fbe.floor_err_hash(floor)
    # The failover/recovery sink regex (fallback-cache-report SINK_RE) skips it.
    import re
    assert not re.match(r"^(\S+) (failover|recovery) (\S+) -> (\S+)", line)


def test_repeat_floor_skips_same_provider_entries(_home, monkeypatch):
    """retry_same: the failover skips an entry on the failing provider (a
    model swap on alr) and lands on a different transport."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a._fallback_chain = [{"provider": "claude-alr", "model": "claude-opus-5-5"},
                         {"provider": "claude-btpr", "model": "claude-fable-5-1"}]
    a.fallback_model = list(a._fallback_chain)
    from agent.chat_completion_helpers import try_activate_fallback as _taf
    a._try_activate_fallback = lambda *x, **k: _taf(a, *x, **k)
    fbe.stash_response_failure(a, "invalid_response", _live_resp(462, "sub-vps-2", None),
                               repeat=True)
    assert try_activate_fallback(a) is True
    assert (a.provider, a.model) == ("claude-btpr", "claude-fable-5-1")


def test_non_repeat_floor_keeps_same_provider_entries(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a._fallback_chain = [{"provider": "claude-alr", "model": "claude-opus-5-5"}]
    a.fallback_model = list(a._fallback_chain)
    fbe.stash_response_failure(a, "invalid_response", _live_resp(462, "sub-vps-2", None))
    assert try_activate_fallback(a) is True
    assert (a.provider, a.model) == ("claude-alr", "claude-opus-5-5")
