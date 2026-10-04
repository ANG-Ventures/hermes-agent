"""The Windows install+update E2E legs must cover every journey file exactly once.

The workflow lists the files per matrix leg explicitly (one batch of journeys per hosted
4-vCPU runner). A new ``test_*.py`` under tests/e2e/core/windows_update that is not in a
leg would never run in CI, so the coverage is checked here against the directory.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-install-update-e2e.yml"
JOURNEYS = ROOT / "tests" / "e2e" / "core" / "windows_update"


def _legs() -> dict[str, list[str]]:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["install-update"]
    return {row["leg"]: row["files"].split() for row in job["strategy"]["matrix"]["include"]}


def test_every_journey_file_runs_in_exactly_one_leg() -> None:
    on_disk = sorted(p.relative_to(ROOT).as_posix() for p in JOURNEYS.glob("test_*.py"))
    assert on_disk, "journey discovery must not silently become empty"
    listed = sorted(f for files in _legs().values() for f in files)
    assert listed == on_disk


def test_run_step_uses_the_legs_files() -> None:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["install-update"]
    run = next(s["run"] for s in job["steps"] if s.get("name") == "Run Windows install + update E2E")
    assert "${{ matrix.files }}" in run
