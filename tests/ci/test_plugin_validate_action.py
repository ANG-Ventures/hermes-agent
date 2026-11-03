"""The external action must never install Hermes into the plugin checkout."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import hermes_yaml as yaml
from tests._fixtures.bounds import uncapped_argv


@pytest.mark.platforms("posix")
def test_external_validator_checkout_uses_requested_ref_and_preserves_caller(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    action = yaml.safe_load((repo / ".github/actions/plugin-validate/action.yml").read_text())
    source_step = next(step for step in action["runs"]["steps"] if step.get("id") == "source")
    # Serve a small local repo, not this checkout or github.com: a depth-1 fetch of the whole
    # tree overran 60 s on the Studio and exited 128 on the mirror runner (t_f1a3e390). The
    # step's `prepare` needs only pm/ and the setup script. The requested ref is the FIRST of
    # two commits, so a checkout of the branch tip would fail the ref assertion.
    # The files are copied from the working tree, not `git archive`d: on the mirror runners git
    # against the runner's own checkout exits 128 (9/9 main runs 11-02..03, t_0fcbd3fa), so the
    # test must not depend on that checkout's git state.
    upstream = tmp_path / "upstream"
    shutil.copytree(repo / "pm", upstream / "pm", ignore=shutil.ignore_patterns("__pycache__"))
    (upstream / "scripts/ci").mkdir(parents=True)
    shutil.copy2(repo / "scripts/ci/setup_toolchain.py", upstream / "scripts/ci/setup_toolchain.py")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=upstream, check=True)
    subprocess.run([*git, "add", "-A"], cwd=upstream, check=True)
    subprocess.run([*git, "commit", "-qm", "requested"], cwd=upstream, check=True)
    ref = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "tip"], cwd=upstream, check=True)
    caller = tmp_path / "plugin"
    caller.mkdir()
    manifest = caller / "pyproject.toml"
    manifest.write_text('[project]\nname = "caller-plugin"\nversion = "1.0.0"\n')
    before = manifest.read_bytes()
    runner = tmp_path / "runner"
    runner.mkdir()
    output = tmp_path / "output"
    env_file = tmp_path / "env"
    env = {**os.environ, "RUNNER_TEMP": str(runner), "RUNNER_OS": "Linux",
           "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(env_file), "_HERMES_REF": ref,
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{upstream.as_uri()}.insteadOf",
           "GIT_CONFIG_VALUE_0": "https://github.com/NousResearch/hermes-agent.git"}
    # The step is a CI runner's shell, not code under the test's budget: under the harness's
    # 6 GiB RLIMIT_AS the local `git upload-pack` cannot start pack-objects' threads on a
    # 128-core box ("unable to create thread", ci-box, t_e9a7a83f).
    subprocess.run(uncapped_argv(["bash", "-c", source_step["run"]]), cwd=caller, env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    source = Path(outputs["source"])
    assert source.is_relative_to(runner)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() == ref
    assert manifest.read_bytes() == before
    assert sorted(path.name for path in caller.iterdir()) == ["pyproject.toml"]
    assert "python-version" in outputs  # the real setup-pm pin reader ran
    assert not (source / ".build/validator").exists()  # preparation is not installation
