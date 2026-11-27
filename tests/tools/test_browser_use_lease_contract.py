"""The installed browser-harness daemon speaks the selection-lease IPC that browser_use_target uses.

The real ``browser_harness.daemon.Daemon`` (pinned fleet fork commit) is driven in-process through
``acquire_lease``/``check_lease``/``release_lease``, so a repin to a build without the lease metas
goes red here. A stub of the pre-lease daemon keeps the fail-closed path covered.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

from browser_harness import daemon
from tools import browser_use_target as target


class _CDP:
    def __init__(self, pages):
        self.pages = pages

    async def send_raw(self, method, params=None, session_id=None):
        if method == "Target.getTargetInfo":
            if params["targetId"] not in self.pages:
                raise RuntimeError("No target with given id found")
            return {"targetInfo": {"targetId": params["targetId"], "type": "page", "url": self.pages[params["targetId"]]}}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-" + params["targetId"]}
        return {}


def _real_daemon(selected="T1"):
    d = daemon.Daemon()
    d.cdp = _CDP({"T1": "https://login.example/", "T2": "https://other.example/"})
    d.target_id, d.session = selected, "s-" + selected
    return d


class _PreLeaseDaemon:
    """browser-harness 0.1.13 has no lease metas: it answers them with an error, never a token."""

    async def handle(self, req):
        if req.get("meta") in ("lease_acquire", "lease_check", "lease_release"):
            return {"error": repr(KeyError("method"))}
        raise AssertionError(f"unexpected request {req!r}")


def _route_into(monkeypatch, d):
    def request(route, req):
        return 4242, asyncio.run(d.handle(req))
    monkeypatch.setattr(target, "_daemon_request", request)


ROUTE = SimpleNamespace(cdp_url="ws://127.0.0.1:9222/devtools/browser/x", ready=True, env={})


def test_installed_daemon_dispatches_lease_metas():
    """Each lease meta goes through the installed Daemon.handle dispatch and gets a lease reply."""
    d = _real_daemon()

    def send(req):
        return asyncio.run(d.handle(req))

    # The daemon expires a lease whose owner pid is dead, so the owner is this (live) process.
    acquired = send({"meta": "lease_acquire", "ttl": 30, "owner_pid": os.getpid()})
    assert set(acquired) >= {"token", "targetId", "generation"}, acquired
    assert acquired["targetId"] == "T1" and acquired["url"] == "https://login.example/"
    token = acquired["token"]

    checked = send({"meta": "lease_check", "token": token})
    assert (checked["targetId"], checked["generation"]) == ("T1", acquired["generation"]), checked
    assert send({"meta": "lease_check", "token": "not-the-token"}) == {"error": "lease_invalid"}

    assert send({"meta": "lease_release", "token": "not-the-token"}) == {"released": False}
    assert send({"meta": "lease_release", "token": token}) == {"released": True}
    assert send({"meta": "lease_check", "token": token}) == {"error": "lease_invalid"}


def test_lease_round_trip_against_installed_daemon(monkeypatch):
    d = _real_daemon()
    _route_into(monkeypatch, d)

    pid, tab, generation, token = target.acquire_lease(ROUTE)
    assert (pid, tab) == (4242, "T1") and type(generation) is int and token
    assert target.check_lease(ROUTE, token) == (4242, "T1", generation)

    # While leased, no daemon caller may move the selection.
    moved = asyncio.run(d.handle({"meta": "set_session", "session_id": "s-T2", "target_id": "T2"}))
    assert str(moved.get("error", "")).startswith("selection_leased")
    with pytest.raises(RuntimeError, match="Another caller holds"):
        target.acquire_lease(ROUTE)

    target.release_lease(ROUTE, token)
    assert d._lease is None
    with pytest.raises(RuntimeError, match="lease lost or selection changed"):
        target.check_lease(ROUTE, token)


def test_pre_lease_daemon_fails_closed(monkeypatch):
    _route_into(monkeypatch, _PreLeaseDaemon())
    with pytest.raises(RuntimeError, match="predates selection leases"):
        target.acquire_lease(ROUTE)


@pytest.mark.platforms("posix")
def test_pre_lease_daemon_dropping_the_request_reports_predates_leases(monkeypatch):
    """A daemon that answers ping but drops the lease request mid-connection (closed with the
    request unread, so the probe's send/recv raises) must read as "predates selection leases"
    through the real isolated probe, not as the generic "daemon is unavailable". (A released
    0.1.13 daemon instead replies {"error": "'method'"}, which already took that path.)"""
    import json
    import socket
    import tempfile
    import threading
    from importlib.metadata import distribution
    from pathlib import Path

    site = Path(distribution("browser-harness").locate_file(""))
    monkeypatch.setattr("sysconfig.get_path", lambda _: str(site))
    monkeypatch.setattr("pm.environments.committed_venv", lambda _: None)
    runtime = tempfile.TemporaryDirectory(prefix="bu-")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(Path(runtime.name) / "bu.sock"))
    server.listen()
    server.settimeout(5)
    seen = []

    def serve():
        for _ in range(2):
            conn, _ = server.accept()
            with conn:
                if not seen:
                    seen.append(json.loads(conn.recv(4096))["meta"])
                    conn.sendall(json.dumps({"pid": 123}).encode() + b"\n")
                else:
                    seen.append("dropped")  # close with the request unread: the client errors

    thread = threading.Thread(target=serve)
    thread.start()
    route = target.BrowserUseRoute({"BU_NAME": "scoped", "BH_RUNTIME_DIR": runtime.name,
                                    "BH_TMP_DIR": runtime.name}, "ws://synthetic", False)
    try:
        with pytest.raises(RuntimeError, match="predates selection leases"):
            target.acquire_lease(route)
        thread.join(5)
        assert seen == ["ping", "dropped"]
    finally:
        server.close()
        thread.join(5)
        runtime.cleanup()
