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
    dead = _dead(_home)
    assert len(dead) == 1
    d = dead[0]
    assert d["cause"] == "invalid_response"
    assert d["floor"] == {
        "site": "invalid_response",
        "detail": "response.content invalid (not a non-empty list)",
        "stop_reason": "tool_use", "content_blocks": 0, "output_tokens": 706,
        "route_id": "5b9217ed061747d29e79488f7b7f30c8", "served_by": "sub-vps-20",
    }
    assert d["elapsed_s"] == 16.4
    assert _rows(_home)[0]["floor_site"] == "invalid_response"
    # Inert on routing: the row's class and the rendered rider are unchanged.
    assert d["trigger_class"] == "unclassified" and d["http_status"] is None
    assert "unclassified error" in d["rendered"]


def test_floor_stash_does_not_change_the_rider(_home, monkeypatch):
    """Same failover with and without the floor stash renders the same text."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _agent, _patch_resolver

    _patch_resolver(monkeypatch)
    a = _agent()
    assert try_activate_fallback(a) is True
    bare = _rows(_home)[-1]["notice_text"]
    b = _agent()
    fbe.stash_response_failure(b, "empty_response", _Resp())
    assert try_activate_fallback(b) is True
    with_floor = _rows(_home)[-1]["notice_text"]
    strip = lambda t: t.rsplit(",", 1)[0]  # drop the HH:MM:SS window
    assert strip(bare) == strip(with_floor)


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
    assert try_activate_fallback(a) is True
    d = _dead(_home)[0]
    assert pw not in d["floor"]["detail"]
    assert pw not in str(d)


def test_endpoint_carries_no_userinfo_or_query():
    """Prism r1: the endpoint is host:port only, never URL userinfo/query."""
    import httpx

    pw = "opaque-pw-" + "9191abc"
    req = httpx.Request("POST", f"https://alice:{pw}@relay.test:18801/v1/messages?key={pw}")
    err = httpx.ConnectError("refused", request=req)
    assert fbe._endpoint(err) == "relay.test:18801"
