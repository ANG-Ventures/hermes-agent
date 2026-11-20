"""Is 127.0.0.1:<port> a LIVE browser lease forwarder? (t_ce9bd5a3, Prism e44cea4791ec + 40878f845b06)

Runs on the host the worker's browser runs on (``python3 -I -c <this file> <port>`` inside the terminal
backend of a placed worker), stdlib only, Python 3.9+. Prints one JSON object: ``{"ok": true, "identity",
"class", "pid"}`` or ``{"ok": false, "why"}``.

A self-reported ``/json/version`` string proves nothing: any local process can serve it. What binds the
port to a live lease here:

1. the process LISTENING on the port runs as this uid, and is the root-installed ``lease-cdp-forward.py``
   run by a root-installed interpreter with no interpreter options: neither file, nor any directory above
   it, is writable by this uid, so this uid cannot have changed what that process runs;
2. its parent is the root-installed ``browserctl.py with <identity> --class read|act``. ``with`` holds the
   lease, heartbeats it, kills the forwarder when a beat is refused and releases the lease when it exits.
   ``buy``/``human`` (or any other class) are refused here as well as at the gate;
3. right now, ``/json/list`` answers 200: the forwarder made a fresh call to the browser gateway with its
   lease token, so the gateway accepted that token at this moment. An expired or released lease fails here.
   ``/json/version`` must name the same identity as the parent's argv.

Nothing is cached: the caller runs this before every browser call. The lease token is never read: not from
the forwarder's process env, nor from anywhere else.
"""
import json
import os
import subprocess
import sys
import urllib.request

FORWARDER = "lease-cdp-forward.py"
BROWSERCTL = "browserctl.py"
CLASSES = ("read", "act")
LEASE_PREFIX = "lease/"
LSOF = ("/usr/sbin/lsof", "/usr/bin/lsof")
TIMEOUT_S = 5.0


class Refused(Exception):
    pass


def _out(argv):
    return subprocess.run(argv, capture_output=True, timeout=TIMEOUT_S, check=False).stdout.decode("utf-8", "replace")


def listener(port):
    """(pid, uid) of the one process listening on TCP ``port``."""
    lsof = next((p for p in LSOF if os.path.exists(p)), None)
    if lsof is None:
        raise Refused("no lsof on this host")
    pids, uid = set(), None
    for line in _out([lsof, "-nP", "-a", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fpu"]).splitlines():
        if line.startswith("p"):
            pids.add(int(line[1:]))
        elif line.startswith("u"):
            uid = int(line[1:])
    if len(pids) != 1 or uid is None:
        raise Refused("%d listeners on port %d" % (len(pids), port))
    return pids.pop(), uid


def args(pid):
    if os.path.exists("/proc/%d/cmdline" % pid):
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    return _out(["ps", "-ww", "-o", "args=", "-p", str(pid)]).split()


def ppid(pid):
    return int(_out(["ps", "-o", "ppid=", "-p", str(pid)]).strip() or 0)


def exe(pid):
    """The executable the process runs (not its argv[0], which the caller chooses)."""
    if os.path.exists("/proc/%d/exe" % pid):
        return os.path.realpath("/proc/%d/exe" % pid)
    return _out(["ps", "-o", "comm=", "-p", str(pid)]).strip()


def trusted(path):
    """Root-owned, and neither ``path`` nor any directory above it (as given and resolved) is writable here."""
    if not path or not os.path.isabs(path):
        return False
    for p in {path, os.path.realpath(path)}:
        while True:
            try:
                if os.stat(p).st_uid != 0 or os.access(p, os.W_OK):
                    return False
            except OSError:
                return False
            if p == os.path.dirname(p):
                break
            p = os.path.dirname(p)
    return True


def _script(argv, name):
    """argv[1] when argv is ``<interpreter> <trusted .../name> ...`` with no interpreter options."""
    if len(argv) < 2 or os.path.basename(argv[1]) != name or not trusted(argv[1]):
        raise Refused("%s is not the installed %s" % (" ".join(argv[:2])[:200], name))
    return argv[1]


def lease_of(ctl_argv):
    """(identity, class) from ``browserctl.py [--socket S] with <identity> [--class C] ...``."""
    rest = ctl_argv[2:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    if "with" not in rest or rest.index("with") + 1 >= len(rest):
        raise Refused("parent is not `browserctl with <identity>`")
    i = rest.index("with")
    identity, klass = rest[i + 1], "read"
    opts = rest[i + 2:]
    for j, a in enumerate(opts):
        if a == "--class" and j + 1 < len(opts):
            klass = opts[j + 1]
        elif a.startswith("--class="):
            klass = a.split("=", 1)[1]
    if klass not in CLASSES:
        raise Refused("lease class %s is not leasable from a pool host (%s)" % (klass, ", ".join(CLASSES)))
    return identity, klass


def _get(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d%s" % (port, path), timeout=TIMEOUT_S) as r:
            return r.status, json.loads(r.read() or b"null")
    except Exception as e:  # noqa: BLE001 - any failure is "not live"
        return getattr(e, "code", 0) or 0, None


def verify(port):
    pid, uid = listener(port)
    if uid != os.getuid():
        raise Refused("listener pid %d runs as uid %d, not this worker's" % (pid, uid))
    _script(args(pid), FORWARDER)
    if not trusted(exe(pid)):
        raise Refused("forwarder pid %d runs an interpreter this uid can change" % pid)
    parent = ppid(pid)
    pargs = args(parent)
    _script(pargs, BROWSERCTL)
    if not trusted(exe(parent)):
        raise Refused("browserctl pid %d runs an interpreter this uid can change" % parent)
    identity, klass = lease_of(pargs)
    status, ver = _get(port, "/json/version")
    if status != 200 or not isinstance(ver, dict) or ver.get("Browser") != LEASE_PREFIX + identity:
        raise Refused("forwarder does not name lease %s" % identity)
    status, pages = _get(port, "/json/list")
    if status != 200 or not isinstance(pages, list):
        raise Refused("lease %s is not live at the gateway (/json/list %s)" % (identity, status))
    return {"ok": True, "identity": identity, "class": klass, "pid": pid}


def main(argv):
    try:
        port = int(argv[0])
        if not 0 < port < 65536:
            raise ValueError
        res = verify(port)
    except Refused as e:
        res = {"ok": False, "why": str(e)}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "why": "probe failed: %s" % type(e).__name__}
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
