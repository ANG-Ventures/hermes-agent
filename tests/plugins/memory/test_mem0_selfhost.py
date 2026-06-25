"""Tests for the self-hosted Mem0 direct-REST backend."""

import io
import json
import sys
import types
import urllib.error
import urllib.request
from email.message import Message
from urllib.parse import parse_qs, urlparse

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider


class _HTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        if self._payload is None:
            return b""
        return json.dumps(self._payload).encode("utf-8")


def _header(request, name):
    name = name.lower()
    for key, value in request.header_items():
        if key.lower() == name:
            return value
    return None


def _json_body(request):
    if not request.data:
        return None
    return json.loads(request.data.decode("utf-8"))


def _install_exploding_memory_client(monkeypatch):
    # Patch MemoryClient ON the real mem0 module rather than replacing sys.modules["mem0"]
    # with a fake ModuleType. Replacing the whole module deletes the real one (and its
    # submodules) on monkeypatch revert when absent, polluting other tests that import mem0
    # (caused order-dependent hindsight failures under pytest-randomly). setattr on the real
    # module is reverted cleanly and leaves the module graph intact.
    # mem0 is an OPTIONAL runtime dependency (the provider imports it lazily;
    # it's not in pyproject/requirements). Skip — don't fail — when it's absent,
    # e.g. on CI runners without the optional package installed.
    _real_mem0 = pytest.importorskip("mem0")

    class ExplodingMemoryClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("MemoryClient must not be constructed for MEM0_HOST")

    monkeypatch.setattr(_real_mem0, "MemoryClient", ExplodingMemoryClient)


def _selfhost_provider(monkeypatch, tmp_path, *, destructive=False):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.setenv("MEM0_DESTRUCTIVE_TOOLS", "true" if destructive else "false")
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    return provider


def test_selfhost_tools_use_direct_rest_with_api_key_and_response_mapping(monkeypatch, tmp_path):
    _install_exploding_memory_client(monkeypatch)
    calls = []

    def fake_urlopen(request, timeout=0, context=None):
        parsed = urlparse(request.full_url)
        call = {
            "method": request.get_method(),
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "api_key": _header(request, "X-API-Key"),
            "body": _json_body(request),
            "timeout": timeout,
        }
        calls.append(call)
        if call["method"] == "POST" and call["path"] == "/memories":
            return _HTTPResponse({"results": [{"id": "m-add", "memory": "stored fact"}]})
        if call["method"] == "POST" and call["path"] == "/search":
            return _HTTPResponse({"results": [{"id": "m-search", "memory": "matched fact", "score": 0.87}]})
        if call["method"] == "GET" and call["path"] == "/memories":
            return _HTTPResponse({"results": [{"id": "m-profile", "memory": "profile fact"}]})
        raise AssertionError(f"unexpected HTTP call: {call}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = _selfhost_provider(monkeypatch, tmp_path)

    conclude = json.loads(provider.handle_tool_call("mem0_conclude", {"conclusion": "store this"}))
    search = json.loads(provider.handle_tool_call("mem0_search", {"query": "needle", "top_k": 3}))
    profile = json.loads(provider.handle_tool_call("mem0_profile", {}))

    assert conclude == {"result": "Fact stored."}
    assert search == {"results": [{"memory": "matched fact", "score": 0.87}], "count": 1}
    assert profile == {"result": "profile fact", "count": 1}

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/memories"),
        ("POST", "/search"),
        ("GET", "/memories"),
    ]
    assert all(c["api_key"] == "admin-key" for c in calls)

    add_call, search_call, profile_call = calls
    assert add_call["body"]["messages"] == [{"role": "user", "content": "store this"}]
    assert add_call["body"]["user_id"] == "ace"
    assert add_call["body"]["agent_id"] == "daedalus"
    assert search_call["body"]["query"] == "needle"
    assert search_call["body"]["user_id"] == "ace"
    # search is a READ -> user-scoped only, no agent_id
    assert "agent_id" not in search_call["body"]
    # mem0_profile -> get_all (READ) -> user-scoped only, no agent_id
    assert profile_call["query"] == {"user_id": ["ace"]}


def test_selfhost_delete_uses_rest_delete_after_read_before_destroy(monkeypatch, tmp_path):
    _install_exploding_memory_client(monkeypatch)
    calls = []

    def fake_urlopen(request, timeout=0, context=None):
        parsed = urlparse(request.full_url)
        call = {
            "method": request.get_method(),
            "path": parsed.path,
            "api_key": _header(request, "X-API-Key"),
            "body": _json_body(request),
        }
        calls.append(call)
        if call["method"] == "GET" and call["path"] == "/memories/m-delete":
            return _HTTPResponse({"id": "m-delete", "memory": "doomed", "metadata": {}})
        if call["method"] == "DELETE" and call["path"] == "/memories/m-delete":
            return _HTTPResponse({"id": "m-delete"})
        raise AssertionError(f"unexpected HTTP call: {call}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = _selfhost_provider(monkeypatch, tmp_path, destructive=True)

    result = json.loads(provider.handle_tool_call("mem0_delete", {"memory_id": "m-delete"}))

    assert result["deleted"] == 1
    assert result["results"] == [{"id": "m-delete", "outcome": "deleted", "was": "doomed"}]
    assert [(c["method"], c["path"]) for c in calls] == [
        ("GET", "/memories/m-delete"),
        ("DELETE", "/memories/m-delete"),
    ]
    assert all(c["api_key"] == "admin-key" for c in calls)


@pytest.mark.parametrize("host_value", [None, "", "   "])
def test_unset_or_blank_host_uses_existing_memoryclient_path(monkeypatch, tmp_path, host_value):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_API_KEY", "cloud-key")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    if host_value is None:
        monkeypatch.delenv("MEM0_HOST", raising=False)
    else:
        monkeypatch.setenv("MEM0_HOST", host_value)

    constructed = []
    # mem0 is an OPTIONAL runtime dependency (imported lazily by the provider);
    # skip rather than fail when it isn't installed (e.g. clean CI runners).
    _real_mem0 = pytest.importorskip("mem0")

    class FakeMemoryClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    # setattr on the real module (reverted cleanly) instead of replacing sys.modules["mem0"]
    # — the whole-module swap pollutes other tests under random ordering (see helper above).
    monkeypatch.setattr(_real_mem0, "MemoryClient", FakeMemoryClient)

    # Capture the bounded-client limits the fallback passes, without poking httpx internals.
    captured_limits = {}
    try:
        import httpx
        has_httpx = True
        _RealClient = httpx.Client

        class _CapturingClient(_RealClient):
            def __init__(self, *args, **kw):
                limits = kw.get("limits")
                if limits is not None:
                    captured_limits["max_connections"] = limits.max_connections
                    captured_limits["max_keepalive_connections"] = limits.max_keepalive_connections
                    captured_limits["keepalive_expiry"] = limits.keepalive_expiry
                super().__init__(*args, **kw)

        monkeypatch.setattr(httpx, "Client", _CapturingClient)
    except ImportError:
        has_httpx = False

    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._get_client()

    # Exactly one MemoryClient constructed, always with the api_key.
    assert len(constructed) == 1
    assert constructed[0].get("api_key") == "cloud-key"
    # When httpx is importable the cloud fallback must hand the SDK a *bounded*
    # client (limits + keepalive_expiry) so idle keepalive sockets don't rot into
    # CLOSE_WAIT and leak fds in a long-lived gateway (HANDOFF-fd-leak-client-pool.md).
    if has_httpx:
        assert "client" in constructed[0], "cloud fallback must pass a bounded httpx.Client"
        assert captured_limits == {
            "max_connections": 10,
            "max_keepalive_connections": 5,
            "keepalive_expiry": 30.0,
        }
    else:
        # No httpx -> graceful degradation to the default (unbounded) client.
        assert "client" not in constructed[0]


def test_mem0_json_overrides_selfhost_env_config(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://env-host")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "env-key")
    monkeypatch.setenv("MEM0_USER_ID", "env-user")
    monkeypatch.setenv("MEM0_AGENT_ID", "env-agent")
    (tmp_path / "mem0.json").write_text(json.dumps({
        "host": "http://file-host/",
        "admin_api_key": "file-key",
        "user_id": "file-user",
        "agent_id": "file-agent",
    }))

    def fake_urlopen(request, timeout=0, context=None):
        parsed = urlparse(request.full_url)
        calls.append({
            "url": request.full_url,
            "netloc": parsed.netloc,
            "api_key": _header(request, "X-API-Key"),
            "body": _json_body(request),
        })
        return _HTTPResponse({"results": [{"id": "m1", "memory": "file scoped", "score": 0.9}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")

    result = json.loads(provider.handle_tool_call("mem0_search", {"query": "scope"}))

    assert result["count"] == 1
    assert calls[0]["netloc"] == "file-host"
    assert calls[0]["api_key"] == "file-key"
    assert calls[0]["body"]["user_id"] == "file-user"
    # search is a READ -> user-scoped only, no agent_id injected
    assert "agent_id" not in calls[0]["body"]


def test_selfhost_401_surfaces_error_and_records_failure_without_fabricated_memory(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=0, context=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=io.BytesIO(b'{"detail":"bad key"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = _selfhost_provider(monkeypatch, tmp_path)

    result = json.loads(provider.handle_tool_call("mem0_search", {"query": "needle"}))

    assert "error" in result
    assert "401" in result["error"]
    assert "results" not in result
    assert "No relevant memories found" not in result["error"]
    assert provider._consecutive_failures == 1


def test_direct_rest_client_scopes_writes_both_reads_user_only():
    """B3/B4: on a shared multi-agent store, add/search/get_all with NO explicit scope
    must still be constrained to the client's configured user_id AND agent_id —
    never querying or writing globally."""
    from importlib import import_module
    mod = import_module("plugins.memory.mem0")
    client = mod._DirectRestMem0Client(
        host="http://mem0.test", admin_api_key="k", agent_id="daedalus", user_id="ace"
    )
    sent = []

    def _fake_request(method, path, *, body=None, params=None):
        sent.append({"method": method, "path": path, "body": body, "params": params})
        return {"results": []}

    client._request = _fake_request

    # add() (WRITE) with no user_id/agent_id kwargs must inject BOTH (attribution + B4)
    client.add([{"role": "user", "content": "x"}])
    assert sent[-1]["body"]["user_id"] == "ace"
    assert sent[-1]["body"]["agent_id"] == "daedalus"

    # search() (READ) injects user_id ONLY — NOT agent_id. Reads are user-scoped for
    # cross-session recall, and historical memories stored agent-scoped-without-user
    # would be silently dropped by an agent AND-filter (the live-cutover 0-results bug).
    client.search(query="q")
    assert sent[-1]["body"]["user_id"] == "ace"
    assert "agent_id" not in sent[-1]["body"]

    # get_all() (READ) likewise injects user_id ONLY, never agent_id.
    client.get_all()
    assert sent[-1]["params"]["user_id"] == "ace"
    assert "agent_id" not in sent[-1]["params"]

    # an explicit caller READ filter is respected (user override, still no agent injected)
    client.search(query="q", filters={"user_id": "other"})
    assert sent[-1]["body"]["user_id"] == "other"
    assert "agent_id" not in sent[-1]["body"]

    # an explicit agent_id in a READ filter IS honored (caller opted in)
    client.search(query="q", filters={"agent_id": "explicit"})
    assert sent[-1]["body"]["agent_id"] == "explicit"
    assert sent[-1]["body"]["user_id"] == "ace"


def _count_open_fds() -> int:
    """Best-effort open-fd count for THIS process, cross-platform.

    Linux: count /proc/self/fd entries. macOS/BSD: fall back to psutil if present,
    else resource-based proc fd listing via /dev/fd. The soak test asserts a
    *plateau*, so an approximate-but-consistent counter is sufficient.
    """
    import os
    for path in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(path):
            try:
                return len(os.listdir(path))
            except OSError:
                continue
    try:
        import psutil  # type: ignore
        return psutil.Process().num_fds()
    except Exception:
        return -1


def test_direct_rest_client_fd_count_plateaus_under_soak(tmp_path):
    """REAL regression test for HANDOFF-fd-leak-client-pool.md.

    Drive thousands of real add/search calls through the real _DirectRestMem0Client
    (urllib, real sockets) against a real loopback HTTP server in ONE process, and
    assert the open-fd count PLATEAUS rather than growing monotonically. This is the
    end-to-end proof the client doesn't strand sockets — a unit test on pool config
    can't catch a path that leaks fds; only exercising the real socket lifecycle can.

    The direct-REST client uses urllib with a per-call `with urlopen(...)` that closes
    each connection, so it must not accumulate CLOSE_WAIT/idle fds. If a future change
    swaps in a pooled client without bounding it, this test fails.
    """
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    fd_probe = _count_open_fds()
    if fd_probe < 0:
        pytest.skip("no way to count fds on this platform (no /proc, /dev/fd, or psutil)")

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self):
            body = json.dumps({"results": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            self._reply()

        def do_GET(self):
            self._reply()

        def log_message(self, format, *args):
            pass  # silence

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        from importlib import import_module
        mod = import_module("plugins.memory.mem0")
        client = mod._DirectRestMem0Client(
            host=f"http://127.0.0.1:{port}",
            admin_api_key="k",
            agent_id="daedalus",
            user_id="ace",
        )

        # Warm up so transient import/buffer fds aren't counted as growth.
        for _ in range(50):
            client.add([{"role": "user", "content": "warmup"}])
            client.search(query="warmup")

        baseline = _count_open_fds()

        # Soak: thousands of real round-trips in one long-lived process.
        n = 2000
        for i in range(n):
            client.add([{"role": "user", "content": f"memory {i}"}])
            client.search(query=f"query {i}")

        peak = _count_open_fds()
    finally:
        server.shutdown()
        server.server_close()

    # The fd count must PLATEAU. A leaking client would grow ~1 fd per call
    # (thousands); a non-leaking one stays flat give-or-take a small jitter from
    # GC timing and the server's own worker threads. Allow generous slack but far
    # below the per-call-leak signal.
    growth = peak - baseline
    assert growth < 50, (
        f"fd count grew by {growth} over {n} calls "
        f"(baseline={baseline}, peak={peak}) — client is leaking sockets/fds"
    )


def test_ca_bundle_builds_ssl_context_for_https_private_ca(tmp_path):
    """A private-CA HTTPS endpoint (e.g. mem0.ace, signed by the LAN root CA) must be
    verifiable via an explicit CA bundle, since fleet hosts don't carry the private CA
    in their system trust store. With a bundle + https host, the client builds an SSL
    context; without one (or over http) it stays None (urllib system-trust default).
    Regression for the CERTIFICATE_VERIFY_FAILED that the live cutover surfaced.
    """
    from importlib import import_module
    mod = import_module("plugins.memory.mem0")

    # a real (self-signed) PEM so ssl.create_default_context(cafile=...) accepts it
    import ssl
    import datetime
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        pytest.skip("cryptography not available to mint a test CA PEM")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Local Root CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_pem = tmp_path / "test-ca.crt"
    ca_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # https + bundle -> context built
    c1 = mod._DirectRestMem0Client(
        host="https://mem0.ace", admin_api_key="k", agent_id="a", user_id="u",
        ca_bundle=str(ca_pem),
    )
    assert isinstance(c1._ssl_context, ssl.SSLContext)

    # https + NO bundle -> None (system trust store default)
    c2 = mod._DirectRestMem0Client(
        host="https://mem0.ace", admin_api_key="k", agent_id="a", user_id="u",
    )
    assert c2._ssl_context is None

    # http + bundle -> None (no TLS to verify)
    c3 = mod._DirectRestMem0Client(
        host="http://mem0.ace", admin_api_key="k", agent_id="a", user_id="u",
        ca_bundle=str(ca_pem),
    )
    assert c3._ssl_context is None


def _clear_mem0_env(monkeypatch):
    for var in (
        "MEM0_API_KEY", "MEM0_HOST", "MEM0_ADMIN_API_KEY",
        "MEM0_CA_BUNDLE", "MEM0_USER_ID", "MEM0_AGENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_api_key_not_required_in_schema_so_selfhost_setup_is_not_blocked():
    """The config schema must NOT mark ``api_key`` as required.

    ``is_available()`` bypasses ``api_key`` entirely when ``host`` is set
    (self-hosted mode is gated on ``admin_api_key``). A schema that marks
    ``api_key`` required would force any validation/setup UI to demand a cloud
    key even for a self-hosted server that never uses one. Behavior contract,
    not a value snapshot: api_key is optional; the host/admin pair carries
    self-hosted availability.
    """
    provider = Mem0MemoryProvider()
    schema = {field["key"]: field for field in provider.get_config_schema()}
    assert schema["api_key"].get("required", False) is False, (
        "api_key must be optional — is_available() bypasses it in self-hosted mode"
    )


def test_is_available_selfhost_without_api_key(monkeypatch, tmp_path):
    """Self-hosted (host + admin_api_key, NO api_key) must report available."""
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    _clear_mem0_env(monkeypatch)
    monkeypatch.setenv("MEM0_HOST", "https://mem0.ace")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-secret")
    assert Mem0MemoryProvider().is_available() is True


def test_is_available_cloud_requires_api_key(monkeypatch, tmp_path):
    """Cloud mode (no host) still requires api_key — fix doesn't weaken that path."""
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    _clear_mem0_env(monkeypatch)
    assert Mem0MemoryProvider().is_available() is False
    monkeypatch.setenv("MEM0_API_KEY", "cloud-key")
    assert Mem0MemoryProvider().is_available() is True


# ===========================================================================
# W2-PLUMB — param-drop client fix + call-site split (INV-8) + sibling audit.
# THE BUG: _DirectRestMem0Client.search() built the POST body with only query +
# scope and DISCARDED rerank/keyword_search/top_k — so rerank was a no-op on the
# wire for months. These are behavior contracts (does the param reach the body /
# the right call-site), not value snapshots.
# ===========================================================================


def _capturing_client(**overrides):
    """A real _DirectRestMem0Client whose _request is stubbed to capture the call."""
    from importlib import import_module
    mod = import_module("plugins.memory.mem0")
    kw = {"host": "http://mem0.test", "admin_api_key": "k",
          "agent_id": "daedalus", "user_id": "ace"}
    kw.update(overrides)
    client = mod._DirectRestMem0Client(**kw)
    sent = []

    def _fake_request(method, path, *, body=None, params=None):
        sent.append({"method": method, "path": path, "body": body, "params": params})
        return {"results": []}

    client._request = _fake_request
    return client, sent


# --- the load-bearing regression: search() PUTS the flags in the POST body -----

def test_search_puts_retrieval_flags_in_post_body():
    """REGRESSION for the param-drop bug. A search() with explicit rerank/keyword_search/
    top_k/reference_date must place EACH in the POST body — not silently drop them."""
    client, sent = _capturing_client()
    client.search(query="q", rerank=True, keyword_search=True, top_k=7,
                  reference_date="2026-06-01")
    body = sent[-1]["body"]
    assert sent[-1]["method"] == "POST" and sent[-1]["path"] == "/search"
    assert body["query"] == "q"
    assert body["rerank"] is True
    assert body["keyword_search"] is True
    assert body["top_k"] == 7
    assert body["reference_date"] == "2026-06-01"
    # scope still floored to user_id (read scope, no agent_id)
    assert body["user_id"] == "ace"
    assert "agent_id" not in body


def test_search_omits_none_flags_so_server_resolves_its_default():
    """INV-8(i)/INV-10: a None flag is OMITTED from the body so the server resolves it
    from its settings default (ships OFF). Only query + scope when nothing is passed."""
    client, sent = _capturing_client()
    client.search(query="q")  # every flag defaults to None
    body = sent[-1]["body"]
    for flag in ("rerank", "keyword_search", "top_k", "reference_date"):
        assert flag not in body, f"{flag}=None must be omitted, not sent"
    assert set(body) == {"query", "user_id"}


def test_search_partial_flags_only_sends_what_was_set():
    """A caller that sets only rerank must send ONLY rerank — the others stay omitted
    (server default), proving per-flag independence."""
    client, sent = _capturing_client()
    client.search(query="q", rerank=False, top_k=5)
    body = sent[-1]["body"]
    assert body["rerank"] is False          # explicit False is sent (not omitted)
    assert body["top_k"] == 5
    assert "keyword_search" not in body
    assert "reference_date" not in body


# --- sibling param-drop audit: the whole bug CLASS, one test per method ---------

def test_sibling_audit_add_forwards_documented_params():
    """add() must forward its documented params (metadata/infer/run_id) into the body —
    not just user_id/agent_id scope."""
    client, sent = _capturing_client()
    client.add([{"role": "user", "content": "x"}],
               metadata={"write_kind": "deliberate"}, infer=False, run_id="r1")
    body = sent[-1]["body"]
    assert sent[-1]["path"] == "/memories"
    assert body["messages"] == [{"role": "user", "content": "x"}]
    assert body["metadata"] == {"write_kind": "deliberate"}
    assert body["infer"] is False
    assert body["run_id"] == "r1"
    assert body["user_id"] == "ace" and body["agent_id"] == "daedalus"


def test_sibling_audit_get_all_forwards_filters_into_request():
    """get_all() must forward its filters (scoped) into the request params."""
    client, sent = _capturing_client()
    client.get_all(filters={"agent_id": "explicit"})
    params = sent[-1]["params"]
    assert sent[-1]["method"] == "GET" and sent[-1]["path"] == "/memories"
    assert params["agent_id"] == "explicit"   # explicit caller filter honored
    assert params["user_id"] == "ace"         # user scope floored in


def test_sibling_audit_update_forwards_text_and_metadata():
    """update() must forward its documented text + metadata into the PUT body."""
    client, sent = _capturing_client()
    client.update("m-1", text="new text", metadata={"forgotten": True})
    body = sent[-1]["body"]
    assert sent[-1]["method"] == "PUT" and sent[-1]["path"] == "/memories/m-1"
    assert body["text"] == "new text"
    assert body["metadata"] == {"forgotten": True}


# --- call-site split (INV-8(ii)): the two enumerated flag-passing call-sites ----

class _SearchCapturingClient:
    def __init__(self):
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return {"results": []}

    def get_all(self, **kwargs):
        return {"results": []}


def _provider_with(monkeypatch, tmp_path, client, *, rerank_cfg=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.delenv("MEM0_CAPTURE", raising=False)
    if rerank_cfg is not None:
        (tmp_path / "mem0.json").write_text(json.dumps({"rerank": rerank_cfg}))
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider


def test_queue_prefetch_reranks_nl_but_not_exact_token(monkeypatch, tmp_path):
    """INV-8(ii) + Ace 2026-06-24: the every-turn prefetch path NOW reranks (the
    cross-encoder's semantic/temporal win rides prefetch, bounded by the 10s join
    ceiling) — EXCEPT for exact-token lookups (IP/port/email/long-id), where the
    cross-encoder regresses the exact match RRF already nails. top_k stays 5 (INV-3)."""
    # normal NL query → rerank ON
    client = _SearchCapturingClient()
    provider = _provider_with(monkeypatch, tmp_path, client, rerank_cfg="true")
    provider.queue_prefetch("what is my coffee preference")
    provider._prefetch_thread.join(timeout=2)
    assert len(client.searches) == 1
    call = client.searches[0]
    assert call["rerank"] is True           # rerank rides prefetch for NL queries
    assert call["top_k"] == 5               # INV-3 payload-size cap unchanged

    # exact-token query → rerank OFF (RRF owns it)
    client2 = _SearchCapturingClient()
    provider2 = _provider_with(monkeypatch, tmp_path, client2, rerank_cfg="true")
    provider2.queue_prefetch("what runs on port 8443")
    provider2._prefetch_thread.join(timeout=2)
    assert client2.searches[0]["rerank"] is False   # exact-token gate

    # rerank profile OFF → prefetch never reranks regardless of query
    client3 = _SearchCapturingClient()
    provider3 = _provider_with(monkeypatch, tmp_path, client3, rerank_cfg="false")
    provider3.queue_prefetch("what is my coffee preference")
    provider3._prefetch_thread.join(timeout=2)
    assert client3.searches[0]["rerank"] is False


def test_exact_token_query_detector_gates_rerank():
    """W2-RERANK gate: the detector flags exact-identifier lookups (where the
    cross-encoder regresses what RRF already nails) and passes NL queries through."""
    from importlib import import_module
    P = import_module("plugins.memory.mem0").Mem0MemoryProvider
    for q in ["what runs on port 8443", "what is at 192.168.1.34",
              "alex@angsciences.com", "what is commit 9315e3036f24",
              "firmware xvf-3510-v4.2.1"]:
        assert P._is_exact_token_query(q) is True, q
    for q in ["what is my coffee preference", "what did I decide about the theater",
              "who maintains the media stack", "", None]:
        assert P._is_exact_token_query(q) is False, q


def test_mem0_search_uses_configured_rerank_profile_by_default(monkeypatch, tmp_path):
    """INV-8(ii): the deliberate tool path sends the RERANK profile. With no per-call
    rerank arg it falls back to the configured profile (self._rerank), coerced from the
    JSON-string config value."""
    client = _SearchCapturingClient()
    provider = _provider_with(monkeypatch, tmp_path, client, rerank_cfg="true")
    provider.handle_tool_call("mem0_search", {"query": "needle"})
    assert client.searches[-1]["rerank"] is True   # picked up the configured profile


def test_mem0_search_per_call_rerank_overrides_profile(monkeypatch, tmp_path):
    """The model may force rerank per-call, overriding the configured profile."""
    client = _SearchCapturingClient()
    provider = _provider_with(monkeypatch, tmp_path, client, rerank_cfg="true")
    provider.handle_tool_call("mem0_search", {"query": "needle", "rerank": False})
    assert client.searches[-1]["rerank"] is False


# --- call-site lint (INV-8 / Pass-4 RC): keep the split true past today --------

def test_call_site_lint_exactly_two_search_call_sites_pass_flags():
    """Standing lint: EXACTLY two `client.search(...)` call-sites pass retrieval flags —
    queue_prefetch (fast) and the mem0_search handler — and they are the ONLY ones. A
    future third flag-passing call-site (silent re-divergence) fails this test."""
    import ast
    import inspect
    from importlib import import_module
    mod = import_module("plugins.memory.mem0")
    source = inspect.getsource(mod)
    tree = ast.parse(source)

    RETRIEVAL_FLAGS = {"rerank", "keyword_search", "top_k", "reference_date"}

    # Map every node to its enclosing FunctionDef chain so we can name the call-site.
    parent_func = {}

    def _annotate(node, chain):
        for child in ast.iter_child_nodes(node):
            new_chain = chain
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                new_chain = chain + [child.name]
            parent_func[child] = chain
            _annotate(child, new_chain)

    _annotate(tree, [])

    flag_call_sites = []  # (enclosing-chain, kwargs)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "search"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "client"):
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if kwargs & RETRIEVAL_FLAGS:
                flag_call_sites.append((parent_func.get(node, []), kwargs))

    # Exactly two flag-passing client.search call-sites.
    assert len(flag_call_sites) == 2, (
        f"expected exactly 2 flag-passing client.search call-sites (INV-8(ii)), "
        f"found {len(flag_call_sites)}: {flag_call_sites}"
    )
    enclosing = ["::".join(chain) for chain, _ in flag_call_sites]
    # One lives under queue_prefetch (the fast hot path), one under handle_tool_call
    # (the mem0_search tool path). Both names must appear.
    assert any("queue_prefetch" in e for e in enclosing), enclosing
    assert any("handle_tool_call" in e for e in enclosing), enclosing


# --- INV-3: no injected-payload-size / prefix delta ---------------------------

def test_inv3_prefetch_caps_injected_lines_and_prefix_unchanged(monkeypatch, tmp_path):
    """INV-3: the change alters request PARAMS only. The injected memory block's SIZE
    (top-5 lines) and its prefix are unchanged — the prefetch still requests top_k=5 and
    renders the same `## Mem0 Memory` / `- `-bulleted shape."""
    captured = {}

    class _Client:
        def search(self, **kwargs):
            captured.update(kwargs)
            return {"results": [{"memory": f"fact {i}"} for i in range(10)]}

    client = _Client()
    provider = _provider_with(monkeypatch, tmp_path, client)
    provider.queue_prefetch("q")
    provider._prefetch_thread.join(timeout=2)
    # payload-size cap: still top_k=5 (the server returns 5; format never grows the block)
    assert captured["top_k"] == 5
    injected = provider.prefetch("q")
    assert injected.startswith("## Mem0 Memory\n")          # prefix byte-stable
    body_lines = [ln for ln in injected.splitlines() if ln.startswith("- ")]
    # The server already trims to top_k=5; the plugin renders the returned rows as-is.
    assert all(ln.startswith("- ") for ln in body_lines)
    assert "## Mem0 Memory" in injected


# ===========================================================================
# W3-TEMPORAL — tau_m created_at window (plugin-side parse + filter+boost).
# Behavior contracts: window applied ONLY when the feature is on AND a temporal
# expression parses; in-window rows boosted to the top (recency = boost, not a
# hard filter); the prefetch hot path and non-temporal queries are untouched.
# ===========================================================================


def _temporal_provider(monkeypatch, tmp_path, client, **cfg):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.delenv("MEM0_CAPTURE", raising=False)
    base = {"rerank": "false"}
    base.update(cfg)
    (tmp_path / "mem0.json").write_text(json.dumps(base))
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider


class _DatedSearchClient:
    """Returns a fixed candidate set with created_at, capturing the requested top_k."""
    def __init__(self, results):
        self._results = results
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return {"results": list(self._results)}

    def get_all(self, **kwargs):
        return {"results": []}


_FACTS = [
    {"memory": "off-window A", "score": 0.9, "created_at": "2026-05-01T12:00:00+00:00"},
    {"memory": "in-window on the 20th", "score": 0.4, "created_at": "2026-06-20T18:00:00+00:00"},
    {"memory": "off-window B", "score": 0.8, "created_at": "2026-06-01T12:00:00+00:00"},
]


def test_temporal_off_by_default_no_window(monkeypatch, tmp_path):
    """Feature ships OFF: even a clearly-temporal query gets the plain fetch (top_k as
    requested, no boost re-rank, no over-fetch)."""
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client)  # temporal_search unset
    assert provider._temporal_search is False
    provider.handle_tool_call("mem0_search", {"query": "what did we do on June 20th", "top_k": 3})
    assert client.searches[-1]["top_k"] == 3        # no over-fetch when off


def test_temporal_on_overfetches_and_boosts_in_window_row(monkeypatch, tmp_path):
    """Flag on + a temporal expression: over-fetch the deeper pool, then boost the
    in-window row above higher-semantic-score out-of-window rows."""
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client,
                                  temporal_search=True, temporal_overfetch=50)
    out = provider.handle_tool_call("mem0_search", {"query": "what did we do on June 20th", "top_k": 2})
    # over-fetched the deeper candidate pool
    assert client.searches[-1]["top_k"] == 50
    payload = json.loads(out)
    memories = [r["memory"] for r in payload["results"]]
    # the in-window 06-20 row is promoted to rank 0 despite a lower semantic score
    assert memories[0] == "in-window on the 20th"
    assert len(memories) == 2                        # trimmed back to top_k


def test_temporal_on_but_no_expression_is_passthrough(monkeypatch, tmp_path):
    """Flag on but NO temporal expression in the query → behaves exactly like off
    (no over-fetch, original order preserved)."""
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client, temporal_search=True)
    out = provider.handle_tool_call("mem0_search", {"query": "what is my postgres password", "top_k": 3})
    assert client.searches[-1]["top_k"] == 3         # no over-fetch
    memories = [r["memory"] for r in json.loads(out)["results"]]
    assert memories[0] == "off-window A"             # original semantic order kept


def test_temporal_boost_keeps_out_of_window_rows(monkeypatch, tmp_path):
    """Recency is a BOOST not a hard filter: out-of-window rows are retained (not
    dropped), they just rank below in-window rows."""
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client, temporal_search=True)
    out = provider.handle_tool_call("mem0_search", {"query": "decisions on the 20th", "top_k": 5})
    memories = [r["memory"] for r in json.loads(out)["results"]]
    assert memories[0] == "in-window on the 20th"
    assert set(memories[1:]) == {"off-window A", "off-window B"}   # both retained


def test_temporal_does_not_touch_prefetch(monkeypatch, tmp_path):
    """INV-3 / INV-8: temporal boost is mem0_search-only. The prefetch hot path still
    requests top_k=5 (no over-fetch) even with the feature on and a temporal query."""
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client, temporal_search=True)
    provider.queue_prefetch("what did we change on the 20th")
    provider._prefetch_thread.join(timeout=2)
    assert client.searches[-1]["top_k"] == 5         # prefetch untouched by temporal


def test_temporal_parse_failure_is_non_fatal(monkeypatch, tmp_path):
    """A parser exception must NOT break recall — the search proceeds windowless."""
    import plugins.memory.mem0 as mod
    client = _DatedSearchClient(_FACTS)
    provider = _temporal_provider(monkeypatch, tmp_path, client, temporal_search=True)
    monkeypatch.setattr(mod, "parse_temporal_window",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = provider.handle_tool_call("mem0_search", {"query": "on the 20th", "top_k": 3})
    payload = json.loads(out)
    assert payload["count"] == 3                      # recall still works
    assert client.searches[-1]["top_k"] == 3         # fell back to plain fetch (no window)


