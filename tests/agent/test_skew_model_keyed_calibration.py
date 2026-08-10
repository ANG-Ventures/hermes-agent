"""Skew calibration persists and seeds keyed by (provider, model).

GAP 2+4. The learned ratio used to die with the session: ``_persist_skew_history``
wrote only to ``record_compression_skew_history(session_id, ...)``, so every NEW
session started at skew=1.0 (raw rough) — exactly when it has no readings of its
own and needs a prior the most.

Two things are wrong with session-keying:

1. **Scope.** A session row is worthless to the next session.
2. **Attribution.** The skew ratio measures a TOKENIZER. A ratio learned on
   claude-opus-5 is not valid for gpt-5.6-sol, so the durable key must carry the
   model, and an UNSEEN model must start uncalibrated rather than inherit.

The tests below are deliberately weighted toward the two failure modes that have
actually shipped in this area:

* **WIRING** — #506 shipped a fix that the production path never called. So it is
  not enough that ``record_model_skew_history`` works; the real
  ``record_skew_from_real`` → ``_persist_skew_history`` path must invoke it.
* **ROUND TRIP THROUGH STORAGE** — #529 shipped inert because persist wrote
  ratios > 1.0 and the DB READER hard-filtered to ``<= 1.0``. Its round-trip test
  passed a Python list straight from persist to seed, never crossing SQLite, so
  the discarding layer sat exactly in the gap the test skipped. Every round-trip
  assertion here goes through a real ``SessionDB``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import agent.context_engine as ce_mod
from agent.context_engine import _SKEW_SCALE_UP_MAX, ContextEngine
from agent.context_compressor import ContextCompressor


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _scale_up_on(monkeypatch):
    """Pin the wide accept band (the shipped default) unless a test opts out."""
    monkeypatch.setattr(ce_mod, "_scale_up_calibration_enabled", lambda: True)
    yield


def _db(tmp_path, name="state.db"):
    from hermes_state import SessionDB

    return SessionDB(db_path=tmp_path / name)


def make_compressor(session_db=None, session_id="", model="claude-opus-5",
                    provider="anthropic"):
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=1_000_000
    ):
        c = ContextCompressor(
            model=model,
            provider=provider,
            threshold_percent=0.75,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        # Force lazy context-length resolution while the patch is live.
        _ = c.context_length
        _ = c.threshold_tokens
    if session_db is not None:
        c.bind_session_state(session_db=session_db, session_id=session_id)
    return c


def record_pair(c, rough, real):
    c.note_rough_sent(rough)
    c.record_skew_from_real(real)


class _Engine(ContextEngine):
    """Minimal concrete engine — exercises the base-class calibration methods."""

    @property
    def name(self) -> str:
        return "test-engine"

    def update_from_response(self, *a, **k):  # pragma: no cover - not used
        pass

    def should_compress(self, *a, **k):  # pragma: no cover - not used
        return False

    def compress(self, *a, **k):  # pragma: no cover - not used
        return []


def _engine(model="claude-opus-5", provider="anthropic", session_db=None,
            session_id=""):
    e = object.__new__(_Engine)
    e._recent_skews = []
    e._last_rough_sent = 0
    e.model = model
    e.provider = provider
    e._session_db = session_db
    e._session_id = session_id
    return e


# ───────────────────────────────────────────────────────── WIRING ────────────
# The production path must call the new keyed writer. A standalone writer that
# nothing invokes is what #506 shipped.


class TestProductionPathWiring:
    def test_record_skew_from_real_invokes_the_model_keyed_writer(self):
        """WIRING: the real turn path, not a hand-called helper."""
        seen = []

        class SpyDB:
            def record_model_skew_history(self, provider, model, history):
                seen.append((provider, model, list(history)))

            def record_compression_skew_history(self, session_id, history):
                pass

        e = _engine(session_db=SpyDB(), session_id="s1")
        # The real production entry point: a provider response pairs with the
        # stashed rough estimate.
        e.note_rough_sent(100_000)
        e.record_skew_from_real(138_000)

        assert seen, (
            "record_skew_from_real -> _persist_skew_history must reach "
            "record_model_skew_history; a keyed writer nothing calls is inert"
        )
        provider, model, history = seen[-1]
        assert (provider, model) == ("anthropic", "claude-opus-5")
        assert history == e._recent_skews

    def test_compressor_bind_invokes_the_model_keyed_reader(self, tmp_path):
        """WIRING: the real bind path must consult the keyed store."""
        reads = []

        class SpyDB:
            def get_compression_skew_history(self, session_id):
                return []

            def get_model_skew_history(self, provider, model):
                reads.append((provider, model))
                return []

            def get_compression_failure_cooldown(self, session_id):
                return None

        make_compressor(SpyDB(), "fresh-session")
        assert ("anthropic", "claude-opus-5") in reads, (
            "bind_session_state must seed from the (provider, model) store; "
            "otherwise a fresh session can never inherit a learned prior"
        )

    def test_persist_writes_both_stores(self):
        """The session row stays (same-session restart); the keyed row is new."""
        calls = {"model": 0, "session": 0}

        class SpyDB:
            def record_model_skew_history(self, provider, model, history):
                calls["model"] += 1

            def record_compression_skew_history(self, session_id, history):
                calls["session"] += 1

        e = _engine(session_db=SpyDB(), session_id="s1")
        e.note_rough_sent(100_000)
        e.record_skew_from_real(120_000)
        assert calls == {"model": 1, "session": 1}


# ─────────────────────────────────────────── ROUND TRIP THROUGH REAL STORAGE ──
# What persist WRITES must be what seed ACCEPTS. #529 broke exactly here, and
# its round-trip test missed it by never crossing SQLite.


class TestRoundTripThroughRealDB:
    def test_scale_up_ratios_survive_the_real_db_round_trip(self, tmp_path):
        """The #529 class: ratios > 1.0 must not be eaten by the storage layer."""
        db = _db(tmp_path)
        src = _engine(session_db=db, session_id="s1")
        for rough, real in ((100_000, 138_000), (100_000, 122_000)):
            src.note_rough_sent(rough)
            src.record_skew_from_real(real)
        written = list(src._recent_skews)
        assert written and max(written) > 1.0, "fixture must exercise scale-up"

        read_back = db.get_model_skew_history("anthropic", "claude-opus-5")
        assert read_back == pytest.approx(written), (
            "the DB reader must not discard the scale-up ratios the recorder "
            "writes; that mismatch is what made #529 ship inert"
        )

        dst = _engine(session_db=db)
        assert dst.seed_skew_calibration_for_model() is True
        assert dst._recent_skews == pytest.approx(written), (
            "persist and seed are two halves of one contract"
        )

    def test_fresh_session_on_a_seen_model_starts_calibrated(self, tmp_path):
        """The headline deliverable, end to end through ContextCompressor."""
        db = _db(tmp_path)
        db.create_session("sess-a", source="test")
        c1 = make_compressor(db, "sess-a")
        record_pair(c1, 767_000, 476_000)
        learned = c1._current_skew()
        assert learned < 1.0

        # A brand-new SESSION (different id, no session row) on the SAME model.
        db.create_session("sess-b", source="test")
        c2 = make_compressor(db, "sess-b")
        assert c2._recent_skews, (
            "a fresh session on a model we have measured must start calibrated"
        )
        assert c2._current_skew() == pytest.approx(learned)

    def test_session_keyed_reader_no_longer_eats_scale_up_ratios(self, tmp_path):
        """Regression on the sibling store the same #529 bug also broke."""
        db = _db(tmp_path)
        db.create_session("s1", source="test")
        db.record_compression_skew_history("s1", [1.38, 1.22, 1.45])
        assert db.get_compression_skew_history("s1") == pytest.approx(
            [1.38, 1.22, 1.45]
        ), "the session-keyed reader hard-filtered to <= 1.0 and dropped everything"


# ──────────────────────────────────────────────── NEGATIVE CONTROL ───────────


class TestUnseenModelDoesNotInherit:
    def test_unseen_model_starts_uncalibrated(self, tmp_path):
        """An unseen (provider, model) must NOT inherit another model's ratio."""
        db = _db(tmp_path)
        db.create_session("sess-a", source="test")
        trained = make_compressor(
            db, "sess-a", model="claude-opus-5", provider="anthropic"
        )
        record_pair(trained, 767_000, 476_000)
        assert trained._current_skew() < 1.0

        db.create_session("sess-b", source="test")
        fresh = make_compressor(
            db, "sess-b", model="gpt-5.6-sol", provider="openai"
        )
        assert fresh._recent_skews == [], (
            "a model we have never measured must start uncalibrated; "
            "tokenizers differ, so another model's ratio is a WRONG prior"
        )
        assert fresh._current_skew() == 1.0

    def test_same_model_different_provider_does_not_inherit(self, tmp_path):
        """The key is the PAIR — a model served elsewhere may tokenize the same
        but bills/counts through a different stack, so it is a distinct key."""
        db = _db(tmp_path)
        e1 = _engine(session_db=db, provider="anthropic", model="claude-opus-5")
        e1.note_rough_sent(100_000)
        e1.record_skew_from_real(60_000)
        assert e1._recent_skews

        e2 = _engine(session_db=db, provider="bedrock", model="claude-opus-5")
        assert e2.seed_skew_calibration_for_model() is False
        assert e2._recent_skews == []

    def test_unknown_model_is_not_persisted_under_a_guessed_key(self, tmp_path):
        """No model → no attributable key → nothing written (never guess)."""
        db = _db(tmp_path)
        e = _engine(session_db=db, model="", provider="anthropic")
        e.note_rough_sent(100_000)
        e.record_skew_from_real(60_000)
        assert e._recent_skews, "the in-memory reading still happens"
        assert db.get_model_skew_history("anthropic", "") == []


# ──────────────────────────────────────────────── ACCEPT BAND (#529) ─────────


class TestSeedingHonorsTheScaleUpBand:
    def test_seed_accepts_up_to_the_scale_up_ceiling(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [_SKEW_SCALE_UP_MAX])
        e = _engine(session_db=db, provider="p", model="m")
        assert e.seed_skew_calibration_for_model() is True
        assert e._recent_skews == [_SKEW_SCALE_UP_MAX]

    def test_seed_rejects_absurd_values(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [_SKEW_SCALE_UP_MAX * 10])
        e = _engine(session_db=db, provider="p", model="m")
        assert e.seed_skew_calibration_for_model() is False
        assert e._recent_skews == []

    def test_scale_up_disabled_falls_back_to_the_1_0_ceiling(
        self, tmp_path, monkeypatch
    ):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [1.38, 0.88])
        monkeypatch.setattr(ce_mod, "_scale_up_calibration_enabled", lambda: False)
        e = _engine(session_db=db, provider="p", model="m")
        assert e.seed_skew_calibration_for_model() is True
        assert e._recent_skews == [0.88], (
            "with the kill switch off the ceiling is 1.0, so the 1.38 reading "
            "is rejected while the sane sub-1.0 reading is kept"
        )

    def test_history_is_capped_to_the_window(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history(
            "p", "m", [1.1] * (ContextEngine._SKEW_HISTORY + 25)
        )
        e = _engine(session_db=db, provider="p", model="m")
        e.seed_skew_calibration_for_model()
        assert len(e._recent_skews) == ContextEngine._SKEW_HISTORY


# ──────────────────────────────────────────────── LIVE HISTORY WINS ──────────


class TestLiveHistoryIsNotClobbered:
    def test_seed_is_a_noop_when_a_live_history_exists(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [1.5, 1.5])
        e = _engine(session_db=db, provider="p", model="m")
        e._recent_skews = [0.8]
        assert e.seed_skew_calibration_for_model() is False
        assert e._recent_skews == [0.8], (
            "a live in-memory reading is fresher than any persisted snapshot"
        )

    def test_bind_does_not_clobber_a_live_history(self, tmp_path):
        db = _db(tmp_path)
        db.create_session("s1", source="test")
        c = make_compressor(db, "s1")
        record_pair(c, 100_000, 60_000)
        live = list(c._recent_skews)
        c.bind_session_state(session_db=db, session_id="s1")
        assert c._recent_skews == pytest.approx(live)


# ──────────────────────────────────────────────── FAIL-SAFE ─────────────────


class TestPersistenceIsFailSafe:
    def test_keyed_writer_exception_does_not_reach_the_turn(self):
        class ExplodingDB:
            def record_model_skew_history(self, *a, **k):
                raise RuntimeError("disk on fire")

            def record_compression_skew_history(self, *a, **k):
                pass

        e = _engine(session_db=ExplodingDB(), session_id="s1")
        e.note_rough_sent(100_000)
        e.record_skew_from_real(60_000)  # must not raise
        assert e._recent_skews, "the in-memory calibration still updated"

    def test_keyed_writer_failure_does_not_block_the_session_writer(self):
        """One store failing must not take the other down with it."""
        wrote = []

        class HalfBrokenDB:
            def record_model_skew_history(self, *a, **k):
                raise RuntimeError("boom")

            def record_compression_skew_history(self, session_id, history):
                wrote.append(session_id)

        e = _engine(session_db=HalfBrokenDB(), session_id="s1")
        e.note_rough_sent(100_000)
        e.record_skew_from_real(60_000)
        assert wrote == ["s1"]

    def test_seed_exception_does_not_reach_bind(self, tmp_path):
        class ExplodingDB:
            def get_compression_skew_history(self, session_id):
                return []

            def get_model_skew_history(self, *a, **k):
                raise RuntimeError("read failed")

            def get_compression_failure_cooldown(self, session_id):
                return None

        c = make_compressor(ExplodingDB(), "s1")  # must not raise
        assert c._recent_skews == []

    def test_db_without_the_keyed_methods_still_works(self):
        """Back-compat: a duck-typed store predating this feature."""
        class LegacyDB:
            def record_compression_skew_history(self, session_id, history):
                pass

            def get_compression_skew_history(self, session_id):
                return []

            def get_compression_failure_cooldown(self, session_id):
                return None

        e = _engine(session_db=LegacyDB(), session_id="s1")
        e.note_rough_sent(100_000)
        e.record_skew_from_real(60_000)
        assert e._recent_skews
        assert e.seed_skew_calibration_for_model() is False


# ──────────────────────────────────────────────── BACK-COMPAT ───────────────


class TestLegacySessionKeyedRowsAreIgnoredNotMigrated:
    def test_preexisting_session_row_still_reads_cleanly(self, tmp_path):
        """A DB written before this change must not crash the reader.

        Chosen disposition: IGNORE, not migrate. A legacy
        ``sessions.compression_skew_history`` row carries no provider/model
        attribution, and ``sessions.model`` records the model the session
        ENDED on — which is not necessarily the model the ratios were measured
        under (a mid-session fallback rewrites it). Migrating would therefore
        manufacture exactly the wrong-prior contamination this change exists to
        prevent. The legacy row keeps serving its original narrower job
        (same-session restart resume) and simply never seeds the keyed store;
        the keyed store fills naturally on the first real reading.
        """
        db = _db(tmp_path)
        db.create_session("legacy", source="test")
        db.record_compression_skew_history("legacy", [0.62, 0.66])

        # The legacy row is readable and still serves same-session resume.
        assert db.get_compression_skew_history("legacy") == pytest.approx(
            [0.62, 0.66]
        )
        # ...and it did NOT leak into the model-keyed store.
        assert db.get_model_skew_history("anthropic", "claude-opus-5") == []

        c = make_compressor(db, "legacy")
        assert c._recent_skews == pytest.approx([0.62, 0.66])

    def test_keyed_store_survives_a_reopen(self, tmp_path):
        """Real durability: close the DB, reopen it, calibration is still there."""
        db = _db(tmp_path)
        db.record_model_skew_history("anthropic", "claude-opus-5", [0.71, 0.69])
        db.close()

        reopened = _db(tmp_path)
        assert reopened.get_model_skew_history(
            "anthropic", "claude-opus-5"
        ) == pytest.approx([0.71, 0.69])

    def test_corrupt_row_is_ignored_not_fatal(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [0.9])
        db._conn.execute(
            "UPDATE compression_skew_calibration SET skew_history = ? "
            "WHERE provider = ? AND model = ?",
            ("{not json", "p", "m"),
        )
        db._conn.commit()
        assert db.get_model_skew_history("p", "m") == []

    def test_upsert_replaces_rather_than_appends(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m", [0.9])
        db.record_model_skew_history("p", "m", [0.7, 0.75])
        assert db.get_model_skew_history("p", "m") == pytest.approx([0.7, 0.75])

    def test_clear_removes_only_the_named_pair(self, tmp_path):
        db = _db(tmp_path)
        db.record_model_skew_history("p", "m1", [0.9])
        db.record_model_skew_history("p", "m2", [0.8])
        db.clear_model_skew_history("p", "m1")
        assert db.get_model_skew_history("p", "m1") == []
        assert db.get_model_skew_history("p", "m2") == pytest.approx([0.8])


# ──────────────────────────────────────────────── SESSION BOUNDARY ──────────


class TestSessionBoundaryDoesNotWipeTheDurableCalibration:
    def test_session_reset_keeps_the_model_keyed_row(self, tmp_path):
        """Greptile #111 clears per-CONVERSATION state; the model-keyed
        calibration is per-TOKENIZER and must outlive the boundary — that is
        the entire point of this change."""
        db = _db(tmp_path)
        db.create_session("s1", source="test")
        c = make_compressor(db, "s1")
        record_pair(c, 767_000, 476_000)
        assert db.get_model_skew_history("anthropic", "claude-opus-5")

        c.on_session_reset()
        assert db.get_compression_skew_history("s1") == [], (
            "the per-session row is still cleared at a real boundary"
        )
        assert db.get_model_skew_history("anthropic", "claude-opus-5"), (
            "the durable (provider, model) calibration must SURVIVE the "
            "session boundary"
        )
