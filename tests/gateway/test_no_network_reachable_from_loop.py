# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""Ratchet: no gateway ``async def`` may transitively REACH a blocking network call.

INCIDENT (2026-09-24, Apollo).  Discord ``/model claude-bpx-N/...`` answered
"The application did not respond" (5 ``interaction expired before defer`` in
one night), and the loop logged::

    PHASE=event_loop_blocked platform=discord seconds=10
        site=hermes_cli/urllib_security.py:217 open_credentialed_url

The chain was ten plain-``def`` frames below the coroutine, so neither the
lexical gate (``test_no_sync_syscalls_on_event_loop.py``) nor the atomic-write
reachability gate could see it::

    _handle_model_command                     (async def, gateway/slash_commands.py)
      _finish_switch                          (async def)
        _set_session_model_override           gateway/run.py  <- plain def
          _model_override_is_persistable
            _reresolve_model_override_credentials
              switch_model                    hermes_cli/model_switch.py
                validate_requested_model      hermes_cli/models.py
                  fetch_api_models -> probe_api_models
                    open_credentialed_url -> urllib ... socket.connect   <- 5 s timeout

The fix has two halves, and this module pins both:

  * ``_reresolve_model_override_credentials`` re-resolves a route that was
    ALREADY accepted, so it now calls ``switch_model(probe_catalog=False)`` --
    no live ``GET /v1/models`` probe and no models.dev fetch, ever;
  * the /model and /reset doors call ``_set_session_model_override`` via
    ``asyncio.to_thread`` (the ``/model reset`` door already did), because
    credential resolution itself can still refresh an OAuth token.

SCOPE.  Start nodes are coroutines under ``gateway/`` and ``plugins/platforms/``;
the walk crosses into ``hermes_cli/``, ``agent/``, ``tools/`` and ``run_agent.py``
because that is where every measured chain actually terminates.  Callee
resolution is the shared engine's (same-file, explicit ``from X import name``,
or tree-unique name) -- read ``_loop_atomic_write_reachability.py`` before
changing anything here.

RATCHET.  The graph over-approximates and the tree has pre-existing offenders
(most sit behind OAuth-refresh paths that only fire on an expired token), so
this is a frozen inventory, not a hard zero: a NEW (module, coroutine, sink)
triple fails, and a triple that disappears without the baseline being updated
also fails.  Fix one: offload it (``asyncio.to_thread``) or annotate the
coroutine ``# noqa: network-on-loop <reason>``, then delete its line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gateway._loop_atomic_write_reachability import (
    build_index,
    derive_scanned_modules,
    find_onloop_sink_sites,
    ratchet_key,
)

SCAN_ROOTS = (
    "gateway",
    "plugins/platforms",
    "utils.py",
    "hermes_cli",
    "agent",
    "tools",
    "run_agent.py",
)
START_ROOTS = ("gateway", "plugins/platforms")

# Anything that opens a socket and waits on it on the calling thread.
NETWORK_SINK_NAMES = frozenset({
    "urlopen",
    "open_credentialed_url",
    "create_connection",
})
NETWORK_SINK_DOTTED = frozenset({
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.head",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "httpx.request",
    "socket.create_connection",
    "request.urlopen",
})
NOQA_TOKEN = "# noqa: network-on-loop"

MIN_DERIVED_MODULES = 300

# Frozen 2026-09-24 when the gate landed. Pre-existing, NOT endorsed; tracked
# for burn-down on the follow-up card. The two /model coroutines this change
# fixed (_finish_switch, _on_model_selected) and /reset are deliberately absent.
REACHABLE_BASELINE = frozenset({
    "gateway/kanban_watchers.py _run_kanban_dispatcher -> urlopen",
    "gateway/platforms/api_server.py _handle_browser_control_ws -> httpx.get",
    "gateway/platforms/api_server.py _handle_model_options -> httpx.post",
    "gateway/platforms/api_server.py _handle_run_events -> httpx.get",
    "gateway/platforms/api_server.py _handle_runs -> urlopen",
    "gateway/platforms/api_server.py _handle_session_chat_stream -> httpx.get",
    "gateway/platforms/api_server.py _handle_toolsets -> httpx.post",
    "gateway/platforms/api_server.py _run_agent -> urlopen",
    "gateway/platforms/api_server.py _run_and_close -> urlopen",
    "gateway/platforms/api_server.py _write_sse_chat_completion -> httpx.get",
    "gateway/platforms/api_server.py _write_sse_responses -> httpx.get",
    "gateway/platforms/base.py _process_message_background -> httpx.get",
    "gateway/relay/media.py download -> urlopen",
    "gateway/relay/media.py upload -> urlopen",
    "gateway/run.py _handle_message_with_agent_admitted -> urlopen",
    "gateway/run.py _post_turn_goal_continuation -> requests.get",
    "gateway/run.py _prepare_inbound_message_text -> urlopen",
    "gateway/run.py _run_agent_admitted -> open_credentialed_url",
    "gateway/run.py _run_background_task_inner -> urlopen",
    "gateway/run.py _stop_impl -> requests.get",
    "gateway/run.py start -> urlopen",
    "gateway/run.py stop -> requests.get",
    "gateway/slash_commands.py _handle_btw_command -> urlopen",
    "gateway/slash_commands.py _handle_compress_command_inner -> urlopen",
    "gateway/slash_commands.py _handle_context_command -> requests.get",
    "gateway/slash_commands.py _handle_debug_command -> urlopen",
    "gateway/slash_commands.py _handle_merge_command -> httpx.get",
    "gateway/slash_commands.py _handle_refine_command -> httpx.get",
    "gateway/slash_commands.py _handle_review_command -> urlopen",
    "plugins/platforms/matrix/adapter.py send_model_picker -> requests.get",
})

# Coroutines this change took off the network path. They must stay off it.
FIXED_COROUTINES = (
    "gateway/slash_commands.py _finish_switch ",
    "gateway/slash_commands.py _on_model_selected ",
    "gateway/slash_commands.py _handle_model_command ",
    "gateway/slash_commands.py _handle_reset_command ",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sites(root: Path, modules) -> list[str]:
    return find_onloop_sink_sites(
        root,
        modules,
        sink_names=NETWORK_SINK_NAMES,
        sink_dotted=NETWORK_SINK_DOTTED,
        noqa_token=NOQA_TOKEN,
        start_roots=START_ROOTS,
    )


@pytest.fixture(scope="module")
def live_sites() -> list[str]:
    repo = _repo_root()
    return _sites(repo, derive_scanned_modules(repo, SCAN_ROOTS))


def test_scope_is_derived_and_reaches_the_incident_modules():
    repo = _repo_root()
    modules = derive_scanned_modules(repo, SCAN_ROOTS)
    assert len(modules) >= MIN_DERIVED_MODULES, (
        f"derived only {len(modules)} modules; the import walk is broken and "
        "this gate is vacuously green."
    )
    # Every frame of the 2026-09-24 chain must be in scope.
    for rel in (
        "gateway/slash_commands.py",
        "gateway/run.py",
        "hermes_cli/model_switch.py",
        "hermes_cli/models.py",
        "hermes_cli/urllib_security.py",
    ):
        assert rel in modules, f"{rel} fell out of the derived scan scope"


def test_reachable_network_calls_do_not_grow(live_sites):
    current = {ratchet_key(s) for s in live_sites}
    chains = {ratchet_key(s): s for s in live_sites}
    added = sorted(current - REACHABLE_BASELINE)
    removed = sorted(REACHABLE_BASELINE - current)
    assert not added, (
        "NEW gateway coroutine(s) can now reach a blocking network call. On "
        "2026-09-24 this class held the Discord loop for 10 s per /model and "
        "expired every slash interaction queued behind it.\n"
        "Offload the call (asyncio.to_thread) or, if it is genuinely "
        "loop-safe, annotate the coroutine `# noqa: network-on-loop <reason>`.\n"
        + "\n".join(f"  {chains[a]}" for a in added)
    )
    assert not removed, (
        "Baseline pair(s) no longer reachable -- good. Delete them from "
        "REACHABLE_BASELINE so the ratchet keeps them gone:\n"
        + "\n".join(f"  {r}" for r in removed)
    )


def test_the_model_switch_doors_never_reach_the_network(live_sites):
    live = [s for s in live_sites if s.startswith(FIXED_COROUTINES)]
    assert not live, (
        "a /model or /reset door reaches a blocking network call on the loop "
        "again (the 2026-09-24 'application did not respond' chain):\n"
        + "\n".join(f"  {s}" for s in live)
    )
    leaked = [k for k in REACHABLE_BASELINE if k.startswith(FIXED_COROUTINES)]
    assert not leaked, f"a fixed door was absorbed into the baseline: {leaked}"


def _reresolve_switch_model_calls(src: str) -> list:
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_reresolve_model_override_credentials":
            return [
                c for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "switch_model"
            ]
    raise AssertionError("_reresolve_model_override_credentials not found")


def _passes_probe_off(call) -> bool:
    import ast

    return any(
        kw.arg == "probe_catalog"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in call.keywords
    )


def test_reresolve_calls_switch_model_with_the_probe_off():
    """The re-resolution helper must never run the live catalog probe.

    It is reached on the loop from paths no ``to_thread`` at the /model door
    covers -- the lazy rehydrate inside ``_resolve_session_agent_runtime``
    (every inbound message after a restart) and ``/reset``'s route restore.
    The reachability walk is name-based and cannot see a kwarg, so the
    contract is pinned at the call site.
    """
    src = (_repo_root() / "gateway" / "run.py").read_text(encoding="utf-8")
    calls = _reresolve_switch_model_calls(src)
    assert calls, "_reresolve_model_override_credentials no longer calls switch_model"
    bad = [c.lineno for c in calls if not _passes_probe_off(c)]
    assert not bad, (
        "_reresolve_model_override_credentials calls switch_model without "
        f"probe_catalog=False at gateway/run.py:{bad} -- that is the 2026-09-24 "
        "sync GET /v1/models on the event loop."
    )


def test_arm_reresolve_kwarg_pin_bites():
    src = "\n".join([
        "def _reresolve_model_override_credentials(self, identity):",
        "    return switch_model(raw_input='m', current_provider='p')",
    ])
    calls = _reresolve_switch_model_calls(src)
    assert calls and not _passes_probe_off(calls[0])


def test_switch_model_probe_off_opens_no_socket(monkeypatch):
    """Behavioral: ``switch_model(probe_catalog=False)`` does no network I/O.

    Every socket connect raises; with the probe on, the same call reaches the
    catalog probe (asserted, so this arm is not vacuous).
    """
    import socket

    from hermes_cli import model_switch, models

    attempts: list = []

    def _no_network(*args, **kwargs):
        attempts.append(args[:1])
        raise OSError("network disabled in test")

    # models.dev: production always has a (possibly stale) disk cache, and
    # fetch_models_dev serves it without a foreground fetch (stage 3). A temp
    # HERMES_HOME has none, which would make get_label() do the one-time
    # cold-start download -- not the defect under test. Pin the served state.
    import agent.models_dev as _mdev

    monkeypatch.setattr(_mdev, "fetch_models_dev", lambda *a, **k: {})
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: _no_network(addr))

    probed: list = []
    real_validate = models.validate_requested_model

    def _spy_validate(*args, **kwargs):
        probed.append(args[:2])
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(models, "validate_requested_model", _spy_validate)

    user_providers = {
        "local-test": {
            "name": "local-test",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "sk-test",
            "models": {"m-1": {}},
        }
    }
    kwargs = dict(
        raw_input="m-1",
        current_provider="local-test",
        current_model="m-1",
        explicit_provider="local-test",
        user_providers=user_providers,
        custom_providers=[],
    )

    model_switch.switch_model(**kwargs, probe_catalog=False)
    assert probed == [], "probe_catalog=False still ran validate_requested_model"
    assert attempts == [], f"probe_catalog=False opened a socket: {attempts}"

    model_switch.switch_model(**kwargs)
    assert probed, "arm is vacuous: the default path never reached the probe"


# ---------------------------------------------------------------------------
# Mutation arms.
# ---------------------------------------------------------------------------


def _write_tree(tmp_path: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


_INCIDENT_SHAPE = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/sec.py": "\n".join([
        "import urllib.request",
        "",
        "def open_credentialed_url(req, timeout):",
        "    return urllib.request.urlopen(req, timeout=timeout)",
        "",
    ]),
    "hermes_cli/switch.py": "\n".join([
        "from hermes_cli.sec import open_credentialed_url",
        "",
        "def validate_requested_model(m):",
        "    return open_credentialed_url(m, 5)",
        "",
        "def switch_model(m):",
        "    return validate_requested_model(m)",
        "",
        # A same-named def elsewhere, so only the explicit import resolves it.
        "",
    ]),
    "agent/__init__.py": "",
    "agent/other.py": "def switch_model(m):\n    return m\n",
    "gateway/__init__.py": "",
    "gateway/run.py": "\n".join([
        "class Runner:",
        "    def _reresolve(self, ident):",
        "        from hermes_cli.switch import switch_model",
        "        return switch_model(ident)",
        "",
        "    def _set_override(self, key, ov):",
        "        self._reresolve(ov)",
        "",
        "    async def _handle_model_command(self, event):",
        "        self._set_override('k', {})",
        "",
    ]),
}


def test_arm_the_incident_shape_is_red(tmp_path):
    """RED: the real 2026-09-24 shape -- coroutine -> 3 plain defs -> cross-module
    import -> urllib.  ``switch_model`` is defined twice, so only the explicit
    ``from hermes_cli.switch import switch_model`` resolution can see it."""
    root = _write_tree(tmp_path, _INCIDENT_SHAPE)
    mods = [k for k in _INCIDENT_SHAPE if k.endswith(".py")]
    sites = _sites(root, mods)
    assert len(sites) == 1, sites
    assert sites[0].startswith("gateway/run.py _handle_model_command -> "), sites
    assert "switch_model" in sites[0] and "_reresolve" in sites[0], sites


def test_arm_to_thread_offload_is_green(tmp_path):
    files = dict(_INCIDENT_SHAPE)
    files["gateway/run.py"] = files["gateway/run.py"].replace(
        "        self._set_override('k', {})",
        "        import asyncio\n        await asyncio.to_thread(self._set_override, 'k', {})",
    )
    root = _write_tree(tmp_path, files)
    assert _sites(root, [k for k in files if k.endswith(".py")]) == []


def test_arm_bare_noqa_is_not_an_exemption(tmp_path):
    files = dict(_INCIDENT_SHAPE)
    files["gateway/run.py"] = files["gateway/run.py"].replace(
        "    async def _handle_model_command(self, event):",
        "    async def _handle_model_command(self, event):  # noqa: network-on-loop",
    )
    root = _write_tree(tmp_path, files)
    assert len(_sites(root, [k for k in files if k.endswith(".py")])) == 1
    files["gateway/run.py"] = files["gateway/run.py"].replace(
        "# noqa: network-on-loop", "# noqa: network-on-loop cached, never cold"
    )
    root = _write_tree(tmp_path, files)
    assert _sites(root, [k for k in files if k.endswith(".py")]) == []


def test_index_is_populated():
    repo = _repo_root()
    index = build_index(repo, derive_scanned_modules(repo, SCAN_ROOTS), noqa_token=NOQA_TOKEN)
    assert len(index) >= 5000, f"indexed only {len(index)} functions"
