"""Replay driver shared by the golden generator and the byte-identical test (t_1e4b9684)."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_worker_pool as kwp

FIXTURE = Path(__file__).parent / "kwlb_v01_replay.json"
GOLDEN = Path(__file__).parent / "kwlb_v01_replay.golden.json"


def _hosts():
    return [kwp.PoolHost(name=n, ssh_host=n, ssh_user=kwp.SSH_USER, slots=4, capacity_pct=0.8,
                         absence="required", profiles=("alpha",), state="active", enabled=True,
                         priority=i + 1)
            for i, n in enumerate(("ace-ai", "ace-media"))]


def replay(**plan_kw) -> str:
    ticks = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))["ticks"]
    out = []
    for n, t in enumerate(ticks):
        answers = t["probe"]

        def probe(h, _a=answers):
            v = _a.get(h.name)
            return None if v is None else (float(v[0]), int(v[1]))

        p = kwp.plan(_hosts(), t["running"], probe=probe, disabled=("ci-box",),
                     planned_at=1000.0 + n, **plan_kw)
        p.band = t["band"]
        takes = []
        for assignee, pin in t["takes"]:
            h = p.take(assignee, pin=pin)
            takes.append([h.name if h else None, p.refusal, p.budget])
        out.append({"snapshot": p.snapshot(), "summary": p.summary(), "takes": takes,
                    "slots": dict(p.slots), "band": p.band})
    return json.dumps(out, sort_keys=True, indent=1) + "\n"


if __name__ == "__main__":
    GOLDEN.write_text(replay(), encoding="utf-8")
