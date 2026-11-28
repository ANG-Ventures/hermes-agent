"""The retry banner names our hop, never "(Anthropic 503)", for a refusal our own
lane door made (t_c5cddd09).

2026-11-27 18:54:07 PT the claude-dtlr lane door (claude-pool ``lane_front.py``,
ERR_HOP = "front") answered its canned 503 before any upstream byte, while the NAS
relays were stopped for a failover test. The banner read
``⏳ retrying claude-dtlr/claude-fable-5-2 after a 1 s wait: relay unavailable
(Anthropic 503)``. Anthropic never answered.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent import fallback_cause, fallback_events

FABLE = "claude-fable-5-2"

DOOR_BODY = {"error": "local_unavailable", "relay_error_class": "pool_pressure",
             "relay_error_hop": "front", "relay_error_cause": "local_unavailable"}
DOOR_HEADERS = {"x-relay-error-hop": "front", "x-relay-error-cause": "local_unavailable",
                "Retry-After": "1", "content-type": "application/json"}


class _Response:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self._body = body

    def json(self):
        return self._body


class APIStatusError(Exception):
    """The OpenAI SDK shape: ``body`` is ``data.get("error", data)``."""

    def __init__(self, status, headers, body):
        super().__init__(f"Error code: {status} - {body}")
        self.status_code = status
        self.response = _Response(status, headers, body)
        self.body = body.get("error", body) if isinstance(body, dict) else body


def _agent(provider="claude-dtlr"):
    return SimpleNamespace(provider=provider, model=FABLE, base_url="http://127.0.0.1:18816/v1",
                           session_id="s1", _wall_budget=None, _failover_cause=None,
                           _failover_carry=None)


def _banner(agent, status, headers, body):
    fallback_events.stash_api_error(agent, APIStatusError(status, headers, body), status)
    fallback_cause.note_wait(agent, 1)
    return fallback_cause.wait_notice(agent)


def test_lane_door_503_names_the_door_not_anthropic():
    text = _banner(_agent(), 503, DOOR_HEADERS, DOOR_BODY)
    print(text)
    assert "Anthropic" not in text
    assert "lane door 503 (local_unavailable)" in text
    assert text.startswith("⏳ retrying claude-dtlr/claude-fable-5-2 after a 1 s wait: ")


def test_lane_door_503_named_from_the_body_alone():
    # A response whose headers were lost still carries the relay's body fields.
    text = _banner(_agent(), 503, {"content-type": "application/json"}, DOOR_BODY)
    print(text)
    assert "Anthropic" not in text and "lane door 503 (local_unavailable)" in text


def test_real_anthropic_status_still_names_anthropic():
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    headers = {"x-relay-error-hop": "bridge->upstream", "x-relay-seat": "sub-vps-4"}
    text = _banner(_agent(), 529, headers, body)
    print(text)
    assert "(Anthropic 529)" in text
    assert "lane door" not in text


def test_hop_read_from_full_json_when_sdk_unwraps_a_nested_error():
    """Prism #600 b6e44a030650 (t_d1de29dd): no x-relay-error-* headers, the hop and cause
    only at the top level, ``error`` an object. The SDK's ``body`` is the inner object."""
    body = {"error": {"message": "lane door has no local relay"},
            "relay_error_hop": "front", "relay_error_cause": "local_unavailable"}
    text = _banner(_agent(), 503, {"content-type": "application/json"}, body)
    print(text)
    assert "Anthropic" not in text
    assert "lane door 503 (local_unavailable)" in text


def test_relay_hop_and_cause_text_is_bounded():
    """Prism #600 d4e148ca1690 (t_d1de29dd): a multi-line 500-char cause renders one line,
    capped; the hop is capped too."""
    cause = ("bad\nline\x1b[31m" + "x" * 500)
    body = {"error": "x", "relay_error_hop": "h" * 200 + "\nzz", "relay_error_cause": cause}
    text = _banner(_agent(), 503, {"content-type": "application/json"}, body)
    print(repr(text))
    assert "\n" not in text and "\x1b" not in text and "\r" not in text
    assert len(text) < 200
    assert "Anthropic" not in text


def test_known_door_causes_render_unchanged():
    for why in ("local_unavailable", "studio-failover", "studio_unavailable",
                "upstream_unavailable"):
        headers = dict(DOOR_HEADERS, **{"x-relay-error-cause": why})
        text = _banner(_agent(), 503, headers, dict(DOOR_BODY, relay_error_cause=why))
        assert f"lane door 503 ({why})" in text, text
