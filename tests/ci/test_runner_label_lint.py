"""Every workflow job must request only runner labels the org can serve."""

import importlib.util
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ci_runner_label_lint", ROOT / "scripts" / "ci_runner_label_lint.py")
lint_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_mod)


def _wf(text):
    return yaml.safe_load(textwrap.dedent(text))


def test_repo_workflows_request_only_servable_labels():
    workflows = lint_mod.load_tree(ROOT)
    assert workflows, "workflow discovery must not silently become empty"
    assert lint_mod.lint(workflows) == []


def test_literal_larger_runner_fails():
    wf = _wf("""
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest-32-core
            steps: [{run: "true"}]
    """)
    errs = lint_mod.lint({"w.yml": wf})
    assert len(errs) == 1 and "ubuntu-latest-32-core" in errs[0]


def test_matrix_runner_value_fails():
    wf = _wf("""
        on: push
        jobs:
          a:
            strategy:
              matrix:
                include:
                  - runner: windows-latest
                  - runner: windows-latest-32-core
            runs-on: ${{ matrix.runner }}
            steps: [{run: "true"}]
    """)
    errs = lint_mod.lint({"w.yml": wf})
    assert len(errs) == 1 and "windows-latest-32-core" in errs[0]


def test_expression_ternary_and_json_array_fail():
    wf = _wf("""
        on: push
        jobs:
          a:
            runs-on: ${{ inputs.arch == 'arm64' && 'windows-latest-32-arm-core' || 'windows-latest' }}
            steps: [{run: "true"}]
          b:
            runs-on: ${{ fromJSON(vars.X || '["ubuntu-latest-96-core"]') }}
            steps: [{run: "true"}]
    """)
    errs = lint_mod.lint({"w.yml": wf})
    assert any("windows-latest-32-arm-core" in e for e in errs)
    assert any("ubuntu-latest-96-core" in e for e in errs)
    assert not any("'windows-latest'" in e for e in errs)


def test_reusable_workflow_input_from_caller_fails():
    callee = _wf("""
        on:
          workflow_call:
            inputs:
              runner: {type: string, default: ubuntu-latest}
        jobs:
          run:
            runs-on: ${{ inputs.runner }}
            steps: [{run: "true"}]
    """)
    caller = _wf("""
        on: push
        jobs:
          call:
            uses: ./.github/workflows/callee.yml
            with:
              runner: ubuntu-latest-32-core
    """)
    errs = lint_mod.lint({".github/workflows/callee.yml": callee, ".github/workflows/caller.yml": caller})
    assert len(errs) == 1 and "ubuntu-latest-32-core" in errs[0]


def test_servable_labels_pass():
    wf = _wf("""
        on: push
        jobs:
          a: {runs-on: ubuntu-latest, steps: [{run: "true"}]}
          b: {runs-on: [self-hosted, X64], steps: [{run: "true"}]}
          c: {runs-on: blacksmith-4vcpu-ubuntu-2404-arm, steps: [{run: "true"}]}
          d: {runs-on: "${{ fromJSON(vars.CI_RUNNER_LABELS || '[\\"ubuntu-latest\\"]') }}", steps: [{run: "true"}]}
    """)
    assert lint_mod.lint({"w.yml": wf}) == []


def test_upstream_repository_guard_exempts_job():
    wf = _wf("""
        on: push
        jobs:
          a:
            if: github.repository == 'NousResearch/hermes-agent' && true
            runs-on: ubuntu-latest-32-core
            steps: [{run: "true"}]
    """)
    assert lint_mod.lint({"w.yml": wf}) == []
