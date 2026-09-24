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

import hashlib
import os
import re
import shlex
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


# The gh-image binary the stubs "download". Tests pin the step's expected
# digest to this content via PUBLISH_EVIDENCE_GH_IMAGE_SHA256, so the real
# verification path runs against a known-good pair on any architecture.
_FAKE_GH_IMAGE_ASSET = "linux-amd64"
_FAKE_GH_IMAGE_BYTES = "#!/bin/sh\necho stub-gh-image\n"
_FAKE_GH_IMAGE_SHA256 = hashlib.sha256(
    _FAKE_GH_IMAGE_BYTES.encode("utf-8")
).hexdigest()

# Prepended to every stub built by ``_run_step``: report the extension as
# already installed so ``ensure_extension`` short-circuits. Tests about the
# install/verify path itself build their own ``gh`` and do not get this.
_EXTENSION_PRESENT_PREAMBLE = """
        case "$*" in
          *"extension list"*) echo "gh image"; exit 0 ;;
        esac
"""


def _run_step(tmp_path: Path, gh_body: str) -> subprocess.CompletedProcess:
    """Run the step script with a fake ``gh`` that behaves like ``gh_body``."""
    return _run_step_raw(
        tmp_path, textwrap.dedent(_EXTENSION_PRESENT_PREAMBLE) + gh_body)


def _run_step_raw(tmp_path: Path, gh_body: str, *,
                  extra_env: dict | None = None,
                  uname_machine: str | None = None,
                  break_hashers: bool = False,
                  ) -> subprocess.CompletedProcess:
    """As ``_run_step`` but with NO extension-present preamble.

    Used by the tests that exercise the install/verify path itself, which
    must see ``ensure_extension`` actually run.

    ``extra_env`` overrides the step environment; a value of ``None``
    REMOVES the variable. Removal is the only way to un-pin: the step reads
    its overrides with ``${VAR:-default}``, so an EMPTY value falls back to
    the shipped architecture pin rather than clearing it.

    ``uname_machine`` puts a fake ``uname`` on PATH reporting that machine,
    so the architecture dispatch can be driven to its unsupported branch on
    any host.

    ``break_hashers`` shadows ``sha256sum``/``shasum`` with stubs that fail,
    which is how the "could not compute a digest" refusal is reached without
    dismantling the PATH the rest of the script needs.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(gh_body), encoding="utf-8")
    gh.chmod(0o755)

    if uname_machine is not None:
        uname = bin_dir / "uname"
        uname.write_text(
            "#!/usr/bin/env bash\n"
            f'if [ "${{1:-}}" = "-m" ]; then echo {shlex.quote(uname_machine)}; '
            "else /usr/bin/uname \"$@\"; fi\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)

    if break_hashers:
        for tool in ("sha256sum", "shasum"):
            stub = bin_dir / tool
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "{tool}: simulated failure" >&2\nexit 1\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)

    env = _step_env(bin_dir, tmp_path)
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    (tmp_path / "temp").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
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
    # And the ambient job summary. Inherited on GitHub Actions, the
    # persistent-transient tests would hand the script the LIVE summary
    # file of the test job and it would dutifully append "evidence not
    # published" to it — false publication warnings in the user-visible
    # summary of an unrelated job. The two tests that assert on summary
    # content opt back in with a temporary file of their own.
    env.pop("GITHUB_STEP_SUMMARY", None)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "SOURCE_REPO": _FAKE_REPO,
        "SOURCE_RUN_ID": "123",
        "HEAD_OWNER": "example-org",
        "HEAD_BRANCH": "topic",
        "HEAD_SHA": _FAKE_PR_HEAD_SHA,
        # Keep the retry path fast. The shipped default is pinned separately
        # by test_the_shipped_retry_backoff_is_not_the_test_value.
        "PUBLISH_EVIDENCE_RETRY_BACKOFF": "0 0",
        # Pin the gh-image asset + digest the stubs serve, so the
        # verification path is exercised regardless of the host
        # architecture the suite runs on. The shipped defaults are pinned
        # separately by test_the_shipped_gh_image_digests_are_not_the_test_value.
        "PUBLISH_EVIDENCE_GH_IMAGE_ASSET": _FAKE_GH_IMAGE_ASSET,
        "PUBLISH_EVIDENCE_GH_IMAGE_SHA256": _FAKE_GH_IMAGE_SHA256,
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
    assert "were skipped" in result.stdout


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
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
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
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
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
def test_publisher_transient_exit_code_is_tolerated(tmp_path):
    """The publisher makes its OWN API calls; the budget can die there.

    It signals that class with a dedicated exit code rather than log text,
    because its stdout echoes untrusted artifact filenames.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
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


def test_publisher_transient_exit_code_matches_the_wrapper():
    """A drifting code silently disables the tolerance."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pub_e2e", _ROOT / "scripts" / "ci" / "publish_e2e_evidence.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pub_e2e"] = mod
    spec.loader.exec_module(mod)

    wrapper = _SCRIPT.read_text(encoding="utf-8")
    declared = int(re.search(r"PUBLISHER_TRANSIENT_RC=(\d+)", wrapper).group(1))
    assert declared == mod.TRANSIENT_EXIT_CODE, (
        f"the wrapper tolerates exit {declared} but the publisher signals "
        f"{mod.TRANSIENT_EXIT_CODE}"
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_all_matching_producers_are_considered_not_just_the_first(tmp_path):
    """A matrix or renamed sibling can yield several matching jobs.

    If the FIRST is `skipped` while another actually ran and delivered
    nothing, skipping on the first publishes silence.
    """
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  printf 'skipped\\nfailure\\n' ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode != 0, (
        "the first producer was skipped but another RAN and delivered "
        f"nothing, yet the step reported success; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_every_producer_skipped_is_still_a_clean_skip(tmp_path):
    """Several skipped jobs must not be mistaken for one that ran."""
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  printf 'skipped\\nskipped\\n' ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode == 0, (
        f"all producers were skipped yet the step failed; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_untrusted_download_output_cannot_fake_a_rate_limit(tmp_path):
    """`gh run download` echoes PR-controlled artifact names and paths.

    A crafted path carrying a rate-limit signature must not classify a real
    download failure as a tolerated rate limit. The control API call is the
    trusted signal instead.
    """
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  echo skipped ;;
          *"/artifacts"*) echo e2e-evidence-desktop ;;
          *"run download"*)
             echo "failed to extract 'API rate limit exceeded/evil.png'" >&2
             exit 1 ;;
          *) exit 0 ;;   # the control API call succeeds => NOT rate limited
        esac
    """)
    assert result.returncode != 0, (
        "a crafted artifact path spoofed the rate-limit tolerance and the "
        f"step reported success with nothing attached; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_revoked_credential_is_not_rewritten_into_a_rate_limit(tmp_path):
    """FleetReview F2 (P1): the probe's FAILURE is not the transient signal.

    Every probe failure used to be rewritten into a hard-coded "API rate
    limit exceeded" line, which ``is_transient`` then matched. A revoked or
    expired credential fails the download AND the probe, so it was retried
    and then exited 0 having published nothing — the masked failure this
    wrapper exists to prevent, reintroduced one layer down.
    """
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  echo success ;;
          *"/artifacts"*) echo e2e-evidence-desktop ;;
          *"run download"*) echo "gh: HTTP 401: Bad credentials" >&2; exit 1 ;;
          *"repos/"*)
             # The control probe fails too, with a CREDENTIAL error and no
             # transient signature anywhere in its output.
             echo "gh: HTTP 401: Bad credentials" >&2; exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode != 0, (
        "a revoked credential was tolerated as a rate limit and the step "
        f"exited 0 with nothing published; stdout={result.stdout!r}"
    )
    assert "::warning::" not in result.stdout, (
        "a credential failure was reported as a tolerated transient"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_genuinely_rate_limited_probe_is_still_tolerated(tmp_path):
    """The positive control for F2's fix.

    Classifying the probe's output must not become "never transient" — a
    download that fails while the API really is rate limited still has to
    reach the tolerated NEUTRAL, or the fix trades a masked failure for an
    innocent PR reddened by a rate limit.
    """
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  echo success ;;
          *"/artifacts"*) echo e2e-evidence-desktop ;;
          *"run download"*) echo "gh: could not download" >&2; exit 1 ;;
          *"repos/"*)
             echo "gh: API rate limit exceeded for installation" >&2; exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode == 0, (
        "a real rate limit during download reddened an innocent PR; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::warning::" in result.stdout, "the tolerated transient was invisible"


@pytest.mark.parametrize("code,transient", [
    ("500", True),
    ("501", False),   # Not Implemented: the request is wrong, retry cannot fix it.
    ("502", True),
    ("503", True),
    ("504", True),
    ("505", True),    # FleetReview F1/F3: `50[0-4]` matched 501 and missed these.
    ("507", True),
    ("511", True),
    ("400", False),
    ("404", False),
])
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_the_shell_server_error_class_is_5xx_except_501(tmp_path, code, transient):
    """FleetReview F1+F3: one 5xx class, and 501 excluded from it.

    The old `50[0-4]` pattern was wrong in BOTH directions: it matched 501
    (so a malformed request was retried and then tolerated as a transient,
    publishing nothing) and missed 505/507/etc (so a genuine server-side
    fault reddened an innocent PR).
    """
    result = _run_step(tmp_path, f"""
        case "$*" in
          *"/pulls"*) echo "gh: HTTP {code}: server said no" >&2; exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    if transient:
        assert result.returncode == 0, (
            f"HTTP {code} is a server-side fault but reddened the PR; "
            f"stdout={result.stdout!r}"
        )
        assert "::warning::" in result.stdout
    else:
        assert result.returncode != 0, (
            f"HTTP {code} was tolerated as transient; it must stay red. "
            f"stdout={result.stdout!r}"
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_longer_status_code_cannot_prefix_match_the_5xx_class(tmp_path):
    """Boundary anchoring: `HTTP 500` must not match `HTTP 5001`."""
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo "gh: HTTP 5001: not a real status" >&2; exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    assert result.returncode != 0, (
        "a non-5xx status prefix-matched the server-error class and was "
        f"tolerated; stdout={result.stdout!r}"
    )


def test_the_two_transient_5xx_enumerations_agree():
    """The shell and Python classifiers must not drift apart.

    Each maintains its own copy of "which 5xx codes are transient". A
    second hand-maintained copy of a vocabulary silently drifts, so pin
    them against each other across the whole range rather than trusting a
    comment.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pub_e2e", _ROOT / "scripts" / "ci" / "publish_e2e_evidence.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pub_e2e"] = mod
    spec.loader.exec_module(mod)

    wrapper = _SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^SERVER_ERROR_PATTERN='([^']+)'", wrapper, re.M)
    assert match is not None, "the shell 5xx pattern could not be located"
    pattern = match.group(1)

    for code in range(500, 600):
        shell_says = bool(re.search(pattern, f"gh: HTTP {code}: x"))
        python_says = code in mod.TRANSIENT_SERVER_ERROR_CODES
        assert shell_says == python_says, (
            f"HTTP {code}: the shell classifier says transient={shell_says} "
            f"but the Python publisher says transient={python_says}"
        )
    assert 501 not in mod.TRANSIENT_SERVER_ERROR_CODES, (
        "501 Not Implemented must stay red: the request is wrong, and "
        "retrying cannot fix it"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_every_evidence_artifact_is_published_not_just_the_first(tmp_path):
    """FleetReview F5 (P1): a partial publish must not report success.

    ``head -n1`` selected one artifact and published it. With a matrix
    producer (or a renamed sibling) the rest are silently dropped and the
    step is green — reviewers never see the evidence they were promised.
    """
    downloads = tmp_path / "downloads.log"
    q_downloads = shlex.quote(str(downloads))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
        '  *"/pulls"*) echo 4242 ;;\n'
        "  *\"/jobs\"*)  printf 'success\\nsuccess\\n' ;;\n"
        "  *\"/artifacts\"*) printf 'e2e-evidence-a\\ne2e-evidence-b\\n' ;;\n"
        '  *"run download"*)\n'
        '     for a in "$@"; do\n'
        f'       case "$a" in e2e-evidence-*) echo "$a" >> {q_downloads} ;; esac\n'
        "     done\n"
        "     exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    # The publisher itself needs credentials we deliberately do not have;
    # stub it as succeeding so the assertion is about WHICH artifacts the
    # loop reached, not about publishing.
    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *publish_e2e_evidence.py*) exit 0 ;;\n"
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
    assert downloads.exists(), (
        f"no artifact was downloaded at all; stdout={result.stdout!r}"
    )
    names = downloads.read_text().split()
    assert names == ["e2e-evidence-a", "e2e-evidence-b"], (
        "not every evidence artifact was published — a partial result "
        f"would have been reported as success; downloaded {names!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_fewer_artifacts_than_producers_that_ran_is_fatal(tmp_path):
    """One producer ran and delivered nothing; the other did.

    Publishing the one that exists and exiting 0 hides the missing half.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
        '  *"/pulls"*) echo 4242 ;;\n'
        "  *\"/jobs\"*)  printf 'success\\nsuccess\\n' ;;\n"
        '  *"/artifacts"*) echo e2e-evidence-a ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    # The publisher must SUCCEED here. Otherwise the step exits non-zero
    # because publishing failed for want of credentials, and the test would
    # pass even with the cardinality guard removed — proving nothing.
    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *publish_e2e_evidence.py*) exit 0 ;;\n"
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
    assert result.returncode != 0, (
        "two producers ran but only one artifact existed, and the step "
        f"reported success anyway; stdout={result.stdout!r}"
    )
    assert "would hide the missing evidence" in (result.stdout + result.stderr), (
        "the step failed for an incidental reason rather than because the "
        f"artifact count did not cover the producers; stdout={result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_untrusted_output_cannot_forge_a_workflow_command(tmp_path):
    """FleetReview F6 (P2): artifact filenames reach the Actions parser.

    A crafted filename carrying a newline plus ``::error::`` or
    ``::add-mask::`` would otherwise forge annotations or mask later
    output. ``stop-commands`` with an unguessable token disables parsing
    for the span the untrusted content occupies.
    """
    result = _run_step(tmp_path, """
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*)  echo success ;;
          *"/artifacts"*) echo e2e-evidence-desktop ;;
          *"run download"*)
             printf 'failed on file\\n::error::forged annotation\\n' >&2
             exit 1 ;;
          *) exit 0 ;;
        esac
    """)
    out = result.stdout
    start = re.search(r"::stop-commands::([0-9a-f]{16,})", out)
    assert start, (
        f"untrusted output was printed with command parsing live; stdout={out!r}"
    )
    token = start.group(1)
    assert f"::{token}::" in out, "command parsing was never resumed"
    forged = out.index("::error::forged annotation")
    assert out.index(f"::stop-commands::{token}") < forged < out.index(f"::{token}::"), (
        "the forged workflow command fell outside the stop-commands span"
    )


def test_the_stop_commands_token_is_unguessable():
    """A fixed token lets the untrusted content resume parsing itself.

    If the resume token were a constant, a crafted filename could simply
    emit it and re-enable command processing mid-span.
    """
    wrapper = _SCRIPT.read_text(encoding="utf-8")
    body_match = re.search(r"^print_untrusted\(\) \{.*?^\}", wrapper, re.M | re.S)
    assert body_match is not None, "print_untrusted could not be located"
    body = body_match.group(0)
    assert "/dev/urandom" in body, (
        "the stop-commands token is not drawn from an unguessable source, "
        "so untrusted output can resume workflow-command parsing itself"
    )




def test_the_step_env_does_not_inherit_the_ambient_job_summary(tmp_path):
    """FleetReview F7 (P1): tests must not write to the LIVE job summary.

    ``GITHUB_STEP_SUMMARY`` is set on every GitHub Actions job. Inherited
    into the step's environment, the persistent-transient tests hand the
    script the summary file of the TEST job, and it appends "evidence not
    published" to it — false publication warnings in the user-visible
    summary of an unrelated job. The two tests that assert on summary
    content set their own temporary file instead.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.environ["GITHUB_STEP_SUMMARY"] = str(tmp_path / "ambient-summary.md")
    try:
        env = _step_env(bin_dir, tmp_path)
    finally:
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
    assert "GITHUB_STEP_SUMMARY" not in env, (
        "the step environment inherits the ambient job summary, so a test run on "
        "GitHub Actions would append to the live summary of the test job"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_tolerated_transient_writes_no_summary_when_none_is_configured(tmp_path):
    """The end-to-end half of F7: nothing ambient gets written.

    Even reaching the NEUTRAL path — the one that writes a summary line —
    the step must not touch a summary file the test did not give it.
    """
    ambient = tmp_path / "ambient-summary.md"
    ambient.write_text("", encoding="utf-8")
    os.environ["GITHUB_STEP_SUMMARY"] = str(ambient)
    try:
        result = _run_step(tmp_path, """
            case "$*" in
              *"/pulls"*) echo "gh: API rate limit exceeded" >&2; exit 1 ;;
              *) exit 0 ;;
            esac
        """)
    finally:
        os.environ.pop("GITHUB_STEP_SUMMARY", None)

    assert "::warning::" in result.stdout, "the NEUTRAL path was never reached"
    assert ambient.read_text() == "", (
        "the step appended to the ambient job summary it inherited from the "
        f"test process; contents={ambient.read_text()!r}"
    )



def _counting_gh(counter: Path, fail_times: int, failure: str) -> str:
    """A gh stub that fails the first ``fail_times`` runs, then succeeds.

    Counts its own invocations of the PR lookup so a test can assert the
    wrapper actually RETRIED rather than merely tolerating.
    """
    q_counter = shlex.quote(str(counter))
    return f"""
        case "$*" in
          *"/pulls"*)
             n=$(cat {q_counter} 2>/dev/null || echo 0)
             n=$((n + 1)); echo "$n" > {q_counter}
             if [ "$n" -le {fail_times} ]; then
               echo "{failure}" >&2
               exit 1
             fi
             echo 4242 ;;
          *"/jobs"*) echo skipped ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_transient_rate_limit_is_retried_and_then_succeeds(tmp_path):
    """The common case: a secondary rate limit clears within seconds.

    Tolerating it without retrying would give up on a publish that would
    have worked, so the retry has to actually happen.
    """
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _counting_gh(counter, fail_times=1,
                     failure="gh: You have exceeded a secondary rate limit"),
    )
    assert result.returncode == 0, (
        f"a transient that cleared on retry still failed; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert int(counter.read_text()) >= 2, (
        "the step tolerated the rate limit without ever retrying, so a "
        "publish that would have succeeded was abandoned"
    )
    assert "::warning::" not in result.stdout, (
        "the retry SUCCEEDED, so this is an ordinary green — emitting the "
        "neutral warning here would cry wolf on every transient blip"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_5xx_is_treated_as_transient_and_retried(tmp_path):
    """A server-side 5xx is GitHub's fault, not this repository's."""
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _counting_gh(counter, fail_times=1,
                     failure="gh: Service Unavailable (HTTP 503)"),
    )
    assert result.returncode == 0, (
        f"a 503 that cleared on retry reddened an innocent PR; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert int(counter.read_text()) >= 2, "the 503 was not retried"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_retrying_is_bounded_not_infinite(tmp_path):
    """A persistent transient must terminate, not spin until SIGKILL."""
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _counting_gh(counter, fail_times=99,
                     failure="gh: You have exceeded a secondary rate limit"),
    )
    assert result.returncode == 0, "a persistent transient must not red the PR"
    attempts = int(counter.read_text())
    assert attempts == 3, (
        f"the retry loop ran {attempts} attempts; it must be bounded at "
        "MAX_ATTEMPTS=3 so a persistent outage cannot burn the job timeout"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_tolerated_transient_is_never_a_silent_green(tmp_path):
    """The whole point of this card: exit 0, but VISIBLY non-green.

    A blanket ``continue-on-error`` produced a bare green tick. A tolerated
    transient must instead leave a neutral check run, a warning annotation
    and a job-summary line.
    """
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    calls = tmp_path / "gh-calls.log"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {shlex.quote(str(calls))}\n'
        'case "$*" in\n'
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
        '  *"/pulls"*) echo "gh: API rate limit exceeded" >&2; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    (tmp_path / "temp").mkdir(exist_ok=True)
    env = _step_env(bin_dir, tmp_path)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, "an innocent PR was reddened by a rate limit"
    logged = calls.read_text()
    assert "check-runs" in logged, (
        "no neutral check run was created, so the tolerated failure is "
        "invisible in the PR's check list — a silent green"
    )
    assert "conclusion=neutral" in logged, (
        f"the check run was not neutral; gh calls were:\n{logged}"
    )
    assert "::warning::" in result.stdout, "no warning annotation was emitted"
    assert "not published" in summary.read_text(), (
        "nothing was written to the job summary"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_the_neutral_status_still_warns_when_the_check_run_cannot_be_created(tmp_path):
    """The check run is itself an API call, and the API is by hypothesis ill.

    If it cannot be created the condition must STILL be visible locally,
    otherwise the tolerance degrades into exactly the silent green this
    change exists to prevent.
    """
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"extension list"*) echo "gh image"; exit 0 ;;\n'
        '  *"check-runs"*) echo "gh: API rate limit exceeded" >&2; exit 1 ;;\n'
        '  *"/pulls"*) echo "gh: API rate limit exceeded" >&2; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    (tmp_path / "temp").mkdir(exist_ok=True)
    env = _step_env(bin_dir, tmp_path)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    result = subprocess.run(
        ["bash", str(_SCRIPT)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0
    assert "::warning::" in result.stdout, (
        "the check run failed AND no warning was emitted — the tolerated "
        "failure left no trace at all"
    )
    assert "not published" in summary.read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_real_defect_is_never_retried_and_stays_red(tmp_path):
    """Retrying a real defect wastes the budget and still has to red.

    Bad credentials are deterministic: attempt two returns the same 401.
    """
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _counting_gh(counter, fail_times=99,
                     failure="gh: Bad credentials (HTTP 401)"),
    )
    assert result.returncode != 0, "a 401 was swallowed as a transient"
    assert int(counter.read_text()) == 1, (
        "a deterministic authentication failure was retried; that spends "
        "quota and delays the red for no possible benefit"
    )
    assert "::warning::" not in result.stdout, (
        "a real defect emitted the transient warning, which would read as "
        "a tolerated condition rather than a failure"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_404_is_not_transient(tmp_path):
    """4xx other than 429 is a real defect; only 5xx and rate limits retry.

    Guards the loose-matching failure mode where a pattern like ``HTTP 4``
    or a bare ``50`` would sweep real errors into the tolerated class.
    """
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _counting_gh(counter, fail_times=99,
                     failure="gh: Not Found (HTTP 404)"),
    )
    assert result.returncode != 0, "a 404 was classified as transient"
    assert int(counter.read_text()) == 1


def test_the_shipped_retry_backoff_is_not_the_test_value():
    """The tests run with a zero backoff; production must not.

    Without this pin, shrinking the backoff for test speed would silently
    become the real behaviour — a hot retry loop against a rate-limited API.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    shipped = re.search(r"^RETRY_BACKOFF_SECONDS=\(([^)]*)\)", text, re.M)
    assert shipped is not None, "the shipped backoff default is gone"
    values = [int(v) for v in shipped.group(1).split()]
    assert values and all(v > 0 for v in values), (
        f"the shipped backoff is {values}; a zero/absent backoff retries "
        "immediately against an API that just refused us"
    )
    assert re.search(r"^MAX_ATTEMPTS=[1-9]", text, re.M), (
        "MAX_ATTEMPTS is missing or zero, so the retry loop is unbounded "
        "or never runs"
    )


def test_the_workflow_grants_the_permission_the_neutral_status_needs():
    """``checks: write`` is what makes the neutral check run possible.

    Without it the API call 403s and the tolerated failure degrades to a
    warning annotation only — much easier to miss in the PR's check list.
    """
    spec = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert spec["permissions"].get("checks") == "write", (
        "the publish workflow cannot create the neutral check run that "
        "makes a tolerated transient visible"
    )


# ─────────────────────────────────────────────────────────────────────────
# The hopeless-wait short-circuit (``transient_wait_is_hopeless``).
#
# Every stub above answers the ``rate_limit`` probe from its catch-all
# ``*) exit 0``, i.e. with EMPTY output — so the function's non-numeric
# guard short-circuits and the three predicates behind it are never
# reached. Measured: inverting the probe's fail-open, the ``remaining``
# guard or the 60s reset window each left the suite 35/35 green.
#
# The predicates decide whether a retry that WOULD have succeeded is
# abandoned, so each one gets a case that distinguishes it. The stubs
# below are the only ones in this file that serve a well-formed probe.
# ─────────────────────────────────────────────────────────────────────────


_HOPELESS_MESSAGE = "not retrying"


def _probing_gh(counter: Path, reset: str, remaining: str) -> str:
    """A gh stub with a persistent rate limit AND a readable budget probe.

    ``reset``/``remaining`` are shell expressions evaluated per call, so a
    test can place the reset window relative to *now*. Either may be
    ``FAIL`` to make that half of the probe unreadable.
    """
    q_counter = shlex.quote(str(counter))
    def _arm(expr: str) -> str:
        return "exit 1" if expr == "FAIL" else f'echo "{expr}"'

    return f"""
        case "$*" in
          *"core.reset"*)     {_arm(reset)} ;;
          *"core.remaining"*) {_arm(remaining)} ;;
          *"/pulls"*)
             n=$(cat {q_counter} 2>/dev/null || echo 0)
             n=$((n + 1)); echo "$n" > {q_counter}
             echo "gh: API rate limit exceeded for installation" >&2
             exit 1 ;;
          *"/jobs"*) echo skipped ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("label,reset,remaining", [
    # The probe is itself an API call and the API is by hypothesis unwell.
    # Failing it CLOSED would let a broken probe abandon a viable retry.
    ("reset unreadable", "FAIL", "0"),
    ("remaining unreadable", "$(( $(date +%s) + 3600 ))", "FAIL"),
    # Budget is NOT exhausted, so this is a secondary limit that clears in
    # seconds — the reset of the primary budget is irrelevant to it.
    ("budget still has room", "$(( $(date +%s) + 3600 ))", "500"),
    # Exhausted, but the reset lands inside the window the retry budget
    # can actually cover, so waiting is viable.
    ("reset inside the retry window", "$(( $(date +%s) + 30 ))", "0"),
])
def test_a_viable_retry_is_never_skipped_by_the_hopeless_check(
    tmp_path, label, reset, remaining,
):
    """The short-circuit must fire ONLY when the wait is truly hopeless.

    Each case is a condition under which the retry could still succeed.
    Short-circuiting any of them converts the bounded-retry tolerance into
    zero retries — a publish that would have worked is abandoned, and the
    only visible difference is a neutral status arriving sooner.
    """
    counter = tmp_path / "calls"
    result = _run_step(tmp_path, _probing_gh(counter, reset, remaining))

    assert int(counter.read_text()) == 3, (
        f"{label}: the retry loop ran {counter.read_text().strip()} attempt(s) "
        "instead of the full 3 — the hopeless-wait check abandoned a retry "
        f"that could still have succeeded; stdout={result.stdout!r}"
    )
    assert _HOPELESS_MESSAGE not in result.stdout, (
        f"{label}: the step declared the wait hopeless when it was not"
    )
    # Still a tolerated transient, and still visibly non-green.
    assert result.returncode == 0
    assert "::warning::" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_genuinely_hopeless_wait_short_circuits_to_neutral(tmp_path):
    """The positive control: exhausted budget, reset far beyond the window.

    Retrying then spends more of the exhausted quota and more of the job
    timeout to reach the same neutral outcome. Without this case the
    window could be widened to infinity — the check never firing at all —
    and every other test here would still pass.
    """
    counter = tmp_path / "calls"
    result = _run_step(
        tmp_path,
        _probing_gh(counter,
                    reset="$(( $(date +%s) + 3600 ))", remaining="0"),
    )

    assert int(counter.read_text()) == 1, (
        "the primary budget is exhausted for an hour, yet the step kept "
        f"retrying; stdout={result.stdout!r}"
    )
    assert _HOPELESS_MESSAGE in result.stdout, (
        "the short-circuit fired without saying why, leaving the single "
        "attempt looking like the retry loop was simply broken"
    )
    # Short-circuiting is an optimisation, not a different verdict.
    assert result.returncode == 0
    assert "::warning::" in result.stdout


def _extension_gh(tmp_path: Path, *, served_bytes: str | None = None,
                  download_rc: int = 0, download_stderr: str = "",
                  rest: str = "") -> str:
    """A gh stub that models the real install path: download, then install.

    ``served_bytes`` is what ``gh release download`` writes to ``--output``;
    the step hashes exactly that and compares it to its pinned digest. Pass
    content other than ``_FAKE_GH_IMAGE_BYTES`` to simulate a retargeted tag
    or a swapped release asset.
    """
    if served_bytes is None:
        served_bytes = _FAKE_GH_IMAGE_BYTES
    payload = tmp_path / "served-gh-image"
    payload.write_text(served_bytes, encoding="utf-8")
    installed = tmp_path / "installed"
    q_payload = shlex.quote(str(payload))
    q_installed = shlex.quote(str(installed))
    return f"""
        case "$*" in
          *"extension list"*)
             if [ -f {q_installed} ]; then echo "gh image"; fi
             exit 0 ;;
          *"release download"*)
             if [ {download_rc} -ne 0 ]; then
               echo "{download_stderr}" >&2; exit {download_rc}
             fi
             out=""
             prev=""
             for a in "$@"; do
               if [ "$prev" = "--output" ]; then out="$a"; fi
               prev="$a"
             done
             cp {q_payload} "$out"
             exit 0 ;;
          *"extension install"*)
             # Mirror real gh (measured 2026-09-23, gh 2.87.3): ONLY "." is a
             # local-directory install; any other argument -- an absolute
             # path included -- is parsed as [HOST/]OWNER/REPO and rejected.
             # The previous stub accepted anything, which is how
             # `gh extension install "$ext_dir"` shipped green and failed
             # every live publish on a runner without a cached install.
             if [ "${{3:-}}" != "." ]; then
               echo "expected the \"[HOST/]OWNER/REPO\" format, got \"${{3:-}}\"" >&2
               exit 1
             fi
             if [ ! -x ./gh-image ]; then
               echo "extension directory has no executable gh-image" >&2
               exit 1
             fi
             touch {q_installed}; exit 0 ;;
        esac
        {textwrap.dedent(rest)}
    """


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_gh_image_binary_that_does_not_match_its_pin_is_fatal(tmp_path):
    """FleetReview F4 (P0): a mutable tag must not decide which bytes run.

    ``gh extension install --pin v1.2.0`` re-resolves a MUTABLE tag at
    install time, so whoever controls the upstream repository can retarget
    it and this step would execute the replacement with the workflow
    token's permissions.

    A commit pin is not available: measured 2026-09-20, ``--pin <commit>``
    exits 1 with "Could not find a release of drogers0/gh-image for
    44f4b9..." because gh accepts a release tag only for a BINARY
    extension. So the digest is what constrains the bytes, and a mismatch
    must be RED — never installed, never tolerated as transient.
    """
    result = _run_step_raw(tmp_path, _extension_gh(
        tmp_path, served_bytes="#!/bin/sh\necho totally-different-binary\n"))

    assert result.returncode != 0, (
        "a gh-image binary that did not match its pinned digest was accepted; "
        f"stdout={result.stdout!r}"
    )
    assert "does not match its pinned digest" in (result.stdout + result.stderr)
    assert "::warning::" not in result.stdout, (
        "a supply-chain mismatch was reported as a tolerated transient"
    )
    assert not (tmp_path / "installed").exists(), (
        "the unverified binary was installed anyway"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_matching_gh_image_binary_is_installed(tmp_path):
    """The positive control: verification must not reject the real asset.

    Without this, widening the check to always-fail would still pass the
    mismatch test above, and the step would simply never install anything.
    """
    result = _run_step_raw(tmp_path, _extension_gh(tmp_path, rest="""
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*) echo skipped ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """))

    assert result.returncode == 0, (
        f"the correctly-pinned binary was rejected; stderr={result.stderr!r}"
    )
    assert (tmp_path / "installed").exists(), (
        "the verified binary was never installed"
    )


def test_the_shipped_gh_image_pin_is_a_digest_not_just_a_tag():
    """The shipped default must pin CONTENT, not a mutable tag.

    ``_step_env`` overrides the digest so tests can run on any
    architecture; that override must not become the real pin, and the
    shipped script must carry a real 64-hex SHA-256 for each supported
    architecture.
    """
    wrapper = _SCRIPT.read_text(encoding="utf-8")
    digests = re.findall(r"GH_IMAGE_SHA256_\w+='([0-9a-f]{64})'", wrapper)
    assert len(digests) >= 2, (
        "the shipped script does not pin a SHA-256 per supported "
        f"architecture; found {digests!r}"
    )
    assert _FAKE_GH_IMAGE_SHA256 not in digests, (
        "the test digest leaked into the shipped pin"
    )
    assert not re.search(r"^\s*gh extension install .*--pin", wrapper, re.M), (
        "the install still pins a mutable tag via --pin instead of "
        "verifying the downloaded asset's digest"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_second_attempt_does_not_fail_on_an_already_installed_extension(tmp_path):
    """``gh extension install`` FAILS when the extension already exists.

    Unguarded, the first attempt installs it and the SECOND attempt dies on
    the install — so the retry loop would manufacture a failure of its own
    and never reach the publish it was retrying for.

    The presence guard must also RECOGNISE our own install: gh renders a
    locally-installed extension as ``gh image`` with no owner/repo column
    (measured 2026-09-20), so a guard matching the ``gh-image`` slug misses
    it and reinstalls on every attempt.
    """
    counter = tmp_path / "calls"
    q_counter = shlex.quote(str(counter))
    installed = tmp_path / "installed"
    q_installed = shlex.quote(str(installed))
    result = _run_step_raw(tmp_path, _extension_gh(tmp_path, rest=f"""
        case "$*" in
          *"extension install"*)
             if [ -f {q_installed} ]; then
               echo "gh: extension already installed" >&2; exit 1
             fi
             touch {q_installed}; exit 0 ;;
          *"/pulls"*)
             n=$(cat {q_counter} 2>/dev/null || echo 0)
             n=$((n + 1)); echo "$n" > {q_counter}
             if [ "$n" -le 1 ]; then
               echo "gh: You have exceeded a secondary rate limit" >&2; exit 1
             fi
             echo 4242 ;;
          *"/jobs"*) echo skipped ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """))

    assert int(counter.read_text()) >= 2, (
        "the retry never reached the PR lookup — the extension install on "
        f"attempt two aborted it; stdout={result.stdout!r}"
    )
    assert result.returncode == 0, (
        f"the retry attempt failed on the already-installed extension; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# CLASS-SWEEP (reviewer round 3): the supply-chain guards in
# ``ensure_extension`` were all individually disableable with the suite
# staying green. Root cause was structural, not six oversights: ``_step_env``
# ALWAYS supplies PUBLISH_EVIDENCE_GH_IMAGE_ASSET/_SHA256 so the
# architecture dispatch and its "no pinned asset" refusal were unreachable,
# and ``_extension_gh``'s ``download_rc`` parameter had no caller so the
# download-failure path was never driven either.
#
# Mutants that survived before these tests (both directions, tree restored
# after each):
#   D2a  the missing-pin branch skipped entirely
#   D2b  the refusal returning 0 instead of the supply-chain code
#   D2c  only the asset checked, an empty digest ignored
#   D2d  an unknown architecture falling back to the amd64 pin
#   D3a  a sha256 that could not be computed treated as a match
#   D5a  a failed release download swallowed rather than propagated
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("unpinned", ["asset", "digest", "both"])
def test_an_unpinned_gh_image_is_refused_rather_than_installed(tmp_path, unpinned):
    """No pin means no install — never an unverified binary.

    Closes D2a and D2c. With no pinned digest there is nothing to verify
    against, so installing anyway would execute whatever the mutable tag
    currently resolves to with the workflow token's permissions, which is
    exactly the P0 this lane exists to prevent. Checking only the asset
    (D2c) is the same hole: an empty digest still compares unequal later,
    but the refusal must happen before the download, not by accident.

    The overrides are REMOVED rather than emptied, and the architecture is
    driven to an unsupported one, because the step resolves its pin with
    ``${VAR:-default}`` — an empty value silently falls back to the shipped
    pin and would test nothing.
    """
    overrides: dict = {
        "asset": {"PUBLISH_EVIDENCE_GH_IMAGE_ASSET": None},
        "digest": {"PUBLISH_EVIDENCE_GH_IMAGE_SHA256": None},
        "both": {"PUBLISH_EVIDENCE_GH_IMAGE_ASSET": None,
                 "PUBLISH_EVIDENCE_GH_IMAGE_SHA256": None},
    }[unpinned]

    result = _run_step_raw(
        tmp_path, _extension_gh(tmp_path), extra_env=overrides,
        uname_machine="s390x")

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"an unpinned gh-image ({unpinned}) was accepted; stdout={result.stdout!r}"
    )
    assert "refusing to install an unverified binary" in combined, (
        f"the refusal never fired; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert not (tmp_path / "installed").exists(), (
        "an unpinned binary was installed"
    )
    assert "::warning::" not in result.stdout, (
        "an unpinned binary was reported as a tolerated transient"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_the_unpinned_refusal_is_not_tolerated_as_transient(tmp_path):
    """Closes D2b: the refusal must carry the supply-chain exit code.

    Returning 0 there would make an unsupported architecture publish
    nothing and report success, and returning the transient code would
    route it to NEUTRAL. Both are silent; a supply-chain refusal is RED.
    """
    result = _run_step_raw(
        tmp_path, _extension_gh(tmp_path), uname_machine="s390x",
        extra_env={"PUBLISH_EVIDENCE_GH_IMAGE_SHA256": None,
                   "PUBLISH_EVIDENCE_GH_IMAGE_ASSET": None})

    assert result.returncode == _supply_chain_exit_code(), (
        "the unpinned refusal did not exit with the supply-chain code "
        f"{_supply_chain_exit_code()}; got {result.returncode}"
    )
    assert "::warning::" not in result.stdout
    assert "conclusion=neutral" not in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_an_unsupported_architecture_has_no_pin_and_refuses(tmp_path):
    """Closes D2d: an unknown arch must not borrow another arch's digest.

    The amd64 digest cannot describe an arm64 (or s390x) asset, so falling
    back to it either rejects every download on a valid host or, worse,
    silently pins the wrong bytes. The shipped code empties both variables
    for an unrecognised machine; this drives that branch with a fake
    ``uname`` so it is exercised regardless of the host running the suite.
    """
    result = _run_step_raw(
        tmp_path, _extension_gh(tmp_path), uname_machine="s390x",
        extra_env={"PUBLISH_EVIDENCE_GH_IMAGE_ASSET": None,
                   "PUBLISH_EVIDENCE_GH_IMAGE_SHA256": None})

    combined = result.stdout + result.stderr
    assert result.returncode == _supply_chain_exit_code(), (
        f"an unsupported architecture did not refuse; stdout={result.stdout!r}"
    )
    assert "No pinned gh-image asset for architecture" in combined
    assert not (tmp_path / "installed").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_supported_architecture_does_resolve_a_pin(tmp_path):
    """The positive control for the architecture dispatch.

    Without this, emptying the pin for EVERY architecture would satisfy the
    unsupported-arch test above while disabling the extension entirely.
    """
    result = _run_step_raw(
        tmp_path, _extension_gh(tmp_path, rest="""
        case "$*" in
          *"/pulls"*) echo 4242 ;;
          *"/jobs"*) echo skipped ;;
          *"/artifacts"*) ;;
          *) exit 0 ;;
        esac
    """), uname_machine="x86_64",
        extra_env={"PUBLISH_EVIDENCE_GH_IMAGE_ASSET": None})

    combined = result.stdout + result.stderr
    assert "No pinned gh-image asset for architecture" not in combined, (
        "a supported architecture resolved no pin"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_an_uncomputable_digest_is_refused_not_assumed_to_match(tmp_path):
    """Closes D3a: no hash tool means no install.

    If ``sha256_of`` fails and the code substitutes the expected digest, the
    comparison trivially passes and an unverified binary runs. The refusal
    must be explicit and must not be transient-tolerated.
    """
    # Neither sha256sum nor shasum usable: the step's own helper then
    # returns non-zero, which is the branch under test. Shadowing the two
    # tools beats emptying PATH, which would also break bash's own builtins
    # lookup and fail the step for an unrelated reason.
    result = _run_step_raw(
        tmp_path, _extension_gh(tmp_path), break_hashers=True)

    combined = result.stdout + result.stderr
    assert result.returncode == _supply_chain_exit_code(), (
        f"an uncomputable digest was tolerated; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "Could not compute a SHA-256" in combined
    assert not (tmp_path / "installed").exists(), (
        "a binary whose digest could not be computed was installed"
    )
    assert "::warning::" not in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_failed_release_download_is_propagated_not_swallowed(tmp_path):
    """Closes D5a: a download failure must not fall through to the hash.

    Swallowing it leaves no asset on disk, so the step would go on to hash
    a missing file and report a confusing digest error — or, if that were
    also tolerated, install nothing and claim success. The download spends
    the rate-limit budget, so its failure is deliberately eligible for
    ordinary transient classification; what it must never be is ignored.
    """
    result = _run_step_raw(tmp_path, _extension_gh(
        tmp_path, download_rc=1,
        download_stderr="HTTP 503: Service Unavailable"))

    assert result.returncode == 0, (
        "a transient download failure should reach the NEUTRAL path; "
        f"got {result.returncode}"
    )
    assert "::warning::" in result.stdout, (
        "a swallowed download failure produced a silent green; "
        f"stdout={result.stdout!r}"
    )
    assert not (tmp_path / "installed").exists(), (
        "the extension was installed despite the download failing"
    )


def _supply_chain_exit_code() -> int:
    """Read the shipped supply-chain exit code rather than hardcoding it.

    Hardcoding 78 in the tests would let a change to the constant drift
    away from the assertions that are supposed to pin it.
    """
    match = re.search(r"^SUPPLY_CHAIN_EXIT_CODE=(\d+)",
                      _SCRIPT.read_text(encoding="utf-8"), re.M)
    assert match, "the step no longer defines SUPPLY_CHAIN_EXIT_CODE"
    return int(match.group(1))
