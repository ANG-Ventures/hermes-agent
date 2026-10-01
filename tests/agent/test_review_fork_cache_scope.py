"""A background-review fork gets its OWN cache scope on xAI once it has compacted.

A same-model fork shares the parent's scope (#109964), so its first request is a warm
prefix read. xAI's ``x-grok-conv-id`` / ``prompt_cache_key`` pin ONE server slot, so
once the fork's own in-place compaction diverges its stream it would evict the
parent's slot; the resolver then derives ``<scope>::<tag>`` on xAI routes only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_cache_scope import resolve_prompt_cache_scope

SYS = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
]


def _agent(provider, model="grok-4.3", tag=None, compactions=0):
    a = SimpleNamespace(
        session_id="parent-sess", _session_db=None, provider=provider, model=model,
        base_url="", context_compressor=SimpleNamespace(compression_count=compactions),
    )
    if tag is not None:
        a._prompt_cache_fork_tag = tag
    return a


def test_inherited_parent_scope_is_derived_on_xai_only_after_fork_compaction():
    """Request #1 shares the parent's key; post-compaction derives on xAI, never elsewhere."""
    fork = _agent("xai-oauth", tag="review")
    fork._inherited_cache_scope = "gwk_parentscope"
    assert resolve_prompt_cache_scope(fork) == "gwk_parentscope"
    fork.context_compressor.compression_count = 1  # the fork's own in-place compaction
    assert resolve_prompt_cache_scope(fork) == "gwk_parentscope::review"
    fork.provider, fork.model = "anthropic", "claude-opus-4-8"
    assert resolve_prompt_cache_scope(fork) == "gwk_parentscope"


def test_xai_fork_sends_distinct_conv_id_and_cache_key():
    from agent.transports.codex import ResponsesApiTransport

    def build(agent):
        return ResponsesApiTransport().build_kwargs(
            model="grok-4.3", messages=SYS, tools=[], session_id=agent.session_id,
            cache_scope_id=resolve_prompt_cache_scope(agent), is_xai_responses=True,
        )

    parent = build(_agent("xai-oauth"))
    fork = build(_agent("xai-oauth", tag="review", compactions=1))
    assert parent["extra_headers"]["x-grok-conv-id"] == "parent-sess"
    assert fork["extra_headers"]["x-grok-conv-id"] == "parent-sess::review"
    assert parent["extra_body"]["prompt_cache_key"] != fork["extra_body"]["prompt_cache_key"]


def test_concurrent_forks_of_one_parent_get_distinct_cache_tags():
    """FleetReview #91: the tag was fixed per write_origin, so two forks alive
    at once shared one slot-keyed scope and evicted each other. A fork that
    starts after the first closed reuses the base tag (warm slot).

    Scope derivation is compaction-gated (xAI only), so both doubles report
    one compaction: the invariant under test is the per-fork tag claim."""
    from agent.background_review import build_cache_parity_fork

    parent = SimpleNamespace(
        model="grok-4.3", provider="xai-oauth", platform="cli", session_id="parent-91",
        tools=[], valid_tool_names=set(), _cached_system_prompt="sys",
        session_start=object(), _memory_store=None, _memory_enabled=False,
        _user_profile_enabled=False, enabled_toolsets=None, disabled_toolsets=None,
        request_overrides={},
    )

    class Fork:
        def __init__(self, **kwargs):
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self.base_url = kwargs.get("base_url") or ""
            self.tools = []
            self.valid_tool_names = set()
            self._memory_manager = None
            self.context_compressor = SimpleNamespace(compression_count=1)

        def close(self):
            pass

    runtime = {"model": "grok-4.3", "provider": "xai-oauth", "routed": False}
    with patch("run_agent.AIAgent", Fork), patch(
        "agent.background_review._resolve_review_runtime", return_value=runtime
    ):
        a, _, _ = build_cache_parity_fork(parent, max_iterations=3)
        b, _, _ = build_cache_parity_fork(parent, max_iterations=3)
        assert resolve_prompt_cache_scope(a) != resolve_prompt_cache_scope(b)
        assert a._prompt_cache_fork_tag == "review"
        a.close()
        c, _, _ = build_cache_parity_fork(parent, max_iterations=3)
        assert c._prompt_cache_fork_tag == "review"
        b.close()
        c.close()
