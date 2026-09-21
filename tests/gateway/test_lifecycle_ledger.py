"""Tests for gateway.lifecycle_ledger — unclean-shutdown detection (NS-608).

The ledger is a tiny sentinel state machine:
``record_startup`` claims ``state/gateway.lifecycle.json`` as
``phase=running``; every exit path calls ``mark_exited``; the next boot's
``record_startup``/``detect_unclean_exit`` reports a still-``running``
sentinel from a dead process as an unclean death (SIGKILL / OOM / VM loss)
and enriches the report with the last heartbeat's memory sample.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from gateway.lifecycle_ledger import (
    detect_unclean_exit,
    get_lifecycle_sentinel_path,
    mark_exited,
    read_last_teardown_seconds,
    read_prior_exit_label,
    record_startup,
    record_teardown_timing,
    sample_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max on Linux; never alive


def _write_sentinel(home: Path, payload: dict) -> Path:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_sentinel(home: Path) -> dict:
    return json.loads(get_lifecycle_sentinel_path(home).read_text(encoding="utf-8"))


def _write_heartbeat(home: Path, payload: dict) -> Path:
    path = home / "state" / "gateway.heartbeat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _exit_diag_records(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# sample_memory
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
def test_sample_memory_has_expected_keys_on_linux() -> None:
    sample = sample_memory()
    assert sample.get("rss_kib", 0) > 0
    assert sample.get("mem_total_kib", 0) > 0
    assert "mem_available_kib" in sample


# ---------------------------------------------------------------------------
# Teardown timing
# ---------------------------------------------------------------------------


def test_teardown_timing_round_trips_and_reaches_exit_diag(tmp_path: Path) -> None:
    assert read_last_teardown_seconds(tmp_path) is None

    record_teardown_timing(
        18.25,
        total_shutdown_seconds=48.5,
        drain_seconds=30.0,
        budgeted=True,
        home=tmp_path,
    )

    assert read_last_teardown_seconds(tmp_path) == 18.25
    records = _exit_diag_records(tmp_path)
    assert records[-1]["tag"] == "gateway.shutdown_teardown_timing"
    assert records[-1]["teardown_seconds"] == 18.25
    assert records[-1]["total_shutdown_seconds"] == 48.5


def test_unbudgeted_teardown_is_diagnosed_but_never_becomes_the_reserve(
    tmp_path: Path,
) -> None:
    """An unconstrained stop's teardown must not size the next drain.

    ``hermes gateway stop`` / Ctrl+C / a foreground run have no supervisor
    deadline, so their post-drain work can legitimately run far longer
    than any launchd budget. Persisting that as the teardown reserve
    drives the next SIGTERM's drain to zero and drops in-flight sessions
    with no drain at all. The sample is still written for diagnostics.
    """
    record_teardown_timing(
        95.0,
        total_shutdown_seconds=120.0,
        drain_seconds=25.0,
        budgeted=False,
        home=tmp_path,
    )

    assert read_last_teardown_seconds(tmp_path) is None
    records = _exit_diag_records(tmp_path)
    assert records[-1]["teardown_seconds"] == 95.0
    assert records[-1]["budgeted"] is False


def test_oversized_budgeted_teardown_is_rejected_against_the_live_ceiling(
    tmp_path: Path,
) -> None:
    """FleetReview P1 #3: a large *finite* sample must not zero the drain.

    The inf/nan guard only covered values no measurement can produce. A
    slow-but-real teardown (wedged adapter, slow SQLite checkpoint) writes
    a large finite number; at clamp 60 a 55s reserve drives the drain to
    0.0 — every in-flight session dropped with no drain — and if that stop
    hard-exits before recording a new sample the file stays poisoned for
    every later shutdown.
    """
    from gateway.lifecycle_ledger import get_teardown_timing_path
    from gateway.restart import (
        resolve_launchd_capped_drain,
        resolve_max_actionable_teardown_reserve_s,
    )

    path = get_teardown_timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teardown_seconds": 55.0, "budgeted": True}))

    ceiling = resolve_max_actionable_teardown_reserve_s(60.0)
    assert ceiling == 50.0

    # Unbounded read still sees the poisoned value...
    assert read_last_teardown_seconds(tmp_path) == 55.0
    # ...and it is exactly what zeroes the drain.
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=55.0) == 0.0

    # Bounded read (what the gateway uses at boot) rejects it.
    assert read_last_teardown_seconds(tmp_path, max_seconds=ceiling) is None
    assert (
        resolve_launchd_capped_drain(
            50.0,
            60.0,
            last_teardown_s=read_last_teardown_seconds(tmp_path, max_seconds=ceiling),
        )
        == 35.0
    )


def test_poisoned_reserve_does_not_survive_the_next_successful_stop(
    tmp_path: Path,
) -> None:
    """The poisoned value must be replaced by the next real measurement."""
    from gateway.lifecycle_ledger import get_teardown_timing_path
    from gateway.restart import resolve_max_actionable_teardown_reserve_s

    path = get_teardown_timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teardown_seconds": 55.0, "budgeted": True}))

    ceiling = resolve_max_actionable_teardown_reserve_s(60.0)
    assert read_last_teardown_seconds(tmp_path, max_seconds=ceiling) is None

    record_teardown_timing(
        12.0,
        total_shutdown_seconds=40.0,
        drain_seconds=28.0,
        budgeted=True,
        home=tmp_path,
    )

    assert read_last_teardown_seconds(tmp_path, max_seconds=ceiling) == 12.0


def test_no_launchd_budget_means_no_teardown_ceiling() -> None:
    """Off launchd there is no SIGKILL to race, so no ceiling applies."""
    from gateway.restart import resolve_max_actionable_teardown_reserve_s

    assert resolve_max_actionable_teardown_reserve_s(None) is None
    assert resolve_max_actionable_teardown_reserve_s(0.0) is None
    assert resolve_max_actionable_teardown_reserve_s("nope") is None  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_non_finite_persisted_teardown_is_rejected(tmp_path: Path, bad: str) -> None:
    """A corrupt file must not become an unbounded teardown reserve.

    ``inf`` passes a bare ``>= 0.0`` check, and the reserve flows straight
    into the next shutdown's drain budget — an unbounded reserve silently
    drives that budget to zero. Exercised through the real file path, not
    the parameter, because the file is what feeds the live value.
    """
    from gateway.lifecycle_ledger import get_teardown_timing_path

    path = get_teardown_timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teardown_seconds": float(bad), "budgeted": True}))

    assert read_last_teardown_seconds(tmp_path) is None


def test_finite_persisted_teardown_still_survives_the_guard(tmp_path: Path) -> None:
    from gateway.lifecycle_ledger import get_teardown_timing_path

    path = get_teardown_timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teardown_seconds": 22.0, "budgeted": True}))

    assert read_last_teardown_seconds(tmp_path) == 22.0
    # Well under the clamp-60 ceiling, so bounding does not reject it.
    assert read_last_teardown_seconds(tmp_path, max_seconds=50.0) == 22.0


def test_legacy_record_without_budgeted_field_is_not_trusted(tmp_path: Path) -> None:
    """Pre-existing files carry no provenance, so they cannot be trusted.

    A record written before ``budgeted`` existed may have come from an
    unconstrained stop. Treating it as budgeted would reintroduce exactly
    the poisoning this guard closes, so it degrades to "no measurement".
    """
    from gateway.lifecycle_ledger import get_teardown_timing_path

    path = get_teardown_timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teardown_seconds": 22.0}))

    assert read_last_teardown_seconds(tmp_path) is None


# ---------------------------------------------------------------------------
# First boot / clean lifecycle
# ---------------------------------------------------------------------------


def test_first_boot_reports_nothing_and_claims_sentinel(tmp_path: Path) -> None:
    assert record_startup(home=tmp_path) is None
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()
    assert "start_time" in sentinel


def test_clean_exit_then_boot_reports_nothing(tmp_path: Path) -> None:
    record_startup(home=tmp_path)
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)

    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "exited"
    assert sentinel["exit_code"] == 0
    assert sentinel["exit_reason"] == "graceful_shutdown"

    assert record_startup(home=tmp_path) is None
    assert _exit_diag_records(tmp_path) == []


# ---------------------------------------------------------------------------
# Unclean-death detection
# ---------------------------------------------------------------------------


def test_running_sentinel_from_dead_pid_is_unclean(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = detect_unclean_exit(home=tmp_path)
    assert evidence is not None
    assert evidence["prior_pid"] == _DEAD_PID
    assert evidence["prior_started_at"] == "2026-07-11T04:30:00+00:00"


def test_record_startup_persists_unclean_report_and_reclaims(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = record_startup(home=tmp_path)
    assert evidence is not None

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tag"] == "gateway.previous_unclean_exit"
    assert records[0]["prior_pid"] == _DEAD_PID
    assert records[0]["pid"] == os.getpid()

    # Sentinel reclaimed for the new life.
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()


def test_record_startup_carries_unclean_flags_onto_new_sentinel(
    tmp_path: Path,
) -> None:
    """The unclean-death verdict must survive on the reclaimed sentinel so
    /api/status can surface "restarted after (suspected) OOM" (NS-656)."""
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })
    # Last heartbeat shows near-exhausted memory → suspected OOM.
    from gateway.shutdown_watchdog import get_loop_heartbeat_path

    hb_path = get_loop_heartbeat_path(tmp_path)
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps({
        "pid": _DEAD_PID,
        "updated_at": "2026-07-11T05:00:00+00:00",
        "mem": {"mem_total_kib": 1024 * 1024, "mem_available_kib": 20 * 1024},
    }), encoding="utf-8")

    evidence = record_startup(home=tmp_path)
    assert evidence is not None
    assert evidence.get("suspected_oom") is True

    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["prior_unclean_exit"] is True
    assert sentinel["prior_suspected_oom"] is True


def test_record_startup_clean_boot_has_no_prior_flags(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "exited",
        "pid": _DEAD_PID,
        "exit_code": 0,
        "exit_reason": "graceful_shutdown",
    })
    assert record_startup(home=tmp_path) is None
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert "prior_unclean_exit" not in sentinel
    assert "prior_suspected_oom" not in sentinel


# ---------------------------------------------------------------------------
# Takeover ownership guard on mark_exited
# ---------------------------------------------------------------------------


def test_mark_exited_leaves_pid_none_sentinel_alone(tmp_path: Path) -> None:
    """A sentinel with pid=None has unknown ownership — mark_exited must not
    clobber it with a clean-exit claim it cannot prove is its own."""
    _write_sentinel(tmp_path, {"phase": "running", "pid": None, "start_time": 2000.0})
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] is None


# ---------------------------------------------------------------------------
# read_prior_exit_label (container-boot annotation)
# ---------------------------------------------------------------------------


def test_prior_exit_label_survives_corrupt_sentinel(tmp_path: Path) -> None:
    path = get_lifecycle_sentinel_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage", encoding="utf-8")
    assert read_prior_exit_label(tmp_path) == "unknown"
