#!/usr/bin/env python3
# SCHEDULE: manual (a CI step in each repo; nothing schedules it)
"""slots-vocabulary-lint: no NEW `slot`/`slots` token in the fleet-work-allocation code paths (t_21a7ace3).

Ace 2026-10-20 05:0x (voice msg 1562070136125394945): fleet work is admitted by measured utilization plus a
per-class estimate; the word "slots" leaves the allocation vocabulary. A count that is still a safety bound is a
CEILING; a capacity count is gone. Same bytes in hermes-home, hermes-agent, prism-router and
fleet-ops-scripts; each repo carries its own ``scripts/lints/slots-vocabulary.json``:

  paths      git globs of the allocation code paths this lint grades
  allow      [{path, why}] files under ``paths`` whose slot words mean something else (PCIe/DIMM slot,
             ``__slots__``, a UI slot, a cron tick slot). Never graded.
  baseline   {path: n}: hits that existed when the lint landed. A file may hold AT MOST its baseline;
             a file not listed holds 0. The baseline only shrinks: with ``--base-ref`` every row is also
             graded against the base copy, so a PR cannot raise a number or add a row.

A token is ``slot``/``slots`` as its own word part: ``max_slots``, ``no_slots:``, ``SLOTS``, ``totalSlots`` hit;
``unsloth`` and ``slotted`` do not. ``__slots__`` and a line carrying ``slots-vocab: allow <why>`` are skipped.

  python3 scripts/lints/slots-vocabulary-lint.py [--config P] [--base-ref REF]   rc 0 green, 1 red, 2 bad config
  python3 scripts/lints/slots-vocabulary-lint.py --selftest
  python3 scripts/lints/slots-vocabulary-lint.py --print-counts     current per-file counts (to lower a baseline)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_REL = "scripts/lints/slots-vocabulary.json"
TOKEN = re.compile(r"(?<![A-Za-z])[Ss][Ll][Oo][Tt][Ss]?(?![a-z])|(?<=[a-z])Slots?(?![a-z])")
SKIP_TOKEN = re.compile(r"__slots__")
PRAGMA = re.compile(r"slots-vocab:\s*allow\s+\S")


def _scrub_git_env() -> dict:
    """os.environ without the inherited git env (GIT_DIR/GIT_COMMON_DIR/GIT_WORK_TREE/GIT_INDEX_FILE): the selftest
    fixture repo must never re-root onto a caller's repo or index (git-env-scrub)."""
    e = dict(os.environ)
    for k in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        e.pop(k, None)
    return e


def count_line(line: str) -> int:
    if PRAGMA.search(line):
        return 0
    return len(TOKEN.findall(SKIP_TOKEN.sub("", line)))


def count_text(text: str) -> int:
    return sum(count_line(ln) for ln in text.splitlines())


def _match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def load_config(doc) -> tuple:
    if not isinstance(doc, dict):
        raise ValueError("config must be an object")
    paths = doc.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("paths must be a non-empty list of globs")
    allow = doc.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(a, dict) and isinstance(a.get("path"), str)
                                              and isinstance(a.get("why"), str) and a["why"].strip()
                                              for a in allow):
        raise ValueError("allow must be a list of {path, why} with a non-empty why")
    base = doc.get("baseline", {})
    if not isinstance(base, dict) or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
                                             for v in base.values()):
        raise ValueError("baseline must map path -> int > 0 (drop a row that reaches 0)")
    return paths, [a["path"] for a in allow], base


def tracked(root: Path) -> list:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                         env=_scrub_git_env()).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def counts(root: Path, paths, allow) -> dict:
    got = {}
    for rel in tracked(root):
        if not _match(rel, paths) or _match(rel, allow):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        n = count_text(text)
        if n:
            got[rel] = n
    return got


def grade(got: dict, baseline: dict, base_baseline=None) -> list:
    errs = []
    for rel, n in sorted(got.items()):
        cap = baseline.get(rel, 0)
        if n > cap:
            errs.append(f"{rel}: {n} slots token(s), baseline {cap}: a new one in an allocation path. Name a "
                        f"safety bound `ceiling`, delete a capacity count; a different meaning goes in `allow`")
    if base_baseline is not None:
        for rel, n in sorted(baseline.items()):
            was = base_baseline.get(rel, 0)
            if n > was:
                errs.append(f"baseline {rel}: {n} > base {was}: the baseline only shrinks")
    return errs


def notes(got: dict, baseline: dict) -> list:
    return [f"note: {rel}: {got.get(rel, 0)} < baseline {n}; lower the row (or drop it at 0)"
            for rel, n in sorted(baseline.items()) if got.get(rel, 0) < n]


def _base_baseline(root: Path, ref: str, rel: str):
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"], capture_output=True, env=_scrub_git_env())
    if r.returncode != 0:
        return None                      # bootstrap: the base has no config yet
    return load_config(json.loads(r.stdout))[2]


def run(root: Path, config_rel: str, base_ref=None, out=print) -> int:
    try:
        paths, allow, baseline = load_config(json.loads((root / config_rel).read_text(encoding="utf-8")))
        base = _base_baseline(root, base_ref, config_rel) if base_ref else None
    except (OSError, ValueError) as exc:
        out(f"slots-vocabulary-lint: bad config {config_rel}: {exc}")
        return 2
    got = counts(root, paths, allow)
    errs = grade(got, baseline, base)
    for n in notes(got, baseline):
        out(n)
    for e in errs:
        out(f"FAIL {e}")
    out(f"slots-vocabulary-lint: {'RED' if errs else 'green'}: {sum(got.values())} grandfathered token(s) "
        f"in {len(got)} file(s), {len(errs)} finding(s)")
    return 1 if errs else 0


def selftest() -> int:
    hits = {"max_slots = 3": 1, "no_slots:kanban": 1, "SLOTS=4": 1, "pool.totalSlots": 1, "slot cap": 1,
            "unsloth docs": 0, "__slots__ = ()": 0, "slotted": 0, "x = slots  # slots-vocab: allow PCIe": 0,
            "ceiling 24": 0, "free_slots slot": 2}
    bad = {s: (count_line(s), n) for s, n in hits.items() if count_line(s) != n}
    if bad:
        print(f"selftest FAIL token counts: {bad}")
        return 1
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=_scrub_git_env())
        (root / "scripts/lints").mkdir(parents=True)
        (root / "alloc").mkdir()
        (root / "alloc/a.py").write_text("max_slots = 1\n")
        (root / "alloc/pcie.py").write_text("PCIe slot 3\n")
        (root / "other.py").write_text("slots = 9\n")
        cfg = {"paths": ["alloc/*"], "allow": [{"path": "alloc/pcie.py", "why": "PCIe slot"}],
               "baseline": {"alloc/a.py": 1}}
        (root / CONFIG_REL).write_text(json.dumps(cfg))
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=_scrub_git_env())
        quiet = lambda *_: None
        cases = [("green at baseline", 0)]
        rc = [run(root, CONFIG_REL, out=quiet)]
        (root / "alloc/a.py").write_text("max_slots = 1\nlive_slots = 2\n")
        rc.append(run(root, CONFIG_REL, out=quiet)); cases.append(("a new token is red", 1))
        (root / "alloc/a.py").write_text("ceiling = 1\n")
        rc.append(run(root, CONFIG_REL, out=quiet)); cases.append(("below baseline is green", 0))
        (root / "alloc/b.py").write_text("slot\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=_scrub_git_env())
        rc.append(run(root, CONFIG_REL, out=quiet)); cases.append(("a new file is red", 1))
        subprocess.run(["git", "-C", str(root), "rm", "-q", "--cached", "alloc/b.py"], check=True, env=_scrub_git_env())
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                        "base"], check=True, env=_scrub_git_env())
        cfg["baseline"]["alloc/a.py"] = 5
        (root / CONFIG_REL).write_text(json.dumps(cfg))
        rc.append(run(root, CONFIG_REL, base_ref="HEAD", out=quiet)); cases.append(("raising the baseline is red", 1))
        (root / CONFIG_REL).write_text("{\"paths\": []}")
        rc.append(run(root, CONFIG_REL, out=quiet)); cases.append(("bad config is rc 2", 2))
    bad = [(c, want, got) for (c, want), got in zip(cases, rc) if want != got]
    if bad:
        print(f"selftest FAIL: {bad}")
        return 1
    print(f"selftest ok: {len(hits)} token cases, {len(cases)} grading cases")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=CONFIG_REL)
    ap.add_argument("--root", default=None, help="repo root (default: git toplevel of cwd)")
    ap.add_argument("--base-ref", default=None, help="grade the baseline against this ref's copy (PR base)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--print-counts", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    root = Path(a.root or subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                                         check=True).stdout.strip())   # the caller's repo: env kept on purpose
    if a.print_counts:
        paths, allow, _ = load_config(json.loads((root / a.config).read_text(encoding="utf-8")))
        print(json.dumps(dict(sorted(counts(root, paths, allow).items())), indent=2))
        return 0
    return run(root, a.config, a.base_ref)


if __name__ == "__main__":
    sys.exit(main())
