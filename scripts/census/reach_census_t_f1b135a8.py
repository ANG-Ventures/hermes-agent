#!/usr/bin/env python3
"""Reach census for t_f1b135a8: which open cards does host_reach bind on BASE
(any token after an ssh/rsync verb, any <token>:) vs on the PR (target position
only, word aliases need a ~/.ssh/config Host)?

Run on the dispatcher host (Studio), from a checkout of the PR branch:
    python3 reach_census_t_f1b135a8.py --repo <hermes-agent checkout> \
        --db ~/.hermes/kanban.db [--db <other board db> ...] \
        [--matrix ~/.hermes/fleet/kanban-reach-matrix.json] [--ssh-config ~/.ssh/config] \
        [--extra-alias ci --extra-alias ai --extra-alias media]

Read-only: opens each DB with mode=ro. --extra-alias simulates the short
aliases the spec's R6 two-door fold adds to the vocabulary.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# --- BASE logic, verbatim from origin/main 0b88b7db26 kanban_worker_pool.py ---
_HOST_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_B_SSH_VERB_RE = re.compile(r"(?<![\w-])(?:ssh|scp|sftp|rsync|mosh)(?![\w-])(.*)", re.IGNORECASE)
_B_AFTER_VERB_RE = re.compile(rf"(?:^|[\s@'\"=/])({_HOST_TOKEN})")
_B_SCP_RE = re.compile(rf"(?:^|[\s@'\"])({_HOST_TOKEN}):(?=[~/.$A-Za-z_])")
_B_DASH_H_RE = re.compile(rf"(?:^|\s)-H[ \t]*[\"']?(?:[\w.-]+@)?({_HOST_TOKEN})")


def base_targets(body, aliases, pins, pin_re):
    found = []
    for line in (body or "").splitlines():
        m = pin_re.match(line)
        if m and m.group(1).lower() in pins:
            continue
        out = []
        m = _B_SSH_VERB_RE.search(line)
        if m:
            out += _B_AFTER_VERB_RE.findall(m.group(1))
        out += _B_SCP_RE.findall(line)
        out += _B_DASH_H_RE.findall(line)
        for word in out:
            w = word.lower().rstrip("._-")
            if w in aliases and w not in found:
                found.append(w)
    return tuple(found)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--db", action="append", required=True)
    ap.add_argument("--matrix", default=str(Path.home() / ".hermes/fleet/kanban-reach-matrix.json"))
    ap.add_argument("--ssh-config", default=str(Path.home() / ".ssh/config"))
    ap.add_argument("--extra-alias", action="append", default=[])
    ap.add_argument("--statuses", default="todo,ready,running,blocked,triage,review")
    a = ap.parse_args()
    sys.path.insert(0, a.repo)
    from hermes_cli import kanban_worker_pool as kwp

    fleet = Path(a.matrix).parent
    _reach, vocab, status = kwp.read_reach(fleet)
    pool_hosts = kwp.known_host_ids(fleet)
    aliases = tuple(dict.fromkeys([*vocab, *pool_hosts, *(x.lower() for x in a.extra_alias)]))
    ssh_hosts = kwp.known_ssh_hosts(fleet, ssh_config=Path(a.ssh_config))
    pins = {kwp.PIN_ANY, kwp.PIN_STUDIO, *pool_hosts}
    words = [x for x in aliases if kwp.word_shaped_alias(x)]
    print(f"matrix={a.matrix} status={status} aliases={len(aliases)}: {', '.join(aliases)}")
    print(f"ssh_config={a.ssh_config} hosts={'UNREADABLE (fail closed)' if ssh_hosts is None else len(ssh_hosts)}")
    if ssh_hosts is not None:
        missing = [x for x in words if x not in ssh_hosts]
        print(f"word-shaped aliases: {words}; neither a Host in ssh config nor reached by any matrix row (cannot bind): {missing}")
    sts = tuple(s.strip() for s in a.statuses.split(",") if s.strip())
    tot = {"open": 0, "base_bound": 0, "new_bound": 0, "unbind": 0, "newly_bound": 0, "changed": 0}
    for db in a.db:
        conn = sqlite3.connect(f"file:{Path(db).expanduser()}?mode=ro", uri=True)
        q = f"select id, title, body from tasks where status in ({','.join('?' * len(sts))})"
        for tid, title, body in conn.execute(q, sts):
            tot["open"] += 1
            b = base_targets(body, set(aliases), pins, kwp.HOST_PIN_RE)
            _nb, n = kwp.host_bound_targets(body, (), aliases, pin_hosts=pool_hosts, ssh_hosts=ssh_hosts)
            tot["base_bound"] += bool(b)
            tot["new_bound"] += bool(n)
            if b and not n:
                tot["unbind"] += 1
                print(f"UNBIND  {db}:{tid} base={list(b)} :: {(title or '')[:90]}")
            elif n and not b:
                tot["newly_bound"] += 1
                print(f"NEWBIND {db}:{tid} new={list(n)} :: {(title or '')[:90]}")
            elif b != n:
                tot["changed"] += 1
                print(f"CHANGED {db}:{tid} base={list(b)} new={list(n)} :: {(title or '')[:90]}")
        conn.close()
    print("TOTAL " + " ".join(f"{k}={v}" for k, v in tot.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
