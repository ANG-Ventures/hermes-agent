#!/usr/bin/env python3
"""Measure the cost of workers that cannot read what their card was written from, and judge the
dispatch evidence bundle (``hermes_cli/kanban_evidence_bundle.py``, t_978ec761) against it.

Read-only. Opens the board with ``mode=ro``; ``--github`` adds REST reads of the cards' first PRs
(``gh api repos/O/R/pulls/N``, never GraphQL) for open-to-merge.

For a date range ``[--since, --until)`` (run start time), over WORKER cards (a card with at least one run
by a profile in the range):

* ``unreadable``: cards whose ``blocked`` or ``changes_requested`` reason matches the card's pattern
  ("cannot read / not found / no such file / attachment / Studio-only", plus the obvious spellings), with
  the count per term, the share of worker cards, and the minutes lost per case (that event to the next
  ``unblocked`` / ``claimed`` / ``review_requested`` / ``completed`` / ``archived`` on the card);
* ``home_paths_off_studio``: cards whose body names a path under ``~/.hermes/{var,cache,plans}/`` and that
  ran at least once on a non-Studio host;
* per card: send-back rounds (``changes_requested``), block rounds (``blocked`` other than a card created
  blocked), request-to-PR minutes (card created to the first event or run that names a GitHub PR), and with
  ``--github`` open-to-merge minutes over a fixed hashed sample of ``--github-limit`` PRs (default 200);
* bundle bytes per card (attachments named ``evidence-*``).

Every metric is reported twice: cohort ``off`` (no ``evidence-manifest.json`` attachment) and ``on``.
Before the toggle is turned on, every card is ``off`` and the output is the baseline.

Usage: python3 scripts/kanban_evidence_bundle_eval.py [--db ~/.hermes/kanban.db] [--days 14 | --since
YYYY-MM-DD [--until YYYY-MM-DD]] [--github] [--json]
"""
# SCHEDULE: manual (operator runs it before and after a dispatch_evidence_bundle trial)
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

MANIFEST_NAME = "evidence-manifest.json"
BUNDLE_PREFIX = "evidence-"

#: The card's pattern, term by term (case-insensitive). "not found" and "attachment" are broad on purpose
#: (the card asked for them); the per-term counts show how much each one contributes.
TERMS = {
    "cannot read": r"\b(?:cannot|can't|can not|could not|couldn't|unable to) (?:read|open|see|access)\b",
    "not found": r"\bnot found\b",
    "no such file": r"\bno such file\b",
    "attachment": r"\battachments?\b",
    "studio-only": r"\bstudio[- ]only\b|\bonly (?:on )?the (?:mac )?studio\b",
}
_TERM_RES = {k: re.compile(v, re.I) for k, v in TERMS.items()}
_HOME_PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes/(?:var|cache|plans)/[^\s'\"`)]+")
_PR_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)|\b([\w.-]+/[\w.-]+)#(\d+)\b")
_RESUME_KINDS = ("unblocked", "claimed", "review_requested", "completed", "archived")


def is_studio_host(host) -> bool:
    """A run's host: NULL is a local (Studio) spawn; a pool host names itself."""
    return host is None or host == "" or "studio" in str(host).lower()


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return round(s[k], 1)


def _dist(xs):
    return {"n": len(xs), "p50": _pct(xs, 0.5), "p90": _pct(xs, 0.9),
            "mean": round(statistics.fmean(xs), 2) if xs else None}


def _reason(payload: str) -> str:
    try:
        d = json.loads(payload or "{}")
    except ValueError:
        return payload or ""
    return str(d.get("reason") or "") if isinstance(d, dict) else ""


def first_pr(texts):
    """First ``(repo, n)`` a sequence of ``(ts, text)`` names, with its ts; None when none does."""
    for ts, text in texts:
        for m in _PR_RE.finditer(text or ""):
            repo, n = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if "/" in repo and not repo.startswith(("http", "www.")):
                return ts, repo, int(n)
    return None


def _gh_pr(repo, n, cache):
    key = f"{repo}#{n}"
    if key in cache:
        return cache[key]
    try:
        out = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{n}", "--jq", "[.created_at,.merged_at]"],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        cache[key] = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        cache[key] = None
    return cache[key]


def _iso(s):
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def evaluate(conn: sqlite3.Connection, since: int, until: int, *, github: bool = False,
             github_limit: int = 200) -> dict:
    conn.row_factory = sqlite3.Row
    runs = conn.execute(
        "SELECT task_id, host FROM task_runs WHERE profile IS NOT NULL AND profile != '' "
        "AND started_at >= ? AND started_at < ?", (since, until)).fetchall()
    hosts: dict[str, set] = {}
    for r in runs:
        hosts.setdefault(r["task_id"], set()).add(r["host"])
    cards = sorted(hosts)
    if not cards:
        return {"since": since, "until": until, "worker_cards": 0, "cohorts": {}}
    meta = {}
    for i in range(0, len(cards), 500):
        chunk = cards[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT id, body, created_at FROM tasks WHERE id IN ({q})", chunk):
            meta[r["id"]] = {"body": r["body"] or "", "created_at": r["created_at"], "bundle_bytes": 0,
                             "on": False}
        for r in conn.execute(f"SELECT task_id, filename, size FROM task_attachments WHERE task_id IN ({q})",
                              chunk):
            m = meta.get(r["task_id"])
            if m is None:
                continue
            if r["filename"].startswith(BUNDLE_PREFIX):
                m["bundle_bytes"] += int(r["size"] or 0)
            if r["filename"].startswith(MANIFEST_NAME.rsplit(".", 1)[0]):
                m["on"] = True
    cards = [c for c in cards if c in meta]
    events: dict[str, list] = {c: [] for c in cards}
    for i in range(0, len(cards), 500):
        chunk = cards[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT task_id, kind, payload, created_at FROM task_events WHERE task_id IN ({q}) "
                "ORDER BY id", chunk):
            events[r["task_id"]].append((r["kind"], r["payload"], r["created_at"]))
        for r in conn.execute(
                f"SELECT task_id, coalesce(ended_at, started_at) AS ts, summary, metadata FROM task_runs "
                f"WHERE task_id IN ({q}) ORDER BY id", chunk):
            events[r["task_id"]].append(("_run", f"{r['summary'] or ''} {r['metadata'] or ''}", r["ts"]))

    gh_cache: dict = {}
    per = {}
    import hashlib  # a fixed pseudo-random order, so a --github-limit sample is not just the oldest cards
    for c in sorted(cards, key=lambda t: hashlib.sha256(t.encode()).hexdigest()):
        ev = sorted(events[c], key=lambda e: e[2] or 0)
        cases = []
        for idx, (kind, payload, ts) in enumerate(ev):
            if kind not in ("blocked", "changes_requested"):
                continue
            reason = _reason(payload)
            if "initial_status" in reason and kind == "blocked":
                continue
            hit = [t for t, rx in _TERM_RES.items() if rx.search(reason)]
            if not hit:
                continue
            nxt = next((e[2] for e in ev[idx + 1:] if e[0] in _RESUME_KINDS and e[2] >= ts), None)
            cases.append({"kind": kind, "terms": hit, "minutes": None if nxt is None else (nxt - ts) / 60.0})
        sendbacks = sum(1 for e in ev if e[0] == "changes_requested")
        blocks = sum(1 for e in ev if e[0] == "blocked" and "initial_status" not in _reason(e[1]))
        pr = first_pr((e[2], e[1] if e[0] != "_run" else e[1]) for e in ev
                      if e[0] in ("_run", "review_requested", "completed", "pr_acceptance", "commented"))
        req_to_pr = None if pr is None else max(0.0, (pr[0] - meta[c]["created_at"]) / 60.0)
        open_to_merge = None
        if github and pr is not None and (f"{pr[1]}#{pr[2]}" in gh_cache or len(gh_cache) < github_limit):
            got = _gh_pr(pr[1], pr[2], gh_cache)
            if got and got[0] and got[1]:
                open_to_merge = (_iso(got[1]) - _iso(got[0])) / 60.0
        per[c] = {
            "on": meta[c]["on"], "off_studio": any(not is_studio_host(h) for h in hosts[c]),
            "home_paths": bool(_HOME_PATH_RE.search(meta[c]["body"])), "cases": cases,
            "sendbacks": sendbacks, "blocks": blocks, "req_to_pr": req_to_pr, "open_to_merge": open_to_merge,
            "bundle_bytes": meta[c]["bundle_bytes"],
        }

    cohorts = {}
    for name, flag in (("off", False), ("on", True)):
        cs = [c for c in cards if per[c]["on"] is flag]
        n = len(cs)
        hit = [c for c in cs if per[c]["cases"]]
        all_cases = [k for c in hit for k in per[c]["cases"]]
        terms = {t: sum(1 for c in hit if any(t in k["terms"] for k in per[c]["cases"])) for t in TERMS}
        narrow = [c for c in hit if any(set(k["terms"]) & {"cannot read", "no such file", "studio-only"}
                                        for k in per[c]["cases"])]
        hp = [c for c in cs if per[c]["home_paths"] and per[c]["off_studio"]]
        reviewed = [c for c in cs if per[c]["sendbacks"] or any(
            e[0] == "review_requested" for e in events[c])]
        cohorts[name] = {
            "worker_cards": n,
            "off_studio_cards": sum(1 for c in cs if per[c]["off_studio"]),
            "unreadable": {
                "cards": len(hit), "share": round(len(hit) / n, 4) if n else None, "cases": len(all_cases),
                "cards_by_term": terms,
                "cards_off_studio": sum(1 for c in hit if per[c]["off_studio"]),
                "minutes_lost_per_case": _dist([k["minutes"] for k in all_cases if k["minutes"] is not None]),
                "unresumed_cases": sum(1 for k in all_cases if k["minutes"] is None),
                "narrow_cards": len(narrow),
                "narrow_share": round(len(narrow) / n, 4) if n else None,
                "narrow_minutes_lost_per_case": _dist([k["minutes"] for c in narrow for k in per[c]["cases"]
                                                        if k["minutes"] is not None]),
            },
            "home_paths_off_studio": {"cards": len(hp), "share": round(len(hp) / n, 4) if n else None,
                                      "ids": sorted(hp)[:50]},
            "sendback_rounds_per_reviewed_card": _dist([per[c]["sendbacks"] for c in reviewed]),
            "block_rounds_per_card": _dist([per[c]["blocks"] for c in cs]),
            "request_to_pr_minutes": _dist([per[c]["req_to_pr"] for c in cs if per[c]["req_to_pr"] is not None]),
            "open_to_merge_minutes": (_dist([per[c]["open_to_merge"] for c in cs
                                             if per[c]["open_to_merge"] is not None])
                                      if github else "not computed (pass --github)"),
            "bundle_bytes_per_card": _dist([per[c]["bundle_bytes"] for c in cs if per[c]["on"]]),
            "unreadable_ids": sorted(hit)[:50],
        }
    return {"since": since, "until": until, "worker_cards": len(cards), "cohorts": cohorts}


def _day(s: str) -> int:
    return int(_dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(Path(os.path.expanduser("~/.hermes/kanban.db"))))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--github", action="store_true", help="REST-read first PRs for open-to-merge")
    ap.add_argument("--github-limit", type=int, default=200,
                    help="at most this many PR reads (a fixed hashed sample of the cards; REST budget)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    until = _day(a.until) if a.until else int(time.time())
    since = _day(a.since) if a.since else until - a.days * 86400
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=60)
    out = evaluate(conn, since, until, github=a.github, github_limit=a.github_limit)
    if a.json:
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    fmt = lambda t: _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")  # noqa: E731
    print(f"evidence-bundle eval {fmt(since)} .. {fmt(until)}: {out['worker_cards']} worker cards")
    for name, c in out.get("cohorts", {}).items():
        u = c["unreadable"]
        print(f"[{name}] cards {c['worker_cards']} (off-Studio {c['off_studio_cards']})")
        print(f"  unreadable: {u['cards']} cards ({u['share']}), {u['cases']} cases, off-Studio {u['cards_off_studio']},"
              f" minutes lost/case {u['minutes_lost_per_case']}, unresumed {u['unresumed_cases']}")
        print(f"    by term {u['cards_by_term']}")
        print(f"    narrow (cannot read | no such file | studio-only): {u['narrow_cards']} cards ({u['narrow_share']}),"
              f" minutes lost/case {u['narrow_minutes_lost_per_case']}")
        h = c["home_paths_off_studio"]
        print(f"  body names ~/.hermes/{{var,cache,plans}}/ and ran off-Studio: {h['cards']} ({h['share']})")
        print(f"  send-back rounds/reviewed card {c['sendback_rounds_per_reviewed_card']}")
        print(f"  block rounds/card {c['block_rounds_per_card']}")
        print(f"  request-to-PR min {c['request_to_pr_minutes']}")
        print(f"  open-to-merge min {c['open_to_merge_minutes']}")
        print(f"  bundle bytes/card {c['bundle_bytes_per_card']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
