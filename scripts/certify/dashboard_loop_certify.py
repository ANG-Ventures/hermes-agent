#!/usr/bin/env python3
"""Dashboard loop-liveness certify harness (AC-4).

Drives concurrent REAL agent turns + concurrent session.list reads (the GIL-pressure regime that
wedges the dashboard) while probing the serving plane. Reports ws-list latency and REST
loop-liveness SEPARATELY. SAFE BY DESIGN: interrupts server-side turns on every exit path, sweeps
disposables by marker, and a CIRCUIT BREAKER aborts the harness's own load if the target starts
failing — so the harness can never wedge the system it measures.

See docs/desktop/2026-07-05-certify-harness-hardening-SPEC.md.

Usage:
  dashboard_loop_certify.py --sessions 3 --duration 300 [--reads 4] [--dry-run]
  dashboard_loop_certify.py --client-gone [--client-gone-timeout 30]
"""
import argparse, json, os, re, select, signal, subprocess, sys, time, threading, http.cookiejar, urllib.parse, urllib.request

BASE = os.environ.get("CERTIFY_BASE", "http://mac-studio-m3u:9119")
CERTIFY_HERMES_HOME = os.path.expanduser(os.environ.get("CERTIFY_HERMES_HOME", "~/.hermes"))
MARKER = "apollo-certify-DISPOSABLE"
_MAX_DRIVERS = 4  # INV-3: bounded concurrency, never the 6+ that wedged the box

def _creds():
    env = open(os.path.join(CERTIFY_HERMES_HOME, ".env"), encoding="utf-8").read()
    U = re.search(r"HERMES_DASHBOARD_BASIC_AUTH_USERNAME=(.*)", env).group(1).strip().strip('"\'')
    P = re.search(r"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=(.*)", env).group(1).strip().strip('"\'')
    return U, P

def _http_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    if os.environ.get("CERTIFY_NO_LOGIN") == "1":
        host = urllib.parse.urlsplit(BASE).hostname
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("CERTIFY_NO_LOGIN is allowed only for a loopback CERTIFY_BASE")
        token = os.environ.get("CERTIFY_SESSION_TOKEN", "")
        if not token:
            raise RuntimeError("CERTIFY_NO_LOGIN requires CERTIFY_SESSION_TOKEN")
        op.addheaders = [("X-Hermes-Session-Token", token)]
        return op, cj
    U, P = _creds()
    op.open(urllib.request.Request(BASE+"/auth/password-login",
        data=json.dumps({"provider":"basic","username":U,"password":P}).encode(),
        headers={"Content-Type":"application/json"}, method="POST"), timeout=60).read()
    return op, cj

def _ws(cj):
    import websocket
    if os.environ.get("CERTIFY_NO_LOGIN") == "1":
        host = urllib.parse.urlsplit(BASE).hostname
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("CERTIFY_NO_LOGIN is allowed only for a loopback CERTIFY_BASE")
        token = os.environ.get("CERTIFY_SESSION_TOKEN", "")
        if not token:
            raise RuntimeError("CERTIFY_NO_LOGIN requires CERTIFY_SESSION_TOKEN")
        ws_url = (
            BASE.replace("http", "ws", 1)
            + "/api/ws?token="
            + urllib.parse.quote(token, safe="")
        )
        ws = websocket.create_connection(ws_url, timeout=30)
        ws.settimeout(125)
        return ws
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    tk = json.loads(op.open(urllib.request.Request(BASE+"/api/auth/ws-ticket", data=b"", method="POST"), timeout=30).read())
    cookies = "; ".join(f"{c.name}={c.value}" for c in cj)
    ws = websocket.create_connection(BASE.replace("http","ws")+f"/api/ws?ticket={tk['ticket']}",
        header=[f"Cookie: {cookies}"], timeout=30)
    ws.settimeout(125)  # D-4: generous; real turns w/ 429 retries run long
    return ws

_rid = [0]; _rid_lock = threading.Lock()
def _next_rid():
    with _rid_lock:
        _rid[0]+=1; return _rid[0]

def _call(ws, method, params, timeout=120):
    rid = _next_rid()
    ws.send(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}))
    t0=time.time()
    while time.time()-t0 < timeout:
        try: m = json.loads(ws.recv())
        except Exception: return {"error":"recv"}
        if m.get("id")==rid: return m
    return {"error":"timeout"}

def _delete_all_disposables(op):
    """INV-2: sweep by marker — catches anything the per-id delete raced."""
    try:
        r = json.loads(op.open(urllib.request.Request(BASE+"/api/sessions?limit=100"), timeout=30).read())
        rows = r if isinstance(r, list) else (r.get("sessions") or [])
    except Exception:
        return -1
    left = [s for s in rows if MARKER in str(s.get("title",""))]
    for s in left:
        sid = s.get("session_id") or s.get("id")
        try: op.open(urllib.request.Request(BASE+f"/api/sessions/{sid}", method="DELETE"), timeout=15).read()
        except Exception: pass
    # reverify
    try:
        r = json.loads(op.open(urllib.request.Request(BASE+"/api/sessions?limit=100"), timeout=30).read())
        rows = r if isinstance(r, list) else (r.get("sessions") or [])
        return len([s for s in rows if MARKER in str(s.get("title",""))])
    except Exception:
        return -1

def _active_session(ctl, sid):
    r = _call(ctl, "session.active_list", {}, timeout=15)
    if "error" in r:
        raise RuntimeError(f"session.active_list failed: {r['error']!r}")
    result = r.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
        raise RuntimeError(f"session.active_list returned an invalid result: {result!r}")
    rows = result["sessions"]
    for row in rows:
        if isinstance(row, dict) and row.get("id") == sid:
            return row
    return None

def _client_gone_child(title):
    """Sacrificial AC-13 client. Parent intentionally SIGKILLs it mid-turn."""
    ws = None
    sid = None
    try:
        _op, cj = _http_session()
        ws = _ws(cj)
        created = _call(ws, "session.create", {"title":title,"source":"tui"}, timeout=30)
        create_result = created.get("result")
        sid = create_result.get("session_id") if isinstance(create_result, dict) else None
        if not sid:
            print(json.dumps({"ready":False,"stage":"create","response":created}), flush=True)
            return 2
        submitted = _call(ws, "prompt.submit", {
            "session_id": sid,
            "text": "Before answering, use the terminal tool to run `sleep 120` and wait for it to finish.",
        }, timeout=60)
        submit_result = submitted.get("result")
        ready = isinstance(submit_result, dict) and submit_result.get("status") == "streaming"
        print(json.dumps({"ready":ready,"stage":"submit","session_id":sid,"response":submitted}), flush=True)
        if not ready:
            return 2
        while True:
            time.sleep(3600)
    finally:
        # This runs for every ordinary child exit. SIGKILL is deliberately the
        # sole path that cannot run cleanup; the parent owns the backstop below.
        if ws is not None and sid is not None:
            try: _call(ws, "session.interrupt", {"session_id":sid}, timeout=10)
            except Exception: pass
            try: ws.close()
            except Exception: pass

def _certify_client_gone(op, ctl, args):
    """AC-13: hard-kill a real WS client and bound server-side turn cleanup."""
    class _Failed(Exception):
        pass

    sid = None
    child = None
    result = {"gate":"FAIL", "timeout_s":args.client_gone_timeout}
    try:
        title = f"{MARKER}-client-gone-{int(time.time())}"
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--_client-gone-child", title],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        ready_line = ""
        assert child.stdout is not None
        readable, _, _ = select.select([child.stdout], [], [], 75)
        if readable:
            ready_line = child.stdout.readline().strip()
        try:
            ready = json.loads(ready_line)
        except Exception:
            ready = {"ready":False,"output":ready_line}
        if not ready.get("ready"):
            result.update(stage="child_ready", child=ready, child_exit=child.poll())
            raise _Failed
        sid = ready.get("session_id")
        if not isinstance(sid, str) or not sid:
            result.update(stage="child_ready", child=ready, child_exit=child.poll())
            raise _Failed

        pre_kill_states = []
        pre_kill_deadline = time.monotonic() + 75
        live = None
        while time.monotonic() < pre_kill_deadline:
            live = _active_session(ctl, sid)
            state = live.get("status") if live else "missing"
            if not pre_kill_states or pre_kill_states[-1] != state:
                pre_kill_states.append(state)
            if state == "working":
                break
            if live is None:
                break
            time.sleep(0.25)
        if not live or live.get("status") != "working":
            result.update(stage="pre_kill", active_session=live, pre_kill_states=pre_kill_states)
            raise _Failed

        killed_at = time.monotonic()
        child.kill()
        child_exit = child.wait(timeout=10)
        states = []
        reaped_after_s = None
        deadline = killed_at + args.client_gone_timeout
        while time.monotonic() < deadline:
            live = _active_session(ctl, sid)
            if live is None:
                reaped_after_s = time.monotonic() - killed_at
                break
            state = live.get("status")
            if not states or states[-1] != state:
                states.append(state)
            time.sleep(0.25)

        kill_confirmed = child_exit == -signal.SIGKILL
        passed = kill_confirmed and reaped_after_s is not None
        result.update(
            gate="PASS" if passed else "FAIL",
            stage="settled" if passed else "timeout",
            session_id=sid,
            child_exit=child_exit,
            kill_confirmed=kill_confirmed,
            pre_kill_states=pre_kill_states,
            active_states=states,
            reaped_after_s=reaped_after_s,
        )
    except _Failed:
        pass
    except Exception as exc:
        result.update(stage="exception", error=repr(exc))
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            try: child.wait(timeout=10)
            except Exception: pass
        if sid:
            try: _call(ctl, "session.interrupt", {"session_id":sid}, timeout=10)
            except Exception: pass
            try: _call(ctl, "session.close", {"session_id":sid}, timeout=15)
            except Exception: pass
        left = _delete_all_disposables(op)
        result["disposables_left"] = left
        if left != 0:
            result["gate"] = "FAIL"
            result.setdefault("cleanup_error", "disposable session sweep did not reach zero")
        json.dump(result, open(args.json_out,"w",encoding="utf-8"), default=str)
        print(json.dumps(result, indent=2, default=str))
        print(f"\nAC-13 CLIENT-GONE GATE: {result['gate']}")
    return 0 if result["gate"] == "PASS" else 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--reads", type=int, default=4, help="concurrent session.list readers")
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--client-gone", action="store_true", help="run AC-13 kill-9 certification")
    ap.add_argument("--client-gone-timeout", type=float, default=30.0)
    ap.add_argument("--_client-gone-child", metavar="TITLE", help=argparse.SUPPRESS)
    ap.add_argument("--json-out", default="/tmp/dashboard-loop-certify.json")
    args = ap.parse_args()

    if args._client_gone_child:
        return _client_gone_child(args._client_gone_child)

    n = 1 if args.dry_run else min(args.sessions, _MAX_DRIVERS)
    dur = 20 if args.dry_run else args.duration
    ctx_turns = 2 if args.dry_run else 30

    op, cj = _http_session()
    print("[auth] ok")
    ctl = _ws(cj)
    if args.client_gone:
        try:
            return _certify_client_gone(op, ctl, args)
        finally:
            try: ctl.close()
            except Exception: pass

    seeds = []
    for i in range(n):
        msgs = []
        for j in range(ctx_turns):
            msgs.append({"role":"user","content":f"S{i}Q{j}: "+("lorem ipsum dolor sit amet "*60)})
            msgs.append({"role":"assistant","content":f"S{i}A{j}: "+("sed do eiusmod tempor "*60)})
        r = _call(ctl, "session.create", {"messages":msgs, "title":f"{MARKER}-{i}", "source":"tui"}, timeout=30)
        sid = r.get("result",{}).get("session_id")
        if sid: seeds.append(sid); print(f"[seed {i}] {sid}")
    if not seeds:
        print("SEED FAILED"); _delete_all_disposables(op); return 2

    stop = threading.Event()
    tripped = threading.Event()
    ws_list_lat, rest_lat = [], []

    def load_turns(sid):
        try:
            ws = _ws(cj)
        except Exception:
            return
        try:
            while not stop.is_set():
                rid = _next_rid()
                try:
                    ws.send(json.dumps({"jsonrpc":"2.0","id":rid,"method":"prompt.submit",
                                        "params":{"session_id":sid,"text":"Summarize in one sentence."}}))
                except Exception:
                    break
                t0=time.time()
                while time.time()-t0 < 120 and not stop.is_set():
                    try: m=json.loads(ws.recv())
                    except Exception: break
                    if m.get("id")==rid and ("result" in m or "error" in m): break
                if stop.is_set(): break
                time.sleep(1)
        finally:
            # INV-1: cancel the server-side turn on EVERY exit path
            try: _call(ws, "session.interrupt", {"session_id": sid}, timeout=10)
            except Exception: pass
            try: ws.close()
            except Exception: pass

    def probe_ws_list():
        try: ws = _ws(cj)
        except Exception: return
        try:
            while not stop.is_set():
                t0=time.time()
                r=_call(ws, "session.list", {"limit":20}, timeout=15)
                ws_list_lat.append((time.time()-t0, "error" in r))
                time.sleep(0.5)
        finally:
            try: ws.close()
            except Exception: pass

    def probe_rest_breaker():
        """REST loop-liveness probe AND the circuit breaker (D-3/INV-3)."""
        consec_fail = 0
        while not stop.is_set():
            t0=time.time()
            try:
                op.open(urllib.request.Request(BASE+"/api/auth/providers"), timeout=8).read()
                dt=time.time()-t0; rest_lat.append((dt, False))
                consec_fail = 0 if dt < 3.0 else consec_fail  # slow-but-alive doesn't trip
            except Exception:
                rest_lat.append((8.0, True)); consec_fail += 1
            if consec_fail >= 5:
                tripped.set(); stop.set()  # ABORT our own load — never wedge the target
                print("\n[CIRCUIT BREAKER] 5 consecutive probe failures — aborting load to protect target")
                break
            time.sleep(0.5)

    threads = [threading.Thread(target=load_turns, args=(s,), daemon=True) for s in seeds]
    threads += [threading.Thread(target=probe_ws_list, daemon=True) for _ in range(max(1,args.reads))]
    threads += [threading.Thread(target=probe_rest_breaker, daemon=True)]
    for t in threads: t.start()
    print(f"[running] {len(seeds)} turn drivers + {args.reads} list readers, {dur}s ...")
    t_end = time.time()+dur
    try:
        while time.time()<t_end and not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[interrupted] stopping + cleaning up")
    stop.set()
    for t in threads: t.join(timeout=15)

    # INV-1 backstop: interrupt every seeded sid from the control conn too
    for s in seeds:
        try: _call(ctl, "session.interrupt", {"session_id": s}, timeout=10)
        except Exception: pass
    # INV-2: delete disposables + marker sweep
    left = _delete_all_disposables(op)
    try: ctl.close()
    except Exception: pass

    def summ(lat):
        oks=[x for x,e in lat if not e]; drops=sum(1 for _,e in lat if e)
        if not oks: return dict(n=len(lat),p50=None,p99=None,mx=None,drops=drops)
        oks.sort()
        return dict(n=len(lat),p50=oks[len(oks)//2],p99=oks[min(len(oks)-1,int(len(oks)*0.99))],mx=max(oks),drops=drops)
    ws_s, rest_s = summ(ws_list_lat), summ(rest_lat)
    def ms(v): return f"{v*1000:.0f}ms" if v is not None else "n/a"
    print(f"\n== certify results ({'DRY' if args.dry_run else f'{n} drivers/{dur}s'}) ==")
    print(f"REST loop-liveness : p50={ms(rest_s['p50'])} p99={ms(rest_s['p99'])} max={ms(rest_s['mx'])} drops={rest_s['drops']} n={rest_s['n']}")
    print(f"ws session.list    : p50={ms(ws_s['p50'])} p99={ms(ws_s['p99'])} max={ms(ws_s['mx'])} drops={ws_s['drops']} n={ws_s['n']}")
    print(f"disposables left   : {left}")
    # INV-4: gate keys on LOOP LIVENESS (REST), not list latency
    gate = (not args.dry_run and not tripped.is_set()
            and rest_s['p99'] is not None and rest_s['p99']<1.0
            and rest_s['drops']==0 and left==0)
    json.dump({"dry_run":args.dry_run,"tripped":tripped.is_set(),"rest":rest_s,"ws_list":ws_s,"disposables_left":left}, open(args.json_out,"w",encoding="utf-8"), default=str)
    if args.dry_run:
        print("\nDRY-RUN: plumbing + safety wiring OK")
    elif tripped.is_set():
        print("\nAC-4 GATE: ABORTED by circuit breaker — target was failing; harness protected it (not a clean measurement)")
    else:
        print(f"\nAC-4 GATE (loop liveness): {'PASS' if gate else 'FAIL'}  (REST p99<1s, 0 drops, 0 disposables left)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
