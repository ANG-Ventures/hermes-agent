"""Flake ledger (spec Phase 4): classifier table + candidate scan + digest."""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import zipfile
from typing import Any

import pytest

from scripts.ci import flake_ledger as fl
from scripts.ci import flake_quarantine as fq

REPO = "ANG-Ventures/hermes-agent"
NOW = dt.datetime(2026, 9, 25, 18, 0, tzinfo=dt.timezone.utc)
FILE = "tests/tools/test_clock.py"
NODE = f"{FILE}::test_tick"


@pytest.mark.parametrize("red_sha,later_sha,later,expected", [
    ("a", "a", "success", "flake"),
    ("a", "b", "success", "fix"),
    ("a", "a", "failure", "real"),
    ("a", "b", "failure", "real"),
    ("a", "a", "cancelled", "unknown"),
])
def test_classifier_table(red_sha, later_sha, later, expected):
    red = {"conclusion": "failure", "head_sha": red_sha}
    assert fl.classify(red, {"conclusion": later, "head_sha": later_sha}) == expected


class World:
    def __init__(self):
        self.json: dict[str, Any] = {}
        self.bytes: dict[str, bytes] = {}
        self.runs: list[dict] = []
        self._aid = 5000

    def get_json(self, path):
        if path.startswith(f"repos/{REPO}/actions/workflows/ci.yaml/runs?"):
            return {"workflow_runs": self.runs if "page=1" in path else []}
        if path in self.json:
            return self.json[path]
        raise urllib.error.HTTPError(path, 404, "nf", None, None)  # type: ignore[arg-type]

    def get_bytes(self, path):
        return self.bytes[path]

    def attempt(self, run_id, n, conclusion, sha, day, outcome, event="push", latest=True):
        att = {"id": run_id, "run_attempt": n, "conclusion": conclusion, "head_sha": sha, "event": event,
               "workflow_id": 1, "run_started_at": f"{day}T0{n}:00:00Z", "repository": {"full_name": REPO}}
        self.json[f"repos/{REPO}/actions/runs/{run_id}/attempts/{n}"] = att
        if latest:
            self.runs = [r for r in self.runs if r["id"] != run_id] + [att]
        buf = io.BytesIO()
        jn = fq.junit_name(FILE)
        inner = "<failure message='x'/>" if outcome == "failed" else ""
        xml = (f'<testsuites><testsuite><testcase classname="tests.tools.test_clock" name="test_tick" '
               f'file="{FILE}">{inner}</testcase></testsuite></testsuites>')
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(jn, xml)
            zf.writestr("slice-result.json", json.dumps({"files": [{"path": FILE, "junit": jn}]}))
        self._aid += 1
        arts = self.json.setdefault(f"repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100", {"artifacts": []})
        arts["artifacts"].append({"id": self._aid, "name": f"ci-slice-result-2-a{n}", "expired": False,
                                  "workflow_run": {"id": run_id}})
        self.bytes[f"repos/{REPO}/actions/artifacts/{self._aid}/zip"] = buf.getvalue()

    def ancestor(self, sha, status="ahead"):
        self.json[f"repos/{REPO}/compare/{sha}...main"] = {"status": status}


def _two_day_flake():
    w = World()
    w.attempt(11, 1, "failure", "s1", "2026-09-20", "failed", latest=False)
    w.attempt(11, 2, "success", "s1", "2026-09-20", "passed")
    w.attempt(22, 1, "failure", "s2", "2026-09-23", "failed", latest=False)
    w.attempt(22, 2, "success", "s2", "2026-09-23", "passed")
    w.ancestor("s1")
    w.ancestor("s2")
    return w


def test_scan_proposes_entry_that_passes_the_gate():
    w = _two_day_flake()
    found, counts = fl.candidates(w, REPO, NOW, {"entries": []})
    assert counts == {"flake": 2}
    assert [e["node_id"] for e in found] == [NODE]
    e = found[0]
    assert e["owner"] == "flake-ledger" and e["until"] == "2026-10-09"
    assert fq.lint({"schema": 1, "entries": found}, NOW.date()) == []
    assert fq.verify_evidence(e, w, REPO, NOW) == []


def test_scan_one_day_only_is_not_a_candidate():
    w = _two_day_flake()
    w.json[f"repos/{REPO}/actions/runs/22/attempts/1"]["run_started_at"] = "2026-09-20T05:00:00Z"
    found, _ = fl.candidates(w, REPO, NOW, {"entries": []})
    assert found == []


def test_scan_red_red_is_real_not_candidate():
    w = World()
    w.attempt(11, 1, "failure", "s1", "2026-09-20", "failed", latest=False)
    w.attempt(11, 2, "failure", "s1", "2026-09-20", "failed")
    w.ancestor("s1")
    found, counts = fl.candidates(w, REPO, NOW, {"entries": []})
    # attempt 1 is followed by red attempt 2 (real); attempt 2 has no retry.
    assert found == [] and counts == {"real": 1, "no same-SHA retry": 1}


def test_scan_non_ancestor_sha_is_skipped():
    w = _two_day_flake()
    w.ancestor("s2", "diverged")
    found, _ = fl.candidates(w, REPO, NOW, {"entries": []})
    assert found == []


def test_scan_skips_already_active_entry():
    w = _two_day_flake()
    existing = {"entries": [{"node_id": NODE, "owner": "x", "until": "2026-10-01", "evidence": []}]}
    found, _ = fl.candidates(w, REPO, NOW, existing)
    assert found == []


def test_digest_warns_three_days_before_expiry():
    data = {"entries": [
        {"node_id": "tests/a.py::t1", "owner": "o", "until": "2026-09-27"},
        {"node_id": "tests/a.py::t2", "owner": "o", "until": "2026-10-05"},
        {"node_id": "tests/a.py::t3", "owner": "o", "until": "2026-09-01"},
    ]}
    lines = fl.digest(data, NOW.date())
    assert len(lines) == 2  # expired entry is not active (it gates again)
    assert "⚠️" in lines[0] and "t1" in lines[0]
    assert "⚠️" not in lines[1]
