"""In-memory stand-in for the browser-harness daemon's selection IPC.

Mirrors the daemon contract (current_tab, lease_acquire/check/release, set_session) that
tools.browser_use_target reaches through ``_daemon_request``. Two modes:

* enforcing (default): the daemon owns the selection; while a lease is live, set_session to
  another target is refused for every caller, exactly like the real daemon.
* observing (``observe=callable(route) -> (pid, target)``): the selection is driven by the test
  and never refused, for tests that model a change the lease check must detect.
"""
import secrets


class FakeDaemon:
    def __init__(self, target="login", pid=123, observe=None):
        self.pid, self.target, self.generation = pid, target, 1
        self.observe = observe
        self.lease = None
        self.requests = []
        self.hooks = {}

    def _now(self, route):
        if self.observe is None:
            return self.pid, self.target
        return self.observe(route)

    def switch(self, target):
        """What another process's switch_tab() sends to the shared daemon."""
        if self.lease and target != self.lease["target"]:
            return {"error": "selection_leased: another caller holds this browser's tab selection; retry"}
        if target != self.target:
            self.target, self.generation = target, self.generation + 1
        return {"session_id": "s-" + target}

    def request(self, route, req):
        self.requests.append(dict(req))
        hook = self.hooks.get(req.get("meta"))
        if hook:
            hook()
        pid, target = self._now(route)
        meta = req.get("meta")
        if meta == "current_tab":
            return pid, ({"targetId": target} if target else {"error": "not_attached"})
        if meta == "set_session":
            return pid, self.switch(req["target_id"])
        if meta == "lease_acquire":
            if self.lease:
                return pid, {"error": "selection_leased: another caller holds this browser's tab selection; retry"}
            if not target:
                return pid, {"error": "cdp_disconnected"}
            self.lease = {"token": secrets.token_hex(8), "target": target, "generation": self.generation}
            return pid, {"token": self.lease["token"], "targetId": target, "generation": self.generation}
        if meta == "lease_check":
            if not self.lease or req.get("token") != self.lease["token"]:
                return pid, {"error": "lease_invalid"}
            if target != self.lease["target"] or self.generation != self.lease["generation"]:
                return pid, {"error": "selection_changed"}
            return pid, {"targetId": target, "generation": self.generation}
        if meta == "lease_release":
            released = bool(self.lease and req.get("token") == self.lease["token"])
            if released:
                self.lease = None
            return pid, {"released": released}
        return pid, {"error": "'method'"}


def install(monkeypatch, target_module, daemon):
    monkeypatch.setattr(target_module, "_daemon_request", daemon.request)
    return daemon
