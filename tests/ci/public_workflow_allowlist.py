"""Public-mirror workflow allowlist, as the trigger-shape tests read it (card t_becd75ae).

On ANG-Repositories/hermes-agent only the workflows listed in
.github/ci/public-workflow-allowlist.txt may self-start (t_868e850a); every other workflow
is workflow_dispatch / workflow_call only (scripts/lints/public-workflow-allowlist-lint.py).
A test that pins a workflow's self-start triggers keeps its original assertion when the
workflow is allowlisted, or when no allowlist exists, and asserts the dispatch-only shape otherwise.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / ".github" / "ci" / "public-workflow-allowlist.txt"
INERT = {"workflow_dispatch", "workflow_call"}


def self_start_allowed(workflow: str) -> bool:
    """True when `workflow` (a file name under .github/workflows/) may keep its own triggers."""
    if not ALLOWLIST.is_file():
        return True
    allowed = set()
    for line in ALLOWLIST.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            allowed.add(line)
    return f".github/workflows/{workflow}" in allowed


def assert_dispatch_only(on, workflow: str) -> None:
    """The non-allowlisted shape: only workflow_dispatch / workflow_call, and at least one of them."""
    triggers = {on} if isinstance(on, str) else set(on or ())
    assert triggers and triggers <= INERT, (
        f"{workflow} is not in {ALLOWLIST.relative_to(ROOT)}: triggers must be workflow_dispatch/"
        f"workflow_call only, got {sorted(triggers)}"
    )
