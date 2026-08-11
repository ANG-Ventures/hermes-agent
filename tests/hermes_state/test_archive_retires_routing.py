"""Archiving a session must RETIRE its gateway routing entry.

Before this, ``set_session_archived`` issued exactly one statement — it flipped
``archived`` and nothing else, leaving ``end_reason IS NULL``. Every path that
can evict an in-memory ``SessionStore._entries`` key gates on the DB row being
*ended*:

* the startup stale prune (``_prune_stale_sessions_locked``) reads
  ``row["end_reason"]``;
* the routing-time self-heal (#54878) calls ``_is_session_ended_in_db``;
* ``prune_old_entries`` ages on ``updated_at``, which every full persist
  re-touches.

So an archived session's routing key was unreachable by all of them and was
rewritten to ``gateway_routing`` from the live index forever. These tests drive
the REAL ``SessionStore`` + ``SessionDB`` and read the durable table, not the
in-memory dict — an immediate re-read after the call is the exact false green
this bug already produced once.
"""

from __future__ import annotations

import json
import time

import pytest

import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source(user_id: str = "user-A", chat_id: str = "chat-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name="Chat",
        chat_type="group",
        user_id=user_id,
    )


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Build SessionStores that share one hermetic state.db + sessions dir."""
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    created: list[SessionStore] = []

    def _make() -> SessionStore:
        store = SessionStore(
            sessions_dir=tmp_path / "sessions", config=GatewayConfig()
        )
        created.append(store)
        return store

    yield _make

    for store in created:
        if store._db is not None:
            store._db.close()


def _durable_routing_keys(store: SessionStore) -> set[str]:
    """Routing keys as they exist in state.db — not the in-memory index."""
    return set(
        store._db.load_gateway_routing_entries(scope=store._routing_scope())
    )


class TestArchiveRetiresRouting:
    def test_archived_key_is_gone_after_a_full_persist_and_a_restart(
        self, store_factory
    ):
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store.persist()
        assert entry.session_key in _durable_routing_keys(store)

        store._db.set_session_archived(entry.session_id, True)

        # Not the immediate re-read: a full persist rewrites gateway_routing
        # from the live in-memory index, which is what resurrected the row.
        store.persist()
        store._db.close()

        restarted = store_factory()
        restarted._ensure_loaded()
        restarted.persist()

        assert entry.session_key not in _durable_routing_keys(restarted)
        assert entry.session_key not in {
            e.session_key for e in restarted.entries()
        }

    def test_sibling_key_for_the_same_chat_is_untouched(self, store_factory):
        """Per-user keys share a chat; archiving one must not take the other."""
        store = store_factory()
        archived = store.get_or_create_session(_source("user-A"))
        sibling = store.get_or_create_session(_source("user-B"))
        store.persist()

        store._db.set_session_archived(archived.session_id, True)
        store.persist()
        store._db.close()

        restarted = store_factory()
        restarted._ensure_loaded()
        restarted.persist()

        keys = _durable_routing_keys(restarted)
        assert sibling.session_key in keys
        assert archived.session_key not in keys

    def test_live_unarchived_session_keeps_its_routing_entry(self, store_factory):
        """Negative control: archiving A must not disturb an unrelated live B."""
        store = store_factory()
        archived = store.get_or_create_session(_source("user-A"))
        live = store.get_or_create_session(_source("user-C", chat_id="chat-2"))
        store.persist()

        store._db.set_session_archived(archived.session_id, True)
        store.persist()
        store._db.close()

        restarted = store_factory()
        restarted._ensure_loaded()
        restarted.persist()

        assert live.session_key in _durable_routing_keys(restarted)
        assert restarted.get_or_create_session(
            _source("user-C", chat_id="chat-2")
        ).session_id == live.session_id

    def test_no_archive_leaves_every_key_in_place(self, store_factory):
        """The eviction must be caused by the archive, not by a restart."""
        store = store_factory()
        a = store.get_or_create_session(_source("user-A"))
        b = store.get_or_create_session(_source("user-B"))
        store.persist()
        store._db.close()

        restarted = store_factory()
        restarted._ensure_loaded()
        restarted.persist()

        assert {a.session_key, b.session_key} <= _durable_routing_keys(restarted)


class TestArchivedSessionDoesNotSilentlyReopen:
    def test_next_message_starts_a_fresh_session(self, store_factory):
        """``archived`` must NOT be in the recovery finder's reopenable set.

        The self-heal reopens ``agent_close`` / ``ws_orphan_reap`` rows,
        preserving their transcript. An archived conversation is an explicit
        user boundary — resurrecting it would undo the archive.
        """
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store._db.append_message(entry.session_id, "user", "hello")
        store.persist()

        store._db.set_session_archived(entry.session_id, True)

        resumed = store.get_or_create_session(_source())

        assert resumed.session_id != entry.session_id
        row = store._db.get_session(entry.session_id)
        assert row["archived"] == 1
        assert row["end_reason"] == store._db.ARCHIVE_END_REASON

    def test_live_gateway_rebinds_the_key_to_the_new_session(self, store_factory):
        """A live gateway rewrites the key — to the FRESH session, not the archived one.

        The workaround card's finding still holds: with the gateway up, the
        in-memory index re-persists the key on the next full save. What changed
        is that the routing-time self-heal now fires, so the key that comes back
        points at a new session and the archived one is unreachable.
        """
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store.persist()

        store._db.set_session_archived(entry.session_id, True)
        store.persist()  # live gateway re-writes from _entries

        rebound = store.get_or_create_session(_source())
        store.persist()

        row_json = store._db.load_gateway_routing_entries(
            scope=store._routing_scope()
        )[entry.session_key]
        assert json.loads(row_json)["session_id"] == rebound.session_id
        assert rebound.session_id != entry.session_id


class TestUnarchive:
    def test_unarchive_reverses_only_what_archive_wrote(self, store_factory):
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store.persist()

        store._db.set_session_archived(entry.session_id, True)
        store._db.set_session_archived(entry.session_id, False)

        row = store._db.get_session(entry.session_id)
        assert row["archived"] == 0
        assert row["end_reason"] is None
        assert row["ended_at"] is None

    def test_unarchive_does_not_resurrect_a_routing_entry(self, store_factory):
        """The route is rebuilt by the next message, not by the flag flip."""
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store.persist()

        store._db.set_session_archived(entry.session_id, True)
        store._db.close()

        offline = store_factory()  # no live index to re-persist the key
        offline._db.set_session_archived(entry.session_id, False)

        assert entry.session_key not in _durable_routing_keys(offline)

    def test_unarchive_preserves_a_real_end_reason(self, store_factory):
        """A session ended for a real reason stays ended through archive+unarchive."""
        store = store_factory()
        entry = store.get_or_create_session(_source())
        store._db.end_session(entry.session_id, "session_reset")
        ended_at = store._db.get_session(entry.session_id)["ended_at"]

        store._db.set_session_archived(entry.session_id, True)
        store._db.set_session_archived(entry.session_id, False)

        row = store._db.get_session(entry.session_id)
        assert row["archived"] == 0
        assert row["end_reason"] == "session_reset"
        assert row["ended_at"] == ended_at


class TestCompressionLineage:
    """The lineage is the archive unit; the routing sweep must follow it."""

    def _lineage(self, db):
        base = time.time() - 100
        db.create_session("root", source="telegram", session_key="k-root")
        db.create_session(
            "tip", source="telegram", parent_session_id="root", session_key="k-tip"
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ?, "
            "end_reason = 'compression', message_count = 1 WHERE id = 'root'",
            (base, base + 10),
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, message_count = 1 WHERE id = 'tip'",
            (base + 20,),
        )
        db._conn.commit()

    def test_archiving_the_tip_retires_the_whole_lineage_routing(
        self, store_factory
    ):
        store = store_factory()
        db = store._db
        self._lineage(db)
        scope = store._routing_scope()
        db.save_gateway_routing_entry(
            "k-root", json.dumps({"session_id": "root"}), scope=scope
        )
        db.save_gateway_routing_entry(
            "k-tip", json.dumps({"session_id": "tip"}), scope=scope
        )
        db.save_gateway_routing_entry(
            "k-other", json.dumps({"session_id": "elsewhere"}), scope=scope
        )

        assert db.set_session_archived("tip", True) is True

        assert set(db.load_gateway_routing_entries(scope=scope)) == {"k-other"}

    def test_compression_end_reason_survives_archiving(self, store_factory):
        """``end_reason='compression'`` is a lineage EDGE — overwriting it would
        break the recursive CTEs that define archive/pin/recency scope."""
        db = store_factory()._db
        self._lineage(db)

        db.set_session_archived("tip", True)

        assert db.get_session("root")["end_reason"] == "compression"
        assert db.get_session("root")["archived"] == 1
        assert db.get_session("tip")["end_reason"] == db.ARCHIVE_END_REASON
        assert db.get_session("tip")["archived"] == 1

    def test_unarchiving_the_tip_keeps_the_compression_edge(self, store_factory):
        db = store_factory()._db
        self._lineage(db)
        db.set_session_archived("tip", True)

        db.set_session_archived("tip", False)

        assert db.get_session("root")["end_reason"] == "compression"
        assert db.get_session("root")["archived"] == 0
        assert db.get_session("tip")["end_reason"] is None
        assert db.get_session("tip")["archived"] == 0


class TestRoutingDeleteRobustness:
    def test_a_corrupt_routing_row_does_not_fail_the_archive(self, store_factory):
        """One unparseable entry_json must not take the whole write down."""
        store = store_factory()
        db = store._db
        entry = store.get_or_create_session(_source())
        store.persist()
        scope = store._routing_scope()
        db.save_gateway_routing_entry("k-corrupt", "not json at all", scope=scope)

        assert db.set_session_archived(entry.session_id, True) is True

        remaining = db.load_gateway_routing_entries(scope=scope)
        assert entry.session_key not in remaining
        assert "k-corrupt" in remaining

    def test_another_scope_mapping_the_same_key_is_untouched(self, store_factory):
        """Two profiles sharing one state.db produce the same key in different
        scopes for DIFFERENT sessions — hence the delete keys on session_id."""
        store = store_factory()
        db = store._db
        entry = store.get_or_create_session(_source())
        store.persist()
        db.create_session("other-profile-session", source="telegram")
        db.save_gateway_routing_entry(
            entry.session_key,
            json.dumps({"session_id": "other-profile-session"}),
            scope="/some/other/profile/sessions",
        )

        db.set_session_archived(entry.session_id, True)

        other = db.load_gateway_routing_entries(scope="/some/other/profile/sessions")
        assert entry.session_key in other
        assert entry.session_key not in db.load_gateway_routing_entries(
            scope=store._routing_scope()
        )


class TestArchiveStaleSweepUsesTheSamePath:
    def test_the_automated_sweep_also_retires_routing(self, store_factory):
        """``archive_stale_sessions`` routes through ``set_session_archived``.

        This is the scale case the blast radius of "1 row today" understates:
        enabling ``sessions.auto_archive`` would otherwise manufacture orphans
        in bulk.
        """
        store = store_factory()
        db = store._db
        entry = store.get_or_create_session(_source())
        store.persist()
        old = time.time() - 30 * 86400
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (old, entry.session_id)
        )
        db._conn.commit()

        assert db.archive_stale_sessions(3) >= 1

        assert entry.session_key not in _durable_routing_keys(store)
