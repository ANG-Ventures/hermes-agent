#!/usr/bin/env python3
"""workflow-trigger-storm-lint: refuse the CI shapes that inflate RUN COUNT (what a platform's abuse
heuristics see) without doing work (ci-redundancy-audit class I, card t_8ca8c919).

WHY: a `workflow_run:` listener fires once per upstream run and decides at the JOB `if:` that there is
nothing to do. Each of those is a run GitHub counts. Measured 2026-10-04 on ANG-Ventures/hermes-agent:
1,039 `Fleet CI-fail alert` runs, 967 skipped = 38 % of the 2,762 runs/day before the github-staff
disable (Support #4838502). hermes-home 2026-10-09: 780 `CI-fail alert` runs, 772 skipped.

RULES (one line per finding; exit 1 on any, 0 clean, 2 unreadable):
  1. A `workflow_run` listener needs ALL of: `types: [completed]` and nothing else (`requested` /
     `in_progress` each create a run before any conclusion exists), at most 3 upstream workflows, and a
     first job whose `if:` tests `workflow_run.conclusion`. Or the `workflow_run:` line carries
     `# trigger-storm-ok: <why>`.
  2. A `pull_request` workflow needs a `paths:`/`paths-ignore:` filter or a workflow-level
     `concurrency:` group (else every push of every PR runs it and nothing cancels the stale run).
     Or the `pull_request:` line carries `# no-paths-ok: <why>`.

  workflow-trigger-storm-lint [--repo DIR] [--json] [--selftest]

ONE FILE, TWO REPOS: hermes-home scripts/workflow-trigger-storm-lint.py and hermes-agent
scripts/ci/workflow_trigger_storm_lint.py are byte-identical; each repo's test pins this file's sha256
(LINT_SHA256). Change both in the same pair of PRs.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    print("workflow-trigger-storm-lint: pyyaml missing", file=sys.stderr)
    sys.exit(2)

MARK_STORM = re.compile(r"#\s*trigger-storm-ok:\s*\S")
MARK_PATHS = re.compile(r"#\s*no-paths-ok:\s*\S")
MAX_UPSTREAM = 3


def _on(doc) -> dict:
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None  # YAML 1.1: bare `on` is True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(k): None for k in on}
    return on if isinstance(on, dict) else {}


def _key_line(text: str, key: str) -> str:
    """The source line of the `key` event INSIDE the top-level `on:` (its mapping key, or its item in the
    list / scalar form), so a marker elsewhere (e.g. in a `run: |` script) never exempts the trigger."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return ""
    if not isinstance(root, yaml.MappingNode):
        return ""
    on = next((v for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value in ("on", "true", "True")), None)
    if isinstance(on, yaml.MappingNode):
        nodes = [k for k, _v in on.value if isinstance(k, yaml.ScalarNode) and k.value == key]
    elif isinstance(on, yaml.SequenceNode):
        nodes = [x for x in on.value if isinstance(x, yaml.ScalarNode) and x.value == key]
    elif isinstance(on, yaml.ScalarNode) and on.value == key:
        nodes = [on]
    else:
        nodes = []
    lines = text.splitlines()
    return lines[nodes[0].start_mark.line] if nodes and nodes[0].start_mark.line < len(lines) else ""


def lint_text(path: str, text: str) -> list[str]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return ["%s: unparseable YAML: %s" % (path, e.__class__.__name__)]
    on = _on(doc)
    jobs = (doc.get("jobs") or {}) if isinstance(doc, dict) else {}
    out = []
    if "workflow_run" in on and not MARK_STORM.search(_key_line(text, "workflow_run")):
        wr = on.get("workflow_run") or {}
        ups = wr.get("workflows") or [] if isinstance(wr, dict) else []
        ups = [ups] if isinstance(ups, str) else list(ups)  # `workflows: Build` is one upstream
        types = wr.get("types") or [] if isinstance(wr, dict) else []
        types = [types] if isinstance(types, str) else list(types)
        first = next(iter(jobs.values()), {}) if isinstance(jobs, dict) and jobs else {}
        cond = str((first or {}).get("if") or "")
        why = []
        if types != ["completed"]:
            why.append("`types:` is %s, not exactly `[completed]`" % (types or "unset (every activity type)"))
        if len(ups) > MAX_UPSTREAM:
            why.append("listens to %d upstream workflows (max %d): one run per upstream run" % (len(ups), MAX_UPSTREAM))
        if "workflow_run.conclusion" not in cond:
            why.append("first job `if:` does not test workflow_run.conclusion (decides after the run exists)")
        if why:
            out.append("%s: workflow_run listener is a trigger storm: %s. Fold the check into the upstream "
                       "workflow's `if: failure()` job or batch it in one scheduled sweep; or mark the "
                       "`workflow_run:` line `# trigger-storm-ok: <why>`" % (path, "; ".join(why)))
    if "pull_request" in on:
        pr = on.get("pull_request")
        has_paths = isinstance(pr, dict) and ("paths" in pr or "paths-ignore" in pr)
        has_conc = isinstance(doc, dict) and "concurrency" in doc
        if not has_paths and not has_conc and not MARK_PATHS.search(_key_line(text, "pull_request")):
            out.append("%s: pull_request with no `paths:` and no `concurrency:`: every push of every PR runs it "
                       "and nothing cancels the stale run; add `paths:` (what the jobs read) or a "
                       "`concurrency:` group with cancel-in-progress, or mark `# no-paths-ok: <why>`" % path)
    return out


def findings(repo: pathlib.Path) -> list[str]:
    wf = repo / ".github" / "workflows"
    if not wf.is_dir():  # a mistyped --repo must not read as clean (exit 2)
        raise OSError("no workflows directory: %s" % wf)
    out = []
    for p in sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml"))):
        out += lint_text(str(p.relative_to(repo)), p.read_text(encoding="utf-8-sig"))
    return out


SELFTEST = {  # name -> (workflow text, expect a finding)
    "storm.yml": ("on:\n  workflow_run:\n    workflows: [A, B, C, D]\n    types: [completed]\n"
                  "jobs:\n  j:\n    if: github.event.workflow_run.conclusion == 'failure'\n", True),
    "no-cond.yml": ("on:\n  workflow_run:\n    workflows: [A]\n    types: [completed]\njobs:\n  j:\n    if: true\n", True),
    "extra-types.yml": ("on:\n  workflow_run:\n    workflows: [A]\n    types: [completed, requested, in_progress]\n"
                        "jobs:\n  j:\n    if: github.event.workflow_run.conclusion == 'failure'\n", True),
    "lean.yml": ("on:\n  workflow_run:\n    workflows: [A]\n    types: [completed]\n"
                 "jobs:\n  j:\n    if: github.event.workflow_run.conclusion == 'failure'\n", False),
    "marked.yml": ("on:\n  workflow_run:  # trigger-storm-ok: tracks green runs too\n    workflows: [A, B, C, D]\n"
                   "jobs:\n  j:\n    if: true\n", False),
    "bare-pr.yml": ("on:\n  pull_request:\njobs: {}\n", True),
    "pr-paths.yml": ("on:\n  pull_request:\n    paths: ['x/**']\njobs: {}\n", False),
    "pr-conc.yml": ("on: [pull_request]\nconcurrency:\n  group: g\n  cancel-in-progress: true\njobs: {}\n", False),
    "pr-marked.yml": ("on:\n  pull_request:  # no-paths-ok: reads the PR body\n    types: [edited]\njobs: {}\n", False),
    "marker-in-script.yml": ("on: [pull_request]\njobs:\n  j:\n    steps:\n      - run: |\n"
                             "          pull_request: # no-paths-ok: example\n", True),
    "sweep.yml": ("on:\n  schedule:\n    - cron: '*/15 * * * *'\njobs: {}\n", False),
}


def selftest() -> int:
    with tempfile.TemporaryDirectory() as d:
        wf = pathlib.Path(d) / ".github" / "workflows"
        wf.mkdir(parents=True)
        for name, (text, _bad) in SELFTEST.items():
            (wf / name).write_text(text, encoding="utf-8")
        got = findings(pathlib.Path(d))
    bad = []
    for name, (_text, want) in SELFTEST.items():
        hit = any(f.startswith(".github/workflows/%s:" % name) for f in got)
        if hit != want:
            bad.append("%s: expected %s, got %s" % (name, "finding" if want else "clean", "finding" if hit else "clean"))
    print("selftest:", "ok" if not bad else "FAIL " + "; ".join(bad))
    return 1 if bad else 0


def _repo_root() -> pathlib.Path:
    """The nearest ancestor of this file that holds .github/ (scripts/ in one repo, scripts/ci/ in the other)."""
    here = pathlib.Path(__file__).resolve().parent
    return next((d for d in (here, *here.parents) if (d / ".github").is_dir()), here)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=pathlib.Path, default=_repo_root())
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    try:
        found = findings(a.repo)
    except OSError as e:
        print("workflow-trigger-storm-lint: unreadable: %s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"findings": found}, indent=1))
    else:
        for f in found:
            print("FAIL trigger-storm: " + f)
        print("workflow-trigger-storm-lint: %d finding(s)" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
