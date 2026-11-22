"""Merge-hold drafts: a worker's ``draft_ok`` becomes a ledger hold + ready PR + review (t_12401145).

A worker cannot set a ledger hold (hermes-home ``scripts/lib/pr_hold.py`` refuses in a kanban worker) and the
derived approver merges an unheld ready PR in ~2 min, so a worker that wants a merge hold leaves its PR a DRAFT and
completes with ``draft_ok``. That closed the card ``done`` with nothing reviewed, and a draft skips CI and Prism
(78 ``completion_draft_override`` events on 62 cards in 7 days to 2026-11-21).

``draft_kind`` on the completion says which override it is. It is an explicit field, never read from the reason:

- ``merge_hold`` (the default when unset): for each of the card's OWN draft PRs (handoff metadata /
  ``--survivor-pr`` / head branch names the card) the kernel (1) sets a ledger hold through ``pr_hold.write`` (the
  writer ``pr_hold.py set`` uses, from the BOARD root, read back through the readers' ``held()``; by = the
  worker run, why = the reason), (2) marks the PR ready through
  ``ace_gate.py ready`` so CI and Prism run, and (3) keeps it on the open-PR route, so the card goes to review
  through the review hold and the settle tick hands it to the operator. A PR whose hold could not be written is
  NOT flipped (an unheld ready PR merges); it stays draft and the card goes straight to the human reviewer.
- ``keep_draft``: today's override, unchanged (a stacked/pinned PR that must stay draft; the card closes done).

A draft the card merely mentions, or one someone else re-drafted after this card flipped it (``draft_held``), keeps
today's override. Kill switch ``KANBAN_DRAFT_MERGE_HOLD=0``: every override behaves as ``keep_draft``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import time
from typing import Callable, Iterable, Optional

_log = logging.getLogger(__name__)

MERGE_HOLD = "merge_hold"
KEEP_DRAFT = "keep_draft"
KINDS = (MERGE_HOLD, KEEP_DRAFT)

HoldFn = Callable[[str, int, str, str], None]          # (repo, number, why, by); raises on failure
ReadyFn = Callable[[str, int], Optional[int]]          # ace_gate.py ready rc, None = not run

# Stable causes that lead every pr_hold_set failure (and so the recorded ``hold: failed: ...`` text).
CAUSE_NO_WRITER = "no pr_hold.py under the board root"
CAUSE_READER_ROOT = "reader root is not the board root"
CAUSE_READ_BACK = "hold read-back failed"
CAUSE_LABEL_MISSING = "held, label missing"


class HeldLabelMissing(RuntimeError):
    """The ledger row is written and ``held()`` reads it back, but the ``hold`` label step failed. The ledger is
    what the readers gate on, so the PR IS held; recorded as such (not "failed") so event and ledger agree. The PR
    still stays draft and goes to the human: the label half of the operator's hold is missing."""


def enabled() -> bool:
    return os.environ.get("KANBAN_DRAFT_MERGE_HOLD", "1").strip() != "0"


def normalize_kind(value) -> str:
    """``draft_kind`` -> one of :data:`KINDS`; unset/blank = :data:`MERGE_HOLD`. Raises ValueError otherwise."""
    if value is None or not str(value).strip():
        return MERGE_HOLD
    kind = str(value).strip().lower().replace("-", "_")
    if kind not in KINDS:
        raise ValueError(f"draft_kind must be one of {', '.join(KINDS)}, got {value!r}")
    return kind


def plan(overridden: Iterable[str], *, kind: str, own_keys: Iterable[str],
         held: Iterable[str] = ()) -> list:
    """The overridden draft keys this completion merge-holds (in order); [] = today's override for all."""
    if not enabled() or kind != MERGE_HOLD:
        return []
    own = {str(k).lower() for k in own_keys}
    skip = {str(k).lower() for k in held}
    return [k for k in overridden if k.lower() in own and k.lower() not in skip]


def by_of(assignee: Optional[str], run_id: Optional[int], task_id: str) -> str:
    """The ledger row's ``by``: the worker run that asked for the hold."""
    who = (assignee or "worker").strip() or "worker"
    return f"{who} run {run_id} ({task_id})" if run_id is not None else f"{who} ({task_id})"


def _board_root():
    """The root beside the board DB: where the hold's readers run (derived-approve-tick, land_on_approve).

    The kanban kernel runs in the worker PROCESS, which is always the dispatcher's child on the board host; a
    pool-placed worker gets only its terminal/file tools over ssh (``kanban_worker_hosts`` module docstring). The
    pr-hold ledger the readers use is ``<board root>/state/pr-hold.jsonl``, so the hold is written there or not at
    all."""
    from hermes_cli.kanban_db import kanban_home
    return kanban_home()


def _load_pr_hold(root):
    path = root / "scripts" / "lib" / "pr_hold.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_kanban_pr_hold", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # type: ignore[union-attr]
    return mod


def _same(a, b) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def pr_hold_set(repo: str, number: int, why: str, by: str) -> None:
    """Set the hold through the board root's ``pr_hold.write("set", ...)``, then read it back. Raises unless the
    readers' own ``held()`` now returns THIS hold.

    Refuses before any write when the ledger ``pr_hold`` resolves for its readers is not the board root's (a
    kernel off the board host, or a linked-worktree root): a hold in a ledger nobody reads, followed by a ready
    flip, is a PR the derived approver merges in ~2 min. After the write, ``held(repo, n)`` with NO root (the
    resolution derived-approve-tick and land_on_approve use) must name this run's ``by`` and ``why``; anything
    else (no hold, someone else's, an unreadable ledger) is a failed hold and the PR is not flipped."""
    # Each message LEADS with a short stable cause, paths after: apply() truncates, and the cause must survive.
    root = _board_root()
    mod = _load_pr_hold(root)
    if mod is None:
        raise RuntimeError(f"{CAUSE_NO_WRITER}: scripts/lib/pr_hold.py not found under {root}")
    reader_root = mod._root()
    if not _same(reader_root, root):
        raise RuntimeError(f"{CAUSE_READER_ROOT}; no write (readers {reader_root}, board {root})")
    def ours(got) -> bool:
        return isinstance(got, str) and got.startswith(f"operator hold ({by} at ") and got.endswith(f"): {why or '-'}")

    try:
        mod.write("set", repo, int(number), why, by)
    except Exception as exc:  # noqa: BLE001 -- write() appends the row BEFORE the label: is the ledger hold there?
        try:
            got = mod.held(repo, int(number))
        except Exception:  # noqa: BLE001 -- unreadable: report the write failure as is
            got = None
        if ours(got):
            raise HeldLabelMissing(f"{CAUSE_LABEL_MISSING}: {exc}") from exc
        raise
    got = mod.held(repo, int(number))
    if not ours(got):
        raise RuntimeError(f"{CAUSE_READ_BACK}: held() = {str(got)[:120]!r}")


def _default_hold() -> Optional[HoldFn]:
    # Same seam as kanban_pr_freshness._default_gh: no live ledger or GitHub write from pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return pr_hold_set


def apply(keys: Iterable[str], *, why: str, by: str, hold: Optional[HoldFn] = None,
          ready: Optional[ReadyFn] = None) -> dict:
    """Hold then flip each ``"o/r#n"`` key. -> ``{key: {"hold", "ready", "ok", "at"}}``; never raises.

    The hold is written first and the flip runs only after it succeeded: a ready PR with no hold is exactly what
    the derived approver merges. ``ok`` = held AND ready (CI and Prism will run)."""
    if hold is None:
        hold = _default_hold()
    if ready is None:
        from hermes_cli import kanban_pr_freshness as _fresh
        ready = _fresh._default_ready()
    out: dict = {}
    for key in keys:
        repo, _, num = key.rpartition("#")
        entry: dict = {"ok": False}
        out[key] = entry
        try:
            if hold is None:
                raise RuntimeError("no pr_hold writer")
            hold(repo, int(num), why, by)
            entry["hold"] = "set"
        except HeldLabelMissing as exc:   # ledger-held, label absent: say so; no flip, the human takes it
            entry["hold"] = str(exc)[:200]
            _log.warning("draft merge hold: %s %s", key, exc)
            continue
        except Exception as exc:  # noqa: BLE001 -- recorded; the PR stays draft
            entry["hold"] = f"failed: {str(exc)[:160]}"
            _log.warning("draft merge hold: %s hold failed: %s", key, exc)
            continue
        entry["at"] = int(time.time())   # BEFORE the flip: every post-ready run starts after it
        try:
            rc = ready(repo, int(num)) if ready is not None else None
        except Exception as exc:  # noqa: BLE001 -- held, still draft: the human takes it (Prism b4aec6063a55)
            rc = f"raised: {str(exc)[:160]}"
            _log.warning("draft merge hold: %s ready flip raised: %s", key, exc)
        entry["ready"] = rc
        entry["ok"] = rc == 0
    return out


def note(result: dict) -> list:
    """One handback line per merge-held PR."""
    lines = []
    for key, e in sorted(result.items()):
        if e.get("ok"):
            lines.append(f"{key}: merge hold set (pr_hold ledger) and marked ready; CI and Prism run, "
                         f"the operator lifts the hold to land")
        elif str(e.get("hold") or "").startswith(CAUSE_LABEL_MISSING):
            lines.append(f"{key}: merge hold in the pr_hold ledger but the `hold` label add failed ({e.get('hold')}); "
                         f"left DRAFT, needs the operator (pr_hold.py status reads held)")
        elif e.get("hold") == "set":
            lines.append(f"{key}: merge hold set but ready flip failed (ace_gate rc={e.get('ready')}); "
                         f"still DRAFT, needs the operator")
        else:
            lines.append(f"{key}: merge hold NOT set ({e.get('hold')}); left DRAFT, needs the operator")
    return lines


def replay_route(payload: dict, own_keys: Iterable[str]) -> str:
    """How a recorded ``completion_draft_override`` event routes under this change (the 7-day replay).

    ``merge_hold`` events with an own draft go to review (``review_hold`` when hold+flip work); everything else
    keeps today's ``done``. Old events carry no ``kind``, so they read as the default."""
    try:
        kind = normalize_kind(payload.get("kind"))
    except ValueError:
        kind = KEEP_DRAFT
    keys = plan(payload.get("prs") or [], kind=kind, own_keys=own_keys,
                held=(payload.get("held") or {}).keys())
    return "review" if keys else "done"
