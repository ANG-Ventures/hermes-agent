from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task_id(output: str) -> str:
    match = re.search(r"t_[a-f0-9]+", output)
    assert match, output
    return match.group(0)


def _create(title: str, *, assignee: str = "worker") -> str:
    return _task_id(kc.run_slash(f"create '{title}' --assignee {assignee}"))


def _configure_provider(home: Path, provider: str = "test-lane") -> None:
    (home / "config.yaml").write_text(
        "providers:\n"
        f"  {provider}:\n"
        "    base_url: http://127.0.0.1:9999/v1\n"
        "    api_key: test\n",
        encoding="utf-8",
    )


def test_lane_override_round_trip_and_expiry(kanban_home):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn,
            provider="test-lane",
            model="model-a",
            expires_at=200,
            reason="capacity",
            assignee="worker",
            now=100,
        )
        active = kb.get_lane_model_override(conn, assignee="worker", now=199)
        assert active.provider == "test-lane"
        assert active.model == "model-a"
        assert active.reason == "capacity"
        assert active.expires_at == 200

        expired = kb.expire_lane_model_overrides(conn, now=200)
        assert [(row.assignee, row.provider, row.model) for row in expired] == [
            ("worker", "test-lane", "model-a")
        ]
        assert kb.get_lane_model_override(conn, assignee="worker", now=200) is None
        assert kb.expire_lane_model_overrides(conn, now=201) == []


def test_get_lane_override_enforces_the_ttl_boundary(kanban_home):
    """A lapsed row must not be returned even when it is still in the table.

    ``expire_lane_model_overrides`` only runs on a dispatcher tick, so between
    ticks an elapsed row is physically present. If the read path didn't filter
    on ``expires_at`` the override would keep routing spawns past its window —
    exactly the never-expires behaviour this feature replaces. Expiry is
    exclusive, so the boundary second itself already belongs to the default.
    """
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="test-lane", model="model-a", expires_at=200,
            reason="capacity", assignee="worker", now=100,
        )
        assert kb.get_lane_model_override(conn, assignee="worker", now=199) is not None
        # Boundary and past-boundary reads see nothing, with the row still there.
        assert kb.get_lane_model_override(conn, assignee="worker", now=200) is None
        assert kb.get_lane_model_override(conn, assignee="worker", now=5000) is None
        assert len(kb.list_lane_model_overrides(conn, include_expired=True)) == 1
        assert kb.list_lane_model_overrides(conn, now=200) == []


def test_assignee_lane_beats_global_lane(kanban_home):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="global", model="g", expires_at=300,
            reason="global", now=100,
        )
        kb.set_lane_model_override(
            conn, provider="specific", model="s", expires_at=300,
            reason="specific", assignee="worker", now=100,
        )
        assert kb.get_lane_model_override(conn, assignee="worker", now=101).provider == "specific"
        assert kb.get_lane_model_override(conn, assignee="other", now=101).provider == "global"


def test_dispatch_precedence_card_then_lane_then_profile(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  provider: profile-provider\n  default: profile-model\n",
        encoding="utf-8",
    )
    seen = {}

    def spawn(task, workspace, *, board=None):
        seen[task.title] = (task.provider_override, task.model_override)
        return 123

    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="lane-provider", model="lane-model",
            expires_at=200, reason="capacity", assignee="worker", now=100,
        )
        kb.create_task(
            conn, title="card", assignee="worker",
            provider_override="card-provider", model_override="card-model",
        )
        kb.create_task(conn, title="lane", assignee="worker")
        kb.create_task(conn, title="profile", assignee="other")
        other = kanban_home / "profiles" / "other"
        other.mkdir(parents=True)
        (other / "config.yaml").write_text(
            "model:\n  provider: other-provider\n  default: other-model\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(kb.time, "time", lambda: 101)
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert seen == {
        "card": ("card-provider", "card-model"),
        "lane": ("lane-provider", "lane-model"),
        "profile": (None, None),
    }
    assert result.spawn_routes
    assert any(source.startswith("lane-override(") for source in result.spawn_route_sources.values())
    lane_id = next(task_id for task_id, route in result.spawn_routes.items() if route == "lane-provider/lane-model")
    assert "99s remaining" in result.spawn_route_sources[lane_id]


def test_dispatch_expiry_falls_back_and_reports_once(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    seen = []

    def spawn(task, workspace, *, board=None):
        seen.append((task.provider_override, task.model_override))
        return 123

    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="lane-provider", model="lane-model",
            expires_at=100, reason="capacity", now=50,
        )
        kb.create_task(conn, title="first", assignee="worker")
        monkeypatch.setattr(kb.time, "time", lambda: 100)
        first = kb.dispatch_once(conn, spawn_fn=spawn)
        assert first.expired_lane_models
        assert seen == [(None, None)]

        kb.create_task(conn, title="second", assignee="worker")
        second = kb.dispatch_once(conn, spawn_fn=spawn)
        assert second.expired_lane_models == []


def test_stats_include_active_lane_override(kanban_home, monkeypatch):
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="test-lane", model="model-a", expires_at=200,
            reason="capacity", assignee="worker", now=100,
        )
        monkeypatch.setattr(kb.time, "time", lambda: 150)
        stats = kb.board_stats(conn)
    assert stats["lane_model_overrides"] == [{
        "assignee": "worker",
        "provider": "test-lane",
        "model": "model-a",
        "reason": "capacity",
        "created_at": 100,
        "expires_at": 200,
        "ttl_remaining_seconds": 50,
    }]


def test_cli_lane_model_set_show_clear(kanban_home):
    _configure_provider(kanban_home)
    out = kc.run_slash(
        "lane-model set test-lane/model-a --ttl 2h --reason 'capacity' --assignee worker"
    )
    assert "route=test-lane/model-a" in out
    assert "assignee=worker" in out
    shown = kc.run_slash("lane-model show")
    assert "test-lane/model-a" in shown
    assert "capacity" in shown
    cleared = kc.run_slash("lane-model clear --assignee worker")
    assert "Cleared" in cleared
    assert "(no active lane-model overrides)" in kc.run_slash("lane-model show")


def test_cli_lane_model_refuses_unknown_provider(kanban_home):
    out = kc.run_slash(
        "lane-model set typo-provider/model-a --ttl 2h --reason 'capacity'"
    )
    assert "unknown provider" in out.lower()
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn) == []


@pytest.mark.parametrize("discovery", ["raises", "empty"])
def test_cli_lane_model_refuses_unvalidated_provider_when_registry_unavailable(
    kanban_home, monkeypatch, discovery,
):
    import providers

    (kanban_home / "config.yaml").write_text("providers: {}\n", encoding="utf-8")
    if discovery == "raises":
        def broken_registry():
            raise RuntimeError("registry unavailable")
        monkeypatch.setattr(providers, "list_providers", broken_registry)
    else:
        monkeypatch.setattr(providers, "list_providers", lambda: [])
    out = kc.run_slash(
        "lane-model set typo-provider-zz/model-a --ttl 2h --reason capacity"
    )
    assert "provider" in out.lower() and ("unknown" in out.lower() or "discover" in out.lower()), out
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn) == []


def test_cli_lane_model_accepts_configured_provider_when_registry_fails(
    kanban_home, monkeypatch,
):
    import providers

    _configure_provider(kanban_home)
    def broken_registry():
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(providers, "list_providers", broken_registry)
    out = kc.run_slash("lane-model set test-lane/model-a --ttl 2h --reason capacity")
    assert "route=test-lane/model-a" in out


def test_cli_lane_model_firepower_requires_reason_beyond_reason_flag(kanban_home):
    _configure_provider(kanban_home)
    out = kc.run_slash(
        "lane-model set test-lane/gpt-6-astra-900k --ttl 2h --reason 'capacity'"
    )
    assert "firepower-only" in out
    ok = kc.run_slash(
        "lane-model set test-lane/gpt-6-astra-900k --ttl 2h --reason 'capacity' "
        "--firepower 'approved burst'"
    )
    assert "route=test-lane/gpt-6-astra-900k" in ok


def test_lane_clear_all_removes_every_lane_in_one_transaction(kanban_home):
    """`clear --all` is one operator action: it reports exactly what it deleted.

    Same partial-commit class as the set-model batch. Listing the rows and then
    deleting them one individually-committed row at a time could leave the
    board half-cleared and report rows a concurrent writer had already removed.
    """
    expires = 4_000_000_000
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="test-lane", model="m-board", expires_at=expires,
        )
        kb.set_lane_model_override(
            conn, provider="test-lane", model="m-worker",
            assignee="worker", expires_at=expires,
        )

    out = kc.run_slash("lane-model clear --all")

    assert "m-board" in out and "m-worker" in out
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn, include_expired=True) == []


def test_lane_clear_all_reports_only_rows_it_actually_deleted(kanban_home):
    """A concurrent delete must not be reported as this call's work."""
    expires = 4_000_000_000
    with kb.connect() as conn:
        kb.set_lane_model_override(
            conn, provider="test-lane", model="m-worker",
            assignee="worker", expires_at=expires,
        )
        kb.clear_lane_model_override(conn, assignee="worker")

    out = kc.run_slash("lane-model clear --all")

    assert "m-worker" not in out
    assert "no lane-model override set for any lane" in out
