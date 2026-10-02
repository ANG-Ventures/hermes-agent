"""C7 durable-state backfill, hermes-agent slice A (t_60634825).

One regression per confirmed finding; each was RED on fork/main 7a81d467.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_budget as kbud
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

HOME = "20260922_000000_home"
OTHER = "20260922_111111_other"
NEW = "20260922_222222_new"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
                "HERMES_PROFILE", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# --- k103: takeover event records the DISPLACED home --------------------


def test_k103_session_takeover_records_displaced_home(board):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker-a",
                             session_id=HOME)
        with kb.mutation_actor(session_ids=(OTHER,), profile="apollo",
                               foreign_ok="home chat is dead"):
            assert kb.set_task_session(conn, tid, NEW)
        assert kb.get_task(conn, tid).session_id in (NEW, OTHER)
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "takeover"]
        assert len(ev) == 1
        assert ev[0].payload["home"] == HOME
        if "prev_session_id" in ev[0].payload:
            assert ev[0].payload["prev_session_id"] == HOME


# --- k105: a failed circuit notify must not latch the episode ------------


def test_k105_failed_circuit_notify_is_retried(board, monkeypatch):
    sent: list = []
    results = iter([False, True])

    def _notify(argv):
        sent.append(argv)
        return next(results)

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", _notify)
    kb._notify_rate_limit_circuit(None, "claude-apr", 1_000, 5, now=500)
    kb._notify_rate_limit_circuit(None, "claude-apr", 1_000, 5, now=560)
    kb._notify_rate_limit_circuit(None, "claude-apr", 1_000, 5, now=620)
    assert len(sent) == 2  # retried once after the failure, then latched


# --- k107: refused coverage on a held review run is not persisted ------


def _held_review_run(conn):
    tid = kb.create_task(conn, title="t", assignee="builder")
    worker = kb.claim_task(conn, tid)
    assert worker is not None
    assert kb.request_review(conn, tid, reviewer="argus",
                             expected_run_id=worker.current_run_id)
    assert kb.claim_review_task(conn, tid) is not None
    return tid


def test_k107_rejected_coverage_is_rolled_back(board):
    bad = json.dumps({"lenses": {n: "done" for n in REQUIRED_REVIEW_LENSES},
                      "findings": 0})
    with kb.connect_closing() as conn:
        tid = _held_review_run(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        ok, detail = kb.request_changes(conn, tid, reason="fix",
                                        expected_run_id=run_id, coverage=bad)
        assert ok is False and detail
        bodies = [c.body for c in kb.list_comments(conn, tid)]
        assert not any("review_coverage:" in b for b in bodies)
        assert kb.get_task(conn, tid).status == "running"


# --- k109: a start token from another boot is not identity -------------


def _spawned_self(conn, boot):
    tid = kb.create_task(conn, title="t", assignee="worker-a")
    assert kb.claim_task(conn, tid) is not None
    pid = os.getpid()
    token = kb._pid_start_token(pid)
    if token is None:
        pytest.skip("start token unreadable on this platform")
    run_id = kb.get_task(conn, tid).current_run_id
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "spawned",
                         {"pid": pid, "start_token": token, "boot_id": boot},
                         run_id=run_id)
        # The recorded worker was claimed/spawned an hour ago (before the
        # reboot); the process now at that pid started after it.
        conn.execute("UPDATE task_events SET created_at = created_at - 3600 "
                     "WHERE task_id = ? AND kind IN ('claimed', 'spawned')", (tid,))
        conn.execute("UPDATE task_runs SET started_at = started_at - 3600 "
                     "WHERE task_id = ?", (tid,))
        conn.execute("UPDATE tasks SET started_at = started_at - 3600 "
                     "WHERE id = ?", (tid,))
    return tid, pid


def test_k109_start_token_from_previous_boot_is_not_owner(board, monkeypatch):
    monkeypatch.setattr(kb, "_boot_id", lambda: "boot-after", raising=False)
    with kb.connect_closing() as conn:
        tid, pid = _spawned_self(conn, "boot-before")
        window = kb._worker_owner_window(conn, tid, pid)
        # Without the other boot's token the causal window decides: this
        # process started after the recorded spawn, so it is not the worker.
        assert kb._owner_identity(pid, *window) == "recycled"


def test_k109_same_boot_token_still_verifies(board, monkeypatch):
    monkeypatch.setattr(kb, "_boot_id", lambda: "boot-same", raising=False)
    with kb.connect_closing() as conn:
        tid, pid = _spawned_self(conn, "boot-same")
        window = kb._worker_owner_window(conn, tid, pid)
        assert kb._owner_identity(pid, *window) == "verified"


# --- k110: the terminal-run reaper keeps existing run metadata --------


def test_k110_orphaned_terminal_reaper_keeps_pool_metadata(board):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker-a")
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        kb._stamp_run_pool(conn, run_id, "claude-bpr")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        assert run_id in kb.end_orphaned_terminal_runs(conn)
        raw = conn.execute("SELECT metadata FROM task_runs WHERE id = ?",
                           (run_id,)).fetchone()["metadata"]
        meta = json.loads(raw)
        assert meta.get("pool") == "claude-bpr"
        assert meta.get("reason") == kb.ORPHANED_TERMINAL_TASK_OUTCOME


# --- k112: another provider's declaration does not vouch for the id ----


def _switch_with_custom(raw, custom_providers):
    from unittest.mock import patch
    from hermes_cli.model_switch import switch_model

    soft = {"accepted": True, "persist": True, "recognized": False,
            "message": "Note: could not verify"}
    user_providers = {
        "my-apr": {"base_url": "http://apr.invalid/v1", "models": {"claude-opus-5": {}}},
    }
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch("hermes_cli.model_switch.normalize_model_for_provider",
               side_effect=lambda model, provider: model), \
         patch("hermes_cli.models_validate.validate_requested_model", return_value=soft), \
         patch("hermes_cli.models.detect_provider_for_model", return_value=None), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={"api_key": "***", "base_url": "http://resolved/v1",
                             "api_mode": ""}):
        return switch_model(
            raw_input=raw, current_provider="my-apr", current_model="claude-opus-5",
            explicit_provider="", user_providers=user_providers,
            custom_providers=custom_providers,
        )


def test_k112_foreign_provider_declaration_does_not_exempt_unknown_prefix():
    # An unnamed entry is invisible to config routing (it needs a ``name``),
    # so the switch stays on the current provider and reaches the refusal;
    # the declaration belongs to a DIFFERENT endpoint and must not exempt it.
    other = [{"base_url": "http://other.invalid/v1",
              "models": {"no-such-prov/claude-fable-5-1": {}}}]
    result = _switch_with_custom("no-such-prov/claude-fable-5-1", other)
    assert not result.success
    assert "Unknown provider 'no-such-prov'" in result.error_message


def test_k112_current_provider_declaration_still_exempts():
    mine = [{"name": "my-apr", "base_url": "http://resolved/v1",
             "models": {"no-such-prov/claude-fable-5-1": {}}}]
    result = _switch_with_custom("no-such-prov/claude-fable-5-1", mine)
    assert result.success, result.error_message


# --- k114: strip_overlay restores PATH components the overlay removed ---


@pytest.mark.parametrize("child_path,expected", [
    ("/c", "/a:/b"),                    # untouched since the overlay
    ("/venv:/c", "/venv:/a:/b"),        # re-edited after start
])
def test_k114_strip_overlay_restores_removed_path(monkeypatch, child_path, expected):
    from hermes_cli import process_env_files as pef

    monkeypatch.setattr(pef, "_OVERLAY", {"PATH": ("/a:/b", "/c")})
    env = {"PATH": child_path.replace(":", os.pathsep)}
    pef.strip_overlay(env)
    assert env["PATH"] == expected.replace(":", os.pathsep)


@pytest.mark.parametrize("old,new,child_path,expected", [
    ("/opt/bin:/usr/bin", "/usr/bin", "/usr/bin", "/opt/bin:/usr/bin"),
    ("/a:/b:/c", "/b", "/venv:/b", "/venv:/a:/b:/c"),
    ("/a:/b:/c:/d", "/x:/b:/d", "/x:/b:/d", "/a:/b:/c:/d"),
])
def test_k114_strip_overlay_keeps_removed_path_precedence(
        monkeypatch, old, new, child_path, expected):
    # FleetReview #1373 5796875694f5: removed components go back at their
    # original position relative to retained ones, not appended.
    from hermes_cli import process_env_files as pef

    sep = lambda s: s.replace(":", os.pathsep)
    monkeypatch.setattr(pef, "_OVERLAY", {"PATH": (sep(old), sep(new))})
    env = {"PATH": sep(child_path)}
    pef.strip_overlay(env)
    assert env["PATH"] == sep(expected)


# --- k115: the desktop cron ticker honours the serve admission hold ----


def test_k115_desktop_cron_ticker_passes_admission_gate(monkeypatch):
    import cron.scheduler_provider as sp
    from gateway import checkout_admission as ca
    from hermes_cli import web_server as ws

    captured = {}

    class _Provider(sp.InProcessCronScheduler):
        def start(self, stop_event, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sp, "resolve_cron_scheduler", lambda: _Provider())

    class _Refusing:
        def admit(self, identity, *, internal=False):
            raise ca.AdmissionRefused("checkout held (freeze) by operator")

    monkeypatch.setattr(ca, "process_gate",
                        lambda kind: _Refusing() if kind == "serve" else None)
    import threading

    ws._start_desktop_cron_ticker(threading.Event())
    gate = captured.get("can_dispatch")
    assert gate is not None, "desktop ticker must pass a dispatch gate"
    assert gate() is True
    assert gate.admit() is None  # the hold refuses scheduled dispatch

    monkeypatch.setattr(ca, "process_gate", lambda kind: None)
    release = gate.admit()
    assert callable(release)  # admission disabled: dispatch proceeds
