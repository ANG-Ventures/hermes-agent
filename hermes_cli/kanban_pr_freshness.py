"""Handoff freshness gate: make an OPEN PR landable while the worker still owns it.

Runs inside :func:`hermes_cli.kanban_db.complete_task` on the OPEN PRs a
worker's handoff names (the same refs :mod:`kanban_open_pr` routes to review),
BEFORE the card leaves the worker. Card t_14b81673 (2026-09-25):

(a) DRAFT -> refuse. A draft PR is not a handoff (hermes-agent#821 sat as a
    draft in the review lane). :class:`DraftPrError` is raised; the card stays
    in-flight so the worker can ``gh pr ready`` and retry.
(b) STALE -> update. A head more than :data:`STALE_BEHIND_MAX` (20, the same
    number as fleet-merge.sh ``FLEET_MERGE_STALE_BASE_MAX``) commits behind its
    base is updated with ``PUT .../update-branch`` bound to the head SHA we
    measured (``expected_head_sha``), so fleet-merge never answers rc=12 for it
    (#1218, #1123). CI re-runs on the new head.
(c) GREEN -> arm. A fresh, non-draft PR whose head check-runs are all green on a
    non-milestone card is handed to ``scripts/fleet-merge.sh`` (the only
    sanctioned merge lane; it applies the FleetReview gate, attribution and
    SHA-bound arm itself). Spawned detached: the handoff never waits on it.

Fail-OPEN everywhere except (a): a GitHub lookup error, an update-branch
failure or a missing fleet-merge.sh is recorded and the handoff proceeds —
the pre-existing behavior. A draft is only refused on a positive ``draft: true``
read. Kill switches: ``KANBAN_HANDOFF_FRESHNESS=0`` disables the whole gate,
``KANBAN_HANDOFF_AUTOMERGE=0`` only (c).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger(__name__)

STALE_BEHIND_MAX = 20
_TIMEOUT_S = 8
_GREEN = {"success", "skipped", "neutral"}

GhFn = Callable[..., Optional[dict]]


class DraftPrError(ValueError):
    """A handoff named an OPEN PR that GitHub reports as a draft."""

    def __init__(self, task_id: str, prs: list):
        self.task_id = task_id
        self.prs = list(prs)
        super().__init__(
            f"handoff refused: {', '.join(self.prs)} "
            f"{'is a DRAFT PR' if len(self.prs) == 1 else 'are DRAFT PRs'}. "
            f"A draft is not landable and must not enter the review lane. "
            f"Run `gh pr ready <n> -R <repo>` (or name the right PR), then retry. "
            f"{task_id} is still in-flight (no state change)."
        )


def enabled() -> bool:
    return os.environ.get("KANBAN_HANDOFF_FRESHNESS", "1").strip() != "0"


def automerge_enabled() -> bool:
    return os.environ.get("KANBAN_HANDOFF_AUTOMERGE", "1").strip() != "0"


def gh_api(*args: str) -> Optional[dict]:
    """``gh api <args>`` parsed as JSON, or None on any failure (fail-open)."""
    try:
        proc = subprocess.run(
            ["gh", "api", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        _log.warning("handoff freshness: gh api %s rc=%s: %s", args[:3], proc.returncode,
                     (proc.stderr or "").strip()[:200])
        return None
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _default_gh() -> Optional[GhFn]:
    # Same seam as kanban_open_pr._default_query: no live GitHub from pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return gh_api


def _ci_green(gh: GhFn, repo: str, sha: str) -> bool:
    runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    items = (runs or {}).get("check_runs")
    if not isinstance(items, list) or not items:
        return False
    for run in items:
        if not isinstance(run, dict):
            return False
        if run.get("status") != "completed" or str(run.get("conclusion") or "") not in _GREEN:
            return False
    status = gh(f"repos/{repo}/commits/{sha}/status")
    state = str((status or {}).get("state") or "")
    # "pending" with zero statuses is GitHub's shape for "no legacy statuses".
    if state == "failure" or state == "error":
        return False
    if state == "pending" and (status or {}).get("total_count"):
        return False
    return True


def _fleet_merge_path() -> Optional[Path]:
    override = os.environ.get("KANBAN_HANDOFF_FLEET_MERGE")
    if override:
        p = Path(override)
    else:
        from hermes_constants import get_default_hermes_root
        p = get_default_hermes_root() / "scripts" / "fleet-merge.sh"
    return p if p.is_file() else None


def spawn_arm(repo: str, number: int, sha: str, task_id: str) -> Optional[str]:
    """Hand the PR to fleet-merge.sh, detached. Returns the log path or None."""
    script = _fleet_merge_path()
    if script is None:
        return None
    from hermes_constants import get_default_hermes_root
    log_dir = get_default_hermes_root() / "logs" / "handoff-automerge"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}-{repo.replace('/', '_')}-{number}.log"
    argv = [
        "bash", str(script), repo, str(number),
        "--by", "kanban-handoff/freshness-gate",
        "--reason", f"handoff freshness gate: CI green on head {sha[:12]}, card {task_id}",
        "--sha", sha, "--card", task_id,
    ]
    try:
        with open(log_path, "ab") as fh:
            subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        _log.warning("handoff freshness: fleet-merge spawn failed: %s", exc)
        return None
    return str(log_path)


ArmFn = Callable[[str, int, str, str], Optional[str]]


def check(refs, *, task_id: str, allow_arm: bool, gh: Optional[GhFn] = None,
          arm: Optional[ArmFn] = None, behind_max: int = STALE_BEHIND_MAX) -> dict:
    """Apply (a)/(b)/(c) to OPEN PR ``refs``. Raises :class:`DraftPrError`.

    Returns a report ``{"prs": {"o/r#n": {...}}}`` for the handoff metadata.
    All draft checks run before any mutation, so a refused handoff has changed
    nothing on GitHub.
    """
    report: dict = {"prs": {}, "checked_at": int(time.time())}
    if not refs or not enabled():
        return report
    if gh is None:
        gh = _default_gh()
        if gh is None:
            return report
    arm = arm or spawn_arm

    views = []
    drafts = []
    for ref in refs:
        key = f"{ref.repo}#{ref.number}"
        pr = gh(f"repos/{ref.repo}/pulls/{ref.number}")
        entry: dict = {}
        report["prs"][key] = entry
        if not isinstance(pr, dict):
            entry["lookup"] = "failed (fail-open)"
            continue
        if pr.get("draft") is True:
            drafts.append(key)
            continue
        views.append((ref, key, pr, entry))
    if drafts:
        raise DraftPrError(task_id, drafts)

    for ref, key, pr, entry in views:
        head = str(((pr.get("head") or {}).get("sha")) or "")
        base = str(((pr.get("base") or {}).get("ref")) or "")
        entry["head"] = head
        if not head or not base:
            entry["lookup"] = "no head/base (fail-open)"
            continue
        cmp = gh(f"repos/{ref.repo}/compare/{base}...{head}")
        behind = (cmp or {}).get("behind_by")
        if not isinstance(behind, int):
            entry["behind_by"] = None
            continue
        entry["behind_by"] = behind
        if behind > behind_max:
            upd = gh("-X", "PUT", f"repos/{ref.repo}/pulls/{ref.number}/update-branch",
                     "-f", f"expected_head_sha={head}")
            entry["update_branch"] = "requested" if upd is not None else "failed (fail-open)"
            # The new head has fresh CI; arming is fleet-merge's job once it is green.
            continue
        if allow_arm and automerge_enabled() and _ci_green(gh, ref.repo, head):
            log = arm(ref.repo, ref.number, head, task_id)
            entry["automerge"] = f"fleet-merge spawned ({log})" if log else "not spawned"
        elif allow_arm and automerge_enabled():
            entry["automerge"] = "ci not green; not armed"
    return report
