"""A wake's want-lane must be canonicalized exactly like the origin's lane.

Incident (2026-09-12, Discord #curator): a ``kanban_notify_subs`` row carrying
``chat_type='channel'`` for a Discord GUILD chat could never match any live
routing entry, because ``_live_chat_participants`` compares

    origin_lane = effective_routing_lane(...)     # canonicalized: channel -> group
    want_lane   = (platform, chat, want_chat_type, want_thread)   # RAW string

Only one side of the compared pair went through ``canonical_chat_type``. For a
Discord guild chat the origin always canonicalizes to ``group``, so a
``channel``-spelled subscription yielded a 0% match rate — structurally, not
intermittently. Zero evidence means ``resolve_wake_identity`` refuses, the wake
keys an identity-less session, and the phantom/shadow session is minted: a
second session replying into the user's channel at config-default model and
reasoning effort, which the per-session route-change announce cannot see.

#659 added the source-side lint (no adapter may LABEL a Discord chat
``channel``). That covers code. This covers the DATA axis: rows already
persisted with the legacy spelling, and any non-adapter producer.
"""
from datetime import datetime, timezone

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.routing_identity import canonical_chat_type, effective_routing_lane
from gateway.session import SessionEntry, SessionSource


DISCORD_GUILD_CHAT = "1514857406025306212"
PARTICIPANT = "117431298246705156"


@pytest.mark.parametrize("sub_chat_type", ["channel", "group"])
def test_both_spellings_of_a_discord_guild_chat_yield_one_lane(sub_chat_type):
    """The whole bug in one assertion: both spellings must agree.

    A subscription row and the live origin entry describe the SAME chat. If the
    two spellings produce different lanes, the wake finds no evidence and mints
    a phantom.
    """
    origin_lane = effective_routing_lane(
        platform="discord", chat_id=DISCORD_GUILD_CHAT, chat_type="channel",
    )
    want_lane = effective_routing_lane(
        platform="discord", chat_id=DISCORD_GUILD_CHAT, chat_type=sub_chat_type,
    )
    assert origin_lane == want_lane, (
        f"a {sub_chat_type!r}-spelled subscription cannot match its own chat's "
        "origin entry — this is the 2026-09-12 shadow-session bug"
    )
    assert origin_lane[2] == "group"


def test_raw_want_lane_construction_is_the_regression():
    """Negative control: pin the BROKEN construction so it can't come back.

    This is the exact tuple the pre-fix ``_live_chat_participants`` built. It is
    asserted UNEQUAL to the canonicalized origin lane, which documents why the
    raw form must never be used as a comparison operand.
    """
    origin_lane = effective_routing_lane(
        platform="discord", chat_id=DISCORD_GUILD_CHAT, chat_type="channel",
    )
    raw_want_lane = ("discord", DISCORD_GUILD_CHAT, "channel", "")
    assert origin_lane != raw_want_lane, (
        "if these are equal the canonicalizer stopped rewriting discord/channel "
        "and this whole guard is inert"
    )


def test_non_discord_channel_platforms_are_untouched():
    """Teams/Telegram/HA own ``channel`` as a real type — must NOT become group.

    A fix that canonicalized every platform's ``channel`` would silently
    re-lane three platforms whose canonical type genuinely is ``channel``
    (see ALLOWED_CHANNEL_LITERALS in test_session_key_producer_contract).
    """
    for platform in ("teams", "telegram", "homeassistant"):
        lane = effective_routing_lane(
            platform=platform, chat_id="C1", chat_type="channel",
        )
        assert lane[2] == "channel", f"{platform} lost its real channel type"
        assert canonical_chat_type(platform, "channel") == "channel"


def test_thread_lane_still_wins_over_chat_type():
    """Guard the branch order: a thread lane must not be flattened to group."""
    lane = effective_routing_lane(
        platform="discord", chat_id=DISCORD_GUILD_CHAT, chat_type="channel",
        thread_id="T9",
    )
    assert lane == ("discord", DISCORD_GUILD_CHAT, "group", "T9")


# ---------------------------------------------------------------------------
# Call-site reproduction: the helper above is correct; the BUG is that only one
# operand of the comparison in _live_chat_participants was routed through it.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal stand-in for the gateway session store's routing index."""

    def __init__(self, entries):
        import threading

        self._lock = threading.RLock()
        self._entries = entries

    def _ensure_loaded_locked(self):
        return None


class _Watcher(GatewayKanbanWatchersMixin):
    def __init__(self, entries):
        self.session_store = _FakeStore(entries)


def _live_discord_guild_entry():
    """The routing entry a real inbound Discord guild message produces."""
    now = datetime.now(timezone.utc)
    key = f"agent:main:discord:group:{DISCORD_GUILD_CHAT}:{PARTICIPANT}"
    return key, SessionEntry(
        session_key=key,
        session_id="20260910_190352_eca8dd",
        created_at=now,
        updated_at=now,
        origin=SessionSource(
            platform=Platform.DISCORD,
            chat_id=DISCORD_GUILD_CHAT,
            chat_type="group",
            user_id=PARTICIPANT,
        ),
        chat_type="group",
    )


@pytest.mark.parametrize("sub_chat_type", ["group", "channel"])
def test_legacy_channel_spelled_subscription_still_finds_its_participant(sub_chat_type):
    """🔴 THE REGRESSION.

    A ``kanban_notify_subs`` row spelled ``channel`` describes the very same
    Discord guild chat as the live ``group`` routing entry. Before the fix the
    ``channel`` case returned an EMPTY set — 0% match rate, structurally — so
    the wake had no identity, keyed a bare ``group:<chat>`` session, and minted
    the shadow session that replied into #curator at the config-default model
    and reasoning effort (2026-09-12).
    """
    key, entry = _live_discord_guild_entry()
    watcher = _Watcher({key: entry})

    found = watcher._live_chat_participants(
        Platform.DISCORD, DISCORD_GUILD_CHAT, None, sub_chat_type, None,
    )

    assert {i.participant for i in found} == {PARTICIPANT}, (
        f"a {sub_chat_type!r}-spelled subscription found no participant for a "
        "chat the gateway has already resolved — the wake will mint a phantom"
    )


def test_a_genuinely_different_chat_is_still_refused():
    """Negative control: the fix must not make the lane match promiscuous."""
    key, entry = _live_discord_guild_entry()
    watcher = _Watcher({key: entry})
    assert watcher._live_chat_participants(
        Platform.DISCORD, "999999999999", None, "channel", None,
    ) == set()


def test_a_dm_lane_is_not_adopted_by_a_channel_spelled_subscription():
    """Negative control: canonicalization must not collapse dm into group."""
    key, entry = _live_discord_guild_entry()
    watcher = _Watcher({key: entry})
    assert watcher._live_chat_participants(
        Platform.DISCORD, DISCORD_GUILD_CHAT, None, "dm", None,
    ) == set()
