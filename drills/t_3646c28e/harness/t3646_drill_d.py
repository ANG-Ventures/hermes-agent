"""Drill d: credential handling on the kanban unit path.
  userinfo: parent's anonymous custom endpoint URL carries userinfo -> persist must refuse (c631db36b404).
  ownkey:   delegation.base_url set, no delegation.api_key, parent on an anonymous custom endpoint with its own key
            -> the unit's child authenticates with the PARENT's key, never the default/env key (467b28edd66c).
usage: t3646_drill_d.py <in_process|kanban> <userinfo|ownkey> <outdir>"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import t3646_rig  # noqa: E402
from t3646_rig import Director, Rig, find_procs  # noqa: E402
from tests.e2e.core.chaos import _helpers as ch  # noqa: E402
from tests.fakes.fake_llm_provider import FakeLLMServer  # noqa: E402

runtime, mode, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
out.mkdir(parents=True, exist_ok=True)
X = f"d{mode[:2]}{runtime[:2]}{int(time.time()) % 100000}"
SECRET = f"pw{X}zz"
PARENT_KEY = f"sk-parent-{X}"
d = Director()
d.sleep[X] = 5
ev = {"drill": "d-" + mode, "runtime": runtime, "token": X, "steps": []}


def step(name, **kw):
    kw["name"], kw["t"] = name, time.time()
    ev["steps"].append(kw)
    print(json.dumps(kw, default=str)[:900], flush=True)


with FakeLLMServer(d) as srv, FakeLLMServer(d) as dsrv:
    orig_cfg = ch.chaos_config

    def cfg(base_url, **kw):
        text = orig_cfg(base_url, **kw)
        if mode == "userinfo":
            text = text.replace(f"base_url: {base_url}", f"base_url: {base_url.replace('http://', f'http://u:{SECRET}@')}")
        else:
            text = text.replace(f"base_url: {base_url}\n", f"base_url: {base_url}\n  api_key: {PARENT_KEY}\n")
        return text

    ch.chaos_config = cfg
    import tests.e2e.core.chaos._gateway_harness as gh
    gh.write_chaos_home.__globals__["chaos_config"] = cfg
    extra = "" if mode == "userinfo" else f"delegation_extra_placeholder\n"
    rig = Rig(out / "rig", srv, runtime, tag=X)
    if mode == "ownkey":
        orig_write = t3646_rig.write_extra
        t3646_rig.write_extra = lambda rt, cwd, ex="": orig_write(rt, cwd, ex).replace(
            "  max_concurrent_children: 3\n", f"  max_concurrent_children: 3\n  base_url: {dsrv.base_url}\n")
    try:
        gw = rig.start()
        step("config", text=(rig.vhome / "config.yaml").read_text().replace(SECRET, "<SECRET>").replace(PARENT_KEY, "<PARENT_KEY>"))
        gw.send_user(f"[drill:{X}] delegate the sleep")
        try:
            rig.wait(lambda: d.wakes.get(X) or [x for x in d.log if x.get("who") == "other" and X in str(x.get("last"))], 240,
                     "result", step=2)
        except AssertionError as e:
            step("no_result", err=str(e))
        time.sleep(8)
        reg_text = (rig.vhome / "state" / "async-delegations.json").read_text() \
            if (rig.vhome / "state" / "async-delegations.json").exists() else ""
        card_text = json.dumps(rig.cards())
        needle = SECRET if mode == "userinfo" else PARENT_KEY
        step("final", cards=[(c["id"], c["status"], c["events"]) for c in rig.cards()],
             registry={k: {"state": v.get("state"), "runtime": (v.get("execution") or {}).get("runtime"),
                           "route": (v.get("execution") or {}).get("route")} for k, v in rig.registry().items()},
             secret_in_registry=needle in reg_text, secret_in_cards=needle in card_text,
             child_auth_on_parent_srv=sorted({r["auth"].replace(PARENT_KEY, "<PARENT_KEY>")[:40] for r in srv.requests
                                              if "CHILDGOAL" in json.dumps(r["body"].get("messages", [])[:3])}),
             child_auth_on_deleg_srv=sorted({r["auth"].replace(PARENT_KEY, "<PARENT_KEY>")[:40] for r in dsrv.requests}),
             deleg_srv_requests=len(dsrv.requests),
             sends=[s.get("text", "")[:400].replace(SECRET, "<SECRET>") for s in rig.sends(gw)],
             wakes=d.wakes.get(X),
             all_auth_by_server={"parent_srv": sorted({r["auth"].replace(PARENT_KEY, "<PARENT_KEY>")[:40] for r in srv.requests}),
                                 "deleg_srv": sorted({r["auth"].replace(PARENT_KEY, "<PARENT_KEY>")[:40] for r in dsrv.requests})},
             delivered=[str(x.get("last"))[:900] for x in d.log if x.get("who") == "other"])
        gw.stop()
    finally:
        if rig.gw and rig.gw.exit_code is None:
            rig.gw.stop()
        ev["director_log"] = json.loads(json.dumps(d.log).replace(SECRET, "<SECRET>").replace(PARENT_KEY, "<PARENT_KEY>"))
        ev["log_grep"] = [l.replace(SECRET, "<SECRET>").replace(PARENT_KEY, "<PARENT_KEY>")
                          for l in rig.log_lines("async_delegation") + rig.log_lines("delegation_unit")
                          + rig.log_lines("persist") + rig.log_lines("SYNCHRON")]
        hits = []
        for f in rig.root.rglob("*"):
            try:
                if f.is_file() and f.stat().st_size < 50_000_000 and (SECRET if mode == "userinfo" else PARENT_KEY).encode() in f.read_bytes():
                    hits.append(str(f.relative_to(rig.root)))
            except OSError:
                pass
        ev["files_containing_secret"] = hits
        (out / "evidence.json").write_text(json.dumps(ev, indent=1, default=str))
        from tests.e2e.core.chaos._helpers import kill_tagged
        kill_tagged(f"t3646{X}")
