"""Foreground ``mem0_conclude`` survives a store outage via the durable capture queue.

Before this, a refused/timed-out/5xx conclude returned an error and the fact was lost unless the
agent re-issued it (4 facts lost 2026-10-09). Now a connection-level failure enqueues a
``conclude`` row in the existing capture queue; the drain worker lands it with a direct
``infer=false`` POST (never extraction), deduped by ``dedup_hash``. A 4xx is a deterministic
rejection and still errors.

The provider tests run the REAL ``_DirectRestMem0Client`` (stdlib urllib) against a real local
HTTP server, so "refused" is a genuine ECONNREFUSED on a closed port.
"""

import http.server
import json
import socket
import sqlite3
import threading
import time

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider
from plugins.memory.mem0 import capture_pipeline as cp
from plugins.memory.mem0.capture_drain import CaptureDrainWorker
from plugins.memory.mem0.capture_queue import CaptureQueue, conclude_idem_key
from plugins.memory.mem0.capture_scrub import filter_facts


# ---------------------------------------------------------------- local mem0 stub
class _Store:
    """In-memory self-host mem0: POST /memories, POST /search (metadata filters)."""

    def __init__(self):
        self.rows = []
        self.posts = []
        self.fail_status = None   # int -> every POST /memories answers that status

    def handle(self, method, path, body):
        if method == "POST" and path == "/memories":
            self.posts.append(body)
            if self.fail_status:
                return self.fail_status, {"detail": "simulated"}
            text = body["messages"][0]["content"]
            row = {"id": f"m{len(self.rows) + 1}", "memory": text,
                   "metadata": body.get("metadata") or {}}
            self.rows.append(row)
            return 200, {"results": [{"id": row["id"], "memory": text, "event": "ADD"}]}
        if method == "POST" and path == "/search":
            flt = body.get("filters") or {}
            hits = [r for r in self.rows
                    if all(r["metadata"].get(k) == v for k, v in flt.items())]
            return 200, {"results": hits[: int(body.get("top_k") or 10)]}
        return 404, {"detail": "unexpected"}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """A real HTTP server that can be started late on a fixed port (refused until then)."""

    def __init__(self, store, port):
        self.store, self.port, self._httpd = store, port, None

    def start(self):
        store = self.store

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                code, payload = store.handle("POST", self.path, body)
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    port = _free_port()
    store = _Store()
    server = _Server(store, port)
    qpath = tmp_path / "state" / "capture_queue.db"
    # Never touch the operator's real queue under ~/.hermes.
    monkeypatch.setattr(cp, "_DEFAULT_QUEUE_PATH", str(qpath))
    # Drive the drain deterministically: no background thread, no first-drain delay.
    monkeypatch.setattr(cp, "_CONCLUDE_FIRST_DRAIN_DELAY_S", 0.0)
    monkeypatch.setattr(CaptureDrainWorker, "start", lambda self: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.setenv("MEM0_CAPTURE", "off")   # voice-style agent: conclude must still queue
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    yield provider, store, server, qpath
    server.stop()


def _rows(qpath):
    if not qpath.exists():
        return []
    conn = sqlite3.connect(str(qpath))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM capture_queue")]
    finally:
        conn.close()


def _conclude(provider, text):
    return json.loads(provider.handle_tool_call("mem0_conclude", {"conclusion": text}))


def _drain(provider):
    worker = provider._capture_pipeline._worker
    while worker.drain_once():
        pass


# ---------------------------------------------------------------- provider path
def test_refused_conclude_is_queued_then_lands_when_store_returns(env):
    provider, store, server, qpath = env
    out = _conclude(provider, "Ace's NAS is at 192.168.1.50.")
    assert out == {"result": "queued (store unreachable); will land when mem0 is back",
                   "queued": True}
    rows = _rows(qpath)
    assert len(rows) == 1 and rows[0]["kind"] == "conclude" and rows[0]["status"] == "pending"
    payload = json.loads(rows[0]["payload"])
    assert payload["text"] == "Ace's NAS is at 192.168.1.50."
    assert payload["user_id"] == "ace" and payload["agent_id"] == "daedalus"

    server.start()
    _drain(provider)

    assert _rows(qpath)[0]["status"] == "done"
    assert len(store.posts) == 1
    body = store.posts[0]
    assert body["infer"] is False                       # verbatim, never extraction
    assert "prompt" not in body and "model_chain" not in body
    assert body["messages"] == [{"role": "user", "content": "Ace's NAS is at 192.168.1.50."}]
    assert body["user_id"] == "ace" and body["agent_id"] == "daedalus"
    assert body["metadata"]["write_kind"] == "deliberate"
    assert body["metadata"]["dedup_hash"] == provider._bgr_norm_hash("Ace's NAS is at 192.168.1.50.")


def test_conclude_4xx_still_errors_and_is_never_queued(env):
    provider, store, server, qpath = env
    store.fail_status = 422
    server.start()
    out = _conclude(provider, "a fact the server rejects")
    assert "error" in out and "queued" not in out
    assert _rows(qpath) == []


def test_conclude_retry_while_down_does_not_double_write(env):
    provider, store, server, qpath = env
    assert _conclude(provider, "Ace prefers dark mode.")["queued"] is True
    assert _conclude(provider, "Ace prefers dark mode.")["queued"] is True   # agent re-issues
    assert len(_rows(qpath)) == 1
    server.start()
    _drain(provider)
    assert len(store.posts) == 1 and len(store.rows) == 1


def test_conclude_5xx_queues_and_drain_skips_when_the_write_had_landed(env):
    """A 5xx may have committed server-side. Queue it, and let the drain's dedup_hash check
    see the landed row instead of writing a second copy."""
    provider, store, server, qpath = env
    server.start()
    store.fail_status = 503
    assert _conclude(provider, "Ace's router is a UDM-SE.")["queued"] is True
    # The 503 hid a commit: the row is in the store with the same dedup_hash.
    meta = store.posts[0]["metadata"]
    store.rows.append({"id": "m-landed", "memory": "Ace's router is a UDM-SE.", "metadata": meta})
    store.fail_status = None
    _drain(provider)
    assert _rows(qpath)[0]["status"] == "done"
    assert len(store.posts) == 1                        # no second POST /memories
    assert len(store.rows) == 1


def test_conclude_queues_while_breaker_is_open(env):
    provider, store, server, qpath = env
    for _ in range(6):
        provider._record_failure()
    assert provider._is_breaker_open()
    assert _conclude(provider, "fact during cooldown")["queued"] is True
    assert _rows(qpath)[0]["kind"] == "conclude"


def test_conclude_success_path_unchanged_plus_dedup_hash(env):
    provider, store, server, qpath = env
    server.start()
    assert _conclude(provider, "stored directly") == {"result": "Fact stored."}
    assert _rows(qpath) == []
    assert store.posts[0]["metadata"]["dedup_hash"] == provider._bgr_norm_hash("stored directly")


# ---------------------------------------------------------------- worker / queue
def _worker(q, *, add, exists, certified=True, **kw):
    return CaptureDrainWorker(
        q, add_fn=lambda m, k: 1, recall_idem_fn=lambda key: 0, scrub_fn=filter_facts,
        conclude_add_fn=add, conclude_exists_fn=exists, turn_rows_allowed=certified, **kw)


def _enqueue_conclude(q, text="fact"):
    key = conclude_idem_key("ace", "daedalus", text)
    q.enqueue(key, {"text": text, "user_id": "ace", "agent_id": "daedalus", "metadata": {}},
              kind="conclude")
    return key


def test_conclude_row_never_dead_letters_on_connection_failure(tmp_path):
    q = CaptureQueue(str(tmp_path / "q.db"))
    key = _enqueue_conclude(q)

    def refused(payload):
        raise RuntimeError("Mem0 self-host REST POST /memories failed: [Errno 61] Connection refused")

    w = _worker(q, add=refused, exists=lambda p: False, backoff_base_s=0.0, max_attempts=2)
    for _ in range(6):                          # far past max_attempts
        w.drain_once()
    row = next(r for r in _rows(tmp_path / "q.db") if r["idem_key"] == key)
    assert row["status"] == "pending" and row["attempts"] >= 6


def test_conclude_row_4xx_in_drain_dead_letters(tmp_path):
    q = CaptureQueue(str(tmp_path / "q.db"))
    _enqueue_conclude(q)

    def reject(payload):
        raise RuntimeError("Mem0 self-host REST POST /memories failed: HTTP 422 Unprocessable")

    w = _worker(q, add=reject, exists=lambda p: False)
    w.drain_once()
    assert _rows(tmp_path / "q.db")[0]["status"] == "dead"


def test_uncertified_worker_drains_conclude_rows_but_not_turn_rows(tmp_path):
    q = CaptureQueue(str(tmp_path / "q.db"))
    q.enqueue("turn-key", {"user": "u", "assistant": "a"})
    _enqueue_conclude(q)
    landed = []
    w = _worker(q, add=lambda p: landed.append(p["text"]), exists=lambda p: False,
                certified=False)
    while w.drain_once():
        pass
    status = {r["kind"]: r["status"] for r in _rows(tmp_path / "q.db")}
    assert landed == ["fact"]
    assert status == {"conclude": "done", "turn": "pending"}


def test_existing_queue_db_gains_kind_column_with_turn_default(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE capture_queue (idem_key TEXT PRIMARY KEY, payload TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " next_attempt_at REAL NOT NULL DEFAULT 0, leased_until REAL, model_verdict TEXT,"
        " add_committed INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL);"
        "INSERT INTO capture_queue (idem_key,payload,created_at,updated_at) VALUES ('k','{}',0,0);")
    conn.close()
    CaptureQueue(str(path))
    assert _rows(path)[0]["kind"] == "turn"


# ---------------------------------------------------------------- shared queue, several mem0 hosts
# The queue file is per machine; profiles on one machine can point at different mem0 hosts.
def test_same_fact_from_two_hosts_queues_two_rows(env, monkeypatch):
    """Host collision: the conclude key must include the target host, or the second profile's
    fact is a silent no-op behind a "queued": true reply."""
    provider_a, _store, _server, qpath = env
    monkeypatch.setenv("MEM0_HOST", f"http://127.0.0.1:{_free_port()}")
    provider_b = Mem0MemoryProvider()
    provider_b.initialize("test-session-b")
    assert provider_a._host != provider_b._host
    assert _conclude(provider_a, "Ace's NAS is at 192.168.1.50.")["queued"] is True
    assert _conclude(provider_b, "Ace's NAS is at 192.168.1.50.")["queued"] is True
    hosts = sorted(json.loads(r["payload"])["host"] for r in _rows(qpath))
    assert hosts == sorted([provider_a._host, provider_b._host])


def _host_worker(q, host, landed, *, fail=False, **kw):
    def add(payload):
        if fail:
            raise RuntimeError("Mem0 self-host REST POST /memories failed: [Errno 61] Connection refused")
        assert payload["host"] == host
        landed.append(payload["text"])
    return CaptureDrainWorker(
        q, add_fn=lambda m, k: 1, recall_idem_fn=lambda key: 0, scrub_fn=filter_facts,
        conclude_add_fn=add, conclude_exists_fn=lambda p: False, turn_rows_allowed=False,
        target=host, **kw)


def _enqueue_for(q, host, text):
    q.enqueue(conclude_idem_key("ace", "daedalus", text, host),
              {"text": text, "user_id": "ace", "agent_id": "daedalus", "metadata": {}, "host": host},
              kind="conclude", target=host)


def test_worker_neither_leases_nor_counts_another_hosts_rows(tmp_path):
    """Drain starvation: a row for host B is not A's to lease, and must not keep A's loop alive."""
    q = CaptureQueue(str(tmp_path / "q.db"))
    _enqueue_for(q, "http://b", "fact for b")
    landed_a, landed_b = [], []
    worker_a = _host_worker(q, "http://a", landed_a)
    worker_b = _host_worker(q, "http://b", landed_b)
    assert worker_a._outstanding() == 0
    assert worker_a.drain_once() is False
    assert _rows(tmp_path / "q.db")[0]["attempts"] == 0     # never leased by A
    assert worker_b._outstanding() == 1
    assert worker_b.drain_once() is True
    assert landed_b == ["fact for b"] and landed_a == []


def test_stuck_host_owner_does_not_block_another_hosts_standby(tmp_path):
    """Replay starvation: A's mem0 is down so A's owner never retires; B's healthy rows must
    still land, so drain ownership cannot be shared across hosts."""
    q = CaptureQueue(str(tmp_path / "q.db"))
    _enqueue_for(q, "http://a", "fact for a")
    _enqueue_for(q, "http://b", "fact for b")
    landed_a, landed_b = [], []
    worker_a = _host_worker(q, "http://a", landed_a, fail=True, backoff_base_s=0.0,
                            poll_interval_s=0.01)
    worker_b = _host_worker(q, "http://b", landed_b, backoff_base_s=0.0, poll_interval_s=0.01)
    worker_a.start()
    try:
        worker_b.start()
        deadline = time.time() + 5
        while not landed_b and time.time() < deadline:
            time.sleep(0.02)
        assert landed_b == ["fact for b"]
        status = {json.loads(r["payload"])["host"]: r["status"] for r in _rows(tmp_path / "q.db")}
        assert status["http://b"] == "done" and status["http://a"] != "done"
    finally:
        worker_b.stop()
        worker_a.stop()


def test_existing_conclude_rows_gain_their_target_host_on_migration(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE capture_queue (idem_key TEXT PRIMARY KEY, payload TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " next_attempt_at REAL NOT NULL DEFAULT 0, leased_until REAL, model_verdict TEXT,"
        " add_committed INTEGER NOT NULL DEFAULT 0, last_error TEXT,"
        " kind TEXT NOT NULL DEFAULT 'turn', created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO capture_queue (idem_key,payload,kind,created_at,updated_at)"
        " VALUES ('c','{\"text\":\"f\",\"host\":\"http://b\"}','conclude',0,0),"
        "        ('t','{}','turn',0,0);")
    conn.close()
    q = CaptureQueue(str(path))
    assert {r["idem_key"]: r["target"] for r in _rows(path)} == {"c": "http://b", "t": ""}
    assert _host_worker(q, "http://a", [])._outstanding() == 0
    assert _host_worker(q, "http://b", [])._outstanding() == 1
