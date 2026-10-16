#!/usr/bin/env python3
"""Vendored Router Core lints match the hermes-home bytes recorded in PIN.json (card t_8b05b16b).

The three lints are byte-identical copies of ANG-Ventures/hermes-home files at the pinned commits (hermes-home is
private, so CI here cannot fetch them). A local edit here would fork the lint silently; change it in hermes-home,
then re-copy and re-pin. Exit 0 when every file's sha256 matches, 1 otherwise.
"""
import hashlib
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
pin = json.loads((here / "PIN.json").read_text(encoding="utf-8"))
bad = 0
for name, row in sorted(pin["files"].items()):
    got = hashlib.sha256((here / name).read_bytes()).hexdigest()
    ok = got == row["sha256"]
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name} == {pin['source_repo']}@{row['source_sha'][:12]}:{row['source_path']}"
          + ("" if ok else f" (sha256 {got[:16]} != pinned {row['sha256'][:16]})"))
if not pin["files"]:
    print("FAIL PIN.json lists no files")
    bad = 1
sys.exit(1 if bad else 0)
