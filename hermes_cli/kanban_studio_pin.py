"""Entry validation and durable notices for bare Studio pins."""
from __future__ import annotations

import logging
import re
import sys

from hermes_cli import kanban_worker_pool as pool

_FIRST_LINE = re.compile(r"^[ \t]*host:[ \t]*(?:studio|mac-studio[\w-]*)[ \t]*$", re.IGNORECASE)
_STUDIO_NEEDS = frozenset({"studio_relay", "studio_launchd", "vault", "skills_tree", "studio_tree"})
NOTICE_PREFIX = "[placement] bare host: studio dropped: add requires: "
CP_PROSE_PREFIX = "[placement] control-plane-read not pinned: "
HR_PROSE_PREFIX = "[placement] host_reach not pinned: "
# Cheap gate before the pool read: every ssh shape host_bound_targets reads has one of these (or _SCP_RE).
_SSH_SHAPE_HINT = re.compile(r"ssh|scp|rsync|sftp|mosh|-H", re.IGNORECASE)
_log = logging.getLogger(__name__)
_REQUIRES_LINE = re.compile(r"^([ \t]*requires:[ \t]*)(.*?)([ \t]*\r?)$", re.IGNORECASE | re.MULTILINE)
# hermes-home studio-requires-reason (create_refusal): a Studio-only word needs >= 3 words after the note cut.
_NOTE_CUT = re.compile(r"\s(?:—|–|--|#)|[(#]")
_NOTE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'./~_-]*")
_NOTE_MIN_WORDS = 3


def requires_line(word, note):
    """``requires: <word> (<note>)``: a line the writer's own reason keeps past create_refusal (t_d0852f2c)."""
    return f"requires: {word} ({note})"


def add_requires_word(body, word, note):
    """Put ``word`` first on the body's first ``requires:`` line; give the line ``note`` when it has no reason."""
    def one(m):
        value = word + (", " + m.group(2) if m.group(2) else "")
        cut = _NOTE_CUT.split(value, maxsplit=1)
        if len(cut) < 2:
            value += f" ({note})"
        elif len(_NOTE_WORD.findall(cut[1])) < _NOTE_MIN_WORDS:
            value += f"; {note}"
        return m.group(1) + value + m.group(3)
    return _REQUIRES_LINE.sub(one, body, count=1)


def prepare_body(body, notices=None):
    """Validate against the dispatcher's vocabulary; unknown maps retain the existing safe-local behavior."""
    if notices is not None and body:
        prose = pool.control_plane_prose(body)
        if prose:
            notices.append(ControlPlaneProse(prose))
        hosts = host_reach_prose(body)
        if hosts:
            notices.append(HostReachProse(hosts))
    # Saved bodies carry provenance before the caller's placement directives.
    content = pool.without_header_lines(body).lstrip("\r\n")
    if not content or not _FIRST_LINE.fullmatch(content.splitlines()[0]):
        return body
    from hermes_cli.config import load_config
    mode = load_config().get("kanban", {}).get("studio_pin_requires_reason", "notice")
    if mode is False or mode == "off":
        return body
    from hermes_cli import kanban_db as kb, kanban_host_capabilities as caps
    from hermes_cli import kanban_studio_capability as ksc
    reasons = ksc.create_reasons(caps.load_map(kb.kanban_home() / "fleet"))  # dispatch's vocabulary (t_9965b8d5)
    if reasons is None or pool.studio_pin_justified(body, reasons):
        return body
    needs = pool.card_needs(body) & _STUDIO_NEEDS
    if needs and "studio-state" in reasons:
        note = "auto: card names " + ", ".join(sorted(needs))
        # One line, including when the caller already declared unrelated requirements.
        if _REQUIRES_LINE.search(body):
            return add_requires_word(body, "studio-state", note)
        # After the host directive itself: a provenance header may precede it on a stored body.
        lines = body.splitlines(keepends=True)
        at = next(i for i, ln in enumerate(lines) if _FIRST_LINE.fullmatch(ln.rstrip("\r\n")))
        host = lines[at] if lines[at].endswith("\n") else lines[at] + "\n"
        return "".join(lines[:at]) + host + requires_line("studio-state", note) \
            + ("\n" if at + 1 < len(lines) else "") + "".join(lines[at + 1:])
    if mode is not True and mode != "refuse":
        if notices is not None:
            notices.append(reasons)
        return body
    raise ValueError(
        "bare host: studio requires a Studio-served reason; accepted words: "
        + ", ".join(sorted(reasons))
        + ". Add a line `requires: <word>` or remove the host: studio line."
    )


class ControlPlaneProse(tuple):
    """A create/edit notice: the body names live control-plane files without ``requires: control-plane-read``."""


class HostReachProse(tuple):
    """A create/edit notice: the body addresses fleet aliases in an ssh shape without a ``reach:`` line."""


def host_reach_prose(body):
    """Aliases the body addresses in an ssh shape with no ``reach:`` line (t_efd1497a); () on any read fault."""
    # ``alias:~/path`` / ``alias:path`` is read by the same regex host_bound_targets uses (Prism 703b3483074b).
    if not body or not (_SSH_SHAPE_HINT.search(body) or pool._SCP_RE.search(body)):
        return ()
    try:
        from hermes_cli.kanban_create_placement import _pool_config
        cfg = _pool_config()
        hosts = tuple(getattr(cfg, "hosts", ()) or ())
        aliases = (*(getattr(cfg, "reach_aliases", ()) or ()), *(h.lower() for h in hosts))
        return pool.host_reach_prose(body, aliases, pin_hosts=hosts, ssh_hosts=getattr(cfg, "ssh_hosts", None))
    except Exception:  # noqa: BLE001 - a notice must never fail a create
        _log.debug("host_reach prose read failed", exc_info=True)
        return ()


def host_reach_prose_text(aliases) -> str:
    """The notice (t_efd1497a): names the alias and the fix."""
    names = ", ".join(f"`{a}`" for a in aliases)
    return (HR_PROSE_PREFIX + "the body addresses " + names + " in an ssh shape but has no `reach:` line, so it "
            "does not hold the card for a host that can ssh there. If the work must ssh to it, add the line `reach: "
            + ", ".join(aliases) + "`; otherwise nothing to do (an example or an instruction for a human stays prose).")


def control_plane_prose_text(paths) -> str:
    """The notice (t_b720fdea): names the file and both fixes."""
    return (CP_PROSE_PREFIX + "the body names " + ", ".join(f"`{p}`" for p in paths)
            + " but declares no control-plane capability, so it runs on a pool host, which reads Studio files over "
              "`ssh studio` (sqlite3 -readonly, cat, tail). If the work must open that file ON the Studio, add the "
              "line `requires: control-plane-read`; otherwise nothing to do (writing \"the board database\" "
              "instead of the path also silences this notice).")


def report_notices(conn, card_id, notices):
    """Report only after a successful write, sharing dispatch's durable dedup key."""
    for reasons in notices:
        if isinstance(reasons, HostReachProse):
            text = host_reach_prose_text(reasons)
            try:
                _note_once(conn, card_id, HR_PROSE_PREFIX, text)
            except Exception as exc:  # the card is already saved; the stderr line still tells the writer
                _log.warning("host_reach notice for %s not written: %s", card_id, exc)
            print("Warning: " + text, file=sys.stderr)
            continue
        if isinstance(reasons, ControlPlaneProse):
            text = control_plane_prose_text(reasons)
            try:
                _note_once(conn, card_id, CP_PROSE_PREFIX, text)
            except Exception as exc:  # the card is already saved; the stderr line still tells the writer
                _log.warning("control-plane notice for %s not written: %s", card_id, exc)
            print("Warning: " + text, file=sys.stderr)
            continue
        try:
            note_drop(conn, card_id, reasons)
        except Exception as exc:  # the card is already saved; the dispatcher posts the notice later
            _log.warning("placement notice for %s not written: %s", card_id, exc)
        print("Warning: bare host: studio accepted without a Studio-served reason; "
              "accepted words: " + ", ".join(sorted(reasons))
              + ". Add `requires: <word>` or remove the host: studio line.", file=sys.stderr)


def note_drop(conn, card_id, reasons):
    """Persist one notice per card, not one per dispatcher process or retry."""
    _note_once(conn, card_id, NOTICE_PREFIX, NOTICE_PREFIX + ", ".join(sorted(reasons or ())))


def _note_once(conn, card_id, prefix, text):
    """One dispatcher comment per card per ``prefix``."""
    from hermes_cli import kanban_db as kb
    exists = ("SELECT 1 FROM task_comments WHERE task_id=? AND substr(body,1,?)=? LIMIT 1",
              (card_id, len(prefix), prefix))
    # Dispatch re-routes dropped pins every tick: an existing notice must not cost the board's write lock.
    if conn.execute(*exists).fetchone():
        return
    with kb.write_txn(conn):
        if conn.execute(*exists).fetchone():
            return
        kb.add_comment(conn, card_id, "dispatcher", text)
