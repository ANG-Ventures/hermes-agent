"""Every ``uv python install`` in CI goes through the retry action with a window
that outlasts a python-build-standalone release-asset outage.

The interpreter download is a GitHub release asset. On 2026-09-28 it returned
HTTP 500 for longer than the retry action's default 3 x 10 s window: main push
run 36457723085 went red on a 30 s window, and merge-group run 36463111043 was
ejected from the queue by a bare ``run: uv python install`` in the e2e job that
had no retry at all (t_9081913e).
"""

from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_RETRY = "./.github/actions/retry"
_MIN_ATTEMPTS = 5
_MIN_WINDOW_S = 120


def _steps_running(needle: str, root: Path = _WORKFLOWS):
    for wf in sorted(root.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                text = str(step.get("run", "")) + str((step.get("with") or {}).get("command", ""))
                if needle in text:
                    yield wf.name, job_name, step


def _violations(root: Path = _WORKFLOWS) -> list[str]:
    bad = []
    for wf, job, step in _steps_running("uv python install", root):
        where = f"{wf}:{job}:{step.get('name', '?')}"
        if step.get("uses") != _RETRY:
            bad.append(f"{where}: not wrapped in {_RETRY}")
            continue
        w = step.get("with") or {}
        attempts = int(w.get("attempts", 3))
        delay = int(w.get("delay", 10))
        if attempts < _MIN_ATTEMPTS or (attempts - 1) * delay < _MIN_WINDOW_S:
            bad.append(f"{where}: attempts={attempts} delay={delay}s is under the outage window")
    return bad


def test_every_uv_python_install_is_retried_over_an_outage_window():
    found = list(_steps_running("uv python install"))
    # Census floor: the unit/lint/docker/desktop lanes now take their
    # interpreter from ./.github/actions/setup-pm (PM-pinned, no `uv python
    # install` step); the e2e-upgrade job and live-providers still install
    # one directly, and those are the steps this guard must keep wrapped.
    assert len(found) >= 2, f"census: only {len(found)} uv python install steps found"
    assert _violations() == []


def test_detector_flags_bare_and_short_window_steps(tmp_path):
    (tmp_path / "x.yml").write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    "a": {"steps": [{"name": "bare", "run": "uv python install 3.11"}]},
                    "b": {
                        "steps": [
                            {"name": "default", "uses": _RETRY, "with": {"command": "uv python install 3.11"}}
                        ]
                    },
                    "c": {
                        "steps": [
                            {
                                "name": "ok",
                                "uses": _RETRY,
                                "with": {"command": "uv python install 3.11", "attempts": "5", "delay": "30"},
                            }
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    bad = _violations(tmp_path)
    assert len(bad) == 2
    assert any("bare" in b and "not wrapped" in b for b in bad)
    assert any("default" in b and "attempts=3" in b for b in bad)
