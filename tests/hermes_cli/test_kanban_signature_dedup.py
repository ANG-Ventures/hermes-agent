"""Signature resolver + flap breaker on ``create_task`` (PRD plans/2026-11-05_machine-minted-cards-signature-dedup-prd.md).

Phase-0 replay: main-tip-red minted 523 cards for 184 failure signatures in 7 days; 336 would have reused a card and
20 signatures closed 3+ times in a row with no code change. One open-or-recent card per signature; a signature that
keeps closing with nothing to do trips the breaker instead of burning another worker run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

SIG = "main-tip-red:ANG-Ventures/hermes-home/chrome-launch-helper: lint the tree/(suite-level)"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for v in ("HERMES_KANBAN_SIGNATURE_DEDUP", "HERMES_KANBAN_MINT_BREAKER",
              "HERMES_KANBAN_SIGNATURE_QUIET_S", "HERMES_KANBAN_SIGNATURE_FLAP_N"):
        monkeypatch.delenv(v, raising=False)
    kb.init_db()
    return home


def _mint(conn, n=0, sig=SIG, by="main-tip-red", **kw):
    return kb.create_task(conn, title="main-tip red: lint the tree #%d" % n, created_by=by, signature=sig,
                          idempotency_key="%s:%012x" % (sig, n), **kw)


def _close(conn, tid, *, ago_s=0, payload=None):
    """Mark a card done with a completed event dated ``ago_s`` seconds in the past."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "completed", payload or {"result_len": 0, "summary": "already fixed on main"})
        conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? AND kind = 'completed'",
                     (ago_s, tid))


def test_open_card_owns_its_signature(kanban_home):
    """Two baselines of one red: the second mint returns the first card and ledgers the re-fire."""
    with kb.connect() as conn:
        a = _mint(conn, 1)
        b = _mint(conn, 2)
        assert b == a
        kinds = [e.kind for e in kb.list_events(conn, a)]
        assert "signature_remint" in kinds
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE signature = ?", (SIG,)).fetchone()[0] == 1


def test_closed_within_quiet_window_returns_existing(kanban_home):
    """The chrome-launch-helper shape: fix lands, card closes, an older baseline re-fires -> no new card."""
    with kb.connect() as conn:
        a = _mint(conn, 1)
        _close(conn, a, ago_s=3600)
        assert _mint(conn, 2) == a


def test_past_quiet_window_mints_a_new_card(kanban_home):
    with kb.connect() as conn:
        a = _mint(conn, 1)
        _close(conn, a, ago_s=kb.SIGNATURE_QUIET_S + 60)
        b = _mint(conn, 2)
        assert b != a


def test_flap_breaker_after_n_no_code_closes(kanban_home):
    with kb.connect() as conn:
        ids = []
        for i in range(kb.SIGNATURE_FLAP_N):
            t = _mint(conn, i)
            ids.append(t)
            _close(conn, t, ago_s=kb.SIGNATURE_QUIET_S + 60)
        with pytest.raises(kb.MintBreakerError) as ei:
            _mint(conn, 99)
        assert ei.value.signature == SIG
        assert set(ei.value.cards) == set(ids)
        assert "HERMES_KANBAN_MINT_BREAKER=0" in str(ei.value)


@pytest.mark.parametrize("payload", [
    {"survivor": {"kind": "landed", "landed": [{"pr": "x"}]}},
    {"survivor": {"kind": "patch", "path": "p", "sha256": "s", "bytes": 1}},
    {"superseded_by": "https://github.com/ANG-Ventures/hermes-home/pull/9051"},
])
def test_a_close_that_carried_code_does_not_count_toward_the_breaker(kanban_home, payload):
    """I4: evidence, not prose. A close that SAYS 'already fixed' but carries a PR/patch is a real fix."""
    with kb.connect() as conn:
        for i in range(kb.SIGNATURE_FLAP_N):
            t = _mint(conn, i)
            _close(conn, t, ago_s=kb.SIGNATURE_QUIET_S + 60,
                   payload=dict(payload, summary="already fixed on main") if i == 0 else None)
        assert _mint(conn, 99)   # mints, no breaker


def test_force_overrides_breaker_and_is_ledgered(kanban_home):
    with kb.connect() as conn:
        for i in range(kb.SIGNATURE_FLAP_N):
            _close(conn, _mint(conn, i), ago_s=kb.SIGNATURE_QUIET_S + 60)
        t = _mint(conn, 99, force_reason="operator: regression is real this time")
        ev = [e.payload for e in kb.list_events(conn, t) if e.kind == "mint_breaker_forced"]
        assert len(ev) == 1 and ev[0]["signature"] == SIG


@pytest.mark.parametrize("by", [None, "", "user", "human:apollo"])
def test_humans_bypass_the_resolver(kanban_home, by):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="h1", created_by=by, signature=SIG)
        b = kb.create_task(conn, title="h2", created_by=by, signature=SIG)
        assert a != b


def test_no_signature_is_todays_behaviour(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="x1", created_by="main-tip-red")
        b = kb.create_task(conn, title="x2", created_by="main-tip-red")
        assert a != b


def test_dedup_kill_switch(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_SIGNATURE_DEDUP", "0")
    with kb.connect() as conn:
        assert _mint(conn, 1) != _mint(conn, 2)


def test_breaker_kill_switch_keeps_dedup(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_BREAKER", "0")
    with kb.connect() as conn:
        for i in range(kb.SIGNATURE_FLAP_N):
            _close(conn, _mint(conn, i), ago_s=kb.SIGNATURE_QUIET_S + 60)
        t = _mint(conn, 99)        # breaker off -> mints
        assert _mint(conn, 100) == t   # dedup still on -> reuses the open card


def test_signature_column_is_stored_and_migrates(kanban_home):
    with kb.connect() as conn:
        t = _mint(conn, 1)
        assert conn.execute("SELECT signature FROM tasks WHERE id = ?", (t,)).fetchone()[0] == SIG
        idx = {r[1] for r in conn.execute("PRAGMA index_list(tasks)")}
        assert "idx_tasks_signature_created" in idx
    kb.init_db()   # re-running the migration is a no-op


def test_no_reuse_mints_fresh_but_keeps_the_signature_and_the_breaker(kanban_home):
    """--no-signature-reuse (a caller-proven regression): a fresh card even inside the quiet window, the card still
    stores the signature, so its no-code close counts toward the breaker (Prism vh#9319 @59f4f7b7 P1 repro:
    signed A closed, fresh B and C closed inside A's window, D a day later -> refused, not a 4th card)."""
    with kb.connect() as conn:
        a = _mint(conn, 1)
        _close(conn, a, ago_s=27 * 3600)
        b = _mint(conn, 2, signature_reuse=False)
        assert b != a
        _close(conn, b, ago_s=26 * 3600)
        c = _mint(conn, 3, signature_reuse=False)
        assert c not in (a, b)
        _close(conn, c, ago_s=25 * 3600)
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE signature = ?", (SIG,)).fetchone()[0] == 3
        with pytest.raises(kb.MintBreakerError):
            _mint(conn, 4)
        with pytest.raises(kb.MintBreakerError):
            _mint(conn, 5, signature_reuse=False)


def test_no_reuse_with_an_open_card_still_mints_fresh(kanban_home):
    with kb.connect() as conn:
        a = _mint(conn, 1)
        assert _mint(conn, 2, signature_reuse=False) != a


def test_cli_no_signature_reuse_flag_parses():
    import argparse  # noqa: PLC0415
    from hermes_cli.kanban_parser import build_parser  # noqa: PLC0415
    p = argparse.ArgumentParser()
    build_parser(p.add_subparsers(dest="cmd"))
    ns = p.parse_args(["kanban", "create", "t", "--signature", "s", "--no-signature-reuse"])
    assert ns.signature_reuse is False
    assert p.parse_args(["kanban", "create", "t", "--signature", "s"]).signature_reuse is True
