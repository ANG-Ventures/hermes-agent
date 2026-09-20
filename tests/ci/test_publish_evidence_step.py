"""The E2E-evidence publish step must tolerate ONLY the rate-limit failure.

FleetReview P1 (publish-e2e-evidence.yml:41): a blanket
``continue-on-error: true`` on the whole step also suppresses missing
artifacts, invalid authentication and a broken gh extension. Publishing
evidence IS this workflow's function, so it could report success while
attaching nothing.

These tests run the real ``scripts/ci/publish_evidence_step.sh`` against a
stubbed ``gh`` on ``PATH`` and assert the exit code per failure class.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "ci" / "publish_evidence_step.sh"
_WORKFLOW = _ROOT / ".github" / "workflows" / "publish-e2e-evidence.yml"


def _run_step(tmp_path: Path, gh_body: str) -> subprocess.CompletedProcess:
    """Run the step script with a fake ``gh`` that behaves like ``gh_body``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(gh_body), encoding="utf-8")
    gh.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "SOURCE_REPO": "ANG-Ventures/hermes-agent",
        "SOURCE_RUN_ID": "123",
        "HEAD_OWNER": "ANG-Ventures",
        "HEAD_BRANCH": "topic",
        "HEAD_SHA": "deadbeef",
    })
    (tmp_path / "temp").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_rate_limit_failure_does_not_red_the_step(tmp_path):
    """The failure this workflow is allowed to swallow."""
    result = _run_step(tmp_path, """
        echo "gh: API rate limit exceeded for installation" >&2
        exit 1
    """)
    assert result.returncode == 0, (
        "a rate-limit exhaustion must not paint a red X on an innocent PR; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::warning::" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("label,gh_body", [
    ("bad credentials", """
        echo "gh: Bad credentials (HTTP 401)" >&2
        exit 1
    """),
    ("missing scope", """
        echo "gh: Resource not accessible by integration (HTTP 403)" >&2
        exit 1
    """),
    ("gh not functional", """
        echo "gh: unknown command \\"api\\"" >&2
        exit 2
    """),
])
def test_non_rate_limit_failures_still_red_the_step(tmp_path, label, gh_body):
    """Everything else is a real defect and must keep failing loudly.

    Under the blanket ``continue-on-error: true`` these all reported
    success while attaching nothing.
    """
    result = _run_step(tmp_path, gh_body)
    assert result.returncode != 0, (
        f"{label} was silently swallowed — the workflow would report success "
        f"having attached nothing; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_no_open_pr_is_a_clean_skip(tmp_path):
    """A superseded run has no PR to attach to; that is not a failure."""
    result = _run_step(tmp_path, """
        # Empty PR list for every call.
        exit 0
    """)
    assert result.returncode == 0
    assert "No open pull request" in result.stdout


def _gh_stub(pr_number: str = "736", artifacts: str = "e2e-evidence-desktop") -> str:
    """A fake gh that answers the two API reads the script makes."""
    return f"""
        case "$*" in
          *"/pulls"*)     echo "{pr_number}" ;;
          *"/artifacts"*) echo "{artifacts}" ;;
          *"run download"*) exit 0 ;;
          *) exit 0 ;;
        esac
    """


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_missing_evidence_artifact_is_fatal(tmp_path):
    """An artifact-generation regression must not report success.

    Publishing evidence IS this workflow's function. Exiting 0 with nothing
    attached is the precise silent failure the wrapper exists to prevent.
    """
    result = _run_step(tmp_path, _gh_stub(artifacts=""))
    assert result.returncode != 0, (
        "no evidence artifact was found, yet the step reported success — "
        f"the workflow passes while attaching nothing; stdout={result.stdout!r}"
    )
    assert "primary function" in result.stderr or "primary function" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_artifact_lookup_paginates(tmp_path):
    """The evidence artifact can sit past the default 30-result first page.

    This CI run uploads 16 test-slice artifacts alone, plus every reusable
    job's review-status artifact.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"/pulls"*) echo 736 ;;\n'
        '  *"/artifacts"*)\n'
        '     if [[ "$*" != *"--paginate"* ]]; then exit 0; fi\n'
        '     echo e2e-evidence-desktop ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    env = dict(os.environ)
    (tmp_path / "temp").mkdir(exist_ok=True)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "SOURCE_REPO": "ANG-Ventures/hermes-agent",
        "SOURCE_RUN_ID": "123",
        "HEAD_OWNER": "ANG-Ventures",
        "HEAD_BRANCH": "topic",
        "HEAD_SHA": "deadbeef",
    })
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    # The script proceeds past the artifact lookup to the real publisher,
    # which then fails on the absent GITHUB_TOKEN. That is fine — the point
    # is that it got there. Without --paginate the listing reads empty and
    # the script stops at the "no evidence artifact" failure instead.
    assert "No E2E evidence artifact" not in result.stdout + result.stderr, (
        "the artifact listing was not paginated, so a present evidence "
        f"artifact read as absent; stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_temp_paths_are_not_predictable(tmp_path):
    """A predictable log path under a shared /tmp is a symlink-attack seam."""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "mktemp -d" in text, "the working directory is not created privately"
    assert 'LOG_FILE="$WORK_DIR' in text, "the log still lives at a shared path"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_failed_pr_lookup_is_not_read_as_no_open_pr(tmp_path):
    """Bad credentials must not be laundered into the clean-skip branch."""
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo "gh: Bad credentials (HTTP 401)" >&2; exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode != 0, (
        "a 401 on the PR lookup produced an empty PR number and was reported "
        f"as a clean skip; stdout={result.stdout!r}"
    )


def test_workflow_does_not_blanket_suppress_the_publish_step():
    """The step must not carry ``continue-on-error``; the script decides."""
    spec = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["publish"]["steps"]
    publish_steps = [s for s in steps if "publish_evidence_step.sh" in str(s.get("run", ""))]
    assert publish_steps, "the workflow no longer calls the publish step script"
    for step in publish_steps:
        assert not step.get("continue-on-error"), (
            "continue-on-error on the publish step masks every failure class, "
            "not just the rate-limit one it was added for"
        )
