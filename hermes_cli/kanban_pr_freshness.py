"""Handoff freshness gate: make an OPEN PR landable while the worker still owns it.

Runs inside :func:`hermes_cli.kanban_db.complete_task` on the OPEN PRs a
worker's handoff names (the same refs :mod:`kanban_open_pr` routes to review),
BEFORE the card leaves the worker. Card t_14b81673 (2026-09-25):

(a) DRAFT -> refuse. A draft PR is not a handoff (hermes-agent#821 sat as a
    draft in the review lane). :class:`DraftPrError` is raised; the card stays
    in-flight so the worker can ``ace_gate.py ready`` and retry.
    Exception (t_c1ee61ae): the worker's OWN draft -- head branch names this card
    and the repo is listed in ``config/worker-pr-draft.json`` (the gh-shim opens
    those as drafts) -- is flipped ready via ``ace_gate.py ready`` (review hold /
    Ace gate still refuse) and not armed in the same handoff. Any foreign draft
    still refuses, before any flip. Kill switch ``KANBAN_HANDOFF_DRAFT_FLIP=0``.
(b) STALE -> update. A head more than :data:`STALE_BEHIND_MAX` (20, the same
    number as fleet-merge.sh ``FLEET_MERGE_STALE_BASE_MAX``) commits behind its
    base AND reported ``mergeable_state=behind`` (a strict up-to-date rule blocks
    the merge, t_39a33e70) is updated with ``PUT .../update-branch`` bound to the
    head SHA we measured (``expected_head_sha``). CI re-runs on the new head.
    Merge-queue repos are left to fleet-merge's own stale-base gate. A far-behind
    head whose ``mergeable_state`` is not computed yet (null/``unknown``) is
    deferred: neither updated nor armed (t_6395b273).
(c) GREEN -> arm. A fresh, non-draft PR whose head check-runs are all green on a
    non-milestone card is handed to ``scripts/fleet-merge.sh`` (the only
    sanctioned merge lane; it applies the FleetReview gate, attribution and
    SHA-bound arm itself). Spawned detached: the handoff never waits on it.
    Pipeline v2 (t_7dbacbbc): when ``config/merge-gate-profile.json`` reads v2
    for the repo (:func:`merge_gate_profile`), a red/pending/no-CI head is armed
    too, its CI state REPORTED in the fleet-merge ``--reason`` (CI gates deploy,
    not merge). Absent/unreadable/unknown profile = v1 = green only.

Fail-OPEN everywhere except (a): a GitHub lookup error, an update-branch
failure or a missing fleet-merge.sh is recorded and the handoff proceeds —
the pre-existing behavior. A draft is only refused on a positive ``draft: true``
read. Kill switches: ``KANBAN_HANDOFF_FRESHNESS=0`` disables the whole gate,
``KANBAN_HANDOFF_AUTOMERGE=0`` only (c).

Per-card override for (a) (t_f38605be): ``check(..., draft_ok=True)`` does not
raise for drafts; it returns them in ``report["draft_override"]`` so the caller
(``complete_task(draft_ok="<reason>")``) can record an audited
``completion_draft_override`` event and drop the intentionally-open draft
vehicle from the open-PR route. The default refusal is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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
            f"Run `~/.hermes/scripts/ace_gate.py ready <owner/repo> <n>` (or name "
            f"the right PR), then retry; rc=21 = a reviewer review-hold: leave it "
            f"draft and complete with --draft-ok 'reviewer hold: <who>'. "
            f"If the draft is otherwise INTENTIONALLY left open, complete with "
            f"--draft-ok/draft_ok \"<reason>\" (audited). "
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
            text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT_S, check=False,
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


def _ci_report(gh: GhFn, repo: str, sha: str) -> str:
    """'' when the head is CI-green, else a one-line description of why not (red/pending/none/unreadable)."""
    runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    items = (runs or {}).get("check_runs")
    if not isinstance(items, list):
        return "check-runs unreadable"
    if not items:
        return "no check runs"
    red, pending = [], []
    for run in items:
        if not isinstance(run, dict):
            return "check-runs unreadable"
        name = str(run.get("name") or "?")
        if run.get("status") != "completed":
            pending.append(name)
        elif str(run.get("conclusion") or "") not in _GREEN:
            red.append(name)
    parts = []
    if red:
        parts.append("red: " + ", ".join(sorted(set(red))[:5]))
    if pending:
        parts.append("pending: " + ", ".join(sorted(set(pending))[:5]))
    status = gh(f"repos/{repo}/commits/{sha}/status")
    state = str((status or {}).get("state") or "")
    # "pending" with zero statuses is GitHub's shape for "no legacy statuses".
    if state in ("failure", "error") or (state == "pending" and (status or {}).get("total_count")):
        parts.append(f"commit status {state}")
    return "; ".join(parts)


def _no_post_ready_ci(gh: GhFn, repo: str, sha: str) -> bool:
    """True when ``sha`` has check-runs and every one concluded ``skipped`` (the draft-time shape).

    Unreadable -> False: the normal CI report path already refuses or reports it."""
    runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    items = (runs or {}).get("check_runs")
    if not isinstance(items, list) or not items:
        return False
    return all(isinstance(r, dict) and r.get("status") == "completed"
               and r.get("conclusion") == "skipped" for r in items)


def _ci_green(gh: GhFn, repo: str, sha: str) -> bool:
    return _ci_report(gh, repo, sha) == ""


def merge_gate_profile(repo: str, path: Optional[str] = None) -> str:
    """Pipeline v2 merge-gate profile for ``repo`` (t_7dbacbbc): ``"v2"`` or ``"v1"``.

    Same semantics as hermes-home ``scripts/lib/merge_gate_profile.py`` (this package cannot import it): reads
    ``<hermes root>/config/merge-gate-profile.json`` (``{"profile": "v1"|"v2", "repos": [...]}``). Absent,
    unreadable or unknown -> v1; v2 only when the profile is v2 AND ``repo`` is listed (case-insensitive).
    ``KANBAN_MERGE_GATE_PROFILE`` overrides the path (tests).
    """
    if path is None:
        path = os.environ.get("KANBAN_MERGE_GATE_PROFILE") or ""
        if not path:
            try:
                from hermes_constants import get_default_hermes_root
                path = str(get_default_hermes_root() / "config" / "merge-gate-profile.json")
            except Exception:  # noqa: BLE001 -- fail-safe: v1
                return "v1"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return "v1"
    if not isinstance(d, dict) or d.get("profile") not in ("v1", "v2"):
        return "v1"
    repos = d.get("repos", ["ANG-Ventures/hermes-home"])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        return "v1"
    return "v2" if d["profile"] == "v2" and repo.casefold() in {r.casefold() for r in repos} else "v1"


def _fleet_merge_path() -> Optional[Path]:
    override = os.environ.get("KANBAN_HANDOFF_FLEET_MERGE")
    if override:
        p = Path(override)
    else:
        from hermes_constants import get_default_hermes_root
        p = get_default_hermes_root() / "scripts" / "fleet-merge.sh"
    return p if p.is_file() else None


def spawn_arm(repo: str, number: int, sha: str, task_id: str, ci_report: str = "") -> Optional[str]:
    """Hand the PR to fleet-merge.sh, detached. Returns the log path or None.

    ``ci_report`` (t_7dbacbbc, profile v2 only): the non-green CI state, carried into ``--reason``."""
    script = _fleet_merge_path()
    if script is None:
        return None
    from hermes_constants import get_default_hermes_root
    log_dir = get_default_hermes_root() / "logs" / "handoff-automerge"
    log_path = log_dir / f"{task_id}-{repo.replace('/', '_')}-{number}.log"
    reason = (f"handoff freshness gate: CI reported not green on head {sha[:12]} ({ci_report}; "
              f"v2: CI gates deploy), card {task_id}") if ci_report else \
        f"handoff freshness gate: CI green on head {sha[:12]}, card {task_id}"
    argv = [
        "bash", str(script), repo, str(number),
        "--by", "kanban-handoff/freshness-gate",
        "--reason", reason,
        "--sha", sha, "--card", task_id,
    ]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)  # inside the fail-open handler (P2 #15)
        with open(log_path, "ab") as fh:
            subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        _log.warning("handoff freshness: fleet-merge spawn failed: %s", exc)
        return None
    return str(log_path)


ArmFn = Callable[..., Optional[str]]   # (repo, number, sha, task_id[, ci_report]) -> log path
ReadyFn = Callable[[str, int], Optional[int]]   # (repo, number) -> ace_gate.py rc, None = not run


def worker_draft_flip_enabled() -> bool:
    return os.environ.get("KANBAN_HANDOFF_DRAFT_FLIP", "1").strip() != "0"


def worker_draft_repos(path: Optional[str] = None) -> set:
    """Casefolded repos listed in ``<hermes root>/config/worker-pr-draft.json`` (t_c1ee61ae).

    The file the gh-shim reads to open worker PRs as drafts (t_ea770a43). Absent/unreadable/
    malformed -> empty set (no flip: the draft refusal is unchanged). ``KANBAN_WORKER_PR_DRAFT``
    overrides the path (tests)."""
    if path is None:
        path = os.environ.get("KANBAN_WORKER_PR_DRAFT") or ""
        if not path:
            try:
                from hermes_constants import get_default_hermes_root
                path = str(get_default_hermes_root() / "config" / "worker-pr-draft.json")
            except Exception:  # noqa: BLE001 -- fail-safe: no flip
                return set()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return set()
    repos = d.get("repos") if isinstance(d, dict) else None
    if not isinstance(repos, list):
        return set()
    return {r.casefold() for r in repos if isinstance(r, str)}


def _owns_draft(ref, pr: dict, task_id: str) -> bool:
    """The worker's own draft: head branch names THIS card and the repo is a worker-draft repo."""
    from hermes_cli.kanban_open_pr import branch_names_card
    head_ref = str(((pr.get("head") or {}).get("ref")) or "")
    return branch_names_card(head_ref, task_id) and ref.repo.casefold() in worker_draft_repos()


def ace_gate_ready(repo: str, number: int) -> Optional[int]:
    """``scripts/ace_gate.py ready <repo> <n>`` (the only sanctioned un-draft). rc, or None if not run."""
    from hermes_constants import get_default_hermes_root
    script = get_default_hermes_root() / "scripts" / "ace_gate.py"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable or "python3", str(script), "ready", repo, str(number)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=90, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("handoff freshness: ace_gate ready %s#%s failed: %s", repo, number, exc)
        return None
    if proc.returncode != 0:
        _log.warning("handoff freshness: ace_gate ready %s#%s rc=%s: %s", repo, number,
                     proc.returncode, (proc.stderr or "").strip()[:300])
    return proc.returncode


def _default_ready() -> Optional[ReadyFn]:
    # Same seam as _default_gh: never un-draft a live PR from pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return ace_gate_ready


def check(refs, *, task_id: str, allow_arm: bool, gh: Optional[GhFn] = None,
          arm: Optional[ArmFn] = None, behind_max: int = STALE_BEHIND_MAX,
          armable=None, draft_ok: bool = False, ready: Optional[ReadyFn] = None) -> dict:
    """Apply (a)/(b)/(c) to OPEN PR ``refs``. Raises :class:`DraftPrError`.

    ``armable`` is the set of ``"o/r#n"`` keys this card actually handed off
    (``metadata.pr_url``/``pr``/``pr_urls`` or ``--survivor-pr``). Only those
    are ever armed for merge: a PR merely MENTIONED in summary/result prose is
    context, not merge authorization. ``complete_task`` always passes it;
    ``None`` (direct callers) keeps the legacy every-ref behavior.

    Returns a report ``{"prs": {"o/r#n": {...}}}`` for the handoff metadata.
    All draft checks run before any mutation, so a refused handoff has changed
    nothing on GitHub. With ``draft_ok`` drafts are not refused: they are listed
    in ``report["draft_override"]`` and skipped by (b)/(c) (the caller audits).
    """
    report: dict = {"prs": {}, "checked_at": int(time.time())}
    # Never update-branch or arm a third-party PR (t_06dccfe3): the caller
    # already passes fleet refs only; this keeps the actuator safe on its own.
    from hermes_cli.kanban_open_pr import is_fleet_ref
    refs = [ref for ref in (refs or []) if is_fleet_ref(ref)]
    if not refs or not enabled():
        return report
    if gh is None:
        gh = _default_gh()
        if gh is None:
            return report
    arm = arm or spawn_arm
    armable_keys = None if armable is None else {str(k).lower() for k in armable}

    views = []
    drafts = []
    own_drafts: dict = {}
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
            entry["draft"] = True
            if worker_draft_flip_enabled() and _owns_draft(ref, pr, task_id):
                own_drafts[key] = (ref, key, pr, entry)
            continue
        views.append((ref, key, pr, entry))
    if drafts and not draft_ok:
        # t_c1ee61ae: the worker's OWN draft (head branch names this card, repo listed in
        # config/worker-pr-draft.json) is flipped ready through ace_gate.py instead of refused.
        # A foreign draft still refuses, before any flip; a failed flip (review hold rc=21, Ace-gated
        # rc=20, read failure) refuses like before.
        own = {k for k in drafts if k in own_drafts}
        foreign = [k for k in drafts if k not in own]
        if foreign or not own:
            raise DraftPrError(task_id, drafts)
        ready = ready or _default_ready()
        failed = []
        for ref, key, pr, entry in own_drafts.values():
            rc = ready(ref.repo, ref.number) if ready is not None else None
            entry["draft_flip"] = f"ready via ace_gate.py (rc={rc})"
            if rc != 0:
                failed.append(key)
                continue
            entry["draft"] = False
            views.append((ref, key, dict(pr, draft=False), entry))
        if failed:
            raise DraftPrError(task_id, failed)
        report["draft_flipped"] = sorted(own)
    elif drafts:
        report["draft_override"] = drafts

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
        # t_39a33e70: update only when GitHub says a strict up-to-date rule blocks the merge
        # (mergeable_state=behind). A head merely behind a non-strict base merges as is, and on a
        # merge-queue repo fleet-merge's stale-base gate updates a far-behind head itself; pushing
        # here only re-ran CI (198 hermes-home handoff updates in 7 d to 2026-10-04).
        mstate = pr.get("mergeable_state")
        # t_6395b273: null/"unknown" = GitHub has not computed mergeability yet. A far-behind
        # head may still be blocked by a strict rule, so neither update nor arm the stale SHA.
        if behind > behind_max and mstate in (None, "", "unknown"):
            entry["deferred"] = f"mergeable_state={mstate!r} not computed; not updated or armed"
            continue
        if behind > behind_max and mstate == "behind":
            upd = gh("-X", "PUT", f"repos/{ref.repo}/pulls/{ref.number}/update-branch",
                     "-f", f"expected_head_sha={head}")
            entry["update_branch"] = "requested" if upd is not None else "failed (fail-open)"
            # The new head has fresh CI; arming is fleet-merge's job once it is green.
            continue
        if entry.get("draft_flip"):
            # Just un-drafted: the head's only check-runs are the draft-skipped ones (`skipped`
            # counts green), so arming now would land on CI that never ran. CI + Prism start on
            # the ready event; the lander arms it from there.
            if allow_arm:
                entry["automerge"] = "not armed: flipped ready at handback, CI starts now"
        elif allow_arm and armable_keys is not None and key.lower() not in armable_keys:
            entry["automerge"] = "not armed: PR only mentioned, not this card's handoff PR"
        elif (allow_arm and automerge_enabled() and _owns_draft(ref, pr, task_id)
              and _no_post_ready_ci(gh, ref.repo, head)):
            # Retry safety (Prism 982040536d15): a flip that persisted on GitHub before a later
            # flip failed leaves no draft_flip marker on the retry. GitHub is the state: the
            # worker's own PR whose head shows only draft-skipped check-runs has had no
            # post-ready CI yet, so it is not armed until a real run appears.
            entry["automerge"] = "not armed: own worker PR, no post-ready CI yet (only skipped runs)"
        elif allow_arm and automerge_enabled():
            ci = _ci_report(gh, ref.repo, head)
            if not ci:
                log = arm(ref.repo, ref.number, head, task_id)
                entry["automerge"] = f"fleet-merge spawned ({log})" if log else "not spawned"
            elif ci != "check-runs unreadable" and merge_gate_profile(ref.repo) == "v2":
                # t_7dbacbbc (pipeline v2 D1): CI gates deploy, not merge. Not green is REPORTED into the
                # fleet-merge --reason, never a reason to skip the arm. v1: unchanged ("ci not green").
                entry["ci_reported"] = ci
                log = arm(ref.repo, ref.number, head, task_id, ci)
                entry["automerge"] = (f"fleet-merge spawned, CI reported not green ({log})" if log
                                      else "not spawned")
            else:
                entry["automerge"] = "ci not green; not armed"
    return report
