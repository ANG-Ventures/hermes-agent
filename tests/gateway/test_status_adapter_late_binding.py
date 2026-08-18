"""Late-binding of the gateway status/side-channel adapter (2026-08-05 incident).

The adapter object snapshotted at turn start can be REPLACED mid-turn by the
platform reconnect watcher (Discord ws ``ack_stale`` → ``self.adapters[platform]``
now holds a NEW adapter object). Closures that captured the turn-start snapshot
then send through the dead adapter and the message is silently dropped — a
``🔄 Model fallback (safety refusal)`` announce generated 80s after a Discord
reconnect never reached the channel.

Two gates:

1. BEHAVIORAL — ``_adapter_for_source`` must resolve the CURRENT registry
   adapter when the source's retained transport ref points at a replaced
   (stale) adapter object. This is the substrate the per-send late-bind
   (``_current_status_adapter``) relies on.
2. STRUCTURAL (AST) — no status/side-channel closure inside
   ``GatewayRunner._run_agent`` may reference the stale ``_status_adapter``
   snapshot directly; they must route through ``_current_status_adapter``.
   Asserted on the real AST so a merge that reverts the late-binding in ANY
   one closure fails the gate (fix the class, not the site).
"""

import ast
import inspect
import textwrap
import types
import weakref


# ── 1. Behavioral: stale transport ref falls through to the live registry ──


class _FakeAdapter:
    platform = None


def _make_runner_with(adapters):
    from gateway.authz_mixin import GatewayAuthorizationMixin

    class _Runner(GatewayAuthorizationMixin):
        def __init__(self):
            self.adapters = adapters
            self._profile_adapters = {}

    return _Runner()


def _make_source(platform, adapter_ref):
    src = types.SimpleNamespace()
    src.platform = platform
    src.chat_id = "123"
    src.thread_id = None
    src.profile = None
    src.delivered_via_upstream_relay = False
    src._transport_adapter_ref = adapter_ref
    return src


def test_adapter_for_source_resolves_replacement_after_reconnect():
    """A source retaining a ref to the PRE-reconnect adapter must resolve to
    the NEW adapter now in the registry — not the stale object, not None."""
    from gateway.config import Platform

    stale = _FakeAdapter()
    fresh = _FakeAdapter()
    runner = _make_runner_with({Platform.DISCORD: fresh})

    source = _make_source(Platform.DISCORD, weakref.ref(stale))

    resolved = runner._adapter_for_source(source)
    assert resolved is fresh, (
        "expected the live registry adapter after a mid-turn reconnect "
        f"replacement; got {resolved!r}"
    )


def test_adapter_for_source_still_prefers_registered_transport():
    """Control: when the retained transport adapter IS still the registered
    one, it is preferred (no behavior change for the healthy path)."""
    from gateway.config import Platform

    live = _FakeAdapter()
    runner = _make_runner_with({Platform.DISCORD: live})
    source = _make_source(Platform.DISCORD, weakref.ref(live))

    assert runner._adapter_for_source(source) is live


# ── 2. Structural: side-channel closures must not capture the snapshot ──


# Closures inside _run_agent that deliver to the status side-channel. Every
# one of these fires from the agent worker thread at an arbitrary point in a
# potentially hours-long turn — exactly when a reconnect may have replaced
# the adapter.
_SIDE_CHANNEL_CLOSURES = {
    "_status_callback_sync",
    "_interim_assistant_cb",
    "_notice_callback_sync",
    "_deliver_bg_review_message",
    "_bg_review_send",
    "_clarify_callback_sync",
    "_approval_notify_sync",
}


def _find_defs(tree, names):
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found[node.name] = node
    return found


def test_side_channel_closures_late_bind_the_adapter():
    import gateway.run as run_mod

    tree = ast.parse(inspect.getsource(run_mod))
    defs = _find_defs(tree, _SIDE_CHANNEL_CLOSURES | {"_current_status_adapter"})

    assert "_current_status_adapter" in defs, (
        "_current_status_adapter (the per-send late-bind helper) is missing "
        "from gateway/run.py — the stale-adapter fix was reverted"
    )

    missing = _SIDE_CHANNEL_CLOSURES - set(defs)
    assert not missing, f"expected closures not found (renamed?): {missing}"

    offenders = {}
    for name in sorted(_SIDE_CHANNEL_CLOSURES):
        node = defs[name]
        stale_refs = [
            n.lineno
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id == "_status_adapter"
        ]
        if stale_refs:
            offenders[name] = stale_refs

    assert not offenders, (
        "side-channel closures reference the turn-start _status_adapter "
        "snapshot directly — a mid-turn platform reconnect replaces that "
        "object and sends through it are silently dropped. Route through "
        f"_current_status_adapter() instead. Offenders: {offenders}"
    )


def test_extracted_status_callback_late_binds_the_adapter():
    """The TurnRunner extraction must not reopen the stale snapshot path."""
    from gateway.run import TurnRunner

    tree = ast.parse(textwrap.dedent(inspect.getsource(TurnRunner._status_callback_sync)))
    attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert "_current_status_adapter" in attrs
    assert "_status_adapter" not in attrs, (
        "TurnRunner._status_callback_sync bypasses the per-send adapter resolver "
        "and can send failover announcements through a replaced adapter"
    )
