"""Same-incident overlap gate: two sessions minting the same incident.

2026-09-30 18:0x PT: the #pr-judge and #prism Apollo sessions each minted a
pair of cards for ONE root cause (xAI HTTP 426 on cliproxy's Grok client
version) within five minutes (t_d979a494 + t_e6bb7b19 vs t_f1437191 +
t_aa47fad3). Four workers started on overlapping scope. The dispatcher's
collision warning keys on file paths in card bodies and the near-duplicate
guard on title+body Jaccard, so neither saw it: different repos, different
wording, same incident.

This gate compares a new card against cards created in the last
:data:`OVERLAP_WINDOW_SECONDS` on the same board by a DIFFERENT origin session
(a session's own fan-out is a deliberate split, not a double-mint) on
DISTINCTIVE tokens only:

* an HTTP status shared together with a model/lane name (``426`` + ``grok``);
* a quoted error string (>= :data:`QUOTE_MIN_CHARS` chars) the two cards share;
* a PR ref (``owner/repo#N``, ``repo#N``), a commit sha, or a card id both cite.

A hit never refuses the create. It writes an ``OVERLAPS t_x (score N)``
comment on BOTH cards, an ``overlap_detected`` event on both, a #logs line,
and holds the NEWER card in ready for :data:`OVERLAP_HOLD_SECONDS` (see
``check_respawn_guard`` reason ``overlap_hold``) so the minting session can
merge or withdraw it. Nothing is ever archived automatically.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from typing import Iterable, Optional

_log = logging.getLogger(__name__)

OVERLAP_WINDOW_SECONDS = 30 * 60
OVERLAP_HOLD_SECONDS = 10 * 60
OVERLAP_THRESHOLD = 3
OVERLAP_EVENT = "overlap_detected"
OVERLAP_COMMENT_AUTHOR = "kanban-overlap-gate"
QUOTE_MIN_CHARS = 40
_SHINGLE_WORDS = 6

# Weights, against OVERLAP_THRESHOLD. Calibrated on 7 days of the live board
# (3,737 cards, 91,859 same-30-min pairs, 2026-10-01): a rare status + lane or
# a shared quoted error flags alone; a PR ref, a sha, a card id or a common
# status needs company.
# A RARE status next to a lane name is an incident signature on its own (426 +
# grok); the everyday statuses (a 400/402/429 on some lane is background in a
# dozen cards a day) only add weight and need a second feature to flag.
_COMMON_STATUSES = frozenset({
    "400", "401", "402", "403", "404", "429", "500", "502", "503", "504",
})
_W_STATUS_LANE_RARE = 3
_W_STATUS_LANE_COMMON = 1
_W_STATUS_LANE_CAP = 4
_W_QUOTE = 3
_W_REF = 2               # per shared PR ref
_W_WEAK_REF = 1          # per shared sha / card id (umbrella cards and base
                         # commits get cited by unrelated work; needs company)
# Status and lane must sit within this many chars on one line to pair up.
_STATUS_LANE_SPAN = 120

# Model families and fleet lane names. A status code alone is too common
# ("500 reviews"); a status next to a lane name is the incident signature.
LANE_TOKENS = frozenset({
    "grok", "xai", "gpt", "codex", "openai", "claude", "anthropic", "opus",
    "sonnet", "haiku", "fable", "astra", "kimi", "moonshot", "gemini",
    "antigravity", "deepseek", "qwen", "glm", "zai", "minimax", "mistral",
    "llama", "apr", "apx", "bpr", "bpx", "cpr", "cpx", "alr", "dpx", "cpa",
    "cliproxy", "cliproxyapi", "openrouter", "copilot", "bedrock", "vertex",
    "ollama", "vllm",
})

# A 4xx/5xx not glued to an identifier, a version, a path, a PR '#', a line
# ':' or a percentage ("ensemble.py:346", "#426", "1.426", "450%").
_STATUS_RE = re.compile(r"(?<![\w.:#/$-])([45]\d\d)(?![\w%]|\.\d)")
_WORD_RE = re.compile(r"[a-z0-9]+")
_LANE_WORD_RE = re.compile(r"[a-z][a-z0-9]*")
_PR_REF_RE = re.compile(r"\b([A-Za-z0-9][\w.-]*(?:/[\w.-]+)?)#(\d{1,6})\b")
_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
# 7-40 hex with at least one digit and one letter; not part of a card id.
_SHA_RE = re.compile(r"(?<![\w-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w-])")
_QUOTE_RES = (
    re.compile(r'"([^"\n]{%d,})"' % QUOTE_MIN_CHARS),
    re.compile(r"`([^`\n]{%d,})`" % QUOTE_MIN_CHARS),
    re.compile(r"“([^”\n]{%d,})”" % QUOTE_MIN_CHARS),
    # Single quotes only at word edges, so "don't ... it's" is not a quote.
    re.compile(r"(?<![\w'])'([^'\n]{%d,}?)'(?![\w'])" % QUOTE_MIN_CHARS),
)
# A quoted span counts as an ERROR string only when it reads like one; quoted
# commands, paths and titles are shared by unrelated cards all the time
# (measured: 192 of 493 false hits over 7 days were quoted non-errors).
_ERROR_MARKER_RE = re.compile(
    r"\b(error|errors|failed|fails|failure|refused|refuses|outdated|denied|"
    r"invalid|unsupported|exceeded|not found|timed out|timeout|cannot|can't|"
    r"unable|forbidden|unauthori[sz]ed|rejected|exception|traceback|fatal|"
    r"please update|deprecated|no longer|is not allowed|overloaded|"
    r"exhausted|draw from)\b",
    re.IGNORECASE,
)
_ORIGIN_SESSION_RE = re.compile(r"\bsession\s+(\d{8}_\d{6}_[0-9a-f]{6,})")


def _strip_origin(body: Optional[str]) -> str:
    """Body without its ``origin:`` provenance lines (chat ids, msg ids)."""
    return "\n".join(
        line for line in (body or "").splitlines()
        if not line.strip().lower().startswith("origin:")
    )


def origin_session(body: Optional[str], session_id: Optional[str]) -> Optional[str]:
    """The session that MINTED the card: the origin line's ``session <id>``.

    The ``session_id`` column is the card's current home and moves on a
    takeover/restamp (all four 09-30 cards ended up homed on one session), so
    the birth line is the better witness; the column is the fallback.
    """
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("origin:"):
            m = _ORIGIN_SESSION_RE.search(line)
            if m:
                return m.group(1)
        break
    return (session_id or "").strip() or None


def _shingles(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < _SHINGLE_WORDS:
        return {" ".join(words)} if words else set()
    return {
        " ".join(words[i:i + _SHINGLE_WORDS])
        for i in range(len(words) - _SHINGLE_WORDS + 1)
    }


def _status_lane_pairs(text: str) -> set[tuple[str, str]]:
    """``(status, lane)`` pairs that sit close together on one line."""
    pairs: set[tuple[str, str]] = set()
    for line in text.splitlines():
        statuses = [(m.start(), m.group(1)) for m in _STATUS_RE.finditer(line)]
        if not statuses:
            continue
        low = line.lower().replace("-", " ").replace("_", " ")
        lanes = [
            (m.start(), m.group(0)) for m in _LANE_WORD_RE.finditer(low)
            if m.group(0) in LANE_TOKENS
        ]
        for spos, status in statuses:
            for lpos, lane in lanes:
                if abs(spos - lpos) <= _STATUS_LANE_SPAN:
                    pairs.add((status, lane))
    return pairs


def features(title: Optional[str], body: Optional[str]) -> dict:
    """Distinctive tokens of one card (origin lines excluded)."""
    text = f"{title or ''}\n{_strip_origin(body)}"
    lower = text.lower()
    quotes: set[str] = set()
    for line in text.splitlines():
        for rx in _QUOTE_RES:
            for m in rx.finditer(line):
                if _ERROR_MARKER_RE.search(m.group(1)):
                    quotes |= _shingles(m.group(1))
    refs: set[str] = set()
    for repo, num in _PR_URL_RE.findall(text):
        refs.add(f"pr:{repo.split('/')[-1].lower()}#{num}")
    for repo, num in _PR_REF_RE.findall(text):
        refs.add(f"pr:{repo.split('/')[-1].lower()}#{num}")
    cards = set(_CARD_RE.findall(lower))
    shas = set(_SHA_RE.findall(_CARD_RE.sub(" ", lower)))
    weak = {f"sha:{s[:7]}" for s in shas} | {f"card:{c}" for c in cards}
    return {
        "status_lanes": _status_lane_pairs(text),
        "quotes": quotes,
        "refs": refs,
        "weak_refs": weak,
    }


def score_pair(a: dict, b: dict, *, exclude_cards: Iterable[str] = ()) -> tuple[int, list[str]]:
    """``(score, reasons)`` for two :func:`features` dicts."""
    score = 0
    reasons: list[str] = []
    shared_sl = a["status_lanes"] & b["status_lanes"]
    if shared_sl:
        statuses = sorted({s for s, _ in shared_sl})
        lanes = sorted({l for _, l in shared_sl})
        score += min(
            sum(
                _W_STATUS_LANE_COMMON if s in _COMMON_STATUSES else _W_STATUS_LANE_RARE
                for s in statuses
            ),
            _W_STATUS_LANE_CAP,
        )
        reasons.append(f"HTTP {'/'.join(statuses)} + {'/'.join(lanes)}")
    shared_quotes = a["quotes"] & b["quotes"]
    if shared_quotes:
        score += _W_QUOTE
        reasons.append(f'quoted error "...{max(shared_quotes, key=len)}..."')
    shared_refs = sorted(a["refs"] & b["refs"])
    if shared_refs:
        score += _W_REF * len(shared_refs)
    skip = {f"card:{c}" for c in exclude_cards}
    shared_weak = sorted((a["weak_refs"] & b["weak_refs"]) - skip)
    if shared_weak:
        score += _W_WEAK_REF * len(shared_weak)
    named = shared_refs + shared_weak
    if named:
        reasons.append("shared " + ", ".join(r.split(":", 1)[1] for r in named[:4]))
    return score, reasons


def _linked_ids(conn, task_id: str, parents: Iterable[str]) -> set[str]:
    """Parents of the new card plus every card sharing a parent with it."""
    ids = {p for p in parents if p}
    if ids:
        ph = ",".join("?" * len(ids))
        for row in conn.execute(
            f"SELECT child_id FROM task_links WHERE parent_id IN ({ph})", tuple(ids)
        ):
            ids.add(row[0])
    for row in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? "
        "UNION SELECT child_id FROM task_links WHERE parent_id = ?",
        (task_id, task_id),
    ):
        ids.add(row[0])
    return ids


def find_overlaps(
    conn,
    *,
    task_id: Optional[str],
    title: str,
    body: Optional[str],
    session_id: Optional[str],
    tenant: Optional[str],
    now: int,
    parents: Iterable[str] = (),
    window_seconds: int = OVERLAP_WINDOW_SECONDS,
    threshold: int = OVERLAP_THRESHOLD,
) -> list[dict]:
    """Cards from another origin session in the window that share the incident."""
    mine = origin_session(body, session_id)
    feats = features(title, body)
    if not any(feats.values()):
        return []
    linked = _linked_ids(conn, task_id or "", parents)
    rows = conn.execute(
        "SELECT id, title, body, session_id, created_at FROM tasks "
        "WHERE created_at >= ? AND status != 'archived' AND tenant IS ? AND id != ?",
        (int(now) - int(window_seconds), tenant, task_id or ""),
    ).fetchall()
    hits: list[dict] = []
    for row in rows:
        if row["id"] in linked:
            continue
        theirs = origin_session(row["body"], row["session_id"])
        if mine and theirs and mine == theirs:
            continue  # a session's own fan-out is a deliberate split
        score, reasons = score_pair(
            feats, features(row["title"], row["body"]),
            exclude_cards=(row["id"], task_id or ""),
        )
        if score >= threshold:
            hits.append({
                "id": row["id"], "score": score, "reasons": reasons,
                "created_at": int(row["created_at"] or 0),
            })
    hits.sort(key=lambda h: (-h["score"], -h["created_at"]))
    return hits


def record_overlaps(conn, task_id: str, hits: list[dict], *, now: int, append_event, add_comment) -> None:
    """Comments + events on BOTH cards. Caller holds the write txn."""
    hold_until = int(now) + OVERLAP_HOLD_SECONDS
    for h in hits[:3]:
        why = "; ".join(h["reasons"])
        add_comment(
            conn, task_id, OVERLAP_COMMENT_AUTHOR,
            f"OVERLAPS {h['id']} (score {h['score']}: {why}). Same incident minted "
            f"by another session in the last {OVERLAP_WINDOW_SECONDS // 60} min. "
            f"This newer card is held in ready until "
            f"{_hhmm(hold_until)} so the minting session can merge or withdraw "
            f"it. Worker: read {h['id']} first and post your scope split.",
        )
        add_comment(
            conn, h["id"], OVERLAP_COMMENT_AUTHOR,
            f"OVERLAPS {task_id} (score {h['score']}: {why}). A newer card from "
            f"another session looks like the same incident; it is held in ready "
            f"until {_hhmm(hold_until)}. Worker: read {task_id} and post your "
            f"scope split.",
        )
        append_event(conn, h["id"], OVERLAP_EVENT, {
            "overlaps": [{"id": task_id, "score": h["score"], "reasons": h["reasons"]}],
            "newer": task_id,
        })
    append_event(conn, task_id, OVERLAP_EVENT, {
        "overlaps": [
            {"id": h["id"], "score": h["score"], "reasons": h["reasons"]} for h in hits[:3]
        ],
        "hold_until": hold_until,
    })


def overlap_hold_until(conn, task_id: str, requeue_kinds: Iterable[str]) -> Optional[int]:
    """``hold_until`` of the card's newest overlap hold, unless an operator
    requeue verb landed after it (the session decided: run it).

    Only hold-bearing events count. A held card that a later card matches
    gets an informational ``overlap_detected`` event (no ``hold_until``);
    that event must not mask the hold (Prism P1 00beb1de4c64)."""
    import json

    row = None
    until = None
    for cand in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC",
        (task_id, OVERLAP_EVENT),
    ):
        try:
            value = json.loads(cand["payload"] or "{}").get("hold_until")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            row, until = cand, value
            break
    if row is None or until is None:
        return None
    kinds = tuple(requeue_kinds)
    if kinds:
        ph = ",".join("?" * len(kinds))
        later = conn.execute(
            f"SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ({ph}) LIMIT 1",
            (task_id, row["id"], *kinds),
        ).fetchone()
        if later is not None:
            return None
    return int(until)


def _hhmm(ts: int) -> str:
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))


def notify_logs(board: Optional[str], task_id: str, hits: list[dict]) -> None:
    """One #logs line per create that overlapped. Fire-and-forget: a create
    never waits on Discord; a hermetic home (no notify.py) is a no-op."""
    try:
        from . import kanban_budget as _kb

        script = _kb._notify_script_path()
        if script is None or not hits:
            return
        pairs = ", ".join(f"{h['id']} ({h['score']})" for h in hits[:3])
        body = (
            f"🔁 Kanban '{board or 'default'}': {task_id} OVERLAPS {pairs}: "
            f"{'; '.join(hits[0]['reasons'])}. Newer card held in ready "
            f"{OVERLAP_HOLD_SECONDS // 60} min; merge or withdraw one."
        )
        subprocess.Popen(
            [sys.executable, script, "--channel", "discord",
             "--target", _kb.RECOVERY_TARGET, "--send", body],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as exc:  # best-effort by contract
        _log.warning("kanban overlap #logs notify failed (%s: %s)", type(exc).__name__, exc)
