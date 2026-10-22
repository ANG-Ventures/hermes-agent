"""Does a review handback answer every Prism key open at its head? (t_6e9f48cc)

Feeds the ``own_repo_review_all_keys_answered`` disposition row. Two reads, both owned by
the hermes-home scripts the landing queue already trusts, never a second copy:

- the OPEN keys: ``scripts/prism_soft_gate.py round --repo --pr --head --card`` (the
  same FRState the merge decision and ``landing_queue.enqueue`` read). Every
  ``unanswered:`` / ``pending-carry:`` / ``open-at-head:`` line is one open key.
- the ANSWERS: the ``prism-verdict:`` grammar of that same module (``FIXED_LINE`` /
  ``VERDICT_LINE``) over the handback's own text (run summary, event summary, card
  result, ``metadata.prism_verdicts``). Any other spelling answers nothing.

``all_answered`` is tri-state: True only when the round is readable, the text carries at
least one grammar line, and every open key is answered by its 12-hex key. False when a
key is unanswered or the text has no line. None when the round is unreadable (no home
script, rc != 0, unknown state): the caller then surfaces.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

#: ``round`` states whose key list is final for the head. WAIT (no Prism record yet), an
#: in-flight review, FR-UNKNOWN and anything new are not: the handback surfaces.
READABLE_STATES = frozenset({"ARM", "HOLD", "FR-TIMEOUT", "FR-EXHAUSTED", "FR-OVERRIDDEN-BY-OPERATOR"})
_KEY_LINE = re.compile(r"^(?:unanswered|pending-carry|open-at-head):\s+([0-9a-f]{12})\b", re.M)
_ROUND_LINE = re.compile(r"^round=\d+ .*\bstate=(\S+)\s*$", re.M)
ROUND_TIMEOUT_S = 90

_GRAMMAR: dict = {}


def _scripts() -> Path:
    """The board scripts are a ROOT asset (``~/.hermes/scripts``), never a profile's."""
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / "scripts"


def _grammar() -> Optional[tuple]:
    """``(FIXED_LINE, VERDICT_LINE)`` from the home's prism_soft_gate.py, cached by mtime."""
    path = _scripts() / "prism_soft_gate.py"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _GRAMMAR.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        spec = importlib.util.spec_from_file_location("_hermes_prism_soft_gate_grammar", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pair = (mod.FIXED_LINE, mod.VERDICT_LINE)
    except Exception as exc:
        logger.debug("kanban prism answers: grammar unreadable (%s)", exc)
        return None
    _GRAMMAR[str(path)] = (mtime, pair)
    return pair


def answered_keys(texts: Iterable[Any]) -> Optional[set]:
    """12-hex keys answered in ``texts`` by the gate's grammar; None when the grammar is unreadable."""
    grammar = _grammar()
    if grammar is None:
        return None
    out: set = set()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for rx in grammar:
            out.update(m.group(1)[:12].lower() for m in rx.finditer(text))
    return out


def _default_open_keys(repo: str, pr: int, head: str, card: str) -> Optional[list]:
    """Open keys of ``round`` at ``head`` (card-scoped answers applied), or None when unreadable."""
    script = _scripts() / "prism_soft_gate.py"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "round", "--repo", repo, "--pr", str(int(pr)), "--head", head,
             "--card", card],
            capture_output=True, text=True, timeout=ROUND_TIMEOUT_S, stdin=subprocess.DEVNULL,
            cwd=str(script.parent.parent))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("kanban prism answers: round failed (%s)", exc)
        return None
    out = proc.stdout or ""
    states = _ROUND_LINE.findall(out)
    if proc.returncode != 0 or not states or states[-1] not in READABLE_STATES:
        return None
    return sorted(set(_KEY_LINE.findall(out)))


#: Seam (tests): ``(repo, pr, head, card) -> [key12] | None``.
open_keys_fn: Callable[..., Optional[list]] = _default_open_keys


def all_answered(repo: str, pr: int, head: str, card: str, texts: Iterable[Any]) -> Optional[bool]:
    """True: every open key at ``head`` is answered in ``texts`` (>= 1 grammar line present).
    False: a key is unanswered, or ``texts`` has no grammar line. None: unreadable."""
    if not (repo and pr and re.fullmatch(r"[0-9a-f]{40}", str(head or "")) and card):
        return None
    answered = answered_keys(list(texts))
    if answered is None:
        return None
    try:
        keys = open_keys_fn(repo, int(pr), str(head), str(card))
    except Exception as exc:
        logger.debug("kanban prism answers: open keys of %s#%s unread (%s)", repo, pr, exc)
        return None
    if keys is None:
        return None
    if not answered:
        return False
    return all(str(k)[:12].lower() in answered for k in keys)
