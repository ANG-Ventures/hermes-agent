import os, sqlite3, time
from collections import Counter
H = os.path.expanduser("~/.hermes")
SINCE = int(time.time()) - 14 * 86400
for b, p in (("default", f"{H}/kanban.db"), ("house-voice", f"{H}/kanban/boards/house-voice/kanban.db")):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = c.execute("select r.task_id, r.id, r.ended_at, t.assignee from task_runs r left join tasks t on t.id=r.task_id "
                     "where r.outcome='no_new_head' and r.ended_at>=?", (SINCE,)).fetchall()
    nxt = Counter(); worker = 0; ready_long = 0
    for tid, rid, ended, who in rows:
        if not who or who in ("default", "apollo", "aegis") or who.startswith("human"):
            continue
        worker += 1
        seq = c.execute("select new_status, changed_at from task_status_audit where task_id=? and changed_at>=? order by id",
                        (tid, ended - 5)).fetchall()
        st = seq[0][0] if seq else None
        nxt[st] += 1
        if st == "ready":
            end = next((at for s, at in seq[1:] if s != "ready"), int(time.time()))
            if end - seq[0][1] >= 1800:
                ready_long += 1
    print(b, "no_new_head runs:", len(rows), "on worker cards:", worker, "next status:", dict(nxt),
          "ready>=30min after:", ready_long)
