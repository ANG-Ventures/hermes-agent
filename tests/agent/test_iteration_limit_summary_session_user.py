"""The iteration-limit summary must carry the provider profile's per-session
routing identity (the OpenAI ``user`` field from ``build_api_kwargs_extras``),
or a session-routed provider (claude-bridge pools) receives the summary keyless
and falls back to content-match session resume. The bridge fork suffix
(``_bridge_route_suffix``) must survive onto the summary call as well.

Since the 2026-10-01 parity sync the summary goes through the same kwargs
builder as the main loop (``_chat_summary_attempt`` -> ``agent._build_api_kwargs``
-> ``agent._interruptible_api_call``) instead of a hand-built
``chat.completions.create()`` call, so the routing identity rides the ordinary
profile path; this test drives that seam."""

import types
from unittest.mock import patch


class _RoutingProfile:
    """Mimics a session-routing provider profile (user = session key)."""

    fixed_temperature = None
    supports_prompt_cache_key = False

    def __init__(self, *, emit_user=True):
        self.emit_user = emit_user
        self.extras_calls = []

    def prepare_messages(self, messages):
        return messages

    def get_max_tokens(self, model):
        return None

    def build_extra_body(self, **_kw):
        return {}

    def build_api_kwargs_extras(self, **kw):
        self.extras_calls.append(kw)
        top = {}
        if self.emit_user and kw.get("session_id"):
            suffix = kw.get("bridge_route_suffix")
            top["user"] = "sess:" + kw["session_id"] + (f"-{suffix}" if suffix else "")
        return {}, top


def _run(profile, *, first_content="SUMMARY", suffix=None):
    from run_agent import AIAgent
    from agent.chat_completion_helpers import handle_max_iterations

    agent = AIAgent(api_key="test-key", base_url="https://example.invalid/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._cached_system_prompt = "SYS"
    agent.session_id = "sess-1"
    agent._bridge_route_suffix = suffix
    calls = []
    contents = iter([first_content, "RETRY-SUMMARY"])

    def _api_call(kwargs):
        calls.append(dict(kwargs))
        msg = types.SimpleNamespace(content=next(contents), tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    with patch("providers.get_provider_profile", return_value=profile), \
            patch.object(agent, "_interruptible_api_call", side_effect=_api_call):
        out = handle_max_iterations(agent, msgs, 5)
    return out, calls


def test_summary_carries_session_routing_user():
    profile = _RoutingProfile()
    out, calls = _run(profile)
    assert out == "SUMMARY"
    assert len(calls) == 1
    assert calls[0]["user"] == "sess:sess-1"
    assert profile.extras_calls[0]["session_id"] == "sess-1"


def test_summary_retry_also_carries_it_and_fork_suffix_is_kept():
    out, calls = _run(_RoutingProfile(), first_content="", suffix="review")
    assert out == "RETRY-SUMMARY"
    assert [c.get("user") for c in calls] == ["sess:sess-1-review"] * 2


def test_profile_without_routing_identity_sends_no_user():
    _, calls = _run(_RoutingProfile(emit_user=False))
    assert "user" not in calls[0]
