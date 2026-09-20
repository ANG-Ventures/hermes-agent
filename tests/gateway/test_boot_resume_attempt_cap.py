"""Boot auto-resume must be bounded PER SESSION, across boots.

Incident (2026-09-20). One Discord session — last real turn 2026-09-19 — was
replayed in full on ten consecutive overnight gateway boots, ~450k chars of
history per replay (~4.5M tokens), because every existing gate is per-TURN:

* ``_prepare_boot_resume_work_check`` asks whether THIS tail has unfinished
  work. A resume turn that is itself amputated by the next restart leaves a
  fresh unfinished tail, so the answer is "yes" forever.
* ``AutoResumeAttemptStore.has_attempt`` is keyed on
  ``(session_key, assistant_rowid)``. Each restart interrupts a NEW assistant
  row, so the once-ever credit is fresh on every boot — and a ``kind=self``
  resume short-circuits before the credit is consulted at all, which is what
  the incident session logged (``kind=self mode=auto`` at 01:15 and 02:01).

Nothing counted resumes per SESSION across boots. This suite pins that counter:
it accumulates across scheduler passes, survives a process restart via the
persisted store, stops the replay at the cap while leaving the transcript
intact, and is released by real forward progress.

Sibling contract (the same boot-resume scheduler) lives in
``test_boot_resume_skips_finished_sessions.py``; this file deliberately reuses
its fixtures so both gates are exercised through the real scheduler.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import gateway.run as gateway_run
import hermes_state
from gateway.auto_resume import AutoResumeAttemptStore
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


def _source(user_id: str = "u1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=user_id,
        chat_type="dm",
        user_id=user_id,
    )


def _runner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    runner, adapter = make_restart_runner()
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.session_store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(),
    )
    db = runner.session_store._db
    assert isinstance(db, SessionDB)
    runner._session_db = AsyncSessionDB(db)
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def _scheduled_resume_stub(_adapter, _event, _session_key):
        return None

    monkeypatch.setattr(runner, "_run_startup_resume_event", _scheduled_resume_stub)
    return runner, adapter, db


def _seed(db: SessionDB, entry, rows: list[dict]) -> None:
    db.create_session(entry.session_id, "gateway", session_key=entry.session_key)
    for row in rows:
        db.append_message(
            entry.session_id,
            row["role"],
            row.get("content"),
            tool_calls=row.get("tool_calls"),
            tool_call_id=row.get("tool_call_id"),
            finish_reason=row.get("finish_reason"),
        )


# An interrupted tool-call tail: unfinished work, so every pre-existing gate
# votes RESUME. The cap is the only thing that can stop the replay.
_INTERRUPTED_TAIL = [
    {"role": "user", "content": "take over the discord channel task"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
        "finish_reason": "tool_calls",
    },
]


def _remark(runner, entry) -> None:
    """Re-mark the session as a fresh boot would after another restart."""
    assert runner.session_store.mark_resume_pending(entry.session_key, "shutdown_timeout")


def _boot(runner) -> int:
    """One scheduler pass, with the per-boot state a real boot starts from."""
    runner._resumed_this_boot = set()
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    return runner._schedule_resume_pending_sessions()


# --------------------------------------------------------------------------
# The store: the counter itself
# --------------------------------------------------------------------------


def test_session_attempts_accumulate_and_persist_across_store_instances(tmp_path):
    """A new process must see the previous boots' count — that is the whole point."""
    path = tmp_path / "state" / "auto_resume_attempts.json"

    first = AutoResumeAttemptStore(path)
    assert first.record_session_attempt("s1") == 1
    assert first.record_session_attempt("s1") == 2

    # Fresh instance = the next gateway process.
    second = AutoResumeAttemptStore(path)
    assert second.session_attempt_count("s1") == 2
    assert second.record_session_attempt("s1") == 3
    assert second.session_cap_reached("s1", 3) is True
    # Sibling sessions are independent.
    assert second.session_attempt_count("s2") == 0
    assert second.session_cap_reached("s2", 3) is False


def test_session_cap_of_zero_disables_the_cap(tmp_path):
    store = AutoResumeAttemptStore(tmp_path / "attempts.json")
    for _ in range(50):
        store.record_session_attempt("s1")
    assert store.session_cap_reached("s1", 0) is False


def test_clear_session_attempts_releases_the_budget(tmp_path):
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path)
    store.record_session_attempt("s1")
    store.record_session_attempt("s1")
    store.clear_session_attempts("s1")

    assert AutoResumeAttemptStore(path).session_attempt_count("s1") == 0


def test_session_counters_do_not_disturb_the_rowid_credits(tmp_path):
    """The two grains share a file; neither may clobber the other."""
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path)

    assert store.consume("s1", 42) is True
    store.record_session_attempt("s1")
    assert store.has_attempt("s1", 42) is True

    reloaded = AutoResumeAttemptStore(path)
    assert reloaded.has_attempt("s1", 42) is True
    assert reloaded.session_attempt_count("s1") == 1
    # ...and clearing the session counter leaves the rowid credit spent.
    reloaded.clear_session_attempts("s1")
    assert AutoResumeAttemptStore(path).has_attempt("s1", 42) is True


def test_store_written_by_an_older_gateway_loads_with_zero_counters(tmp_path):
    """Forward/backward compatible: the key is additive, no version bump."""
    path = tmp_path / "attempts.json"
    now = 1_000_000.0
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": [
                    {"session_key": "s1", "assistant_rowid": 7, "attempted_at": now}
                ],
            }
        ),
        encoding="utf-8",
    )
    store = AutoResumeAttemptStore(path, now=lambda: now)
    assert store.session_attempt_count("s1") == 0
    assert store.has_attempt("s1", 7) is True


def test_unreadable_store_repairs_itself_instead_of_latching_off(tmp_path, caplog):
    """An unreadable counter cache must be RESET, not treated as unusable forever.

    Round-1 review made a poisoned store report a huge number, which capped
    every session on the host. Round 2 made it report ``None``, which capped
    nobody — and measured 10/10 uncapped replays, permanently, because
    ``_invalid`` latched and no write path ever repaired the file. Both
    directions of "latch on the fault" are wrong for a file that holds nothing
    but recoverable counters. Resetting it restores the bound on the same boot.

    Scheduler-grain contract: ``test_boot_resume_cap_poisoned_store.py``.
    """
    path = tmp_path / "attempts.json"
    path.write_text("{not json", encoding="utf-8")
    store = AutoResumeAttemptStore(path)

    with caplog.at_level(logging.WARNING):
        # A never-resumed session honestly reads zero, not "unknown".
        assert store.session_attempt_count("s1") == 0
        assert store.session_cap_reached("s1", 3) is False
        # ...and counting works again immediately, so the cap can still bound.
        assert store.record_session_attempt("s1") == 1
        assert store.record_session_attempt("s1") == 2
        assert store.record_session_attempt("s1") == 3
        assert store.session_cap_reached("s1", 3) is True

    assert sum("was unreadable" in r.getMessage() for r in caplog.records) == 1
    # The repair landed on disk: a fresh process sees valid state, not the
    # corrupt bytes that used to survive every boot.
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert AutoResumeAttemptStore(path).session_attempt_count("s1") == 3
    # The rowid credit still fails CLOSED for the rest of THIS process: the
    # repair discarded real credits, so an empty ledger must not mint new ones.
    assert store.has_attempt("s1", 7) is True


@pytest.mark.parametrize(
    "counters",
    [
        pytest.param("not-a-dict", id="not_an_object"),
        pytest.param({"s1": {"count": -1, "attempted_at": 1.0}}, id="negative_count"),
        pytest.param({"s1": {"count": True, "attempted_at": 1.0}}, id="bool_count"),
        pytest.param({"s1": {"count": 1, "attempted_at": "soon"}}, id="non_numeric_ts"),
    ],
)
def test_malformed_session_counters_repair_to_zero(tmp_path, counters):
    path = tmp_path / "attempts.json"
    path.write_text(
        json.dumps({"version": 1, "attempts": [], "session_attempts": counters}),
        encoding="utf-8",
    )
    store = AutoResumeAttemptStore(path)
    assert store.session_attempt_count("s1") == 0
    assert store.session_cap_reached("s1", 3) is False
    assert store.record_session_attempt("s1") == 1


def test_an_unwritable_store_denies_the_resume_rather_than_replaying_forever(
    tmp_path, caplog
):
    """Lost accounting must not be an unbounded licence to replay.

    Unlike an unreadable file, an unwritable one cannot self-heal, so nothing
    the cap is told can ever be counted. Round 2 measured that arm at 10 boots
    / 10 full-transcript replays / 0 cap lines — the incident, restated. The
    verdict says so instead, and says it BEFORE the resume is scheduled
    (``record_session_attempt`` runs too late to bound anything).

    The attempts field is ``None``, which is how the scheduler knows to leave
    ``resume_pending`` set: this denial is about the disk and must evaporate
    when the disk is fixed.
    """
    path = tmp_path / "attempts.json"
    store = AutoResumeAttemptStore(path)
    assert store.record_session_attempt("s1") == 1

    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    store._write = _boom
    store._persist_proven = False  # a fresh process has not proven the path yet

    with caplog.at_level(logging.WARNING):
        assert store.session_resume_verdict("s1", 3) == (False, None)
    assert store.session_cap_reached("s1", 3) is True
    assert store.record_session_attempt("s1") is None
    assert sum("cannot be written" in r.getMessage() for r in caplog.records) == 1


def test_a_writable_store_is_not_denied_by_the_persistability_probe(tmp_path):
    """The probe must not deny a healthy store, and must not corrupt its counts."""
    path = tmp_path / "nested" / "attempts.json"
    store = AutoResumeAttemptStore(path)
    assert store.record_session_attempt("s1") == 1

    fresh = AutoResumeAttemptStore(path)
    assert fresh.session_resume_verdict("s1", 3) == (True, 1)
    assert fresh.session_attempt_count("s1") == 1


def test_session_counters_expire_with_the_attempt_ttl(tmp_path):
    path = tmp_path / "attempts.json"
    AutoResumeAttemptStore(path, now=lambda: 100.0).record_session_attempt("s1")

    later = AutoResumeAttemptStore(path, now=lambda: 100.0 + 8 * 86400)
    assert later.session_attempt_count("s1") == 0


# --------------------------------------------------------------------------
# The scheduler: the cap wired into the real boot-resume path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_boots_stop_resuming_the_same_session_at_the_cap(
    tmp_path, monkeypatch, caplog
):
    """THE regression: an always-unfinished session replayed on every boot.

    Without the cap this loops forever — which is exactly what ten overnight
    boots did to a ~450k-char transcript on 2026-09-20.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "3")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    for boot in range(3):
        _remark(runner, entry)
        await runner._prepare_boot_resume_work_check()
        assert _boot(runner) == 1, f"boot {boot} should still resume"

    # Fourth boot: budget exhausted.
    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    caplog.clear()  # earlier boots legitimately logged "scheduled"
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert _boot(runner) == 0

    messages = [record.getMessage() for record in caplog.records]
    assert any("cause=attempt_cap" in m for m in messages)
    assert not any("PHASE=boot_resume_scheduled" in m for m in messages)
    assert runner._background_tasks == set()

    # The marker is retired so later boots do not re-litigate it...
    refreshed = runner.session_store._entries[entry.session_key]
    assert refreshed.resume_pending is False
    # ...but the conversation itself is untouched: a real user message still
    # continues it. Losing history would be a far worse bug than the replay.
    assert refreshed.session_id == entry.session_id
    assert len(db.get_messages(entry.session_id)) == len(_INTERRUPTED_TAIL)
    db.close()


@pytest.mark.asyncio
async def test_kind_self_resumes_are_counted_too(tmp_path, monkeypatch, caplog):
    """``kind=self`` skips the rowid credit entirely — it must not skip the cap.

    The incident session logged ``kind=self mode=auto`` on two of its boots.
    """
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "2")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    for _ in range(2):
        assert runner.session_store.mark_resume_pending(
            entry.session_key, "restart_interrupted", resume_kind="self"
        )
        await runner._prepare_auto_resume_decisions()
        assert _boot(runner) == 1

    assert runner.session_store.mark_resume_pending(
        entry.session_key, "restart_interrupted", resume_kind="self"
    )
    await runner._prepare_auto_resume_decisions()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert _boot(runner) == 0
    assert any(
        "cause=attempt_cap" in r.getMessage() and "kind=self" in r.getMessage()
        for r in caplog.records
    )
    db.close()


@pytest.mark.asyncio
async def test_sibling_shutdown_timeout_resumes_are_counted(tmp_path, monkeypatch):
    """``kind=sibling reason=shutdown_timeout`` is the incident's other half."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "2")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    store = runner._get_auto_resume_attempt_store()
    for expected in (1, 2):
        _remark(runner, entry)
        await runner._prepare_boot_resume_work_check()
        assert _boot(runner) == 1
        assert store.session_attempt_count(entry.session_key) == expected

    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 0
    db.close()


@pytest.mark.asyncio
async def test_the_cap_is_per_session_not_global(tmp_path, monkeypatch):
    """A capped session must not starve a healthy sibling on the same boot."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "1")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    noisy = runner.session_store.get_or_create_session(_source("noisy"))
    quiet = runner.session_store.get_or_create_session(_source("quiet"))
    _seed(db, noisy, _INTERRUPTED_TAIL)
    _seed(db, quiet, _INTERRUPTED_TAIL)

    _remark(runner, noisy)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 1

    # Next boot: noisy is capped, quiet has never resumed.
    _remark(runner, noisy)
    _remark(runner, quiet)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 1
    assert quiet.session_key in runner._resumed_this_boot
    assert noisy.session_key not in runner._resumed_this_boot
    # The capped session's marker is retired; the healthy one keeps its marker
    # until its own resumed turn clears it post-turn.
    assert runner.session_store._entries[noisy.session_key].resume_pending is False
    assert runner.session_store._entries[quiet.session_key].resume_pending is True
    db.close()


@pytest.mark.asyncio
async def test_forward_progress_releases_the_budget(tmp_path, monkeypatch):
    """A resumed turn that did real work must not leave the session closer to the cap.

    Otherwise a healthy long-lived session would be locked out of resume after
    three unlucky deploys — the false-positive class the F2 breaker already
    learned the hard way (2026-07-10 kanban-session suspension).
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "2")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    store = runner._get_auto_resume_attempt_store()

    for _ in range(2):
        _remark(runner, entry)
        await runner._prepare_boot_resume_work_check()
        assert _boot(runner) == 1
    assert store.session_cap_reached(entry.session_key, 2) is True

    # The resumed turn finally completes real work (no restart initiated).
    runner._apply_post_turn_resume_gate(entry.session_key)
    assert store.session_attempt_count(entry.session_key) == 0

    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 1
    db.close()


@pytest.mark.asyncio
async def test_a_self_restarting_turn_does_not_refresh_its_own_budget(
    tmp_path, monkeypatch
):
    """Loop progress is not work progress — the F2 rule, applied to the cap.

    A turn whose only outcome is another restart must keep its count, or a
    restart loop would top its own budget up forever.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "2")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    store = runner._get_auto_resume_attempt_store()

    _remark(runner, entry)
    await runner._prepare_boot_resume_work_check()
    assert _boot(runner) == 1
    assert store.session_attempt_count(entry.session_key) == 1

    runner._session_initiated_restart[entry.session_key] = True
    runner._apply_post_turn_resume_gate(entry.session_key)
    assert store.session_attempt_count(entry.session_key) == 1
    db.close()


@pytest.mark.asyncio
async def test_cap_of_zero_restores_unbounded_resume(tmp_path, monkeypatch):
    """The documented escape hatch must actually disable the cap.

    Guards the off-by-one that made the global breaker's ``<= 0`` short-circuit
    dead code: ``_auto_resume_max_attempts`` floors at 0, not 1.
    """
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "0")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)

    for _ in range(6):
        _remark(runner, entry)
        await runner._prepare_boot_resume_work_check()
        assert _boot(runner) == 1
    db.close()
