"""Drill c (/stop mid-unit; variant 'exit' = gateway SIGKILLed right after /stop, before the archive lands)
usage: t3646_drill_c.py <in_process|kanban> <stop|exit> <outdir>"""
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from t3646_rig import Director, Rig, find_procs  # noqa: E402
from tests.fakes.fake_llm_provider import FakeLLMServer  # noqa: E402

runtime, mode, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
out.mkdir(parents=True, exist_ok=True)
X = f"c{mode[:2]}{runtime[:2]}{int(time.time()) % 100000}"
d = Director()
d.sleep[X] = 90
ev = {"drill": "c-" + mode, "runtime": runtime, "token": X, "steps": []}


def step(name, **kw):
    kw["name"], kw["t"] = name, time.time()
    ev["steps"].append(kw)
    print(json.dumps(kw, default=str)[:700], flush=True)


def reg(rig):
    return {k: {"state": v.get("state"), "runtime": (v.get("execution") or {}).get("runtime"),
                "cancel_attribution": v.get("cancel_attribution"), "kanban": v.get("kanban")}
            for k, v in rig.registry().items()}


with FakeLLMServer(d) as srv:
    rig = Rig(out / "rig", srv, runtime, tag=X)
    import t3646_rig
    if mode == "exit":
        t3646_rig.EXTRA_ENV["T3646_BLOCK_CANCEL_ARCHIVE"] = "1"
    try:
        gw = rig.start()
        gw.send_user(f"[drill:{X}] delegate the sleep")
        rig.wait(lambda: d.child_first.get(X), 90, "child first request")
        time.sleep(3)
        step("child_running", cards=rig.cards(), runners=find_procs("delegation-unit", X),
             sleeps=find_procs(f"SLEPT-{X}", X), registry=reg(rig))
        t_stop = time.time()
        gw.send_user("/stop")
        if mode == "exit":
            rig.wait(lambda: any(v.get("state") == "cancelled" for v in rig.registry().values()), 30,
                     "registry cancelled", step=0.1)
            os.kill(gw.proc.pid, signal.SIGKILL)
            gw.proc.wait(30)
            gw.exit_code = -9
            t3646_rig.EXTRA_ENV.pop("T3646_BLOCK_CANCEL_ARCHIVE", None)
            inj = [l for l in (rig.root / f"gateway-{len(rig.boots)-1}.log").read_text(errors="replace").splitlines()
                   if "T3646_INJECT" in l]
            step("gw_sigkilled_after_stop", dt=time.time() - t_stop, inject_lines=inj, cards=rig.cards(),
                 registry=reg(rig), runners=find_procs("delegation-unit", X))
            time.sleep(3)
            gw = rig.start()
            step("reboot", gw=rig.boots[-1])
        try:
            rig.wait(lambda: not find_procs("delegation-unit", X) and not find_procs(f"SLEPT-{X}", X), 150,
                     "runner+tool gone", step=2)
            gone = time.time()
        except AssertionError:
            gone = None
        time.sleep(10)
        late_child = [t for t in d.child_any.get(X, []) if t > t_stop + 6]
        step("final", stop_to_gone_s=(gone - t_stop) if gone else None, cards=rig.cards(), registry=reg(rig),
             runners=find_procs("delegation-unit", X), sleeps=find_procs(f"SLEPT-{X}", X),
             child_requests=d.child_any.get(X), child_requests_after_stop_plus_6s=late_child,
             wakes=d.wakes.get(X), sends=[s.get("text", "")[:200] for s in rig.sends(gw)])
        gw.stop()
    finally:
        if rig.gw and rig.gw.exit_code is None:
            rig.gw.stop()
        ev["director_log"] = d.log
        ev["log_grep"] = rig.log_lines("async_delegation") + rig.log_lines("cancel") + rig.log_lines("stop_command")
        (out / "evidence.json").write_text(json.dumps(ev, indent=1, default=str))
        from tests.e2e.core.chaos._helpers import kill_tagged
        kill_tagged(f"t3646{X}")
