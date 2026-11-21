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
    Operator hold (t_62389c4f): a PR this card already flipped ready that is draft again at the
    recorded head is left draft, not refused, not armed; ``report["draft_held"]`` says why.
    Each flip is recorded as ``{head, at}`` (``at`` = epoch taken BEFORE the
    flip call) in ``report["draft_flipped_at"]`` / ``DraftPrError.flipped``; the
    caller persists it and passes it back as ``flipped=`` on every later
    handoff. A flipped PR is armed only on POSITIVE post-ready evidence: one
    check-runs snapshot (the same one (c) grades) holding a non-skipped run
    whose ``started_at`` is after ``at`` (Prism d34048d53f3c).
(b) STALE -> update. A head more than :data:`STALE_BEHIND_MAX` (20, the same
    number as fleet-merge.sh ``FLEET_MERGE_STALE_BASE_MAX``) commits behind its
    base AND reported ``mergeable_state=behind`` (a strict up-to-date rule blocks
    the merge, t_39a33e70) is updated with ``PUT .../update-branch`` bound to the
    head SHA we measured (``expected_head_sha``). CI re-runs on the new head.
    Merge-queue repos are left to fleet-merge's own stale-base gate. A far-behind
    head whose ``mergeable_state`` is not computed yet (null/``unknown``) is
    deferred: neither updated nor armed (t_6395b273).
(c) GREEN -> hand to the evaluator. A fresh, non-draft PR on a non-milestone card records
    what it waits on (``waiting:fast-tier`` when CI is not green, ``waiting:prism`` when it is)
    and, while ``event_land`` is on (hermes-home ``config/landing-policy.json``, kill file
    ``state/event-land.disabled``), spawns ``land_evaluator.py evaluate --event E6`` (Forge L0
    EV-A, t_d343020c). The handoff never arms a lander: the old ``fleet-merge.sh`` arm was retired
    (it answered rc 61) and removed (retired-lander deletion plan, PR A). Event-land off = waiting
    recorded, nothing spawned.

Fail-OPEN everywhere except (a): a GitHub lookup error, an update-branch
failure or a missing evaluator is recorded and the handoff proceeds —
the pre-existing behavior. A draft is only refused on a positive ``draft: true``
read, with one exception (t_5c6c67c5): the PR lookup is retried once, and the card's
own handoff PR in a ``config/worker-pr-draft.json`` repo that is still unreadable (or no
GitHub client at all) raises :class:`DraftLookupError` (it may be a draft). Kill switches: ``KANBAN_HANDOFF_FRESHNESS=0`` disables the whole gate,
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

    def __init__(self, task_id: str, prs: list, flipped: Optional[dict] = None):
        self.task_id = task_id
        self.prs = list(prs)
        # PRs this call DID flip ready before another failed: {key: {"head", "at"}}; persist them.
        self.flipped = dict(flipped or {})
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


class DraftLookupError(DraftPrError):
    """The card's own PR in a worker-draft repo could not be read, twice (t_5c6c67c5).

    Those repos open worker PRs as drafts, so an unreadable PR there may be a draft; passing it
    through fail-open sent hermes-home#8231 to review still draft. Subclass of
    :class:`DraftPrError` so every caller's draft refusal handling applies unchanged."""

    def __init__(self, task_id: str, prs: list, flipped: Optional[dict] = None):
        ValueError.__init__(
            self,
            f"handoff refused: GitHub lookup of {', '.join(prs)} failed twice, so the gate cannot "
            f"tell whether {'it is' if len(prs) == 1 else 'they are'} still a worker DRAFT. Retry "
            f"the handoff; if the PR is INTENTIONALLY left draft, complete with "
            f"--draft-ok/draft_ok \"<reason>\" (audited). {task_id} is still in-flight (no state change).",
        )
        self.task_id = task_id
        self.prs = list(prs)
        self.flipped = dict(flipped or {})


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


def _check_runs(gh: GhFn, repo: str, sha: str):
    """One check-runs snapshot for ``sha``: the list, or None when unreadable."""
    runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    items = (runs or {}).get("check_runs")
    if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
        return None
    return items


_UNREAD = object()


def _ci_report(gh: GhFn, repo: str, sha: str, items=_UNREAD) -> str:
    """'' when the head is CI-green, else a one-line description of why not (red/pending/none/unreadable).

    ``items``: a check-runs snapshot already read by the caller (None = unreadable), so the arm decision
    and the green grade come from the SAME read (Prism d34048d53f3c)."""
    if items is _UNREAD:
        items = _check_runs(gh, repo, sha)
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


def _iso_epoch(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _post_ready_ci(items: list, flipped_at: Optional[float]) -> bool:
    """POSITIVE evidence that CI ran after the PR left draft, from one check-runs snapshot.

    With a recorded flip time: at least one run that is not ``skipped`` and whose ``started_at``
    is after ``flipped_at`` (a pre-ready lint success does not count, Prism d34048d53f3c). With no
    record (an own PR un-drafted outside this gate): at least one non-skipped run."""
    for r in items:
        if r.get("conclusion") == "skipped":
            continue
        if flipped_at is None:
            return True
        started = _iso_epoch(r.get("started_at"))
        if started is not None and started > flipped_at:
            return True
    return False


def _ci_green(gh: GhFn, repo: str, sha: str) -> bool:
    return _ci_report(gh, repo, sha) == ""


def event_land_enabled(root: Optional[Path] = None) -> bool:
    """Forge L0 EV kill switch, read exactly as hermes-home ``scripts/lib/land_evaluator.py enabled()`` reads it.

    True only when ``<hermes root>/config/landing-policy.json`` has ``"event_land": true`` AND
    ``<root>/state/event-land.disabled`` is absent. Missing/unreadable config = off (I1): off, the
    handoff records what the PR waits on and spawns nothing."""
    try:
        if root is None:
            from hermes_constants import get_default_hermes_root
            root = get_default_hermes_root()
        with open(Path(root) / "config" / "landing-policy.json", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(cfg, dict) or cfg.get("event_land") is not True:
        return False
    return not (Path(root) / "state" / "event-land.disabled").exists()


def spawn_evaluate(repo: str, number: int, sha: str, task_id: str) -> Optional[str]:
    """E6 (card handoff with an open PR): ``land_evaluator.py evaluate --source event --event E6``, detached.

    Runs while event_land is on (Forge L0 EV-A, spec 2.2.2 E6): the evaluator, not the handoff,
    classifies the PR. Returns the log
    path, or None when the evaluator is missing or the spawn failed (fail-open: E1-E3 and the sweep
    re-evaluate the PR)."""
    from hermes_constants import get_default_hermes_root
    root = get_default_hermes_root()
    script = root / "scripts" / "lib" / "land_evaluator.py"
    if not script.is_file():
        return None
    log_dir = root / "logs" / "handoff-automerge"
    log_path = log_dir / f"{task_id}-{repo.replace('/', '_')}-{number}.log"
    argv = [sys.executable or "python3", str(script), "evaluate", "--repo", repo, "--pr", str(number),
            "--source", "event", "--event", "E6", "--head", sha]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as fh:
            subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        _log.warning("handoff freshness: land_evaluator spawn failed: %s", exc)
        return None
    return str(log_path)


ArmFn = Callable[..., Optional[str]]   # (repo, number, sha, task_id) -> log path
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


def handoff_refs(metadata=None, survivor_pr=None) -> list:
    """The card's OWN handoff PR refs: ``metadata.pr_url``/``pr_urls``/``pr``/``own_prs`` or
    ``--survivor-pr``. A PR the summary/result prose merely mentions is context, never the card's
    (FleetReview #1234). One filter for arming (complete_task) and the negative-result flip note."""
    from hermes_cli.kanban_open_pr import extract_pr_refs
    meta = metadata if isinstance(metadata, dict) else None
    return extract_pr_refs(metadata=meta, survivor_pr=survivor_pr)


def _owns_draft(ref, pr: dict, task_id: str) -> bool:
    """The worker's own draft: head branch names THIS card and the repo is a worker-draft repo."""
    from hermes_cli.kanban_open_pr import branch_names_card
    head_ref = str(((pr.get("head") or {}).get("ref")) or "")
    return branch_names_card(head_ref, task_id) and ref.repo.casefold() in worker_draft_repos()


def _operator_hold(record, pr: dict) -> str:
    """Why a draft is an operator hold (t_62389c4f), or '' when it is not.

    ``record`` is this card's own persisted flip (``handoff_draft_flipped``, written only by the kernel). A PR this
    card already flipped ready that is draft again at the SAME head was re-drafted by someone else: the worker's own
    re-draft (worker-pr-redraft.py) runs only right before a push, so it always comes with a new head. GitHub's
    timeline cannot say who: an operator's ``gh pr ready --undo`` and the worker's flip both act as
    ang-fleet-workers[bot] (hermes-home#8702, 2026-11-04)."""
    head = str(((pr.get("head") or {}).get("sha")) or "")
    if not isinstance(record, dict) or not head or str(record.get("head") or "") != head:
        return ""
    return (f"left draft as-is: re-drafted after this card flipped it ready at head {head[:12]} (no push since), "
            f"so someone else holds it; not flipped, not armed")


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


LOOKUP_RETRY_DELAY_S = 2.0


def _lookup_pr(gh: GhFn, ref) -> Optional[dict]:
    """``pulls/N`` for ``ref``, retried ONCE after :data:`LOOKUP_RETRY_DELAY_S` (t_5c6c67c5). None = unreadable."""
    path = f"repos/{ref.repo}/pulls/{ref.number}"
    pr = gh(path)
    if isinstance(pr, dict):
        return pr
    time.sleep(LOOKUP_RETRY_DELAY_S)
    pr = gh(path)
    return pr if isinstance(pr, dict) else None


def _unread_refuses(ref, key: str, armable_keys) -> bool:
    """An unreadable PR refuses (instead of failing open) only where a worker PR may be a draft: the card's own
    handoff PR (``armable_keys``; None = every ref) in a ``config/worker-pr-draft.json`` repo (t_5c6c67c5)."""
    # Not gated on worker_draft_flip_enabled(): that switch decides only whether we flip, never
    # whether the draft gate opens (Prism 6632d57be41c).
    if ref.repo.casefold() not in worker_draft_repos():
        return False
    return armable_keys is None or key.lower() in armable_keys


def check(refs, *, task_id: str, allow_arm: bool, gh: Optional[GhFn] = None,
          behind_max: int = STALE_BEHIND_MAX,
          armable=None, draft_ok: bool = False, ready: Optional[ReadyFn] = None,
          flipped: Optional[dict] = None, evaluate: Optional[ArmFn] = None,
          event_land: Optional[bool] = None, intent=None) -> dict:
    """Apply (a)/(b)/(c) to OPEN PR ``refs``. Raises :class:`DraftPrError`.

    ``armable`` is the set of ``"o/r#n"`` keys this card actually handed off
    (``metadata.pr_url``/``pr``/``pr_urls`` or ``--survivor-pr``). Only those
    are ever handed to the evaluator: a PR merely MENTIONED in summary/result prose is
    context, not merge authorization. ``complete_task`` always passes it;
    ``None`` (direct callers) keeps the legacy every-ref behavior.

    Returns a report ``{"prs": {"o/r#n": {...}}}`` for the handoff metadata.
    All draft checks run before any mutation, so a refused handoff has changed
    nothing on GitHub. With ``draft_ok`` drafts are not refused: they are listed
    in ``report["draft_override"]`` and skipped by (b)/(c) (the caller audits).

    ``flipped``: persisted flip records ``{"o/r#n": {"head": sha, "at": epoch}}`` from earlier
    handoffs of this card (t_c1ee61ae); a recorded PR is armed only on post-flip CI evidence.

    (c) records ``entry["waiting"]`` = ``waiting:fast-tier`` (CI not green / not started) or
    ``waiting:prism`` (CI green; the evaluator reads tier, Prism and review). While
    :func:`event_land_enabled` (``event_land`` None = read it) it calls ``evaluate`` (default
    :func:`spawn_evaluate`, E6); off, nothing is spawned (the retired fleet-merge arm is gone).

    ``intent`` (t_deee5435): ``(repo, number) -> status`` from :func:`kanban_pr_intent.applier`. It writes the
    card's Intent block into each OWN PR body (:func:`_owns_draft`, draft or not) after every refusal check and
    BEFORE the ready flip, so Prism starts on a body that carries the card's ask. Its status lands in
    ``entry["intent"]``; it never raises or refuses.
    """
    report: dict = {"prs": {}, "checked_at": int(time.time())}
    # Never update-branch or arm a third-party PR (t_06dccfe3): the caller
    # already passes fleet refs only; this keeps the actuator safe on its own.
    from hermes_cli.kanban_open_pr import is_fleet_ref
    refs = [ref for ref in (refs or []) if is_fleet_ref(ref)]
    if not refs or not enabled():
        return report
    armable_keys = None if armable is None else {str(k).lower() for k in armable}
    if gh is None:
        gh = _default_gh()
        if gh is None:
            # Prism 6a3d114dfec9: no GitHub client is an unreadable lookup, not a pass, for the
            # card's own PR in a worker-draft repo; every other ref still fails open.
            unread = [f"{r.repo}#{r.number}" for r in refs
                      if not draft_ok and _unread_refuses(r, f"{r.repo}#{r.number}", armable_keys)]
            if unread:
                raise DraftLookupError(task_id, unread)
            return report
    evaluate = evaluate or spawn_evaluate
    ev_on = event_land_enabled() if event_land is None else event_land
    flip_records = {str(k).lower(): v for k, v in (flipped or {}).items() if isinstance(v, dict)}

    views = []
    drafts = []
    unreadable = []
    own_drafts: dict = {}
    intent_targets = []
    for ref in refs:
        key = f"{ref.repo}#{ref.number}"
        pr = _lookup_pr(gh, ref)   # t_5c6c67c5: one retry before failing open
        entry: dict = {}
        report["prs"][key] = entry
        if intent is not None and isinstance(pr, dict) and _owns_draft(ref, pr, task_id):
            intent_targets.append((ref, entry))
        if not isinstance(pr, dict):
            if not draft_ok and _unread_refuses(ref, key, armable_keys):
                entry["lookup"] = "failed after retry (refused)"
                unreadable.append(key)
            else:
                entry["lookup"] = "failed (fail-open)"
            continue
        if pr.get("draft") is True:
            held = _operator_hold(flip_records.get(key.lower()), pr)
            if held:
                # t_62389c4f: re-drafted by someone else after this card flipped it; the hold sticks.
                entry["draft_held"] = held
                report.setdefault("draft_held", {})[key] = held
                _log.info("handoff freshness: %s %s", key, held)
                if not draft_ok:
                    entry["draft"] = True
                    continue
                # An explicit draft_ok is the audited override path, unchanged (Prism 400b19850a8f).
            drafts.append(key)
            entry["draft"] = True
            if worker_draft_flip_enabled() and _owns_draft(ref, pr, task_id):
                own_drafts[key] = (ref, key, pr, entry)
            continue
        views.append((ref, key, pr, entry))
    if unreadable:
        # t_5c6c67c5 (hermes-home#8231): an own PR in a worker-draft repo that GitHub would not return
        # twice may still be a draft; refuse before any mutation rather than pass it through.
        raise DraftLookupError(task_id, unreadable)
    if drafts and not draft_ok:
        # t_c1ee61ae: the worker's OWN draft (head branch names this card, repo listed in
        # config/worker-pr-draft.json) is flipped ready through ace_gate.py instead of refused.
        # A foreign draft still refuses, before any flip; a failed flip (review hold rc=21, Ace-gated
        # rc=20, read failure) refuses like before.
        own = {k for k in drafts if k in own_drafts}
        foreign = [k for k in drafts if k not in own]
        if foreign or not own:
            raise DraftPrError(task_id, drafts)
    # Every refusal is above: a refused handoff writes no PR body. The flip is below, so the body is
    # written while the PR is still a draft and Prism never reviews it without the Intent block.
    for ref, entry in intent_targets:
        try:
            entry["intent"] = intent(ref.repo, ref.number)
        except Exception as exc:  # noqa: BLE001 -- never blocks the handback
            entry["intent"] = f"error: {str(exc)[:160]}"
            _log.warning("handoff freshness: pr intent %s#%s failed: %s", ref.repo, ref.number, exc)
    if drafts and not draft_ok:
        ready = ready or _default_ready()
        failed = []
        flipped_now: dict = {}
        for ref, key, pr, entry in own_drafts.values():
            at = int(time.time())  # BEFORE the flip: every post-ready run starts after it
            rc = ready(ref.repo, ref.number) if ready is not None else None
            entry["draft_flip"] = f"ready via ace_gate.py (rc={rc})"
            if rc != 0:
                failed.append(key)
                continue
            entry["draft"] = False
            flipped_now[key] = {"head": str(((pr.get("head") or {}).get("sha")) or ""), "at": at}
            views.append((ref, key, dict(pr, draft=False), entry))
        if failed:
            raise DraftPrError(task_id, failed, flipped_now)
        report["draft_flipped"] = sorted(own)
        report["draft_flipped_at"] = flipped_now
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
            if ev_on and allow_arm:
                entry["waiting"] = "waiting:fast-tier"   # EV-A: E1 on the new head's CI evaluates it
            # The new head has fresh CI; E1 on it evaluates the PR.
            continue
        if entry.get("draft_flip"):
            # Just un-drafted: the head's only check-runs are the draft-skipped ones (`skipped`
            # counts green), so arming now would land on CI that never ran. CI + Prism start on
            # the ready event; the evaluator takes it from there.
            if allow_arm:
                entry["automerge"] = "not armed: flipped ready at handback, CI starts now"
                if ev_on:
                    entry["waiting"] = "waiting:fast-tier"   # EV-A: E1 on the ready CI evaluates it
        elif allow_arm and armable_keys is not None and key.lower() not in armable_keys:
            entry["automerge"] = "not armed: PR only mentioned, not this card's handoff PR"
        elif allow_arm and automerge_enabled():
            # ONE snapshot decides both "has CI run since ready" and "is it green" (Prism d34048d53f3c:
            # two reads let a transient None on the first bypass the barrier).
            items = _check_runs(gh, ref.repo, head)
            record = flip_records.get(key.lower())
            if record is not None or _owns_draft(ref, pr, task_id):
                # Retry safety (Prism 982040536d15, d34048d53f3c): a PR flipped ready by an earlier
                # handoff of this card is armed only on POSITIVE post-ready evidence. Unreadable is
                # not evidence; a pre-ready lint success beside skipped tests is not either.
                at = record.get("at") if record is not None else None
                at = float(at) if isinstance(at, (int, float)) else None
                if record is not None and at is None:
                    entry["automerge"] = "not armed: flip record has no time"
                    continue
                if items is None or not _post_ready_ci(items, at):
                    entry["automerge"] = ("not armed: own worker PR, no CI started after the ready flip yet"
                                          if items is not None else "not armed: check-runs unreadable")
                    continue
            ci = _ci_report(gh, ref.repo, head, items)
            # EV-A (spec 2.2.2 E6): the handoff records what the PR waits on and hands it to the
            # evaluator; it never asks a lander itself.
            entry["waiting"] = "waiting:fast-tier" if ci else "waiting:prism"
            if ev_on:
                log = evaluate(ref.repo, ref.number, head, task_id)
                entry["automerge"] = (f"event-land: evaluate E6 spawned ({log})" if log
                                      else "event-land: evaluate not spawned")
            else:
                entry["automerge"] = "event-land off: not armed (no automated lander)"
    return report
