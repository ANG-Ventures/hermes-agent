"""Per-file junit + slice result manifest (CI efficiency spec I4 / Phase 4).

The flake-quarantine verdict decides per TEST whether a red slice is fully
explained by quarantined tests. It can only do that from evidence the runner
leaves behind: one junit file per test file (from the file's FINAL attempt)
and a manifest naming every failing file with its exit code. A file that hung
or crashed has no junit, and the verdict must then fail closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _run(tmp: Path, *files: Path, extra=()):
    out = tmp / "junit"
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), "-j", "2", "--file-timeout", "60",
         "--file-retries", "0", "--junit-dir", str(out),
         "--result-file", str(out / "slice-result.json"),
         "--files", ":".join(str(f) for f in files), *extra],
        cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=120,
    )
    return proc, out


def _w(p: Path, body: str) -> Path:
    p.write_text(body)
    return p


def test_manifest_and_per_file_junit_name_failing_node(tmp_path: Path) -> None:
    good = _w(tmp_path / "test_good.py", "def test_ok():\n    assert True\n")
    bad = _w(tmp_path / "test_bad.py",
             "class TestK:\n    def test_boom(self):\n        assert False\n\n"
             "def test_fine():\n    assert True\n")
    proc, out = _run(tmp_path, good, bad)
    assert proc.returncode == 1, proc.stdout
    res = json.loads((out / "slice-result.json").read_text())
    assert res["schema"] == 1
    assert res["runner_rc"] == 1
    assert res["noop_red"] is False and res["no_tests_ran_at_all"] is False
    by = {Path(f["path"]).name: f for f in res["files"]}
    assert by["test_bad.py"]["rc"] == 1 and by["test_good.py"]["rc"] == 0
    assert by["test_bad.py"]["timed_out"] is False
    junit = out / by["test_bad.py"]["junit"]
    root = ET.parse(junit).getroot()
    cases = {(c.get("classname"), c.get("name")): c for c in root.iter("testcase")}
    failed = [k for k, c in cases.items() if c.find("failure") is not None]
    assert len(failed) == 1 and failed[0][1] == "test_boom"
    # xunit1 carries the file attribute the verdict uses to rebuild node IDs.
    assert all(c.get("file") for c in cases.values())
    # The passing file also gets junit: green evidence needs "passed" records.
    assert (out / by["test_good.py"]["junit"]).is_file()


def test_timed_out_file_has_no_junit_and_is_marked(tmp_path: Path) -> None:
    hang = _w(tmp_path / "test_hang.py",
              "import time\n\ndef test_hang():\n    time.sleep(30)\n")
    proc, out = _run(tmp_path, hang, extra=("--file-timeout", "3"))
    assert proc.returncode == 1
    res = json.loads((out / "slice-result.json").read_text())
    (f,) = res["files"]
    assert f["timed_out"] is True
    assert f["junit"] is None


def test_retry_keeps_only_final_attempt_junit(tmp_path: Path) -> None:
    # Fails on the first attempt, passes on the retry: the junit on disk must
    # be the passing one (a stale failing junit would mis-name the node).
    flag = tmp_path / "flag"
    flaky = _w(tmp_path / "test_flaky.py",
               f"from pathlib import Path\nP = Path({str(flag)!r})\n\n"
               "def test_once():\n    if not P.exists():\n        P.write_text('x')\n"
               "        assert False\n")
    proc, out = _run(tmp_path, flaky, extra=("--file-retries", "1"))
    assert proc.returncode == 0, proc.stdout
    res = json.loads((out / "slice-result.json").read_text())
    (f,) = res["files"]
    assert f["rc"] == 0
    root = ET.parse(out / f["junit"]).getroot()
    assert [c for c in root.iter("testcase") if c.find("failure") is not None] == []
