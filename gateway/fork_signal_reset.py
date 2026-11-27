"""Detach a forked child from the gateway's asyncio signal plumbing (t_5e351ab7).

``loop.add_signal_handler`` points ``signal.set_wakeup_fd`` at the loop's self-pipe and installs
a no-op C-level handler. A child forked without exec (``preexec_fn`` spawns, ``pty.fork``, a bare
``os.fork``) inherits both, so on CPython < 3.12 a SIGTERM sent to the CHILD writes its signal byte
into the PARENT's self-pipe and the gateway runs its own shutdown (2026-11-11 19:16 PST: watchdog
reaped a gateway fork, gateway drained and restarted). CPython 3.12+ resets the wakeup fd after
fork; it still leaves the no-op handler, so such a child ignores SIGTERM.

The after-fork hook here does both, in the child only: wakeup fd off, and every signal whose
handler is asyncio's no-op back to its default (SIGINT to ``default_int_handler``), after unwinding
``gateway.host_rendezvous``'s SIGTERM cleanup wrapper and its role/lock state. Each step is
guarded, so a failure never breaks the fork. Kill switch: ``HERMES_GATEWAY_FORK_SIGNAL_RESET=0``.
"""
from __future__ import annotations

import os
import signal
import sys

KILL_SWITCH_ENV = "HERMES_GATEWAY_FORK_SIGNAL_RESET"
_registered = False


def _asyncio_noop():
    try:
        from asyncio import unix_events
        return getattr(unix_events, "_sighandler_noop", None)
    except Exception:
        return None


def reset_signal_plumbing_in_child() -> None:
    """Run in the forked child: wakeup fd off, asyncio no-op handlers back to default."""
    try:
        signal.set_wakeup_fd(-1)
    except Exception:
        pass
    # host_rendezvous's SIGTERM wrapper sits on top of the no-op and runs the parent's role
    # cleanup; unwind it first (t_89c3a411). Only when already imported: never import after fork.
    rendezvous = sys.modules.get("gateway.host_rendezvous")
    if rendezvous is not None:
        try:
            rendezvous.forget_owner_state_in_forked_child()
        except Exception:
            pass
    noop = _asyncio_noop()
    if noop is None:
        return
    for sig in getattr(signal, "valid_signals", lambda: ())():
        try:
            if signal.getsignal(sig) is noop:
                signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT else signal.SIG_DFL)
        except Exception:
            pass


def install_fork_signal_reset() -> bool:
    """Register the after-fork hook once per process. False when skipped (kill switch, no fork)."""
    global _registered
    if _registered:
        return True
    if os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return False
    if not hasattr(os, "register_at_fork"):
        return False
    os.register_at_fork(after_in_child=reset_signal_plumbing_in_child)
    _registered = True
    return True
