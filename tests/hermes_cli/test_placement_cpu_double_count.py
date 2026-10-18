"""Vendored placement: reservation threads already in load1 are not subtracted twice (card t_bfb88c23 PR 2).

Mirrors hermes-home scripts/tests/test_placement_cpu_double_count.py on the vendored bytes.
"""
from __future__ import annotations

from hermes_cli import placement

NOW = 10_000.0


def _book(ledger, threads, age_s, ram_gb=3.0):
    placement.reserve("ace-ai", "kanban", placement.Footprint(ram_gb, threads), ttl_s=1800, path=ledger,
                      now=NOW - age_s, admissions_path=ledger.parent / "admissions.jsonl")


def test_threads_of_rows_older_than_the_load1_window_drop_memory_stays(tmp_path):
    led = tmp_path / "resv.json"
    _book(led, 4, 61, ram_gb=8.0)
    _book(led, 2, 59, ram_gb=3.0)
    full = placement.live_reservations(led, now=NOW)["ace-ai"]
    seen = placement.live_reservations(led, now=NOW, cpu_seen_after_s=placement.LOAD1_WINDOW_S)["ace-ai"]
    assert (full.ram_gb, full.threads) == (11.0, 6.0)
    assert (seen.ram_gb, seen.threads) == (11.0, 2.0)
