"""Tests that the background review fork inherits the parent's cached system prompt.

Regression coverage for issue #25322 (and PR #17276's first root cause): the
background review's outbound HTTP request must carry the same system bytes as
the parent's so Anthropic/OpenRouter's exact-prefix cache key matches.

Without this, every review rebuilds the system prompt from scratch — fresh
``_hermes_now()`` timestamp, fresh ``session_id``, and a different skills
prompt under the (former) narrow toolset — and the prefix-cache miss costs
roughly the full uncached system-prompt cost per nudge (~26% end-to-end on
Sonnet 4.5 per the contributor's measurement).
"""

import json
from types import SimpleNamespace
from unittest.mock import patch


def test_same_model_review_reuses_exact_parent_tools_without_memory_provider():
    """Provider-added mem0 schemas belong in the cache key, not the review's dispatch rights."""
    from agent.background_review import build_cache_parity_fork

    parent_tools = [
        {"type": "function", "function": {"name": "skill_view", "parameters": {}}},
        {"type": "function", "function": {"name": "mem0_search", "parameters": {"type": "object"}}},
    ]
    parent = SimpleNamespace(
        model="test-model", provider="openai", platform="cli", session_id="parent",
        tools=parent_tools, valid_tool_names={"skill_view", "mem0_search"},
        _cached_system_prompt="unchanged system", session_start=object(),
        _memory_store=None, _memory_enabled=False, _user_profile_enabled=False,
        enabled_toolsets=["skills", "memory"], disabled_toolsets=None,
        request_overrides={},
    )
    captured = {}

    class Fork:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tools = [parent_tools[0]]  # skip_memory=True omitted provider tools
            self.valid_tool_names = {"skill_view"}
            self._memory_manager = None
            self.context_compressor = None

    runtime = {"model": "test-model", "provider": "openai", "routed": False}
    with patch("run_agent.AIAgent", Fork), patch(
        "agent.background_review._resolve_review_runtime", return_value=runtime
    ):
        fork, _, routed = build_cache_parity_fork(parent, max_iterations=3)

    assert not routed
    assert captured["skip_memory"] is True
    assert fork._memory_manager is None  # never initialize external provider on review
    assert fork._cached_system_prompt == parent._cached_system_prompt
    assert json.dumps(fork.tools, separators=(",", ":")) == json.dumps(
        parent.tools, separators=(",", ":")
    )
    assert fork.valid_tool_names == parent.valid_tool_names
    assert fork.tools is not parent.tools  # fork cannot mutate the parent's schema list
    from hermes_cli.plugins import (
        clear_thread_tool_whitelist,
        get_pre_tool_call_block_message,
        set_thread_tool_whitelist,
    )
    # The mem0 schema is advertised for cache parity, but a real dispatch
    # through the review's existing gate must still refuse it.
    set_thread_tool_whitelist({"skill_view"})
    try:
        assert get_pre_tool_call_block_message("mem0_search", {"query": "x"})
        assert get_pre_tool_call_block_message("skill_view", {"name": "x"}) is None
    finally:
        clear_thread_tool_whitelist()


def test_review_fork_inherited_tools_survive_compaction_refresh_frozen_generation():
    """A mid-review compaction boundary runs refresh_agent_mcp_tools(content_aware=True).
    The fork has no memory manager (skip_memory), so an unguarded rebuild would drop the
    inherited mem0_* schemas and re-break the cache prefix; the frozen generation refuses it."""
    import copy as _copy
    from agent.background_review import build_cache_parity_fork
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    parent_tools = [
        {"type": "function", "function": {"name": "skill_view", "parameters": {}}},
        {"type": "function", "function": {"name": "mem0_search", "parameters": {"type": "object"}}},
    ]
    parent = SimpleNamespace(
        model="test-model", provider="openai", platform="cli", session_id="parent",
        tools=parent_tools, valid_tool_names={"skill_view", "mem0_search"},
        _cached_system_prompt="unchanged system", session_start=object(),
        _memory_store=None, _memory_enabled=False, _user_profile_enabled=False,
        enabled_toolsets=["skills"], disabled_toolsets=None, request_overrides={},
    )

    class Fork:
        def __init__(self, **kwargs):
            self.enabled_toolsets = kwargs.get("enabled_toolsets")
            self.disabled_toolsets = kwargs.get("disabled_toolsets")
            self.tools = [parent_tools[0]]
            self.valid_tool_names = {"skill_view"}
            self._tool_snapshot_generation = 0
            self._memory_manager = None
            self.context_compressor = None

    runtime = {"model": "test-model", "provider": "openai", "routed": False}
    with patch("run_agent.AIAgent", Fork), patch(
        "agent.background_review._resolve_review_runtime", return_value=runtime
    ):
        fork, _, routed = build_cache_parity_fork(parent, max_iterations=3)
    assert not routed
    before = _copy.deepcopy(fork.tools)

    # Control: an unfrozen copy IS rewritten by the refresh (mem0 dropped), so the
    # assertion below gates the freeze rather than a no-op refresh.
    control = SimpleNamespace(**{k: _copy.deepcopy(v) for k, v in vars(fork).items()})
    control._tool_snapshot_generation = 0
    refresh_agent_mcp_tools(control, content_aware=True)
    assert "mem0_search" not in control.valid_tool_names

    assert refresh_agent_mcp_tools(fork, content_aware=True) == set()
    assert fork.tools == before
    assert fork.valid_tool_names == {"skill_view", "mem0_search"}


def test_codex_review_fork_request_keeps_parent_cache_key_and_prefix():
    """openai-codex: the review fork's Responses request must route to the
    parent's cache (same ``prompt_cache_key``) and replay the parent's input
    as a strict prefix.

    ``prompt_cache_key`` hashes instructions + tool schemas + scope
    (agent/transports/codex.py ``_content_cache_key``), so a fork whose
    tools[] lacks the parent's provider schemas (mem0_*) gets a different
    key and lands on a replica without the parent's prefix. Measured on
    2026-09-23/24 (pre-#974, t_b28ced4f): 87 of 87 worker review-fork calls
    on openai-codex read 0 or the ~14k cross-session head instead of the
    parent's 100-275k prefix.
    """
    from agent.background_review import build_cache_parity_fork
    from agent.prompt_cache_scope import resolve_prompt_cache_scope
    from agent.transports import get_transport
    import agent.transports.codex  # noqa: F401

    parent_tools = [
        {"type": "function", "function": {"name": "skill_view", "description": "v",
                                          "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "mem0_search", "description": "m",
                                          "parameters": {"type": "object"}}},
    ]
    parent = SimpleNamespace(
        model="gpt-6-sol-900k", provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex", platform="cli",
        session_id="20260924_093923_1574ec", tools=parent_tools,
        valid_tool_names={"skill_view", "mem0_search"},
        _cached_system_prompt="PARENT SYSTEM", session_start=object(),
        _memory_store=None, _memory_enabled=False, _user_profile_enabled=False,
        enabled_toolsets=["skills", "memory"], disabled_toolsets=None,
        request_overrides={}, _session_db=None,
    )

    class Fork:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.provider = kwargs.get("provider")
            self.base_url = kwargs.get("base_url")
            # AIAgent(skip_memory=True) assembles tools without provider schemas.
            self.tools = [parent_tools[0]]
            self.valid_tool_names = {"skill_view"}
            self._memory_manager = None
            self.context_compressor = None

    runtime = {"model": parent.model, "provider": parent.provider,
               "base_url": parent.base_url, "routed": False}
    with patch("run_agent.AIAgent", Fork), patch(
        "agent.background_review._resolve_review_runtime", return_value=runtime
    ):
        fork, _, routed = build_cache_parity_fork(parent, max_iterations=3)
    assert not routed

    transport = get_transport("codex_responses")
    history = [
        {"role": "system", "content": parent._cached_system_prompt},
        {"role": "user", "content": "work kanban task t_x"},
        {"role": "assistant", "content": "done"},
    ]

    def request(agent, messages):
        return transport.build_kwargs(
            model=agent.model, messages=messages, tools=agent.tools,
            session_id=agent.session_id,
            cache_scope_id=resolve_prompt_cache_scope(agent),
            is_codex_backend=True,
        )

    parent_kw = request(parent, history)
    fork_kw = request(
        fork,
        [{**history[0], "content": fork._cached_system_prompt}] + history[1:]
        + [{"role": "user", "content": "review the conversation above"}],
    )

    assert fork_kw["prompt_cache_key"] == parent_kw["prompt_cache_key"]
    assert fork_kw["instructions"] == parent_kw["instructions"]
    assert fork_kw["tools"] == parent_kw["tools"]
    n = len(parent_kw["input"])
    assert fork_kw["input"][:n] == parent_kw["input"]
    assert len(fork_kw["input"]) > n


def _make_agent_stub(agent_cls):
    """Create a minimal AIAgent-like object with just enough state for _spawn_background_review."""
    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.session_id = "sess-123"
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._cached_system_prompt = (
        "PARENT-SYSTEM-PROMPT-BYTES — must be inherited verbatim "
        "for prefix-cache parity"
    )
    agent.ephemeral_system_prompt = (
        "WebUI session context:\n- Pinned per-request gateway context"
    )
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    # Non-None so the test catches a missing-kwarg regression.
    agent.enabled_toolsets = ["memory", "skills", "terminal"]
    agent.disabled_toolsets = ["spotify", "feishu_doc"]
    agent.tools = []
    # Chat context the review fork must inherit so its blackbox row is attributable.
    agent._chat_id = "571820863"
    agent._chat_name = "Daemonarchy / #aegis"
    agent._chat_type = "channel"
    # Non-None so the test catches reasoning_config NOT being inherited —
    # which would put the fork into a different Anthropic cache namespace.
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    # Non-empty so tests catch prefill/provider-routing NOT being inherited —
    # prefills sit right after the system message in the request body, and
    # OpenRouter provider pins decide WHICH upstream's cache gets hit.
    agent.prefill_messages = [{"role": "user", "content": "prefill turn"}]
    agent.providers_allowed = ["anthropic"]
    agent.providers_ignored = None
    agent.providers_order = None
    agent.provider_sort = "throughput"
    agent.provider_require_parameters = False
    agent.provider_data_collection = None
    return agent


class _SyncThread:
    """Drop-in replacement for threading.Thread that runs the target inline."""

    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_recorder_class(captured=None, record_on_run=()):
    """Build a Recorder class standing in for the review-fork AIAgent.

    Keeps the stub attribute list in ONE place: when
    ``_spawn_background_review`` starts touching a new fork attribute, only
    this factory needs the extra stub — not one copy per test.

    ``captured`` (dict): if given, ``__init__`` stores the full constructor
    kwargs under ``captured["init_kwargs"]`` so tests can assert on both
    kwarg values and kwarg *presence*.
    ``record_on_run``: instance attribute names copied into ``captured`` when
    ``run_conversation`` fires — for values the production code assigns
    after construction.
    """

    class _Recorder:
        def __init__(self, *args, **kwargs):
            if captured is not None:
                captured["init_kwargs"] = dict(kwargs)
            self._cached_system_prompt = None
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = None
            self._user_profile_enabled = None
            self._memory_nudge_interval = None
            self._skill_nudge_interval = None
            self.suppress_status_output = None
            self.session_start = None
            self.session_id = None
            self.tools = None
            self.valid_tool_names = set()
            self._tool_snapshot_generation = 0
            self.ephemeral_system_prompt = kwargs.get("ephemeral_system_prompt")

        def run_conversation(self, *args, **kwargs):
            if captured is not None:
                for _name in record_on_run:
                    captured[_name] = getattr(self, _name)
            raise RuntimeError(
                "stop after recording — don't actually call the API"
            )

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    return _Recorder


def test_review_fork_inherits_parent_cached_system_prompt():
    """The review fork's _cached_system_prompt must equal the parent's byte-for-byte.

    Anthropic's prefix cache keys on exact bytes; any divergence (timestamp
    minute tick, fresh session_id, narrower skills_prompt) shifts the key
    and forces a full re-cache. Inheriting the parent's cached prompt is
    the cheap, mechanical fix.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    parent_prompt = agent._cached_system_prompt

    _Recorder = _make_recorder_class()

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        # The production code assigns _cached_system_prompt AFTER __init__,
        # so wrap the recorder's __setattr__ to see that post-construction
        # write from _spawn_background_review.
        orig_setattr = _Recorder.__setattr__

        def _spy_setattr(self, name, value):
            if name == "_cached_system_prompt":
                captured["written_prompt"] = value
            orig_setattr(self, name, value)

        with patch.object(_Recorder, "__setattr__", _spy_setattr):
            agent._spawn_background_review(
                messages_snapshot=[],
                review_memory=True,
                review_skills=False,
            )

    assert "written_prompt" in captured, (
        "_spawn_background_review never assigned _cached_system_prompt on the review agent"
    )
    assert captured["written_prompt"] == parent_prompt, (
        f"Review fork's _cached_system_prompt diverged from parent's. "
        f"Got {captured['written_prompt']!r}, expected {parent_prompt!r}. "
        "This breaks Anthropic/OpenRouter prefix-cache parity (#25322)."
    )


def test_review_fork_inherits_parent_ephemeral_system_prompt():
    """The fork must send the parent's complete effective system prompt.

    Gateway session context is appended through ``ephemeral_system_prompt`` at
    API-call time, outside ``_cached_system_prompt``.  Copying only the cached
    base therefore makes every background review diverge at the gateway block
    and miss the parent's warm prefix cache.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}
    _Recorder = _make_recorder_class(
        captured,
        record_on_run=("_cached_system_prompt", "ephemeral_system_prompt"),
    )

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    # Pairwise asserts: stronger than comparing a locally re-joined
    # "effective" prompt (which would re-implement the production join and
    # silently keep passing if the separator ever changed — and would compare
    # equal for cached="A\n\nB"/ephemeral="" vs cached="A"/ephemeral="B").
    assert captured["_cached_system_prompt"] == agent._cached_system_prompt
    assert captured["ephemeral_system_prompt"] == agent.ephemeral_system_prompt


def test_review_fork_inherits_prefill_and_provider_routing():
    """Non-routed fork must inherit prefill messages and OpenRouter pins.

    Prefill messages are inserted right after the system message at
    API-call time, so omitting them diverges the fork's request body from
    the parent's warm prefix at message index 1. OpenRouter provider pins
    (providers_allowed/order/sort/...) decide which UPSTREAM provider serves
    the request — prompt caches live per upstream, so an unpinned fork can
    be routed to a different upstream and miss even a byte-identical prefix.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}
    _Recorder = _make_recorder_class(captured)

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    init_kwargs = captured.get("init_kwargs", {})
    assert init_kwargs.get("prefill_messages") == agent.prefill_messages
    # Must be a DEEP copy: the fork's unicode-error recovery
    # (_sanitize_messages_surrogates) mutates prefill dicts in place, so
    # aliased dicts would let the fork rewrite the parent's prefill bytes
    # — silently breaking the parent's own warm prefix.
    assert (
        init_kwargs["prefill_messages"][0] is not agent.prefill_messages[0]
    ), "fork prefill aliases the parent's dicts (needs deepcopy)"
    assert init_kwargs.get("providers_allowed") == agent.providers_allowed
    assert init_kwargs.get("provider_sort") == agent.provider_sort


def test_review_fork_pins_session_start_and_session_id():
    """Defensive complement to cached-system-prompt inheritance.

    Even though ``_cached_system_prompt`` inheritance short-circuits the
    normal rebuild path, pinning ``session_start`` and ``session_id`` to
    the parent's guarantees byte-identical output from any code path that
    re-renders parts of the system prompt (compression, plugin hooks).
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    _Recorder = _make_recorder_class(
        captured, record_on_run=("session_start", "session_id")
    )

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert captured.get("session_start") == agent.session_start, (
        "Review fork did not inherit parent's session_start — "
        "system-prompt rebuild paths would diverge."
    )
    assert captured.get("session_id") == agent.session_id, (
        "Review fork did not inherit parent's session_id — "
        "system-prompt rebuild paths would diverge."
    )


def test_review_fork_inherits_parent_chat_context():
    """Blackbox attribution: fork must inherit parent's chat_id/chat_name/chat_type.

    Regression for background_review rows landing with empty chat fields, which
    made them un-attributable in /cost session and the blackbox DB.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)

    captured = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            captured["chat_id"] = kwargs.get("chat_id")
            captured["chat_name"] = kwargs.get("chat_name")
            captured["chat_type"] = kwargs.get("chat_type")
            self._cached_system_prompt = None
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = None
            self._user_profile_enabled = None
            self._memory_nudge_interval = None
            self._skill_nudge_interval = None
            self.suppress_status_output = None
            self.session_start = None
            self.session_id = None

        def run_conversation(self, *args, **kwargs):
            raise RuntimeError("stop after recording — don't actually call the API")

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert captured.get("chat_id") == "571820863", (
        f"chat_id not inherited: {captured.get('chat_id')!r}"
    )
    assert captured.get("chat_name") == "Daemonarchy / #aegis", (
        f"chat_name not inherited: {captured.get('chat_name')!r}"
    )
    assert captured.get("chat_type") == "channel", (
        f"chat_type not inherited: {captured.get('chat_type')!r}"
    )


def test_routed_review_fork_does_not_inherit_reasoning_config():
    """Routed aux path: the fork must NOT inherit the parent's reasoning_config.

    When ``auxiliary.background_review.{provider,model}`` routes the review
    to a different model, cache parity is moot (the cache is cold on that
    model regardless) and the parent's effort vocabulary may be invalid for
    the routed model/provider (OpenRouter ``extra_body.reasoning.effort`` is
    forwarded unclamped; codex_responses passes ``max``/``ultra`` through
    unmapped except on gpt-5.6/xAI). The routed fork must fall back to
    provider defaults, mirroring the ``not _routed`` gate on
    ``_cached_system_prompt`` inheritance.
    """
    import run_agent
    import agent.background_review as bg_review

    agent_stub = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    _Recorder = _make_recorder_class(captured)

    routed_runtime = {
        "provider": "openrouter",
        "model": "aux-cheap-model",
        "api_key": "test-key",
        "base_url": None,
        "api_mode": None,
        "credential_pool": None,
        "request_overrides": {},
        "max_tokens": None,
        "command": None,
        "args": [],
        "routed": True,
    }

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch.object(bg_review, "_resolve_review_runtime",
                      return_value=routed_runtime), \
         patch("threading.Thread", _SyncThread):
        agent_stub._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    init_kwargs = captured.get("init_kwargs", {})
    assert "reasoning_config" not in init_kwargs, (
        f"Routed review fork was passed the parent's reasoning_config "
        f"({init_kwargs.get('reasoning_config')!r}). On the routed path the "
        "cache is cold (no parity benefit) and the parent's effort value may "
        "be invalid for the routed model/provider — it must be omitted so "
        "the fork uses provider defaults."
    )
    # The whole cache-parity kwarg family shares the same ``not _routed``
    # gate — a future refactor hoisting any of them out of the gate must
    # fail here, not silently ship parent-only context to a foreign model.
    for _gated in (
        "ephemeral_system_prompt",
        "prefill_messages",
        "providers_allowed",
        "provider_sort",
    ):
        assert _gated not in init_kwargs, (
            f"Routed review fork was passed parent-only kwarg {_gated!r}; "
            "cache-parity inheritance must stay behind the not-routed gate."
        )


def test_review_fork_inherited_tools_survive_compaction_refresh():
    """Inherited tools survive mid-review compaction refresh (#103579).

    Acceptance criterion 1 requires the fork to advertise the same tools[] as
    the parent when targeting the same cache scope. Mid-review compaction
    boundaries invoke refresh_agent_mcp_tools(content_aware=True), which re-reads
    the live registry and drops memory provider tools unless the snapshot
    generation staleness check refuses the rebuild.
    """
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    agent = _make_agent_stub(run_agent.AIAgent)
    parent_tools = [
        {"type": "function", "function": {"name": "terminal_command"}},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "memory"}},
        {"type": "function", "function": {"name": "fact_store"}},
        {"type": "function", "function": {"name": "fact_feedback"}},
    ]
    agent.tools = parent_tools

    _Recorder = _make_recorder_class()

    with patch.object(run_agent, "AIAgent", _Recorder):
        fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)
        assert not routed
        assert fork.tools == parent_tools
        # Deep copy: the fork's later in-place tool edits must not leak into the parent's array.
        assert fork.tools is not parent_tools and fork.tools[0] is not parent_tools[0]

        # Simulate mid-review compaction boundary tool refresh
        added = refresh_agent_mcp_tools(fork, content_aware=True)
        assert added == set()
        assert [t["function"]["name"] for t in fork.tools] == [
            "terminal_command", "read_file", "memory", "fact_store", "fact_feedback"
        ]
        assert fork.valid_tool_names == {
            "terminal_command", "read_file", "memory", "fact_store", "fact_feedback"
        }


def test_unrouted_review_fork_inherits_empty_tool_surface():
    """Empty parent tools[] is a valid snapshot and must be copied and frozen (#103579).

    If no tools pass availability when the parent is constructed (parent.tools = []),
    the unrouted fork must inherit an empty list and freeze _tool_snapshot_generation.
    This guarantees late MCP/plugin tools discovered during fork construction or
    mid-review compaction do not break cache parity against the parent's empty surface.
    """
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    agent = _make_agent_stub(run_agent.AIAgent)
    agent.tools = []

    _BaseRecorder = _make_recorder_class()

    class _RecorderWithLateTool(_BaseRecorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Simulate a late tool appearing in the constructor result before inheritance
            self.tools = [{"type": "function", "function": {"name": "newly_available"}}]
            self.valid_tool_names = {"newly_available"}

    with patch.object(run_agent, "AIAgent", _RecorderWithLateTool):
        fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)
        assert not routed
        assert fork.tools == []
        assert fork.tools is not agent.tools
        assert fork.valid_tool_names == set()

        # Compaction refresh must refuse rebuild on frozen snapshot
        added = refresh_agent_mcp_tools(fork, content_aware=True)
        assert added == set()
        assert fork.tools == []
        assert fork.valid_tool_names == set()


def test_same_model_review_surfaces_ignored_reasoning_effort_once():
    """#104116: ``auxiliary.background_review.reasoning_effort`` is dropped on the same-model path
    (cache parity, #30532) — that no-op must be visible instead of silent, and must not fire per
    fork (a nudge-per-turn session would spam)."""
    import run_agent
    from agent.background_review import build_cache_parity_fork

    agent = _make_agent_stub(run_agent.AIAgent)
    warnings = []
    agent._emit_warning = warnings.append
    captured = {}
    _Recorder = _make_recorder_class(captured)

    with patch.object(run_agent, "AIAgent", _Recorder):
        _fork, _rt, routed = build_cache_parity_fork(
            agent, {"reasoning_effort": "low"}, max_iterations=5)
        assert not routed
        assert len(warnings) == 1, f"expected exactly one notice, got {warnings!r}"
        assert "reasoning_effort" in warnings[0], warnings[0]
        # Cache-parity behaviour itself is unchanged: the fork still inherits the parent verbatim.
        assert captured["init_kwargs"]["reasoning_config"] == agent.reasoning_config
        # Second fork on the same parent: no repeat.
        build_cache_parity_fork(agent, {"reasoning_effort": "low"}, max_iterations=5)
        assert len(warnings) == 1, f"notice repeated per fork: {warnings!r}"


def test_review_effort_notice_only_for_same_model_review_forks():
    """No notice when the key is unset, when the fork is routed (#94825 owns that path), or for the
    /btw ``side_question`` fork sharing ``build_cache_parity_fork``."""
    import run_agent
    import agent.background_review as bg_review
    from agent.background_review import build_cache_parity_fork

    _Recorder = _make_recorder_class()
    routed_runtime = {
        "provider": "openrouter", "model": "aux-cheap-model", "api_key": "test-key",
        "base_url": None, "api_mode": None, "credential_pool": None, "request_overrides": {},
        "max_tokens": None, "command": None, "args": [], "routed": True,
    }

    def _warns(task_cfg, **kwargs):
        agent = _make_agent_stub(run_agent.AIAgent)
        warnings = []
        agent._emit_warning = warnings.append
        with patch.object(run_agent, "AIAgent", _Recorder):
            build_cache_parity_fork(agent, task_cfg, max_iterations=5, **kwargs)
        return warnings

    assert _warns({"reasoning_effort": ""}) == []
    assert _warns({}) == []
    assert _warns({"reasoning_effort": "low"}, write_origin="side_question") == []
    with patch.object(bg_review, "_resolve_review_runtime", return_value=routed_runtime):
        assert _warns({"reasoning_effort": "low"}) == []


def test_same_model_fork_inherits_parent_cache_scope_gateway_key(tmp_path):
    """#109964 invariant 1 (gateway-key case): the same-model review fork must
    resolve the PARENT's cache scope, even though it is _persist_disabled and
    _session_db=None. Pre-fix, both resolvers diverged on their own — the header
    (affinity) and body (prompt_cache_key) keyed a different bucket, costing one
    cold ~full-context request per review."""
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from agent.prompt_cache_scope import (
        declared_conversation_scope,
        resolve_prompt_cache_scope,
    )
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _make_agent_stub(run_agent.AIAgent)
        # Gateway parent shape: declared key, real DB row behind it.
        agent._gateway_session_key = "gw-key-1"
        agent._session_db = db
        db.create_session("sess-123", source="test")

        with patch.object(run_agent, "AIAgent", _make_recorder_class()):
            fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)
            btw, _rt, _ = build_cache_parity_fork(agent, max_iterations=5, write_origin="side_question")

        assert not routed
        # Only the review fork may derive its own xAI scope after compacting; /btw never does.
        tags = [getattr(f, "_prompt_cache_fork_tag", None) for f in (fork, btw)]
        assert tags == ["review", None], tags
        parent_scope = resolve_prompt_cache_scope(agent)
        assert parent_scope.startswith("gwk_"), parent_scope
        # The fork stamps the parent's resolved scope; both resolvers honor it.
        assert getattr(fork, "_inherited_cache_scope", None) == parent_scope
        assert declared_conversation_scope(fork) == parent_scope
        assert resolve_prompt_cache_scope(fork) == parent_scope
    finally:
        db.close()


def test_same_model_fork_inherits_parent_cache_scope_rotated_lineage(tmp_path):
    """#109964 invariant 1 (rotated-lineage case): a CLI parent whose lineage
    root != current physical id must also pass its scope to the fork. Pre-fix
    the parent resolved 'root-sid' while the fork fell to the physical id.

    Every identity the fork publishes must equal the parent's: body cache key, the
    affinity header (None for both — a physical root is not a declared ``gwk_`` scope)
    and the Portal ``conversation=`` root (fork has no DB to walk the lineage)."""
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from agent.prompt_cache_scope import declared_conversation_scope, resolve_prompt_cache_scope
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _make_agent_stub(run_agent.AIAgent)
        agent._session_db = db
        # Legacy compression rotation: parent row ends, child inherits its lineage.
        db.create_session("root-sid", source="test")
        db.end_session("root-sid", "compression")
        db.create_session("sess-123", source="test", parent_session_id="root-sid")
        agent.session_id = "sess-123"

        with patch.object(run_agent, "AIAgent", _make_recorder_class()):
            fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)

        assert not routed
        assert resolve_prompt_cache_scope(agent) == "root-sid"
        assert getattr(fork, "_inherited_cache_scope", None) == "root-sid"
        assert resolve_prompt_cache_scope(fork) == "root-sid"
        assert declared_conversation_scope(fork) is declared_conversation_scope(agent) is None
        fork_root = run_agent.AIAgent._conversation_root_id(fork)
        assert fork_root == agent._conversation_root_id() == "root-sid"
    finally:
        db.close()


def test_routed_fork_does_not_inherit_cache_scope():
    """#109964 invariant 2: a routed (different-model) fork is cache-cold on
    that model anyway — it must NOT inherit the parent's scope. Nor may fresh
    agents (no attribute set) be affected: the fail-closed default stands."""
    import run_agent
    from agent.background_review import build_cache_parity_fork

    agent = _make_agent_stub(run_agent.AIAgent)
    agent._gateway_session_key = "gw-key-1"
    agent._prompt_cache_scope_memo = (("sess-123", True), "gwk_parentscope0000000000abc")

    _RoutedRecorder = _make_recorder_class()

    with patch.object(run_agent, "AIAgent", _RoutedRecorder), \
         patch("agent.background_review._resolve_review_runtime",
               lambda *a, **k: {"routed": True, "model": "other-model"}):
        fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)

    assert routed
    assert not getattr(fork, "_inherited_cache_scope", None), (
        "Routed fork must not inherit the parent's cache scope — its prefix "
        "is cache-cold on the different model regardless."
    )
