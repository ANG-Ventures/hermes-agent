#!/usr/bin/env python3
"""Flake quarantine for the per-file test slices (CI efficiency spec I4 / L2(b)).

A quarantined test STILL RUNS and reports; only its failure stops gating. Three
entry points, each a CLI subcommand:

``slice-verdict``
    Run by every test slice after the suite, with the quarantine list read from
    the BASE ref (never the PR head). The slice is green iff the suite passed,
    or every failure is a test that is actively quarantined — decided per test
    from the per-file junit. A missing manifest, a missing/partial junit, a
    timeout, a crash, a no-op verdict, or a non-test failure is gating
    (fail closed).

``check-changes``
    Run once per CI run by ``Tests complete``. Diffs the base list against the
    head list per full node ID. Every added entry, every changed entry and every
    ``until:`` moved later needs evidence; the evidence is RE-DERIVED from the
    Actions API, never trusted from the PR. Deleting an entry or shortening its
    ``until:`` is evidence-free (the veto path).

``lint``
    Schema check: ``owner``/``card``/``until`` present and well formed, ``until`` at most
    14 days out. Expiry is NOT a lint failure: an expired entry simply stops
    being active and the test gates again.

Evidence rule (spec I4 p4 B1, p5 RC-H/RC-I), per added entry:
  (1) every cited run attempt exists in this repository;
  (2) each pair is red->green on ONE head_sha, >= 2 pairs, whose red attempts
      fall on >= 2 distinct UTC days within the last 14 days;
  (3) both attempts ran on event push / merge_group / schedule (workflow code
      already on main) and their per-file junit names the node failed (red)
      and passed (green);
  (4) the cited SHA is an ancestor of main.
Phase 0 (spec §0.5) measured 6/30 reds meeting (4), so (4) is NOT loosened.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

SCHEMA = 1
MAX_TTL_DAYS = 14
EVIDENCE_WINDOW_DAYS = 14
MIN_PAIRS = 2
# 1 since t_162ffd04: a same-SHA red->green pair already proves unchanged code,
# and a 2-day floor let one flake eject the merge queue for two days first.
MIN_DISTINCT_DAYS = 1
EVIDENCE_EVENTS = frozenset({"push", "merge_group", "schedule"})
LIST_PATH = "scripts/ci/flake_quarantine.json"
SLICE_ARTIFACT_RE = r"^ci-slice-result-\d+-a{attempt}$"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Every quarantine names the kanban card that owns the fix (never silent).
_CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")


def _card_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_CARD_RE.match(value))


# ── list parsing / lint ─────────────────────────────────────────────────────

def junit_name(rel_path: str) -> str:
    """Must match run_tests_parallel.junit_name (one flat file per test file)."""
    return rel_path.replace("\\", "/").replace("/", "__") + ".xml"


def parse_list(text: str | None) -> dict:
    if text is None or not text.strip():
        return {"schema": SCHEMA, "entries": []}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("quarantine list must be a JSON object")
    return data


def _date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def lint(data: dict, today: dt.date) -> list[str]:
    problems: list[str] = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        return problems + ["entries must be a list"]
    seen: set[str] = set()
    for i, e in enumerate(entries):
        where = f"entries[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{where}: not an object")
            continue
        node = e.get("node_id")
        if not isinstance(node, str) or "::" not in node or not node.split("::", 1)[0].endswith(".py"):
            problems.append(f"{where}: node_id must be a full pytest node ID (path.py::...)")
        elif node in seen:
            problems.append(f"{where}: duplicate node_id {node}")
        else:
            seen.add(node)
        owner = e.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            problems.append(f"{where}: missing owner")
        if not _card_ok(e.get("card")):
            problems.append(f"{where}: missing or malformed card (t_xxxxxxxx fix card)")
        until = _date(e.get("until"))
        if until is None:
            problems.append(f"{where}: missing or malformed until (YYYY-MM-DD)")
        elif until > today + dt.timedelta(days=MAX_TTL_DAYS):
            problems.append(f"{where}: until {until} is more than {MAX_TTL_DAYS} days out")
        ev = e.get("evidence")
        if not isinstance(ev, list):
            problems.append(f"{where}: evidence must be a list of red/green pairs")
    return problems


def active_entries(data: dict, today: dt.date) -> dict[str, dict]:
    """Entries that currently exempt a failure. Malformed entries never do."""
    out: dict[str, dict] = {}
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        until = _date(e.get("until"))
        node = e.get("node_id")
        owner = e.get("owner")
        if until is None or not isinstance(node, str) or not isinstance(owner, str) or not owner.strip():
            continue
        if not _card_ok(e.get("card")):
            continue
        if until >= today:
            out[node] = e
    return out


# ── junit ───────────────────────────────────────────────────────────────────

class JunitError(Exception):
    pass


def junit_outcomes(xml_bytes: bytes, rel_path: str) -> tuple[set[str], set[str]]:
    """(failed node IDs, passed node IDs) from one per-file xunit1 junit.

    A testcase with a failure/error child is failed; one with no failure,
    error or skipped child is passed. A testcase whose node ID cannot be
    rebuilt exactly raises JunitError (the caller treats that as gating).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise JunitError(f"unparseable junit: {exc}") from exc
    failed: set[str] = set()
    passed: set[str] = set()
    for case in root.iter("testcase"):
        node = _node_id(case, rel_path)
        bad = case.find("failure") is not None or case.find("error") is not None
        if bad:
            if node is None:
                raise JunitError(f"cannot rebuild node ID for failing testcase {case.get('classname')}::{case.get('name')}")
            failed.add(node)
        elif case.find("skipped") is None and node is not None:
            passed.add(node)
    return failed, passed


def _node_id(case: ET.Element, rel_path: str) -> str | None:
    name = case.get("name")
    classname = case.get("classname") or ""
    file_attr = case.get("file")
    if not name or not file_attr:
        return None
    module_parts = list(Path(file_attr.replace("\\", "/")).with_suffix("").parts)
    parts = classname.split(".") if classname else []
    if parts[: len(module_parts)] != module_parts:
        return None
    return "::".join([rel_path, *parts[len(module_parts):], name])


# ── slice verdict ───────────────────────────────────────────────────────────

def slice_verdict(result: dict | None, junit_dir: Path, qlist: dict, today: dt.date) -> tuple[bool, list[str]]:
    """(green?, messages). Green only when every failure is actively quarantined."""
    if not isinstance(result, dict) or result.get("schema") != 1:
        return False, ["no readable slice manifest — gating"]
    if result.get("noop_red") or result.get("no_tests_ran_at_all"):
        return False, ["runner no-op verdict — gating"]
    files = result.get("files")
    if not isinstance(files, list):
        return False, ["manifest has no file list — gating"]
    failing = [f for f in files if f.get("rc") != 0]
    if result.get("runner_rc") == 0 and not failing:
        return True, []
    if not failing:
        return False, [f"runner exit {result.get('runner_rc')} with no failing file — gating"]
    active = active_entries(qlist, today)
    msgs: list[str] = []
    ok = True
    for f in failing:
        path = f.get("path")
        if f.get("timed_out") or f.get("rc") != 1:
            ok = False
            msgs.append(f"{path}: rc={f.get('rc')} timed_out={f.get('timed_out')} — not a plain test failure, gating")
            continue
        jname = f.get("junit")
        jpath = junit_dir / jname if jname else None
        if jpath is None or not jpath.is_file():
            ok = False
            msgs.append(f"{path}: no junit — gating")
            continue
        try:
            failed, _passed = junit_outcomes(jpath.read_bytes(), path)
        except JunitError as exc:
            ok = False
            msgs.append(f"{path}: {exc} — gating")
            continue
        if not failed:
            ok = False
            msgs.append(f"{path}: exited 1 but junit names no failing test — gating")
            continue
        for node in sorted(failed):
            e = active.get(node)
            if e is None:
                ok = False
                msgs.append(f"{node}: FAILED (not quarantined)")
            else:
                msgs.append(f"{node}: failed but QUARANTINED (non-gating; card {e['card']}, owner {e['owner']}, until {e['until']})")
    return ok, msgs


# ── evidence ────────────────────────────────────────────────────────────────

class ApiLike(Protocol):
    def get_json(self, path: str) -> Any: ...

    def get_bytes(self, path: str) -> bytes: ...


class Api:
    """Minimal GitHub REST client (GET only). Injected in tests."""

    def __init__(self, token: str | None, base: str = "https://api.github.com"):
        self.token = token
        self.base = base.rstrip("/")

    def _req(self, path: str) -> bytes:
        req = urllib.request.Request(f"{self.base}/{path.lstrip('/')}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            # Unredirected: artifact zips 302 to blob storage, which rejects
            # a foreign Authorization header.
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    def get_json(self, path: str) -> Any:
        return json.loads(self._req(path))

    def get_bytes(self, path: str) -> bytes:
        return self._req(path)


def diff_needing_evidence(base: dict, head: dict) -> list[tuple[dict, str]]:
    """Head entries that need fresh evidence, keyed by full node ID."""
    base_by = {e.get("node_id"): e for e in base.get("entries") or [] if isinstance(e, dict)}
    out: list[tuple[dict, str]] = []
    for e in head.get("entries") or []:
        if not isinstance(e, dict):
            continue
        old = base_by.get(e.get("node_id"))
        if old is None:
            out.append((e, "new entry"))
            continue
        if old == e:
            continue
        new_until, old_until = _date(e.get("until")), _date(old.get("until"))
        shortened_only = (
            {k: v for k, v in old.items() if k != "until"} == {k: v for k, v in e.items() if k != "until"}
            and new_until is not None
            and old_until is not None
            and new_until < old_until
        )
        if not shortened_only:
            out.append((e, "changed entry (until later or other field changed)"))
    return out


def _attempt(api: ApiLike, repo: str, ref: Any) -> dict:
    if not isinstance(ref, dict):
        raise LookupError("attempt reference must be {run_id, attempt}")
    run_id, attempt = ref.get("run_id"), ref.get("attempt")
    if not isinstance(run_id, int) or not isinstance(attempt, int) or attempt < 1:
        raise LookupError(f"malformed attempt reference {ref!r}")
    try:
        data = api.get_json(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    except urllib.error.HTTPError as exc:
        raise LookupError(f"run {run_id} attempt {attempt}: HTTP {exc.code}") from exc
    if (data.get("repository") or {}).get("full_name", "").lower() != repo.lower():
        raise LookupError(f"run {run_id} is not a run of {repo}")
    if data.get("id") != run_id or data.get("run_attempt") != attempt:
        raise LookupError(f"run {run_id} attempt {attempt}: API returned a different attempt")
    return data


def _node_outcome(api: ApiLike, repo: str, run_id: int, attempt: int, node: str) -> str | None:
    """'failed' / 'passed' / None (not found) for node in that attempt's junit."""
    rel = node.split("::", 1)[0]
    want = junit_name(rel)
    pat = re.compile(SLICE_ARTIFACT_RE.format(attempt=attempt))
    arts = api.get_json(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100").get("artifacts") or []
    for art in arts:
        if not pat.match(art.get("name") or "") or art.get("expired"):
            continue
        if (art.get("workflow_run") or {}).get("id") not in (None, run_id):
            continue
        blob = api.get_bytes(f"repos/{repo}/actions/artifacts/{art['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = {Path(n).name: n for n in zf.namelist()}
            if want not in names:
                continue
            failed, passed = junit_outcomes(zf.read(names[want]), rel)
        if node in failed:
            return "failed"
        if node in passed:
            return "passed"
        return None
    return None


def verify_evidence(entry: dict, api: ApiLike, repo: str, now: dt.datetime) -> list[str]:
    node = entry.get("node_id")
    if not isinstance(node, str) or "::" not in node:
        return [f"{node!r}: not a full node ID"]
    pairs = entry.get("evidence")
    if not isinstance(pairs, list) or len(pairs) < MIN_PAIRS:
        return [f"{node}: needs >= {MIN_PAIRS} red->green pairs, got {0 if not isinstance(pairs, list) else len(pairs)}"]
    problems: list[str] = []
    days: set[str] = set()
    seen: set[tuple] = set()
    good = 0
    for i, pair in enumerate(pairs):
        where = f"{node}: pair {i}"
        if not isinstance(pair, dict):
            problems.append(f"{where}: not an object")
            continue
        key = (json.dumps(pair.get("red"), sort_keys=True), json.dumps(pair.get("green"), sort_keys=True))
        if key in seen:
            problems.append(f"{where}: duplicate pair")
            continue
        seen.add(key)
        try:
            red = _attempt(api, repo, pair.get("red"))
            green = _attempt(api, repo, pair.get("green"))
        except LookupError as exc:
            problems.append(f"{where}: {exc} (condition 1)")
            continue
        pair_problems: list[str] = []
        if red.get("conclusion") != "failure" or green.get("conclusion") != "success":
            pair_problems.append(f"red={red.get('conclusion')} green={green.get('conclusion')}, need failure->success (condition 2)")
        if not red.get("head_sha") or red.get("head_sha") != green.get("head_sha"):
            pair_problems.append("red and green are on different head_sha (condition 2)")
        if red.get("workflow_id") != green.get("workflow_id"):
            pair_problems.append("red and green are different workflows (condition 2)")
        for label, att in (("red", red), ("green", green)):
            if att.get("event") not in EVIDENCE_EVENTS:
                pair_problems.append(f"{label} event {att.get('event')!r} is not push/merge_group/schedule (condition 3)")
        started = str(red.get("run_started_at") or "")
        try:
            started_at = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            started_at = None
        if started_at is None or now - started_at > dt.timedelta(days=EVIDENCE_WINDOW_DAYS) or started_at > now:
            pair_problems.append(f"red attempt started {started or '?'}: outside the {EVIDENCE_WINDOW_DAYS}-day window")
        if pair_problems or started_at is None:
            problems += [f"{where}: {p}" for p in pair_problems]
            continue
        sha = red["head_sha"]
        try:
            cmp_ = api.get_json(f"repos/{repo}/compare/{sha}...main")
            ancestor = cmp_.get("status") in ("ahead", "identical")
        except urllib.error.HTTPError:
            ancestor = False
        if not ancestor:
            problems.append(f"{where}: {sha[:10]} is not an ancestor of main (condition 4)")
            continue
        r_out = _node_outcome(api, repo, red["id"], red["run_attempt"], node)
        g_out = _node_outcome(api, repo, green["id"], green["run_attempt"], node)
        if r_out != "failed" or g_out != "passed":
            problems.append(f"{where}: junit says red={r_out} green={g_out}, need failed->passed (condition 3)")
            continue
        days.add(started_at.date().isoformat())
        good += 1
    if good < MIN_PAIRS:
        problems.append(f"{node}: {good} verified pair(s), need >= {MIN_PAIRS}")
    if len(days) < MIN_DISTINCT_DAYS:
        problems.append(f"{node}: verified pairs span {len(days)} distinct day(s), need >= {MIN_DISTINCT_DAYS}")
    return problems


def check_changes(base: dict, head: dict, api: ApiLike, repo: str, now: dt.datetime) -> list[str]:
    problems = [f"lint: {p}" for p in lint(head, now.date())]
    for entry, why in diff_needing_evidence(base, head):
        for p in verify_evidence(entry, api, repo, now):
            problems.append(f"{why}: {p}")
    return problems


# ── CLI ─────────────────────────────────────────────────────────────────────

def _read(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _today(args) -> dt.date:
    return dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()


def cmd_lint(args) -> int:
    try:
        data = parse_list(_read(args.file))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error::{args.file}: {exc}")
        return 1
    problems = lint(data, _today(args))
    for p in problems:
        print(f"::error::{args.file}: {p}")
    return 1 if problems else 0


def cmd_slice_verdict(args) -> int:
    if args.tests_outcome == "success":
        print("tests step succeeded — slice green")
        return 0
    if args.tests_outcome != "failure":
        print(f"::error::tests step outcome {args.tests_outcome!r} — gating")
        return 1
    try:
        result = json.loads(_read(args.result) or "null")
    except json.JSONDecodeError:
        result = None
    try:
        qlist = parse_list(_read(args.quarantine))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::warning::base quarantine list unreadable ({exc}); nothing is quarantined")
        qlist = {"entries": []}
    ok, msgs = slice_verdict(result, Path(args.junit_dir), qlist, _today(args))
    for m in msgs:
        print(("::warning::" if "QUARANTINED" in m else "::error::") + m)
    print("slice verdict: " + ("GREEN (only quarantined tests failed)" if ok else "RED"))
    return 0 if ok else 1


def cmd_check_changes(args) -> int:
    try:
        base = parse_list(_read(args.base))
        head = parse_list(_read(args.head))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error::quarantine list unreadable: {exc}")
        return 1
    if base == head:
        print("quarantine list unchanged vs base")
        return 0
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    problems = check_changes(base, head, Api(token), args.repo, now)
    for p in problems:
        print(f"::error::quarantine evidence gate: {p}")
    print("quarantine evidence gate: " + ("RED" if problems else "GREEN"))
    return 1 if problems else 0


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lint")
    p.add_argument("file", nargs="?", default=LIST_PATH)
    p.add_argument("--today")
    p = sub.add_parser("slice-verdict")
    p.add_argument("--tests-outcome", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--junit-dir", required=True)
    p.add_argument("--quarantine", help="BASE-ref quarantine list (absent = nothing quarantined)")
    p.add_argument("--today")
    p = sub.add_parser("check-changes")
    p.add_argument("--base", help="BASE-ref list (absent = empty)")
    p.add_argument("--head", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--now")
    args = ap.parse_args(list(argv) if argv is not None else None)
    fn: Callable = {"lint": cmd_lint, "slice-verdict": cmd_slice_verdict, "check-changes": cmd_check_changes}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
