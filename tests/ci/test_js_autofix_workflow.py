"""Identity invariants for the JS autofix producer workflow."""

from pathlib import Path

import yaml


_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "js-autofix.yml"
)
_APP_TOKEN = "${{ steps.app-token.outputs.token }}"


def _apply_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["apply-patch"]["steps"]


def _named_step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_push_and_pr_operations_use_fleet_app_token():
    """Push and PR calls use the ang-fleet-workers App token.

    A personal PAT gets a 403 on ANG-Ventures repos (t_7d5d7258). The job token
    may not create PRs here, and its pushes queue bot PR checks for approval.
    """
    steps = _apply_steps()

    mint = next(step for step in steps if step.get("id") == "app-token")
    assert str(mint["uses"]).startswith("actions/create-github-app-token@")
    assert mint["with"]["app-id"] == "${{ secrets.FLEET_WORKERS_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.FLEET_WORKERS_APP_PRIVATE_KEY }}"
    assert steps.index(mint) == 0

    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["token"] == _APP_TOKEN

    for name in (
        "Create/update PR and enable auto-merge",
        "Wait for merge, auto-close on failure or stale",
    ):
        assert _named_step(steps, name)["env"]["GH_TOKEN"] == _APP_TOKEN

    rendered = yaml.safe_dump(steps)
    assert "AUTOFIX_BOT_PAT" not in rendered
    assert "github.token" not in rendered


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
