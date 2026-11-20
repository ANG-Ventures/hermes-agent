"""lease_forwarder_probe: what binds a loopback CDP port to a LIVE browser lease (t_ce9bd5a3).

Operator hold on #472 (Apollo, 11-19): (1) a loopback port that is not a lease forwarder is refused;
(2) a forwarder whose lease expired or was released is refused (Prism 40878f845b06); (3) the marker
cannot be forged by a local process, and the token is never read (Prism e44cea4791ec); (4) buy/human
leases are refused from a pool host. The forge cases run REAL processes on this host's loopback.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tools import lease_forwarder_probe as probe

FWD = "/usr/local/libexec/browsers-client/lease-cdp-forward.py"
CTL = "/opt/browsers/browserctl.py"
PY = "/usr/bin/python3"
LIVE_VERSION = {"Browser": "lease/id-github-kyzcreig", "Protocol-Version": "1.3"}


def _genuine(monkeypatch, *, ctl_args=None, list_status=200, version=None, uid=None, parent=200, untrusted=()):
    """Model the process facts of a genuine forwarder under `browserctl with` on port 52656."""
    monkeypatch.setattr(probe, "listener", lambda port: (200, os.getuid() if uid is None else uid))
    argv = {200: [PY, FWD, "--serve"],
            100: ctl_args or [PY, CTL, "--socket", "/run/browserd/browserd.sock", "with", "id-github-kyzcreig",
                              "--class", "read", "--card", "t_x", "--", "/usr/local/bin/browser-lease-forward",
                              "--serve"],
            1: ["/sbin/launchd"]}
    monkeypatch.setattr(probe, "args", lambda pid: argv[pid])
    monkeypatch.setattr(probe, "ppid", lambda pid: parent)
    monkeypatch.setattr(probe, "exe", lambda pid: PY)
    monkeypatch.setattr(probe, "trusted", lambda path: path in (PY, FWD, CTL) and path not in untrusted)
    answers = {"/json/version": (200, version or LIVE_VERSION), "/json/list": (list_status, [] if list_status == 200
                                                                                 else None)}
    monkeypatch.setattr(probe, "_get", lambda port, path: answers[path])


def _verdict(port=52656):
    try:
        return probe.verify(port)
    except probe.Refused as e:
        return {"ok": False, "why": str(e)}


def test_genuine_live_forwarder_passes(monkeypatch):
    _genuine(monkeypatch, parent=100)
    assert _verdict() == {"ok": True, "identity": "id-github-kyzcreig", "class": "read", "pid": 200}


@pytest.mark.parametrize("klass", ["buy", "human", "operator", ""])
def test_buy_and_human_leases_are_refused(monkeypatch, klass):
    for spelling in (["--class", klass], ["--class=" + klass]):
        _genuine(monkeypatch, parent=100, ctl_args=[PY, CTL, "with", "id-github-kyzcreig", *spelling, "--", "x"])
        out = _verdict()
        assert out["ok"] is False and "is not leasable from a pool host" in out["why"], (spelling, out)
    _genuine(monkeypatch, parent=100, ctl_args=[PY, CTL, "with", "id-github-kyzcreig", "--class", "act"])
    assert _verdict()["class"] == "act"
    _genuine(monkeypatch, parent=100, ctl_args=[PY, CTL, "with", "id-github-kyzcreig", "--", "x", "--class", "buy"])
    assert _verdict()["class"] == "read", "the child's argv is not browserctl's --class"


def test_expired_or_released_lease_is_refused(monkeypatch):
    # The forwarder process is still up but the gateway no longer takes its token: /json/list answers 500.
    _genuine(monkeypatch, parent=100, list_status=500)
    out = _verdict()
    assert out["ok"] is False and "not live at the gateway" in out["why"]
    # `browserctl with` died without releasing (SIGKILL): the orphan forwarder's parent is pid 1.
    _genuine(monkeypatch, parent=1)
    out = _verdict()
    assert out["ok"] is False and "is not the installed browserctl.py" in out["why"]
    # Released normally: `with` ended the forwarder, nothing listens.
    monkeypatch.setattr(probe, "listener", lambda port: (_ for _ in ()).throw(probe.Refused("0 listeners on port 1")))
    assert _verdict()["ok"] is False


@pytest.mark.parametrize("case", ["script", "interpreter", "ctl", "uid", "identity", "option"])
def test_marker_alone_is_not_enough(monkeypatch, case):
    kw = {"parent": 100}
    if case == "script":
        kw["untrusted"] = (FWD,)
    elif case == "interpreter":
        kw["untrusted"] = (PY,)
    elif case == "ctl":
        kw["untrusted"] = (CTL,)
    elif case == "uid":
        kw["uid"] = os.getuid() + 1
    elif case == "identity":
        kw["version"] = {"Browser": "lease/id-amazon-alex"}
    _genuine(monkeypatch, **kw)
    if case == "option":
        monkeypatch.setattr(probe, "args", lambda pid: [PY, "-c", "import x", FWD] if pid == 200 else
                            [PY, CTL, "with", "id-github-kyzcreig"])
    assert _verdict()["ok"] is False


def test_trusted_needs_root_owned_and_unwritable_chain(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write everything, so nothing is trusted (fails closed)")
    mine = tmp_path / FWD.rsplit("/", 1)[1]
    mine.write_text("")
    assert probe.trusted(str(mine)) is False
    assert probe.trusted("relative/lease-cdp-forward.py") is False
    assert probe.trusted("/bin/sh") is True


def test_probe_never_reads_any_environment():
    with open(probe.__file__, encoding="utf-8") as f:
        source = f.read()
    assert "environ" not in source and "TOKEN" not in source


FORGE = """
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"Browser": "lease/id-github-kyzcreig"} if self.path.startswith("/json/version") else []).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a):
        pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(s.server_address[1], flush=True)
s.serve_forever()
"""


@pytest.mark.skipif(not any(os.path.exists(p) for p in probe.LSOF), reason="no lsof on this host")
def test_forged_marker_from_a_real_local_process_is_refused(tmp_path):
    # A real process on this host's loopback answers exactly what the forwarder answers, and the
    # probe, run as the worker would run it (isolated interpreter, fresh process), refuses it.
    script = tmp_path / "lease-cdp-forward.py"   # even the forwarder's own file name
    script.write_text(FORGE)
    p = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        port = int(p.stdout.readline())
        with open(probe.__file__, encoding="utf-8") as f:
            out = subprocess.run([sys.executable, "-I", "-c", f.read(), str(port)], capture_output=True, text=True,
                                 timeout=60)
        verdict = __import__("json").loads(out.stdout.strip().splitlines()[-1])
        assert verdict["ok"] is False, verdict
        assert "is not the installed lease-cdp-forward.py" in verdict["why"] or os.geteuid() == 0, verdict
        assert out.returncode == 1
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(not any(os.path.exists(p) for p in probe.LSOF), reason="no lsof on this host")
def test_port_with_no_forwarder_is_refused():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _verdict(port)["ok"] is False
