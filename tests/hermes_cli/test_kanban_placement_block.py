"""A capability block on a misplaced run is re-routed by the dispatcher, once (t_a32116ec).

Fixture reasons are excerpts of real stranded cards on 2026-10-13: t_9a038e36 (ci-box, 26 h) and
t_0e9fdf38 (spilled to ci-box twice).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_placement_block as kpb
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


def _c(reason, placed="ci-box", body="do the thing", kind="capability", again=False):
    return kpb.classify(kind=kind, reason=reason, placed_host=placed, body=body, pool=POOL,
                        rerouted_before=again)


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
    tid = kb.create_task(conn, title="t", assignee="alpha", body=body)
    assert kb.claim_task(conn, tid, claimer="alpha") is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    if host:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_placed", {"host": host, "reason": "band_spilling"}, run_id=run_id)
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
        assert body == "host: studio\noperator edit"


def test_studio_rerouted_card_blocking_again_with_generic_reason_is_recorded_unroutable(kanban_home):
    with kb.connect_closing() as conn:
        tid = _placed_block(conn, "drain", CI_BOX_REASON)
        _tick(conn)
        assert kb.claim_task(conn, tid, claimer="alpha") is not None
        assert kb.block_task(conn, tid, reason="Required tool is unavailable.", kind="capability")
        assert _tick(conn).placement_unroutable == [tid]


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
