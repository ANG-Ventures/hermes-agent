"""Slot aliases P6 (t_efde9d1a): writers that store a model (``/model``, a cron job's model, a
card's model pin) store the slot name when the value is exactly a slot's CURRENT id on a lane that
serves it, and say so in one confirmation line. An explicit opt-out keeps the literal
(``/model --exact``; ``--force-model <reason>`` on cron and kanban). Retired ids keep today's
successor rewrite. No other value is touched.

Before this change every one of these writers stored ``claude-sonnet-5-5`` as typed, so the
literal crept back into seats after the slot sweep and the next rollout needed another sweep.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import model_registry
from hermes_cli.model_registry import slot_canonicalise, slot_store_note

SONNET = "claude-sonnet-5-5"
NOTE = f"stored as slot sonnet, currently id {SONNET}"


def _write(root: Path, name: str, body) -> None:
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) if not isinstance(body, str) else body, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def slots(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(home, "model-slots.json", {"slots": {
        "sonnet": SONNET, "sol": "gpt-6.2-sol", "gemini-pro": "google/gemini-4-argon"}})
    model_registry.clear_catalog_memo()
    yield home
    model_registry.clear_catalog_memo()


# ── the rule ─────────────────────────────────────────────────────────────────

def test_current_id_becomes_its_slot(slots):
    assert slot_canonicalise(SONNET) == ("sonnet", NOTE)
    assert slot_canonicalise(f"claude-alr/{SONNET}", "claude-alr") == (
        "claude-alr/sonnet", f"stored as slot claude-alr/sonnet, currently id claude-alr/{SONNET}")
    assert slot_canonicalise("google/gemini-4-argon")[0] == "gemini-pro"


@pytest.mark.parametrize("value", [
    "sonnet",              # already a slot
    "claude-sonnet-5",     # no slot points at it (a retired id keeps the successor rewrite)
    "claude-opus-5-5",     # not a slot target in this view
    "", None,
])
def test_other_values_untouched(slots, value):
    assert slot_canonicalise(value) == (value, None)


def test_exact_opt_out_keeps_literal(slots):
    assert slot_canonicalise(SONNET, exact=True) == (SONNET, None)


def test_lane_that_does_not_serve_the_id_keeps_literal(slots, monkeypatch):
    monkeypatch.setattr(model_registry, "_fetch_catalog",
                        lambda provider, force_refresh=False: {"claude-alr": ("claude-opus-5-5",)}.get(provider, ()))
    assert slot_canonicalise(SONNET, "claude-alr") == (SONNET, None)
    monkeypatch.setattr(model_registry, "_fetch_catalog",
                        lambda provider, force_refresh=False: ("claude-alr/" + SONNET,))
    model_registry.clear_catalog_memo()
    assert slot_canonicalise(SONNET, "claude-alr") == ("sonnet", NOTE)


def test_id_two_slots_share_keeps_literal(slots):
    _write(slots, "model-slots.json", {"slots": {"sonnet": SONNET, "writer": SONNET}})
    assert slot_canonicalise(SONNET) == (SONNET, None)


def test_missing_view_keeps_literal(slots):
    (slots / "config" / "model-slots.json").unlink()
    assert slot_canonicalise(SONNET) == (SONNET, None)


def test_store_note(slots):
    assert slot_store_note(SONNET, "sonnet") == NOTE
    assert slot_store_note("sonnet", "sonnet") is None
    assert slot_store_note(SONNET, SONNET) is None


# ── /model ───────────────────────────────────────────────────────────────────

def _switch_result(model=SONNET, provider="claude-alr"):
    from hermes_cli.model_switch import ModelSwitchResult
    return ModelSwitchResult(success=True, new_model=model, target_provider=provider)


def test_model_command_stores_slot_and_says_so(slots):
    from hermes_cli.model_switch import canonicalise_switch_result, parse_model_switch_args
    request = parse_model_switch_args(f"{SONNET} --global")
    assert request.exact is False and request.target == SONNET
    result = _switch_result()
    canonicalise_switch_result(result, exact=request.exact)
    assert result.new_model == "sonnet" and NOTE in result.warning_message


def test_model_command_exact_keeps_literal(slots):
    from hermes_cli.model_switch import canonicalise_switch_result, parse_model_switch_args
    request = parse_model_switch_args(f"{SONNET} --exact")
    assert request.exact is True and request.target == SONNET
    assert parse_model_switch_args(f"{SONNET} \u2014exact").exact is True  # phone dash
    result = _switch_result()
    canonicalise_switch_result(result, exact=request.exact)
    assert result.new_model == SONNET and not result.warning_message


def test_gateway_model_command_threads_exact(slots, monkeypatch):
    """The gateway /model path stores the slot (and ``--exact`` keeps the literal) end to end."""
    import asyncio

    from gateway.slash_commands_model import GatewayModelCommandsMixin

    monkeypatch.setattr("gateway.slash_commands_model._model_switch_skew_guard", lambda: None)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: _switch_result(kw["raw_input"]))
    monkeypatch.setattr("hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway",
                        lambda *a, **k: None)
    ctx = SimpleNamespace(current_provider="claude-alr", current_model="x", current_base_url="",
                          current_api_key="", persist_global=False, user_provs=None, custom_provs=None,
                          session_key="k")
    obj = GatewayModelCommandsMixin()
    stored, _ = asyncio.run(obj._perform_model_switch(ctx, SONNET, "", None))
    exact, _ = asyncio.run(obj._perform_model_switch(ctx, SONNET, "", None, exact=True))
    assert stored.new_model == "sonnet" and NOTE in stored.warning_message
    assert exact.new_model == SONNET


# ── cron ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def cron_store(slots, tmp_path, monkeypatch):
    from cron import jobs
    monkeypatch.setattr("hermes_cli.model_policy.validate_worker_model", lambda *a, **k: None)
    with jobs.use_cron_store(tmp_path / "cron"):
        yield jobs


def _stored(jobs, job_id):
    return next(j for j in jobs.load_jobs() if j["id"] == job_id)


def test_cron_create_and_update_store_slot(cron_store):
    jobs = cron_store
    job = jobs.create_job(prompt="p", schedule="every 1h", model=SONNET, provider="claude-alr")
    assert _stored(jobs, job["id"])["model"] == "sonnet"
    jobs.update_job(job["id"], {"model": "claude-opus-5-5"})
    assert _stored(jobs, job["id"])["model"] == "claude-opus-5-5"
    jobs.update_job(job["id"], {"model": SONNET})
    assert _stored(jobs, job["id"])["model"] == "sonnet"


def test_cron_force_model_keeps_literal(cron_store):
    jobs = cron_store
    job = jobs.create_job(prompt="p", schedule="every 1h", model=SONNET, provider="claude-alr",
                          force_model_reason="pin this exact id")
    assert _stored(jobs, job["id"])["model"] == SONNET
    jobs.update_job(job["id"], {"model": SONNET, "force_model_reason": "still exact"})
    assert _stored(jobs, job["id"])["model"] == SONNET


def test_cron_tool_confirmation_line(cron_store):
    from tools import cronjob_tools as ct
    job = {"model": "sonnet"}
    assert ct._model_canonicalised(SONNET, job) == NOTE
    assert ct._model_canonicalised("sonnet", job) is None


# ── kanban card pins ─────────────────────────────────────────────────────────

@pytest.fixture
def board(slots, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def test_card_create_stores_slot(board):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="peer", model_override=SONNET)
    assert kb.get_task(conn, tid).model_override == "sonnet"
    forced = kb.create_task(conn, title="f", assignee="peer", model_override=SONNET,
                            force_model_reason="exact id on purpose")
    assert kb.get_task(conn, forced).model_override == SONNET
    kept = kb.create_task(conn, title="k", assignee="peer", model_override=SONNET, keep_literal_model=True)
    assert kb.get_task(conn, kept).model_override == SONNET


def test_card_set_model_paths_store_slot(board):
    kb, conn = board
    a = kb.create_task(conn, title="a", assignee="peer")
    b = kb.create_task(conn, title="b", assignee="peer")
    assert kb.set_task_model(conn, a, SONNET) == 1
    assert kb.get_task(conn, a).model_override == "sonnet"
    assert kb.set_model_override(conn, b, SONNET, None)
    assert kb.get_task(conn, b).model_override == "sonnet"
    assert kb.set_task_model(conn, a, SONNET, force_model_reason="exact") == 1
    assert kb.get_task(conn, a).model_override == SONNET


def test_kanban_tool_create_says_so_and_retired_keeps_successor(slots, monkeypatch):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    d = json.loads(kt._handle_create({"board": "default", "title": "pin", "assignee": "peer",
                                      "model_override": SONNET}))
    assert d["ok"] is True and d["model_canonicalised"] == NOTE, d
    # A retired id keeps today's rewrite: its successor is stored literally, with the rewrite note.
    _write(slots, "model-retired-ids.txt", "claude-sonnet-5 cutover=2026-11-01T00:00-08:00\n")
    _write(slots, "model-successors.json", {"successors": {"claude-sonnet-5": SONNET}})
    r = json.loads(kt._handle_create({"board": "default", "title": "old", "assignee": "peer",
                                      "model_override": "claude-sonnet-5"}))
    assert r["ok"] is True and "model_canonicalised" not in r and "model_rewritten" in r, r
    conn = kb.connect()
    try:
        assert kb.get_task(conn, d["task_id"]).model_override == "sonnet"
        assert kb.get_task(conn, r["task_id"]).model_override == SONNET
    finally:
        conn.close()


# ── Prism round 1 on #487 ────────────────────────────────────────────────────

def test_lane_qualified_target_matches_bare_id_on_that_lane(slots):
    """A slot whose target carries its lane (``sol: cpa/gpt-6.2-sol``) matches the bare id a
    writer passes with ``provider=cpa`` (Prism 7c7a0c08ca9d). Another lane keeps the literal."""
    _write(slots, "model-slots.json", {"slots": {"sol": "cpa/gpt-6.2-sol"}})
    assert slot_canonicalise("gpt-6.2-sol", "cpa") == (
        "sol", "stored as slot sol, currently id gpt-6.2-sol")
    assert slot_canonicalise("gpt-6.2-sol", "openai-codex") == ("gpt-6.2-sol", None)
    assert slot_canonicalise("gpt-6.2-sol") == ("gpt-6.2-sol", None)


def test_batch_set_model_keeps_stdout_one_record_per_card(board, capsys):
    """Batch ``set-model`` prints one record per card on stdout; the slot line goes to stderr
    (Prism 76ade10dd010). A single named card keeps the line on stdout."""
    import argparse

    from hermes_cli import kanban as kc

    kb, conn = board
    a = kb.create_task(conn, title="a", assignee="peer")
    b = kb.create_task(conn, title="b", assignee="peer")
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="command"))
    capsys.readouterr()
    assert kc.kanban_command(parser.parse_args(["kanban", "set-model", a, b, SONNET])) == 0
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2 and all(line.startswith("t_") for line in lines), out
    assert NOTE in err
    assert kc.kanban_command(parser.parse_args(["kanban", "set-model", a, SONNET])) == 0
    out, _ = capsys.readouterr()
    assert NOTE in out
