"""Per-kwarg isolation for the runtime-footer consumer (t_96049446, follow-on to t_829a3079).

``tests/gateway/test_footer_consumer_in_turn.py`` pins the six restored fork kwargs with one
combined render, a provider check and a kwarg-PRESENCE floor. Presence is not behaviour: a
kwarg passed but wired to the wrong field keeps that file green. This file gives each kwarg
two tests that go red on exactly its own regression and name it in the failure:

1. ``test_wiring_<kwarg>``: the VALUE at the ``build_footer_line`` call equals the value the
   fork's rule derives from the fake turn result / session.
2. ``test_render_<kwarg>``: varying only that input changes the rendered string the way the
   fork specifies, and dropping the kwarg at the call regresses the string to the upstream
   shape (what Ace saw on 2026-10-04).

Consumer: ``GatewayTurnMixin._hmwa_runtime_footer_line`` (gateway/run_turn.py).
Renderer: ``gateway.runtime_footer.build_footer_line``.
Producer of ``provider`` on the turn result: tests/gateway/test_footer_provider_in_turn_result.py.
No test here reads source text.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import gateway.run as gw_run
import gateway.runtime_footer as rf
from gateway.config import Platform
from gateway.session import SessionSource

_REAL_BFL = rf.build_footer_line
_FIELDS = ["provider_model", "reasoning", "context_full", "messages", "latency", "cwd"]
_SESSION_KEY = "agent:main:discord:c1:thread-9"
# What the source alone derives; deliberately different from the turn's key (thread sessions).
_SOURCE_DERIVED_KEY = "agent:main:discord:c1"
_WHERE = "consumer = GatewayTurnMixin._hmwa_runtime_footer_line (gateway/run_turn.py)"


def _source():
    return SessionSource(platform=Platform.DISCORD, user_id="u1", chat_id="c1", user_name="ace")


def _result(**over):
    base = {
        "provider": "claude-alr",
        "model": "claude-fable-5-1",
        "last_prompt_tokens": 405_000,
        "context_length": 1_000_000,
        "reasoning_config": {"enabled": True, "effort": "xhigh"},
    }
    base.update(over)
    return base


def _config(global_effort="low", hard_limit=400):
    return {
        "agent": {"reasoning_effort": global_effort},
        "display": {"runtime_footer": {"enabled": True, "fields": list(_FIELDS)}},
        "compression": {"enabled": True, "hygiene_hard_message_limit": hard_limit},
    }


def _runner(message_count=42, derived_key=_SOURCE_DERIVED_KEY):
    runner = object.__new__(gw_run.GatewayRunner)

    class _DB:
        async def get_session(self, session_id):
            return {"message_count": message_count} if session_id == "sess-1" else None

    runner._session_db = _DB()
    runner._session_key_for_source = lambda source: derived_key
    return runner


def _set_session_override(runner, key, effort):
    runner._session_state(key).conversation.reasoning_override = {"enabled": True, "effort": effort}


def _consume(runner, result, *, session_key=_SESSION_KEY, source=None, turn_seconds=380.0):
    return asyncio.run(runner._hmwa_runtime_footer_line(
        result, source or _source(), turn_seconds,
        session_entry=SimpleNamespace(session_id="sess-1"), session_key=session_key,
    ))


@pytest.fixture
def cfg(monkeypatch):
    holder = {"cfg": _config()}
    monkeypatch.setattr(gw_run, "_load_gateway_config", lambda: holder["cfg"])
    # The session-override resolver falls back to the runtime config; keep it on the same dict.
    monkeypatch.setattr(gw_run, "_load_gateway_runtime_config", lambda: holder["cfg"], raising=False)
    monkeypatch.setattr(gw_run, "_terminal_scope_cwd", lambda default="": "")
    return holder


@pytest.fixture
def spy(cfg, monkeypatch):
    calls = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return _REAL_BFL(**kwargs)

    monkeypatch.setattr(rf, "build_footer_line", _spy)

    def _one(runner, result, **kw):
        calls.clear()
        _consume(runner, result, **kw)
        assert len(calls) == 1, f"expected one build_footer_line call, got {len(calls)}. {_WHERE}"
        return calls[0]

    return _one


@pytest.fixture
def drop(cfg, monkeypatch):
    """Simulate the consumer omitting one kwarg: the real renderer, minus that kwarg."""
    def _install(*names):
        def _without(**kwargs):
            for name in names:
                kwargs.pop(name, None)
            return _REAL_BFL(**kwargs)
        monkeypatch.setattr(rf, "build_footer_line", _without)
    return _install


# --- 1. provider ------------------------------------------------------------------------------

def test_wiring_provider(spy):
    call = spy(_runner(), _result(provider="openai-codex", model="gpt-6-astra"))
    assert call["provider"] == "openai-codex", (
        f"provider at build_footer_line is {call.get('provider')!r}, expected "
        f"agent_result['provider']. {_WHERE}"
    )


def test_render_provider(cfg, drop):
    a = _consume(_runner(), _result(provider="claude-alr"))
    b = _consume(_runner(), _result(provider="openrouter"))
    assert a.startswith("claude-alr/claude-fable-5-1 · ") and b.startswith("openrouter/claude-fable-5-1 · "), (a, b)
    drop("provider")
    regressed = _consume(_runner(), _result(provider="claude-alr"))
    assert regressed.startswith("claude-fable-5-1 · r:xhigh"), (
        f"dropping provider= should regress to Ace's 10-04 footer 'claude-fable-5-1 · …', got {regressed!r}"
    )


# --- 2. context_tokens via _footer_context_tokens ---------------------------------------------

def test_wiring_context_tokens(spy):
    result = _result(context_tokens_display=95_100)
    call = spy(_runner(), result)
    assert call["context_tokens"] == gw_run._footer_context_tokens(result) == 95_100, (
        f"context_tokens at build_footer_line is {call.get('context_tokens')!r}; the fork rule is "
        f"_footer_context_tokens(agent_result) (post-compaction context_tokens_display, not "
        f"last_prompt_tokens=405000). {_WHERE}"
    )
    # No display figure on the result (proxy path): raw last_prompt_tokens.
    assert spy(_runner(), _result())["context_tokens"] == 405_000


def test_render_context_tokens(cfg, drop):
    post = _consume(_runner(), _result(context_tokens_display=95_100))
    assert "95.1k/1M (10%)" in post, post
    drop("context_tokens")
    # build_footer_line requires context_tokens; a call without it raises and the consumer's
    # guard swallows it, so the footer disappears entirely.
    assert _consume(_runner(), _result(context_tokens_display=95_100)) == ""


def test_render_context_tokens_wrong_field_shows_precompaction(cfg, monkeypatch):
    """The realistic mis-wire: last_prompt_tokens instead of _footer_context_tokens."""
    def _wrong(**kwargs):
        kwargs["context_tokens"] = 405_000
        return _REAL_BFL(**kwargs)

    monkeypatch.setattr(rf, "build_footer_line", _wrong)
    line = _consume(_runner(), _result(context_tokens_display=95_100))
    assert "405k/1M (40%)" in line and "95.1k" not in line, line


# --- 3. context_estimated ---------------------------------------------------------------------

def test_wiring_context_estimated(spy):
    for flag in (True, False):
        result = _result(context_tokens_display=95_100, context_tokens_estimated=flag)
        call = spy(_runner(), result)
        assert call["context_estimated"] is bool(result["context_tokens_estimated"]), (
            f"context_estimated at build_footer_line is {call.get('context_estimated')!r} for "
            f"context_tokens_estimated={flag}; fork rule is bool(agent_result['context_tokens_estimated']). {_WHERE}"
        )
    assert spy(_runner(), _result())["context_estimated"] is False


def test_render_context_estimated(cfg, drop):
    est = _consume(_runner(), _result(context_tokens_display=95_100, context_tokens_estimated=True))
    exact = _consume(_runner(), _result(context_tokens_display=95_100, context_tokens_estimated=False))
    assert "~95.1k/1M" in est and "~95.1k" not in exact and "95.1k/1M" in exact, (est, exact)
    drop("context_estimated")
    regressed = _consume(_runner(), _result(context_tokens_display=95_100, context_tokens_estimated=True))
    assert "~95.1k" not in regressed and "95.1k/1M" in regressed, (
        f"dropping context_estimated= should lose the '~' prefix, got {regressed!r}"
    )


# --- 4. message_count / message_limit ---------------------------------------------------------

def test_wiring_message_stats(spy, cfg):
    cfg["cfg"] = _config(hard_limit=600)
    call = spy(_runner(message_count=326), _result())
    assert (call["message_count"], call["message_limit"]) == (326, 600), (
        f"message stats at build_footer_line are {(call.get('message_count'), call.get('message_limit'))}; "
        f"expected the session row's message_count and compression.hygiene_hard_message_limit. {_WHERE}"
    )


def test_render_message_stats(cfg, drop):
    assert "42/400msgs" in _consume(_runner(message_count=42), _result())
    assert "326/400msgs" in _consume(_runner(message_count=326), _result())
    cfg["cfg"] = _config(hard_limit=600)
    assert "326/600msgs" in _consume(_runner(message_count=326), _result())
    drop("message_count", "message_limit")
    regressed = _consume(_runner(message_count=326), _result())
    assert "msgs" not in regressed, f"dropping message stats should hide N/Mmsgs, got {regressed!r}"


# --- 5. reasoning (+ reasoning_config from the turn result) -----------------------------------

def test_wiring_reasoning(spy):
    from hermes_constants import reasoning_label

    for effort in ("xhigh", "minimal"):
        result = _result(reasoning_config={"enabled": True, "effort": effort})
        call = spy(_runner(), result)
        assert call["reasoning"] == reasoning_label(result["reasoning_config"]) == effort, (
            f"reasoning at build_footer_line is {call.get('reasoning')!r}; fork rule is the LIVE "
            f"agent_result['reasoning_config'] label ({effort!r}), not global config. {_WHERE}"
        )
    call = spy(_runner(), _result(reasoning_config={"enabled": False}))
    assert call["reasoning"] == "none"


def test_render_reasoning(cfg, drop):
    assert " · r:xhigh · " in _consume(_runner(), _result())
    assert " · r:high · " in _consume(_runner(), _result(reasoning_config={"enabled": True, "effort": "high"}))
    drop("reasoning")
    regressed = _consume(_runner(), _result())
    assert " · r:low · " in regressed and "r:xhigh" not in regressed, (
        f"dropping reasoning= should fall back to the GLOBAL config label r:low, got {regressed!r}"
    )


# --- 6. session_key / source (session-override fallback for the reasoning label) --------------

def test_wiring_session_key_and_source(cfg, monkeypatch):
    seen = []
    real = gw_run.GatewayRunner._footer_reasoning_label

    def _spy(self, **kwargs):
        seen.append(kwargs)
        return real(self, **kwargs)

    monkeypatch.setattr(gw_run.GatewayRunner, "_footer_reasoning_label", _spy)
    src = _source()
    _consume(_runner(), _result(), session_key=_SESSION_KEY, source=src)
    assert len(seen) == 1, seen
    assert seen[0].get("session_key") == _SESSION_KEY and seen[0].get("source") is src, (
        f"_footer_reasoning_label got session_key={seen[0].get('session_key')!r}, "
        f"source={seen[0].get('source')!r}; expected the turn's own key and source. {_WHERE}"
    )


def _drop_label_kwarg(monkeypatch, name):
    real = gw_run.GatewayRunner._footer_reasoning_label

    def _without(self, **kwargs):
        kwargs.pop(name, None)
        return real(self, **kwargs)

    monkeypatch.setattr(gw_run.GatewayRunner, "_footer_reasoning_label", _without)


def test_render_session_key(cfg, monkeypatch):
    """No live reasoning_config (errored turn): the label comes from THIS session's override."""
    runner = _runner()
    _set_session_override(runner, _SESSION_KEY, "high")
    line = _consume(runner, _result(reasoning_config=None))
    assert " · r:high · " in line, line
    _set_session_override(runner, _SESSION_KEY, "minimal")
    assert " · r:minimal · " in _consume(runner, _result(reasoning_config=None))
    _drop_label_kwarg(monkeypatch, "session_key")
    regressed = _consume(runner, _result(reasoning_config=None))
    assert " · r:low · " in regressed, (
        f"dropping session_key= should lose the thread session's override and show the global r:low, "
        f"got {regressed!r}"
    )


def test_render_source(cfg, monkeypatch):
    """A caller with no session_key: the override is found through the source-derived key."""
    runner = _runner()
    _set_session_override(runner, _SOURCE_DERIVED_KEY, "high")
    line = _consume(runner, _result(reasoning_config=None), session_key=None)
    assert " · r:high · " in line, line
    _drop_label_kwarg(monkeypatch, "source")
    regressed = _consume(runner, _result(reasoning_config=None), session_key=None)
    assert " · r:low · " in regressed, (
        f"dropping source= with no session_key should show the global r:low, got {regressed!r}"
    )
