"""Incident t_d564a8dd (2026-10-11): a dead ``model_override`` must fail at mint, and a
provider 4xx must never be booked ``rate_limited``.

Fixture ``tests/fixtures/kanban/t_d564a8dd_provider_404.log`` is one real run segment of
the incident card's worker log (``HTTP 404: model: claude-sonnet-4-8``, exit 75).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import model_registry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kanban" / "t_d564a8dd_provider_404.log"
DEAD_ID = "claude-sonnet-4-8"
LANE = "claude-alr"
LANE_LIST = (
    f"{LANE}/claude-fable-5-1", f"{LANE}/claude-opus-5-5", f"{LANE}/claude-sonnet-5-5",
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def lane_catalog(monkeypatch):
    """A stubbed live model list: ``claude-alr`` serves LANE_LIST, ``openai-codex`` serves
    one gpt id, anything else is unverifiable (empty)."""
    model_registry.clear_catalog_memo()
    lists = {LANE: LANE_LIST, "openai-codex": ("gpt-6-sol",)}
    monkeypatch.setattr(model_registry, "_fetch_catalog",
                        lambda provider, force_refresh=False: lists.get(provider, ()))
    yield lists
    model_registry.clear_catalog_memo()


# ---------------------------------------------------------------- A: the resolver

def test_resolver_refuses_dead_id_with_the_lane_list(lane_catalog):
    verdict = model_registry.resolve(LANE, DEAD_ID)
    assert not verdict.ok and verdict.reason == "unknown"
    assert verdict.valid_ids == LANE_LIST
    with pytest.raises(model_registry.UnknownModel) as exc:
        model_registry.require(LANE, DEAD_ID)
    assert DEAD_ID in str(exc.value) and "claude-sonnet-5-5" in str(exc.value)
    assert "--force-model" in str(exc.value)


def test_resolver_accepts_bare_and_prefixed_ids_and_force(lane_catalog):
    assert model_registry.resolve(LANE, "claude-sonnet-5-5").reason == "known"
    assert model_registry.resolve(LANE, f"{LANE}/claude-sonnet-5-5").reason == "known"
    assert model_registry.resolve(None, f"{LANE}/claude-sonnet-5-5").reason == "known"
    forced = model_registry.resolve(LANE, DEAD_ID, force_reason="ace said so")
    assert forced.ok and forced.reason == "forced" and forced.force_reason == "ace said so"


def test_resolver_admits_unverifiable_lane_and_no_provider(lane_catalog):
    assert model_registry.resolve("custom-b", "whatever").reason == "unverifiable"
    assert model_registry.resolve(None, DEAD_ID).reason == "no_provider"
    assert model_registry.invalid_pin_reason("custom-b", "whatever") is None
    assert model_registry.invalid_pin_reason(LANE, DEAD_ID) == (
        f"model_override_invalid: {DEAD_ID} not on {LANE}")


def test_resolver_memo_honours_ttl(monkeypatch):
    model_registry.clear_catalog_memo()
    calls = []
    monkeypatch.setattr(model_registry, "_fetch_catalog",
                        lambda provider, force_refresh=False: calls.append(provider) or ("m",))
    model_registry.catalog_for("lane-x")
    model_registry.catalog_for("lane-x")
    assert calls == ["lane-x"]
    model_registry.catalog_for("lane-x", force_refresh=True)
    assert calls == ["lane-x", "lane-x"]
    model_registry.clear_catalog_memo()


# ---------------------------------------------------------------- A: kanban writers

def test_create_task_refuses_dead_pin_and_forces_with_comment(kanban_home, lane_catalog):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="claude-sonnet-5-5"):
            kb.create_task(conn, title="x", assignee="w",
                           model_override=DEAD_ID, provider_override=LANE)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        tid = kb.create_task(conn, title="ok", assignee="w",
                             model_override="claude-sonnet-5-5", provider_override=LANE)
        assert kb.get_task(conn, tid).model_override == "claude-sonnet-5-5"
        forced = kb.create_task(conn, title="forced", assignee="w",
                                model_override=DEAD_ID, provider_override=LANE,
                                force_model_reason="relay lists lag")
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (forced,))]
        assert any(b.startswith(kb.MODEL_FORCE_COMMENT_PREFIX) and "relay lists lag" in b for b in bodies)


def test_set_model_override_and_edit_refuse_dead_pin(kanban_home, lane_catalog):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        with pytest.raises(ValueError, match=DEAD_ID):
            kb.set_model_override(conn, tid, DEAD_ID, LANE)
        with pytest.raises(ValueError, match=DEAD_ID):
            kb.set_task_model(conn, tid, f"{LANE}/{DEAD_ID}")
        assert kb.get_task(conn, tid).model_override is None
        assert kb.set_task_model(conn, tid, f"{LANE}/claude-sonnet-5-5") == 1
        assert kb.get_task(conn, tid).model_override.endswith("claude-sonnet-5-5")
        assert kb.set_task_model(conn, tid, f"{LANE}/{DEAD_ID}", force_model_reason="why") == 1
        assert kb.get_task(conn, tid).model_override.endswith(DEAD_ID)


def test_batch_route_write_refuses_dead_pin_with_nothing_written(kanban_home, lane_catalog):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="w")
        b = kb.create_task(conn, title="b", assignee="w")
        writes = [kb.BatchRouteWrite(task_id=t, touch_model=True, model=DEAD_ID, provider=LANE)
                  for t in (a, b)]
        with pytest.raises(ValueError, match=DEAD_ID):
            kb.apply_batch_route_writes(conn, writes)
        assert all(kb.get_task(conn, t).model_override is None for t in (a, b))


# ---------------------------------------------------------------- A: dispatcher pre-spawn

def test_dispatcher_blocks_hand_written_dead_pin_before_spawn(kanban_home, lane_catalog, monkeypatch):
    from hermes_cli import profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        # The back door: a row written by hand (or before the writers validated).
        conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                     (DEAD_ID, LANE, tid))
        conn.commit()
        spawned = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 4242)
        assert spawned == []
        assert tid in result.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.block_kind == "capability"
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
        assert json.loads(blocked["payload"])["reason"] == (
            f"model_override_invalid: {DEAD_ID} not on {LANE}")
        comment = conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND author='dispatcher'", (tid,)).fetchone()
        assert "claude-sonnet-5-5" in comment["body"]


# ---------------------------------------------------------------- A: cron + delegate

def test_cron_create_and_fallback_chain_refuse_dead_id(kanban_home, lane_catalog, monkeypatch):
    from cron import jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "_resolve_stored_model_pair", lambda m, p: (m, p))
    with pytest.raises(ValueError, match=DEAD_ID):
        cron_jobs.create_job(prompt="p", schedule="every 1h", model=DEAD_ID, provider=LANE)
    job = cron_jobs.create_job(prompt="p", schedule="every 1h",
                               model="claude-sonnet-5-5", provider=LANE)
    with pytest.raises(ValueError, match=DEAD_ID):
        cron_jobs.update_job(job["id"], {"fallback": [
            {"provider": LANE, "model": "claude-sonnet-5-5"},
            {"provider": LANE, "model": DEAD_ID},
        ]})
    assert cron_jobs.get_job(job["id"]).get("fallback") is None
    updated = cron_jobs.update_job(job["id"], {
        "fallback": [{"provider": LANE, "model": DEAD_ID}], "force_model_reason": "testing"})
    assert updated["fallback"][0]["model"] == DEAD_ID
    assert "force_model_reason" not in updated


def test_delegate_task_refuses_dead_model(kanban_home, lane_catalog, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr("hermes_cli.model_switch.resolve_model_pair_for_storage",
                        lambda m, p: (m, p))
    from types import SimpleNamespace

    out = delegate_tool.delegate_task(goal="g", model=DEAD_ID, provider=LANE,
                                      parent_agent=SimpleNamespace(_delegate_depth=0))
    assert DEAD_ID in out and "claude-sonnet-5-5" in out and "force_model" in out


# ---------------------------------------------------------------- B: run classification

def test_provider_rejected_status_reads_the_real_incident_log():
    from hermes_cli.kanban_worker_exit import provider_rejected_status

    text = FIXTURE.read_text()
    assert provider_rejected_status(text) == f"HTTP 404: model: {DEAD_ID}"
    assert provider_rejected_status(
        "x\nProvider said: HTTP 429: rate_limit_error: usage limit reached\n") is None
    assert provider_rejected_status(
        "Provider said: HTTP 503: overloaded\n[kanban-worker-exit] rc=75\n") is None
    # An old 404 above a later wall is the wall.
    assert provider_rejected_status(
        f"Provider said: HTTP 404: model: {DEAD_ID}\n" + "line\n" * 10
        + "Provider said: HTTP 429: too many requests\n") is None


def test_pinned_worker_exit_is_a_failure_on_a_4xx(monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import kanban_worker_route as route

    agent = SimpleNamespace(_kanban_pin_refused_failover={"provider": LANE})
    rejected = {"failed": True, "failure_retryable": True, "failure_reason": "unknown",
                "error": f"HTTP 404: model: {DEAD_ID}"}
    assert route.apply_pin_refusal_to_result(agent, dict(rejected))["failure_reason"] == "unknown"
    outage = {"failed": True, "failure_retryable": True, "failure_reason": "unknown",
              "error": "connection reset by peer"}
    assert route.apply_pin_refusal_to_result(agent, dict(outage))["failure_reason"] == (
        route.PINNED_PROVIDER_FAILURE_REASON)


def _seed_exit(conn, tid, pid, home, log_text, code):
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(log_text)
    kbd._record_worker_exit(pid, code << 8)


def test_rate_limit_exit_with_provider_404_is_provider_rejected_and_counts(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    text = FIXTURE.read_text()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", max_retries=99)
        for i in range(3):
            _seed_exit(conn, tid, 80000 + i, kanban_home, text, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            crashed = kbd.detect_crashed_workers(conn)
            assert tid in crashed
            assert tid not in getattr(kbd.detect_crashed_workers, "_last_rate_limited", [])
            task = kb.get_task(conn, tid)
            run = conn.execute(
                "SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            assert run["outcome"] == "provider_rejected"
            assert f"HTTP 404: model: {DEAD_ID}" in run["error"]
            assert task.consecutive_failures == i + 1
            if i < 2:
                assert task.status == "ready"
        assert task.status == "blocked" and task.block_kind == "capability"
        assert tid in getattr(kbd.detect_crashed_workers, "_last_auto_blocked", [])
        assert f"HTTP 404: model: {DEAD_ID}" in (task.last_failure_error or "")
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        assert "rate_limited" not in kinds
        assert kinds.count("provider_rejected") == 3 and "blocked" in kinds
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_rate_limit_exit_with_a_real_429_stays_rate_limited(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    text = ("[hermes-kanban-run-boundary deadbeef]\n"
            "Provider said: HTTP 429: rate_limit_error: usage limit reached\n\n"
            "session_id: x\n\n[kanban-worker-exit] rc=75\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _seed_exit(conn, tid, 81000, kanban_home, text, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
        crashed = kbd.detect_crashed_workers(conn)
        assert tid not in crashed
        assert tid in getattr(kbd.detect_crashed_workers, "_last_rate_limited", [])
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0


def test_forced_pin_is_honoured_at_dispatch_for_that_pair_only(kanban_home, lane_catalog, monkeypatch):
    """Prism 68402070f7c9: a --force-model pin must run, not be capability-blocked every tick;
    a later re-pin to a DIFFERENT unknown id gets no exemption."""
    from hermes_cli import profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                     (DEAD_ID, LANE, tid))
        kb.add_comment(conn, tid, "apollo", kb._model_force_comment(LANE, DEAD_ID, "canary lane"))
        conn.commit()
        assert kb.model_force_comment_present(conn, tid, LANE, DEAD_ID)
        spawned = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 4242)
        assert tid not in result.auto_blocked
        assert kb.get_task(conn, tid).status != "blocked"
        # re-pin to another unknown id on a fresh card: the force comment covers only its own pair
        tid2 = kb.create_task(conn, title="y", assignee="w")
        conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                     (DEAD_ID + "-other", LANE, tid2))
        kb.add_comment(conn, tid2, "apollo", kb._model_force_comment(LANE, DEAD_ID, "canary lane"))
        conn.commit()
        tid = tid2
        assert not kb.model_force_comment_present(conn, tid, LANE, DEAD_ID + "-other")
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 4242)
        assert tid in result.auto_blocked



def test_plain_nonzero_exit_with_provider_404_is_provider_rejected(kanban_home, monkeypatch):
    """Prism P2 e1cc1e3f8b27 on #1903: ``apply_pin_refusal_to_result`` now lets a 4xx
    exit as an ordinary failure (rc=1, no cooldown marker). That exit must reach the
    same rejection accounting as the rc=75 shape, not generic ``crashed`` accounting."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    text = ("[hermes-kanban-run-boundary deadbeef]\n"
            f"Provider said: HTTP 404: model: {DEAD_ID}\n\n"
            "session_id: x\n\n[kanban-worker-exit] rc=1\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", max_retries=99)
        _seed_exit(conn, tid, 82000, kanban_home, text, 1)
        assert tid in kbd.detect_crashed_workers(conn)
        run = conn.execute(
            "SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        assert run["outcome"] == "provider_rejected"
        assert f"HTTP 404: model: {DEAD_ID}" in run["error"]
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        assert "provider_rejected" in kinds and "crashed" not in kinds
        assert kb.get_task(conn, tid).consecutive_failures == 1
