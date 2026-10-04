"""C7 durable-state backfill, hermes-agent slice B (gateway) -- t_ce7b9d19.

One regression per confirmed FleetReview finding (k<N> = row in the C7
adjudication for t_fd9d632e). Each test was RED on origin/main 533612eb0e
before the fix in the same commit.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import gateway.checkout_admission as ca
from gateway.checkout_admission import UNKNOWN, AdmissionGate, HoldStore


# ------------------------------------------------ k89 auto_resume.py (#1123)
def test_k89_inflight_note_names_every_mutating_call_past_the_cap():
    from gateway.auto_resume import describe_inflight_tool_calls

    calls = [
        {"id": f"c{i}", "type": "function",
         "function": {"name": "terminal", "arguments": json.dumps({"command": f"gh pr merge {i}"})}}
        for i in range(8)
    ]
    note = describe_inflight_tool_calls([
        {"role": "user", "content": "merge them"},
        {"role": "assistant", "content": "", "tool_calls": calls},
    ])
    for i in range(8):
        assert f"gh pr merge {i}" in note, note


# ------------------------------------------- checkout_admission.py (#1035)
GW, GW2 = "gateway:default", "gateway:other"


def _freeze_acked(store):
    store.hold("op", [GW], mode="freeze")
    AdmissionGate(store, GW, active_work=lambda: 0).publish()


def test_k90_stale_record_of_a_live_unlisted_consumer_is_unknown(tmp_path):
    store = HoldStore(tmp_path / "adm")
    stalled = AdmissionGate(store, GW2, active_work=lambda: 3)
    stalled.publish()
    _freeze_acked(store)
    rec = store.read_consumer(GW2)
    rec["published_at"] -= 3600  # publisher stalled; its pid is still alive
    store.write_consumer(GW2, rec)
    rep = ca.evaluate(store)
    assert rep["verdict"] == UNKNOWN, rep


def test_k90_dead_unlisted_consumer_record_is_still_ignored(tmp_path):
    store = HoldStore(tmp_path / "adm")
    dead = AdmissionGate(store, GW2, active_work=lambda: 0)
    dead.pid = 2**22 + 12345  # a pid the injected probe reports as dead
    dead.publish()
    _freeze_acked(store)
    rec = store.read_consumer(GW2)
    rec["published_at"] -= 3600
    store.write_consumer(GW2, rec)
    rep = ca.evaluate(store, pid_alive=lambda pid: pid != dead.pid)
    assert rep["verdict"] == ca.QUIESCENT, rep


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def test_k92_pin_check_confirms_the_remote_tip_not_a_stale_tracking_ref(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(work, "config", k, v)
    _git(work, "commit", "-q", "--allow-empty", "-m", "base")
    _git(work, "commit", "-q", "--allow-empty", "-m", "doomed")
    doomed = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "fetch", "-q", "origin")
    # Someone force-pushes main without `doomed`; our origin/main is now stale.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(other, "config", k, v)
    _git(other, "reset", "-q", "--hard", "HEAD~1")
    _git(other, "commit", "-q", "--allow-empty", "-m", "replacement")
    _git(other, "push", "-q", "--force", "origin", "HEAD:main")

    green = [{"name": "tests", "status": "completed", "conclusion": "success"}]
    res = ca.check_pin(doomed, repo=work, remote_ref="origin/main", check_runs=lambda s: green)
    assert res["ok"] is False, res
    assert any("not on origin/main" in p for p in res["problems"]), res


# ---------------------------------- unclean_restart_notice.py (#1004)
_PREV_BOOT = "2026-09-24T18:02:31+00:00"
_THIS_BOOT = "2026-09-24T19:12:27+00:00"
_DEATH = "2026-09-24T19:11:40+00:00"


def _epoch(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def _ledger(home, rows):
    p = home / "logs" / "gateway-restart-ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(**kw):
    return dict({"event": "kickstart", "target_profile": "default",
                 "initiator_profile": "aegis", "token": "t"}, **kw)


def test_k93_pid_match_older_than_the_prior_life_is_not_a_planned_restart(tmp_path, monkeypatch):
    from gateway.fork_ext.unclean_restart_notice import read_planned_restart_for_sentinel

    monkeypatch.setenv("HERMES_PROFILE", "default")
    # A safe-restart row aimed at pid 4242 two days before the prior life began:
    # the OS later reused 4242 for the life that just died uncleanly.
    _ledger(tmp_path, [_row(pid_before=4242, epoch=_epoch(_PREV_BOOT) - 2 * 86400)])
    sentinel = {"phase": "running", "pid": 1, "started_at": _THIS_BOOT, "prior_unclean_exit": True,
                "prior_pid": 4242, "prior_started_at": _PREV_BOOT, "prior_last_heartbeat_at": _DEATH}
    assert read_planned_restart_for_sentinel(sentinel, tmp_path) is None


def test_k94_window_match_written_after_this_boot_or_for_another_pid_is_rejected(tmp_path, monkeypatch):
    from gateway.fork_ext.unclean_restart_notice import read_planned_restart_for_sentinel

    monkeypatch.setenv("HERMES_PROFILE", "default")
    sentinel = {"phase": "running", "pid": 1, "started_at": _THIS_BOOT, "prior_unclean_exit": True,
                "prior_pid": 4242, "prior_started_at": _PREV_BOOT, "prior_last_heartbeat_at": _DEATH}
    # A restart requested shortly AFTER this boot, aimed at the NEW life.
    _ledger(tmp_path, [_row(pid_before=9999, epoch=_epoch(_THIS_BOOT) + 90)])
    assert read_planned_restart_for_sentinel(sentinel, tmp_path) is None
    # Same row with no pid_before, still after this boot (+ slack): rejected.
    _ledger(tmp_path, [_row(epoch=_epoch(_THIS_BOOT) + 90)])
    assert read_planned_restart_for_sentinel(sentinel, tmp_path) is None
    # Control: a window row before the death with no pid still counts.
    _ledger(tmp_path, [_row(epoch=_epoch(_DEATH) - 30)])
    assert read_planned_restart_for_sentinel(sentinel, tmp_path) is not None


# -------------------------------------------- lifecycle_ledger.py (#1004)
def test_k95_heartbeat_from_an_older_life_is_not_the_death_time(tmp_path, monkeypatch):
    from gateway import lifecycle_ledger as ll
    from gateway.shutdown_watchdog import get_loop_heartbeat_path

    ll._write_sentinel({"phase": "running", "pid": 99678, "start_time": 5000.0,
                        "started_at": _PREV_BOOT}, tmp_path)
    hb = get_loop_heartbeat_path(tmp_path)
    hb.parent.mkdir(parents=True, exist_ok=True)
    # The dying life never wrote a heartbeat: the file belongs to pid 11111.
    hb.write_text(json.dumps({"pid": 11111, "start_time": 10.0,
                              "updated_at": "2026-09-20T01:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(ll, "_pid_is_sentinel_owner", lambda *a, **k: False)  # upstream 40087fba7de renamed the probe
    evidence = ll.detect_unclean_exit(tmp_path)
    assert evidence is not None
    assert "last_heartbeat_at" not in evidence, evidence
    ll._claim_sentinel(evidence, tmp_path)
    sentinel = json.loads(ll.get_lifecycle_sentinel_path(tmp_path).read_text(encoding="utf-8"))
    assert "prior_last_heartbeat_at" not in sentinel


def test_k95_heartbeat_of_the_dead_life_is_still_carried(tmp_path, monkeypatch):
    from gateway import lifecycle_ledger as ll
    from gateway.shutdown_watchdog import get_loop_heartbeat_path

    ll._write_sentinel({"phase": "running", "pid": 99678, "start_time": 5000.0,
                        "started_at": _PREV_BOOT}, tmp_path)
    hb = get_loop_heartbeat_path(tmp_path)
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(json.dumps({"pid": 99678, "start_time": 5000.0, "updated_at": _DEATH}), encoding="utf-8")
    monkeypatch.setattr(ll, "_pid_is_sentinel_owner", lambda *a, **k: False)  # upstream 40087fba7de renamed the probe
    assert ll.detect_unclean_exit(tmp_path)["last_heartbeat_at"] == _DEATH


def test_k93_pid_match_older_than_the_prior_life_does_not_fall_through_to_the_window(tmp_path, monkeypatch):
    """FleetReview #1376 dbb62b713b02: a reused-pid row that fails the lifetime
    bound must be skipped, not re-matched by the time-window fallback."""
    from datetime import datetime, timezone

    from gateway.fork_ext.unclean_restart_notice import read_planned_restart_for_sentinel

    monkeypatch.setenv("HERMES_PROFILE", "default")
    start = _epoch(_DEATH) - 50  # a short prior life
    started_at = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
    # Aimed at the EARLIER process that owned pid 4242, 70 s before this life
    # began (past the 60 s slack) yet 120 s before its death (inside the window).
    _ledger(tmp_path, [_row(pid_before=4242, epoch=start - 70)])
    sentinel = {"phase": "running", "pid": 1, "started_at": _THIS_BOOT, "prior_unclean_exit": True,
                "prior_pid": 4242, "prior_started_at": started_at, "prior_last_heartbeat_at": _DEATH}
    assert read_planned_restart_for_sentinel(sentinel, tmp_path) is None


# ------------------------------------------ platforms/helpers.py (#967)
def test_k96_superseded_writer_cannot_overwrite_its_replacement(tmp_path):
    from gateway.platforms.helpers import CoalescingJsonWriter

    path = tmp_path / "state.json"
    old = CoalescingJsonWriter(path, lambda: {"v": "old"}, min_interval_s=0.3)
    old.flush()          # arms the coalescing interval
    old.schedule()       # trailing write now waits ~0.3 s
    new = CoalescingJsonWriter(path, lambda: {"v": "new"}, min_interval_s=0.3)
    new.flush()          # replacement persists newer state first
    assert old.wait_idle(timeout=5.0)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": "new"}
    old.close(flush=False)
    new.close(flush=False)


def test_k97_failed_background_write_is_retried(tmp_path, monkeypatch):
    from gateway.platforms import helpers

    monkeypatch.setattr(helpers, "_COALESCING_RETRY_DELAY_S", 0.05, raising=False)
    path = tmp_path / "state.json"
    fails = {"n": 1}

    def snap():
        if fails["n"]:
            fails["n"] -= 1
            raise OSError("transient ENOSPC")
        return {"channels": ["c1"]}

    w = helpers.CoalescingJsonWriter(path, snap, min_interval_s=0.0)
    w.schedule()  # one update, no further schedule() calls
    deadline = time.monotonic() + 5.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "a transient failure dropped the pending snapshot"
    assert json.loads(path.read_text(encoding="utf-8")) == {"channels": ["c1"]}
    w.close(flush=False)


def test_k96_superseded_writer_stays_superseded_after_its_replacement_is_collected(tmp_path):
    """FleetReview #1376 48440cc59be1: ownership must outlive the replacement object."""
    import gc

    from gateway.platforms.helpers import CoalescingJsonWriter

    path = tmp_path / "state.json"
    old = CoalescingJsonWriter(path, lambda: {"v": "old"}, min_interval_s=0.3)
    old.flush()
    old.schedule()       # trailing write waits ~0.3 s
    new = CoalescingJsonWriter(path, lambda: {"v": "new"}, min_interval_s=0.3)
    new.flush()
    new.close(flush=False)
    del new
    gc.collect()         # the replacement is gone before old's pending write fires
    assert old.wait_idle(timeout=5.0)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": "new"}
    old.close(flush=False)


# --------------------------------------------------- run.py (#969, #1232)
def test_k99_skill_added_or_removed_inside_a_category_invalidates_the_index(tmp_path):
    from gateway import run as gateway_run

    root = tmp_path / "skills"
    skill = root / "cat" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: Alpha\ndescription: t\n---\n", encoding="utf-8")
    gateway_run._invalidate_skill_slug_index()
    try:
        assert "alpha" in gateway_run._skill_slug_index((root,))
        cat_stat = (root / "cat").stat()
        (skill / "SKILL.md").unlink()  # only the skill dir's mtime changes
        os.utime(root / "cat", ns=(cat_stat.st_atime_ns, cat_stat.st_mtime_ns))
        st = skill.stat()
        os.utime(skill, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert "alpha" not in gateway_run._skill_slug_index((root,))
    finally:
        gateway_run._invalidate_skill_slug_index()


def test_k102_honcho_memo_never_stores_values_under_another_contents_digest(tmp_path, monkeypatch):
    # Upstream moved the honcho cache-busting memo from GatewayRunner._extract_honcho_cache_busting_config
    # into the provider's identity_signature() (consumed via _memory_provider_identity_signature);
    # the k102 contract (no memoization under a digest the parse did not read) rides that seam now.
    import plugins.memory.honcho as honcho
    import plugins.memory.honcho.client as hc

    cfg = tmp_path / "honcho.json"
    a = json.dumps({"peerName": "alice"})
    b = json.dumps({"peerName": "bob"})
    cfg.write_text(a, encoding="utf-8")
    monkeypatch.setattr(hc, "resolve_config_path", lambda: cfg)
    monkeypatch.setattr(honcho, "resolve_config_path", lambda: cfg)
    provider = honcho.HonchoMemoryProvider()
    real = hc.HonchoClientConfig.from_global_config.__func__
    race = {"armed": True}

    def racing(cls, *args, **kw):
        if race["armed"]:  # a writer replaces honcho.json between hash and parse
            race["armed"] = False
            cfg.write_text(b, encoding="utf-8")
        return real(cls, *args, **kw)

    monkeypatch.setattr(hc.HonchoClientConfig, "from_global_config", classmethod(racing))
    provider.identity_signature()
    cfg.write_text(a, encoding="utf-8")  # flipped back to exactly A
    values = provider.identity_signature()
    assert values["user_identity"] == "alice", values
