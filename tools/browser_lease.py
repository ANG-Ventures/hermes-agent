"""A kanban worker's browser_* tools on a Studio identity lease (t_ce9bd5a3).

``browser_navigate(lease_identity=<id>)`` starts the host's installed lease client as this tool's own
child, on the host the browser runs on (inside the terminal backend for a placed worker):
``browserctl with <id> --class read|act --card <card> -- <lease forwarder> --serve``. The Studio gate
answers the acquire; ``buy``/``human`` stay refused from a pool host there. The forwarder prints
``port=N`` once it listens, and that port, read from the child's own stdout, is the only loopback CDP
endpoint a placed worker's browser tools may use while the child lives. The port never comes from the
model, the environment or a cache, and the lease token never leaves ``browserctl``'s child.

The forwarder runs under a stdlib shim (``FORWARDER_SHIM``) that prints ``lease-forwarder-exit rc=N``
the moment the forwarder exits. From that line (or the end of the child's stdout) the lease is dead even
while the watcher and ``browserctl`` are still releasing, so a port the forwarder gave up is never admitted.

Selection: once ``browser_navigate(lease_identity=...)`` selects an identity, the browser tools stay on it
until another identity replaces it or ``clear_lease()`` runs. If its child has exited, every browser call
fails closed with ``BROWSER_LEASE_UNAVAILABLE``; it never falls back to ``BROWSER_CDP_URL`` or
``browser.cdp_url`` (Prism 52ec81c4713e).

Lifetime: the local child (``ssh`` for a placed worker) stays in the worker's process group. A small
watcher on the browser host holds the remote end: when its stdin reaches EOF (the tool closed it, the
worker exited, or the worker was killed and the ssh session dropped) or it gets a signal, it SIGTERMs
``browserctl``, which ends the forwarder and releases the lease.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

BROWSERCTL = "/usr/local/bin/browserctl"
# The installed forwarder, by absolute path: the browsers-client wrapper when present, else the copy
# /opt/browsers carries (ace-ai has only that one).
FORWARDERS = (("/usr/local/bin/browser-lease-forward",),
              ("/usr/bin/python3", "/opt/browsers/lease-cdp-forward.py"))
CLASSES = ("read", "act")
PORT_WAIT_S = 120.0
EXIT_LINE = "lease-forwarder-exit"
# A placeholder that is never a listener: the CDP override while a selected identity has no live lease.
DEAD_URL = "http://127.0.0.1:0"

# Runs under ``browserctl with`` as ``/usr/bin/python3 -I -c FORWARDER_SHIM <forwarder argv>``: the
# forwarder inherits the lease env and stdout; its exit is announced on the same stdout at once.
FORWARDER_SHIM = r'''
import subprocess, sys
p = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)  # the token rides env; nothing reads stdin
rc = p.wait()
print("%(exit_line)s rc=%%d" %% rc, flush=True)
sys.exit(rc if rc >= 0 else 1)
''' % {"exit_line": EXIT_LINE}

# Runs on the browser host as ``/usr/bin/python3 -I -c <watch_source()> <identity> <class> <card>``.
_WATCH = r'''
import os, signal, subprocess, sys, threading
BROWSERCTL = %(browserctl)r
FORWARDERS = %(forwarders)r
SHIM = %(shim)r
ident, klass, card = sys.argv[1:4]
fwd = next((list(f) for f in FORWARDERS if all(os.path.exists(p) for p in f)), None)
if fwd is None or not os.access(BROWSERCTL, os.X_OK):
    print("BROWSER_LEASE_CLIENT_ABSENT no installed lease client on this host", flush=True)
    sys.exit(2)
argv = ([BROWSERCTL, "with", ident, "--class", klass, "--card", card, "--", "/usr/bin/python3", "-I", "-c", SHIM]
        + fwd + ["--serve"])
p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     start_new_session=True)
lock = threading.Lock()
stopping = []
def stop(*_a):
    with lock:
        if stopping:
            return
        stopping.append(1)
    try:
        os.kill(p.pid, signal.SIGTERM)   # browserctl ends the forwarder and releases the lease
    except ProcessLookupError:
        return
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(s, lambda *_a: threading.Thread(target=stop).start())
def watch():
    while True:
        try:
            if not os.read(0, 4096):
                break
        except OSError:
            break
    stop()
threading.Thread(target=watch, daemon=True).start()
for line in iter(p.stdout.readline, b""):
    try:
        os.write(1, line)
    except OSError:
        stop()
        break
sys.exit(p.wait())
'''


def watch_source(browserctl: str = BROWSERCTL, forwarders=FORWARDERS) -> str:
    return _WATCH % {"browserctl": browserctl, "forwarders": tuple(tuple(f) for f in forwarders),
                     "shim": FORWARDER_SHIM}


@dataclass
class Lease:
    identity: str
    klass: str
    port: int
    proc: subprocess.Popen
    tail: List[str] = field(default_factory=list)
    # set when the forwarder announced its exit or the child's stdout ended (``_read_port``)
    gone: threading.Event = field(default_factory=threading.Event)
    # t_62be0ba9: lease_id / identity / class / expires_at from browserctl's grant line; ``released`` its release line
    grant: dict = field(default_factory=dict)
    # set when the child's stdout ended: every line it printed (the release line too) has been read
    closed: threading.Event = field(default_factory=threading.Event)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        """The watcher runs AND its forwarder has not exited (Prism f81ab96c1c7d)."""
        return not self.gone.is_set() and self.proc.poll() is None


_lock = threading.RLock()
_active: Optional[Lease] = None
_selected: Optional[Tuple[str, str]] = None   # (identity, class) chosen by browser_navigate; see clear_lease


def _spawn(identity: str, klass: str, card: str) -> subprocess.Popen:
    """The watcher as this tool's child, on the host agent-browser runs on."""
    argv = ["/usr/bin/python3", "-I", "-c", watch_source(), identity, klass, card]
    from tools.bot_desktop import placement
    if placement.resolve().where == placement.TERMINAL:
        from tools.environments import streams
        env = placement.terminal_environment(create=True)
        if env is None:
            raise RuntimeError("no terminal backend to start the lease client in")
        return streams.open_stream(env, argv, stderr=subprocess.DEVNULL)
    return subprocess.Popen(  # windows-footgun: ok — the lease client exists only on macOS/Linux fleet hosts
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)


_URL_VALUE = re.compile(r'("[A-Za-z0-9_]*url"\s*:\s*)"[^"]*"?', re.IGNORECASE)


def _redact(line: str) -> str:
    """browserctl's stderr grant JSON rides the same stream; its ``*_url`` values (``viewer_url`` carries
    a view ticket) never reach the tail an error line is built from."""
    return _URL_VALUE.sub(r'\1"<redacted>"', line)


GRANT_KEYS = ("lease_id", "identity", "class", "expires_at")   # t_62be0ba9: never a *_url, token or ticket
RELEASE_LINE = "browserctl with: released="


def _grant_fields(line: str) -> dict:
    """The grant JSON browserctl prints on stderr before it runs the forwarder: only ``GRANT_KEYS``."""
    if not line.startswith("{"):
        return {}
    try:
        g = json.loads(line)
    except ValueError:
        return {}
    if not isinstance(g, dict) or "lease_id" not in g:
        return {}
    return {k: g.get(k) for k in GRANT_KEYS}


def _read_port(proc: subprocess.Popen, tail: List[str], timeout: float,
               gone: Optional[threading.Event] = None, grant: Optional[dict] = None,
               closed: Optional[threading.Event] = None) -> Optional[int]:
    """Drain the child's stdout for its whole life (keeps tail lines); return the first ``port=N``.
    ``gone`` is set when the forwarder announces its exit or the stdout ends; ``grant`` gets the grant's
    ``GRANT_KEYS`` and browserctl's release line; ``closed`` is set when the stdout ends."""
    gone = gone if gone is not None else threading.Event()
    grant = grant if grant is not None else {}
    closed = closed if closed is not None else threading.Event()
    got: dict = {}
    ready = threading.Event()

    def drain():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail.append(_redact(line)[:300])
            del tail[:-8]
            if "lease_id" not in grant:
                grant.update(_grant_fields(line))
            if line.startswith(RELEASE_LINE):
                grant["released"] = (line[len(RELEASE_LINE):].split() or [""])[0]
            if line.startswith(EXIT_LINE):
                gone.set()
                ready.set()
                continue
            if "port" not in got and line.startswith("port="):
                try:
                    port = int(line[5:])
                except ValueError:
                    continue
                if 0 < port < 65536:
                    got["port"] = port
                    ready.set()
        gone.set()
        ready.set()
        closed.set()
    threading.Thread(target=drain, name="browser-lease-stdout", daemon=True).start()
    ready.wait(timeout)
    return got.get("port")


# t_239fe5ae: how long release_lease waits for browserctl's release line after the watcher's stdin EOF. Measured on
# ace-media, 10 read leases each after one navigate: 10.3-10.6 s nine times and 19.6 s once (browserctl SIGKILLs a
# forwarder still holding a CDP connection after 10 s, then the release call). The watcher SIGKILLs browserctl 30 s
# after its SIGTERM, so no answer can arrive after about 30 s: wait past that.
RELEASE_WAIT_S = 35.0


def _end(lease: Lease, timeout: float = 40.0) -> None:
    proc = lease.proc
    try:
        if proc.stdin:
            proc.stdin.close()   # EOF at the watcher: browserctl releases the lease
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def close_lease() -> None:
    """End the active lease (if any) and wait for its release. The selection stays: later browser
    calls fail closed until ``open_lease`` replaces it or ``clear_lease`` drops it."""
    global _active
    with _lock:
        lease, _active = _active, None
    if lease is not None:
        _end(lease)


def clear_lease() -> None:
    """End the active lease and drop the selection: the configured CDP override applies again."""
    global _selected
    with _lock:
        _selected = None
        close_lease()


atexit.register(close_lease)


def open_lease(identity: str, klass: str = "read", *, card: str = "",
               timeout: float = PORT_WAIT_S) -> Tuple[Optional[Lease], Optional[str]]:
    """The live lease for ``identity`` (started now, or the one already held), else an error line."""
    global _active, _selected
    identity = str(identity or "").strip()
    klass = str(klass or "read").strip()
    if not identity or not all(c.isalnum() or c in "-_." for c in identity) or identity.startswith("-"):
        return None, "lease_identity must be a browser identity name such as id-github-kyzcreig"
    if klass not in CLASSES:
        return None, f"lease_class must be one of {', '.join(CLASSES)}; buy and human leases are operator verbs"
    with _lock:
        cur = _active
        if cur is not None and cur.alive() and (cur.identity, cur.klass) == (identity, klass):
            return cur, None
        _selected = (identity, klass)   # from here on a failure fails closed, never to the configured CDP
        close_lease()
        try:
            proc = _spawn(identity, klass, card)
        except Exception as e:
            return None, f"could not start the lease client: {e}"
        tail: List[str] = []
        gone, closed, grant = threading.Event(), threading.Event(), {}
        port = _read_port(proc, tail, timeout, gone, grant, closed)
        lease = Lease(identity, klass, port or 0, proc, tail, gone, grant, closed)
        if port is None or not lease.alive():
            _end(lease)
            time.sleep(0.05)
            why = " | ".join(tail[-3:]) or "no port printed"
            return None, f"browser lease {identity} ({klass}) not granted: {why}"
        _active = lease
        return lease, None


def lease_info(lease: Optional[Lease] = None) -> Optional[dict]:
    """t_62be0ba9: what the model may know about the live lease: its id, identity, class and expiry (the grant's;
    heartbeats renew it while the lease is held). None when no lease is live."""
    with _lock:
        lease = lease if lease is not None else _active
    if lease is None or not lease.alive():
        return None
    g = lease.grant
    return {"lease_id": g.get("lease_id"), "identity": g.get("identity") or lease.identity,
            "class": g.get("class") or lease.klass, "expires_at": g.get("expires_at"),
            "release": "browser_lease_release"}


def release_lease() -> dict:
    """t_62be0ba9: release THIS process's own lease (the one its browser_navigate started) and nothing else, and
    wait for browserctl's release answer. The selection stays, so later browser calls fail closed."""
    global _active
    with _lock:
        lease, _active = _active, None
    if lease is None:
        return {"success": True, "released": False, "detail": "this run holds no browser lease"}
    already_exited = lease.proc.poll() is not None   # Prism ba98afcb8f7a: then closing stdin sends no release
    _end(lease, RELEASE_WAIT_S)
    lease.closed.wait(5)
    g = lease.grant
    ans = g.get("released") or ""
    out = {"lease_id": g.get("lease_id"), "identity": lease.identity, "class": lease.klass}
    if not ans and already_exited:
        return dict(out, released=None, release_answer="none", success=False,
                    detail=f"lease client had already exited; no release sent: check egress-status --lease "
                           f"{g.get('lease_id')}")
    if not ans:
        # t_239fe5ae / Prism a0356f510f32: no answer read, and the watcher may have died before handling its stdin
        # EOF, so whether a release went out is unknown. success stays true (nothing known was refused); outcome
        # "unknown" keeps a caller from reading it as released.
        return dict(out, released=None, release_answer="not read", outcome="unknown", success=True,
                    detail=f"release attempted, outcome unknown: check egress-status --lease {g.get('lease_id')}; "
                           f"the lease also ends at its expiry once this run's client is gone")
    # browserctl prints ``released=<lease id>`` (browserd's release answer) or ``released=<refusal token>``
    ok = ans.isdigit() and (g.get("lease_id") is None or ans == str(g.get("lease_id")))
    out.update(released=ok, release_answer=ans)
    out["success"] = out["released"]
    return out


def selected() -> Optional[Tuple[str, str]]:
    """The (identity, class) the browser tools are bound to, live or not; None when none is selected."""
    with _lock:
        return _selected


def override_url() -> str:
    """The CDP override the lease selection imposes: the live lease URL, ``DEAD_URL`` when the selected
    identity has no live lease, "" when no identity is selected (the configured override applies)."""
    with _lock:
        sel, lease = _selected, _active
    if sel is None:
        return ""
    if lease is not None and lease.alive() and (lease.identity, lease.klass) == sel:
        return lease.url
    return DEAD_URL


def unavailable_error() -> Optional[str]:
    """``BROWSER_LEASE_UNAVAILABLE`` when an identity is selected but its lease child has exited."""
    with _lock:
        sel, lease = _selected, _active
    if sel is None or override_url() != DEAD_URL:
        return None
    why = " | ".join((lease.tail if lease is not None else [])[-2:]) or "no live lease client"
    return (f"BROWSER_LEASE_UNAVAILABLE identity {sel[0]} ({sel[1]}): its lease client has exited ({why}). "
            f"Call browser_navigate with lease_identity={sel[0]} to lease it again; the configured browser "
            f"is not used in its place")


def active_url() -> str:
    """The CDP URL of the live lease this process started, else ""."""
    with _lock:
        lease = _active
    return lease.url if lease is not None and lease.alive() else ""


def is_lease_url(cdp_url: str) -> bool:
    """``cdp_url`` is the loopback port of the live lease this process started."""
    url = active_url()
    if not url or not cdp_url:
        return False
    from agent.proxy_bypass import is_loopback_host, split_host_port
    host, port = split_host_port(cdp_url.strip())
    return bool(is_loopback_host(host)) and str(port) == url.rsplit(":", 1)[1]
