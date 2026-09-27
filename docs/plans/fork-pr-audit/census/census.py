#!/usr/bin/env python3
"""Census of fork-only commits (upstream/main..origin/main --no-merges), grouped by PR.

Read-only. Writes census.json + census.md next to this script.
"""
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).parent / "fork"
OUT = Path(__file__).parent
RANGE = "upstream/main..origin/main"

PR_RE = re.compile(r"\(#(\d+)\)\s*$")

TOP_SUBSYSTEMS = [
    "gateway", "agent", "hermes_cli", "plugins", "cron", "tools", "tests",
    "docs", "scripts", "website", "apps", "tui", "hermes_cli", "fleet",
]


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def classify_path(p: str) -> str:
    top = p.split("/", 1)[0]
    if p.startswith("tests/") or "/tests/" in p or re.search(r"(^|/)test_[^/]+\.py$", p):
        return "tests"
    if p.endswith((".md", ".rst", ".txt")) or top in ("docs", "plans", "website"):
        return "docs"
    if top == "plugins":
        parts = p.split("/")
        return "plugins/" + parts[1] if len(parts) > 1 else "plugins"
    if top == "gateway":
        parts = p.split("/")
        if len(parts) > 2 and parts[1] == "platforms":
            return "gateway/platforms"
        return "gateway"
    if top in ("agent", "hermes_cli", "cron", "tools", "scripts", "apps", "tui", "hermes_cli"):
        return top
    if top.startswith("."):
        return "ci"
    return "other:" + top


def main():
    log = git("log", "--no-merges", "--format=%H%x00%h%x00%ad%x00%an%x00%s", "--date=short", RANGE)
    commits = []
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, short, date, author, subject = line.split("\x00")
        m = PR_RE.search(subject)
        pr = int(m.group(1)) if m else None
        numstat = git("show", "--numstat", "--format=", sha)
        files = []
        add = dele = 0
        for row in numstat.splitlines():
            parts = row.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            a = 0 if a == "-" else int(a)
            d = 0 if d == "-" else int(d)
            add += a
            dele += d
            files.append({"path": path, "add": a, "del": d, "class": classify_path(path)})
        commits.append({
            "sha": sha, "short": short, "date": date, "author": author, "subject": subject,
            "pr": pr, "add": add, "del": dele, "files": files,
        })
        sys.stderr.write(f"\r{len(commits)} commits")
    sys.stderr.write("\n")

    # Group by PR; PR-less commits stay individual (key = short sha).
    groups = defaultdict(list)
    for c in commits:
        key = f"#{c['pr']}" if c["pr"] else f"nopr:{c['short']}"
        groups[key].append(c)

    rows = []
    for key, cs in groups.items():
        cls_lines = defaultdict(int)
        paths = set()
        for c in cs:
            for f in c["files"]:
                cls_lines[f["class"]] += f["add"] + f["del"]
                paths.add(f["path"])
        code_classes = {k: v for k, v in cls_lines.items() if k not in ("tests", "docs", "ci")}
        if code_classes:
            subsystem = max(code_classes.items(), key=lambda kv: kv[1])[0]
            kind = "code"
        elif "tests" in cls_lines and "docs" not in cls_lines:
            subsystem, kind = "tests", "tests-only"
        elif "docs" in cls_lines and "tests" not in cls_lines:
            subsystem, kind = "docs", "docs-only"
        elif cls_lines:
            subsystem, kind = "tests+docs", "tests+docs-only"
        else:
            subsystem, kind = "empty", "empty"
        rows.append({
            "key": key,
            "pr": cs[0]["pr"],
            "commits": len(cs),
            "first_date": min(c["date"] for c in cs),
            "last_date": max(c["date"] for c in cs),
            "subject": cs[0]["subject"],
            "subjects": [c["subject"] for c in cs],
            "shas": [c["short"] for c in cs],
            "add": sum(c["add"] for c in cs),
            "del": sum(c["del"] for c in cs),
            "loc": sum(c["add"] + c["del"] for c in cs),
            "files": len(paths),
            "paths": sorted(paths),
            "subsystem": subsystem,
            "kind": kind,
            "class_lines": dict(cls_lines),
        })
    rows.sort(key=lambda r: (r["subsystem"], -(r["pr"] or 0), r["key"]))

    (OUT / "census.json").write_text(json.dumps({
        "range": RANGE,
        "upstream_main": git("rev-parse", "upstream/main").strip(),
        "origin_main": git("rev-parse", "origin/main").strip(),
        "merge_base": git("merge-base", "upstream/main", "origin/main").strip(),
        "commits": len(commits),
        "groups": len(rows),
        "rows": rows,
    }, indent=1))

    # Summary
    by_sub = defaultdict(lambda: {"groups": 0, "commits": 0, "loc": 0})
    for r in rows:
        s = by_sub[r["subsystem"]]
        s["groups"] += 1
        s["commits"] += r["commits"]
        s["loc"] += r["loc"]
    md = [f"# Fork-only census — {RANGE}", "",
          f"upstream/main {git('rev-parse', '--short', 'upstream/main').strip()} · origin/main {git('rev-parse', '--short', 'origin/main').strip()} · {len(commits)} no-merge commits · {len(rows)} PR groups", "",
          "| subsystem | groups | commits | loc |", "|---|---|---|---|"]
    for sub, s in sorted(by_sub.items(), key=lambda kv: -kv[1]["loc"]):
        md.append(f"| {sub} | {s['groups']} | {s['commits']} | {s['loc']} |")
    md += ["", "| key | subsystem | kind | commits | loc | files | date | subject |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        subj = r["subject"][:90].replace("|", "/")
        md.append(f"| {r['key']} | {r['subsystem']} | {r['kind']} | {r['commits']} | {r['loc']} | {r['files']} | {r['last_date']} | {subj} |")
    (OUT / "census.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:40]))


if __name__ == "__main__":
    main()
