"""New-model pricing sentinel — event-driven unpriced-model detection.

Card t_2e382a4b. Covers the four acceptance behaviours:

  (a) a new unpriced model → one ledger row + exactly one alert; a second turn
      on the same model → no duplicate row, no second alert;
  (b) ALERT-FAILURE INJECTION → the turn still records fully (the load-bearing
      hot-path contract: telemetry is never lost to a broken alerter);
  (c) a known/priced model → no ledger write, no alert;
  (d) is_known_model's contract, including that it makes no network call.

Every test runs against the per-test HERMES_HOME the root conftest installs, so
the real ~/.hermes/blackbox/turns.db is never touched.
"""

from __future__ import annotations

import sys
import types

import pytest

from plugins.blackbox import sentinel, store


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Collects alert dispatches and runs them SYNCHRONOUSLY.

    Substituted for sentinel._dispatch_alert so the tests are deterministic
    (no thread-join races). The real fire-and-forget threading behaviour is
    asserted separately in test_dispatch_is_fire_and_forget.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, model, provider, alert_fn):
        self.calls.append((model, provider))
        sentinel._alert_and_stamp(model, provider, alert_fn)


def _ok_alert(_model, _provider) -> bool:
    return True


def _boom_alert(_model, _provider) -> bool:
    raise RuntimeError("discord is on fire")


# An id that is deliberately absent from the pricing snapshot but still routes
# to the anthropic official-docs tier, i.e. exactly the new-model shape.
UNPRICED_MODEL = "claude-opus-99-testonly"


# ---------------------------------------------------------------------------
# (d) is_known_model contract
# ---------------------------------------------------------------------------


def test_is_known_model_true_for_priced_model():
    from agent.usage_pricing import is_known_model

    assert is_known_model("claude-opus-5", "anthropic") is True
    assert is_known_model("claude-opus-4-8", "anthropic") is True


def test_is_known_model_false_for_unpriced_model():
    from agent.usage_pricing import is_known_model

    assert is_known_model(UNPRICED_MODEL, "anthropic") is False


def test_is_known_model_true_through_notional_relay():
    """A subscription relay routes to the anthropic snapshot, so a priced model
    stays known through it — and an unknown one is still caught."""
    from agent.usage_pricing import is_known_model

    assert is_known_model("claude-opus-5", "claude-apr") is True
    assert is_known_model(UNPRICED_MODEL, "claude-apr") is False


def test_is_known_model_does_not_touch_the_network(monkeypatch):
    """The probe runs on the turn-record path; a network call there is a bug."""
    import agent.usage_pricing as up

    def _forbidden(*_a, **_k):
        raise AssertionError("is_known_model made a network call")

    monkeypatch.setattr(up, "fetch_endpoint_model_metadata", _forbidden)
    monkeypatch.setattr(up, "fetch_model_metadata", _forbidden)

    assert up.is_known_model("claude-opus-5", "anthropic") is True
    assert up.is_known_model(UNPRICED_MODEL, "anthropic") is False
    # Live-catalog routes are out of scope and must short-circuit, not fetch.
    assert up.is_known_model("some-model", "openrouter") is True


def test_is_known_model_never_raises(monkeypatch):
    import agent.usage_pricing as up

    monkeypatch.setattr(
        up, "resolve_billing_route", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    # Fails OPEN (True) — an unknowable route must not spam the alert channel.
    assert up.is_known_model("whatever", "anthropic") is True


def test_single_digit_dated_id_is_a_known_gap_the_sentinel_catches():
    """PRE-EXISTING gap, documented not fixed (card t_2e382a4b).

    ``_ANTHROPIC_DATED_SUFFIX_RE`` requires a TWO-segment version tail
    (``-4-8-20260115``), so a single-segment id like ``claude-opus-5-20260724``
    is not date-stripped and misses the snapshot. Loosening the regex would
    newly strip five existing dated entries (claude-opus-4-20250514,
    claude-sonnet-4-20250514, claude-opus-4-6/4-7-*, claude-sonnet-4-6-*), so
    it is deliberately out of scope for this branch.

    This test pins the CURRENT behaviour and, more usefully, proves the sentinel
    treats such an id as an unpriced model — i.e. the gap now announces itself
    instead of sitting silent, which is the whole point of the sentinel.
    """
    from agent.usage_pricing import (
        _strip_anthropic_release_date,
        get_pricing_entry,
        is_known_model,
    )

    assert _strip_anthropic_release_date("claude-opus-5-20260724") is None
    assert get_pricing_entry("claude-opus-5-20260724", provider="anthropic") is None
    assert is_known_model("claude-opus-5-20260724", "anthropic") is False


# ---------------------------------------------------------------------------
# (a) detection + exactly-once dedup
# ---------------------------------------------------------------------------


def test_new_unpriced_model_inserts_row_and_alerts_once():
    rec = _Recorder()

    fired = sentinel.observe_turn(
        UNPRICED_MODEL, "anthropic", "unknown", None,
        alert_fn=_ok_alert, dispatch=rec,
    )

    assert fired is True
    assert rec.calls == [(UNPRICED_MODEL, "anthropic")]

    rows = store.list_unpriced_models()
    assert len(rows) == 1
    assert rows[0]["model"] == UNPRICED_MODEL
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["first_seen"]
    assert rows[0]["alerted_at"]  # stamped on successful delivery


def test_second_turn_same_model_no_duplicate_row_no_second_alert():
    rec = _Recorder()

    first = sentinel.observe_turn(
        UNPRICED_MODEL, "anthropic", "unknown", None, alert_fn=_ok_alert, dispatch=rec
    )
    second = sentinel.observe_turn(
        UNPRICED_MODEL, "anthropic", "unknown", None, alert_fn=_ok_alert, dispatch=rec
    )
    third = sentinel.observe_turn(
        UNPRICED_MODEL, "anthropic", "unknown", None, alert_fn=_ok_alert, dispatch=rec
    )

    assert (first, second, third) == (True, False, False)
    assert len(rec.calls) == 1, "alert must fire exactly once per model"
    assert len(store.list_unpriced_models()) == 1


def test_distinct_provider_is_a_distinct_ledger_key():
    """The PK is (model, provider): the same id on a different lane is a new
    pricing gap and gets its own alert."""
    rec = _Recorder()

    sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                          alert_fn=_ok_alert, dispatch=rec)
    sentinel.observe_turn(UNPRICED_MODEL, "bedrock", "unknown", None,
                          alert_fn=_ok_alert, dispatch=rec)

    assert len(rec.calls) == 2
    assert len(store.list_unpriced_models()) == 2


def test_first_seen_and_alerted_at_are_not_overwritten():
    rec = _Recorder()
    sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                          alert_fn=_ok_alert, dispatch=rec)
    original = store.list_unpriced_models()[0]

    sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                          alert_fn=_ok_alert, dispatch=rec)

    assert store.list_unpriced_models()[0] == original


def test_alerted_at_left_null_when_delivery_reports_failure():
    """A row is still ledgered (so the gap is visible) but stays unstamped."""
    rec = _Recorder()

    fired = sentinel.observe_turn(
        UNPRICED_MODEL, "anthropic", "unknown", None,
        alert_fn=lambda *_: False, dispatch=rec,
    )

    assert fired is True
    row = store.list_unpriced_models()[0]
    assert row["alerted_at"] is None


# ---------------------------------------------------------------------------
# (c) known / priced turns are inert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,provider,status,cost",
    [
        ("claude-opus-5", "anthropic", "estimated", 1.23),   # priced
        ("claude-opus-5", "anthropic", "unknown", None),     # unpriceable but KNOWN
        (UNPRICED_MODEL, "anthropic", "estimated", 0.5),     # unknown model, but priced
        (UNPRICED_MODEL, "anthropic", "included", None),     # subscription $0
        (UNPRICED_MODEL, "anthropic", "priced_zero", 0.0),   # zero-token turn
        (UNPRICED_MODEL, "anthropic", "partial", 0.4),       # sibling-call gap, not ours
        ("", "anthropic", "unknown", None),                  # empty model id
    ],
)
def test_no_ledger_write_and_no_alert(model, provider, status, cost):
    rec = _Recorder()

    fired = sentinel.observe_turn(model, provider, status, cost,
                                  alert_fn=_ok_alert, dispatch=rec)

    assert fired is False
    assert rec.calls == []
    assert store.list_unpriced_models() == []


def test_unknown_status_with_a_real_cost_is_not_treated_as_unpriced():
    rec = _Recorder()
    assert sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", 2.50,
                                 alert_fn=_ok_alert, dispatch=rec) is False
    assert store.list_unpriced_models() == []


# ---------------------------------------------------------------------------
# (b) ALERT-FAILURE INJECTION — the hot path can never be broken
# ---------------------------------------------------------------------------


def test_observe_turn_swallows_a_raising_alert_fn():
    """A thrown alert must not escape observe_turn."""
    rec = _Recorder()  # runs _alert_and_stamp synchronously → the raise is real
    assert sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                                 alert_fn=_boom_alert, dispatch=rec) is True
    # Ledger row survives the alert explosion.
    assert len(store.list_unpriced_models()) == 1
    assert store.list_unpriced_models()[0]["alerted_at"] is None


def test_observe_turn_swallows_a_raising_dispatch():
    def _boom_dispatch(*_a, **_k):
        raise RuntimeError("thread pool exhausted")

    assert sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                                 alert_fn=_ok_alert, dispatch=_boom_dispatch) is False


def test_observe_turn_swallows_a_raising_store(monkeypatch):
    monkeypatch.setattr(
        store, "note_unpriced_model",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    assert sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                                 alert_fn=_ok_alert, dispatch=_Recorder()) is False


def test_observe_turn_swallows_a_raising_pricing_probe(monkeypatch):
    import agent.usage_pricing as up

    monkeypatch.setattr(
        up, "is_known_model",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot corrupt")),
    )
    assert sentinel.observe_turn(UNPRICED_MODEL, "anthropic", "unknown", None,
                                 alert_fn=_ok_alert, dispatch=_Recorder()) is False


def test_send_alert_never_raises_when_notify_is_missing(monkeypatch):
    monkeypatch.setattr(sentinel, "_notify_script", lambda: None)
    assert sentinel.send_alert("m", "p") is False


def test_send_alert_never_raises_when_subprocess_explodes(monkeypatch):
    monkeypatch.setattr(sentinel, "_notify_script", lambda: __import__("pathlib").Path("/x"))
    monkeypatch.setattr(
        sentinel.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no fork for you")),
    )
    assert sentinel.send_alert("m", "p") is False


def test_dispatch_is_fire_and_forget_daemon_thread():
    """The REAL dispatcher must not block the caller: a slow alert returns
    immediately and runs on a daemon thread."""
    import threading

    started = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    def _slow_alert(model, _provider):
        started.set()
        release.wait(10)
        seen.append(model)
        return False  # skip the store write; we only care about ordering

    sentinel._dispatch_alert("m", "p", _slow_alert)

    # Ordering witness (replaces `elapsed < 0.5`): the alert is still parked
    # on `release` and has recorded nothing, so the dispatcher provably did
    # not run it inline.  `seen == []` + `started.wait(10)` is strictly
    # stronger than the stopwatch and immune to machine load.
    assert started.wait(10), "alert thread never ran"
    assert seen == [], "dispatch blocked the caller until the alert finished"
    release.set()


# ---------------------------------------------------------------------------
# END-TO-END through the real record hook — telemetry survives a broken alerter
# ---------------------------------------------------------------------------


@pytest.fixture
def hook(monkeypatch):
    """The real _on_session_end wired to a fake store, as in test_hooks_alert."""
    import plugins.blackbox as bb

    bb._sessions.clear()
    monkeypatch.setattr(bb, "_profile_name", lambda: "TestAgent")
    monkeypatch.setattr(bb, "_turn_id", lambda: "turn_sentinel_test")
    monkeypatch.setattr(bb, "_config", lambda: {"enabled": True, "cost_alert_threshold_usd": 1.0})
    monkeypatch.setattr(
        bb, "compute_turn_cost",
        lambda *a, **k: (None, "unknown",
                         {"uncached": None, "cache_read": None,
                          "cache_write": None, "output": None}),
    )

    fake_store = types.SimpleNamespace(
        records=[],
        insert_turn=lambda record: fake_store.records.append(record),
        mark_alerted=lambda turn_id: True,
        sweep=lambda retention_days, **kw: 0,
        # The sentinel does its OWN lazy `from plugins.blackbox import store`,
        # which resolves to this fake too — so delegate the ledger calls to the
        # real store (writing into the per-test HERMES_HOME temp DB) rather than
        # letting them AttributeError. Only `turns` is faked here.
        note_unpriced_model=store.note_unpriced_model,
        mark_unpriced_alerted=store.mark_unpriced_alerted,
        list_unpriced_models=store.list_unpriced_models,
    )
    monkeypatch.setitem(sys.modules, "plugins.blackbox.store", fake_store)
    monkeypatch.setattr(bb, "store", fake_store, raising=False)
    monkeypatch.setattr(bb.routing, "send_card", lambda *args: None)
    return bb, fake_store


def _usage():
    return {
        "api_calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "context_used": 10,
        "context_length": 100,
        "calls": [{"input_tokens": 10, "output_tokens": 2}],
        "chat_id": "C1",
        "chat_name": "alerts",
    }


def test_turn_records_fully_even_when_the_sentinel_explodes(hook, monkeypatch):
    """THE contract: an alerter failure must never cost us telemetry.

    Also proves the sentinel's own guard earns its keep: the turn is stored
    BEFORE the sentinel runs, so the record always survives — but a raise that
    escaped the sentinel would be caught by _on_session_end's outer handler and
    would silently ABORT everything downstream (retention sweep, spending
    alert). So we assert the downstream work still happened too.
    """
    bb, fake_store = hook

    monkeypatch.setattr(
        sentinel, "observe_turn",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sentinel is broken")),
    )
    swept: list[int] = []
    carded: list[tuple] = []
    monkeypatch.setattr(fake_store, "sweep", lambda days, **kw: swept.append(days) or 0)
    monkeypatch.setattr(bb.routing, "send_card", lambda *a: carded.append(a))
    # always_card so the alert path is reachable for an unpriced (cost=None) turn
    monkeypatch.setattr(
        bb, "_config",
        lambda: {"enabled": True, "cost_alert_threshold_usd": 1.0,
                 "always_card": True, "retention_days": 30},
    )

    bb._on_session_start(session_id="s1")
    bb._on_post_tool_call(tool_name="exec", session_id="s1")
    bb._on_session_end(session_id="s1", model=UNPRICED_MODEL, platform="discord",
                       provider="anthropic", turn_usage=_usage(),
                       user_message="hi", final_response="yo")

    assert len(fake_store.records) == 1
    rec = fake_store.records[0]
    assert rec.model == UNPRICED_MODEL
    assert rec.cost_status == "unknown"
    assert rec.tools == ["exec"]          # full record, nothing truncated
    assert rec.user_text == "hi"
    assert rec.final_text == "yo"
    # …and the sentinel's blast radius was zero:
    assert swept == [30], "sentinel failure aborted the retention sweep"
    assert len(carded) == 1, "sentinel failure aborted the spending alert"


def test_turn_records_fully_when_alert_delivery_raises(hook, monkeypatch):
    bb, fake_store = hook
    monkeypatch.setattr(sentinel, "send_alert", _boom_alert)
    monkeypatch.setattr(sentinel, "_dispatch_alert", _Recorder())

    bb._on_session_end(session_id="s1", model=UNPRICED_MODEL, platform="discord",
                       provider="anthropic", turn_usage=_usage())

    assert len(fake_store.records) == 1
    assert fake_store.records[0].cost_status == "unknown"


def test_hook_end_to_end_ledgers_the_new_model(hook, monkeypatch):
    bb, fake_store = hook
    rec = _Recorder()
    monkeypatch.setattr(sentinel, "_dispatch_alert", rec)
    monkeypatch.setattr(sentinel, "send_alert", _ok_alert)

    bb._on_session_end(session_id="s1", model=UNPRICED_MODEL, platform="discord",
                       provider="anthropic", turn_usage=_usage())
    bb._on_session_end(session_id="s2", model=UNPRICED_MODEL, platform="discord",
                       provider="anthropic", turn_usage=_usage())

    assert len(fake_store.records) == 2, "both turns recorded"
    assert len(rec.calls) == 1, "but only one alert"
    assert len(store.list_unpriced_models()) == 1


def test_hook_end_to_end_known_model_is_inert(hook, monkeypatch):
    bb, fake_store = hook
    rec = _Recorder()
    monkeypatch.setattr(sentinel, "_dispatch_alert", rec)
    monkeypatch.setattr(
        bb, "compute_turn_cost",
        lambda *a, **k: (1.23, "estimated",
                         {"uncached": 1.0, "cache_read": 0.1,
                          "cache_write": 0.1, "output": 0.03}),
    )

    bb._on_session_end(session_id="s1", model="claude-opus-5", platform="discord",
                       provider="anthropic", turn_usage=_usage())

    assert len(fake_store.records) == 1
    assert rec.calls == []
    assert store.list_unpriced_models() == []


# ---------------------------------------------------------------------------
# message shape
# ---------------------------------------------------------------------------


def test_alert_message_carries_model_provider_and_card():
    text = sentinel.render_alert("claude-opus-9", "claude-apr")
    assert "claude-opus-9" in text
    assert "claude-apr" in text
    assert "t_2e382a4b" in text
    assert "rate ingestion needed" in text.lower()


def test_alerts_channel_is_pinned():
    assert sentinel.ALERTS_CHANNEL_ID == "1480528231286181948"
