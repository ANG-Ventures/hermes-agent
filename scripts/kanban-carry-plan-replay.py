#!/usr/bin/env python3
# SCHEDULE: manual (operator replay of the t_f3a411c2 background carry on real workspaces)
"""Read-only replay of the background workspace carry's plan (t_f3a411c2) on real workspaces.

For each workspace: the census (bytes, entries), the checkouts rebuilt on the pool host instead of copied as files,
and per rebuilt checkout the bundle size and how many files would travel by rsync. Nothing is copied, nothing is
written in the workspace or its git dirs (the bundle is built in a throwaway bare repo under $TMPDIR).

    python3 scripts/kanban-carry-plan-replay.py /Volumes/fleet-scratch/workspaces/default/t_fee50817 ...
"""

import json
import sys
import time

from hermes_cli import kanban_carry_background as kcb


def main(paths) -> int:
    rc = 0
    for ws in paths:
        t0 = time.monotonic()
        out = {"workspace": ws}
        try:
            repos: dict = {}
            out["bytes"], out["entries"] = kcb.census(ws, repos)
            rebuild = kcb.rebuilds_of(ws, sorted(repos))
            out["census_s"] = round(time.monotonic() - t0, 2)
            out["rebuilt"] = []
            for rel in rebuild:
                plan = kcb.plan_rebuild(ws, rel, rebuild)
                bundle, _idx = kcb.make_bundle(plan)
                out["rebuilt"].append({"rel": rel, "origin": plan["url"], "branch": plan["branch"],
                                       "head": plan["head"][:12], "stash": len(plan["stash"]),
                                       "bundle_bytes": len(bundle), "files": len(plan["files"]),
                                       "deleted": len(plan["deleted"])})
        except Exception as exc:  # report and continue with the next workspace
            out["error"] = f"{type(exc).__name__}: {exc}"
            rc = 1
        out["seconds"] = round(time.monotonic() - t0, 2)
        print(json.dumps(out), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
