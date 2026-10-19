"""canary-lane + parity-pr-check contracts (t_f34a6e21).

The lane's job is that a registered fork-feature nodeid which is skipped, renamed, deleted or
red turns the lane RED, while one that runs and holds keeps it green. The parity-pr-check's job is
that a repair commit on a sync branch cannot land without a registry entry naming its test.
Both are driven against a throwaway repo so nothing depends on the live registry's contents.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.hermes_parity import canary_lane, parity_pr_check

REPO_ROOT = Path(__file__).resolve().parents[2]

CANARY_SRC = '''
import sys
import pytest

def test_holds():
    assert True

@pytest.mark.skipif(sys.platform != "nonexistent-os", reason="only on nonexistent-os")
def test_skipped_here():
    assert True

def test_red():
    assert False, "the fork behavior is gone"

@pytest.mark.xfail(strict=True, reason="FINDING: declared")
def test_declared_xfail():
    assert False

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    assert n
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _lane_repo(tmp_path: Path, tests: list[str]) -> Path:
    repo = tmp_path / "lane"
    (repo / "tests").mkdir(parents=True)
    (repo / "docs/sync").mkdir(parents=True)
    (repo / "tests/test_canary_x.py").write_text(CANARY_SRC, encoding="utf-8")
    # the plugin is loaded by module path from the repo under test; reuse this tree's copy
    (repo / "scripts").symlink_to(REPO_ROOT / "scripts", target_is_directory=True)
    (repo / "docs/sync/fork-features.json").write_text(json.dumps([{
        "feature": "x", "tests": tests, "paths": ["tests/test_canary_x.py"], "why": "", "lifecycle": "fork-permanent",
    }]), encoding="utf-8")
    return repo


def _run(repo: Path) -> canary_lane.LaneResult:
    return canary_lane.run_lane(repo, python=sys.executable, ptb_python=None, jobs=1, timeout_s=120)


def test_passing_and_strict_xfail_nodeids_are_covered(tmp_path):
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f"{f}::test_holds", f"{f}::test_declared_xfail", f"{f}::test_param"])
    result = _run(repo)
    assert result.ok, result.failures
    assert result.executed[f"{f}::test_holds"] == "passed"
    assert result.executed[f"{f}::test_declared_xfail"] == "xfailed"
    # a parametrized base nodeid expands to every case, each counted as executed
    assert {f"{f}::test_param[1]", f"{f}::test_param[2]"} <= set(result.executed)


def test_skip_on_this_platform_is_red_not_covered(tmp_path):
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f"{f}::test_holds", f"{f}::test_skipped_here"])
    result = _run(repo)
    assert not result.ok
    assert any(line.startswith(f"SKIPPED {f}::test_skipped_here") and "coverage_gap" in line for line in result.failures)
    assert f"{f}::test_skipped_here" not in result.executed


def test_file_registration_is_red_only_when_nothing_in_it_runs(tmp_path):
    repo = _lane_repo(tmp_path, ["tests/test_only_skips.py"])
    (repo / "tests/test_only_skips.py").write_text(
        "import pytest\n\n@pytest.mark.skip(reason='needs a script outside the repo')\ndef test_a():\n    pass\n",
        encoding="utf-8")
    result = _run(repo)
    assert len(result.failures) == 1 and result.failures[0].startswith("SKIPPED tests/test_only_skips.py: every member")


def test_skipped_member_of_a_running_file_registration_is_a_warning(tmp_path):
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f])
    (repo / f).write_text(CANARY_SRC.replace("def test_red():\n    assert False", "def test_red():\n    assert True"),
                          encoding="utf-8")
    result = _run(repo)
    assert result.ok, result.failures
    assert [w for w in result.warnings if "test_skipped_here" in w]


def test_renamed_or_deleted_nodeid_is_red_with_repoint_remedy(tmp_path):
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f"{f}::test_holds", f"{f}::test_renamed_away", "tests/test_gone.py::test_y"])
    result = _run(repo)
    missing = [line for line in result.failures if line.startswith("NOT COLLECTED")]
    assert {m.split()[2] for m in missing} == {f"{f}::test_renamed_away", "tests/test_gone.py::test_y"}
    assert all("repoint" in m for m in missing)
    assert result.executed.get(f"{f}::test_holds") == "passed"   # one rotten nodeid never smears the healthy ones


def test_red_canary_is_red(tmp_path):
    f = "tests/test_canary_x.py"
    result = _run(_lane_repo(tmp_path, [f"{f}::test_red"]))
    assert result.failures == [f"RED {f}::test_red: failed"]


def test_xfail_run_false_never_executes_so_it_is_not_coverage(tmp_path):
    # Prism c001f9d1a1f7: xfail(run=False) reports from setup; the body never runs
    repo = _lane_repo(tmp_path, ["tests/test_guard.py::test_guard"])
    (repo / "tests/test_guard.py").write_text(
        "import pytest\n\n@pytest.mark.xfail(run=False, strict=True, reason='x')\ndef test_guard():\n    assert False\n",
        encoding="utf-8")
    result = _run(repo)
    assert not result.ok and result.failures[0].startswith("SKIPPED tests/test_guard.py::test_guard")
    assert not result.executed


def test_nonzero_pytest_exit_with_passing_items_is_red(tmp_path):
    # Prism 808f1e4f5851 / 38481748ef84: a session hook (or interrupted teardown) fails the run after passing items
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f"{f}::test_holds"])
    (repo / "tests/conftest.py").write_text("def pytest_sessionfinish(session):\n    session.exitstatus = 1\n",
                                            encoding="utf-8")
    result = _run(repo)
    assert len(result.failures) == 1 and result.failures[0].startswith(f"PYTEST EXIT {f}: rc 1")


def test_member_whose_body_never_ran_is_red_not_a_skipped_member(tmp_path):
    # Prism 6c8f20320fee (Apollo-proven on 0ab6cf0e): setup passed, no call report (pytest.exit before the
    # call, a session hook that ends the run), sibling passed -> the old classifier filed it as a skipped
    # member (WARN) and the lane read GREEN while the guard never executed.
    f = "tests/test_canary_x.py"
    data = {"collected": [f + "::test_a", f + "::test_b"], "errors": [], "rc": 0, "reports": [
        {"nodeid": f + "::test_a", "when": w, "outcome": "passed", "xfail": False, "reason": ""}
        for w in ("setup", "call", "teardown")] + [
        {"nodeid": f + "::test_b", "when": w, "outcome": "passed", "xfail": False, "reason": ""}
        for w in ("setup", "teardown")]}
    result = canary_lane.LaneResult()
    canary_lane._classify_checked(f, [f], data, result)
    assert not result.ok
    assert result.failures == [f"NO CALL {f}::test_b: setup passed but the test body never ran (session ended "
                               "before the call; not coverage)"]


def test_interrupt_in_fixture_teardown_is_red(tmp_path):
    repo = _lane_repo(tmp_path, ["tests/test_td.py::test_a"])
    (repo / "tests/test_td.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef fx():\n    yield\n    raise KeyboardInterrupt\n\n"
        "def test_a(fx):\n    assert True\n", encoding="utf-8")
    assert not _run(repo).ok


def test_changed_from_sees_both_sides_of_a_rename(tmp_path):
    # Prism e85a3140eaba: git diff --name-only lists only the new path of a rename by default
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tests").mkdir()
    (repo / "tests/test_guarded.py").write_text("def test_a():\n    assert 1 + 1 == 2\n" * 5, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    _git(repo, "mv", "tests/test_guarded.py", "tests/test_unregistered_name.py")
    _git(repo, "commit", "-q", "-m", "rename")
    assert "tests/test_guarded.py" in canary_lane.changed_from(repo, "main")


def test_inherited_pytest_addopts_cannot_deselect_registered_members(tmp_path, monkeypatch):
    # Prism 7ca5450ad8e2
    f = "tests/test_canary_x.py"
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_holds")
    result = _run(_lane_repo(tmp_path, [f]))
    assert f"RED {f}::test_red: failed" in result.failures


def test_changed_from_returns_non_ascii_paths_unquoted(tmp_path):
    # Prism 5b1bbed43d18: default core.quotePath C-quotes non-ASCII names
    repo = tmp_path / "q"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "tests").mkdir()
    (repo / "tests/test_café.py").write_text("def test_a(): pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add")
    assert canary_lane.changed_from(repo, "main") == ["tests/test_café.py"]


def test_ptb_nodeids_without_a_ptb_interpreter_are_red_not_dropped(tmp_path):
    repo = _lane_repo(tmp_path, ["tests/test_thing_ptb.py::test_a"])
    result = _run(repo)
    assert result.failures and "python-telegram-bot" in result.failures[0]


def test_affected_reads_registry_canaries_registered_tests_and_paths(tmp_path):
    f = "tests/test_canary_x.py"
    repo = _lane_repo(tmp_path, [f"{f}::test_holds"])
    manifest = repo / "docs/sync/fork-features.json"
    entry = json.loads(manifest.read_text(encoding="utf-8"))
    entry[0]["paths"] = ["gateway/run_turn.py"]
    entry[0]["tests"] = ["tests/gateway/test_footer.py::test_a"]
    manifest.write_text(json.dumps(entry), encoding="utf-8")
    for changed in (["docs/sync/fork-features.json"], ["tests/fork_canaries/test_new.py"],
                    ["tests/gateway/test_footer.py"], ["gateway/run_turn.py"], ["scripts/hermes_parity/cli.py"]):
        assert canary_lane.is_affected(manifest, changed)[0], changed
    assert not canary_lane.is_affected(manifest, ["README.md", "gateway/other.py"])[0]


# ---- parity-pr-check -------------------------------------------------------------------------------

def _sync_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sync"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "docs/sync").mkdir(parents=True)
    (repo / "docs/sync/fork-features.json").write_text("[]\n", encoding="utf-8")
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "upstream")
    _git(repo, "checkout", "-q", "upstream")
    (repo / "up.py").write_text("u = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "upstream work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "sync/upstream-2026-10-18")
    _git(repo, "merge", "-q", "--no-ff", "-m", "parity: upstream sync", "upstream")
    return repo


def _commit(repo: Path, msg: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", msg)


def _check(repo: Path) -> list[str]:
    return parity_pr_check.check(repo, base="main", head="HEAD", upstream="upstream")[0]


def test_repair_commit_without_a_test_is_refused(tmp_path):
    repo = _sync_repo(tmp_path)
    _commit(repo, "fix(gateway): restore footer kwargs the merge dropped", {"src.py": "x = 2\n"})
    errors = _check(repo)
    assert len(errors) == 1 and "restore without a canary" in errors[0]


def test_repair_commit_with_an_unregistered_test_is_refused(tmp_path):
    repo = _sync_repo(tmp_path)
    _commit(repo, "fix: restore x regressed by parity merge", {"src.py": "x = 2\n", "tests/test_x.py": "def test_x(): pass\n"})
    errors = _check(repo)
    assert len(errors) == 1 and "canary not registered" in errors[0]


def test_test_only_fix_and_already_registered_test_and_exempt_trailer_pass(tmp_path):
    repo = _sync_repo(tmp_path)
    # the base already guards tests/test_y.py; a sync repair that updates it needs no new row
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "register y", {"tests/test_y.py": "def test_y(): pass\n", "docs/sync/fork-features.json": json.dumps([
        {"feature": "y", "tests": ["tests/test_y.py"], "paths": ["src.py"], "why": "", "lifecycle": "fork-permanent"}])})
    _git(repo, "checkout", "-q", "sync/upstream-2026-10-18")
    _git(repo, "merge", "-q", "--no-edit", "main")
    base = _git(repo, "rev-parse", "main")
    _commit(repo, "fix(tests): heal a flaky fixture on the parity branch", {"tests/test_other.py": "def test_o(): pass\n"})
    _commit(repo, "fix: restore y regressed by the merge", {"src.py": "x = 3\n", "tests/test_y.py": "def test_y(): assert 1\n"})
    _commit(repo, "fix(ci): pin a tool the merge bumped\n\nCanary-Exempt: CI tool pin, no fork behavior restored",
            {"tool.cfg": "v=2\n"})
    errors, exempt = parity_pr_check.check(repo, base=base, head="HEAD", upstream="upstream")
    assert errors == []
    assert len(exempt) == 1 and "CI tool pin" in exempt[0]


def test_repair_commit_with_a_registered_test_passes(tmp_path):
    repo = _sync_repo(tmp_path)
    _commit(repo, "fix: restore x regressed by parity merge", {"src.py": "x = 2\n", "tests/test_x.py": "def test_x(): pass\n"})
    _commit(repo, "fork-features: register x", {"docs/sync/fork-features.json": json.dumps([
        {"feature": "x", "tests": ["tests/test_x.py::test_x"], "paths": ["src.py"], "why": "", "lifecycle": "fork-permanent"},
    ])})
    assert _check(repo) == []


def test_parity_classifier_reads_the_conventions_past_syncs_used():
    # measured: #510 parity-merge-20260807, #643 sync/upstream-2026-08-29, #1624 "parity: upstream sync ..."
    assert parity_pr_check.is_parity_pr("merge: parity sync with upstream (3,466 commits)", "parity-merge-20260807", 3503)
    assert parity_pr_check.is_parity_pr("anything", "sync/upstream-2026-08-29", 12)
    assert parity_pr_check.is_parity_pr("parity: upstream sync 2026-10-01 (NousResearch 612d8e44 into fork)", "x", 0)
    assert parity_pr_check.is_parity_pr("Merge upstream parity into sync/upstream-x", "", 0)
    assert parity_pr_check.is_parity_pr("big", "feature/x", 1001)
    for title, head in (("deploy-parity: record lane", "fix/x"), ("fix(pool): pool_parity lint", "a/b"),
                        ("test(parity): pin claude-apx label", "t/y")):
        assert not parity_pr_check.is_parity_pr(title, head, 5), title


def test_only_fork_side_repair_commits_count(tmp_path):
    repo = _sync_repo(tmp_path)
    # an upstream commit titled fix arrives through the merge: it is upstream's, not a fork repair
    _git(repo, "checkout", "-q", "upstream")
    _commit(repo, "fix: upstream bug", {"up.py": "u = 2\n"})
    _git(repo, "checkout", "-q", "sync/upstream-2026-10-18")
    _git(repo, "merge", "-q", "--no-ff", "-m", "parity: upstream sync 2", "upstream")
    _commit(repo, "chore: resolve conflict notes", {"notes.md": "n\n"})
    assert _check(repo) == []


def test_empty_exempt_trailer_does_not_borrow_the_next_line_as_its_reason(tmp_path):
    # Prism f59b188cd0eb: `\s*` after the colon crossed the newline into `Signed-off-by: ...`
    repo = _sync_repo(tmp_path)
    _commit(repo, "fix: restore x regressed by parity merge\n\nCanary-Exempt:\nSigned-off-by: Some One <s@example.com>",
            {"src.py": "x = 2\n"})
    errors, exempt = parity_pr_check.check(repo, base="main", head="HEAD", upstream="upstream")
    assert exempt == []
    assert len(errors) == 1 and "restore without a canary" in errors[0]


def test_new_test_in_a_file_registered_only_by_another_nodeid_is_refused(tmp_path):
    # Prism bcadb1cf511b: the registry names file::test_old, the repair adds test_new in the same file;
    # the lane never runs test_new, so the repair is not guarded
    repo = _sync_repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "register old", {"tests/test_z.py": "def test_old():\n    assert 1\n",
                                   "docs/sync/fork-features.json": json.dumps([
        {"feature": "z", "tests": ["tests/test_z.py::test_old"], "paths": ["src.py"], "why": "", "lifecycle": "fork-permanent"}])})
    _git(repo, "checkout", "-q", "sync/upstream-2026-10-18")
    _git(repo, "merge", "-q", "--no-edit", "main")
    base = _git(repo, "rev-parse", "main")
    _commit(repo, "fix: restore z regressed by the merge",
            {"src.py": "x = 4\n", "tests/test_z.py": "def test_old():\n    assert 1\n\n\ndef test_new():\n    assert 2\n"})
    errors, _ = parity_pr_check.check(repo, base=base, head="HEAD", upstream="upstream")
    assert len(errors) == 1 and "canary not registered" in errors[0]
    # changing the registered nodeid itself (or registering the new one) guards the repair
    _commit(repo, "fix: restore z harder", {"src.py": "x = 5\n", "tests/test_z.py":
            "def test_old():\n    assert 3\n\n\ndef test_new():\n    assert 2\n"})
    errors, _ = parity_pr_check.check(repo, base=base, head="HEAD", upstream="upstream")
    assert len(errors) == 1 and "restore z regressed" in errors[0]
    _commit(repo, "fork-features: register z new", {"docs/sync/fork-features.json": json.dumps([
        {"feature": "z", "tests": ["tests/test_z.py::test_old", "tests/test_z.py::test_new"], "paths": ["src.py"],
         "why": "", "lifecycle": "fork-permanent"}])})
    assert parity_pr_check.check(repo, base=base, head="HEAD", upstream="upstream")[0] == []


def test_class_and_parametrized_registrations_cover_their_changed_members(tmp_path):
    repo = _sync_repo(tmp_path)
    _commit(repo, "fix: restore c regressed by parity merge", {"src.py": "x = 2\n", "tests/test_c.py":
            "import pytest\n\nclass TestC:\n    def test_m(self):\n        assert 1\n\n\n"
            "@pytest.mark.parametrize('n', [1])\ndef test_p(n):\n    assert n\n"})
    _commit(repo, "fork-features: register c", {"docs/sync/fork-features.json": json.dumps([
        {"feature": "c", "tests": ["tests/test_c.py::TestC"], "paths": ["src.py"], "why": "", "lifecycle": "fork-permanent"}])})
    assert _check(repo) == []
    _commit(repo, "fix: restore p regressed by parity merge", {"src.py": "x = 3\n", "tests/test_c.py":
            "import pytest\n\nclass TestC:\n    def test_m(self):\n        assert 1\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\ndef test_p(n):\n    assert n\n"})
    assert len(_check(repo)) == 1
    _commit(repo, "fork-features: register p", {"docs/sync/fork-features.json": json.dumps([
        {"feature": "c", "tests": ["tests/test_c.py::TestC", "tests/test_c.py::test_p[1]"], "paths": ["src.py"],
         "why": "", "lifecycle": "fork-permanent"}])})
    assert _check(repo) == []


def test_repair_test_with_a_non_ascii_path_is_seen(tmp_path):
    # Prism b83db54ce8eb: core.quotePath quoted the path, so _is_test_file never matched it
    repo = _sync_repo(tmp_path)
    _git(repo, "config", "core.quotePath", "true")
    _commit(repo, "fix: restore e regressed by parity merge", {"src.py": "x = 2\n", "tests/test_café.py": "def test_e(): pass\n"})
    _commit(repo, "fork-features: register e", {"docs/sync/fork-features.json": json.dumps([
        {"feature": "e", "tests": ["tests/test_café.py"], "paths": ["src.py"], "why": "", "lifecycle": "fork-permanent"}])})
    assert _check(repo) == []
    assert "tests/test_café.py" in parity_pr_check.touched_files(repo, _git(repo, "rev-parse", "HEAD~1"))
