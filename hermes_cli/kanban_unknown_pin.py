"""An UNKNOWN ``host:`` pin is a refusal at dispatch (t_cb2b888d, t_229d6bca).

A ``host:<word>`` line that resolves to no host (a typo, a name the registry
does not carry) used to be dropped silently and the card went portable:
2026-10-11 t_cccdee67 (``host:mac-studio-m3u``) spilled to ace-ai and 403'd.
t_cb2b888d held such a card local-only with one comment; it then still RAN
locally, a silent fall-through for a card whose author meant another host.
A pin is a HARD constraint (t_229d6bca): the dispatcher now parks the card
``needs_input`` with one line naming the word and every accepted spelling,
and a comment carrying the same line. The fix is the author's: correct the
``host:`` line and unblock. A dry run only reports.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional

AUTHOR = "dispatcher"
_PREFIX = "unknown host pin: host:"


def accepted_pins(known_hosts: Iterable[str],
                  aliases: Optional[Mapping[str, str]] = None) -> Dict[str, List[str]]:
    """``{canonical pin: [every spelling that resolves to it]}``, sorted, the
    canonical first: ``any``, the Studio (``studio``) and each pool host."""
    from hermes_cli import kanban_worker_pool as _kwp
    table: Dict[str, str] = {w: _kwp.PIN_STUDIO for w in _kwp._studio_words()}
    table[_kwp.PIN_ANY] = _kwp.PIN_ANY
    table.update({h.lower(): h.lower() for h in (known_hosts or ())})
    table.update({k.lower(): v for k, v in (aliases or {}).items()})
    out: Dict[str, List[str]] = {}
    for word, canon in sorted(table.items()):
        out.setdefault(canon, [])
        if word != canon:
            out[canon].append(word)
    return {c: [c, *out[c]] for c in sorted(out)}


def refusal_line(word: str, known_hosts: Iterable[str],
                 aliases: Optional[Mapping[str, str]] = None) -> str:
    groups = accepted_pins(known_hosts, aliases)
    names = "; ".join(" / ".join(spellings) for spellings in groups.values())
    return (f"{_PREFIX}{word} names no host, so this card is refused at dispatch (a pin is a "
            f"hard constraint: never run elsewhere). Accepted spellings: {names}. "
            f"Fix the host: line, then unblock.")


def park_unknown_pins(conn, unknown: Mapping[str, str], *, dry_run: bool = False,
                      default_assignee: Optional[str] = None) -> list:
    """Park each ``{task_id: word}`` card ``needs_input`` with the refusal
    line; returns the ids parked. Fenced like the access park: only a card
    still ready+unclaimed, and only one this home would spawn."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    from hermes_cli import kanban_worker_pool as _kwp

    pending = {t: w for t, w in (unknown or {}).items() if w}
    _kbd._drop_foreign(conn, pending, default_assignee)
    if dry_run or not pending:
        return []
    fleet = _kb.kanban_home() / "fleet"
    known, aliases = _kwp.known_host_ids(fleet), _kwp.pin_aliases(fleet)
    parked = []
    for tid, word in pending.items():
        line = refusal_line(word, known, aliases)
        if _kb.block_task(conn, tid, reason=line, kind="needs_input", require_unclaimed_ready=True):
            _kb.add_comment(conn, tid, AUTHOR, line)
            _kb._log.warning("kanban dispatch: card %s: unknown host pin %s, refused (needs_input)",
                             tid, word)
            parked.append(tid)
        else:
            _kb._log.info("kanban dispatch: %s no longer ready+unclaimed; unknown-pin park skipped", tid)
    return parked
