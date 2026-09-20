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
import re
import shutil
import subprocess
import sys
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

    (tmp_path / "temp").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=_step_env(bin_dir, tmp_path),
        capture_output=True, text=True, timeout=120,
    )


# Never the real repo/PR: the script can reach the real publisher, and with a
# usable token in the environment that would attach evidence to a live PR.
_FAKE_REPO = "example-org/example-repo"
_FAKE_PR_HEAD_SHA = "deadbeef"


def _step_env(bin_dir: Path, tmp_path: Path) -> dict:
    """Environment for the step: no credentials, no real repo."""
    env = dict(os.environ)
    # Strip every credential the publisher could authenticate with.
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_SESSION_TOKEN",
                "GH_IMAGE_SESSION_TOKEN", "GITHUB_API_TOKEN"):
        env.pop(var, None)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "SOURCE_REPO": _FAKE_REPO,
        "SOURCE_RUN_ID": "123",
        "HEAD_OWNER": "example-org",
        "HEAD_BRANCH": "topic",
        "HEAD_SHA": _FAKE_PR_HEAD_SHA,
    })
    return env


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


def _gh_stub(pr_number: str = "4242",
             artifacts: str = "e2e-evidence-desktop",
             producer: str = "success") -> str:
    """A fake gh answering the three API reads the script makes."""
    return f"""
        case "$*" in
          *"/pulls"*)     echo "{pr_number}" ;;
          *"/jobs"*)      echo "{producer}" ;;
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
    result = _run_step(tmp_path, _gh_stub(artifacts="", producer="success"))
    assert result.returncode != 0, (
        "Desktop E2E succeeded but produced no evidence artifact, yet the "
        f"step reported success; stdout={result.stdout!r}"
    )
    assert "primary function" in result.stderr or "primary function" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_missing_artifact_is_a_clean_skip_only_when_the_producer_was_skipped(tmp_path):
    """`Desktop E2E` is hard-disabled in ci.yaml (`if: ${{ false && ... }}`).

    No CI run therefore produces an `e2e-evidence-*` artifact, so treating a
    missing artifact as fatal unconditionally would red the publish workflow
    on 100% of PRs. `skipped` — and ONLY `skipped` — is a clean skip.
    """
    result = _run_step(tmp_path, _gh_stub(artifacts="", producer="skipped"))
    assert result.returncode == 0, (
        "the producer was skipped, yet a missing artifact was treated as a "
        f"regression; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Desktop E2E was skipped" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("producer", ["failure", "cancelled", "timed_out"])
def test_producer_ran_and_delivered_nothing_is_fatal(tmp_path, producer):
    """A producer that RAN and delivered no evidence is a real failure.

    Treating every non-success conclusion as "never ran" would let a
    genuinely broken Desktop E2E report success while attaching nothing.
    """
    result = _run_step(tmp_path, _gh_stub(artifacts="", producer=producer))
    assert result.returncode != 0, (
        f"Desktop E2E concluded {producer!r} and produced no evidence, yet "
        f"the step reported success; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_absent_producer_job_is_fatal_not_a_skip(tmp_path):
    """An empty conclusion is not evidence of a deliberate skip.

    It also occurs when the job is renamed, removed, or no longer matches
    the wrapper's name filter — in which case reporting success while
    attaching nothing silently fails the workflow's primary function.
    """
    result = _run_step(tmp_path, _gh_stub(artifacts="", producer=""))
    assert result.returncode != 0, (
        "no Desktop E2E job matched the filter (renamed? removed?) and the "
        f"step still reported success; stdout={result.stdout!r}"
    )
    assert "renamed or removed" in result.stdout + result.stderr


def test_the_evidence_producer_is_currently_disabled_in_ci():
    """Pins the premise the skip-vs-fail rule depends on.

    If `Desktop E2E` is ever re-enabled, this test fails and forces a
    re-read of that rule rather than letting it silently go stale.
    """
    ci = (_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    e2e = ci.split("e2e-desktop:", 1)[1].split("\n  docs-site:", 1)[0]
    assert "if: ${{ false &&" in e2e, (
        "Desktop E2E is no longer hard-disabled — a missing evidence artifact "
        "may now be a real regression on every PR; revisit the skip rule in "
        "scripts/ci/publish_evidence_step.sh"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_untrusted_publisher_output_cannot_fake_a_rate_limit(tmp_path):
    """The publisher echoes filenames taken from the untrusted PR artifact.

    A manifest naming a file `RateLimitError.png` (or any string carrying a
    rate-limit signature) must not let a validation failure masquerade as a
    rate limit and exit 0. Here a stub publisher emits exactly such a line
    and then fails — the wrapper must still red the step.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"/pulls"*) echo 4242 ;;\n'
        '  *"/jobs"*) echo success ;;\n'
        '  *"/artifacts"*) echo e2e-evidence-desktop ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").chmod(0o755)

    # Stub python3 so the "publisher" emits attacker-shaped text, then fails.
    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *publish_e2e_evidence.py*)\n"
        '     echo "Evidence file is not a PNG: API rate limit exceeded.png"\n'
        "     exit 1 ;;\n"
        f"  *) exec {shutil.which('python3')} \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").chmod(0o755)

    (tmp_path / "temp").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=_step_env(bin_dir, tmp_path),
        capture_output=True, text=True, timeout=120,
    )
    assert "API rate limit exceeded" in result.stdout, \
        "the stub publisher did not emit the attacker-shaped line"
    assert result.returncode != 0, (
        "a publisher-side validation failure carrying a rate-limit signature "
        f"was swallowed as a rate limit; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_unusable_temp_root_refuses_to_run(tmp_path):
    """An empty WORK_DIR would log to /publish.log and download to /."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)

    env = _step_env(bin_dir, tmp_path)
    env["RUNNER_TEMP"] = str(tmp_path / "does-not-exist")
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, (
        "mktemp failed but the script continued with an unset working "
        f"directory; stdout={result.stdout!r}"
    )
    assert "refusing to run" in result.stderr, (
        "the script failed for an incidental reason rather than explicitly "
        f"refusing to run without a working directory; stderr={result.stderr!r}"
    )
    assert "/publish.log" not in result.stderr, \
        "the log was redirected to a root-level path"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_tests_never_target_the_real_repository(monkeypatch):
    """A test that reaches the real publisher could attach to a live PR."""
    assert _FAKE_REPO != "ANG-Ventures/hermes-agent"
    assert "/" in _FAKE_REPO

    # Simulate a runner that DOES carry credentials, so the scrub is what
    # makes this pass rather than the ambient environment happening to be
    # clean (which is what made an earlier version of this test vacuous).
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_SESSION_TOKEN"):
        monkeypatch.setenv(var, "ghp_fake_value_for_test")

    env = _step_env(Path("/bin"), Path("/tmp"))
    assert env["SOURCE_REPO"] == _FAKE_REPO, \
        "the step env targets a real repository"
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_SESSION_TOKEN"):
        assert var not in env, (
            f"{var} was present in the ambient environment and reached the "
            "publisher under test — it could authenticate against a live PR"
        )


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

    (tmp_path / "temp").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=_step_env(bin_dir, tmp_path),
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_publisher_rate_limit_exit_code_is_tolerated(tmp_path):
    """The publisher makes its OWN API calls; the budget can die there.

    It signals that class with a dedicated exit code rather than log text,
    because its stdout echoes untrusted artifact filenames.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"/pulls"*) echo 4242 ;;\n'
        '  *"/jobs"*) echo success ;;\n'
        '  *"/artifacts"*) echo e2e-evidence-desktop ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *publish_e2e_evidence.py*) exit 75 ;;\n"
        f"  *) exec {shutil.which('python3')} \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").chmod(0o755)

    (tmp_path / "temp").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=_step_env(bin_dir, tmp_path),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        "a publisher-side rate limit still reddened the step; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::warning::" in result.stdout


def test_publisher_rate_limit_exit_code_matches_the_wrapper():
    """A drifting code silently disables the tolerance."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pub_e2e", _ROOT / "scripts" / "ci" / "publish_e2e_evidence.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pub_e2e"] = mod
    spec.loader.exec_module(mod)

    wrapper = _SCRIPT.read_text(encoding="utf-8")
    declared = int(re.search(r"PUBLISHER_RATE_LIMITED_RC=(\d+)", wrapper).group(1))
    assert declared == mod.RATE_LIMITED_EXIT_CODE, (
        f"the wrapper tolerates exit {declared} but the publisher signals "
        f"{mod.RATE_LIMITED_EXIT_CODE}"
    )


def test_extension_install_is_inside_the_classifier():
    """`gh extension install` spends the same budget it must be tolerant of.

    Run as a separate workflow step it sat OUTSIDE the classifier, so a
    budget exhaustion there reddened the PR before tolerance applied.
    """
    spec = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["publish"]["steps"]
    install_steps = [s for s in steps if "extension install" in str(s.get("run", ""))]
    assert not install_steps, (
        "gh extension install still runs as its own workflow step, outside "
        "the rate-limit classifier"
    )
    assert "gh extension install" in _SCRIPT.read_text(encoding="utf-8")
