#!/usr/bin/env python3
"""Evaluate results consumed by CI's all-checks-pass umbrella job."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any


_ALLOWED_RESULTS = {"success", "skipped"}


def _expected_jobs(
    needs: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[bool, str]]:
    detect = needs.get("detect", {}).get("outputs", {})
    supply_chain = needs.get("supply-chain", {}).get("outputs", {})
    pull_request = detect.get("event_name") == "pull_request"
    python = detect.get("python") == "true"
    frontend = detect.get("frontend") == "true"

    return {
        "tests": (python, "detect.outputs.python == 'true'"),
        "lint": (python, "detect.outputs.python == 'true'"),
        "js-tests": (frontend, "detect.outputs.frontend == 'true'"),
        # e2e-desktop is DELIBERATELY DISABLED in ci.yml (upstream #76627 — the
        # mock-backend Electron window never gets a title, so every spec fails
        # regardless of the diff; this branch takes upstream's apps/desktop
        # verbatim and inherits the breakage). Expecting it to run would make
        # the umbrella gate red on a skip we chose on purpose.
        #
        # This is NOT the hole #476 closed. That rule is "a required job must
        # not vanish UNNOTICED"; the exemption is declared here, in the file
        # that does the requiring, next to the `false &&` that causes the skip
        # -- so re-enabling the job means deleting this entry, and forgetting to
        # is a two-line diff away from being obvious. Delete both together when
        # upstream fixes #76627.
        "e2e-desktop": (
            False,
            "intentionally disabled pending upstream #76627",
        ),
        "docs-site": (
            detect.get("site") == "true",
            "detect.outputs.site == 'true'",
        ),
        "history-check": (
            pull_request,
            "detect.outputs.event_name == 'pull_request'",
        ),
        "contributor-check": (python, "detect.outputs.python == 'true'"),
        "lockfile-diff": (
            pull_request and detect.get("npm_lock") == "true",
            "pull_request and detect.outputs.npm_lock == 'true'",
        ),
        "docker-lint": (
            detect.get("docker_meta") == "true",
            "detect.outputs.docker_meta == 'true'",
        ),
        "supply-chain": (
            pull_request
            and (detect.get("scan") == "true" or detect.get("deps") == "true"),
            "pull_request and (detect.outputs.scan == 'true' or "
            "detect.outputs.deps == 'true')",
        ),
        "review-labels": (
            pull_request
            and (
                detect.get("ci_review") == "true"
                or detect.get("mcp_catalog") == "true"
                or supply_chain.get("critical_findings") == "true"
            ),
            "pull_request and a review-label trigger is true",
        ),
    }


def evaluate_needs(needs: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Return jobs whose results must fail the umbrella gate."""
    violations = {}
    for name, info in needs.items():
        result = info["result"]
        if result not in _ALLOWED_RESULTS:
            violations[name] = (
                f"{name} concluded {result!r}; expected 'success' or 'skipped'"
            )

    for name, (expected, reason) in _expected_jobs(needs).items():
        if expected and needs.get(name, {}).get("result") == "skipped":
            violations[name] = (
                f"classifier inconsistency: {name} was skipped even though {reason}"
            )
    return violations


def main() -> int:
    needs = json.load(sys.stdin)
    # Emit compact {job_name: result} for the comment assembler.
    compact = {name: info["result"] for name, info in needs.items()}
    output = f"needs-json={json.dumps(compact)}"
    print(output)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(output + "\n")

    failed = evaluate_needs(needs)
    for name, info in sorted(needs.items()):
        result = info["result"]
        icon = "✅" if name not in failed else "❌"
        print(f"{icon} {name}: {result}")
    if failed:
        for message in failed.values():
            print(f"::error::{message}")
        print(f"::error::{len(failed)} job(s) failed: {', '.join(failed)}")
        return 1
    print("All checks passed (or were skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
