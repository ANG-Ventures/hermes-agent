"""Drill e: kanban load gate + card budget force-closed; 4 concurrent background units with
max_concurrent_children=3. usage: t3646_drill_e.py <in_process|kanban> <outdir>"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from t3646_rig import Director, Rig, find_procs  # noqa: E402
from tests.fakes.fake_llm_provider import FakeLLMServer  # noqa: E402

runtime, out = sys.argv[1], Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
base = f"e{runtime[:2]}{int(time.time()) % 100000}"
XS = [f"{base}n{i}" for i in range(4)]
d = Director()
for x in XS:
    d.sleep[x] = 60
ev = {"drill": "e", "runtime": runtime, "tokens": XS, "steps": []}
GATE = (
    "  max_in_progress: 1\n"
    "  max_spawn: 0\n"
    "  dispatch_load_gate:\n"
    "    enabled: true\n"
    "    pause_above: 0.01\n"
    "    resume_below: 0.005\n"
)


def step(name, **kw):
    kw["name"], kw["t"] = name, time.time()
    ev["steps"].append(kw)
    print(json.dumps(kw, default=str)[:900], flush=True)


with FakeLLMServer(d) as srv:
    rig = Rig(out / "rig", srv, runtime, extra=GATE, tag=base)
    try:
        gw = rig.start()
        # Negative control: an ordinary default-board card must NOT spawn while the gate is closed.
        env = dict(os.environ, HERMES_HOME=str(rig.vhome), HOME=str(rig.vhome.parent),
                   HERMES_DISABLE_LAZY_INSTALLS="1")
        import subprocess
        r = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "create", "e-control card",
                            "--assignee", "default", "--unhomed"], env=env, capture_output=True, text=True, timeout=120)
        step("control_card_created", rc=r.returncode, out=r.stdout[-300:], err=r.stderr[-300:])
        for i, x in enumerate(XS[:3]):
            gw.send_user(f"[drill:{x}] delegate the sleep", chat=f"chat-{i}")
            time.sleep(1.5)
        rig.wait(lambda: all(d.child_first.get(x) for x in XS[:3]), 120, "3 children started")
        time.sleep(3)
        step("three_running", cards=[(c["id"], c["status"], c["title"][:40]) for c in rig.cards()],
             runners=find_procs("delegation-unit", base))
        gw.send_user(f"[drill:{XS[3]}] delegate the sleep", chat="chat-3")
        rig.wait(lambda: d.child_first.get(XS[3]), 120, "4th child started")
        time.sleep(3)
        fourth_sleeps = find_procs(f"SLEPT-{XS[3]}", base)
        step("fourth_started", cards=[(c["id"], c["status"], c["title"][:40]) for c in rig.cards()],
             fourth_sleep_procs=fourth_sleeps, gateway_pid=gw.proc.pid)
        rig.wait(lambda: all(d.wakes.get(x) for x in XS[:3]) or None, 300, "3 deliveries", step=3)
        time.sleep(10)
        con = sqlite3.connect(f"file:{rig.default_db()}?mode=ro", uri=True)
        ctrl = [tuple(r) for r in con.execute("select id,title,status,current_run_id from tasks")]
        ctrl_ev = [tuple(r) for r in con.execute(
            "select task_id,kind,created_at from task_events order by id")]
        con.close()
        step("final", wakes={x: d.wakes.get(x) for x in XS}, child_first={x: d.child_first.get(x) for x in XS},
             cards=[(c["id"], c["status"], c["title"][:40], c["events"]) for c in rig.cards()],
             control_board=ctrl, control_events=ctrl_ev,
             sends=[(s.get("chat"), s.get("text", "")[:300]) for s in rig.sends(gw)],
             registry={k: {"state": v.get("state"), "runtime": (v.get("execution") or {}).get("runtime")}
                       for k, v in rig.registry().items()})
        gw.stop()
    finally:
        if rig.gw and rig.gw.exit_code is None:
            rig.gw.stop()
        ev["director_log"] = d.log
        ev["log_grep"] = (rig.log_lines("async_delegation") + rig.log_lines("load gate")
                          + rig.log_lines("paus") + rig.log_lines("capacity") + rig.log_lines("synchronous"))
        (out / "evidence.json").write_text(json.dumps(ev, indent=1, default=str))
        from tests.e2e.core.chaos._helpers import kill_tagged
        kill_tagged(f"t3646{base}")
