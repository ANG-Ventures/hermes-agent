"""t_14b3fa1d: a cron fire must not deep-copy the whole registry.

Live (gateway pid 43930, 10-26) cron-parallel threads held 17.9% of GIL samples, top own-frame
``_record_load_baseline``'s deepcopy of the 693-job registry. A one-shot completion changes
definition fields (enabled/state), so ``mark_job_run`` takes ``_with_job_runtime``'s full-save
branch: each such fire deep-copied every record twice (load baseline + ``refresh_baseline``) and
parsed jobs.json three times. Real store under a temp HERMES_HOME.
"""

import json
from types import SimpleNamespace

import pytest

N_JOBS, N_FIRES = 700, 50


def _job(i, once):
    return {
        "id": f"job{i:04d}abcd", "name": f"fixture-{i}", "prompt": "digest " * 20,
        "script": f"fixture_{i}.py", "no_agent": True, "deliver": "local",
        "schedule": ({"kind": "once", "run_at": "2026-10-26T12:00:00-07:00", "display": "once"}
                     if once else {"kind": "interval", "minutes": 5, "display": "every 5m"}),
        "repeat": {"times": 1 if once else None}, "enabled": True, "state": "scheduled",
        "created_at": "2026-10-01T00:00:00-07:00", "next_run_at": "2026-10-26T12:00:00-07:00",
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    """N_JOBS jobs (the first N_FIRES/2 one-shots); counts top-level deepcopies of job records
    made by cron.jobs and every whole-file jobs.json parse."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    (tmp_path / "cron").mkdir()
    from cron import jobs as cj

    jobs = [_job(i, once=i < N_FIRES // 2) for i in range(N_JOBS)]
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    cj.load_jobs()
    n = {"record_copies": 0, "parses": 0}
    real_copy, real_parse = cj.copy, cj._parse_jobs_file

    def deepcopy(obj, *a, **kw):
        n["record_copies"] += isinstance(obj, dict) and "id" in obj and "schedule" in obj
        return real_copy.deepcopy(obj, *a, **kw)

    def parse(path):
        n["parses"] += 1
        return real_parse(path)

    monkeypatch.setattr(cj, "copy", SimpleNamespace(deepcopy=deepcopy))
    monkeypatch.setattr(cj, "_parse_jobs_file", parse)
    return cj, n


def _fire(cj, jid):
    claimed = cj.claim_job_for_fire(jid, return_job=True)
    owner = claimed["fire_claim"]["by"]
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    assert cj.mark_job_run(jid, True, expected_fire_owner=owner) is True


def test_fires_do_not_copy_the_registry(store):
    cj, n = store
    for i in range(N_FIRES):
        _fire(cj, f"job{i:04d}abcd")
    # O(ticks), not O(fires): only single-record copies (a few per fire), never the 700-job
    # registry. Before: 25 one-shots x 2 x 700 + 400 = 35,400 copies.
    assert n["record_copies"] <= 10 * N_FIRES < N_JOBS, n
    # A one-shot completion parses once for its save (load_jobs) and once to rebuild the
    # single-job cache after the rename; no extra decode to look for a sibling generation
    # when the only pending one is its own. Before: 3 per one-shot (76 total).
    assert n["parses"] <= 2 * (N_FIRES // 2) + 1, n
    view = {j["id"]: j for j in cj.load_jobs()}
    once, rec = view["job0000abcd"], view[f"job{N_FIRES - 1:04d}abcd"]
    assert (once["state"], once["enabled"], once["last_status"]) == ("completed", False, "ok")
    assert (rec["state"], rec["enabled"], rec["last_status"]) == ("scheduled", True, "ok")
    assert rec["fire_claim"] is None and rec["repeat"]["completed"] == 1


def test_shallow_baseline_still_reconciles_a_sibling_definition_edit(store, monkeypatch):
    """A sibling's edit to ANOTHER job landing between the full-save branch's load and its save
    still wins with the shallow baseline."""
    cj, _ = store
    real_load = cj.load_jobs

    def load_then_sibling_edits():
        out = real_load()
        path = cj._current_cron_store().jobs_file
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][600]["paused_reason"] = "sibling pause"
        path.write_text(json.dumps(data), encoding="utf-8")
        return out

    monkeypatch.setattr(cj, "load_jobs", load_then_sibling_edits)
    _fire(cj, "job0000abcd")  # one-shot: definition change -> full-save branch
    monkeypatch.setattr(cj, "load_jobs", real_load)
    view = {j["id"]: j for j in cj.load_jobs()}
    assert view["job0600abcd"]["paused_reason"] == "sibling pause"
    assert view["job0000abcd"]["state"] == "completed"
