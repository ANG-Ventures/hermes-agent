"""Provider admission and explicit, idempotent repair of legacy quota crashes."""
import argparse
import io
import json
from pathlib import Path

from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    with kb.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_provider_capped_then_recovers(board, monkeypatch, lane):
    config = {"kanban": {"provider_health_probes": {"pool": "http://localhost/health"}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    tid = kb.create_task(board, title="probe", assignee="a", model_override="m", provider_override="pool")
    board.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    board.commit()
    probe = Mock(side_effect=[io.BytesIO(b'{"eligible_count":0,"reset_at":123}'),
                              io.BytesIO(b'{"eligible_count":1}')])
    monkeypatch.setattr("urllib.request.urlopen", probe)
    spawn = Mock(return_value=777777)
    first = kb.dispatch_once(board, spawn_fn=spawn)
    assert first.spawned == []
    spawn.assert_not_called()
    current = kb.get_task(board, tid)
    assert current.status == lane
    assert current.consecutive_failures == 0
    assert current.current_run_id is None
    event = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='deferred'", (tid,)).fetchone()
    assert json.loads(event[0]) == {"reason": "provider_capped", "provider": "pool", "reset_at": 123}
    second = kb.dispatch_once(board, spawn_fn=spawn)
    assert second.spawned[0][0] == tid
    assert probe.call_args.kwargs["timeout"] == 1


def test_no_configured_probe_preserves_spawn(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    probe = Mock(side_effect=AssertionError("must not access network"))
    monkeypatch.setattr("urllib.request.urlopen", probe)
    tid = kb.create_task(board, title="probe", assignee="a")
    assert kb.dispatch_once(board, spawn_fn=lambda *a: 777777).spawned[0][0] == tid
    probe.assert_not_called()


def quota_fixture(conn):
    tid = kb.create_task(conn, title="legacy quota breaker", assignee="a", max_retries=4)
    for i, stderr in enumerate(["HTTP 429 rate limit", "no eligible sub", "quota exceeded", "Traceback: real bug"]):
        assert kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            rid = kb._end_run(conn, tid, outcome="crashed", status="crashed", error="pid not alive")
            kb._append_event(conn, tid, "crashed", {"stderr_tail": stderr}, run_id=rid)
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL WHERE id=?", (tid,))
        kb._record_task_failure(conn, tid, "pid not alive", outcome="crashed")
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def test_quota_repair_dry_run_and_apply_are_idempotent(board):
    from hermes_cli.kanban_quota_repair import reclassify_quota_crashes
    tid = quota_fixture(board)
    before = list(board.iterdump())
    report = reclassify_quota_crashes(board, dry_run=True)
    assert report["reclassified"] == 3
    assert report["unblocked"] == [tid]
    assert list(board.iterdump()) == before
    applied = reclassify_quota_crashes(board, dry_run=False)
    assert applied["reclassified"] == 3
    assert kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, tid).consecutive_failures == 1
    outcomes = [r[0] for r in board.execute("SELECT outcome FROM task_runs ORDER BY id")]
    assert outcomes == ["rate_limited", "rate_limited", "rate_limited", "crashed"]
    assert reclassify_quota_crashes(board, dry_run=False)["reclassified"] == 0
    assert kb.get_task(board, tid).consecutive_failures == 1


def test_repair_never_unblocks_a_later_human_block(board):
    from hermes_cli.kanban_quota_repair import reclassify_quota_crashes
    tid = quota_fixture(board)
    assert kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid)
    assert kb.block_task(board, tid, reason="needs operator", kind="needs_input")
    reclassify_quota_crashes(board, dry_run=False)
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.get_task(board, tid).block_kind == "needs_input"


def test_repair_cli_dry_run_uses_readonly_connection(board, monkeypatch, capsys):
    tid = quota_fixture(board)
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "repair", "--reclassify-quota-crashes", "--dry-run", "--json"])
    monkeypatch.setattr(kb, "connect", Mock(side_effect=AssertionError("dry-run must not migrate")))
    assert kanban.kanban_command(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reclassified"] == 3
    assert result["unblocked"] == [tid]
    assert kb.get_task(board, tid).status == "blocked"


@pytest.mark.parametrize("model,provider,expected", [
    ("new-model", "explicit", "explicit"),
    ("inline/new-model", None, "inline"),
    ("new-model", None, "profile-pool"),
    (None, None, "profile-pool"),
])
def test_effective_provider_matches_worker_profile_and_overrides(board, tmp_path, model, provider, expected):
    from hermes_cli.kanban_provider_health import effective_provider
    home = tmp_path / ".hermes" / "profiles" / "a"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text('model:\n  provider: profile-pool\n  default: old-model\n', encoding="utf-8")
    tid = kb.create_task(board, title="provider", assignee="a", model_override=model, provider_override=provider)
    assert effective_provider(kb.get_task(board, tid)) == expected


def test_real_health_http_defers_then_admits(board, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    state = {"status": "all_capped"}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / ".hermes"
    (home / "config.yaml").write_text(json.dumps({"kanban": {"provider_health_probes": {
        "pool": f"http://127.0.0.1:{server.server_port}/health"}}}), encoding="utf-8")
    ids = [kb.create_task(board, title="http", assignee="a", model_override="pool/model") for _ in range(2)]
    try:
        assert kb.dispatch_once(board, spawn_fn=lambda *a: 777777).spawned == []
        assert requests == ["/health"]
        state.clear()
        state["eligible_count"] = 2
        result = kb.dispatch_once(board, spawn_fn=lambda *a: 777777)
        assert {r[0] for r in result.spawned} == set(ids)
        assert requests == ["/health", "/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("payload", [b"garbage", b"[]", b'{"eligible_count":false}', b'{"status":"unknown"}'])
def test_unknown_health_never_blocks_work(board, monkeypatch, payload):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {
        "provider_health_probes": {"pool": "http://localhost/health"}}})
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: io.BytesIO(payload))
    tid = kb.create_task(board, title="unknown", assignee="a", model_override="pool/m")
    assert kb.dispatch_once(board, spawn_fn=lambda *a: 777777).spawned[0][0] == tid


def test_probe_timeout_never_blocks_work(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {
        "provider_health_probes": {"pool": "http://localhost/health"}}})
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=TimeoutError))
    tid = kb.create_task(board, title="timeout", assignee="a", model_override="pool/m")
    assert kb.dispatch_once(board, spawn_fn=lambda *a: 777777).spawned[0][0] == tid
