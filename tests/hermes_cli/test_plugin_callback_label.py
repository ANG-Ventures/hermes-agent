"""Plugin callback identities must not render callback state into model-facing text.

``getattr(cb, "__name__", repr(cb))`` falls through to ``repr()`` for any callable without a
``__name__`` — and ``functools.partial``'s repr renders every bound argument. When a
``pre_tool_call`` guard raises, that label goes into the block directive the MODEL receives as
the tool result, so a credential bound into the callback is disclosed.
"""

from __future__ import annotations

import functools

from hermes_cli.plugins import PluginManager

_PLANTED = "hunter2PRODSup3rSecret"


def _policy(token, **kwargs):
    raise RuntimeError("policy engine unavailable")


def test_raising_partial_guard_does_not_disclose_bound_args():
    callback = functools.partial(_policy, _PLANTED)
    assert not hasattr(callback, "__name__") and _PLANTED in repr(callback)  # the premise

    mgr = PluginManager()
    mgr._hooks["pre_tool_call"] = [callback]
    results = mgr.invoke_hook("pre_tool_call", tool_name="web_search", args={"query": "x"})

    blocks = [r for r in results if isinstance(r, dict) and r.get("action") == "block"]
    assert len(blocks) == 1, "a guard that raised must still fail closed"
    assert _PLANTED not in blocks[0]["message"]
    assert "_policy" in blocks[0]["message"], "the guard must stay identifiable"


def test_callback_label_never_uses_repr():
    from hermes_cli.plugins_dispatch import _callback_label as callback_label

    class _Guard:
        def __init__(self, token):
            self.token = token

        def __call__(self, **kwargs):
            return None

        def __repr__(self):
            return f"_Guard(token={self.token!r})"

    def named(**kwargs):
        return None

    assert callback_label(named).endswith("named")
    assert callback_label(functools.partial(_policy, _PLANTED)) == "partial(_policy)"
    anon = callback_label(_Guard(_PLANTED))
    assert _PLANTED not in anon and anon.startswith("<_Guard#")
    assert anon == callback_label(_Guard("other")), "the fallback must not depend on instance state"
