"""The cron store never persists the literal ``"auto"`` model sentinel.

``"auto"`` means "pin to the creating agent's model"; the scheduler has no
handling for it, so a stored ``model="auto"`` fires against a nonexistent
model. The script-mode (no_agent) create/update paths skipped resolution and
stored the sentinel verbatim; a later ``no_agent=False`` flip then produced an
LLM job with ``model="auto"`` (t_2f1ca8d4). The guard lives in ``cron/jobs.py``
(create_job/update_job), so these tests drive the REAL tool entry points —
direct ``cronjob()`` and ``registry.dispatch`` — and read ``jobs.json`` back.
"""

import json

import pytest

import tools.cronjob_tools as ct


@pytest.fixture(autouse=True)
def _cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    from hermes_constants import get_hermes_home

    scripts = get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "noop.sh").write_text("#!/bin/sh\ntrue\n")
    ct.set_current_agent_model(None, None)
    yield tmp_path / "cron" / "jobs.json"
    ct.set_current_agent_model(None, None)


def _via_direct(**kwargs):
    return json.loads(ct.cronjob(**kwargs))


def _via_registry(**kwargs):
    from tools.registry import registry

    return json.loads(registry.dispatch("cronjob", kwargs))


ENTRY_POINTS = pytest.mark.parametrize("call", [_via_direct, _via_registry], ids=["direct", "registry"])


def _stored_rows(jobs_file):
    data = json.loads(jobs_file.read_text())
    rows = data["jobs"] if isinstance(data, dict) else data
    return {row["id"]: row for row in rows}


def _assert_no_sentinel(jobs_file):
    for row in _stored_rows(jobs_file).values():
        model = row.get("model")
        assert not (isinstance(model, str) and model.strip().lower() == "auto"), row


@ENTRY_POINTS
@pytest.mark.parametrize("spelling", ["auto", " AUTO "])
def test_script_create_auto_then_llm_flip_never_stores_auto(call, spelling, _cron_store):
    created = call(action="create", schedule="every 1h", no_agent=True, script="noop.sh", model=spelling)
    assert created["success"] is True, created
    row = _stored_rows(_cron_store)[created["job_id"]]
    assert row["model"] is None and row["provider"] is None

    flipped = call(action="update", job_id=created["job_id"], no_agent=False, prompt="Summarize the output")
    assert flipped["success"] is True, flipped
    row = _stored_rows(_cron_store)[created["job_id"]]
    assert row["no_agent"] is False
    assert row["model"] is None and row["provider"] is None
    _assert_no_sentinel(_cron_store)


@ENTRY_POINTS
def test_script_update_auto_then_llm_flip_never_stores_auto(call, _cron_store):
    created = call(action="create", schedule="every 1h", no_agent=True, script="noop.sh")
    assert created["success"] is True, created
    upd = call(action="update", job_id=created["job_id"], no_agent=True, model="auto")
    assert upd["success"] is True, upd
    assert _stored_rows(_cron_store)[created["job_id"]]["model"] is None

    flipped = call(action="update", job_id=created["job_id"], no_agent=False, prompt="Summarize")
    assert flipped["success"] is True, flipped
    assert _stored_rows(_cron_store)[created["job_id"]]["model"] is None
    _assert_no_sentinel(_cron_store)


def test_store_resolves_auto_for_llm_job_to_creating_agent(_cron_store):
    from cron.jobs import create_job, update_job

    ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
    job = create_job(prompt="p", schedule="every 1h", model="auto")
    assert (job["model"], job["provider"]) == ("gpt-5.6-terra", "openai-codex")

    ct.set_current_agent_model("claude-apr", "claude-sonnet-5")
    updated = update_job(job["id"], {"model": "Auto"})
    assert (updated["model"], updated["provider"]) == ("claude-sonnet-5", "claude-apr")
    _assert_no_sentinel(_cron_store)


def test_store_heals_legacy_auto_row_on_llm_flip(_cron_store):
    """A row written before the guard still holds "auto"; any update heals it."""
    from cron.jobs import create_job, update_job

    job = create_job(prompt="", schedule="every 1h", no_agent=True, script="noop.sh")
    rows = json.loads(_cron_store.read_text())
    target = rows["jobs"] if isinstance(rows, dict) else rows
    for row in target:
        if row["id"] == job["id"]:
            row["model"] = "auto"
    _cron_store.write_text(json.dumps(rows))

    updated = update_job(job["id"], {"no_agent": False, "prompt": "Summarize"})
    assert updated["model"] is None
    _assert_no_sentinel(_cron_store)
