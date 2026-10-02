import ast
import inspect
"""#84733: prompt-cache TTL/prefix propagation into MoA/aux paths + failover re-preflight.

The main loop threads ``agent._cache_ttl`` and the stable system prefix into
``build_prompt_cache_plan``, but the MoA/aux helper only accepted
``cache_disabled`` — so a configured ``1h`` regressed to the 5m default and
the destination system prompt was marked as one whole breakpoint. These
tests pin the threaded parameters (TTL + static prefix) on
``plan_cache_sections_for_destination`` and the MoA decoration helper, the
per-destination Qwen clamp (1h -> 5m), and the failover re-preflight
contract (every fallback activation must restart the outer iteration so the
pre-API preflight re-runs against the fallback's context window).
"""


def _collect_cache_controls(obj):
    """Return every ``cache_control`` marker dict reachable in ``obj``."""
    markers = []
    if isinstance(obj, dict):
        if "cache_control" in obj:
            markers.append(obj["cache_control"])
        for value in obj.values():
            markers.extend(_collect_cache_controls(value))
    elif isinstance(obj, list):
        for value in obj:
            markers.extend(_collect_cache_controls(value))
    return markers


class TestPlanCacheSectionsThreadsTtlAndPrefix:
    def test_cache_ttl_1h_reaches_markers(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.8",
            cache_disabled=False,
            cache_ttl="1h",
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "expected cache_control markers on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the configured 1h tier must reach the destination plan markers"
        )

    def test_static_system_prefix_gets_early_breakpoint(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "stable prefix\nvolatile suffix"},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.8",
            cache_disabled=False,
            cache_ttl="5m",
            static_system_prefix="stable prefix",
        )
        system_content = out_msgs[0]["content"]
        assert isinstance(system_content, list) and len(system_content) == 2, (
            "the destination system prompt must split into [static, volatile] "
            "parts instead of marking the whole prompt as one breakpoint"
        )
        assert system_content[0]["text"] == "stable prefix"
        assert system_content[1]["text"] == "\nvolatile suffix"

    def test_qwen_1h_clamped_to_5m(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="opencode",
            base_url="https://api.opencode.ai",
            api_mode="chat_completions",
            model="qwen3.6-plus",
            cache_disabled=False,
            cache_ttl="1h",
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "opencode+qwen is a cache-honoring route"
        assert all("ttl" not in m for m in markers), (
            "Qwen's 5-minute-only context cache must clamp a configured 1h"
        )


class TestMoACacheControlThreadsTtl:
    def test_moa_decoration_uses_threaded_1h(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        runtime = {
            "provider": "anthropic",
            "model": "claude-opus-4.8",
            "base_url": "",
            "api_mode": "anthropic_messages",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False, cache_ttl="1h"
        )
        markers = _collect_cache_controls(out)
        assert markers, "expected MoA decoration on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the agent's 1h tier must stop regressing to 5m on MoA advisor calls"
        )
        # Caller messages must stay undecorated.
        assert not _collect_cache_controls(messages)

    def test_moa_qwen_1h_clamped_to_5m(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        runtime = {
            "provider": "opencode",
            "model": "qwen3.6-plus",
            "base_url": "",
            "api_mode": "chat_completions",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False, cache_ttl="1h"
        )
        markers = _collect_cache_controls(out)
        assert markers, "opencode+qwen is a cache-honoring MoA route"
        assert all("ttl" not in m for m in markers), (
            "MoA decoration must clamp 1h to 5m on Qwen destinations"
        )

    def test_moa_decoration_defaults_to_5m_without_ttl(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        runtime = {
            "provider": "anthropic",
            "model": "claude-opus-4.8",
            "base_url": "",
            "api_mode": "anthropic_messages",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False
        )
        markers = _collect_cache_controls(out)
        assert markers
        assert all("ttl" not in m for m in markers)


class TestFailoverRestartsPreflight:
    """#84733: a fallback provider switch must re-run the pre-API preflight.

    ``_try_activate_fallback`` already shrinks the compressor's context
    window to the fallback's; the pre-API preflight runs at the top of the
    OUTER iteration loop, before the retry loop. So the restart discipline
    is loop-aware:

    - Sites INSIDE the retry loop (``while retry_count < max_retries``)
      must ``break`` out of it with ``restart_with_rebuilt_messages`` set,
      so the handler after the retry loop refunds the budget and
      ``continue``s the outer iteration (which re-runs the preflight).
      A plain ``continue`` there would only re-fire the retry loop and
      skip the preflight — the original bug.
    - Sites DIRECTLY in the outer loop must ``continue`` — the next outer
      iteration re-runs the preflight already. A ``break`` there would
      exit the conversation loop and end the turn without ever calling
      the just-activated fallback.

    Source-level guard: parsing the function is cheap, and the assertion
    encodes the bug class — a new failover site added with the wrong
    restart statement for its loop fails here on purpose.
    """

    # Upstream split ``run_conversation`` into phase helpers (agent/turn_*.py) that return a
    # verdict instead of executing ``break`` / ``continue`` themselves. The retry loop is
    # ``conversation_loop._run_api_retry_loop`` and a ``"break"`` verdict from a retry-loop
    # phase leaves it with ``restart_with_rebuilt_messages`` armed (``_arm_fallback_restart``),
    # which ``apply_retry_restarts`` turns into an outer ``continue``. Outer-loop phases must
    # verdict ``"continue"`` directly.
    RETRY_LOOP_PHASES = {
        "nous_rate_limit_guard", "retry_invalid_response", "handle_content_policy_refusal",
        "_content_filter_fallback", "settle_unrecovered_error", "route_classified_error",
    }
    OUTER_LOOP_PHASES = {"recover_empty_response", "continue_codex_incomplete"}
    # Pre-loop: the codex app-server turn hands its failure to the generic loop (bool), no verdict.
    EXEMPT = {"activate_codex_app_server_fallback"}
    PHASE_MODULES = (
        "turn_api_call", "turn_api_error", "turn_empty_response", "turn_recovery",
        "turn_response_check", "turn_truncation",
    )

    @staticmethod
    def _verdict_action(fn, if_node):
        """The loop action a fallback-activation ``if`` body resolves to."""
        rets = [stmt for stmt in if_node.body if isinstance(stmt, ast.Return)]
        assert len(rets) == 1, f"{fn.name}: fallback site must return exactly one verdict"
        value = rets[0].value
        if isinstance(value, ast.Name) and value.id == "CODEX_FALLBACK_ACTIVATED":
            return "continue"  # turn_response_intake maps the sentinel to _verdict("continue")
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and not value.args:
                nested = next(
                    (n for n in ast.walk(fn)
                     if isinstance(n, ast.FunctionDef) and n.name == value.func.id), None)
                if nested is not None:
                    inner = [n.value for n in ast.walk(nested) if isinstance(n, ast.Return)]
                    assert len(inner) == 1
                    value = inner[0]
            if isinstance(value, ast.Call) and value.args and isinstance(value.args[0], ast.Constant):
                return value.args[0].value
        raise AssertionError(f"{fn.name}: unrecognised verdict shape {ast.dump(value)}")

    def test_every_fallback_activation_restarts_preflight(self):
        import importlib

        from agent import conversation_loop

        loop_src = inspect.getsource(conversation_loop._run_api_retry_loop)
        assert "while s.retry_count < s.max_retries" in loop_src, "expected the retry loop"
        for name in self.RETRY_LOOP_PHASES | self.OUTER_LOOP_PHASES | self.EXEMPT:
            assert any(
                hasattr(importlib.import_module(f"agent.{m}"), name) for m in self.PHASE_MODULES
            ), f"phase {name} moved: update the loop table"

        seen = 0
        for mod_name in self.PHASE_MODULES:
            mod = importlib.import_module(f"agent.{mod_name}")
            tree = ast.parse(inspect.getsource(mod))
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute) and node.attr == "_try_activate_fallback"):
                    continue
                seen += 1
                if_node = fn = None
                cur = parents.get(node)
                while cur is not None and fn is None:
                    if isinstance(cur, ast.If) and if_node is None:
                        if_node = cur
                    if isinstance(cur, ast.FunctionDef):
                        fn = cur
                    cur = parents.get(cur)
                assert fn is not None
                if fn.name in self.EXEMPT:
                    continue
                # Every site must be a direct ``if agent._try_activate_fallback(...):`` so this
                # guard can bind its restart discipline (#84733).
                assert if_node is not None and node in list(ast.walk(if_node.test)), (
                    f"{mod_name}.{fn.name}: _try_activate_fallback must be an `if` test"
                )
                action = self._verdict_action(fn, if_node)
                if fn.name in self.RETRY_LOOP_PHASES:
                    assert action == "break", (
                        f"{mod_name}.{fn.name}: retry-loop fallback activation must verdict "
                        "'break' so apply_retry_restarts re-runs the preflight (#84733)"
                    )
                    assert "restart_with_rebuilt_messages = True" in inspect.getsource(mod) or (
                        "_arm_fallback_restart(" in ast.unparse(if_node)
                        or "_fallback_break()" in ast.unparse(if_node)
                    ), f"{mod_name}.{fn.name}: break site must arm restart_with_rebuilt_messages"
                elif fn.name in self.OUTER_LOOP_PHASES:
                    assert action == "continue", (
                        f"{mod_name}.{fn.name}: outer-loop fallback activation must verdict "
                        "'continue' (re-runs the preflight); 'break' ends the turn (#84733)"
                    )
                else:
                    raise AssertionError(
                        f"{mod_name}.{fn.name}: new fallback site — classify it in the loop table"
                    )
        assert seen >= 12, f"expected the known fallback sites, saw {seen}"


class TestAuxFallbackReplanThreadsTtl:
    """#84733 follow-up: the auxiliary fallback replan path threads the
    configured tier too — it has no live agent, so it reads the same
    config key agent_init snapshots into ``agent._cache_ttl``."""

    def test_configured_cache_ttl_reads_valid_tiers(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "1h"}},
        )
        assert arh.configured_cache_ttl() == "1h"
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "5m"}},
        )
        assert arh.configured_cache_ttl() == "5m"

    def test_configured_cache_ttl_none_for_disabled_or_unknown(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        for value in ("off", False, None, "2h"):
            monkeypatch.setattr(
                "hermes_cli.config.load_config_readonly",
                lambda value=value: {"prompt_caching": {"cache_ttl": value}},
            )
            assert arh.configured_cache_ttl() is None, value

    def test_replan_threads_configured_ttl_to_markers(self, monkeypatch):
        from agent import auxiliary_client

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "1h"}},
        )
        destination = auxiliary_client._FallbackDestination(
            "anthropic",
            "https://api.anthropic.com",
            "anthropic_messages",
            "claude-opus-4.8",
        )
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = auxiliary_client._replan_synchronous_cache_sections(
            messages, None, destination=destination
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "expected cache_control markers on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the configured 1h tier must reach auxiliary fallback replans"
        )
