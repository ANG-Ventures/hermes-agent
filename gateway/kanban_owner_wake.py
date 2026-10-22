"""Kanban events WAKE the owning operator session (t_1ae0c35b, Ace 2026-10-03 11:05).

Incident 2026-10-03: Phase 4 (t_e6b3713d) sat BLOCKED 04:14 -> 10:18 with its
fix merged and deployed. The lifecycle only POSTED "blocked" to the home chat;
the Apollo session that owned the card never got a turn, and acted only when
Ace typed "Proceed". The pager and the digest notify a HUMAN. This module gives
the OWNING SESSION a turn.

A card's owner is its home session (``tasks.session_id``). When that session is
live in THIS gateway's session store, its origin is a messaging chat, and this
gateway runs an operator profile (``kanban.wake_owner_profiles``, default
default/aegis), these events enqueue one handoff turn into it:

(a) ``blocked`` with ``kind=needs_input``;
(b) ``blocked`` whose reason names a time or precondition ("respawn me at…",
    "waiting on PR#…", "deploy: …", "after the next restart window");
(c) ``stalled`` / ``crashed`` / ``gave_up`` / ``timed_out``, or a ``reclaimed``
    with a stale heartbeat;
(d) ``completed`` of a card that was the LAST ``blocks`` parent of one or more
    open children;
(e) ``review_requested`` whose fleet PR is red (a failed check-run) or dirty; a red
    the fleet's main-red watcher proved is inherited from the base branch carries
    ``inherited-from-main <sha>`` (t_85639cbc), so the owner fixes main, not the PR;
(f) ``skipped_nonspawnable`` right after a ``schedule_elapsed`` (t_affb1951): the
    scheduled wake fired on a card whose assignee the dispatcher never spawns
    (apollo / human:*). The dispatcher writes that event once per wake, so this is
    one turn per wake, not per tick.

Not on: heartbeats, a review handback whose PR is green or pending (the merge
pass owns it), a ``completed`` with no gated children (the digest covers it),
or an event the owning session made itself.

Coalescing: one wake per card per :data:`COALESCE_SECONDS`; events that arrive
inside the window are held and listed in the next turn. All cards due for one
session in a tick go out as ONE turn. The turn is an internal
``MessageEvent(allow_gateway_control=False)`` pinned to the session's key and
id (the plugin-injection shape), so a busy session queues it like any inbound
message and it can never run a slash command.

Opt-outs: ``kanban.wake_owner_session: false`` (global) or a card body line
``wake: off``. A card homed to a worker, cron or CLI session (not in a
gateway's store, or no chat origin) gets no wake: today's Discord line only.

State (board cursors, pending events, last-wake times) lives in
``<HERMES_HOME>/gateway/kanban_owner_wake.json`` so a restart neither replays
history nor drops held events. A first run with no state starts
:data:`FIRST_RUN_LOOKBACK_SECONDS` back.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from gateway import kanban_notify_suppress as _ns
from gateway import kanban_wake_freshness as _fresh

logger = logging.getLogger(__name__)

COALESCE_SECONDS = 600.0
FIRST_RUN_LOOKBACK_SECONDS = 600
PENDING_MAX_AGE_SECONDS = 3600.0
ROW_RETRY_LIMIT = 10  # ~10 ticks: a row that keeps failing to classify is skipped, loudly
SCAN_LIMIT = 500
REASON_MAX = 1500
DEFAULT_OWNER_PROFILES = ("default", "aegis")
STATE_FILENAME = "kanban_owner_wake.json"

SCAN_KINDS = (
    "blocked", "stalled", "crashed", "gave_up", "timed_out", "reclaimed",
    "completed", "review_requested", "skipped_nonspawnable",
)
STUCK_KINDS = frozenset({"stalled", "crashed", "gave_up", "timed_out"})
# Kinds the per-subscription notify+wake path turns into a wake. The owner-wake
# defers to it only when that wake was DELIVERED into the owner's chat
# (mark_carried), never on the sub's existence: under host load the contention
# gate sends the sub's wake as a plain notify, and the block then reached no
# session (t_156cc147: t_e248dbe7's needs_input block, 2026-10-19 08:52, 1h40m).
SUB_WAKE_KINDS = frozenset({"blocked", "crashed", "gave_up", "timed_out"})
# (e) red handback: the notifier's wake carries the red-PR state itself and
# marks the event (mark_carried); only an uncarried one wakes from here.
# (d) children-ready rides the notifier's ``completed`` wake the same way.
CARRIED_KINDS = frozenset({"review_requested", "completed"}) | SUB_WAKE_KINDS

TRIGGER_NEEDS_INPUT = "needs_input"
TRIGGER_PRECONDITION = "blocked_precondition"
TRIGGER_STUCK = "stuck"
TRIGGER_LAST_BLOCKER = "last_blocker_done"
TRIGGER_RED_HANDBACK = "red_handback"
TRIGGER_WOKE_NONSPAWNABLE = "woke_nonspawnable"

_WAKE_OFF_RE = re.compile(r"^\s*wake\s*:\s*off\s*$", re.I | re.M)
# A block reason that names WHEN or ON WHAT the card can move again.
_PRECONDITION_RE = re.compile(
    r"respawn me|re-?spawn (at|after|when)|waiting (on|for)|\bwait(s)? (on|for)\b"
    r"|\bdeploy\s*:|unblock (me )?(after|when|once)|\bat or after\b|\buntil\b"
    r"|\bonce\b|after the next|\bnext (gateway )?restart\b|restart window"
    r"|\bPR\s*#?\d+|\b[\w.-]+/[\w.-]+#\d+|\b\d{1,2}:\d{2}\s*(PT|PDT|PST|UTC|Z)\b",
    re.I,
)
_NON_CHAT_PLATFORMS = frozenset({"local", "cli", "tui", "api_server", "webhook", "cron"})
_RED_CONCLUSIONS = frozenset({
    "failure", "timed_out", "cancelled", "action_required", "startup_failure",
})
_NO_VERDICT_CONCLUSIONS = frozenset({"cancelled", "skipped"})

PrHealthFn = Callable[[str, int], Optional[dict]]


class PrHealthUnknown(RuntimeError):
    """A fleet PR's health could not be read (timeout, rate limit). The event is
    retried on later ticks, not consumed as a green handback (Prism P1)."""


# --- pure classification ----------------------------------------------------


def wake_off(body: Optional[str]) -> bool:
    return bool(body) and bool(_WAKE_OFF_RE.search(body or ""))


def names_precondition(reason: Optional[str]) -> bool:
    return bool(reason) and bool(_PRECONDITION_RE.search(reason or ""))


def classify_block(payload: Optional[dict]) -> Optional[str]:
    p = payload or {}
    if str(p.get("kind") or "") == "needs_input":
        return TRIGGER_NEEDS_INPUT
    if names_precondition(str(p.get("reason") or "")):
        return TRIGGER_PRECONDITION
    return None


def is_stuck(kind: str, payload: Optional[dict]) -> bool:
    if kind in STUCK_KINDS:
        return True
    return kind == "reclaimed" and bool((payload or {}).get("heartbeat_stale"))


# hermes-agent's review-labels.yml: a CI-sensitive diff is red on this gate (and the aggregator it fails) until
# a maintainer applies ``ci-reviewed``. On a review handback that is the review being asked for, not a PR that
# cannot land: the merge pass owns it (HELD-NEEDS-LABEL, or fleet-merge applies the label on an operator APPROVE).
_LABEL_GATE_RE = re.compile(r"^(?:.+ / )?Review label gate$")
_REQUIRED_ROLLUP_RE = re.compile(r"^(?:.+ / )?All required checks pass$")
REVIEW_LABEL = "ci-reviewed"


# scripts/ci/evaluate_needs.py prints ``::error::N job(s) failed: a, b`` on the aggregator run.
_ROLLUP_FAILED_RE = re.compile(r"^\d+ job\(s\) failed: (.+)$")
_LABEL_JOB = "review-labels"
# review-labels.yml "Fail on missing label": the ONE annotation that means the label is what is missing. A failed
# checkout / emit_review_status.py / timeout / cancel carries no such line and stays a real red (Prism P1 b796fe55bbc1).
_MISSING_LABEL_MSG = "CI-sensitive changes require the ci-reviewed label."


def rollup_failed_jobs(annotations: Optional[list]) -> Optional[frozenset]:
    """The jobs the aggregator run says failed, from its ``N job(s) failed: ...`` annotation, or None (unreadable)."""
    for a in annotations or []:
        m = _ROLLUP_FAILED_RE.match(str((a or {}).get("message") or "").strip())
        if m:
            return frozenset(x.strip() for x in m.group(1).split(",") if x.strip())
    return None


def missing_label_only(annotations: Optional[list]) -> bool:
    """True when a failed review-label gate run carries the workflow's explicit missing-label failure."""
    return any(_MISSING_LABEL_MSG in str((a or {}).get("message") or "") for a in annotations or [])


def _label_gate_red_only(run: dict, annotations: Optional[list]) -> bool:
    """One failing check run's verdict: is it red ONLY because ``ci-reviewed`` is missing?"""
    name, concl = str(run.get("name") or ""), str(run.get("conclusion") or "").lower()
    if concl != "failure":   # cancelled / timed_out / action_required: never evidence of a missing label
        return False
    if _LABEL_GATE_RE.match(name):
        return missing_label_only(annotations)
    if _REQUIRED_ROLLUP_RE.match(name):
        return rollup_failed_jobs(annotations) == frozenset({_LABEL_JOB})
    return False


def label_pending(health: Optional[dict]) -> bool:
    """True when the PR has no ``ci-reviewed`` label, a review-label gate is among the reds, and EVERY failing check
    run was verified (``label_only``, one bool per run of ``failing``) to be red only for the missing label: each gate
    run carries the workflow's missing-label annotation and each aggregator run says it failed on ``review-labels``
    alone (Prism P1 c585adb654b0, 7d6845520733, b796fe55bbc1). Unreadable annotations count as not verified."""
    health = health or {}
    names = [str(n) for n in health.get("failing") or [] if n]
    verdicts = list(health.get("label_only") or [])
    if REVIEW_LABEL in [str(x).strip().lower() for x in health.get("labels") or []]:
        return False
    if not names or len(verdicts) != len(names) or not any(_LABEL_GATE_RE.match(n) for n in names):
        return False
    return all(v is True for v in verdicts)


# t_85639cbc: the fleet's main-red watcher (hermes-home scripts/main-red-summary.py, lib main_red_gate.py) writes
# <fleet root>/state/main-red-inherited.json every 5 min while the base branch is red on a gate lint: per open PR
# head, the failed (workflow|job|step) keys it shares with main (`inherited`) and its own (`own`). Same predicate as
# hermes-home main_red_gate.pr_red_kind, which the merge pass and the overview's "Blocked by main" read.
MAIN_RED_STATE = "main-red-inherited.json"
MAIN_RED_FRESH_S = 1800


def main_red_inherited(state: Optional[dict], repo: str, number: int, head: str, failing: list,
                       now: Optional[float] = None, failing_ids: Optional[list] = None) -> Optional[str]:
    """The base-branch sha this PR's red is inherited from, or None. Inherited only when the watcher's state is
    fresh, names this exact head, the PR has NO red step of its own, every failing check is a job main fails, AND
    every failing check-run id is one the watcher judged (``red_ids``): a check re-run on the same head after the
    snapshot can fail a PR-specific step under the same job name, and is never labelled inherited."""
    now = time.time() if now is None else now
    if not isinstance(state, dict) or not state.get("open") or str(state.get("repo") or "").lower() != repo.lower():
        return None
    try:
        if now - float(state.get("as_of") or 0) > MAIN_RED_FRESH_S:
            return None
    except (TypeError, ValueError):
        return None
    v = (state.get("prs") or {}).get(str(number))
    if not isinstance(v, dict) or not head or v.get("head") != head or not v.get("done") or v.get("own"):
        return None
    jobs = {str(k).split("|")[1] for k in v.get("inherited") or [] if str(k).count("|") >= 2}
    if not jobs or not failing or not set(failing) <= jobs:
        return None
    try:
        judged = {int(i) for i in v.get("red_ids") or []}
        ids = [int(i) for i in failing_ids or []]
    except (TypeError, ValueError):
        return None
    if not ids or len(ids) != len(failing) or not set(ids) <= judged:
        return None
    return str((state.get("episode") or {}).get("sha") or "") or None


def _read_main_red_state() -> Optional[dict]:
    from hermes_constants import get_default_hermes_root

    try:
        return json.loads((get_default_hermes_root() / "state" / MAIN_RED_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pr_is_red_or_dirty(health: Optional[dict]) -> Optional[str]:
    """``"red: a, b"`` / ``"dirty"`` / ``"red: …; dirty"`` or None (green, pending, label pending, unknown).
    An inherited red reads ``"red (inherited-from-main <sha>: fix main, not this PR): a, b"``."""
    if not health or str(health.get("state") or "").lower() != "open":
        return None
    parts = []
    names = [] if label_pending(health) else list(health.get("failing") or [])
    urls = list(health.get("failing_urls") or [])
    failing = [f"{n} <{u}>" if u else str(n)
               for n, u in zip(names, urls + [""] * (len(names) - len(urls))) if n]
    if failing:
        inh = health.get("inherited_from_main")
        head = f"red (inherited-from-main {str(inh)[:9]}: fix main, not this PR; clears when main is green): " \
            if inh else "red: "
        parts.append(head + ", ".join(failing[:6]))
    if str(health.get("mergeable_state") or "").lower() == "dirty":
        parts.append("dirty (merge conflict)")
    return "; ".join(parts) or None


def _clip(text: Any, limit: int = REASON_MAX) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def describe(item: dict) -> str:
    """One sentence for one triggering event (reason verbatim)."""
    trig = item["trigger"]
    reason = item.get("reason") or ""
    if trig == TRIGGER_NEEDS_INPUT:
        out = "blocked, needs_input"
    elif trig == TRIGGER_PRECONDITION:
        out = "blocked on a precondition"
    elif trig == TRIGGER_STUCK:
        out = f"stuck ({item.get('kind')})"
    elif trig == TRIGGER_LAST_BLOCKER:
        kids = ", ".join(f"{c['id']} [{c['status']}]" for c in item.get("children") or [])
        out = (f"done, and it was the last blocker of {kids}; those children are now "
               "unblocked: arm or dispatch them")
    elif trig == TRIGGER_WOKE_NONSPAWNABLE:
        return (f"scheduled wake elapsed; card is assigned to {item.get('assignee') or '?'} and cannot "
                "auto-spawn — decide: reassign to a worker or do it")
    elif trig == TRIGGER_RED_HANDBACK:
        out = f"handed back for review but the PR cannot land ({item.get('pr_state')})"
    else:
        out = str(item.get("kind"))
    if reason:
        out += f'. Reason, verbatim: "{reason}"'
    return out


def render_prompt(cards: list[dict]) -> str:
    """One paragraph per card. Not a question to Ace."""
    paras = []
    for card in cards:
        events = "; then ".join(describe(i) for i in card["items"])
        paras.append(
            f"[kanban wake] Card {card['task_id']} \"{_clip(card.get('title'), 160)}\" "
            f"(board {card.get('board') or 'default'}), a card this session owns: {events}. "
            "Resolve it and get the chain back on track; reply in the home chat with what you did."
        )
    return "\n\n".join(paras)


# --- PR health (REST only; GraphQL is the shared per-user bucket) ------------


def _gh_json(path: str) -> Optional[Any]:
    try:
        proc = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except ValueError:
        return None


def _gh_pages(path: str, key: str, max_pages: int = 10) -> Optional[list]:
    """Every ``key`` row across REST pages of ``path`` (per_page=100), or None when
    any page is unreadable or the page cap is hit (a partial list is not a verdict).
    A head with >100 runs spills onto page 2+ (Prism P1)."""
    rows: list = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        data = _gh_json(f"{path}{sep}per_page=100&page={page}")
        if not isinstance(data, dict):
            return None
        batch = data.get(key) or []
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    return None


def red_check_names(runs: list, workflows: Optional[dict] = None,
                    superseded: frozenset = frozenset()) -> list[str]:
    """Names of the checks that grade the head red; see red_check_runs()."""
    return [str(r.get("name") or "?") for r in red_check_runs(runs, workflows, superseded)]


def red_check_runs(runs: list, workflows: Optional[dict] = None,
                   superseded: frozenset = frozenset()) -> list[dict]:
    """The check-run rows that grade the head red (t_65e5d76f, t_fb481152); the
    newest judging run per check, so its ``html_url`` is the one to open (t_121bd42e).

    A head carries every run ever started on it: override_lint runs twice per
    push and concurrency cancels the superseded one, so counting any red run
    read green PRs red. Per key = (workflow, name) the newest run (highest id)
    that is not cancelled/skipped decides. A key whose runs are ALL cancelled
    stays red unless the same check also has a newer skipped/neutral run (a
    re-trigger that skipped the job is not a cancelled check). A failure is
    only superseded by a later run of the SAME check.
    ``workflows`` maps check_suite id -> (workflow id, event); without an entry
    the key is the suite, so a duplicate in another suite is never merged
    (fails closed). ``superseded`` holds check_suite ids of CANCELLED workflow
    runs that a newer run of the same (workflow, event) replaced: every row in
    them (its cancelled jobs and the failure an ``if: always()`` aggregate
    wrote while cancelling) is dropped; the newer run decides, and while it is
    still running the check is pending, not red. Mirrors hermes-home
    kanban-review-merge-pass effective_checks().
    """
    workflows = workflows or {}
    live: dict = {}
    cancelled: dict = {}
    passed_over: dict = {}   # key -> newest id of a skipped/neutral run
    for i, run in enumerate(runs or []):
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "?")
        suite = (run.get("check_suite") or {}).get("id")
        if suite is not None and suite in superseded:
            continue
        if suite in workflows:
            key = ("wf", workflows[suite], name)
        elif suite is not None:
            key = ("suite", suite, name)
        else:
            key = ("row", i, name)
        concl = str(run.get("conclusion") or "").lower()
        rid = int(run.get("id") or 0)
        if concl in _NO_VERDICT_CONCLUSIONS:
            if concl == "cancelled":
                prev = cancelled.get(key)
                if prev is None or rid >= prev[0]:
                    cancelled[key] = (rid, run)
            else:
                passed_over[key] = max(rid, passed_over.get(key, 0))
            continue
        rank = (rid, i)
        if key not in live or rank > live[key][0]:
            live[key] = (rank, run, concl)
    red = [run for _rank, run, concl in live.values() if concl in _RED_CONCLUSIONS]
    return red + [run for key, (rid, run) in cancelled.items()
                  if key not in live and passed_over.get(key, -1) <= rid]


def _needs_workflow_map(runs: list) -> bool:
    """Merging suites into workflows only changes the verdict when a name with a
    non-pass row spreads over >1 suite, or a cancelled row sits on a head with >1
    suite (a cancelled workflow run a re-trigger may have superseded); skip the
    REST read otherwise."""
    suites: dict = {}
    unsettled = set()
    any_cancelled = False
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        suites.setdefault(name, set()).add((run.get("check_suite") or {}).get("id"))
        concl = str(run.get("conclusion") or "").lower()
        if concl not in ("success", "skipped", "neutral"):
            unsettled.add(name)
        any_cancelled = any_cancelled or concl == "cancelled"
    if any(len(v) > 1 and k in unsettled for k, v in suites.items()):
        return True
    return any_cancelled and len(set().union(*suites.values())) > 1


def _superseded_suites(rows: list) -> frozenset:
    """check_suite ids of CANCELLED workflow runs with a newer run of the same
    (workflow, event) on the head: GitHub cancels the first of two near-identical
    triggers (t_fb481152: #1718 head 94ad2960, Docker/CI/override-lint/sast/
    secret-scan all cancelled at 08:19:23 and re-run at 08:19:24)."""
    newest: dict = {}
    for r in rows:
        wk = (r.get("workflow_id"), r.get("event"))
        newest[wk] = max(newest.get(wk, 0), int(r.get("id") or 0))
    return frozenset(
        r["check_suite_id"] for r in rows
        if str(r.get("conclusion") or "").lower() == "cancelled"
        and r.get("workflow_id") is not None and r.get("id") is not None
        and int(r["id"]) < newest[(r.get("workflow_id"), r.get("event"))])


def _workflow_runs(repo: str, sha: str) -> tuple[dict, frozenset]:
    """(check_suite id -> (workflow id, event), superseded suite ids); ({}, {})
    when unreadable. A push run and a pull_request run of one workflow on one sha
    are separate checks, not re-runs."""
    rows = [r for r in (_gh_pages(f"repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs") or [])
            if isinstance(r, dict) and r.get("check_suite_id") is not None]
    wf = {r["check_suite_id"]: (r.get("workflow_id"), r.get("event")) for r in rows}
    return wf, _superseded_suites(rows)


def query_pr_health(repo: str, number: int) -> Optional[dict]:
    """``{state, mergeable_state, failing: [check names]}`` or None (unknown)."""
    pr = _gh_json(f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict) or not pr.get("state"):
        return None
    out = {"state": "merged" if pr.get("merged_at") else str(pr["state"]),
           "mergeable_state": str(pr.get("mergeable_state") or ""), "failing": [],
           "labels": [str((x or {}).get("name") or "") for x in pr.get("labels") or [] if isinstance(x, dict)]}
    sha = (pr.get("head") or {}).get("sha")
    out["head"] = str(sha or "")
    if out["state"] == "open" and sha:
        runs = _gh_pages(f"repos/{repo}/commits/{sha}/check-runs", "check_runs")
        if runs is None:
            return None  # unknown: retried, never read as green (Prism P1)
        wf, superseded = _workflow_runs(repo, sha) if _needs_workflow_map(runs) else ({}, frozenset())
        red = red_check_runs(runs, wf, superseded)
        out["failing"] = [str(r.get("name") or "?") for r in red]
        # the run each verdict came from, so the wake is verifiable in one click (t_121bd42e)
        out["failing_urls"] = [str(r.get("html_url") or "") for r in red]
        if red:
            out["inherited_from_main"] = main_red_inherited(_read_main_red_state(), repo, number, sha, out["failing"],
                                                            failing_ids=[r.get("id") for r in red])
        if red and REVIEW_LABEL not in [x.strip().lower() for x in out["labels"]] and any(_LABEL_GATE_RE.match(n) for n in out["failing"]) \
                and all(_LABEL_GATE_RE.match(n) or _REQUIRED_ROLLUP_RE.match(n) for n in out["failing"]):
            # only when label_pending could hold: verify EVERY failing run's own annotations (unreadable -> False)
            out["label_only"] = [_label_gate_red_only(r, _run_annotations(repo, r)) for r in red]
    return out


def _run_annotations(repo: str, run: dict) -> Optional[list]:
    try:
        rid = int(run.get("id"))
    except (TypeError, ValueError):
        return None
    data = _gh_json(f"repos/{repo}/check-runs/{rid}/annotations?per_page=100")
    return data if isinstance(data, list) else None


def default_pr_health() -> Optional[PrHealthFn]:
    # Same seam as kanban_open_pr._default_query: no live GitHub from pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return query_pr_health


def red_key(task_id: str, ref: str, health: dict) -> str:
    """S5 dedupe key of one bad PR on a handback: (card, PR, head, red-set, dirty).
    The same key twice is the same news twice (t_1ac4c83b: four turns, one red)."""
    names = [] if label_pending(health) else sorted(str(n) for n in health.get("failing") or [] if n)
    dirty = "dirty" if str(health.get("mergeable_state") or "").lower() == "dirty" else ""
    return f"{task_id}|{ref}@{health.get('head') or ''}|{','.join(names)}|{dirty}"


def handback_pr_verdict(kb: Any, conn: Any, task: dict, payload: Optional[dict],
                        run_id: Optional[int], pr_health: Optional[PrHealthFn],
                        memo: dict) -> dict:
    """``{"state": str|None, "keys": [red_key...], "inherited_only": bool}`` for a review handback.
    ``inherited_only``: every bad PR is red ONLY on checks main fails at the same head (no own red,
    not dirty), so the owner has nothing to do on the PR (S5). Raises :class:`PrHealthUnknown`."""
    out = {"state": None, "keys": [], "inherited_only": False}
    if pr_health is None:
        return out
    from hermes_cli import kanban_open_pr as _op

    meta = None
    if run_id is not None:
        try:
            run = kb.get_run(conn, int(run_id))
            meta = getattr(run, "metadata", None) if run else None
        except Exception:
            meta = None
    refs = _op.extract_pr_refs((payload or {}).get("summary"), task.get("result"),
                               metadata=meta if isinstance(meta, dict) else None)
    states, inherited = [], []
    for ref in refs:
        if not _op.is_fleet_ref(ref):
            continue
        key = (ref.repo.lower(), int(ref.number))
        if key not in memo:
            try:
                memo[key] = pr_health(ref.repo, int(ref.number))
            except Exception:
                memo[key] = None
        if memo[key] is None:
            raise PrHealthUnknown(f"{ref.repo}#{ref.number}")
        health = memo[key]
        bad = pr_is_red_or_dirty(health)
        if bad:
            label = f"{ref.repo}#{ref.number}"
            states.append(f"{label} {bad}")
            out["keys"].append(red_key(str(task.get("id") or ""), label, health))
            inherited.append(bool(health.get("inherited_from_main"))
                             and str(health.get("mergeable_state") or "").lower() != "dirty")
    out["state"] = "; ".join(states) or None
    out["inherited_only"] = bool(inherited) and all(inherited)
    return out


def handback_pr_state(kb: Any, conn: Any, task: dict, payload: Optional[dict],
                      run_id: Optional[int], pr_health: Optional[PrHealthFn],
                      memo: dict) -> Optional[str]:
    return handback_pr_verdict(kb, conn, task, payload, run_id, pr_health, memo)["state"]


def gated_ready_children(kb: Any, conn: Any, task_id: str) -> list[dict]:
    """Open children whose every gating parent is now done/archived."""
    out = []
    for child_id, kind in kb.child_links(conn, task_id):
        if kind not in kb.GATING_LINK_KINDS:
            continue
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (child_id,)).fetchone()
        if row is None or row["status"] in ("done", "archived", "running", "review"):
            continue
        if not kb._parents_satisfied(conn, child_id):  # noqa: SLF001 -- the dispatcher's own gate
            continue
        out.append({"id": child_id, "status": row["status"]})
    return out


# --- state ------------------------------------------------------------------


class WakeState:
    """Board cursors + held events + last-wake times, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cursors: dict[str, int] = {}
        self.pending: dict[str, dict] = {}   # "board|task" -> card dict
        self.last_wake: dict[str, float] = {}
        self.row_failures: dict[str, int] = {}  # "board|event" -> failed classifications
        self.red_seen: dict[str, float] = {}  # S5: red_key -> when it last woke an owner
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.cursors = {str(k): int(v) for k, v in (raw.get("cursors") or {}).items()}
            self.pending = dict(raw.get("pending") or {})
            self.last_wake = {str(k): float(v) for k, v in (raw.get("last_wake") or {}).items()}
            self.red_seen = {str(k): float(v) for k, v in (raw.get("red_seen") or {}).items()}

    def write_owner_wake_state(self, now: float) -> None:
        """Blocking (fsync + rename): call via ``asyncio.to_thread`` only."""
        self.last_wake = {k: v for k, v in self.last_wake.items() if now - v < COALESCE_SECONDS * 6}
        _ns.note_red_seen(self.red_seen, (), now)
        data = {"cursors": self.cursors, "pending": self.pending, "last_wake": self.last_wake,
                "red_seen": self.red_seen}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".owner-wake-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            logger.warning("kanban owner-wake: cannot persist state %s: %s", self.path, exc)


def state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gateway" / STATE_FILENAME


def runner_state(runner: Any) -> WakeState:
    """The gateway's one :class:`WakeState`; the notifier reads its ``red_seen`` too (S5),
    so a red the notifier's wake carried does not wake the owner again from here."""
    state = getattr(runner, "_kanban_owner_wake_state", None)
    if state is None:
        state = WakeState(state_path())
        runner._kanban_owner_wake_state = state
    return state


def _record_noise(card: dict, items: list[dict], why: str) -> list[dict]:
    """Receipt each withheld item (``notify_suppressed``, path owner-wake); return the items that
    could NOT be receipted (a locked board keeps them deliverable). Blocking; worker thread."""
    class _Ev:
        def __init__(self, i: dict) -> None:
            self.id = i["event_id"]
            self.kind = i.get("kind")
            self.payload = i.get("payload")
            self.actor_session_id = i.get("actor_sid") or None
            self.actor_profile = i.get("actor_profile") or None

    return [i for i in items
            if not _ns.record_suppressed(card.get("board"), card["task_id"], _Ev(i), why, "owner-wake")]


def actionable_red_items(card: dict, items: list[dict], status: Optional[str],
                         seen: dict) -> list[dict]:
    """S5: red handbacks minus the non-actionable ones (inherited-only red, a (card, PR, head,
    red-set) that already woke, a card no longer in review). Withheld ones get a receipt.
    Blocking; worker thread."""
    keep, noise = [], {}
    for i in items:
        why = None
        if i.get("trigger") == TRIGGER_RED_HANDBACK:
            why = _ns.red_handback_noise(i.get("red_keys") or (), bool(i.get("inherited_only")),
                                         seen, status)
        if why:
            noise.setdefault(why, []).append(i)
        else:
            keep.append(i)
    for why, group in noise.items():
        keep.extend(_record_noise(card, group, why))
    return keep


def is_review_lease_lapse(item: dict) -> bool:
    """A ``reclaimed`` of an operator ``claim --review`` lease that ran out (no worker pid, and the reclaimer's
    ``dead_claimer_release_basis`` is ``operator_claim_no_worker``; a NULL pid alone can be a failed reviewer launch).
    No worker existed, so nothing is stuck; its ``heartbeat_stale`` is the implementer run's old heartbeat
    (2026-10-13: every one of the 8 ``stuck (reclaimed)`` wakes 07:00-08:30 was this shape)."""
    p = item.get("payload") or {}
    return (item.get("kind") == "reclaimed" and p.get("retry_status") == "review"
            and "worker_pid" in p and p.get("worker_pid") is None
            and p.get("dead_claimer_release_basis") == "operator_claim_no_worker")


def terminal_pending_items(kb: Any, card: dict, items: list[dict], status: Optional[str]) -> list[dict]:
    """t_26ebe71a: ``stuck`` items (gave_up / reclaimed / ...) of a review card approved at its PR head with a land
    in flight there are withheld with a ``terminal-pending`` receipt; the lander owns the card, not the owner.
    A pid-less review-claim lapse is withheld with a ``review-lease-lapse`` receipt. Blocking; worker thread."""
    from hermes_cli import kanban_terminal_pending as _tp

    lapses = [i for i in items if i.get("trigger") == TRIGGER_STUCK and is_review_lease_lapse(i)]
    if lapses:
        unrecorded = _record_noise(card, lapses, _tp.REASON_LEASE_LAPSE)
        items = [i for i in items if not any(i is x for x in lapses)] + unrecorded
    stuck = [i for i in items if i.get("trigger") == TRIGGER_STUCK]
    if not stuck or status not in ("review", "running"):
        return items
    try:
        conn = kb.connect_readonly(board=card["board"])
    except Exception:
        return items
    try:
        pending = _tp.terminal_pending(conn, card["task_id"])
    finally:
        conn.close()
    if pending is None:
        return items
    unrecorded = _record_noise(card, stuck, _tp.REASON)
    return [i for i in items if i.get("trigger") != TRIGGER_STUCK] + unrecorded


def resolve_settings() -> tuple[bool, tuple[str, ...]]:
    """(``kanban.wake_owner_session``, ``kanban.wake_owner_profiles``). Errors -> defaults."""
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        if not isinstance(kcfg, dict):
            kcfg = {}
    except Exception:
        kcfg = {}
    enabled = kcfg.get("wake_owner_session", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "off", "no")
    profiles = kcfg.get("wake_owner_profiles") or DEFAULT_OWNER_PROFILES
    if isinstance(profiles, str):
        profiles = [p.strip() for p in profiles.split(",")]
    return bool(enabled), tuple(str(p).strip() for p in profiles if str(p).strip())


# --- scan (worker thread) ---------------------------------------------------


def _initial_cursor(conn: sqlite3.Connection, now: float) -> int:
    row = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE created_at >= ?",
        (int(now - FIRST_RUN_LOOKBACK_SECONDS),),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0]) - 1
    row = conn.execute("SELECT MAX(id) FROM task_events").fetchone()
    return int(row[0] or 0) if row else 0


def scan(state: WakeState, now: float, pr_health: Optional[PrHealthFn],
         boards: Optional[Iterable[dict]] = None) -> int:
    """Read new events on every board into ``state.pending``. Returns events queued."""
    from hermes_cli import kanban_db as kb

    if boards is None:
        try:
            boards = kb.list_boards(include_archived=False)
        except Exception:
            boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
    queued = 0
    seen: set[str] = set()
    memo: dict = {}
    for meta in boards:
        slug = str(meta.get("slug") or kb.DEFAULT_BOARD)
        try:
            path = Path(meta["db_path"]).expanduser() if meta.get("db_path") else kb.kanban_db_path(slug)
            resolved = str(path.resolve())
        except Exception:
            continue
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            conn = kb.connect_readonly(db_path=path)
        except Exception as exc:
            logger.debug("kanban owner-wake: cannot open board %s: %s", slug, exc)
            continue
        conn.row_factory = sqlite3.Row
        try:
            if slug not in state.cursors:
                state.cursors[slug] = _initial_cursor(conn, now)
            cursor = state.cursors[slug]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
            actor = "e.actor_session_id" if "actor_session_id" in cols else "NULL"
            actor_profile = "e.actor_profile" if "actor_profile" in cols else "NULL"
            marks = ",".join("?" for _ in SCAN_KINDS)
            rows = conn.execute(
                f"SELECT e.id, e.task_id, e.kind, e.payload, e.run_id, e.created_at, "
                f"{actor} AS actor_sid, {actor_profile} AS actor_profile, "
                f"t.session_id AS home_sid, t.title, t.body, "
                f"t.result FROM task_events e JOIN tasks t ON t.id = e.task_id "
                f"WHERE e.id > ? AND e.kind IN ({marks}) ORDER BY e.id LIMIT ?",
                (cursor, *SCAN_KINDS, SCAN_LIMIT),
            ).fetchall()
            for r in rows:
                # The cursor moves past a row only once it is classified and
                # queued; a failure leaves it for the next tick (Prism P1).
                fkey = f"{slug}|{r['id']}"
                try:
                    item = _classify_row(kb, conn, r, pr_health, memo)
                except Exception as exc:
                    n = state.row_failures.get(fkey, 0) + 1
                    state.row_failures[fkey] = n
                    if n < ROW_RETRY_LIMIT:
                        logger.warning("kanban owner-wake: event %s on %s failed to classify "
                                       "(%s); retrying next tick", r["id"], slug, exc)
                        break
                    logger.warning("kanban owner-wake: event %s on %s failed %d times (%s); "
                                   "skipping it", r["id"], slug, n, exc)
                    item = None
                state.row_failures.pop(fkey, None)
                if item is not None:
                    item["queued_at"] = now
                    key = f"{slug}|{r['task_id']}"
                    card = state.pending.setdefault(key, {
                        "board": slug, "task_id": r["task_id"], "home_sid": r["home_sid"] or "",
                        "title": r["title"] or "", "items": [],
                    })
                    card["home_sid"] = r["home_sid"] or card.get("home_sid") or ""
                    card["items"].append(item)
                    queued += 1
                state.cursors[slug] = max(state.cursors[slug], int(r["id"]))
            # A busy board drains over several ticks, LIMIT rows at a time.
        except Exception as exc:
            logger.warning("kanban owner-wake: scan of board %s failed: %s", slug, exc)
        finally:
            conn.close()
    return queued


def _classify_row(kb: Any, conn: Any, r: Any, pr_health: Optional[PrHealthFn],
                  memo: dict) -> Optional[dict]:
    if wake_off(r["body"]):
        return None
    try:
        payload = json.loads(r["payload"]) if r["payload"] else None
    except ValueError:
        payload = None
    kind = r["kind"]
    item = {"event_id": int(r["id"]), "kind": kind, "actor_sid": r["actor_sid"] or "",
            "actor_profile": (r["actor_profile"] if "actor_profile" in r.keys() else None) or "",
            "event_ts": r["created_at"], "payload": payload if isinstance(payload, dict) else None}
    if kind == "blocked":
        trig = classify_block(payload)
        if trig is None:
            return None
        item.update(trigger=trig, reason=_clip((payload or {}).get("reason")))
    elif is_stuck(kind, payload):
        err = (payload or {}).get("error") or (payload or {}).get("reason") or ""
        item.update(trigger=TRIGGER_STUCK, reason=_clip(err, 400))
    elif kind == "completed":
        kids = gated_ready_children(kb, conn, r["task_id"])
        if not kids:
            return None
        item.update(trigger=TRIGGER_LAST_BLOCKER, children=kids)
    elif kind == "skipped_nonspawnable":
        prev = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND id < ? AND kind IN "
            "('schedule_elapsed', 'skipped_nonspawnable', 'claimed') ORDER BY id DESC LIMIT 1",
            (r["task_id"], r["id"])).fetchone()
        if prev is None or prev["kind"] != "schedule_elapsed":
            return None   # a plain ready card on a human assignee, not a scheduled wake
        item.update(trigger=TRIGGER_WOKE_NONSPAWNABLE, assignee=str((payload or {}).get("assignee") or ""))
    elif kind == "review_requested":
        verdict = handback_pr_verdict(kb, conn, {"id": r["task_id"], "result": r["result"]}, payload,
                                      r["run_id"], pr_health, memo)
        if not verdict["state"]:
            return None
        item.update(trigger=TRIGGER_RED_HANDBACK, pr_state=verdict["state"],
                    red_keys=verdict["keys"], inherited_only=verdict["inherited_only"])
    else:
        return None
    return item


# --- owner resolution + delivery (event loop) -------------------------------


def read_card(kb: Any, board: str, task_id: str) -> Optional[dict]:
    """Current ``{session_id, body, title, status}`` of a card, or None if it is gone.

    Read at DELIVERY time: a held event must follow a re-homed card and honour a
    ``wake: off`` added after it was queued (Prism P1s)."""
    conn = kb.connect_readonly(board=board)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT session_id, body, title, status FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def resolve_owner(store: Any, home_sid: str, read_row: Callable[[str], Optional[dict]]):
    """The live session-store entry that owns a card, or None.

    ``tasks.session_id`` is either a raw session id or (gateway-created cards)
    a session KEY. A raw id whose session was rotated (compression, /new)
    is followed through the state.db row's ``session_key``. Raises
    ``kanban_home_route.SessionLookupError`` if state.db cannot be read."""
    sid = (home_sid or "").strip()
    if not sid or sid.startswith("operator:") or store is None:
        return None
    if ":" in sid:
        return store.lookup_by_session_key(sid)
    entry = store.lookup_by_session_id(sid)
    if entry is not None:
        return entry
    row = read_row(sid)
    key = str((row or {}).get("session_key") or "").strip()
    return store.lookup_by_session_key(key) if key else None


def is_chat_origin(entry: Any) -> bool:
    origin = getattr(entry, "origin", None)
    if origin is None or not getattr(origin, "chat_id", None):
        return False
    plat = str(getattr(getattr(origin, "platform", None), "value", getattr(origin, "platform", "")))
    return plat.lower() not in _NON_CHAT_PLATFORMS


def _log_needs_input_cut(key: str, before: list[dict], after: list[dict], why: str) -> None:
    """INFO line for each needs_input item a stage removes (t_156cc147: the 08:52 drop left no line)."""
    kept = {id(i) for i in after}
    ids = [i.get("event_id") for i in before
           if i.get("trigger") == TRIGGER_NEEDS_INPUT and id(i) not in kept]
    if ids:
        logger.info("kanban owner-wake: needs_input event(s) %s of %s dropped without an owner-wake turn: %s",
                    ",".join(str(x) for x in ids), key, why)


def mark_carried(runner: Any, board: str, task_id: str, event_ids: Iterable[int],
                 platform: Any, chat_id: Any, thread_id: Any = "",
                 now: Optional[float] = None, who: Iterable[Any] = (),
                 profile: Any = "") -> None:
    """t_b1d26df7: the notifier's DELIVERED wake into this chat already carried the
    red-PR state (or, t_f3323c8a, the ready children; t_156cc147, a block or stuck
    wake) of these events, so the owner-wake must not wake that chat again.
    ``who``/``profile`` are the sub's participant ids and notifier profile (same
    platform, chat, thread, participant and profile must match the owner), kept
    here because a terminal ``completed`` unsubscribes the sub before the
    owner-wake runs. In-memory: a restart loses it (cost: one duplicate turn)."""
    now = time.time() if now is None else now
    seen = getattr(runner, "_kanban_red_carried", None)
    if seen is None:
        seen = {}
        runner._kanban_red_carried = seen
    for k in [k for k, v in seen.items() if now - v[0] > PENDING_MAX_AGE_SECONDS]:
        seen.pop(k, None)
    ids = frozenset(str(w or "").strip() for w in who) - {""}
    prof = str(profile or "").strip() or "default"
    for eid in event_ids:
        seen[_carried_key(board, task_id, eid, platform, chat_id, thread_id)] = (now, ids, prof)


def _carried_key(board: Any, task_id: Any, event_id: Any, platform: Any, chat_id: Any,
                 thread_id: Any) -> str:
    plat = str(getattr(platform, "value", platform) or "").strip().lower()
    chat = str(chat_id or "").strip()
    thread = str(thread_id or "").strip()
    if thread == chat:
        thread = ""
    return f"{board}|{task_id}|{int(event_id)}|{plat}|{chat}|{thread}"


def is_carried(runner: Any, board: str, task_id: str, event_id: Any, origin: Any,
               profile: Any = "") -> bool:
    """Carried into the OWNER's session: same chat, a participant id shared with
    ``origin`` and the owner's profile. An identity-less mark never counts (a
    duplicate turn is the safe failure, a lost one is not)."""
    seen = getattr(runner, "_kanban_red_carried", None) or {}
    hit = seen.get(_carried_key(board, task_id, event_id, getattr(origin, "platform", ""),
                                getattr(origin, "chat_id", ""), getattr(origin, "thread_id", "")))
    if hit is None:
        return False
    _t, ids, prof = hit
    owner_ids = {str(getattr(origin, a, "") or "").strip() for a in ("user_id", "user_id_alt")} - {""}
    return bool(ids & owner_ids) and prof == (str(profile or "").strip() or "default")


def _owner_home(runner: Any, entry: Any, owner_profile: str) -> Optional[Path]:
    """The owner profile's HERMES_HOME under multiplex (its config.yaml holds its
    policy), else None (= this process's own home)."""
    if not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return None
    try:
        from hermes_cli.profiles import get_profile_dir

        return get_profile_dir(owner_profile or "default")
    except Exception:
        return None


def _drop_redundant(kb: Any, card: dict, items: list[dict], own: set, owner_profile: str,
                    cfg: _ns.SuppressConfig) -> list[dict]:
    """Items minus the ones :func:`kanban_notify_suppress.is_redundant` calls the
    owner's own doing; each dropped item gets a ``notify_suppressed`` receipt.
    Blocking (board read + write); worker thread only."""
    class _Ev:
        def __init__(self, i: dict) -> None:
            self.id = i["event_id"]
            self.kind = i.get("kind")
            self.payload = i.get("payload")
            self.created_at = i.get("event_ts")
            self.actor_session_id = i.get("actor_sid") or None
            self.actor_profile = i.get("actor_profile") or None

    ctx = None
    kept = []
    for i in items:
        ev = _Ev(i)
        if ctx is None:
            try:
                conn = kb.connect_readonly(board=card["board"])
                try:
                    ctx = _ns.card_context(conn, card["task_id"], card.get("home_sid"), owner_profile)
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("kanban owner-wake: suppress context for %s unreadable (%s); not suppressing",
                             card["task_id"], exc)
                return items
        red, why = _ns.is_redundant(ev, ctx, {"session_ids": own}, cfg, trigger=i.get("trigger"))
        # Withheld only once the receipt is persisted: a failed write (locked
        # board) keeps the item deliverable rather than losing the audit row
        # (Prism P1 91fac1e92097).
        if red and _ns.record_suppressed(card.get("board"), card["task_id"], ev, why, "owner-wake"):
            continue
        kept.append(i)
    return kept


async def deliver_turn(runner: Any, entry: Any, text: str,
                       cards: Optional[list[dict]] = None) -> None:
    """Enqueue one handoff turn into ``entry``'s session. Raises on no route."""
    from gateway.platforms.base import MessageEvent, MessageType

    source = dataclasses.replace(entry.origin)
    adapter = runner._adapter_for_source(source)  # noqa: SLF001
    if adapter is None:
        raise RuntimeError(f"no live adapter for {getattr(source.platform, 'value', source.platform)}")
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        allow_gateway_control=False,
        metadata={
            "kanban_owner_wake": True,
            "gateway_session_key": entry.session_key,
            # Not strict: a turn queued behind a busy session survives a
            # compression rotation. run.py follows the pinned id through its
            # verified compression lineage and still drops it after a /new
            # (_resolve_async_delegation_session; Prism P1).
            "gateway_session_id": entry.session_id,
            # Re-checked when a busy session dequeues the turn (t_07ffc6cb).
            _fresh.META_KEY: [
                _fresh.card_entry(c.get("board"), c["task_id"],
                                  [i.get("kind") for i in c.get("items") or []],
                                  max((i.get("event_ts") or 0 for i in c.get("items") or []),
                                      default=None),
                                  render_prompt([c]))
                for c in cards or []
            ],
        },
    )
    await adapter.handle_message(event)


async def tick(runner: Any, *, now: Optional[float] = None,
               pr_health: Optional[PrHealthFn] = None, use_default_pr_health: bool = True,
               state: Optional[WakeState] = None) -> int:
    """One owner-wake pass. Returns the number of turns enqueued."""
    import asyncio

    from gateway import kanban_home_route as _hr
    from hermes_cli import kanban_db as kb

    enabled, profiles = resolve_settings()
    if not enabled:
        return 0
    suppress_cfgs: dict[str, _ns.SuppressConfig] = {}
    try:
        active = runner._active_profile_name()  # noqa: SLF001
    except Exception:
        active = "default"
    hosted = {active} | {str(p) for p in (getattr(runner, "_profile_adapters", None) or {})}
    if not hosted & set(profiles):
        return 0  # a worker-only gateway hosts no operator session: skip the scan
    if getattr(runner, "_draining", False):
        return 0
    now = time.time() if now is None else now
    if state is None:
        state = runner_state(runner)
    if pr_health is None and use_default_pr_health:
        pr_health = default_pr_health()
    await asyncio.to_thread(scan, state, now, pr_health)

    from gateway import kanban_boomerang_wake as _ah

    auto_on = _ah.enabled()
    store = getattr(runner, "session_store", None)
    by_session: dict[str, tuple[Any, list[tuple[str, dict]]]] = {}
    for key, card in list(state.pending.items()):
        # Expire ITEMS by their own age, never a fresh event with an old card.
        fresh = [i for i in card.get("items") or []
                 if now - float(i.get("queued_at", now)) <= PENDING_MAX_AGE_SECONDS]
        if len(fresh) != len(card.get("items") or []):
            logger.warning("kanban owner-wake: dropping %d event(s) of %s held > %ds",
                           len(card["items"]) - len(fresh), key, PENDING_MAX_AGE_SECONDS)
            card["items"] = fresh
        if not fresh:
            state.pending.pop(key, None)
            continue
        if now - state.last_wake.get(key, 0.0) < COALESCE_SECONDS:
            continue  # held: the next turn after the window lists it
        try:
            current = await asyncio.to_thread(read_card, kb, card["board"], card["task_id"])
        except Exception as exc:
            logger.warning("kanban owner-wake: cannot re-read %s (%s); retrying", key, exc)
            continue
        if current is None or wake_off(current.get("body")):
            state.pending.pop(key, None)  # card gone, or opted out while held
            continue
        # t_07ffc6cb: a card that went done/archived while its events were held
        # needs nothing from its owner any more.
        live = []
        for i in card["items"]:
            if _fresh.is_stale(current.get("status"), str(i.get("kind") or "")):
                _fresh.log_dropped(card["task_id"], str(i.get("kind")), i.get("event_ts"),
                                   "owner-wake", current.get("status"), now)
            else:
                live.append(i)
        if not live:
            state.pending.pop(key, None)
            continue
        card["items"] = live
        card["home_sid"] = str(current.get("session_id") or "")
        card["title"] = current.get("title") or card.get("title") or ""
        try:
            entry = await asyncio.to_thread(resolve_owner, store, card["home_sid"],
                                            _hr.read_session_row)
        except _hr.SessionLookupError as exc:
            logger.warning("kanban owner-wake: home of %s unknown (%s); retrying", key, exc)
            continue
        # The operator gate applies to the OWNER's profile: one multiplexing
        # gateway serves several profiles from one store (Prism P1).
        owner_profile = str(getattr(getattr(entry, "origin", None), "profile", None)
                            or active or "default")
        if entry is None or not is_chat_origin(entry) or owner_profile not in profiles:
            why = ("no live session-store entry for home " + repr(card["home_sid"]) if entry is None
                   else "home is not a chat origin" if not is_chat_origin(entry)
                   else f"owner profile {owner_profile!r} is not an operator profile")
            _log_needs_input_cut(key, card["items"], [], why)
            logger.debug("kanban owner-wake: %s has no live operator home here; line only", key)
            state.pending.pop(key, None)
            continue
        own = {str(card.get("home_sid") or ""), str(getattr(entry, "session_id", "") or "")}
        # t_f3323c8a W8b: "children ready" is news even to the session that closed
        # the last blocker, so protected triggers skip the own-session cut too.
        items = [i for i in card["items"]
                 if i.get("trigger") in _ns.NEVER_SUPPRESS_TRIGGERS
                 or not (i.get("actor_sid") and i["actor_sid"] in own)]
        _log_needs_input_cut(key, card["items"], items, "made by the owner session itself")
        # t_4c7da648: the shared redundancy predicate (operator profile / closer
        # after the operator's APPROVE). Protected triggers never drop here.
        if items:
            # Policy of the OWNER's profile (one multiplexing gateway serves several).
            if owner_profile not in suppress_cfgs:
                suppress_cfgs[owner_profile] = await asyncio.to_thread(
                    _ns.resolve_config, _owner_home(runner, entry, owner_profile))
            if suppress_cfgs[owner_profile].enabled:
                before = items
                items = await asyncio.to_thread(
                    _drop_redundant, kb, card, items, own, owner_profile, suppress_cfgs[owner_profile])
                _log_needs_input_cut(key, before, items, "redundant (notify_suppressed receipt)")
        # t_b1d26df7 / t_f3323c8a W8c / t_156cc147: an event the notifier's wake
        # already DELIVERED into the owner's session (same event id, marked by
        # mark_carried) is one wake, not two. A sub whose wake the contention gate
        # sent as a notify marks nothing, so the owner-wake still fires for it.
        before = items
        items = [i for i in items
                 if not (i["kind"] in CARRIED_KINDS
                         and is_carried(runner, card["board"], card["task_id"], i["event_id"],
                                        entry.origin, owner_profile))]
        _log_needs_input_cut(key, before, items, "the notifier's delivered wake carried it")
        # S5 (t_4f6afb67): a red handback the owner cannot act on is a ledger row.
        if items and any(i.get("trigger") == TRIGGER_RED_HANDBACK for i in items):
            if owner_profile not in suppress_cfgs:
                suppress_cfgs[owner_profile] = await asyncio.to_thread(
                    _ns.resolve_config, _owner_home(runner, entry, owner_profile))
            if suppress_cfgs[owner_profile].actionable_only:
                items = await asyncio.to_thread(
                    actionable_red_items, card, items, current.get("status"), state.red_seen)
        if items and any(i.get("trigger") == TRIGGER_STUCK for i in items):
            if owner_profile not in suppress_cfgs:
                suppress_cfgs[owner_profile] = await asyncio.to_thread(
                    _ns.resolve_config, _owner_home(runner, entry, owner_profile))
            # Same kill switch as the notifier's terminal-pending branch (``_s5``).
            if suppress_cfgs[owner_profile].enabled and suppress_cfgs[owner_profile].actionable_only:
                items = await asyncio.to_thread(terminal_pending_items, kb, card, items, current.get("status"))
        # Boomerang v2 P3b (spec §5.5), AFTER the suppressor: a boomerang-wake item is a
        # child's, not a turn; one the notifier already claimed is skipped here.
        if items:
            before = items
            items = await _ah.owner_wake_filter(runner, card, items, entry, dispatch_on=auto_on)
            _log_needs_input_cut(key, before, items, "a boomerang-wake claim owns it")
        if not items:
            state.pending.pop(key, None)
            continue
        card = dict(card, items=items)
        by_session.setdefault(entry.session_key, (entry, []))[1].append((key, card))

    turns = 0
    for session_key, (entry, cards) in by_session.items():
        text = render_prompt([c for _k, c in cards])
        try:
            await deliver_turn(runner, entry, text, [c for _k, c in cards])
        except Exception as exc:
            logger.warning("kanban owner-wake: wake of %s failed (%s); held for retry",
                           session_key, exc)
            continue
        turns += 1
        for key, card in cards:
            state.pending.pop(key, None)
            state.last_wake[key] = now
            for i in card.get("items") or []:
                _ns.note_red_seen(state.red_seen, i.get("red_keys") or (), now)
        logger.info(
            "kanban owner-wake: woke %s for %s",
            session_key,
            ", ".join(f"{c['task_id']}({'+'.join(i['trigger'] for i in c['items'])})"
                      for _k, c in cards),
        )
    await asyncio.to_thread(state.write_owner_wake_state, now)
    return turns
