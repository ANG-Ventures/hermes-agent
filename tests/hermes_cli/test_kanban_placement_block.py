"""A capability block on a misplaced run is re-routed by the dispatcher, once (t_a32116ec).

Fixture reasons are excerpts of real stranded cards on 2026-10-13: t_9a038e36 (ci-box, 26 h) and
t_0e9fdf38 (spilled to ci-box twice).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_placement_block as kpb
from hermes_cli import kanban_placement_reasons as kpr
from hermes_cli import kanban_worker_pool as kwp

CI_BOX_REASON = ("Can't do the drain from here. This run was placed on ci-box (kanbanw), and the local side 6201024 "
                 "only exists in mac-studio-m3u's ~/.hermes. Re-run on the Studio.")
NAMED_AI = "Needs the qBt host: re-dispatch with host: ace-ai (the docker socket is there)."
NAMED_UNKNOWN = "Needs host: nas-1 (the ZFS pool), which no worker reaches."
LOCAL_REAL = "Pinning needs the Kyzcreig admin login, which this worker lacks."


def _host(name, enabled=True):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=enabled, priority=0)


POOL = kwp.PoolConfig(hosts=("ci-box", "ace-ai", "ace-media"), profiles=("alpha",),
                      pool_hosts=(_host("ci-box"), _host("ace-ai"), _host("ace-media", enabled=False)))


def _c(reason, placed="ci-box", body="do the thing", kind="capability", again=False, workspace_kind=None):
    return kpb.classify(kind=kind, reason=reason, placed_host=placed, body=body, pool=POOL,
                        rerouted_before=again, workspace_kind=workspace_kind)


# -- classify (pure) ---------------------------------------------------------

def test_placed_run_capability_block_reroutes_to_the_studio():
    assert _c(CI_BOX_REASON) == ("reroute", kwp.PIN_STUDIO)


def test_reason_naming_an_enabled_pool_host_reroutes_there():
    assert _c(NAMED_AI) == ("reroute", "ace-ai")


@pytest.mark.parametrize("reason", [
    "Needs a worker on the Mac Studio. Re-run with host: studio.",            # t_dcbe83dd shape
    "This needs a Studio shell (re-place with host: mac-studio, then retry)",
])
def test_sentence_punctuation_after_a_studio_pin_still_reroutes(reason):
    assert _c(reason) == ("reroute", kwp.PIN_STUDIO)


@pytest.mark.parametrize("reason, placed, why", [
    (NAMED_UNKNOWN, "ci-box", "not an enabled pool host"),
    ("Needs host: ace-media for the GPU.", "ci-box", "not an enabled pool host"),     # disabled
    (NAMED_AI, "ace-ai", "already failed"),
])
def test_named_host_that_cannot_take_it_is_unroutable(reason, placed, why):
    action, detail = _c(reason, placed=placed)
    assert action == "unroutable" and why in detail


def test_named_host_that_does_not_serve_the_assignee_is_unroutable():
    action, detail = kpb.classify(kind="capability", reason=NAMED_AI, placed_host="ci-box", body="x",
                                  pool=POOL, rerouted_before=False, assignee="beta")
    assert action == "unroutable" and "does not serve profile beta" in detail
    assert kpb.classify(kind="capability", reason=NAMED_AI, placed_host="ci-box", body="x",
                        pool=POOL, rerouted_before=False, assignee="alpha") == ("reroute", "ace-ai")


def test_second_capability_block_after_a_reroute_is_unroutable():
    assert _c(CI_BOX_REASON, again=True)[0] == "unroutable"


def test_operator_studio_pin_is_not_overridden_by_a_remote_host_in_the_reason():
    action, detail = _c(NAMED_AI, body="host: studio\nwork")
    assert action == "unroutable" and "host:studio" in detail


def test_rerouted_card_blocking_again_locally_with_generic_wording_is_unroutable():
    assert _c("Required tool is unavailable.", placed=None, body="host: studio\nx", again=True)[0] == "unroutable"


def test_operator_pin_to_another_host_is_never_overridden():
    action, detail = _c(CI_BOX_REASON, body="host: ci-box\nwork")
    assert action == "unroutable" and "host:ci-box" in detail


@pytest.mark.parametrize("kind", ["needs_input", "transient", None])
def test_other_block_kinds_are_not_placement_blocks(kind):
    assert _c(CI_BOX_REASON, kind=kind) is None


def test_local_run_is_a_placement_block_only_when_the_reason_says_so():
    assert _c(LOCAL_REAL, placed=None) is None
    assert _c("This run landed on ci-box, which has no Studio shell.", placed=None) == ("reroute", "studio")
    # Already on the Studio by pin: a real capability block, left for a human.
    assert _c("re-run on the Studio", placed=None, body="host: studio\nx") is None


# -- dispatcher tick ---------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in placement-block test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    monkeypatch.setattr(kwp, "read_pool", lambda *a, **kw: POOL)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    return home


def _placed_block(conn, body, reason, host="ci-box"):
    # Seed a legacy body: this suite exercises recovery, not mint validation.
    tid = kb.create_task(conn, title="t", assignee="alpha")
    conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, tid))
    conn.commit()
    assert kb.claim_task(conn, tid, claimer="alpha") is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    if host:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_allocated", {"host": host, "reason": "band_spilling"}, run_id=run_id)
    assert kb.block_task(conn, tid, reason=reason, kind="capability")
    return tid


def _events(conn, tid, kind):
    return [json.loads(r[0] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _tick(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **kw: None, max_spawn=0, reconcile_orphans=False)


def test_tick_pins_a_misplaced_capability_block_and_requeues_it(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain the live tree", CI_BOX_REASON)
        res = _tick(conn)
        assert res.placement_rerouted == [(tid, "studio")]
        row = conn.execute("SELECT status, body FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "ready"
        assert row["body"].splitlines()[0] == "host: studio"
        pin, _ = kwp.resolve_pin(row["body"], POOL.hosts)
        assert pin == kwp.PIN_STUDIO
        assert _events(conn, tid, kpb.REROUTED_EVENT)[0]["from_host"] == "ci-box"
        comments = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]
        assert any("placement re-route" in c for c in comments)


def test_unroutable_block_is_recorded_once_and_stays_blocked(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "zfs scrub", NAMED_UNKNOWN)
        assert _tick(conn).placement_unroutable == [tid]
        assert _tick(conn).placement_unroutable == []          # once per block, not per tick
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "blocked"
        assert len(_events(conn, tid, kpb.UNROUTABLE_EVENT)) == 1


def test_a_rerouted_card_that_blocks_again_is_unroutable_not_rerouted_again(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        _tick(conn)
        assert kb.claim_task(conn, tid, claimer="alpha") is not None
        assert kb.block_task(conn, tid, reason="still cannot: re-run on the Studio", kind="capability")
        res = _tick(conn)
        assert res.placement_rerouted == [] and res.placement_unroutable == [tid]
        body = conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert body.count("host: studio") == 1


def test_concurrent_body_edit_is_not_overwritten(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        real = kpb._latest_blocks

        def edit_after_read(c):
            rows = real(c)
            c.execute("UPDATE tasks SET body = 'operator edit' WHERE id = ?", (tid,))
            c.commit()
            return rows
        monkeypatch.setattr(kpb, "_latest_blocks", edit_after_read)
        res = _tick(conn)
        assert res.placement_rerouted == []
        row = conn.execute("SELECT status, body FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["body"]) == ("blocked", "operator edit")
        monkeypatch.setattr(kpb, "_latest_blocks", real)
        assert _tick(conn).placement_rerouted == [(tid, "studio")]       # next tick, against the edit
        body = conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert _NOTE_RE.sub("", body.splitlines()[1]) == "requires: studio-state"   # t_388c4ab2: never a bare pin
        assert body.splitlines()[0::2] == ["host: studio", "operator edit"]


def test_studio_rerouted_card_blocking_again_with_generic_reason_is_recorded_unroutable(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        _tick(conn)
        assert kb.claim_task(conn, tid, claimer="alpha") is not None
        assert kb.block_task(conn, tid, reason="Required tool is unavailable.", kind="capability")
        assert _tick(conn).placement_unroutable == [tid]


def test_failed_unblock_leaves_the_card_blocked_unpinned_and_unmarked(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        before = conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        real = kb.unblock_task
        monkeypatch.setattr(kb, "unblock_task", lambda *a, **kw: False)
        assert _tick(conn).placement_rerouted == []
        row = conn.execute("SELECT status, body FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["body"]) == ("blocked", before)
        assert _events(conn, tid, kpb.REROUTED_EVENT) == []
        monkeypatch.setattr(kb, "unblock_task", real)
        assert _tick(conn).placement_rerouted == [(tid, "studio")]      # retried, not escalated


def test_human_question_blocks_are_untouched(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "x", LOCAL_REAL, host=None)
        res = _tick(conn)
        assert res.placement_rerouted == [] and res.placement_unroutable == []
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "blocked"


def test_dry_run_tick_writes_nothing(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        kbd.dispatch_once(conn, dry_run=True, max_spawn=0, reconcile_orphans=False)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "blocked"
        assert _events(conn, tid, kpb.REROUTED_EVENT) == []


# -- t_dde97120: a repo path missing on the pool host is cured by a clone, never by a Studio pin ----

# Verbatim block reason of t_02e30922 run 59308 (placed on ci-box, scratch workspace, 2026-11-05).
T02E_REASON = ("Placed on ci-box, where required /Users/alexgierczyk/Projects/fleet-ops-scripts does not exist "
               "(Python probe verified). Operator explicitly requires capability refusal in this case. Re-place on "
               "Studio or authorize portable clone/test-gate workflow; no edits or tests performed.\n"
               "unblock_when: ruling:t_02e30922-execution-repo-placement")


def test_scratch_card_blocked_on_a_missing_repo_path_is_not_pinned_to_the_studio():
    assert _c(T02E_REASON, workspace_kind="scratch") == ("clone", None)


@pytest.mark.parametrize("reason", [
    "/home/kanbanw/dev/hermes-home: No such file or directory on this host; re-run on the Studio.",
    "Cannot proceed: ~/Projects/foo is missing on ace-ai. Re-run on the Studio.",
])
def test_other_missing_repo_path_shapes_take_the_clone_cure(reason):
    assert _c(reason, workspace_kind="scratch") == ("clone", None)


@pytest.mark.parametrize("reason", [
    # names a Studio-only resource the capability map lists (vault): the pin stays
    "~/Projects/notes does not exist here and the vault is Studio-only. Re-run on the Studio.",
    # a missing path in the live home tree is Studio state, not a repo a clone recreates
    "~/.hermes/state/pager does not exist on ci-box. Re-run on the Studio.",
    CI_BOX_REASON,
])
def test_studio_only_resources_keep_the_studio_pin(reason):
    assert _c(reason, workspace_kind="scratch") == ("reroute", kwp.PIN_STUDIO)


def test_non_scratch_card_with_a_missing_repo_path_keeps_the_old_pin():
    assert _c(T02E_REASON, workspace_kind="worktree") == ("reroute", kwp.PIN_STUDIO)
    assert _c(T02E_REASON, workspace_kind=None) == ("reroute", kwp.PIN_STUDIO)


def test_t02e30922_event_sequence_requeues_without_a_pin_and_escalates_on_a_second_block(kanban_home):
    """Replay of t_02e30922: placed on ci-box, blocked kind=capability on the missing clone path.
    Red on main: the tick prepends ``host: studio`` and the card never spills again."""
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "origin: cli\nMain CI on ANG-Ventures/fleet-ops-scripts is red", T02E_REASON)
        res = _tick(conn)
        assert res.placement_rerouted == [(tid, kpb.CLONE_CURE)]
        row = conn.execute("SELECT status, body FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "ready"
        assert "host:" not in row["body"]
        assert kwp.resolve_pin(row["body"], POOL.hosts)[0] is None
        (ev,) = _events(conn, tid, kpb.REROUTED_EVENT)
        assert ev["from_host"] == "ci-box" and ev["to_host"] is None and ev["cure"] == kpb.CLONE_CURE
        comments = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]
        assert any("task-private clone" in c for c in comments)
        # The clone cure is once only, like the pin: a second capability block pages instead.
        assert kb.claim_task(conn, tid, claimer="alpha") is not None
        assert kb.block_task(conn, tid, reason=T02E_REASON, kind="capability")
        res = _tick(conn)
        assert res.placement_rerouted == [] and res.placement_unroutable == [tid]


# -- B: the reach matrix fails closed, so a vault card never spills (t_a32116ec) ---------------

VAULT_BODY = "Update the doc in the vault at ~/Obsidian/Ace Place/AI/Deal Alerts/How It Works.md"


@pytest.mark.parametrize("matrix", [None, {"schema": 1, "hosts": {}}, "not json"])
def test_absent_or_empty_matrix_holds_a_vault_card_on_the_studio(tmp_path, matrix):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    if matrix is not None:
        (fleet / kwp.REACH_FILE).write_text(matrix if isinstance(matrix, str) else json.dumps(matrix))
    assert kwp.read_reach(fleet)[2] != "ok"
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),),
                          caps=kwp.read_caps(fleet))
    assert kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="alpha", body=VAULT_BODY, pool=pool) == (False, "studio_needs", None)


def test_fresh_matrix_row_covering_the_vault_lets_that_host_take_it(tmp_path):
    import time as _t
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / kwp.REACH_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ci-box": {"at": _t.time(), "reach": {}, "caps": {"vault": True}}}}))
    pool = kwp.PoolConfig(hosts=("ci-box",), profiles=("alpha",), pool_hosts=(_host("ci-box"),),
                          caps=kwp.read_caps(fleet))
    assert kwp.needs_allowed_hosts(VAULT_BODY, pool) == ("ci-box",)


# -- t_388c4ab2: the re-route's Studio pin carries the requires: line #138's drop needs -----------------
# Live 2026-11-09 03:29-04:48 (t_a57beae6, t_3473dd0e): the tick wrote a BARE `host: studio`,
# effective_pin() dropped it (no requires: reason), the next spawn landed on mbp-2026 again, and the
# second capability block paged #alerts as placement_unroutable.

STUDIO_CAPS = frozenset({"studio-state", "fleet-ssh", "docker", "browser-session"})
CMAP = khc.CapabilityMap(capabilities=tuple(sorted(STUDIO_CAPS)),
                         hosts={khc.STUDIO_ROW: STUDIO_CAPS, "ci-box": frozenset()})
REASONS = kwp.studio_map_reasons(CMAP)


@pytest.fixture
def studio_map(monkeypatch):
    monkeypatch.setattr(khc, "load_map", lambda fleet: CMAP)
    return REASONS


def _portable(body):
    return kwp.portable(workspace_kind="scratch", workspace_has_content=False, assignee="alpha", body=body,
                        pool=POOL, studio_reasons=REASONS)[:2]


def _body(conn, tid):
    return conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]


def _pin_lines(body):
    """The ``host:`` / ``requires:`` lines, in order (create_task adds an ``origin:`` header we ignore)."""
    return [_NOTE_RE.sub("", ln) for ln in body.splitlines() if ln.startswith(("host:", "requires:"))]


# The re-route's own reason on its requires: line (t_d0852f2c); test_reroute_requires_line_passes_create_refusal
# pins the note itself, these compare the words.
_NOTE_RE = re.compile(r" \(re-routed after capability block[^\n]*\)$")


def test_rerouted_studio_pin_survives_the_bare_pin_drop(kanban_home, studio_map):
    """Red on main: the re-route writes a bare `host: studio`, which effective_pin() drops."""
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain the live tree", CI_BOX_REASON, host="ace-ai")
        assert _tick(conn).placement_rerouted == [(tid, "studio")]
        body = _body(conn, tid)
    assert kwp.studio_pin_justified(body, studio_map)
    assert kwp.effective_pin(body, POOL.hosts, None, studio_map)[0] == kwp.PIN_STUDIO
    assert _portable(body) == (False, kpr.HOST_STUDIO)


@pytest.mark.parametrize("reason, word", [
    ("Needs fleet SSH to the NAS, which this worker lacks. Re-run on the Studio.", "fleet-ssh"),
    ("No ssh key for ace-ai here; re-run on the Studio.", "fleet-ssh"),
    ("The docker socket is not reachable from this host. Re-run on the Studio.", "docker"),
    ("The launchd job and its logs live on the Studio. Re-run on the Studio.", "studio-state"),
    ("Restarting the relay needs the Studio. Re-run on the Studio.", "studio-state"),
    (CI_BOX_REASON, "studio-state"),                                       # names none: the default
])
def test_requires_word_comes_from_the_block_reason(kanban_home, studio_map, reason, word):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "do it", reason)
        assert _tick(conn).placement_rerouted == [(tid, "studio")]
        body = _body(conn, tid)
        assert _pin_lines(body) == ["host: studio", f"requires: {word}"]
        assert _pin_lines("\n".join(body.splitlines()[:2])) == ["host: studio", f"requires: {word}"]
        assert any(f"requires: {word}" in c for c in _comments(conn, tid) if "placement re-route" in c)
    assert _portable(body) == (False, kpr.HOST_STUDIO)


@pytest.mark.parametrize("body, want", [
    # the operator-edit gap of 11-09: a bare pin already in the body gets only the requires: line
    ("host: studio\ndrain", {"host: studio", "requires: studio-state"}),
    # a requires: word the Studio serves already justifies it: only the pin is added
    ("requires: docker\ndrain", {"host: studio", "requires: docker"}),
    # already pinned and justified: nothing is written twice
    ("host: studio\nrequires: studio-state\ndrain", {"host: studio", "requires: studio-state"}),
    # a requires: line naming no Studio reason gets the word on THAT line, never a second line
    ("requires: none\ndrain", {"host: studio", "requires: studio-state, none"}),
])
def test_reroute_never_writes_a_second_host_or_requires_line(kanban_home, studio_map, body, want):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, body, CI_BOX_REASON)
        before = _body(conn, tid)
        assert _tick(conn).placement_rerouted == [(tid, "studio")]
        got = _body(conn, tid)
    assert len(_pin_lines(got)) == 2 and set(_pin_lines(got)) == want
    assert [ln for ln in got.splitlines() if _NOTE_RE.sub("", ln) not in want] == \
        [ln for ln in before.splitlines() if not ln.startswith(("host:", "requires:"))]   # the rest is untouched
    assert kwp.studio_pin_justified(got, studio_map) and _portable(got) == (False, kpr.HOST_STUDIO)


def test_no_capability_map_still_writes_the_default_requires_line(kanban_home, monkeypatch):
    """With no map this tick every Studio pin holds; the line is written anyway so the pin survives once it loads."""
    monkeypatch.setattr(khc, "load_map", lambda fleet: None)
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", "fleet ssh is missing here; re-run on the Studio")
        _tick(conn)
        body = _body(conn, tid)
    assert _pin_lines("\n".join(body.splitlines()[:2])) == ["host: studio", "requires: studio-state"]
    assert kwp.studio_pin_justified(body, REASONS)


def test_reroute_to_a_pool_host_writes_no_requires_line(kanban_home, studio_map):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "x", NAMED_AI)
        assert _tick(conn).placement_rerouted == [(tid, "ace-ai")]
        assert _pin_lines(_body(conn, tid)) == ["host: ace-ai"]


@pytest.mark.parametrize("reasons", [REASONS, None, frozenset({"browser-session"}),
                                     frozenset({kpr.ACCESS_CONTROL_PLANE_READ})])
@pytest.mark.parametrize("reason", [CI_BOX_REASON, "fleet ssh", "docker", "launchd", "browser", ""])
@pytest.mark.parametrize("body", ["", "x", "host: studio\nx", "requires: none\nx", "requires: docker"])
def test_every_studio_pin_the_dispatcher_writes_passes_the_dispatch_predicate(reasons, reason, body):
    """One source of truth: the body pin_body() writes is judged by studio_pin_justified, the same predicate
    effective_pin() drops a bare pin with (None = no map: judged against the full test vocabulary)."""
    out = kpb.pin_body(body, kwp.PIN_STUDIO, reason=reason, studio_reasons=reasons, pool=POOL)
    assert kwp.resolve_pin(out, POOL.hosts)[0] == kwp.PIN_STUDIO
    assert kwp.studio_pin_justified(out, reasons if reasons is not None else REASONS)
    assert out.count("host:") == 1 and sum(1 for ln in out.splitlines() if ln.startswith("requires:")) == 1


def test_pin_body_is_the_only_dispatcher_code_that_writes_a_host_pin():
    """A new code path that writes a `host:` line into a card body must go through pin_body() (t_388c4ab2)."""
    import ast
    root = Path(kpb.__file__).parent
    writers = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.JoinedStr) and node.values \
                        and isinstance(node.values[0], ast.Constant) \
                        and str(node.values[0].value).lower().startswith("host: ") \
                        and any(isinstance(v, ast.FormattedValue) for v in node.values):
                    writers.add((path.name, fn.name))
    # _reason_pin builds a probe string for resolve_pin; it never reaches a body.
    assert writers == {("kanban_placement_block.py", "pin_body"), ("kanban_placement_block.py", "_reason_pin")}
