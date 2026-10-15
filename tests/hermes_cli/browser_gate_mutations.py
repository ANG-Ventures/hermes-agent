#!/usr/bin/env python3
"""t_42728faf mutation arms for the browser resolution gate (AC-1.15 x3, AC-1.13 x1, pass-3 lens x1).

Each arm rewrites ONE source line in a temp copy of the tree, runs the gate test file there, and must go
RED. Prints one line per arm. Run from the hermes-agent checkout root:
    ~/.hermes/scripts/test-gate <venv python> tests/hermes_cli/browser_gate_mutations.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST = "tests/hermes_cli/test_browser_resolution_gate.py"
ARMS = [  # (name, file, old line, new line, the test that must go red)
    ("ac115_remove_migrated_lanes_check", "hermes_cli/kanban_browser_gate.py",
     "    if lane == kn.LANE_NONE or lane not in registry.migrated:\n",
     "    if False:\n", "test_ac115_i_ii_unmigrated_lanes_are_placed_exactly_as_today"),
    ("ac115_remove_policy_check", "hermes_cli/kanban_browser_gate.py",
     "    if not assignee or assignee not in _names(pol.get(klass)):\n",
     "    if False:\n", "test_ac115_iii_a_claimed_lane_does_not_grant"),
    ("ac115_lane_of_first_skill_unconditionally", "hermes_cli/kanban_needs.py",
     "        if isinstance(s, str) and skill_key(s) in names:\n",
     "        if isinstance(s, str):\n", "test_ac115_iv_lane_of_is_the_first_registered_skill"),
    ("ac115v_remove_creator_provenance", "hermes_cli/kanban_browser_gate.py",
     "    if (minted_by or {}).get(\"class\") == \"worker\" and \"worker\" not in _names(pol.get(\"creators\")):\n",
     "    if False:\n", "test_ac115_v_worker_minted_card_is_refused_creator_provenance"),
    ("ac113_cache_mode_at_dispatcher_start", "hermes_cli/kanban_db_dispatch.py",
     "        reg = _kn.load_registry(_kb.kanban_home() / \"fleet\")\n",
     "        reg = _CACHED_REG.setdefault(\"r\", _kn.load_registry(_kb.kanban_home() / \"fleet\"))\n",
     "test_ac113_mode_off_is_today_and_flip_needs_no_restart"),
    ("lens_dry_run_bypasses_gate", "hermes_cli/kanban_db_dispatch.py",
     "    _browser_gate(conn, ready_rows, result, row_route, dry_run, default_assignee)\n",
     "    _browser_gate(conn, ready_rows, result, row_route, dry_run, default_assignee) if not dry_run else None\n",
     "test_dry_run_and_live_tick_call_the_same_placement_function"),
]


def main() -> int:
    py = sys.executable
    bad = 0
    for name, rel, old, new, target in ARMS:
        with tempfile.TemporaryDirectory(prefix="bgm-") as td:
            dst = Path(td) / "tree"
            shutil.copytree(ROOT, dst, symlinks=True,
                            ignore=shutil.ignore_patterns(".git", "node_modules", "venv", ".venv", "web",
                                                          "apps", "website", "ui-tui", "__pycache__"))
            f = dst / rel
            src = f.read_text()
            if src.count(old) != 1:
                print(f"ARM-BROKEN {name}: anchor count {src.count(old)}")
                bad += 1
                continue
            src = src.replace(old, new)
            if name == "ac113_cache_mode_at_dispatcher_start":
                src += "\n_CACHED_REG: dict = {}\n"
            f.write_text(src)
            r = subprocess.run([py, "-m", "pytest", TEST + "::" + target, "-q", "-p", "no:cacheprovider"], cwd=dst,
                               capture_output=True, text=True, timeout=600)
            tail = [l for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR")) or " passed" in l
                    or " failed" in l]
            # Prism 743be93ef223: RED only when the target test RAN and FAILED (pytest rc 1, summary "1 failed", no
            # error). rc 2/4/5 (collection/usage error, no such test) or an error in the summary is ARM-BROKEN.
            summary = next((l for l in reversed(r.stdout.splitlines()) if " in " in l and ("failed" in l or "passed" in l
                                                                                        or "error" in l)), "")
            ran_failed = r.returncode == 1 and "1 failed" in summary and "error" not in summary
            verdict = "RED" if ran_failed else ("GREEN(SURVIVED)" if r.returncode == 0 else "ARM-BROKEN")
            bad += not ran_failed
            print(f"{verdict:16} {name}: {' | '.join(tail[-2:])}")
    print(f"mutations: {len(ARMS) - bad}/{len(ARMS)} RED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
