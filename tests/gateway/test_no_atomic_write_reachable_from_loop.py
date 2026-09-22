# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""Ratchet: no ``async def`` may transitively REACH an atomic write.

The lexical sibling gate (``test_no_sync_syscalls_on_event_loop.py``) documents
its own blind spot:

    "synchronous calls reached INDIRECTLY -- an ``async def`` calling a plain
     ``def`` helper that blocks.  Only lexical containment is checked."

The 2026-09-20 Apollo incident lived entirely inside that blind spot.  py-spy
caught the MainThread in ``os.replace`` for 14 consecutive 2s dumps (>= 30s),
four plain-``def`` frames below the coroutine:

    _handle_message_with_agent    (async def, gateway/run.py:25593)
      _apply_post_turn_resume_gate            (gateway/run.py:13948)
        clear_resume_pending                  (gateway/session.py:4058)
          _save                               (gateway/session.py:2152)
            _persist_routing_data             (gateway/session.py:2276)
              _save_sessions_json             (gateway/session.py:2313)
                _write_sessions_json_unlocked (gateway/session.py:2349)
                  utils.atomic_replace -> os.replace   <- 30s

Every one of those frames is lexically clean, so the lexical gate was green and
always would be.  It also scanned ZERO references of ``gateway/session.py``'s
persistence path, so the site was neither fixed nor frozen.

This module closes that gap with a call-graph walk.  The engine, its honest
limits, and why callee resolution is deliberately conservative live in
``_loop_atomic_write_reachability.py`` -- read that docstring before changing a
threshold here.

SCOPE IS DERIVED, NOT HAND-LISTED.  ``derive_scanned_modules`` walks the import
graph from ``gateway/run.py`` and keeps whatever lands inside the package
roots.  ``test_scanned_module_set_is_derived_not_handwritten`` asserts that the
derivation actually happened (and that ``gateway/session.py`` is in it), so a
future edit cannot quietly shrink the scope back to a literal tuple.

RATCHET SEMANTICS.  The name-resolved graph over-approximates, so this is not a
hard zero.  The tree carries a frozen inventory (``REACHABLE_BASELINE``) of the
sites that existed when the gate landed.  A NEW pair fails; a pair that
disappears without the baseline being updated ALSO fails.  The inventory can
therefore only shrink.
"""

from __future__ import annotations

from pathlib import Path

from tests.gateway._loop_atomic_write_reachability import (
    GRAPH_ENTRY_MODULE,
    PACKAGE_ROOTS,
    build_index,
    derive_scanned_modules,
    find_onloop_atomic_write_sites,
    ratchet_key,
)

# Vacuity floors.  A broken derivation or a broken walk must fail LOUDLY, not
# report a clean tree.  Measured on the commit that introduced this gate:
# 102 modules derived, 3616 functions indexed, 96 reachable pairs.
MIN_DERIVED_MODULES = 60
MIN_INDEXED_FUNCTIONS = 1500


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# Frozen inventory of PRE-EXISTING reachable pairs.  Captured 2026-09-20 at 96
# entries on the commit that moved the per-turn sessions.json mirror write off
# the loop (#782), then SHRUNK to 51 by #773, which closed the last two
# `_save_sessions_json` call sites that still bypassed
# `_dispatch_sessions_json_save` (the startup/Discord alias-migration
# retirements in `gateway/session.py`).  45 coroutines stopped reaching any
# sink; ZERO new coroutines appeared.  The 14 entries whose *sink label*
# changed in that pass (`atomic_replace` -> `os.replace`, ->
# `atomic_json_write`) are the same coroutines with the session.py chain now
# closed, so the DFS reports a different pre-existing sink first.
#
# This is an incident-to-lint ratchet, NOT an endorsement.  Each entry is a
# real instance of the same class -- a coroutine that can reach a blocking
# rename -- but most sit on startup, slash-command, shutdown or media-upload
# paths rather than the per-inbound-message hot path, and several would need
# their own behavioural test to fix safely.  Fixing them is out of scope for
# the incident that added this gate; the remainder is tracked on kanban card
# t_13445a80.
#
# To fix one: move the call off-loop (or behind a loop-conditional dispatch,
# annotated `# noqa: atomic-write-on-loop <reason>`), then DELETE its line here.
REACHABLE_BASELINE = frozenset({
    "gateway/platforms/api_server.py _handle_artifact_upload -> os.fsync",
    "gateway/platforms/base.py cancel_background_tasks -> atomic_json_write",
    "gateway/platforms/weixin.py _poll_loop -> atomic_json_write",
    "gateway/platforms/weixin.py qr_login -> atomic_json_write",
    "gateway/run.py _finalize_shutdown_agents -> atomic_json_write",
    "gateway/run.py _handle_message -> atomic_replace",
    # Re-keyed, NOT introduced, by the transcript-spool fix: this coroutine's
    # first reported sink used to be the spool's atomic_json_write via
    # append_to_transcript>.._serialized>spool_dropped_transcript_message.
    # With the spool off-loop, the DFS (which reports only the FIRST sink per
    # coroutine) now surfaces the checkpoint rename that was always behind it:
    # _is_telegram_boot_redelivered_duplicate>maybe_checkpoint>_write.
    # Verified pre-existing on pristine fork/main by masking
    # spool_dropped_transcript_message and re-running the walk.
    "gateway/run.py _handle_message_with_agent_admitted -> os.replace",
    # Unmasked by the status-write fix, NOT introduced by it: the DFS reports
    # only the FIRST sink per coroutine, so the
    # _schedule_resume_pending_sessions>..>_persist>_write chain was shadowed
    # by write_runtime_status.  Verified present on pristine fork/main by
    # masking the status sink and re-running the walk.
    "gateway/run.py _platform_reconnect_watcher -> os.fsync",
    "gateway/run.py _prepare_auto_resume_decisions -> os.replace",
    "gateway/run.py _restore_resume_pending_sessions_at_startup -> os.fsync",
    "gateway/run.py _stop_impl -> atomic_json_write",
    "gateway/run.py _stop_impl_body -> atomic_json_write",
    "gateway/run.py start -> atomic_json_write",
    "gateway/run.py start_gateway -> atomic_json_write",
    "gateway/run.py stop -> atomic_json_write",
    "plugins/platforms/matrix/adapter.py _resolve_message_context -> atomic_json_write",
    "plugins/platforms/telegram/adapter.py _handle_sticker -> os.fsync",
})


# ---------------------------------------------------------------------------
# Scope: derived, and provably so.
# ---------------------------------------------------------------------------


def test_scanned_module_set_is_derived_not_handwritten():
    """The scan scope must come from the import graph, not a literal list.

    Three things are asserted, and the third is the one that matters:

    1. the derivation returns a non-trivial set (vacuity floor),
    2. every member resolves to a real file under a declared package root,
    3. ``gateway/session.py`` is a member -- the module the lexical gate
       scanned zero references of, which is precisely why the 2026-09-20 site
       was neither fixed nor frozen.
    """
    repo = _repo_root()
    modules = derive_scanned_modules(repo)

    assert len(modules) >= MIN_DERIVED_MODULES, (
        f"derived only {len(modules)} modules from {GRAPH_ENTRY_MODULE}; "
        f"expected >= {MIN_DERIVED_MODULES}. The import walk is broken and "
        "this gate is now vacuously green."
    )
    for rel in modules:
        assert (repo / rel).is_file(), f"derived a non-file: {rel}"
        assert any(
            rel == root or rel.startswith(root + "/") for root in PACKAGE_ROOTS
        ), f"derived a module outside the declared package roots: {rel}"

    assert "gateway/session.py" in modules, (
        "gateway/session.py is NOT in the derived scan set. That module holds "
        "the per-turn sessions.json persistence path that blocked the Apollo "
        "event loop for 30s on 2026-09-20; the lexical gate scanned zero "
        "references of it. If the derivation no longer reaches it, the class "
        "this gate exists to freeze is unfrozen again."
    )
    # The entry point itself and the sink module must both be in scope.
    assert "gateway/run.py" in modules
    assert "utils.py" in modules


def test_function_index_is_populated():
    """'Clean' must be distinguishable from 'clean because nothing was walked'."""
    repo = _repo_root()
    index = build_index(repo, derive_scanned_modules(repo))
    assert len(index) >= MIN_INDEXED_FUNCTIONS, (
        f"indexed only {len(index)} functions; expected >= "
        f"{MIN_INDEXED_FUNCTIONS}. The AST walk is broken."
    )
    # Both flavours must be present, or the async filter is silently dropping
    # everything.
    assert any(v["async"] for v in index.values())
    assert any(not v["async"] for v in index.values())


# ---------------------------------------------------------------------------
# The ratchet.
# ---------------------------------------------------------------------------


def test_reachable_atomic_writes_do_not_grow():
    repo = _repo_root()
    modules = derive_scanned_modules(repo)
    sites = find_onloop_atomic_write_sites(repo, modules)
    current = {ratchet_key(s) for s in sites}

    # Keep the full chain for any NEW offender: the chain is what makes the
    # failure actionable, even though it is too unstable to be the key.
    chains = {ratchet_key(s): s for s in sites}

    added = sorted(current - REACHABLE_BASELINE)
    removed = sorted(REACHABLE_BASELINE - current)

    assert not added, (
        "NEW coroutine(s) can now reach a blocking atomic write. This is the "
        "class that took Apollo off the air for ~5 minutes on 2026-09-20: the "
        "rename was 4 plain-def frames below the coroutine, so the lexical "
        "gate could not see it, and os.replace blocked the loop for >=30s "
        "under filesystem contention.\n"
        "Either move the write off-loop (asyncio.to_thread / a coalescing "
        "writer thread) or, if the call is already behind a loop-conditional "
        "dispatch, annotate that function "
        "`# noqa: atomic-write-on-loop <reason>`.\n"
        + "\n".join(f"  {chains[a]}" for a in added)
    )

    assert not removed, (
        "Reachable pair(s) in REACHABLE_BASELINE no longer exist -- good! "
        "Delete these entries from the baseline so the ratchet keeps them "
        "gone:\n" + "\n".join(f"  {r}" for r in removed)
    )


def test_the_site_fixed_by_this_change_is_absent_from_tree_and_baseline():
    """The 2026-09-20 site must be gone AND not listed as 'pre-existing'.

    Without this, a regression could reappear and be silently absorbed into
    the baseline as if it had always been there.
    """
    repo = _repo_root()
    sites = find_onloop_atomic_write_sites(repo, derive_scanned_modules(repo))

    # The incident chain, by its distinctive per-turn frame.  The 2026-09-20
    # stall went clear_resume_pending -> _save -> _persist_routing_data, and
    # _persist_routing_data is the frame unique to the per-TURN write (the
    # startup alias migration reaches _save_sessions_json by another route).
    live = [s for s in sites if "_persist_routing_data" in s]
    assert not live, (
        "the 2026-09-20 per-turn sessions.json chain "
        "(clear_resume_pending -> _save -> _persist_routing_data -> "
        "_save_sessions_json -> atomic_replace) is reachable on the loop "
        "again:\n" + "\n".join(f"  {s}" for s in live)
    )

    fixed_keys = {
        k
        for k in REACHABLE_BASELINE
        if "_persist_routing_data" in k or "_dispatch_sessions_json_save" in k
    }
    assert not fixed_keys, (
        "a site this change fixed is listed in REACHABLE_BASELINE; the "
        f"ratchet would absorb its regression: {sorted(fixed_keys)}"
    )


def test_no_session_persistence_chain_reaches_the_loop():
    """Pin that BOTH session.py mirror-write chains are off the loop.

    Two routes reach ``_save_sessions_json``, and they were closed by two
    different changes:

    * the PER-TURN route (``_persist_routing_data``) -- the 2026-09-20 Apollo
      incident's own chain, routed through ``_dispatch_sessions_json_save`` by
      #782;
    * the STARTUP / Discord ALIAS-MIGRATION route
      (``_redirect_legacy_alias_routes_locked`` and
      ``migrate_discord_session_keys``), which still called
      ``_save_sessions_json`` directly and was the last bypass.  #773 routed
      both through the same loop-conditional dispatcher.

    A regression on either route is a real defect: ``_save_sessions_json`` ends
    in an mkstemp + fsync + ``os.replace`` whose tail is unbounded under
    filesystem pressure, and both routes are reachable from a coroutine.
    """
    repo = _repo_root()
    sites = find_onloop_atomic_write_sites(repo, derive_scanned_modules(repo))

    startup_route = [s for s in sites if "_redirect_legacy_alias_routes_locked" in s]
    discord_route = [s for s in sites if "migrate_discord_session_keys" in s]
    turn_route = [s for s in sites if "_persist_routing_data" in s]

    assert not turn_route, (
        "the per-turn routing-persistence route is reachable on the loop "
        "again:\n" + "\n".join(f"  {s}" for s in turn_route)
    )
    assert not startup_route, (
        "the startup alias-migration route calls _save_sessions_json directly "
        "again instead of _dispatch_sessions_json_save:\n"
        + "\n".join(f"  {s}" for s in startup_route)
    )
    assert not discord_route, (
        "the Discord alias-migration route calls _save_sessions_json directly "
        "again instead of _dispatch_sessions_json_save:\n"
        + "\n".join(f"  {s}" for s in discord_route)
    )


# ---------------------------------------------------------------------------
# Mutation arms -- the gate must BITE, and must not bite the exempt forms.
# ---------------------------------------------------------------------------


def _write_tree(tmp_path: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


CHAIN_DEPTH_5 = "\n".join([
    "import utils",
    "",
    "",
    "class Store:",
    "    def _write_unlocked(self):",
    "        utils.atomic_replace('/tmp/a', '/tmp/b')",
    "",
    "    def _save_json(self):",
    "        self._write_unlocked()",
    "",
    "    def _persist(self):",
    "        self._save_json()",
    "",
    "    def _save(self):",
    "        self._persist()",
    "",
    "    def clear_flag(self):",
    "        self._save()",
    "",
    "",
    "class Runner:",
    "    async def handle_message(self):",
    "        Store().clear_flag()",
    "",
])


def test_arm_the_real_incident_shape_is_red(tmp_path):
    """RED arm: the exact 5-frame indirection the lexical gate cannot see.

    This is the mutation proof. Inject one on-loop atomic write behind five
    plain-def frames and the gate must NAME the coroutine.
    """
    root = _write_tree(tmp_path, {"gateway/m.py": CHAIN_DEPTH_5})
    sites = find_onloop_atomic_write_sites(root, ["gateway/m.py"])
    assert len(sites) == 1, sites
    assert sites[0].startswith("gateway/m.py handle_message -> atomic_replace"), sites
    # The chain must be reported, not just the endpoint.
    assert "clear_flag" in sites[0] and "_write_unlocked" in sites[0], sites


def test_arm_a_session_py_style_injection_is_named(tmp_path):
    """Mutation arm targeting the module the old gate did not scan at all.

    Injecting one on-loop atomic write into a ``gateway/session.py`` shaped
    module must make the gate name it.
    """
    src = "\n".join([
        "import utils",
        "",
        "",
        "class SessionStore:",
        "    def _write_sessions_json_unlocked(self):",
        "        utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "    async def _mutation_arm_probe(self):",
        "        self._write_sessions_json_unlocked()",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/session.py": src})
    sites = find_onloop_atomic_write_sites(root, ["gateway/session.py"])
    assert sites == [
        "gateway/session.py _mutation_arm_probe -> atomic_replace via "
        "_mutation_arm_probe>_write_sessions_json_unlocked"
    ], sites


def test_arm_clean_tree_is_green(tmp_path):
    src = "\n".join([
        "import asyncio",
        "",
        "",
        "def helper():",
        "    return 1",
        "",
        "",
        "async def handle():",
        "    await asyncio.sleep(0)",
        "    return helper()",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/m.py": src})
    assert find_onloop_atomic_write_sites(root, ["gateway/m.py"]) == []


def test_arm_offload_to_thread_is_green(tmp_path):
    """GREEN arm: the FIX must not read as the defect."""
    src = "\n".join([
        "import asyncio",
        "import utils",
        "",
        "",
        "def _blocking_write():",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "async def handle():",
        "    await asyncio.to_thread(_blocking_write)",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/m.py": src})
    assert find_onloop_atomic_write_sites(root, ["gateway/m.py"]) == []


def test_arm_run_in_executor_is_green(tmp_path):
    src = "\n".join([
        "import asyncio",
        "import functools",
        "import utils",
        "",
        "",
        "def _blocking_write():",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "async def handle():",
        "    loop = asyncio.get_running_loop()",
        "    await loop.run_in_executor(None, functools.partial(_blocking_write))",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/m.py": src})
    assert find_onloop_atomic_write_sites(root, ["gateway/m.py"]) == []


def test_arm_noqa_requires_a_reason(tmp_path):
    """A bare marker must NOT exempt; a marker with a reason must."""
    bare = "\n".join([
        "import utils",
        "",
        "",
        "def _w():  # noqa: atomic-write-on-loop",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "async def handle():",
        "    _w()",
        "",
    ])
    root = _write_tree(tmp_path / "bare", {"gateway/m.py": bare})
    assert find_onloop_atomic_write_sites(root, ["gateway/m.py"]) != []

    with_reason = "\n".join([
        "import utils",
        "",
        "",
        "def _w():  # noqa: atomic-write-on-loop loop-conditional dispatch",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "async def handle():",
        "    _w()",
        "",
    ])
    root2 = _write_tree(tmp_path / "reason", {"gateway/m.py": with_reason})
    assert find_onloop_atomic_write_sites(root2, ["gateway/m.py"]) == []


def test_arm_plain_def_chain_alone_is_not_an_offender(tmp_path):
    """No coroutine anywhere in the chain means no loop to block."""
    src = "\n".join([
        "import utils",
        "",
        "",
        "def _w():",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "def caller():",
        "    _w()",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/m.py": src})
    assert find_onloop_atomic_write_sites(root, ["gateway/m.py"]) == []


def test_arm_nested_coroutine_is_not_attributed_to_its_caller(tmp_path):
    """An awaited coroutine is enumerated on its own, not folded into callers."""
    src = "\n".join([
        "import utils",
        "",
        "",
        "def _w():",
        "    utils.atomic_replace('/tmp/a', '/tmp/b')",
        "",
        "",
        "async def inner():",
        "    _w()",
        "",
        "",
        "async def outer():",
        "    await inner()",
        "",
    ])
    root = _write_tree(tmp_path, {"gateway/m.py": src})
    sites = find_onloop_atomic_write_sites(root, ["gateway/m.py"])
    assert len(sites) == 1, sites
    assert " inner -> " in sites[0], sites
