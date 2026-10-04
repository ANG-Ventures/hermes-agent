"""``hermes kanban session-closeout <sid>``: one Obsidian-ready closeout for a session's cards.

Why (t_2d022d30, 2026-10-04): a 10-day orchestrator session had 911 cards across 17 boards,
and every "drive it to completion" pass meant hand-assembling the same census from kanban
SQL, PR reads, blackbox and comment greps. This verb does it in one read-only pass.

Sections:
  * header accounting: count(*) + GROUP BY status per board (W8-4 shape), parts sum to N or
    the line says ACCOUNTING MISMATCH; blocked split into blocked / external (EXTERNAL:/PARKED:).
  * every wave/round with every card and its outcome (PRs + state, live proof, retractions).
    Every card of the session appears exactly once; ``cards_listed`` in the JSON proves it.
  * standing rulings: 'Ace ruled' / 'Ace MM-DD HH:MM' / 'Ace YYYY-MM-DD HH:MM' in bodies and
    comments, deduped by stamp.
  * open remainder grouped by what flips it.
  * cost: the session's own state.db row + every blackbox turns.db (root + profiles) for the
    worker turns, which key ``chat_id`` on the card id.
  * incident ledger: every comment saying 'root cause'.
  * skills/docs named by the cards (skills/, plans/, vault paths), with on-disk existence.
  * ang-closeout gate list, PASS/FAIL per row. No machine evidence = FAIL (ang-closeout rule).

Read-only: board DBs, state.db and turns.db are opened ``mode=ro``. GitHub is read with REST
(``gh api``), never GraphQL; ``--no-network`` skips it and marks PR state UNREAD.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

from hermes_cli import kanban_open_pr as _opr
from hermes_cli import kanban_pr_gate as _prg

TERMINAL = ("done", "archived", "cancelled")
STATUS_ORDER = ("done", "archived", "cancelled", "review", "running", "blocked", "external",
                "ready", "todo", "triage", "scheduled")

# Title tags: "W8-4: …", "r12 D: …", "noise r3: …", "HARDEN 7: …", "[x] W3-2 …".
_TITLE_TAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:noise\s+)?(?:W(\d+)-\d+|r(\d+)\b|(HARDEN)\s*\d+)", re.I)
# Body origin line: "… 'boil the ocean' wave 7 …", "… Round 13. Skill …".
_BODY_TAG = re.compile(r"\b[Ww]ave\s+(\d+)\b|\b[Rr]ound\s+(\d+)\b")
_BODY_TAG_WINDOW = 600
# Automation-minted lanes, grouped by their title prefix instead of left in "untagged".
_AUTO_PREFIX = re.compile(r"^\s*(Prism P\d wake|FleetReview P\d(?: wake)?|rebase |crons\.ace fail)", re.I)

_RULING = re.compile(
    r"Ace ruled\b[^\n]{0,220}"
    r"|Ace (?:20\d\d-)?\d\d-\d\d \d\d:\d\d[^\n]{0,220}"
)
_STAMP = re.compile(r"Ace (?:ruled\b.*?)?((?:20\d\d-)?\d\d-\d\d \d\d:\d\d)")
_ROOT_CAUSE = re.compile(r"root[ -]cause", re.I)
_RETRACT = re.compile(r"\bCORRECTION\b|\bretract(?:ed|ion|s)?\b|\bFALSE-DONE\b", re.I)
_PROOF = re.compile(r"live-evidence:\s*yes|\b(?:live|native) proof\b", re.I)
_CLOSE_ON = re.compile(r"close-on:\s*([^\n]{1,200})", re.I)
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
_CLOSE_RECORD = re.compile(r"SUPERSED|\bCLOSED:|\bclosed\b", re.I)
_DOC_PATH = re.compile(
    r"(?<![\w/.-])((?:skills-shared|skills|plans|docs)/[\w./@+-]+?\.(?:md|html)"
    r"|(?:AI|Engineering|Runbooks)/[^\n`'\"|*<>]{1,160}?\.md)"
)

PrQuery = Callable[[str, int], Optional[str]]


# --------------------------------------------------------------------------- sources

def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def board_dbs(root: Path) -> list[tuple[str, Path]]:
    out = [("default", root / "kanban.db")]
    boards = root / "kanban" / "boards"
    if boards.is_dir():
        for child in sorted(boards.iterdir(), key=lambda p: p.name.lower()):
            if child.name not in ("default", "_archived"):
                out.append((child.name, child / "kanban.db"))
    return [(s, p) for s, p in out if p.is_file() and p.stat().st_size > 0]


def _ph(n: int) -> str:
    return ",".join("?" * n)


def load_cards(root: Path, sids: list[str]) -> tuple[list[dict], list[str]]:
    """Every card whose session_id is in ``sids`` on every board, with comments, runs and
    the latest block reason. Returns (cards, unreadable board slugs)."""
    cards, bad = [], []
    for slug, path in board_dbs(root):
        try:
            conn = _ro(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE session_id IN ({_ph(len(sids))}) ORDER BY created_at, id",
                sids).fetchall()
            if not rows:
                conn.close()
                continue
            ids = [r["id"] for r in rows]
            comments = collections.defaultdict(list)
            for r in conn.execute(f"SELECT task_id, author, body, created_at FROM task_comments "
                                  f"WHERE task_id IN ({_ph(len(ids))}) ORDER BY id", ids):
                comments[r["task_id"]].append(dict(r))
            runs = collections.defaultdict(list)
            for r in conn.execute(f"SELECT task_id, summary, metadata, outcome FROM task_runs "
                                  f"WHERE task_id IN ({_ph(len(ids))}) ORDER BY id", ids):
                runs[r["task_id"]].append(dict(r))
            blocked = {}
            for r in conn.execute(f"SELECT task_id, payload FROM task_events WHERE kind='blocked' "
                                  f"AND task_id IN ({_ph(len(ids))}) ORDER BY id", ids):
                try:
                    blocked[r["task_id"]] = str((json.loads(r["payload"] or "{}") or {}).get("reason") or "")
                except (TypeError, ValueError):
                    blocked[r["task_id"]] = ""
            for r in rows:
                d = dict(r)
                d["board"] = slug
                d["comments"] = comments.get(d["id"], [])
                d["runs"] = runs.get(d["id"], [])
                d["block_reason"] = blocked.get(d["id"], "")
                cards.append(d)
            conn.close()
        except sqlite3.Error:
            bad.append(slug)
    return cards, bad


def header_counts(root: Path, sids: list[str]) -> tuple[dict, int, dict]:
    """ONE count(*) + GROUP BY status query per board, independent of :func:`load_cards`."""
    by, total, per = collections.Counter(), 0, {}
    for slug, path in board_dbs(root):
        try:
            with _ro(path) as conn:
                rows = conn.execute(
                    "SELECT coalesce(status,'NULL'), count(*), sum(count(*)) OVER () FROM tasks "
                    f"WHERE session_id IN ({_ph(len(sids))}) GROUP BY status", sids).fetchall()
        except sqlite3.Error:
            continue
        if rows:
            per[slug] = rows[0][2]
            total += rows[0][2]
            for st, n, _ in rows:
                by[st] += n
    return dict(by), total, per


def session_rows(root: Path, sids: list[str]) -> list[dict]:
    path = root / "state.db"
    if not path.is_file():
        return []
    try:
        with _ro(path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT id, started_at, ended_at, chat_id, estimated_cost_usd, input_tokens, output_tokens, "
                f"cache_read_tokens, cache_write_tokens FROM sessions WHERE id IN ({_ph(len(sids))})", sids)]
    except sqlite3.Error:
        return []


def turns_dbs(root: Path) -> list[tuple[str, Path]]:
    out = [("default", root / "blackbox" / "turns.db")]
    profiles = root / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            out.append((p.name, p / "blackbox" / "turns.db"))
    return [(n, p) for n, p in out if p.is_file() and p.stat().st_size > 0]


_COST_COLS = ("count(*), coalesce(sum(cost_usd),0), coalesce(sum(input_tokens),0), "
              "coalesce(sum(output_tokens),0), coalesce(sum(cache_read),0), coalesce(sum(cache_write),0)")


def worker_cost(root: Path, card_ids: list[str]) -> dict:
    """Blackbox turns whose chat_id is a card id (kanban worker turns), per profile."""
    per, tot = {}, [0, 0.0, 0, 0, 0, 0]
    for name, path in turns_dbs(root):
        try:
            with _ro(path) as conn:
                row = [0, 0.0, 0, 0, 0, 0]
                for i in range(0, len(card_ids), 900):
                    chunk = card_ids[i:i + 900]
                    r = conn.execute(f"SELECT {_COST_COLS} FROM turns WHERE chat_id IN ({_ph(len(chunk))})",
                                     chunk).fetchone()
                    row = [a + b for a, b in zip(row, r)]
        except sqlite3.Error:
            continue
        if row[0]:
            per[name] = row
            tot = [a + b for a, b in zip(tot, row)]
    return {"per_profile": per, "total": tot}


def chat_cost(root: Path, sess: dict) -> list:
    """Blackbox turns in the session's chat between its start and end (the orchestrator's turns)."""
    if not sess.get("chat_id"):
        return [0, 0.0, 0, 0, 0, 0]
    end = sess.get("ended_at") or 9e12
    tot = [0, 0.0, 0, 0, 0, 0]
    for _name, path in turns_dbs(root):
        try:
            with _ro(path) as conn:
                r = conn.execute(f"SELECT {_COST_COLS} FROM turns WHERE chat_id=? AND ts_end>=? AND ts_end<?",
                                 (str(sess["chat_id"]), sess["started_at"], end)).fetchone()
        except sqlite3.Error:
            continue
        tot = [a + b for a, b in zip(tot, r)]
    return tot


# --------------------------------------------------------------------------- PR state

def gh_pr_state(repo: str, number: int) -> Optional[str]:
    """REST PR state OPEN / CLOSED / MERGED, or None when it cannot be read."""
    try:
        proc = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{number}",
                               "--jq", "[.state, (.merged_at != null)] | @tsv"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or "\t" not in proc.stdout:
        return None
    state, merged = proc.stdout.strip().split("\t")
    return "MERGED" if merged == "true" else state.upper()


def gh_repo_states(repo: str, wanted: set, max_pages: int = 30) -> dict:
    """Page ``pulls?state=all`` newest-first until every wanted number is seen (one call per
    100 PRs instead of one per PR)."""
    out, low = {}, min(wanted)
    for page in range(1, max_pages + 1):
        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=100&page={page}",
                 "--jq", ".[] | [.number, .state, (.merged_at != null)] | @tsv"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            break
        if proc.returncode != 0:
            break
        lines = [ln.split("\t") for ln in proc.stdout.splitlines() if ln.count("\t") == 2]
        if not lines:
            break
        for n, state, merged in lines:
            out[int(n)] = "MERGED" if merged == "true" else state.upper()
        if int(lines[-1][0]) <= low or wanted <= set(out):
            break
    return out


def resolve_pr_states(refs: Iterable[tuple[str, int]], query: Optional[PrQuery] = None,
                      single_cap: int = 25) -> dict:
    """{(repo, n): state} for every ref; None = unreadable. Repos with many refs are paged."""
    by_repo = collections.defaultdict(set)
    for repo, n in refs:
        by_repo[repo].add(n)
    out = {}
    for repo, nums in sorted(by_repo.items()):
        if query is None and len(nums) > single_cap:
            got = gh_repo_states(repo, nums)
            for n in nums:
                out[(repo, n)] = got.get(n)
            missing = [n for n in nums if out[(repo, n)] is None][:single_cap]
        else:
            missing = sorted(nums)
        for n in missing:
            out[(repo, n)] = (query or gh_pr_state)(repo, n)
    return out


# --------------------------------------------------------------------------- per-card facts

def card_prs(card: dict) -> list[tuple[str, int]]:
    """The card's own PRs: run metadata pr_url/pr_urls/pr, else qualified refs in the result."""
    found = []

    def add(text):
        for ref in _prg.parse_pr_refs(text, default_repo=None):
            key = (ref.repo, ref.number)
            if key not in found:
                found.append(key)

    for run in card["runs"]:
        try:
            md = json.loads(run.get("metadata") or "null")
        except (TypeError, ValueError):
            md = None
        if isinstance(md, dict):
            for k in ("pr_url", "pr_urls", "pr"):
                v = md.get(k)
                for s in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
                    if isinstance(s, str):
                        add(s)
    if not found:
        add(card.get("result") or "")
    return found


def is_fleet(repo: str) -> bool:
    """Fleet-owned repo (kanban_open_pr.FLEET_OWNERS). Upstream PRs are mentions, never gates."""
    return repo.split("/", 1)[0].lower() in _opr.FLEET_OWNERS


def close_recorded(card: dict, repo: str, number: int) -> bool:
    """The card's own text records why the PR closed unmerged: a line naming ``#N`` (or the
    full ref) together with SUPERSEDED / CLOSED: / closed."""
    texts = [card.get("result") or ""] + [r.get("summary") or "" for r in card["runs"]] + \
            [x.get("body") or "" for x in card["comments"]]
    pat = re.compile(rf"(?:{re.escape(repo)})?#{number}\b")
    return any(pat.search(ln) and _CLOSE_RECORD.search(ln) for t in texts for ln in t.splitlines())


def wave_of(card: dict) -> tuple[str, int, str]:
    """(kind, number, label) sort key + display label for the card's wave/round group."""
    m = _TITLE_TAG.match(card.get("title") or "")
    if m:
        if m.group(1):
            return ("1wave", int(m.group(1)), f"Wave {m.group(1)}")
        if m.group(2):
            return ("2round", int(m.group(2)), f"Round {m.group(2)}")
        return ("3harden", 0, "HARDEN")
    m = _BODY_TAG.search((card.get("body") or "")[:_BODY_TAG_WINDOW])
    if m:
        if m.group(1):
            return ("1wave", int(m.group(1)), f"Wave {m.group(1)}")
        return ("2round", int(m.group(2)), f"Round {m.group(2)}")
    m = _AUTO_PREFIX.match(card.get("title") or "")
    if m:
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        label = label[0].upper() + label[1:]   # "rebase" and "Rebase" are one lane
        return ("4auto", 0, f"Automation-minted: {label}")
    return ("5untagged", 0, "Untagged (no wave/round in title or origin line)")


def is_external(card: dict) -> bool:
    return card["status"] == "blocked" and card["block_reason"].lstrip().upper().startswith(("EXTERNAL:", "PARKED:"))


def flip_of(card: dict, open_ids: set) -> tuple[str, str]:
    """(group, why) — what has to happen for an open card to close."""
    reason = card.get("block_reason") or ""
    close_on = ""
    for c in reversed(card["comments"]):
        m = _CLOSE_ON.search(c.get("body") or "")
        if m:
            close_on = m.group(1).strip()
            break
    st = card["status"]
    if st == "running":
        return "worker running", close_on or "worker turn in flight"
    if st == "review":
        return "review (closer / Apollo)", close_on or "awaiting review verdict"
    if st == "scheduled":
        due = card.get("next_eligible_at")
        when = _dt.datetime.fromtimestamp(due).strftime("%m-%d %H:%M") if due else "?"
        return "clock", f"scheduled wake {when}" + (f" — {close_on}" if close_on else "")
    if st == "triage":
        return "triage", "needs triage-resolve"
    if st in ("ready", "todo"):
        return "dispatcher", "waiting for a worker slot"
    up = reason.lstrip().upper()
    if up.startswith("EXTERNAL:"):
        return "external", reason[:160]
    if up.startswith("PARKED:"):
        return "parked (by ruling)", reason[:160]
    if close_on:
        return "close-on named", close_on[:160]
    kind = card.get("block_kind") or ""
    if kind == "needs_input":
        return "needs Ace", reason[:160]
    if kind == "capability":
        return "needs Apollo", reason[:160]
    ids = [i for i in _CARD_ID.findall(reason) if i != card["id"]]
    if ids:
        ours = [i for i in ids if i in open_ids]
        return ("gated on in-session card" if ours else "gated on other card"), reason[:160]
    return "no flip named", reason[:160] or "(no block reason)"


# --------------------------------------------------------------------------- build

def _when(ts) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|")


def build(root: Path, sids: list[str], *, pr_query: Optional[PrQuery] = None, network: bool = True,
          vault_dir: Optional[Path] = None) -> dict:
    cards, unreadable = load_cards(root, sids)
    by, total, per_board = header_counts(root, sids)
    ids = [c["id"] for c in cards]
    open_ids = {c["id"] for c in cards if c["status"] not in TERMINAL}

    # PRs
    pr_map = {c["id"]: card_prs(c) for c in cards}
    all_refs = {r for refs in pr_map.values() for r in refs}
    states = resolve_pr_states(all_refs, pr_query) if (network and all_refs) else {r: None for r in all_refs}

    # per-card outcome flags
    for c in cards:
        texts = [x.get("body") or "" for x in c["comments"]] + [r.get("summary") or "" for r in c["runs"]]
        md_proof = any('"live_evidence": "yes' in (r.get("metadata") or "") for r in c["runs"])
        c["proof"] = md_proof or any(_PROOF.search(t) for t in texts)
        c["retracted"] = any(_RETRACT.search(x.get("body") or "") for x in c["comments"])
        c["prs"] = [(r, states.get(r)) for r in pr_map[c["id"]]]

    # waves
    groups = collections.defaultdict(list)
    for c in cards:
        groups[wave_of(c)].append(c)

    # rulings, deduped by stamp
    rulings = collections.OrderedDict()
    for c in cards:
        sources = [(c.get("body") or "", c["created_at"])] + [(x.get("body") or "", x["created_at"]) for x in c["comments"]]
        for text, ts in sources:
            for m in _RULING.finditer(text):
                snippet = m.group(0)
                st = _STAMP.search(snippet)
                key = st.group(1) if st else _clip(snippet, 60).lower()
                entry = rulings.setdefault(key, {"stamp": key, "text": _clip(snippet, 240), "cards": [], "first": ts})
                if c["id"] not in entry["cards"]:
                    entry["cards"].append(c["id"])
                entry["first"] = min(entry["first"], ts)

    # incidents
    incidents = []
    for c in cards:
        for x in c["comments"]:
            body = x.get("body") or ""
            m = _ROOT_CAUSE.search(body)
            if m:
                start = max(0, m.start() - 60)
                incidents.append({"card": c["id"], "at": x["created_at"], "author": x.get("author") or "?",
                                  "text": _clip(body[start:start + 360], 340)})
    incidents.sort(key=lambda d: d["at"])

    # docs/skills named
    docs = collections.OrderedDict()
    for c in cards:
        for t in [c.get("result") or ""] + [r.get("summary") or "" for r in c["runs"]] + \
                 [x.get("body") or "" for x in c["comments"]]:
            for m in _DOC_PATH.finditer(t):
                p = m.group(1).rstrip(".,;:)")
                docs.setdefault(p, set()).add(c["id"])
    doc_rows = []
    for p, cs in docs.items():
        if p.startswith(("AI/", "Engineering/", "Runbooks/")):
            exists = (vault_dir / p).is_file() if vault_dir else None
        elif p.startswith(("skills/", "skills-shared/", "plans/")):
            cand = [root / p, root / "skills-shared" / p.split("/", 1)[-1]] if p.startswith("skills/") else [root / p]
            exists = any(x.is_file() for x in cand)
        else:  # docs/… is relative to whichever repo the card worked in: not resolvable here
            exists = None
        doc_rows.append({"path": p, "exists": exists, "cards": sorted(cs)})

    # cost
    sess = session_rows(root, sids)
    cost = {"sessions": [{"id": s["id"], "state_db_usd": s.get("estimated_cost_usd"),
                          "chat_turns": chat_cost(root, s)} for s in sess],
            "workers": worker_cost(root, ids)}

    # open remainder
    flips = collections.defaultdict(list)
    for c in cards:
        if c["status"] not in TERMINAL:
            g, why = flip_of(c, open_ids)
            flips[g].append((c, why))

    # board-vs-census, blocked split
    ext = sum(1 for c in cards if is_external(c))
    by_split = dict(by)
    if ext:
        by_split["blocked"] = by_split.get("blocked", 0) - ext
        by_split["external"] = ext
    report = {
        "sids": sids, "root": str(root), "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": total, "by_status": by_split, "per_board": per_board, "unreadable_boards": unreadable,
        "census": len(cards), "cards": cards, "groups": groups, "rulings": list(rulings.values()),
        "incidents": incidents, "docs": doc_rows, "cost": cost, "flips": flips, "states": states,
    }
    report["gates"] = gates(report)
    return report


# --------------------------------------------------------------------------- gates

def gates(r: dict) -> list[dict]:
    """ang-closeout checklist rows, PASS/FAIL from the data this verb can see. A row with no
    machine evidence is FAIL, never N/A (ang-closeout: 'No evidence = FAIL/BLOCK')."""
    cards = r["cards"]
    out = []

    def g(row, name, ok, evidence):
        out.append({"row": row, "gate": name, "status": "PASS" if ok else "FAIL", "evidence": evidence})

    parts = sum(v for k, v in r["by_status"].items())
    g("acct", "Header accounting (W8-4): parts sum to N and N == census",
      parts == r["total"] == r["census"] and not r["unreadable_boards"],
      f"N={r['total']} parts={parts} census={r['census']} unreadable={r['unreadable_boards'] or 'none'}")
    fleet = [(c, ref, s) for c in cards for ref, s in c["prs"] if is_fleet(ref[0])]
    unread = sum(1 for _, _, s in fleet if s is None)
    false_done = sorted({c["id"] for c, ref, s in fleet if c["status"] == "done" and
                         (s == "OPEN" or (s == "CLOSED" and not close_recorded(c, *ref)))})
    g("-1", "Unverified-claim elicitation: no done card names an OPEN / unrecorded closed-unmerged fleet PR "
      "(FALSE-DONE)", not false_done and not unread,
      f"false-done={len(false_done)} {' '.join(false_done[:12])}; fleet PR states unreadable={unread}")
    g("0", "Hardening pass recorded", False, "not derivable from the board; run ang-harden and record it")
    g("1", "E2E / live proof on cards with PRs",
      all(c["proof"] for c in cards if c["prs"] and c["status"] == "done"),
      f"{sum(1 for c in cards if c['prs'] and c['proof'])}/{sum(1 for c in cards if c['prs'])} PR cards carry a "
      "live-evidence/live-proof line")
    g("2", "Acceptance criteria", False, "per-card criteria are prose; not machine-checkable here")
    g("3", "Constitution/Invariants", False, "no ang-spec invariants section attached to a session")
    missing_docs = [d["path"] for d in r["docs"] if d["exists"] is False]
    checked = sum(1 for d in r["docs"] if d["exists"] is not None)
    g("4", "Skills/docs named by cards exist on disk", checked > 0 and not missing_docs,
      f"{len(r['docs'])} paths named, {checked} checkable, {len(missing_docs)} missing" + (f": {', '.join(missing_docs[:8])}" if missing_docs else ""))
    g("5", "Obsidian overview written", bool(r.get("vault_written")), r.get("vault_written") or "no --vault-out given")
    open_prs = sorted({f"{repo}#{n}" for c, (repo, n), s in fleet if s == "OPEN"})
    closed = sorted({f"{repo}#{n}" for c, (repo, n), s in fleet if s == "CLOSED" and not close_recorded(c, repo, n)})
    foreign_open = sorted({f"{repo}#{n}" for c in cards for (repo, n), s in c["prs"]
                           if not is_fleet(repo) and s == "OPEN"})
    g("6b", "No fleet PR of the session left OPEN", not open_prs,
      f"open={len(open_prs)} {' '.join(open_prs[:12])}; upstream still-open, mention only: {len(foreign_open)}")
    g("6b'", "No fleet PR closed-unmerged without a close record on its card", not closed,
      f"unrecorded closed-unmerged={len(closed)} {' '.join(closed[:12])} (audit each with ang-git §5)")
    g("7", "mem0 fact hygiene", False, "not derivable from the board")
    g("8", "Cron/alerts committed", False, "not derivable from the board; check cron/jobs.json on origin")
    noflip = r["flips"].get("no flip named", [])
    g("9", "Loose ends triaged (every open card names what flips it)", not noflip,
      f"open={sum(len(v) for v in r['flips'].values())}, no-flip={len(noflip)} "
      + " ".join(c["id"] for c, _ in noflip[:12]))
    g("10", "DISCOVERIES captured (root-cause comments)", bool(r["incidents"]), f"{len(r['incidents'])} root-cause comments")
    open_n = sum(len(v) for v in r["flips"].values())
    g("11", "Kanban cards swept: every card terminal", open_n == 0, f"{open_n} open of {r['census']}")
    bare = [c["id"] for c in cards if c["status"] == "done" and not c["comments"] and not (c.get("result") or "").strip()]
    g("11'", "Every done card carries evidence (comment or result)", not bare,
      f"{len(bare)} bare done cards " + " ".join(bare[:12]))
    return out


# --------------------------------------------------------------------------- render

def _money(x) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "?"


def _tok(row) -> str:
    return (f"{row[0]:,} turns · {_money(row[1])} · in {row[2]:,} · out {row[3]:,} · "
            f"cache read {row[4]:,} · cache write {row[5]:,}")


def render(r: dict) -> str:
    L = []
    sid = ", ".join(r["sids"])
    # Vault frontmatter vocabulary (obsidian-vault .github/scripts/vault_pr_lint.py VALID_T/VALID_S):
    # a closeout is a handoff; it is "done" only when no card is left open.
    open_n = sum(len(v) for v in r["flips"].values())
    L.append(f"---\ntype: handoff\nstatus: {'done' if open_n == 0 else 'active'}\n"
             f"last_verified: {r['generated_at'][:10]}\n"
             f"session: {sid}\ngenerated: {r['generated_at']}\n"
             "generator: hermes kanban session-closeout\n---\n")
    L.append(f"# ANG Closeout — session {sid}\n")
    parts = " · ".join(f"{k} {r['by_status'][k]}" for k in STATUS_ORDER if r["by_status"].get(k)) or "none"
    extra = [k for k in r["by_status"] if k not in STATUS_ORDER and r["by_status"][k]]
    if extra:
        parts += " · " + " · ".join(f"{k} {r['by_status'][k]}" for k in extra)
    psum = sum(r["by_status"].values())
    mismatch = "" if psum == r["total"] == r["census"] else f" — ACCOUNTING MISMATCH (parts {psum}, census {r['census']})"
    L.append(f"**{r['total']} cards: {parts}**{mismatch}  ")
    L.append("Boards: " + ", ".join(f"{b} {n}" for b, n in sorted(r["per_board"].items(), key=lambda x: -x[1])))
    if r["unreadable_boards"]:
        L.append(f"  \nUNREADABLE boards: {', '.join(r['unreadable_boards'])}")
    if r["cards"]:
        L.append(f"  \nSpan: {_when(min(c['created_at'] for c in r['cards']))} → "
                 f"{_when(max(c['created_at'] for c in r['cards']))} (card creation)")
    L.append("")

    # gates
    L.append("## Gates (ang-closeout)\n\n| Row | Gate | Status | Evidence |\n|---|---|---|---|")
    for g in r["gates"]:
        L.append(f"| {g['row']} | {_md_cell(g['gate'])} | **{g['status']}** | {_md_cell(_clip(g['evidence'], 300))} |")
    L.append("")

    # cost
    L.append("## Cost (blackbox turns.db + state.db)\n")
    for s in r["cost"]["sessions"]:
        L.append(f"- Session `{s['id']}` state.db estimate {_money(s['state_db_usd'])}; "
                 f"chat turns in window: {_tok(s['chat_turns'])}")
    w = r["cost"]["workers"]
    L.append(f"- Worker turns on this session's cards (chat_id = card id, every profile): {_tok(w['total'])}")
    for name, row in sorted(w["per_profile"].items(), key=lambda x: -x[1][1]):
        L.append(f"  - {name}: {_tok(row)}")
    L.append("")

    # open remainder
    L.append(f"## Open remainder — {open_n} cards, grouped by what flips it\n")
    order = ["needs Ace", "needs Apollo", "review (closer / Apollo)", "worker running", "dispatcher", "triage",
             "close-on named", "gated on in-session card", "gated on other card", "clock", "external",
             "parked (by ruling)", "no flip named"]
    for grp in order + [k for k in r["flips"] if k not in order]:
        rows = r["flips"].get(grp)
        if not rows:
            continue
        L.append(f"### {grp} ({len(rows)})\n")
        for c, why in rows:
            L.append(f"- `{c['id']}` [{c['board']}·{c['status']}] {_clip(c['title'], 90)} — {_clip(why, 200)}")
        L.append("")

    # waves
    L.append("## Waves and rounds — every card\n")
    L.append("Outcome legend: PR state from GitHub REST at generation time · ✔proof = live-evidence/live-proof "
             "recorded · ⚠retracted = a CORRECTION / retraction / FALSE-DONE comment on the card.\n")
    for key in sorted(r["groups"], key=lambda k: (k[0], k[1])):
        cs = r["groups"][key]
        cnt = collections.Counter(c["status"] for c in cs)
        merged = sum(1 for c in cs for _, s in c["prs"] if s == "MERGED")
        L.append(f"### {key[2]} — {len(cs)} cards ({', '.join(f'{k} {v}' for k, v in cnt.most_common())}; "
                 f"{merged} merged PRs)\n")
        for c in sorted(cs, key=lambda c: c["created_at"]):
            bits = []
            if c["prs"]:
                bits.append(", ".join(f"{repo}#{n} {s or 'UNREAD'}" for (repo, n), s in c["prs"][:4])
                            + (f" +{len(c['prs']) - 4}" if len(c["prs"]) > 4 else ""))
            if c["proof"]:
                bits.append("✔proof")
            if c["retracted"]:
                bits.append("⚠retracted")
            tail = (" — " + " · ".join(bits)) if bits else ""
            board = "" if c["board"] == "default" else f"{c['board']}·"
            L.append(f"- `{c['id']}` [{board}{c['status']}] {_when(c['created_at'])} {_clip(c['title'], 110)}{tail}")
        L.append("")

    # rulings
    L.append(f"## Standing rulings cited in-session — {len(r['rulings'])} distinct stamps\n")
    for ru in sorted(r["rulings"], key=lambda x: x["first"]):
        more = f" +{len(ru['cards']) - 3}" if len(ru["cards"]) > 3 else ""
        L.append(f"- **{ru['stamp']}** — {_clip(ru['text'], 220)} ({', '.join(ru['cards'][:3])}{more})")
    L.append("")

    # incidents
    L.append(f"## Incident ledger — {len(r['incidents'])} root-cause comments\n")
    for i in r["incidents"]:
        L.append(f"- {_when(i['at'])} `{i['card']}` ({i['author']}): {i['text']}")
    L.append("")

    # docs
    L.append(f"## Skills and docs named by the cards — {len(r['docs'])}\n")
    for d in sorted(r["docs"], key=lambda d: d["path"]):
        mark = {True: "✔", False: "✘ missing", None: "? (vault not checked)"}[d["exists"]]
        L.append(f"- `{d['path']}` {mark} ({', '.join(d['cards'][:3])}{' +' + str(len(d['cards']) - 3) if len(d['cards']) > 3 else ''})")
    L.append("")
    return "\n".join(L) + "\n"


def to_json(r: dict) -> dict:
    listed = sorted({c["id"] for cs in r["groups"].values() for c in cs})
    return {
        "sids": r["sids"], "total": r["total"], "by_status": r["by_status"], "per_board": r["per_board"],
        "census": r["census"], "cards_listed": listed, "gates": r["gates"],
        "open": {g: [c["id"] for c, _ in v] for g, v in r["flips"].items()},
        "rulings": len(r["rulings"]), "incidents": len(r["incidents"]), "docs": len(r["docs"]),
        "cost": r["cost"], "unreadable_boards": r["unreadable_boards"],
    }


def run(args) -> int:
    from hermes_cli import kanban_db as kb

    root = Path(args.root).expanduser() if getattr(args, "root", None) else kb.kanban_home()
    vault = Path(args.vault).expanduser() if getattr(args, "vault", None) else None
    sids = list(dict.fromkeys(args.session_ids))
    r = build(root, sids, network=not args.no_network, vault_dir=vault)
    if not r["census"]:
        print(f"kanban session-closeout: no cards for {', '.join(sids)} on any board under {root}",
              file=__import__("sys").stderr)
        return 2
    if args.vault_out:
        r["vault_written"] = str(Path(args.vault_out).expanduser())
        r["gates"] = gates(r)
    text = render(r)
    for dest in filter(None, (args.out, args.vault_out)):
        p = Path(dest).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if args.json:
        print(json.dumps(to_json(r), indent=2, default=str))
    elif not args.out and not args.vault_out:
        print(text, end="")
    else:
        for g in r["gates"]:
            print(f"{g['status']:4}  {g['row']:5} {g['gate']} — {_clip(g['evidence'], 160)}")
        print(f"wrote {', '.join(filter(None, (args.out, args.vault_out)))} "
              f"({r['census']} cards, {len(text):,} bytes)")
    if args.check and any(g["status"] == "FAIL" for g in r["gates"]):
        return 1
    return 0
