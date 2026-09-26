#!/usr/bin/env python3
"""Mechanical absorption probe: for each fork-only PR, match its substantive ADDED src lines
and its added def/class symbols against a read-only snapshot of upstream/main.

Writes absorb.json {key: {src_hit, src_total, sym_hit, sym_total, syms_missing[:8]}}.
Interpretation trap (pr-absorption-census §6): high hit% can mean the opposite of absorption
(refactor PRs, shared scaffolding). Auditors must read the matched lines; this is a starting signal only.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE / "fork"
SNAP = HERE / "upsnap"
RG = "/opt/homebrew/bin/rg"


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout


if not SNAP.exists():
    SNAP.mkdir()
    subprocess.run(f"git archive upstream/main | tar -x -C {SNAP}", shell=True, cwd=REPO, check=True)

d = json.load(open(HERE / "census_v2.json", encoding="utf-8"))
SKIP_LINE = re.compile(r"^\s*(#|//|\"\"\"|'''|import |from |return\b|pass\b|else:|try:|finally:|[\]\)\}\],;]*$)")
SYM = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
out = {}
n = 0
for r in d["rows"]:
    n += 1
    sys.stderr.write(f"\r{n}/{len(d['rows'])}")
    src_lines, syms = set(), set()
    for sha in r["shas"]:
        diff = git("show", "--format=", "--no-color", sha)
        cur = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                cur = line[6:]
                continue
            if not cur or not line.startswith("+") or line.startswith("+++"):
                continue
            if cur.startswith("tests/") or "/tests/" in cur or cur.endswith((".md", ".txt", ".rst", ".json", ".lock")):
                continue
            body = line[1:]
            m = SYM.match(line)
            if m:
                syms.add(m.group(1))
            s = body.strip()
            if len(s) < 25 or SKIP_LINE.match(body):
                continue
            src_lines.add(s)
    res = {"src_total": len(src_lines), "sym_total": len(syms), "src_hit": 0, "sym_hit": 0, "syms_missing": []}
    if src_lines or syms:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".pat") as f:
            pats = sorted(src_lines | syms)
            f.write("\n".join(pats) + "\n")
            pf = f.name
        p = subprocess.run([RG, "-F", "-f", pf, "-o", "--no-heading", "--no-filename", "--no-line-number",
                            "--no-messages", "-g", "!tests/**", str(SNAP)], capture_output=True, text=True)
        found = set(x.strip() for x in p.stdout.splitlines())
        # -o prints the matched fragment; a line pattern matches itself, a symbol matches itself
        res["src_hit"] = sum(1 for s in src_lines if s in found)
        hit_syms = {s for s in syms if s in found}
        res["sym_hit"] = len(hit_syms)
        res["syms_missing"] = sorted(syms - hit_syms)[:8]
        Path(pf).unlink()
    out[r["key"]] = res
sys.stderr.write("\n")
json.dump(out, open(HERE / "absorb.json", "w", encoding="utf-8"), indent=1)

# summary buckets
b = {"likely-absorbed(>=80% src)": 0, "partial(20-80%)": 0, "outstanding(<20%)": 0, "no-src-lines": 0}
for k, v in out.items():
    if v["src_total"] == 0:
        b["no-src-lines"] += 1
    else:
        pct = v["src_hit"] / v["src_total"]
        b["likely-absorbed(>=80% src)" if pct >= .8 else "partial(20-80%)" if pct >= .2 else "outstanding(<20%)"] += 1
print(b)
