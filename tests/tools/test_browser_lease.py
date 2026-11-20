"""A kanban worker's browser_* tools on a Studio identity lease the tool starts itself (t_ce9bd5a3).

The port a placed worker's browser tools may use comes only from the stdout of the lease client the
tool spawned, and only while that child lives. Hermetic: fake lease clients, no real browserd.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from tools import browser_lease as bl
from tools import rail_preflight as rp
from hermes_cli import kanban_worker_hosts as kwh

EXPECTED = (
    "BROWSER_RAIL_ABSENT: no browser session on {host}. This card needs Ace's logged-in browser, "
    "which lives only on the Mac Studio. Add the line `host: studio` to the card body (or "
    "`requires: browser-session` once the affinity spec lands) and re-run; do not retry here."
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bl.clear_lease()
    rp._emitted.clear()
    rp._probe_ok.clear()
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lease001")
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "ace-ai", "env": {"TERMINAL_ENV": "ssh"}}))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("KANBAN_PLACEMENT_CAPS", raising=False)
    yield
    bl.clear_lease()
    rp._emitted.clear()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# browserctl's stderr grant line (merged into the watcher's stdout), with the secret-bearing values it really carries
GRANT = {"lease_id": 12473, "identity": "id-github-kyzcreig", "class": "read", "expires_at": 1795213000.5,
         "cdp_url": "wss://cdp.browsers.ace/SECRETCDP", "viewer_url": "https://studio/view?ticket=SECRETVIEW",
         "heartbeat_every_s": 60}


def _fake_client(monkeypatch, port: int, *, then_exit: bool = False, refuse: str = ""):
    """``_spawn`` replaced by a child that behaves like the watcher: prints browserctl's grant line and ``port=N``
    (or a refusal); on stdin EOF prints browserctl's release line, as `with` does when the watcher ends it."""
    spawned = []
    body = f"print({refuse!r}, flush=True); raise SystemExit(3)" if refuse else textwrap.dedent(f"""
        import sys, time
        print({json.dumps(GRANT, sort_keys=True)!r}, flush=True)
        print("lease-cdp-forward: gateway ok", flush=True)
        print("port={port}", flush=True)
        if {then_exit!r}:
            raise SystemExit(0)
        sys.stdin.read()
        print("browserctl with: released=12473 heartbeats ok=0 fail=0", flush=True)
    """)

    def spawn(identity, klass, card):
        spawned.append((identity, klass, card))
        return subprocess.Popen([sys.executable, "-c", body], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    monkeypatch.setattr(bl, "_spawn", spawn)
    return spawned


def test_the_lease_port_is_admitted_only_while_the_tools_own_child_lives(monkeypatch):
    port = _free_port()
    spawned = _fake_client(monkeypatch, port)
    lease, err = bl.open_lease("id-github-kyzcreig", "read", card="t_lease001", timeout=10)
    assert err is None and lease.port == port
    assert spawned == [("id-github-kyzcreig", "read", "t_lease001")]
    url = f"http://127.0.0.1:{port}"
    assert rp.browser_rail_error(url) is None
    from tools import browser_tool_cdp as cdp
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert cdp._get_cdp_override_raw() == url, "the lease the tool started wins over any env value"
    # (1) any other loopback port is refused, even while the lease lives
    assert rp.browser_rail_error("http://127.0.0.1:9222") == EXPECTED.format(host="ace-ai")
    assert rp.browser_rail_error(f"http://127.0.0.1:{port + 1}") == EXPECTED.format(host="ace-ai")
    # browser_cdp connects from this process: never to the pool host's lease port
    assert rp.browser_rail_error(url, host_side=True) == rp.LEASE_HOST_SIDE
    # (2) the child ends (lease released or expired: browserctl exits) -> the same port is refused,
    # and the selection holds: no fallback to the configured CDP (Prism 52ec81c4713e)
    lease.proc.stdin.close()
    lease.proc.wait(10)
    assert bl.active_url() == ""
    assert rp.browser_rail_error(url).startswith("BROWSER_LEASE_UNAVAILABLE identity id-github-kyzcreig")
    assert cdp._get_cdp_override_raw() == bl.DEAD_URL
    # only an explicit clear gives the configured override back
    bl.clear_lease()
    assert cdp._get_cdp_override_raw() == "http://127.0.0.1:9222"
    assert rp.browser_rail_error(url) == EXPECTED.format(host="ace-ai")


def test_an_expired_lease_is_refused_on_the_next_call(monkeypatch):
    port = _free_port()
    _fake_client(monkeypatch, port, then_exit=True)   # browserctl rc 75: the lease ended under `with`
    lease, err = bl.open_lease("id-github-kyzcreig", timeout=10)
    if lease is not None:
        lease.proc.wait(10)
    assert bl.active_url() == ""
    assert rp.browser_rail_error(f"http://127.0.0.1:{port}").startswith("BROWSER_LEASE_UNAVAILABLE")


def test_a_refused_acquire_gives_no_port(monkeypatch):
    # (4) buy/human are refused by the Studio gate; the refusal line comes back, no port is admitted.
    _fake_client(monkeypatch, 0, refuse="BROWSER_REMOTE_CLASS_REFUSED class buy is not leasable by a remote worker")
    lease, err = bl.open_lease("id-github-kyzcreig", "read", timeout=10)
    assert lease is None and "BROWSER_REMOTE_CLASS_REFUSED" in err
    assert bl.active_url() == ""
    assert bl.unavailable_error().startswith("BROWSER_LEASE_UNAVAILABLE"), "a failed lease fails closed too"


@pytest.mark.parametrize("klass", ["buy", "human", "identity", "--class=buy"])
def test_buy_and_human_classes_never_reach_the_client(monkeypatch, klass):
    spawned = _fake_client(monkeypatch, _free_port())
    lease, err = bl.open_lease("id-github-kyzcreig", klass, timeout=5)
    assert lease is None and "lease_class" in err and spawned == []


@pytest.mark.parametrize("ident", ["", "-x", "--class=buy", "a b", "id;rm"])
def test_identity_must_be_a_name(monkeypatch, ident):
    spawned = _fake_client(monkeypatch, _free_port())
    assert bl.open_lease(ident)[0] is None and spawned == []


def test_navigate_takes_an_identity_never_a_port(monkeypatch):
    import model_tools  # noqa: F401
    from tools import browser_tool as bt
    from tools.registry import registry
    schema = next(s for s in bt.BROWSER_TOOL_SCHEMAS if s["name"] == "browser_navigate")
    props = schema["parameters"]["properties"]
    assert "lease_identity" in props and "lease_cdp_port" not in props
    assert props["lease_class"]["enum"] == ["read", "act"]
    monkeypatch.setattr("tools.rail_preflight.browser_capability_error", lambda **_k: None)
    monkeypatch.setattr(bt, "browser_navigate", lambda url, task_id=None: json.dumps({"success": True, "url": url}))
    cleaned = []
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: cleaned.append(1))
    port = _free_port()

    def nav(**extra):
        return json.loads(registry.dispatch("browser_navigate", {"url": "https://github.com", **extra}, task_id="t"))
    # a port argument is ignored: with no lease the loopback override is refused as before
    monkeypatch.setenv("BROWSER_CDP_URL", f"http://127.0.0.1:{port}")
    assert nav(lease_cdp_port=port)["error"] == EXPECTED.format(host="ace-ai")
    _fake_client(monkeypatch, port)
    lease = {"lease_id": 12473, "identity": "id-github-kyzcreig", "class": "read", "expires_at": 1795213000.5,
             "release": "browser_lease_release"}
    assert nav(lease_identity="id-github-kyzcreig") == {"success": True, "url": "https://github.com", "lease": lease}
    assert cleaned == [1]
    assert nav() == {"success": True, "url": "https://github.com"}, "later calls stay on the lease"
    assert nav(lease_identity="id-github-kyzcreig") == {"success": True, "url": "https://github.com", "lease": lease}
    assert cleaned == [1], "the same live lease is reused, not re-acquired"


def test_navigate_returns_the_lease_id_and_release_ends_only_this_runs_lease(monkeypatch):
    """t_62be0ba9: the leasing navigate names its lease (id, identity, class, expiry) and no grant URL; the release
    tool ends that lease (browserctl's release answer comes back), later calls fail closed, and a second release
    finds nothing to release."""
    import model_tools  # noqa: F401
    from tools import browser_tool as bt
    from tools.registry import registry
    monkeypatch.setattr("tools.rail_preflight.browser_capability_error", lambda **_k: None)
    monkeypatch.setattr(bt, "browser_navigate", lambda url, task_id=None: json.dumps({"success": True, "url": url}))
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    port = _free_port()
    _fake_client(monkeypatch, port)
    raw = registry.dispatch("browser_navigate", {"url": "https://github.com", "lease_identity": "id-github-kyzcreig"},
                            task_id="t")
    out = json.loads(raw)
    assert out["lease"]["lease_id"] == 12473, "the lease id is in the navigate result"
    assert (out["lease"]["identity"], out["lease"]["class"], out["lease"]["expires_at"]) == (
        "id-github-kyzcreig", "read", 1795213000.5)
    assert "SECRET" not in raw and "cdp_url" not in raw and "viewer_url" not in raw
    lease = bl._active
    assert bt.check_browser_lease_release_requirements(), "a kanban worker sees the release tool"
    rel = json.loads(registry.dispatch("browser_lease_release", {}, task_id="t"))
    assert rel == {"success": True, "released": True, "lease_id": 12473, "identity": "id-github-kyzcreig",
                   "class": "read", "release_answer": "12473"}
    assert lease.proc.poll() is not None, "the lease client has exited"
    assert rp.browser_rail_error(f"http://127.0.0.1:{port}").startswith("BROWSER_LEASE_UNAVAILABLE")
    again = json.loads(registry.dispatch("browser_lease_release", {}, task_id="t"))
    assert (again["success"], again["released"]) == (True, False)


def test_the_release_tool_is_for_kanban_workers_only(monkeypatch):
    from tools import browser_tool as bt
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert not bt.check_browser_lease_release_requirements()


def test_lease_is_for_kanban_workers_only(monkeypatch):
    from tools import browser_tool as bt
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    spawned = _fake_client(monkeypatch, _free_port())
    assert "kanban workers" in bt._connect_lease("id-github-kyzcreig")
    assert spawned == []


def test_spawn_runs_the_installed_client_inside_the_terminal_backend(monkeypatch):
    from tools.bot_desktop import placement
    from tools.environments import streams
    env = object()
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "ssh"))
    monkeypatch.setattr(placement, "terminal_environment", lambda create=True: env)
    got = []
    monkeypatch.setattr(streams, "open_stream", lambda e, argv, **kw: got.append((e, argv, kw)) or "proc")
    assert bl._spawn("id-github-kyzcreig", "read", "t_lease001") == "proc"
    (e, argv, kw), = got
    assert e is env and argv[:3] == ["/usr/bin/python3", "-I", "-c"] and argv[4:] == [
        "id-github-kyzcreig", "read", "t_lease001"]
    assert "child_env" not in kw, "nothing from this process's environment is passed"
    src = argv[3]
    assert "/usr/local/bin/browserctl" in src and "/opt/browsers/lease-cdp-forward.py" in src
    assert "TOKEN" not in src and "PATH" not in src


def _fake_browserctl(tmp_path, port):
    """An installed-client stand-in: prints the forwarder's ``port=N``, releases on SIGTERM like `with`."""
    released = tmp_path / "released"
    ctl = tmp_path / "browserctl"
    ctl.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import signal, sys, time
        def rel(*_a):
            open({str(released)!r}, "w").write(" ".join(sys.argv[1:]))
            sys.exit(143)
        signal.signal(signal.SIGTERM, rel)
        print("port={port}", flush=True)
        while True:
            time.sleep(0.1)
        """), encoding="utf-8")
    ctl.chmod(0o755)
    fwd = tmp_path / "fwd"
    fwd.write_text("", encoding="utf-8")
    return ctl, fwd, released


def _wait(path, s=15.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not path.exists():
        time.sleep(0.05)
    return path.exists()


def test_watcher_releases_when_the_tool_closes_its_stdin(tmp_path):
    ctl, fwd, released = _fake_browserctl(tmp_path, 4711)
    p = subprocess.Popen([sys.executable, "-I", "-c", bl.watch_source(str(ctl), ((str(fwd),),)),
                          "id-github-kyzcreig", "read", "t_lease001"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert p.stdout.readline().strip() == b"port=4711"
    p.stdin.close()
    assert _wait(released)
    got = released.read_text(encoding="utf-8-sig")
    assert got.startswith("with id-github-kyzcreig --class read --card t_lease001 -- /usr/bin/python3 -I -c ")
    assert got.endswith(f" {fwd} --serve") and bl.EXIT_LINE in got
    assert p.wait(15) == 143


def test_watcher_releases_when_the_worker_is_killed(tmp_path):
    # (lifetime) the worker dies with SIGKILL: its pipe closes, the watcher reads EOF, `with` releases.
    ctl, fwd, released = _fake_browserctl(tmp_path, 4712)
    worker = textwrap.dedent(f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-I", "-c", {bl.watch_source(str(ctl), ((str(fwd),),))!r},
                              "id-github-kyzcreig", "read", "t_lease001"], stdin=subprocess.PIPE, stdout=sys.stdout)
        p.wait()
    """)
    w = subprocess.Popen([sys.executable, "-c", worker], stdout=subprocess.PIPE, start_new_session=True)
    assert w.stdout.readline().strip() == b"port=4712"
    os.kill(w.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    w.wait(10)
    assert _wait(released)


def test_watcher_refuses_without_an_installed_client(tmp_path):
    out = subprocess.run([sys.executable, "-I", "-c", bl.watch_source(str(tmp_path / "nope"), ((str(tmp_path / "x"),),)),
                          "id-github-kyzcreig", "read", "t"], input=b"", capture_output=True, timeout=15)
    assert out.returncode == 2 and b"BROWSER_LEASE_CLIENT_ABSENT" in out.stdout


def _cdp_server():
    """A reachable CDP-shaped endpoint that is NOT the lease: /json/version answers 200."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"Browser": "configured", "webSocketDebuggerUrl": ""}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("where", ["placed-remote-cdp", "studio-loopback-cdp"])
def test_a_dead_lease_never_falls_back_to_the_configured_browser(monkeypatch, where):
    """Prism 52ec81c4713e: a reachable non-lease BROWSER_CDP_URL is set, the lease child is killed, and
    browser_snapshot must refuse instead of running on the configured browser."""
    import model_tools  # noqa: F401
    from tools import browser_tool as bt
    from tools.registry import registry
    srv = None
    if where == "placed-remote-cdp":
        configured = "http://192.0.2.10:9222"   # not loopback: the rail admits it from any host
    else:
        monkeypatch.delenv(kwh.PLACEMENT_ENV)  # a worker on the dispatcher host: loopback is its own
        srv = _cdp_server()
        configured = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        monkeypatch.setenv("BROWSER_CDP_URL", configured)
        monkeypatch.setattr("tools.rail_preflight.browser_capability_error", lambda **_k: None)
        monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
        ran = []
        monkeypatch.setattr(bt, "browser_snapshot", lambda **kw: ran.append(kw) or json.dumps({"success": True}))
        monkeypatch.setattr(bt, "browser_navigate", lambda url, task_id=None: json.dumps({"success": True}))
        snap = lambda: json.loads(registry.dispatch("browser_snapshot", {}, task_id="t"))  # noqa: E731
        assert snap() == {"success": True}, "control: before any lease the configured browser is used"
        ran.clear()
        _fake_client(monkeypatch, _free_port())
        nav = json.loads(registry.dispatch("browser_navigate", {"url": "https://github.com",
                                                                "lease_identity": "id-github-kyzcreig"}, task_id="t"))
        assert nav["success"] is True and nav["lease"]["lease_id"] == 12473
        lease = bl._active
        os.kill(lease.proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        lease.proc.wait(10)
        out = snap()
        assert out.get("success") is not True and ran == []
        assert out["error"].startswith("BROWSER_LEASE_UNAVAILABLE identity id-github-kyzcreig (read)")
    finally:
        if srv is not None:
            srv.shutdown()


def test_a_forwarder_exit_kills_the_grant_while_the_watcher_still_lives(monkeypatch):
    """Prism f81ab96c1c7d: the forwarder exits (its port may be reused) while the watcher/browserctl are
    still alive and releasing; the port is refused at once."""
    port = _free_port()
    body = textwrap.dedent(f"""
        import sys
        print("port={port}", flush=True)
        sys.stdin.readline()                       # the test's go: the forwarder exits now
        print("{bl.EXIT_LINE} rc=1", flush=True)   # what FORWARDER_SHIM prints
        sys.stdin.read()                           # the watcher itself stays alive
    """)
    monkeypatch.setattr(bl, "_spawn", lambda *a: subprocess.Popen(
        [sys.executable, "-c", body], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
    lease, err = bl.open_lease("id-github-kyzcreig", timeout=10)
    assert err is None
    url = lease.url
    assert rp.browser_rail_error(url) is None
    lease.proc.stdin.write(b"go\n")
    lease.proc.stdin.flush()
    assert lease.gone.wait(10)
    assert lease.proc.poll() is None, "the watcher is still alive"
    assert not lease.alive() and not bl.is_lease_url(url)
    assert rp.browser_rail_error(url).startswith("BROWSER_LEASE_UNAVAILABLE")


def test_the_shim_announces_the_forwarder_exit_through_the_real_watcher(tmp_path, monkeypatch):
    """End to end on the real watcher + FORWARDER_SHIM: browserctl stays up after its child exits (it is
    still releasing), the forwarder is gone, and the tool drops the grant from the shim's line."""
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("the watcher runs the shim under /usr/bin/python3")
    port = _free_port()
    go = tmp_path / "go"
    fwd = tmp_path / "fwd"
    fwd.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, time
        print("port={port}", flush=True)
        while not os.path.exists({str(go)!r}):
            time.sleep(0.05)
        """), encoding="utf-8")
    fwd.chmod(0o755)
    ctl = tmp_path / "browserctl"
    ctl.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import subprocess, sys, time
        cmd = sys.argv[sys.argv.index("--") + 1:]
        subprocess.Popen(cmd).wait()
        time.sleep(60)                      # releasing: still alive after the forwarder exited
        """), encoding="utf-8")
    ctl.chmod(0o755)
    monkeypatch.setattr(bl, "_spawn", lambda i, k, c: subprocess.Popen(
        [sys.executable, "-I", "-c", bl.watch_source(str(ctl), ((str(fwd),),)), i, k, c],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
    lease, err = bl.open_lease("id-github-kyzcreig", timeout=15)
    assert err is None and lease.port == port
    assert rp.browser_rail_error(lease.url) is None
    go.write_text("", encoding="utf-8")
    assert lease.gone.wait(15)
    assert lease.proc.poll() is None, "watcher and browserctl still alive"
    assert rp.browser_rail_error(lease.url).startswith("BROWSER_LEASE_UNAVAILABLE")
    assert any(t.startswith(bl.EXIT_LINE) for t in lease.tail)


def _gate_records(monkeypatch, refuse: bool = False):
    """capability_gate recording the capability each browser call asks for (#477 split)."""
    asked = []

    def gate(cap):
        asked.append(cap)
        return f"BROWSER_RAIL_ABSENT cap={cap}" if refuse else None
    monkeypatch.setattr("hermes_cli.kanban_placement_failfast.capability_gate", gate)
    return asked


def test_a_leasing_call_is_gated_on_the_identity_capability(monkeypatch):
    """Review item 2: with no override yet, the call that acquires the lease is identity work: it asks
    for browser-session, not browser-public, and a refusal spawns no lease client."""
    from tools import browser_tool as bt
    asked = _gate_records(monkeypatch, refuse=True)
    spawned = _fake_client(monkeypatch, _free_port())
    call = bt._fallback_call("browser_navigate", {"url": ""})
    out = json.loads(call({"url": "https://github.com", "lease_identity": "id-github-kyzcreig"}, {"task_id": "t"}))
    assert asked == ["browser-session"]
    assert out["error"] == "BROWSER_RAIL_ABSENT cap=browser-session"
    assert spawned == [], "no lease client when the gate refuses"


def test_a_lease_never_goes_to_camofox(monkeypatch):
    """Review item 1: with Camofox selected as the backend, a leased call stays on the lease."""
    from tools import browser_camofox, browser_tool as bt
    _gate_records(monkeypatch)
    _fake_client(monkeypatch, _free_port())
    monkeypatch.setattr("tools.tool_backend_helpers.read_selection", lambda kind: "camofox")
    monkeypatch.setattr(browser_camofox, "_config_cdp_url", lambda: "")
    assert browser_camofox.is_camofox_mode(), "precondition: Camofox is the selected backend"
    camo = []
    monkeypatch.setattr(bt, "_camofox", lambda *a: camo.append(a) or json.dumps({"success": True, "camofox": True}))
    monkeypatch.setattr(bt._session, "_run_browser_command",
                        lambda key, cmd, args=None, **kw: {"success": True, "data": {"title": "t", "url": "u"}})
    monkeypatch.setattr(bt._session, "_get_session_info", lambda key: {})
    monkeypatch.setattr(bt, "_maybe_start_recording", lambda key: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    call = bt._fallback_call("browser_navigate", {"url": ""})
    call({"url": "https://github.com", "lease_identity": "id-github-kyzcreig"}, {"task_id": "t"})
    assert camo == []
    assert not browser_camofox.is_camofox_mode()
    bl.clear_lease()
    assert browser_camofox.is_camofox_mode(), "an explicit clear gives Camofox back"


def test_the_grant_json_urls_never_reach_the_model(monkeypatch):
    """Review item 3: browserctl's stderr grant JSON (viewer_url carries a view ticket) is merged into the
    watcher's stdout; no *_url value is kept in the tail that goes into an error line."""
    grant = json.dumps({"lease_id": 1, "class": "read", "viewer_url": "https://studio/view?ticket=SECRET1",
                        "cdp_url": "ws://x/SECRET2"})
    _fake_client(monkeypatch, 0, refuse=grant)
    lease, err = bl.open_lease("id-github-kyzcreig", "read", timeout=10)
    assert lease is None
    assert "SECRET" not in err and "lease_id" in err
    time.sleep(0.1)
    assert "SECRET" not in (bl.unavailable_error() or "")
