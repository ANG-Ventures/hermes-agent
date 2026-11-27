#!/usr/bin/env python3
# SCHEDULE: manual (fixture benchmark invoked by implementer/reviewer)
"""Fixture-only skill_manage call-count benchmark: main's _find_skill vs the per-call memo.

Builds 2,000 skills (40 categories, each skill with a references/ dir) in a temp home and runs
the same six public operations under each arm. Both arms use the same dispatcher; the main arm
binds main's ``_find_skill`` (tests/tools/review171_legacy_lookup.py), so they differ only by
the memo. Counts are deterministic; thread CPU is indicative. Approval staging, sync push and
the cross-profile scan are isolated on both arms. Not a production latency claim.
"""
import json
import os
import sys
import tempfile
import time
import types
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools.skill_manager_tool as smt  # noqa: E402
from tests.tools import review171_legacy_lookup as legacy  # noqa: E402

_CONTENT = "---\nname: {n}\ndescription: Use when testing. Does a thing.\n---\n\n# x\n\nold line\n"
_OPS = [
    ("patch hit", dict(action="patch", name="sk17-21", old_string="old line", new_string="new line")),
    ("patch miss", dict(action="patch", name="absent", old_string="a", new_string="b")),
    ("create", dict(action="create", name="fresh", category="cat0", local=True,
                    content=_CONTENT.format(n="fresh"))),
    ("delete", dict(action="delete", name="fresh")),
    ("write_file", dict(action="write_file", name="sk3-7", file_path="references/b.md", file_content="b")),
    ("batch 3 patches", dict(action="patch", name="sk1-1", operations=[
        {"action": "patch", "name": n, "old_string": "old line", "new_string": "new line"}
        for n in ("sk1-1", "sk2-2", "sk4-4")])),
]


def _counting(stack, counts):
    for mod, attr in [(os, "scandir"), (os, "stat"), (os, "lstat")]:
        real = getattr(mod, attr)
        real = getattr(real, "__wrapped__", real)  # count real calls, not the test harness guard

        def make(real, key):
            def wrapper(*a, **k):
                counts[key] += 1
                return real(*a, **k)
            return wrapper
        stack.enter_context(patch.object(mod, attr, make(real, attr)))


def run(arm, home):
    root = home / "skills"
    for c in range(40):
        for s in range(50):
            d = root / f"cat{c}" / f"sk{c}-{s}"
            (d / "references").mkdir(parents=True)
            (d / "SKILL.md").write_text(_CONTENT.format(n=d.name), encoding="utf-8")
            (d / "references" / "r.md").write_text("r", encoding="utf-8")
    (root / ".locks").mkdir()
    rows = {}
    with ExitStack() as stack:
        for target, value in [("SKILLS_DIR", root), ("HERMES_HOME", home)]:
            stack.enter_context(patch.object(smt, target, value))
        stack.enter_context(patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]))
        for symbol, rv in [("_apply_skill_write_gate", None), ("_find_skill_in_other_profiles", []),
                           ("_maybe_debounced_sync_push", None)]:
            stack.enter_context(patch.object(smt, symbol, return_value=rv))
        if arm == "main":
            spy = stack.enter_context(patch.object(smt, "_find_skill", side_effect=types.FunctionType(
                legacy._find_skill.__code__, smt.__dict__)))
        else:
            spy = stack.enter_context(patch.object(smt, "_discover_skill", wraps=smt._discover_skill))
        json.loads(smt.skill_manage(action="patch", name="sk0-0", old_string="old line",
                                    new_string="new line"))  # warm imports, outside measurement
        for label, kw in _OPS:
            counts = Counter()
            before = spy.call_count
            with ExitStack() as inner:
                _counting(inner, counts)
                t = time.thread_time()
                out = json.loads(smt.skill_manage(**kw))
                cpu = time.thread_time() - t
            rows[label] = dict(success=out.get("success"), discoveries=spy.call_count - before,
                               scandir=counts["scandir"], stat=counts["stat"], lstat=counts["lstat"],
                               cpu_ms=round(cpu * 1000, 1))
    return rows


def main():
    results = {}
    for arm in ("main", "head"):
        with tempfile.TemporaryDirectory(prefix=f"skill-memo-bench-{arm}-") as td:
            results[arm] = run(arm, Path(td))
    print(f"{'operation':<17} {'arm':<5} {'ok':<5} {'discoveries':>11} {'scandir':>8} "
          f"{'stat':>7} {'lstat':>7} {'cpu_ms':>8}")
    for label, _ in _OPS:
        for arm in ("main", "head"):
            r = results[arm][label]
            print(f"{label:<17} {arm:<5} {str(r['success']):<5} {r['discoveries']:>11} {r['scandir']:>8} "
                  f"{r['stat']:>7} {r['lstat']:>7} {r['cpu_ms']:>8}")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
