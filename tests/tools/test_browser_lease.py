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
    bl.close_lease()
    rp._emitted.clear()
    rp._probe_ok.clear()
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lease001")
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "ace-ai", "env": {"TERMINAL_ENV": "ssh"}}))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("KANBAN_PLACEMENT_CAPS", raising=False)
    yield
    bl.close_lease()
    rp._emitted.clear()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_client(monkeypatch, port: int, *, then_exit: bool = False, refuse: str = ""):
    """``_spawn`` replaced by a child that behaves like the watcher: prints ``port=N`` (or a refusal)."""
    spawned = []
    body = f"print({refuse!r}, flush=True); raise SystemExit(3)" if refuse else textwrap.dedent(f"""
        import sys, time
        print("lease-cdp-forward: gateway ok", flush=True)
        print("port={port}", flush=True)
        if {then_exit!r}:
            raise SystemExit(0)
        sys.stdin.read()
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
    # (2) the child ends (lease released or expired: browserctl exits) -> the same port is refused
    lease.proc.stdin.close()
    lease.proc.wait(10)
    assert bl.active_url() == ""
    assert rp.browser_rail_error(url) == EXPECTED.format(host="ace-ai")
    assert cdp._get_cdp_override_raw() == "http://127.0.0.1:9222"


def test_an_expired_lease_is_refused_on_the_next_call(monkeypatch):
    port = _free_port()
    _fake_client(monkeypatch, port, then_exit=True)   # browserctl rc 75: the lease ended under `with`
    lease, err = bl.open_lease("id-github-kyzcreig", timeout=10)
    if lease is not None:
        lease.proc.wait(10)
    assert bl.active_url() == ""
    assert rp.browser_rail_error(f"http://127.0.0.1:{port}") == EXPECTED.format(host="ace-ai")


def test_a_refused_acquire_gives_no_port(monkeypatch):
    # (4) buy/human are refused by the Studio gate; the refusal line comes back, no port is admitted.
    _fake_client(monkeypatch, 0, refuse="BROWSER_REMOTE_CLASS_REFUSED class buy is not leasable by a remote worker")
    lease, err = bl.open_lease("id-github-kyzcreig", "read", timeout=10)
    assert lease is None and "BROWSER_REMOTE_CLASS_REFUSED" in err
    assert bl.active_url() == ""


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
    monkeypatch.setattr("tools.rail_preflight.browser_capability_error", lambda: None)
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
    assert nav(lease_identity="id-github-kyzcreig") == {"success": True, "url": "https://github.com"}
    assert cleaned == [1]
    assert nav() == {"success": True, "url": "https://github.com"}, "later calls stay on the lease"
    assert nav(lease_identity="id-github-kyzcreig") == {"success": True, "url": "https://github.com"}
    assert cleaned == [1], "the same live lease is reused, not re-acquired"


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
    assert released.read_text(encoding="utf-8-sig") == f"with id-github-kyzcreig --class read --card t_lease001 -- {fwd} --serve"
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
