"""pytest plugin for ``hermes_parity canary-lane``: record exact per-item outcomes.

Loaded with ``-p scripts.hermes_parity.canary_plugin --canary-report PATH``. A junit file loses the
nodeid (classname is ambiguous between a module and a class), so the lane reads this report instead:
the final collected item list plus one row per runtest phase.
"""

from __future__ import annotations

import json
from pathlib import Path

_STATE: dict = {"collected": [], "reports": [], "errors": []}


def pytest_addoption(parser):
    parser.addoption("--canary-report", default=None, help="write per-item canary outcomes to this JSON file")


def pytest_collection_finish(session):
    _STATE["collected"] = [item.nodeid for item in session.items]


def pytest_collectreport(report):
    if report.failed:
        _STATE["errors"].append({"nodeid": report.nodeid, "longrepr": str(report.longrepr)[-600:]})


def pytest_runtest_logreport(report):
    reason = ""
    if report.skipped:
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    elif report.failed:
        reason = str(report.longrepr)[-600:]
    _STATE["reports"].append({
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "xfail": hasattr(report, "wasxfail"),
        "reason": str(reason)[-600:],
    })


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--canary-report")
    if path:
        Path(path).write_text(json.dumps(_STATE), encoding="utf-8")
