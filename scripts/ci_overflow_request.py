#!/usr/bin/env python3
"""Generate-side CI overflow outputs: the all-local matrix and the placement request.

Runs in tests.yml's ``generate`` job right after ``run_tests_parallel.py
--generate-slices``. Slice membership is computed ONCE by that generator; this
script only relabels (``local_matrix``) and summarises (request artifact). The
request carries slice ids, the core flag and duration weights — never file
lists, test contents or shell (spec §5.1, parsed by ci_overflow_plan.parse_request).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci_overflow_plan import POOL  # noqa: E402
from scripts import run_tests_parallel as rtp  # noqa: E402

LOCAL_RUNS_ON = json.dumps(POOL, separators=(",", ":"))
E2E_JOB_ID = "e2e"
LEGACY_NOTE = "policy=legacy, excluded_from_overflow_budget"


def local_matrix(matrix: dict) -> dict:
    """The identical matrix with every slice pinned to the approved X64 pool."""
    out = copy.deepcopy(matrix)
    for row in out["slice"]:
        row["runs_on"] = LOCAL_RUNS_ON
    return out


def _weight(files, durations) -> float:
    # Same per-file 2.0 s fallback as the generator's LPT slicing.
    return round(sum(durations.get(f, 2.0) for f in files), 3)


def is_core(row: dict) -> bool:
    # Same core definition _route_arm_slices uses to keep core off ARM.
    return row["name"] == "core smoke" or bool(set(rtp._CORE_SMOKE_TESTS).intersection(rtp._split_pathspec(row["files"])))


def build_request(matrix: dict, durations: dict, e2e_files: list[str]) -> dict:
    slices = [{
        "job_id": row["name"],
        "core": is_core(row),
        "estimated_duration_s": _weight(rtp._split_pathspec(row["files"]), durations),
    } for row in matrix["slice"]]
    return {"slices": slices, "e2e": {"job_id": E2E_JOB_ID, "estimated_duration_s": _weight(e2e_files, durations)}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-file", type=Path, required=True, help="generate's matrix JSON file")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--event", default="", help="github.event_name")
    parser.add_argument("--managed", default="", help="explicit Phase-4 placement switch")
    args = parser.parse_args(argv)
    matrix = json.loads(args.matrix_file.read_text(encoding="utf-8"))
    durations = rtp._load_durations(ROOT)
    e2e_files = [rtp._format_file(p, ROOT) for p in rtp._discover_files([ROOT / "tests" / "e2e"])]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    request = build_request(matrix, durations, e2e_files)
    (args.out_dir / "request.json").write_text(json.dumps(request, separators=(",", ":")) + "\n", encoding="utf-8")
    (args.out_dir / "local_matrix.json").write_text(json.dumps(local_matrix(matrix)), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and (args.event != "merge_group" or args.managed != "true"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"CI overflow placement: `{LEGACY_NOTE}` (event `{args.event}` uses the original generate matrix).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
