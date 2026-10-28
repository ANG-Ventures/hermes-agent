"""SessionStore: Matrix routes that follow a room's shape (dm <-> group, per-user -> per-room).

A Matrix room's chat type is derived from its joined-member count (``MatrixAdapter.
_resolve_room_identity``: <= 2 members is a DM). The type therefore CHANGES when a third member
joins or leaves, while the routing index refuses a second type for the same chat (the type-free
owner in ``routing_identity``). Without a migration the room's first message after the flip died
with ``SessionKeyConflict`` and every later one did too (2026-10-28, room "Packaging materials
quote").

Decision: the key follows the room's CURRENT shape and the conversation follows the room. On the
first message under the new shape, the old route is folded onto the new key and its session
continues. The type is not pinned to the first route: a 3-member room keyed ``dm`` would read as a
private 1:1 (no sender attribution, DM gating), which misdescribes the room to the agent. A room
that drops back to 2 members folds back the same way; there is only ever one live route, so the
pair cannot loop.

The same fold retires per-user group rows once ``matrix.group_sessions_per_user`` is false (one
shared conversation per room): at load when that is conflict-free, else on the room's next message.
Mixin split out of ``gateway/session.py``; bound onto ``SessionStore`` via the MRO.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from gateway.config import Platform, session_isolation_flags

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

logger = logging.getLogger("gateway.session")

_ROOM_SHAPES = ("dm", "group")


def _matrix_ids(tail: str) -> list:
    """Split a key tail (``!room[:server][:$event][:@user:server]``) into Matrix ids. Ids are
    sigil-typed (``!`` room, ``$`` event, ``@`` user) and may themselves contain ``:server``."""
    ids: list = []
    for token in tail.split(":"):
        if token[:1] in ("!", "$", "@") or not ids:
            ids.append(token)
        else:
            ids[-1] += ":" + token
    return ids


class SessionRoomShapeMixin:
    """Fold a Matrix room's routes onto the key its current shape produces."""

    def _matrix_route_key_as(self, key: str, entry: Optional["SessionEntry"], chat_type: str) -> Optional[str]:
        """The key *key*'s conversation would have as a *chat_type* room under the current
        isolation flags, or None when *key* is not a Matrix dm/group route."""
        from gateway.session import SessionSource, build_session_key, profile_from_session_key_namespace

        parts = str(key or "").split(":", 4)
        if len(parts) < 5 or parts[0] != "agent" or parts[2] != Platform.MATRIX.value:
            return None
        if parts[3] not in _ROOM_SHAPES or chat_type not in _ROOM_SHAPES:
            return None
        ids = _matrix_ids(parts[4])
        if not ids or not ids[0].startswith("!"):
            return None
        origin = getattr(entry, "origin", None)
        if origin is not None and str(origin.chat_id or "") == ids[0]:
            source = replace(origin, chat_type=chat_type)
        else:
            source = SessionSource(
                platform=Platform.MATRIX, chat_id=ids[0], chat_type=chat_type,
                user_id=next((i for i in ids[1:] if i.startswith("@")), None),
                thread_id=next((i for i in ids[1:] if i.startswith("$")), None),
            )
        group_per_user, thread_per_user = session_isolation_flags(self.config, Platform.MATRIX)
        return build_session_key(
            source, group_sessions_per_user=group_per_user, thread_sessions_per_user=thread_per_user,
            profile=profile_from_session_key_namespace(parts[1]),
        )

    def _room_shape_canonical_key(self, key: str, entry: Optional["SessionEntry"]) -> Optional[str]:
        """Load-time: the same-type key for a Matrix route keyed under stale isolation flags
        (a per-user group row once the room is shared), else None. Never changes the type."""
        parts = str(key or "").split(":", 4)
        if len(parts) < 5:
            return None
        canonical = self._matrix_route_key_as(key, entry, parts[3])
        return canonical if canonical and canonical != key else None

    def _adopt_room_shape_aliases(self, source: "SessionSource", session_key: str) -> int:
        """Fold every route of *source*'s room that belongs on *session_key* (the other shape,
        or a stale per-user spelling) onto it before the route transition writes. Returns the
        number of canonical routes touched (0 or 1)."""
        if source.platform != Platform.MATRIX:
            return 0
        parts = str(session_key or "").split(":", 4)
        if len(parts) < 5 or parts[0] != "agent" or parts[3] not in _ROOM_SHAPES:
            return 0
        room = _matrix_ids(parts[4])[0]
        lane = ":".join(parts[:3]) + ":"  # same namespace + platform; other profiles never fold
        with self._lock:
            self._ensure_loaded_locked()
            aliases = [
                (key, entry) for key, entry in self._entries.items()
                if key != session_key and key.startswith(lane) and key.count(":") >= 4
                and _matrix_ids(key.split(":", 4)[4])[0] == room
                and self._matrix_route_key_as(key, entry, parts[3]) == session_key
            ]
            if not aliases:
                return 0
            return self._fold_alias_groups_locked({session_key: aliases})
