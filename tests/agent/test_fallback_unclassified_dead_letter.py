"""Dead-letter sentinel for unclassified fallback riders (t_a716610d).

An announce rendered from a floor branch (``unclassified error``,
``(hop unknown, sub unknown)``, the generic ``connection issue`` head) appends
ONE JSON line with the raw evidence to ``<home>/state/fallback-unclassified.jsonl``.
A classified announce writes nothing. The rendered text never changes, and a
sentinel failure never breaks the failover.
"""

import json
import types

import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.error_classifier import FailoverReason
from tests.agent.test_fallback_events_ledger import (  # noqa: F401
    _Err,
    _fail_over,
    _home,
    _rows,
)
from tests.context_engine.test_lcm_redaction_corpus import _secret_corpus


def _dead(home):
    p = home / "state" / "fallback-unclassified.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


_HEADERS = {
    "X-Relay-Error-Hop": "relay",
    "Retry-After": "7",
    "anthropic-ratelimit-unified-status": "allowed",
    "x-request-id": "req_should_not_be_kept",
    "content-type": "application/json",
}


def _novel_500():
    msg = "upstream said something nobody has a needle for yet"
    return _Err(msg, 500, headers=dict(_HEADERS),
                body={"error": {"message": msg, "type": "weird_new_shape"}})


def test_floor_rider_writes_exactly_one_row(_home, monkeypatch):
    _fail_over(monkeypatch, _novel_500(), reason=FailoverReason.server_error)
    dead = _dead(_home)
    assert len(dead) == 1
    d = dead[0]
    assert fp.FLOOR_CAUSE in d["floors"]
    assert d["http_status"] == 500 and d["exc_name"] == "_Err"
    assert d["provider"] == "claude-bpr" and d["model"] == "claude-fable-5-1"
    assert d["session"] == "20260925_120000_abcd"
    # Only the relay / retry-after / anthropic-ratelimit headers, lowercased.
    assert set(d["headers"]) == {"x-relay-error-hop", "retry-after",
                                 "anthropic-ratelimit-unified-status"}
    assert "weird_new_shape" in d["body"] and len(d["body"]) <= fbe.DEAD_LETTER_BODY_MAX
    # The rendered string is the notice the ledger recorded, unchanged.
    assert "unclassified error" in d["rendered"]
    assert d["rendered"] == _rows(_home)[0]["notice_text"]


def test_classified_rider_writes_nothing(_home, monkeypatch):
    msg = "connection reset by peer"
    err = _Err(msg, 502, headers={"x-relay-error-class": "conn", "x-relay-error-hop": "relay",
                                  "x-relay-seat": "sub-vps-3"},
               body={"error": {"message": msg}})
    _fail_over(monkeypatch, err, reason=FailoverReason.server_error)
    assert _rows(_home)  # the failover happened and was ledgered
    assert _dead(_home) == []


def test_generic_head_floor_is_filed_and_text_unchanged(_home, monkeypatch):
    msg = "connection reset by peer"
    err = _Err(msg, 502, headers={"x-relay-error-hop": "relay", "x-relay-seat": "sub-vps-3"},
               body={"error": {"message": msg}})
    a = _fail_over(monkeypatch, err, reason=FailoverReason.unknown)
    dead = _dead(_home)
    assert len(dead) == 1 and dead[0]["floors"] == [fp.FLOOR_HEAD]
    assert "(connection issue)" in dead[0]["rendered"]
    assert dead[0]["rendered"] == _rows(_home)[0]["notice_text"]


def test_body_scrub_drops_leak_corpus(_home, monkeypatch):
    corpus = _secret_corpus()
    # Short enough that every secret sits inside the 400-char window.
    for name, secret in corpus.items():
        body_msg = f"novel failure {name}: {secret}"
        err = _Err(body_msg, 500, body={"error": {"message": body_msg}})
        _fail_over(monkeypatch, err, reason=FailoverReason.server_error)
    dead = _dead(_home)
    assert len(dead) == len(corpus)
    # Decoded values, not the JSON text: escaping (the key block's newlines)
    # would hide a leak from a substring check on json.dumps output.
    blob = "\n".join(d["body"] + d["rendered"] + "".join(d["headers"].values())
                     for d in dead)
    for name, secret in corpus.items():
        assert secret not in blob, name
        assert f"novel failure {name}: " in blob, name  # the row is there, scrubbed


def _wire_err(wire_text, status=500):
    err = _Err("novel wire failure", status)
    err.response = types.SimpleNamespace(headers={}, text=wire_text)
    err.body = None
    return err


def test_escaped_json_url_credentials_scrubbed(_home, monkeypatch):
    """Prism r1: the wire spelling escapes ``/``; scrub the decoded value."""
    pw = "opaque-pw-" + "4242xyz"
    wire = '{"error": {"message": "bad redirect https:\\/\\/alice:' + pw + '@host.test\\/v1"}}'
    assert "\\/" in wire and pw in wire
    _fail_over(monkeypatch, _wire_err(wire), reason=FailoverReason.server_error)
    dead = _dead(_home)
    assert len(dead) == 1
    assert "host.test" in dead[0]["body"] and pw not in dead[0]["body"]


def test_top_level_json_string_is_decoded_before_scrub(_home, monkeypatch):
    """Prism r2: a body that is one JSON string is decoded too."""
    pw = "opaque-pw-" + "9191abc"
    wire = '"redirect to https:\\/\\/alice:' + pw + '@host.test\\/v1"'
    _fail_over(monkeypatch, _wire_err(wire), reason=FailoverReason.server_error)
    dead = _dead(_home)
    assert len(dead) == 1
    assert "host.test" in dead[0]["body"] and pw not in dead[0]["body"]


def test_key_block_longer_than_memory_cap_is_not_persisted(_home, monkeypatch):
    """Prism r1: a key block whose END lies past the kept window must not
    leave its opening material in the persisted 400 chars."""
    begin = "-" * 5 + "BEGIN RSA " + "PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END RSA " + "PRIVATE KEY" + "-" * 5
    material = "MIIJKAIBAAKCAgEA" + "q" * (fbe._DL_RAW_MAX + 5000)
    wire = "novel html-ish failure\n" + begin + "\n" + material + "\n" + end
    _fail_over(monkeypatch, _wire_err(wire), reason=FailoverReason.server_error)
    dead = _dead(_home)
    assert len(dead) == 1
    assert "novel html-ish failure" in dead[0]["body"]
    assert "MIIJKAIBAAKCAgEA" not in dead[0]["body"] and "qqqq" not in dead[0]["body"]


def test_sentinel_failure_never_breaks_failover(_home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sentinel exploded")

    monkeypatch.setattr(fbe, "note_unclassified", boom)
    a = _fail_over(monkeypatch, _novel_500(), reason=FailoverReason.server_error)
    assert len(_rows(_home)) == 1
    assert "unclassified error" in _rows(_home)[0]["notice_text"]


def test_unwritable_ledger_returns_false(tmp_path):
    target = tmp_path / "is-a-dir"
    target.mkdir()
    assert fbe.note_unclassified({}, "x", ["unclassified_cause"], path=target) is False


@pytest.mark.parametrize("row,floors", [
    ({"trigger_class": "unclassified", "from_provider": "claude-bpr", "http_status": 529},
     (fp.FLOOR_CAUSE, fp.FLOOR_HOP_SUB)),
    ({"trigger_class": "conn", "err_head": "connection reset", "from_provider": "claude-bpr",
      "http_status": 500, "hop": "relay", "seat": "sub-vps-3"}, ()),
    ({"trigger_class": "pool_pressure", "err_head": "draining-for-deploy",
      "from_provider": "claude-bpr", "http_status": 503}, ()),
    ({"trigger_class": "unclassified", "from_provider": "openrouter", "http_status": 500}, (fp.FLOOR_CAUSE,)),
])
def test_floors_match_rendered_text(row, floors):
    text, got = fp.cause_rider_with_floors(dict(row, ts=0))
    assert got == floors
    assert text == fp.format_cause_rider(dict(row, ts=0))
    assert (fp.UNCLASSIFIED_CAUSE in text) == (fp.FLOOR_CAUSE in got)
    assert ("(hop unknown, sub unknown)" in text) == (fp.FLOOR_HOP_SUB in got)
