#!/usr/bin/env python3
"""mem0 host-literal lint (PRD-portability AC5, card t_c0586226).

The fleet reaches the mem0 store by NAME (https://mem0.ace, from mem0.json). A loopback or LAN literal for the
store breaks on the next host move: on 2026-10-10 the clients that carried http://127.0.0.1:8888 kept writing
to the retired box after the DNS cutover. This lint fails on any tracked line that pairs a mem0 marker with a
literal store address:

  * a line naming mem0 (any case, incl. MEM0_* env names) that also carries http(s)://127.0.0.1, localhost,
    0.0.0.0, 10.x, 172.16-31.x or 192.168.x;
  * a `"host": "<literal url>"` / `host = <literal url>` line in a file whose path names mem0 (mem0.json shape).

Skipped: tests/, fixtures/, test_*.py / *_test.py, this lint and its allow file. A line carrying
`noqa: mem0-host-literal` is not counted. Whole files are allow-listed in the allow file
(`<path>  <reason>`, one per line, `#` comments); an entry without a reason fails, and an entry whose file
has no hit any more is reported so it gets removed.

Run: python3 scripts/mem0_host_literal_lint.py [--repo DIR] [--allow FILE]
Exit 0 clean, 1 on a finding, 2 on a bad allow file or a tracked file it cannot read. Last line: MEM0_HOST_LITERAL=ok|fail <n>.
Same file in hermes-agent and pipecat-house-voice; the deploy repo runs stack-tools/portability_lint.py.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
LITERAL = (r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
           r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")
LITERAL_RE = re.compile(LITERAL)
MEM0_RE = re.compile(r"mem0", re.I)
HOST_KEY_RE = re.compile(r"""["']?host["']?\s*[:=]\s*["']?""" + LITERAL, re.I)
NOQA = "noqa: mem0-host-literal"
SKIP_DIRS = {"tests", "test", "fixtures", "__tests__"}


WALK_SKIP = {".git", "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build"}


def tracked(repo: Path) -> list[str]:
    """Tracked files of `repo`. Falls back to a filesystem walk when `repo` is not a git checkout
    (off-box CI ships the tree as `git archive | tar`, with no .git: the lint must still grade it, and
    it must grade THAT tree, never a parent checkout git happens to find above it)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
    r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       timeout=120)
    if r.returncode == 0:
        return [p for p in r.stdout.split("\0") if p]
    if "not a git repository" not in r.stderr.lower():
        raise RuntimeError(f"git ls-files rc={r.returncode}: {r.stderr.strip()[:200]}")
    out: list[str] = []
    def _raise(e: OSError) -> None:   # os.walk drops an unreadable directory silently by default
        raise e

    for dirpath, dirnames, filenames in os.walk(repo, onerror=_raise):
        dirnames[:] = sorted(d for d in dirnames if d not in WALK_SKIP)
        for f in filenames:
            out.append(str((Path(dirpath) / f).relative_to(repo)))
    return sorted(out)


def skipped(rel: str, own: set[str]) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (rel in own or any(p in SKIP_DIRS for p in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name.endswith((".png", ".jpg", ".ico", ".lock", ".svg")))


def scan(repo: Path, own: set[str]) -> dict[str, list[tuple[int, str]]]:
    hits: dict[str, list[tuple[int, str]]] = {}
    for rel in tracked(repo):
        if skipped(rel, own):
            continue
        p = repo / rel
        if p.is_dir():   # submodule gitlink: its own repo runs its own lint
            continue
        try:
            raw = p.read_bytes()
        except FileNotFoundError:   # tracked but deleted in the worktree (or a dangling symlink)
            continue
        # Any other read error propagates: an unread file must not be certified clean.
        if b"\0" in raw[:4096]:
            continue
        mem0_path = bool(MEM0_RE.search(rel))
        if not mem0_path and not MEM0_RE.search(raw.decode("latin-1")):
            continue
        for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if NOQA in line or not LITERAL_RE.search(line):
                continue
            if MEM0_RE.search(line) or (mem0_path and HOST_KEY_RE.search(line)):
                hits.setdefault(rel, []).append((i, line.strip()[:120]))
    return hits


def load_allow(path: Path) -> dict[str, str]:
    out = {}
    if not path.is_file():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, why = line.partition(" ")
        if not why.strip():
            raise ValueError(f"{path}:{n}: {rel} has no reason")
        out[rel] = why.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(HERE.parents[1]))
    ap.add_argument("--allow", default=str(HERE.with_name("mem0_host_literal_allow.txt")))
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    try:
        allow = load_allow(Path(a.allow))
        own = {str(Path(x).resolve().relative_to(repo.resolve())) for x in (HERE, a.allow)
               if Path(x).resolve().is_relative_to(repo.resolve())}
        hits = scan(repo, own)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"MEM0_HOST_LITERAL=error {e}", file=sys.stderr)
        return 2
    fails = 0
    for rel, lines in sorted(hits.items()):
        if rel in allow:
            continue
        for ln, text in lines:
            fails += 1
            print(f"FAIL {rel}:{ln} mem0 store reached by a literal address ({text!r}); use the fleet name "
                  f"(mem0.json host / https://mem0.ace) or allow-list the file with a reason")
    for rel in sorted(set(allow) - set(hits)):
        print(f"INFO allow entry {rel} has no hit any more: remove it")
    print(f"MEM0_HOST_LITERAL={'ok' if not fails else 'fail'} {fails} finding(s), {len(set(hits) & set(allow))} "
          f"allow-listed file(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
