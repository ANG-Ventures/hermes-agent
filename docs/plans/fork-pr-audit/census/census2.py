#!/usr/bin/env python3
"""Refine census.json -> census_v2.json + tranches.

- drop parity-sync squash commits (#510 #643 #644): upstream content, not fork patches
- flag `cherry picked from commit` trailers (upstream content already; auto SUPERSEDED)
- sync_overlap: how many of the PR's paths the two sync squashes also touched (cost proxy)
- tranche assignment by subsystem
"""
import json
import subprocess
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE / "fork"
d = json.load(open(HERE / "census.json"))
rows = d["rows"]


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout


SYNC_PRS = {510, 643, 644}
import re as _re
LEDGERS = {
    "0723": "docs/sync/review/RESOLUTION-LEDGER.md",
    "0807": "docs/sync/review/RESOLUTION-LEDGER-20260807.md",
    "0829": "docs/sync/review/RESOLUTION-LEDGER-2026-08-29.md",
}
PATH_TOK = _re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*\.(?:py|md|json|yaml|yml|ts|tsx|js|toml|txt|sh)")
all_paths = set()
for r in rows:
    all_paths.update(r["paths"])
ledger_files = {}
for k, p in LEDGERS.items():
    toks = set(PATH_TOK.findall((REPO / p).read_text(errors="replace")))
    # keep tokens that are real repo paths (basename match allowed for ledgers that list bare names)
    by_base = defaultdict(set)
    for ap in all_paths:
        by_base[ap.rsplit("/", 1)[-1]].add(ap)
    hits = set()
    for t in toks:
        if t in all_paths:
            hits.add(t)
        elif "/" not in t and len(by_base.get(t, ())) == 1:
            hits.add(next(iter(by_base[t])))
    ledger_files[k] = hits
print({k: len(v) for k, v in ledger_files.items()})

# cherry-pick trailers
cp = set()
for line in git("log", "--no-merges", "--format=%h%x00%b", "upstream/main..origin/main").split("\n"):
    pass
buf = git("log", "--no-merges", "--format=%x01%h%x00%b", "upstream/main..origin/main")
for rec in buf.split("\x01")[1:]:
    h, _, body = rec.partition("\x00")
    # only count picks whose source commit is actually in upstream/main (fork-internal picks are not absorption)
    for m in _re.finditer(r"cherry picked from commit ([0-9a-f]{7,40})", body):
        if subprocess.run(["git", "merge-base", "--is-ancestor", m.group(1), "upstream/main"], cwd=REPO,
                          capture_output=True).returncode == 0:
            cp.add(h)
            break


def tranche(sub, paths):
    if sub in ("gateway", "gateway/platforms", "other:tui_gateway", "plugins/platforms"):
        return "gateway"
    if sub in ("agent", "other:run_agent.py", "other:hermes_state.py", "other:hermes_undo.py", "other:hermes_test_context.py"):
        return "agent"
    if sub in ("hermes_cli", "other:cli.py", "other:pyproject.toml", "other:locales"):
        return "hermes_cli"
    if sub.startswith("plugins/"):
        return "plugins"
    if sub in ("cron", "tools"):
        return "cron+tools"
    if sub in ("scripts", "other:staging", "other:receipts", "other:gates.jsonl", "other:eval", "other:contributors", "other:providers", "other:ui-tui", "other:web", "apps"):
        return "scripts+misc"
    if sub in ("tests", "docs", "tests+docs"):
        return "auto"
    return "scripts+misc"


out = []
for r in rows:
    if r["pr"] in SYNC_PRS:
        continue
    r2 = dict(r)
    r2.pop("subjects", None)
    r2["cherry_picked_from_upstream"] = any(s in cp for s in r["shas"])
    ps = set(r["paths"])
    r2["conflict_files"] = sorted(ps & (ledger_files["0723"] | ledger_files["0807"] | ledger_files["0829"]))
    r2["conflict_syncs"] = sum(1 for k in ledger_files if ps & ledger_files[k])
    r2["sync_overlap_files"] = len(r2["conflict_files"])
    sub = r["subsystem"]
    if sub == "apps":
        # D9: apps/desktop is upstream-owned (retired 2026-08-08). Pure-desktop rows are DROP-by-policy;
        # mixed rows are re-bucketed by their non-desktop code lines.
        non_apps = {k: v for k, v in r["class_lines"].items() if k not in ("apps", "tests", "docs", "ci")}
        if non_apps:
            sub = max(non_apps.items(), key=lambda kv: kv[1])[0]
            r2["subsystem"] = sub
            r2["note"] = "mixed desktop row; re-bucketed by non-desktop code paths (apps/ part is D9 DROP)"
        else:
            r2["tranche"] = "auto-desktop-retired"
            out.append(r2)
            continue
    r2["tranche"] = "auto-cherry-pick" if r2["cherry_picked_from_upstream"] else tranche(sub, r["paths"])
    out.append(r2)

by = defaultdict(lambda: {"rows": 0, "loc": 0, "code_rows": 0})
for r in out:
    b = by[r["tranche"]]
    b["rows"] += 1
    b["loc"] += r["loc"]
    b["code_rows"] += r["kind"] == "code"

json.dump({**{k: v for k, v in d.items() if k != "rows"}, "excluded_sync_prs": sorted(SYNC_PRS),
           "rows": out}, open(HERE / "census_v2.json", "w"), indent=1)

md = ["| tranche | rows | code rows | loc |", "|---|---|---|---|"]
for t, b in sorted(by.items(), key=lambda kv: -kv[1]["loc"]):
    md.append(f"| {t} | {b['rows']} | {b['code_rows']} | {b['loc']} |")
print("\n".join(md))
print("\ncherry-picked rows:", sum(r["cherry_picked_from_upstream"] for r in out))
print("rows with sync overlap>0:", sum(r["sync_overlap_files"] > 0 for r in out))

# per-tranche markdown tables (+ absorb signal when absorb.json exists)
absorb = json.load(open(HERE / "absorb.json")) if (HERE / "absorb.json").exists() else {}
for r in out:
    a = absorb.get(r["key"])
    if a:
        r["absorb"] = a
json.dump({**{k: v for k, v in d.items() if k != "rows"}, "excluded_sync_prs": sorted(SYNC_PRS),
           "rows": out}, open(HERE / "census_v2.json", "w"), indent=1)
for t in by:
    trs = [r for r in out if r["tranche"] == t]
    trs.sort(key=lambda r: -r["loc"])
    lines = [f"# Tranche: {t} — {len(trs)} rows", "",
             "absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.", "",
             "| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in trs:
        subj = r["subject"][:100].replace("|", "/")
        hot = f"{r['sync_overlap_files']}({r['conflict_syncs']})"
        a = r.get("absorb") or {}
        ab = f"{a.get('src_hit', '?')}/{a.get('src_total', '?')}"
        sy = f"{a.get('sym_hit', '?')}/{a.get('sym_total', '?')}"
        lines.append(f"| {r['key']} | {r['subsystem']} | {r['kind']} | {r['loc']} | {r['files']} | {hot} | {ab} | {sy} | {r['last_date']} | {subj} |")
    (HERE / f"tranche_{t.replace('+', '_')}.md").write_text("\n".join(lines) + "\n")
