"""Regenerate gateway.md from gateway.verdicts.json (row order = json key order; header kept verbatim
except the counts line, which is recomputed)."""
import json
import re
import sys
from collections import Counter

V = sys.argv[1]
d = json.load(open(V + "gateway.verdicts.json"))
old = open(V + "gateway.md").read().split("\n")


def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def cell_cost(c):
    return f"{c.get('loc')}/{c.get('conflict_files')}({c.get('conflict_syncs')}) deps={c.get('dependents')}"


def row(key, r):
    ev = r.get("evidence") or {}
    subj = r.get("subject") or ""
    first = f"{key} {subj[:SUBJ_W]}"
    prob = f"{r.get('problem')} — {ev.get('original')}"
    branch = r.get("branch") or ""
    return (f"| {esc(first)} | {esc(prob)} | {esc(ev.get('upstream_state'))} | {esc(ev.get('upstream_fix'))} | "
            f"{esc(ev.get('fires'))} | {cell_cost(ev.get('cost') or {})} | **{r.get('verdict')}** | "
            f"{esc(branch)}{(' ' + esc(r['notes'])) if r.get('notes') else ''} |")


# infer subject width from an existing row
SUBJ_W = 70  # the bank step truncates the subject to 70 chars
head = []
for l in old:
    if l.startswith("| #") or l.startswith("| nopr:"):
        break
    head.append(l)
counts = Counter(r["verdict"] for r in d.values())
order = ["KEEP", "DROP", "UPSTREAM", "SUPERSEDED-BY-UPSTREAM", "UNRESOLVED"]
cline = "Counts: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in order if counts.get(k) or k in counts)
head = [cline if l.startswith("Counts: ") else l for l in head]
tail_start = max(i for i, l in enumerate(old) if l.startswith("| #") or l.startswith("| nopr:")) + 1
old_keys = [l[2:].split(" ", 1)[0] for l in old if l.startswith("| #") or l.startswith("| nopr:")]
keys = [k for k in old_keys if k in d] + [k for k in d if k not in old_keys]
out = head + [row(k, d[k]) for k in keys] + old[tail_start:]
sys.stdout.write("\n".join(out))
