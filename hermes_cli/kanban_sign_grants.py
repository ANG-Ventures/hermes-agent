"""Scoped board-signing grants for same-model delegate children (t_19c567a8).

A ``delegate_task`` child is fenced off the board: its one write, a comment, is
stamped ``(subagent)`` so no operator-trust reader honours it. That is right by
default (one accountable actor per merge authorization) but it serialises every
review through the parent. A GRANT lets the parent hand a child a narrow,
expiring piece of its signing authority instead of its identity:

* minted by the parent at spawn (``delegate_task(sign_grant={...})``), only when
  the parent can PROVE the ``by`` identity itself (operator token, or the
  operator gateway with no dispatched worker in between), the child runs on the
  parent's model, and the child inherited the parent's context;
* one ``sign_grants`` row per card, on the board holding the cards, carrying the
  verbs, the expiry, the child's session fingerprint and the PRs the grant may
  never cover (the parent run's own PRs);
* a granted comment is stored as ``[grant:<id>] by (subagent)`` (:func:`granted_author`)
  with ``task_comments.grant_id`` set, after :func:`validate_comment` passed inside
  the insert's transaction, so no reader that trusts an author label honours it;
* the ONE promotion is :func:`operator_rows_with_live_grants`: a reader meant to
  honour live grants reads the rows it returns (author = ``by``) and nothing else;
  a revoked or expired grant's rows drop out, the way a head move voids a stamp.

Kill switch: ``kanban.sign_grants`` (board-home config first, then the profile);
anything but ``on`` refuses every mint and every granted write.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from typing import Iterable, Optional

#: Verbs a grant may carry. ``verdict``: a review note / ``prism-verdict:`` line /
#: non-verdict coverage record; ``approve``: an APPROVE coverage record;
#: ``request_changes``: a hold-verdict coverage record; ``release``: a
#: ``close-on:`` line. ``complete`` / ``defer`` are reserved for the lifecycle
#: verbs and are accepted in a grant, but no CLI verb consumes them yet.
VALID_VERBS = frozenset({"verdict", "approve", "request_changes", "release", "complete", "defer"})
DEFAULT_EXPIRES_MIN = 45
MAX_EXPIRES_MIN = 120
GRANT_ID_RE = re.compile(r"^g-[0-9a-f]{16}$")

# The ``sign_grants`` table lives in ``kanban_db.SCHEMA_SQL`` (created on every board init).

_HOLD = ("CHANGES_REQUESTED", "REQUEST_CHANGES", "CHANGES_NEEDED", "BLOCK", "BLOCKED", "REJECT", "REJECTED")
_APPROVE = ("APPROVE", "APPROVED")
_PRISM_VERDICT_RE = re.compile(r"prism-verdict\s*:", re.I)
_CLOSE_ON_RE = re.compile(r"close-on", re.I)


class GrantRefused(PermissionError):
    """A grant mint or a granted write failed validation; nothing was written."""


def enabled() -> bool:
    """``kanban.sign_grants == "on"`` for this board; any read error is off."""
    try:
        from hermes_cli.kanban_db import _kanban_review_setting

        value, _source = _kanban_review_setting("sign_grants", "off")
    except Exception:
        return False
    # YAML 1.1 reads a bare `on` as True: accept it, so the documented `sign_grants: on` works.
    return value is True or str(value).strip().lower() == "on"


def _norm_model(model: Optional[str]) -> str:
    """One normaliser for the grant model and the wire model, at mint and at every check, so a
    provider spelling (``anthropic/claude-opus-4.6`` vs ``claude-opus-4-6``) is not drift (Prism 9b929567c500)."""
    from agent.anthropic_message_convert import normalize_model_name

    return normalize_model_name(str(model or "").strip()).lower()


#: ``[grant:<id>] <by> (subagent)``: how a granted comment is STORED. The grant marker is a PREFIX, so
#: the label never starts with an operator scheme: a suffix left ``human:apollo (subagent) [grant:..]``
#: inside the ``human:`` namespace that ``is_operator_label`` and the close-on ``gate_setter`` trust by
#: ``startswith`` (Prism 779afdc7e72e). The `` (subagent)`` suffix stays, so readers keyed on it still
#: see a child's comment. Only :func:`operator_rows_with_live_grants` promotes it back to ``by``, and
#: only while the grant is live.
_GRANTED_AUTHOR_RE = re.compile(r"^\[grant:(?P<gid>g-[0-9a-f]{16})\] (?P<by>.+?) \(subagent\)$")
_GRANTED_AUTHOR_LIKE = "[grant:g-%] % (subagent)"


def granted_author(by: str, grant_id: str) -> str:
    """The stored author of a comment ``by`` signed under ``grant_id``."""
    from hermes_cli.kanban_db import SUBAGENT_AUTHOR_MARKER

    return f"[grant:{grant_id}] {str(by).strip()}{SUBAGENT_AUTHOR_MARKER}"


def parse_granted_author(author: Optional[str]) -> Optional[tuple]:
    """``(by, grant_id)`` for a grant-marked author, else None."""
    m = _GRANTED_AUTHOR_RE.match(str(author or "").strip())
    return (m.group("by"), m.group("gid")) if m else None


def _grant_live(row: Optional[sqlite3.Row], by: str, now: int) -> bool:
    return (row is not None and row["revoked_at"] is None and int(row["expires_at"]) > now
            and str(row["by_author"]).strip().lower() == str(by).strip().lower()
            and _norm_model(row["parent_model"]) == _norm_model(row["child_model"]))


def operator_rows_with_live_grants(conn: sqlite3.Connection, task_id: str, *,
                                   now: Optional[int] = None) -> list:
    """THE one promotion of granted comments (Apollo r8 ruling, Prism 568d804ef684).

    The ``task_comments`` rows of ``task_id`` stored under a grant-marked author whose grant
    :func:`get` reads live now (exists for this card, signs as that author, not revoked, not
    expired, same model), oldest first, each a dict with ``author`` set to the grant's ``by``
    and ``stored_author`` the marked label. A revoked/expired/unknown grant's rows are absent.
    A reader that is meant to honour live grants reads these; every other reader sees the
    marked author and honours nothing.
    """
    now = int(time.time()) if now is None else int(now)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_comments)").fetchall()}
    sel = "id, author, body, run_id, session_ref, created_at" + (", grant_id" if "grant_id" in cols else "")
    out = []
    for r in conn.execute(f"SELECT {sel} FROM task_comments WHERE task_id = ? AND author LIKE ? ORDER BY id",
                          (task_id, _GRANTED_AUTHOR_LIKE)):
        parsed = parse_granted_author(r["author"])
        if parsed is None:
            continue
        by, gid = parsed
        if "grant_id" in cols and r["grant_id"] != gid:
            continue
        if not _grant_live(get(conn, gid, task_id), by, now):
            continue
        row = {k: r[k] for k in r.keys()}
        row.update(author=by, stored_author=r["author"], grant_id=gid)
        out.append(row)
    return out


def _pr_keys(texts: Iterable[str]) -> set:
    """``owner/repo#N`` (lowercased) for every PR ref (URL or short form) in ``texts``."""
    from hermes_cli.kanban_pr_gate import parse_pr_refs

    return {f"{r.repo.lower()}#{r.number}" for t in texts for r in parse_pr_refs(str(t or ""), default_repo=None)}


def _run_meta_pr_keys(meta_text) -> set:
    from hermes_cli.kanban_open_pr import recorded_pr_refs

    try:
        meta = json.loads(meta_text) if meta_text else None
    except (TypeError, ValueError):
        meta = None
    return _pr_keys(recorded_pr_refs(meta))


def card_pr_keys(conn: sqlite3.Connection, task_id: str) -> set:
    """PRs any run on ``task_id`` recorded as the card's own (``owner/repo#N``)."""
    out: set = set()
    for (meta,) in conn.execute("SELECT metadata FROM task_runs WHERE task_id = ?", (task_id,)):
        out |= _run_meta_pr_keys(meta)
    return out


def authored_pr_keys(conn: sqlite3.Connection, profiles: Iterable[str], *, task_id: Optional[str] = None,
                     candidates: Iterable[str] = ()) -> set:
    """PRs a run of one of ``profiles`` recorded as its own: every such run on ``task_id``, plus any run on the
    board whose metadata names one of ``candidates`` (``owner/repo#N``). That is "the child or its parent run
    authored it": the child runs in its parent's profile, so the parent profile's runs are both."""
    profiles = sorted({str(p).strip() for p in profiles or () if str(p).strip()})
    if not profiles:
        return set()
    marks = ",".join("?" * len(profiles))
    out: set = set()
    if task_id:
        for (meta,) in conn.execute(
                f"SELECT metadata FROM task_runs WHERE task_id = ? AND profile IN ({marks})", (task_id, *profiles)):
            out |= _run_meta_pr_keys(meta)
    for key in sorted(set(candidates or ())):
        number = key.rsplit("#", 1)[-1]
        for (meta,) in conn.execute(
                f"SELECT metadata FROM task_runs WHERE profile IN ({marks}) AND metadata LIKE ?",
                (*profiles, f"%{number}%")):
            out |= _run_meta_pr_keys(meta) & {key}
    return out


def mint(
    conn: sqlite3.Connection,
    *,
    cards: Iterable[str],
    verbs: Iterable[str],
    by: str,
    child_session_ref: str,
    parent_model: str,
    child_model: str,
    parent_session_ref: Optional[str] = None,
    child_id: Optional[str] = None,
    expires_in_min: Optional[float] = None,
    parent_profiles: Iterable[str] = (),
    parent_task: Optional[str] = None,
    now: Optional[int] = None,
) -> dict:
    """Validate and write one grant (one row per card). Raises :class:`GrantRefused`.

    The caller (``delegate_task``) has already proven ``by`` and the inherited
    context; this checks everything the board can see.
    """
    if not enabled():
        raise GrantRefused("sign grants are off (kanban.sign_grants is not 'on')")
    cards = list(dict.fromkeys(str(c).strip() for c in cards or () if str(c).strip()))
    verbs = sorted({str(v).strip().lower() for v in verbs or () if str(v).strip()})
    if not cards:
        raise GrantRefused("a grant needs at least one card")
    if not verbs or set(verbs) - VALID_VERBS:
        raise GrantRefused(f"grant verbs must be a non-empty subset of {sorted(VALID_VERBS)}, got {verbs}")
    by = str(by or "").strip()
    if not by:
        raise GrantRefused("a grant needs a 'by' identity")
    if not child_session_ref:
        raise GrantRefused("a grant needs the child's session")
    if not _norm_model(parent_model) or _norm_model(parent_model) != _norm_model(child_model):
        raise GrantRefused(f"child model {child_model!r} != parent model {parent_model!r}; "
                           "a grant never goes to a different (downgraded) reviewer")
    minutes = DEFAULT_EXPIRES_MIN if expires_in_min in (None, "") else float(expires_in_min)
    if not (0 < minutes <= MAX_EXPIRES_MIN):
        raise GrantRefused(f"expires_in_min must be in (0, {MAX_EXPIRES_MIN}], got {expires_in_min!r}")
    parent_profiles = sorted({str(p).strip() for p in parent_profiles or () if str(p).strip()})
    if not parent_profiles:
        raise GrantRefused("a grant needs the parent's profile (its runs' PRs are never covered)")
    for card in cards:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (card,)).fetchone():
            raise GrantRefused(f"card {card} is not on this board")
        if parent_task and card == parent_task:
            raise GrantRefused(f"card {card} is the parent's own kanban card; a grant never covers it")
        own = authored_pr_keys(conn, parent_profiles, task_id=card)
        if own:
            raise GrantRefused(f"card {card} names PR(s) {sorted(own)} the parent run authored; "
                               "a grant never covers an own PR")
    now = int(time.time()) if now is None else int(now)
    gid = "g-" + secrets.token_hex(8)
    expires_at = now + int(minutes * 60)
    from hermes_cli.kanban_db import write_txn

    with write_txn(conn):
        for card in cards:
            conn.execute(
                "INSERT INTO sign_grants (grant_id, task_id, verbs, by_author, parent_session_ref, child_id, "
                "child_session_ref, parent_model, child_model, parent_profiles, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, card, json.dumps(verbs), by, parent_session_ref, child_id, child_session_ref,
                 str(parent_model), str(child_model), json.dumps(parent_profiles), now, expires_at),
            )
    return {"grant_id": gid, "cards": cards, "verbs": verbs, "by": by, "expires_at": expires_at,
            "child_id": child_id, "model": str(child_model)}


def revoke(conn: sqlite3.Connection, grant_id: str, *, now: Optional[int] = None) -> int:
    """Revoke every row of ``grant_id``; returns the rows changed. Voids its stamps.

    From inside a delegate child (its board connection is read-only) this is the one other
    write it may make, because it only narrows authority: the child revokes its OWN grant
    when its model moves away from the grant's. "Own" is the session executing this call, the
    same per-session identity :func:`mint` bound; never a caller-supplied ref, which a sibling
    child could copy off another child's row (Prism 27c3b03d5260).
    """
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    where, args = "grant_id = ? AND revoked_at IS NULL", [grant_id]
    if not kb._is_delegated_child():
        with kb.write_txn(conn):
            cards = _revoke_rows(conn, where, args, now)
    else:
        own_ref = executing_session_ref()
        if not own_ref:
            raise GrantRefused("a delegate child revokes only its own grant (no session bound)")
        if not conn.execute("SELECT 1 FROM sign_grants WHERE grant_id = ? AND child_session_ref = ?",
                            (grant_id, own_ref)).fetchone():
            raise GrantRefused(f"grant {grant_id} belongs to another session; a child revokes only its own")
        with kb.delegated_child_write(conn):
            with kb.write_txn(conn):
                cards = _revoke_rows(conn, where + " AND child_session_ref = ?", [*args, own_ref], now)
    # Journaled after commit, one record per card, so a snapshot + journal replay restores the grant
    # revoked instead of live (Prism a35d26840b79). Best-effort, like every journal write. Under the
    # board ``conn`` is open on, not the ambient one, since replay reads only the restored board's
    # journal (Prism c2baca2e9dd1).
    try:
        from hermes_cli import kanban_journal

        board = _conn_board_slug(conn)
        for card in cards:
            kanban_journal.append(board, card, "sign_grant_revoked",
                                  {"grant_id": grant_id, "revoked_at": now})
    except Exception:  # pragma: no cover - the journal is a net, never a gate
        pass
    return len(cards)


def _conn_board_slug(conn: sqlite3.Connection) -> Optional[str]:
    """The board whose database file ``conn`` is open on; the ambient board when none matches."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_board_registry import board_db_layout

    try:
        for slug, _path in board_db_layout():
            if kb._conn_is_board(conn, slug):
                return slug
    except Exception:
        pass
    return kb._journal_board_slug()


def _revoke_rows(conn: sqlite3.Connection, where: str, args: list, now: int) -> list:
    cards = [r[0] for r in conn.execute(f"SELECT task_id FROM sign_grants WHERE {where}", args)]
    conn.execute(f"UPDATE sign_grants SET revoked_at = ? WHERE {where}", [now, *args])
    return cards


def executing_session_ref() -> Optional[str]:
    """The board fingerprint of the session executing this call: the per-session identity
    ``delegated_child_context(child.session_id)`` binds and :func:`mint` stored. Grant ownership
    is judged against this, never a caller-supplied ref a sibling could copy off a readable row."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_identity import _resolve_current_session_id

    return kb.derive_session_ref(_resolve_current_session_id())


def get(conn: sqlite3.Connection, grant_id: str, task_id: str) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM sign_grants WHERE grant_id = ? AND task_id = ?", (grant_id, task_id)).fetchone()
    except sqlite3.OperationalError:   # no table yet: no grant exists
        return None


def _coverage_verdicts(body: str) -> list:
    # The kernel's one coverage parser: a record the board's readers honour (bare or
    # backtick-wrapped) is the same record the grant is checked against (Prism 9e7e6653e13e).
    from hermes_cli.kanban_db import _review_coverage_payload

    out = []
    for line in str(body or "").splitlines():
        payload = _review_coverage_payload(line)
        if payload is None:
            continue
        try:
            rec = json.loads(payload)
        except ValueError:
            rec = None
        out.append(rec if isinstance(rec, dict) else {})
    return out


# t_301365d1 (FLIP gate): the read side void-filters only review_coverage records, prism-verdict lines and
# close-on release lines. Every other operator line grammar is honoured on author alone, so a granted body may
# never carry one. Named grammars mirror their hermes-home readers, matched at least as broadly (anywhere in
# the body, through markdown decoration, with no left boundary: `-GATE-PROVEN:` / `xGATE-PROVEN:` are refused too,
# Prism d9a6097a446f), so an out-of-kind line is refused at the insert, never read later.
_DECOR = r"[ \t>*_`#~+\-]*"
_OUT_OF_KIND = (
    ("ACCEPTANCE-CLOSED", re.compile(r"ACCEPTANCE-(?:CLOSED|OPEN)[*_`]*[ \t]*:")),  # acceptance_marker
    ("GATE-PROVEN", re.compile(r"GATE-PROVEN[*_`]*[ \t]*:")),                     # close_contract
    ("gate_repair", re.compile(r"gate_repair[*_`]*[ \t]*:", re.I)),               # gate_repair
    ("ungraded-run", re.compile(r"ungraded-run[*_`]*[ \t]*:", re.I)),             # new_red_gate
    ("prism-scope", re.compile(r"prism-scope[*_`]*[ \t]*:", re.I)),               # prism-findings-route
    ("prism-ruling", re.compile(r"prism-ruling[*_`]*[ \t]*:", re.I)),             # prism_ruling
    ("OPERATOR OVERRIDE / CUT RULING",                                                      # round_cap_override,
     re.compile(r"OPERATOR (?:CUT RULING|OVERRIDE)\b")),                          # prism-findings-route
)
# Any other marker-shaped line leading a line: a multi-word `UPPER-TOKEN:` / `UPPER TOKEN:` (the shape of every
# operator marker above) or a `kebab-key:` / `snake_key:` (fixes-main-red:, ...), plus `deadman:`. Fail closed: a
# granted body is a review note, not a new grammar. Single-word prose labels (`P1:`, `NOTE:`, `Fix:`) stay allowed.
_GENERIC_MARKER = re.compile(r"^" + _DECOR + r"(?:[A-Z][A-Z0-9]*(?:[- ][A-Z][A-Z0-9]*)+|[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)"
                             r"[*_`]*[ \t]*:")
_ALLOWED_LINE = re.compile(r"^" + _DECOR + r"(?:prism-verdict|close-on)[*_`]*[ \t]*:", re.I)
_DEADMAN = re.compile(r"^" + _DECOR + r"deadman[*_`]*[ \t]*:", re.I | re.M)


def out_of_kind_line(body: str) -> Optional[tuple]:
    """``(kind, line)`` for the first line a granted body may not carry, else None.

    Allowed: review_coverage records (bare or backticked, the kernel's parser), ``prism-verdict:`` and
    ``close-on:`` lines, and prose. A named operator grammar is refused anywhere in the body, including
    inside an allowed line (``GATE-PROVEN:`` is matched unanchored by its reader).
    """
    from hermes_cli.kanban_db import _review_coverage_payload

    for line in str(body or "").splitlines():
        for kind, rx in _OUT_OF_KIND:
            if rx.search(line):
                return kind, line.strip()
        if _review_coverage_payload(line) is not None or _ALLOWED_LINE.match(line):
            continue
        if _DEADMAN.match(line) or _GENERIC_MARKER.match(line):
            return "operator marker", line.strip()
    return None


def body_verbs(body: str) -> set:
    """The verbs a comment body exercises (see :data:`VALID_VERBS`)."""
    verbs = set()
    for rec in _coverage_verdicts(body):
        v = str(rec.get("verdict") or "").strip().upper().replace("-", "_").replace(" ", "_")
        verbs.add("approve" if v in _APPROVE else "request_changes" if v in _HOLD else "verdict")
        if rec.get("close_on"):
            # A record's close_on is a release gate to close_on.release / latest, the same
            # authority as a `close-on:` line (Prism 1654e8f04fd7).
            verbs.add("release")
    if _PRISM_VERDICT_RE.search(body or ""):
        verbs.add("verdict")
    if _CLOSE_ON_RE.search(body or ""):
        verbs.add("release")
    return verbs or {"verdict"}


def body_pr_keys(body: str) -> set:
    """PRs the body's release authority could reach: a coverage record's ``pr`` and ``close_on``
    fields, and every ``close-on:`` line. A standalone release line naming the parent's own PR
    (recorded on another card) is an own-PR release too (Prism 6bfefb466a37)."""
    recs = _coverage_verdicts(body)
    texts = [str(rec.get(f) or "") for rec in recs for f in ("pr", "close_on")]
    texts += [line for line in str(body or "").splitlines() if _CLOSE_ON_RE.search(line)]
    return _pr_keys(texts)


def validate_comment(
    conn: sqlite3.Connection,
    grant_id: str,
    task_id: str,
    author: str,
    body: str,
    *,
    caller_session_ref: Optional[str],
    now: Optional[int] = None,
) -> sqlite3.Row:
    """The grant row that authorizes this comment, or :class:`GrantRefused`."""
    if not enabled():
        raise GrantRefused("sign grants are off (kanban.sign_grants is not 'on'); granted write refused")
    if not GRANT_ID_RE.fullmatch(str(grant_id or "")):
        raise GrantRefused(f"malformed grant id {grant_id!r}")
    row = get(conn, grant_id, task_id)
    if row is None:
        raise GrantRefused(f"grant {grant_id} does not cover card {task_id}")
    now = int(time.time()) if now is None else int(now)
    if row["revoked_at"] is not None:
        raise GrantRefused(f"grant {grant_id} was revoked")
    if int(row["expires_at"]) <= now:
        raise GrantRefused(f"grant {grant_id} expired")
    if _norm_model(row["parent_model"]) != _norm_model(row["child_model"]):
        raise GrantRefused(f"grant {grant_id}: child model != parent model")
    # Prism 28bafed528a0: ownership is the EXECUTING session's, as in revoke(); the caller's
    # ``session_ref`` is only shape-checked upstream, so a mismatching one is refused, never trusted.
    own_ref = executing_session_ref()
    if not own_ref or own_ref != row["child_session_ref"]:
        raise GrantRefused(f"grant {grant_id} belongs to another session")
    if caller_session_ref and caller_session_ref != own_ref:
        raise GrantRefused(f"grant {grant_id}: session_ref is not the executing session's; it belongs to another session")
    if str(author or "").strip().lower() != str(row["by_author"]).strip().lower():
        raise GrantRefused(f"grant {grant_id} signs as {row['by_author']!r}, not {author!r}")
    allowed = set(json.loads(row["verbs"] or "[]"))
    bad = out_of_kind_line(body)
    if bad:
        verbs = ",".join(sorted(body_verbs(body)))
        raise GrantRefused(f"grant {grant_id} (verb {verbs}) may carry only review_coverage, prism-verdict and "
                           f"close-on lines; refused {bad[0]} line: {bad[1][:120]!r}")
    missing = body_verbs(body) - allowed
    if missing:
        raise GrantRefused(f"grant {grant_id} does not carry verb(s) {sorted(missing)} (has {sorted(allowed)})")
    candidates = body_pr_keys(body)
    if "release" in body_verbs(body):
        # A bare `close-on: merged` names no PR: it releases the card on ITS PRs, so those are candidates
        # too, and a release whose PR cannot be established is refused (Prism 4fdc26d69c92).
        candidates |= card_pr_keys(conn, task_id)
        if not candidates:
            raise GrantRefused(f"grant {grant_id}: release on {task_id} names no PR and the card records none; "
                               "a granted release needs an established PR")
    own = authored_pr_keys(conn, json.loads(row["parent_profiles"] or "[]"), task_id=task_id,
                           candidates=candidates)
    if own:
        raise GrantRefused(f"grant {grant_id} never covers PR(s) {sorted(own)} its parent run authored")
    return row
