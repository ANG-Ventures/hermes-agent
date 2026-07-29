"""Identity invariants for the JS autofix producer workflow."""

from pathlib import Path

import yaml


_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "js-autofix.yml"
)
_TRUSTED_TOKEN = "${{ secrets.AUTOFIX_BOT_PAT }}"
_JOB_TOKEN = "${{ github.token }}"


def _apply_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["apply-patch"]["steps"]


def _named_step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_push_uses_trusted_identity_and_pr_operations_use_job_token():
    """The PAT triggers checks; the job token has PR auto-merge permission."""
    steps = _apply_steps()

    guard = _named_step(steps, "Require trusted push token")
    assert guard["env"]["AUTOFIX_TOKEN"] == _TRUSTED_TOKEN

    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["token"] == _TRUSTED_TOKEN

    for name in (
        "Create/update PR and enable auto-merge",
        "Wait for merge, auto-close on failure or stale",
    ):
        assert _named_step(steps, name)["env"]["GH_TOKEN"] == _JOB_TOKEN


def test_auto_merge_is_rearmed_for_the_current_bot_head():
    """A stale request must be replaced before the PR enters the merge queue."""
    step = _named_step(_apply_steps(), "Create/update PR and enable auto-merge")
    merge_commands = [
        line.strip()
        for line in step["run"].splitlines()
        if line.strip().startswith("gh pr merge")
    ]

    assert merge_commands == [
        'gh pr merge "$PR_NUM" --disable-auto 2>/dev/null || true',
        'gh pr merge "$PR_NUM"',
    ]
