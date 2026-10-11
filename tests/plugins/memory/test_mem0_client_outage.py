"""mem0 client behaviour against a dead / wedged / recovering store, from the QA sweep t_2a8d31b5.

Every test drives the REAL ``_DirectRestMem0Client`` (stdlib urllib) against a real socket: a
closed port is a genuine ECONNREFUSED, a "blackhole" is a listener that accepts and never answers.
The asserted effects are the ones a user or the next turn sees: how many real requests a dead store
costs, how long a turn blocks, what text the model is handed, and how many rows land in the store.
"""

import http.server
import json
import socket
import threading
import time

import pytest

import plugins.memory.mem0 as mem0_mod
from plugins.memory.mem0 import Mem0MemoryProvider, window
from plugins.memory.mem0 import config_schema


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Store:
    """Minimal self-host mem0: POST /memories, POST /search (metadata equality), GET /memories."""

    def __init__(self):
        self.rows, self.posts, self.requests = [], [], 0

    def handle(self, method, path, body):
        self.requests += 1
        if method == "POST" and path == "/memories":
            self.posts.append(body)
            self.rows.append({"id": f"m{len(self.rows)}", "memory": body["messages"][0]["content"],
                              "metadata": body.get("metadata") or {}})
            return 200, {"results": [{"id": self.rows[-1]["id"], "event": "ADD"}]}
        if method == "POST" and path == "/search":
            f = body.get("filters") or {}
            hits = [r for r in self.rows if all(r["metadata"].get(k) == v for k, v in f.items())]
            return 200, {"results": hits[: body.get("top_k") or 10]}
        return 200, {"results": list(self.rows)}


class _Server:
    def __init__(self, store, port):
        self.store, self.port, self._httpd = store, port, None

    def start(self):
        store = self.store

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                code, payload = store.handle(method, self.path.split("?")[0], body)
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                self._go("POST")

            def do_GET(self):
                self._go("GET")

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


class _Blackhole:
    """Accepts TCP connections and never answers (a wedged store)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self._held = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self._held.append(c)

    def close(self):
        for c in self._held:
            c.close()
        self.sock.close()


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.setenv("MEM0_CAPTURE", "off")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    from plugins.memory.mem0 import capture_pipeline as cp
    monkeypatch.setattr(cp, "_DEFAULT_QUEUE_PATH", str(tmp_path / "state" / "capture_queue.db"))
    window._expired_logged.clear()
    # rerank off: an outage must never drive the rerank incident pager from a test
    (tmp_path / "mem0.json").write_text(json.dumps({"rerank": "off"}))
    return tmp_path


def _provider(monkeypatch, host):
    monkeypatch.setenv("MEM0_HOST", host)
    p = Mem0MemoryProvider()
    p.initialize("s")
    return p


def _search(p, q="where is the NAS"):
    return json.loads(p.handle_tool_call("mem0_search", {"query": q}))


# ---- breaker ---------------------------------------------------------------------------------
def test_half_open_breaker_reopens_on_one_failed_probe(home, monkeypatch):
    """A still-dead store after the cooldown costs ONE real request, not THRESHOLD more."""
    port = _free_port()
    p = _provider(monkeypatch, f"http://127.0.0.1:{port}")
    attempts = []
    real = p._get_client().search

    def counting(*a, **k):
        attempts.append(1)
        return real(*a, **k)
    monkeypatch.setattr(p._get_client(), "search", counting)

    for _ in range(mem0_mod._BREAKER_THRESHOLD):
        _search(p)
    assert p._is_breaker_open()
    assert len(attempts) == mem0_mod._BREAKER_THRESHOLD

    p._breaker_open_until = time.monotonic() - 1          # cooldown elapsed: half-open
    assert not p._is_breaker_open()
    _search(p)                                              # the probe fails ...
    assert len(attempts) == mem0_mod._BREAKER_THRESHOLD + 1
    assert p._is_breaker_open()                             # ... and re-opens at once
    for _ in range(3):
        _search(p)
    assert len(attempts) == mem0_mod._BREAKER_THRESHOLD + 1  # no traffic while open


def test_half_open_success_closes_the_breaker(home, monkeypatch):
    port = _free_port()
    p = _provider(monkeypatch, f"http://127.0.0.1:{port}")
    for _ in range(mem0_mod._BREAKER_THRESHOLD):
        _search(p)
    assert p._is_breaker_open()
    store, server = _Store(), _Server(_Store(), port)
    server.store = store
    server.start()
    try:
        p._breaker_open_until = time.monotonic() - 1
        assert "results" in _search(p) or "result" in _search(p)
        assert p._consecutive_failures == 0
        _search(p)
        assert store.requests >= 2                          # closed: requests flow again
    finally:
        server.stop()


def test_ack_turn_without_a_request_does_not_close_the_breaker(home, monkeypatch):
    """Gate A skips the search on an acknowledgment turn; that is no evidence the store is up."""
    p = _provider(monkeypatch, f"http://127.0.0.1:{_free_port()}")
    for _ in range(mem0_mod._BREAKER_THRESHOLD):
        _search(p)
    p._breaker_open_until = time.monotonic() - 1            # half-open
    p.queue_prefetch("ok thanks")
    p.prefetch("ok thanks")
    assert p._consecutive_failures >= mem0_mod._BREAKER_THRESHOLD


# ---- what the model is told when the store is down -------------------------------------------
def test_down_store_tool_text_says_down_not_empty(home, monkeypatch):
    p = _provider(monkeypatch, f"http://127.0.0.1:{_free_port()}")
    first = _search(p)
    assert "DOWN" in first["error"] and "NOT an empty result" in first["error"]
    assert "No relevant memories" not in json.dumps(first)
    prof = json.loads(p.handle_tool_call("mem0_profile", {}))
    assert "DOWN" in prof["error"] and "No memories stored" not in json.dumps(prof)
    for _ in range(mem0_mod._BREAKER_THRESHOLD):
        _search(p)
    assert p._is_breaker_open()
    opened = _search(p)
    assert "DOWN" in opened["error"] and "NOT an empty result" in opened["error"]


def test_a_4xx_is_not_reported_as_an_outage(home, monkeypatch):
    port = _free_port()

    class _Rejecting(_Store):
        def handle(self, method, path, body):
            return 422, {"detail": "bad request"}
    server = _Server(_Rejecting(), port)
    server.start()
    try:
        p = _provider(monkeypatch, f"http://127.0.0.1:{port}")
        out = _search(p)
        assert "422" in out["error"] and "DOWN" not in out["error"]
    finally:
        server.stop()


# ---- turn budget against a wedged store -------------------------------------------------------
def test_wedged_store_trips_the_breaker_and_turns_stop_paying_the_join_budget(home, monkeypatch):
    """Accept-and-never-answer: before the fix every turn blocked the full join budget forever
    (the in-flight worker never reached its 30 s urlopen timeout before the next turn rotated it,
    so no failure was ever recorded). Now each join timeout counts, and once the breaker opens a
    turn no longer blocks at all."""
    hole = _Blackhole()
    try:
        (home / "mem0.json").write_text(json.dumps({"rerank": "off", "prefetch_join_timeout_s": 0.3}))
        p = _provider(monkeypatch, f"http://127.0.0.1:{hole.port}")
        blocked = []
        for i in range(mem0_mod._BREAKER_THRESHOLD + 2):
            t = time.monotonic()
            p.queue_prefetch(f"what is the backup schedule for the nas {i}")
            out = p.prefetch("q")
            blocked.append(time.monotonic() - t)
            assert out == ""
        assert p._is_breaker_open()
        assert max(blocked) < 0.3 + 1.0                     # never past the budget
        assert blocked[-1] < 0.05 and blocked[-2] < 0.05   # breaker open: no join at all
    finally:
        hole.close()


# ---- window replay dedup ------------------------------------------------------------------------
def test_window_replay_stamps_dedup_hash_and_skips_a_fact_already_live(home, monkeypatch):
    port = _free_port()
    store, server = _Store(), None
    server = _Server(store, port)
    server.start()
    try:
        monkeypatch.setenv("MEM0_HOST", f"http://127.0.0.1:{port}")
        add = window.default_add_fn()
        entry = {"text": "Ace's NAS is at 10.0.0.5.", "user_id": "ace", "agent_id": "daedalus"}
        add(entry)
        add(entry)                                          # a replay re-run / the other host's journal
        assert len(store.posts) == 1
        meta = store.posts[0]["metadata"]
        assert meta["dedup_hash"] == Mem0MemoryProvider._bgr_norm_hash(entry["text"])
        # and the conclude path's dedup now sees the replayed row
        p = _provider(monkeypatch, f"http://127.0.0.1:{port}")
        assert p._queued_conclude_landed({"text": entry["text"], "host": p._host,
                                          "metadata": {"dedup_hash": meta["dedup_hash"]}})
    finally:
        server.stop()


# ---- mem0.json schema ------------------------------------------------------------------------
def test_schema_flags_the_drift_that_breaks_a_client():
    bad = {
        "host": "mem0.ace",                                 # no scheme
        "pin_user_id": True,                                # no user_id anywhere
        "rerank_deadline_ms": "8647",                       # string where a number belongs
        "capture": "always",                                # not a capture mode
        "prefetch_rerank_gate": {"enabeld": True},          # typo'd sub-key
        "destructive_tools_enabld": True,                   # typo'd key
    }
    found = {(lvl, key) for lvl, key, _ in config_schema.validate(bad)}
    assert ("error", "host") in found
    assert ("error", "user_id") in found
    assert ("error", "rerank_deadline_ms") in found
    assert ("error", "capture") in found
    assert ("error", "admin_api_key") in found
    assert ("warn", "prefetch_rerank_gate.enabeld") in found
    assert ("warn", "destructive_tools_enabld") in found


def test_schema_accepts_env_supplied_identity_and_never_prints_values(tmp_path, capsys):
    good = {"host": "https://mem0.ace", "admin_api_key": "SECRET-VALUE-123", "ca_bundle": "/x.crt",
            "pin_user_id": True}
    assert config_schema.validate(good, env_keys={"MEM0_USER_ID"}) == []
    (tmp_path / "profiles" / "p1").mkdir(parents=True)
    (tmp_path / "mem0.json").write_text(json.dumps(good))
    (tmp_path / ".env").write_text("MEM0_USER_ID=ace\n")
    (tmp_path / "profiles" / "p1" / "mem0.json").write_text(json.dumps({**good, "pin_user_id": "yes"}))
    assert config_schema.main(["--home", str(tmp_path)]) == 1   # p1 has no MEM0_USER_ID
    out = capsys.readouterr().out
    assert "SECRET-VALUE-123" not in out and "profiles/p1/mem0.json user_id" in out


def test_schema_covers_every_key_the_shipped_example_config_uses():
    """Every key a live fleet mem0.json carries today must be known to the schema (a contract
    between the schema and the config the plugin actually reads)."""
    example = {"destructive_tools_enabled": True, "host": "https://mem0.ace", "admin_api_key": "k",
               "ca_bundle": "/c", "pin_user_id": True, "user_id": "ace", "capture": "auto",
               "retrieval_kill": {"rerank": False}, "graph": {"ssh_host": "local", "container": "c"},
               "prefetch_relevance_floor": {"enabled": True, "min_content_tokens": 1, "min_cosine": 0.1},
               "prefetch_rerank_gate": {"enabled": True, "min_rerank": 0.0, "min_rerank_specific": -0.75,
                                        "specific_min_content_tokens": 3},
               "prefetch_rerank_gap": {"enabled": False, "max_gap": 6.0}, "mem0_gbrain": {"enabled": True},
               "mem0_capture_router": {"enabled": True, "staging_mode": False, "model": "m",
                                       "fallback_model": "f", "primary_secret_ref": "op://a",
                                       "fallback_secret_ref": "op://b"},
               "rerank": "builtin", "rerank_deadline_ms": 8647.17}
    assert config_schema.validate(example) == []
