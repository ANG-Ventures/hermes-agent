"""Kanban dispatcher must not hand an inherited GitHub lane to a worker (hermes-home spec
plans/2026-09-25_github-app-identities D3): the gh shim decides a worker's lane from its PROFILE."""

from __future__ import annotations

from tests.hermes_cli.test_kanban_worker_chat_identity import _capture_spawn_env


def test_dispatcher_scrubs_gh_lane_from_worker_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_GH_LANE", "lander")
    env = _capture_spawn_env(monkeypatch, tmp_path)
    assert "HERMES_GH_LANE" not in env


def test_worker_env_still_carries_profile(monkeypatch, tmp_path):
    env = _capture_spawn_env(monkeypatch, tmp_path)
    assert any(k.endswith("PROFILE") and v == "worker" for k, v in env.items())
