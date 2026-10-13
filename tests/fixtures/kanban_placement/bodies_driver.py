"""Route table of the 50-card body fixture (t_8c474c2e, spec s7 Phase 1 test 6).

Shared by the golden generator (run against the BASE commit, before the
capability layer existed) and ``test_absent_map_is_byte_identical``.
``routes(conn)`` needs a kanban home with the fixture cards already created.
"""

from __future__ import annotations

import json
from pathlib import Path

BODIES = Path(__file__).parent / "placement_bodies_50.json"
GOLDEN = Path(__file__).parent / "placement_bodies_50.routes.golden.json"


def cards():
    return json.loads(BODIES.read_text(encoding="utf-8"))["cards"]


def create_cards(kb, conn):
    return [kb.create_task(conn, title=f"fixture-{i}", assignee="alpha", body=c["body"],
                           skills=c["skills"] or None)
            for i, c in enumerate(cards())]


def plan(kwp):
    hosts = [kwp.PoolHost(name=n, ssh_host=n, ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                          absence="required", profiles=("alpha",), state="active", enabled=True,
                          priority=i)
             for i, n in enumerate(("ci-box", "ace-ai", "ace-media"))]
    cfg = kwp.PoolConfig(hosts=("ci-box", "ace-ai", "ace-media"), profiles=("alpha",),
                         studio_bound_skills=("hermes-config-editing", "safe-gateway-restart"),
                         pool_hosts=tuple(hosts))
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "spilling", "load"
    return p


def routes(kb, kbd, kwp, conn, ids) -> str:
    """One line per card, in fixture order: ``index route arg``."""
    p = plan(kwp)
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker FROM tasks")}
    out = []
    for i, tid in enumerate(ids):
        route, arg = kbd._route_row(conn, rows[tid], p, None, {}, {})
        out.append([i, route, arg])
    return json.dumps(out, indent=0) + "\n"
