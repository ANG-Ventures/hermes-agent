"""FleetReview P2 backfill (t_f4ef102c): one regression per small fix, red on base."""
import asyncio
import gc
import logging

import pytest


def test_same_commit_on_another_ref_is_not_skew(monkeypatch):  # backfill #36
    from gateway import code_skew
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")
    code_skew.record_boot_fingerprint()
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/other:abc1234567890")
    assert code_skew.detect_code_skew() is None


def test_self_repo_block_message_quotes_the_clone_destination(monkeypatch):  # backfill #48
    import shlex
    from pathlib import Path
    from tools import self_repo_guard as g
    monkeypatch.setattr(g, "_scratch_dir_hint", lambda: "/tmp/has space/scratch")
    msg = g._block_message("git checkout", Path("/src/repo"))
    cmd = msg.split("`git clone", 1)[1].split("`", 1)[0]
    argv = shlex.split("git clone" + cmd)
    assert argv[-1] == "/tmp/has space/scratch/<task>", argv


def test_lifecycle_gc_throttle_survives_a_wall_clock_rollback(monkeypatch):  # backfill #81
    import time
    from plugins.context_engine.lcm import engine
    monkeypatch.setattr(engine, "_LIFECYCLE_GC_LAST_RUN", {})
    real = time.time()
    monkeypatch.setattr(engine.time, "time", lambda: real)
    assert engine._lifecycle_gc_due("db", 6.0) is True
    monotonic = engine.time.monotonic()
    monkeypatch.setattr(engine.time, "time", lambda: real - 30 * 86400)   # clock steps back
    monkeypatch.setattr(engine.time, "monotonic", lambda: monotonic + 7 * 3600)
    assert engine._lifecycle_gc_due("db", 6.0) is True


def test_heavy_read_semaphores_do_not_outlive_their_loop():  # backfill #92
    from hermes_cli import web_server as ws

    async def grab():
        return ws._session_db_heavy_read_semaphore()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(grab())
    loop.close()
    del loop
    gc.collect()
    assert len(ws._SESSION_DB_HEAVY_READ_SEMAPHORES) == 0


def test_moa_never_logs_prompt_content(monkeypatch, caplog):  # backfill #100
    from tools import mixture_of_agents_tool as moa
    monkeypatch.setattr(moa, "check_openrouter_api_key", lambda: False)
    secret = "hunter2-SECRET-TOKEN"
    with caplog.at_level(logging.DEBUG):
        try:
            asyncio.run(moa.mixture_of_agents_tool(secret + " rest of prompt"))
        except Exception:
            pass
    assert secret not in caplog.text


def test_spawn_arm_fails_open_when_the_log_dir_cannot_be_created(monkeypatch, tmp_path):  # #15
    from hermes_cli import kanban_pr_freshness as fr
    import hermes_constants
    script = tmp_path / "fleet-merge.sh"
    script.write_text("exit 0\n")
    blocker = tmp_path / "root"
    blocker.write_text("a file where the root dir should be\n")
    monkeypatch.setattr(fr, "_fleet_merge_path", lambda: script)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: blocker)
    assert fr.spawn_arm("o/r", 1, "a" * 40, "t_x") is None
