"""Identity invariants for the JS autofix producer workflow."""

from pathlib import Path

import yaml


_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "js-autofix.yml"
)
_TRUSTED_TOKEN = "${{ secrets.AUTOFIX_BOT_PAT }}"


def _apply_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["apply-patch"]["steps"]


def _named_step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_push_and_pr_operations_share_trusted_maintainer_identity():
    """Bot-actor pushes require approval, so every mutation must use the PAT."""
    steps = _apply_steps()

    guard = _named_step(steps, "Require trusted automation token")
    assert guard["env"]["AUTOFIX_TOKEN"] == _TRUSTED_TOKEN

    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["token"] == _TRUSTED_TOKEN

    for name in (
        "Create/update PR and enable auto-merge",
        "Wait for merge, auto-close on failure or stale",
    ):
        assert _named_step(steps, name)["env"]["GH_TOKEN"] == _TRUSTED_TOKEN
