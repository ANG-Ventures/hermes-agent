"""Effect-gate: every KNOWABLE-reason `_try_activate_fallback` call site in the
conversation loop threads a non-None `reason=` (2026-07-12).

Background: PR #280 gave the fallback announce a `(reason)` rider so a failover
says WHY (safety refusal vs rate limit vs provider overloaded vs the honest
"connection issue" floor). But the rider only renders if a reason actually
REACHES the announce. Several failover *call sites* in `conversation_loop.py`
were calling `agent._try_activate_fallback()` BARE, dropping the classified
reason — so a retry-exhausted `503 overloaded` failover announced a reason-less
line (the 2026-07-11 21:14 incident, session 20260710_182847_84cb906d).

This test is an EFFECT gate, not a paren-emptiness grep: it parses the AST of
each flagged call site and asserts the actual `reason=` keyword argument is
present AND not a bare `None` literal. A site "fixed" as `reason=None` FAILS
here. It also asserts the by-design floor sites remain reason-less, so we don't
over-fix and fabricate a reason where none is classified.

Authoritative site audit (ground-truthed 2026-07-12), 10 sites total:
  THREAD (knowable reason): 1241 rate_limit · 1991 content_policy_blocked ·
                            3935 classified.reason · 4144 classified.reason
  ALREADY THREADED:         1820 · 3386 · 3422
  FLOOR (no classification): 1587 · 1660 · 5246
"""

import ast
import pathlib

import pytest

_AGENT = pathlib.Path(__file__).resolve().parents[2] / "agent"
# 2026-10-01 parity sync: upstream extracted the conversation loop into ``agent/turn_*.py``;
# the failover call sites moved with it. The gate follows the sites, keyed by (file, line).
_LOOP_FAMILY = sorted(_AGENT.glob("turn_*.py")) + [_AGENT / "conversation_loop.py"]


def _fallback_calls():
    """Return {(path, lineno): ast.Call} for every `agent._try_activate_fallback(...)`
    call in the extracted loop family, keyed by file and line number."""
    calls = {}
    for path in _LOOP_FAMILY:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "_try_activate_fallback"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "agent"
            ):
                calls[(path, node.lineno)] = node
    return calls


def _reason_kw(call: ast.Call):
    """Return the `reason=` keyword node, or None if absent."""
    for kw in call.keywords:
        if kw.arg == "reason":
            return kw
    return None


def _is_bare_none(kw) -> bool:
    """True if the reason= arg is the literal `None` (an effectively-bare 'fix')."""
    return kw is not None and isinstance(kw.value, ast.Constant) and kw.value.value is None


# The four sites that MUST thread a real reason. We key on a nearby structural
# anchor (a distinctive nearby literal) rather than a bare line number so the
# test survives small line drift in the file.
_THREAD_ANCHORS = {
    # upstream reworded the Nous guard's log line; the user-facing message is the stable anchor
    "nous_rate_limit_guard": "hit its rate limit; it resets in",
    "content_filter_stream_kill": "Content filter terminated stream",
    "client_error_should_fallback": "Non-retryable error (HTTP",
    "retry_exhaustion_floor": "Max retries ({max_retries}) exhausted",
    "safety_refusal": "(safety refusal) — trying fallback",
    # t_5d79bfea: a bridge 409 tui_busy that outlived its one same-route wait.
    "session_busy": "Bridge session still busy — switching to fallback",
}


def _lineno_after_anchor(anchor: str):
    """Find the first `_try_activate_fallback` call AFTER the anchor string (same file)."""
    for path in _LOOP_FAMILY:
        src = path.read_text().splitlines()
        anchor_line = next((i for i, line in enumerate(src, start=1) if anchor in line), None)
        if anchor_line is None:
            continue
        calls = sorted(ln for (p, ln) in _fallback_calls() if p == path)
        for ln in calls:
            if ln >= anchor_line:
                return (path, ln)
        raise AssertionError(f"no fallback call after anchor {anchor!r} in {path.name}")
    raise AssertionError(f"anchor not found: {anchor!r}")


class TestKnowableReasonSitesThread:
    """Each of the 4 knowable-reason sites passes a non-None reason= (RC1 effect gate)."""

    @pytest.mark.parametrize("name,anchor", list(_THREAD_ANCHORS.items()))
    def test_site_threads_non_none_reason(self, name, anchor):
        ln = _lineno_after_anchor(anchor)
        call = _fallback_calls()[ln]
        kw = _reason_kw(call)
        assert kw is not None, (
            f"{name} ({ln[0].name}:{ln[1]}): _try_activate_fallback called BARE — "
            f"must pass reason= (the classified/known FailoverReason)"
        )
        assert not _is_bare_none(kw), (
            f"{name} ({ln[0].name}:{ln[1]}): reason=None is an effectively-bare 'fix' — "
            f"pass the real classified reason, not the None literal"
        )


class TestFloorSitesStayReasonless:
    """The 3 by-design floor sites carry NO reason (don't over-fix / fabricate).

    These are the empty/invalid/empty-response eager fallbacks where no
    classification exists at the call site; they resolve to the honest
    "connection issue" floor via _resolve_failover_reason, and must not be
    handed a fabricated reason.
    """

    _FLOOR_ANCHORS = {
        "empty_malformed_eager": "Empty/malformed response — switching to fallback",
        "invalid_response_exhaustion": "for invalid responses — trying fallback",
        "empty_responses_switch": "Model returning empty responses",
    }

    @pytest.mark.parametrize("name,anchor", list(_FLOOR_ANCHORS.items()))
    def test_floor_site_has_no_reason(self, name, anchor):
        ln = _lineno_after_anchor(anchor)
        call = _fallback_calls()[ln]
        kw = _reason_kw(call)
        assert kw is None, (
            f"{name} ({ln[0].name}:{ln[1]}): floor site should NOT pass a reason= — "
            f"no classification exists here; let it fall to the 'connection issue' floor"
        )


class TestSiteCountReconciles:
    """There are exactly 13 fallback call sites across the extracted loop family (RC3
    reconciliation; re-reconciled 2026-10-01: the fork's 10 + upstream's incomplete_response
    continuation site and the refusal site that the extraction split out of the shared branch;
    2026-10-07 t_5d79bfea: + the session_busy site, which threads its reason)."""

    _EXPECTED = 13

    def test_ten_sites(self):
        found = sorted(f"{p.name}:{ln}" for (p, ln) in _fallback_calls())
        assert len(found) == self._EXPECTED, (
            f"expected {self._EXPECTED} _try_activate_fallback sites, found {len(found)} — "
            f"the audit table in the PR must be re-reconciled: {found}"
        )


class TestOverloadedFailoverAnnouncesReasonE2E:
    """AC1 EFFECT gate: a real `_try_activate_fallback(reason=overloaded)` on a
    real AIAgent emits an announce whose text contains `(provider overloaded)`
    — the exact live symptom (2026-07-11 21:14, a 503 `no eligible sub` that
    exhausted retries and announced a BARE line). This drives the real failover
    path (client swap, identity rewrite, announce emit) with only the provider
    client construction mocked, and captures what reaches the gateway
    `status_callback`.
    """

    def _make_agent_with_sink(self, fallback_model):
        from unittest.mock import MagicMock, patch
        from run_agent import AIAgent

        captured = []
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=fallback_model,
                status_callback=lambda kind, msg: captured.append((kind, msg)),
            )
            agent.client = MagicMock()
        return agent, captured

    def _mock_client(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.base_url = "https://api-proxy.example/anthropic"
        m.api_key = "fb-key"
        return m

    def test_overloaded_failover_announces_provider_overloaded(self):
        from unittest.mock import patch
        from agent.error_classifier import FailoverReason

        fbs = [{"provider": "claude-apx-1", "model": "claude-opus-4-8"}]
        agent, captured = self._make_agent_with_sink(fallback_model=fbs)
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(self._mock_client(), "resolved"),
            ),
            # Force the chat announce on regardless of ambient config.
            patch(
                "hermes_cli.config.read_raw_config",
                return_value={"model": {"announce_route_change": True}},
            ),
        ):
            activated = agent._try_activate_fallback(reason=FailoverReason.overloaded)

        assert activated is True, "failover should activate against a configured chain"
        announce = " || ".join(m for _, m in captured)
        assert "provider overloaded" in announce, (
            f"retry-exhausted overloaded failover must announce '(provider overloaded)'; "
            f"captured sink was: {captured!r}"
        )
        assert "🔄 Model fallback (provider overloaded):" in announce, (
            f"announce must carry the reason rider in the verb segment; got: {captured!r}"
        )

    def test_unmapped_classified_reason_renders_connection_issue_floor(self):
        # A CLASSIFIED-but-unmapped reason (e.g. FailoverReason.unknown) must
        # render the honest '(connection issue)' floor — never a bare line.
        # (This is the floor's real domain: a classified fault we can't name
        # precisely, NOT a genuine no-op call.)
        from unittest.mock import patch
        from agent.error_classifier import FailoverReason

        fbs = [{"provider": "claude-apx-1", "model": "claude-opus-4-8"}]
        agent, captured = self._make_agent_with_sink(fallback_model=fbs)
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(self._mock_client(), "resolved"),
            ),
            patch(
                "hermes_cli.config.read_raw_config",
                return_value={"model": {"announce_route_change": True}},
            ),
        ):
            agent._try_activate_fallback(reason=FailoverReason.unknown)

        announce = " || ".join(m for _, m in captured)
        assert "connection issue" in announce, (
            f"a classified-but-unmapped reason must render the '(connection issue)' "
            f"floor, never a bare line; captured: {captured!r}"
        )

    def test_genuine_noop_call_stays_bare_by_design(self):
        # Contract check (PR #280): a GENUINE no-op failover (reason=None AND no
        # pending stamp) renders a bare line — this is intended, not a
        # regression. The floor is for CLASSIFIED faults; a true no-reason call
        # carries no suffix. The 3 by-design floor sites (empty/invalid response)
        # legitimately land here when no stamp exists.
        from unittest.mock import patch

        fbs = [{"provider": "claude-apx-1", "model": "claude-opus-4-8"}]
        agent, captured = self._make_agent_with_sink(fallback_model=fbs)
        setattr(agent, "_pending_stream_error_reason", None)
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(self._mock_client(), "resolved"),
            ),
            patch(
                "hermes_cli.config.read_raw_config",
                return_value={"model": {"announce_route_change": True}},
            ),
        ):
            agent._try_activate_fallback()  # genuine no-op: no reason, no stamp

        announce = " || ".join(m for _, m in captured)
        # Bare verb segment: no '(...)' suffix before the ':' — but still a real
        # route-change line, never silent.
        assert "🔄 Model fallback:" in announce
        verb_segment = announce.split(":")[0]
        assert "(" not in verb_segment, (
            f"a genuine no-op must render a bare verb segment (no reason suffix); "
            f"got: {captured!r}"
        )
