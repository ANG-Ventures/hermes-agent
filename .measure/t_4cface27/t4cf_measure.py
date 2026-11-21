#!/usr/bin/env python3
"""t_4cface27 measurement, read-only. Worker cards that sat `ready` > 2 h in the last 14 days, grouped by hold.

Ready intervals come from task_status_audit (new_status='ready' until the next audit row for the card, or now).
Hold class per interval = the most frequent hold event inside it, across live task_events and archived_events:
  respawn_guarded:<reason> | placement:<code> (placement_wait/allocation_wait, not cleared) |
  dependency_wait | skipped_nonspawnable | none.
"""
import json, os, sqlite3, sys, time, zlib
from collections import Counter, defaultdict

NOW = int(time.time())
SINCE = NOW - 14 * 86400
MIN_S = 2 * 3600
OPS = {"default", "apollo", "aegis"}
H = os.path.expanduser("~/.hermes")
BOARDS = {
    "default": (f"{H}/kanban.db", f"{H}/kanban-archive.db", f"{H}/kanban-events-archive.db"),
    "house-voice": tuple(f"{H}/kanban/boards/house-voice/{n}" for n in
                         ("kanban.db", "kanban-archive.db", "kanban-events-archive.db")),
}
HOLD_KINDS = ("respawn_guarded", "placement_wait", "allocation_wait", "dependency_wait", "skipped_nonspawnable",
              "dispatch_skipped", "priority_starved", "stranded_no_worker", "scheduled", "schedule_wake_set")


def ro(path):
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def payload_of(row):
    raw = row["payload"] if "payload" in row.keys() else None
    if raw is None and "payload_z" in row.keys() and row["payload_z"] is not None:
        try:
            raw = zlib.decompress(row["payload_z"]).decode()
        except Exception:
            raw = None
    try:
        p = json.loads(raw or "{}")
    except Exception:
        p = {}
    return p if isinstance(p, dict) else {}


def classify(kind, p):
    if kind == "respawn_guarded":
        return f"respawn_guarded:{p.get('reason') or '?'}"
    if kind in ("placement_wait", "allocation_wait"):
        if p.get("cleared"):
            return None
        code = str(p.get("code") or p.get("reason") or "?")
        return "placement:" + code.split(" ")[0].split(":")[0]
    if kind == "dispatch_skipped":
        return f"dispatch_skipped:{p.get('reason') or '?'}"
    if kind in ("scheduled", "schedule_wake_set"):
        return "scheduled"
    return kind


out = {}
for board, (live, arch, evarch) in BOARDS.items():
    L, A, E = ro(live), ro(arch), ro(evarch)
    assignee = {r["id"]: r["assignee"] for r in L.execute("select id, assignee from tasks")}
    if A is not None:
        try:
            for r in A.execute("select id, assignee from tasks"):
                assignee.setdefault(r["id"], r["assignee"])
        except sqlite3.Error:
            pass
    rows = L.execute("select task_id, new_status, changed_at from task_status_audit "
                     "where changed_at >= ? order by task_id, id", (SINCE - 14 * 86400,)).fetchall()
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append((r["new_status"], int(r["changed_at"])))
    intervals = []
    for tid, seq in by_task.items():
        who = assignee.get(tid)
        if not who or who in OPS or who.startswith("human"):
            continue
        for i, (st, at) in enumerate(seq):
            if st != "ready" or (i and seq[i - 1][0] == "ready"):
                continue  # ready->ready rows belong to the interval that began first
            j = i + 1
            while j < len(seq) and seq[j][0] == "ready":
                j += 1
            end = seq[j][1] if j < len(seq) else NOW
            if end < SINCE or end - at < MIN_S:
                continue
            intervals.append((tid, who, at, end))
    res = []
    for tid, who, start, end in intervals:
        cnt = Counter()
        prs = Counter()
        merge = Counter()
        holdtxt = Counter()
        for src in (L, E):
            if src is None:
                continue
            table = "task_events" if src is L else "archived_events"
            col = "payload" if src is L else "payload_z"
            try:
                evs = src.execute(f"select kind, {col} from {table} where task_id=? and created_at between ? and ? "
                                  f"and kind in ({','.join('?' * len(HOLD_KINDS))})",
                                  (tid, start, end, *HOLD_KINDS)).fetchall()
            except sqlite3.Error:
                continue
            for e in evs:
                p = payload_of(e)
                c = classify(e["kind"], p)
                if c:
                    cnt[c] += 1
                if c == "respawn_guarded:active_pr":
                    if p.get("pr"):
                        prs[p["pr"]] += 1
                    merge[str(p.get("merge_state"))] += 1
                    holdtxt[str(p.get("hold"))] += 1
        cls = cnt.most_common(1)[0][0] if cnt else "none"
        res.append({"board": board, "task": tid, "assignee": who, "start": start, "end": end,
                    "hours": round((end - start) / 3600, 1), "still_ready": end == NOW, "class": cls,
                    "classes": dict(cnt), "prs": dict(prs), "merge_states": dict(merge), "holds": dict(holdtxt)})
    out[board] = res
    # Every active_pr streak >= 30 min (any length of ready), for the false-wake count of candidate (b).
    streaks = []
    for src, table, col in ((L, "task_events", "payload"), (E, "archived_events", "payload_z")):
        if src is None:
            continue
        cur = {}
        for e in src.execute(f"select task_id, created_at, kind, {col} from {table} where created_at >= ? and "
                             "kind in ('respawn_guarded','spawned','claimed','requeued','unblocked') "
                             "order by task_id, created_at", (SINCE,)):
            tid, at = e["task_id"], int(e["created_at"])
            p = payload_of(e) if e["kind"] == "respawn_guarded" else {}
            is_pr = e["kind"] == "respawn_guarded" and p.get("reason") == "active_pr"
            s = cur.get(tid)
            if s and (not is_pr or at - s["last"] > 600 or p.get("pr") != s["pr"]):
                streaks.append(s)
                cur.pop(tid)
                s = None
            if is_pr:
                if s is None:
                    s = cur[tid] = {"board": board, "task": tid, "pr": p.get("pr"), "first": at, "last": at,
                                    "merge": Counter(), "owned": p.get("pr_owned"),
                                    "assignee": assignee.get(tid)}
                s["last"] = at
                s["merge"][str(p.get("merge_state"))] += 1
        streaks.extend(cur.values())
    long = [dict(s, merge=dict(s["merge"]), minutes=(s["last"] - s["first"]) // 60) for s in streaks
            if s["last"] - s["first"] >= 1800]
    out[board + ":active_pr_streaks"] = long
    # Candidate (a): runs that ended in the window with a summary saying a finding was declined and no new commits.
    a = []
    for r in L.execute("select task_id, id, outcome, summary, ended_at from task_runs where ended_at >= ? and "
                       "summary is not null", (SINCE,)):
        s = (r["summary"] or "").lower()
        if "declin" in s and ("no new commit" in s or "no commit" in s or "operator decides" in s):
            nxt = L.execute("select new_status from task_status_audit where task_id=? and changed_at>=? "
                            "order by id limit 1", (r["task_id"], int(r["ended_at"]) - 5)).fetchone()
            a.append({"task": r["task_id"], "run": r["id"], "outcome": r["outcome"],
                      "next_status": nxt[0] if nxt else None, "assignee": assignee.get(r["task_id"])})
    out[board + ":candidate_a_runs"] = a
json.dump({"now": NOW, "since": SINCE, "rows": out}, sys.stdout)
