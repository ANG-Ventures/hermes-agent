"""Trusted Browser Use route and selected-target binding (never model stdout)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from hermes_constants import hermes_home_key


@dataclass(frozen=True)
class BrowserUseRoute:
    env: dict
    cdp_url: str
    local: bool
    ready: bool = True

    def __post_init__(self):
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


_lock = threading.Lock()
_routes: dict[tuple[str, str], BrowserUseRoute] = {}
_pin: ContextVar[VaultTransaction | None] = ContextVar("browser_use_vault_target", default=None)


def route_for_task(task_id):
    with _lock:
        return _routes.get((hermes_home_key(), task_id or "default"))


def retire_route(task_id):
    """The task's native browser session became current: its Browser Use route no longer owns vault ops."""
    with _lock:
        _routes.pop((hermes_home_key(), task_id or "default"), None)


def browser_use_owns(task_id):
    """Whether Browser Use owns this task's vault ops. Only in CLI mode; outside it a recorded
    route is stale (the native browser stack is current) and is retired, never consulted."""
    from tools.browser_use_cli import is_browser_use_cli_mode
    if is_browser_use_cli_mode():
        return True
    retire_route(task_id)
    return False


def record_route(task_id, env, local=False, ready=True):
    from tools.browser_tool_cdp import _resolve_cdp_override
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    route = BrowserUseRoute(dict(env), _resolve_cdp_override(endpoint) if endpoint else "", local, ready)
    with _lock:
        _routes[(hermes_home_key(), task_id or "default")] = route
    return route


@contextmanager
def selection_execution(task_id, env, *, local=False):
    # The named daemon is shared across tasks. Use the dispatch authority itself
    # to exclude selection changes through ACK/cancellation, not a later IPC sample.
    from tools.browser_tool_cdp import _resolve_cdp_override
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    route = BrowserUseRoute(env, _resolve_cdp_override(endpoint) if endpoint else "", local, False)
    key = (hermes_home_key(), task_id or "default")
    with _lock:
        for peer_key, peer in list(_routes.items()):
            if (peer_key[0] == key[0] and peer.cdp_url == route.cdp_url
                    and peer.env.get("BU_NAME") == route.env.get("BU_NAME")):
                _routes[peer_key] = replace(peer, ready=False)
        _routes[key] = route
        def complete(ready):
            _routes[key] = replace(route, ready=ready)
        yield complete


# This separate, fixed program queries the daemon's private IPC. Agent code/stdout cannot
# forge the result. No credential or secret expression ever enters subprocess arguments.
_PROBE = """import json, os, sys
from pathlib import Path
from importlib.metadata import distributions
# Only explicitly bootstrapped installation roots, never cwd or PYTHONPATH.
for root in json.loads(sys.argv[1]):
    for dist in distributions(path=[root]):
        if dist.metadata.get('Name', '').replace('_', '-').lower() != 'browser-harness':
            continue
        files = {str(f): f for f in dist.files or []}
        entry = files.get('browser_harness/_ipc.py')
        if entry is None or entry.hash is None:
            continue
        import hashlib, base64
        path = Path(dist.locate_file(entry)).resolve()
        if path != Path(root).resolve() / 'browser_harness' / '_ipc.py' or entry.hash.mode != 'sha256':
            continue
        for item in files.values():
            if not str(item).startswith('browser_harness/') or item.hash is None:
                continue
            installed = Path(dist.locate_file(item)).resolve()
            if not installed.is_relative_to(Path(root).resolve()) or item.hash.mode != 'sha256':
                raise RuntimeError('browser-harness invalid installation record')
            digest = base64.urlsafe_b64encode(hashlib.sha256(installed.read_bytes()).digest()).decode().rstrip('=')
            if digest != item.hash.value:
                raise RuntimeError('browser-harness integrity check failed')
        sys.path.insert(0, str(Path(root).resolve()))
        break
    else:
        continue
    break
else:
    raise RuntimeError('Trusted browser-harness installation unavailable')
from browser_harness import _ipc
# The daemon request arrives on stdin so lease tokens never appear in argv.
request = json.loads(sys.stdin.read())
c, token = _ipc.connect(os.environ.get('BU_NAME', 'default'), timeout=5)
try:
    ping = _ipc.request(c, token, {'meta': 'ping'})
finally:
    c.close()
c, token = _ipc.connect(os.environ.get('BU_NAME', 'default'), timeout=5)
try:
    try:
        reply = _ipc.request(c, token, request)
    except Exception as exc:
        # The daemon answered ping but dropped this request (a pre-lease daemon fails the unknown
        # meta and closes the connection): report it as an unsupported request, not as a dead daemon.
        reply = {'error': 'request_dropped: ' + type(exc).__name__}
finally:
    c.close()
print(json.dumps({'pid': ping.get('pid'), 'reply': reply}))
"""

# A vault operation is bounded by its 300s transaction deadline; the daemon lease
# outlives it slightly so selection stays pinned through the final dispatch ACK.
_LEASE_TTL = 330


def _daemon_request(route, request):
    """Send one request to the route's Browser Use daemon -> (daemon pid, reply dict)."""
    import sys
    import sysconfig
    if not route.cdp_url or not route.ready:
        raise RuntimeError("Browser Use target unavailable; run browser_exec on a CDP-routed browser first")
    from pm.environments import committed_venv, site_packages

    environment = committed_venv(Path(__file__).resolve().parents[1])
    if environment is not None:
        # PM store Python keeps sys.prefix unchanged. Trust the install's committed
        # dependency record, never inherited PYTHONPATH or a cwd-discovered package.
        roots = {str(site_packages(environment))}
    else:
        roots = {sysconfig.get_path(key) for key in ("purelib", "platlib")}
        bundle = Path(__file__).resolve().parents[2] / "venv"
        roots.update(str(p) for p in (bundle / "Lib" / "site-packages",
            bundle / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages") if p.is_dir())
    env = {k: v for k, v in route.env.items() if not k.startswith("PYTHON")}
    proc = subprocess.run([sys.executable, "-I", "-c", _PROBE, json.dumps(sorted(roots))],
                          env=env, input=json.dumps(request),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        if type(data.get("pid")) is not int or not isinstance(data.get("reply"), dict):
            raise ValueError()
        return data["pid"], data["reply"]
    except (ValueError, AttributeError):
        raise RuntimeError("Browser Use daemon is unavailable; select a live tab with browser_exec and retry") from None


def acquire_lease(route):
    """Pin the daemon's selected tab for every daemon caller -> (pid, target, generation, token)."""
    pid, reply = _daemon_request(route, {"meta": "lease_acquire", "ttl": _LEASE_TTL, "owner_pid": os.getpid()})
    if str(reply.get("error", "")).startswith("selection_leased"):
        raise RuntimeError("Another caller holds this browser's tab selection; retry shortly")
    if "error" in reply or not reply.get("token") or not reply.get("targetId") or type(reply.get("generation")) is not int:
        # A daemon started before the lease API answers with an unrelated error: never proceed unpinned.
        raise RuntimeError("Browser Use daemon could not pin the selected tab (closed tab, or a daemon that "
                           "predates selection leases: restart it); select a live tab with browser_exec and retry")
    return pid, reply["targetId"], reply["generation"], reply["token"]


def check_lease(route, token):
    pid, reply = _daemon_request(route, {"meta": "lease_check", "token": token})
    if "error" in reply:
        raise RuntimeError("Browser Use selection lease lost or selection changed; retry")
    return pid, reply.get("targetId"), reply.get("generation")


def release_lease(route, token):
    try:
        _daemon_request(route, {"meta": "lease_release", "token": token})
    except Exception:
        pass  # The daemon also frees the lease on TTL expiry or this process's exit.


@dataclass(frozen=True)
class VaultTransaction:
    task_id: str
    profile: str
    route: BrowserUseRoute
    generation: int
    target: str
    lease_epoch: int | None
    deadline: float
    selection: int = -1
    selection_lease: str = ""

    def validate(self):
        from tools.browser_tool_cdp import _resolve_cdp_override
        if (time.monotonic() >= self.deadline
                or check_lease(self.route, self.selection_lease) != (self.generation, self.target, self.selection)
                or _resolve_cdp_override(self.route.env.get("BU_CDP_WS") or self.route.env.get("BU_CDP_URL")) != self.route.cdp_url
                or route_for_task(self.task_id) is not self.route):
            raise RuntimeError("Browser Use target/endpoint changed or deadline expired; retry")

    @contextmanager
    def dispatch(self):
        # Route replacement and dispatch authorization have a single linearization
        # point. Never block the supervisor loop waiting for another thread.
        if not _lock.acquire(blocking=False):
            raise RuntimeError("Browser Use route is being replaced; retry")
        try:
            if (_routes.get((self.profile, self.task_id)) is not self.route
                    or time.monotonic() >= self.deadline):
                raise RuntimeError("Browser Use transaction expired or route changed")
            if self.lease_epoch is not None:
                from tools.bot_desktop import lease
                current = lease.assert_agent_may_act(self.profile)
                if current.epoch != self.lease_epoch:
                    raise RuntimeError("Browser Use desktop lease changed; retry")
            yield
        finally:
            _lock.release()


def begin(task_id, *, expected_route):
    route = route_for_task(task_id)
    if route is not expected_route:
        raise RuntimeError("Browser Use route changed after takeover fence selection; retry")
    if route is None:
        raise RuntimeError("No Browser Use route for this task/profile; open the page with browser_exec first")
    deadline = time.monotonic() + 300
    profile = hermes_home_key()
    epoch = None
    if route.local:
        from tools.bot_desktop import lease
        epoch = lease.assert_agent_may_act(profile).epoch
    # The daemon itself refuses selection moves from every caller (other Hermes
    # processes, direct IPC) while this lease is held, through the dispatch ACK.
    generation, target, selection, lease = acquire_lease(route)
    if route_for_task(task_id) is not route:
        release_lease(route, lease)
        raise RuntimeError("Browser Use route changed during target selection; retry")
    return _pin.set(VaultTransaction(task_id, profile, route, generation, target, epoch, deadline,
                                     selection, lease))


def end(token):
    tx = _pin.get()
    _pin.reset(token)
    if tx is not None:
        release_lease(tx.route, tx.selection_lease)


def supervisor_for_task(task_id):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    tx = _pin.get()
    if tx is None or tx.task_id != task_id:
        raise RuntimeError("Browser Use vault target is not pinned; retry the vault operation")
    tx.validate()
    supervisor = SUPERVISOR_REGISTRY.get_or_start(
        task_id=f"bu-vault:{tx.profile}:{task_id}", cdp_url=tx.route.cdp_url)

    class TargetView:
        def evaluate_runtime(self, expression):
            try:
                tx.validate()
            except Exception:
                return {"ok": False, "outcome": "not_dispatched", "error": "Browser Use target/endpoint changed; select a live tab and retry"}
            return supervisor.evaluate_runtime(expression, target_id=tx.target,
                deadline=tx.deadline, dispatch_guard=tx.dispatch)
    return TargetView()
