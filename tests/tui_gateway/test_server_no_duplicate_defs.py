"""Regression locks for the merge-introduced duplicate module-level defs in
``tui_gateway/server.py``.

The 2026-07-23 upstream parity merge landed the compute-host helper cluster
TWICE (upstream's copy first, the fork's copy second). Python binds
last-def-wins, so the fork copy went live and every upstream fix in the earlier
copy became silently dead code:

* ``_send_compute_host_control`` lost its ``timeout`` parameter, so the
  ``session.compress`` call site — which passes ``timeout=120.0`` — raised
  ``TypeError`` and returned error 5019 on every isolated /compress.
* ``_submit_prompt_to_compute_host`` lost the ``send_failed`` early-return, so a
  synchronous pipe failure emitted a terminal error bubble to the client even
  though ``prompt.submit`` then fails open and re-runs the turn inline.

These tests exercise the helpers at the SUPERVISOR boundary (the real helper
signature is called for real, not monkeypatched away) and add a structural
contract so no future merge can re-introduce a duplicate module-level binding.
"""

import ast
import inspect
import threading
from pathlib import Path

import pytest

from tui_gateway import server


def _session(**extra):
    session = {
        "agent": None,
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "transport": None,
        "last_active": 0.0,
    }
    session.update(extra)
    return session


# ---------------------------------------------------------------------------
# Effect gates — the two dead upstream fixes, proven at the supervisor boundary
# ---------------------------------------------------------------------------


def test_session_compress_reaches_supervisor_with_the_120_second_budget(monkeypatch):
    """/compress under turn isolation must reach ``supervisor.control`` carrying
    the 120s budget.

    This drives the REAL ``_send_compute_host_control`` (only the supervisor
    underneath it is faked), so a helper whose signature cannot accept
    ``timeout`` fails here instead of being masked by a permissive
    ``*args, **kwargs`` stub.
    """
    controls = []

    class _Supervisor:
        def control(self, sid, *, route_name, payload=None, wait=True, timeout=30.0):
            controls.append(
                {"sid": sid, "route_name": route_name, "wait": wait, "timeout": timeout}
            )
            return {
                "type": "control.ack",
                "result": {
                    "status": "compressed",
                    "messages": [],
                    "removed": 0,
                    "summary": {"headline": "Already compressed", "noop": True},
                },
            }

    session = _session(_compute_host_active=True)
    server._sessions["compress-sid"] = session
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _Supervisor())

    try:
        resp = server.handle_request(
            {"id": "1", "method": "session.compress", "params": {"session_id": "compress-sid"}}
        )
    finally:
        server._sessions.pop("compress-sid", None)

    assert "error" not in resp, resp.get("error")
    assert resp["result"]["status"] == "compressed"
    assert controls == [
        {
            "sid": "compress-sid",
            "route_name": "session.compress",
            "wait": True,
            "timeout": 120.0,
        }
    ]


def test_send_compute_host_control_accepts_every_call_sites_keywords():
    """Every keyword any call site passes must exist on the live helper."""
    params = inspect.signature(server._send_compute_host_control).parameters
    for required in ("route_name", "command", "payload", "wait", "timeout"):
        assert required in params, f"live _send_compute_host_control lost `{required}`"


def test_synchronous_send_failure_does_not_emit_a_terminal_error_bubble(monkeypatch):
    """``submit_turn`` reports a synchronous pipe failure through ``on_complete``
    BEFORE re-raising (``host_supervisor.py``). ``prompt.submit`` fails open and
    re-runs the turn inline, so the callback must NOT tear the session down or
    emit a terminal error — otherwise the user sees "Error: broken pipe"
    followed by a perfectly good answer.
    """

    class _BrokenSupervisor:
        def submit_turn(self, frame, *, on_complete=None):
            if on_complete is not None:
                on_complete(
                    {
                        "type": "turn.error",
                        "request_id": frame["request_id"],
                        "reason": "send_failed",
                        "message": "broken pipe",
                    }
                )
            raise BrokenPipeError("broken pipe")

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append(event))
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _BrokenSupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda rid, sid, session: False)

    session = _session(running=True)
    resp = server._submit_prompt_to_compute_host("rid-1", "send-fail-sid", session, "hello")

    assert resp["error"]["code"] == 5019
    assert "message.complete" not in emitted, (
        "a send_failed callback emitted a terminal error bubble; prompt.submit "
        "fails open and re-runs the turn inline, so the client would show a "
        f"spurious error. emitted={emitted}"
    )


def test_dispatch_failure_releases_the_running_flag():
    """A dispatch failure must clear ``running`` so the session isn't wedged."""

    class _BrokenSupervisor:
        def submit_turn(self, frame, *, on_complete=None):
            raise BrokenPipeError("broken pipe")

    session = _session(running=True)
    original = server._get_compute_host_supervisor
    server._get_compute_host_supervisor = lambda _cfg=None: _BrokenSupervisor()
    try:
        resp = server._submit_prompt_to_compute_host("rid-2", "wedge-sid", session, "hello")
    finally:
        server._get_compute_host_supervisor = original

    assert resp["error"]["code"] == 5019
    assert session["running"] is False


def test_inflight_turn_is_registered_and_cleared_around_a_compute_host_turn(monkeypatch):
    """The auto-resume inflight registry must be populated on submit and drained
    on completion (the fork-side behavior that must survive the dedup)."""
    completions = {}

    def _is_registered(sid: str) -> bool:
        with server._compute_host_inflight_turns_lock:
            return sid in server._compute_host_inflight_turns

    class _Supervisor:
        def submit_turn(self, frame, *, on_complete=None):
            completions["cb"] = on_complete
            completions["sid_registered"] = _is_registered("inflight-sid")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _Supervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda rid, sid, session: False)
    monkeypatch.setattr(server, "_apply_compute_host_metadata_mirror", lambda session, frame: None)
    monkeypatch.setattr(server, "_session_info", lambda agent, session=None: {})

    session = _session(running=True)
    try:
        resp = server._submit_prompt_to_compute_host("rid-3", "inflight-sid", session, "hello")
        assert "error" not in resp, resp.get("error")
        assert completions["sid_registered"] is True

        completions["cb"]({"type": "turn.end", "sid": "inflight-sid", "request_id": "rid-3"})
        assert _is_registered("inflight-sid") is False
    finally:
        with server._compute_host_inflight_turns_lock:
            server._compute_host_inflight_turns.pop("inflight-sid", None)


# ---------------------------------------------------------------------------
# Structural contract — no module-level name may be bound twice
# ---------------------------------------------------------------------------

SERVER_PATH = Path(server.__file__)


def _module_level_bindings(source: str) -> dict[str, list[tuple[str, int]]]:
    """Map every module-level bound name -> [(kind, lineno), ...].

    Covers defs, async defs, classes, and plain assignments — the full
    shadowing surface. Route handlers are deliberately all named ``_`` (the
    ``@method("...")`` registration idiom), so ``_`` is not a duplicate
    binding; its uniqueness contract is the route string, checked separately.
    """
    bindings: dict[str, list[tuple[str, int]]] = {}

    def add(name: str, kind: str, lineno: int) -> None:
        bindings.setdefault(name, []).append((kind, lineno))

    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node.name, type(node).__name__, node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add(target.id, "Assign", node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add(node.target.id, "AnnAssign", node.lineno)
    return bindings


def _string_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    legacy = getattr(ast, "Str", None)
    if legacy is not None and isinstance(node, legacy):
        return node.s
    return None


def test_no_module_level_name_is_bound_twice_in_server():
    """A merge that lands the same helper cluster twice is invisible at runtime
    (last-def-wins) but silently kills every fix in the earlier copy. Lock it.
    """
    bindings = _module_level_bindings(SERVER_PATH.read_text())
    offenders = {
        name: sites
        for name, sites in bindings.items()
        if name != "_" and len(sites) > 1
    }
    assert offenders == {}, (
        "duplicate module-level bindings in tui_gateway/server.py — the later "
        "definition wins and the earlier one is dead code: "
        + "; ".join(
            f"{name} at lines {[lineno for _kind, lineno in sites]}"
            for name, sites in sorted(offenders.items())
        )
    )


def test_no_rpc_route_is_registered_twice_in_server():
    """``_`` is bound ~138 times on purpose (the ``@method('route')`` idiom).
    What must be unique is the ROUTE STRING each one registers.
    """
    tree = ast.parse(SERVER_PATH.read_text())
    routes: dict[tuple[str, str], list[int]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name)):
                continue
            if not deco.args:
                continue
            route = _string_constant(deco.args[0])
            if route is not None:
                routes.setdefault((deco.func.id, route), []).append(node.lineno)

    assert routes, "found no decorator-registered routes — the scan is vacuous"
    duplicates = {key: lines for key, lines in routes.items() if len(lines) > 1}
    assert duplicates == {}, f"duplicate RPC route registrations: {duplicates}"


@pytest.mark.parametrize(
    "snippet",
    [
        "def _duplicate_helper():\n    return 1\n\n\ndef _duplicate_helper():\n    return 2\n",
        "_DUP_TABLE = {}\n\n_DUP_TABLE = {'a': 1}\n",
    ],
)
def test_duplicate_binding_scanner_has_teeth(snippet):
    """The scanner must actually flag a duplicate — not merely count sites."""
    bindings = _module_level_bindings(snippet)
    offenders = {n: s for n, s in bindings.items() if n != "_" and len(s) > 1}
    assert offenders, "scanner failed to detect an injected duplicate binding"
