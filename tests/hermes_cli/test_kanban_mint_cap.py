"""The per-creator rolling-24 h mint cap is GONE (Ace 2026-11-09, msg 1569346219715592214: "kill it, get rid of it").

PR #65 refused the 25th card an automated ``created_by`` minted in 24 h. That cap is removed, not raised and not
exempted: no creator is refused for volume and no env var brings it back. What still holds machine minting are the
independent controls, pinned here so the removal cannot take them with it: idempotency keys, the signature
resolver/flap breaker, the near-duplicate refusal, and the explicit mint moratorium (with its ledger, whose rows of
a forced card are written only when the outer transaction commits).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for v in ("HERMES_KANBAN_SIGNATURE_DEDUP", "HERMES_KANBAN_MINT_BREAKER"):
        monkeypatch.delenv(v, raising=False)
    kb.init_db()
    return home


def _mint(conn, by, n, prefix="main-tip red: repo · lint"):
    return [kb.create_task(conn, title="%s %d" % (prefix, i), created_by=by) for i in range(n)]


def _ledger(conn):
    p = kb.mint_cap_ledger_path(conn)
    if p is None or not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _moratorium_on():
    flag = kb.mint_moratorium_flag()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    return flag


def test_same_automated_creator_mints_well_past_24_in_24h(kanban_home):
    """The ruled number was 24; 60 distinct cards from one automated creator inside the window all mint."""
    with kb.connect() as conn:
        ids = _mint(conn, "landing-queue", 60)
        assert len(set(ids)) == 60
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by='landing-queue'").fetchone()[0]
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM task_events")}
        assert _ledger(conn) == []
    assert n == 60
    assert not kinds & {"mint_cap_forced", "mint_cap_bypassed"}


@pytest.mark.parametrize("env", ["1", "3", "24"])
def test_the_old_env_var_cannot_reinstate_a_cap(kanban_home, monkeypatch, env):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", env)
    with kb.connect() as conn:
        assert len(set(_mint(conn, "main-tip-red", 30))) == 30


def test_no_default_cap_path_remains():
    for name in ("MINT_CAP_PER_DAY", "MINT_CAP_WINDOW_S", "mint_cap", "mint_count_24h", "mint_cap_bypass"):
        assert not hasattr(kb, name), name
    src = Path(kb.__file__).read_text()
    assert "HERMES_KANBAN_MINT_CAP" not in src.replace('``HERMES_KANBAN_MINT_CAP``', "")


def test_compat_shims_are_inert(kanban_home):
    """Callers that still import MintCapError / call check_mint_cap keep working; nothing raises."""
    assert issubclass(kb.MintCapError, ValueError)
    with kb.connect() as conn:
        _mint(conn, "ci-red-owner", 30)
        assert kb.check_mint_cap(conn, "ci-red-owner") is None


def test_idempotency_key_still_returns_the_existing_card(kanban_home):
    with kb.connect() as conn:
        _mint(conn, "ci-red-owner", 30)
        first = kb.create_task(conn, title="CI starved: repo", created_by="ci-red-owner", idempotency_key="k:repo")
        again = kb.create_task(conn, title="CI starved: repo", created_by="ci-red-owner", idempotency_key="k:repo")
    assert again == first


def test_signature_dedup_still_returns_the_owning_card_past_24(kanban_home):
    with kb.connect() as conn:
        _mint(conn, "main-tip-red", 30)
        a = kb.create_task(conn, title="main-tip red: lint X", created_by="main-tip-red", signature="sig:lint-x")
        b = kb.create_task(conn, title="main-tip red: lint X again", created_by="main-tip-red",
                           signature="sig:lint-x")
    assert a == b


def test_near_duplicate_refusal_still_applies(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="pager owner: disk full on studio", created_by="pager-owner-card")
        with pytest.raises(kb.NearDuplicateError):
            kb.create_task(conn, title="pager owner: disk full on studio", created_by="pager-owner-card",
                           duplicate_guard=True)


def test_moratorium_still_refuses_automated_creators_and_ledgers(kanban_home):
    _moratorium_on()
    with kb.connect() as conn:
        with pytest.raises(kb.MintMoratoriumError):
            kb.create_task(conn, title="new finding", created_by="main-tip-red")
        assert [r["outcome"] for r in _ledger(conn)] == ["moratorium_refused"]
        # Humans are unaffected.
        assert kb.create_task(conn, title="human card", created_by="user")


def test_a_rolled_back_outer_transaction_ledgers_no_forced_row(kanban_home):
    """Prism 31f2934ed515 (kept from the cap's ledger tests): a forced create nested in a caller's write_txn only
    releases a savepoint; when the OUTER transaction rolls back no ledger row may exist for the card."""
    _moratorium_on()
    with kb.connect() as conn:
        with pytest.raises(RuntimeError):
            with kb.write_txn(conn):
                kb.create_task(conn, title="b", created_by="main-tip-red", force_reason="operator: needed now")
                raise RuntimeError("swarm construction failed")
        assert _ledger(conn) == []
        assert conn.execute("SELECT count(*) FROM tasks WHERE title='b'").fetchone()[0] == 0
        with kb.write_txn(conn):
            t = kb.create_task(conn, title="c", created_by="main-tip-red", force_reason="operator: needed now")
            assert _ledger(conn) == [], "ledgered before the outer commit"
        rows = _ledger(conn)
    assert [(r["outcome"], r["task_id"]) for r in rows] == [("moratorium_forced", t)]


def test_concurrent_same_creator_admission_all_succeed(kanban_home):
    """Four connections minting for one creator at once: every create lands, ids are unique, no cap refusal."""
    errors: list[BaseException] = []
    ids: list[str] = []
    lock = threading.Lock()

    def worker(w):
        try:
            with kb.connect() as conn:
                got = [kb.create_task(conn, title="conc %d-%d" % (w, i), created_by="landing-queue")
                       for i in range(10)]
            with lock:
                ids.extend(got)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(set(ids)) == 40
