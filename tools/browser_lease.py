"""A kanban worker's browser_* tools on a Studio identity lease (t_ce9bd5a3).

``browser_navigate(lease_identity=<id>)`` starts the host's installed lease client as this tool's own
child, on the host the browser runs on (inside the terminal backend for a placed worker):
``browserctl with <id> --class read|act --card <card> -- <lease forwarder> --serve``. The Studio gate
answers the acquire; ``buy``/``human`` stay refused from a pool host there. The forwarder prints
``port=N`` once it listens, and that port, read from the child's own stdout, is the only loopback CDP
endpoint a placed worker's browser tools may use while the child lives. The port never comes from the
model, the environment or a cache, and the lease token never leaves ``browserctl``'s child.

Lifetime: the local child (``ssh`` for a placed worker) stays in the worker's process group. A small
watcher on the browser host holds the remote end: when its stdin reaches EOF (the tool closed it, the
worker exited, or the worker was killed and the ssh session dropped) or it gets a signal, it SIGTERMs
``browserctl``, which ends the forwarder and releases the lease.
"""
from __future__ import annotations

import atexit
import os
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

# Runs on the browser host as ``/usr/bin/python3 -I -c <watch_source()> <identity> <class> <card>``.
_WATCH = r'''
import os, signal, subprocess, sys, threading
BROWSERCTL = %(browserctl)r
FORWARDERS = %(forwarders)r
ident, klass, card = sys.argv[1:4]
fwd = next((list(f) for f in FORWARDERS if all(os.path.exists(p) for p in f)), None)
if fwd is None or not os.access(BROWSERCTL, os.X_OK):
    print("BROWSER_LEASE_CLIENT_ABSENT no installed lease client on this host", flush=True)
    sys.exit(2)
argv = [BROWSERCTL, "with", ident, "--class", klass, "--card", card, "--"] + fwd + ["--serve"]
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
    return _WATCH % {"browserctl": browserctl, "forwarders": tuple(tuple(f) for f in forwarders)}


@dataclass
class Lease:
    identity: str
    klass: str
    port: int
    proc: subprocess.Popen
    tail: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc.poll() is None


_lock = threading.RLock()
_active: Optional[Lease] = None


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


def _read_port(proc: subprocess.Popen, tail: List[str], timeout: float) -> Optional[int]:
    """Drain the child's stdout for its whole life (keeps tail lines); return the first ``port=N``."""
    got: dict = {}
    ready = threading.Event()

    def drain():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail.append(line[:300])
            del tail[:-8]
            if "port" not in got and line.startswith("port="):
                try:
                    port = int(line[5:])
                except ValueError:
                    continue
                if 0 < port < 65536:
                    got["port"] = port
                    ready.set()
        ready.set()
    threading.Thread(target=drain, name="browser-lease-stdout", daemon=True).start()
    ready.wait(timeout)
    return got.get("port")


def _end(lease: Lease) -> None:
    proc = lease.proc
    try:
        if proc.stdin:
            proc.stdin.close()   # EOF at the watcher: browserctl releases the lease
    except OSError:
        pass
    try:
        proc.wait(timeout=40)
    except subprocess.TimeoutExpired:
        proc.kill()


def close_lease() -> None:
    """End the active lease (if any) and wait for its release."""
    global _active
    with _lock:
        lease, _active = _active, None
    if lease is not None:
        _end(lease)


atexit.register(close_lease)


def open_lease(identity: str, klass: str = "read", *, card: str = "",
               timeout: float = PORT_WAIT_S) -> Tuple[Optional[Lease], Optional[str]]:
    """The live lease for ``identity`` (started now, or the one already held), else an error line."""
    global _active
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
        close_lease()
        try:
            proc = _spawn(identity, klass, card)
        except Exception as e:
            return None, f"could not start the lease client: {e}"
        tail: List[str] = []
        port = _read_port(proc, tail, timeout)
        lease = Lease(identity, klass, port or 0, proc, tail)
        if port is None or not lease.alive():
            _end(lease)
            time.sleep(0.05)
            why = " | ".join(tail[-3:]) or "no port printed"
            return None, f"browser lease {identity} ({klass}) not granted: {why}"
        _active = lease
        return lease, None


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
