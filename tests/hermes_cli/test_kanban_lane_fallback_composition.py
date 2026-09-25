"""Lane override (#829) composes with capped-pool dispatch fallback (#943).

Precedence at spawn: card pin > lane override > profile default picks the
intended route; the capped-pool fallback then replaces it iff that route's
provider is capped; the flagship gate covers the post-fallback route.
"""
import io
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    assert kb.kanban_db_path().resolve().is_relative_to(home.resolve())
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    (home / "config.yaml").write_text(json.dumps({"kanban": {"provider_health_probes": {
        "pool": "http://localhost/pool", "lanepool": "http://localhost/lanepool",
    }}}))
    with kb.connect_closing() as conn:
        yield conn


def _profile(tmp_path, fallback_model="gpt-5.5"):
    home = tmp_path / ".hermes" / "profiles" / "a"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "pool", "default": "primary"},
        "fallback_providers": [{"provider": "openai-codex", "model": fallback_model}],
    }))


def _health(monkeypatch, capped):
    def urlopen(url, *a, **kw):
        name = url.rsplit("/", 1)[-1]
        return io.BytesIO(b'{"eligible_count":0}' if name in capped else b'{"eligible_count":3}')
    monkeypatch.setattr("urllib.request.urlopen", urlopen)


def _card(conn, lane):
    tid = kb.create_task(conn, title="compose", assignee="a")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    conn.commit()
    kb.set_lane_model_override(
        conn, provider="lanepool", model="lane-model", assignee="a",
        expires_at=int(time.time()) + 3600, reason="capacity window",
    )
    return tid


def _spawn(observed):
    def spawn(task, workspace, **kwargs):
        observed.append((task.model_override, task.provider_override))
        return 777777
    return spawn


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_capped_lane_still_falls_back_on_effective_route(board, monkeypatch, tmp_path, lane):
    _profile(tmp_path)
    _health(monkeypatch, capped={"lanepool"})
    tid = _card(board, lane)
    observed = []
    result = kb.dispatch_once(board, spawn_fn=_spawn(observed))
    assert [s[0] for s in result.spawned] == [tid]
    assert observed == [("gpt-5.5", "openai-codex")]
    assert result.spawn_route_sources[tid].startswith("dispatch-fallback(capped lane-override(")
    assert _events(board, tid, "dispatch_provider_fallback")[0]["from_provider"] == "lanepool"
    persisted = kb.get_task(board, tid)
    assert persisted.model_override is None and persisted.provider_override is None


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_healthy_lane_over_capped_profile_spawns_on_lane_without_fallback(board, monkeypatch, tmp_path, lane):
    _profile(tmp_path)
    _health(monkeypatch, capped={"pool"})
    tid = _card(board, lane)
    observed = []
    result = kb.dispatch_once(board, spawn_fn=_spawn(observed))
    assert observed == [("lane-model", "lanepool")]
    assert result.spawn_route_sources[tid].startswith("lane-override(")
    assert _events(board, tid, "dispatch_provider_fallback") == []


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_flagship_gate_covers_post_fallback_route(board, monkeypatch, tmp_path, lane):
    _profile(tmp_path, fallback_model="gpt-6-astra-900k")
    _health(monkeypatch, capped={"lanepool"})
    tid = _card(board, lane)
    observed = []
    result = kb.dispatch_once(board, spawn_fn=_spawn(observed))
    assert observed == [] and result.spawned == []
    assert kb.get_task(board, tid).status == lane
    deferred = _events(board, tid, "deferred")
    assert deferred[-1]["provider"] == "lanepool"
    assert deferred[-1]["fallback_refused"] == {
        "model": "gpt-6-astra-900k", "provider": "openai-codex", "reason": "flagship",
    }
    # The card's own flagship authorization admits the fallback rung.
    kb.add_comment(board, tid, "apollo", "flagship override: capacity emergency")
    result = kb.dispatch_once(board, spawn_fn=_spawn(observed))
    assert observed == [("gpt-6-astra-900k", "openai-codex")]
