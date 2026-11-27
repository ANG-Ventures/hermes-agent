"""The ``requires:`` line a child inherits from its parents, and the stored-body check (t_1aea9763).

``create_task`` used to append every hard declared word of every parent to the child's body as one
bare ``requires: <words>`` line, after the caller's body had passed hermes-home's
studio-requires-reason check. On 2026-11-12 that append wrote 53 of the board's 107 bare
``requires: studio-state`` lines, and held cards like t_7dabb879 off every pool host.

1. A Studio-only word (served by no pool host: :func:`kanban_studio_capability.studio_only`, the
   set the home hook's ``studio_only_caps`` computes) is not inherited. A child that does need the
   Studio fails fast on a pool host and is re-routed with a reason. Other hard words inherit as
   before. ``--continues`` is unchanged: its card inherits every word, and the line says where it
   came from so the stored-body check accepts it.
2. :func:`stored_refusal` runs the same studio-requires-reason verdict on the body ``create_task``
   is about to INSERT, after every append. Same kill switch (``STUDIO_REQUIRES_REASON=0``), same
   fail-open on a missing hook module.
"""
from __future__ import annotations

import sys
from typing import Iterable, List, Optional, Tuple

NOT_INHERITED_PREFIX = "requires not inherited: "


class StoredBodyRefused(ValueError):
    """The body create_task would store carries a ``requires:`` line the studio rule refuses."""


def inherited_line(conn, parents: Iterable[str], fleet_dir, *, continues: Optional[str] = None
                   ) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """``(line or None, [(word, parent), ...] not inherited)`` for a child of ``parents``.

    Only hard parent declarations inherit (body sniffs never do). An unreadable capability map
    leaves the Studio-only set unknown: everything inherits, as before this card."""
    from hermes_cli import kanban_host_capabilities as khc
    from hermes_cli import kanban_studio_capability as ksc
    cmap = khc.load_map(fleet_dir)
    only = None if continues else ksc.studio_only(cmap)
    words: set = set()
    dropped: List[Tuple[str, str]] = []
    for parent in parents:
        row = conn.execute("SELECT body, skills FROM tasks WHERE id=?", (parent,)).fetchone()
        if not row:
            continue
        found = {w for w in khc.declared_words(row[0]) if w != khc.NONE_WORD}
        if cmap is not None:
            import json
            try:
                skills = json.loads(row[1]) if row[1] else []
            except (TypeError, ValueError):
                skills = []
            reqs, _none = khc.derive(row[0], skills or [], cmap)
            found.update(r.cap.removeprefix("unknown:") for r in reqs if r.strength == "hard")
        for w in sorted(found):
            if only and w in only:
                if (w, parent) not in dropped:
                    dropped.append((w, parent))
            else:
                words.add(w)
    if not words:
        return None, dropped
    line = "requires: " + ", ".join(sorted(words))
    if continues:
        line += f" (inherited from {continues} by --continues)"
    return line, dropped


def report_not_inherited(conn, task_id: str, dropped: List[Tuple[str, str]]) -> None:
    """One comment on the card and one line on stderr per create, naming each word and its parent."""
    if not dropped:
        return
    from hermes_cli import kanban_db as kb
    named = ", ".join(f"{w} (from {p})" for w, p in dropped)
    text = (NOT_INHERITED_PREFIX + named + ". A Studio-only capability does not pass to a child: it "
            "runs on a pool host and is re-routed with a reason if it truly needs the Studio. To pin "
            "it, add `requires: <word> (<what only the Studio has>)` to the body.")
    try:
        kb.add_comment(conn, task_id, "dispatcher", text)
    except Exception as exc:  # the card is saved; the stderr line still says it
        kb._log.warning("requires-not-inherited comment for %s not written: %s", task_id, exc)
    print(f"kanban create: {task_id}: {NOT_INHERITED_PREFIX}{named}", file=sys.stderr)


def not_inherited_note(conn, task_id: str) -> Optional[str]:
    """The card's ``requires not inherited`` comment, or None: the kanban_create tool returns it
    so the creating agent sees it, not only stderr (t_41763e36)."""
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = 'dispatcher' "
        "AND substr(body, 1, ?) = ? ORDER BY id LIMIT 1",
        (task_id, len(NOT_INHERITED_PREFIX), NOT_INHERITED_PREFIX)).fetchone()
    return row[0] if row else None


def stored_refusal(body: Optional[str]) -> Optional[str]:
    """studio-requires-reason on the body to be STORED, or None (allowed / kill switch / fail open)."""
    from hermes_cli import kanban_access_bound_pin as kabp
    return kabp.studio_refusal(body)
