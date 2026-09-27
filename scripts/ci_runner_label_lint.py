#!/usr/bin/env python3
"""Fail any workflow job whose ``runs-on`` names a label this org cannot serve.

GitHub queues a job with an unknown label forever instead of failing it. On
2026-09-25, 7 CI runs (63 jobs) sat queued for hours: they asked for
upstream's paid larger runners (``ubuntu-latest-32-core``,
``ubuntu-latest-96-core``, ``windows-latest-32-core``,
``windows-latest-32-arm-core``). The ANG-Ventures org has no such pools.

The lint reads every label a job can resolve to:

* literal ``runs-on`` strings, lists and ``labels:`` mappings;
* ``${{ matrix.<key> }}``, which expands to the job's matrix values;
* ``${{ inputs.<name> }}``, which expands to the input's default, its
  ``options``, and every ``with: <name>:`` value a caller in this repo passes;
* quoted tokens inside any other expression that are runner-label shaped
  (``ubuntu-*``, ``windows-*``, ``macos-*``, ``blacksmith-*``, ``*-core``),
  including JSON arrays such as ``'["ubuntu-latest"]'``.

A job is exempt when its ``if:`` pins it to the upstream repository
(``github.repository == 'NousResearch/hermes-agent'``). Such a job never
schedules here, so its upstream labels are harmless.

Usage::

    python scripts/ci_runner_label_lint.py                  # working tree
    python scripts/ci_runner_label_lint.py --git-ref REF    # a branch/sha, e.g.
                                                            # before dispatching CI on it

Exit 0 when clean, 1 on any unservable label.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

WORKFLOW_DIR = ".github/workflows"
UPSTREAM_REPO = "NousResearch/hermes-agent"

# Standard GitHub-hosted labels (no larger-runner pools exist in this org).
GITHUB_HOSTED = frozenset(
    {
        "ubuntu-latest",
        "ubuntu-24.04",
        "ubuntu-22.04",
        "ubuntu-24.04-arm",
        "ubuntu-22.04-arm",
        "ubuntu-slim",
        "windows-latest",
        "windows-2025",
        "windows-2022",
        "windows-11-arm",
        "macos-latest",
        "macos-15",
        "macos-14",
        "macos-13",
        "macos-15-intel",
    }
)
# Blacksmith runner groups installed on the org.
BLACKSMITH = re.compile(r"^blacksmith-\d+vcpu-ubuntu-(2204|2404)(-arm)?$")
# Self-hosted pool labels (opt-in lanes; CI_RUNNER_LABELS / placement plan).
SELF_HOSTED = frozenset({"self-hosted", "linux", "Linux", "X64", "ARM64", "hermes-ci"})

# Quoted tokens inside expressions that look like a runner label.
LABEL_SHAPE = re.compile(r"^(ubuntu|windows|macos|blacksmith)-[A-Za-z0-9._-]+$|^[A-Za-z0-9._-]+-core$")
QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}$")
INPUT_REF = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")
UPSTREAM_GUARD = re.compile(r"github\.repository\s*==\s*'" + re.escape(UPSTREAM_REPO) + "'")


def servable(label: str) -> bool:
    return label in GITHUB_HOSTED or label in SELF_HOSTED or bool(BLACKSMITH.match(label))


def _expression_tokens(text: str) -> list[str]:
    out: list[str] = []
    for a, b in QUOTED.findall(text):
        tok = a or b
        if tok.startswith("["):
            try:
                arr = json.loads(tok)
            except ValueError:
                arr = None
            if isinstance(arr, list):
                out.extend(x for x in arr if isinstance(x, str) and "{" not in x)
                continue
        if LABEL_SHAPE.match(tok):
            out.append(tok)
    return out


def _matrix_values(job: dict, key: str) -> list:
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if not isinstance(matrix, dict):
        return []
    vals: list = []
    direct = matrix.get(key)
    if isinstance(direct, list):
        vals.extend(direct)
    elif direct is not None:
        vals.append(direct)
    for row in matrix.get("include") or []:
        if isinstance(row, dict) and key in row:
            vals.append(row[key])
    return vals


def _on(wf: dict) -> dict:
    # PyYAML parses the bare key ``on`` as boolean True.
    on = wf.get("on", wf.get(True)) or {}
    return on if isinstance(on, dict) else {}


def _input_values(name: str, path: str, wf: dict, workflows: dict[str, dict]) -> list:
    vals: list = []
    on = _on(wf)
    for trig in ("workflow_call", "workflow_dispatch"):
        spec = ((on.get(trig) or {}).get("inputs") or {}).get(name) or {}
        if "default" in spec:
            vals.append(spec["default"])
        vals.extend(spec.get("options") or [])
    fname = Path(path).name
    for other in workflows.values():
        for job in (other.get("jobs") or {}).values():
            uses = job.get("uses") if isinstance(job, dict) else None
            if isinstance(uses, str) and uses.endswith("/" + fname):
                with_ = job.get("with") or {}
                if name in with_:
                    v = with_[name]
                    m = MATRIX_REF.match(v) if isinstance(v, str) else None
                    vals.extend(_matrix_values(job, m.group(1)) if m else [v])
    return vals


def _labels(value, job: dict, path: str, wf: dict, workflows: dict[str, dict]) -> list[str]:
    """Every concrete label ``value`` can resolve to."""
    if isinstance(value, list):
        return [lbl for v in value for lbl in _labels(v, job, path, wf, workflows)]
    if isinstance(value, dict):
        # ``runs-on: {group: ..., labels: ...}``; runner groups are not labels.
        return _labels(value.get("labels") or [], job, path, wf, workflows)
    if not isinstance(value, str):
        return []
    s = value.strip()
    if "${{" not in s:
        return [s]
    m = MATRIX_REF.match(s)
    if m:
        return [lbl for v in _matrix_values(job, m.group(1)) for lbl in _labels(v, job, path, wf, workflows)]
    m = INPUT_REF.match(s)
    if m:
        vals = _input_values(m.group(1), path, wf, workflows)
        return [lbl for v in vals for lbl in _labels(v, job, path, wf, workflows)]
    return _expression_tokens(s)


def lint(workflows: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    for path, wf in sorted(workflows.items()):
        for job_id, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict) or "runs-on" not in job:
                continue
            if UPSTREAM_GUARD.search(str(job.get("if", ""))):
                continue
            bad = sorted({lbl for lbl in _labels(job["runs-on"], job, path, wf, workflows) if not servable(lbl)})
            for lbl in bad:
                errors.append(f"{path}: job {job_id!r} can request runner label {lbl!r}, which this org cannot serve")
    return errors


def load_tree(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((root / WORKFLOW_DIR).glob("*.y*ml")):
        out[f"{WORKFLOW_DIR}/{p.name}"] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return out


def load_ref(ref: str) -> dict[str, dict]:
    names = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{ref}:{WORKFLOW_DIR}"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    out = {}
    for name in names:
        if name.endswith((".yml", ".yaml")):
            body = subprocess.run(
                ["git", "show", f"{ref}:{WORKFLOW_DIR}/{name}"],
                check=True, capture_output=True, text=True,
            ).stdout
            out[f"{WORKFLOW_DIR}/{name}"] = yaml.safe_load(body) or {}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--git-ref", help="lint the workflows at this git ref instead of the working tree")
    args = ap.parse_args(argv)
    workflows = load_ref(args.git_ref) if args.git_ref else load_tree(Path(args.root))
    errors = lint(workflows)
    for e in errors:
        print(f"ERROR {e}")
    print(f"ci_runner_label_lint: {len(workflows)} workflows, {len(errors)} unservable label reference(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
