"""cron create on an A-lane pin gets the ruling's chain (t_6c0138c7).

Ace 2026-10-02/10-20: an A-lane falls back to the D-lane (dtlr) first, then a B-lane (btpr).
A session-minted one-shot pinned to claude-alr with no ``fallback`` was pin-filtered by the
scheduler to alr->alr and paged lane-fallback-census every time.
"""
from types import SimpleNamespace

import pytest

from cron import jobs
from cron.scheduler import _resolve_job_fallback_chain

MODEL = "claude-fable-5-1"
GLOBAL = [{"provider": "claude-alr", "model": MODEL}, {"provider": "claude-dtlr", "model": MODEL}]


@pytest.fixture
def served(monkeypatch):
    """Every lane serves every model unless listed in ``refuse``; no live catalog reads."""
    refuse = set()

    def fake_resolve(provider, model, **_kw):
        return SimpleNamespace(ok=(provider, model) not in refuse)

    monkeypatch.setattr("hermes_cli.model_registry.resolve", fake_resolve)
    monkeypatch.setattr(jobs, "_require_known_job_route", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_resolve_stored_model_pair", lambda m, p: (m, p))
    monkeypatch.setattr("hermes_cli.model_policy.validate_worker_model", lambda *a, **k: None)
    return refuse


def _create(tmp_path, **kw):
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="approve #6194", schedule="every 10m", **kw)
        return next(j for j in jobs.load_jobs() if j["id"] == job["id"])


@pytest.mark.parametrize("lane", ["claude-alr", "claude-apr", "claude-alrf", "claude-ahpx-3"])
def test_a_lane_pin_gets_dtlr_then_btpr(tmp_path, served, lane):
    job = _create(tmp_path, model=MODEL, provider=lane)
    assert job["fallback"] == [{"provider": "claude-dtlr", "model": MODEL},
                               {"provider": "claude-btpr", "model": MODEL}]
    assert job["allow_cross_provider_fallback"] is True
    # What the scheduler will actually walk: the ruling chain, not alr->alr.
    assert [e["provider"] for e in _resolve_job_fallback_chain(job, GLOBAL, job["id"])] == [
        "claude-dtlr", "claude-btpr"]


@pytest.mark.parametrize("lane", ["claude-dtlr", "claude-btpr", "claude-bpr", "claude-clr", "openai-codex"])
def test_non_a_lane_untouched(tmp_path, served, lane):
    job = _create(tmp_path, model=MODEL, provider=lane)
    assert "fallback" not in job and "allow_cross_provider_fallback" not in job


def test_unserved_rung_dropped(tmp_path, served):
    served.add(("claude-btpr", MODEL))
    job = _create(tmp_path, model=MODEL, provider="claude-alr")
    assert job["fallback"] == [{"provider": "claude-dtlr", "model": MODEL}]


def test_existing_job_fallback_respected(served):
    job = {"provider": "claude-alr", "model": MODEL, "fallback": [{"provider": "claude-clr", "model": MODEL}]}
    jobs._apply_a_lane_default_fallback(job)
    assert job["fallback"] == [{"provider": "claude-clr", "model": MODEL}]
    assert "allow_cross_provider_fallback" not in job
    opted_out = {"provider": "claude-alr", "model": MODEL, "allow_cross_provider_fallback": False}
    jobs._apply_a_lane_default_fallback(opted_out)
    assert "fallback" not in opted_out
