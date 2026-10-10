"""mem0 maintenance window (PRD-studio-cutover v4 I2b/c, RC1-3): conclude freeze + journal,
replay CLI, drain pause, status --check. No live store: fake urlopen / injected add_fn."""

import json
import time
import urllib.request
from urllib.parse import urlparse

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider
from plugins.memory.mem0 import window
from plugins.memory.mem0.capture_drain import CaptureDrainWorker
from plugins.memory.mem0.capture_queue import CaptureQueue, idem_key
from plugins.memory.mem0.capture_scrub import filter_facts


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._p).encode()


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_AGENT_ID", "daedalus")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    window._expired_logged.clear()
    return tmp_path


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=0, context=None):
        p = urlparse(request.full_url)
        if request.get_method() == "POST" and p.path == "/memories":
            calls.append(json.loads(request.data.decode()))
            return _Resp({"results": [{"id": "m1", "memory": "x"}]})
        raise AssertionError(f"unexpected HTTP call {request.get_method()} {p.path}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _provider():
    p = Mem0MemoryProvider()
    p.initialize("test-session")
    return p


def _journal_rows():
    jp = window.journal_path()
    return [json.loads(ln) for ln in jp.read_text().splitlines()] if jp.exists() else []


def _write_raw_flag(path, expires_at):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"started_at": "2026-10-09T00:00:00Z",
                                "expires_at": expires_at, "reason": "t"}))


# ---- 1. conclude freeze ---------------------------------------------------------------------
def test_flag_lives_in_host_state_dir(home):
    assert window.window_flag_path() == home / "state" / "mem0-window.flag"
    assert window.journal_path() == home / "state" / "mem0-window-journal.jsonl"
    assert window.pause_flag_path() == home / "state" / "mem0-capture-drain.pause"


def test_profile_home_shares_the_host_flag(home, monkeypatch):
    # the window is host-wide: a profile (aegis) sees the root's flag, not a per-profile one
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "aegis"))
    assert window.window_flag_path() == home / "state" / "mem0-window.flag"


def test_conclude_refused_and_journaled_under_flag(home, posts):
    data = window.write_flag(window.window_flag_path(), minutes=30, reason="cutover")
    out = json.loads(_provider().handle_tool_call("mem0_conclude", {"conclusion": "Ace likes tea"}))
    assert out == {"error": f"mem0 maintenance window, re-issue after {data['expires_at']}"}
    assert posts == []  # nothing reached the store
    rows = _journal_rows()
    assert len(rows) == 1
    assert rows[0]["text"] == "Ace likes tea"
    assert rows[0]["user_id"] == "ace" and rows[0]["agent_id"] == "daedalus"
    assert rows[0]["ts"]


def test_conclude_ignores_expired_flag(home, posts, caplog):
    _write_raw_flag(window.window_flag_path(), "2020-01-01T00:00:00Z")
    p = _provider()
    with caplog.at_level("WARNING"):
        a = json.loads(p.handle_tool_call("mem0_conclude", {"conclusion": "one"}))
        b = json.loads(p.handle_tool_call("mem0_conclude", {"conclusion": "two"}))
    assert a == b == {"result": "Fact stored."}
    assert len(posts) == 2
    assert not window.journal_path().exists()
    assert sum("EXPIRED" in r.getMessage() for r in caplog.records) == 1  # logged once


def test_conclude_without_flag_unchanged(home, posts):
    out = json.loads(_provider().handle_tool_call("mem0_conclude", {"conclusion": "x"}))
    assert out == {"result": "Fact stored."}
    assert len(posts) == 1


# ---- 3. replay ------------------------------------------------------------------------------
def test_replay_bypasses_flag_and_drains_atomically(home, posts):
    window.write_flag(window.window_flag_path(), minutes=30, reason="cutover")
    p = _provider()
    for t in ("fact A", "fact B"):
        p.handle_tool_call("mem0_conclude", {"conclusion": t})
    assert posts == []

    seen_during = []

    def add(entry):
        # the live journal was moved aside before the first POST (atomic drain)
        seen_during.append(window.journal_path().exists())
        return window.default_add_fn()(entry)

    rc = window.main(["replay"], add_fn=add)
    assert rc == 0
    assert seen_during == [False, False]
    assert [c["messages"][0]["content"] for c in posts] == ["fact A", "fact B"]
    assert all(c["infer"] is False and c["user_id"] == "ace" and c["agent_id"] == "daedalus"
               for c in posts)
    assert window.window_flag_path().exists()  # replay alone does not remove the flag
    assert not window.journal_path().exists()
    assert not (window.journal_path().parent / "mem0-window-journal.jsonl.replaying").exists()


def test_replay_partial_failure_exits_1_and_keeps_only_failed_rows(home, capsys):
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    for t in ("ok-1", "bad", "ok-2"):
        window.refuse_conclude("ace", "a", t)
    landed = []

    def add(entry):
        if entry["text"] == "bad":
            raise RuntimeError("HTTP 502")
        landed.append(entry["text"])

    assert window.main(["replay"], add_fn=add) == 1
    assert "journaled=3 replayed=2" in capsys.readouterr().out
    left = window.journal_path().with_name("mem0-window-journal.jsonl.replaying")
    assert [json.loads(ln)["text"] for ln in left.read_text().splitlines()] == ["bad"]
    # re-run drains the leftover without re-posting the rows that landed
    assert window.main(["replay"], add_fn=lambda e: landed.append(e["text"])) == 0
    assert landed == ["ok-1", "ok-2", "bad"]
    assert not left.exists()


def test_replay_drains_crash_leftover_then_new_journal(home, capsys):
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    window.refuse_conclude("ace", "a", "old")
    jp = window.journal_path()
    jp.rename(jp.with_name(jp.name + ".replaying"))  # a replay that crashed mid-way
    window.refuse_conclude("ace", "a", "new")
    landed = []
    assert window.main(["replay"], add_fn=lambda e: landed.append(e["text"])) == 0
    assert landed == ["old", "new"]
    assert "journaled=2 replayed=2" in capsys.readouterr().out


def test_close_sweep_catches_fact_journaled_during_replay(home, capsys):
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    window.write_flag(window.pause_flag_path(), minutes=30, reason="t")
    window.refuse_conclude("ace", "a", "before")
    landed = []

    def add(entry):
        landed.append(entry["text"])
        if entry["text"] == "before":
            # a conclude that read the flag just before close removed it journals mid-replay
            window._append_journal(window.journal_path(),
                                   {"user_id": "ace", "agent_id": "a", "text": "during", "ts": "t"})

    assert window.main(["close"], add_fn=add) == 0
    assert landed == ["before", "during"]
    assert "journaled=2 replayed=2" in capsys.readouterr().out
    assert not window.window_flag_path().exists()
    assert not window.pause_flag_path().exists()
    assert not window.journal_path().exists()


def _slow_append(monkeypatch, opened, go):
    """A journal writer that opens the journal fd, then stalls before writing: the window in
    which a rotation used to strand its row in an already-replayed inode (Prism 5df08c92362d)."""
    import os as _os

    def slow(path, entry):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o600)
        try:
            opened.set()
            go.wait(5)
            _os.write(fd, (json.dumps(entry) + "\n").encode())
        finally:
            _os.close(fd)

    monkeypatch.setattr(window, "_append_journal", slow)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("verb", ["close", "replay"])
def test_rotation_waits_for_in_flight_journal_append(home, monkeypatch, verb):
    import threading
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    opened, go = threading.Event(), threading.Event()
    _slow_append(monkeypatch, opened, go)
    landed = []
    writer = threading.Thread(target=window.refuse_conclude, args=("ace", "a", "in-flight"))
    writer.start()
    assert opened.wait(5)
    fn = window.close if verb == "close" else window.replay
    rot = threading.Thread(target=lambda: fn(lambda e: landed.append(e["text"])))
    rot.start()
    rot.join(0.5)  # base: the rotation finishes here, before the append; fixed: blocked on the lock
    go.set()
    writer.join(5)
    rot.join(5)
    if verb == "replay":
        window.replay(lambda e: landed.append(e["text"]))  # whatever the first pass left
    assert landed == ["in-flight"], "the in-flight fact must be replayed exactly once, never lost"
    left = [f.name for f in window.journal_path().parent.glob("mem0-window-journal.jsonl*")
            if not f.name.endswith(".lock")]
    assert left == []


def test_refusal_after_close_barrier_writes_through_not_to_journal(home, posts):
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    p = _provider()
    assert window.close(lambda e: None) == (0, 0)
    p.handle_tool_call("mem0_conclude", {"conclusion": "after"})
    assert [c["messages"][0]["content"] for c in posts] == ["after"]
    assert not window.journal_path().exists()


def test_close_with_failing_store_reports_true_count(home, capsys):
    window.write_flag(window.window_flag_path(), minutes=30, reason="t")
    window.refuse_conclude("ace", "a", "only")

    def add(entry):
        raise RuntimeError("connection refused")

    assert window.main(["close"], add_fn=add) == 1
    assert "journaled=1 replayed=0" in capsys.readouterr().out
    assert not window.window_flag_path().exists()
    # the stranded fact is visible to the healthcheck
    assert window.main(["status", "--check", "--queue", str(home / "none.db")]) == 2
    assert "journal_unreplayed" in capsys.readouterr().out


def test_open_writes_both_flags_with_expiry(home, capsys):
    assert window.main(["open", "--minutes", "45"]) == 0
    w = json.loads(window.window_flag_path().read_text())
    pz = json.loads(window.pause_flag_path().read_text())
    assert set(w) == {"started_at", "expires_at", "reason"}
    assert w["expires_at"] == pz["expires_at"]
    span = window._parse_ts(w["expires_at"]) - window._parse_ts(w["started_at"])
    assert span == pytest.approx(45 * 60, abs=1)


# ---- 2. drain pause -------------------------------------------------------------------------
class _Store:
    def __init__(self):
        self.rows, self.add_calls = [], 0

    def add(self, messages, kwargs):
        self.add_calls += 1
        self.rows.append({"id": f"m{self.add_calls}", "memory": messages[0]["content"],
                          "capture_idem": (kwargs.get("metadata") or {}).get("capture_idem", "")})
        return 1

    def recall_idem(self, key):
        return sum(1 for r in self.rows if r["capture_idem"] == key)

    def get_written(self, key):
        return [r for r in self.rows if r["capture_idem"] == key]

    def forget(self, mid):
        self.rows = [r for r in self.rows if r["id"] != mid]


def _worker(q, store):
    return CaptureDrainWorker(q, add_fn=store.add, recall_idem_fn=store.recall_idem,
                              scrub_fn=filter_facts, forget_fn=store.forget,
                              get_written_fn=store.get_written, gate="G", model="m",
                              write_filters={"user_id": "ace"}, backoff_base_s=1.0)


def test_drain_pauses_and_resumes(home, tmp_path):
    q = CaptureQueue(str(tmp_path / "q.db"))
    k = idem_key("s", 1, "u", "a")
    q.enqueue(k, {"user": "Ace prefers oolong tea", "assistant": "noted"})
    store = _Store()
    w = _worker(q, store)

    window.write_flag(window.pause_flag_path(), minutes=30, reason="t")
    assert w.drain_once() is False
    assert store.add_calls == 0
    assert q.counts()["pending"] == 1 and q.counts()["inflight"] == 0

    _write_raw_flag(window.pause_flag_path(), "2020-01-01T00:00:00Z")  # expired = ignored
    assert w.drain_once() is True
    assert store.add_calls == 1 and q.counts()["done"] == 1


def test_drain_resumes_when_pause_removed(home, tmp_path):
    q = CaptureQueue(str(tmp_path / "q.db"))
    q.enqueue(idem_key("s", 1, "u", "a"), {"user": "Ace's NAS is a DS1823", "assistant": "ok"})
    store = _Store()
    w = _worker(q, store)
    window.write_flag(window.pause_flag_path(), minutes=30, reason="t")
    assert w.drain_once() is False
    window.pause_flag_path().unlink()
    assert w.drain_once() is True
    assert q.counts()["done"] == 1


# ---- 4. status --check ----------------------------------------------------------------------
def test_check_exits_0_when_clean_or_active(home, tmp_path):
    qp = str(tmp_path / "q.db")
    CaptureQueue(qp)
    assert window.main(["status", "--check", "--queue", qp]) == 0
    window.main(["open", "--minutes", "30"])
    assert window.main(["status", "--check", "--queue", qp]) == 0


@pytest.mark.parametrize("flag", ["window", "pause"])
def test_check_exits_2_on_stale_flag(home, tmp_path, flag, capsys):
    path = window.window_flag_path() if flag == "window" else window.pause_flag_path()
    _write_raw_flag(path, "2020-01-01T00:00:00Z")
    assert window.main(["status", "--check", "--queue", str(tmp_path / "none.db")]) == 2
    assert f"{flag}_flag_expired" in capsys.readouterr().out
    # without --check, status is informational
    assert window.main(["status", "--queue", str(tmp_path / "none.db")]) == 0


def test_check_exits_2_on_old_pending_while_paused(home, tmp_path, capsys):
    qp = str(tmp_path / "q.db")
    q = CaptureQueue(qp)
    q.enqueue(idem_key("s", 1, "u", "a"), {"user": "u", "assistant": "a"},
              now=time.time() - 31 * 60)
    assert window.main(["status", "--check", "--queue", qp]) == 0  # not paused: not this check's job
    window.write_flag(window.pause_flag_path(), minutes=60, reason="t")
    assert window.main(["status", "--check", "--queue", qp]) == 2
    assert "pending_older_than_30m_while_paused" in capsys.readouterr().out
