"""Gateway session PROFILE attribution (sibling of the session-id attribution bug).

`_set_session_vars_for_source` bound `profile=getattr(source, "profile", "") or ""`.
A SessionSource commonly carries no profile attribute, so on a gateway launched
with `--profile athena` the bound `HERMES_SESSION_PROFILE` was `""` — not
"athena". Every consumer that resolves identity from it (papercut ledger
attribution, delegate_tool, kanban_tools) then silently fell back to the DEFAULT
profile's identity, mis-attributing a sibling agent's work to the default agent.

The fix: a per-source profile still wins (multiplexed serving binds each inbound
to its owning profile), but an absent one falls back to the profile THIS gateway
was actually launched with, via the existing `_active_profile_name()` helper.
"""

import pytest

import gateway.session_context as sc
from gateway.config import Platform
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _clean():
    """Reset the profile contextvar around each test so bindings never leak."""
    sc._SESSION_PROFILE.set(sc._UNSET)
    yield
    sc._SESSION_PROFILE.set(sc._UNSET)


def _runner(active_profile="athena"):
    """A bare GatewayRunner (object.__new__, no __init__) like the sibling suites."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._active_profile_name = lambda: active_profile
    return runner


def _source(profile=None):
    kwargs = dict(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )
    src = SessionSource(**kwargs)
    if profile is not None:
        src.profile = profile
    return src


def _bind_and_read(runner, source):
    tokens = runner._set_session_vars_for_source(
        source=source, session_key="k", session_id="s", message_id="m"
    )
    try:
        return sc.get_session_env("HERMES_SESSION_PROFILE", "")
    finally:
        sc.clear_session_vars(tokens)


def test_absent_source_profile_falls_back_to_gateway_launch_profile():
    """THE BUG: a source with no profile must bind the gateway's real profile,
    not "" (which downstream resolves as the default agent's identity)."""
    runner = _runner(active_profile="athena")
    assert _bind_and_read(runner, _source()) == "athena"


def test_empty_source_profile_also_falls_back():
    """An explicitly-empty profile is the same defect as an absent one."""
    runner = _runner(active_profile="daedalus")
    assert _bind_and_read(runner, _source(profile="")) == "daedalus"


def test_explicit_source_profile_still_wins():
    """Multiplex correctness: when the inbound carries its owning profile, that
    MUST win over the gateway's own launch profile — the fallback is a fallback."""
    runner = _runner(active_profile="athena")
    assert _bind_and_read(runner, _source(profile="momus")) == "momus"


def test_resolver_failure_degrades_to_empty_not_crash():
    """A broken/absent resolver must not break session binding (fail-soft)."""
    runner = _runner()

    def _boom():
        raise RuntimeError("no profile machinery")

    runner._active_profile_name = _boom
    assert _bind_and_read(runner, _source()) == ""


def test_bare_runner_resolves_real_profile_machinery():
    """A runner that hasn't stubbed the helper still binds a real profile name
    (the class method resolves the live profile, defaulting to "default") —
    never "", which was the bug."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    # no stub: exercises the REAL _active_profile_name method
    bound = _bind_and_read(runner, _source())
    assert bound, "profile must never bind empty when a resolver exists"
    assert bound == "default"
