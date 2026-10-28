"""The Discord standalone REST send honours HTTP 429 (cron runner Q5, t_8c1539e2).

The fake transport replaces ``aiohttp.ClientSession``; a 429 response means Discord did not
create the message, so every 429 must be retried (bounded) and never counted as delivered.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.platforms.discord import standalone_rate_limit as rl
from plugins.platforms.discord.adapter import _remember_channel_is_forum, _standalone_send


class _Content:
    def __init__(self, body: bytes):
        self._chunks = [body]

    async def read(self, _n=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self._body = json.dumps(body if body is not None else {}).encode()
        self.content = _Content(self._body)

    def fresh(self):
        """A copy with an unread body (the scripted session repeats its last response)."""
        clone = _Resp.__new__(_Resp)
        clone.status, clone.headers, clone._body = self.status, self.headers, self._body
        clone.content = _Content(self._body)
        return clone

    def get_encoding(self):
        return "utf-8"


def _ok(msg_id="m1", headers=None):
    return _Resp(200, {"id": msg_id}, headers)


def _r429(retry_after, *, is_global=False, body=True, headers=None):
    payload = {"message": "You are being rate limited.", "retry_after": retry_after, "global": is_global}
    return _Resp(429, payload if body else {"message": "rate limited"}, headers)


class _Ctx:
    def __init__(self, value, on_exit=None):
        self._value, self._on_exit = value, on_exit

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        if self._on_exit:
            self._on_exit()
        return False


class _ScriptedSession:
    """Returns the scripted responses in order (last one repeats), recording each POST url."""

    def __init__(self, responses):
        self.responses, self.posts = list(responses), []

    def __call__(self, *a, **kw):
        return _Ctx(self)

    def post(self, url, **kwargs):
        self.posts.append(url)
        idx = min(len(self.posts) - 1, len(self.responses) - 1)
        return _Ctx(self.responses[idx].fresh())


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(rl, "_CHANNEL_LOCKS", {})
    monkeypatch.setattr(rl, "_CHANNEL_RESUME_AT", {})
    monkeypatch.setattr(rl, "_GLOBAL_RESUME_AT", [0.0])


@pytest.fixture
def fake_clock(monkeypatch):
    """Virtual time: ``_sleep`` records the delay and advances ``_monotonic`` instead of sleeping."""
    clock = SimpleNamespace(now=1000.0, sleeps=[])

    async def _sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(rl, "_sleep", _sleep)
    monkeypatch.setattr(rl, "_monotonic", lambda: clock.now)
    return clock


def _send(chat_id, session, text="hello"):
    _remember_channel_is_forum(chat_id, False)
    with patch("aiohttp.ClientSession", session):
        return asyncio.run(_standalone_send(SimpleNamespace(token="bot-token", extra={}), chat_id, text))


def test_429_twice_then_200_is_delivered_after_the_server_delay(fake_clock):
    session = _ScriptedSession([_r429(0.2), _r429(0.2), _ok("m42")])
    res = _send("700001", session)
    assert res["success"] is True and res["message_id"] == "m42"
    assert len(session.posts) == 3
    assert sum(fake_clock.sleeps) == pytest.approx(0.4)


def test_retry_after_header_is_used_when_the_body_has_none(fake_clock):
    session = _ScriptedSession([_r429(None, body=False, headers={"Retry-After": "1.5"}), _ok()])
    assert _send("700002", session)["success"] is True
    assert fake_clock.sleeps == [pytest.approx(1.5)]


def test_429_forever_returns_the_error_after_the_retry_cap(fake_clock):
    session = _ScriptedSession([_r429(0.2)])
    res = _send("700003", session)
    assert res.get("success") is not True
    assert "(429)" in res["error"]
    assert len(session.posts) == rl.MAX_429_RETRIES + 1
    assert sum(fake_clock.sleeps) <= rl.MAX_WAIT_S


def test_429_forever_with_long_delays_stops_at_the_wait_cap(fake_clock):
    session = _ScriptedSession([_r429(10.0)])
    res = _send("700004", session)
    assert "(429)" in res["error"]
    assert sum(fake_clock.sleeps) <= rl.MAX_WAIT_S
    assert len(session.posts) == 4  # waits 10+10+10; a 4th wait would pass 30 s


def test_a_delay_longer_than_the_cap_is_not_slept(fake_clock):
    session = _ScriptedSession([_r429(45.0), _ok()])
    res = _send("700005", session)
    assert "(429)" in res["error"]
    assert fake_clock.sleeps == [] and len(session.posts) == 1


@pytest.mark.parametrize("marker", ["body", "header"])
def test_global_429_pauses_sends_to_other_channels(fake_clock, monkeypatch, marker):
    if marker == "body":
        first = _r429(0.5, is_global=True)
    else:
        first = _r429(None, body=False, headers={"Retry-After": "0.5", "X-RateLimit-Global": "true"})
    monkeypatch.setattr(rl, "MAX_429_RETRIES", 0)  # A gives up at once; only the global pause remains
    assert "(429)" in _send("700006", _ScriptedSession([first]))["error"]
    monkeypatch.setattr(rl, "MAX_429_RETRIES", 5)
    other = _ScriptedSession([_ok()])
    assert _send("700007", other)["success"] is True
    assert fake_clock.sleeps == [pytest.approx(0.5)]
    assert len(other.posts) == 1


def test_non_global_429_does_not_pause_other_channels(fake_clock, monkeypatch):
    monkeypatch.setattr(rl, "MAX_429_RETRIES", 0)
    _send("700008", _ScriptedSession([_r429(0.5)]))
    assert _send("700009", _ScriptedSession([_ok()]))["success"] is True
    assert fake_clock.sleeps == []


def test_exhausted_bucket_headers_delay_the_next_send_on_that_channel(fake_clock):
    session = _ScriptedSession([_ok("a", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.7"}), _ok("b")])
    assert _send("700010", session)["success"] is True
    assert _send("700010", session)["success"] is True
    assert fake_clock.sleeps == [pytest.approx(0.7)]


class _BucketServer:
    """One channel, ``capacity`` POSTs per ``window`` seconds (real time), Discord-shaped 429s.

    Tracks how many POSTs are in flight at once and which sends Discord created."""

    def __init__(self, capacity=5, window=0.25, latency=0.01):
        self.capacity, self.window, self.latency = capacity, window, latency
        self.lock = threading.Lock()
        self.window_start, self.used = time.monotonic(), 0
        self.in_flight = self.max_in_flight = 0
        self.created, self.http_429 = [], 0

    def __call__(self, *a, **kw):
        return _Ctx(self)

    def post(self, url, **kwargs):
        server = self

        class _Post:
            async def __aenter__(self):
                with server.lock:
                    server.in_flight += 1
                    server.max_in_flight = max(server.max_in_flight, server.in_flight)
                await asyncio.sleep(server.latency)
                with server.lock:
                    now = time.monotonic()
                    if now - server.window_start >= server.window:
                        server.window_start, server.used = now, 0
                    reset_after = round(server.window - (now - server.window_start), 3)
                    if server.used >= server.capacity:
                        server.http_429 += 1
                        return _r429(reset_after, headers={"Retry-After": "1"})
                    server.used += 1
                    server.created.append(kwargs["json"]["content"])
                    remaining = server.capacity - server.used
                    return _ok(str(len(server.created)), {
                        "X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset-After": str(reset_after),
                    })

            async def __aexit__(self, *exc):
                with server.lock:
                    server.in_flight -= 1
                return False

        return _Post()


def test_26_concurrent_sends_to_one_channel_queue_and_all_land():
    """The P0-4 shape: 26 standalone sends to one channel at once, one thread + asyncio.run each."""
    server = _BucketServer()
    chat_id = "700026"
    _remember_channel_is_forum(chat_id, False)
    results = [None] * 26

    def _one(i):
        results[i] = asyncio.run(_standalone_send(SimpleNamespace(token="t", extra={}), chat_id, f"msg {i}"))

    with patch("aiohttp.ClientSession", server):
        threads = [threading.Thread(target=_one, args=(i,)) for i in range(26)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
    lost = [r for r in results if not (r and r.get("success"))]
    assert lost == []
    assert sorted(server.created) == sorted(f"msg {i}" for i in range(26))  # no loss, no duplicate
    assert server.max_in_flight == 1
