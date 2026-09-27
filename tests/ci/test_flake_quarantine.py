"""Flake quarantine (CI efficiency spec I4 / Phase 4): verdict, lint, evidence gate.

Negative proofs named in spec §6 Phase 4:
  (a) a PR that quarantines the test it broke stays RED (the verdict reads the
      BASE list; the head list's new entry has no evidence);
  (b) a quarantined test that HANGS while a sibling breaks is RED;
  (c) a backdated ``until:`` makes the test gating again with no human action;
  (d) fabricated / insufficient / same-branch / wrong-junit evidence is RED;
  (e) ``until:`` moved later without fresh evidence is RED.
"""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import flake_quarantine as fq

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_tests_parallel.py"
REPO = "ANG-Ventures/hermes-agent"
TODAY = dt.date(2026, 9, 25)
NOW = dt.datetime(2026, 9, 25, 18, 0, tzinfo=dt.timezone.utc)
NODE = "tests/ci/test_widget.py::TestW::test_flaky[a-1]"


def _entry(node=NODE, until="2026-10-05", owner="daedalus", evidence=None, card="t_162ffd04"):
    return {"node_id": node, "card": card, "owner": owner, "until": until, "reason": "flake",
            "evidence": evidence if evidence is not None else []}


def _qlist(*entries):
    return {"schema": 1, "entries": list(entries)}


# ── lint ────────────────────────────────────────────────────────────────────

def test_lint_accepts_well_formed_entry():
    assert fq.lint(_qlist(_entry()), TODAY) == []


@pytest.mark.parametrize("mut,frag", [
    (lambda e: e.pop("owner"), "missing owner"),
    (lambda e: e.pop("card"), "malformed card"),
    (lambda e: e.update(card="flake-ledger"), "malformed card"),
    (lambda e: e.update(card="t_123"), "malformed card"),
    (lambda e: e.update(owner="  "), "missing owner"),
    (lambda e: e.pop("until"), "until"),
    (lambda e: e.update(until="next week"), "until"),
    (lambda e: e.update(until="2026-10-30"), "more than 14 days"),
    (lambda e: e.update(node_id="TestW::test_flaky"), "node_id"),
])
def test_lint_rejects_missing_or_malformed_owner_until(mut, frag):
    e = _entry()
    mut(e)
    problems = fq.lint(_qlist(e), TODAY)
    assert any(frag in p for p in problems), problems


def test_lint_does_not_fail_on_expiry():
    # Expiry is one behaviour: the entry stops exempting. It is not a lint error.
    assert fq.lint(_qlist(_entry(until="2026-09-01")), TODAY) == []


def test_lint_rejects_duplicate_node():
    assert any("duplicate" in p for p in fq.lint(_qlist(_entry(), _entry()), TODAY))


def test_committed_list_passes_lint():
    data = fq.parse_list((ROOT / fq.LIST_PATH).read_text())
    assert fq.lint(data, dt.datetime.now(dt.timezone.utc).date()) == []


# ── junit node IDs ──────────────────────────────────────────────────────────

def _junit(cases):
    """cases: list of (file, classname, name, outcome) in pytest xunit1 shape."""
    body = []
    for file, cls, name, outcome in cases:
        inner = {"failed": "<failure message='x'>boom</failure>",
                 "error": "<error message='x'>boom</error>",
                 "skipped": "<skipped message='x'/>", "passed": ""}[outcome]
        body.append(f'<testcase classname="{cls}" name="{name}" file="{file}" line="1" time="0.1">{inner}</testcase>')
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
            f'tests="{len(cases)}">' + "".join(body) + "</testsuite></testsuites>").encode()


def test_junit_rebuilds_exact_node_ids():
    xml = _junit([
        ("tests/ci/test_widget.py", "tests.ci.test_widget.TestW", "test_flaky[a-1]", "failed"),
        ("tests/ci/test_widget.py", "tests.ci.test_widget", "test_mod", "passed"),
        ("tests/ci/test_widget.py", "tests.ci.test_widget", "test_skip", "skipped"),
    ])
    failed, passed = fq.junit_outcomes(xml, "tests/ci/test_widget.py")
    assert failed == {NODE}
    assert passed == {"tests/ci/test_widget.py::test_mod"}


def test_junit_same_name_other_file_is_a_different_node():
    xml = _junit([("tests/other/test_widget.py", "tests.other.test_widget.TestW", "test_flaky[a-1]", "failed")])
    failed, _ = fq.junit_outcomes(xml, "tests/other/test_widget.py")
    assert NODE not in failed


def test_junit_unrebuildable_failing_node_raises():
    xml = _junit([("tests/ci/test_widget.py", "somewhere.else.TestW", "test_x", "failed")])
    with pytest.raises(fq.JunitError):
        fq.junit_outcomes(xml, "tests/ci/test_widget.py")


# ── slice verdict (unit) ────────────────────────────────────────────────────

def _slice(tmp: Path, cases, rc=1, timed_out=False, junit=True, **top):
    jdir = tmp / "junit"
    jdir.mkdir(exist_ok=True)
    rel = "tests/ci/test_widget.py"
    name = fq.junit_name(rel)
    if junit:
        (jdir / name).write_bytes(_junit(cases))
    result = {"schema": 1, "runner_rc": 1 if rc else 0, "noop_red": False, "no_tests_ran_at_all": False,
              "files": [{"path": rel, "rc": rc, "timed_out": timed_out, "junit": name if junit else None},
                        {"path": "tests/ci/test_ok.py", "rc": 0, "timed_out": False, "junit": None}]}
    result.update(top)
    return result, jdir


FLAKY_FAIL = ("tests/ci/test_widget.py", "tests.ci.test_widget.TestW", "test_flaky[a-1]", "failed")
SIBLING_FAIL = ("tests/ci/test_widget.py", "tests.ci.test_widget.TestW", "test_sibling", "failed")
SIBLING_PASS = ("tests/ci/test_widget.py", "tests.ci.test_widget.TestW", "test_sibling", "passed")


def test_only_quarantined_failures_is_green_and_still_reported(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL, SIBLING_PASS])
    ok, msgs = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert ok
    assert any("QUARANTINED" in m and NODE in m for m in msgs)


def test_a_self_exemption_base_list_lacks_entry_is_red(tmp_path):
    # (a): the PR's head list adds NODE, but the verdict reads the BASE list.
    result, jdir = _slice(tmp_path, [FLAKY_FAIL])
    ok, _ = fq.slice_verdict(result, jdir, _qlist(), TODAY)
    assert not ok


def test_non_quarantined_sibling_failure_is_red(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL, SIBLING_FAIL])
    ok, msgs = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok and any("test_sibling" in m and "not quarantined" in m for m in msgs)


def test_b_quarantined_test_hangs_is_red(tmp_path):
    # (b): a hang is a timeout with no junit — absent results count as gating.
    result, jdir = _slice(tmp_path, [], rc=124, timed_out=True, junit=False)
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok


def test_missing_junit_is_red(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL], junit=False)
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok


def test_c_backdated_until_re_gates(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL])
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry(until="2026-09-24")), TODAY)
    assert not ok
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry(until="2026-09-25")), TODAY)
    assert ok  # until is inclusive


@pytest.mark.parametrize("override", [
    {"noop_red": True}, {"no_tests_ran_at_all": True}, {"schema": 2},
])
def test_noop_or_bad_manifest_is_red(tmp_path, override):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL], **override)
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok


def test_no_manifest_is_red(tmp_path):
    ok, _ = fq.slice_verdict(None, tmp_path, _qlist(_entry()), TODAY)
    assert not ok


def test_nonzero_exit_without_failing_testcase_is_red(tmp_path):
    result, jdir = _slice(tmp_path, [SIBLING_PASS])  # rc 1, but junit shows no failure
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok


def test_collection_error_rc2_is_red(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL], rc=2)
    ok, _ = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert not ok


def test_entry_without_owner_never_exempts(tmp_path):
    e = _entry()
    e.pop("owner")
    result, jdir = _slice(tmp_path, [FLAKY_FAIL])
    ok, _ = fq.slice_verdict(result, jdir, _qlist(e), TODAY)
    assert not ok


@pytest.mark.parametrize("card", [None, "", "flake-ledger", "t_XYZ"])
def test_entry_without_fix_card_never_exempts(tmp_path, card):
    # t_162ffd04: never quarantine silently; an entry must name its fix card.
    e = _entry(card=card)
    result, jdir = _slice(tmp_path, [FLAKY_FAIL])
    ok, _ = fq.slice_verdict(result, jdir, _qlist(e), TODAY)
    assert not ok


def test_quarantined_verdict_names_card_and_owner(tmp_path):
    result, jdir = _slice(tmp_path, [FLAKY_FAIL])
    ok, msgs = fq.slice_verdict(result, jdir, _qlist(_entry()), TODAY)
    assert ok and any("card t_162ffd04" in m and "owner daedalus" in m for m in msgs)


# ── slice verdict (E2E through the real runner + CLI) ───────────────────────

def _run_runner(tmp: Path, *files: Path):
    out = tmp / "junit"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "-j", "2", "--file-timeout", "60", "--file-retries", "0",
         "--junit-dir", str(out), "--result-file", str(out / "slice-result.json"),
         "--files", ":".join(str(f) for f in files)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return proc, out


def _cli_verdict(out: Path, qfile: Path | None, outcome: str = "failure"):
    args = [sys.executable, str(ROOT / "scripts/ci/flake_quarantine.py"), "slice-verdict",
            "--tests-outcome", outcome, "--result", str(out / "slice-result.json"),
            "--junit-dir", str(out), "--today", TODAY.isoformat()]
    if qfile is not None:
        args += ["--quarantine", str(qfile)]
    return subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_e2e_real_runner_quarantined_failure_runs_reports_and_does_not_gate(tmp_path):
    tf = tmp_path / "test_real.py"
    tf.write_text("class TestK:\n    def test_flaky(self):\n        assert False\n\n"
                  "def test_sibling():\n    assert True\n")
    proc, out = _run_runner(tmp_path, tf)
    assert proc.returncode == 1  # the test RAN and failed
    node = f"{tf}::TestK::test_flaky"
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_qlist(_entry(node=node))))
    v = _cli_verdict(out, q)
    assert v.returncode == 0, v.stdout
    assert "QUARANTINED" in v.stdout and node in v.stdout
    # Same run, no base list (bootstrap / self-exemption): RED.
    assert _cli_verdict(out, None).returncode == 1


def test_e2e_real_runner_hang_plus_sibling_break_is_red(tmp_path):
    hang = tmp_path / "test_hang.py"
    hang.write_text("import time\n\ndef test_flaky():\n    time.sleep(30)\n")
    sib = tmp_path / "test_sib.py"
    sib.write_text("def test_sibling():\n    assert False\n")
    out = tmp_path / "junit"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "-j", "2", "--file-timeout", "3", "--file-retries", "0",
         "--junit-dir", str(out), "--result-file", str(out / "slice-result.json"),
         "--files", f"{hang}:{sib}"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    assert proc.returncode == 1
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_qlist(_entry(node=f"{hang}::test_flaky"))))
    assert _cli_verdict(out, q).returncode == 1


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", ""])
def test_cli_non_failure_non_success_outcome_is_red(tmp_path, outcome):
    assert _cli_verdict(tmp_path, None, outcome).returncode == 1


# ── evidence gate ───────────────────────────────────────────────────────────

class FakeApi:
    def __init__(self):
        self.json: dict[str, Any] = {}
        self.bytes: dict[str, bytes] = {}

    def get_json(self, path):
        if path in self.json:
            return copy.deepcopy(self.json[path])
        raise urllib.error.HTTPError(path, 404, "Not Found", None, None)  # type: ignore[arg-type]

    def get_bytes(self, path):
        if path in self.bytes:
            return self.bytes[path]
        raise urllib.error.HTTPError(path, 404, "Not Found", None, None)  # type: ignore[arg-type]


_ART_ID = [1000]


def _add_attempt(api, run_id, attempt, *, conclusion, sha, day, outcome, event="push",
                 repo=REPO, workflow_id=340224912, node=NODE, with_junit=True):
    api.json[f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}"] = {
        "id": run_id, "run_attempt": attempt, "conclusion": conclusion, "head_sha": sha,
        "event": event, "workflow_id": workflow_id, "run_started_at": f"{day}T10:00:00Z",
        "repository": {"full_name": repo}}
    arts = api.json.setdefault(f"repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100", {"artifacts": []})
    if not with_junit:
        return
    rel, rest = node.split("::", 1)
    parts = rest.split("::")
    cls = ".".join([rel[:-3].replace("/", "."), *parts[:-1]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(fq.junit_name(rel), _junit([(rel, cls, parts[-1], outcome)]))
        zf.writestr("slice-result.json", "{}")
    _ART_ID[0] += 1
    aid = _ART_ID[0]
    arts["artifacts"].append({"id": aid, "name": f"ci-slice-result-3-a{attempt}", "expired": False,
                              "workflow_run": {"id": run_id}})
    api.bytes[f"repos/{REPO}/actions/artifacts/{aid}/zip"] = zipfile_bytes = buf.getvalue()
    assert zipfile_bytes


def _ancestor(api, sha, status="ahead"):
    api.json[f"repos/{REPO}/compare/{sha}...main"] = {"status": status}


def _good_world():
    """Two same-SHA red->green pairs on two days, push:main, junit agrees."""
    api = FakeApi()
    _add_attempt(api, 501, 1, conclusion="failure", sha="a" * 40, day="2026-09-20", outcome="failed")
    _add_attempt(api, 501, 2, conclusion="success", sha="a" * 40, day="2026-09-20", outcome="passed")
    _add_attempt(api, 602, 1, conclusion="failure", sha="b" * 40, day="2026-09-22", outcome="failed", event="merge_group")
    _add_attempt(api, 603, 1, conclusion="success", sha="b" * 40, day="2026-09-22", outcome="passed", event="merge_group")
    _ancestor(api, "a" * 40)
    _ancestor(api, "b" * 40, "identical")
    evidence = [{"red": {"run_id": 501, "attempt": 1}, "green": {"run_id": 501, "attempt": 2}},
                {"red": {"run_id": 602, "attempt": 1}, "green": {"run_id": 603, "attempt": 1}}]
    return api, evidence


def test_good_evidence_passes():
    api, ev = _good_world()
    assert fq.verify_evidence(_entry(evidence=ev), api, REPO, NOW) == []


def _problems(mutate):
    api, ev = _good_world()
    mutate(api, ev)
    return fq.verify_evidence(_entry(evidence=ev), api, REPO, NOW)


@pytest.mark.parametrize("name,mutate,frag", [
    ("fabricated run id", lambda a, ev: ev[0]["red"].update(run_id=999999), "condition 1"),
    ("run of another repo", lambda a, ev: a.json[f"repos/{REPO}/actions/runs/501/attempts/1"]["repository"].update(full_name="evil/fork"), "condition 1"),
    ("green is red", lambda a, ev: a.json[f"repos/{REPO}/actions/runs/501/attempts/2"].update(conclusion="failure"), "condition 2"),
    ("pair on two SHAs", lambda a, ev: a.json[f"repos/{REPO}/actions/runs/603/attempts/1"].update(head_sha="c" * 40), "condition 2"),
    ("pull_request evidence", lambda a, ev: a.json[f"repos/{REPO}/actions/runs/501/attempts/1"].update(event="pull_request"), "condition 3"),
    ("red junit does not name node", lambda a, ev: (_add_attempt(a, 777, 1, conclusion="failure", sha="a" * 40, day="2026-09-20", outcome="failed", node="tests/ci/test_widget.py::TestW::test_other"), ev[0]["red"].update(run_id=777)), "condition 3"),
    ("red junit shows pass", lambda a, ev: (_add_attempt(a, 778, 1, conclusion="failure", sha="a" * 40, day="2026-09-20", outcome="passed"), ev[0]["red"].update(run_id=778)), "condition 3"),
    ("no junit artifact", lambda a, ev: (_add_attempt(a, 779, 1, conclusion="failure", sha="a" * 40, day="2026-09-20", outcome="failed", with_junit=False), ev[0]["red"].update(run_id=779)), "condition 3"),
    ("same-branch SHA", lambda a, ev: _ancestor(a, "a" * 40, "diverged"), "condition 4"),
    ("stale evidence", lambda a, ev: a.json[f"repos/{REPO}/actions/runs/501/attempts/1"].update(run_started_at="2026-09-01T10:00:00Z"), "window"),
])
def test_d_fabricated_or_insufficient_evidence_is_red(name, mutate, frag):
    problems = _problems(mutate)
    assert any(frag in p for p in problems), (name, problems)
    assert any("verified pair" in p for p in problems), (name, problems)


def test_d_single_pair_is_insufficient():
    api, ev = _good_world()
    problems = fq.verify_evidence(_entry(evidence=ev[:1]), api, REPO, NOW)
    assert problems


def test_d_duplicated_pair_does_not_count_twice():
    api, ev = _good_world()
    problems = fq.verify_evidence(_entry(evidence=[ev[0], copy.deepcopy(ev[0])]), api, REPO, NOW)
    assert any("duplicate" in p for p in problems)


def test_two_pairs_same_day_is_sufficient():
    # t_162ffd04: >= 2 same-SHA red->green pairs inside one day qualifies; the
    # old 2-day floor let a flake eject the merge queue for two days first.
    api, ev = _good_world()
    for rid in (602, 603):
        api.json[f"repos/{REPO}/actions/runs/{rid}/attempts/1"]["run_started_at"] = "2026-09-20T12:00:00Z"
    assert fq.verify_evidence(_entry(evidence=ev), api, REPO, NOW) == []


def test_d_one_pair_is_still_insufficient():
    api, ev = _good_world()
    problems = fq.verify_evidence(_entry(evidence=ev[:1]), api, REPO, NOW)
    assert any(">= 2" in p for p in problems)


# ── diff rule (p5 RC-H) ─────────────────────────────────────────────────────

def test_diff_add_change_extend_need_evidence_delete_shorten_do_not():
    base = _qlist(_entry(), _entry(node="tests/a/test_b.py::test_c"))
    # delete + shorten: evidence-free
    head = _qlist(_entry(until="2026-09-30"))
    assert fq.diff_needing_evidence(base, head) == []
    # (e) extend: needs evidence
    head = _qlist(_entry(until="2026-10-06"), _entry(node="tests/a/test_b.py::test_c"))
    assert [e["node_id"] for e, _ in fq.diff_needing_evidence(base, head)] == [NODE]
    # key change (a parametrize id moved) is an add
    head = _qlist(_entry(node=NODE.replace("a-1", "a-2")))
    assert len(fq.diff_needing_evidence(base, head)) == 1
    # shorten AND change owner: not evidence-free
    head = _qlist(_entry(until="2026-09-30", owner="someone"))
    assert len(fq.diff_needing_evidence(base, head)) == 1


def test_e_until_moved_later_without_fresh_evidence_is_red():
    api, ev = _good_world()
    base = _qlist(_entry(evidence=ev))
    head = _qlist(_entry(until="2026-10-08", evidence=[]))
    assert fq.check_changes(base, head, api, REPO, NOW)


def test_a_pr_adding_its_own_entry_without_evidence_is_red():
    assert fq.check_changes(_qlist(), _qlist(_entry()), FakeApi(), REPO, NOW)


def test_bot_shaped_pr_with_fabricated_ids_is_red():
    # The PR shape is irrelevant: a perfectly formatted ledger-bot entry whose
    # run IDs do not resolve is RED (evidence is re-derived, never trusted).
    ev = [{"red": {"run_id": 11, "attempt": 1}, "green": {"run_id": 11, "attempt": 2}},
          {"red": {"run_id": 12, "attempt": 1}, "green": {"run_id": 12, "attempt": 2}}]
    e = _entry(evidence=ev)
    e["reason"] = "flake-ledger bot: 2 same-SHA red->green pairs on 2 days"
    assert fq.check_changes(_qlist(), _qlist(e), FakeApi(), REPO, NOW)


def test_good_add_passes_check_changes():
    api, ev = _good_world()
    assert fq.check_changes(_qlist(), _qlist(_entry(evidence=ev)), api, REPO, NOW) == []


def test_shorten_only_passes_without_api():
    api, ev = _good_world()
    base = _qlist(_entry(evidence=ev))
    head = _qlist(_entry(until="2026-09-27", evidence=ev))
    assert fq.check_changes(base, head, FakeApi(), REPO, NOW) == []
