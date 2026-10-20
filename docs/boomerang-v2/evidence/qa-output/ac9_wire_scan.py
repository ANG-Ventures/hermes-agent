"""AC-9 independent re-scan (Argus, t_df1e3b88). Reads JSONL wire records on stdin (pre-filtered by grep to
the 05:58-06:08Z window = 22:58-23:08 PDT, 2026-10-19), and for every record reports:
  - every header NAME in req.headers / req.headerOrder / req.rawNames / req.outHeaders that starts x-hermes-
  - usage (cache_read) so the fork child's requests (~340k prefix) can be told apart from other traffic.
Header values are never printed.
"""
import json
import sys


def names(v):
    out = []
    if isinstance(v, dict):
        out += list(v)
    elif isinstance(v, list):
        for p in v:
            if isinstance(p, (list, tuple)) and p:
                out.append(str(p[0]))
            elif isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                out.append(str(p.get("name") or p.get("k") or ""))
    return out


rows = []
for line in sys.stdin:
    try:
        r = json.loads(line)
    except Exception:
        continue
    req = r.get("req") or {}
    res = r.get("res") or {}
    body = (res.get("body") or {})
    u = body.get("usage") or body.get("usageTail") or {}
    fields = {k: names(req.get(k)) for k in ("headers", "headerOrder", "rawNames", "outHeaders")}
    leaks = {k: sorted({n for n in v if n.lower().startswith("x-hermes-")}) for k, v in fields.items()}
    rows.append({
        "id": r.get("id"), "ts": r.get("ts"), "lane": r.get("lane"), "sub": r.get("sub"),
        "model": (req.get("body") or {}).get("model"), "n_msgs": (req.get("body") or {}).get("messages"),
        "cache_read": u.get("cache_read_input_tokens"), "input": u.get("input_tokens"),
        "n_header_names": {k: len(v) for k, v in fields.items()},
        "x_hermes": {k: v for k, v in leaks.items() if v},
        "mutations": [m if isinstance(m, str) else json.dumps(m)[:120] for m in (req.get("mutations") or [])][:6],
    })
rows.sort(key=lambda x: x["ts"] or "")
fork_like = [x for x in rows if (x["cache_read"] or 0) >= 300000]
print(json.dumps({
    "records": len(rows),
    "records_with_any_x_hermes": sum(1 for x in rows if x["x_hermes"]),
    "fork_like_records(cache_read>=300k)": len(fork_like),
    "fork_like_with_x_hermes": sum(1 for x in fork_like if x["x_hermes"]),
    "rows": rows,
}, indent=1))
