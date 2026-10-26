"""t_71499bfa: an API edit of a cron row's definition is journaled to cron/edits.jsonl.

hermes-home's exec-tier push gate and cron-jobs-runtime-committer (scripts/lib/autocommit_tier.py) read the
record's per-field ``from``/``to`` to tell an API edit from a hand edit; without it every API prompt/schedule/model
edit froze the committed registry until someone carried it to main by hand.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    return home


def _edits(home):
    p = home / "cron" / "edits.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def _lifecycle(home):
    p = home / "cron" / "lifecycle.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_prompt_edit_is_journaled_with_from_and_to(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="v1", schedule="every 5m", name="w", deliver="local")
    update_job(job["id"], {"prompt": "v2"})
    recs = _edits(hermes_env)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["event"] == "updated" and rec["job_id"] == job["id"]
    assert rec["fields"] == {"prompt": {"from": "v1", "to": "v2"}}
    assert rec["at"] and rec["actor"]
    # the lifecycle journal keeps only events its own reader knows (#2349 / R4-4)
    assert {r["event"] for r in _lifecycle(hermes_env)} <= {"created", "removed", "paused", "resumed"}


def test_schedule_edit_names_every_changed_definition_key(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="p", schedule="every 5m", name="w", deliver="local")
    update_job(job["id"], {"schedule": "every 10m"})
    fields = _edits(hermes_env)[-1]["fields"]
    assert "schedule" in fields and fields["schedule"]["to"]["minutes"] == 10
    assert "next_run_at" not in fields   # scheduler runtime is never a journaled definition


def test_pause_and_runtime_only_writes_journal_no_edit(hermes_env):
    from cron.jobs import create_job, pause_job, resume_job, update_job

    job = create_job(prompt="p", schedule="every 5m", name="w", deliver="local")
    pause_job(job["id"], reason="test")
    resume_job(job["id"])
    update_job(job["id"], {"last_delivery_error": "boom"})
    assert _edits(hermes_env) == []


def test_lifecycle_reader_stays_healthy_after_an_edit(hermes_env):
    from cron.jobs import create_job, update_job
    from cron.lifecycle_journal import read_entries_with_health

    job = create_job(prompt="v1", schedule="every 5m", name="w", deliver="local")
    update_job(job["id"], {"prompt": "v2"})
    _, malformed = read_entries_with_health()
    assert malformed == 0
