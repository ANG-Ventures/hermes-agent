"""Tests for the inviter allowlist gate on pending-invite reconciliation.

``_on_invite`` only auto-joins when the inviter is allow-listed, but the
reconcile pass over sync's ``rooms.invite`` runs after event dispatch and
sees every pending invite — including ones ``_on_invite`` just rejected,
and ones that arrived while the gateway was down. An unconditional join
there re-admitted rejected live invites milliseconds later and auto-joined
arbitrary federated invites on restart. ``_schedule_pending_invite_joins``
must apply the same gate, reading the inviter from the stripped invite
state.
"""

import time
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


def _make_adapter():
    """Create a MatrixAdapter with mocked config and a one-user allowlist."""
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@hermes:example.org",
        },
    )
    adapter = MatrixAdapter(config)
    adapter._allowed_user_ids = {"@alice:example.org"}
    adapter._join_room_by_id = AsyncMock(return_value=True)
    adapter._record_dm_room = AsyncMock()
    return adapter


def _member_invite_event(
    state_key="@hermes:example.org",
    sender="@alice:example.org",
    is_direct=True,
    membership="invite",
):
    """Create a stripped m.room.member event as found in invite_state."""
    return {
        "type": "m.room.member",
        "state_key": state_key,
        "sender": sender,
        "content": {"membership": membership, "is_direct": is_direct},
    }


def _invite_sync_data(room_id="!pending_room:example.org", invite_state=None):
    """Create a sync payload with one pending invite room."""
    room = {} if invite_state is None else {"invite_state": invite_state}
    return {"rooms": {"invite": {room_id: room}}, "next_batch": "s1"}


async def _drain_invite_tasks(adapter):
    """Await any tasks _schedule_invite_join spawned."""
    for task in list(adapter._invite_join_tasks.values()):
        await task


class TestPendingInviteAuthorization:
    """_schedule_pending_invite_joins applies _on_invite's inviter gate.

    Rejection mirrors _on_invite exactly: no join is scheduled (so no
    entry lands in _invite_join_tasks), nothing is recorded in m.direct,
    and the pending invite is otherwise left untouched.
    """

    @pytest.mark.asyncio
    async def test_allowed_inviter_is_joined(self):
        adapter = _make_adapter()

        sync_data = _invite_sync_data(invite_state={"events": [_member_invite_event()]})
        adapter._schedule_pending_invite_joins(sync_data)
        await _drain_invite_tasks(adapter)

        adapter._join_room_by_id.assert_awaited_once_with("!pending_room:example.org")
        adapter._record_dm_room.assert_awaited_once_with(
            "!pending_room:example.org", "@alice:example.org"
        )

    @pytest.mark.parametrize(
        "invite_state",
        [
            pytest.param(
                {"events": [_member_invite_event(sender="@mallory:evil.example")]},
                id="non-allowed-inviter",
            ),
            pytest.param(None, id="no-invite-state"),
            pytest.param({"events": []}, id="empty-events"),
            pytest.param(
                {"events": [_member_invite_event(state_key="@other:example.org")]},
                id="member-event-for-other-user",
            ),
            pytest.param(
                {"events": [_member_invite_event(sender="")]},
                id="missing-inviter",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unauthorized_or_unknown_inviter_is_not_joined(self, invite_state):
        """An inviter outside the allowlist, or one that cannot be read
        from the stripped invite state at all, fails closed like
        _on_invite: no join is scheduled and nothing is recorded."""
        adapter = _make_adapter()

        sync_data = _invite_sync_data(invite_state=invite_state)
        adapter._schedule_pending_invite_joins(sync_data)
        await _drain_invite_tasks(adapter)

        adapter._join_room_by_id.assert_not_awaited()
        adapter._record_dm_room.assert_not_awaited()
        assert adapter._invite_join_tasks == {}


class _RunnerStub:
    """Minimal gateway runner: the real per-profile store resolution, nothing else."""

    def __init__(self, store, per_profile=None):
        self.pairing_store = store
        self.pairing_stores = dict(per_profile or {})

    from gateway.authz_mixin import GatewayAuthorizationMixin as _M
    _pairing_store_for = _M._pairing_store_for
    del _M


def _approved_store(tmp_path, monkeypatch, *user_ids):
    from gateway import pairing
    from gateway.pairing import PairingStore

    monkeypatch.setattr(pairing, "PAIRING_DIR", tmp_path)
    store = PairingStore()
    with store._lock:
        for uid in user_ids:
            store._approve_user("matrix", uid, "")
    return store


class TestInviteGateHonoursPairingStore:
    """A sender approved in the adapter profile's PairingStore after boot (absent from the
    boot-time MATRIX_ALLOWED_USERS snapshot) is joined on invite without a restart."""

    PAIRED = "@friend:example.org"

    def _invite(self, room_id, sender):
        from types import SimpleNamespace
        return SimpleNamespace(room_id=room_id, sender=sender,
                               content=SimpleNamespace(is_direct=True))

    @pytest.mark.asyncio
    async def test_paired_inviter_absent_from_env_is_joined(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        assert self.PAIRED not in adapter._allowed_user_ids
        adapter.gateway_runner = _RunnerStub(_approved_store(tmp_path, monkeypatch, self.PAIRED))

        await adapter._on_invite(self._invite("!dm:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_awaited_once_with("!dm:example.org")
        adapter._record_dm_room.assert_awaited_once_with("!dm:example.org", self.PAIRED)

        adapter._join_room_by_id.reset_mock()
        sync_data = _invite_sync_data(
            room_id="!pending:example.org",
            invite_state={"events": [_member_invite_event(sender=self.PAIRED)]})
        adapter._schedule_pending_invite_joins(sync_data)
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_awaited_once_with("!pending:example.org")

    @pytest.mark.asyncio
    async def test_grant_after_boot_takes_effect_without_restart(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        store = _approved_store(tmp_path, monkeypatch)
        adapter.gateway_runner = _RunnerStub(store)

        await adapter._on_invite(self._invite("!dm1:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_not_awaited()

        with store._lock:
            store._approve_user("matrix", self.PAIRED, "")
        await adapter._on_invite(self._invite("!dm2:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_awaited_once_with("!dm2:example.org")

    @pytest.mark.parametrize("inviter", ["@mallory:evil.example", "", "@_telegram_123:example.org"])
    @pytest.mark.asyncio
    async def test_unknown_empty_or_bridge_inviter_still_rejected(self, tmp_path, monkeypatch, inviter):
        adapter = _make_adapter()
        # Even an approved bridge identity is refused (echo-loop guard).
        adapter.gateway_runner = _RunnerStub(
            _approved_store(tmp_path, monkeypatch, self.PAIRED, "@_telegram_123:example.org"))

        await adapter._on_invite(self._invite("!dm:example.org", inviter))
        adapter._schedule_pending_invite_joins(_invite_sync_data(
            invite_state={"events": [_member_invite_event(sender=inviter)]}))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_not_awaited()
        assert adapter._invite_join_tasks == {}

    @pytest.mark.asyncio
    async def test_secondary_profile_uses_its_own_store(self, tmp_path, monkeypatch):
        """A secondary bot resolves its own profile's store, not the default one."""
        from gateway.pairing import PairingStore

        default_store = _approved_store(tmp_path / "default", monkeypatch, self.PAIRED)
        other = PairingStore.__new__(PairingStore)
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        import threading
        other._dir, other._lock, other._profile, other._approved_cache = other_dir, threading.RLock(), "other", {}
        adapter = _make_adapter()
        adapter.set_owner_profile("other")
        adapter.gateway_runner = _RunnerStub(default_store, {"other": other})

        await adapter._on_invite(self._invite("!dm:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_not_awaited()

        with other._lock:
            other._approve_user("matrix", self.PAIRED, "")
        await adapter._on_invite(self._invite("!dm:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_awaited_once_with("!dm:example.org")

    @pytest.mark.asyncio
    async def test_no_runner_fails_closed(self):
        adapter = _make_adapter()
        adapter.gateway_runner = None
        await adapter._on_invite(self._invite("!dm:example.org", self.PAIRED))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_not_awaited()
