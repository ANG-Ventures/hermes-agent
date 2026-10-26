"""Drill a (graceful restart mid-unit) and b (SIGKILL mid-unit), parametrised by knob.
usage: t3646_drill_a.py <in_process|kanban> <restart|sigkill> <outdir>"""
import json
import os
import psutil
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from t3646_rig import Director, Rig, find_procs, proc_info  # noqa: E402
from tests.fakes.fake_llm_provider import FakeLLMServer  # noqa: E402

runtime, mode, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
out.mkdir(parents=True, exist_ok=True)
X = f"{mode[:2]}{runtime[:2]}{int(time.time()) % 100000}"
d = Director()
d.sleep[X] = 600 if mode in ("crash", "lostcard") else 75
ev = {"drill": mode, "runtime": runtime, "token": X, "steps": []}


def step(name, **kw):
    kw["name"], kw["t"] = name, time.time()
    ev["steps"].append(kw)
    print(json.dumps(kw, default=str)[:600], flush=True)


with FakeLLMServer(d) as srv:
    rig = Rig(out / "rig", srv, runtime, tag=X)
    try:
        gw = rig.start()
        step("boot1", gw=rig.boots[-1])
        gw.send_user(f"[drill:{X}] delegate the sleep")
        rig.wait(lambda: d.child_first.get(X), 90, "child first request")
        step("child_started", child_first=d.child_first.get(X), cards=rig.cards(),
             runners=find_procs("delegation-unit", X), sleeps=find_procs(f"SLEPT-{X}", X))
        time.sleep(3)
        if mode == "restart":
            gw.stop()
            step("gw1_stopped", exit_code=gw.exit_code, shutdown_s=gw.shutdown_s)
        else:
            os.kill(gw.proc.pid, signal.SIGKILL)
            gw.proc.wait(30)
            gw.exit_code = -9
            step("gw1_sigkilled")
            if mode in ("crash", "lostcard"):
                # whole unit dies too: runner SIGKILL (create-time checked) -> crash sweep / lost-card reaper
                for r in find_procs("delegation-unit", X):
                    p = psutil.Process(r["pid"])
                    if p.create_time() == r["create_time"]:
                        p.kill()
                        step("runner_sigkilled", pid=r["pid"], create_time=r["create_time"])
                time.sleep(2)
                step("sleep_procs_after_runner_kill", sleeps=find_procs(f"SLEPT-{X}", X))
            if mode == "lostcard":
                import sqlite3
                db = rig.deleg_db()
                con = sqlite3.connect(str(db))
                ids = [r[0] for r in con.execute("select id from tasks")]
                for tbl in ("task_events", "task_runs", "task_comments", "task_links"):
                    try:
                        con.execute(f"delete from {tbl} where task_id in ({','.join('?'*len(ids))})", ids)
                    except sqlite3.Error:
                        pass
                con.execute(f"delete from tasks where id in ({','.join('?'*len(ids))})", ids)
                con.commit()
                con.close()
                step("card_rows_deleted", ids=ids)
        step("after_stop", runners=find_procs("delegation-unit", X), sleeps=find_procs(f"SLEPT-{X}", X),
             cards=rig.cards(), registry={k: {"state": v.get("state"), "gen": (v.get("attempt") or {}).get("generation"),
                                              "runtime": (v.get("execution") or {}).get("runtime")}
                                          for k, v in rig.registry().items()})
        time.sleep(2)
        gw2 = rig.start()
        step("boot2", gw=rig.boots[-1])

        def delivered():
            return [s for s in rig.sends(gw2) if f"PARENT-ACK" in s.get("text", "") and X in s.get("text", "")] \
                or d.wakes.get(X) or [s for s in rig.sends(gw2) if "deleg_" in s.get("text", "")
                                      or "elegation" in s.get("text", "")]
        try:
            rig.wait(delivered, 240, "delivery", step=2)
        except AssertionError as e:
            step("no_delivery", err=str(e))
        time.sleep(15)
        step("final", child_first=d.child_first.get(X), child_any=len(d.child_any.get(X, [])),
             wakes=d.wakes.get(X), sends2=[s.get("text", "")[:300] for s in rig.sends(gw2)],
             cards=rig.cards(), registry={k: {"state": v.get("state"), "attempt": v.get("attempt"),
                                              "runtime": (v.get("execution") or {}).get("runtime"),
                                              "redispatch_count": v.get("redispatch_count")}
                                          for k, v in rig.registry().items()},
             runners=find_procs("delegation-unit", X), sleeps=find_procs(f"SLEPT-{X}", X),
             last_user_texts=[x.get("last") for x in d.log if x.get("who") == "other"])
        # third restart: no new delivery
        n_w = len(d.wakes.get(X, []))
        gw2.stop()
        gw3 = rig.start()
        time.sleep(25)
        step("third_boot", wakes_before=n_w, wakes_after=len(d.wakes.get(X, [])),
             sends3=[s.get("text", "")[:200] for s in rig.sends(gw3)])
        gw3.stop()
    finally:
        if rig.gw and rig.gw.exit_code is None:
            rig.gw.stop()
        ev["director_log"] = d.log
        ev["model_requests"] = [{"t": r["t"], "kind": r["kind"], "auth": r.get("auth", "")[:14]} for r in srv.requests]
        ev["log_grep"] = rig.log_lines("async_delegation") + rig.log_lines("delegation_unit") + rig.log_lines("kanban_spawn")
        (out / "evidence.json").write_text(json.dumps(ev, indent=1, default=str))
        from tests.e2e.core.chaos._helpers import kill_tagged
        kill_tagged(f"t3646{X}")
