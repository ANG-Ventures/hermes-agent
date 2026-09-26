#!/usr/bin/env python3
"""Nightly flake ledger (CI efficiency spec L2(b) / Phase 4).

Reads the Actions API only. For the CI workflow's push / merge_group / schedule
runs in the last 14 days it pairs every red attempt with a later green attempt
on the SAME head_sha (a re-run of the run, or another run of that SHA), reads
both attempts' per-file junit, and records every node that failed on red and
passed on green. A node with >= 2 such pairs on >= 2 distinct days, on SHAs
that are ancestors of main, is a quarantine CANDIDATE. The candidate is then
run through the same evidence gate ``Tests complete`` runs on the PR
(``flake_quarantine.verify_evidence``) — the ledger has no privilege the gate
does not re-check.

Classifier (the table the spec asks for):
    red -> green, same head_sha       => flake
    red -> green, different head_sha  => fix
    red -> red                        => real

Subcommands:
    scan    print candidate entries as JSON (``--apply`` merges them into the list)
    digest  every active entry with owner + until; warns 3 days before expiry
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci import flake_quarantine as fq  # noqa: E402

CI_WORKFLOW = "ci.yaml"
EXPIRY_WARN_DAYS = 3
MAX_PAGES = 30


def classify(red: dict, later: dict) -> str:
    """Outcome class of a red attempt given a LATER attempt."""
    if later.get("conclusion") == "failure":
        return "real"
    if later.get("conclusion") != "success":
        return "unknown"
    return "flake" if red.get("head_sha") == later.get("head_sha") else "fix"


def _runs(api: fq.ApiLike, repo: str, since: dt.date) -> list[dict]:
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = api.get_json(
            f"repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?created=>={since.isoformat()}"
            f"&status=completed&per_page=100&page={page}")
        runs = data.get("workflow_runs") or []
        out += [r for r in runs if r.get("event") in fq.EVIDENCE_EVENTS]
        if len(runs) < 100:
            break
    return out


def _attempts(api: fq.ApiLike, repo: str, run: dict) -> list[dict]:
    n = int(run.get("run_attempt") or 1)
    if n == 1:
        return [run]
    return [api.get_json(f"repos/{repo}/actions/runs/{run['id']}/attempts/{k}") for k in range(1, n + 1)]


def _junit_nodes(api: fq.ApiLike, repo: str, run_id: int, attempt: int) -> tuple[set[str], set[str]]:
    """(failed, passed) node IDs across every slice artifact of one attempt."""
    import io
    import re
    import zipfile

    pat = re.compile(fq.SLICE_ARTIFACT_RE.format(attempt=attempt))
    failed: set[str] = set()
    passed: set[str] = set()
    arts = api.get_json(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100").get("artifacts") or []
    for art in arts:
        if not pat.match(art.get("name") or "") or art.get("expired"):
            continue
        blob = api.get_bytes(f"repos/{repo}/actions/artifacts/{art['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            manifest = json.loads(zf.read("slice-result.json")) if "slice-result.json" in zf.namelist() else {}
            for f in manifest.get("files") or []:
                if not f.get("junit") or f["junit"] not in zf.namelist():
                    continue
                try:
                    fl, ps = fq.junit_outcomes(zf.read(f["junit"]), f["path"])
                except fq.JunitError:
                    continue
                failed |= fl
                passed |= ps
    return failed, passed


def red_green_pairs(api: fq.ApiLike, repo: str, since: dt.date) -> tuple[list[tuple[dict, dict]], dict[str, int]]:
    """Every (red attempt, first later green attempt on the same SHA), plus class counts."""
    by_sha: dict[str, list[dict]] = defaultdict(list)
    for run in _runs(api, repo, since):
        for att in _attempts(api, repo, run):
            by_sha[att.get("head_sha") or ""].append(att)
    pairs: list[tuple[dict, dict]] = []
    counts: dict[str, int] = defaultdict(int)
    for sha, atts in by_sha.items():
        atts.sort(key=lambda a: (str(a.get("run_started_at") or ""), a.get("id") or 0, a.get("run_attempt") or 0))
        for i, a in enumerate(atts):
            if a.get("conclusion") != "failure":
                continue
            later = [b for b in atts[i + 1:] if b.get("conclusion") in ("success", "failure")]
            if not later:
                counts["no same-SHA retry"] += 1
                continue
            cls = classify(a, later[0])
            counts[cls] += 1
            if cls == "flake":
                pairs.append((a, later[0]))
    return pairs, dict(counts)


def candidates(api: fq.ApiLike, repo: str, now: dt.datetime, existing: dict) -> tuple[list[dict], dict]:
    since = (now - dt.timedelta(days=fq.EVIDENCE_WINDOW_DAYS)).date()
    pairs, counts = red_green_pairs(api, repo, since)
    per_node: dict[str, list[dict]] = defaultdict(list)
    ancestor_cache: dict[str, bool] = {}
    for red, green in pairs:
        sha = red["head_sha"]
        if sha not in ancestor_cache:
            try:
                ancestor_cache[sha] = api.get_json(f"repos/{repo}/compare/{sha}...main").get("status") in ("ahead", "identical")
            except urllib.error.HTTPError:
                ancestor_cache[sha] = False
        if not ancestor_cache[sha]:
            continue
        r_failed, _ = _junit_nodes(api, repo, red["id"], red["run_attempt"])
        _, g_passed = _junit_nodes(api, repo, green["id"], green["run_attempt"])
        for node in sorted(r_failed & g_passed):
            per_node[node].append({
                "red": {"run_id": red["id"], "attempt": red["run_attempt"]},
                "green": {"run_id": green["id"], "attempt": green["run_attempt"]},
                "_day": str(red.get("run_started_at") or "")[:10],
            })
    active = fq.active_entries(existing, now.date())
    out = []
    for node, ev in sorted(per_node.items()):
        days = {e["_day"] for e in ev}
        if node in active or len(ev) < fq.MIN_PAIRS or len(days) < fq.MIN_DISTINCT_DAYS:
            continue
        entry = {
            "node_id": node,
            "owner": "flake-ledger",
            "until": (now.date() + dt.timedelta(days=fq.MAX_TTL_DAYS)).isoformat(),
            "reason": f"flake-ledger: {len(ev)} same-SHA red->green pairs on {len(days)} days",
            "evidence": [{k: v for k, v in e.items() if not k.startswith("_")} for e in ev],
        }
        # Self-check with the gate's own verifier: never propose what CI would reject.
        if not fq.verify_evidence(entry, api, repo, now):
            out.append(entry)
    return out, counts


def digest(data: dict, today: dt.date) -> list[str]:
    lines = []
    for e in sorted(fq.active_entries(data, today).values(), key=lambda e: e["until"]):
        left = (dt.date.fromisoformat(e["until"]) - today).days
        warn = " ⚠️ expires in <= 3 days — re-gates automatically" if left <= EXPIRY_WARN_DAYS else ""
        lines.append(f"{e['node_id']} — owner {e['owner']}, until {e['until']} ({left} d){warn}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--repo", required=True)
    p.add_argument("--list", default=fq.LIST_PATH)
    p.add_argument("--apply", action="store_true", help="merge candidates into --list")
    p = sub.add_parser("digest")
    p.add_argument("--list", default=fq.LIST_PATH)
    args = ap.parse_args(argv)
    path = Path(args.list)
    data = fq.parse_list(path.read_text(encoding="utf-8") if path.is_file() else None)
    now = dt.datetime.now(dt.timezone.utc)
    if args.cmd == "digest":
        lines = digest(data, now.date())
        print("\n".join(lines) if lines else "no active quarantine entries")
        return 0
    api = fq.Api(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    found, counts = candidates(api, args.repo, now, data)
    print(json.dumps({"counts": counts, "candidates": found}, indent=1))
    if args.apply and found:
        data.setdefault("entries", []).extend(found)
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
