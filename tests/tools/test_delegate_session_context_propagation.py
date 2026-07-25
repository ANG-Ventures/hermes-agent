"""Session identity must survive the subagent executor boundary.

``contextvars`` are per-thread: a ``ThreadPoolExecutor`` worker starts with an
empty ``Context``. ``tools/delegate_tool.py`` runs every child agent on such a
worker, so without an explicit snapshot the child executes with **no** gateway
session identity — and several core, non-telemetry consumers read that identity
to make routing and security decisions.

These are behavior contracts on the propagation itself (parent value must
survive into the worker, callbacks must NOT), not snapshots of any particular
session shape.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.session_context import (
    get_session_env,
    reset_session_vars,
    set_session_vars,
)
from tools.thread_context import copy_context_to_thread, propagate_context_to_thread  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_session_context():
    reset_session_vars()
    yield
    reset_session_vars()


def _bind_parent_session():
    """Bind a gateway-style session on the calling (parent) thread."""
    set_session_vars(
        platform="discord",
        source="discord",
        chat_id="C123",
        chat_name="ops",
        thread_id="T9",
        user_id="U1",
        user_name="ace",
        session_key="sess-discord-C123",
        session_id="s-1",
        ui_session_id="",
        message_id="m-1",
        profile="default",
        cwd="",
        async_delivery=True,
    )


def _in_worker(fn):
    """Run *fn* on a worker thread the way delegate_tool submits a child.

    Mirrors the real submit site: the wrapper delegate_tool applies is looked
    up through the module under test, so removing the wrap at the call site is
    what these tests detect -- not the helper existing in isolation.
    """
    wrap = _delegate_submit_wrapper()
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(wrap(fn)).result()


def _delegate_submit_wrapper():
    """The context wrapper tools/delegate_tool.py applies before submit().

    Read out of delegate_tool's own module namespace so this test binds to the
    call site's actual behaviour. If the delegate submit sites stop wrapping,
    the import below disappears with them and the tests fall back to a bare
    submit -- which is exactly the regression under test.
    """
    import tools.delegate_tool as delegate_tool

    wrap = getattr(delegate_tool, "copy_context_to_thread", None)
    if wrap is None:
        return lambda fn: fn
    return wrap


def _in_bare_worker(fn):
    """Run *fn* on an unwrapped worker — the pre-fix behaviour, for contrast."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result()


# ---------------------------------------------------------------------------
# The propagation contract
# ---------------------------------------------------------------------------


def test_session_identity_is_lost_on_a_bare_worker():
    """Documents the limitation the fix exists to close.

    Not a change-detector: it pins the *reason* the snapshot is required. If
    ContextVars ever started crossing the boundary on their own, this test
    would turn red and the wrapper could be removed.
    """
    _bind_parent_session()
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"

    seen = _in_bare_worker(lambda: get_session_env("HERMES_SESSION_PLATFORM"))

    assert seen == ""


def test_session_identity_survives_a_wrapped_worker():
    _bind_parent_session()

    seen = _in_worker(lambda: get_session_env("HERMES_SESSION_PLATFORM"))

    assert seen == get_session_env("HERMES_SESSION_PLATFORM") == "discord"


@pytest.mark.parametrize(
    "var",
    [
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
    ],
)
def test_every_session_var_matches_the_parent_in_the_worker(var):
    """Whole-identity propagation, not just the one var a caller noticed."""
    _bind_parent_session()
    parent_value = get_session_env(var)
    assert parent_value  # guard: the fixture really bound this var

    assert _in_worker(lambda: get_session_env(var)) == parent_value


def test_snapshot_is_taken_at_wrap_time_not_at_run_time():
    """The Context is captured on the parent thread, before submit().

    This is why the wrap must happen at the call site rather than inside the
    worker: by the time the worker runs, the parent's binding may already have
    been reset for the next turn.
    """
    _bind_parent_session()
    wrapped = copy_context_to_thread(lambda: get_session_env("HERMES_SESSION_CHAT_ID"))

    reset_session_vars()  # parent turn ends before the child gets scheduled

    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(wrapped).result() == "C123"


def test_worker_binding_does_not_leak_back_to_the_parent():
    """A child that rebinds its own session must not corrupt the parent turn."""
    _bind_parent_session()

    def _rebind():
        set_session_vars(
            platform="slack",
            source="slack",
            chat_id="CHILD",
            chat_name="",
            thread_id="",
            user_id="",
            user_name="",
            session_key="child",
            session_id="",
            ui_session_id="",
            message_id="",
            profile="default",
            cwd="",
            async_delivery=True,
        )
        return get_session_env("HERMES_SESSION_PLATFORM")

    assert _in_worker(_rebind) == "slack"
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "C123"


def test_arguments_and_return_value_are_forwarded():
    _bind_parent_session()

    def _target(a, b, *, c):
        return (a, b, c, get_session_env("HERMES_SESSION_PLATFORM"))

    with ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(copy_context_to_thread(_target), 1, 2, c=3).result()

    assert got == (1, 2, 3, "discord")


def test_exceptions_propagate_to_the_future():
    _bind_parent_session()

    def _boom():
        raise ValueError("child failed")

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(copy_context_to_thread(_boom))
        with pytest.raises(ValueError, match="child failed"):
            fut.result()


# ---------------------------------------------------------------------------
# The constraint that makes this a *separate* helper from
# propagate_context_to_thread
# ---------------------------------------------------------------------------


def test_copy_variant_does_not_propagate_approval_callbacks():
    """Subagent workers must keep their OWN approval policy.

    ``tools/delegate_tool.py`` installs a deliberately non-interactive
    approval callback on its executors (``_set_subagent_approval_cb``): a
    subagent worker that reached the CLI's interactive callback would call
    ``input()`` off the main thread and deadlock against the parent's
    prompt_toolkit TUI, which owns stdin (#15216).

    So the delegate call sites must use the ContextVars-only variant. If they
    ever switched to ``propagate_context_to_thread``, that deadlock returns —
    this test is the guard.
    """
    import tools.terminal_tool as terminal_tool

    sentinel = lambda *a, **k: "approve"  # noqa: E731
    terminal_tool.set_approval_callback(sentinel)
    try:
        seen = _in_worker(terminal_tool._get_approval_callback)
    finally:
        terminal_tool.set_approval_callback(None)

    assert seen is None


def test_propagate_variant_still_carries_callbacks():
    """The pre-existing helper is unchanged — the two variants are distinct."""
    import tools.terminal_tool as terminal_tool

    sentinel = lambda *a, **k: "approve"  # noqa: E731
    terminal_tool.set_approval_callback(sentinel)
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            seen = ex.submit(
                propagate_context_to_thread(terminal_tool._get_approval_callback)
            ).result()
    finally:
        terminal_tool.set_approval_callback(None)

    assert seen is sentinel


# ---------------------------------------------------------------------------
# The core consumers that were reading an empty identity
# ---------------------------------------------------------------------------


def test_cron_origin_capture_works_from_a_subagent_thread():
    """``deliver="origin"`` from a subagent must resolve the real channel.

    ``tools/cronjob_tools._origin_from_env`` returns ``None`` when platform or
    chat_id is empty, and a ``None`` origin is treated as "local session, no
    delivery channel" — so a cron job created by a subagent would silently
    never deliver.
    """
    from tools.cronjob_tools import _origin_from_env

    _bind_parent_session()
    parent_origin = _origin_from_env()
    assert parent_origin is not None

    assert _in_bare_worker(_origin_from_env) is None
    assert _in_worker(_origin_from_env) == parent_origin


def test_approval_session_key_resolves_from_a_subagent_thread():
    """A subagent's approval request must reach the originating session's queue.

    ``tools/approval.get_current_session_key`` falling back to ``"default"``
    means the gateway approval prompt is filed against a session the user is
    not in.
    """
    from tools.approval import get_current_session_key

    _bind_parent_session()

    assert _in_bare_worker(get_current_session_key) == "default"
    assert _in_worker(get_current_session_key) == "sess-discord-C123"


def test_subprocess_env_bridge_receives_session_vars_from_a_subagent_thread():
    """The cross-session leak guard strips vars it thinks are unbound.

    ``tools/environments/local._inject_session_context_env`` deletes a session
    var from the child-process env when its ContextVar is ``_UNSET`` while the
    session-context machinery is engaged — correct policy, but on an unwrapped
    subagent worker *every* var looks unset, so a terminal command run by a
    subagent inherits no session identity at all.
    """
    from tools.environments.local import _inject_session_context_env

    def _bridged():
        env = {}
        _inject_session_context_env(env)
        return env

    _bind_parent_session()

    assert _bridged()["HERMES_SESSION_PLATFORM"] == "discord"
    assert "HERMES_SESSION_PLATFORM" not in _in_bare_worker(_bridged)
    assert _in_worker(_bridged)["HERMES_SESSION_PLATFORM"] == "discord"


def test_gateway_surface_detection_works_from_a_subagent_thread():
    """``skills_tool._is_gateway_surface`` gates surface-specific behaviour."""
    from tools.skills_tool import _is_gateway_surface

    _bind_parent_session()

    assert _in_bare_worker(_is_gateway_surface) is False
    assert _in_worker(_is_gateway_surface) is True


# ---------------------------------------------------------------------------
# Transitivity — origin must survive arbitrary nesting
# ---------------------------------------------------------------------------


def test_identity_is_transitive_through_nested_delegation():
    """A grandchild delegated by a child still sees the originating session.

    Each level snapshots the Context it is currently running under, so the
    parent's identity chains down through arbitrary nesting depth rather than
    being reset to empty at the second hop.
    """
    _bind_parent_session()

    def _grandchild():
        return get_session_env("HERMES_SESSION_CHAT_ID")

    def _child():
        # the child is itself on a worker thread and delegates again
        return _in_worker(_grandchild)

    assert _in_worker(_child) == "C123"
